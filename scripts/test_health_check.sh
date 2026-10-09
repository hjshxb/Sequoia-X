#!/usr/bin/env bash
# scripts/health_check.sh 的回归测试。
#
# 为什么需要：这个脚本的失败方式是「误导」而不是报错 —— 旧版（tmp/health_0928.sh）
#   把日期写死成 '2026-09-28'，过了那天它报出的「今天行数」永远是个历史值，而输出
#   看上去一切正常。所以这里专门锁住「日期确实是个参数」和「非法输入被拒」。
#
# 用法：bash scripts/test_health_check.sh
# 不联网：第 ④ 例自建一对**本地** TCP 连接来验证 socket 增量判据。
set -u

PROJ=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJ" || { echo "ERROR: 进不了项目根 $PROJ"; exit 1; }
PY=${PY:-/home/hxb/miniconda3/envs/sequoia-x/bin/python}

TMP=$(mktemp -d)
FAKE_PID=""
cleanup() {
  [ -z "$FAKE_PID" ] || kill "$FAKE_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

# 冒充 main.py 的本地进程：自建一对本地 TCP 连接，每秒往对端写 2048 字节。
# 必须让 cmdline 里出现 "main.py"，否则 health_check.sh 的 pgrep -f 'main.py'
# 命不中（用 bash 的 `exec -a main.py` 改写 argv[0] 来做到）。
cat > "$TMP/fake_main.py" <<'PYEOF'
import socket
import threading
import time

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
cli = socket.create_connection(srv.getsockname())
conn, _ = srv.accept()


def pump() -> None:
    while True:
        try:
            cli.sendall(b"x" * 2048)
        except OSError:
            return
        time.sleep(1)


threading.Thread(target=pump, daemon=True).start()
time.sleep(25)
PYEOF

PASS=0
FAIL=0

check() { # check <名称> <期望> <实际>
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1)); echo "  PASS  $1"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL  $1  -> 期望「$2」≠ 实际「$3」"
  fi
}

bash -n scripts/health_check.sh || { echo "语法错误，中止"; exit 1; }

echo "==== health_check.sh 回归测试 $(date '+%F %T') ===="

echo "---- ① 无进程时不空等 ----"
OUT=$(bash scripts/health_check.sh 2026-10-09 1 2>&1)
printf '%s' "$OUT" | /usr/bin/grep -qF "NO_PROCESS" && R=ok || R="未报 NO_PROCESS"
check "无 main.py 时报 NO_PROCESS" "ok" "$R"

echo "---- ② 日期是参数（旧版写死 2026-09-28）----"
OUT2=$(bash scripts/health_check.sh 2026-09-28 1 2>&1)
printf '%s' "$OUT" | /usr/bin/grep -qF "2026-10-09 行数" && R=ok || R="未报该日行数"
check "显式传 2026-10-09 → 报该日行数" "ok" "$R"
printf '%s' "$OUT2" | /usr/bin/grep -qF "2026-09-28 行数" && R=ok || R="未报该日行数"
check "显式传 2026-09-28 → 报该日行数" "ok" "$R"
printf '%s' "$OUT2" | /usr/bin/grep -qF "2026-10-09 行数" && R="串成当天了" || R=ok
check "  └ 不应顺手把当天日期也报出来" "ok" "$R"

echo "---- ③ 非法输入被拒（不静默退化成某个默认值）----"
bash scripts/health_check.sh 2026/09/28 >/dev/null 2>&1; check "非法日期 → exit 2" "2" "$?"
bash scripts/health_check.sh 2026-9-28 >/dev/null 2>&1; check "非补零日期 → exit 2" "2" "$?"
bash scripts/health_check.sh 2026-10-09 0 >/dev/null 2>&1; check "间隔 0 → exit 2" "2" "$?"
bash scripts/health_check.sh 2026-10-09 abc >/dev/null 2>&1; check "间隔非数字 → exit 2" "2" "$?"

echo "---- ④ socket 字节增量（本地连接，每秒 2048B）----"
bash -c "exec -a main.py $PY $TMP/fake_main.py" &
FAKE_PID=$!
/usr/bin/sleep 1
OUT3=$(bash scripts/health_check.sh "$(date +%F)" 3 2>&1)
printf '%s\n' "$OUT3" | /usr/bin/sed 's/^/    | /'
printf '%s' "$OUT3" | /usr/bin/grep -qE 'DELTA_3s sent=[1-9][0-9]*' && R=ok || R="没算出发送增量"
check "抓得到 socket 且 sent 增量 > 0" "ok" "$R"
printf '%s' "$OUT3" | /usr/bin/grep -qF "库内最新日期" && R=ok || R="没报库内最新日期"
check "  同时给出库内最新日期与当日行数" "ok" "$R"

echo "---- 结果：$PASS passed, $FAIL failed ----"
[ "$FAIL" = "0" ] || exit 1
