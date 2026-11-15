"""Bot factory: frozen decisions, forward-only books, AI guardrails, schedule (synthetic data, no network)."""

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from lumisafe.daytrade.universe import Session
from lumisafe.factory import engine
from lumisafe.factory.bots import Bot, Context, Decision, _ai_news, all_bots
from lumisafe.factory.run import NY, Factory
from lumisafe.reporting.factory_dashboard import table
from lumisafe.signals import zoo

N = 2700
IDX = pd.bdate_range("2016-01-04", periods=N)
SYMS = [f"S{i}" for i in range(25)] + zoo.ETFS


def synthetic():
    rng = np.random.default_rng(3)
    out = {}
    for s in SYMS:
        c = 50 * np.cumprod(1 + rng.normal(0.0003, 0.014, N))
        out[s] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6}, index=IDX)
    return out


BARS = synthetic()


class MD:
    def __init__(self):
        self.calls = 0

    def daily_bars(self, symbols, start, end, adjustment="all", refresh=False):
        self.calls += 1
        return {k: v[v.index <= pd.Timestamp(end)] for k, v in BARS.items()}


class Live:
    def __init__(self, items=None):
        self.items = items or []

    def news(self, symbols, start, end):
        return self.items

    def recent_daily(self, symbols, days=7, adjustment="all"):
        return {s: BARS[s].iloc[-3:] for s in symbols if s in BARS}


def sessions_fn(a, b):
    out = []
    for d in pd.bdate_range(a, b):
        d = d.date()
        out.append(Session(d, datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY),
                           datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY)))
    return out


def factory(bots=None, llm=None, live=None):
    return Factory(MD(), live or Live(), lambda s, y: 0.001, llm=llm, bots=bots, sessions_fn=sessions_fn,
                   log=lambda m: None)


DAY1 = IDX[-40].date()


def evening(d):
    return datetime(d.year, d.month, d.day, 17, 0, tzinfo=NY)


def test_quant_decisions_are_frozen_and_stored_only_when_they_change():
    bots = [b for b in all_bots() if b.name in ("benchmark_spy", "momentum_12m_top10", "spy_above_200d")]
    f = factory(bots)
    f.decide(evening(DAY1))
    n1 = len(f.ledger.decisions)
    assert {d.bot for d in f.ledger.decisions} == {b.name for b in bots}  # every bot anchored on day 1
    f.decide(evening(DAY1))  # same day again: nothing new
    assert len(f.ledger.decisions) == n1
    nxt = (pd.Timestamp(DAY1) + pd.offsets.BDay(1)).date()
    f.decide(evening(nxt))
    spy = f.ledger.for_bot("benchmark_spy")
    assert len(spy) == 1  # SPY weights never change → stored once
    lines = (engine.folder() / "decisions.jsonl").read_text().splitlines()
    assert len(lines) == len(f.ledger.decisions)  # append-only file matches


def test_books_only_count_days_after_the_first_decision():
    bots = [b for b in all_bots() if b.name == "benchmark_spy"]
    f = factory(bots)
    f.decide(evening(DAY1))
    st = f.grade(evening(IDX[-1].date()))
    m = st["bots"][0]["metrics"]
    expected_days = len(IDX[IDX > pd.Timestamp(DAY1)])
    assert m["days"] == expected_days  # no backfilled history before the start
    assert m["excess"] == pytest.approx(0.0, abs=0.01)  # ≈ 0: entry cost + the first overnight gap (buys at the next open)
    assert st["bots"][0]["verdict"].startswith("TOO EARLY")


def test_event_picks_hold_a_slot_until_their_exit_day():
    d = Decision("x", "t", DAY1.isoformat(), picks=[{"symbol": "S1", "entry_day": IDX[-39].date().isoformat(),
                                                     "exit_day": IDX[-30].date().isoformat(), "why": "w"}])
    bot = Bot("x", "qual", "short", "pool", "", lambda c: d, "event")
    led = engine.Ledger(engine.folder() / "t.jsonl")
    assert engine.record(led, bot, d)
    data = zoo.build(BARS)
    W = engine.weights_frame(led, bot, data)
    held = W["S1"][W["S1"] > 0]
    assert held.index[0] == IDX[-40] and held.index[-1] == IDX[-31] and (held == 0.10).all()
    assert engine.holdings_now(led, bot, IDX[-35].date()) == {"S1": 0.10}
    assert engine.holdings_now(led, bot, IDX[-20].date()) == {}


class LLM:
    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.calls = reply, fail, []

    def complete(self, system, user, *, json_schema, timeout):
        self.calls.append(user)
        if self.fail:
            raise TimeoutError("down")
        return json.dumps(self.reply)


def _ctx(llm, items):
    data = zoo.build({k: v[v.index <= pd.Timestamp(DAY1)] for k, v in BARS.items()})
    nxt = [d.date() for d in IDX[IDX > pd.Timestamp(DAY1)]]
    return Context(evening(DAY1), DAY1, data, nxt[0], nxt, Live(items), llm)


def test_ai_bot_only_trades_symbols_it_was_shown_and_never_guesses_when_down(monkeypatch):
    import lumisafe.daytrade.universe as uni

    monkeypatch.setattr(uni, "POOL", ("S1", "S2"))
    items = [{"symbols": ["S1"], "headline": "S1 Q3 EPS beats, raises guidance"},
             {"symbols": ["S1", "S2"], "headline": "Sector roundup"}]
    llm = LLM({"picks": [{"symbol": "S1", "hold_days": 50, "reason": "beat+raise", "confidence": 0.7},
                         {"symbol": "NVDA", "hold_days": 10, "reason": "not shown", "confidence": 0.9}]})
    d = _ai_news(_ctx(llm, items))
    assert [p["symbol"] for p in d.picks] == ["S1"]  # NVDA was never in the headlines it saw
    assert "S2" not in llm.calls[0]  # multi-ticker roundups are not shown
    assert d.picks[0]["exit_day"] == IDX[IDX > pd.Timestamp(DAY1)][19].date().isoformat()  # hold capped at 20
    down = _ai_news(_ctx(LLM(fail=True), items))
    assert down.picks == [] and "failed" in down.note
    assert "unavailable" in _ai_news(_ctx(None, items)).note


def test_schedule_preopen_then_live_marks_then_evening(monkeypatch):
    bots = [b for b in all_bots() if b.name in ("benchmark_spy", "news_rules")]
    f = factory(bots)
    d = IDX[-10].date()
    f.step(datetime(d.year, d.month, d.day, 9, 20, tzinfo=NY))  # pre-open: only news/AI bots
    assert {x.bot for x in f.ledger.decisions} == {"news_rules"}
    st = f.step(datetime(d.year, d.month, d.day, 11, 0, tzinfo=NY))  # market hours: marks only
    assert st == [] and engine.read_state() is not None
    f.step(datetime(d.year, d.month, d.day, 16, 45, tzinfo=NY))  # evening: everyone decides
    assert "benchmark_spy" in {x.bot for x in f.ledger.decisions}
    rows = table(engine.read_state())
    assert set(rows["bot"]) == {"benchmark_spy", "news_rules"}


def test_factory_has_quant_qual_and_ai_bots():
    bots = all_bots()
    kinds = {b.kind for b in bots}
    assert kinds == {"quant", "qual", "ai"} and len(bots) == 34 + 2 + 3
    assert {b.horizon for b in bots} == {"short", "long"}


def test_autostart_plist_runs_the_factory_from_the_repo_and_restarts():
    from lumisafe.factory import autostart

    p = autostart.build_plist("/py", "/repo", "/log", None)
    assert p["ProgramArguments"][-5:] == ["/py", "-m", "lumisafe", "factory", "run"]
    assert p["ProgramArguments"][0] == "/usr/bin/caffeinate"
    assert p["WorkingDirectory"] == "/repo"  # .env is found relative to the repo, as by hand
    assert p["KeepAlive"] and p["RunAtLoad"] and p["ThrottleInterval"] >= 60
    assert "LUMISAFE_HOME" not in p["EnvironmentVariables"]
    assert not any("live" in a for a in p["ProgramArguments"])  # paper factory only
