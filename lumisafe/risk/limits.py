"""Individual deterministic risk rules used by :class:`lumisafe.risk.gate.RiskGate`.

Every function here is pure: no I/O, no clock reads, no randomness.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from lumisafe.config.schema import RiskConfig
from lumisafe.types import OrderIntent, PortfolioSnapshot, Side, Source, TaxLot

# US-listed ETFs LumiSafe recognizes. Any symbol that is neither a core ETF nor
# listed here is treated as a single stock and gets the stricter single-stock
# cap (R1). Unknown means stricter, never looser.
KNOWN_ETFS = frozenset({
    # broad US equity
    "VTI", "ITOT", "SCHB", "SPTM", "VOO", "SPY", "IVV", "SPLG", "SCHX", "VV", "QQQ", "QQQM", "DIA",
    "IWM", "IJR", "IJH", "VB", "VO", "VXF", "RSP", "MDY", "IWB", "IWV",
    # international equity
    "VXUS", "IXUS", "VEA", "IEFA", "EFA", "SCHF", "VWO", "IEMG", "EEM", "SCHE", "VEU", "VT", "ACWI",
    "SPDW", "SPEM",
    # bonds
    "BND", "AGG", "SCHZ", "BNDX", "TLT", "IEF", "SHY", "VGIT", "VGLT", "VGSH", "BIV", "BSV", "BLV",
    "TIP", "VTIP", "SCHP", "MUB", "LQD", "VCIT", "VCSH", "GOVT", "SPAB", "IAGG",
    # other diversified
    "VNQ", "SCHH", "GLD", "IAU", "VIG", "SCHD", "VYM",
})

EPS = 1e-9


def is_etf(symbol: str, core_symbols: set[str]) -> bool:
    return symbol in core_symbols or symbol in KNOWN_ETFS


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate(intent: OrderIntent, snap: PortfolioSnapshot) -> str | None:
    """Return a reason string if the input is malformed (R11), else None."""
    if not isinstance(intent, OrderIntent):
        return "order is not a valid OrderIntent"
    if not isinstance(snap, PortfolioSnapshot):
        return "portfolio snapshot is missing or invalid"
    if not isinstance(intent.source, Source) or not isinstance(intent.side, Side):
        return "order source or side is invalid"
    if not isinstance(intent.symbol, str) or not intent.symbol.strip():
        return "order has no symbol"
    if not _finite(intent.quantity) or intent.quantity <= 0:
        return "order quantity must be a positive number"
    if not isinstance(snap.as_of, datetime):
        return "snapshot time is invalid"
    for name in ("cash", "equity", "peak_equity", "satellite_equity", "satellite_peak_equity",
                 "satellite_day_start_equity"):
        if not _finite(getattr(snap, name)):
            return f"snapshot {name} is not a number"
    if snap.equity <= 0:
        return "account equity is zero or negative"
    if not isinstance(snap.trades_this_week, int) or isinstance(snap.trades_this_week, bool) \
            or snap.trades_this_week < 0:
        return "weekly trade count is invalid"
    if not isinstance(snap.positions, dict) or not isinstance(snap.prices, dict):
        return "positions or prices are invalid"
    for sym, q in snap.positions.items():
        if not isinstance(sym, str) or not _finite(q) or q < 0:
            return "a held position is invalid (negative or not a number)"
    price = snap.prices.get(intent.symbol)
    if not _finite(price) or price <= 0:
        return f"no valid price for {intent.symbol}"
    if not isinstance(snap.lots, list):
        return "tax lots are invalid"
    for lot in snap.lots:
        if not isinstance(lot, TaxLot) or not _finite(lot.quantity) or lot.quantity < 0 \
                or not isinstance(lot.acquired_at, datetime):
            return "a tax lot is invalid"
    return None


def drawdown(value: float, peak: float) -> float:
    """Fractional drop from peak (0 when peak is unknown/zero)."""
    if peak <= 0:
        return 0.0
    return max(0.0, 1.0 - value / peak)


def satellite_daily_halt(snap: PortfolioSnapshot, cfg: RiskConfig) -> bool:
    """R6: satellite down >= daily-loss halt since the start of the day."""
    return drawdown(snap.satellite_equity, snap.satellite_day_start_equity) >= cfg.satellite_daily_loss_halt - EPS \
        and snap.satellite_day_start_equity > 0


def satellite_drawdown_halt(snap: PortfolioSnapshot, cfg: RiskConfig) -> bool:
    """R7: satellite down >= drawdown halt from its peak."""
    return drawdown(snap.satellite_equity, snap.satellite_peak_equity) >= cfg.satellite_drawdown_halt - EPS \
        and snap.satellite_peak_equity > 0


def account_drawdown_alert(snap: PortfolioSnapshot, cfg: RiskConfig) -> bool:
    """R8: account down >= alert level from its peak."""
    return drawdown(snap.equity, snap.peak_equity) >= cfg.account_drawdown_alert - EPS and snap.peak_equity > 0


def business_days_held(acquired_at: datetime, as_of: datetime) -> int:
    """Weekdays between acquisition and now (exchange holidays ignored; deterministic)."""
    a = acquired_at.date()
    b = as_of.date()
    if b <= a:
        return 0
    return int(np.busday_count(a, b))


def sellable_after_holding_period(symbol: str, held: float, snap: PortfolioSnapshot, min_days: int) -> float:
    """R9: quantity of ``symbol`` held at least ``min_days`` trading days.

    Holdings not covered by any tracked lot (e.g. bought before LumiSafe ran)
    count as aged: we only know about lots LumiSafe recorded.
    """
    lots = [lot for lot in snap.lots if lot.symbol == symbol and lot.quantity > 0]
    tracked = sum(lot.quantity for lot in lots)
    aged = sum(lot.quantity for lot in lots if business_days_held(lot.acquired_at, snap.as_of) >= min_days)
    untracked = max(0.0, held - tracked)
    return min(held, aged + untracked)


def max_quantity_under_cap(held: float, price: float, equity: float, cap: float, whole_shares: bool) -> float:
    """Largest additional quantity such that (held + q) * price <= cap * equity exactly (R1/R2).

    The result is verified with the same float arithmetic the invariant uses,
    so a property test of ``post_weight <= cap`` holds without tolerance.
    """
    def weight(q: float) -> float:
        # Exactly the expression the invariant is stated in: post-trade weight.
        return (held + q) * price / equity

    room = cap * equity / price - held
    if room <= 0:
        return 0.0
    step = 1.0 if whole_shares else 1e-6
    q = round(math.floor(room / step) * step, 6)
    while q > 0 and weight(q) > cap:
        q = round(q - step, 6)
    return max(q, 0.0)


def affordable(quantity: float, price: float, cash: float) -> bool:
    """R3: the order must not make cash negative (that would be margin)."""
    return quantity * price <= cash
