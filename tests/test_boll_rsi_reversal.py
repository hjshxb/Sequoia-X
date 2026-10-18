"""布林 + RSI 超跌反转策略的回归测试。

锁三件事：

1. **条件本身** —— 跌破布林下轨、RSI 极低、且处缩量区间才入选；
   上涨的票、RSI 不达标、停牌（无当日行情）都不选。
2. **缩量是区间而非单边阈值** —— 这是本策略最容易改错的地方：
   量比 1.0（不缩量）与 0.13（缩死，无人接盘）都必须被挡掉，
   只留 0.4~0.8 这一档。用同一套收盘价、只改成交量来隔离这个条件。
3. **均线维度的豁免** —— 这是该策略能工作的前提：同一条超跌信号会被
   `apply()`（含 `close >= MA120`）整条剔除，却应被 `apply_without_ma()` 放行。
   如果哪天有人把策略改回走注入池，第 3 组用例会立刻变红。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.boll_rsi_reversal import BollRsiReversalStrategy

_ROWS = 130
_OVERSOLD_PATH = [10.0] * (_ROWS - 3) + [9.4, 8.8, 8.0]

# 四个「同样超跌」的样本，只靠成交量区分——用来隔离缩量区间这一条
_SHRINK = [1000.0] * (_ROWS - 5) + [400.0] * 5  # 量比 ≈ 0.47，落在区间内
_FLAT_VOL = [1000.0] * _ROWS  # 量比 1.00，不缩量
_DEAD_VOL = [1000.0] * (_ROWS - 5) + [100.0] * 5  # 量比 ≈ 0.13，缩死
_SURGE_VOL = [1000.0] * (_ROWS - 5) + [2000.0] * 5  # 量比 ≈ 1.60，放量下跌

_OVERSOLD = "000001"  # 超跌 + 缩量：唯一应当入选
_RISING = "000002"  # 单边上涨：贴上轨 + RSI=100
_STALE = "000003"  # 同样超跌，但最后一根 K 线早于全市场最新日（停牌）
_DEAD = "000004"  # 超跌 + 极度缩量
_VOL_SURGE = "000005"  # 超跌 + 放量

# 精筛阈值。`test_main` 里的 `load_dotenv()` 会把仓库根 `.env` 的真实值写进
# `os.environ`，而 `Settings(_env_file=None)` **只管住 .env 文件、管不住环境变量** ——
# 本文件按字母序恰好在 test_main 之前才一直没暴露，但改名就会红，故显式清空。
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
    """清掉可能被其他测试污染的精筛阈值，保证「只有本策略的条件在起作用」。"""
    for name in _FILTER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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
        _OVERSOLD: (_OVERSOLD_PATH, _SHRINK),
        _RISING: ([10.0 + 0.05 * i for i in range(_ROWS)], _FLAT_VOL),
        _STALE: (([10.0] * 25 + [9.4, 8.8, 8.0])[:28], _FLAT_VOL),
        _DEAD: (_OVERSOLD_PATH, _DEAD_VOL),
        _VOL_SURGE: (_OVERSOLD_PATH, _SURGE_VOL),
    }
    rows = []
    for symbol, (closes, volumes) in series.items():
        for day, close, volume in zip(dates, closes, volumes):
            rows.append((symbol, day, close, close, close, close, volume, 1.0))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def test_selects_only_the_oversold_shrinking_symbol(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    strategy = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path))

    assert strategy.run() == [_OVERSOLD]


def test_rising_and_stale_symbols_are_not_selected(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    result = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _RISING not in result  # 剔除上轨附近、RSI=100 的票
    # 停牌股在全市场最新日没有 K 线；若按逐股「最后一根」取值会误选出旧信号
    assert _STALE not in result


def test_extreme_volume_shrink_is_rejected(tmp_path) -> None:
    """缩量过头 = 无人接盘 / 流动性枯竭，实测 20 日均值反而是 −1.19%，必须挡掉。"""
    engine = _make_engine(tmp_path)
    result = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _DEAD not in result


def test_volume_surge_is_rejected(tmp_path) -> None:
    """放量下跌不属「抛压衰竭」，实测胜率 62.1% < 缩量档 73~79%，挡掉。"""
    engine = _make_engine(tmp_path)
    result = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _VOL_SURGE not in result


def test_volume_band_can_be_disabled(tmp_path) -> None:
    """把区间放宽到 (0, ∞) 后，缩死与放量两只都应重新入选 —— 反证区间确实在起作用。"""
    engine = _make_engine(tmp_path)
    strategy = BollRsiReversalStrategy(engine=engine, settings=_settings(engine.db_path))
    strategy.vol_ratio_min = 0.0
    strategy.vol_ratio_max = 100.0

    result = strategy.run()
    assert _DEAD in result
    assert _VOL_SURGE in result


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
