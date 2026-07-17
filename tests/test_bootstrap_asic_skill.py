from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "bootstrap-asic-project"
    / "scripts"
    / "bootstrap_manifest.py"
)
SYNTHESIS_ROLES = {
    "pdk.revision-attestation",
    "constraints.sdc",
    "standard-cell.liberty",
    "license.manifest",
}
ROUTED_ROLES = SYNTHESIS_ROLES | {
    "standard-cell.lef",
    "standard-cell.gds",
    "standard-cell.cdl",
    "drc.deck",
    "lvs.deck",
    "rcx.rules",
}
FULL_CHIP_ROLES = ROUTED_ROLES | {
    "io.liberty",
    "io.lef",
    "io.gds",
    "io.cdl",
    "bondpad.lef",
    "bondpad.gds",
    "antenna.deck",
    "density.deck",
    "fill.deck",
    "seal-ring.config",
    "padframe.config",
    "pdn.config",
    "pinout",
    "package.plan",
}
SUBMISSION_ROLES = FULL_CHIP_ROLES | {
    "submission.checklist",
    "waiver.ledger",
}
SYNTHESIS_TOOLS = {"yosys", "verilator"}
PHYSICAL_TOOLS = SYNTHESIS_TOOLS | {
    "librelane",
    "openroad",
    "opensta",
    "klayout",
    "netgen",
    "magic",
}


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert not result.stderr
    return json.loads(result.stdout)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _initialize(tmp_path: Path, deliverable: str) -> tuple[Path, dict[str, Path]]:
    project = tmp_path / "project"
    pdk = tmp_path / "pdk"
    project.mkdir()
    pdk.mkdir()
    files = {
        "spec": _write(project / "PROJECT_SPEC.md", "clock: 50 MHz\npackage: wirebond\n"),
        "sources": _write(project / "SOURCE_MANIFEST.sha256", "abc  src/cpu.sv\n"),
        "runtime": _write(tmp_path / "runtime-launch.json", '{"network":"none"}\n'),
        "config": _write(project / "config.yaml", "DESIGN_NAME: cpu_chip\n"),
        "template_lock": _write(project / "flake.lock", '{"version":7}\n'),
    }
    manifest = project / ".openada" / "bootstrap-manifest.json"
    arguments = [
        "init",
        "--output",
        str(manifest),
        "--project-root",
        str(project),
        "--name",
        "cpu-chip",
        "--deliverable",
        deliverable,
        "--top",
        "cpu_chip",
        "--project-spec",
        str(files["spec"]),
        "--source-manifest",
        str(files["sources"]),
        "--pdk-id",
        "ihp-sg13g2",
        "--pdk-root",
        str(pdk),
        "--pdk-revision-scheme",
        "git-sha1",
        "--pdk-revision",
        "1" * 40,
        "--runtime-kind",
        "oci",
        "--runtime-profile",
        "iic-osic-tools",
        "--runtime-identity",
        str(files["runtime"]),
        "--image-reference",
        f"example.invalid/eda@sha256:{'2' * 64}",
        "--image-platform",
        "linux/amd64",
        "--flow-name",
        "librelane",
        "--flow-tool",
        "librelane",
        "--flow-revision-scheme",
        "content-sha256",
        "--flow-revision",
        "3" * 64,
        "--flow-config",
        str(files["config"]),
        "--evidence-root",
        str(tmp_path / "large-external-evidence"),
    ]
    if deliverable in {"full-chip", "submission-candidate"}:
        arguments += [
            "--template-origin",
            "https://example.invalid/full-chip-template",
            "--template-revision-scheme",
            "git-sha256",
            "--template-revision",
            "4" * 64,
            "--template-lock",
            str(files["template_lock"]),
        ]
    result = _run(*arguments)
    assert result.returncode == 0, result.stdout
    assert _payload(result)["claim"] == "identity-ledger-created-draft"
    return manifest, files


def _bind_roles(manifest: Path, directory: Path, roles: set[str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for index, role in enumerate(sorted(roles)):
        stable_id = f"input-{index:02d}"
        path = _write(directory / f"{stable_id}.txt", f"declared {role}\n")
        result = _run(
            "bind-file",
            str(manifest),
            "--id",
            stable_id,
            "--role",
            role,
            "--path",
            str(path),
        )
        assert result.returncode == 0, result.stdout
        paths[stable_id] = path
    return paths


def _set_tools(manifest: Path, directory: Path, tools: set[str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for tool in sorted(tools):
        path = _write(directory / tool, "#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        result = _run(
            "set-tool",
            str(manifest),
            "--name",
            tool,
            "--path",
            str(path),
            "--version",
            f"fixture-declared {tool} 1.0",
        )
        assert result.returncode == 0, result.stdout
        assert _payload(result)["claim"] == "caller-declared-tool-version"
        paths[tool] = path
    return paths


def _freeze_complete(
    manifest: Path, tmp_path: Path, deliverable: str
) -> tuple[dict[str, Path], dict[str, Path]]:
    roles = {
        "synthesized-core": SYNTHESIS_ROLES,
        "routed-core": ROUTED_ROLES,
        "full-chip": FULL_CHIP_ROLES,
        "submission-candidate": SUBMISSION_ROLES,
    }[deliverable]
    tools = SYNTHESIS_TOOLS if deliverable == "synthesized-core" else PHYSICAL_TOOLS
    collateral = _bind_roles(manifest, tmp_path / "collateral", roles)
    executables = _set_tools(manifest, tmp_path / "tools", tools)
    result = _run("freeze", str(manifest))
    assert result.returncode == 0, result.stdout
    payload = _payload(result)
    assert payload["outcome"] == "valid"
    assert payload["claim"] == "structurally-declared-and-hash-consistent"
    return collateral, executables


@pytest.mark.parametrize("deliverable", ["synthesized-core", "routed-core"])
def test_stage_conditional_core_freeze(deliverable: str, tmp_path: Path) -> None:
    manifest, _ = _initialize(tmp_path, deliverable)
    _freeze_complete(manifest, tmp_path, deliverable)

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["project"]["deliverable"] == deliverable
    assert document["project"]["template"] == {
        "origin": None,
        "revision": None,
        "lock": None,
    }
    assert Path(document["evidence_root"]).parent == tmp_path
    assert not Path(document["evidence_root"]).is_relative_to(document["project"]["root"])


@pytest.mark.parametrize("deliverable", ["full-chip", "submission-candidate"])
def test_full_chip_and_submission_require_different_collateral(
    deliverable: str, tmp_path: Path
) -> None:
    manifest, _ = _initialize(tmp_path, deliverable)
    _freeze_complete(manifest, tmp_path, deliverable)

    document = json.loads(manifest.read_text(encoding="utf-8"))
    roles = {item["role"] for item in document["collateral"]}
    if deliverable == "full-chip":
        assert "submission.checklist" not in roles
        assert "waiver.ledger" not in roles
    else:
        assert {"submission.checklist", "waiver.ledger"}.issubset(roles)


def test_frozen_lifecycle_detects_mutation_and_requires_explicit_thaw(
    tmp_path: Path,
) -> None:
    manifest, files = _initialize(tmp_path, "synthesized-core")
    collateral, _ = _freeze_complete(manifest, tmp_path, "synthesized-core")

    frozen_edit = _run(
        "set-flow",
        str(manifest),
        "--name",
        "librelane",
        "--tool",
        "librelane",
        "--revision-scheme",
        "git-sha1",
        "--revision",
        "5" * 40,
        "--config",
        str(files["config"]),
    )
    assert frozen_edit.returncode == 2
    assert "thaw" in str(_payload(frozen_edit)["diagnostic"])

    thaw = _run("thaw", str(manifest), "--reason", "intentional config repair")
    assert thaw.returncode == 0
    replacement = _write(tmp_path / "replacement.sdc", "create_clock -period 40 clk\n")
    replaced = _run(
        "replace-file",
        str(manifest),
        "--id",
        "input-00",
        "--role",
        "constraints.sdc",
        "--path",
        str(replacement),
    )
    assert replaced.returncode == 0
    changed_config = _write(tmp_path / "changed-config.yaml", "DESIGN_NAME: repaired_cpu\n")
    set_flow = _run(
        "set-flow",
        str(manifest),
        "--name",
        "librelane",
        "--tool",
        "librelane",
        "--revision-scheme",
        "git-sha1",
        "--revision",
        "5" * 40,
        "--config",
        str(changed_config),
    )
    assert set_flow.returncode == 0
    refrozen = _run("freeze", str(manifest))
    assert refrozen.returncode == 0, refrozen.stdout

    stale_path = next(path for key, path in collateral.items() if key != "input-00")
    stale_path.write_text("mutated after freeze\n", encoding="utf-8")
    stale = _run("validate", str(manifest))
    assert stale.returncode == 2
    assert "does not match current regular-file bytes" in str(_payload(stale)["diagnostic"])
    recover = _run("thaw", str(manifest), "--reason", "repair unexpected input drift")
    assert recover.returncode == 0, recover.stdout


def test_gap_resolution_is_retained_and_tool_mode_is_rechecked(tmp_path: Path) -> None:
    manifest, _ = _initialize(tmp_path, "synthesized-core")
    added = _run(
        "add-gap",
        str(manifest),
        "--id",
        "behavioral-simulation-evidence",
        "--stage",
        "function",
        "--kind",
        "evidence",
        "--detail",
        "Self-checking behavioral HDL evidence is not yet retained",
    )
    assert added.returncode == 0
    resolved = _run(
        "resolve-gap",
        str(manifest),
        "--id",
        "behavioral-simulation-evidence",
        "--resolution",
        "Native simulation retained separately under explicit authorization",
    )
    assert resolved.returncode == 0
    _, tools = _freeze_complete(manifest, tmp_path, "synthesized-core")

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["gaps"][0]["status"] == "resolved"
    tools["yosys"].chmod(0o644)
    invalid = _run("validate", str(manifest), "--require-frozen")
    assert invalid.returncode == 2
    assert "no longer executable" in str(_payload(invalid)["diagnostic"])


def test_incomplete_full_chip_freeze_fails_without_claiming_engineering_failure(
    tmp_path: Path,
) -> None:
    manifest, _ = _initialize(tmp_path, "full-chip")
    _bind_roles(manifest, tmp_path / "collateral", SYNTHESIS_ROLES)
    _set_tools(manifest, tmp_path / "tools", SYNTHESIS_TOOLS)

    result = _run("freeze", str(manifest))
    assert result.returncode == 2
    payload = _payload(result)
    assert payload["outcome"] == "invalid"
    assert "engineering" not in result.stdout.lower()
    assert "librelane" in str(payload["diagnostic"])

    _set_tools(
        manifest,
        tmp_path / "tools",
        PHYSICAL_TOOLS - SYNTHESIS_TOOLS,
    )
    missing_collateral = _run("freeze", str(manifest))
    assert missing_collateral.returncode == 2
    assert "bondpad.gds" in str(_payload(missing_collateral)["diagnostic"])


def test_draft_validation_reports_freeze_readiness_and_missing_requirements(
    tmp_path: Path,
) -> None:
    manifest, _ = _initialize(tmp_path, "full-chip")

    draft = _run("validate", str(manifest), "--check-paths")
    assert draft.returncode == 0, draft.stdout
    payload = _payload(draft)
    assert payload["outcome"] == "valid"
    assert payload["freeze_ready"] is False
    assert payload["missing_freeze_requirements"]["template"] is False
    assert payload["missing_freeze_requirements"]["tools"] == sorted(PHYSICAL_TOOLS)
    assert payload["missing_freeze_requirements"]["collateral_roles"] == sorted(
        FULL_CHIP_ROLES
    )

    _freeze_complete(manifest, tmp_path, "full-chip")
    frozen = _run("validate", str(manifest), "--require-frozen")
    assert frozen.returncode == 0, frozen.stdout
    frozen_payload = _payload(frozen)
    assert frozen_payload["freeze_ready"] is True
    assert frozen_payload["missing_freeze_requirements"] == {
        "template": False,
        "tools": [],
        "collateral_roles": [],
    }


def test_pdk_runtime_and_evidence_replacement_clear_stale_identities(
    tmp_path: Path,
) -> None:
    manifest, _ = _initialize(tmp_path, "synthesized-core")
    _bind_roles(manifest, tmp_path / "collateral", SYNTHESIS_ROLES)
    _set_tools(manifest, tmp_path / "tools", SYNTHESIS_TOOLS)

    replacement_pdk = tmp_path / "replacement-pdk"
    replacement_pdk.mkdir()
    changed_pdk = _run(
        "set-pdk",
        str(manifest),
        "--id",
        "ihp-sg13g2",
        "--root",
        str(replacement_pdk),
        "--revision-scheme",
        "content-sha256",
        "--revision",
        "6" * 64,
        "--flow-name",
        "librelane",
        "--flow-tool",
        "librelane",
        "--flow-revision-scheme",
        "content-sha256",
        "--flow-revision",
        "8" * 64,
        "--flow-config",
        str(tmp_path / "project" / "config.yaml"),
    )
    assert changed_pdk.returncode == 0
    assert "cleared" in str(_payload(changed_pdk)["claim"])
    after_pdk = json.loads(manifest.read_text(encoding="utf-8"))
    assert after_pdk["collateral"] == []
    assert after_pdk["runtime"]["tools"] == {}
    assert after_pdk["flow"]["revision"] == {
        "scheme": "content-sha256",
        "value": "8" * 64,
    }

    new_runtime = _write(tmp_path / "new-runtime.json", '{"network":"none"}\n')
    changed_runtime = _run(
        "set-runtime",
        str(manifest),
        "--runtime-kind",
        "oci",
        "--runtime-profile",
        "iic-osic-tools",
        "--runtime-identity",
        str(new_runtime),
        "--image-reference",
        f"example.invalid/new@sha256:{'7' * 64}",
        "--image-platform",
        "linux/amd64",
    )
    assert changed_runtime.returncode == 0
    after_runtime = json.loads(manifest.read_text(encoding="utf-8"))
    assert after_runtime["runtime"]["tools"] == {}

    external = tmp_path.parent / f"{tmp_path.name}-external-evidence"
    moved = _run("set-evidence-root", str(manifest), "--path", str(external))
    assert moved.returncode == 0
    after_move = json.loads(manifest.read_text(encoding="utf-8"))
    assert after_move["evidence_root"] == str(external.resolve())


def test_manifest_reader_rejects_symlink_and_oversized_input(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(1024 * 1024 + 1)
    too_large = _run("validate", str(oversized))
    assert too_large.returncode == 2
    assert "1 MiB" in str(_payload(too_large)["diagnostic"])

    target = _write(tmp_path / "target.json", "{}\n")
    symlink = tmp_path / "manifest-link.json"
    os.symlink(target, symlink)
    linked = _run("validate", str(symlink))
    assert linked.returncode == 2
    assert "symbolic link" in str(_payload(linked)["diagnostic"])


def test_pdk_stable_id_help_and_lowercase_suggestion(tmp_path: Path) -> None:
    for command, option in (("init", "--pdk-id"), ("set-pdk", "--id")):
        help_result = _run(command, "--help")
        assert help_result.returncode == 0
        assert option in help_result.stdout
        normalized_help = " ".join(help_result.stdout.split())
        assert "sky130a for native name sky130A" in normalized_help

    spec = importlib.util.spec_from_file_location("bootstrap_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(
        module.ManifestError,
        match=r"target\.pdk_id is not a lowercase stable identifier; use 'sky130a'",
    ):
        module._safe_id("sky130A", "target.pdk_id")

    manifest, files = _initialize(tmp_path, "synthesized-core")
    before = manifest.read_bytes()
    rejected = _run(
        "set-pdk",
        str(manifest),
        "--id",
        "sky130A",
        "--root",
        str(tmp_path / "pdk"),
        "--revision-scheme",
        "git-sha1",
        "--revision",
        "4" * 40,
        "--flow-name",
        "librelane",
        "--flow-tool",
        "librelane",
        "--flow-revision-scheme",
        "content-sha256",
        "--flow-revision",
        "5" * 64,
        "--flow-config",
        str(files["config"]),
    )
    assert rejected.returncode == 2
    assert _payload(rejected)["diagnostic"] == (
        "target.pdk_id is not a lowercase stable identifier; use 'sky130a'"
    )
    assert manifest.read_bytes() == before


def test_encoder_refuses_to_create_an_unreadable_oversized_ledger() -> None:
    spec = importlib.util.spec_from_file_location("bootstrap_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    oversized = {"payload": "x" * (1024 * 1024)}
    with pytest.raises(module.ManifestError, match="encoded manifest exceeds"):
        module._encoded(oversized)
