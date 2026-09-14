import jsonschema
import pytest

from blueprint.canvas import CATEGORY_QUESTIONS, CanvasCategory, CanvasState
from blueprint.discovery import (
    RECORD_TOOL_FULL_NAME,
    RECORD_TOOL_NAME,
    RECORD_TOOL_SCHEMA,
    build_discovery_options,
    build_record_tool,
    build_system_prompt,
)


class TestSystemPrompt:
    def test_lists_every_category_in_canonical_order(self) -> None:
        prompt = build_system_prompt()
        positions = [prompt.index(f"[`{c.value}`]") for c in CanvasCategory]
        assert positions == sorted(positions)

    def test_includes_every_guiding_question(self) -> None:
        prompt = build_system_prompt()
        for question in CATEGORY_QUESTIONS.values():
            assert question in prompt

    def test_marks_multi_select_categories(self) -> None:
        prompt = build_system_prompt()
        assert "[`input_types`] (multi-select)" in prompt
        assert "[`output_format`] (multi-select)" in prompt
        assert "[`key_stakeholders`] (multi-select)" not in prompt

    def test_names_the_record_tool(self) -> None:
        assert f"`{RECORD_TOOL_NAME}`" in build_system_prompt()


class TestRecordToolSchema:
    def test_category_enum_matches_canvas(self) -> None:
        assert RECORD_TOOL_SCHEMA["properties"]["category"]["enum"] == [
            c.value for c in CanvasCategory
        ]

    def test_accepts_valid_input(self) -> None:
        jsonschema.validate(
            {"category": "key_activities", "summary": "Weekly, team of 4."}, RECORD_TOOL_SCHEMA
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"category": "budget", "summary": "x"},  # not one of the seven
            {"category": "key_activities"},  # missing summary
            {"category": "key_activities", "summary": ""},  # empty summary
            {"category": "key_activities", "summary": "x", "turn": 3},  # model may not set turn
        ],
    )
    def test_rejects_invalid_input(self, bad: dict[str, object]) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, RECORD_TOOL_SCHEMA)


class TestRecordTool:
    async def test_records_with_injected_turn_and_reports_missing(self) -> None:
        state = CanvasState()
        turn = 3
        record = build_record_tool(state, lambda: turn)

        result = await record.handler({"category": "key_stakeholders", "summary": "HR ops team"})

        assert "is_error" not in result
        text = result["content"][0]["text"]
        assert text.startswith("Recorded key_stakeholders (v1).")
        assert "Still uncovered: key_activities, value_proposition" in text
        entry = state.current(CanvasCategory.KEY_STAKEHOLDERS)
        assert entry is not None and entry.turn == 3

    async def test_turn_is_read_at_call_time_not_build_time(self) -> None:
        state = CanvasState()
        clock = {"turn": 1}
        record = build_record_tool(state, lambda: clock["turn"])

        await record.handler({"category": "key_stakeholders", "summary": "first"})
        clock["turn"] = 5
        await record.handler({"category": "key_stakeholders", "summary": "second"})

        assert [e.turn for e in state.history(CanvasCategory.KEY_STAKEHOLDERS)] == [1, 5]

    async def test_re_recording_bumps_version(self) -> None:
        state = CanvasState()
        record = build_record_tool(state, lambda: 1)
        await record.handler({"category": "output_format", "summary": "chatbot"})
        result = await record.handler({"category": "output_format", "summary": "chatbot + summary"})
        assert "Recorded output_format (v2)." in result["content"][0]["text"]

    async def test_reports_completion(self) -> None:
        state = CanvasState()
        record = build_record_tool(state, lambda: 1)
        for cat in CanvasCategory:
            result = await record.handler({"category": cat.value, "summary": f"{cat.value} done"})
        assert "All seven categories are now recorded." in result["content"][0]["text"]
        assert state.is_complete

    async def test_state_validation_error_becomes_tool_error(self) -> None:
        # Bypasses schema validation (which the SDK normally does first) to prove the handler's
        # own boundary check returns an actionable error instead of raising.
        state = CanvasState()
        record = build_record_tool(state, lambda: 1)
        result = await record.handler({"category": "key_stakeholders", "summary": "   "})
        assert result["is_error"] is True
        assert "must not be empty" in result["content"][0]["text"]
        assert state.entries == []


class TestDiscoveryOptions:
    def test_isolation_and_tool_wiring(self) -> None:
        options = build_discovery_options(CanvasState(), lambda: 0)
        assert options.setting_sources == []
        assert options.tools == []
        assert options.allowed_tools == [RECORD_TOOL_FULL_NAME]
        assert isinstance(options.system_prompt, str)
        assert options.include_partial_messages is True
        assert isinstance(options.mcp_servers, dict) and "canvas" in options.mcp_servers

    def test_budget_and_model_pass_through(self) -> None:
        options = build_discovery_options(
            CanvasState(), lambda: 0, model="claude-sonnet-5", max_budget_usd=2.5
        )
        assert options.model == "claude-sonnet-5"
        assert options.max_budget_usd == 2.5
