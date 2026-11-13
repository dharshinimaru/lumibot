"""Historical market data from Alpaca (SIP feed) with an on-disk cache.

Used by the day-trading lab only. Read-only: it never touches orders or the
account. Keys come from the environment (``ALPACA_API_KEY`` /
``ALPACA_API_SECRET``, loaded from ``.env`` by the CLI) and are never logged.

RULE #1 (no fake data): anything Alpaca doesn't return stays missing. A day
with no bars, no news or no quotes is simply absent; nothing is filled in.

Cache layout under ``paths.cache_dir()``:
  daily_<adjustment>/<SYMBOL>.parquet   whole history per symbol
  minute/<YYYY-MM-DD>.parquet           regular-hours minute bars, split-adjusted, many symbols
  news/<YYYY-MM-DD>.json                headlines in a time window, with their publish time
  spreads.json                          median quoted spread (fraction of mid) at a minute
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from lumisafe import paths

NY = ZoneInfo("America/New_York")
REQUESTS_PER_MINUTE = 180  # Alpaca's free data plan allows 200/min; stay under it


class RateLimiter:
    def __init__(self, per_minute: int = REQUESTS_PER_MINUTE):
        self.per_minute = per_minute
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] > 60:
                self._times.popleft()
            if len(self._times) >= self.per_minute:
                time.sleep(60 - (now - self._times[0]) + 0.05)
            self._times.append(time.monotonic())


def _retry(fn, tries: int = 5):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # network blips and 429s: back off and retry
            if attempt == tries - 1:
                raise
            msg = str(exc).lower()
            time.sleep(min(60, (2 ** attempt) * (5 if "429" in msg or "rate" in msg else 1)))


class AlpacaMarketData:
    """Thin cached wrapper around alpaca-py's historical clients."""

    def __init__(self, cache: Path | None = None, key: str | None = None, secret: str | None = None,
                 limiter: RateLimiter | None = None):
        self.cache = Path(cache or paths.cache_dir())
        self._key = key or os.environ.get("ALPACA_API_KEY")
        self._secret = secret or os.environ.get("ALPACA_API_SECRET")
        self.limiter = limiter or RateLimiter()
        self._stock = None
        self._news = None
        self._spreads: dict[str, float | None] | None = None

    # -- clients ------------------------------------------------------------------------------

    def _stock_client(self):
        if self._stock is None:
            if not (self._key and self._secret):
                raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET are not set (put your PAPER keys in .env).")
            from alpaca.data.historical import StockHistoricalDataClient

            self._stock = StockHistoricalDataClient(self._key, self._secret)
        return self._stock

    def _news_client(self):
        if self._news is None:
            from alpaca.data.historical.news import NewsClient

            self._news = NewsClient(self._key, self._secret)
        return self._news

    # -- daily bars ---------------------------------------------------------------------------

    def daily_bars(self, symbols: Iterable[str], start: date, end: date, adjustment: str = "split",
                   refresh: bool = False) -> dict[str, pd.DataFrame]:
        """Daily OHLCV per symbol, index = NY trading date (naive Timestamp)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        folder = self.cache / f"daily_{adjustment}"
        folder.mkdir(parents=True, exist_ok=True)
        out: dict[str, pd.DataFrame] = {}
        todo = []
        for s in symbols:
            f = folder / f"{s}.parquet"
            if f.is_file() and not refresh:  # cached whole range (pass refresh=True to extend it)
                out[s] = pd.read_parquet(f)
                continue
            todo.append(s)
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                   start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                                   end=datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
                                   adjustment=Adjustment(adjustment), feed=DataFeed.SIP)
            self.limiter.wait()
            df = _retry(lambda: self._stock_client().get_stock_bars(req).df)
            for s in chunk:
                if df is not None and len(df) and s in df.index.get_level_values(0):
                    d = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].copy()
                    d.index = pd.DatetimeIndex(d.index).tz_convert(NY).tz_localize(None).normalize()
                else:
                    d = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
                d.to_parquet(folder / f"{s}.parquet")
                out[s] = d
        return out

    # -- minute bars --------------------------------------------------------------------------

    def minute_bars(self, symbols: Iterable[str], day: date, open_ts: datetime, close_ts: datetime
                    ) -> dict[str, pd.DataFrame]:
        """Regular-session minute bars (split-adjusted) for one day. Index = bar start (tz-aware NY)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        folder = self.cache / "minute"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{day.isoformat()}.parquet"
        symbols = sorted(set(symbols))
        have = pd.read_parquet(f) if f.is_file() else None
        cached = set(have["symbol"].unique()) if have is not None and len(have) else set()
        done = set(json.loads(have.attrs.get("requested", "[]"))) if have is not None else set()
        need = [s for s in symbols if s not in cached and s not in done]
        if need:
            req = StockBarsRequest(symbol_or_symbols=need, timeframe=TimeFrame.Minute, start=open_ts,
                                   end=close_ts - timedelta(seconds=1), adjustment=Adjustment.SPLIT,
                                   feed=DataFeed.SIP)
            self.limiter.wait()
            df = _retry(lambda: self._stock_client().get_stock_bars(req).df)
            new = df.reset_index()[["symbol", "timestamp", "open", "high", "low", "close", "volume"]] \
                if df is not None and len(df) else pd.DataFrame(
                    columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"])
            have = new if have is None else pd.concat([have, new], ignore_index=True)
            have.attrs["requested"] = json.dumps(sorted(done | set(need)))
            have.to_parquet(f)
        out = {}
        for s in symbols:
            d = have[have["symbol"] == s]
            if len(d):
                d = d.set_index(pd.DatetimeIndex(d["timestamp"]).tz_convert(NY))[
                    ["open", "high", "low", "close", "volume"]].sort_index()
                out[s] = d
        return out

    # -- news ---------------------------------------------------------------------------------

    def news(self, symbols: Iterable[str], day: date, start: datetime, end: datetime,
             folder_name: str = "news") -> list[dict]:
        """Headlines for ``symbols`` published in [start, end]. Cached per trading day.

        One folder per window convention: a cache file only answers requests for the
        same window it was filled with (``news`` = gap-day mornings, ``news_full`` = whole days).
        """
        from alpaca.data.requests import NewsRequest

        folder = self.cache / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{day.isoformat()}.json"
        symbols = sorted(set(symbols))
        cached = json.loads(f.read_text()) if f.is_file() else {"requested": [], "items": []}
        need = [s for s in symbols if s not in cached["requested"]]
        if need:
            # limit=None: alpaca-py pages through everything itself (a numeric limit caps the TOTAL;
            # limit=50 silently returned only the first 50 headlines of a busy day).
            req = NewsRequest(symbols=",".join(need), start=start, end=end, limit=None, exclude_contentless=False)
            self.limiter.wait()
            res = _retry(lambda: self._news_client().get_news(req))
            batch = res.data.get("news", []) if hasattr(res, "data") else getattr(res, "news", [])
            items = [{"created_at": n.created_at.isoformat(), "headline": n.headline, "source": n.source,
                      "symbols": list(n.symbols)} for n in batch]
            known = {(i["created_at"], i["headline"]) for i in cached["items"]}
            cached["items"] += [i for i in items if (i["created_at"], i["headline"]) not in known]
            cached["requested"] = sorted(set(cached["requested"]) | set(need))
            f.write_text(json.dumps(cached))
        lo, hi = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        wanted = set(symbols)
        return [i for i in cached["items"] if set(i["symbols"]) & wanted
                and lo <= datetime.fromisoformat(i["created_at"]).astimezone(timezone.utc) <= hi]

    # -- quoted spread --------------------------------------------------------------------------

    def _spread_cache(self) -> dict[str, float | None]:
        if self._spreads is None:
            f = self.cache / "spreads.json"
            self._spreads = json.loads(f.read_text()) if f.is_file() else {}
        return self._spreads

    def save_spreads(self) -> None:
        if self._spreads is not None:
            (self.cache / "spreads.json").write_text(json.dumps(self._spreads))

    def spread(self, symbol: str, at: datetime) -> float | None:
        """Median quoted spread, as a fraction of the mid price, over the first 50 quotes from ``at``.

        Uses the real NBBO. Returns None when Alpaca has no quotes (it is never guessed).
        """
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest

        key = f"{symbol}|{at.astimezone(NY):%Y-%m-%d %H:%M}"
        cache = self._spread_cache()
        if key in cache:
            return cache[key]
        req = StockQuotesRequest(symbol_or_symbols=symbol, start=at, end=at + timedelta(minutes=5), limit=50,
                                 feed=DataFeed.SIP)
        self.limiter.wait()
        q = _retry(lambda: self._stock_client().get_stock_quotes(req).df)
        value = None
        if q is not None and len(q):
            q = q[(q["bid_price"] > 0) & (q["ask_price"] >= q["bid_price"])]
            if len(q):
                mid = (q["ask_price"] + q["bid_price"]) / 2
                value = float(((q["ask_price"] - q["bid_price"]) / mid).median())
        cache[key] = value
        return value


class LiveMarketData:
    """Same data as ``AlpacaMarketData`` but for *today*, never cached (the day isn't finished)."""

    def __init__(self, md: AlpacaMarketData):
        self.md = md

    def recent_daily(self, symbols: Iterable[str], days: int = 45) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        out: dict[str, pd.DataFrame] = {}
        symbols = sorted(set(symbols))
        start = datetime.now(timezone.utc) - timedelta(days=days)
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start,
                                   adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
            self.md.limiter.wait()
            df = _retry(lambda: self.md._stock_client().get_stock_bars(req).df)
            for s in chunk:
                if df is not None and len(df) and s in df.index.get_level_values(0):
                    d = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].copy()
                    d.index = pd.DatetimeIndex(d.index).tz_convert(NY).tz_localize(None).normalize()
                    out[s] = d
        return out

    def minute_bars(self, symbols: Iterable[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        """Today's minute bars as actually traded (RAW: no later split can apply to today)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start, end=end,
                               adjustment=Adjustment.RAW, feed=DataFeed.SIP)
        self.md.limiter.wait()
        df = _retry(lambda: self.md._stock_client().get_stock_bars(req).df)
        out = {}
        if df is None or not len(df):
            return out
        for s in symbols:
            if s in df.index.get_level_values(0):
                d = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].copy()
                d.index = pd.DatetimeIndex(d.index).tz_convert(NY)
                out[s] = d.sort_index()
        return out

    def news(self, symbols: Iterable[str], start: datetime, end: datetime) -> list[dict]:
        from alpaca.data.requests import NewsRequest

        symbols = sorted(set(symbols))
        if not symbols:
            return []
        req = NewsRequest(symbols=",".join(symbols), start=start, end=end, limit=None)  # SDK pages itself
        self.md.limiter.wait()
        res = _retry(lambda: self.md._news_client().get_news(req))
        batch = res.data.get("news", []) if hasattr(res, "data") else getattr(res, "news", [])
        return [{"created_at": n.created_at.isoformat(), "headline": n.headline, "source": n.source,
                 "symbols": list(n.symbols)} for n in batch]

    def spread(self, symbol: str, at: datetime) -> float | None:
        """Median quoted spread / mid over the latest quotes up to ``at`` (real NBBO; None if no quotes)."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest

        req = StockQuotesRequest(symbol_or_symbols=symbol, start=at - timedelta(minutes=2), end=at, limit=200,
                                 feed=DataFeed.SIP)
        self.md.limiter.wait()
        q = _retry(lambda: self.md._stock_client().get_stock_quotes(req).df)
        if q is None or not len(q):
            return None
        q = q[(q["bid_price"] > 0) & (q["ask_price"] >= q["bid_price"])].tail(50)
        if not len(q):
            return None
        mid = (q["ask_price"] + q["bid_price"]) / 2
        return float(((q["ask_price"] - q["bid_price"]) / mid).median())
