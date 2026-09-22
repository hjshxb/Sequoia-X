#!/usr/bin/env bash
# 只读检查：本地库现状 + baostock 版本（不联网）
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python

"$PY" - <<'PYEOF'
import sqlite3, importlib.metadata as md
con = sqlite3.connect("data/sequoia_v2.db")
print("stock_daily 总行数 :", con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0])
print("标的只数           :", con.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0])
print("最新 5 个交易日     :")
for d, n in con.execute(
    "SELECT date, COUNT(*) FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 5"
):
    print(f"   {d}  {n} 只")
try:
    print("baostock 版本      :", md.version("baostock"))
except Exception as e:
    print("baostock 版本      : 未知", e)
PYEOF
