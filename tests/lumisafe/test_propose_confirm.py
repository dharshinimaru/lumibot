"""`lumisafe propose` / `confirm` through the real engine wiring (coach → tax → risk), offline."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from lumisafe import paths
from lumisafe.coach.behavior import PendingStore
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.journal import Journal
from lumisafe.reporting import cli
from lumisafe.runner import StateStore, assemble_engine
from lumisafe.types import Side, Source, Verdict

from .fakes import FakeExecutor

NOW = datetime(2026, 3, 2, 15, tzinfo=timezone.utc)  # a Monday
PRICES = {"VTI": 100.0, "VXUS": 50.0, "BND": 80.0, "AAPL": 100.0, "MEME": 130.0}


def history(symbol, as_of):
    idx = pd.bdate_range(end=as_of.date(), periods=300)
    v = np.full(len(idx), 100.0)
    if symbol == "MEME":
        v[-20:] = np.linspace(102, 130, 20)  # +30% in 20 days
    if symbol == "VTI":
        v[-20:] = np.linspace(99, 88, 20)  # -12%
    return pd.Series(v, index=idx)


class World:
    def __init__(self):
        self.now = NOW
        self.ex = FakeExecutor(NOW, 100_000, PRICES, positions={"VTI": 100}, is_backtesting=False,
                               auto_fill=False)

    def runner(self, config, run_once=True):
        self.ex.now = self.now
        engine = assemble_engine(config, Journal(), StateStore(paths.state_path()), mode="paper",
                                 history_fn=history)
        engine.cycle(self.ex)
        for o in self.ex.submitted:  # the paper broker fills after the cycle has journaled
            if o.status != "filled":
                self.ex.fill(o)
                engine.on_fill(o.identifier, o.avg_fill_price, o.quantity, self.now)


@pytest.fixture
def world(monkeypatch):
    w = World()
    cfg = LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2},
                                                  "monthly_contribution_usd": 500},
                                         "risk": {"max_single_etf_weight": 0.65}})
    save_config(cfg, paths.config_path())
    monkeypatch.setattr(cli, "paper_runner", w.runner)
    monkeypatch.setattr(cli, "now_utc", lambda: w.now)
    return w


def run(*args):
    return CliRunner().invoke(cli.main, list(args))


def user_entries():
    return Journal().query(source=Source.USER)


def test_propose_approved_trade_is_submitted_and_journaled(world):
    res = run("propose", "BUY", "AAPL", "3")
    assert res.exit_code == 0, res.output
    assert "Result: APPROVE" in res.output
    [e] = user_entries()
    assert [r.reviewer for r in e.reviews] == ["coach", "tax", "risk"]
    assert any(o.symbol == "AAPL" for o in world.ex.submitted)


def test_propose_oversized_stock_is_resized_by_the_gate(world):
    res = run("propose", "BUY", "AAPL", "500")
    assert "Result: RESIZE" in res.output


def test_propose_chasing_is_delayed_then_confirmed_after_24h(world):
    res = run("propose", "BUY", "MEME", "5")
    assert "Result: DELAY" in res.output and "lumisafe confirm" in res.output
    [delayed] = user_entries()
    assert not any(o.symbol == "MEME" for o in world.ex.submitted)
    short_id = delayed.intent.intent_id[:8]

    world.now = NOW + timedelta(hours=2)
    early = run("confirm", short_id)
    assert early.exit_code == 1 and "Still cooling off" in early.output

    world.now = NOW + timedelta(hours=25)
    ok = run("confirm", short_id)
    assert ok.exit_code == 0, ok.output
    assert "Result: APPROVE" in ok.output
    entries = user_entries()
    assert len(entries) == 2  # J2: original (DELAY) + confirmed re-entry, one entry each
    reentry = entries[1]
    assert reentry.intent.intent_id != delayed.intent.intent_id
    assert reentry.reviews[0].reviewer == "coach"  # re-entered from the start (B4)
    assert any(o.symbol == "MEME" for o in world.ex.submitted)
    assert PendingStore(paths.pending_path()).get(short_id).status == "confirmed"
    again = run("confirm", short_id)
    assert again.exit_code == 1 and "already confirmed" in again.output


def test_unconfirmed_delayed_intent_expires_after_72h(world):
    run("propose", "SELL", "VTI", "10")  # panic sell in a -12% drop
    [delayed] = user_entries()
    assert delayed.final is Verdict.DELAY
    world.now = NOW + timedelta(hours=73)
    res = run("confirm", delayed.intent.intent_id[:8])
    assert res.exit_code == 1 and "expired" in res.output
    assert Journal().events("delayed_intent_expired")


def test_third_proposal_of_the_day_is_delayed(world):
    run("propose", "BUY", "AAPL", "1")
    run("propose", "BUY", "AAPL", "1")
    res = run("propose", "BUY", "AAPL", "1")
    assert "Result: DELAY" in res.output and "#3" in res.output


def test_wash_sale_after_losing_sale_is_blocked(world):
    from lumisafe.types import TaxLot

    st = StateStore(paths.state_path())
    st.set("lots", [{"symbol": "AAPL", "quantity": 10, "cost_basis": 150.0,
                     "acquired_at": (NOW - timedelta(days=200)).isoformat()}])
    st.save()
    world.ex.positions["AAPL"] = 10
    assert "Result: APPROVE" in run("propose", "SELL", "AAPL", "10").output  # sold at 100: a loss
    assert Journal().events("realized")[0].payload["pnl"] == pytest.approx(-500.0)
    world.now = NOW + timedelta(days=1)
    res = run("propose", "BUY", "AAPL", "1")
    assert "Result: BLOCK" in res.output and "wash sale" in res.output and "Not tax advice." in res.output
    _ = TaxLot


def test_unknown_or_bad_input(world):
    assert run("confirm", "deadbeef").exit_code == 1
    assert run("propose", "BUY", "AAPL", "0").exit_code == 1
    assert run("propose", "HOLD", "AAPL", "1").exit_code != 0


def test_propose_without_a_cycle_says_queued(monkeypatch, world):
    monkeypatch.setattr(cli, "paper_runner", lambda config, run_once=True: None)
    res = run("propose", "BUY", "AAPL", "1")
    assert "Queued" in res.output
