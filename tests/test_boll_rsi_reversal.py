"""布林 + RSI 超跌反转策略的回归测试。

锁两件事：

1. **条件本身** —— 跌破布林下轨且 RSI 极低才入选；上涨的票、RSI 不达标、停牌（无当日行情）都不选。
2. **均线维度的豁免** —— 这是该策略能工作的前提：同一条超跌信号会被
   `apply()`（含 `close >= MA120`）整条剔除，却应被 `apply_without_ma()` 放行。
   如果哪天有人把策略改回走注入池，第 2 组用例会立刻变红。
"""

import sqlite3

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.boll_rsi_reversal import BollRsiReversalStrategy

_ROWS = 130
_OVERSOLD = "000001"  # 长期横盘后连跌三天：跌破下轨 + RSI≈0
_RISING = "000002"  # 单边上涨：贴上轨 + RSI=100
_STALE = "000003"  # 同样超跌，但最后一根 K 线早于全市场最新日（停牌）


def _settings(db_path) -> Settings:
    """只启用「均线」一个维度，避免测试过程中触发联网维度。

    `_env_file=None` 必须给：否则 Settings 会读到仓库根目录的 `.env`
    （里面有 MIN_TURNOVER=3 / MIN_TURN=2~20），测试用的假行情会被直接筛空。
    """
    return Settings(
        _env_file=None,
        db_path=str(db_path),
        start_date="2026-01-01",
        feishu_webhook_url="https://example.com/hook",
        min_ma_deviation=0.0,
    )


def _make_engine(tmp_path) -> DataEngine:
    settings = _settings(tmp_path / "reversal.db")
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_ROWS, freq="D").strftime("%Y-%m-%d")

    series = {
        _OVERSOLD: [10.0] * (_ROWS - 3) + [9.4, 8.8, 8.0],
        _RISING: [10.0 + 0.05 * i for i in range(_ROWS)],
        _STALE: ([10.0] * 25 + [9.4, 8.8, 8.0])[:28],
    }
    rows = []
    for symbol, closes in series.items():
        for day, close in zip(dates, closes):
            rows.append((symbol, day, close, close, close, close, 1.0, 1.0))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def test_selects_only_the_oversold_symbol(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    strategy = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path))

    assert strategy.run() == [_OVERSOLD]


def test_rising_and_stale_symbols_are_not_selected(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    result = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _RISING not in result  # 剔除上轨附近、RSI=100 的票
    # 停牌股在全市场最新日没有 K 线；若按逐股「最后一根」取值会误选出旧信号
    assert _STALE not in result


def test_rsi_threshold_is_enforced(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    strategy = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path))
    strategy.rsi_threshold = -1.0  # 人为把门槛压到不可能满足

    assert strategy.run() == []


def test_ma_dimension_is_what_would_block_it(tmp_path) -> None:
    """同一只超跌股：过 `apply()` 被 MA120 剔除，过 `apply_without_ma()` 放行。"""
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply([_OVERSOLD]) == []  # close 8.0 远低于 MA120 ≈ 9.97
    assert universe.apply_without_ma([_OVERSOLD]) == [_OVERSOLD]


def test_apply_without_ma_handles_empty_input(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply_without_ma([]) == []


def test_empty_database_returns_empty(tmp_path) -> None:
    settings = _settings(tmp_path / "empty.db")
    engine = DataEngine(settings)

    assert BollRsiReversalStrategy(engine=engine, settings=settings).run() == []
