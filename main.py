"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）

可选开关：
  --no-push                    # 只跑策略并生成本地 HTML 报告，跳过飞书推送
"""

import argparse
import sys

from dotenv import load_dotenv

import socket

socket.setdefaulttimeout(10.0)

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
    # 说明：`load_dotenv()` 刻意放在函数内而不是模块顶层 —— 顶层调用会在
    # 「import main」时就把 .env 写进 os.environ，污染同进程内的其它测试
    # （pydantic 的 `_env_file=None` 只屏蔽 .env 文件，挡不住 os.environ）。
    load_dotenv()

    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="跳过飞书推送，仅跑策略并生成本地 HTML 报告",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        # 3.1 打印精筛配置（未配置时为「未启用」）
        universe = UniverseFilter(settings=settings, engine=engine)
        logger.info(universe.describe())

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 前置过滤：先算出全市场合格股票池，再交给各策略执行。
        #    精筛未启用时该池即全市场（零网络开销）；启用时分级执行
        #    （成交额/行业零成本维度先收窄，再对残余拉取昂贵指标）。
        universe_pool: list[str] | None = None
        if universe.enabled:
            universe_pool = universe.get_passing_universe()
            if not universe_pool:
                # 容错：池子为空极可能是数据源故障（而非真的全被条件剔除），
                # 此时不注入池子，回退到「全市场选股 + 后置过滤」，避免结果被误杀成空。
                logger.error("精筛预筛结果为空，判定为数据源异常，回退为全市场 + 后置过滤")
                universe_pool = None
            else:
                logger.info(f"精筛预筛完成，合格股票池 {len(universe_pool)} 只")

        # 5. 策略列表（新增策略在此追加即可）
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

        notifier = FeishuNotifier(settings)

        # 6. 遍历策略，收集结果（推送统一放到最后，汇总成一张卡片）
        results: dict[str, list[str]] = {}
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            results[strategy_name] = selected
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

        # 7. 推送：各策略汇总成单张卡片（中文策略名 + 按板块分组，只给代码+名称）
        if args.no_push:
            logger.info("已指定 --no-push，跳过飞书推送")
        elif not any(results.values()):
            logger.info("所有策略均无选股结果，跳过飞书推送")
        else:
            # 推送失败不应中断后续流程，否则本地报告会一并丢失
            try:
                notifier.send_report(
                    results,
                    # describe() 自带「精筛：」前缀，卡片里那行已有标签，去掉避免重复
                    filter_desc=universe.describe().removeprefix("精筛："),
                )
            except Exception as exc:
                logger.error(f"飞书推送异常，已忽略：{exc}")

        # 8. 生成本地 HTML 报告（按策略分块 + 板块分组，不依赖飞书）
        if settings.report_enabled:
            try:
                # 展示 市值/换手率/PE 三列。精筛若已启用，这些指标在精筛阶段
                # 就已拉取并进了进程内缓存，此处不会再产生网络请求；
                # 只有精筛完全未配置时才会新拉一次。
                displayed = sorted({s for symbols in results.values() for s in symbols})
                metrics = universe.fetch_metrics(displayed) if displayed else {}
                report_path = HtmlReportGenerator(settings).generate(
                    results,
                    filter_desc=universe.describe(),
                    metrics=metrics,
                )
                logger.info(f"HTML 报告已生成：{report_path.resolve()}")
            except Exception as exc:
                logger.error(f"HTML 报告生成失败：{exc}")
        else:
            logger.info("HTML 报告已关闭（REPORT_ENABLED=false）")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
