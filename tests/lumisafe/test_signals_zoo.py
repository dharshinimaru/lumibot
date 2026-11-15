"""Strategy zoo engine: next-open execution, costs, no look-ahead, deflated Sharpe (synthetic data only)."""

import numpy as np
import pandas as pd
import pytest

from lumisafe.signals import zoo


def bars_for(prices: dict[str, list[float]], opens: dict[str, list[float]] | None = None, start="2016-01-04"):
    idx = pd.bdate_range(start, periods=len(next(iter(prices.values()))))
    out = {}
    for s, c in prices.items():
        o = (opens or {}).get(s, c)
        out[s] = pd.DataFrame({"open": o, "high": c, "low": c, "close": c, "volume": 1e6}, index=idx)
    return out


def test_trade_happens_at_the_next_open_not_the_signal_close():
    # day 0 signal; day 1 opens at 110 (gap) and closes at 121; weight set on day 0 earns only from day 1's open
    b = bars_for({"SPY": [100, 121, 121], "BIL": [1, 1, 1]}, {"SPY": [100, 110, 121]})
    data = zoo.build(b)
    W = pd.DataFrame(0.0, index=data.days, columns=data.C.columns)
    W.loc[data.days[0]:, "SPY"] = 1.0
    r = zoo.simulate(W, data, lambda s, y: 0.0)
    assert r.iloc[1] == pytest.approx(121 / 110 - 1 - zoo.ETF_COST - zoo.SLIPPAGE)  # the gap is not captured
    assert r.iloc[2] == pytest.approx(0.0)


def test_a_signal_that_peeks_at_tomorrow_earns_nothing_extra():
    rng = np.random.default_rng(0)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, 400))
    b = bars_for({"SPY": list(c), "BIL": [1.0] * 400})
    data = zoo.build(b)
    # weights decided on day d from the return of day d (known at the close): fine. Engine must apply them to d+1.
    up_today = data.C["SPY"].pct_change() > 0
    W = zoo._daily(up_today, {"SPY": 1.0}, {}, data)
    r = zoo.simulate(W, data, lambda s, y: 0.0)
    cheat = (data.C["SPY"].pct_change().where(up_today, 0.0)).sum()  # what look-ahead would earn
    assert r.sum() < 0.5 * cheat


def test_month_end_rebalance_and_universe_are_point_in_time():
    n = 60
    b = bars_for({"AAA": list(np.linspace(10, 20, n)), "BBB": list(np.linspace(20, 10, n)), "SPY": [100.0] * n,
                  "BIL": [1.0] * n})
    data = zoo.build(b, top_n=1)
    assert not data.U.iloc[:20].any().any()  # needs 20 prior days of dollar volume
    me = zoo.month_ends(data.days)
    assert all(d.month != (d + pd.offsets.BDay(1)).month for d in me[:-1])


def test_deflated_sharpe_punishes_many_tries():
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(0.0004, 0.01, 1750))
    few = zoo.deflated_sharpe(x, [0.0, 0.01])
    many = zoo.deflated_sharpe(x, list(rng.normal(0, 0.02, 35)))
    assert many < few
    assert zoo.deflated_sharpe(pd.Series(rng.normal(0, 0.01, 1750)), list(rng.normal(0, 0.02, 35))) < 0.95


def test_the_list_is_fixed():
    assert len(zoo.STRATEGIES) == 34
    assert {b for _f, b, _d in zoo.STRATEGIES.values()} == {"pool", "SPY"}


def test_every_strategy_runs_end_to_end_on_synthetic_data():
    rng = np.random.default_rng(2)
    n = 2900  # 2016 → 2027 business days, covers all three periods
    syms = [f"S{i}" for i in range(30)] + zoo.ETFS
    prices = {s: list(50 * np.cumprod(1 + rng.normal(0.0003, 0.015, n))) for s in syms}
    out = zoo.run(bars_for(prices), lambda s, y: 0.001, progress=lambda m: None)
    assert out["trials"] == 34 and len(out["results"]) == 34
    assert all(0.0 <= r.dsr <= 1.0 for r in out["results"])
    assert "Passed all three tests" in zoo.format_zoo(out)
