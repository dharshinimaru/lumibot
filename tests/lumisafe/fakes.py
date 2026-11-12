"""A fake Lumibot Strategy executor: just the methods the bridge calls."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime


@dataclass
class _Asset:
    symbol: str
    asset_type: str = "stock"


@dataclass
class _Position:
    asset: _Asset
    quantity: float


class FakeOrder:
    _ids = itertools.count(1)

    def __init__(self, symbol, quantity, side, **kw):
        self.symbol = symbol
        self.quantity = float(quantity)
        self.side = side
        self.kw = kw
        self.identifier = f"ord{next(self._ids)}"
        self.status = "new"
        self.avg_fill_price = None

    def is_filled(self):
        return self.status == "filled"

    def is_canceled(self):
        return self.status == "canceled"


class FakeExecutor:
    """Fills market orders immediately at the last price when ``auto_fill`` is on."""

    def __init__(self, now: datetime, cash: float, prices: dict, positions: dict | None = None,
                 auto_fill=True, is_backtesting=True, fail_submit=False):
        self.now = now
        self.cash = cash
        self.prices = dict(prices)
        self.positions = dict(positions or {})
        self.auto_fill = auto_fill
        self.is_backtesting = is_backtesting
        self.fail_submit = fail_submit
        self.orders: dict[str, FakeOrder] = {}
        self.submitted: list[FakeOrder] = []
        self.fill_listeners = []

    # account
    def get_positions(self):
        return [_Position(_Asset(s), q) for s, q in self.positions.items() if q]

    def get_last_price(self, symbol):
        return self.prices.get(symbol)

    def get_cash(self):
        return self.cash

    def get_portfolio_value(self):
        return self.cash + sum(q * self.prices.get(s, 0) for s, q in self.positions.items())

    def get_datetime(self):
        return self.now

    def get_historical_prices(self, symbol, length, timestep):
        return None

    # orders
    def create_order(self, symbol, quantity, side, **kw):
        return FakeOrder(symbol, quantity, side, **kw)

    def submit_order(self, order, reason=None):
        if self.fail_submit:
            raise RuntimeError("broker down")
        order.reason = reason
        self.orders[order.identifier] = order
        self.submitted.append(order)
        if self.auto_fill:
            self.fill(order)
        return order

    def fill(self, order, price=None):
        price = price or self.prices[order.symbol]
        sign = 1 if order.side == "buy" else -1
        self.cash -= sign * order.quantity * price
        self.positions[order.symbol] = self.positions.get(order.symbol, 0) + sign * order.quantity
        order.status = "filled"
        order.avg_fill_price = price
        for cb in self.fill_listeners:
            cb(order.identifier, price, order.quantity, self.now)

    def get_order(self, identifier):
        return self.orders.get(identifier)
