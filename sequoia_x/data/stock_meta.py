"""股票静态信息模块：股票名称与行业的获取、缓存与板块判定。

**为什么要落盘缓存**：名称与行业几乎不变，但旧实现每次运行都要多一次
`bs.login()` + 一次 `query_stock_industry()` 全市场请求；更糟的是数据源不可用
时直接返回空字典，报告与飞书卡片里的名称全部退化成「—」。
现在改为 read-through：内存 → 本地 SQLite → baostock → 写回本地，
且联网失败时**降级用本地旧数据**。

为什么单独成模块：股票名称与行业既有「报告展示」用途，也有「行业过滤」用途，
两处都要用。放在数据层可以让 notify 层与过滤逻辑共用同一份缓存，
避免重复请求，也避免数据层反向依赖 notify 层。

另外提供**上市板块**判定（`board_of`），它只由代码前缀决定、无需联网，
报告里按板块分组展示，过滤逻辑也可复用。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 证监会行业分类形如 "J66货币金融服务"、"C39计算机、通信和其他电子设备制造业"。
# 展示与匹配时都去掉前缀的字母+两位数字，只保留行业名。
_INDUSTRY_PREFIX_RE = re.compile(r"^[A-Z]\d{2}")

# 进程内缓存：{symbol: StockMeta}
_META_CACHE: dict[str, StockMeta] | None = None

# ── 本地落盘缓存（默认关闭，由 configure_cache 显式开启）──
CACHE_TABLE = "stock_meta"
DEFAULT_TTL_DAYS = 7
_TS_FMT = "%Y-%m-%d %H:%M:%S"

_CACHE_DB_PATH: str | None = None
_CACHE_TTL_DAYS: int = DEFAULT_TTL_DAYS

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    industry   TEXT,
    updated_at TEXT NOT NULL
);
"""

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


def configure_cache(db_path: str | None, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
    """启用或关闭「代码 → 名称/行业」的本地落盘缓存。

    刻意不在本模块内自己去读配置：`load_stock_meta()` 有多个调用方
    （行业过滤 / HTML 报告 / 飞书推送），保持无参签名可以让它们零改动；
    同时也避免测试误写到真实的行情库里。

    Args:
        db_path: SQLite 路径，直接复用行情库（`settings.db_path`）即可；
            传 None 表示关闭落盘，退回「内存 + 网络」的旧行为。
        ttl_days: 缓存有效期，超过就联网刷新；0 表示每次都刷新。
    """
    global _CACHE_DB_PATH, _CACHE_TTL_DAYS
    if db_path != _CACHE_DB_PATH:
        # 数据来源换了，旧的进程内结果不再可信
        reset_cache()
    _CACHE_DB_PATH = db_path
    _CACHE_TTL_DAYS = max(0, ttl_days)


def _read_cache(db_path: str) -> tuple[dict[str, StockMeta], str | None]:
    """读本地缓存。

    Returns:
        (meta, 上次刷新时间)。表不存在或读取失败时返回 ({}, None) —— 调用方
        会因此判定为「不新鲜」并走一次联网刷新，不会因为读库失败而中断。
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)  # 老库可能还没这张表
            rows = conn.execute(
                f"SELECT symbol, name, industry, updated_at FROM {CACHE_TABLE}"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(f"股票名称/行业：读本地缓存失败，将走联网刷新：{exc}")
        return {}, None

    meta = {
        row[0]: StockMeta(symbol=row[0], name=row[1] or None, industry=row[2] or None)
        for row in rows
    }
    last_updated = max((row[3] for row in rows if row[3]), default=None)
    return meta, last_updated


def _write_cache(db_path: str, meta: dict[str, StockMeta]) -> None:
    """整表 upsert，并给所有行打同一个时间戳 —— 因此 `MAX(updated_at)`
    就等于「上次整表刷新时间」，无需额外的状态表。"""
    ts = datetime.now().strftime(_TS_FMT)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.executemany(
                f"INSERT OR REPLACE INTO {CACHE_TABLE}"
                " (symbol, name, industry, updated_at) VALUES (?,?,?,?)",
                [(s, m.name, m.industry, ts) for s, m in meta.items()],
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning(f"股票名称/行业：写本地缓存失败（忽略，不影响本次结果）：{exc}")
        return
    logger.info(f"股票名称/行业：已写入本地缓存 {len(meta)} 条")


def _is_fresh(last_updated: str | None) -> bool:
    """本地缓存是否仍在 TTL 内。取不到/时间戳异常一律视为过期（触发一次刷新）。"""
    if not last_updated:
        return False
    try:
        ts = datetime.strptime(last_updated, _TS_FMT)
    except ValueError:
        return False
    return datetime.now() - ts < timedelta(days=_CACHE_TTL_DAYS)


def _fetch_from_baostock() -> dict[str, StockMeta]:
    """一次 login + 一次 `query_stock_industry()`（单次返回全市场）。

    失败时返回空字典并记 ERROR，不抛异常 —— 由调用方决定是降级还是兜底。
    """
    import baostock as bs

    meta: dict[str, StockMeta] = {}
    lg = bs.login()
    if lg.error_code != "0":
        logger.error(f"股票名称/行业：baostock 登录失败 {lg.error_code} {lg.error_msg}")
        return meta

    try:
        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            logger.error(f"股票名称/行业：获取失败 {rs.error_code} {rs.error_msg}")
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
        return {}
    finally:
        bs.logout()

    logger.info(f"股票名称/行业：从 baostock 拉取 {len(meta)} 条")
    return meta


def load_stock_meta() -> dict[str, StockMeta]:
    """加载全市场股票名称与行业，优先命中本地缓存。

    读取顺序：
        1. 进程内缓存 —— 一次运行内「行业过滤 / HTML 报告 / 飞书推送」共用；
        2. 本地 SQLite `stock_meta` 表（`configure_cache()` 配置后启用），
           数据仍在 TTL 内则**完全不联网**；
        3. baostock `query_stock_industry()`（一次请求返回全市场）；
        4. 拉取结果写回本地表，供后续运行离线命中。

    **降级行为（重要）**：本地有过期数据但联网失败（登录被拒 / 查询报错）时，
    退货本地旧数据并记 WARNING，而不是返回空 —— 空数据会让报告与卡片里的
    名称全部退化成「—」。本地也空才返回空字典。

    Returns:
        {symbol: StockMeta}；本地与网络都拿不到时为空字典，不抛异常。
    """
    global _META_CACHE
    if _META_CACHE is not None:
        return _META_CACHE

    local: dict[str, StockMeta] = {}
    last_updated: str | None = None
    if _CACHE_DB_PATH:
        local, last_updated = _read_cache(_CACHE_DB_PATH)
        if local and _is_fresh(last_updated):
            logger.info(f"股票名称/行业：命中本地缓存 {len(local)} 条（未联网）")
            _META_CACHE = local
            return local

    fetched = _fetch_from_baostock()
    if fetched:
        if _CACHE_DB_PATH:
            _write_cache(_CACHE_DB_PATH, fetched)
        _META_CACHE = fetched
        return fetched

    if local:
        logger.warning(
            f"股票名称/行业：联网获取失败，降级使用本地缓存 {len(local)} 条（可能已过期）"
        )
        _META_CACHE = local
        return local

    # 本地与网络都没有：缓存这个空结果，避免一次运行内反复失败登录（旧行为如此）
    logger.error("股票名称/行业：本地与 baostock 均无数据，本次运行将没有名称与行业")
    _META_CACHE = {}
    return _META_CACHE


def reset_cache() -> None:
    """清空进程内缓存（主要供测试使用；切换数据源时也会被 configure_cache 调用）。"""
    global _META_CACHE
    _META_CACHE = None
