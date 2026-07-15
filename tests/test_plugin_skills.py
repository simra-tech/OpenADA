from __future__ import annotations

import json
import re
from pathlib import Path

from openada import __version__


ROOT = Path(__file__).parents[1]
SKILLS_ROOT = ROOT / "skills"
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ANALOG_SKILLS = {
    "characterize-analog-block",
    "analyze-feedback-stability",
    "analyze-spectral-linearity",
    "assess-pvt-and-yield",
}
ANALOG_CONTRACT_IDS = {
    "openada.operation/circuit.simulate/v1alpha2",
    "openada.assertion/simulation.evidence.valid/v1alpha1",
    "openada.operation/result.measure/v1alpha1",
    "openada.assertion/measurement.valid/v1alpha1",
    "openada.operation/specification.evaluate/v1alpha1",
    "openada.assertion/specification.satisfied/v1alpha1",
}


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
    } | ANALOG_SKILLS

    for skill_directory in skill_directories:
        fields, body = _frontmatter(skill_directory / "SKILL.md")

        assert set(fields) == {"name", "description"}
        assert fields["name"] == skill_directory.name
        assert SKILL_NAME.fullmatch(fields["name"])
        assert 1 <= len(fields["description"]) <= 1024
        assert "TODO" not in body
        assert not (skill_directory / "README.md").exists()
        assert len((skill_directory / "SKILL.md").read_text().splitlines()) < 500


def test_all_plugin_skills_have_agent_discovery_metadata():
    for skill_directory in _skill_directories():
        metadata_path = skill_directory / "agents" / "openai.yaml"
        metadata = metadata_path.read_text(encoding="utf-8")

        assert "display_name:" in metadata
        assert "short_description:" in metadata
        assert "default_prompt:" in metadata
        assert f"$openada:{skill_directory.name}" in metadata


def test_codex_manifest_discovers_the_shared_skills_directory():
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["skills"] == "./skills/"
    assert "engineering skills" in manifest["description"]


def test_package_runtime_and_plugin_release_versions_match():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
    package_match = re.search(r'^version = "([^"]+)"$', project_section, re.MULTILINE)
    assert package_match is not None

    codex_manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    claude_manifest = json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    versions = {
        package_match.group(1),
        __version__,
        codex_manifest["version"],
        claude_manifest["version"],
    }
    assert versions == {__version__}
    assert __version__ == "0.3.0"


def test_engineering_skill_catalog_names_every_shipped_skill():
    catalog = (ROOT / "docs" / "ENGINEERING_SKILLS.md").read_text(encoding="utf-8")

    for skill_directory in _skill_directories():
        assert f"`{skill_directory.name}`" in catalog


def test_public_install_docs_use_plugin_namespaces_and_standard_user_skill_path():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "$openada:<skill-name>" in readme
    assert "/openada:<skill-name>" in readme
    assert "~/.agents/skills" in readme
    assert "~/.codex/skills" not in readme


def test_analog_skills_preserve_the_full_contract_ladder():
    for skill_name in ANALOG_SKILLS:
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

        for contract_id in ANALOG_CONTRACT_IDS:
            assert contract_id in text
        assert "signoff: not claimed" in text
        assert "capabil" in text.lower()


def test_analog_characterization_composes_the_focused_skills():
    text = (
        SKILLS_ROOT / "characterize-analog-block" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "$openada:analyze-feedback-stability" in text
    assert "$openada:analyze-spectral-linearity" in text
    assert "$openada:assess-pvt-and-yield" in text
    recipes = (
        SKILLS_ROOT
        / "characterize-analog-block"
        / "references"
        / "application-recipes.md"
    )
    assert recipes.is_file()
