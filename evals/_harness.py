"""Shared machinery for the eval suites.

Each suite is a directory with `cases.yaml`, a `run.py`, and a `results/` folder. The
suites differ in what they measure; everything around that — loading labeled cases, running
them with bounded concurrency, writing results in the shape the admin dashboard reads,
printing a comparison table — is the same, and lives here.

The result file is `{"summary": {<variant>: {...metrics}}, "results": [...per-call...]}`.
`blueprint/eval_runs.py` reads exactly that, so a new suite shows up in the dashboard without
the dashboard learning anything about it.

A "variant" is whatever the suite is comparing: a model name usually, but `rules` for a
deterministic suite that has nothing to compare.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CONCURRENCY = 4


class HasVariant(Protocol):
    """What every suite's per-call result must expose for the shared metrics to work.

    Read-only properties rather than plain attributes: a plain attribute in a Protocol must
    be *settable*, which a frozen dataclass is not, so results could not satisfy it.
    """

    @property
    def variant(self) -> str: ...
    @property
    def duration_ms(self) -> int: ...
    @property
    def cost_usd(self) -> float | None: ...
    @property
    def error(self) -> str | None: ...


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Read a suite's labeled cases. A case is a mapping; suites give it meaning."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must hold a non-empty list of cases")
    ids = [str(case.get("id", "")) for case in raw]
    missing = [i for i, cid in enumerate(ids) if not cid]
    if missing:
        raise ValueError(f"{path}: cases at {missing} have no id")
    duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate case ids {duplicates}")
    return list(raw)


async def run_all[R: HasVariant](
    jobs: Sequence[Callable[[], Awaitable[R]]], *, concurrency: int = DEFAULT_CONCURRENCY
) -> list[R]:
    """Run every job with a concurrency cap, so a big suite does not open 200 sockets."""
    limit = asyncio.Semaphore(concurrency)

    async def guarded(job: Callable[[], Awaitable[R]]) -> R:
        async with limit:
            return await job()

    return list(await asyncio.gather(*(guarded(job) for job in jobs)))


def timing_and_cost[R: HasVariant](results: Sequence[R]) -> dict[str, Any]:
    """The metrics every suite reports, whatever it is measuring."""
    if not results:
        return {"calls": 0, "errors": 0}
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    durations = sorted(r.duration_ms for r in results)
    return {
        "calls": len(results),
        "errors": sum(1 for r in results if r.error),
        "mean_cost_usd": statistics.mean(costs) if costs else None,
        "total_cost_usd": sum(costs) if costs else 0.0,
        "p50_ms": statistics.median(durations),
        # ceil rather than int: with int(), n=2 gives index 0 and p95 comes back BELOW
        # p50. A latency number that can be lower than the median is worse than none.
        "p95_ms": durations[min(math.ceil(0.95 * len(durations)) - 1, len(durations) - 1)],
    }


def by_variant[R: HasVariant](results: Sequence[R]) -> dict[str, list[R]]:
    """Group per-call results by the thing being compared."""
    grouped: dict[str, list[R]] = {}
    for result in results:
        grouped.setdefault(result.variant, []).append(result)
    return grouped


def results_path(suite_dir: Path) -> Path:
    """Reserve a timestamped output file, creating the directory if it is not there.

    ``parents=True`` because a new suite's ``results/`` has no parent until its first run, and
    the crash that taught this discarded a whole paid run at the final line.
    """
    results_dir = suite_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"


def write_results_to(
    path: Path, summary: dict[str, dict[str, Any]], results: Sequence[Any]
) -> Path:
    """Write a run to an exact path. Safe to call repeatedly as a run progresses.

    An expensive suite should call this after every case rather than once at the end: a run
    that writes its only artifact on the last line loses everything to any failure there, and
    the transcripts are usually what the run was bought for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": summary, "results": list(results)}, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def write_results(
    suite_dir: Path, summary: dict[str, dict[str, Any]], results: Sequence[Any]
) -> Path:
    """Write the run where the dashboard and `blueprint.eval_runs` will find it."""
    return write_results_to(results_path(suite_dir), summary, results)


def print_table(summary: dict[str, dict[str, Any]], rows: Sequence[tuple[str, str]]) -> None:
    """One column per variant, one row per metric. `rows` are (label, format string)."""
    variants = list(summary)
    width = max((len(label) for label, _ in rows), default=10) + 2
    header = "metric".ljust(width) + "".join(v.rjust(20) for v in variants)
    print("\n" + header)
    print("-" * len(header))
    for label, template in rows:
        cells = []
        for variant in variants:
            try:
                cells.append(template.format(**summary[variant]).rjust(20))
            except (KeyError, ValueError, TypeError):
                cells.append("-".rjust(20))
        print(label.ljust(width) + "".join(cells))
    print()


def common_parser(description: str, *, default_variants: Iterable[str]) -> argparse.ArgumentParser:
    """The flags every suite accepts, so muscle memory carries between them."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(default_variants),
        help="what to compare; a deterministic suite ignores this",
    )
    parser.add_argument("--repeats", type=int, default=1, help="runs per case, for stability")
    parser.add_argument("--limit", type=int, default=None, help="only the first N cases")
    parser.add_argument("--dry-run", action="store_true", help="estimate cost and exit")
    return parser


def confirm_spend(estimate_usd: float, *, calls: int, assume_yes: bool = False) -> bool:
    """Ask before spending real money. An eval that surprises you on cost gets run once."""
    print(f"\n{calls} calls, roughly ${estimate_usd:.2f}.")
    if assume_yes or estimate_usd < 0.25:
        return True
    reply = input("Proceed? [y/N] ").strip().lower()
    return reply in {"y", "yes"}
