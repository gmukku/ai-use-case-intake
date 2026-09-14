"""The orchestrator: owns one discovery conversation end to end.

Option A architecture: the discovery conversation *is* the main SDK session, and this class is
the Python around it. It owns the canvas state and the turn counter, drives the session one
stakeholder message at a time, and returns a typed, serializable ``TurnResult`` per turn so
the audit layer and the UIs never have to parse SDK message objects themselves.

Later steps plug in here without changing the model-facing side:
    - completeness check / clarification loop (after each ``send``),
    - HITL gate (once ``is_complete``),
    - audit logging (consume ``TurnResult.to_dict()``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from blueprint.canvas import CanvasCategory, CanvasState
from blueprint.discovery import DEFAULT_MODEL, build_discovery_options

logger = logging.getLogger(__name__)

# Anything that quacks like ClaudeSDKClient: connect / query / receive_response / disconnect.
# Kept as a factory so tests can substitute a scripted fake.
ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


class SessionNotStartedError(RuntimeError):
    """Raised when ``send`` is called before ``start`` (or after ``close``)."""


class TurnProtocolError(RuntimeError):
    """Raised when the SDK stream ends without a ``ResultMessage`` for the turn."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation the model made during a turn (for the trace)."""

    id: str
    name: str
    input: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Everything that happened in response to one stakeholder message."""

    turn: int
    user_text: str
    assistant_text: str
    tool_calls: tuple[ToolCall, ...]
    captured: tuple[CanvasCategory, ...]
    """Categories recorded during this turn, in the order they were recorded."""
    session_id: str
    cost_usd: float | None
    """Cost of this turn alone (delta of the session's running total)."""
    session_cost_usd: float | None
    """Session running total as of the end of this turn, as reported by the SDK."""
    num_agentic_turns: int
    duration_ms: int
    is_error: bool
    errors: tuple[str, ...]
    started_at: datetime
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for persistence and the audit trail."""
        return {
            "turn": self.turn,
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "captured": [c.value for c in self.captured],
            "session_id": self.session_id,
            "cost_usd": self.cost_usd,
            "session_cost_usd": self.session_cost_usd,
            "num_agentic_turns": self.num_agentic_turns,
            "duration_ms": self.duration_ms,
            "is_error": self.is_error,
            "errors": list(self.errors),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }


# -- streaming events -----------------------------------------------------------------------
#
# ``stream()`` yields these as the turn progresses so a UI can render text as it is generated
# instead of waiting ~5-9 s for the whole turn. They are *our* types, not raw SDK events, so
# FastAPI and the frontend never depend on the SDK's wire format.


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A few characters of assistant text, as generated."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """The model began a tool call (e.g. recording a canvas answer).

    UIs may show a subtle activity indicator; the requester view must never name the tool.
    """

    name: str


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Terminal event for a turn; always the last one yielded."""

    result: TurnResult


TurnEvent = TextDelta | ToolCallStarted | TurnCompleted


class DiscoverySession:
    """One stakeholder's discovery conversation, from first message to a complete canvas.

    Usage::

        async with DiscoverySession(max_budget_usd=3.0) as session:
            result = await session.send("I want to automate onboarding paperwork.")
            ...
            if session.is_complete:
                spec = session.state.to_dict()
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_budget_usd: float | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self.state = CanvasState()
        self.turn = 0
        self.session_id: str | None = None
        self.total_cost_usd = 0.0
        self.turns: list[TurnResult] = []
        # The tool reads the turn number through this lambda at call time, so a capture is
        # always attributed to the stakeholder message that triggered it.
        self.options = build_discovery_options(
            self.state, lambda: self.turn, model=model, max_budget_usd=max_budget_usd
        )
        self._client_factory = client_factory
        self._client: ClaudeSDKClient | None = None

    # ------------------------------- lifecycle ----------------------------------

    async def start(self) -> None:
        """Spawn the SDK session. Explicit (not only ``async with``) so a web layer can own it."""
        if self._client is not None:
            return
        client = self._client_factory(self.options)
        await client.connect()
        self._client = client
        logger.info("session.start", extra={"model": self.options.model})

    async def close(self) -> None:
        """Tear down the SDK session. Safe to call twice."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        finally:
            self._client = None
            logger.info(
                "session.close",
                extra={
                    "session_id": self.session_id,
                    "turns": self.turn,
                    "total_cost_usd": round(self.total_cost_usd, 6),
                    "is_complete": self.is_complete,
                },
            )

    async def __aenter__(self) -> Self:
        """Enter: start the session."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit: always close, even on error."""
        await self.close()

    # -- the turn loop -------------------------------------------------------------------

    async def stream(self, user_text: str) -> AsyncIterator[TurnEvent]:
        """Deliver one stakeholder message and yield events as the model responds.

        Yields ``TextDelta`` / ``ToolCallStarted`` while the turn is in progress and exactly
        one ``TurnCompleted`` at the end. ``send`` is the non-streaming convenience wrapper.

        Raises:
            SessionNotStartedError: if called outside ``start``/``close``.
            TurnProtocolError: if the SDK stream ends without a ``ResultMessage``.
        """
        if self._client is None:
            raise SessionNotStartedError("call start() (or use 'async with') before send()")

        # Increment BEFORE query() so tool calls made during this turn see the new number.
        self.turn += 1
        turn = self.turn
        started_at = datetime.now(UTC)
        entries_before = len(self.state.entries)
        logger.info("turn.start", extra={"turn": turn, "chars": len(user_text)})

        await self._client.query(user_text)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        result: ResultMessage | None = None

        async for message in self._client.receive_response():
            if isinstance(message, StreamEvent):
                event = _translate_stream_event(message.event)
                if event is not None:
                    yield event
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(
                            ToolCall(id=block.id, name=block.name, input=dict(block.input))
                        )
            elif isinstance(message, ResultMessage):
                result = message

        if result is None:
            raise TurnProtocolError(f"turn {turn}: stream ended without a ResultMessage")

        self.session_id = result.session_id
        # The SDK reports a RUNNING TOTAL per ResultMessage, not a per-turn cost.
        turn_cost: float | None = None
        if result.total_cost_usd is not None:
            turn_cost = result.total_cost_usd - self.total_cost_usd
            self.total_cost_usd = result.total_cost_usd

        captured = tuple(e.category for e in self.state.entries[entries_before:])
        turn_result = TurnResult(
            turn=turn,
            user_text=user_text,
            assistant_text="\n".join(p for p in text_parts if p.strip()).strip(),
            tool_calls=tuple(tool_calls),
            captured=captured,
            session_id=result.session_id,
            cost_usd=turn_cost,
            session_cost_usd=result.total_cost_usd,
            num_agentic_turns=result.num_turns,
            duration_ms=result.duration_ms,
            is_error=result.is_error,
            errors=tuple(result.errors or ()),
            started_at=started_at,
        )
        self.turns.append(turn_result)

        logger.log(
            logging.ERROR if result.is_error else logging.INFO,
            "turn.end",
            extra={
                "turn": turn,
                "session_id": result.session_id,
                "captured": [c.value for c in captured],
                "tool_calls": len(tool_calls),
                "cost_usd": turn_cost,
                "session_cost_usd": result.total_cost_usd,
                "is_error": result.is_error,
                "missing": [c.value for c in self.state.missing()],
            },
        )
        yield TurnCompleted(turn_result)

    async def send(self, user_text: str) -> TurnResult:
        """Non-streaming turn: drain ``stream`` and return the final ``TurnResult``."""
        async for event in self.stream(user_text):
            if isinstance(event, TurnCompleted):
                return event.result
        raise TurnProtocolError("stream() ended without a TurnCompleted event")  # unreachable

    # -- derived state -------------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """True once every canvas category has been recorded at least once."""
        return self.state.is_complete

    def to_dict(self) -> dict[str, Any]:
        """Full serializable snapshot: canvas + every turn. This is the spec plus its trace."""
        return {
            "session_id": self.session_id,
            "model": self.options.model,
            "turn": self.turn,
            "total_cost_usd": self.total_cost_usd,
            "is_complete": self.is_complete,
            "canvas": self.state.to_dict(),
            "turns": [t.to_dict() for t in self.turns],
        }


def _translate_stream_event(event: dict[str, Any]) -> TurnEvent | None:
    """Map a raw Anthropic API stream event to one of ours; ``None`` for events we ignore.

    We only surface text deltas and tool-call starts. ``message_start``/``message_delta``/
    ``content_block_stop`` carry nothing a UI needs mid-turn.
    """
    kind = event.get("type")
    if kind == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta" and delta.get("text"):
            return TextDelta(text=str(delta["text"]))
    elif kind == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "tool_use":
            return ToolCallStarted(name=str(block.get("name", "")))
    return None
