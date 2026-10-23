"""距 MA20 偏离度硬剔除：策略选完之后、评分之前的一道风控闸门。

**为什么放在这里，而不是并进精筛**：
    精筛面对全市场 5000+ 只、只拿静态字段（市值/成交额/换手/估值），跑不起价格序列；
    而偏离度需要每只票近 21 根 K 线。策略选完只有几十只，现算成本可忽略，
    且「哪几只被剔」能记进日志与报告，可追溯。

**为什么分两档**：同样的偏离对高低波动股含义完全不同。实测（2026-10，109 万条）：
    低波股（年化波动 <33%）偏离到 15% 时 20 日中位收益仍有 +0.89%，20~25% 才转负；
    非低波股 9~12% 就转负，>30% 时中位收益 −6.69%、回撤≥10% 的概率 81%。
默认阈值 低波 20% / 非低波 30%（用户 2026-10-23 指定）。
⚠️ 30% 对非低波是**很宽**的口子（那一档历史中位收益已经是 −3.6%），
   设成更严的值（如 8~12%）会显著提高留存质量但砍掉大半名单 —— 见 README 的曲线表。

**降级原则**：取不到价格序列（新股、停牌、库缺行）时**保留**该股并记 WARNING，
   不因为算不出来就把它悄悄剔除 —— 那会把「数据问题」变成「选股结论」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 算 20 日均线 + 20 日波动率至少要 21 根；多取几根留缓冲（停牌缺口）
_BARS_NEEDED = 26
_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class DroppedStock:
    """被剔除的一只票，保留计算依据便于复盘。"""

    symbol: str
    dev: float  # 距 MA20 偏离（%）
    vol: float  # 20 日年化波动率（%）
    limit: float  # 该股适用的阈值（%）
    is_low_vol: bool  # 走的是低波那一档吗


@dataclass(frozen=True)
class FilterResult:
    """一次过滤的结果。"""

    results: dict[str, list[str]]  # 过滤后的 {策略: 代码列表}
    dropped: tuple[DroppedStock, ...]  # 被剔除的票
    missing: tuple[str, ...]  # 取不到价格序列、已按降级保留的票

    @property
    def dropped_symbols(self) -> set[str]:
        return {d.symbol for d in self.dropped}

    def summary(self) -> str:
        """一行中文摘要，供日志与报告页首使用；无剔除时返回空串。"""
        if not self.dropped:
            return ""
        low = sum(1 for d in self.dropped if d.is_low_vol)
        worst = max(self.dropped, key=lambda d: d.dev)
        return (
            f"距MA20剔除 {len(self.dropped)} 只（低波 {low} / 非低波 {len(self.dropped) - low}，"
            f"最大偏离 {worst.symbol} {worst.dev:+.1f}%）"
        )


class Ma20DeviationFilter:
    """按「低波 / 非低波」两档偏离阈值剔除候选股。"""

    def __init__(self, settings: Settings, db_path: str | None = None) -> None:
        self.settings = settings
        self.db_path = db_path or settings.db_path

    # ── 取数 ──
    def _load(self, symbols: list[str]) -> pd.DataFrame:
        """一次读出这批票最近的 K 线（按股票分组、只留最后若干根）。"""
        if not symbols:
            return pd.DataFrame()
        placeholders = ",".join("?" * len(symbols))
        try:
            with sqlite3.connect(self.db_path) as conn:
                return pd.read_sql(
                    f"SELECT symbol, date, close FROM stock_daily "
                    f"WHERE symbol IN ({placeholders}) AND close > 0 "
                    f"ORDER BY symbol, date",
                    conn,
                    params=symbols,
                )
        except Exception as exc:  # noqa: BLE001 - 取数失败只降级
            logger.error(f"距MA20过滤：读取行情失败，本次不剔除任何票：{exc}")
            return pd.DataFrame()

    @staticmethod
    def _features(bars: pd.DataFrame) -> dict[str, tuple[float, float]]:
        """算 {symbol: (偏离%, 年化波动%)}；不足 21 根的票不返回。

        空表（取数失败 / 没有候选）要能直接吃下去返回空 —— 否则 `groupby("symbol")`
        会在一个没有列的 DataFrame 上抛 KeyError，把「降级」变成「崩掉」。
        """
        out: dict[str, tuple[float, float]] = {}
        if bars is None or getattr(bars, "empty", True) or "symbol" not in bars.columns:
            return out
        for symbol, group in bars.groupby("symbol", sort=False):
            closes = group["close"].astype(float).tolist()[-_BARS_NEEDED:]
            if len(closes) < 21:
                continue
            ma20 = sum(closes[-20:]) / 20
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
            rets = rets[-20:]
            if len(rets) < 20 or ma20 <= 0:
                continue
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol = (var**0.5) * (_TRADING_DAYS_PER_YEAR**0.5) * 100
            out[str(symbol)] = (closes[-1] / ma20 - 1, vol)
        return out

    # ── 主入口 ──
    def apply(self, results: Mapping[str, list[str]]) -> FilterResult:
        """剔除偏离过大的票，返回过滤后的结果（保持原有策略顺序与组内顺序）。"""
        if not self.settings.ma20_filter_enabled:
            return FilterResult(
                results={k: list(v) for k, v in results.items()}, dropped=(), missing=()
            )

        symbols = sorted({s for group in results.values() for s in group})
        bars = self._load(symbols)
        feats = self._features(bars)

        dropped: list[DroppedStock] = []
        missing: list[str] = []
        for symbol in symbols:
            if symbol not in feats:
                missing.append(symbol)
                continue
            dev, vol = feats[symbol]
            dev_pct = dev * 100
            is_low = vol < self.settings.ma20_low_vol_cut
            limit = (
                self.settings.ma20_dev_limit_low_vol
                if is_low
                else self.settings.ma20_dev_limit_others
            )
            if dev_pct > limit:
                dropped.append(DroppedStock(symbol, dev_pct, vol, limit, is_low))

        keep = {d.symbol for d in dropped}
        filtered = {k: [s for s in v if s not in keep] for k, v in results.items()}
        if missing:
            logger.warning(
                f"距MA20过滤：{len(missing)} 只取不到足够的 K 线，已按降级保留"
                f"（{', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}）"
            )
        return FilterResult(results=filtered, dropped=tuple(dropped), missing=tuple(missing))
