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
DIGITAL_SKILLS = {
    "review-rtl-architecture",
    "assess-synthesis-and-inference",
    "assess-asic-timing",
}
COORDINATOR_SKILLS = {"bootstrap-asic-project"}
LAYOUT_SKILLS = {"close-layout-incrementally"}
DIGITAL_CONTRACT_IDS = {
    "review-rtl-architecture": {
        "openada.operation/rtl.lint/v1alpha1",
        "openada.assertion/rtl.lint.clean/v1alpha1",
        "openada.feature/rtl.lint.systemverilog/v1alpha1",
    },
    "assess-synthesis-and-inference": {
        "openada.operation/logic.synthesize/v1alpha1",
        "openada.assertion/synthesized-netlist.valid/v1alpha1",
        "openada.feature/synthesis.asic-liberty/v1alpha1",
    },
    "assess-asic-timing": {
        "openada.operation/timing.analyze/v1alpha1",
        "openada.assertion/timing.constraints-satisfied/v1alpha1",
        "openada.feature/timing.setup-hold/v1alpha1",
    },
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
    } | ANALOG_SKILLS | DIGITAL_SKILLS | LAYOUT_SKILLS | COORDINATOR_SKILLS

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
    assert manifest["name"] == "openada"
    assert len(manifest["interface"]["defaultPrompt"]) <= 3
    assert all(
        len(prompt) <= 128 for prompt in manifest["interface"]["defaultPrompt"]
    )


def test_repo_marketplace_exposes_the_root_plugin_with_explicit_policy():
    marketplace = json.loads(
        (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )

    assert marketplace["name"] == "openada"
    assert marketplace["interface"] == {"displayName": "OpenADA"}
    assert marketplace["plugins"] == [
        {
            "name": "openada",
            "source": {"source": "local", "path": "./"},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Developer Tools",
        }
    ]


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
    assert __version__ == "0.4.0"


def test_both_plugin_manifests_advertise_the_digital_connectors():
    for path in (
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "plugin.json",
    ):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert {"verilator", "yosys", "opensta"}.issubset(manifest["keywords"])


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
    assert "codex plugin marketplace add simra-tech/OpenADA --ref main" in readme
    assert "--ref v0.4.0" not in readme
    assert "OpenADA.git#v0.4.0" not in readme


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
    intent_routing = (
        SKILLS_ROOT
        / "characterize-analog-block"
        / "references"
        / "intent-routing.md"
    )
    assert intent_routing.is_file()
    assert "openada.operation/result.series.extract/v1alpha1" in text
    assert "openada.operation/result.spectral.measure/v1alpha1" in text


def test_digital_skills_use_exact_semantic_contracts_and_stop_boundaries():
    for skill_name, contract_ids in DIGITAL_CONTRACT_IDS.items():
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

        for contract_id in contract_ids:
            assert contract_id in text
        assert "$openada:openada" in text
        assert "signoff: not claimed" in text
        assert "capability" in text.lower()
        assert "unavailable" in text.lower()
        assert "engineering `unknown`" in text

    timing = (SKILLS_ROOT / "assess-asic-timing" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "ideal interconnect" in timing
    assert "routed" in timing
    assert "MCMM" in timing


def test_digital_skills_invoke_openada_surfaces_not_native_tool_commands():
    for skill_name in DIGITAL_SKILLS:
        text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

        assert "openada profile show" in text
        assert "openada doctor" in text
        assert "yosys " not in text.lower()
        assert "verilator " not in text.lower()
        assert "opensta " not in text.lower()


def test_digital_skills_stay_within_the_current_result_and_workflow_boundary():
    texts = {
        skill_name: (SKILLS_ROOT / skill_name / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for skill_name in DIGITAL_SKILLS
    }

    for text in texts.values():
        assert "request/result IDs" not in text
        assert "result ID" not in text
        assert "normalized evidence" in text
        assert "source" in text.lower() and "inspection" in text.lower()

    rtl = texts["review-rtl-architecture"]
    synthesis = texts["assess-synthesis-and-inference"]
    timing = texts["assess-asic-timing"]
    timing_compact = " ".join(timing.split())

    assert "no module-parameter override" in rtl
    assert "conservative capture" in rtl
    assert "no module-parameter override" in synthesis
    assert "conservative rather than exact conditional-preprocessor" in synthesis
    assert "not a violating-endpoint count" in timing
    assert "neither violating-path" in timing
    assert "one `critical_path` summary per analysis" in timing
    assert "does not expose multiple ranked paths" in timing_compact


def test_bootstrap_coordinator_preserves_native_gap_and_signoff_boundaries():
    directory = SKILLS_ROOT / "bootstrap-asic-project"
    text = (directory / "SKILL.md").read_text(encoding="utf-8")

    assert "$openada:openada" in text
    assert "not evaluated — capability unavailable" in text
    assert "explicitly requests an exploratory end-to-end run" in text
    assert "Never place native output inside an `openada.result` envelope" in text
    assert "openada.operation/rtl.test/v1alpha1" in text
    assert "no semantic operation for behavioral HDL simulation" not in text
    assert "scoped doctor catalog does not yet map the RTL self-test assertion" in text
    assert "signoff: not claimed" in text
    assert (directory / "scripts" / "bootstrap_manifest.py").is_file()
    assert (directory / "references" / "project-manifest.md").is_file()
    assert (directory / "references" / "ihp-sg13g2-full-chip.md").is_file()


def test_incremental_layout_skill_requires_visual_and_verification_gates():
    directory = SKILLS_ROOT / "close-layout-incrementally"
    text = (directory / "SKILL.md").read_text(encoding="utf-8")

    assert "$openada:openada" in text
    assert "Do not place or connect the whole block at once" in text
    assert "Visual reasoning proposes and localizes a diagnosis" in text
    assert "A DRC pass does not prove connectivity" in text
    assert "signoff: not claimed" in text
    assert (directory / "scripts" / "render_layout.rb").is_file()


def test_spectral_skill_uses_the_closed_method_and_standards_reference():
    directory = SKILLS_ROOT / "analyze-spectral-linearity"
    text = (directory / "SKILL.md").read_text(encoding="utf-8")
    reference = directory / "references" / "standards-and-methods.md"

    assert reference.is_file()
    assert "openada.operation/result.series.extract/v1alpha1" in text
    assert "openada.operation/result.spectral.measure/v1alpha1" in text
    reference_text = reference.read_text(encoding="utf-8")
    assert "IEEE 1241-2023" in reference_text
    assert "candidate" in reference_text
    assert "IEEE compliant" in reference_text
