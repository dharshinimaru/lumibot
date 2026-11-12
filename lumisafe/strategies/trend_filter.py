"""Reference satellite: TrendFilter (SPEC §7.2).

A deliberately simple, well-known rule, included to exercise the pipeline, not
as a promised edge. Monthly: hold the top N by trailing return, but only those
trading above their 200-day moving average; otherwise hold cash.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from lumisafe.strategies.base import check_weights, visible

TRADING_DAYS_PER_MONTH = 21


@dataclass
class TrendFilter:
    lookback_months: int = 6
    top_n: int = 2
    ma_days: int = 200
    name: str = "trend_filter"
    universe: tuple[str, ...] = ("SPY", "QQQ", "IWM", "EFA", "TLT")
    rebalance_frequency: str = "monthly"

    # Walk-forward chooses among these on each train window (SPEC §8).
    PARAM_GRID = tuple({"lookback_months": lb, "top_n": n} for lb in (3, 6, 12) for n in (1, 2, 3))

    def __post_init__(self):
        self.universe = list(self.universe)

    @property
    def params(self) -> dict:
        return {"lookback_months": self.lookback_months, "top_n": self.top_n, "ma_days": self.ma_days}

    def target_weights(self, history: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, float]:
        lookback = self.lookback_months * TRADING_DAYS_PER_MONTH
        need = max(lookback, self.ma_days) + 1
        scores = {}
        for sym in self.universe:
            df = visible(history.get(sym), as_of)
            if df is None or "close" not in df:
                continue
            closes = df["close"].dropna()
            if len(closes) < need:
                continue
            last = float(closes.iloc[-1])
            momentum = last / float(closes.iloc[-1 - lookback]) - 1
            above_trend = last > float(closes.iloc[-self.ma_days:].mean())
            if above_trend:
                scores[sym] = momentum
        picks = sorted(scores, key=lambda s: (-scores[s], s))[: self.top_n]
        if not picks:
            return {}
        return check_weights({s: 1.0 / self.top_n for s in picks})
