"""Wires everything together. This is the only module that calls ``bridge.submit()`` (SPEC §4).

Order flow, every order, no exceptions::

    OrderIntent → BehaviorCoach → TaxGuard → AIAdvisor → RiskGate → bridge.submit → Journal

``Pipeline.run`` implements that flow and writes exactly one journal entry per
intent, whether it was submitted or rejected (J2). ``Engine`` runs one trading
cycle: it reads the account through the bridge, builds a
``PortfolioSnapshot``, asks the core (and satellite) for intents, and pushes
each through the pipeline. ``get_strategy_class()`` wraps the engine in a
Lumibot ``Strategy`` subclass for backtests and paper trading.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from lumisafe import paths
from lumisafe.adapters import lumibot_bridge as bridge
from lumisafe.coach.behavior import BehaviorCoach, MarketContext, PendingStore
from lumisafe.config.schema import LumiSafeConfig
from lumisafe.journal.journal import (
    PAPER_CYCLE_EVENT, Journal, JournalEntry, intent_from_dict, intent_to_dict, new_entry_id,
)
from lumisafe.portfolio.core import CoreIndexStrategy
from lumisafe.portfolio.lots import LotBook
from lumisafe.risk.gate import RiskGate
from lumisafe.risk.kill_switch import KillSwitch
from lumisafe.tax.guard import TaxGuard
from lumisafe.types import (
    OrderIntent, PortfolioSnapshot, ReviewResult, Side, Source, Verdict, clip_explanation,
)

# --- state ----------------------------------------------------------------------------


class StateStore:
    """Small JSON state (peaks, lots, halts, pending orders). In-memory when ``path`` is None."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = {}
        if self.path and self.path.is_file():
            self.data = json.loads(self.path.read_text() or "{}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True, default=str))
            tmp.replace(self.path)


class Inbox:
    """User intents queued by `lumisafe propose` / `confirm`, drained by the next paper cycle."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def push(self, intent: OrderIntent, confirmed: bool = False) -> None:
        items = self._load()
        items.append({"intent": intent_to_dict(intent), "confirmed": confirmed})
        self._write(items)

    def peek(self) -> list[tuple[OrderIntent, bool]]:
        return [(intent_from_dict(i["intent"]), bool(i.get("confirmed"))) for i in self._load()]

    def remove(self, intent_ids: set[str]) -> None:
        """Drop processed intents. Called only after they are journaled, so none is lost (J2)."""
        self._write([i for i in self._load() if i["intent"]["intent_id"] not in intent_ids])

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text() or "[]") if self.path.is_file() else []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        tmp.replace(self.path)


# --- pipeline ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    entry: JournalEntry
    order_id: str | None
    executed_qty: float

    @property
    def submitted(self) -> bool:
        return self.order_id is not None


@dataclass
class PipelineContext:
    """Per-intent inputs the optional reviewers need (built by the Engine or CLI)."""

    market: Any = None
    recent_sales: list[JournalEntry] = field(default_factory=list)
    advisor_context: Any = None


class Pipeline:
    """coach → tax → advisor → risk → submit → journal (SPEC §4)."""

    def __init__(self, journal: Journal, gate: RiskGate, *, coach=None, tax=None, advisor=None,
                 on_submitted: Callable[[str, JournalEntry], None] | None = None):
        self.journal = journal
        self.gate = gate
        self.coach = coach
        self.tax = tax
        self.advisor = advisor
        self.on_submitted = on_submitted

    def run(self, intent: OrderIntent, snap: PortfolioSnapshot, executor: Any,
            ctx: PipelineContext | None = None) -> Outcome:
        ctx = ctx or PipelineContext()
        reviews: list[ReviewResult] = []
        final: Verdict | None = None
        order_id: str | None = None
        qty = float(intent.quantity)
        try:
            if self.coach is not None:
                r = self.coach.review(intent, snap, ctx.market)
                reviews.append(r)
                if r.verdict is Verdict.DELAY:
                    final = Verdict.DELAY
            if final is None and self.tax is not None:
                r = self.tax.review(intent, snap, ctx.recent_sales)
                reviews.append(r)
                if r.verdict is Verdict.BLOCK:
                    final = Verdict.BLOCK
            if final is None and self.advisor is not None:
                r = self.advisor.review(intent, ctx.advisor_context)
                reviews.append(r)
                if r.verdict is Verdict.VETO:
                    final = Verdict.VETO
            if final is None:
                # RiskGate always runs last and has the final say.
                r = self.gate.check(intent, snap)
                reviews.append(r)
                if r.verdict is Verdict.BLOCK:
                    final = Verdict.BLOCK
                elif r.verdict is Verdict.RESIZE:
                    if r.adjusted_quantity is None or not (0 < r.adjusted_quantity < qty):
                        final = Verdict.BLOCK  # R10 violated: never trust it
                    else:
                        qty = float(r.adjusted_quantity)
                        final = Verdict.RESIZE
                elif r.verdict is Verdict.APPROVE:
                    final = Verdict.APPROVE
                else:  # pragma: no cover - gate only returns the three verdicts
                    final = Verdict.BLOCK
            if final in (Verdict.APPROVE, Verdict.RESIZE):
                order_id = bridge.submit(executor, intent.symbol, Side(intent.side).value, qty,
                                         reason=clip_explanation(intent.reason))
        except Exception as exc:  # anything unexpected → nothing submitted, journal says why
            order_id = None
            final = Verdict.BLOCK
            reviews.append(ReviewResult("pipeline", Verdict.BLOCK,
                                        clip_explanation(f"Not submitted: {type(exc).__name__}: {exc}")))
        if final in (Verdict.APPROVE, Verdict.RESIZE) and order_id is None:  # pragma: no cover
            final = Verdict.BLOCK
        entry = JournalEntry(
            entry_id=new_entry_id(), ts=intent.created_at, intent=intent, reviews=reviews, final=final,
            fill_price=None, fill_qty=None, shadow_return_20d=None,
        )
        self.journal.record(entry)  # exactly one entry per intent (J2)
        if order_id is not None and self.on_submitted is not None:
            self.on_submitted(order_id, entry)
        return Outcome(entry, order_id, qty if order_id else 0.0)


# --- engine -----------------------------------------------------------------------------


def week_start(ts: datetime) -> datetime:
    d = ts - timedelta(days=ts.weekday())
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class CycleReport:
    as_of: datetime
    outcomes: list[Outcome]
    alerts: list[str]
    notes: list[str]


class Engine:
    def __init__(self, config: LumiSafeConfig, journal: Journal, state: StateStore, *,
                 mode: str | None = None, fractional: bool = True, coach=None, tax=None, advisor=None,
                 satellite=None, context_builder: Callable[..., PipelineContext] | None = None,
                 inbox: Inbox | None = None, pending: PendingStore | None = None,
                 history_fn: Callable[[str, datetime], Any] | None = None):
        self.config = config
        self.mode = mode or config.mode
        if self.mode not in ("backtest", "paper"):
            raise ValueError("LumiSafe engine runs only in backtest or paper mode")
        self.journal = journal
        self.state = state
        self.core = CoreIndexStrategy(config.core, fractional=fractional)
        self.gate = RiskGate(config.risk, self.core.symbols)
        self.kill_switch = KillSwitch(config.risk, state.get("kill_switch"))
        self.lots = LotBook.from_list(state.get("lots"))
        self.satellite = satellite
        self.context_builder = context_builder
        self.inbox = inbox
        self.pending = pending
        self.history_fn = history_fn
        self.advisor = advisor
        self.pipeline = Pipeline(journal, self.gate, coach=coach, tax=tax, advisor=advisor,
                                 on_submitted=self._remember_order)

    # -- bookkeeping ---------------------------------------------------------------------

    def _remember_order(self, order_id: str, entry: JournalEntry) -> None:
        pending = self.state.get("pending_orders", {})
        pending[order_id] = {
            "entry_id": entry.entry_id, "symbol": entry.intent.symbol,
            "side": Side(entry.intent.side).value, "source": Source(entry.intent.source).value,
            "strategy_name": entry.intent.strategy_name,
        }
        self.state.set("pending_orders", pending)

    def on_fill(self, order_id: str, price: float, qty: float, at: datetime) -> None:
        pending = self.state.get("pending_orders", {})
        info = pending.pop(order_id, None)
        if info is None:
            return
        self.state.set("pending_orders", pending)
        self.journal.update_fill(info["entry_id"], price, qty)
        if info["side"] == Side.BUY.value:
            self.lots.add(info["symbol"], qty, price, at)
        else:
            consumed = self.lots.consume(info["symbol"], qty, at, self.config.risk.min_holding_days,
                                         respect_holding_period=info["source"] != Source.CORE.value)
            if consumed:
                pnl = sum((price - lot.cost_basis) * q for lot, q in consumed)
                self.journal.record_event("realized", {"entry_id": info["entry_id"], "symbol": info["symbol"],
                                                       "pnl": pnl, "qty": qty}, ts=at)
        if info["source"] == Source.SATELLITE.value and self.satellite is not None:
            self.satellite.on_fill(info.get("strategy_name"), info["symbol"], info["side"], price, qty)
        self.state.set("lots", self.lots.to_list())
        self.state.save()

    def _reconcile_pending(self, executor: Any, at: datetime) -> None:
        """Paper: fills may land after the process exits; pick them up next cycle."""
        for oid in list(self.state.get("pending_orders", {})):
            try:
                status, price, qty = bridge.order_fill(executor, oid)
            except Exception:
                continue
            if status == "filled" and price is not None and qty:
                self.on_fill(oid, price, qty, at)
            elif status in ("canceled", "cancelled", "expired", "rejected"):
                pending = self.state.get("pending_orders", {})
                info = pending.pop(oid, None) or {}
                self.state.set("pending_orders", pending)
                self.journal.record_event("order_not_filled", {"order_id": oid, "entry_id": info.get("entry_id"),
                                                               "status": status}, ts=at)

    def trades_this_week(self, as_of: datetime) -> int:
        """Submitted satellite + user trades since Monday. Core trades are exempt from R5."""
        since = week_start(as_of)
        return sum(1 for e in self.journal.query(since=since, submitted=True) if e.intent.source is not Source.CORE)

    def symbols(self) -> set[str]:
        syms = set(self.core.symbols) | {l.symbol for l in self.lots.lots}
        if self.satellite is not None:
            syms |= self.satellite.symbols()
        return syms

    def build_snapshot(self, acct: bridge.AccountState) -> PortfolioSnapshot:
        st = self.state
        sat_equity = self.satellite.equity(acct) if self.satellite is not None else 0.0
        peak = max(float(st.get("peak_equity", 0.0)), acct.equity)
        today = acct.as_of.date().isoformat()
        if st.get("day") != today:
            st.set("day", today)
            st.set("satellite_day_start_equity", sat_equity)
        sat_peak = float(st.get("satellite_peak_equity", 0.0))
        if not self.kill_switch.satellite_latched:  # frozen while R7 is latched
            sat_peak = max(sat_peak, sat_equity)
        st.set("peak_equity", peak)
        st.set("satellite_peak_equity", sat_peak)
        return PortfolioSnapshot(
            as_of=acct.as_of, cash=acct.cash, equity=acct.equity, positions=dict(acct.positions),
            prices=dict(acct.prices), peak_equity=peak, satellite_equity=sat_equity,
            satellite_peak_equity=sat_peak,
            satellite_day_start_equity=float(st.get("satellite_day_start_equity", sat_equity)),
            trades_this_week=self.trades_this_week(acct.as_of), lots=list(self.lots.lots),
        )

    @staticmethod
    def after_submission(snap: PortfolioSnapshot, outcome: Outcome) -> PortfolioSnapshot:
        """Conservative local update so later intents in the same cycle see spent cash.

        Sale proceeds are not credited until the fill arrives.
        """
        if not outcome.submitted:
            return snap
        i = outcome.entry.intent
        positions = dict(snap.positions)
        cash = snap.cash
        if i.side is Side.BUY:
            cash -= outcome.executed_qty * snap.prices[i.symbol]
            positions[i.symbol] = positions.get(i.symbol, 0.0) + outcome.executed_qty
        else:
            positions[i.symbol] = positions.get(i.symbol, 0.0) - outcome.executed_qty
        trades = snap.trades_this_week + (0 if i.source is Source.CORE else 1)
        return replace(snap, cash=max(cash, 0.0), positions=positions, trades_this_week=trades)

    def context_for(self, intent: OrderIntent, snap: PortfolioSnapshot, executor: Any) -> PipelineContext:
        if self.context_builder is None:
            return self.default_context(intent, snap, executor)
        return self.context_builder(self, intent, snap, executor)

    def process(self, intents: list[OrderIntent], snap: PortfolioSnapshot, executor: Any,
                confirmed: frozenset[str] = frozenset()) -> list[Outcome]:
        outcomes = []
        self._confirmed = confirmed
        # Sells first, so a cycle never needs cash it doesn't have yet.
        for intent in sorted(intents, key=lambda i: 0 if i.side is Side.SELL else 1):
            outcome = self.pipeline.run(intent, snap, executor, self.context_for(intent, snap, executor))
            outcomes.append(outcome)
            if outcome.entry.final is Verdict.DELAY and self.pending is not None:
                release = next((r.release_at for r in outcome.entry.reviews if r.release_at), None)
                if release is not None:
                    self.pending.add(intent, release)
            snap = self.after_submission(snap, outcome)
        return outcomes

    # -- reviewer inputs -----------------------------------------------------------------------

    def realized_pnl(self, entry_id: str) -> float | None:
        for ev in self.journal.events("realized"):
            if ev.payload.get("entry_id") == entry_id:
                return float(ev.payload["pnl"])
        return None

    def user_intents_today(self, as_of: datetime) -> int:
        start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        return len(self.journal.query(source=Source.USER, since=start))

    def default_context(self, intent: OrderIntent, snap: PortfolioSnapshot, executor: Any) -> PipelineContext:
        recent_sales = self.journal.query(symbol=intent.symbol, side=Side.SELL, submitted=True,
                                          since=snap.as_of - timedelta(days=31))
        market = None
        if intent.source is Source.USER:
            closes = {}
            if self.history_fn is not None:
                try:
                    series = self.history_fn(intent.symbol, snap.as_of)
                    if series is not None:
                        closes[intent.symbol] = series
                except Exception:
                    closes = {}
            market = MarketContext(closes=closes, user_intents_today=self.user_intents_today(snap.as_of),
                                   confirmed_intent_ids=getattr(self, "_confirmed", frozenset()))
        advisor_context = None
        if self.advisor is not None and intent.source is not Source.CORE:
            from lumisafe.advisor.advisor import build_advisor_context

            advisor_context = build_advisor_context(intent, snap, self.journal, self.history_fn)
        return PipelineContext(market=market, recent_sales=recent_sales, advisor_context=advisor_context)

    # -- one cycle ---------------------------------------------------------------------------

    def cycle(self, executor: Any) -> CycleReport:
        queued = self.inbox.peek() if self.inbox is not None else []
        acct = bridge.account_state(executor, self.symbols() | {i.symbol for i, _ in queued})
        now = acct.as_of
        if not acct.is_backtesting:
            self._reconcile_pending(executor, now)
        last = self.state.get("last_cycle_date")
        first_day_of_month = last is None or last[:7] != now.date().isoformat()[:7]
        self.state.set("last_cycle_date", now.date().isoformat())

        self.lots.reconcile(acct.positions)
        snap = self.build_snapshot(acct)

        alerts = self.kill_switch.update(snap)
        for a in alerts:
            self.journal.record_event("alert", {"message": a}, ts=now)
        self.state.set("kill_switch", self.kill_switch.to_dict())
        self.journal.record_event(
            PAPER_CYCLE_EVENT,
            {"mode": self.mode, "satellite_enabled": bool(self.config.satellite.enabled),
             "equity": acct.equity, "cash": acct.cash},
            ts=now,
        )

        reserve = self.satellite.cash_reserve(acct) if self.satellite is not None else 0.0
        plan = self.core.plan(snap, investable_cash=max(0.0, acct.cash - reserve),
                              first_trading_day_of_month=first_day_of_month,
                              rebalance_pending=bool(self.state.get("rebalance_pending", False)))
        self.state.set("rebalance_pending", plan.rebalance_pending)
        intents = list(plan.intents)
        if self.satellite is not None:
            # Equity tracking, retirement and paper promotion run even while R7 is latched.
            self.satellite.housekeeping(snap)
            if not self.kill_switch.satellite_latched:
                intents += self.satellite.intents(snap, acct, executor)

        outcomes = self.process(intents, snap, executor)
        if queued:
            for o in outcomes:
                snap = self.after_submission(snap, o)
            snap = replace(snap, trades_this_week=self.trades_this_week(now))
            confirmed = frozenset(i.intent_id for i, c in queued if c)
            done = set()
            for user_intent, _ in queued:
                if self.journal.query(intent_id=user_intent.intent_id):
                    done.add(user_intent.intent_id)  # already processed by an interrupted cycle
                    continue
                out = self.process([user_intent], snap, executor, confirmed)
                outcomes += out
                snap = self.after_submission(snap, out[0])
                done.add(user_intent.intent_id)
            self.inbox.remove(done)
        if self.pending is not None:
            for p in self.pending.expire_old(now):
                self.journal.record_event("delayed_intent_expired", {"intent_id": p.intent.intent_id}, ts=now)
        self.state.set("lots", self.lots.to_list())
        self.state.set("last_snapshot", {
            "as_of": now.isoformat(), "cash": acct.cash, "equity": acct.equity,
            "positions": acct.positions, "prices": acct.prices, "mode": self.mode,
        })
        self.state.save()
        return CycleReport(now, outcomes, alerts, plan.notes)


# --- Lumibot Strategy wrapper ------------------------------------------------------------

_STRATEGY_CLASS = None


def build_engine(params: dict) -> Engine:
    config = LumiSafeConfig.model_validate(params["lumisafe_config"])
    journal = Journal(params["journal_path"])
    state = StateStore(params.get("state_path"))
    factory = params.get("engine_factory")
    if factory is not None:
        return factory(config, journal, state, params)
    mode = params.get("mode") or config.mode
    advisor = None
    if mode == "paper":
        from lumisafe.advisor.advisor import AIAdvisor, AnthropicLLMClient

        advisor = AIAdvisor(AnthropicLLMClient() if config.ai_enabled else None, enabled=config.ai_enabled)
    return assemble_engine(config, journal, state, mode=mode, fractional=params.get("fractional", True),
                           advisor=advisor)


def yahoo_closes(symbol: str, as_of: datetime):
    """Adjusted daily closes up to ``as_of`` (no look-ahead)."""
    hist = bridge.fetch_daily_history([symbol], None, as_of)
    df = hist.get(symbol)
    return None if df is None else df["close"]


def yahoo_history(symbols: list[str], as_of: datetime) -> dict:
    """Adjusted daily bars for ``symbols`` up to ``as_of`` (no look-ahead)."""
    return bridge.fetch_daily_history(list(symbols), None, as_of)


def build_satellite(config: LumiSafeConfig, journal: Journal, state: StateStore, history=None,
                    kill_switch=None, fractional: bool = True):
    from lumisafe.portfolio.satellite import SatelliteManager
    from lumisafe.promotion.registry import Registry
    from lumisafe.strategies import CATALOG

    if not config.satellite.enabled:
        return None
    return SatelliteManager(config.satellite, Registry(journal=journal), {n: cls() for n, cls in CATALOG.items()},
                            journal, state, history or yahoo_history, benchmark=config.benchmark,
                            fractional=fractional, kill_switch=kill_switch)


def assemble_engine(config: LumiSafeConfig, journal: Journal, state: StateStore, *, mode: str,
                    fractional: bool = True, history_fn=None, advisor=None, satellite=None,
                    satellite_history=None) -> Engine:
    """The production wiring: coach, tax guard, inbox/pending stores (paper), optional advisor/satellite."""
    core_symbols = set(config.core.etfs)
    engine = Engine(
        config, journal, state, mode=mode, fractional=fractional,
        coach=BehaviorCoach(core_symbols),
        advisor=advisor,
        satellite=satellite,
        inbox=Inbox(paths.inbox_path()) if mode == "paper" else None,
        pending=PendingStore(paths.pending_path()) if mode == "paper" else None,
        history_fn=history_fn if history_fn is not None else (yahoo_closes if mode == "paper" else None),
    )
    engine.pipeline.tax = TaxGuard(core_symbols, min_holding_days=config.risk.min_holding_days,
                                   realized_pnl=engine.realized_pnl)
    engine.tax = engine.pipeline.tax
    if engine.satellite is None and mode == "paper":
        engine.satellite = build_satellite(config, journal, state, satellite_history,
                                           kill_switch=engine.kill_switch, fractional=fractional)
    return engine


def get_strategy_class():
    """LumiSafeStrategy(lumibot Strategy), built lazily via the bridge."""
    global _STRATEGY_CLASS
    if _STRATEGY_CLASS is not None:
        return _STRATEGY_CLASS
    Base = bridge.strategy_base()

    class LumiSafeStrategy(Base):
        def initialize(self, parameters=None):
            self.sleeptime = "1D"
            self._lumisafe = build_engine(self.parameters)
            # Lumibot's one-shot live run skips on_trading_iteration while the market is
            # closed; the CLI reads this flag so it never claims a cycle that didn't happen.
            self.lumisafe_cycle_ran = False

        def on_trading_iteration(self):
            self.lumisafe_cycle_ran = True
            report = self._lumisafe.cycle(self)
            for alert in report.alerts:
                self.log_message(f"LumiSafe alert: {alert}")

        def on_filled_order(self, position, order, price, quantity, multiplier):
            self._lumisafe.on_fill(bridge.order_id(order), float(price), float(quantity), self.get_datetime())

    _STRATEGY_CLASS = LumiSafeStrategy
    return LumiSafeStrategy


def strategy_parameters(config: LumiSafeConfig, *, journal_path: Path, state_path: Path | None, mode: str,
                        fractional: bool = True, engine_factory=None) -> dict:
    params = {
        "lumisafe_config": config.model_dump(mode="json"),
        "journal_path": str(journal_path),
        "state_path": str(state_path) if state_path else None,
        "mode": mode,
        "fractional": fractional,
    }
    if engine_factory is not None:
        params["engine_factory"] = engine_factory
    return params


@dataclass(frozen=True)
class CoreBacktestResult:
    output: Any
    journal_path: Path
    out_dir: Path


def run_core_backtest(config: LumiSafeConfig, start: datetime, end: datetime, budget: float = 100_000.0,
                      out_dir: Path | None = None) -> CoreBacktestResult:
    """Core-only backtest through Lumibot's Yahoo backtester. Uses its own journal, never the paper one."""
    config = config.model_copy(update={"mode": "backtest", "satellite": config.satellite.model_copy(
        update={"enabled": False})})
    out_dir = Path(out_dir or tempfile.mkdtemp(prefix="core_bt_", dir=paths.reports_dir()))
    journal_path = out_dir / "backtest_journal.db"
    params = strategy_parameters(config, journal_path=journal_path, state_path=None, mode="backtest")
    output = bridge.run_backtest(get_strategy_class(), params, start, end, budget, config.benchmark, out_dir,
                                 symbols=sorted(config.core.etfs))
    return CoreBacktestResult(output, journal_path, out_dir)


def run_paper_cycle(config: LumiSafeConfig, engine_factory=None, run_once: bool = True) -> Any:
    """Paper trading against Alpaca PAPER, journaling to ~/.lumisafe/journal.db.

    ``run_once=True`` runs a single cycle and exits (cron-friendly); otherwise
    Lumibot's live loop keeps running daily cycles until interrupted.
    """
    if config.mode != "paper":
        raise ValueError("run_paper_cycle requires mode: paper")
    params = strategy_parameters(config, journal_path=paths.journal_path(), state_path=paths.state_path(),
                                 mode="paper", engine_factory=engine_factory)
    return bridge.run_paper(get_strategy_class(), params, run_once=run_once)
