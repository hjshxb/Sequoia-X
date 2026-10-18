"""深回调缩量策略（PullbackShrinkStrategy）的回归测试。

每个「不入选」的样例都只违反**一个**条件，用来锁死各自的门槛：

- `_VOL_FLAT`     —— 同样的回调，但成交不缩量
- `_SHALLOW`      —— 只回落 15%，没到 30% 下限
- `_TOO_DEEP`     —— 回落 55%，超过 45% 上限（但仍站在 MA60 上方，故只可能被上限挡掉）
- `_BELOW_MA60`   —— 回调 35% 达标，但收盘跌破 MA60（趋势已破）

另外用 `_SELECTED` 正向确认条件齐全时确实入选，并验证注入池会生效。
"""

import sqlite3

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.pullback_shrink import PullbackShrinkStrategy

_ROWS = 62
_FLAT = 10.0

# 精筛的各项阈值。`test_main` 里的 `load_dotenv()` 会把仓库根 `.env` 的真实值
# 写进 `os.environ`，而 `Settings(_env_file=None)` **只管住 .env 文件、管不住环境变量**，
# 于是排在它后面的本文件会莫名其妙被真实阈值筛空（本文件按字母序正好在 test_main 之后）。
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


_SELECTED = "000001"  # 60日高点 20 → 现价 13（−35%），仍在 MA60 上方，缩量
_VOL_FLAT = "000002"  # 同上，但量能不变
_SHALLOW = "000003"  # 高点 20 → 17（−15%），没到下限
_TOO_DEEP = "000004"  # 高点 30 → 13.5（−55%），超过上限
_BELOW_MA60 = "000005"  # 高点 20 → 13，但 MA60 已被跌破


def _settings(db_path) -> Settings:
    """`_env_file=None` 必须给：否则会读到仓库根目录 `.env` 里的 MIN_TURNOVER=3 等真实阈值，
    测试用的假行情会被精筛直接筛空。

    注意它只关掉 .env **文件**；环境变量那份由 `_isolate_filter_env` 负责清空。
    """
    return Settings(
        _env_file=None,
        db_path=str(db_path),
        start_date="2026-01-01",
        feishu_webhook_url="https://example.com/hook",
    )


def _closes(peak: float, latest: float, prefix_bars: int = 59) -> list[float]:
    """构造「长期横盘 → 单日脉冲到 peak → 回落到 latest」的收盘价序列。"""
    return [_FLAT] * prefix_bars + [peak, latest, latest]


def _make_engine(tmp_path) -> DataEngine:
    settings = _settings(tmp_path / "pullback.db")
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_ROWS, freq="D").strftime("%Y-%m-%d")

    shrink_volume = [1000.0] * (_ROWS - 5) + [100.0] * 5
    flat_volume = [1000.0] * _ROWS

    series = {
        _SELECTED: (_closes(20.0, 13.0), shrink_volume),
        _VOL_FLAT: (_closes(20.0, 13.0), flat_volume),
        _SHALLOW: (_closes(20.0, 17.0), shrink_volume),
        _TOO_DEEP: (_closes(30.0, 13.5), shrink_volume),
        _BELOW_MA60: ([20.0] * 3 + [13.0] * (_ROWS - 3), shrink_volume),
    }

    rows = []
    for symbol, (closes, volumes) in series.items():
        for day, close, volume in zip(dates, closes, volumes):
            rows.append((symbol, day, close, close, close, close, volume, 1e9))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def _strategy(engine: DataEngine) -> PullbackShrinkStrategy:
    return PullbackShrinkStrategy(engine=engine, settings=_settings(engine.db_path))


def test_selects_only_the_deep_shrunk_symbol(tmp_path) -> None:
    engine = _make_engine(tmp_path)

    assert _strategy(engine).run() == [_SELECTED]


def test_volume_must_shrink(tmp_path) -> None:
    """量能不收缩就不是「抛压衰竭」，不该入选。"""
    result = _strategy(_make_engine(tmp_path)).run()

    assert _SELECTED in result
    assert _VOL_FLAT not in result


def test_drop_band_bounds_are_both_enforced(tmp_path) -> None:
    """回落幅度是闭区间 [30%, 45%]：太浅（未跌够）与太深（接近崩盘）都排除。"""
    result = _strategy(_make_engine(tmp_path)).run()

    assert _SHALLOW not in result  # −15%，低于下限
    assert _TOO_DEEP not in result  # −55%，高于上限


def test_must_stay_above_ma60(tmp_path) -> None:
    """回调幅度达标但趋势已破，不算「强势股回调」。"""
    result = _strategy(_make_engine(tmp_path)).run()

    assert _BELOW_MA60 not in result


def test_shrink_threshold_is_enforced(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    strategy = _strategy(engine)
    strategy._VOL_SHRINK = 0.0  # 人为把门槛压到不可能满足

    assert strategy.run() == []


def test_injected_universe_is_respected(tmp_path) -> None:
    """注入池是精筛结果，策略自己选出的票也必须落在池内。"""
    engine = _make_engine(tmp_path)
    strategy = _strategy(engine)
    strategy.set_universe([_VOL_FLAT])  # 池里只有一只不合格的票

    assert strategy.run() == []


def test_empty_database_returns_empty(tmp_path) -> None:
    settings = _settings(tmp_path / "empty.db")
    engine = DataEngine(settings)

    assert PullbackShrinkStrategy(engine=engine, settings=settings).run() == []
