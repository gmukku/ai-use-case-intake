"""Tests for the stakeholder simulator.

No model is called. What is tested here is everything that decides whether a paid run means
anything: that a persona covering six of seven categories is rejected rather than quietly
scoring the agent down, that the prompt actually forbids volunteering, and that a failed
simulator call ends one conversation instead of raising through a run that has already spent
money on the others.
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from blueprint.canvas import CanvasCategory
from blueprint.isolation import AGENT_ENV
from evals.e2e.simulator import (
    Persona,
    PersonaError,
    Utterance,
    build_system_prompt,
    build_turn_prompt,
    load_personas,
    reply,
)

ALL_FACTS = {c.value: f"ground truth for {c.value}" for c in CanvasCategory}
PERSONAS = Path(__file__).resolve().parents[1] / "evals" / "e2e" / "personas.yaml"


def persona(**overrides: Any) -> Persona:
    base: dict[str, Any] = {"id": "p", "brief": "Something takes too long.", "facts": ALL_FACTS}
    return Persona(**{**base, **overrides})


class TestPersonaValidation:
    def test_a_missing_category_is_rejected(self) -> None:
        # The important one. A persona with nothing to say about output_format would make
        # completeness unreachable, and the run would report the fixture's gap as the agent's
        # failure — a number that looks like a finding and is not.
        facts = {k: v for k, v in ALL_FACTS.items() if k != "output_format"}
        with pytest.raises(PersonaError, match="output_format"):
            persona(facts=facts)

    def test_an_unknown_category_is_rejected(self) -> None:
        with pytest.raises(PersonaError, match="budget"):
            persona(facts={**ALL_FACTS, "budget": "lots"})

    def test_blank_ground_truth_is_rejected(self) -> None:
        # Present-but-empty is worse than absent: it passes a key check and answers nothing.
        with pytest.raises(PersonaError, match="input_types"):
            persona(facts={**ALL_FACTS, "input_types": "   "})

    def test_a_persona_needs_an_id_and_a_brief(self) -> None:
        with pytest.raises(PersonaError):
            persona(brief="  ")

    def test_a_complete_persona_is_accepted(self) -> None:
        assert persona().facts["key_stakeholders"]


class TestShippedPersonas:
    """The fixtures themselves, which are as much a part of the eval as the code."""

    def test_they_load_and_are_valid(self) -> None:
        people = load_personas(PERSONAS)
        assert len(people) >= 5

    def test_ids_are_unique(self) -> None:
        people = load_personas(PERSONAS)
        assert len(people) == len({p.id for p in people})

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        body = textwrap.dedent("""
            personas:
              - id: same
                brief: One thing.
                facts: {%s}
              - id: same
                brief: Another thing.
                facts: {%s}
        """) % (
            ", ".join(f"{k}: x" for k in ALL_FACTS),
            ", ".join(f"{k}: y" for k in ALL_FACTS),
        )
        path = tmp_path / "dupes.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(PersonaError, match="duplicate"):
            load_personas(path)

    def test_expected_flags_are_real_flag_keys(self) -> None:
        # A typo here would be a permanently missed flag: the suite would report a false
        # negative on every run for a rule that was never going to fire under that name.
        #
        # Read out of review.py rather than written down here. The first version of this test
        # hardcoded the names and got three of them wrong — inventing `external_sharing`,
        # `automation_without_review` and `web_grounding`, and missing `external_sources` and
        # `workflow_automation`. A list that has to agree with another list is one list.
        known = _risk_flag_keys()
        assert {"named_integration", "writes_to_system"} <= known, f"scanner found only {known}"
        for p in load_personas(PERSONAS):
            unknown = set(p.expect_flags) - known
            assert not unknown, f"{p.id} expects unknown flag(s): {sorted(unknown)}"

    def test_expected_flags_are_reachable_from_the_personas_own_facts(self) -> None:
        """A persona must not expect a flag its own ground truth cannot raise.

        Free, deterministic, and it would have saved a paid run. One persona expected
        `writes_to_system` from facts the rule could not match -- every verb in them was past
        tense -- which would have reported a false negative on every run forever, for a rule
        that was never going to fire on that wording. Another expected no flags while naming
        Intercom. Both were caught by running `assess_risk` over the fixtures themselves.
        """
        from blueprint.review import assess_risk
        from blueprint.skills import load_skills

        skills = load_skills()
        for p in load_personas(PERSONAS):
            risk = assess_risk(dict(p.facts), open_gaps={}, web_searches=0, skills=skills)
            unreachable = sorted(set(p.expect_flags) - {f.key for f in risk.flags})
            assert not unreachable, (
                f"{p.id}: expects {unreachable}, which its own ground truth does not raise"
            )

    def test_the_set_covers_both_sides_of_the_risk_gate(self) -> None:
        # A suite where every persona expects a flag cannot detect a gate that flags
        # everything, and one where none do cannot detect a gate that flags nothing.
        people = load_personas(PERSONAS)
        assert any(p.expect_flags for p in people), "no persona expects a flag"
        assert any(not p.expect_flags for p in people), "no persona expects a clean gate"

    def test_at_least_one_persona_should_match_more_than_one_department(self) -> None:
        # Skill matching is one-to-many by design; nothing else exercises that end to end.
        people = load_personas(PERSONAS)
        assert any(len(p.expect_departments) > 1 for p in people)


class TestSystemPrompt:
    def test_it_forbids_volunteering(self) -> None:
        # The whole point of the module. A simulator that offers all seven categories
        # unprompted scores the agent at 100% no matter how the agent behaves.
        text = build_system_prompt(persona())
        assert "Answer ONLY what you were just asked" in text
        assert "Never bring up something nobody asked" in text

    def test_every_fact_reaches_the_prompt(self) -> None:
        text = build_system_prompt(persona())
        for value in ALL_FACTS.values():
            assert value in text

    def test_withholds_and_style_appear_when_set(self) -> None:
        text = build_system_prompt(
            persona(withholds=("Real ticket text.",), style="Guarded and brief.")
        )
        assert "Real ticket text." in text
        assert "Guarded and brief." in text

    def test_nothing_empty_is_rendered_when_they_are_not_set(self) -> None:
        text = build_system_prompt(persona())
        assert "You will not give up" not in text
        assert "How you come across" not in text

    def test_it_tells_the_persona_not_to_invent(self) -> None:
        # An inventing simulator makes a run unreproducible and can hand the agent a system
        # name that is not in the ground truth, which would then score as a spurious flag.
        assert "Never invent a specific number" in build_system_prompt(persona())


class TestTurnPrompt:
    def test_it_labels_both_sides(self) -> None:
        text = build_turn_prompt(
            [Utterance("you", "We check packets by hand."), Utterance("agent", "Who does that?")]
        )
        assert "You: We check packets by hand." in text
        assert "Them: Who does that?" in text

    def test_an_empty_transcript_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            build_turn_prompt([])


class TestReply:
    @staticmethod
    def _fake(messages: list[Any]) -> Any:
        async def q(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            q.seen_options = options  # type: ignore[attr-defined]
            q.seen_prompt = prompt  # type: ignore[attr-defined]
            for m in messages:
                yield m

        return q

    @pytest.mark.asyncio
    async def test_it_is_isolated_like_every_other_agent(self) -> None:
        # evals/ sits outside the package the isolation scanner walks, so this asserts it at
        # runtime too. A simulator that loaded the developer's CLAUDE.md would be reading the
        # answer sheet.
        from claude_agent_sdk import ResultMessage

        q = self._fake(
            [
                _assistant("Two coordinators."),
                ResultMessage(
                    subtype="success",
                    duration_ms=5,
                    duration_api_ms=4,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.001,
                ),
            ]
        )
        await reply(persona(), [Utterance("agent", "Who does the work?")], query_fn=q)
        options = q.seen_options
        assert options.setting_sources == []
        assert options.env == AGENT_ENV
        assert options.tools == []

    @pytest.mark.asyncio
    async def test_a_successful_turn_returns_the_text_and_the_cost(self) -> None:
        from claude_agent_sdk import ResultMessage

        q = self._fake(
            [
                _assistant("Two HR coordinators, "),
                _assistant("and payroll waits on them."),
                ResultMessage(
                    subtype="success",
                    duration_ms=7,
                    duration_api_ms=6,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.002,
                ),
            ]
        )
        answer = await reply(persona(), [Utterance("agent", "Who?")], query_fn=q)
        assert answer.text == "Two HR coordinators, and payroll waits on them."
        assert answer.cost_usd == 0.002
        assert not answer.is_error

    @pytest.mark.asyncio
    async def test_an_sdk_failure_ends_one_conversation_rather_than_the_run(self) -> None:
        async def q(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            # An async generator that raises before its first yield, which is how the SDK
            # fails when the CLI cannot start at all.
            if prompt:
                raise RuntimeError("overloaded")
            yield None

        answer = await reply(persona(), [Utterance("agent", "Who?")], query_fn=q)
        assert answer.is_error and "overloaded" in answer.errors[0]

    @pytest.mark.asyncio
    async def test_an_empty_reply_is_an_error_not_an_empty_turn(self) -> None:
        # Sending "" to the agent would waste a turn and read as the stakeholder saying
        # nothing, which is not a thing a person does.
        from claude_agent_sdk import ResultMessage

        q = self._fake(
            [
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.0,
                )
            ]
        )
        answer = await reply(persona(), [Utterance("agent", "Who?")], query_fn=q)
        assert answer.is_error


def _risk_flag_keys() -> set[str]:
    """Every flag key `assess_risk` can raise, read from the source it raises them in."""
    import re

    source = (Path(__file__).resolve().parents[1] / "blueprint" / "review.py").read_text(
        encoding="utf-8"
    )
    return set(re.findall(r'key="([a-z_]+)"', source))


def _assistant(text: str) -> Any:
    from claude_agent_sdk import AssistantMessage, TextBlock

    return AssistantMessage(content=[TextBlock(text=text)], model="test")


class TestScoring:
    """The pure half of the runner: a conversation turned into numbers.

    Worth testing without spending anything, because these are the numbers a paid run is
    bought for, and a scorer that quietly reports zero missed flags is indistinguishable from
    a system with no missed flags.
    """

    @staticmethod
    def _snapshot(current: dict[str, Any]) -> dict[str, Any]:
        return {
            "canvas": {"current": current},
            "open_gaps": {},
            "web_search": {"attempts": []},
            "match": {"departments": ["hr"]},
        }

    def test_uncaptured_counts_every_category_the_agent_never_recorded(self) -> None:
        from blueprint.skills import load_skills
        from evals.e2e.run import ConversationResult, _score

        result = ConversationResult(persona_id="p", variant="m")
        snapshot = self._snapshot({"key_stakeholders": "Two coordinators.", "key_activities": None})
        _score(result, persona(), snapshot, load_skills())

        assert result.captured == ["key_stakeholders"]
        assert "key_activities" in result.uncaptured
        assert "output_format" in result.uncaptured, "an absent key counts the same as a null one"
        assert len(result.uncaptured) == 6

    def test_a_missed_flag_is_recorded_as_a_false_negative(self) -> None:
        from blueprint.skills import load_skills
        from evals.e2e.run import ConversationResult, _score

        result = ConversationResult(persona_id="p", variant="m")
        # Nothing in the canvas names a system, so named_integration cannot fire — which is
        # exactly the false negative this suite exists to surface.
        _score(
            result,
            persona(expect_flags=("named_integration",)),
            self._snapshot({"system_integrations": "Not discussed."}),
            load_skills(),
        )
        assert result.missed_flags == ["named_integration"]

    def test_a_flag_that_fires_is_not_a_false_negative(self) -> None:
        from blueprint.skills import load_skills
        from evals.e2e.run import ConversationResult, _score

        result = ConversationResult(persona_id="p", variant="m")
        _score(
            result,
            persona(expect_flags=("named_integration",)),
            self._snapshot({"system_integrations": "We read and write records in Salesforce."}),
            load_skills(),
        )
        assert result.missed_flags == []
        assert "named_integration" in result.predicted_flags

    def test_departments_expected_but_not_matched_are_reported(self) -> None:
        from blueprint.skills import load_skills
        from evals.e2e.run import ConversationResult, _score

        result = ConversationResult(persona_id="p", variant="m")
        _score(
            result,
            persona(expect_departments=("hr", "finance")),
            self._snapshot({}),
            load_skills(),
        )
        assert result.missed_departments == ["finance"]

    def test_a_failed_conversation_is_counted_but_never_scored(self) -> None:
        # The lesson the completeness suite paid for: an errored call that fabricates a
        # verdict puts API weather into the headline metric.
        from evals.e2e.run import ConversationResult, summarize

        ok = ConversationResult(persona_id="a", variant="m", completed=True, turns=5)
        broken = ConversationResult(persona_id="b", variant="m", error="overloaded")
        summary = summarize([ok, broken])["m"]

        assert summary["calls"] == 2
        assert summary["errors"] == 1
        assert summary["scored"] == 1
        assert summary["completion_rate"] == 1.0, "the failed run must not drag the rate down"

    def test_completion_rate_is_none_rather_than_a_crash_when_nothing_scored(self) -> None:
        from evals.e2e.run import ConversationResult, summarize

        summary = summarize([ConversationResult(persona_id="a", variant="m", error="x")])["m"]
        assert summary["completion_rate"] is None
