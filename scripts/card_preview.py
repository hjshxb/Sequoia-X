"""从已生成的 HTML 报告还原数据，用真实渲染代码打印飞书卡片内容（离线，不发送）。

用途：在本机预览「飞书汇总卡片」的实际版式，不必重复跑全市场预筛。
用完即删。
"""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

import sequoia_x.notify.feishu as feishu_module  # noqa: E402
from sequoia_x.core.config import Settings  # noqa: E402
from sequoia_x.data.stock_meta import StockMeta  # noqa: E402
from sequoia_x.data.universe_filter import UniverseFilter  # noqa: E402
from sequoia_x.notify.feishu import FeishuNotifier  # noqa: E402
from sequoia_x.notify.html_report import STRATEGY_LABELS  # noqa: E402

reports = sorted((root / "reports").glob("stock_report_*.html"))
if not reports:
    raise SystemExit("没有找到报告文件")
path = reports[-1]
html = path.read_text(encoding="utf-8")
print(f"# 数据来源：{path.name}")

label_to_class = {v: k for k, v in STRATEGY_LABELS.items()}

row_re = re.compile(
    r'data-board="([^"]+)"'
    r'|<td class="code"><a[^>]*>(\d{6})</a></td>\s*<td class="name">([^<]*)</td>'
)

results: dict[str, list[str]] = {}
names: dict[str, str] = {}

for chunk in html.split('<section class="card"')[1:]:
    m = re.search(r"<h2>(.*?)</h2>", chunk)
    if not m:
        continue
    label = m.group(1)
    codes: list[str] = []
    for hit in row_re.finditer(chunk):
        board, code, name = hit.groups()
        if code:
            codes.append(code)
            if name and name != "—":
                names[code] = name
    results[label_to_class.get(label, label)] = codes

# 屏蔽 baostock：名称直接用报告里已有的，保证离线且与报告一致
meta = {c: StockMeta(c, n, None) for c, n in names.items()}
feishu_module.stock_meta_module.load_stock_meta = lambda: meta  # type: ignore[assignment]

settings = Settings(_env_file=root / ".env")
desc = UniverseFilter(settings=settings, engine=None).describe(brief=True).removeprefix("精筛：")

card = FeishuNotifier(settings)._build_report_card(results, filter_desc=desc)

print("# 各策略只数：{k: len(v) for k, v in results.items()}")
print(f"# 命中策略数：{sum(1 for v in results.values() if v)} / {len(results)}")
print(f"# 选股总数：{sum(len(v) for v in results.values())}")
print()
print("=" * 72)
for el in card["card"]["elements"]:
    if el["tag"] == "hr":
        print("-" * 72)
        continue
    print(el["text"]["content"])
print("=" * 72)
print()
print("### raw json ###")
print(json.dumps(card["card"]["elements"], ensure_ascii=False, indent=2))
