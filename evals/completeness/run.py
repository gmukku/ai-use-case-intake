"""Benchmark the completeness checker against the labeled captures.

Usage::

    uv run python evals/completeness/run.py                      # default models, 2 repeats
    uv run python evals/completeness/run.py --models claude-haiku-4-5-20251001 --repeats 1
    uv run python evals/completeness/run.py --dry-run            # cost estimate only

Reference-based: the labels in `cases.yaml` are the judge. The checker runs inside the record
tool, so its verdict becomes a follow-up question in the same reply — which makes both error
directions expensive in different ways:

    a MISSED gap  -> the spec ships thin and the reviewer finds it
    a FALSE gap   -> the stakeholder is asked something they already answered

`missed_rate` is the headline. It answers the deferred question from step 4: can a cheaper
model hold this job?
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blueprint.canvas import CATEGORY_RUBRIC, CanvasCategory
from blueprint.completeness import AssessmentError, assess_capture
from evals._harness import (
    by_variant,
    confirm_spend,
    load_cases,
    print_table,
    run_all,
    timing_and_cost,
    write_results,
)

HERE = Path(__file__).resolve().parent
DEFAULT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
EST_COST_PER_CALL = 0.008


@dataclass(frozen=True)
class CallResult:
    case_id: str
    variant: str
    repeat: int
    category: str
    predicted_missing: list[str]
    expected_missing: list[str]
    missed: list[str]
    """Real gaps the checker did not report — the spec ships thin."""
    invented: list[str]
    """Gaps the checker reported that a human says are answered — the stakeholder is nagged."""
    exact: bool
    sufficient_agreement: bool
    """Did it agree on the binary question: is this capture good enough?"""
    duration_ms: int
    cost_usd: float | None
    question: str | None
    error: str | None


async def run_case(case: dict[str, Any], model: str, repeat: int) -> CallResult:
    category = CanvasCategory(case["category"])
    expected = sorted(case.get("expect_missing", []))
    case_id = str(case["id"])
    try:
        assessment = await assess_capture(
            category, str(case["summary"]).strip(), version=1, model=model
        )
    except (AssessmentError, ValueError) as exc:
        # A failed call counts as missing every real gap: in production that is what the
        # conversation would have done with it.
        return CallResult(
            case_id=case_id,
            variant=model,
            repeat=repeat,
            category=category.value,
            expected_missing=expected,
            predicted_missing=[],
            missed=expected,
            invented=[],
            exact=False,
            sufficient_agreement=False,
            duration_ms=0,
            cost_usd=None,
            question=None,
            error=str(exc),
        )

    predicted = sorted(assessment.missing)
    return CallResult(
        case_id=case_id,
        variant=model,
        repeat=repeat,
        category=category.value,
        expected_missing=expected,
        predicted_missing=predicted,
        missed=sorted(set(expected) - set(predicted)),
        invented=sorted(set(predicted) - set(expected)),
        exact=predicted == expected,
        sufficient_agreement=(not predicted) == (not expected),
        duration_ms=assessment.duration_ms,
        cost_usd=assessment.cost_usd,
        question=assessment.question,
        error=None,
    )


def summarize(results: list[CallResult]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for variant, group in by_variant(results).items():
        n = len(group)
        keys = sorted({k for c in CanvasCategory for k in (e.key for e in CATEGORY_RUBRIC[c])})
        per_key = {}
        for key in keys:
            tp = sum(1 for r in group if key in r.expected_missing and key in r.predicted_missing)
            fp = sum(
                1 for r in group if key not in r.expected_missing and key in r.predicted_missing
            )
            fn = sum(
                1 for r in group if key in r.expected_missing and key not in r.predicted_missing
            )
            if tp or fp or fn:
                per_key[key] = {
                    "precision": tp / (tp + fp) if tp + fp else None,
                    "recall": tp / (tp + fn) if tp + fn else None,
                    "missed": fn,
                }

        # Stability: did every repeat of a case give the same verdict?
        by_case: dict[str, set[frozenset[str]]] = {}
        for r in group:
            by_case.setdefault(r.case_id, set()).add(frozenset(r.predicted_missing))

        summary[variant] = {
            **timing_and_cost(group),
            "missed_rate": sum(1 for r in group if r.missed) / n,
            "invented_rate": sum(1 for r in group if r.invented) / n,
            "exact_match": sum(r.exact for r in group) / n,
            "sufficient_agreement": sum(r.sufficient_agreement for r in group) / n,
            "gaps_missed": sum(len(r.missed) for r in group),
            "gaps_invented": sum(len(r.invented) for r in group),
            "per_key": per_key,
            "unstable_cases": sorted(c for c, preds in by_case.items() if len(preds) > 1),
            "n_unstable": sum(1 for preds in by_case.values() if len(preds) > 1),
            "wrong_cases": sorted({r.case_id for r in group if not r.exact}),
            "missed_cases": sorted({r.case_id for r in group if r.missed}),
        }
    return summary


def print_disagreements(results: list[CallResult]) -> None:
    wrong = [r for r in results if not r.exact]
    if not wrong:
        print("\nEvery call matched its label.")
        return
    print(f"\n{len(wrong)} call(s) disagreed with the labels:\n")
    for r in sorted(wrong, key=lambda r: (not r.missed, r.case_id, r.variant)):
        mark = "MISSED" if r.missed else "invented"
        print(f"  [{mark}] {r.case_id} · {r.variant} (repeat {r.repeat})")
        print(f"      expected {r.expected_missing or '[]'}, got {r.predicted_missing or '[]'}")
        if r.question:
            print(f"      would have asked: {r.question}")
        if r.error:
            print(f"      error: {r.error}")
    print()


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    args = parser.parse_args()

    cases = load_cases(HERE / "cases.yaml")
    if args.limit:
        cases = cases[: args.limit]

    calls = len(cases) * len(args.models) * args.repeats
    print(f"completeness: {len(cases)} cases x {len(args.models)} models x {args.repeats} repeats")
    if args.dry_run:
        print(f"{calls} calls, roughly ${calls * EST_COST_PER_CALL:.2f}")
        return 0
    if not confirm_spend(calls * EST_COST_PER_CALL, calls=calls, assume_yes=args.yes):
        print("cancelled")
        return 0

    jobs = [
        (lambda c=case, m=model, r=repeat: run_case(c, m, r))
        for case in cases
        for model in args.models
        for repeat in range(1, args.repeats + 1)
    ]
    results = await run_all(jobs)
    summary = summarize(results)

    print_table(
        summary,
        [
            ("calls", "{calls}"),
            ("errors", "{errors}"),
            ("MISSED gap rate", "{missed_rate:.1%}"),
            ("invented gap rate", "{invented_rate:.1%}"),
            ("exact match", "{exact_match:.1%}"),
            ("sufficient agree", "{sufficient_agreement:.1%}"),
            ("unstable cases", "{n_unstable}"),
            ("mean cost/call", "${mean_cost_usd:.4f}"),
            ("total cost", "${total_cost_usd:.3f}"),
            ("p50 latency", "{p50_ms:.0f}ms"),
            ("p95 latency", "{p95_ms:.0f}ms"),
        ],
    )
    for variant, metrics in summary.items():
        unstable = metrics["unstable_cases"]
        if unstable:
            print(f"{variant}: unstable across repeats -> {', '.join(unstable)}")

    print_disagreements(results)

    out = write_results(HERE, summary, [asdict(r) for r in results])
    print(f"results: {out.relative_to(Path(__file__).resolve().parents[2])}")
    print(f"total spend: ${sum(m['total_cost_usd'] for m in summary.values()):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
