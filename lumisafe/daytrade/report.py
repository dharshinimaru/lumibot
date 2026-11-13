"""Walk-forward test of the day-trading lab, after every cost and tax, against simply holding SPY.

Protocol (SPEC §8 thresholds, unchanged):
  * rolling folds: tune on 2 calendar years, test on the next one, 2018-2025;
  * parameters are picked only on the training years, by Sharpe after costs;
  * out-of-sample (OOS) results are the test years stitched together;
  * the same promotion bar as every other strategy (``promotion.criteria``).

Each parameter set is simulated once over the whole period with the account
state carried day to day (equity, PDT count, unsettled cash); folds take
slices of those daily returns. Returns are on the day-trading slice's money.

Taxes (applied to the stitched OOS trades; set your rate in config):
  * net short-term gains are taxed at ``short_term_tax_rate`` each calendar year;
  * a net loss offsets up to $3,000 of other income that year (credited at the
    same rate); the rest carries forward;
  * wash sales: a loss is deferred into the next trade of the same stock opened
    within 30 days and realized when that trade closes (so a December loss
    followed by a January re-entry moves to the next tax year). Purchases in the
    30 days *before* a loss sale are not modelled (those lots are already closed).
  * SPY: dividends taxed yearly at ``long_term_tax_rate`` (qualified); the
    gain shown both before and after the tax due if sold at the end.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from lumisafe.config.schema import DayTradeConfig
from lumisafe.daytrade.sim import AccountState, Costs, DayResult, Params, Trade, simulate_day
from lumisafe.promotion.walk_forward import FoldResult, WalkForwardReport, folds_for
from lumisafe.reporting.benchmark import max_drawdown, sharpe

from lumisafe.daytrade import INTRADAY_STRATEGY as STRATEGY_NAME  # noqa: E402
LOSS_DEDUCTION_LIMIT = 3_000.0
WASH_SALE_DAYS = 30

GRID: list[Params] = [
    Params(min_gap=g, min_rel_volume=rv, target_r=r, focused_news=fn)
    for g, rv, r, fn in itertools.product((0.02, 0.04), (0.05, 0.15), (1.0, 2.0), (False, True))
]


def run_period(days: list[date], cands_by_day: dict[str, list], load_bars: Callable[[date, list[str]], dict],
               params: Params, cfg: DayTradeConfig, costs: Costs, account_equity: float
               ) -> tuple[pd.Series, list[DayResult]]:
    """Daily returns of the slice (0 on days without trades) and each day's result."""
    acct = AccountState(slice_equity=cfg.slice_fraction * account_equity,
                        other_equity=(1 - cfg.slice_fraction) * account_equity)
    rets, results = {}, []
    for d in days:
        cands = cands_by_day.get(d.isoformat(), [])
        bars = load_bars(d, [c.symbol for c in cands]) if cands else {}
        start = acct.slice_equity
        res, acct = simulate_day(d, cands, bars, params, cfg, costs, acct)
        rets[pd.Timestamp(d)] = res.pnl / start if start > 0 else 0.0
        results.append(res)
    return pd.Series(rets, dtype=float), results


# --- taxes --------------------------------------------------------------------------------------------


def taxable_events(trades: list[Trade]) -> list[tuple[date, float]]:
    """(realization date, taxable amount) after wash-sale deferral."""
    out: list[tuple[date, float]] = []
    pending: dict[str, list[tuple[date, float]]] = defaultdict(list)  # symbol -> [(loss sale date, loss)]
    for t in sorted(trades, key=lambda t: t.entry_ts):
        d = datetime.fromisoformat(t.entry_ts).date()
        carried = 0.0
        keep = []
        for sold_on, loss in pending[t.symbol]:
            if (d - sold_on).days <= WASH_SALE_DAYS:
                carried += loss  # disallowed; added to this trade's cost basis
            else:
                out.append((sold_on, loss))  # no replacement within 30 days: realized when sold
        pending[t.symbol] = keep
        amount = t.net_pnl + carried
        exit_day = datetime.fromisoformat(t.exit_ts).date()
        if amount < 0:
            pending[t.symbol].append((exit_day, amount))
        else:
            out.append((exit_day, amount))
    for sym, losses in pending.items():
        out += losses
    return out


def yearly_tax(events: list[tuple[date, float]], rate: float) -> dict[int, float]:
    """Tax paid (+) or saved (-) per calendar year, with the $3,000 loss limit and carryforward."""
    by_year: dict[int, float] = defaultdict(float)
    for d, amt in events:
        by_year[d.year] += amt
    tax, carry = {}, 0.0
    for y in sorted(by_year):
        net = by_year[y] + carry
        if net >= 0:
            tax[y], carry = rate * net, 0.0
        else:
            deductible = max(net, -LOSS_DEDUCTION_LIMIT)
            tax[y], carry = rate * deductible, net - deductible
    return tax


def apply_taxes(returns: pd.Series, tax_by_year: dict[int, float], start_equity: float) -> pd.Series:
    """Subtract each year's tax on its last trading day (a refund adds)."""
    out = returns.copy()
    equity = start_equity * (1 + out).cumprod()
    for y, tax in tax_by_year.items():
        days = out.index[out.index.year == y]
        if len(days) == 0:
            continue
        last = days[-1]
        prev_equity = equity.shift(1).fillna(start_equity).loc[last]
        out.loc[last] = (equity.loc[last] - tax) / prev_equity - 1
        equity = start_equity * (1 + out).cumprod()
    return out


def cagr(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    total = float((1 + returns).prod())
    years = len(returns) / 252
    return total ** (1 / years) - 1 if total > 0 and years > 0 else -1.0


# --- the test ---------------------------------------------------------------------------------------------


@dataclass
class LabReport:
    walk_forward: WalkForwardReport
    passed: bool
    reasons: list[str]
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"walk_forward": self.walk_forward.to_dict(), "passed": self.passed, "reasons": self.reasons,
                "summary": self.summary}

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path


def walk_forward(cands_by_day: dict[str, list], load_bars: Callable[[date, list[str]], dict],
                 spy_total: pd.Series, spy_price: pd.Series, sessions_days: list[date], cfg: DayTradeConfig,
                 costs: Costs = Costs(), grid: list[Params] = GRID, account_equity: float = 100_000.0,
                 first_train_start: str = "2016-01-01", train_years: int = 2, test_years: int = 1,
                 progress: Callable[[str], None] = print) -> LabReport:
    from lumisafe.promotion.criteria import backtest_is_valid, backtest_to_paper

    runs: dict[Params, tuple[pd.Series, list[DayResult]]] = {}
    for i, p in enumerate(grid):
        runs[p] = run_period(sessions_days, cands_by_day, load_bars, p, cfg, costs, account_equity)
        progress(f"simulated {i + 1}/{len(grid)}: {p.label()}")

    index = pd.DatetimeIndex([pd.Timestamp(d) for d in sessions_days])
    spy_ret = spy_total.pct_change().reindex(index).fillna(0.0)
    spy_price_ret = spy_price.pct_change().reindex(index).fillna(0.0)
    folds, oos_parts, oos_results, chosen = [], [], [], []
    for tr_s, tr_e, te_s, te_e in folds_for(index, pd.Timestamp(first_train_start), train_years, test_years):
        best = max(grid, key=lambda p: (round(sharpe(runs[p][0].loc[tr_s:tr_e]), 6), -p.min_rel_volume))
        test = runs[best][0].loc[te_s:te_e]
        bench = spy_ret.loc[te_s:te_e]
        folds.append(FoldResult(str(tr_s.date()), str(tr_e.date()), str(te_s.date()), str(te_e.date()),
                                asdict(best), sharpe(runs[best][0].loc[tr_s:tr_e]), sharpe(test),
                                float((1 + test).prod() - 1), max_drawdown((1 + test).cumprod()),
                                sharpe(bench), float((1 + bench).prod() - 1), max_drawdown((1 + bench).cumprod())))
        oos_parts.append(test)
        oos_results += [r for r in runs[best][1] if te_s <= pd.Timestamp(r.day) <= te_e]
        chosen.append(best.label())

    oos = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
    bench_oos = spy_ret.reindex(oos.index).fillna(0.0)
    wf = WalkForwardReport(strategy=STRATEGY_NAME, benchmark="SPY",
                           created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           data_start=str(index[0].date()), data_end=str(index[-1].date()),
                           years=(index[-1] - index[0]).days / 365.25, cost_per_trade=costs.slippage,
                           folds=folds)
    if len(oos):
        wf.oos_sharpe = sharpe(oos)
        wf.oos_cagr = cagr(oos)
        wf.oos_max_drawdown = max_drawdown((1 + oos).cumprod())
        wf.benchmark_oos_sharpe = sharpe(bench_oos)
        wf.benchmark_oos_max_drawdown = max_drawdown((1 + bench_oos).cumprod())
        wf.folds_matching_benchmark = sum(f.matches_benchmark for f in folds) / len(folds)

    valid, promo = backtest_is_valid(wf), backtest_to_paper(wf)
    reasons = valid.reasons + promo.reasons
    summary = summarize(oos, bench_oos, spy_price_ret.reindex(oos.index).fillna(0.0), oos_results, cfg,
                        account_equity, chosen)
    return LabReport(wf, valid.passed and promo.passed, reasons, summary)


def summarize(oos: pd.Series, spy: pd.Series, spy_price: pd.Series, results: list[DayResult],
              cfg: DayTradeConfig, account_equity: float, chosen: list[str]) -> dict:
    trades = [t for r in results for t in r.trades]
    start_equity = cfg.slice_fraction * account_equity
    tax = yearly_tax(taxable_events(trades), cfg.short_term_tax_rate)
    after_tax = apply_taxes(oos, tax, start_equity) if len(oos) else oos
    years = len(oos) / 252 if len(oos) else 0.0
    # SPY in the same slice: dividends taxed yearly, gain taxed if sold at the end
    spy_div = (spy - spy_price).clip(lower=0)  # the dividend part of each day's total return
    spy_eq = start_equity * float((1 + spy).prod()) if len(spy) else start_equity
    held_before = start_equity * (1 + spy).cumprod().shift(1).fillna(1.0)
    div_tax = cfg.long_term_tax_rate * float((held_before * spy_div).sum())  # paid yearly, not reinvested
    spy_after_div_tax = spy_eq - div_tax
    spy_sold_after_tax = spy_after_div_tax - cfg.long_term_tax_rate * max(spy_after_div_tax - start_equity, 0.0)
    skipped = Counter()
    for r in results:
        skipped.update(r.skipped)
    gross = sum(t.gross_pnl for t in trades)
    end_pre = start_equity * float((1 + oos).prod()) if len(oos) else start_equity
    end_after = start_equity * float((1 + after_tax).prod()) if len(after_tax) else start_equity
    return {
        "test_period": f"{oos.index[0].date()} to {oos.index[-1].date()}" if len(oos) else None,
        "years": round(years, 2),
        "slice_start": round(start_equity, 2),
        "params_chosen_each_year": chosen,
        "trades": len(trades),
        "days_traded": sum(1 for r in results if r.trades),
        "days_in_test": len(oos),
        "exits": dict(Counter(t.exit_reason for t in trades)),
        "daily_loss_halts": sum(1 for r in results if r.halted),
        "partial_fills": sum(1 for t in trades if t.partial),
        "skipped": dict(skipped),
        "money": {
            "price_moves_before_costs": round(gross, 2),
            "bid_ask_spread": round(-sum(t.spread_cost for t in trades), 2),
            "slippage": round(-sum(t.slippage_cost for t in trades), 2),
            "regulatory_fees": round(-sum(t.fees for t in trades), 2),
            "net_before_tax": round(end_pre - start_equity, 2),
            "income_tax": round(-sum(tax.values()), 2),
            "net_after_tax": round(end_after - start_equity, 2),
        },
        "avg_net_per_trade": round(sum(t.net_pnl for t in trades) / len(trades), 2) if trades else 0.0,
        "end_value": {"day_trading_before_tax": round(end_pre, 2), "day_trading_after_tax": round(end_after, 2),
                      "spy_held_before_tax": round(spy_eq, 2),
                      "spy_held_after_dividend_tax": round(spy_after_div_tax, 2),
                      "spy_held_if_sold_at_end_after_tax": round(spy_sold_after_tax, 2)},
        "yearly_return": {str(y): round(float((1 + oos[oos.index.year == y]).prod() - 1), 4)
                          for y in sorted(set(oos.index.year))},
        "spy_yearly_return": {str(y): round(float((1 + spy[spy.index.year == y]).prod() - 1), 4)
                              for y in sorted(set(spy.index.year))},
        "tax_by_year": {str(y): round(v, 2) for y, v in tax.items()},
        "cagr_before_tax": round(cagr(oos), 4) if len(oos) else 0.0,
        "cagr_after_tax": round(cagr(after_tax), 4) if len(after_tax) else 0.0,
        "spy_cagr": round(cagr(spy), 4) if len(spy) else 0.0,
        "max_drawdown": round(max_drawdown((1 + oos).cumprod()), 4) if len(oos) else 0.0,
        "spy_max_drawdown": round(max_drawdown((1 + spy).cumprod()), 4) if len(spy) else 0.0,
        "assumptions": [
            f"Account ${account_equity:,.0f} ({cfg.account_type} account); day-trading slice "
            f"{cfg.slice_fraction:.0%}; whole shares; commission $0.",
            "Spreads: real NBBO at 09:45 per stock and day, used for entries and exits (morning spreads are "
            "wider, so this is conservative for afternoon exits).",
            f"Short-term tax rate {cfg.short_term_tax_rate:.0%}, long-term/qualified {cfg.long_term_tax_rate:.0%}.",
            "Stock pool chosen in 2026; ranked daily by trailing dollar volume (some survivorship bias remains).",
            "Headlines only count if published before 09:44 New York time; the AI veto is NOT in this test "
            "(the model has seen these years, so it can only be judged on live paper trading).",
            "Idle cash earns nothing in either the strategy or the comparison.",
        ],
    }


def format_report(d: dict) -> str:
    """Plain-English summary of a saved lab report (``LabReport.to_dict()``)."""
    s, wf = d["summary"], d["walk_forward"]
    m, ev = s["money"], s["end_value"]
    lines = [
        f"Day-trading lab: {s['test_period']} ({s['years']} years tested, each year tuned only on the 2 before it)",
        f"Started with ${s['slice_start']:,.0f} (the day-trading slice of a ${s['slice_start'] / 0.10:,.0f} account)"
        if abs(s["slice_start"] - 10_000) < 1 else f"Started with ${s['slice_start']:,.0f}",
        "",
        f"  Trades: {s['trades']} on {s['days_traded']} of {s['days_in_test']} market days · "
        f"daily loss stops hit: {s['daily_loss_halts']}",
        f"  Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(s["exits"].items())),
        "",
        "Where the money went:",
        f"  Price moves you caught         ${m['price_moves_before_costs']:>12,.2f}",
        f"  Bid/ask spread                 ${m['bid_ask_spread']:>12,.2f}",
        f"  Slippage                       ${m['slippage']:>12,.2f}",
        f"  Regulatory fees (SEC, FINRA)   ${m['regulatory_fees']:>12,.2f}",
        f"  = Result before tax            ${m['net_before_tax']:>12,.2f}",
        f"  Income tax (short-term rate)   ${m['income_tax']:>12,.2f}"
        + ("   (a net loss lowers your other taxes)" if m["income_tax"] > 0 else ""),
        f"  = Result after tax             ${m['net_after_tax']:>12,.2f}",
        f"  Average per trade, before tax  ${s['avg_net_per_trade']:>12,.2f}",
        "",
        "Same money, same years, just holding SPY:",
        f"  Day trading  ${ev['day_trading_before_tax']:,.0f} before tax · ${ev['day_trading_after_tax']:,.0f} after tax"
        f" · {s['cagr_before_tax']:+.1%}/yr before tax, {s['cagr_after_tax']:+.1%} after"
        f" · worst drop {s['max_drawdown']:.1%}",
        f"  Holding SPY  ${ev['spy_held_before_tax']:,.0f} before tax · ${ev['spy_held_if_sold_at_end_after_tax']:,.0f}"
        f" after tax even if sold at the end · {s['spy_cagr']:+.1%}/yr · worst drop {s['spy_max_drawdown']:.1%}",
        "",
        "Year by year (day trading vs SPY):",
    ]
    for y, r in s["yearly_return"].items():
        lines.append(f"  {y}: {r:+7.1%}   vs   {s['spy_yearly_return'].get(y, 0.0):+7.1%}")
    lines += ["", f"Risk-adjusted (Sharpe): {wf['oos_sharpe']:.2f} vs SPY {wf['benchmark_oos_sharpe']:.2f}; "
                  f"beat SPY in {wf['folds_matching_benchmark']:.0%} of test years.",
              "Verdict: " + ("PASSED the promotion bar, so it may run on paper." if d["passed"] else
                             "did NOT pass the promotion bar: " + "; ".join(d["reasons"]) + "."),
              "", "Assumptions:"] + [f"  - {a}" for a in s["assumptions"]]
    return "\n".join(lines)
