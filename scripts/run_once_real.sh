#!/usr/bin/env bash
# 真实跑一次 main.py（--no-push 保险），观察「数据源不可用」护栏的行为：
# 期望 = 1 次 login、0 次行情请求、ERROR 日志、退出码 1、reports/ 无新文件。
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python

BEFORE=$(ls -1 reports/ | sort)
T0=$(date +%s)

echo "---- 运行 main.py --no-push ----"
"$PY" -u main.py --no-push 2>&1 | tail -30
RC=${PIPESTATUS[0]}
T1=$(date +%s)

echo
echo "退出码         : $RC"
echo "耗时           : $((T1 - T0)) 秒"
AFTER=$(ls -1 reports/ | sort)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "reports/       : 无新文件 ✓"
else
  echo "reports/       : 出现新文件 ✗"
  diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER")
fi
echo
echo "---- 本地库最新日期（应仍是 09-18，未被写入）----"
"$PY" -c "
import sqlite3
c = sqlite3.connect('data/sequoia_v2.db')
print(' ', c.execute('SELECT MAX(date) FROM stock_daily').fetchone()[0])
"
