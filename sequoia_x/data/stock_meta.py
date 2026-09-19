"""股票静态信息模块：从 baostock 一次性获取全市场的股票名称与行业。

为什么单独成模块：股票名称与行业既有「报告展示」用途，也有「行业过滤」用途，
两处都要用。放在数据层可以让 notify 层与过滤逻辑共用同一份缓存，
避免重复请求，也避免数据层反向依赖 notify 层。

另外提供**上市板块**判定（`board_of`），它只由代码前缀决定、无需联网，
报告里按板块分组展示，过滤逻辑也可复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 证监会行业分类形如 "J66货币金融服务"、"C39计算机、通信和其他电子设备制造业"。
# 展示与匹配时都去掉前缀的字母+两位数字，只保留行业名。
_INDUSTRY_PREFIX_RE = re.compile(r"^[A-Z]\d{2}")

# 进程内缓存：{symbol: StockMeta}
_META_CACHE: dict[str, StockMeta] | None = None

# ── 上市板块 ──
BOARD_MAIN = "主板"
BOARD_CHINEXT = "创业板"
BOARD_STAR = "科创板"
BOARD_BSE = "北交所"
BOARD_OTHER = "其他"

# 顺序即报告中的分组顺序
BOARD_ORDER: tuple[str, ...] = (BOARD_MAIN, BOARD_CHINEXT, BOARD_STAR, BOARD_BSE, BOARD_OTHER)
_BOARD_RANK: dict[str, int] = {name: i for i, name in enumerate(BOARD_ORDER)}

# 代码前缀 -> 板块。注意顺序：先匹配更具体的前缀。
# 600/601/603/605 沪主板，000/001/002/003 深主板（中小板 2021 年已并入主板）。
_BOARD_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("688", "689"), BOARD_STAR),
    (("300", "301"), BOARD_CHINEXT),
    (("600", "601", "603", "605"), BOARD_MAIN),
    (("000", "001", "002", "003"), BOARD_MAIN),
    (("43", "83", "87", "88", "920"), BOARD_BSE),
)


@dataclass(frozen=True)
class StockMeta:
    """股票静态信息。

    Attributes:
        symbol: 纯数字代码，如 '600000'。
        name: 股票名称，如 '浦发银行'。取不到时为 None。
        industry: 行业名称（已去掉分类前缀），如 '货币金融服务'。取不到时为 None。
    """

    symbol: str
    name: str | None
    industry: str | None


def clean_industry(raw: str) -> str:
    """去掉证监会行业分类的前缀码，如 'J66货币金融服务' -> '货币金融服务'。"""
    if not raw:
        return ""
    return _INDUSTRY_PREFIX_RE.sub("", raw.strip())


def board_of(symbol: str) -> str:
    """按代码前缀判定上市板块（纯本地判断，不联网）。

    - 科创板：688 / 689（涨跌幅 20%）
    - 创业板：300 / 301（涨跌幅 20%）
    - 主板  ：600 / 601 / 603 / 605（沪）、000 / 001 / 002 / 003（深）（涨跌幅 10%）
    - 北交所：43 / 83 / 87 / 88 / 920（涨跌幅 30%）

    Args:
        symbol: 纯数字股票代码，如 '300454'。

    Returns:
        板块名称；前缀无法识别时返回「其他」。
    """
    for prefixes, board in _BOARD_PREFIXES:
        if symbol.startswith(prefixes):
            return board
    return BOARD_OTHER


def board_rank(symbol: str) -> int:
    """板块排序权重，用于报告分组排序（主板 → 创业板 → 科创板 → 北交所 → 其他）。"""
    return _BOARD_RANK.get(board_of(symbol), len(BOARD_ORDER))


def load_stock_meta() -> dict[str, StockMeta]:
    """从 baostock 一次性拉取全市场股票名称与行业（带进程内缓存）。

    只发一次网络请求（`query_stock_industry` 单次返回全市场），
    失败时返回空字典并记 ERROR，不抛异常。

    Returns:
        {symbol: StockMeta}；拉取失败时为空字典。
    """
    global _META_CACHE
    if _META_CACHE is not None:
        return _META_CACHE

    import baostock as bs

    meta: dict[str, StockMeta] = {}
    lg = bs.login()
    if lg.error_code != "0":
        logger.error(f"股票名称/行业：baostock 登录失败 {lg.error_msg}")
        _META_CACHE = meta
        return meta

    try:
        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            logger.error(f"股票名称/行业：获取失败 {rs.error_msg}")
            return {}
        while rs.next():
            row = rs.get_row_data()
            # fields: [updateDate, code, code_name, industry, industryClassification]
            symbol = row[1].split(".")[-1]
            meta[symbol] = StockMeta(
                symbol=symbol,
                name=row[2].strip() or None,
                industry=clean_industry(row[3]) or None,
            )
    except Exception as exc:
        logger.error(f"股票名称/行业：拉取异常 {exc}")
    finally:
        bs.logout()

    _META_CACHE = meta
    logger.info(f"股票名称/行业加载完成，共 {len(meta)} 条")
    return meta


def reset_cache() -> None:
    """清空缓存（主要供测试使用）。"""
    global _META_CACHE
    _META_CACHE = None
