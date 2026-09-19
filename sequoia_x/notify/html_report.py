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
from datetime import date
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import BOARD_ORDER, StockMeta, board_of, board_rank
from sequoia_x.data.universe_filter import StockMetric

logger = get_logger(__name__)

# 基础列；传入 metrics 时再追加指标列
_BASE_COLUMNS: tuple[str, ...] = ("代码", "名称", "板块", "行业")
_METRIC_COLUMNS: tuple[str, ...] = ("市值(亿)", "换手率", "PE(TTM)")

YI = 1e8

# 策略类名 -> 中文展示名。未收录的策略回退为类名本身。
_STRATEGY_LABELS: dict[str, str] = {
    "MaVolumeStrategy": "均线放量",
    "TurtleTradeStrategy": "海龟突破",
    "HighTightFlagStrategy": "高窄旗形",
    "LimitUpShakeoutStrategy": "涨停洗盘",
    "UptrendLimitDownStrategy": "上升跌停反包",
    "RpsBreakoutStrategy": "RPS 突破",
    "PrivatePlacementStrategy": "定增公告",
}


def to_xueqiu_code(symbol: str) -> str:
    """把纯数字代码转成雪球代码：6开头→SH，4/8开头→BJ，其余→SZ。"""
    if symbol.startswith("6"):
        return f"SH{symbol}"
    if symbol.startswith(("4", "8")):
        return f"BJ{symbol}"
    return f"SZ{symbol}"


def group_by_board(symbols: list[str]) -> list[tuple[str, list[str]]]:
    """按上市板块归类并排序。

    板块顺序为 主板 → 创业板 → 科创板 → 北交所 → 其他，板块内按代码升序。
    只返回非空的板块，因此调用方可以直接遍历生成分组标题。

    Args:
        symbols: 纯数字股票代码列表。

    Returns:
        [(板块名, 该板块下的代码列表)]，按板块顺序排列。
    """
    buckets: dict[str, list[str]] = {}
    for symbol in sorted(symbols, key=lambda s: (board_rank(s), s)):
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
    ) -> Path:
        """生成 HTML 报告并写入磁盘。

        Args:
            results: {策略类名: 选股代码列表}。
            filter_desc: 精筛条件的描述文本，展示在报告页首。
            output_path: 输出路径；为 None 时使用 default_path()。
            metrics: 可选的 {代码: StockMetric}。传入后额外展示
                市值 / 换手率 / PE(TTM) 三列；缺数据的股票显示为「—」。

        Returns:
            实际写入的报告文件路径。
        """
        meta = stock_meta_module.load_stock_meta()
        if metrics is None:
            metrics = {}
        missing = 0
        for symbols in results.values():
            for symbol in symbols:
                if symbol not in meta:
                    missing += 1
        if missing:
            logger.warning(f"HTML 报告：{missing} 条记录缺少名称/行业，将显示为「—」")

        content = self._render(results, meta, filter_desc, metrics)

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
    ) -> str:
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(v) for v in results.values())
        hit_strategies = sum(1 for v in results.values() if v)

        show_metrics = bool(metrics)
        headers = _BASE_COLUMNS + (_METRIC_COLUMNS if show_metrics else ())
        n_cols = len(headers)
        thead = "".join(f"<th>{html.escape(h)}</th>" for h in headers)

        sections: list[str] = []
        for strategy_name, symbols in results.items():
            label = _STRATEGY_LABELS.get(strategy_name, strategy_name)
            if symbols:
                grouped = group_by_board(symbols)
                parts: list[str] = []
                for board, group in grouped:
                    parts.append(self._render_group_row(board, len(group), n_cols))
                    for symbol in group:
                        parts.append(
                            self._render_row(
                                symbol,
                                meta.get(symbol),
                                metrics.get(symbol),
                                show_metrics,
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
        show_metrics: bool,
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

        return f'            <tr data-board="{html.escape(board)}">{cells}</tr>'
