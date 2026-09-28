"""Both stores, one test suite.

Every behavioural test runs against `FileRunStore` and `SqliteRunStore` through the `RunStore`
protocol. If the two ever diverge, swapping them in `create_app` would change behaviour — and
the whole point of having had the protocol since step 7 is that it cannot.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from blueprint.store import (
    AuditEvent,
    FileRunStore,
    RunNotFoundError,
    RunStore,
    SqliteRunStore,
    build_events,
    migrate_files_to_sqlite,
)

SESSION_A = "11111111-2222-3333-4444-555555555555"
SESSION_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def snapshot(session_id: str = SESSION_A, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "turn": 2,
        "is_complete": False,
        "review_status": "pending",
        "total_cost_usd": 0.1466,
        "canvas": {"entries": [], "current": {}},
        "turns": [],
    }
    base.update(extra)
    return base


@pytest.fixture(params=["file", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RunStore]:
    if request.param == "file":
        yield FileRunStore(tmp_path / "runs")
    else:
        # Redaction off here: these tests are about storage behaviour, and it has its own suite.
        db = SqliteRunStore(tmp_path / "blueprint.db", redact=False)
        yield db
        db.close()


class TestSnapshots:
    def test_save_then_load(self, store: RunStore) -> None:
        store.save(snapshot())
        assert store.load(SESSION_A)["total_cost_usd"] == 0.1466

    def test_save_replaces(self, store: RunStore) -> None:
        store.save(snapshot(turn=1))
        store.save(snapshot(turn=7))
        assert store.load(SESSION_A)["turn"] == 7
        assert list(store.ids()) == [SESSION_A]

    def test_missing_raises(self, store: RunStore) -> None:
        with pytest.raises(RunNotFoundError):
            store.load(SESSION_A)

    def test_exists(self, store: RunStore) -> None:
        assert not store.exists(SESSION_A)
        store.save(snapshot())
        assert store.exists(SESSION_A)

    def test_ids_lists_every_stored_run(self, store: RunStore) -> None:
        # Ordering is asserted per implementation below: two saves in the same clock tick can
        # share a timestamp, and both stores then fall back to id order. Real turns are
        # seconds apart, so this only bites a test.
        store.save(snapshot(SESSION_A))
        store.save(snapshot(SESSION_B))
        assert sorted(store.ids()) == sorted([SESSION_A, SESSION_B])

    def test_a_bad_id_never_reaches_storage(self, store: RunStore) -> None:
        with pytest.raises(ValueError, match="invalid session id"):
            store.save({"session_id": "../escape"})

    def test_round_trips_nested_structure(self, store: RunStore) -> None:
        full = snapshot(
            turns=[{"turn": 1, "user_text": "hello", "tool_calls": [{"name": "x", "input": {}}]}],
            reviews=[{"action": "approve", "reviewer": "Dana"}],
        )
        store.save(full)
        assert store.load(SESSION_A) == full


class TestAuditEvents:
    def test_append_and_read_back(self, store: RunStore) -> None:
        store.append_event(AuditEvent(session_id=SESSION_A, action="review.decision", actor="Dana"))
        events = store.events(SESSION_A)
        assert [e.action for e in events] == ["review.decision"]
        assert events[0].actor == "Dana"

    def test_events_are_per_session(self, store: RunStore) -> None:
        store.append_event(AuditEvent(session_id=SESSION_A, action="a"))
        store.append_event(AuditEvent(session_id=SESSION_B, action="b"))
        assert [e.action for e in store.events(SESSION_A)] == ["a"]

    def test_oldest_first(self, store: RunStore) -> None:
        for i in range(5):
            store.append_event(AuditEvent(session_id=SESSION_A, action=f"step{i}"))
        assert [e.action for e in store.events(SESSION_A)] == [f"step{i}" for i in range(5)]

    def test_saving_a_snapshot_does_not_disturb_events(self, store: RunStore) -> None:
        # The snapshot is current state and gets overwritten; an event happened and does not.
        store.append_event(AuditEvent(session_id=SESSION_A, action="review.decision"))
        store.save(snapshot(turn=9))
        assert len(store.events(SESSION_A)) == 1

    def test_detail_round_trips(self, store: RunStore) -> None:
        store.append_event(
            AuditEvent(
                session_id=SESSION_A,
                action="tool.called",
                detail={"name": "mcp__sop__read_sop", "input": {"sop_id": "hr-packet"}},
            )
        )
        assert store.events(SESSION_A)[0].detail["input"]["sop_id"] == "hr-packet"

    def test_no_events_is_empty_not_an_error(self, store: RunStore) -> None:
        assert store.events(SESSION_A) == []


class TestSqliteSpecifics:
    def test_redaction_is_on_by_default(self, tmp_path: Path) -> None:
        db = SqliteRunStore(tmp_path / "b.db")
        db.save(snapshot(turns=[{"turn": 1, "user_text": "mail me at dana@example.com"}]))
        assert db.load(SESSION_A)["turns"][0]["user_text"] == "mail me at [redacted:email]"
        db.close()

    def test_there_is_no_un_redact(self, tmp_path: Path) -> None:
        # The original is never written, so a leak of the file is a leak of redacted text.
        path = tmp_path / "b.db"
        db = SqliteRunStore(path)
        db.save(snapshot(turns=[{"turn": 1, "user_text": "call 555-123-4567"}]))
        db.close()
        assert b"555-123-4567" not in path.read_bytes()

    def test_event_detail_is_redacted_too(self, tmp_path: Path) -> None:
        db = SqliteRunStore(tmp_path / "b.db")
        db.append_event(
            AuditEvent(session_id=SESSION_A, action="review.decision", detail={"note": "x@y.com"})
        )
        assert db.events(SESSION_A)[0].detail["note"] == "[redacted:email]"
        db.close()

    def test_ids_are_newest_written_first(self, tmp_path: Path) -> None:
        db = SqliteRunStore(tmp_path / "b.db", redact=False)
        db.save(snapshot(SESSION_A))
        db.save(snapshot(SESSION_B))
        older = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db._db.execute("UPDATE runs SET updated_at = ? WHERE session_id = ?", (older, SESSION_A))
        db._db.commit()
        assert list(db.ids()) == [SESSION_B, SESSION_A]
        db.close()

    def test_index_columns_follow_the_snapshot(self, tmp_path: Path) -> None:
        # They exist so the audit database can be queried by hand without parsing JSON in SQL.
        db = SqliteRunStore(tmp_path / "b.db", redact=False)
        db.save(snapshot(turn=8, is_complete=True, review_status="approved"))
        row = db._db.execute("SELECT * FROM runs WHERE session_id = ?", (SESSION_A,)).fetchone()
        assert (row["turn"], row["is_complete"], row["review_status"]) == (8, 1, "approved")
        db.close()

    def test_reopening_the_file_keeps_everything(self, tmp_path: Path) -> None:
        path = tmp_path / "b.db"
        first = SqliteRunStore(path, redact=False)
        first.save(snapshot())
        first.append_event(AuditEvent(session_id=SESSION_A, action="turn.completed"))
        first.close()

        second = SqliteRunStore(path, redact=False)
        assert second.load(SESSION_A)["turn"] == 2
        assert len(second.events(SESSION_A)) == 1
        second.close()


class TestPurge:
    def test_zero_days_deletes_nothing(self, tmp_path: Path) -> None:
        # The default. Dropping a conversation because a config value was absent is worse
        # than keeping it.
        db = SqliteRunStore(tmp_path / "b.db")
        db.save(snapshot())
        assert db.purge(0) == []
        assert db.exists(SESSION_A)
        db.close()

    def test_recent_runs_survive(self, tmp_path: Path) -> None:
        db = SqliteRunStore(tmp_path / "b.db")
        db.save(snapshot())
        assert db.purge(30) == []
        db.close()

    def test_old_runs_and_their_events_go(self, tmp_path: Path) -> None:
        db = SqliteRunStore(tmp_path / "b.db")
        db.save(snapshot())
        db.append_event(AuditEvent(session_id=SESSION_A, action="turn.completed"))
        stale = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        db._db.execute("UPDATE runs SET updated_at = ?", (stale,))
        db._db.commit()

        assert db.purge(365) == [SESSION_A]
        assert not db.exists(SESSION_A)
        assert db.events(SESSION_A) == []  # retention means the trail goes too
        db.close()


class TestMigration:
    def test_moves_snapshots_and_events(self, tmp_path: Path) -> None:
        source = FileRunStore(tmp_path / "runs")
        source.save(snapshot(SESSION_A))
        source.save(snapshot(SESSION_B))
        source.append_event(AuditEvent(session_id=SESSION_A, action="review.decision"))

        target = SqliteRunStore(tmp_path / "b.db", redact=False)
        moved = migrate_files_to_sqlite(source, target)

        assert sorted(moved) == sorted([SESSION_A, SESSION_B])
        assert target.load(SESSION_A)["turn"] == 2
        assert [e.action for e in target.events(SESSION_A)] == ["review.decision"]
        target.close()

    def test_is_idempotent(self, tmp_path: Path) -> None:
        source = FileRunStore(tmp_path / "runs")
        source.save(snapshot())
        target = SqliteRunStore(tmp_path / "b.db", redact=False)

        migrate_files_to_sqlite(source, target)
        migrate_files_to_sqlite(source, target)

        assert list(target.ids()) == [SESSION_A]  # replaced, not duplicated
        target.close()

    def test_migrating_redacts_if_the_target_does(self, tmp_path: Path) -> None:
        source = FileRunStore(tmp_path / "runs")
        source.save(snapshot(turns=[{"turn": 1, "user_text": "dana@example.com"}]))
        target = SqliteRunStore(tmp_path / "b.db")
        migrate_files_to_sqlite(source, target)
        assert target.load(SESSION_A)["turns"][0]["user_text"] == "[redacted:email]"
        target.close()


class TestBuildEvents:
    def test_derives_the_pipelines_own_steps(self) -> None:
        events = build_events(
            snapshot(
                match={"departments": ["hr", "finance"], "model": "claude-opus-5"},
                turns=[
                    {
                        "turn": 1,
                        "origin": "stakeholder",
                        "captured": ["key_stakeholders"],
                        "cost_usd": 0.02,
                        "tool_calls": [
                            {"name": "mcp__sop__read_sop", "input": {"sop_id": "hr-packet"}}
                        ],
                    }
                ],
            )
        )
        actions = [e.action for e in events]
        assert actions == ["departments.matched", "turn.completed", "tool.called"]
        assert events[2].detail["input"]["sop_id"] == "hr-packet"  # every retrieved example

    def test_a_reviewer_turn_is_attributed_to_the_reviewer(self) -> None:
        events = build_events(snapshot(turns=[{"turn": 11, "origin": "reviewer"}]))
        assert events[0].actor == "reviewer"

    def test_no_match_means_no_match_event(self) -> None:
        assert [e.action for e in build_events(snapshot())] == []

    def test_a_snapshot_without_an_id_yields_nothing(self) -> None:
        assert build_events({"turns": [{"turn": 1}]}) == []
