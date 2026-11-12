"""Deterministic synthetic price histories for strategy tests (never used as market data)."""

import numpy as np
import pandas as pd

SYMS = ("SPY", "QQQ", "IWM", "EFA", "TLT")


def random_walks(start="2003-01-01", end="2025-12-31", seed=7, drift=None, vol=0.012):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    drift = drift or {s: d for s, d in zip(SYMS, (0.0004, 0.0005, 0.0003, 0.0002, 0.0001))}
    out = {}
    for s in SYMS:
        r = rng.normal(drift[s], vol, len(idx))
        out[s] = pd.DataFrame({"close": 100 * np.exp(np.cumsum(r))}, index=idx)
    return out
