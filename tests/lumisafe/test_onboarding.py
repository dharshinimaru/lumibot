"""Onboarding quiz and plan (SPEC §6)."""

import pytest

from lumisafe import DISCLAIMER
from lumisafe.onboarding import quiz
from lumisafe.onboarding.plan import CORE_ETFS, build_plan, etf_cap_for
from lumisafe.onboarding.quiz import Answers, run_quiz, score


def answers(**kw):
    base = dict(goal="grow", horizon_years=20, sell_below_usd=600, monthly_usd=200, emergency_fund=True,
                wants_strategies=True, lump_sum=False)
    base.update(kw)
    return Answers(**base)


@pytest.mark.parametrize("kw,expected", [
    ({}, "growth"),
    ({"sell_below_usd": 800, "horizon_years": 7}, "balanced"),
    ({"sell_below_usd": 700, "horizon_years": 7}, "balanced"),
    ({"sell_below_usd": 900}, "conservative"),
    ({"horizon_years": 4, "sell_below_usd": 600}, "conservative"),
    ({"goal": "learn", "horizon_years": 12}, "growth"),
])
def test_profile_scoring(kw, expected):
    assert score(answers(**kw)).name == expected


@pytest.mark.parametrize("kw", [{"horizon_years": 2.5}, {"goal": "save_short"}, {"horizon_years": 0}])
def test_short_horizon_forces_conservative_and_no_satellite(kw):
    p = score(answers(**kw))
    assert p.name == "conservative" and p.satellite_max == 0
    plan = build_plan(p, answers(**kw))
    assert plan.config.satellite.enabled is False and plan.config.satellite.max_weight == 0


@pytest.mark.parametrize("name,sat", [("conservative", 0.0), ("balanced", 0.05), ("growth", 0.10)])
def test_plan_matches_profile_table(name, sat):
    kw = {"conservative": {"sell_below_usd": 900}, "balanced": {"sell_below_usd": 800, "horizon_years": 6},
          "growth": {}}[name]
    plan = build_plan(score(answers(**kw)), answers(**kw))
    assert plan.profile.name == name
    assert plan.config.core.etfs == CORE_ETFS[name]
    assert plan.config.satellite.max_weight == sat
    assert plan.config.mode == "paper"
    # no core weight is ever choked by the single-ETF cap
    assert plan.config.risk.max_single_etf_weight >= max(CORE_ETFS[name].values())


def test_plan_text_states_losses_and_past_results_and_disclaimer():
    plan = build_plan(score(answers()), answers())
    assert "lose value" in plan.text
    assert "Past results don't guarantee future ones" in plan.text
    assert DISCLAIMER in plan.text


def test_no_emergency_fund_recommends_building_one_but_allows_paper():
    plan = build_plan(score(answers(emergency_fund=False)), answers(emergency_fund=False))
    assert "emergency fund" in plan.text and plan.config.mode == "paper"


def test_satellite_off_unless_requested_and_lump_sum_mode():
    a = answers(wants_strategies=False, lump_sum=True)
    plan = build_plan(score(a), a)
    assert plan.config.satellite.enabled is False
    assert plan.config.core.monthly_contribution_usd == 0


def test_etf_cap_rule():
    assert etf_cap_for(CORE_ETFS["balanced"]) == 0.65
    assert etf_cap_for(CORE_ETFS["growth"]) == 0.75
    assert etf_cap_for(CORE_ETFS["conservative"]) == 0.60


def test_interactive_quiz_reprompts_and_has_at_most_8_questions():
    replies = iter(["z", "a", "abc", "15", "d", "$1,200", "maybe", "y", "n", "y"])
    prompts = []

    def ask(p):
        prompts.append(p)
        return next(replies)

    a = run_quiz(ask)
    assert a == Answers("grow", 15.0, 600, 1200.0, True, False, True)
    assert len(quiz.QUESTIONS) <= 8


def test_quiz_gives_up_after_too_many_invalid_answers():
    with pytest.raises(ValueError):
        run_quiz(lambda p: "???")
