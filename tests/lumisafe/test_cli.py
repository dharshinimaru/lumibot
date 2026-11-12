"""CLI: init → config → status shows you-vs-benchmark (Phase 4 done criterion)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import load_config
from lumisafe.journal import Journal
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.reporting import cli
from lumisafe.runner import StateStore

QUIZ_INPUT = "\n".join(["a", "20", "c", "300", "y", "n", "n"]) + "\n"


@pytest.fixture
def runner():
    return CliRunner()


def fake_history(symbols, start=None, end=None):
    idx = pd.bdate_range("2026-01-01", periods=60)
    return {s: pd.DataFrame({"close": 400 * np.cumprod(np.full(len(idx), 1.001))}, index=idx) for s in symbols}


def test_init_writes_plan_and_shows_disclaimer(runner):
    res = runner.invoke(cli.main, ["init"], input=QUIZ_INPUT)
    assert res.exit_code == 0, res.output
    assert DISCLAIMER in res.output and "lose value" in res.output
    cfg = load_config(paths.config_path())
    assert cfg.mode == "paper" and cfg.core.monthly_contribution_usd == 300
    again = runner.invoke(cli.main, ["init"], input=QUIZ_INPUT)
    assert again.exit_code == 1 and "--force" in again.output
    assert runner.invoke(cli.main, ["init", "--force"], input=QUIZ_INPUT).exit_code == 0


def test_status_without_plan_asks_for_init(runner):
    res = runner.invoke(cli.main, ["status"])
    assert res.exit_code == 1 and "lumisafe init" in res.output


def test_quiz_to_config_to_status_shows_you_vs_benchmark(runner, monkeypatch):
    monkeypatch.setattr(cli, "history_provider", fake_history)
    assert runner.invoke(cli.main, ["init"], input=QUIZ_INPUT).exit_code == 0
    j = Journal()
    t0 = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    for d, eq in enumerate([10_000, 10_050, 10_120, 10_090]):
        j.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "satellite_enabled": False, "equity": eq},
                       ts=t0 + timedelta(days=d))
    st = StateStore(paths.state_path())
    st.set("last_snapshot", {"as_of": "2026-01-08T15:00:00", "cash": 100.0, "equity": 10_090.0,
                             "positions": {"VTI": 30, "VXUS": 20, "BND": 25, "AAPL": 1},
                             "prices": {"VTI": 200.0, "VXUS": 60.0, "BND": 75.0, "AAPL": 150.0}})
    st.set("kill_switch", {"satellite_latched": True})
    st.save()
    res = runner.invoke(cli.main, ["status"])
    assert res.exit_code == 0, res.output
    assert "Your return: +0.90%" in res.output
    assert "If you'd just held SPY: +" in res.output
    assert "VTI" in res.output and "AAPL" in res.output and "reset-halt" in res.output
    assert DISCLAIMER in res.output


def test_status_degrades_gracefully_offline(runner, monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(cli, "history_provider", boom)
    runner.invoke(cli.main, ["init"], input=QUIZ_INPUT)
    j = Journal()
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for d in range(3):
        j.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "equity": 100 + d}, ts=t0 + timedelta(days=d))
    res = runner.invoke(cli.main, ["status"])
    assert res.exit_code == 0 and "comparison unavailable" in res.output


def test_status_with_no_history_yet(runner):
    runner.invoke(cli.main, ["init"], input=QUIZ_INPUT)
    res = runner.invoke(cli.main, ["status"])
    assert res.exit_code == 0 and "Not enough history" in res.output


def test_journal_command_lists_and_filters(runner, tmp_path):
    from lumisafe.journal import JournalEntry, new_entry_id
    from lumisafe.types import ReviewResult, Verdict

    from .helpers import intent

    j = Journal()
    assert "No journal entries" in runner.invoke(cli.main, ["journal"]).output
    for final in (Verdict.APPROVE, Verdict.BLOCK):
        i = intent("AAPL", qty=3)
        j.record(JournalEntry(new_entry_id(), i.created_at, i, [ReviewResult("risk", final, f"risk {final.value}")],
                              final, None, None, None))
    out = runner.invoke(cli.main, ["journal"]).output
    assert "APPROVE" in out and "BLOCK" in out
    rej = runner.invoke(cli.main, ["journal", "--rejected"]).output
    assert "BLOCK" in rej and "APPROVE" not in rej


def test_run_paper_uses_paper_runner(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "paper_runner", lambda cfg, run_once=True: calls.append((cfg.mode, run_once)))
    runner.invoke(cli.main, ["init"], input=QUIZ_INPUT)
    res = runner.invoke(cli.main, ["run-paper"])
    assert res.exit_code == 0 and calls == [("paper", True)]


def test_go_live_command_exists_but_is_not_exercised_here():
    # SPEC §3 rule 2: the agent never runs `lumisafe go-live`. The gate itself is
    # tested directly in test_live_unlock.py.
    assert "go-live" in cli.main.commands
