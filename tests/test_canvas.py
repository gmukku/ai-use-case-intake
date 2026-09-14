from datetime import UTC, datetime

import pytest

from blueprint.canvas import (
    CATEGORY_QUESTIONS,
    MULTI_SELECT_CATEGORIES,
    CanvasCategory,
    CanvasEntry,
    CanvasState,
)


class TestCanvasCategory:
    def test_seven_categories_in_canonical_order(self) -> None:
        assert [c.value for c in CanvasCategory] == [
            "key_stakeholders",
            "key_activities",
            "value_proposition",
            "system_integrations",
            "input_source",
            "input_types",
            "output_format",
        ]

    def test_every_category_has_a_guiding_question(self) -> None:
        assert set(CATEGORY_QUESTIONS) == set(CanvasCategory)
        assert all(q.strip() for q in CATEGORY_QUESTIONS.values())

    def test_label_is_human_readable(self) -> None:
        assert CanvasCategory.KEY_STAKEHOLDERS.label == "Key stakeholders"

    def test_multi_select_categories(self) -> None:
        assert {CanvasCategory.INPUT_TYPES, CanvasCategory.OUTPUT_FORMAT} == MULTI_SELECT_CATEGORIES

    def test_constructible_from_string(self) -> None:
        assert CanvasCategory("input_source") is CanvasCategory.INPUT_SOURCE


class TestCanvasEntry:
    def test_rejects_blank_summary(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            CanvasEntry(category=CanvasCategory.INPUT_TYPES, summary="   ", turn=1)

    def test_rejects_negative_turn(self) -> None:
        with pytest.raises(ValueError, match="turn must be >= 0"):
            CanvasEntry(category=CanvasCategory.INPUT_TYPES, summary="docs", turn=-1)

    def test_is_immutable(self) -> None:
        entry = CanvasEntry(category=CanvasCategory.INPUT_TYPES, summary="docs", turn=1)
        with pytest.raises(AttributeError):
            entry.summary = "changed"  # type: ignore[misc]

    def test_timestamp_is_utc(self) -> None:
        entry = CanvasEntry(category=CanvasCategory.INPUT_TYPES, summary="docs", turn=1)
        assert entry.recorded_at.tzinfo is UTC

    def test_to_dict_round_trips_scalars(self) -> None:
        ts = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
        entry = CanvasEntry(
            category=CanvasCategory.OUTPUT_FORMAT, summary="chatbot", turn=3, recorded_at=ts
        )
        assert entry.to_dict() == {
            "category": "output_format",
            "summary": "chatbot",
            "turn": 3,
            "recorded_at": "2026-09-12T12:00:00+00:00",
        }


class TestCanvasState:
    def test_starts_empty_and_incomplete(self) -> None:
        state = CanvasState()
        assert state.entries == []
        assert state.covered() == []
        assert state.missing() == list(CanvasCategory)
        assert not state.is_complete

    def test_record_accepts_enum_and_string(self) -> None:
        state = CanvasState()
        state.record(CanvasCategory.KEY_STAKEHOLDERS, "HR team", turn=1)
        state.record("key_activities", "manual review", turn=2)
        assert state.covered() == [CanvasCategory.KEY_STAKEHOLDERS, CanvasCategory.KEY_ACTIVITIES]

    def test_record_strips_summary(self) -> None:
        state = CanvasState()
        entry = state.record(CanvasCategory.KEY_STAKEHOLDERS, "  HR team  ", turn=1)
        assert entry.summary == "HR team"

    def test_record_rejects_unknown_category_with_helpful_message(self) -> None:
        state = CanvasState()
        with pytest.raises(ValueError, match=r"unknown canvas category 'budget'.*key_stakeholders"):
            state.record("budget", "lots", turn=1)
        assert state.entries == []  # nothing appended on failure

    def test_record_rejects_blank_summary(self) -> None:
        state = CanvasState()
        with pytest.raises(ValueError, match="must not be empty"):
            state.record(CanvasCategory.KEY_STAKEHOLDERS, "", turn=1)

    def test_re_recording_appends_and_current_is_latest(self) -> None:
        state = CanvasState()
        state.record(CanvasCategory.KEY_STAKEHOLDERS, "HR team", turn=1)
        state.record(CanvasCategory.KEY_STAKEHOLDERS, "HR team + payroll", turn=4)
        hist = state.history(CanvasCategory.KEY_STAKEHOLDERS)
        assert [e.summary for e in hist] == ["HR team", "HR team + payroll"]
        cur = state.current(CanvasCategory.KEY_STAKEHOLDERS)
        assert cur is not None and cur.summary == "HR team + payroll" and cur.turn == 4
        assert len(state.entries) == 2  # log is append-only

    def test_current_is_none_when_uncovered(self) -> None:
        assert CanvasState().current(CanvasCategory.INPUT_SOURCE) is None

    def test_missing_preserves_canonical_order_regardless_of_capture_order(self) -> None:
        state = CanvasState()
        state.record(CanvasCategory.OUTPUT_FORMAT, "chatbot", turn=1)
        state.record(CanvasCategory.KEY_ACTIVITIES, "weekly", turn=2)
        assert state.missing() == [
            CanvasCategory.KEY_STAKEHOLDERS,
            CanvasCategory.VALUE_PROPOSITION,
            CanvasCategory.SYSTEM_INTEGRATIONS,
            CanvasCategory.INPUT_SOURCE,
            CanvasCategory.INPUT_TYPES,
        ]

    def test_complete_after_all_seven(self) -> None:
        state = CanvasState()
        for i, cat in enumerate(CanvasCategory):
            state.record(cat, f"answer {i}", turn=i)
        assert state.is_complete
        assert state.missing() == []
        assert state.covered() == list(CanvasCategory)

    def test_iteration_yields_log_in_insertion_order(self) -> None:
        state = CanvasState()
        state.record(CanvasCategory.OUTPUT_FORMAT, "chatbot", turn=1)
        state.record(CanvasCategory.KEY_ACTIVITIES, "weekly", turn=2)
        assert [e.category for e in state] == [
            CanvasCategory.OUTPUT_FORMAT,
            CanvasCategory.KEY_ACTIVITIES,
        ]

    def test_to_dict_snapshot(self) -> None:
        state = CanvasState()
        state.record(CanvasCategory.KEY_STAKEHOLDERS, "HR", turn=1)
        snap = state.to_dict()
        assert len(snap["entries"]) == 1
        assert snap["current"]["key_stakeholders"] == "HR"
        assert snap["current"]["input_types"] is None
        assert snap["missing"][0] == "key_activities"
        assert snap["is_complete"] is False
