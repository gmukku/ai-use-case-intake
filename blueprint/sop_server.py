"""SOP library as a standalone MCP server over stdio.

Run with ``python -m blueprint.sop_server``. The discovery session spawns it through the
SDK's ``mcp_servers`` stdio config, the same way any external MCP server (a CRM, an HRIS, a
document store) would be attached. All logic lives in ``sops.py``; this module only maps
tools to it.

Two rules for any stdio MCP server:
    1. **Never write to stdout.** It is the protocol channel; a stray ``print`` corrupts the
       stream. All diagnostics go to stderr, which the SDK surfaces separately.
    2. Return text the model can use directly. The client (the discovery agent) does not
       parse JSON out of tool results; it reads them.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from mcp.server.mcpserver import MCPServer

from blueprint.sops import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SOP_DIR,
    SopDocument,
    load_sops,
    search_sops,
)

SERVER_NAME: Final = "sop"
SEARCH_TOOL: Final = "search_sops"
READ_TOOL: Final = "read_sop"
LIST_TOOL: Final = "list_sops"

logger = logging.getLogger(__name__)


def build_server(docs: dict[str, SopDocument]) -> MCPServer:
    """Create the MCP server exposing ``docs``. Separated from ``main`` for tests."""
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Northbridge Group's internal SOP library (synthetic). Search by topic, then read "
            "a document by id for the full procedure."
        ),
    )

    @server.tool(
        name=SEARCH_TOOL,
        description=(
            "Search the company's internal SOP library by topic (e.g. 'onboarding packet', "
            "'expense receipts', 'ticket escalation'). Optionally restrict to a department: "
            "hr, finance, sales, customer_success. Returns up to a few matches with id, "
            "title, and a relevant snippet."
        ),
    )
    def search(query: str, department: str | None = None, limit: int = DEFAULT_LIMIT) -> str:
        query = " ".join(query.split())
        if len(query) < 3:
            return "Query too short; give a topic of at least 3 characters."
        hits = search_sops(docs, query, department=department, limit=min(limit, MAX_LIMIT))
        logger.info(
            "sop.search",
            extra={"query": query, "department": department, "hits": [h.doc.id for h in hits]},
        )
        if not hits:
            scope = f" in department '{department}'" if department else ""
            return f"No SOPs match '{query}'{scope}."
        lines = [
            f"{i}. [{h.doc.id}] {h.doc.title} ({h.doc.department}; updated "
            f"{h.doc.last_updated.isoformat()})\n   {h.snippet}"
            for i, h in enumerate(hits, start=1)
        ]
        return f"{len(hits)} SOP(s) for '{query}':\n\n" + "\n\n".join(lines)

    @server.tool(
        name=READ_TOOL,
        description="Read the full text of one SOP by its id (from search_sops or list_sops).",
    )
    def read(sop_id: str) -> str:
        doc = docs.get(sop_id.strip().lower())
        logger.info("sop.read", extra={"sop_id": sop_id, "found": doc is not None})
        if doc is None:
            known = ", ".join(sorted(docs))
            return f"No SOP with id '{sop_id}'. Known ids: {known}"
        header = (
            f"# {doc.title}\n"
            f"id: {doc.id} | department: {doc.department} | owner: {doc.owner} | "
            f"updated: {doc.last_updated.isoformat()}\n\n"
        )
        return header + doc.body

    @server.tool(
        name=LIST_TOOL,
        description="List every SOP in the library with id, title, and department.",
    )
    def list_all() -> str:
        rows = [f"- [{d.id}] {d.title} ({d.department})" for d in docs.values()]
        return f"{len(rows)} SOP(s):\n" + "\n".join(rows)

    return server


def main() -> None:
    """Load the library and serve it over stdio until the client disconnects."""
    # stderr only: stdout is the MCP transport.
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    docs = load_sops(SOP_DIR)
    logger.info("sop.server.start", extra={"count": len(docs), "dir": str(SOP_DIR)})
    build_server(docs).run(transport="stdio")


if __name__ == "__main__":
    main()
