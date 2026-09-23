"""测试模式：跳过 sync_today_bulk，直接用本地库数据跑 预筛 → 策略 → 量化评分 → 飞书推送 → HTML 报告。

用途：非交易日 / 不想等全市场同步时，验证「预筛 → 策略 → 评分 → 汇总推送 → 报告」链路是否正常。

⚠️ 这个脚本是**生产链路的镜像**：除了不调用 `engine.sync_today_bulk()`，其余步骤必须与
`main.py` 保持同构 —— 尤其传给 `send_report()` / `generate()` 的参数（含 `scores=`）。
2026-09-24 修过一次：原先没传 `scores`，推出去的卡片比生产少「量化评分 Top 5」小节、
板块内排序也不同，**用它验证过的链路等于没验证**。

刻意保留的一处差异：指标/筹码取数失败时降级为 `{}` 并记 WARNING，而不是中断。
main.py 靠的是「精筛已把指标拉进缓存」，测试模式下缓存可能不全，宁可少两个维度也要把推送发出去。
"""

import socket

socket.setdefaulttimeout(10.0)

from dotenv import load_dotenv

from sequoia_x.analysis.scorer import ScoreDetail, score_from_settings
from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.notify.html_report import HtmlReportGenerator, build_symbol_marks
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

    # 3. 指标 + 筹码（与 main.py 同步步骤一致；失败降级为空，不阻断推送）
    displayed = sorted({s for codes in results.values() for s in codes})
    metrics: dict = {}
    holdings: dict = {}
    try:
        metrics = universe.fetch_metrics(displayed) if displayed else {}
        holdings = universe.cached_holder_ratios()
    except Exception as exc:
        logger.warning(f"[测试模式] 指标/筹码取数失败，降级为空继续推送：{exc}")

    # 4. 量化评分（零网络，全取自本地行情库）
    ranking: list[ScoreDetail] = []
    if not settings.score_enabled:
        logger.info("量化评分已关闭（SCORE_ENABLED=false）")
    elif displayed:
        try:
            ranking = score_from_settings(
                displayed,
                settings=settings,
                metrics=metrics,
                holdings=holdings,
                tags=build_symbol_marks(results),
            )
            if ranking:
                logger.info(
                    f"量化评分完成：{len(ranking)} 只，最高 {ranking[0].score:.1f}（{ranking[0].symbol}）"
                )
        except Exception as exc:
            logger.error(f"量化评分失败，本次报告与推送不含评分：{exc}")

    # 5. 汇总成一张卡片推送
    if not any(results.values()):
        logger.info("所有策略均无选股结果，跳过飞书推送")
    else:
        try:
            FeishuNotifier(settings).send_report(
                results,
                filter_desc=universe.describe(brief=True).removeprefix("精筛："),
                scores=ranking,
            )
            logger.info("飞书汇总卡片推送完成")
        except Exception as exc:
            logger.error(f"飞书推送异常：{exc}")

    # 6. 本地 HTML 报告
    path = HtmlReportGenerator(settings).generate(
        results,
        filter_desc=universe.describe(),
        metrics=metrics,
        holdings=holdings,
        scores=ranking,
    )
    logger.info(f"HTML 报告：{path.resolve()}")
    logger.info("测试模式运行完成")


if __name__ == "__main__":
    main()
