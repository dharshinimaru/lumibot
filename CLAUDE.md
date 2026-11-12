# Entry-page translation policy

Follow the shared entry-page and translation policy in `AGENTS.md`: README and documentation-homepage changes include every shipped localized counterpart in the same commit. Review meaning, commands, prerequisites and links together; do not update English alone or treat a matching source fingerprint as proof of translation quality.

# Capability first: agents may use undated sources; live trading has no date rules (Rob, 2026-10-22)

- Never make LumiBot agents less capable to look safer. Do not add an instruction, tool filter or gate that blocks ordinary research without a concrete failure behind it.
- Backtests: prefer sources that say when each document became public, and never use anything known to be from after the simulated time (the `document_after_cutoff` refusal and price-data clamping stay). A source with no date at all is still allowed: the agent estimates when it became public, uses it only if that estimate is at or before the simulated time, and says in its answer that the date is an estimate and why. Never invent a date or present an estimate as exact.
- Live trading has no publication-date restrictions. Backtest-only date rules must stay inside the backtest branch of the prompt and tool results.
- Enforced by `tests/test_agent_undated_sources_capability.py` and the evals `web_documents_undated_source_estimated_date`, `web_documents_known_future_document_not_opened` and `web_documents_live_undated_source_used_freely`. Do not loosen them.

# Generic platform behavior and evidence-bound selection (Rob, 2026-10-18, ABSOLUTE)

- LumiBot, BotSpot, BotSpot Agent, Node, Bot Manager and their infrastructure serve every supported strategy, broker, account and customer. Fix generic capabilities at their existing owner. Never add strategy/customer names, IDs, ticker-specific intent branches, private network routes, resource exceptions or hidden defaults to shared runtime, prompts, skills, tools or infrastructure to solve one incident.
- Strategy-specific investment rules and research instructions belong in that strategy's own saved files/prompts. Clearly labeled example strategies may demonstrate generic capabilities; they must never become production defaults or special cases in core.
- Evals reproduce reusable failure modes with neutral, varied synthetic identities and configurations, including multiple live accounts and unrelated strategies. Do not teach a remembered customer strategy as the expected identity. Keep exact incident evidence as provenance, separate from fixtures and behavior rules. A pass tied to one name or configuration does not establish generic correctness.
- Saved memory, prior conversations, sidebar order and example names are clues, not authority to select the current strategy/account. For an unbound new chat saying "this strategy", inspect current owned inventory without silently filtering to a remembered name. If multiple candidates remain, ask the user to select a safe name/account label. Honor an explicitly selected current-chat target and explicit switches; never borrow another account's metrics.
- Before shipping a shared fix, state the generic contract, affected configuration classes, negative controls and source-to-served proof. Do not introduce tool filters, regex intent routing, response rewriting or strategy-specific infrastructure as enforcement shortcuts. Do not alter running customer workloads without their normal exact authorization.

# LumiBot is the platform: never put strategy-specific code in it (Rob, 2026-10-17, ABSOLUTE)

- LumiBot is one generic platform for every strategy. Never add strategy-, person-, company- or website-specific logic, text, tools, skills, prompts, eval fixtures or branches to LumiBot core (for example "House Clerk", "Pelosi", or one site's filing format). Strategy-specific knowledge lives only in that strategy's own prompts.
- Improve LumiBot only with generic capabilities that help every strategy: browser use, file downloads, ZIP, PDF and CSV reading, API calls, and look-ahead safety that works for any source. While fixing a strategy, explain exactly what a proposed LumiBot change does and get Rob's explicit approval before touching LumiBot core. Never release, tag or deploy LumiBot without Rob's explicit approval.
- AI strategy examples (LumiBot `example_strategies` and BotSpot AI strategies) use AI and AI only: exactly one agent dedicated to trading, plus any number of research or planning agents, all called every iteration. The Ray Dalio and Citadel examples are the reference pattern; do not change them. Agents hand work to each other only through their answers, and `self.vars` may carry an agent's last answer forward so it does not redo old work. No Python in between that fetches or parses data, filters filings, computes weights, or skips agents with if statements. The whole point is to show how simple LumiBot makes it: agents research, one agent trades.
- Let the agent find its own way: browse, download, read. A short hint about where to look may go in the prompt. Parsing or availability code may not.
- If a platform behavior blocks a simple example, report the evidence to Rob and propose a generic platform fix. Never work around it with strategy Python or a strategy-specific platform change.
- Why: on 2026-10-17 an agent "fixed" the Pelosi bot with Python that downloaded and filtered House filings and skipped the AI on most days. It then edited LumiBot's shared web-documents skill and evals with House Clerk text on a `pelosi-house-filings` branch. Rob: "LumiBot is not a strategy. LumiBot is the platform. The platform needs to work for all strategies."

# CLAUDE.md - AI Assistant Instructions for LumiBot

## Public Repo Secret Hygiene (CRITICAL)

LumiBot is open source. Never put private, account-specific, or
machine-specific information in this file or any other tracked repo file.

- Do not include usernames, passwords, API keys, tokens, account emails,
  customer identifiers, broker credentials, paid vendor credentials, private
  hostnames, private URLs, cookies, local profile paths, or absolute personal
  filesystem paths.
- Do not document real `.env` locations, credential-file paths, secret store
  values, or commands that write real credentials to disk.
- Use placeholders such as `<your-vendor-username>`,
  `<your-downloader-host>`, `<repo>`, and `$TMPDIR`, and point contributors to
  public environment variable names or their own secret manager.
- If sensitive data is needed for private BotSpot/Lumiwealth operations, keep it
  in private repos/docs/secret stores, not in this public LumiBot checkout.

## BotSpot integration release qualification

- LumiBot has its own package/release cycle. When a BotSpot release explicitly
  includes a changed LumiBot artifact or contract, run the complete locally
  runnable LumiBot gate on that exact package candidate before downstream Dev.
- The consuming BotSpot candidate must pass every Playwright test owned by its
  scope locally before Dev; focused tests never qualify. If Agent is scoped,
  mandatory local eval freshness and required targeted repeats must also pass.
- Hosted qualification reruns the same committed BotSpot inventory against the
  exact Dev tuple. Hosted-only failures block production and return to local
  parity, contention, or nondeterminism diagnosis.

## 🚨🚨🚨 RULE #1 — NEVER FABRICATE BACKTEST DATA 🚨🚨🚨

**Fake / synthesized / default-filled market data is STRICTLY FORBIDDEN.**
**Missing data is fine. Fake data is a lawsuit waiting to happen.**

- If real bars are unavailable for a day / symbol / timeframe, return empty. Do NOT invent a placeholder bar, do NOT carry-forward the last price, do NOT substitute a constant (e.g. `VIX=14.2`), do NOT interpolate.
- This applies to **every provider path** (IBKR, ThetaData, Polygon, Yahoo, DataBento, custom) and **every timestep** (minute, day, tick).
- Placeholder rows used for cache bookkeeping ("we tried this day, nothing was there") must NEVER be returned to a strategy as real bars. Filter them out, and surface the absence to the caller.
- If you find code that fabricates/synthesizes/forward-fills missing market data to keep a strategy alive — **delete it**. The honest outcome (empty frame, explicit warning, strategy skip) is the ONLY correct outcome.
- A backtest that makes decisions on fake data produces fake PnL. Showing fake PnL to customers is a legal and reputational catastrophe.
- Full details and enforcement rules live in `docs/BACKTESTING_ARCHITECTURE.md` under "RULE #1". Read it before touching any data-fetching code.

## Quick Start

**First, read these files:**
1. `docs/BACKTESTING_ARCHITECTURE.md` - Understand the backtesting data flow AND the no-fake-data rule
2. `AGENTS.md` - Critical rules for ThetaData (DO NOT SKIP)

## Image Generation Rule (CRITICAL)

- For any generated or AI-edited image, infographic, diagram, marketing visual, README visual, documentation visual, or repo asset image, use the approved Image Generator only.
- `Nano Banana` is a compatibility phrase for the approved Image Generator. It does not select Google or Gemini unless Rob explicitly insists on that provider after rejecting the approved default result.
- The Image Generator controls provider, model, resolution, and quality. Callers may provide only the prompt, purpose, aspect ratio, approved reference images, and an explicit Rob-requested Medium-quality exception. Never request Pro, High, Auto, an arbitrary resolution, or another model.
- Never use local SVG/HTML/canvas placeholders, Python drawing scripts, Mermaid screenshots, manually assembled box diagrams, or other fallback pipelines for generated documentation/product images.
- For Lumibot, BotSpot, and Lumiwealth visuals, use the canonical Spot mascot reference or the approved brand reference images when a mascot is helpful. Spot should usually be doing something relevant to the concept being explained, such as reviewing filings, managing agents, guarding risk, or filing memories. Do not accept off-brand mascot variants or generic static poses when a topic-specific Spot action would make the visual clearer.
- Every generated image must be visually inspected before it is committed. Reject outputs with broken/missing text, awkward arrows, cluttered layouts, inaccurate product claims, or anything that looks like a placeholder.
- If the Image Generator is unavailable or broken, stop and report that blocker instead of making replacement images another way.

## Backtesting Accuracy (Definition)

Backtesting “accuracy” is measured against live broker behavior when possible (replay a live-traded interval and reproduce fills + PnL within tolerances). Vendor parity (e.g., stored DataBento artifacts) is a regression signal, not absolute truth.

## Multi-Agent Collaboration (CRITICAL)
This repo is often worked on by **multiple AI sessions** at the same time.

- Canonical checkout: normal LumiBot work must happen in this repository
  checkout. Keep that checkout on the active `version/X.Y.Z` branch, clean, and
  ready for release. Do not create sibling
  worktrees like `lumibot-version-X.Y.Z` for normal development. Temporary
  worktrees outside this folder are acceptable only for isolated review of
  unusually large or risky external PRs, and must not become the active release
  workspace.
- Branch etiquette: if a task mandates a specific version branch (e.g., `4.4.25`), treat it as the shared branch—stay on it and do not create new branches/PRs unless explicitly instructed. Otherwise, start new work branches from a stable base branch (e.g., `dev`/`main`/`master`), and avoid chaining feature/WIP branches.
- No “feature branch chaining”: if you’re already on a feature/WIP or version branch (e.g., `feature/*`, `fix/*`, `wip/*`, `version/*`, `release/*`, or a version-named branch like `X.Y.Z`), keep working there; don’t create another feature branch from it unless explicitly instructed.
- Branch naming (LumiBot convention): prefer version-scoped branches so multiple agents can collaborate without “feature branch naming drift”.
  - Default for active release work: the shared version branch (e.g., `4.4.25` or `version/X.Y.Z`).
  - Avoid `feature/*` and `fix/*` here unless explicitly requested.
  - If you truly need isolation and are explicitly instructed to branch, use a scoped suffix (e.g., `4.4.25/<topic>` or `version/X.Y.Z/<topic>`)—but don’t chain off that unless explicitly instructed.
- Never run `git checkout`; avoid destructive commands (`git reset --hard`, `git clean -f`, `git stash`).
- Dirty-tree safety: if you need to branch with uncommitted changes, create the new branch from the current working tree so the changes come with you; avoid `git stash`. Verify with `git status --porcelain=v1`.
- Before committing: `git status` must be clean/understood; read diffs for changes you didn’t personally create.
- Coordinate via `docs/handoffs/` when touching shared areas (CI/baselines/backtest harnesses).
- Any behavioral change must include docs updates + regression tests, with comments explaining “why/invariants”.

## Branch Management (CRITICAL - Branches Are Extremely Sensitive)

**ALWAYS stay on a `version/X.Y.Z` branch.** Never work on `master`, `main`, or `dev` directly. If you find yourself on `master` or `main`, something is wrong. Fix it immediately by switching to the current version branch.

### At session start (MANDATORY every time)
```bash
git branch --show-current  # MUST be version/X.Y.Z
grep ‘version=’ setup.py   # MUST match the branch name
```
If the branch is NOT `version/X.Y.Z`, find the latest version branch and switch to it:
```bash
git fetch origin
git branch -r | grep ‘version/’ | sort -V | tail -1  # Find latest
git switch version/X.Y.Z                               # Switch to it
```

### Before every commit (MANDATORY)
```bash
git branch --show-current  # Verify still on version/X.Y.Z
```

### Deployment procedure (follow `docs/DEPLOYMENT.md` strictly)
1. **All work** happens on `version/X.Y.Z`. Never push directly to `dev`.
2. **Before release**: merge `dev` into `version/X.Y.Z` to pick up other engineers’ changes.
3. **Release**: merge `version/X.Y.Z` into `dev` via PR, tag `vX.Y.Z` on the dev merge commit.
4. **After release (CRITICAL, DO NOT SKIP)**: the release workflow auto-creates `version/X.Y.(Z+1)` on the REMOTE. You must switch your LOCAL checkout to it. Steps:
   - `git status --porcelain=v1` — if non-empty, commit as `wip: carry-over to X.Y.(Z+1)` on the OLD branch first.
   - `git fetch origin && git switch version/X.Y.(Z+1)` (never `git checkout`).
   - If you had local-only commits on `version/X.Y.Z` (including the carry-over above), cherry-pick them onto the new branch.
   - Verify: `git branch --show-current` must print `version/X.Y.(Z+1)`; `grep 'version=' setup.py` must match.
   - Staying on the released branch locally causes every new commit to land on frozen code and uncommitted work to silently belong to the wrong version (the 2026-04-20 4.5.1 incident — WEEX/Coinbase/iter_count work had to be manually cherry-picked to 4.5.2 because the local checkout was never switched).
5. **BotManager deploy**: update `LUMIBOT_VERSION` variable, trigger dev then prod workflows.
6. **Post-deploy**: verify `lumibot_version` in a backtest’s `settings.json` matches (via BotSpot MCP `start_backtest` + `get_backtest_artifact` with `label="settings.json"`).

### What went wrong (2026-04-15 incident)
During the flat-price bug investigation, the local checkout silently switched from `version/4.4.62` to `master`. This was not caught until the user noticed. The deploy itself was correct (all remote artifacts were on the right branch), but the local state was wrong. **Always verify your branch before and after every operation.**

### The `master` branch is NOT used for active work
`master` is a legacy branch used only for GitHub Pages. The active branches are `dev` (source of truth) and `version/X.Y.Z` (active work). If you find yourself on `master`, you are in the wrong place.

## AGENTS.md / CLAUDE.md Best Practices (how we keep instructions useful)
- These instruction files are loaded automatically at session start, so keep guidance here **universal** and avoid dumping long, task-specific walls of text here.
- Prefer **progressive disclosure**:
  - Architecture + runbooks: `docs/` (start with `docs/BACKTESTING_ARCHITECTURE.md`).
  - Investigations and full trade audits: `docs/investigations/`.
  - Cross-session coordination: `docs/handoffs/`.
  - One-off helpers: `scripts/` (safe-timeout friendly).
  - **Public docs (Sphinx):** `docsrc/` is the source for the public documentation site; keep docstrings and Sphinx pages up to date for user-facing behaviors.
- When a workflow changes (env vars, cache semantics, harness flags), update the relevant `docs/*` page in the same change set.
- **AI Navigation:** See `llms.txt` in repo root for structured documentation index
- **File naming convention (MANDATORY):** All docs use **UPPERCASE** names with underscores:
  - Root docs: `TOPIC_NAME.md` (e.g., `BACKTESTING_ARCHITECTURE.md`)
  - Handoffs: `YYYY-MM-DD_TOPIC_NAME.md` (e.g., `2026-01-04_THETADATA_HANDOFF.md`)
  - Investigations: `YYYY-MM-DD_TOPIC_NAME.md` (e.g., `2026-01-02_ACCURACY_AUDIT.md`)
  - Date-first for chronological sorting; UPPERCASE for consistency
- **File header (REQUIRED):** New docs must start with: Title, one-line description, Last Updated date, Status, Audience, and Overview section
- Interop note: `AGENTS.md` is the cross-tool convention; `CLAUDE.md` is Claude Code’s native file. If you want a single source of truth, Claude Code supports importing:
  - `@AGENTS.md`

## Env var documentation (REQUIRED)
- If you add or change an environment variable, update:
  - `docsrc/environment_variables.rst` (public docs), and
  - `docs/ENV_VARS.md` when engineering notes help contributors.

## Changelog Maintenance (MANDATORY)

**Location:** `CHANGELOG.md`

**CRITICAL:** The changelog MUST be updated for every deployment, release, or significant change. The changelog documents ALL changes in a version, not just what the current AI session worked on.

### Version Source of Truth

The **authoritative version** is in `setup.py` (look for the `version=` line):
```python
version="X.Y.Z",  # e.g., "4.4.25", "4.5.0", etc.
```

When this version changes and is committed, it signals a deploy/release is happening or imminent.

### How to Find Changes for a Version

Before updating the changelog, **gather ALL commits** since the last version change:

```bash
# Find the current version
grep 'version=' setup.py

# Find when setup.py version was last changed
git log --oneline -p setup.py | grep -A2 -B2 'version=' | head -30

# Get commits since a specific commit/tag (replace X.Y.Z with actual previous version)
git log --oneline <last-version-commit>..HEAD

# Get commits since last version bump - replace X.Y.Z with the PREVIOUS version
git log --oneline $(git log --oneline -1 --all -S 'version="X.Y.Z"' -- setup.py | cut -d' ' -f1)..HEAD

# Or use the tag if available (replace X.Y.Z with previous version)
git log --oneline vX.Y.Z..HEAD
```

**IMPORTANT:** The changelog should include changes from ALL contributors (multiple AI agents, human developers), not just the current session. Read through ALL commits to capture the full picture.

### When to Update the Changelog

Update the changelog when ANY of these occur:
1. **Deployments** - Any code deployed to production
2. **Version bumps** - New version tags or releases
3. **Bug fixes** - Especially data source, split adjustment, or dividend fixes
4. **New features** - New brokers, data sources, or strategy capabilities
5. **Breaking changes** - API changes, env var changes, config changes
6. **Performance improvements** - Cache optimizations, query efficiency
7. **Dependency updates** - Major library version changes

### Changelog Format

```markdown
## X.Y.Z - YYYY-MM-DD

### Added
- New feature description

### Changed
- Modified behavior description

### Fixed
- Bug fix description

### Deprecated
- Feature being phased out

### Removed
- Removed feature

### Security
- Security-related changes
```

### Pre-Deployment Checklist

Before any deployment:
- [ ] Reviewed git commits since last version (`git log <last-version>..HEAD`)
- [ ] Changelog entry added with current date
- [ ] Version number updated in setup.py (if applicable)
- [ ] All significant changes documented (from ALL contributors)
- [ ] Breaking changes clearly marked with ⚠️

**If you forget to update the changelog, you MUST add a retroactive entry before the next deployment.**

## GitHub Release Markers (RECOMMENDED)

To keep deployments traceable (and easy to diff):

- Tag the deploy commit with the semantic version (annotated tag): `vX.Y.Z`
- Push the tag to GitHub
- Create a GitHub Release from that tag and paste the corresponding `CHANGELOG.md` entry
- PR title convention: `X.Y.Z` (or `Release X.Y.Z`) so the version is visible in the PR list

## Documentation Layout

- `docs/` = hand-authored markdown (architecture, investigations, handoffs, ops notes)
- Backtesting speed/parity playbook: `docs/BACKTESTING_PERFORMANCE.md`
- Handoffs: `docs/handoffs/`
- Investigations: `docs/investigations/`
- `docsrc/` = Sphinx source for the public docs site
- `generated-docs/` = local build output from `docsrc/` (gitignored)
- Docs publishing should happen via GitHub Actions on `dev` (avoid committing generated HTML)

## Public Documentation Updates (MANDATORY)

**Location:** `docsrc/` (Sphinx source files)

The public documentation at lumibot.lumiwealth.com is a critical resource for users. **Always update it when making user-facing changes.**

### When to Update Which File

| Change Type | Update This File |
|-------------|------------------|
| New broker added | `docsrc/brokers.*.rst` (create new file following existing pattern) |
| New strategy method | `docsrc/strategy_methods.*.rst` (add to appropriate category) |
| New strategy property | `docsrc/strategy_properties.rst` |
| New lifecycle method | `docsrc/lifecycle_methods.*.rst` |
| New entity class | `docsrc/entities.*.rst` |
| New backtesting data source | `docsrc/backtesting.*.rst` |
| New environment variable | `docsrc/environment_variables.rst` |
| Bug fix users hit often | `docsrc/common_mistakes.rst` |
| Common user question | `docsrc/faq.rst` |
| New feature (general) | `docsrc/getting_started.rst` or relevant section |
| Deployment changes | `docsrc/deployment.rst` |

### AI Agent Documentation Responsibility (CRITICAL)

**Every time you work on LumiBot code, you MUST check and update BOTH documentation locations:**

| Location | Purpose | Audience |
|----------|---------|----------|
| `docs/` | Architecture, investigations, handoffs, engineering notes | AI agents & contributors |
| `docsrc/` | Public Sphinx documentation | End users & customers |

**When working on LumiBot, AI agents MUST:**
1. **Proactively update docs** - Don't wait to be asked; update both `docs/` and `docsrc/` as needed
2. **Check for doc gaps** - If you notice missing docs while working, flag or fix them in BOTH locations
3. **Keep examples current** - If you change API, update example code in both folders
4. **Add to FAQ/Common Mistakes** - When fixing user-reported bugs, document the fix
5. **Update engineering docs** - When you learn something about the codebase, add it to `docs/` so future AI sessions benefit

**Workflow for every code change:**
```
1. Identify relevant files in BOTH docs/ and docsrc/
2. Read them - are they accurate? Complete? Up to date?
3. If NO → Update them as part of your changes
4. If feature is undocumented → Add documentation to both locations
5. Build docsrc locally to verify customer-facing changes render correctly
```

**For `docs/` (AI/Engineering documentation):**
- Update `docs/BACKTESTING_ARCHITECTURE.md` when changing data flow
- Add to `docs/investigations/` when debugging complex issues
- Use `docs/handoffs/` for cross-session coordination
- Document "why" decisions and gotchas that future AI sessions need to know

**For `docsrc/` (Customer-facing documentation):**
- Update the relevant `.rst` file when changing user-facing behavior
- Add code examples that customers can copy-paste
- Document all parameters, return values, and edge cases
- Keep Getting Started guide accessible to beginners

**Examples of proactive documentation:**
- Working on `get_historical_prices()`? Check `strategy_methods.data.rst` AND `docs/BACKTESTING_ARCHITECTURE.md`
- Fixing a bug in Alpaca broker? Check `brokers.alpaca.rst` - does it mention this edge case?
- Adding a new order type? Update `strategy_methods.orders.rst` AND add an example to `examples.rst`
- Discovered a tricky caching behavior? Add it to `docs/` so future AI sessions don't rediscover it

**Do NOT:**
- Assume docs are complete because they exist
- Skip doc updates because "it's a small change"
- Leave doc updates for "later" or "another PR"
- Only update one location when both need updating
- Forget that `docs/` helps future AI sessions be more effective

**Remember:** If you touch code, check BOTH doc locations. If docs are incomplete, fix them. This is part of the definition of done for any task.

### Periodic Documentation Audits

**Frequency:** At least once per major release or quarterly

**Audit checklist:**
- [ ] All public methods in Strategy class are documented
- [ ] All brokers have complete documentation
- [ ] All environment variables are listed
- [ ] Examples are up-to-date and working
- [ ] FAQ reflects recent user questions (check Discord/GitHub issues)
- [ ] Common Mistakes reflects recent bug reports
- [ ] Getting Started guide works for new users
- [ ] No broken links or missing images

## Project Overview

LumiBot is a trading and backtesting framework supporting multiple data sources (Yahoo, ThetaData, Polygon) and brokers (Alpaca, Interactive Brokers, Tradier, etc.).

## Key Locations

| What | Where |
|------|-------|
| LumiBot library | This repository checkout |
| Strategy examples | `example_strategies/` and `tests/backtest/acceptance_strategies/` |
| Demo strategies | Use your own untracked local strategy folder |
| Environment config | Use untracked local `.env` files or your secret manager |
| Backtest logs | Use `logs/` or another untracked local artifact folder |

## Critical Rules

### ThetaData Rules (MUST FOLLOW)

1. **NEVER run ThetaTerminal locally WITH PRODUCTION CREDENTIALS** - It will kill production connections
2. **Only use the Data Downloader** configured via `DATADOWNLOADER_BASE_URL` for backtests (avoid hard-coded IPs/hostnames—they can change on redeploy)
3. **Always compare ThetaData vs Yahoo** - Yahoo is the gold standard for split-adjusted prices
4. **Do not publish local ThetaTerminal credentials** - See `AGENTS.md` for the
   public-safe policy. Use your own secret manager or untracked local
   environment for rare local ThetaTerminal debugging.
5. **Wrap long commands with safe-timeout (20m default max).** Use `bin/safe-timeout 1200s ...` when available, or another timeout wrapper, and split work into smaller chunks if it would run longer.
6. See `AGENTS.md` for complete rules

### Private endpoint hygiene (MUST FOLLOW)

- Never hardcode or paste private downloader URLs/hostnames into code, docs, tests, logs, `AGENTS.md`, or `CLAUDE.md`.
- Use placeholders like `http://localhost:8080` or `https://<your-downloader-host>:8080`, and refer readers to `DATADOWNLOADER_BASE_URL`.

### Data Source Selection

The `BACKTESTING_DATA_SOURCE` env var **OVERRIDES** explicit code:
```bash
# In .env file
BACKTESTING_DATA_SOURCE=thetadata  # Uses ThetaData regardless of code
BACKTESTING_DATA_SOURCE=yahoo      # Uses Yahoo regardless of code
BACKTESTING_DATA_SOURCE=none       # Uses whatever class the code specifies
```

### Cache Management

**LOCAL/DEV ONLY — if seeing wrong/stale data on your own machine or in dev:**
1. Bump `LUMIBOT_CACHE_S3_VERSION` (e.g., v5 → v6) — invalidates everything, forces clean rebuild
2. Clear local cache: `rm -rf ~/Library/Caches/lumibot/`

**🚨 PRODUCTION — NEVER bump `LUMIBOT_CACHE_S3_VERSION`.** Doing so invalidates the entire prod cache including ThetaData option chains, which are extremely expensive to rewarm and will make the site/backtests painfully slow for a long time. For prod issues:
- Prove bad data is actually *cached* (download a specific parquet and inspect) before deleting anything — the bug may be runtime-only
- Use **surgical** `aws s3 rm --recursive` on the narrowest affected prefix (e.g., `s3://lumibot-cache-prod/prod/cache/v1/ibkr/stock/` or `.../ibkr/index/`)
- It is OK to clear most of `prod/cache/v1/ibkr/` **except `ibkr/future/`** (futures cache is costly to rebuild)
- Never touch `prod/cache/v1/thetadata/` — that data is clean and rebuilds are slow

## Common Tasks

### Run a Backtest

```bash
cd <your-untracked-strategy-folder>
python3 "<your-strategy>.py"
```

### Compare Yahoo vs ThetaData

1. Edit `Demos/.env`:
   - Set `BACKTESTING_DATA_SOURCE=yahoo`
2. Run backtest, note results
3. Edit `Demos/.env`:
   - Set `BACKTESTING_DATA_SOURCE=thetadata`
4. Run backtest, compare results
5. Results should match within ~1-2%

### Check Backtest Results

```bash
ls -la logs/ | grep TQQQ | tail -10
```

Look at `*_tearsheet.csv` for CAGR and metrics.

## Known Issues & Fixes

### ✅ ThetaData Split Adjustment (FIXED - Nov 28, 2025)

**Status:** FIXED - Split handling now correct

**Root cause:** The `_apply_corporate_actions_to_frame()` function was being called 26+ times per backtest without any idempotency check, causing split adjustments to be applied multiple times.

**Fix applied:**
1. Added idempotency check at start of `_apply_corporate_actions_to_frame()` - checks for `_split_adjusted` column marker
2. Added marker at end of function after successful adjustment
3. Cache version bumped to v7

### ✅ ThetaData Dividend Split Adjustment (FIXED - Nov 28, 2025)

**Status:** FIXED - 17/21 dividends now match Yahoo within 5%

**Root causes found:**
1. `_update_cash_with_dividends()` was called 3 times per day without idempotency
2. ThetaData dividend amounts were UNADJUSTED for splits
3. ThetaData returned duplicate dividends for same ex_date (e.g., 2019-03-20 appeared 4x)
4. ThetaData returned special distributions with `less_amount > 0` (e.g., 2015-07-02)

**Fixes applied:**
1. Added `_dividends_applied_tracker` in `_strategy.py` to prevent multiple applications
2. Added split adjustment to `get_yesterday_dividends()` in `thetadata_backtesting_pandas.py`
3. Added deduplication by ex_date in `_normalize_dividend_events()`
4. Added filter for `less_amount > 0` to exclude special distributions

**Verified split adjustment:**
- ThetaData cumulative factor for 2014 dividends: 48x (2×3×2×2×2)
- After adjustment: $0.01182 raw → $0.000246 adjusted ≈ Yahoo's $0.000250 ✓

**Current results:** ~47% CAGR with ThetaData vs ~42% with Yahoo (gap due to phantom dividends)

### ⚠️ ThetaData Phantom Dividend (KNOWN ISSUE - Reported to ThetaData)

**Status:** KNOWN DATA QUALITY ISSUE - Reported to ThetaData support team

| Date | ThetaData | Yahoo | Status |
|------|-----------|-------|--------|
| 2014-09-18 | $0.41 raw | None | ⚠️ PHANTOM - main cause of CAGR gap |
| 2015-07-02 | $1.22 raw | None | ✅ FILTERED (less_amount=22.93) |
| 2020-12-23 | $0.000283 | None | ⚠️ PHANTOM |
| 2021-12-23 | $0.000119 | None | ⚠️ PHANTOM |

**Root cause:** ThetaData phantom dividends are DATA ERRORS in the SIP feed, not Return of Capital (ROC) distributions. Confirmed via Perplexity research - these amounts don't appear in any other financial database (Yahoo, Bloomberg, SEC filings).

**Workaround options:**
1. Use `BACKTESTING_DATA_SOURCE=yahoo` for dividend-sensitive strategies
2. Wait for ThetaData to fix the data quality issue
3. Accept ~5% CAGR gap as known ThetaData limitation

**Key files:**
- `lumibot/tools/thetadata_helper.py` - `_apply_corporate_actions_to_frame()`, `_normalize_dividend_events()`
- `lumibot/backtesting/thetadata_backtesting_pandas.py` - `get_yesterday_dividends()`
- `lumibot/strategies/_strategy.py` - `_update_cash_with_dividends()`

### ✅ ThetaData Zero-Price Data Filtering (FIXED - Nov 28, 2025)

**Status:** FIXED - Zero-price rows now filtered automatically

**Root cause:** ThetaData sometimes returns rows with all-zero OHLC values (e.g., Saturday 2019-06-08 for MELI), which caused `ZeroDivisionError` when strategies tried to calculate positions.

**Fix applied:**
1. Added zero-price filtering when loading from cache (`thetadata_helper.py` lines ~2501-2513)
2. Added zero-price filtering when receiving new data from ThetaData (`thetadata_helper.py` lines ~2817-2829)
3. Cache is self-healing - bad data is filtered on load

**Unit tests added:**
- `TestZeroPriceFiltering` class with 6 tests covering all edge cases
- Tests verify: zero-row removal, valid-zero-volume preservation, weekend-zero handling, partial-zeros, empty DF, all-zero DF

### Cache Version Mismatch

Always ensure `.env` files have matching cache versions:
- `lumibot/.env`
- `Demos/.env`

## Split Adjustment Testing (Added Dec 11, 2025)

### Test Files

Two new test files provide comprehensive split adjustment coverage:

| File | Tests | Purpose |
|------|-------|---------|
| `tests/test_split_adjustment.py` | 18 unit tests | Tests split-related functions with mocked data |
| `tests/test_thetadata_yahoo_parity.py` | 14 tests | Compares ThetaData vs Yahoo (gold standard) |

### Running Split Tests

```bash
cd <repo>

# Run unit tests only (no API calls, fast)
pytest tests/test_split_adjustment.py tests/test_thetadata_yahoo_parity.py -v -m "not apitest"

# Run integration tests (requires ThetaData credentials)
THETADATA_USERNAME=<username> THETADATA_PASSWORD=<password> \
pytest tests/test_thetadata_yahoo_parity.py -v -m apitest
```

### Test Coverage

**test_split_adjustment.py** covers:
- `_get_option_query_strike()` - Strike conversion for API queries (GOOG 20:1, AAPL 4:1, TSLA 3:1)
- `_apply_corporate_actions_to_frame()` - Price/dividend adjustments, idempotency
- `_normalize_split_events()` - Split event parsing (numerator/denominator, ratio string)
- `_normalize_dividend_events()` - Deduplication, special distribution filtering
- Edge cases: fractional splits, reverse splits, timezone handling

**test_thetadata_yahoo_parity.py** covers:
- Price parity before and after splits for GOOG, AAPL, TSLA
- GOOG March 2020 COVID crash (the specific scenario that triggered this testing)
- SPY baseline test (no recent splits)
- Multi-date consistency checks

### When to Run These Tests

1. **Before any change to `thetadata_helper.py`** - Run the full unit test suite
2. **After fixing a split-related bug** - Run integration tests to verify against Yahoo
3. **When investigating a backtest discrepancy** - Run parity tests for the affected symbol

### Known Test Status

- All 18 unit tests in `test_split_adjustment.py`: **PASSING**
- All 5 mocked tests in `test_thetadata_yahoo_parity.py`: **PASSING**
- Integration tests (9 tests): Require ThetaData credentials, marked with `@pytest.mark.apitest`

## Test Philosophy (CRITICAL - READ THIS)

### Test Age = Test Authority

When tests fail, how you fix them depends on **how old the test is**:

| Test Age | Authority Level | How to Fix |
|----------|----------------|------------|
| **>1 year old** | LEGACY - High authority | **Fix the CODE**, not the test. These tests have proven themselves over time. |
| **6-12 months** | ESTABLISHED - Medium authority | Investigate carefully. Likely fix the code, but could be test issue. |
| **<6 months** | NEW - Lower authority | Test may need adjustment. Still verify code isn't broken. |
| **<1 month** | EXPERIMENTAL | Test is still being refined. Adjust as needed. |

### Check Test Age Before Fixing

```bash
# Check when a test file was first created
git log --format="%ai" --follow -- tests/path/to/test.py | tail -1

# Check when a specific test function was added
git log -p --all -S 'def test_function_name' -- tests/
```

### Conflict Resolution

When old tests and new tests conflict:
1. **Old test wins by default** - it has proven track record
2. If the new test represents genuinely new functionality, ask the user
3. Document any judgment calls in the test file with comments

### Adding Comments to Tests

For tests over 1 year old, add a comment when modifying:
```python
# LEGACY TEST (created Aug 2023) - This test has proven correct behavior
# DO NOT modify expected values without understanding the full impact
# If this test fails, FIX THE CODE, not this test
def test_important_behavior():
    ...
```

## Testing Checklist for Data Source Changes

1. Run TQQQ 200-Day MA with Yahoo (2013-2025) → expect ~30-45% CAGR
2. Run same strategy with ThetaData → should match Yahoo within ~5%
3. Check for anomalous daily returns (>50% gain/loss indicates split issue)
4. Compare specific prices around split dates (esp. Jan 13, 2022 2:1 forward split)

## Architecture Quick Reference

```
Strategy.backtest()
    │
    ▼
Data Source Selection (env var overrides code)
    │
    ├── Yahoo: yfinance → split-adjusted prices
    ├── ThetaData: Data Downloader → split-adjusted prices
    └── Polygon: Polygon API → handles splits in cache validation
    │
    ▼
BacktestingBroker (simulates trades)
    │
    ▼
Results (tearsheet, trades, logs)
```

See `BACKTESTING_ARCHITECTURE.md` for detailed data flow diagrams.

## CRITICAL: MCP Servers (MUST USE)

**ATTENTION CLAUDE:** These MCP servers are **ESSENTIAL** for working effectively on LumiBot. They have proven to make a **massive difference** in debugging complex data issues, validating financial data accuracy, and navigating this large codebase. **USE THEM PROACTIVELY AND FREQUENTLY.**

**IF THESE MCP SERVERS ARE NOT AVAILABLE:** Inform the user immediately and strongly recommend they install them. The debugging experience without these tools is significantly degraded. Installation instructions can be found in the Claude Code documentation.

### Why These Tools Matter for LumiBot

LumiBot deals with:
- Complex financial data from multiple sources (Yahoo, ThetaData, Polygon)
- Split adjustments, dividend calculations, and corporate actions
- Large codebase with intricate data flow paths
- Data accuracy requirements where small errors compound over time

Without MCP tools, debugging these issues is slow and error-prone. With them, you can validate data against external sources, systematically break down problems, and navigate the codebase efficiently.

---

### Sequential Thinking (`mcp__sequential-thinking__sequentialthinking`) - **USE THIS FIRST**

**STRONGLY RECOMMENDED** for ANY complex debugging task. This tool has been critical for:
- Breaking down backtesting discrepancies into systematic steps
- Analyzing why ThetaData vs Yahoo results differ
- Planning fixes that don't introduce regressions
- Debugging split/dividend calculation issues

**USE IT:** Before diving into complex code changes, use sequential thinking to plan your approach.

### Perplexity (`mcp__perplexity__*`) - **ESSENTIAL FOR DATA VALIDATION**

**CRITICAL** for validating financial data. LumiBot data issues often stem from incorrect source data. Perplexity lets you:
- `perplexity_search` - Verify stock split dates and ratios
- `perplexity_research` - Deep dive into dividend history discrepancies
- `perplexity_ask` - Quick validation of corporate action data

**REAL EXAMPLE:** We discovered ThetaData "phantom dividends" by using Perplexity to cross-reference against Yahoo, Bloomberg, and SEC filings. This identified data quality issues that would have been impossible to find otherwise.

**USE IT:** Whenever you see unexpected financial data, validate it with Perplexity before assuming the code is wrong.

### Memory (`mcp__memory__*`) - **TRACK YOUR FINDINGS**

**HIGHLY RECOMMENDED** for maintaining context across debugging sessions:
- Store known data source discrepancies
- Record phantom dividend dates and amounts
- Track cache version changes and their reasons
- Remember which fixes were applied and why

**USE IT:** When you discover an issue, store it in memory so future sessions don't have to rediscover it.

### X-Ray (`mcp__xray__*`) - **NAVIGATE THE CODEBASE**

**PARTIALLY USEFUL** for understanding the LumiBot codebase:
- `explore_repo` - **WORKS** - Map the directory structure
- `what_breaks` - **WORKS** - Find all usages of a function (text search)
- `find_symbol` - **REQUIRES ast-grep** - If this fails, install: `brew install ast-grep`

**USE IT:** Before modifying any function, use `what_breaks` to understand the impact. If `find_symbol` fails, use Grep tool instead.

### Context7 (`mcp__context7__*`) - **GET CURRENT DOCS**

**USEFUL** for library documentation:
- `resolve-library-id` - Find library IDs
- `get-library-docs` - Get current pandas, yfinance, polygon docs

### Chrome DevTools (`mcp__chrome-devtools__*`)

**USEFUL** for debugging Data Downloader issues:
- Test API endpoints directly
- Inspect network responses from ThetaData

---

## North Star Metrics and OKRs (CRITICAL)

**Every project MUST have clearly defined North Star metrics and OKRs.** If metrics don't exist for this project, propose them as your first action. If they exist, reference them when making decisions.

- Track leading indicators weekly. Create dashboards and graphs to visualize progress.
- When starting any task, check: does this move a North Star metric? If not, question its priority.
- See the private workspace operating instructions for the full framework when
  working inside Rob's local BotSpot/Lumiwealth environment.

# Agent evals are part of "done" (2026-09-24)

LumiBot's agent runtime now ships 78 builtin tools against 16 eval cases.
Twenty-seven of those tools landed in a single day and one of them got an eval.
That is the gap these rules close.

- **Every new agent tool ships with at least one eval.** A tool with no eval is
  not done. Put the case in `agent_eval_cases/` next to the others.
- **Every eval must fail first**, for the reason a customer would actually hit,
  and the red artifact is saved in `agent_eval_baselines/` with the date in the
  filename. An eval that was green from birth proves nothing.
- **Repeat policy.** A new or changed case must reach three consecutive passes
  before it is recorded as established; the runner enforces this itself through
  `target_passes()`, so you do not need to pass `--repeat 3`. An established
  case then runs once on the ordinary gate. Prior passes carry forward, so an
  interrupted run resumes rather than restarting.
- **Run it:**

  ```bash
  python3 scripts/run_agent_evals.py --max-cost-usd 10
  ```

  Defaults are `--repeat 1` and `--max-workers 8`. Evals are network-bound, not
  CPU-bound, so raising workers is the cheapest speedup available.
- **Highest-value gaps right now**, in order: order lifecycle (market vs limit
  vs bracket, limit walking, wait-for-terminal, partial fills, rejections),
  look-ahead discipline when the agent reads the open web through
  `browser_*`, `http_request` or `rss_fetch`, then indicators, futures and
  forex. See `docs/research/2026-09-24_lumibot-4.6.1-game-plan.md`.
- **Look-ahead is special.** LumiBot blocks look-ahead mechanically where it
  owns the data path. It cannot once the agent reaches an arbitrary page or
  API, so those cases are the only protection that exists there. When you add a
  look-ahead eval, teach the rule in the matching skill in the same change.


# CLAUDE.md — Mandate

Agent-to-agent B2B procurement marketplace. Auth0 governs **whether an agent may act**; Stripe governs **where the money goes**; a human sits between them holding a phone.

Built for the Auth0 × Stripe hackathon. Build window is 4.5 hours with two people. Every decision below is optimized for *a demo that works on stage*, not for production correctness. When those two goals conflict, the demo wins — but say so in a comment.

---

## 1. The money shot

Everything in this repo exists to make these 30 seconds work:

> Two AI agents negotiate a contract in a live transcript. They settle at **$850**. The buying agent's mandate ceiling is **$800**. The agent cannot pay — it holds no token that permits it. A phone buzzes with an Auth0 push showing the real deal terms. A human taps approve. Stripe captures the payment, splits it to the vendor's connected account, and takes the platform's 3%.

**If a change doesn't serve those 30 seconds, don't make it.**

Three properties are non-negotiable and must be *true*, not simulated:

1. **The mandate lives in a signed token, not a database row.** The ceiling is a custom claim on the agent's Auth0 access token. The server reads it from verified JWT claims. Never from the store.
2. **The negotiation always settles above the ceiling.** Seed data is rigged so this happens every single run (§11). A demo that sometimes lands under the ceiling has no ending.
3. **The payment route is scope-gated.** The agent's normal token carries `deals:negotiate` only. `POST /api/deals/:id/pay` requires `payments:execute` *plus* a matching deal id. Hitting it without approval returns a real 403, and the UI shows it.

---

## 2. Stack — locked, do not deviate

| Concern | Choice | Why |
|---|---|---|
| Framework | **Next.js 15, App Router, TypeScript** | Auth0 + Stripe both have first-class Next paths |
| Styling | **Tailwind CSS** | No component library. Hand-roll. Faster than learning someone's API at 3pm. |
| User auth | **`@auth0/nextjs-auth0` v4** | v4 has a different API from v3 — see §9.1 |
| Agent auth | **Raw `client_credentials` via fetch** | No SDK needed, it's one POST |
| Persistence | **In-memory singleton + JSON snapshot** | See §7.4. Do NOT add Postgres, Prisma, Supabase, or an ORM. |
| Payments | **`stripe` node SDK** | |
| LLM | **`@anthropic-ai/sdk`** | With a deterministic fallback (§11.3) |
| Streaming | **SSE via a Route Handler `ReadableStream`** | No websockets, no Pusher |
| Deploy | **Vercel** | Deploy at 2pm and keep deploying. Do not first deploy at 5pm. |

No monorepo. No Turborepo. One Next.js app.

---

## 3. Repo layout

```
/app
  /(marketing)/page.tsx              landing → "Sign in"
  /dashboard/page.tsx                buyer org home: jobs, mandate, tier
  /dashboard/mandate/page.tsx        admin-only: set agent mandate
  /deals/[id]/page.tsx               THE DEMO SCREEN — transcript + approval + payment
  /approve/[approvalId]/page.tsx     phone-facing approval screen (front-channel fallback)
  /vendor/page.tsx                   vendor org home + Connect onboarding CTA
  /api
    /auth/[auth0]/route.ts           @auth0/nextjs-auth0 v4 handler
    /jobs/route.ts                   POST: NL text → structured Job
    /deals/route.ts                  POST: open a deal room
    /deals/[id]/negotiate/route.ts   GET: SSE stream of negotiation turns
    /deals/[id]/pay/route.ts         POST: SCOPE-GATED. The 403 lives here.
    /approvals/route.ts              POST: request approval (CIBA or front-channel)
    /approvals/[id]/route.ts         GET: poll status  |  POST: approve/deny
    /stripe/connect/route.ts         POST: create Express account + onboarding link
    /stripe/subscribe/route.ts       POST: Billing Checkout session for Pro
    /stripe/webhook/route.ts         POST: raw-body webhook handler
/lib
  store.ts          in-memory singleton, seeded
  types.ts          §7 — write this FIRST, both people import from it
  auth0/
    session.ts      user session + org helpers
    agent.ts        M2M token minting, mandate claim extraction
    verify.ts       JWT verification via jose + JWKS
    ciba.ts         /bc-authorize + polling
  approval.ts       mode switch: ciba | frontchannel. One interface.
  fga.ts            canRead(actor, dealId, field). Mode switch: fga | local.
  stripe/
    client.ts
    connect.ts      Express accounts, destination charges
    billing.ts      subscription checkout
  agents/
    negotiate.ts    the loop
    personas.ts     buyer/vendor system prompts
    fallback.ts     scripted transcript when LLM is slow or absent
  seed.ts
/components
  Transcript.tsx    streaming turn list — the visual centerpiece
  MandateBadge.tsx  shows ceiling + live deal amount, turns red on breach
  ApprovalCard.tsx  pending → approved, with the RAR payload rendered
  PaymentPanel.tsx  Stripe result, fee split breakdown
```

---

## 4. Environment variables

Write `.env.example` with every one of these on day zero.

```bash
# --- Auth0 (user-facing app) ---
AUTH0_DOMAIN=
AUTH0_CLIENT_ID=
AUTH0_CLIENT_SECRET=
AUTH0_SECRET=                      # openssl rand -hex 32
APP_BASE_URL=http://localhost:3000
AUTH0_AUDIENCE=https://api.mandate.dev

# --- Auth0 (agent M2M) ---
AUTH0_AGENT_CLIENT_ID=
AUTH0_AGENT_CLIENT_SECRET=

# --- Auth0 (CIBA client — may be the same app) ---
AUTH0_CIBA_CLIENT_ID=
AUTH0_CIBA_CLIENT_SECRET=

# --- Auth0 FGA (optional) ---
FGA_MODE=local                     # local | fga
FGA_STORE_ID=
FGA_CLIENT_ID=
FGA_CLIENT_SECRET=
FGA_API_URL=

# --- Stripe ---
STRIPE_SECRET_KEY=
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRO_PRICE_ID=

# --- Behavior flags (THE IMPORTANT ONES) ---
APPROVAL_MODE=ciba                 # ciba | frontchannel  — see §10
DEMO_MODE=live                     # live | mock          — see §14
NEGOTIATION_MODE=llm               # llm | scripted       — see §11.3
```

Every flag must work in every combination. `DEMO_MODE=mock` + `APPROVAL_MODE=frontchannel` + `NEGOTIATION_MODE=scripted` must produce a complete, convincing end-to-end run with **no external service configured at all**. That combination is the parachute.

---

## 5. Manual setup a human must do

Claude Code cannot click these. Generate `SETUP.md` containing them, print the list at the start of the session, and stub anything that's blocked rather than stalling.

**Stripe Projects (do this first — the judges asked for it):**
```bash
brew install stripe/stripe-cli/stripe
stripe plugin install projects
stripe projects init mandate
stripe projects add auth0/client
```
Screen-record this. It goes in the demo.

**Auth0 dashboard:**
1. **Get CIBA enabled.** It requires an Enterprise plan or add-on. Assume the tenant does not have it. Auth0 staff are in the room — ask at 1:01pm. If blocked, set `APPROVAL_MODE=frontchannel` and move on.
2. Create API `https://api.mandate.dev` with scopes `deals:negotiate`, `deals:read`, `payments:execute`.
3. On that API, register authorization detail type `procurement_payment` (required for RAR).
4. Create Organizations `org_acme` (buyer), `org_bright` + `org_delta` (vendors). Add members, assign roles `admin` / `requester`.
5. Create the M2M application for agents; authorize it for the API with `deals:negotiate` and `deals:read` **only**. Not `payments:execute`.
6. Enable the CIBA grant on the CIBA application.
7. Enroll a phone in **Guardian**. Do this at 1:05 — it's the second-longest-lead item after #1.
8. Add the Action from §9.3.

**Stripe dashboard:** enable Connect (Express), create the Pro price, `stripe listen --forward-to localhost:3000/api/stripe/webhook`.

---

## 6. Build order

Build in phases. **Every phase ends with something demoable.** Never be in a state where nothing runs.

### Phase 0 (20 min) — skeleton
`lib/types.ts`, `lib/store.ts`, `lib/seed.ts`, routes stubbed returning fixtures, Tailwind shell. **Write types.ts before splitting work.** Both people import from it; this is what prevents integration hell at 4:15.

### Phase 1 (45 min) — THE WHOLE DEMO, FAKED
`DEMO_MODE=mock`. Scripted transcript streams over real SSE. Mandate breach detected. Fake approval card resolves after a 3s timer. Fake payment shows the fee split. The entire narrative arc works end to end.

**This is the single most important phase in the build.** It is your insurance policy. From here on you are *replacing fakes with real services one at a time*, and if you run out of clock you still have a demo. Do not skip ahead to "real" work because Phase 1 feels like cheating. It isn't; it's the plan.

### Phase 2 (30 min) — real Auth0 login + Organizations
Org-scoped session, role-gated mandate page, real member identities in the UI.

### Phase 3 (40 min) — real agent identity
M2M token with mandate claims. Server verifies JWT and reads the ceiling from claims. `/pay` scope gate returns a real 403. Ceiling in the UI now comes from a decoded token, and the UI shows the decoded claim.

### Phase 4 (60 min) — real approval  ← **the demo's spine, give it the better debugger**
CIBA or front-channel per `APPROVAL_MODE`. Phone buzzes. Approve. Elevated token unlocks `/pay`.

### Phase 5 (45 min) — real Stripe
Connect Express onboarding, destination charge with application fee, webhook-driven state.

### Phase 6 (30 min) — FGA
Real store if the clock allows, `local` mode if not. Same tuple model either way.

### Phase 7 (20 min) — Billing tier
Free vs Pro, gate a feature, let the paywall appear on screen for two seconds during the demo.

**Hard feature freeze at 4:45pm.** 4:45–5:15 is three full demo runs; record the backup on run #2.

---

## 7. Domain model

`lib/types.ts` — write this first, change it only by mutual agreement.

```ts
export type OrgRole = 'admin' | 'requester';
export type Tier = 'free' | 'pro';

export type Org = {
  id: string;                    // matches Auth0 org_id
  name: string;
  kind: 'buyer' | 'vendor';
  tier: Tier;
  stripeCustomerId?: string;
  stripeAccountId?: string;      // vendors: Connect Express account
  // vendor economics — never exposed to the buyer agent
  hourlyRateCents?: number;
  floorCents?: number;
};

export type Mandate = {
  maxAmountCents: number;
  currency: 'usd';
  categories: string[];
  expiresAt: number;             // unix seconds
};

export type Job = {
  id: string;
  buyerOrgId: string;
  requestedByUserId: string;
  rawText: string;               // what the human typed
  title: string;
  category: string;
  scope: string;
  deadline: string;
  createdAt: number;
};

export type Turn = {
  speaker: 'buyer_agent' | 'vendor_agent' | 'system';
  text: string;
  offerCents?: number;
  ts: number;
};

export type DealState =
  | 'negotiating'
  | 'settled_within_mandate'
  | 'awaiting_approval'
  | 'approved'
  | 'denied'
  | 'paid'
  | 'failed';

export type Deal = {
  id: string;
  jobId: string;
  buyerOrgId: string;
  vendorOrgId: string;
  state: DealState;
  amountCents?: number;          // settled amount
  mandateSnapshot: Mandate;      // from the token, at negotiation time
  transcript: Turn[];
  approvalId?: string;
  paymentIntentId?: string;
  applicationFeeCents?: number;
  createdAt: number;
};

export type Approval = {
  id: string;
  dealId: string;
  approverUserId: string;
  mode: 'ciba' | 'frontchannel';
  status: 'pending' | 'approved' | 'denied' | 'expired';
  authReqId?: string;            // CIBA
  bindingMessage: string;
  authorizationDetails: unknown; // the RAR payload — render this in the UI
  grantedToken?: string;         // elevated, single-use
  createdAt: number;
  resolvedAt?: number;
};
```

**§7.4 — the store.** A module-level singleton of plain Maps. Snapshot to `.data/store.json` on write so a dev-server restart mid-demo doesn't wipe state. Guard the singleton against Next.js hot-reload double-init with a `globalThis` key. No database. If you find yourself wanting one, you're building the wrong thing.

---

## 8. The authorization model

This is the product. Get it exactly right.

**The agent's normal token** (`client_credentials`, audience `https://api.mandate.dev`):
```json
{
  "sub": "agt_buyer_01@clients",
  "scope": "deals:negotiate deals:read",
  "https://mandate.dev/org_id": "org_acme",
  "https://mandate.dev/mandate": {
    "max_amount": 80000,
    "currency": "usd",
    "categories": ["electrical", "hvac"],
    "expires_at": 1785700000
  }
}
```

**The elevated token** (post-approval, from CIBA or front-channel): adds `payments:execute` and carries `authorization_details` naming the specific deal and amount.

**`POST /api/deals/:id/pay` must, in this order:**
1. Verify the bearer JWT against the tenant JWKS (`jose`, `createRemoteJWKSet`). Reject unsigned/expired.
2. Require `payments:execute` in `scope`. Missing → **403 with body `{ error: 'mandate_exceeded', ceiling, requested }`**. The UI renders this; it is a demo beat, not an error state to hide.
3. Require `authorization_details[0].deal_id === params.id`. A token approved for deal A must not pay deal B. Say this out loud in the demo — it's the detail that reads as *engineer wrote this*, and it's what the Stripe judge will probe.
4. Require `amount === deal.amountCents`. No amount substitution after approval.
5. Mark the approval consumed. Single use.
6. Only then call Stripe.

Never trust `mandateSnapshot` from the store for the authorization decision. It's there for display and audit. The decision reads verified claims.

---

## 9. Auth0 integration

### 9.1 v4 SDK notes
v4 differs substantially from v3. It uses middleware plus an `Auth0Client` instance, not `handleAuth()`. Create `lib/auth0.ts` exporting a configured client; use `auth0.getSession()` in server components and `auth0.middleware(req)` in `middleware.ts`. Check the installed version's types before writing against remembered v3 APIs — if there's a mismatch, trust the types in `node_modules`, not this file.

Org-scoped login: pass `authorizationParameters: { organization: orgId }` on the login route.

### 9.2 Agent token minting (`lib/auth0/agent.ts`)
```ts
POST https://${AUTH0_DOMAIN}/oauth/token
{ grant_type: 'client_credentials',
  client_id: AUTH0_AGENT_CLIENT_ID,
  client_secret: AUTH0_AGENT_CLIENT_SECRET,
  audience: AUTH0_AUDIENCE,
  organization: orgId }
```
Cache by org id until 60s before expiry.

### 9.3 The Action that injects the mandate
On the **Client Credentials Exchange** trigger, read the mandate from the org's metadata (or a static map keyed by `org_id` — acceptable for a hackathon, note the shortcut in a comment) and set:
```js
api.accessToken.setCustomClaim('https://mandate.dev/org_id', orgId);
api.accessToken.setCustomClaim('https://mandate.dev/mandate', mandate);
```

### 9.4 CIBA (`lib/auth0/ciba.ts`)
```
POST https://${AUTH0_DOMAIN}/bc-authorize
  content-type: application/x-www-form-urlencoded
  client_id, client_secret
  login_hint={"format":"iss_sub","iss":"https://${AUTH0_DOMAIN}/","sub":"auth0|USER_ID"}
  scope=openid payments:execute
  audience=${AUTH0_AUDIENCE}
  binding_message=Approve $850.00 to Bright Electric
  authorization_details=[{"type":"procurement_payment","deal_id":"deal_123",
    "amount":85000,"currency":"usd","vendor":"Bright Electric",
    "scope_of_work":"200A panel upgrade, suite 300","deadline":"2026-11-03"}]
  requested_expiry=300
```

Then poll:
```
POST https://${AUTH0_DOMAIN}/oauth/token
  grant_type=urn:openid:params:grant-type:ciba
  auth_req_id=...
  client_id, client_secret
```

**Rules that will bite you:**
- `binding_message` is **required**, max 64 chars, alphanumeric + `+-_.,:#` only. **A `$` will be rejected.** Write `Approve 850.00 USD to Bright Electric`.
- `requested_expiry` **must be ≤ 300**. Above 300 Auth0 switches to email delivery and the phone never buzzes.
- The grant type is documented as `urn:openid:params:grant-type:ciba`. Some references show `urn:openid:params:oauth:grant-type:ciba`. Try the documented one first; if you get `unsupported_grant_type`, try the other before assuming anything else is wrong.
- Every `type` in `authorization_details` must be pre-registered on the API in the dashboard or the request fails.
- Poll at the `interval` from the response. On `slow_down`, back off to the new interval. Handle `authorization_pending`, `access_denied`, `expired_token` distinctly — the UI should say *why*.

Poll server-side and expose status to the client via `GET /api/approvals/:id`. The browser polls your API every 1s; your server polls Auth0 at its interval. Do not poll Auth0 from the browser.

### 9.5 FGA (`lib/fga.ts`)
One interface, two implementations, `FGA_MODE` selects.

```
type: deal
  relations:
    buyer_agent, vendor_agent, buyer_admin
  permissions:
    view_terms:            buyer_agent | vendor_agent | buyer_admin
    view_budget_ceiling:   buyer_agent | buyer_admin        # NOT vendor
    view_vendor_floor:     vendor_agent                     # NOT buyer
```

Every field handed into an agent's prompt context goes through `canRead()`. That's the point: the vendor agent cannot be prompt-injected into revealing the buyer's ceiling because it was never given it. Expect this question from the Auth0 judge and have the model on a slide.

In `local` mode, implement the identical tuple checks in-process. Do not fake the *answers* — fake only the *storage*. And say which mode you're in when asked.

---

## 10. The approval abstraction

`lib/approval.ts` exposes exactly this, and nothing upstream knows which mode is active:

```ts
requestApproval(deal, approverUserId): Promise<Approval>
getApproval(id): Promise<Approval>
resolveApproval(id, 'approved'|'denied'): Promise<Approval>   // frontchannel only
```

**`ciba` mode:** §9.4.

**`frontchannel` mode** (when CIBA isn't licensed):
1. Create the `Approval` with the same `authorizationDetails` payload.
2. Send the approver a link to `/approve/[approvalId]` — SMS, or just show a QR code on screen. **A QR code the presenter scans on stage is nearly as good theater as a push, and has zero external dependencies.** Strongly consider this even as a backup for CIBA mode.
3. The phone opens it, authenticates through normal Auth0 login, sees the deal terms.
4. On approve, mint a short-lived single-use token scoped to `payments:execute` for that deal id only, and store it on the approval.

The downstream `/pay` gate is byte-identical in both modes. Say the honest line in the demo: *"on an Enterprise tenant this is CIBA with a RAR payload; we're showing the same authorization model over the front channel."*

---

## 11. Negotiation engine

### 11.1 Rigged seed data — do not change these numbers
```
Job:            200A panel upgrade, suite 300, category "electrical"
Buyer mandate:  maxAmountCents 80000        ($800.00)
Bright Electric floorCents:   85000         ($850.00)
Delta Contracting floorCents: 92000         ($920.00)
```
The cheapest vendor's floor is **above** the ceiling. The negotiation therefore *always* breaches. This is deliberate and load-bearing. Do not "fix" it.

### 11.2 The loop
Max 3 rounds. Buyer opens low, vendor counters, converge toward the vendor floor, settle at `min(vendorFloors)` = 85000. Stream each turn over SSE with a 600–900ms delay between turns — it reads as *thinking* and gives the presenter room to talk. Instant text is worse theater.

Each agent's prompt context is assembled through `canRead()` (§9.5). The buyer agent knows the ceiling. The vendor agent does not, and vice versa.

On settle: compare `amountCents` to the **token's** mandate claim. Above → state `awaiting_approval`, create the approval, and the UI turns red. Below → `settled_within_mandate` and pay straight through (build this path; it proves the ceiling actually gates rather than always firing).

### 11.3 Fallback
`NEGOTIATION_MODE=scripted` replays a hand-written transcript with identical timing and the identical settle amount. **Flip to scripted if any round takes over 4 seconds.** Wire a keyboard shortcut or query param to force it — `?scripted=1`. You want that switch reachable in one second while standing at a podium.

Write the scripted transcript by hand and make it *good*. Give the vendor agent a line about travel time and permit costs. Judges remember personality.

---

## 12. UI requirements

The deal page carries 90 of your 180 demo seconds. It deserves disproportionate polish.

- **Transcript** — chat-style, buyer left, vendor right, offer amounts as inline badges. Auto-scroll. Each turn fades in.
- **MandateBadge** — persistently visible: `Ceiling $800.00`. When the settle exceeds it, it turns red and animates once. This is the visual beat the whole demo pivots on. Make it unmissable from the back of a room.
- **ApprovalCard** — renders the RAR payload as human text, not JSON. Show a spinner reading *"waiting for approver…"*. When it resolves, show who approved and when. Include a small collapsed `<details>` with the raw payload for the judge who asks.
- **PaymentPanel** — `$850.00 → Bright Electric $824.50 · Mandate fee $25.50`. Real numbers from the Stripe response, not computed client-side.
- **Decoded token drawer** — a collapsible panel showing the agent's live decoded claims. Costs 15 minutes, and it's the fastest way to prove the mandate is in a signed token rather than a database row.

Dark UI. Large type. Assume a projector and a room at the back.

---

## 13. Stripe integration

**Connect Express onboarding** — `accounts.create({ type: 'express' })` then `accountLinks.create`. Test mode autofills fast. Do this for `org_bright` early and keep the account id; if onboarding drags past 45 minutes, hardcode a pre-created connected account id and move on.

**The charge** — destination charge, server-confirmed for demo determinism:
```ts
stripe.paymentIntents.create({
  amount: deal.amountCents,
  currency: 'usd',
  payment_method: 'pm_card_visa',
  confirm: true,
  automatic_payment_methods: { enabled: true, allow_redirects: 'never' },
  application_fee_amount: Math.round(deal.amountCents * 0.03),
  transfer_data: { destination: vendor.stripeAccountId },
  metadata: { deal_id: deal.id, approval_id: deal.approvalId },
}, { idempotencyKey: `deal_${deal.id}_pay` });
```
The idempotency key is not optional. It's keyed on the deal so a double-click can't double-charge, and it is exactly the kind of detail a Stripe staff engineer scans for.

**Billing** — Checkout Session in `subscription` mode for Pro. Webhook on `checkout.session.completed` flips `org.tier`. Gate something visible on `tier === 'free'` so the paywall appears on screen.

**Webhooks** — raw body (`await req.text()`), verify with `constructEvent`, handle `payment_intent.succeeded`, `payment_intent.payment_failed`, `checkout.session.completed`, `account.updated`. Drive deal state from webhooks, don't poll. Log every event to a visible in-app feed — it makes the system feel alive and it's free credibility.

---

## 14. Demo mode

`DEMO_MODE=mock` must run the complete flow with **no external service configured**: scripted negotiation, timer-resolved approval, fabricated payment result with correct-looking fee math. Clearly marked in the UI with a small `MOCK` chip — never let a judge think a mock is real. This is what you run if the venue wifi dies.

---

## 15. Do not build

Deleting these is what makes 4.5 hours work.

- Fetch.ai, uAgents, AgentVerse, any blockchain, any on-chain payment protocol
- Postgres, Supabase, Prisma, any ORM or migration
- Self-serve signup, password reset, email verification, org creation UI
- Vendor calendars, scheduling, status tracking, aggregate dashboards, profile builders
- More than 2 agents, or more than 3 negotiation rounds
- Tests, beyond one smoke script that runs the happy path headlessly
- Mobile responsive layouts — it's a projector
- Dark/light toggle, i18n, an admin panel, a settings page
- Stripe Shared Payment Tokens **unless** a Stripe engineer on site unblocks a network business profile before 2:30pm. It's a preview API (`Stripe-Version: 2026-04-22.preview`) and it's a rabbit hole. If you skip it, put it on the roadmap slide — `usage_limits.max_amount` *is* the mandate enforced by Stripe, and knowing that reads well.

---

## 16. Gotchas

- **CIBA needs Enterprise.** Assume you don't have it until someone confirms otherwise. `APPROVAL_MODE=frontchannel` is a first-class path, not a consolation prize.
- **`binding_message` rejects `$`.** Alphanumeric and `+-_.,:#` only, 64 chars max.
- **`requested_expiry > 300` silently switches to email.** Your phone will not buzz and you will lose 30 minutes finding out why.
- **RAR types must be pre-registered** on the API or `/bc-authorize` fails.
- **Guardian push needs the phone on cellular data**, not conference wifi.
- **Next.js hot reload re-instantiates module singletons.** Pin the store to `globalThis`.
- **Stripe webhooks need the raw body.** `req.text()`, never `req.json()`.
- **`@auth0/nextjs-auth0` v4 ≠ v3.** Read the installed types.
- **SSE through Vercel** needs the route to stream properly; test the deployed URL by 3pm, not at 5pm.
- **Never log a token, even truncated.** You'll be screen-sharing.

---

## 17. Commands

```bash
npm run dev
npm run seed                 # reset store to §11.1 fixtures — bind this to a key, you'll run it 30 times
npm run demo:check           # headless happy path: negotiate → breach → approve → pay
stripe listen --forward-to localhost:3000/api/stripe/webhook
```

`npm run seed` must be instant and total. Between demo runs you will need to reset in under two seconds while talking.

---

## 18. Working agreement

- **`lib/types.ts` is the contract.** Changing it requires telling the other person out loud.
- Person A owns `/lib/auth0/**`, `/lib/approval.ts`, `/lib/fga.ts`. Person B owns `/lib/stripe/**`, `/lib/agents/**`. Shared: `types.ts`, `store.ts`, components.
- Commit every 15 minutes. Push. A lost laptop at 4pm should cost 15 minutes, not the day.
- **Never break `main`.** If it's broken, fixing it preempts everything.
- At 4:45 the branch is frozen. After that: run the demo, fix only what's broken on stage, write the script.
