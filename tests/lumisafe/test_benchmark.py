"""Benchmark-first reporting math (SPEC §9)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from lumisafe.journal import Journal
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.reporting import benchmark as bm

IDX = pd.bdate_range("2020-01-01", periods=600)


def series(start, daily):
    return pd.Series(start * np.cumprod(np.full(len(IDX), 1 + daily)), index=IDX)


def test_compare_and_headline():
    eq = series(100, 0.0005)
    spy = series(300, 0.001)
    c = bm.compare(eq, spy, "SPY")
    assert c.user_return == pytest.approx(eq.iloc[-1] / eq.iloc[0] - 1)
    assert c.benchmark_return == pytest.approx(spy.iloc[-1] / spy.iloc[0] - 1)
    assert c.difference < 0
    assert c.headline().startswith("Your return: +") and "If you'd just held SPY: +" in c.headline()


def test_compare_handles_missing_data():
    assert bm.compare(pd.Series([1.0], index=IDX[:1]), series(1, 0), "SPY") is None
    assert bm.compare(series(1, 0), pd.Series(dtype=float), "SPY") is None
    late = series(300, 0.001)[10:]  # benchmark history starts after the equity does
    assert bm.compare(series(100, 0.001), late, "SPY") is not None


def test_cagr_drawdown_sharpe():
    s = pd.Series([100.0, 110.0], index=[pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01")])
    assert bm.cagr(s) == pytest.approx(0.0997, abs=1e-3)
    assert bm.max_drawdown(pd.Series([100, 120, 90, 130.0])) == pytest.approx(0.25)
    assert bm.sharpe(pd.Series([0.01, -0.01] * 50)) == pytest.approx(0.0, abs=1e-9)
    assert bm.total_return(pd.Series([2.0])) == 0.0


def test_blended_benchmark_monthly_rebalance():
    prices = {"A": series(10, 0.001), "B": series(50, 0.0)}
    blend = bm.blended_benchmark(prices, {"A": 0.6, "B": 0.4}, start_value=100)
    assert blend.iloc[0] == pytest.approx(100)
    hold = bm.blended_benchmark(prices, {"A": 0.6, "B": 0.4}, start_value=100, rebalance="none")
    assert hold.iloc[-1] > blend.iloc[-1]  # without rebalancing the winner grows its share


def test_tracking_gap_is_zero_for_identical_series():
    s = series(100, 0.0004)
    assert bm.tracking_gap_per_year(s, s) == pytest.approx(0.0)
    assert np.isnan(bm.tracking_gap_per_year(s[:1], s[:1]))


def test_equity_from_journal_filters_by_mode(tmp_path):
    j = Journal(tmp_path / "j.db")
    t = datetime(2026, 1, 5, tzinfo=timezone.utc)
    j.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "equity": 100.0}, ts=t)
    j.record_event(PAPER_CYCLE_EVENT, {"mode": "backtest", "equity": 5.0}, ts=t + timedelta(days=1))
    j.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "equity": 103.0}, ts=t + timedelta(days=2))
    eq = bm.equity_from_journal(j)
    assert list(eq.values) == [100.0, 103.0]


def test_no_win_rate_metric_anywhere_in_reporting():
    import inspect

    from lumisafe.reporting import benchmark, cli
    for mod in (benchmark, cli):
        src = inspect.getsource(mod).lower()
        assert "win rate" not in src.replace("no win-rate metric", "") and "win_rate" not in src
