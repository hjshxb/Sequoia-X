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


def _seed_stale_rows(engine: DataEngine, symbols: list[str], when: str = "2000-01-01") -> None:
    """给每只股票塞一条 `when`（默认很久以前）的记录，使它们被判定为需要更新。"""
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(s, when, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for s in symbols],
        )
        conn.commit()


def _fake_rows(symbols: list[str], when: str = "2026-09-24") -> list:
    """构造 baostock 形态的行：`[symbol, date, open, high, low, close, volume, amount]`。

    用独立函数而不是就地写 `[[s, ...]]`，是因为 `_bs_fetch_batch` 的替身散落在
    多个用例里 —— 改字段顺序时只改这里，不必逐个用例对齐（数量：`volume > 0`
    与 `close` 非空都必须满足，否则行会在落库前被过滤掉，占比判定随之失真）。
    """
    return [[s, when, "1", "2", "3", "4", "1000", "2000"] for s in symbols]


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

        def fake_batch(tasks: list) -> tuple[list, int]:
            calls.append(list(tasks))
            return _fake_rows([t[0] for t in tasks]), 0

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("单进程模式不应创建 multiprocessing.Pool")

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", fake_batch)
        monkeypatch.setattr(multiprocessing, "Pool", boom)

        stats = engine.sync_today_bulk()
        assert stats.updated == 3
        assert stats.missing == 0
        assert len(calls) == 1, "单进程应只调用一次 worker"
        assert {t[0] for t in calls[0]} == {"600000", "000001", "600519"}


def test_sync_today_bulk_single_process_covers_all_tasks(monkeypatch) -> None:
    """单进程下所有待更新股票都应被覆盖，不能因为不分片而漏掉。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        symbols = [f"60000{i}" for i in range(7)]
        _seed_stale_rows(engine, symbols)

        seen: list[str] = []

        def fake_batch(tasks: list) -> tuple[list, int]:
            seen.extend(t[0] for t in tasks)
            return _fake_rows([t[0] for t in tasks]), 0

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
                return [(_fake_rows([t[0] for t in c]), 0) for c in chunks]

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
                return [(_fake_rows([t[0] for t in c]), 0) for c in chunks]

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
    """正常路径：登录成功 + 查询成功 ⇒ 返回带 symbol 前缀的行，且 failed 为 0。"""
    fake = _FakeBaoStock(rows=[["2026-09-21", "1", "2", "3", "4", "5", "6"]])
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows, failed = engine_module._bs_fetch_batch(_TASKS[:1])
    assert rows == [["600000", "2026-09-21", "1", "2", "3", "4", "5", "6"]]
    assert failed == 0
    assert fake.logout_called is True


def test_fetch_batch_tolerates_partial_query_failure(monkeypatch) -> None:
    """部分查询失败属正常（退市/停牌等），不应整批判死 —— 但失败数要如实回传。

    `failed` 是判定「本次数据完不完整」的输入（全市场维度，见
    `sync_today_bulk` 的占比阈值），就地丢一条 warning 就没人知道缺了多少。
    """

    class _Flaky(_FakeBaoStock):
        def query_history_k_data_plus(self, code, *_a, **_kw):
            self.query_calls.append(code)
            if code.endswith("0"):  # 只让第一只失败
                return _FakeResultSet([], "10002007", "查询失败")
            return _FakeResultSet([["2026-09-21", "1", "2", "3", "4", "5", "6"]])

    fake = _Flaky()
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows, failed = engine_module._bs_fetch_batch(_TASKS)
    assert len(rows) == 2  # 3 只里 2 只成功
    assert failed == 1


def test_sync_today_bulk_propagates_unavailable_and_writes_nothing(monkeypatch) -> None:
    """数据源不可用时 sync_today_bulk 必须抛出，且不得写库、不得静默返回 0。

    静默返回 0 会让 main.py 拿旧数据照跑策略并推飞书（历史事故即如此）。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        _seed_stale_rows(engine, ["600000", "000001"])

        def boom(_tasks: list) -> tuple[list, int]:
            raise engine_module.BaostockUnavailable("baostock 登录失败: 10001011 黑名单用户")

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", boom)

        before = _count_rows(engine)
        with pytest.raises(engine_module.BaostockUnavailable):
            engine.sync_today_bulk()
        assert _count_rows(engine) == before, "失败时不应改动数据库"


def test_sync_today_bulk_raises_when_requested_but_no_rows(monkeypatch) -> None:
    """「发了 N 只请求却一行都没拿到」是失败，不是「今天没有信号」。

    这是最初那个 bug 的正身：旧实现只 log 一句 `return 0`，主流程便拿上一
    交易日的数据跑策略，还推一张日期写着今天的卡片。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        _seed_stale_rows(engine, ["600000", "000001"])

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", lambda _tasks: ([], 0))

        before = _count_rows(engine)
        with pytest.raises(engine_module.BaostockUnavailable):
            engine.sync_today_bulk()
        assert _count_rows(engine) == before, "数据未就绪时不应改动数据库"


def test_sync_today_bulk_raises_when_incomplete_beyond_threshold(monkeypatch) -> None:
    """拿到了一部分、但未更新占比超阈值 ⇒ SyncIncomplete，且不得写库。

    典型场景：上游只发布了部分行情，或个别节点异常。此时照常推送，榜单就是
    「一半新数据 + 一半旧数据」的混合体 —— 比不出结果更误导。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1, sync_max_fail_ratio=0.05)
        symbols = [f"60000{i}" for i in range(4)]
        _seed_stale_rows(engine, symbols)

        # 4 只里只回 3 只 ⇒ 缺 25%，远超 5% 上限
        monkeypatch.setattr(
            engine_module,
            "_bs_fetch_batch",
            lambda tasks: (_fake_rows([t[0] for t in tasks[:3]]), 0),
        )

        before = _count_rows(engine)
        with pytest.raises(engine_module.SyncIncomplete) as exc_info:
            engine.sync_today_bulk()
        assert "25.0%" in str(exc_info.value)
        assert _count_rows(engine) == before, "数据不完整时不应改动数据库"


def test_sync_today_bulk_tolerates_missing_within_threshold(monkeypatch) -> None:
    """正常停牌股造成的少量缺失必须放行 —— 否则每个交易日都会中止。

    边界写成「**超过**上限才中止」：占比恰好等于阈值算通过，与配置项
    `sync_max_fail_ratio` 的语义（上限）一致。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1, sync_max_fail_ratio=0.05)
        symbols = [f"60000{i}" for i in range(20)]
        _seed_stale_rows(engine, symbols)

        # 20 只里回 19 只 ⇒ 未更新 5%，正好在阈值上
        monkeypatch.setattr(
            engine_module,
            "_bs_fetch_batch",
            lambda tasks: (_fake_rows([t[0] for t in tasks[:19]]), 0),
        )

        stats = engine.sync_today_bulk()
        assert stats.requested == 20
        assert stats.updated == 19
        assert stats.missing == 1
        assert stats.missing_ratio == pytest.approx(0.05)
        assert stats.rows == 19


def test_sync_today_bulk_raises_on_empty_db_without_querying(monkeypatch) -> None:
    """库为空（没做过 --backfill）同样是「数据没就绪」：中止，且一只都不许查。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)

        calls: list = []
        monkeypatch.setattr(
            engine_module, "_bs_fetch_batch", lambda tasks: (calls.append(tasks), ([], 0))[1]
        )

        with pytest.raises(engine_module.SyncIncomplete):
            engine.sync_today_bulk()
        assert calls == [], "库为空时不应向数据源发请求"


def test_sync_today_bulk_reports_no_update_needed_without_network(monkeypatch) -> None:
    """所有股票都已是今天的行 ⇒ 零请求、零写入，`requested == 0` 才是「正常无更新」。

    与上面「需要更新却一行没拿到」严格区分：那个是故障，这个是正常态
    （同一天重复跑、或手工触发）。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir, sync_workers=1)
        today = date.today().strftime("%Y-%m-%d")
        _seed_stale_rows(engine, ["600000", "000001"], when=today)

        def boom(_tasks: list) -> tuple[list, int]:
            raise AssertionError("无需更新时不应向数据源发请求")

        monkeypatch.setattr(engine_module, "_bs_fetch_batch", boom)

        stats = engine.sync_today_bulk()
        assert stats.requested == 0
        assert stats.updated == 0
        assert stats.rows == 0
        assert stats.missing_ratio == 0.0


def test_sync_stats_missing_and_ratio() -> None:
    """`missing` / `missing_ratio` 是阈值判定的输入，边界必须准且不能除零。"""
    partial = engine_module.SyncStats(requested=20, updated=19, failed=0, rows=19)
    assert partial.missing == 1
    assert partial.missing_ratio == pytest.approx(0.05)
    # requested == 0（本次无需更新）时占比定义为 0，不能 ZeroDivisionError
    idle = engine_module.SyncStats(requested=0, updated=0, failed=0, rows=0)
    assert idle.missing == 0
    assert idle.missing_ratio == 0.0


# ── 最近两个交易日的行情（供「今日跌幅」维度使用）──


def _seed_daily(engine: DataEngine, rows: list[tuple[str, str, float]]) -> None:
    """rows: [(symbol, date, close), ...]，其余字段用 close 填充占位。"""
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(s, d, c, c, c, c, 1.0, 1.0) for s, d, c in rows],
        )
        conn.commit()


def test_get_market_latest_date_takes_max_across_all_symbols() -> None:
    """基准日必须取全市场 MAX(date)，而不是某只股票自己的 MAX(date)。

    停牌股的最后一行会明显早于全市场，若按逐股取值，就会把停牌前的
    旧涨跌幅当成「今日跌幅」。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_daily(
            engine,
            [
                ("600000", "2026-09-21", 10.0),
                ("600000", "2026-09-22", 10.5),
                ("000001", "2026-09-18", 8.0),  # 09-18 之后一直停牌
            ],
        )
        assert engine.get_market_latest_date() == "2026-09-22"


def test_get_market_latest_date_on_empty_db_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        assert engine.get_market_latest_date() is None


def test_get_recent_rows_returns_rows_in_chronological_order() -> None:
    """升序返回：[-1] 是最新一行，[-2] 即昨收。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_daily(
            engine,
            [
                ("600000", "2026-09-18", 10.0),
                ("600000", "2026-09-21", 10.2),
                ("600000", "2026-09-22", 9.9),
            ],
        )
        rows = engine.get_recent_rows(["600000"], rows=2)
        assert [r["date"] for r in rows["600000"]] == ["2026-09-21", "2026-09-22"]
        assert rows["600000"][-1]["close"] == 9.9
        assert rows["600000"][-2]["close"] == 10.2


def test_get_recent_rows_handles_single_row_and_unknown_symbol() -> None:
    """次新股只有一行；完全不存在的代码不进入结果。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_daily(engine, [("600000", "2026-09-22", 10.0)])
        rows = engine.get_recent_rows(["600000", "000002"], rows=2)
        assert len(rows["600000"]) == 1
        assert "000002" not in rows


def test_get_recent_rows_empty_input_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        assert engine.get_recent_rows([], rows=2) == {}
