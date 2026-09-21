"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
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


def _bs_fetch_batch(tasks: list) -> list:
    """同步 worker：独立 login，批量拉取 baostock 数据。

    单进程模式下由 `sync_today_bulk` 直接调用（整份任务一次传入）；
    多进程模式下作为 `Pool.map` 的任务函数，每个分片各起一个进程。
    """
    import baostock as bs

    bs.login()
    results = []
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
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        # 增量同步的并发进程数，默认 1（单进程串行）。见 config.MAX_SYNC_WORKERS 说明。
        self.sync_workers: int = settings.sync_workers
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

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """通过 baostock 拉取增量数据（后复权），写入 SQLite。

        并发度由 `SYNC_WORKERS` 控制，**默认 1（单进程串行）**。
        历史上 8 进程并发拉全市场曾触发 baostock 风控黑名单（10001011），
        故默认保守；需要提速时再手动调大 `SYNC_WORKERS`。
        """
        from datetime import date, timedelta

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        # 并发度：配置值 与 待更新股票数 取小，避免创建空分片。
        # 单进程时刻意不进 multiprocessing.Pool —— 省掉 fork 与任务序列化，
        # 且 baostock 的连接与报错都留在主进程，排障时日志是一条完整时间线。
        n_workers = max(1, min(self.sync_workers, len(tasks)))

        if n_workers == 1:
            logger.info(f"需要更新 {len(tasks)} 只股票，单进程串行拉取...")
            all_rows = _bs_fetch_batch(tasks)
        else:
            from multiprocessing import Pool

            logger.info(f"需要更新 {len(tasks)} 只股票，启动 {n_workers} 进程并行拉取...")
            chunks = [tasks[i::n_workers] for i in range(n_workers)]
            with Pool(n_workers) as pool:
                batch_results = pool.map(_bs_fetch_batch, chunks)
            all_rows = [row for batch in batch_results for row in batch]

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql(
                "stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500
            )
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

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
