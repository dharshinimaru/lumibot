"""CoreIndexStrategy (SPEC §7.1) and LotBook (T3 lot order)."""

from datetime import timedelta

from lumisafe.config.schema import CoreConfig
from lumisafe.portfolio.core import CASH_BUFFER_FRACTION, CoreIndexStrategy
from lumisafe.portfolio.lots import LotBook, select_lots
from lumisafe.types import Side, Source, TaxLot

from .helpers import T0, snapshot

PRICES = {"VTI": 100.0, "VXUS": 50.0, "BND": 80.0}
CFG = CoreConfig(etfs={"VTI": 0.6, "VXUS": 0.2, "BND": 0.2}, monthly_contribution_usd=1000)


def test_contribution_goes_to_most_underweight_first_and_never_sells():
    # 70/15/15 → VXUS and BND are underweight; $1000 shouldn't touch VTI
    s = snapshot(cash=5000, positions={"VTI": 70, "VXUS": 30, "BND": 18.75}, prices=PRICES)
    plan = CoreIndexStrategy(CFG).plan(s, investable_cash=5000, first_trading_day_of_month=True)
    assert all(i.side is Side.BUY and i.source is Source.CORE for i in plan.intents)
    assert {i.symbol for i in plan.intents} <= {"VXUS", "BND"}
    spent = sum(i.quantity * PRICES[i.symbol] for i in plan.intents)
    assert 990 <= spent <= 1000


def test_contribution_capped_by_investable_cash():
    s = snapshot(cash=300, prices=PRICES)
    plan = CoreIndexStrategy(CFG).plan(s, investable_cash=300, first_trading_day_of_month=True)
    assert sum(i.quantity * PRICES[i.symbol] for i in plan.intents) <= 300 * (1 - CASH_BUFFER_FRACTION) + 1e-9


def test_zero_contribution_means_lump_sum_of_all_investable_cash():
    cfg = CFG.model_copy(update={"monthly_contribution_usd": 0})
    s = snapshot(cash=100_000, prices=PRICES)
    plan = CoreIndexStrategy(cfg).plan(s, investable_cash=100_000, first_trading_day_of_month=True)
    spent = {i.symbol: i.quantity * PRICES[i.symbol] for i in plan.intents}
    assert abs(sum(spent.values()) - 98_000) < 50
    assert abs(spent["VTI"] / sum(spent.values()) - 0.6) < 0.01


def test_no_trades_mid_month_without_pending_rebalance():
    s = snapshot(cash=5000, prices=PRICES)
    assert CoreIndexStrategy(CFG).plan(s, investable_cash=5000, first_trading_day_of_month=False).intents == []


def test_rebalance_sells_only_when_contribution_cannot_fix_drift():
    # VTI at ~80% of core: way beyond the 5% band, a $1000 contribution can't fix it
    s = snapshot(cash=1000, positions={"VTI": 800, "VXUS": 200, "BND": 125}, prices=PRICES)
    plan = CoreIndexStrategy(CFG).plan(s, investable_cash=1000, first_trading_day_of_month=True)
    sells = [i for i in plan.intents if i.side is Side.SELL]
    assert [i.symbol for i in sells] == ["VTI"] and plan.rebalance_pending
    # small drift that the contribution fixes: no sells
    s2 = snapshot(cash=2000, positions={"VTI": 630, "VXUS": 380, "BND": 237.5}, prices=PRICES)  # VTI ~62%
    plan2 = CoreIndexStrategy(CFG).plan(s2, investable_cash=2000, first_trading_day_of_month=True)
    assert not [i for i in plan2.intents if i.side is Side.SELL]


def test_pending_rebalance_reinvests_next_cycle():
    s = snapshot(cash=20_000, positions={"VTI": 600, "VXUS": 100, "BND": 100}, prices=PRICES)
    plan = CoreIndexStrategy(CFG).plan(s, investable_cash=20_000, first_trading_day_of_month=False,
                                       rebalance_pending=True)
    assert plan.intents and all(i.side is Side.BUY for i in plan.intents) and not plan.rebalance_pending


def test_quarterly_rebalance_only_in_quarter_months():
    cfg = CFG.model_copy(update={"rebalance_check": "quarterly"})
    core = CoreIndexStrategy(cfg)
    assert core.is_rebalance_month(1) and core.is_rebalance_month(4) and not core.is_rebalance_month(2)
    s = snapshot(cash=0, positions={"VTI": 800, "VXUS": 200, "BND": 125}, prices=PRICES,
                 as_of=T0.replace(month=5))
    assert core.plan(s, investable_cash=0, first_trading_day_of_month=True).intents == []


def test_missing_prices_skip_cycle_and_whole_share_mode():
    s = snapshot(cash=1000, prices={"VTI": 100.0})
    assert CoreIndexStrategy(CFG).plan(s, investable_cash=1000, first_trading_day_of_month=True).intents == []
    s2 = snapshot(cash=1000, prices=PRICES)
    plan = CoreIndexStrategy(CFG, fractional=False).plan(s2, investable_cash=1000, first_trading_day_of_month=True)
    assert all(float(i.quantity).is_integer() for i in plan.intents)


def test_core_planner_never_reads_news_or_ai():
    import inspect

    import lumisafe.portfolio.core as core_mod
    src = inspect.getsource(core_mod)
    assert "advisor" not in src.lower() and "headline" not in src.lower()


# --- lots -----------------------------------------------------------------------------------

def test_select_lots_highest_cost_first_and_respects_holding_period():
    lots = [TaxLot("SPY", 5, 90.0, T0 - timedelta(days=30)), TaxLot("SPY", 5, 120.0, T0 - timedelta(days=30)),
            TaxLot("SPY", 5, 150.0, T0 - timedelta(days=1))]
    picked = select_lots(lots, "SPY", 7, T0, 5, respect_holding_period=True)
    assert [(l.cost_basis, q) for l, q in picked] == [(120.0, 5), (90.0, 2)]
    picked_core = select_lots(lots, "SPY", 7, T0, 5, respect_holding_period=False)
    assert [(l.cost_basis, q) for l, q in picked_core] == [(150.0, 5), (120.0, 2)]


def test_lotbook_add_consume_reconcile_round_trip():
    book = LotBook()
    book.add("VTI", 10, 100.0, T0 - timedelta(days=30))
    book.add("VTI", 5, 120.0, T0 - timedelta(days=1))
    book.consume("VTI", 6, T0, 5, respect_holding_period=True)  # young lot skipped → from the 100 lot
    assert sorted((l.cost_basis, l.quantity) for l in book.lots) == [(100.0, 4), (120.0, 5)]
    book.reconcile({"VTI": 6})  # sold 3 outside LumiSafe: cheapest go first
    assert sorted((l.cost_basis, l.quantity) for l in book.lots) == [(100.0, 1), (120.0, 5)]
    book.reconcile({})
    assert book.lots == []
    again = LotBook.from_list(LotBook([TaxLot("A", 1, 2.0, T0)]).to_list())
    assert again.lots == [TaxLot("A", 1, 2.0, T0)]


def test_lotbook_consume_falls_back_when_aged_lots_insufficient():
    book = LotBook([TaxLot("VTI", 2, 100.0, T0 - timedelta(days=1))])
    book.consume("VTI", 1, T0, 5, respect_holding_period=True)
    assert book.lots[0].quantity == 1
