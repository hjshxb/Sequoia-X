"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import contextlib
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import REQUIRED_HISTORY_DAYS, Settings
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


class MarketHistoryIncomplete(SyncIncomplete):
    """本地库里**最近的交易日**不完整（某天只有个位数行），数据本身有洞。

    与 `SyncIncomplete` 分开是因为**判据完全不同**：那个看「本次同步拿到多少」，
    这个看「库里已存的最近几天全不全」，而且后者**发生在写库之后**（所以不适用
    「未改动数据库」那句话）。继承 `SyncIncomplete` 是为了让上层沿用同一个出口。

    为什么必须单独查：写库是按日期组织的，某一天被整列清空后 `MAX(date)` 依然正常、
    「数据日期」也照常显示今天，**只有按日行数能看出问题**。而「上一交易日」会因此
    悄悄前移（2026-09-29 事故：9/28 只剩 1 行 ⇒ 全市场当日涨跌幅变成 2 日口径，
    000513 在主板写出 +16.23%）。宁可当天不出结果，也不播报口径错误的数据。
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
        universe: 库内全市场股票数 —— 未更新占比的**分母**，见 `missing_ratio`。
            由调用方（`sync_today_bulk`）给出；手写构造时不传则退回 `requested`。
    """

    requested: int
    updated: int
    failed: int
    rows: int
    universe: int = 0

    @property
    def missing(self) -> int:
        """未拿到新行的股票数：既含查询报错的，也含查得到但当天无行的（停牌）。"""
        return max(0, self.requested - self.updated)

    @property
    def missing_ratio(self) -> float:
        """未更新占比 = 未拿到新数据的股票数 ÷ 本次**应覆盖的全市场股票数**。

        分母取 `max(requested, universe)`，**刻意不能就用 `requested`**：
        `requested` 只是「本地最新日期 < 今天」的那部分股票，同一天重跑时会塌缩成
        少数几只长期停牌股。拿它当分母，这几只停牌股就会算出 100%，
        把一次完全正常的重跑判成「数据不完整」并中止主流程。
        实测（2026-09-29）：18:30 首次跑 12/5221 = 0.2% 通过；
        20:36 回补后重跑 12/12 = 100% 被拦，报告出不来。
        `universe` 是库内全市场股票数，两次跑的分母一致，占比才可比。
        回归测试：tests/test_data_engine.py（measured_against_the_whole_universe）。

        `universe` 未提供（`0`，仅手写构造的调用点）时退回 `requested`，
        与旧行为一致 —— 真实同步路径一定带上它。
        """
        denominator = max(self.requested, self.universe)
        return self.missing / denominator if denominator else 0.0


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
        # 精筛的均线窗口（交易日数）。引擎自己不筛股，但要靠它算「库内完整性闸门」
        # 该查多长的历史 —— MA120 的窗口也是每日同步不会回补的那一段（见
        # required_history_days）。同 sync_* 字段，这里按值拷贝一份。
        self.ma_window: int = settings.ma_window
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

    def get_recent_date_counts(self, days: int = 10) -> list[tuple[str, int]]:
        """取库内最近 `days` 个**有数据的交易日**各自的行情行数。

        一次 `GROUP BY date` 走完，用于回答「库里的历史是不是有洞」——
        逐股看 `MAX(date)` 是看不出来的：写库按日期组织，某一天被整列清空后，
        每只股票的最后一个日期照样正常，只有「按日行数」会掉到个位数。

        Args:
            days: 往回取几个交易日（按日期降序，跳过无数据的日期）。

        Returns:
            `[("2026-09-29", 5209), ("2026-09-28", 5208), ...]`，日期降序；
            库为空或 `days < 1` 时返回 []。
        """
        if days < 1:
            return []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT date, COUNT(*) FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def required_history_days(self) -> int:
        """本次运行真正会读到的最大交易日跨度 —— 完整性闸门的检查窗口。

        窗口不按「今天 + 昨天」这种局部需求取，而按**全系统消费者里最长的那条
        回看链**取（来源清单见 `REQUIRED_HISTORY_DAYS`）。理由：写库按日期组织，
        某一天被整列清空后，所有横跨该日的滚动窗口都整体错位一天 —— 250 日回撤、
        MA120、RPS120（shift(120) + rolling(120)）全都受影响，而它们正是榜单上
        展示的列。只查最近两天的话，中段的洞会被放行。

        `+1`：最老的样本还需它自己的前值参与 shift/rolling，窗口首日落在边界上，
        多留一天做余量。

        另注意每日同步的任务窗口是「本地最后日期 + 1 天起」，更老的历史不会被自动
        修补 —— 洞留在那里，就是要靠这条检查挡住。

        Returns:
            检查窗口的交易日数：`max(REQUIRED_HISTORY_DAYS, self.ma_window) + 1`。
        """
        return max(REQUIRED_HISTORY_DAYS, self.ma_window) + 1

    def assert_recent_dates_complete(self, days: int | None = None, min_ratio: float = 0.9) -> None:
        """校验库内最近 `days` 个交易日的行数「全市场级」完整，否则抛异常。

        `sync_today_bulk()` 的成功只证明**本次拉取**没问题，不证明**库里**完整，
        所以这道检查必须在写库之后单独跑。判据：以窗口内最多的一天为基准，
        任何一天的行数低于 `min_ratio` × 基准即判定有洞 —— 真实的交易日之间
        行数只会差几只（退市/停牌），不会差一个数量级，所以 0.9 的余量很宽。

        Args:
            days: 检查最近几个交易日。`None`（默认）取 `required_history_days()`，
                即「全系统最长回看链 + 1」；只有测试才需要显式传值。
                刻意不是「2」：只看最近两天的话，中段的洞（2026-09-29 的 9/24）
                会被放行，而横跨它的 250 日回撤 / MA120 / RPS120 已经错位一天。
            min_ratio: 允许的最少行数占比（相对窗口内最多的一天）。

        Raises:
            MarketHistoryIncomplete: 有交易日行数明显偏少（典型是被误清空）。
        """
        if days is None:
            days = self.required_history_days()
        counts = self.get_recent_date_counts(days)
        if len(counts) < 2:
            # 库内不足两个交易日（首次回填中 / 空库）：无从比较，
            # 「库为空」已由 sync_today_bulk 的第 3 类出口兜住，这里不重复报错。
            return

        reference = max(count for _, count in counts)
        thin = [(d, count) for d, count in counts if count < reference * min_ratio]
        if not thin:
            return

        detail = "、".join(f"{d} 仅 {count} 行" for d, count in thin)
        raise MarketHistoryIncomplete(
            f"最近 {len(counts)} 个交易日中有 {len(thin)} 天数据不完整"
            f"（{detail}，正常应在 {reference} 行左右）——"
            "库内存在被清空的交易日：横跨该日的滚动窗口会整体错位一天"
            "（250 日回撤 / MA120 / RPS120 等榜单展示的口径全部失真），"
            "「上一交易日」也可能因此前移。"
            "本次不生成报告、不推送飞书；请先回补这些缺失的交易日再重跑"
        )

    def assert_no_missing_trading_days(self, expected: list[str] | None = None) -> None:
        """校验「最近窗口那一段里每个交易日都在库内」—— 补上行数判据的盲区。

        行数判据（`assert_recent_dates_complete`）只能看见**已经存在的日期**行数是否偏少；
        某交易日若被清成 0 行，它根本不出现 `GROUP BY date` 的结果里，判据对它完全瞎。
        而「整个交易日消失」同样会让涨跌幅 / 量比 / 均线跨过那一天 —— 是要防的那类错。

        判定区间 = `[窗口内最早日期, 库内最新日期]`，与行数判据**共用同一个窗口**，
        刻意不扫全历史：库内历史只到 2024-01-02，若要求交易日历里的每一天都存在，
        更早的交易日会全部被判「缺失」，闸门将永远无法放行（退化成「全库必须完整」）。
        更早的洞由 `scripts/backfill_dates.py --use-calendar` 按需扫。

        日历查不到时**降级放行并告警**，与行数判据的取舍不同：行数偏差是我们自己库里的
        硬故障，必须拦；日历查不到只是数据源的临时问题，不该因此否定一次已经通过行数
        校验的同步。代价是当天这个盲区没被覆盖，所以告警必须写清楚。

        Args:
            expected: 判定区间内的交易日清单。`None`（默认）时自己查 baostock；
                测试传固定清单即可完全离线。

        Raises:
            MarketHistoryIncomplete: 区间内有交易日整日缺失（一行都没有）。
        """
        counts = self.get_recent_date_counts(self.required_history_days())
        if len(counts) < 2:
            # 库内不足两个交易日（首次回填中 / 空库）：划不出有意义的区间，
            # 「库为空」已由 sync_today_bulk 的第 3 类出口兜住。
            return
        oldest, newest = counts[-1][0], counts[0][0]
        present = {d for d, _ in counts}

        if expected is None:
            try:
                expected = self.fetch_trade_dates(oldest, newest)
            except BaostockUnavailable as exc:
                logger.warning(
                    f"交易日历不可用，本次跳过「整日缺失」核对"
                    f"（{oldest}~{newest}）—— 行数判据已通过，但 0 行的日期查不出来：{exc}"
                )
                return
        if not expected:
            logger.warning(f"交易日历在 {oldest}~{newest} 区间内为空，跳过「整日缺失」核对")
            return

        missing = sorted(d for d in expected if d not in present)
        if not missing:
            return

        detail = "、".join(missing)
        raise MarketHistoryIncomplete(
            f"{oldest}~{newest} 区间内有 {len(missing)} 个交易日整日缺失（{detail}）——"
            "这些日期一行都没有，按日行数的判据看不见它们，"
            "但横跨它们的涨跌幅 / 量比 / 均线全部会变成多日口径。"
            "本次不生成报告、不推送飞书；"
            "请先跑 scripts/backfill_dates.py --use-calendar 回补后再重跑"
        )

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

    def write_daily_rows(self, df: pd.DataFrame) -> int:
        """把行情 DataFrame **幂等**写入 `stock_daily`，返回写入行数。

        **唯一的写库入口**：增量同步（`sync_today_bulk`）与按日期补洞
        （`scripts/backfill_dates.py`）都走它。两份「幂等写入」各写一遍必然漂移，
        而 2026-09-29 的整列清空事故正是出在这段逻辑的旧版本上。

        幂等保证：先删「本批真实出现的 (symbol, date) 对」，再 append。
        绝不能写成 `DELETE FROM stock_daily WHERE date = ?`（不限 symbol）——
        个股同步窗口是「本地最后日期 + 1 起」，一只陈旧股票就能把窗口拉长，
        于是删掉的是**那几天的全市场行**，却只 append 回它自己。
        `(symbol, date)` 上有 UNIQUE 约束与 `idx_symbol_date` 索引，逐对删是索引查找。

        Args:
            df: 需含 `symbol/date/open/high/low/close/volume/turnover` 列。

        Returns:
            实际写入的行数（空表返回 0）。
        """
        if df.empty:
            return 0
        pairs = df[["symbol", "date"]].drop_duplicates().itertuples(index=False, name=None)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("DELETE FROM stock_daily WHERE symbol = ? AND date = ?", list(pairs))
            df.to_sql(
                "stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500
            )
            conn.commit()
        return len(df)

    def fetch_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """取 `[start_date, end_date]` 区间内的交易日清单（升序，含首尾）。

        供完整性闸门做「整日缺失」核对：行数判据看不见 0 行的日期，
        只有拿交易日历当「应有清单」才能发现某个交易日整个不见了。

        与 `_bs_fetch_batch` 同一条纪律：登录失败立即抛、不发任何请求；
        日历查询本身报错也**抛**而不是返回空列表 —— 空列表会被上层读成
        「核对不出问题」，正是这条闸门最怕的静默失效。

        Args:
            start_date: 起始日 `YYYY-MM-DD`（含）。
            end_date: 结束日 `YYYY-MM-DD`（含）。

        Returns:
            交易日日期列表，如 `["2026-09-24", "2026-09-28"]`（休市日不在其中）。

        Raises:
            BaostockUnavailable: 登录失败，或日历查询报错。
        """
        import baostock as bs

        with contextlib.redirect_stdout(io.StringIO()):  # login()/logout() 会自己 print
            try:
                lg = bs.login()
                if lg.error_code != "0":
                    raise BaostockUnavailable(
                        f"baostock 登录失败: {lg.error_code} {lg.error_msg}（交易日历未查询）"
                    )
                rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
                if rs.error_code != "0":
                    raise BaostockUnavailable(f"交易日历查询失败: {rs.error_code} {rs.error_msg}")
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
            finally:
                # 登录本身失败时 logout 也可能报错；不能让它盖掉上面的异常
                with contextlib.suppress(Exception):
                    bs.logout()

        # baostock 的每行是 [日期, 是否交易日]，只有标志位为 "1" 才是交易日
        return sorted(row[0] for row in rows if row[1] == "1")

    def fetch_daily_range(self, symbols: list[str], start: str, end: str) -> tuple[list, int]:
        """批量拉取这批股票在 `[start, end]` 区间内的日线（后复权）。

        对 `_bs_fetch_batch` 的公开封装，给 `scripts/` 下的工具一个**稳定入口**，
        不必伸手进私有函数；登录校验、整批失败即抛、单只失败计数等语义全部沿用
        增量同步那一套（同一份实现，不另起一套口径）。

        Args:
            symbols: 纯数字股票代码。
            start: 起始日 `YYYY-MM-DD`（含）。
            end: 结束日 `YYYY-MM-DD`（含）。区间内的周末/节假日不会有 K 线。

        Returns:
            `(rows, failed)`：`rows` 元素为
            `[symbol, date, open, high, low, close, volume, amount]`。

        Raises:
            BaostockUnavailable: 登录失败或本批全部查询失败。
        """
        tasks = [(s, self._to_baostock_code(s), start, end) for s in symbols]
        return _bs_fetch_batch(tasks)

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
            return SyncStats(requested=0, updated=0, failed=0, rows=0, universe=len(rows))

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
        # `universe` 必须传：分母要的是**全市场股票数**（`rows` = 库里每只股票一行），
        # 不是本次待更新的那几只 —— 同一天重跑时 `requested` 会塌缩成少数停牌股。
        count = len(df)
        stats = SyncStats(
            requested=len(tasks),
            updated=df["symbol"].nunique(),
            failed=failed,
            rows=count,
            universe=len(rows),
        )
        if stats.missing_ratio > self.sync_max_fail_ratio:
            raise SyncIncomplete(
                f"需要更新 {stats.requested} 只，其中 {stats.missing} 只未拿到新数据；"
                f"相对全市场 {stats.universe} 只的未更新占比 {stats.missing_ratio:.1%}，"
                f"超过上限 {self.sync_max_fail_ratio:.1%}"
                "，判定为数据不完整；未改动数据库，本次不生成报告、不推送"
            )

        # 幂等写入（按 (symbol, date) 精确删除 + append）—— 走唯一的写库入口，
        # 与按日期补洞（scripts/backfill_dates.py）共用同一份实现。
        # 旧版本在**这里**按日期全表删（`DELETE ... WHERE date = ?`，不限 symbol）：
        # 个别 last_date 陈旧的股票会把窗口拉长，于是清掉的是整列全市场历史 ——
        # 2026-09-29 事故：300211 / 300532 两只停在 9/23 的股票把 9/24 从 1195 行
        # 削到 2 行、9/28 从 5208 行削到 1 行，全市场「上一交易日」前移到 9/23，
        # 当日涨跌幅 / 量比全变成 2 日口径（主板 000513 写出 +16.23%）。
        # 回归测试：tests/test_data_engine.py（scopes_delete_to_fetched_pairs）。
        self.write_daily_rows(df)

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
