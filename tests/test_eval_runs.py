import json
from pathlib import Path
from typing import Any

import pytest

from blueprint.eval_runs import EvalNotFoundError, latest, load, runs, suites


def make_suite(root: Path, name: str, *result_files: tuple[str, dict[str, Any]]) -> Path:
    suite = root / name
    (suite / "results").mkdir(parents=True)
    (suite / "run.py").write_text("# harness\n", encoding="utf-8")
    for run_id, payload in result_files:
        (suite / "results" / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return suite


def payload(**models: float) -> dict[str, Any]:
    return {
        "summary": {m: {"calls": 60, "exact_match": v} for m, v in models.items()},
        "results": [{"case_id": "c1", "model": m} for m in models],
    }


class TestSuites:
    def test_empty_when_the_directory_does_not_exist(self, tmp_path: Path) -> None:
        assert suites(tmp_path / "nope") == []

    def test_a_directory_without_a_harness_is_not_a_suite(self, tmp_path: Path) -> None:
        (tmp_path / "notes").mkdir()
        make_suite(tmp_path, "skill_matching")
        assert suites(tmp_path) == ["skill_matching"]

    def test_sorted(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "risk_gate")
        make_suite(tmp_path, "skill_matching")
        assert suites(tmp_path) == ["risk_gate", "skill_matching"]


class TestRuns:
    def test_newest_first_by_run_id(self, tmp_path: Path) -> None:
        make_suite(
            tmp_path,
            "skill_matching",
            ("20260101T000000Z", payload(opus=0.9)),
            ("20260914T015734Z", payload(opus=0.95)),
        )
        found = runs("skill_matching", tmp_path)
        assert [r.run_id for r in found] == ["20260914T015734Z", "20260101T000000Z"]

    def test_parses_the_timestamp_out_of_the_run_id(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("20260914T015734Z", payload(opus=0.95)))
        ran_at = runs("skill_matching", tmp_path)[0].ran_at
        assert ran_at is not None
        assert (ran_at.year, ran_at.month, ran_at.day, ran_at.hour) == (2026, 9, 14, 1)

    def test_carries_the_summary_and_counts_the_calls(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("r1", payload(opus=0.95, sonnet=0.917)))
        run = runs("skill_matching", tmp_path)[0]
        assert set(run.summary) == {"opus", "sonnet"}
        assert run.summary["opus"]["exact_match"] == 0.95
        assert run.calls == 2

    def test_a_corrupt_file_is_skipped_rather_than_fatal(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("good", payload(opus=0.9)))
        (tmp_path / "skill_matching" / "results" / "halfwritten.json").write_text(
            '{"summary":', encoding="utf-8"
        )
        assert [r.run_id for r in runs("skill_matching", tmp_path)] == ["good"]

    def test_no_results_directory_is_empty_not_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "skill_matching").mkdir()
        (tmp_path / "skill_matching" / "run.py").write_text("#", encoding="utf-8")
        assert runs("skill_matching", tmp_path) == []

    def test_to_dict_leaves_out_the_per_call_results(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("r1", payload(opus=0.9)))
        as_dict = runs("skill_matching", tmp_path)[0].to_dict()
        assert set(as_dict) == {"suite", "run_id", "ran_at", "summary", "calls"}


class TestLoad:
    def test_returns_the_whole_file(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("r1", payload(opus=0.9)))
        data = load("skill_matching", "r1", tmp_path)
        assert data["results"][0]["case_id"] == "c1"

    def test_missing_run(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("r1", payload(opus=0.9)))
        with pytest.raises(EvalNotFoundError):
            load("skill_matching", "r2", tmp_path)

    @pytest.mark.parametrize(
        ("suite", "run_id"),
        [
            ("../../etc", "r1"),
            ("skill_matching", "../../../secret"),
            ("skill_matching", "r1/../../x"),
            ("", "r1"),
            ("skill_matching", ""),
        ],
    )
    def test_names_that_could_escape_the_directory_are_refused(
        self, tmp_path: Path, suite: str, run_id: str
    ) -> None:
        # These arrive from a URL path, so they are checked before being joined to a path.
        with pytest.raises(ValueError, match="invalid"):
            load(suite, run_id, tmp_path)


class TestLatest:
    def test_one_row_per_suite(self, tmp_path: Path) -> None:
        make_suite(
            tmp_path,
            "skill_matching",
            ("20260101T000000Z", payload(opus=0.9)),
            ("20260914T015734Z", payload(opus=0.95)),
        )
        make_suite(tmp_path, "risk_gate", ("20260201T000000Z", payload(rules=1.0)))
        newest = latest(tmp_path)
        assert [(r.suite, r.run_id) for r in newest] == [
            ("risk_gate", "20260201T000000Z"),
            ("skill_matching", "20260914T015734Z"),
        ]

    def test_a_suite_with_no_runs_is_left_out(self, tmp_path: Path) -> None:
        make_suite(tmp_path, "skill_matching", ("r1", payload(opus=0.9)))
        make_suite(tmp_path, "risk_gate")
        assert [r.suite for r in latest(tmp_path)] == ["skill_matching"]
