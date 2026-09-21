"""stock_meta 落盘缓存测试：本地优先、按 TTL 刷新、网络故障降级。

覆盖目标：把「代码 → 名称/行业」从「每次运行都 login + 全市场请求」改成
read-through 本地缓存，且数据源不可用时要能用本地旧数据顶上。
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sequoia_x.data import stock_meta as sm

_ROWS = [
    ["2026-09-14", "sh.600000", "浦发银行", "J66货币金融服务", "证监会行业分类"],
    ["2026-09-14", "sz.000001", "平安银行", "J66货币金融服务", "证监会行业分类"],
]

_TS_FMT = "%Y-%m-%d %H:%M:%S"


class _FakeRS:
    def __init__(self, rows, error_code="0", error_msg=""):
        self._rows = list(rows)
        self.error_code = error_code
        self.error_msg = error_msg
        self._i = -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


class _FakeBS:
    """baostock 替身，统计 login / query 次数（用来断言「没联网」）。"""

    def __init__(self, login_code="0", query_code="0", rows=None, calls=None):
        self.login_code = login_code
        self.query_code = query_code
        self.rows = _ROWS if rows is None else rows
        self.calls = calls if calls is not None else {}

    def login(self):
        self.calls["login"] = self.calls.get("login", 0) + 1
        return type("L", (), {"error_code": self.login_code, "error_msg": "fake"})()

    def logout(self):
        pass

    def query_stock_industry(self):
        self.calls["query"] = self.calls.get("query", 0) + 1
        return _FakeRS(self.rows, self.query_code, "查询失败")


def _seed_db(db_path: str, rows: dict[str, tuple], age_days: float) -> None:
    """往 stock_meta 表塞入指定「上次刷新时间」的数据。"""
    ts = (datetime.now() - timedelta(days=age_days)).strftime(_TS_FMT)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_meta ("
            "symbol TEXT PRIMARY KEY, name TEXT, industry TEXT, updated_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO stock_meta "
            "(symbol, name, industry, updated_at) VALUES (?,?,?,?)",
            [(s, n, i, ts) for s, (n, i) in rows.items()],
        )
        conn.commit()


def _read_db(db_path: str) -> dict[str, dict]:
    with sqlite3.connect(db_path) as conn:
        return {
            r[0]: {"name": r[1], "industry": r[2], "updated_at": r[3]}
            for r in conn.execute("SELECT symbol, name, industry, updated_at FROM stock_meta")
        }


@pytest.fixture
def env(monkeypatch):
    """临时库 + 假 baostock，并在测试后复位模块状态。"""
    tmp = tempfile.mkdtemp()
    db_path = str(Path(tmp) / "meta.db")
    calls: dict[str, int] = {}
    fake = _FakeBS(calls=calls)
    monkeypatch.setitem(sys.modules, "baostock", fake)

    sm.reset_cache()
    sm.configure_cache(db_path, ttl_days=7)
    try:
        yield db_path, calls, fake
    finally:
        sm.reset_cache()
        sm.configure_cache(None)


def test_first_load_fetches_and_persists(env):
    """库是空的 ⇒ 走一次 baostock，并把结果写进本地库。"""
    db_path, calls, _ = env
    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls == {"login": 1, "query": 1}
    rows = _read_db(db_path)
    assert rows["600000"]["name"] == "浦发银行"
    assert rows["600000"]["industry"] == "货币金融服务"  # 已清洗分类前缀
    assert rows["000001"]["name"] == "平安银行"


def test_fresh_cache_avoids_network(env):
    """本地数据在 TTL 内 ⇒ 完全联网为 0（这是本次优化的核心收益）。"""
    db_path, calls, _ = env
    _seed_db(db_path, {"600000": ("浦发银行", "货币金融服务")}, age_days=1)

    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls == {}, "本地数据新鲜时不应登录、不应请求"


def test_stale_cache_triggers_refresh(env):
    """本地数据超过 TTL ⇒ 联网刷新并覆盖旧值。"""
    db_path, calls, _ = env
    _seed_db(db_path, {"600000": ("改名前的旧名", None)}, age_days=30)

    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls == {"login": 1, "query": 1}
    assert _read_db(db_path)["600000"]["name"] == "浦发银行"


def test_stale_cache_falls_back_to_local_on_login_failure(env):
    """登录被拒（如黑名单）时要用本地旧数据顶上，而不是返回空。

    旧实现会返回空字典，报告与飞书里的名称全部退化成「—」。
    """
    db_path, calls, fake = env
    _seed_db(db_path, {"600000": ("浦发银行", "货币金融服务")}, age_days=30)
    fake.login_code = "10001011"

    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls.get("query", 0) == 0, "登录都没成功，不该再去发查询"


def test_stale_cache_falls_back_to_local_on_query_failure(env):
    """登录成功但查询报错 ⇒ 同样降级用本地旧数据。"""
    db_path, calls, fake = env
    _seed_db(db_path, {"600000": ("浦发银行", "货币金融服务")}, age_days=30)
    fake.query_code = "10002007"

    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls["query"] == 1


def test_empty_cache_and_login_failure_returns_empty(env):
    """本地也空 + 联网失败 ⇒ 只能返回空（保持原有兜底行为，不抛异常）。"""
    _, calls, fake = env
    fake.login_code = "10001011"

    assert sm.load_stock_meta() == {}
    assert calls.get("query", 0) == 0


def test_unconfigured_cache_does_not_touch_disk(env):
    """未调用 configure_cache ⇒ 退回「内存 + 网络」，一个文件都不该建。"""
    db_path, calls, _ = env
    sm.configure_cache(None)
    sm.reset_cache()

    meta = sm.load_stock_meta()

    assert meta["600000"].name == "浦发银行"
    assert calls["login"] == 1
    assert not Path(db_path).exists()


def test_ttl_zero_always_refreshes(env):
    """TTL=0 ⇒ 每次运行都刷新（等价于关闭缓存收益）。"""
    db_path, calls, _ = env
    sm.configure_cache(db_path, ttl_days=0)
    _seed_db(db_path, {"600000": ("改名前的旧名", None)}, age_days=0)

    sm.load_stock_meta()

    assert calls == {"login": 1, "query": 1}


def test_second_call_hits_memory_cache(env):
    """一次运行内多处调用（过滤 / 报告 / 推送）只读一次、只连一次网。"""
    _, calls, _ = env

    first = sm.load_stock_meta()
    second = sm.load_stock_meta()

    assert first is second
    assert calls == {"login": 1, "query": 1}


def test_cache_hit_reuses_db_rows_for_all_symbols(env):
    """本地命中时行业也要一起带出来（过滤维度依赖它，不能只剩名字）。"""
    db_path, _, _ = env
    _seed_db(
        db_path,
        {
            "600000": ("浦发银行", "货币金融服务"),
            "300750": ("宁德时代", "电气机械和器材制造业"),
        },
        age_days=1,
    )

    meta = sm.load_stock_meta()

    assert meta["300750"].industry == "电气机械和器材制造业"
    assert set(meta) == {"600000", "300750"}
