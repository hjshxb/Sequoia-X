"""距 MA20 偏离度剔除的测试。

拆成两层测：
  1) `_features` 的**算值**正确性 —— 用一条能手算的序列，期望值在测试里独立算一遍；
  2) **决策**（哪档阈值、剔谁、边界、降级）—— 直接 monkeypatch `_features` 返回
     受控的 (dev, vol)，不去硬造「低波 + 大偏离」的价格序列。
     为什么？现实里低波股几乎不可能出现 20% 以上的偏离（今天 49 只里只有 3 只低波），
     硬造出来的序列一定伴随巨量单日跳变，那测到的就不是阈值逻辑了。
"""

import sqlite3
import statistics

import pytest

from sequoia_x.analysis.ma20_filter import Ma20DeviationFilter
from sequoia_x.core.config import Settings


def _settings(tmp_path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        db_path=str(tmp_path / "ma20.db"),
        start_date="2025-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    base.update(kwargs)
    return Settings(**base)


def _seed(tmp_path, series: dict[str, list[float]]) -> str:
    db = str(tmp_path / "ma20.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_daily ("
            "symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
            "volume REAL, turnover REAL, PRIMARY KEY (symbol, date))"
        )
        rows = []
        for symbol, closes in series.items():
            for i, c in enumerate(closes):
                day = f"2026-01-{i + 1:02d}"
                rows.append((symbol, day, c, c, c, c, 1000, c * 1000))
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
    return db


# ── 1) 算值 ──


def test_features_match_hand_computed_values(tmp_path) -> None:
    """21 根：前 20 根都是 100，最后 110 ⇒ 偏离 = 110/100.5−1，波动由 19 个 0 与 1 个 +10% 决定。"""
    closes = [100.0] * 20 + [110.0]
    db = _seed(tmp_path, {"600000": closes})
    filt = Ma20DeviationFilter(_settings(tmp_path), db_path=db)

    dev, vol = filt._features(filt._load(["600000"]))["600000"]

    rets = [0.0] * 19 + [0.1]
    expected_vol = statistics.stdev(rets) * (252**0.5) * 100
    assert dev == pytest.approx(110.0 / statistics.mean(closes[-20:]) - 1)
    assert vol == pytest.approx(expected_vol)


def test_features_skips_series_that_is_too_short(tmp_path) -> None:
    db = _seed(tmp_path, {"600000": [100.0] * 10})
    filt = Ma20DeviationFilter(_settings(tmp_path), db_path=db)

    assert filt._features(filt._load(["600000"])) == {}


# ── 2) 决策（受控 dev/vol） ──


def _filter_with(tmp_path, feats: dict[str, tuple[float, float]], **kwargs):
    """构造一个 `_features` 被写死的过滤器（dev/vol 由调用方指定）。"""
    filt = Ma20DeviationFilter(_settings(tmp_path, **kwargs), db_path=str(tmp_path / "x.db"))
    filt._load = lambda symbols: object()  # 跳过真实取数
    filt._features = lambda bars: feats
    return filt


def test_low_vol_uses_its_own_limit(tmp_path) -> None:
    """低波（vol 20%）阈值 20%：偏离 25% 剔、19% 留。"""
    feats = {"600000": (0.25, 20.0), "600001": (0.19, 20.0)}
    res = _filter_with(tmp_path, feats).apply({"S": ["600000", "600001"]})

    assert res.results["S"] == ["600001"]
    assert res.dropped_symbols == {"600000"}
    assert res.dropped[0].is_low_vol is True
    assert res.dropped[0].limit == 20.0


def test_high_vol_uses_the_looser_limit(tmp_path) -> None:
    """非低波（vol 60%）阈值 30%：偏离 25% 留、35% 剔。"""
    feats = {"600000": (0.25, 60.0), "600001": (0.35, 60.0)}
    res = _filter_with(tmp_path, feats).apply({"S": ["600000", "600001"]})

    assert res.results["S"] == ["600000"]
    assert res.dropped_symbols == {"600001"}
    assert res.dropped[0].is_low_vol is False
    assert res.dropped[0].limit == 30.0


def test_threshold_boundary_is_exclusive(tmp_path) -> None:
    """恰好等于阈值不剔（严格大于才剔），避免边界抖动。"""
    feats = {"600000": (0.20, 20.0)}
    res = _filter_with(tmp_path, feats).apply({"S": ["600000"]})

    assert res.dropped == ()


def test_negative_deviation_is_never_dropped(tmp_path) -> None:
    """跌破 MA20 的票不在本闸门范围内（那是 BottomDoji/ATR 那些策略的活）。"""
    feats = {"600000": (-0.25, 20.0), "600001": (-0.40, 90.0)}
    res = _filter_with(tmp_path, feats).apply({"S": ["600000", "600001"]})

    assert res.dropped == ()
    assert res.results["S"] == ["600000", "600001"]


def test_thresholds_are_configurable(tmp_path) -> None:
    feats = {"600000": (0.15, 20.0)}
    res = _filter_with(
        tmp_path, feats, ma20_dev_limit_low_vol=10.0
    ).apply({"S": ["600000"]})

    assert res.dropped_symbols == {"600000"}


def test_switch_off_keeps_everything(tmp_path) -> None:
    feats = {"600000": (5.0, 20.0)}
    res = _filter_with(tmp_path, feats, ma20_filter_enabled=False).apply({"S": ["600000"]})

    assert res.dropped == ()
    assert res.results["S"] == ["600000"]


def test_missing_price_series_is_kept_with_warning(tmp_path) -> None:
    """取不到 K 线 ⇒ 保留并计入 missing，不把数据问题变成选股结论。"""
    res = _filter_with(tmp_path, {}).apply({"S": ["600000"]})

    assert res.results["S"] == ["600000"]
    assert res.missing == ("600000",)


def test_removes_from_every_strategy_group(tmp_path) -> None:
    """同一只票命中多个策略时，每个分组都要剔掉（不能只处理第一处）。"""
    feats = {"600000": (0.50, 30.0)}
    res = _filter_with(tmp_path, feats).apply({"A": ["600000"], "B": ["600000", "600001"]})

    assert res.results == {"A": [], "B": ["600001"]}


def test_summary_mentions_counts_and_worst(tmp_path) -> None:
    feats = {"600000": (0.25, 20.0), "600001": (0.60, 70.0)}
    res = _filter_with(tmp_path, feats).apply({"S": ["600000", "600001"]})

    text = res.summary()
    assert "剔除 2 只" in text
    assert "低波 1" in text and "非低波 1" in text
    assert "600001 +60.0%" in text


def test_summary_is_empty_without_drops(tmp_path) -> None:
    assert _filter_with(tmp_path, {"600000": (0.01, 20.0)}).apply({"S": ["600000"]}).summary() == ""


def test_empty_results_returns_empty(tmp_path) -> None:
    res = Ma20DeviationFilter(_settings(tmp_path)).apply({})

    assert res.results == {}
    assert res.dropped == ()


def test_database_failure_degrades_to_no_filtering(tmp_path) -> None:
    """库读不出来时一只都不剔 —— 宁可放过，也不要因为读不到就把名单清空。"""
    filt = Ma20DeviationFilter(_settings(tmp_path), db_path=str(tmp_path / "不存在.db"))
    res = filt.apply({"S": ["600000"]})

    assert res.results["S"] == ["600000"]
    assert res.dropped == ()
