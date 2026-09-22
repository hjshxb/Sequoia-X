"""验证「均线」维度在真实库上的效果（只读本地库，不联网、不推送）。

用法：在仓库根目录执行 `python scripts/verify_ma.py`
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402

settings = Settings()
engine = DataEngine(settings)
f = UniverseFilter(settings=settings, engine=engine)

window = settings.ma_window
floor = settings.min_ma_deviation
print(f"均线窗口 MA_WINDOW = {window}；阈值 MIN_MA_DEVIATION = {floor}")
print(f"精筛条件：{f.describe()}")

base_date = engine.get_market_latest_date()
print(f"\n基准日（全市场最新交易日）：{base_date}")

all_symbols = engine.get_local_symbols()
print(f"全市场：{len(all_symbols)} 只")

# ── 1) 均线覆盖率与耗时 ──
t0 = time.time()
deviations = f._fetch_ma_deviations(all_symbols)
elapsed = time.time() - t0
missing = len(all_symbols) - len(deviations)
print(
    f"\nMA{window} 算得出：{len(deviations)} 只；算不出 {missing} 只"
    f"（次新股 / 长期停牌，历史不足 {window} 行）"
)
print(f"耗时：{elapsed:.1f}s（本地库窗口函数查询，零网络请求）")

if deviations:
    values = sorted(deviations.values())
    n = len(values)
    print(
        f"  偏离度分位：P10={values[n // 10]:+.2f}%  P50={values[n // 2]:+.2f}%  "
        f"P90={values[9 * n // 10]:+.2f}%  最高={values[-1]:+.2f}%  最低={values[0]:+.2f}%"
    )
    above = sum(1 for v in values if v >= 0)
    print(f"  站上 MA{window}：{above} 只（{above / n * 100:.1f}%）")


def apply(**kw):
    args = {
        "valuation": False,
        "liquidity": False,
        "technical": False,
        "today_drop": False,
        "ma": False,
        "industry": False,
        "holder": False,
    }
    args.update(kw)
    return f._apply(all_symbols, **args)


# ── 2) 逐级收敛（行业维度需联网，本次跳过）──
print("\n逐级收敛（行业维度跳过）：")
s_turn = apply(liquidity=True)
s_drop = apply(liquidity=True, today_drop=True)
s_ma = apply(liquidity=True, today_drop=True, ma=True)
s_hold = apply(liquidity=True, today_drop=True, ma=True, holder=True)

print(f"  全市场                          : {len(all_symbols)}")
print(
    f"  + 成交额 >= {settings.min_turnover:g}亿              : {len(s_turn)}"
    f"   (剔除 {len(all_symbols) - len(s_turn)})"
)
print(
    f"  + 今日跌幅 <= {settings.max_today_drop:g}%             : {len(s_drop)}"
    f"   (剔除 {len(s_turn) - len(s_drop)})"
)
print(
    f"  + 收盘价 >= MA{window}               : {len(s_ma)}"
    f"   (剔除 {len(s_drop) - len(s_ma)})"
)
print(
    f"  + 十大流通股东 >= {settings.min_top10_free_holding:g}%        : {len(s_hold)}"
    f"   (剔除 {len(s_ma) - len(s_hold)})"
)

# 均线那一步的剔除原因拆解：到底是跌破，还是压根没有均线数据
dev_in = {s: deviations[s] for s in s_drop if s in deviations}
kept = set(s_ma)
below = sum(1 for s in s_drop if s in dev_in and dev_in[s] < (floor or 0) and s not in kept)
nodata = sum(1 for s in s_drop if s not in dev_in)
print(f"    其中：跌破均线 {below} 只；历史不足 / 无行情 {nodata} 只")

# ── 3) 飞书卡片的条件行有长度上限，多一个条款后要确认不会被截断 ──
from sequoia_x.notify.feishu import _MAX_FILTER_CHARS, _elide  # noqa: E402

desc = f.describe(brief=True).removeprefix("精筛：")  # main.py 推送时的用法
print(f"\n飞书卡片条件行（{len(desc)}/{_MAX_FILTER_CHARS} 字符）：")
print(f"  {_elide(desc)}")
truncated = len(desc) > _MAX_FILTER_CHARS
print(f"  是否被截断：{'是 ⚠️ 需要调 brief 折叠策略' if truncated else '否 ✓'}")
