"""日志系统属性测试。"""

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 3: get_logger 同名返回同一实例
@given(name=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="._")))
@h_settings(max_examples=100)
def test_get_logger_same_instance(name: str) -> None:
    """属性 3：对任意 name，多次调用 get_logger(name) 应返回同一 Logger 实例。"""
    from sequoia_x.core.logger import get_logger

    # 名字统一加前缀：hypothesis 的示例库里可能存着与**真实模块同名**的样例
    # （例如 `backfill_dates` —— `scripts/backfill_dates.py` 在 import 时就会
    # `get_logger("backfill_dates")`）。撞上它时该 logger 早已被 pytest 的日志
    # 插件挂上 caplog 的 handler，handler 数自然不是 1，断言就会偶发变红。
    # 那是**测试自身的命名冲突**，不是 get_logger 的缺陷，所以这里换命名空间。
    name = "test_logger_" + name

    logger1 = get_logger(name)
    logger2 = get_logger(name)
    assert logger1 is logger2
    # 确保 handler 没有被重复添加
    assert len(logger1.handlers) == 1
