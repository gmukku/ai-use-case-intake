import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from blueprint.canvas import CATEGORY_RUBRIC, CanvasCategory
from blueprint.completeness import Assessment, AssessmentError
from blueprint.discovery import EXAMPLES_TOOL_FULL_NAME, RECORD_TOOL_FULL_NAME
from blueprint.matching import DepartmentMatch, MatchError
from blueprint.orchestrator import (
    MATCH_FALLBACK_TURN,
    MAX_CLARIFICATION_ROUNDS,
    MAX_MATCH_ATTEMPTS,
    MAX_USER_TEXT_CHARS,
    DepartmentsMatched,
    DiscoverySession,
    SessionNotStartedError,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
    TurnProtocolError,
    validate_user_text,
)
from blueprint.review import ReviewAction, ReviewDecision, RiskLevel
from blueprint.skills import SKILLS_DIR, load_skills

SKILLS = load_skills(SKILLS_DIR)

# --- a scripted stand-in for ClaudeSDKClient --------------------------------------------
#
# Each "script" entry is a list of steps for one turn. A step is either a str (assistant text)
# or a dict (tool-call arguments: with "summary" it is a record call, with only "category" it
# is an examples call). The fake emits SDK message objects the way the real client would, and
# *actually dispatches through the real in-process MCP server* for dict steps, so schema
# validation, turn-number injection and `captured` bookkeeping are exercised for real.

Step = str | dict[str, Any]


def result_message(
    *, session_id: str = "sess-1", cost: float | None = 0.01, is_error: bool = False
) -> ResultMessage:
    return ResultMessage(
        subtype="error" if is_error else "success",
        duration_ms=120,
        duration_api_ms=100,
        is_error=is_error,
        num_turns=2,
        session_id=session_id,
        total_cost_usd=cost,
        errors=["budget exceeded"] if is_error else None,
    )


def _tool_step(step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "summary" in step:
        return RECORD_TOOL_FULL_NAME, step
    return EXAMPLES_TOOL_FULL_NAME, step


class FakeClient:
    def __init__(self, options: ClaudeAgentOptions, script: list[list[Step]]) -> None:
        self.options = options
        self.script = script
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self.omit_result = False
        self.turn_cost = 0.01
        self.running_total = 0.0
        self.stream_chunk = 4  # chars per text_delta; mirrors the SDK's partial messages
        self.tool_results: list[str] = []
        # Failure knobs, consumed on the next receive_response():
        self.raise_result_error: ResultError | None = None  # after the text, before the result
        self.hang = False  # never deliver anything (idle-timeout path)
        self.emit_rate_limit = False

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        if self.hang:
            await asyncio.sleep(3600)
        if self.emit_rate_limit:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="allowed_warning", utilization=0.9),
                uuid="rl",
                session_id="sess-1",
            )
        steps = self.script.pop(0)
        for i, step in enumerate(steps):
            if isinstance(step, str):
                for j in range(0, len(step), self.stream_chunk):
                    yield self._stream_event(
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {
                                "type": "text_delta",
                                "text": step[j : j + self.stream_chunk],
                            },
                        }
                    )
                yield AssistantMessage(content=[TextBlock(text=step)], model="fake")
            else:
                tool_name, args = _tool_step(step)
                yield self._stream_event(
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": f"tu-{i}",
                            "name": tool_name,
                            "input": {},
                        },
                    }
                )
                yield AssistantMessage(
                    content=[ToolUseBlock(id=f"tu-{i}", name=tool_name, input=args)],
                    model="fake",
                )
                self.tool_results.append(await self._call_tool(tool_name, args))
        if self.raise_result_error is not None:
            # Mirrors the SDK: the CLI emits an error result then exits, surfaced as ResultError.
            self.disconnected = True
            raise self.raise_result_error
        if not self.omit_result:
            self.running_total += self.turn_cost  # the SDK reports a running total
            yield result_message(cost=self.running_total)

    @staticmethod
    def _stream_event(event: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid="ev", session_id="sess-1", event=event)

    async def _call_tool(self, full_name: str, args: dict[str, Any]) -> str:
        """Dispatch through the real in-process MCP server, so the SDK's schema validation runs."""
        from mcp.types import CallToolRequestParams

        server: Any = self.options.mcp_servers
        entry = server["canvas"]["instance"].get_request_handler("tools/call")
        params = CallToolRequestParams(name=full_name.split("__")[-1], arguments=args)
        result = await entry.handler(None, params)
        return str(result.content[0].text)


class FakeMatcher:
    """Stand-in for match_departments: records calls, returns a fixed match or raises."""

    def __init__(self, departments: tuple[str, ...] = ("hr",), *, fail_times: int = 0) -> None:
        self.departments = departments
        self.fail_times = fail_times
        self.calls: list[str] = []

    async def __call__(self, transcript: str, skills: Any, *, model: str) -> DepartmentMatch:
        self.calls.append(transcript)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MatchError("classifier down")
        return DepartmentMatch(
            departments=self.departments,
            rationale="test",
            model=model,
            cost_usd=0.004,
            duration_ms=50,
            matched_at=datetime.now(UTC),
        )


class FakeAssessor:
    """Stand-in for assess_capture. Verdicts are scripted per category: a list of `missing`
    tuples consumed in order (empty tuple = sufficient); unlisted categories are sufficient."""

    def __init__(self, script: dict[str, list[tuple[str, ...]]] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[tuple[str, int]] = []

    async def __call__(
        self, category: CanvasCategory, summary: str, *, version: int, model: str
    ) -> Assessment:
        self.calls.append((category.value, version))
        queue = self.script.get(category.value)
        missing = queue.pop(0) if queue else ()
        keys = [e.key for e in CATEGORY_RUBRIC[category]]
        return Assessment(
            category=category,
            version=version,
            satisfied=tuple(k for k in keys if k not in missing),
            missing=tuple(k for k in keys if k in missing),
            question=f"Could you say more about {', '.join(missing)}?" if missing else None,
            model=model,
            cost_usd=0.008,
            duration_ms=40,
            assessed_at=datetime.now(UTC),
        )


def make_session(
    script: list[list[Step]],
    matcher: FakeMatcher | None = None,
    assessor: FakeAssessor | None = None,
    *,
    completeness_check: bool = True,
    max_clarification_rounds: int = MAX_CLARIFICATION_ROUNDS,
    snapshot: dict[str, Any] | None = None,
) -> tuple[DiscoverySession, list[FakeClient]]:
    created: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> Any:
        client = FakeClient(options, script)
        created.append(client)
        return client

    session = DiscoverySession(
        client_factory=factory,
        skills=SKILLS,
        match_fn=matcher or FakeMatcher(),
        assess_fn=assessor or FakeAssessor(),
        completeness_check=completeness_check,
        max_clarification_rounds=max_clarification_rounds,
        snapshot=snapshot,
    )
    return session, created


# --- tests -------------------------------------------------------------------------------


class TestLifecycle:
    async def test_send_before_start_raises(self) -> None:
        session, _ = make_session([])
        with pytest.raises(SessionNotStartedError):
            await session.send("hi")

    async def test_context_manager_connects_and_disconnects(self) -> None:
        session, created = make_session([["hello"]])
        async with session:
            assert created[0].connected
            await session.send("hi")
        assert created[0].disconnected

    async def test_start_and_close_are_idempotent(self) -> None:
        session, created = make_session([])
        await session.start()
        await session.start()
        assert len(created) == 1
        await session.close()
        await session.close()
        assert created[0].disconnected


class TestTurnLoop:
    async def test_plain_text_turn(self) -> None:
        session, created = make_session([["Who will use this?"]])
        async with session:
            result = await session.send("I want to automate onboarding.")

        assert created[0].queries == ["I want to automate onboarding."]
        assert result.turn == 1
        assert result.user_text == "I want to automate onboarding."
        assert result.assistant_text == "Who will use this?"
        assert result.tool_calls == ()
        assert result.captured == ()
        assert result.session_id == "sess-1"
        assert result.cost_usd == pytest.approx(0.01)
        assert result.session_cost_usd == pytest.approx(0.01)
        assert not result.is_error
        assert session.session_id == "sess-1"
        assert session.total_cost_usd == pytest.approx(0.01)

    async def test_tool_call_is_traced_and_attributed_to_the_right_turn(self) -> None:
        script: list[list[Step]] = [
            ["Tell me about the team."],
            [
                {"category": "key_stakeholders", "summary": "HR ops, 4 people"},
                "Got it. What does the process look like today?",
            ],
        ]
        session, _ = make_session(script)
        async with session:
            await session.send("I want to automate onboarding.")
            second = await session.send("It's the HR ops team, four of us.")

        assert second.turn == 2
        assert [t.name for t in second.tool_calls] == [RECORD_TOOL_FULL_NAME]
        assert second.captured == (CanvasCategory.KEY_STAKEHOLDERS,)
        entry = session.state.current(CanvasCategory.KEY_STAKEHOLDERS)
        assert entry is not None
        assert entry.summary == "HR ops, 4 people"
        assert entry.turn == 2  # injected turn number, not build-time 0

    async def test_multiple_captures_in_one_turn_preserve_order(self) -> None:
        script: list[list[Step]] = [
            [
                {"category": "output_format", "summary": "chatbot"},
                {"category": "input_types", "summary": "prompts, PDFs"},
                "Great, and who owns this today?",
            ]
        ]
        session, _ = make_session(script)
        async with session:
            result = await session.send("A chatbot that answers from our PDFs.")
        assert result.captured == (CanvasCategory.OUTPUT_FORMAT, CanvasCategory.INPUT_TYPES)
        assert session.state.missing()[0] is CanvasCategory.KEY_STAKEHOLDERS

    async def test_completion_is_derived_from_state(self) -> None:
        steps: list[Step] = [
            {"category": c.value, "summary": f"{c.value} ok"} for c in CanvasCategory
        ]
        steps.append("Here is a recap...")
        session, _ = make_session([steps])
        async with session:
            assert not session.is_complete
            await session.send("everything at once")
            assert session.is_complete

    async def test_cost_accumulates_across_turns(self) -> None:
        session, _ = make_session([["a"], ["b"], ["c"]])
        async with session:
            for text in ("1", "2", "3"):
                await session.send(text)
        assert session.total_cost_usd == pytest.approx(0.03)
        assert [t.cost_usd for t in session.turns] == pytest.approx([0.01, 0.01, 0.01])
        assert [t.session_cost_usd for t in session.turns] == pytest.approx([0.01, 0.02, 0.03])
        assert len(session.turns) == 3

    async def test_missing_result_message_is_a_protocol_error(self) -> None:
        session, created = make_session([["text only"]])
        async with session:
            created[0].omit_result = True
            with pytest.raises(TurnProtocolError, match="turn 1"):
                await session.send("hi")

    async def test_schema_invalid_tool_call_never_reaches_state(self) -> None:
        # The SDK validates arguments against RECORD_TOOL_SCHEMA before our handler runs.
        script: list[list[Step]] = [
            [{"category": "budget", "summary": "lots"}, "Let me rephrase that."]
        ]
        session, _ = make_session(script)
        async with session:
            result = await session.send("we have a big budget")
        assert len(result.tool_calls) == 1  # the attempt is still in the trace
        assert result.captured == ()
        assert session.state.entries == []


class TestStreaming:
    async def test_text_arrives_as_deltas_then_turn_completed(self) -> None:
        session, _ = make_session([["Who will use this system?"]])
        async with session:
            events = [e async for e in session.stream("hi")]

        deltas = [e for e in events if isinstance(e, TextDelta)]
        assert len(deltas) > 1  # actually chunked, not one blob
        assert "".join(d.text for d in deltas) == "Who will use this system?"
        assert isinstance(events[-1], TurnCompleted)
        assert events[-1].result.assistant_text == "Who will use this system?"
        assert sum(isinstance(e, TurnCompleted) for e in events) == 1

    async def test_tool_call_start_is_surfaced_before_text(self) -> None:
        script: list[list[Step]] = [
            [{"category": "key_stakeholders", "summary": "HR"}, "Got it. Next?"]
        ]
        session, _ = make_session(script)
        async with session:
            events = [e async for e in session.stream("HR team")]

        kinds = [type(e).__name__ for e in events]
        assert kinds[0] == "ToolCallStarted"
        assert isinstance(events[0], ToolCallStarted)
        assert events[0].name == RECORD_TOOL_FULL_NAME
        assert kinds.index("ToolCallStarted") < kinds.index("TextDelta")
        assert isinstance(events[-1], TurnCompleted)
        assert events[-1].result.captured == (CanvasCategory.KEY_STAKEHOLDERS,)

    async def test_send_is_equivalent_to_draining_stream(self) -> None:
        session, _ = make_session([["a b c d e f"], ["a b c d e f"]])
        async with session:
            streamed = [e async for e in session.stream("1")]
            sent = await session.send("2")
        assert isinstance(streamed[-1], TurnCompleted)
        assert streamed[-1].result.assistant_text == sent.assistant_text
        assert sent.turn == 2

    async def test_ignored_stream_events_produce_nothing(self) -> None:
        from blueprint.orchestrator import _translate_stream_event

        assert _translate_stream_event({"type": "message_start", "message": {}}) is None
        assert _translate_stream_event({"type": "content_block_stop", "index": 0}) is None
        assert (
            _translate_stream_event(
                {"type": "content_block_delta", "delta": {"type": "input_json_delta"}}
            )
            is None
        )
        text_start = {"type": "content_block_start", "content_block": {"type": "text"}}
        assert _translate_stream_event(text_start) is None


class TestDepartmentMatching:
    async def test_runs_when_key_activities_is_captured(self) -> None:
        matcher = FakeMatcher(("hr", "finance"))
        script: list[list[Step]] = [
            ["Who is involved?"],
            [{"category": "key_stakeholders", "summary": "HR"}, "What is the process?"],
            [{"category": "key_activities", "summary": "packets"}, "What is the pain?"],
        ]
        session, _ = make_session(script, matcher)
        async with session:
            await session.send("onboarding")
            await session.send("HR team")
            assert matcher.calls == []  # not yet: key_activities not captured
            events = [e async for e in session.stream("we check packets")]

        assert len(matcher.calls) == 1
        assert "you> onboarding" in matcher.calls[0]
        assert "assistant> Who is involved?" in matcher.calls[0]
        assert session.match is not None
        assert session.match.departments == ("hr", "finance")
        kinds = [type(e).__name__ for e in events]
        assert kinds.index("DepartmentsMatched") == kinds.index("TurnCompleted") - 1
        assert isinstance(events[-2], DepartmentsMatched)

    async def test_falls_back_to_turn_three_if_activities_not_captured(self) -> None:
        matcher = FakeMatcher()
        session, _ = make_session([["a"], ["b"], ["c"], ["d"]], matcher)
        async with session:
            for i in range(MATCH_FALLBACK_TURN - 1):
                await session.send(str(i))
                assert matcher.calls == []
            await session.send("fallback turn")
            assert len(matcher.calls) == 1
            await session.send("one more")
            assert len(matcher.calls) == 1  # runs once, not every turn

    async def test_matched_skills_reflects_match_in_order(self) -> None:
        session, _ = make_session([["a"], ["b"], ["c"]], FakeMatcher(("sales", "finance")))
        async with session:
            assert session.matched_skills() == []
            for i in range(MATCH_FALLBACK_TURN):
                await session.send(str(i))
        assert [s.name for s in session.matched_skills()] == ["sales", "finance"]

    async def test_failure_is_logged_retried_then_abandoned(self) -> None:
        matcher = FakeMatcher(fail_times=MAX_MATCH_ATTEMPTS + 1)
        session, _ = make_session([["a"]] * 6, matcher)
        async with session:
            for i in range(6):
                result = await session.send(str(i))
                assert not result.is_error  # discovery is unaffected
        assert len(matcher.calls) == MAX_MATCH_ATTEMPTS
        assert session.match is None

    async def test_examples_tool_sees_matched_skills(self) -> None:
        script: list[list[Step]] = [
            [{"category": "system_integrations"}, "example please?"],  # before match
            ["b"],
            [{"category": "key_activities", "summary": "x"}, "c"],  # triggers match
            [{"category": "system_integrations"}, "here are examples"],  # after match
        ]
        session, created = make_session(script, FakeMatcher(("customer_success",)))
        async with session:
            for text in ("1", "2", "3", "4"):
                await session.send(text)
        before, after = created[0].tool_results[0], created[0].tool_results[-1]
        assert "No department profile" in before
        assert "## customer_success" in after and "Zendesk" in after
        assert "## hr" not in after

    async def test_snapshot_includes_match(self) -> None:
        session, _ = make_session([["a"], ["b"], ["c"]], FakeMatcher(("hr",)))
        async with session:
            for i in range(MATCH_FALLBACK_TURN):
                await session.send(str(i))
        assert session.to_dict()["match"]["departments"] == ["hr"]


class TestInputValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
    def test_blank_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="empty"):
            validate_user_text(bad)

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="limit is"):
            validate_user_text("x" * (MAX_USER_TEXT_CHARS + 1))

    def test_strips_and_passes_through(self) -> None:
        assert validate_user_text("  hello  ") == "hello"

    async def test_session_rejects_blank_before_spending(self) -> None:
        session, created = make_session([["never used"]])
        async with session:
            with pytest.raises(ValueError):
                await session.send("   ")
            assert created[0].queries == []
            assert session.turn == 0  # no turn consumed


class TestFailureContainment:
    async def test_result_error_becomes_error_turn_and_kills_session(self) -> None:
        session, created = make_session([["Partial text"], ["never reached"]])
        async with session:
            created[0].raise_result_error = ResultError(
                "Claude Code returned an error result: budget exceeded",
                data={
                    "subtype": "error_max_budget",
                    "result": "budget exceeded",
                    "terminal_reason": "max_budget",
                },
            )
            result = await session.send("hi")

            assert result.is_error
            assert result.errors == ("max_budget: budget exceeded",)
            assert result.assistant_text == "Partial text"  # what arrived is kept
            assert session.failure is not None and "budget" in session.failure
            with pytest.raises(SessionNotStartedError, match="budget"):
                await session.send("again")
        assert created[0].disconnected
        assert session.to_dict()["failure"] == session.failure

    async def test_idle_timeout_becomes_error_turn(self) -> None:
        session, created = make_session([["x"]])
        session.idle_timeout_s = 0.05
        async with session:
            created[0].hang = True
            result = await session.send("hi")
        assert result.is_error
        assert "no response from the model" in result.errors[0]
        assert session.failure is not None

    async def test_rate_limit_event_is_logged_not_fatal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session, created = make_session([["fine"]])
        with caplog.at_level("WARNING", logger="blueprint.orchestrator"):
            async with session:
                created[0].emit_rate_limit = True
                result = await session.send("hi")
        assert not result.is_error and result.assistant_text == "fine"
        warn = [r for r in caplog.records if r.getMessage() == "turn.rate_limit"]
        assert len(warn) == 1
        assert getattr(warn[0], "status", None) == "allowed_warning"

    async def test_close_survives_disconnect_failure(self) -> None:
        session, created = make_session([])
        await session.start()

        async def boom() -> None:
            raise ProcessError("gone")

        created[0].disconnect = boom  # type: ignore[method-assign]
        await session.close()  # must not raise


def ready(session: DiscoverySession) -> bool:
    # A function call so mypy does not narrow the property across statements.
    return session.is_ready_for_review


class TestCompletenessLoop:
    async def test_sufficient_capture_says_so(self) -> None:
        script: list[list[Step]] = [
            [{"category": "key_stakeholders", "summary": "HR ops, hiring managers wait"}, "ok"]
        ]
        session, created = make_session(script)
        async with session:
            result = await session.send("x")
        assert "Sufficient." in created[0].tool_results[0]
        assert len(result.assessments) == 1 and result.assessments[0].sufficient
        assert session.completeness.rounds == {}

    async def test_gap_becomes_a_follow_up_in_the_tool_result(self) -> None:
        assessor = FakeAssessor({"key_activities": [("headcount", "frequency"), ()]})
        script: list[list[Step]] = [
            [{"category": "key_activities", "summary": "they check packets"}, "How often?"],
            [{"category": "key_activities", "summary": "3 people, daily"}, "Thanks."],
        ]
        session, created = make_session(script, assessor=assessor)
        async with session:
            first = await session.send("we check packets")
            second = await session.send("three of us, every day")

        text = created[0].tool_results[0]
        assert "Incomplete: missing headcount, frequency" in text
        assert "ask something like" in text and "Could you say more about" in text
        assert first.assessments[0].missing == ("headcount", "frequency")
        assert "Sufficient." in created[0].tool_results[1]
        assert second.assessments[0].sufficient
        assert session.completeness.rounds == {CanvasCategory.KEY_ACTIVITIES: 1}
        assert assessor.calls == [("key_activities", 1), ("key_activities", 2)]

    async def test_clarification_rounds_are_capped(self) -> None:
        always_missing: list[tuple[str, ...]] = [("frequency",)] * 5
        assessor = FakeAssessor({"key_activities": always_missing})
        steps: list[list[Step]] = [
            [{"category": "key_activities", "summary": f"v{i}"}, "?"] for i in range(1, 5)
        ]
        session, created = make_session(script=steps, assessor=assessor, max_clarification_rounds=2)
        async with session:
            for i in range(4):
                await session.send(str(i))
        results = created[0].tool_results
        assert "Before moving on, ask" in results[0]
        assert "Before moving on, ask" in results[1]
        assert "do not ask again" in results[2]  # cap reached: gaps go to the reviewer
        assert "do not ask again" in results[3]
        assert session.completeness.rounds == {CanvasCategory.KEY_ACTIVITIES: 2}

    async def test_ready_for_review_requires_sufficiency_not_just_completeness(self) -> None:
        assessor = FakeAssessor({"value_proposition": [("measurable_impact",), ()]})
        all_seven: list[Step] = [
            {"category": c.value, "summary": f"{c.value} ok"} for c in CanvasCategory
        ]
        script: list[list[Step]] = [
            [*all_seven, "recap"],
            [{"category": "value_proposition", "summary": "saves 10 hours a week"}, "thanks"],
        ]
        session, _ = make_session(script, assessor=assessor)
        async with session:
            await session.send("everything at once")
            assert session.is_complete
            assert not ready(session)
            assert session.completeness.gaps(session.state) == {
                CanvasCategory.VALUE_PROPOSITION: ("measurable_impact",)
            }
            await session.send("about ten hours a week")
            assert ready(session)
        snap = session.to_dict()
        assert snap["is_ready_for_review"] is True
        assert snap["open_gaps"] == {}
        assert len(snap["completeness"]["assessments"]) == 8

    async def test_capped_gaps_still_count_as_ready(self) -> None:
        assessor = FakeAssessor({"output_format": [("formats_selected",)] * 4})
        all_seven: list[Step] = [
            {"category": c.value, "summary": f"{c.value} ok"} for c in CanvasCategory
        ]
        script: list[list[Step]] = [
            [*all_seven, "recap"],
            [{"category": "output_format", "summary": "still vague"}, "ok"],
            [{"category": "output_format", "summary": "still vague"}, "ok"],
        ]
        session, _ = make_session(script, assessor=assessor, max_clarification_rounds=2)
        async with session:
            await session.send("1")
            assert not ready(session)
            await session.send("2")
            await session.send("3")
            # two follow-ups asked; a third gap is accepted and carried to the reviewer
            assert ready(session)
        assert session.to_dict()["open_gaps"] == {"output_format": ["formats_selected"]}

    async def test_checker_failure_never_blocks_a_capture(self) -> None:
        class Exploding(FakeAssessor):
            async def __call__(self, *a: Any, **k: Any) -> Assessment:
                raise AssessmentError("checker down")

        script: list[list[Step]] = [[{"category": "input_source", "summary": "SharePoint"}, "ok"]]
        session, created = make_session(script, assessor=Exploding())
        async with session:
            result = await session.send("x")
        assert session.state.current(CanvasCategory.INPUT_SOURCE) is not None
        assert result.assessments == ()
        assert "Recorded input_source (v1)." in created[0].tool_results[0]
        assert "Sufficient" not in created[0].tool_results[0]

    async def test_disabled_check_means_ready_equals_complete(self) -> None:
        all_seven: list[Step] = [
            {"category": c.value, "summary": f"{c.value} ok"} for c in CanvasCategory
        ]
        assessor = FakeAssessor()
        session, created = make_session(
            [[*all_seven, "recap"]], assessor=assessor, completeness_check=False
        )
        async with session:
            await session.send("all")
        assert assessor.calls == []
        assert session.is_ready_for_review
        assert session.to_dict()["completeness"] is None
        assert "Sufficient" not in created[0].tool_results[0]


class TestRestoreAndReview:
    async def _completed_session(self) -> tuple[DiscoverySession, list[FakeClient]]:
        assessor = FakeAssessor({"value_proposition": [("measurable_impact",)] * 3})
        all_seven: list[Step] = [
            {"category": c.value, "summary": f"{c.value} ok"} for c in CanvasCategory
        ]
        session, created = make_session(
            [[*all_seven, "recap"], ["ok"], ["ok"]],
            FakeMatcher(("hr", "finance")),
            assessor,
            max_clarification_rounds=1,
        )
        async with session:
            await session.send("everything at once")
            await session.send("one")
            await session.send("two")
        return session, created

    async def test_restore_rebuilds_every_log_and_sets_resume(self) -> None:
        original, _ = await self._completed_session()
        original.record_review(
            ReviewDecision(ReviewAction.SEND_BACK, "goutam", "Which HRIS?", 1, RiskLevel.ELEVATED)
        )
        snap = original.to_dict()
        assert snap["review_status"] == "sent_back" and snap["resumed"] is False

        def factory(opts: ClaudeAgentOptions) -> Any:
            return FakeClient(opts, [["Which HRIS do you use?"]])

        restored = DiscoverySession(
            client_factory=factory,
            skills=SKILLS,
            match_fn=FakeMatcher(),
            assess_fn=FakeAssessor(),
            snapshot=snap,
        )
        assert restored.resumed and restored.options.resume == "sess-1"
        assert restored.session_id == "sess-1" and restored.turn == 3
        assert restored.total_cost_usd == pytest.approx(0.03)
        assert restored.state.to_dict()["current"] == snap["canvas"]["current"]
        assert len(restored.completeness.assessments) == len(snap["completeness"]["assessments"])
        assert restored.completeness.rounds == {CanvasCategory.VALUE_PROPOSITION: 1}
        assert restored.match is not None and restored.match.departments == ("hr", "finance")
        assert [s.name for s in restored.matched_skills()] == ["hr", "finance"]
        assert restored.review_status == "sent_back"
        assert restored.reviews[0].note == "Which HRIS?"
        # Derived views survive too.
        assert restored.is_complete
        assert restored.to_dict()["open_gaps"] == snap["open_gaps"]

    async def test_cost_keeps_rising_across_a_resume(self) -> None:
        # The SDK's running total restarts at zero in the new subprocess; the restored session
        # must carry the earlier spend as an offset, never report a negative turn.
        original, _ = await self._completed_session()
        assert original.total_cost_usd == pytest.approx(0.03)
        snap = original.to_dict()

        def factory(opts: ClaudeAgentOptions) -> Any:
            return FakeClient(opts, [["ok"], ["ok"]])  # fresh fake: running total starts at 0

        restored = DiscoverySession(
            client_factory=factory,
            skills=SKILLS,
            match_fn=FakeMatcher(),
            assess_fn=FakeAssessor(),
            snapshot=snap,
        )
        async with restored:
            first = await restored.send("a")
            second = await restored.send("b")
        assert first.cost_usd == pytest.approx(0.01)
        assert first.session_cost_usd == pytest.approx(0.04)
        assert second.cost_usd == pytest.approx(0.01)
        assert restored.total_cost_usd == pytest.approx(0.05)

    async def test_reviewer_note_is_framed_for_the_model_and_raw_in_the_trace(self) -> None:
        session, created = make_session([["Which HRIS do you use?"]])
        async with session:
            result = await session.send_reviewer_note("The spec lacks the HRIS name.")

        sent = created[0].queries[0]
        assert sent.startswith("REVIEWER NOTE (from the internal reviewer")
        assert "The spec lacks the HRIS name." in sent
        assert "Do not mention a reviewer" in sent
        assert result.origin == "reviewer"
        assert result.user_text == "The spec lacks the HRIS name."  # raw, not framed
        assert result.turn == 1
        assert "reviewer> The spec lacks the HRIS name." in session.render_transcript()
        assert session.to_dict()["turns"][0]["origin"] == "reviewer"

    async def test_stakeholder_turns_have_stakeholder_origin(self) -> None:
        session, _ = make_session([["hi"]])
        async with session:
            result = await session.send("hello")
        assert result.origin == "stakeholder"
        assert "you> hello" in session.render_transcript()

    async def test_review_log_is_append_only_and_in_snapshot(self) -> None:
        session, _ = make_session([])
        session.record_review(ReviewDecision(ReviewAction.REJECT, "goutam", "no", 1, RiskLevel.LOW))
        session.record_review(ReviewDecision(ReviewAction.APPROVE, "goutam", "", 2, RiskLevel.LOW))
        snap = session.to_dict()
        assert [d["action"] for d in snap["reviews"]] == ["reject", "approve"]
        assert snap["review_status"] == "approved"


class TestSnapshot:
    async def test_web_search_guard_in_snapshot_when_enabled(self) -> None:
        class NoopSearch:
            async def search(self, query: str) -> list[Any]:
                return []

        created: list[FakeClient] = []

        def factory(options: ClaudeAgentOptions) -> Any:
            client = FakeClient(options, [["hi"]])
            created.append(client)
            return client

        session = DiscoverySession(
            client_factory=factory,
            skills=SKILLS,
            match_fn=FakeMatcher(),
            web_search=NoopSearch(),
            max_web_searches=2,
        )
        async with session:
            await session.send("x")
        assert session.to_dict()["web_search"] == {"max_calls": 2, "attempts": []}
        assert session.web_search_guard is not None
        assert "web_search" in str(created[0].options.system_prompt)

    async def test_to_dict_contains_canvas_and_turns(self) -> None:
        script: list[list[Step]] = [
            [{"category": "key_stakeholders", "summary": "HR"}, "Next question?"]
        ]
        session, _ = make_session(script)
        async with session:
            await session.send("HR team")
        snap = session.to_dict()
        assert snap["session_id"] == "sess-1"
        assert snap["is_complete"] is False
        assert snap["match"] is None
        assert snap["web_search"] is None  # off by default
        assert snap["canvas"]["current"]["key_stakeholders"] == "HR"
        assert snap["turns"][0]["captured"] == ["key_stakeholders"]
        assert snap["turns"][0]["tool_calls"][0]["name"] == RECORD_TOOL_FULL_NAME


class TestSessionIdentity:
    """A caller-supplied id is the key the API and the run store use, so it must not move."""

    async def test_an_unpinned_session_adopts_the_id_the_sdk_reports(self) -> None:
        session, _ = make_session([["Hello."]])
        async with session:
            await session.send("hi")
        assert session.session_id == "sess-1"

    async def test_a_pinned_id_survives_a_differing_result_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        created: list[FakeClient] = []

        def factory(options: ClaudeAgentOptions) -> Any:
            client = FakeClient(options, [["Hello."]])
            created.append(client)
            return client

        pinned = "11111111-2222-3333-4444-555555555555"
        session = DiscoverySession(
            client_factory=factory,
            skills=SKILLS,
            match_fn=FakeMatcher(),
            assess_fn=FakeAssessor(),
            session_id=pinned,
        )
        async with session:
            await session.send("hi")

        assert created[0].options.session_id == pinned  # the SDK was told which id to use
        assert session.session_id == pinned
        assert session.to_dict()["session_id"] == pinned
        assert "session.id_mismatch" in caplog.text


class TestSnapshotIsAnAuditTrail:
    """A resumed session must not overwrite the record of what happened before it."""

    async def test_restoring_then_saving_keeps_the_earlier_turns(self) -> None:
        first, _ = make_session([[{"category": "key_stakeholders", "summary": "HR ops"}, "Go on?"]])
        async with first:
            await first.send("we handle onboarding")
        snapshot = first.to_dict()
        assert len(snapshot["turns"]) == 1

        second, _ = make_session([["And which system is that?"]], snapshot=snapshot)
        async with second:
            await second.send("BambooHR")
        after = second.to_dict()

        assert after["turn"] == 2
        assert len(after["turns"]) == 2, "the restored turn was dropped from the snapshot"
        assert after["turns"][0]["user_text"] == "we handle onboarding"
        assert after["turns"][1]["user_text"] == "BambooHR"

    async def test_prior_turns_are_carried_verbatim(self) -> None:
        first, _ = make_session([[{"category": "output_format", "summary": "chatbot"}, "Noted."]])
        async with first:
            await first.send("a chatbot please")
        original = first.to_dict()["turns"][0]

        second, _ = make_session([["Anything else?"]], snapshot=first.to_dict())
        async with second:
            await second.send("no")
        assert second.to_dict()["turns"][0] == original

    async def test_a_restore_chain_does_not_lose_the_middle(self) -> None:
        session, _ = make_session([["one"]])
        async with session:
            await session.send("first")
        snapshot = session.to_dict()

        for text, reply in [("second", "two"), ("third", "three")]:
            nxt, _ = make_session([[reply]], snapshot=snapshot)
            async with nxt:
                await nxt.send(text)
            snapshot = nxt.to_dict()

        assert [t["user_text"] for t in snapshot["turns"]] == ["first", "second", "third"]
