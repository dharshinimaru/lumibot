"""`lumisafe` CLI (SPEC §9). Run as ``python -m lumisafe <command>``."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import click

from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import LumiSafeConfig, load_config, save_config
from lumisafe.journal import Journal
from lumisafe.reporting import benchmark as bm


# Indirection points so tests can run fully offline.
def history_provider(symbols, start=None, end=None):
    from lumisafe.adapters import lumibot_bridge as bridge

    return bridge.fetch_daily_history(symbols, start, end)


def paper_runner(config: LumiSafeConfig, run_once: bool = True):
    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.runner import run_paper_cycle

    bridge.load_dotenv_file(".env")
    return run_paper_cycle(config, run_once=run_once)


def _run_paper_or_exit(config: LumiSafeConfig, run_once: bool = True):
    """Run a paper cycle; a setup problem (missing or non-paper keys) is a plain message, not a traceback."""
    try:
        return paper_runner(config, run_once=run_once)
    except Exception as exc:
        from lumisafe.adapters.lumibot_bridge import PaperOnlyError

        if not isinstance(exc, PaperOnlyError):
            raise
        click.echo(f"Can't start paper trading: {exc}")
        click.echo("Add your Alpaca PAPER keys to a `.env` file in the folder you run lumisafe from:\n"
                   "  ALPACA_API_KEY=your-paper-key\n  ALPACA_API_SECRET=your-paper-secret\n  ALPACA_IS_PAPER=true")
        sys.exit(1)


MARKET_CLOSED_MSG = ("The market is closed right now, so no paper cycle ran and nothing was traded. "
                     "Run `lumisafe run-paper` on a weekday between 9:30 and 16:00 New York time, "
                     "or leave `lumisafe run-paper --loop` running.")


def _cycle_ran(result) -> bool:
    """False only when the paper run reports it skipped the cycle (market closed)."""
    return getattr(result, "lumisafe_cycle_ran", True) is not False


def _load_config_or_exit() -> LumiSafeConfig:
    path = paths.config_path()
    if not path.is_file():
        click.echo("No plan yet. Run `lumisafe init` first.")
        sys.exit(1)
    return load_config(path)


def _state() -> dict:
    from lumisafe.runner import StateStore

    return StateStore(paths.state_path()).data


@click.group()
def main():
    """LumiSafe: beginner mode for Lumibot (paper trading only)."""


# --- init ----------------------------------------------------------------------------------

@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing plan.")
def init(force):
    """Onboarding quiz → your plan, saved to ~/.lumisafe/config.yaml."""
    from lumisafe.onboarding.plan import build_plan
    from lumisafe.onboarding.quiz import run_quiz, score

    click.echo(DISCLAIMER + "\n")
    path = paths.config_path()
    if path.is_file() and not force:
        click.echo(f"A plan already exists at {path}. Use --force to replace it.")
        sys.exit(1)
    answers = run_quiz(input)
    plan = build_plan(score(answers), answers)
    save_config(plan.config, path)
    click.echo("\n" + plan.text)
    click.echo(f"\nSaved to {path}. Next: `lumisafe run-paper` to start paper trading.")


# --- status -------------------------------------------------------------------------------

@main.command()
def status():
    """Headline: you vs benchmark, holdings, halts."""
    config = _load_config_or_exit()
    journal = Journal()
    state = _state()
    equity = bm.equity_from_journal(journal, mode="paper")

    click.echo("LumiSafe status (paper)\n")
    if len(equity) >= 2:
        try:
            hist = history_provider([config.benchmark], equity.index[0] - timedelta(days=7), None)
            prices = hist.get(config.benchmark)
            cmp = bm.compare(equity, prices["close"], config.benchmark) if prices is not None else None
        except Exception:
            cmp = None
        if cmp is not None:
            click.echo(cmp.headline())
            click.echo(f"(since {cmp.start.date()}; same dates, same starting money)")
        else:
            click.echo(f"Your return: {bm.total_return(equity):+.2%} · {config.benchmark} comparison unavailable "
                       "(no benchmark data right now)")
    else:
        click.echo("Not enough history yet for a return comparison. Run `lumisafe run-paper` daily.")

    snap = state.get("last_snapshot")
    if snap:
        click.echo(f"\nAccount value ${snap['equity']:,.2f} · cash ${snap['cash']:,.2f} · as of {snap['as_of'][:10]}")
        positions, prices = snap.get("positions", {}), snap.get("prices", {})
        total = snap["equity"] or 1
        click.echo("\nHoldings vs target (core):")
        for sym, target in config.core.etfs.items():
            value = positions.get(sym, 0.0) * prices.get(sym, 0.0)
            click.echo(f"  {sym:<6} {value / total:>6.1%}  (target {target:.0%} of core)")
        others = sorted(set(positions) - set(config.core.etfs))
        for sym in others:
            value = positions[sym] * prices.get(sym, 0.0)
            click.echo(f"  {sym:<6} {value / total:>6.1%}")

    ks = state.get("kill_switch", {})
    click.echo("\nHalts:")
    click.echo(f"  Satellite drawdown halt: {'ON (run `lumisafe reset-halt satellite`)' if ks.get('satellite_latched') else 'off'}")
    click.echo(f"  Account drawdown alert:  {'ON (core stays invested; new non-core buys paused)' if ks.get('account_alert_active') else 'off'}")
    click.echo("\n" + DISCLAIMER)


# --- journal --------------------------------------------------------------------------------

@main.command("journal")
@click.option("--rejected", is_flag=True, help="Only show blocked, vetoed or delayed intents.")
@click.option("--limit", default=30, show_default=True)
def journal_cmd(rejected, limit):
    """Show decisions, taken and rejected."""
    j = Journal()
    entries = j.query(rejected=True, limit=limit) if rejected else j.query(limit=limit)
    if not entries:
        click.echo("No journal entries yet.")
        return
    for e in entries:
        why = e.reviews[-1].explanation if e.reviews else ""
        fill = f" @ ${e.fill_price:,.2f}" if e.fill_price else ""
        shadow = f" · 20d later: {e.shadow_return_20d:+.1%}" if e.shadow_return_20d is not None else ""
        click.echo(f"{e.ts:%Y-%m-%d} {e.intent.source.value:<9} {e.intent.side.value.upper():<4} "
                   f"{e.intent.quantity:g} {e.intent.symbol:<6} → {e.final.value.upper()}{fill}{shadow}")
        click.echo(f"    {why}  [{e.intent.intent_id[:8]}]")


# --- paper trading ------------------------------------------------------------------------------

@main.command("run-paper")
@click.option("--loop", is_flag=True, help="Keep running daily cycles instead of a single cycle.")
def run_paper(loop):
    """Run the plan against your Alpaca PAPER account (one cycle by default)."""
    config = _load_config_or_exit()
    if config.mode != "paper":
        click.echo("Config mode must be `paper`.")
        sys.exit(1)
    result = _run_paper_or_exit(config, run_once=not loop)
    run_shadow_job(Journal())
    if not loop and not _cycle_ran(result):
        click.echo(MARKET_CLOSED_MSG)
        return
    click.echo("Paper cycle complete. See `lumisafe status` and `lumisafe journal`.")


# --- user intents: propose / confirm ----------------------------------------------------------------

def _print_outcome(intent_id: str) -> None:
    entries = Journal().query(intent_id=intent_id)
    if not entries:
        click.echo("Queued. It will be reviewed on the next paper cycle (`lumisafe run-paper`).")
        return
    e = entries[0]
    for r in e.reviews:
        click.echo(f"  {r.reviewer:<8} {r.verdict.value.upper():<8} {r.explanation}")
    click.echo(f"Result: {e.final.value.upper()}")
    if e.final.value == "delay":
        release = next((r.release_at for r in e.reviews if r.release_at), None)
        when = f" after {release:%Y-%m-%d %H:%M %Z}" if release else ""
        click.echo(f"To go ahead anyway, run `lumisafe confirm {intent_id[:8]}`{when} (expires after 72h).")


def _queue_and_run(config: LumiSafeConfig, intent, confirmed: bool) -> None:
    from lumisafe.runner import Inbox

    Inbox(paths.inbox_path()).push(intent, confirmed=confirmed)
    result = _run_paper_or_exit(config, run_once=True)
    if not _cycle_ran(result):
        click.echo(MARKET_CLOSED_MSG.replace("no paper cycle ran and nothing was traded",
                                             "your trade wasn't reviewed yet"))
    _print_outcome(intent.intent_id)


@main.command()
@click.argument("side", type=click.Choice(["BUY", "SELL", "buy", "sell"]))
@click.argument("symbol")
@click.argument("quantity", type=float)
def propose(side, symbol, quantity):
    """Propose a trade (e.g. `propose BUY AAPL 3`). It goes through the full safety pipeline."""
    import uuid

    from lumisafe.types import OrderIntent, Side, Source

    config = _load_config_or_exit()
    if quantity <= 0:
        click.echo("Quantity must be positive.")
        sys.exit(1)
    intent = OrderIntent(
        intent_id=str(uuid.uuid4()), created_at=now_utc(), source=Source.USER, symbol=symbol.upper(),
        side=Side(side.lower()), quantity=quantity, reason="Your request via `lumisafe propose`.",
    )
    click.echo(f"Reviewing {side.upper()} {quantity:g} {symbol.upper()} [{intent.intent_id[:8]}]...")
    _queue_and_run(config, intent, confirmed=False)


@main.command()
@click.argument("intent_id")
def confirm(intent_id):
    """Confirm a delayed trade after its cooling-off period. It re-enters the pipeline from the start."""
    import uuid

    from lumisafe.coach.behavior import PendingStore
    from lumisafe.types import OrderIntent

    config = _load_config_or_exit()
    store = PendingStore(paths.pending_path())
    p = store.get(intent_id)
    if p is None:
        click.echo(f"No delayed trade matches {intent_id!r}.")
        sys.exit(1)
    if p.status != "pending":
        click.echo(f"That trade was already {p.status}.")
        sys.exit(1)
    now = now_utc()
    if now >= p.expires_at:
        store.set_status(p.intent.intent_id, "expired")
        Journal().record_event("delayed_intent_expired", {"intent_id": p.intent.intent_id}, ts=now)
        click.echo("That trade expired (delayed trades must be confirmed within 72 hours). Propose it again if needed.")
        sys.exit(1)
    if now < p.release_at:
        click.echo(f"Still cooling off. You can confirm after {p.release_at:%Y-%m-%d %H:%M %Z}.")
        sys.exit(1)
    orig = p.intent
    again = OrderIntent(
        intent_id=str(uuid.uuid4()), created_at=now, source=orig.source, symbol=orig.symbol, side=orig.side,
        quantity=orig.quantity, reason=f"Confirmed after cooling-off (original {orig.intent_id[:8]}): {orig.reason}",
        strategy_name=orig.strategy_name,
    )
    store.set_status(orig.intent_id, "confirmed")
    Journal().record_event("delayed_intent_confirmed", {"intent_id": orig.intent_id, "reentry": again.intent_id},
                           ts=now)
    click.echo(f"Confirmed. Re-checking {orig.side.value.upper()} {orig.quantity:g} {orig.symbol} from the start...")
    _queue_and_run(config, again, confirmed=True)


# --- strategies & promotion ------------------------------------------------------------------------

@main.command()
def strategies():
    """Registry: every satellite strategy with its state and key metrics."""
    from lumisafe.promotion.registry import Registry
    from lumisafe.strategies import CATALOG

    reg = Registry().all()
    for name in sorted(set(CATALOG) | set(reg)):
        rec = reg.get(name)
        if rec is None:
            click.echo(f"{name:<16} not backtested yet (run `lumisafe backtest {name}`)")
            continue
        m = rec.get("backtest_metrics") or rec.get("metrics") or {}
        bits = []
        if "oos_sharpe" in m:
            bits.append(f"OOS Sharpe {m['oos_sharpe']:.2f}")
        if "oos_cagr" in m:
            bits.append(f"OOS CAGR {m['oos_cagr']:+.1%}")
        if "oos_max_drawdown" in m:
            bits.append(f"max drawdown {m['oos_max_drawdown']:.1%}")
        click.echo(f"{name:<16} {rec['state']:<14} {' · '.join(bits)}")
        last = rec["history"][-1] if rec.get("history") else None
        if last and last.get("reasons"):
            click.echo(f"{'':<16} why: {'; '.join(last['reasons'])}")


@main.command()
@click.argument("strategy")
def backtest(strategy):
    """Run the walk-forward for STRATEGY, save the report, update the registry."""
    from lumisafe.promotion import backtest_and_promote
    from lumisafe.promotion.registry import Registry
    from lumisafe.strategies import CATALOG

    config = load_config(paths.config_path()) if paths.config_path().is_file() else None
    bench = config.benchmark if config else "SPY"
    if strategy not in CATALOG:
        click.echo(f"Unknown strategy {strategy!r}. Available: {', '.join(sorted(CATALOG))}")
        sys.exit(1)
    cls = CATALOG[strategy]
    symbols = sorted(set(cls().universe) | {bench})
    click.echo(f"Loading daily history for {', '.join(symbols)}...")
    history = history_provider(symbols, None, None)
    report, path, state = backtest_and_promote(strategy, cls, history, Registry(journal=Journal()), benchmark=bench,
                                               now=now_utc())
    click.echo(f"Walk-forward: {len(report.folds)} folds, {report.data_start} → {report.data_end} "
               f"({report.years:.1f} years), costs {report.cost_per_trade:.2%} per trade")
    click.echo(f"Out-of-sample: Sharpe {report.oos_sharpe:.2f} (benchmark {report.benchmark_oos_sharpe:.2f}) · "
               f"CAGR {report.oos_cagr:+.1%} · max drawdown {report.oos_max_drawdown:.1%} "
               f"(benchmark {report.benchmark_oos_max_drawdown:.1%})")
    click.echo(f"Matched the benchmark's risk-adjusted return in {report.folds_matching_benchmark:.0%} of folds.")
    click.echo(f"State: {state.value}")
    click.echo(f"Report saved to {path}")


# --- day-trading lab (paper only; owner-approved SPEC amendment, see DECISIONS.md) ---------------------

@main.group()
def daytrade():
    """Paper-only day-trading lab: download data, test it honestly against SPY, then paper-trade if it passes."""


def _market_data():
    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.adapters.alpaca_data import AlpacaMarketData

    bridge.load_dotenv_file(".env")
    return AlpacaMarketData()


@daytrade.command("fetch")
@click.option("--start", default="2016-01-04", show_default=True)
@click.option("--end", default="2025-12-31", show_default=True)
def daytrade_fetch(start, end):
    """Download minute prices, headlines and bid/ask quotes (cached; resumes where it stopped; ~2-3 hours)."""
    from datetime import date as _date

    from lumisafe.daytrade import dataset

    try:
        cands = dataset.build(_market_data(), _date.fromisoformat(start), _date.fromisoformat(end),
                              progress=click.echo)
    except RuntimeError as exc:
        click.echo(str(exc))
        sys.exit(1)
    click.echo(f"Done: {len(cands)} candidate trades-days. Next: `lumisafe daytrade backtest`.")


@daytrade.command("backtest")
@click.option("--start", default="2016-01-04", show_default=True)
@click.option("--end", default="2025-12-31", show_default=True)
def daytrade_backtest(start, end):
    """Test the lab on the downloaded years after every cost and tax, and record the verdict."""
    import json as _json
    from datetime import date as _date

    from lumisafe.daytrade.lab import run_backtest
    from lumisafe.daytrade.report import format_report
    from lumisafe.promotion.registry import Registry

    config = load_config(paths.config_path()) if paths.config_path().is_file() else None
    cfg = config.daytrade if config else LumiSafeConfig.model_fields["daytrade"].default
    try:
        report, path, state = run_backtest(_market_data(), cfg, Registry(journal=Journal()),
                                           start=_date.fromisoformat(start), end=_date.fromisoformat(end),
                                           now=now_utc(), progress=click.echo)
    except RuntimeError as exc:
        click.echo(str(exc))
        sys.exit(1)
    click.echo("\n" + format_report(_json.loads(path.read_text())))
    click.echo(f"\nState: {state} · report saved to {path}")
    click.echo("\n" + DISCLAIMER)


def _set_daytrade_enabled(on: bool) -> None:
    config = _load_config_or_exit()
    config.daytrade.enabled = on
    save_config(config, paths.config_path())


@daytrade.command("enable")
def daytrade_enable():
    """Turn the paper day-trading lab on (it still trades only if its test passed)."""
    _set_daytrade_enabled(True)
    click.echo("Day trading is ON for paper trading only. It trades only if `lumisafe daytrade report` says it "
               "passed; start it with `lumisafe daytrade run` during market hours.")


@daytrade.command("disable")
def daytrade_disable():
    """Turn the paper day-trading lab off."""
    _set_daytrade_enabled(False)
    click.echo("Day trading is OFF.")


def daytrade_runner(config, state, params):  # indirection for tests
    from lumisafe.runner import run_daytrade_paper

    from lumisafe.adapters import lumibot_bridge as bridge

    bridge.load_dotenv_file(".env")
    return run_daytrade_paper(config, state, params)


@daytrade.command("run")
def daytrade_run():
    """Paper day trading, one check per minute during market hours (Ctrl-C to stop)."""
    from lumisafe.daytrade import INTRADAY_STRATEGY
    from lumisafe.daytrade.lab import latest_report
    from lumisafe.daytrade.live import params_from_report
    from lumisafe.promotion.registry import Registry
    from lumisafe.runner import DayTradeNotAllowed, daytrade_allowed

    config = _load_config_or_exit()
    st = Registry().state(INTRADAY_STRATEGY)
    params = params_from_report(latest_report())
    why = daytrade_allowed(config, st.value if st else None, params)
    if why:
        click.echo(why)
        sys.exit(1)
    click.echo(f"Paper day trading with: {params.label()}. Ctrl-C to stop.")
    try:
        daytrade_runner(config, st.value, params)
    except DayTradeNotAllowed as exc:
        click.echo(str(exc))
        sys.exit(1)


@daytrade.command("status")
def daytrade_status():
    """Today's day trades: open positions, result so far, limits."""
    from lumisafe.daytrade import INTRADAY_STRATEGY
    from lumisafe.promotion.registry import Registry
    from lumisafe.runner import DAYTRADE_BOOK_KEY

    config = _load_config_or_exit()
    st = Registry().state(INTRADAY_STRATEGY)
    cfg = config.daytrade
    click.echo(f"Day-trading lab: {'ON' if cfg.enabled else 'OFF'} · strategy state "
               f"{st.value if st else 'not tested yet'} · slice {cfg.slice_fraction:.0%} of the account · "
               f"daily loss limit {cfg.daily_loss_halt:.0%} of the slice")
    book = _state().get(DAYTRADE_BOOK_KEY)
    if not book:
        click.echo("No day trades yet.")
    else:
        click.echo(f"{book['day']}: {book['trades']} trade(s) · result so far ${book['realized']:,.2f} (closed trades)"
                   + (" · STOPPED for the day (loss limit)" if book.get("halted") else ""))
        for sym, p in book.get("positions", {}).items():
            click.echo(f"  holding {p['shares']} {sym} from ${p['entry']:,.2f} · stop ${p['stop']:,.2f} · "
                       f"target ${p['target']:,.2f}")
        day_start = datetime.fromisoformat(book["day"]).replace(tzinfo=timezone.utc) - timedelta(hours=12)
        exits = Journal().events(kind="daytrade_exit", since=day_start)
        for e in exits:
            p = e.payload
            click.echo(f"  sold {p['qty']:g} {p['symbol']} at ${p['exit']:,.2f} (bought ${p['entry']:,.2f}) → "
                       f"${p['pnl']:+,.2f} after fees" + (f" [{p['note']}]" if p.get("note") else ""))
    click.echo("\n" + DISCLAIMER)


@daytrade.command("report")
def daytrade_report():
    """Show the last day-trading lab report."""
    import json as _json

    from lumisafe.daytrade.lab import latest_report
    from lumisafe.daytrade.report import format_report

    path = latest_report()
    if path is None:
        click.echo("No day-trading report yet. Run `lumisafe daytrade fetch`, then `lumisafe daytrade backtest`.")
        sys.exit(1)
    click.echo(format_report(_json.loads(path.read_text())))
    click.echo("\n" + DISCLAIMER)


# --- signal research engine (news + price signals, 5-20 day holds) ------------------------------------

@main.group()
def signals():
    """Research engine: test many news and price signals on real data and rank them after costs."""


@signals.command("fetch")
@click.option("--start", default="2016-01-01", show_default=True)
@click.option("--end", default="2025-12-31", show_default=True)
def signals_fetch(start, end):
    """Download every headline for the stock pool plus total-return prices (cached; resumable; ~2 hours)."""
    from datetime import date as _date

    from lumisafe.daytrade.universe import POOL
    from lumisafe.signals.fetch import fetch_all_news

    md = _market_data()
    a, b = _date.fromisoformat(start), _date.fromisoformat(end)
    md.daily_bars(list(POOL) + ["SPY"], a - timedelta(days=400), b, adjustment="all")
    n = fetch_all_news(md, a, b, progress=click.echo)
    click.echo(f"Done: {n} days of headlines. Next: `lumisafe signals study`.")


@signals.command("study")
def signals_study():
    """Run the pre-registered study, print the ranked results and record the verdict."""
    import json as _json

    from lumisafe.promotion import apply_backtest
    from lumisafe.promotion.registry import Registry
    from lumisafe.promotion.walk_forward import FoldResult, WalkForwardReport
    from lumisafe.signals.run import STRATEGY_NAME, format_study, run

    d = run(_market_data(), progress=click.echo)
    now = now_utc()
    out = paths.reports_dir() / f"signals_{now:%Y%m%d_%H%M%S}.json"
    out.write_text(_json.dumps(d, indent=2, default=str))
    wf_d = dict(d["walk_forward"])
    wf_d["folds"] = [FoldResult(**f) for f in wf_d["folds"]]
    state = apply_backtest(STRATEGY_NAME, WalkForwardReport(**wf_d), out, Registry(journal=Journal()), now)
    click.echo("\n" + format_study(d))
    click.echo(f"\nState: {state.value} · report saved to {out}")
    click.echo("\n" + DISCLAIMER)


def signals_live():  # indirection for tests
    from lumisafe.adapters.alpaca_data import AlpacaMarketData, LiveMarketData

    return LiveMarketData(_market_data())


@signals.command("today")
def signals_today():
    """What the signals that passed the study point to for the next market open (read-only)."""
    import json as _json

    from lumisafe.signals.today import format_today, scan

    files = sorted(paths.reports_dir().glob("signals_*.json"))
    if not files:
        click.echo("No signal study yet. Run `lumisafe signals fetch`, then `lumisafe signals study`.")
        sys.exit(1)
    d = _json.loads(files[-1].read_text())
    picks, pick_stats = d.get("current_picks", {}), d.get("current_stats", {})
    snap = _state().get("last_snapshot") or {}
    config = load_config(paths.config_path()) if paths.config_path().is_file() else None
    slice_value = (config.satellite.max_weight * snap["equity"]) if (config and snap.get("equity")) else None
    ideas, avoid = scan(signals_live(), picks, pick_stats, datetime.now(timezone.utc)) if picks else ([], [])
    click.echo(format_today(ideas, avoid, picks, slice_value))
    click.echo("\n" + DISCLAIMER)


@signals.command("report")
def signals_report():
    """Show the last signal study."""
    import json as _json

    from lumisafe.signals.run import format_study

    files = sorted(paths.reports_dir().glob("signals_*.json"))
    if not files:
        click.echo("No signal study yet. Run `lumisafe signals fetch`, then `lumisafe signals study`.")
        sys.exit(1)
    click.echo(format_study(_json.loads(files[-1].read_text())))
    click.echo("\n" + DISCLAIMER)


@main.command("reset-halt")
@click.argument("what", type=click.Choice(["satellite"]))
def reset_halt(what):
    """Manually reset the satellite drawdown halt (R7). Asks for typed confirmation."""
    from lumisafe.risk.kill_switch import RESET_CONFIRMATION, KillSwitch
    from lumisafe.runner import StateStore

    config = _load_config_or_exit()
    store = StateStore(paths.state_path())
    ks = KillSwitch(config.risk, store.get("kill_switch"))
    if not ks.satellite_latched:
        click.echo("The satellite is not halted.")
        return
    click.echo("The satellite was halted after a large drop. Resetting lets its strategies trade again.")
    typed = input(f'Type "{RESET_CONFIRMATION}" to confirm: ')
    if not ks.reset_satellite(typed):
        click.echo("Not reset.")
        sys.exit(1)
    store.set("kill_switch", ks.to_dict())
    store.set("satellite_peak_equity", 0.0)  # next cycle re-bases the peak at current satellite equity
    store.save()
    Journal().record_event("halt_reset", {"what": "satellite"}, ts=now_utc())
    click.echo("Satellite halt reset.")


# --- AI daily note + shadow returns ------------------------------------------------------------------

def advisor_factory(config: LumiSafeConfig):
    from lumisafe.adapters import lumibot_bridge as bridge
    from lumisafe.advisor.advisor import AIAdvisor, AnthropicLLMClient

    bridge.load_dotenv_file(".env")  # ANTHROPIC_API_KEY may live in .env; a standalone `today` must see it

    return AIAdvisor(AnthropicLLMClient() if config.ai_enabled else None, enabled=config.ai_enabled)


def _closes(symbol: str):
    hist = history_provider([symbol], None, None)
    df = hist.get(symbol)
    return None if df is None else df["close"]


def run_shadow_job(journal: Journal) -> int:
    from lumisafe.advisor.advisor import update_shadow_returns

    try:
        return update_shadow_returns(journal, _closes, now_utc())
    except Exception:
        return 0


def latest_closes(symbols) -> tuple[dict[str, float], str]:
    """Latest daily price per symbol (today's price so far while the market is open) and its date."""
    hist = history_provider(sorted(symbols), datetime.now(timezone.utc) - timedelta(days=14), None)
    prices, asof = {}, None
    for sym, df in hist.items():
        if df is not None and len(df):
            prices[sym] = float(df["close"].iloc[-1])
            asof = max(asof, df.index[-1]) if asof is not None else df.index[-1]
    return prices, (f"{asof:%b %-d}" if asof is not None else "unknown date")


@main.command("next")
@click.option("--amount", type=float, default=None,
              help="Dollars you're adding now (default: your plan's monthly amount).")
@click.option("--holdings", default=None, help='Shares you already own, e.g. "VTI=10,VXUS=4,BND=2".')
@click.option("--paper", "use_paper", is_flag=True, help="Use the paper account's holdings instead.")
@click.option("--whole-shares", is_flag=True, help="Your broker only sells whole shares.")
def next_buy(amount, holdings, use_paper, whole_shares):
    """What to buy, how much, and when. Read-only: it never places an order."""
    from lumisafe.portfolio.advice import parse_holdings, what_to_buy, when_text

    config = _load_config_or_exit()
    core = config.core
    if amount is None:
        amount = core.monthly_contribution_usd
        if amount <= 0:
            click.echo("Your plan invests a lump sum. Say how much with --amount, e.g. `lumisafe next --amount 500`.")
            sys.exit(1)
    try:
        held = parse_holdings(holdings)
    except ValueError as exc:
        click.echo(f"--holdings: {exc}")
        sys.exit(1)
    source = "the shares you entered" if holdings else "no current holdings (add --holdings to use yours)"
    if use_paper:
        snap = _state().get("last_snapshot")
        if not snap:
            click.echo("No paper account snapshot yet. Run `lumisafe run-paper` first, or use --holdings.")
            sys.exit(1)
        held, source = dict(snap.get("positions", {})), f"your paper account (as of {snap['as_of'][:10]})"
    try:
        prices, asof = latest_closes(core.etfs)
        advice = what_to_buy(core, held, prices, amount, fractional=not whole_shares)
    except Exception as exc:
        click.echo(f"Couldn't get prices right now ({exc}). Check your internet connection and try again.")
        sys.exit(1)

    mix = " · ".join(f"{w:.0%} {s}" for s, w in core.etfs.items())
    click.echo(f"What to buy next (your plan: {mix})")
    click.echo(f"Adding ${amount:,.2f} · starting from {source} · latest prices from {asof}\n")
    if not advice.lines:
        click.echo("  Nothing to buy with this amount.")
    for l in sorted(advice.lines, key=lambda l: (l.side != "sell", -l.dollars)):
        share_fmt = f"{l.shares:g}" if whole_shares else f"{l.shares:.4f}"
        click.echo(f"  {l.side.upper():<4} {l.symbol:<5} {share_fmt:>9} shares  ≈ ${l.dollars:>9,.2f}   "
                   f"(share of your funds {advice.weights_before[l.symbol]:.0%} → "
                   f"{advice.weights_after[l.symbol]:.0%}, target {advice.targets[l.symbol]:.0%})")
    if advice.leftover >= 0.01 and advice.lines:
        click.echo(f"  Left over: ${advice.leftover:,.2f} (keep it for next month)")
    click.echo("\nWhen: " + when_text(datetime.now(timezone.utc).date()))
    click.echo("Then do nothing until next month. Prices will go up and down; the plan doesn't react to that.")
    for n in advice.notes:
        click.echo("Note: " + n)
    click.echo("Use limit orders near the prices above, or market orders during market hours. "
               "Prices move, so share counts are approximate.")
    click.echo("\n" + DISCLAIMER)


@main.command()
def today():
    """A plain-English note about your portfolio today (AI, with an offline fallback)."""
    from lumisafe.types import PortfolioSnapshot

    config = _load_config_or_exit()
    journal = Journal()
    snap_d = _state().get("last_snapshot")
    if not snap_d:
        click.echo("No paper cycle has run yet. Run `lumisafe run-paper` first.")
        sys.exit(1)
    as_of = datetime.fromisoformat(snap_d["as_of"])
    snap = PortfolioSnapshot(as_of=as_of, cash=snap_d["cash"], equity=snap_d["equity"],
                             positions=snap_d.get("positions", {}), prices=snap_d.get("prices", {}),
                             peak_equity=snap_d["equity"], satellite_equity=0.0, satellite_peak_equity=0.0,
                             satellite_day_start_equity=0.0, trades_this_week=0)
    comparison = None
    equity = bm.equity_from_journal(journal, mode="paper")
    if len(equity) >= 2:
        try:
            prices = history_provider([config.benchmark], equity.index[0] - timedelta(days=7), None)
            cmp = bm.compare(equity, prices[config.benchmark]["close"], config.benchmark)
            comparison = cmp.headline() if cmp else None
        except Exception:
            comparison = None
    updated = run_shadow_job(journal)
    note = advisor_factory(config).daily_note(snap, journal.query(limit=10), comparison)
    click.echo(note)
    if updated:
        click.echo(f"\n(Updated the 20-day outcome for {updated} held-back trade(s); see `lumisafe journal --rejected`.)")
    click.echo("\n" + DISCLAIMER)


# --- live gate (human only) -------------------------------------------------------------------------

@main.command("go-live")
def go_live():
    """Human-only check of the four live-trading conditions (SPEC §10). Does not place orders."""
    from lumisafe.live_unlock import LiveUnlockError, unlock

    try:
        unlock(Journal())
    except LiveUnlockError as exc:
        click.echo(str(exc))
        sys.exit(1)
    click.echo("All four live-trading conditions are met.\n"
               "LumiSafe ships paper-only: connecting a live broker is a step you do yourself, "
               "following the README. Live mode may run only the core and LIVE_ELIGIBLE satellites.")
    click.echo(DISCLAIMER)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

