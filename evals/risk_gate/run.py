"""Check the risk gate against labeled cases, with false negatives as the headline.

Usage::

    uv run python evals/risk_gate/run.py
    uv run python evals/risk_gate/run.py --limit 5

`assess_risk` is deterministic rules, so this costs nothing, runs instantly, and is exact.
No model grades anything — the labels in `cases.yaml` are the judge.

CLAUDE.md asks specifically for "a false-negative check on the risk gate: cases that should
have been flagged for extra review but weren't". That is the number printed first, because a
false positive costs a reviewer thirty seconds and a false negative is an unreviewed build.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blueprint.canvas import CanvasCategory
from blueprint.review import assess_risk
from blueprint.skills import SKILLS_DIR, DepartmentSkill, load_skills
from evals._harness import (
    by_variant,
    load_cases,
    print_table,
    timing_and_cost,
    write_results,
)

HERE = Path(__file__).resolve().parent
NEUTRAL = "Not discussed in any detail."


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    variant: str = "rules"
    predicted_flags: list[str] = field(default_factory=list)
    expected_flags: list[str] = field(default_factory=list)
    predicted_scrutiny: bool = False
    expected_scrutiny: bool = False
    level: str = "low"
    missed: list[str] = field(default_factory=list)
    """Expected flags the rules did not raise — the false negatives, per flag."""
    spurious: list[str] = field(default_factory=list)
    exact: bool = False
    scrutiny_false_negative: bool = False
    scrutiny_false_positive: bool = False
    duration_ms: int = 0
    cost_usd: float | None = None
    error: str | None = None
    note: str = ""


def build_canvas(case: dict[str, Any]) -> dict[str, str | None]:
    """A full seven-category canvas: the case's summaries, neutral filler elsewhere."""
    given = case.get("canvas", {})
    return {category.value: given.get(category.value, NEUTRAL) for category in CanvasCategory}


def evaluate(case: dict[str, Any], skills: Mapping[str, DepartmentSkill]) -> CaseResult:
    expected = sorted(case.get("expect_flags", []))
    gaps = {k: list(v) for k, v in (case.get("gaps") or {}).items()}
    assessment = assess_risk(build_canvas(case), open_gaps=gaps, web_searches=0, skills=skills)
    predicted = sorted(flag.key for flag in assessment.flags)

    missed = sorted(set(expected) - set(predicted))
    spurious = sorted(set(predicted) - set(expected))
    expected_scrutiny = bool(case.get("scrutiny", False))

    return CaseResult(
        case_id=str(case["id"]),
        predicted_flags=predicted,
        expected_flags=expected,
        predicted_scrutiny=assessment.needs_extra_scrutiny,
        expected_scrutiny=expected_scrutiny,
        level=assessment.level.value,
        missed=missed,
        spurious=spurious,
        exact=predicted == expected,
        scrutiny_false_negative=expected_scrutiny and not assessment.needs_extra_scrutiny,
        scrutiny_false_positive=assessment.needs_extra_scrutiny and not expected_scrutiny,
        note=str(case.get("note", "")).strip(),
    )


def summarize(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for variant, group in by_variant(results).items():
        n = len(group)
        flags = sorted({f for r in group for f in r.expected_flags + r.predicted_flags})
        per_flag = {}
        for flag in flags:
            tp = sum(1 for r in group if flag in r.expected_flags and flag in r.predicted_flags)
            fp = sum(1 for r in group if flag not in r.expected_flags and flag in r.predicted_flags)
            fn = sum(1 for r in group if flag in r.expected_flags and flag not in r.predicted_flags)
            per_flag[flag] = {
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "missed": fn,
            }
        summary[variant] = {
            **timing_and_cost(group),
            # The headline. Everything else is diagnosis.
            "scrutiny_false_negatives": sum(r.scrutiny_false_negative for r in group),
            "scrutiny_false_negative_rate": sum(r.scrutiny_false_negative for r in group) / n,
            "scrutiny_false_positives": sum(r.scrutiny_false_positive for r in group),
            "scrutiny_recall": (
                sum(1 for r in group if r.expected_scrutiny and r.predicted_scrutiny)
                / max(1, sum(1 for r in group if r.expected_scrutiny))
            ),
            "exact_match": sum(r.exact for r in group) / n,
            "flags_missed": sum(len(r.missed) for r in group),
            "flags_spurious": sum(len(r.spurious) for r in group),
            "per_flag": per_flag,
            "wrong_cases": sorted(r.case_id for r in group if not r.exact),
            "false_negative_cases": sorted(r.case_id for r in group if r.scrutiny_false_negative),
        }
    return summary


def print_disagreements(results: list[CaseResult]) -> None:
    wrong = [r for r in results if not r.exact or r.scrutiny_false_negative]
    if not wrong:
        print("\nEvery case matched its label.")
        return
    print(f"\n{len(wrong)} case(s) disagreed with the labels:\n")
    for r in sorted(wrong, key=lambda r: (not r.scrutiny_false_negative, r.case_id)):
        mark = "FALSE NEGATIVE" if r.scrutiny_false_negative else "flag mismatch"
        print(f"  [{mark}] {r.case_id}  (level={r.level})")
        if r.missed:
            print(f"      missed:   {', '.join(r.missed)}")
        if r.spurious:
            print(f"      spurious: {', '.join(r.spurious)}")
        if r.note:
            print(f"      label rationale: {r.note.splitlines()[0]}")
    print()


async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    cases = load_cases(HERE / "cases.yaml")
    if args.limit:
        cases = cases[: args.limit]
    skills = load_skills(SKILLS_DIR)

    results = [evaluate(case, skills) for case in cases]
    summary = summarize(results)

    print(f"\nrisk gate: {len(cases)} labeled cases, deterministic rules, $0.00")
    print_table(
        summary,
        [
            ("cases", "{calls}"),
            ("scrutiny FALSE NEG", "{scrutiny_false_negatives}"),
            ("  as a rate", "{scrutiny_false_negative_rate:.1%}"),
            ("scrutiny recall", "{scrutiny_recall:.1%}"),
            ("scrutiny false pos", "{scrutiny_false_positives}"),
            ("exact flag match", "{exact_match:.1%}"),
            ("flags missed", "{flags_missed}"),
            ("flags spurious", "{flags_spurious}"),
        ],
    )

    per_flag = summary["rules"]["per_flag"]
    print("per flag (precision / recall / missed):")
    for flag, metrics in sorted(per_flag.items()):
        p = "  -  " if metrics["precision"] is None else f"{metrics['precision']:.2f}"
        r = "  -  " if metrics["recall"] is None else f"{metrics['recall']:.2f}"
        print(f"  {flag:22} {p}   {r}   {metrics['missed']}")

    print_disagreements(results)

    out = write_results(HERE, summary, [asdict(r) for r in results])
    print(f"results: {out.relative_to(Path(__file__).resolve().parents[2])}")

    # A false negative is the failure this suite exists to catch, so it fails the run.
    return 1 if summary["rules"]["scrutiny_false_negatives"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
