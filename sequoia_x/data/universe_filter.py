"""股票池过滤器：对策略初选结果做统一的多维度精筛。

支持七个维度，全部可选（未配置即该维度不参与过滤）：
    1. 估值   —— 流通市值区间、市盈率(TTM)区间、市净率区间
    2. 流动性 —— 成交额下限（取自本地库，无需联网）
    3. 技术面 —— 换手率区间
    4. 技术面 —— 今日跌幅区间（取自本地库，无需联网）
    5. 技术面 —— 收盘价是否在 N 日均线上方（取自本地库，无需联网）
    6. 行业   —— 行业关键词白名单 / 黑名单（子串匹配）
    7. 筹码   —— 前十大流通股东合计持股占流通股比例区间

两种调用方式：
    - `apply(symbols)`            —— 后置过滤：对策略初选结果做精筛。
    - `get_passing_universe()`    —— 前置过滤：先算出全市场合格股票池，
                                     再交给策略去执行（见 BaseStrategy.set_universe）。
      前置过滤采用**分级执行**：先用零成本维度（成交额/今日跌幅/均线/行业/筹码）
      把全市场收窄，再对残余股票拉取昂贵指标（市值/PE/PB/换手率），
      避免对 5000+ 只逐个联网。

数据来源与成本：
    - 流通市值 / 市盈率 / 市净率 / 换手率：baostock 不复权日线，**只对候选股**查询，
      进程内缓存，多策略共享。
    - 成交额 / 今日跌幅 / 均线：本地 SQLite 一次批量查询（窗口函数），零网络开销。
    - 行业：baostock `query_stock_industry()`，整市场一次请求；结果落盘到
      本地库的 `stock_meta` 表（见 stock_meta.load_stock_meta），
      命中缓存时**零网络请求**，数据源不可用时降级用本地旧数据。
    - 筹码集中度：东财全市场接口（**非逐股**），按报告期落盘缓存，
      一年仅更新 4 次，命中缓存后零网络开销（见 data/holder_concentration.py）。
    - **七个维度都未配置时完全不产生网络请求。**

容错策略（重要）：
    - 某一数据源「完全拉取失败」时，只跳过该维度的判据并记 ERROR，
      其余维度照常生效 —— 避免单个数据源故障把全部股票误杀成空结果。
    - 单只股票某个指标缺失，视为不通过该维度（例如亏损股 peTTM 为空）。
      例外：行业 / 筹码 / 今日跌幅存在**结构性缺失**（次新股、未披露、停牌），
      只有在配置了下限（白名单 / MIN）时才剔除缺失股，配上限时放行。
      均线维度只有下限语义，历史不足 N 行的次新股一律剔除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import holder_concentration
from sequoia_x.data import stock_meta as stock_meta_module

logger = get_logger(__name__)

YI = 1e8  # 1 亿元

# 回看窗口天数：覆盖周末与连续假期，取窗口内最后一个有数据的交易日
_LOOKBACK_DAYS = 15

# `describe(brief=True)` 时关键词表最多列出的项数，超出则折叠成「等 N 项」
_BRIEF_KEYWORD_HEAD = 3

# 进程内缓存：{symbol: StockMetric}，同一次运行内多策略共享
_METRIC_CACHE: dict[str, StockMetric] = {}


@dataclass(frozen=True)
class StockMetric:
    """候选股票的估值与技术面指标。

    Attributes:
        symbol: 纯数字股票代码。
        circ_market_cap: 流通市值，单位「元」。
        pe_ttm: 滚动市盈率。
        pb: 市净率。
        turn: 换手率，单位「%」。
        各项数据不可用时为 None。
    """

    symbol: str
    circ_market_cap: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    turn: float | None = None


def calc_circ_market_cap(close: float, volume: float, turn: float) -> float | None:
    """由不复权收盘价、成交量、换手率推算流通市值（单位：元）。

    流通股本 = 成交量 / (换手率% / 100)，流通市值 = 流通股本 × 不复权收盘价。

    Args:
        close: 不复权收盘价。
        volume: 成交量（股）。
        turn: 换手率（百分比，如 0.1554 表示 0.1554%）。

    Returns:
        流通市值（元）；数据非法时返回 None。
    """
    if turn <= 0 or volume <= 0 or close <= 0:
        return None
    circulating_shares = volume / (turn / 100)
    return circulating_shares * close


def split_keywords(raw: str) -> list[str]:
    """把逗号分隔的关键词串拆成列表，兼容中英文逗号与多余空白。"""
    if not raw:
        return []
    normalized = raw.replace("，", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


@dataclass
class _Context:
    """单次 apply() 的运行时上下文：哪些维度生效、已有数据、剔除原因统计。

    维度开关与「配置是否填写」分离，是为了在数据源整体失败时把对应维度真正关掉，
    避免退化成「全部因数据缺失被剔除」。
    """

    valuation: bool
    liquidity: bool
    technical: bool
    today_drop: bool
    ma: bool
    industry: bool
    holder: bool
    metrics: dict[str, StockMetric]
    turnovers: dict[str, float]
    metas: dict
    holdings: dict[str, float]
    drops: dict[str, float]
    ma_deviations: dict[str, float]
    reasons: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        """记录一次剔除原因。"""
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class UniverseFilter:
    """按估值 / 流动性 / 技术面 / 行业对候选股票做统一过滤。"""

    def __init__(self, settings: Settings, engine: object | None = None) -> None:
        """
        Args:
            settings: 提供各维度阈值。
            engine: DataEngine 实例，用于成交额快照查询与代码转换。允许为 None（便于测试）。
        """
        self.settings = settings
        self.engine = engine

    # ── 配置状态 ──

    @property
    def _include_industries(self) -> list[str]:
        return split_keywords(self.settings.include_industries)

    @property
    def _exclude_industries(self) -> list[str]:
        return split_keywords(self.settings.exclude_industries)

    @property
    def _valuation_enabled(self) -> bool:
        s = self.settings
        return any(
            v is not None
            for v in (
                s.min_market_cap,
                s.max_market_cap,
                s.min_pe,
                s.max_pe,
                s.min_pb,
                s.max_pb,
            )
        )

    @property
    def _liquidity_enabled(self) -> bool:
        return self.settings.min_turnover is not None

    @property
    def _technical_enabled(self) -> bool:
        return self.settings.min_turn is not None or self.settings.max_turn is not None

    @property
    def _industry_enabled(self) -> bool:
        return bool(self._include_industries or self._exclude_industries)

    @property
    def _holder_enabled(self) -> bool:
        return (
            self.settings.min_top10_free_holding is not None
            or self.settings.max_top10_free_holding is not None
        )

    @property
    def _today_drop_enabled(self) -> bool:
        return self.settings.min_today_drop is not None or self.settings.max_today_drop is not None

    @property
    def _ma_enabled(self) -> bool:
        return self.settings.min_ma_deviation is not None

    @property
    def enabled(self) -> bool:
        """是否启用了任一维度的过滤。"""
        return (
            self._valuation_enabled
            or self._liquidity_enabled
            or self._technical_enabled
            or self._today_drop_enabled
            or self._ma_enabled
            or self._industry_enabled
            or self._holder_enabled
        )

    def describe(self, *, brief: bool = False) -> str:
        """返回人类可读的过滤条件描述，用于日志与报告页首。

        Args:
            brief: 为 True 时把长关键词表折叠成「前 3 个 等 N 项」。
                **长度受限的展示端（飞书卡片）必须用 brief=True** ——
                完整版里行业黑名单动辄 40+ 个关键词，会把后面的条款挤出可视长度。
                报告页首（无长度限制）保留完整版。

        Returns:
            形如 `精筛：流通市值 >=100亿，成交额 >=5亿` 的描述文本。
        """
        if not self.enabled:
            return "精筛：未启用"

        s = self.settings
        parts: list[str] = []

        def rng(lo, hi, unit: str = "") -> str:
            if lo is not None and hi is not None:
                return f"{lo:g}~{hi:g}{unit}"
            if lo is not None:
                return f">={lo:g}{unit}"
            return f"<={hi:g}{unit}"

        def keywords(prefix: str, items: list[str]) -> str:
            """关键词表。brief 模式下超长时只列前几个 + 总数。"""
            if not brief or len(items) <= _BRIEF_KEYWORD_HEAD:
                return prefix + "/".join(items)
            head = "/".join(items[:_BRIEF_KEYWORD_HEAD])
            return f"{prefix}{head} 等 {len(items)} 项"

        if s.min_market_cap is not None or s.max_market_cap is not None:
            parts.append(f"流通市值 {rng(s.min_market_cap, s.max_market_cap, '亿')}")
        if s.min_pe is not None or s.max_pe is not None:
            parts.append(f"市盈率TTM {rng(s.min_pe, s.max_pe)}")
        if s.min_pb is not None or s.max_pb is not None:
            parts.append(f"市净率 {rng(s.min_pb, s.max_pb)}")
        if self._liquidity_enabled:
            parts.append(f"成交额 >={s.min_turnover:g}亿")
        if self._technical_enabled:
            parts.append(f"换手率 {rng(s.min_turn, s.max_turn, '%')}")
        if self._today_drop_enabled:
            parts.append(f"今日跌幅 {rng(s.min_today_drop, s.max_today_drop, '%')}")
        if self._ma_enabled:
            dev = s.min_ma_deviation or 0.0
            suffix = f"+{dev:g}%" if dev > 0 else ""
            parts.append(f"收盘价>=MA{s.ma_window}{suffix}")
        inc, exc = self._include_industries, self._exclude_industries
        if inc:
            parts.append(keywords("行业含 ", inc))
        if exc:
            parts.append(keywords("排除行业 ", exc))
        if self._holder_enabled:
            holder_range = rng(s.min_top10_free_holding, s.max_top10_free_holding, "%")
            parts.append(f"前十大流通股东 {holder_range}")

        return "精筛：" + "，".join(parts)

    # ── 数据获取 ──

    def _to_baostock_code(self, symbol: str) -> str:
        if self.engine is not None:
            return self.engine._to_baostock_code(symbol)  # type: ignore[attr-defined]
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    def fetch_metrics(self, symbols: list[str]) -> dict[str, StockMetric]:
        """批量拉取候选股的估值与技术面指标（含进程内缓存）。"""
        result: dict[str, StockMetric] = {}
        todo: list[str] = []

        for symbol in symbols:
            cached = _METRIC_CACHE.get(symbol)
            if cached is not None:
                result[symbol] = cached
            elif symbol not in todo:
                todo.append(symbol)

        if not todo:
            return result

        fetched = self._fetch_from_baostock(todo)
        _METRIC_CACHE.update(fetched)
        result.update(fetched)
        return result

    def _fetch_from_baostock(self, symbols: list[str]) -> dict[str, StockMetric]:
        """通过 baostock 批量拉取指标。整批共用一个登录会话。"""
        import baostock as bs

        today = date.today()
        start = (today - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        metrics: dict[str, StockMetric] = {}

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"精筛：baostock 登录失败 {lg.error_msg}")
            return metrics

        try:
            for symbol in symbols:
                try:
                    rs = bs.query_history_k_data_plus(
                        self._to_baostock_code(symbol),
                        "date,close,volume,turn,peTTM,pbMRQ",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="3",  # 不复权：市值/估值必须用真实价格
                    )
                    if rs.error_code != "0":
                        continue

                    last_row: list[str] | None = None
                    while rs.next():
                        last_row = rs.get_row_data()
                    if last_row is None:
                        continue

                    _date, close_s, volume_s, turn_s, pe_s, pb_s = last_row

                    def num(value: str) -> float:
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            return float("nan")

                    close, volume, turn = num(close_s), num(volume_s), num(turn_s)
                    pe, pb = num(pe_s), num(pb_s)

                    def clean(value: float) -> float | None:
                        return None if value != value else value  # NaN -> None

                    metrics[symbol] = StockMetric(
                        symbol=symbol,
                        circ_market_cap=calc_circ_market_cap(close, volume, turn),
                        pe_ttm=clean(pe),
                        pb=clean(pb),
                        turn=clean(turn),
                    )
                except Exception as exc:  # 单只失败不影响整批
                    logger.warning(f"精筛：[{symbol}] 指标拉取失败 {exc}")
                    continue
        finally:
            bs.logout()

        return metrics

    def _fetch_turnover(self, symbols: list[str]) -> dict[str, float]:
        """取候选股最新交易日的成交额（元），来源为本地库，无网络开销。"""
        if self.engine is None:
            return {}
        try:
            snapshot = self.engine.get_latest_snapshot(symbols)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"精筛：本地成交额读取失败 {exc}")
            return {}
        return {
            symbol: row["turnover"]
            for symbol, row in snapshot.items()
            if row.get("turnover") is not None
        }

    def _fetch_today_drops(self, symbols: list[str]) -> dict[str, float]:
        """取每只股票的**今日跌幅**（%），正数表示下跌、负数表示上涨。

        口径：`(今收 - 昨收) / 昨收`，数据全部来自本地行情库，无网络开销。
        「今日」以**全市场最新交易日**为基准：停牌股的最后一行早于该日，
        不能拿停牌前的旧涨跌幅充数，所以这类股票不进结果（按数据缺失处理）。

        Returns:
            {股票代码: 今日跌幅(%)}；取不到数据时返回 {}。
        """
        if self.engine is None:
            return {}

        try:
            latest = self.engine.get_market_latest_date()  # type: ignore[attr-defined]
            rows = self.engine.get_recent_rows(symbols, rows=2)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"精筛：本地行情读取失败 {exc}")
            return {}

        drops: dict[str, float] = {}
        for symbol, series in rows.items():
            if len(series) < 2:
                continue  # 次新股 / 长期停牌：没有昨收，算不出涨跌幅
            last, prev = series[-1], series[-2]
            if latest is not None and last["date"] != latest:
                continue  # 当日停牌：最后一行不是全市场最新交易日
            close, prev_close = last["close"], prev["close"]
            if not close or not prev_close:
                continue
            drops[symbol] = -(close - prev_close) / prev_close * 100
        return drops

    def _fetch_ma_deviations(self, symbols: list[str]) -> dict[str, float]:
        """取每只股票**收盘价相对 N 日均线的偏离度**（%）。

        口径：`(最新收盘价 - MA(N)) / MA(N) * 100`，
        `MA(N)` 为最近 N 个交易日收盘价的简单算术平均（窗口见 `MA_WINDOW`）。

        用**后复权**序列计算：本地库存的就是后复权价（`adjustflag="1"`），
        这样除权除息不会在均线上留下跳空缺口，均线才是连续的。

        刻意**不**像「今日跌幅」那样要求最后一行等于全市场最新交易日：
        均线是趋势指标，个股停牌一两天不改变「是否站在均线上方」的结论，
        而且它的序列本身就是自己连续的交易日。

        Returns:
            {股票代码: 偏离度(%)}；历史不足一个窗口的股票不在结果中
            （由调用方按「数据缺失」处理）。取不到数据时返回 {}。
        """
        if self.engine is None:
            return {}

        window = self.settings.ma_window
        try:
            rows = self.engine.get_recent_rows(symbols, rows=window)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"精筛：本地行情读取失败 {exc}")
            return {}

        deviations: dict[str, float] = {}
        for symbol, series in rows.items():
            if len(series) < window:
                continue  # 次新股 / 长期停牌：历史不足一个窗口，算不出均线
            closes = [row["close"] for row in series]
            if any(c is None for c in closes):
                continue
            average = sum(closes) / window
            if average <= 0:
                continue
            deviations[symbol] = (closes[-1] - average) / average * 100
        return deviations

    def _fetch_holder_ratios(self) -> dict[str, float]:
        """取全市场「前十大流通股东合计持股占流通股比例」（%）。

        该接口是**市场级**的（一次拿全市场，不是逐股），并且按报告期落盘缓存，
        所以可以放心放在第 1 级；命中缓存后零网络开销。

        Returns:
            {股票代码: 合计占比(%)}；完全不可用时返回 {}。
        """
        try:
            return holder_concentration.load_top10_free_holding(
                report_date=self.settings.holder_report_date,
                cache_dir=self.settings.holder_cache_dir,
            )
        except Exception as exc:  # 数据模块内部已降级，这里兜底
            logger.error(f"精筛：前十大流通股东数据获取失败 {exc}")
            return {}

    def cached_holder_ratios(self) -> dict[str, float]:
        """返回本次运行已加载的「前十大流通股东合计占比」快照。

        未启用筹码维度时返回 {}（**不触发任何请求**）。启用时复用精筛阶段
        已拉取并缓存的快照，因此再次调用不产生额外网络开销。

        Returns:
            {股票代码: 合计占比(%)}；未启用该维度时为 {}。
        """
        if not self._holder_enabled:
            return {}
        return self._fetch_holder_ratios()

    # ── 过滤 ──

    def apply(self, symbols: list[str]) -> list[str]:
        """对候选股票列表应用全部已配置的过滤维度（后置过滤，保留兼容）。

        Args:
            symbols: 策略选出的候选股票代码列表。

        Returns:
            过滤后的代码列表。未启用过滤、或输入为空时原样返回。
        """
        return self._apply(
            symbols,
            valuation=self._valuation_enabled,
            liquidity=self._liquidity_enabled,
            technical=self._technical_enabled,
            today_drop=self._today_drop_enabled,
            ma=self._ma_enabled,
            industry=self._industry_enabled,
            holder=self._holder_enabled,
        )

    def get_passing_universe(self) -> list[str]:
        """返回通过精筛的**全市场**股票池，供策略在执行前限定候选范围（前置过滤）。

        分级执行以控制网络成本：
          第 1 级 —— 零边际成本维度：成交额 / 今日跌幅 / 均线取自本地库（无请求）、
                     行业与筹码集中度为全市场单次请求（筹码还按报告期缓存），
                     先把 5000+ 只收窄到几百只；
          第 2 级 —— 昂贵维度：市值 / PE / PB / 换手率需逐股请求 baostock，
                     只对第 1 级的残余执行。
        两个阶段是「与」关系，最终结果与直接对全市场做一次完整 apply 完全一致。

        未启用任何维度时直接返回全市场，且不产生任何网络请求。

        Returns:
            通过全部已配置维度的股票代码列表（保持全市场原有顺序）。
        """
        if self.engine is None:
            logger.error("精筛预筛：未提供 engine，无法获取全市场股票池")
            return []

        all_symbols: list[str] = self.engine.get_local_symbols()  # type: ignore[attr-defined]
        if not self.enabled or not all_symbols:
            return all_symbols

        liquidity_on = self._liquidity_enabled
        today_drop_on = self._today_drop_enabled
        ma_on = self._ma_enabled
        industry_on = self._industry_enabled
        holder_on = self._holder_enabled

        if not (liquidity_on or today_drop_on or ma_on or industry_on or holder_on):
            logger.warning(
                "精筛预筛：未配置「成交额 / 今日跌幅 / 均线 / 行业 / 筹码」等零成本维度，"
                f"将直接对全市场 {len(all_symbols)} 只拉取估值/换手率指标，"
                "单线程耗时约 10 分钟以上"
            )

        # 第 1 级：零边际成本维度（本地库成交额/跌幅/均线 + 全市场单次行业/股东请求）
        stage1 = self._apply(
            all_symbols,
            valuation=False,
            liquidity=liquidity_on,
            technical=False,
            today_drop=today_drop_on,
            ma=ma_on,
            industry=industry_on,
            holder=holder_on,
        )
        logger.info(
            f"精筛预筛：全市场 {len(all_symbols)} -> {len(stage1)}"
            "（成交额/今日跌幅/均线/行业/筹码，零边际成本）"
        )

        if not stage1:
            return []

        # 第 2 级：昂贵维度（逐股 baostock 指标），只对残余执行
        stage2 = self._apply(
            stage1,
            valuation=self._valuation_enabled,
            liquidity=False,
            technical=self._technical_enabled,
            today_drop=False,
            ma=False,
            industry=False,
            holder=False,
        )
        logger.info(f"精筛预筛：{len(stage1)} -> {len(stage2)}（市值/PE/PB/换手率）")
        return stage2

    def _apply(
        self,
        symbols: list[str],
        *,
        valuation: bool,
        liquidity: bool,
        technical: bool,
        today_drop: bool = False,
        ma: bool = False,
        industry: bool,
        holder: bool = False,
    ) -> list[str]:
        """按显式指定的维度集合过滤候选股票。

        维度以参数传入（而非直接读配置），是为了支持「分级预筛」：
        第 1 级只跑零边际成本维度、第 2 级只跑昂贵维度，两级的交集等价于一次完整过滤。

        Args:
            symbols: 待过滤的股票代码列表。
            valuation: 是否启用估值维度（市值/PE/PB）。
            liquidity: 是否启用流动性维度（成交额）。
            technical: 是否启用技术面维度（换手率）。
            today_drop: 是否启用今日跌幅维度（本地库今收 vs 昨收）。
            ma: 是否启用均线维度（本地库收盘价 vs N 日均线）。
            industry: 是否启用行业维度（白名单/黑名单）。
            holder: 是否启用筹码维度（前十大流通股东合计占比）。

        Returns:
            过滤后的代码列表。指定维度全为 False、或输入为空时原样返回。
        """
        if not symbols:
            return list(symbols)
        if not (valuation or liquidity or technical or today_drop or ma or industry or holder):
            return list(symbols)

        # 维度是否生效。注意：某一数据源「完全拉取失败」时必须把对应维度真正关掉，
        # 否则会退化成「全部因数据缺失被剔除」，把结果误杀成空。
        valuation_on = valuation
        technical_on = technical
        liquidity_on = liquidity
        today_drop_on = today_drop
        ma_on = ma
        industry_on = industry
        holder_on = holder

        # 1) 估值 / 换手率（baostock，一次登录批量拉取）
        metrics: dict[str, StockMetric] = {}
        if valuation_on or technical_on:
            metrics = self.fetch_metrics(symbols)
            if not metrics:
                logger.error("精筛：估值/技术面指标完全获取失败，跳过该维度")
                valuation_on = technical_on = False

        # 2) 成交额（本地库，无网络开销）
        turnovers: dict[str, float] = {}
        if liquidity_on:
            turnovers = self._fetch_turnover(symbols)
            if not turnovers:
                logger.error("精筛：成交额数据完全获取失败，跳过该维度")
                liquidity_on = False

        # 3) 今日跌幅（本地库，无网络开销）
        drops: dict[str, float] = {}
        if today_drop_on:
            drops = self._fetch_today_drops(symbols)
            if not drops:
                logger.error("精筛：本地行情（今日跌幅）完全获取失败，跳过该维度")
                today_drop_on = False

        # 4) 均线偏离度（本地库，无网络开销）
        ma_deviations: dict[str, float] = {}
        if ma_on:
            ma_deviations = self._fetch_ma_deviations(symbols)
            if not ma_deviations:
                logger.error("精筛：本地行情（均线）完全获取失败，跳过该维度")
                ma_on = False

        # 5) 行业（baostock 全市场一次请求）
        metas: dict = {}
        if industry_on:
            metas = stock_meta_module.load_stock_meta()
            if not metas:
                logger.error("精筛：行业数据完全获取失败，跳过该维度")
                industry_on = False

        # 6) 筹码集中度（东财全市场一次请求 + 报告期磁盘缓存）
        holdings: dict[str, float] = {}
        if holder_on:
            holdings = self._fetch_holder_ratios()
            if not holdings:
                logger.error("精筛：前十大流通股东数据完全获取失败，跳过该维度")
                holder_on = False

        ctx = _Context(
            valuation=valuation_on,
            liquidity=liquidity_on,
            technical=technical_on,
            today_drop=today_drop_on,
            ma=ma_on,
            industry=industry_on,
            holder=holder_on,
            metrics=metrics,
            turnovers=turnovers,
            metas=metas,
            holdings=holdings,
            drops=drops,
            ma_deviations=ma_deviations,
        )

        kept: list[str] = []
        for symbol in symbols:
            if self._passes(symbol, ctx):
                kept.append(symbol)

        detail = " | ".join(f"{k} {v}" for k, v in ctx.reasons.items()) or "无剔除"
        logger.info(f"精筛：{len(symbols)} -> {len(kept)} （{detail}）")
        return kept

    def _passes(self, symbol: str, ctx: _Context) -> bool:
        """判断单只股票是否同时满足所有已生效的维度。"""
        s = self.settings

        # ── 估值：市值 / PE / PB ──
        metric = ctx.metrics.get(symbol)
        if ctx.valuation:
            if metric is None:
                ctx.drop("估值数据缺失")
                return False

            if s.min_market_cap is not None or s.max_market_cap is not None:
                if metric.circ_market_cap is None:
                    ctx.drop("市值缺失")
                    return False
                cap_yi = metric.circ_market_cap / YI
                if s.min_market_cap is not None and cap_yi < s.min_market_cap:
                    ctx.drop("市值偏小")
                    return False
                if s.max_market_cap is not None and cap_yi > s.max_market_cap:
                    ctx.drop("市值偏大")
                    return False

            if s.min_pe is not None or s.max_pe is not None:
                if metric.pe_ttm is None:
                    ctx.drop("PE缺失")
                    return False
                if s.min_pe is not None and metric.pe_ttm < s.min_pe:
                    ctx.drop("PE偏低")
                    return False
                if s.max_pe is not None and metric.pe_ttm > s.max_pe:
                    ctx.drop("PE偏高")
                    return False

            if s.min_pb is not None or s.max_pb is not None:
                if metric.pb is None:
                    ctx.drop("PB缺失")
                    return False
                if s.min_pb is not None and metric.pb < s.min_pb:
                    ctx.drop("PB偏低")
                    return False
                if s.max_pb is not None and metric.pb > s.max_pb:
                    ctx.drop("PB偏高")
                    return False

        # ── 流动性：成交额 ──
        if ctx.liquidity:
            amount = ctx.turnovers.get(symbol)
            if amount is None:
                ctx.drop("成交额缺失")
                return False
            if amount / YI < s.min_turnover:
                ctx.drop("成交额不足")
                return False

        # ── 技术面：换手率 ──
        if ctx.technical:
            if metric is None or metric.turn is None:
                ctx.drop("换手率缺失")
                return False
            if s.min_turn is not None and metric.turn < s.min_turn:
                ctx.drop("换手率偏低")
                return False
            if s.max_turn is not None and metric.turn > s.max_turn:
                ctx.drop("换手率偏高")
                return False

        # ── 今日跌幅：本地库（今收 vs 昨收）──
        if ctx.today_drop:
            drop = ctx.drops.get(symbol)
            lo = s.min_today_drop
            hi = s.max_today_drop

            if drop is None:
                # 停牌 / 次新股当日没有行情。
                #   设了下限 -> 无法证明它「跌够深」，判不通过；
                #   只设上限 -> 无法证明它「跌超上限」，放行。
                if lo is not None:
                    ctx.drop("今日行情缺失")
                    return False
            else:
                if lo is not None and drop < lo:
                    ctx.drop("今日跌幅不足")
                    return False
                if hi is not None and drop > hi:
                    ctx.drop("今日跌幅过大")
                    return False

        # ── 均线：收盘价是否在 N 日均线上方 ──
        if ctx.ma:
            deviation = ctx.ma_deviations.get(symbol)
            floor = s.min_ma_deviation

            if deviation is None:
                # 次新股 / 长期停牌：历史不足一个窗口，算不出均线。
                # 该维度只有下限语义（没有「上限」这种反向用法），
                # 无法证明它站在均线上方 —— 剔除。
                ctx.drop("均线数据缺失")
                return False

            if floor is not None and deviation < floor:
                ctx.drop("跌破均线")
                return False

        # ── 行业：白名单 / 黑名单（子串匹配）──
        if ctx.industry:
            meta = ctx.metas.get(symbol)
            industry = (meta.industry if meta else None) or ""
            include = self._include_industries
            exclude = self._exclude_industries

            if not industry:
                # 行业未知时的取舍：
                #   有白名单 -> 无法证明它命中白名单，判不通过；
                #   只有黑名单 -> 无法证明它属于被排除的行业，放行。
                # （全市场约 6% 的股票没有行业分类，一刀切剔除会误杀）
                if include:
                    ctx.drop("行业缺失")
                    return False
            else:
                if include and not any(kw in industry for kw in include):
                    ctx.drop("不在行业白名单")
                    return False

                if exclude and any(kw in industry for kw in exclude):
                    ctx.drop("命中行业黑名单")
                    return False

        # ── 筹码：前十大流通股东合计占比 ──
        if ctx.holder:
            ratio = ctx.holdings.get(symbol)
            lo = s.min_top10_free_holding
            hi = s.max_top10_free_holding

            if ratio is None:
                # 与行业同理：次新股 / 尚未披露报告期的个股没有股东数据。
                #   设了下限 -> 无法证明它足够集中，判不通过；
                #   只设上限 -> 无法证明它超过上限，放行。
                # （全市场约 4% 的个股没有股东数据，一刀切剔除会误杀）
                if lo is not None:
                    ctx.drop("股东数据缺失")
                    return False
            else:
                if lo is not None and ratio < lo:
                    ctx.drop("筹码分散")
                    return False
                if hi is not None and ratio > hi:
                    ctx.drop("筹码过度集中")
                    return False

        return True
