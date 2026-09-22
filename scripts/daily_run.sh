#!/usr/bin/env bash
# Sequoia-X 每日选股报告 —— 供 WorkBuddy 定时任务调用
#
# 为什么需要这个脚本：
#   main.py 的 sync_today_bulk() 判据是「本地最新日期 < 今天」，非交易日（周末/节假日）
#   会让全市场 5221 只股票都重新发一次请求却拿不到新数据，实测 16+ 分钟跑不完。
#   所以这里先用 baostock 交易日历判断「今天是不是交易日」，不是就直接退出。
set -u
export PATH="/usr/bin:/bin:/c/Windows/System32:/c/Windows:/usr/local/bin:${PATH:-}"

PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
STAMP=$(date +%Y-%m-%d)
cd "$PROJ" || { echo "ERROR: 项目目录不存在 $PROJ"; exit 1; }

echo "==== Sequoia-X 每日任务 $(date '+%F %T') ===="
echo "项目: $PROJ"

# ---- 1) 判断今天是否 A 股交易日（一次网络请求，秒级） ----
# 刻意**不**丢弃 stderr：baostock 的失败原因（login failed / 错误码）是排障的关键，
# 之前用 2>/dev/null 把它们全吞了，只留下一个无从下手的 "ERR"。
# 同时把错误码/错误信息随 stdout 一起带出来，方便直接读出「黑名单」还是「网络故障」。
IS_TRADE=$("$PY" - <<'PYEOF'
import datetime
try:
    import baostock as bs
except Exception as exc:
    print(f"ERR:import:{exc}"); raise SystemExit
lg = bs.login()
if lg.error_code != "0":
    print(f"ERR:login:{lg.error_code}:{lg.error_msg}"); raise SystemExit
d = datetime.date.today().strftime("%Y-%m-%d")
rs = bs.query_trade_dates(start_date=d, end_date=d)
flag = ""
while rs.error_code == "0" and rs.next():
    flag = rs.get_row_data()[1]
bs.logout()
if flag in ("0", "1"):
    print(flag)
else:
    print(f"ERR:calendar:{rs.error_code}:{rs.error_msg}")
PYEOF
)
echo "今日交易日标志: $IS_TRADE  (1=交易日, 0=休市, ERR:*=查询失败)"

case "$IS_TRADE" in
  0)
    echo "今天($STAMP)不是交易日，跳过选股，避免全市场空转。"
    exit 0
    ;;
  1)
    echo "今天($STAMP)是交易日，继续执行选股流程。"
    ;;
  ERR*)
    # 关键：数据源故障 != 休市。以前这里也是 exit 0，于是「baostock 挂了」
    # 被伪装成「今天休市，正常跳过」，定时任务一路绿 —— 故障会被静默很久。
    # 退出码约定：0=正常（含休市跳过）、1=main.py 失败、3=交易日历不可用。
    echo "ERROR: 交易日历查询失败（${IS_TRADE#ERR:}）"
    echo "       这是数据源故障，不是休市 —— 本次不执行选股，以退出码 3 上报。"
    echo "       排障：$PY scripts/probe_baostock_route.py --login"
    exit 3
    ;;
  *)
    # 兜底：Python 段若因异常没吐出任何内容，IS_TRADE 会是空串。
    # 旧代码在这种情况下会直接落下去跑 main.py（两个 if 都不匹配），
    # 等于「判断失败」被当成「交易日」。这里一并堵住。
    echo "ERROR: 交易日历返回了非法结果（'$IS_TRADE'），无法判断今天是否交易日。"
    echo "       本次不执行选股，以退出码 3 上报。"
    exit 3
    ;;
esac

# ---- 1.5) 演练开关：只做交易日判断就退出，不跑 main.py（供测试/预演） ----
if [ "${DAILY_RUN_DRY:-0}" = "1" ]; then
  echo "DAILY_RUN_DRY=1：已完成交易日判断，跳过 main.py（演练模式）"
  exit 0
fi

# ---- 2) 跑主流程：跑策略 + 生成 HTML 报告 + 飞书推送 ----
# 注意：增量同步的并发进程数由 SYNC_WORKERS 控制，**默认 1（单进程串行）**。
#   这是为了避开 baostock 的并发登录风控（8 进程曾换来 10001011 黑名单）。
#   代价是全市场增量同步会明显变慢；若确认链路稳定、想恢复速度，
#   可在此处追加 --workers 4（或改 .env 的 SYNC_WORKERS），建议先小步试。
echo "---- 运行 main.py ----"
"$PY" -u main.py
RC=$?
echo "main.py exit=$RC"

# ---- 3) 校验报告 ----
REPORT="reports/stock_report_${STAMP}.html"
if [ -f "$REPORT" ]; then
  echo "OK 报告已生成: $REPORT ($(wc -c < "$REPORT") bytes)"
else
  echo "WARN 未找到 $REPORT；reports/ 最新文件如下："
  ls -lt reports/ 2>/dev/null | head -6
fi
echo "==== 结束 $(date '+%F %T') ===="
exit $RC
