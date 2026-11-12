"""SatelliteStrategy protocol (SPEC §5.4). Binding contract.

S1: ``target_weights`` must give identical output for full history and for
history truncated at ``as_of`` (no look-ahead).
S2: weights are >= 0 and sum to <= 1.0; the remainder is cash within the sleeve.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class SatelliteStrategy(Protocol):
    name: str
    universe: list[str]  # tickers it may trade (US-listed stocks/ETFs only)
    rebalance_frequency: str  # "weekly" | "monthly"; never intraday

    def target_weights(self, history: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, float]:
        """Return target weights WITHIN the satellite sleeve (sum <= 1.0, all >= 0).
        Must use only rows with index <= as_of (no look-ahead). Pure function."""


def visible(df: pd.DataFrame | None, as_of: pd.Timestamp) -> pd.DataFrame | None:
    """Rows with index <= as_of. Every strategy should slice through this."""
    if df is None or df.empty:
        return None
    idx = pd.DatetimeIndex(df.index)
    ts = pd.Timestamp(as_of)
    if idx.tz is not None and ts.tz is None:
        ts = ts.tz_localize(idx.tz)
    elif idx.tz is None and ts.tz is not None:
        ts = ts.tz_convert("America/New_York").tz_localize(None)
    return df[idx <= ts]


def check_weights(weights: dict[str, float]) -> dict[str, float]:
    """Enforce S2 defensively: drop negatives/NaNs and scale down if the sum exceeds 1."""
    clean = {k: float(v) for k, v in weights.items() if v is not None and v == v and v > 0}
    total = sum(clean.values())
    if total > 1.0:
        clean = {k: v / total for k, v in clean.items()}
    return clean
