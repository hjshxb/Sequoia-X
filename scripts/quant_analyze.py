#!/usr/bin/env python3
"""Sequoia-X 候选池量价分析器（CLI）—— 解析当日报告 → 调用评分模块 → 打印排行。

职责边界（重要）：
    报告解析（HTML → 候选池）留在本脚本；**打分逻辑统一在
    `sequoia_x.analysis.scorer`**，这里只做「取数 → 调模块 → 打印」。
    这样 CLI 与 main.py 每日流程用的是同一套分数，不会各改各的而漂移。

为什么需要 CLI：
    main.py 每日跑完才有评分，但调参数、复盘、验证某一天时不想跑整条流程
    （全市场同步可能几十分钟）。用已生成的报告 + 本地库即可离线复现。

用法（WSL，仓库根目录）：
    python scripts/quant_analyze.py                    # 自动取 reports/ 最新报告
    python scripts/quant_analyze.py --date 2026-09-23
    python scripts/quant_analyze.py --top 10
    python scripts/quant_analyze.py --no-enhance       # 跳过外部量化工具增强
"""

import argparse
import glob
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

from sequoia_x.analysis.scorer import score_pool  # noqa: E402
from sequoia_x.data.universe_filter import StockMetric  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

DB_PATH = os.path.join(ROOT, "data", "sequoia_v2.db")
REPORT_DIR = os.path.join(ROOT, "reports")
SKILL_PATH = os.environ.get("QUANT_SKILL_PATH", "").strip()

# 报告里的策略名 -> 标记字母（与 notify/html_report.STRATEGY_MARKS 对应）
STRATEGY_TAG = {
    "均线放量": "M",
    "海龟突破": "T",
    "高窄旗形": "F",
    "涨停洗盘": "S",
    "上升跌停反包": "D",
    "RPS 突破": "R",
    "定增公告": "P",
}

# 注意 `href="[^"]*"[^>]*>` —— 链接后还有 target/rel 属性，少写 `[^>]*>` 会解析出 0 行。
# 末尾的「评分」列（class="num score"）必须写成**可选**：新版报告有、旧报告没有，
# 写死会让整行匹配失败 → 同样解析出 0 行（同一个坑踩了两次）。
ROW_RE = re.compile(
    r'<tr data-board="[^"]*">'
    r'<td class="code"><a href="[^"]*"[^>]*>(?P<code>\d+)</a></td>'
    r'<td class="name">(?P<name>[^<]*)</td>'
    r'<td class="board">(?P<board>[^<]*)</td>'
    r'<td class="industry">(?P<industry>[^<]*)</td>'
    r'<td class="num">(?P<cap>[^<]*)</td>'
    r'<td class="num">(?P<turn>[^<]*)</td>'
    r'<td class="num">(?P<pe>[^<]*)</td>'
    r'<td class="num">(?P<hold>[^<]*)</td>'
    r'(?:<td class="num score"[^>]*>[^<]*</td>)?'
    r"</tr>"
)
CARD_RE = re.compile(r'<section class="card"[^>]*>\s*<div class="card-head">\s*<h2>([^<]+)</h2>')


def num(value):
    """把报告单元格里的数字转成 float；空值/破折号返回 nan。"""
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return float("nan")


def finite(value: float | None) -> float | None:
    """nan / 空串统一成 None（评分器用 None 表示「数据缺失」）。"""
    if value is None:
        return None
    return value if isinstance(value, float) and math.isfinite(value) else None


def latest_report() -> str | None:
    files = sorted(glob.glob(os.path.join(REPORT_DIR, "stock_report_*.html")))
    return files[-1] if files else None


def parse_report(path: str) -> dict:
    """解析报告，返回 {code: {...}}；同一股票命中多策略时合并标记字母。"""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    # 定位每张卡片的策略名，随后把该卡片区间内的行归属到该策略
    cards = [(m.start(), STRATEGY_TAG.get(m.group(1).strip(), "?")) for m in CARD_RE.finditer(html)]
    pool: dict[str, dict] = {}
    for m in ROW_RE.finditer(html):
        pos = m.start()
        tag = "?"
        for start, letter in cards:
            if start <= pos:
                tag = letter
            else:
                break
        code = m.group("code")
        rec = pool.setdefault(
            code,
            {
                "code": code,
                "name": m.group("name").strip(),
                "board": m.group("board").strip(),
                "industry": m.group("industry").strip(),
                "cap": num(m.group("cap")),
                "turn": num(m.group("turn")),
                "pe": num(m.group("pe")),
                "hold": num(m.group("hold")),
                "tags": set(),
            },
        )
        rec["tags"].add(tag)
    for rec in pool.values():
        rec["tag"] = "".join(sorted(rec["tags"]))
    return pool


def main() -> int:
    ap = argparse.ArgumentParser(description="候选池量价评分（复用 sequoia_x.analysis.scorer）")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD，默认取最新报告")
    ap.add_argument("--top", type=int, default=0, help="只打印前 N 名，0 = 全部")
    ap.add_argument("--no-enhance", action="store_true", help="跳过外部量化工具增强")
    args = ap.parse_args()

    if args.date:
        path = os.path.join(REPORT_DIR, f"stock_report_{args.date}.html")
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

    metrics = {
        code: StockMetric(
            symbol=code,
            pe_ttm=finite(rec["pe"]),
            turn=finite(rec["turn"]),
            circ_market_cap=(finite(rec["cap"]) or 0) * 1e8 or None,
        )
        for code, rec in pool.items()
    }
    holdings = {c: r for c, r in ((k, finite(v["hold"])) for k, v in pool.items()) if r is not None}

    result = score_pool(
        list(pool),
        db_path=DB_PATH,
        metrics=metrics,
        holdings=holdings,
        tags={code: rec["tag"] for code, rec in pool.items()},
        enhance_top=0 if args.no_enhance else len(pool),
        skill_path="" if args.no_enhance else SKILL_PATH,
    )
    print(f"候选池 {len(pool)} 只 → 成功评分 {len(result)} 只"
          f"（跳过 {len(pool) - len(result)} 只历史数据不足）\n")
    if not result:
        return 1

    if args.top:
        result = result[: args.top]

    print(f"{'#':<3}{'代码':<8}{'名称':<10}{'S':<5}{'标记':<6}{'基础':<6}{'罚':<5}"
          f"{'当日%':<9}{'20日%':<9}{'60日%':<9}{'量比':<7}{'距高%':<8}{'MA20偏%':<9}"
          f"{'波动%':<8}{'连涨':<6}{'PE':<9}{'筹码%':<8}{'胜率%':<8}{'回撤%'}")
    print("-" * 168)
    for i, d in enumerate(result, 1):
        rec = pool.get(d.symbol, {})
        print(
            f"{i:<3}{d.symbol:<8}{rec.get('name', ''):<10}{d.score:<5}{d.tags or '-':<6}"
            f"{d.total:<6.0f}{d.penalty:<5.0f}"
            f"{(d.chg1 if d.chg1 is not None else float('nan')):>6.2f}   "
            f"{(d.chg20 if d.chg20 is not None else float('nan')):>6.2f}   "
            f"{(d.chg60 if d.chg60 is not None else float('nan')):>6.2f}   "
            f"{(d.vol_ratio if d.vol_ratio is not None else float('nan')):>5.2f}  "
            f"{(1 - d.near_high) * 100 if d.near_high else float('nan'):>5.2f}   "
            f"{(d.dev_ma20 if d.dev_ma20 is not None else float('nan')):>6.2f}   "
            f"{(d.vol20 if d.vol20 is not None else float('nan')):>5.1f}   "
            f"{d.streak:<6}{rec.get('pe', float('nan')):<9.1f}"
            f"{rec.get('hold', float('nan')):<8.1f}"
            f"{(d.prob_up if d.prob_up is not None else float('nan')):>6.1f}  "
            f"{(d.max_drawdown if d.max_drawdown is not None else float('nan')):>6.1f}"
        )

    print("\n--- 分组提示 ---")
    print("接近新高(距高<5%):", [pool.get(r.symbol, {}).get("name", r.symbol)
                                 for r in result if (r.near_high or 0) >= 0.95])
    print("深度回撤(距高>10%):", [pool.get(r.symbol, {}).get("name", r.symbol)
                                  for r in result if (r.near_high or 1) < 0.90])
    print("放量上涨(量比>=1.3):", [pool.get(r.symbol, {}).get("name", r.symbol)
                                   for r in result if (r.chg1 or 0) > 0 and (r.vol_ratio or 0) >= 1.3])
    print("偏离MA20>40%(重度追高):", [f"{pool.get(r.symbol, {}).get('name', r.symbol)}"
                                      f"({r.dev_ma20:.1f}%)"
                                      for r in result if (r.dev_ma20 or 0) > 40])
    print("被扣分(位置/波动):", [f"{pool.get(r.symbol, {}).get('name', r.symbol)}(-{r.penalty:.0f})"
                                 for r in result if r.penalty > 0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
