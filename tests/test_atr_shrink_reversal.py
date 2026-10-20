"""ATR 超跌 + 缩量反转策略的回归测试。

锁四件事：

1. **ATR 阈值确实在起作用（双向）** —— 深跌（跌破 MA20 约 5.7 个 ATR）入选，
   浅跌（约 1.4 个 ATR）落选；把 `atr_threshold` 放宽到 −0.5 后浅跌那只反过来入选。
   双向断言是为了防「阈值写反/恒真」这类静默失效。
2. **缩量是区间而非单边阈值** —— 这是本策略最容易改错的地方：
   量比 1.0（不缩量）与 0.13（缩死，无人接盘）都必须被挡掉，只留 0.4~0.8 这一档。
   用同一套收盘价、只改成交量来隔离这个条件。
3. **停牌股不选** —— 「今日」取全市场 `MAX(date)`，不按逐股「最后一根 K 线」，
   否则停牌股会拿旧价算出早已过期的假信号。
4. **均线维度的豁免** —— 这是该策略能工作的前提：同一条超跌信号会被
   `apply()`（含 `close >= MA120`）整条剔除，却应被 `apply_without_ma()` 放行。
   实测真实库里 97.6% 的 ATR 超跌信号都在 MA120 之下，若哪天有人把策略改回走注入池，
   第 4 组用例会立刻变红。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.atr_shrink_reversal import AtrShrinkReversalStrategy

_ROWS = 130
# 深跌：10.0 → 9.4 → 8.8 → 8.0，dist_atr ≈ −5.68（< −4）
_DEEP_PATH = [10.0] * (_ROWS - 3) + [9.4, 8.8, 8.0]
# 浅跌：10.0 → 9.9 → 9.8 → 9.7，dist_atr ≈ −1.35（> −4）
_SHALLOW_PATH = [10.0] * (_ROWS - 3) + [9.9, 9.8, 9.7]

# 四个「同样深跌」的样本，只靠成交量区分——用来隔离缩量区间这一条
_SHRINK = [1000.0] * (_ROWS - 5) + [400.0] * 5  # 量比 ≈ 0.47，落在区间内
_DEAD_VOL = [1000.0] * (_ROWS - 5) + [100.0] * 5  # 量比 ≈ 0.13，缩死
_SURGE_VOL = [1000.0] * (_ROWS - 5) + [2000.0] * 5  # 量比 ≈ 1.60，放量下跌

_SELECTED = "000001"  # 深跌 + 缩量：唯一应当入选
_RISING = "000002"  # 单边上涨：贴上轨、dist_atr 为正
_STALE = "000003"  # 同样深跌，但最后一根 K 线早于全市场最新日（停牌）
_DEAD = "000004"  # 深跌 + 极度缩量
_VOL_SURGE = "000005"  # 深跌 + 放量
_SHALLOW = "000006"  # 浅跌 + 缩量：被 ATR 阈值挡掉

# 精筛阈值。`test_main` 里的 `load_dotenv()` 会把仓库根 `.env` 的真实值写进
# `os.environ`，而 `Settings(_env_file=None)` **只管住 .env 文件、管不住环境变量** ——
# 本文件按字母序在 test_main 之前才一直没暴露，但改名/新增文件就会红，故显式清空。
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
    settings = _settings(tmp_path / "atr_reversal.db")
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_ROWS, freq="D").strftime("%Y-%m-%d")

    series = {
        _SELECTED: (_DEEP_PATH, _SHRINK),
        # 上涨票也给缩量成交量：否则它被「缩量区间」顺手挡掉，就测不出 ATR 的符号方向
        _RISING: ([10.0 + 0.05 * i for i in range(_ROWS)], _SHRINK),
        # 停牌票同样给缩量成交量：否则它被缩量顺手挡掉，就测不出「按全市场最新日取值」
        _STALE: (([10.0] * 25 + [9.4, 8.8, 8.0])[:28], [1000.0] * 23 + [400.0] * 5),
        _DEAD: (_DEEP_PATH, _DEAD_VOL),
        _VOL_SURGE: (_DEEP_PATH, _SURGE_VOL),
        _SHALLOW: (_SHALLOW_PATH, _SHRINK),
    }
    rows = []
    for symbol, (closes, volumes) in series.items():
        for day, close, volume in zip(dates, closes, volumes):
            # 留 2% 的日内振幅，让 ATR 有真实波幅可算（高=低=收 会让 ATR 退化成收盘差）
            rows.append((symbol, day, close, close * 1.01, close * 0.99, close, volume, 1.0))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def test_selects_only_the_deep_drop_shrinking_symbol(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    strategy = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path))

    assert strategy.run() == [_SELECTED]


def test_rising_and_stale_symbols_are_not_selected(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    result = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _RISING not in result  # 收盘价在 MA20 上方，dist_atr 为正
    # 停牌股在全市场最新日没有 K 线；若按逐股「最后一根」取值会误选出旧信号
    assert _STALE not in result


def test_shallow_drop_is_rejected(tmp_path) -> None:
    """浅跌（约 1.4 个 ATR）不该入选 —— 阈值本身要在「自然方向」上生效。"""
    engine = _make_engine(tmp_path)
    result = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _SHALLOW not in result


def test_loosening_atr_threshold_admits_shallow_drop(tmp_path) -> None:
    """把阈值放宽到 −0.5 后，浅跌那只应反向入选 —— 反证 _SHALLOW 是被 ATR 挡掉的。"""
    engine = _make_engine(tmp_path)
    strategy = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path))
    strategy.atr_threshold = -0.5

    result = strategy.run()
    assert result == [_SELECTED, _SHALLOW]  # 按 dist_atr 升序：−5.68 在 −1.35 之前
    assert _RISING not in result


def test_extreme_volume_shrink_is_rejected(tmp_path) -> None:
    """缩量过头 = 无人接盘 / 流动性枯竭，实测该档收益反而为负，必须挡掉。"""
    engine = _make_engine(tmp_path)
    result = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _DEAD not in result


def test_volume_surge_is_rejected(tmp_path) -> None:
    """放量下跌不属「抛压衰竭」，实测胜率明显低于缩量档，挡掉。"""
    engine = _make_engine(tmp_path)
    result = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path)).run()

    assert _VOL_SURGE not in result


def test_volume_band_can_be_disabled(tmp_path) -> None:
    """把区间放宽到 (0, ∞) 后，缩死与放量两只都应重新入选 —— 反证区间确实在起作用。"""
    engine = _make_engine(tmp_path)
    strategy = AtrShrinkReversalStrategy(engine=engine, settings=_settings(engine.db_path))
    strategy.vol_ratio_min = 0.0
    strategy.vol_ratio_max = 100.0

    result = strategy.run()
    assert _DEAD in result
    assert _VOL_SURGE in result


def test_ma_dimension_is_what_would_block_it(tmp_path) -> None:
    """同一只深跌股：过 `apply()` 被 MA120 剔除，过 `apply_without_ma()` 放行。"""
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply([_SELECTED]) == []  # close 8.0 远低于 MA120
    assert universe.apply_without_ma([_SELECTED]) == [_SELECTED]


def test_apply_without_ma_handles_empty_input(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply_without_ma([]) == []


def test_empty_database_returns_empty(tmp_path) -> None:
    settings = _settings(tmp_path / "empty.db")
    engine = DataEngine(settings)

    assert AtrShrinkReversalStrategy(engine=engine, settings=settings).run() == []
