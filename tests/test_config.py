"""配置管理属性测试。"""

import os
import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st
from pydantic import ValidationError


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(db_path=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-")))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(cfg_module, "_settings", None)
    from sequoia_x.core.config import Settings
    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2: 同步并发进程数可配置、默认单进程
def test_sync_workers_defaults_to_single_process() -> None:
    """默认必须为单进程 —— 并发登录会触发 baostock 风控拉黑。"""
    from sequoia_x.core.config import Settings

    s = Settings(_env_file=None, feishu_webhook_url="https://example.com/hook")
    assert s.sync_workers == 1


def test_sync_workers_can_be_overridden(monkeypatch) -> None:
    """可通过环境变量手动指定并发进程数。"""
    import sequoia_x.core.config as cfg_module
    from sequoia_x.core.config import Settings

    monkeypatch.setenv("SYNC_WORKERS", "4")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(cfg_module, "_settings", None)
    assert Settings().sync_workers == 4


def test_sync_workers_blank_falls_back_to_default() -> None:
    """`.env` 里 `SYNC_WORKERS=` 留空是常见写法，应回落到默认 1 而不是报错。"""
    from sequoia_x.core.config import Settings

    s = Settings(_env_file=None, feishu_webhook_url="https://example.com/hook", sync_workers="")
    assert s.sync_workers == 1


@pytest.mark.parametrize("bad", [0, -1, 33, 999])
def test_sync_workers_rejects_out_of_range(bad: int) -> None:
    """0/负数无意义；过大极易触发风控 —— 非法值应在启动时直接报错。"""
    from sequoia_x.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            feishu_webhook_url="https://example.com/hook",
            sync_workers=bad,
        )


def test_ma_window_defaults_to_120() -> None:
    """均线窗口默认 120 个交易日。"""
    from sequoia_x.core.config import Settings

    s = Settings(_env_file=None, feishu_webhook_url="https://example.com/hook")
    assert s.ma_window == 120
    assert s.min_ma_deviation is None


def test_ma_window_blank_falls_back_to_default() -> None:
    """`.env` 里 `MA_WINDOW=` 留空视为未配置，回落 120。"""
    from sequoia_x.core.config import Settings

    s = Settings(_env_file=None, feishu_webhook_url="https://example.com/hook", ma_window="")
    assert s.ma_window == 120


def test_min_ma_deviation_blank_becomes_none() -> None:
    """`MIN_MA_DEVIATION=` 留空 = 该维度不参与过滤。"""
    from sequoia_x.core.config import Settings

    s = Settings(
        _env_file=None,
        feishu_webhook_url="https://example.com/hook",
        min_ma_deviation="  ",
    )
    assert s.min_ma_deviation is None


@pytest.mark.parametrize("bad", [0, 1, -5, 501, 9999])
def test_ma_window_rejects_out_of_range(bad: int) -> None:
    """窗口过小没有意义，过大则超出合理的行情历史长度 —— 启动即报错。"""
    from sequoia_x.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            feishu_webhook_url="https://example.com/hook",
            ma_window=bad,
        )


def test_missing_required_field_raises() -> None:
    """属性 2：缺少 feishu_webhook_url 时，实例化 Settings 应抛出 ValidationError。"""
    import os
    from sequoia_x.core.config import Settings
    # 确保环境变量中没有该字段
    env_backup = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "feishu_webhook_url" in str(exc_info.value).lower()
    finally:
        if env_backup is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = env_backup
