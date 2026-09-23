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
from tests._score_factory import make_score

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


def test_card_shows_trailing_condition_despite_long_blacklist() -> None:
    """回归：行业黑名单很长时，排在它后面的筹码条件也必须出现在卡片上。

    实测踩过：`describe()` 完整版里 44 个行业关键词就把 120 字预算吃光了，
    卡片上的「筛股条件」以「…」结尾 —— 刚加的条件反而看不见。
    所以卡片改用 `describe(brief=True)`（长关键词表折叠成「等 N 项」）。
    """
    from sequoia_x.data.universe_filter import UniverseFilter

    settings = Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/default",
        min_market_cap=100,
        min_pe=0,
        min_turnover=5,
        min_turn=2,
        max_turn=20,
        exclude_industries=",".join(f"行业{i}" for i in range(44)),
        min_top10_free_holding=30,
        _env_file=None,
    )
    desc = UniverseFilter(settings=settings).describe(brief=True).removeprefix("精筛：")

    notifier = FeishuNotifier(settings)
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["600000"]}, filter_desc=desc))

    assert "前十大流通股东 >=30%" in text
    assert "…" not in text, "brief 描述不应再触发截断"


def test_card_shows_summary_counts() -> None:
    """卡片要有命中策略数与选股总数，方便一眼看清。"""
    notifier = FeishuNotifier(make_settings())
    results = {"MaVolumeStrategy": ["600000", "600601"], "TurtleTradeStrategy": []}
    text = card_text(posted_card(notifier, results))

    assert "1 / 2" in text  # 命中 1 个策略 / 共 2 个
    assert "2" in text  # 选股总数


# ── 综合评分榜（评分 + 胜率，单一栏目）──
# 卡片只推**一个**「🎯 综合评分 Top 10」栏目。过去拆成「量化评分 Top 5」+
# 「形态匹配 Top 5」两个榜：两个视角的名单经常不一致甚至反向（高分股胜率为 0、
# 低分股胜率很高），摆在一起只会让人问「信哪个」。现在统一按
# `scorer.composite_score`（评分 ×0.7 + 胜率按可信度收缩后 ×0.3）排序，
# 排序口径只有一处真源，展示层不再各写一个 key。


def composite_section(text: str) -> str:
    """截出「综合评分 Top N」小节，便于只在该节内部断言顺序。"""
    assert "综合评分 Top" in text, "卡片里没有综合评分小节"
    return text.split("综合评分 Top", 1)[1]


def test_card_without_scores_has_no_composite_section() -> None:
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"TurtleTradeStrategy": ["600000"]}))

    assert "综合评分 Top" not in text
    assert "量化评分 Top" not in text  # 旧的两个榜已合并，不应残留
    assert "形态匹配 Top" not in text


def test_card_shows_composite_section_with_scores() -> None:
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("300750", 86.0, tags="TR"), make_score("600000", 72.5)]
    text = card_text(
        posted_card(notifier, {"TurtleTradeStrategy": ["600000", "300750"]}, scores=scores)
    )

    assert "🎯 综合评分 Top 2" in text
    assert "**1.**" in text and "**2.**" in text
    # 整批都没有胜率时，综合分退化为评分：两个数应当相同
    assert "**综合 86.0**" in text and "**综合 72.5**" in text
    assert "评分 86.0" in text and "评分 72.5" in text
    assert "`TR`" in text  # 命中的策略标记
    assert "xueqiu.com/S/SZ300750" in text  # 榜内同样是可点链接


def test_card_composite_section_is_capped_at_ten() -> None:
    """卡片元素数有限，综合榜只放前 10 名；完整排行在本地 HTML 报告里。"""
    notifier = FeishuNotifier(make_settings())
    symbols = [f"{i:06d}" for i in range(12)]
    scores = [make_score(s, 90.0 - i) for i, s in enumerate(symbols)]
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": symbols}, scores=scores))

    assert "🎯 综合评分 Top 10" in text
    assert "**11.**" not in composite_section(text)


def test_card_composite_blends_winrate_into_score() -> None:
    """高胜率能把评分略低的票抬上来 —— 这就是「结合胜率和分数」的含义。"""
    notifier = FeishuNotifier(make_settings())
    scores = [
        make_score("600000", 80.0, prob_up=20.0, prob_samples=5, prob_confidence=100.0),
        make_score("600601", 70.0, prob_up=80.0, prob_samples=5, prob_confidence=100.0),
    ]
    text = card_text(
        posted_card(notifier, {"MaVolumeStrategy": ["600000", "600601"]}, scores=scores)
    )

    # 600000：0.7×80 + 0.3×20 = 62.0；600601：0.7×70 + 0.3×80 = 73.0
    assert "**综合 73.0**" in text and "**综合 62.0**" in text
    section = composite_section(text)
    assert section.index("600601") < section.index("600000")
    assert "胜率 1/5" in section and "胜率 4/5" in section


def test_card_composite_shrinks_low_confidence_winrate() -> None:
    """「1/1 = 100%」这类低可信度胜率不能主导排序 —— 按可信度向 50% 收缩。

    若直接把胜率当权重，600000 会算出 0.7×70 + 0.3×100 = 79.0 直接夺榜首；
    收缩后有效胜率只有 50 + 50×0.2 = 60，综合 67.0，输给更稳的 600601。
    """
    notifier = FeishuNotifier(make_settings())
    scores = [
        make_score("600000", 70.0, prob_up=100.0, prob_samples=1, prob_confidence=20.0),
        make_score("600601", 75.0, prob_up=60.0, prob_samples=5, prob_confidence=100.0),
    ]
    text = card_text(
        posted_card(notifier, {"MaVolumeStrategy": ["600000", "600601"]}, scores=scores)
    )

    section = composite_section(text)
    assert section.index("600601") < section.index("600000")
    assert "**综合 70.5**" in section and "**综合 67.0**" in section
    assert "**综合 79.0**" not in section


def test_card_composite_shows_winrate_k_over_n() -> None:
    """有窗口数时胜率写成 `k/n`：单看「100%」看不出是 1 次还是 4 次。"""
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("300750", 70.0, prob_up=75.0, prob_samples=4, prob_confidence=50.0)]
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["300750"]}, scores=scores))

    assert "胜率 3/4" in text


def test_card_composite_k_over_n_rounds() -> None:
    """`prob_up` 是四舍五入过的百分比，回算 k 时会带上零点几，必须取整。"""
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("300750", 70.0, prob_up=66.7, prob_samples=3, prob_confidence=50.0)]
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["300750"]}, scores=scores))

    assert "胜率 2/3" in text


def test_card_composite_omits_winrate_when_missing() -> None:
    """没算出胜率的票照样进榜（综合分退化为评分），只是该行不显示胜率。

    不能像旧「形态匹配榜」那样把它整只剔掉 —— 那会让人误以为这只票没被评分。
    """
    notifier = FeishuNotifier(make_settings())
    scores = [
        make_score("600000", 95.0),  # 无胜率
        make_score("600601", 50.0, prob_up=42.0, prob_samples=5, prob_confidence=10.0),
    ]
    text = card_text(
        posted_card(notifier, {"MaVolumeStrategy": ["600000", "600601"]}, scores=scores)
    )

    section = composite_section(text)
    assert "600000" in section and "600601" in section
    # 只看行内（标题里也带「胜率」二字）：只有 600601 那一行有胜率字段
    assert section.count("· 胜率 ") == 1


def test_card_composite_tie_breaks_by_score_then_symbol() -> None:
    """综合分并列时按评分、再按代码升序 —— 同样输入恒定产出同样卡片。"""
    notifier = FeishuNotifier(make_settings())
    scores = [
        make_score("600000", 80.0, prob_up=50.0, prob_samples=5, prob_confidence=100.0),
        make_score("600601", 80.0, prob_up=50.0, prob_samples=5, prob_confidence=100.0),
    ]
    text = card_text(
        posted_card(notifier, {"MaVolumeStrategy": ["600000", "600601"]}, scores=scores)
    )

    section = composite_section(text)
    assert section.index("600000") < section.index("600601")


def test_card_composite_links_to_xueqiu() -> None:
    """综合榜里的票要可点跳雪球，并带股票名称。"""
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("300750", 70.0, prob_up=60.0, prob_samples=5, prob_confidence=40.0)]
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["300750"]}, scores=scores))

    section = composite_section(text)
    assert "https://xueqiu.com/S/SZ300750" in section
    assert "宁德时代" in section


def test_card_sorts_strategy_rows_by_score_inside_board() -> None:
    """策略小节内部按综合分名次排列（板块分组保留）。

    这里两只票都没有胜率，综合分 == 评分，所以断言等价于「按评分降序」；
    有胜率时的排序口径由 `scorer.composite_score` 单测覆盖。
    """
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("600601", 90.0), make_score("600000", 60.0)]
    text = card_text(
        posted_card(notifier, {"TurtleTradeStrategy": ["600000", "600601"]}, scores=scores)
    )

    board_line = text.split(f"{BOARD_MAIN} 2：", 1)[1]
    assert board_line.index("600601") < board_line.index("600000")


def test_card_keeps_code_order_without_scores() -> None:
    notifier = FeishuNotifier(make_settings())
    text = card_text(posted_card(notifier, {"TurtleTradeStrategy": ["600601", "600000"]}))

    board_line = text.split(f"{BOARD_MAIN} 2：", 1)[1]
    assert board_line.index("600000") < board_line.index("600601")


def test_card_composite_handles_missing_name() -> None:
    """名称取不到时只显示代码，但链接与名次仍在。"""
    notifier = FeishuNotifier(make_settings())
    scores = [make_score("999999", 61.0)]
    text = card_text(posted_card(notifier, {"MaVolumeStrategy": ["999999"]}, scores=scores))

    assert "**1.** [999999](https://xueqiu.com/S/SZ999999) · **综合 61.0** · 评分 61.0" in text


# 旧的「📈 形态匹配 Top 5」单独栏目已并入「🎯 综合评分 Top 10」，
# 相关用例（按可信度排序 / k over n / 缺可信度排最后 等）已由上面的
# 综合榜用例覆盖；胜率的排序语义现在住在 `scorer.composite_score` 里，
# 由 `tests/test_scorer.py` 覆盖。
