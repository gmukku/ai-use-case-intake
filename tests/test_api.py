import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions

from blueprint import eval_runs
from blueprint.api import create_app
from blueprint.builder import BuildResult, Verification
from blueprint.canvas import CanvasCategory
from blueprint.orchestrator import DiscoverySession
from blueprint.review import DiscoverySpec
from blueprint.settings import Settings
from blueprint.skills import SKILLS_DIR, load_skills
from blueprint.store import RunNotFoundError
from tests.test_orchestrator import FakeAssessor, FakeClient, FakeMatcher, Step

SKILLS = load_skills(SKILLS_DIR)

# One turn that records all seven categories, so the canvas is complete and a spec compiles.
# output_format names a Q&A chatbot on purpose: it is the one format the Builder has a
# template for, so the build routes have something to select.
COMPLETE_TURN: list[Step] = [
    *(
        {
            "category": c.value,
            "summary": (
                "A question-and-answer chatbot the team can ask."
                if c is CanvasCategory.OUTPUT_FORMAT
                else f"{c.label} summary."
            ),
        }
        for c in CanvasCategory
    ),
    "That covers everything, thank you.",
]


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "auth_mode": "api_key",
        "model": "fake-model",
        "classifier_model": "fake-model",
        "max_budget_usd": 1.0,
        "idle_timeout_s": 5.0,
        "web_search_enabled": False,
        "tavily_api_key": None,
        "sop_grounding": False,
        "completeness_check": True,
        "checker_model": "fake-checker",
        "build_budget_usd": 1.0,
        "admin_token": None,
        "session_idle_s": 900.0,
        "cors_origins": ("http://localhost:3000",),
    }
    base.update(overrides)
    return Settings(**base)


class MemoryStore:
    """A RunStore that never touches disk; insertion order stands in for mtime order."""

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, Any]] = {}

    def save(self, snapshot: dict[str, Any]) -> None:
        self.saved[str(snapshot["session_id"])] = json.loads(json.dumps(snapshot))

    def load(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.saved:
            raise RunNotFoundError(session_id)
        return self.saved[session_id]

    def exists(self, session_id: str) -> bool:
        return session_id in self.saved

    def ids(self) -> Iterator[str]:
        return iter(reversed(list(self.saved)))


def fake_build_result(session_id: str, *, ok: bool = True) -> BuildResult:
    return BuildResult(
        session_id=session_id,
        spec_version=1,
        template="qa_chatbot",
        workspace=f"prototypes/{session_id}",
        report="Built a Q&A stub.",
        verification=Verification(
            tests_passed=ok,
            test_output="8 passed",
            files=("qa.py",),
            total_lines=120,
            violations=() if ok else ("missing banner",),
        ),
        guard={"denied": 0},
        model="fake-model",
        cost_usd=0.4,
        duration_ms=1000,
        is_error=False,
        errors=(),
    )


class Harness:
    """An app wired to fakes, plus the handles a test needs to poke at its insides."""

    def __init__(self, app: Any, store: MemoryStore, clients: list[FakeClient]) -> None:
        self.app = app
        self.store = store
        self.clients = clients
        self.build_calls: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None

    def hold_builds(self) -> asyncio.Event:
        """Make every build block until the returned event is set."""
        self.gate = asyncio.Event()
        return self.gate

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", **kwargs
        )


def make_harness(
    script: list[list[Step]] | None = None,
    *,
    admin_token: str | None = None,
    build_ok: bool = True,
    build_raises: bool = False,
    tmp_path: Path | None = None,
) -> Harness:
    turns = script if script is not None else [list(COMPLETE_TURN)]
    store = MemoryStore()
    clients: list[FakeClient] = []
    harness_box: list[Harness] = []

    def session_factory(
        cfg: Settings, snapshot: dict[str, Any] | None, session_id: str | None
    ) -> DiscoverySession:
        def client_factory(options: ClaudeAgentOptions) -> Any:
            client = FakeClient(options, turns)
            clients.append(client)
            return client

        return DiscoverySession(
            client_factory=client_factory,
            skills=SKILLS,
            match_fn=FakeMatcher(),
            assess_fn=FakeAssessor(),
            snapshot=snapshot,
            session_id=session_id,
        )

    async def build_fn(spec: DiscoverySpec, **kwargs: Any) -> BuildResult:
        harness = harness_box[0]
        harness.build_calls.append({"spec": spec, **kwargs})
        if harness.gate is not None:
            await harness.gate.wait()  # hold the build "running" for as long as a test wants
        if build_raises:
            raise RuntimeError("builder exploded")
        return fake_build_result(spec.session_id, ok=build_ok)

    app = create_app(
        settings(admin_token=admin_token),
        session_factory=session_factory,
        store=store,
        build_fn=build_fn,
        skills=SKILLS,
        prototypes_dir=tmp_path or Path("prototypes"),
    )
    harness = Harness(app, store, clients)
    harness_box.append(harness)
    return harness


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Split an SSE body into (event name, data) pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name, data = "message", "{}"
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append((name, json.loads(data)))
    return events


async def start_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/sessions")
    assert response.status_code == 201
    session_id: str = response.json()["session_id"]
    return session_id


async def complete_canvas(client: httpx.AsyncClient) -> str:
    """Create a session and run the one turn that fills every category."""
    session_id = await start_session(client)
    response = await client.post(f"/sessions/{session_id}/messages", json={"text": "hello"})
    assert response.status_code == 200
    return session_id


async def approved_session(client: httpx.AsyncClient) -> str:
    """Complete the canvas and approve it, so a build is allowed to start."""
    session_id = await complete_canvas(client)
    response = await client.post(
        f"/reviews/{session_id}/decision",
        json={"action": "approve", "reviewer": "dana", "note": ""},
    )
    assert response.status_code == 200
    return session_id


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    yield make_harness()


class TestHealthAndSessions:
    async def test_health(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    async def test_create_session_returns_a_uuid_and_registers_it(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
        assert len(session_id) == 36
        assert harness.app.state.registry.get(session_id) is not None

    async def test_the_api_id_is_the_session_id_the_sdk_transcript_uses(
        self, harness: Harness
    ) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
        options = harness.clients[0].options
        assert options.session_id == session_id

    async def test_a_result_message_cannot_rename_the_session(self, harness: Harness) -> None:
        # The fake reports session_id="sess-1" on every turn; the caller's id must survive it,
        # or the registry key and the stored snapshot would drift apart.
        async with harness.client() as client:
            session_id = await complete_canvas(client)
        assert harness.store.load(session_id)["session_id"] == session_id

    async def test_invalid_session_id_is_rejected_before_the_store(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.get("/sessions/..%2F..%2Fetc")
        assert response.status_code in (400, 404)

    async def test_unknown_session_is_404(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.get(f"/sessions/{'a' * 36}")
        assert response.status_code == 404

    async def test_delete_closes_the_subprocess(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.delete(f"/sessions/{session_id}")
        assert response.status_code == 204
        assert harness.app.state.registry.get(session_id) is None
        assert harness.clients[0].disconnected is True
        assert harness.store.exists(session_id)  # closing persists, it does not discard


class TestMessageStream:
    async def test_stream_carries_text_then_done(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.post(
                f"/sessions/{session_id}/messages", json={"text": "I want to automate onboarding"}
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        assert [name for name, _ in events][-1] == "done"
        text = "".join(data["text"] for name, data in events if name == "text")
        assert "That covers everything" in text
        done = events[-1][1]
        assert done["is_complete"] is True and done["error"] is None

    async def test_activity_pings_never_name_a_tool(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.post(f"/sessions/{session_id}/messages", json={"text": "hi"})
        events = parse_sse(response.text)
        activity = [data for name, data in events if name == "activity"]
        assert len(activity) == len(CanvasCategory)  # one per record call
        assert all(data == {} for data in activity)
        assert "record_canvas_answer" not in response.text
        assert "mcp__" not in response.text

    async def test_each_turn_persists_the_snapshot(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
        saved = harness.store.load(session_id)
        assert saved["turn"] == 1
        assert saved["is_complete"] is True
        assert len(saved["turns"]) == 1

    async def test_blank_message_is_rejected_by_the_model(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.post(f"/sessions/{session_id}/messages", json={"text": ""})
        assert response.status_code == 422

    async def test_a_second_turn_while_one_runs_is_a_conflict(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            live = harness.app.state.registry.get(session_id)
            await live.lock.acquire()  # stand in for a turn already streaming
            try:
                response = await client.post(
                    f"/sessions/{session_id}/messages", json={"text": "again"}
                )
            finally:
                live.lock.release()
        assert response.status_code == 409

    async def test_the_lock_is_released_after_a_turn(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            harness.clients[0].script.append(["Anything else?"])
            second = await client.post(f"/sessions/{session_id}/messages", json={"text": "more"})
        assert second.status_code == 200
        assert parse_sse(second.text)[-1][1]["turn"] == 2

    async def test_a_closed_session_is_reopened_from_the_store(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.delete(f"/sessions/{session_id}")
            assert harness.app.state.registry.get(session_id) is None
            harness.clients[0].script.append(list(COMPLETE_TURN))
            response = await client.post(f"/sessions/{session_id}/messages", json={"text": "back"})
        assert response.status_code == 200
        assert harness.app.state.registry.get(session_id) is not None


class TestRequesterStatus:
    async def test_status_says_nothing_about_the_machinery(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.get(f"/sessions/{session_id}")
        body = response.json()
        assert body["ready_for_review"] is True
        assert body["outcome"].startswith("Thanks, a reviewer")
        for leak in ("canvas", "risk", "match", "categories", "turns", "open_gaps"):
            assert leak not in body

    async def test_outcome_follows_the_review_decision(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "reject", "reviewer": "sam", "note": "not now"},
            )
            response = await client.get(f"/sessions/{session_id}")
        body = response.json()
        assert body["review_status"] == "rejected"
        assert body["outcome"] == "A reviewer decided not to proceed with this request."


class TestReviewerSurface:
    async def test_queue_lists_only_complete_sessions(self, harness: Harness) -> None:
        async with harness.client() as client:
            incomplete = await start_session(client)
            await client.post(f"/sessions/{incomplete}/messages", json={"text": "hi"})
            # Burn the completing turn on the first session; the second never completes.
            harness.clients[0].script.append([{"category": "key_stakeholders", "summary": "HR"}])
            other = await start_session(client)
            await client.post(f"/sessions/{other}/messages", json={"text": "hi"})
            response = await client.get("/reviews")
        rows = response.json()
        assert [row["session_id"] for row in rows] == [incomplete]
        assert rows[0]["review_status"] == "pending"
        assert rows[0]["ready_for_review"] is True

    async def test_review_detail_carries_the_spec_and_risk(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.get(f"/reviews/{session_id}")
        body = response.json()
        assert body["spec"]["session_id"] == session_id
        assert len(body["spec"]["categories"]) == len(CanvasCategory)
        assert body["spec"]["risk"]["level"] in {"low", "elevated", "high"}
        assert body["review_status"] == "pending"

    async def test_review_of_an_incomplete_canvas_is_a_conflict(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.get(f"/reviews/{session_id}")
        assert response.status_code == 409

    async def test_approve_records_the_decision_and_freezes_the_spec(
        self, harness: Harness
    ) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "approve", "reviewer": "dana", "note": "looks good"},
            )
        assert response.status_code == 200
        assert response.json() == {"review_status": "approved", "spec_version": 1}
        saved = harness.store.load(session_id)
        assert saved["review_status"] == "approved"
        assert saved["reviews"][0]["reviewer"] == "dana"
        assert saved["spec"]["version"] == 1

    async def test_send_back_note_is_delivered_on_resume(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "send_back", "reviewer": "dana", "note": "Which HRIS exactly?"},
            )
            harness.clients[0].script.append(["Good question, which HRIS is it?"])
            response = await client.post(f"/sessions/{session_id}/resume")
        assert response.status_code == 200
        assert "Which HRIS exactly?" in harness.clients[0].queries[-1]
        assert parse_sse(response.text)[-1][0] == "done"

    async def test_a_delivered_send_back_is_not_delivered_again(self, harness: Harness) -> None:
        # review_status stays "sent_back" after the question is asked, so a status check alone
        # would offer to reopen forever — and a reload would ask the requester twice.
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "send_back", "reviewer": "dana", "note": "Which HRIS?"},
            )
            before = await client.get(f"/sessions/{session_id}")
            assert before.json()["can_reopen"] is True

            harness.clients[0].script.append(["Which system is it?"])
            await client.post(f"/sessions/{session_id}/resume")

            after = await client.get(f"/sessions/{session_id}")
            assert after.json()["can_reopen"] is False
            assert after.json()["review_status"] == "sent_back"  # still, until reviewed again
            assert after.json()["outcome"] is None

            sent = len(harness.clients[0].queries)
            second = await client.post(f"/sessions/{session_id}/resume")
        assert second.status_code == 200
        assert len(harness.clients[0].queries) == sent  # the model was not asked twice

    async def test_a_second_send_back_can_be_delivered(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            for note in ("Which HRIS?", "And which payroll system?"):
                await client.post(
                    f"/reviews/{session_id}/decision",
                    json={"action": "send_back", "reviewer": "dana", "note": note},
                )
                harness.clients[0].script.append([f"Asking about: {note}"])
                assert (await client.get(f"/sessions/{session_id}")).json()["can_reopen"] is True
                await client.post(f"/sessions/{session_id}/resume")
            assert (await client.get(f"/sessions/{session_id}")).json()["can_reopen"] is False
        assert "And which payroll system?" in harness.clients[0].queries[-1]

    async def test_resume_without_a_send_back_just_reopens(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            sent = len(harness.clients[0].queries)
            response = await client.post(f"/sessions/{session_id}/resume")
        assert response.status_code == 200
        assert parse_sse(response.text) == [
            ("done", {"turn": 1, "is_complete": True, "ready_for_review": True, "error": None})
        ]
        assert len(harness.clients[0].queries) == sent  # nothing was sent to the model

    async def test_a_reviewer_cannot_invent_an_action(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "ship_it", "reviewer": "dana", "note": ""},
            )
        assert response.status_code == 422


class TestAdminSurface:
    async def test_trace_is_the_whole_snapshot(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.get(f"/admin/sessions/{session_id}/trace")
        body = response.json()
        assert body["canvas"]["entries"]
        assert body["match"]["departments"] == ["hr"]
        assert body["turns"][0]["tool_calls"]

    async def test_reviewer_and_admin_routes_need_the_token(self) -> None:
        harness = make_harness(admin_token="s3cret")
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            for path in (
                "/reviews",
                f"/reviews/{session_id}",
                f"/admin/sessions/{session_id}/trace",
                f"/builds/{session_id}",
            ):
                assert (await client.get(path)).status_code == 401
            ok = await client.get("/reviews", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

    async def test_the_requester_surface_stays_open(self) -> None:
        harness = make_harness(admin_token="s3cret")
        async with harness.client() as client:
            session_id = await start_session(client)
            response = await client.get(f"/sessions/{session_id}")
        assert response.status_code == 200


class TestBuilds:
    async def test_build_requires_approval(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.post(f"/builds/{session_id}")
        assert response.status_code == 409

    async def test_approved_build_runs_in_the_background(self, tmp_path: Path) -> None:
        harness = make_harness(tmp_path=tmp_path)
        async with harness.client() as client:
            session_id = await approved_session(client)
            accepted = await client.post(f"/builds/{session_id}")
            assert accepted.status_code == 202
            assert accepted.json()["status"] == "running"
            await _settle()
            status = await client.get(f"/builds/{session_id}")
        assert status.json()["status"] == "ok"
        assert harness.build_calls[0]["workspace"] == tmp_path / session_id
        assert harness.store.load(session_id)["build"]["ok"] is True

    async def test_a_failed_verification_is_reported_not_raised(self, tmp_path: Path) -> None:
        harness = make_harness(build_ok=False, tmp_path=tmp_path)
        async with harness.client() as client:
            session_id = await approved_session(client)
            await client.post(f"/builds/{session_id}")
            await _settle()
            status = await client.get(f"/builds/{session_id}")
        assert status.json()["status"] == "failed"

    async def test_a_crashing_builder_never_takes_the_api_down(self, tmp_path: Path) -> None:
        harness = make_harness(build_raises=True, tmp_path=tmp_path)
        async with harness.client() as client:
            session_id = await approved_session(client)
            await client.post(f"/builds/{session_id}")
            await _settle()
            status = await client.get(f"/builds/{session_id}")
            health = await client.get("/health")
        assert status.json()["status"] == "failed"
        assert "builder exploded" in status.json()["result"]["error"]
        assert health.status_code == 200

    async def test_a_second_start_while_running_returns_the_running_build(
        self, tmp_path: Path
    ) -> None:
        harness = make_harness(tmp_path=tmp_path)
        gate = harness.hold_builds()
        async with harness.client() as client:
            session_id = await approved_session(client)
            first = await client.post(f"/builds/{session_id}")
            second = await client.post(f"/builds/{session_id}")
            assert first.json()["status"] == second.json()["status"] == "running"
            assert len(harness.build_calls) == 1  # the second request started nothing
            gate.set()
            await _settle()
            assert (await client.get(f"/builds/{session_id}")).json()["status"] == "ok"

    async def test_rebuilding_a_finished_build_needs_force(self, tmp_path: Path) -> None:
        # A build costs money and overwrites its workspace, so a repeated POST must not
        # silently redo it. This is the case a naive "is it running?" check lets through.
        harness = make_harness(tmp_path=tmp_path)
        async with harness.client() as client:
            session_id = await approved_session(client)
            await client.post(f"/builds/{session_id}")
            await _settle()
            refused = await client.post(f"/builds/{session_id}")
            assert refused.status_code == 409
            assert "force=true" in refused.json()["detail"]
            assert len(harness.build_calls) == 1

            forced = await client.post(f"/builds/{session_id}?force=true")
            await _settle()
        assert forced.status_code == 202
        assert len(harness.build_calls) == 2


async def _settle() -> None:
    """Let the background build task run to completion."""
    for _ in range(50):
        await asyncio.sleep(0)


class TestTranscript:
    async def test_restores_both_sides_of_every_turn(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.get(f"/sessions/{session_id}/messages")
        body = response.json()
        assert [m["role"] for m in body["messages"]] == ["you", "agent"]
        assert body["messages"][0]["text"] == "hello"
        assert "That covers everything" in body["messages"][1]["text"]
        assert body["messages"][0]["turn"] == 1

    async def test_a_reviewers_send_back_note_never_appears(self, harness: Harness) -> None:
        # The note is the `user_text` of its turn, but the requester never saw it. Only the
        # agent's reply — written to stand on its own — belongs in their transcript.
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(
                f"/reviews/{session_id}/decision",
                json={"action": "send_back", "reviewer": "dana", "note": "Which HRIS exactly?"},
            )
            harness.clients[0].script.append(["One more thing — which system is it?"])
            await client.post(f"/sessions/{session_id}/resume")
            response = await client.get(f"/sessions/{session_id}/messages")
        body = response.json()
        assert "Which HRIS exactly?" not in response.text
        assert [m["role"] for m in body["messages"]] == ["you", "agent", "agent"]
        assert body["messages"][2]["text"] == "One more thing — which system is it?"

    async def test_a_closed_session_still_has_its_transcript(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.delete(f"/sessions/{session_id}")
            response = await client.get(f"/sessions/{session_id}/messages")
        assert len(response.json()["messages"]) == 2

    async def test_unknown_session_is_404(self, harness: Harness) -> None:
        async with harness.client() as client:
            response = await client.get(f"/sessions/{'b' * 36}/messages")
        assert response.status_code == 404


class TestFeedback:
    async def test_rating_a_turn_is_stored_and_read_back(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            posted = await client.post(
                f"/sessions/{session_id}/feedback",
                json={"turn": 1, "rating": "up", "comment": "exactly right"},
            )
            transcript = await client.get(f"/sessions/{session_id}/messages")
        assert posted.status_code == 204
        assert transcript.json()["feedback"] == {"1": "up"}
        stored = harness.store.load(session_id)["feedback"]
        assert stored[0]["rating"] == "up" and stored[0]["comment"] == "exactly right"

    async def test_changing_a_rating_appends_and_the_latest_wins(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            for rating in ("down", "up"):
                await client.post(
                    f"/sessions/{session_id}/feedback", json={"turn": 1, "rating": rating}
                )
            transcript = await client.get(f"/sessions/{session_id}/messages")
        assert transcript.json()["feedback"] == {"1": "up"}
        assert len(harness.store.load(session_id)["feedback"]) == 2  # the log keeps both

    async def test_feedback_survives_the_next_turn(self, harness: Harness) -> None:
        # The live session owns the snapshot, so a rating held only in the store would be
        # overwritten the next time a turn persisted.
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(f"/sessions/{session_id}/feedback", json={"turn": 1, "rating": "up"})
            harness.clients[0].script.append(["Anything else?"])
            await client.post(f"/sessions/{session_id}/messages", json={"text": "more"})
        assert harness.store.load(session_id)["feedback"][0]["turn"] == 1

    async def test_a_closed_session_can_still_be_rated(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.delete(f"/sessions/{session_id}")
            response = await client.post(
                f"/sessions/{session_id}/feedback", json={"turn": 1, "rating": "down"}
            )
        assert response.status_code == 204
        assert harness.store.load(session_id)["feedback"][0]["rating"] == "down"

    @pytest.mark.parametrize(
        "body",
        [
            {"turn": 0, "rating": "up"},  # not a real turn number
            {"turn": 1, "rating": "meh"},  # not a rating
            {"turn": 1},  # no rating at all
            {"turn": 1, "rating": "up", "comment": "x" * 2001},  # over the comment cap
        ],
    )
    async def test_invalid_feedback_is_rejected(
        self, harness: Harness, body: dict[str, Any]
    ) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.post(f"/sessions/{session_id}/feedback", json=body)
        assert response.status_code == 422

    async def test_cannot_rate_a_turn_that_has_not_happened(self, harness: Harness) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            response = await client.post(
                f"/sessions/{session_id}/feedback", json={"turn": 9, "rating": "up"}
            )
        assert response.status_code == 422
        assert "has not happened" in response.json()["detail"]


class TestAdminSessions:
    async def test_lists_incomplete_sessions_too(self, harness: Harness) -> None:
        # The review queue deliberately hides these; the admin view is where they show up.
        async with harness.client() as client:
            started = await start_session(client)
            response = await client.get("/admin/sessions")
        rows = response.json()
        assert [r["session_id"] for r in rows] == [started]
        assert rows[0]["is_complete"] is False and rows[0]["turn"] == 0
        assert rows[0]["live"] is True

    async def test_carries_the_technical_detail_the_requester_never_sees(
        self, harness: Harness
    ) -> None:
        async with harness.client() as client:
            session_id = await complete_canvas(client)
            await client.post(f"/sessions/{session_id}/feedback", json={"turn": 1, "rating": "up"})
            response = await client.get("/admin/sessions")
        row = next(r for r in response.json() if r["session_id"] == session_id)
        assert row["departments"] == ["hr"]
        assert row["feedback"] == {"1": "up"}
        assert row["review_status"] == "pending"
        assert row["build_status"] == "none"
        assert row["total_cost_usd"] > 0
        assert row["failure"] is None

    async def test_needs_the_admin_token(self) -> None:
        harness = make_harness(admin_token="s3cret")
        async with harness.client() as client:
            assert (await client.get("/admin/sessions")).status_code == 401
            ok = await client.get("/admin/sessions", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


class TestAdminEvals:
    async def test_lists_suites_and_runs(self, harness: Harness, tmp_path: Path) -> None:
        suite = tmp_path / "skill_matching" / "results"
        suite.mkdir(parents=True)
        (tmp_path / "skill_matching" / "run.py").write_text("#", encoding="utf-8")
        (suite / "20260914T015734Z.json").write_text(
            json.dumps({"summary": {"opus": {"exact_match": 0.95}}, "results": [{"case_id": "c"}]}),
            encoding="utf-8",
        )
        with patch.object(eval_runs, "EVALS_DIR", tmp_path):
            async with harness.client() as client:
                listed = await client.get("/admin/evals")
                one = await client.get("/admin/evals/skill_matching/20260914T015734Z")

        body = listed.json()["suites"]
        assert body[0]["suite"] == "skill_matching"
        assert body[0]["runs"][0]["summary"]["opus"]["exact_match"] == 0.95
        assert body[0]["runs"][0]["calls"] == 1
        assert one.json()["results"][0]["case_id"] == "c"  # full file, per-call results included

    async def test_missing_run_is_404(self, harness: Harness, tmp_path: Path) -> None:
        with patch.object(eval_runs, "EVALS_DIR", tmp_path):
            async with harness.client() as client:
                response = await client.get("/admin/evals/skill_matching/nope")
        assert response.status_code == 404

    async def test_a_traversal_attempt_is_400_not_a_file_read(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        with patch.object(eval_runs, "EVALS_DIR", tmp_path):
            async with harness.client() as client:
                response = await client.get("/admin/evals/skill_matching/..%2F..%2Fsecret")
        assert response.status_code in (400, 404)
