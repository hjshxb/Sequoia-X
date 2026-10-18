"""布林下轨 + RSI 超跌反转策略。

来源：qtrade（moyang11111/qtrade）的 `bb_rsi`，迁移时**只取入场条件**，
丢弃它逐 K 线的出场与仓位管理 —— 本系统的策略只回答「今天选哪些票」。

入场（全部满足）：
  1. 收盘价 <= 布林下轨（bb_period 日、bb_std 倍标准差）
  2. RSI(rsi_period) < rsi_threshold（极度超跌）

⚠️ 本策略**刻意绕开精筛的「均线」维度**（见 `UniverseFilter.apply_without_ma`）：
   超跌股必然已跌破 MA120，若沿用注入池，信号会被那一条规则清空（实测只剩 9%）。
   成交额 / 今日跌幅 / 行业 / 筹码仍然生效；估值与换手率因逐股联网的成本被跳过。

本地库实测（2024-01~2026-10，剔次新）：全市场 20 日胜率 **71.6%**、均值 **+11.99%**，
但**强 regime 依赖** —— 2025 牛市 85.2%/+12.04%，2026 震荡市 44.5%/+0.43%。
因此它只担任「候选生成器」，最终排序与取舍交给评分层。
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
    # 最长指标窗口是布林 20 日；取 150 个自然日（≈100 个交易日）足够且不至于全表扫
    _HISTORY_DAYS: int = 150

    def _fetch_recent(self, start: str) -> pd.DataFrame:
        """读取 `start` 之后的收盘价序列（一次批量读，不逐股查库）。"""
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, close FROM stock_daily "
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
        """返回今日「跌破布林下轨且 RSI 极低」的股票代码列表。"""
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

        df = df.assign(lower=lower, rsi=rsi)
        today = df[df["date"] == latest_date]
        hit = today[(today["close"] <= today["lower"]) & (today["rsi"] < self.rsi_threshold)]

        # 越超跌（RSI 越低）越靠前；同值按代码，保证输出稳定
        hit = hit.sort_values(["rsi", "symbol"])
        selected = self.universe_filter.apply_without_ma(hit["symbol"].tolist())

        logger.info(
            f"BollRsiReversalStrategy 选出 {len(selected)} 只股票"
            f"（原始命中 {len(hit)} 只，数据日 {latest_date}）"
        )
        return selected
