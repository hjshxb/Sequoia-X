"""一次性验证：用真实 .env 跑一遍「前置精筛」，确认筹码维度端到端可用。"""

import sys
import time

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data import holder_concentration  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402

logger = get_logger(__name__)

settings = get_settings()
print("=== 配置 ===")
print(f"min_top10_free_holding = {settings.min_top10_free_holding}")
print(f"max_top10_free_holding = {settings.max_top10_free_holding}")
print(f"holder_report_date     = {settings.holder_report_date!r}")
print(f"holder_cache_dir       = {settings.holder_cache_dir}")
print()

# 1) 先单独验一次数据模块（会落盘缓存）
t0 = time.time()
ratios = holder_concentration.load_top10_free_holding(
    report_date=settings.holder_report_date,
    cache_dir=settings.holder_cache_dir,
)
print(f"=== 筹码快照 === {len(ratios)} 只，耗时 {time.time() - t0:.1f}s")
vals = sorted(ratios.values())
if vals:
    n = len(vals)
    print(f"  中位数 {vals[n // 2]:.2f}%  均值 {sum(vals) / n:.2f}%")
    for th in (30, 40, 50, 60):
        k = sum(1 for v in vals if v >= th)
        print(f"  >= {th}% -> {k} 只 ({k / n * 100:.1f}%)")
print()

# 2) 再验一次缓存命中（应当零网络、几乎瞬时）
t0 = time.time()
holder_concentration.load_top10_free_holding(
    report_date=settings.holder_report_date,
    cache_dir=settings.holder_cache_dir,
)
print(f"=== 缓存命中耗时 {time.time() - t0:.2f}s ===")
print()

# 3) 真实运行前置精筛
engine = DataEngine(settings)
universe = UniverseFilter(settings=settings, engine=engine)
print("=== 精筛描述 ===")
print(universe.describe())
print()

t0 = time.time()
pool = universe.get_passing_universe()
dt = time.time() - t0
print(f"=== 前置精筛结果 === {len(pool)} 只，耗时 {dt:.1f}s")
print("样例:", pool[:10])
