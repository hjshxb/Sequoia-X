#!/usr/bin/env bash
# 一次性诊断脚本：判断 main.py 是「在干活」还是「卡死」。
# 判据（来自项目经验）：
#   WCHAN=poll_schedule_timeout  -> 正在等网络（baostock 抓取中）
#   WCHAN=p9_client_rpc          -> 正在向 /mnt/c 写 SQLite
#   /proc/<pid>/fd 里有 socket:  -> 有活跃连接
#   /proc/<pid>/fd 里有 *.db-journal -> 正在写库
set -u
echo "---- $(date '+%F %T') ----"

found=0
for p in $(pgrep -f 'main\.py'); do
  found=1
  wchan=$(cat "/proc/$p/wchan" 2>/dev/null)
  state=$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null)
  sock=$(ls -l "/proc/$p/fd" 2>/dev/null | grep -c 'socket:')
  jrnl=$(ls -l "/proc/$p/fd" 2>/dev/null | grep -c '\.db-journal')
  etime=$(ps -o etime= -p "$p" 2>/dev/null | tr -d ' ')
  echo "PID=$p STATE=$state WCHAN=${wchan:-?} 已运行=$etime socket数=$sock journal数=$jrnl"
done

if [ "$found" = "0" ]; then
  echo "未发现 main.py 进程（可能已结束或尚未启动）"
fi

echo "---- 本地库当日行数 ----"
DB=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X/data/sequoia_v2.db
TODAY=$(date +%F)
/home/hxb/miniconda3/envs/sequoia-x/bin/python - "$DB" "$TODAY" <<'PYEOF'
import sqlite3, sys
db, today = sys.argv[1], sys.argv[2]
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()
    cur.execute("SELECT MAX(date) FROM stock_daily")
    print("库内最新日期:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM stock_daily WHERE date=?", (today,))
    print(f"{today} 行数:", cur.fetchone()[0])
    con.close()
except Exception as exc:
    print("读库失败（锁/未提交属正常）:", exc)
PYEOF
