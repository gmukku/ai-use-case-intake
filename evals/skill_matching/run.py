"""Benchmark the department classifier against the labeled cases.

Usage::

    uv run python evals/skill_matching/run.py                       # opus vs sonnet, 3 repeats
    uv run python evals/skill_matching/run.py --models claude-opus-5 --repeats 1
    uv run python evals/skill_matching/run.py --dry-run             # print cost estimate only

Reference-based: the labels in ``cases.yaml`` are the judge. Prints a comparison table and
writes raw per-call results to ``results/<timestamp>.json`` for later inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from blueprint.matching import MatchError, match_departments  # noqa: E402
from blueprint.skills import DepartmentSkill, load_skills  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES_FILE = HERE / "cases.yaml"
RESULTS_DIR = HERE / "results"
DEFAULT_MODELS = ("claude-opus-5", "claude-sonnet-5")
CONCURRENCY = 4
EST_COST_PER_CALL = 0.016  # from the three-case probe; refined by the actual run


@dataclass(frozen=True)
class Case:
    id: str
    transcript: str
    expected: frozenset[str]
    note: str


@dataclass(frozen=True)
class CallResult:
    case_id: str
    model: str
    repeat: int
    predicted: list[str]
    expected: list[str]
    exact: bool
    jaccard: float
    over: int  # predicted but not expected
    under: int  # expected but not predicted
    cost_usd: float | None
    duration_ms: int
    rationale: str
    error: str | None


def load_cases() -> list[Case]:
    raw = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    cases = [
        Case(
            id=c["id"],
            transcript=c["transcript"].strip(),
            expected=frozenset(c["expected"]),
            note=str(c.get("note", "")).strip(),
        )
        for c in raw
    ]
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return cases


def score(predicted: frozenset[str], expected: frozenset[str]) -> tuple[bool, float, int, int]:
    union = predicted | expected
    jaccard = len(predicted & expected) / len(union) if union else 1.0
    return predicted == expected, jaccard, len(predicted - expected), len(expected - predicted)


async def run_one(
    case: Case, model: str, repeat: int, skills: dict[str, DepartmentSkill], sem: asyncio.Semaphore
) -> CallResult:
    async with sem:
        try:
            m = await match_departments(case.transcript, skills, model=model)
        except MatchError as exc:
            return CallResult(
                case_id=case.id,
                model=model,
                repeat=repeat,
                predicted=[],
                expected=sorted(case.expected),
                exact=False,
                jaccard=0.0,
                over=0,
                under=len(case.expected),
                cost_usd=None,
                duration_ms=0,
                rationale="",
                error=str(exc),
            )
    predicted = frozenset(m.departments)
    exact, jaccard, over, under = score(predicted, case.expected)
    return CallResult(
        case_id=case.id,
        model=model,
        repeat=repeat,
        predicted=list(m.departments),
        expected=sorted(case.expected),
        exact=exact,
        jaccard=jaccard,
        over=over,
        under=under,
        cost_usd=m.cost_usd,
        duration_ms=m.duration_ms,
        rationale=m.rationale,
        error=None,
    )


def summarize(results: list[CallResult], cases: list[Case]) -> dict[str, dict[str, Any]]:
    by_model: dict[str, list[CallResult]] = defaultdict(list)
    for r in results:
        by_model[r.model].append(r)

    departments = sorted({d for c in cases for d in c.expected})
    summary: dict[str, dict[str, Any]] = {}
    for model, rs in by_model.items():
        n = len(rs)
        # per-department precision / recall over all calls
        tp: dict[str, int] = defaultdict(int)
        fp: dict[str, int] = defaultdict(int)
        fn: dict[str, int] = defaultdict(int)
        for r in rs:
            p, e = set(r.predicted), set(r.expected)
            for d in departments:
                if d in p and d in e:
                    tp[d] += 1
                elif d in p:
                    fp[d] += 1
                elif d in e:
                    fn[d] += 1
        per_dept = {
            d: {
                "precision": tp[d] / (tp[d] + fp[d]) if tp[d] + fp[d] else None,
                "recall": tp[d] / (tp[d] + fn[d]) if tp[d] + fn[d] else None,
            }
            for d in departments
        }
        # per-case stability: did all repeats of a case agree?
        by_case: dict[str, set[frozenset[str]]] = defaultdict(set)
        for r in rs:
            by_case[r.case_id].add(frozenset(r.predicted))
        unstable = sorted(cid for cid, preds in by_case.items() if len(preds) > 1)
        wrong = sorted({r.case_id for r in rs if not r.exact})
        costs = [r.cost_usd for r in rs if r.cost_usd is not None]
        summary[model] = {
            "calls": n,
            "errors": sum(1 for r in rs if r.error),
            "exact_match": sum(r.exact for r in rs) / n,
            "mean_jaccard": statistics.mean(r.jaccard for r in rs),
            "over_selection_rate": sum(1 for r in rs if r.over) / n,
            "under_selection_rate": sum(1 for r in rs if r.under) / n,
            "per_department": per_dept,
            "unstable_cases": unstable,
            "wrong_cases": wrong,
            "mean_cost_usd": statistics.mean(costs) if costs else None,
            "total_cost_usd": sum(costs),
            "p50_ms": statistics.median(r.duration_ms for r in rs),
            "p95_ms": sorted(r.duration_ms for r in rs)[int(0.95 * (n - 1))],
        }
    return summary


def _pct(v: float | None) -> str:
    return "  -  " if v is None else f"{v:.2f}"


def print_table(summary: dict[str, dict[str, Any]]) -> None:
    models = list(summary)
    rows = [
        ("calls", "{calls}"),
        ("errors", "{errors}"),
        ("exact match", "{exact_match:.1%}"),
        ("mean jaccard", "{mean_jaccard:.3f}"),
        ("over-selection", "{over_selection_rate:.1%}"),
        ("under-selection", "{under_selection_rate:.1%}"),
        ("unstable cases", "{n_unstable}"),
        ("mean cost/call", "${mean_cost_usd:.4f}"),
        ("total cost", "${total_cost_usd:.3f}"),
        ("p50 latency", "{p50_ms:.0f} ms"),
        ("p95 latency", "{p95_ms:.0f} ms"),
    ]
    width = max(len(m) for m in models) + 2
    print("\n" + " " * 18 + "".join(m.rjust(width) for m in models))
    for label, fmt in rows:
        cells = []
        for m in models:
            s = dict(summary[m], n_unstable=len(summary[m]["unstable_cases"]))
            cells.append(fmt.format(**s).rjust(width))
        print(label.ljust(18) + "".join(cells))

    print("\nper-department (precision / recall)")
    depts = list(next(iter(summary.values()))["per_department"])
    for d in depts:
        cells = []
        for m in models:
            pr = summary[m]["per_department"][d]
            cells.append(f"{_pct(pr['precision'])} / {_pct(pr['recall'])}".rjust(width))
        print(d.ljust(18) + "".join(cells))

    for m in models:
        s = summary[m]
        if s["wrong_cases"] or s["unstable_cases"]:
            print(f"\n{m}")
            if s["wrong_cases"]:
                print("  wrong:    " + ", ".join(s["wrong_cases"]))
            if s["unstable_cases"]:
                print("  unstable: " + ", ".join(s["unstable_cases"]))


def print_disagreements(results: list[CallResult]) -> None:
    """Show every call whose prediction differed from the label, with the model's reason."""
    print("\ndisagreements with the labels")
    for r in sorted(results, key=lambda r: (r.case_id, r.model, r.repeat)):
        if not r.exact:
            print(f"  {r.case_id} [{r.model} #{r.repeat}] expected={r.expected} got={r.predicted}")
            if r.rationale:
                print(f"      {r.rationale[:200]}")
            if r.error:
                print(f"      ERROR: {r.error}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="estimate cost and exit")
    args = parser.parse_args()

    cases = load_cases()
    n_calls = len(cases) * len(args.models) * args.repeats
    print(
        f"{len(cases)} cases x {len(args.models)} models x {args.repeats} repeats = {n_calls} calls"
    )
    print(f"estimated cost ~${n_calls * EST_COST_PER_CALL:.2f}")
    if args.dry_run:
        return 0

    load_dotenv(REPO_ROOT / ".env")
    skills = load_skills()
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        run_one(case, model, repeat, skills, sem)
        for model in args.models
        for repeat in range(1, args.repeats + 1)
        for case in cases
    ]
    results = list(await asyncio.gather(*tasks))

    summary = summarize(results, cases)
    print_table(summary)
    print_disagreements(results)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(
        json.dumps(
            {"summary": summary, "results": [asdict(r) for r in results]}, indent=2, default=str
        ),
        encoding="utf-8",
    )
    print(f"\nraw results: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(asyncio.run(main()))
