import sys
from datetime import date
from pathlib import Path

import pytest

from blueprint.sops import (
    MAX_LIMIT,
    SOP_DIR,
    SopLoadError,
    load_sops,
    parse_sop,
    score_document,
    search_sops,
    tokenize,
)

VALID = """---
id: test-doc
title: Widget Return Procedure
department: finance
owner: AP
last_updated: 2026-01-05
summary: How returned widgets are credited.
---

# Widget Return Procedure

Step one: log the return in the ERP.

Step two: issue the credit memo within 5 business days.
"""


class TestParse:
    def test_parses_all_fields(self) -> None:
        doc = parse_sop(VALID)
        assert doc.id == "test-doc"
        assert doc.title == "Widget Return Procedure"
        assert doc.department == "finance"
        assert doc.last_updated == date(2026, 1, 5)
        assert doc.body.startswith("# Widget Return Procedure")
        assert "body" not in doc.to_dict()  # metadata only

    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            (lambda s: s.replace("id: test-doc\n", ""), "'id' is required"),
            (lambda s: s.replace("id: test-doc", "id: Bad_ID"), "lowercase"),
            (lambda s: s.replace("last_updated: 2026-01-05", "last_updated: soon"), "YYYY-MM-DD"),
            (lambda s: s.replace("last_updated: 2026-01-05\n", ""), "'last_updated' is required"),
            (lambda s: s.split("---\n\n")[0] + "---\n\n", "body is empty"),
            (lambda s: s.replace("---\nid", "id", 1), "missing YAML frontmatter"),
        ],
    )
    def test_rejects_malformed(self, mutation: object, message: str) -> None:
        with pytest.raises(SopLoadError, match=message):
            parse_sop(mutation(VALID), source="x.md")  # type: ignore[operator]

    def test_source_in_message(self) -> None:
        with pytest.raises(SopLoadError, match=r"^x\.md:"):
            parse_sop("nope", source="x.md")


class TestLoad:
    def test_missing_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_sops(tmp_path / "nope")

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text(VALID, encoding="utf-8")
        (tmp_path / "b.md").write_text(VALID, encoding="utf-8")
        with pytest.raises(SopLoadError, match="duplicate SOP id"):
            load_sops(tmp_path)

    def test_shipped_library_loads(self) -> None:
        docs = load_sops(SOP_DIR)
        assert len(docs) == 8
        by_dept: dict[str, int] = {}
        for d in docs.values():
            by_dept[d.department] = by_dept.get(d.department, 0) + 1
        assert by_dept == {"hr": 2, "finance": 2, "sales": 2, "customer_success": 2}


class TestSearch:
    def test_tokenize_drops_stopwords_and_lowercases(self) -> None:
        assert tokenize("The I-9 Section 2 deadline") == ["i-9", "section", "2", "deadline"]

    def test_title_outranks_body(self) -> None:
        docs = load_sops(SOP_DIR)
        hits = search_sops(docs, "expense report")
        assert hits[0].doc.id == "fin-expense-report-review"

    def test_department_filter(self) -> None:
        docs = load_sops(SOP_DIR)
        assert all(h.doc.department == "hr" for h in search_sops(docs, "review", department="hr"))
        assert search_sops(docs, "invoice", department="sales") == []

    def test_no_match_returns_empty(self) -> None:
        docs = load_sops(SOP_DIR)
        assert search_sops(docs, "zebra quantum") == []
        assert search_sops(docs, "the and of") == []  # all stopwords

    def test_limit_is_capped_and_ties_are_stable(self) -> None:
        docs = load_sops(SOP_DIR)
        hits = search_sops(docs, "systems", limit=99)
        assert len(hits) <= MAX_LIMIT
        assert [h.doc.id for h in hits] == sorted(
            (h.doc.id for h in hits),
            key=lambda i: (-next(x.score for x in hits if x.doc.id == i), i),
        )

    def test_phrase_bonus(self) -> None:
        doc = parse_sop(VALID)
        assert score_document(doc, "credit memo") > score_document(doc, "memo credit")

    def test_rare_terms_outweigh_generic_ones(self) -> None:
        # "business days" appears in nearly every SOP; "i-9" in one. A realistic query must
        # rank the packet SOP first even though the generic words also match elsewhere.
        docs = load_sops(SOP_DIR)
        hits = search_sops(docs, "I-9 Section 2 completion within three business days")
        assert hits[0].doc.id == "hr-onboarding-packet-review"

    def test_term_rarity_is_idf_shaped(self) -> None:
        from blueprint.sops import term_rarity

        docs = load_sops(SOP_DIR)
        rarity = term_rarity(docs)
        assert rarity["i-9"] > rarity["business"] > 0

    def test_snippet_targets_the_matching_paragraph(self) -> None:
        docs = load_sops(SOP_DIR)
        hit = search_sops(docs, "I-9 Section 2")[0]
        assert hit.doc.id == "hr-onboarding-packet-review"
        assert "Section 2" in hit.snippet


class TestStdioServer:
    """Spawn the real server over stdio, as the SDK will, and speak MCP to it."""

    async def test_end_to_end_over_stdio(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "blueprint.sop_server"],
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "PYTHONIOENCODING": "utf-8",
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == {"search_sops", "read_sop", "list_sops"}

            found = await session.call_tool(
                "search_sops", {"query": "onboarding packet", "department": "hr"}
            )
            text = found.content[0].text  # type: ignore[union-attr]
            assert "[hr-onboarding-packet-review]" in text

            doc = await session.call_tool("read_sop", {"sop_id": "hr-onboarding-packet-review"})
            body = doc.content[0].text  # type: ignore[union-attr]
            assert "3 business days" in body and "# New Hire Onboarding Packet Review" in body

            missing = await session.call_tool("read_sop", {"sop_id": "nope"})
            assert "No SOP with id" in missing.content[0].text  # type: ignore[union-attr]
