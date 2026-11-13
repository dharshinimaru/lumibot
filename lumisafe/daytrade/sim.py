"""Minute-by-minute day-trading simulator with every cost and rule that changes the account's numbers.

Pure: no I/O, no clock, no network. Inputs are one session's candidates with
their real minute bars and real quoted spreads.

The setup (fixed before any test; only its few parameters are tuned, on
training years only):
  * a stock in the day's liquid universe opened >= ``min_gap`` above yesterday's
    close and had a headline before 09:44 (``dataset.py``),
  * after the first 15 minutes (the "opening range"), traded volume was at least
    ``min_rel_volume`` x its 20-day average daily volume,
  * buy when price breaks above the opening-range high, before ``entry_until``,
  * stop at the opening-range low (never further than ``max_stop_pct``), take
    profit at ``target_r`` x the risk, and sell everything ``flat_minutes`` before
    the close (13:00 closes handled). Long only; never held overnight.

What it charges and enforces (each one changes the account's numbers):
  * Bid/ask spread: real NBBO at the decision time. Market and stop orders pay
    half the spread on each side; a take-profit limit fills at its price only if
    the market trades through it.
  * Slippage on top of the spread; extra for stop orders (they fill in fast markets);
    a bar that opens beyond the stop fills at that open (gaps and trading halts).
  * Regulatory fees on every sale: SEC Section 31 fee and FINRA TAF (commission $0 at Alpaca).
  * Whole shares only (Alpaca doesn't allow fractional shares in bracket/stop orders).
  * Partial fills: no more than ``max_volume_participation`` of the minute's volume.
  * Cash: the slice never borrows; positions are limited by its cash. In a cash
    account, sale proceeds can't be reused until they settle (T+2, T+1 from 2024-05-28).
  * Pattern-day-trader rule: in a margin account under $25,000, no more than 3
    day trades in any 5 business days.
  * Daily loss halt: once the slice is down ``daily_loss_halt`` (realized + open),
    it sells everything at the next minute and stops for the day.
  * Max positions and max trades per day.
  * Same-minute ambiguity: if a bar touches both stop and target, the stop counts.
Taxes (short-term rates, wash sales) are applied to the trade list in ``report.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

import pandas as pd

from lumisafe.config.schema import DayTradeConfig

PDT_EQUITY_MIN = 25_000.0
PDT_MAX_DAY_TRADES = 3  # in any 5 business days, for margin accounts under $25k
T1_SETTLEMENT_FROM = date(2024, 5, 28)


@dataclass(frozen=True)
class Params:
    min_gap: float = 0.02
    max_gap: float = 0.12
    min_rel_volume: float = 0.10
    focused_news: bool = False  # require a headline naming at most 2 tickers (not a roundup)
    target_r: float = 1.0
    max_stop_pct: float = 0.03
    entry_until_minutes: int = 90  # after the open
    flat_minutes: int = 10  # before the close

    def label(self) -> str:
        return (f"gap>={self.min_gap:.0%} relvol>={self.min_rel_volume:.2f} target={self.target_r:g}R "
                f"news={'focused' if self.focused_news else 'any'}")


@dataclass(frozen=True)
class Costs:
    slippage: float = 0.0002  # beyond the quoted spread, market orders
    stop_slippage: float = 0.0005  # beyond the quoted spread, stop orders
    sec_fee_per_dollar: float = 27.80e-6  # highest Section 31 rate in 2016-2025 (conservative)
    taf_per_share: float = 0.000166
    taf_max: float = 8.30
    commission_per_order: float = 0.0  # Alpaca
    max_volume_participation: float = 0.01

    def sell_fees(self, shares: int, price: float) -> float:
        return shares * price * self.sec_fee_per_dollar + min(shares * self.taf_per_share, self.taf_max)


@dataclass
class Trade:
    day: str
    symbol: str
    entry_ts: str
    exit_ts: str
    shares: int
    entry_price: float
    exit_price: float
    exit_reason: str  # target | stop | close | halt
    spread_cost: float
    slippage_cost: float
    fees: float
    partial: bool

    @property
    def gross_pnl(self) -> float:
        return self.shares * (self.exit_price - self.entry_price) + self.spread_cost + self.slippage_cost

    @property
    def net_pnl(self) -> float:
        return self.shares * (self.exit_price - self.entry_price) - self.fees


@dataclass
class DayResult:
    day: str
    trades: list[Trade] = field(default_factory=list)
    halted: bool = False
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)


@dataclass
class AccountState:
    """What carries from day to day."""

    slice_equity: float
    other_equity: float  # the rest of the account (for the $25k PDT test)
    recent_day_trades: list[int] = field(default_factory=list)  # counts for the previous sessions, newest last
    unsettled: list[tuple[date, float]] = field(default_factory=list)  # (settles_on, amount), cash accounts


@dataclass
class _Setup:
    symbol: str
    bars: pd.DataFrame
    half_spread: float
    trigger: float
    or_low: float
    rel_volume: float


@dataclass
class _Position:
    setup: _Setup
    shares: int
    entry: float
    stop: float
    target: float
    entry_ts: datetime
    spread_cost: float
    slippage_cost: float
    partial: bool


@dataclass(frozen=True)
class Sizing:
    shares: int
    stop: float
    target: float
    partial: bool
    reason: str | None  # why it's 0 shares, if it is


def size_position(entry: float, or_low: float, half_spread: float, params: Params, cfg: DayTradeConfig,
                  costs: Costs, slice_equity: float, cash: float, bar_volume: float) -> Sizing:
    """Whole shares so that a stop-out loses about ``risk_per_trade`` of the slice (shared by test and paper)."""
    stop = max(or_low, entry * (1 - params.max_stop_pct))
    risk = entry - stop + entry * (half_spread + costs.stop_slippage)
    if stop >= entry or risk <= 0:
        return Sizing(0, stop, entry, False, "no room for a stop")
    want = math.floor(cfg.risk_per_trade * slice_equity / risk)
    cap_value = math.floor(cfg.max_position_fraction * slice_equity / entry)
    cap_cash = math.floor(max(cash, 0.0) / entry)
    cap_volume = math.floor(costs.max_volume_participation * float(bar_volume))
    shares = min(want, cap_value, cap_cash, cap_volume)
    unconstrained = min(want, cap_value, cap_cash)
    if shares < 1:
        return Sizing(0, stop, entry, False, "too small for a whole share" if unconstrained < 1 else "volume cap")
    return Sizing(shares, stop, entry + params.target_r * (entry - stop), shares < unconstrained, None)


def settlement_days(day: date) -> int:
    return 1 if day >= T1_SETTLEMENT_FROM else 2


def _skip(res: DayResult, why: str) -> None:
    res.skipped[why] = res.skipped.get(why, 0) + 1


def prepare(candidates: list, bars: dict[str, pd.DataFrame], params: Params, res: DayResult,
            opening_range_minutes: int = 15) -> list[_Setup]:
    setups = []
    for c in candidates:
        if not (params.min_gap <= c.gap <= params.max_gap):
            continue
        if params.focused_news and not any(h.get("n_symbols", 99) <= 2 for h in c.headlines):
            continue
        b = bars.get(c.symbol)
        if b is None or not c.has_bars or len(b) == 0:
            _skip(res, "no minute bars")
            continue
        if c.spread is None:
            _skip(res, "no quotes")
            continue
        open_ts = pd.Timestamp(c.open_ts)
        orng = b[(b.index >= open_ts) & (b.index < open_ts + pd.Timedelta(minutes=opening_range_minutes))]
        if len(orng) < opening_range_minutes // 2:
            _skip(res, "thin opening range")
            continue
        rel = float(orng["volume"].sum()) / c.avg_volume_20d if c.avg_volume_20d > 0 else 0.0
        if rel < params.min_rel_volume:
            continue
        setups.append(_Setup(c.symbol, b, c.spread / 2, float(orng["high"].max()) + 0.01,
                             float(orng["low"].min()), rel))
    return sorted(setups, key=lambda s: s.rel_volume, reverse=True)


def simulate_day(day: date, candidates: list, bars: dict[str, pd.DataFrame], params: Params,
                 cfg: DayTradeConfig, costs: Costs, acct: AccountState,
                 opening_range_minutes: int = 15) -> tuple[DayResult, AccountState]:
    res = DayResult(day.isoformat())
    acct = replace(acct, recent_day_trades=list(acct.recent_day_trades), unsettled=list(acct.unsettled))
    acct.unsettled = [(d, a) for d, a in acct.unsettled if d > day]
    if not candidates:
        acct.recent_day_trades = (acct.recent_day_trades + [0])[-4:]
        return res, acct

    open_ts = pd.Timestamp(candidates[0].open_ts)
    close_ts = pd.Timestamp(candidates[0].close_ts)
    decision = open_ts + pd.Timedelta(minutes=opening_range_minutes)
    entry_until = open_ts + pd.Timedelta(minutes=params.entry_until_minutes)
    flat_at = close_ts - pd.Timedelta(minutes=params.flat_minutes)
    setups = prepare(candidates, bars, params, res, opening_range_minutes)

    start_equity = acct.slice_equity
    halt_level = -cfg.daily_loss_halt * start_equity
    pdt_limited = (cfg.account_type == "margin" and acct.slice_equity + acct.other_equity < PDT_EQUITY_MIN)
    pdt_used = sum(acct.recent_day_trades[-4:])

    cash = start_equity - sum(a for _, a in acct.unsettled)
    positions: list[_Position] = []
    traded: set[str] = set()
    realized = 0.0
    halted = False
    minutes = sorted(set().union(*[set(s.bars.index[(s.bars.index >= decision) & (s.bars.index < close_ts)])
                                   for s in setups])) if setups else []

    def close_position(p: _Position, price: float, ts, reason: str, *, limit: bool, stop: bool) -> None:
        nonlocal cash, realized
        hs = 0.0 if limit else p.setup.half_spread
        slip = 0.0 if limit else (costs.stop_slippage if stop else costs.slippage)
        fill = price * (1 - hs - slip)
        fees = costs.sell_fees(p.shares, fill) + costs.commission_per_order * 2
        t = Trade(res.day, p.setup.symbol, p.entry_ts.isoformat(), pd.Timestamp(ts).isoformat(), p.shares,
                  p.entry, fill, reason, p.spread_cost + p.shares * price * hs,
                  p.slippage_cost + p.shares * price * slip, fees, p.partial)
        res.trades.append(t)
        proceeds = p.shares * fill - fees
        realized += t.net_pnl
        if cfg.account_type == "cash":
            acct.unsettled.append((_add_business_days(day, settlement_days(day)), proceeds))
        else:
            cash += proceeds
        positions.remove(p)

    for ts in minutes:
        # 1) exits on this minute's bar
        for p in list(positions):
            bar = p.setup.bars.loc[ts] if ts in p.setup.bars.index else None
            if bar is None:
                continue  # no trade this minute (thin or halted); checked again on the next bar
            if ts >= flat_at:
                close_position(p, float(bar["open"]), ts, "close", limit=False, stop=False)
            elif bar["low"] <= p.stop:
                close_position(p, min(p.stop, float(bar["open"])), ts, "stop", limit=False, stop=True)
            elif bar["high"] > p.target:
                close_position(p, p.target, ts, "target", limit=True, stop=False)

        # 2) daily loss halt on realized + open (marked at the bid)
        if not halted:
            open_pnl = 0.0
            for p in positions:
                px = p.setup.bars["close"][p.setup.bars.index <= ts]
                if len(px):
                    open_pnl += p.shares * (float(px.iloc[-1]) * (1 - p.setup.half_spread) - p.entry)
            if realized + open_pnl <= halt_level:
                halted = True
                res.halted = True
                for p in list(positions):
                    nxt = p.setup.bars[p.setup.bars.index > ts]
                    row = nxt.iloc[0] if len(nxt) else p.setup.bars.iloc[-1]
                    close_position(p, float(row["open"] if len(nxt) else row["close"]),
                                   nxt.index[0] if len(nxt) else p.setup.bars.index[-1], "halt",
                                   limit=False, stop=False)

        # 3) entries
        if halted or ts >= entry_until or ts >= flat_at:
            continue
        for s in setups:
            if s.symbol in traded or len(positions) >= cfg.max_positions or len(traded) >= cfg.max_trades_per_day:
                continue
            if ts not in s.bars.index:
                continue
            bar = s.bars.loc[ts]
            if bar["high"] < s.trigger:
                continue
            traded.add(s.symbol)  # one attempt per stock per day
            if pdt_limited and pdt_used + len(res.trades) + len(positions) >= PDT_MAX_DAY_TRADES:
                _skip(res, "pattern-day-trader limit")
                continue
            ref = max(s.trigger, float(bar["open"]))
            entry = ref * (1 + s.half_spread + costs.stop_slippage)  # buy-stop order: spread + stop slippage
            z = size_position(entry, s.or_low, s.half_spread, params, cfg, costs, start_equity, cash,
                              float(bar["volume"]))
            if z.shares < 1:
                if z.reason != "no room for a stop":
                    _skip(res, z.reason)
                continue
            cash -= z.shares * entry + costs.commission_per_order
            positions.append(_Position(s, z.shares, entry, z.stop, z.target, ts, z.shares * ref * s.half_spread,
                                       z.shares * ref * costs.stop_slippage, partial=z.partial))

    # anything still open (no bar at/after the flat time): last real trade price of the day
    for p in list(positions):
        close_position(p, float(p.setup.bars["close"].iloc[-1]), p.setup.bars.index[-1], "close",
                       limit=False, stop=False)

    acct.slice_equity = start_equity + realized
    acct.recent_day_trades = (acct.recent_day_trades + [len(res.trades)])[-4:]
    return res, acct


def _add_business_days(d: date, n: int) -> date:
    out = d
    while n > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out
