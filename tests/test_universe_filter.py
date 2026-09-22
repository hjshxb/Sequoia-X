"""股票池精筛（估值 / 流动性 / 技术面 / 行业）单元测试。

覆盖点：
    - 未配置任何条件时为纯透传，且不产生任何网络请求
    - .env 中留空字符串应被视为「未配置」
    - 各维度自己的过滤逻辑、边界值、关键字匹配
    - 指标缺失的股票判定为不通过
    - 单个数据源完全失败时只跳过该维度，其余维度照常生效
    - 流通市值公式与关键词拆分
"""

from datetime import date, timedelta

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import StockMeta
from sequoia_x.data.universe_filter import (
    _METRIC_CACHE,
    StockMetric,
    UniverseFilter,
    calc_circ_market_cap,
    split_keywords,
)

# 精筛相关的环境变量。`Settings(_env_file=None)` 只能屏蔽 .env 文件本身，
# 挡不住 os.environ —— 同一 pytest 进程内只要有模块在导入期 load_dotenv()
# （例如 import main），仓库真实 .env 的内容就会渗进这些用例。
_FILTER_ENV_VARS = (
    "MIN_MARKET_CAP",
    "MAX_MARKET_CAP",
    "MIN_PE",
    "MAX_PE",
    "MIN_PB",
    "MAX_PB",
    "MIN_TURNOVER",
    "MIN_TURN",
    "MAX_TURN",
    "INCLUDE_INDUSTRIES",
    "EXCLUDE_INDUSTRIES",
    "MIN_TOP10_FREE_HOLDING",
    "MAX_TOP10_FREE_HOLDING",
    "HOLDER_REPORT_DATE",
    "MIN_TODAY_DROP",
    "MAX_TODAY_DROP",
    "MIN_MA_DEVIATION",
    "MA_WINDOW",
)


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """每个用例前清空进程内缓存与环境变量，避免相互污染。"""
    _METRIC_CACHE.clear()
    stock_meta_module.reset_cache()
    for var in _FILTER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    _METRIC_CACHE.clear()
    stock_meta_module.reset_cache()


def make_settings(**overrides) -> Settings:
    """构造测试用 Settings。

    注意 `_env_file=None`：必须屏蔽仓库根目录的 .env，
    否则本地真实配置（如 MIN_MARKET_CAP=100）会渗进测试，
    导致「未配置即不过滤」这类断言随 .env 内容而失败。
    """
    base = {
        "db_path": "unused.db",
        "start_date": "2024-01-01",
        "feishu_webhook_url": "https://example.com/hook",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def stub_metrics(mapping: dict[str, StockMetric]):
    def _fetch(self, symbols):
        return {s: mapping[s] for s in symbols if s in mapping}

    return _fetch


def stub_turnover(mapping: dict[str, float]):
    def _fetch(self, symbols):
        return {s: mapping[s] for s in symbols if s in mapping}

    return _fetch


def stub_meta(mapping: dict[str, str]):
    """mapping: {symbol: industry}"""
    data = {s: StockMeta(symbol=s, name=f"股票{s}", industry=ind) for s, ind in mapping.items()}

    def _load():
        return data

    return _load


def stub_holdings(mapping: dict[str, float]):
    """stub `_fetch_holder_ratios`（市场级接口，不接收 symbols）。"""

    def _fetch(self):
        return dict(mapping)

    return _fetch


# 「今日跌幅」维度需要两个交易日的收盘价；这两个日期是 FakeEngine 的默认基准。
MARKET_LATEST_DATE = "2026-09-22"
PREV_DATE = "2026-09-21"


def fake_rows(
    drops: dict[str, float | None], latest_date: str = MARKET_LATEST_DATE
) -> dict[str, list[dict]]:
    """把 {symbol: 今日跌幅%} 反推成引擎返回的原始行情行。

    刻意**不**直接 stub「跌幅计算结果」，而是造原始行让过滤器自己算，
    这样能一并覆盖「昨收 → 涨跌幅」这条真实计算路径。

    值为 None 表示该股当日停牌（最后一行早于全市场最新交易日），
    过滤器应判为「今日行情缺失」。
    """
    rows: dict[str, list[dict]] = {}
    for symbol, drop in drops.items():
        if drop is None:
            rows[symbol] = [{"date": PREV_DATE, "close": 10.0}]
            continue
        rows[symbol] = [
            {"date": PREV_DATE, "close": 10.0},
            {"date": latest_date, "close": 10.0 * (1 - drop / 100)},
        ]
    return rows


def fake_history(closes: list[float], latest_date: str = MARKET_LATEST_DATE) -> list[dict]:
    """由收盘价序列（**旧 → 新**）生成引擎返回的行情行，末行为全市场最新交易日。

    均线维度要的是「最近 N 行的收盘价」，所以这里造的是完整序列，
    让过滤器自己截窗口、自己算均值 —— 而不是直接 stub 均线值。
    """
    end = date.fromisoformat(latest_date)
    n = len(closes)
    return [
        {"date": (end - timedelta(days=n - 1 - i)).isoformat(), "close": c}
        for i, c in enumerate(closes)
    ]


class FakeEngine:
    """最小 DataEngine 替身：全市场代码 + 本地成交额快照 + 本地行情历史。"""

    def __init__(
        self,
        symbols,
        turnover: dict[str, float] | None = None,
        drops: dict[str, float | None] | None = None,
        latest_date: str | None = MARKET_LATEST_DATE,
        histories: dict[str, list[float]] | None = None,
    ):
        self._symbols = list(symbols)
        self._turnover = dict(turnover or {})
        self._latest_date = latest_date
        base = latest_date or MARKET_LATEST_DATE
        self._rows = fake_rows(drops or {}, base)
        # histories 与 drops 可并存：前者用于均线（长序列），后者用于今日跌幅（两行）。
        for symbol, closes in (histories or {}).items():
            self._rows[symbol] = fake_history(closes, base)

    def get_local_symbols(self):
        return list(self._symbols)

    def get_latest_snapshot(self, symbols):
        return {s: {"turnover": self._turnover[s]} for s in symbols if s in self._turnover}

    def get_market_latest_date(self):
        return self._latest_date

    def get_recent_rows(self, symbols, rows=2):
        return {s: self._rows[s][-rows:] for s in symbols if s in self._rows}

    def _to_baostock_code(self, symbol):
        return f"sh.{symbol}" if symbol.startswith(("6", "9")) else f"sz.{symbol}"


NA = StockMetric("", None, None, None, None)


def metric(symbol, cap_yi=None, pe=None, pb=None, turn=None) -> StockMetric:
    return StockMetric(
        symbol=symbol,
        circ_market_cap=None if cap_yi is None else cap_yi * 1e8,
        pe_ttm=pe,
        pb=pb,
        turn=turn,
    )


# ── 配置解析 ──


def test_blank_env_values_become_none():
    settings = make_settings(
        min_market_cap="",
        max_pb="   ",
        min_turn="",
        min_turnover="",
        max_turn=None,
        min_top10_free_holding="",
        max_top10_free_holding="  ",
        min_today_drop="",
        max_today_drop="   ",
    )
    assert settings.min_market_cap is None
    assert settings.max_pb is None
    assert settings.min_turn is None
    assert settings.min_turnover is None
    assert settings.max_turn is None
    assert settings.min_top10_free_holding is None
    assert settings.max_top10_free_holding is None
    assert settings.min_today_drop is None
    assert settings.max_today_drop is None


def test_numeric_env_values_parsed():
    settings = make_settings(min_market_cap="100", min_turn="0.5", min_turnover="2")
    assert settings.min_market_cap == 100.0
    assert settings.min_turn == 0.5
    assert settings.min_turnover == 2.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("电子,软件", ["电子", "软件"]),
        ("电子，软件", ["电子", "软件"]),  # 中文逗号
        (" 电子 , 软件 ", ["电子", "软件"]),
        ("", []),
        (",,", []),
    ],
)
def test_split_keywords(raw, expected):
    assert split_keywords(raw) == expected


# ── enabled / describe ──


def test_disabled_when_nothing_configured():
    f = UniverseFilter(settings=make_settings())
    assert f.enabled is False
    assert "未启用" in f.describe()


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_market_cap", 50.0),
        ("max_pe", 60.0),
        ("min_pb", 1.0),
        ("min_turnover", 2.0),
        ("max_turn", 10.0),
        ("include_industries", "电子"),
        ("exclude_industries", "房地产"),
        ("min_top10_free_holding", 30.0),
        ("max_top10_free_holding", 70.0),
        ("min_today_drop", 5.0),
        ("max_today_drop", 5.0),
    ],
)
def test_enabled_when_any_configured(field, value):
    assert UniverseFilter(settings=make_settings(**{field: value})).enabled is True


def test_describe_mentions_all_dimensions():
    f = UniverseFilter(
        settings=make_settings(
            min_market_cap=100,
            max_pe=60,
            max_pb=8,
            min_turnover=2,
            min_turn=0.5,
            max_turn=15,
            max_today_drop=5,
            include_industries="电子,软件",
            exclude_industries="房地产",
            min_top10_free_holding=30,
        )
    )
    text = f.describe()
    for token in [
        "流通市值",
        "市盈率",
        "市净率",
        "成交额",
        "换手率",
        "今日跌幅",
        "电子",
        "房地产",
        "流通股东",
    ]:
        assert token in text


# ── describe(brief=True)：给「长度受限」的展示端（飞书卡片）用 ──


def test_describe_brief_collapses_long_keyword_list():
    """长关键词表折叠成「前 3 个 等 N 项」，避免吃掉整行预算。"""
    f = UniverseFilter(settings=make_settings(exclude_industries="房地产,银行,煤炭,钢铁,电力"))
    brief = f.describe(brief=True)
    assert "房地产/银行/煤炭" in brief
    assert "等 5 项" in brief
    assert "钢铁" not in brief


def test_describe_default_keeps_every_keyword():
    """默认（报告端）必须仍然列全，不因 brief 而丢失信息。"""
    f = UniverseFilter(settings=make_settings(exclude_industries="房地产,银行,煤炭,钢铁,电力"))
    assert "钢铁" in f.describe()


def test_describe_brief_keeps_short_keyword_list():
    f = UniverseFilter(settings=make_settings(include_industries="电子,软件"))
    assert "行业含 电子/软件" in f.describe(brief=True)


def test_describe_brief_omits_nothing_but_keyword_lists():
    """brief 的核心诉求：短条款一个都不能少，尾部条款不能被长列表挤掉。"""
    f = UniverseFilter(
        settings=make_settings(
            min_market_cap=100,
            min_pe=0,
            min_turnover=5,
            min_turn=2,
            max_turn=20,
            exclude_industries=",".join(f"行业{i}" for i in range(44)),
            min_top10_free_holding=30,
        )
    )
    brief = f.describe(brief=True)
    expected = [
        "流通市值 >=100亿",
        "市盈率TTM >=0",
        "成交额 >=5亿",
        "换手率 2~20%",
        "前十大流通股东 >=30%",
    ]
    for token in expected:
        assert token in brief
    assert brief.startswith("精筛：")


# ── 纯透传（未启用时不得发网络请求）──


def test_disabled_is_passthrough_without_any_io(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("未启用过滤时不应发起任何数据请求")

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", boom)
    monkeypatch.setattr(UniverseFilter, "_fetch_turnover", boom)
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", boom)
    monkeypatch.setattr(UniverseFilter, "_fetch_today_drops", boom)
    monkeypatch.setattr(stock_meta_module, "load_stock_meta", boom)

    f = UniverseFilter(settings=make_settings())
    assert f.apply(["600000", "000001"]) == ["600000", "000001"]


def test_empty_input_short_circuits(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("空输入不应发起任何数据请求")

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", boom)
    f = UniverseFilter(settings=make_settings(min_market_cap=50))
    assert f.apply([]) == []


# ── 估值维度 ──


def test_filter_by_market_cap(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", cap_yi=30),  # 太小
                "000002": metric("000002", cap_yi=100),  # 区间内
                "000003": metric("000003", cap_yi=900),  # 太大
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_market_cap=50, max_market_cap=500))
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


def test_filter_by_pe(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", pe=-8.0),  # 亏损
                "000002": metric("000002", pe=15.0),  # 区间内
                "000003": metric("000003", pe=120.0),  # 太高
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_pe=0, max_pe=60))
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


def test_filter_by_pb(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", pb=0.3),
                "000002": metric("000002", pb=3.0),
                "000003": metric("000003", pb=20.0),
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_pb=1.0, max_pb=8.0))
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


@pytest.mark.parametrize(
    "cap_yi,expected",
    [(50.0, True), (49.99, False), (500.0, True), (500.01, False)],
)
def test_market_cap_boundaries_inclusive(monkeypatch, cap_yi, expected):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics({"000001": metric("000001", cap_yi=cap_yi)}),
    )
    f = UniverseFilter(settings=make_settings(min_market_cap=50, max_market_cap=500))
    assert (f.apply(["000001"]) == ["000001"]) is expected


def test_valuation_dimensions_are_anded(monkeypatch):
    """同属估值维度时，市值/PE/PB 必须同时满足。"""
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", cap_yi=100, pe=15.0, pb=3.0),  # 全满足
                "000002": metric("000002", cap_yi=100, pe=15.0, pb=99.0),  # PB 不行
                "000003": metric("000003", cap_yi=100, pe=999.0, pb=3.0),  # PE 不行
                "000004": metric("000004", cap_yi=1.0, pe=15.0, pb=3.0),  # 市值不行
            }
        ),
    )
    f = UniverseFilter(
        settings=make_settings(min_market_cap=50, max_market_cap=500, min_pe=0, max_pe=60, max_pb=8)
    )
    assert f.apply(["000001", "000002", "000003", "000004"]) == ["000001"]


# ── 流动性维度 ──


def test_filter_by_min_turnover(monkeypatch):
    """成交额取自本地库，单位为元；配置单位为亿元。"""
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_turnover",
        stub_turnover(
            {
                "000001": 0.5e8,  # 0.5 亿，不足
                "000002": 2e8,  # 2 亿，达标
                "000003": 50e8,  # 50 亿，达标
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_turnover=1))
    assert f.apply(["000001", "000002", "000003"]) == ["000002", "000003"]


def test_liquidity_does_not_require_baostock(monkeypatch):
    """只配成交额时，不应调用 baostock 指标接口。"""
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)
    monkeypatch.setattr(UniverseFilter, "_fetch_turnover", stub_turnover({"600000": 5e8}))

    f = UniverseFilter(settings=make_settings(min_turnover=1))
    assert f.apply(["600000"]) == ["600000"]
    assert calls["n"] == 0


# ── 技术面维度 ──


def test_filter_by_turn_rate(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", turn=0.1),  # 太低
                "000002": metric("000002", turn=3.0),  # 区间内
                "000003": metric("000003", turn=40.0),  # 太高（过热）
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_turn=0.5, max_turn=15))
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


# ── 行业维度 ──


def test_industry_include_whitelist(monkeypatch):
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta(
            {
                "000001": "计算机、通信和其他电子设备制造业",
                "000002": "货币金融服务",
                "000003": "软件和信息技术服务业",
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(include_industries="电子,软件"))
    # 「计算机、通信和其他电子设备制造业」含「电子」；「软件和信息技术服务业」含「软件」
    assert f.apply(["000001", "000002", "000003"]) == ["000001", "000003"]


def test_industry_exclude_blacklist(monkeypatch):
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta(
            {
                "000001": "货币金融服务",
                "000002": "房地产业",
                "000003": "软件和信息技术服务业",
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(exclude_industries="房地产,金融"))
    assert f.apply(["000001", "000002", "000003"]) == ["000003"]


def test_industry_include_and_exclude_combined(monkeypatch):
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta(
            {
                "000001": "计算机、通信和其他电子设备制造业",
                "000002": "电子元件及电子专用材料制造",
                "000003": "房地产业",
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(include_industries="电子", exclude_industries="元件"))
    assert f.apply(["000001", "000002", "000003"]) == ["000001"]


def test_industry_substring_match_is_not_exact(monkeypatch):
    """关键词按子串匹配，用户不必写出完整行业名。"""
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta({"000001": "计算机、通信和其他电子设备制造业"}),
    )
    f = UniverseFilter(settings=make_settings(include_industries="通信"))
    assert f.apply(["000001"]) == ["000001"]


def test_missing_industry_is_rejected(monkeypatch):
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta({"000001": "软件和信息技术服务业"}),
    )
    f = UniverseFilter(settings=make_settings(include_industries="软件"))
    # 000002 不在行业表中 -> 行业为空 -> 不通过
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_missing_industry_passes_when_only_blacklist(monkeypatch):
    """只有黑名单时，行业未知应放行（无法证明它属于被排除的行业）。

    全市场约 6% 的股票没有行业分类，一刀切剔除会误杀。
    """
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta({"000001": "房地产业"}),
    )
    f = UniverseFilter(settings=make_settings(exclude_industries="房地产"))
    # 000001 命中黑名单被剔除；000002 行业未知 -> 放行
    assert f.apply(["000001", "000002"]) == ["000002"]


def test_unknown_industry_rejected_when_whitelist_present(monkeypatch):
    """白名单 + 黑名单同时配置时，行业未知仍按「无法命中白名单」剔除。"""
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta({"000001": "房地产管理服务"}),
    )
    f = UniverseFilter(
        settings=make_settings(include_industries="电子", exclude_industries="房地产")
    )
    assert f.apply(["000001", "000002"]) == []


def test_industry_does_not_require_baostock_metrics(monkeypatch):
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)
    monkeypatch.setattr(
        stock_meta_module, "load_stock_meta", stub_meta({"600000": "软件和信息技术服务业"})
    )

    f = UniverseFilter(settings=make_settings(include_industries="软件"))
    assert f.apply(["600000"]) == ["600000"]
    assert calls["n"] == 0


# ── 缺失与容错 ──


def test_missing_metric_is_rejected(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", cap_yi=100, pe=None),  # 亏损股 PE 缺失
                "000002": metric("000002", cap_yi=100, pe=20.0),
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(max_pe=60))
    assert f.apply(["000001", "000002"]) == ["000002"]


def test_valuation_failure_skips_only_that_dimension(monkeypatch):
    """估值数据全挂时跳过估值判据，但行业判据仍应生效。"""
    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", stub_metrics({}))
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta({"000001": "软件和信息技术服务业", "000002": "房地产业"}),
    )

    f = UniverseFilter(settings=make_settings(min_market_cap=100, include_industries="软件"))
    # 000001 通过行业，000002 被行业挡掉；市值判据因数据全失败被跳过
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_industry_failure_skips_only_that_dimension(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {"000001": metric("000001", cap_yi=200), "000002": metric("000002", cap_yi=200)}
        ),
    )
    monkeypatch.setattr(stock_meta_module, "load_stock_meta", lambda: {})

    f = UniverseFilter(settings=make_settings(min_market_cap=100, include_industries="软件"))
    # 行业数据全失败 -> 行业判据跳过，市值判据生效，两只都保留
    assert f.apply(["000001", "000002"]) == ["000001", "000002"]


def test_symbol_without_fetched_data_is_rejected(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics({"000001": metric("000001", cap_yi=100)}),
    )
    f = UniverseFilter(settings=make_settings(min_market_cap=50))
    assert f.apply(["000001", "000002"]) == ["000001"]


# ── 缓存 ──


def test_metrics_are_fetched_only_once(monkeypatch):
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {s: metric(s, cap_yi=100) for s in symbols}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)

    f = UniverseFilter(settings=make_settings(min_market_cap=50))
    f.apply(["000001"])
    f.apply(["000001"])
    assert calls["n"] == 1


# ── 前十大流通股东合计占比维度 ──


def test_filter_by_top10_holding_min(monkeypatch):
    """MIN 语义：保留「筹码集中」（前十大合计占比达标）的股票。"""
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_holder_ratios",
        stub_holdings(
            {
                "000001": 21.5,  # 分散，不达标
                "000002": 30.0,  # 恰好达标（闭区间）
                "000003": 68.4,  # 集中
            }
        ),
    )
    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30))
    assert f.apply(["000001", "000002", "000003"]) == ["000002", "000003"]


def test_filter_by_top10_holding_max(monkeypatch):
    """MAX 语义：剔除过度集中（流通盘被锁死、流动性差）的股票。"""
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_holder_ratios",
        stub_holdings({"000001": 20.0, "000002": 70.0, "000003": 95.0}),
    )
    f = UniverseFilter(settings=make_settings(max_top10_free_holding=70))
    assert f.apply(["000001", "000002", "000003"]) == ["000001", "000002"]


def test_filter_by_top10_holding_range(monkeypatch):
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_holder_ratios",
        stub_holdings({"000001": 25.0, "000002": 45.0, "000003": 80.0}),
    )
    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30, max_top10_free_holding=70))
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


def test_top10_holding_missing_rejected_when_min(monkeypatch):
    """设了 MIN 时，没有股东数据的股票无法证明达标 -> 剔除。"""
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({"000001": 55.0}))
    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30))
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_top10_holding_missing_passes_when_only_max(monkeypatch):
    """只设 MAX 时，数据缺失应放行（无法证明它超过上限）。"""
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({"000001": 95.0}))
    f = UniverseFilter(settings=make_settings(max_top10_free_holding=70))
    assert f.apply(["000001", "000002"]) == ["000002"]


def test_top10_holding_failure_skips_only_that_dimension(monkeypatch):
    """股东数据整体拉取失败时，只跳过该维度，其余维度照常生效。"""
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({}))
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {"000001": metric("000001", cap_yi=200), "000002": metric("000002", cap_yi=5)}
        ),
    )
    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30, min_market_cap=100))
    # 股东维度被跳过；市值维度生效，只剩 000001
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_top10_holding_does_not_require_baostock(monkeypatch):
    """只配股东维度时，不应调用 baostock 指标接口。"""
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({"600000": 40.0}))

    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30))
    assert f.apply(["600000"]) == ["600000"]
    assert calls["n"] == 0


def test_top10_holding_does_not_require_stock_meta(monkeypatch):
    """只配股东维度时，不应加载行业表。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {}

    monkeypatch.setattr(stock_meta_module, "load_stock_meta", counting)
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({"600000": 40.0}))

    f = UniverseFilter(settings=make_settings(min_top10_free_holding=30))
    assert f.apply(["600000"]) == ["600000"]
    assert calls["n"] == 0


def test_fetch_holder_ratios_delegates_to_holder_module(monkeypatch):
    """过滤器只负责判定，取数细节（报告期 / 缓存目录）交给数据模块。"""
    from sequoia_x.data import holder_concentration

    seen: dict = {}

    def fake_load(**kwargs):
        seen.update(kwargs)
        return {"600000": 40.0}

    monkeypatch.setattr(holder_concentration, "load_top10_free_holding", fake_load)

    f = UniverseFilter(
        settings=make_settings(
            min_top10_free_holding=30,
            holder_report_date="2025-12-31",
            holder_cache_dir="tmp/cache",
        )
    )
    assert f.apply(["600000"]) == ["600000"]
    assert seen["report_date"] == "2025-12-31"
    assert seen["cache_dir"] == "tmp/cache"


# ── 前置过滤（分级执行） ──


def test_get_passing_universe_is_passthrough_when_disabled():
    engine = FakeEngine(["600000", "000001"])
    f = UniverseFilter(settings=make_settings(), engine=engine)
    assert f.get_passing_universe() == ["600000", "000001"]


def test_get_passing_universe_staged_matches_single_pass(monkeypatch):
    """分级执行的结果必须与「一次完整过滤」完全一致。"""
    symbols = ["600001", "600002", "000003", "000004"]
    engine = FakeEngine(
        symbols,
        turnover={"600001": 10e8, "600002": 10e8, "000003": 0.1e8, "000004": 10e8},
    )
    monkeypatch.setattr(
        stock_meta_module,
        "load_stock_meta",
        stub_meta(
            {
                "600001": "软件和信息技术服务业",
                "600002": "房地产业",
                "000003": "软件和信息技术服务业",
                "000004": "软件和信息技术服务业",
            }
        ),
    )
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "600001": metric("600001", cap_yi=200, pe=15.0, turn=3.0),
                "000004": metric("000004", cap_yi=5, pe=15.0, turn=3.0),
            }
        ),
    )
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_holder_ratios",
        stub_holdings({"600001": 55.0, "000004": 55.0}),
    )

    settings = make_settings(
        min_turnover=1,
        include_industries="软件",
        min_market_cap=100,
        min_pe=0,
        min_turn=1,
        min_top10_free_holding=30,
    )
    staged = UniverseFilter(settings=settings, engine=engine).get_passing_universe()
    # 单次完整过滤（不借助分级）
    single = UniverseFilter(settings=settings, engine=engine).apply(symbols)
    assert staged == single == ["600001"]


def test_get_passing_universe_applies_holder_in_stage_one(monkeypatch):
    """股东维度不逐股联网，应放在第 1 级（零边际成本）。"""
    engine = FakeEngine(["600001", "600002"], turnover={"600001": 10e8, "600002": 10e8})
    monkeypatch.setattr(UniverseFilter, "_fetch_holder_ratios", stub_holdings({"600001": 55.0}))
    baostock_calls = {"n": 0}

    def counting(self, symbols):
        baostock_calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)

    f = UniverseFilter(
        settings=make_settings(min_turnover=1, min_top10_free_holding=30), engine=engine
    )
    assert f.get_passing_universe() == ["600001"]
    assert baostock_calls["n"] == 0


# ── 市值公式 ──


def test_calc_circ_market_cap_from_turn():
    """以浦发银行 2026-09-18 真实数据量级校验。

    close=9.07, volume=51759299, turn=0.1554
    流通股本 = 51759299 / 0.001554 ≈ 3.33e10 股
    流通市值 ≈ 3.02e11 元（约 3021 亿元）
    """
    cap = calc_circ_market_cap(close=9.07, volume=51759299.0, turn=0.1554)
    assert cap is not None
    assert 2900e8 < cap < 3100e8


@pytest.mark.parametrize(
    "close,volume,turn",
    [(9.07, 51759299.0, 0.0), (0.0, 100.0, 1.0), (10.0, 0.0, 1.0)],
)
def test_calc_circ_market_cap_invalid_input(close, volume, turn):
    assert calc_circ_market_cap(close, volume, turn) is None


# ── 与策略层的集成 ──


def test_strategy_applies_universe_filter(monkeypatch):
    """策略的 apply_universe_filter 应接入统一的过滤器。"""
    from sequoia_x.strategy.ma_volume import MaVolumeStrategy

    settings = make_settings(min_market_cap=50, max_market_cap=500)
    strategy = MaVolumeStrategy(engine=None, settings=settings)  # type: ignore[arg-type]

    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics(
            {
                "000001": metric("000001", cap_yi=100),
                "000002": metric("000002", cap_yi=5),
            }
        ),
    )
    assert strategy.apply_universe_filter(["000001", "000002"]) == ["000001"]


# ── 今日跌幅维度（取自本地库，零网络开销）──


def test_today_drop_disabled_when_not_configured():
    assert UniverseFilter(settings=make_settings())._today_drop_enabled is False


def test_filter_by_max_today_drop_excludes_big_losers():
    """MAX 语义：剔除今日跌超阈值的，保留抗跌与上涨的。

    「跌幅」为有符号量：上涨时为负（-9.9 = 涨 9.9%），天然落在上限之内。
    """
    engine = FakeEngine(
        ["000001", "000002", "000003", "000004"],
        drops={
            "000001": 2.0,  # 小跌 -> 保留
            "000002": 5.0,  # 恰好到线（闭区间）-> 保留
            "000003": 5.01,  # 跌超 5% -> 剔除
            "000004": -9.9,  # 逆势大涨 -> 保留
        },
    )
    f = UniverseFilter(settings=make_settings(max_today_drop=5), engine=engine)
    assert f.apply(["000001", "000002", "000003", "000004"]) == ["000001", "000002", "000004"]


def test_filter_by_min_today_drop_keeps_only_big_losers():
    """MIN 语义：只保留今日跌够深的（超跌 / 错杀）。"""
    engine = FakeEngine(["000001", "000002"], drops={"000001": 2.0, "000002": 6.0})
    f = UniverseFilter(settings=make_settings(min_today_drop=5), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000002"]


def test_filter_by_today_drop_range():
    engine = FakeEngine(
        ["000001", "000002", "000003"],
        drops={"000001": 1.0, "000002": 4.0, "000003": 9.0},
    )
    f = UniverseFilter(settings=make_settings(min_today_drop=2, max_today_drop=5), engine=engine)
    assert f.apply(["000001", "000002", "000003"]) == ["000002"]


def test_today_drop_missing_passes_when_only_max():
    """停牌股当日没有行情：只设上限时放行（无法证明它跌超上限）。"""
    engine = FakeEngine(["000001", "000002"], drops={"000001": 1.0, "000002": None})
    f = UniverseFilter(settings=make_settings(max_today_drop=5), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001", "000002"]


def test_today_drop_missing_rejected_when_min():
    """设了下限时，无今日行情的股票无法证明「跌够深」-> 剔除。"""
    engine = FakeEngine(["000001", "000002"], drops={"000001": 9.0, "000002": None})
    f = UniverseFilter(settings=make_settings(min_today_drop=5), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_today_drop_does_not_require_baostock(monkeypatch):
    """跌幅只在本地库计算，不应触发任何 baostock 指标请求。"""
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)
    engine = FakeEngine(["600000"], drops={"600000": 1.0})

    f = UniverseFilter(settings=make_settings(max_today_drop=5), engine=engine)
    assert f.apply(["600000"]) == ["600000"]
    assert calls["n"] == 0


def test_today_drop_failure_skips_only_that_dimension():
    """本地行情整体取不到时只跳过该维度，其余维度照常生效。"""
    engine = FakeEngine(
        ["000001", "000002"],
        turnover={"000001": 10e8, "000002": 0.1e8},
        drops={},
    )
    f = UniverseFilter(
        settings=make_settings(max_today_drop=5, min_turnover=1), engine=engine
    )
    # 跌幅维度被跳过；成交额维度生效，000002 成交额不足被剔除
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_today_drop_without_engine_is_passthrough():
    """没有 engine 就取不到本地行情 —— 必须跳过该维度，而不是把股票全剔除。"""
    f = UniverseFilter(settings=make_settings(max_today_drop=5))
    assert f.apply(["000001", "000002"]) == ["000001", "000002"]


def test_get_passing_universe_applies_today_drop_in_stage_one(monkeypatch):
    """今日跌幅取自本地库，属零边际成本，应放在第 1 级预筛。"""
    engine = FakeEngine(
        ["600001", "600002"],
        turnover={"600001": 10e8, "600002": 10e8},
        drops={"600001": 1.0, "600002": 8.0},
    )
    baostock_calls = {"n": 0}

    def counting(self, symbols):
        baostock_calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)

    f = UniverseFilter(settings=make_settings(min_turnover=1, max_today_drop=5), engine=engine)
    assert f.get_passing_universe() == ["600001"]
    assert baostock_calls["n"] == 0


def test_get_passing_universe_staged_matches_single_pass_with_today_drop(monkeypatch):
    """加入今日跌幅维度后，分级执行仍须与「一次完整过滤」结果一致。"""
    symbols = ["600001", "600002", "600003"]
    engine = FakeEngine(
        symbols,
        turnover={s: 10e8 for s in symbols},
        drops={"600001": 1.0, "600002": 8.0, "600003": 3.0},
    )
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics({s: metric(s, cap_yi=200) for s in symbols}),
    )
    settings = make_settings(min_turnover=1, max_today_drop=5, min_market_cap=100)
    staged = UniverseFilter(settings=settings, engine=engine).get_passing_universe()
    single = UniverseFilter(settings=settings, engine=engine).apply(symbols)
    assert staged == single == ["600001", "600003"]


def test_describe_mentions_today_drop():
    f = UniverseFilter(settings=make_settings(max_today_drop=5))
    assert "今日跌幅 <=5%" in f.describe()


# ── 均线维度：收盘价在 N 日均线上方 ──
#
# 构造思路：历史前 n-1 个点恒为 10，最后一点决定它站在均线的哪一侧。
#   MA = ((n-1)*10 + last) / n ⇒ last > 10 时收盘价高于均线，last < 10 时低于均线。
_FLAT = 10.0
_MA_N = 120


def flat_then(last: float, n: int = _MA_N) -> list[float]:
    """n 个收盘价：前 n-1 个为 10，最后一个是 `last`。"""
    return [_FLAT] * (n - 1) + [last]


def test_ma_disabled_when_not_configured():
    """未配置 MIN_MA_DEVIATION 时该维度不参与过滤。"""
    assert UniverseFilter(settings=make_settings())._ma_enabled is False


def test_ma_default_window_is_120():
    assert make_settings().ma_window == 120


def test_filter_by_ma_keeps_only_above_average():
    """站上 120 日线的留下，跌破的剔除。"""
    engine = FakeEngine(
        ["000001", "000002"],
        histories={"000001": flat_then(11.0), "000002": flat_then(9.0)},
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_close_exactly_on_average_passes():
    """闭区间：收盘价恰好等于均线算「站在上方」。"""
    engine = FakeEngine(
        ["000001", "000002"],
        histories={"000001": [_FLAT] * _MA_N, "000002": flat_then(9.0)},
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_rejects_insufficient_history():
    """次新股历史不足 N 行 —— 算不出均线，无法证明它在均线上方，故剔除。"""
    engine = FakeEngine(
        ["000001", "000002"],
        # 000001 只有 30 个交易日，不足 120
        histories={"000001": flat_then(11.0, n=30), "000002": flat_then(11.0)},
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000002"]


def test_ma_deviation_threshold_requires_stronger_signal():
    """MIN_MA_DEVIATION=5 时，只是小幅站上均线的股票不达标。"""
    engine = FakeEngine(
        ["000001", "000002"],
        # 11.0 相对均线约 +9.9%；10.2 只有约 +2.0%
        histories={"000001": flat_then(11.0), "000002": flat_then(10.2)},
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=5), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_window_is_configurable():
    """窗口可配：MA_WINDOW=5 时按最近 5 个交易日算。"""
    engine = FakeEngine(
        ["000001", "000002"],
        histories={"000001": [10.0, 10.0, 10.0, 10.0, 12.0], "000002": [10.0] * 4 + [8.0]},
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0, ma_window=5), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_uses_only_the_recent_window():
    """窗口必须严格截到最近 N 行，历史更早的高价不得影响均线。

    000001 前 10 天是 100、之后 119 天是 10、最新 11：
      只看最近 120 行 → 均线 ≈ 10.01，收盘价在上方（通过）；
      若误用全部 130 行 → 均线 ≈ 16.9，会被误杀。
    000002 直接跌破均线，确保这条用例真的在过滤而不是透传。
    """
    engine = FakeEngine(
        ["000001", "000002"],
        histories={
            "000001": [100.0] * 10 + [10.0] * (_MA_N - 1) + [11.0],
            "000002": flat_then(9.0),
        },
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0), engine=engine)
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_does_not_require_baostock(monkeypatch):
    """均线只用本地行情库，不应触发任何 baostock 指标请求。"""
    calls = {"n": 0}

    def counting(self, symbols):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)
    engine = FakeEngine(
        ["600000", "600001"],
        histories={"600000": flat_then(11.0), "600001": flat_then(9.0)},
    )

    f = UniverseFilter(settings=make_settings(min_ma_deviation=0), engine=engine)
    assert f.apply(["600000", "600001"]) == ["600000"]
    assert calls["n"] == 0


def test_ma_failure_skips_only_that_dimension():
    """本地行情整体取不到时只跳过均线维度，其余维度照常生效。"""
    engine = FakeEngine(
        ["000001", "000002"],
        turnover={"000001": 10e8, "000002": 0.1e8},
        histories={},  # 没有任何历史行情
    )
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0, min_turnover=1), engine=engine)
    # 均线维度被跳过；成交额维度生效，000002 成交额不足被剔除
    assert f.apply(["000001", "000002"]) == ["000001"]


def test_ma_without_engine_is_passthrough():
    """没有 engine 就取不到本地行情 —— 必须跳过该维度，而不是把股票全剔除。"""
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0))
    assert f.apply(["000001", "000002"]) == ["000001", "000002"]


def test_get_passing_universe_applies_ma_in_stage_one(monkeypatch):
    """均线取自本地库，属零边际成本，应放在第 1 级预筛、且不碰 baostock。"""
    engine = FakeEngine(
        ["600001", "600002"],
        turnover={"600001": 10e8, "600002": 10e8},
        histories={"600001": flat_then(11.0), "600002": flat_then(9.0)},
    )
    baostock_calls = {"n": 0}

    def counting(self, symbols):
        baostock_calls["n"] += 1
        return {}

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", counting)

    f = UniverseFilter(settings=make_settings(min_turnover=1, min_ma_deviation=0), engine=engine)
    assert f.get_passing_universe() == ["600001"]
    assert baostock_calls["n"] == 0


def test_get_passing_universe_staged_matches_single_pass_with_ma(monkeypatch):
    """加入均线维度后，分级执行仍须与「一次完整过滤」结果一致。"""
    symbols = ["600001", "600002", "600003"]
    engine = FakeEngine(
        symbols,
        turnover={s: 10e8 for s in symbols},
        histories={
            "600001": flat_then(11.0),
            "600002": flat_then(9.0),  # 跌破均线 -> 剔除
            "600003": flat_then(12.0),
        },
    )
    monkeypatch.setattr(
        UniverseFilter,
        "_fetch_from_baostock",
        stub_metrics({s: metric(s, cap_yi=200) for s in symbols}),
    )
    settings = make_settings(min_turnover=1, min_ma_deviation=0, min_market_cap=100)
    staged = UniverseFilter(settings=settings, engine=engine).get_passing_universe()
    single = UniverseFilter(settings=settings, engine=engine).apply(symbols)
    assert staged == single == ["600001", "600003"]


def test_describe_mentions_ma():
    f = UniverseFilter(settings=make_settings(min_ma_deviation=0))
    assert "收盘价>=MA120" in f.describe()


def test_describe_mentions_ma_deviation_and_window():
    f = UniverseFilter(settings=make_settings(min_ma_deviation=3, ma_window=60))
    text = f.describe()
    assert "MA60" in text
    assert "3%" in text
