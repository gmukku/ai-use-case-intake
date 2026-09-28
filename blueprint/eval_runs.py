"""Reading what the eval harness wrote.

`evals/<suite>/run.py` writes one JSON file per run to `evals/<suite>/results/`, holding a
`summary` keyed by model and the raw per-call `results`. This module is the read side: the
admin view lists runs and opens one, and step 11's harness writes more suites into the same
shape rather than inventing a second one.

Nothing here executes an eval. Running one costs real money and takes minutes, so it stays a
deliberate command-line act, not something a page can trigger.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# Resolved from the package so the server can be launched from anywhere. Every function
# below reads this at call time rather than binding it as a default argument, because a
# default binds once at import and can then never be overridden — including by a test.
_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
EVALS_DIR: Final = Path(os.environ.get("BLUEPRINT_EVALS_DIR", _REPO_ROOT / "evals"))

# Suite directories and run files are named by us, never by a user — but they arrive back
# through a URL, so they are checked before they are joined to a path.
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class EvalNotFoundError(KeyError):
    """No such suite, or no such run inside it."""


def _safe(name: str, kind: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid {kind}: {name!r}")
    return name


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One recorded run of one suite."""

    suite: str
    run_id: str
    """The result file's stem, e.g. ``20260914T015734Z``."""
    ran_at: datetime | None
    """Parsed from the run id when it is a timestamp; the file mtime otherwise."""
    summary: dict[str, Any]
    """Per-model metrics, exactly as the harness wrote them."""
    calls: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view. The per-call results are not included; they are large."""
        return {
            "suite": self.suite,
            "run_id": self.run_id,
            "ran_at": self.ran_at.isoformat() if self.ran_at else None,
            "summary": self.summary,
            "calls": self.calls,
        }


def _parse_ran_at(run_id: str, path: Path) -> datetime | None:
    try:
        return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            return None


def suites(root: Path | None = None) -> list[str]:
    """Suite names, alphabetically. A suite is a directory holding a ``run.py``."""
    root = root if root is not None else EVALS_DIR
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and (d / "run.py").is_file() and _NAME_RE.match(d.name)
    )


def runs(suite: str, root: Path | None = None) -> list[EvalRun]:
    """Every recorded run of one suite, newest first. Unreadable files are skipped."""
    root = root if root is not None else EVALS_DIR
    results = root / _safe(suite, "suite") / "results"
    if not results.is_dir():
        return []

    found: list[EvalRun] = []
    for path in results.glob("*.json"):
        if not _NAME_RE.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # a half-written or hand-edited file is not worth failing the page over
        found.append(
            EvalRun(
                suite=suite,
                run_id=path.stem,
                ran_at=_parse_ran_at(path.stem, path),
                summary=data.get("summary", {}),
                calls=len(data.get("results", [])),
            )
        )
    return sorted(found, key=lambda r: r.run_id, reverse=True)


def load(suite: str, run_id: str, root: Path | None = None) -> dict[str, Any]:
    """One run in full, including every per-call result.

    Raises:
        EvalNotFoundError: if the suite or the run does not exist.
        ValueError: if either name is not a plain identifier.
    """
    root = root if root is not None else EVALS_DIR
    path = root / _safe(suite, "suite") / "results" / f"{_safe(run_id, 'run id')}.json"
    if not path.is_file():
        raise EvalNotFoundError(f"{suite}/{run_id}")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def latest(root: Path | None = None) -> list[EvalRun]:
    """The most recent run of each suite, for the dashboard's top line."""
    root = root if root is not None else EVALS_DIR
    newest: list[EvalRun] = []
    for suite in suites(root):
        suite_runs = runs(suite, root)
        if suite_runs:
            newest.append(suite_runs[0])
    return newest
