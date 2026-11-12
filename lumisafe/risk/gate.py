"""RiskGate (SPEC §5.3): the deterministic final say on every order.

Pure and deterministic: no I/O, no LLM, no clock reads (time comes from
``snap.as_of``). ``check()`` never raises (R11). Any malformed input or
internal error returns BLOCK.
"""

from __future__ import annotations

from lumisafe.config.schema import RiskConfig
from lumisafe.risk import limits
from lumisafe.types import (
    OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, Verdict, clip_explanation,
)

REVIEWER = "risk"


def _result(verdict: Verdict, text: str, adjusted: float | None = None) -> ReviewResult:
    return ReviewResult(REVIEWER, verdict, clip_explanation(text), adjusted_quantity=adjusted)


def _block(text: str) -> ReviewResult:
    return _result(Verdict.BLOCK, text)


class RiskGate:
    def __init__(self, config: RiskConfig, core_symbols: set[str]):
        self.config = config
        self.core_symbols = frozenset(core_symbols)

    # -- public contract --------------------------------------------------------------

    def check(self, intent: OrderIntent, snap: PortfolioSnapshot) -> ReviewResult:
        try:
            result = self._check(intent, snap)
            if not isinstance(result, ReviewResult):  # pragma: no cover - defensive
                return _block("Risk check produced no decision, so the order is blocked for safety.")
            return result
        except Exception:
            return _block("Risk check hit an internal error, so the order is blocked for safety.")

    def halted(self, snap: PortfolioSnapshot) -> dict[Source, bool]:
        try:
            cfg = self.config
            account = limits.account_drawdown_alert(snap, cfg)
            satellite = (
                limits.satellite_daily_halt(snap, cfg)
                or limits.satellite_drawdown_halt(snap, cfg)
                or account
            )
            return {Source.CORE: False, Source.SATELLITE: bool(satellite), Source.USER: bool(account)}
        except Exception:
            # Fail closed: if we can't tell, treat every source as halted.
            return {Source.CORE: True, Source.SATELLITE: True, Source.USER: True}

    # -- rules ------------------------------------------------------------------------------

    def _check(self, intent: OrderIntent, snap: PortfolioSnapshot) -> ReviewResult:
        cfg = self.config
        if cfg.allow_leverage or cfg.allow_short or cfg.allow_options:  # C1 defense in depth
            return _block("Leverage, shorting and options are never allowed.")
        problem = limits.validate(intent, snap)
        if problem:
            return _block(f"Order blocked: {problem}.")

        sym = intent.symbol
        price = float(snap.prices[sym])
        held = float(snap.positions.get(sym, 0.0))
        qty = float(intent.quantity)
        src = intent.source
        is_core_symbol = sym in self.core_symbols
        account_alert = limits.account_drawdown_alert(snap, cfg)

        if src is Source.SATELLITE:
            if limits.satellite_drawdown_halt(snap, cfg):  # R7
                return _block(
                    f"Satellite is down {cfg.satellite_drawdown_halt:.0%}+ from its peak. Satellite trading "
                    "is halted until you reset it manually (lumisafe reset-halt satellite)."
                )
            if limits.satellite_daily_halt(snap, cfg):  # R6
                return _block(
                    f"Satellite lost {cfg.satellite_daily_loss_halt:.0%}+ today. Satellite trading is paused "
                    "for the rest of the day."
                )

        if intent.side is Side.SELL:
            if qty > held + limits.EPS:  # R4
                return _block(f"You hold {held:g} {sym}; selling {qty:g} would be a short sale, which is not allowed.")
            if account_alert and is_core_symbol:  # R8
                return _block(
                    f"Your account is down {cfg.account_drawdown_alert:.0%}+ from its peak. LumiSafe never "
                    f"sells core holdings like {sym} in a drawdown; that is when selling hurts most."
                )
            if src is not Source.CORE:  # R9
                sellable = limits.sellable_after_holding_period(sym, held, snap, cfg.min_holding_days)
                if qty > sellable + limits.EPS:
                    return _block(
                        f"Only {sellable:g} {sym} has been held at least {cfg.min_holding_days} trading days. "
                        "Short holding periods are blocked to prevent overtrading."
                    )
            return _result(Verdict.APPROVE, "Within all risk limits.")

        # BUY
        if account_alert and src in (Source.SATELLITE, Source.USER):  # R8
            return _block(
                f"Your account is down {cfg.account_drawdown_alert:.0%}+ from its peak. New non-core buys "
                "are paused; your core plan keeps running."
            )
        if src in (Source.SATELLITE, Source.USER) and snap.trades_this_week >= cfg.max_trades_per_week:  # R5
            return _block(
                f"You've reached the limit of {cfg.max_trades_per_week} trades this week. Frequent trading "
                "is one of the biggest drags on beginner returns."
            )

        etf = limits.is_etf(sym, self.core_symbols)
        cap = cfg.max_single_etf_weight if etf else cfg.max_single_stock_weight  # R2 / R1
        whole = float(qty).is_integer()
        room = limits.max_quantity_under_cap(held, price, snap.equity, cap, whole)
        kind = "fund" if etf else "single stock"
        if room <= 0:
            return _block(f"{sym} is already at the {cap:.0%} limit for one {kind}.")
        final_qty = min(qty, room)

        if not limits.affordable(final_qty, price, snap.cash):  # R3
            return _block(
                f"Not enough cash: {final_qty:g} {sym} costs about ${final_qty * price:,.2f} and you have "
                f"${snap.cash:,.2f}. Borrowing (margin) is never used."
            )

        if final_qty < qty:  # R10: only ever downward, and > 0
            return _result(
                Verdict.RESIZE,
                f"Reduced from {qty:g} to {final_qty:g} {sym} to keep any one {kind} at or under {cap:.0%} "
                "of your account.",
                adjusted=final_qty,
            )
        return _result(Verdict.APPROVE, "Within all risk limits.")
