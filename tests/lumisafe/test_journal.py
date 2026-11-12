"""Journal invariants J1 (append-only, SQLite-trigger enforced) and J2 (one entry per intent)."""

import sqlite3
from datetime import timedelta

import pytest

from lumisafe.journal import Journal, JournalEntry, new_entry_id
from lumisafe.types import ReviewResult, Side, Source, Verdict

from .helpers import T0, intent


def make_entry(final=Verdict.APPROVE, **kw):
    it = kw.pop("intent", None) or intent(**kw)
    return JournalEntry(
        entry_id=new_entry_id(), ts=it.created_at, intent=it,
        reviews=[ReviewResult("coach", Verdict.APPROVE, "ok"), ReviewResult("risk", final, "ok")],
        final=final, fill_price=None, fill_qty=None, shadow_return_20d=None,
    )


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j.db")


def test_record_and_query_round_trip(journal):
    e = make_entry()
    journal.record(e)
    [got] = journal.query()
    assert got == e


def test_default_path_is_under_lumisafe_home(_isolated_lumisafe_home):
    j = Journal()
    assert j.path == _isolated_lumisafe_home / "journal.db"


def test_query_filters(journal):
    a = make_entry(symbol="VTI", source=Source.CORE)
    b = make_entry(final=Verdict.BLOCK, symbol="AAPL", source=Source.USER, at=T0 + timedelta(days=1))
    c = make_entry(final=Verdict.VETO, symbol="QQQ", source=Source.SATELLITE, at=T0 + timedelta(days=2))
    for e in (a, b, c):
        journal.record(e)
    assert [e.entry_id for e in journal.query(rejected=True)] == [b.entry_id, c.entry_id]
    assert [e.entry_id for e in journal.query(submitted=True)] == [a.entry_id]
    assert [e.entry_id for e in journal.query(symbol="AAPL")] == [b.entry_id]
    assert [e.entry_id for e in journal.query(source=Source.SATELLITE)] == [c.entry_id]
    assert [e.entry_id for e in journal.query(since=T0 + timedelta(hours=1))] == [b.entry_id, c.entry_id]
    assert [e.entry_id for e in journal.query(limit=1)] == [c.entry_id]
    assert [e.entry_id for e in journal.query(side=Side.BUY, final=[Verdict.APPROVE, Verdict.VETO])] == [
        a.entry_id, c.entry_id]
    with pytest.raises(TypeError):
        journal.query(colour="red")


# --- J1 -------------------------------------------------------------------------------

@pytest.mark.parametrize("column,value", [
    ("final", "approve"), ("intent_json", "{}"), ("reviews_json", "[]"), ("symbol", "X"),
    ("ts", "2000-01-01"), ("intent_id", "other"), ("source", "core"), ("side", "sell"), ("entry_id", "new"),
])
def test_j1_trigger_rejects_update_of_immutable_columns(journal, column, value):
    e = make_entry(final=Verdict.BLOCK)
    journal.record(e)
    with sqlite3.connect(journal.path) as conn, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE entries SET {column} = ? WHERE entry_id = ?", (value, e.entry_id))
    assert journal.query()[0] == e


def test_j1_trigger_rejects_delete(journal):
    e = make_entry()
    journal.record(e)
    with sqlite3.connect(journal.path) as conn, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM entries")
    assert len(journal.query()) == 1


def test_j1_fill_and_shadow_can_each_be_set_once(journal):
    e = make_entry()
    journal.record(e)
    journal.update_fill(e.entry_id, 101.5, 2)
    journal.update_shadow(e.entry_id, -0.03)
    got = journal.query()[0]
    assert (got.fill_price, got.fill_qty, got.shadow_return_20d) == (101.5, 2.0, -0.03)
    with pytest.raises(sqlite3.IntegrityError):
        journal.update_fill(e.entry_id, 99.0, 2)
    with pytest.raises(sqlite3.IntegrityError):
        journal.update_shadow(e.entry_id, 0.5)
    with sqlite3.connect(journal.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE entries SET fill_price = NULL")
    assert journal.query()[0].fill_price == 101.5


def test_j1_update_unknown_entry_raises(journal):
    with pytest.raises(KeyError):
        journal.update_fill("nope", 1, 1)
    with pytest.raises(KeyError):
        journal.update_shadow("nope", 1)


def test_j1_events_are_append_only(journal):
    ev = journal.record_event("halt", {"why": "R7"})
    assert journal.events("halt")[0].payload == {"why": "R7"}
    with sqlite3.connect(journal.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE events SET kind = 'x'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM events")
    assert [e.event_id for e in journal.events()] == [ev.event_id]


def test_j1_triggers_survive_reopen(journal):
    e = make_entry()
    journal.record(e)
    reopened = Journal(journal.path)
    with sqlite3.connect(reopened.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM entries")


# --- J2 (storage level; the pipeline-level half is tested with the runner) -------------

def test_j2_one_entry_per_intent(journal):
    it = intent()
    journal.record(make_entry(intent=it))
    with pytest.raises(sqlite3.IntegrityError):
        journal.record(make_entry(intent=it, final=Verdict.BLOCK))
    assert len(journal.query(intent_id=it.intent_id)) == 1
