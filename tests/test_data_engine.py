"""数据引擎属性测试。"""

import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

import sequoia_x.data.engine as engine_module
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str, **overrides: object) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例（overrides 覆盖任意配置项）。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
        **overrides,  # type: ignore[arg-type]
    )
    engine = DataEngine(settings)
    return engine, settings


def _seed_stale_rows(engine: DataEngine, symbols: list[str]) -> None:
    """给每只股票塞一条很久以前的记录，使 sync_today_bulk 认为它们需要更新。"""
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(s, "2000-01-01", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for s in symbols],
        )
        conn.commit()


def _count_rows(engine: DataEngine) -> int:
    with sqlite3.connect(engine.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


# ── 同步并发度可配置（默认单进程）──


def test_engine_reads_sync_workers_from_settings() -> None:
    """DataEngine 应把配置里的并发进程数带进实例，供 sync_today_bulk 使用。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=3)
        assert engine.sync_workers == 3


def test_engine_sync_workers_defaults_to_one() -> None:
    """未配置时默认单进程。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        assert engine.sync_workers == 1


def test_sync_today_bulk_single_process_does_not_fork(monkeypatch) -> None:
    """默认单进程：必须不走 multiprocessing.Pool，而是一次性串行拉取全部任务。

    这是本次线上故障（8 进程并发重试风暴 → baostock 黑名单）的核心回归点。
    """
    import multiprocessing

    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        _seed_stale_rows(engine, ["600000", "000001", "600519"])

        calls: list[list] = []

        def fake_batch(tasks: list) -> list:
            calls.append(list(tasks))
            return []

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("单进程模式不应创建 multiprocessing.Pool")

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", fake_batch)
        monkeypatch.setattr(multiprocessing, "Pool", boom)

        assert engine.sync_today_bulk() == 0
        assert len(calls) == 1, "单进程应只调用一次 worker"
        assert {t[0] for t in calls[0]} == {"600000", "000001", "600519"}


def test_sync_today_bulk_single_process_covers_all_tasks(monkeypatch) -> None:
    """单进程下所有待更新股票都应被覆盖，不能因为不分片而漏掉。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        symbols = [f"60000{i}" for i in range(7)]
        _seed_stale_rows(engine, symbols)

        seen: list[str] = []

        def fake_batch(tasks: list) -> list:
            seen.extend(t[0] for t in tasks)
            return []

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", fake_batch)
        engine.sync_today_bulk()
        assert sorted(seen) == sorted(symbols)


def test_sync_today_bulk_multi_process_splits_into_requested_workers(monkeypatch) -> None:
    """显式配置 N 进程时，应按 N 个分片交给 Pool，且分片覆盖全部任务。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=3)
        symbols = [f"60000{i}" for i in range(7)]
        _seed_stale_rows(engine, symbols)

        observed: dict[str, object] = {}

        class FakePool:
            def __init__(self, n: int) -> None:
                observed["n_workers"] = n

            def __enter__(self) -> "FakePool":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def map(self, _fn: object, chunks: list) -> list:
                observed["chunks"] = [list(c) for c in chunks]
                return [[] for _ in chunks]

        monkeypatch.setattr("multiprocessing.Pool", FakePool)
        engine.sync_today_bulk()

        assert observed["n_workers"] == 3
        chunks = observed["chunks"]
        assert len(chunks) == 3
        assert sorted(t[0] for c in chunks for t in c) == sorted(symbols)


def test_sync_today_bulk_workers_capped_by_task_count(monkeypatch) -> None:
    """配置的进程数超过待更新股票数时应收缩，避免空分片。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=8)
        _seed_stale_rows(engine, ["600000", "000001"])

        observed: dict[str, object] = {}

        class FakePool:
            def __init__(self, n: int) -> None:
                observed["n_workers"] = n

            def __enter__(self) -> "FakePool":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def map(self, _fn: object, chunks: list) -> list:
                observed["chunks"] = [list(c) for c in chunks]
                return [[] for _ in chunks]

        monkeypatch.setattr("multiprocessing.Pool", FakePool)
        engine.sync_today_bulk()
        assert observed["n_workers"] == 2


# ── 数据源不可用时必须立即失败，不能空发请求 ──


class _FakeResultSet:
    """最小 baostock 结果集替身。"""

    def __init__(self, rows: list, error_code: str = "0", error_msg: str = "") -> None:
        self._rows = list(rows)
        self.error_code = error_code
        self.error_msg = error_msg
        self._i = 0

    def next(self) -> bool:
        if self._i < len(self._rows):
            self._i += 1
            return True
        return False

    def get_row_data(self) -> list:
        return self._rows[self._i - 1]


class _FakeLogin:
    def __init__(self, code: str, msg: str) -> None:
        self.error_code = code
        self.error_msg = msg


class _FakeBaoStock:
    """baostock 模块替身，记录 query 调用次数（用于断言「没有空发请求」）。"""

    def __init__(
        self,
        login_code: str = "0",
        login_msg: str = "success",
        query_code: str = "0",
        rows: list | None = None,
    ) -> None:
        self.login_code = login_code
        self.login_msg = login_msg
        self.query_code = query_code
        self.rows = rows or []
        self.query_calls: list[str] = []
        self.logout_called = False

    def login(self) -> _FakeLogin:
        return _FakeLogin(self.login_code, self.login_msg)

    def logout(self) -> None:
        self.logout_called = True

    def query_history_k_data_plus(self, code: str, *_a: object, **_kw: object) -> _FakeResultSet:
        self.query_calls.append(code)
        return _FakeResultSet(self.rows, self.query_code, "查询失败")


_TASKS = [(f"60000{i}", f"sh.60000{i}", "2026-09-19", "2026-09-21") for i in range(3)]


def test_fetch_batch_raises_on_login_failure_without_querying(monkeypatch) -> None:
    """登录被拒（如 10001011 黑名单）时必须立即中止 —— 一只都不许查。

    这是本次事故的放大器：旧代码丢弃 login 返回值，照样给全市场 5221 只
    各发一次注定失败的请求，白白加重风控。
    """
    fake = _FakeBaoStock(login_code="10001011", login_msg="黑名单用户，请与管理员联系")
    monkeypatch.setitem(sys.modules, "baostock", fake)

    with pytest.raises(engine_module.BaostockUnavailable):
        engine_module._bs_fetch_batch(_TASKS)

    assert fake.query_calls == [], "登录失败后不应再逐只发请求"
    assert fake.logout_called is False


def test_fetch_batch_reports_login_error_code(monkeypatch) -> None:
    """异常信息里要带上 baostock 的错误码，方便直接对着报错码排障。"""
    fake = _FakeBaoStock(login_code="10001011", login_msg="黑名单用户，请与管理员联系")
    monkeypatch.setitem(sys.modules, "baostock", fake)

    with pytest.raises(engine_module.BaostockUnavailable) as exc_info:
        engine_module._bs_fetch_batch(_TASKS)
    assert "10001011" in str(exc_info.value)


def test_fetch_batch_raises_when_every_query_fails(monkeypatch) -> None:
    """登录成功但整批查询全错 ⇒ 数据源故障，不能伪装成「无新数据」。"""
    fake = _FakeBaoStock(query_code="10002007", rows=[])
    monkeypatch.setitem(sys.modules, "baostock", fake)

    with pytest.raises(engine_module.BaostockUnavailable):
        engine_module._bs_fetch_batch(_TASKS)


def test_fetch_batch_returns_rows_on_success(monkeypatch) -> None:
    """正常路径：登录成功 + 查询成功 ⇒ 返回带 symbol 前缀的行。"""
    fake = _FakeBaoStock(rows=[["2026-09-21", "1", "2", "3", "4", "5", "6"]])
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows = engine_module._bs_fetch_batch(_TASKS[:1])
    assert rows == [["600000", "2026-09-21", "1", "2", "3", "4", "5", "6"]]
    assert fake.logout_called is True


def test_fetch_batch_tolerates_partial_query_failure(monkeypatch) -> None:
    """部分查询失败属正常（退市/停牌等），不应整批判死。"""

    class _Flaky(_FakeBaoStock):
        def query_history_k_data_plus(self, code, *_a, **_kw):
            self.query_calls.append(code)
            if code.endswith("0"):  # 只让第一只失败
                return _FakeResultSet([], "10002007", "查询失败")
            return _FakeResultSet([["2026-09-21", "1", "2", "3", "4", "5", "6"]])

    fake = _Flaky()
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows = engine_module._bs_fetch_batch(_TASKS)
    assert len(rows) == 2  # 3 只里 2 只成功


def test_sync_today_bulk_propagates_unavailable_and_writes_nothing(monkeypatch) -> None:
    """数据源不可用时 sync_today_bulk 必须抛出，且不得写库、不得静默返回 0。

    静默返回 0 会让 main.py 拿旧数据照跑策略并推飞书（历史事故即如此）。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        _seed_stale_rows(engine, ["600000", "000001"])

        def boom(_tasks: list) -> list:
            raise engine_module.BaostockUnavailable("baostock 登录失败: 10001011 黑名单用户")

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", boom)

        before = _count_rows(engine)
        with pytest.raises(engine_module.BaostockUnavailable):
            engine.sync_today_bulk()
        assert _count_rows(engine) == before, "失败时不应改动数据库"


def test_sync_today_bulk_returns_zero_only_on_genuine_no_new_data(monkeypatch) -> None:
    """查询都成功但没有新行（非交易日）才是「无新数据」，此时返回 0。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        _seed_stale_rows(engine, ["600000"])

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", lambda _tasks: [])
        assert engine.sync_today_bulk() == 0
