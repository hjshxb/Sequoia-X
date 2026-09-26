"""HTML 选股报告模块单元测试。

覆盖点：
    - 雪球代码转换与行业前缀清洗
    - 上市板块判定与板块分组排序
    - 报告按策略分块渲染，包含代码 / 名称 / 板块 / 行业
    - 传入 metrics 时额外渲染 市值 / 换手率 / PE 三列
    - 缺失名称或行业的降级展示
    - HTML 转义（防止股票名称等外部数据破坏页面结构）
    - 空结果时仍能生成可打开的报告
    - 输出路径与目录自动创建
"""

import sys

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import (
    BOARD_BSE,
    BOARD_CHINEXT,
    BOARD_MAIN,
    BOARD_OTHER,
    BOARD_STAR,
    StockMeta,
    board_of,
    board_rank,
    clean_industry,
)
from sequoia_x.data.universe_filter import StockMetric
from sequoia_x.notify.html_report import (
    HtmlReportGenerator,
    build_symbol_marks,
    group_by_board,
    to_xueqiu_code,
)
from tests._score_factory import make_score

_FAKE_META = {
    "600000": StockMeta("600000", "浦发银行", "货币金融服务"),
    "000001": StockMeta("000001", "平安银行", "货币金融服务"),
    "300750": StockMeta("300750", "宁德时代", "电气机械和器材制造业"),
}


@pytest.fixture(autouse=True)
def _clear_meta_cache():
    stock_meta_module.reset_cache()
    yield
    stock_meta_module.reset_cache()


@pytest.fixture
def gen(tmp_path, monkeypatch) -> HtmlReportGenerator:
    """构造一个使用临时目录、且不访问网络的报告生成器。"""
    monkeypatch.setattr(stock_meta_module, "load_stock_meta", lambda: dict(_FAKE_META))
    settings = Settings(
        feishu_webhook_url="https://example.com/hook",
        report_dir=str(tmp_path / "reports"),
        _env_file=None,  # 与仓库根目录的 .env 隔离，避免真实配置渗入
    )
    return HtmlReportGenerator(settings)


# ── 工具函数 ──


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600000", "SH600000"),
        ("601398", "SH601398"),
        ("000001", "SZ000001"),
        ("300750", "SZ300750"),
        ("830799", "BJ830799"),
        ("430047", "BJ430047"),
    ],
)
def test_to_xueqiu_code(symbol, expected):
    assert to_xueqiu_code(symbol) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("J66货币金融服务", "货币金融服务"),
        ("C39计算机、通信和其他电子设备制造业", "计算机、通信和其他电子设备制造业"),
        ("G56航空运输业", "航空运输业"),
        ("", ""),
        ("   ", ""),
        ("无前缀行业", "无前缀行业"),
    ],
)
def test_clean_industry(raw, expected):
    assert clean_industry(raw) == expected


# ── 路径 ──


def test_default_path_uses_report_dir(gen):
    path = gen.default_path()
    assert path.parent.name == "reports"
    assert path.name.startswith("stock_report_")
    assert path.suffix == ".html"


# ── 渲染 ──


def test_report_contains_code_name_industry(gen, tmp_path):
    out = gen.generate({"TurtleTradeStrategy": ["600000", "000001"]})

    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "600000" in text and "浦发银行" in text and "货币金融服务" in text
    assert "000001" in text and "平安银行" in text


def test_report_uses_chinese_strategy_label(gen):
    out = gen.generate({"TurtleTradeStrategy": ["600000"]})
    text = out.read_text(encoding="utf-8")
    assert "海龟突破" in text
    # 不应把原始类名直接暴露在标题里
    assert "<h2>TurtleTradeStrategy</h2>" not in text


def test_unknown_strategy_falls_back_to_classname(gen):
    out = gen.generate({"BrandNewStrategy": ["600000"]})
    assert "BrandNewStrategy" in out.read_text(encoding="utf-8")


def test_report_is_self_contained_single_file(gen):
    """单文件：不得引用任何外部 CSS/JS/图片资源。"""
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert "<link" not in text
    assert "<script src=" not in text
    assert "http://" not in text.replace("http://www.w3.org", "")  # 排除 svg 命名空间
    # 雪球外链是正常业务链接
    assert "https://xueqiu.com/S/SH600000" in text


def test_report_shows_all_strategies_even_when_empty(gen):
    out = gen.generate(
        {
            "TurtleTradeStrategy": ["600000"],
            "MaVolumeStrategy": [],
        }
    )
    text = out.read_text(encoding="utf-8")
    assert "海龟突破" in text
    assert "均线放量" in text
    assert "该策略本次无选股结果" in text


def test_report_when_everything_is_empty(gen):
    out = gen.generate({"TurtleTradeStrategy": [], "MaVolumeStrategy": []})
    text = out.read_text(encoding="utf-8")
    assert "所有策略均无选股结果" in text
    assert out.stat().st_size > 0


def test_missing_meta_renders_placeholder(gen):
    """名称/行业取不到时应展示占位符，而不是崩溃或留空。"""
    out = gen.generate({"TurtleTradeStrategy": ["999999"]})
    text = out.read_text(encoding="utf-8")
    assert "999999" in text
    assert "—" in text


def test_report_escapes_untrusted_text(gen, monkeypatch):
    """股票名称属于外部数据，必须转义后才能写入 HTML。"""
    evil = {
        "600000": StockMeta("600000", '<script>alert("x")</script>', "<b>行业</b>"),
    }
    monkeypatch.setattr(stock_meta_module, "load_stock_meta", lambda: evil)

    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text
    assert "<b>行业</b>" not in text
    assert "&lt;b&gt;" in text


def test_report_includes_filter_description(gen):
    out = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        filter_desc="基本面过滤：流通市值 >=100亿，市盈率TTM >=0",
    )
    text = out.read_text(encoding="utf-8")
    assert "流通市值 &gt;=100亿" in text or "流通市值 >=100亿" in text


def test_custom_output_path(gen, tmp_path):
    target = tmp_path / "nested" / "deep" / "my_report.html"
    out = gen.generate({"TurtleTradeStrategy": ["600000"]}, output_path=target)

    assert out == target
    assert target.exists()  # 父目录应被自动创建


def test_totals_and_counts_in_header(gen):
    out = gen.generate(
        {"TurtleTradeStrategy": ["600000", "000001"], "MaVolumeStrategy": ["300750"]}
    )
    text = out.read_text(encoding="utf-8")
    assert "合计选出：3 只" in text
    assert "策略命中：2 / 2" in text


# ── 数据状态一行 ──
# 页首的「日期」是**运行日**，数据可能来自更早的交易日（非交易日重跑、
# 数据源当天未发布行情等）。单列一个「数据日期」标签，两者才不会被读成同一件事。


def test_header_shows_data_status(gen):
    out = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        data_status="2026-09-24 · 全市场 5221 只已更新",
    )
    text = out.read_text(encoding="utf-8")
    assert '<span class="status">数据日期：2026-09-24 · 全市场 5221 只已更新</span>' in text


def test_header_omits_data_status_when_absent(gen):
    """没传就不显示（离线重算等场景没有同步动作，不能凭空写一个日期）。"""
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert 'class="status"' not in text


def test_header_escapes_data_status(gen):
    """数据状态会带 `%` 等字符，且终究来自外部文本 —— 必须走转义。"""
    out = gen.generate({"TurtleTradeStrategy": ["600000"]}, data_status="<b>x</b> · 1%")
    text = out.read_text(encoding="utf-8")
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>x</b>" not in text


# ── 全市场名称/行业表的拉取与缓存 ──


class _FakeRS:
    """伪造成 baostock 的 ResultData。"""

    fields = ["updateDate", "code", "code_name", "industry", "industryClassification"]
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = -1

    def next(self) -> bool:
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


def test_load_stock_meta_parses_and_caches(monkeypatch):
    """真实走一遍解析逻辑，并确认模块级缓存只登录一次。"""
    stock_meta_module.reset_cache()
    calls = {"login": 0}

    class _FakeBS:
        @staticmethod
        def login():
            calls["login"] += 1
            return type("L", (), {"error_code": "0", "error_msg": ""})()

        @staticmethod
        def logout():
            pass

        @staticmethod
        def query_stock_industry():
            return _FakeRS(
                [
                    ["2026-09-14", "sh.600000", "浦发银行", "J66货币金融服务", "证监会行业分类"],
                    ["2026-09-14", "sz.000001", "平安银行", "", "证监会行业分类"],
                ]
            )

    monkeypatch.setitem(sys.modules, "baostock", _FakeBS)

    try:
        first = stock_meta_module.load_stock_meta()
        second = stock_meta_module.load_stock_meta()

        assert calls["login"] == 1, "第二次调用应命中缓存，不再登录"
        assert first is second
        assert first["600000"].name == "浦发银行"
        assert first["600000"].industry == "货币金融服务"  # 前缀已清洗
        assert first["000001"].industry is None  # 空行业 -> None
    finally:
        stock_meta_module.reset_cache()


# ── 上市板块判定 ──


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600000", BOARD_MAIN),
        ("601398", BOARD_MAIN),
        ("603228", BOARD_MAIN),
        ("605111", BOARD_MAIN),
        ("000001", BOARD_MAIN),
        ("001979", BOARD_MAIN),
        ("002913", BOARD_MAIN),
        ("003816", BOARD_MAIN),
        ("300750", BOARD_CHINEXT),
        ("301251", BOARD_CHINEXT),
        ("688981", BOARD_STAR),
        ("689009", BOARD_STAR),
        ("430047", BOARD_BSE),
        ("830799", BOARD_BSE),
        ("871981", BOARD_BSE),
        ("920819", BOARD_BSE),
        ("999999", BOARD_OTHER),
    ],
)
def test_board_of(symbol, expected):
    assert board_of(symbol) == expected


def test_board_rank_is_ordered():
    """排序权重：主板 < 创业板 < 科创板 < 北交所 < 其他。"""
    ranks = [board_rank(s) for s in ("600000", "300750", "688981", "830799", "999999")]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 5


def test_group_by_board_orders_and_sorts():
    groups = group_by_board(["688981", "300750", "600000", "000001", "830799", "688111"])

    assert [b for b, _ in groups] == [BOARD_MAIN, BOARD_CHINEXT, BOARD_STAR, BOARD_BSE]
    assert groups[0][1] == ["000001", "600000"]  # 主板内按代码升序
    assert groups[2][1] == ["688111", "688981"]  # 科创板内按代码升序


def test_group_by_board_skips_empty_boards():
    assert group_by_board(["600000"]) == [(BOARD_MAIN, ["600000"])]


def test_group_by_board_handles_empty_input():
    assert group_by_board([]) == []


# ── 报告中的板块分组 ──


def test_report_groups_rows_by_board_in_order(gen):
    text = gen.generate(
        {"TurtleTradeStrategy": ["688981", "300750", "600000", "000001", "830799"]}
    ).read_text(encoding="utf-8")

    assert 'class="board-row"' in text
    positions = [
        text.index(f'data-board="{board}"')
        for board in (BOARD_MAIN, BOARD_CHINEXT, BOARD_STAR, BOARD_BSE)
    ]
    assert positions == sorted(positions), "板块分组标题应按 主板→创业板→科创板→北交所 出现"


def test_report_sorts_within_board_by_code(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000", "000001"]}).read_text(encoding="utf-8")
    assert text.index("000001") < text.index("600000")


def test_card_head_shows_board_breakdown(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000", "300750"]}).read_text(encoding="utf-8")
    assert 'class="boards"' in text
    assert "主板 1" in text
    assert "创业板 1" in text


# ── 指标列（市值 / 换手率 / PE） ──


def _metric(cap_yi: float, turn: float, pe: float) -> StockMetric:
    """按「亿元」构造一个指标对象，方便断言展示格式。"""
    return StockMetric(
        symbol="x",
        circ_market_cap=cap_yi * 1e8,
        pe_ttm=pe,
        pb=None,
        turn=turn,
    )


def test_metric_columns_absent_without_metrics(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert "市值(亿)" not in text
    assert "PE(TTM)" not in text


def test_metric_columns_rendered_when_provided(gen):
    metrics = {"600000": _metric(1234.0, 3.456, 42.35)}
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, metrics=metrics).read_text(
        encoding="utf-8"
    )

    assert "市值(亿)" in text and "换手率" in text and "PE(TTM)" in text
    assert "1,234" in text  # 市值千分位、无小数
    assert "3.46" in text  # 换手率两位小数
    assert "42.4" in text  # PE 一位小数


def test_missing_metric_renders_placeholder(gen):
    """指标缺失时展示占位符，而不是留空或崩溃。"""
    metrics = {"600000": StockMetric(symbol="600000")}  # 各字段均为 None
    text = gen.generate({"TurtleTradeStrategy": ["600000", "000001"]}, metrics=metrics).read_text(
        encoding="utf-8"
    )

    assert "市值(亿)" in text
    assert text.count("—") >= 6  # 2 只 x 3 列指标全部降级


def test_group_header_colspan_follows_column_count(gen):
    plain = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert 'colspan="4"' in plain

    with_metrics = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        metrics={"600000": _metric(100.0, 1.0, 10.0)},
    ).read_text(encoding="utf-8")
    assert 'colspan="7"' in with_metrics

    with_both = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        metrics={"600000": _metric(100.0, 1.0, 10.0)},
        holdings={"600000": 50.0},
    ).read_text(encoding="utf-8")
    assert 'colspan="8"' in with_both


# ── 筹码集中度列（前十大流通股东合计占比） ──


def test_holding_column_absent_without_holdings(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")
    assert "十大流通" not in text


def test_holding_column_rendered_when_provided(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, holdings={"600000": 74.87}).read_text(
        encoding="utf-8"
    )

    assert "十大流通" in text
    assert "74.9" in text  # 一位小数


def test_holding_column_coexists_with_metrics(gen):
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        metrics={"600000": _metric(1234.0, 3.456, 42.35)},
        holdings={"600000": 74.87},
    ).read_text(encoding="utf-8")

    assert "市值(亿)" in text and "PE(TTM)" in text and "十大流通" in text


def test_missing_holding_renders_placeholder(gen):
    """只有当部分股票有股东数据时，缺失的那些应展示占位符。"""
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000", "000001"]}, holdings={"600000": 74.87}
    ).read_text(encoding="utf-8")

    assert "十大流通" in text
    assert "—" in text


# ── 量化评分：策略标记 ──


def test_build_symbol_marks_merges_and_sorts():
    """同一只股票命中多个策略时标记合并、按字典序排列（保证与飞书一致）。"""
    marks = build_symbol_marks(
        {
            "TurtleTradeStrategy": ["600000"],
            "RpsBreakoutStrategy": ["600000", "600601"],
            "MaVolumeStrategy": ["600601"],
        }
    )
    assert marks == {"600000": "RT", "600601": "MR"}


def test_build_symbol_marks_ignores_unknown_strategy():
    """未收录标记的策略不应让股票凭空获得标记。"""
    assert build_symbol_marks({"BrandNewStrategy": ["600000"]}) == {}


def test_build_symbol_marks_empty_results():
    assert build_symbol_marks({}) == {}


# ── 量化评分：板块内按名次排序 ──


def test_group_by_board_sorts_by_rank_within_board():
    symbols = ["600601", "600000"]  # 主板，输入顺序为代码降序
    ranked = dict(group_by_board(symbols, order={"600601": 0, "600000": 1}))
    assert ranked[BOARD_MAIN] == ["600601", "600000"]
    # 不传 order 时保持原有的代码升序
    assert dict(group_by_board(symbols))[BOARD_MAIN] == ["600000", "600601"]


def test_group_by_board_puts_unranked_last():
    """没算出评分的股票不能被丢掉，排在所属板块末尾。"""
    ranked = dict(group_by_board(["600000", "600601"], order={"600601": 0}))
    assert ranked[BOARD_MAIN] == ["600601", "600000"]


# ── 量化评分：排行卡片 ──


def test_ranking_card_absent_without_scores(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}).read_text(encoding="utf-8")

    assert "综合评分排行" not in text
    assert '<td class="num score"' not in text


def test_ranking_card_rendered_with_scores(gen):
    scores = [make_score("300750", 86.0), make_score("600000", 72.5)]
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000", "300750"]}, scores=scores
    ).read_text(encoding="utf-8")

    assert '<section class="card ranking"' in text
    assert "综合评分排行" in text
    assert '<td class="rank">1</td>' in text and '<td class="rank">2</td>' in text
    assert "86.0" in text and "72.5" in text


def test_ranking_card_shows_composite_column(gen):
    """「综合」列紧跟「评分」，排序依据必须直接在表里可见（不只是悬停里）。

    无胜率时综合分 == 评分（两个单元格同值）；有胜率时综合分 = 评分×0.7 +
    有效胜率×0.3，故两个值应当不同，且悬停能说明它是怎么算出来的。
    """
    scores = [
        make_score("600000", 80.0, prob_up=20.0, prob_samples=5, prob_confidence=100.0),
    ]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "<th>综合</th>" in text
    # 0.7×80 + 0.3×20 = 62.0
    assert '<td class="num composite"' in text and ">62.0</td>" in text
    assert "评分 80.0 ×0.7 + 有效胜率 20.0 ×0.3" in text


def test_ranking_card_composite_equals_score_without_winrate(gen):
    """没有胜率信息时综合分退化为评分，并如实说明「无胜率信息」。"""
    scores = [make_score("600000", 72.5)]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "无胜率信息，综合分 = 评分" in text
    assert ">72.5</td>" in text


def test_ranking_card_keeps_given_order(gen):
    """名次由传入顺序决定（调用方已按综合分降序排好）。"""
    scores = [make_score("300750", 90.0), make_score("600000", 60.0)]
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000", "300750"]}, scores=scores
    ).read_text(encoding="utf-8")

    ranking = text.split("综合评分排行", 1)[1]
    assert ranking.index("300750") < ranking.index("600000")


def test_ranking_card_explains_score_on_hover(gen):
    """分数必须是可解释的：悬停能看到五维分项、扣分、胜率与回撤。"""
    scores = [make_score("600000", 72.5, penalty=6.0, prob_up=66.7, max_drawdown=18.4)]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "策略共振 20" in text
    assert "惩罚 -6" in text
    assert "形态胜率 67%" in text
    assert "最大回撤 18.4%" in text
    # 表格单元格本身也要显示增强层的两个数字
    assert "66.7%" in text and "18.4%" in text


def test_ranking_card_shows_window_count_and_confidence(gen):
    """胜率必须带分母才可解读：悬停给「4/5（57%）」「候选窗口 9」，表格也各占一列。

    `prob_up` 是 `k / len(matches) * 100`，窗口数最多 5 —— 只写「57%」
    看不出它是「5 个窗口里涨了 4 个」还是「1 个窗口恰好涨了」。
    """
    scores = [
        make_score(
            "600000",
            72.5,
            prob_up=57.0,
            prob_samples=5,
            prob_confidence=55.0,
            prob_candidates=9,
        )
    ]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "<th>窗口</th>" in text and "<th>置信</th>" in text
    assert "形态胜率 3/5（57%）" in text
    assert "候选窗口 9" in text and "匹配可信度 55" in text
    assert '<td class="num">5</td>' in text and '<td class="num">55</td>' in text  # 表格单元格


def test_ranking_card_omits_window_hints_without_enhancement(gen):
    """没跑增强层（或旧口径数据）时不给窗口/候选/置信提示，不留空标签。"""
    scores = [make_score("600000", 72.5, prob_up=66.7)]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "形态胜率 67%" in text  # 没有分母时退回百分比，不写「67%（67%）」
    assert "候选窗口" not in text and "匹配可信度" not in text


def test_ranking_card_shows_marks_and_placeholder_for_missing_meta(gen):
    scores = [make_score("600601", 55.0, tags="T")]  # 600601 不在桩 meta 里
    text = gen.generate({"TurtleTradeStrategy": ["600601"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert ">T</td>" in text  # 标记列
    assert '<td class="name">—</td>' in text  # 缺名称的降级展示


# ── 量化评分：策略卡片追加评分列 ──


def test_strategy_card_shows_score_column(gen):
    scores = [make_score("600000", 80.0)]
    text = gen.generate({"TurtleTradeStrategy": ["600000"]}, scores=scores).read_text(
        encoding="utf-8"
    )

    assert "<th>评分</th>" in text
    assert '<td class="num score" title="' in text
    assert "80.0" in text


def test_strategy_card_score_placeholder_for_unscored_symbol(gen):
    """历史不足没算出评分的股票，评分列显示「—」而不是 0。"""
    scores = [make_score("600000", 80.0)]
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000", "000001"]}, scores=scores
    ).read_text(encoding="utf-8")

    assert '<td class="num score">—</td>' in text


def test_strategy_card_sorts_rows_by_score_inside_board(gen):
    """板块内部按评分降序，翻掉默认的代码升序。"""
    scores = [make_score("600601", 90.0), make_score("600000", 60.0)]
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000", "600601"]}, scores=scores
    ).read_text(encoding="utf-8")

    section = text.split("<h2>海龟突破</h2>", 1)[1]
    assert section.index("600601") < section.index("600000")


def test_strategy_card_keeps_code_order_without_scores(gen):
    text = gen.generate({"TurtleTradeStrategy": ["600601", "600000"]}).read_text(encoding="utf-8")

    section = text.split("<h2>海龟突破</h2>", 1)[1]
    assert section.index("600000") < section.index("600601")


def test_score_column_follows_holding_column(gen):
    """列顺序稳定：基础列 → 市值/换手/PE → 十大流通 → 评分。"""
    text = gen.generate(
        {"TurtleTradeStrategy": ["600000"]},
        holdings={"600000": 74.87},
        scores=[make_score("600000", 80.0)],
    ).read_text(encoding="utf-8")

    # 必须先定位到策略卡片 —— 页首的排行卡片排在最前面，直接取第一个 <thead> 会取错
    section = text.split("<h2>海龟突破</h2>", 1)[1]
    header = section.split("<thead><tr>", 1)[1].split("</tr>", 1)[0]
    assert header.index("十大流通") < header.index("评分")
