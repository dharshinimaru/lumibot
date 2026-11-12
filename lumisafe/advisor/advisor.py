"""AIAdvisor: explain and veto only (SPEC §5.5).

A1: returns only APPROVE or VETO (or WARN when unavailable for user intents).
    It never resizes, creates intents, changes config or touches a broker; its
    LLM client is called with no tools at all.
A2: CORE intents are never sent to the LLM; they auto-approve as "core plan".
A3: fail-closed for satellite. LLM error, > 30 s timeout or invalid JSON →
    satellite VETO ("advisor unavailable"); user WARN, continuing to the
    other checks.
A4: prompts live only in ``advisor/prompts.py``; context is limited to the
    intent, recent prices, the position, this symbol's journal history and
    headlines if available.
A5: veto quality is measured by ``update_shadow_returns`` (20-trading-day
    price change after each rejected intent, written to the journal).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

import pandas as pd

from lumisafe.advisor import prompts
from lumisafe.advisor.schema import AdvisorOutput, json_schema
from lumisafe.journal.journal import Journal, JournalEntry
from lumisafe.types import OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, Verdict, clip_explanation

REVIEWER = "advisor"
TIMEOUT_SECONDS = 30.0
DEFAULT_MODEL = "claude-opus-5-5"
SHADOW_DAYS = 20


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *, json_schema: dict | None, timeout: float) -> str: ...


@dataclass(frozen=True)
class AdvisorContext:
    """Everything the advisor may see (A4). No keys, no account numbers."""

    symbol: str
    side: str
    quantity: float
    source: str
    reason: str
    recent_closes: list[float] = field(default_factory=list)  # oldest → newest, up to 20 days
    position_qty: float = 0.0
    position_weight: float = 0.0
    symbol_history: list[dict] = field(default_factory=list)  # this symbol's recent journal decisions
    headlines: list[str] = field(default_factory=list)

    def to_prompt_dict(self) -> dict:
        return {
            "trade": {"side": self.side, "symbol": self.symbol, "quantity": self.quantity,
                      "requested_by": self.source, "stated_reason": self.reason},
            "recent_daily_closes": [round(c, 2) for c in self.recent_closes],
            "current_position": {"shares": self.position_qty, "share_of_account": round(self.position_weight, 4)},
            "past_decisions_on_this_symbol": self.symbol_history,
            "recent_headlines": self.headlines,
        }


def build_advisor_context(intent: OrderIntent, snap: PortfolioSnapshot, journal: Journal,
                          history_fn: Callable[[str, datetime], Any] | None,
                          headlines: list[str] | None = None) -> AdvisorContext:
    closes: list[float] = []
    if history_fn is not None:
        try:
            s = history_fn(intent.symbol, snap.as_of)
            if s is not None:
                closes = [float(x) for x in pd.Series(s).dropna().iloc[-20:]]
        except Exception:
            closes = []
    held = float(snap.positions.get(intent.symbol, 0.0))
    price = float(snap.prices.get(intent.symbol, 0.0) or 0.0)
    past = [
        {"date": e.ts.date().isoformat(), "side": e.intent.side.value, "quantity": e.intent.quantity,
         "source": e.intent.source.value, "outcome": e.final.value}
        for e in journal.query(symbol=intent.symbol, limit=10)
    ]
    return AdvisorContext(
        symbol=intent.symbol, side=Side(intent.side).value, quantity=float(intent.quantity),
        source=Source(intent.source).value, reason=intent.reason[:300], recent_closes=closes, position_qty=held,
        position_weight=(held * price / snap.equity) if snap.equity > 0 else 0.0, symbol_history=past,
        headlines=list(headlines or [])[:5],
    )


def _call_with_timeout(fn: Callable[[], str], timeout: float) -> str:
    """Run ``fn`` in a daemon thread; raise TimeoutError if it takes longer than ``timeout``."""
    box: dict[str, Any] = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"advisor took longer than {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


class AIAdvisor:
    timeout_s: float = TIMEOUT_SECONDS

    def __init__(self, llm_client: LLMClient | None, enabled: bool):
        self._llm = llm_client
        self.enabled = bool(enabled)

    def review(self, intent: OrderIntent, context: AdvisorContext | None) -> ReviewResult:
        if intent.source is Source.CORE:  # A2
            return ReviewResult(REVIEWER, Verdict.APPROVE, "core plan")
        if not self.enabled:
            return ReviewResult(REVIEWER, Verdict.APPROVE, "AI advisor is turned off in your settings.")
        try:
            if self._llm is None or context is None:
                raise RuntimeError("no advisor client or context")
            user = prompts.review_user_prompt(context.to_prompt_dict())
            raw = _call_with_timeout(
                lambda: self._llm.complete(prompts.REVIEW_SYSTEM, user, json_schema=json_schema(),
                                           timeout=self.timeout_s),
                self.timeout_s,
            )
            out = AdvisorOutput.model_validate_json(raw)
        except Exception:  # A3: any failure → fail closed for satellite, warn for user
            if intent.source is Source.SATELLITE:
                return ReviewResult(REVIEWER, Verdict.VETO, "advisor unavailable")
            return ReviewResult(REVIEWER, Verdict.WARN,
                                "advisor unavailable; your trade continues to the remaining safety checks.")
        verdict = Verdict.VETO if out.verdict == "veto" else Verdict.APPROVE
        return ReviewResult(REVIEWER, verdict, clip_explanation(out.plain_english))

    def daily_note(self, snap: PortfolioSnapshot, recent: list[JournalEntry],
                   comparison: str | None = None) -> str:
        facts = {
            "date": snap.as_of.date().isoformat(),
            "account_value": round(snap.equity, 2),
            "cash": round(snap.cash, 2),
            "holdings": {s: q for s, q in sorted(snap.positions.items())},
            "you_vs_benchmark": comparison,
            "recent_decisions": [
                {"date": e.ts.date().isoformat(), "trade": f"{e.intent.side.value} {e.intent.quantity:g} "
                 f"{e.intent.symbol}", "by": e.intent.source.value, "outcome": e.final.value,
                 "why": e.reviews[-1].explanation if e.reviews else ""}
                for e in recent[-10:]
            ],
        }
        if self.enabled and self._llm is not None:
            try:
                text = _call_with_timeout(
                    lambda: self._llm.complete(prompts.DAILY_NOTE_SYSTEM, prompts.daily_note_user_prompt(facts),
                                               json_schema=None, timeout=self.timeout_s),
                    self.timeout_s,
                )
                if text and text.strip():
                    return text.strip()
            except Exception:
                pass
        return fallback_note(facts)


def fallback_note(facts: dict) -> str:
    """Plain, deterministic note used when the AI is off or unavailable."""
    lines = [f"{facts['date']}: your account is worth ${facts['account_value']:,.2f} "
             f"(${facts['cash']:,.2f} in cash)."]
    if facts.get("you_vs_benchmark"):
        lines.append(facts["you_vs_benchmark"] + ".")
    decisions = facts.get("recent_decisions") or []
    if decisions:
        taken = sum(1 for d in decisions if d["outcome"] in ("approve", "resize"))
        lines.append(f"Recent decisions: {taken} trade(s) placed, {len(decisions) - taken} held back by safety checks.")
    else:
        lines.append("No trades recently. That's normal: the plan mostly waits.")
    lines.append("Nothing to do today unless your goals changed.")
    return " ".join(lines)


# --- A5: shadow outcomes of rejected intents ---------------------------------------------------


def update_shadow_returns(journal: Journal, closes_fn: Callable[[str], pd.Series | None], now: datetime) -> int:
    """For rejected (VETO/BLOCK) intents, record the price change over the following 20 trading days.

    Positive = the price rose after the rejection (for a vetoed BUY, a missed gain;
    for a vetoed SELL, a loss avoided). Only filled once the 20 days have passed.
    Returns how many entries were updated.
    """
    updated = 0
    candidates = [e for e in journal.query(final=[Verdict.VETO, Verdict.BLOCK])
                  if e.shadow_return_20d is None and e.ts < now - timedelta(days=SHADOW_DAYS)]
    by_symbol: dict[str, pd.Series | None] = {}
    for e in candidates:
        sym = e.intent.symbol
        if sym not in by_symbol:
            try:
                by_symbol[sym] = closes_fn(sym)
            except Exception:
                by_symbol[sym] = None
        s = by_symbol[sym]
        if s is None or s.empty:
            continue
        s = s.dropna().sort_index()
        idx = pd.DatetimeIndex(s.index)
        if idx.tz is not None:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        s = pd.Series(s.values, index=idx)
        start_day = pd.Timestamp(e.ts.date())
        after = s[s.index >= start_day]
        if len(after) <= SHADOW_DAYS:
            continue
        ret = float(after.iloc[SHADOW_DAYS] / after.iloc[0] - 1)
        journal.update_shadow(e.entry_id, ret)
        updated += 1
    return updated


# --- production LLM client -----------------------------------------------------------------------


class AnthropicLLMClient:
    """Direct Anthropic client behind the ``LLMClient`` interface (see DECISIONS.md, Phase 0).

    Sends no ``tools`` (A1). Review calls use structured JSON output; the
    advisor still validates the result itself. Refused requests fall back
    server-side to another model (``fallbacks="default"``); a refusal that
    survives the fallback raises, which the advisor treats as unavailable (A3).
    """

    def __init__(self, model: str = DEFAULT_MODEL, client: Any = None):
        self.model = model
        self._client = client

    def _get(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(max_retries=1)
        return self._client

    def complete(self, system: str, user: str, *, json_schema: dict | None, timeout: float) -> str:
        output_config: dict[str, Any] = {"effort": "low"}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        response = self._get().beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            timeout=timeout,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("advisor model declined the request")
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        if not text.strip():
            raise RuntimeError("empty advisor response")
        return text
