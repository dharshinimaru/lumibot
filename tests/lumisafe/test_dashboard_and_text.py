"""Dashboard (read-only), disclaimers and the banned-words grep (SPEC §9, §11)."""

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import lumisafe
from lumisafe import DISCLAIMER, paths
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.journal import Journal, JournalEntry, new_entry_id
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.reporting import dashboard
from lumisafe.runner import StateStore
from lumisafe.types import ReviewResult, Side, Source, Verdict

from .helpers import T0, intent

ROOT = Path(lumisafe.__file__).parent
REPO = ROOT.parent


def seed():
    save_config(LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 0.6, "VXUS": 0.2, "BND": 0.2},
                                                        "monthly_contribution_usd": 100}}), paths.config_path())
    j = Journal()
    for d, eq in enumerate([1000, 1010, 1030]):
        j.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "equity": eq},
                       ts=datetime(2026, 1, 5 + d, tzinfo=timezone.utc))
    for final, side, shadow in ((Verdict.VETO, Side.BUY, -0.04), (Verdict.BLOCK, Side.SELL, 0.02),
                                (Verdict.APPROVE, Side.BUY, None)):
        i = intent("AAPL", side, 1, source=Source.USER, at=T0)
        e = JournalEntry(new_entry_id(), T0, i, [ReviewResult("risk", final, "why")], final, None, None, None)
        j.record(e)
        if shadow is not None:
            j.update_shadow(e.entry_id, shadow)
    st = StateStore(paths.state_path())
    st.set("last_snapshot", {"as_of": "2026-01-07T15:00:00+00:00", "cash": 100.0, "equity": 1030.0,
                             "positions": {"VTI": 5}, "prices": {"VTI": 100.0}})
    st.set("kill_switch", {"satellite_latched": True})
    st.save()


def fake_history(symbols, start=None, end=None):
    idx = pd.bdate_range("2026-01-01", periods=10)
    return {s: pd.DataFrame({"close": np.linspace(100, 105, 10)}, index=idx) for s in symbols}


def test_dashboard_data_has_benchmark_headline_and_panels():
    seed()
    data = dashboard.load_data(fake_history)
    assert data["headline"].startswith("Your return: +3.00% · If you'd just held SPY:")
    assert list(data["allocation"]["fund"]) == ["VTI", "VXUS", "BND"]
    assert data["halts"]["satellite_latched"] is True
    assert len(data["journal"]) == 3
    vq = data["veto_quality"].set_index(["held back by", "side"])
    assert vq.loc[("AI advisor", "buy"), "avg 20-day price move after"] == -0.04
    assert data["disclaimer"] == DISCLAIMER


def test_dashboard_renders_headless_with_disclaimer_footer(monkeypatch):
    from streamlit.testing.v1 import AppTest

    seed()
    monkeypatch.setattr(dashboard, "default_history", fake_history)
    at = AppTest.from_string(
        "from lumisafe.reporting import dashboard\n"
        "import streamlit as st\n"
        "dashboard.render(st, dashboard.load_data(dashboard.default_history))\n"
    )
    at.run(timeout=30)
    assert not at.exception
    assert any("If you'd just held" in h.value for h in at.header)
    assert any(DISCLAIMER in c.value for c in at.caption)


def test_dashboard_without_any_data():
    data = dashboard.load_data(fake_history)
    assert data["config"] is None and data["headline"] is None and data["journal"].empty
    assert data["veto_quality"].empty


def test_dashboard_is_read_only():
    tree = ast.parse((ROOT / "reporting" / "dashboard.py").read_text())
    forbidden_calls = {"submit", "submit_order", "create_order", "record", "record_event", "update_fill",
                       "update_shadow", "push", "save", "save_config", "set", "transition", "write_text",
                       "run_paper_cycle", "paper_runner"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden_calls, name
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("lumisafe.runner",), node.module


# --- §11 disclaimers ------------------------------------------------------------------------------

def test_disclaimer_text_and_code_paths():
    assert DISCLAIMER == "Educational software. Not financial, tax, or legal advice. Investments can lose value."
    cli_src = (ROOT / "reporting" / "cli.py").read_text()
    for cmd in ("def init(", "def status("):
        body = cli_src[cli_src.index(cmd):]
        body = body[: body.index("\n@main.command")] if "\n@main.command" in body else body
        assert "DISCLAIMER" in body, cmd
    assert "st.caption(data[\"disclaimer\"])" in (ROOT / "reporting" / "dashboard.py").read_text()


# --- §11 banned words -------------------------------------------------------------------------------

# Assembled from pieces so this file doesn't itself contain the banned words.
_RISK, _FREE, _LOSE = "ri" + "sk", "fr" + "ee", "lo" + "se"
BANNED = re.compile(rf"guarantee" + rf"d|{_RISK}[- ]{_FREE}|can['’]?t {_LOSE}|can" + rf"not {_LOSE}", re.IGNORECASE)


def lumisafe_text_files():
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".md", ".txt", ".json"}]
    files += [p for p in (REPO / "tests" / "lumisafe").rglob("*.py")]
    files += [REPO / "DECISIONS.md", REPO / "requirements_lumisafe.txt", REPO / "docs" / "LUMISAFE.md"]
    files += list((REPO / "reports" / "lumisafe").glob("*.json"))
    return [p for p in files if p.is_file()]


def lumisafe_readme_sections():
    for p in REPO.glob("README*.md"):
        text = p.read_text(encoding="utf-8")
        m = re.search(r"<!-- lumisafe:start -->(.*?)<!-- lumisafe:end -->", text, flags=re.S)
        if m:
            yield p.name, m.group(1)


def test_no_banned_words_in_any_lumisafe_code_string_or_doc():
    hits = []
    for p in lumisafe_text_files():
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if BANNED.search(line):
                hits.append(f"{p.relative_to(REPO)}:{n}")
    for name, section in lumisafe_readme_sections():
        if BANNED.search(section):
            hits.append(name)
    assert hits == []


def test_banned_word_pattern_catches_variants():
    for s in ("Guarantee" + "d returns", _RISK + "-" + _FREE, _RISK + " " + _FREE, "you can't " + _LOSE,
              "you can’t " + _LOSE, "you can" + "not " + _LOSE):
        assert BANNED.search(s)
    assert not BANNED.search("past results don't guarantee future ones")


def test_readme_has_lumisafe_quickstart_in_10_commands_or_fewer():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = dict(lumisafe_readme_sections())["README.md"]
    assert "## LumiSafe quickstart" in section and DISCLAIMER in section
    block = re.search(r"```bash\n(.*?)```", section, flags=re.S).group(1)
    commands = [l for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert 0 < len(commands) <= 10, commands
    assert "lumisafe init" in block and "lumisafe run-paper" in block
    assert text.count("<!-- lumisafe:start -->") == 1
    for lang in ("zh-CN", "es", "fr", "de", "ja", "ko", "pt", "ru"):
        loc = (REPO / f"README.{lang}.md").read_text(encoding="utf-8")
        assert "README.md#lumisafe-quickstart" in loc, lang
