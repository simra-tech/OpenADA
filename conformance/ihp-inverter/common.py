"""Shared helpers for the pinned IHP inverter conformance workflow."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


MANIFEST_SCHEMA = "openada.conformance/v0alpha1"
RESULT_SCHEMA = "openada.result/v0alpha1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DRC_OPERATION_NAMES = {"drc", "drc_fail"}
EXPECTED_OPERATION_NAMES = {*DRC_OPERATION_NAMES, "lvs"}
PINNED_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
PINNED_OPERATION_RECORDS = {
    "drc": {
        "tool_identity": {
            "path": "/foss/tools/klayout/klayout",
            "version": "KLayout 0.30.9",
        },
        "container_timeout_seconds": 240,
        "arguments": {
            "gds": "/design/modules/module_0_foundations/PEX_Demo/layout/inverter.gds",
            "rules": "/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc",
            "report": "/evidence/inverter.drc.lyrdb",
            "top_cell": "inverter",
            "provenance_inputs": ["/foss/pdks/ihp-sg13g2/COMMIT"],
            "timeout_seconds": 180,
        },
        "inputs": [
            {
                "path": "/design/modules/module_0_foundations/PEX_Demo/layout/inverter.gds",
                "kind": "gds",
                "role": "input",
                "sha256": "9ed664d54d8c1b82a86a0d02e59a21c7593c94d1a3ca1bcc6aec614726007cd1",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc",
                "kind": "klayout-drc-deck",
                "role": "rules",
                "sha256": "22a570bfb43d564b7ce3de9d7df0795197be79f86c8a484b4959932c8dd75961",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/COMMIT",
                "kind": "klayout-rules-input",
                "role": "rules-dependency",
                "sha256": "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5",
            },
        ],
        "artifact": {
            "path": "/evidence/inverter.drc.lyrdb",
            "filename": "inverter.drc.lyrdb",
            "kind": "klayout-lyrdb",
            "role": "evidence",
        },
        "transcript_artifact": {
            "path": "/evidence/inverter.drc.lyrdb.openada.log",
            "filename": "inverter.drc.lyrdb.openada.log",
            "kind": "klayout-transcript",
            "role": "evidence",
        },
        "native_report": {
            "generator": "drc: script='/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc'",
            "top_cell": "inverter",
            "minimum_categories": 1,
        },
        "result_filename": "drc.json",
    },
    "drc_fail": {
        "tool_identity": {
            "path": "/foss/tools/klayout/klayout",
            "version": "KLayout 0.30.9",
        },
        "container_timeout_seconds": 240,
        "arguments": {
            "gds": "/design/modules/module_0_foundations/lvs_tester/GDS/gallery.gds",
            "rules": "/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc",
            "report": "/evidence/lvs-tester.drc.lyrdb",
            "top_cell": "lvs_tester",
            "provenance_inputs": ["/foss/pdks/ihp-sg13g2/COMMIT"],
            "timeout_seconds": 180,
        },
        "inputs": [
            {
                "path": "/design/modules/module_0_foundations/lvs_tester/GDS/gallery.gds",
                "kind": "gds",
                "role": "input",
                "sha256": "c536ff737248e62cc209a6aec764a7f21750d0978e2e8351a4f0c2a6f144bc96",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc",
                "kind": "klayout-drc-deck",
                "role": "rules",
                "sha256": "22a570bfb43d564b7ce3de9d7df0795197be79f86c8a484b4959932c8dd75961",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/COMMIT",
                "kind": "klayout-rules-input",
                "role": "rules-dependency",
                "sha256": "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5",
            },
        ],
        "artifact": {
            "path": "/evidence/lvs-tester.drc.lyrdb",
            "filename": "lvs-tester.drc.lyrdb",
            "kind": "klayout-lyrdb",
            "role": "evidence",
        },
        "transcript_artifact": {
            "path": "/evidence/lvs-tester.drc.lyrdb.openada.log",
            "filename": "lvs-tester.drc.lyrdb.openada.log",
            "kind": "klayout-transcript",
            "role": "evidence",
        },
        "native_report": {
            "generator": "drc: script='/foss/pdks/ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc'",
            "top_cell": "lvs_tester",
            "minimum_categories": 1,
            "expected_item_count": 8,
            "expected_total_violations": 8,
            "expected_waived_violations": 0,
            "expected_category_counts": [
                {
                    "category": "M1.b",
                    "category_path": ["M1.b"],
                    "violations": 6,
                },
                {
                    "category": "Cnt.d",
                    "category_path": ["Cnt.d"],
                    "violations": 1,
                },
                {
                    "category": "Cnt.e",
                    "category_path": ["Cnt.e"],
                    "violations": 1,
                },
            ],
            "expected_violations": [
                {
                    "category_path": ["Cnt.d"],
                    "cell": "lvs_tester",
                    "multiplicity": 1,
                    "waived": False,
                },
                {
                    "category_path": ["Cnt.e"],
                    "cell": "lvs_tester",
                    "multiplicity": 1,
                    "waived": False,
                },
                *[
                    {
                        "category_path": ["M1.b"],
                        "cell": "lvs_tester",
                        "multiplicity": 1,
                        "waived": False,
                    }
                    for _ in range(4)
                ],
                *[
                    {
                        "category_path": ["M1.b"],
                        "cell": "nmos$1",
                        "multiplicity": 1,
                        "waived": False,
                    }
                    for _ in range(2)
                ],
            ],
            "expected_normalization": {
                "geometry_values": 8,
                "retained_geometries": 8,
                "retained_coordinate_pairs": 32,
                "global_geometry_limit_reached": False,
            },
        },
        "result_filename": "drc-fail.json",
    },
    "lvs": {
        "tool_identity": {
            "path": "/foss/tools/netgen/bin/netgen",
            "version": "Netgen 1.5.321 compiled on Mon Jun 22 11:31:27 AM CEST 2026",
        },
        "container_timeout_seconds": 240,
        "arguments": {
            "layout_netlist": "/design/modules/module_0_foundations/PEX_Demo/layout/inverter_extracted.cir",
            "schematic_netlist": "/design/modules/module_0_foundations/PEX_Demo/simulations/inverter.spice",
            "cell": "inverter",
            "setup": "/foss/pdks/ihp-sg13g2/libs.tech/netgen/ihp-sg13g2_setup.tcl",
            "report": "/evidence/inverter.lvs.comp",
            "provenance_inputs": ["/foss/pdks/ihp-sg13g2/COMMIT"],
            "timeout_seconds": 180,
        },
        "inputs": [
            {
                "path": "/design/modules/module_0_foundations/PEX_Demo/layout/inverter_extracted.cir",
                "kind": "layout-netlist",
                "role": "input",
                "sha256": "ea05ff8402661fdb09978f2210b3d985ad6b679c1b69b0e564d047b135bc14a9",
            },
            {
                "path": "/design/modules/module_0_foundations/PEX_Demo/simulations/inverter.spice",
                "kind": "schematic-netlist",
                "role": "reference",
                "sha256": "d6e54b972bbd3140498081f79b1bfa06f6da29f6528ebda1065d47499dc5f8f9",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/libs.tech/netgen/ihp-sg13g2_setup.tcl",
                "kind": "netgen-setup",
                "role": "rules",
                "sha256": "9981101e0bbe9d8f6af656f03475d0d00fb7507559645e85e4d9d5f704b3878e",
            },
            {
                "path": "/foss/pdks/ihp-sg13g2/COMMIT",
                "kind": "netgen-rules-input",
                "role": "rules-dependency",
                "sha256": "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5",
            },
        ],
        "artifact": {
            "path": "/evidence/inverter.lvs.comp",
            "filename": "inverter.lvs.comp",
            "kind": "netgen-comparison",
            "role": "evidence",
        },
        "json_artifact": {
            "path": "/evidence/inverter.lvs.json",
            "filename": "inverter.lvs.json",
            "kind": "netgen-comparison-json",
            "role": "evidence",
        },
        "transcript_artifact": {
            "path": "/evidence/inverter.lvs.comp.openada.log",
            "filename": "inverter.lvs.comp.openada.log",
            "kind": "netgen-transcript",
            "role": "evidence",
        },
        "result_filename": "lvs.json",
    },
}


class ConformanceError(RuntimeError):
    """A deterministic conformance precondition or assertion failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read conformance manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError("conformance manifest root must be an object")
    validate_manifest(document)
    return document


def _require_sha256(value: Any, location: str, errors: list[str]) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        errors.append(f"{location} must be a lowercase SHA-256 hex digest")


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate both manifest shape and the required golden-path semantics."""

    errors: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"schema must be {MANIFEST_SCHEMA!r}")
    if manifest.get("id") != "ihp-inverter-drc-lvs":
        errors.append("id must be 'ihp-inverter-drc-lvs'")

    design = manifest.get("design")
    if not isinstance(design, dict):
        errors.append("design must be an object")
        design = {}
    if design.get("repository") != "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git":
        errors.append("design.repository is not the public IHP AnalogAcademy repository")
    revision = design.get("revision")
    if revision != "133ecf657572e021b5921b5a1b7693abfb209623":
        errors.append("design.revision is not the reviewed commit")
    license_record = design.get("license")
    if not isinstance(license_record, dict):
        errors.append("design.license must be an object")
        license_record = {}
    if license_record.get("spdx") != "Apache-2.0":
        errors.append("design.license.spdx must be Apache-2.0")
    if license_record.get("path") != "LICENSE":
        errors.append("design.license.path must be LICENSE")
    if license_record.get("sha256") != PINNED_LICENSE_SHA256:
        errors.append("design.license.sha256 is not the reviewed Apache-2.0 license hash")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("runtime must be an object")
        runtime = {}
    image = runtime.get("image")
    if not isinstance(image, dict):
        errors.append("runtime.image must be an object")
        image = {}
    digest = image.get("manifest_digest")
    expected_digest = "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
    if digest != expected_digest:
        errors.append("runtime.image.manifest_digest is not the reviewed linux/amd64 digest")
    if image.get("name") != "hpretl/iic-osic-tools":
        errors.append("runtime.image.name must be hpretl/iic-osic-tools")
    if image.get("tag") != "2026.06":
        errors.append("runtime.image.tag must be 2026.06")
    if image.get("platform") != "linux/amd64":
        errors.append("runtime.image.platform must be linux/amd64")
    expected_reference = f"hpretl/iic-osic-tools@{expected_digest}"
    if image.get("reference") != expected_reference:
        errors.append("runtime.image.reference must use the manifest digest, not a mutable tag")
    pdk = runtime.get("pdk")
    if not isinstance(pdk, dict) or pdk.get("name") != "ihp-sg13g2":
        errors.append("runtime.pdk.name must be ihp-sg13g2")
    elif pdk.get("source") != "bundled in the pinned runtime image":
        errors.append("runtime.pdk.source must identify the pinned runtime image")
    else:
        if pdk.get("revision") != "144f811cdffda49b71d28f64e8a92b697b61cf06":
            errors.append("runtime.pdk.revision is not the reviewed IHP PDK revision")
        if pdk.get("revision_file") != "/foss/pdks/ihp-sg13g2/COMMIT":
            errors.append("runtime.pdk.revision_file differs from the reviewed runtime path")
        if (
            pdk.get("revision_file_sha256")
            != "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5"
        ):
            errors.append("runtime.pdk.revision_file_sha256 differs from the reviewed file")

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
        policy = {}
    if policy.get("eda_network") != "none":
        errors.append("policy.eda_network must be none")
    if policy.get("setup_network") != "allowed for the pinned Git fetch and image pull only":
        errors.append("policy.setup_network differs from the reviewed setup policy")
    if policy.get("openada_mount") != "read-only":
        errors.append("policy.openada_mount must be read-only")
    if policy.get("design_mount") != "read-only":
        errors.append("policy.design_mount must be read-only")
    if policy.get("evidence_directory") != "new and writable":
        errors.append("policy.evidence_directory must be new and writable")

    operations = manifest.get("operations")
    if not isinstance(operations, dict):
        errors.append("operations must be an object")
        operations = {}
    if set(operations) != EXPECTED_OPERATION_NAMES:
        errors.append("operations must contain exactly drc, drc_fail, and lvs")

    required_expectations = {
        "drc": {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "pass",
            "drc_clean": True,
            "total_violations": 0,
        },
        "drc_fail": {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "fail",
            "drc_clean": False,
            "total_violations": 8,
        },
        "lvs": {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "pass",
            "lvs_match": True,
            "mismatch_count": 0,
        },
    }
    required_tools = {"drc": "klayout", "drc_fail": "klayout", "lvs": "netgen"}
    seen_result_names: set[str] = set()
    seen_artifact_names: set[str] = set()
    for name in sorted(EXPECTED_OPERATION_NAMES):
        operation = operations.get(name)
        location = f"operations.{name}"
        if not isinstance(operation, dict):
            errors.append(f"{location} must be an object")
            continue
        if operation.get("tool") != required_tools[name]:
            errors.append(f"{location}.tool must be {required_tools[name]}")
        if operation.get("expect") != required_expectations[name]:
            errors.append(f"{location}.expect does not encode the required golden result")
        pinned = PINNED_OPERATION_RECORDS[name]
        if operation.get("tool_identity") != pinned["tool_identity"]:
            errors.append(f"{location}.tool_identity differs from the reviewed runtime tool")
        if operation.get("container_timeout_seconds") != pinned["container_timeout_seconds"]:
            errors.append(f"{location}.container_timeout_seconds must be 240")
        if operation.get("arguments") != pinned["arguments"]:
            errors.append(f"{location}.arguments differ from the reviewed invocation")
        if operation.get("inputs") != pinned["inputs"]:
            errors.append(f"{location}.inputs differ from the reviewed input paths and hashes")
        if operation.get("artifact") != pinned["artifact"]:
            errors.append(f"{location}.artifact differs from the reviewed native artifact record")
        if operation.get("transcript_artifact") != pinned["transcript_artifact"]:
            errors.append(
                f"{location}.transcript_artifact differs from the reviewed transcript record"
            )
        if name in DRC_OPERATION_NAMES:
            if operation.get("native_report") != pinned["native_report"]:
                errors.append(
                    f"{location}.native_report differs from the reviewed LYRDB identity"
                )
        else:
            if operation.get("json_artifact") != pinned["json_artifact"]:
                errors.append(
                    f"{location}.json_artifact differs from the reviewed native JSON record"
                )
        if operation.get("result_filename") != pinned["result_filename"]:
            errors.append(f"{location}.result_filename differs from the reviewed filename")

        result_filename = operation.get("result_filename")
        if not _safe_filename(result_filename):
            errors.append(f"{location}.result_filename must be a plain JSON filename")
        elif not result_filename.endswith(".json"):
            errors.append(f"{location}.result_filename must end in .json")
        elif result_filename in seen_result_names:
            errors.append(f"duplicate result filename: {result_filename}")
        else:
            seen_result_names.add(result_filename)

        inputs = operation.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            errors.append(f"{location}.inputs must be a non-empty array")
            inputs = []
        input_paths: set[str] = set()
        for index, record in enumerate(inputs):
            item_location = f"{location}.inputs[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{item_location} must be an object")
                continue
            path = record.get("path")
            if not isinstance(path, str) or not path.startswith(("/design/", "/foss/pdks/")):
                errors.append(f"{item_location}.path must be under /design or /foss/pdks")
            elif path in input_paths:
                errors.append(f"duplicate input path in {location}: {path}")
            else:
                input_paths.add(path)
            for field in ("kind", "role"):
                if not isinstance(record.get(field), str) or not record[field]:
                    errors.append(f"{item_location}.{field} must be a non-empty string")
            _require_sha256(record.get("sha256"), f"{item_location}.sha256", errors)

        artifact = operation.get("artifact")
        if not isinstance(artifact, dict):
            errors.append(f"{location}.artifact must be an object")
            artifact = {}
        if "sha256" in artifact:
            errors.append(f"{location}.artifact must not pin a generated artifact hash")
        artifact_filename = artifact.get("filename")
        if not _safe_filename(artifact_filename):
            errors.append(f"{location}.artifact.filename must be a plain filename")
        elif artifact_filename in seen_artifact_names:
            errors.append(f"duplicate artifact filename: {artifact_filename}")
        else:
            seen_artifact_names.add(artifact_filename)
        if artifact.get("path") != f"/evidence/{artifact_filename}":
            errors.append(f"{location}.artifact.path must match artifact.filename under /evidence")
        for field in ("kind", "role"):
            if not isinstance(artifact.get(field), str) or not artifact[field]:
                errors.append(f"{location}.artifact.{field} must be a non-empty string")

        extra_artifact_keys = ["transcript_artifact"]
        if name == "lvs":
            extra_artifact_keys.insert(0, "json_artifact")
        for artifact_key in extra_artifact_keys:
            extra = operation.get(artifact_key)
            if not isinstance(extra, dict):
                errors.append(f"{location}.{artifact_key} must be an object")
                extra = {}
            if "sha256" in extra:
                errors.append(
                    f"{location}.{artifact_key} must not pin a generated artifact hash"
                )
            extra_filename = extra.get("filename")
            if not _safe_filename(extra_filename):
                errors.append(
                    f"{location}.{artifact_key}.filename must be a plain filename"
                )
            elif extra_filename in seen_artifact_names:
                errors.append(f"duplicate artifact filename: {extra_filename}")
            else:
                seen_artifact_names.add(extra_filename)
            if extra.get("path") != f"/evidence/{extra_filename}":
                errors.append(
                    f"{location}.{artifact_key}.path must match its filename under /evidence"
                )
            for field in ("kind", "role"):
                if not isinstance(extra.get(field), str) or not extra[field]:
                    errors.append(
                        f"{location}.{artifact_key}.{field} must be a non-empty string"
                    )

        arguments = operation.get("arguments")
        if not isinstance(arguments, dict):
            errors.append(f"{location}.arguments must be an object")
        else:
            report_argument = arguments.get("report")
            if not isinstance(report_argument, str) or report_argument != artifact.get("path"):
                errors.append(f"{location}.arguments.report must equal artifact.path")
            elif name == "lvs":
                json_artifact = operation.get("json_artifact")
                transcript_artifact = operation.get("transcript_artifact")
                json_path = (
                    json_artifact.get("path") if isinstance(json_artifact, dict) else None
                )
                transcript_path = (
                    transcript_artifact.get("path")
                    if isinstance(transcript_artifact, dict)
                    else None
                )
                if json_path != str(Path(report_argument).with_suffix(".json")):
                    errors.append(
                        f"{location}.json_artifact.path must be the native JSON path derived from report"
                    )
                if transcript_path != report_argument + ".openada.log":
                    errors.append(
                        f"{location}.transcript_artifact.path must be the transcript path derived from report"
                    )

    if errors:
        raise ConformanceError("invalid conformance manifest:\n- " + "\n- ".join(errors))


def _safe_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def default_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "openada" / "conformance" / "ihp-inverter"


def run_checked(argv: Iterable[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ConformanceError(f"cannot execute {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        suffix = f": {detail}" if detail else ""
        raise ConformanceError(
            f"command failed with exit code {completed.returncode}: {command!r}{suffix}"
        )
    return completed


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    image = manifest["runtime"]["image"]
    reference = image["reference"]
    completed = run_checked([container_engine, "image", "inspect", reference])
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(f"container image inspection was not valid JSON: {exc}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ConformanceError("container image inspection returned an unexpected document")
    record = records[0]
    if record.get("Os") != "linux" or record.get("Architecture") != "amd64":
        raise ConformanceError(
            f"local image is {record.get('Os')}/{record.get('Architecture')}, expected linux/amd64"
        )
    repo_digests = record.get("RepoDigests")
    if not isinstance(repo_digests, list) or reference not in repo_digests:
        raise ConformanceError(f"local image does not record the required digest: {reference}")
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    if not design_dir.is_dir() or not (design_dir / ".git").exists():
        raise ConformanceError(
            f"pinned design checkout is missing at {design_dir}; run setup.py first"
        )
    revision = manifest["design"]["revision"]
    head = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != revision:
        raise ConformanceError(f"design checkout is at {head}, expected {revision}")
    status = run_checked(
        ["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status:
        raise ConformanceError("design checkout has local changes; use a clean pinned checkout")

    expected_files = [manifest["design"]["license"]]
    for operation in manifest["operations"].values():
        expected_files.extend(
            record for record in operation["inputs"] if record["path"].startswith("/design/")
        )
    for record in expected_files:
        relative = record.get("path", "")
        if relative.startswith("/design/"):
            relative = relative.removeprefix("/design/")
        candidate = design_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"required regular design file is missing: {candidate}")
        actual = sha256_file(candidate)
        if actual != record["sha256"]:
            raise ConformanceError(
                f"design input hash mismatch for {candidate}: expected {record['sha256']}, got {actual}"
            )
    return head


def ensure_external_cache(cache_dir: Path, repository_root: Path) -> None:
    resolved_cache = cache_dir.expanduser().resolve()
    resolved_root = repository_root.resolve()
    if resolved_cache == resolved_root or resolved_root in resolved_cache.parents:
        raise ConformanceError(
            "the conformance cache must be outside the OpenADA checkout; do not vendor the design"
        )


def ensure_external_design_path(
    design_dir: Path,
    repository_root: Path,
    cache_dir: Path,
) -> Path:
    if design_dir.is_symlink():
        raise ConformanceError(
            f"the pinned design checkout path may not be a symbolic link: {design_dir}"
        )
    resolved_design = design_dir.expanduser().resolve()
    resolved_root = repository_root.resolve()
    resolved_cache = cache_dir.expanduser().resolve()
    if resolved_design == resolved_root or resolved_root in resolved_design.parents:
        raise ConformanceError(
            "the pinned design checkout must be outside the OpenADA checkout"
        )
    if resolved_design.parent != resolved_cache:
        raise ConformanceError(
            "the pinned design checkout must be the IHP-AnalogAcademy child of the selected cache"
        )
    return resolved_design


def require_mount_safe_path(path: Path) -> None:
    if "," in str(path):
        raise ConformanceError(f"container bind-mount source paths may not contain commas: {path}")
