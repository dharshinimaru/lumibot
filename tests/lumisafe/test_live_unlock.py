"""SPEC §10 live gate: each of the four conditions alone is insufficient; all four pass."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from lumisafe import live_unlock
from lumisafe.journal import Journal, JournalEntry, new_entry_id
from lumisafe.journal.journal import PAPER_CYCLE_EVENT
from lumisafe.live_unlock import (
    PHRASE, LiveAuthorization, LiveUnlockError, is_authorized, qualifying_paper_months, unlock,
)
from lumisafe.types import ReviewResult, Source, Verdict

from .helpers import intent

NOW = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)


class FakeStdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def fill_paper_month(journal, year=2026, month=2, satellite=False, core_fill=True, sat_trade=False,
                     days=range(2, 27)):
    for d in days:
        ts = datetime(year, month, d, 15, tzinfo=timezone.utc)
        journal.record_event(PAPER_CYCLE_EVENT, {"mode": "paper", "satellite_enabled": satellite}, ts=ts)
    ts = datetime(year, month, 3, 15, tzinfo=timezone.utc)
    if core_fill:
        it = intent(source=Source.CORE, at=ts)
        e = JournalEntry(new_entry_id(), ts, it, [ReviewResult("risk", Verdict.APPROVE, "ok")],
                         Verdict.APPROVE, None, None, None)
        journal.record(e)
        journal.update_fill(e.entry_id, 100.0, 1.0)
    if sat_trade:
        it = intent(source=Source.SATELLITE, at=ts, symbol="QQQ")
        journal.record(JournalEntry(new_entry_id(), ts, it, [ReviewResult("risk", Verdict.APPROVE, "ok")],
                                    Verdict.APPROVE, None, None, None))


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ALPACA_IS_PAPER=true\n")
    return p


def attempt(journal, *, tty, phrase, env, dotenv):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return PHRASE if phrase else "i understand"

    return unlock(
        journal,
        stdin=FakeStdin(tty),
        environ={"LUMISAFE_LIVE": "1"} if env else {},
        dotenv_paths=[dotenv],
        ask=ask,
        now=NOW,
    ), asked


CONDITIONS = ["tty", "phrase", "env", "history"]


def _run(tmp_path, env_file, enabled: set[str]):
    journal = Journal(tmp_path / f"j_{'_'.join(sorted(enabled)) or 'none'}.db")
    if "history" in enabled:
        fill_paper_month(journal)
    return attempt(journal, tty="tty" in enabled, phrase="phrase" in enabled, env="env" in enabled,
                   dotenv=env_file)


@pytest.mark.parametrize("only", CONDITIONS)
def test_each_condition_alone_is_insufficient(tmp_path, env_file, only):
    with pytest.raises(LiveUnlockError):
        _run(tmp_path, env_file, {only})


@pytest.mark.parametrize("missing", CONDITIONS)
def test_any_three_conditions_are_insufficient(tmp_path, env_file, missing):
    with pytest.raises(LiveUnlockError) as exc:
        _run(tmp_path, env_file, set(CONDITIONS) - {missing})
    expected = {"tty": "TTY", "phrase": "phrase", "env": "LUMISAFE_LIVE", "history": "paper trading"}[missing]
    assert any(expected in f for f in exc.value.failures)
    # Without a TTY the phrase is never asked, so that case reports both reasons.
    assert len(exc.value.failures) == (2 if missing == "tty" else 1)


def test_no_subset_short_of_all_four_unlocks(tmp_path, env_file):
    for r in range(0, 4):
        for subset in itertools.combinations(CONDITIONS, r):
            with pytest.raises(LiveUnlockError):
                _run(tmp_path, env_file, set(subset))


def test_all_four_together_pass(tmp_path, env_file):
    auth, asked = _run(tmp_path, env_file, set(CONDITIONS))
    assert is_authorized(auth)
    assert asked, "the human must be prompted for the phrase"
    assert auth.allowed_sources == ("core", "satellite")
    assert auth.allowed_satellite_states == ("LIVE_ELIGIBLE",)


def test_phrase_is_not_even_asked_without_a_tty(tmp_path, env_file):
    journal = Journal(tmp_path / "j.db")
    fill_paper_month(journal)
    with pytest.raises(LiveUnlockError):
        _, asked = attempt(journal, tty=False, phrase=True, env=True, dotenv=env_file)
    # attempt raised before returning; re-run capturing prompts explicitly
    prompts = []
    with pytest.raises(LiveUnlockError):
        unlock(journal, stdin=FakeStdin(False), environ={"LUMISAFE_LIVE": "1"}, dotenv_paths=[env_file],
               ask=lambda p: prompts.append(p) or PHRASE, now=NOW)
    assert prompts == []


@pytest.mark.parametrize("typed", [PHRASE.lower(), PHRASE + ".", " " + PHRASE, "", None])
def test_phrase_must_be_exact(typed):
    assert live_unlock.check_phrase(typed) is False
    assert live_unlock.check_phrase(PHRASE + "\n") is True


def test_env_var_in_dotenv_file_is_refused(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=1\nexport LUMISAFE_LIVE=1\n")
    ok, msg = live_unlock.check_env({"LUMISAFE_LIVE": "1"}, [p])
    assert not ok and ".env" in msg


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "1 "])
def test_env_var_must_be_exactly_1(tmp_path, value):
    ok, _ = live_unlock.check_env({"LUMISAFE_LIVE": value}, [tmp_path / "missing.env"])
    assert not ok


def test_authorization_cannot_be_forged():
    with pytest.raises(PermissionError):
        LiveAuthorization(granted_at=NOW)
    with pytest.raises(PermissionError):
        LiveAuthorization(granted_at=NOW, _key=object())
    assert not is_authorized(object())
    assert not is_authorized(None)


def test_tty_check_is_fail_closed():
    class Broken:
        def isatty(self):
            raise OSError

    assert live_unlock.check_tty(Broken()) is False
    assert live_unlock.check_tty(None) is False


# --- condition 4 details ------------------------------------------------------------

def test_history_requires_a_completed_month(tmp_path):
    j = Journal(tmp_path / "j.db")
    fill_paper_month(j, year=2026, month=3, days=range(1, 10))  # current month: not complete
    assert qualifying_paper_months(j, NOW) == []


@pytest.mark.parametrize("kwargs", [
    {"satellite": True},
    {"core_fill": False},
    {"sat_trade": True},
    {"days": range(10, 27)},       # started late in the month
    {"days": range(1, 15)},        # stopped early
    {"days": range(1, 28, 3)},     # too few days
])
def test_history_rejects_non_full_or_non_core_only_months(tmp_path, kwargs):
    j = Journal(tmp_path / "j.db")
    fill_paper_month(j, **kwargs)
    assert qualifying_paper_months(j, NOW) == []


def test_history_ignores_backtest_heartbeats(tmp_path):
    j = Journal(tmp_path / "j.db")
    for d in range(1, 28):
        j.record_event(PAPER_CYCLE_EVENT, {"mode": "backtest", "satellite_enabled": False},
                       ts=datetime(2026, 2, d, tzinfo=timezone.utc))
    fill_paper_month(j, days=[])
    assert qualifying_paper_months(j, NOW) == []


def test_history_accepts_a_full_core_only_month(tmp_path):
    j = Journal(tmp_path / "j.db")
    fill_paper_month(j)
    assert qualifying_paper_months(j, NOW) == [(2026, 2)]
    assert live_unlock.check_paper_history(j, NOW) is True
    assert live_unlock.check_paper_history(j, datetime(2026, 2, 28, tzinfo=timezone.utc)) is False
