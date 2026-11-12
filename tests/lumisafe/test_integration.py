"""Integration tests (SPEC §13). Run with: pytest tests/lumisafe -m integration

They touch Yahoo data and Alpaca PAPER only. They never run in the default suite.
"""

from datetime import datetime

import pytest

from lumisafe.config.schema import LumiSafeConfig
from lumisafe.journal import Journal
from lumisafe.types import Source, Verdict

pytestmark = pytest.mark.integration

BALANCED = {"core": {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2}, "monthly_contribution_usd": 0},
            "risk": {"max_single_etf_weight": 0.65}}


def test_balanced_core_5y_backtest_completes_via_lumibot():
    from lumisafe.runner import run_core_backtest

    r = run_core_backtest(LumiSafeConfig.model_validate(BALANCED), datetime(2019, 1, 1), datetime(2024, 1, 1))
    j = Journal(r.journal_path)
    entries = j.query()
    assert entries, "the core placed no orders"
    assert all(e.intent.source is Source.CORE for e in entries)
    submitted = j.query(submitted=True)
    assert submitted and all(e.fill_qty for e in submitted), "every submitted order should be filled and journaled"
    assert len(j.events("paper_cycle")) > 1000  # one heartbeat per trading day
    assert r.output.results is not None


def test_paper_cycle_places_and_journals_core_dca_order(_isolated_lumisafe_home):
    from lumisafe import paths
    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.runner import run_paper_cycle

    bridge.load_dotenv_file(".env")
    cfg = LumiSafeConfig.model_validate({**BALANCED, "core": {**BALANCED["core"], "monthly_contribution_usd": 50},
                                         "ai_enabled": False})
    run_paper_cycle(cfg)
    j = Journal(paths.journal_path())
    entries = j.query(source=Source.CORE)
    assert entries, "no core intent reached the pipeline"
    submitted = [e for e in entries if e.final in (Verdict.APPROVE, Verdict.RESIZE)]
    assert submitted, f"no core DCA order was submitted: {[r.explanation for e in entries for r in e.reviews]}"
    assert j.events("paper_cycle")[-1].payload["mode"] == "paper"


def test_balanced_core_10y_tracks_blended_benchmark_within_1_5_points():
    """SPEC §15: last-10-year Balanced core backtest tracks its blended benchmark within 1.5 pts/yr."""
    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.reporting import benchmark as bm
    from lumisafe.runner import run_core_backtest

    cfg = LumiSafeConfig.model_validate(BALANCED)
    start, end = datetime(2015, 11, 1), datetime(2025, 11, 1)
    r = run_core_backtest(cfg, start, end)
    equity = bm.equity_from_journal(Journal(r.journal_path), mode="backtest")
    hist = bridge.fetch_daily_history(list(cfg.core.etfs), start, end)
    blend = bm.blended_benchmark({s: hist[s]["close"] for s in cfg.core.etfs}, cfg.core.etfs)
    gap = bm.tracking_gap_per_year(equity, blend)
    print(f"core CAGR {bm.cagr(equity):.2%} vs blend {bm.cagr(blend):.2%} → gap {gap:+.2%}/yr")
    assert abs(gap) <= 0.015
