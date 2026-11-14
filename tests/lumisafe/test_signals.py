"""Signal research engine: headline parsing, timing (no look-ahead), costs, selection (synthetic fixtures)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from lumisafe.signals import study
from lumisafe.signals.events import Event, parse, parse_all

NY = ZoneInfo("America/New_York")


def item(h, syms=("X",), at="2019-04-24T20:05:00+00:00"):
    return {"created_at": at, "headline": h, "symbols": list(syms)}


@pytest.mark.parametrize("headline,expected", [
    ("Ford Motor Q1 Adj. EPS $0.44 Beats $0.26 Estimate, Sales $40.3B Beat $37.08B Estimate",
     [("earnings_beat", 0.692), ("sales_beat", None)]),
    ("Coinbase Global Q2 EPS $(4.98) Misses $(2.68) Estimate, Sales $802.60M Miss $830.52M Estimate",
     [("earnings_miss", -0.858), ("sales_miss", None)]),
    ("JP Morgan Upgrades Twitter to Overweight, Raises Price Target to $65", [("upgrade", "overweight"),
                                                                             ("pt_raise", 65.0)]),
    ("KeyBanc Downgrades Netflix to Sector Weight", [("downgrade", "sector weight")]),
    ("RBC Capital Initiates Coverage On Micron Technology with Outperform Rating, Announces $80 Price Target",
     [("initiate_bullish", "outperform")]),
    ("Adobe Sees FY23 Adj. EPS $15.65-$15.75 vs $15.50 Est., Total Revenue $19.25B-$19.35B vs $19.3B Est.",
     [("guide_up", 0.013)]),
    ("Intel Sees Q2 Adj. EPS $0.89 vs $1.01 Estimate, Sales $15.6B vs $16.85B Estimate", [("guide_down", -0.119)]),
    ("Abbott Raises FY18 Guidance", [("guide_up", None)]),
    # 2016-2017 formats
    ("UPDATE: 3M Q4 Adj. EPS $1.80 vs $1.63 Est.", [("earnings_beat", 0.104)]),
    ("Yum Reports Q1 EPS $0.95 vs. Est. $0.83, Rev. $2.619B vs. Est. $2.66B",
     [("earnings_beat", 0.145), ("sales_miss", None)]),
    ("Union Pacific Reports Q4 EPS $1.31 vs $1.42 Est., Sales $5.2B vs $5.45B Est.",
     [("earnings_miss", -0.077), ("sales_miss", None)]),
    ("UPDATE: Medtronic Q4 Adjusted EPS $1.06 vs $1.06 est", [("earnings_inline", 0.0)]),
    ("Tesla Reports Q2 Adj. EPS $(1.06) vs $(1.02) Est.", [("earnings_miss", -0.039)]),
    ("Cowen &amp; Company Maintains Market Perform on McDonald's, Raises PT to $127.00", [("pt_raise", 127.0)]),
    ("Ford Q1 Adj. EPS $0.44 Up From $0.30 YoY", []),
    ("UPDATE: Wal Mart Raises FY16 Adj. EPS to $4.15-$4.35 vs $4.27 Est.", [("guide_up", None)]),
    ("Benzinga's Top Upgrades, Downgrades For April 20, 2018", []),
    ("Oracle Misses Q4 Earnings — Stock Surges On Booming AI Demand", []),
])
def test_headlines_become_events(headline, expected):
    got = [(e.kind, round(e.value, 3) if isinstance(e.value, float) else e.value) for e in parse(item(headline))]
    assert got == expected


def test_multi_ticker_headlines_and_duplicates_are_ignored():
    h = "Ford Motor Q1 Adj. EPS $0.44 Beats $0.26 Estimate"
    assert parse(item(h, syms=("F", "GM"))) == []
    assert len(parse_all([item(h), item(h)])) == 1  # re-published headline counts once


# --- a tiny synthetic market ----------------------------------------------------------------------------------

DAYS = [d.date() for d in pd.bdate_range("2019-01-01", "2020-12-31")]


def opens(d):
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)


def market(path=None):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in DAYS])
    px = np.full(len(idx), 100.0)
    bars = {"SPY": pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1e6}, index=idx),
            "ABC": pd.DataFrame({"open": px.copy(), "high": px, "low": px, "close": px.copy(), "volume": 1e6},
                                index=idx)}
    for d, (o, c) in (path or {}).items():
        bars["ABC"].loc[pd.Timestamp(d), ["open", "close"]] = [o, c]
    return study.Market(bars, DAYS)


EVERYONE = lambda d: {"ABC"}  # noqa: E731


def test_news_is_traded_only_at_the_first_open_after_it_was_public():
    mkt = market()
    pre = datetime(2020, 3, 3, 8, 0, tzinfo=NY)
    late = datetime(2020, 3, 3, 9, 27, tzinfo=NY)  # 9:27 + 5 min > 9:30 open
    during = datetime(2020, 3, 3, 11, 0, tzinfo=NY)
    assert mkt.next_session_on_or_after(pre, opens) == date(2020, 3, 3)
    assert mkt.next_session_on_or_after(late, opens) == date(2020, 3, 4)
    assert mkt.next_session_on_or_after(during, opens) == date(2020, 3, 4)


def ev(kind, value, at, h="ABC Q1 EPS $1.10 Beats $1.00 Estimate"):
    return Event("ABC", at, kind, value, h)


def test_price_confirmed_signal_enters_the_day_after_confirmation():
    d0 = date(2020, 3, 4)
    mkt = market({d0: (103.0, 104.0)})  # gapped 3% and held
    at = datetime(2020, 3, 3, 20, 0, tzinfo=NY)  # after-hours report
    entries = study.generate([ev("earnings_beat", 0.10, at)], mkt, opens, EVERYONE)
    kinds = {(e.signal, e.entry_day) for e in entries}
    assert ("earnings_beat", d0) in kinds
    assert ("beat_gap_held", date(2020, 3, 5)) in kinds  # needs day-0's close, so the NEXT open


def test_price_only_signals_do_not_look_ahead():
    path = {d: (100.0 + i * 0.1, 100.0 + i * 0.1) for i, d in enumerate(DAYS)}
    full = market(path)
    cut = date(2020, 6, 30)
    trunc_days = [d for d in DAYS if d <= cut]
    trunc = study.Market({s: b[b.index <= pd.Timestamp(cut)] for s, b in full.bars.items()}, trunc_days)
    a = {(e.signal, e.symbol, e.entry_day) for e in study.generate([], full, opens, EVERYONE) if e.entry_day <= cut}
    b = {(e.signal, e.symbol, e.entry_day) for e in study.generate([], trunc, opens, EVERYONE)}
    assert a == b and a  # same decisions with or without the future


def test_costs_and_spy_excess():
    mkt = market({date(2020, 3, 4): (100.0, 100.0), date(2020, 3, 10): (110.0, 110.0)})
    en = study.Entry("earnings_beat", "ABC", date(2020, 3, 4), "x")
    res = {r.hold: r for r in study.evaluate([en], mkt, lambda s, y: 0.001, holds=(5,))}
    r = res[5]
    assert r.exit_day == date(2020, 3, 10) and r.gross == pytest.approx(0.10)
    assert r.cost == pytest.approx(0.001 + 2 * study.SLIPPAGE + study.SELL_FEE)
    assert r.net == pytest.approx(0.10 - r.cost) and r.excess == pytest.approx(r.net - 0.0)


def test_spread_model_uses_real_samples_by_stock_and_year():
    f = study.spread_model({"ABC|2019-03-01 09:45": 0.002, "ABC|2019-05-01 09:45": 0.004,
                            "XYZ|2019-03-01 09:45": 0.010, "ABC|2020-01-02 09:45": None})
    assert f("ABC", 2019) == pytest.approx(0.003)
    assert f("NEW", 2019) == pytest.approx(0.004)  # that year's all-stock median
    assert f("ABC", 2024) == pytest.approx(0.004)  # overall median when the year has no samples


def tr(day, excess, signal="earnings_beat", hold=5, exit_offset=7):
    d = date.fromisoformat(day)
    return study.TradeResult(signal, "ABC", hold, d, d + timedelta(days=exit_offset), excess + 0.01, 0.001,
                             excess + 0.009, 0.009)


def test_selection_uses_only_trades_closed_before_the_year():
    rng = np.random.default_rng(1)
    good = [tr(str(DAYS[i]), 0.02 + rng.normal(0, 0.01)) for i in range(0, 400, 5)]
    assert study.select(good, before=date(2021, 1, 1)) == {"earnings_beat": 5}
    assert study.select(good, before=date(2019, 2, 1)) == {}  # too few closed trades yet
    noise = [tr(str(DAYS[i]), rng.normal(0, 0.03)) for i in range(0, 400, 5)]
    assert study.select(noise, before=date(2021, 1, 1)) == {}
    info = [tr(str(DAYS[i]), 0.05, signal="downgrade") for i in range(0, 400, 5)]
    assert study.select(info, before=date(2021, 1, 1)) == {}  # info-only signals are never traded


def test_stats_hit_rate_is_after_costs_and_t_is_clustered_by_day():
    trades = [tr("2020-03-02", 0.01), tr("2020-03-02", 0.01), tr("2020-03-03", -0.02), tr("2020-03-04", 0.03)]
    s = study.stats(trades, "earnings_beat", 5, "x")
    assert s.trades == 4 and s.hit_rate == pytest.approx(0.75)
    assert s.mean_excess == pytest.approx((0.01 + 0.01 - 0.02 + 0.03) / 4)


def test_portfolio_caps_positions_and_charges_costs():
    mkt = market({date(2020, 3, 4): (100.0, 101.0)})
    trades = [study.TradeResult("earnings_beat", "ABC", 5, date(2020, 3, 4), date(2020, 3, 10), 0.01, 0.002,
                                0.008, 0.0)]
    r, taken = study.portfolio_returns(trades, {"earnings_beat": 5}, mkt, date(2020, 3, 1), date(2020, 3, 31))
    assert len(taken) == 1
    entry_day = r.loc[pd.Timestamp("2020-03-04")]
    assert entry_day == pytest.approx(0.10 * (0.01 - (0.002 - study.SELL_FEE) / 2))
    assert r.loc[pd.Timestamp("2020-03-05")] == pytest.approx(0.10 * (100 / 101 - 1))  # marked to market
    exit_day = r.loc[pd.Timestamp("2020-03-10")]  # flat price that day: only the exit costs
    assert exit_day == pytest.approx(0.10 * (-(0.002 - study.SELL_FEE) / 2 - study.SELL_FEE))
    assert r.loc[pd.Timestamp("2020-03-11")] == 0.0  # position closed


# --- today's shortlist --------------------------------------------------------------------------------------


class FakeLive:
    def __init__(self, news, last_close=100.0, partial_today=None):
        self._news, self.last_close, self.partial = news, last_close, partial_today

    def recent_daily(self, symbols, days=420, adjustment="all"):
        idx = pd.bdate_range(end="2026-11-12", periods=300)
        if self.partial:
            idx = idx.append(pd.DatetimeIndex([pd.Timestamp("2026-11-13")]))
        px = np.full(len(idx), 100.0)
        df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1e6}, index=idx)
        if self.partial:  # today's half-finished bar: down 9% so far
            df.iloc[-1] = [100.0, 100.0, 91.0, 91.0, 2e6]
        return {"ABC": df, "SPY": df.copy()}

    def news(self, symbols, start, end):
        return self._news


def test_today_lists_only_passed_signals_and_an_avoid_list():
    from lumisafe.signals.today import format_today, scan

    news = [item("ABC Q3 EPS $1.30 Beats $1.10 Estimate, Sales $5B Beat $4.8B Estimate", ("ABC",),
                 "2026-11-12T21:05:00+00:00"),
            item("Morgan Stanley Downgrades XYZ to Underweight", ("XYZ",), "2026-11-12T22:00:00+00:00")]
    now = datetime(2026, 11, 12, 18, 0, tzinfo=NY)  # Thursday evening
    ideas, avoid = scan(FakeLive(news), {"earnings_beat_strong": 10}, {"earnings_beat_strong": {
        "trades": 80, "hit_rate": 0.6, "mean_net": 0.012, "mean_excess": 0.006}}, now)
    assert [(i.signal, i.symbol, i.buy_at_open_on) for i in ideas] == [("earnings_beat_strong", "ABC", date(2026, 11, 13))]
    assert ideas[0].sell_at_close_on == date(2026, 11, 27)  # 10 trading days; Thanksgiving (Nov 26) skipped
    text = format_today(ideas, avoid, {"earnings_beat_strong": 10}, 10_000.0)
    assert "BUY ABC at the open on Fri Nov 13" in text and "60% ended positive" in text and "$1,000" in text
    assert "never" not in text  # 2022-2025 informed which signals are listed; never call it untouched
    # signals that didn't pass are never suggested
    ideas, _ = scan(FakeLive(news), {"upgrade_bullish": 5}, {}, now)
    assert ideas == []


def test_today_never_uses_a_half_finished_day():
    from lumisafe.signals.today import scan

    now = datetime(2026, 11, 13, 11, 0, tzinfo=NY)  # market open, ABC down 9% so far today
    ideas, _ = scan(FakeLive([], partial_today=True), {"pullback_uptrend": 5}, {}, now)
    assert ideas == []  # the dip only counts once today's close is final


def test_today_with_no_passed_signals_says_so_plainly():
    from lumisafe.signals.today import format_today

    assert "nothing to suggest" in format_today([], [], {}, None)


def test_bias_check_keeps_only_signals_that_beat_the_same_stocks_in_both_periods():
    rng = np.random.default_rng(3)

    def mk(signal, day, edge_vs_pool):
        d = DAYS[day]
        net = 0.02 + edge_vs_pool + rng.normal(0, 0.01)
        return study.TradeResult(signal, "ABC", 20, d, d + timedelta(days=28), net + 0.001, 0.001, net, 0.0, 0.02)

    real = [mk("news_gap_held", i, 0.01) for i in range(0, 500, 2)]
    list_bias = [mk("pullback_uptrend", i, 0.0) for i in range(0, 500, 2)]  # beats SPY, but only as much as the pool
    faded = [mk("rel_strength", i, 0.01 if DAYS[i] <= date(2019, 12, 31) else -0.005) for i in range(0, 500, 2)]
    trades = real + list_bias + faded
    chosen = {"news_gap_held": 20, "pullback_uptrend": 20, "rel_strength": 20}
    assert study.robust_picks(trades, chosen, date(2019, 12, 31)) == {"news_gap_held": 20}
    s = study.stats(real, "news_gap_held", 20, "x")
    assert s.mean_vs_pool == pytest.approx(np.mean([t.net - 0.02 for t in real])) and s.t_vs_pool > 2


def test_gap_idea_shows_the_real_headline_and_one_line_per_stock():
    from lumisafe.signals.today import format_today, scan

    class Gap(FakeLive):
        def recent_daily(self, symbols, days=420, adjustment="all"):
            out = super().recent_daily(symbols, days, adjustment)
            df = out["ABC"]
            df.iloc[-1] = [104.0, 105.0, 103.5, 104.5, 3e6]  # Nov 12: opened 4% up and held
            return out

    news = [item("ABC Q3 EPS $1.30 Beats $1.10 Estimate, Sales $5B Beat $4.8B Estimate", ("ABC",),
                 "2026-11-12T12:00:00+00:00")]
    now = datetime(2026, 11, 12, 18, 0, tzinfo=NY)
    ideas, _ = scan(Gap(news), {"news_gap_held": 20, "beat_gap_held": 10}, {}, now)
    assert len(ideas) == 1 and ideas[0].symbol == "ABC" and ideas[0].hold == 20
    text = format_today(ideas, [], {"news_gap_held": 20}, None)
    assert "ABC Q3 EPS $1.30 Beats" in text and "News gap up 3%+ held" in text and "Earnings beat, gap up held" in text


def test_gap_reason_prefers_the_good_news_over_a_later_unrelated_headline():
    at = datetime(2020, 3, 4, 7, 0, tzinfo=NY)
    events = [Event("ABC", at, "earnings_beat", 0.1, "ABC Q4 EPS $1.10 Beats $1.00 Estimate"),
              Event("ABC", at + timedelta(minutes=30), "pt_cut", 90.0, "Firm Lowers ABC Price Target to $90")]
    mkt = market({date(2020, 3, 4): (104.0, 105.0)})
    gap = [e for e in study.generate(events, mkt, opens, EVERYONE) if e.signal == "news_gap_held"]
    assert gap and "Beats" in gap[0].why
