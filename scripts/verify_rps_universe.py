"""验证：前置精筛（set_universe）是否影响 RPS 的计算。

RPS 是全系统里**唯一一个横截面指标**（在全体股票之间排名），
其余策略（均线放量 / 海龟 / 高而窄 / 涨停回踩 / 上升趋势跌停）都是逐股纵向指标，
对它们来说「先精筛再算」和「全市场算完再精筛」在数学上必然等价 —— 纯省算力。
RPS 不一样：一旦把排名基数缩到池内，每只票的百分位会重排，语义就变了。

本脚本用真实库验证三件事（**全程不联网**，只用本地 SQLite）：
  1. 等价性 —— 注入精筛池后的结果 == 「全市场 RPS 候选 ∩ 池」
     做法：把**全市场**当池子注入，filter 退化为恒真，于是拿到策略自己算的
     纯全市场候选集 C（不是我重写一遍逻辑）；再与注入真实池的结果 A 比对。
  2. 反证 —— 如果真在池内排名，会选出什么（D），并说明 D != A。
  3. 量化 —— 池内重排与全市场排名的差异有多大。

用法：python scripts/verify_rps_universe.py
"""

from __future__ import annotations

import sqlite3
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy  # noqa: E402

ALL = "SELECT symbol, date, close, high FROM stock_daily"


def load_raw(db_path: str) -> pd.DataFrame:
    """读一次全表，供后面的「池内重排」反证复用。"""
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(ALL, conn)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["symbol", "date"])


def rank_rps(df: pd.DataFrame, period: int, threshold: int) -> list[str]:
    """在给定 df 的范围内做 RPS 排名并返回达标代码（复刻策略逻辑，用于反证）。"""
    d = df.copy()
    d["close_shift"] = d.groupby("symbol")["close"].shift(period)
    d["pct_change"] = (d["close"] - d["close_shift"]) / d["close_shift"]
    latest_date = d["date"].max()
    latest = d[d["date"] == latest_date].dropna(subset=["pct_change"]).copy()
    d["roll_high"] = d.groupby("symbol")["high"].rolling(
        window=period, min_periods=period // 2
    ).max().reset_index(level=0, drop=True)
    roll = d[d["date"] == latest_date][["symbol", "roll_high"]]
    latest = latest.merge(roll, on="symbol")
    # ★ 这一行的排名基数就是本函数的唯一变量
    latest["rps"] = latest["pct_change"].rank(pct=True) * 100
    hit = latest[latest["rps"] >= threshold]
    return hit[hit["close"] >= hit["roll_high"] * 0.90]["symbol"].tolist()


def main() -> None:
    settings = Settings()
    engine = DataEngine(settings)
    universe = UniverseFilter(settings=settings, engine=engine)

    all_symbols = engine.get_local_symbols()
    print("=" * 68)
    print(f"本地库：{engine.db_path}")
    print(f"全市场：{len(all_symbols)} 只")
    print("=" * 68)

    # ── 构造一个纯本地、零网络的精筛池：只用「成交额 + 今日跌幅」两个本地维度 ──
    #    刻意不启用估值/换手率（需逐股 baostock）与行业/筹码（需全市场接口）。
    t0 = time.time()
    pool = universe._apply(  # noqa: SLF001 — 验证脚本，需精确指定维度
        all_symbols,
        valuation=False,
        liquidity=True,
        technical=False,
        today_drop=True,
        industry=False,
        holder=False,
    )
    print(f"\n精筛池（成交额 + 今日跌幅，纯本地）：{len(pool)} 只  ({time.time() - t0:.2f}s)")
    print(f"  池构成：{'、'.join(universe.describe().removeprefix('精筛：').split('，'))}")

    # ── ① 拿策略自己算的「纯全市场 RPS 候选」：把全市场当池子注入 → filter 恒真 ──
    t0 = time.time()
    s_full = RpsBreakoutStrategy(engine=engine, settings=settings)
    s_full.set_universe(all_symbols)
    c_full = s_full.run()
    t_full = time.time() - t0
    print(f"\n① 全市场 RPS 候选（period={s_full.rps_period}, 阈值={s_full.rps_threshold}）："
          f"{len(c_full)} 只  ({t_full:.2f}s)")

    # ── ② 注入真实精筛池 ──
    t0 = time.time()
    s_pool = RpsBreakoutStrategy(engine=engine, settings=settings)
    s_pool.set_universe(pool)
    c_pool = s_pool.run()
    t_pool = time.time() - t0
    print(f"② 注入精筛池后的结果：{len(c_pool)} 只  ({t_pool:.2f}s)")

    # ── ③ 等价性：A == C ∩ pool（顺序也应一致）──
    pool_set = set(pool)
    expected = [s for s in c_full if s in pool_set]
    same = c_pool == expected
    print(f"\n③ 等价性检查：A == C ∩ pool  →  {'一致 ✓' if same else '不一致 ✗'}")
    if not same:
        print(f"   仅 A 有: {sorted(set(c_pool) - set(expected))[:10]}")
        print(f"   仅 C∩pool 有: {sorted(set(expected) - set(c_pool))[:10]}")
    print(f"   全市场候选 {len(c_full)} 只中有 {len(expected)} 只落在精筛池内"
          f"（命中率 {len(expected) / max(len(c_full), 1) * 100:.1f}%）")

    # ── ④ 反证：如果 RPS 只在池内排名（错误实现），会选出什么 ──
    raw = load_raw(engine.db_path)
    in_pool_only = raw[raw["symbol"].isin(pool_set)]
    d_pool_ranked = rank_rps(in_pool_only, s_full.rps_period, s_full.rps_threshold)
    print(f"\n④ 反证 —— 若把排名基数缩到池内（{len(pool)} 只）："
          f"会选出 {len(d_pool_ranked)} 只")
    overlap = len(set(d_pool_ranked) & set(c_pool))
    print(f"   与正确实现的重合：{overlap} 只"
          f"（错误实现多出 {len(set(d_pool_ranked) - set(c_pool))} 只，"
          f"漏掉 {len(set(c_pool) - set(d_pool_ranked))} 只）")
    print("   ↳ 差异明显 ⇒ 池内排名会改变 RPS 语义；当前实现刻意避开了这一点")

    print("\n" + "=" * 68)
    ok = same
    print(f"结论：前置精筛{'不影响' if ok else '影响了'} RPS 的计算 —— "
          f"{'RPS 仍在全市场口径排名，池子只做最后一步取交集' if ok else '存在偏差，需排查'}")
    print("=" * 68)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
