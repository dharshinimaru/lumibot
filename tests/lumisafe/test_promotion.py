"""Promotion pipeline (SPEC §8): walk-forward, every state transition, P1, auto-retirement."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from lumisafe.config.schema import SatelliteConfig
from lumisafe.journal import Journal
from lumisafe.portfolio.satellite import EQUITY_EVENT, SatelliteManager
from lumisafe.promotion import backtest_and_promote, criteria
from lumisafe.promotion.registry import ALLOWED, PROMOTION_EVENT, Registry, State, TransitionError
from lumisafe.promotion.walk_forward import (
    COST, buy_and_hold, folds_for, rebalance_dates, simulate, walk_forward,
)
from lumisafe.risk.kill_switch import KillSwitch
from lumisafe.config.schema import RiskConfig
from lumisafe.runner import StateStore
from lumisafe.strategies import TrendFilter
from lumisafe.types import Source, Verdict

from .helpers import T0, snapshot
from .synthetic import random_walks

HIST = random_walks()
NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --- walk-forward ---------------------------------------------------------------------------------

def test_walk_forward_has_at_least_4_folds_over_8_years_and_costs():
    r = base_report()
    assert len(r.folds) >= 4 and r.years >= 8
    assert r.cost_per_trade == COST == 0.001
    for f in r.folds:
        assert f.train_end < f.test_start <= f.test_end
        assert f.chosen_params in TrendFilter.PARAM_GRID
    assert 0 <= r.folds_matching_benchmark <= 1


def test_walk_forward_params_are_chosen_on_train_window_only():
    base = base_report()
    # scramble every test window's prices: choices must not move
    f0 = base.folds[0]
    scrambled = {s: df.copy() for s, df in HIST.items()}
    for s, df in scrambled.items():
        mask = (df.index >= f0.test_start) & (df.index <= f0.test_end)
        df.loc[mask, "close"] = df.loc[mask, "close"].values[::-1]
    again = walk_forward(lambda **p: TrendFilter(**p), list(TrendFilter.PARAM_GRID), scrambled, "SPY",
                         first_train_start="2005-01-01", now=NOW)
    assert again.folds[0].chosen_params == f0.chosen_params


def test_walk_forward_refuses_too_little_data():
    short = {s: df[df.index < "2012-01-01"] for s, df in HIST.items()}
    with pytest.raises(ValueError, match="folds"):
        walk_forward(lambda **p: TrendFilter(**p), list(TrendFilter.PARAM_GRID), short, "SPY",
                     first_train_start="2005-01-01")
    with pytest.raises(ValueError, match="years"):
        walk_forward(lambda **p: TrendFilter(**p), list(TrendFilter.PARAM_GRID), HIST, "SPY",
                     first_train_start="2018-01-01", train_years=1, test_years=1)


def test_simulation_charges_costs_and_trades_at_rebalance_only():
    s, e = pd.Timestamp("2010-01-01"), pd.Timestamp("2012-12-31")
    free = simulate(TrendFilter(), HIST, s, e, cost=0.0)
    paid = simulate(TrendFilter(), HIST, s, e, cost=0.01)
    assert paid.sum() < free.sum()
    diff = (free - paid)
    assert set(diff[diff > 1e-12].index) <= set(rebalance_dates(free.index, "monthly"))
    assert simulate(TrendFilter(), HIST, pd.Timestamp("2040-01-01"), pd.Timestamp("2041-01-01")).empty
    assert len(buy_and_hold(HIST, "SPY", s, e)) == len(free)
    assert len(folds_for(HIST["SPY"].index, pd.Timestamp("2005-01-01"), 4, 2)) >= 4
    assert len(rebalance_dates(free.index, "weekly")) > len(rebalance_dates(free.index, "monthly"))


def test_report_round_trips_to_json(tmp_path):
    r = base_report()
    p = r.save(tmp_path / "r.json")
    import json
    d = json.loads(p.read_text())
    assert d["strategy"] == "trend_filter" and len(d["folds"]) == len(r.folds)


# --- registry state machine -----------------------------------------------------------------------

EDGES = [(a, b) for a, bs in ALLOWED.items() for b in bs]


def at_state(reg, name, target):
    path = {State.DRAFT: [], State.BACKTESTED: [State.BACKTESTED],
            State.PAPER: [State.BACKTESTED, State.PAPER],
            State.LIVE_ELIGIBLE: [State.BACKTESTED, State.PAPER, State.LIVE_ELIGIBLE],
            State.REJECTED: [State.BACKTESTED, State.REJECTED], State.RETIRED: [State.RETIRED]}[target]
    reg.register(name)
    for s in path:
        reg.transition(name, s)


@pytest.mark.parametrize("src,dst", EDGES)
def test_every_allowed_transition_works_and_is_journaled(tmp_path, src, dst):
    j = Journal(tmp_path / "j.db")
    reg = Registry(tmp_path / "r.json", journal=j)
    at_state(reg, "s", src)
    reg.transition("s", dst, {"m": 1}, ["why"])
    assert reg.state("s") is dst
    ev = j.events(PROMOTION_EVENT)[-1].payload
    assert ev["from"] == src.value and ev["to"] == dst.value and ev["metrics"] == {"m": 1}


@pytest.mark.parametrize("src,dst", [(a, b) for a in State for b in State if b not in ALLOWED[a]])
def test_every_other_transition_is_refused(tmp_path, src, dst):
    reg = Registry(tmp_path / "r.json")
    at_state(reg, "s", src)
    with pytest.raises(TransitionError):
        reg.transition("s", dst)
    assert reg.state("s") is src


def test_live_eligible_is_never_reached_automatically_from_backtest(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.register("s")
    reg.transition("s", State.BACKTESTED)
    with pytest.raises(TransitionError):
        reg.transition("s", State.LIVE_ELIGIBLE)
    with pytest.raises(TransitionError):
        reg.transition("nope", State.PAPER)


_BASE_REPORT = []


def base_report():
    if not _BASE_REPORT:
        _BASE_REPORT.append(walk_forward(lambda **p: TrendFilter(**p), list(TrendFilter.PARAM_GRID), HIST, "SPY",
                                         first_train_start="2005-01-01", now=NOW))
    return _BASE_REPORT[0]


def report(**kw):
    import copy

    r = copy.deepcopy(base_report())
    for k, v in kw.items():
        setattr(r, k, v)
    return r


@pytest.mark.parametrize("kw,ok", [
    ({"oos_sharpe": 0.8, "oos_max_drawdown": 0.2, "benchmark_oos_max_drawdown": 0.2, "folds_matching_benchmark": .5},
     True),
    ({"oos_sharpe": 0.49, "oos_max_drawdown": 0.2, "benchmark_oos_max_drawdown": 0.2,
      "folds_matching_benchmark": 1}, False),
    ({"oos_sharpe": 0.8, "oos_max_drawdown": 0.26, "benchmark_oos_max_drawdown": 0.2,
      "folds_matching_benchmark": 1}, False),
    ({"oos_sharpe": 0.8, "oos_max_drawdown": 0.1, "benchmark_oos_max_drawdown": 0.2,
      "folds_matching_benchmark": .49}, False),
])
def test_backtest_to_paper_criteria(kw, ok):
    assert criteria.backtest_to_paper(report(**kw)).passed is ok


def test_backtest_and_promote_end_to_end(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    reg = Registry(tmp_path / "r.json", journal=j)
    rep, path, state = backtest_and_promote("trend_filter", TrendFilter, HIST, reg, first_train_start="2005-01-01",
                                            now=NOW, reports_dir=tmp_path)
    assert path.is_file()
    assert state in (State.PAPER, State.REJECTED)
    hist = reg.record("trend_filter")["history"]
    assert [h["to"] for h in hist][:1] == ["BACKTESTED"]
    assert len(j.events(PROMOTION_EVENT)) == 2
    # forced pass / fail paths
    for name, ok in (("good", True), ("bad", False)):
        monkeypatch.setattr(criteria, "backtest_to_paper",
                            lambda r, ok=ok: criteria.Decision(ok, [] if ok else ["nope"], {"oos_cagr": 0.05}))
        _, _, st = backtest_and_promote(name, TrendFilter, HIST, reg, first_train_start="2005-01-01", now=NOW,
                                        reports_dir=tmp_path)
        assert st is (State.PAPER if ok else State.REJECTED)
    assert reg.record("good")["paper_started_at"] == NOW.isoformat()
    # a REJECTED strategy is re-reported but its state doesn't move
    _, _, st = backtest_and_promote("bad", TrendFilter, HIST, reg, first_train_start="2005-01-01", now=NOW,
                                    reports_dir=tmp_path)
    assert st is State.REJECTED


def test_paper_and_retirement_criteria():
    ok = criteria.PaperStats(60, 0, 0.05, 0.06, 0)
    assert criteria.paper_to_live_eligible(ok).passed
    for bad in (criteria.PaperStats(59, 0, 0.05, 0.06, 0), criteria.PaperStats(60, 1, 0.05, 0.06, 0),
                criteria.PaperStats(60, 0, 0.02, 0.06, 0), criteria.PaperStats(60, 0, 0.05, 0.06, 1)):
        assert not criteria.paper_to_live_eligible(bad).passed
    assert not criteria.paper_is_failing(criteria.PaperStats(30, 3, 0, 0.1, 0)).passed  # too early to judge
    assert criteria.paper_is_failing(criteria.PaperStats(60, 3, 0, 0.1, 0)).passed
    assert criteria.should_retire(0.00, 0.06, 0).passed
    assert not criteria.should_retire(0.02, 0.06, 0).passed
    assert criteria.should_retire(None, None, 2).passed
    assert not criteria.should_retire(None, None, 1).passed


# --- SatelliteManager: P1, ledgers, retirement, paper promotion -------------------------------------

class Acct:
    def __init__(self, equity, prices):
        self.equity = equity
        self.prices = prices


def manager(tmp_path, state_name, *, enabled=True, kill_switch=None, history=None):
    j = Journal(tmp_path / "j.db")
    reg = Registry(tmp_path / "r.json", journal=j)
    if state_name is not None:
        at_state(reg, "trend_filter", State(state_name))
    hist = history or HIST
    m = SatelliteManager(SatelliteConfig(enabled=enabled, max_weight=0.10, allowed_states=["PAPER", "LIVE_ELIGIBLE"]),
                         reg, {"trend_filter": TrendFilter()}, j, StateStore(None),
                         lambda syms, as_of: {s: hist[s][hist[s].index <= pd.Timestamp(as_of).tz_localize(None)]
                                              for s in syms if s in hist},
                         kill_switch=kill_switch)
    return m, j, reg


def sat_snap(as_of=datetime(2024, 6, 3, 15, tzinfo=timezone.utc)):
    prices = {s: float(HIST[s]["close"].asof(pd.Timestamp(as_of.date()))) for s in HIST}
    return snapshot(cash=100_000, prices=prices, as_of=as_of)


@pytest.mark.parametrize("st_name", ["DRAFT", "BACKTESTED", "REJECTED", "RETIRED", None])
def test_p1_non_promoted_strategies_never_produce_intents(tmp_path, st_name):
    m, _, _ = manager(tmp_path, st_name)
    snap = sat_snap()
    for d in range(40):  # many days, several month boundaries
        s = snapshot(cash=100_000, prices=snap.prices, as_of=snap.as_of + timedelta(days=d))
        assert m.intents(s, Acct(100_000, s.prices), None) == []
    assert m.active() == []


@pytest.mark.parametrize("st_name", ["PAPER", "LIVE_ELIGIBLE"])
def test_p1_allowed_states_produce_satellite_intents_within_the_sleeve(tmp_path, st_name):
    m, _, _ = manager(tmp_path, st_name)
    snap = sat_snap()
    intents = m.intents(snap, Acct(100_000, snap.prices), None)
    assert intents and all(i.source is Source.SATELLITE and i.strategy_name == "trend_filter" for i in intents)
    spend = sum(i.quantity * snap.prices[i.symbol] for i in intents)
    assert spend <= 0.10 * 100_000 + 1e-6
    assert m.intents(snap, Acct(100_000, snap.prices), None) == []  # once per month
    assert m.cash_reserve(None) == pytest.approx(10_000)


def test_disabled_satellite_never_trades(tmp_path):
    m, _, _ = manager(tmp_path, "LIVE_ELIGIBLE", enabled=False)
    assert m.intents(sat_snap(), Acct(100_000, sat_snap().prices), None) == []


def test_ledger_tracks_fills_and_equity(tmp_path):
    m, _, _ = manager(tmp_path, "PAPER")
    snap = sat_snap()
    assert m.equity(Acct(100_000, snap.prices)) == pytest.approx(10_000)
    m.on_fill("trend_filter", "SPY", "buy", 100.0, 50)
    m.on_fill("unknown", "SPY", "buy", 100.0, 50)  # ignored
    prices = {**snap.prices, "SPY": 80.0}
    assert m.equity(Acct(100_000, prices)) == pytest.approx(5_000 + 50 * 80)
    m.on_fill("trend_filter", "SPY", "sell", 80.0, 50)
    assert "SPY" not in m._ledgers()["trend_filter"]["positions"]
    assert "SPY" in m.symbols()


def test_auto_retire_on_two_r7_halts_in_12_months(tmp_path):
    ks = KillSwitch(RiskConfig(), {"r7_triggers": [(T0 - timedelta(days=200)).isoformat(),
                                                   (T0 - timedelta(days=10)).isoformat()]})
    m, j, reg = manager(tmp_path, "LIVE_ELIGIBLE", kill_switch=ks)
    m.housekeeping(snapshot(cash=1, as_of=T0))
    assert reg.state("trend_filter") is State.RETIRED
    assert "halt" in reg.record("trend_filter")["history"][-1]["reasons"][0]


def test_auto_retire_when_trailing_benchmark_by_more_than_5_points(tmp_path):
    idx = pd.bdate_range("2023-01-02", periods=200)
    spy = pd.DataFrame({"close": np.linspace(100, 120, 200)}, index=idx)  # +20%
    m, j, reg = manager(tmp_path, "PAPER", history={**HIST, "SPY": spy})
    for k, ts in enumerate(idx[:130]):
        j.record_event(EQUITY_EVENT, {"strategy": "trend_filter", "equity": 10_000 + k},  # ~+1.3%
                       ts=datetime.combine(ts.date(), datetime.min.time(), tzinfo=timezone.utc))
    m.housekeeping(snapshot(cash=1, as_of=datetime.combine(idx[130].date(), datetime.min.time(),
                                                           tzinfo=timezone.utc)))
    assert reg.state("trend_filter") is State.RETIRED


def test_paper_promotes_to_live_eligible_after_60_clean_days(tmp_path):
    m, j, reg = manager(tmp_path, "PAPER")
    data = reg.all()
    data["trend_filter"]["paper_started_at"] = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    data["trend_filter"]["backtest_metrics"] = {"oos_cagr": 0.06}
    reg._save(data)
    days = pd.bdate_range("2024-01-02", periods=61)
    for k, d in enumerate(days):
        j.record_event(EQUITY_EVENT, {"strategy": "trend_filter", "equity": 10_000 * (1 + 0.0004 * k)},
                       ts=datetime.combine(d.date(), datetime.min.time(), tzinfo=timezone.utc))
    stats = m.paper_stats("trend_filter", datetime(2024, 4, 1, tzinfo=timezone.utc))
    assert stats.trading_days == 61 and stats.gate_violations == 0
    m.housekeeping(snapshot(cash=1, as_of=datetime(2024, 3, 27, tzinfo=timezone.utc)))
    assert reg.state("trend_filter") is State.LIVE_ELIGIBLE


def test_paper_rejected_after_60_days_with_gate_violations(tmp_path):
    from lumisafe.journal import JournalEntry, new_entry_id
    from lumisafe.types import ReviewResult, Side

    from .helpers import intent

    m, j, reg = manager(tmp_path, "PAPER")
    data = reg.all()
    data["trend_filter"]["paper_started_at"] = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    reg._save(data)
    i = intent("SPY", Side.BUY, 1, source=Source.SATELLITE, strategy_name="trend_filter",
               at=datetime(2024, 1, 5, tzinfo=timezone.utc))
    j.record(JournalEntry(new_entry_id(), i.created_at, i, [ReviewResult("risk", Verdict.BLOCK, "cap")],
                          Verdict.BLOCK, None, None, None))
    for k, d in enumerate(pd.bdate_range("2024-01-02", periods=61)):
        j.record_event(EQUITY_EVENT, {"strategy": "trend_filter", "equity": 10_000.0},
                       ts=datetime.combine(d.date(), datetime.min.time(), tzinfo=timezone.utc))
    m.housekeeping(snapshot(cash=1, as_of=datetime(2024, 3, 27, tzinfo=timezone.utc)))
    assert reg.state("trend_filter") is State.REJECTED
