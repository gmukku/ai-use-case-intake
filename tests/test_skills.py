from pathlib import Path

import pytest

from blueprint.canvas import CanvasCategory
from blueprint.skills import SKILLS_DIR, SkillLoadError, load_skills, parse_skill

# A minimal, valid skill document used as the base for mutation tests.
VALID = """---
name: demo
description: A demo department.
---

# Demo

## Classification hints

- mentions of demos

## Key stakeholders

- demo owners

## Key activities

- running demos

## Value proposition

- fewer demos

## System integrations

- DemoCRM

## Input source

- demo docs

## Input types

- typed prompts

## Output format

- summarization
"""


class TestParseSkill:
    def test_parses_frontmatter_hints_and_all_categories(self) -> None:
        skill = parse_skill(VALID)
        assert skill.name == "demo"
        assert skill.description == "A demo department."
        assert skill.hints == ("mentions of demos",)
        assert set(skill.examples) == set(CanvasCategory)
        assert skill.examples[CanvasCategory.SYSTEM_INTEGRATIONS] == ("DemoCRM",)

    def test_headings_are_case_insensitive_and_bullets_keep_order(self) -> None:
        text = VALID.replace(
            "## Key stakeholders\n\n- demo owners", "## KEY STAKEHOLDERS\n\n- a\n- b"
        )
        skill = parse_skill(text)
        assert skill.examples[CanvasCategory.KEY_STAKEHOLDERS] == ("a", "b")

    def test_missing_frontmatter(self) -> None:
        with pytest.raises(SkillLoadError, match="missing YAML frontmatter"):
            parse_skill("# no frontmatter\n\n## Key stakeholders\n- x\n")

    def test_missing_name(self) -> None:
        with pytest.raises(SkillLoadError, match="'name' is required"):
            parse_skill(VALID.replace("name: demo\n", ""))

    def test_unknown_heading_is_an_error(self) -> None:
        text = VALID.replace("## Input types", "## Inputs")
        with pytest.raises(SkillLoadError, match=r"unknown heading 'Inputs'.*Input types"):
            parse_skill(text)

    def test_missing_category_is_an_error(self) -> None:
        text = VALID.replace("## Output format\n\n- summarization\n", "")
        with pytest.raises(SkillLoadError, match="missing section\\(s\\): Output format"):
            parse_skill(text)

    def test_missing_hints_is_an_error(self) -> None:
        text = VALID.replace("## Classification hints\n\n- mentions of demos\n", "")
        with pytest.raises(SkillLoadError, match="Classification hints"):
            parse_skill(text)

    def test_empty_section_is_an_error(self) -> None:
        text = VALID.replace("- DemoCRM\n", "")
        with pytest.raises(SkillLoadError, match="'System integrations' has no bullet points"):
            parse_skill(text)

    def test_duplicate_heading_is_an_error(self) -> None:
        text = VALID + "\n## Input types\n\n- again\n"
        with pytest.raises(SkillLoadError, match="duplicate heading"):
            parse_skill(text)

    def test_source_appears_in_errors(self) -> None:
        with pytest.raises(SkillLoadError, match=r"^hr/SKILL\.md:"):
            parse_skill("nope", source="hr/SKILL.md")

    def test_to_dict_uses_category_values(self) -> None:
        d = parse_skill(VALID).to_dict()
        assert d["examples"]["system_integrations"] == ["DemoCRM"]
        assert d["hints"] == ["mentions of demos"]


class TestLoadSkills:
    def test_missing_root(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_skills(tmp_path / "nope")

    def test_loads_sorted_and_rejects_duplicate_names(self, tmp_path: Path) -> None:
        for d in ("b_dir", "a_dir"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "SKILL.md").write_text(VALID, encoding="utf-8")
        with pytest.raises(SkillLoadError, match="duplicate skill name 'demo'"):
            load_skills(tmp_path)

    def test_ignores_dirs_without_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "SKILL.md").write_text(VALID, encoding="utf-8")
        (tmp_path / "notes").mkdir()
        assert list(load_skills(tmp_path)) == ["demo"]


class TestRealSkillFiles:
    """The four shipped department files must load and be well-formed."""

    def test_four_departments_load(self) -> None:
        skills = load_skills(SKILLS_DIR)
        assert sorted(skills) == ["customer_success", "finance", "hr", "sales"]

    @pytest.mark.parametrize("name", ["hr", "finance", "sales", "customer_success"])
    def test_each_department_is_substantive(self, name: str) -> None:
        skill = load_skills(SKILLS_DIR)[name]
        assert len(skill.hints) >= 3
        for category in CanvasCategory:
            assert len(skill.examples[category]) >= 3, f"{name}: thin {category.value}"

    def test_directory_name_matches_skill_name(self) -> None:
        for path in SKILLS_DIR.glob("*/SKILL.md"):
            skill = parse_skill(path.read_text(encoding="utf-8"), source=str(path))
            assert skill.name == path.parent.name
