"""个股「所属板块 / 概念」模块：取自东方财富 F10 核心题材。

**为什么单独成模块**：选股结果只回答了「哪几只」，答不了「今天这批票扎堆在
什么方向」。看榜单的人（尤其是复盘时）最想知道的是**题材聚合**：是不是一批
票都落在「固态电池 / 国产芯片」这种同一条线上 —— 那意味着这是一次板块性
行情，而不是零散个股机会。行业与地域同理。

数据源：东财 `datacenter-web` 的 `RPT_F10_CORETHEME_BOARDTYPE`（F10「所属板块」
用同一张表），**逐股一次请求**、实测约 0.3s/只。刻意**不用** `push2.eastmoney.com`
的板块接口：push2 有较激进的限流（短时间内十余次请求即被整体拒连，恢复期内
连 `p1z=20` 的正常请求也返回 `RemoteDisconnected`），不适合放进每日定时任务；
`datacenter-web` 是与「筹码集中度」同一个已在生产跑稳的主机。

三类的判定口径（看 `BOARD_TYPE` / `IS_PRECISE` 两个字段）：

- **概念**：`IS_PRECISE == "1"`。这是东财标了「精准概念」的那批（储能概念、
  麒麟电池、国产芯片…），也是唯一适合做「题材聚合」的口径。
- **申万行业**：`BOARD_TYPE == "行业"`。东财给的是多层级（电力设备 → 电池 →
  锂电池），展示端要的是「这只票做什么」，故取**最具体**的一层，见 `_pick_industry`。
- **地域**：`BOARD_TYPE == "板块"`（福建板块、贵州板块…）。

`IS_PRECISE == "0"` 的行**一律丢弃**：那是指数样本与风格 / 交易属性标签
（茅指数、HS300_、深股通、融资融券、大盘股、权重股、周期股、百元股、
东方财富热股…）。把它们混进聚合结果会得出「今天选出来的票扎堆在大盘股」
这种等于没说的话，还会把真正的题材挤到后面。

**取数失败一律降级**（返回空 / 用旧缓存并记 WARNING），不抛异常：
这一节只是报告的附注，不该让一次网络抖动把当天的报告和推送整块吞掉。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REPORT_NAME = "RPT_F10_CORETHEME_BOARDTYPE"
_COLUMNS = "SECUCODE,SECURITY_CODE,BOARD_CODE,BOARD_NAME,IS_PRECISE,BOARD_TYPE"
_PAGE_SIZE = 120  # 单只股票实测最多 ~40 行，留足余量
_TIMEOUT = 15.0
_RETRIES = 3
_MAX_WORKERS = 8
_DEFAULT_TTL_DAYS = 1
_CACHE_FILE = "board_concept.json"

# BOARD_TYPE 的两个有效取值。其余（含 None）都是 `IS_PRECISE` 说了算的概念行。
_TYPE_INDUSTRY = "行业"
_TYPE_REGION = "板块"
_PRECISE = "1"

# 申万一级行业（2021 分类）31 个。东财的「行业」是多层级的，用这份名单把
# **一级行业**挑出来 —— 「电子 8 只」是有意义的聚合，「集成电路制造 1 只」不是。
# 名单没命中时退回 `_pick_industry` 的兜底逻辑（见下）。
_PRIMARY_INDUSTRIES = frozenset(
    {
        "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器", "食品饮料",
        "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产",
        "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备",
        "国防军工", "计算机", "传媒", "通信", "银行", "非银金融", "汽车",
        "机械设备", "煤炭", "石油石化", "环保", "美容护理",
    }
)  # fmt: skip

# 进程内缓存：{(cache_dir, symbol): StockBoards}
_MEMO: dict[tuple[str, str], StockBoards] = {}


@dataclass(frozen=True)
class StockBoards:
    """一只股票的所属板块。

    Attributes:
        symbol: 纯数字代码，如 '300750'。
        concepts: 精准概念名（已去重、保持接口返回顺序）。
        industry: 申万一级行业（优先）或最具体的行业层，取不到为 None。
        region: 地域板块名，如 '福建板块'，取不到为 None。
    """

    symbol: str
    concepts: tuple[str, ...] = ()
    industry: str | None = None
    region: str | None = None

    @property
    def is_empty(self) -> bool:
        """三类全空 —— 视为「没取到板块数据」，不计入覆盖数。"""
        return not self.concepts and not self.industry and not self.region


@dataclass(frozen=True)
class BoardSummary:
    """一批股票的板块 / 概念聚合结果，供报告与飞书卡片直接排版。

    Attributes:
        total: 参与统计的股票数（去重后）。
        covered: 其中取到板块数据的只数（`total - covered` 即缺失数）。
        concepts: [(概念名, 命中只数)]，按只数降序、同数按名称升序。
        industries: [(申万行业, 命中只数)]，排序同上。
        regions: [(地域板块, 命中只数)]，排序同上。
    """

    total: int = 0
    covered: int = 0
    concepts: tuple[tuple[str, int], ...] = ()
    industries: tuple[tuple[str, int], ...] = ()
    regions: tuple[tuple[str, int], ...] = ()

    @property
    def is_empty(self) -> bool:
        """三类分布都为空 —— 渲染层据此整块略过。"""
        return not self.concepts and not self.industries and not self.regions


def reset_cache() -> None:
    """清空进程内缓存（测试与 `--refresh` 场景使用；磁盘缓存不受影响）。"""
    _MEMO.clear()


# ── 解析 ──


def _as_int(raw: object) -> int:
    """把东财的字符串代码转成 int；非法值给 -1（排序时排在最前）。"""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return -1


def _pick_industry(rows: Sequence[tuple[str, str]]) -> str | None:
    """从多层级行业里选一个展示用的。

    优先命中申万一级名单（东财的层级里一定包含一级行业，且名字与申万一致）；
    一个都没命中时退回「行业代码最大」的那条 —— 东财的**申万三级**行业代码
    集中在新分配的 1300+ / 1500+ 段，实测：
    `电力设备/电池/锂电池 → 锂电池`、`电子/半导体/集成电路制造 → 集成电路制造`、
    `食品饮料/白酒Ⅱ/白酒Ⅲ → 白酒Ⅲ`，都恰好是最具体的那一层。这条兜底只是
    为了「新增 / 改名行业」时仍有值可显示，不承担准确性保证。

    Args:
        rows: [(行业代码, 行业名)]。

    Returns:
        行业名；`rows` 为空时返回 None。
    """
    for _code, name in rows:
        if name in _PRIMARY_INDUSTRIES:
            return name
    if not rows:
        return None
    return max(rows, key=lambda row: _as_int(row[0]))[1]


def parse_rows(symbol: str, rows: Iterable[Mapping[str, Any]]) -> StockBoards:
    """把东财原始行解析成 `StockBoards`（纯函数，便于单测）。

    Args:
        symbol: 纯数字代码。
        rows: 东财 `RPT_F10_CORETHEME_BOARDTYPE` 的 data 数组。

    Returns:
        解析结果；三类都取不到时各字段为空。
    """
    concepts: list[str] = []
    industries: list[tuple[str, str]] = []
    region: str | None = None

    for row in rows:
        name = str(row.get("BOARD_NAME") or "").strip()
        if not name:
            continue
        board_type = row.get("BOARD_TYPE")
        if board_type == _TYPE_REGION:
            # 地域板块每股最多一个，多命中时保首个（顺序由接口决定，不额外排序）
            region = region or name
        elif board_type == _TYPE_INDUSTRY:
            industries.append((str(row.get("BOARD_CODE") or ""), name))
        elif str(row.get("IS_PRECISE") or "") == _PRECISE and name not in concepts:
            concepts.append(name)

    return StockBoards(
        symbol=symbol,
        concepts=tuple(concepts),
        industry=_pick_industry(industries),
        region=region,
    )


# ── 网络 ──


def _request(symbol: str) -> list[dict]:
    """拉取单只股票的所属板块，带重试。

    Raises:
        Exception: 重试耗尽后抛出（由调用方汇总成「这一只失败」）。
    """
    params = {
        "reportName": _REPORT_NAME,
        "columns": _COLUMNS,
        "filter": f'(SECURITY_CODE="{symbol}")',
        "pageNumber": 1,
        "pageSize": _PAGE_SIZE,
        "source": "HSF10",
        "client": "PC",
    }
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(_API_URL, params=params, timeout=_TIMEOUT)
            payload = resp.json()
            return list(((payload.get("result") or {}).get("data")) or [])
        except Exception as exc:  # 网络抖动 / JSON 解析失败
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("未知请求错误")


# ── 磁盘缓存 ──


def cache_path(cache_dir: str) -> Path:
    """缓存文件路径（单文件存全市场，键为股票代码）。"""
    return Path(cache_dir) / _CACHE_FILE


def _read_cache(cache_dir: str) -> dict[str, dict]:
    """读缓存文件；不存在 / 损坏一律返回 {}（等价于全量刷新，不抛异常）。"""
    path = cache_path(cache_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"板块/概念：缓存 {path} 读取失败，将全量刷新：{exc}")
        return {}
    entries = payload.get("boards") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _write_cache(cache_dir: str, entries: Mapping[str, dict]) -> None:
    """整文件覆写。失败只记 WARNING —— 缓存写不进去不该影响本次结果。"""
    path = cache_path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(entries),
                    "boards": entries,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"板块/概念：写缓存失败（忽略，不影响本次结果）：{exc}")


def _is_fresh(entry: Mapping, ttl_days: int) -> bool:
    """缓存条目是否仍在 TTL 内；时间戳异常一律视为过期。"""
    raw = str(entry.get("fetched_at") or "")
    try:
        parsed = time.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (time.time() - time.mktime(parsed)) < ttl_days * 86400


def _entry_to_boards(symbol: str, entry: Mapping) -> StockBoards:
    return StockBoards(
        symbol=symbol,
        concepts=tuple(str(c) for c in (entry.get("concepts") or ())),
        industry=(str(entry["industry"]) if entry.get("industry") else None),
        region=(str(entry["region"]) if entry.get("region") else None),
    )


def _boards_to_entry(boards: StockBoards) -> dict:
    return {
        "concepts": list(boards.concepts),
        "industry": boards.industry,
        "region": boards.region,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 对外入口 ──


def load_board_concepts(
    symbols: Sequence[str],
    *,
    cache_dir: str,
    ttl_days: int = _DEFAULT_TTL_DAYS,
    workers: int = _MAX_WORKERS,
    refresh: bool = False,
) -> dict[str, StockBoards]:
    """按需拉取一批股票的「所属板块 / 概念」，命中缓存则不联网。

    只对「需要的股票」请求：`symbols` 一般是当日选出的**去重名单**（几十只），
    不是全市场 —— 这是这套逐股接口能放进每日任务的前提。

    失败处理刻意分两级，且都不抛异常：
        - 单只失败：记 WARNING、该只缺数据（聚合时计入 `total - covered`）；
        - 有旧缓存但已过期、刷新又失败：**降级用旧值**（与 `stock_meta` 一致，
          概念变化慢，旧值远好过没有）。

    Args:
        symbols: 需要查询的股票代码（自动去重、保序）。
        cache_dir: 缓存目录（与筹码缓存同目录即可，已 gitignore）。
        ttl_days: 缓存有效期（天）；0 表示每次都刷新。
        workers: 并发线程数。
        refresh: True 时忽略缓存与进程内 memo，强制重新拉取。

    Returns:
        {代码: StockBoards}；只包含至少取到一类的股票（取不到的键不会出现）。
    """
    wanted = list(dict.fromkeys(symbols))
    if not wanted:
        return {}

    cached = _read_cache(cache_dir)
    result: dict[str, StockBoards] = {}
    todo: list[str] = []

    for symbol in wanted:
        if not refresh:
            memo = _MEMO.get((cache_dir, symbol))
            if memo is not None:
                if not memo.is_empty:
                    result[symbol] = memo
                continue
        entry = cached.get(symbol)
        if entry and not refresh and _is_fresh(entry, ttl_days):
            boards = _entry_to_boards(symbol, entry)
            _MEMO[(cache_dir, symbol)] = boards
            if not boards.is_empty:
                result[symbol] = boards
            continue
        todo.append(symbol)

    if not todo:
        logger.info(f"板块/概念：{len(result)} 只全部命中缓存（未联网）")
        return result

    failed = 0
    fetched: dict[str, StockBoards] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_request, symbol): symbol for symbol in todo}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fetched[symbol] = parse_rows(symbol, future.result())
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed == 1:  # 只报第一只，避免刷屏
                    logger.warning(f"板块/概念：{symbol} 拉取失败 {exc}")

    # 写回：新结果覆盖旧的，未刷新到的（含失败）保留原条目 —— 它们是下次的降级来源。
    merged = dict(cached)
    for symbol, boards in fetched.items():
        merged[symbol] = _boards_to_entry(boards)
    _write_cache(cache_dir, merged)

    for symbol, boards in fetched.items():
        _MEMO[(cache_dir, symbol)] = boards
        if not boards.is_empty:
            result[symbol] = boards

    # 刷新失败但本地有旧值的：降级使用（记一次汇总日志，不逐只刷屏）
    degraded = 0
    for symbol in todo:
        if symbol in fetched:
            continue
        entry = cached.get(symbol)
        if not entry:
            continue
        boards = _entry_to_boards(symbol, entry)
        _MEMO[(cache_dir, symbol)] = boards
        if not boards.is_empty:
            result[symbol] = boards
            degraded += 1

    if failed:
        logger.warning(
            f"板块/概念：{failed}/{len(todo)} 只拉取失败"
            + (f"，其中 {degraded} 只已降级使用本地旧缓存" if degraded else "")
        )
    logger.info(f"板块/概念：本轮联网 {len(todo) - failed} 只，可用 {len(result)} 只")
    return result


def summarize(
    boards: Mapping[str, StockBoards],
    symbols: Iterable[str],
) -> BoardSummary:
    """把「股票 → 板块」聚合成「板块 → 命中只数」，供展示层排版。

    **同时统计总数与缺失数**，因为「今天选出 40 只」和「其中只有 26 只取到
    板块数据」必须一起说清楚 —— 否则一个只显示 5 个概念的分布块会被读成
    「其余票没有任何题材」，那是完全相反的结论。

    Args:
        boards: `load_board_concepts` 的返回值。
        symbols: 参与统计的股票（自动去重，通常在去重后的当日名单）。

    Returns:
        `BoardSummary`；三类分布按「只数降序 → 名称升序」排列 ——
        同只数时按名称排序是为了让同一份数据每次渲染顺序一致（可测试、
        也不会在两次运行间无故换位）。
    """
    wanted = list(dict.fromkeys(symbols))
    concept = Counter()
    industry = Counter()
    region = Counter()
    covered = 0
    for symbol in wanted:
        item = boards.get(symbol)
        if item is None or item.is_empty:
            continue
        covered += 1
        concept.update(item.concepts)
        if item.industry:
            industry[item.industry] += 1
        if item.region:
            region[item.region] += 1

    def ranked(counter: Counter) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))

    return BoardSummary(
        total=len(wanted),
        covered=covered,
        concepts=ranked(concept),
        industries=ranked(industry),
        regions=ranked(region),
    )
