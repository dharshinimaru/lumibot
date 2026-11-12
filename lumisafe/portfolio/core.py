"""CoreIndexStrategy: DCA + rebalance (SPEC §7.1).

A pure planner. It turns a portfolio snapshot and the calendar into
OrderIntents. It never reacts to news, prices beyond valuation, or the AI.
Every intent it emits still goes through the full pipeline and the RiskGate.

* On the first trading day of each month it invests new money: the smaller of
  ``monthly_contribution_usd`` and the investable cash. If the contribution is
  0, it invests all investable cash (lump-sum mode). Money goes to the most
  underweight ETFs first, so contributions do the rebalancing and avoid sells.
* On rebalance-check days, if any ETF is still off target by more than
  ``rebalance_band`` after the contribution, it sells the overweight ETFs back
  to target. The proceeds are reinvested on the next cycle, once the cash has
  settled (``rebalance_pending``).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from lumisafe.config.schema import CoreConfig
from lumisafe.types import OrderIntent, PortfolioSnapshot, Side, Source

STRATEGY_NAME = "core"
# Keep a cash cushion. Fees, rounding and the gap between the decision price
# and the fill price (orders fill at the next bar / market price) must never
# push the account below zero; Alpaca paper accounts would quietly use margin.
CASH_BUFFER_FRACTION = 0.02
# Skip dust orders; they add cost and journal noise without moving allocation.
MIN_ORDER_USD = 10.0


@dataclass(frozen=True)
class CorePlan:
    intents: list[OrderIntent]
    rebalance_pending: bool
    notes: list[str]


def _intent(symbol: str, side: Side, qty: float, reason: str, at: datetime) -> OrderIntent:
    return OrderIntent(
        intent_id=str(uuid.uuid4()), created_at=at, source=Source.CORE, symbol=symbol, side=side,
        quantity=qty, reason=reason, strategy_name=STRATEGY_NAME,
    )


def _round_qty(qty: float, fractional: bool) -> float:
    if fractional:
        return math.floor(qty * 1e4) / 1e4
    return float(math.floor(qty + 1e-9))


class CoreIndexStrategy:
    def __init__(self, config: CoreConfig, fractional: bool = True):
        self.config = config
        self.fractional = fractional

    @property
    def symbols(self) -> set[str]:
        return set(self.config.etfs)

    def is_rebalance_month(self, month: int) -> bool:
        return self.config.rebalance_check == "monthly" or month in (1, 4, 7, 10)

    def weights(self, snap: PortfolioSnapshot, extra_cash: float = 0.0) -> dict[str, float]:
        values = {s: snap.positions.get(s, 0.0) * snap.prices[s] for s in self.config.etfs}
        total = sum(values.values()) + extra_cash
        if total <= 0:
            return {s: 0.0 for s in self.config.etfs}
        return {s: v / total for s, v in values.items()}

    def plan(self, snap: PortfolioSnapshot, *, investable_cash: float, first_trading_day_of_month: bool,
             rebalance_pending: bool = False) -> CorePlan:
        cfg = self.config
        at = snap.as_of
        notes: list[str] = []
        if any(snap.prices.get(s, 0) <= 0 for s in cfg.etfs):
            return CorePlan([], rebalance_pending, ["Missing core prices; skipped this cycle."])

        investable_cash = max(0.0, investable_cash * (1 - CASH_BUFFER_FRACTION))
        intents: list[OrderIntent] = []

        if first_trading_day_of_month:
            budget = investable_cash if cfg.monthly_contribution_usd == 0 else min(
                cfg.monthly_contribution_usd, investable_cash)
            intents += self._allocate(snap, budget, "monthly contribution", at)

            if self.is_rebalance_month(at.month):
                drift = self._drift_after(snap, intents)
                worst = max(drift.values(), key=abs) if drift else 0.0
                if abs(worst) > cfg.rebalance_band:
                    sells = self._rebalance_sells(snap, intents)
                    if sells:
                        notes.append(f"Drift {worst:+.1%} exceeds the {cfg.rebalance_band:.0%} band; rebalancing.")
                        intents += sells
                        rebalance_pending = True
            return CorePlan(intents, rebalance_pending, notes)

        if rebalance_pending:
            # Reinvest settled sale proceeds into the underweight ETFs.
            intents += self._allocate(snap, investable_cash, "rebalance: reinvest proceeds", at)
            return CorePlan(intents, False, ["Reinvested rebalance proceeds."])

        return CorePlan([], False, notes)

    # -- helpers ------------------------------------------------------------------------------

    def _allocate(self, snap: PortfolioSnapshot, budget: float, why: str, at: datetime) -> list[OrderIntent]:
        """Spend ``budget`` on the most underweight ETFs first (contributions rebalance first)."""
        if budget <= 0:
            return []
        cfg = self.config
        values = {s: snap.positions.get(s, 0.0) * snap.prices[s] for s in cfg.etfs}
        total_after = sum(values.values()) + budget
        deficits = {s: max(0.0, cfg.etfs[s] * total_after - values[s]) for s in cfg.etfs}
        order = sorted(cfg.etfs, key=lambda s: deficits[s] / (cfg.etfs[s] * total_after or 1), reverse=True)
        remaining = budget
        intents = []
        for s in order:
            if remaining <= 0:
                break
            spend = min(deficits[s], remaining)
            qty = _round_qty(spend / snap.prices[s], self.fractional)
            if qty <= 0 or qty * snap.prices[s] < MIN_ORDER_USD:
                continue
            remaining -= qty * snap.prices[s]
            target_pct = cfg.etfs[s]
            intents.append(_intent(s, Side.BUY, qty, f"Core plan: {why} toward {s} target {target_pct:.0%}.", at))
        return intents

    def _drift_after(self, snap: PortfolioSnapshot, buys: list[OrderIntent]) -> dict[str, float]:
        cfg = self.config
        values = {s: snap.positions.get(s, 0.0) * snap.prices[s] for s in cfg.etfs}
        for i in buys:
            values[i.symbol] += i.quantity * snap.prices[i.symbol]
        total = sum(values.values())
        if total <= 0:
            return {}
        return {s: values[s] / total - cfg.etfs[s] for s in cfg.etfs}

    def _rebalance_sells(self, snap: PortfolioSnapshot, buys: list[OrderIntent]) -> list[OrderIntent]:
        cfg = self.config
        values = {s: snap.positions.get(s, 0.0) * snap.prices[s] for s in cfg.etfs}
        for i in buys:
            values[i.symbol] += i.quantity * snap.prices[i.symbol]
        total = sum(values.values())
        sells = []
        for s in cfg.etfs:
            excess = values[s] - cfg.etfs[s] * total
            if excess <= 0:
                continue
            qty = min(_round_qty(excess / snap.prices[s], self.fractional), snap.positions.get(s, 0.0))
            if qty > 0 and qty * snap.prices[s] >= MIN_ORDER_USD:
                sells.append(_intent(s, Side.SELL, qty,
                                     f"Core plan: rebalance {s} back to its {cfg.etfs[s]:.0%} target.", snap.as_of))
        return sells
