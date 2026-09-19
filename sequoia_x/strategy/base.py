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

    股票池有两种工作模式：

    1. **前置过滤（推荐，日常运行使用）** —— 调用方先算好全市场合格股票池，
       通过 `set_universe()` 注入。子类用 `candidate_symbols()` 取池子，
       策略只在「已通过精筛」的股票中选股，选股阶段的读库量大幅下降。
    2. **后置过滤（兼容旧行为 / 单独调用策略时）** —— 未注入池子时，
       `candidate_symbols()` 退化为全市场，子类在 run() 末尾调用
       `apply_universe_filter(candidates)` 做精筛。

    两种模式下 `apply_universe_filter()` 都可用：未注入池子时走完整精筛；
    已注入池子时退化为一次纯内存的「是否在池内」判定（不联网）。

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
        # 前置过滤的股票池：为 None 表示未注入，退化为全市场 + 后置过滤
        self._universe: list[str] | None = None
        self._universe_set: set[str] | None = None

    def set_universe(self, symbols: list[str] | None) -> None:
        """注入「已通过精筛」的全市场股票池（前置过滤）。

        Args:
            symbols: 合格股票代码列表；传 None 表示取消注入，回退到后置过滤模式。
        """
        if symbols is None:
            self._universe = None
            self._universe_set = None
            logger.info(f"{type(self).__name__} 已取消预筛池，回退为全市场 + 后置过滤")
            return
        self._universe = list(symbols)
        self._universe_set = set(symbols)

    def candidate_symbols(self) -> list[str]:
        """返回策略应当遍历的候选股票池。

        Returns:
            已注入预筛池时返回该池；否则返回本地全市场股票列表。
        """
        if self._universe is not None:
            return self._universe
        return self.engine.get_local_symbols()

    def apply_universe_filter(self, symbols: list[str]) -> list[str]:
        """对候选股票应用统一精筛（估值 / 流动性 / 技术面 / 行业）。

        已注入预筛池时，这里退化为「是否在池内」的纯内存判定 ——
        因为池子本身就是精筛结果，无需重复联网；对候选来自外部的策略
        （如定增公告，候选并非取自本地库）而言则等价于「只保留池内股票」。

        Args:
            symbols: 策略初步选出的股票代码列表。

        Returns:
            过滤后的代码列表。未配置过滤条件时原样返回。
        """
        if self._universe_set is not None:
            return [s for s in symbols if s in self._universe_set]
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
