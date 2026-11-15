"""Strategy zoo: many classic strategies, one honest test (pre-registered 2026-11-14, before any result).

Owner's goal: find strategies whose decisions can be copied for profit. Testing many and keeping the best
backtest mostly finds luck, so the rules below are fixed BEFORE the first run and committed with this file.

Data: Alpaca SIP daily bars adjusted for splits AND dividends (total return), 2016-01-04 → 2026-11-13, for the
~155-stock pool (incl. names later delisted) plus 22 ETFs. Cash earns BIL.

Execution (no look-ahead): weights are decided from data up to a day's close and traded at the NEXT open.
A day's return = old weights × overnight move + new weights × open→close move. Costs on every change in weight:
half the median real quoted spread for that stock and year (from our 09:45 NBBO samples; 0.02% for ETFs) plus
0.05% slippage. Long only, no leverage (weights sum ≤ 1). Between rebalances target weights are held
(drift turnover ignored; small, and it is the same for the benchmarks).

Universe for stock strategies: each day's top 100 pool stocks by trailing 20-day dollar volume (point in time).

Periods:
  * DISCOVERY  2017-01-01 … 2023-12-31  (2016 is warm-up for 12-month lookbacks)
  * VALIDATION 2024-01-01 … 2025-12-31  (none of these strategies was ever run on it before)
  * FRESH      2026-01-01 … 2026-11-13  (no part of this project has used 2026 prices to choose anything)

Benchmarks (the comparison that removes the pool's survivorship bias):
  * stock-picking strategies → equal weight of the same daily top-100 universe, rebalanced monthly
  * SPY / ETF timing and allocation strategies → SPY

Pass rule (all of):
  1. Deflated Sharpe ratio ≥ 0.95 on DISCOVERY daily excess returns vs its benchmark, deflated for the
     number of strategies tried (Bailey & López de Prado 2014). This is the "higher bar for more tries".
  2. Absolute Sharpe ≥ 0.5 on DISCOVERY (SPEC §8 bar).
  3. Positive annualized excess return vs its benchmark on VALIDATION.
FRESH 2026 is reported for every strategy and never used to pass or fail anything.

The list of strategies (STRATEGIES below) and every parameter are fixed in this commit. Nothing is added,
removed or re-tuned after results are seen; any later change is logged in DECISIONS.md as post-hoc.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from statistics import NormalDist
from typing import Callable

import numpy as np
import pandas as pd

ETF_COST = 0.0002
SLIPPAGE = 0.0005
TRADING_DAYS = 252
PERIODS = {"discovery": ("2017-01-01", "2023-12-31"), "validation": ("2024-01-01", "2025-12-31"),
           "fresh": ("2026-01-01", "2026-11-13")}
SECTORS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU"]  # XLRE/XLC start late: excluded
ETFS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "AGG", "TLT", "IEF", "GLD", "BIL", "SHY", *SECTORS, "XLRE", "XLC"]


# --- data ----------------------------------------------------------------------------------------------------


@dataclass
class Data:
    C: pd.DataFrame  # adjusted close, dates × symbols
    O: pd.DataFrame  # adjusted open
    V: pd.DataFrame  # volume
    U: pd.DataFrame  # bool: in the day's top-100 stock universe (known before that day's open)
    stocks: list[str]

    @property
    def days(self) -> pd.DatetimeIndex:
        return self.C.index


def build(bars: dict[str, pd.DataFrame], top_n: int = 100) -> Data:
    C = pd.DataFrame({s: b["close"] for s, b in bars.items() if len(b)}).sort_index().astype(float)
    O = pd.DataFrame({s: b["open"] for s, b in bars.items() if len(b)}).reindex(C.index).astype(float)
    V = pd.DataFrame({s: b["volume"] for s, b in bars.items() if len(b)}).reindex(C.index).astype(float)
    stocks = [s for s in C.columns if s not in ETFS]
    dv = (C[stocks] * V[stocks]).rolling(20, min_periods=20).mean().shift(1)
    rank = dv.rank(axis=1, ascending=False)
    U = (rank <= top_n) & C[stocks].notna()
    return Data(C, O, V, U.reindex(columns=C.columns, fill_value=False), stocks)


@lru_cache(maxsize=256)
def _more_sessions_in(last: pd.Timestamp, period: str) -> bool:
    """True if the NYSE trades again after ``last`` in the same month/ISO week (so ``last`` isn't its end).

    Without this, the final day of the data always looks like a month end, which in live use would make
    every monthly bot rebalance daily (found on the first live factory run, 2026-11-14)."""
    from lumisafe.daytrade.universe import sessions

    d = last.date()
    if period == "month":
        end = (last + pd.offsets.MonthEnd(0)).date()
    else:
        end = d + timedelta(days=6 - d.weekday())
    return end > d and any(x.day > d for x in sessions(d + timedelta(days=1), end))


def _drop_unfinished(ends: pd.DatetimeIndex, period: str) -> pd.DatetimeIndex:
    if len(ends) and _more_sessions_in(pd.Timestamp(ends[-1]), period):
        return ends[:-1]
    return ends


def month_ends(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(days, index=days)
    return _drop_unfinished(pd.DatetimeIndex(s.groupby([days.year, days.month]).max().values), "month")


def week_ends(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(days, index=days)
    iso = days.isocalendar()
    return _drop_unfinished(pd.DatetimeIndex(s.groupby([iso.year.values, iso.week.values]).max().values), "week")


def _hold(sparse: dict[pd.Timestamp, dict[str, float]], data: Data) -> pd.DataFrame:
    """Target weights set on rebalance days, held until the next one."""
    W = pd.DataFrame(np.nan, index=data.days, columns=data.C.columns)
    for d, w in sparse.items():
        W.loc[d] = 0.0
        for s, x in w.items():
            W.loc[d, s] = x
    return W.ffill().fillna(0.0)


def top_n(score: pd.DataFrame, data: Data, n: int, when: pd.DatetimeIndex, largest: bool = True,
          gate: pd.Series | None = None, off: dict[str, float] | None = None) -> pd.DataFrame:
    """Equal weight the ``n`` best-scoring universe stocks on each rebalance day.

    ``gate`` (bool by day): when False, hold ``off`` instead (e.g. cash / bonds)."""
    sparse = {}
    for d in when:
        if gate is not None and not bool(gate.get(d, False)):
            sparse[d] = dict(off or {})
            continue
        row = score.loc[d, data.stocks][data.U.loc[d, data.stocks]].dropna()
        if len(row) < n:
            sparse[d] = {}
            continue
        pick = row.nlargest(n) if largest else row.nsmallest(n)
        sparse[d] = {s: 1.0 / n for s in pick.index}
    return _hold(sparse, data)


# --- backtest engine -------------------------------------------------------------------------------------------


def simulate(W: pd.DataFrame, data: Data, spread_fn: Callable[[str, int], float]) -> pd.Series:
    """Daily net returns. W.loc[d] is decided at d's close and traded at the next open."""
    W = W.reindex(index=data.days, columns=data.C.columns).fillna(0.0)
    r_on = (data.O / data.C.shift(1) - 1).fillna(0.0)
    r_id = (data.C / data.O - 1).fillna(0.0)
    held_prev = W.shift(2).fillna(0.0)  # through the overnight gap into day d
    held_new = W.shift(1).fillna(0.0)  # from day d's open
    gross = (held_prev * r_on).sum(axis=1) + (held_new * r_id).sum(axis=1)
    bil = (data.C["BIL"] / data.C["BIL"].shift(1) - 1).fillna(0.0) if "BIL" in data.C else 0.0
    gross = gross + (1 - held_new.sum(axis=1)).clip(lower=0) * bil
    dW = (W - W.shift(1).fillna(0.0)).abs().shift(1).fillna(0.0)  # traded at day d's open
    years = data.days.year
    cost_rate = pd.DataFrame({s: ([ETF_COST] * len(years) if s in ETFS else
                                  [spread_fn(s, int(y)) / 2 for y in years]) for s in data.C.columns},
                             index=data.days) + SLIPPAGE
    return gross - (dW * cost_rate).sum(axis=1)


# --- metrics -------------------------------------------------------------------------------------------------


def seg(r: pd.Series, period: str) -> pd.Series:
    a, b = PERIODS[period]
    return r.loc[a:b]


def ann_return(r: pd.Series) -> float:
    if len(r) == 0:
        return float("nan")
    return float((1 + r).prod() ** (TRADING_DAYS / len(r)) - 1)


def sharpe(r: pd.Series) -> float:
    sd = r.std()
    return float(r.mean() / sd * math.sqrt(TRADING_DAYS)) if sd and sd > 0 else 0.0


def max_dd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float((1 - eq / eq.cummax()).max()) if len(eq) else 0.0


def deflated_sharpe(excess: pd.Series, trial_srs: list[float]) -> float:
    """Probability the true Sharpe of ``excess`` > 0 after trying ``len(trial_srs)`` strategies.

    Per-period Sharpe; benchmark SR0 = expected max of N trial Sharpes under the null
    (Bailey & López de Prado, "The Deflated Sharpe Ratio", 2014)."""
    x = excess.dropna()
    n, T = len(trial_srs), len(x)
    if T < 30 or not x.std() > 0:
        return 0.0
    sr = x.mean() / x.std()
    var = float(np.var(trial_srs, ddof=1)) if n > 1 else 0.0
    nd, g = NormalDist(), 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * nd.inv_cdf(1 - 1 / n) + g * nd.inv_cdf(1 - 1 / (n * math.e))) if n > 1 else 0.0
    skew = float(((x - x.mean()) ** 3).mean() / x.std() ** 3)
    kurt = float(((x - x.mean()) ** 4).mean() / x.std() ** 4)
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return nd.cdf((sr - sr0) * math.sqrt(T - 1) / denom)


# --- indicators ------------------------------------------------------------------------------------------------


def mom(C: pd.DataFrame, months: int, skip: int = 21) -> pd.DataFrame:
    return C.shift(skip) / C.shift(skip + 21 * months) - 1


def vol(C: pd.DataFrame, n: int) -> pd.DataFrame:
    return C.pct_change().rolling(n, min_periods=n).std()


def rsi(C: pd.DataFrame, n: int) -> pd.DataFrame:
    d = C.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


# --- the strategies (fixed list) ---------------------------------------------------------------------------------


def _spy_above(data: Data, n: int) -> pd.Series:
    spy = data.C["SPY"]
    return spy > spy.rolling(n, min_periods=n).mean()


def s_momentum(months: int, n: int):
    return lambda data: top_n(mom(data.C, months), data, n, month_ends(data.days))


def s_vol_adj_mom(n: int):
    return lambda data: top_n(mom(data.C, 12) / vol(data.C, 126), data, n, month_ends(data.days))


def s_low_vol(n: int):
    return lambda data: top_n(vol(data.C, 63), data, n, month_ends(data.days), largest=False)


def s_low_beta(n: int):
    def f(data):
        r = data.C.pct_change()
        cov = r.rolling(252, min_periods=200).cov(r["SPY"])
        beta = cov.div(r["SPY"].rolling(252, min_periods=200).var(), axis=0)
        return top_n(beta, data, n, month_ends(data.days), largest=False)
    return f


def s_high52(n: int):
    return lambda data: top_n(data.C / data.C.rolling(252, min_periods=200).max(), data, n, month_ends(data.days))


def s_mom_trend(n: int):
    return lambda data: top_n(mom(data.C, 12), data, n, month_ends(data.days), gate=_spy_above(data, 200), off={})


def s_lowvol_trend(n: int):
    return lambda data: top_n(vol(data.C, 63), data, n, month_ends(data.days), largest=False,
                              gate=_spy_above(data, 200), off={})


def s_reversal_week(n: int, uptrend: bool = False):
    def f(data):
        score = data.C / data.C.shift(5) - 1
        if uptrend:
            score = score.where(data.C > data.C.rolling(200, min_periods=200).mean())
        return top_n(score, data, n, week_ends(data.days), largest=False)
    return f


def s_reversal_month(n: int):
    return lambda data: top_n(data.C / data.C.shift(21) - 1, data, n, month_ends(data.days), largest=False)


def s_mom_lowvol_combo(n: int):
    def f(data):
        m = mom(data.C, 12)[data.stocks].rank(axis=1, pct=True)
        v = (-vol(data.C, 63)[data.stocks]).rank(axis=1, pct=True)
        return top_n((m + v).reindex(columns=data.C.columns), data, n, month_ends(data.days))
    return f


def s_rsi2(threshold: float, max_pos: int = 10):
    """Connors: buy universe stocks above their 200-day average when RSI(2) < threshold; sell above the 5-day average."""
    def f(data):
        r2, sma200 = rsi(data.C, 2), data.C.rolling(200, min_periods=200).mean()
        sma5 = data.C.rolling(5, min_periods=5).mean()
        W = pd.DataFrame(0.0, index=data.days, columns=data.C.columns)
        held: list[str] = []
        for d in data.days:
            c = data.C.loc[d]
            held = [s for s in held if not (c[s] > sma5.loc[d, s]) and not math.isnan(c[s])]
            if len(held) < max_pos:
                u = data.U.loc[d, data.stocks]
                cand = r2.loc[d, data.stocks][u & (c[data.stocks] > sma200.loc[d, data.stocks])]
                for s in cand[cand < threshold].nsmallest(max_pos).index:
                    if s not in held and len(held) < max_pos:
                        held.append(s)
            for s in held:
                W.loc[d, s] = 1.0 / max_pos
        return W
    return f


def _daily(signal: pd.Series, on: dict[str, float], off: dict[str, float], data: Data) -> pd.DataFrame:
    W = pd.DataFrame(0.0, index=data.days, columns=data.C.columns)
    for s, x in on.items():
        W.loc[signal.fillna(False).astype(bool), s] = x
    for s, x in off.items():
        W.loc[~signal.fillna(False).astype(bool), s] = x
    return W


def s_spy_sma(n: int):
    return lambda data: _daily(_spy_above(data, n), {"SPY": 1.0}, {}, data)


def s_spy_cross():
    def f(data):
        spy = data.C["SPY"]
        return _daily(spy.rolling(50).mean() > spy.rolling(200).mean(), {"SPY": 1.0}, {}, data)
    return f


def s_vol_target(target: float = 0.15):
    def f(data):
        rv = data.C["SPY"].pct_change().rolling(21, min_periods=21).std() * math.sqrt(TRADING_DAYS)
        W = pd.DataFrame(0.0, index=data.days, columns=data.C.columns)
        W["SPY"] = (target / rv).clip(upper=1.0).fillna(0.0)
        return W
    return f


def s_turn_of_month():
    def f(data):
        days = data.days
        s = pd.Series(range(len(days)), index=days)
        grp = s.groupby([days.year, days.month])
        pos_from_start = grp.cumcount()
        pos_from_end = grp.cumcount(ascending=False)
        # decided at the close of day d for the next session: hold through the last day and the first 3 days
        nxt_start = pos_from_start.shift(-1)
        nxt_end = pos_from_end.shift(-1)
        on = (nxt_end == 0) | (nxt_start <= 2)
        return _daily(on, {"SPY": 1.0}, {}, data)
    return f


def _rotation(assets: list[str], months: int, k: int, safe: str | None, abs_filter: bool, data: Data,
              gate: pd.Series | None = None) -> pd.DataFrame:
    m = data.C[assets].shift(1) / data.C[assets].shift(1 + 21 * months) - 1
    bil = data.C["BIL"].shift(1) / data.C["BIL"].shift(1 + 21 * months) - 1
    sparse = {}
    for d in month_ends(data.days):
        row = m.loc[d].dropna()
        if len(row) < k or (gate is not None and not bool(gate.get(d, False))):
            sparse[d] = {safe: 1.0} if safe else {}
            continue
        best = row.nlargest(k)
        if abs_filter:
            best = best[best > bil.loc[d]]
        w = {s: 1.0 / k for s in best.index}
        if safe and len(w) < k:
            w[safe] = w.get(safe, 0.0) + (k - len(w)) / k
        sparse[d] = w
    return _hold(sparse, data)


def s_dual_momentum():
    return lambda data: _rotation(["SPY", "EFA"], 12, 1, "AGG", True, data)


def s_asset_rotation():
    return lambda data: _rotation(["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD"], 6, 1, "AGG", True, data)


def s_sector_rotation(months: int, trend: bool = False):
    def f(data):
        return _rotation(SECTORS, months, 3, None, False, data, gate=_spy_above(data, 200) if trend else None)
    return f


def s_fixed(weights: dict[str, float]):
    return lambda data: _hold({d: weights for d in month_ends(data.days)}, data)


def s_risk_parity():
    def f(data):
        v = vol(data.C[["SPY", "TLT", "GLD"]], 63)
        sparse = {}
        for d in month_ends(data.days):
            iv = (1 / v.loc[d]).dropna()
            sparse[d] = (iv / iv.sum()).to_dict() if len(iv) == 3 else {}
        return _hold(sparse, data)
    return f


# name → (builder, benchmark, plain-English description)
STRATEGIES: dict[str, tuple[Callable[[Data], pd.DataFrame], str, str]] = {
    **{f"momentum_{m}m_top{n}": (s_momentum(m, n), "pool",
                                 f"Monthly: buy the {n} stocks that rose most over {m} months (skipping the last month)")
       for m in (3, 6, 12) for n in (10, 20)},
    **{f"vol_adj_momentum_top{n}": (s_vol_adj_mom(n), "pool",
                                    f"Monthly: {n} stocks with the best 12-month rise per unit of volatility")
       for n in (10, 20)},
    **{f"low_vol_top{n}": (s_low_vol(n), "pool", f"Monthly: the {n} calmest stocks (lowest 3-month volatility)")
       for n in (20, 30)},
    "low_beta_top20": (s_low_beta(20), "pool", "Monthly: the 20 stocks that move least with the market"),
    **{f"near_52w_high_top{n}": (s_high52(n), "pool", f"Monthly: the {n} stocks closest to their 1-year high")
       for n in (10, 20)},
    "momentum_12m_top20_trend": (s_mom_trend(20), "pool",
                                 "Monthly: 12-month momentum top 20, but cash when SPY is below its 200-day average"),
    "low_vol_top20_trend": (s_lowvol_trend(20), "pool",
                            "Monthly: 20 calmest stocks, but cash when SPY is below its 200-day average"),
    **{f"weekly_losers_top{n}": (s_reversal_week(n), "pool", f"Weekly: buy the {n} biggest 5-day losers")
       for n in (10, 20)},
    "weekly_losers_uptrend_top10": (s_reversal_week(10, True), "pool",
                                    "Weekly: the 10 biggest 5-day losers among stocks above their 200-day average"),
    "monthly_losers_top20": (s_reversal_month(20), "pool", "Monthly: buy the 20 biggest 1-month losers"),
    "momentum_plus_low_vol_top20": (s_mom_lowvol_combo(20), "pool",
                                    "Monthly: 20 stocks ranking best on 12-month momentum and calmness combined"),
    "rsi2_below10": (s_rsi2(10), "pool", "Daily: buy short sharp dips (RSI2 < 10) in uptrending stocks, sell on rebound"),
    "rsi2_below5": (s_rsi2(5), "pool", "Daily: buy very sharp dips (RSI2 < 5) in uptrending stocks, sell on rebound"),
    "spy_above_100d": (s_spy_sma(100), "SPY", "Daily: hold SPY above its 100-day average, else cash"),
    "spy_above_200d": (s_spy_sma(200), "SPY", "Daily: hold SPY above its 200-day average, else cash"),
    "spy_50_200_cross": (s_spy_cross(), "SPY", "Daily: hold SPY while its 50-day average is above its 200-day"),
    "spy_vol_target_15": (s_vol_target(0.15), "SPY", "Daily: SPY sized to 15% yearly volatility (less in storms)"),
    "spy_turn_of_month": (s_turn_of_month(), "SPY", "Hold SPY only on the last day and first 3 days of each month"),
    "dual_momentum_gem": (s_dual_momentum(), "SPY", "Monthly: US or international stocks, whichever is stronger; bonds if neither beats cash"),
    "asset_rotation_top1": (s_asset_rotation(), "SPY", "Monthly: the strongest of 7 asset ETFs over 6 months; bonds if none beats cash"),
    "sector_rotation_3m": (s_sector_rotation(3), "SPY", "Monthly: the 3 strongest sector ETFs over 3 months"),
    "sector_rotation_6m": (s_sector_rotation(6), "SPY", "Monthly: the 3 strongest sector ETFs over 6 months"),
    "sector_rotation_6m_trend": (s_sector_rotation(6, True), "SPY",
                                 "Monthly: 3 strongest sectors over 6 months, cash when SPY is below its 200-day"),
    "sixty_forty": (s_fixed({"SPY": 0.6, "AGG": 0.4}), "SPY", "Monthly: 60% SPY, 40% bonds"),
    "risk_parity_spy_tlt_gld": (s_risk_parity(), "SPY", "Monthly: SPY, long bonds and gold weighted by inverse volatility"),
}


def pool_weights(data: Data) -> pd.DataFrame:
    sparse = {}
    for d in month_ends(data.days):
        u = data.U.loc[d, data.stocks]
        names = list(u[u].index)
        sparse[d] = {s: 1.0 / len(names) for s in names} if names else {}
    return _hold(sparse, data)


@dataclass
class Result:
    name: str
    benchmark: str
    description: str
    stats: dict  # period → {ann, sharpe, max_dd, excess_ann, bench_ann}
    dsr: float
    passed: bool
    reasons: list[str]


def run(bars: dict[str, pd.DataFrame], spread_fn: Callable[[str, int], float],
        progress: Callable[[str], None] = print) -> dict:
    data = build(bars)
    bench = {"SPY": simulate(_daily(pd.Series(True, index=data.days), {"SPY": 1.0}, {}, data), data, spread_fn),
             "pool": simulate(pool_weights(data), data, spread_fn)}
    rets: dict[str, pd.Series] = {}
    for name, (fn, _b, _d) in STRATEGIES.items():
        rets[name] = simulate(fn(data), data, spread_fn)
        progress(f"  {name}")
    excess = {n: rets[n] - bench[STRATEGIES[n][1]] for n in rets}
    def _sr(x: pd.Series) -> float:
        sd = x.std()
        return float(x.mean() / sd) if sd and sd > 0 and math.isfinite(sd) else 0.0

    trial_srs = [_sr(seg(x, "discovery")) for x in excess.values()]
    results = []
    for name, r in rets.items():
        fn, b, desc = STRATEGIES[name]
        st = {}
        for p in PERIODS:
            rp, bp = seg(r, p), seg(bench[b], p)
            st[p] = {"ann": ann_return(rp), "sharpe": sharpe(rp), "max_dd": max_dd(rp),
                     "bench_ann": ann_return(bp), "bench_sharpe": sharpe(bp),
                     "excess_ann": ann_return(rp) - ann_return(bp)}
        dsr = deflated_sharpe(seg(excess[name], "discovery"), trial_srs)
        reasons = []
        if dsr < 0.95:
            reasons.append(f"deflated Sharpe {dsr:.2f} < 0.95 (not clearly better than luck after {len(rets)} tries)")
        if st["discovery"]["sharpe"] < 0.5:
            reasons.append(f"discovery Sharpe {st['discovery']['sharpe']:.2f} < 0.5")
        if not st["validation"]["excess_ann"] > 0:
            reasons.append(f"lost to its benchmark in 2024-2025 ({st['validation']['excess_ann']:+.1%}/yr)")
        results.append(Result(name, b, desc, st, dsr, not reasons, reasons))
    bstats = {k: {p: {"ann": ann_return(seg(v, p)), "sharpe": sharpe(seg(v, p)), "max_dd": max_dd(seg(v, p))}
                  for p in PERIODS} for k, v in bench.items()}
    return {"results": results, "benchmarks": bstats, "trials": len(rets)}


def format_zoo(out: dict) -> str:
    res = sorted(out["results"], key=lambda r: r.stats["discovery"]["excess_ann"], reverse=True)
    b = out["benchmarks"]
    L = [f"{out['trials']} strategies tested. Benchmarks (yearly return / Sharpe):",
         *[f"  {k:<5} 2017-23 {v['discovery']['ann']:+.1%}/{v['discovery']['sharpe']:.2f} · "
           f"2024-25 {v['validation']['ann']:+.1%}/{v['validation']['sharpe']:.2f} · "
           f"2026 {v['fresh']['ann']:+.1%}/{v['fresh']['sharpe']:.2f}" for k, v in b.items()],
         "", "Ranked by 2017-2023 return over benchmark (what 'pick the best backtest' would choose):",
         f"  {'strategy':<30} {'vs':<4} {'2017-23 ex':>10} {'DSR':>5} {'2024-25 ex':>10} {'2026 ex':>8}  result"]
    for r in res:
        s = r.stats
        L.append(f"  {r.name:<30} {r.benchmark:<4} {s['discovery']['excess_ann']:>+9.1%} {r.dsr:>5.2f} "
                 f"{s['validation']['excess_ann']:>+9.1%} {s['fresh']['excess_ann']:>+7.1%}  "
                 f"{'PASS' if r.passed else 'fail'}")
    passed = [r for r in res if r.passed]
    L += ["", f"Passed all three tests: {len(passed)}"]
    for r in passed:
        s = r.stats
        L.append(f"  {r.name}: {r.description}. 2017-23 {s['discovery']['ann']:+.1%}/yr (Sharpe "
                 f"{s['discovery']['sharpe']:.2f}, worst drop {s['discovery']['max_dd']:.0%}); 2024-25 "
                 f"{s['validation']['ann']:+.1%}/yr; 2026 so far {s['fresh']['ann']:+.1%} annualized")
    return "\n".join(L)
