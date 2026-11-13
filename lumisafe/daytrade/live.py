"""Paper day trading: today's candidates from live data, the day's book, and the lab's own limits.

No broker calls here (those live in ``runner.py`` behind the Pipeline). The
setup, filters and sizing are the SAME code the walk-forward test used
(``sim.prepare`` and ``sim.size_position``), with the parameters the most
recent test picked. Today's data is never cached.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from lumisafe.config.schema import DayTradeConfig
from lumisafe.daytrade.dataset import (
    DATASET_MIN_GAP, MIN_PRICE, NEWS_DELAY_SECONDS, OPENING_RANGE_MINUTES, Candidate,
)
from lumisafe.daytrade.sim import PDT_EQUITY_MIN, PDT_MAX_DAY_TRADES, Params
from lumisafe.daytrade.universe import POOL, Session, dollar_volume_rank


@dataclass
class LivePosition:
    symbol: str
    shares: int
    entry: float  # expected fill (decision price + half spread); replaced by the real fill when known
    stop: float
    target: float
    order_id: str
    entry_ts: str
    filled: bool = False


@dataclass
class DayBook:
    """Today's day-trading state (persisted so a restart mid-day keeps every limit)."""

    day: str
    start_equity: float  # the slice at the start of the day
    realized: float = 0.0
    trades: int = 0  # entries submitted today (a day trade each)
    attempted: list[str] = field(default_factory=list)
    positions: dict[str, LivePosition] = field(default_factory=dict)
    halted: bool = False
    flattened: bool = False
    recent_day_trades: list[int] = field(default_factory=list)  # previous sessions' counts, newest last

    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = {k: asdict(v) for k, v in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DayBook":
        d = dict(d)
        d["positions"] = {k: LivePosition(**v) for k, v in d.get("positions", {}).items()}
        return cls(**d)

    @classmethod
    def for_today(cls, saved: dict | None, today: date, slice_equity: float) -> "DayBook":
        """Carry over the PDT history; start a fresh book on a new day."""
        if saved and saved.get("day") == today.isoformat():
            return cls.from_dict(saved)
        history = list(saved.get("recent_day_trades", [])) if saved else []
        if saved:
            history = (history + [int(saved.get("trades", 0))])[-4:]
        return cls(day=today.isoformat(), start_equity=slice_equity, recent_day_trades=history)

    def open_value(self, prices: dict[str, float]) -> float:
        return sum(p.shares * prices.get(p.symbol, p.entry) for p in self.positions.values())

    def unrealized(self, prices: dict[str, float]) -> float:
        return sum(p.shares * (prices.get(p.symbol, p.entry) - p.entry) for p in self.positions.values())


def entry_block_reason(book: DayBook, cfg: DayTradeConfig, symbol: str, shares: int, price: float, *,
                       now: datetime, entry_until: datetime, flat_at: datetime, account_equity: float,
                       prices: dict[str, float]) -> str | None:
    """The lab's own limits (replacing R5/R9 for this slice). None = allowed."""
    if not cfg.enabled:
        return "day trading is turned off in your settings"
    if book.halted:
        return f"the {cfg.daily_loss_halt:.0%} daily loss limit was hit; no more day trades today"
    if now >= entry_until or now >= flat_at:
        return "outside the entry window"
    if symbol in book.attempted:
        return f"already tried {symbol} today (one attempt per stock per day)"
    if len(book.positions) >= cfg.max_positions:
        return f"already holding {cfg.max_positions} day trades"
    if book.trades >= cfg.max_trades_per_day:
        return f"reached {cfg.max_trades_per_day} day trades today"
    if (cfg.account_type == "margin" and account_equity < PDT_EQUITY_MIN
            and sum(book.recent_day_trades[-4:]) + book.trades >= PDT_MAX_DAY_TRADES):
        return "pattern-day-trader rule: max 3 day trades in 5 business days under $25,000"
    slice_now = book.start_equity + book.realized + book.unrealized(prices)
    if shares * price > cfg.max_position_fraction * slice_now + 1e-6:
        return "position larger than the per-trade cap"
    if book.open_value(prices) + shares * price > slice_now + 1e-6:
        return "the day-trading slice doesn't have that much cash (it never borrows)"
    return None


def should_halt(book: DayBook, cfg: DayTradeConfig, prices: dict[str, float]) -> bool:
    return book.realized + book.unrealized(prices) <= -cfg.daily_loss_halt * book.start_equity


def params_from_report(path: Path | None) -> Params | None:
    """The parameters the most recent walk-forward year chose (trained only on earlier years)."""
    if path is None or not Path(path).is_file():
        return None
    folds = json.loads(Path(path).read_text())["walk_forward"]["folds"]
    return Params(**folds[-1]["chosen_params"]) if folds else None


def live_candidates(live, s: Session, now: datetime, prev_close_time: datetime
                    ) -> tuple[list[Candidate], dict[str, pd.DataFrame]]:
    """Today's candidates exactly as the test defined them, from live data available at ``now``."""
    decision = s.open + timedelta(minutes=OPENING_RANGE_MINUTES)
    if now < decision:
        return [], {}
    daily = live.recent_daily(POOL)
    today = pd.Timestamp(s.day)
    daily = {k: v[v.index < today] for k, v in daily.items()}
    universe = dollar_volume_rank(daily, s.day, top_n=100)
    bars = live.minute_bars(universe, s.open, now)
    gappers = []
    for sym in universe:
        b, d = bars.get(sym), daily.get(sym)
        if b is None or not len(b) or d is None or not len(d):
            continue
        first = b[b.index >= s.open]
        if not len(first):
            continue
        pc, op = float(d["close"].iloc[-1]), float(first["open"].iloc[0])
        if pc > 0 and op >= MIN_PRICE and op / pc - 1 >= DATASET_MIN_GAP:
            gappers.append((sym, pc, op, float(d["volume"].tail(20).mean())))
    if not gappers:
        return [], {}
    items = live.news([g[0] for g in gappers], prev_close_time, decision - timedelta(seconds=NEWS_DELAY_SECONDS))
    cands = []
    for sym, pc, op, avgv in gappers:
        hl = [{"at": i["created_at"], "headline": i["headline"], "source": i["source"],
               "n_symbols": len(i["symbols"])} for i in items if sym in i["symbols"]]
        if hl:
            cands.append(Candidate(s.day.isoformat(), sym, s.open.isoformat(), s.close.isoformat(), pc, op,
                                   op / pc - 1, avgv, sorted(hl, key=lambda h: h["at"]),
                                   live.spread(sym, now), True))
    return cands, {c.symbol: bars[c.symbol] for c in cands}
