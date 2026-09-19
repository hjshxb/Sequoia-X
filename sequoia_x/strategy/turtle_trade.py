"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），且必须真涨（今日 close > 昨日 close）

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """查询候选股票的流通市值（单位：元），用于按市值排序。

        复用 FundamentalFilter 的批量拉取与进程内缓存：
        - 使用回看窗口取最近一个交易日的真实（不复权）价格，
          避免在周末/节假日因当天无行情而拿到空数据、导致排序静默失效；
        - 与基本面过滤共享缓存，避免重复请求同一只股票。

        Returns:
            {symbol: 流通市值(元)}；取不到市值的股票不在字典中。
        """
        metrics = self.universe_filter.fetch_metrics(symbols)
        return {
            symbol: metric.circ_market_cap
            for symbol, metric in metrics.items()
            if metric.circ_market_cap is not None
        }

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        candidates: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化：前20日 high 的滚动最大值（不含当日，shift(1) 后取 rolling(20)）
                df["high_20"] = df["high"].shift(1).rolling(20).max()

                last = df.iloc[-1]
                prev = df.iloc[-2]  # 获取昨日数据，用于对比

                if pd.isna(last["high_20"]):
                    continue

                # 核心条件 1：突破前 20 天最高点
                breakout = last["close"] > last["high_20"]
                # 核心条件 2：流动性过亿
                liquid = last["turnover"] > 100_000_000

                # 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
                is_yang = last["close"] > last["open"]  # 实体必须是阳线（红柱）
                is_up = last["close"] > prev["close"]  # 必须是真涨，不能是假阳线

                if breakout and liquid and is_yang and is_up:
                    candidates.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 先应用精筛（估值 / 流动性 / 技术面 / 行业），再按流通市值从大到小排序
        candidates = self.apply_universe_filter(candidates)

        if candidates:
            market_caps = self._get_market_caps(candidates)
            candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates
