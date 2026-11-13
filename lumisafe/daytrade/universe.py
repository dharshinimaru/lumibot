"""Which stocks the day-trading lab may trade on a given day, and the market calendar.

The pool is broad on purpose: large, heavily traded US stocks from 2016 to
2025, including ones that later fell, merged or were delisted (Twitter,
Celgene, Activision, Xilinx, Walgreens, Paramount...). Each day the lab
keeps the ``top_n`` by average dollar volume over the previous 20 sessions,
using only data that existed that morning.

Residual bias (stated in every report): the pool was written in 2026, so it
leans toward names that were already well known. Picking by trailing dollar
volume limits but does not remove that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

POOL: tuple[str, ...] = (
    # tech and communication
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA", "NFLX", "AMD", "INTC", "CSCO", "ORCL", "IBM",
    "QCOM", "TXN", "AVGO", "MU", "ADBE", "CRM", "PYPL", "NOW", "INTU", "AMAT", "LRCX", "KLAC", "ADI", "MRVL",
    "XLNX", "SHOP", "UBER", "ABNB", "SNOW", "PLTR", "COIN", "ROKU", "ZM", "DOCU", "TWTR", "SNAP", "PINS",
    "SPOT", "EA", "ATVI", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "PARA", "WBD", "BABA", "JD", "PDD", "NIO",
    "SMCI", "DELL", "HPQ", "WDC", "STX",
    # industrials, transport, travel
    "BA", "GE", "CAT", "DE", "MMM", "HON", "LMT", "RTX", "UNP", "UPS", "FDX", "DAL", "UAL", "AAL", "LUV",
    "CCL", "RCL", "NCLH", "MAR", "BKNG", "EXPE",
    # financials
    "JPM", "BAC", "C", "WFC", "GS", "MS", "SCHW", "AXP", "V", "MA", "USB", "PNC", "COF", "BLK",
    # health
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD", "CELG", "BIIB", "REGN", "VRTX", "MRNA", "UNH",
    "CVS", "WBA", "CI", "HUM", "ISRG", "TMO", "ABT", "MDT",
    # energy and materials
    "XOM", "CVX", "COP", "OXY", "SLB", "HAL", "DVN", "MPC", "FCX", "NEM", "DOW",
    # consumer
    "KO", "PEP", "PG", "WMT", "COST", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "LULU", "ETSY", "F", "GM",
    "MO", "PM", "KHC", "CL", "MDLZ", "KR", "DG", "DLTR", "M", "GPS", "CMG", "YUM",
)

PRE_MARKET_NEWS_FROM_HOUR = 16  # news counts from the previous session's close (16:00 NY)


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime  # tz-aware
    close: datetime  # tz-aware; 13:00 on half days

    @property
    def half_day(self) -> bool:
        return self.close.hour < 16


def sessions(start: date, end: date) -> list[Session]:
    """NYSE sessions with real open/close times (holidays skipped, half days included)."""
    import pandas_market_calendars as mcal

    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)
    return [Session(idx.date(), row.market_open.tz_convert("America/New_York").to_pydatetime(),
                    row.market_close.tz_convert("America/New_York").to_pydatetime())
            for idx, row in sched.iterrows()]


def dollar_volume_rank(daily: dict[str, pd.DataFrame], day: date, top_n: int = 100,
                       lookback: int = 20) -> list[str]:
    """Top ``top_n`` symbols by mean dollar volume over the ``lookback`` sessions BEFORE ``day``."""
    ts = pd.Timestamp(day)
    scores = {}
    for sym, df in daily.items():
        prior = df[df.index < ts].tail(lookback)
        if len(prior) < lookback:
            continue
        scores[sym] = float((prior["close"] * prior["volume"]).mean())
    return [s for s, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]
