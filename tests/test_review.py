from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage

from blueprint.canvas import CanvasCategory
from blueprint.review import (
    ReviewAction,
    ReviewDecision,
    RiskLevel,
    SpecSummaryError,
    assess_risk,
    compile_spec,
    review_status,
    summarize_spec,
)
from blueprint.skills import SKILLS_DIR, load_skills

SKILLS = load_skills(SKILLS_DIR)


def current(**overrides: str) -> dict[str, str | None]:
    base = {c.value: f"{c.label} summary." for c in CanvasCategory}
    base.update(overrides)
    return base  # type: ignore[return-value]


def risk(**overrides: str) -> Any:
    return assess_risk(current(**overrides), open_gaps={}, web_searches=0, skills=SKILLS)


def flags(assessment: Any) -> dict[str, Any]:
    return {f.key: f for f in assessment.flags}


class TestRiskRules:
    def test_clean_request_is_low(self) -> None:
        r = risk(system_integrations="No other systems are involved.")
        assert r.level is RiskLevel.LOW and r.flags == ()
        assert not r.needs_extra_scrutiny

    def test_named_integration_from_skill_lists_and_generic_types(self) -> None:
        r = risk(system_integrations="Forms come in through Workday; data ends up in our HRIS.")
        f = flags(r)["named_integration"]
        assert f.level is RiskLevel.ELEVATED and f.category is CanvasCategory.SYSTEM_INTEGRATIONS
        assert "Workday" in f.evidence and "hris" in f.evidence
        assert r.needs_extra_scrutiny

    def test_email_alone_is_not_an_integration(self) -> None:
        r = risk(system_integrations="They chase people over email.")
        assert "named_integration" not in flags(r)

    def test_writes_to_system_is_high_unless_negated(self) -> None:
        r = risk(output_format="Extract the fields and write them into the HRIS automatically.")
        f = flags(r)["writes_to_system"]
        assert f.level is RiskLevel.HIGH and r.level is RiskLevel.HIGH
        assert "write them into the HRIS" in f.evidence

        r2 = risk(
            output_format="A checklist of what is missing. Explicitly out of scope: writing data "
            "into the HRIS or auto-filling forms."
        )
        assert "writes_to_system" not in flags(r2)

    def test_workflow_automation_flag(self) -> None:
        r = risk(output_format="Workflow automation: automatically send the chase email.")
        assert flags(r)["workflow_automation"].level is RiskLevel.ELEVATED

    def test_sensitive_data_scans_every_category(self) -> None:
        r = risk(input_types="Scanned I-9 and W-4 forms plus direct deposit details.")
        f = flags(r)["sensitive_data"]
        assert f.category is None
        assert "i-9" in f.evidence and "direct deposit" in f.evidence

    def test_external_sources_from_text_or_search_count(self) -> None:
        r = risk(input_source="Answers should come from the public website of the state agency.")
        assert flags(r)["external_sources"].category is CanvasCategory.INPUT_SOURCE
        r2 = assess_risk(current(), open_gaps={}, web_searches=2, skills=SKILLS)
        assert "2 web search" in flags(r2)["external_sources"].evidence

    def test_open_gaps_is_attention_only(self) -> None:
        r = assess_risk(
            current(system_integrations="none"),
            open_gaps={"key_activities": ["frequency"]},
            web_searches=0,
            skills=SKILLS,
        )
        assert r.level is RiskLevel.ATTENTION
        assert "key_activities: frequency" in flags(r)["open_gaps"].evidence
        assert not r.needs_extra_scrutiny

    def test_level_is_the_highest_flag(self) -> None:
        r = risk(
            system_integrations="Workday",
            output_format="push the values into Workday",
            input_types="SSN",
        )
        assert r.level is RiskLevel.HIGH
        assert {f.key for f in r.flags} >= {
            "named_integration",
            "writes_to_system",
            "sensitive_data",
        }


def snapshot(*, complete: bool = True, check: bool = True) -> dict[str, Any]:
    cats = list(CanvasCategory)
    entries = [
        {"category": c.value, "summary": f"{c.label} summary.", "turn": i + 1, "recorded_at": "t"}
        for i, c in enumerate(cats)
    ]
    entries.append(
        {
            "category": "key_stakeholders",
            "summary": "HR ops + payroll",
            "turn": 9,
            "recorded_at": "t",
        }
    )
    cur = current(
        key_stakeholders="HR ops + payroll",
        system_integrations="Forms live in Workday.",
    )
    if not complete:
        cur["output_format"] = None
    return {
        "session_id": "sess-42",
        "turn": 9,
        "total_cost_usd": 0.15,
        "match": {"departments": ["hr", "finance"], "rationale": "payroll mentioned"},
        "web_search": {"max_calls": 3, "attempts": [{"query": "I-9 deadline", "allowed": True}]},
        "canvas": {
            "entries": entries,
            "current": cur,
            "missing": [] if complete else ["output_format"],
            "is_complete": complete,
        },
        "completeness": {
            "assessments": [
                {
                    "category": "key_stakeholders",
                    "version": 1,
                    "sufficient": False,
                    "missing": ["dependents"],
                },
                {"category": "key_stakeholders", "version": 2, "sufficient": True, "missing": []},
                {
                    "category": "value_proposition",
                    "version": 1,
                    "sufficient": False,
                    "missing": ["measurable_impact"],
                },
            ],
            "rounds": {"key_stakeholders": 1, "value_proposition": 2},
        }
        if check
        else None,
        "open_gaps": {"value_proposition": ["measurable_impact"]} if check else {},
        "turns": [
            {
                "turn": 1,
                "user_text": "I run onboarding and the paperwork is a mess",
                "assistant_text": "Who?",
                "tool_calls": [
                    {"id": "a", "name": "mcp__sop__search_sops", "input": {"query": "onboarding"}},
                    {
                        "id": "b",
                        "name": "mcp__sop__read_sop",
                        "input": {"sop_id": "hr-onboarding-packet-review"},
                    },
                ],
            },
            {
                "turn": 2,
                "user_text": "...",
                "assistant_text": "Here is the recap.",
                "tool_calls": [],
            },
        ],
    }


class TestCompileSpec:
    def test_assembles_everything_deterministically(self) -> None:
        spec = compile_spec(snapshot(), SKILLS)
        assert spec.version == 1 and spec.session_id == "sess-42"
        assert spec.title == "I run onboarding and the paperwork is a mess"
        assert spec.narrative == "Here is the recap."
        assert [c.category for c in spec.categories] == list(CanvasCategory)
        ks = spec.categories[0]
        assert ks.summary == "HR ops + payroll" and ks.version == 2 and ks.sufficient is True
        vp = next(c for c in spec.categories if c.category is CanvasCategory.VALUE_PROPOSITION)
        assert vp.sufficient is False and vp.missing == ("measurable_impact",)
        assert spec.departments == ("hr", "finance")
        assert spec.sops_consulted == ("hr-onboarding-packet-review",)
        assert spec.web_sources == ("I-9 deadline",)
        assert spec.open_gaps == {"value_proposition": ["measurable_impact"]}
        assert {f.key for f in spec.risk.flags} >= {
            "named_integration",
            "external_sources",
            "open_gaps",
        }
        assert spec.risk.level is RiskLevel.ELEVATED
        assert spec.turns == 9 and spec.total_cost_usd == 0.15

    def test_title_and_narrative_overrides(self) -> None:
        spec = compile_spec(snapshot(), SKILLS, title="Packet checker", narrative="Two sentences.")
        assert spec.title == "Packet checker" and spec.narrative == "Two sentences."

    def test_check_off_means_sufficiency_unknown(self) -> None:
        spec = compile_spec(snapshot(check=False), SKILLS)
        assert all(c.sufficient is None for c in spec.categories)
        assert spec.open_gaps == {}

    def test_incomplete_canvas_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="incomplete"):
            compile_spec(snapshot(complete=False), SKILLS)

    def test_to_dict_round_trip_shape(self) -> None:
        d = compile_spec(snapshot(), SKILLS).to_dict()
        assert (
            d["risk"]["level"] == "elevated" and d["categories"][0]["label"] == "Key stakeholders"
        )


class TestSummarizeSpec:
    async def test_happy_path(self) -> None:
        async def q(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            assert "Key stakeholders: HR ops + payroll" in prompt
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="x",
                total_cost_usd=0.01,
                structured_output={
                    "title": "Packet checker for HR Ops.",
                    "narrative": "A tool that flags missing items in onboarding packets.",
                },
            )

        spec = compile_spec(snapshot(), SKILLS)
        title, narrative = await summarize_spec(spec.categories, query_fn=q)
        assert title == "Packet checker for HR Ops"  # trailing period stripped
        assert narrative.startswith("A tool that flags")

    async def test_failure_is_typed(self) -> None:
        async def q(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="x",
                total_cost_usd=0.01,
                structured_output={"title": "x"},
            )

        with pytest.raises(SpecSummaryError):
            await summarize_spec(compile_spec(snapshot(), SKILLS).categories, query_fn=q)


class TestDecisions:
    def test_approve_needs_no_note_but_others_do(self) -> None:
        ReviewDecision(ReviewAction.APPROVE, "goutam", "", 1, RiskLevel.LOW)
        with pytest.raises(ValueError, match="requires a note"):
            ReviewDecision(ReviewAction.REJECT, "goutam", "", 1, RiskLevel.LOW)
        with pytest.raises(ValueError, match="requires a note"):
            ReviewDecision(ReviewAction.SEND_BACK, "goutam", "  ", 1, RiskLevel.LOW)
        with pytest.raises(ValueError, match="reviewer"):
            ReviewDecision(ReviewAction.APPROVE, " ", "", 1, RiskLevel.LOW)

    def test_round_trip_and_status(self) -> None:
        d = ReviewDecision(
            ReviewAction.SEND_BACK,
            "goutam",
            "Which HRIS?",
            1,
            RiskLevel.ELEVATED,
            decided_at=datetime(2026, 9, 14, tzinfo=UTC),
        )
        assert ReviewDecision.from_dict(d.to_dict()) == d
        assert review_status([]) == "pending"
        assert review_status([d]) == "sent_back"
        approve = ReviewDecision(ReviewAction.APPROVE, "goutam", "", 2, RiskLevel.ELEVATED)
        assert review_status([d, approve]) == "approved"
        assert (
            review_status([ReviewDecision(ReviewAction.REJECT, "g", "no", 1, RiskLevel.LOW)])
            == "rejected"
        )


class TestTitleFallbackIgnoresTheReviewer:
    """A send-back note is a turn's user_text, and it is not the requester speaking."""

    def test_a_reviewer_turn_is_never_the_title(self) -> None:
        snap = snapshot()
        snap["turns"].insert(
            0,
            {
                "turn": 1,
                "origin": "reviewer",
                "user_text": "Which HRIS is the packet data actually going into?",
                "assistant_text": "One more thing about your systems.",
                "tool_calls": [],
            },
        )
        spec = compile_spec(snap, SKILLS)
        assert spec.title == "I run onboarding and the paperwork is a mess"
        assert "Which HRIS" not in spec.title

    def test_falls_back_cleanly_when_only_a_reviewer_turn_survives(self) -> None:
        snap = snapshot()
        snap["turns"] = [
            {
                "turn": 11,
                "origin": "reviewer",
                "user_text": "Which HRIS exactly?",
                "assistant_text": "Which system is it?",
                "tool_calls": [],
            }
        ]
        spec = compile_spec(snap, SKILLS)
        assert spec.title == ""  # empty beats quoting the reviewer back at themselves
        assert spec.narrative == "Which system is it?"  # the agent's own voice is fine

    def test_a_turn_with_no_user_text_is_skipped(self) -> None:
        snap = snapshot()
        snap["turns"].insert(
            0, {"turn": 0, "user_text": "", "assistant_text": "", "tool_calls": []}
        )
        spec = compile_spec(snap, SKILLS)
        assert spec.title == "I run onboarding and the paperwork is a mess"
