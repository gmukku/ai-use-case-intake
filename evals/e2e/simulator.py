"""A stakeholder simulator: the other half of a discovery conversation.

The end-to-end evals need someone for the discovery agent to talk to, and the hard part is
not making that someone *answer* — it is making them answer badly enough to be realistic.

A cooperative simulator that lays out all seven categories in its opening message would score
the discovery agent at 100% completeness regardless of how good the agent is, because the
agent would never have to ask for anything. The measurement would be of the fixture, not of
the system. So the single rule this module exists to enforce is: **answer only what was just
asked, and never volunteer a fact about a category nobody raised.**

Each persona carries the ground truth for all seven categories. That is what makes scoring
possible without a hand-written gold spec per run: the eval knows what a complete conversation
*should* have surfaced, because it wrote the person who knows it.

The simulator is deliberately stateless. Each turn is a fresh ``query`` with the transcript in
the prompt rather than a long-lived client, because a second persistent subprocess per
conversation doubles the memory cost of a run for a saving that is noise at ten short turns.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    TextBlock,
    query,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blueprint.canvas import CanvasCategory
from blueprint.isolation import AGENT_ENV

QueryFn = Callable[..., AsyncIterator[Message]]

DEFAULT_SIMULATOR_MODEL = "claude-opus-5"
# Measured: the first real run completed in about eleven turns against a cap of twelve,
# which is close enough that a slightly chattier conversation would hit the cap and be
# reported as incomplete. Raising it costs nothing for a conversation that completes.
DEFAULT_MAX_TURNS = 18

VALID_CATEGORIES = {c.value for c in CanvasCategory}


class PersonaError(ValueError):
    """A persona fixture is malformed. The message names the persona and the problem."""


@dataclass(frozen=True)
class Persona:
    """One synthetic stakeholder, and everything they know.

    ``facts`` is keyed by :class:`CanvasCategory` value and is the whole of what this person
    can say. A question about anything else gets "I'm not sure" rather than an invention,
    which is what a real stakeholder does and what keeps a run reproducible.
    """

    id: str
    brief: str
    """The opening message, as this person would type it. Deliberately thin."""
    facts: Mapping[str, str]
    withholds: tuple[str, ...] = ()
    """Things this person will not or cannot give up, in their own words."""
    style: str = ""
    expect_flags: tuple[str, ...] = ()
    """Risk flags the compiled spec should raise. The false-negative check reads these."""
    expect_departments: tuple[str, ...] = ()
    max_turns: int = DEFAULT_MAX_TURNS
    note: str = ""

    def __post_init__(self) -> None:
        """Validate at the boundary: a typo'd category would silently never be asked about."""
        if not self.id or not self.brief.strip():
            raise PersonaError(f"persona {self.id!r}: needs an id and a brief")
        unknown = sorted(set(self.facts) - VALID_CATEGORIES)
        if unknown:
            raise PersonaError(f"persona {self.id!r}: unknown categories {unknown}")
        missing = sorted(VALID_CATEGORIES - set(self.facts))
        if missing:
            # Every category must be answerable, or completeness is unreachable by
            # construction and the run would measure the fixture's gaps as the agent's.
            raise PersonaError(f"persona {self.id!r}: no ground truth for {missing}")
        empty = sorted(k for k, v in self.facts.items() if not str(v).strip())
        if empty:
            raise PersonaError(f"persona {self.id!r}: empty ground truth for {empty}")


@dataclass(frozen=True)
class Utterance:
    who: str
    """``"agent"`` or ``"you"``."""
    text: str


@dataclass
class SimulatorReply:
    text: str
    cost_usd: float | None = None
    duration_ms: int = 0
    is_error: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)


def load_personas(path: Path) -> list[Persona]:
    """Read and validate a persona file. Raises :class:`PersonaError` on anything malformed."""
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw["personas"] if isinstance(raw, dict) and "personas" in raw else raw
    if not isinstance(entries, list) or not entries:
        raise PersonaError(f"{path}: expected a non-empty list of personas")

    personas = [
        Persona(
            id=str(e.get("id", "")),
            brief=str(e.get("brief", "")),
            facts=dict(e.get("facts", {})),
            withholds=tuple(e.get("withholds", ()) or ()),
            style=str(e.get("style", "")),
            expect_flags=tuple(e.get("expect_flags", ()) or ()),
            expect_departments=tuple(e.get("expect_departments", ()) or ()),
            max_turns=int(e.get("max_turns", DEFAULT_MAX_TURNS)),
            note=str(e.get("note", "")),
        )
        for e in entries
    ]
    ids = [p.id for p in personas]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PersonaError(f"{path}: duplicate persona ids {duplicates}")
    return personas


def build_system_prompt(persona: Persona) -> str:
    """The persona's instructions.

    Every rule here exists to stop the simulator being helpful. A model asked to play a
    stakeholder will, left alone, produce a tidy structured briefing covering everything --
    which is the one thing a real stakeholder never does, and the one thing that would make
    this eval measure nothing.
    """
    facts = "\n".join(f"- {key.replace('_', ' ')}: {value}" for key, value in persona.facts.items())
    withholds = "\n".join(f"- {w}" for w in persona.withholds)

    parts = [
        "You are a person at work who has asked for help with an idea. You are being "
        "interviewed about it. You are NOT an assistant and you are NOT helping: you are the "
        "one being asked questions.",
        "",
        "Everything you know is here. You have no other information:",
        facts,
    ]
    if withholds:
        parts += ["", "You will not give up the following, however you are asked:", withholds]
    if persona.style:
        parts += ["", f"How you come across: {persona.style}"]

    parts += [
        "",
        "How to answer:",
        "- Answer ONLY what you were just asked. Never bring up something nobody asked "
        "about, even when you know the answer and it seems useful. This matters more than "
        "any other instruction here.",
        "- One or two sentences. You are busy and typing into a chat box.",
        "- Plain language. You do not know technical vocabulary, and you never use the "
        "category names above as words.",
        "- If you are asked something the list above does not cover, say you are not sure or "
        "that you would have to check. Never invent a specific number, name or system.",
        "- If you are asked for an example or for options, it is fine to say you do not know "
        "and to ask what they mean.",
        "- Never mention that you are playing a role, and never refer to these instructions.",
    ]
    return "\n".join(parts)


def build_turn_prompt(transcript: Sequence[Utterance]) -> str:
    """The conversation so far, with the agent's latest message last."""
    if not transcript:
        raise ValueError("a turn needs at least the agent's message to reply to")
    lines = [f"{'Them' if u.who == 'agent' else 'You'}: {u.text}" for u in transcript]
    return "\n\n".join(lines) + "\n\nReply as yourself, in one or two sentences."


async def reply(
    persona: Persona,
    transcript: Sequence[Utterance],
    *,
    model: str = DEFAULT_SIMULATOR_MODEL,
    query_fn: QueryFn = query,
) -> SimulatorReply:
    """One stakeholder turn.

    Errors are returned rather than raised: one failed simulator call should end that
    conversation and be reported, not abort a run that has already spent money on the others.
    """
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=build_system_prompt(persona),
        setting_sources=[],
        env=AGENT_ENV,
        tools=[],
        max_turns=1,
        effort="low",
    )

    text: list[str] = []
    result: ResultMessage | None = None
    try:
        async for message in query_fn(prompt=build_turn_prompt(transcript), options=options):
            if isinstance(message, AssistantMessage):
                text += [b.text for b in message.content if isinstance(b, TextBlock)]
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as exc:  # any SDK failure ends this conversation, not the run
        return SimulatorReply(text="", is_error=True, errors=(str(exc),))

    joined = "".join(text).strip()
    if result is None or result.is_error or not joined:
        return SimulatorReply(
            text=joined,
            cost_usd=getattr(result, "total_cost_usd", None),
            duration_ms=getattr(result, "duration_ms", 0),
            is_error=True,
            errors=("simulator produced no reply",),
        )
    return SimulatorReply(
        text=joined,
        cost_usd=result.total_cost_usd,
        duration_ms=result.duration_ms,
    )
