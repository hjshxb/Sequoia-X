"""RPS 预筛影响实证：对比「后置过滤」与「前置预筛」两种模式下的 RPS 选股结果。

结论预期：两者结果集完全一致（RPS 排名仍按全市场计算）。用完即删。
"""

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy  # noqa: E402

settings = Settings(_env_file=root / ".env")
engine = DataEngine(settings)

# ── A) 后置过滤（旧行为）：不注入池子，RPS 选完后自己过滤 ──
a = RpsBreakoutStrategy(engine=engine, settings=settings)
a_universe = None
res_a = a.run()

# ── B) 前置预筛（新行为）：先算全市场合格池，再注入 ──
universe = UniverseFilter(settings=settings, engine=engine)
pool = universe.get_passing_universe()

b = RpsBreakoutStrategy(engine=engine, settings=settings)
b.set_universe(pool)
res_b = b.run()

set_a, set_b = set(res_a), set(res_b)
print()
print("=" * 60)
print(f"合格股票池: {len(pool)} 只")
print(f"A 后置过滤: {len(res_a)} 只 -> {sorted(res_a)}")
print(f"B 前置预筛: {len(res_b)} 只 -> {sorted(res_b)}")
print(f"仅在 A: {sorted(set_a - set_b)}")
print(f"仅在 B: {sorted(set_b - set_a)}")
print(f"结果集是否完全一致: {set_a == set_b}")
print("=" * 60)
