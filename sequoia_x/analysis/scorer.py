"""候选股量化评分器：给策略选出的股票打多维量价分并排序。

为什么需要：
    策略的输出是「入选 / 未入选」的布尔结果，回答不了「这几十只里哪几只更值得看」。
    而本地行情库 `data/sequoia_v2.db` 已有全市场 600+ 个交易日的 OHLCV，
    足以在**零网络开销**的前提下算出一整套量价特征，把布尔结果变成可排序的分数。

数据口径（实测确认，勿改）：
    - `close` 是**后复权**价 ⇒ 涨跌幅 / 均线偏离 / 量比这类**比值指标正确**，
      但绝对价格不可直接展示；真实成交均价 = `turnover / volume`。
    - `volume` 是真实成交股数，`turnover` 是真实成交额（元）。

评分构成（基础分满分 100，五维各 20）：
    1. 策略共振  命中的策略越多越高；海龟 / RPS 属突破类信号，权重更高
    2. 量能配合  当日上涨且放量最佳，下跌放量最差
    3. 趋势强度  均线多头排列数 + 距区间高点位置
    4. 估值      PE(TTM) 越低越高
    5. 筹码      前十大流通股东合计占流通股比例越高越高

风险调整：
    `adjusted = 基础分 - 位置惩罚`，惩罚两件事：距 MA20 过度偏离（追高，10/6/3 分）、
    年化波动率过高（6/3 分）。动机是「越接近新高」不能无条件加分 ——
    静态字段版曾把「距 MA20 +55%、波动 103%」的股票排到第 2 名，
    扣分后掉到第 14 名，趋势末端的极端偏离必须惩罚。

可选增强（外部 stock-researcher 工具的纯离线函数，喂本地库价格序列）：
    形态匹配胜率 / 最大回撤。该工具不属于本项目依赖，因此做成**可选层**：
    路径未配置、目录不存在、导入报错、样本不足 —— 一律跳过并记 WARNING，
    主评分链路完全不受影响。增强只对排名靠前的若干只执行，以控制耗时。
"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.universe_filter import StockMetric

logger = get_logger(__name__)

# 每只股票回看的交易日数。250 ≈ 一年，够算 120 日均线与年内高点位置。
LOOKBACK = 250

# 少于该行数直接跳过：算不出 MA60 与区间位置，硬算会给出误导性分数。
# 130 = MA120 窗口(120) + 计算偏离所需的缓冲。
MIN_ROWS = 130

# SQLite 单条语句的参数个数上限保护（与 DataEngine 的做法保持一致）。
_SQL_PARAM_CHUNK = 900

# 近 N 个交易日用于估计日收益波动率。
_VOL_WINDOW = 20

_SERIES_SQL = """
    SELECT symbol, date, high, low, close, volume, turnover FROM (
        SELECT symbol, date, high, low, close, volume, turnover,
               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
        FROM stock_daily
        WHERE symbol IN ({placeholders})
    ) WHERE rn <= ? ORDER BY symbol, date
"""


@dataclass(frozen=True)
class ScoreDetail:
    """单只候选股的评分明细。

    量价特征全部来自本地库（零网络）。`prob_up` / `max_drawdown` 来自外部
    量化工具的增强层，未启用或计算失败时为 None。
    """

    symbol: str
    last_date: str
    tags: str  # 命中的策略标记字母，如 "TR"；无标记为空串
    # ── 量价特征 ──
    chg1: float | None  # 当日涨跌幅 %
    chg20: float | None
    chg60: float | None
    vol_ratio: float | None  # 当日量 / 前 5 日均量
    near_high: float | None  # 最新收盘 / 区间最高价，1.0 = 恰在最高点
    dev_ma20: float | None  # 距 MA20 偏离 %
    vol20: float | None  # 年化波动率 %
    streak: int  # 连涨天数
    bull: int  # 均线多头排列数（0~3）
    amount: float | None  # 最新成交额（亿元）
    new_high60: bool
    new_high120: bool
    # ── 评分 ──
    dims: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    penalty: float = 0.0
    adjusted: float = 0.0
    # ── 增强层（可选）──
    prob_up: float | None = None  # 形态匹配胜率 %
    expected_pct: float | None = None  # 形态匹配预期涨跌幅 %
    max_drawdown: float | None = None  # 区间最大回撤 %
    # 下面是胜率的两个「可信度陪衬」字段，缺了它们胜率就没法解读：
    # `prob_up` 是 k/n 的离散值，实测 n 常只有 3~8（例如「100%」= 3 次里涨了 3 次）。
    # 只给百分比时无法区分 3/3 与 5/5，也没法把「样本太少」的票排到后面。
    prob_samples: int | None = None  # 形态匹配的样本数（n_matches）
    prob_confidence: float | None = None  # 形态匹配可信度 0~100（相似度+样本量）

    @property
    def score(self) -> float:
        """对外展示用的分数（风险调整后，保留一位小数）。"""
        return round(self.adjusted, 1)


# ── 五维打分（各 20 分）──


def score_resonance(tags: str) -> float:
    """策略共振：命中数越多越高；海龟(T)/RPS(R) 是突破类信号，权重更高。"""
    has_turtle = "T" in tags
    has_rps = "R" in tags
    if has_turtle and has_rps:
        return 20.0
    if has_turtle or has_rps:
        return 14.0
    return 10.0


def score_volume(chg1: float | None, vol_ratio: float | None) -> float:
    """量能配合：涨且放量最佳；跌且放量最差。

    数据缺失时给中性分（12），不奖不罚 —— 宁可低估也不要用默认值制造假信号。
    """
    if chg1 is None or vol_ratio is None:
        return 12.0
    if chg1 > 0:
        if vol_ratio >= 1.3:
            return 20.0
        if vol_ratio >= 0.8:
            return 15.0
        return 12.0
    # 下跌：量越大越差（放量下跌 = 抛压）
    if vol_ratio < 1.0:
        return 10.0
    if vol_ratio < 1.3:
        return 7.0
    return 4.0


def score_trend(bull: int, near_high: float | None) -> float:
    """趋势强度：均线多头排列数（每项 4 分）+ 距区间高点位置（最高 8 分）。"""
    if near_high is None:
        return float(bull * 4)
    if near_high >= 0.98:
        position = 8.0
    elif near_high >= 0.90:
        position = 6.0
    elif near_high >= 0.80:
        position = 4.0
    else:
        position = 2.0
    return bull * 4.0 + position


def score_valuation(pe: float | None) -> float:
    """估值：PE(TTM) 越低越高。

    **负 PE（亏损股）单独判低分** —— 早期版本直接用 `pe < 40` 判断，
    负市盈率会被判成满分 20，是个隐蔽的反向 bug。当前精筛配置用
    `MIN_PE=0` 排除了亏损股，所以实践中不触发，但这里必须堵上。
    """
    if pe is None or not math.isfinite(pe):
        return 13.0
    if pe < 0:
        return 2.0
    if pe < 40:
        return 20.0
    if pe < 60:
        return 17.0
    if pe < 90:
        return 13.0
    if pe < 120:
        return 9.0
    if pe < 180:
        return 5.0
    return 2.0


def score_chip(hold: float | None) -> float:
    """筹码：前十大流通股东合计占流通股比例越高越高。"""
    if hold is None or not math.isfinite(hold):
        return 10.0
    if hold >= 75:
        return 20.0
    if hold >= 65:
        return 17.0
    if hold >= 55:
        return 13.0
    if hold >= 45:
        return 9.0
    return 5.0


def position_penalty(dev_ma20: float | None, vol20: float | None) -> float:
    """位置惩罚：追高（距 MA20 过度偏离）与高波动各扣一次。"""
    penalty = 0.0
    if dev_ma20 is not None:
        if dev_ma20 > 40:
            penalty += 10.0
        elif dev_ma20 > 30:
            penalty += 6.0
        elif dev_ma20 > 20:
            penalty += 3.0
    if vol20 is not None:
        if vol20 > 100:
            penalty += 6.0
        elif vol20 > 80:
            penalty += 3.0
    return penalty


# ── 本地库读取 ──


def read_price_series(
    db_path: str, symbols: Iterable[str], lookback: int = LOOKBACK
) -> dict[str, list[tuple]]:
    """批量读取每只股票最近 `lookback` 个交易日的 OHLCV。

    一次窗口函数查完（超过 900 只自动分批），比逐股查询少读几十倍数据。
    只读方式打开数据库，不干扰正在进行的同步写入。

    Returns:
        {symbol: [(date, high, low, close, volume, turnover), ...]}，按日期**升序**。
        无数据的股票不在字典中。
    """
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return {}

    result: dict[str, list[tuple]] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        for i in range(0, len(wanted), _SQL_PARAM_CHUNK):
            batch = wanted[i : i + _SQL_PARAM_CHUNK]
            placeholders = ",".join("?" * len(batch))
            sql = _SERIES_SQL.format(placeholders=placeholders)
            for row in conn.execute(sql, [*batch, lookback]):
                result.setdefault(row[0], []).append(row[1:])
    finally:
        conn.close()
    return result


# ── 特征计算 ──


def _change(closes: list[float], k: int) -> float | None:
    """k 个交易日前的涨跌幅（%）。数据不足返回 None。"""
    if len(closes) <= k:
        return None
    base = closes[-1 - k]
    if not base:
        return None
    return (closes[-1] / base - 1) * 100


def _ma(closes: list[float], k: int) -> float | None:
    return statistics.mean(closes[-k:]) if len(closes) >= k else None


def _annual_vol(closes: list[float], window: int = _VOL_WINDOW) -> float | None:
    """近 `window` 个交易日日收益的年化标准差（%）。"""
    start = max(1, len(closes) - window)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(start, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(252) * 100


def _streak(closes: list[float]) -> int:
    """连涨天数。"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            count += 1
        else:
            break
    return count


def build_detail(
    symbol: str,
    rows: list[tuple],
    *,
    metric: StockMetric | None = None,
    holding: float | None = None,
    tags: str = "",
) -> ScoreDetail | None:
    """由价格序列算出单只股票的评分明细；数据不足返回 None。"""
    if len(rows) < MIN_ROWS:
        return None

    highs = [r[1] for r in rows]
    closes = [r[3] for r in rows]
    vols = [r[4] for r in rows]
    amounts = [r[5] for r in rows]

    m5, m10, m20, m60 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20), _ma(closes, 60)
    pairs = ((m5, m10), (m10, m20), (m20, m60))
    bull = sum(1 for a, b in pairs if a is not None and b is not None and a > b)

    chg1 = _change(closes, 1)
    prev5 = statistics.mean(vols[-6:-1]) if len(vols) >= 6 else None
    vol_ratio = (vols[-1] / prev5) if prev5 else None

    peak = max(highs) if highs else None
    near_high = (closes[-1] / peak) if peak else None
    dev_ma20 = ((closes[-1] / m20 - 1) * 100) if m20 else None

    dims = {
        "策略共振": score_resonance(tags),
        "量能配合": score_volume(chg1, vol_ratio),
        "趋势强度": score_trend(bull, near_high),
        "估值": score_valuation(metric.pe_ttm if metric else None),
        "筹码": score_chip(holding),
    }
    total = sum(dims.values())
    vol20 = _annual_vol(closes)
    penalty = position_penalty(dev_ma20, vol20)

    return ScoreDetail(
        symbol=symbol,
        last_date=rows[-1][0],
        tags=tags,
        chg1=chg1,
        chg20=_change(closes, 20),
        chg60=_change(closes, 60),
        vol_ratio=vol_ratio,
        near_high=near_high,
        dev_ma20=dev_ma20,
        vol20=vol20,
        streak=_streak(closes),
        bull=bull,
        amount=(amounts[-1] / 1e8) if amounts and amounts[-1] is not None else None,
        new_high60=len(closes) >= 60 and closes[-1] >= max(closes[-60:]),
        new_high120=len(closes) >= 120 and closes[-1] >= max(closes[-120:]),
        dims=dims,
        total=total,
        penalty=penalty,
        adjusted=total - penalty,
    )


# ── 可选增强层（外部量化工具）──


def load_quant_skill(skill_path: str):
    """尽力加载外部量化工具的离线函数；不可用时返回 None。

    该工具（stock-researcher）通常装在 `~/.workbuddy/skills/` 下，
    **不属于本项目的依赖** —— 所以这里刻意做成可选：
    未配置路径 / 目录不存在 / 导入报错，一律返回 None 并记 WARNING，
    评分主链路不受任何影响。
    """
    if not skill_path:
        return None
    if not os.path.isdir(skill_path):
        logger.warning(f"评分增强：量化工具路径不存在，已跳过 → {skill_path}")
        return None
    if skill_path not in sys.path:
        sys.path.insert(0, skill_path)
    try:
        from stock_researcher import (  # type: ignore[import-not-found]
            drawdown_report,
            quick_pattern_forecast,
        )
    except Exception as exc:
        logger.warning(f"评分增强：导入 stock_researcher 失败，已跳过（{exc}）")
        return None
    return quick_pattern_forecast, drawdown_report


def _enhance(detail: ScoreDetail, closes: list[float], funcs) -> ScoreDetail:
    """给单只股票补充形态胜率、可信度、样本数与最大回撤；任一步失败则保持原值。"""
    quick_pattern_forecast, drawdown_report = funcs
    prob_up = expected = max_dd = None
    samples = confidence = None

    try:
        fc = quick_pattern_forecast(closes, horizon=5)
        if isinstance(fc, dict):
            raw = fc.get("prob_up")
            # 实测口径（2026-09-23 真库验证）：`prob_up` 已经是 0~100 的百分数
            # （实测到 0.0 / 33.3 / 66.7），且 `data_mode == "ok"` 表示是真实计算
            # 而非降级值。**不要做「<=1 就乘 100」的猜测性换算** ——
            # 真实值 0.5（即 0.5%）会被误放大成 50%。
            if raw is not None and fc.get("data_mode") in (None, "ok"):
                prob_up = float(raw)
                # 样本数与可信度和 prob_up 同源，必须一起取、一起受 data_mode 约束，
                # 否则会出现「胜率是真实值、样本数是降级值」的错配。
                n = fc.get("n_matches")
                samples = int(n) if n is not None else None
                conf = fc.get("confidence")
                confidence = float(conf) if conf is not None else None
            exp = fc.get("predicted_pct")
            expected = float(exp) if exp is not None else None
    except Exception as exc:
        logger.debug(f"评分增强：形态匹配失败（{detail.symbol}）：{exc}")

    try:
        dd = drawdown_report(closes)
        if isinstance(dd, dict) and dd.get("max_drawdown") is not None:
            max_dd = float(dd["max_drawdown"]) * 100
    except Exception as exc:
        logger.debug(f"评分增强：回撤剖析失败（{detail.symbol}）：{exc}")

    if prob_up is None and max_dd is None:
        return detail
    return replace(
        detail,
        prob_up=prob_up,
        expected_pct=expected,
        max_drawdown=max_dd,
        prob_samples=samples,
        prob_confidence=confidence,
    )


# ── 对外主入口 ──


def score_pool(
    symbols: Iterable[str],
    *,
    db_path: str,
    metrics: Mapping[str, StockMetric] | None = None,
    holdings: Mapping[str, float] | None = None,
    tags: Mapping[str, str] | None = None,
    lookback: int = LOOKBACK,
    enhance_top: int = 0,
    skill_path: str = "",
) -> list[ScoreDetail]:
    """对候选池打分并按**风险调整后得分**降序返回。

    Args:
        symbols: 候选股票代码。
        db_path: 本地行情库路径。
        metrics: 可选 {代码: StockMetric}，提供 PE 用于估值维度。
        holdings: 可选 {代码: 前十大流通股东合计占比 %}，提供筹码维度。
        tags: 可选 {代码: 策略标记字母}，提供策略共振维度。
        lookback: 每只股票回看的交易日数。
        enhance_top: 仅对排名前 N 名调用外部量化工具做增强；0 = 不增强。
        skill_path: 外部量化工具根目录；留空则不增强。

    Returns:
        ScoreDetail 列表，按 adjusted 降序。历史数据不足的股票被跳过。
    """
    metrics = metrics or {}
    holdings = holdings or {}
    tags = tags or {}

    series = read_price_series(db_path, symbols, lookback)
    if not series:
        return []

    details: list[ScoreDetail] = []
    skipped = 0
    for symbol, rows in series.items():
        detail = build_detail(
            symbol,
            rows,
            metric=metrics.get(symbol),
            holding=holdings.get(symbol),
            tags=tags.get(symbol, ""),
        )
        if detail is None:
            skipped += 1
        else:
            details.append(detail)

    if skipped:
        logger.info(f"评分：{skipped} 只历史数据不足 {MIN_ROWS} 行，未参与评分")

    details.sort(key=lambda d: (-d.adjusted, d.symbol))

    if enhance_top > 0 and skill_path:
        funcs = None
        try:
            funcs = load_quant_skill(skill_path)
        except Exception as exc:  # 可选层，绝不允许拖垮基础分
            logger.warning(f"评分增强：加载量化工具时异常，已跳过（{exc}）")
        if funcs is not None:
            enhanced = 0
            for i, detail in enumerate(details):
                if i >= enhance_top:
                    break
                rows = series.get(detail.symbol) or []
                if len(rows) < 60:
                    continue
                details[i] = _enhance(detail, [r[3] for r in rows], funcs)
                if details[i].prob_up is not None or details[i].max_drawdown is not None:
                    enhanced += 1
            top = min(enhance_top, len(details))
            logger.info(f"评分增强：已对前 {top} 名补充形态/回撤，成功 {enhanced} 只")

    return details


# ── 结构归纳 ──

# `summarize()` 的分组名。刻意用固定字面量而不是「距高<5%」这类带阈值的字符串：
# 阈值将来要调，键名不该跟着变（调用方与测试都按这些键取值）。
SHAPE_NEAR_HIGH = "接近新高"
SHAPE_DEEP_PULLBACK = "深度回撤"
SHAPE_VOLUME_RALLY = "放量上涨"
SHAPE_OVEREXTENDED = "重度追高"
SHAPE_PENALISED = "被扣分"

_NEAR_HIGH_MIN = 0.95
_PULLBACK_MAX = 0.90
_VOLUME_RALLY_MIN = 1.3
_OVEREXTENDED_MIN = 40.0


def summarize(details: Iterable[ScoreDetail]) -> dict[str, list[ScoreDetail]]:
    """按「形态」把评分结果分组，回答「这批票是同一类机会还是混着两类」。

    为什么值得单独一个函数：候选池经常出现**双峰** —— 一头是「接近新高」的突破型，
    另一头是「深度回撤」的超跌反弹型，两者性质完全不同，混在一张排行榜里看会误判。
    报告与飞书给的是逐只明细（排行/分数），这里是**结构性判断**，互补而非重复。

    只返回明细、不返回名称：分析层不该知道股票名称从哪来（与策略标记同一原则）。

    Returns:
        {分组名: [ScoreDetail, ...]}，缺失数据的股票**不计入任何分组**（不猜）。
    """
    items = list(details)
    return {
        SHAPE_NEAR_HIGH: [
            d for d in items if d.near_high is not None and d.near_high >= _NEAR_HIGH_MIN
        ],
        SHAPE_DEEP_PULLBACK: [
            d for d in items if d.near_high is not None and d.near_high < _PULLBACK_MAX
        ],
        SHAPE_VOLUME_RALLY: [
            d for d in items if (d.chg1 or 0) > 0 and (d.vol_ratio or 0) >= _VOLUME_RALLY_MIN
        ],
        SHAPE_OVEREXTENDED: [
            d for d in items if d.dev_ma20 is not None and d.dev_ma20 > _OVEREXTENDED_MIN
        ],
        SHAPE_PENALISED: [d for d in items if d.penalty > 0],
    }


# ── 配置驱动的入口（每日流程与离线重算共用）──


def score_from_settings(
    symbols: Iterable[str],
    *,
    settings: Settings,
    metrics: Mapping[str, StockMetric] | None = None,
    holdings: Mapping[str, float] | None = None,
    tags: Mapping[str, str] | None = None,
) -> list[ScoreDetail]:
    """按配置开关打分，`main.py`（每日）与 `scripts/regen_report.py`（离线重算）共用。

    把「配置怎么读」收敛到一处。开关组合与那条容易忘的约定（**展示范围必须等于
    增强范围**，否则报告里排到后面几名的「胜率/回撤」列会是空的）散落在两个入口里，
    迟早会各改各的而漂移。

    不抛异常、不记日志 —— 调用方决定「评分失败要不要影响主流程」。
    评分关闭或无候选时返回空列表，语义等同于「本次没有评分」。
    """
    if not settings.score_enabled:
        return []
    wanted = list(symbols)
    if not wanted:
        return []

    limit = settings.score_top_n or len(wanted)
    ranking = score_pool(
        wanted,
        db_path=settings.db_path,
        metrics=metrics,
        holdings=holdings,
        tags=tags,
        enhance_top=limit if settings.score_enhance else 0,
        skill_path=settings.quant_skill_path if settings.score_enhance else "",
    )
    # 0 = 不限制（展示与增强都全量）；非零才截断
    return ranking[: settings.score_top_n] if settings.score_top_n else ranking

