"""Scoped web search for regulatory grounding, plus the hook that caps it.

Purpose is narrow on purpose: when a stakeholder mentions a government or regulatory
requirement (I-9 timing, FMLA, state withholding), one search lets the discovery agent ask a
sharper follow-up. It is never used to answer the stakeholder's questions.

Safety properties live in code, not prose:
    - a fixed domain allowlist is sent to the provider *and* re-checked on the response,
    - result count and snippet length are capped,
    - the API key appears only in the request header and is never logged or returned,
    - a ``PreToolUse`` hook caps calls per session and records every attempt.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import HookContext, HookInput, HookMatcher, SdkMcpTool, tool
from claude_agent_sdk.types import HookJSONOutput, PreToolUseHookSpecificOutput, SyncHookJSONOutput

logger = logging.getLogger(__name__)

TOOL_NAME: Final = "web_search"
TAVILY_URL: Final = "https://api.tavily.com/search"

# Federal sources for the regulatory topics discovery conversations actually hit. State sites
# are added per deployment via settings rather than guessed here.
DEFAULT_ALLOWED_DOMAINS: Final[tuple[str, ...]] = (
    "uscis.gov",
    "dol.gov",
    "irs.gov",
    "eeoc.gov",
    "ssa.gov",
    "osha.gov",
    "nlrb.gov",
    "hhs.gov",
    "cms.gov",
    "sec.gov",
    "ftc.gov",
    "gsa.gov",
)
DEFAULT_MAX_RESULTS: Final = 3
DEFAULT_MAX_SEARCHES_PER_SESSION: Final = 3
SNIPPET_CHARS: Final = 600
REQUEST_TIMEOUT_S: Final = 15.0

TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 3,
            "maxLength": 200,
            "description": (
                "A generic public-topic query, e.g. 'Form I-9 Section 2 completion deadline'. "
                "Never include names, company details, or the stakeholder's own words."
            ),
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One search hit, already trimmed and allowlist-checked."""

    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SearchError(RuntimeError):
    """The provider could not be reached or returned an unusable response."""


class SearchClient(Protocol):
    """What the tool needs from a search backend; ``TavilyClient`` or a test fake."""

    async def search(self, query: str) -> list[SearchResult]:
        """Run one search; results are already scoped and trimmed."""
        ...


def _host_allowed(url: str, allowed: Sequence[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


class TavilyClient:
    """Tavily search, constrained to an allowlist with capped, trimmed results."""

    def __init__(
        self,
        api_key: str,
        *,
        allowed_domains: Sequence[str] = DEFAULT_ALLOWED_DOMAINS,
        max_results: int = DEFAULT_MAX_RESULTS,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Tavily API key is empty")
        if not allowed_domains:
            raise ValueError("allowed_domains must not be empty; unscoped search is not allowed")
        self._api_key = api_key.strip()
        self.allowed_domains = tuple(d.lower() for d in allowed_domains)
        self.max_results = max_results
        self._http = http or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    async def search(self, query: str) -> list[SearchResult]:
        """Query Tavily; filter to the allowlist; trim snippets.

        Raises:
            SearchError: on transport failure, non-2xx status, or a malformed body. The
                message never includes the key or the raw response.
        """
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": self.max_results,
            "include_domains": list(self.allowed_domains),
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            response = await self._http.post(
                TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"search provider unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SearchError(f"search provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
            raw = payload["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SearchError("search provider returned an unexpected body") from exc

        results: list[SearchResult] = []
        for item in raw:
            url = str(item.get("url", ""))
            if not _host_allowed(url, self.allowed_domains):
                logger.warning("websearch.dropped_off_allowlist", extra={"url": url})
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title", "")).strip()[:200],
                    url=url,
                    snippet=" ".join(str(item.get("content", "")).split())[:SNIPPET_CHARS],
                )
            )
            if len(results) >= self.max_results:
                break
        return results

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self._http.aclose()


# -- the tool ---------------------------------------------------------------------------------


def build_web_search_tool(client: SearchClient) -> SdkMcpTool[Any]:
    """Create the ``web_search`` tool bound to a scoped search client."""

    @tool(
        TOOL_NAME,
        "Search authoritative public (government/regulatory) sources for a requirement the "
        "stakeholder mentioned, so you can ask a sharper follow-up. Not for answering their "
        "questions. Queries must be generic and contain nothing the stakeholder said.",
        TOOL_SCHEMA,
    )
    async def web_search(args: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(str(args["query"]).split())
        try:
            results = await client.search(query)
        except SearchError as exc:
            logger.error("websearch.failed", extra={"query": query, "error": str(exc)})
            return {
                "content": [
                    {"type": "text", "text": f"Search unavailable ({exc}). Continue without it."}
                ],
                "is_error": True,
            }

        logger.info(
            "websearch.ok",
            extra={"query": query, "results": len(results), "urls": [r.url for r in results]},
        )
        if not results:
            text = "No results from allowed sources. Continue without it."
            return {"content": [{"type": "text", "text": text}]}
        lines = [
            f"{i}. {r.title}\n   {r.url}\n   {r.snippet}" for i, r in enumerate(results, start=1)
        ]
        text = (
            "Public sources (use to sharpen your next question; cite the source title if you "
            "rely on it; do not present this as advice):\n\n" + "\n\n".join(lines)
        )
        return {"content": [{"type": "text", "text": text}]}

    return web_search


# -- the guard: a PreToolUse hook -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchAttempt:
    """One attempt to call ``web_search``, allowed or denied, for the trace."""

    query: str
    allowed: bool
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {"query": self.query, "allowed": self.allowed, "at": self.at.isoformat()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchAttempt:
        """Inverse of :meth:`to_dict`."""
        return cls(
            query=str(data["query"]),
            allowed=bool(data["allowed"]),
            at=datetime.fromisoformat(data["at"]),
        )


@dataclass(slots=True)
class WebSearchGuard:
    """Caps ``web_search`` calls per session and records every attempt.

    Implemented as a ``PreToolUse`` hook: the SDK calls :meth:`hook` before each matching tool
    call; returning a deny decision stops the call and shows the reason to the model. The tool
    itself never knows the guard exists, which is the point of a hook.
    """

    max_calls: int = DEFAULT_MAX_SEARCHES_PER_SESSION
    attempts: list[SearchAttempt] = field(default_factory=list)

    @property
    def allowed_calls(self) -> int:
        """How many searches have been permitted so far."""
        return sum(1 for a in self.attempts if a.allowed)

    async def hook(
        self, input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """The ``PreToolUse`` callback. Allows until the cap, then denies with a reason."""
        tool_input = input_data.get("tool_input")
        query = str(tool_input.get("query", "")) if isinstance(tool_input, dict) else ""
        if self.allowed_calls >= self.max_calls:
            self.attempts.append(SearchAttempt(query=query, allowed=False))
            logger.warning("websearch.denied", extra={"query": query, "max_calls": self.max_calls})
            decision: PreToolUseHookSpecificOutput = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"The web search budget for this conversation ({self.max_calls}) is used "
                    "up. Continue the discovery without searching."
                ),
            }
            denied: SyncHookJSONOutput = {"hookSpecificOutput": decision}
            return denied
        self.attempts.append(SearchAttempt(query=query, allowed=True))
        logger.info(
            "websearch.allowed",
            extra={"query": query, "used": self.allowed_calls, "max_calls": self.max_calls},
        )
        allowed: SyncHookJSONOutput = {}
        return allowed

    def matcher(self, tool_full_name: str) -> HookMatcher:
        """The ``HookMatcher`` to register under ``hooks["PreToolUse"]``."""
        return HookMatcher(matcher=tool_full_name, hooks=[self.hook])

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for the session snapshot."""
        return {"max_calls": self.max_calls, "attempts": [a.to_dict() for a in self.attempts]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WebSearchGuard:
        """Inverse of :meth:`to_dict`; the budget already used carries over."""
        return cls(
            max_calls=int(data["max_calls"]),
            attempts=[SearchAttempt.from_dict(a) for a in data["attempts"]],
        )
