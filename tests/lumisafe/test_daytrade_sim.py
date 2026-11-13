"""Day-trading lab: every cost and rule that changes the account's numbers (synthetic fixtures only)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from lumisafe.config.schema import DayTradeConfig, LumiSafeConfig
from lumisafe.daytrade.dataset import Candidate
from lumisafe.daytrade.report import apply_taxes, taxable_events, yearly_tax
from lumisafe.daytrade.sim import AccountState, Costs, Params, Trade, prepare, simulate_day, DayResult

NY = ZoneInfo("America/New_York")
DAY = date(2024, 3, 5)
OPEN = datetime(2024, 3, 5, 9, 30, tzinfo=NY)
CLOSE = datetime(2024, 3, 5, 16, 0, tzinfo=NY)
P = Params(min_gap=0.02, min_rel_volume=0.05, target_r=1.0)
NO_SLIP = Costs(slippage=0.0, stop_slippage=0.0, sec_fee_per_dollar=0.0, taf_per_share=0.0)


def bars(path, open_=OPEN, close=CLOSE, volume=100_000):
    """Minute bars: ``path`` maps minute offset -> (open, high, low, close); other minutes flat at the last close."""
    idx = pd.date_range(open_, close - timedelta(minutes=1), freq="1min")
    rows, last = [], 100.0
    for i in range(len(idx)):
        if i in path:
            o, h, l, c = path[i]
        else:
            o = h = l = c = last
        rows.append((o, h, l, c, volume))
        last = c
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def opening_range(lo=99.0, hi=101.0):
    """First 15 minutes trade between lo and hi."""
    return {i: (100.0, hi if i == 3 else 100.0, lo if i == 5 else 100.0, 100.0) for i in range(15)}


def cand(sym="ABC", spread=0.001, gap=0.03, avg_vol=1_000_000, open_=OPEN, close=CLOSE, headlines=None):
    return Candidate(open_.date().isoformat(), sym, open_.isoformat(), close.isoformat(), 97.0, 100.0, gap,
                     avg_vol, headlines or [{"at": "2024-03-05T12:00:00+00:00", "headline": "ABC beats",
                                             "source": "x", "n_symbols": 1}], spread, True)


def acct(slice_equity=10_000.0, other=90_000.0, **kw):
    return AccountState(slice_equity, other, **kw)


CFG = DayTradeConfig(enabled=True)


def run(path, *, costs=NO_SLIP, params=P, cfg=CFG, a=None, c=None, **bk):
    b = bars(path, **bk)
    return simulate_day(DAY, [c or cand()], {"ABC": b}, params, cfg, costs, a or acct())


def test_breakout_entry_and_take_profit_with_spread():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4), 30: (101.4, 104.0, 101.4, 103.9)}
    res, after = run(path)
    t = res.trades[0]
    trigger = 101.01
    assert t.entry_price == pytest.approx(trigger * 1.0005)  # buy-stop pays half the 0.10% spread
    stop = 99.0
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(t.entry_price + (t.entry_price - stop))  # limit: no spread on exit
    risk = t.entry_price - stop + t.entry_price * 0.0005
    assert t.shares == min(int(25 / risk), int(0.34 * 10_000 / t.entry_price))  # 0.25% of the slice at risk
    assert after.slice_equity == pytest.approx(10_000 + t.net_pnl)


def test_stop_gaps_through_and_fills_at_the_open_minus_costs():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4), 40: (97.0, 97.2, 96.5, 97.0)}
    costs = Costs(slippage=0.0002, stop_slippage=0.0005, sec_fee_per_dollar=27.8e-6, taf_per_share=0.000166)
    res, _ = run(path, costs=costs)
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(97.0 * (1 - 0.0005 - 0.0005))  # opened below the 99 stop
    assert t.fees == pytest.approx(t.shares * t.exit_price * 27.8e-6 + t.shares * 0.000166)
    assert t.spread_cost > 0 and t.slippage_cost > 0
    assert t.net_pnl < t.shares * (99.0 - t.entry_price)  # worse than the stop price


def test_bar_touching_stop_and_target_counts_as_stop():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4), 25: (101.4, 110.0, 90.0, 101.0)}
    res, _ = run(path)
    assert res.trades[0].exit_reason == "stop"


def test_everything_sold_before_the_close_including_half_days():
    close = datetime(2024, 3, 5, 13, 0, tzinfo=NY)
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}
    res, _ = run(path, c=cand(close=close), close=close)
    t = res.trades[0]
    assert t.exit_reason == "close"
    assert pd.Timestamp(t.exit_ts) == pd.Timestamp(datetime(2024, 3, 5, 12, 50, tzinfo=NY))


def test_no_entry_after_the_entry_window_or_without_a_breakout():
    late = opening_range() | {100: (101.0, 102.0, 101.0, 101.5)}  # 09:30 + 100 min > 90 min window
    assert run(late)[0].trades == []
    assert run(opening_range())[0].trades == []


def test_daily_loss_halt_sells_everything_and_stops_for_the_day():
    cfg = DayTradeConfig(enabled=True, daily_loss_halt=0.001, max_positions=3)  # $10 on a $10k slice
    b = {}
    for sym in ("AAA", "BBB", "CCC"):
        b[sym] = bars(opening_range() | {20: (101.0, 101.5, 101.0, 101.4), 22: (101.4, 101.4, 99.5, 99.6)})
    late = bars(opening_range() | {60: (101.0, 101.5, 101.0, 101.4)})
    b["DDD"] = late
    cands = [cand(s) for s in ("AAA", "BBB", "CCC", "DDD")]
    res, _ = simulate_day(DAY, cands, b, P, cfg, NO_SLIP, acct())
    assert res.halted
    assert {t.exit_reason for t in res.trades} == {"halt"}
    assert "DDD" not in {t.symbol for t in res.trades}  # no new entries after the halt


def test_pattern_day_trader_rule_under_25k_margin():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}
    small = acct(slice_equity=2_000.0, other=18_000.0, recent_day_trades=[1, 1, 1, 0])
    res, _ = run(path, a=small)
    assert res.trades == [] and res.skipped.get("pattern-day-trader limit") == 1
    # over $25k the rule doesn't apply
    res, _ = run(path, a=acct(recent_day_trades=[5, 5, 5, 5]))
    assert len(res.trades) == 1


def test_cash_account_cannot_reuse_unsettled_proceeds():
    cfg = DayTradeConfig(enabled=True, account_type="cash")
    a = acct(unsettled=[(date(2024, 3, 6), 9_950.0)])  # yesterday's sale settles tomorrow
    res, _ = run(opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}, cfg=cfg, a=a)
    assert res.trades == [] and res.skipped.get("too small for a whole share") == 1
    # settled cash is usable, and today's proceeds go to "unsettled" (T+1 since 2024-05-28; T+2 before)
    res, after = run(opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}, cfg=cfg)
    assert len(res.trades) == 1 and after.unsettled and after.unsettled[0][0] == date(2024, 3, 7)


def test_volume_cap_makes_a_partial_fill():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}
    res, _ = run(path, volume=1_000, c=cand(avg_vol=100_000))  # 1% of 1,000 shares = 10 shares
    t = res.trades[0]
    assert t.shares == 10 and t.partial


def test_filters_gap_rel_volume_and_focused_news():
    r = DayResult("x")
    b = {"ABC": bars(opening_range())}
    assert prepare([cand(gap=0.01)], b, P, r) == []  # gap too small
    assert prepare([cand(avg_vol=1e12)], b, P, r) == []  # not enough early volume
    roundup = [{"at": "2024-03-05T12:00:00+00:00", "headline": "Top 10 movers", "source": "x", "n_symbols": 10}]
    assert prepare([cand(headlines=roundup)], b, Params(focused_news=True, min_rel_volume=0.05), r) == []
    assert len(prepare([cand(headlines=roundup)], b, P, r)) == 1
    assert prepare([cand(spread=None)], b, P, r) == [] and r.skipped["no quotes"] == 1


def test_setup_uses_only_the_opening_range_no_look_ahead():
    r = DayResult("x")
    early = bars(opening_range())
    late_spike = bars(opening_range() | {200: (100.0, 500.0, 1.0, 100.0)})
    a = prepare([cand()], {"ABC": early}, P, r)[0]
    b = prepare([cand()], {"ABC": late_spike}, P, r)[0]
    assert (a.trigger, a.or_low, a.rel_volume) == (b.trigger, b.or_low, b.rel_volume)


def test_never_borrows_and_never_holds_overnight():
    path = opening_range() | {20: (101.0, 101.5, 101.0, 101.4)}
    res, after = run(path, cfg=DayTradeConfig(enabled=True, max_position_fraction=0.5, risk_per_trade=0.01))
    t = res.trades[0]
    assert t.shares * t.entry_price <= 10_000
    assert all(pd.Timestamp(t.exit_ts).date() == DAY for t in res.trades)


# --- taxes -----------------------------------------------------------------------------------------------


def trade(day, pnl, sym="ABC"):
    ts = f"{day}T10:00:00-05:00"
    return Trade(day, sym, ts, f"{day}T11:00:00-05:00", 1, 100.0, 100.0 + pnl, "close", 0.0, 0.0, 0.0, False)


def test_wash_sale_moves_a_december_loss_into_january():
    events = taxable_events([trade("2024-12-20", -500), trade("2025-01-06", 100)])
    assert events == [(date(2025, 1, 6), -400)]
    # no re-entry within 30 days: the loss stays in its own year
    events = taxable_events([trade("2024-11-01", -500), trade("2025-01-06", 100)])
    assert sorted(events) == [(date(2024, 11, 1), -500), (date(2025, 1, 6), 100)]
    # a different stock is not a wash sale
    events = taxable_events([trade("2024-12-20", -500), trade("2025-01-06", 100, "XYZ")])
    assert sorted(events) == [(date(2024, 12, 20), -500), (date(2025, 1, 6), 100)]


def test_yearly_tax_loss_limit_and_carryforward():
    tax = yearly_tax([(date(2023, 6, 1), -10_000), (date(2024, 6, 1), 10_000)], 0.30)
    assert tax[2023] == pytest.approx(-900)  # $3,000 deduction at 30%
    assert tax[2024] == pytest.approx(0.30 * (10_000 - 7_000))  # $7,000 carried forward


def test_taxes_reduce_returns_on_the_last_day_of_the_year():
    idx = pd.bdate_range("2024-12-30", "2025-01-02")
    r = pd.Series([0.01, 0.0, 0.0, 0.0], index=idx)
    out = apply_taxes(r, {2024: 30.0}, 10_000.0)
    end = 10_000 * float((1 + out).prod())
    assert end == pytest.approx(10_000 * 1.01 - 30)


def test_daytrade_config_defaults_and_limits():
    c = LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 1.0}, "monthly_contribution_usd": 0}})
    assert c.daytrade.enabled is False and c.daytrade.slice_fraction == 0.10
    assert c.daytrade.daily_loss_halt == 0.01
    with pytest.raises(Exception):
        DayTradeConfig(slice_fraction=0.5)
    with pytest.raises(Exception):
        DayTradeConfig(daily_loss_halt=0.10)
