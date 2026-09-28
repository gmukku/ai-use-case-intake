"""The HITL gate: compiled spec, rule-based risk assessment, and the review decision log.

Everything here operates on a session *snapshot* (``DiscoverySession.to_dict()``), not a live
session, because review happens after the conversation ended and possibly hours later. That
also makes it pure data in, pure data out: fully testable, and the same code path for a run
loaded from disk or from the audit store.

Risk is assessed by rules with evidence attached to every flag. A reviewer sees *why* a spec
was flagged ("matched 'ADP' in system_integrations"), which is what a badge with no reason
cannot give, and the false-negative eval can measure exactly these rules.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import jsonschema
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, Message, ResultMessage, query

from blueprint.canvas import CanvasCategory
from blueprint.isolation import AGENT_ENV
from blueprint.skills import DepartmentSkill

logger = logging.getLogger(__name__)

DEFAULT_SPEC_MODEL: Final = "claude-opus-5"
QueryFn = Callable[..., AsyncIterator[Message]]


# -- risk -----------------------------------------------------------------------------------


class RiskLevel(StrEnum):
    """Ordered: ``low < attention < elevated < high``."""

    LOW = "low"
    ATTENTION = "attention"
    ELEVATED = "elevated"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """Position in the ordering, for ``max``."""
        return ["low", "attention", "elevated", "high"].index(self.value)


@dataclass(frozen=True, slots=True)
class RiskFlag:
    """One triggered rule, with the evidence a reviewer needs to judge it."""

    key: str
    level: RiskLevel
    category: CanvasCategory | None
    evidence: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "key": self.key,
            "level": self.level.value,
            "category": self.category.value if self.category else None,
            "evidence": self.evidence,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """All triggered flags plus the overall level (the highest flag; ``low`` if none)."""

    level: RiskLevel
    flags: tuple[RiskFlag, ...]

    @property
    def needs_extra_scrutiny(self) -> bool:
        """True at ``elevated`` or above: the reviewer should look, not skim."""
        return self.level.rank >= RiskLevel.ELEVATED.rank

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "level": self.level.value,
            "needs_extra_scrutiny": self.needs_extra_scrutiny,
            "flags": [f.to_dict() for f in self.flags],
        }


# Generic system types that count as a named integration even without a product name.
_GENERIC_SYSTEMS: Final = (
    "hris",
    "hcm",
    "erp",
    "crm",
    "ats",
    "payroll system",
    "help desk",
    "helpdesk",
    "ticketing",
    "sharepoint",
    "onboarding tool",
    "expense tool",
    "general ledger",
)
_NONE_RE: Final = re.compile(
    r"\b(no|none|not any|does not touch|doesn't touch|no external|no other) "
    r"(systems?|integrations?|tools?|apps?|sources?)\b|\bnone\b",
    re.IGNORECASE,
)
# A write is one of these verbs followed, inside the same clause, by something that is a
# system of record. The verbs are fixed; the targets are deliberately NOT a literal list.
# An eval run found that the hand-written one had drifted until 74 of the systems the
# department skills name (BambooHR, Greenhouse, QuickBooks, ServiceNow…) and 8 of the generic
# types in _GENERIC_SYSTEMS were invisible here, while `named_integration` matched them all.
# The targets now come from those same two sources, so the two rules cannot diverge again.
_WRITE_VERB_RE: Final = re.compile(
    r"\b(writ(?:e|es|ing)|updat(?:e|es|ing)|push(?:es|ing)?|sync(?:s|ing)?|post(?:s|ing)?|"
    r"enter(?:s|ing)?|creat(?:e|es|ing)|modif(?:y|ies|ying)|submit(?:s|ting)?)\b",
    re.IGNORECASE,
)
# How far past the verb a target still reads as its object. A clause break ends the search:
# "write the summary. Records live in X" is not a write into X.
_WRITE_WINDOW: Final = 70
# Systems of record that name no product.
_WRITE_BASE_TARGETS: Final = ("system", "record", "records", "database", "ledger")
_NEGATION_RE: Final = re.compile(
    r"\b(not|no|never|without|out of scope|excluded|shouldn't|should not|won't|will not|"
    r"doesn't|does not|don't|do not|isn't|is not|explicitly out)\b",
    re.IGNORECASE,
)
# "automatically route it" and "route it automatically" are the same request. The original
# pattern only matched the first word order, so an eval case phrased the second way was missed.
_AUTOMATION_ACTS: Final = r"(?:send|email|route|trigger|notify|update|create|submit|assign|file)"
_AUTOMATION_RE: Final = re.compile(
    r"\bworkflow automation\b"
    rf"|\bautomat(?:e|es|ed|ically|ion)\b[^.;]{{0,40}}\b{_AUTOMATION_ACTS}\b"
    rf"|\b{_AUTOMATION_ACTS}\b[^.;]{{0,40}}\bautomat(?:e|es|ed|ically|ion)\b",
    re.IGNORECASE,
)
_SENSITIVE_RE: Final = re.compile(
    r"\b(ssn|social security|bank(?:ing)? (?:details?|account|info)|direct deposit|routing number|"
    r"account number|medical|health (?:record|condition|information)|diagnos\w*|salary|"
    r"compensation|pay rate|i-9|w-4|w-2|1099|passport|driver'?s licen[cs]e|date of birth|dob|"
    r"\bpii\b|personal (?:data|information)|id documents?)\b",
    re.IGNORECASE,
)
_EXTERNAL_RE: Final = re.compile(
    # `websites?` on its own: "the government website" is an external source however it is
    # phrased, and an eval case phrased it that way rather than as "external website".
    r"\b(public (?:web|internet|website)|the internet|external (?:web)?site|websites?|"
    r"web search|scrap(?:e|ing))\b",
    re.IGNORECASE,
)


# Skill bullets list these as channels, not systems of record; not worth a flag on their own.
_NOT_SYSTEMS: Final = frozenset({"email", "in-app chat", "outlook", "gmail", "bank portals"})


def _system_names(skills: Mapping[str, DepartmentSkill]) -> list[str]:
    """Product names from the skills' system-integration bullets, e.g. 'Workday', 'NetSuite'."""
    names: set[str] = set()
    for skill in skills.values():
        for bullet in skill.examples[CanvasCategory.SYSTEM_INTEGRATIONS]:
            _, _, rest = bullet.partition(":")
            for part in (rest or bullet).split(","):
                name = part.strip().split(" (")[0].strip()
                if (
                    2 < len(name) <= 30
                    and not name.lower().startswith(("and ", "or "))
                    and name.lower() not in _NOT_SYSTEMS
                ):
                    names.add(name)
    return sorted(names, key=len, reverse=True)


def _write_targets(skills: Mapping[str, DepartmentSkill]) -> tuple[str, ...]:
    """Everything that counts as a system of record: generic types, skill products, base nouns.

    Deliberately the same sources ``named_integration`` matches on, so a system that rule can
    see is never one this rule is blind to.
    """
    return (*_WRITE_BASE_TARGETS, *_GENERIC_SYSTEMS, *(n.lower() for n in _system_names(skills)))


def _write_evidence(
    text: str, verb_start: int, verb_end: int, targets: tuple[str, ...]
) -> str | None:
    """The verb through the system it acts on, or ``None`` if no system follows it.

    Returns the whole phrase rather than the bare verb so the reviewer sees "write them into
    BambooHR" instead of "write" — a flag without its evidence is just an accusation.
    """
    window = text[verb_end : verb_end + _WRITE_WINDOW]
    # A sentence or clause break ends the verb's reach: "write the summary. Records live in X"
    # is not a write into X.
    for stop in (".", ";"):
        cut = window.find(stop)
        if cut != -1:
            window = window[:cut]

    best: int | None = None
    for target in targets:
        match = re.search(rf"(?<![\w-]){re.escape(target)}(?![\w-])", window, re.IGNORECASE)
        if match and (best is None or match.end() < best):
            best = match.end()
    return text[verb_start : verb_end + best] if best is not None else None


def _negated(text: str, start: int) -> bool:
    """Is the match preceded (within ~8 words) by a negation? Handles 'out of scope: writing…'."""
    window = text[max(0, start - 80) : start]
    return _NEGATION_RE.search(window) is not None


def assess_risk(
    current: Mapping[str, str | None],
    *,
    open_gaps: Mapping[str, list[str]],
    web_searches: int,
    skills: Mapping[str, DepartmentSkill],
) -> RiskAssessment:
    """Apply the risk rules to a snapshot's current category summaries.

    Args:
        current: ``snapshot["canvas"]["current"]``: category value -> latest summary or None.
        open_gaps: ``snapshot["open_gaps"]``.
        web_searches: number of allowed web searches in the session.
        skills: loaded department skills; their system lists seed the integration matcher.
    """
    flags: list[RiskFlag] = []
    text_of = {CanvasCategory(k): (v or "") for k, v in current.items() if k}

    # named_integration: any system named in system_integrations, unless it says "none".
    integrations = text_of.get(CanvasCategory.SYSTEM_INTEGRATIONS, "")
    if integrations and not (_NONE_RE.search(integrations) and len(integrations) < 80):
        found: list[str] = []
        for name in _system_names(skills):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", integrations, re.IGNORECASE):
                found.append(name)
        for generic in _GENERIC_SYSTEMS:
            if re.search(rf"\b{re.escape(generic)}\b", integrations, re.IGNORECASE):
                found.append(generic)
        if found:
            seen = sorted({n.lower(): n for n in found}.values(), key=str.lower)
            flags.append(
                RiskFlag(
                    key="named_integration",
                    level=RiskLevel.ELEVATED,
                    category=CanvasCategory.SYSTEM_INTEGRATIONS,
                    evidence=", ".join(seen[:8]),
                    note="Named systems are involved; confirm the prototype only reads, and "
                    "that credentials and data access are understood.",
                )
            )

    # writes_to_system: language about writing/updating records into a system, not negated.
    write_targets = _write_targets(skills)
    for cat in (CanvasCategory.OUTPUT_FORMAT, CanvasCategory.SYSTEM_INTEGRATIONS):
        text = text_of.get(cat, "")
        for m in _WRITE_VERB_RE.finditer(text):
            if _negated(text, m.start()):
                continue
            evidence = _write_evidence(text, m.start(), m.end(), write_targets)
            if evidence is None:
                continue
            flags.append(
                RiskFlag(
                    key="writes_to_system",
                    level=RiskLevel.HIGH,
                    category=cat,
                    evidence=evidence,
                    note="The request implies writing to a system of record. A prototype must "
                    "not do this; confirm scope is read-only or human-submitted.",
                )
            )
            break

    # workflow_automation: automated actions with side effects (emails, routing, updates).
    output = text_of.get(CanvasCategory.OUTPUT_FORMAT, "")
    m_auto = _AUTOMATION_RE.search(output)
    if m_auto and not _negated(output, m_auto.start()):
        flags.append(
            RiskFlag(
                key="workflow_automation",
                level=RiskLevel.ELEVATED,
                category=CanvasCategory.OUTPUT_FORMAT,
                evidence=m_auto.group(0),
                note="Automated actions have side effects; confirm a human stays in the loop.",
            )
        )

    # sensitive_data: anywhere in the summaries.
    hits: list[str] = []
    for text in text_of.values():
        for m in _SENSITIVE_RE.finditer(text):
            hits.append(m.group(0).lower())
    if hits:
        flags.append(
            RiskFlag(
                key="sensitive_data",
                level=RiskLevel.ELEVATED,
                category=None,
                evidence=", ".join(sorted(set(hits))[:8]),
                note="Personal or financial data is in scope; synthetic samples only for the "
                "prototype, and confirm handling rules before anything real is used.",
            )
        )

    # external_sources: public web as an input, or web search used in discovery.
    src = text_of.get(CanvasCategory.INPUT_SOURCE, "")
    m_ext = _EXTERNAL_RE.search(src)
    if m_ext or web_searches:
        flags.append(
            RiskFlag(
                key="external_sources",
                level=RiskLevel.ELEVATED,
                category=CanvasCategory.INPUT_SOURCE if m_ext else None,
                evidence=m_ext.group(0) if m_ext else f"{web_searches} web search(es) in discovery",
                note="External content is involved; confirm source trust and what leaves the "
                "company boundary.",
            )
        )

    # open_gaps: the completeness check could not close these.
    if open_gaps:
        flags.append(
            RiskFlag(
                key="open_gaps",
                level=RiskLevel.ATTENTION,
                category=None,
                evidence="; ".join(f"{k}: {', '.join(v)}" for k, v in open_gaps.items()),
                note="The stakeholder could not fill these; decide whether to send back.",
            )
        )

    level = max((f.level for f in flags), key=lambda lv: lv.rank, default=RiskLevel.LOW)
    return RiskAssessment(level=level, flags=tuple(flags))


# -- the compiled spec ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecCategory:
    """One canvas category as the reviewer sees it."""

    category: CanvasCategory
    summary: str
    version: int
    sufficient: bool | None
    """Latest completeness verdict; ``None`` when the check was off."""
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "category": self.category.value,
            "label": self.category.label,
            "summary": self.summary,
            "version": self.version,
            "sufficient": self.sufficient,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class DiscoverySpec:
    """What the reviewer approves. Deterministic assembly of the session snapshot."""

    version: int
    session_id: str
    title: str
    narrative: str
    categories: tuple[SpecCategory, ...]
    departments: tuple[str, ...]
    department_rationale: str | None
    sops_consulted: tuple[str, ...]
    web_sources: tuple[str, ...]
    open_gaps: dict[str, list[str]]
    risk: RiskAssessment
    turns: int
    total_cost_usd: float
    compiled_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "version": self.version,
            "session_id": self.session_id,
            "title": self.title,
            "narrative": self.narrative,
            "categories": [c.to_dict() for c in self.categories],
            "departments": list(self.departments),
            "department_rationale": self.department_rationale,
            "sops_consulted": list(self.sops_consulted),
            "web_sources": list(self.web_sources),
            "open_gaps": {k: list(v) for k, v in self.open_gaps.items()},
            "risk": self.risk.to_dict(),
            "turns": self.turns,
            "total_cost_usd": self.total_cost_usd,
            "compiled_at": self.compiled_at.isoformat(),
        }


def _latest_assessments(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    completeness = snapshot.get("completeness") or {}
    for a in completeness.get("assessments", []):
        latest[a["category"]] = a
    return latest


def compile_spec(
    snapshot: Mapping[str, Any],
    skills: Mapping[str, DepartmentSkill],
    *,
    version: int = 1,
    title: str | None = None,
    narrative: str | None = None,
) -> DiscoverySpec:
    """Assemble the spec from a session snapshot. No model call; see :func:`summarize_spec`.

    Raises:
        ValueError: if the snapshot's canvas is not complete.
    """
    canvas = snapshot["canvas"]
    if not canvas.get("is_complete"):
        raise ValueError("cannot compile a spec from an incomplete canvas")

    current: Mapping[str, str | None] = canvas["current"]
    versions: dict[str, int] = {}
    for e in canvas["entries"]:
        versions[e["category"]] = versions.get(e["category"], 0) + 1
    latest = _latest_assessments(snapshot)
    check_on = snapshot.get("completeness") is not None

    categories = tuple(
        SpecCategory(
            category=cat,
            summary=str(current[cat.value] or ""),
            version=versions.get(cat.value, 0),
            sufficient=(latest[cat.value]["sufficient"] if cat.value in latest else None)
            if check_on
            else None,
            missing=tuple(latest[cat.value]["missing"]) if cat.value in latest else (),
        )
        for cat in CanvasCategory
    )

    sops: list[str] = []
    for turn in snapshot.get("turns", []):
        for call in turn.get("tool_calls", []):
            if call["name"].endswith("__read_sop"):
                sop_id = str(call["input"].get("sop_id", "")).strip()
                if sop_id and sop_id not in sops:
                    sops.append(sop_id)
    web = snapshot.get("web_search") or {}
    web_queries = [a["query"] for a in web.get("attempts", []) if a.get("allowed")]

    match = snapshot.get("match") or {}
    open_gaps: dict[str, list[str]] = {k: list(v) for k, v in snapshot.get("open_gaps", {}).items()}
    risk = assess_risk(current, open_gaps=open_gaps, web_searches=len(web_queries), skills=skills)

    # Fallbacks when no model-written title/narrative was supplied. A reviewer's send-back
    # note is the `user_text` of its turn, so the origin filter matters: without it a spec
    # whose first surviving turn is a send-back gets titled with the reviewer's own words.
    first_user = next(
        (
            t["user_text"]
            for t in snapshot.get("turns", [])
            if t.get("origin") != "reviewer" and t.get("user_text")
        ),
        "",
    )
    last_assistant = next(
        (t["assistant_text"] for t in reversed(snapshot.get("turns", [])) if t["assistant_text"]),
        "",
    )
    spec = DiscoverySpec(
        version=version,
        session_id=str(snapshot.get("session_id") or ""),
        title=title or (first_user[:80].rstrip() + ("…" if len(first_user) > 80 else "")),
        narrative=narrative or last_assistant,
        categories=categories,
        departments=tuple(match.get("departments", [])),
        department_rationale=match.get("rationale"),
        sops_consulted=tuple(sops),
        web_sources=tuple(web_queries),
        open_gaps=open_gaps,
        risk=risk,
        turns=int(snapshot.get("turn", 0)),
        total_cost_usd=float(snapshot.get("total_cost_usd", 0.0)),
    )
    logger.info(
        "review.spec.compiled",
        extra={
            "session_id": spec.session_id,
            "version": version,
            "risk": risk.level.value,
            "flags": [f.key for f in risk.flags],
        },
    )
    return spec


SUMMARY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 3,
            "maxLength": 80,
            "description": "A short noun phrase naming the request, e.g. 'Onboarding packet "
            "completeness checker for HR Ops'. No trailing period.",
        },
        "narrative": {
            "type": "string",
            "minLength": 20,
            "maxLength": 600,
            "description": "Two plain-language sentences: what the stakeholder wants and for "
            "whom, and what the prototype would produce.",
        },
    },
    "required": ["title", "narrative"],
    "additionalProperties": False,
}


async def summarize_spec(
    categories: tuple[SpecCategory, ...],
    *,
    model: str = DEFAULT_SPEC_MODEL,
    query_fn: QueryFn = query,
) -> tuple[str, str]:
    """One small structured-output call for the queue-facing title and narrative.

    Raises:
        SpecSummaryError: on failure; callers fall back to the deterministic defaults.
    """
    body = "\n".join(f"- {c.category.label}: {c.summary}" for c in categories)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt="You write short, plain titles and summaries for internal review queues.",
        setting_sources=[],
        env=AGENT_ENV,
        tools=[],
        max_turns=3,
        effort="low",
        output_format={"type": "json_schema", "schema": SUMMARY_SCHEMA},
    )
    result: ResultMessage | None = None
    try:
        async for message in query_fn(
            prompt=f"Discovery notes:\n\n{body}\n\nWrite the title and narrative.", options=options
        ):
            if isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        raise SpecSummaryError(str(exc)) from exc
    if result is None or result.is_error:
        raise SpecSummaryError("no result")
    payload = result.structured_output
    try:
        jsonschema.validate(payload, SUMMARY_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise SpecSummaryError(exc.message) from exc
    assert isinstance(payload, dict)
    return str(payload["title"]).strip().rstrip("."), str(payload["narrative"]).strip()


class SpecSummaryError(RuntimeError):
    """The title/narrative call failed; the spec falls back to deterministic text."""


# -- decisions ------------------------------------------------------------------------------


class ReviewAction(StrEnum):
    """What a reviewer can do with a spec."""

    APPROVE = "approve"
    REJECT = "reject"
    SEND_BACK = "send_back"


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """One human decision on one spec version. Append-only; never edited."""

    action: ReviewAction
    reviewer: str
    note: str
    spec_version: int
    risk_level: RiskLevel
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate at the boundary: a reviewer is named, and send-backs carry a note."""
        if not self.reviewer.strip():
            raise ValueError("reviewer must be named")
        if self.action is not ReviewAction.APPROVE and not self.note.strip():
            raise ValueError(f"{self.action.value} requires a note")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "action": self.action.value,
            "reviewer": self.reviewer,
            "note": self.note,
            "spec_version": self.spec_version,
            "risk_level": self.risk_level.value,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewDecision:
        """Inverse of :meth:`to_dict`."""
        return cls(
            action=ReviewAction(data["action"]),
            reviewer=str(data["reviewer"]),
            note=str(data["note"]),
            spec_version=int(data["spec_version"]),
            risk_level=RiskLevel(data["risk_level"]),
            decided_at=datetime.fromisoformat(data["decided_at"]),
        )


def review_status(decisions: list[ReviewDecision]) -> str:
    """Derived state: ``pending`` | ``approved`` | ``rejected`` | ``sent_back``."""
    if not decisions:
        return "pending"
    last = decisions[-1]
    return {
        ReviewAction.APPROVE: "approved",
        ReviewAction.REJECT: "rejected",
        ReviewAction.SEND_BACK: "sent_back",
    }[last.action]
