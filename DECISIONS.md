# LumiSafe — Decisions Log

Every assumption, deviation and ambiguity resolution made while implementing
`SPEC.md`, one dated entry each: what, why, alternatives considered (SPEC §3 rule 7).

---

## Phase 0 — Recon (2026-11-12)

### Environment
- Lumibot version: **4.6.54** (`setup.py`), imported from the repo checkout (`.venv`, Python **3.11.9**).
- Installed: pydantic 2.14.3, pandas 2.3.3, numpy 2.4.6, PyYAML 6.0.3, click 8.5.1,
  streamlit 1.67.0, hypothesis 6.170.15, pytest 9.2.0, anthropic 2.8.0, alpaca-py 0.45.0, yfinance 1.7.0.

### Lumibot API signatures LumiSafe uses (all through `lumisafe/adapters/lumibot_bridge.py`)
| Purpose | Real signature (lumibot 4.6.54) |
|---|---|
| Strategy base | `lumibot.strategies.Strategy(broker=None, data_source=None, ..., sleeptime="1M", benchmark_asset="SPY", name=None, budget=None, parameters={}, buy_trading_fees=[], sell_trading_fees=[], ...)` |
| Lifecycle | `initialize(self, parameters: dict = None)`, `on_trading_iteration(self)`, `before_market_opens(self)`, `on_filled_order(self, position, order, price, quantity, multiplier)` |
| Sleeptime | `self.sleeptime` property; `"1D"` for daily iteration |
| Orders | `create_order(asset: str|Asset, quantity: int|str|Decimal, side: str, limit_price=None, ..., time_in_force="gtc", ...)` then `submit_order(order, *, reason: str|None=None, evidence: dict|None=None, **kwargs)` |
| Account | `get_cash()`, `get_portfolio_value()`, `get_positions(include_cash_positions=False, ...)`, `get_position(asset, ...)` (Position has `.asset.symbol`, `.quantity`) |
| Prices | `get_last_price(asset, quote=None, exchange=None)`, `get_historical_prices(asset, length, timestep="", timeshift=None, ...)` → `Bars` with `.df` |
| Clock | `get_datetime(adjust_for_delay=False)` |
| Backtest | classmethod `Strategy.backtest(datasource_class=None, backtesting_start=None, backtesting_end=None, ..., budget=None, benchmark_asset="SPY", parameters={}, buy_trading_fees=[], sell_trading_fees=[], buy_trading_slippages=[], sell_trading_slippages=[], show_plot=None, save_tearsheet=True, show_tearsheet=None, ...)` and the lower-level `run_backtest(...)` (same args, returns results + strategy) |
| Data source | `lumibot.backtesting.YahooDataBacktesting` (lazy export) |
| Fees | `lumibot.entities.TradingFee(flat_fee=0.0, percent_fee=0.0, per_contract_fee=0.0, maker=True, taker=True)`; `TradingSlippage(amount)` is an **absolute** price amount |
| Paper broker | `lumibot.brokers.Alpaca(config)`, config keys `API_KEY`, `API_SECRET`, `PAPER`. Note `lumibot.credentials.ALPACA_CONFIG["PAPER"]` reads `ALPACA_IS_PAPER` from env |
| Live loop | `Trader(logfile="", backtest=False, ...)`, `trader.add_strategy(s)`, `trader.run_all(run_once=...)`; `run_once` is supported only for live/paper strategies |
| Agent runtime | `self.agents.create(*, name, system_prompt=None, model=None, tools=None, allow_trading=None, allow_network=None, include_builtin_tools=True, output_schema=None, run_timeout_seconds=None, ...)` → `AgentHandle.run(...)`. Bound to a running Strategy instance. |

### 2026-11-12 — CLAUDE.md contains an unrelated project spec
- **What:** the commit that added `SPEC.md` also appended a "CLAUDE.md — Mandate" section (an Auth0 × Stripe Next.js hackathon app) to `CLAUDE.md`.
- **Decision:** ignore it for LumiSafe. It describes a different product and stack (Next.js/TypeScript) and has no bearing on this spec. SPEC §3 also states its rules override everything else.
- **Also:** LumiBot's own CLAUDE.md requires working on a `version/X.Y.Z` branch. The owner put this work on the `lumisafe` branch; I stayed there and never touched `lumibot/`, so LumiBot release rules don't apply.

### 2026-11-12 — Runner subclasses Lumibot's Strategy through the bridge
- **What:** §4 says `runner.py` defines `LumiSafeStrategy(lumibot Strategy)`, while §3 rule 1 says only the bridge imports lumibot.
- **Decision:** the bridge re-exports `StrategyBase = lumibot.strategies.Strategy`, and `runner.py` subclasses `bridge.StrategyBase`. `runner.py` never has `import lumibot`. An AST test enforces that only the bridge imports lumibot.
- **Alternatives:** define the strategy class inside the bridge, which would put pipeline logic in the adapter (rejected).

### 2026-11-12 — AIAdvisor uses a direct Anthropic client, not `self.agents`
- **What:** §5.5 prefers Lumibot's agent runtime "if it fits".
- **Decision:** it doesn't fit. `self.agents` is bound to a running Strategy instance, but the advisor must also run from the CLI (`propose`, `today`) with no Strategy loop, and A1 needs a client with **no** tools at all. Lumibot's agent runtime exposes ~78 builtin tools by default and has `allow_trading` flags rather than a no-tool client. `AIAdvisor` takes an injected `llm_client` with a single `complete(system, user, timeout)` method. The production implementation wraps the Anthropic SDK (already installed) and sends no `tools` parameter.
- **Alternatives:** `self.agents.create(allow_trading=False, include_builtin_tools=False, ...)`. That works only inside a Strategy, and the Strategy loop isn't running during CLI calls.

### 2026-11-12 — Test harness
- Tests live in `tests/lumisafe/`. Their `conftest.py` gives each test its own `LUMISAFE_HOME` (tmp dir) and blocks sockets unless the test is marked `integration` (rule 6). The `integration` marker is registered there, so the repo-wide `setup.cfg` is untouched.
- `LUMISAFE_HOME` env var overrides `~/.lumisafe` (journal, config, registry, state). Added so tests and sandboxes never touch the real directory.
- Added `pytest-cov` (dev-only, for the §13 coverage targets) → `requirements_lumisafe.txt`. No runtime dependency added.
- Added `.github/workflows/lumisafe.yml` (unit tests + coverage + a `git diff lumibot/` empty check).

### 2026-11-12 — CLI entry point
- **What:** §9 shows a `lumisafe` command. Adding a console script would mean editing Lumibot's `setup.py`, which is Lumibot packaging.
- **Decision:** ship `python -m lumisafe ...` (`lumisafe/__main__.py`). README tells users to `alias lumisafe="python -m lumisafe"`.

---

## Phase 1 — Contracts (2026-11-12)

### 2026-11-12 — Config strictness beyond the contract
- `weights_sum` raises `ValueError` instead of using `assert`, which `python -O` would strip. It also rejects an empty or negative-weight core. Same contract, stronger.
- All config models use `extra="forbid"` and `validate_assignment=True`. So `cfg.risk.allow_leverage = True` and `cfg.mode = "live"` raise after construction, and unknown keys such as `allow_margin` are rejected rather than silently ignored. C1/C2 then hold for the object's whole lifetime, not just at construction.

### 2026-11-12 — Default ETF cap vs core weights
- **What:** §6 profiles hold up to 70% VTI in the core, but the `max_single_etf_weight` default is 0.60. With the defaults, R2 would resize every Growth-profile VTI purchase.
- **Decision:** the `RiskConfig` default stays 0.60 per contract. The plan generator (Phase 4) and `config/defaults.yaml` set `max_single_etf_weight = min(1.0, max(0.60, largest core weight + rebalance_band))`, which is 0.65 for Balanced and 0.75 for Growth, and is within the field's `le=1.0` bound.
- **Alternatives:** exempt core trades from R2. Rejected: §5.3 has no such exemption, and the cap remains a useful backstop against a corrupted target weight.

### 2026-11-12 — Journal storage details
- Aware datetimes are stored as UTC ISO strings so text ordering equals time ordering in range filters. LumiSafe uses tz-aware datetimes throughout.
- `intent_id` is `UNIQUE`, which enforces the "at most one" half of J2 in storage. The runner enforces the "at least one" half (tested in Phase 3).
- Triggers: DELETE is forbidden; UPDATE of any intent/review/verdict column is forbidden; `fill_price`/`fill_qty` and `shadow_return_20d` can go NULL→value once and never change again.
- **Added an append-only `events` table** (same DB, same triggers) for facts that are not order intents: promotion transitions (§8 "every transition is journaled"), halts, alerts, and per-cycle `paper_cycle` heartbeats. `JournalEntry` requires an `OrderIntent`, so these facts can't be entries without inventing fake intents.
- `query(**filters)` supports: symbol, source, side, final (single or list), intent_id, entry_id, since, until, rejected, submitted, limit. Unknown filters raise `TypeError`.

### 2026-11-12 — Live gate (`live_unlock.py`) interpretation
- **Condition 1:** `stdin.isatty()`; any exception counts as "not a TTY" (fail closed). The phrase prompt only runs on a TTY, so it can't be piped in.
- **Condition 2:** exact match after stripping only the trailing newline. Case and punctuation must match.
- **Condition 3:** `LUMISAFE_LIVE` must equal exactly `"1"` AND must not appear in the `.env` file (default: `./.env`). A dotenv loader such as Lumibot's could otherwise inject it, and §10 says "set by the human in their shell (not in .env)". An unreadable `.env` counts as "defined" (fail closed).
- **Condition 4:** a calendar month strictly before the current one where: `paper_cycle` heartbeats with `mode == "paper"` fall on ≥15 distinct days, the first by day 7 and the last on day 22 or later (covers the month end to end); every heartbeat had `satellite_enabled == False`; ≥1 CORE entry was submitted and filled; and zero SATELLITE entries were submitted. The thresholds are module constants. Backtest heartbeats never count.
- `LiveAuthorization` can only be built with a private mint key, so constructing it directly raises `PermissionError`. `is_authorized()` checks the key. It carries `allowed_sources = (core, satellite)` and `allowed_satellite_states = (LIVE_ELIGIBLE,)` from §10's "only CORE plus LIVE_ELIGIBLE satellites", so USER intents are not live-eligible. The runner must enforce these.
- Per §3 rule 2 this file is now frozen for the agent. Later phases only import from it.

---

## Phase 2 — Risk (2026-11-12)

### 2026-11-12 — ETF vs single stock classification
- **What:** R1 (stock cap 5%) and R2 (ETF cap 60%) need to know whether a symbol is an ETF. The snapshot doesn't carry asset types, and asking the broker would make the gate impure.
- **Decision:** `limits.KNOWN_ETFS` is a fixed list of common diversified US ETFs, and core symbols always count as ETFs. **Any unknown symbol is treated as a single stock**, so it gets the stricter cap. Misclassification can only make the gate stricter, never looser.
- **Alternatives:** an asset-type lookup through the bridge passed in via the snapshot (needs a contract change), or treating unknowns as ETFs (unsafe).

### 2026-11-12 — Rule order and scope details
- Order inside `check()`: C1 defense (forged config flags → BLOCK) → R11 validation → R7/R6 (satellite, both sides) → SELL: R4, R8 (core symbols, any source), R9 → BUY: R8 (satellite/user), R5, R1/R2 resize, R3.
- **R3 vs resize:** R3 says BLOCK, so an unaffordable order (after any R1/R2 resize) is blocked, not shrunk to fit the cash. The core strategy sizes its own buys to available cash.
- **R6/R7** block all satellite intents, buys and sells. That follows §5.3 literally, even though it means a halted satellite can't exit. Exiting is not a beginner-protecting action the spec asks for; a human can reset.
- **R8** blocks SELLs of core symbols from every source, including CORE rebalancing ("core holdings are never sold by the gate").
- **R9** counts weekdays with `numpy.busday_count`, ignoring exchange holidays, so the gate stays deterministic with no calendar I/O. Lots must be aged for a SELL to pass. Holdings not covered by any tracked lot (bought before LumiSafe ran) count as aged, since LumiSafe can only know lots it recorded.
- **Whole vs fractional:** if the intent quantity is a whole number, resizes stay whole; otherwise resizes are in 1e-6 steps.
- **Float exactness:** the cap search verifies with the same expression the invariant is written in, `(held + q) * price / equity <= cap`. Hypothesis found a case where checking `(held+q)*price <= cap*equity` instead let 0.05000000000000001 through.

### 2026-11-12 — R7 "until manual reset" with a pure gate
- **What:** the gate is pure and the snapshot has no "halt latched" field, so on its own the gate would un-halt the satellite once it recovered above the threshold.
- **Decision:** `risk/kill_switch.py` `KillSwitch` latches R7 (persisted in state), counts R7 triggers (for §8 auto-retirement), and raises the R8 alert once per drawdown episode. While latched, the runner (a) doesn't run the SatelliteManager and (b) keeps `satellite_peak_equity` frozen, so the gate also keeps blocking. Reset requires typing `RESET SATELLITE`, after which the runner re-bases the satellite peak to current equity.
- **Alternatives:** add a `satellite_halted` field to `PortfolioSnapshot` (a contract change needing owner approval).

### 2026-11-12 — Guard test for live_unlock.py
- Added `test_live_unlock_frozen.py`, which pins the SHA-256 of `live_unlock.py` as committed in Phase 1. The agent can't edit it accidentally without a red test. The owner may update both together.
