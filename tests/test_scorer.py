"""候选股量化评分器单元测试（`sequoia_x.analysis.scorer`）。

覆盖点：
    - 五维打分的分档边界，重点是**负 PE 不再被判满分**这个历史 bug
    - 位置惩罚的两个触发源（追高偏离 / 高波动）与阈值的开闭区间
    - 本地库批量读取：升序、条数上限、自动分批、无数据股票不出现
    - `score_pool` 的排序语义与 metrics / holdings / tags 的接线
    - 增强层是**可选**的：未配置路径、导入失败、样本不足、工具抛异常，
      一律降级且不影响基础分；`prob_up` 已是 0~100 百分数，**不得再乘 100**
"""

import sqlite3
import sys
from dataclasses import replace
from datetime import date, timedelta

import pytest

from sequoia_x.analysis import scorer
from sequoia_x.data.universe_filter import StockMetric

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""


def make_rows(
    symbol: str,
    n: int = 200,
    *,
    start: float = 10.0,
    daily: float = 0.004,
    volume: float = 1_000_000.0,
    volumes: list[float] | None = None,
    swing: float = 0.02,
) -> list[tuple]:
    """生成一段等比上涨的日线，返回可直接写库的 8 元组。

    Args:
        daily: 每日涨幅（0.004 ≈ 稳健上行）。
        volumes: 逐日成交量；给定时覆盖 `volume`（用于构造量比场景）。
        swing: 最高/最低价相对收盘价的百分比摆幅。
    """
    rows = []
    for i in range(n):
        d = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
        close = start * (1 + daily) ** i
        vol = volumes[i] if volumes is not None else volume
        rows.append(
            (
                symbol,
                d,
                close,
                close * (1 + swing),
                close * (1 - swing),
                close,
                vol,
                close * vol,
            )
        )
    return rows


def write_db(tmp_path, series: dict[str, list[tuple]]) -> str:
    """把多只股票的行写进临时 SQLite，返回库路径。"""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_SQL)
    for rows in series.values():
        conn.executemany(
            "INSERT INTO stock_daily"
            " (symbol, date, open, high, low, close, volume, turnover)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path):
    """三只股票：AAA 强势放量、BBB 温和上涨、CCC 下跌。"""
    return write_db(
        tmp_path,
        {
            "AAA": make_rows("AAA", 200, daily=0.006),
            "BBB": make_rows("BBB", 200, daily=0.002),
            "CCC": make_rows("CCC", 200, daily=-0.003),
        },
    )


# ── 五维打分 ──


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ("TR", 20.0),  # 海龟 + RPS 双突破信号共振
        ("T", 14.0),
        ("R", 14.0),
        ("TX", 14.0),  # 非突破类策略不额外加权
        ("", 10.0),
    ],
)
def test_score_resonance(tags, expected):
    assert scorer.score_resonance(tags) == expected


@pytest.mark.parametrize(
    ("chg1", "vol_ratio", "expected"),
    [
        (2.0, 1.5, 20.0),  # 涨且显著放量
        (2.0, 1.3, 20.0),  # 阈值含等号
        (2.0, 1.0, 15.0),
        (2.0, 0.5, 12.0),  # 缩量上涨，中性
        (-1.0, 0.5, 10.0),  # 缩量下跌
        (-1.0, 1.1, 7.0),
        (-1.0, 1.5, 4.0),  # 放量下跌 = 抛压
        (None, 1.5, 12.0),  # 缺数据给中性分，不奖不罚
        (2.0, None, 12.0),
    ],
)
def test_score_volume(chg1, vol_ratio, expected):
    assert scorer.score_volume(chg1, vol_ratio) == expected


def test_score_valuation_negative_pe_is_penalised():
    """回归：负 PE（亏损股）必须判低分。

    早期版本用 `pe < 40` 一把抓，亏损股会被判成满分 20 —— 这是个反向的
    隐蔽 bug，因为「PE 越低越好」在负数区间恰好翻转了。
    """
    assert scorer.score_valuation(-5.0) == 2.0
    # 亏损股不能优于任何一个正的估值档位（含最贵的 PE 100 档）
    assert scorer.score_valuation(-5.0) < scorer.score_valuation(100.0)
    assert scorer.score_valuation(-5.0) < scorer.score_valuation(25.0)


@pytest.mark.parametrize(
    ("pe", "expected"),
    [(0.1, 20.0), (39.9, 20.0), (40.0, 17.0), (59.9, 17.0), (60.0, 13.0), (89.9, 13.0)],
)
def test_score_valuation_brackets(pe, expected):
    assert scorer.score_valuation(pe) == expected


def test_score_valuation_missing_or_infinite_is_neutral():
    assert scorer.score_valuation(None) == 13.0
    assert scorer.score_valuation(float("inf")) == 13.0
    assert scorer.score_valuation(float("nan")) == 13.0


@pytest.mark.parametrize(
    ("hold", "expected"),
    [
        (80.0, 20.0),
        (75.0, 20.0),
        (74.9, 17.0),
        (65.0, 17.0),
        (55.0, 13.0),
        (45.0, 9.0),
        (10.0, 5.0),
    ],
)
def test_score_chip(hold, expected):
    assert scorer.score_chip(hold) == expected


def test_score_chip_missing_is_neutral():
    assert scorer.score_chip(None) == 10.0
    assert scorer.score_chip(float("nan")) == 10.0


def test_score_trend_combines_ma_stack_and_position():
    assert scorer.score_trend(3, 0.99) == 20.0  # 三线多头 + 贴区间高点
    assert scorer.score_trend(0, 0.50) == 2.0  # 空头 + 深度回落
    assert scorer.score_trend(2, None) == 8.0  # 无高点数据时只用均线分


# ── 位置惩罚 ──


@pytest.mark.parametrize(
    ("dev_ma20", "vol20", "expected"),
    [
        (0.0, 0.0, 0.0),
        (20.0, 0.0, 0.0),  # 阈值为「严格大于」，等值不罚
        (20.1, 0.0, 3.0),
        (30.0, 0.0, 3.0),
        (30.1, 0.0, 6.0),
        (40.0, 0.0, 6.0),
        (40.1, 0.0, 10.0),
        (0.0, 80.0, 0.0),
        (0.0, 80.1, 3.0),
        (0.0, 100.1, 6.0),
        (40.1, 100.1, 16.0),  # 追高 + 高波动叠加
        (None, None, 0.0),  # 无数据不惩罚
    ],
)
def test_position_penalty(dev_ma20, vol20, expected):
    assert scorer.position_penalty(dev_ma20, vol20) == expected


# ── 特征计算 ──


def test_change_needs_enough_history():
    closes = [10.0, 11.0]
    assert scorer._change(closes, 1) == pytest.approx(10.0)
    assert scorer._change(closes, 2) is None  # 不足 k+1 个点


def test_annual_vol_of_flat_series_is_zero():
    flat = make_rows("X", 60, daily=0.0)
    closes = [r[3] for r in flat]
    assert scorer._annual_vol(closes) == pytest.approx(0.0, abs=1e-9)


def _wave_closes(n: int, amp: float) -> list[float]:
    """逐日交替涨跌 ±amp 的收盘序列（用于构造可控的波动率）。

    注意不能拿 `make_rows(daily=...)` 比波动率 —— 那是**等比**序列，
    每日收益率恒定，波动率必然为 0，比的只是浮点噪声。
    """
    closes = [10.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + (amp if i % 2 else -amp)))
    return closes


def test_annual_vol_grows_with_swings():
    assert scorer._annual_vol(_wave_closes(60, 0.05)) > scorer._annual_vol(_wave_closes(60, 0.005))


def test_streak_counts_consecutive_gains():
    assert scorer._streak([1.0, 2.0, 3.0, 4.0]) == 3
    assert scorer._streak([1.0, 2.0, 1.5]) == 0
    assert scorer._streak([1.0, 0.5]) == 0


# ── build_detail ──


def test_build_detail_skips_insufficient_history(db):
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    assert scorer.build_detail("AAA", rows[: scorer.MIN_ROWS - 1]) is None
    assert scorer.build_detail("AAA", rows[: scorer.MIN_ROWS]) is not None


def test_build_detail_computes_features(db):
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    detail = scorer.build_detail(
        "AAA",
        rows,
        metric=StockMetric(symbol="AAA", pe_ttm=25.0),
        holding=70.0,
        tags="TR",
    )
    assert detail.symbol == "AAA"
    assert detail.last_date == rows[-1][0]
    assert detail.tags == "TR"
    assert detail.chg1 > 0
    assert detail.chg20 is not None and detail.chg60 is not None
    assert detail.streak == len(rows) - 1  # 全程单边上行
    assert detail.bull == 3  # 5/10/20/60 日均线多头排列
    assert detail.vol20 == pytest.approx(0.0, abs=1e-6)  # 等比上涨 => 收益恒定
    assert detail.amount is not None and detail.amount > 0
    # 区间最高价高于收盘价，故 near_high 略小于 1
    assert 0.97 < detail.near_high < 1.0
    assert detail.dims == {
        "策略共振": 20.0,
        "量能配合": 15.0,
        "趋势强度": 20.0,
        "估值": 20.0,
        "筹码": 17.0,
    }


def test_build_detail_marks_new_highs(db):
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    detail = scorer.build_detail("AAA", rows)
    assert detail.new_high60 is True  # 单边上行，最新收盘即区间最高
    assert detail.new_high120 is True
    assert detail.near_high is not None and detail.near_high < 1.0  # 高点取 high 不是 close


def test_build_detail_no_penalty_for_healthy_uptrend(db):
    """稳健上行（距 MA20 个位数百分比）不该被扣分。"""
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    detail = scorer.build_detail("AAA", rows)
    assert detail.dev_ma20 is not None and detail.dev_ma20 < 20
    assert detail.penalty == 0.0
    assert detail.adjusted == detail.total


def test_build_detail_applies_position_penalty(tmp_path):
    """末日跳涨 60% ⇒ 既追高（距 MA20 > 40%）又高波动，两笔惩罚都要落上。"""
    rows = make_rows("SPK", 200, daily=0.0)
    last = rows[-1]
    shot = last[5] * 1.6
    rows[-1] = (last[0], last[1], shot, shot * 1.01, shot * 0.99, shot, last[6], shot * last[6])
    path = write_db(tmp_path, {"SPK": rows})

    detail = scorer.score_pool(["SPK"], db_path=path)[0]
    assert detail.dev_ma20 is not None and detail.dev_ma20 > 40
    assert detail.vol20 is not None and detail.vol20 > 100
    assert detail.penalty == 16.0  # 追高 10 + 高波动 6
    assert detail.adjusted == detail.total - 16.0


def test_score_property_rounds_to_one_decimal():
    rows = make_rows("AAA", 200)
    detail = scorer.build_detail("AAA", [r[1:] for r in rows])
    assert detail is not None
    rounded = replace(detail, adjusted=71.234)
    assert rounded.score == 71.2


# ── 本地库读取 ──


def test_read_price_series_returns_ascending_window(db):
    series = scorer.read_price_series(db, ["AAA"], lookback=250)
    rows = series["AAA"]
    assert len(rows) == 200  # 只有 200 天，小于 lookback
    dates = [r[0] for r in rows]
    assert dates == sorted(dates)
    assert rows[-1][0] == (date(2025, 1, 1) + timedelta(days=199)).isoformat()


def test_read_price_series_limits_to_lookback(db):
    rows = scorer.read_price_series(db, ["AAA"], lookback=60)["AAA"]
    assert len(rows) == 60
    # 截取的必须是**最近** 60 天
    assert rows[-1][0] == (date(2025, 1, 1) + timedelta(days=199)).isoformat()
    assert rows[0][0] == (date(2025, 1, 1) + timedelta(days=140)).isoformat()


def test_read_price_series_omits_unknown_symbols(db):
    series = scorer.read_price_series(db, ["AAA", "NOPE"])
    assert "NOPE" not in series
    assert set(series) == {"AAA"}


def test_read_price_series_empty_input(db):
    assert scorer.read_price_series(db, []) == {}
    assert scorer.read_price_series(db, ["", None]) == {}


def test_read_price_series_chunks_large_pool(tmp_path, monkeypatch):
    """代码数超过单条 SQL 参数上限时自动分批，不能漏股票。"""
    series = {f"{i:06d}": make_rows(f"{i:06d}", 140) for i in range(5)}
    path = write_db(tmp_path, series)
    monkeypatch.setattr(scorer, "_SQL_PARAM_CHUNK", 2)
    got = scorer.read_price_series(path, list(series))
    assert set(got) == set(series)


# ── score_pool ──


def test_score_pool_sorted_by_adjusted_desc(db):
    details = scorer.score_pool(["AAA", "BBB", "CCC"], db_path=db)
    assert len(details) == 3
    adjusted = [d.adjusted for d in details]
    assert adjusted == sorted(adjusted, reverse=True)
    # 上行趋势的 AAA 应优于下跌的 CCC
    assert details[0].symbol == "AAA"
    assert details[-1].symbol == "CCC"


def test_score_pool_wires_metrics_holdings_tags(db):
    plain = scorer.score_pool(["AAA"], db_path=db)[0]
    enriched = scorer.score_pool(
        ["AAA"],
        db_path=db,
        metrics={"AAA": StockMetric(symbol="AAA", pe_ttm=25.0)},
        holdings={"AAA": 80.0},
        tags={"AAA": "TR"},
    )[0]
    assert plain.dims["估值"] == 13.0  # 无 PE -> 中性分
    assert enriched.dims["估值"] == 20.0
    assert plain.dims["筹码"] == 10.0
    assert enriched.dims["筹码"] == 20.0
    assert plain.dims["策略共振"] == 10.0
    assert enriched.dims["策略共振"] == 20.0
    assert enriched.adjusted > plain.adjusted


def test_score_pool_skips_short_history(tmp_path):
    series = {"AAA": make_rows("AAA", 200), "NEW": make_rows("NEW", 40)}
    path = write_db(tmp_path, series)
    details = scorer.score_pool(["AAA", "NEW"], db_path=path)
    assert [d.symbol for d in details] == ["AAA"]


def test_score_pool_empty_when_no_history(db):
    assert scorer.score_pool(["NOPE"], db_path=db) == []


# ── 增强层（可选）──


def test_load_quant_skill_returns_none_when_unset():
    assert scorer.load_quant_skill("") is None


def test_load_quant_skill_returns_none_when_path_missing(tmp_path):
    assert scorer.load_quant_skill(str(tmp_path / "nope")) is None


def test_load_quant_skill_returns_none_when_import_fails(tmp_path, monkeypatch):
    """工具不在依赖里 —— 导入失败必须静默降级，不能把主流程带崩。"""
    monkeypatch.setitem(sys.modules, "stock_researcher", None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert scorer.load_quant_skill(str(tmp_path)) is None


def test_score_pool_without_skill_path_does_not_enhance(db):
    details = scorer.score_pool(["AAA", "BBB"], db_path=db, enhance_top=5)
    assert all(d.prob_up is None and d.max_drawdown is None for d in details)


def test_score_pool_enhance_keeps_prob_up_scale(db, monkeypatch):
    """回归：`prob_up` 已是 0~100 百分数，不得做「<=1 就乘 100」的换算。

    真实值 0.5（即 0.5%）曾被猜成 50%，会把最没把握的形态排到最前。
    """
    calls: list[str] = []

    def fake_forecast(closes, horizon=5):
        return {"prob_up": 0.5, "predicted_pct": 1.2, "data_mode": "ok"}

    def fake_drawdown(closes):
        return {"max_drawdown": 0.18}

    def fake_loader(skill_path):
        calls.append(skill_path)
        return fake_forecast, fake_drawdown

    monkeypatch.setattr(scorer, "load_quant_skill", fake_loader)
    details = scorer.score_pool(
        ["AAA", "BBB"], db_path=db, enhance_top=5, skill_path="/whatever"
    )
    assert calls == ["/whatever"]
    assert all(d.prob_up == pytest.approx(0.5) for d in details)
    assert all(d.expected_pct == pytest.approx(1.2) for d in details)
    assert all(d.max_drawdown == pytest.approx(18.0) for d in details)  # 小数 -> 百分数


def test_score_pool_enhance_only_top_n(db, monkeypatch):
    seen: list[int] = []

    def fake_forecast(closes, horizon=5):
        seen.append(len(closes))
        return {"prob_up": 60.0, "data_mode": "ok"}

    def fake_drawdown(closes):
        return {"max_drawdown": 0.1}

    monkeypatch.setattr(scorer, "load_quant_skill", lambda _p: (fake_forecast, fake_drawdown))
    details = scorer.score_pool(
        ["AAA", "BBB", "CCC"], db_path=db, enhance_top=2, skill_path="/x"
    )
    assert len(seen) == 2  # 只增强前 2 名
    assert details[0].prob_up is not None
    assert details[1].prob_up is not None
    assert details[2].prob_up is None


def test_enhance_ignores_degraded_pattern_result(db):
    """工具降级返回（data_mode 非 ok）时不能把占位值当真实胜率。"""
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    detail = scorer.build_detail("AAA", rows)
    assert detail is not None

    def fake_forecast(closes, horizon=5):
        return {"prob_up": 33.3, "data_mode": "insufficient_sample"}

    def fake_drawdown(closes):
        return {"max_drawdown": 0.25}

    enhanced = scorer._enhance(detail, [r[3] for r in rows], (fake_forecast, fake_drawdown))
    assert enhanced.prob_up is None  # 降级值不入库
    assert enhanced.max_drawdown == pytest.approx(25.0)  # 回撤照常可用
    assert enhanced.adjusted == detail.adjusted  # 基础分不受影响


def test_enhance_swallows_tool_exceptions(db):
    rows = scorer.read_price_series(db, ["AAA"])["AAA"]
    detail = scorer.build_detail("AAA", rows)
    assert detail is not None

    def boom(*_a, **_kw):
        raise RuntimeError("tool exploded")

    enhanced = scorer._enhance(detail, [r[3] for r in rows], (boom, boom))
    assert enhanced == detail  # 原样返回，不抛异常


def test_score_pool_survives_broken_enhancer(db, monkeypatch):
    """增强层的加载意外抛错时，基础评分必须照常返回。"""

    def boom(*_a, **_kw):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(scorer, "load_quant_skill", boom)
    details = scorer.score_pool(["AAA", "BBB"], db_path=db, enhance_top=1, skill_path="/x")
    assert [d.symbol for d in details] == ["AAA", "BBB"]
    assert all(d.adjusted > 0 for d in details)
    assert all(d.prob_up is None for d in details)


def test_read_price_series_opens_read_only(db, monkeypatch):
    """必须以只读方式打开：同步进程可能正在写这个库。"""
    targets: list[str] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        targets.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    scorer.read_price_series(db, ["AAA"])
    assert targets and all("mode=ro" in t for t in targets)


def test_annual_vol_is_positive_for_noisy_series():
    """跳变必须落在 20 日窗口内才影响波动率。"""
    closes = [r[3] for r in make_rows("X", 60, daily=0.0)]
    closes[55] = closes[54] * 1.1
    vol = scorer._annual_vol(closes)
    assert vol is not None and vol > 0
