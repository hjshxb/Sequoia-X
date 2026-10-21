"""底部十字星 + 次日放量阳线确认（逆势低吸·形态档）。

数据日 T 是**确认日**，T-1 是**形态日**。两层条件全满足才入选：

形态日（T-1，逐股取上一根 K 线）：
  1. 十字星：|收盘−开盘| / (最高−最低) <= 0.2，且上下影线都存在
  2. 有振幅：振幅 (最高−最低)/收盘 >= 2%（排除一字板与近乎无成交的僵尸票）
  3. 处于底部：(收盘 − 60日最低) / (60日最高 − 60日最低) <= 0.15
  4. 缩量：5 日均量 / 20 日均量 <= 0.85
  5. 跌破 MA20 至少 5%（与条件 3 高度相关，用于排除「贴着均线的浅回调」）

确认日（T，全市场最新交易日）：
  6. 阳线：收盘 > 开盘
  7. 收红：收盘 >= 前收（`confirm_min_gain`，默认 0）
  8. 放量：当日量 / 含当日的 5 日均量 >= 1.2（`confirm_vol_min`）
  9. 未创前 5 日新高（`require_no_breakout`，默认开）

精筛：走 `apply_without_ma()`，且**成交额门槛单独放宽到 1 亿**（全局 3 亿）。
  理由见下面 ⑥：3 亿这一刀同时砍掉 76% 的信号和三分之一的超额。

为什么必须要有确认（用户口径：我们只选票，买不买看后续）：
  只靠形态选，池子 43.6 只/日，人根本看不过来；加上确认后 8.3 只/日，
  且**胜率与稳健性同时抬升**。实测（本地库 2024-01~2026-10，剔次新，20 日前向）：

    口径                                只/日   胜率   同日超额   日中位
    十字星当日入选（无确认）             43.6  57.6%   +1.23%   +0.03%
    + 确认（窗1、量1.2）                 9.3  60.3%   +1.08%   −0.29%
    + 确认 + 未创前5日新高（本类默认）    8.3  62.9%   +1.46%   +0.44%

  分年看本类默认口径三年全部为正超额（2024 +1.54% / 2025 +2.38% / 2026 +0.83%），
  胜率三年也都高于无确认版（68.9/82.3/47.6 对 62.0/79.0/46.0）。
  ⚠️ 2026 年中位数为 −3.71%，无确认版同样为 −2.57% —— 是这一年的市场环境问题，
     不是确认条件引入的；2026 全年本策略的均值仍为正超额 +0.83%。

参数是怎么定的（每一档都实测过，`tmp/probe_doji_confirm*.py`）：
  ① 🔴 **放量幅度是单调递减的，越猛越差**：
       量/MA5 1.2~1.5   7.5 只/日  62.0%  +1.54%  日中位 +0.73%
       量/MA5 1.5~2.0   3.9 只/日  60.7%  +0.44%  日中位 −0.93%
       量/MA5 2.0~3.0   1.9 只/日  42.7%  −0.86%  日中位 −1.65%
     ⇒ 阈值只能是 1.2 ~ 1.5 这一档，**不能往上加**。翻倍放量是「一日游」的特征。
  ② 确认窗口只能给 1 日（严格次日）。放宽到 2/3/5 日会把超额稀释回基准：
       <=1 日 +1.08% / <=2 日 +0.92% / <=3 日 +0.93% / <=5 日 +0.81%
  ③ ⚠️ 用「确认日涨幅上限」替代「未突破」这条路走不通：cap<=7% 会得到 +1.11%、
     但**用户举的 002472(+8.55%)、603119(+10.00%) 双双被剔除**（涨幅 8.55%/10%）。
     改用「未创前 5 日新高」后两只都保留（当时仍在前期结构下方），且指标更好。
     涨幅分档本身也不单调（0~2% +1.04% / 2~4% +1.58% / 4~7% +0.57% / >10% +0.65%），
     故不采用百分比上限。
  ④ 反向对照：把确认根换成「放量阴线」反而 +1.85%，「阳线但缩量」+1.07% ——
     说明「放量阳线」这一条不是靠方向取胜，而是靠**筛掉追高**。
     这也与项目里另外两次独立结论一致：威科夫 SOS（放量突破）−1.92%、
     底部十字星「次日涨>=5% 才买」+0.91% 对基线 +1.26%。A 股里「等确认」= 追高。

与 AtrShrinkReversal 的关系：加了确认之后两者几乎不重叠
  （Jaccard 0.0015，全期同日同票仅 10 次交集）⇒ 是真正的独立信号族。
  ⚠️ 旧版（十字星当日入选）曾记录「两者同时命中 +4.03%」，确认版已不适用，勿再引用。

⑥ 🔴 **成交额门槛砍掉的「主要是 alpha」，故本类单独放宽到 1 亿**：
     全局精筛要求成交额 >=3 亿，而底部缩量形态天然偏小盘（确认版 75.8% 的信号不到 3 亿）。
       全部信号           8.3 只/日  62.9%  +1.46%  日中位 +0.44%
       + 成交额 >=0.5亿   7.0 只/日  61.4%  +1.50%  日中位 +0.04%
       + 成交额 >=1.0亿   5.5 只/日  60.2%  +1.08%  日中位 +0.04%   ← 本类默认
       + 成交额 >=2.0亿   3.9 只/日  58.2%  +1.03%  日中位 −1.17%
       + 成交额 >=3.0亿   3.2 只/日  57.8%  +0.92%  日中位 −0.18%   ← 全局门槛
       + 成交额 >=5.0亿   2.4 只/日  52.3%  +0.48%  日中位 −2.24%
     门槛越高越差、且几乎单调 ⇒ 3 亿这一刀同时砍掉了 76% 的信号和三分之一的超额。
     放宽到 1 亿是「保住形态族」与「避免流动性过差」的折中；再往下（0.5 亿）收益更好
     但可成交性开始成问题，故不到 0.5 亿。
     ⚠️ 这是**风控取舍**：1 亿的票冲击成本明显高于 3 亿，仓位要相应放小。

⚠️ 必须走 `apply_without_ma()`：实测 92.9% 的形态落在 MA120 下方，
   沿用注入池会被「收盘价 >= MA120」整条清空。
⚠️ 信号稀疏且**间歇**：全期 2,146 个信号分布于 449 个交易日中的 262 天
   ⇒ 约 **40% 的交易日这个策略会空手**，这是正常的，不是故障。
"""

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BottomDojiStrategy(BaseStrategy):
    """底部十字星 + 次日放量阳线确认（逆势低吸·形态档）。

    Attributes:
        webhook_key: 路由到 'reversal' 专属飞书机器人（未配置时回退主 webhook）。
    """

    webhook_key: str = "reversal"

    # ---- 形态日（T-1）----
    # 十字星：实体占全幅比例上限。0.2 对应用户举的两只（0.04 / 0.15）；
    # 收紧到 0.1 会把 603119（0.15）漏掉
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

    # ---- 确认日（T）----
    # 放量下限：当日量 / 含当日 5 日均量。实测 1.2~1.5 是唯一有效的档位，
    # 1.5 以上收益转负（见模块 docstring ①），故默认取档位下沿
    confirm_vol_min: float = 1.2
    confirm_vol_win: int = 5
    # 收红下限：收盘 / 前收 − 1。0 = 至少要收在前收之上
    confirm_min_gain: float = 0.0
    # 未创前 N 日新高。开了以后收益/胜率/日中位全面优于关掉；
    # 但若想看「已经放量突破」的票，把这里置 False 即可
    require_no_breakout: bool = True
    breakout_lookback: int = 5

    # ---- 策略级放宽的成交额门槛（亿元）----
    # 全局精筛是 3 亿，但底部缩量形态天然偏小盘：确认版 75.8% 的信号成交额不到 3 亿，
    # 而「成交额门槛」砍掉的**主要是 alpha**（详见模块 docstring ⑥）。
    # 这里放宽到 1 亿以保住形态族，同时避免流动性过差的票。
    # 只有比全局更松时才生效（见 `_turnover_override`）—— 全局没配成交额维度时不会被打开。
    min_turnover_yi: float = 1.0

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

    def _shift(self, df: pd.DataFrame, series: pd.Series, periods: int = 1) -> pd.Series:
        """按股票分组位移，避免跨股票串行（每只票的第一根位移后为 NaN）。"""
        return series.groupby(df["symbol"], sort=False).shift(periods)

    def _turnover_override(self) -> float | None:
        """返回本次调用要用的成交额下限覆盖值；不需要放宽时返回 None。

        只在「全局配了成交额、且本策略的门槛确实更松」时返回覆盖值 ——
        `apply_without_ma(min_turnover=...)` 是**覆盖**语义，
        无条件传值会在全局未启用该维度时把它强行打开，等于凭策略配置改变全局行为。
        """
        global_floor = self.settings.min_turnover
        if global_floor is None or self.min_turnover_yi >= global_floor:
            return None
        return self.min_turnover_yi

    def run(self) -> list[str]:
        """返回今日「昨日底部十字星 + 今日放量阳线确认」的股票代码列表。"""
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
        ma20 = self._rolling(df, df["close"], 20, "mean").replace(0, np.nan)
        dist_ma20 = df["close"] / ma20 - 1

        # ---- 形态日：逐股取上一根的十字星特征 ----
        doji = (
            body_ratio.le(self.body_ratio_max)
            & amp.ge(self.amp_min)
            & both_wick
            & pos60.le(self.pos60_max)
            & dist_ma20.le(self.dist_ma20_max)
            & vol_ratio.le(self.vol_ratio_max)
        ).fillna(False)
        doji_prev = self._shift(df, doji.astype(float)).fillna(0.0) > 0
        pos60_prev = self._shift(df, pos60)

        # ---- 确认日：阳线 + 收红 + 放量 + 未创前 N 日新高 ----
        prev_close = self._shift(df, df["close"])
        gain = df["close"] / prev_close.replace(0, np.nan) - 1
        yang = df["close"].gt(df["open"]) & gain.ge(self.confirm_min_gain)
        vol_ma = self._rolling(df, df["volume"], self.confirm_vol_win, "mean").replace(0, np.nan)
        surged = (df["volume"] / vol_ma).ge(self.confirm_vol_min)
        hi_n_prev = self._shift(df, self._rolling(df, df["high"], self.breakout_lookback, "max"))
        no_breakout = df["close"].le(hi_n_prev)

        today = df["date"] == latest_date
        base = today & doji_prev
        n_doji = int(base.sum())
        n_yang = int((base & yang).sum())
        n_vol = int((base & yang & surged).sum())
        hit_mask = base & yang & surged
        if self.require_no_breakout:
            hit_mask = hit_mask & no_breakout

        hit = df[hit_mask]
        # 形态日位置越低越靠前；同值按代码，保证输出稳定
        hit = hit.assign(_pos60=pos60_prev.loc[hit.index]).sort_values(["_pos60", "symbol"])
        codes = hit["symbol"].tolist()
        override = self._turnover_override()
        selected = self.universe_filter.apply_without_ma(codes, min_turnover=override)

        chain = (
            f"昨日十字星 {n_doji} 只 → 今日阳线收红 {n_yang} 只 → 放量 {self.confirm_vol_min}x "
            f"{n_vol} 只"
        )
        if self.require_no_breakout:
            chain += f" → 未创前{self.breakout_lookback}日新高 {len(hit)} 只"
        turnover_note = (
            f"，成交额门槛放宽到 {override:g} 亿（全局 {self.settings.min_turnover:g} 亿）"
            if override is not None
            else ""
        )
        logger.info(
            f"BottomDojiStrategy 选出 {len(selected)} 只股票"
            f"（{chain} → 精筛后 {len(selected)} 只，数据日 {latest_date}{turnover_note}）"
        )
        return selected
