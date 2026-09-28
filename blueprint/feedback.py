"""What the requester thought of a reply.

The reviewer's judgement lives in ``review.py``; this is the other human in the loop. A
thumb on an agent turn is cheap to give and is the only signal that comes from the person
the conversation is actually for.

It is kept append-only for the same reason the canvas is: a rating that changes is itself
information. ``current`` collapses the log to the latest rating per turn for display, and
step 11 reads the whole log as human labels for judge validation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

# Long enough for a real sentence of "this missed the point because…", short enough that the
# field cannot be used as free storage.
MAX_COMMENT_CHARS: Final = 2_000


class Rating(StrEnum):
    """A thumb. Deliberately two-valued: a 5-point scale invites deliberation, not reaction."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class RequesterFeedback:
    """One rating of one agent turn, by the person who asked for the thing."""

    turn: int
    rating: Rating
    comment: str = ""
    given_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate at the boundary: a rating points at a real turn and carries bounded text."""
        if self.turn < 1:
            raise ValueError(f"turn must be 1 or greater, got {self.turn}")
        if len(self.comment) > MAX_COMMENT_CHARS:
            raise ValueError(f"comment must be at most {MAX_COMMENT_CHARS} characters")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "turn": self.turn,
            "rating": self.rating.value,
            "comment": self.comment,
            "given_at": self.given_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequesterFeedback:
        """Inverse of :meth:`to_dict`."""
        return cls(
            turn=int(data["turn"]),
            rating=Rating(data["rating"]),
            comment=str(data.get("comment", "")),
            given_at=datetime.fromisoformat(data["given_at"]),
        )


def current(entries: Sequence[RequesterFeedback]) -> dict[int, RequesterFeedback]:
    """Collapse the log to the latest rating per turn, for display."""
    latest: dict[int, RequesterFeedback] = {}
    for entry in entries:
        latest[entry.turn] = entry
    return latest
