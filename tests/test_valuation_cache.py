"""估值本地缓存（`stock_valuation` 表）的测试。

覆盖三件事：
  1. 存取往返（含 NULL 值：亏损股 PE 为 None 也要能存回来）；
  2. `fetch_metrics` 同一天第二次调用**不再走网络**（这正是 2026-10-23 那次
     「连跑 5 次、其中一次 82/200 取数失败导致池子腰斩」的解药）；
  3. 缺失率过高要告警 —— 否则报告只会「莫名少了一半票」。
"""

import sqlite3

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data import universe_filter, valuation_cache
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import StockMetric, UniverseFilter


def _settings(tmp_path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        db_path=str(tmp_path / "v.db"),
        start_date="2025-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clear_process_cache(monkeypatch):
    """进程内缓存会掩盖「库缓存是否命中」，每个用例都从空开始。"""
    monkeypatch.setattr(universe_filter, "_METRIC_CACHE", {})


class _Engine:
    """只要能提供 db_path 与最新交易日的壳子，够 fetch_metrics 用。"""

    def __init__(self, db_path: str, latest: str = "2026-10-23") -> None:
        self.db_path = db_path
        self._latest = latest

    def get_market_latest_date(self) -> str:
        return self._latest


# ── 存取往返 ──


def test_round_trip_keeps_null_values(tmp_path) -> None:
    """亏损股 PE 为 None，必须能原样存回来 —— 否则会被当成「数据缺失」剔除。"""
    db = str(tmp_path / "v.db")
    valuation_cache.save(
        db,
        "2026-10-23",
        {
            "600000": {"pe_ttm": 6.2, "pb": 0.8, "turn": 1.5, "circ_market_cap": 3.1e11},
            "000002": {"pe_ttm": None, "pb": 0.5, "turn": 2.1, "circ_market_cap": 3.2e10},
        },
    )

    got = valuation_cache.load(db, "2026-10-23", ["600000", "000002"])

    assert got["600000"]["pe_ttm"] == 6.2
    assert got["000002"]["pe_ttm"] is None
    assert got["000002"]["pb"] == 0.5


def test_load_is_isolated_by_date(tmp_path) -> None:
    """按交易日分桶：昨天的估值不能被当成今天的。"""
    db = str(tmp_path / "v.db")
    valuation_cache.save(db, "2026-10-22", {"600000": {"pe_ttm": 6.0, "pb": 0.8,
                                                       "turn": 1.0, "circ_market_cap": 1e11}})

    assert valuation_cache.load(db, "2026-10-23", ["600000"]) == {}


def test_load_and_save_never_raise(tmp_path) -> None:
    """缓存是加速手段，不是正确性依赖：库坏了也要安静退化成「没缓存」。"""
    broken = str(tmp_path / "不存在的目录" / "v.db")

    assert valuation_cache.load(broken, "2026-10-23", ["600000"]) == {}
    assert valuation_cache.save(broken, "2026-10-23", {"600000": {"pe_ttm": 1.0}}) == 0


# ── fetch_metrics 走本地缓存 ──


def test_second_call_same_day_skips_network(tmp_path, monkeypatch) -> None:
    """同一天第二次 fetch_metrics 应命中库缓存，一次网络都不发。"""
    db = str(tmp_path / "v.db")
    DataEngine(_settings(tmp_path))  # 建出 stock_daily，保证库可用
    calls = {"n": 0}

    def fake_fetch(self, symbols):
        calls["n"] += 1
        return {
            s: StockMetric(symbol=s, circ_market_cap=1e10, pe_ttm=10.0, pb=1.0, turn=2.0)
            for s in symbols
        }

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", fake_fetch)
    engine = _Engine(db)

    first = UniverseFilter(settings=_settings(tmp_path), engine=engine)
    first.fetch_metrics(["600000", "000001"])

    monkeypatch.setattr(universe_filter, "_METRIC_CACHE", {})  # 清进程缓存，只留库缓存
    second = UniverseFilter(settings=_settings(tmp_path), engine=engine)
    got = second.fetch_metrics(["600000", "000001"])

    assert calls["n"] == 1, "第二次不该再发网络请求"
    assert set(got) == {"600000", "000001"}
    assert got["600000"].pe_ttm == 10.0


def test_no_engine_means_no_cache(tmp_path, monkeypatch) -> None:
    """没有注入 engine 就不碰库 —— 否则测试会互相污染（曾写进仓库根的 unused.db）。"""
    calls = {"n": 0}

    def fake_fetch(self, symbols):
        calls["n"] += 1
        return {s: StockMetric(symbol=s, circ_market_cap=1e10, pe_ttm=10.0, pb=1.0, turn=2.0)
                for s in symbols}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", fake_fetch)

    f = UniverseFilter(settings=_settings(tmp_path))  # 不传 engine
    f.fetch_metrics(["600000"])
    monkeypatch.setattr(universe_filter, "_METRIC_CACHE", {})
    f.fetch_metrics(["600000"])

    assert calls["n"] == 2, "没有 engine 时不该缓存，两次都走网络"


# ── 缺失率告警 ──


def test_high_missing_ratio_warns(tmp_path, monkeypatch, caplog) -> None:
    """批量取数失败率过高要喊出来：否则只会表现为「选股莫名少一半」。"""
    db = str(tmp_path / "v.db")
    DataEngine(_settings(tmp_path))

    def half_broken(self, symbols):
        # 只成功三分之一
        ok = symbols[: max(1, len(symbols) // 3)]
        return {s: StockMetric(symbol=s, circ_market_cap=1e10, pe_ttm=10.0, pb=1.0, turn=2.0)
                for s in ok}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", half_broken)

    with caplog.at_level("WARNING"):
        UniverseFilter(settings=_settings(tmp_path), engine=_Engine(db)).fetch_metrics(
            ["600000", "600001", "600002", "600003", "600004", "600005"]
        )

    assert "估值批量取数失败" in caplog.text


def test_low_missing_ratio_is_quiet(tmp_path, monkeypatch, caplog) -> None:
    """个别退市/停牌票缺失属于正常，不该刷告警。"""
    db = str(tmp_path / "v.db")
    DataEngine(_settings(tmp_path))

    def one_missing(self, symbols):
        return {
            s: StockMetric(symbol=s, circ_market_cap=1e10, pe_ttm=10.0, pb=1.0, turn=2.0)
            for s in symbols[:-1]
        }

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", one_missing)

    with caplog.at_level("WARNING"):
        UniverseFilter(settings=_settings(tmp_path), engine=_Engine(db)).fetch_metrics(
            ["600000", "600001", "600002", "600003", "600004", "600005", "600006", "600007"]
        )

    assert "估值批量取数失败" not in caplog.text


def test_table_is_created_on_demand(tmp_path) -> None:
    """首次运行表不存在也要能用（不允许「先手动跑迁移」这种前置条件）。"""
    db = str(tmp_path / "fresh.db")

    assert valuation_cache.load(db, "2026-10-23", ["600000"]) == {}
    with sqlite3.connect(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert valuation_cache.TABLE in names
