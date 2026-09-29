import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略。

    ⚠️ 本策略的**计算口径**与其他策略不同，改动前请先读完这段。

    RPS 是横截面指标（全市场 120 日涨幅的百分位排名），所以它分成两段、两段的
    股票范围刻意不一样：

    1. **计算阶段走全市场** —— `run()` 直接读整张 `stock_daily` 算百分位，**不**用
       `candidate_symbols()` 取精筛池。排名基数必须是全市场，缩到池内每只票的
       百分位就会重排（实测 2026-09-22：全市场口径选 71 只，池内口径只剩 25 只）。
    2. **结果阶段照常过精筛** —— 算完之后与其他 6 个策略完全一样，末尾调用
       `apply_universe_filter()`，即「全市场强势股 ∩ 精筛池」。当日强势不等于
       符合选股条件，池外的票不进最终榜单。

    对逐股纵向指标的策略（其余 6 个），这两段是等价的、「先精筛再算」只是省算力；
    对 RPS 不等价，因为排名基数会变 —— 所以它必须两段分开。回归测试见
    `tests/test_strategy.py::TestRpsUniverseInteraction`。
    """

    webhook_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90

    def run(self) -> list[str]:
        # ① 计算阶段刻意读全表（见类文档第 1 条）。RPS 只有在**全市场**里排进
        #    前 10%，才叫真正的相对强度；限定池内等于换掉排名基数。
        #
        #    ⚠️ 不要为了「省算力」把下面的 SQL 改成 `WHERE symbol IN (...)`
        #    或改用 `self.candidate_symbols()`。实测（2026-09-22，全市场 5221 只 /
        #    精筛池 917 只）：全市场口径选出 71 只，池内口径只剩 25 只，且是前者的
        #    真子集 —— 会静默漏掉 46 只真正的全市场强势股。
        #    （精筛池本身偏向强势股，池内前 10% 的门槛反而比全市场更高，所以只漏不多选。）
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

        # ② 结果阶段照常过精筛（与其余 6 个策略一致）：当日强势不等于符合选股条件，
        #    池外的票不进最终榜单。已注入池子时这一步退化为「是否在池内」的内存判定。
        selected_symbols = self.apply_universe_filter(selected["symbol"].tolist())

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected_symbols)} 只股票")
        return selected_symbols
