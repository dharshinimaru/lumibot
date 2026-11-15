"""Machine-learning stock pickers, trained walk-forward on real prices and headlines (pre-registered).

Everything below is fixed BEFORE any real-data result is seen; there is no tuning loop.

* Universe: each day's top-100 stocks by trailing dollar volume (``zoo.Data.U``), the same stocks the
  "same stocks" benchmark holds. Pick 10, equal weight, hold until the next rebalance.
* Features at day t's close (only data known by then): ~20 price/volume features, ranked within the
  day's universe, plus a few market-regime features; news variants add counts of parsed headline
  events (earnings beats/misses, upgrades/downgrades, price-target moves, guidance) over 5 and 21
  sessions, headline volume, and the latest earnings surprise. A headline counts for day t only if
  it was published before 16:00 New York time that day (later ones count for the next session).
* Label: the stock's return from the next open over ``h`` sessions, ranked within the universe.
* Model: scikit-learn HistGradientBoostingRegressor with fixed, conservative settings.
* Walk-forward: for each test year Y (2018 on), train on every 5th day before Y whose label was
  already known before Y started (an ``h + 1`` session embargo), then predict all of Y. Nothing in
  year Y ever informs the model that trades year Y.
* Grading: the zoo engine (next-open fills, real spreads, 0.05% slippage, idle cash in BIL) vs the
  same-stocks benchmark, and the zoo pass rule: deflated Sharpe >= 0.95 counting all 34 zoo
  strategies plus these 4 variants as tries, discovery (2018-2023) Sharpe >= 0.5, and positive
  excess return in 2024-2025. 2026 is reported only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from lumisafe.signals import zoo

NY = ZoneInfo("America/New_York")
TOP = 10
WARMUP = 260  # sessions of history before the first usable feature row
FIRST_TEST_YEAR = 2018
SAMPLE_EVERY = 5
PERIODS = {"discovery": ("2018-01-01", "2023-12-31"), "validation": ("2024-01-01", "2025-12-31"),
           "fresh": ("2026-01-01", "2099-12-31")}
MODEL_PARAMS = dict(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=300,
                    l2_regularization=1.0, random_state=0)
NEWS_KINDS = ["earnings_beat", "earnings_miss", "sales_beat", "sales_miss", "upgrade", "downgrade",
              "initiate_bullish", "pt_raise", "pt_cut", "guide_up", "guide_down"]


@dataclass(frozen=True)
class Variant:
    name: str
    horizon: int  # sessions
    rebalance: str  # weekly / monthly
    news: bool
    description: str


VARIANTS = {v.name: v for v in [
    Variant("ml_weekly_price", 5, "weekly", False, "ML on ~20 price/volume features; top 10, rebalanced weekly"),
    Variant("ml_weekly_news", 5, "weekly", True, "ML on price/volume + headline events; top 10, rebalanced weekly"),
    Variant("ml_monthly_price", 21, "monthly", False, "ML on ~20 price/volume features; top 10, rebalanced monthly"),
    Variant("ml_monthly_news", 21, "monthly", True, "ML on price/volume + headline events; top 10, rebalanced monthly"),
]}


# --- features ----------------------------------------------------------------------------------------------------


def price_features(data: zoo.Data) -> dict[str, pd.DataFrame]:
    C, O, V = data.C[data.stocks], data.O[data.stocks], data.V[data.stocks]
    lr = np.log(C / C.shift(1))
    spy = np.log(data.C["SPY"] / data.C["SPY"].shift(1))
    var_spy = spy.rolling(63).var()
    beta = lr.rolling(63).cov(spy).div(var_spy, axis=0)
    r21 = C / C.shift(21) - 1
    spy21 = data.C["SPY"] / data.C["SPY"].shift(21) - 1
    f = {
        "r1": C / C.shift(1) - 1, "r5": C / C.shift(5) - 1, "r21": r21, "r63": C / C.shift(63) - 1,
        "r126": C / C.shift(126) - 1, "r252": C / C.shift(252) - 1, "mom12_1": C.shift(21) / C.shift(252) - 1,
        "vol21": lr.rolling(21).std(), "vol63": lr.rolling(63).std(),
        "sma50": C / C.rolling(50).mean() - 1, "sma200": C / C.rolling(200).mean() - 1,
        "hi252": C / C.rolling(252).max() - 1, "rsi14": zoo.rsi(C, 14), "rsi2": zoo.rsi(C, 2),
        "vol_ratio": np.log((V / V.rolling(20).mean()).replace(0, np.nan)),
        "dollar_vol": np.log((C * V).rolling(20).mean().replace(0, np.nan)),
        "beta63": beta, "idio21": r21 - beta.mul(spy21, axis=0),
        "gap1": O / C.shift(1) - 1, "intraday1": C / O - 1, "max21": lr.rolling(21).max(),
    }
    U = data.U[data.stocks]
    return {k: v.where(U).rank(axis=1, pct=True) for k, v in f.items()}


def market_features(data: zoo.Data) -> dict[str, pd.Series]:
    s = data.C["SPY"]
    lr = np.log(s / s.shift(1))
    U = data.U[data.stocks]
    r21 = (data.C[data.stocks] / data.C[data.stocks].shift(21) - 1).where(U)
    return {"spy_r21": s / s.shift(21) - 1, "spy_r63": s / s.shift(63) - 1, "spy_vol21": lr.rolling(21).std(),
            "spy_sma200": s / s.rolling(200).mean() - 1, "dispersion21": r21.std(axis=1)}


def news_day_index(at: datetime, days: pd.DatetimeIndex) -> int:
    """Index of the first session whose 16:00 close comes after ``at`` (decisions are made after the close)."""
    t = at.astimezone(NY)
    d = t.date() if t.hour < 16 else t.date() + timedelta(days=1)
    return int(days.searchsorted(pd.Timestamp(d)))


def news_features(items: list[dict], data: zoo.Data) -> dict[str, pd.DataFrame]:
    from lumisafe.signals.events import parse_all

    days, stocks = data.days, data.stocks
    col = {s: i for i, s in enumerate(stocks)}
    n = len(days)
    heads = np.zeros((n, len(stocks)))
    kinds = {k: np.zeros((n, len(stocks))) for k in NEWS_KINDS}
    surprise = np.full((n, len(stocks)), np.nan)
    for it in items:
        syms = it.get("symbols") or []
        if len(syms) == 1 and syms[0] in col:
            i = news_day_index(datetime.fromisoformat(it["created_at"]), days)
            if i < n:
                heads[i, col[syms[0]]] += 1
    for e in parse_all(items):
        if e.symbol not in col or e.kind not in kinds:
            continue
        i = news_day_index(e.at, days)
        if i >= n:
            continue
        kinds[e.kind][i, col[e.symbol]] += 1
        if e.kind.startswith("earnings_") and isinstance(e.value, float):
            surprise[i, col[e.symbol]] = max(-1.0, min(1.0, e.value))
    out: dict[str, pd.DataFrame] = {}
    H = pd.DataFrame(heads, index=days, columns=stocks)
    out["heads5"] = np.log1p(H.rolling(5, min_periods=1).sum())
    out["heads21"] = np.log1p(H.rolling(21, min_periods=1).sum())
    out["heads_abnormal"] = H.rolling(5, min_periods=1).sum() / (H.rolling(63, min_periods=1).sum() / 12.6 + 1)
    for k, a in kinds.items():
        K = pd.DataFrame(a, index=days, columns=stocks)
        out[f"{k}_5"] = K.rolling(5, min_periods=1).sum()
        out[f"{k}_21"] = K.rolling(21, min_periods=1).sum()
    out["last_surprise"] = pd.DataFrame(surprise, index=days, columns=stocks).ffill(limit=63)
    return out


def labels(data: zoo.Data, h: int) -> pd.DataFrame:
    """Return from the next open over ``h`` sessions, ranked within the decision day's universe."""
    O = data.O[data.stocks]
    fwd = O.shift(-(h + 1)) / O.shift(-1) - 1
    return fwd.where(data.U[data.stocks]).rank(axis=1, pct=True)


def feature_cube(data: zoo.Data, news_items: list[dict] | None) -> tuple[np.ndarray, list[str]]:
    """X[day, stock, feature] built only from information available at each day's close."""
    f = price_features(data)
    if news_items is not None:
        f.update(news_features(news_items, data))
    names = list(f)
    m = market_features(data)
    n, k = len(data.days), len(data.stocks)
    X = np.empty((n, k, len(names) + len(m)), dtype=np.float32)
    for j, name in enumerate(names):
        X[:, :, j] = f[name].reindex(index=data.days, columns=data.stocks).to_numpy(dtype=np.float32)
    for j, (name, s) in enumerate(m.items()):
        X[:, :, len(names) + j] = s.reindex(data.days).to_numpy(dtype=np.float32)[:, None]
    return X, names + list(m)


# --- walk-forward ----------------------------------------------------------------------------------------------------


def train_rows(n_days: int, test_start: int, h: int) -> np.ndarray:
    """Day indices usable to train a model that first trades on day ``test_start``.

    A row's label ends at the open ``h + 1`` sessions later, so it must end before the test starts."""
    last = test_start - h - 1
    return np.array([i for i in range(WARMUP, max(WARMUP, last + 1)) if i % SAMPLE_EVERY == 0], dtype=int)


def fit(X: np.ndarray, Y: np.ndarray, U: np.ndarray, rows: np.ndarray):
    from sklearn.ensemble import HistGradientBoostingRegressor

    Xs, ys = [], []
    for i in rows:
        ok = U[i] & ~np.isnan(Y[i])
        if ok.any():
            Xs.append(X[i][ok])
            ys.append(Y[i][ok])
    if not Xs:
        return None
    model = HistGradientBoostingRegressor(**MODEL_PARAMS)
    model.fit(np.concatenate(Xs), np.concatenate(ys))
    return model


def rebalance_days(data: zoo.Data, rebalance: str) -> pd.DatetimeIndex:
    return zoo.week_ends(data.days) if rebalance == "weekly" else zoo.month_ends(data.days)


def picks_on(model, X: np.ndarray, U: np.ndarray, i: int, stocks: list[str], top: int = TOP) -> list[str]:
    ok = U[i]
    if model is None or ok.sum() < top:
        return []
    pred = model.predict(X[i][ok])
    names = np.array(stocks)[ok]
    order = np.argsort(-pred, kind="stable")[:top]
    return sorted(names[order].tolist())


def walk_forward(data: zoo.Data, v: Variant, news_items: list[dict] | None,
                 progress: Callable[[str], None] = print) -> pd.DataFrame:
    """Target weights for every day, from models retrained once a year on strictly earlier data."""
    X, _ = feature_cube(data, news_items if v.news else None)
    Y = labels(data, v.horizon).reindex(index=data.days, columns=data.stocks).to_numpy(dtype=np.float32)
    U = data.U[data.stocks].reindex(index=data.days).to_numpy(dtype=bool)
    days = data.days
    when = set(rebalance_days(data, v.rebalance))
    sparse: dict[pd.Timestamp, dict[str, float]] = {}
    for year in sorted(set(days.year)):
        if year < FIRST_TEST_YEAR:
            continue
        idx = np.where(days.year == year)[0]
        model = fit(X, Y, U, train_rows(len(days), int(idx[0]), v.horizon))
        progress(f"  {v.name}: trained for {year}")
        for i in idx:
            if days[i] in when:
                p = picks_on(model, X, U, int(i), data.stocks)
                sparse[days[i]] = {s: 1.0 / len(p) for s in p} if p else {}
    return zoo._hold(sparse, data)


# --- grading -----------------------------------------------------------------------------------------------------------


def seg(r: pd.Series, period: str) -> pd.Series:
    a, b = PERIODS[period]
    return r.loc[a:b]


@dataclass
class MLResult:
    name: str
    description: str
    stats: dict
    yearly: dict  # year → {ann, bench_ann}
    dsr: float
    passed: bool
    reasons: list[str]
    latest_picks: list[str]


def _sr(x: pd.Series) -> float:
    sd = x.std()
    return float(x.mean() / sd) if sd and sd > 0 and math.isfinite(sd) else 0.0


def run(bars: dict[str, pd.DataFrame], news_items: list[dict], spread_fn: Callable[[str, int], float],
        progress: Callable[[str], None] = print, variants: list[str] | None = None) -> dict:
    data = zoo.build(bars)
    pool = zoo.simulate(zoo.pool_weights(data), data, spread_fn)
    names = variants or list(VARIANTS)
    rets, weights = {}, {}
    for n in names:
        weights[n] = walk_forward(data, VARIANTS[n], news_items, progress)
        rets[n] = zoo.simulate(weights[n], data, spread_fn)
    trial_srs = [_sr(seg(r - pool, "discovery")) for r in rets.values()]
    for name, (fn, b, _d) in zoo.STRATEGIES.items():  # every earlier try counts against luck
        bench = pool if b == "pool" else zoo.simulate(zoo._daily(pd.Series(True, index=data.days), {"SPY": 1.0},
                                                                 {}, data), data, spread_fn)
        trial_srs.append(_sr(seg(zoo.simulate(fn(data), data, spread_fn) - bench, "discovery")))
    results = []
    for n in names:
        r = rets[n]
        st = {}
        for p in PERIODS:
            rp, bp = seg(r, p), seg(pool, p)
            st[p] = {"ann": zoo.ann_return(rp), "sharpe": zoo.sharpe(rp), "max_dd": zoo.max_dd(rp),
                     "bench_ann": zoo.ann_return(bp), "excess_ann": zoo.ann_return(rp) - zoo.ann_return(bp),
                     "days": len(rp)}
        yearly = {int(y): {"ann": float((1 + r[r.index.year == y]).prod() - 1),
                           "bench_ann": float((1 + pool[pool.index.year == y]).prod() - 1)}
                  for y in sorted(set(r.index.year)) if y >= FIRST_TEST_YEAR}
        dsr = zoo.deflated_sharpe(seg(r - pool, "discovery"), trial_srs)
        reasons = []
        if dsr < 0.95:
            reasons.append(f"deflated Sharpe {dsr:.2f} < 0.95 (not clearly better than luck after "
                           f"{len(trial_srs)} tries)")
        if st["discovery"]["sharpe"] < 0.5:
            reasons.append(f"discovery Sharpe {st['discovery']['sharpe']:.2f} < 0.5")
        if not st["validation"]["excess_ann"] > 0:
            reasons.append(f"lost to the same stocks in 2024-2025 ({st['validation']['excess_ann']:+.1%}/yr)")
        last = weights[n].iloc[-1]
        results.append(MLResult(n, VARIANTS[n].description, st, yearly, dsr, not reasons, reasons,
                                sorted(s for s, x in last.items() if x > 0)))
    bstats = {p: {"ann": zoo.ann_return(seg(pool, p)), "sharpe": zoo.sharpe(seg(pool, p))} for p in PERIODS}
    return {"results": results, "benchmark": bstats, "trials": len(trial_srs)}


def format_ml(out: dict) -> str:
    b = out["benchmark"]
    L = [f"Same-stocks benchmark: 2018-23 {b['discovery']['ann']:+.1%}/yr · 2024-25 {b['validation']['ann']:+.1%}/yr"
         f" · 2026 {b['fresh']['ann']:+.1%} annualized", "",
         f"  {'model':<18} {'2018-23':>8} {'vs bench':>9} {'DSR':>5} {'2024-25':>8} {'vs bench':>9} "
         f"{'2026':>7} {'vs bench':>9}  result"]
    for r in out["results"]:
        s = r.stats
        L.append(f"  {r.name:<18} {s['discovery']['ann']:>+7.1%} {s['discovery']['excess_ann']:>+8.1%} {r.dsr:>5.2f} "
                 f"{s['validation']['ann']:>+7.1%} {s['validation']['excess_ann']:>+8.1%} "
                 f"{s['fresh']['ann']:>+6.1%} {s['fresh']['excess_ann']:>+8.1%}  {'PASS' if r.passed else 'fail'}")
    L += ["", "Year by year (model / same stocks):"]
    years = sorted({y for r in out["results"] for y in r.yearly})
    L.append(f"  {'model':<18} " + " ".join(f"{y:>13}" for y in years))
    for r in out["results"]:
        L.append(f"  {r.name:<18} " + " ".join(f"{r.yearly[y]['ann']:>+6.0%}/{r.yearly[y]['bench_ann']:>+5.0%}"
                                               if y in r.yearly else f"{'':>13}" for y in years))
    for r in out["results"]:
        if r.reasons:
            L.append(f"  {r.name} failed: " + "; ".join(r.reasons))
    L += ["", f"Passed: {sum(r.passed for r in out['results'])} of {len(out['results'])} "
              f"(luck correction counts {out['trials']} tries)"]
    return "\n".join(L)


def load_news(cache, start: date, end: date) -> list[dict]:
    import json

    items = []
    for f in sorted((cache / "news_full").glob("*.json")):
        d = date.fromisoformat(f.stem)
        if start <= d <= end:
            items += json.loads(f.read_text())["items"]
    return items
