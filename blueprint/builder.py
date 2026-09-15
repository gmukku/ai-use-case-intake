"""The Builder: an SDK subagent that scaffolds a small, labeled prototype from an approved spec.

Shape:
    parent agent (no file tools of its own) --Agent tool--> ``builder`` subagent (Read/Write/
    Edit/Glob/Grep/Bash, jailed to one workspace) --> one report back --> Python verifies.

Permission model, layered:
    - ``tools`` on the session is the universe; ``AgentDefinition.tools`` narrows it per agent.
      (A subagent cannot use a tool the session does not carry: learned by probe.)
    - ``cwd`` is the workspace and ``permission_mode="acceptEdits"`` covers file edits there.
    - ``BuildGuard`` (a ``PreToolUse`` hook) is the actual policy: the parent may not touch
      files or run commands at all; the builder may only read/write inside the workspace and
      run a short allowlist of single, operator-free commands. Vetted calls are *allowed*
      explicitly so the CLI never prompts; everything else is denied with a reason the model
      reads. Every decision is recorded for the trace.
    - ``max_budget_usd`` and ``max_turns`` are the stop.

Verification is done by Python after the run (``pytest`` in the workspace, file limits, the
prototype banner), never by trusting the model's own report.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookContext,
    HookInput,
    HookMatcher,
    Message,
    ResultMessage,
    query,
)
from claude_agent_sdk.types import HookJSONOutput, PreToolUseHookSpecificOutput, SyncHookJSONOutput

from blueprint.isolation import AGENT_ENV
from blueprint.review import DiscoverySpec
from blueprint.skills import parse_skill_frontmatter

logger = logging.getLogger(__name__)

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
BUILDER_SKILLS_DIR: Final = _REPO_ROOT / "builder_skills"
PROTOTYPES_DIR: Final = _REPO_ROOT / "prototypes"

DEFAULT_BUILDER_MODEL: Final = "claude-opus-5"
DEFAULT_BUILD_BUDGET_USD: Final = 1.5
BUILD_MAX_TURNS: Final = 40
VERIFY_TIMEOUT_S: Final = 120

BUILDER_AGENT_NAME: Final = "builder"
FILE_TOOLS: Final = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS: Final = frozenset({"Read", "Glob", "Grep"})
SESSION_TOOLS: Final = ["Task", "Read", "Write", "Edit", "Glob", "Grep", "Bash"]
BUILDER_TOOLS: Final = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# Bash policy: one command, first token from this list, no shell operators, no absolute paths
# outside the workspace. The model adapts to denials (seen in the probe); keep this strict.
ALLOWED_COMMANDS: Final = frozenset({"python", "pytest", "ls", "dir", "cat", "type", "mkdir"})
_SHELL_OPERATORS: Final = re.compile(r"(&&|\|\||;|\||>|<|`|\$\()")

# Prototype limits, checked after the run.
MAX_FILES: Final = 8
MAX_TOTAL_LINES: Final = 450
BANNER: Final = "ILLUSTRATIVE PROTOTYPE"

# Which template a spec's output_format summary maps to. Only the simplest format exists yet;
# others raise so the CLI can say so instead of building the wrong thing.
_QA_RE: Final = re.compile(r"\b(question|q&a|qa|chatbot|chat bot|ask questions?)\b", re.IGNORECASE)


class UnsupportedOutputFormatError(ValueError):
    """The spec asks for an output format this Builder has no template for yet."""


class BuildError(RuntimeError):
    """The build run failed before producing a result."""


def select_template(spec: DiscoverySpec) -> str:
    """Pick the output-format template for a spec, or raise if none applies."""
    output = next(c.summary for c in spec.categories if c.category.value == "output_format")
    if _QA_RE.search(output):
        return "qa_chatbot"
    raise UnsupportedOutputFormatError(
        "no Builder template matches this output format yet (only 'qa_chatbot' exists); "
        f"output_format was: {output[:160]}"
    )


def load_template(name: str, root: Path = BUILDER_SKILLS_DIR) -> str:
    """Return a builder skill's markdown body (frontmatter stripped) for the subagent prompt."""
    path = root / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"builder template not found: {path}")
    _, body = parse_skill_frontmatter(path.read_text(encoding="utf-8"), source=str(path))
    return body.strip()


# -- the guard: a PreToolUse hook -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """One tool call the guard saw, and what it decided."""

    agent: str | None
    tool: str
    target: str
    allowed: bool
    reason: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "agent": self.agent,
            "tool": self.tool,
            "target": self.target,
            "allowed": self.allowed,
            "reason": self.reason,
            "at": self.at.isoformat(),
        }


@dataclass(slots=True)
class BuildGuard:
    """Jail the builder to its workspace; deny the parent any file or shell access."""

    workspace: Path
    decisions: list[GuardDecision] = field(default_factory=list)

    def _inside(self, raw: str) -> bool:
        path = Path(raw)
        resolved = (self.workspace / path if not path.is_absolute() else path).resolve()
        return resolved == self.workspace or self.workspace in resolved.parents

    def check(self, agent: str | None, tool: str, args: Mapping[str, Any]) -> GuardDecision:
        """Pure policy: decide without touching the SDK, so it is unit-testable."""
        guarded = tool in FILE_TOOLS or tool in READ_TOOLS or tool == "Bash"
        if guarded and agent != BUILDER_AGENT_NAME:
            return GuardDecision(
                agent, tool, "", False, "Only the builder subagent may use file or shell tools."
            )
        if tool in FILE_TOOLS or tool in READ_TOOLS:
            target = str(
                args.get("file_path") or args.get("path") or args.get("notebook_path") or ""
            )
            if tool in ("Glob", "Grep") and not target:
                return GuardDecision(agent, tool, "", True, "workspace-relative search")
            if not target:
                return GuardDecision(agent, tool, "", False, "No file path given.")
            if not self._inside(target):
                return GuardDecision(
                    agent, tool, target, False, f"Paths must stay inside {self.workspace}."
                )
            return GuardDecision(agent, tool, target, True, "inside workspace")
        if tool == "Bash":
            command = str(args.get("command", "")).strip()
            return self._check_command(agent, command)
        return GuardDecision(agent, tool, "", True, "not a guarded tool")

    def _check_command(self, agent: str | None, command: str) -> GuardDecision:
        if not command:
            return GuardDecision(agent, "Bash", "", False, "Empty command.")
        if _SHELL_OPERATORS.search(command):
            return GuardDecision(
                agent, "Bash", command, False, "One plain command only: no &&, |, ;, redirects."
            )
        try:
            # Forward slashes so POSIX-mode shlex does not eat Windows path separators.
            tokens = shlex.split(command.replace("\\", "/"), posix=True)
        except ValueError:
            return GuardDecision(agent, "Bash", command, False, "Could not parse the command.")
        if not tokens or Path(tokens[0]).name not in ALLOWED_COMMANDS:
            return GuardDecision(
                agent,
                "Bash",
                command,
                False,
                f"Only these commands are allowed: {', '.join(sorted(ALLOWED_COMMANDS))}.",
            )
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            if Path(token).is_absolute() and not self._inside(token):
                return GuardDecision(
                    agent, "Bash", command, False, f"Paths must stay inside {self.workspace}."
                )
        return GuardDecision(agent, "Bash", command, True, "allowlisted command")

    async def hook(
        self, input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        """The ``PreToolUse`` callback: allow vetted calls explicitly, deny the rest."""
        agent = input_data.get("agent_type")
        tool = str(input_data.get("tool_name", ""))
        args = input_data.get("tool_input")
        decision = self.check(
            str(agent) if agent else None, tool, args if isinstance(args, dict) else {}
        )
        self.decisions.append(decision)
        logger.log(
            logging.INFO if decision.allowed else logging.WARNING,
            "build.guard",
            extra={
                "agent": decision.agent,
                "tool": decision.tool,
                "target": decision.target[:160],
                "allowed": decision.allowed,
                "reason": decision.reason,
            },
        )
        spec: PreToolUseHookSpecificOutput = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if decision.allowed else "deny",
        }
        if not decision.allowed:
            spec["permissionDecisionReason"] = decision.reason
        out: SyncHookJSONOutput = {"hookSpecificOutput": spec}
        return out

    def matchers(self) -> list[HookMatcher]:
        """Register on every guarded tool name."""
        names = "|".join(sorted(FILE_TOOLS | READ_TOOLS | {"Bash"}))
        return [HookMatcher(matcher=names, hooks=[self.hook])]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "workspace": str(self.workspace),
            "decisions": [d.to_dict() for d in self.decisions],
        }


# -- prompts ----------------------------------------------------------------------------------


def render_spec_for_builder(spec: DiscoverySpec) -> str:
    """The spec as the builder sees it: title, narrative, the seven summaries. No risk/trace."""
    lines = [f"# Approved discovery spec: {spec.title}", "", spec.narrative, ""]
    for c in spec.categories:
        lines.append(f"## {c.category.label}")
        lines.append(c.summary)
        lines.append("")
    return "\n".join(lines).strip()


def builder_prompt(template_body: str, spec: DiscoverySpec, workspace: Path) -> str:
    """System prompt for the ``builder`` subagent."""
    return (
        f"You are the Builder. Your workspace is `{workspace}`; every file you create goes "
        "there, using absolute paths. You may run `python` and `pytest` there. Nothing "
        "outside the workspace is readable or writable, and no other commands are allowed; if "
        "a call is denied, do not retry it, adapt.\n\n"
        "Build only what the output-format template below specifies, tailored to the spec. "
        "Use the stakeholder's domain for the synthetic documents and examples. Stay within "
        "the limits; smaller is better.\n\n"
        f"{template_body}\n\n---\n\n{render_spec_for_builder(spec)}"
    )


PARENT_SYSTEM_PROMPT: Final = (
    "You coordinate one build. Delegate the entire job to the `builder` subagent in a single "
    "Agent call, passing along that it should follow its instructions and report the files "
    "written and the test output. You have no file or shell tools yourself. When the builder "
    "reports back, reply with its report, verbatim, and nothing else."
)


# -- the run ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verification:
    """Python's own check of the workspace after the run."""

    tests_passed: bool
    test_output: str
    files: tuple[str, ...]
    total_lines: int
    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Tests pass and no limit or banner violation."""
        return self.tests_passed and not self.violations

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "ok": self.ok,
            "tests_passed": self.tests_passed,
            "test_output": self.test_output[-4000:],
            "files": list(self.files),
            "total_lines": self.total_lines,
            "violations": list(self.violations),
        }


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Everything about one build, for the snapshot and the views."""

    session_id: str
    spec_version: int
    template: str
    workspace: str
    report: str
    """The builder's own report, relayed by the parent."""
    verification: Verification
    guard: dict[str, Any]
    model: str
    cost_usd: float | None
    duration_ms: int
    is_error: bool
    errors: tuple[str, ...]
    built_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        """The run finished and verification passed."""
        return not self.is_error and self.verification.ok

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "spec_version": self.spec_version,
            "template": self.template,
            "workspace": self.workspace,
            "report": self.report,
            "verification": self.verification.to_dict(),
            "guard": self.guard,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "is_error": self.is_error,
            "errors": list(self.errors),
            "built_at": self.built_at.isoformat(),
        }


# Secrets must not reach a process running model-written code. Everything else (PATH,
# SYSTEMROOT, TEMP) is needed for Python to start at all, so inherit and strip.
_SECRET_ENV_RE: Final = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)|^AWS_|^ANTHROPIC_|^TAVILY_|^CLAUDE_", re.IGNORECASE
)


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def verify_workspace(workspace: Path, *, run_tests: bool = True) -> Verification:
    """Check limits and the banner, then run ``pytest`` in the workspace."""
    files = sorted(
        p.relative_to(workspace).as_posix()
        for p in workspace.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    )
    violations: list[str] = []
    total_lines = 0
    for rel in files:
        text = (workspace / rel).read_text(encoding="utf-8", errors="replace")
        total_lines += text.count("\n") + 1
        if rel.endswith(".py") and BANNER not in text[:600]:
            violations.append(f"{rel}: missing prototype banner")
    if "README.md" not in files:
        violations.append("README.md missing")
    elif BANNER not in (workspace / "README.md").read_text(encoding="utf-8", errors="replace"):
        violations.append("README.md: missing prototype banner")
    if len(files) > MAX_FILES:
        violations.append(f"{len(files)} files exceeds the limit of {MAX_FILES}")
    if total_lines > MAX_TOTAL_LINES:
        violations.append(f"{total_lines} lines exceeds the limit of {MAX_TOTAL_LINES}")
    if not any(f.startswith("test_") and f.endswith(".py") for f in files):
        violations.append("no test file")

    tests_passed, output = False, "(tests not run)"
    has_tests = any(f.startswith("test_") and f.endswith(".py") for f in files)
    if run_tests and has_tests:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=VERIFY_TIMEOUT_S,
                env=_scrubbed_env(),
            )
            tests_passed, output = proc.returncode == 0, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            output = f"pytest timed out after {VERIFY_TIMEOUT_S}s"
    return Verification(
        tests_passed=tests_passed,
        test_output=output,
        files=tuple(files),
        total_lines=total_lines,
        violations=tuple(violations),
    )


QueryFn = Callable[..., AsyncIterator[Message]]


async def build_prototype(
    spec: DiscoverySpec,
    *,
    workspace: Path,
    model: str = DEFAULT_BUILDER_MODEL,
    max_budget_usd: float = DEFAULT_BUILD_BUDGET_USD,
    template_root: Path = BUILDER_SKILLS_DIR,
    query_fn: QueryFn = query,
) -> BuildResult:
    """Run the parent + builder subagent against ``spec`` inside ``workspace``, then verify.

    Raises:
        UnsupportedOutputFormatError: if no template matches the spec.
        BuildError: if the run produced no result at all.
    """
    template = select_template(spec)
    body = load_template(template, template_root)
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    guard = BuildGuard(workspace)

    builder = AgentDefinition(
        description="Scaffolds a small labeled prototype in the workspace from an approved spec.",
        prompt=builder_prompt(body, spec, workspace),
        tools=BUILDER_TOOLS,
        model="inherit",
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=PARENT_SYSTEM_PROMPT,
        setting_sources=[],
        env=AGENT_ENV,
        cwd=str(workspace),
        permission_mode="acceptEdits",
        tools=SESSION_TOOLS,
        agents={BUILDER_AGENT_NAME: builder},
        hooks={"PreToolUse": guard.matchers()},
        max_turns=BUILD_MAX_TURNS,
        max_budget_usd=max_budget_usd,
    )

    started = datetime.now(UTC)
    logger.info(
        "build.start",
        extra={"session_id": spec.session_id, "template": template, "workspace": str(workspace)},
    )
    result: ResultMessage | None = None
    failure: str | None = None
    try:
        async for message in query_fn(
            prompt=f"Build the prototype for: {spec.title}", options=options
        ):
            if isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        failure = f"build run failed: {exc}"

    if result is None and failure is None:
        raise BuildError("build run produced no ResultMessage")

    verification = verify_workspace(workspace)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    build = BuildResult(
        session_id=spec.session_id,
        spec_version=spec.version,
        template=template,
        workspace=str(workspace),
        report=(result.result or "") if result else "",
        verification=verification,
        guard=guard.to_dict(),
        model=model,
        cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else duration_ms,
        is_error=(result.is_error if result else True),
        errors=tuple(result.errors or ()) if result else (failure or "unknown failure",),
    )
    logger.info(
        "build.end",
        extra={
            "session_id": spec.session_id,
            "ok": build.ok,
            "files": list(verification.files),
            "tests_passed": verification.tests_passed,
            "violations": list(verification.violations),
            "denied": sum(1 for d in guard.decisions if not d.allowed),
            "cost_usd": build.cost_usd,
        },
    )
    return build
