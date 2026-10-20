"""底部十字星 + 放量阳线确认 的回归测试。

锁十件事 —— 前五件是「形态日」的条件，后四件是「确认日」的条件，最后一件是日期口径：

形态日（T-1）：
 1. **十字星**：实体占全幅比例要小（_FAT 实体占比 27%）。
 2. **缩量**：量比（5日/20日）放大的（_VOL_UP）落选。
 3. **底部**：60 日区间偏高的同形状十字星（_TOP_DOJI）落选。
 4. **跌破 MA20**：贴着均线的浅回调（_SHALLOW）落选，与条件 3 相关但不等价。
 5. **振幅**：几乎无振幅的僵尸票（_FLATLINE）落选。

确认日（T）：
 6. **放量**：只放 1.0 倍的（_WEAK_CONFIRM）落选。
 7. **收红**：低开回升但收在前收之下的（_LOW_CONFIRM）落选。
 8. **阳线**：高开回落收阴（_FADE）落选。
 9. **未创前 5 日新高**：确认根直接突破前高的（_BREAKOUT）落选。

日期口径：
10. **停牌股不选**：_SUSPENDED 的形态与确认都合格，但整段停在 3 个交易日之前
    ⇒ 落选。「今日」取全市场 `MAX(date)`，不按逐股最后一根 K 线。

⚠️ 「只找十字星、不等确认」这个旧行为**不需要单独用例**：十字星在 T-1、确认在 T，
   两根不是同一根，结构上就不可能只凭十字星入选。真正会出错的是日期口径（第 10 条）。

每个参数都配一个「放宽后反向入选」用例，否则断言只证明了「它不在结果里」，
证明不了「是被这条条件挡掉的」。阳线（第 8 条）**没有可放宽的参数**，
它的因果由 `tmp/mutation_doji.py` 的变异检验负责（把 `>` 改成 `<` 应当让测试红）。

⚠️ 本文件**不再**断言「只有十字星也能入选」—— 那正是本次要修的行为。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.bottom_doji import BottomDojiStrategy

_ROWS = 130
_PLATEAU = 11.0  # 前期高位平台：把 60 日振幅拉够，避免十字星自己的长下影把自己顶出「底部」

# 合格形态日：开=收=8.0，上影到 8.6、下影到 7.6（实体 0、振幅 12.5%、双向影线齐全）
_DOJI = (8.0, 8.6, 7.6, 8.0)
# 合格确认日：开 8.05 → 收 8.40（阳线、涨 5%），且 8.40 < 前 5 日最高 8.6（未突破）
_CONFIRM = (8.05, 8.45, 8.0, 8.40)

# 最后 6 根在 8.0（前期高位平台塌下来），前段全部 11.0
_DEEP_CLOSES = [_PLATEAU] * (_ROWS - 6) + [8.0] * 6
# 崩到 8.0 后横盘 50 根：MA20 ≈ 8.0 ⇒「贴着均线的浅回调」
_SHALLOW_CLOSES = [_PLATEAU] * (_ROWS - 50) + [8.0] * 50
# 从 8.0 一路收复到 ≈9.57，最后一根回落到 8.7：
# 60 日位置 ≈0.24（偏高）但距 MA20 仍有 −6.4% ⇒ 只违反「底部」
_TOP_CLOSES = (
    [_PLATEAU] * 70 + [8.0 + 1.6 * i / 58 for i in range(58)] + [8.7, 8.7]
)

_HIT = "000001"  # 底部十字星 + 放量阳线确认：唯一应当入选
_FAT = "000002"  # 形态日实体占比 0.27，不是十字星
_VOL_UP = "000003"  # 形态日同样形状，但完全不缩量
_TOP_DOJI = "000004"  # 同形状十字星，但位于 60 日区间偏高位置
_SHALLOW = "000005"  # 位置够低，但只距 MA20 约 0%
_FLATLINE = "000006"  # 位置、形状、量都对，但振幅只有 0.5%
_STALE = "000007"  # 形态 + 确认都合格，但整段停在 3 个交易日前（停牌）
_WEAK_CONFIRM = "000008"  # 确认根是阳线收红，但量只有 1.0 倍
_LOW_CONFIRM = "000009"  # 确认根是阳线，但收在前收之下（低开回升，未收复）
_FADE = "000010"  # 确认根高开回落收阴（close < open），但涨了 +1.25%
_BREAKOUT = "000011"  # 形态、放量、阳线全对，但确认根直接创前 5 日新高

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


def _volumes(n: int, tail=None) -> list[float]:
    """前段 1000；最后 5 根 400（缩量，量比≈0.59）；`tail` 逐根覆盖，None 表示不覆盖。"""
    vols = [1000.0] * (n - 5) + [400.0] * 5
    if tail:
        for i, v in enumerate(tail):
            if v is not None:
                vols[n - len(tail) + i] = v
    return vols


def _bars(closes, volumes, tail):
    """平坦序列：收盘=开盘、高低各留 1% 振幅；最后 `len(tail)` 根用 tail 的 OHLC 覆盖。

    Args:
        tail: [(open, high, low, close), ...]，长度可小于序列长度。
    """
    n = len(closes)
    start = n - len(tail)
    bars = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        if i >= start:
            o, h, lo, cl = tail[i - start]
            bars.append((o, h, lo, cl, v))
        else:
            bars.append((c, c * 1.01, c * 0.99, c, v))
    return bars


def _make_engine(tmp_path) -> DataEngine:
    settings = _settings(tmp_path / "doji.db")
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_ROWS, freq="D").strftime("%Y-%m-%d")

    deep = _volumes(_ROWS, [None, None, None, None, 600.0])  # 确认根 600 ⇒ 1.36 倍
    weak = _volumes(_ROWS, [None, None, None, None, 400.0])  # 确认根 400 ⇒ 1.00 倍
    flat = [1000.0] * (_ROWS - 1) + [1400.0]  # 量比恒为 1，仅形态日不缩量

    series = {
        # 唯一应入选：形态日十字星 + 缩量，确认日阳线收红 + 放量 + 未突破
        _HIT: _bars(_DEEP_CLOSES, deep, [_DOJI, _CONFIRM]),
        # 实体 0.27 / 全幅 1.0 = 0.27 > 0.2 ⇒ 普通 K 线，不是十字星
        _FAT: _bars(_DEEP_CLOSES, deep, [(7.75, 8.6, 7.6, 8.02), (8.05, 8.45, 8.0, 8.45)]),
        # 形状与 _HIT 完全一致，只有形态日的成交量不缩
        _VOL_UP: _bars(_DEEP_CLOSES, flat, [_DOJI, _CONFIRM]),
        # 同形状十字星，但 60 日位置 ≈0.24（仍距 MA20 −6.4%）
        _TOP_DOJI: _bars(_TOP_CLOSES, deep, [(8.7, 8.85, 8.55, 8.7), (8.75, 9.1, 8.7, 9.0)]),
        # 60 日位置 ≈0.11（合格），但 MA20 = 8.0、距 MA20 = 0%（不合格）
        _SHALLOW: _bars(_SHALLOW_CLOSES, deep, [_DOJI, _CONFIRM]),
        # 振幅仅 0.5%（8.02 − 7.98）/ 8.0；另外给出 T-5 的 8.6 上影，避免被「未突破」顺带挡掉
        _FLATLINE: _bars(
            _DEEP_CLOSES,
            deep,
            [(8.0, 8.6, 7.92, 8.0), (8.0, 8.02, 7.98, 8.0), _CONFIRM],
        ),
        # 形态与确认都合格，但整段停在 3 个交易日之前（停牌）。
        # ⚠️ 序列必须整段重建，不能对定长序列切片 —— 成交量是按位置对齐的，
        #    切片会把确认根那根 600 的量切掉，变成被「放量不足」挡掉（假的通过）。
        _STALE: _bars(
            _DEEP_CLOSES[:-3],
            _volumes(_ROWS - 3, [None, None, None, None, 600.0]),
            [_DOJI, _CONFIRM],
        ),
        # 阳线收红，但确认根量只有 1.0 倍
        _WEAK_CONFIRM: _bars(_DEEP_CLOSES, weak, [_DOJI, _CONFIRM]),
        # 形态日在 8.2；确认根开 8.0 收 8.10 ⇒ 阳线但涨 −1.2%（未收复前收）
        _LOW_CONFIRM: _bars(
            _DEEP_CLOSES, deep, [(8.2, 8.5, 8.0, 8.2), (8.0, 8.15, 7.95, 8.10)]
        ),
        # 确认根高开 8.6 收 8.10 ⇒ 收阴，但相对前收仍 +1.25%
        _FADE: _bars(_DEEP_CLOSES, deep, [_DOJI, (8.6, 8.65, 8.05, 8.10)]),
        # 形态日上影只到 8.2，确认根收 8.40 ⇒ 直接创前 5 日新高
        _BREAKOUT: _bars(_DEEP_CLOSES, deep, [(8.0, 8.2, 7.7, 8.0), _CONFIRM]),
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


def test_selects_only_the_confirmed_bottom_doji(tmp_path) -> None:
    assert _strategy(tmp_path).run() == [_HIT]


def test_fat_body_is_rejected(tmp_path) -> None:
    """形态日实体占全幅 27%，是普通 K 线不是十字星。"""
    assert _FAT not in _strategy(tmp_path).run()


def test_volume_expansion_on_doji_day_is_rejected(tmp_path) -> None:
    """形状对、位置对，但形态日量比 1.0 不缩量 ⇒ 落选（注意与确认日的放量是两回事）。"""
    assert _VOL_UP not in _strategy(tmp_path).run()


def test_high_position_doji_is_rejected(tmp_path) -> None:
    """同样标准的十字星 + 缩量，但 60 日位置 0.24，不是「底部」。"""
    assert _TOP_DOJI not in _strategy(tmp_path).run()


def test_shallow_pullback_is_rejected(tmp_path) -> None:
    """位置够低、形状也对，但只距 MA20 约 0%（贴着均线），不是「跌破」。"""
    assert _SHALLOW not in _strategy(tmp_path).run()


def test_too_flat_amplitude_is_rejected(tmp_path) -> None:
    """振幅仅 0.5%：形状上是十字星，但那是没人交易的僵尸票，不是「多空僵持」。"""
    assert _FLATLINE not in _strategy(tmp_path).run()


def test_stale_symbol_without_confirmation_is_rejected(tmp_path) -> None:
    """形态与确认都合格，但整段停在 3 个交易日前 ⇒ 不该被算作「今日」信号。

    若把「今日」改成逐股取最后一根 K 线，它就会混进榜单 —— 这是本节要防的错。
    """
    assert _STALE not in _strategy(tmp_path).run()


def test_weak_volume_confirmation_is_rejected(tmp_path) -> None:
    """确认根是阳线、也收红，但量只有 1.0 倍，够不上「放量」。"""
    assert _WEAK_CONFIRM not in _strategy(tmp_path).run()


def test_unrecovered_gap_down_confirmation_is_rejected(tmp_path) -> None:
    """确认根是阳线（低开回升），但仍收在前收之下 ⇒ 不算「确认」。"""
    assert _LOW_CONFIRM not in _strategy(tmp_path).run()


def test_faded_confirmation_is_rejected(tmp_path) -> None:
    """确认根高开 8.6 收 8.10，虽是相对上涨，但收阴 ⇒ 不是「阳线」。"""
    assert _FADE not in _strategy(tmp_path).run()


def test_breakout_confirmation_is_rejected(tmp_path) -> None:
    """其余全对，但确认根直接创了前 5 日新高 ⇒ 属于追高，默认不收。"""
    assert _BREAKOUT not in _strategy(tmp_path).run()


def test_relaxing_body_ratio_admits_fat_candle(tmp_path) -> None:
    """放宽实体上限后 _FAT 应反向入选 —— 反证它是被形状条件挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.body_ratio_max = 0.5

    result = strategy.run()
    assert _FAT in result
    assert _HIT in result  # 放宽不该把原本合格的挤掉


def test_relaxing_doji_volume_admits_expansion(tmp_path) -> None:
    """放宽形态日量比上限后 _VOL_UP 应反向入选。"""
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
    """放宽距 MA20 上限后 _SHALLOW 应反向入选。"""
    strategy = _strategy(tmp_path)
    strategy.dist_ma20_max = 1.0  # 正数 = 该项恒真，等于关掉

    result = strategy.run()
    assert _SHALLOW in result
    assert _HIT in result


def test_relaxing_amplitude_admits_flatline(tmp_path) -> None:
    """放宽振幅下限后 _FLATLINE 应反向入选。"""
    strategy = _strategy(tmp_path)
    strategy.amp_min = 0.0

    result = strategy.run()
    assert _FLATLINE in result
    assert _HIT in result


def test_lowering_confirm_volume_admits_weak_confirmation(tmp_path) -> None:
    """把放量门槛降到 0.9 后 _WEAK_CONFIRM 应反向入选 —— 反证它是被「放量」挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.confirm_vol_min = 0.9

    result = strategy.run()
    assert _WEAK_CONFIRM in result
    assert _HIT in result


def test_lowering_confirm_gain_admits_unrecovered_gap(tmp_path) -> None:
    """容许收在前收之下（-50%）后 _LOW_CONFIRM 应反向入选。"""
    strategy = _strategy(tmp_path)
    strategy.confirm_min_gain = -0.5

    result = strategy.run()
    assert _LOW_CONFIRM in result
    assert _HIT in result


def test_disabling_breakout_guard_admits_breakout(tmp_path) -> None:
    """关掉「未创前 N 日新高」后 _BREAKOUT 应反向入选 —— 反证它是被这条挡掉的。"""
    strategy = _strategy(tmp_path)
    strategy.require_no_breakout = False

    result = strategy.run()
    assert _BREAKOUT in result
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
