"""SOP document library: loading and deterministic keyword search.

The synthetic SOPs under ``sop_docs/`` stand in for a company's internal process documents.
Unlike department skills (a fixed taxonomy), these are *content*: they change and grow, and
the discovery agent looks them up rather than being prompted with them. The MCP server in
``sop_server.py`` exposes this module over stdio; keeping the logic here means it is testable
without spawning anything.

Search is plain keyword scoring: title hits weigh most, then summary, then body, with a bonus
for the whole query appearing as a phrase. Eight documents do not need embeddings, and a
deterministic ranker keeps tests and evals reproducible.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
SOP_DIR: Final = Path(os.environ.get("BLUEPRINT_SOP_DIR", _REPO_ROOT / "sop_docs"))

_FRONTMATTER_RE: Final = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_TOKEN_RE: Final = re.compile(r"[a-z0-9][a-z0-9\-]*")
_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")

# Words too common to carry meaning for ranking.
_STOPWORDS: Final = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "we",
        "our",
        "your",
        "you",
        "they",
        "their",
    ]
)

TITLE_WEIGHT: Final = 3.0
SUMMARY_WEIGHT: Final = 2.0
BODY_WEIGHT: Final = 1.0
PHRASE_BONUS: Final = 4.0
DEFAULT_LIMIT: Final = 3
MAX_LIMIT: Final = 5
SNIPPET_CHARS: Final = 300


class SopLoadError(ValueError):
    """A SOP file is malformed. The message names the file and the problem."""


@dataclass(frozen=True, slots=True)
class SopDocument:
    """One SOP, parsed and validated."""

    id: str
    title: str
    department: str
    owner: str
    last_updated: date
    summary: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable metadata (no body)."""
        return {
            "id": self.id,
            "title": self.title,
            "department": self.department,
            "owner": self.owner,
            "last_updated": self.last_updated.isoformat(),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class SopHit:
    """A search result: the document plus its score and a query-relevant snippet."""

    doc: SopDocument
    score: float
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {**self.doc.to_dict(), "score": round(self.score, 3), "snippet": self.snippet}


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens with stopwords removed."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def parse_sop(text: str, *, source: str = "<string>") -> SopDocument:
    """Parse one SOP markdown document.

    Raises:
        SopLoadError: on missing frontmatter, missing or malformed required fields.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SopLoadError(f"{source}: missing YAML frontmatter block")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SopLoadError(f"{source}: invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SopLoadError(f"{source}: frontmatter must be a mapping")

    def required(key: str) -> str:
        value = meta.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SopLoadError(f"{source}: frontmatter '{key}' is required")
        return value.strip()

    sop_id = required("id")
    if not _ID_RE.match(sop_id):
        raise SopLoadError(f"{source}: id must be lowercase letters, digits, hyphens: {sop_id!r}")

    raw_date = meta.get("last_updated")
    if isinstance(raw_date, date):
        last_updated = raw_date
    elif isinstance(raw_date, str):
        try:
            last_updated = date.fromisoformat(raw_date.strip())
        except ValueError as exc:
            raise SopLoadError(f"{source}: last_updated must be YYYY-MM-DD") from exc
    else:
        raise SopLoadError(f"{source}: frontmatter 'last_updated' is required")

    body = match.group(2).strip()
    if not body:
        raise SopLoadError(f"{source}: document body is empty")

    return SopDocument(
        id=sop_id,
        title=required("title"),
        department=required("department").lower(),
        owner=required("owner"),
        last_updated=last_updated,
        summary=required("summary"),
        body=body,
    )


def load_sops(root: Path = SOP_DIR) -> dict[str, SopDocument]:
    """Load every ``*.md`` under ``root``, keyed by id, sorted by filename for determinism.

    Raises:
        SopLoadError: if any file is malformed or two files declare the same id.
        FileNotFoundError: if ``root`` does not exist.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"SOP directory not found: {root}")
    docs: dict[str, SopDocument] = {}
    for path in sorted(root.glob("*.md")):
        doc = parse_sop(path.read_text(encoding="utf-8"), source=str(path))
        if doc.id in docs:
            raise SopLoadError(f"{path}: duplicate SOP id '{doc.id}'")
        docs[doc.id] = doc
    logger.info("sops.loaded", extra={"count": len(docs), "root": str(root)})
    return docs


def _snippet(body: str, terms: list[str]) -> str:
    """The first body paragraph mentioning any query term, trimmed; else the first paragraph."""
    paragraphs = [" ".join(p.split()) for p in body.split("\n\n") if p.strip()]
    chosen = next(
        (p for p in paragraphs if any(t in p.lower() for t in terms)),
        paragraphs[0] if paragraphs else "",
    )
    return chosen[:SNIPPET_CHARS]


def _doc_terms(doc: SopDocument) -> set[str]:
    return set(tokenize(doc.title + " " + doc.summary + " " + doc.body))


def term_rarity(docs: Mapping[str, SopDocument]) -> dict[str, float]:
    """Inverse document frequency per term: rare terms weigh more than ones in every SOP.

    ``log(1 + N / df)``: a term in every document scores ~0.69, a term in one of eight ~2.2.
    """
    n = max(len(docs), 1)
    df: dict[str, int] = {}
    for doc in docs.values():
        for term in _doc_terms(doc):
            df[term] = df.get(term, 0) + 1
    return {term: math.log(1 + n / count) for term, count in df.items()}


def score_document(
    doc: SopDocument, query: str, rarity: Mapping[str, float] | None = None
) -> float:
    """Deterministic relevance score; 0.0 means no term matched.

    Each matched term contributes its field weight (title > summary > body) scaled by its
    rarity across the library when ``rarity`` is given, plus a bonus if the whole query
    appears as a phrase.
    """
    terms = tokenize(query)
    if not terms:
        return 0.0
    title = set(tokenize(doc.title))
    summary = set(tokenize(doc.summary))
    body = set(tokenize(doc.body))
    score = 0.0
    for term in set(terms):
        weight = rarity.get(term, 1.0) if rarity is not None else 1.0
        if term in title:
            score += TITLE_WEIGHT * weight
        if term in summary:
            score += SUMMARY_WEIGHT * weight
        if term in body:
            score += BODY_WEIGHT * weight
    phrase = " ".join(terms)
    if len(terms) > 1 and phrase in " ".join(tokenize(doc.title + " " + doc.body)):
        score += PHRASE_BONUS
    return score


def search_sops(
    docs: dict[str, SopDocument],
    query: str,
    *,
    department: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SopHit]:
    """Rank documents for ``query``; optionally restrict to one department.

    Returns at most ``limit`` hits (capped at ``MAX_LIMIT``) with a positive score, best first.
    Ties break on document id so results are stable.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    dept = department.strip().lower() if department else None
    terms = tokenize(query)
    rarity = term_rarity(docs)
    hits = [
        SopHit(doc=d, score=s, snippet=_snippet(d.body, terms))
        for d in docs.values()
        if (dept is None or d.department == dept) and (s := score_document(d, query, rarity)) > 0
    ]
    hits.sort(key=lambda h: (-h.score, h.doc.id))
    return hits[:limit]
