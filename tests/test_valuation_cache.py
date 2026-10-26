"""估值本地缓存（`stock_valuation` 表）的测试。

覆盖三件事：
  1. 存取往返（含 NULL 值：亏损股 PE 为 None 也要能存回来）；
  2. `fetch_metrics` 同一天第二次调用**不再走网络**（这正是 2026-10-23 那次
     「连跑 5 次、其中一次 82/200 取数失败导致池子腰斩」的解药）；
  3. 缺失率过高要告警 —— 否则报告只会「莫名少了一半票」。
"""

import sqlite3
from datetime import date, timedelta

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


# `_env_file=None` 只关 .env 文件，管不住 `os.environ` —— 而 `main.py` 在导入时
# 调了 `load_dotenv()`，所以只要 `test_main` 先跑过，本机 .env 的阈值就被灌进环境变量，
# 后面这些用例的 UniverseFilter 会凭空多出「成交额/换手率/均线/行业」等维度
# （表现为「单跑绿、全量红」，2026-10-23 已踩过一次）。用例要自己把它们清掉。
_FILTER_ENV_VARS = (
    "MIN_MARKET_CAP",
    "MAX_MARKET_CAP",
    "MIN_PE",
    "MAX_PE",
    "MIN_PB",
    "MAX_PB",
    "MIN_TURNOVER",
    "MIN_TURN",
    "MAX_TURN",
    "MIN_TODAY_DROP",
    "MAX_TODAY_DROP",
    "MIN_MA_DEVIATION",
    "INCLUDE_INDUSTRIES",
    "EXCLUDE_INDUSTRIES",
    "MIN_TOP10_FREE_HOLDING",
    "MAX_TOP10_FREE_HOLDING",
)


@pytest.fixture(autouse=True)
def _isolate_filter_env(monkeypatch):
    """清掉可能被其他测试污染的精筛阈值，保证断言只反映本用例配置的维度。"""
    for name in _FILTER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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


# ── 网络整批失败时的兜底与「如实播报」 ──


def test_stale_cache_fills_gap_when_network_fails(tmp_path, monkeypatch, caplog) -> None:
    """网络全失败时，用最近 7 天内的缓存顶上 —— 否则市值门槛会静默失效。

    这是 2026-10-26 的事故形态：baostock 登录失败 174/174 ⇒ 估值维度被整体跳过
    ⇒ 市值 >=100亿 不生效 ⇒ 鹿山新材（38 亿）进榜，而页首仍写着那条门槛。
    """
    db = str(tmp_path / "v.db")
    DataEngine(_settings(tmp_path))
    previous = (date.today() - timedelta(days=2)).isoformat()
    valuation_cache.save(
        db,
        previous,
        {"603051": {"pe_ttm": 30.0, "pb": 2.0, "turn": 3.0, "circ_market_cap": 3.82e9}},
    )
    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", lambda self, symbols: {})

    with caplog.at_level("WARNING"):
        got = UniverseFilter(settings=_settings(tmp_path), engine=_Engine(db)).fetch_metrics(
            ["603051"]
        )

    assert "603051" in got, "网络失败时必须用缓存兜底"
    assert got["603051"].circ_market_cap == 3.82e9
    assert "改用最近缓存" in caplog.text


def test_stale_cache_older_than_window_is_ignored(tmp_path, monkeypatch) -> None:
    """超过 7 天的缓存不能用：可能跨过除权/定增，判市值反而误导。"""
    db = str(tmp_path / "v.db")
    DataEngine(_settings(tmp_path))
    old = (date.today() - timedelta(days=30)).isoformat()
    valuation_cache.save(
        db, old, {"603051": {"pe_ttm": 30.0, "pb": 2.0, "turn": 3.0, "circ_market_cap": 3.82e9}}
    )
    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", lambda self, symbols: {})

    got = UniverseFilter(settings=_settings(tmp_path), engine=_Engine(db)).fetch_metrics(["603051"])

    assert got == {}


class _FakeRs:
    """假的 baostock 结果集：只喂一行数据。"""

    error_code = "0"

    def __init__(self, row: list[str]) -> None:
        self._row = row
        self._done = False

    def next(self) -> bool:
        if self._done:
            return False
        self._done = True
        return True

    def get_row_data(self) -> list[str]:
        return self._row


def _fake_baostock(fail_login_times: int):
    """构造一个「前 N 次登录失败、之后成功」的假 baostock 模块。"""
    import types

    state = {"login": 0}

    class _Lib:
        @staticmethod
        def login():
            state["login"] += 1
            code = "10001001" if state["login"] <= fail_login_times else "0"
            return types.SimpleNamespace(error_code=code, error_msg="网络接收错误")

        @staticmethod
        def logout():
            return types.SimpleNamespace(error_code="0")

        @staticmethod
        def query_history_k_data_plus(*_args, **_kwargs):
            return _FakeRs(["2026-10-26", "10.0", "1000000", "2.0", "15.0", "1.5"])

    return _Lib, state


def test_baostock_login_is_retried_once(tmp_path, monkeypatch) -> None:
    """登录偶发「网络接收错误」必须重试一次 —— 一次失败会让整天门槛失效。"""
    import sys

    fake, state = _fake_baostock(fail_login_times=1)
    monkeypatch.setitem(sys.modules, "baostock", fake)
    monkeypatch.setattr(universe_filter.time, "sleep", lambda _s: None)

    got = UniverseFilter(settings=_settings(tmp_path))._fetch_from_baostock(["600000"])

    assert state["login"] == 2, "第一次登录失败后应再试一次"
    assert "600000" in got


def test_describe_marks_dimensions_that_did_not_take_effect(tmp_path) -> None:
    """维度没生效必须写在描述里，否则报告页首就是在撒谎。"""
    f = UniverseFilter(settings=_settings(tmp_path, min_market_cap=100))

    assert "本次未生效" not in f.describe()
    f.skipped_dimensions = ("市值",)
    assert "本次未生效：市值" in f.describe()
    assert "本次未生效" in f.describe(brief=True)


def test_apply_records_skipped_valuation_dimension(tmp_path, monkeypatch) -> None:
    """估值整批失败 ⇒ 该维度被跳过（不剔除），但必须被记进 skipped_dimensions。"""
    db = str(tmp_path / "v.db")
    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", lambda self, symbols: {})
    f = UniverseFilter(settings=_settings(tmp_path, min_market_cap=100), engine=_Engine(db))

    kept = f.apply(["603051"])

    assert kept == ["603051"], "数据源整体故障时不能把票全杀光"
    assert f.skipped_dimensions == ("市值",)
