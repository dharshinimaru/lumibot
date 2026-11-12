"""Promotion pipeline: walk-forward → registry transitions (SPEC §8)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lumisafe import paths
from lumisafe.promotion import criteria
from lumisafe.promotion.registry import Registry, State
from lumisafe.promotion.walk_forward import WalkForwardReport, walk_forward

# TrendFilter needs ~12 months + 200 days of warm-up before the first train window.
DEFAULT_FIRST_TRAIN_START = "2006-01-01"


def backtest_and_promote(name: str, strategy_cls, history, registry: Registry, *, benchmark: str = "SPY",
                         first_train_start: str = DEFAULT_FIRST_TRAIN_START, now: datetime | None = None,
                         reports_dir: Path | None = None) -> tuple[WalkForwardReport, Path, State]:
    """Run the walk-forward, save the report, and move the strategy through the state machine."""
    now = now or datetime.now(timezone.utc)
    registry.register(name, at=now)
    report = walk_forward(lambda **p: strategy_cls(**p), list(strategy_cls.PARAM_GRID), history, benchmark,
                          first_train_start=first_train_start, now=now)
    out = Path(reports_dir or paths.reports_dir()) / f"walk_forward_{name}_{now:%Y%m%d_%H%M%S}.json"
    report.save(out)

    state = registry.state(name)
    if state is State.DRAFT:
        valid = criteria.backtest_is_valid(report)
        if valid.passed:
            registry.transition(name, State.BACKTESTED, valid.metrics, at=now, last_report=str(out))
            state = State.BACKTESTED
    if state is State.BACKTESTED:
        d = criteria.backtest_to_paper(report)
        if d.passed:
            registry.transition(name, State.PAPER, d.metrics, at=now, last_report=str(out),
                                backtest_metrics=d.metrics, paper_started_at=now.isoformat())
        else:
            registry.transition(name, State.REJECTED, d.metrics, d.reasons, at=now, last_report=str(out),
                                backtest_metrics=d.metrics)
        state = registry.state(name)
    return report, out, state
