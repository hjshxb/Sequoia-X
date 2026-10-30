"""板块当日涨幅榜：用本地行情 + 全市场概念映射，离线算出「今天最强的是什么方向」。

**为什么自己算，而不是取东财的板块行情**
    东财的板块行情在 `push2.eastmoney.com`，本项目禁用（限流较激进，短时间内
    十余次请求会把整个域名拒连）；`datacenter-web` 没有等价的「板块当日涨幅」接口。
    而概念成分已经通过 F10 全市场拿到了（`board_concept`），行情本来就在本地库
    `stock_daily` 里 —— 自己聚合等于**零额外请求**，口径也完全可控：
    谁算的、怎么算的，全写在这个文件里。

**口径（不写清的话「板块涨 3%」是没有意义的）**
    · **等权平均**成分股当日涨跌幅。刻意不用市值加权：这里回答的是「这个方向
      今天热不热」，市值加权会被一两只巨无霸绑架（银行板块永远跑不动）。
    · 只统计**当日有行情**的成分股，且要求成分数 `>= min_members`。
      东财概念里有大量 3~5 只的小题材，不设门槛的话榜首全是「某某小概念」。
    · 涨跌幅取 `stock_daily.close` 的日环比。本地库是**后复权**价，
      比值不受复权影响（绝对价格不能展示，比值可以）——
      所以这个字段口径正确，不是"凑合"。
    · 同时给出**上涨家数占比**：平均涨幅高但只有 2/10 上涨，说明是领涨股拉起来的，
      不是板块性行情。这是判断「能不能追」的关键旁证，比单看涨幅有用得多。

**为什么要把「领涨股」也带上**
    「人形机器人 +3.2%」有两种完全不同的成因：全板块普涨，或者一只票涨停带的。
    前者可以找第二梯队，后者往往次日就回吐。领涨股 + 上涨家数两列就是为了区分它们。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sequoia_x.core.logger import get_logger
from sequoia_x.data.board_concept import StockBoards

logger = get_logger(__name__)

# 榜上展示几个。用户 2026-10-30 指定「每日涨幅最大的 5 个板块以及概念」。
TOP_N = 5

# 成分股下限：低于这个数不参与排名。
# 东财概念里有大量 3~5 只的小题材，不设门槛时榜首会被它们占满 ——
# 「某某材料 +9%」只是 3 只票的平均，说明不了任何板块效应。
MIN_MEMBERS = 8

# 概念 / 申万一级行业两类。顺序即展示顺序。
KINDS: tuple[tuple[str, str], ...] = (("concept", "概念"), ("industry", "申万行业"))

# 单日涨跌幅的**可信上限**（%），按上市板放宽后的值 + 容差。超过就是数据错位。
# 为什么必须有这道闸：本地库存在「值级错位」的老缺陷（连续号段成块，单日
# -80% / +570% 这种），2026-10-30 实测把榜单直接带歪 —— 有色金属平均 +2.40%，
# 但上涨家数只有 12%，就是被 002996 那个 +572% 的错值拉起来的。
# 只在**聚合层**剔除（不删库、不影响其它功能），并记 WARNING 说明剔了几只。
_LIMIT_TOLERANCE = 3.0


def _price_limit(symbol: str) -> float:
    """按上市板返回单日涨跌幅上限（%）：创业板/科创板 20%，北交所 30%，其余 10%。

    ⚠️ 新股上市前 5 日没有涨跌停，可能被误判成「数据可疑」而剔除 ——
    这对「板块今天强不强」这个问题的损失可以忽略（它们本来也不该定义一个板块）。
    """
    if symbol.startswith(("300", "301", "688", "689")):
        return 20.0
    if symbol.startswith(("4", "8")):
        return 30.0
    return 10.0


def is_plausible_change(symbol: str, change: float) -> bool:
    """单日涨跌幅是否可能（超过所在板块涨跌停上限 + 容差即判为脏数据）。"""
    return abs(change) <= _price_limit(symbol) + _LIMIT_TOLERANCE


@dataclass(frozen=True)
class BoardRank:
    """一个板块在当日的表现。

    Attributes:
        kind: `concept` 或 `industry`。
        name: 板块名（概念名 / 申万一级行业名）。
        avg_chg: 成分股当日涨跌幅的**等权平均**（%，已四舍五入到 2 位）。
        up: 当日上涨的成分股只数。
        members: 当日有行情的成分股只数。
        leader: 涨幅最大的成分股代码。
        leader_chg: 该股当日涨跌幅（%）。
    """

    kind: str
    name: str
    avg_chg: float
    up: int
    members: int
    leader: str
    leader_chg: float

    @property
    def up_ratio(self) -> float:
        """上涨家数占比（%）；无成分股时返回 0。"""
        return round(self.up / self.members * 100, 1) if self.members else 0.0


def load_daily_changes(
    db_path: str, on_date: str | None = None
) -> tuple[str, dict[str, float]]:
    """从本地库算全市场**某日涨跌幅**（%），返回 `(日期, {代码: 涨跌幅})`。

    用该日与**前一交易日**的收盘价比值。`stock_daily.close` 是后复权价，
    比值不受复权方式影响；只出现在其中一天的票（停牌/次新）不参与。

    任何异常都返回 `("", {})`：这一节只是报告附注，取不到就整块略过，
    绝不能让它把当天的报告与推送拖没。

    Args:
        db_path: 本地库路径。
        on_date: 指定交易日；None 表示「库里最新的一天」。
            ⚠️ `regen_report.py` 重算历史日期时必须显式传 —— 否则会拿**今天的**
            行情去给一份旧报告排板块榜，看起来完全正常、其实全错。

    Returns:
        (交易日, {代码: 涨跌幅百分比})；拿不到时 `("", {})`。
    """
    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            if on_date:
                cur_date = on_date
                row = conn.execute(
                    "SELECT MAX(date) FROM stock_daily WHERE date < ?", (on_date,)
                ).fetchone()
                prev_date = str(row[0]) if row and row[0] else ""
            else:
                rows = conn.execute(
                    "SELECT date FROM stock_daily GROUP BY date ORDER BY date DESC LIMIT 2"
                ).fetchall()
                if len(rows) < 2:
                    return "", {}
                cur_date, prev_date = str(rows[0][0]), str(rows[1][0])
            if not cur_date or not prev_date:
                return "", {}
            pairs = conn.execute(
                "SELECT symbol, date, close FROM stock_daily WHERE date IN (?, ?)",
                (cur_date, prev_date),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 附注信息，失败即降级
        logger.warning(f"板块涨幅榜：本地行情读取失败，本次不出该节：{exc}")
        return "", {}

    # 一只票最多两行（今天 / 昨天），拼起来算比值
    closes: dict[str, dict[str, float]] = defaultdict(dict)
    for symbol, day, close in pairs:
        try:
            closes[str(symbol)][str(day)] = float(close)
        except (TypeError, ValueError):
            continue

    changes: dict[str, float] = {}
    for symbol, by_date in closes.items():
        cur, prev = by_date.get(cur_date), by_date.get(prev_date)
        if not cur or not prev or prev <= 0:
            continue
        changes[symbol] = round((cur / prev - 1) * 100, 2)
    return cur_date, changes


def _members_of(boards: Mapping[str, StockBoards], kind: str) -> dict[str, list[str]]:
    """把 `{代码: StockBoards}` 反转成 `{板块名: [代码...]}`。"""
    out: dict[str, list[str]] = defaultdict(list)
    for symbol, stock in boards.items():
        if kind == "industry":
            if stock.industry:
                out[stock.industry].append(symbol)
        else:
            for name in stock.concepts:
                out[name].append(symbol)
    return out


def rank_boards(
    boards: Mapping[str, StockBoards],
    changes: Mapping[str, float],
    *,
    kind: str,
    top: int = TOP_N,
    min_members: int = MIN_MEMBERS,
) -> tuple[BoardRank, ...]:
    """按当日等权平均涨幅给某一类板块排名，取前 `top` 个。

    Args:
        boards: 全市场 `{代码: StockBoards}`（`board_concept` 的返回）。
        changes: `{代码: 当日涨跌幅%}`（`load_daily_changes` 的返回）。
        kind: `concept` / `industry`。
        top: 取前几个。
        min_members: 成分股下限，低于此数不参与排名。

    Returns:
        按平均涨幅降序（同值按板块名升序，保证输出稳定）的榜单；
        数据不足时返回空元组。
    """
    if not boards or not changes:
        return ()

    ranked: list[BoardRank] = []
    dirty: set[str] = set()
    for name, symbols in _members_of(boards, kind).items():
        hits: list[str] = []
        for symbol in symbols:
            change = changes.get(symbol)
            if change is None:
                continue
            if not is_plausible_change(symbol, change):
                dirty.add(symbol)
                continue
            hits.append(symbol)
        if len(hits) < min_members:
            continue
        values = [changes[s] for s in hits]
        leader = max(hits, key=lambda s: changes[s])
        ranked.append(
            BoardRank(
                kind=kind,
                name=name,
                avg_chg=round(sum(values) / len(values), 2),
                up=sum(1 for v in values if v > 0),
                members=len(hits),
                leader=leader,
                leader_chg=changes[leader],
            )
        )

    ranked.sort(key=lambda r: (-r.avg_chg, r.name))
    if dirty:
        logger.warning(
            f"板块涨幅榜：{len(dirty)} 只当日涨跌幅超出涨跌停上限（数据错位），"
            f"已从 {kind} 统计中剔除：{'、'.join(sorted(dirty)[:6])}"
            + ("…" if len(dirty) > 6 else "")
        )
    return tuple(ranked[:top])


def rank_all(
    boards: Mapping[str, StockBoards],
    changes: Mapping[str, float],
    *,
    top: int = TOP_N,
    min_members: int = MIN_MEMBERS,
) -> dict[str, tuple[BoardRank, ...]]:
    """两类一起算，返回 `{kind: 榜单}`；某类算不出来时该键缺失或为空元组。"""
    return {
        kind: rank_boards(boards, changes, kind=kind, top=top, min_members=min_members)
        for kind, _label in KINDS
    }


def weakness_note(ranks: Sequence[BoardRank]) -> str:
    """榜首涨幅也为负时，返回一句提示；否则空串。

    为什么要写：弱势日「最强概念」可能是 −0.03% —— 榜单本身没错（它确实是当天
    相对最好的），但读者只看标题「最强」会以为市场在涨。展示层把这句话接在标题后，
    避免误读。报告与卡片共用同一句，免得两处措辞不一致。
    """
    if ranks and ranks[0].avg_chg <= 0:
        return "（当日整体偏弱，榜首涨幅也为负）"
    return ""
