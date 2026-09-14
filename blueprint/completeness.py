"""Completeness check: is a recorded category summary sufficient for a reviewer?

The discovery model decides *when* to record a category. This module independently judges
*whether what it recorded is enough*, against the per-category rubric in ``canvas.py``. The
rubric keys are an enum in the output schema, so the checker can only report gaps we defined;
its latitude is limited to judging whether prose satisfies an element.

It runs inside the ``record_canvas_answer`` tool call (see ``discovery.py``), so a gap turns
into a clarifying question in the same reply the stakeholder is about to read. A clarification
cap in the orchestrator stops it from interrogating.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import jsonschema
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, Message, ResultMessage, query

from blueprint.canvas import CATEGORY_RUBRIC, CanvasCategory

logger = logging.getLogger(__name__)

DEFAULT_CHECKER_MODEL: Final = "claude-opus-5"
CHECKER_MAX_TURNS: Final = 3  # structured output needs a hidden tool round-trip

QueryFn = Callable[..., AsyncIterator[Message]]


class AssessmentError(RuntimeError):
    """The checker did not produce a usable result."""


@dataclass(frozen=True, slots=True)
class Assessment:
    """The checker's verdict on one version of one category's summary."""

    category: CanvasCategory
    version: int
    """Which capture of this category was judged (1 = first record)."""
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    question: str | None
    """A single plain-language follow-up covering the gaps; ``None`` when sufficient."""
    model: str
    cost_usd: float | None
    duration_ms: int
    assessed_at: datetime

    @property
    def sufficient(self) -> bool:
        """True when no rubric element is missing."""
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for the trace and the reviewer."""
        return {
            "category": self.category.value,
            "version": self.version,
            "sufficient": self.sufficient,
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "question": self.question,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "assessed_at": self.assessed_at.isoformat(),
        }


def build_assessment_schema(category: CanvasCategory) -> dict[str, Any]:
    """Output schema whose enums are exactly this category's rubric keys."""
    keys = [e.key for e in CATEGORY_RUBRIC[category]]
    return {
        "type": "object",
        "properties": {
            "satisfied": {
                "type": "array",
                "items": {"type": "string", "enum": keys},
                "uniqueItems": True,
                "description": "Rubric elements the summary states (or explicitly rules out).",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string", "enum": keys},
                "uniqueItems": True,
                "description": "Rubric elements the summary does not state.",
            },
            "question": {
                "type": "string",
                "description": (
                    "If anything is missing: ONE plain-language follow-up, at most 25 words, "
                    "that a consultant would ask the stakeholder to cover the missing elements. "
                    "Empty string if nothing is missing."
                ),
            },
        },
        "required": ["satisfied", "missing", "question"],
        "additionalProperties": False,
    }


def build_assessment_prompt(category: CanvasCategory, summary: str) -> str:
    """Render the judging request: the rubric, then the summary to judge."""
    rubric = "\n".join(f"- `{e.key}`: {e.description}" for e in CATEGORY_RUBRIC[category])
    return (
        f"Judge whether this discovery summary for the category **{category.label}** is "
        "sufficient for a reviewer who will decide whether to build a prototype.\n\n"
        f"# Rubric\n\n{rubric}\n\n"
        f"# Summary to judge\n\n{summary.strip()}\n\n"
        "# Rules\n\n"
        "- Judge only what the summary states. Do not infer facts it does not contain.\n"
        "- An element is satisfied if the summary states it concretely, or explicitly says it "
        "does not apply.\n"
        "- Every rubric key must appear in exactly one of `satisfied` or `missing`.\n"
        "- If anything is missing, write one natural follow-up question covering all missing "
        "elements, in plain language, at most 25 words, addressed to the stakeholder. "
        "Otherwise return an empty string."
    )


CHECKER_SYSTEM_PROMPT: Final = (
    "You are a precise reviewer of discovery notes. You judge a short summary against a rubric "
    "and output only the structured result requested."
)


async def assess_capture(
    category: CanvasCategory,
    summary: str,
    *,
    version: int,
    model: str = DEFAULT_CHECKER_MODEL,
    query_fn: QueryFn = query,
) -> Assessment:
    """Judge one recorded summary against its category's rubric.

    Raises:
        AssessmentError: if the run fails or the output is inconsistent with the rubric.
    """
    if not summary.strip():
        raise ValueError("summary is empty; nothing to assess")

    schema = build_assessment_schema(category)
    keys = {e.key for e in CATEGORY_RUBRIC[category]}
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=CHECKER_SYSTEM_PROMPT,
        setting_sources=[],
        tools=[],
        max_turns=CHECKER_MAX_TURNS,
        effort="low",
        output_format={"type": "json_schema", "schema": schema},
    )

    started = datetime.now(UTC)
    result: ResultMessage | None = None
    try:
        async for message in query_fn(
            prompt=build_assessment_prompt(category, summary), options=options
        ):
            if isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        raise AssessmentError(f"checker run failed: {exc}") from exc

    if result is None:
        raise AssessmentError("checker produced no ResultMessage")
    if result.is_error:
        raise AssessmentError(f"checker run failed: {'; '.join(result.errors or ['unknown'])}")

    payload = result.structured_output
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise AssessmentError(f"checker output failed schema validation: {exc.message}") from exc
    assert isinstance(payload, dict)

    satisfied, missing = set(payload["satisfied"]), set(payload["missing"])
    if satisfied & missing or (satisfied | missing) != keys:
        raise AssessmentError(
            f"checker output inconsistent: satisfied={sorted(satisfied)} "
            f"missing={sorted(missing)} rubric={sorted(keys)}"
        )
    question = str(payload["question"]).strip() or None
    if missing and question is None:
        raise AssessmentError("checker reported gaps but no follow-up question")

    assessment = Assessment(
        category=category,
        version=version,
        satisfied=tuple(k for k in (e.key for e in CATEGORY_RUBRIC[category]) if k in satisfied),
        missing=tuple(k for k in (e.key for e in CATEGORY_RUBRIC[category]) if k in missing),
        question=question if missing else None,
        model=model,
        cost_usd=result.total_cost_usd,
        duration_ms=result.duration_ms,
        assessed_at=started,
    )
    logger.info(
        "completeness.assessed",
        extra={
            "category": category.value,
            "version": version,
            "sufficient": assessment.sufficient,
            "missing": list(assessment.missing),
            "cost_usd": assessment.cost_usd,
            "duration_ms": assessment.duration_ms,
        },
    )
    return assessment
