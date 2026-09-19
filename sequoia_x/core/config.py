"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # 4) 行业：逗号分隔的关键词，子串匹配行业名。
    #    include 非空时只保留命中任一关键词的；exclude 命中的一律排除。
    #    例：include_industries=电子,软件,医药  /  exclude_industries=房地产,银行
    include_industries: str = ""
    exclude_industries: str = ""
    # 5) 筹码集中度：前十大流通股东合计持股占流通股比例区间，单位：%
    #    取自东财全市场接口，按报告期缓存（一年仅更新 4 次），命中缓存后零网络开销。
    #    min=30 表示只保留「前十大流通股东合计持股 >= 30%」的股票。
    min_top10_free_holding: float | None = None
    max_top10_free_holding: float | None = None
    # 指定股东数据报告期（YYYY-MM-DD 或 YYYYMMDD）；留空 = 自动取最新已披露季末
    holder_report_date: str = ""
    # 股东数据缓存目录
    holder_cache_dir: str = "data/cache"

    # ── 本地 HTML 报告 ──
    # 跑完策略后生成一份本地单文件 HTML 报告（按策略分块展示选股结果）
    report_enabled: bool = True
    report_dir: str = "reports"

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
