"""一次性分析脚本：评估各精筛维度对当前选股结果的收敛效果。

不重新跑策略，直接读取最近一次生成的 HTML 报告拿到各策略候选，
再叠加不同维度的过滤配置，输出剩余数量对比，便于确定阈值。
"""

import re
import socket

socket.setdefaulttimeout(20.0)

from dotenv import load_dotenv

load_dotenv()

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.data.universe_filter import UniverseFilter

REPORT = "reports/stock_report_2026-09-19.html"

html = open(REPORT, encoding="utf-8").read()
blocks = re.split(r'<section class="card"', html)[1:]

per: dict[str, list[str]] = {}
for b in blocks:
    label = re.search(r"<h2>([^<]*)</h2>", b).group(1)
    rows = re.findall(r'<td class="code">.*?>(\d{6})</a>', b)
    per[label] = rows

order = ["均线放量", "海龟突破", "高窄旗形", "涨停洗盘", "上升跌停反包", "RPS 突破", "定增公告"]
per = {k: per[k] for k in order if k in per}
union = sorted({c for v in per.values() for c in v})

print("基线各策略: " + "  ".join(f"{k}={len(v)}" for k, v in per.items()))
print(f"去重合计: {len(union)} 只")
print()

base = Settings(feishu_webhook_url="x", _env_file=None)
engine = DataEngine(base)


def make(**over):
    return Settings(feishu_webhook_url="x", _env_file=None, **over)


SCENARIOS = [
    ("基线：市值>=100亿, PE>=0", dict(min_market_cap=100, min_pe=0)),
    ("+ 成交额>=2亿", dict(min_market_cap=100, min_pe=0, min_turnover=2)),
    ("+ 成交额>=5亿", dict(min_market_cap=100, min_pe=0, min_turnover=5)),
    ("+ 换手率 1~15%", dict(min_market_cap=100, min_pe=0, min_turn=1, max_turn=15)),
    ("+ 换手率 2~10%", dict(min_market_cap=100, min_pe=0, min_turn=2, max_turn=10)),
    ("+ PB <=8", dict(min_market_cap=100, min_pe=0, max_pb=8)),
    ("+ 行业含 电子/软件/医药", dict(min_market_cap=100, min_pe=0, include_industries="电子,软件,医药")),
    ("+ 排除 房地产/银行/金融/建筑", dict(min_market_cap=100, min_pe=0, exclude_industries="房地产,银行,金融,建筑")),
    ("+ 市值 100~500亿", dict(min_market_cap=100, max_market_cap=500, min_pe=0)),
    ("组合A: 成交额>=2亿 + 换手1~15", dict(min_market_cap=100, min_pe=0, min_turnover=2, min_turn=1, max_turn=15)),
    ("组合B: 组合A + 行业含电子/软件/医药", dict(min_market_cap=100, min_pe=0, min_turnover=2, min_turn=1, max_turn=15, include_industries="电子,软件,医药")),
    ("组合C: 组合A + 市值 100~500亿", dict(min_market_cap=100, max_market_cap=500, min_pe=0, min_turnover=2, min_turn=1, max_turn=15)),
    ("组合D: 成交额>=5亿 + 换手2~10", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10)),
    ("组合E: 组合D + PB<=10", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10, max_pb=10)),
    ("组合F: 组合D + 市值 100~800亿", dict(min_market_cap=100, max_market_cap=800, min_pe=0, min_turnover=5, min_turn=2, max_turn=10)),
    ("组合G: 组合D + 排除地产/银行/金融/建筑/公用", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10, exclude_industries="房地产,银行,金融,建筑,公用")),
    ("H: 成交额>=5亿 + 换手1~15", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=1, max_turn=15)),
    ("I: 成交额>=5亿 + 换手2~10 + 行业电软医", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10, include_industries="电子,软件,医药")),
    ("J: I + PB<=10", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10, max_pb=10, include_industries="电子,软件,医药")),
    ("K: 成交额>=5亿 + 换手2~10 + PB<=10", dict(min_market_cap=100, min_pe=0, min_turnover=5, min_turn=2, max_turn=10, max_pb=10)),
]

import logging

seq = logging.getLogger("sequoia_x.data.universe_filter")

W = 96
print(f"{'场景':<46}{'保留':>6}{'剔除':>6}   主要剔除原因")
print("-" * W)

for name, cfg in SCENARIOS:
    f = UniverseFilter(settings=make(**cfg), engine=engine)
    allkept: set[str] = set()
    dropped = 0

    prev = seq.level
    seq.setLevel(logging.ERROR)  # 静默逐策略明细日志
    for k in per:
        kept = f.apply(per[k])
        allkept.update(kept)
        dropped += len(per[k]) - len(kept)
    seq.setLevel(prev)

    # 汇总一次全局剔除原因（重新跑一遍拿 reasons 代价高，这里用差值近似）
    print(f"{name:<46}{len(allkept):>6}{dropped:>6}")

print("-" * W)
print("注：'保留'为跨策略去重后的股票数；'剔除'为各策略剔除次数之和（含重复）。")
print(f"基线去重合计：{len(union)} 只。")

# ── 基线 92 只的行业分布 ──
print()
print("=" * W)
print("基线 92 只的行业分布（baostock 证监会行业分类）")
print("=" * W)

from sequoia_x.data import stock_meta as sm

metas = sm.load_stock_meta()
hist: dict[str, int] = {}
for code in union:
    meta = metas.get(code)
    ind = (meta.industry if meta else None) or "（未知）"
    hist[ind] = hist.get(ind, 0) + 1

for ind, n in sorted(hist.items(), key=lambda kv: -kv[1]):
    bar = "█" * n
    print(f"{n:>3}  {ind:<28}{bar}")
