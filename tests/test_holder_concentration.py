"""十大流通股东合计持股比例（筹码集中度）数据模块单元测试。

数据来源：东财数据中心 `RPT_F10_EH_FREEHOLDERS`（全市场，按报告期分页）。
覆盖点：
    - 报告期推算（仅取已披露的季末，且包含当日恰为季末的情况）
    - 逐行聚合：只累加前 10 名，忽略缺失值
    - 缓存：命中缓存不联网；联网成功写缓存
    - 降级：最新期无数据时回退上一期；全部拉取失败时回退到旧缓存
"""

import json
from datetime import date

import pytest

from sequoia_x.data import holder_concentration as hc


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例用独立缓存目录，并清空进程内缓存。"""
    hc.reset_cache()
    monkeypatch.setattr(hc, "DEFAULT_CACHE_DIR", str(tmp_path / "cache"))
    yield
    hc.reset_cache()


def rows(*specs):
    """specs: (code, rank, ratio)"""
    return [
        {"SECURITY_CODE": code, "HOLDER_RANK": rank, "FREE_HOLDNUM_RATIO": ratio}
        for code, rank, ratio in specs
    ]


def stub_fetch(mapping: dict[str, dict[str, float]], calls: list | None = None):
    """mapping: {report_date: {symbol: ratio}}；缺失的报告期返回 {}。"""

    def _fetch(report_date, **kwargs):
        if calls is not None:
            calls.append(report_date)
        return dict(mapping.get(report_date, {}))

    return _fetch


# ── 报告期推算 ──


def test_report_periods_skips_unpublished_quarter():
    """2026-09-19 时，2026-09-30 尚未到，首个候选应为 2026-06-30。"""
    periods = hc.report_periods(date(2026, 9, 19))
    assert periods[0] == "2026-06-30"
    assert "2026-03-31" in periods
    assert "2025-12-31" in periods
    assert "2026-09-30" not in periods


def test_report_periods_is_descending():
    periods = hc.report_periods(date(2026, 9, 19))
    assert periods == sorted(periods, reverse=True)


def test_report_periods_includes_today_when_quarter_end():
    """报告期当天也应算作候选（数据商可能已更新）。"""
    periods = hc.report_periods(date(2026, 6, 30))
    assert periods[0] == "2026-06-30"


def test_report_periods_covers_two_years():
    periods = hc.report_periods(date(2026, 9, 19))
    assert len(periods) == 6  # 2026Q1/Q2 + 2025 四个季末 = 6


# ── 聚合 ──


def test_aggregate_sums_top10():
    got = hc.aggregate(rows(("600000", 1, 21.28), ("600000", 2, 18.18), ("600000", 3, 8.35)))
    assert got == {"600000": pytest.approx(47.81)}


def test_aggregate_ignores_rank_above_10():
    got = hc.aggregate(rows(("600000", 1, 10.0), ("600000", 11, 99.0)))
    assert got == {"600000": pytest.approx(10.0)}


def test_aggregate_skips_missing_or_invalid():
    got = hc.aggregate(
        [
            {"SECURITY_CODE": "600000", "HOLDER_RANK": 1, "FREE_HOLDNUM_RATIO": None},
            {"SECURITY_CODE": "", "HOLDER_RANK": 1, "FREE_HOLDNUM_RATIO": 5.0},
            {"SECURITY_CODE": "600000", "HOLDER_RANK": None, "FREE_HOLDNUM_RATIO": 5.0},
            {"SECURITY_CODE": "600000", "HOLDER_RANK": 2, "FREE_HOLDNUM_RATIO": "abc"},
            {"SECURITY_CODE": "600000", "HOLDER_RANK": 3, "FREE_HOLDNUM_RATIO": 12.5},
        ]
    )
    assert got == {"600000": pytest.approx(12.5)}


def test_aggregate_string_numbers_are_tolerated():
    got = hc.aggregate([{"SECURITY_CODE": 600000, "HOLDER_RANK": "1", "FREE_HOLDNUM_RATIO": "7.5"}])
    assert got == {"600000": pytest.approx(7.5)}


# ── 缓存 ──


def test_load_writes_cache_and_does_not_refetch(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(
        hc, "fetch_market_ratios", stub_fetch({"2026-06-30": {"600000": 47.8}}, calls)
    )

    first = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=str(tmp_path / "cache"))
    assert first == {"600000": pytest.approx(47.8)}

    hc.reset_cache()  # 只清进程内缓存，磁盘缓存仍在
    second = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=str(tmp_path / "cache"))
    assert second == {"600000": pytest.approx(47.8)}
    assert calls == ["2026-06-30"], f"第二次应命中磁盘缓存，实际请求 {calls}"


def test_cache_file_records_report_date(monkeypatch, tmp_path):
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setattr(hc, "fetch_market_ratios", stub_fetch({"2026-06-30": {"600000": 30.0}}))
    hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)

    payload = json.loads(hc.cache_path(cache_dir, "2026-06-30").read_text(encoding="utf-8"))
    assert payload["report_date"] == "2026-06-30"
    assert payload["ratios"] == {"600000": 30.0}


def test_refresh_bypasses_cache(monkeypatch, tmp_path):
    cache_dir = str(tmp_path / "cache")
    calls: list = []
    monkeypatch.setattr(
        hc, "fetch_market_ratios", stub_fetch({"2026-06-30": {"600000": 30.0}}, calls)
    )
    hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)
    hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir, refresh=True)
    assert calls == ["2026-06-30", "2026-06-30"]


# ── 降级 ──


def test_falls_back_to_previous_period_when_latest_empty(monkeypatch, tmp_path):
    """最新报告期暂无数据时，应自动回退到上一期，而不是返回空。"""
    calls: list = []
    monkeypatch.setattr(
        hc,
        "fetch_market_ratios",
        stub_fetch({"2026-03-31": {"600000": 41.0}}, calls),
    )
    got = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=str(tmp_path / "cache"))
    assert got == {"600000": pytest.approx(41.0)}
    assert calls[0] == "2026-06-30" and calls[1] == "2026-03-31"


def test_original_failure_returns_empty_without_cache(monkeypatch, tmp_path):
    def boom(report_date, **kwargs):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(hc, "fetch_market_ratios", boom)
    got = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=str(tmp_path / "cache"))
    assert got == {}


def test_falls_back_to_stale_cache_on_failure(monkeypatch, tmp_path):
    """联网全挂时，退回到磁盘上现有的旧报告期缓存（记录会变更旧）。"""
    cache_dir = str(tmp_path / "cache")
    monkeypatch.setattr(hc, "fetch_market_ratios", stub_fetch({"2026-03-31": {"600000": 41.0}}))
    hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)

    def boom(report_date, **kwargs):
        raise RuntimeError("网络不可用")

    hc.reset_cache()
    monkeypatch.setattr(hc, "fetch_market_ratios", boom)
    got = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)
    assert got == {"600000": pytest.approx(41.0)}


# ── 指定报告期 ──


def test_pinned_report_date_is_used(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(
        hc,
        "fetch_market_ratios",
        stub_fetch({"2025-12-31": {"600000": 50.0}}, calls),
    )
    got = hc.load_top10_free_holding(
        today=date(2026, 9, 19), report_date="2025-12-31", cache_dir=str(tmp_path / "cache")
    )
    assert got == {"600000": pytest.approx(50.0)}
    assert calls == ["2025-12-31"]


def test_pinned_report_date_accepts_compact_form(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(
        hc, "fetch_market_ratios", stub_fetch({"2026-06-30": {"600000": 50.0}}, calls)
    )
    hc.load_top10_free_holding(
        today=date(2026, 9, 19), report_date="20260630", cache_dir=str(tmp_path / "cache")
    )
    assert calls == ["2026-06-30"]


# ── 进程内缓存 ──


def test_memo_avoids_repeated_disk_reads(monkeypatch, tmp_path):
    cache_dir = str(tmp_path / "cache")
    calls: list = []
    monkeypatch.setattr(
        hc, "fetch_market_ratios", stub_fetch({"2026-06-30": {"600000": 30.0}}, calls)
    )
    a = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)
    b = hc.load_top10_free_holding(today=date(2026, 9, 19), cache_dir=cache_dir)
    assert a == b
    assert calls == ["2026-06-30"]


# ── 分页拉取（网络层） ──


def stub_pages(total: int, raise_on: set[int] | None = None, seen: list | None = None):
    """构造一个假的 `_request`：共 total 页，`raise_on` 中的页码抛异常。"""
    raise_on = raise_on or set()

    def _request(params):
        number = int(params["pageNumber"])
        if seen is not None:
            seen.append(number)
        if number in raise_on:
            raise RuntimeError(f"第 {number} 页炸了")
        return {
            "result": {
                "pages": total,
                "data": rows((f"{number:06d}", 1, 10.0), (f"{number:06d}", 2, 5.0)),
            }
        }

    return _request


def test_fetch_market_ratios_walks_every_page(monkeypatch):
    seen: list = []
    monkeypatch.setattr(hc, "_request", stub_pages(3, seen=seen))
    got = hc.fetch_market_ratios("2026-06-30", workers=2)

    assert sorted(seen) == [1, 2, 3]
    # 每页两只，各 10.0 + 5.0 = 15.0
    assert got == {"000001": 15.0, "000002": 15.0, "000003": 15.0}


def test_fetch_market_ratios_tolerates_one_failed_page(monkeypatch):
    """单页失败不能让整批拉取崩掉（这是并发分支最容易写错的地方）。"""
    monkeypatch.setattr(hc, "_request", stub_pages(3, raise_on={2}))
    got = hc.fetch_market_ratios("2026-06-30", workers=2)

    assert set(got) == {"000001", "000003"}
    assert got["000001"] == pytest.approx(15.0)


def test_fetch_market_ratios_empty_when_report_missing(monkeypatch):
    """报告期尚未披露时接口返回 result=None，应返回空字典而不是抛异常。"""
    monkeypatch.setattr(
        hc, "_request", lambda params: {"result": None, "message": "报表配置不存在"}
    )
    assert hc.fetch_market_ratios("2099-12-31") == {}


def test_fetch_market_ratios_caps_absurd_page_count(monkeypatch):
    """异常的超大页数会被截断，避免无限拉取。"""
    seen: list = []
    monkeypatch.setattr(hc, "_MAX_PAGES", 3)
    monkeypatch.setattr(hc, "_request", stub_pages(9999, seen=seen))
    hc.fetch_market_ratios("2026-06-30", workers=2)
    assert sorted(seen) == [1, 2, 3]
