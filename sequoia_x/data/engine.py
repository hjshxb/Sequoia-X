"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


class BaostockUnavailable(RuntimeError):
    """baostock 整体不可用：登录被拒（如被风控拉黑）或整批查询全部失败。

    刻意用独立异常类型而不是返回空列表 —— 空列表会被上层当成「非交易日、无新数据」，
    于是拿旧数据照跑策略并推送，把数据源故障伪装成正常结果。
    """


class SyncIncomplete(RuntimeError):
    """本次同步没能凑出「当日可用」的数据（未更新占比超阈值，或库为空）。

    与 `BaostockUnavailable` 分开，是因为排障方向不同：前者查网络与风控，
    这个通常是个别节点异常、上游只发布了一部分，或本地库还没回填过。
    上层的处理相同 —— 中止本次推送，绝不拿「一半是旧数据」的结果冒充当日选股结果。
    """


@dataclass(frozen=True)
class SyncStats:
    """一次增量同步的结果 —— 让上层能判断「这批数据够不够出结果」。

    语义刻意拆成几个数，而不是只回一个「写入行数」：写入行数无法区分
    「全市场本来就无需更新」（正常）和「发了 5000 只请求却一行没拿到」（故障）。
    旧实现把两者都表示成 `return 0`，于是数据源当天没发布时，主流程会拿
    上一交易日的数据照跑策略、并推一张日期写着今天的卡片。

    Attributes:
        requested: 本次判定**需要更新**的股票数（本地最新日期 < 今天的那些）。
        updated: 实际拿到新行的股票数。
        failed: 查询直接报错的股票数（是 `missing` 的一部分，另一部分是
            「查询成功但当天无行情」，典型是停牌）。
        rows: 实际写入数据库的**行数**。长休市后一只股票可能补多天，
            所以行数 ≥ 股票数。
    """

    requested: int
    updated: int
    failed: int
    rows: int

    @property
    def missing(self) -> int:
        """未拿到新行的股票数：既含查询报错的，也含查得到但当天无行的（停牌）。"""
        return max(0, self.requested - self.updated)

    @property
    def missing_ratio(self) -> float:
        """未更新占比；本次无需更新（requested == 0）时为 0。"""
        return self.missing / self.requested if self.requested else 0.0


def _bs_fetch_batch(tasks: list) -> tuple[list, int]:
    """同步 worker：独立 login，批量拉取 baostock 数据。

    单进程模式下由 `sync_today_bulk` 直接调用（整份任务一次传入）；
    多进程模式下作为 `Pool.map` 的任务函数，每个分片各起一个进程。

    Returns:
        `(rows, failed)`：成功取到的行，以及查询直接报错的股票数。
        把 `failed` 一并回传（而不是就地打个 warning 丢掉），是因为
        「本次数据完不完整」必须由**全市场**维度判断，而单个分片看不到全局。

    Raises:
        BaostockUnavailable: 登录失败，或本批查询**全部**失败。
            登录失败必须立即抛出、一只都不许查 —— 旧实现丢弃了 `bs.login()`
            的返回值，照样给整批股票各发一次注定失败的请求，把一次故障放大成
            「全市场重试风暴」，最终换来 10001011 黑名单（历史事故的放大器）。
    """
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise BaostockUnavailable(
            f"baostock 登录失败: {lg.error_code} {lg.error_msg}"
            f"（{len(tasks)} 只待拉取，已放弃，未发出任何行情请求）"
        )

    results = []
    failed = 0
    for symbol, bs_code, start, end in tasks:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )
        if rs.error_code != "0":
            failed += 1
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()

    # 个别股票查不到（退市 / 长期停牌 / 新代码）是正常的，跳过即可；
    # 但「整批全部失败」只可能是数据源出了问题，不能伪装成「无新数据」。
    if failed and failed == len(tasks):
        raise BaostockUnavailable(
            f"baostock 本批 {len(tasks)} 只股票查询全部失败，判定为数据源故障"
        )
    if failed:
        logger.warning(f"本批 {len(tasks)} 只中有 {failed} 只查询失败，已跳过")

    return results, failed


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        # 增量同步的并发进程数，默认 1（单进程串行）。见 config.MAX_SYNC_WORKERS 说明。
        self.sync_workers: int = settings.sync_workers
        # 单次同步允许的「未更新占比」上限，超过即中止本次推送。见 config 里该字段的说明。
        self.sync_max_fail_ratio: float = settings.sync_max_fail_ratio
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    # SQLite 的绑定变量上限默认约 999（老版本）/ 32766（新版本），
    # 全市场 5000+ 只股票一次性 IN 查询会超限，因此分批执行。
    _SQL_PARAM_CHUNK = 900

    def get_latest_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """批量取每只股票「最新一个交易日」的快照行。

        用于技术面过滤（如成交额）与股票池预筛，避免为了一行数据把整段历史都读出来。
        用窗口函数一次查完，配合 (symbol, date) 索引，几十到几百只股票毫秒级返回；
        传入全市场 5000+ 只时代码会自动分批（见 `_SQL_PARAM_CHUNK`）。

        Args:
            symbols: 纯数字股票代码列表。

        Returns:
            {symbol: {"date": str, "close": float|None, "turnover": float|None}}；
            无数据的股票不在字典中。
        """
        if not symbols:
            return {}

        sql = """
            SELECT symbol, date, close, turnover FROM (
                SELECT symbol, date, close, turnover,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM stock_daily
                WHERE symbol IN ({placeholders})
            ) WHERE rn = 1
        """

        result: dict[str, dict] = {}
        chunk = self._SQL_PARAM_CHUNK
        with sqlite3.connect(self.db_path) as conn:
            for i in range(0, len(symbols), chunk):
                batch = symbols[i : i + chunk]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(sql.format(placeholders=placeholders), batch).fetchall()
                for row in rows:
                    result[row[0]] = {"date": row[1], "close": row[2], "turnover": row[3]}
        return result

    def get_market_latest_date(self) -> str | None:
        """全市场最新交易日（`MAX(date)`），作为「今日」的判定基准。

        刻意取**全市场**最大值、而不是某只股票自己的最大值：停牌股的最后一行
        可能比全市场早好几天，若按逐股取值，就会把停牌前的旧涨跌幅当成今日涨跌幅。

        Returns:
            最新交易日（YYYY-MM-DD）；库为空时返回 None。
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_recent_rows(self, symbols: list[str], rows: int = 2) -> dict[str, list[dict]]:
        """批量取每只股票**最近 N 个交易日**的收盘行情。

        供「今日跌幅」这类需要「今收 vs 昨收」的判据使用：比逐股 `get_ohlcv()`
        少读几十倍数据，全市场由一次窗口函数查完（超过 900 只自动分批）。

        Args:
            symbols: 纯数字股票代码列表。
            rows: 每只股票取几行，默认 2（今日 + 昨日）。

        Returns:
            {symbol: [按日期**升序**排列的行]}，行内含 `date` / `close`；
            即 `[-1]` 是最新一行、`[-2]` 是前一行。无数据的股票不在字典中。
        """
        if not symbols or rows < 1:
            return {}

        sql = """
            SELECT symbol, date, close FROM (
                SELECT symbol, date, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM stock_daily
                WHERE symbol IN ({placeholders})
            ) WHERE rn <= ? ORDER BY symbol, date
        """

        result: dict[str, list[dict]] = {}
        chunk = self._SQL_PARAM_CHUNK
        with sqlite3.connect(self.db_path) as conn:
            for i in range(0, len(symbols), chunk):
                batch = symbols[i : i + chunk]
                placeholders = ",".join("?" * len(batch))
                for row in conn.execute(sql.format(placeholders=placeholders), [*batch, rows]):
                    result.setdefault(row[0], []).append({"date": row[1], "close": row[2]})
        return result

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> SyncStats:
        """通过 baostock 拉取增量数据（后复权），写入 SQLite。

        并发度由 `SYNC_WORKERS` 控制，**默认 1（单进程串行）**。
        历史上 8 进程并发拉全市场曾触发 baostock 风控黑名单（10001011），
        故默认保守；需要提速时再手动调大 `SYNC_WORKERS`。

        三种出口，上层据此决定「这批数据够不够出当日结果」：

        1. **正常返回 `SyncStats`**：本次无需更新（`requested == 0`），
           或已拿到新数据且未更新占比在阈值内（`sync_max_fail_ratio`）。
        2. **抛 `BaostockUnavailable`**：登录被拒 / 整批查询全失败 /
           发了 N 只请求却一行没拿到 —— 数据源没就绪。**未改动数据库。**
        3. **抛 `SyncIncomplete`**：拿到了一部分，但未更新占比超阈值 ——
           数据不完整。**未改动数据库。**

        2、3 两种都必须在写库**之前**抛出：绝不能先落一半数据、再让上层
        基于「一半是旧数据」的库跑策略。库为空（未做过 `--backfill`）同样
        归入第 3 类 —— 没有任何可用数据时推报告毫无意义。
        """
        from datetime import date, timedelta

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            raise SyncIncomplete(
                "本地行情库为空，无法判断当日数据是否就绪；请先执行 --backfill 完成首次回填"
            )

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return SyncStats(requested=0, updated=0, failed=0, rows=0)

        # 并发度：配置值 与 待更新股票数 取小，避免创建空分片。
        # 单进程时刻意不进 multiprocessing.Pool —— 省掉 fork 与任务序列化，
        # 且 baostock 的连接与报错都留在主进程，排障时日志是一条完整时间线。
        n_workers = max(1, min(self.sync_workers, len(tasks)))

        # `failed` 必须跨分片累加后再判断：单个分片只看得到自己那几千只，
        # 「本次数据完不完整」是**全市场**维度的结论（见下面的占比判定）。
        all_rows: list = []
        failed = 0
        try:
            if n_workers == 1:
                logger.info(f"需要更新 {len(tasks)} 只股票，单进程串行拉取...")
                all_rows, failed = _bs_fetch_batch(tasks)
            else:
                from multiprocessing import Pool

                logger.info(f"需要更新 {len(tasks)} 只股票，启动 {n_workers} 进程并行拉取...")
                chunks = [tasks[i::n_workers] for i in range(n_workers)]
                with Pool(n_workers) as pool:
                    batch_results = pool.map(_bs_fetch_batch, chunks)
                for batch_rows, batch_failed in batch_results:
                    all_rows.extend(batch_rows)
                    failed += batch_failed
        except BaostockUnavailable as exc:
            # 数据源不可用必须显式失败并终止主流程，绝不能返回 0 ——
            # 返回 0 会让上层拿上一交易日的旧数据照跑策略并推送，
            # 把「拿不到数据」伪装成「今天没有信号」。
            logger.error(f"baostock 不可用，本次增量同步中止（未改动数据库）：{exc}")
            raise

        # 「需要更新 N 只，却一行都没拿到」只可能是数据源当天还没发布（或整体异常），
        # 绝不等于「今天没有信号」。旧实现这里只 log 一句就 `return 0`，主流程便把
        # 上一交易日的数据当成今日结果跑了策略并推送 —— 卡片日期还写着今天。
        # 注意：这与上面 `if not tasks` 是两件事，那个才是真正的「无需更新、正常」。
        if not all_rows:
            raise BaostockUnavailable(
                f"需要更新 {len(tasks)} 只股票，但一条新数据都没取到"
                "（数据源尚未发布当日行情 / 今天不是交易日 / 整体异常）；"
                "判定为数据未就绪，本次不生成报告、不推送"
            )

        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        # 用「拿到新行的**股票**数」而不是「查询报错的股票数」来算未更新占比：
        # 停牌股查询是成功的、只是当天没有行情，同样是「这一只没更新」。
        # 占比口径只在 `SyncStats` 里写一次，这里的判定与上层的展示同源。
        count = len(df)
        stats = SyncStats(
            requested=len(tasks), updated=df["symbol"].nunique(), failed=failed, rows=count
        )
        if stats.missing_ratio > self.sync_max_fail_ratio:
            raise SyncIncomplete(
                f"{stats.requested} 只待更新，其中 {stats.missing} 只未拿到新数据"
                f"（{stats.missing_ratio:.1%}，超过上限 {self.sync_max_fail_ratio:.1%}）"
                f"，判定为数据不完整；未改动数据库，本次不生成报告、不推送"
            )

        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql(
                "stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500
            )
            conn.commit()

        if stats.missing:
            logger.warning(
                f"本次有 {stats.missing} 只未拿到新数据"
                f"（{stats.missing_ratio:.1%}），已按上限内继续"
            )
        logger.info(f"sync_today_bulk: 写入 {count} 条数据（覆盖 {stats.updated} 只股票）")
        return stats

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily",
                            conn,
                            if_exists="append",
                            index=False,
                            method="multi",
                            chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]  # "sh.600000" or "sz.000001"
                status = row[4]  # "1" = 上市
                stock_type = row[5]  # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])  # 提取纯数字代码
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM stock_daily").fetchall()
        return [row[0] for row in rows]
