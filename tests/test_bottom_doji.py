"""底部十字星策略的回归测试。

锁六件事，每一件对应用户需求里的一个词：

1. **「十字星」是形状条件** —— 实体占全幅比例必须小（_FAT 实体占比 27%）。
2. **「缩量」是当日可观测条件** —— 量比（5日/20日）放大的（_VOL_UP）必须落选。
3. **「底部」是位置条件** —— 位于 60 日区间偏高的同形状十字星（_TOP_DOJI）落选。
4. **「跌破 MA20」是独立条件** —— 贴着均线的浅回调（_SHALLOW）落选，
   它与条件 3 相关但不等价，故单列一个 fixture。
5. **停牌股不选** —— 「今日」取全市场 `MAX(date)`，不按逐股最后一根 K 线。
6. **均线维度豁免** —— 实测 92.9% 的信号在 MA120 下方，过 `apply()` 会被清空。

每个条件都配一个「放宽参数后反向入选」用例（_FAT / _VOL_UP / _TOP_DOJI / _SHALLOW），
否则断言只证明了「它不在结果里」，证明不了「是被这条条件挡掉的」。已用
`tmp/mutation_doji.py` 做过变异检验，五项改动全部被拦住。

⚠️ 刻意没有被测的东西：代码里**没有**「次日放量上涨」条件。它不是漏了 ——
   实测该条件使同日超额从 +1.26% 降到 +0.91%，且「次日跌」反而 +3.06%，
   方向是反的。若有人把它加回来，本文件不会报警，但模块 docstring 里记着数据。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.bottom_doji import BottomDojiStrategy

_ROWS = 130
# 深跌到 60 日低位：10.0 → 9.4 → 8.6 → 8.0
_DEEP_CLOSES = [10.0] * (_ROWS - 3) + [9.4, 8.6, 8.0]
# 崩到 8.0 后横盘 49 根，再收十字星：MA20 ≈ 8.0，即「贴着均线的浅回调」
_SHALLOW_CLOSES = [10.0] * (_ROWS - 50) + [8.0] * 50
# 先崩到 8.0 再一路收复到 9.6，最后一根回落收十字星：
# 60 日位置偏高（≈0.44）但距 MA20 仍有 −6.6% ⇒ 只违反「底部」不违反「跌破 MA20」
_TOP_CLOSES = (
    [10.0] * 70 + [8.0 + 1.6 * i / 58 for i in range(59)] + [8.7]
)
# 停牌：比全市场少一根，但**最后一根本身是合格的底部十字星**
_STALE_CLOSES = [10.0] * (_ROWS - 3) + [9.4, 8.0]
# 跌到 8.0 后最后一根几乎没振幅（0.5%）：形状上是十字星，但属于僵尸票
_FLATLINE_CLOSES = [10.0] * 111 + [10.0 - 2.0 * i / 18 for i in range(1, 19)] + [8.0]

_SHRINK = [1000.0] * (_ROWS - 5) + [400.0] * 5  # 量比 ≈ 0.47
_FLAT_VOL = [1000.0] * _ROWS  # 量比 1.00

_HIT = "000001"  # 底部十字星 + 缩量：唯一应当入选
_FAT = "000002"  # 实体占比 0.27，不是十字星
_VOL_UP = "000003"  # 同样形状，但完全不缩量
_TOP_DOJI = "000004"  # 同形状十字星，但位于 60 日区间偏高位置
_SHALLOW = "000005"  # 位置够低，但只距 MA20 约 0%
_STALE = "000006"  # 合格形态，但最后一根早于全市场最新日（停牌）
_FLATLINE = "000007"  # 位置、形状、量都合格，但振幅只有 0.5%（僵尸票）

# 精筛阈值。`test_main` 里的 `load_dotenv()` 会把仓库根 `.env` 的真实值写进
# `os.environ`，而 `Settings(_env_file=None)` **只管住 .env 文件、管不住环境变量** ——
# 故这里显式清空，避免「单跑绿、全量红」。
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
    """只启用「均线」一个维度，避免测试过程中触发联网维度。"""
    return Settings(
        _env_file=None,
        db_path=str(db_path),
        start_date="2026-01-01",
        feishu_webhook_url="https://example.com/hook",
        min_ma_deviation=0.0,
    )


def _flat_bars(closes, volumes, last_ohlc):
    """平坦序列：开盘=收盘、高低各留 1% 振幅；最后一根用 `last_ohlc` 覆盖。

    Args:
        last_ohlc: (open, high, low, close) 四元组。
    """
    bars = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        if i == len(closes) - 1:
            o, h, lo, cl = last_ohlc
            bars.append((o, h, lo, cl, v))
        else:
            bars.append((c, c * 1.01, c * 0.99, c, v))
    return bars


def _make_engine(tmp_path) -> DataEngine:
    settings = _settings(tmp_path / "doji.db")
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_ROWS, freq="D").strftime("%Y-%m-%d")

    series = {
        # 十字星本体：开=收=8.0，上影到 8.3、下影到 7.7
        _HIT: _flat_bars(_DEEP_CLOSES, _SHRINK, (8.0, 8.3, 7.7, 8.0)),
        # 实体 0.12 / 全幅 0.45 = 0.27 > 0.2 ⇒ 普通 K 线，不是十字星
        _FAT: _flat_bars(_DEEP_CLOSES, _SHRINK, (8.06, 8.15, 7.7, 7.94)),
        # 形状与 _HIT 完全一致，只有成交量不缩
        _VOL_UP: _flat_bars(_DEEP_CLOSES, _FLAT_VOL, (8.0, 8.3, 7.7, 8.0)),
        # 同形状十字星，但 60 日位置 ≈ 0.44（仍距 MA20 −6.6%）
        _TOP_DOJI: _flat_bars(_TOP_CLOSES, _SHRINK, (8.7, 8.85, 8.55, 8.7)),
        # 60 日位置 ≈ 0.13（合格），但 MA20 ≈ 8.0、距 MA20 ≈ 0%（不合格）
        _SHALLOW: _flat_bars(_SHALLOW_CLOSES, _SHRINK, (8.0, 8.3, 7.7, 8.0)),
        # 比全市场最新日早一根：其余条件全部合格
        _STALE: _flat_bars(_STALE_CLOSES, _SHRINK, (8.0, 8.3, 7.7, 8.0)),
        # 振幅仅 0.5%（8.02 − 7.98）/ 8.0，形状、位置、量都合格
        _FLATLINE: _flat_bars(_FLATLINE_CLOSES, _SHRINK, (8.0, 8.02, 7.98, 8.0)),
    }

    rows = []
    for symbol, bars in series.items():
        for day, (o, h, lo, c, v) in zip(dates, bars):
            rows.append((symbol, day, o, h, lo, c, v, 1.0))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def _strategy(tmp_path) -> BottomDojiStrategy:
    engine = _make_engine(tmp_path)
    return BottomDojiStrategy(engine=engine, settings=_settings(engine.db_path))


def test_selects_only_the_bottom_doji(tmp_path) -> None:
    assert _strategy(tmp_path).run() == [_HIT]


def test_fat_body_is_rejected(tmp_path) -> None:
    """实体占全幅 27%，是普通 K 线不是十字星。"""
    assert _FAT not in _strategy(tmp_path).run()


def test_volume_expansion_is_rejected(tmp_path) -> None:
    """形状对、位置对，但量比 1.0 不缩量 ⇒ 落选。"""
    assert _VOL_UP not in _strategy(tmp_path).run()


def test_high_position_doji_is_rejected(tmp_path) -> None:
    """同样标准的十字星 + 缩量，但 60 日位置 0.44，不是「底部」。"""
    assert _TOP_DOJI not in _strategy(tmp_path).run()


def test_shallow_pullback_is_rejected(tmp_path) -> None:
    """位置够低、形状也对，但只距 MA20 约 0%（贴着均线），不是「跌破」。"""
    assert _SHALLOW not in _strategy(tmp_path).run()


def test_too_flat_amplitude_is_rejected(tmp_path) -> None:
    """振幅仅 0.5%：形状上是十字星，但那是没人交易的僵尸票，不是「多空僵持」。"""
    assert _FLATLINE not in _strategy(tmp_path).run()


def test_stale_symbol_is_not_selected(tmp_path) -> None:
    """停牌股在全市场最新日没有 K 线；若按逐股「最后一根」取值会误选出旧信号。"""
    assert _STALE not in _strategy(tmp_path).run()


def test_relaxing_body_ratio_admits_fat_candle(tmp_path) -> None:
    """放宽实体上限后 _FAT 应反向入选 —— 反证它是被形状条件挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.body_ratio_max = 0.5

    result = strategy.run()
    assert _FAT in result
    assert _HIT in result  # 放宽不该把原本合格的挤掉


def test_relaxing_volume_admits_expansion(tmp_path) -> None:
    """放宽量比上限后 _VOL_UP 应反向入选 —— 反证它是被缩量条件挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.vol_ratio_max = 1.1

    result = strategy.run()
    assert _VOL_UP in result
    assert _HIT in result


def test_relaxing_position_admits_top_doji(tmp_path) -> None:
    """放宽 60 日位置上限后 _TOP_DOJI 应反向入选 —— 反证它是被「底部」挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.pos60_max = 1.0

    result = strategy.run()
    assert _TOP_DOJI in result
    assert _HIT in result


def test_relaxing_ma_distance_admits_shallow_pullback(tmp_path) -> None:
    """放宽距 MA20 上限后 _SHALLOW 应反向入选 —— 反证它是被「跌破 MA20」挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.dist_ma20_max = 1.0  # 正数 = 该项恒真，等于关掉

    result = strategy.run()
    assert _SHALLOW in result
    assert _HIT in result


def test_relaxing_amplitude_admits_flatline(tmp_path) -> None:
    """放宽振幅下限后 _FLATLINE 应反向入选 —— 反证它是被振幅条件挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.amp_min = 0.0

    result = strategy.run()
    assert _FLATLINE in result
    assert _HIT in result


def test_ma_dimension_is_what_would_block_it(tmp_path) -> None:
    """同一只底部十字星：过 `apply()` 被 MA120 剔除，过 `apply_without_ma()` 放行。"""
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply([_HIT]) == []  # close 8.0 远低于 MA120
    assert universe.apply_without_ma([_HIT]) == [_HIT]


def test_apply_without_ma_handles_empty_input(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply_without_ma([]) == []


def test_empty_database_returns_empty(tmp_path) -> None:
    settings = _settings(tmp_path / "empty.db")
    engine = DataEngine(settings)

    assert BottomDojiStrategy(engine=engine, settings=settings).run() == []
