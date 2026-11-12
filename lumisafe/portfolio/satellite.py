"""SatelliteManager: runs only promoted strategies inside a capped sleeve (SPEC §8).

P1: only strategies whose registry state is in ``config.allowed_states``
produce intents. DRAFT, BACKTESTED, REJECTED and RETIRED never do.

Accounting: each strategy has its own ledger (sleeve cash + positions),
seeded once with ``max_weight × account equity`` split across active
strategies. Satellite equity is the ledgers' cash plus the market value of
their positions, so losses show up honestly (R6/R7 measure this number). The
core never spends the sleeve's cash (``cash_reserve``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from lumisafe.config.schema import SatelliteConfig
from lumisafe.journal.journal import Journal
from lumisafe.promotion import criteria
from lumisafe.promotion.registry import Registry, State
from lumisafe.types import OrderIntent, PortfolioSnapshot, Side, Source, Verdict

MIN_ORDER_USD = 10.0
EQUITY_EVENT = "satellite_equity"


class SatelliteManager:
    def __init__(self, config: SatelliteConfig, registry: Registry, strategies: dict[str, Any], journal: Journal,
                 state: Any, history_fn: Callable[[list[str], datetime], dict[str, pd.DataFrame]],
                 benchmark: str = "SPY", fractional: bool = True, kill_switch: Any = None):
        self.config = config
        self.registry = registry
        self.strategies = strategies
        self.journal = journal
        self.state = state
        self.history_fn = history_fn
        self.benchmark = benchmark
        self.fractional = fractional
        self.kill_switch = kill_switch

    # -- selection (P1) -------------------------------------------------------------------

    def active(self) -> list[Any]:
        if not self.config.enabled or self.config.max_weight <= 0:
            return []
        allowed = {State(s) for s in self.config.allowed_states if s in State.__members__}
        allowed -= {State.DRAFT, State.BACKTESTED, State.REJECTED, State.RETIRED}  # P1, belt and braces
        return [s for name, s in sorted(self.strategies.items()) if self.registry.state(name) in allowed]

    # -- ledgers ------------------------------------------------------------------------------

    def _ledgers(self) -> dict[str, dict]:
        return self.state.get("satellite_ledgers", {}) or {}

    def _save_ledgers(self, ledgers: dict[str, dict]) -> None:
        self.state.set("satellite_ledgers", ledgers)

    def _ensure_ledgers(self, equity: float) -> dict[str, dict]:
        ledgers = self._ledgers()
        active = [s.name for s in self.active()]
        new = [n for n in active if n not in ledgers]
        if new:
            budget = self.config.max_weight * equity
            used = sum(self._ledger_value(l, {}) for n, l in ledgers.items() if n in active)
            per = max(0.0, budget - used) / len(new)
            for n in new:
                ledgers[n] = {"cash": per, "positions": {}}
            self._save_ledgers(ledgers)
        return ledgers

    @staticmethod
    def _ledger_value(ledger: dict, prices: dict[str, float]) -> float:
        return ledger["cash"] + sum(q * prices.get(s, 0.0) for s, q in ledger["positions"].items())

    def symbols(self) -> set[str]:
        syms = set()
        for s in self.strategies.values():
            syms |= set(s.universe)
        for l in self._ledgers().values():
            syms |= set(l["positions"])
        return syms

    def equity(self, acct) -> float:
        self._ensure_ledgers(acct.equity)
        return sum(self._ledger_value(l, acct.prices) for l in self._ledgers().values())

    def cash_reserve(self, acct) -> float:
        return sum(max(0.0, l["cash"]) for l in self._ledgers().values())

    def on_fill(self, strategy_name: str | None, symbol: str, side: str, price: float, qty: float) -> None:
        ledgers = self._ledgers()
        if not strategy_name or strategy_name not in ledgers:
            return
        l = ledgers[strategy_name]
        sign = 1 if side == Side.BUY.value else -1
        l["cash"] -= sign * price * qty
        l["positions"][symbol] = l["positions"].get(symbol, 0.0) + sign * qty
        if abs(l["positions"][symbol]) < 1e-9:
            del l["positions"][symbol]
        self._save_ledgers(ledgers)

    # -- intents --------------------------------------------------------------------------------

    def _due(self, strategy, as_of: datetime) -> bool:
        key = f"{as_of.year}-{as_of.month:02d}" if strategy.rebalance_frequency == "monthly" \
            else f"{as_of.isocalendar().year}-W{as_of.isocalendar().week:02d}"
        last = (self.state.get("satellite_last_rebalance") or {}).get(strategy.name)
        return last != key

    def _mark(self, strategy, as_of: datetime) -> None:
        key = f"{as_of.year}-{as_of.month:02d}" if strategy.rebalance_frequency == "monthly" \
            else f"{as_of.isocalendar().year}-W{as_of.isocalendar().week:02d}"
        marks = dict(self.state.get("satellite_last_rebalance") or {})
        marks[strategy.name] = key
        self.state.set("satellite_last_rebalance", marks)

    def intents(self, snap: PortfolioSnapshot, acct, executor) -> list[OrderIntent]:
        out: list[OrderIntent] = []
        ledgers = self._ensure_ledgers(snap.equity)
        for strat in self.active():
            if not self._due(strat, snap.as_of):
                continue
            history = self.history_fn(list(strat.universe), snap.as_of)
            weights = strat.target_weights(history, pd.Timestamp(snap.as_of))
            ledger = ledgers[strat.name]
            sleeve_value = self._ledger_value(ledger, snap.prices)
            cap = self.config.max_weight * snap.equity
            sleeve_value = min(sleeve_value, cap)
            for sym in sorted(set(weights) | set(ledger["positions"])):
                px = snap.prices.get(sym)
                if not px:
                    continue
                target_qty = weights.get(sym, 0.0) * sleeve_value / px
                diff = target_qty - ledger["positions"].get(sym, 0.0)
                qty = round(abs(diff), 4) if self.fractional else float(int(abs(diff)))
                if qty <= 0 or qty * px < MIN_ORDER_USD:
                    continue
                side = Side.BUY if diff > 0 else Side.SELL
                out.append(OrderIntent(
                    intent_id=str(uuid.uuid4()), created_at=snap.as_of, source=Source.SATELLITE, symbol=sym,
                    side=side, quantity=qty, strategy_name=strat.name,
                    reason=f"{strat.name}: rebalance to {weights.get(sym, 0.0):.0%} of the satellite sleeve.",
                ))
            self._mark(strat, snap.as_of)
        return out

    # -- promotion & retirement (run each cycle) ----------------------------------------------------

    def housekeeping(self, snap: PortfolioSnapshot) -> None:
        ledgers = self._ledgers()
        for name, ledger in ledgers.items():
            self.journal.record_event(EQUITY_EVENT, {"strategy": name,
                                                     "equity": self._ledger_value(ledger, snap.prices)},
                                      ts=snap.as_of)
        r7 = self.kill_switch.r7_count_within(snap.as_of) if self.kill_switch is not None else 0
        for name in list(self.strategies):
            st = self.registry.state(name)
            if st in (None, State.RETIRED, State.REJECTED, State.DRAFT):
                continue
            trailing, bench = self.trailing_returns(name, snap.as_of)
            d = criteria.should_retire(trailing, bench, r7)
            if d.passed:
                self.registry.transition(name, State.RETIRED, d.metrics, d.reasons, at=snap.as_of)
                continue
            if st is State.PAPER:
                stats = self.paper_stats(name, snap.as_of)
                promote = criteria.paper_to_live_eligible(stats)
                if promote.passed:
                    self.registry.transition(name, State.LIVE_ELIGIBLE, promote.metrics, at=snap.as_of)
                else:
                    failing = criteria.paper_is_failing(stats)
                    if failing.passed:
                        self.registry.transition(name, State.REJECTED, failing.metrics, failing.reasons,
                                                 at=snap.as_of)

    def _equity_series(self, name: str) -> pd.Series:
        pts = {pd.Timestamp(e.ts): e.payload["equity"] for e in self.journal.events(EQUITY_EVENT)
               if e.payload.get("strategy") == name}
        return pd.Series(pts, dtype=float).sort_index()

    def trailing_returns(self, name: str, as_of: datetime) -> tuple[float | None, float | None]:
        eq = self._equity_series(name)
        if len(eq) < criteria.RETIRE_TRAILING_DAYS:
            return None, None
        window = eq.iloc[-criteria.RETIRE_TRAILING_DAYS:]
        mine = float(window.iloc[-1] / window.iloc[0] - 1) if window.iloc[0] > 0 else None
        hist = self.history_fn([self.benchmark], as_of).get(self.benchmark)
        if hist is None or hist.empty:
            return mine, None
        c = hist["close"]
        start = pd.Timestamp(window.index[0])
        start = start.tz_convert("America/New_York").tz_localize(None) if start.tz is not None else start
        c = c[c.index >= start.normalize()]
        bench = float(c.iloc[-1] / c.iloc[0] - 1) if len(c) >= 2 else None
        return mine, bench

    def paper_stats(self, name: str, as_of: datetime) -> criteria.PaperStats:
        rec = self.registry.record(name) or {}
        since = datetime.fromisoformat(rec["paper_started_at"]) if rec.get("paper_started_at") else None
        eq = self._equity_series(name)
        if since is not None:
            s = pd.Timestamp(since)
            eq = eq[eq.index >= s]
        days = int(len({ts.date() for ts in eq.index}))
        entries = [e for e in self.journal.query(source=Source.SATELLITE, since=since)
                   if e.intent.strategy_name == name]
        violations = sum(1 for e in entries
                         if any(r.reviewer == "risk" and r.verdict in (Verdict.BLOCK, Verdict.RESIZE)
                                for r in e.reviews))
        not_filled = {ev.payload.get("entry_id") for ev in self.journal.events("order_not_filled")}
        stale = as_of - timedelta(days=3)
        unexplained = sum(1 for e in entries if e.final in (Verdict.APPROVE, Verdict.RESIZE)
                          and e.fill_qty is None and e.ts < stale and e.entry_id not in not_filled)
        paper_return = float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) >= 2 and eq.iloc[0] > 0 else 0.0
        oos_cagr = float((rec.get("backtest_metrics") or {}).get("oos_cagr", 0.0))
        expected = (1 + oos_cagr) ** (days / 252) - 1 if days else 0.0
        return criteria.PaperStats(days, violations, paper_return, expected, unexplained)
