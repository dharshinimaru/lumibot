"""User vs buy-and-hold benchmark (SPEC §9).

Every result is shown next to "if you'd just held the benchmark". This module
has no win-rate metric anywhere, by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from lumisafe.journal.journal import PAPER_CYCLE_EVENT, Journal


@dataclass(frozen=True)
class Comparison:
    start: pd.Timestamp
    end: pd.Timestamp
    user_return: float
    benchmark_return: float
    benchmark: str

    @property
    def difference(self) -> float:
        return self.user_return - self.benchmark_return

    def headline(self) -> str:
        return (f"Your return: {self.user_return:+.2%} · If you'd just held {self.benchmark}: "
                f"{self.benchmark_return:+.2%}")


def _naive_index(s: pd.Series) -> pd.Series:
    s = s.dropna().sort_index()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    return pd.Series(s.values, index=idx)


def total_return(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) < 2 or s.iloc[0] <= 0:
        return 0.0
    return float(s.iloc[-1] / s.iloc[0] - 1)


def cagr(series: pd.Series) -> float:
    s = _naive_index(series)
    if len(s) < 2 or s.iloc[0] <= 0:
        return 0.0
    years = (s.index[-1] - s.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1)


def max_drawdown(series: pd.Series) -> float:
    s = series.dropna()
    if s.empty:
        return 0.0
    return float((1 - s / s.cummax()).max())


def sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    r = returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(np.sqrt(periods_per_year) * r.mean() / r.std())


def compare(equity: pd.Series, benchmark_prices: pd.Series, benchmark: str) -> Comparison | None:
    """Return over the equity series' window vs buy-and-hold of the benchmark in the same window."""
    eq = _naive_index(equity)
    bp = _naive_index(benchmark_prices)
    if len(eq) < 2 or bp.empty:
        return None
    start, end = eq.index[0].normalize(), eq.index[-1].normalize()
    b_start = bp.asof(start)
    b_end = bp.asof(end)
    if pd.isna(b_start) or pd.isna(b_end):
        # benchmark history doesn't reach back to the start; use the first available price on/after it
        after = bp[bp.index >= start]
        if after.empty or pd.isna(b_end):
            return None
        b_start = after.iloc[0]
    return Comparison(start, end, total_return(eq), float(b_end / b_start - 1), benchmark)


def blended_benchmark(prices: dict[str, pd.Series], weights: dict[str, float], start_value: float = 1.0,
                      rebalance: str = "monthly") -> pd.Series:
    """Value of a fixed-weight blend of buy-and-hold ETFs, rebalanced monthly (or never)."""
    df = pd.DataFrame({s: _naive_index(prices[s]) for s in weights}).dropna()
    if df.empty:
        return pd.Series(dtype=float)
    w = pd.Series(weights, dtype=float)
    value = start_value
    shares = (value * w) / df.iloc[0]
    out = []
    prev_month = df.index[0].month
    for ts, row in df.iterrows():
        if rebalance == "monthly" and ts.month != prev_month:
            value = float((shares * row).sum())
            shares = (value * w) / row
            prev_month = ts.month
        out.append(float((shares * row).sum()))
    return pd.Series(out, index=df.index)


def equity_from_journal(journal: Journal, mode: str = "paper", since: datetime | None = None) -> pd.Series:
    """Account equity per cycle, from the journal's heartbeat events."""
    points = {}
    for ev in journal.events(PAPER_CYCLE_EVENT, since=since):
        if ev.payload.get("mode") == mode and ev.payload.get("equity") is not None:
            points[pd.Timestamp(ev.ts)] = float(ev.payload["equity"])
    if not points:
        return pd.Series(dtype=float)
    return _naive_index(pd.Series(points))


def tracking_gap_per_year(strategy_equity: pd.Series, benchmark_equity: pd.Series) -> float:
    """Annualized return difference (strategy CAGR − benchmark CAGR) over their common window."""
    a = _naive_index(strategy_equity)
    b = _naive_index(benchmark_equity)
    a = a.groupby(a.index.normalize()).last()
    b = b.groupby(b.index.normalize()).last()
    common = a.index.intersection(b.index)
    if len(common) < 2:
        return float("nan")
    return cagr(a.loc[common]) - cagr(b.loc[common])
