"""TaxGuard T1–T4 (SPEC §5.8)."""

from datetime import timedelta

import pytest

from lumisafe.journal import JournalEntry, new_entry_id
from lumisafe.tax.guard import TaxGuard
from lumisafe.types import ReviewResult, Side, Source, TaxLot, Verdict

from .helpers import T0, intent, snapshot


def sale(symbol="SPY", days_ago=10, price=90.0, qty=5):
    i = intent(symbol, Side.SELL, qty, at=T0 - timedelta(days=days_ago))
    return JournalEntry(new_entry_id(), i.created_at, i, [ReviewResult("risk", Verdict.APPROVE, "ok")],
                        Verdict.APPROVE, price, qty, None)


def guard(pnl=None):
    return TaxGuard({"VTI", "VXUS", "BND"}, realized_pnl=lambda eid: pnl)


SNAP = snapshot(cash=10_000, prices={"SPY": 95.0, "VTI": 100.0})


# --- T1 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("src,expected", [(Source.USER, Verdict.BLOCK), (Source.SATELLITE, Verdict.BLOCK),
                                          (Source.CORE, Verdict.WARN)])
def test_t1_wash_sale(src, expected):
    sym = "VTI" if src is Source.CORE else "SPY"
    r = guard(pnl=-50.0).review(intent(sym, Side.BUY, 1, source=src), SNAP, [sale(sym)])
    assert r.verdict is expected and "wash sale" in r.explanation


def test_t1_not_triggered_by_gains_old_sales_or_other_symbols():
    g = guard(pnl=+50.0)
    assert g.review(intent("SPY", Side.BUY, 1), SNAP, [sale()]).verdict is Verdict.APPROVE
    g = guard(pnl=-50.0)
    assert g.review(intent("SPY", Side.BUY, 1), SNAP, [sale(days_ago=31)]).verdict is Verdict.APPROVE
    assert g.review(intent("QQQ", Side.BUY, 1), SNAP, [sale()]).verdict is Verdict.APPROVE


def test_t1_falls_back_to_lot_cost_when_realized_pnl_unknown():
    lots = [TaxLot("SPY", 5, 120.0, T0 - timedelta(days=100))]
    snap = snapshot(cash=10_000, positions={"SPY": 5}, prices={"SPY": 95.0}, lots=lots)
    assert guard().review(intent("SPY", Side.BUY, 1), snap, [sale(price=90.0)]).verdict is Verdict.BLOCK
    assert guard().review(intent("SPY", Side.BUY, 1), SNAP, [sale(price=90.0)]).verdict is Verdict.APPROVE


# --- T2 --------------------------------------------------------------------------------------------

def test_t2_short_term_gain_warns_with_estimate():
    lots = [TaxLot("SPY", 10, 80.0, T0 - timedelta(days=100))]
    snap = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=lots)
    r = guard().review(intent("SPY", Side.SELL, 10), snap, [])
    assert r.verdict is Verdict.WARN
    assert "$200.00 of short-term gain" in r.explanation and "$34.00" in r.explanation  # 200 * (0.32-0.15)


def test_t2_long_term_losses_and_core_sales_do_not_warn():
    long = [TaxLot("SPY", 10, 80.0, T0 - timedelta(days=400))]
    snap = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=long)
    assert guard().review(intent("SPY", Side.SELL, 10), snap, []).verdict is Verdict.APPROVE
    loss = [TaxLot("SPY", 10, 120.0, T0 - timedelta(days=100))]
    snap2 = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=loss)
    assert guard().review(intent("SPY", Side.SELL, 10), snap2, []).verdict is Verdict.APPROVE
    core = [TaxLot("VTI", 10, 50.0, T0 - timedelta(days=100))]
    snap3 = snapshot(cash=0, positions={"VTI": 10}, prices={"VTI": 100.0}, lots=core)
    assert guard().review(intent("VTI", Side.SELL, 10, source=Source.CORE), snap3, []).verdict is Verdict.APPROVE


def test_t2_rates_are_configurable():
    lots = [TaxLot("SPY", 10, 80.0, T0 - timedelta(days=100))]
    snap = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=lots)
    g = TaxGuard(set(), short_term_rate=0.40, long_term_rate=0.20)
    assert "$40.00" in g.review(intent("SPY", Side.SELL, 10), snap, []).explanation


# --- T3 --------------------------------------------------------------------------------------------

def test_t3_highest_cost_lots_first_unless_r9_forbids():
    lots = [TaxLot("SPY", 5, 70.0, T0 - timedelta(days=100)),
            TaxLot("SPY", 5, 90.0, T0 - timedelta(days=100)),
            TaxLot("SPY", 5, 99.0, T0 - timedelta(days=2))]  # highest cost but too young (R9)
    snap = snapshot(cash=0, positions={"SPY": 15}, prices={"SPY": 100.0}, lots=lots)
    picked = guard().lots_for_sale(intent("SPY", Side.SELL, 6), snap)
    assert [(l.cost_basis, q) for l, q in picked] == [(90.0, 5), (70.0, 1)]
    # gain counted from the T3 lots: (100-90)*5 + (100-70)*1 = 80
    assert "$80.00 of short-term gain" in guard().review(intent("SPY", Side.SELL, 6), snap, []).explanation
    core_pick = guard().lots_for_sale(intent("SPY", Side.SELL, 6, source=Source.CORE), snap)
    assert core_pick[0][0].cost_basis == 99.0


# --- T4 --------------------------------------------------------------------------------------------

def test_t4_every_explanation_ends_with_not_tax_advice():
    lots = [TaxLot("SPY", 10, 80.0, T0 - timedelta(days=100))]
    cases = [
        (intent("SPY", Side.BUY, 1), SNAP, [sale()], -1.0),
        (intent("VTI", Side.BUY, 1, source=Source.CORE), SNAP, [sale("VTI")], -1.0),
        (intent("SPY", Side.BUY, 1), SNAP, [], None),
        (intent("SPY", Side.SELL, 10), snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=lots),
         [], None),
        (intent("SPY", Side.SELL, 1, source=Source.CORE), SNAP, [], None),
        (intent("ZZZ", Side.SELL, 1), SNAP, [], None),
        (intent("SPY" * 100, Side.BUY, 1), SNAP, [sale("SPY" * 100)], -1e9),
    ]
    for i, snap, sales, pnl in cases:
        r = guard(pnl).review(i, snap, sales)
        assert r.explanation.endswith("Not tax advice."), r.explanation
        assert len(r.explanation) <= 300
