"""股票池过滤器：对策略初选结果做统一的多维度精筛。

支持四个维度，全部可选（未配置即该维度不参与过滤）：
    1. 估值   —— 流通市值区间、市盈率(TTM)区间、市净率区间
    2. 流动性 —— 成交额下限（取自本地库，无需联网）
    3. 技术面 —— 换手率区间
    4. 行业   —— 行业关键词白名单 / 黑名单（子串匹配）

数据来源与成本：
    - 流通市值 / 市盈率 / 市净率 / 换手率：baostock 不复权日线，**只对候选股**查询，
      进程内缓存，多策略共享。
    - 成交额：本地 SQLite 一次批量查询（窗口函数），零网络开销。
    - 行业：baostock `query_stock_industry()`，整市场一次请求 + 进程内缓存。
    - **四个维度都未配置时完全不产生网络请求。**

容错策略（重要）：
    - 某一数据源「完全拉取失败」时，只跳过该维度的判据并记 ERROR，
      其余维度照常生效 —— 避免单个数据源故障把全部股票误杀成空结果。
    - 单只股票某个指标缺失，视为不通过该维度（例如亏损股 peTTM 为空）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta as stock_meta_module

logger = get_logger(__name__)

YI = 1e8  # 1 亿元

# 回看窗口天数：覆盖周末与连续假期，取窗口内最后一个有数据的交易日
_LOOKBACK_DAYS = 15

# 进程内缓存：{symbol: StockMetric}，同一次运行内多策略共享
_METRIC_CACHE: dict[str, StockMetric] = {}


@dataclass(frozen=True)
class StockMetric:
    """候选股票的估值与技术面指标。

    Attributes:
        symbol: 纯数字股票代码。
        circ_market_cap: 流通市值，单位「元」。
        pe_ttm: 滚动市盈率。
        pb: 市净率。
        turn: 换手率，单位「%」。
        各项数据不可用时为 None。
    """

    symbol: str
    circ_market_cap: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    turn: float | None = None


def calc_circ_market_cap(close: float, volume: float, turn: float) -> float | None:
    """由不复权收盘价、成交量、换手率推算流通市值（单位：元）。

    流通股本 = 成交量 / (换手率% / 100)，流通市值 = 流通股本 × 不复权收盘价。

    Args:
        close: 不复权收盘价。
        volume: 成交量（股）。
        turn: 换手率（百分比，如 0.1554 表示 0.1554%）。

    Returns:
        流通市值（元）；数据非法时返回 None。
    """
    if turn <= 0 or volume <= 0 or close <= 0:
        return None
    circulating_shares = volume / (turn / 100)
    return circulating_shares * close


def split_keywords(raw: str) -> list[str]:
    """把逗号分隔的关键词串拆成列表，兼容中英文逗号与多余空白。"""
    if not raw:
        return []
    normalized = raw.replace("，", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


@dataclass
class _Context:
    """单次 apply() 的运行时上下文：哪些维度生效、已有数据、剔除原因统计。

    维度开关与「配置是否填写」分离，是为了在数据源整体失败时把对应维度真正关掉，
    避免退化成「全部因数据缺失被剔除」。
    """

    valuation: bool
    liquidity: bool
    technical: bool
    industry: bool
    metrics: dict[str, StockMetric]
    turnovers: dict[str, float]
    metas: dict
    reasons: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        """记录一次剔除原因。"""
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class UniverseFilter:
    """按估值 / 流动性 / 技术面 / 行业对候选股票做统一过滤。"""

    def __init__(self, settings: Settings, engine: object | None = None) -> None:
        """
        Args:
            settings: 提供各维度阈值。
            engine: DataEngine 实例，用于成交额快照查询与代码转换。允许为 None（便于测试）。
        """
        self.settings = settings
        self.engine = engine

    # ── 配置状态 ──

    @property
    def _include_industries(self) -> list[str]:
        return split_keywords(self.settings.include_industries)

    @property
    def _exclude_industries(self) -> list[str]:
        return split_keywords(self.settings.exclude_industries)

    @property
    def _valuation_enabled(self) -> bool:
        s = self.settings
        return any(
            v is not None
            for v in (
                s.min_market_cap,
                s.max_market_cap,
                s.min_pe,
                s.max_pe,
                s.min_pb,
                s.max_pb,
            )
        )

    @property
    def _liquidity_enabled(self) -> bool:
        return self.settings.min_turnover is not None

    @property
    def _technical_enabled(self) -> bool:
        return self.settings.min_turn is not None or self.settings.max_turn is not None

    @property
    def _industry_enabled(self) -> bool:
        return bool(self._include_industries or self._exclude_industries)

    @property
    def enabled(self) -> bool:
        """是否启用了任一维度的过滤。"""
        return (
            self._valuation_enabled
            or self._liquidity_enabled
            or self._technical_enabled
            or self._industry_enabled
        )

    def describe(self) -> str:
        """返回人类可读的过滤条件描述，用于日志与报告页首。"""
        if not self.enabled:
            return "精筛：未启用"

        s = self.settings
        parts: list[str] = []

        def rng(lo, hi, unit: str = "") -> str:
            if lo is not None and hi is not None:
                return f"{lo:g}~{hi:g}{unit}"
            if lo is not None:
                return f">={lo:g}{unit}"
            return f"<={hi:g}{unit}"

        if s.min_market_cap is not None or s.max_market_cap is not None:
            parts.append(f"流通市值 {rng(s.min_market_cap, s.max_market_cap, '亿')}")
        if s.min_pe is not None or s.max_pe is not None:
            parts.append(f"市盈率TTM {rng(s.min_pe, s.max_pe)}")
        if s.min_pb is not None or s.max_pb is not None:
            parts.append(f"市净率 {rng(s.min_pb, s.max_pb)}")
        if self._liquidity_enabled:
            parts.append(f"成交额 >={s.min_turnover:g}亿")
        if self._technical_enabled:
            parts.append(f"换手率 {rng(s.min_turn, s.max_turn, '%')}")
        inc, exc = self._include_industries, self._exclude_industries
        if inc:
            parts.append("行业含 " + "/".join(inc))
        if exc:
            parts.append("排除行业 " + "/".join(exc))

        return "精筛：" + "，".join(parts)

    # ── 数据获取 ──

    def _to_baostock_code(self, symbol: str) -> str:
        if self.engine is not None:
            return self.engine._to_baostock_code(symbol)  # type: ignore[attr-defined]
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    def fetch_metrics(self, symbols: list[str]) -> dict[str, StockMetric]:
        """批量拉取候选股的估值与技术面指标（含进程内缓存）。"""
        result: dict[str, StockMetric] = {}
        todo: list[str] = []

        for symbol in symbols:
            cached = _METRIC_CACHE.get(symbol)
            if cached is not None:
                result[symbol] = cached
            elif symbol not in todo:
                todo.append(symbol)

        if not todo:
            return result

        fetched = self._fetch_from_baostock(todo)
        _METRIC_CACHE.update(fetched)
        result.update(fetched)
        return result

    def _fetch_from_baostock(self, symbols: list[str]) -> dict[str, StockMetric]:
        """通过 baostock 批量拉取指标。整批共用一个登录会话。"""
        import baostock as bs

        today = date.today()
        start = (today - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        metrics: dict[str, StockMetric] = {}

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"精筛：baostock 登录失败 {lg.error_msg}")
            return metrics

        try:
            for symbol in symbols:
                try:
                    rs = bs.query_history_k_data_plus(
                        self._to_baostock_code(symbol),
                        "date,close,volume,turn,peTTM,pbMRQ",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="3",  # 不复权：市值/估值必须用真实价格
                    )
                    if rs.error_code != "0":
                        continue

                    last_row: list[str] | None = None
                    while rs.next():
                        last_row = rs.get_row_data()
                    if last_row is None:
                        continue

                    _date, close_s, volume_s, turn_s, pe_s, pb_s = last_row

                    def num(value: str) -> float:
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            return float("nan")

                    close, volume, turn = num(close_s), num(volume_s), num(turn_s)
                    pe, pb = num(pe_s), num(pb_s)

                    def clean(value: float) -> float | None:
                        return None if value != value else value  # NaN -> None

                    metrics[symbol] = StockMetric(
                        symbol=symbol,
                        circ_market_cap=calc_circ_market_cap(close, volume, turn),
                        pe_ttm=clean(pe),
                        pb=clean(pb),
                        turn=clean(turn),
                    )
                except Exception as exc:  # 单只失败不影响整批
                    logger.warning(f"精筛：[{symbol}] 指标拉取失败 {exc}")
                    continue
        finally:
            bs.logout()

        return metrics

    def _fetch_turnover(self, symbols: list[str]) -> dict[str, float]:
        """取候选股最新交易日的成交额（元），来源为本地库，无网络开销。"""
        if self.engine is None:
            return {}
        try:
            snapshot = self.engine.get_latest_snapshot(symbols)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"精筛：本地成交额读取失败 {exc}")
            return {}
        return {
            symbol: row["turnover"]
            for symbol, row in snapshot.items()
            if row.get("turnover") is not None
        }

    # ── 过滤 ──

    def apply(self, symbols: list[str]) -> list[str]:
        """对候选股票列表应用全部已配置的过滤维度。

        Args:
            symbols: 策略选出的候选股票代码列表。

        Returns:
            过滤后的代码列表。未启用过滤、或输入为空时原样返回。
        """
        if not self.enabled or not symbols:
            return symbols

        # 维度是否生效。注意：某一数据源「完全拉取失败」时必须把对应维度真正关掉，
        # 否则会退化成「全部因数据缺失被剔除」，把结果误杀成空。
        valuation_on = self._valuation_enabled
        technical_on = self._technical_enabled
        liquidity_on = self._liquidity_enabled
        industry_on = self._industry_enabled

        # 1) 估值 / 换手率（baostock，一次登录批量拉取）
        metrics: dict[str, StockMetric] = {}
        if valuation_on or technical_on:
            metrics = self.fetch_metrics(symbols)
            if not metrics:
                logger.error("精筛：估值/技术面指标完全获取失败，跳过该维度")
                valuation_on = technical_on = False

        # 2) 成交额（本地库，无网络开销）
        turnovers: dict[str, float] = {}
        if liquidity_on:
            turnovers = self._fetch_turnover(symbols)
            if not turnovers:
                logger.error("精筛：成交额数据完全获取失败，跳过该维度")
                liquidity_on = False

        # 3) 行业（baostock 全市场一次请求）
        metas: dict = {}
        if industry_on:
            metas = stock_meta_module.load_stock_meta()
            if not metas:
                logger.error("精筛：行业数据完全获取失败，跳过该维度")
                industry_on = False

        ctx = _Context(
            valuation=valuation_on,
            liquidity=liquidity_on,
            technical=technical_on,
            industry=industry_on,
            metrics=metrics,
            turnovers=turnovers,
            metas=metas,
        )

        kept: list[str] = []
        for symbol in symbols:
            if self._passes(symbol, ctx):
                kept.append(symbol)

        detail = " | ".join(f"{k} {v}" for k, v in ctx.reasons.items()) or "无剔除"
        logger.info(f"精筛：{len(symbols)} -> {len(kept)} （{detail}）")
        return kept

    def _passes(self, symbol: str, ctx: _Context) -> bool:
        """判断单只股票是否同时满足所有已生效的维度。"""
        s = self.settings

        # ── 估值：市值 / PE / PB ──
        metric = ctx.metrics.get(symbol)
        if ctx.valuation:
            if metric is None:
                ctx.drop("估值数据缺失")
                return False

            if s.min_market_cap is not None or s.max_market_cap is not None:
                if metric.circ_market_cap is None:
                    ctx.drop("市值缺失")
                    return False
                cap_yi = metric.circ_market_cap / YI
                if s.min_market_cap is not None and cap_yi < s.min_market_cap:
                    ctx.drop("市值偏小")
                    return False
                if s.max_market_cap is not None and cap_yi > s.max_market_cap:
                    ctx.drop("市值偏大")
                    return False

            if s.min_pe is not None or s.max_pe is not None:
                if metric.pe_ttm is None:
                    ctx.drop("PE缺失")
                    return False
                if s.min_pe is not None and metric.pe_ttm < s.min_pe:
                    ctx.drop("PE偏低")
                    return False
                if s.max_pe is not None and metric.pe_ttm > s.max_pe:
                    ctx.drop("PE偏高")
                    return False

            if s.min_pb is not None or s.max_pb is not None:
                if metric.pb is None:
                    ctx.drop("PB缺失")
                    return False
                if s.min_pb is not None and metric.pb < s.min_pb:
                    ctx.drop("PB偏低")
                    return False
                if s.max_pb is not None and metric.pb > s.max_pb:
                    ctx.drop("PB偏高")
                    return False

        # ── 流动性：成交额 ──
        if ctx.liquidity:
            amount = ctx.turnovers.get(symbol)
            if amount is None:
                ctx.drop("成交额缺失")
                return False
            if amount / YI < s.min_turnover:
                ctx.drop("成交额不足")
                return False

        # ── 技术面：换手率 ──
        if ctx.technical:
            if metric is None or metric.turn is None:
                ctx.drop("换手率缺失")
                return False
            if s.min_turn is not None and metric.turn < s.min_turn:
                ctx.drop("换手率偏低")
                return False
            if s.max_turn is not None and metric.turn > s.max_turn:
                ctx.drop("换手率偏高")
                return False

        # ── 行业：白名单 / 黑名单（子串匹配）──
        if ctx.industry:
            meta = ctx.metas.get(symbol)
            industry = (meta.industry if meta else None) or ""
            include = self._include_industries
            exclude = self._exclude_industries

            if not industry:
                # 行业未知时的取舍：
                #   有白名单 -> 无法证明它命中白名单，判不通过；
                #   只有黑名单 -> 无法证明它属于被排除的行业，放行。
                # （全市场约 6% 的股票没有行业分类，一刀切剔除会误杀）
                if include:
                    ctx.drop("行业缺失")
                    return False
            else:
                if include and not any(kw in industry for kw in include):
                    ctx.drop("不在行业白名单")
                    return False

                if exclude and any(kw in industry for kw in exclude):
                    ctx.drop("命中行业黑名单")
                    return False

        return True
