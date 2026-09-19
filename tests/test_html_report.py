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
    group_by_board,
    to_xueqiu_code,
)

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
