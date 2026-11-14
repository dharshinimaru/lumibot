"""Forward test: every decision is written down *before* the trade, then scored on real prices.

This is what makes the signals checkable. A decision gets a timestamp the moment the
scan makes it (the evening before, or before 09:30 on the day), so nothing can be
picked or dropped after the result is known. The scorecard then grades every decision:

* **market result**: buy at the real opening price on the entry day, sell at the real
  closing price on the exit day, minus the quoted spread and 0.05% slippage per side.
  This is exactly what you'd have got copying the decision, whether or not the paper
  account traded it (the 5-trades-a-week limit can block a paper order; the decision
  still counts, so a bad idea can't hide behind a blocked order).
* **paper result**: what the Alpaca paper account actually filled, when it traded.
* **vs SPY**: the same days held in SPY instead.

Pure functions plus a small JSON file under ``~/.lumisafe``; no network here.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import mean

import pandas as pd

from lumisafe import paths

SLIPPAGE = 0.0005  # per side, SPEC §8 cost assumption
DEFAULT_SPREAD = 0.001  # used only when no live quote was available at decision time
MIN_DAYS_FOR_VERDICT = 60  # SPEC §8: PAPER → LIVE_ELIGIBLE needs ≥ 60 trading days


@dataclass
class Decision:
    symbol: str
    signal: str
    why: str
    decided_at: str  # ISO timestamp, always before entry_day's open
    entry_day: str  # buy at this day's open
    exit_day: str  # sell at this day's close
    hold: int
    spread: float | None = None  # quoted spread (fraction of price) when decided
    status: str = "planned"  # planned → ordered → holding → selling → closed; or blocked / missed
    note: str = ""
    qty: float = 0.0
    buy_order: str | None = None
    buy_fill: float | None = None
    bought_at: str | None = None
    sell_order: str | None = None
    sell_fill: float | None = None
    sold_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.entry_day}"


@dataclass
class Ledger:
    decisions: list[Decision] = field(default_factory=list)

    # -- persistence ----------------------------------------------------------------------------

    @staticmethod
    def path() -> Path:
        return paths.home() / "signals_forward.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Ledger":
        p = path or cls.path()
        if not p.is_file():
            return cls()
        raw = json.loads(p.read_text())
        return cls([Decision(**d) for d in raw.get("decisions", [])])

    def save(self, path: Path | None = None) -> None:
        p = path or self.path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"decisions": [asdict(d) for d in self.decisions]}, indent=1))
        tmp.replace(p)

    # -- edits ----------------------------------------------------------------------------------

    def get(self, key: str) -> Decision | None:
        return next((d for d in self.decisions if d.key == key), None)

    def add(self, ideas, decided_at: datetime, spread_fn=None) -> list[Decision]:
        """Record new ideas (``signals.today.Idea``). Same stock + same entry day = same decision."""
        new = []
        for i in ideas:
            d = Decision(i.symbol, i.signal, i.why, decided_at.isoformat(timespec="seconds"),
                         i.buy_at_open_on.isoformat(), i.sell_at_close_on.isoformat(), int(i.hold))
            if self.get(d.key) is not None:
                continue
            if spread_fn is not None:
                try:
                    d.spread = spread_fn(i.symbol)
                except Exception:  # a missing quote only means we fall back to the default spread
                    d.spread = None
            self.decisions.append(d)
            new.append(d)
        return new

    def by_order(self, order_id: str) -> tuple[Decision, str] | None:
        for d in self.decisions:
            if d.buy_order == order_id:
                return d, "buy"
            if d.sell_order == order_id:
                return d, "sell"
        return None

    def to_buy(self, day: date) -> list[Decision]:
        return [d for d in self.decisions if d.entry_day == day.isoformat() and d.status == "planned"]

    def to_sell(self, day: date) -> list[Decision]:
        return [d for d in self.decisions if d.exit_day <= day.isoformat() and d.status == "holding"]

    def open_positions(self) -> list[Decision]:
        return [d for d in self.decisions if d.status in ("holding", "selling") and d.qty > 0]


# --- scoring ---------------------------------------------------------------------------------------


@dataclass
class Graded:
    d: Decision
    finished: bool  # the exit day's close is known
    market: float | None  # net return copying the decision (open → close, after costs)
    spy: float | None  # SPY over the same days
    paper: float | None  # the paper account's actual fills, after nothing (fills already include spread)


def _price(bars: dict[str, pd.DataFrame], sym: str, day: str, col: str) -> float | None:
    df = bars.get(sym)
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(day)
    if ts not in df.index:
        return None
    v = float(df.loc[ts, col])
    return v if math.isfinite(v) and v > 0 else None


def _last_close(bars, sym: str, upto: str) -> tuple[float | None, str | None]:
    df = bars.get(sym)
    if df is None or df.empty:
        return None, None
    df = df[df.index <= pd.Timestamp(upto)]
    if df.empty:
        return None, None
    return float(df["close"].iloc[-1]), df.index[-1].date().isoformat()


def grade(ledger: Ledger, bars: dict[str, pd.DataFrame], today: date) -> list[Graded]:
    """Grade every decision whose entry open has happened. Unfinished ones are marked to the last close.

    ``bars``: daily bars (dividend-adjusted is fine; returns are ratios) incl. SPY, as of ``today``.
    Missing bars mean "not graded", never a made-up price.
    """
    out = []
    for d in ledger.decisions:
        if d.entry_day > today.isoformat() or d.status == "missed":
            continue
        buy = _price(bars, d.symbol, d.entry_day, "open")
        spy_buy = _price(bars, "SPY", d.entry_day, "open")
        finished = d.exit_day <= today.isoformat() and _price(bars, d.symbol, d.exit_day, "close") is not None
        if finished:
            sell, spy_sell = _price(bars, d.symbol, d.exit_day, "close"), _price(bars, "SPY", d.exit_day, "close")
        else:
            sell, _ = _last_close(bars, d.symbol, today.isoformat())
            spy_sell, _ = _last_close(bars, "SPY", today.isoformat())
        cost = (d.spread if d.spread is not None else DEFAULT_SPREAD) + 2 * SLIPPAGE
        market = (sell / buy - 1 - cost) if (buy and sell) else None
        spy = (spy_sell / spy_buy - 1) if (spy_buy and spy_sell) else None
        paper = (d.sell_fill / d.buy_fill - 1) if (d.buy_fill and d.sell_fill) else None
        out.append(Graded(d, finished, market, spy, paper))
    return out


def summary(graded: list[Graded]) -> dict:
    done = [g for g in graded if g.finished and g.market is not None]
    vs = [g.market - g.spy for g in done if g.spy is not None]
    paper = [g.paper for g in graded if g.paper is not None]
    days = sorted({g.d.entry_day for g in graded})
    return {
        "decisions": len(graded), "finished": len(done),
        "win_rate": (sum(g.market > 0 for g in done) / len(done)) if done else None,
        "avg": mean(g.market for g in done) if done else None,
        "avg_vs_spy": mean(vs) if vs else None,
        "beat_spy_rate": (sum(v > 0 for v in vs) / len(vs)) if vs else None,
        "paper_trades": len(paper), "paper_avg": mean(paper) if paper else None,
        "first_day": days[0] if days else None,
    }


def format_scorecard(graded: list[Graded], expected: dict | None, trading_days_running: int) -> str:
    s = summary(graded)
    lines = []
    if not graded:
        return ("No decisions graded yet. The forward test starts recording when `lumisafe signals autopilot` "
                "runs; most days there are no signals, so the first decision can take a while.")
    lines.append(f"Forward test since {s['first_day']} ({trading_days_running} trading days so far)")
    lines.append(f"  decisions: {s['decisions']} · finished: {s['finished']}")
    if s["finished"]:
        lines.append(f"  copying every decision: {s['win_rate']:.0%} made money, average {s['avg']:+.2%} per trade "
                     f"after costs, {s['avg_vs_spy']:+.2%} vs holding SPY the same days "
                     f"(beat SPY {s['beat_spy_rate']:.0%} of the time)")
    if s["paper_trades"]:
        lines.append(f"  paper account fills: {s['paper_trades']} round trips, average {s['paper_avg']:+.2%}")
    if expected:
        lines.append(f"  the study expected: {expected.get('hit_rate', 0):.0%} winners, average "
                     f"{expected.get('mean_net', 0):+.2%} ({expected.get('mean_excess', 0):+.2%} vs SPY)")
    lines.append("")
    lines.append(verdict(s, expected, trading_days_running))
    lines.append("")
    lines.append("Every decision (newest first):")
    for g in sorted(graded, key=lambda g: (g.d.entry_day, g.d.symbol), reverse=True):
        d = g.d
        res = "n/a" if g.market is None else f"{g.market:+.2%}"
        spy = "" if g.spy is None or g.market is None else f" (SPY {g.spy:+.2%})"
        state = "done" if g.finished else "open, marked to last close"
        paper = f" · paper {d.status}" + (f" {d.qty:g} sh" if d.qty else "") + (f": {d.note}" if d.note else "")
        lines.append(f"  {d.symbol:<5} bought {d.entry_day} open → sell {d.exit_day} close: {res}{spy} [{state}]"
                     f"{paper}")
    return "\n".join(lines)


def verdict(s: dict, expected: dict | None, days: int) -> str:
    """Plain-English answer to "can I trust this yet?" Never says yes before SPEC §8's 60 trading days."""
    n = s["finished"]
    if days < MIN_DAYS_FOR_VERDICT or n < 20:
        return (f"Verdict: TOO EARLY. {days} of {MIN_DAYS_FOR_VERDICT} trading days and {n} finished trades "
                "(need 20+). A few days of results is mostly luck, good or bad; don't scale up on it.")
    exp = (expected or {}).get("mean_excess", 0.0)
    if s["avg_vs_spy"] is not None and s["avg_vs_spy"] >= 0.5 * exp and s["avg_vs_spy"] > 0:
        return ("Verdict: HOLDING UP. After 60+ trading days the decisions beat SPY by at least half of what the "
                "study expected. This is the point where copying with small real money is a reasonable choice.")
    return ("Verdict: NOT HOLDING UP. The live decisions are doing worse than half of what the study expected. "
            "Don't copy them with real money; the index-fund plan is the better default.")
