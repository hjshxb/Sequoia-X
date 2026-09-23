"""HTML 选股报告模块：把各策略的选股结果渲染成本地单文件 HTML。

设计要点：
    - 单文件输出，CSS/JS 全部内联，双击即可用浏览器打开，无需任何依赖或网络。
    - 按策略分块展示；每个策略内部**按上市板块分组排序**
      （主板 → 创业板 → 科创板 → 北交所 → 其他），板块内按代码升序。
    - 每只股票给出「代码 / 名称 / 板块 / 行业」，代码可点击跳转雪球；
      传入 `metrics` 时额外展示「市值 / 换手率 / PE(TTM)」三列。
    - 股票名称与行业由数据层 `sequoia_x.data.stock_meta` 提供（一次请求 + 进程内缓存），
      与「行业过滤」共用同一份数据，不重复请求；板块由代码前缀本地判定，零开销。
    - 报告生成失败不影响主流程（调用方自行兜底），也不依赖飞书是否配置。
"""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from sequoia_x.analysis.scorer import ScoreDetail
from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import BOARD_ORDER, StockMeta, board_of, board_rank
from sequoia_x.data.universe_filter import StockMetric

logger = get_logger(__name__)

# 基础列；传入 metrics / holdings 时再追加指标列
_BASE_COLUMNS: tuple[str, ...] = ("代码", "名称", "板块", "行业")
_METRIC_COLUMNS: tuple[str, ...] = ("市值(亿)", "换手率", "PE(TTM)")
_HOLDING_COLUMNS: tuple[str, ...] = ("十大流通(%)",)

YI = 1e8

# 策略类名 -> 中文展示名。未收录的策略回退为类名本身。
# 这份映射是「展示层公共词汇」：本地 HTML 报告与飞书推送都从这里取，
# 保证两处对同一策略的叫法一致（见 feishu.py）。
STRATEGY_LABELS: dict[str, str] = {
    "MaVolumeStrategy": "均线放量",
    "TurtleTradeStrategy": "海龟突破",
    "HighTightFlagStrategy": "高窄旗形",
    "LimitUpShakeoutStrategy": "涨停洗盘",
    "UptrendLimitDownStrategy": "上升跌停反包",
    "RpsBreakoutStrategy": "RPS 突破",
    "PrivatePlacementStrategy": "定增公告",
}

# 兼容旧引用（原名带下划线，仅供历史代码/测试使用）
_STRATEGY_LABELS = STRATEGY_LABELS

# 策略类名 -> 单字母标记，用于一眼看出「这只命中过哪些策略」。
# 与 STRATEGY_LABELS 同属展示层公共词汇；评分器只接收拼好的字母串，
# 不依赖策略名（分析层不该知道有哪些策略）。
STRATEGY_MARKS: dict[str, str] = {
    "MaVolumeStrategy": "M",
    "TurtleTradeStrategy": "T",
    "HighTightFlagStrategy": "F",
    "LimitUpShakeoutStrategy": "S",
    "UptrendLimitDownStrategy": "D",
    "RpsBreakoutStrategy": "R",
    "PrivatePlacementStrategy": "P",
}


def strategy_label(strategy_name: str) -> str:
    """取策略的中文展示名；未收录时回退为类名本身。"""
    return STRATEGY_LABELS.get(strategy_name, strategy_name)


def build_symbol_marks(results: Mapping[str, list[str]]) -> dict[str, str]:
    """把 {策略类名: 代码列表} 压成 {代码: 字母标记}。

    同一只股票被多个策略选中时字母合并并按字典序排列，例如同时命中海龟与
    RPS 得到 `"RT"`。排序保证报告与飞书卡片对同一只股票给出相同标记。
    """
    marks: dict[str, set[str]] = {}
    for strategy_name, symbols in results.items():
        letter = STRATEGY_MARKS.get(strategy_name)
        if not letter:
            continue
        for symbol in symbols:
            marks.setdefault(symbol, set()).add(letter)
    return {symbol: "".join(sorted(letters)) for symbol, letters in marks.items()}


def to_xueqiu_code(symbol: str) -> str:
    """把纯数字代码转成雪球代码：6开头→SH，4/8开头→BJ，其余→SZ。"""
    if symbol.startswith("6"):
        return f"SH{symbol}"
    if symbol.startswith(("4", "8")):
        return f"BJ{symbol}"
    return f"SZ{symbol}"


def group_by_board(
    symbols: list[str], order: Mapping[str, int] | None = None
) -> list[tuple[str, list[str]]]:
    """按上市板块归类并排序。

    板块顺序为 主板 → 创业板 → 科创板 → 北交所 → 其他。
    板块内默认按**代码升序**；传入 `order`（如「代码 → 评分名次」）时改为按该
    名次升序 —— 既保留板块分组的可读性，又让高分股排在前面。

    Args:
        symbols: 纯数字股票代码列表。
        order: 可选 {代码: 名次}。未收录的代码排在所属板块末尾（不会丢失）。

    Returns:
        [(板块名, 该板块下的代码列表)]，按板块顺序排列。
    """

    def sort_key(symbol: str) -> tuple:
        if order is None:
            return (board_rank(symbol), symbol)
        return (board_rank(symbol), order.get(symbol, 1 << 30), symbol)

    buckets: dict[str, list[str]] = {}
    for symbol in sorted(symbols, key=sort_key):
        buckets.setdefault(board_of(symbol), []).append(symbol)
    return [(board, buckets[board]) for board in BOARD_ORDER if board in buckets]


def _fmt_cap(metric: StockMetric | None) -> str:
    """流通市值，单位亿元（千分位，无小数）。"""
    if metric is None or metric.circ_market_cap is None:
        return "—"
    return f"{metric.circ_market_cap / YI:,.0f}"


def _fmt_turn(metric: StockMetric | None) -> str:
    """换手率，单位 %（两位小数）。"""
    if metric is None or metric.turn is None:
        return "—"
    return f"{metric.turn:.2f}"


def _fmt_pe(metric: StockMetric | None) -> str:
    """滚动市盈率（一位小数）。"""
    if metric is None or metric.pe_ttm is None:
        return "—"
    return f"{metric.pe_ttm:.1f}"


def _fmt_holding(ratio: float | None) -> str:
    """前十大流通股东合计占流通股比例（%，一位小数）。"""
    if ratio is None:
        return "—"
    return f"{ratio:.1f}"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    """通用数值格式化；None / 非有限值一律显示为「—」。

    评分卡片里的指标缺失是常态（历史不足、增强层未启用），统一走这里，
    避免每处都重复写 None 判断。
    """
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2, signed: bool = False) -> str:
    """百分比格式化；`signed=True` 时带正负号（涨跌幅这种有方向的量）。"""
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def _fmt_int(value: float | int | None) -> str:
    """整数格式化（窗口数、可信度这种没有小数的量）；缺失显示为「—」。"""
    if value is None:
        return "—"
    try:
        if not math.isfinite(float(value)):
            return "—"
    except (TypeError, ValueError):
        return "—"
    return f"{round(float(value)):d}"


def winrate_text(detail: ScoreDetail) -> str:
    """胜率的展示文本：有窗口数时写成 `k/n`，否则退回百分比。

    `prob_up` 就是 `k / len(matches) * 100`（读工具源码确认），所以 `k/n`
    是**精确**还原、不是近似。分母是相似度最高的那 5 个窗口，**不是**候选总数
    `prob_candidates` —— 把候选数当分母会写出「20% 却显示 1/7」这种假分数。

    写成 `k/n` 是为了让人一眼看到分母有多小：`1/1` 和 `4/5` 都是「100%」，
    但含义天差地别。窗口数缺失（旧报告 / 未启用增强）时保持百分比写法。

    飞书卡片与 HTML 报告共用这一份实现（`feishu` 从本模块导入），
    避免两处对同一字段各写一套格式化。
    """
    up = detail.prob_up or 0.0
    if detail.prob_samples:
        return f"{round(up / 100 * detail.prob_samples)}/{detail.prob_samples}"
    return f"{up:.0f}%"


class HtmlReportGenerator:
    """把各策略选股结果渲染成本地单文件 HTML 报告。"""

    def __init__(self, settings: Settings) -> None:
        """
        Args:
            settings: 提供 report_dir 等配置。
        """
        self.settings = settings

    # ── 路径 ──

    def default_path(self) -> Path:
        """默认报告路径：<report_dir>/stock_report_<日期>.html"""
        today = date.today().strftime("%Y-%m-%d")
        return Path(self.settings.report_dir) / f"stock_report_{today}.html"

    # ── 生成 ──

    def generate(
        self,
        results: dict[str, list[str]],
        filter_desc: str = "",
        output_path: str | Path | None = None,
        metrics: dict[str, StockMetric] | None = None,
        holdings: dict[str, float] | None = None,
        scores: Sequence[ScoreDetail] | None = None,
    ) -> Path:
        """生成 HTML 报告并写入磁盘。

        Args:
            results: {策略类名: 选股代码列表}。
            filter_desc: 精筛条件的描述文本，展示在报告页首。
            output_path: 输出路径；为 None 时使用 default_path()。
            metrics: 可选的 {代码: StockMetric}。传入后额外展示
                市值 / 换手率 / PE(TTM) 三列；缺数据的股票显示为「—」。
            holdings: 可选的 {代码: 前十大流通股东合计占流通股比例(%)}。
                传入后额外展示「十大流通(%)」一列，便于回看与调阈值。
            scores: 可选的量化评分明细（应按得分降序）。传入后：页首多一张
                「量化评分排行」卡片、各策略卡片追加「评分」列，且板块分组
                **内部**改按评分降序排列（板块之间的先后顺序不变）。

        Returns:
            实际写入的报告文件路径。
        """
        meta = stock_meta_module.load_stock_meta()
        if metrics is None:
            metrics = {}
        if holdings is None:
            holdings = {}
        score_list = list(scores or [])
        missing = 0
        for symbols in results.values():
            for symbol in symbols:
                if symbol not in meta:
                    missing += 1
        if missing:
            logger.warning(f"HTML 报告：{missing} 条记录缺少名称/行业，将显示为「—」")

        content = self._render(results, meta, filter_desc, metrics, holdings, score_list)

        path = Path(output_path) if output_path else self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # ── 渲染 ──

    def _render(
        self,
        results: dict[str, list[str]],
        meta: dict[str, StockMeta],
        filter_desc: str,
        metrics: dict[str, StockMetric],
        holdings: dict[str, float] | None = None,
        scores: Sequence[ScoreDetail] = (),
    ) -> str:
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(v) for v in results.values())
        hit_strategies = sum(1 for v in results.values() if v)

        show_metrics = bool(metrics)
        # 只有部分股票有股东数据时也展示该列，缺失的显示为「—」
        holdings = holdings or {}
        show_holdings = bool(holdings)
        # 评分：{代码: 明细} 用于展示分数，{代码: 名次} 用于板块内排序。
        # `scores` 已按得分降序传入，故这里的名次天然就是排名。
        score_of = {d.symbol: d for d in scores}
        show_scores = bool(score_of)
        rank_of = {d.symbol: i for i, d in enumerate(scores)}
        headers = (
            _BASE_COLUMNS
            + (_METRIC_COLUMNS if show_metrics else ())
            + (_HOLDING_COLUMNS if show_holdings else ())
            + (("评分",) if show_scores else ())
        )
        n_cols = len(headers)
        thead = "".join(f"<th>{html.escape(h)}</th>" for h in headers)

        sections: list[str] = []
        for strategy_name, symbols in results.items():
            label = _STRATEGY_LABELS.get(strategy_name, strategy_name)
            if symbols:
                # 有评分时，板块**内部**按评分降序（板块之间的先后顺序不变）
                grouped = group_by_board(symbols, order=rank_of if show_scores else None)
                parts: list[str] = []
                for board, group in grouped:
                    parts.append(self._render_group_row(board, len(group), n_cols))
                    for symbol in group:
                        parts.append(
                            self._render_row(
                                symbol,
                                meta.get(symbol),
                                metrics.get(symbol),
                                holdings.get(symbol),
                                score_of.get(symbol),
                                show_metrics,
                                show_holdings,
                                show_scores,
                            )
                        )
                rows = "\n".join(parts)
                table = f"""<div class="table-wrap">
        <table>
          <thead><tr>{thead}</tr></thead>
          <tbody>
{rows}
          </tbody>
        </table>
      </div>"""
            else:
                table = '<p class="empty">该策略本次无选股结果</p>'

            badges = " · ".join(f"{b} {len(g)}" for b, g in group_by_board(symbols))
            board_summary = f'<span class="boards">{html.escape(badges)}</span>' if badges else ""

            sections.append(
                f"""<section class="card" data-count="{len(symbols)}">
      <div class="card-head">
        <h2>{html.escape(label)}</h2>
        <span class="badge">{len(symbols)}</span>
        {board_summary}
      </div>
      {table}
    </section>"""
            )

        empty_hint = '<p class="empty">本次运行所有策略均无选股结果。</p>' if total == 0 else ""
        filter_line = (
            f'<span class="filter">{html.escape(filter_desc)}</span>' if filter_desc else ""
        )
        ranking_html = self._render_ranking(scores, meta)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sequoia-X 选股报告 {today}</title>
<style>
  :root {{
    --bg: #f5f6f8;
    --card: #ffffff;
    --text: #1a1d21;
    --muted: #6b7280;
    --border: #e5e7eb;
    --accent: #d92b2b;
    --accent-soft: #fdf0f0;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 20px 64px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  header {{ margin-bottom: 24px; }}
  h1 {{
    margin: 0 0 10px;
    font-size: 24px;
    font-weight: 650;
    letter-spacing: -0.01em;
  }}
  h1 .dot {{ color: var(--accent); }}
  .meta {{ color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 8px 18px; }}
  .filter {{
    display: inline-block;
    background: var(--accent-soft);
    color: var(--accent);
    border-radius: 4px;
    padding: 1px 8px;
    font-size: 12px;
  }}
  .toolbar {{ margin: 18px 0 20px; }}
  #q {{
    width: 100%;
    padding: 9px 13px;
    font-size: 14px;
    color: var(--text);
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    outline: none;
  }}
  #q:focus {{ border-color: var(--accent); }}
  #hint {{ color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 18px; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px 6px;
    margin-bottom: 16px;
  }}
  .card-head {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }}
  h2 {{ margin: 0; font-size: 16px; font-weight: 620; }}
  .badge {{
    background: var(--accent);
    color: #fff;
    border-radius: 10px;
    padding: 1px 9px;
    font-size: 12px;
    font-weight: 600;
    line-height: 18px;
  }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    text-transform: none;
    white-space: nowrap;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:not(.board-row):hover {{ background: #fafbfc; }}
  tr.board-row td {{
    background: #f7f8fa;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    padding: 5px 10px;
  }}
  .board-count {{
    display: inline-block;
    margin-left: 6px;
    padding: 0 7px;
    border-radius: 8px;
    background: #e8eaee;
    font-weight: 500;
  }}
  td.code {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.code a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  td.code a:hover {{ text-decoration: underline; }}
  td.name {{ font-weight: 500; }}
  td.board {{ color: var(--muted); white-space: nowrap; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.industry {{ color: var(--muted); }}
  .boards {{ margin-left: auto; color: var(--muted); font-size: 12px; }}
  .empty {{ color: var(--muted); font-size: 13px; margin: 4px 0 16px; }}
  .card.ranking {{ border-color: #efd2d2; }}
  .card.ranking h2::before {{ content: "▍"; color: var(--accent); margin-right: 4px; }}
  td.rank {{ color: var(--muted); font-variant-numeric: tabular-nums; width: 34px; }}
  td.score {{ font-weight: 700; color: var(--accent); cursor: help; }}
  td.marks {{ color: var(--muted); font-size: 12px; letter-spacing: 0.04em; }}
  footer {{ margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center; }}
  mark {{ background: #ffe9a8; padding: 0 1px; border-radius: 2px; }}
  @media (max-width: 560px) {{
    body {{ padding: 20px 12px 40px; }}
    .card {{ padding: 14px 14px 4px; }}
    th, td {{ padding: 7px 6px; font-size: 13px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Sequoia-X <span class="dot">·</span> 选股报告</h1>
    <div class="meta">
      <span>日期：{today}</span>
      <span>策略命中：{hit_strategies} / {len(results)}</span>
      <span>合计选出：{total} 只</span>
      {filter_line}
    </div>
  </header>

  <div class="toolbar">
    <input id="q" type="search" placeholder="搜索代码、名称、行业或板块…" autocomplete="off">
    <div id="hint"></div>
  </div>

  <main id="report">
{empty_hint}
{ranking_html}
{chr(10).join(sections)}
  </main>

  <footer>由 Sequoia-X V2 自动生成 · 数据仅供参考，不构成投资建议</footer>
</div>

<script>
(function () {{
  var input = document.getElementById('q');
  var hint = document.getElementById('hint');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));

  function rowsOf(card) {{
    return Array.prototype.slice.call(card.querySelectorAll('tbody tr'));
  }}

  function apply() {{
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {{
      var rows = rowsOf(card);
      var visible = 0;
      var header = null;
      var groupVisible = 0;
      var boardHit = false;

      // 分组标题只在「自身命中板块名」或「组内还有可见行」时显示
      function closeGroup() {{
        if (header) header.style.display = (boardHit || groupVisible) ? '' : 'none';
      }}

      rows.forEach(function (tr) {{
        if (tr.classList.contains('board-row')) {{
          closeGroup();
          header = tr;
          groupVisible = 0;
          boardHit = !!q && (tr.getAttribute('data-board') || '').toLowerCase().indexOf(q) !== -1;
          tr.style.display = '';
          return;
        }}
        var hit = !q || boardHit || tr.textContent.toLowerCase().indexOf(q) !== -1;
        tr.style.display = hit ? '' : 'none';
        if (hit) {{ visible++; groupVisible++; }}
      }});
      closeGroup();

      if (rows.length === 0) {{                    // 本就无结果的策略
        card.style.display = q ? 'none' : '';
      }} else {{
        card.style.display = visible ? '' : 'none';
      }}
      shown += visible;
    }});
    hint.textContent = q ? ('匹配 ' + shown + ' 条') : '';
  }}

  input.addEventListener('input', apply);
}})();
</script>
</body>
</html>
"""

    def _render_ranking(self, scores: Sequence[ScoreDetail], meta: dict[str, StockMeta]) -> str:
        """渲染页首的「量化评分排行」卡片（已按风险调整后得分降序）。

        回答的是策略答不了的问题：**入选的这几十只里，哪几只更值得看**。
        分数刻意做成可解释的 —— 鼠标悬停在分数上能看到五维分项与扣分，
        不让它变成一个黑箱数字。
        """
        if not scores:
            return ""

        head = (
            "<th>#</th><th>代码</th><th>名称</th><th>板块</th><th>评分</th><th>标记</th>"
            "<th>当日</th><th>20日</th><th>量比</th><th>距高</th><th>MA20偏离</th>"
            "<th>波动</th><th>胜率</th><th>窗口</th><th>置信</th><th>回撤</th>"
        )
        body: list[str] = []
        for i, detail in enumerate(scores, 1):
            item = meta.get(detail.symbol)
            name = html.escape(item.name) if item and item.name else "—"
            dist_high = (1 - detail.near_high) * 100 if detail.near_high else None

            # 悬停提示：把分数拆开，说明它为什么高 / 为什么被扣分
            tips = [f"{key} {value:.0f}" for key, value in detail.dims.items()]
            if detail.penalty:
                tips.append(f"惩罚 -{detail.penalty:.0f}")
            if detail.prob_up is not None:
                # 胜率必须带上分母才可解读：「100%」在 1/1 和 4/5 时含义完全不同。
                # 没有窗口数（旧数据）时不要写成「67%（67%）」这种重复。
                if detail.prob_samples:
                    tips.append(
                        f"形态胜率 {winrate_text(detail)}（{detail.prob_up:.0f}%）"
                    )
                else:
                    tips.append(f"形态胜率 {detail.prob_up:.0f}%")
            if detail.prob_candidates is not None:
                # 候选数（通过相似度门槛的窗口总数）≠ 胜率的分母（只取最像的 5 个），
                # 它进的是置信度的「样本量分」，所以单独标注、别和窗口数混淆。
                tips.append(f"候选窗口 {detail.prob_candidates}")
            if detail.prob_confidence is not None:
                tips.append(f"匹配可信度 {detail.prob_confidence:.0f}")
            if detail.max_drawdown is not None:
                tips.append(f"最大回撤 {detail.max_drawdown:.1f}%")

            body.append(
                "            <tr>"
                f'<td class="rank">{i}</td>'
                f'<td class="code"><a href="https://xueqiu.com/S/{to_xueqiu_code(detail.symbol)}"'
                f' target="_blank" rel="noopener">{detail.symbol}</a></td>'
                f'<td class="name">{name}</td>'
                f'<td class="board">{html.escape(board_of(detail.symbol))}</td>'
                f'<td class="num score" title="{html.escape("、".join(tips))}">'
                f"{detail.score:.1f}</td>"
                f'<td class="marks">{html.escape(detail.tags or "—")}</td>'
                f'<td class="num">{_fmt_pct(detail.chg1, signed=True)}</td>'
                f'<td class="num">{_fmt_pct(detail.chg20, signed=True)}</td>'
                f'<td class="num">{_fmt_num(detail.vol_ratio)}</td>'
                f'<td class="num">{_fmt_pct(dist_high)}</td>'
                f'<td class="num">{_fmt_pct(detail.dev_ma20)}</td>'
                f'<td class="num">{_fmt_pct(detail.vol20, digits=1)}</td>'
                f'<td class="num">{_fmt_pct(detail.prob_up, digits=1)}</td>'
                f'<td class="num">{_fmt_int(detail.prob_samples)}</td>'
                f'<td class="num">{_fmt_int(detail.prob_confidence)}</td>'
                f'<td class="num">{_fmt_pct(detail.max_drawdown, digits=1)}</td>'
                "</tr>"
            )

        return f"""<section class="card ranking" data-count="{len(scores)}">
      <div class="card-head">
        <h2>量化评分排行</h2>
        <span class="badge">{len(scores)}</span>
        <span class="boards">按风险调整后得分降序 · 悬停分数可看构成</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>{head}</tr></thead>
          <tbody>
{chr(10).join(body)}
          </tbody>
        </table>
      </div>
    </section>"""

    @staticmethod
    def _render_group_row(board: str, count: int, n_cols: int) -> str:
        """板块分组标题行，整行合并单元格。"""
        return (
            f'            <tr class="board-row" data-board="{html.escape(board)}">'
            f'<td colspan="{n_cols}">{html.escape(board)}'
            f'<span class="board-count">{count}</span></td></tr>'
        )

    @staticmethod
    def _render_row(
        symbol: str,
        meta: StockMeta | None,
        metric: StockMetric | None,
        holding: float | None,
        score: ScoreDetail | None,
        show_metrics: bool,
        show_holdings: bool = False,
        show_scores: bool = False,
    ) -> str:
        name = html.escape(meta.name) if meta and meta.name else "—"
        industry = html.escape(meta.industry) if meta and meta.industry else "—"
        board = board_of(symbol)
        xq = to_xueqiu_code(symbol)

        cells = (
            f'<td class="code"><a href="https://xueqiu.com/S/{xq}" '
            f'target="_blank" rel="noopener">{symbol}</a></td>'
            f'<td class="name">{name}</td>'
            f'<td class="board">{html.escape(board)}</td>'
            f'<td class="industry">{industry}</td>'
        )
        if show_metrics:
            cells += (
                f'<td class="num">{_fmt_cap(metric)}</td>'
                f'<td class="num">{_fmt_turn(metric)}</td>'
                f'<td class="num">{_fmt_pe(metric)}</td>'
            )
        if show_holdings:
            cells += f'<td class="num">{_fmt_holding(holding)}</td>'
        if show_scores:
            # 该股可能没算出评分（历史数据不足），此时显示「—」而不是凭空给 0
            text = f"{score.score:.1f}" if score else "—"
            tips = ""
            if score:
                parts = [f"{key} {value:.0f}" for key, value in score.dims.items()]
                if score.penalty:
                    parts.append(f"惩罚 -{score.penalty:.0f}")
                tips = f' title="{html.escape("、".join(parts))}"'
            cells += f'<td class="num score"{tips}>{text}</td>'

        return f'            <tr data-board="{html.escape(board)}">{cells}</tr>'
