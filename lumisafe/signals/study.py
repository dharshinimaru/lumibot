"""The signal study: every signal x holding period, on real data, after costs, judged out of sample.

PRE-REGISTERED (written before any result was seen; see DECISIONS.md). Changing a
signal or the rules after looking at results would turn this into curve fitting.

Signals (long only; entries at a market OPEN, exits at a CLOSE ``hold`` sessions later):
  News only, entered at the first open >= 5 minutes after the headline:
    earnings_beat_strong  EPS surprise >= +5% AND a sales beat in the same headline
    earnings_beat         any EPS beat
    upgrade_bullish       analyst upgrade to a buy-type rating
    initiate_bullish      new coverage starting at a buy-type rating
    pt_raise_cluster      >= 2 price-target raises on the stock within 24 hours
    guide_up              company raises guidance / guides above the estimate
  News confirmed by the first day's price, entered at the NEXT open:
    beat_gap_held         earnings beat; opened >= 2% up and closed at or above that open
    upgrade_gap_held      upgrade or bullish initiation; closed >= 2% above the previous close
    news_gap_held         any single-ticker headline in the prior 16 h; opened >= 3% up, closed >= open
  Price only, entered at the next open:
    new_high_volume       closed at a 252-day high on >= 2x its 20-day average volume
    pullback_uptrend      above its 200-day average but down >= 7% over 5 days
    gap_down_no_news      opened >= 4% down with no single-ticker headline in the prior 24 h
    rel_strength          first session of each month: the 10 strongest 126-day performers
  For information only (long-only can't profit from them; they feed the "avoid" list):
    earnings_miss, downgrade, guide_down

Holding periods: 5, 10, 20 sessions (tradable; the SPEC's 5-day satellite minimum),
2 sessions for information only.

Costs per round trip: the real quoted spread (median of the 09:45 NBBO samples for
that stock and year; all-stock median for that year when the stock has none), plus
0.02% slippage each side, plus the SEC/FINRA fees on the sale. Prices: Alpaca daily
bars adjusted for splits AND dividends (total return). Universe: the day's top 100
of the pool by trailing 20-day dollar volume.

Judging rules:
  * "excess" = net return minus SPY's total return over the same open-to-close window;
    t-statistics cluster trades by entry date (same-day trades aren't independent).
  * Walk-forward portfolio (the promotion test): for each year 2018-2025 select signals
    using ONLY trades that closed before that year: >= 30 trades, mean excess > 0 and
    t >= 2.0 (best holding period per signal). Trade them that year in a 10% slice,
    10% of the slice per position, at most 10 open. Stitch the years; same SPEC §8 bar.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from lumisafe.daytrade.universe import dollar_volume_rank
from lumisafe.signals.events import BULLISH, Event

HOLDS_TRADABLE = (5, 10, 20)
HOLDS_INFO = (2,)
SLIPPAGE = 0.0002
SELL_FEE = 27.80e-6 + 0.00002  # SEC fee + FINRA TAF (per $; TAF approximated per dollar at ~$100/share)
ENTRY_DELAY = timedelta(minutes=5)
INFO_ONLY = {"earnings_miss", "downgrade", "guide_down"}
SIGNALS = ("earnings_beat_strong", "earnings_beat", "upgrade_bullish", "initiate_bullish", "pt_raise_cluster",
           "guide_up", "beat_gap_held", "upgrade_gap_held", "news_gap_held", "new_high_volume",
           "pullback_uptrend", "gap_down_no_news", "rel_strength", "earnings_miss", "downgrade", "guide_down")
MIN_TRADES, MIN_T = 30, 2.0


@dataclass(frozen=True)
class Entry:
    signal: str
    symbol: str
    entry_day: date  # the session whose OPEN we buy at
    why: str


@dataclass
class TradeResult:
    signal: str
    symbol: str
    hold: int
    entry_day: date
    exit_day: date
    gross: float
    cost: float
    net: float
    spy: float
    pool: float | None = None  # equal-weight return of that day's same-stock universe over the same window

    @property
    def excess(self) -> float:
        return self.net - self.spy

    @property
    def vs_pool(self) -> float | None:
        return None if self.pool is None else self.net - self.pool


# --- data plumbing ------------------------------------------------------------------------------------------


class Market:
    """Daily total-return bars, the session calendar and position lookups."""

    def __init__(self, bars: dict[str, pd.DataFrame], days: list[date]):
        self.bars = {s: b.sort_index() for s, b in bars.items() if len(b)}
        self.days = days
        self.index = {d: i for i, d in enumerate(days)}

    def next_session_on_or_after(self, ts: datetime, open_time: Callable[[date], datetime]) -> date | None:
        d = ts.date()
        # find the first session whose open is >= ts + delay
        i = self._first_index_on_or_after(d)
        while i is not None and i < len(self.days):
            if open_time(self.days[i]) >= ts + ENTRY_DELAY:
                return self.days[i]
            i += 1
        return None

    def _first_index_on_or_after(self, d: date) -> int | None:
        lo, hi = 0, len(self.days)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.days[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(self.days) else None

    def shift(self, d: date, n: int) -> date | None:
        i = self.index.get(d)
        if i is None or not (0 <= i + n < len(self.days)):
            return None
        return self.days[i + n]

    def row(self, sym: str, d: date) -> pd.Series | None:
        b = self.bars.get(sym)
        ts = pd.Timestamp(d)
        if b is None or ts not in b.index:
            return None
        return b.loc[ts]


def spread_model(samples: dict[str, float | None]) -> Callable[[str, int], float]:
    """Median real quoted spread per (stock, year), then per year, from the 09:45 NBBO samples."""
    by_sym_year: dict[tuple[str, int], list[float]] = defaultdict(list)
    by_year: dict[int, list[float]] = defaultdict(list)
    for key, v in samples.items():
        if v is None:
            continue
        sym, when = key.split("|")
        y = int(when[:4])
        by_sym_year[(sym, y)].append(v)
        by_year[y].append(v)
    sym_year = {k: float(np.median(v)) for k, v in by_sym_year.items()}
    year = {k: float(np.median(v)) for k, v in by_year.items()}
    overall = float(np.median([x for v in by_year.values() for x in v])) if by_year else 0.001

    def f(sym: str, y: int) -> float:
        return sym_year.get((sym, y), year.get(y, overall))

    return f


# --- signal generation ----------------------------------------------------------------------------------------


def generate(events: list[Event], mkt: Market, open_time: Callable[[date], datetime],
             universe_fn: Callable[[date], set[str]]) -> list[Entry]:
    """All entries for every signal (pure given its inputs; uses only data known at each decision)."""
    out: list[Entry] = []
    by_sym_kind: dict[tuple[str, str], list[Event]] = defaultdict(list)
    news_times: dict[str, list[datetime]] = defaultdict(list)
    headlines: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for e in events:
        by_sym_kind[(e.symbol, e.kind)].append(e)
        news_times[e.symbol].append(e.at)
        headlines[e.symbol].append((e.at, e.headline, e.kind))
    for v in news_times.values():
        v.sort()
    for v in headlines.values():
        v.sort()

    good_news = ("earnings_beat", "guide_up", "upgrade", "initiate_bullish", "sales_beat")

    def latest_headline(sym: str, lo: datetime, hi: datetime) -> str:
        """The headline that most likely explains a gap up: good news first, else the latest one."""
        hs = [(t, h, k) for t, h, k in headlines.get(sym, []) if lo <= t <= hi]
        good = [h for t, h, k in hs if k in good_news]
        return good[-1] if good else (hs[-1][1] if hs else "company news")

    def at_open(sig, e: Event, why: str):
        d = mkt.next_session_on_or_after(e.at, open_time)
        if d is not None and e.symbol in universe_fn(d):
            out.append(Entry(sig, e.symbol, d, why))

    def confirmed(sig, e: Event, cond, why: str):
        d0 = mkt.next_session_on_or_after(e.at, open_time)
        if d0 is None or e.symbol not in universe_fn(d0):
            return
        r0, prev = mkt.row(e.symbol, d0), mkt.row(e.symbol, mkt.shift(d0, -1) or d0)
        d1 = mkt.shift(d0, 1)
        if r0 is not None and prev is not None and d1 is not None and cond(r0, prev):
            out.append(Entry(sig, e.symbol, d1, why))

    for (sym, kind), evs in by_sym_kind.items():
        for e in evs:
            if kind == "earnings_beat":
                at_open("earnings_beat", e, e.headline)
                same = [x for x in by_sym_kind.get((sym, "sales_beat"), []) if x.headline == e.headline]
                if isinstance(e.value, float) and e.value >= 0.05 and same:
                    at_open("earnings_beat_strong", e, e.headline)
                confirmed("beat_gap_held", e, lambda r, p: r["open"] >= 1.02 * p["close"] and r["close"] >= r["open"],
                          e.headline)
            elif kind == "upgrade" and str(e.value) in BULLISH:
                at_open("upgrade_bullish", e, e.headline)
                confirmed("upgrade_gap_held", e, lambda r, p: r["close"] >= 1.02 * p["close"], e.headline)
            elif kind == "initiate_bullish":
                at_open("initiate_bullish", e, e.headline)
                confirmed("upgrade_gap_held", e, lambda r, p: r["close"] >= 1.02 * p["close"], e.headline)
            elif kind == "guide_up":
                at_open("guide_up", e, e.headline)
            elif kind in INFO_ONLY:
                at_open(kind, e, e.headline)
        if kind == "pt_raise":
            evs = sorted(evs, key=lambda x: x.at)
            used: set[date] = set()
            for i, e in enumerate(evs):
                window = [x for x in evs[: i + 1] if e.at - x.at <= timedelta(hours=24)]
                if len(window) >= 2:
                    d = mkt.next_session_on_or_after(e.at, open_time)
                    if d is not None and d not in used and sym in universe_fn(d):
                        used.add(d)
                        out.append(Entry("pt_raise_cluster", sym, d, f"{len(window)} price-target raises in 24h"))

    # price-confirmed news gap and the price-only signals, day by day
    for sym, b in mkt.bars.items():
        if sym == "SPY" or len(b) < 260:
            continue
        close, opn, vol = b["close"], b["open"], b["volume"]
        hi252 = close.rolling(252, min_periods=252).max()
        sma200 = close.rolling(200, min_periods=200).mean()
        vol20 = vol.shift(1).rolling(20, min_periods=20).mean()
        ret5 = close / close.shift(5) - 1
        prev_close = close.shift(1)
        times = news_times.get(sym, [])
        for ts in b.index[260:]:
            d = ts.date()
            if d not in mkt.index or sym not in universe_fn(d):
                continue
            d1 = mkt.shift(d, 1)
            if d1 is None:
                continue
            o, c, pc = float(opn[ts]), float(close[ts]), float(prev_close[ts])
            if close[ts] >= hi252[ts] and vol[ts] >= 2 * vol20[ts]:
                out.append(Entry("new_high_volume", sym, d1, "closed at a 1-year high on 2x volume"))
            if close[ts] > sma200[ts] and ret5[ts] <= -0.07:
                out.append(Entry("pullback_uptrend", sym, d1, "down 7%+ in 5 days, still above its 200-day average"))
            if pc > 0 and o >= 1.03 * pc and c >= o and _news_between(times, open_time(d) - timedelta(hours=16),
                                                                      open_time(d)):
                out.append(Entry("news_gap_held", sym, d1, "opened 3%+ up on news and held into the close: " +
                                 # explanation only: look back 24 h so last evening's earnings show up; the
                                 # signal's own news test above stays the pre-registered 16 h
                                 latest_headline(sym, open_time(d) - timedelta(hours=24), open_time(d))))
            if pc > 0 and o <= 0.96 * pc and not _news_between(times, open_time(d) - timedelta(hours=24),
                                                                open_time(d)):
                # buy at THIS open is not possible after seeing it gap; enter at the next open
                out.append(Entry("gap_down_no_news", sym, d1, "opened 4%+ down with no company news"))

    # relative strength: first session of each month
    firsts = [d for i, d in enumerate(mkt.days) if i == 0 or d.month != mkt.days[i - 1].month]
    for d in firsts:
        prev = mkt.shift(d, -1)
        if prev is None:
            continue
        scores = {}
        for sym in universe_fn(d):
            b = mkt.bars.get(sym)
            if b is None:
                continue
            hist = b["close"][b.index <= pd.Timestamp(prev)]
            if len(hist) > 126:
                scores[sym] = float(hist.iloc[-1] / hist.iloc[-127] - 1)
        for sym in sorted(scores, key=scores.get, reverse=True)[:10]:
            out.append(Entry("rel_strength", sym, d, "among the 10 strongest 6-month performers"))
    return out


def _news_between(times: list[datetime], lo: datetime, hi: datetime) -> bool:
    import bisect

    i = bisect.bisect_left(times, lo)
    return i < len(times) and times[i] <= hi


# --- evaluation ------------------------------------------------------------------------------------------------


def evaluate(entries: list[Entry], mkt: Market, spread: Callable[[str, int], float],
             holds=HOLDS_TRADABLE + HOLDS_INFO,
             pool_fn: Callable[[date, date], float | None] | None = None) -> list[TradeResult]:
    """Net and SPY-relative return of every entry at every holding period (one per signal/stock/day)."""
    out, seen = [], set()
    for en in entries:
        key = (en.signal, en.symbol, en.entry_day)
        if key in seen:
            continue
        seen.add(key)
        r_in, s_in = mkt.row(en.symbol, en.entry_day), mkt.row("SPY", en.entry_day)
        if r_in is None or s_in is None or float(r_in["open"]) <= 0:
            continue
        for h in holds:
            xd = mkt.shift(en.entry_day, h - 1)
            if xd is None:
                continue
            r_out, s_out = mkt.row(en.symbol, xd), mkt.row("SPY", xd)
            if r_out is None or s_out is None:
                continue  # missing data stays missing (RULE #1)
            gross = float(r_out["close"]) / float(r_in["open"]) - 1
            cost = spread(en.symbol, en.entry_day.year) + 2 * SLIPPAGE + SELL_FEE
            spy = float(s_out["close"]) / float(s_in["open"]) - 1
            pool = pool_fn(en.entry_day, xd) if pool_fn is not None else None
            out.append(TradeResult(en.signal, en.symbol, h, en.entry_day, xd, gross, cost, gross - cost, spy, pool))
    return out


@dataclass
class Stats:
    signal: str
    hold: int
    period: str
    trades: int
    hit_rate: float  # share of trades positive AFTER costs
    mean_net: float
    mean_excess: float
    t_excess: float  # clustered by entry date
    mean_vs_pool: float = 0.0  # vs holding the same day's stock universe equally (removes stock-list bias)
    t_vs_pool: float = 0.0

    @property
    def tradable(self) -> bool:
        return self.signal not in INFO_ONLY and self.hold in HOLDS_TRADABLE


def stats(trades: list[TradeResult], signal: str, hold: int, period: str) -> Stats:
    ts = [t for t in trades if t.signal == signal and t.hold == hold]
    if not ts:
        return Stats(signal, hold, period, 0, 0.0, 0.0, 0.0, 0.0)
    t_stat = _clustered_t(ts, lambda t: t.excess)
    pooled = [t for t in ts if t.vs_pool is not None]
    return Stats(signal, hold, period, len(ts), float(np.mean([t.net > 0 for t in ts])),
                 float(np.mean([t.net for t in ts])), float(np.mean([t.excess for t in ts])), t_stat,
                 float(np.mean([t.vs_pool for t in pooled])) if pooled else 0.0,
                 _clustered_t(pooled, lambda t: t.vs_pool) if pooled else 0.0)


def _clustered_t(ts: list[TradeResult], value: Callable[[TradeResult], float]) -> float:
    """t-statistic of the mean, with trades entered on the same day averaged first (they aren't independent)."""
    by_day: dict[date, list[float]] = defaultdict(list)
    for t in ts:
        by_day[t.entry_day].append(value(t))
    daily = np.array([np.mean(v) for v in by_day.values()])
    if len(daily) <= 2 or daily.std(ddof=1) == 0:
        return 0.0
    return float(daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))))


ROBUST_T = 1.5


def robust_picks(trades: list[TradeResult], chosen: dict[str, int], split: date) -> dict[str, int]:
    """Of ``chosen``, keep signals that ALSO beat the same-stock benchmark (t >= 1.5) both before and after
    ``split``. Added after the first full run exposed stock-list bias; it can only remove signals."""
    keep = {}
    for s, h in chosen.items():
        a = stats([t for t in trades if t.entry_day <= split], s, h, "a")
        b = stats([t for t in trades if t.entry_day > split], s, h, "b")
        if a.mean_vs_pool > 0 and b.mean_vs_pool > 0 and a.t_vs_pool >= ROBUST_T and b.t_vs_pool >= ROBUST_T:
            keep[s] = h
    return keep


def table(trades: list[TradeResult], period: str) -> list[Stats]:
    return [stats(trades, s, h, period) for s in SIGNALS for h in HOLDS_TRADABLE + HOLDS_INFO]


def select(trades: list[TradeResult], before: date) -> dict[str, int]:
    """Signals (and their best holding period) that passed on trades CLOSED before ``before``."""
    known = [t for t in trades if t.exit_day < before]
    chosen: dict[str, tuple[int, float]] = {}
    for s in SIGNALS:
        if s in INFO_ONLY:
            continue
        for h in HOLDS_TRADABLE:
            st = stats(known, s, h, "train")
            if st.trades >= MIN_TRADES and st.mean_excess > 0 and st.mean_net > 0 and st.t_excess >= MIN_T:
                if s not in chosen or st.t_excess > chosen[s][1]:
                    chosen[s] = (h, st.t_excess)
    return {s: h for s, (h, _) in sorted(chosen.items(), key=lambda kv: -kv[1][1])}


# --- walk-forward portfolio -------------------------------------------------------------------------------------


def portfolio_returns(trades: list[TradeResult], chosen: dict[str, int], mkt: Market, start: date, end: date,
                      max_positions: int = 10, weight: float = 0.10) -> tuple[pd.Series, list[TradeResult]]:
    """Daily returns of a slice that takes the chosen signals' trades (entry in [start, end])."""
    cands = sorted((t for t in trades if t.signal in chosen and t.hold == chosen[t.signal]
                    and start <= t.entry_day <= end),
                   key=lambda t: (t.entry_day, list(chosen).index(t.signal)))
    days = [d for d in mkt.days if start <= d <= end]
    open_pos: list[TradeResult] = []
    taken: list[TradeResult] = []
    by_day: dict[date, list[TradeResult]] = defaultdict(list)
    for t in cands:
        by_day[t.entry_day].append(t)
    rets = {}
    for d in days:
        open_pos = [p for p in open_pos if p.exit_day >= d]
        held_syms = {p.symbol for p in open_pos}
        for t in by_day.get(d, []):
            if len(open_pos) < max_positions and t.symbol not in held_syms:
                open_pos.append(t)
                taken.append(t)
                held_syms.add(t.symbol)
        r = 0.0
        for p in open_pos:
            r += weight * _day_return(p, d, mkt)
        rets[pd.Timestamp(d)] = r
    return pd.Series(rets, dtype=float), taken


def _day_return(p: TradeResult, d: date, mkt: Market) -> float:
    """This position's contribution on day d: open->close on entry, close->close after, costs on entry/exit days."""
    row = mkt.row(p.symbol, d)
    if row is None:
        return 0.0
    if d == p.entry_day:
        base = float(row["open"])
        r = float(row["close"]) / base - 1 - (p.cost - SELL_FEE) / 2  # entry pays half the spread + slippage
    else:
        prev = mkt.row(p.symbol, mkt.shift(d, -1))
        r = float(row["close"]) / float(prev["close"]) - 1 if prev is not None else 0.0
    if d == p.exit_day:
        r -= (p.cost - SELL_FEE) / 2 + SELL_FEE
    return r
