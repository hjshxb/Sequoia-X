"""布林下轨 + RSI 超跌反转策略。

来源：qtrade（moyang11111/qtrade）的 `bb_rsi`，迁移时**只取入场条件**，
丢弃它逐 K 线的出场与仓位管理 —— 本系统的策略只回答「今天选哪些票」。

入场（全部满足）：
  1. 收盘价 <= 布林下轨（bb_period 日、bb_std 倍标准差）
  2. RSI(rsi_period) < rsi_threshold（极度超跌）
  3. 缩量但不缩死：vol_ratio_min <= 5日均量/20日均量 <= vol_ratio_max

第 3 条为什么是**区间**而不是「越小越好」—— 本地库按量比分桶（2024-01~2026-10，剔次新）：
    极度缩量 <0.4      118 样本  胜率 54.2%  均值 −1.19%
    缩量 0.4-0.6     1,111 样本  胜率 79.2%  均值 +11.29%
    温和缩量 0.6-0.8  3,512 样本  胜率 73.3%  均值 +9.14%
    平量 0.8-1.2     7,216 样本  胜率 64.3%  均值 +6.00%
    放量 1.2-1.8     1,839 样本  胜率 62.1%  均值 +5.19%
    大幅放量 >1.8      236 样本  胜率 54.7%  均值 +3.40%
  ⇒ 缩量确实优于放量，但**极度缩量是反向的**（无人接盘 / 流动性枯竭），故下界必须有。

⚠️ 本策略**刻意绕开精筛的「均线」维度**（见 `UniverseFilter.apply_without_ma`）：
   超跌股必然已跌破 MA120，若沿用注入池，信号会被那一条规则清空（实测只剩 9%）。
   成交额 / 今日跌幅 / 行业 / 筹码仍然生效；估值与换手率因逐股联网的成本被跳过。

本地库实测（2024-01~2026-10，剔次新）：
  不加缩量：14,032 样本、20 日胜率 67.2%、均值 +7.00%
            （分年 2024 +4.83% / 2025 +12.04% / 2026 +0.43%）
  加缩量区间：4,623 样本、20 日胜率 **74.7%**、均值 **+9.66%**
            （分年 2024 +2.16% / 2025 +13.47% / 2026 +2.81%）
  逐股口径全面变好，且**最差年份从 +0.43% 抬到 +2.16~2.81%**（震荡市才是短板）。

  ⚠️ 但要如实记一笔：换成「按交易日等权」口径后，加缩量反而略降
  （日等权均值 +1.67% → +1.39%，负收益日占比 45.7% → 48.8%）。两种口径方向不一致，
  说明「缩量带来的超额」并不稳健 —— 它剔除掉的那些放量下跌日，本身就是反弹最猛的日子。
  故本策略仍然只担任「候选生成器」，最终排序与取舍交给评分层，不要单独依赖它下单。
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BollRsiReversalStrategy(BaseStrategy):
    """布林下轨 + RSI 超跌反转（逆势低吸）。

    Attributes:
        webhook_key: 路由到 'reversal' 专属飞书机器人。
    """

    webhook_key: str = "reversal"
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_threshold: float = 20.0
    # 缩量区间（5 日均量 / 20 日均量）。下界防「缩死」（无人接盘），上界防「放量下跌」。
    vol_short: int = 5
    vol_long: int = 20
    vol_ratio_min: float = 0.4
    vol_ratio_max: float = 0.8
    # 最长指标窗口是布林 20 日；取 150 个自然日（≈100 个交易日）足够且不至于全表扫
    _HISTORY_DAYS: int = 150

    def _fetch_recent(self, start: str) -> pd.DataFrame:
        """读取 `start` 之后的收盘价与成交量序列（一次批量读，不逐股查库）。"""
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, close, volume FROM stock_daily "
                    "WHERE date >= ? ORDER BY symbol, date",
                    conn,
                    params=(start,),
                )
        except Exception as exc:
            logger.error(f"BollRsiReversalStrategy 读取数据库失败：{exc}")
            return pd.DataFrame()

    def _rolling(self, df: pd.DataFrame, series: pd.Series, window: int, how: str) -> pd.Series:
        """按股票分组滚动，再把 groupby 多出来的一层索引吃掉、对齐回原索引。"""
        rolled = getattr(series.groupby(df["symbol"], sort=False).rolling(window), how)()
        return rolled.reset_index(level=0, drop=True).sort_index()

    def run(self) -> list[str]:
        """返回今日「跌破布林下轨 + RSI 极低 + 缩量」的股票代码列表。"""
        # 「今日」取全市场 MAX(date)，与精筛、其余策略同一口径；
        # 停牌股不会出现在这一天，自然被排除，避免拿停牌前的旧价算出假信号。
        latest_date = self.engine.get_market_latest_date()
        if not latest_date:
            logger.warning("BollRsiReversalStrategy 库内无行情数据")
            return []

        start = (date.fromisoformat(latest_date) - timedelta(days=self._HISTORY_DAYS)).isoformat()
        df = self._fetch_recent(start)
        if df.empty:
            logger.warning("BollRsiReversalStrategy 无可用行情数据")
            return []

        close = df["close"]
        ma = self._rolling(df, close, self.bb_period, "mean")
        std = self._rolling(df, close, self.bb_period, "std")
        lower = ma - self.bb_std * std

        delta = close.groupby(df["symbol"], sort=False).diff()
        avg_gain = self._rolling(df, delta.clip(lower=0), self.rsi_period, "mean")
        avg_loss = self._rolling(df, (-delta.clip(upper=0)), self.rsi_period, "mean")
        # avg_loss == 0 表示区间内只涨不跌，RSI 应为 100（不可能 < 阈值）；
        # 用 NaN 取代 0 既避开除零，也让它自然落选。
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + rs)

        # 缩量比：短均量 / 长均量。成交量为 0 或缺失时落成 NaN，自然被区间过滤掉。
        vol_ratio = self._rolling(df, df["volume"], self.vol_short, "mean") / self._rolling(
            df, df["volume"], self.vol_long, "mean"
        ).replace(0, float("nan"))

        df = df.assign(lower=lower, rsi=rsi, vol_ratio=vol_ratio)
        today = df[df["date"] == latest_date]
        oversold = today[(today["close"] <= today["lower"]) & (today["rsi"] < self.rsi_threshold)]
        hit = oversold[oversold["vol_ratio"].between(self.vol_ratio_min, self.vol_ratio_max)]

        # 越超跌（RSI 越低）越靠前；同值按代码，保证输出稳定
        hit = hit.sort_values(["rsi", "symbol"])
        selected = self.universe_filter.apply_without_ma(hit["symbol"].tolist())

        logger.info(
            f"BollRsiReversalStrategy 选出 {len(selected)} 只股票"
            f"（跌破下轨+RSI {len(oversold)} 只 → 缩量区间 {len(hit)} 只"
            f" → 精筛后 {len(selected)} 只，数据日 {latest_date}）"
        )
        return selected
