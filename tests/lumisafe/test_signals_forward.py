"""Signal autopilot: decisions are written down before the open, traded on paper, graded on real prices."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from click.testing import CliRunner

from lumisafe import paths
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.daytrade.universe import Session
from lumisafe.journal import Journal
from lumisafe.reporting import cli
from lumisafe.runner import SIGNALS_STRATEGY, SignalRunner, StateStore, signals_allowed
from lumisafe.signals.forward import Ledger, format_scorecard, grade, summary, verdict
from lumisafe.signals.today import Idea
from lumisafe.types import Source

from .fakes import FakeExecutor
from .test_daytrade_sim import NY

D1, D2 = date(2024, 3, 5), date(2024, 3, 19)
S1 = Session(D1, datetime(2024, 3, 5, 9, 30, tzinfo=NY), datetime(2024, 3, 5, 16, 0, tzinfo=NY))
S2 = Session(D2, datetime(2024, 3, 19, 9, 30, tzinfo=NY), datetime(2024, 3, 19, 16, 0, tzinfo=NY))
EVENING = datetime(2024, 3, 4, 17, 0, tzinfo=NY)


def idea(sym="ABC", entry=D1, exit_=D2, hold=10):
    return Idea("beat_gap_held", sym, entry, exit_, f"{sym} Q4 EPS $1.10 Beats $1.00 Estimate", hold, {})


class Live:
    def spread(self, symbol, at):
        return 0.002


def config():
    return LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 1.0}, "monthly_contribution_usd": 0}})


def runner(ideas, notes=None):
    scan = lambda live, picks, stats, now: (ideas, [])  # noqa: E731
    sess = lambda d, previous=False: {D1: S1, D2: S2}.get(d)  # noqa: E731
    return SignalRunner(config(), Journal(), StateStore(paths.state_path()), Live(), {"beat_gap_held": 10}, {},
                        notify=(notes.append if notes is not None else None), session_fn=sess, scan_fn=scan)


def broker(now, price=100.0, **kw):
    return FakeExecutor(now, cash=100_000.0, prices={"ABC": price, "VTI": 200.0}, is_backtesting=False,
                        auto_fill=False, **kw)


def test_decision_is_written_down_before_the_open_and_notified():
    notes = []
    msgs = runner([idea()], notes).scan(EVENING)
    assert msgs[0].startswith("DECISION: BUY ABC at the open on 2024-03-05, SELL at the close on 2024-03-19")
    assert notes == msgs
    d = Ledger.load().decisions[0]
    assert d.decided_at.startswith("2024-03-04T17:00") and d.spread == 0.002 and d.status == "planned"
    assert "BUY ABC" in (paths.home() / "decisions.log").read_text()
    # scanning again (e.g. at 09:20) never duplicates or re-dates a decision
    runner([idea()]).scan(EVENING + timedelta(hours=16))
    assert len(Ledger.load().decisions) == 1 and Ledger.load().decisions[0].decided_at.startswith("2024-03-04")


def test_buys_at_the_open_through_the_pipeline_and_sells_on_the_exit_day():
    r = runner([idea()])
    r.scan(EVENING)
    b = broker(S1.open + timedelta(minutes=1))
    r.tick(b)
    o = b.submitted[0]
    assert o.side == "buy" and float(o.quantity).is_integer() and o.quantity > 0
    e = Journal().query(symbol="ABC")[0]
    assert e.intent.source is Source.SATELLITE and e.intent.strategy_name == SIGNALS_STRATEGY
    assert [rv.reviewer for rv in e.reviews] == ["tax", "risk"]  # no AI veto: grade exactly what was studied
    b.fill(o, 100.0)
    r.on_fill(o.identifier, "buy", 100.0, o.quantity, b.now)
    assert Ledger.load().decisions[0].status == "holding"
    # a week later nothing happens; on the exit day, 20 minutes before the close, it sells
    b.now = S2.close - timedelta(minutes=19)
    b.prices["ABC"] = 104.0
    r.tick(b)
    sell = b.submitted[-1]
    assert sell.side == "sell" and sell.quantity == o.quantity
    b.fill(sell, 104.0)
    r.on_fill(sell.identifier, "sell", 104.0, sell.quantity, b.now)
    d = Ledger.load().decisions[0]
    assert d.status == "closed" and d.sell_fill == 104.0
    assert StateStore(paths.state_path()).get("signals_book")["realized"] == pytest.approx(4.0 * o.quantity)


def test_never_buys_late_and_a_missed_fill_is_recovered_from_the_broker():
    r = runner([idea()])
    r.scan(EVENING)
    b = broker(S1.open + timedelta(minutes=45))
    r.tick(b)
    assert b.submitted == [] and Ledger.load().decisions[0].status == "missed"

    r2 = runner([idea("XYZ")])
    r2.scan(EVENING)
    b2 = FakeExecutor(S1.open + timedelta(minutes=1), cash=100_000.0, prices={"XYZ": 50.0}, is_backtesting=False,
                      auto_fill=False)
    r2.tick(b2)
    b2.fill(b2.submitted[0], 50.0)  # the fill callback never arrives (process restarted)
    b2.now += timedelta(minutes=5)
    r2.tick(b2)
    d = Ledger.load().get("XYZ|2024-03-05")
    assert d.status == "holding" and d.buy_fill == 50.0


def test_weekly_trade_cap_blocks_the_order_but_the_decision_is_still_graded():
    j = Journal()
    r = runner([idea()])
    r.scan(EVENING)
    b = broker(S1.open + timedelta(minutes=1))
    from lumisafe import runner as rmod

    orig = rmod.non_core_trades_since
    rmod.non_core_trades_since = lambda journal, since: 5
    try:
        r.tick(b)
    finally:
        rmod.non_core_trades_since = orig
    d = Ledger.load().decisions[0]
    assert b.submitted == [] and d.status == "blocked" and "limit of 5 trades" in d.note
    assert j.query(symbol="ABC")[0].final.value == "block"
    bars = {"ABC": pd.DataFrame({"open": [100.0, 101.0], "close": [100.5, 106.0]},
                                index=pd.DatetimeIndex([pd.Timestamp(D1), pd.Timestamp(D2)])),
            "SPY": pd.DataFrame({"open": [400.0, 404.0], "close": [401.0, 404.0]},
                                index=pd.DatetimeIndex([pd.Timestamp(D1), pd.Timestamp(D2)]))}
    g = grade(Ledger.load(), bars, D2)[0]
    assert g.finished and g.market == pytest.approx(106 / 100 - 1 - 0.002 - 0.001) and g.spy == pytest.approx(0.01)
    assert g.paper is None


def test_grading_never_invents_prices_and_marks_open_trades():
    led = Ledger()
    led.add([idea()], EVENING)
    bars = {"ABC": pd.DataFrame({"open": [100.0], "close": [103.0]}, index=pd.DatetimeIndex([pd.Timestamp(D1)])),
            "SPY": pd.DataFrame({"open": [400.0], "close": [400.0]}, index=pd.DatetimeIndex([pd.Timestamp(D1)]))}
    g = grade(led, bars, D1)[0]
    assert not g.finished and g.market == pytest.approx(0.03 - 0.001 - 0.001)
    assert grade(led, {}, D1)[0].market is None  # no bars → not graded, never a made-up price
    assert grade(led, bars, D1 - timedelta(days=1)) == []  # not entered yet → not graded


def test_verdict_refuses_to_bless_a_short_run():
    s = {"finished": 30, "avg_vs_spy": 0.02}
    assert verdict(s, {"mean_excess": 0.01}, 10).startswith("Verdict: TOO EARLY")
    assert verdict({"finished": 5, "avg_vs_spy": 0.05}, {"mean_excess": 0.01}, 80).startswith("Verdict: TOO EARLY")
    assert verdict(s, {"mean_excess": 0.01}, 61).startswith("Verdict: HOLDING UP")
    assert verdict({"finished": 30, "avg_vs_spy": 0.001}, {"mean_excess": 0.01}, 61).startswith(
        "Verdict: NOT HOLDING UP")
    assert "No decisions graded yet" in format_scorecard([], None, 0)
    assert summary([])["finished"] == 0


def test_autopilot_needs_a_plan_and_a_passed_study(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "signals_runner", lambda *a: called.append(a))
    r = CliRunner()
    assert "Run `lumisafe init` first" in r.invoke(cli.main, ["signals", "autopilot"]).output
    save_config(config(), paths.config_path())
    res = r.invoke(cli.main, ["signals", "autopilot"])
    assert res.exit_code == 1 and "not tested yet" in res.output and not called
    assert "REJECTED" in signals_allowed("REJECTED", {"x": 1})
    assert "nothing to trade" in signals_allowed("PAPER", {})
    assert signals_allowed("PAPER", {"x": 1}) is None
    assert "No decisions recorded yet" in r.invoke(cli.main, ["signals", "scorecard"]).output
