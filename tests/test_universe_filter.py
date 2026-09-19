"""股票池精筛（估值 / 流动性 / 技术面 / 行业）单元测试。

覆盖点：
    - 未配置任何条件时为纯透传，且不产生任何网络请求
    - .env 中留空字符串应被视为「未配置」
    - 四个维度各自的过滤逻辑、边界值、关键字匹配
    - 指标缺失的股票判定为不通过
    - 单个数据源完全失败时只跳过该维度，其余维度照常生效
    - 流通市值公式与关键词拆分
"""

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


@pytest.fixture(autouse=True)
def _clear_caches():
    """每个用例前清空进程内缓存，避免相互污染。"""
    _METRIC_CACHE.clear()
    stock_meta_module.reset_cache()
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
    )
    assert settings.min_market_cap is None
    assert settings.max_pb is None
    assert settings.min_turn is None
    assert settings.min_turnover is None
    assert settings.max_turn is None


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
            include_industries="电子,软件",
            exclude_industries="房地产",
        )
    )
    text = f.describe()
    for token in ["流通市值", "市盈率", "市净率", "成交额", "换手率", "电子", "房地产"]:
        assert token in text


# ── 纯透传（未启用时不得发网络请求）──


def test_disabled_is_passthrough_without_any_io(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("未启用过滤时不应发起任何数据请求")

    monkeypatch.setattr(UniverseFilter, "_fetch_from_baostock", boom)
    monkeypatch.setattr(UniverseFilter, "_fetch_turnover", boom)
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
