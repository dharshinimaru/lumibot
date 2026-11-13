"""Read-only "what to buy, and when" for your own account (no broker, no orders).

Uses the same rules as the core planner (``portfolio/core.py``) but for money
you invest yourself:

* **What:** split the amount you're adding across the plan's funds, most
  underweight first, so each purchase also nudges you back toward target.
* **When:** once a month, on the first market day. If a fund is still off
  target by more than the plan's band afterwards, it also lists the sale that
  would bring it back (selling can create a tax bill; it says so).

It reads nothing but your plan, the holdings you type in and recent closing
prices. It never journals, never submits and never changes the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from lumisafe.config.schema import CoreConfig
from lumisafe.portfolio.core import CoreIndexStrategy
from lumisafe.types import PortfolioSnapshot


@dataclass(frozen=True)
class Line:
    symbol: str
    side: str  # "buy" | "sell"
    shares: float
    price: float

    @property
    def dollars(self) -> float:
        return self.shares * self.price


@dataclass(frozen=True)
class Advice:
    amount: float
    lines: list[Line]
    weights_before: dict[str, float]
    weights_after: dict[str, float]
    targets: dict[str, float]
    leftover: float
    notes: list[str] = field(default_factory=list)

    @property
    def buys(self) -> list[Line]:
        return [l for l in self.lines if l.side == "buy"]

    @property
    def sells(self) -> list[Line]:
        return [l for l in self.lines if l.side == "sell"]


def parse_holdings(text: str | None) -> dict[str, float]:
    """``"VTI=10, VXUS=4.5"`` → ``{"VTI": 10.0, "VXUS": 4.5}``. Raises ValueError on bad input."""
    out: dict[str, float] = {}
    if not text:
        return out
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"'{part}' should look like SYMBOL=SHARES, e.g. VTI=10")
        sym, qty = part.split("=", 1)
        sym = sym.strip().upper()
        shares = float(qty.strip())
        if not sym or shares < 0:
            raise ValueError(f"'{part}' should look like SYMBOL=SHARES with shares of 0 or more")
        out[sym] = out.get(sym, 0.0) + shares
    return out


def _weights(values: dict[str, float]) -> dict[str, float]:
    total = sum(values.values())
    return {s: (v / total if total > 0 else 0.0) for s, v in values.items()}


def what_to_buy(core: CoreConfig, holdings: dict[str, float], prices: dict[str, float], amount: float,
                fractional: bool = True) -> Advice:
    """Split ``amount`` dollars across the plan's funds, most underweight first."""
    missing = [s for s in core.etfs if prices.get(s, 0) <= 0]
    if missing:
        raise ValueError(f"No recent price for {', '.join(missing)}; try again later.")
    planner = CoreIndexStrategy(core, fractional=fractional)
    positions = {s: float(holdings.get(s, 0.0)) for s in core.etfs}
    px = {s: float(prices[s]) for s in core.etfs}
    equity = sum(positions[s] * px[s] for s in core.etfs) + max(amount, 0.0)
    snap = PortfolioSnapshot(as_of=datetime.now(timezone.utc), cash=max(amount, 0.0), equity=equity,
                             positions=positions, prices=px, peak_equity=equity, satellite_equity=0.0,
                             satellite_peak_equity=0.0, satellite_day_start_equity=0.0, trades_this_week=0)
    values = {s: snap.positions[s] * snap.prices[s] for s in core.etfs}
    budget = max(amount, 0.0)
    buys = planner._allocate(snap, budget, "your contribution", snap.as_of)
    sells = []
    notes: list[str] = []

    after = dict(values)
    for i in buys:
        after[i.symbol] += i.quantity * snap.prices[i.symbol]
    drift = {s: w - core.etfs[s] for s, w in _weights(after).items()} if sum(after.values()) > 0 else {}
    worst = max(drift.values(), key=abs) if drift else 0.0
    if abs(worst) > core.rebalance_band:
        sells = planner._rebalance_sells(snap, buys)
        if sells:
            # Sell the overweight fund back to target, then spend the new money plus the
            # sale proceeds on the underweight funds, so nothing sits idle as cash.
            proceeds = sum(i.quantity * snap.prices[i.symbol] for i in sells)
            sold = {i.symbol: i.quantity for i in sells}
            snap = replace(snap, positions={s: q - sold.get(s, 0.0) for s, q in snap.positions.items()})
            buys = [i for i in planner._allocate(snap, budget + proceeds, "your contribution", snap.as_of)
                    if i.symbol not in sold]  # never sell and buy the same fund in one go
            notes.append(f"A fund is {abs(worst):.0%} off target even after adding new money, beyond your "
                         f"{core.rebalance_band:.0%} band, so the plan sells some of it and buys the others. "
                         "Selling in a taxable account can create a tax bill; adding more new money over the "
                         "next few months instead also works.")
            budget += proceeds

    lines = [Line(i.symbol, "sell", i.quantity, prices[i.symbol]) for i in sells]
    lines += [Line(i.symbol, "buy", i.quantity, snap.prices[i.symbol]) for i in buys]
    after = {s: snap.positions[s] * snap.prices[s] for s in core.etfs}
    for l in lines:
        if l.side == "buy":
            after[l.symbol] += l.dollars
    spent = sum(l.dollars for l in lines if l.side == "buy")
    if amount > 0 and not buys:
        notes.append("That amount can't buy a whole share of any fund that needs topping up. Keep it and add it "
                     "to next month's amount." if not fractional else
                     "That amount is too small to split sensibly (each order would be under $10). "
                     "Add a little more or wait until next month.")
    elif not fractional and budget - spent >= 1:
        notes.append("Whole shares only, so some money is left over; add it to next month's amount.")
    return Advice(amount=amount, lines=lines, weights_before=_weights(values), weights_after=_weights(after),
                  targets=dict(core.etfs), leftover=max(budget - spent, 0.0), notes=notes)


def first_market_day(year: int, month: int) -> date:
    """First NYSE trading day of the month (weekends and exchange holidays skipped)."""
    first = date(year, month, 1)
    try:
        import pandas_market_calendars as mcal

        days = mcal.get_calendar("NYSE").schedule(first, first + timedelta(days=10)).index
        if len(days):
            return days[0].date()
    except Exception:  # calendar package unavailable: fall back to the first weekday
        pass
    d = first
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def buy_dates(today: date) -> tuple[date, date]:
    """(this month's buy day, next month's buy day)."""
    this = first_market_day(today.year, today.month)
    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return this, first_market_day(ny, nm)


def when_text(today: date) -> str:
    this, nxt = buy_dates(today)
    if today < this:
        return f"Buy on {this:%A %b %-d} (this month's first market day)."
    return (f"This month's buy day was {this:%a %b %-d}. If you haven't bought yet this month, buy now; "
            f"otherwise your next buy is {nxt:%A %b %-d}.")
