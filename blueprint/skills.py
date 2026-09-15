"""Department skills: the static, procedural example banks for discovery.

Each department is a ``skills/<name>/SKILL.md`` at the repo root, in the standard Claude Code
skill format (YAML frontmatter + markdown body). We load them ourselves rather than through
the SDK's ``skills`` option so that (a) which departments apply can change mid-conversation
once the classifier has run, (b) the stakeholder session stays isolated from ``CLAUDE.md``,
and (c) every example the model retrieves passes through a tool we control and log.

They live in ``skills/`` rather than ``.claude/skills/`` because they are the application's
runtime data, not Claude Code configuration; keeping them out of ``.claude/`` also stops the
coding assistant from discovering them as its own skills while working on this repo.

Body headings are strict: one ``## <canvas category label>`` section per category, plus
``## Classification hints``. A heading outside that set is a load-time error, so a typo in a
skill file fails fast instead of silently dropping a category.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from blueprint.canvas import CanvasCategory

logger = logging.getLogger(__name__)

# Resolved relative to the package, not the current working directory, so the app works no
# matter where it is launched from. ``BLUEPRINT_SKILLS_DIR`` overrides it for deployments.
_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
SKILLS_DIR: Final = Path(os.environ.get("BLUEPRINT_SKILLS_DIR", _REPO_ROOT / "skills"))
HINTS_HEADING: Final = "Classification hints"

_FRONTMATTER_RE: Final = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_H2_RE: Final = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
_BULLET_RE: Final = re.compile(r"^\s*[-*] +(.+?)\s*$", re.MULTILINE)

# Heading text -> canvas category, e.g. "Key stakeholders" -> KEY_STAKEHOLDERS.
_LABEL_TO_CATEGORY: Final[dict[str, CanvasCategory]] = {c.label.lower(): c for c in CanvasCategory}


class SkillLoadError(ValueError):
    """A SKILL.md file is malformed. The message names the file and the problem."""


@dataclass(frozen=True, slots=True)
class DepartmentSkill:
    """One department's example bank, parsed and validated."""

    name: str
    description: str
    hints: tuple[str, ...]
    """Signals for the classifier: phrases that suggest this department applies."""
    examples: dict[CanvasCategory, tuple[str, ...]]
    """Bullet points per canvas category, in file order."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view."""
        return {
            "name": self.name,
            "description": self.description,
            "hints": list(self.hints),
            "examples": {c.value: list(v) for c, v in self.examples.items()},
        }


def parse_skill_frontmatter(text: str, *, source: str = "<string>") -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into its validated frontmatter mapping and markdown body.

    Shared by department skills and builder templates, which use the same file format.

    Raises:
        SkillLoadError: on a missing frontmatter block, invalid YAML, or a non-mapping.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillLoadError(f"{source}: missing YAML frontmatter block")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"{source}: invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillLoadError(f"{source}: frontmatter must be a mapping")
    return meta, match.group(2)


def parse_skill(text: str, *, source: str = "<string>") -> DepartmentSkill:
    """Parse one SKILL.md document.

    Raises:
        SkillLoadError: on missing frontmatter, missing ``name``/``description``, an unknown
            heading, a missing canvas category, or an empty section.
    """
    meta, body = parse_skill_frontmatter(text, source=source)

    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillLoadError(f"{source}: frontmatter 'name' is required")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError(f"{source}: frontmatter 'description' is required")

    sections = _split_sections(body)

    hints_section = sections.pop(HINTS_HEADING.lower(), None)
    if hints_section is None:
        raise SkillLoadError(f"{source}: missing '## {HINTS_HEADING}' section")
    hints = hints_section.bullets

    examples: dict[CanvasCategory, tuple[str, ...]] = {}
    for key, section in sections.items():
        category = _LABEL_TO_CATEGORY.get(key)
        if category is None:
            valid = ", ".join([HINTS_HEADING, *(c.label for c in CanvasCategory)])
            raise SkillLoadError(
                f"{source}: unknown heading '{section.heading}'; expected one of: {valid}"
            )
        examples[category] = section.bullets

    missing = [c.label for c in CanvasCategory if c not in examples]
    if missing:
        raise SkillLoadError(f"{source}: missing section(s): {', '.join(missing)}")

    for heading, bullets in [(HINTS_HEADING, hints), *((c.label, examples[c]) for c in examples)]:
        if not bullets:
            raise SkillLoadError(f"{source}: section '{heading}' has no bullet points")

    return DepartmentSkill(
        name=name.strip(), description=description.strip(), hints=hints, examples=examples
    )


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str  # as written in the file, for error messages
    bullets: tuple[str, ...]


def _split_sections(body: str) -> dict[str, _Section]:
    """Map each lower-cased ``## heading`` to the section beneath it."""
    headings = list(_H2_RE.finditer(body))
    sections: dict[str, _Section] = {}
    for i, h in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        chunk = body[h.end() : end]
        heading = h.group(1).strip()
        key = heading.lower()
        if key in sections:
            raise SkillLoadError(f"duplicate heading '{heading}'")
        sections[key] = _Section(heading, tuple(m.group(1) for m in _BULLET_RE.finditer(chunk)))
    return sections


def load_skills(root: Path = SKILLS_DIR) -> dict[str, DepartmentSkill]:
    """Load every ``<root>/<dir>/SKILL.md``; keyed by skill name, sorted for determinism.

    Raises:
        SkillLoadError: if any file is malformed or two files declare the same name.
        FileNotFoundError: if ``root`` does not exist.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"skills directory not found: {root}")

    skills: dict[str, DepartmentSkill] = {}
    for path in sorted(root.glob("*/SKILL.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"), source=str(path))
        if skill.name in skills:
            raise SkillLoadError(f"{path}: duplicate skill name '{skill.name}'")
        skills[skill.name] = skill
        logger.info(
            "skills.loaded",
            extra={
                "skill": skill.name,
                "hints": len(skill.hints),
                "examples": sum(len(v) for v in skill.examples.values()),
            },
        )
    return skills
