"""Where session snapshots and the audit trail live.

The snapshot (``DiscoverySession.to_dict()``) is the persistence format for a run: canvas,
turns, verdicts, match, reviews, feedback, build. This module is the interface the API talks
to, plus two implementations of it:

- ``FileRunStore`` writes one JSON file per session under ``runs/``, which is what the CLI has
  written since step 1.
- ``SqliteRunStore`` keeps the same snapshots in a database, adds an append-only
  ``audit_events`` table, and can redact free text on the way in.

Both satisfy ``RunStore``, so the API cannot tell them apart — which is the point of having
had the protocol since step 7.

**The audit trail is append-only and separate from the snapshot.** A snapshot is current
state and gets overwritten; an event is something that happened and never changes. "Who
approved this, when, under which spec version" has to survive a later re-approval, so it
cannot live in a field that the next save replaces.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from blueprint.redaction import redact_snapshot

logger = logging.getLogger(__name__)

_ID_RE: Final = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class RunNotFoundError(KeyError):
    """No snapshot exists for that session id."""


def validate_session_id(session_id: str) -> str:
    """Session ids are UUID-shaped; anything else is rejected before it touches a path."""
    if not _ID_RE.match(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One thing that happened, recorded once and never edited.

    ``actor`` is the authenticated account for a human action, ``"system"`` for something the
    pipeline did on its own. The distinction is the reason this table exists.
    """

    session_id: str
    action: str
    actor: str = "system"
    detail: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "session_id": self.session_id,
            "action": self.action,
            "actor": self.actor,
            "detail": self.detail,
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEvent:
        """Inverse of :meth:`to_dict`."""
        return cls(
            session_id=str(data["session_id"]),
            action=str(data["action"]),
            actor=str(data.get("actor", "system")),
            detail=dict(data.get("detail") or {}),
            at=datetime.fromisoformat(data["at"]),
        )


class RunStore(Protocol):
    """Persist and retrieve session snapshots, and record what happened to them."""

    def save(self, snapshot: dict[str, Any]) -> None:
        """Write (or overwrite) the snapshot for ``snapshot["session_id"]``."""
        ...

    def load(self, session_id: str) -> dict[str, Any]:
        """Read one snapshot; raise ``RunNotFoundError`` if absent."""
        ...

    def exists(self, session_id: str) -> bool:
        """Whether a snapshot is stored."""
        ...

    def ids(self) -> Iterator[str]:
        """Every stored session id, newest first."""
        ...

    def append_event(self, event: AuditEvent) -> None:
        """Record something that happened. Never overwrites."""
        ...

    def events(self, session_id: str) -> list[AuditEvent]:
        """Everything recorded for one session, oldest first."""
        ...


class FileRunStore:
    """One ``<session_id>.json`` per run in a directory, plus an audit JSON-lines file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._audit = self.root / "audit.jsonl"

    def _path(self, session_id: str) -> Path:
        return self.root / f"{validate_session_id(session_id)}.json"

    def save(self, snapshot: dict[str, Any]) -> None:
        """Atomic-enough write: to a temp file, then replace."""
        session_id = str(snapshot.get("session_id") or "")
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp.replace(path)
        logger.debug("store.saved", extra={"session_id": session_id})

    def load(self, session_id: str) -> dict[str, Any]:
        """Read one snapshot."""
        path = self._path(session_id)
        if not path.is_file():
            raise RunNotFoundError(session_id)
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def exists(self, session_id: str) -> bool:
        """Whether a snapshot file exists."""
        return self._path(session_id).is_file()

    def ids(self) -> Iterator[str]:
        """Stored ids, newest modification first."""
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if _ID_RE.match(path.stem):
                yield path.stem

    def append_event(self, event: AuditEvent) -> None:
        """Append one line to ``audit.jsonl``. Append-only by construction."""
        validate_session_id(event.session_id)
        with self._audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict()) + "\n")

    def events(self, session_id: str) -> list[AuditEvent]:
        """Scan the audit file for one session's events."""
        validate_session_id(session_id)
        if not self._audit.is_file():
            return []
        found: list[AuditEvent] = []
        for line in self._audit.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line is not worth failing a read over
            if data.get("session_id") == session_id:
                found.append(AuditEvent.from_dict(data))
        return found


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS runs (
    session_id   TEXT PRIMARY KEY,
    snapshot     TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- Denormalized from the snapshot so the audit database is queryable by hand without
    -- parsing JSON in SQL. The snapshot stays the source of truth; these are a view of it.
    turn            INTEGER NOT NULL DEFAULT 0,
    is_complete     INTEGER NOT NULL DEFAULT 0,
    review_status   TEXT    NOT NULL DEFAULT 'pending',
    total_cost_usd  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_updated_at ON runs (updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_session ON audit_events (session_id, id);
"""


class SqliteRunStore:
    """Snapshots and audit events in one SQLite file.

    Args:
        path: the database file; parent directories are created.
        redact: run free text through :mod:`blueprint.redaction` on the way in. On by
            default — the point of moving off loose JSON files is that what lands on disk is
            deliberate. There is no un-redact; the original is never written.
    """

    def __init__(self, path: Path, *, redact: bool = True) -> None:
        self.path = path
        self.redact = redact
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock. FastAPI can touch this from the event loop and
        # from a threadpool worker, and sqlite3 objects are not safe across threads by default.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets a reader (a reviewer opening a spec) proceed while a turn is writing.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        """Close the connection. Tests and shutdown; not needed in normal operation."""
        with self._lock:
            self._db.close()

    def save(self, snapshot: dict[str, Any]) -> None:
        """Insert or replace one run, redacting free text first if configured."""
        session_id = validate_session_id(str(snapshot.get("session_id") or ""))
        stored = redact_snapshot(snapshot) if self.redact else snapshot
        with self._lock:
            self._db.execute(
                """
                INSERT INTO runs (session_id, snapshot, updated_at, turn, is_complete,
                                  review_status, total_cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    snapshot=excluded.snapshot, updated_at=excluded.updated_at,
                    turn=excluded.turn, is_complete=excluded.is_complete,
                    review_status=excluded.review_status,
                    total_cost_usd=excluded.total_cost_usd
                """,
                (
                    session_id,
                    json.dumps(stored),
                    datetime.now(UTC).isoformat(),
                    int(stored.get("turn", 0)),
                    int(bool(stored.get("is_complete"))),
                    str(stored.get("review_status", "pending")),
                    float(stored.get("total_cost_usd", 0.0)),
                ),
            )
            self._db.commit()
        logger.debug("store.saved", extra={"session_id": session_id})

    def load(self, session_id: str) -> dict[str, Any]:
        """Read one snapshot."""
        validate_session_id(session_id)
        with self._lock:
            row = self._db.execute(
                "SELECT snapshot FROM runs WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(session_id)
        data: dict[str, Any] = json.loads(row["snapshot"])
        return data

    def exists(self, session_id: str) -> bool:
        """Whether a run is stored."""
        validate_session_id(session_id)
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM runs WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def ids(self) -> Iterator[str]:
        """Stored ids, most recently written first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id FROM runs ORDER BY updated_at DESC, session_id"
            ).fetchall()
        for row in rows:
            yield str(row["session_id"])

    def append_event(self, event: AuditEvent) -> None:
        """Record one event. There is no update or delete path for this table."""
        validate_session_id(event.session_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO audit_events (session_id, at, actor, action, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.session_id,
                    event.at.isoformat(),
                    event.actor,
                    event.action,
                    json.dumps(redact_snapshot(event.detail) if self.redact else event.detail),
                ),
            )
            self._db.commit()

    def events(self, session_id: str) -> list[AuditEvent]:
        """One session's events, oldest first."""
        validate_session_id(session_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, at, actor, action, detail FROM audit_events "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [
            AuditEvent(
                session_id=str(row["session_id"]),
                action=str(row["action"]),
                actor=str(row["actor"]),
                detail=json.loads(row["detail"]),
                at=datetime.fromisoformat(row["at"]),
            )
            for row in rows
        ]

    def purge(self, older_than_days: int) -> list[str]:
        """Delete runs (and their events) not written within the retention window.

        Returns the ids removed. ``older_than_days <= 0`` deletes nothing and is the default
        everywhere: silently dropping a stakeholder's conversation because a config value was
        absent would be worse than keeping it.
        """
        if older_than_days <= 0:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id FROM runs WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            removed = [str(row["session_id"]) for row in rows]
            if removed:
                marks = ",".join("?" * len(removed))
                self._db.execute(f"DELETE FROM runs WHERE session_id IN ({marks})", removed)
                self._db.execute(f"DELETE FROM audit_events WHERE session_id IN ({marks})", removed)
            self._db.commit()
        if removed:
            logger.info("store.purged", extra={"count": len(removed), "days": older_than_days})
        return removed


def migrate_files_to_sqlite(source: FileRunStore, target: SqliteRunStore) -> list[str]:
    """Copy every run from a file store into a database, redacting if the target does.

    Idempotent: a run already present is replaced with the same content, so a half-finished
    migration can simply be re-run.
    """
    moved: list[str] = []
    for session_id in list(source.ids()):
        target.save(source.load(session_id))
        for event in source.events(session_id):
            target.append_event(event)
        moved.append(session_id)
    return moved


def build_events(snapshot: dict[str, Any]) -> Sequence[AuditEvent]:
    """Derive the pipeline's own audit events from a finished snapshot.

    CLAUDE.md asks the audit layer to record "every step, every retrieved example, every human
    decision". Human decisions are recorded as they happen, with the account that made them.
    The agent's steps are already written down in the snapshot, so they are derived from it
    rather than double-written during the turn — one source of truth, and a migrated run gets
    the same events as a live one.
    """
    session_id = str(snapshot.get("session_id") or "")
    if not session_id:
        return []
    events: list[AuditEvent] = []

    match = snapshot.get("match")
    if match:
        events.append(
            AuditEvent(
                session_id=session_id,
                action="departments.matched",
                detail={
                    "departments": list(match.get("departments", [])),
                    "model": match.get("model"),
                },
            )
        )

    for turn in snapshot.get("turns", []):
        number = int(turn.get("turn", 0))
        events.append(
            AuditEvent(
                session_id=session_id,
                action="turn.completed",
                actor="requester" if turn.get("origin") != "reviewer" else "reviewer",
                detail={
                    "turn": number,
                    "captured": list(turn.get("captured", [])),
                    "cost_usd": turn.get("cost_usd"),
                    "is_error": bool(turn.get("is_error")),
                },
            )
        )
        # Every retrieved example: which SOP was read, which search ran, which tool was called.
        for call in turn.get("tool_calls", []):
            events.append(
                AuditEvent(
                    session_id=session_id,
                    action="tool.called",
                    detail={"turn": number, "name": call.get("name"), "input": call.get("input")},
                )
            )

    return events
