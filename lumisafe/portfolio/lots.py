"""Tax-lot bookkeeping for fills LumiSafe has seen.

Lots feed R9 (minimum holding period), T2 (short-term gains) and T3 (which lots
a sell consumes). Lots are chosen highest-cost-first (T3). For non-core sells,
only lots past the minimum holding period may be used, because R9 forbids the
rest. Core rebalance sells may use any lot.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from lumisafe.risk.limits import business_days_held
from lumisafe.types import TaxLot


def select_lots(lots: list[TaxLot], symbol: str, quantity: float, as_of: datetime,
                min_holding_days: int, respect_holding_period: bool) -> list[tuple[TaxLot, float]]:
    """Pick (lot, qty) pairs highest cost first (T3), skipping young lots when R9 applies.

    Returns as much as the eligible lots cover; the caller decides what to do
    with any shortfall (untracked holdings).
    """
    eligible = [lot for lot in lots if lot.symbol == symbol and lot.quantity > 0]
    if respect_holding_period:
        eligible = [lot for lot in eligible if business_days_held(lot.acquired_at, as_of) >= min_holding_days]
    eligible.sort(key=lambda lot: (-lot.cost_basis, lot.acquired_at))
    picked: list[tuple[TaxLot, float]] = []
    remaining = quantity
    for lot in eligible:
        if remaining <= 1e-12:
            break
        take = min(lot.quantity, remaining)
        picked.append((lot, take))
        remaining -= take
    return picked


class LotBook:
    def __init__(self, lots: list[TaxLot] | None = None):
        self.lots: list[TaxLot] = list(lots or [])

    # -- persistence --------------------------------------------------------------
    def to_list(self) -> list[dict[str, Any]]:
        return [
            {"symbol": l.symbol, "quantity": l.quantity, "cost_basis": l.cost_basis,
             "acquired_at": l.acquired_at.isoformat()}
            for l in self.lots
        ]

    @classmethod
    def from_list(cls, data: list[dict[str, Any]] | None) -> "LotBook":
        return cls([
            TaxLot(d["symbol"], float(d["quantity"]), float(d["cost_basis"]),
                   datetime.fromisoformat(d["acquired_at"]))
            for d in (data or [])
        ])

    # -- mutation on fills --------------------------------------------------------------
    def add(self, symbol: str, quantity: float, price: float, at: datetime) -> None:
        if quantity > 0:
            self.lots.append(TaxLot(symbol, float(quantity), float(price), at))

    def consume(self, symbol: str, quantity: float, as_of: datetime, min_holding_days: int,
                respect_holding_period: bool) -> list[tuple[TaxLot, float]]:
        """Remove ``quantity`` from lots (T3 order) and return what was consumed.

        If the holding-period-respecting pass can't cover the sale (the gate
        already approved it, e.g. against untracked holdings), the remainder
        is taken from any lot, highest cost first.
        """
        picked = select_lots(self.lots, symbol, quantity, as_of, min_holding_days, respect_holding_period)
        covered = sum(q for _, q in picked)
        if covered + 1e-12 < quantity and respect_holding_period:
            rest = [l for l in self.lots if l not in {p for p, _ in picked}]
            picked += select_lots(rest, symbol, quantity - covered, as_of, min_holding_days, False)
        for lot, q in picked:
            idx = self.lots.index(lot)
            left = lot.quantity - q
            if left <= 1e-9:
                self.lots.pop(idx)
            else:
                self.lots[idx] = replace(lot, quantity=left)
        return picked

    def reconcile(self, positions: dict[str, float]) -> None:
        """Trim lots so no symbol's lots exceed the broker's actual position.

        Sales made outside LumiSafe drop the cheapest lots first (the opposite
        of T3), so the lots that remain are the conservative ones for R9 and T2.
        """
        for symbol in {l.symbol for l in self.lots}:
            held = float(positions.get(symbol, 0.0))
            excess = sum(l.quantity for l in self.lots if l.symbol == symbol) - held
            if excess <= 1e-9:
                continue
            for lot in sorted([l for l in self.lots if l.symbol == symbol], key=lambda l: l.cost_basis):
                if excess <= 1e-9:
                    break
                take = min(lot.quantity, excess)
                idx = self.lots.index(lot)
                if lot.quantity - take <= 1e-9:
                    self.lots.pop(idx)
                else:
                    self.lots[idx] = replace(lot, quantity=lot.quantity - take)
                excess -= take
