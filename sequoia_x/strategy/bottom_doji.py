"""底部十字星策略：60 日低位 + 十字星 + 缩量（逆势低吸·形态档）。

选股条件（全部满足，全部是**当日**可观测的量，不需要等次日）：
  1. 十字星：|收盘−开盘| / (最高−最低) <= 0.2，且上下影线都存在
  2. 有振幅：振幅 (最高−最低)/收盘 >= 2%（排除一字板与近乎无成交的僵尸票）
  3. 处于底部：(收盘 − 60日最低) / (60日最高 − 60日最低) <= 0.15
  4. 缩量：5 日均量 / 20 日均量 <= 0.85
  5. 跌破 MA20 至少 5%（与条件 3 高度相关，用于排除「贴着均线的浅回调」）

⚠️ **刻意没有「次日放量上涨」这一条**。用户最初的描述包含「第二天放量上涨」，
   实测这一条是**负贡献**，必须去掉 —— 详见下面第 ③ 条。

实测结论（本地库 2024-01~2026-10，剔次新，20 日前向，`tmp/probe_doji_*.py`）：
  ① 「低位」是主要贡献，十字星形状只是加成：
       只看「60日位置<=0.2 + 缩量」         同日超额 +0.39%
       再加「十字星形状」                    同日超额 +0.89%   ⇒ 形状贡献约 +0.5%
  ② 与 ATR 超跌**几乎不重叠**（Jaccard 0.007），是独立信号族，不是冗余。
     两者同时命中时同日超额 **+4.03%**（297 样本、78.8% 胜率），但仅 4.8 只/日，太稀疏。
  ③ 🔴 **「次日放量上涨」是减分项**（同样以 T+1 收盘为买入价，严格可比）：
       中等口径全量，T+1 买入              +1.26%
       + 次日涨>=2% 且量>=1.2×             +1.20%
       + 次日涨>=3% 且量>=1.2×             +0.99%
       + 次日涨>=5% 且量>=1.2×             +0.91%（日中位 −2.43%、负日 54.6%）
       + **次日跌>=3%**                    **+3.06%** ← 方向完全相反
     即：等「放量确认」等于等它涨完再追，A 股里这是买在高点；真正的低吸是次日更弱时买。
     另外「次日涨>=5%」的信号里 12.7% 涨停、28.2% 涨超 8% ⇒ 这部分根本买不到。
  ④ 🔴 **幸存者偏差警告**：用户举的 002472(+8.55%)、603119(+10.00%) 在 2026-10-16
     确实命中本策略，但它们只是当日 81~123 只候选里的第 2~4 名。
     那 123 只的次日涨幅中位数只有 +0.50%，涨超 5% 的仅 12 只（9.8%）。
  ⑤ ⚠️ **精筛的成交额门槛会显著削弱它**：中等口径精筛（成交额>=3亿）后
     同日超额从 +1.20% 掉到 **+0.17%**，只剩 8.6 只/日。
     收紧到「实体<=0.1 + 量比<=0.7」则能保住 +0.89%（3.3 只/日），但会漏掉上面两只例子。
     本类默认用「中等口径」以贴合用户的形态直觉，想更严可直接改类常量。

⚠️ 必须走 `apply_without_ma()`：实测 92.9% 的信号落在 MA120 下方，
   沿用注入池会被「收盘价 >= MA120」整条清空。
"""

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BottomDojiStrategy(BaseStrategy):
    """底部十字星 + 缩量（逆势低吸·形态档）。

    Attributes:
        webhook_key: 路由到 'reversal' 专属飞书机器人（未配置时回退主 webhook）。
    """

    webhook_key: str = "reversal"

    # 十字星：实体占全幅比例上限。0.2 对应用户举的两只（0.04 / 0.15）；
    # 收紧到 0.1 次日超额能从 +1.20% 抬到 +1.50%，但会漏掉 603119（0.15）
    body_ratio_max: float = 0.2
    amp_min: float = 0.02
    # 60 日区间内的相对位置上限。越低越「底部」，但信号也越少
    pos60_max: float = 0.15
    # 缩量：5 日均量 / 20 日均量。0.85 能命中两只例子（0.80 / 0.74）
    vol_short: int = 5
    vol_long: int = 20
    vol_ratio_max: float = 0.85
    # 距 MA20 的上限（负数）。与 pos60 高度相关，用于排除「贴着均线的浅回调」
    dist_ma20_max: float = -0.05
    # 最长窗口是 60 日；取 150 个自然日（≈100 个交易日）足够且不至于全表扫
    _HISTORY_DAYS: int = 150

    def _fetch_recent(self, start: str) -> pd.DataFrame:
        """读取 `start` 之后的 OHLC 与成交量（一次批量读，不逐股查库）。"""
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, open, high, low, close, volume FROM stock_daily "
                    "WHERE date >= ? ORDER BY symbol, date",
                    conn,
                    params=(start,),
                )
        except Exception as exc:
            logger.error(f"BottomDojiStrategy 读取数据库失败：{exc}")
            return pd.DataFrame()

    def _rolling(self, df: pd.DataFrame, series: pd.Series, window: int, how: str) -> pd.Series:
        """按股票分组滚动，再把 groupby 多出来的一层索引吃掉、对齐回原索引。"""
        rolled = getattr(series.groupby(df["symbol"], sort=False).rolling(window), how)()
        return rolled.reset_index(level=0, drop=True).sort_index()

    def run(self) -> list[str]:
        """返回今日「60 日低位 + 十字星 + 缩量」的股票代码列表。"""
        # 「今日」取全市场 MAX(date)，与精筛、其余策略同一口径；
        # 停牌股不会出现在这一天，自然被排除，避免拿停牌前的旧价算出假信号。
        latest_date = self.engine.get_market_latest_date()
        if not latest_date:
            logger.warning("BottomDojiStrategy 库内无行情数据")
            return []

        start = (date.fromisoformat(latest_date) - timedelta(days=self._HISTORY_DAYS)).isoformat()
        df = self._fetch_recent(start)
        if df.empty:
            logger.warning("BottomDojiStrategy 无可用行情数据")
            return []

        rng = df["high"] - df["low"]
        # 一字板（high == low）会让实体比发散，落成 NaN 自然被剔除
        body_ratio = (df["close"] - df["open"]).abs() / rng.replace(0, np.nan)
        amp = rng / df["close"].replace(0, np.nan)
        both_wick = (df["high"] > df[["open", "close"]].max(axis=1)) & (
            df[["open", "close"]].min(axis=1) > df["low"]
        )

        lo60 = self._rolling(df, df["low"], 60, "min")
        hi60 = self._rolling(df, df["high"], 60, "max")
        pos60 = (df["close"] - lo60) / (hi60 - lo60).replace(0, np.nan)

        vol_short = self._rolling(df, df["volume"], self.vol_short, "mean")
        vol_long = self._rolling(df, df["volume"], self.vol_long, "mean").replace(0, np.nan)
        vol_ratio = vol_short / vol_long
        dist_ma20 = df["close"] / self._rolling(df, df["close"], 20, "mean").replace(0, np.nan) - 1

        df = df.assign(body_ratio=body_ratio, amp=amp, pos60=pos60, vol_ratio=vol_ratio)
        today = df[df["date"] == latest_date]

        doji = today["body_ratio"].le(self.body_ratio_max) & today["amp"].ge(self.amp_min)
        doji = doji & both_wick.loc[today.index]
        bottom = today["pos60"].le(self.pos60_max) & dist_ma20.loc[today.index].le(
            self.dist_ma20_max
        )
        shrink = today["vol_ratio"].le(self.vol_ratio_max)

        hit = today[doji & bottom & shrink]
        # 位置越低越靠前；同值按代码，保证输出稳定
        hit = hit.sort_values(["pos60", "symbol"])
        selected = self.universe_filter.apply_without_ma(hit["symbol"].tolist())

        logger.info(
            f"BottomDojiStrategy 选出 {len(selected)} 只股票"
            f"（十字星 {int(doji.sum())} 只 → 底部 {int((doji & bottom).sum())} 只"
            f" → 缩量 {len(hit)} 只 → 精筛后 {len(selected)} 只，数据日 {latest_date}）"
        )
        return selected
