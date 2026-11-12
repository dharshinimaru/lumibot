"""Order flow (SPEC §4): coach → tax → advisor → risk → submit → journal; J2; AST caller check."""

import ast
from pathlib import Path

import pytest

import lumisafe
from lumisafe.config.schema import LumiSafeConfig, RiskConfig
from lumisafe.journal import Journal
from lumisafe.risk.gate import RiskGate
from lumisafe.runner import Pipeline, PipelineContext
from lumisafe.types import ReviewResult, Side, Source, Verdict

from .fakes import FakeExecutor
from .helpers import T0, intent, snapshot

PRICES = {"VTI": 100.0, "AAPL": 100.0}


class Recorder:
    def __init__(self, calls, name, verdict=Verdict.APPROVE, adjusted=None):
        self.calls, self.name, self.verdict, self.adjusted = calls, name, verdict, adjusted

    def review(self, intent, *args):
        self.calls.append(self.name)
        return ReviewResult(self.name, self.verdict, f"{self.name} says {self.verdict.value}", self.adjusted)


class RecordingGate(RiskGate):
    def __init__(self, calls, verdict=None, adjusted=None):
        super().__init__(RiskConfig(), {"VTI"})
        self.calls, self.forced, self.adjusted = calls, verdict, adjusted

    def check(self, intent, snap):
        self.calls.append("risk")
        if self.forced:
            return ReviewResult("risk", self.forced, "forced", self.adjusted)
        return super().check(intent, snap)


class RecordingExecutor(FakeExecutor):
    def __init__(self, calls, **kw):
        super().__init__(T0, 100_000, PRICES, **kw)
        self.calls = calls

    def submit_order(self, order, reason=None):
        self.calls.append("submit")
        return super().submit_order(order, reason)


class RecordingJournal(Journal):
    def __init__(self, path, calls):
        super().__init__(path)
        self.calls = calls

    def record(self, entry):
        self.calls.append("journal")
        super().record(entry)


def build(tmp_path, calls, coach=Verdict.APPROVE, tax=Verdict.APPROVE, advisor=Verdict.APPROVE, gate=None,
          adjusted=None, **exec_kw):
    journal = RecordingJournal(tmp_path / "j.db", calls)
    p = Pipeline(journal, RecordingGate(calls, gate, adjusted), coach=Recorder(calls, "coach", coach),
                 tax=Recorder(calls, "tax", tax), advisor=Recorder(calls, "advisor", advisor))
    return p, journal, RecordingExecutor(calls, **exec_kw)


SNAP = snapshot(cash=100_000, prices=PRICES)


def test_full_order_flow_in_spec_order(tmp_path):
    calls = []
    p, journal, ex = build(tmp_path, calls)
    out = p.run(intent("VTI", qty=2), SNAP, ex)
    assert calls == ["coach", "tax", "advisor", "risk", "submit", "journal"]
    assert out.submitted and out.entry.final is Verdict.APPROVE
    assert [r.reviewer for r in journal.query()[0].reviews] == ["coach", "tax", "advisor", "risk"]


@pytest.mark.parametrize("stage,verdict,expected_calls,final", [
    ("coach", Verdict.DELAY, ["coach", "journal"], Verdict.DELAY),
    ("tax", Verdict.BLOCK, ["coach", "tax", "journal"], Verdict.BLOCK),
    ("advisor", Verdict.VETO, ["coach", "tax", "advisor", "journal"], Verdict.VETO),
    ("gate", Verdict.BLOCK, ["coach", "tax", "advisor", "risk", "journal"], Verdict.BLOCK),
])
def test_rejections_stop_before_submission_and_are_journaled(tmp_path, stage, verdict, expected_calls, final):
    calls = []
    p, journal, ex = build(tmp_path, calls, **{stage: verdict})
    out = p.run(intent("VTI", qty=2), SNAP, ex)
    assert calls == expected_calls
    assert not out.submitted and ex.submitted == []
    assert journal.query()[0].final is final


def test_warnings_continue_to_risk_gate(tmp_path):
    calls = []
    p, _, ex = build(tmp_path, calls, tax=Verdict.WARN, advisor=Verdict.WARN)
    assert p.run(intent("VTI", qty=2), SNAP, ex).submitted
    assert calls[-2:] == ["submit", "journal"]


def test_resize_submits_the_smaller_quantity(tmp_path):
    calls = []
    p, journal, ex = build(tmp_path, calls)
    out = p.run(intent("AAPL", qty=100), SNAP, ex)  # 10% of account → resized to 5%
    assert out.entry.final is Verdict.RESIZE and ex.submitted[0].quantity == 50 == out.executed_qty


@pytest.mark.parametrize("bad", [None, 0, 5, 7])
def test_resize_that_breaks_r10_is_never_submitted(tmp_path, bad):
    calls = []
    p, journal, ex = build(tmp_path, calls, gate=Verdict.RESIZE, adjusted=bad)
    out = p.run(intent("VTI", qty=5), SNAP, ex)
    assert not out.submitted and out.entry.final is Verdict.BLOCK


def test_broker_failure_is_journaled_as_block(tmp_path):
    calls = []
    p, journal, ex = build(tmp_path, calls, fail_submit=True)
    out = p.run(intent("VTI", qty=1), SNAP, ex)
    assert not out.submitted and out.entry.final is Verdict.BLOCK
    assert out.entry.reviews[-1].reviewer == "pipeline" and "broker down" in out.entry.reviews[-1].explanation


def test_reviewer_crash_fails_closed(tmp_path):
    class Boom:
        def review(self, *a):
            raise ValueError("nope")

    j = Journal(tmp_path / "j.db")
    ex = FakeExecutor(T0, 100_000, PRICES)
    p = Pipeline(j, RiskGate(RiskConfig(), {"VTI"}), tax=Boom())
    out = p.run(intent("VTI", qty=1), SNAP, ex)
    assert not out.submitted and ex.submitted == [] and j.query()[0].final is Verdict.BLOCK


def test_j2_every_intent_gets_exactly_one_entry(tmp_path):
    j = Journal(tmp_path / "j.db")
    ex = FakeExecutor(T0, 1_000, PRICES)
    p = Pipeline(j, RiskGate(RiskConfig(), {"VTI"}))
    intents = [intent("VTI", qty=1), intent("VTI", qty=1000), intent("AAPL", Side.SELL, 1),
               intent("ZZZ", qty=1), intent("AAPL", qty=0.3, source=Source.SATELLITE)]
    for i in intents:
        p.run(i, snapshot(cash=1_000, prices=PRICES), ex)
    entries = j.query()
    assert sorted(e.intent.intent_id for e in entries) == sorted(i.intent_id for i in intents)


def test_reason_is_passed_to_broker(tmp_path):
    j = Journal(tmp_path / "j.db")
    ex = FakeExecutor(T0, 100_000, PRICES)
    Pipeline(j, RiskGate(RiskConfig(), {"VTI"})).run(intent("VTI", qty=1, reason="core plan"), SNAP, ex)
    assert ex.submitted[0].reason == "core plan" and ex.submitted[0].kw["time_in_force"] == "day"


def test_pipeline_context_defaults():
    ctx = PipelineContext()
    assert ctx.recent_sales == [] and ctx.market is None


# --- AST: only runner.py calls bridge.submit(); only the bridge calls Lumibot order APIs ---------

ROOT = Path(lumisafe.__file__).parent


def _calls(path):
    tree = ast.parse(path.read_text())
    bridge_aliases = {"bridge", "lumibot_bridge"}
    imported_submit = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("lumibot_bridge"):
            imported_submit |= {a.asname or a.name for a in node.names if a.name == "submit"}
        if isinstance(node, ast.ImportFrom) and node.module == "lumisafe.adapters":
            bridge_aliases |= {a.asname or a.name for a in node.names if a.name == "lumibot_bridge"}
        if isinstance(node, ast.Import):
            bridge_aliases |= {a.asname for a in node.names if a.name.endswith("lumibot_bridge") and a.asname}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value
            if f.attr == "submit" and isinstance(base, ast.Name) and base.id in bridge_aliases:
                yield "bridge.submit"
            if f.attr == "submit" and isinstance(base, ast.Attribute) and base.attr == "lumibot_bridge":
                yield "bridge.submit"
            if f.attr in ("submit_order", "create_order", "submit_orders"):
                yield f.attr
        elif isinstance(f, ast.Name) and f.id in imported_submit:
            yield "bridge.submit"
        if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in ("submit", "submit_order"):
            yield "getattr-submit"


def test_only_runner_calls_bridge_submit():
    callers = {}
    for path in ROOT.rglob("*.py"):
        for call in _calls(path):
            callers.setdefault(call, set()).add(str(path.relative_to(ROOT)))
    assert callers.get("bridge.submit") == {"runner.py"}
    assert callers.get("getattr-submit") is None
    for api in ("submit_order", "create_order", "submit_orders"):
        assert callers.get(api, {"adapters/lumibot_bridge.py"}) == {"adapters/lumibot_bridge.py"}, api


def test_runner_submits_only_after_risk_gate_in_source_order():
    src = (ROOT / "runner.py").read_text()
    run_src = src[src.index("    def run(self, intent"):src.index("# --- engine")]
    assert run_src.index("self.gate.check(") < run_src.index("bridge.submit(")
    assert run_src.count("bridge.submit(") == 1
