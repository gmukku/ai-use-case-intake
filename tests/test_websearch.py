from typing import Any

import httpx
import jsonschema
import pytest
from claude_agent_sdk import HookContext

from blueprint.websearch import (
    DEFAULT_ALLOWED_DOMAINS,
    SNIPPET_CHARS,
    TOOL_SCHEMA,
    SearchError,
    SearchResult,
    TavilyClient,
    WebSearchGuard,
    build_web_search_tool,
)


class FakeSearchClient:
    def __init__(self, results: list[SearchResult] | None = None, *, fail: bool = False) -> None:
        self.results = results or []
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        if self.fail:
            raise SearchError("search provider returned HTTP 503")
        return self.results


def tavily_transport(status: int = 200, results: list[dict[str, Any]] | None = None) -> Any:
    """An httpx MockTransport that plays Tavily and records the request it received."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"detail": "nope"})
        return httpx.Response(200, json={"results": results or []})

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


class TestSchema:
    def test_bounds(self) -> None:
        jsonschema.validate({"query": "Form I-9 Section 2 deadline"}, TOOL_SCHEMA)
        for bad in ({"query": "ab"}, {"query": "x" * 201}, {}, {"query": "ok", "domain": "x"}):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, TOOL_SCHEMA)


class TestTavilyClient:
    def test_rejects_empty_key_and_empty_allowlist(self) -> None:
        with pytest.raises(ValueError, match="key"):
            TavilyClient("  ")
        with pytest.raises(ValueError, match="allowed_domains"):
            TavilyClient("tvly-x", allowed_domains=[])

    async def test_sends_allowlist_and_bearer_and_trims(self) -> None:
        transport = tavily_transport(
            results=[
                {
                    "title": "  USCIS I-9  ",
                    "url": "https://www.uscis.gov/i-9",
                    "content": "a " * 1000,
                },
                {"title": "Blog", "url": "https://example.com/i9", "content": "not allowed"},
                {"title": "DOL", "url": "https://dol.gov/x", "content": "ok"},
            ]
        )
        client = TavilyClient("tvly-secret", http=httpx.AsyncClient(transport=transport))
        results = await client.search("I-9 deadline")

        req = transport.seen[0]
        assert req.headers["authorization"] == "Bearer tvly-secret"
        body = req.read()
        assert b'"include_domains"' in body and b"uscis.gov" in body
        assert b"tvly-secret" not in body  # key only in the header

        assert [r.url for r in results] == ["https://www.uscis.gov/i-9", "https://dol.gov/x"]
        assert results[0].title == "USCIS I-9"
        assert len(results[0].snippet) == SNIPPET_CHARS  # trimmed, whitespace-collapsed

    async def test_caps_result_count(self) -> None:
        many = [{"title": f"t{i}", "url": f"https://irs.gov/{i}", "content": "x"} for i in range(9)]
        http = httpx.AsyncClient(transport=tavily_transport(results=many))
        client = TavilyClient("tvly-x", max_results=2, http=http)
        assert len(await client.search("q")) == 2

    async def test_http_error_becomes_search_error_without_leaking(self) -> None:
        client = TavilyClient(
            "tvly-secret", http=httpx.AsyncClient(transport=tavily_transport(status=503))
        )
        with pytest.raises(SearchError) as exc:
            await client.search("q")
        assert "503" in str(exc.value) and "tvly-secret" not in str(exc.value)

    async def test_transport_failure_becomes_search_error(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no network")

        client = TavilyClient("tvly-x", http=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
        with pytest.raises(SearchError, match="unreachable"):
            await client.search("q")

    def test_default_allowlist_is_government_only(self) -> None:
        assert all(d.endswith(".gov") for d in DEFAULT_ALLOWED_DOMAINS)


class TestTool:
    async def test_formats_results_with_guidance(self) -> None:
        hit = SearchResult(title="USCIS I-9", url="https://uscis.gov/i-9", snippet="3 days")
        fake = FakeSearchClient([hit])
        tool = build_web_search_tool(fake)
        out = await tool.handler({"query": "  Form   I-9   deadline "})
        text = out["content"][0]["text"]
        assert fake.queries == ["Form I-9 deadline"]  # whitespace normalized
        assert "1. USCIS I-9" in text and "https://uscis.gov/i-9" in text
        assert "do not present this as advice" in text
        assert "is_error" not in out

    async def test_no_results(self) -> None:
        out = await build_web_search_tool(FakeSearchClient()).handler({"query": "anything here"})
        assert "No results" in out["content"][0]["text"]

    async def test_provider_failure_is_an_actionable_tool_error(self) -> None:
        out = await build_web_search_tool(FakeSearchClient(fail=True)).handler({"query": "abc"})
        assert out["is_error"] is True
        assert "Continue without it" in out["content"][0]["text"]


def pre_tool_use(query: str) -> Any:
    """The input dict the SDK passes to a PreToolUse hook (only the fields we read)."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__canvas__web_search",
        "tool_input": {"query": query},
        "session_id": "s",
        "transcript_path": "",
        "cwd": "",
        "permission_mode": "default",
    }


class TestGuardHook:
    async def test_allows_until_cap_then_denies_with_reason(self) -> None:
        guard = WebSearchGuard(max_calls=2)
        ctx = HookContext(signal=None)
        assert await guard.hook(pre_tool_use("q1"), "tu1", ctx) == {}
        assert await guard.hook(pre_tool_use("q2"), "tu2", ctx) == {}
        denied: dict[str, Any] = dict(await guard.hook(pre_tool_use("q3"), "tu3", ctx))
        spec = denied["hookSpecificOutput"]
        assert spec["permissionDecision"] == "deny"
        assert "used up" in spec["permissionDecisionReason"]
        assert guard.allowed_calls == 2
        assert [(a.query, a.allowed) for a in guard.attempts] == [
            ("q1", True),
            ("q2", True),
            ("q3", False),
        ]

    async def test_denied_attempts_do_not_consume_budget(self) -> None:
        guard = WebSearchGuard(max_calls=1)
        ctx = HookContext(signal=None)
        await guard.hook(pre_tool_use("a"), None, ctx)
        for _ in range(3):
            await guard.hook(pre_tool_use("b"), None, ctx)
        assert guard.allowed_calls == 1 and len(guard.attempts) == 4

    def test_matcher_targets_only_the_search_tool(self) -> None:
        m = WebSearchGuard().matcher("mcp__canvas__web_search")
        assert m.matcher == "mcp__canvas__web_search"
        assert m.hooks == [m.hooks[0]] and callable(m.hooks[0])

    def test_to_dict(self) -> None:
        d = WebSearchGuard(max_calls=3).to_dict()
        assert d == {"max_calls": 3, "attempts": []}
