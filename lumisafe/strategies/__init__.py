"""Satellite strategy catalog. Add a strategy here to make it available to `lumisafe backtest`."""

from lumisafe.strategies.base import SatelliteStrategy
from lumisafe.strategies.trend_filter import TrendFilter

CATALOG = {"trend_filter": TrendFilter}

__all__ = ["CATALOG", "SatelliteStrategy", "TrendFilter"]
