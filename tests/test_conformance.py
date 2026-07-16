from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "conformance" / "ihp-inverter"
MANIFEST = BUNDLE / "manifest.json"
VERIFY = BUNDLE / "verify.py"
SEMANTIC = BUNDLE / "semantic.py"
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
TRANSITIVE_PROVENANCE_MESSAGE = (
    "KLayout decks are executable Ruby. Only the main deck, declared provenance "
    "inputs, and optional waiver database are hashed by this operation."
)
TRANSCRIPT_LIMITATION = (
    "The artifact retains bounded stdout/stderr tails, not an unbounded native log."
)
NETGEN_PROVENANCE_MESSAGE = (
    "The executable setup Tcl may read transitive files or ambient environment "
    "state that OpenADA cannot infer."
)
NETGEN_SETUP_TRUST = (
    "caller-supplied executable Tcl; OpenADA does not sandbox the setup"
)
NETGEN_TRANSCRIPT_LIMITATION = (
    "Pass or fail requires both native streams to fit the complete capture bound; "
    "the artifact is not an unbounded native log."
)
DRC_FAIL_DESCRIPTIONS = {
    "Cnt.d": "5.14. Cnt.d Min. GatPoly enclosure of Cont is 0.07 um",
    "Cnt.e": "5.14. Cnt.e Min. Cont on GatPoly space to Activ is 0.14 um",
    "M1.b": "5.16. M1.b: Min. Metal1 (drawing + filler) space or notch: 0.18 μm.",
}
DRC_FAIL_GEOMETRY_VALUES = [
    "edge-pair: (3.215,-0.065;3.055,-0.065)/(3.283,-0.08;2.987,-0.08)",
    "edge-pair: (3.055,0.095;3.215,0.095)/(3.355,0.1;2.915,0.1)",
    "edge-pair: (2.93,-0.065;2.93,0.095)|(2.79,0.208;2.79,-0.178)",
    "edge-pair: (3.46,-0.095;3.46,0.208)|(3.32,0.095;3.32,-0.065)",
    "edge-pair: (3.46,-0.095;3.46,0.1)|(3.32,0.095;3.32,-0.065)",
    "edge-pair: (2.93,-0.065;2.93,0.095)|(2.79,0.1;2.79,-0.178)",
    "edge-pair: (0.37,-0.113;0.37,-0.005)|(0.23,0.108;0.23,0)",
    "edge-pair: (0.76,-0.005;0.76,-0.113)|(0.9,0;0.9,0.108)",
]
DRC_FAIL_COORDINATES = [
    [[3.215, -0.065], [3.055, -0.065], [3.283, -0.08], [2.987, -0.08]],
    [[3.055, 0.095], [3.215, 0.095], [3.355, 0.1], [2.915, 0.1]],
    [[2.93, -0.065], [2.93, 0.095], [2.79, 0.208], [2.79, -0.178]],
    [[3.46, -0.095], [3.46, 0.208], [3.32, 0.095], [3.32, -0.065]],
    [[3.46, -0.095], [3.46, 0.1], [3.32, 0.095], [3.32, -0.065]],
    [[2.93, -0.065], [2.93, 0.095], [2.79, 0.1], [2.79, -0.178]],
    [[0.37, -0.113], [0.37, -0.005], [0.23, 0.108], [0.23, 0.0]],
    [[0.76, -0.005], [0.76, -0.113], [0.9, 0.0], [0.9, 0.108]],
]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_drc(operation: dict) -> bool:
    return operation["tool"] == "klayout"


def _synthetic_drc_summary(operation: dict) -> dict:
    native = operation["native_report"]
    expected_violations = native.get("expected_violations", [])
    if not expected_violations:
        return {
            "category_count": 4,
            "cell_count": 1,
            "item_count": 0,
            "total_violations": 0,
            "waived_violations": 0,
            "category_counts": [],
            "violations": [],
            "normalization": {
                "geometry_values": 0,
                "retained_geometries": 0,
                "retained_coordinate_pairs": 0,
                "global_geometry_limit_reached": False,
            },
        }
    assert len(expected_violations) == len(DRC_FAIL_COORDINATES)
    violations = []
    for expected, coordinates in zip(expected_violations, DRC_FAIL_COORDINATES):
        category_path = expected["category_path"]
        category = ".".join(category_path)
        violations.append(
            {
                "category": category,
                "category_path": category_path,
                "description": DRC_FAIL_DESCRIPTIONS[category],
                "cell": expected["cell"],
                "multiplicity": expected["multiplicity"],
                "waived": expected["waived"],
                "tags": [],
                "geometries": [
                    {
                        "type": "edge-pair",
                        "coordinates": coordinates,
                        "coordinates_truncated": False,
                    }
                ],
                "geometries_truncated": False,
            }
        )
    return {
        "category_count": len(DRC_FAIL_DESCRIPTIONS),
        "cell_count": len({item["cell"] for item in expected_violations}),
        "item_count": native["expected_item_count"],
        "total_violations": native["expected_total_violations"],
        "waived_violations": native["expected_waived_violations"],
        "category_counts": native["expected_category_counts"],
        "violations": violations,
        "normalization": native["expected_normalization"],
    }


def _synthetic_result(operation_name: str, operation: dict, artifact: bytes) -> dict:
    inputs = [
        {
            "path": record["path"],
            "kind": record["kind"],
            "role": record["role"],
            "exists": True,
            "bytes": 1,
            "sha256": record["sha256"],
        }
        for record in operation["inputs"]
    ]
    identity = operation["tool_identity"]
    arguments = operation["arguments"]
    if _is_drc(operation):
        command = [
            identity["path"],
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
    else:
        command = [
            identity["path"],
            "-batch",
            "lvs",
            f"{arguments['layout_netlist']} {arguments['cell']}",
            f"{arguments['schematic_netlist']} {arguments['cell']}",
            arguments["setup"],
            arguments["report"],
            "-json",
        ]
    artifacts = [
        {
            "path": operation["artifact"]["path"],
            "kind": operation["artifact"]["kind"],
            "role": operation["artifact"]["role"],
            "exists": True,
            "bytes": len(artifact),
            "sha256": _sha256(artifact),
        }
    ]
    if _is_drc(operation):
        transcript = _transcript_content()
        artifacts.append(
            {
                "path": operation["transcript_artifact"]["path"],
                "kind": operation["transcript_artifact"]["kind"],
                "role": operation["transcript_artifact"]["role"],
                "exists": True,
                "bytes": len(transcript),
                "sha256": _sha256(transcript),
            }
        )
    else:
        native_json = _lvs_json_content(operation)
        transcript = _lvs_transcript_content(operation)
        for key, content in (
            ("json_artifact", native_json),
            ("transcript_artifact", transcript),
        ):
            expected = operation[key]
            artifacts.append(
                {
                    "path": expected["path"],
                    "kind": expected["kind"],
                    "role": expected["role"],
                    "exists": True,
                    "bytes": len(content),
                    "sha256": _sha256(content),
                }
            )
    result = {
        "schema": "openada.result/v0alpha1",
        "operation": "drc" if _is_drc(operation) else operation_name,
        "tool": {
            "name": operation["tool"],
            "path": identity["path"],
            "version": identity["version"],
        },
        "execution": {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 1,
            "command": command,
            "cwd": "/evidence",
        },
        "engineering": {
            "status": operation["expect"]["engineering_status"],
            "summary": (
                (
                    "KLayout reported zero DRC violations."
                    if operation["expect"].get("total_violations") == 0
                    else (
                        f"KLayout reported {operation['expect']['total_violations']} "
                        "DRC violation(s)."
                    )
                )
                if _is_drc(operation)
                else (
                    "Netgen produced clean, agreeing native evidence for a unique "
                    "LVS match."
                )
            ),
        },
        "inputs": inputs,
        "artifacts": artifacts,
        "diagnostics": [],
        "data": {},
        "provenance": {
            "openada_version": "test",
            "created_at": "2026-07-13T00:00:00Z",
            "host": {
                "system": "Linux",
                "machine": "x86_64",
                "python": "3.11.0",
            },
        },
    }
    if _is_drc(operation):
        drc_summary = _synthetic_drc_summary(operation)
        transcript = _transcript_content()
        validation = {
            "valid": True,
            "reason": "lyrdb.valid",
            "bytes": len(artifact),
        }
        result["diagnostics"] = [
            {
                "severity": "warning",
                "code": "provenance.transitive_rules_unenumerated",
                "message": TRANSITIVE_PROVENANCE_MESSAGE,
            }
        ]
        result["data"] = {
            "drc_clean": operation["expect"]["drc_clean"],
            "inputs_stable": True,
            "changed_inputs": [],
            "working_directory": "/evidence",
            "working_directory_is_sandbox": False,
            "top_cell": arguments["top_cell"],
            "deck_variables": [],
            "startup": {
                "batch_flag": "-b",
                "database_only": True,
                "configuration_files": "disabled",
                "implicit_macros": "disabled",
            },
            "transitive_rule_inputs_enumerated": False,
            "ambient_environment_enumerated": False,
            "deck_trust": (
                "caller-supplied executable Ruby; OpenADA does not sandbox the deck"
            ),
            "environment": {
                "PDK": None,
                "PDK_ROOT": "/foss/pdks",
                "KLAYOUT_PATH": None,
                "KLAYOUT_HOME": None,
            },
            "report_output": {
                "ownership": "variable",
                "binding_variable": "report",
                "fresh_required": True,
                "parent_anchored": True,
                "declared_path": arguments["report"],
                "path": arguments["report"],
                "capture": {
                    "path": arguments["report"],
                    "origin": "deck",
                    "parent_anchored": True,
                    "status": "valid",
                    "bytes": len(artifact),
                    "sha256": _sha256(artifact),
                    "validation": validation,
                },
            },
            "waiver_database": {
                "policy": "disabled-by-absence",
                "path": arguments["report"] + ".w",
                "declared": False,
                "status": "absent",
            },
            "transcript": {
                "path": operation["transcript_artifact"]["path"],
                "origin": "openada",
                "capture_policy": "bounded process tails",
                "status": "valid",
                "bytes": len(transcript),
                "sha256": _sha256(transcript),
                "stdout_retained_bytes": 0,
                "stderr_retained_bytes": 0,
                "stdout_observed_bytes": 0,
                "stderr_observed_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_tail": "",
                "stderr_tail": "",
                "limitation": TRANSCRIPT_LIMITATION,
            },
            "report": {
                "validation": validation,
                "generator": operation["native_report"]["generator"],
                "generator_script": arguments["rules"],
                "top_cell": operation["native_report"]["top_cell"],
                "category_count": drc_summary["category_count"],
                "cell_count": drc_summary["cell_count"],
                "item_count": drc_summary["item_count"],
                "total_violations": drc_summary["total_violations"],
                "waived_violations": drc_summary["waived_violations"],
                "category_counts": drc_summary["category_counts"],
                "category_counts_truncated": False,
                "violations": drc_summary["violations"],
                "violations_truncated": False,
                "normalization": drc_summary["normalization"],
            },
        }
    else:
        native_json = _lvs_json_content(operation)
        transcript = _lvs_transcript_content(operation)
        stdout = _lvs_stdout(operation)
        report_validation = {
            "valid": True,
            "bytes": len(artifact),
            "reason": "report.valid",
        }
        json_validation = {
            "valid": True,
            "bytes": len(native_json),
            "reason": "json.valid",
        }
        native_report = {
            "validation": report_validation,
            "outcome": "pass",
            "final_match": True,
            "legacy_terminal_match": False,
            "unique_match_markers": True,
            "terminal_outcome": "pass",
            "terminal_style": "final-result",
            "terminal_conflict": False,
            "top_cell": arguments["cell"],
            "comparison_count": 1,
            "top_comparison_count": 1,
            "summary_binding": [arguments["cell"], arguments["cell"]],
            "pins_binding": [arguments["cell"], arguments["cell"]],
            "device_classes_binding": [arguments["cell"], arguments["cell"]],
            "pin_lists_equivalent": True,
            "structure_complete": True,
            "device_counts": [4, 4],
            "node_counts": [6, 6],
            "mismatch_count": 0,
            "mismatches": [],
            "mismatches_truncated": False,
        }
        comparison = {
            "validation": json_validation,
            "outcome": "pass",
            "comparison_count": 1,
            "top_cell": arguments["cell"],
            "top_comparison_count": 1,
            "device_counts": [
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
            ],
            "node_counts": [6, 6],
            "pin_counts": [4, 4],
            "mismatch_count": 0,
            "mismatches": [],
            "mismatches_truncated": False,
            "lvs_match": True,
            "report_outcome": "pass",
            "json_outcome": "pass",
            "outcomes_agree": True,
            "structural_counts_agree": True,
            "evidence_agrees": True,
            "report": native_report,
        }
        result["diagnostics"] = [
            {
                "severity": "warning",
                "code": "netgen.provenance_incomplete",
                "message": NETGEN_PROVENANCE_MESSAGE,
            }
        ]
        result["data"] = {
            "working_directory": "/evidence",
            "working_directory_is_sandbox": False,
            "report_output": {
                "ownership": "native",
                "fresh_required": True,
                "parent_anchored": True,
                "path": operation["artifact"]["path"],
                "native_json_path": operation["json_artifact"]["path"],
                "capture": {
                    "path": operation["artifact"]["path"],
                    "origin": "netgen",
                    "parent_anchored": True,
                    "status": "valid",
                    "bytes": len(artifact),
                    "sha256": _sha256(artifact),
                    "validation": report_validation,
                },
            },
            "json_output": {
                "ownership": "native-netgen-json",
                "fresh_required": True,
                "parent_anchored": True,
                "path": operation["json_artifact"]["path"],
                "capture": {
                    "path": operation["json_artifact"]["path"],
                    "origin": "netgen",
                    "parent_anchored": True,
                    "status": "valid",
                    "bytes": len(native_json),
                    "sha256": _sha256(native_json),
                    "validation": json_validation,
                },
            },
            "setup_trust": NETGEN_SETUP_TRUST,
            "transitive_setup_inputs_enumerated": False,
            "ambient_environment_enumerated": False,
            "inputs_stable": True,
            "changed_inputs": [],
            "lvs_match": True,
            "comparison": comparison,
            "transcript": {
                "path": operation["transcript_artifact"]["path"],
                "origin": "openada",
                "capture_policy": "bounded complete-or-unknown process streams",
                "stdout_observed_bytes": len(stdout),
                "stderr_observed_bytes": 0,
                "stdout_retained_bytes": len(stdout),
                "stderr_retained_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "status": "valid",
                "bytes": len(transcript),
                "sha256": _sha256(transcript),
                "assessment": {
                    "complete": True,
                    "utf8_valid": True,
                    "setup_read": True,
                    "setup_error": False,
                    "stdout_error": False,
                    "stderr_empty": True,
                    "stderr_policy": "empty-or-reviewed-netgen-permute-warning",
                    "stderr_line_count": 0,
                    "stderr_reviewed_warning_count": 0,
                    "stderr_unrecognized_count": 0,
                    "stderr_accepted": True,
                    "lvs_done": True,
                    "clean": True,
                },
                "stdout_tail": stdout.decode("utf-8")[-4_000:],
                "stderr_tail": "",
                "limitation": NETGEN_TRANSCRIPT_LIMITATION,
            },
        }
    return result


def _write_run_metadata(evidence: Path, manifest: dict) -> None:
    clean_checkout_state = {
        "commit": "0" * 40,
        "tracked_files_modified": False,
        "untracked_files_present": False,
        "working_tree_modified": False,
        "status_entry_count": 0,
        "status_sha256": hashlib.sha256(b"").hexdigest(),
    }
    metadata = {
        "schema": "openada.conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "conformance_manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "created_at": "2026-07-13T00:00:00Z",
        "design_revision": manifest["design"]["revision"],
        "image": {
            "reference": manifest["runtime"]["image"]["reference"],
            "id": IMAGE_CONFIG_DIGEST,
            "os": "linux",
            "architecture": "amd64",
        },
        "openada_checkout": {
            "before": clean_checkout_state,
            "after": dict(clean_checkout_state),
            "state_unchanged": True,
            "commit_exact": True,
        },
        "network": "none during EDA execution",
    }
    (evidence / "run.json").write_text(json.dumps(metadata), encoding="utf-8")


def _transcript_content() -> bytes:
    return (
        b"OpenADA bounded KLayout process transcript\n"
        b"stdout: retained_tail_bytes=0 observed_bytes=0 truncated=false\n"
        b"--- stdout tail ---\n\n"
        b"stderr: retained_tail_bytes=0 observed_bytes=0 truncated=false\n"
        b"--- stderr tail ---\n\n"
    )


def _lvs_json_content(operation: dict) -> bytes:
    cell = operation["arguments"]["cell"]
    document = [
        {
            "name": [cell, cell],
            "devices": [
                [
                    ["sg13_lv_nmos", 1],
                    ["sg13_lv_pmos", 1],
                    ["ntap1", 1],
                    ["ptap1", 1],
                ],
                [
                    ["sg13_lv_nmos", 1],
                    ["sg13_lv_pmos", 1],
                    ["ntap1", 1],
                    ["ptap1", 1],
                ],
            ],
            "nets": [6, 6],
            "badnets": [],
            "badelements": [],
            "pins": [
                ["Vout", "Vin", "Gnd", "Vdd"],
                ["Vout", "Vin", "Gnd", "Vdd"],
            ],
        }
    ]
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def _lvs_stdout(operation: dict) -> bytes:
    arguments = operation["arguments"]
    return (
        f"Netgen synthetic pinned fixture\n"
        f"Generating JSON file result\n"
        f"Reading setup file {arguments['setup']}\n"
        f"Comparison output logged to file {arguments['report']}\n"
        "Netlists match uniquely.\n"
        "Final result: Circuits match uniquely.\n"
        "LVS Done.\n"
    ).encode("utf-8")


def _lvs_transcript_content(
    operation: dict,
    *,
    stdout: bytes | None = None,
    stderr: bytes = b"",
) -> bytes:
    retained_stdout = _lvs_stdout(operation) if stdout is None else stdout
    return b"\n".join(
        (
            b"OpenADA bounded complete Netgen process transcript",
            (
                f"stdout: retained_utf8_bytes={len(retained_stdout)} "
                f"observed_bytes={len(retained_stdout)} truncated=false"
            ).encode("ascii"),
            b"--- stdout ---",
            retained_stdout,
            (
                f"stderr: retained_utf8_bytes={len(stderr)} "
                f"observed_bytes={len(stderr)} truncated=false"
            ).encode("ascii"),
            b"--- stderr ---",
            stderr,
            b"",
        )
    )


def _artifact_content(operation_name: str, operation: dict) -> bytes:
    if _is_drc(operation):
        expected_violations = operation["native_report"].get(
            "expected_violations", []
        )
        if expected_violations:
            category_xml = "".join(
                (
                    "<category>"
                    f"<name>{category}</name>"
                    f"<description>{description}</description>"
                    "<categories/>"
                    "</category>"
                )
                for category, description in DRC_FAIL_DESCRIPTIONS.items()
            )
            cell_xml = "".join(
                f"<cell><name>{cell}</name><variant/></cell>"
                for cell in ("lvs_tester", "nmos$1")
            )
            item_xml = "".join(
                (
                    "<item><tags/>"
                    f"<category>'{'.'.join(expected['category_path'])}'</category>"
                    f"<cell>{expected['cell']}</cell>"
                    f"<multiplicity>{expected['multiplicity']}</multiplicity>"
                    f"<values><value>{geometry}</value></values>"
                    "</item>"
                )
                for expected, geometry in zip(
                    expected_violations, DRC_FAIL_GEOMETRY_VALUES
                )
            )
            return (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<report-database>"
                f"<generator>{operation['native_report']['generator']}</generator>"
                f"<top-cell>{operation['native_report']['top_cell']}</top-cell>"
                f"<categories>{category_xml}</categories>"
                f"<cells>{cell_xml}</cells>"
                f"<items>{item_xml}</items>"
                "</report-database>\n"
            ).encode("utf-8")
        return (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b"<report-database>"
            + f"<generator>{operation['native_report']['generator']}</generator>".encode()
            + f"<top-cell>{operation['native_report']['top_cell']}</top-cell>".encode()
            + b"<categories><category><name>P1</name><categories><category><name>SAME</name>"
            b"</category></categories></category><category><name>P2</name><categories>"
            b"<category><name>SAME</name></category></categories></category></categories>"
            + (
                "<cells><cell><name>"
                f"{operation['native_report']['top_cell']}"
                "</name><variant/></cell></cells>"
            ).encode()
            + b"<items></items></report-database>\n"
        )
    return (
        b"Subcircuit summary:\n"
        b"Circuit 1: inverter |Circuit 2: inverter\n"
        b"Number of devices: 4 |Number of devices: 4\n"
        b"Number of nets: 6 |Number of nets: 6\n"
        b"Netlists match uniquely.\n"
        b"Subcircuit pins:\n"
        b"Circuit 1: inverter |Circuit 2: inverter\n"
        b"Vout |Vout\nVin |Vin\nGnd |Gnd\nVdd |Vdd\n"
        b"Cell pin lists are equivalent.\n"
        b"Device classes inverter and inverter are equivalent.\n"
        b"Final result: Circuits match uniquely.\n.\n"
    )


def _write_synthetic_evidence(evidence: Path, manifest: dict) -> None:
    evidence.mkdir()
    _write_run_metadata(evidence, manifest)
    for name, operation in manifest["operations"].items():
        artifact = _artifact_content(name, operation)
        (evidence / operation["artifact"]["filename"]).write_bytes(artifact)
        if _is_drc(operation):
            (evidence / operation["transcript_artifact"]["filename"]).write_bytes(
                _transcript_content()
            )
        else:
            (evidence / operation["json_artifact"]["filename"]).write_bytes(
                _lvs_json_content(operation)
            )
            (evidence / operation["transcript_artifact"]["filename"]).write_bytes(
                _lvs_transcript_content(operation)
            )
        result = _synthetic_result(name, operation, artifact)
        (evidence / operation["result_filename"]).write_text(
            json.dumps(result), encoding="utf-8"
        )


def _verify(evidence: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY), str(evidence)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _rewrite_native_artifact(
    evidence: Path,
    operation: dict,
    *,
    kind: str,
    content: bytes,
) -> None:
    candidates = [operation["artifact"]]
    for key in ("json_artifact", "transcript_artifact"):
        if key in operation:
            candidates.append(operation[key])
    expected = next(record for record in candidates if record["kind"] == kind)
    (evidence / expected["filename"]).write_bytes(content)
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    record = next(item for item in result["artifacts"] if item["kind"] == kind)
    record["bytes"] = len(content)
    record["sha256"] = _sha256(content)
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _tamper_drc_result(
    evidence: Path,
    manifest: dict,
    path: tuple[str, ...],
    value: object,
) -> None:
    operation = manifest["operations"]["drc"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _drc_item(multiplicity: str | None, *, category: str = "'P1'.SAME") -> bytes:
    multiplicity_xml = (
        b"" if multiplicity is None else f"<multiplicity>{multiplicity}</multiplicity>".encode()
    )
    return (
        b"<item><tags/><category>"
        + category.encode()
        + b"</category><cell>inverter</cell>"
        + multiplicity_xml
        + b"<values/></item>"
    )


def _drc_with_items(operation: dict, items: bytes) -> bytes:
    return _artifact_content("drc", operation).replace(
        b"<items></items>",
        b"<items>" + items + b"</items>",
    )


def test_pinned_ihp_manifest_and_offline_verifier(tmp_path: Path) -> None:
    manifest_only = subprocess.run(
        [sys.executable, str(VERIFY), "--manifest-only"],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert manifest_only.returncode == 0, manifest_only.stderr

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)

    verified = _verify(evidence)
    assert verified.returncode == 0, verified.stderr


def test_manifest_pins_real_ihp_drc_failure_and_exact_native_outcome() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    operation = manifest["operations"]["drc_fail"]

    assert operation["arguments"]["gds"] == (
        "/design/modules/module_0_foundations/lvs_tester/GDS/gallery.gds"
    )
    assert operation["arguments"]["top_cell"] == "lvs_tester"
    assert operation["inputs"][0]["sha256"] == (
        "c536ff737248e62cc209a6aec764a7f21750d0978e2e8351a4f0c2a6f144bc96"
    )
    assert operation["expect"] == {
        "execution_status": "completed",
        "exit_code": 0,
        "engineering_status": "fail",
        "drc_clean": False,
        "total_violations": 8,
    }
    native = operation["native_report"]
    assert native["expected_item_count"] == 8
    assert native["expected_total_violations"] == 8
    assert native["expected_waived_violations"] == 0
    assert [
        (entry["category"], entry["violations"])
        for entry in native["expected_category_counts"]
    ] == [("M1.b", 6), ("Cnt.d", 1), ("Cnt.e", 1)]
    assert [entry["cell"] for entry in native["expected_violations"]] == [
        "lvs_tester",
        "lvs_tester",
        "lvs_tester",
        "lvs_tester",
        "lvs_tester",
        "lvs_tester",
        "nmos$1",
        "nmos$1",
    ]
    assert native["expected_normalization"] == {
        "geometry_values": 8,
        "retained_geometries": 8,
        "retained_coordinate_pairs": 32,
        "global_geometry_limit_reached": False,
    }


def test_verifier_rejects_seven_item_failure_even_with_reconciled_result(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc_fail"]
    artifact = _artifact_content("drc_fail", operation)
    last_start = artifact.rfind(b"<item>")
    last_end = artifact.find(b"</item>", last_start) + len(b"</item>")
    artifact = artifact[:last_start] + artifact[last_end:]
    artifact_path = evidence / operation["artifact"]["filename"]
    artifact_path.write_bytes(artifact)

    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    digest = _sha256(artifact)
    artifact_record = next(
        record
        for record in result["artifacts"]
        if record["kind"] == "klayout-lyrdb"
    )
    artifact_record.update({"bytes": len(artifact), "sha256": digest})
    validation = {"valid": True, "reason": "lyrdb.valid", "bytes": len(artifact)}
    result["data"]["report_output"]["capture"].update(
        {"bytes": len(artifact), "sha256": digest, "validation": validation}
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
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "native DRC item count" in verified.stderr


@pytest.mark.parametrize(
    "path,value,location",
    [
        (("engineering", "status"), "pass", "drc_fail.engineering.status"),
        (("data", "drc_clean"), True, "drc_fail.data.drc_clean"),
        (
            ("data", "report", "total_violations"),
            7,
            "drc.data.report.total_violations",
        ),
        (("data", "report", "category_counts"), [], "drc_fail.data.report.category_counts"),
    ],
)
def test_verifier_rejects_normalized_drc_failure_claim_tamper(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    location: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc_fail"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert location in verified.stderr


@pytest.mark.parametrize(
    "path,value",
    [
        (
            ("operations", "drc_fail", "arguments", "gds"),
            "/design/unreviewed.gds",
        ),
        (("operations", "drc_fail", "inputs", 0, "sha256"), "0" * 64),
        (
            ("operations", "drc_fail", "native_report", "expected_item_count"),
            7,
        ),
    ],
)
def test_manifest_rejects_drc_failure_pin_tamper(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target = manifest
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--manifest",
            str(manifest_path),
            "--manifest-only",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert verified.returncode == 1
    assert "operations.drc_fail" in verified.stderr
    assert "reviewed" in verified.stderr


def test_verifier_rejects_an_artifact_hash_mismatch(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)

    drc_artifact = evidence / manifest["operations"]["drc"]["artifact"]["filename"]
    drc_artifact.write_bytes(b"tampered after result creation\n")
    verified = _verify(evidence)
    assert verified.returncode == 1
    assert "klayout-lyrdb.bytes" in verified.stderr or "klayout-lyrdb.sha256" in verified.stderr


def test_verifier_rejects_schema_invalid_result(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    result_path = evidence / manifest["operations"]["drc"]["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("provenance")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "violates its JSON Schema" in verified.stderr


def test_verifier_rejects_invalid_run_metadata_shape(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    run_path = evidence / "run.json"
    metadata = json.loads(run_path.read_text(encoding="utf-8"))
    metadata["unreviewed"] = True
    run_path.write_text(json.dumps(metadata), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "run metadata violates its JSON Schema" in verified.stderr


def test_verifier_requires_untracked_checkout_provenance(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    run_path = evidence / "run.json"
    metadata = json.loads(run_path.read_text(encoding="utf-8"))
    metadata["openada_checkout"]["before"].pop("untracked_files_present")
    run_path.write_text(json.dumps(metadata), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "run metadata violates its JSON Schema" in verified.stderr


def test_verifier_recomputes_commit_exact_flag(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    run_path = evidence / "run.json"
    metadata = json.loads(run_path.read_text(encoding="utf-8"))
    metadata["openada_checkout"]["commit_exact"] = False
    run_path.write_text(json.dumps(metadata), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "run.openada_checkout.commit_exact" in verified.stderr


def test_verifier_rejects_result_and_directory_symlinks(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    result_path = evidence / manifest["operations"]["drc"]["result_filename"]
    external_result = tmp_path / "external-drc.json"
    result_path.replace(external_result)
    result_path.symlink_to(external_result)

    result_link = _verify(evidence)
    assert result_link.returncode == 1
    assert "regular, non-symlink file" in result_link.stderr

    result_path.unlink()
    external_result.replace(result_path)
    evidence_link = tmp_path / "evidence-link"
    evidence_link.symlink_to(evidence, target_is_directory=True)
    directory_link = _verify(evidence_link)
    assert directory_link.returncode == 1
    assert "non-symlink directory" in directory_link.stderr


def test_verifier_independently_rejects_native_lvs_mismatch(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    artifact_path = evidence / operation["artifact"]["filename"]
    artifact = b"Final result: Circuits do not match.\n"
    artifact_path.write_bytes(artifact)
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["artifacts"][0]["bytes"] = len(artifact)
    result["artifacts"][0]["sha256"] = _sha256(artifact)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "native LVS report" in verified.stderr


@pytest.mark.parametrize(
    "original, replacement, label",
    [
        (
            b"Number of devices: 4 |Number of devices: 4",
            b"Number of devices: 5 |Number of devices: 5",
            "device",
        ),
        (
            b"Number of nets: 6 |Number of nets: 6",
            b"Number of nets: 7 |Number of nets: 7",
            "node",
        ),
    ],
)
def test_verifier_cross_checks_lvs_report_and_json_counts(
    tmp_path: Path,
    original: bytes,
    replacement: bytes,
    label: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    report = _artifact_content("lvs", operation).replace(original, replacement)
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="netgen-comparison",
        content=report,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert f"report and JSON {label} counts disagree" in verified.stderr


@pytest.mark.parametrize(
    "tamper",
    [
        "malformed",
        "duplicate-key",
        "non-finite",
        "overlong-key",
        "duplicate-device",
        "duplicate-pin",
        "mismatching-counts",
        "wrong-top",
    ],
)
def test_verifier_independently_rejects_untrustworthy_native_lvs_json(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    if tamper == "malformed":
        content = b"[{"
    elif tamper == "duplicate-key":
        content = (
            b'[{"name":["inverter","inverter"],'
            b'"name":["inverter","inverter"]}]'
        )
    elif tamper == "non-finite":
        content = _lvs_json_content(operation).replace(b"1", b"1e9999", 1)
    else:
        document = json.loads(_lvs_json_content(operation))
        if tamper == "overlong-key":
            document[0]["goodnets"] = [{"x" * 65_537: 0}]
        elif tamper == "duplicate-device":
            document[0]["devices"][0].append(["sg13_lv_nmos", 0])
            document[0]["devices"][1].append(["sg13_lv_nmos", 0])
        elif tamper == "duplicate-pin":
            document[0]["pins"][0].append("Vin")
            document[0]["pins"][1].append("Vin")
        elif tamper == "mismatching-counts":
            document[0]["nets"] = [6, 7]
        else:
            document[0]["name"] = ["ambient", "ambient"]
        content = json.dumps(document).encode("utf-8")
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="netgen-comparison-json",
        content=content,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "native LVS JSON" in verified.stderr


def test_verifier_rejects_netgen_setup_error_false_pass_transcript(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    stdout = _lvs_stdout(operation).replace(
        b"LVS Done.\n",
        b"Warning: There were errors reading the setup file\nLVS Done.\n",
    )
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="netgen-transcript",
        content=_lvs_transcript_content(operation, stdout=stdout),
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "does not prove a clean setup and completion" in verified.stderr
    assert "setup_error" in verified.stderr


def test_verifier_accepts_and_exposes_only_reviewed_netgen_stderr(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    stderr = (
        b"Unable to permute model ntap1 pins 1, 2.\n"
        b"Unable to permute model sg13_lv_nmos pins 1, 3.\n"
    )
    transcript = _lvs_transcript_content(operation, stderr=stderr)
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="netgen-transcript",
        content=transcript,
    )
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    transcript_data = result["data"]["transcript"]
    transcript_data.update(
        {
            "stderr_observed_bytes": len(stderr),
            "stderr_retained_bytes": len(stderr),
            "bytes": len(transcript),
            "sha256": _sha256(transcript),
            "stderr_tail": stderr.decode("utf-8"),
        }
    )
    transcript_data["assessment"].update(
        {
            "stderr_empty": False,
            "stderr_line_count": 2,
            "stderr_reviewed_warning_count": 2,
            "stderr_unrecognized_count": 0,
            "stderr_accepted": True,
        }
    )
    result["diagnostics"].append(
        {
            "severity": "warning",
            "code": "netgen.stderr_reviewed_warning",
            "message": (
                "Netgen emitted 2 reviewed "
                "'Unable to permute model ... pins ...' warning line(s)."
            ),
        }
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 0, verified.stderr


def test_verifier_rejects_unrecognized_netgen_stderr(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="netgen-transcript",
        content=_lvs_transcript_content(
            operation,
            stderr=b"Unable to permute model bad! pins 1, 2.\n",
        ),
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "does not prove a clean setup and completion" in verified.stderr
    assert "stderr_unrecognized_count" in verified.stderr


def test_verifier_rejects_a_changed_lvs_native_json_hash(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    (evidence / operation["json_artifact"]["filename"]).write_bytes(b"[]\n")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "netgen-comparison-json" in verified.stderr


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize(
    "artifact_key", ["artifact", "json_artifact", "transcript_artifact"]
)
def test_verifier_rejects_linked_lvs_native_evidence(
    tmp_path: Path,
    link_kind: str,
    artifact_key: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    expected = manifest["operations"]["lvs"][artifact_key]
    artifact_path = evidence / expected["filename"]
    external = tmp_path / f"external-{artifact_path.name}"
    artifact_path.replace(external)
    if link_kind == "symlink":
        artifact_path.symlink_to(external)
    else:
        os.link(external, artifact_path)

    verified = _verify(evidence)

    assert verified.returncode == 1
    expected_message = (
        "regular, non-symlink file" if link_kind == "symlink" else "exactly one hard link"
    )
    assert expected_message in verified.stderr


def test_verifier_rejects_stale_unbound_lvs_output(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    stale = evidence / "inverter.lvs.comp.previous"
    stale.write_text("stale comparison\n", encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "evidence directory contents differ" in verified.stderr
    assert stale.name in verified.stderr


def test_verifier_independently_rejects_native_drc_violation(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    artifact_path = evidence / operation["artifact"]["filename"]
    artifact = _drc_with_items(operation, _drc_item("37"))
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=artifact,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "native DRC report contains 1 item(s), weighted as 37 violation(s)" in verified.stderr


@pytest.mark.parametrize(
    "multiplicity",
    [None, "0", "-1", "1.0", "01", "9223372036854775808"],
    ids=["missing", "zero", "negative", "fractional", "leading-zero", "overflow"],
)
def test_verifier_requires_one_bounded_positive_native_multiplicity(
    tmp_path: Path,
    multiplicity: str | None,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=_drc_with_items(operation, _drc_item(multiplicity)),
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "multiplicity" in verified.stderr


def test_verifier_rejects_the_former_minimal_clean_lyrdb_spoof(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=b"<report-database><categories/><items/></report-database>",
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "must contain one direct generator section" in verified.stderr


def test_verifier_requires_the_native_child_generator_not_a_root_attribute(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    generator = operation["native_report"]["generator"]
    spoof = (
        f'<report-database generator="{generator}"><top-cell>inverter</top-cell>'
        "<categories><category><name>A</name></category></categories>"
        "<cells><cell><name>inverter</name></cell></cells><items/>"
        "</report-database>"
    ).encode()
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=spoof,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "must contain one direct generator section" in verified.stderr


@pytest.mark.parametrize(
    "junk",
    [
        b"<junk><category><name>HIDDEN</name></category></junk>",
        b"<category><name>PARENT</name><category><name>HIDDEN</name></category></category>",
    ],
    ids=["junk-wrapper", "missing-categories-container"],
)
def test_verifier_rejects_categories_outside_exact_recursive_native_ancestry(
    tmp_path: Path,
    junk: bytes,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    artifact = _artifact_content("drc", operation).replace(
        b"<categories>",
        b"<categories>" + junk,
        1,
    )
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=artifact,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "category is outside its exact native ancestry" in verified.stderr


def test_verifier_requires_full_recursive_category_identity_for_items(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=_drc_with_items(operation, _drc_item("1", category="SAME")),
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "references an undeclared category" in verified.stderr


def test_verifier_rejects_overdeep_native_xml(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    nested = b""
    for index in range(70):
        nested += f"<category><name>N{index}</name><categories>".encode()
    nested += b"<category><name>LEAF</name></category>"
    nested += b"</categories></category>" * 70
    artifact = _artifact_content("drc", operation).replace(
        b"<categories>",
        b"<categories>" + nested,
        1,
    )
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=artifact,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "XML depth limit" in verified.stderr


def test_verifier_rejects_namespaced_native_shape_spoof(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    artifact = _artifact_content("drc", operation).replace(
        b"<report-database>",
        b'<report-database xmlns="urn:not-klayout">',
        1,
    )
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-lyrdb",
        content=artifact,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "non-native XML namespace" in verified.stderr


def test_verifier_rejects_an_ambient_klayout_waiver_sidecar(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    waiver = evidence / (operation["artifact"]["filename"] + ".w")
    waiver.write_text("ambient waiver\n", encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "evidence directory contents differ" in verified.stderr
    assert waiver.name in verified.stderr


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("artifact_key", ["artifact", "transcript_artifact"])
def test_verifier_rejects_linked_native_evidence(
    tmp_path: Path,
    link_kind: str,
    artifact_key: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    artifact_path = evidence / operation[artifact_key]["filename"]
    external = tmp_path / f"external-{artifact_path.name}"
    artifact_path.replace(external)
    if link_kind == "symlink":
        artifact_path.symlink_to(external)
    else:
        os.link(external, artifact_path)

    verified = _verify(evidence)

    assert verified.returncode == 1
    if link_kind == "symlink":
        assert "regular, non-symlink file" in verified.stderr
    else:
        assert "exactly one hard link" in verified.stderr


@pytest.mark.parametrize(
    "content, message",
    [
        (
            _transcript_content().replace(
                b"OpenADA bounded KLayout process transcript",
                b"untrusted transcript",
            ),
            "capture header",
        ),
        (
            _transcript_content().replace(
                b"observed_bytes=0 truncated=false",
                b"observed_bytes=1 truncated=false",
                1,
            ),
            "inconsistent stdout metadata",
        ),
        (
            _transcript_content().replace(
                b"retained_tail_bytes=0 observed_bytes=0 truncated=false",
                b"retained_tail_bytes=0 observed_bytes=12001 truncated=true",
                1,
            ),
            "inconsistent stdout metadata",
        ),
        (
            b"OpenADA bounded KLayout process transcript\n"
            b"stdout: retained_tail_bytes=1 observed_bytes=12001 truncated=true\n"
            b"--- stdout tail ---\nx\n"
            b"stderr: retained_tail_bytes=0 observed_bytes=0 truncated=false\n"
            b"--- stderr tail ---\n\n",
            "inconsistent stdout metadata",
        ),
        (
            _transcript_content().replace(
                b"retained_tail_bytes=0",
                b"retained_tail_bytes=00",
                1,
            ),
            "invalid stdout metadata",
        ),
        (_transcript_content() + b"junk", "stderr tail has the wrong byte length"),
        (
            b"OpenADA bounded KLayout process transcript\n"
            b"stdout: retained_tail_bytes=1 observed_bytes=1 truncated=false\n"
            b"--- stdout tail ---\n\xff\n"
            b"stderr: retained_tail_bytes=0 observed_bytes=0 truncated=false\n"
            b"--- stderr tail ---\n\n",
            "tail is not UTF-8",
        ),
    ],
    ids=[
        "header",
        "observed-length",
        "truncated-length",
        "implausibly-short-truncated-tail",
        "noncanonical-decimal",
        "trailing-bytes",
        "utf8",
    ],
)
def test_verifier_rejects_noncanonical_transcript_grammar(
    tmp_path: Path,
    content: bytes,
    message: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-transcript",
        content=content,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert message in verified.stderr


def test_verifier_rejects_transcript_over_25_000_bytes(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-transcript",
        content=b"x" * 25_001,
    )

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "exceeds the 25000-byte verification limit" in verified.stderr


def test_verifier_accepts_utf8_normalization_distinct_from_raw_observed_bytes(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    normalized_tail = "\ufffd" * 4_000
    encoded_tail = normalized_tail.encode("utf-8")
    assert len(encoded_tail) == 12_000
    transcript = (
        b"OpenADA bounded KLayout process transcript\n"
        b"stdout: retained_tail_bytes=12000 observed_bytes=5000 truncated=false\n"
        b"--- stdout tail ---\n"
        + encoded_tail
        + b"\n"
        b"stderr: retained_tail_bytes=0 observed_bytes=0 truncated=false\n"
        b"--- stderr tail ---\n\n"
    )
    _rewrite_native_artifact(
        evidence,
        operation,
        kind="klayout-transcript",
        content=transcript,
    )
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    capture = result["data"]["transcript"]
    capture.update(
        {
            "bytes": len(transcript),
            "sha256": _sha256(transcript),
            "stdout_retained_bytes": 12_000,
            "stdout_observed_bytes": 5_000,
            "stdout_tail": normalized_tail,
        }
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 0, verified.stderr


def test_runner_and_result_bind_the_reviewed_top_cell_argv(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    operation = manifest["operations"]["drc"]
    arguments = operation["arguments"]
    runner_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                f"sys.path.insert(0, {str(BUNDLE)!r}); "
                "import run; "
                f"manifest=json.load(open({str(MANIFEST)!r}, encoding='utf-8')); "
                "print(json.dumps(run._operation_argv('drc', manifest['operations']['drc'])))"
            ),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert runner_probe.returncode == 0, runner_probe.stderr
    assert json.loads(runner_probe.stdout) == [
        "drc",
        arguments["gds"],
        "--rules",
        arguments["rules"],
        "--report",
        arguments["report"],
        "--top-cell",
        "inverter",
        "--timeout",
        "180",
        "--provenance-input",
        "/foss/pdks/ihp-sg13g2/COMMIT",
    ]

    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    result = json.loads(
        (evidence / operation["result_filename"]).read_text(encoding="utf-8")
    )
    assert result["execution"]["command"] == [
        operation["tool_identity"]["path"],
        "-b",
        "-r",
        arguments["rules"],
        "-rd",
        f"input={arguments['gds']}",
        "-rd",
        f"report={arguments['report']}",
        "-rd",
        "topcell=inverter",
    ]
    assert len(result["execution"]["command"]) == 10


def test_runner_and_result_bind_the_reviewed_failing_drc_argv(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    operation = manifest["operations"]["drc_fail"]
    arguments = operation["arguments"]
    runner_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                f"sys.path.insert(0, {str(BUNDLE)!r}); "
                "import run; "
                f"manifest=json.load(open({str(MANIFEST)!r}, encoding='utf-8')); "
                "print(json.dumps(run._operation_argv("
                "'drc_fail', manifest['operations']['drc_fail'])))"
            ),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert runner_probe.returncode == 0, runner_probe.stderr
    assert json.loads(runner_probe.stdout) == [
        "drc",
        arguments["gds"],
        "--rules",
        arguments["rules"],
        "--report",
        arguments["report"],
        "--top-cell",
        "lvs_tester",
        "--timeout",
        "180",
        "--provenance-input",
        "/foss/pdks/ihp-sg13g2/COMMIT",
    ]

    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    result = json.loads(
        (evidence / operation["result_filename"]).read_text(encoding="utf-8")
    )
    assert result["operation"] == "drc"
    assert result["execution"]["command"] == [
        operation["tool_identity"]["path"],
        "-b",
        "-r",
        arguments["rules"],
        "-rd",
        f"input={arguments['gds']}",
        "-rd",
        f"report={arguments['report']}",
        "-rd",
        "topcell=lvs_tester",
    ]


def test_runner_accepts_reviewed_cli_failure_exit_as_engineering_evidence(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "result.json"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,pathlib,sys; "
                f"sys.path.insert(0, {str(BUNDLE)!r}); "
                "import run; "
                "command=[sys.executable,'-c',"
                '"import json,sys; print(json.dumps({\'fixture\': True})); sys.exit(1)"]; '
                f"result=pathlib.Path({str(result_path)!r}); "
                "run._run_operation(command,result,timeout=10,"
                "container_engine='unused',container_name='unused',"
                "expected_returncode=1); "
                "print(json.dumps(json.loads(result.read_text())))"
            ),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == {"fixture": True}


def test_runner_and_result_bind_the_reviewed_lvs_argv_and_pdk_provenance(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    operation = manifest["operations"]["lvs"]
    arguments = operation["arguments"]
    runner_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                f"sys.path.insert(0, {str(BUNDLE)!r}); "
                "import run; "
                f"manifest=json.load(open({str(MANIFEST)!r}, encoding='utf-8')); "
                "print(json.dumps(run._operation_argv('lvs', manifest['operations']['lvs'])))"
            ),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert runner_probe.returncode == 0, runner_probe.stderr
    assert json.loads(runner_probe.stdout) == [
        "lvs",
        arguments["layout_netlist"],
        arguments["schematic_netlist"],
        "--cell",
        arguments["cell"],
        "--setup",
        arguments["setup"],
        "--report",
        arguments["report"],
        "--timeout",
        "180",
        "--provenance-input",
        "/foss/pdks/ihp-sg13g2/COMMIT",
    ]

    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    result = json.loads(
        (evidence / operation["result_filename"]).read_text(encoding="utf-8")
    )
    assert result["execution"]["command"] == [
        operation["tool_identity"]["path"],
        "-batch",
        "lvs",
        f"{arguments['layout_netlist']} {arguments['cell']}",
        f"{arguments['schematic_netlist']} {arguments['cell']}",
        arguments["setup"],
        arguments["report"],
        "-json",
    ]


@pytest.mark.parametrize(
    "tamper", ["temporary-report", "missing-json", "wrong-setup", "extra-argument"]
)
def test_verifier_rejects_any_native_lvs_argv_drift(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    command = result["execution"]["command"]
    if tamper == "temporary-report":
        command[-2] = "/evidence/.openada-netgen-stale/comparison.comp"
    elif tamper == "missing-json":
        command.pop()
    elif tamper == "wrong-setup":
        command[5] = "/evidence/ambient.tcl"
    else:
        command.append("ambient")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "lvs.execution.command differs from the reviewed argv" in verified.stderr


@pytest.mark.parametrize(
    "tamper",
    ["missing-top-cell", "wrong-top-cell", "wrong-report", "extra-variable"],
)
def test_verifier_rejects_any_native_drc_argv_drift(
    tmp_path: Path,
    tamper: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    command = result["execution"]["command"]
    if tamper == "missing-top-cell":
        del command[-2:]
    elif tamper == "wrong-top-cell":
        command[-1] = "topcell=other"
    elif tamper == "wrong-report":
        command[7] = "report=/evidence/stale.lyrdb"
    else:
        command.extend(["-rd", "ambient=1"])
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "drc.execution.command differs from the reviewed argv" in verified.stderr


@pytest.mark.parametrize(
    "path, value, location",
    [
        (("execution", "status"), "failed", "drc.execution.status"),
        (("engineering", "status"), "unknown", "drc.engineering.status"),
        (("diagnostics",), [], "drc.diagnostics"),
        (("data", "drc_clean"), False, "drc.data.drc_clean"),
        (("data", "inputs_stable"), False, "drc.data.inputs_stable"),
        (("data", "changed_inputs"), ["/tampered"], "drc.data.changed_inputs"),
        (("data", "working_directory"), "/tmp", "drc.data.working_directory"),
        (
            ("data", "working_directory_is_sandbox"),
            True,
            "drc.data.working_directory_is_sandbox",
        ),
        (("data", "top_cell"), "other", "drc.data.top_cell"),
        (("data", "deck_variables"), [["ambient", "1"]], "drc.data.deck_variables"),
        (("data", "startup", "database_only"), False, "drc.data.startup"),
        (
            ("data", "transitive_rule_inputs_enumerated"),
            True,
            "drc.data.transitive_rule_inputs_enumerated",
        ),
        (
            ("data", "ambient_environment_enumerated"),
            True,
            "drc.data.ambient_environment_enumerated",
        ),
        (("data", "deck_trust"), "trusted", "drc.data.deck_trust"),
        (("data", "environment"), {}, "drc.data.environment"),
        (
            ("data", "environment", "PDK_ROOT"),
            "/tampered",
            "drc.data.environment",
        ),
        (
            ("data", "report_output", "ownership"),
            "script",
            "drc.data.report_output.ownership",
        ),
        (
            ("data", "report_output", "binding_variable"),
            "other",
            "drc.data.report_output.binding_variable",
        ),
        (
            ("data", "report_output", "fresh_required"),
            False,
            "drc.data.report_output.fresh_required",
        ),
        (
            ("data", "report_output", "parent_anchored"),
            False,
            "drc.data.report_output.parent_anchored",
        ),
        (
            ("data", "report_output", "capture", "origin"),
            "ambient",
            "drc.data.report_output.capture.origin",
        ),
        (
            ("data", "report_output", "capture", "status"),
            "missing",
            "drc.data.report_output.capture.status",
        ),
        (
            ("data", "report_output", "capture", "bytes"),
            1,
            "drc.data.report_output.capture.bytes",
        ),
        (
            ("data", "report_output", "capture", "sha256"),
            "0" * 64,
            "drc.data.report_output.capture.sha256",
        ),
        (
            ("data", "report_output", "capture", "validation", "valid"),
            False,
            "drc.data.report_output.capture.validation",
        ),
        (
            ("data", "report", "validation", "reason"),
            "spoofed",
            "drc.data.report.validation",
        ),
        (("data", "report", "generator"), "spoofed", "drc.data.report.generator"),
        (("data", "report", "top_cell"), "other", "drc.data.report.top_cell"),
        (("data", "report", "category_count"), 0, "drc.data.report.category_count"),
        (("data", "report", "cell_count"), 0, "drc.data.report.cell_count"),
        (("data", "report", "item_count"), 1, "drc.data.report.item_count"),
        (
            ("data", "report", "total_violations"),
            1,
            "drc.data.report.total_violations",
        ),
        (
            ("data", "report", "waived_violations"),
            1,
            "drc.data.report.waived_violations",
        ),
        (("data", "report", "category_counts"), [{}], "drc.data.report.category_counts"),
        (("data", "report", "violations"), [{}], "drc.data.report.violations"),
        (
            ("data", "report", "normalization", "geometry_values"),
            1,
            "drc.data.report.normalization",
        ),
        (
            ("data", "waiver_database", "status"),
            "stable",
            "drc.data.waiver_database",
        ),
        (("data", "transcript", "origin"), "deck", "drc.data.transcript.origin"),
        (("data", "transcript", "status"), "missing", "drc.data.transcript.status"),
        (("data", "transcript", "bytes"), 1, "drc.data.transcript.bytes"),
        (
            ("data", "transcript", "sha256"),
            "0" * 64,
            "drc.data.transcript.sha256",
        ),
        (
            ("data", "transcript", "stdout_observed_bytes"),
            1,
            "drc.data.transcript.stdout_observed_bytes",
        ),
        (("data", "transcript", "stdout_tail"), "spoofed", "drc.data.transcript.stdout_tail"),
        (("data", "transcript", "limitation"), "none", "drc.data.transcript.limitation"),
    ],
    ids=lambda value: str(value)[:80],
)
def test_verifier_binds_every_pinned_drc_trust_field(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    location: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    _tamper_drc_result(evidence, manifest, path, value)

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert location in verified.stderr


def test_verifier_binds_the_declared_pdk_provenance_input(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    revision_path = manifest["runtime"]["pdk"]["revision_file"]
    revision_record = next(item for item in result["inputs"] if item["path"] == revision_path)
    revision_record["sha256"] = "0" * 64
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert f"drc.inputs[{revision_path}].sha256" in verified.stderr


@pytest.mark.parametrize(
    "path, value",
    [
        (("inputs_stable",), False),
        (("changed_inputs",), ["/foss/pdks/ambient"]),
        (("setup_trust",), "trusted"),
        (("transitive_setup_inputs_enumerated",), True),
        (("ambient_environment_enumerated",), True),
        (("report_output", "ownership"), "temporary-copy"),
        (("report_output", "fresh_required"), False),
        (("json_output", "parent_anchored"), False),
        (("comparison", "outcomes_agree"), False),
        (("comparison", "structural_counts_agree"), False),
        (("comparison", "evidence_agrees"), False),
        (("transcript", "assessment", "setup_read"), False),
    ],
    ids=lambda value: str(value)[:80],
)
def test_verifier_binds_every_pinned_lvs_trust_field(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    target = result["data"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "lvs.data" in verified.stderr


def test_verifier_binds_the_declared_lvs_pdk_provenance_input(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["lvs"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    revision_path = manifest["runtime"]["pdk"]["revision_file"]
    revision_record = next(item for item in result["inputs"] if item["path"] == revision_path)
    revision_record["sha256"] = "0" * 64
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert f"lvs.inputs[{revision_path}].sha256" in verified.stderr


def test_verifier_rejects_runtime_identity_tampering(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    operation = manifest["operations"]["drc"]
    result_path = evidence / operation["result_filename"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["tool"]["version"] = "KLayout unreviewed"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "drc.tool.version" in verified.stderr


def test_runner_rejects_evidence_inside_checkout() -> None:
    forbidden = ROOT / "conformance-evidence-must-not-exist"
    assert not forbidden.exists()
    replay = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "run.py"),
            "--evidence-dir",
            str(forbidden),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert replay.returncode == 1
    assert "evidence directory must be outside the OpenADA checkout" in replay.stderr
    assert not forbidden.exists()


def test_runner_rejects_evidence_inside_conformance_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    forbidden = cache / "IHP-AnalogAcademy" / "evidence"
    replay = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "run.py"),
            "--cache-dir",
            str(cache),
            "--evidence-dir",
            str(forbidden),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert replay.returncode == 1
    assert "outside the conformance cache" in replay.stderr
    assert not forbidden.exists()


def test_runner_rejects_evidence_through_symlinked_design_path(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    actual_design = tmp_path / "actual-design"
    actual_design.mkdir()
    design_link = cache / "IHP-AnalogAcademy"
    design_link.symlink_to(actual_design, target_is_directory=True)
    forbidden = design_link / "evidence"
    replay = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "run.py"),
            "--cache-dir",
            str(cache),
            "--evidence-dir",
            str(forbidden),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert replay.returncode == 1
    assert "design checkout path may not be a symbolic link" in replay.stderr
    assert not (actual_design / "evidence").exists()


def test_setup_rejects_symlinked_design_before_pull(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "IHP-AnalogAcademy").symlink_to(ROOT, target_is_directory=True)

    setup = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "setup.py"),
            "--cache-dir",
            str(cache),
            "--container-engine",
            str(tmp_path / "must-not-run"),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert setup.returncode == 1
    assert "design checkout path may not be a symbolic link" in setup.stderr
    assert "cannot execute" not in setup.stderr


@pytest.mark.conformance
@pytest.mark.skipif(
    os.environ.get("OPENADA_RUN_IHP_CONFORMANCE") != "1",
    reason="set OPENADA_RUN_IHP_CONFORMANCE=1 after running the pinned setup",
)
def test_real_pinned_ihp_inverter_conformance(tmp_path: Path) -> None:
    evidence = tmp_path / "real-evidence"
    command = [
        sys.executable,
        str(BUNDLE / "run.py"),
        "--evidence-dir",
        str(evidence),
    ]
    cache_dir = os.environ.get("OPENADA_IHP_CACHE_DIR")
    if cache_dir:
        command.extend(["--cache-dir", cache_dir])
    container_engine = os.environ.get("OPENADA_CONTAINER_ENGINE")
    if container_engine:
        command.extend(["--container-engine", container_engine])

    replay = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert replay.returncode == 0, (
        f"real conformance replay failed\nstdout:\n{replay.stdout}\nstderr:\n{replay.stderr}\n"
        "Run conformance/ihp-inverter/setup.py first."
    )
    verified = subprocess.run(
        [sys.executable, str(VERIFY), str(evidence)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert verified.returncode == 0, verified.stderr


def _publish_semantic_fixture(
    tmp_path: Path,
    evidence: Path,
) -> tuple[Path, Path, Path, subprocess.CompletedProcess[str]]:
    snapshot = tmp_path / "semantic-evidence.json"
    probes = tmp_path / "semantic-probes.json"
    bundle = tmp_path / "semantic-artifacts"
    completed = subprocess.run(
        [
            sys.executable,
            str(SEMANTIC),
            "--native-evidence",
            str(evidence),
            "--bundle-dir",
            str(bundle),
            "--snapshot-output",
            str(snapshot),
            "--probe-output",
            str(probes),
            "--normalized-output",
            str(tmp_path / "semantic-normalized.json"),
            "--oracle-output",
            str(tmp_path / "semantic-oracle.json"),
            "--decision-output",
            str(tmp_path / "semantic-decision.json"),
            "--replay-output-dir",
            str(tmp_path / "semantic-replays"),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return snapshot, probes, bundle, completed


def test_semantic_publication_closes_native_evidence_to_scoped_agent_decisions(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)

    snapshot_path, probes_path, bundle, published = _publish_semantic_fixture(
        tmp_path,
        evidence,
    )

    assert published.returncode == 0, published.stderr
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["chain_id"] == "openada.chain/ihp-inverter-physical/v1"
    assert snapshot["decisions"]["inverter"]["decision"] == "proceed"
    assert snapshot["decisions"]["gallery"]["decision"] == "block"
    failing = snapshot["operations"]["drc_fail"]["evidence"]
    assert failing["total_violations"] == 8
    assert failing["category_counts"] == [
        {"category": "M1.b", "category_path": ["M1.b"], "violations": 6},
        {"category": "Cnt.d", "category_path": ["Cnt.d"], "violations": 1},
        {"category": "Cnt.e", "category_path": ["Cnt.e"], "violations": 1},
    ]
    assert [
        violation["geometries"][0]["coordinates"]
        for violation in failing["violations"]
    ] == DRC_FAIL_COORDINATES
    assert failing["normalization"] == {
        "geometry_values": 8,
        "global_geometry_limit_reached": False,
        "retained_coordinate_pairs": 32,
        "retained_geometries": 8,
    }
    lvs = snapshot["operations"]["lvs_match"]["evidence"]
    assert lvs["lvs_match"] is True
    assert lvs["mismatch_count"] == 0
    assert lvs["report"]["final_match"] is True
    assert lvs["evidence_agrees"] is True
    assert snapshot["standards"]["ieee_measurement_standard"]["status"] == (
        "not-applicable"
    )
    assert {item["id"] for item in snapshot["limitations"]} >= {
        "pre-extracted-layout-netlist",
        "reference-flow-not-signoff",
        "separate-negative-fixture",
    }

    probes = json.loads(probes_path.read_text(encoding="utf-8"))
    assert probes["summary"] == {
        "all_required_diagnostics_observed": True,
        "probe_count": 4,
        "status": "pass",
    }
    assert [record["id"] for record in probes["negative_replays"]] == [
        "real-gallery-drc-fail",
        "synthetic-native-lvs-mismatch",
    ]
    assert [record["id"] for record in probes["tamper_replays"]] == [
        "reconciled-seven-item-drc",
        "unbound-native-lvs-json",
    ]

    expected_bundle = {"run.json"}
    for operation in manifest["operations"].values():
        expected_bundle.add(operation["result_filename"])
        for key in ("artifact", "json_artifact", "transcript_artifact"):
            if key in operation:
                expected_bundle.add(operation[key]["filename"])
    assert {path.name for path in bundle.iterdir()} == expected_bundle
    for filename in expected_bundle:
        assert (bundle / filename).read_bytes() == (evidence / filename).read_bytes()

    offline = subprocess.run(
        [
            sys.executable,
            str(SEMANTIC),
            str(snapshot_path),
            "--probe-report",
            str(probes_path),
            "--bundle-dir",
            str(bundle),
            "--normalized-output",
            str(tmp_path / "semantic-normalized.json"),
            "--oracle-output",
            str(tmp_path / "semantic-oracle.json"),
            "--decision-output",
            str(tmp_path / "semantic-decision.json"),
            "--replay-output-dir",
            str(tmp_path / "semantic-replays"),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert offline.returncode == 0, offline.stderr
    supporting_paths = [
        tmp_path / "semantic-normalized.json",
        tmp_path / "semantic-oracle.json",
        tmp_path / "semantic-decision.json",
        *sorted((tmp_path / "semantic-replays").glob("*.json")),
    ]
    assert len(supporting_paths) == 7
    assert len({_sha256(path.read_bytes()) for path in supporting_paths}) == 7

    fresh_compare = subprocess.run(
        [
            sys.executable,
            str(SEMANTIC),
            str(snapshot_path),
            "--native-evidence",
            str(evidence),
            "--bundle-dir",
            str(bundle),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert fresh_compare.returncode == 0, fresh_compare.stderr


@pytest.mark.parametrize(
    "path,value,diagnostic",
    [
        (
            ("decisions", "gallery", "decision"),
            "proceed",
            "semantic evidence.decisions",
        ),
        (
            (
                "operations",
                "drc_fail",
                "evidence",
                "violations",
                0,
                "geometries",
                0,
                "coordinates",
                0,
                0,
            ),
            99.0,
            "semantic evidence.operations.drc_fail.evidence.violations",
        ),
        (
            ("operations", "lvs_match", "evidence", "lvs_match"),
            False,
            "semantic evidence.operations.lvs_match.evidence",
        ),
        (
            (
                "operations",
                "drc_clean",
                "retained_artifacts",
                1,
                "sha256",
            ),
            "0" * 64,
            "semantic evidence.trust_chain",
        ),
    ],
    ids=["decision", "geometry", "lvs-match", "published-artifact-digest"],
)
def test_semantic_offline_verifier_rejects_agent_evidence_tamper(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
    diagnostic: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    snapshot_path, probes_path, bundle, published = _publish_semantic_fixture(
        tmp_path,
        evidence,
    )
    assert published.returncode == 0, published.stderr
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    target: object = snapshot
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    verified = subprocess.run(
        [
            sys.executable,
            str(SEMANTIC),
            str(snapshot_path),
            "--probe-report",
            str(probes_path),
            "--bundle-dir",
            str(bundle),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert verified.returncode == 1
    assert diagnostic in verified.stderr


def test_semantic_publication_rejects_unverified_native_input_before_writing(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence"
    _write_synthetic_evidence(evidence, manifest)
    native_json = evidence / manifest["operations"]["lvs"]["json_artifact"]["filename"]
    native_json.write_bytes(native_json.read_bytes() + b" ")

    snapshot_path, probes_path, bundle, published = _publish_semantic_fixture(
        tmp_path,
        evidence,
    )

    assert published.returncode == 1
    assert "lvs.netgen-comparison-json.bytes" in published.stderr
    assert not snapshot_path.exists()
    assert not probes_path.exists()
    assert not bundle.exists()
