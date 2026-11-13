# LUMISAFE

Beginner mode for LumiBot: a guarded, paper-only, benchmark-first layer in the `lumisafe/` package.

- **Last Updated:** 2026-11-13
- **Status:** Implemented (SPEC.md phases 0-8); paper trading only
- **Audience:** contributors and AI agents working on `lumisafe/`

## Overview

LumiSafe is specified in [`SPEC.md`](../SPEC.md); every deviation and judgment call is logged in [`DECISIONS.md`](../DECISIONS.md). It never modifies `lumibot/`. All coupling to LumiBot lives in `lumisafe/adapters/lumibot_bridge.py`, the only module that imports `lumibot` (enforced by an AST test).

## Order flow (every order)

```
OrderIntent (core | satellite | user)
  → BehaviorCoach   (user only; may DELAY 24h: panic-sell, chasing, overtrading)
  → TaxGuard        (wash sale BLOCK/WARN, short-term gain WARN; "Not tax advice.")
  → AIAdvisor       (satellite/user only; APPROVE or VETO; fail-closed for satellite)
  → RiskGate        (pure, deterministic, final say: BLOCK or RESIZE down, never up)
  → bridge.submit   (called ONLY from runner.py; AST test)
  → Journal         (append-only SQLite; exactly one entry per intent)
```

## Key modules

| Module | Role |
|---|---|
| `runner.py` | `Pipeline` (the flow above), `Engine` (one cycle: snapshot → core/satellite/inbox intents → pipeline), `LumiSafeStrategy` (Lumibot `Strategy` subclass built via the bridge) |
| `risk/gate.py`, `risk/limits.py` | R1-R11; property-tested with hypothesis |
| `risk/kill_switch.py` | Latches the R7 satellite halt until `lumisafe reset-halt satellite` |
| `portfolio/core.py` | DCA into the most underweight ETF; rebalance sells only beyond the band |
| `portfolio/satellite.py` | Runs only registry states in `allowed_states` (P1); per-strategy ledgers; auto-retirement; paper promotion |
| `promotion/` | Walk-forward (≥ 4 folds, ≥ 8 years, costs 0.10%), criteria, JSON registry state machine |
| `journal/journal.py` | SQLite triggers forbid UPDATE/DELETE of decisions; fill/shadow fields set once; `events` table for heartbeats, alerts, promotions |
| `live_unlock.py` | Human-only live gate (four conditions). Frozen after Phase 1; a hash test pins it. The bridge has **no** live-broker factory. |

## Gotchas learned while building it

- **Dividends in backtests:** LumiBot 4.6.54's Yahoo backtest credits no dividend cash on raw closes (~2%/yr missing for an ETF mix), and `auto_adjust=True` drops Open/High/Low so no orders fill. LumiSafe backtests through `PandasDataBacktesting` with real Yahoo bars scaled by Yahoo's own `Adj Close / Close` factor. The 10-year Balanced core now tracks its blend within +0.29 pts/yr.
- **`YahooHelper.get_symbols_data` recurses forever** (defined twice). Use the per-symbol `get_symbol_data`.
- **Implicit brokers:** importing `lumibot.credentials` with Alpaca keys in `.env` builds a broker and opens a websocket. The bridge sets `LUMIBOT_LAZY_CREDENTIALS=true` and `IS_BACKTESTING=true` during backtests.
- **Fill price vs decision price:** orders fill at the next price, so the core keeps a 2% cash cushion. Negative cash blocks buys (R3) but never blocks repair sells.
- **Market closed = no cycle:** LumiBot's one-shot live run (`run_once`) silently skips `on_trading_iteration` while the market is closed. `LumiSafeStrategy` sets `lumisafe_cycle_ran` and the CLI prints a "market is closed, nothing traded" message instead of "Paper cycle complete".
- **macOS `CERTIFICATE_VERIFY_FAILED` on the Alpaca websocket:** python.org builds ship without CA certificates. REST calls (orders, account) still work and fills are reconciled next cycle; to silence it run `Install Certificates.command` from the Python folder, or `export SSL_CERT_FILE="$(python -m certifi)"`.
- **Paper fills arrive later:** pending order IDs persist in `~/.lumisafe/state.json` and are reconciled next cycle.

## State on disk (`~/.lumisafe/`, override with `LUMISAFE_HOME`)

`config.yaml` (plan) · `journal.db` (append-only) · `state.json` (peaks, lots, halts, pending orders, ledgers) · `registry.json` (strategy states) · `inbox.json` / `pending.json` (user intents) · `reports/` (walk-forward and backtest output).

## Tests

```bash
python -m pytest tests/lumisafe                       # unit (network blocked)
python -m pytest tests/lumisafe -m integration        # Yahoo + Alpaca PAPER (places small paper orders)
python -m pytest tests/lumisafe --cov=lumisafe --cov-report=term-missing
```
