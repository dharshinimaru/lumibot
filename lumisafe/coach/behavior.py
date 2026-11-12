"""BehaviorCoach (SPEC §5.6): cooling-off delays on emotional trades.

Only ``Source.USER`` intents are reviewed. Core and satellite pass with APPROVE.
The coach never BLOCKs (B5). It only delays and explains, because the user
stays in control. Time comes from ``snap.as_of``, never the clock.

* B1 panic-sell: user SELL of a core ETF down ≥ 10% over 20 trading days → DELAY 24h,
  with the historical recovery context for similar drops, computed from data.
* B2 chasing: user BUY of a symbol up ≥ 25% over 20 trading days → DELAY 24h.
* B3 overtrading: a user's 3rd+ intent in a day → DELAY 24h.
* B4: delayed intents need ``lumisafe confirm <intent_id>`` after the delay,
  re-enter the pipeline from the start, and expire 72h after creation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from lumisafe.journal.journal import intent_from_dict, intent_to_dict
from lumisafe.types import OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, Verdict, clip_explanation

REVIEWER = "coach"
LOOKBACK_DAYS = 20
PANIC_DROP = -0.10
CHASE_RISE = 0.25
MAX_USER_INTENTS_PER_DAY = 2  # the 3rd+ is delayed
DELAY = timedelta(hours=24)
EXPIRY = timedelta(hours=72)
RECOVERY_HORIZON_DAYS = 252  # ~1 trading year


@dataclass(frozen=True)
class MarketContext:
    """What the coach may look at. ``closes`` must end at or before ``snap.as_of`` (no look-ahead)."""

    closes: dict[str, pd.Series] = field(default_factory=dict)
    user_intents_today: int = 0  # user intents already journaled today, before this one
    confirmed_intent_ids: frozenset[str] = frozenset()  # re-entries the user confirmed (B4)


def trailing_return(closes: pd.Series | None, days: int = LOOKBACK_DAYS) -> float | None:
    if closes is None:
        return None
    s = closes.dropna()
    if len(s) <= days or s.iloc[-days - 1] <= 0:
        return None
    return float(s.iloc[-1] / s.iloc[-days - 1] - 1)


def recovery_context(closes: pd.Series, drop: float) -> str:
    """How past 20-day drops at least this deep played out one year later, from the data itself."""
    s = closes.dropna()
    if len(s) < LOOKBACK_DAYS + RECOVERY_HORIZON_DAYS + 2:
        return "There isn't enough price history to show how similar drops played out."
    r20 = s / s.shift(LOOKBACK_DAYS) - 1
    fwd = s.shift(-RECOVERY_HORIZON_DAYS) / s - 1
    threshold = min(drop, PANIC_DROP)
    episodes, last = [], None
    for i, (ts, r) in enumerate(r20.items()):
        if pd.isna(r) or r > threshold or pd.isna(fwd.iloc[i]):
            continue
        if last is not None and i - last < LOOKBACK_DAYS:
            continue  # same episode
        episodes.append(float(fwd.iloc[i]))
        last = i
    if not episodes:
        return "This symbol's history has no completed earlier drop this large to compare with."
    higher = sum(1 for e in episodes if e > 0)
    med = float(pd.Series(episodes).median())
    since = pd.Timestamp(s.index[0]).year
    return (f"Since {since}, it had {len(episodes)} similar drops; a year later it was higher in "
            f"{higher} of {len(episodes)} (median {med:+.0%}).")


class BehaviorCoach:
    def __init__(self, core_symbols: set[str]):
        self.core_symbols = frozenset(core_symbols)

    def review(self, intent: OrderIntent, snap: PortfolioSnapshot, market: MarketContext | None) -> ReviewResult:
        if intent.source is not Source.USER:
            return ReviewResult(REVIEWER, Verdict.APPROVE, "Not a manual trade; the coach only reviews your trades.")
        market = market or MarketContext()
        if intent.intent_id in market.confirmed_intent_ids:
            return ReviewResult(REVIEWER, Verdict.APPROVE, "You confirmed this trade after the cooling-off period.")

        release = snap.as_of + DELAY
        closes = market.closes.get(intent.symbol)
        r20 = trailing_return(closes)

        if intent.side is Side.SELL and intent.symbol in self.core_symbols and r20 is not None and r20 <= PANIC_DROP:
            context = recovery_context(closes, r20)
            return self._delay(
                f"{intent.symbol} is down {abs(r20):.0%} in 20 trading days; selling core funds in a drop locks in "
                f"the loss. Sleep on it: confirm after {release:%b %d %H:%M} if you still want to. {context}",
                release)

        if intent.side is Side.BUY and r20 is not None and r20 >= CHASE_RISE:
            return self._delay(
                f"{intent.symbol} is up {r20:.0%} in 20 trading days. Buying after a big run-up is how many "
                f"beginners buy high. Take a day: confirm after {release:%b %d %H:%M} if you still want it.", release)

        if market.user_intents_today >= MAX_USER_INTENTS_PER_DAY:
            return self._delay(
                f"This is your trade idea #{market.user_intents_today + 1} today. Frequent trading is one of the "
                f"biggest drags on returns. Confirm after {release:%b %d %H:%M} if it still makes sense.", release)

        return ReviewResult(REVIEWER, Verdict.APPROVE, "No warning signs (panic-selling, chasing, or overtrading).")

    @staticmethod
    def _delay(text: str, release: datetime) -> ReviewResult:
        return ReviewResult(REVIEWER, Verdict.DELAY, clip_explanation(text), release_at=release)


# --- delayed intents (B4) ------------------------------------------------------------------


@dataclass(frozen=True)
class PendingIntent:
    intent: OrderIntent
    release_at: datetime
    expires_at: datetime
    status: str  # pending | confirmed | expired


class PendingStore:
    """Delayed user intents waiting for confirmation. Small JSON file, mutable by design.

    The journal keeps the immutable record (the DELAY entry). This store only
    tracks whether a delayed intent was later confirmed or expired.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def add(self, intent: OrderIntent, release_at: datetime) -> PendingIntent:
        data = self._load()
        p = PendingIntent(intent, release_at, intent.created_at + EXPIRY, "pending")
        data[intent.intent_id] = {"intent": intent_to_dict(intent), "release_at": release_at.isoformat(),
                                  "expires_at": p.expires_at.isoformat(), "status": "pending"}
        self._save(data)
        return p

    def get(self, intent_id: str) -> PendingIntent | None:
        data = self._load()
        matches = [k for k in data if k == intent_id or k.startswith(intent_id)]
        if len(matches) != 1:
            return None
        d = data[matches[0]]
        return PendingIntent(intent_from_dict(d["intent"]), datetime.fromisoformat(d["release_at"]),
                             datetime.fromisoformat(d["expires_at"]), d["status"])

    def set_status(self, intent_id: str, status: str) -> None:
        data = self._load()
        data[intent_id]["status"] = status
        self._save(data)

    def all(self) -> list[PendingIntent]:
        return [p for k in self._load() if (p := self.get(k)) is not None]

    def expire_old(self, now: datetime) -> list[PendingIntent]:
        expired = []
        for p in self.all():
            if p.status == "pending" and now >= p.expires_at:
                self.set_status(p.intent.intent_id, "expired")
                expired.append(p)
        return expired
