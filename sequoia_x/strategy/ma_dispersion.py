"""均线收敛发散策略：纠结之后方向刚出来（逆势低吸·形态档）。

数据日 T 的全部条件：
  1. 收敛过：近 20 日内，均线离散度的 z 值曾 <= -0.5
  2. 已发散：今日 z >= 0.5
  3. 方向朝上：MA5 的 5 日斜率 > 0（**这是关键，见下**）
  4. 缩量：5 日均量 / 20 日均量 <= 0.85
  5. 仍在均线下方：收盘距 MA20 <= -3%

离散度 = std(MA5, MA10, MA20, MA60) / mean(同四条)，越大表示均线越张开；
z = (离散度 − 其 250 日均值) / 250 日标准差，用来剔除不同股票波动基准的差异。

**方向为什么要用 MA5 斜率**（用户 2026-10-21 提出）：一开始我用「多头排列
（MA5>MA10>MA20>MA60）」判方向，结果它与「近 20 日涨幅前 5%」重叠 **82.6%** ——
四条线全排好时票已经涨完，所以是 −0.94%（追高）。MA5 斜率要早得多：
均线刚张开时 5 日线已经拐向哪边，就是往哪边发散。

实测（本地库，20 日同日超额）：
    形态全量（不分方向）          484.4 只/日  +0.32%
    + MA5 上翘                    259.9 只/日  +0.21%
    + MA5 下拐                    224.2 只/日  +0.46%
    + 上翘 + 缩量                   38.3 只/日  +0.79%  胜率 56.5%
    + 上翘 + 缩量 + 距MA20<=-3%      16.0 只/日  **+1.28%**  胜率 **75.6%**  ← 本类
    上翘 + 放量                    143.8 只/日  −0.40%

  ① 🔴 **向下发散比向上发散更赚**（下拐 >3% 那档 +0.67%，是所有斜率分档里最高的）。
     这是 A 股反复出现的「买跌不买涨」：收敛后向下跌破 = 超跌反弹的起点。
     但「下拐」那一族的收益集中度更高（负日 48.9%），且与 ATR 超跌档重叠，
     所以本类选择**上翘 + 缩量**这条更稳的路（胜率 75.6%）。
  ② 🔴 **放量上攻是负的**（−0.40%）。上翘必须配合**缩量** —— 静悄悄启动才是低吸，
     一旦放量就是别人已经在买了。这与 BottomDoji 的「放量确认」看似矛盾，
     其实是同一件事的两面：那个是「形态日的缩量 + 确认日的放量」，
     这个是「均线刚翘头、还没放量」的更早期阶段。
  ③ ⚠️ 分年只跑出 2025（+1.28%）与 2026（+0.30%）两段 —— z 需要 250 日历史，
     本地库从 2024-01 起，2024 年整年样本不足。**2026 明显走弱**，需持续观察。
  ④ ⚠️ 负日占比 49.2%：超额集中在少数反弹日，属于「候选生成器」而非稳定选股器。

与现有策略的重叠：`BottomDoji` Jaccard 0.0023、`AtrShrinkReversal` 0.0001
  ⇒ 独立信号族（前者要十字星+放量确认，后者要跌破 4 个 ATR）。

⚠️ 必须走 `apply_without_ma()`：候选里 **96.8% 已在 MA120 下方**
   （本类本身就要求「还在 MA20 下方 3%」），沿用注入池会被整条清空。
⚠️ 成交额门槛按策略放宽到 2 亿（与 BottomDoji 同因）：缩量票天然偏小盘，
   候选成交额中位数仅 1.01 亿、71% 不到 2 亿，3 亿那一刀会砍掉大部分信号。
"""

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaDispersionStrategy(BaseStrategy):
    """均线收敛 → 发散（MA5 上翘 + 缩量 + 仍在均线下方）。"""

    webhook_key: str = "reversal"

    # 离散度用的四条均线。改这里要连带改 `_HISTORY_DAYS` 的下限判断
    ma_windows: tuple[int, ...] = (5, 10, 20, 60)
    # z 的基准窗口：一只票自身的波动基准差异很大，必须用自身历史标准化
    disp_window: int = 250
    # 「收敛过」与「已发散」的阈值（单位：自身标准差）
    z_low: float = -0.5
    z_high: float = 0.5
    converge_lookback: int = 20
    # 方向：MA5 的 N 日斜率必须 > `slope_min`（0 = 上翘；负值等于放宽方向约束）
    slope_window: int = 5
    slope_min: float = 0.0
    # 缩量：5 日均量 / 20 日均量
    vol_short: int = 5
    vol_long: int = 20
    vol_ratio_max: float = 0.85
    # 仍在均线下方（负数）：离 MA20 至少跌 3%
    dist_ma20_max: float = -0.03
    # 成交额门槛（亿元）：见模块 docstring 的说明
    min_turnover_yi: float = 2.0
    # 250 日 z 基准 + 60 日均线 + 缓冲 ⇒ 约 320 个交易日，取 520 个自然日
    _HISTORY_DAYS: int = 520

    def _fetch_recent(self, start: str) -> pd.DataFrame:
        """读取 `start` 之后的 OHLC 与成交量（一次批量读，不逐股查库）。"""
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, low, close, volume FROM stock_daily "
                    "WHERE date >= ? ORDER BY symbol, date",
                    conn,
                    params=(start,),
                )
        except Exception as exc:
            logger.error(f"MaDispersionStrategy 读取数据库失败：{exc}")
            return pd.DataFrame()

    def _rolling(self, df: pd.DataFrame, series: pd.Series, window: int, how: str) -> pd.Series:
        """按股票分组滚动，再把 groupby 多出来的一层索引吃掉、对齐回原索引。"""
        rolled = getattr(series.groupby(df["symbol"], sort=False).rolling(window), how)()
        return rolled.reset_index(level=0, drop=True).sort_index()

    def _shift(self, df: pd.DataFrame, series: pd.Series, periods: int = 1) -> pd.Series:
        """按股票分组位移，避免跨股票串行（每只票的第一根位移后为 NaN）。"""
        return series.groupby(df["symbol"], sort=False).shift(periods)

    def _turnover_override(self) -> float | None:
        """返回本次调用要用的成交额下限覆盖值；不需要放宽时返回 None。

        只在「全局配了成交额、且本策略的门槛确实更松」时返回 ——
        `apply_without_ma(min_turnover=...)` 是**覆盖**语义，无条件传值会在
        全局未启用该维度时把它强行打开。
        """
        global_floor = self.settings.min_turnover
        if global_floor is None or self.min_turnover_yi >= global_floor:
            return None
        return self.min_turnover_yi

    def run(self) -> list[str]:
        """返回今日「均线刚向上发散 + 缩量 + 仍在均线下方」的股票代码列表。"""
        latest_date = self.engine.get_market_latest_date()
        if not latest_date:
            logger.warning("MaDispersionStrategy 库内无行情数据")
            return []

        start = (date.fromisoformat(latest_date) - timedelta(days=self._HISTORY_DAYS)).isoformat()
        df = self._fetch_recent(start)
        if df.empty:
            logger.warning("MaDispersionStrategy 无可用行情数据")
            return []

        # ── ① 均线离散度与它自身的 z ──
        ma_cols = [self._rolling(df, df["close"], w, "mean") for w in self.ma_windows]
        stack = pd.concat(ma_cols, axis=1)
        mean_ma = stack.mean(axis=1).replace(0, np.nan)
        dispersion = stack.std(axis=1, ddof=0) / mean_ma
        disp_mean = self._rolling(df, dispersion, self.disp_window, "mean")
        disp_std = self._rolling(df, dispersion, self.disp_window, "std").replace(0, np.nan)
        z = (dispersion - disp_mean) / disp_std

        # ── ② 形态：近 N 日曾收敛，今天已发散 ──
        z_min = self._rolling(df, z, self.converge_lookback, "min")

        # ── ③ 方向：MA5 的斜率 ──
        ma5 = ma_cols[self.ma_windows.index(5)]
        slope = ma5 / self._shift(df, ma5, self.slope_window).replace(0, np.nan) - 1

        # ── ④ 缩量与位置 ──
        vol_short = self._rolling(df, df["volume"], self.vol_short, "mean")
        vol_long = self._rolling(df, df["volume"], self.vol_long, "mean").replace(0, np.nan)
        vol_ratio = vol_short / vol_long
        ma20 = ma_cols[self.ma_windows.index(20)] if 20 in self.ma_windows else None
        dist_ma20 = (
            df["close"] / ma20.replace(0, np.nan) - 1 if ma20 is not None else pd.Series(np.nan)
        )

        today = df["date"] == latest_date
        converged = z_min.le(self.z_low).fillna(False)
        diverged = z.ge(self.z_high).fillna(False)
        up = slope.gt(self.slope_min).fillna(False)
        shrink = vol_ratio.le(self.vol_ratio_max).fillna(False)
        deep = dist_ma20.le(self.dist_ma20_max).fillna(False)

        n_conv = int((today & converged).sum())
        n_div = int((today & converged & diverged).sum())
        n_up = int((today & converged & diverged & up).sum())
        n_shrink = int((today & converged & diverged & up & shrink).sum())
        hit_mask = today & converged & diverged & up & shrink & deep

        hit = df[hit_mask].assign(_dist=dist_ma20.loc[df[hit_mask].index])
        # 离均线越远越靠前（低吸程度），同值按代码保证输出稳定
        hit = hit.sort_values(["_dist", "symbol"])
        codes = hit["symbol"].tolist()
        override = self._turnover_override()
        selected = self.universe_filter.apply_without_ma(codes, min_turnover=override)

        chain = (
            f"20日内收敛过 {n_conv} 只 → 今日已发散 {n_div} 只 → MA5 上翘 {n_up} 只"
            f" → 缩量 {n_shrink} 只 → 距MA20<={self.dist_ma20_max:.0%} {len(hit)} 只"
        )
        turnover_note = (
            f"，成交额门槛放宽到 {override:g} 亿（全局 {self.settings.min_turnover:g} 亿）"
            if override is not None
            else ""
        )
        logger.info(
            f"MaDispersionStrategy 选出 {len(selected)} 只股票"
            f"（{chain} → 精筛后 {len(selected)} 只，数据日 {latest_date}{turnover_note}）"
        )
        return selected
