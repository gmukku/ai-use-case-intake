from pathlib import Path
from typing import Any

import jsonschema
import pytest

from blueprint.canvas import CATEGORY_QUESTIONS, CanvasCategory, CanvasState
from blueprint.discovery import (
    EXAMPLES_TOOL_FULL_NAME,
    EXAMPLES_TOOL_NAME,
    RECORD_TOOL_FULL_NAME,
    RECORD_TOOL_NAME,
    RECORD_TOOL_SCHEMA,
    SOP_READ_TOOL_FULL_NAME,
    SOP_SEARCH_TOOL_FULL_NAME,
    WEB_SEARCH_TOOL_FULL_NAME,
    build_discovery_options,
    build_examples_tool,
    build_record_tool,
    build_system_prompt,
    sop_server_config,
)
from blueprint.skills import SKILLS_DIR, load_skills
from blueprint.websearch import SearchResult, WebSearchGuard

SKILLS = load_skills(SKILLS_DIR)


class _NoopSearch:
    async def search(self, query: str) -> list[SearchResult]:
        return []


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

    def test_names_both_tools(self) -> None:
        prompt = build_system_prompt()
        assert f"`{RECORD_TOOL_NAME}`" in prompt
        assert f"`{EXAMPLES_TOOL_NAME}`" in prompt


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


class TestExamplesTool:
    async def test_unmatched_returns_guidance_not_examples(self) -> None:
        examples = build_examples_tool(lambda: [])
        result = await examples.handler({"category": "system_integrations"})
        text = result["content"][0]["text"]
        assert "No department profile has been matched yet" in text
        assert "Salesforce" not in text

    async def test_matched_returns_only_matched_departments_for_that_category(self) -> None:
        examples = build_examples_tool(lambda: [SKILLS["hr"], SKILLS["finance"]])
        result = await examples.handler({"category": "system_integrations"})
        text = result["content"][0]["text"]
        assert "## hr" in text and "## finance" in text
        assert "## sales" not in text and "## customer_success" not in text
        assert "Workday" in text and "NetSuite" in text  # system-integration content
        assert "Greenhouse" in text  # hr systems, not hr stakeholders
        assert "Pick 2-4 and adapt" in text

    async def test_match_is_read_at_call_time(self) -> None:
        current: list[Any] = []
        examples = build_examples_tool(lambda: list(current))
        first = await examples.handler({"category": "output_format"})
        current.append(SKILLS["sales"])
        second = await examples.handler({"category": "output_format"})
        assert "No department profile" in first["content"][0]["text"]
        assert "## sales" in second["content"][0]["text"]


class TestDiscoveryOptions:
    def test_isolation_and_tool_wiring(self) -> None:
        options = build_discovery_options(CanvasState(), lambda: 0, lambda: [])
        assert options.setting_sources == []
        assert options.tools == []
        assert options.allowed_tools == [
            RECORD_TOOL_FULL_NAME,
            EXAMPLES_TOOL_FULL_NAME,
            SOP_SEARCH_TOOL_FULL_NAME,
            SOP_READ_TOOL_FULL_NAME,
        ]
        assert isinstance(options.system_prompt, str)
        assert options.env == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        assert options.include_partial_messages is True
        assert isinstance(options.mcp_servers, dict)
        assert set(options.mcp_servers) == {"canvas", "sop"}

    def test_sop_server_is_an_external_stdio_config(self) -> None:
        import sys

        options = build_discovery_options(CanvasState(), lambda: 0, lambda: [])
        servers: dict[str, Any] = dict(options.mcp_servers)  # type: ignore[arg-type]
        sop = servers["sop"]
        assert sop["type"] == "stdio"
        assert sop["command"] == sys.executable
        assert sop["args"] == ["-m", "blueprint.sop_server"]
        assert "PYTHONPATH" in sop["env"] and "BLUEPRINT_SOP_DIR" in sop["env"]
        # The in-process canvas server is a different shape entirely.
        assert servers["canvas"]["type"] == "sdk"
        prompt = str(options.system_prompt)
        assert "# Looking up existing process documents" in prompt
        assert "`search_sops`" in prompt and "`read_sop`" in prompt
        assert "they are the authority" in prompt

    def test_sop_grounding_can_be_disabled(self) -> None:
        options = build_discovery_options(CanvasState(), lambda: 0, lambda: [], sop_grounding=False)
        assert set(options.mcp_servers) == {"canvas"}  # type: ignore[arg-type]
        assert SOP_SEARCH_TOOL_FULL_NAME not in options.allowed_tools
        assert "search_sops" not in str(options.system_prompt)

    def test_sop_dir_override_reaches_the_server_env(self, tmp_path: Path) -> None:
        cfg = sop_server_config(tmp_path)
        assert cfg["env"]["BLUEPRINT_SOP_DIR"] == str(tmp_path)

    def test_web_search_off_by_default(self) -> None:
        options = build_discovery_options(CanvasState(), lambda: 0, lambda: [])
        assert WEB_SEARCH_TOOL_FULL_NAME not in options.allowed_tools
        assert options.hooks is None
        assert "web_search" not in str(options.system_prompt)

    def test_web_search_wires_tool_prompt_and_hook_together(self) -> None:
        guard = WebSearchGuard(max_calls=2)
        options = build_discovery_options(
            CanvasState(), lambda: 0, lambda: [], web_search=_NoopSearch(), web_search_guard=guard
        )
        assert WEB_SEARCH_TOOL_FULL_NAME in options.allowed_tools
        assert options.hooks is not None
        matchers = options.hooks["PreToolUse"]
        assert len(matchers) == 1 and matchers[0].matcher == WEB_SEARCH_TOOL_FULL_NAME
        assert matchers[0].hooks == [guard.hook]
        prompt = str(options.system_prompt)
        assert "# Web search (regulatory grounding only)" in prompt
        assert "at most 2 times" in prompt
        assert "never include names, company details" in prompt

    def test_budget_and_model_pass_through(self) -> None:
        options = build_discovery_options(
            CanvasState(), lambda: 0, lambda: [], model="claude-sonnet-5", max_budget_usd=2.5
        )
        assert options.model == "claude-sonnet-5"
        assert options.max_budget_usd == 2.5
