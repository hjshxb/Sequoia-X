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

load_dotenv()


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
        logger.info(UniverseFilter(settings=settings, engine=engine).describe())

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

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings)
        if args.no_push:
            logger.info("已指定 --no-push，跳过全部飞书推送")

        # 5. 遍历策略，有结果则推送至对应机器人
        results: dict[str, list[str]] = {}
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            results[strategy_name] = selected
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if not selected:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")
            elif args.no_push:
                continue
            else:
                # 推送失败不应中断后续策略，否则本地报告会一并丢失
                try:
                    notifier.send(
                        symbols=selected,
                        strategy_name=strategy_name,
                        webhook_key=strategy.webhook_key,
                    )
                except Exception as exc:
                    logger.error(f"{strategy_name} 飞书推送异常，已忽略：{exc}")

        # 6. 生成本地 HTML 报告（按策略分块 + 板块分组，不依赖飞书）
        if settings.report_enabled:
            try:
                universe = UniverseFilter(settings=settings, engine=engine)
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
