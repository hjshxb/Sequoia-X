"""板块涨幅榜（`analysis/board_rank.py`）的测试。

口径是这个模块的全部价值，所以每条断言都对着「口径写错了会怎样」：
  · 等权平均（不是市值加权、不是中位数）
  · 成分数门槛（3 只票的小概念不许霸榜）
  · 领涨股 + 上涨家数（区分「普涨」和「一只票拉的」）
  · 指定日期（重算历史日期时不许拿今天的行情）
"""

import sqlite3

import pytest

from sequoia_x.analysis import board_rank
from sequoia_x.data.board_concept import StockBoards


def _boards(concepts_of: dict[str, tuple[str, ...]], industries: dict[str, str]):
    return {
        code: StockBoards(symbol=code, concepts=concepts_of.get(code, ()),
                          industry=industries.get(code))
        for code in set(concepts_of) | set(industries)
    }


# ── 收益计算 ──


def _make_db(path: str, rows: list[tuple[str, str, float]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE stock_daily (symbol TEXT, date TEXT, close REAL, "
            "open REAL, high REAL, low REAL, volume REAL, turnover REAL, "
            "PRIMARY KEY (symbol, date))"
        )
        conn.executemany(
            "INSERT INTO stock_daily (symbol, date, close) VALUES (?,?,?)", rows
        )
        conn.commit()


def test_load_daily_changes_computes_pct(tmp_path) -> None:
    db = str(tmp_path / "t.db")
    _make_db(
        db,
        [
            ("600000", "2026-10-27", 10.0),
            ("600000", "2026-10-28", 11.0),  # +10%
            ("000001", "2026-10-27", 20.0),
            ("000001", "2026-10-28", 19.0),  # -5%
        ],
    )

    day, changes = board_rank.load_daily_changes(db)

    assert day == "2026-10-28"
    assert changes == {"600000": 10.0, "000001": -5.0}


def test_load_daily_changes_skips_symbols_missing_one_day(tmp_path) -> None:
    """停牌/次新只有一天的数据 ⇒ 算不出涨跌幅，直接不参与（不能当 0%）。"""
    db = str(tmp_path / "t.db")
    _make_db(
        db,
        [
            ("600000", "2026-10-27", 10.0),
            ("600000", "2026-10-28", 11.0),
            ("000002", "2026-10-28", 30.0),  # 只有今天
        ],
    )

    _day, changes = board_rank.load_daily_changes(db)

    assert set(changes) == {"600000"}


def test_load_daily_changes_honours_given_date(tmp_path) -> None:
    """重算历史日期时必须用那一天 + 它的前一交易日，不能拿最新两天。"""
    db = str(tmp_path / "t.db")
    _make_db(
        db,
        [
            ("600000", "2026-10-26", 10.0),
            ("600000", "2026-10-27", 12.0),  # +20%
            ("600000", "2026-10-28", 12.0),  # 0%
        ],
    )

    day, changes = board_rank.load_daily_changes(db, "2026-10-27")

    assert day == "2026-10-27"
    assert changes["600000"] == pytest.approx(20.0)


def test_load_daily_changes_degrades_on_broken_db(tmp_path) -> None:
    """库坏了只降级成空，不抛异常 —— 这一节不该拖垮当天报告。"""
    assert board_rank.load_daily_changes(str(tmp_path / "nope.db")) == ("", {})


# ── 排名 ──


def test_rank_uses_equal_weight_average_not_sum() -> None:
    """等权平均：10 只各 +2% ⇒ 板块 +2%，不是 +20%。

    ⚠️ 这条必须用**同向**数据。用「5 涨 5 跌」那种对称样本时，
    求和与平均恰好都等于 0，把实现改成求和也测不出来（变异检验抓到过一次）。
    """
    codes = [f"60000{i}" for i in range(10)]
    boards = _boards({c: ("普涨概念",) for c in codes}, {})
    changes = {c: 2.0 for c in codes}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    assert got[0].avg_chg == pytest.approx(2.0)
    assert got[0].up == 10 and got[0].up_ratio == 100.0


def test_rank_breadth_on_symmetric_move() -> None:
    """5 只 +10%、5 只 −10% ⇒ 平均 0%、上涨占比 50%：板块没动，但分歧很大。"""
    codes = [f"60000{i}" for i in range(10)]
    boards = _boards({c: ("对称概念",) for c in codes}, {})
    changes = {c: (10.0 if i < 5 else -10.0) for i, c in enumerate(codes)}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    assert got[0].avg_chg == 0.0
    assert got[0].up == 5 and got[0].members == 10
    assert got[0].up_ratio == 50.0


def test_rank_excludes_small_concepts() -> None:
    """3 只票的小概念涨 9% 也不许上榜 —— 它不构成板块效应。"""
    boards = _boards(
        {f"00000{i}": ("小概念",) for i in range(3)}
        | {f"00010{i}": ("大概念",) for i in range(8)},
        {},
    )
    changes = {**{f"00000{i}": 9.0 for i in range(3)}, **{f"00010{i}": 1.0 for i in range(8)}}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    assert [r.name for r in got] == ["大概念"]


def test_rank_orders_by_avg_and_reports_leader() -> None:
    boards = _boards(
        {f"60000{i}": ("A概念",) for i in range(8)}
        | {f"60010{i}": ("B概念",) for i in range(8)},
        {},
    )
    a = {f"60000{i}": 1.0 for i in range(8)}
    a["600003"] = 9.5  # 领涨股（主板涨停附近），把 A 的平均拉到 (7×1+9.5)/8 = 2.06
    b = {f"60010{i}": 1.5 for i in range(8)}  # B 均匀 +1.5
    changes = {**a, **b}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    assert [r.name for r in got] == ["A概念", "B概念"], "按平均涨幅降序"
    assert got[0].avg_chg == pytest.approx(2.06)
    assert got[0].leader == "600003"
    assert got[0].leader_chg == 9.5
    assert got[1].avg_chg == pytest.approx(1.5)


def test_up_ratio_separates_broad_rally_from_one_stock() -> None:
    """平均涨幅是正的但只有 2/10 上涨 ⇒ 是少数票拉的，不是板块性行情。

    这正是「能不能追」的关键旁证：同样 +1.5%，普涨和独苗是两码事。
    """
    codes = [f"60000{i}" for i in range(10)]
    boards = _boards({c: ("独苗概念",) for c in codes}, {})
    # 两只涨 9.8%、八只跌 0.5% ⇒ 平均 +1.56%，与「10 只各 +1.5%」几乎一样
    changes = {c: (-0.5 if i >= 2 else 9.8) for i, c in enumerate(codes)}

    got = board_rank.rank_boards(boards, changes, kind="concept")[0]

    assert got.avg_chg == pytest.approx(1.56)
    assert got.up == 2
    assert got.up_ratio == 20.0


def test_rank_is_stable_on_ties() -> None:
    """同涨幅按名称升序，保证两次跑出来的榜单顺序一致（否则 diff 里全是噪声）。

    ⚠️ 构造时必须让**插入顺序与名称顺序相反**（先放「甲」再放「乙」），
    否则 Python 的稳定排序会碰巧给出同样的结果，把排序键改成「只按涨幅」
    也测不出来（变异检验抓到过一次）。
    """
    boards: dict[str, StockBoards] = {}
    for i in range(8):
        boards[f"60000{i}"] = StockBoards(symbol=f"60000{i}", concepts=("甲概念",))
    for i in range(8):
        boards[f"60010{i}"] = StockBoards(symbol=f"60010{i}", concepts=("乙概念",))
    changes = {code: 2.0 for code in boards}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    # 「乙」(U+4E59) 的码位小于「甲」(U+7532)：同涨幅时乙排前面
    assert [r.name for r in got] == ["乙概念", "甲概念"]
    assert got == board_rank.rank_boards(boards, changes, kind="concept")


def test_rank_all_covers_concept_and_industry() -> None:
    boards = _boards(
        {f"00000{i}": ("某概念",) for i in range(8)},
        {f"00000{i}": "医药生物" for i in range(8)},
    )
    changes = {f"00000{i}": 1.5 for i in range(8)}

    got = board_rank.rank_all(boards, changes)

    assert set(got) == {"concept", "industry"}
    assert got["concept"][0].name == "某概念"
    assert got["industry"][0].name == "医药生物"


def test_rank_returns_empty_without_data() -> None:
    """没有映射 / 没有行情时返回空元组，调用方不用判空。"""
    assert board_rank.rank_boards({}, {"600000": 1.0}, kind="concept") == ()
    assert board_rank.rank_boards(_boards({"600000": ("X",)}, {}), {}, kind="concept") == ()


def test_rank_caps_at_top_n() -> None:
    """默认只取前 5 个（用户 2026-10-30 指定的口径）。"""
    concepts = {f"概念{i}": (f"{i}00000",) for i in range(9)}
    flat: dict[str, tuple[str, ...]] = {}
    for i in range(9):
        for j in range(8):
            flat[f"{i}0000{j}"] = (f"概念{i}",)
    boards = _boards(flat, {})
    changes = {code: float(i) for i in range(9) for code in flat if code.startswith(f"{i}0000")}

    got = board_rank.rank_boards(boards, changes, kind="concept")

    assert len(got) == board_rank.TOP_N == 5
    assert got[0].name == "概念8"
    assert concepts  # 保留构造可读性


# ── 脏数据闸门（2026-10-30 实测踩到）──


def test_implausible_change_is_excluded_from_stats() -> None:
    """单日 +572%（主板，上限 10%）是数据错位，不许参与平均、更不许当领涨股。

    事故：本地库的值级错位让「有色金属」平均 +2.40%，但上涨家数只有 12% ——
    涨幅完全是被 002996 那个 +572% 的错值撑起来的，榜单等于在骗人。
    """
    codes = [f"60000{i}" for i in range(10)]
    boards = _boards({c: ("有色金属",) for c in codes}, {})
    changes = {c: 1.0 for c in codes}
    changes["600003"] = 572.1  # 错位值

    got = board_rank.rank_boards(boards, changes, kind="concept")[0]

    assert got.members == 9, "错位票不计入成分数"
    assert got.avg_chg == pytest.approx(1.0), "也不许进平均"
    assert got.leader_chg <= 10.0, "领涨股不能是脏数据"


def test_plausible_limit_depends_on_board() -> None:
    """涨跌停上限按上市板：主板 10%、创业板/科创 20%、北交所 30%（各留 3pp 容差）。"""
    assert board_rank.is_plausible_change("600000", 11.5) is True
    assert board_rank.is_plausible_change("600000", 27.0) is False  # 主板不可能 +27%
    assert board_rank.is_plausible_change("300750", 19.5) is True
    assert board_rank.is_plausible_change("300750", 25.0) is False
    assert board_rank.is_plausible_change("830799", 29.0) is True
    assert board_rank.is_plausible_change("830799", 35.0) is False


def test_weakness_note_only_when_top_is_negative() -> None:
    """弱势日「最强概念」也可能是 −0.03%：标题里要说明，否则会被读成「市场在涨」。"""
    from sequoia_x.analysis.board_rank import BoardRank

    up = (BoardRank("concept", "A", 0.42, 5, 10, "600000", 3.0),)
    down = (BoardRank("concept", "A", -0.03, 3, 10, "600000", 3.0),)

    assert board_rank.weakness_note(up) == ""
    assert "偏弱" in board_rank.weakness_note(down)
    assert board_rank.weakness_note(()) == ""
