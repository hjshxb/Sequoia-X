"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# baostock 增量同步允许的最大并发进程数。
# 上限刻意收得很紧：baostock 对同一客户端的并发登录有风控，实测 8 进程并发
# 拉全市场 5000+ 只股票会触发「黑名单用户(10001011)」，恢复期极长。
MAX_SYNC_WORKERS = 32

# 均线窗口的合法范围。过小（1 天）没有意义，过大则超出常规行情历史长度。
MIN_MA_WINDOW = 2
MAX_MA_WINDOW = 500


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"
    feishu_webhook_url: str  # 必填字段，缺失时抛出 ValidationError
    strategy_webhooks: dict[str, str] = {}

    # ── 股票池精筛（全部可选，不配置即不过滤）──
    # 1) 估值
    # 流通市值区间，单位：亿元。例如 min=100 表示只看 100 亿以上流通盘
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    # 市盈率(TTM)区间。亏损股市盈率为负，设 min_pe=0 即可排除亏损股
    min_pe: float | None = None
    max_pe: float | None = None
    # 市净率区间
    min_pb: float | None = None
    max_pb: float | None = None
    # 2) 流动性：成交额下限，单位：亿元（取自本地库，无网络开销）
    min_turnover: float | None = None
    # 3) 技术面：换手率区间，单位：%
    min_turn: float | None = None
    max_turn: float | None = None
    # 4) 技术面（本地库）：今日跌幅区间，单位：%
    #    跌幅是**有符号**量：正数 = 下跌（5 表示今日跌 5%），负数 = 上涨
    #    （-9.9 表示今日涨 9.9%）。所以 max=5 即「剔除今日跌超 5% 的」，
    #    等价于要求今日涨跌幅 >= -5%；min=5 则反向只留跌够深的（超跌）。
    #    口径为**收盘涨跌幅** (今收-昨收)/昨收，数据取自本地行情库，
    #    零网络开销，因此与成交额/行业/筹码同属第 1 级预筛。
    min_today_drop: float | None = None
    max_today_drop: float | None = None
    # 5) 技术面（本地库）：收盘价是否在 N 日均线上方
    #    判据为「相对均线的偏离度 >= min_ma_deviation」，单位：%。
    #      min_ma_deviation=0  -> 收盘价 >= MA（即站上均线）
    #      min_ma_deviation=3  -> 收盘价高于均线 3% 以上（趋势更强）
    #      留空                -> 该维度不参与过滤
    #    窗口由 ma_window 指定，默认 120 个交易日（半年线）。
    #    历史不足 N 行的次新股算不出均线，按数据缺失处理（剔除）。
    #    与今日跌幅一样取自本地行情库（后复权收盘价），零网络开销。
    min_ma_deviation: float | None = None
    ma_window: int = 120
    # 6) 行业：逗号分隔的关键词，子串匹配行业名。
    #    include 非空时只保留命中任一关键词的；exclude 命中的一律排除。
    #    例：include_industries=电子,软件,医药  /  exclude_industries=房地产,银行
    include_industries: str = ""
    exclude_industries: str = ""
    # 7) 筹码集中度：前十大流通股东合计持股占流通股比例区间，单位：%
    #    取自东财全市场接口，按报告期缓存（一年仅更新 4 次），命中缓存后零网络开销。
    #    min=40 表示只保留「前十大流通股东合计持股 >= 40%」的股票。
    min_top10_free_holding: float | None = None
    max_top10_free_holding: float | None = None
    # 指定股东数据报告期（YYYY-MM-DD 或 YYYYMMDD）；留空 = 自动取最新已披露季末
    holder_report_date: str = ""
    # 股东数据缓存目录
    holder_cache_dir: str = "data/cache"

    # ── 数据同步 ──
    # baostock 增量同步的并发进程数。**默认 1（单进程串行）**。
    # 注意：并行登录会触发 baostock 风控。历史上「5221 只 × 8 进程」的重试风暴
    # 直接换来「黑名单用户(10001011)」，且每次登录要 1.5~2 分钟才超时，排障极痛。
    # 因此默认保守串行；确有需要再手动调大（上限见 MAX_SYNC_WORKERS），
    # 且建议先用小批量（如 --workers 2）观察是否稳定。
    sync_workers: int = 1

    # ── 股票静态信息缓存（代码 → 名称 / 行业）──
    # 名称与行业几乎不变，但旧实现每次运行都要多一次 baostock login + 一次
    # 全市场请求。现在落盘到行情库的 stock_meta 表（复用 db_path），
    # 命中缓存即**完全不联网**，且数据源不可用时能用旧数据降级。
    # 缓存有效期（天）：超过则联网刷新；0 = 每次都刷新。
    stock_meta_ttl_days: int = 7

    # ── 本地 HTML 报告 ──
    # 跑完策略后生成一份本地单文件 HTML 报告（按策略分块展示选股结果）
    report_enabled: bool = True
    report_dir: str = "reports"

    # ── 候选股量化评分（量价打分排序）──
    # 策略只回答「入选 / 未入选」，答不了「这几十只里哪几只更值得看」。
    # 评分器用本地行情库（零网络开销）算量价特征并给出 0~100 分，
    # 报告与飞书卡片据此排序展示。见 sequoia_x/analysis/scorer.py。
    score_enabled: bool = True
    # 报告「综合评分排行」卡片最多展示前 N 名，**同时决定**增强层算多少只。
    # 两者必须一致，否则会出现「行展示了、增强列却是空的 —」这种不一致。
    # 截断按**评分**做（不能用只有增强后才知道的综合分，否则逻辑成环）；
    # 截断之后统一改按综合分排序展示，见 scorer.score_from_settings。
    # 0 = 不限制（全部展示、全部增强）；实测 36 只全量增强仅约 1 秒，
    # 故默认不限制。
    score_top_n: int = 0
    # 是否调用外部量化工具补充「形态匹配胜率 / 最大回撤」两列。
    # 该工具不属于本项目依赖，未配置路径 / 导入失败会自动跳过，
    # 主评分链路完全不受影响。
    score_enhance: bool = True
    # 外部量化工具（stock-researcher）的根目录；留空 = 不加载增强层。
    # 例：QUANT_SKILL_PATH=/mnt/c/Users/hxb/.workbuddy/skills/aistockresearcher__skillhub
    quant_skill_path: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )

    @field_validator(
        "min_market_cap",
        "max_market_cap",
        "min_pe",
        "max_pe",
        "min_pb",
        "max_pb",
        "min_turnover",
        "min_turn",
        "max_turn",
        "min_today_drop",
        "max_today_drop",
        "min_ma_deviation",
        "min_top10_free_holding",
        "max_top10_free_holding",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """把空字符串/空白字符串视为「未配置」（.env 中留空是常见写法）。"""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("sync_workers", mode="before")
    @classmethod
    def _blank_sync_workers_to_default(cls, v: object) -> object:
        """`.env` 里写 `SYNC_WORKERS=`（留空）是常见写法，视为未配置，回落默认 1。"""
        if isinstance(v, str) and v.strip() == "":
            return 1
        return v

    @field_validator("sync_workers", mode="after")
    @classmethod
    def _check_sync_workers(cls, v: int) -> int:
        """并发进程数必须落在 1~MAX_SYNC_WORKERS：0/负数无意义，过大易触发风控。"""
        if not 1 <= v <= MAX_SYNC_WORKERS:
            raise ValueError(f"sync_workers 必须在 1~{MAX_SYNC_WORKERS} 之间，当前为 {v}")
        return v

    @field_validator("ma_window", mode="before")
    @classmethod
    def _blank_ma_window_to_default(cls, v: object) -> object:
        """`.env` 里 `MA_WINDOW=` 留空视为未配置，回落默认 120。"""
        if isinstance(v, str) and v.strip() == "":
            return 120
        return v

    @field_validator("ma_window", mode="after")
    @classmethod
    def _check_ma_window(cls, v: int) -> int:
        """窗口必须落在 MIN_MA_WINDOW~MAX_MA_WINDOW：过小无意义，过大超出合理行情历史。"""
        if not MIN_MA_WINDOW <= v <= MAX_MA_WINDOW:
            raise ValueError(
                f"ma_window 必须在 {MIN_MA_WINDOW}~{MAX_MA_WINDOW} 之间，当前为 {v}"
            )
        return v

    @field_validator("stock_meta_ttl_days", mode="before")
    @classmethod
    def _blank_ttl_to_default(cls, v: object) -> object:
        """`STOCK_META_TTL_DAYS=` 留空视为未配置，回落默认 7 天。"""
        if isinstance(v, str) and v.strip() == "":
            return 7
        return v

    @field_validator("stock_meta_ttl_days", mode="after")
    @classmethod
    def _check_ttl(cls, v: int) -> int:
        """TTL 允许为 0（每次都刷新），但不接受负数。"""
        if v < 0:
            raise ValueError(f"stock_meta_ttl_days 不能为负，当前为 {v}")
        return v

    @field_validator("score_top_n", mode="before")
    @classmethod
    def _blank_score_top_n_to_default(cls, v: object) -> object:
        """`SCORE_TOP_N=` 留空视为未配置，回落默认 0（不限制）。"""
        if isinstance(v, str) and v.strip() == "":
            return 0
        return v

    @field_validator("score_top_n", mode="after")
    @classmethod
    def _check_score_top_n(cls, v: int) -> int:
        """Top N 允许为 0（不限制），但不接受负数。"""
        if v < 0:
            raise ValueError(f"score_top_n 不能为负（0 表示不限制），当前为 {v}")
        return v

    @field_validator("score_enabled", "score_enhance", mode="before")
    @classmethod
    def _blank_switch_to_default(cls, v: object) -> object:
        """布尔开关留空视为「未配置」，回落 True（默认开启）。"""
        if isinstance(v, str) and v.strip() == "":
            return True
        return v

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[override]
        """扩展配置源，支持从环境变量中扫描 STRATEGY_WEBHOOK_ 前缀的键。"""
        import os

        sources = super().settings_customise_sources(settings_cls, **kwargs)

        # 扫描环境变量，将 STRATEGY_WEBHOOK_<KEY> 收集到 strategy_webhooks
        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix) :].lower()
                webhooks[strategy_key] = value

        # 注入到初始化数据中（通过 init_kwargs source）
        if webhooks:
            original_init = kwargs.get("init_settings")
            # 直接在 env 层注入，通过 model_post_init 处理
            os.environ.setdefault("_STRATEGY_WEBHOOKS_PARSED", "1")
            # 存储解析结果供 model_validator 使用
            cls._parsed_strategy_webhooks = webhooks

        return sources

    def model_post_init(self, __context: object) -> None:
        """初始化后合并 STRATEGY_WEBHOOK_ 前缀的环境变量到 strategy_webhooks。"""
        import os

        prefix = "STRATEGY_WEBHOOK_"
        webhooks: dict[str, str] = dict(self.strategy_webhooks)
        for key, value in os.environ.items():
            if key.upper().startswith(prefix):
                strategy_key = key[len(prefix) :].lower()
                webhooks[strategy_key] = value

        # 使用 object.__setattr__ 绕过 pydantic 的不可变保护
        object.__setattr__(self, "strategy_webhooks", webhooks)

    def get_webhook_url(self, webhook_key: str) -> str:
        """
        根据 webhook_key 返回对应的 Webhook URL。

        优先从 strategy_webhooks 查找，找不到则 fallback 到 feishu_webhook_url。

        Args:
            webhook_key: 策略标识，如 'ma_volume'、'breakout'。

        Returns:
            对应的 Webhook URL 字符串。
        """
        return self.strategy_webhooks.get(webhook_key.lower(), self.feishu_webhook_url)


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。
    若必填字段（feishu_webhook_url）缺失，抛出 pydantic_core.ValidationError。

    Returns:
        Settings: 全局唯一的配置实例。

    Raises:
        pydantic_core.ValidationError: 当必填字段缺失或字段类型不匹配时抛出。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
