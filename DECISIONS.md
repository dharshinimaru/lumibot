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
