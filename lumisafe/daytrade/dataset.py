"""Build the day-trading lab's dataset from real Alpaca data (cached; safe to re-run).

For every NYSE session it records the candidates the lab could have seen at
the 09:45 decision time, using only information public by then:

* opened at least ``min_gap`` above the previous close (today's official open
  is known at 09:30),
* among the day's ``top_n`` stocks by trailing dollar volume,
* at least one headline published between the previous close (16:00 NY) and
  ``decision time - news_delay`` (a feed/reading delay; later news is ignored).

For each candidate it downloads that day's regular-session minute bars and
the real quoted spread at the decision time. Nothing is synthesized: a
candidate whose bars or quotes are missing is kept with that field empty and
the simulator skips it (counted in the report).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from lumisafe import paths
from lumisafe.daytrade.universe import POOL, Session, dollar_volume_rank, sessions

OPENING_RANGE_MINUTES = 15
NEWS_DELAY_SECONDS = 60
DATASET_MIN_GAP = 0.02  # the lowest gap any parameter set may use
MIN_PRICE = 5.0


@dataclass
class Candidate:
    day: str
    symbol: str
    open_ts: str  # session open (ISO, NY)
    close_ts: str  # session close (ISO, NY); 13:00 on half days
    prev_close: float
    open: float
    gap: float
    avg_volume_20d: float
    headlines: list[dict] = field(default_factory=list)
    spread: float | None = None  # median quoted spread / mid at the decision time; None = no quotes
    has_bars: bool = False


def decision_time(s: Session) -> datetime:
    return s.open + timedelta(minutes=OPENING_RANGE_MINUTES)


def dataset_path(start: date, end: date) -> Path:
    return paths.cache_dir() / f"daytrade_candidates_{start}_{end}.json"


def load_candidates(start: date, end: date) -> list[Candidate]:
    f = dataset_path(start, end)
    return [Candidate(**c) for c in json.loads(f.read_text())["candidates"]] if f.is_file() else []


def build(md, start: date, end: date, top_n: int = 100, progress: Callable[[str], None] = print) -> list[Candidate]:
    """Download (or reuse cached) data and return the candidate list for [start, end]."""
    daily = md.daily_bars(list(POOL), start - timedelta(days=60), end)
    daily = {s: d for s, d in daily.items() if len(d)}
    out_file = dataset_path(start, end)
    done: dict[str, list[dict]] = {}
    if out_file.is_file():
        saved = json.loads(out_file.read_text())
        done = saved.get("by_day", {})
    sess = sessions(start, end)
    prev_close_time: datetime | None = None
    for n, s in enumerate(sess):
        key = s.day.isoformat()
        if key in done:
            prev_close_time = s.close
            continue
        day_ts = pd.Timestamp(s.day)
        universe = dollar_volume_rank(daily, s.day, top_n=top_n)
        gappers = []
        for sym in universe:
            d = daily[sym]
            today = d[d.index == day_ts]
            prior = d[d.index < day_ts]
            if today.empty or prior.empty:
                continue
            pc, op = float(prior["close"].iloc[-1]), float(today["open"].iloc[0])
            if pc > 0 and op >= MIN_PRICE and op / pc - 1 >= DATASET_MIN_GAP:
                gappers.append((sym, pc, op, float(prior["volume"].tail(20).mean())))
        cands: list[Candidate] = []
        if gappers:
            since = prev_close_time or (s.open - timedelta(hours=17, minutes=30))
            until = decision_time(s) - timedelta(seconds=NEWS_DELAY_SECONDS)
            items = md.news([g[0] for g in gappers], s.day, since, until)
            for sym, pc, op, avgv in gappers:
                hl = [{"at": i["created_at"], "headline": i["headline"], "source": i["source"],
                       "n_symbols": len(i["symbols"])}
                      for i in items if sym in i["symbols"]]
                if hl:
                    cands.append(Candidate(key, sym, s.open.isoformat(), s.close.isoformat(), pc, op, op / pc - 1,
                                           avgv, sorted(hl, key=lambda h: h["at"])))
        if cands:
            bars = md.minute_bars([c.symbol for c in cands], s.day, s.open, s.close)
            for c in cands:
                c.has_bars = c.symbol in bars and len(bars[c.symbol]) > OPENING_RANGE_MINUTES
                if c.has_bars:
                    c.spread = md.spread(c.symbol, decision_time(s))
        done[key] = [asdict(c) for c in cands]
        prev_close_time = s.close
        if n % 20 == 0 or n == len(sess) - 1:
            md.save_spreads()
            _save(out_file, done)
            progress(f"{s.day}: {n + 1}/{len(sess)} sessions, {sum(len(v) for v in done.values())} candidates")
    md.save_spreads()
    _save(out_file, done)
    return [Candidate(**c) for day in sorted(done) for c in done[day]]


def _save(f: Path, by_day: dict) -> None:
    flat = [c for day in sorted(by_day) for c in by_day[day]]
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"by_day": by_day, "candidates": flat}))
    tmp.replace(f)


if __name__ == "__main__":  # pragma: no cover - long-running download, run from the CLI
    import sys

    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.adapters.alpaca_data import AlpacaMarketData

    bridge.load_dotenv_file(".env")
    a, b = (date.fromisoformat(x) for x in sys.argv[1:3])
    build(AlpacaMarketData(), a, b)
