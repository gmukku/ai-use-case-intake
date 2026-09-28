import json
from pathlib import Path

import pytest

from blueprint.store import FileRunStore, RunNotFoundError, validate_session_id

SESSION_A = "11111111-2222-3333-4444-555555555555"
SESSION_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def snapshot(session_id: str, **extra: object) -> dict[str, object]:
    return {"session_id": session_id, "turn": 1, "canvas": {"entries": []}, **extra}


class TestValidateSessionId:
    @pytest.mark.parametrize("sid", [SESSION_A, "abc12345", "A" * 64])
    def test_accepts_uuid_shapes(self, sid: str) -> None:
        assert validate_session_id(sid) == sid

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/passwd",  # traversal
            "run/../secret",  # traversal with a legal-looking prefix
            "short",  # under the minimum length
            "a" * 65,  # over the maximum
            "no_underscores_here",  # outside the hex-and-dash alphabet
            "",
        ],
    )
    def test_rejects_anything_that_could_escape_the_directory(self, bad: str) -> None:
        with pytest.raises(ValueError, match="invalid session id"):
            validate_session_id(bad)


class TestFileRunStore:
    def test_creates_its_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "runs" / "nested"
        FileRunStore(root)
        assert root.is_dir()

    def test_save_then_load_round_trips(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        store.save(snapshot(SESSION_A, review_status="pending"))
        assert store.load(SESSION_A) == snapshot(SESSION_A, review_status="pending")

    def test_save_overwrites_in_place(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        store.save(snapshot(SESSION_A, turn=1))
        store.save(snapshot(SESSION_A, turn=7))
        assert store.load(SESSION_A)["turn"] == 7
        assert list(tmp_path.glob("*.json")) == [tmp_path / f"{SESSION_A}.json"]

    def test_written_file_is_readable_json_on_disk(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        store.save(snapshot(SESSION_A))
        on_disk = json.loads((tmp_path / f"{SESSION_A}.json").read_text(encoding="utf-8"))
        assert on_disk["session_id"] == SESSION_A

    def test_load_missing_raises_run_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(RunNotFoundError):
            FileRunStore(tmp_path).load(SESSION_A)

    def test_exists(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        assert not store.exists(SESSION_A)
        store.save(snapshot(SESSION_A))
        assert store.exists(SESSION_A)

    def test_ids_are_newest_first_and_skip_foreign_files(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        store.save(snapshot(SESSION_A))
        store.save(snapshot(SESSION_B))
        # Make the ordering explicit rather than relying on filesystem timestamp resolution.
        (tmp_path / f"{SESSION_B}.json").touch()
        (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
        assert list(store.ids()) == [SESSION_B, SESSION_A]

    def test_a_bad_id_never_reaches_the_filesystem(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        with pytest.raises(ValueError, match="invalid session id"):
            store.save({"session_id": "../escape"})
        assert list(tmp_path.iterdir()) == []
