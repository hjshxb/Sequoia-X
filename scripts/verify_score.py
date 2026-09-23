#!/usr/bin/env python3
"""用真实库验证评分模块：解析当日报告的候选池 → 打分 → 打印排行。

为什么单独一个脚本：评分模块被 main.py 每日调用，改动它必须能**独立复现**，
不必跑整条流程（全市场同步 33 分钟 + 真的推飞书）。

用法（WSL，仓库根目录）：
    python scripts/verify_score.py                 # 取最新报告
    python scripts/verify_score.py --date 2026-09-23 --top 20
    python scripts/verify_score.py --no-enhance    # 跳过外部量化工具增强
"""

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from quant_analyze import latest_report, parse_report  # noqa: E402

from sequoia_x.analysis.scorer import score_pool  # noqa: E402
from sequoia_x.data.universe_filter import StockMetric  # noqa: E402

# 外部量化工具根目录（与 .env 的 QUANT_SKILL_PATH 保持一致）
SEQUOIA_SKILL = "/mnt/c/Users/hxb/.workbuddy/skills/aistockresearcher__skillhub"


def _finite(value):
    """把解析出来的 nan / 空串统一成 None，避免污染评分。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD，默认取最新报告")
    ap.add_argument("--top", type=int, default=15, help="打印前 N 名")
    ap.add_argument("--no-enhance", action="store_true", help="跳过外部量化工具增强")
    args = ap.parse_args()

    if args.date:
        path = os.path.join(ROOT, "reports", f"stock_report_{args.date}.html")
    else:
        path = latest_report()
    if not path or not os.path.exists(path):
        print(f"ERROR: 找不到报告 {path}", file=sys.stderr)
        return 1
    print(f"报告: {os.path.relpath(path, ROOT)}")

    pool = parse_report(path)
    if not pool:
        print("ERROR: 报告里没解析出任何股票行", file=sys.stderr)
        return 1

    symbols = list(pool)
    tags = {code: rec["tag"] for code, rec in pool.items()}
    metrics = {
        code: StockMetric(
            symbol=code,
            pe_ttm=_finite(rec["pe"]),
            turn=_finite(rec["turn"]),
            circ_market_cap=(_finite(rec["cap"]) or 0) * 1e8 or None,
        )
        for code, rec in pool.items()
    }
    holdings = {code: _finite(rec["hold"]) for code, rec in pool.items()}
    holdings = {k: v for k, v in holdings.items() if v is not None}

    enhance_top = 0 if args.no_enhance else args.top
    result = score_pool(
        symbols,
        db_path=os.path.join(ROOT, "data", "sequoia_v2.db"),
        metrics=metrics,
        holdings=holdings,
        tags=tags,
        enhance_top=enhance_top,
        skill_path="" if args.no_enhance else SEQUOIA_SKILL,
    )

    print(f"候选池 {len(pool)} 只 → 成功评分 {len(result)} 只"
          f"（跳过 {len(pool) - len(result)} 只历史数据不足）\n")

    head = (f"{'#':<3}{'代码':<8}{'名称':<10}{'S':<4}{'标记':<6}{'基础':<6}{'罚':<4}"
            f"{'当日%':<8}{'20日%':<8}{'量比':<7}{'距高%':<7}{'MA20偏':<8}"
            f"{'波动%':<7}{'PE':<8}{'筹码':<6}{'胜率%':<7}{'回撤%'}")
    print(head)
    print("-" * 150)
    for i, d in enumerate(result[: args.top], 1):
        rec = pool.get(d.symbol, {})
        print(
            f"{i:<3}{d.symbol:<8}{rec.get('name', ''):<10}"
            f"{d.score:<4}{d.tags or '-':<6}{d.total:<6.0f}{d.penalty:<4.0f}"
            f"{(d.chg1 or 0):>6.2f}  {(d.chg20 or 0):>6.2f}  "
            f"{(d.vol_ratio or 0):>5.2f}  {(1 - (d.near_high or 0)) * 100:>5.2f}  "
            f"{(d.dev_ma20 or 0):>6.2f}  {(d.vol20 or 0):>5.1f}  "
            f"{rec.get('pe', '')!s:<8}{rec.get('hold', '')!s:<6}"
            f"{(d.prob_up if d.prob_up is not None else float('nan')):>6.1f}  "
            f"{(d.max_drawdown if d.max_drawdown is not None else float('nan')):>6.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
