"""Factory bookkeeping: frozen decisions → virtual books → leaderboard and "copy now" list.

* Decisions are appended, never edited. A rebalance bot's decision is stored only when its targets
  change; an event bot's picks are stored when made. Missing a day just means "no change".
* Books are priced by ``zoo.simulate`` (next-open execution, real spreads, 0.05% slippage, idle cash
  earns BIL). Event picks hold ``EVENT_SLOT`` of the book from the entry open until the open after
  their exit day (the engine trades at opens).
* Only days AFTER a bot's first decision count. There is no backfilled history: the forward record
  starts the day the factory starts.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from lumisafe import paths
from lumisafe.factory.bots import EVENT_SLOT, Bot, Decision
from lumisafe.signals import zoo

MIN_DAYS = 60  # SPEC §8: 60 trading days of paper before anything counts as evidence


def folder() -> Path:
    d = paths.home() / "factory"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or folder() / "decisions.jsonl"
        self.decisions: list[Decision] = []
        if self.path.is_file():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self.decisions.append(Decision(**json.loads(line)))

    def for_bot(self, bot: str) -> list[Decision]:
        return [d for d in self.decisions if d.bot == bot]

    def last_weights(self, bot: str) -> dict[str, float] | None:
        for d in reversed(self.for_bot(bot)):
            if d.weights is not None:
                return d.weights
        return None

    def decided(self, bot: str, day: str) -> bool:
        return any(d.day == day for d in self.for_bot(bot))

    def append(self, d: Decision) -> None:
        self.decisions.append(d)
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(d)) + "\n")

    def started(self, bot: str) -> str | None:
        ds = self.for_bot(bot)
        return ds[0].day if ds else None


def record(ledger: Ledger, bot: Bot, d: Decision) -> bool:
    """Store a decision if it says something new. Returns True when something was stored."""
    if ledger.decided(bot.name, d.day):
        return False
    first = ledger.started(bot.name) is None
    if d.weights is not None:
        prev = ledger.last_weights(bot.name)
        if prev is not None and _same(prev, d.weights) and not first:
            return False
        ledger.append(d)
        return True
    if d.picks or first:  # first decision anchors the start date even if empty
        ledger.append(d)
        return True
    return False


def _same(a: dict, b: dict, tol: float = 1e-6) -> bool:
    keys = set(a) | set(b)
    return all(abs(a.get(k, 0.0) - b.get(k, 0.0)) < tol for k in keys)


def weights_frame(ledger: Ledger, bot: Bot, data: zoo.Data) -> pd.DataFrame:
    days = data.days
    W = pd.DataFrame(0.0, index=days, columns=data.C.columns)
    ds = ledger.for_bot(bot.name)
    if not ds:
        return W
    if bot.rebalance != "event":
        sparse = pd.DataFrame(np.nan, index=days, columns=data.C.columns)
        any_set = False
        for d in ds:
            if d.weights is None:
                continue
            t = pd.Timestamp(d.day)
            pos = days.searchsorted(t)
            if pos >= len(days):
                continue
            row = pd.Series(0.0, index=data.C.columns)
            for s, x in d.weights.items():
                if s in row.index:
                    row[s] = x
            sparse.iloc[pos] = row.values
            any_set = True
        return sparse.ffill().fillna(0.0) if any_set else W
    for d in ds:
        for p in d.picks:
            s = p["symbol"]
            if s not in W.columns:
                continue
            e, x = pd.Timestamp(p["entry_day"]), pd.Timestamp(p["exit_day"])
            i0 = days.searchsorted(e) - 1  # decided the close before entry
            i1 = days.searchsorted(x) - 1
            if i0 < 0:
                continue
            W.iloc[i0:max(i0 + 1, i1 + 1), W.columns.get_loc(s)] += EVENT_SLOT
    total = W.sum(axis=1)
    over = total > 1.0
    W.loc[over] = W.loc[over].div(total[over], axis=0)  # never more than 100% invested
    return W


def forward_returns(ledger: Ledger, bot: Bot, data: zoo.Data, spread_fn) -> pd.Series:
    start = ledger.started(bot.name)
    if start is None:
        return pd.Series(dtype=float)
    r = zoo.simulate(weights_frame(ledger, bot, data), data, spread_fn)
    return r[r.index > pd.Timestamp(start)]


def metrics(r: pd.Series, bench: pd.Series) -> dict:
    n = len(r)
    if n == 0:
        return {"days": 0}
    b = bench.reindex(r.index).fillna(0.0)
    tot = float((1 + r).prod() - 1)
    out = {"days": n, "total": tot, "bench_total": float((1 + b).prod() - 1),
           "last_5d": float((1 + r.tail(5)).prod() - 1), "last_20d": float((1 + r.tail(20)).prod() - 1),
           "bench_5d": float((1 + b.tail(5)).prod() - 1), "bench_20d": float((1 + b.tail(20)).prod() - 1),
           "max_dd": zoo.max_dd(r), "sharpe": zoo.sharpe(r) if n >= 20 else None,
           "annual": zoo.ann_return(r) if n >= MIN_DAYS else None,
           "bench_annual": zoo.ann_return(b) if n >= MIN_DAYS else None}
    out["excess"] = out["total"] - out["bench_total"]
    return out


def verdicts(results: dict[str, dict], excess: dict[str, pd.Series]) -> dict[str, str]:
    """TOO EARLY before 60 days; then PROVEN only if its excess beats luck across all bots (deflated Sharpe)."""
    srs = [float(x.mean() / x.std()) for x in excess.values() if len(x) >= 20 and x.std() > 0]
    out = {}
    for name, m in results.items():
        if m.get("days", 0) < MIN_DAYS:
            out[name] = f"TOO EARLY ({m.get('days', 0)}/{MIN_DAYS} days)"
            continue
        dsr = zoo.deflated_sharpe(excess[name], srs or [0.0])
        if dsr >= 0.95 and m["excess"] > 0:
            out[name] = f"PROVEN (deflated Sharpe {dsr:.2f})"
        elif m["excess"] > 0:
            out[name] = f"AHEAD, NOT PROVEN (deflated Sharpe {dsr:.2f})"
        else:
            out[name] = "BEHIND ITS BENCHMARK"
    return out


def holdings_now(ledger: Ledger, bot: Bot, today: date) -> dict[str, float]:
    """What the book should hold from the next open (the thing to copy)."""
    if bot.rebalance != "event":
        return ledger.last_weights(bot.name) or {}
    w: dict[str, float] = {}
    for d in ledger.for_bot(bot.name):
        for p in d.picks:
            if p["exit_day"] >= today.isoformat():
                w[p["symbol"]] = w.get(p["symbol"], 0.0) + EVENT_SLOT
    return w


def changes(ledger: Ledger, bot: Bot, day: str) -> list[str]:
    """Plain-English orders implied by decisions made on ``day`` (for the copy list)."""
    out = []
    ds = [d for d in ledger.for_bot(bot.name) if d.day == day]
    if not ds:
        return out
    d = ds[-1]
    if d.weights is not None:
        before = {}
        for x in ledger.for_bot(bot.name):
            if x is d:
                break
            if x.weights is not None:
                before = x.weights
        for s in sorted(set(before) | set(d.weights)):
            a, b = before.get(s, 0.0), d.weights.get(s, 0.0)
            if b > a + 1e-6:
                out.append(f"BUY {s} → {b:.0%} of the book")
            elif a > b + 1e-6:
                out.append(f"SELL {s} → {b:.0%} of the book" if b > 0 else f"SELL all {s}")
    for p in d.picks:
        out.append(f"BUY {p['symbol']} ({EVENT_SLOT:.0%}) at the {p['entry_day']} open, sell after the "
                   f"{p['exit_day']} close · {p['why'][:120]}")
    return out


def live_today(weights: dict[str, float], last: dict[str, float], prev_close: dict[str, float]) -> float | None:
    """Estimated return so far today for a book holding ``weights`` (from the latest prices)."""
    if not weights:
        return 0.0
    tot, have = 0.0, 0.0
    for s, w in weights.items():
        if s in last and s in prev_close and prev_close[s] > 0:
            tot += w * (last[s] / prev_close[s] - 1)
            have += w
    return tot if have > 0 else None


def write_state(state: dict) -> Path:
    p = folder() / "state.json"
    tmp = p.with_suffix(".tmp")

    def clean(o):
        if isinstance(o, float) and not math.isfinite(o):
            return None
        return o

    tmp.write_text(json.dumps(state, indent=1, default=lambda o: clean(o) if isinstance(o, float) else str(o)))
    tmp.replace(p)
    return p


def read_state() -> dict | None:
    p = folder() / "state.json"
    return json.loads(p.read_text()) if p.is_file() else None
