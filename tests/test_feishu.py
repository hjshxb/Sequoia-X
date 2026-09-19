"""飞书通知属性测试。

覆盖：
    - 合并卡片包含全部策略与全部股票代码（属性 10）
    - 推送目标 URL 等于 config 中的 webhook（属性 11）
    - HTTP 失败记 ERROR 且不抛异常（属性 12）
    - 卡片格式：中文策略名 / 按板块分组排序 / 只有代码+名称（无外链）
    - 无结果的策略收成一行、全空时给出提示

所有用例都通过桩数据屏蔽 baostock，**不联网**。
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

import sequoia_x.notify.feishu as feishu_module
from sequoia_x.core.config import Settings
from sequoia_x.data.stock_meta import BOARD_CHINEXT, BOARD_MAIN, BOARD_STAR, StockMeta
from sequoia_x.notify.feishu import FeishuNotifier

# 桩数据：覆盖 主板 / 创业板 / 科创板 / 北交所，其中一只有行业、一只无行业
_FAKE_META = {
    "600000": StockMeta("600000", "浦发银行", "货币金融服务"),
    "600601": StockMeta("600601", "方正科技", "计算机、通信和其他电子设备制造业"),
    "300750": StockMeta("300750", "宁德时代", "电气机械和器材制造业"),
    "688981": StockMeta("688981", "中芯国际", "计算机、通信和其他电子设备制造业"),
    "830799": StockMeta("830799", "艾融软件", None),
}


def make_settings(webhook_url: str = "https://example.com/default") -> Settings:
    """构造测试用 Settings（`_env_file=None` 屏蔽仓库真实 .env）。"""
    return Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url=webhook_url,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
def _stub_meta(monkeypatch):
    """屏蔽 baostock：股票名称走桩数据，保证测试离线。"""
    monkeypatch.setattr(
        feishu_module.stock_meta_module, "load_stock_meta", lambda: dict(_FAKE_META)
    )


def posted_card(notifier: FeishuNotifier, results: dict, **kwargs) -> dict:
    """跑一遍 send_report，返回实际 POST 出去的卡片体。"""
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        mock_post.return_value.json.return_value = {"code": 0}
        notifier.send_report(results, **kwargs)
    raw = mock_post.call_args.kwargs.get("data")
    assert raw, "send_report 没有发出请求体"
    return json.loads(raw)


def card_text(card: dict) -> str:
    """把整张卡片序列化成字符串，便于做包含性断言。"""
    return json.dumps(card, ensure_ascii=False)


# ── 属性测试（沿用原 Property 10 / 11 / 12 的语义）──


# Feature: sequoia-x-v2, Property 10: 飞书通知包含所有选股结果
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
@h_settings(max_examples=50, deadline=None)
def test_notification_contains_all_symbols(symbols: list[str]) -> None:
    """属性 10：推送内容应包含所有 symbol，且不因缺名称而丢。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": symbols}))
    for symbol in symbols:
        assert symbol in text


# Feature: sequoia-x-v2, Property 11: 飞书通知使用 ConfigManager 中的 Webhook URL
@given(
    webhook_url=st.from_regex(
        r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[a-z0-9\-]{8,36}", fullmatch=True
    )
)
@h_settings(max_examples=50, deadline=None)
def test_notification_uses_config_url(webhook_url: str) -> None:
    """属性 11：请求目标 URL 应等于 settings.feishu_webhook_url。"""
    notifier = FeishuNotifier(make_settings(webhook_url=webhook_url))

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.json.return_value = {"code": 0}
        notifier.send_report({"MaVolumeStrategy": ["600000"]}, webhook_key="default")

    assert mock_post.call_args.args[0] == webhook_url


# Feature: sequoia-x-v2, Property 12: HTTP 失败时记录 ERROR 日志
@given(status_code=st.integers(min_value=400, max_value=599))
@h_settings(max_examples=50, deadline=None)
def test_http_failure_logs_error(status_code: int) -> None:
    """属性 12：非 200 响应时应记 ERROR，且不抛异常。"""
    notifier = FeishuNotifier(make_settings())

    feishu_logger = logging.getLogger(feishu_module.__name__)
    log_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    handler = _ListHandler(logging.ERROR)
    feishu_logger.addHandler(handler)
    try:
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=status_code, text="error")
            mock_post.return_value.json.return_value = {"code": 99999}
            notifier.send_report({"MaVolumeStrategy": ["600000"]})
    finally:
        feishu_logger.removeHandler(handler)

    assert any(r.levelno == logging.ERROR for r in log_records)


# ── 卡片格式 ──


def test_card_uses_chinese_strategy_labels() -> None:
    """策略名必须用中文展示，不能出现英文类名。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["600000"]}))

    assert "均线放量" in text
    assert "MaVolumeStrategy" not in text


def test_card_lists_every_strategy_label_for_non_empty_results() -> None:
    """每个有结果的策略都要出现，且用其中文名。"""
    notifier = FeishuNotifier(make_settings())
    results = {
        "MaVolumeStrategy": ["600000"],
        "TurtleTradeStrategy": ["600601"],
        "RpsBreakoutStrategy": ["300750"],
    }
    text = card_text(posted_card(notifier, results))

    for label in ("均线放量", "海龟突破", "RPS 突破"):
        assert label in text


def test_card_groups_stocks_by_board_order() -> None:
    """同一策略下按 主板 → 创业板 → 科创板 → 北交所 排序分组。"""
    notifier = FeishuNotifier(make_settings())
    # 故意乱序传入，验证排序由卡片自己完成
    results = {"MaVolumeStrategy": ["830799", "688981", "300750", "600000"]}
    text = card_text(posted_card(notifier, results))

    i_main = text.index(BOARD_MAIN)
    i_chinext = text.index(BOARD_CHINEXT)
    i_star = text.index(BOARD_STAR)
    i_bse = text.index("北交所")
    assert i_main < i_chinext < i_star < i_bse


def test_card_shows_code_and_name_only() -> None:
    """展示内容只有 代码 + 名称（不带行业/市值等列）。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["600000", "600601"]}))

    assert "600000" in text and "浦发银行" in text
    assert "600601" in text and "方正科技" in text


def test_card_links_every_stock_to_xueqiu() -> None:
    """每只股票都要挂雪球链接，保持「点代码跳行情」的老习惯。

    链接的可见文本仍是 `代码 名称`，所以卡片不会变长。
    交易所前缀规则与 HTML 报告共用（6→SH，4/8→BJ，其余→SZ）。
    """
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["600000", "300750", "830799"]}))

    assert "https://xueqiu.com/S/SH600000" in text
    assert "https://xueqiu.com/S/SZ300750" in text
    assert "https://xueqiu.com/S/BJ830799" in text
    # 可见文本仍是 代码 + 名称
    assert "浦发银行" in text and "宁德时代" in text and "艾融软件" in text


def test_card_falls_back_to_code_when_name_missing() -> None:
    """名称缺失时仍要出现代码，不能整行丢失。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["123456"]}))

    assert "123456" in text


def test_card_collapses_empty_strategies_into_one_line() -> None:
    """无结果的策略不单独成段，收成一行，且只出现一次。"""
    notifier = FeishuNotifier(make_settings())
    results = {"MaVolumeStrategy": ["600000"], "TurtleTradeStrategy": []}
    text = card_text(posted_card(notifier, results))

    assert "海龟突破" in text
    assert text.count("海龟突破") == 1
    assert "本次无结果" in text
    # 有结果的策略仍要正常成段
    assert "均线放量" in text


def test_card_all_empty_shows_hint() -> None:
    """全部策略无结果时给出明确提示。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": [], "TurtleTradeStrategy": []}))

    assert "均无选股结果" in text


def test_card_includes_filter_description() -> None:
    """卡片要带精筛条件，便于判断结果为何偏少。"""
    notifier = FeishuNotifier(make_settings())
    text = card_text(
        posted_card(notifier, {"MaVolumeStrategy": ["600000"]}, filter_desc="成交额 >=5亿")
    )

    assert "成交额 >=5亿" in text


def test_card_elides_overlong_filter_description() -> None:
    """精筛条件过长时必须截断，否则行业黑名单会把卡片撑爆。

    阈值项（市值/PE/成交额/换手）排在描述前面，截断后仍能看到；
    被截掉的是尾部的行业黑名单，属于可以牺牲的信息。
    """
    notifier = FeishuNotifier(make_settings())
    long_desc = "成交额 >=5亿，排除行业 " + "、".join(f"行业{i}" for i in range(200))
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["600000"]}, filter_desc=long_desc))

    assert "成交额 >=5亿" in text
    assert "行业199" not in text
    assert "…" in text


def test_card_shows_summary_counts() -> None:
    """卡片要有命中策略数与选股总数，方便一眼看清。"""
    notifier = FeishuNotifier(make_settings())
    results = {"MaVolumeStrategy": ["600000", "600601"], "TurtleTradeStrategy": []}
    text = card_text(posted_card(notifier, results))

    assert "1 / 2" in text  # 命中 1 个策略 / 共 2 个
    assert "2" in text  # 选股总数
