#!/usr/bin/env python3
"""Publish and verify agent-facing evidence for the pinned IHP physical chain.

The native runner deliberately writes outside the repository.  This module is
the reviewed publication boundary: it first invokes the independent verifier,
runs negative and tamper probes, copies the complete verified evidence set into
a repository-local bundle, and emits a closed decision document for agents.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

from common import ConformanceError, load_manifest, sha256_file
import verify as independent_verifier

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
DEFAULT_SNAPSHOT = HERE / "semantic-evidence.json"
DEFAULT_PROBE_REPORT = HERE / "semantic-probes.json"
DEFAULT_BUNDLE = HERE / "semantic-artifacts"
DEFAULT_NORMALIZED = HERE / "semantic-normalized.json"
DEFAULT_ORACLE = HERE / "semantic-oracle.json"
DEFAULT_DECISION = HERE / "semantic-decision.json"
DEFAULT_REPLAY_DIR = HERE / "semantic-replays"
CHAIN_MANIFEST_PATH = HERE / "semantic-chain.json"
CHAIN_RUN_PATH = HERE / "semantic-chain-run.json"
PUBLICATION_PREFIX = "conformance/ihp-inverter/semantic-artifacts"
CHAIN_ID = "openada.chain/ihp-inverter-physical/v1"
SNAPSHOT_SCHEMA = "openada.physical-decision/v0alpha1"
PROBE_SCHEMA = "openada.semantic-chain-probes/v0alpha1"
NORMALIZED_SCHEMA = "openada.physical-normalized/v0alpha1"
ORACLE_SCHEMA = "openada.physical-oracle/v0alpha1"
DECISION_SCHEMA = "openada.physical-decision-verdict/v0alpha1"
REPLAY_VERDICT_SCHEMA = "openada.semantic-replay-verdict/v0alpha1"
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z$"
)
MAX_JSON_BYTES = 16 * 1024 * 1024
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402

DRC_ROWS = [
    "surface|openada.surface/cli.drc/v1",
    "preflight|drc-clean",
]
LVS_ROWS = [
    "surface|openada.surface/cli.lvs/v1",
    "preflight|lvs-match",
]
EXPECTED_DRC_CATEGORY_COUNTS = [
    {"category": "M1.b", "category_path": ["M1.b"], "violations": 6},
    {"category": "Cnt.d", "category_path": ["Cnt.d"], "violations": 1},
    {"category": "Cnt.e", "category_path": ["Cnt.e"], "violations": 1},
]
EXPECTED_DRC_DESCRIPTIONS = {
    "Cnt.d": "5.14. Cnt.d Min. GatPoly enclosure of Cont is 0.07 um",
    "Cnt.e": "5.14. Cnt.e Min. Cont on GatPoly space to Activ is 0.14 um",
    "M1.b": "5.16. M1.b: Min. Metal1 (drawing + filler) space or notch: 0.18 μm.",
}
EXPECTED_DRC_COORDINATES = [
    [[3.215, -0.065], [3.055, -0.065], [3.283, -0.08], [2.987, -0.08]],
    [[3.055, 0.095], [3.215, 0.095], [3.355, 0.1], [2.915, 0.1]],
    [[2.93, -0.065], [2.93, 0.095], [2.79, 0.208], [2.79, -0.178]],
    [[3.46, -0.095], [3.46, 0.208], [3.32, 0.095], [3.32, -0.065]],
    [[3.46, -0.095], [3.46, 0.1], [3.32, 0.095], [3.32, -0.065]],
    [[2.93, -0.065], [2.93, 0.095], [2.79, 0.1], [2.79, -0.178]],
    [[0.37, -0.113], [0.37, -0.005], [0.23, 0.108], [0.23, 0.0]],
    [[0.76, -0.005], [0.76, -0.113], [0.9, 0.0], [0.9, 0.108]],
]
EXPECTED_DRC_CELLS = [
    "lvs_tester",
    "lvs_tester",
    "lvs_tester",
    "lvs_tester",
    "lvs_tester",
    "lvs_tester",
    "nmos$1",
    "nmos$1",
]
EXPECTED_DRC_CATEGORIES = [
    "Cnt.d",
    "Cnt.e",
    "M1.b",
    "M1.b",
    "M1.b",
    "M1.b",
    "M1.b",
    "M1.b",
]
EXPECTED_DEVICE_COUNTS = [
    [
        ["ntap1", 1],
        ["ptap1", 1],
        ["sg13_lv_nmos", 1],
        ["sg13_lv_pmos", 1],
    ],
    [
        ["ntap1", 1],
        ["ptap1", 1],
        ["sg13_lv_nmos", 1],
        ["sg13_lv_pmos", 1],
    ],
]

LIMITATIONS = [
    {
        "id": "klayout-transitive-rules",
        "impact": (
            "The executable Ruby deck may read transitive inputs; the reviewed main "
            "deck, declared provenance inputs, and optional waiver database boundary "
            "are hashed, but undeclared transitive reads are not enumerated."
        ),
    },
    {
        "id": "bounded-klayout-transcript",
        "impact": "KLayout stdout and stderr are retained as bounded tails, not unbounded logs.",
    },
    {
        "id": "netgen-transitive-setup",
        "impact": (
            "The executable Netgen setup Tcl may read transitive files or ambient "
            "environment state that the operation cannot infer."
        ),
    },
    {
        "id": "bounded-netgen-transcript",
        "impact": (
            "The Netgen decision requires complete streams within the capture bound; "
            "the retained transcript is not an unbounded native log."
        ),
    },
    {
        "id": "pre-extracted-layout-netlist",
        "impact": (
            "LVS compares the pinned pre-extracted layout netlist with the pinned "
            "schematic netlist; this chain does not extract an LVS netlist from GDS."
        ),
    },
    {
        "id": "reference-flow-not-signoff",
        "impact": (
            "This open-source reference flow supports an engineering proceed/block "
            "decision; it is not foundry tapeout signoff."
        ),
    },
    {
        "id": "separate-negative-fixture",
        "impact": (
            "The gallery DRC failure is a separate real design fixture and does not "
            "change the clean inverter's proceed decision."
        ),
    },
    {
        "id": "checkout-state-recorded",
        "impact": (
            "The replay records the OpenADA checkout before and after execution; a "
            "dirty or changing checkout makes publication provisional until a source freeze."
        ),
    },
]


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _require_regular_file(path: Path, *, label: str, maximum_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum_bytes:
        raise ConformanceError(
            f"{label} size {metadata.st_size} is outside 1..{maximum_bytes} bytes"
        )
    return metadata.st_size


def _read_document(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular_file(path, label=label, maximum_bytes=MAX_JSON_BYTES)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConformanceError(f"{label} root must be an object")
    return payload


def _encode_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, document: object) -> None:
    encoded = _encode_json(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise ConformanceError(f"temporary publication path already exists: {temporary}")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ConformanceError(f"cannot publish JSON document {path}: {exc}") from exc


def _expect_keys(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConformanceError(f"{location} must be an object")
    if set(value) != expected:
        raise ConformanceError(
            f"{location} keys differ; expected={sorted(expected)!r}, got={sorted(value)!r}"
        )
    return value


def _expect_equal(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_record(
    path: Path,
    *,
    filename: str,
    kind: str,
    role: str,
) -> dict[str, Any]:
    size = _require_regular_file(path, label=kind, maximum_bytes=128 * 1024 * 1024)
    return {
        "repository_path": f"{PUBLICATION_PREFIX}/{filename}",
        "filename": filename,
        "path": f"/evidence/{filename}",
        "kind": kind,
        "role": role,
        "bytes": size,
        "sha256": sha256_file(path),
    }


def _result_artifacts(
    evidence: Path,
    operation: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    result_filename = operation["result_filename"]
    records = [
        _file_record(
            evidence / result_filename,
            filename=result_filename,
            kind="openada-result",
            role="normalized-evidence",
        )
    ]
    filenames = {
        record["path"]: record["filename"]
        for key in ("artifact", "json_artifact", "transcript_artifact")
        if (record := operation.get(key)) is not None
    }
    for artifact in result["artifacts"]:
        filename = filenames[artifact["path"]]
        records.append(
            _file_record(
                evidence / filename,
                filename=filename,
                kind=artifact["kind"],
                role="native-evidence",
            )
        )
    return records


def _drc_evidence(result: dict[str, Any]) -> dict[str, Any]:
    report = result["data"]["report"]
    return {
        "drc_clean": result["data"]["drc_clean"],
        "top_cell": result["data"]["top_cell"],
        "category_count": report["category_count"],
        "cell_count": report["cell_count"],
        "item_count": report["item_count"],
        "total_violations": report["total_violations"],
        "waived_violations": report["waived_violations"],
        "category_counts": copy.deepcopy(report["category_counts"]),
        "category_counts_truncated": report["category_counts_truncated"],
        "violations": copy.deepcopy(report["violations"]),
        "violations_truncated": report["violations_truncated"],
        "normalization": copy.deepcopy(report["normalization"]),
    }


def _lvs_evidence(result: dict[str, Any]) -> dict[str, Any]:
    comparison = result["data"]["comparison"]
    report = comparison["report"]
    return {
        "outcome": comparison["outcome"],
        "lvs_match": comparison["lvs_match"],
        "mismatch_count": comparison["mismatch_count"],
        "comparison_count": comparison["comparison_count"],
        "top_cell": comparison["top_cell"],
        "top_comparison_count": comparison["top_comparison_count"],
        "device_counts": copy.deepcopy(comparison["device_counts"]),
        "node_counts": copy.deepcopy(comparison["node_counts"]),
        "pin_counts": copy.deepcopy(comparison["pin_counts"]),
        "report_outcome": comparison["report_outcome"],
        "json_outcome": comparison["json_outcome"],
        "outcomes_agree": comparison["outcomes_agree"],
        "structural_counts_agree": comparison["structural_counts_agree"],
        "evidence_agrees": comparison["evidence_agrees"],
        "report": {
            "outcome": report["outcome"],
            "final_match": report["final_match"],
            "unique_match_markers": report["unique_match_markers"],
            "terminal_outcome": report["terminal_outcome"],
            "terminal_style": report["terminal_style"],
            "terminal_conflict": report["terminal_conflict"],
            "top_cell": report["top_cell"],
            "comparison_count": report["comparison_count"],
            "top_comparison_count": report["top_comparison_count"],
            "structure_complete": report["structure_complete"],
            "pin_lists_equivalent": report["pin_lists_equivalent"],
            "device_counts": copy.deepcopy(report["device_counts"]),
            "node_counts": copy.deepcopy(report["node_counts"]),
            "mismatch_count": report["mismatch_count"],
        },
    }


def _operation_record(
    evidence: Path,
    manifest: dict[str, Any],
    operation_name: str,
    *,
    surface_id: str,
    assertion: str,
) -> dict[str, Any]:
    operation = manifest["operations"][operation_name]
    result = _read_document(
        evidence / operation["result_filename"],
        label=f"{operation_name} normalized result",
    )
    source_inputs = [
        {key: record[key] for key in ("path", "kind", "role", "sha256")}
        for record in operation["inputs"]
    ]
    return {
        "surface_id": surface_id,
        "assertion": assertion,
        "source_inputs": source_inputs,
        "execution": {
            "status": result["execution"]["status"],
            "exit_code": result["execution"]["exit_code"],
            "command": copy.deepcopy(result["execution"]["command"]),
        },
        "engineering_status": result["engineering"]["status"],
        "summary": result["engineering"]["summary"],
        "evidence": (
            _drc_evidence(result)
            if operation_name in {"drc", "drc_fail"}
            else _lvs_evidence(result)
        ),
        "retained_artifacts": _result_artifacts(evidence, operation, result),
    }


def _decisions(operations: dict[str, Any]) -> dict[str, Any]:
    clean = operations["drc_clean"]["evidence"]
    failing = operations["drc_fail"]["evidence"]
    lvs = operations["lvs_match"]["evidence"]
    return {
        "inverter": {
            "decision": "proceed",
            "scope": "continue the inverter workflow; this is not tapeout signoff",
            "basis": [
                {
                    "assertion": "drc-clean",
                    "outcome": clean["drc_clean"],
                    "unwaived_violations": (
                        clean["total_violations"] - clean["waived_violations"]
                    ),
                },
                {
                    "assertion": "lvs-match",
                    "outcome": lvs["lvs_match"],
                    "match": "unique" if lvs["report"]["final_match"] else "not-unique",
                    "mismatch_count": lvs["mismatch_count"],
                },
            ],
            "next_action": "advance to the next engineering workflow stage with limitations attached",
            "evidence_paths": [
                "/operations/drc_clean/evidence",
                "/operations/lvs_match/evidence",
            ],
        },
        "gallery": {
            "decision": "block",
            "scope": "do not treat the gallery fixture as DRC-clean",
            "basis": [
                {
                    "assertion": "drc-clean",
                    "outcome": failing["drc_clean"],
                    "unwaived_violations": (
                        failing["total_violations"] - failing["waived_violations"]
                    ),
                    "classes": copy.deepcopy(failing["category_counts"]),
                }
            ],
            "next_action": "inspect and remediate the six M1.b, one Cnt.d, and one Cnt.e geometries",
            "evidence_paths": ["/operations/drc_fail/evidence/violations"],
        },
    }


def _probe_verdict(
    evidence: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    *,
    required_diagnostic: str,
) -> str:
    try:
        independent_verifier.verify_evidence(
            manifest,
            evidence,
            manifest_sha256=manifest_sha256,
        )
    except ConformanceError as exc:
        diagnostic = str(exc)
        if required_diagnostic not in diagnostic:
            raise ConformanceError(
                f"probe rejected for the wrong reason; required {required_diagnostic!r}, "
                f"got {diagnostic!r}"
            ) from exc
        return diagnostic
    raise ConformanceError(
        f"probe unexpectedly passed; required rejection containing {required_diagnostic!r}"
    )


def _copy_evidence(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, copy_function=shutil.copyfile)


def _replace_native_artifact_record(
    result: dict[str, Any],
    *,
    kind: str,
    content: bytes,
) -> None:
    record = next(item for item in result["artifacts"] if item["kind"] == kind)
    record["bytes"] = len(content)
    record["sha256"] = _sha256_bytes(content)


def run_probes(
    evidence: Path,
    manifest: dict[str, Any],
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    independent_verifier.verify_evidence(
        manifest,
        evidence,
        manifest_sha256=manifest_sha256,
    )
    fail_operation = manifest["operations"]["drc_fail"]
    fail_result_path = evidence / fail_operation["result_filename"]
    fail_result = _read_document(fail_result_path, label="real gallery DRC result")
    fail_artifact = evidence / fail_operation["artifact"]["filename"]
    negative_replays: list[dict[str, Any]] = [
        {
            "id": "real-gallery-drc-fail",
            "covers": DRC_ROWS,
            "fixture": "public-design-native-run",
            "expected_status": "fail",
            "observed_status": fail_result["engineering"]["status"],
            "verdict": "accepted-negative-outcome",
            "required_diagnostic": "KLayout reported 8 DRC violation(s).",
            "observed_diagnostic": fail_result["engineering"]["summary"],
            "mutation": None,
            "evidence": [
                {
                    "filename": fail_operation["result_filename"],
                    "bytes": fail_result_path.stat().st_size,
                    "sha256": sha256_file(fail_result_path),
                },
                {
                    "filename": fail_operation["artifact"]["filename"],
                    "bytes": fail_artifact.stat().st_size,
                    "sha256": sha256_file(fail_artifact),
                },
            ],
        }
    ]
    tamper_replays: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="openada-ihp-lvs-negative-") as temporary:
        probe = Path(temporary) / "evidence"
        _copy_evidence(evidence, probe)
        operation = manifest["operations"]["lvs"]
        content = b"Final result: Circuits do not match.\n"
        (probe / operation["artifact"]["filename"]).write_bytes(content)
        result_path = probe / operation["result_filename"]
        result = _read_document(result_path, label="LVS mismatch probe result")
        _replace_native_artifact_record(result, kind="netgen-comparison", content=content)
        result_path.write_bytes(_encode_json(result))
        required = "native LVS report contains mismatch evidence"
        diagnostic = _probe_verdict(
            probe,
            manifest,
            manifest_sha256,
            required_diagnostic=required,
        )
        negative_replays.append(
            {
                "id": "synthetic-native-lvs-mismatch",
                "covers": LVS_ROWS,
                "fixture": "synthetic-mismatch-injection-into-real-replay",
                "expected_status": "fail",
                "observed_status": "rejected",
                "verdict": "expected-rejection",
                "required_diagnostic": required,
                "observed_diagnostic": diagnostic,
                "mutation": {
                    "target": operation["artifact"]["filename"],
                    "method": "replace with an explicit native mismatch terminal",
                    "bytes": len(content),
                    "sha256": _sha256_bytes(content),
                },
                "evidence": [],
            }
        )

    with tempfile.TemporaryDirectory(prefix="openada-ihp-drc-tamper-") as temporary:
        probe = Path(temporary) / "evidence"
        _copy_evidence(evidence, probe)
        operation = manifest["operations"]["drc_fail"]
        artifact_path = probe / operation["artifact"]["filename"]
        artifact = artifact_path.read_bytes()
        last_start = artifact.rfind(b"<item>")
        last_end = artifact.find(b"</item>", last_start)
        if last_start < 0 or last_end < 0:
            raise ConformanceError("DRC tamper probe cannot locate the eighth native item")
        last_end += len(b"</item>")
        artifact = artifact[:last_start] + artifact[last_end:]
        artifact_path.write_bytes(artifact)
        result_path = probe / operation["result_filename"]
        result = _read_document(result_path, label="reconciled DRC tamper result")
        _replace_native_artifact_record(result, kind="klayout-lyrdb", content=artifact)
        validation = {"valid": True, "reason": "lyrdb.valid", "bytes": len(artifact)}
        result["data"]["report_output"]["capture"].update(
            {
                "bytes": len(artifact),
                "sha256": _sha256_bytes(artifact),
                "validation": validation,
            }
        )
        report = result["data"]["report"]
        report.update(
            {
                "validation": validation,
                "item_count": 7,
                "total_violations": 7,
                "category_counts": [
                    {"category": "M1.b", "category_path": ["M1.b"], "violations": 5},
                    {"category": "Cnt.d", "category_path": ["Cnt.d"], "violations": 1},
                    {"category": "Cnt.e", "category_path": ["Cnt.e"], "violations": 1},
                ],
                "violations": report["violations"][:-1],
                "normalization": {
                    "geometry_values": 7,
                    "retained_geometries": 7,
                    "retained_coordinate_pairs": 28,
                    "global_geometry_limit_reached": False,
                },
            }
        )
        result_path.write_bytes(_encode_json(result))
        required = "native DRC item count"
        diagnostic = _probe_verdict(
            probe,
            manifest,
            manifest_sha256,
            required_diagnostic=required,
        )
        tamper_replays.append(
            {
                "id": "reconciled-seven-item-drc",
                "covers": DRC_ROWS,
                "fixture": "tampered-copy-of-real-replay",
                "expected_status": "unknown",
                "observed_status": "rejected",
                "verdict": "expected-rejection",
                "required_diagnostic": required,
                "observed_diagnostic": diagnostic,
                "mutation": {
                    "target": operation["artifact"]["filename"],
                    "method": "remove one item and reconcile the normalized count and digest",
                    "bytes": len(artifact),
                    "sha256": _sha256_bytes(artifact),
                },
            }
        )

    with tempfile.TemporaryDirectory(prefix="openada-ihp-lvs-tamper-") as temporary:
        probe = Path(temporary) / "evidence"
        _copy_evidence(evidence, probe)
        operation = manifest["operations"]["lvs"]
        artifact_path = probe / operation["json_artifact"]["filename"]
        artifact = artifact_path.read_bytes() + b" "
        artifact_path.write_bytes(artifact)
        result_path = probe / operation["result_filename"]
        result = _read_document(result_path, label="unbound LVS JSON tamper result")
        record = next(
            item
            for item in result["artifacts"]
            if item["kind"] == "netgen-comparison-json"
        )
        record["bytes"] = len(artifact)
        result_path.write_bytes(_encode_json(result))
        required = "lvs.netgen-comparison-json.sha256"
        diagnostic = _probe_verdict(
            probe,
            manifest,
            manifest_sha256,
            required_diagnostic=required,
        )
        tamper_replays.append(
            {
                "id": "unbound-native-lvs-json",
                "covers": LVS_ROWS,
                "fixture": "tampered-copy-of-real-replay",
                "expected_status": "unknown",
                "observed_status": "rejected",
                "verdict": "expected-rejection",
                "required_diagnostic": required,
                "observed_diagnostic": diagnostic,
                "mutation": {
                    "target": operation["json_artifact"]["filename"],
                    "method": "append one unbound byte without changing the normalized artifact digest",
                    "bytes": len(artifact),
                    "sha256": _sha256_bytes(artifact),
                },
            }
        )

    run_path = evidence / "run.json"
    return {
        "schema": PROBE_SCHEMA,
        "chain_id": CHAIN_ID,
        "source_evidence": {
            "conformance_manifest_sha256": manifest_sha256,
            "run": {
                "filename": "run.json",
                "bytes": run_path.stat().st_size,
                "sha256": sha256_file(run_path),
            },
        },
        "negative_replays": negative_replays,
        "tamper_replays": tamper_replays,
        "summary": {
            "status": "pass",
            "probe_count": len(negative_replays) + len(tamper_replays),
            "all_required_diagnostics_observed": True,
        },
        "extensions": {},
    }


def _support_ref(repository_path: str, document: object) -> dict[str, Any]:
    encoded = _encode_json(document)
    return {
        "repository_path": repository_path,
        "bytes": len(encoded),
        "sha256": _sha256_bytes(encoded),
    }


def build_replay_documents(probes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for replay_type, records in (
        ("negative-replay", probes["negative_replays"]),
        ("tamper-replay", probes["tamper_replays"]),
    ):
        for record in records:
            replay_id = record["id"]
            documents[replay_id] = {
                "schema": REPLAY_VERDICT_SCHEMA,
                "chain_id": CHAIN_ID,
                "replay_type": replay_type,
                "source_evidence": copy.deepcopy(probes["source_evidence"]),
                "replay": copy.deepcopy(record),
                "extensions": {},
            }
    return documents


def _replay_refs(probes: dict[str, Any]) -> list[dict[str, Any]]:
    documents = build_replay_documents(probes)
    references = []
    for replay_id in sorted(documents):
        document = documents[replay_id]
        reference = _support_ref(
            f"conformance/ihp-inverter/semantic-replays/{replay_id}.json",
            document,
        )
        references.append(
            {
                "replay_id": replay_id,
                "replay_type": document["replay_type"],
                **reference,
            }
        )
    return references


def build_supporting_documents(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized = {
        "schema": NORMALIZED_SCHEMA,
        "chain_id": CHAIN_ID,
        "source": copy.deepcopy(snapshot["source"]),
        "runtime": copy.deepcopy(snapshot["runtime"]),
        "replay": copy.deepcopy(snapshot["replay"]),
        "operations": copy.deepcopy(snapshot["operations"]),
        "replays": copy.deepcopy(snapshot["replays"]),
        "limitations": copy.deepcopy(snapshot["limitations"]),
        "standards": copy.deepcopy(snapshot["standards"]),
        "extensions": {},
    }
    native_artifacts = [snapshot["replay"]["retained_run_artifact"]]
    for operation in snapshot["operations"].values():
        native_artifacts.extend(operation["retained_artifacts"])
    oracle = {
        "schema": ORACLE_SCHEMA,
        "chain_id": CHAIN_ID,
        "source": copy.deepcopy(snapshot["source"]),
        "status": snapshot["independent_oracle"]["status"],
        "implementation": copy.deepcopy(
            snapshot["independent_oracle"]["implementation"]
        ),
        "claim": snapshot["independent_oracle"]["claim"],
        "verified_artifacts": copy.deepcopy(native_artifacts),
        "replay_summary": copy.deepcopy(snapshot["replays"]["summary"]),
        "extensions": {},
    }
    decision = {
        "schema": DECISION_SCHEMA,
        "chain_id": CHAIN_ID,
        "inputs": {
            "normalized_evidence": _support_ref(
                "conformance/ihp-inverter/semantic-normalized.json",
                normalized,
            ),
            "independent_oracle": _support_ref(
                "conformance/ihp-inverter/semantic-oracle.json",
                oracle,
            ),
        },
        "decisions": copy.deepcopy(snapshot["decisions"]),
        "limitations": copy.deepcopy(snapshot["limitations"]),
        "standards": copy.deepcopy(snapshot["standards"]),
        "extensions": {},
    }
    return {
        "normalized_evidence": normalized,
        "independent_oracle": oracle,
        "downstream_decision": decision,
    }


def _trust_chain_refs(snapshot: dict[str, Any]) -> dict[str, Any]:
    documents = build_supporting_documents(snapshot)
    return {
        "normalized_evidence": _support_ref(
            "conformance/ihp-inverter/semantic-normalized.json",
            documents["normalized_evidence"],
        ),
        "independent_oracle": _support_ref(
            "conformance/ihp-inverter/semantic-oracle.json",
            documents["independent_oracle"],
        ),
        "downstream_decision": _support_ref(
            "conformance/ihp-inverter/semantic-decision.json",
            documents["downstream_decision"],
        ),
    }


def build_snapshot(
    evidence: Path,
    manifest: dict[str, Any],
    probes: dict[str, Any],
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    independent_verifier.verify_evidence(
        manifest,
        evidence,
        manifest_sha256=manifest_sha256,
    )
    run = _read_document(evidence / "run.json", label="conformance run")
    if run["image"]["id"] != IMAGE_CONFIG_DIGEST:
        raise ConformanceError(
            "run.image.id differs from the reviewed image config digest: "
            f"{run['image']['id']!r}"
        )
    operations = {
        "drc_clean": _operation_record(
            evidence,
            manifest,
            "drc",
            surface_id="openada.surface/cli.drc/v1",
            assertion="drc-clean",
        ),
        "drc_fail": _operation_record(
            evidence,
            manifest,
            "drc_fail",
            surface_id="openada.surface/cli.drc/v1",
            assertion="drc-clean",
        ),
        "lvs_match": _operation_record(
            evidence,
            manifest,
            "lvs",
            surface_id="openada.surface/cli.lvs/v1",
            assertion="lvs-match",
        ),
    }
    verify_path = HERE / "verify.py"
    probe_bytes = _encode_json(probes)
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "chain_id": CHAIN_ID,
        "source": {
            "conformance_id": manifest["id"],
            "conformance_manifest_sha256": manifest_sha256,
            "repository": manifest["design"]["repository"],
            "revision": manifest["design"]["revision"],
            "license": copy.deepcopy(manifest["design"]["license"]),
        },
        "runtime": {
            "image_reference": manifest["runtime"]["image"]["reference"],
            "image_config_digest": run["image"]["id"],
            "platform": manifest["runtime"]["image"]["platform"],
            "pdk": copy.deepcopy(manifest["runtime"]["pdk"]),
            "tools": [
                {
                    "id": "klayout",
                    **copy.deepcopy(manifest["operations"]["drc"]["tool_identity"]),
                },
                {
                    "id": "netgen",
                    **copy.deepcopy(manifest["operations"]["lvs"]["tool_identity"]),
                },
            ],
            "network": run["network"],
        },
        "replay": {
            "created_at": run["created_at"],
            "openada_checkout": copy.deepcopy(run["openada_checkout"]),
            "retained_run_artifact": _file_record(
                evidence / "run.json",
                filename="run.json",
                kind="conformance-run",
                role="native-run-metadata",
            ),
        },
        "independent_oracle": {
            "status": "pass",
            "implementation": {
                "repository_path": "conformance/ihp-inverter/verify.py",
                "bytes": verify_path.stat().st_size,
                "sha256": sha256_file(verify_path),
            },
            "claim": (
                "Every normalized result, retained native artifact, exact reviewed "
                "outcome, and replay metadata record passed independent verification."
            ),
        },
        "operations": operations,
        "decisions": _decisions(operations),
        "replays": {
            "report": {
                "repository_path": "conformance/ihp-inverter/semantic-probes.json",
                "bytes": len(probe_bytes),
                "sha256": _sha256_bytes(probe_bytes),
            },
            "negative_replays": copy.deepcopy(probes["negative_replays"]),
            "tamper_replays": copy.deepcopy(probes["tamper_replays"]),
            "verdict_artifacts": _replay_refs(probes),
            "summary": copy.deepcopy(probes["summary"]),
        },
        "limitations": copy.deepcopy(LIMITATIONS),
        "standards": {
            "ieee_measurement_standard": {
                "status": "not-applicable",
                "reason": (
                    "These assertions classify foundry-deck geometry and structural "
                    "netlist equivalence; they are not signal measurements such as SNR."
                ),
            },
            "governing_sources": [
                {
                    "kind": "ihp-foundry-drc-deck",
                    "path": manifest["operations"]["drc"]["inputs"][1]["path"],
                    "sha256": manifest["operations"]["drc"]["inputs"][1]["sha256"],
                },
                {
                    "kind": "ihp-netgen-setup",
                    "path": manifest["operations"]["lvs"]["inputs"][2]["path"],
                    "sha256": manifest["operations"]["lvs"]["inputs"][2]["sha256"],
                },
            ],
        },
        "extensions": {},
    }
    snapshot["trust_chain"] = _trust_chain_refs(snapshot)
    verify_snapshot(
        snapshot,
        manifest,
        probes,
        manifest_sha256=manifest_sha256,
        bundle_dir=None,
        verify_publication=False,
    )
    return snapshot


def _expected_violation(index: int) -> dict[str, Any]:
    category = EXPECTED_DRC_CATEGORIES[index]
    return {
        "category": category,
        "category_path": [category],
        "description": EXPECTED_DRC_DESCRIPTIONS[category],
        "cell": EXPECTED_DRC_CELLS[index],
        "multiplicity": 1,
        "waived": False,
        "tags": [],
        "geometries": [
            {
                "type": "edge-pair",
                "coordinates": EXPECTED_DRC_COORDINATES[index],
                "coordinates_truncated": False,
            }
        ],
        "geometries_truncated": False,
    }


def _verify_artifact_record(record: Any, *, location: str) -> None:
    record = _expect_keys(
        record,
        {"repository_path", "filename", "path", "kind", "role", "bytes", "sha256"},
        location,
    )
    filename = record["filename"]
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ConformanceError(f"{location}.filename must be a safe basename")
    _expect_equal(
        record["repository_path"],
        f"{PUBLICATION_PREFIX}/{filename}",
        f"{location}.repository_path",
    )
    _expect_equal(record["path"], f"/evidence/{filename}", f"{location}.path")
    if not isinstance(record["bytes"], int) or isinstance(record["bytes"], bool) or record["bytes"] < 1:
        raise ConformanceError(f"{location}.bytes must be a positive integer")
    if not isinstance(record["sha256"], str) or SHA256_RE.fullmatch(record["sha256"]) is None:
        raise ConformanceError(f"{location}.sha256 must be a lowercase SHA-256 digest")


def _verify_drc_operation(
    record: dict[str, Any],
    operation: dict[str, Any],
    *,
    failing: bool,
    location: str,
) -> None:
    _expect_keys(
        record,
        {
            "surface_id",
            "assertion",
            "source_inputs",
            "execution",
            "engineering_status",
            "summary",
            "evidence",
            "retained_artifacts",
        },
        location,
    )
    _expect_equal(record["surface_id"], "openada.surface/cli.drc/v1", f"{location}.surface_id")
    _expect_equal(record["assertion"], "drc-clean", f"{location}.assertion")
    expected_inputs = [
        {key: item[key] for key in ("path", "kind", "role", "sha256")}
        for item in operation["inputs"]
    ]
    _expect_equal(record["source_inputs"], expected_inputs, f"{location}.source_inputs")
    execution = _expect_keys(record["execution"], {"status", "exit_code", "command"}, f"{location}.execution")
    _expect_equal(execution["status"], "completed", f"{location}.execution.status")
    _expect_equal(execution["exit_code"], 0, f"{location}.execution.exit_code")
    arguments = operation["arguments"]
    expected_command = [
        operation["tool_identity"]["path"],
        "-b",
        "-r",
        arguments["rules"],
        "-rd",
        f"input={arguments['gds']}",
        "-rd",
        f"report={arguments['report']}",
        "-rd",
        f"topcell={arguments['top_cell']}",
    ]
    _expect_equal(execution["command"], expected_command, f"{location}.execution.command")
    expected_status = "fail" if failing else "pass"
    expected_count = 8 if failing else 0
    _expect_equal(record["engineering_status"], expected_status, f"{location}.engineering_status")
    expected_summary = (
        "KLayout reported 8 DRC violation(s)."
        if failing
        else "KLayout reported zero DRC violations."
    )
    _expect_equal(record["summary"], expected_summary, f"{location}.summary")
    evidence = _expect_keys(
        record["evidence"],
        {
            "drc_clean",
            "top_cell",
            "category_count",
            "cell_count",
            "item_count",
            "total_violations",
            "waived_violations",
            "category_counts",
            "category_counts_truncated",
            "violations",
            "violations_truncated",
            "normalization",
        },
        f"{location}.evidence",
    )
    _expect_equal(evidence["drc_clean"], not failing, f"{location}.evidence.drc_clean")
    _expect_equal(evidence["top_cell"], arguments["top_cell"], f"{location}.evidence.top_cell")
    if not isinstance(evidence["category_count"], int) or evidence["category_count"] < 3:
        raise ConformanceError(f"{location}.evidence.category_count must be at least three")
    _expect_equal(evidence["cell_count"], 2 if failing else 1, f"{location}.evidence.cell_count")
    _expect_equal(evidence["item_count"], expected_count, f"{location}.evidence.item_count")
    _expect_equal(evidence["total_violations"], expected_count, f"{location}.evidence.total_violations")
    _expect_equal(evidence["waived_violations"], 0, f"{location}.evidence.waived_violations")
    _expect_equal(evidence["category_counts_truncated"], False, f"{location}.evidence.category_counts_truncated")
    _expect_equal(evidence["violations_truncated"], False, f"{location}.evidence.violations_truncated")
    if failing:
        _expect_equal(evidence["category_counts"], EXPECTED_DRC_CATEGORY_COUNTS, f"{location}.evidence.category_counts")
        _expect_equal(
            evidence["violations"],
            [_expected_violation(index) for index in range(8)],
            f"{location}.evidence.violations",
        )
        expected_normalization = {
            "geometry_values": 8,
            "retained_geometries": 8,
            "retained_coordinate_pairs": 32,
            "global_geometry_limit_reached": False,
        }
    else:
        _expect_equal(evidence["category_counts"], [], f"{location}.evidence.category_counts")
        _expect_equal(evidence["violations"], [], f"{location}.evidence.violations")
        expected_normalization = {
            "geometry_values": 0,
            "retained_geometries": 0,
            "retained_coordinate_pairs": 0,
            "global_geometry_limit_reached": False,
        }
    _expect_equal(evidence["normalization"], expected_normalization, f"{location}.evidence.normalization")
    expected_kinds = ["openada-result", "klayout-lyrdb", "klayout-transcript"]
    _expect_equal(
        [item.get("kind") for item in record["retained_artifacts"]],
        expected_kinds,
        f"{location}.retained_artifacts.kinds",
    )
    for position, artifact in enumerate(record["retained_artifacts"]):
        _verify_artifact_record(artifact, location=f"{location}.retained_artifacts[{position}]")


def _verify_lvs_operation(
    record: dict[str, Any], operation: dict[str, Any], *, location: str
) -> None:
    _expect_keys(
        record,
        {
            "surface_id",
            "assertion",
            "source_inputs",
            "execution",
            "engineering_status",
            "summary",
            "evidence",
            "retained_artifacts",
        },
        location,
    )
    _expect_equal(record["surface_id"], "openada.surface/cli.lvs/v1", f"{location}.surface_id")
    _expect_equal(record["assertion"], "lvs-match", f"{location}.assertion")
    expected_inputs = [
        {key: item[key] for key in ("path", "kind", "role", "sha256")}
        for item in operation["inputs"]
    ]
    _expect_equal(record["source_inputs"], expected_inputs, f"{location}.source_inputs")
    execution = _expect_keys(record["execution"], {"status", "exit_code", "command"}, f"{location}.execution")
    _expect_equal(execution["status"], "completed", f"{location}.execution.status")
    _expect_equal(execution["exit_code"], 0, f"{location}.execution.exit_code")
    arguments = operation["arguments"]
    expected_command = [
        operation["tool_identity"]["path"],
        "-batch",
        "lvs",
        f"{arguments['layout_netlist']} {arguments['cell']}",
        f"{arguments['schematic_netlist']} {arguments['cell']}",
        arguments["setup"],
        arguments["report"],
        "-json",
    ]
    _expect_equal(execution["command"], expected_command, f"{location}.execution.command")
    _expect_equal(record["engineering_status"], "pass", f"{location}.engineering_status")
    _expect_equal(
        record["summary"],
        "Netgen produced clean, agreeing native evidence for a unique LVS match.",
        f"{location}.summary",
    )
    evidence = _expect_keys(
        record["evidence"],
        {
            "outcome",
            "lvs_match",
            "mismatch_count",
            "comparison_count",
            "top_cell",
            "top_comparison_count",
            "device_counts",
            "node_counts",
            "pin_counts",
            "report_outcome",
            "json_outcome",
            "outcomes_agree",
            "structural_counts_agree",
            "evidence_agrees",
            "report",
        },
        f"{location}.evidence",
    )
    expected = {
        "outcome": "pass",
        "lvs_match": True,
        "mismatch_count": 0,
        "comparison_count": 1,
        "top_cell": "inverter",
        "top_comparison_count": 1,
        "device_counts": EXPECTED_DEVICE_COUNTS,
        "node_counts": [6, 6],
        "pin_counts": [4, 4],
        "report_outcome": "pass",
        "json_outcome": "pass",
        "outcomes_agree": True,
        "structural_counts_agree": True,
        "evidence_agrees": True,
        "report": {
            "outcome": "pass",
            "final_match": True,
            "unique_match_markers": True,
            "terminal_outcome": "pass",
            "terminal_style": "final-result",
            "terminal_conflict": False,
            "top_cell": "inverter",
            "comparison_count": 1,
            "top_comparison_count": 1,
            "structure_complete": True,
            "pin_lists_equivalent": True,
            "device_counts": [4, 4],
            "node_counts": [6, 6],
            "mismatch_count": 0,
        },
    }
    _expect_equal(evidence, expected, f"{location}.evidence")
    expected_kinds = [
        "openada-result",
        "netgen-comparison",
        "netgen-comparison-json",
        "netgen-transcript",
    ]
    _expect_equal(
        [item.get("kind") for item in record["retained_artifacts"]],
        expected_kinds,
        f"{location}.retained_artifacts.kinds",
    )
    for position, artifact in enumerate(record["retained_artifacts"]):
        _verify_artifact_record(artifact, location=f"{location}.retained_artifacts[{position}]")


def verify_probe_report(
    probes: dict[str, Any], *, manifest_sha256: str, run_record: dict[str, Any]
) -> None:
    _expect_keys(
        probes,
        {
            "schema",
            "chain_id",
            "source_evidence",
            "negative_replays",
            "tamper_replays",
            "summary",
            "extensions",
        },
        "probe report",
    )
    _expect_equal(probes["schema"], PROBE_SCHEMA, "probe report.schema")
    _expect_equal(probes["chain_id"], CHAIN_ID, "probe report.chain_id")
    _expect_equal(probes["extensions"], {}, "probe report.extensions")
    source = _expect_keys(
        probes["source_evidence"],
        {"conformance_manifest_sha256", "run"},
        "probe report.source_evidence",
    )
    _expect_equal(
        source["conformance_manifest_sha256"],
        manifest_sha256,
        "probe report.source_evidence.conformance_manifest_sha256",
    )
    _expect_equal(source["run"], run_record, "probe report.source_evidence.run")
    expected_negative = {
        "real-gallery-drc-fail": (DRC_ROWS, "KLayout reported 8 DRC violation(s)."),
        "synthetic-native-lvs-mismatch": (LVS_ROWS, "native LVS report contains mismatch evidence"),
    }
    expected_tamper = {
        "reconciled-seven-item-drc": (DRC_ROWS, "native DRC item count"),
        "unbound-native-lvs-json": (LVS_ROWS, "lvs.netgen-comparison-json.sha256"),
    }
    for records, expected, label in (
        (probes["negative_replays"], expected_negative, "negative replay"),
        (probes["tamper_replays"], expected_tamper, "tamper replay"),
    ):
        if not isinstance(records, list):
            raise ConformanceError(f"probe report {label}s must be an array")
        mapped = {record.get("id"): record for record in records if isinstance(record, dict)}
        _expect_equal(set(mapped), set(expected), f"probe report {label} IDs")
        for replay_id, (covers, diagnostic) in expected.items():
            record = mapped[replay_id]
            _expect_equal(record.get("covers"), covers, f"probe report {replay_id}.covers")
            _expect_equal(
                record.get("required_diagnostic"),
                diagnostic,
                f"probe report {replay_id}.required_diagnostic",
            )
            observed = record.get("observed_diagnostic")
            if not isinstance(observed, str) or diagnostic not in observed:
                raise ConformanceError(
                    f"probe report {replay_id}.observed_diagnostic lacks {diagnostic!r}"
                )
            expected_verdict = (
                "accepted-negative-outcome"
                if replay_id == "real-gallery-drc-fail"
                else "expected-rejection"
            )
            _expect_equal(record.get("verdict"), expected_verdict, f"probe report {replay_id}.verdict")
    _expect_equal(
        probes["summary"],
        {
            "status": "pass",
            "probe_count": 4,
            "all_required_diagnostics_observed": True,
        },
        "probe report.summary",
    )


def _published_path(record: dict[str, Any], bundle_dir: Path | None) -> Path:
    if bundle_dir is not None:
        return bundle_dir / record["filename"]
    return REPOSITORY_ROOT / record["repository_path"]


def verify_snapshot(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    probes: dict[str, Any],
    *,
    manifest_sha256: str,
    bundle_dir: Path | None,
    verify_publication: bool = True,
) -> None:
    _expect_keys(
        snapshot,
        {
            "schema",
            "chain_id",
            "source",
            "runtime",
            "replay",
            "independent_oracle",
            "operations",
            "decisions",
            "replays",
            "trust_chain",
            "limitations",
            "standards",
            "extensions",
        },
        "semantic evidence",
    )
    _expect_equal(snapshot["schema"], SNAPSHOT_SCHEMA, "semantic evidence.schema")
    _expect_equal(snapshot["chain_id"], CHAIN_ID, "semantic evidence.chain_id")
    _expect_equal(snapshot["extensions"], {}, "semantic evidence.extensions")
    expected_source = {
        "conformance_id": manifest["id"],
        "conformance_manifest_sha256": manifest_sha256,
        "repository": manifest["design"]["repository"],
        "revision": manifest["design"]["revision"],
        "license": manifest["design"]["license"],
    }
    _expect_equal(snapshot["source"], expected_source, "semantic evidence.source")
    expected_runtime = {
        "image_reference": manifest["runtime"]["image"]["reference"],
        "image_config_digest": IMAGE_CONFIG_DIGEST,
        "platform": manifest["runtime"]["image"]["platform"],
        "pdk": manifest["runtime"]["pdk"],
        "tools": [
            {"id": "klayout", **manifest["operations"]["drc"]["tool_identity"]},
            {"id": "netgen", **manifest["operations"]["lvs"]["tool_identity"]},
        ],
        "network": "none during EDA execution",
    }
    _expect_equal(snapshot["runtime"], expected_runtime, "semantic evidence.runtime")
    replay = _expect_keys(
        snapshot["replay"],
        {"created_at", "openada_checkout", "retained_run_artifact"},
        "semantic evidence.replay",
    )
    if not isinstance(replay["created_at"], str) or UTC_RE.fullmatch(replay["created_at"]) is None:
        raise ConformanceError("semantic evidence.replay.created_at must be UTC")
    run_artifact = replay["retained_run_artifact"]
    _verify_artifact_record(run_artifact, location="semantic evidence.replay.retained_run_artifact")
    _expect_equal(run_artifact["filename"], "run.json", "semantic evidence.replay.retained_run_artifact.filename")
    checkout = replay["openada_checkout"]
    _expect_keys(checkout, {"before", "after", "state_unchanged", "commit_exact"}, "semantic evidence.replay.openada_checkout")
    oracle = _expect_keys(
        snapshot["independent_oracle"],
        {"status", "implementation", "claim"},
        "semantic evidence.independent_oracle",
    )
    _expect_equal(oracle["status"], "pass", "semantic evidence.independent_oracle.status")
    verify_path = HERE / "verify.py"
    _expect_equal(
        oracle["implementation"],
        {
            "repository_path": "conformance/ihp-inverter/verify.py",
            "bytes": verify_path.stat().st_size,
            "sha256": sha256_file(verify_path),
        },
        "semantic evidence.independent_oracle.implementation",
    )
    operations = _expect_keys(
        snapshot["operations"],
        {"drc_clean", "drc_fail", "lvs_match"},
        "semantic evidence.operations",
    )
    _verify_drc_operation(
        operations["drc_clean"],
        manifest["operations"]["drc"],
        failing=False,
        location="semantic evidence.operations.drc_clean",
    )
    _verify_drc_operation(
        operations["drc_fail"],
        manifest["operations"]["drc_fail"],
        failing=True,
        location="semantic evidence.operations.drc_fail",
    )
    _verify_lvs_operation(
        operations["lvs_match"],
        manifest["operations"]["lvs"],
        location="semantic evidence.operations.lvs_match",
    )
    _expect_equal(snapshot["decisions"], _decisions(operations), "semantic evidence.decisions")
    _expect_equal(snapshot["limitations"], LIMITATIONS, "semantic evidence.limitations")
    expected_standards = {
        "ieee_measurement_standard": {
            "status": "not-applicable",
            "reason": (
                "These assertions classify foundry-deck geometry and structural "
                "netlist equivalence; they are not signal measurements such as SNR."
            ),
        },
        "governing_sources": [
            {
                "kind": "ihp-foundry-drc-deck",
                "path": manifest["operations"]["drc"]["inputs"][1]["path"],
                "sha256": manifest["operations"]["drc"]["inputs"][1]["sha256"],
            },
            {
                "kind": "ihp-netgen-setup",
                "path": manifest["operations"]["lvs"]["inputs"][2]["path"],
                "sha256": manifest["operations"]["lvs"]["inputs"][2]["sha256"],
            },
        ],
    }
    _expect_equal(snapshot["standards"], expected_standards, "semantic evidence.standards")
    probe_bytes = _encode_json(probes)
    expected_replays = {
        "report": {
            "repository_path": "conformance/ihp-inverter/semantic-probes.json",
            "bytes": len(probe_bytes),
            "sha256": _sha256_bytes(probe_bytes),
        },
        "negative_replays": probes["negative_replays"],
        "tamper_replays": probes["tamper_replays"],
        "verdict_artifacts": _replay_refs(probes),
        "summary": probes["summary"],
    }
    _expect_equal(snapshot["replays"], expected_replays, "semantic evidence.replays")
    _expect_equal(
        snapshot["trust_chain"],
        _trust_chain_refs(snapshot),
        "semantic evidence.trust_chain",
    )
    expected_run_record = {
        "filename": "run.json",
        "bytes": run_artifact["bytes"],
        "sha256": run_artifact["sha256"],
    }
    verify_probe_report(
        probes,
        manifest_sha256=manifest_sha256,
        run_record=expected_run_record,
    )
    if verify_publication:
        artifacts = [run_artifact]
        for operation_record in operations.values():
            artifacts.extend(operation_record["retained_artifacts"])
        paths = [record["repository_path"] for record in artifacts]
        if len(paths) != len(set(paths)):
            raise ConformanceError("semantic evidence repeats a retained artifact path")
        for position, record in enumerate(artifacts):
            path = _published_path(record, bundle_dir)
            size = _require_regular_file(
                path,
                label=f"published semantic artifact {position}",
                maximum_bytes=128 * 1024 * 1024,
            )
            _expect_equal(size, record["bytes"], f"published semantic artifact {position}.bytes")
            _expect_equal(
                sha256_file(path),
                record["sha256"],
                f"published semantic artifact {position}.sha256",
            )


def publish_bundle(evidence: Path, destination: Path) -> None:
    if destination.exists() and (
        destination.is_symlink() or not destination.is_dir()
    ):
        raise ConformanceError(f"publication bundle is not a real directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.next-", dir=destination.parent)
    )
    try:
        for source in evidence.iterdir():
            _require_regular_file(
                source,
                label="source evidence artifact",
                maximum_bytes=128 * 1024 * 1024,
            )
            shutil.copy2(source, staging / source.name)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except OSError as exc:
        if staging.exists():
            shutil.rmtree(staging)
        raise ConformanceError(f"cannot publish evidence bundle: {exc}") from exc


def publish_supporting_documents(
    snapshot: dict[str, Any],
    probes: dict[str, Any],
    *,
    normalized_path: Path,
    oracle_path: Path,
    decision_path: Path,
    replay_dir: Path,
) -> None:
    supporting = build_supporting_documents(snapshot)
    _write_json(normalized_path, supporting["normalized_evidence"])
    _write_json(oracle_path, supporting["independent_oracle"])
    _write_json(decision_path, supporting["downstream_decision"])
    replay_dir.mkdir(parents=True, exist_ok=True)
    replay_documents = build_replay_documents(probes)
    expected_names = {f"{replay_id}.json" for replay_id in replay_documents}
    actual_names = {entry.name for entry in replay_dir.iterdir()}
    if actual_names and actual_names != expected_names:
        raise ConformanceError(
            "existing replay verdict directory contents differ; "
            f"expected={sorted(expected_names)!r}, got={sorted(actual_names)!r}"
        )
    for replay_id, document in replay_documents.items():
        _write_json(replay_dir / f"{replay_id}.json", document)


def verify_supporting_documents(
    snapshot: dict[str, Any],
    probes: dict[str, Any],
    *,
    normalized_path: Path,
    oracle_path: Path,
    decision_path: Path,
    replay_dir: Path,
) -> None:
    expected = build_supporting_documents(snapshot)
    for label, path, key in (
        ("normalized semantic evidence", normalized_path, "normalized_evidence"),
        ("independent oracle verdict", oracle_path, "independent_oracle"),
        ("downstream decision verdict", decision_path, "downstream_decision"),
    ):
        actual = _read_document(path, label=label)
        _expect_equal(actual, expected[key], label)
        reference = snapshot["trust_chain"][key]
        _expect_equal(path.stat().st_size, reference["bytes"], f"{label}.bytes")
        _expect_equal(sha256_file(path), reference["sha256"], f"{label}.sha256")
    replay_documents = build_replay_documents(probes)
    expected_names = {f"{replay_id}.json" for replay_id in replay_documents}
    try:
        actual_names = {entry.name for entry in replay_dir.iterdir()}
    except OSError as exc:
        raise ConformanceError(f"cannot read replay verdict directory {replay_dir}: {exc}") from exc
    _expect_equal(actual_names, expected_names, "replay verdict filenames")
    references = {
        record["replay_id"]: record
        for record in snapshot["replays"]["verdict_artifacts"]
    }
    for replay_id, expected_document in replay_documents.items():
        path = replay_dir / f"{replay_id}.json"
        actual = _read_document(path, label=f"replay verdict {replay_id}")
        _expect_equal(actual, expected_document, f"replay verdict {replay_id}")
        reference = references[replay_id]
        _expect_equal(path.stat().st_size, reference["bytes"], f"replay verdict {replay_id}.bytes")
        _expect_equal(sha256_file(path), reference["sha256"], f"replay verdict {replay_id}.sha256")


def _run_contract_tests() -> dict[str, Any]:
    suite = REPOSITORY_ROOT / "tests/test_conformance.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_conformance.py",
            "-k",
            "not semantic_",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ConformanceError(
            "physical contract tests failed: " + completed.stdout[-4000:]
        )
    passed_matches = re.findall(r"(?:^|\s)([0-9]+) passed", completed.stdout)
    skipped_matches = re.findall(r"(?:^|\s)([0-9]+) skipped", completed.stdout)
    if len(passed_matches) != 1 or len(skipped_matches) > 1:
        raise ConformanceError("cannot parse the focused physical contract-test summary")
    passed = int(passed_matches[0])
    skipped = int(skipped_matches[0]) if skipped_matches else 0
    return {
        "schema": "openada.semantic-contract-verdict/v0alpha1",
        "chain_id": CHAIN_ID,
        "status": "pass",
        "suite": {
            "repository_path": "tests/test_conformance.py",
            "sha256": sha256_file(suite),
            "selection": "not semantic_",
            "passed": passed,
            "skipped": skipped,
            "failed": 0,
        },
        "implementations": [
            {
                "repository_path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                "sha256": sha256_file(path),
            }
            for path in (HERE / "semantic.py", HERE / "verify.py")
        ],
        "native_replay": {
            "repository_path": "conformance/ihp-inverter/semantic-artifacts/run.json",
            "sha256": sha256_file(DEFAULT_BUNDLE / "run.json"),
            "independent_verification": "pass",
        },
        "extensions": {},
    }


def _chain_artifact(
    path: Path,
    role: str,
    source_step: str | None,
    source_output: str | None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    size = _require_regular_file(
        path, label="semantic chain artifact", maximum_bytes=128 * 1024 * 1024
    )
    return {
        "repository_path": path.relative_to(REPOSITORY_ROOT).as_posix(),
        "bytes": size,
        "sha256": sha256_file(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": replay_id,
    }


def _chain_artifact_definitions() -> list[tuple[Path, str, str | None, str | None, str | None]]:
    return [
        (HERE / "semantic-contract.json", "contract-test", "contract-tests", "contract-test-verdict", None),
        (DEFAULT_BUNDLE / "design-provenance.json", "design-provenance", "materialize-pinned-sources", "design-provenance", None),
        (DEFAULT_BUNDLE / "run.json", "native-artifact", "drc-clean", "native-run-receipt", None),
        (DEFAULT_BUNDLE / "drc.json", "native-artifact", "drc-clean", "clean-drc-result", None),
        (DEFAULT_BUNDLE / "inverter.drc.lyrdb", "native-artifact", "drc-clean", "clean-drc-lyrdb", None),
        (DEFAULT_BUNDLE / "inverter.drc.lyrdb.openada.log", "native-artifact", "drc-clean", "clean-drc-transcript", None),
        (DEFAULT_BUNDLE / "drc-fail.json", "native-artifact", "drc-fail", "failing-drc-result", None),
        (DEFAULT_BUNDLE / "lvs-tester.drc.lyrdb", "native-artifact", "drc-fail", "failing-drc-lyrdb", None),
        (DEFAULT_BUNDLE / "lvs-tester.drc.lyrdb.openada.log", "native-artifact", "drc-fail", "failing-drc-transcript", None),
        (DEFAULT_BUNDLE / "lvs.json", "native-artifact", "lvs-match", "lvs-result", None),
        (DEFAULT_BUNDLE / "inverter.lvs.comp", "native-artifact", "lvs-match", "lvs-comparison-report", None),
        (DEFAULT_BUNDLE / "inverter.lvs.json", "native-artifact", "lvs-match", "lvs-native-json", None),
        (DEFAULT_BUNDLE / "inverter.lvs.comp.openada.log", "native-artifact", "lvs-match", "lvs-transcript", None),
        (DEFAULT_ORACLE, "independent-oracle", "independent-verifier", "independent-oracle-verdict", None),
        (DEFAULT_NORMALIZED, "normalized-evidence", "normalize-physical-evidence", "normalized-physical-evidence", None),
        (DEFAULT_DECISION, "downstream-decision", "engineering-decision", "downstream-physical-decision", None),
        (DEFAULT_SNAPSHOT, "agent-visible-evidence", "agent-evidence", "agent-visible-physical-evidence", None),
        (DEFAULT_REPLAY_DIR / "real-gallery-drc-fail.json", "negative-replay", None, None, "real-gallery-drc-fail"),
        (DEFAULT_REPLAY_DIR / "synthetic-native-lvs-mismatch.json", "negative-replay", None, None, "synthetic-native-lvs-mismatch"),
        (DEFAULT_REPLAY_DIR / "reconciled-seven-item-drc.json", "tamper-replay", None, None, "reconciled-seven-item-drc"),
        (DEFAULT_REPLAY_DIR / "unbound-native-lvs-json.json", "tamper-replay", None, None, "unbound-native-lvs-json"),
    ]


def _write_chain_run(source_receipt: dict[str, Any]) -> None:
    chain = _read_document(CHAIN_MANIFEST_PATH, label="semantic chain manifest")
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": chain["id"],
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
            "org.openada.physical": {
                "receipt_status": source_receipt["receipt_class"],
                "decision_scope": "engineering proceed/block; not foundry signoff",
            }
        },
    }
    _write_json(CHAIN_RUN_PATH, document)


def _verify_chain_run() -> None:
    chain = _read_document(CHAIN_MANIFEST_PATH, label="semantic chain manifest")
    run = _read_document(CHAIN_RUN_PATH, label="semantic chain run")
    for document, schema_path, label in (
        (chain, REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json", "semantic chain manifest"),
        (run, REPOSITORY_ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json", "semantic chain run"),
    ):
        schema = _read_document(schema_path, label=f"{label} schema")
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ConformanceError(f"{label} violates its schema: {errors[0].message}")
    _expect_equal(run["chain_id"], chain["id"], "chain run ID")
    _expect_equal(run["chain_manifest_sha256"], sha256_file(CHAIN_MANIFEST_PATH), "chain run manifest digest")
    subject = semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )
    _expect_equal(run["semantic_subject_sha256"], subject, "chain run semantic subject")
    _expect_equal(run["source_attestation"]["semantic_subject_sha256"], subject, "chain run source subject")
    _expect_equal(run["source_attestation"]["state_unchanged"], True, "chain run source state")
    expected = _chain_artifact_definitions()
    _expect_equal(len(run["artifacts"]), len(expected), "chain run artifact count")
    for index, (record, definition) in enumerate(zip(run["artifacts"], expected, strict=True)):
        path, role, step, output, replay = definition
        _expect_equal(record, _chain_artifact(path, role, step, output, replay), f"chain run artifact {index}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish or verify the agent-facing IHP physical semantic chain."
    )
    parser.add_argument("snapshot", type=Path, nargs="?")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--native-evidence", type=Path)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--probe-report", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument("--normalized-output", type=Path)
    parser.add_argument("--oracle-output", type=Path)
    parser.add_argument("--decision-output", type=Path)
    parser.add_argument("--replay-output-dir", type=Path)
    parser.add_argument("--emit", action="store_true")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the verified native replay and generate its semantic chain receipt.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        default_publication = (
            args.snapshot is None
            and args.native_evidence is None
            and args.bundle_dir is None
            and args.probe_report is None
            and args.snapshot_output is None
            and args.probe_output is None
            and args.normalized_output is None
            and args.oracle_output is None
            and args.decision_output is None
            and args.replay_output_dir is None
            and not args.emit
            and not args.publish
        )
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        manifest_sha256 = sha256_file(manifest_path)
        if args.publish:
            if args.native_evidence is None:
                raise ConformanceError("--publish requires --native-evidence")
            explicit_outputs = (
                args.bundle_dir,
                args.probe_output,
                args.snapshot_output,
                args.normalized_output,
                args.oracle_output,
                args.decision_output,
                args.replay_output_dir,
            )
            if any(value is not None for value in explicit_outputs):
                raise ConformanceError(
                    "--publish selects the repository publication paths; do not mix explicit outputs"
                )
            args.bundle_dir = DEFAULT_BUNDLE
            args.probe_output = DEFAULT_PROBE_REPORT
            args.snapshot_output = DEFAULT_SNAPSHOT
            args.normalized_output = DEFAULT_NORMALIZED
            args.oracle_output = DEFAULT_ORACLE
            args.decision_output = DEFAULT_DECISION
            args.replay_output_dir = DEFAULT_REPLAY_DIR
        if args.native_evidence is not None:
            evidence = args.native_evidence.expanduser()
            if not evidence.is_absolute():
                evidence = Path.cwd() / evidence
            if args.publish:
                _require_regular_file(
                    evidence / "design-provenance.json",
                    label="release design provenance",
                    maximum_bytes=MAX_JSON_BYTES,
                )
                release_run = _read_document(
                    evidence / "run.json", label="release native run"
                )
                if "source_attestation" not in release_run:
                    raise ConformanceError(
                        "release publication requires a source-attested native replay"
                    )
            probes = run_probes(
                evidence,
                manifest,
                manifest_sha256=manifest_sha256,
            )
            snapshot = build_snapshot(
                evidence,
                manifest,
                probes,
                manifest_sha256=manifest_sha256,
            )
            bundle_dir = args.bundle_dir.expanduser().resolve() if args.bundle_dir else None
            if bundle_dir is not None:
                publish_bundle(evidence, bundle_dir)
                verify_snapshot(
                    snapshot,
                    manifest,
                    probes,
                    manifest_sha256=manifest_sha256,
                    bundle_dir=bundle_dir,
                )
            if args.probe_output is not None:
                _write_json(args.probe_output.expanduser().resolve(), probes)
            if args.snapshot_output is not None:
                _write_json(args.snapshot_output.expanduser().resolve(), snapshot)
            supporting_outputs = (
                args.normalized_output,
                args.oracle_output,
                args.decision_output,
                args.replay_output_dir,
            )
            if any(path is not None for path in supporting_outputs):
                if any(path is None for path in supporting_outputs):
                    raise ConformanceError(
                        "normalized, oracle, decision, and replay output paths must be "
                        "provided together"
                    )
                publish_supporting_documents(
                    snapshot,
                    probes,
                    normalized_path=args.normalized_output.expanduser().resolve(),
                    oracle_path=args.oracle_output.expanduser().resolve(),
                    decision_path=args.decision_output.expanduser().resolve(),
                    replay_dir=args.replay_output_dir.expanduser().resolve(),
                )
                verify_supporting_documents(
                    snapshot,
                    probes,
                    normalized_path=args.normalized_output.expanduser().resolve(),
                    oracle_path=args.oracle_output.expanduser().resolve(),
                    decision_path=args.decision_output.expanduser().resolve(),
                    replay_dir=args.replay_output_dir.expanduser().resolve(),
                )
            if args.snapshot is not None:
                expected = _read_document(
                    args.snapshot.expanduser().resolve(),
                    label="reviewed semantic evidence",
                )
                _expect_equal(snapshot, expected, "fresh semantic evidence")
            if args.emit:
                sys.stdout.buffer.write(_encode_json(snapshot))
            elif args.snapshot_output is None:
                print("Physical semantic chain verified from native evidence.")
            if args.publish:
                contract = _run_contract_tests()
                _write_json(HERE / "semantic-contract.json", contract)
                native_run = _read_document(
                    evidence / "run.json", label="native run metadata"
                )
                _write_chain_run(native_run["source_attestation"])
                _expect_equal(
                    _read_document(
                        HERE / "semantic-contract.json", label="semantic contract"
                    ),
                    contract,
                    "semantic contract",
                )
                _verify_chain_run()
            return 0

        snapshot_path = (
            args.snapshot.expanduser().resolve()
            if args.snapshot is not None
            else DEFAULT_SNAPSHOT
        )
        probe_path = (
            args.probe_report.expanduser().resolve()
            if args.probe_report is not None
            else DEFAULT_PROBE_REPORT
        )
        snapshot = _read_document(snapshot_path, label="semantic evidence")
        probes = _read_document(probe_path, label="semantic probe report")
        bundle_dir = (
            DEFAULT_BUNDLE
            if default_publication
            else args.bundle_dir.expanduser().resolve()
            if args.bundle_dir
            else None
        )
        verify_snapshot(
            snapshot,
            manifest,
            probes,
            manifest_sha256=manifest_sha256,
            bundle_dir=bundle_dir,
        )
        supporting_paths = (
            args.normalized_output.expanduser().resolve()
            if args.normalized_output
            else DEFAULT_NORMALIZED,
            args.oracle_output.expanduser().resolve()
            if args.oracle_output
            else DEFAULT_ORACLE,
            args.decision_output.expanduser().resolve()
            if args.decision_output
            else DEFAULT_DECISION,
            args.replay_output_dir.expanduser().resolve()
            if args.replay_output_dir
            else DEFAULT_REPLAY_DIR,
        )
        verify_supporting_documents(
            snapshot,
            probes,
            normalized_path=supporting_paths[0],
            oracle_path=supporting_paths[1],
            decision_path=supporting_paths[2],
            replay_dir=supporting_paths[3],
        )
        if default_publication:
            _expect_equal(
                _read_document(
                    HERE / "semantic-contract.json", label="semantic contract"
                ),
                _run_contract_tests(),
                "fresh physical contract tests",
            )
            _verify_chain_run()
    except ConformanceError as exc:
        print(f"semantic verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Physical semantic evidence verified: {CHAIN_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
