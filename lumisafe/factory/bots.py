"""The bots. Each one answers: given everything known at this close, what should my book hold from the next open?

Kinds:
  * quant: price/volume rules (every strategy in the zoo, plus the two benchmarks)
  * qual:  headline rules (the news signals that passed the study)
  * ai:    Claude reads the news (and, for the hybrid, momentum candidates). These can only be
           tested going forward: the model already knows what happened in past years, so a
           backtest of it would be look-ahead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import pandas as pd

from lumisafe.advisor import prompts
from lumisafe.signals import zoo

MAX_AI_PICKS = 5
EVENT_SLOT = 0.10  # each event pick is 10% of the bot's book (max 10 open)


@dataclass
class Context:
    now: datetime
    day: date  # last completed session: decisions use data through its close
    data: zoo.Data  # adjusted daily bars through ``day``
    next_open: date
    sessions_after: list[date]  # sessions from next_open on (for exit dates)
    live: Any = None  # LiveMarketData for news
    llm: Any = None  # LLMClient for AI bots
    news_signals: tuple[dict, dict] = ({}, {})  # (picks, stats) from the signal study


@dataclass
class Decision:
    """A frozen decision. ``weights`` = whole-book targets (rebalance bots) or ``picks`` (event bots)."""

    bot: str
    decided_at: str
    day: str  # data through this close; trades at the next open
    weights: dict[str, float] | None = None
    picks: list[dict] = field(default_factory=list)  # {symbol, entry_day, exit_day, why}
    note: str = ""


@dataclass
class Bot:
    name: str
    kind: str  # quant / qual / ai
    horizon: str  # short (days-weeks) / long (months)
    benchmark: str  # SPY / pool
    description: str
    decide: Callable[[Context], Decision]
    rebalance: str = "daily"  # how often decisions change: daily / weekly / monthly / event


def _quant(name: str, fn, benchmark: str, desc: str) -> Bot:
    horizon = "short" if any(k in name for k in ("weekly", "rsi2", "turn_of_month")) else "long"

    def decide(ctx: Context) -> Decision:
        W = fn(ctx.data)
        row = W.loc[pd.Timestamp(ctx.day)] if pd.Timestamp(ctx.day) in W.index else W.iloc[-1]
        w = {s: round(float(x), 6) for s, x in row.items() if x > 1e-9}
        return Decision(name, ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat(), weights=w)

    return Bot(name, "quant", horizon, benchmark, desc, decide)


def _benchmarks() -> list[Bot]:
    def spy(ctx):
        return Decision("benchmark_spy", ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat(),
                        weights={"SPY": 1.0})

    def pool(ctx):
        W = zoo.pool_weights(ctx.data)
        row = W.loc[pd.Timestamp(ctx.day)]
        return Decision("benchmark_same_stocks", ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat(),
                        weights={s: float(x) for s, x in row.items() if x > 0})

    return [Bot("benchmark_spy", "quant", "long", "SPY", "Just hold SPY (the bar every bot must beat)", spy),
            Bot("benchmark_same_stocks", "quant", "long", "pool",
                "Hold the same ~100 big stocks equally (what stock pickers must beat)", pool)]


def _exit_day(ctx: Context, hold: int) -> date:
    s = ctx.sessions_after
    return s[hold - 1] if len(s) >= hold else ctx.next_open + timedelta(days=int(hold * 1.45))


def _news_rules(ctx: Context) -> Decision:
    from lumisafe.signals.today import scan

    picks, stats = ctx.news_signals
    d = Decision("news_rules", ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat())
    if not picks or ctx.live is None:
        d.note = "no study picks or no live data"
        return d
    ideas, _avoid = scan(ctx.live, picks, stats, ctx.now)
    d.picks = [{"symbol": i.symbol, "entry_day": i.buy_at_open_on.isoformat(),
                "exit_day": i.sell_at_close_on.isoformat(), "why": i.why[:200]} for i in ideas]
    return d



AI_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["symbol", "hold_days", "reason", "confidence"],
        "properties": {"symbol": {"type": "string"}, "hold_days": {"type": "integer"},
                       "reason": {"type": "string"}, "confidence": {"type": "number"}}}}},
}


def _headlines(ctx: Context, hours: int = 30) -> dict[str, list[str]]:
    from lumisafe.daytrade.universe import POOL

    items = ctx.live.news(list(POOL), ctx.now - timedelta(hours=hours), ctx.now)
    out: dict[str, list[str]] = {}
    for it in items:
        syms = it.get("symbols") or []
        if len(syms) == 1 and syms[0] in POOL:
            out.setdefault(syms[0], []).append(it.get("headline", "")[:200])
    return out


def _moves(ctx: Context, symbols) -> dict[str, str]:
    C = ctx.data.C
    t = pd.Timestamp(ctx.day)
    out = {}
    for s in symbols:
        if s not in C or t not in C.index:
            continue
        c = C[s].loc[:t].dropna()
        if len(c) < 22:
            continue
        out[s] = f"1d {c.iloc[-1] / c.iloc[-2] - 1:+.1%}, 5d {c.iloc[-1] / c.iloc[-6] - 1:+.1%}, " \
                 f"1m {c.iloc[-1] / c.iloc[-22] - 1:+.1%}"
    return out


def _ask(ctx: Context, user: str, allowed: set[str]) -> list[dict]:
    raw = ctx.llm.complete(prompts.FACTORY_NEWS_SYSTEM.format(n=MAX_AI_PICKS), user, json_schema=AI_SCHEMA, timeout=180)
    picks = json.loads(raw).get("picks", [])
    out, seen = [], set()
    for p in picks:
        s = str(p.get("symbol", "")).upper().strip()
        if s not in allowed or s in seen:
            continue  # never trade a symbol the model wasn't shown
        seen.add(s)
        hold = min(20, max(5, int(p.get("hold_days", 10))))
        out.append({"symbol": s, "entry_day": ctx.next_open.isoformat(),
                    "exit_day": _exit_day(ctx, hold).isoformat(), "why": str(p.get("reason", ""))[:200]})
    return out[:MAX_AI_PICKS]


def _ai_news(ctx: Context) -> Decision:
    d = Decision("ai_news_analyst", ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat())
    if ctx.llm is None or ctx.live is None:
        d.note = "AI unavailable (no ANTHROPIC_API_KEY or AI turned off): no decision"
        return d
    heads = _headlines(ctx)
    if not heads:
        d.note = "no company news"
        return d
    moves = _moves(ctx, heads)
    lines = [f"{s} ({moves.get(s, 'no price history')}):\n  - " + "\n  - ".join(h[-6:]) for s, h in sorted(heads.items())]
    user = (f"Today is {ctx.now:%A %Y-%m-%d %H:%M} New York time. Next open: {ctx.next_open}. "
            f"Company headlines from the last 30 hours (recent price moves in brackets):\n\n" + "\n".join(lines))
    try:
        d.picks = _ask(ctx, user, set(heads))
    except Exception as exc:  # the AI being down is "no decision", never a guess
        d.note = f"AI call failed: {type(exc).__name__}"
    return d


def _ai_momentum(ctx: Context) -> Decision:
    """Hybrid: 12-month momentum shortlists 20 stocks; Claude reads their news and keeps up to 10."""
    d = Decision("ai_momentum_news", ctx.now.isoformat(timespec="seconds"), ctx.day.isoformat())
    me = zoo.month_ends(ctx.data.days)
    if pd.Timestamp(ctx.day) not in me:
        d.note = "rebalances at month end only"
        d.weights = None
        return d
    W = zoo.top_n(zoo.mom(ctx.data.C, 12), ctx.data, 20, pd.DatetimeIndex([pd.Timestamp(ctx.day)]))
    shortlist = [s for s, x in W.loc[pd.Timestamp(ctx.day)].items() if x > 0]
    if ctx.llm is None or ctx.live is None or not shortlist:
        d.note = "AI unavailable: no decision"
        return d
    from lumisafe.daytrade.universe import POOL  # noqa: F401

    items = ctx.live.news(shortlist, ctx.now - timedelta(days=30), ctx.now)
    heads: dict[str, list[str]] = {}
    for it in items:
        for s in it.get("symbols") or []:
            if s in shortlist and len(it.get("symbols")) <= 2:
                heads.setdefault(s, []).append(it.get("headline", "")[:160])
    moves = _moves(ctx, shortlist)
    lines = [f"{s} ({moves.get(s, '')}): " + " | ".join(heads.get(s, ["no recent news"])[-8:]) for s in shortlist]
    system = prompts.FACTORY_MOMENTUM_SYSTEM
    schema = {"type": "object", "additionalProperties": False, "required": ["keep"],
              "properties": {"keep": {"type": "array", "items": {"type": "string"}},
                             "notes": {"type": "string"}}}
    try:
        raw = ctx.llm.complete(system, f"Month end {ctx.day}.\n" + "\n".join(lines), json_schema=schema,
                               timeout=180)
        parsed = json.loads(raw)
        keep = [s.upper() for s in parsed.get("keep", []) if s.upper() in shortlist][:10]
        d.note = str(parsed.get("notes", ""))[:300]
    except Exception as exc:
        d.note = f"AI call failed: {type(exc).__name__}"
        return d
    if keep:
        d.weights = {s: 1.0 / len(keep) for s in keep}
    return d


def all_bots() -> list[Bot]:
    bots = _benchmarks()
    bots += [_quant(n, fn, b, desc) for n, (fn, b, desc) in zoo.STRATEGIES.items()]
    bots.append(Bot("news_rules", "qual", "short", "pool",
                    "Headline rules that passed the 10-year study: news gap up held (20 d), earnings beat + gap (10 d)",
                    _news_rules, "event"))
    bots.append(Bot("ai_news_analyst", "ai", "short", "pool",
                    "Claude reads each day's company headlines and picks up to 5 stocks to hold 5-20 days",
                    _ai_news, "event"))
    bots.append(Bot("ai_momentum_news", "ai", "long", "pool",
                    "Momentum shortlists 20 stocks monthly; Claude reads their news and keeps 10",
                    _ai_momentum, "monthly"))
    return bots
