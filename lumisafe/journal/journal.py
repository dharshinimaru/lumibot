"""Append-only SQLite journal (SPEC §5.7).

J1: an entry's intent, reviews and final verdict can never be updated or
deleted. ``fill_price``/``fill_qty`` and ``shadow_return_20d`` may each be set
exactly once. SQLite triggers enforce this, so it holds even for code that
bypasses this class.
J2: every intent that reaches the pipeline gets exactly one entry. ``intent_id``
is UNIQUE here, and the runner writes one entry per intent.

Besides order entries, the journal keeps an append-only ``events`` table for
non-order facts: promotion transitions, halts, alerts and paper-cycle
heartbeats (the live gate reads these, SPEC §10 condition 4).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lumisafe import paths
from lumisafe.types import OrderIntent, ReviewResult, Side, Source, Verdict

# Heartbeat the runner writes once per paper/live cycle. Payload:
# {"mode": "paper"|"backtest"|"live", "satellite_enabled": bool}. live_unlock reads it.
PAPER_CYCLE_EVENT = "paper_cycle"

SUBMITTED_VERDICTS = (Verdict.APPROVE, Verdict.RESIZE)
REJECTED_VERDICTS = (Verdict.BLOCK, Verdict.VETO, Verdict.DELAY)


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    ts: datetime
    intent: OrderIntent
    reviews: list[ReviewResult]  # in pipeline order
    final: Verdict  # APPROVE/RESIZE (submitted) or BLOCK/VETO/DELAY (not)
    fill_price: float | None
    fill_qty: float | None
    shadow_return_20d: float | None  # filled later for rejected intents (A5)


@dataclass(frozen=True)
class JournalEvent:
    event_id: str
    ts: datetime
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


def new_entry_id() -> str:
    return uuid.uuid4().hex


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    intent_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    side TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    reviews_json TEXT NOT NULL,
    final TEXT NOT NULL,
    fill_price REAL,
    fill_qty REAL,
    shadow_return_20d REAL
);
CREATE INDEX IF NOT EXISTS entries_symbol ON entries(symbol, ts);

CREATE TRIGGER IF NOT EXISTS entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: DELETE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS entries_immutable_core
BEFORE UPDATE ON entries
WHEN NEW.entry_id IS NOT OLD.entry_id
  OR NEW.ts IS NOT OLD.ts
  OR NEW.intent_id IS NOT OLD.intent_id
  OR NEW.symbol IS NOT OLD.symbol
  OR NEW.source IS NOT OLD.source
  OR NEW.side IS NOT OLD.side
  OR NEW.intent_json IS NOT OLD.intent_json
  OR NEW.reviews_json IS NOT OLD.reviews_json
  OR NEW.final IS NOT OLD.final
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: intent, reviews and final verdict are immutable');
END;

CREATE TRIGGER IF NOT EXISTS entries_fill_once
BEFORE UPDATE OF fill_price, fill_qty ON entries
WHEN (OLD.fill_price IS NOT NULL AND NEW.fill_price IS NOT OLD.fill_price)
  OR (OLD.fill_qty IS NOT NULL AND NEW.fill_qty IS NOT OLD.fill_qty)
BEGIN
    SELECT RAISE(ABORT, 'journal fill fields may be set only once');
END;

CREATE TRIGGER IF NOT EXISTS entries_shadow_once
BEFORE UPDATE OF shadow_return_20d ON entries
WHEN OLD.shadow_return_20d IS NOT NULL AND NEW.shadow_return_20d IS NOT OLD.shadow_return_20d
BEGIN
    SELECT RAISE(ABORT, 'journal shadow return may be set only once');
END;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'journal events are append-only: DELETE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'journal events are append-only: UPDATE forbidden');
END;
"""


# --- (de)serialization ------------------------------------------------------


def _dt(value: datetime | None) -> str | None:
    """ISO string; aware datetimes are normalized to UTC so text order == time order."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def intent_to_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "created_at": _dt(intent.created_at),
        "source": Source(intent.source).value,
        "symbol": intent.symbol,
        "side": Side(intent.side).value,
        "quantity": float(intent.quantity),
        "reason": intent.reason,
        "strategy_name": intent.strategy_name,
    }


def intent_from_dict(d: dict[str, Any]) -> OrderIntent:
    return OrderIntent(
        intent_id=d["intent_id"],
        created_at=_parse_dt(d["created_at"]),
        source=Source(d["source"]),
        symbol=d["symbol"],
        side=Side(d["side"]),
        quantity=float(d["quantity"]),
        reason=d["reason"],
        strategy_name=d.get("strategy_name"),
    )


def review_to_dict(r: ReviewResult) -> dict[str, Any]:
    return {
        "reviewer": r.reviewer,
        "verdict": Verdict(r.verdict).value,
        "explanation": r.explanation,
        "adjusted_quantity": r.adjusted_quantity,
        "release_at": _dt(r.release_at),
    }


def review_from_dict(d: dict[str, Any]) -> ReviewResult:
    return ReviewResult(
        reviewer=d["reviewer"],
        verdict=Verdict(d["verdict"]),
        explanation=d["explanation"],
        adjusted_quantity=d.get("adjusted_quantity"),
        release_at=_parse_dt(d.get("release_at")),
    )


# --- journal ------------------------------------------------------------------


class Journal:
    """Append-only decision journal at ``~/.lumisafe/journal.db`` by default."""

    _FILTERS = {"symbol", "source", "side", "final", "intent_id", "entry_id", "since", "until",
                "rejected", "submitted", "limit"}

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else paths.journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- order entries -------------------------------------------------------

    def record(self, entry: JournalEntry) -> None:
        intent = entry.intent
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO entries (entry_id, ts, intent_id, symbol, source, side, intent_json,"
                " reviews_json, final, fill_price, fill_qty, shadow_return_20d)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    _dt(entry.ts),
                    intent.intent_id,
                    intent.symbol,
                    Source(intent.source).value,
                    Side(intent.side).value,
                    json.dumps(intent_to_dict(intent), sort_keys=True),
                    json.dumps([review_to_dict(r) for r in entry.reviews], sort_keys=True),
                    Verdict(entry.final).value,
                    entry.fill_price,
                    entry.fill_qty,
                    entry.shadow_return_20d,
                ),
            )

    def update_fill(self, entry_id: str, price: float, qty: float) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE entries SET fill_price = ?, fill_qty = ? WHERE entry_id = ?",
                (float(price), float(qty), entry_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"no journal entry {entry_id!r}")

    def update_shadow(self, entry_id: str, ret: float) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE entries SET shadow_return_20d = ? WHERE entry_id = ?", (float(ret), entry_id)
            )
            if cur.rowcount != 1:
                raise KeyError(f"no journal entry {entry_id!r}")

    def query(self, **filters) -> list[JournalEntry]:
        """Filter entries. Supported keys: symbol, source, side, final (one or a list),
        intent_id, entry_id, since/until (datetime, inclusive), rejected=True,
        submitted=True, limit. Results are oldest first."""
        unknown = set(filters) - self._FILTERS
        if unknown:
            raise TypeError(f"unknown journal filters: {sorted(unknown)}")
        clauses, args = [], []
        for key in ("symbol", "intent_id", "entry_id"):
            if filters.get(key) is not None:
                clauses.append(f"{key} = ?")
                args.append(filters[key])
        if filters.get("source") is not None:
            clauses.append("source = ?")
            args.append(Source(filters["source"]).value)
        if filters.get("side") is not None:
            clauses.append("side = ?")
            args.append(Side(filters["side"]).value)
        finals = filters.get("final")
        if filters.get("rejected"):
            finals = REJECTED_VERDICTS
        if filters.get("submitted"):
            finals = SUBMITTED_VERDICTS
        if finals is not None:
            if isinstance(finals, (str, Verdict)):
                finals = [finals]
            finals = [Verdict(f).value for f in finals]
            clauses.append(f"final IN ({','.join('?' * len(finals))})")
            args.extend(finals)
        if filters.get("since") is not None:
            clauses.append("ts >= ?")
            args.append(_dt(filters["since"]))
        if filters.get("until") is not None:
            clauses.append("ts <= ?")
            args.append(_dt(filters["until"]))
        sql = "SELECT *, rowid AS _seq FROM entries"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if filters.get("limit") is not None:
            # newest N, still returned oldest first
            sql = f"SELECT * FROM ({sql} ORDER BY ts DESC, _seq DESC LIMIT ?) ORDER BY ts, _seq"
            args.append(int(filters["limit"]))
        else:
            sql += " ORDER BY ts, _seq"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=row["entry_id"],
            ts=_parse_dt(row["ts"]),
            intent=intent_from_dict(json.loads(row["intent_json"])),
            reviews=[review_from_dict(d) for d in json.loads(row["reviews_json"])],
            final=Verdict(row["final"]),
            fill_price=row["fill_price"],
            fill_qty=row["fill_qty"],
            shadow_return_20d=row["shadow_return_20d"],
        )

    # -- events ----------------------------------------------------------------

    def record_event(self, kind: str, payload: dict[str, Any] | None = None,
                     ts: datetime | None = None) -> JournalEvent:
        event = JournalEvent(
            event_id=uuid.uuid4().hex,
            ts=ts or datetime.now().astimezone(),
            kind=kind,
            payload=dict(payload or {}),
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_id, ts, kind, payload_json) VALUES (?,?,?,?)",
                (event.event_id, _dt(event.ts), event.kind, json.dumps(event.payload, sort_keys=True, default=str)),
            )
        return event

    def events(self, kind: str | None = None, since: datetime | None = None,
               until: datetime | None = None) -> list[JournalEvent]:
        clauses, args = [], []
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        if since is not None:
            clauses.append("ts >= ?")
            args.append(_dt(since))
        if until is not None:
            clauses.append("ts <= ?")
            args.append(_dt(until))
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts, rowid"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [
            JournalEvent(r["event_id"], _parse_dt(r["ts"]), r["kind"], json.loads(r["payload_json"]))
            for r in rows
        ]
