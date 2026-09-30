"""Drive whole discovery conversations against simulated stakeholders.

Usage::

    uv run python evals/e2e/run.py --dry-run
    uv run python evals/e2e/run.py --limit 1
    uv run python evals/e2e/run.py --yes

This is the only suite that exercises the pipeline end to end: a real ``DiscoverySession``
talking to a simulated person, until the canvas completes or a cap stops it. It is also by far
the most expensive, which is why every knob here is a limit.

**Everything it scores is deterministic.** Each persona carries the ground truth for all seven
categories, so the eval knows what a complete conversation should have surfaced without a
model grading anything. The two headline numbers:

  - **incomplete conversations** — the canvas did not close inside the turn cap. The agent
    failed to get from a real person what that person demonstrably knew.
  - **MISSED risk flags** — flags the compiled spec should have raised and did not. The
    false-negative check from CLAUDE.md, run against a spec compiled from a real conversation
    rather than from a hand-written summary.

Not yet built, and deliberately: the rubric-scored judge comparison of the compiled spec
against a gold one (completeness / accuracy / actionability). That needs a gold spec per
persona and a judge, and it is worth having the transcripts this produces before writing it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blueprint.canvas import CanvasCategory
from blueprint.orchestrator import DiscoverySession
from blueprint.review import assess_risk
from blueprint.settings import SettingsError, load_settings
from blueprint.skills import load_skills
from evals._harness import (
    by_variant,
    confirm_spend,
    print_table,
    timing_and_cost,
    write_results,
)
from evals.e2e.simulator import (
    DEFAULT_SIMULATOR_MODEL,
    Persona,
    Utterance,
    load_personas,
    reply,
)

HERE = Path(__file__).resolve().parent

# A whole conversation, both sides, plus the per-capture completeness checks. Measured at
# roughly eight turns; the dry run multiplies this, so it is meant to be pessimistic.
EST_COST_PER_CONVERSATION = 0.40


@dataclass
class ConversationResult:
    persona_id: str
    variant: str
    completed: bool = False
    turns: int = 0
    captured: list[str] = field(default_factory=list)
    uncaptured: list[str] = field(default_factory=list)
    """Categories the persona could have answered and the agent never recorded."""
    predicted_flags: list[str] = field(default_factory=list)
    expected_flags: list[str] = field(default_factory=list)
    missed_flags: list[str] = field(default_factory=list)
    """Expected flags the gate did not raise. The false negatives."""
    spurious_flags: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    missed_departments: list[str] = field(default_factory=list)
    stopped_by: str = ""
    """``complete``, ``turn_cap``, ``budget``, ``agent_error`` or ``simulator_error``."""
    cost_usd: float | None = None
    duration_ms: int = 0
    error: str | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)


async def run_conversation(
    persona: Persona,
    *,
    model: str,
    simulator_model: str,
    budget_usd: float,
    skills: dict[str, Any],
) -> ConversationResult:
    """One persona, one conversation, until the canvas closes or a cap stops it."""
    result = ConversationResult(persona_id=persona.id, variant=model)
    started = time.monotonic()
    transcript: list[Utterance] = []

    try:
        async with DiscoverySession(
            model=model,
            classifier_model=model,
            max_budget_usd=budget_usd,
            skills=skills,
        ) as session:
            message = persona.brief
            while True:
                transcript.append(Utterance(who="you", text=message))
                turn = await session.send(message)
                transcript.append(Utterance(who="agent", text=turn.assistant_text))
                result.turns = session.turn

                if turn.is_error:
                    result.stopped_by = "agent_error"
                    result.error = "; ".join(turn.errors) or "agent turn failed"
                    break
                if session.is_complete:
                    result.stopped_by = "complete"
                    result.completed = True
                    break
                if session.turn >= persona.max_turns:
                    result.stopped_by = "turn_cap"
                    break
                if (session.total_cost_usd or 0.0) >= budget_usd:
                    result.stopped_by = "budget"
                    break

                answer = await reply(persona, transcript, model=simulator_model)
                if answer.is_error:
                    result.stopped_by = "simulator_error"
                    result.error = "; ".join(answer.errors)
                    break
                message = answer.text

            snapshot = session.to_dict()
            result.cost_usd = session.total_cost_usd
    except Exception as exc:  # one conversation failing must not end the run
        result.stopped_by = result.stopped_by or "agent_error"
        result.error = str(exc)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.transcript = [{"who": u.who, "text": u.text} for u in transcript]
        return result

    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.transcript = [{"who": u.who, "text": u.text} for u in transcript]
    _score(result, persona, snapshot, skills)
    return result


def _score(
    result: ConversationResult,
    persona: Persona,
    snapshot: dict[str, Any],
    skills: dict[str, Any],
) -> None:
    """Fill in the deterministic measures. No model grades anything here."""
    current: dict[str, str | None] = snapshot["canvas"]["current"]
    result.captured = sorted(k for k, v in current.items() if v)
    # Every persona answers all seven by construction, so anything unrecorded is the agent's.
    result.uncaptured = sorted({c.value for c in CanvasCategory} - set(result.captured))

    risk = assess_risk(
        current,
        open_gaps=snapshot.get("open_gaps", {}),
        web_searches=len(((snapshot.get("web_search") or {}).get("attempts")) or []),
        skills=skills,
    )
    result.predicted_flags = sorted(f.key for f in risk.flags)
    result.expected_flags = sorted(persona.expect_flags)
    result.missed_flags = sorted(set(result.expected_flags) - set(result.predicted_flags))
    result.spurious_flags = sorted(set(result.predicted_flags) - set(result.expected_flags))

    match = snapshot.get("match") or {}
    result.departments = sorted(match.get("departments", []))
    result.missed_departments = sorted(set(persona.expect_departments) - set(result.departments))


def summarize(results: list[ConversationResult]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for variant, group in by_variant(results).items():
        # Conversations that never ran are counted, never scored — the same rule the
        # completeness suite learned when a 529 was being reported as a missed gap.
        scored = [r for r in group if r.error is None]
        n = len(scored)
        summary[variant] = {
            **timing_and_cost(group),
            "scored": n,
            "incomplete": sum(1 for r in scored if not r.completed),
            "completion_rate": (sum(r.completed for r in scored) / n) if n else None,
            "conversations_with_missed_flags": sum(1 for r in scored if r.missed_flags),
            "gaps_missed": sum(len(r.uncaptured) for r in scored),
            "flags_missed": sum(len(r.missed_flags) for r in scored),
            "flags_spurious": sum(len(r.spurious_flags) for r in scored),
            "departments_missed": sum(len(r.missed_departments) for r in scored),
            "mean_turns": (sum(r.turns for r in scored) / n) if n else None,
            "incomplete_cases": sorted(r.persona_id for r in scored if not r.completed),
            "missed_flag_cases": sorted(r.persona_id for r in scored if r.missed_flags),
        }
    return summary


def print_detail(results: list[ConversationResult]) -> None:
    failed = [r for r in results if r.error]
    if failed:
        print(f"\n{len(failed)} conversation(s) failed and were not scored:\n")
        for r in failed:
            print(f"  [error] {r.persona_id} · {r.variant} ({r.stopped_by})")
            print(f"      {r.error}")
        print()

    notable = [r for r in results if not r.error and (not r.completed or r.missed_flags)]
    if not notable:
        print("\nEvery scored conversation completed with no missed risk flags.\n")
        return

    print(f"{len(notable)} conversation(s) worth looking at:\n")
    for r in sorted(notable, key=lambda r: (not r.missed_flags, r.persona_id)):
        mark = "MISSED FLAG" if r.missed_flags else "incomplete"
        print(
            f"  [{mark}] {r.persona_id} · {r.variant} — {r.turns} turns, stopped by {r.stopped_by}"
        )
        if r.uncaptured:
            print(f"      never captured: {', '.join(r.uncaptured)}")
        if r.missed_flags:
            print(f"      expected flags not raised: {', '.join(r.missed_flags)}")
        if r.missed_departments:
            print(f"      departments not matched: {', '.join(r.missed_departments)}")
    print()


async def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="+", default=["claude-opus-5"])
    parser.add_argument("--simulator-model", default=DEFAULT_SIMULATOR_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", nargs="+", default=None, help="persona ids to run")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=1.0,
        help="hard per-conversation cap; the session stops itself at this",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    args = parser.parse_args()

    personas = load_personas(HERE / "personas.yaml")
    if args.only:
        wanted = set(args.only)
        unknown = sorted(wanted - {p.id for p in personas})
        if unknown:
            print(f"unknown persona id(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        personas = [p for p in personas if p.id in wanted]
    if args.limit:
        personas = personas[: args.limit]

    conversations = len(personas) * len(args.models)
    estimate = conversations * EST_COST_PER_CONVERSATION
    print(f"e2e: {len(personas)} personas x {len(args.models)} models")
    print(f"{conversations} conversations, roughly ${estimate:.2f}")
    print(
        f"(hard cap ${args.budget_usd:.2f} per conversation, so at most "
        f"${conversations * args.budget_usd:.2f})"
    )
    if args.dry_run:
        for p in personas:
            print(f"  - {p.id} (max {p.max_turns} turns)")
        return 0

    try:
        load_settings()
    except SettingsError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 2

    if not confirm_spend(estimate, calls=conversations, assume_yes=args.yes):
        print("cancelled")
        return 0

    skills = load_skills()
    results: list[ConversationResult] = []
    # Sequentially, not concurrently: each live session holds its own CLI subprocess at
    # roughly 226 MB, and six at once is most of a small machine's memory for no gain on a
    # suite this size.
    for model in args.models:
        for persona in personas:
            print(f"  {persona.id} · {model} …", flush=True)
            results.append(
                await run_conversation(
                    persona,
                    model=model,
                    simulator_model=args.simulator_model,
                    budget_usd=args.budget_usd,
                    skills=skills,
                )
            )

    summary = summarize(results)
    print_table(
        summary,
        [
            ("conversations", "{calls}"),
            ("errors (unscored)", "{errors}"),
            ("scored", "{scored}"),
            ("INCOMPLETE", "{incomplete}"),
            ("MISSED risk flags", "{flags_missed}"),
            ("completion rate", "{completion_rate:.1%}"),
            ("categories never captured", "{gaps_missed}"),
            ("spurious flags", "{flags_spurious}"),
            ("departments missed", "{departments_missed}"),
            ("mean turns", "{mean_turns:.1f}"),
            ("mean cost/conversation", "${mean_cost_usd:.4f}"),
            ("total cost", "${total_cost_usd:.3f}"),
            ("p50 latency", "{p50_ms:.0f}ms"),
            ("p95 latency", "{p95_ms:.0f}ms"),
        ],
    )
    print_detail(results)

    path = write_results(HERE / "results", summary, [asdict(r) for r in results])
    print(f"results: {path}")
    print(f"total spend: ${sum(r.cost_usd or 0.0 for r in results):.3f}")

    # Non-zero on a false negative, matching the risk_gate suite: an unflagged write into a
    # named system is the failure the gate exists to prevent.
    return 1 if any(r.missed_flags for r in results if r.error is None) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
