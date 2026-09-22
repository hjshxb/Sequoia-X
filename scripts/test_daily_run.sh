#!/usr/bin/env bash
# daily_run.sh 交易日判断分支的回归测试。
#
# 为什么需要：这个脚本最危险的失败模式是「静默」—— 数据源挂了却被报成
# 「今天休市，正常跳过」，退出码还是 0，定时任务一路绿。
# 所以这里专门覆盖 4 个分支，全部用假 baostock（PYTHONPATH 注入），不联网。
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

echo "---- 结果：$PASS passed, $FAIL failed ----"
[ "$FAIL" = "0" ] || exit 1
