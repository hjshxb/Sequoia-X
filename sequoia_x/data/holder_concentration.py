"""十大流通股东合计持股比例（筹码集中度）：全市场快照 + 按报告期落盘缓存。

口径说明
--------
「前十大流通股东占比」= 该股前 10 名流通股东各自「占流通股比例」之和，单位 %。
数值越高说明流通筹码越集中（大股东 / 机构锁仓越多），越低说明越分散。

数据来源
--------
东方财富数据中心 `RPT_F10_EH_FREEHOLDERS`（股东分析-十大流通股东），
全市场按报告期分页返回，字段 `FREE_HOLDNUM_RATIO` 即「占流通股比例」。

成本与缓存（重要）
------------------
该接口是**全市场**接口（约 5.6 万行 / 113 页），不是逐股接口，因此：
    - 首次拉取 ~5 秒（8 线程并发），之后结果按报告期缓存到磁盘；
    - 股东数据按季度披露，一年只更新 4 次，缓存命中后**零网络开销**。
    - 新的报告期尚未披露时，自动回退到上一期，不会拿到空数据。
    - 联网整体失败时，回退到磁盘上已有的旧报告期缓存（记 ERROR 提示数据陈旧）。
    - 磁盘缓存与旧缓存都没有时返回 {}，由调用方把该维度整体关掉（避免误杀成全空）。

缓存文件：`<cache_dir>/top10_free_holding_<YYYY-MM-DD>.json`
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import requests

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CACHE_DIR = "data/cache"

_API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REPORT_NAME = "RPT_F10_EH_FREEHOLDERS"
_PAGE_SIZE = 500
_MAX_WORKERS = 8
_MAX_RANK = 10  # 「前十大」流通股东
_MAX_PAGES = 500  # 安全阀：异常返回超大页数时不要无限循环
_TIMEOUT = 20.0
_RETRIES = 3

# 季末报告期，按年内先后倒序
_QUARTER_ENDS: tuple[tuple[int, int], ...] = ((12, 31), (9, 30), (6, 30), (3, 31))

# 进程内缓存：{(cache_dir, report_date): {symbol: ratio}}
_MEMO: dict[tuple[str, str], dict[str, float]] = {}


def reset_cache() -> None:
    """清空进程内缓存（测试与 `--refresh` 场景使用；磁盘缓存不受影响）。"""
    _MEMO.clear()


# ── 报告期 ──


def normalize_report_date(raw: str) -> str:
    """把 `20260630` / `2026-06-30` 统一成 `2026-06-30`；空串返回空串。"""
    text = (raw or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def report_periods(today: date | None = None) -> list[str]:
    """返回候选报告期（`YYYY-MM-DD`），由新到旧。

    只包含「已到期」的季末；例如 2026-09-19 时不含 2026-09-30。
    含当前年与上一年，共 4~8 个候选，足够覆盖披露延迟。

    Args:
        today: 基准日期，默认今天。

    Returns:
        降序排列的报告期字符串列表。
    """
    today = today or date.today()
    periods: list[str] = []
    for year in (today.year, today.year - 1):
        for month, day in _QUARTER_ENDS:
            candidate = date(year, month, day)
            if candidate <= today:
                periods.append(candidate.isoformat())
    return periods


# ── 聚合 ──


def aggregate(rows: Iterable[Mapping]) -> dict[str, float]:
    """把「一行=一个股东」的原始数据聚合成「只=前十大合计占比」。

    只累加 `HOLDER_RANK <= 10` 的行；代码 / 名次 / 占比任一非法则跳过该行。

    Args:
        rows: 东财接口返回的原始记录。

    Returns:
        {股票代码: 前十大流通股东合计占流通股比例(%)}。
    """
    totals: dict[str, float] = {}
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not code:
            continue
        try:
            rank = int(row.get("HOLDER_RANK"))
        except (TypeError, ValueError):
            continue
        if rank > _MAX_RANK:
            continue
        try:
            ratio = float(row.get("FREE_HOLDNUM_RATIO"))
        except (TypeError, ValueError):
            continue
        if ratio != ratio:  # NaN
            continue
        totals[code] = totals.get(code, 0.0) + ratio
    return totals


# ── 网络 ──


def _request(params: dict) -> dict:
    """带重试的单次请求。"""
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(_API_URL, params=params, timeout=_TIMEOUT)
            return resp.json()
        except Exception as exc:  # 网络抖动 / JSON 解析失败
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("未知请求错误")


def _base_params(report_date: str, page_size: int) -> dict:
    return {
        "sortColumns": "SECURITY_CODE,HOLDER_RANK",
        "sortTypes": "1,1",
        "pageSize": str(page_size),
        "reportName": _REPORT_NAME,
        "columns": "SECURITY_CODE,HOLDER_NAME,HOLDER_RANK,FREE_HOLDNUM_RATIO",
        "source": "WEB",
        "client": "WEB",
        "filter": f"(END_DATE='{report_date}')",
    }


def fetch_market_ratios(
    report_date: str,
    *,
    page_size: int = _PAGE_SIZE,
    workers: int = _MAX_WORKERS,
) -> dict[str, float]:
    """拉取指定报告期的全市场前十大流通股东合计占比。

    Args:
        report_date: 报告期，`YYYY-MM-DD`。
        page_size: 每页行数（东财上限 500）。
        workers: 并发线程数。

    Returns:
        {股票代码: 合计占比(%)}；该报告期无数据时返回 {}。
    """
    base = _base_params(report_date, page_size)
    payload = _request({**base, "pageNumber": "1"})
    result = payload.get("result") or {}
    rows: list[dict] = list(result.get("data") or [])

    try:
        pages = int(result.get("pages") or 0)
    except (TypeError, ValueError):
        pages = 0
    if pages > _MAX_PAGES:
        logger.warning(f"十大流通股东：{report_date} 返回页数 {pages} 异常，截断到 {_MAX_PAGES}")
        pages = _MAX_PAGES

    if pages > 1:

        def _page(number: int) -> list[dict]:
            page_payload = _request({**base, "pageNumber": str(number)})
            return list(((page_payload.get("result") or {}).get("data")) or [])

        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_page, n): n for n in range(2, pages + 1)}
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception as exc:  # 单页失败不影响其余页
                    failed += 1
                    if failed == 1:  # 只报第一页，避免刷屏
                        logger.warning(
                            f"十大流通股东：{report_date} 第 {futures[future]} 页失败 {exc}"
                        )
        if failed:
            logger.warning(
                f"十大流通股东：{report_date} 共 {failed}/{pages} 页失败，结果可能不完整"
            )

    if not rows:
        return {}
    return aggregate(rows)


# ── 磁盘缓存 ──


def cache_path(cache_dir: str, report_date: str) -> Path:
    """返回某报告期的缓存文件路径。"""
    return Path(cache_dir) / f"top10_free_holding_{report_date}.json"


def _read_cache(cache_dir: str, report_date: str) -> dict[str, float] | None:
    path = cache_path(cache_dir, report_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ratios = payload.get("ratios")
        if not isinstance(ratios, dict) or not ratios:
            return None
        return {str(k): float(v) for k, v in ratios.items()}
    except Exception as exc:
        logger.warning(f"十大流通股东：缓存 {path} 读取失败 {exc}")
        return None


def _write_cache(cache_dir: str, report_date: str, ratios: dict[str, float]) -> None:
    path = cache_path(cache_dir, report_date)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "report_date": report_date,
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(ratios),
                    "ratios": ratios,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # 缓存写失败不影响本次结果
        logger.warning(f"十大流通股东：缓存写入失败 {path} {exc}")


def latest_cached_report(cache_dir: str) -> str | None:
    """返回磁盘缓存中最新的报告期，没有则返回 None。"""
    directory = Path(cache_dir)
    if not directory.is_dir():
        return None
    found: list[str] = []
    for path in directory.glob("top10_free_holding_*.json"):
        raw = path.stem.removeprefix("top10_free_holding_")
        if len(raw) == 10 and raw[4] == "-":
            found.append(raw)
    return max(found) if found else None


# ── 对外入口 ──


def load_top10_free_holding(
    *,
    today: date | None = None,
    report_date: str = "",
    cache_dir: str = DEFAULT_CACHE_DIR,
    refresh: bool = False,
) -> dict[str, float]:
    """读取（必要时拉取）前十大流通股东合计占比。

    解析顺序：
        1. 进程内缓存 → 2. 磁盘缓存（候选报告期由新到旧）→ 3. 联网拉取
        → 4. 磁盘上最新的旧报告期缓存（数据陈旧，记 ERROR）→ 5. 返回 {}。

    Args:
        today: 基准日期，默认今天。
        report_date: 指定报告期（`YYYY-MM-DD` 或 `YYYYMMDD`）；留空则自动推算。
        cache_dir: 缓存目录。
        refresh: 忽略既有缓存，强制联网拉取。

    Returns:
        {股票代码: 前十大流通股东合计占流通股比例(%)}；完全不可用时返回 {}。
    """
    pinned = normalize_report_date(report_date)
    candidates = [pinned] if pinned else report_periods(today)

    if not refresh:
        for candidate in candidates:
            key = (cache_dir, candidate)
            if key in _MEMO:
                return _MEMO[key]
            cached = _read_cache(cache_dir, candidate)
            if cached:
                logger.info(f"十大流通股东：命中缓存 {candidate}，覆盖 {len(cached)} 只")
                _MEMO[key] = cached
                return cached

    for candidate in candidates:
        try:
            ratios = fetch_market_ratios(candidate)
        except Exception as exc:
            logger.warning(f"十大流通股东：{candidate} 拉取失败 {exc}")
            ratios = {}
        if not ratios:
            continue
        _write_cache(cache_dir, candidate, ratios)
        _MEMO[(cache_dir, candidate)] = ratios
        logger.info(f"十大流通股东：{candidate} 拉取成功，覆盖 {len(ratios)} 只")
        return ratios

    stale_date = latest_cached_report(cache_dir)
    if stale_date:
        stale = _read_cache(cache_dir, stale_date)
        if stale:
            logger.error(
                f"十大流通股东：最新报告期不可用，回退到旧缓存 {stale_date}（数据可能陈旧）"
            )
            _MEMO[(cache_dir, stale_date)] = stale
            return stale

    logger.error("十大流通股东：无可用缓存且数据源不可用，该维度将被跳过")
    return {}
