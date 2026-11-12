"""Human-only live-trading gate (SPEC §10).

THIS FILE IS HUMAN-OWNED. The implementing agent created it in Phase 1 and must
never edit it afterward (SPEC §3 rule 2).

Live trading is enabled only when ALL of these hold:
  1. the process runs in an interactive TTY (``sys.stdin.isatty()``);
  2. the human types the exact phrase ``I understand I can lose money``;
  3. ``LUMISAFE_LIVE=1`` is set by the human in their shell, not in a ``.env`` file;
  4. at least one full core-only calendar month has completed in paper mode,
     as recorded in the journal.

The only way to get a :class:`LiveAuthorization` is :func:`unlock`. Every live
code path (the bridge's live broker factory) must check
:func:`is_authorized` first. Config can never say ``mode: live`` (Invariant C2).
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from lumisafe.journal.journal import PAPER_CYCLE_EVENT, Journal
from lumisafe.types import Source

PHRASE = "I understand I can lose money"
ENV_VAR = "LUMISAFE_LIVE"

# Live mode may run only CORE intents plus satellites that reached LIVE_ELIGIBLE.
LIVE_ALLOWED_SOURCES = (Source.CORE.value, Source.SATELLITE.value)
LIVE_ALLOWED_SATELLITE_STATES = ("LIVE_ELIGIBLE",)

# A paper month counts as "full" when heartbeats cover it end to end.
MIN_PAPER_DAYS_IN_MONTH = 15
FIRST_HEARTBEAT_BY_DAY = 7
LAST_HEARTBEAT_FROM_DAY = 22

_MINT_KEY = object()


class LiveUnlockError(PermissionError):
    def __init__(self, failures: list[str]):
        self.failures = list(failures)
        super().__init__("Live trading NOT enabled:\n- " + "\n- ".join(self.failures))


@dataclass(frozen=True)
class LiveAuthorization:
    granted_at: datetime
    allowed_sources: tuple[str, ...] = LIVE_ALLOWED_SOURCES
    allowed_satellite_states: tuple[str, ...] = LIVE_ALLOWED_SATELLITE_STATES
    _key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._key is not _MINT_KEY:
            raise PermissionError("LiveAuthorization can only be created by lumisafe.live_unlock.unlock()")


def is_authorized(auth: object) -> bool:
    return isinstance(auth, LiveAuthorization) and auth._key is _MINT_KEY


# --- the four conditions ---------------------------------------------------------


def check_tty(stdin) -> bool:
    try:
        return bool(stdin is not None and stdin.isatty())
    except Exception:
        return False


def check_phrase(typed: str | None) -> bool:
    if typed is None:
        return False
    return typed.rstrip("\r\n") == PHRASE


_ENV_LINE = re.compile(rf"^\s*(export\s+)?{ENV_VAR}\s*=")


def _defined_in_dotenv(dotenv_paths: Iterable[str | Path]) -> bool:
    for p in dotenv_paths:
        path = Path(p)
        try:
            if not path.is_file():
                continue
            for line in path.read_text(errors="ignore").splitlines():
                if _ENV_LINE.match(line):
                    return True
        except OSError:
            # Unreadable .env: we can't prove the flag isn't there, so fail closed.
            return True
    return False


def check_env(environ: Mapping[str, str], dotenv_paths: Iterable[str | Path]) -> tuple[bool, str | None]:
    if environ.get(ENV_VAR) != "1":
        return False, f"{ENV_VAR}=1 is not set in your shell environment."
    if _defined_in_dotenv(dotenv_paths):
        return False, f"{ENV_VAR} is defined in a .env file; it must be set by you in your shell only."
    return True, None


def _month_key(ts: datetime) -> tuple[int, int]:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.year, ts.month


def qualifying_paper_months(journal: Journal, now: datetime) -> list[tuple[int, int]]:
    """Completed calendar months that count as a full core-only paper month."""
    current = _month_key(now)
    days: dict[tuple[int, int], set[int]] = defaultdict(set)
    satellite_seen: set[tuple[int, int]] = set()
    for ev in journal.events(kind=PAPER_CYCLE_EVENT):
        if ev.payload.get("mode") != "paper":
            continue
        key = _month_key(ev.ts)
        if key >= current:
            continue
        ts = ev.ts.astimezone(timezone.utc) if ev.ts.tzinfo else ev.ts
        days[key].add(ts.day)
        if ev.payload.get("satellite_enabled"):
            satellite_seen.add(key)

    core_fills: set[tuple[int, int]] = set()
    for e in journal.query(source=Source.CORE, submitted=True):
        if e.fill_qty:
            core_fills.add(_month_key(e.ts))
    satellite_trades = {_month_key(e.ts) for e in journal.query(source=Source.SATELLITE, submitted=True)}

    good = []
    for key, ds in sorted(days.items()):
        if (
            len(ds) >= MIN_PAPER_DAYS_IN_MONTH
            and min(ds) <= FIRST_HEARTBEAT_BY_DAY
            and max(ds) >= LAST_HEARTBEAT_FROM_DAY
            and key not in satellite_seen
            and key not in satellite_trades
            and key in core_fills
        ):
            good.append(key)
    return good


def check_paper_history(journal: Journal, now: datetime) -> bool:
    try:
        return len(qualifying_paper_months(journal, now)) >= 1
    except Exception:
        return False


# --- entry point -------------------------------------------------------------------


def unlock(
    journal: Journal,
    *,
    stdin=None,
    environ: Mapping[str, str] | None = None,
    dotenv_paths: Iterable[str | Path] | None = None,
    ask: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> LiveAuthorization:
    """Check all four human conditions and mint a LiveAuthorization, or raise.

    The phrase is only asked for when stdin is a real TTY, so a script can't
    pipe it in.
    """
    stdin = sys.stdin if stdin is None else stdin
    environ = os.environ if environ is None else environ
    dotenv_paths = [Path.cwd() / ".env"] if dotenv_paths is None else list(dotenv_paths)
    ask = input if ask is None else ask
    now = now or datetime.now(timezone.utc)

    failures: list[str] = []

    tty = check_tty(stdin)
    if not tty:
        failures.append("Not running in an interactive terminal (TTY).")

    env_ok, env_msg = check_env(environ, dotenv_paths)
    if not env_ok:
        failures.append(env_msg)

    if not check_paper_history(journal, now):
        failures.append("No full core-only month of paper trading is recorded in the journal yet.")

    phrase_ok = False
    if tty:
        try:
            typed = ask(f'Type exactly "{PHRASE}" to continue: ')
        except (EOFError, KeyboardInterrupt):
            typed = None
        phrase_ok = check_phrase(typed)
    if not phrase_ok:
        failures.append("The confirmation phrase was not typed exactly.")

    if failures:
        raise LiveUnlockError(failures)
    return LiveAuthorization(granted_at=now, _key=_MINT_KEY)
