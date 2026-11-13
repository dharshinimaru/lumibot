"""Download every headline for the stock pool, one New York calendar day at a time (cached, resumable)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from lumisafe.daytrade.universe import POOL

NY = ZoneInfo("America/New_York")
FOLDER = "news_full"


def fetch_all_news(md, start: date, end: date, progress: Callable[[str], None] = print) -> int:
    d, n = start, 0
    while d <= end:
        lo = datetime.combine(d, time(0, 0), tzinfo=NY)
        md.news(list(POOL), d, lo, lo + timedelta(days=1) - timedelta(seconds=1), folder_name=FOLDER)
        n += 1
        if n % 30 == 0:
            progress(f"news through {d}")
        d += timedelta(days=1)
    return n


if __name__ == "__main__":  # pragma: no cover - long download
    import sys

    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.adapters.alpaca_data import AlpacaMarketData

    bridge.load_dotenv_file(".env")
    md = AlpacaMarketData()
    a, b = (date.fromisoformat(x) for x in sys.argv[1:3])
    md.daily_bars(list(POOL) + ["SPY"], a - timedelta(days=400), b, adjustment="all")
    md.daily_bars(list(POOL) + ["SPY"], a - timedelta(days=400), b, adjustment="split")
    fetch_all_news(md, a, b, progress=lambda m: print(m, flush=True))
