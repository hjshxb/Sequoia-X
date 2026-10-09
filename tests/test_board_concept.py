"""「所属板块 / 概念」数据模块单元测试。

覆盖点：
    - 解析口径：只要 `IS_PRECISE=1` 的精准概念，丢掉指数样本 / 风格噪音
    - 行业取申万一级（名单命中）与「最具体一层」的兜底
    - 聚合口径：命中只数、排序、覆盖数（`total - covered` 即缺失数）
    - 缓存：命中不联网、TTL 过期才刷新、写回落盘
    - 降级：单只失败不影响整批；刷新失败时用本地旧值

全部用例都通过桩 `_request` 屏蔽网络，**不联网**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import sequoia_x.data.board_concept as bc
from sequoia_x.data.board_concept import StockBoards, parse_rows, summarize

# ── 桩数据 ──

_CATL_ROWS = [
    {"BOARD_NAME": "茅指数", "IS_PRECISE": "0", "BOARD_TYPE": None, "BOARD_CODE": "999"},
    {"BOARD_NAME": "HS300_", "IS_PRECISE": "0", "BOARD_TYPE": None, "BOARD_CODE": "500"},
    {"BOARD_NAME": "深股通", "IS_PRECISE": "0", "BOARD_TYPE": None, "BOARD_CODE": "804"},
    {"BOARD_NAME": "储能概念", "IS_PRECISE": "1", "BOARD_TYPE": None, "BOARD_CODE": "989"},
    {"BOARD_NAME": "固态电池", "IS_PRECISE": "1", "BOARD_TYPE": None, "BOARD_CODE": "968"},
    {"BOARD_NAME": "储能概念", "IS_PRECISE": "1", "BOARD_TYPE": None, "BOARD_CODE": "989"},
    {"BOARD_NAME": "电力设备", "IS_PRECISE": None, "BOARD_TYPE": "行业", "BOARD_CODE": "1200"},
    {"BOARD_NAME": "电池", "IS_PRECISE": "0", "BOARD_TYPE": "行业", "BOARD_CODE": "1033"},
    {"BOARD_NAME": "锂电池", "IS_PRECISE": None, "BOARD_TYPE": "行业", "BOARD_CODE": "1303"},
    {"BOARD_NAME": "福建板块", "IS_PRECISE": "0", "BOARD_TYPE": "板块", "BOARD_CODE": "151"},
]


@pytest.fixture(autouse=True)
def _clear_memo():
    bc.reset_cache()
    yield
    bc.reset_cache()


def _fresh_entry(concepts=("固态电池",), industry="电力设备", region="福建板块") -> dict:
    return {
        "concepts": list(concepts),
        "industry": industry,
        "region": region,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _write_cache(cache_dir: Path, entries: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "board_concept.json").write_text(
        json.dumps({"boards": entries}, ensure_ascii=False), encoding="utf-8"
    )


# ── 解析 ──


def test_parse_rows_keeps_only_precise_concepts() -> None:
    """`IS_PRECISE=0` 的指数样本 / 风格标签必须全部丢掉。

    放进来会得出「今天选出来的票扎堆在大盘股 / 融资融券」这种等于没说的话，
    还会把真正的题材挤出展示上限。
    """
    boards = parse_rows("300750", _CATL_ROWS)

    assert boards.concepts == ("储能概念", "固态电池")  # 去重且保序
    for noise in ("茅指数", "HS300_", "深股通", "电池"):
        assert noise not in boards.concepts


def test_parse_rows_industry_prefers_primary_level() -> None:
    """行业优先取申万一级（名单命中）：展示端要的是大板块，不是最细的那一层。"""
    assert parse_rows("300750", _CATL_ROWS).industry == "电力设备"


def test_parse_rows_industry_falls_back_to_most_specific() -> None:
    """一级行业名单没命中时，退回「行业代码最大」的那条（东财实测即最具体一层）。"""
    rows = [
        {"BOARD_NAME": "半导体", "IS_PRECISE": "0", "BOARD_TYPE": "行业", "BOARD_CODE": "1036"},
        {
            "BOARD_NAME": "集成电路制造",
            "IS_PRECISE": None,
            "BOARD_TYPE": "行业",
            "BOARD_CODE": "1329",
        },
    ]
    assert parse_rows("688981", rows).industry == "集成电路制造"


def test_parse_rows_region_and_empty() -> None:
    """地域板块取首个；三类全空时 `is_empty` 为真（聚合时不计入覆盖数）。"""
    boards = parse_rows("300750", _CATL_ROWS)
    assert boards.region == "福建板块"

    empty = parse_rows("999999", [])
    assert empty.concepts == () and empty.industry is None and empty.region is None
    assert empty.is_empty is True


def test_parse_rows_skips_blank_names() -> None:
    """`BOARD_NAME` 为空的脏行直接跳过，不进任何一类。"""
    rows = [{"BOARD_NAME": "  ", "IS_PRECISE": "1", "BOARD_TYPE": "行业"}]
    assert parse_rows("600000", rows).is_empty is True


# ── 聚合 ──


def test_summarize_counts_order_and_coverage() -> None:
    """计数、排序（只数降序 → 名称升序）与覆盖数。"""
    boards = {
        "600000": StockBoards("600000", ("创新药", "CRO"), "医药生物", "上海板块"),
        "000001": StockBoards("000001", ("创新药",), "银行", "深圳板块"),
        "000002": StockBoards("000002"),  # 三类全空 ⇒ 不计入覆盖
    }
    summary = summarize(boards, ["600000", "000001", "000002", "999999"])

    assert summary.total == 4
    assert summary.covered == 2, "空结果与查不到的股票都不算覆盖"
    assert summary.concepts == (("创新药", 2), ("CRO", 1))
    assert summary.industries == (("医药生物", 1), ("银行", 1))
    assert summary.regions == (("上海板块", 1), ("深圳板块", 1))
    assert summary.is_empty is False


def test_summarize_dedupes_symbols_and_handles_empty() -> None:
    """重复代码只算一次（调用方可能传「各策略拼接」而非去重名单）。"""
    boards = {"600000": StockBoards("600000", ("创新药",), "医药生物", None)}
    summary = summarize(boards, ["600000", "600000", "600000"])

    assert summary.total == 1
    assert summary.concepts == (("创新药", 1),)

    assert summarize({}, []).is_empty is True


# ── 取数与缓存 ──


def test_load_uses_fresh_cache_without_network(tmp_path, monkeypatch) -> None:
    """TTL 内命中缓存 ⇒ 一次网络请求都不发。"""
    _write_cache(tmp_path, {"300750": _fresh_entry()})

    def boom(*_a, **_kw):
        raise AssertionError("命中缓存时不应联网")

    monkeypatch.setattr(bc, "_request", boom)
    boards = bc.load_board_concepts(["300750"], cache_dir=str(tmp_path))

    assert boards["300750"].concepts == ("固态电池",)
    assert boards["300750"].industry == "电力设备"


def test_load_refreshes_when_ttl_is_zero(tmp_path, monkeypatch) -> None:
    """`ttl_days=0` 表示不信任缓存、每次都刷新。"""
    _write_cache(tmp_path, {"300750": _fresh_entry(concepts=("旧概念",))})
    calls: list[str] = []

    def fake_request(symbol: str) -> list[dict]:
        calls.append(symbol)
        return _CATL_ROWS

    monkeypatch.setattr(bc, "_request", fake_request)
    boards = bc.load_board_concepts(["300750"], cache_dir=str(tmp_path), ttl_days=0)

    assert calls == ["300750"]
    assert boards["300750"].concepts == ("储能概念", "固态电池")
    # 写回：磁盘上的旧值已被刷新后的结果覆盖
    payload = json.loads((tmp_path / "board_concept.json").read_text(encoding="utf-8"))
    assert payload["boards"]["300750"]["concepts"] == ["储能概念", "固态电池"]


def test_load_ignores_nonexistent_symbols_and_reports_partial_failure(
    tmp_path, monkeypatch
) -> None:
    """单只失败不影响其余：成功的照常返回，失败的只是缺数据（不抛异常）。"""

    def fake_request(symbol: str) -> list[dict]:
        if symbol == "600000":
            raise RuntimeError("连接被重置")
        return _CATL_ROWS

    monkeypatch.setattr(bc, "_request", fake_request)
    boards = bc.load_board_concepts(["300750", "600000"], cache_dir=str(tmp_path))

    assert set(boards) == {"300750"}
    assert "600000" not in boards


def test_load_degrades_to_stale_cache_when_refresh_fails(tmp_path, monkeypatch) -> None:
    """刷新失败但本地有旧值时**降级使用旧值** —— 题材变化慢，旧值远好过没有。"""
    stale = _fresh_entry(concepts=("老概念",))
    stale["fetched_at"] = "2000-01-01 00:00:00"
    _write_cache(tmp_path, {"300750": stale})

    def boom(_symbol: str) -> list[dict]:
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(bc, "_request", boom)
    boards = bc.load_board_concepts(["300750"], cache_dir=str(tmp_path))

    assert boards["300750"].concepts == ("老概念",)


def test_load_empty_symbols_makes_no_request(tmp_path, monkeypatch) -> None:
    """空名单不发请求（当日无选股结果时不该产生任何网络开销）。"""

    def boom(*_a, **_kw):
        raise AssertionError("空名单不应联网")

    monkeypatch.setattr(bc, "_request", boom)
    assert bc.load_board_concepts([], cache_dir=str(tmp_path)) == {}


def test_corrupt_cache_file_degrades_to_network(tmp_path, monkeypatch) -> None:
    """缓存文件损坏时当作「无缓存」重拉，而不是抛异常中断报告。"""
    (tmp_path / "board_concept.json").write_text("{ 坏掉的 json", encoding="utf-8")
    monkeypatch.setattr(bc, "_request", lambda _s: _CATL_ROWS)

    boards = bc.load_board_concepts(["300750"], cache_dir=str(tmp_path))
    assert boards["300750"].industry == "电力设备"
