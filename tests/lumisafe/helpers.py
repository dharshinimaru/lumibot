"""Small builders shared by LumiSafe tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from lumisafe.types import OrderIntent, PortfolioSnapshot, Side, Source, TaxLot

T0 = datetime(2025, 6, 2, 15, 0, tzinfo=timezone.utc)  # a Monday


def intent(symbol="VTI", side=Side.BUY, qty=1.0, source=Source.USER, at=T0, reason="test",
           strategy_name=None) -> OrderIntent:
    return OrderIntent(
        intent_id=str(uuid.uuid4()), created_at=at, source=source, symbol=symbol, side=side,
        quantity=qty, reason=reason, strategy_name=strategy_name,
    )


def snapshot(cash=10_000.0, positions=None, prices=None, as_of=T0, peak_equity=None,
             satellite_equity=0.0, satellite_peak_equity=0.0, satellite_day_start_equity=0.0,
             trades_this_week=0, lots=None, equity=None) -> PortfolioSnapshot:
    positions = dict(positions or {})
    prices = dict(prices or {})
    if equity is None:
        equity = cash + sum(q * prices.get(s, 0.0) for s, q in positions.items())
    if lots is None:
        # by default every holding is an old lot (bought 400 days ago at today's price)
        lots = [TaxLot(s, q, prices.get(s, 1.0), as_of - timedelta(days=400)) for s, q in positions.items() if q > 0]
    return PortfolioSnapshot(
        as_of=as_of, cash=cash, equity=equity, positions=positions, prices=prices,
        peak_equity=equity if peak_equity is None else peak_equity,
        satellite_equity=satellite_equity, satellite_peak_equity=satellite_peak_equity,
        satellite_day_start_equity=satellite_day_start_equity, trades_this_week=trades_this_week,
        lots=lots,
    )
