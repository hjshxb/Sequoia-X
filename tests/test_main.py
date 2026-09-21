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
