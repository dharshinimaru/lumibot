"""RiskGate invariants R1–R11 (SPEC §5.3). P = property-based (hypothesis)."""

import ast
import inspect
import math
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lumisafe.config.schema import RiskConfig
from lumisafe.risk import gate as gate_module
from lumisafe.risk import limits as limits_module
from lumisafe.risk.gate import RiskGate
from lumisafe.types import OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, TaxLot, Verdict

from .helpers import T0, intent, snapshot

CORE = {"VTI", "VXUS", "BND"}
CFG = RiskConfig()  # defaults: stock 5%, etf 60%, 5 trades/week, 3% daily, 15% dd, 25% alert, 5 days
GATE = RiskGate(CFG, CORE)
SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def post_weight(i: OrderIntent, s: PortfolioSnapshot, r: ReviewResult) -> float:
    q = r.adjusted_quantity if r.verdict is Verdict.RESIZE else i.quantity
    return (s.positions.get(i.symbol, 0.0) + q) * s.prices[i.symbol] / s.equity


def executed_qty(i, r):
    if r.verdict is Verdict.RESIZE:
        return r.adjusted_quantity
    if r.verdict is Verdict.APPROVE:
        return i.quantity
    return 0.0


# --- strategies for property tests ---------------------------------------------------

money = st.floats(min_value=0.0, max_value=5e6, allow_nan=False, allow_infinity=False)
price = st.floats(min_value=0.01, max_value=5e4, allow_nan=False, allow_infinity=False)
qty = st.one_of(st.integers(min_value=1, max_value=100_000).map(float),
                st.floats(min_value=1e-4, max_value=1e5, allow_nan=False, allow_infinity=False))
sources = st.sampled_from(list(Source))


@st.composite
def buy_case(draw, symbols):
    sym = draw(st.sampled_from(symbols))
    p = draw(price)
    held = draw(st.floats(min_value=0, max_value=1e5, allow_nan=False, allow_infinity=False))
    cash = draw(money)
    other = draw(money)
    equity = cash + held * p + other
    s = snapshot(cash=cash, positions={sym: held, "OTHER": 1.0}, prices={sym: p, "OTHER": other},
                 equity=equity if equity > 0 else 1.0)
    i = intent(symbol=sym, side=Side.BUY, qty=draw(qty), source=draw(sources))
    return i, s


# --- R1 (P) -------------------------------------------------------------------------------

@SETTINGS
@given(buy_case(["AAPL", "TSLA", "ZZZZ"]))
def test_r1_property_single_stock_never_exceeds_cap(case):
    i, s = case
    r = GATE.check(i, s)
    if r.verdict in (Verdict.APPROVE, Verdict.RESIZE):
        assert post_weight(i, s, r) <= CFG.max_single_stock_weight


def test_r1_resizes_down_to_cap():
    s = snapshot(cash=100_000, prices={"AAPL": 100.0})
    r = GATE.check(intent("AAPL", qty=100), s)  # 100 * 100 = 10% of 100k
    assert r.verdict is Verdict.RESIZE and r.adjusted_quantity == 50


def test_r1_blocks_when_cap_gives_zero_shares():
    s = snapshot(cash=1_000, prices={"AAPL": 100.0})  # 5% of 1k = $50 < 1 share
    assert GATE.check(intent("AAPL", qty=1), s).verdict is Verdict.BLOCK


def test_r1_blocks_when_already_at_cap():
    s = snapshot(cash=95_000, positions={"AAPL": 50}, prices={"AAPL": 100.0})
    assert GATE.check(intent("AAPL", qty=1), s).verdict is Verdict.BLOCK


def test_r1_unknown_symbols_are_treated_as_stocks():
    s = snapshot(cash=100_000, prices={"NEWFUND": 100.0})
    r = GATE.check(intent("NEWFUND", qty=100), s)
    assert r.verdict is Verdict.RESIZE and r.adjusted_quantity == 50


def test_r1_fractional_orders_resize_fractionally():
    s = snapshot(cash=1_000, prices={"AAPL": 100.0})
    r = GATE.check(intent("AAPL", qty=1.5), s)
    assert r.verdict is Verdict.RESIZE and 0 < r.adjusted_quantity <= 0.5


# --- R2 (P) -------------------------------------------------------------------------------

@SETTINGS
@given(buy_case(["VTI", "BND", "SPY", "QQQ"]))
def test_r2_property_single_etf_never_exceeds_cap(case):
    i, s = case
    r = GATE.check(i, s)
    if r.verdict in (Verdict.APPROVE, Verdict.RESIZE):
        assert post_weight(i, s, r) <= CFG.max_single_etf_weight


def test_r2_core_etf_buy_resized_to_cap():
    s = snapshot(cash=50_000, positions={"VTI": 500}, prices={"VTI": 100.0})  # equity 100k, VTI 50%
    r = GATE.check(intent("VTI", qty=200, source=Source.CORE), s)
    assert r.verdict is Verdict.RESIZE and r.adjusted_quantity == 100


# --- R3 (P) -------------------------------------------------------------------------------

@SETTINGS
@given(buy_case(["VTI", "AAPL", "SPY"]))
def test_r3_property_cash_never_goes_negative(case):
    i, s = case
    r = GATE.check(i, s)
    assert s.cash - executed_qty(i, r) * s.prices[i.symbol] >= 0


def test_r3_blocks_unaffordable_order():
    s = snapshot(cash=500, positions={"BND": 1000}, prices={"BND": 100.0, "VTI": 100.0})
    r = GATE.check(intent("VTI", qty=10, source=Source.CORE), s)
    assert r.verdict is Verdict.BLOCK and "cash" in r.explanation.lower()


# --- R4 ---------------------------------------------------------------------------------------

def test_r4_cannot_sell_more_than_held():
    s = snapshot(cash=0, positions={"VTI": 3}, prices={"VTI": 100.0})
    for src in Source:
        assert GATE.check(intent("VTI", Side.SELL, 4, source=src), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("AAPL", Side.SELL, 1), snapshot(prices={"AAPL": 5.0})).verdict is Verdict.BLOCK
    assert GATE.check(intent("VTI", Side.SELL, 3, source=Source.CORE), s).verdict is Verdict.APPROVE


# --- R5 ---------------------------------------------------------------------------------------

def test_r5_weekly_trade_limit_blocks_satellite_and_user_buys_only():
    s = snapshot(cash=100_000, prices={"SPY": 100.0, "VTI": 100.0}, trades_this_week=5)
    assert GATE.check(intent("SPY", qty=1, source=Source.USER), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("SPY", qty=1, source=Source.SATELLITE), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("VTI", qty=1, source=Source.CORE), s).verdict is Verdict.APPROVE
    under = snapshot(cash=100_000, prices={"SPY": 100.0}, trades_this_week=4)
    assert GATE.check(intent("SPY", qty=1, source=Source.USER), under).verdict is Verdict.APPROVE


# --- R6 / R7 ----------------------------------------------------------------------------------

def sat_snap(sat_eq, day_start=10_000, peak=10_000):
    return snapshot(cash=100_000, positions={"QQQ": 10}, prices={"QQQ": 100.0, "VTI": 100.0},
                    satellite_equity=sat_eq, satellite_day_start_equity=day_start, satellite_peak_equity=peak)


def test_r6_daily_loss_halts_satellite_intents():
    s = sat_snap(9_700)  # -3% today
    assert GATE.check(intent("QQQ", qty=1, source=Source.SATELLITE), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("QQQ", Side.SELL, 1, source=Source.SATELLITE), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("VTI", qty=1, source=Source.CORE), s).verdict is Verdict.APPROVE
    assert GATE.halted(s)[Source.SATELLITE] is True
    ok = sat_snap(9_750)  # -2.5%
    assert GATE.check(intent("QQQ", qty=1, source=Source.SATELLITE), ok).verdict is Verdict.APPROVE
    assert GATE.halted(ok) == {Source.CORE: False, Source.SATELLITE: False, Source.USER: False}


def test_r7_drawdown_halts_satellite_intents():
    s = sat_snap(8_500, day_start=8_500, peak=10_000)  # flat today, -15% from peak
    r = GATE.check(intent("QQQ", qty=1, source=Source.SATELLITE), s)
    assert r.verdict is Verdict.BLOCK and "reset" in r.explanation
    assert GATE.check(intent("QQQ", qty=1, source=Source.USER), s).verdict is Verdict.APPROVE
    assert GATE.halted(s)[Source.SATELLITE] is True


# --- R8 ---------------------------------------------------------------------------------------

def test_r8_account_drawdown_never_sells_core_and_blocks_noncore_buys():
    s = snapshot(cash=10_000, positions={"VTI": 300, "BND": 300, "AAPL": 10},
                 prices={"VTI": 100.0, "BND": 100.0, "AAPL": 100.0},
                 peak_equity=100_000)  # equity 71k => -29%; VTI 42% (under the 60% cap)
    for src in Source:
        assert GATE.check(intent("VTI", Side.SELL, 1, source=src), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("AAPL", qty=1, source=Source.USER), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("QQQ", qty=1, source=Source.SATELLITE),
                      snapshot(cash=10_000, prices={"QQQ": 1.0}, peak_equity=100_000)).verdict is Verdict.BLOCK
    assert GATE.check(intent("VTI", qty=1, source=Source.CORE), s).verdict is Verdict.APPROVE
    assert GATE.check(intent("AAPL", Side.SELL, 1, source=Source.USER), s).verdict is Verdict.APPROVE
    assert GATE.halted(s) == {Source.CORE: False, Source.SATELLITE: True, Source.USER: True}


# --- R9 ---------------------------------------------------------------------------------------

def test_r9_young_lots_cannot_be_sold_except_by_core():
    young = [TaxLot("SPY", 10, 100.0, T0 - timedelta(days=2))]
    s = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=young)
    assert GATE.check(intent("SPY", Side.SELL, 1, source=Source.USER), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("SPY", Side.SELL, 1, source=Source.SATELLITE), s).verdict is Verdict.BLOCK
    assert GATE.check(intent("SPY", Side.SELL, 1, source=Source.CORE), s).verdict is Verdict.APPROVE


def test_r9_counts_trading_days_and_mixed_lots():
    lots = [TaxLot("SPY", 4, 100.0, T0 - timedelta(days=7)),   # Monday-to-Monday = 5 trading days
            TaxLot("SPY", 6, 100.0, T0 - timedelta(days=1))]
    s = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=lots)
    assert GATE.check(intent("SPY", Side.SELL, 4), s).verdict is Verdict.APPROVE
    assert GATE.check(intent("SPY", Side.SELL, 5), s).verdict is Verdict.BLOCK
    friday = [TaxLot("SPY", 10, 100.0, T0 - timedelta(days=3))]  # Fri -> Mon = 1 trading day
    s2 = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=friday)
    assert GATE.check(intent("SPY", Side.SELL, 1), s2).verdict is Verdict.BLOCK


def test_r9_untracked_holdings_count_as_aged():
    s = snapshot(cash=0, positions={"SPY": 10}, prices={"SPY": 100.0}, lots=[])
    assert GATE.check(intent("SPY", Side.SELL, 10), s).verdict is Verdict.APPROVE


# --- R10 --------------------------------------------------------------------------------------

@SETTINGS
@given(buy_case(["AAPL", "VTI", "SPY"]))
def test_r10_resize_is_strictly_smaller_and_positive(case):
    i, s = case
    r = GATE.check(i, s)
    if r.verdict is Verdict.RESIZE:
        assert 0 < r.adjusted_quantity < i.quantity
    else:
        assert r.adjusted_quantity is None


# --- R11 (P) ----------------------------------------------------------------------------------

anything = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=5),
                     st.just(float("nan")), st.just(float("inf")), st.just(-1.0))


@st.composite
def garbage_case(draw):
    i = OrderIntent(
        intent_id=draw(anything), created_at=draw(st.one_of(anything, st.just(T0))),
        source=draw(st.one_of(sources, anything)), symbol=draw(st.one_of(st.sampled_from(["VTI", "AAPL", ""]),
                                                                          anything)),
        side=draw(st.one_of(st.sampled_from(list(Side)), anything)), quantity=draw(st.one_of(qty, anything)),
        reason=draw(anything),
    )
    s = PortfolioSnapshot(
        as_of=draw(st.one_of(st.just(T0), anything)), cash=draw(st.one_of(money, anything)),
        equity=draw(st.one_of(money, anything)),
        positions=draw(st.one_of(st.dictionaries(st.sampled_from(["VTI", "AAPL"]), st.one_of(qty, anything)),
                                 anything)),
        prices=draw(st.one_of(st.dictionaries(st.sampled_from(["VTI", "AAPL"]), st.one_of(price, anything)),
                              anything)),
        peak_equity=draw(st.one_of(money, anything)), satellite_equity=draw(st.one_of(money, anything)),
        satellite_peak_equity=draw(st.one_of(money, anything)),
        satellite_day_start_equity=draw(st.one_of(money, anything)),
        trades_this_week=draw(st.one_of(st.integers(0, 10), anything)),
        lots=draw(st.one_of(st.just([]), st.lists(anything, max_size=2), anything)),
    )
    return draw(st.sampled_from([i, None, "x"])), draw(st.sampled_from([s, None, 3]))


@SETTINGS
@given(garbage_case())
def test_r11_property_never_raises_and_returns_one_result(case):
    i, s = case
    r = GATE.check(i, s)
    assert isinstance(r, ReviewResult)
    assert r.reviewer == "risk"
    assert r.verdict in (Verdict.APPROVE, Verdict.RESIZE, Verdict.BLOCK)
    if not isinstance(i, OrderIntent) or not isinstance(s, PortfolioSnapshot):
        assert r.verdict is Verdict.BLOCK
    halted = GATE.halted(s)
    assert set(halted) == set(Source)


@pytest.mark.parametrize("bad", [
    {"quantity": 0}, {"quantity": -1}, {"quantity": float("nan")}, {"quantity": float("inf")},
    {"quantity": True}, {"symbol": ""}, {"source": "user"}, {"side": "buy"},
])
def test_r11_malformed_intent_blocks(bad):
    fields = dict(intent_id="x", created_at=T0, source=Source.USER, symbol="VTI", side=Side.BUY,
                  quantity=1.0, reason="r")
    fields.update(bad)
    r = GATE.check(OrderIntent(**fields), snapshot(prices={"VTI": 10.0}))
    assert r.verdict is Verdict.BLOCK


@pytest.mark.parametrize("bad", [
    {"prices": {}}, {"prices": {"VTI": 0.0}}, {"equity": 0.0}, {"cash": -5.0},
    {"positions": {"VTI": -1.0}}, {"trades_this_week": -1}, {"lots": ["not a lot"]},
])
def test_r11_malformed_snapshot_blocks(bad):
    kw = dict(cash=1000.0, prices={"VTI": 10.0})
    kw.update(bad)
    r = GATE.check(intent("VTI", qty=1), snapshot(**kw))
    assert r.verdict is Verdict.BLOCK


def test_r11_internal_error_blocks(monkeypatch):
    monkeypatch.setattr(limits_module, "validate", lambda *a: 1 / 0)
    r = GATE.check(intent("VTI", qty=1), snapshot(prices={"VTI": 10.0}))
    assert r.verdict is Verdict.BLOCK and "internal error" in r.explanation


def test_c1_defense_in_depth_blocks_if_flags_were_bypassed():
    forged = RiskConfig.model_construct(**{**CFG.model_dump(), "allow_leverage": True})
    r = RiskGate(forged, CORE).check(intent("VTI", qty=1), snapshot(prices={"VTI": 10.0}))
    assert r.verdict is Verdict.BLOCK


# --- purity -------------------------------------------------------------------------------------

def test_gate_is_pure_no_io_llm_or_clock():
    banned_calls = {"now", "today", "utcnow", "time", "open", "urlopen", "request", "sleep", "random"}
    banned_imports = {"time", "random", "requests", "urllib", "socket", "sqlite3", "anthropic", "os",
                      "lumisafe.adapters", "lumisafe.advisor", "lumisafe.journal"}
    for mod in (gate_module, limits_module):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in banned_calls, f"{mod.__name__} calls {name}()"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                for n in names:
                    assert not any(n == b or n.startswith(b + ".") for b in banned_imports), n


def test_gate_is_deterministic():
    s = snapshot(cash=100_000, prices={"AAPL": 123.45})
    i = intent("AAPL", qty=77)
    assert len({GATE.check(i, s) for _ in range(20)}) == 1


def test_explanations_are_short_plain_english():
    s = snapshot(cash=100_000, prices={"AAPL": 100.0})
    r = GATE.check(intent("AAPL", qty=100), s)
    assert 0 < len(r.explanation) <= 300
    assert math.isfinite(r.adjusted_quantity)
