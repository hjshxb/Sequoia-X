"""策略基类模块：定义所有选股策略的抽象接口。"""

from abc import ABC, abstractmethod

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter

logger = get_logger(__name__)


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 run() 方法。

    子类在 run() 返回结果前，应调用 self.apply_universe_filter(candidates)
    以应用配置中的精筛条件（估值 / 流动性 / 技术面 / 行业）。
    未配置任何条件时该调用为纯透传，不产生任何网络请求，对性能与既有行为无影响。

    Attributes:
        webhook_key: 策略对应的飞书 webhook 标识，用于路由到不同机器人。
            默认为 'default'，将使用 Settings.feishu_webhook_url。
            子类可覆盖此属性以路由到专属机器人，例如 'ma_volume'。
    """

    webhook_key: str = "default"

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        self.engine = engine
        self.settings = settings
        self.universe_filter = UniverseFilter(settings=settings, engine=engine)

    def apply_universe_filter(self, symbols: list[str]) -> list[str]:
        """对候选股票应用统一精筛（估值 / 流动性 / 技术面 / 行业）。

        Args:
            symbols: 策略初步选出的股票代码列表。

        Returns:
            过滤后的代码列表。未配置过滤条件时原样返回。
        """
        return self.universe_filter.apply(symbols)

    @abstractmethod
    def run(self) -> list[str]:
        """
        执行选股逻辑，返回选中的股票代码列表。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        ...
