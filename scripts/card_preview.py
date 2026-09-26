"""从已生成的 HTML 报告还原数据，用真实渲染代码打印飞书卡片内容（离线，不发送）。

用途：在本机预览「飞书汇总卡片」的实际版式，不必重复跑全市场预筛。

用法（WSL，仓库根目录）：
    python scripts/card_preview.py                  # 取日期最新的一份报告
    python scripts/card_preview.py --date 2026-09-19

⚠️ 这个脚本是**生产推送的镜像**：必须与 `main.py` 实际传给
`FeishuNotifier.send_report()` 的参数保持一致（含 `scores=`），
否则预览出来的卡片会比真实推送少小节，看起来"格式变了"。
2026-09-24 修过一次：原先没传 `scores`，导致预览里看不到「🎯 量化评分 Top 5」，
板块内部的排序也和真实卡片不同。同一处后来又补过一刀：卡片新增胜率展示后，
`prob_up`（以及决定排序的 `prob_confidence`、展示用的 `prob_samples`）都得从
排行表对应列还原出来，否则预览板块的**顺序**会和真实卡片不一样。

同日更晚一刀：卡片的「量化评分 Top 5」+「形态匹配 Top 5」两个小节**合并**成
一个「🎯 综合评分 Top 10」（按 `scorer.composite_score` 排序，见
`feishu._composite_section`）。这里无需改逻辑 —— 综合分由 `feishu` 内部现算，
本脚本只管把 `prob_up / prob_samples / prob_confidence` 三个字段喂全，
缺一个综合分就会退化（胜率不当权重、预览顺序与真实卡片不符）。

再一刀（同步完整性）：卡片摘要多一行「**数据日期：** …」，值来自
`main._format_data_status`。这里从报告页首的 `<span class="status">` 还原 ——
报告与卡片是同一份值渲染两次，所以必须传进去，不传预览就少一行。
"""

import argparse
import html as html_module
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

# 默认取「日期最新」的一份报告，与真实推送取当天报告的口径一致。
# 加 `--date` 是为了验证历史报告（例如刚改了渲染层，想看旧报告长什么样）。
parser = argparse.ArgumentParser(description="离线预览飞书汇总卡片（不发送）")
parser.add_argument("--date", help="报告日期 YYYY-MM-DD，默认取最新一份")
args = parser.parse_args()

reports = sorted((root / "reports").glob("stock_report_*.html"))
if args.date:
    reports = [p for p in reports if p.stem == f"stock_report_{args.date}"]
if not reports:
    raise SystemExit(f"没有找到报告文件{'：' + args.date if args.date else ''}")
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

# ── 评分：从报告页首的「综合评分排行」表还原 ──
# 真实推送里的分数来自 production 的 scorer；这里不重算（重算会因缺少
# 估值/筹码入参而与报告里的分数不一致），而是直接读报告里已经算好的结果。
# 锚定开头的 `<td class="rank">` 是刻意的：策略卡片里的行也有
# `<td class="num score">` + `<td class="marks">`，但**没有** rank 单元格
# （它前面是 industry/市值/换手/PE/十大流通 等列）。锚住 rank 就能保证
# 只匹配排行表，不会把策略表里的行重复算进 `scores`。
# 「综合」列（class="num composite"）写成**可选**：它排在「评分」与「标记」
# 之间，新版报告有、刚改前的报告没有 —— 不写可选会把旧报告解析成 0 行。
rank_re = re.compile(
    r'<td class="rank">\d+</td>\s*'
    r'<td class="code"><a[^>]*>(\d{6})</a></td>\s*'
    r'<td class="name">([^<]*)</td>\s*'
    r'<td class="board">[^<]*</td>\s*'
    r'<td class="num score"[^>]*>([\d.]+)</td>\s*'
    r'(?:<td class="num composite"[^>]*>[^<]*</td>\s*)?'
    r'<td class="marks">([^<]*)</td>\s*'
    r"(.*?)</tr>"
)

_CELL_RE = re.compile(r'<td class="num">([^<]*)</td>')

# 排行表「标记」列之后的列顺序（见 html_report._render_ranking）：
#   旧报告：当日 / 20日 / 量比 / 距高 / MA20偏离 / 波动 / **胜率** / 回撤
#   新报告：在上面基础上，胜率后插入 **窗口** / **置信** 两列
# ⚠️ 不能写死正向下标：旧报告没有「窗口」「置信」，写死 7 会正好取到「回撤」，
# 预览里就会出现「胜率 -21/-21」这种鬼值（2026-09-24 实测踩到）。
# 改为**从行尾倒着数**（回撤永远是最末一列），并看表头有没有「窗口」决定偏移。
# 「综合」列用的是 `class="num composite"`（**不是** `class="num"`），所以下面
# 的 `_CELL_RE` 抓不到它、不会把行尾的倒数下标整体挪位 —— 加列时特意用独立
# class 就是为了不动这套下标。
# 卡片的「综合评分 Top 10」要用到这三个字段：`prob_up` 用于显示胜率，
# `prob_confidence` **参与综合分计算（决定排序）**（缺了它预览顺序就跟真实
# 卡片不一样），`prob_samples` 用于把胜率显示成 `k/n`。
_HAS_WINDOW_COLS = "<th>窗口</th>" in html

if _HAS_WINDOW_COLS:
    _WINRATE_IDX: int | None = -4
    _SAMPLES_IDX: int | None = -3
    _CONFIDENCE_IDX: int | None = -2
else:
    _WINRATE_IDX, _SAMPLES_IDX, _CONFIDENCE_IDX = -2, None, None


def _num_from_tail(tail: str, index: int | None) -> float | None:
    """从排行表行尾的 `<td class="num">` 里按（倒数）下标取值；缺列/「—」返回 None。

    `index` 为负表示从末尾数（`-1` = 最后一列）。传 `None` 表示这张报告
    根本没有这一列（例如旧报告的「窗口」「置信」）。
    """
    if index is None:
        return None
    cells = _CELL_RE.findall(tail)
    if len(cells) < abs(index):
        return None
    raw = cells[index].strip().removesuffix("%")
    if not raw or raw == "—":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _prob_up_from_tail(tail: str) -> float | None:
    """从排行表行尾的 `<td class="num">` 里取胜率；缺失或「—」返回 None。"""
    return _num_from_tail(tail, _WINRATE_IDX)


def _placeholder(
    symbol: str,
    score: float,
    tags: str,
    prob_up: float | None = None,
    prob_samples: int | None = None,
    prob_confidence: float | None = None,
) -> ScoreDetail:
    """用报告里读得到的字段构造 ScoreDetail，其余填占位值。

    卡片渲染只用到 `symbol` / `score` / `tags` / `prob_up` / `prob_samples`
    / `prob_confidence`（见 `feishu._composite_section` 与 `scorer.composite_score`），
    其余量价字段在卡片里不出现，故无需还原。
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
        prob_up=prob_up,
        prob_samples=prob_samples,
        prob_confidence=prob_confidence,
    )


scores: list[ScoreDetail] = []
ranking_chunks = html.split('<section class="card ranking"')[1:]
if ranking_chunks:
    for hit in rank_re.finditer(ranking_chunks[0]):
        symbol, name, score, marks, tail = hit.groups()
        if name and name != "—":
            names.setdefault(symbol, name)
        samples = _num_from_tail(tail, _SAMPLES_IDX)
        scores.append(
            _placeholder(
                symbol,
                float(score),
                "" if marks == "—" else marks,
                _prob_up_from_tail(tail),
                int(samples) if samples is not None else None,
                _num_from_tail(tail, _CONFIDENCE_IDX),
            )
        )
with_winrate = sum(1 for d in scores if d.prob_up is not None)
with_conf = sum(1 for d in scores if d.prob_confidence is not None)
print(
    f"# 评分排行还原：{len(scores)} 只，其中带胜率 {with_winrate} 只、"
    f"带匹配可信度 {with_conf} 只（缺胜率时综合分退化为评分）"
)

# 屏蔽 baostock：名称直接用报告里已有的，保证离线且与报告一致
meta = {c: StockMeta(c, n, None) for c, n in names.items()}
feishu_module.stock_meta_module.load_stock_meta = lambda: meta  # type: ignore[assignment]

settings = Settings(_env_file=root / ".env")
desc = UniverseFilter(settings=settings, engine=None).describe(brief=True).removeprefix("精筛：")

# 数据状态：报告页首那个 `<span class="status">数据日期：…</span>` 里就是卡片要发的值。
# 旧报告没有这一行 ⇒ 取到空串，卡片少一行（与当时的真实卡片一致）。
status_match = re.search(r'<span class="status">数据日期：([^<]*)</span>', html)
data_status = html_module.unescape(status_match.group(1)).strip() if status_match else ""

card = FeishuNotifier(settings)._build_report_card(
    results, filter_desc=desc, scores=scores, data_status=data_status
)

print("# 各策略只数：" + "、".join(f"{k} {len(v)}" for k, v in results.items()))
print(f"# 命中策略数：{sum(1 for v in results.values() if v)} / {len(results)}")
print(f"# 选股总数：{sum(len(v) for v in results.values())}")
print(f"# 数据状态：{data_status or '（报告未记录）'}")
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
