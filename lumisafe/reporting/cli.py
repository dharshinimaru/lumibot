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
    paper_runner(config, run_once=not loop)
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
    paper_runner(config, run_once=True)
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

