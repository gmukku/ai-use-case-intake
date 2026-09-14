from collections.abc import AsyncIterator
from typing import Any

import jsonschema
import pytest
from claude_agent_sdk import ResultMessage

from blueprint.matching import (
    MAX_MATCHES,
    MatchError,
    build_match_prompt,
    build_match_schema,
    match_departments,
)
from blueprint.skills import SKILLS_DIR, load_skills

SKILLS = load_skills(SKILLS_DIR)


def fake_query(structured: Any, *, is_error: bool = False) -> Any:
    """Build a query_fn that records the options it was called with and returns `structured`."""
    calls: list[dict[str, Any]] = []

    async def _query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        calls.append({"prompt": prompt, "options": options})
        yield ResultMessage(
            subtype="error" if is_error else "success",
            duration_ms=250,
            duration_api_ms=200,
            is_error=is_error,
            num_turns=1,
            session_id="cls-1",
            total_cost_usd=0.004,
            structured_output=structured,
            errors=["boom"] if is_error else None,
        )

    _query.calls = calls  # type: ignore[attr-defined]
    return _query


class TestSchema:
    def test_enum_is_the_loaded_skill_names_sorted(self) -> None:
        schema = build_match_schema(["sales", "hr"])
        assert schema["properties"]["departments"]["items"]["enum"] == ["hr", "sales"]

    def test_one_to_three_unique(self) -> None:
        schema = build_match_schema(list(SKILLS))
        ok = {"departments": ["hr", "finance"], "rationale": "x"}
        jsonschema.validate(ok, schema)
        for bad in (
            {"departments": [], "rationale": "x"},
            {"departments": ["hr", "hr"], "rationale": "x"},
            {"departments": ["hr", "finance", "sales", "customer_success"], "rationale": "x"},
            {"departments": ["legal"], "rationale": "x"},
            {"departments": ["hr"]},
            {"departments": ["hr"], "rationale": ""},
            {"departments": ["hr"], "rationale": "x", "confidence": 0.9},
        ):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)

    def test_max_matches_is_three(self) -> None:
        assert MAX_MATCHES == 3

    def test_requires_at_least_one_skill(self) -> None:
        with pytest.raises(ValueError):
            build_match_schema([])


class TestPrompt:
    def test_includes_every_department_and_its_hints(self) -> None:
        prompt = build_match_prompt(SKILLS, "we do onboarding")
        for skill in SKILLS.values():
            assert f"### {skill.name}" in prompt
            assert skill.hints[0] in prompt
        assert "we do onboarding" in prompt
        assert "between 1 and 3" in prompt


class TestMatchDepartments:
    async def test_happy_path_and_options_wiring(self) -> None:
        q = fake_query({"departments": ["hr", "finance"], "rationale": "'new hires' and 'W-4'"})
        match = await match_departments("we onboard new hires", SKILLS, query_fn=q)

        assert match.departments == ("hr", "finance")
        assert match.rationale == "'new hires' and 'W-4'"
        assert match.cost_usd == 0.004
        assert match.duration_ms == 250

        options = q.calls[0]["options"]
        assert options.setting_sources == []
        assert options.tools == []
        assert options.max_turns == 3
        assert options.effort == "low"
        assert options.output_format["type"] == "json_schema"
        assert "hr" in options.output_format["schema"]["properties"]["departments"]["items"]["enum"]

    async def test_model_is_a_parameter(self) -> None:
        q = fake_query({"departments": ["sales"], "rationale": "demos"})
        match = await match_departments(
            "follow-ups after demos", SKILLS, model="claude-sonnet-5", query_fn=q
        )
        assert match.model == "claude-sonnet-5"
        assert q.calls[0]["options"].model == "claude-sonnet-5"

    async def test_invalid_output_is_rejected_even_if_api_let_it_through(self) -> None:
        q = fake_query({"departments": ["legal"], "rationale": "contracts"})
        with pytest.raises(MatchError, match="schema validation"):
            await match_departments("contracts", SKILLS, query_fn=q)

    async def test_sdk_exception_becomes_match_error(self) -> None:
        from claude_agent_sdk import ResultError

        async def exploding(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            if prompt:  # always true; keeps this an async generator without dead code
                raise ResultError("Reached maximum number of turns (1)")
            yield

        with pytest.raises(MatchError, match="maximum number of turns"):
            await match_departments("anything", SKILLS, query_fn=exploding)

    async def test_error_result_raises(self) -> None:
        q = fake_query(None, is_error=True)
        with pytest.raises(MatchError, match="boom"):
            await match_departments("anything", SKILLS, query_fn=q)

    async def test_empty_inputs_are_rejected_before_calling_the_model(self) -> None:
        q = fake_query({"departments": ["hr"], "rationale": "x"})
        with pytest.raises(ValueError, match="empty"):
            await match_departments("   ", SKILLS, query_fn=q)
        with pytest.raises(ValueError, match="no skills"):
            await match_departments("text", {}, query_fn=q)
        assert q.calls == []

    async def test_to_dict(self) -> None:
        q = fake_query({"departments": ["customer_success"], "rationale": "tickets"})
        match = await match_departments("support tickets", SKILLS, query_fn=q)
        d = match.to_dict()
        assert d["departments"] == ["customer_success"]
        assert d["model"] == "claude-opus-5"
        assert "matched_at" in d
