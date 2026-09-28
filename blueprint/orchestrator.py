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

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final, Self

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from blueprint.canvas import CanvasCategory, CanvasEntry, CanvasState
from blueprint.completeness import (
    DEFAULT_CHECKER_MODEL,
    Assessment,
    AssessmentError,
    assess_capture,
)
from blueprint.discovery import DEFAULT_MODEL, build_discovery_options
from blueprint.matching import DepartmentMatch, MatchError, match_departments
from blueprint.review import ReviewDecision, review_status
from blueprint.skills import SKILLS_DIR, DepartmentSkill, load_skills
from blueprint.websearch import DEFAULT_MAX_SEARCHES_PER_SESSION, SearchClient, WebSearchGuard

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
    origin: str
    """``"stakeholder"`` or ``"reviewer"`` (a send-back note delivered to the model)."""
    assistant_text: str
    tool_calls: tuple[ToolCall, ...]
    captured: tuple[CanvasCategory, ...]
    """Categories recorded during this turn, in the order they were recorded."""
    assessments: tuple[Assessment, ...]
    """Completeness verdicts produced during this turn (one per capture when enabled)."""
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
            "origin": self.origin,
            "assistant_text": self.assistant_text,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "captured": [c.value for c in self.captured],
            "assessments": [a.to_dict() for a in self.assessments],
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
class DepartmentsMatched:
    """The classification pass ran during this turn. Admin/reviewer views only."""

    match: DepartmentMatch


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """Terminal event for a turn; always the last one yielded."""

    result: TurnResult


TurnEvent = TextDelta | ToolCallStarted | DepartmentsMatched | TurnCompleted

# Anything with match_departments()'s signature; injectable for tests.
MatchFn = Callable[..., Awaitable[DepartmentMatch]]

# Run the classifier no later than this turn even if key_activities is not yet captured.
MATCH_FALLBACK_TURN = 3
# Give up on classification after this many failed attempts; discovery continues without it.
MAX_MATCH_ATTEMPTS = 2

# Anything with the signature of assess_capture; injectable for tests.
AssessFn = Callable[..., Awaitable[Assessment]]

# After this many clarifying follow-ups on one category, accept it as-is and leave the
# remaining gaps for the reviewer. This is the "do not interrogate" rule enforced in code.
MAX_CLARIFICATION_ROUNDS: Final = 2


@dataclass(slots=True)
class CompletenessTracker:
    """Append-only log of completeness verdicts plus the per-category clarification budget."""

    assessments: list[Assessment] = field(default_factory=list)
    rounds: dict[CanvasCategory, int] = field(default_factory=dict)
    """How many clarifying follow-ups have been requested per category."""

    def latest(self, category: CanvasCategory) -> Assessment | None:
        """The most recent verdict for ``category``, if any."""
        for a in reversed(self.assessments):
            if a.category is category:
                return a
        return None

    def record(self, assessment: Assessment, *, max_rounds: int) -> bool:
        """Append a verdict; return whether the model should ask the follow-up now."""
        self.assessments.append(assessment)
        if assessment.sufficient:
            return False
        used = self.rounds.get(assessment.category, 0)
        if used >= max_rounds:
            return False
        self.rounds[assessment.category] = used + 1
        return True

    def gaps(self, state: CanvasState) -> dict[CanvasCategory, tuple[str, ...]]:
        """Categories whose latest capture still has missing rubric elements."""
        out: dict[CanvasCategory, tuple[str, ...]] = {}
        for category in state.covered():
            latest = self.latest(category)
            if latest is not None and not latest.sufficient:
                out[category] = latest.missing
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "assessments": [a.to_dict() for a in self.assessments],
            "rounds": {c.value: n for c, n in self.rounds.items()},
        }


# Stakeholder message bounds: blank messages are rejected, long ones capped before they are
# sent (and billed). 8k chars is far beyond anything typed in a chat and well under context.
MAX_USER_TEXT_CHARS: Final = 8_000
# Seconds without any message from the SDK before a turn is declared dead.
DEFAULT_IDLE_TIMEOUT_S: Final = 90.0


def validate_user_text(text: str) -> str:
    """Normalize and bound a stakeholder message.

    Raises:
        ValueError: if the message is blank or exceeds ``MAX_USER_TEXT_CHARS``.
    """
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("message is empty")
    if len(cleaned) > MAX_USER_TEXT_CHARS:
        raise ValueError(f"message is {len(cleaned)} chars; limit is {MAX_USER_TEXT_CHARS}")
    return cleaned


def frame_reviewer_note(note: str) -> str:
    """Wrap a reviewer's send-back note as an instruction the model acts on, not a user turn."""
    return (
        "REVIEWER NOTE (from the internal reviewer, not from the stakeholder; the stakeholder "
        f"cannot see this message): {note.strip()}\n\n"
        "In your next message, ask the stakeholder about this in your own words. Do not mention "
        "a reviewer or that anything was sent back. Once they answer, re-record the affected "
        "category with the new detail."
    )


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
        classifier_model: str = DEFAULT_MODEL,
        max_budget_usd: float | None = None,
        skills: dict[str, DepartmentSkill] | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
        match_fn: MatchFn = match_departments,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        web_search: SearchClient | None = None,
        max_web_searches: int = DEFAULT_MAX_SEARCHES_PER_SESSION,
        sop_grounding: bool = True,
        completeness_check: bool = True,
        checker_model: str = DEFAULT_CHECKER_MODEL,
        assess_fn: AssessFn = assess_capture,
        max_clarification_rounds: int = MAX_CLARIFICATION_ROUNDS,
        snapshot: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Create a fresh session, or restore one from a ``to_dict`` snapshot.

        With ``snapshot``, every log is rebuilt (canvas, verdicts, match, search budget,
        reviews, turn count, cost) *before* the tools are built, and the SDK options carry
        ``resume=<session_id>`` so the model reconnects to its own transcript. Restoration
        must precede tool construction because the record tool closes over ``self.state``.
        """
        self.state = CanvasState()
        self.turn = 0
        self.session_id: str | None = None
        self.total_cost_usd = 0.0
        self._cost_offset = 0.0
        """Cost spent in previous subprocesses of this session (set on restore)."""
        self.turns: list[TurnResult] = []
        self.idle_timeout_s = idle_timeout_s
        self.failure: str | None = None
        """Why the session died, if it did (budget, API error, timeout). ``None`` while healthy."""
        self.skills = skills if skills is not None else load_skills(SKILLS_DIR)
        self.match: DepartmentMatch | None = None
        self._classifier_model = classifier_model
        self._match_fn = match_fn
        self._match_attempts = 0
        self.completeness = CompletenessTracker()
        self._completeness_check = completeness_check
        self._checker_model = checker_model
        self._assess_fn = assess_fn
        self._max_clarification_rounds = max_clarification_rounds
        self.reviews: list[ReviewDecision] = []
        """Append-only log of human decisions on this session's spec."""
        # The guard is owned here (not inside discovery.py) so its attempts are in the snapshot.
        self.web_search_guard = (
            WebSearchGuard(max_calls=max_web_searches) if web_search is not None else None
        )
        self.resumed: bool = False

        if snapshot is not None:
            self._restore(snapshot)

        # The tools read turn number and matched skills through these lambdas at call time,
        # so a capture is attributed to the right turn and examples reflect the latest match.
        self.options = build_discovery_options(
            self.state,
            lambda: self.turn,
            self.matched_skills,
            model=model,
            max_budget_usd=max_budget_usd,
            web_search=web_search,
            web_search_guard=self.web_search_guard,
            sop_grounding=sop_grounding,
            assess=self._assess if completeness_check else None,
        )
        resume_id = str(snapshot.get("session_id") or "") if snapshot is not None else ""
        # A pinned id is the caller's, and stays authoritative: it is the registry key and the
        # snapshot filename, so letting a ResultMessage rename the session mid-run would strand
        # the stored run under its old name. Divergence is logged, never silently adopted.
        self._pinned_session_id: str | None = resume_id or session_id or None
        if resume_id:
            self.options = replace(self.options, resume=resume_id)
        elif session_id:
            # A caller-chosen UUID becomes the SDK's session id too, so the API, the SDK
            # transcript, and the snapshot file share one identifier from the first turn.
            self.session_id = session_id
            self.options = replace(self.options, session_id=session_id)
        self._client_factory = client_factory
        self._client: ClaudeSDKClient | None = None

    def _restore(self, snapshot: Mapping[str, Any]) -> None:
        """Rebuild every log from a snapshot. Turn results are not replayed; only their count."""
        self.session_id = snapshot.get("session_id") or None
        self.turn = int(snapshot.get("turn", 0))
        self.total_cost_usd = float(snapshot.get("total_cost_usd", 0.0))
        self._cost_offset = self.total_cost_usd
        self.state = CanvasState.from_dict(snapshot["canvas"])
        if snapshot.get("match"):
            self.match = DepartmentMatch.from_dict(snapshot["match"])
            self._match_attempts = 1
        completeness = snapshot.get("completeness")
        if completeness:
            self.completeness = CompletenessTracker(
                assessments=[Assessment.from_dict(a) for a in completeness["assessments"]],
                rounds={CanvasCategory(k): int(v) for k, v in completeness["rounds"].items()},
            )
        if snapshot.get("web_search") and self.web_search_guard is not None:
            self.web_search_guard = WebSearchGuard.from_dict(snapshot["web_search"])
        self.reviews = [ReviewDecision.from_dict(d) for d in snapshot.get("reviews", [])]
        self.resumed = True
        logger.info(
            "session.restored",
            extra={
                "session_id": self.session_id,
                "turn": self.turn,
                "entries": len(self.state.entries),
                "reviews": len(self.reviews),
            },
        )

    def record_review(self, decision: ReviewDecision) -> None:
        """Append a human decision. The log is never edited."""
        self.reviews.append(decision)
        logger.info(
            "review.decision",
            extra={
                "session_id": self.session_id,
                "action": decision.action.value,
                "reviewer": decision.reviewer,
                "spec_version": decision.spec_version,
                "risk_level": decision.risk_level.value,
            },
        )

    @property
    def review_status(self) -> str:
        """``pending`` | ``approved`` | ``rejected`` | ``sent_back``, derived from the log."""
        return review_status(self.reviews)

    async def _assess(self, entry: CanvasEntry, version: int) -> tuple[Assessment | None, bool]:
        """Run the completeness check for one capture; called from inside the record tool.

        Failures are logged and swallowed: a capture must never fail because the checker did.
        """
        try:
            assessment = await self._assess_fn(
                entry.category, entry.summary, version=version, model=self._checker_model
            )
        except (AssessmentError, ValueError) as exc:
            logger.error(
                "completeness.failed",
                extra={"category": entry.category.value, "version": version, "error": str(exc)},
            )
            return None, False
        should_ask = self.completeness.record(assessment, max_rounds=self._max_clarification_rounds)
        logger.info(
            "completeness.recorded",
            extra={
                "category": entry.category.value,
                "version": version,
                "sufficient": assessment.sufficient,
                "ask": should_ask,
                "rounds": self.completeness.rounds.get(entry.category, 0),
            },
        )
        return assessment, should_ask

    def matched_skills(self) -> list[DepartmentSkill]:
        """Department skills the classifier selected, in relevance order; empty until it runs."""
        if self.match is None:
            return []
        return [self.skills[name] for name in self.match.departments if name in self.skills]

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
        client, self._client = self._client, None
        try:
            await client.disconnect()
        except ClaudeSDKError:
            # A subprocess that is already gone is not an error worth propagating on close.
            logger.warning("session.close.disconnect_failed", exc_info=True)
        finally:
            logger.info(
                "session.close",
                extra={
                    "session_id": self.session_id,
                    "turns": self.turn,
                    "total_cost_usd": round(self.total_cost_usd, 6),
                    "is_complete": self.is_complete,
                    "failure": self.failure,
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
        async for event in self._stream(user_text, origin="stakeholder"):
            yield event

    async def stream_reviewer_note(self, note: str) -> AsyncIterator[TurnEvent]:
        """Deliver a reviewer's send-back note; the model turns it into a question.

        The note is framed as an instruction the stakeholder cannot see, so the model asks
        about it in its own voice. The trace records the raw note with ``origin="reviewer"``;
        the requester view hides such turns, the admin view shows them.
        """
        async for event in self._stream(note, origin="reviewer"):
            yield event

    async def _stream(self, text: str, *, origin: str) -> AsyncIterator[TurnEvent]:
        if self._client is None:
            raise SessionNotStartedError(
                f"session is not running ({self.failure or 'call start() first'})"
            )
        user_text = validate_user_text(text)
        model_text = frame_reviewer_note(user_text) if origin == "reviewer" else user_text

        # Increment BEFORE query() so tool calls made during this turn see the new number.
        self.turn += 1
        turn = self.turn
        started_at = datetime.now(UTC)
        entries_before = len(self.state.entries)
        assessments_before = len(self.completeness.assessments)
        logger.info("turn.start", extra={"turn": turn, "origin": origin, "chars": len(user_text)})

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        result: ResultMessage | None = None
        failure: str | None = None

        try:
            await self._client.query(model_text)
            messages = self._client.receive_response().__aiter__()
            while True:
                # Idle timeout: "the SDK stopped talking to us", not a cap on the whole turn,
                # so a slow consumer between yields is not penalized.
                try:
                    message = await asyncio.wait_for(messages.__anext__(), self.idle_timeout_s)
                except StopAsyncIteration:
                    break
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
                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    logger.warning(
                        "turn.rate_limit",
                        extra={
                            "turn": turn,
                            "status": info.status,
                            "rate_limit_type": info.rate_limit_type,
                            "utilization": info.utilization,
                            "resets_at": info.resets_at,
                        },
                    )
                elif isinstance(message, ResultMessage):
                    result = message
                    break
        except TimeoutError:
            failure = f"no response from the model for {self.idle_timeout_s:.0f}s"
        except ResultError as exc:
            # The CLI reported a terminal error result and exited: budget exceeded, max turns,
            # API failure. The subprocess is gone; the session cannot continue.
            failure = f"{exc.terminal_reason or exc.subtype or 'error'}: {exc.result or exc}"
        except ClaudeSDKError as exc:
            failure = f"sdk error: {exc}"

        if failure is None and result is None:
            raise TurnProtocolError(f"turn {turn}: stream ended without a ResultMessage")

        if result is not None:
            if self._pinned_session_id and result.session_id != self._pinned_session_id:
                logger.warning(
                    "session.id_mismatch",
                    extra={
                        "session_id": self._pinned_session_id,
                        "sdk_session_id": result.session_id,
                    },
                )
            else:
                self.session_id = result.session_id
            # The SDK reports a RUNNING TOTAL per ResultMessage, not a per-turn cost, and that
            # total restarts at zero in each new subprocess. After a resume, the cost spent in
            # earlier processes is carried in ``_cost_offset`` so the session total keeps rising.
            turn_cost: float | None = None
            session_cost: float | None = None
            if result.total_cost_usd is not None:
                session_cost = self._cost_offset + result.total_cost_usd
                turn_cost = session_cost - self.total_cost_usd
                self.total_cost_usd = session_cost
            is_error = result.is_error
            errors: tuple[str, ...] = tuple(result.errors or ())
            duration_ms = result.duration_ms
            num_agentic_turns = result.num_turns
        else:
            turn_cost = None
            is_error = True
            errors = (failure or "unknown failure",)
            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            num_agentic_turns = 0
            session_cost = self.total_cost_usd

        captured = tuple(e.category for e in self.state.entries[entries_before:])
        assessments = tuple(self.completeness.assessments[assessments_before:])
        turn_result = TurnResult(
            turn=turn,
            user_text=user_text,
            origin=origin,
            assistant_text="\n".join(p for p in text_parts if p.strip()).strip(),
            tool_calls=tuple(tool_calls),
            captured=captured,
            assessments=assessments,
            session_id=self.session_id or "",
            cost_usd=turn_cost,
            session_cost_usd=session_cost,
            num_agentic_turns=num_agentic_turns,
            duration_ms=duration_ms,
            is_error=is_error,
            errors=errors,
            started_at=started_at,
        )
        self.turns.append(turn_result)

        logger.log(
            logging.ERROR if is_error else logging.INFO,
            "turn.end",
            extra={
                "turn": turn,
                "session_id": self.session_id,
                "captured": [c.value for c in captured],
                "tool_calls": len(tool_calls),
                "cost_usd": turn_cost,
                "session_cost_usd": session_cost,
                "is_error": is_error,
                "errors": list(errors),
                "missing": [c.value for c in self.state.missing()],
            },
        )

        if failure is not None:
            # The subprocess exited or hung; drop it so the next send() fails fast and clearly.
            await self._abandon(failure)
        elif self._should_match(captured):
            match = await self._run_matching()
            if match is not None:
                yield DepartmentsMatched(match)

        yield TurnCompleted(turn_result)

    async def _abandon(self, reason: str) -> None:
        """Mark the session dead after an unrecoverable SDK failure; best-effort cleanup."""
        self.failure = reason
        client, self._client = self._client, None
        logger.error("session.abandoned", extra={"turn": self.turn, "reason": reason})
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # cleanup of a dead subprocess; nothing to recover
                logger.debug("session.abandon.disconnect_failed", exc_info=True)

    # -- department matching -------------------------------------------------------------

    def _should_match(self, captured_this_turn: tuple[CanvasCategory, ...]) -> bool:
        """Classify once we know who and what (key_activities captured), or by the fallback turn."""
        if self.match is not None or self._match_attempts >= MAX_MATCH_ATTEMPTS:
            return False
        know_who_and_what = CanvasCategory.KEY_ACTIVITIES in captured_this_turn
        return know_who_and_what or self.turn >= MATCH_FALLBACK_TURN

    async def _run_matching(self) -> DepartmentMatch | None:
        """Run the classifier over the conversation so far. Failures are logged, never raised."""
        self._match_attempts += 1
        transcript = self.render_transcript()
        try:
            self.match = await self._match_fn(transcript, self.skills, model=self._classifier_model)
        except MatchError as exc:
            logger.error(
                "matching.failed",
                extra={"turn": self.turn, "attempt": self._match_attempts, "error": str(exc)},
            )
            return None
        logger.info(
            "session.matched",
            extra={"turn": self.turn, "departments": list(self.match.departments)},
        )
        return self.match

    def render_transcript(self) -> str:
        """The conversation so far as plain text, for the classifier."""
        lines = []
        for t in self.turns:
            lines.append(f"{'reviewer' if t.origin == 'reviewer' else 'you'}> {t.user_text}")
            lines.append(f"assistant> {t.assistant_text}")
        return "\n".join(lines)

    async def send(self, user_text: str) -> TurnResult:
        """Non-streaming turn: drain ``stream`` and return the final ``TurnResult``."""
        return await self._drain(self.stream(user_text))

    async def send_reviewer_note(self, note: str) -> TurnResult:
        """Non-streaming reviewer turn; see :meth:`stream_reviewer_note`."""
        return await self._drain(self.stream_reviewer_note(note))

    @staticmethod
    async def _drain(events: AsyncIterator[TurnEvent]) -> TurnResult:
        async for event in events:
            if isinstance(event, TurnCompleted):
                return event.result
        raise TurnProtocolError("stream ended without a TurnCompleted event")  # unreachable

    # -- derived state -------------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """True once every canvas category has been recorded at least once."""
        return self.state.is_complete

    @property
    def is_ready_for_review(self) -> bool:
        """The handoff decision: complete, and no category's latest capture has open gaps.

        With the check disabled this equals ``is_complete``. A category that has used its
        clarification rounds still counts as ready; its gaps are carried to the reviewer.
        """
        if not self.state.is_complete:
            return False
        if not self._completeness_check:
            return True
        return all(
            self.completeness.rounds.get(category, 0) >= self._max_clarification_rounds
            for category in self.completeness.gaps(self.state)
        )

    def to_dict(self) -> dict[str, Any]:
        """Full serializable snapshot: canvas + every turn. This is the spec plus its trace."""
        return {
            "session_id": self.session_id,
            "model": self.options.model,
            "turn": self.turn,
            "total_cost_usd": self.total_cost_usd,
            "is_complete": self.is_complete,
            "failure": self.failure,
            "match": self.match.to_dict() if self.match else None,
            "web_search": self.web_search_guard.to_dict() if self.web_search_guard else None,
            "is_ready_for_review": self.is_ready_for_review,
            "completeness": self.completeness.to_dict() if self._completeness_check else None,
            "open_gaps": {c.value: list(g) for c, g in self.completeness.gaps(self.state).items()},
            "reviews": [d.to_dict() for d in self.reviews],
            "review_status": self.review_status,
            "resumed": self.resumed,
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
