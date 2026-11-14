"""Today's shortlist: what the signals that passed the study point to right now (read-only, no orders)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from lumisafe.daytrade.universe import POOL, sessions
from lumisafe.signals import study
from lumisafe.signals.events import parse_all
from lumisafe.signals.run import NAMES, universe_fn

NY = ZoneInfo("America/New_York")
AVOID = ("earnings_miss", "downgrade", "guide_down")


@dataclass
class Idea:
    signal: str
    symbol: str
    buy_at_open_on: date
    sell_at_close_on: date
    why: str
    hold: int
    history: dict  # the signal's 2022-2025 (never-seen) stats


def scan(live, picks: dict[str, int], pick_stats: dict[str, dict], now: datetime) -> tuple[list[Idea], list[Idea]]:
    """(ideas, avoid list) for the next market open, from live data available at ``now``."""
    start = (now - timedelta(days=420)).date()
    sess = sessions(start, now.date() + timedelta(days=10))
    days = [s.day for s in sess]
    opens = {s.day: s.open for s in sess}
    nxt = next((s.day for s in sess if s.open > now), None)
    if nxt is None:
        return [], []
    bars = live.recent_daily(list(POOL) + ["SPY"], days=420, adjustment="all")
    today = next((s for s in sess if s.day == now.date()), None)
    if today is not None and now < today.close:  # today's bar is still forming: use completed days only
        bars = {k: v[v.index < pd.Timestamp(today.day)] for k, v in bars.items()}
    completed = [d for d in days if d < now.date() or (today is not None and d == today.day and now >= today.close)]
    known_days = completed + [nxt]
    mkt = study.Market(bars, known_days)
    items = live.news(list(POOL), now - timedelta(hours=40), now)
    events = parse_all(items)
    uni = universe_fn(bars, known_days)
    entries = study.generate(events, mkt, lambda d: opens[d], lambda d: uni(d) or uni(completed[-1]))
    ideas, avoid, seen = [], [], set()
    for e in entries:
        if e.entry_day != nxt or (e.signal, e.symbol) in seen:
            continue
        seen.add((e.signal, e.symbol))
        if e.signal in picks:
            h = picks[e.signal]
            sell = days[days.index(nxt) + h - 1] if nxt in days and days.index(nxt) + h - 1 < len(days) else None
            ideas.append(Idea(e.signal, e.symbol, nxt, sell or nxt + timedelta(days=int(h * 1.4)), e.why, h,
                              pick_stats.get(e.signal, {})))
        elif e.signal in AVOID:
            avoid.append(Idea(e.signal, e.symbol, nxt, nxt, e.why, 0, {}))
    order = list(picks)
    ideas.sort(key=lambda i: order.index(i.signal))
    merged: dict[str, Idea] = {}
    for i in ideas:  # one line per stock; if both signals fire, keep both reasons and the longer hold
        if i.symbol not in merged:
            merged[i.symbol] = i
        else:
            m = merged[i.symbol]
            longer = i if i.hold > m.hold else m
            merged[i.symbol] = Idea(m.signal + "+" + i.signal, i.symbol, m.buy_at_open_on, longer.sell_at_close_on,
                                    m.why if m.why == i.why else f"{m.why} | {i.why}", longer.hold,
                                    longer.history)
    return list(merged.values()), avoid


def format_today(ideas: list[Idea], avoid: list[Idea], picks: dict[str, int], slice_value: float | None) -> str:
    if not picks:
        return ("No signal has passed the study, so there is nothing to suggest. That is the honest answer: "
                "none of the tested news or price patterns beat simply holding SPY after costs. "
                "See `lumisafe signals report`.")
    lines = []
    if not ideas:
        lines.append("Nothing today: none of the proven signals fired for the next open. Most days are like this.")
    for i in ideas:
        h = i.history
        stat = (f"since 2022 (data it was never tuned on): {h.get('trades', 0)} trades, "
                f"{h.get('hit_rate', 0):.0%} ended positive after costs, average {h.get('mean_net', 0):+.2%} "
                f"({h.get('mean_excess', 0):+.2%} vs SPY)") if h else "no history"
        size = f" · about ${0.10 * slice_value:,.0f} (10% of your satellite slice)" if slice_value else ""
        lines += [f"BUY {i.symbol} at the open on {i.buy_at_open_on:%a %b %-d}, sell at the close on "
                  f"{i.sell_at_close_on:%a %b %-d} ({i.hold} trading days){size}",
                  f"    signal: {' + '.join(NAMES.get(s, s) for s in i.signal.split('+'))}",
                  f"    why: {i.why[:140]}",
                  f"    track record: {stat}"]
    if avoid:
        lines += ["", "Avoid for now (bad news since yesterday): " +
                  ", ".join(sorted({f"{a.symbol} ({NAMES.get(a.signal, a.signal).split(' (')[0].lower()})"
                                    for a in avoid}))]
    lines += ["", "These are historical tendencies, not predictions; any single trade can lose. "
                  "Never put more than 10% of the satellite slice in one idea."]
    return "\n".join(lines)
