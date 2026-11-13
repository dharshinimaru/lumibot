"""Day-trading lab entry points: download the data, run the honest test, record the verdict."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from lumisafe import paths
from lumisafe.config.schema import DayTradeConfig
from lumisafe.daytrade import dataset
from lumisafe.daytrade.report import STRATEGY_NAME, LabReport, walk_forward
from lumisafe.daytrade.universe import sessions

DEFAULT_START = date(2016, 1, 4)
DEFAULT_END = date(2025, 12, 31)


def by_day(cands: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in cands:
        out.setdefault(c.day, []).append(c)
    return out


def run_backtest(md, cfg: DayTradeConfig, registry, *, start: date = DEFAULT_START, end: date = DEFAULT_END,
                 account_equity: float = 100_000.0, now: datetime | None = None,
                 progress: Callable[[str], None] = print) -> tuple[LabReport, Path, str]:
    from lumisafe.promotion import apply_backtest

    now = now or datetime.now(timezone.utc)
    cands = dataset.load_candidates(start, end)
    if not cands:
        raise RuntimeError("No day-trading data yet. Run `lumisafe daytrade fetch` first (about 2-3 hours).")
    days = [s.day for s in sessions(start, end)]
    spy_total = md.daily_bars(["SPY"], start, end, adjustment="all")["SPY"]["close"]
    spy_price = md.daily_bars(["SPY"], start, end, adjustment="split")["SPY"]["close"]

    cands_by_day = by_day(cands)
    symbols = sorted({c.symbol for c in cands})
    split_daily = md.daily_bars(symbols, start - timedelta(days=60), end, adjustment="split")
    raw_daily = md.daily_bars(symbols, start - timedelta(days=60), end, adjustment="raw")

    def load_bars(d: date, syms: list[str]) -> dict[str, pd.DataFrame]:
        cs = cands_by_day[d.isoformat()]
        bars = md.minute_bars(syms, d, pd.Timestamp(cs[0].open_ts).to_pydatetime(),
                              pd.Timestamp(cs[0].close_ts).to_pydatetime())
        return {s: to_actual_prices(b, s, d, split_daily, raw_daily) for s, b in bars.items()}

    report = walk_forward(cands_by_day, load_bars, spy_total, spy_price, days, cfg,
                          account_equity=account_equity, progress=progress)
    out = paths.reports_dir() / f"daytrade_{STRATEGY_NAME}_{now:%Y%m%d_%H%M%S}.json"
    report.save(out)
    state = apply_backtest(STRATEGY_NAME, report.walk_forward, out, registry, now)
    return report, out, state.value


def split_factor(symbol: str, d: date, split_daily: dict, raw_daily: dict) -> float | None:
    """Actual price / split-adjusted price on ``d`` (1.0 unless the stock split later)."""
    a, r = split_daily.get(symbol), raw_daily.get(symbol)
    ts = pd.Timestamp(d)
    if a is None or r is None or ts not in a.index or ts not in r.index or float(a.loc[ts, "close"]) <= 0:
        return None
    return float(r.loc[ts, "close"]) / float(a.loc[ts, "close"])


def to_actual_prices(bars: pd.DataFrame, symbol: str, d: date, split_daily: dict, raw_daily: dict) -> pd.DataFrame:
    """Minute bars as they actually traded that day (cached bars are split-adjusted).

    Day trades never span a split, so the actual price is what matters for whole
    shares, per-share fees and volume limits. If the factor is unknown the bars
    are returned unchanged (no guessing).
    """
    f = split_factor(symbol, d, split_daily, raw_daily)
    if f is None or abs(f - 1.0) < 1e-6:
        return bars
    out = bars.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * f
    out["volume"] = out["volume"] / f
    return out


def latest_report() -> Path | None:
    files = sorted(paths.reports_dir().glob(f"daytrade_{STRATEGY_NAME}_*.json"))
    return files[-1] if files else None
