#!/usr/bin/env bash
# 验证 stock_meta 落盘缓存：
#   ① 真实库上的建表/读表往返（不联网）
#   ② 真库副本上「本地有过期数据 + 联网失败 ⇒ 降级用旧数据」（baostock 打桩，不联网）
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python

echo "===== ① 真实库：建表 / 读表（不联网）====="
"$PY" - <<'PYEOF'
import sqlite3

from sequoia_x.core.config import get_settings
from sequoia_x.data import stock_meta as sm

s = get_settings()
print("行情库        :", s.db_path)
print("TTL(天)       :", s.stock_meta_ttl_days)
sm.configure_cache(s.db_path, s.stock_meta_ttl_days)

# 只读缓存，不调用 load_stock_meta ⇒ 不触发任何 baostock 请求
meta, last = sm._read_cache(s.db_path)
print("当前行数      :", len(meta), "| 上次刷新:", last or "（从未刷新过）")
with sqlite3.connect(s.db_path) as conn:
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (sm.CACHE_TABLE,)
    ).fetchone()
print("建表语句      :", " ".join(ddl[0].split()) if ddl else "（缺失）")
PYEOF

echo
echo "===== ② 真库副本：过期本地数据 + 联网失败 ⇒ 降级（baostock 打桩）====="
"$PY" - <<'PYEOF'
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sequoia_x.core.config import get_settings
from sequoia_x.data import stock_meta as sm


class _DeadBS:
    """登录被拒（模拟黑名单）。若被查到 query 次数 > 0 说明没有及时中止。"""

    def __init__(self):
        self.calls = {}

    def login(self):
        self.calls["login"] = self.calls.get("login", 0) + 1
        return type("L", (), {"error_code": "10001011", "error_msg": "黑名单用户"})()

    def logout(self):
        pass

    def query_stock_industry(self):
        self.calls["query"] = self.calls.get("query", 0) + 1
        raise AssertionError("登录失败后不应再发查询")


dead = _DeadBS()
sys.modules["baostock"] = dead

tmp = Path(tempfile.mkdtemp()) / "copy.db"
shutil.copy(get_settings().db_path, tmp)          # 用真库副本，绝不污染真库
print("副本          :", tmp)

stale = (datetime.now() - timedelta(days=999)).strftime(sm._TS_FMT)
with sqlite3.connect(tmp) as conn:
    conn.executemany(
        f"INSERT OR REPLACE INTO {sm.CACHE_TABLE} (symbol,name,industry,updated_at)"
        " VALUES (?,?,?,?)",
        [("600000", "浦发银行", "货币金融服务", stale),
         ("300750", "宁德时代", "电气机械和器材制造业", stale)],
    )
    conn.commit()

sm.reset_cache()
sm.configure_cache(str(tmp), ttl_days=7)

meta = sm.load_stock_meta()
print("降级返回条数  :", len(meta))
print("600000        :", meta.get("600000"))
print("300750        :", meta.get("300750"))
print("baostock 调用 :", dead.calls)

assert meta["600000"].name == "浦发银行", "联网失败时应降级用本地旧数据，而不是返回空"
assert meta["300750"].industry == "电气机械和器材制造业"
assert dead.calls == {"login": 1}, "只应尝试一次登录，且失败后不再发查询"
print()
print("✓ 降级路径成立：本地旧数据顶上，未因登录失败而丢名称")
PYEOF
