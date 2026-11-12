"""Strategy lifecycle state machine (SPEC §8), stored in ``~/.lumisafe/registry.json``.

    DRAFT → BACKTESTED → PAPER → LIVE_ELIGIBLE → RETIRED
              ↘ REJECTED   ↘ REJECTED   ↘ RETIRED
    Any → RETIRED (automatic)

LIVE_ELIGIBLE only means a human *may* enable it in live mode; nothing
switches automatically. Every transition is journaled with its metrics.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from lumisafe import paths
from lumisafe.journal.journal import Journal


class State(str, Enum):
    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    PAPER = "PAPER"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


ALLOWED: dict[State, set[State]] = {
    State.DRAFT: {State.BACKTESTED, State.RETIRED},
    State.BACKTESTED: {State.PAPER, State.REJECTED, State.RETIRED},
    State.PAPER: {State.LIVE_ELIGIBLE, State.REJECTED, State.RETIRED},
    State.LIVE_ELIGIBLE: {State.RETIRED},
    State.REJECTED: {State.RETIRED},
    State.RETIRED: set(),
}

PROMOTION_EVENT = "promotion"


class TransitionError(ValueError):
    pass


class Registry:
    def __init__(self, path: str | Path | None = None, journal: Journal | None = None):
        self.path = Path(path) if path else paths.registry_path()
        self.journal = journal

    def _load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text() or "{}") if self.path.is_file() else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
        tmp.replace(self.path)

    def all(self) -> dict[str, dict[str, Any]]:
        return self._load()

    def state(self, name: str) -> State | None:
        rec = self._load().get(name)
        return State(rec["state"]) if rec else None

    def record(self, name: str) -> dict[str, Any] | None:
        return self._load().get(name)

    def register(self, name: str, at: datetime | None = None) -> None:
        data = self._load()
        if name not in data:
            ts = (at or datetime.now(timezone.utc)).isoformat()
            data[name] = {"state": State.DRAFT.value, "history": [], "metrics": {}, "created_at": ts}
            self._save(data)

    def transition(self, name: str, to: State, metrics: dict | None = None, reasons: list[str] | None = None,
                   at: datetime | None = None, **fields) -> None:
        data = self._load()
        if name not in data:
            raise TransitionError(f"unknown strategy {name!r}")
        current = State(data[name]["state"])
        to = State(to)
        if to not in ALLOWED[current]:
            raise TransitionError(f"{name}: {current.value} → {to.value} is not allowed")
        ts = (at or datetime.now(timezone.utc))
        entry = {"ts": ts.isoformat(), "from": current.value, "to": to.value, "metrics": metrics or {},
                 "reasons": reasons or []}
        data[name]["history"].append(entry)
        data[name]["state"] = to.value
        data[name]["metrics"] = metrics or data[name]["metrics"]
        data[name].update(fields)
        self._save(data)
        if self.journal is not None:
            self.journal.record_event(PROMOTION_EVENT, {"strategy": name, **entry}, ts=ts)
