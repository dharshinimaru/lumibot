"""BehaviorCoach B1–B5 (SPEC §5.6)."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from lumisafe.coach.behavior import (
    EXPIRY, BehaviorCoach, MarketContext, PendingStore, recovery_context, trailing_return,
)
from lumisafe.types import Side, Source, Verdict

from .helpers import T0, intent, snapshot

COACH = BehaviorCoach({"VTI", "VXUS", "BND"})
SNAP = snapshot(cash=10_000, positions={"VTI": 10}, prices={"VTI": 90.0, "MEME": 125.0, "AAPL": 100.0})


def closes(ret20: float, n=400, start=100.0):
    """A flat history whose last 20 trading days move by ``ret20``."""
    idx = pd.bdate_range(end=T0.date(), periods=n)
    vals = np.full(n, start)
    vals[-20:] = start * (1 + ret20 * np.arange(1, 21) / 20)
    return pd.Series(vals, index=idx)


def ctx(symbol, ret20, **kw):
    return MarketContext(closes={symbol: closes(ret20)}, **kw)


# --- B1 -------------------------------------------------------------------------------------------

def test_b1_panic_sell_of_core_etf_in_a_drop_is_delayed_24h():
    r = COACH.review(intent("VTI", Side.SELL, 1), SNAP, ctx("VTI", -0.12))
    assert r.verdict is Verdict.DELAY and r.release_at == T0 + timedelta(hours=24)
    assert "down 12%" in r.explanation and len(r.explanation) <= 300


def test_b1_not_triggered_for_small_drops_or_non_core_or_buys():
    assert COACH.review(intent("VTI", Side.SELL, 1), SNAP, ctx("VTI", -0.08)).verdict is Verdict.APPROVE
    assert COACH.review(intent("AAPL", Side.SELL, 1), SNAP, ctx("AAPL", -0.20)).verdict is Verdict.APPROVE
    assert COACH.review(intent("VTI", Side.BUY, 1), SNAP, ctx("VTI", -0.20)).verdict is Verdict.APPROVE


def test_b1_recovery_context_is_computed_from_data():
    # Two past -15% drops: the first recovers +20% a year later, the second is -5% a year later.
    idx = pd.bdate_range("2010-01-01", periods=1500)
    v = np.full(len(idx), 100.0)
    v[300:320] = np.linspace(100, 85, 20); v[320:] = 85; v[560:] = 102            # +20% a year after
    v[800:820] = np.linspace(102, 86.7, 20); v[820:] = 86.7; v[1060:] = 82.4      # ≈ -5% a year after
    s = pd.Series(v, index=idx)
    text = recovery_context(s, -0.12)
    assert text.startswith("Since 2010, it had 2 similar drops; a year later it was higher in 1 of 2")
    assert "median" in text


def test_b1_recovery_context_says_so_when_data_is_insufficient():
    assert "isn't enough price history" in recovery_context(closes(-0.2, n=100), -0.2)
    flat = pd.Series(100.0, index=pd.bdate_range("2010-01-01", periods=600))
    assert "no completed earlier drop" in recovery_context(flat, -0.12)


# --- B2 -------------------------------------------------------------------------------------------

def test_b2_chasing_a_hot_symbol_is_delayed():
    r = COACH.review(intent("MEME", Side.BUY, 1), SNAP, ctx("MEME", 0.30))
    assert r.verdict is Verdict.DELAY and "up 30%" in r.explanation and r.release_at == T0 + timedelta(hours=24)
    assert COACH.review(intent("MEME", Side.BUY, 1), SNAP, ctx("MEME", 0.20)).verdict is Verdict.APPROVE


# --- B3 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("prior,expected", [(0, Verdict.APPROVE), (1, Verdict.APPROVE), (2, Verdict.DELAY),
                                            (5, Verdict.DELAY)])
def test_b3_third_user_intent_in_a_day_is_delayed(prior, expected):
    r = COACH.review(intent("AAPL", Side.BUY, 1), SNAP, MarketContext(user_intents_today=prior))
    assert r.verdict is expected


# --- B4 -------------------------------------------------------------------------------------------

def test_b4_confirmed_reentry_is_not_delayed_again():
    i = intent("VTI", Side.SELL, 1)
    r = COACH.review(i, SNAP, ctx("VTI", -0.12, confirmed_intent_ids=frozenset({i.intent_id})))
    assert r.verdict is Verdict.APPROVE


def test_b4_pending_store_release_and_72h_expiry(tmp_path):
    store = PendingStore(tmp_path / "p.json")
    i = intent("VTI", Side.SELL, 1)
    p = store.add(i, T0 + timedelta(hours=24))
    assert p.expires_at == i.created_at + EXPIRY == T0 + timedelta(hours=72)
    assert store.get(i.intent_id[:8]).status == "pending"
    assert store.expire_old(T0 + timedelta(hours=71)) == []
    assert [x.intent.intent_id for x in store.expire_old(T0 + timedelta(hours=72))] == [i.intent_id]
    assert store.get(i.intent_id).status == "expired"
    assert store.get("nope") is None


# --- B5 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("side,ret,prior", [(Side.SELL, -0.5, 9), (Side.BUY, 2.0, 9), (Side.SELL, 0.0, 0)])
def test_b5_coach_never_blocks(side, ret, prior):
    r = COACH.review(intent("VTI", side, 1), SNAP, ctx("VTI", ret, user_intents_today=prior))
    assert r.verdict in (Verdict.APPROVE, Verdict.DELAY)


@pytest.mark.parametrize("src", [Source.CORE, Source.SATELLITE])
def test_coach_only_reviews_user_intents(src):
    r = COACH.review(intent("VTI", Side.SELL, 1, source=src), SNAP, ctx("VTI", -0.5, user_intents_today=9))
    assert r.verdict is Verdict.APPROVE


def test_trailing_return_and_missing_data():
    assert trailing_return(closes(0.1)) == pytest.approx(0.1)
    assert trailing_return(None) is None and trailing_return(closes(0.1)[-10:]) is None
    assert COACH.review(intent("AAPL", Side.BUY, 1), SNAP, None).verdict is Verdict.APPROVE
