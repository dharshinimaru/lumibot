"""Walk-forward backtester (SPEC §8).

For each fold, parameters are chosen ONLY on the train window (best Sharpe)
and evaluated on the next, unseen test window. Combined out-of-sample (OOS)
results are the concatenated test windows. The benchmark is buy-and-hold over
the same windows.

Prices are dividend-adjusted daily bars from Yahoo via the Lumibot bridge
(the same data the core backtest uses). The simulation is vectorized rather
than one event-driven Lumibot backtest per fold × parameter set; see
DECISIONS.md (Phase 6). Costs: 5 bps per trade + 0.05% slippage, charged on
traded notional (turnover).

Look-ahead: at each rebalance date ``t`` the strategy sees only rows with
index <= t (via ``SatelliteStrategy.target_weights(history, t)``), trades at
the close of ``t``, and earns returns from ``t+1`` onward.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from lumisafe.reporting.benchmark import max_drawdown, sharpe

COMMISSION = 0.0005  # 5 bps per trade
SLIPPAGE = 0.0005  # 0.05%
COST = COMMISSION + SLIPPAGE

MIN_FOLDS = 4
MIN_YEARS = 8


@dataclass
class FoldResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    chosen_params: dict
    train_sharpe: float
    test_sharpe: float
    test_return: float
    test_max_drawdown: float
    benchmark_sharpe: float
    benchmark_return: float
    benchmark_max_drawdown: float

    @property
    def matches_benchmark(self) -> bool:
        return self.test_sharpe >= self.benchmark_sharpe


@dataclass
class WalkForwardReport:
    strategy: str
    benchmark: str
    created_at: str
    data_start: str
    data_end: str
    years: float
    cost_per_trade: float
    folds: list[FoldResult] = field(default_factory=list)
    oos_sharpe: float = 0.0
    oos_cagr: float = 0.0
    oos_max_drawdown: float = 0.0
    benchmark_oos_sharpe: float = 0.0
    benchmark_oos_max_drawdown: float = 0.0
    folds_matching_benchmark: float = 0.0  # fraction of folds with test Sharpe >= benchmark's

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


def rebalance_dates(index: pd.DatetimeIndex, frequency: str) -> pd.DatetimeIndex:
    """First trading day of each month (or week)."""
    s = pd.Series(index, index=index)
    key = index.to_period("M") if frequency == "monthly" else index.to_period("W")
    return pd.DatetimeIndex(s.groupby(key).first().values)


def simulate(strategy, history: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp,
             cost: float = COST) -> pd.Series:
    """Daily returns of the sleeve run by ``strategy`` over [start, end]."""
    closes = pd.DataFrame({s: history[s]["close"] for s in strategy.universe if s in history}).sort_index()
    closes = closes[(closes.index >= start) & (closes.index <= end)]
    if closes.empty:
        return pd.Series(dtype=float)
    rets = closes.pct_change().fillna(0.0).to_numpy()
    cols = list(closes.columns)
    rebal = set(rebalance_dates(closes.index, strategy.rebalance_frequency))
    weights = np.zeros(len(cols))
    out = np.zeros(len(closes))
    for k, ts in enumerate(closes.index):
        r = rets[k]
        day_ret = float(weights @ r)  # returns earned today on yesterday's weights
        total = 1 + day_ret
        weights = weights * (1 + r) / total if total > 0 else weights * 0  # drift with today's moves
        if ts in rebal:
            tw = strategy.target_weights(history, ts)
            target = np.array([float(tw.get(c, 0.0)) for c in cols])
            day_ret -= float(np.abs(target - weights).sum()) * cost  # cost on turnover
            weights = target
        out[k] = day_ret
    return pd.Series(out, index=closes.index)


def buy_and_hold(history: dict[str, pd.DataFrame], symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    c = history[symbol]["close"].sort_index()
    c = c[(c.index >= start) & (c.index <= end)]
    return c.pct_change().fillna(0.0)


def _equity(returns: pd.Series) -> pd.Series:
    return (1 + returns).cumprod()


def _cagr(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    years = len(returns) / 252
    total = float(_equity(returns).iloc[-1])
    return total ** (1 / years) - 1 if years > 0 and total > 0 else 0.0


def folds_for(index: pd.DatetimeIndex, first_train_start: pd.Timestamp, train_years: int, test_years: int):
    """Rolling folds: train [s, s+train), test [s+train, s+train+test), stepping by test_years."""
    folds = []
    s = pd.Timestamp(first_train_start)
    last = index[-1]
    while True:
        tr_end = s + pd.DateOffset(years=train_years)
        te_end = tr_end + pd.DateOffset(years=test_years)
        if te_end - pd.Timedelta(days=1) > last:
            break
        folds.append((s, tr_end - pd.Timedelta(days=1), tr_end, te_end - pd.Timedelta(days=1)))
        s = s + pd.DateOffset(years=test_years)
    return folds


def walk_forward(strategy_factory: Callable[..., object], param_grid: list[dict], history: dict[str, pd.DataFrame],
                 benchmark: str, *, first_train_start: str | pd.Timestamp, train_years: int = 4,
                 test_years: int = 2, cost: float = COST, now: datetime | None = None) -> WalkForwardReport:
    probe = strategy_factory()
    bench_idx = history[benchmark]["close"].sort_index().index
    folds = folds_for(bench_idx, pd.Timestamp(first_train_start), train_years, test_years)
    if len(folds) < MIN_FOLDS:
        raise ValueError(f"walk-forward needs >= {MIN_FOLDS} folds; data allows {len(folds)}")
    data_start, data_end = folds[0][0], folds[-1][3]
    years = (data_end - data_start).days / 365.25
    if years < MIN_YEARS:
        raise ValueError(f"walk-forward needs >= {MIN_YEARS} years of data; got {years:.1f}")

    report = WalkForwardReport(
        strategy=probe.name, benchmark=benchmark, created_at=(now or datetime.now()).isoformat(),
        data_start=str(data_start.date()), data_end=str(data_end.date()), years=round(years, 2), cost_per_trade=cost,
    )
    oos, oos_bench = [], []
    for tr_s, tr_e, te_s, te_e in folds:
        best, best_sharpe = None, -np.inf
        for params in param_grid:  # selection uses the TRAIN window only
            r = simulate(strategy_factory(**params), history, tr_s, tr_e, cost)
            sh = sharpe(r)
            if sh > best_sharpe:
                best, best_sharpe = params, sh
        test = simulate(strategy_factory(**best), history, te_s, te_e, cost)
        bench = buy_and_hold(history, benchmark, te_s, te_e)
        oos.append(test)
        oos_bench.append(bench)
        report.folds.append(FoldResult(
            train_start=str(tr_s.date()), train_end=str(tr_e.date()), test_start=str(te_s.date()),
            test_end=str(te_e.date()), chosen_params=dict(best), train_sharpe=round(best_sharpe, 4),
            test_sharpe=round(sharpe(test), 4), test_return=round(float(_equity(test).iloc[-1] - 1), 4),
            test_max_drawdown=round(max_drawdown(_equity(test)), 4),
            benchmark_sharpe=round(sharpe(bench), 4),
            benchmark_return=round(float(_equity(bench).iloc[-1] - 1), 4),
            benchmark_max_drawdown=round(max_drawdown(_equity(bench)), 4),
        ))
    all_oos = pd.concat(oos)
    all_bench = pd.concat(oos_bench)
    report.oos_sharpe = round(sharpe(all_oos), 4)
    report.oos_cagr = round(_cagr(all_oos), 4)
    report.oos_max_drawdown = round(max_drawdown(_equity(all_oos)), 4)
    report.benchmark_oos_sharpe = round(sharpe(all_bench), 4)
    report.benchmark_oos_max_drawdown = round(max_drawdown(_equity(all_bench)), 4)
    report.folds_matching_benchmark = round(sum(f.matches_benchmark for f in report.folds) / len(report.folds), 4)
    return report
