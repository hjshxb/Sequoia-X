"""估值 / 换手的本地缓存：把 baostock 的 PE、PB、换手率、流通市值按「(股票, 交易日)」落库。

**为什么要落库**（2026-10-23 的实际事故）：
    精筛次段要逐股去 baostock 取 PE / PB / 换手（200 只 ≈ 一分钟的串行查询）。
    同一晚连跑几次就会重复取几次；更糟的是**批量取数会偶发失败** ——
    那晚有一次 82/200（41%）没取到，而精筛的策略是「估值缺失就整只剔除」，
    池子当场从 106 缩到 60，选股数 44 → 24。数据源抖一下，选股就少一半。

**为什么只缓存候选股、不缓存全市场**：
    日线同步对全市场用的是 `adjustflag=1`（后复权），而 PE / 市值必须用
    **不复权**真实价格（见 `_fetch_from_baostock` 的注释）。若为了估值把同步改成
    不复权，或每只票发两次请求，5221 只 × 33 分钟的同步会翻倍。
    候选项每天只有 ~200 只，缓存它们既够用又便宜。

**粒度是「股票 + 交易日」**：PE 每天变，跨日复用会算错涨跌，
所以按日隔离；同一天内多次运行（重算报告、改阈值重跑）才能命中。

不做 TTL：key 里已经含日期，过期数据永远不会被读到。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

TABLE = "stock_valuation"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,
    pe_ttm          REAL,
    pb              REAL,
    turn            REAL,
    circ_market_cap REAL,
    PRIMARY KEY (symbol, date)
);
"""

# 写库时等锁的超时。同步进程可能正在写同一个库，短超时比整轮崩掉好。
_TIMEOUT = 5.0


def ensure_schema(db_path: str) -> None:
    """建表（幂等）。调用方不必关心表是否存在。"""
    try:
        with sqlite3.connect(db_path, timeout=_TIMEOUT) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 缓存失败不能阻断主流程
        logger.warning(f"估值缓存：建表失败，本次不走缓存：{exc}")


def load(db_path: str, on_date: str, symbols: Sequence[str]) -> dict[str, dict[str, float | None]]:
    """读某一天已缓存的估值；{symbol: {pe_ttm, pb, turn, circ_market_cap}}。

    任何异常都退化成「没缓存」—— 缓存是加速手段，不是正确性依赖。
    """
    if not symbols:
        return {}
    try:
        with sqlite3.connect(db_path, timeout=_TIMEOUT) as conn:
            conn.execute(_CREATE_TABLE_SQL)  # 首次运行时建表
            placeholders = ",".join("?" * len(symbols))
            rows = conn.execute(
                f"SELECT symbol, pe_ttm, pb, turn, circ_market_cap FROM {TABLE} "
                f"WHERE date = ? AND symbol IN ({placeholders})",
                (on_date, *symbols),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"估值缓存：读取失败，本次全部走网络：{exc}")
        return {}

    return {
        str(row[0]): {
            "pe_ttm": row[1],
            "pb": row[2],
            "turn": row[3],
            "circ_market_cap": row[4],
        }
        for row in rows
    }


def load_latest(
    db_path: str, symbols: Sequence[str], since: str
) -> dict[str, tuple[str, dict[str, float | None]]]:
    """读每只票**最近一次**缓存的估值（不限当天），返回 {symbol: (date, values)}。

    用途只有一个：**兜底**。2026-10-26 那次 baostock 登录失败、174/174 全取不到，
    估值维度被整体跳过 —— 市值 / PE / 换手率三条门槛同时失效，池子 174→174，
    小市值票（鹿山新材 38 亿）直接进了榜单，而报告页首仍写着「流通市值 >=100亿」。

    这里用「最近 N 天内的一条」来判市值/PE：
      · 市值 = 价格 × 流通股本，几天内变动有限，用来判「是否 >=100亿」够用；
      · PE 只用于 `MIN_PE=0` 这类粗过滤，旧值也可接受。
    ⚠️ 但它**不能**替代当天的换手率（换手率是当日量能，跨日复用会算错）。

    Args:
        since: 最早可接受的日期（含），一般传「今天 - N 天」。
    """
    if not symbols:
        return {}
    try:
        with sqlite3.connect(db_path, timeout=_TIMEOUT) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            placeholders = ",".join("?" * len(symbols))
            rows = conn.execute(
                f"SELECT symbol, date, pe_ttm, pb, turn, circ_market_cap FROM {TABLE} "
                f"WHERE date >= ? AND symbol IN ({placeholders}) "
                f"ORDER BY symbol, date DESC",
                (since, *symbols),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"估值缓存：读取最近记录失败：{exc}")
        return {}

    out: dict[str, tuple[str, dict[str, float | None]]] = {}
    for row in rows:  # 已按 date DESC，每只票第一条就是最近的
        symbol = str(row[0])
        if symbol in out:
            continue
        out[symbol] = (
            str(row[1]),
            {"pe_ttm": row[2], "pb": row[3], "turn": row[4], "circ_market_cap": row[5]},
        )
    return out


def save(db_path: str, on_date: str, rows: Mapping[str, Mapping[str, float | None]]) -> int:
    """写入某一天的估值，返回写入条数。失败只记 WARNING（下轮重取）。"""
    if not rows:
        return 0
    payload = [
        (
            symbol,
            on_date,
            values.get("pe_ttm"),
            values.get("pb"),
            values.get("turn"),
            values.get("circ_market_cap"),
        )
        for symbol, values in rows.items()
    ]
    try:
        with sqlite3.connect(db_path, timeout=_TIMEOUT) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.executemany(
                f"INSERT OR REPLACE INTO {TABLE} "
                f"(symbol, date, pe_ttm, pb, turn, circ_market_cap) VALUES (?,?,?,?,?,?)",
                payload,
            )
            conn.commit()
        return len(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"估值缓存：写入失败（{len(payload)} 条），下轮会重新取：{exc}")
        return 0
