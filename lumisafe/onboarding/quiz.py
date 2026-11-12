"""Onboarding quiz: plain-English questions → RiskProfile (SPEC §6).

``score()`` is pure and testable. ``run_quiz()`` handles the interactive
asking through an injected ``ask`` function so tests can script answers.
At most 8 questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

Goal = Literal["grow", "save_short", "learn"]
ProfileName = Literal["conservative", "balanced", "growth"]

SATELLITE_MAX = {"conservative": 0.0, "balanced": 0.05, "growth": 0.10}

# Q3: "If your $1,000 dropped to $X, would you sell?" → the first X they'd sell at.
LOSS_CHOICES = {"a": 900, "b": 800, "c": 700, "d": 600}


@dataclass(frozen=True)
class Answers:
    goal: Goal
    horizon_years: float
    sell_below_usd: int  # the $1,000 would be sold if it fell to this value
    monthly_usd: float
    emergency_fund: bool
    wants_strategies: bool
    lump_sum: bool = False  # invest the current balance at once instead of monthly

    @property
    def loss_tolerance(self) -> float:
        return (1000 - self.sell_below_usd) / 1000


@dataclass(frozen=True)
class RiskProfile:
    name: ProfileName
    satellite_max: float
    reasons: list[str] = field(default_factory=list)


def score(a: Answers) -> RiskProfile:
    reasons: list[str] = []
    if a.horizon_years < 3 or a.goal == "save_short":
        reasons.append("You need this money within 3 years, so a steadier mix is safer.")
        return RiskProfile("conservative", 0.0, reasons)
    tol = a.loss_tolerance
    if tol >= 0.30 and a.horizon_years >= 10:
        name: ProfileName = "growth"
        reasons.append("You have 10+ years and said you could sit through a 30%+ drop.")
    elif tol >= 0.20 and a.horizon_years >= 5:
        name = "balanced"
        reasons.append("You have 5+ years and could sit through a 20% drop.")
    else:
        name = "conservative"
        reasons.append("You'd sell after a smaller drop, so a steadier mix fits you better.")
    return RiskProfile(name, SATELLITE_MAX[name], reasons)


# --- interactive -----------------------------------------------------------------------------

QUESTIONS = {
    "goal": "1) What's your main goal?\n   [a] grow money over the long term\n"
            "   [b] save for something in less than 3 years\n   [c] mostly learn how investing works\n> ",
    "horizon": "2) How many years until you'll need most of this money? > ",
    "loss": "3) Say you invest $1,000 and it drops. At which value would you sell?\n"
            "   [a] $900  [b] $800  [c] $700  [d] $600 or lower / I'd hold\n> ",
    "monthly": "4) How much can you add each month, in dollars? (0 is fine) > ",
    "emergency": "5) Do you have an emergency fund (about 3-6 months of expenses) already? [y/n] > ",
    "strategies": "6) Would you like to experiment with tested strategies in a small, capped slice? [y/n] > ",
    "lump": "7) Invest your whole current balance right away instead of a little each month? [y/n] > ",
}


def _choice(raw: str, options: set[str]) -> str | None:
    v = raw.strip().lower()[:1]
    return v if v in options else None


def _number(raw: str) -> float | None:
    try:
        v = float(raw.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return v if v >= 0 else None


def _ask_until(ask: Callable[[str], str], prompt: str, parse, tries: int = 5):
    for _ in range(tries):
        value = parse(ask(prompt))
        if value is not None:
            return value
        prompt = "   Sorry, I didn't get that. " + prompt.split("\n")[-1]
    raise ValueError("Too many invalid answers; please run `lumisafe init` again.")


def run_quiz(ask: Callable[[str], str] = input) -> Answers:
    goal = {"a": "grow", "b": "save_short", "c": "learn"}[
        _ask_until(ask, QUESTIONS["goal"], lambda r: _choice(r, {"a", "b", "c"}))]
    horizon = _ask_until(ask, QUESTIONS["horizon"], _number)
    loss = LOSS_CHOICES[_ask_until(ask, QUESTIONS["loss"], lambda r: _choice(r, set(LOSS_CHOICES)))]
    monthly = _ask_until(ask, QUESTIONS["monthly"], _number)
    yn = lambda r: {"y": True, "n": False}.get(_choice(r, {"y", "n"}) or "")  # noqa: E731
    emergency = _ask_until(ask, QUESTIONS["emergency"], yn)
    strategies = _ask_until(ask, QUESTIONS["strategies"], yn)
    lump = _ask_until(ask, QUESTIONS["lump"], yn)
    return Answers(goal, horizon, loss, monthly, emergency, strategies, lump)
