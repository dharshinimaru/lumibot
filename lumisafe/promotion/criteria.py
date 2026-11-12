"""Promotion and retirement thresholds (SPEC §8). Pure functions over metrics."""

from __future__ import annotations

from dataclasses import dataclass

from lumisafe.promotion.walk_forward import MIN_FOLDS, MIN_YEARS, WalkForwardReport

MIN_OOS_SHARPE = 0.5
MAX_DRAWDOWN_VS_BENCHMARK = 1.25
MIN_FOLDS_MATCHING_BENCHMARK = 0.5

MIN_PAPER_TRADING_DAYS = 60
MIN_PAPER_FRACTION_OF_EXPECTED = 0.5

RETIRE_TRAILING_DAYS = 126  # ~6 months of trading days
RETIRE_TRAIL_POINTS = 0.05
RETIRE_R7_COUNT = 2


@dataclass(frozen=True)
class Decision:
    passed: bool
    reasons: list[str]
    metrics: dict


def backtest_is_valid(r: WalkForwardReport) -> Decision:
    """DRAFT → BACKTESTED: a proper walk-forward was actually run."""
    reasons = []
    if len(r.folds) < MIN_FOLDS:
        reasons.append(f"only {len(r.folds)} folds (need {MIN_FOLDS})")
    if r.years < MIN_YEARS:
        reasons.append(f"only {r.years:.1f} years (need {MIN_YEARS})")
    return Decision(not reasons, reasons, {"folds": len(r.folds), "years": r.years})


def backtest_to_paper(r: WalkForwardReport) -> Decision:
    """BACKTESTED → PAPER on combined out-of-sample results."""
    reasons = []
    if r.oos_sharpe < MIN_OOS_SHARPE:
        reasons.append(f"out-of-sample Sharpe {r.oos_sharpe:.2f} < {MIN_OOS_SHARPE}")
    dd_cap = MAX_DRAWDOWN_VS_BENCHMARK * r.benchmark_oos_max_drawdown
    if r.oos_max_drawdown > dd_cap:
        reasons.append(f"max drawdown {r.oos_max_drawdown:.1%} > 1.25x benchmark's {r.benchmark_oos_max_drawdown:.1%}")
    if r.folds_matching_benchmark < MIN_FOLDS_MATCHING_BENCHMARK:
        reasons.append(f"matched the benchmark's Sharpe in only {r.folds_matching_benchmark:.0%} of folds "
                       f"(need {MIN_FOLDS_MATCHING_BENCHMARK:.0%})")
    metrics = {"oos_sharpe": r.oos_sharpe, "oos_max_drawdown": r.oos_max_drawdown,
               "benchmark_oos_max_drawdown": r.benchmark_oos_max_drawdown,
               "folds_matching_benchmark": r.folds_matching_benchmark, "oos_cagr": r.oos_cagr}
    return Decision(not reasons, reasons, metrics)


@dataclass(frozen=True)
class PaperStats:
    trading_days: int
    gate_violations: int  # intents by this strategy that the RiskGate blocked or resized
    paper_return: float
    expected_return: float  # backtest OOS CAGR scaled to the paper period
    unexplained_fills: int  # submitted orders with neither a fill nor a recorded cancel/expiry


def paper_to_live_eligible(p: PaperStats) -> Decision:
    reasons = []
    if p.trading_days < MIN_PAPER_TRADING_DAYS:
        reasons.append(f"{p.trading_days} paper trading days (need {MIN_PAPER_TRADING_DAYS})")
    if p.gate_violations > 0:
        reasons.append(f"{p.gate_violations} RiskGate violation(s) attempted")
    if p.expected_return > 0 and p.paper_return < MIN_PAPER_FRACTION_OF_EXPECTED * p.expected_return:
        reasons.append(f"paper return {p.paper_return:.1%} < 50% of expected {p.expected_return:.1%}")
    if p.unexplained_fills > 0:
        reasons.append(f"{p.unexplained_fills} unexplained fill(s)")
    return Decision(not reasons, reasons, dict(p.__dict__))


def paper_is_failing(p: PaperStats) -> Decision:
    """PAPER → REJECTED once the 60-day window is done and the bar was not met."""
    d = paper_to_live_eligible(p)
    failing = p.trading_days >= MIN_PAPER_TRADING_DAYS and not d.passed
    return Decision(failing, d.reasons, d.metrics)


def should_retire(trailing_return: float | None, benchmark_trailing_return: float | None,
                  r7_triggers_12m: int) -> Decision:
    """Any → RETIRED: trails the benchmark by > 5 pts over 6 months, or R7 fired twice in 12 months."""
    reasons = []
    if trailing_return is not None and benchmark_trailing_return is not None \
            and benchmark_trailing_return - trailing_return > RETIRE_TRAIL_POINTS:
        reasons.append(f"6-month return {trailing_return:+.1%} trails the benchmark's "
                       f"{benchmark_trailing_return:+.1%} by more than 5 points")
    if r7_triggers_12m >= RETIRE_R7_COUNT:
        reasons.append(f"satellite drawdown halt fired {r7_triggers_12m} times in 12 months")
    return Decision(bool(reasons), reasons, {"trailing_return": trailing_return,
                                             "benchmark_trailing_return": benchmark_trailing_return,
                                             "r7_triggers_12m": r7_triggers_12m})
