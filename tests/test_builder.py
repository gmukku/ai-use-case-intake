import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import HookContext, ResultMessage

from blueprint.builder import (
    ALLOWED_COMMANDS,
    BANNER,
    BUILDER_AGENT_NAME,
    MAX_FILES,
    BuildGuard,
    UnsupportedOutputFormatError,
    build_prototype,
    builder_prompt,
    load_template,
    select_template,
    verify_workspace,
)
from blueprint.review import compile_spec
from blueprint.skills import SKILLS_DIR, load_skills
from tests.test_review import snapshot

SKILLS = load_skills(SKILLS_DIR)


def spec_with(output_format: str) -> Any:
    snap = snapshot()
    snap["canvas"]["current"]["output_format"] = output_format
    return compile_spec(snap, SKILLS)


# -- guard --------------------------------------------------------------------------------------


class TestGuardPolicy:
    def test_parent_may_not_touch_files_or_shell(self, tmp_path: Path) -> None:
        guard = BuildGuard(tmp_path.resolve())
        for tool, args in (
            ("Write", {"file_path": str(tmp_path / "a.py")}),
            ("Read", {"file_path": str(tmp_path / "a.py")}),
            ("Bash", {"command": "python a.py"}),
        ):
            d = guard.check(None, tool, args)
            assert not d.allowed and "Only the builder" in d.reason
            d = guard.check("other-agent", tool, args)
            assert not d.allowed

    def test_builder_writes_inside_only(self, tmp_path: Path) -> None:
        ws = tmp_path.resolve()
        guard = BuildGuard(ws)
        assert guard.check(BUILDER_AGENT_NAME, "Write", {"file_path": str(ws / "qa.py")}).allowed
        assert guard.check(BUILDER_AGENT_NAME, "Write", {"file_path": "knowledge/a.md"}).allowed
        assert guard.check(BUILDER_AGENT_NAME, "Edit", {"file_path": str(ws / "sub" / "x")}).allowed
        outside = guard.check(BUILDER_AGENT_NAME, "Write", {"file_path": str(ws.parent / "x.py")})
        assert not outside.allowed and "inside" in outside.reason
        traversal = guard.check(BUILDER_AGENT_NAME, "Write", {"file_path": "../escape.py"})
        assert not traversal.allowed
        assert not guard.check(BUILDER_AGENT_NAME, "Write", {}).allowed  # no path at all

    def test_reads_are_jailed_too(self, tmp_path: Path) -> None:
        # Reading outside the workspace would pull repo files (.env!) into the model's context.
        ws = tmp_path.resolve()
        guard = BuildGuard(ws)
        assert not guard.check(BUILDER_AGENT_NAME, "Read", {"file_path": "/etc/passwd"}).allowed
        assert not guard.check(BUILDER_AGENT_NAME, "Grep", {"path": str(ws.parent)}).allowed
        assert guard.check(BUILDER_AGENT_NAME, "Grep", {"pattern": "x"}).allowed  # cwd-relative
        assert guard.check(BUILDER_AGENT_NAME, "Glob", {"pattern": "*.py"}).allowed

    @pytest.mark.parametrize(
        "command",
        ["python qa.py", "pytest -q", "ls", "mkdir knowledge", 'python "qa.py"'],
    )
    def test_allowlisted_commands(self, tmp_path: Path, command: str) -> None:
        assert (
            BuildGuard(tmp_path.resolve())
            .check(BUILDER_AGENT_NAME, "Bash", {"command": command})
            .allowed
        )

    @pytest.mark.parametrize(
        ("command", "why"),
        [
            ("rm -rf .", "Only these commands"),
            ("pip install requests", "Only these commands"),
            ("curl http://x", "Only these commands"),
            ("git push", "Only these commands"),
            ("cd /tmp && python x.py", "One plain command"),
            ("python x.py | tee log", "One plain command"),
            ("python x.py > out.txt", "One plain command"),
            ("python $(echo x).py", "One plain command"),
            ("python 'unterminated", "Could not parse"),
            ("", "Empty"),
        ],
    )
    def test_denied_commands(self, tmp_path: Path, command: str, why: str) -> None:
        d = BuildGuard(tmp_path.resolve()).check(BUILDER_AGENT_NAME, "Bash", {"command": command})
        assert not d.allowed and why in d.reason

    def test_absolute_path_argument_outside_workspace_is_denied(self, tmp_path: Path) -> None:
        ws = tmp_path.resolve()
        guard = BuildGuard(ws)
        assert guard.check(
            BUILDER_AGENT_NAME, "Bash", {"command": f"python {ws / 'qa.py'}"}
        ).allowed
        d = guard.check(BUILDER_AGENT_NAME, "Bash", {"command": f"cat {ws.parent / 'secret'}"})
        assert not d.allowed

    def test_unguarded_tools_pass(self, tmp_path: Path) -> None:
        assert BuildGuard(tmp_path.resolve()).check(None, "Task", {"prompt": "x"}).allowed

    def test_allowlist_is_small_and_has_no_network_or_package_tools(self) -> None:
        assert {"python", "pytest", "ls", "dir", "cat", "type", "mkdir"} >= ALLOWED_COMMANDS


class TestGuardHook:
    async def test_hook_allows_explicitly_and_denies_with_reason(self, tmp_path: Path) -> None:
        ws = tmp_path.resolve()
        guard = BuildGuard(ws)
        ctx = HookContext(signal=None)

        def inp(agent: str | None, tool: str, args: dict[str, Any]) -> Any:
            return {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_input": args,
                "agent_type": agent,
                "session_id": "s",
                "transcript_path": "",
                "cwd": str(ws),
            }

        ok: dict[str, Any] = dict(
            await guard.hook(inp("builder", "Write", {"file_path": str(ws / "a.py")}), "t1", ctx)
        )
        assert ok["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "permissionDecisionReason" not in ok["hookSpecificOutput"]

        no: dict[str, Any] = dict(
            await guard.hook(inp(None, "Bash", {"command": "python a.py"}), "t2", ctx)
        )
        assert no["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Only the builder" in no["hookSpecificOutput"]["permissionDecisionReason"]
        assert [d.allowed for d in guard.decisions] == [True, False]
        assert guard.to_dict()["decisions"][1]["tool"] == "Bash"

    def test_matchers_cover_every_guarded_tool(self, tmp_path: Path) -> None:
        (m,) = BuildGuard(tmp_path).matchers()
        assert m.matcher is not None
        for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep", "Bash"):
            assert name in m.matcher


# -- templates and prompts ------------------------------------------------------------------------


class TestTemplates:
    def test_qa_is_selected_for_qa_like_output(self) -> None:
        assert select_template(spec_with("A Q&A chatbot over the policy docs.")) == "qa_chatbot"
        assert (
            select_template(spec_with("Coordinators want to ask questions about it"))
            == "qa_chatbot"
        )

    def test_unsupported_format_raises_with_the_summary(self) -> None:
        with pytest.raises(UnsupportedOutputFormatError, match="invoice comparison"):
            select_template(spec_with("Side-by-side invoice comparison against the PO."))

    def test_template_loads_and_prompt_embeds_spec(self) -> None:
        body = load_template("qa_chatbot")
        assert body.startswith("# Output format: question-and-answer chatbot")
        assert BANNER in body
        prompt = builder_prompt(body, spec_with("Q&A"), Path("/w"))
        assert "Your workspace is `" in prompt
        assert "# Approved discovery spec:" in prompt
        assert "## Key stakeholders" in prompt and "HR ops + payroll" in prompt
        assert "risk" not in prompt.lower()  # the builder never sees the risk assessment

    def test_missing_template(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_template("nope", tmp_path)


# -- verification ---------------------------------------------------------------------------------


GOOD_QA = f'''"""{BANNER} - not production code."""


def add(a: int, b: int) -> int:
    return a + b
'''
GOOD_TEST = f'''"""{BANNER}"""
from qa import add


def test_add() -> None:
    assert add(1, 2) == 3
'''


def write_workspace(
    ws: Path, *, qa: str = GOOD_QA, test: str = GOOD_TEST, readme: bool = True
) -> None:
    (ws / "qa.py").write_text(qa, encoding="utf-8")
    (ws / "test_qa.py").write_text(test, encoding="utf-8")
    if readme:
        (ws / "README.md").write_text(f"# {BANNER}\n\nA tiny stub.\n", encoding="utf-8")
    (ws / "knowledge").mkdir(exist_ok=True)
    (ws / "knowledge" / "a.md").write_text("Alpha document.\n", encoding="utf-8")


class TestVerifyWorkspace:
    def test_secrets_are_stripped_from_the_test_environment(self) -> None:
        from blueprint.builder import _scrubbed_env

        os.environ["BLUEPRINT_TEST_FAKE_KEY"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        try:
            env = _scrubbed_env()
        finally:
            del os.environ["BLUEPRINT_TEST_FAKE_KEY"]
            del os.environ["ANTHROPIC_API_KEY"]
        assert "BLUEPRINT_TEST_FAKE_KEY" not in env and "ANTHROPIC_API_KEY" not in env
        assert "PATH" in env and env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_good_workspace_passes_with_real_pytest(self, tmp_path: Path) -> None:
        write_workspace(tmp_path)
        v = verify_workspace(tmp_path)
        assert v.ok, v.test_output
        assert v.tests_passed and v.violations == ()
        assert v.files == ("README.md", "knowledge/a.md", "qa.py", "test_qa.py")

    def test_failing_tests_are_reported(self, tmp_path: Path) -> None:
        write_workspace(tmp_path, test=GOOD_TEST.replace("== 3", "== 4"))
        v = verify_workspace(tmp_path)
        assert not v.ok and not v.tests_passed and "1 failed" in v.test_output

    def test_missing_banner_readme_and_tests_are_violations(self, tmp_path: Path) -> None:
        write_workspace(tmp_path, qa="x = 1\n", readme=False)
        (tmp_path / "test_qa.py").unlink()
        v = verify_workspace(tmp_path, run_tests=False)
        assert set(v.violations) == {
            "qa.py: missing prototype banner",
            "README.md missing",
            "no test file",
        }

    def test_file_limit(self, tmp_path: Path) -> None:
        write_workspace(tmp_path)
        for i in range(MAX_FILES):
            (tmp_path / f"extra{i}.txt").write_text("x", encoding="utf-8")
        v = verify_workspace(tmp_path, run_tests=False)
        assert any("exceeds the limit" in x for x in v.violations)


# -- the run, with a fake model that writes the files itself --------------------------------------


class TestBuildPrototype:
    async def test_end_to_end_with_fake_query(self, tmp_path: Path) -> None:
        ws = tmp_path / "proto"
        seen: dict[str, Any] = {}

        async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            seen["prompt"], seen["options"] = prompt, options
            write_workspace(ws)  # the "model" builds the prototype
            yield ResultMessage(
                subtype="success",
                duration_ms=5000,
                duration_api_ms=4000,
                is_error=False,
                num_turns=6,
                session_id="build-1",
                total_cost_usd=0.42,
                result="Wrote README.md, knowledge/a.md, qa.py, test_qa.py. 1 passed.",
            )

        spec = spec_with("A Q&A chatbot over the checklist.")
        result = await build_prototype(spec, workspace=ws, query_fn=fake_query)

        assert result.ok and result.template == "qa_chatbot"
        assert result.verification.tests_passed
        assert result.report.startswith("Wrote README.md")
        assert result.cost_usd == 0.42 and result.spec_version == 1
        assert result.session_id == "sess-42"

        opts = seen["options"]
        assert opts.cwd == str(ws.resolve())
        assert opts.permission_mode == "acceptEdits"
        assert opts.setting_sources == [] and "Task" in opts.tools
        assert set(opts.agents) == {"builder"}
        assert opts.agents["builder"].tools == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
        assert "PreToolUse" in opts.hooks and opts.max_budget_usd == 1.5
        assert "HR ops + payroll" in opts.agents["builder"].prompt
        assert seen["prompt"].startswith("Build the prototype for:")
        assert result.to_dict()["verification"]["ok"] is True

    async def test_unsupported_format_never_starts_a_run(self, tmp_path: Path) -> None:
        async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            if prompt:  # always true; keeps this an async generator without dead code
                raise AssertionError("must not be called")
            yield

        with pytest.raises(UnsupportedOutputFormatError):
            await build_prototype(
                spec_with("invoice comparison"), workspace=tmp_path, query_fn=fake_query
            )

    async def test_run_error_is_captured_not_raised(self, tmp_path: Path) -> None:
        from claude_agent_sdk import ResultError

        async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
            if prompt:
                raise ResultError("Claude Code returned an error result: budget exceeded")
            yield

        result = await build_prototype(
            spec_with("Q&A"), workspace=tmp_path / "w", query_fn=fake_query
        )
        assert result.is_error and "budget exceeded" in result.errors[0]
        assert not result.ok and result.verification.files == ()
