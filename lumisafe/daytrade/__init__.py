"""Paper-only day-trading lab (owner-approved amendment to SPEC §2, 2026-11-13; see DECISIONS.md).

Its trades are SATELLITE intents tagged with ``INTRADAY_STRATEGY``. Only that
name is exempt from the 5-trades-a-week cap (R5) and the 5-day minimum hold
(R9); it has its own per-day limits instead (``daytrade/gate.py``). Every other
satellite rule, the AI veto (fail-closed) and the account-level rules still apply.
"""

INTRADAY_STRATEGY = "daytrade_news_breakout"
INTRADAY_STRATEGIES = frozenset({INTRADAY_STRATEGY})
