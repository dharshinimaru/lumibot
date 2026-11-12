"""RiskProfile → Plan: allocations plus a plain-English explanation (SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass

from lumisafe import DISCLAIMER
from lumisafe.config.schema import CoreConfig, LumiSafeConfig, RiskConfig, SatelliteConfig
from lumisafe.onboarding.quiz import Answers, RiskProfile

CORE_ETFS = {
    "conservative": {"VTI": 0.40, "VXUS": 0.20, "BND": 0.40},
    "balanced": {"VTI": 0.60, "VXUS": 0.20, "BND": 0.20},
    "growth": {"VTI": 0.70, "VXUS": 0.25, "BND": 0.05},
}
ETF_NAMES = {"VTI": "total US stock market", "VXUS": "international stocks", "BND": "US bonds"}
REBALANCE_BAND = 0.05

LOSS_NOTICE = (
    "Your money can lose value, including in the core plan. Past results don't guarantee future ones."
)


@dataclass(frozen=True)
class Plan:
    profile: RiskProfile
    config: LumiSafeConfig
    text: str


def etf_cap_for(etfs: dict[str, float], band: float = REBALANCE_BAND) -> float:
    """Largest core weight + its rebalance band, never below the 0.60 default (DECISIONS: Phase 1)."""
    return round(min(1.0, max(0.60, max(etfs.values()) + band)), 4)


def build_plan(profile: RiskProfile, answers: Answers) -> Plan:
    etfs = dict(CORE_ETFS[profile.name])
    satellite_on = bool(answers.wants_strategies and profile.satellite_max > 0)
    config = LumiSafeConfig(
        mode="paper",
        core=CoreConfig(
            etfs=etfs,
            monthly_contribution_usd=0.0 if answers.lump_sum else float(answers.monthly_usd),
            rebalance_band=REBALANCE_BAND,
        ),
        satellite=SatelliteConfig(
            enabled=satellite_on,
            max_weight=profile.satellite_max,
            # Paper mode runs PAPER-stage strategies so they can earn LIVE_ELIGIBLE (§8).
            allowed_states=["PAPER", "LIVE_ELIGIBLE"],
        ),
        risk=RiskConfig(max_single_etf_weight=etf_cap_for(etfs)),
    )
    return Plan(profile, config, render_text(profile, answers, config))


def render_text(profile: RiskProfile, answers: Answers, config: LumiSafeConfig) -> str:
    lines = [f"Your plan: {profile.name.upper()}", ""]
    lines += [f"Why: {r}" for r in profile.reasons]
    lines += ["", "Core (the part that does most of the work):"]
    for sym, w in config.core.etfs.items():
        lines.append(f"  {w:>4.0%}  {sym}  ({ETF_NAMES.get(sym, sym)})")
    if config.core.monthly_contribution_usd > 0:
        lines.append(f"Each month LumiSafe invests up to ${config.core.monthly_contribution_usd:,.0f}, "
                     "topping up whatever has fallen below its target.")
    else:
        lines.append("LumiSafe invests your available cash right away, then tops up whatever falls behind.")
    lines.append(f"It rebalances only when a fund drifts more than {config.core.rebalance_band:.0%} from target.")
    lines.append("")
    if config.satellite.enabled:
        lines.append(f"Satellite: up to {config.satellite.max_weight:.0%} for tested strategies. A strategy "
                     "only runs after passing a walk-forward backtest, and it's switched off automatically if "
                     "it trails simply holding the market.")
    else:
        lines.append("Satellite: off. Your plan is just the core; you can turn strategies on later.")
    if not answers.emergency_fund:
        lines += ["", "First things first: build an emergency fund (3-6 months of expenses) in a savings "
                      "account before investing real money. Paper trading is fine meanwhile."]
    if answers.goal == "learn":
        lines += ["", "Since you're here to learn, run `lumisafe today` daily for a plain-English note."]
    lines += ["", "Guardrails always on: no leverage, no shorting, no options, max 5% in any single stock, "
                  "limits on how often you can trade, and cooling-off delays on emotional trades."]
    lines += ["", LOSS_NOTICE, DISCLAIMER]
    return "\n".join(lines)
