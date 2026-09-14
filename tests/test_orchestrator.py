from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from blueprint.canvas import CanvasCategory
from blueprint.discovery import RECORD_TOOL_FULL_NAME
from blueprint.orchestrator import (
    DiscoverySession,
    SessionNotStartedError,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
    TurnProtocolError,
)

# --- a scripted stand-in for ClaudeSDKClient --------------------------------------------
#
# Each "script" entry is a list of steps for one turn. A step is either a str (assistant text)
# or a dict (arguments for the record tool). The fake emits SDK message objects the way the
# real client would, and *actually calls our tool handler* for dict steps, so the orchestrator's
# turn-number injection and `captured` bookkeeping are exercised for real.

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

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
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
                yield self._stream_event(
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": f"tu-{i}",
                            "name": RECORD_TOOL_FULL_NAME,
                            "input": {},
                        },
                    }
                )
                yield AssistantMessage(
                    content=[ToolUseBlock(id=f"tu-{i}", name=RECORD_TOOL_FULL_NAME, input=step)],
                    model="fake",
                )
                await self._call_record_tool(step)
        if not self.omit_result:
            self.running_total += self.turn_cost  # the SDK reports a running total
            yield result_message(cost=self.running_total)

    @staticmethod
    def _stream_event(event: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid="ev", session_id="sess-1", event=event)

    async def _call_record_tool(self, args: dict[str, Any]) -> None:
        """Dispatch through the real in-process MCP server, so the SDK's schema validation runs."""
        from mcp.types import CallToolRequestParams

        server: Any = self.options.mcp_servers
        entry = server["canvas"]["instance"].get_request_handler("tools/call")
        params = CallToolRequestParams(name="record_canvas_answer", arguments=args)
        await entry.handler(None, params)


def make_session(script: list[list[Step]]) -> tuple[DiscoverySession, list[FakeClient]]:
    created: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> Any:
        client = FakeClient(options, script)
        created.append(client)
        return client

    return DiscoverySession(client_factory=factory), created


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
        assert (
            _translate_stream_event(
                {"type": "content_block_start", "content_block": {"type": "text"}}
            )
            is None
        )


class TestSnapshot:
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
        assert snap["canvas"]["current"]["key_stakeholders"] == "HR"
        assert snap["turns"][0]["captured"] == ["key_stakeholders"]
        assert snap["turns"][0]["tool_calls"][0]["name"] == RECORD_TOOL_FULL_NAME
