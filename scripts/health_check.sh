#!/usr/bin/env bash
# 判活：区分「健康但慢」与「卡死」。
#
# 为什么必须看 socket 字节的**增量**（而不是别的信号）：
#   · rchar 只统计 read()/write()，**不计 recv**，所以同步在收数据时它几乎不动；
#   · WCHAN 停在 poll_schedule_timeout（等网络）或 p9_client_rpc（写 /mnt/c）时，
#     「正在等 baostock 回包」与「连接其实已经死了」是**同一个值** —— 它只能说明
#     在等什么，说明不了还在不在动。
#   唯一可信的判据是 ss 明细行里 bytes_sent / bytes_received 的增量：健康同步约
#   +18000 字节/分钟，一次完整的全市场增量同步 ≈745KB。
#   （scripts/check_alive.sh 用的是 WCHAN/fd，只能粗看状态，不能判活。）
#
# 用法：
#   bash scripts/health_check.sh                 # 采样今天，间隔 60s
#   bash scripts/health_check.sh 2026-09-28      # 指定要看行数的日期
#   bash scripts/health_check.sh 2026-09-28 30   # 再用 30s 间隔采样
#
# 旧版（tmp/health_0928.sh）把日期写死成 '2026-09-28'，过了那天它报出的
# 「今天行数」就永远是个历史值 —— 判活脚本本身会误导人，这类写死必须去掉。
set -u
DATE=${1:-$(date +%F)}
INTERVAL=${2:-60}

case "$DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *) echo "ERROR: 日期格式应为 YYYY-MM-DD，收到 '$DATE'"; exit 2 ;;
esac
case "$INTERVAL" in
  '' | *[!0-9]* | 0) echo "ERROR: 采样间隔必须是正整数秒，收到 '$INTERVAL'"; exit 2 ;;
esac

# 项目根按脚本自身位置推导，不依赖调用者的 cwd。
# （当初在工作区根目录凭空冒出一个空壳库，根因就是某次调用没有正确的 cwd。）
PROJ=$(cd "$(dirname "$0")/.." && pwd)
DB="$PROJ/data/sequoia_v2.db"
PY=${PY:-/home/hxb/miniconda3/envs/sequoia-x/bin/python}

echo "==== 判活 $(date '+%F %T')  项目=$PROJ ===="

PID=$(/usr/bin/pgrep -f 'main.py' | /usr/bin/head -1)
if [ -z "$PID" ]; then
  echo "NO_PROCESS：当前没有在跑的 main.py（可能已结束，也可能还没启动）"
else
  echo "PID=$PID  $(/usr/bin/ps -o stat=,wchan=,etime= -p "$PID")"

  grab() {
    /usr/bin/ss -tinp 2>/dev/null | /usr/bin/grep -A1 "pid=$PID," \
      | /usr/bin/grep -oE 'bytes_sent:[0-9]+|bytes_received:[0-9]+' \
      | /usr/bin/awk -F: '{s[$1]+=$2} END {printf "%d %d", s["bytes_sent"], s["bytes_received"]}'
  }

  A=$(grab); S1=$(echo "$A" | /usr/bin/cut -d' ' -f1); R1=$(echo "$A" | /usr/bin/cut -d' ' -f2)
  echo "t0      sent=$S1 recv=$R1"
  /usr/bin/sleep "$INTERVAL"
  B=$(grab); S2=$(echo "$B" | /usr/bin/cut -d' ' -f1); R2=$(echo "$B" | /usr/bin/cut -d' ' -f2)
  echo "t${INTERVAL}s     sent=$S2 recv=$R2"

  if [ "$S1" = "0" ] && [ "$R1" = "0" ] && [ "$S2" = "0" ] && [ "$R2" = "0" ]; then
    echo "DELTA_${INTERVAL}s 两个采样点都没抓到该 PID 的 socket 计数"
    echo "        → 进程在但已无活跃连接：可能已收工，也可能是连接断了还挂着，"
    echo "          配合 ps 的 STAT/WCHAN 与其运行时长一起看。"
  else
    echo "DELTA_${INTERVAL}s sent=$((S2 - S1)) recv=$((R2 - R1))   (参考：健康约 +18000 字节/分钟)"
  fi
fi

echo "---- 本地库（目标日期 $DATE）----"
"$PY" - "$DB" "$DATE" <<'PYEOF'
import os
import sqlite3
import sys

db, day = sys.argv[1], sys.argv[2]
if not os.path.exists(db):
    print("库文件不存在:", db)
    raise SystemExit
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()
    cur.execute("SELECT MAX(date) FROM stock_daily")
    print("库内最新日期:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM stock_daily WHERE date=?", (day,))
    n = cur.fetchone()[0]
    note = "" if n else "   ← 0 行：既可能是「还没同步到今天」，也可能是「被整列清空」，需再核"
    print(f"{day} 行数: {n}{note}")
    con.close()
except Exception as exc:
    print("读库失败（锁/未提交属正常）:", exc)
PYEOF
