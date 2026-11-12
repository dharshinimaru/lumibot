"""CLI: strategies, backtest, reset-halt; satellite intents through the engine pipeline."""

from datetime import datetime, timezone

import pandas as pd
from click.testing import CliRunner

from lumisafe import paths
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.journal import Journal
from lumisafe.promotion.registry import Registry, State
from lumisafe.reporting import cli
from lumisafe.risk.kill_switch import RESET_CONFIRMATION
from lumisafe.runner import StateStore, assemble_engine
from lumisafe.types import Source

from .fakes import FakeExecutor
from .synthetic import random_walks

HIST = random_walks()
NOW = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
CFG = {"core": {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2}, "monthly_contribution_usd": 1000},
       "risk": {"max_single_etf_weight": 0.65},
       "satellite": {"enabled": True, "max_weight": 0.10, "allowed_states": ["PAPER", "LIVE_ELIGIBLE"]}}


def run(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input)


def test_strategies_lists_catalog_before_any_backtest():
    out = run("strategies").output
    assert "trend_filter" in out and "not backtested yet" in out


def test_backtest_command_saves_report_and_sets_state(monkeypatch):
    monkeypatch.setattr(cli, "history_provider", lambda syms, s=None, e=None: {k: HIST[k] for k in syms})
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    res = run("backtest", "trend_filter")
    assert res.exit_code == 0, res.output
    assert "Walk-forward:" in res.output and "Report saved to" in res.output
    state = Registry().state("trend_filter")
    assert state in (State.PAPER, State.REJECTED)
    assert list(paths.reports_dir().glob("walk_forward_trend_filter_*.json"))
    listing = run("strategies").output
    assert state.value in listing and "OOS Sharpe" in listing
    assert run("backtest", "nope").exit_code == 1


def test_reset_halt_requires_typed_confirmation():
    save_config(LumiSafeConfig.model_validate(CFG), paths.config_path())
    assert "not halted" in run("reset-halt", "satellite").output
    st = StateStore(paths.state_path())
    st.set("kill_switch", {"satellite_latched": True, "r7_triggers": ["2026-01-01T00:00:00+00:00"]})
    st.set("satellite_peak_equity", 12_000)
    st.save()
    bad = run("reset-halt", "satellite", input="yes\n")
    assert bad.exit_code == 1 and StateStore(paths.state_path()).get("kill_switch")["satellite_latched"]
    good = run("reset-halt", "satellite", input=RESET_CONFIRMATION + "\n")
    assert good.exit_code == 0 and "reset" in good.output
    after = StateStore(paths.state_path())
    assert after.get("kill_switch")["satellite_latched"] is False
    assert after.get("kill_switch")["r7_triggers"]  # history kept for auto-retirement
    assert after.get("satellite_peak_equity") == 0.0
    assert Journal().events("halt_reset")


def test_paper_engine_runs_promoted_satellite_through_the_pipeline():
    config = LumiSafeConfig.model_validate(CFG)
    journal = Journal()
    reg = Registry(journal=journal)
    reg.register("trend_filter")
    reg.transition("trend_filter", State.BACKTESTED)
    reg.transition("trend_filter", State.PAPER, paper_started_at=NOW.isoformat())

    def sat_hist(symbols, as_of):
        cut = pd.Timestamp(as_of).tz_convert("America/New_York").tz_localize(None)
        return {s: HIST[s][HIST[s].index <= cut] for s in symbols if s in HIST}

    prices = {s: float(HIST[s]["close"].asof(pd.Timestamp("2026-01-05"))) for s in HIST}
    prices.update({"VTI": 100.0, "VXUS": 50.0, "BND": 80.0})
    engine = assemble_engine(config, journal, StateStore(paths.state_path()), mode="paper",
                             history_fn=lambda s, a: None, satellite_history=sat_hist)
    ex = FakeExecutor(NOW, 100_000, prices, auto_fill=False, is_backtesting=False)
    report = engine.cycle(ex)
    sat = [o for o in report.outcomes if o.entry.intent.source is Source.SATELLITE]
    assert sat, "a PAPER-state strategy should trade in paper mode"
    for o in sat:
        assert [r.reviewer for r in o.entry.reviews] == ["coach", "tax", "risk"]
    core_spend = sum(o.executed_qty * prices[o.entry.intent.symbol] for o in report.outcomes
                     if o.entry.intent.source is Source.CORE)
    assert core_spend <= 1000  # the sleeve's cash is reserved, not spent by the core
    for o in ex.submitted:
        ex.fill(o)
        engine.on_fill(o.identifier, o.avg_fill_price, o.quantity, NOW)
    assert engine.satellite.equity(type("A", (), {"equity": 100_000, "prices": ex.prices})()) > 9_000
