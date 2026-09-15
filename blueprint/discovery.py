"""The discovery agent: system prompt, canvas-recording tool, and SDK options.

This module *configures* the model's side of the conversation. It owns no state of its own:
the orchestrator owns the ``CanvasState`` and the turn counter and injects both, so the
tool defined here is a thin, validated bridge from model output to Python state.

Split of responsibilities (keep it this way):
    - Fixed (deterministic): the seven categories, their order, the tool's input schema.
    - Adaptive (the model's latitude): which follow-up to ask, when a category is
      "sufficiently" answered, which examples to offer.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import HookEvent, McpServerConfig, McpStdioServerConfig

from blueprint.canvas import (
    CATEGORY_QUESTIONS,
    MULTI_SELECT_CATEGORIES,
    CanvasCategory,
    CanvasEntry,
    CanvasState,
)
from blueprint.completeness import Assessment
from blueprint.isolation import AGENT_ENV
from blueprint.skills import DepartmentSkill
from blueprint.sop_server import LIST_TOOL as SOP_LIST_TOOL
from blueprint.sop_server import READ_TOOL as SOP_READ_TOOL
from blueprint.sop_server import SEARCH_TOOL as SOP_SEARCH_TOOL
from blueprint.sop_server import SERVER_NAME as SOP_SERVER_NAME
from blueprint.sops import SOP_DIR
from blueprint.websearch import (
    TOOL_NAME as WEB_SEARCH_TOOL_NAME,
)
from blueprint.websearch import SearchClient, WebSearchGuard, build_web_search_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------
# Tool naming. The SDK exposes in-process MCP tools to the model as ``mcp__<server>__<tool>``.
# ---------------------------------------------------------------------------------------
CANVAS_SERVER_NAME: Final = "canvas"
RECORD_TOOL_NAME: Final = "record_canvas_answer"
RECORD_TOOL_FULL_NAME: Final = f"mcp__{CANVAS_SERVER_NAME}__{RECORD_TOOL_NAME}"
EXAMPLES_TOOL_NAME: Final = "department_examples"
EXAMPLES_TOOL_FULL_NAME: Final = f"mcp__{CANVAS_SERVER_NAME}__{EXAMPLES_TOOL_NAME}"
WEB_SEARCH_TOOL_FULL_NAME: Final = f"mcp__{CANVAS_SERVER_NAME}__{WEB_SEARCH_TOOL_NAME}"
# External stdio server: the SDK names its tools mcp__<server>__<tool> exactly like ours.
SOP_SEARCH_TOOL_FULL_NAME: Final = f"mcp__{SOP_SERVER_NAME}__{SOP_SEARCH_TOOL}"
SOP_READ_TOOL_FULL_NAME: Final = f"mcp__{SOP_SERVER_NAME}__{SOP_READ_TOOL}"
SOP_LIST_TOOL_FULL_NAME: Final = f"mcp__{SOP_SERVER_NAME}__{SOP_LIST_TOOL}"

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent

# Callable the orchestrator supplies: which department skills currently apply (may be empty
# before the classifier has run).
MatchedSkills = Callable[[], list[DepartmentSkill]]

# Callable the orchestrator supplies for the completeness check: (entry, version) ->
# (assessment or None, should the model ask the follow-up now).
AssessFn = Callable[[CanvasEntry, int], Awaitable[tuple[Assessment | None, bool]]]

DEFAULT_MODEL: Final = "claude-opus-5"

# A discovery *turn* is: stakeholder speaks -> model may call the tool a few times -> model
# replies. Each tool round-trip is an agentic turn in SDK terms, so leave headroom above 1.
MAX_AGENTIC_TURNS_PER_QUERY: Final = 8

# JSON Schema for the tool. ``enum`` makes the category set a hard constraint enforced by the
# SDK before our handler runs: the deterministic backbone expressed at the schema layer.
RECORD_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in CanvasCategory],
            "description": "Which canvas category this answer covers.",
        },
        "summary": {
            "type": "string",
            "minLength": 1,
            "description": (
                "1-3 concrete sentences capturing what the stakeholder said for this category. "
                "Use their specifics (team names, system names, frequencies, numbers). "
                "For multi-select categories list every option they chose."
            ),
        },
    },
    "required": ["category", "summary"],
    "additionalProperties": False,
}

EXAMPLES_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in CanvasCategory],
            "description": "The canvas category the stakeholder wants examples for.",
        },
    },
    "required": ["category"],
    "additionalProperties": False,
}


def _render_canvas_section() -> str:
    """Render the seven categories, in order, with their guiding questions."""
    lines: list[str] = []
    for i, cat in enumerate(CanvasCategory, start=1):
        multi = " (multi-select)" if cat in MULTI_SELECT_CATEGORIES else ""
        lines.append(f"{i}. {cat.label} [`{cat.value}`]{multi}: {CATEGORY_QUESTIONS[cat]}")
    return "\n".join(lines)


def sop_server_config(sop_dir: Path = SOP_DIR) -> McpStdioServerConfig:
    """Stdio launch config for the SOP library server.

    The SDK spawns ``<this interpreter> -m blueprint.sop_server`` and speaks MCP to it over
    stdin/stdout. ``sys.executable`` guarantees the venv that has our package; ``PYTHONPATH``
    makes the package importable regardless of the spawn cwd; ``PYTHONIOENCODING`` keeps the
    transport UTF-8 on Windows consoles.
    """
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "blueprint.sop_server"],
        "env": {
            "PYTHONPATH": str(_REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "BLUEPRINT_SOP_DIR": str(sop_dir),
        },
    }


def _sop_section() -> str:
    return f"""

# Looking up existing process documents

You have access to the company's internal SOP library. When the stakeholder refers to an \
existing process, checklist, policy, or procedure (for example "our onboarding checklist", \
"the expense policy", "how we escalate tickets"), call `{SOP_SEARCH_TOOL}` with a short topic \
before asking your next question, and `{SOP_READ_TOOL}` if a match looks relevant. Use what \
the SOP says to ask ONE sharper follow-up: whether a documented step, deadline, or control is \
actually part of what they do today, or where their practice differs from the document. \
Mention the SOP title in the recorded summary when it informed the capture. Never recite the \
SOP to the stakeholder or treat it as the truth about their process; they are the authority \
on what actually happens."""


def _web_search_section(max_searches: int) -> str:
    return f"""

# Web search (regulatory grounding only)

You may call `{WEB_SEARCH_TOOL_NAME}` at most {max_searches} times in this conversation, and \
only when the stakeholder mentions a government or regulatory requirement (tax or employment \
forms, leave laws, filing deadlines, eligibility rules). Use what you find to ask ONE sharper \
follow-up, e.g. whether a specific requirement is part of their process. Never use it to \
answer the stakeholder's questions or to give advice. Queries must be generic public-topic \
phrases; never include names, company details, or anything the stakeholder said verbatim. \
If you rely on a result, mention the source title in the recorded summary."""


def build_system_prompt(*, web_search_max: int | None = None, sop_grounding: bool = False) -> str:
    """Build the discovery system prompt from the canvas definition.

    Derived from ``canvas.py`` rather than hand-written so the prompt and the state machine
    can never disagree about what the categories are.

    Args:
        web_search_max: When set, adds the scoped web-search section with this per-session cap.
        sop_grounding: When true, adds the SOP-library section.
    """
    extra = (_sop_section() if sop_grounding else "") + (
        _web_search_section(web_search_max) if web_search_max is not None else ""
    )
    return f"""You are a discovery consultant helping a non-technical stakeholder shape an idea \
for an AI or automation use case. Your job is to run a structured discovery conversation, \
then hand a clear spec to a human reviewer. You do not build anything.

# The discovery canvas

Every conversation covers all seven categories below, in this order. None may be skipped.

{_render_canvas_section()}

# How to run the conversation

- Work through the categories in order. If the stakeholder volunteers information about a \
later category early, record it, but keep steering back to the current one.
- A category is sufficiently answered when you could write 1-3 concrete sentences about it. \
If you cannot, ask ONE focused follow-up grounded in what they have already said. Do not \
interrogate: if an answer is reasonable, accept it and move on.
- Ask one question at a time. Keep each message short and in plain language. No jargon.
- If they ask for an example ("what do you mean by system integration?"), first call \
`{EXAMPLES_TOOL_NAME}` for the category you are on. It returns examples from department \
profiles matched to their team. Pick 2-4 and adapt them to what they have told you so far \
(their team, their systems, their process). Never paste the list verbatim, and never give a \
generic list.
- Stay strictly on discovery. Do not propose architectures, models, or vendors, and do not \
promise what will be built.

# Recording answers (silent bookkeeping)

As soon as a category is sufficiently answered, call `{RECORD_TOOL_NAME}` with that category \
and a 1-3 sentence summary in the stakeholder's own specifics. Call it again for the same \
category if they later add or change something. You may record more than one category in a \
single turn if their message covered several.

The stakeholder must never know this bookkeeping exists. Never mention categories, the canvas, \
tools, recording, summaries, or reviewers in your messages until the end.{extra}

# Finishing

Once all seven categories are recorded, give the stakeholder a brief plain-language recap of \
what you understood (a short paragraph, not a list of categories), and tell them a reviewer \
will look it over before anything is built."""


def build_record_tool(
    state: CanvasState,
    current_turn: Callable[[], int],
    assess: AssessFn | None = None,
) -> SdkMcpTool[Any]:
    """Create the ``record_canvas_answer`` tool bound to one conversation's state.

    Args:
        state: The append-only canvas log the orchestrator owns.
        current_turn: Zero-arg callable returning the current stakeholder turn number. Injected
            (not passed by the model) so the model cannot misattribute a capture to another turn.
        assess: Optional completeness check, called with the recorded entry and its version.
            Returns ``(assessment, should_ask)``: the verdict, and whether the model should ask
            the follow-up now (the orchestrator caps clarification rounds). ``None`` for the
            assessment means the check was disabled or failed; recording proceeds regardless.
    """

    @tool(
        RECORD_TOOL_NAME,
        "Record that a canvas category has been sufficiently answered by the stakeholder. "
        "Call once per category as soon as it is covered; call again to update. The result "
        "tells you if the summary has gaps you should ask about before moving on.",
        RECORD_TOOL_SCHEMA,
    )
    async def record_canvas_answer(args: dict[str, Any]) -> dict[str, Any]:
        turn = current_turn()
        try:
            entry = state.record(args["category"], args["summary"], turn=turn)
        except ValueError as exc:
            # Feed the validation message back to the model so it can self-correct.
            logger.warning("discovery.record.rejected", extra={"turn": turn, "error": str(exc)})
            return {"content": [{"type": "text", "text": f"Rejected: {exc}"}], "is_error": True}

        version = len(state.history(entry.category))
        logger.info(
            "discovery.record.ok",
            extra={"category": entry.category.value, "turn": turn, "version": version},
        )

        verdict = ""
        if assess is not None:
            assessment, should_ask = await assess(entry, version)
            if assessment is not None and not assessment.sufficient:
                gaps = ", ".join(assessment.missing)
                if should_ask:
                    verdict = (
                        f" Incomplete: missing {gaps}. Before moving on, ask something like: "
                        f'"{assessment.question}"'
                    )
                else:
                    verdict = (
                        f" Gaps remain ({gaps}) but do not ask again; they are noted for the "
                        "reviewer. Move on."
                    )
            elif assessment is not None:
                verdict = " Sufficient."

        missing = state.missing()
        status = (
            "All seven categories are now recorded."
            if not missing
            else "Still uncovered: " + ", ".join(c.value for c in missing)
        )
        text = f"Recorded {entry.category.value} (v{version}).{verdict} {status}"
        return {"content": [{"type": "text", "text": text}]}

    return record_canvas_answer


def build_examples_tool(matched_skills: MatchedSkills) -> SdkMcpTool[Any]:
    """Create the ``department_examples`` tool: per-category examples from matched departments.

    Python decides which departments apply (the classifier's output, read at call time), so
    the model never chooses a department itself. Every retrieval is logged with exactly what
    was returned, which is what the audit layer needs.
    """

    @tool(
        EXAMPLES_TOOL_NAME,
        "Get concrete examples for a canvas category, drawn from department profiles matched "
        "to this stakeholder. Call this before offering examples; adapt what it returns.",
        EXAMPLES_TOOL_SCHEMA,
    )
    async def department_examples(args: dict[str, Any]) -> dict[str, Any]:
        category = CanvasCategory(args["category"])
        skills = matched_skills()
        if not skills:
            logger.info("discovery.examples.unmatched", extra={"category": category.value})
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "No department profile has been matched yet. Offer examples from "
                            "your own judgment, grounded in what the stakeholder has said."
                        ),
                    }
                ]
            }

        sections = []
        for skill in skills:
            bullets = "\n".join(f"- {b}" for b in skill.examples[category])
            sections.append(f"## {skill.name}\n{bullets}")
        logger.info(
            "discovery.examples.retrieved",
            extra={
                "category": category.value,
                "departments": [s.name for s in skills],
                "count": sum(len(s.examples[category]) for s in skills),
            },
        )
        text = (
            f"Examples for {category.label} from matched department profiles. Pick 2-4 and "
            "adapt them to this stakeholder; do not paste the list.\n\n" + "\n\n".join(sections)
        )
        return {"content": [{"type": "text", "text": text}]}

    return department_examples


def build_discovery_options(
    state: CanvasState,
    current_turn: Callable[[], int],
    matched_skills: MatchedSkills,
    *,
    model: str = DEFAULT_MODEL,
    max_budget_usd: float | None = None,
    web_search: SearchClient | None = None,
    web_search_guard: WebSearchGuard | None = None,
    sop_grounding: bool = True,
    sop_dir: Path = SOP_DIR,
    assess: AssessFn | None = None,
) -> ClaudeAgentOptions:
    """Assemble the SDK options for one discovery session.

    Args:
        state: The canvas log this session records into.
        current_turn: See :func:`build_record_tool`.
        matched_skills: See :func:`build_examples_tool`.
        model: Model ID for the conversation.
        max_budget_usd: Optional hard spend cap for the whole session; the SDK stops the run
            when it is exceeded.
        web_search: Optional scoped search client. When given, the ``web_search`` tool, its
            prompt section, and the capping ``PreToolUse`` hook are all wired in together.
        web_search_guard: The guard to use (owned by the caller so it can read the attempts);
            a default one is created if ``web_search`` is given without it.
        sop_grounding: Attach the SOP library as an external stdio MCP server (see
            :func:`sop_server_config`). On by default; it is local and deterministic.
        sop_dir: Directory the SOP server should load; overridable for tests.
        assess: Optional completeness check wired into the record tool (see
            :func:`build_record_tool`).
    """
    tools = [
        build_record_tool(state, current_turn, assess),
        build_examples_tool(matched_skills),
    ]
    allowed = [RECORD_TOOL_FULL_NAME, EXAMPLES_TOOL_FULL_NAME]
    hooks: dict[HookEvent, list[HookMatcher]] = {}
    web_search_max: int | None = None
    servers: dict[str, McpServerConfig] = {}

    if sop_grounding:
        servers[SOP_SERVER_NAME] = sop_server_config(sop_dir)
        allowed.extend([SOP_SEARCH_TOOL_FULL_NAME, SOP_READ_TOOL_FULL_NAME])

    if web_search is not None:
        guard = web_search_guard or WebSearchGuard()
        tools.append(build_web_search_tool(web_search))
        allowed.append(WEB_SEARCH_TOOL_FULL_NAME)
        # The hook runs before every web_search call and denies past the cap. Registered by
        # the tool's full name so it never fires for the canvas tools.
        hooks["PreToolUse"] = [guard.matcher(WEB_SEARCH_TOOL_FULL_NAME)]
        web_search_max = guard.max_calls

    # In-process server (canvas tools) and, optionally, the external stdio server (SOPs) sit
    # side by side in the same dict; the SDK handles the transport difference.
    servers[CANVAS_SERVER_NAME] = create_sdk_mcp_server(name=CANVAS_SERVER_NAME, tools=tools)

    return ClaudeAgentOptions(
        model=model,
        system_prompt=build_system_prompt(
            web_search_max=web_search_max, sop_grounding=sop_grounding
        ),
        # Isolation: never inherit CLAUDE.md or ~/.claude settings into a stakeholder session.
        setting_sources=[],
        env=AGENT_ENV,
        # No built-in tools at all (no Read/Bash/etc. schemas in context); only our MCP tools.
        tools=[],
        mcp_servers=servers,
        # Pre-approve our tools so they run without a permission prompt.
        allowed_tools=allowed,
        hooks=hooks or None,
        max_turns=MAX_AGENTIC_TURNS_PER_QUERY,
        max_budget_usd=max_budget_usd,
        # Emit StreamEvents mid-turn so the orchestrator can stream text to the UI.
        include_partial_messages=True,
    )
