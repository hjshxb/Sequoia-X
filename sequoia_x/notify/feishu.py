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

from sequoia_x.analysis.scorer import ScoreDetail
from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data import stock_meta as stock_meta_module
from sequoia_x.data.stock_meta import StockMeta
from sequoia_x.notify.html_report import (
    group_by_board,
    strategy_label,
    to_xueqiu_code,
)

logger = get_logger(__name__)

# 精筛条件在卡片里的最大展示长度。`describe()` 会把行业黑名单全量列出，
# 实际配置动辄几十个关键词，不截断会把整张卡片撑爆。
# 阈值项（市值/PE/成交额/换手）排在描述前面，所以截断牺牲的是尾部行业名单。
_MAX_FILTER_CHARS = 120

# 卡片里「高分榜」只放前 N 名：飞书交互卡片对元素数量与总长度都有限制，
# 完整排行放在本地 HTML 报告里（那张表可横向滚动、可搜索）。
_TOP_RANKING = 5


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
    def _ranking_section(cls, scores: Sequence[ScoreDetail], meta: dict[str, StockMeta]) -> str:
        """渲染「高分榜」小节：按评分降序列出前 `_TOP_RANKING` 名。

        分数只在这里出现 —— 各策略小节仍只显示代码+名称，避免整张卡片
        都是数字而看不清结构。
        """
        lines = [f"**🎯 量化评分 Top {min(_TOP_RANKING, len(scores))}**"]
        for i, detail in enumerate(scores[:_TOP_RANKING], 1):
            item = meta.get(detail.symbol)
            name = (item.name if item else None) or ""
            label = f"{detail.symbol} {name}".strip()
            link = f"[{label}](https://xueqiu.com/S/{to_xueqiu_code(detail.symbol)})"
            mark = f" `{detail.tags}`" if detail.tags else ""
            lines.append(f"**{i}.** {link} · **{detail.score:.1f}**{mark}")
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
            scores: 可选的量化评分（应按得分降序）。传入后卡片顶部多一个
                「高分榜」小节，且各策略小节**内部**改按评分降序排列。

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
            add_markdown(self._ranking_section(score_list, meta))

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
