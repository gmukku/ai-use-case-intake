"""HTTP layer over the pipeline: sessions, streaming turns, review, resume, build.

Three audiences, three surfaces, as CLAUDE.md requires:

- **Requester** (``/sessions/...``): plain language only. The SSE stream carries text deltas,
  an anonymous ``activity`` ping (never a tool name), and a ``done`` summary. Status exposes
  progress, never categories, risk, or classifier output.
- **Reviewer** (``/reviews/...``): the compiled spec with risk flags, and decisions.
- **Admin** (``/admin/...``, ``/builds/...``): the full trace and builds.

Reviewer and admin surfaces require a bearer token when ``BLUEPRINT_ADMIN_TOKEN`` is set.

Sessions live in this process (each owns an SDK subprocess); the snapshot is saved to the
``RunStore`` after every turn and decision, so a reviewer can read a live conversation and a
closed session can be resumed from disk. An idle sweeper closes abandoned subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from blueprint import eval_runs
from blueprint.builder import (
    PROTOTYPES_DIR,
    BuildResult,
    UnsupportedOutputFormatError,
    build_prototype,
    select_template,
)
from blueprint.feedback import MAX_COMMENT_CHARS, Rating, RequesterFeedback
from blueprint.feedback import current as current_feedback
from blueprint.observability import configure_logging
from blueprint.orchestrator import (
    MAX_USER_TEXT_CHARS,
    DepartmentsMatched,
    DiscoverySession,
    SessionNotStartedError,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
    TurnEvent,
)
from blueprint.review import (
    DiscoverySpec,
    ReviewAction,
    ReviewDecision,
    SpecSummaryError,
    compile_spec,
    review_status,
    summarize_spec,
)
from blueprint.settings import Settings, SettingsError, load_settings
from blueprint.skills import DepartmentSkill, load_skills
from blueprint.store import (
    AuditEvent,
    FileRunStore,
    RunNotFoundError,
    RunStore,
    SqliteRunStore,
    build_events,
    validate_session_id,
)
from blueprint.websearch import TavilyClient

logger = logging.getLogger(__name__)

SessionFactory = Callable[[Settings, dict[str, Any] | None, str | None], DiscoverySession]
BuildFn = Callable[..., Awaitable[BuildResult]]

IDLE_SWEEP_INTERVAL_S: Final = 30.0
LOG_FILE: Final = Path("logs") / "api.jsonl"


# -- request / response models (validation at the HTTP boundary) ------------------------------


class MessageIn(BaseModel):
    """A stakeholder message."""

    text: str = Field(min_length=1, max_length=MAX_USER_TEXT_CHARS)


class SessionCreated(BaseModel):
    """Response to ``POST /sessions``."""

    session_id: str


class SessionStatus(BaseModel):
    """Requester-safe view of a session. No categories, risk, or classifier detail."""

    session_id: str
    live: bool
    turn: int
    is_complete: bool
    ready_for_review: bool
    review_status: Literal["pending", "approved", "rejected", "sent_back"]
    build_status: Literal["none", "running", "ok", "failed"]
    can_reopen: bool = Field(
        default=False,
        description="A reviewer's question is waiting and has not been asked yet.",
    )
    outcome: str | None = Field(
        default=None,
        description="Plain-language note for the requester once there is an outcome.",
    )


class Utterance(BaseModel):
    """One side of one turn, as the requester saw it."""

    turn: int
    role: Literal["you", "agent"]
    text: str


class Transcript(BaseModel):
    """The conversation, for restoring it after a reload. Requester-safe by construction."""

    messages: list[Utterance]
    feedback: dict[int, str] = Field(
        default_factory=dict,
        description="Latest rating per turn, so the UI can show which turns were rated.",
    )


class FeedbackIn(BaseModel):
    """A requester's rating of one agent turn."""

    turn: int = Field(ge=1)
    rating: Rating
    comment: str = Field(default="", max_length=MAX_COMMENT_CHARS)


class DecisionIn(BaseModel):
    """A reviewer's decision."""

    action: ReviewAction
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=4000)


class QueueItem(BaseModel):
    """One row of the reviewer's queue."""

    session_id: str
    title: str
    risk_level: str
    needs_extra_scrutiny: bool
    review_status: str
    turns: int
    ready_for_review: bool
    live: bool


class AdminSession(BaseModel):
    """One row of the admin's session list. Unlike the review queue, nothing is filtered out."""

    session_id: str
    live: bool
    turn: int
    is_complete: bool
    ready_for_review: bool
    review_status: str
    build_status: str
    departments: list[str]
    open_gaps: dict[str, list[str]]
    total_cost_usd: float
    model: str | None
    failure: str | None
    feedback: dict[int, str]
    resumed: bool


class BuildStatus(BaseModel):
    """State of the build for a session."""

    session_id: str
    status: Literal["none", "running", "ok", "failed"]
    result: dict[str, Any] | None = None


# -- in-process session registry --------------------------------------------------------------


@dataclass
class LiveSession:
    """A running session plus the bookkeeping the API needs around it."""

    session: DiscoverySession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Mark activity for the idle sweeper."""
        self.last_used = time.monotonic()


class Registry:
    """Live sessions keyed by id, with an idle timeout that closes abandoned subprocesses."""

    def __init__(self, idle_s: float) -> None:
        self.idle_s = idle_s
        self._live: dict[str, LiveSession] = {}

    def get(self, session_id: str) -> LiveSession | None:
        """The live session, or ``None`` if not running."""
        return self._live.get(session_id)

    def add(self, session_id: str, session: DiscoverySession) -> LiveSession:
        """Register a started session."""
        live = LiveSession(session=session)
        self._live[session_id] = live
        return live

    async def close(self, session_id: str) -> bool:
        """Close and forget a live session; ``False`` if it was not live."""
        live = self._live.pop(session_id, None)
        if live is None:
            return False
        await live.session.close()
        return True

    async def sweep(self) -> list[str]:
        """Close sessions idle longer than ``idle_s``; return their ids."""
        now = time.monotonic()
        stale = [sid for sid, live in self._live.items() if now - live.last_used > self.idle_s]
        for sid in stale:
            logger.info("api.session.idle_closed", extra={"session_id": sid})
            await self.close(sid)
        return stale

    async def close_all(self) -> None:
        """Shutdown: close every subprocess."""
        for sid in list(self._live):
            await self.close(sid)

    @property
    def ids(self) -> list[str]:
        """Ids of live sessions."""
        return list(self._live)


# -- app factory ------------------------------------------------------------------------------


def default_session_factory(
    settings: Settings, snapshot: dict[str, Any] | None, session_id: str | None
) -> DiscoverySession:
    """Build a real session from settings (mirrors the CLI)."""
    search = (
        TavilyClient(settings.tavily_api_key)
        if settings.web_search_enabled and settings.tavily_api_key
        else None
    )
    return DiscoverySession(
        model=settings.model,
        classifier_model=settings.classifier_model,
        max_budget_usd=settings.max_budget_usd,
        idle_timeout_s=settings.idle_timeout_s,
        web_search=search,
        sop_grounding=settings.sop_grounding,
        completeness_check=settings.completeness_check,
        checker_model=settings.checker_model,
        snapshot=snapshot,
        session_id=session_id,
    )


def _default_store(settings: Settings) -> RunStore:
    """SQLite when a path is configured, the JSON files otherwise.

    Both satisfy `RunStore`, so nothing below this line changes either way — which is what
    the protocol was for.
    """
    if settings.store_path:
        return SqliteRunStore(Path(settings.store_path), redact=settings.redact_stored_text)
    return FileRunStore(Path("runs"))


def create_app(
    settings: Settings,
    *,
    session_factory: SessionFactory = default_session_factory,
    store: RunStore | None = None,
    build_fn: BuildFn = build_prototype,
    skills: dict[str, DepartmentSkill] | None = None,
    prototypes_dir: Path = PROTOTYPES_DIR,
) -> FastAPI:
    """Assemble the application. Everything with side effects is injectable for tests."""
    run_store: RunStore = store if store is not None else _default_store(settings)
    loaded_skills = skills if skills is not None else load_skills()
    registry = Registry(idle_s=settings.session_idle_s)
    builds: dict[str, BuildStatus] = {}
    # Background tasks are strongly referenced until they finish: the event loop only holds a
    # weak reference, so a task that nothing keeps can be garbage-collected mid-run.
    background: set[asyncio.Task[None]] = set()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:
            while True:
                await asyncio.sleep(IDLE_SWEEP_INTERVAL_S)
                with contextlib.suppress(Exception):
                    await registry.sweep()

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            await registry.close_all()

    app = FastAPI(title="Blueprint AI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.registry = registry
    app.state.store = run_store

    # -- auth -----------------------------------------------------------------------------

    def require_admin(request: Request) -> None:
        if settings.admin_token is None:
            return
        header = request.headers.get("authorization", "")
        if header != f"Bearer {settings.admin_token}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required")

    admin = Depends(require_admin)

    # -- helpers --------------------------------------------------------------------------

    def _sid(session_id: str) -> str:
        try:
            return validate_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    def _snapshot(session_id: str) -> dict[str, Any]:
        live = registry.get(session_id)
        if live is not None:
            return live.session.to_dict()
        try:
            return run_store.load(session_id)
        except RunNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc

    def _persist(session: DiscoverySession) -> None:
        snapshot = session.to_dict()
        run_store.save(snapshot)
        _record_pipeline_events(snapshot)

    def _audit(session_id: str, action: str, *, actor: str = "system", **detail: object) -> None:
        """Record a human action.

        Never fails a request: an audit write that breaks the thing it is auditing is
        worse than a gap in the trail, which the JSON-lines logs still cover.
        """
        try:
            run_store.append_event(
                AuditEvent(session_id=session_id, action=action, actor=actor, detail=detail)
            )
        except Exception:
            logger.exception("api.audit.failed", extra={"session_id": session_id})

    def _record_pipeline_events(snapshot: dict[str, Any]) -> None:
        """Append the agent's own steps for turns that have happened since the last save.

        Derived from the snapshot rather than written during the turn, so there is one source
        of truth and a migrated run ends up with the same trail as a live one. Idempotent by
        counting what is already recorded.
        """
        session_id = str(snapshot.get("session_id") or "")
        if not session_id:
            return
        try:
            # Count only the derived actions: human events share the log and would otherwise
            # push this index past the events that still need writing.
            derived = {"departments.matched", "turn.completed", "tool.called"}
            already = sum(1 for e in run_store.events(session_id) if e.action in derived)
            for event in list(build_events(snapshot))[already:]:
                run_store.append_event(event)
        except Exception:
            logger.exception("api.audit.failed", extra={"session_id": session_id})

    def _build_status(session_id: str, snapshot: dict[str, Any]) -> BuildStatus:
        if session_id in builds:
            return builds[session_id]
        stored = snapshot.get("build")
        if stored:
            return BuildStatus(
                session_id=session_id, status="ok" if stored.get("ok") else "failed", result=stored
            )
        return BuildStatus(session_id=session_id, status="none")

    def _pending_note(snapshot: dict[str, Any]) -> str | None:
        """The reviewer's question, if one is still waiting to be asked.

        A send-back is "delivered" once a turn with ``origin == "reviewer"`` has run for it.
        Comparing counts rather than setting a flag keeps this derivable from the snapshot
        alone, so a reload cannot deliver the same question to the requester twice.
        """
        reviews = snapshot.get("reviews", [])
        if not reviews or reviews[-1].get("action") != ReviewAction.SEND_BACK.value:
            return None
        sent_back = sum(1 for r in reviews if r.get("action") == ReviewAction.SEND_BACK.value)
        delivered = sum(1 for t in snapshot.get("turns", []) if t.get("origin") == "reviewer")
        return str(reviews[-1].get("note", "")) if delivered < sent_back else None

    def _status(session_id: str, snapshot: dict[str, Any]) -> SessionStatus:
        rstatus = snapshot.get("review_status", "pending")
        build = _build_status(session_id, snapshot)
        can_reopen = _pending_note(snapshot) is not None
        outcome: str | None = None
        if build.status == "ok":
            outcome = "A small illustrative prototype is ready for you to try."
        elif rstatus == "approved":
            outcome = "Your request was approved and a prototype is being prepared."
        elif rstatus == "rejected":
            outcome = "A reviewer decided not to proceed with this request."
        elif can_reopen:
            outcome = "A reviewer has a follow-up question; reopen the conversation to answer it."
        elif rstatus == "sent_back":
            outcome = None  # the question has been asked; the conversation is simply open again
        elif snapshot.get("is_ready_for_review"):
            outcome = "Thanks, a reviewer will look at this before anything is built."
        return SessionStatus(
            session_id=session_id,
            live=registry.get(session_id) is not None,
            turn=int(snapshot.get("turn", 0)),
            is_complete=bool(snapshot.get("is_complete")),
            ready_for_review=bool(snapshot.get("is_ready_for_review")),
            review_status=rstatus,
            build_status=build.status,
            can_reopen=can_reopen,
            outcome=outcome,
        )

    async def _open(session_id: str) -> LiveSession:
        """Return the live session, restoring it from the store if needed."""
        live = registry.get(session_id)
        if live is not None:
            return live
        try:
            snapshot = run_store.load(session_id)
        except RunNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session") from exc
        session = session_factory(settings, snapshot, None)
        await session.start()
        return registry.add(session_id, session)

    def _sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    async def _stream_turn(
        session_id: str, live: LiveSession, events: AsyncIterator[TurnEvent]
    ) -> AsyncIterator[str]:
        """Translate orchestrator events into the requester-safe SSE vocabulary."""
        try:
            async for event in events:
                match event:
                    case TextDelta(text=chunk):
                        yield _sse("text", {"text": chunk})
                    case ToolCallStarted():
                        yield _sse("activity", {})  # never the tool name
                    case DepartmentsMatched():
                        pass  # admin trace only
                    case TurnCompleted(result=result):
                        _persist(live.session)
                        yield _sse(
                            "done",
                            {
                                "turn": result.turn,
                                "is_complete": live.session.is_complete,
                                "ready_for_review": live.session.is_ready_for_review,
                                "error": result.errors[0] if result.is_error else None,
                            },
                        )
        except SessionNotStartedError as exc:
            yield _sse("error", {"message": str(exc)})
        finally:
            live.touch()
            live.lock.release()

    # -- requester surface ----------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "live_sessions": len(registry.ids)}

    @app.post("/sessions", response_model=SessionCreated, status_code=status.HTTP_201_CREATED)
    async def create_session() -> SessionCreated:
        session_id = str(uuid.uuid4())
        session = session_factory(settings, None, session_id)
        await session.start()
        registry.add(session_id, session)
        logger.info("api.session.created", extra={"session_id": session_id})
        _audit(session_id, "session.created", actor="requester")
        return SessionCreated(session_id=session_id)

    @app.post("/sessions/{session_id}/messages")
    async def send_message(session_id: str, body: MessageIn) -> StreamingResponse:
        session_id = _sid(session_id)
        live = await _open(session_id)
        if live.lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, "a turn is already in progress")
        await live.lock.acquire()
        return StreamingResponse(
            _stream_turn(session_id, live, live.session.stream(body.text)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> StreamingResponse:
        """Reopen a session. If a reviewer sent it back, their question is delivered now."""
        session_id = _sid(session_id)
        live = await _open(session_id)
        session = live.session
        note = _pending_note(session.to_dict())
        if note is None:
            # Nothing to deliver: the session is simply reopened. Answer in the same SSE
            # vocabulary so the client has one code path for both cases.
            async def nothing() -> AsyncIterator[str]:
                yield _sse(
                    "done",
                    {
                        "turn": session.turn,
                        "is_complete": session.is_complete,
                        "ready_for_review": session.is_ready_for_review,
                        "error": None,
                    },
                )

            return StreamingResponse(nothing(), media_type="text/event-stream")
        if live.lock.locked():
            raise HTTPException(status.HTTP_409_CONFLICT, "a turn is already in progress")
        await live.lock.acquire()
        return StreamingResponse(
            _stream_turn(session_id, live, session.stream_reviewer_note(note)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/sessions/{session_id}", response_model=SessionStatus)
    async def get_session(session_id: str) -> SessionStatus:
        session_id = _sid(session_id)
        return _status(session_id, _snapshot(session_id))

    @app.get("/sessions/{session_id}/messages", response_model=Transcript)
    async def transcript(session_id: str) -> Transcript:
        """The conversation as the requester saw it, for restoring the page after a reload."""
        session_id = _sid(session_id)
        snap = _snapshot(session_id)
        messages: list[Utterance] = []
        for turn in snap.get("turns", []):
            number = int(turn.get("turn", 0))
            # A reviewer's send-back note is the `user_text` of its turn, and the requester
            # never saw it. The agent's reply to it is safe: the model is instructed to ask
            # in its own voice without mentioning that anything was sent back.
            if turn.get("origin") != "reviewer" and turn.get("user_text"):
                messages.append(Utterance(turn=number, role="you", text=str(turn["user_text"])))
            if turn.get("assistant_text"):
                messages.append(
                    Utterance(turn=number, role="agent", text=str(turn["assistant_text"]))
                )
        entries = [RequesterFeedback.from_dict(f) for f in snap.get("feedback", [])]
        return Transcript(
            messages=messages,
            feedback={t: f.rating.value for t, f in current_feedback(entries).items()},
        )

    @app.post("/sessions/{session_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
    async def rate_turn(session_id: str, body: FeedbackIn) -> None:
        """Record what the requester thought of one agent turn. Append-only; last one wins."""
        session_id = _sid(session_id)
        entry = RequesterFeedback(turn=body.turn, rating=body.rating, comment=body.comment)
        live = registry.get(session_id)
        if live is not None:
            try:
                live.session.record_feedback(entry)
            except ValueError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
            _persist(live.session)
            live.touch()
        else:
            snap = _snapshot(session_id)
            if entry.turn > int(snap.get("turn", 0)):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, f"turn {entry.turn} has not happened"
                )
            snap.setdefault("feedback", []).append(entry.to_dict())
            run_store.save(snap)
        logger.info(
            "api.feedback",
            extra={"session_id": session_id, "turn": entry.turn, "rating": entry.rating.value},
        )
        _audit(
            session_id,
            "feedback.recorded",
            actor="requester",
            turn=entry.turn,
            rating=entry.rating.value,
            comment=entry.comment,
        )

    @app.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def close_session(session_id: str) -> None:
        session_id = _sid(session_id)
        live = registry.get(session_id)
        if live is not None:
            _persist(live.session)
        if not await registry.close(session_id) and not run_store.exists(session_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")

    # -- reviewer surface -----------------------------------------------------------------

    def _compiled(session_id: str, snap: dict[str, Any]) -> DiscoverySpec:
        """Compile the spec for a snapshot, reusing a stored title/narrative when present."""
        stored = snap.get("spec")
        version = len(snap.get("reviews", [])) + 1
        try:
            return compile_spec(
                snap,
                loaded_skills,
                version=version,
                title=stored["title"] if stored else None,
                narrative=stored["narrative"] if stored else None,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @app.get("/reviews", response_model=list[QueueItem], dependencies=[admin])
    async def review_queue() -> list[QueueItem]:
        items: list[QueueItem] = []
        seen: set[str] = set()
        for sid in [*registry.ids, *run_store.ids()]:
            if sid in seen:
                continue
            seen.add(sid)
            snap = _snapshot(sid)
            if not snap.get("is_complete"):
                continue
            spec = _compiled(sid, snap)
            items.append(
                QueueItem(
                    session_id=sid,
                    title=spec.title,
                    risk_level=spec.risk.level.value,
                    needs_extra_scrutiny=spec.risk.needs_extra_scrutiny,
                    review_status=snap.get("review_status", "pending"),
                    turns=int(snap.get("turn", 0)),
                    ready_for_review=bool(snap.get("is_ready_for_review")),
                    live=registry.get(sid) is not None,
                )
            )
        return items

    @app.get("/reviews/{session_id}", dependencies=[admin])
    async def get_review(session_id: str, summarize: bool = False) -> dict[str, Any]:
        session_id = _sid(session_id)
        snap = _snapshot(session_id)
        spec = _compiled(session_id, snap)
        if summarize and not snap.get("spec"):
            try:
                title, narrative = await summarize_spec(spec.categories, model=settings.model)
                spec = compile_spec(
                    snap, loaded_skills, version=spec.version, title=title, narrative=narrative
                )
            except SpecSummaryError as exc:
                logger.warning("api.review.summarize_failed", extra={"error": str(exc)})
        return {
            "spec": spec.to_dict(),
            "review_status": snap.get("review_status", "pending"),
            "reviews": snap.get("reviews", []),
        }

    @app.post("/reviews/{session_id}/decision", dependencies=[admin])
    async def decide(session_id: str, body: DecisionIn) -> dict[str, Any]:
        session_id = _sid(session_id)
        snap = _snapshot(session_id)
        spec = _compiled(session_id, snap)
        try:
            decision = ReviewDecision(
                action=body.action,
                reviewer=body.reviewer,
                note=body.note,
                spec_version=spec.version,
                risk_level=spec.risk.level,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        live = registry.get(session_id)
        if live is not None:
            live.session.record_review(decision)
            snap = live.session.to_dict()
        else:
            decisions = [ReviewDecision.from_dict(d) for d in snap.get("reviews", [])]
            decisions.append(decision)
            snap["reviews"] = [d.to_dict() for d in decisions]
            snap["review_status"] = review_status(decisions)
        snap["spec"] = spec.to_dict()
        run_store.save(snap)
        logger.info(
            "api.review.decision",
            extra={
                "session_id": session_id,
                "action": body.action.value,
                "reviewer": body.reviewer,
                "spec_version": spec.version,
            },
        )
        _audit(
            session_id,
            "review.decision",
            actor=body.reviewer,
            action_taken=body.action.value,
            spec_version=spec.version,
            risk_level=spec.risk.level.value,
            note=body.note,
        )
        return {"review_status": snap["review_status"], "spec_version": spec.version}

    # -- admin surface --------------------------------------------------------------------

    @app.get("/admin/sessions", response_model=list[AdminSession], dependencies=[admin])
    async def admin_sessions() -> list[AdminSession]:
        """Every session, complete or not. The review queue hides the unfinished ones."""
        rows: list[AdminSession] = []
        seen: set[str] = set()
        for sid in [*registry.ids, *run_store.ids()]:
            if sid in seen:
                continue
            seen.add(sid)
            snap = _snapshot(sid)
            entries = [RequesterFeedback.from_dict(f) for f in snap.get("feedback", [])]
            rows.append(
                AdminSession(
                    session_id=sid,
                    live=registry.get(sid) is not None,
                    turn=int(snap.get("turn", 0)),
                    is_complete=bool(snap.get("is_complete")),
                    ready_for_review=bool(snap.get("is_ready_for_review")),
                    review_status=str(snap.get("review_status", "pending")),
                    build_status=_build_status(sid, snap).status,
                    departments=list((snap.get("match") or {}).get("departments", [])),
                    open_gaps={k: list(v) for k, v in (snap.get("open_gaps") or {}).items()},
                    total_cost_usd=float(snap.get("total_cost_usd", 0.0)),
                    model=snap.get("model"),
                    failure=snap.get("failure"),
                    feedback={t: f.rating.value for t, f in current_feedback(entries).items()},
                    resumed=bool(snap.get("resumed")),
                )
            )
        return rows

    @app.get("/admin/evals", dependencies=[admin])
    async def admin_evals() -> dict[str, Any]:
        """Recorded eval runs, grouped by suite, newest first."""
        return {
            "suites": [
                {"suite": suite, "runs": [r.to_dict() for r in eval_runs.runs(suite)]}
                for suite in eval_runs.suites()
            ]
        }

    @app.get("/admin/evals/{suite}/{run_id}", dependencies=[admin])
    async def admin_eval_run(suite: str, run_id: str) -> dict[str, Any]:
        """One eval run in full, including every per-call result."""
        try:
            return eval_runs.load(suite, run_id)
        except eval_runs.EvalNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such eval run") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @app.get("/admin/sessions/{session_id}/audit", dependencies=[admin])
    async def audit_trail(session_id: str) -> dict[str, Any]:
        """The append-only record of what happened to one session, oldest first.

        Distinct from the trace: the trace is current state, this is history. A spec that was
        sent back and then approved shows both decisions here and only the latest there.
        """
        session_id = _sid(session_id)
        if not run_store.exists(session_id) and registry.get(session_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown session")
        return {"events": [e.to_dict() for e in run_store.events(session_id)]}

    @app.get("/admin/sessions/{session_id}/trace", dependencies=[admin])
    async def trace(session_id: str) -> dict[str, Any]:
        session_id = _sid(session_id)
        return _snapshot(session_id)

    @app.post(
        "/builds/{session_id}",
        response_model=BuildStatus,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[admin],
    )
    async def start_build(session_id: str, force: bool = False) -> BuildStatus:
        """Start the Builder for an approved session.

        A build costs real money and overwrites its workspace, so this is deliberately not
        idempotent-by-rebuilding: a request while one is in flight returns that run, and a
        request against a finished build is refused unless ``force`` says to redo it.
        """
        session_id = _sid(session_id)
        snap = _snapshot(session_id)
        if snap.get("review_status") != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, "session is not approved")
        current = _build_status(session_id, snap)
        if current.status == "running":
            return current
        if current.status != "none" and not force:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"this session was already built ({current.status}); pass force=true to rebuild",
            )
        spec = _compiled(session_id, snap)
        try:
            select_template(spec)
        except UnsupportedOutputFormatError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        builds[session_id] = BuildStatus(session_id=session_id, status="running")

        async def run() -> None:
            try:
                result = await build_fn(
                    spec,
                    workspace=prototypes_dir / session_id,
                    model=settings.model,
                    max_budget_usd=settings.build_budget_usd,
                )
                latest = _snapshot(session_id)
                latest["build"] = result.to_dict()
                run_store.save(latest)
                builds[session_id] = BuildStatus(
                    session_id=session_id,
                    status="ok" if result.ok else "failed",
                    result=result.to_dict(),
                )
                _audit(
                    session_id,
                    "build.finished",
                    ok=result.ok,
                    files=len(result.verification.files),
                    cost_usd=result.cost_usd,
                    violations=list(result.verification.violations),
                )
            except Exception as exc:  # a build must never take the API down
                logger.exception("api.build.crashed", extra={"session_id": session_id})
                builds[session_id] = BuildStatus(
                    session_id=session_id, status="failed", result={"error": str(exc)}
                )

        _audit(session_id, "build.started", template=select_template(spec))
        task = asyncio.create_task(run())
        background.add(task)
        task.add_done_callback(background.discard)
        return builds[session_id]

    @app.get("/builds/{session_id}", response_model=BuildStatus, dependencies=[admin])
    async def get_build(session_id: str) -> BuildStatus:
        session_id = _sid(session_id)
        return _build_status(session_id, _snapshot(session_id))

    return app


def app_from_env() -> FastAPI:
    """Entry point for ``uvicorn blueprint.api:app_from_env --factory``.

    Reads ``.env`` and validates settings here, so a misconfigured deployment fails at
    startup with a plain message rather than on a stakeholder's first message.
    """
    load_dotenv()
    configure_logging(LOG_FILE)
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        # The docstring's promise is "a plain message", and a traceback is not one. Missing
        # credentials is the first thing anyone hits on a fresh clone; it should read as a
        # step they skipped, not as software that is broken. Matches what cli.py does.
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    logger.info("api.starting", extra=settings.to_dict())
    return create_app(settings)
