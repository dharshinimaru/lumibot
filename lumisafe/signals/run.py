"""Run the pre-registered signal study on cached real data and write the report."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from lumisafe import paths
from lumisafe.daytrade.universe import POOL, sessions
from lumisafe.promotion.walk_forward import FoldResult, WalkForwardReport
from lumisafe.reporting.benchmark import max_drawdown, sharpe
from lumisafe.signals import study
from lumisafe.signals.events import parse_all

STRATEGY_NAME = "news_signals"
DISCOVERY_END = date(2021, 12, 31)


def load_events(cache: Path, start: date, end: date) -> list:
    items = []
    for f in sorted((cache / "news_full").glob("*.json")):
        d = date.fromisoformat(f.stem)
        if start <= d <= end:
            items += json.loads(f.read_text())["items"]
    return parse_all(items)


def universe_fn(bars: dict[str, pd.DataFrame], days: list[date], top_n: int = 100) -> Callable[[date], set[str]]:
    """Top ``top_n`` by trailing 20-session dollar volume, using only sessions BEFORE the day."""
    dv = pd.DataFrame({s: (b["close"] * b["volume"]) for s, b in bars.items() if s != "SPY" and len(b)})
    trailing = dv.sort_index().rolling(20, min_periods=20).mean().shift(1)
    cache: dict[date, set[str]] = {}
    for d in days:
        ts = pd.Timestamp(d)
        if ts in trailing.index:
            row = trailing.loc[ts].dropna()
            cache[d] = set(row.nlargest(top_n).index)
    return lambda d: cache.get(d, set())


def run(md, start: date = date(2016, 1, 4), end: date = date(2025, 12, 31),
        progress: Callable[[str], None] = print) -> dict:
    sess = sessions(start, end)
    days = [s.day for s in sess]
    opens = {s.day: s.open for s in sess}
    bars = md.daily_bars(list(POOL) + ["SPY"], start, end, adjustment="all")
    mkt = study.Market(bars, days)
    progress("parsing headlines...")
    events = load_events(md.cache, start, end)
    progress(f"{len(events)} single-company events from headlines")
    uni = universe_fn(bars, days)
    entries = study.generate(events, mkt, lambda d: opens[d], uni)
    progress(f"{len(entries)} signal entries")
    spread_samples = json.loads((md.cache / "spreads.json").read_text()) if (md.cache / "spreads.json").is_file() else {}
    trades = study.evaluate(entries, mkt, study.spread_model(spread_samples))
    progress(f"{len(trades)} trade results (all signals x holding periods)")

    disc = [t for t in trades if t.entry_day <= DISCOVERY_END]
    hold = [t for t in trades if t.entry_day > DISCOVERY_END]
    table = {"discovery_2016_2021": [asdict(s) for s in study.table(disc, "2016-2021")],
             "holdout_2022_2025": [asdict(s) for s in study.table(hold, "2022-2025")]}

    # walk-forward portfolio 2018-2025, selection on trades closed before each year
    spy = bars["SPY"]["close"]
    spy_ret = spy.pct_change()
    parts, folds, picks = [], [], {}
    for y in range(2018, end.year + 1):
        ys, ye = date(y, 1, 1), date(y, 12, 31)
        chosen = study.select(trades, before=ys)
        picks[str(y)] = chosen
        r, _ = study.portfolio_returns(trades, chosen, mkt, ys, min(ye, end)) if chosen else (
            pd.Series(0.0, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days if ys <= d <= ye])), [])
        b = spy_ret.reindex(r.index).fillna(0.0)
        tr, _ = study.portfolio_returns(trades, chosen, mkt, date(2016, 1, 4), date(y - 1, 12, 31)) if chosen else (
            pd.Series(dtype=float), [])
        folds.append(FoldResult("2016-01-04", f"{y - 1}-12-31", str(ys), str(ye), {"signals": chosen},
                                sharpe(tr) if len(tr) else 0.0, sharpe(r), float((1 + r).prod() - 1),
                                max_drawdown((1 + r).cumprod()), sharpe(b), float((1 + b).prod() - 1),
                                max_drawdown((1 + b).cumprod())))
        parts.append(r)
        progress(f"{y}: {len(chosen)} signals selected → {float((1 + r).prod() - 1):+.1%} vs SPY "
                 f"{float((1 + b).prod() - 1):+.1%}")
    oos = pd.concat(parts)
    bench = spy_ret.reindex(oos.index).fillna(0.0)
    wf = WalkForwardReport(STRATEGY_NAME, "SPY", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           str(start), str(end), (end - start).days / 365.25, study.SLIPPAGE, folds,
                           sharpe(oos), _cagr(oos), max_drawdown((1 + oos).cumprod()), sharpe(bench),
                           max_drawdown((1 + bench).cumprod()),
                           sum(f.matches_benchmark for f in folds) / len(folds))
    from lumisafe.promotion.criteria import backtest_is_valid, backtest_to_paper

    v, p = backtest_is_valid(wf), backtest_to_paper(wf)
    return {"walk_forward": wf.to_dict(), "passed": v.passed and p.passed, "reasons": v.reasons + p.reasons,
            "table": table, "picks_by_year": picks, "events": len(events), "entries": len(entries),
            "oos_cagr": _cagr(oos), "spy_cagr": _cagr(bench)}


def _cagr(r: pd.Series) -> float:
    if r.empty:
        return 0.0
    total = float((1 + r).prod())
    return total ** (252 / len(r)) - 1 if total > 0 else -1.0


NAMES = {
    "earnings_beat_strong": "Big earnings beat (EPS +5%, sales beat)",
    "earnings_beat": "Any earnings beat",
    "upgrade_bullish": "Analyst upgrade to Buy",
    "initiate_bullish": "New coverage at Buy",
    "pt_raise_cluster": "2+ price-target raises in a day",
    "guide_up": "Company raises guidance",
    "beat_gap_held": "Earnings beat, gap up held (next day)",
    "upgrade_gap_held": "Upgrade, +2% day (next day)",
    "news_gap_held": "News gap up 3%+ held (next day)",
    "new_high_volume": "1-year high on 2x volume",
    "pullback_uptrend": "7% dip in an uptrend",
    "gap_down_no_news": "4% gap down, no news",
    "rel_strength": "Top-10 6-month strength (monthly)",
    "earnings_miss": "Earnings miss (avoid list)",
    "downgrade": "Analyst downgrade (avoid list)",
    "guide_down": "Guidance cut (avoid list)",
}


def format_study(d: dict) -> str:
    disc = {(s["signal"], s["hold"]): s for s in d["table"]["discovery_2016_2021"]}
    hold = {(s["signal"], s["hold"]): s for s in d["table"]["holdout_2022_2025"]}
    lines = [f"Signal study on real data: {d['events']:,} company events from headlines, {d['entries']:,} signal "
             "entries, every result after spread, slippage and fees.",
             "Each row: trades · share positive after costs · average result after costs · vs SPY same days "
             "(t = how unlikely by luck; 2+ is meaningful)", ""]
    hdr = f"  {'signal':<42}{'hold':>5}  {'2016-2021 (found on)':<36}{'2022-2025 (never seen)':<36}"
    lines += [hdr, "  " + "-" * (len(hdr) - 2)]

    def cell(s):
        if not s or not s["trades"]:
            return f"{'-':<36}"
        return f"{s['trades']:>5} · {s['hit_rate']:>4.0%} · {s['mean_net']:+6.2%} · {s['mean_excess']:+6.2%} t{s['t_excess']:+.1f}  "

    for sig in study.SIGNALS:
        for h in (5, 10, 20):
            lines.append(f"  {NAMES.get(sig, sig):<42}{h:>4}d  {cell(disc.get((sig, h)))}{cell(hold.get((sig, h)))}")
    lines += ["", "Walk-forward (each year trades only the signals that had passed on earlier years):"]
    for f in d["walk_forward"]["folds"]:
        sigs = f["chosen_params"].get("signals", {})
        names = ", ".join(f"{NAMES.get(s, s)} ({h}d)" for s, h in sigs.items()) or "none passed → stay in cash"
        lines.append(f"  {f['test_start'][:4]}: {f['test_return']:+6.1%} vs SPY {f['benchmark_return']:+6.1%} · {names}")
    wf = d["walk_forward"]
    lines += ["", f"Overall 2018-2025: {d['oos_cagr']:+.1%}/yr vs SPY {d['spy_cagr']:+.1%}/yr · Sharpe "
                  f"{wf['oos_sharpe']:.2f} vs {wf['benchmark_oos_sharpe']:.2f} · worst drop {wf['oos_max_drawdown']:.1%} "
                  f"vs {wf['benchmark_oos_max_drawdown']:.1%}",
              "Verdict: " + ("PASSED; it may run on paper." if d["passed"] else
                             "did NOT pass: " + "; ".join(d["reasons"]) + ".")]
    return "\n".join(lines)
