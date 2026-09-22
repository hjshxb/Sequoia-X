"""测试模式：跳过 sync_today_bulk，直接用本地库数据跑 预筛 → 策略 → 飞书推送 → HTML 报告。

用途：非交易日 / 不想等全市场同步时，验证「预筛 → 策略 → 汇总推送 → 报告」链路是否正常。
与 main.py 的唯一差别：不调用 engine.sync_today_bulk()。
"""

import socket

socket.setdefaulttimeout(10.0)

from dotenv import load_dotenv

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.notify.html_report import HtmlReportGenerator
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy


def main() -> None:
    # 与其他入口一致：load_dotenv 放在函数内，避免 import 期污染 os.environ
    load_dotenv()

    logger = get_logger(__name__)
    settings = get_settings()
    engine = DataEngine(settings)

    logger.info("=" * 60)
    logger.info("[测试模式] 跳过 sync_today_bulk，直接使用本地库已有数据")
    universe = UniverseFilter(settings=settings, engine=engine)
    logger.info(universe.describe())
    logger.info("=" * 60)

    # 1. 前置过滤：先算全市场合格股票池
    universe_pool = universe.get_passing_universe() if universe.enabled else None
    if universe_pool is not None and not universe_pool:
        logger.error("预筛结果为空，判定为数据源异常，回退为全市场 + 后置过滤")
        universe_pool = None
    elif universe_pool is not None:
        logger.info(f"预筛完成，合格股票池 {len(universe_pool)} 只")

    # 2. 策略在池内选股
    strategies: list[BaseStrategy] = [
        MaVolumeStrategy(engine=engine, settings=settings),
        TurtleTradeStrategy(engine=engine, settings=settings),
        HighTightFlagStrategy(engine=engine, settings=settings),
        LimitUpShakeoutStrategy(engine=engine, settings=settings),
        UptrendLimitDownStrategy(engine=engine, settings=settings),
        RpsBreakoutStrategy(engine=engine, settings=settings),
        PrivatePlacementStrategy(engine=engine, settings=settings),
    ]
    for strategy in strategies:
        strategy.set_universe(universe_pool)

    results: dict[str, list[str]] = {}
    for strategy in strategies:
        name = type(strategy).__name__
        selected = strategy.run()
        results[name] = selected
        logger.info(f"{name} 选出 {len(selected)} 只")

    # 3. 汇总成一张卡片推送
    if not any(results.values()):
        logger.info("所有策略均无选股结果，跳过飞书推送")
    else:
        try:
            FeishuNotifier(settings).send_report(
                results,
                filter_desc=universe.describe(brief=True).removeprefix("精筛："),
            )
            logger.info("飞书汇总卡片推送完成")
        except Exception as exc:
            logger.error(f"飞书推送异常：{exc}")

    # 4. 本地 HTML 报告
    displayed = sorted({s for codes in results.values() for s in codes})
    metrics = universe.fetch_metrics(displayed) if displayed else {}
    path = HtmlReportGenerator(settings).generate(
        results,
        filter_desc=universe.describe(),
        metrics=metrics,
        holdings=universe.cached_holder_ratios(),
    )
    logger.info(f"HTML 报告：{path.resolve()}")
    logger.info("测试模式运行完成")


if __name__ == "__main__":
    main()
