"""AIAdvisor A1–A5 (SPEC §5.5). The LLM is always mocked (no network)."""

import ast
import inspect
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

import lumisafe
from lumisafe import paths
from lumisafe.advisor import advisor as advisor_mod
from lumisafe.advisor.advisor import (
    TIMEOUT_SECONDS, AdvisorContext, AIAdvisor, AnthropicLLMClient, build_advisor_context, fallback_note,
    update_shadow_returns,
)
from lumisafe.advisor.schema import AdvisorOutput, json_schema
from lumisafe.config.schema import LumiSafeConfig, save_config
from lumisafe.journal import Journal, JournalEntry, new_entry_id
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.reporting import cli
from lumisafe.runner import StateStore
from lumisafe.types import ReviewResult, Side, Source, Verdict

from .helpers import T0, intent, snapshot

GOOD = json.dumps({"verdict": "approve", "reasons": ["diversified"], "confidence": 0.7,
                   "plain_english": "Looks consistent with your plan."})
VETO = json.dumps({"verdict": "veto", "reasons": ["chasing"], "confidence": 0.8,
                   "plain_english": "This looks like chasing a hot stock."})


class FakeLLM:
    def __init__(self, reply=GOOD, delay=0.0, error=None):
        self.reply, self.delay, self.error = reply, delay, error
        self.calls = []

    def complete(self, system, user, *, json_schema, timeout):
        self.calls.append({"system": system, "user": user, "json_schema": json_schema, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


CTX = AdvisorContext(symbol="AAPL", side="buy", quantity=3, source="user", reason="I like it",
                     recent_closes=[100.0, 101.0], position_qty=0)


# --- A1 ------------------------------------------------------------------------------------------

def test_a1_only_approve_or_veto_never_resize():
    assert AIAdvisor(FakeLLM(GOOD), True).review(intent("AAPL", source=Source.USER), CTX).verdict is Verdict.APPROVE
    r = AIAdvisor(FakeLLM(VETO), True).review(intent("AAPL", source=Source.USER), CTX)
    assert r.verdict is Verdict.VETO and r.adjusted_quantity is None and "chasing" in r.explanation
    for bad in ({"verdict": "resize", "reasons": ["x"], "confidence": 0.5, "plain_english": "x"},
                {"verdict": "approve", "reasons": ["x"], "confidence": 0.5, "plain_english": "x", "quantity": 99},
                {"verdict": "approve", "reasons": ["x"], "confidence": 2, "plain_english": "x"}):
        with pytest.raises(Exception):
            AdvisorOutput.model_validate_json(json.dumps(bad))
    assert json_schema()["properties"]["verdict"]["enum"] == ["approve", "veto"]


def test_a1_advisor_has_no_trading_access():
    root = Path(lumisafe.__file__).parent / "advisor"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    assert not any(x in m for x in ("adapters", "runner", "lumibot", "risk", "config")), (path, m)
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in ("submit", "submit_order", "create_order", "save_config"), (path, name)


def test_a1_production_client_sends_no_tools_and_strict_json():
    sent = {}

    class Resp:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": GOOD})()]

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return Resp()

    fake = type("C", (), {"beta": type("B", (), {"messages": Msgs()})()})()
    out = AnthropicLLMClient(client=fake).complete("sys", "usr", json_schema=json_schema(), timeout=30)
    assert out == GOOD
    assert "tools" not in sent and "tool_choice" not in sent
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"


def test_a1_production_client_raises_on_refusal():
    class Resp:
        stop_reason = "refusal"
        content = []

    fake = type("C", (), {"beta": type("B", (), {"messages": type("M", (), {"create": lambda self, **k: Resp()})()})()})()
    with pytest.raises(RuntimeError):
        AnthropicLLMClient(client=fake).complete("s", "u", json_schema=None, timeout=1)


# --- A2 --------------------------------------------------------------------------------------------

def test_a2_core_intents_never_reach_the_llm():
    llm = FakeLLM(VETO)
    r = AIAdvisor(llm, True).review(intent("VTI", source=Source.CORE), CTX)
    assert r.verdict is Verdict.APPROVE and r.explanation == "core plan" and llm.calls == []


# --- A3 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("llm", [FakeLLM("not json"), FakeLLM('{"verdict": "approve"}'),
                                 FakeLLM(error=ConnectionError("down")), None])
def test_a3_failures_veto_satellite_and_warn_user(llm):
    adv = AIAdvisor(llm, True)
    sat = adv.review(intent("QQQ", source=Source.SATELLITE), CTX)
    assert sat.verdict is Verdict.VETO and sat.explanation == "advisor unavailable"
    usr = adv.review(intent("AAPL", source=Source.USER), CTX)
    assert usr.verdict is Verdict.WARN and "advisor unavailable" in usr.explanation


def test_a3_timeout_over_30_seconds():
    assert TIMEOUT_SECONDS == 30.0 and AIAdvisor.timeout_s == 30.0
    adv = AIAdvisor(FakeLLM(GOOD, delay=1.0), True)
    adv.timeout_s = 0.1  # same code path as the 30 s limit, scaled down
    t = time.monotonic()
    assert adv.review(intent("QQQ", source=Source.SATELLITE), CTX).verdict is Verdict.VETO
    assert adv.review(intent("AAPL", source=Source.USER), CTX).verdict is Verdict.WARN
    assert time.monotonic() - t < 0.9


def test_a3_missing_context_fails_closed_and_disabled_advisor_approves():
    assert AIAdvisor(FakeLLM(), True).review(intent("QQQ", source=Source.SATELLITE), None).verdict is Verdict.VETO
    assert AIAdvisor(None, False).review(intent("QQQ", source=Source.SATELLITE), CTX).verdict is Verdict.APPROVE


# --- A4 --------------------------------------------------------------------------------------------

def test_a4_no_secrets_or_account_numbers_in_prompts(tmp_path, monkeypatch):
    secrets = {"ALPACA_API_KEY": "PKSECRETKEY123", "ALPACA_API_SECRET": "supersecretvalue987",
               "ANTHROPIC_API_KEY": "sk-ant-abcdef", "ALPACA_ACCOUNT_NUMBER": "PA3ACCT0001"}
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    j = Journal(tmp_path / "j.db")
    snap = snapshot(cash=10_000, positions={"AAPL": 2}, prices={"AAPL": 100.0})
    ctx = build_advisor_context(intent("AAPL", source=Source.USER), snap, j,
                                lambda s, a: pd.Series(range(1, 40), dtype=float), headlines=["h1"])
    llm = FakeLLM()
    adv = AIAdvisor(llm, True)
    adv.review(intent("AAPL", source=Source.USER), ctx)
    adv.daily_note(snap, [], "Your return: +1%")
    blob = json.dumps(llm.calls)
    for v in secrets.values():
        assert v not in blob
    keys = set(json.loads(llm.calls[0]["user"].split("\n", 1)[1]))
    assert keys == {"trade", "recent_daily_closes", "current_position", "past_decisions_on_this_symbol",
                    "recent_headlines"}
    assert len(ctx.recent_closes) == 20


def test_a4_prompts_live_only_in_prompts_module():
    root = Path(lumisafe.__file__).parent
    for path in root.rglob("*.py"):
        if path.name == "prompts.py":
            continue
        assert "You are a" not in path.read_text(), path
    assert "prompts.REVIEW_SYSTEM" in inspect.getsource(advisor_mod)


# --- A5 --------------------------------------------------------------------------------------------

def test_a5_shadow_return_recorded_20_trading_days_after_a_veto(tmp_path):
    j = Journal(tmp_path / "j.db")
    i = intent("MEME", source=Source.USER, at=T0)
    e = JournalEntry(new_entry_id(), T0, i, [ReviewResult("advisor", Verdict.VETO, "no")], Verdict.VETO,
                     None, None, None)
    j.record(e)
    approved = intent("VTI", source=Source.USER, at=T0)
    j.record(JournalEntry(new_entry_id(), T0, approved, [ReviewResult("risk", Verdict.APPROVE, "ok")],
                          Verdict.APPROVE, None, None, None))
    idx = pd.bdate_range(T0.date(), periods=40)
    closes = pd.Series([100.0] * 20 + [110.0] * 20, index=idx)
    assert update_shadow_returns(j, lambda s: closes, T0 + timedelta(days=10)) == 0  # too early
    assert update_shadow_returns(j, lambda s: closes, T0 + timedelta(days=60)) == 1
    got = {x.intent.symbol: x.shadow_return_20d for x in j.query()}
    assert got["MEME"] == pytest.approx(0.10) and got["VTI"] is None
    assert update_shadow_returns(j, lambda s: closes, T0 + timedelta(days=60)) == 0  # set once


# --- daily note + CLI ---------------------------------------------------------------------------------

def test_daily_note_uses_llm_and_falls_back():
    snap = snapshot(cash=500, positions={"VTI": 10}, prices={"VTI": 100.0})
    assert AIAdvisor(FakeLLM("All calm today."), True).daily_note(snap, []) == "All calm today."
    note = AIAdvisor(FakeLLM(error=OSError()), True).daily_note(snap, [], "Your return: +1%")
    assert "$1,500.00" in note and "Your return" in note
    assert "No trades recently" in fallback_note({"date": "2026-01-01", "account_value": 1, "cash": 1})


def test_today_command_in_paper_mode(monkeypatch):
    save_config(LumiSafeConfig.model_validate({"core": {"etfs": {"VTI": 1.0}, "monthly_contribution_usd": 0}}),
                paths.config_path())
    assert CliRunner().invoke(cli.main, ["today"]).exit_code == 1
    st = StateStore(paths.state_path())
    st.set("last_snapshot", {"as_of": "2026-01-08T15:00:00+00:00", "cash": 10.0, "equity": 1010.0,
                             "positions": {"VTI": 10}, "prices": {"VTI": 100.0}})
    st.save()
    Journal().record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "equity": 1000.0},
                           ts=datetime(2026, 1, 7, tzinfo=timezone.utc))
    llm = FakeLLM("Your plan held steady.")
    monkeypatch.setattr(cli, "advisor_factory", lambda cfg: AIAdvisor(llm, True))
    monkeypatch.setattr(cli, "history_provider", lambda *a, **k: {})
    res = CliRunner().invoke(cli.main, ["today"])
    assert res.exit_code == 0, res.output
    assert "Your plan held steady." in res.output and lumisafe.DISCLAIMER in res.output
