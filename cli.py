"""Terminal runner for hand-testing the discovery pipeline.

Usage::

    uv run python cli.py                                    # chat, interactive
    uv run python cli.py chat --script conversations/x.txt  # replay a saved conversation
    uv run python cli.py review runs/<id>.json              # compile spec + risk, no decision
    uv run python cli.py review runs/<id>.json --send-back --note "Which HRIS?" --reviewer me
    uv run python cli.py resume runs/<id>.json              # reopen; delivers the send-back note
    uv run python cli.py build runs/<id>.json               # build the prototype (approved runs)

Prints the assistant's replies plus a one-line trace per turn. On exit, writes the full
session snapshot (canvas + every turn) to ``runs/<session_id>.json`` and JSON-line logs to
``logs/discovery.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from blueprint.builder import (
    PROTOTYPES_DIR,
    UnsupportedOutputFormatError,
    build_prototype,
    select_template,
)
from blueprint.canvas import CanvasCategory
from blueprint.observability import configure_logging
from blueprint.orchestrator import (
    DepartmentsMatched,
    DiscoverySession,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
    TurnEvent,
    TurnResult,
)
from blueprint.review import (
    DiscoverySpec,
    ReviewAction,
    ReviewDecision,
    SpecSummaryError,
    compile_spec,
    review_status,
    summarize_spec,
)
from blueprint.settings import Settings, SettingsError, load_settings
from blueprint.skills import load_skills
from blueprint.websearch import TavilyClient

RUNS_DIR = Path("runs")
LOG_FILE = Path("logs") / "discovery.jsonl"


def _trace_line(result: TurnResult, session: DiscoverySession) -> str:
    captured = ", ".join(
        f"{a.category.value}{'' if a.sufficient else '?'}" for a in result.assessments
    ) or (", ".join(c.value for c in result.captured) or "-")
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "$?"
    remaining = len(session.state.missing())
    flag = "  !! error" if result.is_error else ""
    who = " (reviewer)" if result.origin == "reviewer" else ""
    return (
        f"  [turn {result.turn}{who} · captured: {captured} · {cost} · "
        f"{result.duration_ms} ms · {remaining} category(ies) left{flag}]"
    )


async def _print_stream(events: AsyncIterator[TurnEvent]) -> TurnResult:
    """Print the assistant's reply as it is generated; return the completed turn."""
    print("\nassistant> ", end="", flush=True)
    async for event in events:
        match event:
            case TextDelta(text=chunk):
                print(chunk, end="", flush=True)
            case ToolCallStarted(name=name):
                # The UI equivalent is a typing indicator; the CLI shows which tool for the trace:
                # ⌕ web search, § SOP library, · canvas bookkeeping.
                glyph = "⌕" if name.endswith("web_search") else "§" if "__sop__" in name else "·"
                print(glyph, end="", flush=True)
            case DepartmentsMatched(match=match):
                # Admin-trace only; a requester UI would not render this.
                print(f"\n  [matched: {', '.join(match.departments)}]", end="", flush=True)
            case TurnCompleted(result=result):
                print()
                return result
    raise AssertionError("stream ended without TurnCompleted")  # orchestrator guarantees one


def _fmt_gaps(gaps: dict[CanvasCategory, tuple[str, ...]]) -> str:
    return "; ".join(f"{c.value}: {', '.join(g)}" for c, g in gaps.items()) or "-"


def _script_lines(path: Path) -> Iterator[str]:
    """Yield non-empty, non-comment lines from a saved conversation script."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _prompt_lines() -> Iterator[str]:
    """Yield stakeholder messages typed at the terminal until 'quit' or EOF."""
    while True:
        try:
            text = input("\nPlease provide your response here> ").strip()
        except EOFError:
            return
        if text.lower() in {"quit", "exit"}:
            return
        if text:
            yield text


# -- session plumbing -----------------------------------------------------------------------


def _build_session(settings: Settings, snapshot: dict[str, Any] | None = None) -> DiscoverySession:
    """One place that turns settings into a session, fresh or restored."""
    search = (
        TavilyClient(settings.tavily_api_key)
        if settings.web_search_enabled and settings.tavily_api_key
        else None
    )
    return DiscoverySession(
        model=settings.model,
        classifier_model=settings.classifier_model,
        max_budget_usd=settings.max_budget_usd,
        idle_timeout_s=settings.idle_timeout_s,
        web_search=search,
        sop_grounding=settings.sop_grounding,
        completeness_check=settings.completeness_check,
        checker_model=settings.checker_model,
        snapshot=snapshot,
    )


def _save_snapshot(session: DiscoverySession) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"{session.session_id or 'no-session'}.json"
    out.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
    return out


def _load_snapshot(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


async def _converse(session: DiscoverySession, messages: Iterator[str], *, echo: bool) -> None:
    """Drive turns until ready for review, a failure, or the messages run out."""
    for text in messages:
        if echo:
            print(f"\nyou> {text}")
        try:
            result = await _print_stream(session.stream(text))
        except ValueError as exc:  # blank or oversized message; nothing was sent
            print(f"  (not sent: {exc})", file=sys.stderr)
            continue
        print(_trace_line(result, session))
        if result.is_error:
            print(f"  session ended: {'; '.join(result.errors)}", file=sys.stderr)
            return
        if session.is_ready_for_review:
            gaps = session.completeness.gaps(session.state)
            note = f"; carried gaps: {_fmt_gaps(gaps)}" if gaps else ""
            print(f"\n  [ready for review{note}]")
            return
        if session.is_complete:
            open_gaps = _fmt_gaps(session.completeness.gaps(session.state))
            print(f"  [complete, clarifying: {open_gaps}]")


def _finish(session: DiscoverySession) -> int:
    out = _save_snapshot(session)
    total = f"${session.total_cost_usd:.4f}"
    print(f"\n  session {session.session_id} · {session.turn} turns · {total}")
    print(f"  snapshot: {out}")
    return 0 if session.is_ready_for_review else 1


# -- commands -------------------------------------------------------------------------------


async def cmd_chat(settings: Settings, *, script: Path | None) -> int:
    """Start a new discovery conversation."""
    messages = _script_lines(script) if script else _prompt_lines()
    session = _build_session(settings)
    try:
        async with session:
            await _converse(session, messages, echo=script is not None)
    finally:
        code = _finish(session)
    return code


async def cmd_resume(settings: Settings, *, run_file: Path, script: Path | None) -> int:
    """Reopen a saved session. A pending send-back note is delivered first, then you continue."""
    session = _build_session(settings, _load_snapshot(run_file))
    pending = (
        [d for d in session.reviews if d.action is ReviewAction.SEND_BACK][-1:]
        if session.review_status == "sent_back"
        else []
    )
    messages = _script_lines(script) if script else _prompt_lines()
    try:
        async with session:
            if pending:
                print(f"\n  [delivering reviewer note: {pending[0].note}]")
                result = await _print_stream(session.stream_reviewer_note(pending[0].note))
                print(_trace_line(result, session))
            await _converse(session, messages, echo=script is not None)
    finally:
        code = _finish(session)
    return code


def _print_spec(spec: DiscoverySpec) -> None:
    print(f"\n== {spec.title} ==")
    print(
        f"session {spec.session_id} · spec v{spec.version} · {spec.turns} turns · "
        f"${spec.total_cost_usd:.4f}"
    )
    print(f"departments: {', '.join(spec.departments) or '-'}")
    if spec.department_rationale:
        print(f"  {spec.department_rationale}")
    print(f"\n{spec.narrative}\n")
    for c in spec.categories:
        if c.sufficient is None:
            mark = ""
        elif c.sufficient:
            mark = " ✓"
        else:
            mark = f" ? missing {', '.join(c.missing)}"
        print(f"[{c.category.label}] (v{c.version}){mark}\n  {c.summary}")
    if spec.sops_consulted:
        print(f"\nSOPs consulted: {', '.join(spec.sops_consulted)}")
    if spec.web_sources:
        print(f"web searches: {', '.join(spec.web_sources)}")
    scrutiny = "  (extra scrutiny)" if spec.risk.needs_extra_scrutiny else ""
    print(f"\nRISK: {spec.risk.level.value.upper()}{scrutiny}")
    for f in spec.risk.flags:
        where = f" [{f.category.value}]" if f.category else ""
        print(f"  - {f.key} ({f.level.value}){where}: {f.evidence}\n      {f.note}")


async def cmd_review(
    settings: Settings,
    *,
    run_file: Path,
    action: str | None,
    note: str,
    reviewer: str,
    summarize: bool,
) -> int:
    """Compile the spec for a saved run, show it with risk flags, optionally record a decision."""
    snapshot = _load_snapshot(run_file)
    skills = load_skills()
    decisions = [ReviewDecision.from_dict(d) for d in snapshot.get("reviews", [])]
    version = len(decisions) + 1
    title = narrative = None
    if summarize:
        cats = compile_spec(snapshot, skills, version=version).categories
        try:
            title, narrative = await summarize_spec(cats, model=settings.model)
        except SpecSummaryError as exc:
            print(f"  (title/narrative fallback: {exc})", file=sys.stderr)
    spec = compile_spec(snapshot, skills, version=version, title=title, narrative=narrative)
    _print_spec(spec)
    print(f"\nreview status: {review_status(decisions)}")

    if action is None:
        return 0
    decision = ReviewDecision(
        action=ReviewAction(action),
        reviewer=reviewer,
        note=note,
        spec_version=spec.version,
        risk_level=spec.risk.level,
    )
    decisions.append(decision)
    snapshot["reviews"] = [d.to_dict() for d in decisions]
    snapshot["review_status"] = review_status(decisions)
    snapshot["spec"] = spec.to_dict()
    run_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    logging.getLogger("blueprint.cli").info(
        "review.recorded",
        extra={
            "session_id": spec.session_id,
            "action": action,
            "reviewer": reviewer,
            "spec_version": spec.version,
            "risk": spec.risk.level.value,
        },
    )
    status = snapshot["review_status"]
    print(f"\n  recorded: {action} by {reviewer} on spec v{spec.version} -> {status}")
    if decision.action is ReviewAction.SEND_BACK:
        print(f"  next: uv run python cli.py resume {run_file}")
    return 0


async def cmd_build(settings: Settings, *, run_file: Path, force: bool) -> int:
    """Build the prototype for an approved run. The result is written into the run file."""
    snapshot = _load_snapshot(run_file)
    status = snapshot.get("review_status", "pending")
    if status != "approved" and not force:
        print(f"refusing to build: review status is '{status}', not 'approved'", file=sys.stderr)
        return 3
    skills = load_skills()
    stored = snapshot.get("spec")
    spec = compile_spec(
        snapshot,
        skills,
        version=int(stored["version"]) if stored else 1,
        title=stored["title"] if stored else None,
        narrative=stored["narrative"] if stored else None,
    )
    try:
        template = select_template(spec)
    except UnsupportedOutputFormatError as exc:
        print(f"cannot build: {exc}", file=sys.stderr)
        return 4
    workspace = PROTOTYPES_DIR / spec.session_id
    print(f"\n== building '{spec.title}' ({template}) ==\n   workspace: {workspace}")
    result = await build_prototype(
        spec, workspace=workspace, model=settings.model, max_budget_usd=settings.build_budget_usd
    )
    snapshot["build"] = result.to_dict()
    run_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    v = result.verification
    print(f"\nfiles: {', '.join(v.files) or '-'}  ({v.total_lines} lines)")
    print(f"tests: {'passed' if v.tests_passed else 'FAILED'}")
    if v.violations:
        print("violations: " + "; ".join(v.violations))
    denied = [d for d in result.guard["decisions"] if not d["allowed"]]
    print(f"guard: {len(result.guard['decisions'])} calls, {len(denied)} denied")
    for d in denied:
        print(f"   - {d['tool']} {d['target'][:70]!r}: {d['reason']}")
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "$?"
    print(f"cost: {cost} · {result.duration_ms} ms · {'OK' if result.ok else 'NOT OK'}")
    if result.errors:
        print("errors: " + "; ".join(result.errors), file=sys.stderr)
    print(f"\nbuilder report:\n{result.report}")
    return 0 if result.ok else 1


def main() -> None:
    """Parse args, configure logging, dispatch."""
    parser = argparse.ArgumentParser(description="Blueprint AI hand-testing CLI.")
    parser.add_argument("--model", help="override BLUEPRINT_MODEL for this run")
    parser.add_argument("--budget", type=float, help="override BLUEPRINT_MAX_BUDGET_USD (USD)")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="start a discovery conversation (default)")
    chat.add_argument("--script", type=Path, help="replay a saved conversation instead of typing")

    review = sub.add_parser("review", help="compile the spec for a saved run; record a decision")
    review.add_argument("run_file", type=Path)
    review.add_argument("--approve", action="store_const", const="approve", dest="action")
    review.add_argument("--reject", action="store_const", const="reject", dest="action")
    review.add_argument("--send-back", action="store_const", const="send_back", dest="action")
    review.add_argument("--note", default="", help="required for reject and send-back")
    default_reviewer = os.environ.get("USERNAME") or os.environ.get("USER") or "reviewer"
    review.add_argument("--reviewer", default=default_reviewer)
    review.add_argument("--summarize", action="store_true", help="model-written title/narrative")

    resume = sub.add_parser("resume", help="reopen a saved session (delivers a pending send-back)")
    resume.add_argument("run_file", type=Path)
    resume.add_argument("--script", type=Path)

    build = sub.add_parser("build", help="build the prototype for an approved run")
    build.add_argument("run_file", type=Path)
    build.add_argument("--force", action="store_true", help="build even if not approved (dev only)")

    args = parser.parse_args()

    # Windows consoles default to a legacy code page; force UTF-8 so trace glyphs print.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    load_dotenv()
    if args.model:
        os.environ["BLUEPRINT_MODEL"] = args.model
    if args.budget is not None:
        os.environ["BLUEPRINT_MAX_BUDGET_USD"] = str(args.budget)
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    configure_logging(LOG_FILE)
    logging.getLogger("blueprint.cli").info("cli.start", extra=settings.to_dict())

    command = args.command or "chat"
    if command == "chat":
        code = asyncio.run(cmd_chat(settings, script=getattr(args, "script", None)))
    elif command == "review":
        code = asyncio.run(
            cmd_review(
                settings,
                run_file=args.run_file,
                action=args.action,
                note=args.note,
                reviewer=args.reviewer,
                summarize=args.summarize,
            )
        )
    elif command == "build":
        code = asyncio.run(cmd_build(settings, run_file=args.run_file, force=args.force))
    else:
        code = asyncio.run(cmd_resume(settings, run_file=args.run_file, script=args.script))
    sys.exit(code)


if __name__ == "__main__":
    main()
