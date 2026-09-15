"""Department matching: the explicit classification pass that selects 1-3 skills.

This is its own step on purpose. Something has to decide which departments' example banks
apply to a conversation, and that decision should be visible, logged, and evaluable rather
than an implicit side effect of prompting. It runs once, after the stakeholder's early
answers (who they are, what the process is), and the result is stored on the session.

Mechanism: a one-shot ``query()`` with a JSON-schema ``output_format``. The schema, not the
prose, enforces the 1-3 rule and restricts values to loaded skill names.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import jsonschema
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, Message, ResultMessage, query

from blueprint.discovery import DEFAULT_MODEL
from blueprint.isolation import AGENT_ENV
from blueprint.skills import DepartmentSkill

logger = logging.getLogger(__name__)

MAX_MATCHES: Final = 3
CLASSIFIER_MAX_TURNS: Final = 3

# Anything with query()'s signature: (prompt, options) -> async iterator of SDK messages.
QueryFn = Callable[..., AsyncIterator[Message]]


class MatchError(RuntimeError):
    """The classifier did not produce a usable result."""


@dataclass(frozen=True, slots=True)
class DepartmentMatch:
    """Outcome of one classification pass."""

    departments: tuple[str, ...]
    """1-3 skill names, in the order the model listed them (most relevant first)."""
    rationale: str
    model: str
    cost_usd: float | None
    duration_ms: int
    matched_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for the session snapshot and audit trail."""
        return {
            "departments": list(self.departments),
            "rationale": self.rationale,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "matched_at": self.matched_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DepartmentMatch:
        """Inverse of :meth:`to_dict`."""
        return cls(
            departments=tuple(data["departments"]),
            rationale=str(data["rationale"]),
            model=str(data["model"]),
            cost_usd=data.get("cost_usd"),
            duration_ms=int(data["duration_ms"]),
            matched_at=datetime.fromisoformat(data["matched_at"]),
        )


def build_match_schema(skill_names: list[str]) -> dict[str, Any]:
    """JSON Schema for the classifier's output. The 1-3 rule and the allowed set live here."""
    if not skill_names:
        raise ValueError("at least one skill is required to build the match schema")
    return {
        "type": "object",
        "properties": {
            "departments": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(skill_names)},
                "minItems": 1,
                "maxItems": MAX_MATCHES,
                "uniqueItems": True,
                "description": (
                    "Departments whose work this request clearly touches, most relevant first."
                ),
            },
            "rationale": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "One or two sentences citing the specific phrases that drove the choice."
                ),
            },
        },
        "required": ["departments", "rationale"],
        "additionalProperties": False,
    }


def build_match_prompt(skills: Mapping[str, DepartmentSkill], transcript: str) -> str:
    """Render the classification request: department profiles, then the conversation so far."""
    profiles = []
    for skill in skills.values():
        hints = "\n".join(f"  - {h}" for h in skill.hints)
        profiles.append(f"### {skill.name}\n{skill.description}\nSignals:\n{hints}")
    return (
        "Classify which departments' work this discovery conversation touches.\n\n"
        "# Departments\n\n" + "\n\n".join(profiles) + "\n\n"
        "# Conversation so far\n\n" + transcript.strip() + "\n\n"
        "# Rules\n\n"
        f"- Choose between 1 and {MAX_MATCHES} departments. Prefer fewer: include a department "
        "only if the request clearly involves its people, processes, or systems.\n"
        "- A request can genuinely span departments (e.g. vendor onboarding touches finance and "
        "the team that owns the vendor relationship). Do not force a single pick when two apply.\n"
        "- Order by relevance, most relevant first.\n"
        "- In the rationale, quote the specific phrases from the conversation that drove the "
        "choice."
    )


CLASSIFIER_SYSTEM_PROMPT: Final = (
    "You are a precise classifier. You read a short discovery conversation and decide which "
    "departments it concerns. You output only the structured result requested."
)


async def match_departments(
    transcript: str,
    skills: Mapping[str, DepartmentSkill],
    *,
    model: str = DEFAULT_MODEL,
    query_fn: QueryFn = query,
) -> DepartmentMatch:
    """Run the classification pass.

    Args:
        transcript: The conversation so far, rendered as plain text.
        skills: Loaded department skills; their names are the only allowed outputs.
        model: Model ID for the classifier. Kept as a parameter so it can be benchmarked.
        query_fn: Injection point for tests; defaults to the SDK's ``query``.

    Raises:
        MatchError: if no result arrives, the run errored, or the output fails validation.
    """
    if not skills:
        raise ValueError("no skills loaded; cannot classify")
    if not transcript.strip():
        raise ValueError("transcript is empty; nothing to classify")

    schema = build_match_schema(list(skills))
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=CLASSIFIER_SYSTEM_PROMPT,
        setting_sources=[],
        env=AGENT_ENV,
        tools=[],
        # Structured output is delivered through a hidden tool round-trip, so a single
        # agentic turn is not enough; 1 was intermittently hit in the benchmark.
        max_turns=CLASSIFIER_MAX_TURNS,
        effort="low",  # small, well-specified job; depth buys nothing here
        output_format={"type": "json_schema", "schema": schema},
    )

    started = datetime.now(UTC)
    result: ResultMessage | None = None
    try:
        async for message in query_fn(
            prompt=build_match_prompt(skills, transcript), options=options
        ):
            if isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        # Translate SDK failures at the boundary so callers only ever handle MatchError.
        raise MatchError(f"classifier run failed: {exc}") from exc

    if result is None:
        raise MatchError("classifier produced no ResultMessage")
    if result.is_error:
        raise MatchError(f"classifier run failed: {'; '.join(result.errors or ['unknown'])}")

    payload = result.structured_output
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise MatchError(f"classifier output failed schema validation: {exc.message}") from exc
    assert isinstance(payload, dict)  # narrowed by the schema; for the type checker

    match = DepartmentMatch(
        departments=tuple(payload["departments"]),
        rationale=str(payload["rationale"]).strip(),
        model=model,
        cost_usd=result.total_cost_usd,
        duration_ms=result.duration_ms,
        matched_at=started,
    )
    logger.info(
        "matching.done",
        extra={
            "departments": list(match.departments),
            "model": model,
            "cost_usd": match.cost_usd,
            "duration_ms": match.duration_ms,
        },
    )
    return match
