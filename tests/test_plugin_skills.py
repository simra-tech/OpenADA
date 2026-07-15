from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILLS_ROOT = ROOT / "skills"
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _skill_directories() -> list[Path]:
    return sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))


def _frontmatter(skill_file: Path) -> tuple[dict[str, str], str]:
    text = skill_file.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) == 3 and not parts[0], f"invalid frontmatter in {skill_file}"

    fields: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip(), f"invalid frontmatter line: {line}"
        fields[key] = value.strip()
    return fields, parts[2]


def test_all_plugin_skills_have_minimal_valid_metadata():
    skill_directories = _skill_directories()
    assert {path.name for path in skill_directories} >= {
        "openada",
        "review-circuit-simulation",
    }

    for skill_directory in skill_directories:
        fields, body = _frontmatter(skill_directory / "SKILL.md")

        assert set(fields) == {"name", "description"}
        assert fields["name"] == skill_directory.name
        assert SKILL_NAME.fullmatch(fields["name"])
        assert 1 <= len(fields["description"]) <= 1024
        assert "TODO" not in body
        assert not (skill_directory / "README.md").exists()


def test_all_plugin_skills_have_agent_discovery_metadata():
    for skill_directory in _skill_directories():
        metadata_path = skill_directory / "agents" / "openai.yaml"
        metadata = metadata_path.read_text(encoding="utf-8")

        assert "display_name:" in metadata
        assert "short_description:" in metadata
        assert "default_prompt:" in metadata
        assert f"${skill_directory.name}" in metadata


def test_codex_manifest_discovers_the_shared_skills_directory():
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["skills"] == "./skills/"
    assert "engineering skills" in manifest["description"]


def test_engineering_skill_catalog_names_every_shipped_skill():
    catalog = (ROOT / "docs" / "ENGINEERING_SKILLS.md").read_text(encoding="utf-8")

    for skill_directory in _skill_directories():
        assert f"`{skill_directory.name}`" in catalog
