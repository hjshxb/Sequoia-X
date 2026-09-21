"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
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
