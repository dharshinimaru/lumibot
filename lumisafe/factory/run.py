"""Factory schedule (New York time), checked every few minutes:

* 09:15-09:29  news and AI bots read overnight news; picks are frozen for the 09:30 open
* 09:30-16:00  live marks: every book is re-priced from the latest trades (for the dashboard)
* 16:30+       full refresh of daily bars, every bot decides for the next open, books re-graded

Everything goes to ``~/.lumisafe/factory/`` (decisions.jsonl is append-only; state.json feeds the
dashboard and ``lumisafe factory status``).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from lumisafe.daytrade.universe import POOL, sessions
from lumisafe.factory import engine
from lumisafe.factory.bots import Bot, Context, all_bots
from lumisafe.signals import zoo

NY = ZoneInfo("America/New_York")
START = date(2016, 1, 1)


class Factory:
    def __init__(self, md, live, spread_fn: Callable[[str, int], float], *, llm=None,
                 news_signals: tuple[dict, dict] = ({}, {}), bots: list[Bot] | None = None,
                 ledger: engine.Ledger | None = None, zoo_report: dict | None = None,
                 sessions_fn: Callable = sessions, log: Callable[[str], None] = print):
        self.md, self.live, self.spread_fn, self.llm = md, live, spread_fn, llm
        self.news_signals = news_signals
        self.bots = bots or all_bots()
        self.ledger = ledger or engine.Ledger()
        self.zoo_report = zoo_report or {}
        self.sessions_fn = sessions_fn
        self.log = log
        self._bars: dict[str, pd.DataFrame] | None = None
        self._bars_day: date | None = None
        self.last_decide: dict[str, date] = {}

    # -- data ----------------------------------------------------------------------------------------

    def bars(self, through: date, refresh: bool = False) -> dict[str, pd.DataFrame]:
        if self._bars is None or refresh or (self._bars_day is not None and self._bars_day < through):
            syms = sorted(set(POOL) | set(zoo.ETFS))
            b = self.md.daily_bars(syms, START, through, adjustment="all", refresh=refresh)
            self._bars = {k: v for k, v in b.items() if len(v)}
            self._bars_day = through
        return self._bars

    def _sessions(self, now: datetime):
        ss = self.sessions_fn(now.date() - timedelta(days=10), now.date() + timedelta(days=45))
        done = [s for s in ss if s.close <= now]
        nxt = [s for s in ss if s.open > now]
        return done, nxt

    # -- decisions ---------------------------------------------------------------------------------------

    def decide(self, now: datetime, kinds: set[str] | None = None, refresh: bool = False) -> list[str]:
        done, nxt = self._sessions(now)
        if not done or not nxt:
            return []
        day = done[-1].day
        bars = self.bars(day, refresh=refresh)
        if "SPY" not in bars or pd.Timestamp(day) not in bars["SPY"].index:
            return [f"daily bars for {day} not available yet; will retry"]
        data = zoo.build({k: v[v.index <= pd.Timestamp(day)] for k, v in bars.items()})
        ctx = Context(now, day, data, nxt[0].day, [s.day for s in nxt], self.live, self.llm, self.news_signals)
        msgs = []
        for bot in self.bots:
            if kinds is not None and bot.kind not in kinds:
                continue
            if bot.rebalance != "event" and self.ledger.decided(bot.name, day.isoformat()):
                continue
            try:
                d = bot.decide(ctx)
            except Exception as exc:  # one broken bot never stops the others
                self.log(f"{bot.name}: decision failed ({type(exc).__name__}: {exc})")
                continue
            if bot.rebalance == "event":
                have = {(p["symbol"], p["entry_day"]) for x in self.ledger.for_bot(bot.name) for p in x.picks}
                d.picks = [p for p in d.picks if (p["symbol"], p["entry_day"]) not in have]
            if engine.record(self.ledger, bot, d):
                for c in engine.changes(self.ledger, bot, d.day) if bot.rebalance != "event" else \
                        [f"BUY {p['symbol']} at the {p['entry_day']} open, sell after the {p['exit_day']} close"
                         for p in d.picks]:
                    msgs.append(f"{bot.name}: {c}")
        return msgs

    # -- grading -----------------------------------------------------------------------------------------

    def grade(self, now: datetime, live_prices: tuple[dict, dict] | None = None) -> dict:
        done, _ = self._sessions(now)
        day = done[-1].day if done else now.date()
        bars = self.bars(day)
        data = zoo.build(bars)
        spy_w = pd.DataFrame(0.0, index=data.days, columns=data.C.columns)
        spy_w["SPY"] = 1.0
        bench = {"SPY": zoo.simulate(spy_w, data, self.spread_fn),
                 "pool": zoo.simulate(zoo.pool_weights(data), data, self.spread_fn)}
        results, excess, curves, rows = {}, {}, {}, []
        zoo_by_name = {r["name"]: r for r in self.zoo_report.get("results", [])}
        for bot in self.bots:
            r = engine.forward_returns(self.ledger, bot, data, self.spread_fn)
            m = engine.metrics(r, bench[bot.benchmark])
            results[bot.name] = m
            excess[bot.name] = r - bench[bot.benchmark].reindex(r.index).fillna(0.0)
            if len(r):
                curves[bot.name] = [[d.date().isoformat(), round(float(v), 6)] for d, v in (1 + r).cumprod().items()]
        ver = engine.verdicts(results, excess)
        last, prev = live_prices or ({}, {})
        for bot in self.bots:
            hold = engine.holdings_now(self.ledger, bot, now.date())
            zr = zoo_by_name.get(bot.name, {})
            last_day = self.ledger.for_bot(bot.name)[-1].day if self.ledger.for_bot(bot.name) else None
            rows.append({
                "name": bot.name, "kind": bot.kind, "horizon": bot.horizon, "benchmark": bot.benchmark,
                "description": bot.description, "started": self.ledger.started(bot.name),
                "metrics": results[bot.name], "verdict": ver[bot.name],
                "live_today": engine.live_today(hold, last, prev) if last else None,
                "holdings": dict(sorted(hold.items(), key=lambda kv: -kv[1])[:20]),
                "latest_changes": engine.changes(self.ledger, bot, last_day) if last_day else [],
                "latest_decision_day": last_day,
                "backtest": {p: (zr.get("stats") or {}).get(p) for p in ("discovery", "validation", "fresh")}
                if zr else None,
                "backtest_passed": zr.get("passed") if zr else None,
            })
        state = {"updated_at": now.isoformat(timespec="seconds"), "as_of_close": day.isoformat(),
                 "bots": rows, "curves": curves,
                 "bench_curves": {k: [[d.date().isoformat(), round(float(v), 6)]
                                      for d, v in (1 + v[v.index > pd.Timestamp(min(
                                          (r["started"] for r in rows if r["started"]), default=day.isoformat()))]
                                                   ).cumprod().items()] for k, v in bench.items()}}
        engine.write_state(state)
        return state

    def live_prices(self) -> tuple[dict, dict]:
        held = set()
        for bot in self.bots:
            held |= set(engine.holdings_now(self.ledger, bot, datetime.now(NY).date()))
        if not held:
            return {}, {}
        b = self.live.recent_daily(sorted(held), days=7, adjustment="all")
        last, prev = {}, {}
        for s, df in b.items():
            if len(df) >= 2:
                last[s], prev[s] = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        return last, prev

    # -- schedule ----------------------------------------------------------------------------------------

    def step(self, now: datetime) -> list[str]:
        """One scheduler check. Safe to call as often as you like."""
        msgs: list[str] = []
        today = self.sessions_fn(now.date(), now.date())
        s = today[0] if today else None
        if s is not None and s.open - timedelta(minutes=15) <= now < s.open \
                and self.last_decide.get("pre") != now.date():
            msgs += self.decide(now, kinds={"qual", "ai"})
            self.last_decide["pre"] = now.date()
            self.grade(now)
        elif s is not None and s.open <= now < s.close + timedelta(minutes=30):
            self.grade(now, self.live_prices())
        elif (s is None or now >= s.close + timedelta(minutes=30)) and self.last_decide.get("post") != now.date():
            out = self.decide(now, refresh=True)
            if out and out[0].startswith("daily bars for"):
                return out  # Alpaca hasn't published today's bar yet: retry next check
            msgs += out
            self.last_decide["post"] = now.date()
            self.grade(now)
        return msgs

    def loop(self, every_seconds: int = 300) -> None:  # pragma: no cover - wall-clock loop
        while True:
            try:
                for m in self.step(datetime.now(NY)):
                    self.log(m)
            except Exception as exc:
                self.log(f"factory step failed: {type(exc).__name__}: {exc}")
            time.sleep(every_seconds)


def make_llm():
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from lumisafe.advisor.advisor import AnthropicLLMClient

    return AnthropicLLMClient()


def default_factory(log: Callable[[str], None] = print) -> Factory:
    import json

    from lumisafe import paths
    from lumisafe.adapters.alpaca_data import AlpacaMarketData, LiveMarketData
    from lumisafe.signals import study

    md = AlpacaMarketData(cache=paths.cache_dir() / "zoo")
    live = LiveMarketData(AlpacaMarketData())
    f = paths.cache_dir() / "spreads.json"
    spread_fn = study.spread_model(json.loads(f.read_text()) if f.is_file() else {})
    sig = sorted(paths.reports_dir().glob("signals_*.json"))
    d = json.loads(sig[-1].read_text()) if sig else {}
    zr = sorted(paths.reports_dir().glob("zoo_*.json"))
    return Factory(md, live, spread_fn, llm=make_llm(), news_signals=(d.get("current_picks", {}),
                                                                      d.get("current_stats", {})),
                   zoo_report=json.loads(zr[-1].read_text()) if zr else None, log=log)
