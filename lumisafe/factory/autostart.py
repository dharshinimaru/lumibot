"""Start the factory automatically on macOS (launchd), so it never misses a market day.

The job runs ``python -m lumisafe factory run`` from the repo folder (so ``.env`` is found the same
way as when you run it by hand), restarts it if it crashes, starts it at login, and wraps it in
``caffeinate -is`` so the Mac doesn't idle-sleep while it runs (closing the lid on battery still
sleeps the Mac; plugged in, it stays awake).

The plist is generated on the owner's machine from the running interpreter and folder. Nothing
machine-specific is stored in the repo.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from lumisafe import paths

LABEL = "com.lumisafe.factory"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    d = paths.home() / "factory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "run.log"


def build_plist(python: str, workdir: str, log: str, home: str | None = None) -> dict:
    env = {"PYTHONUNBUFFERED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if home:
        env["LUMISAFE_HOME"] = home
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", python, "-m", "lumisafe", "factory", "run"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "KeepAlive": True,  # restart after a crash
        "ThrottleInterval": 60,  # never restart faster than once a minute
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def install(workdir: Path | None = None) -> Path:
    workdir = (workdir or Path.cwd()).resolve()
    if not (workdir / "lumisafe").is_dir():
        raise RuntimeError(f"run this from the repo folder (no lumisafe/ in {workdir})")
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = build_plist(sys.executable, str(workdir), str(log_path()), os.environ.get("LUMISAFE_HOME"))
    domain = f"gui/{os.getuid()}"
    if p.is_file():
        _launchctl("bootout", f"{domain}/{LABEL}")  # replace an older job cleanly
    p.write_bytes(plistlib.dumps(data))
    r = _launchctl("bootstrap", domain, str(p))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
    return p


def uninstall() -> bool:
    p = plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if p.is_file():
        p.unlink()  # our own generated file; the decisions/books are untouched
        return True
    return False


def status() -> str:
    if not plist_path().is_file():
        return "not installed"
    r = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if r.returncode != 0:
        return "installed but not loaded"
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("state ="):
            pid = next((x.split("=")[1].strip() for x in r.stdout.splitlines() if x.strip().startswith("pid =")), None)
            return f"{line.split('=')[1].strip()}" + (f" (pid {pid})" if pid else "")
    return "loaded"
