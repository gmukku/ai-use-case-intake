from collections.abc import AsyncIterator
from typing import Any

import jsonschema
import pytest
from claude_agent_sdk import ResultMessage

from blueprint.canvas import CATEGORY_RUBRIC, CanvasCategory
from blueprint.completeness import (
    AssessmentError,
    assess_capture,
    build_assessment_prompt,
    build_assessment_schema,
)

KA = CanvasCategory.KEY_ACTIVITIES
KA_KEYS = [e.key for e in CATEGORY_RUBRIC[KA]]


def fake_query(structured: Any, *, is_error: bool = False) -> Any:
    calls: list[dict[str, Any]] = []

    async def _query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        calls.append({"prompt": prompt, "options": options})
        yield ResultMessage(
            subtype="error" if is_error else "success",
            duration_ms=300,
            duration_api_ms=250,
            is_error=is_error,
            num_turns=1,
            session_id="chk-1",
            total_cost_usd=0.008,
            structured_output=structured,
            errors=["boom"] if is_error else None,
        )

    _query.calls = calls  # type: ignore[attr-defined]
    return _query


class TestRubric:
    def test_every_category_has_a_rubric_with_unique_keys(self) -> None:
        assert set(CATEGORY_RUBRIC) == set(CanvasCategory)
        for cat, elements in CATEGORY_RUBRIC.items():
            keys = [e.key for e in elements]
            assert keys, cat
            assert len(keys) == len(set(keys)), cat
            assert all(e.description.strip() for e in elements), cat


class TestSchema:
    def test_enums_are_the_rubric_keys(self) -> None:
        schema = build_assessment_schema(KA)
        assert schema["properties"]["missing"]["items"]["enum"] == KA_KEYS
        assert schema["properties"]["satisfied"]["items"]["enum"] == KA_KEYS

    def test_validation(self) -> None:
        schema = build_assessment_schema(KA)
        jsonschema.validate(
            {
                "satisfied": ["process_steps"],
                "missing": ["headcount", "frequency"],
                "question": "q",
            },
            schema,
        )
        bad: dict[str, Any]
        for bad in (
            {"satisfied": ["nope"], "missing": [], "question": ""},
            {"satisfied": [], "missing": ["headcount", "headcount"], "question": ""},
            {"satisfied": [], "missing": []},
            {"satisfied": [], "missing": [], "question": "", "extra": 1},
        ):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)


class TestPrompt:
    def test_contains_rubric_and_summary(self) -> None:
        prompt = build_assessment_prompt(KA, "  Coordinators check packets daily.  ")
        for e in CATEGORY_RUBRIC[KA]:
            assert f"`{e.key}`" in prompt and e.description in prompt
        assert "Coordinators check packets daily." in prompt
        assert "Key activities" in prompt


class TestAssessCapture:
    async def test_insufficient_with_question(self) -> None:
        q = fake_query(
            {
                "satisfied": ["process_steps"],
                "missing": ["headcount", "frequency"],
                "question": "How many people do this, and how often?",
            }
        )
        a = await assess_capture(KA, "Coordinators check packets.", version=1, query_fn=q)
        assert not a.sufficient
        assert a.missing == ("headcount", "frequency")  # rubric order, not model order
        assert a.satisfied == ("process_steps",)
        assert a.question == "How many people do this, and how often?"
        assert a.version == 1 and a.cost_usd == 0.008 and a.duration_ms == 300
        options = q.calls[0]["options"]
        assert options.setting_sources == [] and options.tools == []
        assert options.effort == "low" and options.max_turns == 3
        assert options.output_format["type"] == "json_schema"

    async def test_sufficient_has_no_question(self) -> None:
        q = fake_query({"satisfied": KA_KEYS, "missing": [], "question": ""})
        a = await assess_capture(KA, "3 people check packets daily.", version=2, query_fn=q)
        assert a.sufficient and a.question is None and a.missing == ()

    async def test_sufficient_ignores_stray_question_text(self) -> None:
        q = fake_query({"satisfied": KA_KEYS, "missing": [], "question": "Anything else?"})
        a = await assess_capture(KA, "3 people, daily, steps.", version=1, query_fn=q)
        assert a.sufficient and a.question is None

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (
                {"satisfied": ["process_steps"], "missing": ["process_steps"], "question": "q"},
                "inconsistent",
            ),
            ({"satisfied": ["process_steps"], "missing": [], "question": ""}, "inconsistent"),
            ({"satisfied": [], "missing": KA_KEYS, "question": ""}, "no follow-up question"),
            ({"satisfied": ["legal"], "missing": [], "question": ""}, "schema validation"),
        ],
    )
    async def test_bad_output_is_rejected(self, payload: Any, message: str) -> None:
        with pytest.raises(AssessmentError, match=message):
            await assess_capture(KA, "x", version=1, query_fn=fake_query(payload))

    async def test_error_result_and_empty_summary(self) -> None:
        with pytest.raises(AssessmentError, match="boom"):
            await assess_capture(KA, "x", version=1, query_fn=fake_query(None, is_error=True))
        with pytest.raises(ValueError, match="empty"):
            await assess_capture(KA, "   ", version=1, query_fn=fake_query({}))

    async def test_to_dict(self) -> None:
        q = fake_query({"satisfied": [], "missing": KA_KEYS, "question": "Tell me more?"})
        d = (await assess_capture(KA, "vague", version=1, query_fn=q)).to_dict()
        assert d["category"] == "key_activities" and d["sufficient"] is False
        assert d["missing"] == KA_KEYS and d["question"] == "Tell me more?"
