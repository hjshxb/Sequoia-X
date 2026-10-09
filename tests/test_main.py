"""主程序入口属性测试。"""

import sys
from unittest.mock import patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code != 0


def test_main_stops_without_report_or_push_when_data_source_unavailable(monkeypatch) -> None:
    """数据源不可用 ⇒ 非零退出，且不生成报告、不推送飞书。

    否则会拿上一交易日的旧数据照跑策略并推一张「看起来像今日结果」的卡片，
    用陈旧数据冒充当日选股结果 —— 比不出结果更糟。
    """
    import sequoia_x.data.engine as engine_module
    from sequoia_x.core.config import Settings

    class _DeadEngine:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def sync_today_bulk(self) -> int:
            raise engine_module.BaostockUnavailable("baostock 登录失败: 10001011 黑名单用户")

    pushed: list = []
    generated: list = []

    class _Notifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send_report(self, results: object, *args: object, **kwargs: object) -> None:
            pushed.append(results)

    class _Report:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def generate(self, *args: object, **kwargs: object) -> str:
            generated.append(args)
            return "reports/should_not_exist.html"

    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(_env_file=None, feishu_webhook_url="https://example.com/hook"),
    )
    monkeypatch.setattr(main_module, "DataEngine", _DeadEngine)
    monkeypatch.setattr(main_module, "FeishuNotifier", _Notifier)
    monkeypatch.setattr(main_module, "HtmlReportGenerator", _Report)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert pushed == [], "数据源不可用时不应推送"
    assert generated == [], "数据源不可用时不应生成报告"


def test_main_stops_without_report_or_push_when_sync_incomplete(monkeypatch) -> None:
    """同步结果不完整（未更新占比超阈值）⇒ 同样非零退出，不发卡片也不出报告。

    「一条都没拿到」与「只拿到一部分」都算数据未就绪：拿混合了新旧的库跑策略，
    榜单看起来很正常，却可能是用一半旧行情选出来的 —— 比直接不出结果更危险。
    """
    import sequoia_x.data.engine as engine_module
    from sequoia_x.core.config import Settings

    class _IncompleteEngine:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def sync_today_bulk(self) -> object:
            raise engine_module.SyncIncomplete(
                "5000 只待更新，其中 800 只未拿到新数据（16.0%，超过上限 5.0%）"
            )

    pushed: list = []
    generated: list = []

    class _Notifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send_report(self, results: object, *args: object, **kwargs: object) -> None:
            pushed.append(results)

    class _Report:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def generate(self, *args: object, **kwargs: object) -> str:
            generated.append(args)
            return "reports/should_not_exist.html"

    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(_env_file=None, feishu_webhook_url="https://example.com/hook"),
    )
    monkeypatch.setattr(main_module, "DataEngine", _IncompleteEngine)
    monkeypatch.setattr(main_module, "FeishuNotifier", _Notifier)
    monkeypatch.setattr(main_module, "HtmlReportGenerator", _Report)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert pushed == [], "数据不完整时不应推送"
    assert generated == [], "数据不完整时不应生成报告"


# ── 数据状态一行 ──

# main.py 里策略类**直接**引用的名字（`from ... import X` 后再实例化），
# 所以必须 patch `main` 模块上的属性，patch 策略模块本身不生效。
_STRATEGY_ATTRS = (
    "MaVolumeStrategy",
    "TurtleTradeStrategy",
    "HighTightFlagStrategy",
    "LimitUpShakeoutStrategy",
    "UptrendLimitDownStrategy",
    "RpsBreakoutStrategy",
    "PrivatePlacementStrategy",
)


class _StubUniverse:
    """精筛替身：完全不联网（真实实现会在 `fetch_metrics` 里登录 baostock）。"""

    enabled = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def describe(self, *, brief: bool = False) -> str:
        return "精筛：未启用"

    def fetch_metrics(self, symbols: list[str]) -> dict:
        return {}

    def cached_holder_ratios(self) -> dict:
        return {}


class _StubStrategy:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def set_universe(self, pool: object) -> None:
        pass

    def run(self) -> list[str]:
        return ["600000"]


def test_format_data_status_reports_date_and_completeness() -> None:
    """数据状态固定是「数据日期 · 更新情况」两段，缺日期时写「未知」而不是留空。"""
    from sequoia_x.data.engine import SyncStats

    assert (
        main_module._format_data_status(SyncStats(100, 100, 0, 100), "2026-09-24")
        == "2026-09-24 · 全市场 100 只已更新"
    )
    partial = main_module._format_data_status(SyncStats(100, 98, 0, 98), "2026-09-24")
    assert partial == "2026-09-24 · 更新 98 只，未更新 2 只（2.0%）"
    assert (
        main_module._format_data_status(SyncStats(0, 0, 0, 0), "2026-09-24")
        == "2026-09-24 · 无需更新（已是最新）"
    )
    assert main_module._format_data_status(SyncStats(0, 0, 0, 0), None).startswith("未知 · ")


def test_main_forwards_data_status_to_card_and_report(monkeypatch, tmp_path) -> None:
    """同步结果要变成一行「数据日期 + 更新情况」，同时出现在卡片与报告里。

    标题里的日期只是**运行日**，数据可能来自更早的交易日（非交易日重跑等）——
    这一行是读者判断榜单「能不能信」的唯一依据，不能只在报告里有、卡片里没有。
    """
    from sequoia_x.core.config import REQUIRED_HISTORY_DAYS, Settings
    from sequoia_x.data.engine import SyncStats

    gate_calls: list[str] = []

    class _HealthyEngine:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def sync_today_bulk(self) -> SyncStats:
            # 98/100：有 2 只停牌，在上限内，属正常
            return SyncStats(requested=100, updated=98, failed=0, rows=98)

        def get_market_latest_date(self) -> str:
            return "2026-09-24"

        def required_history_days(self) -> int:
            # 主流程会把它打进日志（让人一眼看到闸门查了多长的窗口）。
            # 真引擎的算法见 DataEngine.required_history_days；这里给等价的值即可。
            return REQUIRED_HISTORY_DAYS + 1

        def assert_recent_dates_complete(self, *args: object, **kwargs: object) -> None:
            # 「同步成功且未更新占比在上限内」在本替身里就代表库也完整，放行即可。
            # 库内完整性校验本身有 tests/test_data_engine.py 的专门用例。
            gate_calls.append("recent_dates")

        def assert_no_missing_trading_days(self, *args: object, **kwargs: object) -> None:
            # 同上：整日缺失的核对也有 test_data_engine.py 的专门用例。
            # 但**调用顺序有语义** —— 先按日行数、再对交易日历，两道都要在。
            gate_calls.append("no_missing_trading_days")

    pushes: list[dict] = []
    reports: list[dict] = []

    class _Notifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send_report(self, results: object, **kwargs: object) -> None:
            pushes.append(kwargs)

    class _Report:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def generate(self, results: object, **kwargs: object) -> str:
            reports.append(kwargs)
            return str(tmp_path / "report.html")

    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            feishu_webhook_url="https://example.com/hook",
            db_path=str(tmp_path / "t.db"),
        ),
    )
    monkeypatch.setattr(main_module, "DataEngine", _HealthyEngine)
    monkeypatch.setattr(main_module, "UniverseFilter", _StubUniverse)
    monkeypatch.setattr(main_module, "FeishuNotifier", _Notifier)
    monkeypatch.setattr(main_module, "HtmlReportGenerator", _Report)
    for attr in _STRATEGY_ATTRS:
        monkeypatch.setattr(main_module, attr, _StubStrategy)

    main_module.main()

    expected = "2026-09-24 · 更新 98 只，未更新 2 只（2.0%）"
    assert pushes, "有选股结果时应推送"
    assert reports, "应生成本地报告"
    assert pushes[0]["data_status"] == expected
    assert reports[0]["data_status"] == expected
    assert gate_calls == ["recent_dates", "no_missing_trading_days"], (
        "两道完整性闸门都必须在写库之后、跑策略之前被调用"
    )


# ── 板块 / 概念分布 ──


def _run_offline_main(monkeypatch, tmp_path) -> tuple[list[dict], list[dict]]:
    """跑一遍离线化的 main()，返回 (推送参数, 报告参数)。

    复用与上面用例相同的替身：引擎不联网、精筛不联网、策略固定返回一只、
    输出层只记录参数。板块/概念的取数由调用方按需 patch。
    """
    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import SyncStats

    class _HealthyEngine:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def sync_today_bulk(self) -> SyncStats:
            return SyncStats(requested=100, updated=100, failed=0, rows=100)

        def get_market_latest_date(self) -> str:
            return "2026-09-24"

        def required_history_days(self) -> int:
            return 251

        def assert_recent_dates_complete(self, *args: object, **kwargs: object) -> None:
            pass

        def assert_no_missing_trading_days(self, *args: object, **kwargs: object) -> None:
            pass

    pushes: list[dict] = []
    reports: list[dict] = []

    class _Notifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def send_report(self, results: object, **kwargs: object) -> None:
            pushes.append(kwargs)

    class _Report:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def generate(self, results: object, **kwargs: object) -> str:
            reports.append(kwargs)
            return str(tmp_path / "report.html")

    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            feishu_webhook_url="https://example.com/hook",
            db_path=str(tmp_path / "t.db"),
        ),
    )
    monkeypatch.setattr(main_module, "DataEngine", _HealthyEngine)
    monkeypatch.setattr(main_module, "UniverseFilter", _StubUniverse)
    monkeypatch.setattr(main_module, "FeishuNotifier", _Notifier)
    monkeypatch.setattr(main_module, "HtmlReportGenerator", _Report)
    for attr in _STRATEGY_ATTRS:
        monkeypatch.setattr(main_module, attr, _StubStrategy)

    main_module.main()
    return pushes, reports


def test_main_forwards_board_summary_to_card_and_report(monkeypatch, tmp_path) -> None:
    """板块/概念聚合要同时进卡片与报告 —— 只给报告，飞书上就看不到题材分布。"""
    from sequoia_x.data.board_concept import StockBoards

    seen: list[list[str]] = []

    def fake_load(symbols, **_kwargs):
        seen.append(list(symbols))
        return {"600000": StockBoards("600000", ("创新药",), "医药生物", "上海板块")}

    monkeypatch.setattr(main_module.board_concept, "load_board_concepts", fake_load)
    pushes, reports = _run_offline_main(monkeypatch, tmp_path)

    assert seen == [["600000"]], "应只查当日选出的股票，不是全市场"
    assert pushes[0]["board_summary"].concepts == (("创新药", 1),)
    assert reports[0]["board_summary"] is pushes[0]["board_summary"]


def test_main_survives_board_concept_failure(monkeypatch, tmp_path) -> None:
    """取数失败只降级（少一节），绝不能因此丢掉当天的报告与推送。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("东财连接被重置")

    monkeypatch.setattr(main_module.board_concept, "load_board_concepts", boom)
    pushes, reports = _run_offline_main(monkeypatch, tmp_path)

    assert pushes and reports, "板块取数失败不应阻断推送与报告"
    assert pushes[0]["board_summary"] is None
    assert reports[0]["board_summary"] is None
