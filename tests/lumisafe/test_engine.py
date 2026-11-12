"""Engine: one trading cycle end to end against a fake Lumibot executor (no network)."""

from datetime import timedelta

import pytest

from lumisafe.adapters import lumibot_bridge as bridge
from lumisafe.config.schema import LumiSafeConfig
from lumisafe.journal import Journal
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.runner import Engine, StateStore, strategy_parameters, week_start
from lumisafe.types import Side, Source, Verdict

from .fakes import FakeExecutor
from .helpers import T0, intent

PRICES = {"VTI": 100.0, "VXUS": 50.0, "BND": 80.0}


def config(**kw):
    base = {"core": {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2}, "monthly_contribution_usd": 1000},
            "risk": {"max_single_etf_weight": 0.65}}
    base.update(kw)
    return LumiSafeConfig.model_validate(base)


@pytest.fixture
def setup(tmp_path):
    journal = Journal(tmp_path / "j.db")
    state = StateStore(tmp_path / "state.json")
    engine = Engine(config(), journal, state, mode="paper")
    ex = FakeExecutor(T0, 10_000, PRICES, auto_fill=False, is_backtesting=False)
    return engine, journal, state, ex


def fill_all(ex, engine):
    for o in list(ex.submitted):
        if o.status != "filled":
            ex.fill(o)
            engine.on_fill(o.identifier, o.avg_fill_price, o.quantity, ex.now)


def test_first_cycle_invests_monthly_contribution_and_journals(setup):
    engine, journal, state, ex = setup
    report = engine.cycle(ex)
    assert report.outcomes and all(o.submitted for o in report.outcomes)
    assert all(o.entry.intent.source is Source.CORE for o in report.outcomes)
    spent = sum(o.executed_qty * PRICES[o.entry.intent.symbol] for o in report.outcomes)
    assert 950 <= spent <= 1000
    fill_all(ex, engine)
    entries = journal.query()
    assert len(entries) == len(report.outcomes) and all(e.fill_qty for e in entries)
    assert {l.symbol for l in engine.lots.lots} == {o.entry.intent.symbol for o in report.outcomes}
    beats = journal.events(PAPER_CYCLE_EVENT)
    assert beats[-1].payload == {"mode": "paper", "satellite_enabled": False, "equity": 10_000, "cash": 10_000}
    assert state.get("last_snapshot")["equity"] == 10_000
    assert state.path.is_file()


def test_no_new_buys_later_in_the_same_month(setup):
    engine, journal, _, ex = setup
    engine.cycle(ex)
    fill_all(ex, engine)
    ex.now = T0 + timedelta(days=1)
    assert engine.cycle(ex).outcomes == []
    ex.now = T0 + timedelta(days=31)  # next month
    assert engine.cycle(ex).outcomes


def test_paper_fills_are_reconciled_next_cycle(setup):
    engine, journal, state, ex = setup
    engine.cycle(ex)
    for o in ex.submitted:
        ex.fill(o)  # broker fills while the process isn't running
    ex.now = T0 + timedelta(days=1)
    engine.cycle(ex)
    assert all(e.fill_qty for e in journal.query())
    assert state.get("pending_orders") == {}


def test_canceled_orders_are_dropped_and_recorded(setup):
    engine, journal, state, ex = setup
    engine.cycle(ex)
    ex.submitted[0].status = "canceled"
    ex.now = T0 + timedelta(days=1)
    engine.cycle(ex)
    assert journal.events("order_not_filled")


def test_state_survives_restart(setup, tmp_path):
    engine, journal, state, ex = setup
    engine.cycle(ex)
    fill_all(ex, engine)
    again = Engine(config(), journal, StateStore(tmp_path / "state.json"), mode="paper")
    assert len(again.lots.lots) == len(engine.lots.lots)
    assert again.state.get("peak_equity") == pytest.approx(state.get("peak_equity"))


def test_trades_this_week_counts_only_non_core_submissions(setup):
    engine, journal, _, ex = setup
    engine.cycle(ex)  # core trades
    snap = engine.build_snapshot(bridge.account_state(
        ex, engine.symbols()))
    engine.pipeline.run(intent("SPY", qty=1, source=Source.USER, at=T0), snap, ex)
    ex.prices["SPY"] = 100.0
    snap = engine.build_snapshot(bridge.account_state(
        ex, engine.symbols() | {"SPY"}))
    engine.pipeline.run(intent("SPY", qty=1, source=Source.USER, at=T0), snap, ex)
    assert engine.trades_this_week(T0) == 1
    assert week_start(T0 + timedelta(days=3)) == week_start(T0)


def test_after_submission_spends_cash_and_counts_trades(setup):
    engine, _, _, ex = setup
    engine.cycle(ex)
    out = engine.pipeline.run(intent("VTI", qty=1, source=Source.USER), engine.build_snapshot(
        bridge.account_state(ex, engine.symbols())), ex)
    snap = engine.build_snapshot(bridge.account_state(
        ex, engine.symbols()))
    after = Engine.after_submission(snap, out)
    assert after.cash == pytest.approx(snap.cash - 100.0)
    assert after.trades_this_week == snap.trades_this_week + 1


def test_engine_refuses_live_mode(tmp_path):
    with pytest.raises(ValueError):
        Engine(config(), Journal(tmp_path / "j.db"), StateStore(None), mode="live")


def test_account_alert_is_journaled(setup):
    engine, journal, state, ex = setup
    engine.cycle(ex)
    fill_all(ex, engine)
    state.set("peak_equity", 100_000)
    ex.now = T0 + timedelta(days=1)
    report = engine.cycle(ex)
    assert report.alerts and journal.events("alert")


class FakeSatellite:
    def __init__(self, equity):
        self.eq = equity
        self.calls = 0

    def symbols(self):
        return {"QQQ"}

    def equity(self, acct):
        return self.eq

    def cash_reserve(self, acct):
        return 0.0

    def intents(self, snap, acct, executor):
        self.calls += 1
        return []


def test_r7_latch_stops_satellite_and_freezes_its_peak(tmp_path):
    sat = FakeSatellite(10_000)
    engine = Engine(config(), Journal(tmp_path / "j.db"), StateStore(None), mode="paper", satellite=sat)
    ex = FakeExecutor(T0, 10_000, {**PRICES, "QQQ": 1.0}, auto_fill=False, is_backtesting=False)
    engine.cycle(ex)
    assert sat.calls == 1
    sat.eq = 8_000  # -20% → R7
    ex.now = T0 + timedelta(days=1)
    engine.cycle(ex)
    assert engine.kill_switch.satellite_latched and sat.calls == 1
    sat.eq = 12_000  # recovered beyond old peak: still latched, peak frozen
    ex.now = T0 + timedelta(days=2)
    engine.cycle(ex)
    assert sat.calls == 1 and engine.state.get("satellite_peak_equity") == 10_000


def test_strategy_parameters_shape(tmp_path):
    p = strategy_parameters(config(), journal_path=tmp_path / "j.db", state_path=None, mode="backtest")
    assert p["mode"] == "backtest" and p["state_path"] is None
    assert LumiSafeConfig.model_validate(p["lumisafe_config"]) == config()
