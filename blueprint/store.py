"""Where session snapshots live between requests and across restarts.

The snapshot (``DiscoverySession.to_dict()``) is the persistence format for a run: canvas,
turns, verdicts, match, reviews, build. This module is the small interface the API talks to;
``FileRunStore`` keeps one JSON file per session under ``runs/`` (what the CLI has written all
along), and step 12 replaces it with SQLite behind the same ``RunStore`` protocol.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, Protocol

logger = logging.getLogger(__name__)

_ID_RE: Final = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class RunNotFoundError(KeyError):
    """No snapshot exists for that session id."""


def validate_session_id(session_id: str) -> str:
    """Session ids are UUID-shaped; anything else is rejected before it touches a path."""
    if not _ID_RE.match(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


class RunStore(Protocol):
    """Persist and retrieve session snapshots."""

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


class FileRunStore:
    """One ``<session_id>.json`` per run in a directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

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
