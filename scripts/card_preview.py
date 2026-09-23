"""从已生成的 HTML 报告还原数据，用真实渲染代码打印飞书卡片内容（离线，不发送）。

用途：在本机预览「飞书汇总卡片」的实际版式，不必重复跑全市场预筛。

⚠️ 这个脚本是**生产推送的镜像**：必须与 `main.py` 实际传给
`FeishuNotifier.send_report()` 的参数保持一致（含 `scores=`），
否则预览出来的卡片会比真实推送少小节，看起来"格式变了"。
2026-09-24 修过一次：原先没传 `scores`，导致预览里看不到「量化评分 Top 5」，
板块内部的排序也和真实卡片不同。
"""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

import sequoia_x.notify.feishu as feishu_module  # noqa: E402
from sequoia_x.analysis.scorer import ScoreDetail  # noqa: E402
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

# 策略小节：`split` 的锚点刻意写成 '<section class="card"'（结尾带引号），
# 这样页首的 `<section class="card ranking"` 会被排除，不会被当成一个策略。
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

# ── 评分：从报告页首的「量化评分排行」表还原 ──
# 真实推送里的分数来自 production 的 scorer；这里不重算（重算会因缺少
# 估值/筹码入参而与报告里的分数不一致），而是直接读报告里已经算好的结果。
# 锚定开头的 `<td class="rank">` 是刻意的：策略卡片里的行也有
# `<td class="num score">` + `<td class="marks">`，但**没有** rank 单元格
# （它前面是 industry/市值/换手/PE/十大流通 等列）。锚住 rank 就能保证
# 只匹配排行表，不会把策略表里的行重复算进 `scores`。
rank_re = re.compile(
    r'<td class="rank">\d+</td>\s*'
    r'<td class="code"><a[^>]*>(\d{6})</a></td>\s*'
    r'<td class="name">([^<]*)</td>\s*'
    r'<td class="board">[^<]*</td>\s*'
    r'<td class="num score"[^>]*>([\d.]+)</td>\s*'
    r'<td class="marks">([^<]*)</td>'
)


def _placeholder(symbol: str, score: float, tags: str) -> ScoreDetail:
    """用报告里读得到的三个字段构造 ScoreDetail，其余填占位值。

    卡片渲染只用到 `symbol` / `score` / `tags`（见 `feishu._ranking_section`
    与 `group_by_board(order=…)`），其余量价字段在卡片里不出现，故无需还原。
    """
    return ScoreDetail(
        symbol=symbol,
        last_date="",
        tags=tags,
        chg1=None,
        chg20=None,
        chg60=None,
        vol_ratio=None,
        near_high=None,
        dev_ma20=None,
        vol20=None,
        streak=0,
        bull=0,
        amount=None,
        new_high60=False,
        new_high120=False,
        adjusted=score,
    )


scores: list[ScoreDetail] = []
ranking_chunks = html.split('<section class="card ranking"')[1:]
if ranking_chunks:
    for hit in rank_re.finditer(ranking_chunks[0]):
        symbol, name, score, marks = hit.groups()
        if name and name != "—":
            names.setdefault(symbol, name)
        scores.append(_placeholder(symbol, float(score), "" if marks == "—" else marks))
print(f"# 评分排行还原：{len(scores)} 只（报告里没有排行表时为 0，卡片会少一节）")

# 屏蔽 baostock：名称直接用报告里已有的，保证离线且与报告一致
meta = {c: StockMeta(c, n, None) for c, n in names.items()}
feishu_module.stock_meta_module.load_stock_meta = lambda: meta  # type: ignore[assignment]

settings = Settings(_env_file=root / ".env")
desc = UniverseFilter(settings=settings, engine=None).describe(brief=True).removeprefix("精筛：")

card = FeishuNotifier(settings)._build_report_card(results, filter_desc=desc, scores=scores)

print("# 各策略只数：" + "、".join(f"{k} {len(v)}" for k, v in results.items()))
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
