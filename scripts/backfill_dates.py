"""按**日期**定向补洞：把缺失交易日的全市场行情补回本地库。

背景：`engine.sync_today_bulk()` 的个股窗口是「本地最后日期 + 1 天起」，所以
**中段的洞永远不会自愈** —— 绝大多数股票的 last_date 已是今天，9/24 那种夹在
中间的空白不会被任何一次日常同步碰到。必须显式回补。

为什么**不要**跑 `main.py --backfill`：

    那个是**按股票**回填，且 `last_date >= today` 的股票一律 skip（engine.py 里
    `backfill()` 的第 555 行附近）。而洞的特征恰恰是「绝大多数股票不缺、只是某几天
    全市场都没写进去」⇒ 全量回填会跳过 99% 的股票、一行也修不回来，还要白跑几小时
    （且它是按股票全历史取数，5221 只 × 665 天）。方向反了。

本脚本的方向是**按日期**出发：每只股票发**一次**请求取回 [最早缺失日, 最晚缺失日]
区间（区间里的周末/节假日本来就没有 K 线，不会被误判成缺数据）。耗时 = 一次每日
同步的量级（全市场单进程约 30~60 分钟），与缺几天无关。

幂等 & 可续跑：写入走 `DataEngine.write_daily_rows()`（按 (symbol, date) 精确删 +
append），且**已拥有全部目标日期的股票直接跳过** —— 中途断了再跑一次即可，不会
重复请求，也不会产生重复行。

用法（WSL）：
    S=/home/hxb/miniconda3/envs/sequoia-x/bin/python
    $S scripts/backfill_dates.py --dry-run                 # 只看要补哪些日期、多少只
    $S scripts/backfill_dates.py --limit 20                # 小样本自检（20 只）
    $S scripts/backfill_dates.py                           # 自动检测洞 + 全市场补
    $S scripts/backfill_dates.py --dates 2026-09-24 2026-09-28
    $S scripts/backfill_dates.py --use-calendar            # 连「整日缺失」一起查
    $S scripts/backfill_dates.py --chunk 500               # 每批股票数（默认 500）
    $S scripts/backfill_dates.py --workers 2               # 提速，baostock 有风控，慎用

退出码：0 = 补完且闸门放行；1 = 补完但闸门仍拦截（还有别的洞）；2 = 参数/环境错误。
"""

import argparse
import io
import sqlite3
import sys
from contextlib import redirect_stdout
from datetime import date, timedelta
from functools import partial
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.core.logger import get_logger  # noqa: E402
from sequoia_x.data.engine import BaostockUnavailable, DataEngine  # noqa: E402

logger = get_logger("backfill_dates")

# `IN (...)` 的占位符上限（SQLite 默认 999 个变量，留点余量）。
_SQL_CHUNK = 900


def detect_thin_dates(engine: DataEngine, min_ratio: float = 0.9) -> list[str]:
    """从闸门口径里读出「行数明显偏少」的交易日（升序）。

    与 `assert_recent_dates_complete` 同一判据、同一窗口 —— 补洞的范围就是闸门
    报警的范围，否则会出现「补完了闸门还在拦」或「闸门没拦但数据是错的」。
    """
    counts = engine.get_recent_date_counts(engine.required_history_days())
    if len(counts) < 2:
        return []
    reference = max(count for _, count in counts)
    return sorted(d for d, count in counts if count < reference * min_ratio)


def detect_missing_calendar_days(engine: DataEngine) -> list[str]:
    """比对 baostock 交易日历，找出**整日缺失**的交易日（升序）。

    行数判据看不见这类洞 —— 某天若是 0 行，它根本不出现 `GROUP BY date` 的结果里。
    而「整个交易日缺失」同样会让涨跌幅变成跨多日口径，正是要防的那类错。

    刻意排除今天：当天尚未同步不等于缺数据（那是 `sync_today_bulk` 的事）。
    """
    import baostock as bs

    today = date.today().isoformat()
    # 窗口含 required 个交易日，按 1.5 倍自然日换算后再留 10 天余量。
    start = (
        date.today() - timedelta(days=int(engine.required_history_days() * 1.5) + 10)
    ).isoformat()

    with redirect_stdout(io.StringIO()):  # login() 会自己 print，别污染日志
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败，跳过日历核对：{lg.error_msg}")
            return []
        calendar: list[str] = []
        rs = bs.query_trade_dates(start_date=start, end_date=today)
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if row[1] == "1":
                calendar.append(row[0])
        bs.logout()

    with sqlite3.connect(engine.db_path) as conn:
        have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM stock_daily")}
    return sorted(d for d in calendar if d not in have and d < today)


def _listing_first_dates(engine: DataEngine) -> dict[str, str]:
    """每只股票在库内的首个日期 —— 充当它「上市日」的近似。

    只取**下界**（最早日期），刻意不取最晚日期：
    - 上市日晚于目标日 ⇒ 那些日期它本来就没有数据，数据源也拿不回来。这是**单向的
      既成事实**，可以安全用来少发请求。
    - 「最晚日期早于目标日」却**不能**反过来当上界：某股数据只到 9/25，既可能是退市，
      也可能正是缺数据 —— 拿它当上界会把「缺最后一天」的股票整只跳过，而那恰恰是
      要补的洞。宁可多问几只退市股（数据源返回空、写 0 行），也不能漏补一只。
    """
    with sqlite3.connect(engine.db_path) as conn:
        return dict(conn.execute("SELECT symbol, MIN(date) FROM stock_daily GROUP BY symbol"))


def symbols_lacking_dates(engine: DataEngine, symbols: list[str], dates: list[str]) -> list[str]:
    """筛出「上市日之后还缺目标日期」的股票 —— 命中齐全的直接跳过（续跑的关键）。

    只把**上市日当天及之后**的目标日期计入需求：早于上市日的日期它本来就没有数据，
    计入需求只会让每次续跑都重复问一遍，而且这个计数永远降不到 0（实测把 2026-08-28
    当洞时，13 只待补里有 9 只是 9 月才上市的新股）。`need == 0` 的股票直接不发请求。

    注意必须**按日期裁**，不能「整只股票排除」：某股 9/25 上市、要补的是 9/24~9/28
    时，它 9/28 的数据是真缺的（`need == 1`），整只跳过就会漏补。

    ⚠️ `MIN(date)` 只是库内近似 —— 若某只股票整体历史都缺，它看起来也会像新股。
    所以这里只用于**少发请求**，补洞的成败始终以 `assert_recent_dates_complete()`
    （按日行数的完整性闸门）为准，不以本函数的返回是否为空为准。
    """
    first_date = _listing_first_dates(engine)
    need: dict[str, int] = {}
    for s in symbols:
        listing = first_date.get(s, "")
        # 库内查不到这只票（不该发生）时 listing 为空串，`d >= ""` 恒真 ⇒ 保守地全都要。
        need[s] = sum(1 for d in dates if d >= listing)

    candidates = [s for s in symbols if need[s] > 0]
    skipped = len(symbols) - len(candidates)
    if skipped:
        logger.info(f"{skipped} 只股票的目标日期全早于上市日（本来就没有数据），已跳过")

    placeholders_date = ",".join("?" * len(dates))
    lacking: list[str] = []
    with sqlite3.connect(engine.db_path) as conn:
        for i in range(0, len(candidates), _SQL_CHUNK):
            batch = candidates[i : i + _SQL_CHUNK]
            placeholders_symbol = ",".join("?" * len(batch))
            sql = (
                "SELECT symbol, COUNT(DISTINCT date) FROM stock_daily "
                f"WHERE date IN ({placeholders_date}) AND symbol IN ({placeholders_symbol}) "
                "GROUP BY symbol"
            )
            got = dict(conn.execute(sql, [*dates, *batch]))
            lacking.extend(s for s in batch if got.get(s, 0) < need[s])
    return lacking


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按日期回补缺失交易日的全市场行情")
    parser.add_argument("--dates", nargs="+", default=None, help="要补的日期，如 2026-09-24")
    parser.add_argument(
        "--use-calendar",
        action="store_true",
        help="额外比对 baostock 交易日历，把「整日缺失」的交易日也算进来",
    )
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只股票（自检用）")
    parser.add_argument("--chunk", type=int, default=500, help="每批股票数（每批各自 login）")
    parser.add_argument("--workers", type=int, default=1, help="并发进程数，默认 1（风控保守）")
    parser.add_argument("--dry-run", action="store_true", help="只报告要补什么，不发请求")
    return parser.parse_args(argv)


def _to_frame(rows: list) -> pd.DataFrame:
    """把 baostock 行整理成 `write_daily_rows` 需要的列（与增量同步同口径清洗）。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
    )
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["volume"] > 0]
    return df


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    engine = DataEngine(settings)

    before = dict(engine.get_recent_date_counts(engine.required_history_days()))

    if args.dates:
        targets = sorted(args.dates)
        logger.info(f"使用显式指定的日期：{targets}")
    else:
        targets = detect_thin_dates(engine)
        logger.info(f"按闸门口径自动检测到 {len(targets)} 个缺失交易日：{targets}")

    if args.use_calendar:
        extra = detect_missing_calendar_days(engine)
        new = [d for d in extra if d not in targets]
        if new:
            logger.info(f"日历比对额外发现整日缺失：{new}")
            targets = sorted(set(targets) | set(new))

    if not targets:
        logger.info("没有检测到缺失的交易日 —— 库内完整性闸门应当已放行")
        return 0

    symbols = engine.get_local_symbols()
    if args.limit:
        symbols = symbols[: args.limit]
    todo = symbols_lacking_dates(engine, symbols, targets)
    logger.info(
        f"目标日期 {targets[0]}~{targets[-1]}（{len(targets)} 天）："
        f"本地 {len(symbols)} 只股票中有 {len(todo)} 只需要补"
    )

    if args.dry_run:
        logger.info(
            f"[dry-run] 预计发出 {len(todo)} 次查询（每只 1 次，取 "
            f"{targets[0]}~{targets[-1]} 区间）。按实测 0.4~1.4s/只估（区间查询比每日"
            f"同步的单日查询便宜，全量批次均值 ≈0.4s/只），单进程约 "
            f"{len(todo) * 0.4 / 3600:.1f}~{len(todo) * 1.4 / 3600:.1f} 小时"
        )
        return 0

    if not todo:
        logger.info("所有股票都已具备目标日期，无需查询")
        return 0

    fetch = partial(engine.fetch_daily_range, start=targets[0], end=targets[-1])
    written = 0
    updated_symbols = 0
    failed_total = 0
    chunk = max(1, args.chunk)
    batches = [todo[i : i + chunk] for i in range(0, len(todo), chunk)]

    for idx, batch in enumerate(batches, start=1):
        try:
            if args.workers > 1:
                from multiprocessing import Pool

                shards = [batch[i :: args.workers] for i in range(args.workers)]
                with Pool(args.workers) as pool:
                    results = pool.map(fetch, shards)
                rows = [r for res in results for r in res[0]]
                failed = sum(res[1] for res in results)
            else:
                rows, failed = fetch(batch)
        except BaostockUnavailable as exc:
            logger.error(f"数据源不可用，已中止（已写入的批次不会丢，重跑即续传）：{exc}")
            return 2

        failed_total += failed
        df = _to_frame(rows)
        if not df.empty:
            written += engine.write_daily_rows(df)
            updated_symbols += df["symbol"].nunique()
        logger.info(
            f"批次 {idx}/{len(batches)}：本批 {len(batch)} 只 → 写入 {len(df)} 行"
            f"（累计 {written} 行 / {updated_symbols} 只，失败 {failed_total} 只）"
        )

    logger.info("回补完成，复核闸门口径：")
    after = dict(engine.get_recent_date_counts(engine.required_history_days()))
    for d in targets:
        logger.info(f"  {d}: {before.get(d, 0)} 行 → {after.get(d, 0)} 行")

    from sequoia_x.data.engine import MarketHistoryIncomplete

    try:
        engine.assert_recent_dates_complete()
    except MarketHistoryIncomplete as exc:
        logger.error(f"仍有洞，闸门继续拦截：{exc}")
        return 1
    logger.info("✅ 库内完整性闸门已放行，可重跑 main.py 出报告")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
