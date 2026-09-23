"""构造 `ScoreDetail` 测试数据的工厂。

`ScoreDetail` 字段多且基本必填，如果在每个测试文件里手写一遍，
「新增一个字段」就会变成全局扫雷。集中在这里，默认给一组中性量价特征，
用例只覆盖自己关心的那几项。
"""

from __future__ import annotations

from sequoia_x.analysis.scorer import ScoreDetail

# 五维全满 100 是不可能的组合（估值与筹码不同时取满分场景），这里只是取值方便断言
DEFAULT_DIMS = {
    "策略共振": 20.0,
    "量能配合": 15.0,
    "趋势强度": 20.0,
    "估值": 13.0,
    "筹码": 10.0,
}


def make_score(
    symbol: str,
    adjusted: float = 70.0,
    *,
    tags: str = "",
    penalty: float = 0.0,
    prob_up: float | None = None,
    max_drawdown: float | None = None,
    **overrides,
) -> ScoreDetail:
    """构造一条评分明细，`adjusted` 即对外展示的分数。

    Args:
        symbol: 股票代码。
        adjusted: 风险调整后得分（= total - penalty）。
        tags: 命中的策略字母标记。
        penalty: 位置惩罚分（>0 时报告的悬停提示里应出现「惩罚」）。
        prob_up: 形态匹配胜率（%，0~100 口径）。
        max_drawdown: 最大回撤（%）。
        **overrides: 覆盖任意默认量价特征，如 `chg1=2.35`。
    """
    fields = {
        "symbol": symbol,
        "last_date": "2026-09-23",
        "tags": tags,
        "chg1": -0.5,
        "chg20": 3.2,
        "chg60": 11.0,
        "vol_ratio": 0.9,
        "near_high": 0.99,
        "dev_ma20": 4.5,
        "vol20": 38.0,
        "streak": 0,
        "bull": 2,
        "amount": 12.3,
        "new_high60": False,
        "new_high120": False,
        "dims": dict(DEFAULT_DIMS),
        "total": adjusted + penalty,
        "penalty": penalty,
        "adjusted": adjusted,
        "prob_up": prob_up,
        "expected_pct": None,
        "max_drawdown": max_drawdown,
    }
    fields.update(overrides)
    return ScoreDetail(**fields)
