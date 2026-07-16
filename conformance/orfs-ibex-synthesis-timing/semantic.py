#!/usr/bin/env python3
"""Publish and verify agent-facing ORFS Ibex synthesis/timing evidence."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from common import CHAIN_ID, ConformanceError, load_manifest, sha256_file
import verify as independent_verifier


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MANIFEST_PATH = HERE / "manifest.json"
BUNDLE = HERE / "semantic-artifacts"
REPLAY_DIR = HERE / "semantic-replays"
ORACLE_PATH = HERE / "semantic-oracle.json"
NORMALIZED_PATH = HERE / "semantic-normalized.json"
DECISION_PATH = HERE / "semantic-decision.json"
EVIDENCE_PATH = HERE / "semantic-evidence.json"
PROBES_PATH = HERE / "semantic-probes.json"
CONTRACT_PATH = HERE / "semantic-contract.json"
CHAIN_MANIFEST_PATH = HERE / "semantic-chain.json"
CHAIN_RUN_PATH = HERE / "semantic-chain-run.json"
TEST_PATH = REPOSITORY_ROOT / "tests/test_orfs_ibex_synthesis_timing_conformance.py"
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402


SYNTHESIS_ROWS = [
    "surface|openada.surface/cli.synthesize/v1",
    "preflight|asic-netlist-synthesized",
    "profile|openada.operation/logic.synthesize/v1alpha1",
    (
        "assertion|openada.operation/logic.synthesize/v1alpha1|"
        "openada.assertion/synthesized-netlist.valid/v1alpha1"
    ),
    (
        "feature|openada.operation/logic.synthesize/v1alpha1|"
        "openada.feature/synthesis.asic-liberty/v1alpha1"
    ),
    (
        "native-mapping|openada.operation/logic.synthesize/v1alpha1|"
        "org.openada.driver.yosys|org.yosyshq.yosys|"
        "openada.feature/synthesis.asic-liberty/v1alpha1"
    ),
    (
        "provider|org.openada.driver.yosys|"
        "openada.operation/logic.synthesize/v1alpha1|"
        "openada.feature/synthesis.asic-liberty/v1alpha1"
    ),
]
TIMING_ROWS = [
    "surface|openada.surface/cli.timing-analyze/v1",
    "preflight|timing-constraints-satisfied",
    "profile|openada.operation/timing.analyze/v1alpha1",
    (
        "assertion|openada.operation/timing.analyze/v1alpha1|"
        "openada.assertion/timing.constraints-satisfied/v1alpha1"
    ),
    (
        "feature|openada.operation/timing.analyze/v1alpha1|"
        "openada.feature/timing.setup-hold/v1alpha1"
    ),
    (
        "native-mapping|openada.operation/timing.analyze/v1alpha1|"
        "org.openada.driver.opensta|org.openroad.opensta|"
        "openada.feature/timing.setup-hold/v1alpha1"
    ),
    (
        "provider|org.openada.driver.opensta|"
        "openada.operation/timing.analyze/v1alpha1|"
        "openada.feature/timing.setup-hold/v1alpha1"
    ),
]
ROWS = [*SYNTHESIS_ROWS, *TIMING_ROWS]
NATIVE_FILES = [
    "design-provenance.json",
    *independent_verifier.NATIVE_ARTIFACT_PATHS,
    "run.json",
]
SYNTH_NEGATIVE_ID = "real-synthesis-missing-top"
TIMING_NEGATIVE_ID = "real-setup-timing-violation"
SYNTH_TAMPER_ID = "reconciled-mapped-stat-count-change"
TIMING_TAMPER_ID = "reconciled-setup-path-slack-change"
SYNTH_TAMPER_DIAGNOSTIC = "mapped statistics cell histogram does not sum to num_cells"
TIMING_TAMPER_DIAGNOSTIC = "setup critical path"
LIMITATIONS = [
    {
        "id": "single-corner-ideal-interconnect",
        "impact": (
            "Timing uses one Nangate45 Liberty/SDC corner with ideal interconnect and no "
            "SPEF. It is useful synthesis-stage evidence, not routed or MCMM signoff timing."
        ),
    },
    {
        "id": "setup-timing-fails",
        "impact": (
            "The reviewed run has negative setup WNS/TNS. Synthesis evidence may inform "
            "architecture and implementation work, but the timing assertion is blocked."
        ),
    },
    {
        "id": "no-physical-or-power-closure",
        "impact": (
            "The chain performs no floorplan, placement, clock-tree synthesis, routing, "
            "extraction, IR-drop, power, DRC, LVS, or gate-level functional simulation."
        ),
    },
    {
        "id": "implementation-specific-language-and-library-support",
        "impact": (
            "The evidence certifies the pinned Slang/Yosys, OpenSTA, Liberty, SDC, and "
            "Nangate collateral behavior, not complete language or file-format conformance."
        ),
    },
    {
        "id": "inactive-include-branches-recorded",
        "impact": (
            "Five literal includes in inactive conditional branches are unresolved and "
            "retained explicitly; the active Slang elaboration and resolved closure pass."
        ),
    },
]


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _read_json(path: Path, *, label: str, maximum: int = 64 * 1024 * 1024) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_symlink():
        raise ConformanceError(f"{label} must be one regular non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum:
        raise ConformanceError(f"{label} size is outside 1..{maximum} bytes: {path}")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def _write_json(path: Path, document: object) -> None:
    encoded = (
        json.dumps(document, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _file_record(path: Path, repository_path: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ConformanceError(f"publication artifact is missing or unsafe: {path}")
    return {
        "repository_path": repository_path,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _bundle_record(relative: str) -> dict[str, Any]:
    return _file_record(
        BUNDLE / relative,
        f"conformance/orfs-ibex-synthesis-timing/semantic-artifacts/{relative}",
    )


def _native_records() -> list[dict[str, Any]]:
    return [_bundle_record(relative) for relative in NATIVE_FILES]


def _copy_verified_evidence(evidence: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".orfs-ibex-artifacts-", dir=HERE))
    try:
        for relative in NATIVE_FILES:
            source = evidence / relative
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if BUNDLE.exists():
            if BUNDLE.is_symlink() or not BUNDLE.is_dir():
                raise ConformanceError(f"refusing to replace unsafe publication path: {BUNDLE}")
            shutil.rmtree(BUNDLE)
        os.replace(staging, BUNDLE)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _replace_run_record(run: dict[str, Any], evidence: Path, relative: str) -> None:
    records = [item for item in run["native_artifacts"] if item.get("path") == relative]
    if len(records) != 1:
        raise ConformanceError(f"tamper fixture cannot find one run record for {relative}")
    path = evidence / relative
    records[0]["bytes"] = path.stat().st_size
    records[0]["sha256"] = sha256_file(path)


def _replace_result_artifact(result: dict[str, Any], evidence: Path, relative: str) -> None:
    absolute = f"/evidence/{relative}"
    records = [item for item in result["artifacts"] if item.get("path") == absolute]
    if len(records) != 1:
        raise ConformanceError(f"tamper fixture cannot find one result record for {absolute}")
    path = evidence / relative
    records[0]["bytes"] = path.stat().st_size
    records[0]["sha256"] = sha256_file(path)


def _run_synthesis_tamper_probe(manifest: dict[str, Any], evidence: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openada-orfs-ibex-synth-tamper-") as temporary:
        tampered = Path(temporary) / "evidence"
        shutil.copytree(evidence, tampered)
        stats_path = tampered / "synthesis/mapped-stats.json"
        stats = _read_json(stats_path, label="tamper mapped statistics")
        stats["design"]["num_cells"] += 1
        _write_json(stats_path, stats)

        result_path = tampered / "synthesis/synthesize.result.json"
        result = _read_json(result_path, label="tamper synthesis result")
        _replace_result_artifact(result, tampered, "synthesis/mapped-stats.json")
        _write_json(result_path, result)

        run_path = tampered / "run.json"
        run = _read_json(run_path, label="tamper run")
        _replace_run_record(run, tampered, "synthesis/mapped-stats.json")
        _replace_run_record(run, tampered, "synthesis/synthesize.result.json")
        _write_json(run_path, run)
        try:
            independent_verifier.verify_evidence(
                manifest,
                tampered,
                manifest_sha256=sha256_file(MANIFEST_PATH),
            )
        except ConformanceError as exc:
            diagnostic = str(exc)
        else:
            raise ConformanceError("independent verifier accepted reconciled mapped-stat tampering")
    if SYNTH_TAMPER_DIAGNOSTIC not in diagnostic:
        raise ConformanceError(
            "synthesis tamper probe failed for the wrong reason: "
            f"expected {SYNTH_TAMPER_DIAGNOSTIC!r}, got {diagnostic!r}"
        )
    return {
        "id": SYNTH_TAMPER_ID,
        "status": "rejected",
        "expected_status": "unknown",
        "covers": SYNTHESIS_ROWS,
        "mutation": (
            "changed native mapped-stat num_cells and reconciled both the OpenADA "
            "artifact digest and run-level result/artifact digests"
        ),
        "required_diagnostic": SYNTH_TAMPER_DIAGNOSTIC,
        "observed_diagnostic": diagnostic,
        "verifier": "conformance/orfs-ibex-synthesis-timing/verify.py",
    }


def _run_timing_tamper_probe(manifest: dict[str, Any], evidence: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openada-orfs-ibex-timing-tamper-") as temporary:
        tampered = Path(temporary) / "evidence"
        shutil.copytree(evidence, tampered)
        report_path = tampered / "timing/setup-paths.json"
        report = _read_json(report_path, label="tamper setup paths")
        checks = report.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ConformanceError("timing tamper fixture lacks setup paths")
        critical = min(checks, key=lambda item: item["slack"])
        critical["slack"] += 1.0
        _write_json(report_path, report)

        result_path = tampered / "timing/timing-analyze.result.json"
        result = _read_json(result_path, label="tamper timing result")
        _replace_result_artifact(result, tampered, "timing/setup-paths.json")
        _write_json(result_path, result)

        run_path = tampered / "run.json"
        run = _read_json(run_path, label="tamper run")
        _replace_run_record(run, tampered, "timing/setup-paths.json")
        _replace_run_record(run, tampered, "timing/timing-analyze.result.json")
        _write_json(run_path, run)
        try:
            independent_verifier.verify_evidence(
                manifest,
                tampered,
                manifest_sha256=sha256_file(MANIFEST_PATH),
            )
        except ConformanceError as exc:
            diagnostic = str(exc)
        else:
            raise ConformanceError("independent verifier accepted reconciled timing-path tampering")
    if TIMING_TAMPER_DIAGNOSTIC not in diagnostic:
        raise ConformanceError(
            "timing tamper probe failed for the wrong reason: "
            f"expected {TIMING_TAMPER_DIAGNOSTIC!r}, got {diagnostic!r}"
        )
    return {
        "id": TIMING_TAMPER_ID,
        "status": "rejected",
        "expected_status": "unknown",
        "covers": TIMING_ROWS,
        "mutation": (
            "changed the native setup critical-path slack and reconciled both the OpenADA "
            "artifact digest and run-level result/artifact digests"
        ),
        "required_diagnostic": TIMING_TAMPER_DIAGNOSTIC,
        "observed_diagnostic": diagnostic,
        "verifier": "conformance/orfs-ibex-synthesis-timing/verify.py",
    }


def _negative_replays(verified: dict[str, Any]) -> list[dict[str, Any]]:
    negative = verified["negative"]
    timing = verified["timing"]["result"]
    setup = timing["data"]["setup"]
    return [
        {
            "id": SYNTH_NEGATIVE_ID,
            "status": "observed",
            "expected_status": "fail",
            "covers": SYNTHESIS_ROWS,
            "fixture": "the pinned Ibex sources with a real Yosys request for missing_ibex_core",
            "execution_status": negative["result"]["execution"]["status"],
            "exit_code": negative["result"]["execution"]["exit_code"],
            "engineering_status": negative["result"]["engineering"]["status"],
            "required_diagnostic": "missing_ibex_core",
            "observed_diagnostic": negative["diagnostic"],
            "native_transcript_bound": True,
        },
        {
            "id": TIMING_NEGATIVE_ID,
            "status": "observed",
            "expected_status": "fail",
            "covers": TIMING_ROWS,
            "fixture": (
                "the real mapped Ibex netlist analyzed against the pinned Nangate45 "
                "Liberty and ORFS Ibex SDC"
            ),
            "execution_status": timing["execution"]["status"],
            "exit_code": timing["execution"]["exit_code"],
            "engineering_status": timing["engineering"]["status"],
            "required_diagnostic": "setup WNS is negative",
            "observed_diagnostic": f"setup WNS is negative: {setup['wns_s']:.12g} s",
            "native_transcript_bound": True,
        },
    ]


def _contract_document() -> dict[str, Any]:
    tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"), filename=str(TEST_PATH))
    test_count = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        for node in tree.body
    )
    return {
        "schema": "openada.semantic-contract-test/v0alpha1",
        "chain_id": CHAIN_ID,
        "status": "pass",
        "tests": {
            "suite": "tests/test_orfs_ibex_synthesis_timing_conformance.py",
            "suite_sha256": sha256_file(TEST_PATH),
            "passed": test_count,
            "failed": 0,
            "execution": "fresh pytest exit status zero",
        },
        "native_replay": {
            "independent_verifier": "pass",
            "real_synthesis_negative": "pass",
            "real_timing_negative": "pass",
            "synthesis_tamper_probe": "pass",
            "timing_tamper_probe": "pass",
        },
    }


def _run_contract_tests() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TEST_PATH.relative_to(REPOSITORY_ROOT).as_posix()],
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise ConformanceError("ORFS Ibex contract tests failed: " + completed.stdout[-4000:])
    return _contract_document()


def _chain_artifact(
    path: Path,
    role: str,
    source_step: str | None,
    source_output: str | None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ConformanceError(f"cannot bind unsafe chain artifact: {path}")
    return {
        "repository_path": path.relative_to(REPOSITORY_ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": replay_id,
    }


def _native_definition(relative: str) -> tuple[Path, str, str, str, None]:
    if relative == "design-provenance.json":
        return (
            BUNDLE / relative,
            "design-provenance",
            "materialize-pinned-inputs",
            "design-provenance",
            None,
        )
    if relative.startswith("synthesis/"):
        step = "synthesize-ibex"
    elif relative.startswith("timing/"):
        step = "analyze-ibex-timing"
    elif relative.startswith("negative/"):
        step = "synthesis-missing-top"
    else:
        step = "synthesize-ibex"
    output = relative.replace("/", "-").replace(".", "-")
    return (BUNDLE / relative, "native-artifact", step, output, None)


def _chain_artifact_definitions() -> list[tuple[Path, str, str | None, str | None, str | None]]:
    definitions: list[tuple[Path, str, str | None, str | None, str | None]] = [
        (CONTRACT_PATH, "contract-test", "contract-tests", "contract-test-verdict", None),
    ]
    # A valid OpenSTA constraint check is intentionally zero bytes. The negative
    # synthesis replay also has a byte-identical RTL dependency closure because
    # only the requested top changes. Both remain bound by the normalized result
    # and run receipt, but the release trust index binds each distinct payload
    # once and semantic-chain artifact records require nonempty files.
    definitions.extend(
        _native_definition(relative)
        for relative in NATIVE_FILES
        if relative not in {"timing/check-setup.txt", "negative/rtl-inputs.json"}
    )
    definitions.extend(
        (
            (ORACLE_PATH, "independent-oracle", "independent-verifier", "independent-oracle-verdict", None),
            (NORMALIZED_PATH, "normalized-evidence", "normalize-digital-evidence", "normalized-digital-evidence", None),
            (DECISION_PATH, "downstream-decision", "engineering-decision", "downstream-digital-decision", None),
            (EVIDENCE_PATH, "agent-visible-evidence", "agent-evidence", "agent-visible-digital-evidence", None),
            (REPLAY_DIR / f"{SYNTH_NEGATIVE_ID}.json", "negative-replay", None, None, SYNTH_NEGATIVE_ID),
            (REPLAY_DIR / f"{TIMING_NEGATIVE_ID}.json", "negative-replay", None, None, TIMING_NEGATIVE_ID),
            (REPLAY_DIR / f"{SYNTH_TAMPER_ID}.json", "tamper-replay", None, None, SYNTH_TAMPER_ID),
            (REPLAY_DIR / f"{TIMING_TAMPER_ID}.json", "tamper-replay", None, None, TIMING_TAMPER_ID),
        )
    )
    return definitions


def _write_chain_run(source_receipt: dict[str, Any]) -> None:
    manifest = _read_json(CHAIN_MANIFEST_PATH, label="semantic chain manifest")
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": manifest["id"],
        "chain_manifest_sha256": sha256_file(CHAIN_MANIFEST_PATH),
        "semantic_subject_sha256": semantic_subject(
            REPOSITORY_ROOT,
            REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
        ),
        "source_attestation": source_receipt,
        "status": "pass",
        "checks": {
            "contract_test": True,
            "pinned_real_design": True,
            "native_run": True,
            "independent_artifact_check": True,
            "normalized_evidence": True,
            "downstream_decision": True,
            "negative_replay": True,
            "tamper_replay": True,
            "agent_visible_evidence": True,
        },
        "artifacts": [
            _chain_artifact(path, role, step, output, replay)
            for path, role, step, output, replay in _chain_artifact_definitions()
        ],
        "extensions": {
            "org.openada.digital": {
                "receipt_status": source_receipt["receipt_class"],
                "analysis_scope": "single-corner ideal-interconnect synthesis-stage timing; not signoff",
            }
        },
    }
    _write_json(CHAIN_RUN_PATH, document)


def _validate_chain_documents() -> None:
    manifest = _read_json(CHAIN_MANIFEST_PATH, label="semantic chain manifest")
    run = _read_json(CHAIN_RUN_PATH, label="semantic chain run")
    for document, schema_path, label in (
        (manifest, REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json", "semantic chain manifest"),
        (run, REPOSITORY_ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json", "semantic chain run"),
    ):
        schema = _read_json(schema_path, label=f"{label} schema")
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ConformanceError(f"{label} violates its schema: {errors[0].message}")
    _expect(run["chain_id"], manifest["id"], "chain run ID")
    _expect(run["chain_manifest_sha256"], sha256_file(CHAIN_MANIFEST_PATH), "chain manifest digest")
    subject = semantic_subject(REPOSITORY_ROOT, REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json")
    _expect(run["semantic_subject_sha256"], subject, "chain semantic subject")
    _expect(run["source_attestation"]["semantic_subject_sha256"], subject, "chain source subject")
    expected = _chain_artifact_definitions()
    _expect(len(run["artifacts"]), len(expected), "chain artifact count")
    for index, (record, definition) in enumerate(zip(run["artifacts"], expected, strict=True)):
        path, role, step, output, replay = definition
        _expect(record, _chain_artifact(path, role, step, output, replay), f"chain artifact {index}")


def _publish(evidence: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    verified = independent_verifier.verify_evidence(
        manifest,
        evidence,
        manifest_sha256=sha256_file(MANIFEST_PATH),
    )
    negatives = _negative_replays(verified)
    synthesis_tamper = _run_synthesis_tamper_probe(manifest, evidence)
    timing_tamper = _run_timing_tamper_probe(manifest, evidence)
    tampers = [synthesis_tamper, timing_tamper]
    _copy_verified_evidence(evidence)

    if REPLAY_DIR.exists():
        if REPLAY_DIR.is_symlink() or not REPLAY_DIR.is_dir():
            raise ConformanceError(f"refusing to replace unsafe replay path: {REPLAY_DIR}")
        shutil.rmtree(REPLAY_DIR)
    REPLAY_DIR.mkdir()
    for replay in [*negatives, *tampers]:
        _write_json(
            REPLAY_DIR / f"{replay['id']}.json",
            {
                "schema": "openada.semantic-replay-verdict/v0alpha1",
                "chain_id": CHAIN_ID,
                **replay,
            },
        )

    synthesis = verified["synthesis"]
    timing = verified["timing"]
    native_records = _native_records()
    oracle = {
        "schema": "openada.digital-independent-oracle/v0alpha1",
        "chain_id": CHAIN_ID,
        "verdict": "pass",
        "verifier": {
            "repository_path": "conformance/orfs-ibex-synthesis-timing/verify.py",
            "sha256": sha256_file(HERE / "verify.py"),
            "imports_openada": False,
        },
        "bindings": {
            "manifest_sha256": sha256_file(MANIFEST_PATH),
            "run_sha256": sha256_file(BUNDLE / "run.json"),
            "native_artifacts": native_records,
        },
        "synthesis_facts": {
            "engineering_status": "pass",
            "top": "ibex_core",
            "mapping_complete": True,
            "tool_identity_stable": True,
            "abc_tool": copy.deepcopy(synthesis["result"]["data"]["abc_tool"]),
            "abc_tool_identity_stable": True,
            "environment_policy": copy.deepcopy(
                synthesis["result"]["data"]["environment_policy"]
            ),
            "mapped_structure": copy.deepcopy(
                synthesis["result"]["data"]["mapped_structure"]
            ),
            "stats": copy.deepcopy(synthesis["stats"]),
            "structure": copy.deepcopy(synthesis["structure"]),
            "unmapped_cell_types": [],
        },
        "timing_facts": {
            "engineering_status": "fail",
            "analysis_model": timing["result"]["data"]["analysis_model"],
            "environment_policy": "closed-opensta-runtime-v1",
            "netlist_validation": timing["result"]["data"]["netlist_validation"],
            "liberty_validation": timing["result"]["data"]["liberty_validation"],
            "tool_identity_stable": True,
            "setup": copy.deepcopy(timing["result"]["data"]["setup"]),
            "hold": copy.deepcopy(timing["result"]["data"]["hold"]),
            "constraints_complete": True,
            "reports_complete": True,
            "timing_constraints_satisfied": False,
        },
        "negative_facts": copy.deepcopy(negatives),
        "tamper_facts": copy.deepcopy(tampers),
    }
    _write_json(ORACLE_PATH, oracle)

    normalized = {
        "schema": "openada.digital-normalized-evidence/v0alpha1",
        "chain_id": CHAIN_ID,
        "coverage": ROWS,
        "design": {
            "repository": manifest["design"]["repository"],
            "revision": manifest["design"]["revision"],
            "tree": manifest["design"]["tree"],
            "upstream_revision": manifest["design"]["upstream"]["revision"],
            "top": "ibex_core",
        },
        "technology": copy.deepcopy(manifest["technology"]),
        "runtime": {
            "image_reference": manifest["runtime"]["image"]["reference"],
            "image_config_digest": manifest["runtime"]["image"]["config_digest"],
            "tools": copy.deepcopy(manifest["runtime"]["tools"]),
            "eda_network": "none",
        },
        "synthesis": {
            "surface_id": "openada.surface/cli.synthesize/v1",
            "operation_profile": "openada.operation/logic.synthesize/v1alpha1",
            "assertion_profile": "openada.assertion/synthesized-netlist.valid/v1alpha1",
            "assertion": "asic-netlist-synthesized",
            "execution_status": synthesis["result"]["execution"]["status"],
            "exit_code": synthesis["result"]["execution"]["exit_code"],
            "engineering_status": synthesis["result"]["engineering"]["status"],
            "summary": synthesis["result"]["engineering"]["summary"],
            "evidence": {
                "stats": copy.deepcopy(synthesis["stats"]),
                "inference_stats": copy.deepcopy(synthesis["inference_stats"]),
                "structure": copy.deepcopy(synthesis["structure"]),
                "mapping_policy": copy.deepcopy(synthesis["result"]["data"]["mapping_policy"]),
                "mapping_complete": True,
                "mapped_structure": copy.deepcopy(
                    synthesis["result"]["data"]["mapped_structure"]
                ),
                "unmapped_cell_types": [],
                "inputs_stable": True,
                "dependency_closure_stable": True,
                "tool_identity_stable": True,
                "abc_tool": copy.deepcopy(synthesis["result"]["data"]["abc_tool"]),
                "abc_tool_identity_stable": True,
                "environment_policy": copy.deepcopy(
                    synthesis["result"]["data"]["environment_policy"]
                ),
            },
        },
        "timing": {
            "surface_id": "openada.surface/cli.timing-analyze/v1",
            "operation_profile": "openada.operation/timing.analyze/v1alpha1",
            "assertion_profile": "openada.assertion/timing.constraints-satisfied/v1alpha1",
            "assertion": "timing-constraints-satisfied",
            "execution_status": timing["result"]["execution"]["status"],
            "exit_code": timing["result"]["execution"]["exit_code"],
            "engineering_status": timing["result"]["engineering"]["status"],
            "summary": timing["result"]["engineering"]["summary"],
            "evidence": copy.deepcopy(timing["result"]["data"]),
        },
        "independent_oracle": _file_record(
            ORACLE_PATH,
            "conformance/orfs-ibex-synthesis-timing/semantic-oracle.json",
        ),
        "run": {
            "created_at": verified["run"]["created_at"],
            "checkout_state_unchanged": verified["run"]["openada_checkout"]["state_unchanged"],
            "checkout_commit_exact": verified["run"]["openada_checkout"]["commit_exact"],
        },
    }
    _write_json(NORMALIZED_PATH, normalized)

    decision = {
        "schema": "openada.digital-engineering-decision/v0alpha1",
        "chain_id": CHAIN_ID,
        "decision": "block",
        "scope": (
            "use the mapped synthesis evidence for architecture and implementation iteration, "
            "but block timing closure and signoff claims"
        ),
        "basis": [
            "the pinned public Ibex source closure elaborated through the declared Slang frontend",
            "Yosys produced a flattened netlist whose complete cell histogram is declared by the pinned Liberty",
            "mapped and inference statistics, mapped Verilog, mapped JSON, scripts, logs, and configuration are digest-bound",
            "OpenSTA completed with complete constraints and setup/hold reports under the declared single-corner ideal-interconnect model",
            "setup WNS and TNS are negative, so the timing assertion fails even though hold is nonnegative",
            "the real missing-top synthesis request and real timing violation are preserved as fail replays",
            "reconciled synthesis-stat and timing-path tampering are rejected as unknown evidence",
        ],
        "next_checks": [
            "inspect the setup critical path and revise constraints, microarchitecture, or synthesis strategy",
            "run placement, clock-tree synthesis, routing, extraction, and multi-corner timing",
            "perform equivalence or gate-level functional verification of the mapped netlist",
            "add power, signal-integrity, DRC, and LVS evidence before physical signoff decisions",
        ],
        "block_conditions": [
            "setup or hold WNS is negative",
            "constraints, path reports, metrics, or input-stability evidence are incomplete",
            "any mapped cell is absent from the exact Liberty or any process/memory remains",
            "any source, tool, image, script, netlist, constraint, report, or receipt digest differs",
        ],
        "evidence": {
            "normalized": _file_record(
                NORMALIZED_PATH,
                "conformance/orfs-ibex-synthesis-timing/semantic-normalized.json",
            ),
            "oracle": _file_record(
                ORACLE_PATH,
                "conformance/orfs-ibex-synthesis-timing/semantic-oracle.json",
            ),
            "negative_replays": [
                _file_record(
                    REPLAY_DIR / f"{replay['id']}.json",
                    f"conformance/orfs-ibex-synthesis-timing/semantic-replays/{replay['id']}.json",
                )
                for replay in negatives
            ],
            "tamper_replays": [
                _file_record(
                    REPLAY_DIR / f"{replay['id']}.json",
                    f"conformance/orfs-ibex-synthesis-timing/semantic-replays/{replay['id']}.json",
                )
                for replay in tampers
            ],
        },
        "limitations": LIMITATIONS,
    }
    _write_json(DECISION_PATH, decision)

    agent = {
        "schema": "openada.digital-agent-evidence/v0alpha1",
        "chain_id": CHAIN_ID,
        "decision": decision["decision"],
        "scope": decision["scope"],
        "basis": copy.deepcopy(decision["basis"]),
        "next_checks": copy.deepcopy(decision["next_checks"]),
        "operations": {
            "synthesis": copy.deepcopy(normalized["synthesis"]),
            "timing": copy.deepcopy(normalized["timing"]),
            "synthesis_missing_top_negative": {
                "decision": "block",
                "engineering_status": "fail",
                "diagnostic": negatives[0]["observed_diagnostic"],
            },
        },
        "replays": {
            "negative_replays": copy.deepcopy(negatives),
            "tamper_replays": copy.deepcopy(tampers),
        },
        "trust_chain": {
            "oracle": _file_record(
                ORACLE_PATH,
                "conformance/orfs-ibex-synthesis-timing/semantic-oracle.json",
            ),
            "normalized": _file_record(
                NORMALIZED_PATH,
                "conformance/orfs-ibex-synthesis-timing/semantic-normalized.json",
            ),
            "decision": _file_record(
                DECISION_PATH,
                "conformance/orfs-ibex-synthesis-timing/semantic-decision.json",
            ),
            "native_artifacts": native_records,
        },
        "limitations": LIMITATIONS,
        "standards": {
            "ieee_measurement_standard": {
                "status": "not-applicable",
                "basis": (
                    "The chain reports deterministic STA slack metrics and performs no "
                    "waveform-quality measurement such as SNR."
                ),
            },
            "hdl_language": {
                "standard": "IEEE 1800-2023",
                "official_url": "https://standards.ieee.org/ieee/1800/7743/",
                "status": "context-only",
                "basis": (
                    "The request declares SystemVerilog 1800-2017 to the pinned Slang "
                    "frontend; this is implementation evidence, not full standard compliance."
                ),
            },
            "timing_formats": {
                "status": "implementation-specific",
                "basis": (
                    "Liberty and SDC inputs are pinned by exact bytes; no IEEE conformance "
                    "claim is made for those industry formats."
                ),
            },
        },
    }
    _write_json(EVIDENCE_PATH, agent)
    _write_json(
        PROBES_PATH,
        {
            "schema": "openada.semantic-chain-probes/v0alpha1",
            "chain_id": CHAIN_ID,
            "status": "pass",
            "negative_replays": negatives,
            "tamper_replays": tampers,
        },
    )
    contract = _contract_document()
    _write_json(CONTRACT_PATH, contract)
    _write_chain_run(verified["run"]["source_attestation"])
    _expect(_run_contract_tests(), contract, "fresh contract-test report")
    verify_publication()


def _verify_file_record(record: Any, path: Path, repository_path: str) -> None:
    if not isinstance(record, dict):
        raise ConformanceError(f"trust record for {repository_path} must be an object")
    _expect(record.get("repository_path"), repository_path, f"{repository_path}.repository_path")
    _expect(record.get("bytes"), path.stat().st_size, f"{repository_path}.bytes")
    _expect(record.get("sha256"), sha256_file(path), f"{repository_path}.sha256")


def verify_publication() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    verified = independent_verifier.verify_evidence(
        manifest,
        BUNDLE,
        manifest_sha256=sha256_file(MANIFEST_PATH),
    )
    negatives = _negative_replays(verified)
    tampers = [
        _run_synthesis_tamper_probe(manifest, BUNDLE),
        _run_timing_tamper_probe(manifest, BUNDLE),
    ]
    oracle = _read_json(ORACLE_PATH, label="semantic oracle")
    normalized = _read_json(NORMALIZED_PATH, label="normalized evidence")
    decision = _read_json(DECISION_PATH, label="engineering decision")
    agent = _read_json(EVIDENCE_PATH, label="agent evidence")
    probes = _read_json(PROBES_PATH, label="semantic probes")
    contract = _read_json(CONTRACT_PATH, label="semantic contract")
    for label, document in (
        ("oracle", oracle),
        ("normalized", normalized),
        ("decision", decision),
        ("agent", agent),
        ("probes", probes),
        ("contract", contract),
    ):
        _expect(document.get("chain_id"), CHAIN_ID, f"{label}.chain_id")
    _expect(oracle.get("verdict"), "pass", "oracle.verdict")
    _expect(normalized.get("coverage"), ROWS, "normalized coverage")
    _expect(normalized["synthesis"]["engineering_status"], "pass", "normalized synthesis status")
    _expect(normalized["timing"]["engineering_status"], "fail", "normalized timing status")
    _expect(decision.get("decision"), "block", "engineering decision")
    _expect(agent.get("decision"), "block", "agent decision")
    _expect(agent["replays"]["negative_replays"], negatives, "agent negative replays")
    _expect(agent["replays"]["tamper_replays"], tampers, "agent tamper replays")
    _expect(probes.get("status"), "pass", "probe status")
    _expect(contract, _contract_document(), "contract evidence")
    trust = agent["trust_chain"]
    _verify_file_record(
        trust["oracle"],
        ORACLE_PATH,
        "conformance/orfs-ibex-synthesis-timing/semantic-oracle.json",
    )
    _verify_file_record(
        trust["normalized"],
        NORMALIZED_PATH,
        "conformance/orfs-ibex-synthesis-timing/semantic-normalized.json",
    )
    _verify_file_record(
        trust["decision"],
        DECISION_PATH,
        "conformance/orfs-ibex-synthesis-timing/semantic-decision.json",
    )
    native = trust.get("native_artifacts")
    if not isinstance(native, list) or len(native) != len(NATIVE_FILES):
        raise ConformanceError("agent trust chain does not bind every native artifact")
    for record, relative in zip(native, NATIVE_FILES, strict=True):
        _verify_file_record(
            record,
            BUNDLE / relative,
            f"conformance/orfs-ibex-synthesis-timing/semantic-artifacts/{relative}",
        )
    if len({sha256_file(ORACLE_PATH), sha256_file(NORMALIZED_PATH), sha256_file(DECISION_PATH), sha256_file(EVIDENCE_PATH)}) != 4:
        raise ConformanceError("oracle, normalized, decision, and agent evidence must be distinct")
    _validate_chain_documents()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.publish:
            if args.evidence_dir is None:
                raise ConformanceError("--publish requires --evidence-dir")
            _publish(args.evidence_dir.expanduser().resolve())
            print(f"Published verified ORFS Ibex agent evidence: {EVIDENCE_PATH}")
        else:
            if args.evidence_dir is not None:
                raise ConformanceError("--evidence-dir is only valid with --publish")
            verify_publication()
            print(f"Verified published ORFS Ibex agent evidence: {EVIDENCE_PATH}")
    except ConformanceError as exc:
        print(f"semantic evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
