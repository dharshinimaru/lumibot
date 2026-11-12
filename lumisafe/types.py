"""Core dataclasses and enums (SPEC §5.2). These are binding contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Source(str, Enum):
    CORE = "core"
    SATELLITE = "satellite"
    USER = "user"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Verdict(str, Enum):
    APPROVE = "approve"
    RESIZE = "resize"
    DELAY = "delay"
    WARN = "warn"
    BLOCK = "block"
    VETO = "veto"


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str  # uuid4
    created_at: datetime
    source: Source
    symbol: str
    side: Side
    quantity: float  # > 0; whole shares unless broker supports fractional
    reason: str  # human-readable why
    strategy_name: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    reviewer: str  # "coach" | "tax" | "advisor" | "risk"
    verdict: Verdict
    explanation: str  # plain English, <= 300 chars
    adjusted_quantity: float | None = None  # only with RESIZE; must be < original
    release_at: datetime | None = None  # only with DELAY


@dataclass(frozen=True)
class TaxLot:
    symbol: str
    quantity: float
    cost_basis: float  # per share
    acquired_at: datetime


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: datetime
    cash: float
    equity: float
    positions: dict[str, float]  # symbol -> quantity
    prices: dict[str, float]  # symbol -> last price
    peak_equity: float
    satellite_equity: float
    satellite_peak_equity: float
    satellite_day_start_equity: float
    trades_this_week: int
    lots: list[TaxLot] = field(default_factory=list)


MAX_EXPLANATION_CHARS = 300


def clip_explanation(text: str) -> str:
    """Keep explanations within the 300-char contract without losing the ending.

    Callers that need a mandatory suffix (e.g. TaxGuard's "Not tax advice.")
    should clip the body first and then append the suffix.
    """
    text = " ".join(str(text).split())
    if len(text) <= MAX_EXPLANATION_CHARS:
        return text
    return text[: MAX_EXPLANATION_CHARS - 1].rstrip() + "…"
