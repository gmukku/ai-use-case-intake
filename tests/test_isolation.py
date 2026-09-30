"""Every agent subprocess must be isolated from the developer's machine.

Two leaks were found and closed during the build; this test keeps them closed by scanning
the source for every ``ClaudeAgentOptions(`` construction in the package and requiring both
``setting_sources=[]`` (no CLAUDE.md / settings) and ``env=AGENT_ENV`` (no auto-memory).
"""

import re
from pathlib import Path

from blueprint.isolation import AGENT_ENV

PACKAGE = Path(__file__).resolve().parents[1] / "blueprint"


def _option_blocks(source: str) -> list[str]:
    """Each ``ClaudeAgentOptions(`` call up to its closing paren, roughly."""
    blocks = []
    for m in re.finditer(r"ClaudeAgentOptions\(", source):
        depth, i = 0, m.end() - 1
        while i < len(source):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append(source[m.start() : i + 1])
    return blocks


def _agent_sources() -> list[Path]:
    """Every file in this repo that may construct an agent.

    `evals/` is included, and was not until the stakeholder simulator was written. The
    simulator spawns a CLI subprocess like any other agent, and one that inherited the
    developer's CLAUDE.md would be reading the answer sheet: the project brief describes the
    seven canvas categories the eval exists to check the agent discovers for itself.
    """
    return sorted(PACKAGE.glob("*.py")) + sorted(PACKAGE.parent.glob("evals/**/*.py"))


def test_every_agent_is_isolated() -> None:
    checked = 0
    for path in _agent_sources():
        source = path.read_text(encoding="utf-8")
        for block in _option_blocks(source):
            if "import" in block[:40]:  # a type annotation, not a construction
                continue
            checked += 1
            assert "setting_sources=[]" in block, f"{path.name}: options without setting_sources=[]"
            assert "env=AGENT_ENV" in block, f"{path.name}: options without env=AGENT_ENV"
    assert checked >= 6, (
        "expected the discovery, classifier, checker, spec, builder and simulator options"
    )


def test_env_disables_auto_memory() -> None:
    assert AGENT_ENV["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
