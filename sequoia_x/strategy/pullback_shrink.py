"""深回调缩量策略：强势股回落 30-45% 且成交量收缩（迁移自 qtrade 的 pullback 家族）。"""

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class PullbackShrinkStrategy(BaseStrategy):
    """深回调缩量策略（逆势低吸的第二档，幅度介于「高位横盘」与「破位超跌」之间）。

    选股条件（全部满足）：
    1. 深度回调：距过去 60 日最高收盘价回落 30%~45%
    2. 趋势未破：最新收盘价仍站在 MA60 上方
    3. 缩量：近 5 日均量 / 近 20 日均量 < 0.6

    与 qtrade 原版的差异（口径来源 `tmp/qtrade_probe5~10.py`）：
    - 原版把 ``pullback_20d`` / ``pullback_deep`` / ``pullback_vol`` 拆成三个策略，但实测
      ``pullback_20d`` 的独有信号为 **0 条**（完全被 ``pullback_vol`` 的入口 C 包含），
      三者并集里 60% 的信号来自 ``pullback_deep`` 的「布林中轨回踩」入口。三份拷贝同时上线
      会让同一只票在评分器的「策略共振」维度被记 3 次，因此这里只保留一条腿，不照搬三条。
    - 只保留原版最严的那一档（30-45% + 缩量），因为它是唯一在 2024/2025 两年都为正超额的分支；
      15-40% 的温和回调档与「布林中轨回踩」档实测均为负超额，已丢弃。

    ⚠️ 有效性边界（本地库 2024-01 ~ 2026-10，线上门槛内，20 日前向）：
      样本 867 / 约 2.9 只每日，胜率仅 41.3%，均值 +1.99%（同域基线 +0.20%）。
      分年超额：2024 +4.65%、2025 +2.21%、2026 −0.60%。
      **中位数为负、均值全靠右尾**（P10 −17.8% / P90 +24.4%），属「低胜率高赔率」型，
      2026 震荡市已无优势。故它只做候选生成器，取舍交给评分层。

    Attributes:
        webhook_key: 路由到 'pullback' 专属飞书机器人（未配置时回退主 webhook）。
    """

    webhook_key: str = "pullback"

    _MIN_BARS: int = 60  # 至少需要 60 根 K 线（60 日高点 / MA60）
    _PEAK_WINDOW: int = 60
    _MIN_DROP: float = 0.30
    _MAX_DROP: float = 0.45
    _VOL_SHORT: int = 5
    _VOL_LONG: int = 20
    _VOL_SHRINK: float = 0.6

    def run(self) -> list[str]:
        """
        遍历候选股票池，返回「深度回调 + 缩量 + 趋势未破」的股票代码列表。

        Returns:
            满足条件的股票代码列表。
        """
        symbols = self.candidate_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                volume = df["volume"]

                peak = close.tail(self._PEAK_WINDOW).max()
                latest = close.iloc[-1]
                if peak <= 0:
                    continue

                drop = (peak - latest) / peak
                if not (self._MIN_DROP <= drop <= self._MAX_DROP):
                    continue

                # 趋势未破：深度回调时 MA5 必然跌破 MA20，只能要求中期均线
                ma60 = close.tail(self._PEAK_WINDOW).mean()
                if latest <= ma60:
                    continue

                vol_short = volume.iloc[-self._VOL_SHORT :].mean()
                vol_long = volume.iloc[-self._VOL_LONG :].mean()
                if vol_long <= 0 or vol_short / vol_long >= self._VOL_SHRINK:
                    continue

                selected.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] PullbackShrinkStrategy 计算失败：{exc}")
                continue

        selected = self.apply_universe_filter(selected)
        selected.sort()
        logger.info(f"PullbackShrinkStrategy 选出 {len(selected)} 只股票")
        return selected
