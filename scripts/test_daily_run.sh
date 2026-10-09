#!/usr/bin/env bash
# daily_run.sh 的回归测试，两段：
#   第 1 段 —— 交易日判断分支。这个脚本最危险的失败模式是「静默」：数据源挂了却被
#             报成「今天休市，正常跳过」，退出码还是 0，定时任务一路绿。
#             4 个分支全部用假 baostock（PYTHONPATH 注入）覆盖，不联网。
#   第 2 段 —— 报告校验（第 3 步）。只判文件是否存在会让「同名旧文件」冒充成功：
#             main.py 的报告生成失败只记日志、不改退出码，所以 RC=0 也可能配一份
#             没重写的旧报告。用 DAILY_RUN_PY 注入桩冒充 main.py、DAILY_RUN_REPORT
#             把报告指到临时目录，从而**离线且不碰真报告**地验新鲜度判据。
#
# 用法：bash scripts/test_daily_run.sh
set -u

PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
cd "$PROJ" || { echo "ERROR: 项目目录不存在 $PROJ"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/fake"

# ── 假 baostock：行为由 FAKE_BS_MODE 决定，通过 PYTHONPATH 覆盖真模块 ──
cat > "$TMP/fake/baostock.py" <<'PYEOF'
"""仅用于 test_daily_run.sh 的 baostock 替身，不联网。"""
import os

MODE = os.environ.get("FAKE_BS_MODE", "trade_day")


class _LoginResult:
    def __init__(self, error_code, error_msg):
        self.error_code = error_code
        self.error_msg = error_msg


class _ResultSet:
    def __init__(self, flag, error_code="0", error_msg="success"):
        self.error_code = error_code
        self.error_msg = error_msg
        self._flag = flag
        self._served = False

    def next(self):
        if self._served:
            return False
        self._served = True
        return True

    def get_row_data(self):
        # fields: [calendar_date, is_trading_day]
        return ["2026-09-22", self._flag]


def login(user_id="anonymous", password="123456"):
    if MODE == "login_fail":
        return _LoginResult("10001011", "黑名单用户，请与管理员联系")
    if MODE == "login_network":
        return _LoginResult("10002007", "网络接收错误。")
    # 真实 baostock 登录成功会**直接往 stdout 打印**这行（库内部的 print，
    # 不是日志）。必须模拟，否则测试抓不到「噪音污染 $(...) 捕获」这一类问题
    # —— 真机上踩过：IS_TRADE 变成 "login success!\nlogout success!\n1"。
    print("login success!")
    return _LoginResult("0", "success")


def logout():
    print("logout success!")
    return _LoginResult("0", "success")


def query_trade_dates(start_date="", end_date=""):
    if MODE == "holiday":
        return _ResultSet("0")
    if MODE == "calendar_error":
        return _ResultSet("", error_code="10002007", error_msg="网络接收错误。")
    return _ResultSet("1")
PYEOF

PASS=0
FAIL=0

# run_case <名称> <FAKE_BS_MODE> <期望退出码> <期望输出包含> <期望输出不含>
run_case() {
  local name="$1" mode="$2" want_rc="$3" want_out="$4" reject_out="${5:-}"
  local out rc
  out=$(FAKE_BS_MODE="$mode" PYTHONPATH="$TMP/fake" DAILY_RUN_DRY=1 \
        bash scripts/daily_run.sh 2>&1)
  rc=$?

  local ok=1 reason=""
  if [ "$rc" != "$want_rc" ]; then
    ok=0; reason="退出码 $rc != $want_rc"
  elif [ -n "$want_out" ] && ! printf '%s' "$out" | /usr/bin/grep -qF "$want_out"; then
    ok=0; reason="输出缺少「$want_out」"
  elif [ -n "$reject_out" ] && printf '%s' "$out" | /usr/bin/grep -qF "$reject_out"; then
    ok=0; reason="输出不应包含「$reject_out」"
  fi

  if [ "$ok" = "1" ]; then
    PASS=$((PASS + 1))
    echo "  PASS  $name  (exit=$rc)"
  else
    FAIL=$((FAIL + 1))
    echo "  FAIL  $name  -> $reason"
    echo "-------- 实际输出 --------"
    printf '%s\n' "$out" | /usr/bin/sed 's/^/    /'
    echo "--------------------------"
  fi
}

echo "==== daily_run.sh 分支测试 $(date '+%F %T') ===="

run_case "休市 → exit 0，正常跳过"          holiday        0 "不是交易日"
run_case "交易日 → 继续往下（演练模式退出）"  trade_day      0 "DAILY_RUN_DRY=1"
run_case "  └ 不应误报为休市"               trade_day      0 "" "不是交易日"
run_case "黑名单 → exit 3，且带出错误码"     login_fail     3 "ERR:login:10001011"
run_case "  └ 不应伪装成休市"               login_fail     3 "" "正常跳过"
run_case "网络故障 → exit 3"                login_network  3 "10002007"
run_case "交易日历查询报错 → exit 3"         calendar_error 3 "ERR:calendar"

# ═══════════════════════════════════════════════════════════════════════════
# 第 2 段：第 3 步的报告校验（不设 DAILY_RUN_DRY，用桩代替 main.py）
# ═══════════════════════════════════════════════════════════════════════════

# 冒充 main.py 的桩：忽略传进来的 "-u main.py"，只看 STUB_MODE。
# 报告路径由 DAILY_RUN_REPORT 给出（daily_run.sh 从环境里读到，子进程自然继承）。
STUB="$TMP/stub_main.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
case "${STUB_MODE:-none}" in
  write) printf '<html>stub report</html>' > "$DAILY_RUN_REPORT" ;;
  none)  : ;;          # 什么都不做：旧报告留着 / 报告本来就不存在
  fail)  exit 1 ;;     # 冒充「main.py 自己失败」
esac
exit 0
STUBEOF
chmod +x "$STUB"

RFILE="$TMP/report_step3.html"

# 与 run_case 同构，差别只在环境变量：用桩跑主流程、报告落到临时目录。
# 报告文件由各用例自己预置（本函数不删不建，否则「旧文件」用例没法准备现场）。
# ENABLED 对应用例里的「报告开关」接缝，默认 1（开启），第 ⑤⑥ 例设为 0。
# 注意必须经 /usr/bin/env 传参：`${VAR:-}` 展开出来的 `NAME=value` 不会被 bash
# 当成赋值前缀（赋值是在展开**之前**按语法认出来的），只会被当成命令名而报
# "command not found"、退出 127。
run_report_case() {
  local name="$1" mode="$2" want_rc="$3" want_out="$4" reject_out="${5:-}"
  local out rc
  out=$(/usr/bin/env FAKE_BS_MODE=trade_day PYTHONPATH="$TMP/fake" STUB_MODE="$mode" \
        DAILY_RUN_PY="$STUB" DAILY_RUN_REPORT="$RFILE" \
        DAILY_RUN_REPORT_ENABLED="${ENABLED:-1}" \
        bash scripts/daily_run.sh 2>&1)
  rc=$?

  local ok=1 reason=""
  if [ "$rc" != "$want_rc" ]; then
    ok=0; reason="退出码 $rc != $want_rc"
  elif [ -n "$want_out" ] && ! printf '%s' "$out" | /usr/bin/grep -qF "$want_out"; then
    ok=0; reason="输出缺少「$want_out」"
  elif [ -n "$reject_out" ] && printf '%s' "$out" | /usr/bin/grep -qF "$reject_out"; then
    ok=0; reason="输出不应包含「$reject_out」"
  fi

  if [ "$ok" = "1" ]; then
    PASS=$((PASS + 1))
    echo "  PASS  $name  (exit=$rc)"
  else
    FAIL=$((FAIL + 1))
    echo "  FAIL  $name  -> $reason"
    echo "-------- 实际输出 --------"
    printf '%s\n' "$out" | /usr/bin/sed 's/^/    /'
    echo "--------------------------"
  fi
}

echo "---- 第 3 步：报告新鲜度校验 ----"

# ① 本次确实写出来了 → 成功
rm -f "$RFILE"
run_report_case "写出新报告 → exit 0 判为本次生成" write 0 "OK 报告已生成"

# ② 本次修的核心：同名旧文件留在原地，桩一行没写 → 必须判失败
#    mtime 必须真的**早于**本次运行开始时刻，否则会被正确地判成「新鲜」；
#    本用例靠 touch -d 把它推到 2020 年（用「刚刚创建」的文件是测不出这个洞的：
#    同一秒内的 mtime 与 RUN_START 相等，本来就该算新鲜）。
printf '<html>stale report</html>' > "$RFILE"
touch -d '2020-01-01 00:00:00' "$RFILE"
run_report_case "同名旧文件未重写 → exit 1「报告未刷新」" none 1 "报告未刷新"
run_report_case "  └ 不应报成 OK"                    none 1 "" "OK 报告已生成"

# ③ 报告压根不存在（例如 REPORT_DIR 变了、或生成时抛异常）
rm -f "$RFILE"
run_report_case "报告不存在 → exit 1" none 1 "报告未生成"

# ④ main.py 自己以 1 退出：应保留它自己的失败语义
rm -f "$RFILE"
run_report_case "桩 exit 1 → exit 1" fail 1 "main.py exit=1"

# ⑤⑥ 报告已关闭时不该因为「没有报告/报告是旧的」而误报失败
rm -f "$RFILE"
ENABLED=0
run_report_case "报告已关闭（无文件）→ exit 0" none 0 "报告已关闭"
printf '<html>stale report</html>' > "$RFILE"
touch -d '2020-01-01 00:00:00' "$RFILE"
run_report_case "报告已关闭（有旧文件）→ exit 0" none 0 "报告已关闭"
unset ENABLED
rm -f "$RFILE"

echo "---- 结果：$PASS passed, $FAIL failed ----"
[ "$FAIL" = "0" ] || exit 1
