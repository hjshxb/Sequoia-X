"""均线收敛发散策略的回归测试。

价格路径是**参数化构造**的（`_path`），因为离散度 z 是相对自身 250 日历史的，
手写数字算不出来。基准路径（`_HIT`）分四段：
  ① 220 根 ±28% 锯齿：把「历史离散度均值」抬上去
  ② 70 根持平：均线挤在一起 ⇒ 离散度塌到 0 ⇒ **收敛**（z 远小于 −0.5）
  ③ 8 根急跌 100 → 72：均线重新张开 ⇒ **今日发散**（z ≥ 0.5）
  ④ 8 根小反弹 72 → 74：MA5 上翘 ⇒ **方向朝上**，且仍在 MA20 下方 11.5%

每个变体只改一个参数，且**先用 `tmp/tune_disp_fixture*.py` 验过「只踩一条」**：
  `_NO_DIVERGE`（锯齿振幅 0.45）只踩「发散」；`_NO_SLOPE`（跌到 66 只反弹 1 根）
  只踩「上翘」。另两条（发散不够 / 距 MA20 不够深）在 fixture 层面与「发散」结构耦合
  —— 要让离散度张开就必须急跌，急跌就必然离均线远 —— 所以它们改用
  **「收紧参数后命中样本落选」**来证明条件确实接在正确的量上。

量比、成交额、停牌这些与价格形状无关的条件，各自单独构造。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.strategy.ma_dispersion import MaDispersionStrategy


def _path(zig_amp=0.28, flat_n=70, drop_to=72.0, bounce_n=8, bounce_to=74.0, zig_n=220):
    """按「锯齿 → 持平 → 急跌 → 小反弹」构造收盘价路径。"""
    prices = [100.0 * (1 + zig_amp * (1 if (i // 10) % 2 == 0 else -1)) for i in range(zig_n)]
    prices += [100.0] * flat_n
    prices += [100.0 - (100.0 - drop_to) * k / 8 for k in range(1, 9)]
    if bounce_n:
        prices += [drop_to + (bounce_to - drop_to) * k / bounce_n for k in range(1, bounce_n + 1)]
    return prices


_HIT = "000001"  # 五项全过
_NO_DIVERGE = "000002"  # 只在「今日发散」上不够（锯齿振幅大 ⇒ 历史均值高）
_NO_SLOPE = "000003"  # 只「未上翘」（跌到 66 只反弹 1 根）
_VOL_UP = "000004"  # 只「不缩量」（量比 1.0）
_STALE = "000005"  # 只「停牌」（少最后 3 根）

_SHRINK = 400.0
_BASE_VOL = 1000.0

# 精筛阈值：`test_main` 的 `load_dotenv()` 会把 .env 写进 os.environ，
# 而 `Settings(_env_file=None)` 只管 .env 文件、管不住环境变量 ⇒ 显式清空。
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
    for name in _FILTER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(db_path, **kwargs) -> Settings:
    base = dict(
        _env_file=None,
        db_path=str(db_path),
        start_date="2025-01-01",
        feishu_webhook_url="https://example.com/hook",
        min_ma_deviation=0.0,
    )
    base.update(kwargs)
    return Settings(**base)


def _volumes(n: int, flat_last5: bool = False) -> list[float]:
    """前段 1000；最后 5 根 400（量比≈0.5）⇒ 缩量。"""
    if flat_last5:
        return [_BASE_VOL] * n
    return [_BASE_VOL] * (n - 5) + [_SHRINK] * 5


def _make_engine(tmp_path) -> DataEngine:
    settings = _settings(tmp_path / "disp.db")
    engine = DataEngine(settings)

    series = {
        _HIT: _path(),
        _NO_DIVERGE: _path(zig_amp=0.45),
        # ⚠️ 必须与 `_path()` 等长：只反弹 1 根会让路径少 7 根，
        # 而日期是按各自长度生成的 ⇒ 它的最后一个交易日就不是全市场最新日，
        # 会被当成停牌股排除（第一版就踩了这个坑，测出来的「未上翘」其实是「停牌」）。
        _NO_SLOPE: _path(flat_n=77, drop_to=66.0, bounce_n=1, bounce_to=67.0),
        _VOL_UP: _path(),
        _STALE: _path(),
    }
    lengths = {len(v) for v in series.values()}
    assert lengths == {306}, f"各样本路径必须等长，实际 {lengths}"
    rows = []
    for symbol, closes in series.items():
        # 停牌样本用**同一形状**、整体提前 3 天 —— 这样它的最后一根完全满足五项条件，
        # 唯一区别就是「最后一个交易日不是全市场最新日」，才真正只测日期口径。
        # （若截短序列，最后一根会落在反弹途中、本身就不合格，测到的是别的东西。）
        offset = -3 if symbol == _STALE else 0
        days = pd.date_range("2025-01-01", periods=len(closes), freq="D")
        dates = (days + pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
        flat = symbol == _VOL_UP
        vols = _volumes(len(closes), flat_last5=flat)
        for day, c, v in zip(dates, closes, vols):
            rows.append((symbol, day, c, c * 1.01, c * 0.99, c, v, c * v))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def _strategy(tmp_path, **kwargs) -> MaDispersionStrategy:
    engine = _make_engine(tmp_path)
    return MaDispersionStrategy(engine=engine, settings=_settings(engine.db_path, **kwargs))


def test_selects_only_the_converged_then_diverged_stock(tmp_path) -> None:
    assert _strategy(tmp_path).run() == [_HIT]


def test_not_diverged_today_is_rejected(tmp_path) -> None:
    """历史离散度基准太高，今天的张开幅度够不上 z>=0.5。"""
    assert _NO_DIVERGE not in _strategy(tmp_path).run()


def test_ma5_turning_down_is_rejected(tmp_path) -> None:
    """其余四项全过，但 MA5 还在向下（跌到 66 只反弹 1 根）⇒ 方向不对，不是向上发散。"""
    assert _NO_SLOPE not in _strategy(tmp_path).run()


def test_volume_expansion_is_rejected(tmp_path) -> None:
    """不缩量（量比 1.0）：均线刚翘头就放量，属于别人已经在买，落选。"""
    assert _VOL_UP not in _strategy(tmp_path).run()


def test_stale_symbol_is_rejected(tmp_path) -> None:
    """形状与命中样本完全相同、但整段提前 3 个交易日（停牌）⇒ 不能当今日信号。

    这只能靠「全市场最新日」判定；若改成逐股取最后一根，它会混进榜单 ——
    `tmp/mutation_disp.py` 的第 7 项变异就是盯这一条。
    """
    assert _STALE not in _strategy(tmp_path).run()


# ── 反向证明：放宽参数后，被挡的样本应回来 ──


def test_lowering_z_high_admits_not_diverged(tmp_path) -> None:
    """把「已发散」门槛降到 z>=0，`_NO_DIVERGE`（z≈−0.25）应反向入选。"""
    strategy = _strategy(tmp_path)
    strategy.z_high = -0.5

    result = strategy.run()
    assert _NO_DIVERGE in result
    assert _HIT in result


def test_lowering_slope_floor_admits_downtrend(tmp_path) -> None:
    """把方向下限放到 −50%，`_NO_SLOPE`（斜率 −23%）应反向入选。"""
    strategy = _strategy(tmp_path)
    strategy.slope_min = -0.5

    result = strategy.run()
    assert _NO_SLOPE in result
    assert _HIT in result


def test_raising_volume_ceiling_admits_expansion(tmp_path) -> None:
    strategy = _strategy(tmp_path)
    strategy.vol_ratio_max = 1.5

    result = strategy.run()
    assert _VOL_UP in result
    assert _HIT in result


# ── 「发散」与「距 MA20」在 fixture 层面结构耦合，改用收紧参数证明 ──


def test_tightening_z_high_drops_the_hit(tmp_path) -> None:
    """命中样本 z=+0.57，门槛收紧到 +2.0 应把它挡掉 ⇒ 条件确认接在 z 上。"""
    strategy = _strategy(tmp_path)
    strategy.z_high = 2.0

    assert _HIT not in strategy.run()


def test_tightening_dist_ma20_drops_the_hit(tmp_path) -> None:
    """命中样本距 MA20 只有 −11.5%，要求「至少跌 15%」应把它挡掉。"""
    strategy = _strategy(tmp_path)
    strategy.dist_ma20_max = -0.15

    assert _HIT not in strategy.run()


def test_tightening_z_low_drops_the_hit(tmp_path) -> None:
    """命中样本 20 日内最收敛只到 z=−1.77，要求「曾收敛到 −3」应把它挡掉。"""
    strategy = _strategy(tmp_path)
    strategy.z_low = -3.0

    assert _HIT not in strategy.run()


# ── 成交额门槛（按策略放宽）──


def test_turnover_override_is_applied_only_when_looser(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    loose_default = MaDispersionStrategy(engine=engine, settings=_settings(engine.db_path))
    assert loose_default._turnover_override() is None

    strict = MaDispersionStrategy(
        engine=engine, settings=_settings(engine.db_path, min_turnover=3.0)
    )
    assert strict._turnover_override() == 2.0


def test_turnover_floor_of_two_yi_keeps_the_hit(tmp_path, monkeypatch) -> None:
    """全局 3 亿、本策略 2 亿：成交额 2.5 亿的命中样本应放行。"""
    monkeypatch.setattr(
        UniverseFilter, "_fetch_turnover", lambda self, symbols: dict.fromkeys(symbols, 2.5e8)
    )
    engine = _make_engine(tmp_path)
    strategy = MaDispersionStrategy(
        engine=engine, settings=_settings(engine.db_path, min_turnover=3.0)
    )

    assert strategy.run() == [_HIT]


# ── 均线维度豁免 ──


def test_ma_dimension_is_what_would_block_it(tmp_path) -> None:
    """候选距 MA20 −11.5%，通常在 MA120 下方：过 `apply()` 被剔除，豁免则放行。"""
    engine = _make_engine(tmp_path)
    universe = UniverseFilter(settings=_settings(engine.db_path), engine=engine)

    assert universe.apply([_HIT]) == []
    assert universe.apply_without_ma([_HIT]) == [_HIT]


def test_empty_database_returns_empty(tmp_path) -> None:
    settings = _settings(tmp_path / "empty.db")
    engine = DataEngine(settings)

    assert MaDispersionStrategy(engine=engine, settings=settings).run() == []
