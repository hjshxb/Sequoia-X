"""端到端验证：启用全部维度（含新加的均线）后，精筛最终股票池有多大。

只读本地行情库；第 2 级需要一次 baostock 登录逐股拉取估值 / 换手率，
但只作用于第 1 级的残余（本次约 300 只，远低于触发风控的量级）。
**不推送、不写库、不改报告。**

用法：在仓库根目录执行 `python scripts/verify_ma_universe.py`
"""

from __future__ import annotations

import sys
import time
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
print(f"精筛条件：{f.describe()}\n")

total = len(engine.get_local_symbols())
t0 = time.time()
pool = f.get_passing_universe()
elapsed = time.time() - t0

print(f"\n全市场 {total} 只 -> 最终股票池 {len(pool)} 只（耗时 {elapsed:.0f}s）")
