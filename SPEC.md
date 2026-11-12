# LumiSafe — Beginner Mode for Lumibot

**Spec version:** 1.0 · **Owner:** Dharshini · **Implementer:** Claude Code (autonomous)
**Target:** a local fork/copy of Lumibot. All new code lives in a new package `lumisafe/` beside `lumibot/`.

---

## 0. How to use this file

1. Put `SPEC.md` and `CLAUDE.md` in the root of your Lumibot copy.
2. Create an Alpaca **paper** account and put the keys in `.env` (never commit it).
3. Start Claude Code in the repo root and paste the kickoff prompt from **§16**.
4. Claude Code works phase by phase (§14), commits after each phase, and logs every judgment call in `DECISIONS.md`.

---

## 1. Problem, scale, and why this solution

### Problem
People who know nothing about investing can't use Lumibot safely. It's a powerful framework with no guardrails: a beginner can write or copy a strategy that overtrades, uses leverage, concentrates in one stock, or panic-sells, and Lumibot will execute it faithfully.

Beginners mostly lose because of **behavior and costs**: trading too often, selling in drops, chasing hot stocks, leverage. Lack of predictions isn't the cause. A better predictor doesn't fix that, but a system that blocks those behaviors does.

### Scale
- A study of 19,646 Brazilian day traders over 300+ days found **97% lost money**, with no evidence of improvement with practice.
- Barber & Odean (66,465 US households): the most active traders earned **~7 percentage points/year less** than the least active.
- SEBI (India): **91%** of retail equity futures-and-options traders had net losses in FY25.
- DALBAR: even fund investors earned **16.54% vs the S&P 500's 25.02%** in 2024, an 8.48-point gap caused by buying and selling at the wrong times.

### Why this solution is the best way to do it
1. **It attacks the real cause.** Guardrails, a passive core and a behavior coach close the behavior gap, which is the largest *controllable* source of underperformance. No AI or hedge fund reliably predicts prices; every beginner can stop sabotaging their returns.
2. **Safety is enforced in code, not by the AI.** Risk rules are deterministic Python the AI can't edit or override, so a bad model output can't blow up an account.
3. **Honest by construction.** Every result is shown against "if you'd just held the S&P 500." Strategies that lose to that benchmark are retired automatically.
4. **Built on Lumibot, not from scratch.** It reuses Lumibot's broker integrations, backtester and backtest/paper/live parity instead of rebuilding execution infrastructure.

**Success metric:** across simulated beginner profiles, the user's net return is within 1.5 points/year of a buy-and-hold benchmark of the same risk level, with zero guardrail violations. LumiSafe does **not** promise profits.

---

## 2. Scope

### In scope
- Onboarding quiz that produces a risk profile and a plain-English plan
- Core-satellite portfolio: index-ETF core plus an optional, gated satellite of tested strategies
- Deterministic risk gate (hard limits, kill switches)
- Strategy promotion pipeline: walk-forward backtest, then paper, then live-eligible, with automatic retirement
- AI advisor that only explains and vetoes
- Behavior coach (cooling-off delays on emotional trades)
- Append-only decision journal
- Benchmark-first reporting (CLI plus a simple local dashboard)
- Tax guard (wash-sale and short-term-gain awareness)

### Out of scope (do not build)
- Options, futures, crypto, margin, short selling, leverage
- High-frequency or intraday strategies (minimum holding period: 5 trading days)
- Any "price prediction" model presented as a source of edge
- Managing other people's money, payments, user accounts or hosting
- Automatic live trading enablement (see §3, rule 2)

---

## 3. Operating rules for the implementing agent (Claude Code)

These rules override anything else in this spec.

1. **Never modify `lumibot/`.** Extend it only through `lumisafe/`. Put all coupling to Lumibot APIs in `lumisafe/adapters/lumibot_bridge.py`.
2. **Paper only.** Never set, write or enable live-trading configuration. Never run `lumisafe go-live`. Never edit `lumisafe/live_unlock.py` after Phase 1 creates it.
3. **Never commit secrets.** `.env` stays in `.gitignore`. Never print API keys to logs.
4. **Read before you write.** Before Phase 1, inspect the installed Lumibot version, its `Strategy` lifecycle, order API, backtesting entry points and the agent runtime (`self.agents`). Record the exact signatures you'll use in `DECISIONS.md`. If an API in this spec doesn't match the real one, adapt the bridge and log it. Don't change the contracts in §5.
5. **Tests gate progress.** Don't start phase N+1 until phase N's tests pass. Never delete or weaken a test to make it pass.
6. **No network in unit tests.** Mock brokers and LLMs. Only tests marked `integration` may touch Yahoo data or Alpaca paper.
7. **Log judgment calls.** Every assumption, deviation or ambiguity resolution gets one dated entry in `DECISIONS.md`: what, why, alternatives considered.
8. **Commit per phase**, message `lumisafe: phase N — <summary>`.
9. **Stop and report** instead of guessing if: a guardrail can't be enforced with the real Lumibot API, a test of a §5 invariant can't be made to pass, or something would require live trading.

---

## 4. Architecture

```
lumisafe/
├── __init__.py
├── config/
│   ├── schema.py            # pydantic models (§5.1)
│   └── defaults.yaml        # beginner defaults
├── types.py                 # dataclasses + enums (§5.2)
├── onboarding/
│   ├── quiz.py              # questions → RiskProfile
│   └── plan.py              # RiskProfile → Plan (plain English + allocations)
├── risk/
│   ├── gate.py              # RiskGate (§5.3) — deterministic, final say
│   ├── limits.py            # individual rule functions
│   └── kill_switch.py       # drawdown + daily-loss halts
├── portfolio/
│   ├── core.py              # CoreIndexStrategy: DCA + rebalance
│   └── satellite.py         # SatelliteManager: runs only promoted strategies
├── strategies/
│   ├── base.py              # SatelliteStrategy protocol (§5.4)
│   └── trend_filter.py      # reference satellite strategy (§7.2)
├── promotion/
│   ├── walk_forward.py      # walk-forward backtester using Lumibot backtesting
│   ├── registry.py          # strategy lifecycle state machine (§8)
│   └── criteria.py          # promotion/retirement thresholds
├── advisor/
│   ├── advisor.py           # AIAdvisor (§5.5) — explain + veto only
│   ├── prompts.py           # all prompts, single source of truth
│   └── schema.py            # strict JSON output schema
├── coach/
│   └── behavior.py          # BehaviorCoach (§5.6)
├── journal/
│   └── journal.py           # append-only SQLite journal (§5.7)
├── tax/
│   └── guard.py             # TaxGuard (§5.8)
├── reporting/
│   ├── benchmark.py         # user vs buy-and-hold
│   ├── cli.py               # `lumisafe` CLI
│   └── dashboard.py         # local Streamlit dashboard (read-only)
├── adapters/
│   └── lumibot_bridge.py    # the ONLY module importing lumibot
├── live_unlock.py           # human-only live gate (§10)
└── runner.py                # wires everything; LumiSafeStrategy(lumibot Strategy)
tests/lumisafe/...
DECISIONS.md
```

### Order flow (every order, no exceptions)
```
OrderIntent (from core, satellite, or the user)
   → BehaviorCoach.review()      # may DELAY user-initiated intents
   → TaxGuard.review()           # may BLOCK or WARN
   → AIAdvisor.review()          # may VETO satellite/user intents; never creates intents
   → RiskGate.check()            # deterministic FINAL say; may BLOCK or RESIZE down
   → lumibot_bridge.submit()
   → Journal.record()            # every step's verdict, taken AND rejected
```
`RiskGate` always runs last before submission. Nothing may call `lumibot_bridge.submit()` except `runner.py`, after `RiskGate` approval. A test enforces this (§13).

---

## 5. Contracts

Contracts are binding. Implementations may add private helpers but must not change these signatures, fields or invariants without a `DECISIONS.md` entry and owner approval.

### 5.1 Config (`lumisafe/config/schema.py`)

```python
from pydantic import BaseModel, Field, model_validator
from typing import Literal

class CoreConfig(BaseModel):
    etfs: dict[str, float]                 # ticker -> target weight within core; sums to 1.0
    monthly_contribution_usd: float = Field(ge=0)
    rebalance_band: float = Field(0.05, gt=0, le=0.20)   # rebalance when drift > band
    rebalance_check: Literal["monthly", "quarterly"] = "monthly"

class SatelliteConfig(BaseModel):
    enabled: bool = False                  # OFF by default for beginners
    max_weight: float = Field(0.10, ge=0, le=0.20)
    allowed_states: list[str] = ["LIVE_ELIGIBLE"]

class RiskConfig(BaseModel):
    max_single_stock_weight: float = Field(0.05, gt=0, le=0.10)
    max_single_etf_weight: float = Field(0.60, gt=0, le=1.0)
    max_trades_per_week: int = Field(5, ge=1, le=20)
    satellite_daily_loss_halt: float = Field(0.03, gt=0, le=0.10)
    satellite_drawdown_halt: float = Field(0.15, gt=0, le=0.30)
    account_drawdown_alert: float = Field(0.25, gt=0, le=0.50)
    min_holding_days: int = Field(5, ge=1)
    allow_leverage: Literal[False] = False
    allow_short: Literal[False] = False
    allow_options: Literal[False] = False

class LumiSafeConfig(BaseModel):
    mode: Literal["backtest", "paper"] = "paper"   # "live" is NOT a valid value here (§10)
    core: CoreConfig
    satellite: SatelliteConfig = SatelliteConfig()
    risk: RiskConfig = RiskConfig()
    benchmark: str = "SPY"
    ai_enabled: bool = True

    @model_validator(mode="after")
    def weights_sum(self):
        assert abs(sum(self.core.etfs.values()) - 1.0) < 1e-6
        return self
```
**Invariant C1:** `allow_leverage`, `allow_short` and `allow_options` cannot be set True. The type is `Literal[False]`.
**Invariant C2:** `mode` can never be `"live"` in config. Live only exists through §10.

### 5.2 Core types (`lumisafe/types.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class Source(str, Enum):
    CORE = "core"; SATELLITE = "satellite"; USER = "user"

class Side(str, Enum):
    BUY = "buy"; SELL = "sell"

class Verdict(str, Enum):
    APPROVE = "approve"; RESIZE = "resize"; DELAY = "delay"; WARN = "warn"
    BLOCK = "block"; VETO = "veto"

@dataclass(frozen=True)
class OrderIntent:
    intent_id: str                 # uuid4
    created_at: datetime
    source: Source
    symbol: str
    side: Side
    quantity: float                # > 0; whole shares unless broker supports fractional
    reason: str                    # human-readable why
    strategy_name: str | None = None

@dataclass(frozen=True)
class ReviewResult:
    reviewer: str                  # "coach" | "tax" | "advisor" | "risk"
    verdict: Verdict
    explanation: str               # plain English, <= 300 chars
    adjusted_quantity: float | None = None   # only with RESIZE; must be < original
    release_at: datetime | None = None        # only with DELAY

@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: datetime
    cash: float
    equity: float
    positions: dict[str, float]          # symbol -> quantity
    prices: dict[str, float]             # symbol -> last price
    peak_equity: float
    satellite_equity: float
    satellite_peak_equity: float
    satellite_day_start_equity: float
    trades_this_week: int
    lots: list["TaxLot"] = field(default_factory=list)

@dataclass(frozen=True)
class TaxLot:
    symbol: str
    quantity: float
    cost_basis: float
    acquired_at: datetime
```

### 5.3 RiskGate (`lumisafe/risk/gate.py`)

```python
class RiskGate:
    def __init__(self, config: RiskConfig, core_symbols: set[str]): ...
    def check(self, intent: OrderIntent, snap: PortfolioSnapshot) -> ReviewResult: ...
    def halted(self, snap: PortfolioSnapshot) -> dict[Source, bool]: ...
```
Pure and deterministic: no I/O, no LLM, no clock reads (time comes from `snap.as_of`).

**Invariants (each must have a dedicated test, and property-based tests via `hypothesis` where marked P):**
- **R1 (P):** Post-trade weight of any non-core single stock ≤ `max_single_stock_weight`. If exceeded, RESIZE down to the cap, or BLOCK if the cap gives zero shares.
- **R2 (P):** Post-trade weight of any single ETF ≤ `max_single_etf_weight`.
- **R3 (P):** No order may make cash negative. That would be margin, so BLOCK.
- **R4:** No SELL may exceed the held quantity (no shorts), so BLOCK.
- **R5:** Satellite and user BUYs are BLOCKed when `trades_this_week ≥ max_trades_per_week`. Core DCA and rebalance trades are exempt.
- **R6:** Satellite intents are BLOCKed for the rest of the day when the satellite is down ≥ `satellite_daily_loss_halt` from `satellite_day_start_equity`.
- **R7:** Satellite intents are BLOCKed until manual reset when the satellite is down ≥ `satellite_drawdown_halt` from `satellite_peak_equity`.
- **R8:** At account drawdown ≥ `account_drawdown_alert`, **core holdings are never sold by the gate** (selling the core in a drawdown is the behavior we prevent). The runner emits an alert, and satellite and user BUYs are BLOCKed.
- **R9:** A SELL of a lot held fewer than `min_holding_days` is BLOCKed unless the source is CORE rebalance.
- **R10:** RESIZE always yields `adjusted_quantity < intent.quantity` and `> 0`. The gate never increases an order.
- **R11 (P):** For any input, `check()` returns exactly one `ReviewResult` and never raises. Malformed input returns BLOCK.

### 5.4 SatelliteStrategy protocol (`lumisafe/strategies/base.py`)

```python
from typing import Protocol
import pandas as pd

class SatelliteStrategy(Protocol):
    name: str
    universe: list[str]                 # tickers it may trade (US-listed stocks/ETFs only)
    rebalance_frequency: str            # "weekly" | "monthly"; never intraday
    def target_weights(self, history: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, float]:
        """Return target weights WITHIN the satellite sleeve (sum <= 1.0, all >= 0).
        Must use only rows with index <= as_of (no look-ahead). Pure function."""
```
**Invariant S1:** `target_weights` must give identical output when given data truncated at `as_of`. A test feeds the full history and truncated history and asserts equality, which catches look-ahead.
**Invariant S2:** Weights are ≥ 0 and sum to ≤ 1.0. The remainder is cash within the sleeve.

### 5.5 AIAdvisor (`lumisafe/advisor/advisor.py`)

```python
class AIAdvisor:
    def __init__(self, llm_client, enabled: bool): ...
    def review(self, intent: OrderIntent, context: AdvisorContext) -> ReviewResult: ...
    def daily_note(self, snap: PortfolioSnapshot, recent: list[JournalEntry]) -> str: ...
```
- The LLM is called through Lumibot's agent runtime if it fits (read-only research role, `allow_trading=False`). Otherwise use a direct client behind the same interface. Log the choice in `DECISIONS.md`.
- **Output schema (strict JSON, validated with pydantic):**
  ```json
  {"verdict": "approve" | "veto", "reasons": ["..."], "confidence": 0.0-1.0, "plain_english": "..."}
  ```
- **A1:** The advisor can only return APPROVE or VETO. It can never RESIZE upward, create an intent, change config or call broker functions. Its client has no tool access to trading.
- **A2:** CORE intents are never sent to the advisor. They are auto-APPROVE with the explanation "core plan".
- **A3 (fail-closed for satellite):** if the LLM errors, times out (>30s) or returns invalid JSON, satellite intents get VETO ("advisor unavailable"). User intents get WARN and continue to the other checks.
- **A4:** Prompts live only in `advisor/prompts.py`. Context is limited to: intent, recent prices, position, journal history for that symbol, and recent headlines if available. Never include API keys or account numbers.
- **A5:** Track veto quality. The journal records each veto's "shadow outcome" (the price change over the following 20 trading days) so we can measure whether the advisor helps.

### 5.6 BehaviorCoach (`lumisafe/coach/behavior.py`)

```python
class BehaviorCoach:
    def review(self, intent: OrderIntent, snap: PortfolioSnapshot,
               market: MarketContext) -> ReviewResult: ...
```
Only reviews `Source.USER` intents. Core and satellite pass through with APPROVE.
- **B1 Panic-sell:** a user SELL of a core ETF when that ETF is down ≥ 10% over the past 20 trading days → DELAY 24h. The explanation shows the historical recovery context for drawdowns of similar size (computed from data, not invented).
- **B2 Chasing:** a user BUY of a symbol up ≥ 25% over the past 20 trading days → DELAY 24h with a plain-English warning.
- **B3 Overtrading:** a user's 3rd+ intent in a single day → DELAY 24h.
- **B4:** After the delay, the intent re-enters the pipeline from the start and needs explicit confirmation (`lumisafe confirm <intent_id>`). Unconfirmed delayed intents expire after 72h.
- **B5:** The coach never BLOCKs. It only delays and explains, because the user stays in control.

### 5.7 Journal (`lumisafe/journal/journal.py`)

SQLite at `~/.lumisafe/journal.db`, append-only.
```python
@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    ts: datetime
    intent: OrderIntent
    reviews: list[ReviewResult]      # in pipeline order
    final: Verdict                   # APPROVE/RESIZE (submitted) or BLOCK/VETO/DELAY (not)
    fill_price: float | None
    fill_qty: float | None
    shadow_return_20d: float | None  # filled later for rejected intents (A5)

class Journal:
    def record(self, entry: JournalEntry) -> None: ...
    def update_fill(self, entry_id: str, price: float, qty: float) -> None: ...
    def update_shadow(self, entry_id: str, ret: float) -> None: ...
    def query(self, **filters) -> list[JournalEntry]: ...
```
- **J1:** No UPDATE or DELETE of an existing entry's intent, reviews or final verdict. Only the fill and shadow fields may be set, and only once each. A SQLite trigger enforces this.
- **J2:** Every intent that reaches the pipeline produces exactly one entry, whether submitted or rejected.

### 5.8 TaxGuard (`lumisafe/tax/guard.py`)

```python
class TaxGuard:
    def review(self, intent: OrderIntent, snap: PortfolioSnapshot,
               recent_sales: list[JournalEntry]) -> ReviewResult: ...
```
- **T1 Wash sale:** a BUY of a symbol sold at a loss within the past 30 days → WARN for the core, BLOCK for satellite and user, with an explanation.
- **T2 Short-term gain:** a non-core SELL of a lot held < 365 days at a gain → WARN with the estimated extra tax at a configurable placeholder rate.
- **T3:** Lots are chosen highest-cost-first for sells, unless that conflicts with R9.
- **T4:** Every TaxGuard explanation ends with "Not tax advice."

---

## 6. Onboarding (`lumisafe/onboarding/`)

`lumisafe init` runs an interactive quiz (≤ 8 questions, plain English):
1. Goal (grow long-term / save for something in < 3 years / learn)
2. Time horizon (years)
3. "If your $1,000 dropped to $X, would you sell?" (finds loss tolerance)
4. Monthly amount you can add
5. Emergency fund in place? (if no → recommend building one first; still allow paper mode)
6. Want to experiment with strategies? (controls the satellite, which defaults to off)

**Output:** a `RiskProfile` (conservative / balanced / growth) plus a `Plan`, written to `~/.lumisafe/config.yaml` and shown in plain English.

| Profile | Core ETFs (example defaults) | Satellite max |
|---|---|---|
| Conservative | 40% total US stock (VTI), 20% intl (VXUS), 40% bonds (BND) | 0% |
| Balanced | 60% VTI, 20% VXUS, 20% BND | 5% |
| Growth | 70% VTI, 25% VXUS, 5% BND | 10% |

- A horizon < 3 years forces Conservative and satellite 0%.
- The plan text must state that the money can lose value and that past results don't guarantee future ones.

---

## 7. Strategies

### 7.1 CoreIndexStrategy (`portfolio/core.py`)
- Buys on the first trading day of each month with `monthly_contribution_usd`, choosing the most underweight ETFs first (contributions rebalance first, which avoids sells).
- Sells only when drift > `rebalance_band` and contributions can't fix it.
- Never reacts to news, prices or the AI.

### 7.2 Reference satellite: TrendFilter (`strategies/trend_filter.py`)
A deliberately simple, well-known rule, included to exercise the pipeline, not as a promised edge:
- Universe: SPY, QQQ, IWM, EFA, TLT.
- Monthly: hold the top 2 by 6-month return, but only those trading above their 200-day moving average. Otherwise hold cash.
- Must satisfy S1/S2 and go through §8 like any strategy.

---

## 8. Promotion pipeline (`lumisafe/promotion/`)

State machine:
```
DRAFT → BACKTESTED → PAPER → LIVE_ELIGIBLE → RETIRED
          ↘ REJECTED   ↘ REJECTED   ↘ RETIRED
```
- **DRAFT → BACKTESTED:** walk-forward with ≥ 4 folds over ≥ 8 years of daily data. In each fold, parameters are chosen only on the train window and evaluated on the next test window. Costs: 5 bps per trade plus 0.05% slippage.
- **BACKTESTED → PAPER** requires, on combined out-of-sample results:
  - Sharpe ≥ 0.5
  - Max drawdown ≤ 1.25× the benchmark's in the same windows
  - Beats or matches benchmark risk-adjusted return (Sharpe) in ≥ 50% of folds
  - Otherwise → REJECTED, with the report saved.
- **PAPER → LIVE_ELIGIBLE** requires ≥ 60 trading days on Alpaca paper with:
  - zero RiskGate violations attempted by the strategy itself
  - paper return within a tolerance band of the backtest expectation (paper ≥ 50% of expected, and no unexplained fills)
  - LIVE_ELIGIBLE only means a human *may* enable it in live mode (§10). Nothing switches automatically.
- **Any → RETIRED (automatic):**
  - 6-month trailing return trails the benchmark by > 5 points, or
  - R7 drawdown halt is triggered twice in 12 months
- Registry is stored in `~/.lumisafe/registry.json`. Every transition is journaled with its metrics.
- **P1:** `SatelliteManager` only runs strategies in `allowed_states`. A test asserts that DRAFT, BACKTESTED, REJECTED and RETIRED strategies never produce intents.

---

## 9. Reporting (`lumisafe/reporting/`)

### CLI
```
lumisafe init                    # onboarding quiz
lumisafe status                  # headline: you vs benchmark, holdings, halts
lumisafe today                   # AI daily note in plain English
lumisafe propose BUY AAPL 3      # user intent → full pipeline
lumisafe confirm <intent_id>     # confirm a delayed intent
lumisafe journal [--rejected]    # show decisions
lumisafe strategies              # registry with states + metrics
lumisafe backtest <strategy>     # run walk-forward, update registry
lumisafe reset-halt satellite    # manual reset after R7 (asks for typed confirmation)
```

### Dashboard (`streamlit run lumisafe/reporting/dashboard.py`)
Read-only. Never places orders.

The **headline number** is "Your return: X% · If you'd just held SPY: Y%". No win-rate metric anywhere in the UI. Other panels: allocation vs target, halts, recent journal, advisor veto quality (shadow returns).

---

## 10. Live trading (human-only)

`lumisafe/live_unlock.py`, created in Phase 1 and never touched by the agent afterward.

Live trading is enabled only when **all** of these hold:
1. The process is started via `lumisafe go-live`, run in an interactive TTY (`sys.stdin.isatty()`).
2. The human types the exact phrase: `I understand I can lose money`.
3. The env var `LUMISAFE_LIVE=1` is set by the human in their shell (not in `.env`).
4. At least one full core-only month has completed in paper mode (checked via the journal).

Live mode allows only CORE plus LIVE_ELIGIBLE satellites. Tests must prove that each condition alone is insufficient (§13).

---

## 11. Disclaimers (must appear in the code paths that show them)
- `lumisafe init`, `status` and the dashboard footer show: *"Educational software. Not financial, tax, or legal advice. Investments can lose value."*
- No code, string or doc may contain the words "guaranteed", "risk-free" or "can't lose". A test greps for them.

---

## 12. Dependencies
- Python version matching the installed Lumibot
- `pydantic>=2`, `pandas`, `numpy`, `pyyaml`, `click` (CLI), `streamlit` (dashboard), `hypothesis` and `pytest` (tests)
- LLM client: whichever Lumibot's agent runtime supports. Otherwise the Anthropic SDK behind `AIAdvisor`.
- No new dependency without a `DECISIONS.md` entry.

---

## 13. Testing requirements

| Area | Required tests |
|---|---|
| Config | C1, C2: invalid configs rejected |
| RiskGate | R1–R11, with property tests (hypothesis) for R1, R2, R3, R11 |
| Strategies | S1 look-ahead test, S2 weight bounds |
| Advisor | A1 (no trade tools exposed), A2, A3 (timeout/invalid JSON paths), A4 (no secrets in prompt) |
| Coach | B1–B5 |
| Journal | J1 (SQLite trigger rejects UPDATE/DELETE), J2 |
| Tax | T1–T4 |
| Promotion | Every state transition, P1, auto-retirement |
| Pipeline | Order flow order (§4); **only `runner.py` calls `bridge.submit()`**, checked by an AST scan test |
| Live gate | Each of the 4 conditions alone fails; all 4 together pass (mocked TTY/env) |
| Text | Banned-words grep test (§11) |
| Integration (`-m integration`) | 5-year backtest of the Balanced core on Yahoo data completes; one paper cycle against Alpaca paper places and journals a core DCA order |

Coverage target: ≥ 90% lines for `risk/`, `journal/`, `promotion/` and `live_unlock.py`; ≥ 75% overall.

---

## 14. Phases (each ends with passing tests and a commit)

| Phase | Deliverables | Done when |
|---|---|---|
| **0. Recon** | Read the Lumibot source/docs; write the API signatures to `DECISIONS.md`; set up the `lumisafe/` skeleton, pytest and CI config | Skeleton imports; empty test suite runs |
| **1. Contracts** | `types.py`, `config/schema.py`, `live_unlock.py`, `journal/` | C1, C2, J1, J2 and live-gate tests pass |
| **2. Risk** | `risk/` complete | R1–R11 pass, including property tests |
| **3. Core** | `portfolio/core.py`, `adapters/lumibot_bridge.py`, `runner.py` (core-only pipeline) | Core-only backtest runs end to end via Lumibot; integration paper DCA order journaled |
| **4. Onboarding + reporting** | `onboarding/`, `reporting/benchmark.py`, CLI `init`/`status`/`journal` | Quiz → config → status shows you-vs-benchmark |
| **5. Coach + tax** | `coach/`, `tax/`, `propose`/`confirm` CLI | B1–B5, T1–T4 pass |
| **6. Promotion** | `promotion/`, `strategies/`, `satellite.py` | TrendFilter runs walk-forward; registry transitions; P1 passes |
| **7. Advisor** | `advisor/`, `today` CLI, shadow-return job | A1–A5 pass; daily note generated in paper mode |
| **8. Dashboard + polish** | `dashboard.py`, disclaimers, README section | §11 grep test passes; full suite green; coverage targets met |

---

## 15. Final acceptance checklist
- [ ] `pytest` passes (unit), and `pytest -m integration` passes with paper keys
- [ ] No file under `lumibot/` modified (`git diff --stat lumibot/` is empty)
- [ ] Every order path goes through RiskGate (AST test)
- [ ] Live trading impossible without all 4 human conditions
- [ ] Balanced-profile core backtest (last 10 years) tracks its blended benchmark within 1.5 points/year
- [ ] TrendFilter has a saved walk-forward report and a registry state, whichever state it earns
- [ ] `DECISIONS.md` explains every deviation from this spec
- [ ] README "LumiSafe quickstart" lets a beginner go from clone to paper trading in ≤ 10 commands

---

## 16. Kickoff prompt (paste into Claude Code)

```
Read SPEC.md and CLAUDE.md fully. Then execute SPEC.md §14 phase by phase, starting with Phase 0.
Follow every rule in SPEC.md §3 — they override everything else. Do not modify lumibot/.
Paper trading only. After each phase: run the tests, fix failures without weakening tests,
commit as "lumisafe: phase N — <summary>", append judgment calls to DECISIONS.md, then continue.
If you hit a §3 rule-9 stop condition, stop and write a clear report of the blocker and options.
When all phases are done, run the §15 checklist and report results.
```

---

### Sources for §1
- 19,646-trader Brazil study and Barber & Odean: [Freedom Isn't Free summary](https://freedomisntfree.co.uk/articles/why-97-percent-of-day-traders-lose-money-uk)
- SEBI FY25 study: [Angel One](https://www.angelone.in/news/retail-f-o-losses-rose-to-over-1-lakh-crore-trader-participation-dropped-20-percent)
- DALBAR 2025 QAIB: [DALBAR press release](https://www.dalbar.com/Portals/dalbar/Cache/News/PressReleases/2025QAIBPressRelease.pdf)
