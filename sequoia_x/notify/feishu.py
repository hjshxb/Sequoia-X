"""飞书通知模块：把各策略选股结果汇总成一张卡片推送到飞书群。

推送形态：**每个交易日一张汇总卡片** —— 各策略是卡片里的小节，
小节内部再按上市板块（主板 → 创业板 → 科创板 → 北交所 → 其他）分组。
每只股票只展示「代码 + 名称」，但整块可点击跳转雪球行情页（不放行业/市值等列）。

与本地 HTML 报告保持一致：
    - 策略中文名、板块分组、雪球代码前缀都复用 `html_report` 的实现
      （`strategy_label` / `group_by_board` / `to_xueqiu_code`），
      两处对同一策略、同一板块、同一交易所前缀的判定不会漂移；
    - 股票名称来自 `sequoia_x.data.stock_meta`（全市场一次请求 + 进程内缓存），
      不做逐股请求，也不会因为取不到名称而丢股票。
"""

import json
from collections.abc import Sequence
from datetime import date

import requests

from sequoia_x.analysis.scorer import ScoreDetail, composite_score, rank_by_composite
from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import StockMeta
from sequoia_x.notify.html_report import (
    group_by_board,
    strategy_label,
    to_xueqiu_code,
    winrate_text,
)

logger = get_logger(__name__)

# 精筛条件在卡片里的最大展示长度。`describe()` 会把行业黑名单全量列出，
# 实际配置动辄几十个关键词，不截断会把整张卡片撑爆。
# 阈值项（市值/PE/成交额/换手）排在描述前面，所以截断牺牲的是尾部行业名单。
_MAX_FILTER_CHARS = 120

# 卡片里的「综合评分榜」只放前 N 名：飞书交互卡片对元素数量与总长度都有限制，
# 完整排行放在本地 HTML 报告里（那张表可横向滚动、可搜索）。
#
# 为什么是**一个**榜而不是过去的「评分 Top 5 + 形态匹配 Top 5」两个榜：
# 两个榜的名单经常不一致、甚至方向相反（高分股胜率为 0、低分股胜率很高），
# 摆在一起只会让人问「到底信哪个」。改成一个榜、一个明确的排序口径后，
# 卡片上每一行都是同一个问题的答案：「按评分和胜率的综合排序，我最该先看谁」。
#
# 为什么给 10 而不是 5：两个榜各 5 是 10 个位置，合并后保持 10 个位置，
# 信息量不缩水；且综合分天然把「高分低胜率」和「高胜率低分」都摆在中间地带，
# 单看前 5 容易只剩高分那一类。
_TOP_COMPOSITE = 10


def _elide(text: str, limit: int = _MAX_FILTER_CHARS) -> str:
    """超长文本截断并加省略号；未超长时原样返回。"""
    return text if len(text) <= limit else text[:limit] + "…"


class FeishuNotifier:
    """飞书 Webhook 推送器：把多策略结果汇总成单张卡片。

    Attributes:
        settings: 提供 webhook 配置。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings

    # ── 卡片内容 ──

    @staticmethod
    def _stock_text(symbol: str, meta: dict[str, StockMeta]) -> str:
        """单只股票的展示文本：`[代码 名称](雪球链接)`。

        可见文本只有「代码 + 名称」，但整块可点，点开就是雪球行情页
        （与本地 HTML 报告一致）。名称缺失时退化为只有代码，股票不会丢。
        """
        item = meta.get(symbol)
        name = (item.name if item else None) or ""
        label = f"{symbol} {name}".strip()
        return f"[{label}](https://xueqiu.com/S/{to_xueqiu_code(symbol)})"

    @classmethod
    def _strategy_section(
        cls,
        label: str,
        symbols: list[str],
        meta: dict[str, StockMeta],
        order: dict[str, int] | None = None,
    ) -> str:
        """渲染一个策略小节：标题行 + 每个板块一行。

        板块标题带只数，例如 `主板 3：600000 浦发银行、600601 方正科技`。
        传入 `order`（代码 → 评分名次）时，板块**内部**改按评分降序 ——
        保留板块分组便于手机阅读，同时让高分股先出现。
        """
        lines = [f"**{label}**（{len(symbols)} 只）"]
        for board, group in group_by_board(symbols, order=order):
            stocks = "、".join(cls._stock_text(s, meta) for s in group)
            lines.append(f"{board} {len(group)}：{stocks}")
        return "\n".join(lines)

    @classmethod
    def _composite_section(cls, scores: Sequence[ScoreDetail], meta: dict[str, StockMeta]) -> str:
        """渲染「综合评分 Top 10」小节：按评分 + 胜率的综合分降序。

        综合分的口径完全交给 `scorer.composite_score()`（唯一真源），本方法
        只负责排版 —— 排序口径散落在展示层迟早会漂移（报告页首、飞书卡片、
        策略小节内的名次都要一致）。

        **为什么把两个榜并成一个**：过去卡片上是「🎯 量化评分 Top 5」与
        「📈 形态匹配 Top 5」两个榜。两个榜的名单经常不一致、甚至方向相反
        （高分股胜率为 0、低分股胜率很高），摆在一起只会让人问「到底信哪个」。
        合并成一个榜、一个明确口径后，每一行都是同一个问题的答案：
        「按评分与胜率的综合排序，我最该先看谁」。

        **为什么胜率要先按可信度收缩**：胜率 `prob_up = k / len(matches) * 100`，
        分母是相似度最高的那 5 个历史窗口（`top_k=5`），**不是**候选总数 ——
        分母越小越容易拿满分，一个「1/1 = 100%」不该把票顶上榜首。
        `composite_score` 把胜率按 `prob_confidence` 向 50% 收缩：可信度低的
        胜率自动趋近中性、几乎不影响排序，可信度高的才真正说话。

        整批都缺胜率（未启用增强 / 旧数据）时综合分退化为评分，本小节照常
        显示，只是每行少一段胜率文字 —— 不显示空值，也不拿 0 分去凑行数。

        每行给三个数：**综合分**（排序依据）、评分、胜率，让人看得出融合过程。
        """
        ranked = rank_by_composite(scores)[:_TOP_COMPOSITE]
        lines = [f"**🎯 综合评分 Top {len(ranked)}**（评分 ×0.7 + 胜率 ×0.3，胜率按可信度收缩）"]
        for i, detail in enumerate(ranked, 1):
            item = meta.get(detail.symbol)
            name = (item.name if item else None) or ""
            label = f"{detail.symbol} {name}".strip()
            link = f"[{label}](https://xueqiu.com/S/{to_xueqiu_code(detail.symbol)})"
            mark = f" `{detail.tags}`" if detail.tags else ""
            parts = [f"**{i}.** {link}", f"**综合 {composite_score(detail):.1f}**"]
            parts.append(f"评分 {detail.score:.1f}")
            if detail.prob_up is not None:
                parts.append(f"胜率 {winrate_text(detail)}")
            lines.append(" · ".join(parts) + mark)
        return "\n".join(lines)

    def _build_report_card(
        self,
        results: dict[str, list[str]],
        filter_desc: str = "",
        scores: Sequence[ScoreDetail] = (),
    ) -> dict:
        """把 {策略类名: 代码列表} 渲染成一张飞书交互卡片。

        Args:
            results: 各策略的选股结果，顺序即卡片中小节的顺序。
            filter_desc: 精筛条件描述，展示在卡片顶部便于解释结果为何偏少。
            scores: 可选的量化评分。传入后卡片顶部多一个「🎯 综合评分 Top 10」
                小节（按评分与胜率的综合分排序，见 `_composite_section`），
                且各策略小节**内部**改按综合分名次排列（综合分口径唯一，
                见 `scorer.composite_score`）。

        Returns:
            飞书 `msg_type=interactive` 的请求体。
        """
        meta = stock_meta_module.load_stock_meta()
        today = date.today().strftime("%Y-%m-%d")
        total = sum(len(v) for v in results.values())
        hit = sum(1 for v in results.values() if v)
        score_list = list(scores or [])
        order = {d.symbol: i for i, d in enumerate(score_list)}

        summary = [f"**日期：** {today}", f"**命中策略：** {hit} / {len(results)}"]
        summary.append(f"**选股数量：** {total}")
        if filter_desc:
            summary.append(f"**筛股条件：** {_elide(filter_desc)}")

        elements: list[dict] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(summary)}}
        ]

        def add_markdown(content: str) -> None:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})

        if score_list:
            add_markdown(self._composite_section(score_list, meta))

        for strategy_name, symbols in results.items():
            if symbols:
                add_markdown(
                    self._strategy_section(
                        strategy_label(strategy_name),
                        symbols,
                        meta,
                        order=order if score_list else None,
                    )
                )

        if total == 0:
            # 全空时不留白卡片：明确说明「跑了但没选出来」
            add_markdown("本次运行所有策略均无选股结果。")
        else:
            # 无结果的策略不单独成段，收成一行，避免卡片被空小节淹没
            silent = [strategy_label(n) for n, s in results.items() if not s]
            if silent:
                add_markdown(f"**本次无结果：** {'、'.join(silent)}")

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {today}",
                    },
                    "template": "blue",
                },
                "elements": elements,
            },
        }

    # ── 发送 ──

    def _post(self, url: str, payload: dict, webhook_key: str, count: int) -> None:
        """POST 卡片并判读飞书返回；失败只记日志，不抛异常。"""
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # 飞书真正的成功标志是响应体内部的 code == 0
            resp_json = resp.json()

            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(f"飞书推送成功 [{webhook_key}]，共 {count} 只股票")

        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")

    def send_report(
        self,
        results: dict[str, list[str]],
        filter_desc: str = "",
        webhook_key: str = "default",
        scores: Sequence[ScoreDetail] = (),
    ) -> None:
        """把所有策略的选股结果汇总成一张卡片推送出去。

        Args:
            results: {策略类名: 选股代码列表}，顺序决定卡片中小节的顺序。
            filter_desc: 精筛条件描述。
            webhook_key: 用于路由 Webhook；未配置专属地址时回退到默认地址。
            scores: 可选的量化评分（应按得分降序），用于生成高分榜与节内排序。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_report_card(results, filter_desc, scores)
        self._post(url, payload, webhook_key, sum(len(v) for v in results.values()))
