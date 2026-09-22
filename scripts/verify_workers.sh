#!/usr/bin/env bash
# 验证 --workers / SYNC_WORKERS 的行为（全程不联网、不碰 baostock）。
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python

echo "── 1) --help 里能看到 --workers ──"
"$PY" main.py --help 2>&1 | /usr/bin/grep -A3 -- "--workers"

echo
echo "── 2) --workers 0 / 99 应被拒绝（退出码 2）──"
for bad in 0 99; do
  out=$("$PY" main.py --workers "$bad" --no-push 2>&1)
  code=$?
  echo "  --workers $bad -> exit=$code | $(printf '%s' "$out" | tail -1)"
done

echo
echo "── 3) SYNC_WORKERS=0 应在启动时报错 ──"
SYNC_WORKERS=0 "$PY" -c "
from sequoia_x.core.config import Settings
try:
    Settings(_env_file=None, feishu_webhook_url='x')
    print('  ✗ 未报错（不符合预期）')
except Exception as exc:
    line = [l for l in str(exc).splitlines() if 'sync_workers' in l][:1]
    print('  ✓ 已拒绝:', line)
"

echo
echo "── 4) 默认值 / 留空 / 显式值 ──"
"$PY" -c "
from sequoia_x.core.config import Settings
kw = dict(_env_file=None, feishu_webhook_url='x')
print('  默认     ->', Settings(**kw).sync_workers)
print('  留空(\"\") ->', Settings(**kw, sync_workers='').sync_workers)
print('  显式 6   ->', Settings(**kw, sync_workers=6).sync_workers)
print('  上限 32  ->', Settings(**kw, sync_workers=32).sync_workers)
"

echo
echo "── 5) 运行时覆盖（main.py 里 settings.sync_workers = args.workers）──"
"$PY" -c "
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
import tempfile, os
with tempfile.TemporaryDirectory() as d:
    s = Settings(_env_file=None, feishu_webhook_url='x', db_path=os.path.join(d, 't.db'))
    s.sync_workers = 7
    print('  赋值后 settings ->', s.sync_workers)
    print('  DataEngine 读到 ->', DataEngine(s).sync_workers)
"

echo
echo "── 6) 单进程路径实际日志（worker 已打桩，不联网）──"
"$PY" - <<'PYEOF'
import sqlite3, tempfile, os
import sequoia_x.data.engine as eng
from sequoia_x.core.config import Settings

eng._bs_fetch_batch = lambda tasks: []          # 打桩：绝不碰 baostock

with tempfile.TemporaryDirectory() as d:
    s = Settings(_env_file=None, feishu_webhook_url="x", db_path=os.path.join(d, "t.db"))
    e = eng.DataEngine(s)
    with sqlite3.connect(e.db_path) as c:
        c.executemany(
            "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover)"
            " VALUES (?,?,1,1,1,1,1,1)",
            [(f"60000{i}", "2000-01-01") for i in range(5)],
        )
        c.commit()
    print("  sync_workers =", e.sync_workers)
    e.sync_today_bulk()
PYEOF
