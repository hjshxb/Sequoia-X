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


# ── RPS 是横截面指标：精筛池对它必须完全无效（连最后一步交集也不做）──

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
    """精筛池对 RPS 必须完全无效（全系统只有 RPS 是横截面指标）。

    其余策略都是逐股纵向指标，先精筛再算与「全市场算完再精筛」必然等价，
    前置精筛对它们只是省算力。RPS 不一样：排名基数一旦缩到池内，每只票的
    百分位会重排，语义就变了；而且「全市场强势股 ∩ 精筛池」会把池外的真·强势股
    整批漏掉（2026-09-22 实测 71 只 → 25 只，且是前者的真子集）。
    所以 `applies_universe_filter = False` —— 候选范围与最终结果都走全市场。
    这两条测试把该语义钉住。
    """

    def test_pool_does_not_change_rps_result(self, tmp_path) -> None:
        """★ 核心回归：注入精筛池不得改变 RPS 的结果。

        技巧：先把**全市场**当池子注入，`apply_universe_filter` 若真生效会退化为
        恒真，于是拿到的是策略自己算出的纯全市场候选集（无需在测试里重写策略逻辑，
        否则测的就只是「我的复刻对不对」）。再拿一个真子集当池子跑一次，
        两次结果必须逐元素相同 —— 不同就说明池子又在「预选筛出」了。
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

        assert pooled.run() == candidates

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
