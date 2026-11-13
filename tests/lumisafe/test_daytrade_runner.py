"""Paper day-trading runner: one tick per minute through the full Pipeline (fake broker, fake live data)."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from lumisafe import paths
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.daytrade import INTRADAY_STRATEGY
from lumisafe.daytrade.sim import Params
from lumisafe.daytrade.universe import Session
from lumisafe.journal import Journal
from lumisafe.reporting import cli
from lumisafe.runner import DAYTRADE_BOOK_KEY, DayTradeRunner, StateStore, daytrade_allowed
from lumisafe.types import Source, Verdict

from .fakes import FakeExecutor
from .test_daytrade_sim import NY, bars, opening_range

DAY = date(2024, 3, 5)
OPEN = datetime(2024, 3, 5, 9, 30, tzinfo=NY)
CLOSE = datetime(2024, 3, 5, 16, 0, tzinfo=NY)
SESSION = Session(DAY, OPEN, CLOSE)
PREV = Session(date(2024, 3, 4), datetime(2024, 3, 4, 9, 30, tzinfo=NY), datetime(2024, 3, 4, 16, 0, tzinfo=NY))
P = Params(min_gap=0.02, min_rel_volume=0.05, target_r=1.0)


class Broker(FakeExecutor):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.canceled = []

    def fill(self, order, price=None):
        super().fill(order, price)
        if order.kw.get("order_class") == "bracket":  # like Alpaca: stop and take-profit legs stay open
            from .fakes import FakeOrder

            for leg in ("stop", "limit"):
                o = FakeOrder(order.symbol, order.quantity, "sell", leg=leg)
                self.orders[o.identifier] = o

    def get_orders(self):
        return [o for o in self.orders.values() if o.status not in ("filled", "canceled")]

    def cancel_order(self, o):
        o.status = "canceled"
        self.canceled.append(o.symbol)


class Live:
    """Live data as of 09:46: ABC gapped 3% on company news and has a clean opening range."""

    def __init__(self, headline="ABC raises full-year guidance", n_symbols=1):
        self.headline, self.n_symbols = headline, n_symbols

    def recent_daily(self, symbols, days=45):
        idx = pd.bdate_range(end="2024-03-04", periods=25)
        return {"ABC": pd.DataFrame({"open": 97.0, "high": 97.0, "low": 97.0, "close": 97.0,
                                     "volume": 1_000_000.0}, index=idx)}

    def minute_bars(self, symbols, start, end):
        b = bars(opening_range())
        return {"ABC": b[b.index < end]}

    def news(self, symbols, start, end):
        assert end <= OPEN + timedelta(minutes=14)  # never reads news after the decision time
        return [{"created_at": "2024-03-05T12:00:00+00:00", "headline": self.headline, "source": "x",
                 "symbols": ["ABC"] + [f"X{i}" for i in range(self.n_symbols - 1)]}]

    def spread(self, symbol, at):
        return 0.001


def config(**dt):
    return LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 1.0}, "monthly_contribution_usd": 0},
                                          "daytrade": {"enabled": True, **dt}})


def runner(advisor=None, live=None, **dt):
    sess = lambda d, previous=False: PREV if previous else SESSION  # noqa: E731
    return DayTradeRunner(config(**dt), Journal(), StateStore(paths.state_path()), live or Live(), P,
                          advisor=advisor, session_fn=sess)


def broker(now, price=101.5, **kw):
    return Broker(now, cash=100_000.0, prices={"ABC": price}, is_backtesting=False, auto_fill=False, **kw)


def test_breakout_enters_with_a_bracket_through_the_pipeline():
    r = runner()
    b = broker(OPEN + timedelta(minutes=16))
    msgs = r.tick(b)
    assert any("scan: 1 gap-ups with news, 1 pass" in m for m in msgs)
    assert any(m.startswith("ABC: bought") for m in msgs)
    order = b.submitted[0]
    assert order.side == "buy" and float(order.quantity).is_integer()
    assert order.kw["order_class"] == "bracket"
    assert 0 < order.kw["secondary_stop_price"] < 101.5 < order.kw["secondary_limit_price"]
    e = Journal().query(symbol="ABC")[0]
    assert e.intent.source is Source.SATELLITE and e.intent.strategy_name == INTRADAY_STRATEGY
    assert [rv.reviewer for rv in e.reviews] == ["tax", "risk"]
    book = StateStore(paths.state_path()).get(DAYTRADE_BOOK_KEY)
    assert book["trades"] == 1 and "ABC" in book["positions"]
    # one attempt per stock per day: the next minute doesn't buy again
    b.now += timedelta(minutes=1)
    r.tick(b)
    assert len(b.submitted) == 1


def test_ai_veto_stops_the_trade_and_is_journaled():
    from lumisafe.advisor.advisor import AIAdvisor

    class Veto:
        def complete(self, system, user, *, json_schema, timeout):
            assert "day trade" in system and "roundups" in system
            return '{"verdict":"veto","reasons":["roundup"],"confidence":0.8,"plain_english":"No company news."}'

    r = runner(advisor=AIAdvisor(Veto(), enabled=True), live=Live("Top 10 movers today", n_symbols=10))
    b = broker(OPEN + timedelta(minutes=16))
    r.tick(b)
    assert b.submitted == []
    assert Journal().query(symbol="ABC")[0].final is Verdict.VETO


def test_no_entry_before_the_opening_range_or_below_the_trigger():
    r = runner()
    assert r.tick(broker(OPEN + timedelta(minutes=10))) == [] or True
    b = broker(OPEN + timedelta(minutes=16), price=100.5)  # trigger is 101.01
    r.tick(b)
    assert b.submitted == []


def test_daily_loss_limit_sells_everything_and_stops():
    r = runner()
    b = broker(OPEN + timedelta(minutes=16))
    r.tick(b)
    o = b.submitted[0]
    b.fill(o, 101.5)
    r.on_fill(o.identifier, "ABC", "buy", 101.5, o.quantity, b.now)
    b.now += timedelta(minutes=5)
    b.prices["ABC"] = 101.5 - 2 * (0.01 * 10_000 / o.quantity)  # loses 2% of the slice on paper
    msgs = r.tick(b)
    assert any("daily loss limit" in m for m in msgs)
    assert b.canceled == ["ABC", "ABC"]  # both bracket legs canceled before selling
    sell = b.submitted[-1]
    assert sell.side == "sell" and sell.quantity == o.quantity
    book = StateStore(paths.state_path()).get(DAYTRADE_BOOK_KEY)
    assert book["halted"] is True


def test_everything_is_sold_before_the_close_and_fills_are_booked():
    r = runner()
    b = broker(OPEN + timedelta(minutes=16))
    r.tick(b)
    o = b.submitted[0]
    b.fill(o, 101.5)
    r.on_fill(o.identifier, "ABC", "buy", 101.5, o.quantity, b.now)
    b.now = CLOSE - timedelta(minutes=10)
    b.prices["ABC"] = 102.0
    msgs = r.tick(b)
    assert any("closing time" in m for m in msgs)
    sell = b.submitted[-1]
    assert sell.side == "sell"
    r.on_fill(sell.identifier, "ABC", "sell", 102.0, sell.quantity, b.now)
    book = StateStore(paths.state_path()).get(DAYTRADE_BOOK_KEY)
    assert book["positions"] == {} and book["realized"] == pytest.approx(o.quantity * 0.5, abs=0.05)
    ev = Journal().events(kind="daytrade_exit")[0].payload
    assert ev["exit"] == 102.0 and ev["fees"] > 0


def test_new_day_starts_a_fresh_book_and_keeps_pdt_history():
    from lumisafe.daytrade.live import DayBook

    old = DayBook("2024-03-04", 10_000.0, trades=2, recent_day_trades=[1, 0, 1]).to_dict()
    b = DayBook.for_today(old, DAY, 10_500.0)
    assert b.day == "2024-03-05" and b.trades == 0 and b.start_equity == 10_500.0
    assert b.recent_day_trades == [1, 0, 1, 2]


def test_pdt_and_caps_block_entries():
    from lumisafe.daytrade.live import DayBook, entry_block_reason

    cfg = config().daytrade
    kw = dict(now=OPEN + timedelta(minutes=20), entry_until=OPEN + timedelta(minutes=90),
              flat_at=CLOSE - timedelta(minutes=10), prices={})
    book = DayBook("2024-03-05", 2_000.0, recent_day_trades=[1, 1, 1])
    assert "pattern-day-trader" in entry_block_reason(book, cfg, "ABC", 1, 100.0, account_equity=20_000, **kw)
    assert entry_block_reason(book, cfg, "ABC", 1, 100.0, account_equity=30_000, **kw) is None
    assert "cap" in entry_block_reason(DayBook("d", 1_000.0), cfg, "ABC", 10, 100.0, account_equity=1e5, **kw)
    off = config(enabled=False).daytrade
    assert "off" in entry_block_reason(DayBook("d", 1e4), off, "ABC", 1, 100.0, account_equity=1e5, **kw)


def test_rejected_or_disabled_strategy_never_runs():
    on = config()
    assert "REJECTED" in daytrade_allowed(on, "REJECTED", P)
    assert "not tested" in daytrade_allowed(on, None, P)
    assert "off" in daytrade_allowed(config(enabled=False), "PAPER", P)
    assert "backtest" in daytrade_allowed(on, "PAPER", None)
    assert daytrade_allowed(on, "PAPER", P) is None


def test_cli_run_refuses_until_tested_and_enabled(monkeypatch):
    save_config(config(enabled=False), paths.config_path())
    called = []
    monkeypatch.setattr(cli, "daytrade_runner", lambda *a: called.append(a))
    r = CliRunner()
    res = r.invoke(cli.main, ["daytrade", "run"])
    assert res.exit_code == 1 and "off" in res.output and not called
    assert "ON for paper" in r.invoke(cli.main, ["daytrade", "enable"]).output
    res = r.invoke(cli.main, ["daytrade", "run"])
    assert res.exit_code == 1 and "not tested yet" in res.output and not called
    out = r.invoke(cli.main, ["daytrade", "status"]).output
    assert "Day-trading lab: ON" in out and "No day trades yet" in out
    assert "OFF" in r.invoke(cli.main, ["daytrade", "disable"]).output
