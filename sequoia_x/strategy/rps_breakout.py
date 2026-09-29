import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略

    **本策略刻意不受精筛池约束**（`applies_universe_filter = False`）：全系统只有
    RPS 是横截面指标，其余策略都是逐股纵向指标 —— 对它们来说「先精筛再算」与
    「全市场算完再精筛」必然等价，前置精筛只是省算力；RPS 不一样，排名基数决定
    每只票的百分位是多少，套上池子就换了口径。

    因此这里从**候选范围**到**最终结果**都走全市场：读全表算百分位，选出的强势股
    也不再与精筛池取交集。理由见下面 `run()` 的注释与回归测试
    `tests/test_strategy.py::TestRpsUniverseInteraction`。

    注意（对外播报时）：本策略的入选股票**不满足**报告/卡片页首那行「精筛：…」
    条件 —— 那行描述的是另外 6 个策略的候选范围。
    """

    webhook_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90
    applies_universe_filter: bool = False

    def run(self) -> list[str]:
        # 注意：RPS 是**横截面**指标（全市场涨幅百分位排名），因此这里刻意直接读全表，
        # 不限定在预筛池内 —— 只有在全市场里排进前 10%，才叫真正的相对强度。
        #
        # ⚠️ 不要为了「省算力」把下面的 SQL 改成只查池内（`WHERE symbol IN (...)`）。
        # 那样排名基数就从全市场变成池子，每只票的百分位会重排。实测（2026-09-22，
        # 全市场 5221 只 / 精筛池 917 只）：全市场口径选出 71 只，池内口径只剩 25 只，
        # 且是前者的**真子集** —— 会静默漏掉 46 只真正的全市场强势股。
        # （精筛池本身偏向强势股，池内前 10% 的门槛反而比全市场更高，所以只漏不多选。）
        #
        # 同理，末尾的 `apply_universe_filter()` 对本策略是**空操作**（类属性已声明豁免）：
        # 「全市场强势股 ∩ 精筛池」同样会把这 46 只剔掉，等于用精筛池覆盖全市场结论。
        # 回归测试：tests/test_strategy.py::TestRpsUniverseInteraction。
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            return []

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["symbol", "date"])

        # 纵向计算涨幅
        df["close_shift"] = df.groupby("symbol")["close"].shift(self.rps_period)
        df["pct_change"] = (df["close"] - df["close_shift"]) / df["close_shift"]

        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()
        latest_df = latest_df.dropna(subset=["pct_change"])

        # 横向排位 (RPS)
        latest_df["rps"] = latest_df["pct_change"].rank(pct=True) * 100
        strong_stocks = latest_df[latest_df["rps"] >= self.rps_threshold].copy()

        # 计算滚动最高价
        roll_high = (
            df.groupby("symbol")["high"]
            .rolling(window=self.rps_period, min_periods=self.rps_period // 2)
            .max()
            .reset_index(level=0, drop=True)
        )
        df["roll_high"] = roll_high

        latest_roll_high = df[df["date"] == latest_date][["symbol", "roll_high"]]
        strong_stocks = strong_stocks.merge(latest_roll_high, on="symbol")

        # 突破判定
        breakout_condition = strong_stocks["close"] >= strong_stocks["roll_high"] * 0.90
        selected = strong_stocks[breakout_condition]

        selected_symbols = self.apply_universe_filter(selected["symbol"].tolist())

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected_symbols)} 只股票")
        return selected_symbols
