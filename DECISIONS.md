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

---

## Phase 3 — Core (2026-11-12)

### 2026-11-12 — Pipeline semantics
- `runner.Pipeline.run` is the only place `bridge.submit()` is called. It runs coach → tax → advisor → risk, submits only on APPROVE/RESIZE from the RiskGate, then writes exactly one journal entry (J2) **after** submission so the final verdict is known. Any exception (reviewer crash, broker error) → nothing submitted, final BLOCK, plus an extra `pipeline` review explaining why (fail closed).
- A RESIZE whose `adjusted_quantity` breaks R10 (None, ≤0, ≥ original) is treated as BLOCK. The pipeline doesn't trust the gate blindly.
- WARN from tax/advisor continues to the next stage. DELAY (coach), BLOCK (tax/risk) and VETO (advisor) stop the flow.
- Within a cycle, sells run before buys. After each submission the snapshot is updated locally (cash spent, position added, non-core trade counted), so later intents in the same cycle see it. Sale proceeds aren't credited until filled.
- The AST test enforces that only `runner.py` calls `bridge.submit` (direct, aliased, from-import, or via `getattr`), and that only the bridge calls Lumibot's `create_order`/`submit_order`.

### 2026-11-12 — Core contribution semantics
- **What:** §7.1 says "buys on the first trading day of each month with `monthly_contribution_usd`". Alpaca paper accounts can't receive deposits, and Lumibot backtests have a fixed budget.
- **Decision:** each first trading day of a month the core invests `min(monthly_contribution_usd, investable cash)`. **`monthly_contribution_usd: 0` means lump-sum mode**: invest all investable cash (used for backtests and for users who want to invest their whole balance). Investable cash = cash − satellite reserve − a 2% cushion.
- **Why a 2% cushion:** in the first 10-year run, fills landed at the next bar's price, above the decision price, and cash went slightly negative. On Alpaca a paper (margin) account would silently borrow. Orders under $10 are skipped as dust.
- Rebalance: on rebalance-check days, if drift after the contribution exceeds `rebalance_band`, the core sells overweight ETFs back to target. The next cycle (`rebalance_pending`) reinvests the proceeds into underweights.

### 2026-11-12 — Negative cash is an account state, not malformed input (gate change)
- **What:** in Phase 2 I counted `snap.cash < 0` as malformed (R11 → BLOCK). In the 10-year backtest that froze the account after a fill overshoot: even the rebalance **sells** that would restore cash were blocked.
- **Decision:** negative cash is no longer "malformed". BUYs are still blocked, now by R3 (`qty*price <= cash` fails for any positive buy). SELLs, which raise cash, can proceed. I changed my own Phase 2 test case from `cash=-5` to `cash=NaN` (still malformed) and added `test_r3_negative_cash_blocks_buys_but_allows_sells_that_repair_it`. No invariant got weaker: no order can make cash more negative.

### 2026-11-12 — Backtest costs and broker isolation
- Costs: Lumibot's `TradingSlippage` is an absolute price amount, so §8's "5 bps + 0.05% slippage" is charged as a single 0.10% `TradingFee(percent_fee=0.001)` on buys and sells.
- Lumibot's `credentials.py` auto-builds an Alpaca broker (and opens a websocket) from `.env` keys at import time. The bridge sets `LUMIBOT_LAZY_CREDENTIALS=true` (setdefault) and forces `IS_BACKTESTING=true` during backtests (restored afterwards), so a backtest never touches a broker.
- Lumibot bug noticed (not fixed, `lumibot/` is off limits): `YahooHelper.get_symbols_data` is defined twice and the second definition calls itself (infinite recursion). The bridge uses the per-symbol `get_symbol_data`. Worth reporting upstream.

### 2026-11-12 — Paper only; no live broker factory in the bridge
- `make_paper_broker()` always passes `PAPER: True` and refuses if `ALPACA_IS_PAPER` is set to a non-true value or keys are missing.
- **Deliberately not built:** a live-broker factory. §10 describes the human gate (built in Phase 1), but writing a `PAPER: False` broker config would be "writing live-trading configuration", which §3 rule 2 forbids. Wiring live trading after `live_unlock.unlock()` succeeds is left to the human owner. `lumisafe go-live` (Phase 4) only runs the four checks and reports the result.

### 2026-11-12 — Fills and state
- Backtests: fills arrive via `on_filled_order`. Paper: fills may arrive after the run-once process exits, so pending order IDs live in `~/.lumisafe/state.json` and are reconciled through `get_order()` on the next cycle. Canceled/expired orders are dropped and journaled as an `order_not_filled` event.
- `trades_this_week` (R5) counts submitted **satellite + user** trades since Monday 00:00 (account time zone). Core trades are exempt from R5, so they don't use up the user's weekly allowance either.
- The `paper_cycle` heartbeat is written on every cycle with the actual mode. Backtest journals live in their own file under `~/.lumisafe/reports/`, never in the paper journal.

---

## Phase 4 — Onboarding + reporting (2026-11-12)

### 2026-11-12 — Backtests trade on dividend-adjusted Yahoo bars via PandasDataBacktesting
- **What:** the first 10-year Balanced backtest trailed its blended benchmark by **2.22 pts/yr**, outside §15's 1.5. Diagnosis: cash stayed ~0% of equity and there were no monthly reinvestment buys, so Lumibot 4.6.54's `YahooDataBacktesting` (default `auto_adjust=False`) prices raw closes **and credits no dividend cash**. The ~2%/yr gap equals the portfolio's dividend yield.
- Tried `auto_adjust=True`: Lumibot's `YahooHelper.get_symbol_data(auto_adjust=True)` keeps only `Close` (Open/High/Low dropped), so the backtester got no prices and placed **zero** orders. Second Lumibot bug, not fixed here.
- **Decision:** `bridge._adjusted_ohlcv()` takes Yahoo's raw OHLC and scales it by Yahoo's own `Adj Close / Close` factor (standard total-return adjustment of real data). The bridge backtests through Lumibot's `PandasDataBacktesting` with those bars. No bars are synthesized or forward-filled; missing days stay missing (Lumibot RULE #1). Result: core CAGR 10.74% vs monthly-rebalanced blend 10.44% → **+0.29 pts/yr**. The §15 test asserts `|gap| <= 1.5` unchanged.
- **Alternatives:** credit dividends inside the LumiSafe strategy (duplicates broker logic and risks double counting if Lumibot ever starts paying them), or loosen the tolerance (forbidden).
- Both Lumibot issues (Yahoo backtest pays no dividends with raw closes; `auto_adjust=True` drops OHLC) plus the `get_symbols_data` recursion are worth reporting upstream. `lumibot/` is untouched.

### 2026-11-12 — Quiz
- 7 questions (≤ 8): the six from §6 plus "invest your whole current balance now?" (yes → `monthly_contribution_usd: 0`, lump-sum mode; see Phase 3).
- Q3 is asked as a choice: sell at $900 / $800 / $700 / $600-or-hold → loss tolerance 10/20/30/40%.
- Scoring: horizon < 3 years or goal "save in < 3 years" → Conservative, satellite 0 (§6). Otherwise Growth if tolerance ≥ 30% and horizon ≥ 10, Balanced if tolerance ≥ 20% and horizon ≥ 5, else Conservative. These thresholds are my judgment; §6 gives the profiles but not the cut-offs.
- No emergency fund → the plan recommends building one first; paper mode is still allowed (§6).
- The satellite is enabled only if the user wants to experiment AND the profile allows > 0%.

### 2026-11-12 — Satellite `allowed_states` in paper mode
- The `SatelliteConfig` default is `["LIVE_ELIGIBLE"]`. But a strategy can only reach LIVE_ELIGIBLE after 60 trading days in PAPER (§8), so with the default, nothing could ever be paper-tested. Onboarding writes `allowed_states: ["PAPER", "LIVE_ELIGIBLE"]` for paper mode. P1 still holds: DRAFT/BACKTESTED/REJECTED/RETIRED never run. Live mode is restricted to LIVE_ELIGIBLE by `LiveAuthorization`.

### 2026-11-12 — Status / benchmark
- The headline is exactly "Your return: X% · If you'd just held SPY: Y%". X = equity change between the first and latest paper heartbeat; Y = buy-and-hold of the benchmark over the same dates with the same starting money. A simple (not time-weighted) return is fine because Alpaca paper accounts can't take deposits. Revisit if live deposits ever exist.
- Heartbeat payload now also carries `equity` and `cash`. `live_unlock` only reads `mode`/`satellite_enabled`, so the frozen file is unaffected. The engine also saves a `last_snapshot` (positions/prices/equity) to `state.json`, so `status` works offline. If benchmark data can't be fetched, status says the comparison is unavailable instead of failing.
- No win-rate metric anywhere (a test greps for it).

### 2026-11-12 — CLI additions and safety
- Added `lumisafe run-paper [--loop]` (not in §9's list): there must be a command that actually runs the plan on Alpaca paper. Default is one cycle (cron-friendly); `--loop` runs Lumibot's daily loop.
- `lumisafe go-live` only runs `live_unlock.unlock()` and reports the result; it never connects a live broker (Phase 3 decision). Per §3 rule 2 the agent never runs it, and tests only assert that the command exists.
- Integration tests are opt-in: `tests/lumisafe/conftest.py` skips `integration`-marked tests unless `-m` mentions `integration`. A plain `pytest` run had executed them and placed a second ~$50 **paper** order. Paper only, but unintended, so it can't happen again.

---

## Phase 5 — Coach + tax (2026-11-12)

### 2026-11-12 — `MarketContext` (not specified in §5.6)
- `MarketContext(closes, user_intents_today, confirmed_intent_ids)`. `closes` are adjusted daily closes ending at the snapshot time (paper: Yahoo via the bridge; never read in backtests because only USER intents need them). The coach reads time only from `snap.as_of`.
- "20 trading days" = the last 21 closes. B1 threshold ≤ −10%, B2 ≥ +25%, B3 = 3rd+ user intent today, counted from journal USER entries since local midnight (any verdict, so delayed and blocked ideas also count).

### 2026-11-12 — B1 recovery context
- Computed only from the symbol's own history: every earlier 20-day drop at least as deep as today's (or −10%, whichever is deeper), de-duplicated so one episode counts once, with the return 252 trading days later. The explanation reports the count, how many were higher a year later, and the median. With too little history, or no earlier comparable drop, it says exactly that. Nothing is invented.
- The release time comes before the context in the text, so trimming to 300 chars cuts the context, never the action.

### 2026-11-12 — B4 confirmation creates a linked re-entry intent
- **What:** J2 says one entry per intent, and the original intent already has its DELAY entry. B4 says a confirmed intent re-enters the pipeline from the start.
- **Decision:** `lumisafe confirm <id>` creates a NEW `OrderIntent` (new id; same symbol/side/qty; reason names the original id) and records a `delayed_intent_confirmed` event linking both ids. The re-entry carries `confirmed_intent_ids` so the coach doesn't delay the same decision again. Tax and risk review it normally. Confirmation is allowed from `release_at` (24h) up to `created_at + 72h`, after which the pending item is marked expired (also swept every paper cycle) and a `delayed_intent_expired` event is journaled.
- Pending delayed intents live in `~/.lumisafe/pending.json`, a mutable status store. The journal keeps the immutable DELAY record.

### 2026-11-12 — One submission path for user intents
- `lumisafe propose` doesn't talk to the broker itself. It queues the intent in `~/.lumisafe/inbox.json` and runs one paper cycle, and the engine processes the inbox through the same `Pipeline` as core intents (after the core's own intents, so DCA keeps priority for cash). Only `runner.py` ever submits.
- Queued symbols are priced in that cycle's snapshot. The first propose test caught that the snapshot only priced core symbols, which made the gate block every user trade with "no valid price".
- Inbox items are removed only **after** they're journaled, and an item already in the journal is skipped, so an interrupted cycle can neither lose (J2) nor double-process an intent.

### 2026-11-12 — TaxGuard details
- **T1 "sold at a loss"** needs the sale's cost basis, which `JournalEntry` doesn't carry. On every SELL fill the engine journals a `realized` event `{entry_id, pnl}` computed from the T3 lots consumed. `TaxGuard` takes a `realized_pnl(entry_id)` lookup. If no event exists, it falls back to comparing the sale's fill price with the average cost of lots still held; if neither is known, T1 doesn't fire (it can't prove a loss). Window: sale timestamp within 30 calendar days before `snap.as_of`.
- **T2:** "non-core SELL" means `source != CORE`. Gain counts only T3-selected lots held < 365 calendar days with price > cost. Extra tax = gain × (short-term − long-term placeholder rate), defaults 32% / 15%, configurable in the constructor. The text names the date the lots turn long-term.
- **T3:** `select_lots` (shared with the engine's lot consumption) picks highest cost first and skips lots younger than `min_holding_days` for non-core sells (R9). Core rebalance sells may use any lot.
- **T4:** every explanation is clipped to fit and then ends with " Not tax advice." (always ≤ 300 chars).

---

## Phase 6 — Promotion (2026-11-12)

### 2026-11-12 — Walk-forward is vectorized on Lumibot-sourced data (deviation from "using Lumibot backtesting")
- **What:** §4/§8 say the walk-forward uses Lumibot backtesting.
- **Decision:** `promotion/walk_forward.py` simulates the sleeve on daily bars with numpy. The bars are the same dividend-adjusted Yahoo data the bridge feeds Lumibot's `PandasDataBacktesting` for the core backtest. Same cost model (5 bps + 0.05% on traded notional).
- **Why:** parameter selection needs, per evaluation, folds × grid = 8 × 9 = 72 event-driven backtests plus the OOS runs. Through Lumibot that's minutes per `lumisafe backtest` and very slow unit tests. And a strategy-only Lumibot backtest would need a second order path outside `runner.py` (or would mix RiskGate effects into the strategy's own metrics). Look-ahead safety doesn't depend on the engine: the strategy only ever receives `as_of`, trades at that day's close, and earns from the next day; S1 is tested directly and a "scramble the test window" test proves parameter choice uses train data only.
- **Alternative if the owner wants it:** run the final chosen-parameter OOS windows through Lumibot as a parity check. Not done; noted as a follow-up.

### 2026-11-12 — Walk-forward design
- Rolling folds: train 4 years, test the next 2, step 2 years; the first train window starts 2006-01-01 (earlier data is warm-up for the 200-day MA / 12-month lookback). Today's data gives **8 folds over 20 years** (≥ 4 folds / ≥ 8 years enforced; fewer raises).
- Per fold, the grid (lookback 3/6/12 months × top 1/2/3) is ranked by train-window Sharpe; the winner is evaluated on the test window. Combined OOS = concatenated test windows. The benchmark (config `benchmark`, default SPY) is buy-and-hold over the same windows.
- "Beats or matches benchmark risk-adjusted return (Sharpe) in ≥ 50% of folds" is implemented as test Sharpe ≥ benchmark Sharpe per fold. The field is `folds_matching_benchmark` (deliberately not called a "win rate").
- **Result (real data, saved to `~/.lumisafe/reports/` and copied to `reports/lumisafe/walk_forward_trend_filter_20261112_172752.json`):** OOS Sharpe 0.59 (SPY 0.84), CAGR +8.0%, max drawdown 30.6% (SPY 33.7%), matched SPY's Sharpe in 3/8 folds (38%) → **REJECTED**. That's the honest outcome; §15 accepts whichever state it earns.

### 2026-11-12 — State machine details
- Allowed edges: DRAFT→{BACKTESTED, RETIRED}; BACKTESTED→{PAPER, REJECTED, RETIRED}; PAPER→{LIVE_ELIGIBLE, REJECTED, RETIRED}; LIVE_ELIGIBLE→{RETIRED}; REJECTED→{RETIRED}; RETIRED is terminal. Anything else raises `TransitionError`. Every edge is tested, and every non-edge is tested to be refused.
- Every transition is journaled as an append-only `promotion` event with metrics and reasons (§8).
- Re-running `lumisafe backtest` on a REJECTED/PAPER/LIVE_ELIGIBLE strategy saves a fresh report but does not change state. There's no way back from REJECTED except a new strategy version.
- PAPER → LIVE_ELIGIBLE (checked every paper cycle): ≥ 60 distinct trading days of satellite-equity heartbeats since `paper_started_at`, zero intents by that strategy that the RiskGate blocked **or resized** (a resize means it tried to break a limit), paper return ≥ 50% of the backtest OOS CAGR scaled to the paper period, and no submitted order older than 3 days with neither a fill nor an `order_not_filled` event. Once 60 days pass without meeting the bar → REJECTED. LIVE_ELIGIBLE only means a human may enable it; nothing switches to live.
- Auto-retirement (every cycle, any active state): the strategy's 126-trading-day (~6 months) sleeve return trails the benchmark's by > 5 points, or R7 fired ≥ 2 times in the last 365 days (from `KillSwitch.r7_triggers`). These checks keep running while the satellite is halted.

### 2026-11-12 — Satellite sleeve accounting
- Each active strategy gets a ledger (cash + positions) seeded once with `max_weight × account equity`, split across active strategies. Fills update the ledger. Satellite equity = ledger cash + market value of ledger positions, which is what R6/R7 and retirement measure. The ledger cash is reserved: the core never invests it.
- Rebalances are sized to the ledger's value, capped at `max_weight × account equity`, once per month (or ISO week). Orders under $10 are skipped. Satellite intents go through the full pipeline (coach passes them; tax; advisor in Phase 7; RiskGate).
- `reset-halt satellite` needs the typed phrase `RESET SATELLITE`. It clears the latch, keeps the trigger history (it counts toward retirement), and re-bases the satellite peak on the next cycle.

---

## Phase 7 — Advisor (2026-11-12)

### 2026-11-12 — LLM client and model
- `AnthropicLLMClient` (behind the `LLMClient.complete(system, user, *, json_schema, timeout)` interface; see the Phase 0 decision on not using `self.agents`). Model `claude-opus-5-5` (current default) at `effort: low`: the advisor makes a short approve/veto judgment, not deep analysis. Reviews request structured JSON through `output_config.format` (JSON Schema with `verdict ∈ {approve, veto}`); the advisor still validates with a strict pydantic model (`extra="forbid"`), so a malformed reply can never pass (A3).
- **No `tools` parameter is ever sent** (A1). The advisor package imports nothing from adapters/runner/risk/config/lumibot (AST test).
- **Server-side refusal fallback is on** (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`): if the model declines on a safety classifier, the API retries on a fallback model in the same call. A refusal that survives the fallback raises → "advisor unavailable" (A3). The owner can remove it in `AnthropicLLMClient.complete`.
- Timeout: the SDK request timeout plus an independent 30 s thread-join guard in `_call_with_timeout`, so a hung client can't stall the pipeline. Satellite → VETO "advisor unavailable"; user → WARN and continue.
- `ai_enabled: false` → user/satellite intents get APPROVE "AI advisor is turned off" (a user choice, not an outage), and `today` uses the deterministic offline note.

### 2026-11-12 — Context (A4)
- `AdvisorContext` = trade (side/symbol/qty/source/stated reason), up to 20 recent daily closes, current position shares and account weight, the last 10 journal decisions on that symbol, and optional headlines (none are fetched yet; the field exists for later). No account IDs, keys, cash or broker data. A test sets fake secrets in the environment and asserts none reach any prompt.
- Every system prompt lives in `advisor/prompts.py`. A test asserts no other lumisafe module contains prompt text.

### 2026-11-12 — Shadow returns (A5)
- `update_shadow_returns` fills `shadow_return_20d` for VETO **and BLOCK** entries (all held-back trades, not only AI vetoes, so the dashboard can compare advisor vetoes with gate blocks) once 20 trading days have passed: the price change from the first close on/after the decision date to 20 trading days later. Positive means the price rose afterwards (for a vetoed BUY that's a missed gain; for a vetoed SELL, a loss avoided). The journal trigger lets it be written once.
- It runs after `lumisafe run-paper` and `lumisafe today` (CLI side, so backtests never fetch data for it).

### 2026-11-12 — Wiring and verification
- Paper engines built by `build_engine` get the advisor; backtests don't (core-only, and A2 means it would never be called).
- Done criterion "daily note generated in paper mode": the paper integration test now also runs `lumisafe today` after a real Alpaca paper cycle. Its first run produced the offline note because that test's config had `ai_enabled: False` (since set to `True`). A direct `daily_note` call against the real API then produced a Claude-written note from paper-account facts.
