"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：增量补数据 + 跑策略 + 飞书推送
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）

可选开关：
  --no-push                    # 只跑策略并生成本地 HTML 报告，跳过飞书推送
  --workers N                  # 覆盖 SYNC_WORKERS，指定增量同步的并发进程数（1~32）
                               # 默认取配置，未配置即单进程串行

当日增量数据未就绪时（登录被拒 / 整批查询失败 / 一行没拿到 / 未更新占比超
SYNC_MAX_FAIL_RATIO），主流程直接以退出码 1 终止：不生成报告、不推送飞书。
库内最近的交易日有洞（按日行数远少于邻近日，典型是被误清空）同样终止 ——
「上一交易日」前移会让当日涨跌幅 / 量比全部变成多日口径。
宁可当天不出结果，也不用上一交易日的旧数据冒充当日选股结果。
"""

import argparse
import socket
import sys

from dotenv import load_dotenv

from sequoia_x.analysis.scorer import ScoreDetail, composite_score, score_from_settings
from sequoia_x.core.config import MAX_SYNC_WORKERS, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta
from sequoia_x.data.engine import BaostockUnavailable, DataEngine, SyncIncomplete, SyncStats
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

# baostock 用的是裸 socket，这里统一把默认超时压到 10s，避免网络异常时长时间挂起。
# 必须早于任何 socket 建立 —— 放在 import 之后即可（导入阶段不会建连接）。
# 注：这行刻意不夹在 import 中间，否则后面的 import 全会被判为 E402。
socket.setdefaulttimeout(10.0)


def _format_data_status(stats: SyncStats, latest_date: str | None) -> str:
    """把本次同步结果压成一行「数据日期 + 更新情况」，随卡片与报告一起发出去。

    拿到榜单的人最需要先确认两件事：**这份数据是哪一天的**、**全不全**。
    标题里的日期只是**运行日**，数据却可能来自更早的交易日（非交易日重跑、
    数据源当天未发布行情等），两者必须在页面上分得清。

    返回的是**值**（`2026-09-24 · 全市场 5221 只已更新`），「数据日期：」这个
    标签由渲染层补 —— 卡片用 lark_md、报告用 HTML，标签写法不同，但值一致。

    Args:
        stats: `sync_today_bulk()` 的返回值。
        latest_date: 同步后库内全市场最新交易日（`get_market_latest_date()`）。

    Returns:
        形如 `2026-09-24 · 全市场 5221 只已更新` 的单行文本。
    """
    if stats.requested == 0:
        detail = "无需更新（已是最新）"
    elif stats.missing:
        detail = f"更新 {stats.updated} 只，未更新 {stats.missing} 只（{stats.missing_ratio:.1%}）"
    else:
        detail = f"全市场 {stats.updated} 只已更新"
    return f"{latest_date or '未知'} · {detail}"


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
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"增量同步的并发进程数（1~{MAX_SYNC_WORKERS}），覆盖 SYNC_WORKERS；"
            "默认取配置（未配置即单进程串行）"
        ),
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 1.1 命令行 --workers 覆盖配置。范围在这里就先挡掉，
        #     避免把一个明显错误的并发度带进同步流程。
        if args.workers is not None:
            if not 1 <= args.workers <= MAX_SYNC_WORKERS:
                parser.error(f"--workers 必须在 1~{MAX_SYNC_WORKERS} 之间")
            settings.sync_workers = args.workers

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")
        logger.info(f"增量同步并发进程数：{settings.sync_workers}")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        # 3.0 股票名称/行业落盘缓存：复用行情库，命中即完全不联网。
        #     必须在任何策略/报告/推送之前配置好，否则那三处会各自走一次网络。
        stock_meta.configure_cache(settings.db_path, settings.stock_meta_ttl_days)
        logger.info(f"静态信息缓存：{settings.db_path}（TTL {settings.stock_meta_ttl_days} 天）")

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
        try:
            stats = engine.sync_today_bulk()
            # 「本次同步成功」不等于「库里的交易日是完整的」：写库按日期组织，
            # 某一天被整列清空后 MAX(date) 依然正常、数据状态行也照常显示今天，
            # 但横跨该日的滚动窗口已整体错位（250 日回撤 / MA120 / RPS120）。
            # 所以写库之后必须再按日行数独立校验一次，失败同样中止本次推送。
            # 窗口由 DataEngine.required_history_days() 定 = 全系统最长回看链 + 1，
            # 不是「最近两天」—— 中段的洞会静默改变 250 日口径。
            logger.info(f"校验库内最近 {engine.required_history_days()} 个交易日的完整性...")
            engine.assert_recent_dates_complete()
            # 行数判据看不见「0 行」的日期：某交易日若被清空到一行不剩，它根本
            # 不出现在 GROUP BY date 的结果里。再拿交易日历当应有清单核一遍，
            # 日期范围同样限定在闸门窗口内（防「全库必须完整」式误报）。
            engine.assert_no_missing_trading_days()
        except (BaostockUnavailable, SyncIncomplete) as exc:
            # 数据源不可用（登录被拒 / 整批失败 / 一行没拿到）、拿到的数据不完整
            # （未更新占比超阈值），或库内最近交易日有洞 ⇒ 一律终止。绝不拿上一
            # 交易日（或口径错乱的）旧数据照跑策略并推送：那等于用陈旧数据冒充
            # 当日选股结果，比不出结果更糟。
            logger.error(f"当日数据未就绪，本次不生成报告、不推送飞书：{exc}")
            sys.exit(1)

        # 「数据状态」一行随卡片与报告发出，让看榜单的人先知道数据是哪天的、全不全。
        # 取同步**之后**的全市场 MAX(date)，即这批结果真正的数据基准日。
        data_status = _format_data_status(stats, engine.get_market_latest_date())
        logger.info(f"快照同步完成：{data_status}")

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
            # 注入的是「候选范围」：各策略用 candidate_symbols() 取池以省算力。
            # 个别策略可声明 `applies_universe_filter = False` 完全豁免精筛
            # （目前只有 RPS —— 横截面指标，排名基数与结果都必须是全市场）。
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

        # 6.5 量化评分：把「入选 / 未入选」的布尔结果变成可排序的分数。
        #     全部数据取自本地行情库，零网络开销；精筛已拉取的指标直接复用。
        #     评分失败**不能**阻断主流程 —— 报告和推送仍要照常产出，
        #     少一个评分列远好过当天什么都收不到。
        displayed = sorted({s for symbols in results.values() for s in symbols})
        metrics = universe.fetch_metrics(displayed) if displayed else {}
        holdings = universe.cached_holder_ratios()

        ranking: list[ScoreDetail] = []
        if not settings.score_enabled:
            logger.info("量化评分已关闭（SCORE_ENABLED=false）")
        elif displayed:
            try:
                # 配置怎么读统一在 score_from_settings 里（离线重算脚本共用同一份实现）
                ranking = score_from_settings(
                    displayed,
                    settings=settings,
                    metrics=metrics,
                    holdings=holdings,
                    tags=build_symbol_marks(results),
                )
                if ranking:
                    # 排序口径是**综合分**（评分 + 胜率），所以榜首是综合分最高，
                    # 不一定是评分最高 —— 两个数都打出来，免得日志误导。
                    logger.info(
                        f"量化评分完成：{len(ranking)} 只，"
                        f"综合最高 {composite_score(ranking[0]):.1f}"
                        f"（{ranking[0].symbol}，评分 {ranking[0].score:.1f}）"
                    )
            except Exception as exc:
                logger.error(f"量化评分失败，本次报告与推送不含评分：{exc}")

        # 7. 推送：各策略汇总成单张卡片（中文策略名 + 板块分组 + 高分榜）
        if args.no_push:
            logger.info("已指定 --no-push，跳过飞书推送")
        elif not any(results.values()):
            logger.info("所有策略均无选股结果，跳过飞书推送")
        else:
            # 推送失败不应中断后续流程，否则本地报告会一并丢失
            try:
                notifier.send_report(
                    results,
                    # describe() 自带「精筛：」前缀，卡片里那行已有标签，去掉避免重复。
                    # 必须用 brief=True：完整版里 40+ 个行业关键词会把后面的条款挤出
                    # 卡片可视长度（实测「前十大流通股东」曾被截掉）。
                    filter_desc=universe.describe(brief=True).removeprefix("精筛："),
                    scores=ranking,
                    data_status=data_status,
                )
            except Exception as exc:
                logger.error(f"飞书推送异常，已忽略：{exc}")

        # 8. 生成本地 HTML 报告（按策略分块 + 板块分组 + 评分排行，不依赖飞书）
        if settings.report_enabled:
            try:
                # metrics / holdings 在第 6.5 步已算好：精筛若已启用，这些指标
                # 在精筛阶段就已拉取并进了进程内缓存，此处不会再产生网络请求；
                # 只有精筛完全未配置时才会新拉一次。
                report_path = HtmlReportGenerator(settings).generate(
                    results,
                    filter_desc=universe.describe(),
                    metrics=metrics,
                    holdings=holdings,
                    scores=ranking,
                    data_status=data_status,
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
