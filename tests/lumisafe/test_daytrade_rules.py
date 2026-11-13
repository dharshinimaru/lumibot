"""The day-trading amendment is scoped to ONE named, paper-only satellite strategy.

Only ``INTRADAY_STRATEGY`` skips R5 (weekly cap) and R9 (5-day minimum hold) and
gets a wash-sale WARN instead of BLOCK. Every other source and every other
satellite strategy keeps the original SPEC rules, and the rest of the gate
(R1-R4, R6-R8, R10, R11) still applies to the intraday strategy.
"""

from datetime import timedelta

from lumisafe.config.schema import RiskConfig
from lumisafe.daytrade import INTRADAY_STRATEGIES, INTRADAY_STRATEGY
from lumisafe.journal import JournalEntry, new_entry_id
from lumisafe.risk.gate import RiskGate
from lumisafe.tax.guard import TaxGuard
from lumisafe.types import ReviewResult, Side, Source, TaxLot, Verdict

from .helpers import T0, intent, snapshot

CORE = {"VTI", "VXUS", "BND"}
GATE = RiskGate(RiskConfig(), CORE, intraday_strategies=INTRADAY_STRATEGIES)
DT = dict(source=Source.SATELLITE, strategy_name=INTRADAY_STRATEGY)


def snap(**kw):
    base = dict(cash=100_000, positions={"AAPL": 10}, prices={"AAPL": 100.0, "VTI": 100.0},
                satellite_equity=10_000, satellite_day_start_equity=10_000, satellite_peak_equity=10_000)
    base.update(kw)
    return snapshot(**base)


def bought_today(s):
    return snap(lots=[TaxLot("AAPL", 10, 100.0, T0 - timedelta(hours=1))], **s)


def test_same_day_sell_allowed_only_for_the_intraday_strategy():
    s = bought_today({})
    assert GATE.check(intent("AAPL", Side.SELL, 10, **DT), s).verdict is Verdict.APPROVE
    for other in (dict(source=Source.SATELLITE, strategy_name="trend_filter"), dict(source=Source.USER)):
        assert GATE.check(intent("AAPL", Side.SELL, 10, **other), s).verdict is Verdict.BLOCK  # R9 still applies


def test_weekly_cap_skipped_only_for_the_intraday_strategy():
    s = snap(trades_this_week=5)
    assert GATE.check(intent("AAPL", Side.BUY, 1, **DT), s).verdict is Verdict.APPROVE
    assert GATE.check(intent("AAPL", Side.BUY, 1, source=Source.SATELLITE, strategy_name="trend_filter"),
                      s).verdict is Verdict.BLOCK
    assert GATE.check(intent("AAPL", Side.BUY, 1, source=Source.USER), s).verdict is Verdict.BLOCK


def test_gate_without_the_amendment_list_treats_it_like_any_satellite_strategy():
    plain = RiskGate(RiskConfig(), CORE)
    assert plain.check(intent("AAPL", Side.SELL, 10, **DT), bought_today({})).verdict is Verdict.BLOCK


def test_other_rules_still_apply_to_day_trades():
    # R4: no short sales
    assert GATE.check(intent("AAPL", Side.SELL, 11, **DT), snap()).verdict is Verdict.BLOCK
    # R1: single stock capped at 5% of the account
    r = GATE.check(intent("AAPL", Side.BUY, 100, **DT), snap())
    assert r.verdict is Verdict.RESIZE and r.adjusted_quantity < 100
    # R3: never borrows
    assert GATE.check(intent("MSFT", Side.BUY, 10, **DT), snap(cash=50.0, prices={"MSFT": 100.0, "AAPL": 100.0})
                      ).verdict is Verdict.BLOCK
    # R6: satellite daily loss halt
    assert GATE.check(intent("AAPL", Side.BUY, 1, **DT), snap(satellite_equity=9_600)).verdict is Verdict.BLOCK
    # R7: satellite drawdown halt
    assert GATE.check(intent("AAPL", Side.BUY, 1, **DT),
                      snap(satellite_equity=8_400, satellite_day_start_equity=8_400)).verdict is Verdict.BLOCK
    # R8: account drawdown alert blocks new buys
    assert GATE.check(intent("AAPL", Side.BUY, 1, **DT), snap(peak_equity=200_000)).verdict is Verdict.BLOCK


def _loss_sale(sym="AAPL"):
    i = intent(sym, Side.SELL, 5, at=T0 - timedelta(days=2), **DT)
    return JournalEntry(new_entry_id(), i.created_at, i, [ReviewResult("risk", Verdict.APPROVE, "ok")],
                        Verdict.APPROVE, 90.0, 5, None)


def test_wash_sale_warns_for_the_intraday_strategy_and_still_blocks_others():
    g = TaxGuard(CORE, realized_pnl=lambda eid: -40.0, intraday_strategies=INTRADAY_STRATEGIES)
    s = snapshot(cash=10_000, prices={"AAPL": 95.0})
    r = g.review(intent("AAPL", Side.BUY, 1, **DT), s, [_loss_sale()])
    assert r.verdict is Verdict.WARN and "wash sale" in r.explanation and r.explanation.endswith("Not tax advice.")
    other = g.review(intent("AAPL", Side.BUY, 1, source=Source.SATELLITE, strategy_name="trend_filter"), s,
                     [_loss_sale()])
    assert other.verdict is Verdict.BLOCK


def test_halts_block_new_day_trades_but_never_their_exits():
    halted = snap(satellite_equity=9_600, lots=[TaxLot("AAPL", 10, 100.0, T0 - timedelta(hours=1))])  # R6 hit
    assert GATE.check(intent("AAPL", Side.BUY, 1, **DT), halted).verdict is Verdict.BLOCK
    assert GATE.check(intent("AAPL", Side.SELL, 10, **DT), halted).verdict is Verdict.APPROVE
    other = intent("AAPL", Side.SELL, 10, source=Source.SATELLITE, strategy_name="trend_filter")
    assert GATE.check(other, halted).verdict is Verdict.BLOCK  # original R6 behaviour for everyone else


def test_intraday_trades_get_their_own_review_prompt():
    from lumisafe.advisor import prompts
    from lumisafe.advisor.advisor import AdvisorContext, AIAdvisor

    seen = {}

    class LLM:
        def complete(self, system, user, *, json_schema, timeout):
            seen["system"] = system
            return '{"verdict": "veto", "reasons": ["roundup"], "confidence": 0.7, "plain_english": "No company news."}'

    adv = AIAdvisor(LLM(), enabled=True)
    ctx = AdvisorContext("AAPL", "buy", 5, "satellite", "breakout", headlines=["Top 10 movers"], kind="intraday")
    r = adv.review(intent("AAPL", Side.BUY, 5, **DT), ctx)
    assert seen["system"] == prompts.INTRADAY_REVIEW_SYSTEM and r.verdict is Verdict.VETO
    adv.review(intent("AAPL", Side.BUY, 5, **DT), AdvisorContext("AAPL", "buy", 5, "satellite", "x"))
    assert seen["system"] == prompts.REVIEW_SYSTEM

    class Down:
        def complete(self, *a, **k):
            raise TimeoutError("slow")

    r = AIAdvisor(Down(), enabled=True).review(intent("AAPL", Side.BUY, 5, **DT), ctx)
    assert r.verdict is Verdict.VETO and r.explanation == "advisor unavailable"  # fail-closed (A3)
