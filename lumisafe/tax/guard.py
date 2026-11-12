"""TaxGuard (SPEC §5.8): wash-sale and short-term-gain awareness.

* T1 wash sale: BUY of a symbol sold at a loss within the past 30 days → WARN
  for core, BLOCK for satellite and user.
* T2 short-term gain: non-core SELL of lots held < 365 days at a gain → WARN
  with the estimated extra tax at configurable placeholder rates.
* T3: sells consume lots highest-cost-first, except where that conflicts with
  R9 (young lots are skipped for non-core sells). See ``portfolio.lots.select_lots``.
* T4: every explanation ends with "Not tax advice."
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from lumisafe.journal.journal import JournalEntry
from lumisafe.portfolio.lots import select_lots
from lumisafe.types import (
    MAX_EXPLANATION_CHARS, OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, TaxLot, Verdict,
    clip_explanation,
)

REVIEWER = "tax"
SUFFIX = "Not tax advice."
WASH_SALE_DAYS = 30
LONG_TERM_DAYS = 365


def _explain(text: str) -> str:
    body = clip_explanation(text)
    room = MAX_EXPLANATION_CHARS - len(SUFFIX) - 1
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{body} {SUFFIX}"


class TaxGuard:
    def __init__(self, core_symbols: set[str], *, short_term_rate: float = 0.32, long_term_rate: float = 0.15,
                 min_holding_days: int = 5, realized_pnl: Callable[[str], float | None] | None = None):
        self.core_symbols = frozenset(core_symbols)
        self.short_term_rate = short_term_rate
        self.long_term_rate = long_term_rate
        self.min_holding_days = min_holding_days
        self.realized_pnl = realized_pnl or (lambda entry_id: None)

    def review(self, intent: OrderIntent, snap: PortfolioSnapshot, recent_sales: list[JournalEntry]) -> ReviewResult:
        if intent.side is Side.BUY:
            return self._wash_sale(intent, snap, recent_sales)
        return self._short_term(intent, snap)

    # -- T1 -----------------------------------------------------------------------------------

    def _sale_loss(self, sale: JournalEntry, snap: PortfolioSnapshot) -> float | None:
        pnl = self.realized_pnl(sale.entry_id)
        if pnl is not None:
            return pnl
        # Fallback: compare the sale price with the cost of lots still held in that symbol.
        lots = [l for l in snap.lots if l.symbol == sale.intent.symbol and l.quantity > 0]
        if sale.fill_price is None or not lots:
            return None
        avg_cost = sum(l.cost_basis * l.quantity for l in lots) / sum(l.quantity for l in lots)
        return (sale.fill_price - avg_cost) * (sale.fill_qty or 0)

    def _wash_sale(self, intent: OrderIntent, snap: PortfolioSnapshot,
                   recent_sales: list[JournalEntry]) -> ReviewResult:
        cutoff = snap.as_of - timedelta(days=WASH_SALE_DAYS)
        for sale in recent_sales:
            if sale.intent.symbol != intent.symbol or sale.intent.side is not Side.SELL or not sale.fill_qty:
                continue
            if not (cutoff <= sale.ts <= snap.as_of):
                continue
            loss = self._sale_loss(sale, snap)
            if loss is None or loss >= 0:
                continue
            when = sale.ts.strftime("%b %d")
            if intent.source is Source.CORE:
                return ReviewResult(REVIEWER, Verdict.WARN, _explain(
                    f"{intent.symbol} was sold at a loss on {when}; buying it back within 30 days can be a wash "
                    "sale, which defers that loss. The core plan proceeds because staying invested matters more."))
            return ReviewResult(REVIEWER, Verdict.BLOCK, _explain(
                f"{intent.symbol} was sold at a ${abs(loss):,.2f} loss on {when}. Buying it back within 30 days "
                "is a wash sale: the loss can't be claimed now. Wait until the 30 days pass."))
        return ReviewResult(REVIEWER, Verdict.APPROVE, _explain("No wash-sale issue."))

    # -- T2 / T3 --------------------------------------------------------------------------------

    def lots_for_sale(self, intent: OrderIntent, snap: PortfolioSnapshot) -> list[tuple[TaxLot, float]]:
        """T3: highest-cost lots first; R9-protected (young) lots skipped for non-core sells."""
        return select_lots(snap.lots, intent.symbol, intent.quantity, snap.as_of, self.min_holding_days,
                           respect_holding_period=intent.source is not Source.CORE)

    def _short_term(self, intent: OrderIntent, snap: PortfolioSnapshot) -> ReviewResult:
        if intent.source is Source.CORE:
            return ReviewResult(REVIEWER, Verdict.APPROVE, _explain("Core rebalancing sale."))
        price = snap.prices.get(intent.symbol)
        if not price:
            return ReviewResult(REVIEWER, Verdict.APPROVE, _explain("No price to estimate taxes."))
        gain = 0.0
        earliest_long_term = None
        for lot, q in self.lots_for_sale(intent, snap):
            held = (snap.as_of - lot.acquired_at).days
            if held < LONG_TERM_DAYS and price > lot.cost_basis:
                gain += (price - lot.cost_basis) * q
                lt_date = lot.acquired_at + timedelta(days=LONG_TERM_DAYS)
                earliest_long_term = lt_date if earliest_long_term is None else max(earliest_long_term, lt_date)
        if gain <= 0:
            return ReviewResult(REVIEWER, Verdict.APPROVE, _explain("No short-term gain on the shares sold."))
        extra = gain * (self.short_term_rate - self.long_term_rate)
        return ReviewResult(REVIEWER, Verdict.WARN, _explain(
            f"This sale realizes about ${gain:,.2f} of short-term gain (held under a year). At placeholder rates "
            f"of {self.short_term_rate:.0%} vs {self.long_term_rate:.0%}, that's roughly ${extra:,.2f} more tax "
            f"than waiting until {earliest_long_term:%b %d, %Y}."))
