"""`scripts/backfill_dates.py` 的「该补哪些股票」口径。

判错有两个方向，都要防：
- **误判**（新股也算进待补）⇒ 每次续跑都为同一批股票白问一遍，计数永远降不到 0；
- **漏判**（真缺却跳过）⇒ 洞补不上，闸门天天拦截主流程。

所以这里既锁「上市日之前的目标日期不计入需求」，也锁「不能拿库内最晚日期当上界」。
"""

import sqlite3
import tempfile
from pathlib import Path

from scripts.backfill_dates import symbols_lacking_dates
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def _engine(tmp_dir: str) -> DataEngine:
    return DataEngine(
        Settings(
            db_path=str(Path(tmp_dir) / "t.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
    )


def _seed(engine: DataEngine, rows: list[tuple[str, str]]) -> None:
    """写入 `(symbol, date)` 两列，价格/量列给足非空值。"""
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,1,1,1,1,1,1)",
            rows,
        )
        conn.commit()


def test_symbol_listed_after_target_date_is_skipped() -> None:
    """上市晚于目标日的股票：本来就没有那天的数据，不该为它发请求。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = _engine(tmp_dir)
        _seed(
            engine,
            [
                ("600000", "2026-09-01"),
                ("600000", "2026-09-02"),
                ("600001", "2026-08-01"),
                ("600001", "2026-08-27"),
                ("600001", "2026-09-01"),
            ],
        )
        assert symbols_lacking_dates(engine, ["600000", "600001"], ["2026-08-28"]) == ["600001"]


def test_hole_spanning_listing_date_is_still_requested() -> None:
    """★ 按日期裁，不是整只排除：9/25 上市、要补 9/24~9/28 时，9/28 是真缺的。

    若实现写成「上市日晚于最早缺失日就整只跳过」，这条会红 —— 而那正是会漏补的写法。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = _engine(tmp_dir)
        _seed(engine, [("301697", "2026-09-25"), ("301697", "2026-09-26")])

        dates = ["2026-09-24", "2026-09-28"]
        assert symbols_lacking_dates(engine, ["301697"], dates) == ["301697"]

        _seed(engine, [("301697", "2026-09-28")])
        assert symbols_lacking_dates(engine, ["301697"], dates) == []


def test_symbol_missing_after_its_last_local_day_is_not_skipped() -> None:
    """★ 不能拿库内最晚日期当上界：数据停在 9/25 的股票，仍然要补 9/28。

    「最晚日期早于目标日」既可能是退市、也可能正是缺数据。把它当上界就会整只跳过 ⇒
    漏补。宁可多问几只退市股（返回空、写 0 行），也不能漏一只。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = _engine(tmp_dir)
        _seed(engine, [("600519", "2026-09-20"), ("600519", "2026-09-25")])

        assert symbols_lacking_dates(engine, ["600519"], ["2026-09-28"]) == ["600519"]


def test_symbol_listed_on_the_target_date_itself_is_requested() -> None:
    """边界：上市首日恰好是目标日之一 —— 上市日当天算「该有」，其余目标日照常要求。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = _engine(tmp_dir)
        _seed(engine, [("301999", "2026-09-24")])

        dates = ["2026-09-24", "2026-09-28"]
        assert symbols_lacking_dates(engine, ["301999"], dates) == ["301999"]


def test_symbol_holding_every_target_date_is_skipped() -> None:
    """已具备全部目标日期的股票直接跳过（续跑不会重复请求）。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = _engine(tmp_dir)
        dates = ["2026-09-24", "2026-09-28"]
        _seed(engine, [("600036", "2026-09-24"), ("600036", "2026-09-28")])
        _seed(engine, [("600001", "2026-09-24")])

        assert symbols_lacking_dates(engine, ["600036", "600001"], dates) == ["600001"]
