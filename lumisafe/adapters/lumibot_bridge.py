"""The ONLY lumisafe module that imports lumibot (SPEC §3 rule 1).

Every coupling to Lumibot's API lives here, so a Lumibot upgrade means fixing
one file. Imports are lazy so the CLI and unit tests don't pay Lumibot's
import cost unless they actually trade or backtest.

Paper only: :func:`make_paper_broker` always builds Alpaca with ``PAPER=True``
and refuses to run if the environment says otherwise. This module deliberately
contains no live-broker factory. Wiring live trading is left to the human
owner after :mod:`lumisafe.live_unlock` passes (SPEC §3 rule 2, §10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd


class PaperOnlyError(RuntimeError):
    pass


# Lumibot builds a default broker from .env credentials at import time (and
# opens an Alpaca trading stream) unless credentials are deferred. LumiSafe
# always builds its own paper broker explicitly, so defer the implicit one.
os.environ.setdefault("LUMIBOT_LAZY_CREDENTIALS", "true")


# --- Strategy base class ------------------------------------------------------------


def strategy_base():
    """Lumibot's Strategy class. runner.py subclasses it via this function."""
    from lumibot.strategies import Strategy

    return Strategy


# --- account state ------------------------------------------------------------------


@dataclass(frozen=True)
class AccountState:
    as_of: datetime
    cash: float
    equity: float
    positions: dict[str, float]
    prices: dict[str, float]
    is_backtesting: bool


def account_state(strategy: Any, symbols: set[str]) -> AccountState:
    """Read cash, equity, stock/ETF positions and last prices from a running Strategy."""
    positions: dict[str, float] = {}
    for p in strategy.get_positions() or []:
        asset = getattr(p, "asset", None)
        sym = getattr(asset, "symbol", None)
        atype = str(getattr(asset, "asset_type", "stock")).lower()
        if not sym or ("stock" not in atype and "etf" not in atype):
            continue
        qty = float(p.quantity)
        if qty != 0:
            positions[sym] = qty
    prices: dict[str, float] = {}
    for sym in sorted(set(symbols) | set(positions)):
        try:
            px = strategy.get_last_price(sym)
        except Exception:
            px = None
        if px is not None and float(px) > 0:
            prices[sym] = float(px)
    return AccountState(
        as_of=strategy.get_datetime(),
        cash=float(strategy.get_cash()),
        equity=float(strategy.get_portfolio_value()),
        positions=positions,
        prices=prices,
        is_backtesting=bool(getattr(strategy, "is_backtesting", False)),
    )


def last_price(strategy: Any, symbol: str) -> float | None:
    try:
        px = strategy.get_last_price(symbol)
    except Exception:
        return None
    return float(px) if px is not None and float(px) > 0 else None


def daily_closes(strategy: Any, symbol: str, length: int) -> pd.Series | None:
    """Daily close series up to the strategy's current time (no look-ahead in backtests)."""
    try:
        bars = strategy.get_historical_prices(symbol, length, "day")
        df = getattr(bars, "df", None)
        if df is None or df.empty:
            return None
        return df["close"].astype(float)
    except Exception:
        return None


# --- orders ---------------------------------------------------------------------------


def submit(strategy: Any, symbol: str, side: str, quantity: float, reason: str) -> str:
    """Place a market DAY order through Lumibot and return its identifier.

    Only ``lumisafe/runner.py`` may call this, and only after RiskGate approval
    (an AST test enforces the caller). DAY time-in-force is what Alpaca
    requires for fractional share orders.
    """
    qty = Decimal(str(round(float(quantity), 6)))
    order = strategy.create_order(symbol, qty, side, time_in_force="day")
    strategy.submit_order(order, reason=reason)
    return str(order.identifier)


def order_id(order: Any) -> str:
    return str(getattr(order, "identifier", ""))


def order_fill(strategy: Any, identifier: str) -> tuple[str, float | None, float | None]:
    """(status, avg_fill_price, filled_quantity) for a previously submitted order."""
    order = strategy.get_order(identifier)
    if order is None:
        return "unknown", None, None
    status = str(getattr(order, "status", "unknown")).lower()
    if order.is_filled():
        price = getattr(order, "avg_fill_price", None)
        return "filled", float(price) if price is not None else None, float(order.quantity)
    if getattr(order, "is_canceled", lambda: False)():
        return "canceled", None, None
    return status, None, None


# --- historical data (Yahoo through Lumibot) ---------------------------------------------


def _adjusted_ohlcv(symbol: str) -> pd.DataFrame | None:
    """Real Yahoo daily bars, dividend/split adjusted (total return), with full OHLC.

    Lumibot 4.6.54's ``auto_adjust=True`` keeps only Close, so we take the raw
    bars and scale Open/High/Low/Close by Yahoo's own ``Adj Close / Close``
    factor. This is the standard total-return adjustment of real data. Nothing
    is synthesized, and days Yahoo doesn't have stay missing (RULE #1).
    """
    from lumibot.tools import YahooHelper

    raw = YahooHelper.get_symbol_data(symbol, interval="1d", caching=True, auto_adjust=False)
    if raw is None or raw.empty:
        return None
    raw = raw.rename(columns=str.lower)
    if not {"open", "high", "low", "close", "adj close"} <= set(raw.columns):
        return None
    raw = raw.dropna(subset=["open", "high", "low", "close", "adj close"])
    raw = raw[(raw["close"] > 0) & (raw["adj close"] > 0)]
    factor = raw["adj close"] / raw["close"]
    df = pd.DataFrame({
        "open": raw["open"] * factor, "high": raw["high"] * factor, "low": raw["low"] * factor,
        "close": raw["adj close"], "volume": raw.get("volume", 0),
    }, index=raw.index)
    return df


def fetch_daily_history(symbols: list[str], start: datetime | None = None,
                        end: datetime | None = None) -> dict[str, pd.DataFrame]:
    """Dividend/split-adjusted daily OHLCV per symbol (Yahoo via Lumibot), naive NY-date index.

    Uses the per-symbol ``get_symbol_data``. Lumibot 4.6.54's
    ``YahooHelper.get_symbols_data`` is defined twice and the second definition
    calls itself (infinite recursion). See DECISIONS.md.
    """
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = _adjusted_ohlcv(sym)
        if df is None or df.empty:
            continue
        df = df.copy()
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert("America/New_York").tz_localize(None)
        df.index = idx.normalize()
        if start is not None:
            df = df[df.index >= _naive(start)]
        if end is not None:
            df = df[df.index <= _naive(end)]
        out[sym] = df
    return out


def _naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_convert("America/New_York").tz_localize(None) if t.tz is not None else t


# --- backtesting -------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestOutput:
    results: Any
    strategy: Any
    stats_dir: Path


def _pandas_data(symbols: list[str], start: datetime, end: datetime) -> dict:
    from lumibot.entities import Asset, Data

    data = {}
    lo = _naive(start) - pd.Timedelta(days=400)  # warm-up history for indicators
    hi = _naive(end) + pd.Timedelta(days=7)
    for sym in symbols:
        df = _adjusted_ohlcv(sym)
        if df is None or df.empty:
            raise ValueError(f"No Yahoo data for {sym}")
        idx = pd.DatetimeIndex(df.index)
        naive = idx.tz_convert("America/New_York").tz_localize(None) if idx.tz is not None else idx
        df = df[(naive >= lo) & (naive <= hi)]
        asset = Asset(sym, asset_type="stock")
        data[asset] = Data(asset, df, timestep="day")
    return data


def run_backtest(strategy_cls: type, parameters: dict, start: datetime, end: datetime, budget: float,
                 benchmark: str, out_dir: Path, symbols: list[str], cost_fraction: float = 0.001) -> BacktestOutput:
    """Backtest ``strategy_cls`` with Lumibot's PandasDataBacktesting on adjusted Yahoo bars.

    * Data: real Yahoo daily bars, dividend-adjusted (see ``_adjusted_ohlcv``).
      Lumibot's own Yahoo backtest prices raw closes and credits no dividend
      cash in 4.6.54, which silently drops ~2%/yr for an ETF portfolio
      (DECISIONS.md, Phase 4). ``symbols`` must list every symbol the strategy
      can trade.
    * Costs: ``cost_fraction`` is charged as a percent fee on buys and sells.
      The default 0.001 is 5 bps commission plus 0.05% slippage (SPEC §8).
      Lumibot's ``TradingSlippage`` is an absolute price amount, so
      percentage slippage is folded into the fee.
    """
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import TradingFee

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fee = TradingFee(percent_fee=cost_fraction)
    os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "false")
    previous = os.environ.get("IS_BACKTESTING")
    os.environ["IS_BACKTESTING"] = "true"  # never touch a broker during a backtest
    try:
        pandas_data = _pandas_data(sorted(set(symbols) | {benchmark}), start, end)
        results, strat = strategy_cls.run_backtest(
            PandasDataBacktesting,
            start,
            end,
            pandas_data=pandas_data,
            budget=budget,
            benchmark_asset=benchmark,
            parameters=parameters,
            buy_trading_fees=[fee],
            sell_trading_fees=[fee],
            show_plot=False,
            show_tearsheet=False,
            save_tearsheet=False,
            show_indicators=False,
            show_progress_bar=False,
            save_stats_file=True,
            stats_file=str(out_dir / "stats.csv"),
            plot_file_html=str(out_dir / "plot.html"),
            trades_file=str(out_dir / "trades.csv"),
            settings_file=str(out_dir / "settings.json"),
            tearsheet_file=str(out_dir / "tearsheet.html"),
            indicators_file=str(out_dir / "indicators.html"),
            logfile=str(out_dir / "log.txt"),
            quiet_logs=True,
        )
    finally:
        if previous is None:
            os.environ.pop("IS_BACKTESTING", None)
        else:
            os.environ["IS_BACKTESTING"] = previous
    return BacktestOutput(results=results, strategy=strat, stats_dir=out_dir)


# --- paper trading -------------------------------------------------------------------


def _paper_config() -> dict[str, Any]:
    if os.environ.get("ALPACA_IS_PAPER", "true").strip().lower() not in ("true", "1", "yes"):
        raise PaperOnlyError(
            "ALPACA_IS_PAPER is set to a non-paper value. LumiSafe only runs Alpaca in paper mode."
        )
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        raise PaperOnlyError("ALPACA_API_KEY / ALPACA_API_SECRET are not set (put your PAPER keys in .env).")
    return {"API_KEY": key, "API_SECRET": secret, "PAPER": True}


def load_dotenv_file(path: str | Path = ".env") -> None:
    """Load ``.env`` without overriding variables already set in the shell."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    if Path(path).is_file():
        load_dotenv(path, override=False)


def make_paper_broker():
    """Alpaca broker forced to PAPER. There is intentionally no live equivalent here."""
    from lumibot.brokers import Alpaca

    config = _paper_config()
    broker = Alpaca(config)
    if getattr(broker, "is_paper", True) is False:  # pragma: no cover - belt and braces
        raise PaperOnlyError("Alpaca broker did not come up in paper mode; refusing to trade.")
    return broker


def run_paper(strategy_cls: type, parameters: dict, run_once: bool = True, name: str = "LumiSafe") -> Any:
    """Run ``strategy_cls`` against Alpaca PAPER: one iteration, or Lumibot's live loop."""
    from lumibot.traders import Trader

    broker = make_paper_broker()
    strategy = strategy_cls(broker=broker, parameters=parameters, name=name)
    trader = Trader(logfile="", backtest=False, quiet_logs=True)
    trader.add_strategy(strategy)
    trader.run_all(run_once=run_once, show_plot=False, show_tearsheet=False, save_tearsheet=False)
    return strategy
