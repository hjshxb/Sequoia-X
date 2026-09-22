"""baostock 连通性探针。

用法（WSL）：
    wsl.exe -d Ubuntu --cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X \\
        -- /home/hxb/miniconda3/envs/sequoia-x/bin/python scripts/probe_baostock.py

排障时的第一件事：先跑它，确认 baostock 是否可用，再决定要不要跑全市场同步。
仅做一次 login + 一次 query，不会触发限流。
"""
import os
import time

import baostock as bs

# 可选延迟：黑名单刚解除时用 PROBE_SLEEP=60 观察「恢复得稳不稳」。
# 默认 0 —— 排障第一件事就是跑这个探针，不该先无谓地等一分钟。
time.sleep(float(os.environ.get("PROBE_SLEEP", "0")))
for i in range(2):
    t0 = time.time()
    lg = bs.login()
    print(
        f"LOGIN#{i + 1}:", lg.error_code, lg.error_msg,
        f"elapsed={time.time() - t0:.2f}s", flush=True,
    )
    if lg.error_code == "0":
        rs = bs.query_history_k_data_plus(
            "sh.600000", "date,code,close",
            start_date="2026-09-18", end_date="2026-09-21",
            frequency="d", adjustflag="2",
        )
        print("QUERY:", rs.error_code, rs.error_msg, flush=True)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        print("ROWS:", len(rows), rows, flush=True)
        bs.logout()
        break
    time.sleep(20)
