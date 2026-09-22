"""验证「今日跌幅」维度在真实库上的效果（只读本地库 + 筹码磁盘缓存，不联网、不推送）。

用法：在仓库根目录执行 `python scripts/verify_today_drop.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.data import stock_meta  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402

settings = Settings()
engine = DataEngine(settings)
stock_meta.configure_cache(settings.db_path, settings.stock_meta_ttl_days)

f = UniverseFilter(settings=settings, engine=engine)
print(f"配置：{f.describe()}")
print(f"筹码阈值 MIN_TOP10_FREE_HOLDING = {settings.min_top10_free_holding}")
print(f"今日跌幅 MAX_TODAY_DROP = {settings.max_today_drop}")

base_date = engine.get_market_latest_date()
print(f"\n基准日（全市场最新交易日）：{base_date}")

all_symbols = engine.get_local_symbols()
print(f"全市场：{len(all_symbols)} 只")

drops = f._fetch_today_drops(all_symbols)
missing = len(all_symbols) - len(drops)
print(f"\n今日跌幅可取到：{len(drops)} 只；缺失 {missing} 只（当日停牌 / 次新股无昨收）")

if drops:
    values = sorted(drops.values())
    n = len(values)
    print(
        f"  跌幅分位：P10={values[n // 10]:.2f}%  P50={values[n // 2]:.2f}%  "
        f"P90={values[9 * n // 10]:.2f}%  最惨={values[-1]:.2f}%"
    )
    up = sum(1 for v in values if v < 0)
    print(f"  其中上涨 {up} 只，下跌 {n - up} 只")
    if settings.max_today_drop is not None:
        over = sum(1 for v in values if v > settings.max_today_drop)
        print(f"  跌超 {settings.max_today_drop:g}% 的：{over} 只（将被剔除）")

print("\n逐级收敛（行业维度需联网，本次跳过）：")
s1 = f._apply(
    all_symbols, valuation=False, liquidity=True, technical=False, today_drop=False, industry=False
)
s2 = f._apply(
    all_symbols,
    valuation=False,
    liquidity=True,
    technical=False,
    today_drop=True,
    industry=False,
)
s3 = f._apply(
    all_symbols,
    valuation=False,
    liquidity=True,
    technical=False,
    today_drop=True,
    industry=False,
    holder=True,
)
print(f"  全市场            : {len(all_symbols)}")
print(f"  + 成交额 >= {settings.min_turnover:g}亿    : {len(s1)}  (剔除 {len(all_symbols) - len(s1)})")
print(f"  + 今日跌幅 <= {settings.max_today_drop:g}%   : {len(s2)}  (剔除 {len(s1) - len(s2)})")
print(
    f"  + 十大流通股东 >= {settings.min_top10_free_holding:g}% : {len(s3)}"
    f"  (剔除 {len(s2) - len(s3)})"
)

# ── 飞书卡片的条件行有长度上限，多一个条款后要确认不会被截断 ──
from sequoia_x.notify.feishu import _MAX_FILTER_CHARS, _elide  # noqa: E402

desc = f.describe(brief=True).removeprefix("精筛：")  # main.py 推送时的用法
print(f"\n飞书卡片条件行（{len(desc)}/{_MAX_FILTER_CHARS} 字符）：")
print(f"  {_elide(desc)}")
print(f"  是否被截断：{'是 ⚠️ 需要调 brief 折叠策略' if len(desc) > _MAX_FILTER_CHARS else '否 ✓'}")
