"""Terminal runner for hand-testing discovery conversations.

Usage::

    uv run python cli.py                # interactive, default budget cap
    uv run python cli.py --budget 2.0   # tighter spend cap for this session
    uv run python cli.py --script conversations/hr_onboarding.txt   # replay a saved script

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
from collections.abc import Iterator
from pathlib import Path

from dotenv import load_dotenv

from blueprint.observability import configure_logging
from blueprint.orchestrator import (
    DepartmentsMatched,
    DiscoverySession,
    TextDelta,
    ToolCallStarted,
    TurnCompleted,
    TurnResult,
)
from blueprint.settings import Settings, SettingsError, load_settings

RUNS_DIR = Path("runs")
LOG_FILE = Path("logs") / "discovery.jsonl"


def _trace_line(result: TurnResult, session: DiscoverySession) -> str:
    captured = ", ".join(c.value for c in result.captured) or "-"
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "$?"
    remaining = len(session.state.missing())
    flag = "  !! error" if result.is_error else ""
    return (
        f"  [turn {result.turn} · captured: {captured} · {cost} · "
        f"{result.duration_ms} ms · {remaining} category(ies) left{flag}]"
    )


async def _stream_turn(session: DiscoverySession, text: str) -> TurnResult:
    """Print the assistant's reply as it is generated; return the completed turn."""
    print("\nassistant> ", end="", flush=True)
    async for event in session.stream(text):
        match event:
            case TextDelta(text=chunk):
                print(chunk, end="", flush=True)
            case ToolCallStarted():
                print("·", end="", flush=True)  # the UI equivalent is a typing indicator
            case DepartmentsMatched(match=match):
                # Admin-trace only; a requester UI would not render this.
                print(f"\n  [matched: {', '.join(match.departments)}]", end="", flush=True)
            case TurnCompleted(result=result):
                print()
                return result
    raise AssertionError("stream ended without TurnCompleted")  # orchestrator guarantees one


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


async def run(settings: Settings, *, script: Path | None) -> int:
    """Drive one session; return a process exit code."""
    messages = _script_lines(script) if script else _prompt_lines()
    session = DiscoverySession(
        model=settings.model,
        classifier_model=settings.classifier_model,
        max_budget_usd=settings.max_budget_usd,
        idle_timeout_s=settings.idle_timeout_s,
    )

    try:
        async with session:
            for text in messages:
                if script:
                    print(f"\nyou> {text}")
                try:
                    result = await _stream_turn(session, text)
                except ValueError as exc:  # blank or oversized message; nothing was sent
                    print(f"  (not sent: {exc})", file=sys.stderr)
                    continue
                print(_trace_line(result, session))
                if result.is_error:
                    print(f"  session ended: {'; '.join(result.errors)}", file=sys.stderr)
                    break
                if session.is_complete:
                    print("\n  [canvas complete]")
                    break
    finally:
        # The snapshot is the most valuable output of a run; write it no matter what.
        RUNS_DIR.mkdir(exist_ok=True)
        out = RUNS_DIR / f"{session.session_id or 'no-session'}.json"
        out.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
        total = f"${session.total_cost_usd:.4f}"
        print(f"\n  session {session.session_id} · {session.turn} turns · {total}")
        print(f"  snapshot: {out}")

    return 0 if session.is_complete else 1


def main() -> None:
    """Parse args, configure logging, run."""
    parser = argparse.ArgumentParser(description="Hand-test a discovery conversation.")
    parser.add_argument("--model", help="override BLUEPRINT_MODEL for this run")
    parser.add_argument("--budget", type=float, help="override BLUEPRINT_MAX_BUDGET_USD (USD)")
    parser.add_argument("--script", type=Path, help="replay a saved conversation instead of typing")
    args = parser.parse_args()

    # Windows consoles default to a legacy code page; force UTF-8 so trace glyphs print.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    load_dotenv()
    # Flags win over the environment for this one run.
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
    sys.exit(asyncio.run(run(settings, script=args.script)))


if __name__ == "__main__":
    main()
