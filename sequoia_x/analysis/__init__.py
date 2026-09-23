"""分析层：把策略的「入选 / 未入选」布尔结果加工成可排序的量化评分。"""

from sequoia_x.analysis.scorer import (
    LOOKBACK,
    MIN_ROWS,
    ScoreDetail,
    build_detail,
    read_price_series,
    score_pool,
)

__all__ = [
    "LOOKBACK",
    "MIN_ROWS",
    "ScoreDetail",
    "build_detail",
    "read_price_series",
    "score_pool",
]
