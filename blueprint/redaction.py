"""Strip identifiers out of stakeholder text before it is stored.

Everything in this project is synthetic, but the shape of the problem is not: a discovery
conversation is someone describing their work, and people put phone numbers, email addresses
and account numbers into that description without thinking about it. The snapshot is kept
forever and read by a reviewer and an admin, so the identifiers should not be in it.

Deliberately **not** a PII detector. It recognises a short list of things that have a
mechanical shape, replaces each with a label saying what was removed, and leaves everything
else alone. A name in prose survives, because a regex that tried to catch names would either
miss most of them or shred the conversation. What this buys is that the obvious, high-regret
identifiers never reach disk; what it does not buy is a guarantee, and the difference is
recorded here rather than implied.

The redaction happens on the way *in* to the store. There is no un-redact: the original is
never written, so a leak of the database is a leak of redacted text.
"""

from __future__ import annotations

import re
from typing import Any, Final, cast

# What JSON actually is. Used *inside* the walker only: a fully recursive alias is
# accurate but miserable at call sites, because every caller then has to narrow a union
# before it can index the result. The public functions below take and return the shape
# callers actually hold — a snapshot dict — and the precision stays where it helps.
type Json = str | int | float | bool | list["Json"] | dict[str, "Json"] | None

# Ordered: a more specific pattern must win before a looser one sees the same characters.
# (An SSN would otherwise be eaten by the long-digit-run rule and labelled less precisely.)
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # US-shaped phone numbers, with or without punctuation and country code.
    (
        "phone",
        re.compile(r"(?<![\w-])(?:\+1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?![\w-])"),
    ),
    ("card", re.compile(r"(?<![\w-])(?:\d{4}[ -]){3}\d{4}(?![\w-])")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"')]+")),
    # A bare run of 9+ digits: account, routing, employee id. Short runs are left alone
    # because "30 to 40 new hires" and "2026" are the substance of these conversations.
    ("number", re.compile(r"(?<![\w.-])\d{9,}(?![\w.-])")),
)

REDACTED: Final = "[redacted:{kind}]"


def redact_text(text: str) -> str:
    """Replace recognisable identifiers in one string."""
    for kind, pattern in _PATTERNS:
        text = pattern.sub(REDACTED.format(kind=kind), text)
    return text


# Which fields of a snapshot hold words a human typed or the model wrote back. Redaction is
# applied to these and nothing else: cost figures, category names and tool names are ours.
_TEXT_FIELDS: Final = frozenset(
    {
        "user_text",
        "assistant_text",
        "summary",
        "note",
        "comment",
        "narrative",
        "title",
        "rationale",
        "evidence",
        "question",
        "report",
        "test_output",
        "query",
    }
)


def _walk(value: Json, fields: frozenset[str]) -> Json:
    """Recurse through decoded JSON, redacting the fields named in ``fields``."""
    if isinstance(value, dict):
        out: dict[str, Json] = {}
        for key, item in value.items():
            if key in fields and isinstance(item, str):
                out[key] = redact_text(item)
            elif key == "current" and isinstance(item, dict):
                # `canvas.current` is {category_name: summary}, so the free text is in the
                # values and the field-name rule above cannot see it.
                out[key] = {
                    cat: (redact_text(v) if isinstance(v, str) else v) for cat, v in item.items()
                }
            else:
                out[key] = _walk(item, fields)
        return out
    if isinstance(value, list):
        return [_walk(item, fields) for item in value]
    return value


def redact_snapshot(
    snapshot: dict[str, Any], *, fields: frozenset[str] = _TEXT_FIELDS
) -> dict[str, Any]:
    """Redact every free-text field in a snapshot, leaving its structure untouched.

    Works on the JSON shape rather than on the dataclasses, so a new field that happens to be
    called ``note`` is covered the day it is added, and a field that is not free text is never
    touched by accident. The input is not mutated.
    """
    walked = _walk(cast("Json", snapshot), fields)
    return cast("dict[str, Any]", walked)
