"""ATR 超跌反转策略：跌破 MA20 超过 N 个 ATR，且成交量收缩。

**替代** 原先的「布林下轨 + RSI」版本（`boll_rsi_reversal.py`，本文件上线时删除）：
两者判断的是同一类「深跌低吸」，但 ATR 口径更本质 —— 它把「跌了多少」按**个股自身波动率**
标准化了。一只高波动股跌 20% 可能只算 2 个 ATR（正常波动），低波动股跌 8% 就可能是 4 个 ATR
（异常暴跌）。布林带用收盘价 20 日标准差做尺度，对跳空与日内振幅不敏感；ATR 用真实波幅
（含高低价与跳空）计算，对 A 股涨跌停这类跳空更贴切。

入场（全部满足）：
  1. (收盘价 − MA20) / ATR14 < -4     —— 跌破 20 日线超过 4 个平均波幅
  2. 缩量区间：0.4 <= 5日均量/20日均量 <= 0.8

几条实测结论（本地库 2024-01~2026-10，剔次新，20 日前向；`tmp/probe_*atr*.py`）：

  ① 阈值不能只设单边。跌破的 ATR 倍数越多并非越好：
       <-3.0 超额 +3.07%   <-3.5 +4.21%   <-4.0 **+5.17%**
       <-4.5 +5.36%（但 2026 转 −1.09%）  <-5.0 +4.63%（2026 −5.86%）  <-6.0 **−4.65%**
     跌太深是「已经崩了」，不是机会。取 -4 是收益与跨年稳定性的平衡点。

  ② RSI 在 ATR 口径下**冗余，已移除**：ATR<-4 本身已隐含极低 RSI。
     再加 RSI<20 只把样本从 10,609 削到 8,708，胜率 71.6%→71.2%，
     2026 超额 +1.98%→+1.10%（反而变差）。

  ③ 缩量在 ATR 口径下**依然有效且更强**（与旧布林口径一致）：
       ATR<-4           10,609；胜率 71.6%、均值 +7.74%（超额 +5.17%）、2026 +1.98%
       ATR<-4 + 缩量     3,179；胜率 **79.3%**、均值 **+11.25%**、分年 +2.44/+12.72/**+4.76%**

  ④ 与旧布林+RSI 信号 Jaccard 仅 **0.25**（ATR 信号 54% 是布林版选不到的），不是重复。

⚠️ 本策略**必须豁免精筛的「均线」维度**（`UniverseFilter.apply_without_ma`）：
   实测 dist_atr<-4 的信号里 **97.6% 已在 MA120 下方**，沿用注入池会被那条规则整条清空。

🔴 有效性边界（务必连同下面这条一起看）：
   逐股口径 79.3% 胜率很漂亮，但换成**按交易日等权**后，日中位数 −0.17%、负收益日占 50.8%；
   剔除 2025-04-07/08 这两个全市场极端反弹日后，逐股胜率跌到 66.6%、均值 +5.79%。
   用更严的口径「同日超额」（每笔 20 日收益先减掉当天全市场均值，再平均，即剔掉市场 beta）
   是 **+2.73%**，分年 −0.32% / +3.86% / +1.42% ⇒ **相对强度是真的**，只是绝对收益被 beta 主导。
   （同口径下旧「布林+RSI+缩量」只有 +1.87% ⇒ 本次换 ATR 口径带来 +0.86% 的真实提升。）
   但收益分布右偏：日中位数仍为负，超额几乎都来自少数反弹日 —— 见下面的广度分组：
       广度 < P50（0.14%）        胜率 45.5%  均值 −0.76%   ← 平静市里，跌破 4ATR 只是「这票出事了」
       广度 P50~P90             胜率 55.0%  均值 +1.35%
       广度 ≥ P90（0.95%）        胜率 86.2%  均值 +14.00%  ← 但只有 34 个出手日，且日中位 −1.30%
   即：**它的超额集中在全市场恐慌后的反弹日**，本质更接近「恐慌探测器」而非稳定选股能力。
   34 个出手日这个量级，靠「每天推 10 只」的系统是抓不住的。
   因此它只作候选生成器，绝不能单独据此下单；真要发挥价值，应该做的是广度门控（尚未实现）。
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class AtrShrinkReversalStrategy(BaseStrategy):
    """ATR 超跌 + 缩量反转（逆势低吸·深跌档）。

    Attributes:
        webhook_key: 路由到 'reversal' 专属飞书机器人（未配置时回退主 webhook）。
    """

    webhook_key: str = "reversal"
    ma_period: int = 20
    atr_period: int = 14
    # 跌破 MA20 的 ATR 倍数阈值（负数）。阈值取 −4 见模块文档的实验数据
    atr_threshold: float = -4.0
    vol_short: int = 5
    vol_long: int = 20
    vol_ratio_min: float = 0.4
    vol_ratio_max: float = 0.8
    # 最长窗口是 MA20 + ATR14；取 150 个自然日（≈100 个交易日）足够且不至于全表扫
    _HISTORY_DAYS: int = 150

    def _fetch_recent(self, start: str) -> pd.DataFrame:
        """读取 `start` 之后的高低价与成交量（一次批量读，不逐股查库）。"""
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                return pd.read_sql(
                    "SELECT symbol, date, high, low, close, volume FROM stock_daily "
                    "WHERE date >= ? ORDER BY symbol, date",
                    conn,
                    params=(start,),
                )
        except Exception as exc:
            logger.error(f"AtrShrinkReversalStrategy 读取数据库失败：{exc}")
            return pd.DataFrame()

    def _rolling(self, df: pd.DataFrame, series: pd.Series, window: int, how: str) -> pd.Series:
        """按股票分组滚动，再把 groupby 多出来的一层索引吃掉、对齐回原索引。"""
        rolled = getattr(series.groupby(df["symbol"], sort=False).rolling(window), how)()
        return rolled.reset_index(level=0, drop=True).sort_index()

    def _atr(self, df: pd.DataFrame) -> pd.Series:
        """真实波幅均值：max(高−低, |高−昨收|, |低−昨收|) 的 N 日均值。"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.groupby(df["symbol"], sort=False).shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        return self._rolling(df, tr, self.atr_period, "mean")

    def run(self) -> list[str]:
        """返回今日「跌破 MA20 超 N 个 ATR 且缩量」的股票代码列表。"""
        # 「今日」取全市场 MAX(date)，与精筛、其余策略同一口径；
        # 停牌股不会出现在这一天，自然被排除，避免拿停牌前的旧价算出假信号。
        latest_date = self.engine.get_market_latest_date()
        if not latest_date:
            logger.warning("AtrShrinkReversalStrategy 库内无行情数据")
            return []

        start = (date.fromisoformat(latest_date) - timedelta(days=self._HISTORY_DAYS)).isoformat()
        df = self._fetch_recent(start)
        if df.empty:
            logger.warning("AtrShrinkReversalStrategy 无可用行情数据")
            return []

        df = df.assign(
            ma=self._rolling(df, df["close"], self.ma_period, "mean"),
            atr=self._atr(df),
        )
        # ATR 为 0（长期停牌/一字板）会让比值发散，直接落成 NaN 剔除
        df["dist_atr"] = (df["close"] - df["ma"]) / df["atr"].replace(0, float("nan"))
        vol_short = self._rolling(df, df["volume"], self.vol_short, "mean")
        vol_long = self._rolling(df, df["volume"], self.vol_long, "mean").replace(0, float("nan"))
        df["vol_ratio"] = vol_short / vol_long

        today = df[df["date"] == latest_date]
        deep = today[today["dist_atr"] < self.atr_threshold]
        hit = deep[deep["vol_ratio"].between(self.vol_ratio_min, self.vol_ratio_max)]

        # 跌得越深（ATR 倍数越小）越靠前；同值按代码，保证输出稳定
        hit = hit.sort_values(["dist_atr", "symbol"])
        selected = self.universe_filter.apply_without_ma(hit["symbol"].tolist())

        logger.info(
            f"AtrShrinkReversalStrategy 选出 {len(selected)} 只股票"
            f"（跌破 {abs(self.atr_threshold):g} 个 ATR {len(deep)} 只"
            f" → 缩量区间 {len(hit)} 只 → 精筛后 {len(selected)} 只，数据日 {latest_date}）"
        )
        return selected
