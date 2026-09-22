"""按当前 .env 精筛配置重新生成 HTML 报告（跳过全市场增量同步）。

目录约定 —— `reports/` 是**唯一**的 HTML 输出目录，已在 `.gitignore` 中整体忽略：
    candidates_<日期>.html    策略候选快照（冻结的输入源，供离线分析 / 重算用）
    stock_report_<日期>.html  生成的选股报告（本脚本与 main.py 的输出）

适用场景：非交易日（周末 / 节假日）本地库不会新增数据，此时 `main.py` 的
全市场增量同步只是空转（每只股票都发一次请求却拉不到新行），可能耗时数十分钟。
既然数据没变、策略又都是确定性的，直接复用候选快照再套一层精筛，
结果与完整跑一遍 `main.py` 等价，但只需一两分钟。
"""

import re
import socket
import sys
from datetime import date

socket.setdefaulttimeout(20.0)

import logging

from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.data import stock_meta
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter
from sequoia_x.notify.html_report import HtmlReportGenerator


def _find_candidates() -> Path:
    """找候选快照：优先今天的，否则用最新一个（非交易日会落到上一交易日）。"""
    today = Path(f"reports/candidates_{date.today().strftime('%Y-%m-%d')}.html")
    if today.exists():
        return today
    found = sorted(Path("reports").glob("candidates_*.html"))
    if not found:
        sys.exit("reports/ 下没有 candidates_*.html；请先跑一次 `python main.py`。")
    return found[-1]


SRC = _find_candidates()
# 报告按「数据日期」命名，而不是当天日期 —— 周末/节假日跑出来的报告
# 代表的仍是上一个交易日的行情，用 wall-clock 日期命名会误导。
DATE = SRC.stem.removeprefix("candidates_")
OUT = Path("reports") / f"stock_report_{DATE}.html"

html = SRC.read_text(encoding="utf-8")

# 按策略拆出候选股（候选快照本身只经过了「市值 + PE」这一层基础筛选）
blocks = re.split(r'<section class="card"', html)[1:]

raw: dict[str, list[str]] = {}
for block in blocks:
    label = re.search(r"<h2>([^<]*)</h2>", block).group(1)
    raw[label] = re.findall(r'<td class="code">.*?>(\d{6})</a>', block)

settings = Settings()
engine = DataEngine(settings)
# 名称/行业落盘缓存：命中就不联网（本脚本常用于数据源不可用的场景，尤其重要）
stock_meta.configure_cache(settings.db_path, settings.stock_meta_ttl_days)
universe = UniverseFilter(settings=settings, engine=engine)

print(f"精筛条件：{universe.describe()}")
print()
print(f"{'策略':<16}{'候选':>6}{'精筛后':>8}")
print("-" * 32)

logging.getLogger("sequoia_x.data.universe_filter").setLevel(logging.WARNING)

results: dict[str, list[str]] = {}
for name, codes in raw.items():
    kept = universe.apply(codes) if codes else []
    results[name] = kept
    print(f"{name:<16}{len(codes):>6}{len(kept):>8}")

union = sorted({code for codes in results.values() for code in codes})
print("-" * 32)
print(f"{'合计去重':<16}{'':>6}{len(union):>8}")

meta = stock_meta.load_stock_meta()

print()
print(f"最终名单（{len(union)} 只）：")
for code in union:
    info = meta.get(code)
    name = info.name if info else "—"
    industry = info.industry if info else "—"
    print(f"  {code}  {name:<10}{industry}")

metrics = universe.fetch_metrics(union) if union else {}
path = HtmlReportGenerator(settings).generate(
    results,
    filter_desc=universe.describe(),
    output_path=OUT,
    metrics=metrics,
    # 与 main.py 保持一致：仅在配置了筹码维度时展示该列，复用已加载的快照
    holdings=universe.cached_holder_ratios(),
)
print()
print(f"候选快照：{SRC}")
print(f"报告已生成：{path.resolve()}")
