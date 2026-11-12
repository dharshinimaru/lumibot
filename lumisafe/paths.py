"""Filesystem locations for LumiSafe state.

Everything lives under ``~/.lumisafe`` by default. ``LUMISAFE_HOME`` overrides
the directory so tests and multiple sandboxes never touch the real one.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    root = Path(os.environ.get("LUMISAFE_HOME") or (Path.home() / ".lumisafe"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def journal_path() -> Path:
    return home() / "journal.db"


def config_path() -> Path:
    return home() / "config.yaml"


def registry_path() -> Path:
    return home() / "registry.json"


def state_path() -> Path:
    return home() / "state.json"


def reports_dir() -> Path:
    d = home() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_path() -> Path:
    """User intents waiting for the next paper cycle (`lumisafe propose`)."""
    return home() / "inbox.json"


def pending_path() -> Path:
    """Delayed user intents awaiting `lumisafe confirm` (B4)."""
    return home() / "pending.json"
