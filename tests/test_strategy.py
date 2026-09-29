"""策略引擎属性测试。"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy


# Feature: sequoia-x-v2, Property 9: 策略 run() 返回值类型正确
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=0, max_size=3, unique=True,
    )
)
@h_settings(max_examples=30, deadline=None)
def test_strategy_run_returns_list_of_str(symbols: list[str]) -> None:
    """属性 9：run() 应返回 list[str]，每个元素为非空字符串。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = Settings(
            db_path=str(Path(tmp_dir) / "test.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/hook",
        )
        engine = DataEngine(settings)

        with patch.object(engine, "get_all_symbols", return_value=symbols):
            with patch.object(engine, "get_ohlcv", return_value=pd.DataFrame()):
                strategy = MaVolumeStrategy(engine=engine, settings=settings)
                result = strategy.run()

    assert isinstance(result, list)
    assert all(isinstance(s, str) and len(s) > 0 for s in result)


# ── RPS 是横截面指标：计算必须全市场，但结果照常过精筛 ──

_RPS_ROWS = 130
_RPS_BASE = 10.0


def _rps_symbol(i: int) -> str:
    return f"0000{i:02d}"


def make_rps_engine(tmp_path, n: int = 30) -> DataEngine:
    """建一个假行情库：第 i 只股票的 120 交易日涨幅恰好是 i%。

    前 121 根 K 线恒定、最后 9 根线性上行 —— 于是 shift(120) 取到的基准价
    正好是常量 BASE，涨幅完全可控。high = close × 1.05 使 roll_high 等于当日
    最高价，「突破确认」恒成立，从而让 RPS 成为唯一变量。
    """
    settings = Settings(
        db_path=str(tmp_path / "rps.db"),
        start_date="2026-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    dates = pd.date_range("2026-01-01", periods=_RPS_ROWS, freq="D").strftime("%Y-%m-%d")

    rows = []
    for i in range(n):
        tail = [_RPS_BASE * (1 + (i / 100.0) * k / 9) for k in range(1, 10)]
        closes = [_RPS_BASE] * (_RPS_ROWS - 9) + tail
        for day, close in zip(dates, closes):
            high = close * 1.05
            rows.append((_rps_symbol(i), day, close, high, close, close, 1.0, 1.0))

    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            "INSERT INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return engine


def make_rps_settings(db_path: str) -> Settings:
    return Settings(
        db_path=db_path,
        start_date="2026-01-01",
        feishu_webhook_url="https://example.com/hook",
    )


class TestRpsUniverseInteraction:
    """RPS 的两段口径：**计算走全市场，结果过精筛**（全系统只有它一个横截面指标）。

    其余策略都是逐股纵向指标，先精筛再算与「全市场算完再精筛」必然等价，前置精筛
    对它们只是省算力。RPS 不一样：它必须先在**全市场**排名算百分位（基数一缩到
    池内，每只票的 RPS 就重排了），但算完之后与其他策略完全一样，要过
    `apply_universe_filter()` 这道精筛 —— 当日强势不等于符合选股条件。

    这几条测试把两个方向都钉住：池**不能**改变排名基数，但**必须**裁剪结果。
    """

    def test_pool_trims_result_without_changing_ranking(self, tmp_path) -> None:
        """★ 核心回归：池只裁剪结果，绝不参与排名。

        技巧：先把**全市场**当池子注入，`apply_universe_filter` 退化为恒真，于是拿到
        策略自己算出的纯全市场候选集（无需在测试里复刻策略逻辑，否则测的只是「我的
        复刻对不对」）。再拿一个真子集当池子跑一次，结果必须恰好等于「候选 ∩ 池」。

        两个方向都会变红：退化成池内排名 ⇒ 池内的 000003 会因「池内第一」而入选；
        结果不套精筛 ⇒ 池外的 000027/000029 会留在结果里。
        """
        engine = make_rps_engine(tmp_path)
        settings = make_rps_settings(engine.db_path)
        all_symbols = engine.get_local_symbols()

        full = RpsBreakoutStrategy(engine=engine, settings=settings)
        full.set_universe(all_symbols)
        candidates = full.run()
        assert candidates, "构造的数据应当能选出全市场强势股"

        pool = [_rps_symbol(3), _rps_symbol(26), _rps_symbol(28)]
        pooled = RpsBreakoutStrategy(engine=engine, settings=settings)
        pooled.set_universe(pool)

        assert pooled.run() == [s for s in candidates if s in set(pool)]

    def test_full_market_strong_but_outside_pool_is_excluded(self, tmp_path) -> None:
        """池外的全市场强势股不进最终榜单 —— 「结果过精筛」的最直接断言。

        池里只放一只中等涨幅股（000010，全市场 RPS 约 36.7%），它够不到 90 的门槛，
        所以候选为空；而全市场算出的 000026~000029 四只全都不在池内，被精筛剔掉。
        「结果不套精筛」的实现会返回那 4 只 ⇒ 本测试失败。
        """
        engine = make_rps_engine(tmp_path)
        strategy = RpsBreakoutStrategy(
            engine=engine, settings=make_rps_settings(engine.db_path)
        )
        strategy.set_universe([_rps_symbol(10)])

        assert strategy.run() == []

    def test_pool_member_without_full_market_strength_is_not_selected(
        self, tmp_path
    ) -> None:
        """池内的「全市场根本不算强势」的股票不得入选。

        30 只股票涨幅 0%~29% ⇒ 全市场 RPS >= 90 只对应涨幅最高的 4 只
        （000026~000029）。池里只放两只中等涨幅股：000005 涨 5%、000010 涨 10%，
        它们在全市场的 RPS 分别只有约 20% 和 36.7%，都够不到 90。
        若实现退化成池内排名，000010 会在池内排第一（RPS = 100）被选出。
        """
        engine = make_rps_engine(tmp_path)
        strategy = RpsBreakoutStrategy(
            engine=engine, settings=make_rps_settings(engine.db_path)
        )
        strategy.set_universe([_rps_symbol(5), _rps_symbol(10)])

        selected = strategy.run()
        assert _rps_symbol(5) not in selected
        assert _rps_symbol(10) not in selected
