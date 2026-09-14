"""The GenAI Discovery Canvas: the deterministic backbone of every discovery conversation.

Seven fixed categories, always covered, always in this order. The *questions asked inside*
each category are adaptive (the model's job); *which categories exist and when the canvas
is complete* is fixed (this module's job). Keep that split intact.

This module has no SDK dependency on purpose: it is plain data + pure functions so it can be
unit-tested exhaustively and reused by the audit layer and the eval harness.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class CanvasCategory(StrEnum):
    """The seven canvas categories, in canonical order.

    Iteration order of this enum *is* the order the discovery conversation covers them.
    """

    KEY_STAKEHOLDERS = "key_stakeholders"
    KEY_ACTIVITIES = "key_activities"
    VALUE_PROPOSITION = "value_proposition"
    SYSTEM_INTEGRATIONS = "system_integrations"
    INPUT_SOURCE = "input_source"
    INPUT_TYPES = "input_types"
    OUTPUT_FORMAT = "output_format"

    @property
    def label(self) -> str:
        """Human-readable label, e.g. ``"Key stakeholders"``.

        Named ``label`` rather than ``title`` because ``StrEnum`` is a ``str`` and
        ``str.title()`` already exists.
        """
        return self.value.replace("_", " ").capitalize()


# Guiding questions, taken verbatim from the canvas definition. These anchor the model;
# they are not the only questions it may ask.
CATEGORY_QUESTIONS: dict[CanvasCategory, str] = {
    CanvasCategory.KEY_STAKEHOLDERS: "Who will use or depend on this AI system?",
    CanvasCategory.KEY_ACTIVITIES: (
        "What's the existing process for this task? How many users perform it "
        "(e.g. a team of 2 vs a team of 12)? How often (daily, weekly, monthly, quarterly)?"
    ),
    CanvasCategory.VALUE_PROPOSITION: (
        "What's the core problem the system solves (time savings, reduced errors, etc.)? "
        "Can we measure the impact?"
    ),
    CanvasCategory.SYSTEM_INTEGRATIONS: (
        "What other systems or third-party apps does this process touch "
        "(e.g. Salesforce, Hubspot, Jira etc)?"
    ),
    CanvasCategory.INPUT_SOURCE: (
        "Any external knowledge sources involved "
        "(SharePoint, Salesforce, a database, an external website)?"
    ),
    CanvasCategory.INPUT_TYPES: (
        "Input prompts, document uploads, images? (multi-select) Ask for real samples."
    ),
    CanvasCategory.OUTPUT_FORMAT: (
        "Summarization, comparison, workflow automation, drafting into a specific template, "
        "pulling data from a source, or a question-and-answer chatbot interface? (multi-select)"
    ),
}

# Categories where more than one answer is expected; the compiled spec renders these as lists.
MULTI_SELECT_CATEGORIES: frozenset[CanvasCategory] = frozenset(
    {CanvasCategory.INPUT_TYPES, CanvasCategory.OUTPUT_FORMAT}
)


@dataclass(frozen=True, slots=True)
class RubricElement:
    """One thing a category's recorded summary must state before it counts as sufficient."""

    key: str
    """Stable identifier; the completeness checker's schema uses these as an enum."""
    description: str
    """What "satisfied" means, phrased so a model can judge prose against it."""


# What a reviewer needs to see in each category's summary. This is the deterministic half of
# the completeness check: the checker may only report gaps from this list, and the discovery
# agent may only be nudged to ask about these. An element is also satisfied when the summary
# states explicitly that it does not apply (e.g. "no external systems").
CATEGORY_RUBRIC: dict[CanvasCategory, tuple[RubricElement, ...]] = {
    CanvasCategory.KEY_STAKEHOLDERS: (
        RubricElement("performers", "Who does the work today, by role or team (not just 'us')."),
        RubricElement(
            "dependents",
            "Who depends on or consumes the result (downstream teams, managers, customers), "
            "or an explicit statement that nobody else depends on it.",
        ),
    ),
    CanvasCategory.KEY_ACTIVITIES: (
        RubricElement("process_steps", "The existing process as concrete steps or actions."),
        RubricElement("headcount", "How many people perform it (a number or a clear size)."),
        RubricElement(
            "frequency", "How often it happens (daily, weekly, monthly, per event, a volume)."
        ),
    ),
    CanvasCategory.VALUE_PROPOSITION: (
        RubricElement(
            "core_problem", "The problem being solved (time, errors, risk, consistency, cost)."
        ),
        RubricElement(
            "measurable_impact",
            "Something that could be measured: a current number, a target, or a named metric.",
        ),
    ),
    CanvasCategory.SYSTEM_INTEGRATIONS: (
        RubricElement(
            "systems_named",
            "The systems the process touches, by name or clear type (e.g. 'our HRIS', "
            "'Salesforce'), or an explicit statement that none are involved.",
        ),
    ),
    CanvasCategory.INPUT_SOURCE: (
        RubricElement(
            "sources_named",
            "Where the knowledge or reference material lives (a named store, document, or "
            "system), or an explicit statement that no external source is needed.",
        ),
    ),
    CanvasCategory.INPUT_TYPES: (
        RubricElement(
            "types_selected",
            "Which input types apply: typed prompts, document uploads, images, structured data.",
        ),
        RubricElement(
            "samples_discussed",
            "Whether real sample inputs exist and can be provided (asked and answered).",
        ),
    ),
    CanvasCategory.OUTPUT_FORMAT: (
        RubricElement(
            "formats_selected",
            "Which output formats apply: summarization, comparison, workflow automation, "
            "drafting into a template, pulling data from a source, question-and-answer.",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class CanvasEntry:
    """One immutable capture event: the model judged a category sufficiently answered.

    Frozen so the event log is append-only by construction; a later capture for the same
    category is a *new* entry, never a mutation of this one.
    """

    category: CanvasCategory
    summary: str
    turn: int
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate at the boundary so bad model output never reaches the log."""
        if not self.summary.strip():
            raise ValueError(f"summary for {self.category} must not be empty")
        if self.turn < 0:
            raise ValueError(f"turn must be >= 0, got {self.turn}")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for persistence and the audit trail."""
        return {
            "category": self.category.value,
            "summary": self.summary,
            "turn": self.turn,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(slots=True)
class CanvasState:
    """Append-only log of captures for one discovery conversation.

    "Current answer" for a category is *derived* (latest entry), never stored separately,
    so state and history cannot drift apart.
    """

    entries: list[CanvasEntry] = field(default_factory=list)

    def record(self, category: CanvasCategory | str, summary: str, *, turn: int) -> CanvasEntry:
        """Append a capture. Accepts the category as a string so tool-call input can pass through.

        Raises:
            ValueError: if ``category`` is not one of the seven, or ``summary`` is blank.
        """
        try:
            cat = CanvasCategory(category)
        except ValueError:
            valid = ", ".join(c.value for c in CanvasCategory)
            raise ValueError(
                f"unknown canvas category {category!r}; expected one of: {valid}"
            ) from None

        entry = CanvasEntry(category=cat, summary=summary.strip(), turn=turn)
        self.entries.append(entry)
        logger.info(
            "canvas.record",
            extra={"category": cat.value, "turn": turn, "version": len(self.history(cat))},
        )
        return entry

    def history(self, category: CanvasCategory) -> list[CanvasEntry]:
        """All captures for ``category``, oldest first."""
        return [e for e in self.entries if e.category is category]

    def current(self, category: CanvasCategory) -> CanvasEntry | None:
        """The latest capture for ``category``, or ``None`` if not yet covered."""
        hist = self.history(category)
        return hist[-1] if hist else None

    def covered(self) -> list[CanvasCategory]:
        """Categories with at least one capture, in canonical order."""
        return [c for c in CanvasCategory if self.current(c) is not None]

    def missing(self) -> list[CanvasCategory]:
        """Categories with no capture yet, in canonical order — the next things to ask about."""
        return [c for c in CanvasCategory if self.current(c) is None]

    @property
    def is_complete(self) -> bool:
        """True once every category has at least one capture."""
        return not self.missing()

    def __iter__(self) -> Iterator[CanvasEntry]:
        """Iterate the raw event log in insertion order."""
        return iter(self.entries)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot: the full log plus the derived current view."""
        return {
            "entries": [e.to_dict() for e in self.entries],
            "current": {
                c.value: (cur.summary if (cur := self.current(c)) else None) for c in CanvasCategory
            },
            "missing": [c.value for c in self.missing()],
            "is_complete": self.is_complete,
        }
