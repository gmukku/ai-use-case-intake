"""What every agent subprocess must NOT inherit from the developer's machine.

Two channels leak developer context into a stakeholder-facing agent:

1. Filesystem settings and ``CLAUDE.md``: closed with ``setting_sources=[]`` (step 1).
2. **Auto-memory**: Claude Code loads the developer's per-project memory files for any
   session whose working directory maps to a known project, and a user-level index otherwise,
   *regardless* of ``setting_sources``. Found in step 6 when the Builder's report quoted this
   project's memory notes. Closed with the env switch below, passed to every subprocess.

Every ``ClaudeAgentOptions`` in this package must carry ``env=AGENT_ENV`` (and
``setting_sources=[]``). ``tests/test_isolation.py`` scans the source to enforce it.
"""

from __future__ import annotations

from typing import Final

AGENT_ENV: Final[dict[str, str]] = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
