"""SatelliteStrategy invariants S1 (no look-ahead) and S2 (weight bounds) for TrendFilter."""

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lumisafe.strategies import CATALOG, SatelliteStrategy, TrendFilter
from lumisafe.strategies.base import check_weights, visible

from .synthetic import random_walks

HIST = random_walks()
DATES = HIST["SPY"].index


def truncated(history, as_of):
    return {s: df[df.index <= as_of] for s, df in history.items()}


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=300, max_value=len(DATES) - 2), st.sampled_from(TrendFilter.PARAM_GRID))
def test_s1_no_look_ahead_full_vs_truncated_history(i, params):
    as_of = DATES[i]
    strat = TrendFilter(**params)
    assert strat.target_weights(HIST, as_of) == strat.target_weights(truncated(HIST, as_of), as_of)


def test_s1_future_shock_cannot_change_todays_weights():
    as_of = DATES[2000]
    shocked = {s: df.copy() for s, df in HIST.items()}
    for df in shocked.values():
        df.loc[df.index > as_of, "close"] *= 10  # wildly different future
    strat = TrendFilter()
    assert strat.target_weights(HIST, as_of) == strat.target_weights(shocked, as_of)


@settings(max_examples=80, deadline=None)
@given(st.integers(min_value=0, max_value=len(DATES) - 1), st.sampled_from(TrendFilter.PARAM_GRID),
       st.integers(min_value=0, max_value=50))
def test_s2_weights_nonnegative_and_sum_at_most_one(i, params, seed):
    hist = random_walks(seed=seed) if seed else HIST
    w = TrendFilter(**params).target_weights(hist, DATES[i])
    assert all(v >= 0 for v in w.values())
    assert sum(w.values()) <= 1.0 + 1e-12
    assert set(w) <= set(TrendFilter().universe)


def test_trend_filter_rules():
    idx = pd.bdate_range("2020-01-01", periods=400)
    up = pd.DataFrame({"close": [100 + i for i in range(400)]}, index=idx)
    down = pd.DataFrame({"close": [500 - i for i in range(400)]}, index=idx)
    flat_up = pd.DataFrame({"close": [100 + i * 0.1 for i in range(400)]}, index=idx)
    hist = {"SPY": up, "QQQ": flat_up, "IWM": down, "EFA": down, "TLT": down}
    w = TrendFilter().target_weights(hist, idx[-1])
    assert w == {"SPY": 0.5, "QQQ": 0.5}  # top 2 by momentum, both above their 200-day average
    only_one = {**hist, "QQQ": down}
    assert TrendFilter().target_weights(only_one, idx[-1]) == {"SPY": 0.5}  # rest stays in cash
    assert TrendFilter().target_weights({s: down for s in hist}, idx[-1]) == {}
    assert TrendFilter().target_weights({}, idx[-1]) == {}
    assert TrendFilter().target_weights(hist, idx[100]) == {}  # not enough history yet


def test_protocol_and_catalog():
    assert isinstance(TrendFilter(), SatelliteStrategy)
    assert CATALOG["trend_filter"] is TrendFilter
    t = TrendFilter()
    assert t.universe == ["SPY", "QQQ", "IWM", "EFA", "TLT"] and t.rebalance_frequency == "monthly"
    assert len(TrendFilter.PARAM_GRID) == 9


def test_helpers():
    assert check_weights({"A": 0.7, "B": 0.6, "C": -1, "D": float("nan")}) == pytest.approx(
        {"A": 0.7 / 1.3, "B": 0.6 / 1.3})
    assert visible(None, DATES[0]) is None
    tz = HIST["SPY"].tz_localize("America/New_York")
    assert len(visible(tz, DATES[10])) == 11
    assert len(visible(HIST["SPY"], pd.Timestamp(DATES[10]).tz_localize("UTC"))) in (10, 11)
