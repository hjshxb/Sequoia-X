#!/usr/bin/env bash
# 手动跑一次完整流程（同步 + 策略 + HTML 报告 + 飞书推送），输出落盘。
# 为什么要落盘：main.py 单进程串行时全市场同步要 ~33 分钟且中途无进度日志，
# 直接从前台 stdout 拿结果容易在超时/断连时把日志一起丢掉。
set -u
export PATH="/usr/bin:/bin:/c/Windows/System32:/c/Windows:/usr/local/bin:${PATH:-}"

PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
LOG="$PROJ/logs/run_push_$(date +%Y%m%d_%H%M%S).log"

cd "$PROJ" || exit 1
echo "==== run_push_once $(date '+%F %T') ===="
echo "log -> $LOG"

"$PY" -u main.py > "$LOG" 2>&1
RC=$?
echo "exit=$RC" >> "$LOG"
echo "main.py exit=$RC"
echo "LOG=$LOG"
exit $RC
