#!/usr/bin/env python3
"""Publish and verify agent-facing evidence for the pinned IHP SAR RTL chain."""

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

from common import ConformanceError, load_manifest, sha256_file
import verify as independent_verifier

from jsonschema import Draft202012Validator, FormatChecker


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
CHAIN_ID = "openada.chain/ihp-sar-rtl/v1"
STRUCTURAL_ROWS = [
    "surface|openada.surface/cli.rtl-check/v1",
    "preflight|rtl-structural-check-passes",
]
LINT_ROWS = [
    "surface|openada.surface/cli.rtl-lint/v1",
    "preflight|rtl-lint-clean",
    "profile|openada.operation/rtl.lint/v1alpha1",
    "assertion|openada.operation/rtl.lint/v1alpha1|openada.assertion/rtl.lint.clean/v1alpha1",
    "feature|openada.operation/rtl.lint/v1alpha1|openada.feature/rtl.lint.systemverilog/v1alpha1",
    "native-mapping|openada.operation/rtl.lint/v1alpha1|org.openada.driver.verilator|org.verilator.verilator|openada.feature/rtl.lint.systemverilog/v1alpha1",
    "provider|org.openada.driver.verilator|openada.operation/rtl.lint/v1alpha1|openada.feature/rtl.lint.systemverilog/v1alpha1",
]
ROWS = [*STRUCTURAL_ROWS, *LINT_ROWS]
NATIVE_FILES = [
    "design-provenance.json",
    "positive/rtl-check.result.json",
    "positive/rtl-check.ys",
    "positive/sar_logic.json",
    "positive/yosys.transcript.json",
    "negative/rtl-check.result.json",
    "negative/rtl-check.ys",
    "negative/yosys.transcript.json",
    "positive/rtl-lint.result.json",
    "positive/rtl-lint.log",
    "positive-2023/rtl-lint.result.json",
    "positive-2023/rtl-lint.log",
    "negative/rtl-lint.result.json",
    "negative/rtl-lint.log",
    "run.json",
]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402
NEGATIVE_ID = "real-missing-top"
TAMPER_ID = "reconciled-json-port-removal"
TAMPER_DIAGNOSTIC = "positive Yosys JSON ports"
LINT_NEGATIVE_ID = "real-verilator-missing-top"
LINT_TAMPER_ID = "reconciled-lint-log-finding-injection"
LINT_TAMPER_DIAGNOSTIC = "native Verilator transcript diagnostics"
LIMITATIONS = [
    {
        "id": "structural-elaboration-only",
        "impact": (
            "This proves pinned Yosys elaboration and check -assert structural checks; "
            "it does not prove cycle-accurate behavior, formal properties, or mixed-signal behavior."
        ),
    },
    {
        "id": "strict-lint-only",
        "impact": (
            "Strict Verilator lint under the pinned 1800-2017 and 1800-2023 selectors proves "
            "only that those requests emitted no warning or error. It is not proof of "
            "functional correctness, CDC safety, timing closure, or physical correctness."
        ),
    },
    {
        "id": "no-timing-or-physical-closure",
        "impact": (
            "No synthesis constraints, static timing analysis, clock-domain analysis, "
            "place-and-route, power analysis, DRC, or LVS is part of this RTL chain."
        ),
    },
    {
        "id": "implementation-specific-language-support",
        "impact": (
            "The source is parsed with Yosys read_verilog -sv and linted with pinned Verilator "
            "selectors for 1800-2017 and 1800-2023. The chain records those implementation "
            "behaviors and is not a certification of complete IEEE 1800-2023 support."
        ),
    },
    {
        "id": "real-negative-is-missing-top",
        "impact": (
            "The native negative replay proves an invalid top request is blocked; it is "
            "not a mutation of the reviewed SAR source and does not weaken its proceed decision."
        ),
    },
    {
        "id": "dirty-checkout-recorded",
        "impact": (
            "The run binds the OpenADA checkout state before and after execution. A dirty "
            "but unchanged checkout is reproducible evidence, not a commit-exact release receipt."
        ),
    },
]


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _read_json(path: Path, *, label: str, maximum: int = 64 * 1024 * 1024) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum:
        raise ConformanceError(f"{label} has an invalid size: {path}")
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
        json.dumps(document, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
        raise ConformanceError(f"publication artifact is missing: {path}")
    return {
        "repository_path": repository_path,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _bundle_record(relative: str) -> dict[str, Any]:
    return _file_record(
        BUNDLE / relative,
        f"conformance/ihp-sar-rtl/semantic-artifacts/{relative}",
    )


def _copy_verified_evidence(evidence: Path) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".ihp-sar-artifacts-", dir=HERE))
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
    matches = [record for record in run["native_artifacts"] if record.get("path") == relative]
    if len(matches) != 1:
        raise ConformanceError(f"tamper fixture cannot find one run artifact record for {relative}")
    path = evidence / relative
    matches[0]["bytes"] = path.stat().st_size
    matches[0]["sha256"] = sha256_file(path)


def _run_tamper_probe(manifest: dict[str, Any], evidence: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openada-ihp-sar-tamper-") as temporary:
        tampered = Path(temporary) / "evidence"
        shutil.copytree(evidence, tampered)
        netlist_path = tampered / "positive/sar_logic.json"
        netlist = _read_json(netlist_path, label="tamper Yosys JSON")
        del netlist["modules"]["sar_logic"]["ports"]["rst"]
        _write_json(netlist_path, netlist)

        result_path = tampered / "positive/rtl-check.result.json"
        result = _read_json(result_path, label="tamper positive result")
        records = [
            item
            for item in result["artifacts"]
            if item.get("path") == "/evidence/positive/sar_logic.json"
        ]
        if len(records) != 1:
            raise ConformanceError("tamper fixture cannot find the positive Yosys JSON record")
        records[0]["bytes"] = netlist_path.stat().st_size
        records[0]["sha256"] = sha256_file(netlist_path)
        _write_json(result_path, result)

        run_path = tampered / "run.json"
        run = _read_json(run_path, label="tamper run metadata")
        _replace_run_record(run, tampered, "positive/sar_logic.json")
        _replace_run_record(run, tampered, "positive/rtl-check.result.json")
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
            raise ConformanceError("independent verifier accepted reconciled port-removal tampering")
    if TAMPER_DIAGNOSTIC not in diagnostic:
        raise ConformanceError(
            f"tamper probe failed for the wrong reason: expected {TAMPER_DIAGNOSTIC!r}, got {diagnostic!r}"
        )
    return {
        "id": TAMPER_ID,
        "status": "rejected",
        "expected_status": "unknown",
        "covers": STRUCTURAL_ROWS,
        "mutation": (
            "removed the rst port from native Yosys JSON and reconciled both the OpenADA "
            "artifact digest and run-level artifact digests"
        ),
        "required_diagnostic": TAMPER_DIAGNOSTIC,
        "observed_diagnostic": diagnostic,
        "verifier": "conformance/ihp-sar-rtl/verify.py",
    }


def _run_lint_tamper_probe(manifest: dict[str, Any], evidence: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="openada-ihp-sar-lint-tamper-") as temporary:
        tampered = Path(temporary) / "evidence"
        shutil.copytree(evidence, tampered)
        log_path = tampered / "positive/rtl-lint.log"
        try:
            body = log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConformanceError(f"cannot read lint tamper transcript: {exc}") from exc
        injected = "%Warning-LATCH: injected reconciled native finding\n"
        if "stderr_bytes: 0\n" not in body or not body.endswith("--- stderr ---\n"):
            raise ConformanceError("lint tamper fixture is not the reviewed clean transcript")
        body = body.replace(
            "stderr_bytes: 0\n",
            f"stderr_bytes: {len(injected.encode('utf-8'))}\n",
            1,
        )
        log_path.write_text(body + injected, encoding="utf-8")

        result_path = tampered / "positive/rtl-lint.result.json"
        result = _read_json(result_path, label="tamper positive lint result")
        records = [
            item
            for item in result["artifacts"]
            if item.get("path") == "/evidence/positive/rtl-lint.log"
        ]
        if len(records) != 1:
            raise ConformanceError("lint tamper fixture cannot find the positive log record")
        records[0]["bytes"] = log_path.stat().st_size
        records[0]["sha256"] = sha256_file(log_path)
        _write_json(result_path, result)

        run_path = tampered / "run.json"
        run = _read_json(run_path, label="lint tamper run metadata")
        _replace_run_record(run, tampered, "positive/rtl-lint.log")
        _replace_run_record(run, tampered, "positive/rtl-lint.result.json")
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
            raise ConformanceError("independent verifier accepted reconciled lint-log tampering")
    if LINT_TAMPER_DIAGNOSTIC not in diagnostic:
        raise ConformanceError(
            "lint tamper probe failed for the wrong reason: "
            f"expected {LINT_TAMPER_DIAGNOSTIC!r}, got {diagnostic!r}"
        )
    return {
        "id": LINT_TAMPER_ID,
        "status": "rejected",
        "expected_status": "unknown",
        "covers": LINT_ROWS,
        "mutation": (
            "injected a native Verilator latch warning into the clean lint transcript and "
            "reconciled both the OpenADA artifact digest and run-level artifact digests"
        ),
        "required_diagnostic": LINT_TAMPER_DIAGNOSTIC,
        "observed_diagnostic": diagnostic,
        "verifier": "conformance/ihp-sar-rtl/verify.py",
    }


def _negative_replay(verified: dict[str, Any]) -> dict[str, Any]:
    negative = verified["negative"]
    return {
        "id": NEGATIVE_ID,
        "status": "observed",
        "expected_status": "fail",
        "covers": STRUCTURAL_ROWS,
        "fixture": "the pinned source with a real native Yosys request for missing_sar_logic",
        "execution_status": negative["result"]["execution"]["status"],
        "exit_code": negative["result"]["execution"]["exit_code"],
        "engineering_status": negative["result"]["engineering"]["status"],
        "required_diagnostic": negative["diagnostic"],
        "observed_diagnostic": negative["diagnostic"],
        "native_transcript_bound": True,
    }


def _lint_negative_replay(verified: dict[str, Any]) -> dict[str, Any]:
    negative = verified["lint_negative"]
    messages = [item["message"] for item in negative["transcript"]["diagnostics"]]
    return {
        "id": LINT_NEGATIVE_ID,
        "status": "observed",
        "expected_status": "fail",
        "covers": LINT_ROWS,
        "fixture": "the pinned source with a real native Verilator request for missing_sar_logic",
        "execution_status": negative["result"]["execution"]["status"],
        "exit_code": negative["result"]["execution"]["exit_code"],
        "engineering_status": negative["result"]["engineering"]["status"],
        "required_diagnostic": messages[0],
        "observed_diagnostic": messages[0],
        "observed_diagnostics": messages,
        "native_transcript_bound": True,
    }


def _native_records() -> list[dict[str, Any]]:
    return [_bundle_record(relative) for relative in NATIVE_FILES]


def _normalized_lint_operation(
    verified: dict[str, Any],
    *,
    verified_key: str,
    result_relative: str,
    log_relative: str,
) -> dict[str, Any]:
    result = verified[verified_key]["result"]
    return {
        "surface_id": "openada.surface/cli.rtl-lint/v1",
        "operation_profile": "openada.operation/rtl.lint/v1alpha1",
        "assertion_profile": "openada.assertion/rtl.lint.clean/v1alpha1",
        "assertion": "rtl-lint-clean",
        "execution_status": result["execution"]["status"],
        "exit_code": result["execution"]["exit_code"],
        "engineering_status": result["engineering"]["status"],
        "summary": result["engineering"]["summary"],
        "evidence": copy.deepcopy(result["data"]),
        "native_artifacts": [
            _bundle_record(result_relative),
            _bundle_record(log_relative),
        ],
    }


def _contract_document() -> dict[str, Any]:
    suite = REPOSITORY_ROOT / "tests/test_ihp_sar_rtl_conformance.py"
    tree = ast.parse(suite.read_text(encoding="utf-8"), filename=str(suite))
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
            "suite": "tests/test_ihp_sar_rtl_conformance.py",
            "suite_sha256": sha256_file(suite),
            "passed": test_count,
            "failed": 0,
            "execution": "fresh pytest exit status zero",
        },
        "native_replay": {
            "independent_verifier": "pass",
            "real_negative": "pass",
            "tamper_probe": "pass",
            "real_lint_negative": "pass",
            "clean_lint_2023": "pass",
            "lint_tamper_probe": "pass",
        },
    }


def _run_contract_tests() -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_ihp_sar_rtl_conformance.py",
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
            "SAR RTL contract tests failed: " + completed.stdout[-4000:]
        )
    return _contract_document()


def _publish(evidence: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    if not (evidence / "design-provenance.json").is_file():
        raise ConformanceError(
            "release publication requires retained public-design provenance"
        )
    release_run = _read_json(evidence / "run.json", label="release native run")
    if "source_attestation" not in release_run:
        raise ConformanceError(
            "release publication requires a source-attested native replay"
        )
    verified = independent_verifier.verify_evidence(
        manifest,
        evidence,
        manifest_sha256=sha256_file(MANIFEST_PATH),
    )
    tamper = _run_tamper_probe(manifest, evidence)
    lint_tamper = _run_lint_tamper_probe(manifest, evidence)
    negative = _negative_replay(verified)
    lint_negative = _lint_negative_replay(verified)
    _copy_verified_evidence(evidence)

    if REPLAY_DIR.exists():
        if REPLAY_DIR.is_symlink() or not REPLAY_DIR.is_dir():
            raise ConformanceError(f"refusing to replace unsafe replay path: {REPLAY_DIR}")
        shutil.rmtree(REPLAY_DIR)
    REPLAY_DIR.mkdir()
    negative_path = REPLAY_DIR / f"{NEGATIVE_ID}.json"
    tamper_path = REPLAY_DIR / f"{TAMPER_ID}.json"
    lint_negative_path = REPLAY_DIR / f"{LINT_NEGATIVE_ID}.json"
    lint_tamper_path = REPLAY_DIR / f"{LINT_TAMPER_ID}.json"
    _write_json(
        negative_path,
        {"schema": "openada.semantic-replay-verdict/v0alpha1", "chain_id": CHAIN_ID, **negative},
    )
    _write_json(
        tamper_path,
        {"schema": "openada.semantic-replay-verdict/v0alpha1", "chain_id": CHAIN_ID, **tamper},
    )
    _write_json(
        lint_negative_path,
        {
            "schema": "openada.semantic-replay-verdict/v0alpha1",
            "chain_id": CHAIN_ID,
            **lint_negative,
        },
    )
    _write_json(
        lint_tamper_path,
        {
            "schema": "openada.semantic-replay-verdict/v0alpha1",
            "chain_id": CHAIN_ID,
            **lint_tamper,
        },
    )

    structure = verified["positive"]["structure"]
    run = verified["run"]
    native_records = _native_records()
    oracle = {
        "schema": "openada.rtl-independent-oracle/v0alpha1",
        "chain_id": CHAIN_ID,
        "verdict": "pass",
        "verifier": {
            "repository_path": "conformance/ihp-sar-rtl/verify.py",
            "sha256": sha256_file(HERE / "verify.py"),
            "imports_openada": False,
        },
        "bindings": {
            "manifest_sha256": sha256_file(MANIFEST_PATH),
            "run_sha256": sha256_file(BUNDLE / "run.json"),
            "native_artifacts": native_records,
        },
        "positive_facts": copy.deepcopy(structure),
        "positive_lint_facts": {
            "top": "sar_logic",
            "language": "1800-2017",
            "engineering_status": verified["lint_positive"]["result"]["engineering"]["status"],
            "warning_policy": "strict",
            "environment_policy": "closed-verilator-runtime-v1",
            "warning_count": 0,
            "error_count": 0,
            "diagnostic_count": 0,
        },
        "positive_lint_2023_facts": {
            "top": "sar_logic",
            "language": "1800-2023",
            "engineering_status": verified["lint_positive_2023"]["result"]["engineering"]["status"],
            "warning_policy": "strict",
            "environment_policy": "closed-verilator-runtime-v1",
            "warning_count": 0,
            "error_count": 0,
            "diagnostic_count": 0,
        },
        "negative_facts": {
            "top": "missing_sar_logic",
            "engineering_status": "fail",
            "exit_code": 1,
            "diagnostic": negative["observed_diagnostic"],
        },
        "tamper_facts": {
            "id": TAMPER_ID,
            "status": "rejected",
            "diagnostic": tamper["observed_diagnostic"],
        },
        "negative_lint_facts": {
            "top": "missing_sar_logic",
            "engineering_status": "fail",
            "exit_code": 1,
            "diagnostic": lint_negative["observed_diagnostic"],
        },
        "lint_tamper_facts": {
            "id": LINT_TAMPER_ID,
            "status": "rejected",
            "diagnostic": lint_tamper["observed_diagnostic"],
        },
    }
    _write_json(ORACLE_PATH, oracle)

    normalized = {
        "schema": "openada.rtl-normalized-evidence/v0alpha1",
        "chain_id": CHAIN_ID,
        "coverage": ROWS,
        "design": {
            "repository": manifest["design"]["repository"],
            "revision": manifest["design"]["revision"],
            "source": {
                "path": manifest["source"]["repository_path"],
                "bytes": manifest["source"]["bytes"],
                "sha256": manifest["source"]["sha256"],
                "top": "sar_logic",
            },
        },
        "runtime": {
            "image_reference": manifest["runtime"]["image"]["reference"],
            "image_config_digest": manifest["runtime"]["image"]["config_digest"],
            "tool": copy.deepcopy(manifest["runtime"]["tool"]),
            "lint_tool": copy.deepcopy(manifest["runtime"]["lint_tool"]),
            "eda_network": "none",
        },
        "operation": {
            "surface_id": "openada.surface/cli.rtl-check/v1",
            "assertion": "rtl-structural-check-passes",
            "execution_status": verified["positive"]["result"]["execution"]["status"],
            "exit_code": verified["positive"]["result"]["execution"]["exit_code"],
            "engineering_status": verified["positive"]["result"]["engineering"]["status"],
            "summary": verified["positive"]["result"]["engineering"]["summary"],
            "evidence": copy.deepcopy(structure),
            "native_artifacts": native_records,
        },
        "lint_operation": _normalized_lint_operation(
            verified,
            verified_key="lint_positive",
            result_relative="positive/rtl-lint.result.json",
            log_relative="positive/rtl-lint.log",
        ),
        "lint_2023_operation": _normalized_lint_operation(
            verified,
            verified_key="lint_positive_2023",
            result_relative="positive-2023/rtl-lint.result.json",
            log_relative="positive-2023/rtl-lint.log",
        ),
        "independent_oracle": _file_record(
            ORACLE_PATH, "conformance/ihp-sar-rtl/semantic-oracle.json"
        ),
        "run": {
            "created_at": run["created_at"],
            "checkout_state_unchanged": run["openada_checkout"]["state_unchanged"],
            "checkout_commit_exact": run["openada_checkout"]["commit_exact"],
        },
    }
    _write_json(NORMALIZED_PATH, normalized)

    decision = {
        "schema": "openada.rtl-engineering-decision/v0alpha1",
        "chain_id": CHAIN_ID,
        "decision": "proceed",
        "scope": "continue to behavioral, formal, timing, and mixed-signal verification; not tapeout signoff",
        "basis": [
            "the pinned public source elaborated as exactly one sar_logic top module",
            "the exact five-input and three-output interface is retained, including 8-bit B, BN, and D",
            "B is structurally aliased to D; counter, BN, and D map to three state elements with widths 4, 8, and 8",
            "Yosys check -assert completed with no warnings or errors and a parsed native JSON netlist",
            "strict Verilator lint completed with zero warnings and zero errors on the same pinned source and top under both 1800-2017 and 1800-2023 selectors",
            "an independent parser bound the source, script, result, transcript, JSON structure, image, and tool",
            "real Yosys and Verilator missing-top requests failed with the required native diagnostics",
            "reconciled native-JSON port tampering and native lint-log finding injection were rejected by the independent oracle",
        ],
        "next_checks": [
            "run directed and randomized RTL simulation against SAR sequencing requirements",
            "add formal assertions for reset, counter termination, and B/D/BN relationships",
            "review reset and clock-domain behavior at the mixed-signal integration boundary",
            "synthesize with target constraints and run static timing and implementation checks",
        ],
        "block_conditions": [
            "any source, image, wrapper, tool, script, native artifact, or manifest digest differs",
            "the independent oracle no longer finds the exact interface and reviewed state structure",
            "native Yosys reports warnings, errors, black boxes, missing JSON, or nonzero exit",
            "strict Verilator lint under either reviewed language selector reports any warning or error, or either bounded transcript is incomplete",
        ],
        "evidence": {
            "normalized": _file_record(
                NORMALIZED_PATH, "conformance/ihp-sar-rtl/semantic-normalized.json"
            ),
            "oracle": _file_record(ORACLE_PATH, "conformance/ihp-sar-rtl/semantic-oracle.json"),
            "negative_replay": _file_record(
                negative_path,
                f"conformance/ihp-sar-rtl/semantic-replays/{NEGATIVE_ID}.json",
            ),
            "tamper_replay": _file_record(
                tamper_path,
                f"conformance/ihp-sar-rtl/semantic-replays/{TAMPER_ID}.json",
            ),
            "lint_negative_replay": _file_record(
                lint_negative_path,
                f"conformance/ihp-sar-rtl/semantic-replays/{LINT_NEGATIVE_ID}.json",
            ),
            "lint_tamper_replay": _file_record(
                lint_tamper_path,
                f"conformance/ihp-sar-rtl/semantic-replays/{LINT_TAMPER_ID}.json",
            ),
        },
        "limitations": LIMITATIONS,
    }
    _write_json(DECISION_PATH, decision)

    evidence_document = {
        "schema": "openada.rtl-agent-evidence/v0alpha1",
        "chain_id": CHAIN_ID,
        "decision": decision["decision"],
        "scope": decision["scope"],
        "basis": copy.deepcopy(decision["basis"]),
        "next_checks": copy.deepcopy(decision["next_checks"]),
        "operations": {
            "rtl_structural_check": copy.deepcopy(normalized["operation"]),
            "rtl_lint": copy.deepcopy(normalized["lint_operation"]),
            "rtl_lint_2023": copy.deepcopy(normalized["lint_2023_operation"]),
            "missing_top_negative": {
                "decision": "block",
                "engineering_status": "fail",
                "exit_code": 1,
                "diagnostic": negative["observed_diagnostic"],
            },
            "lint_missing_top_negative": {
                "decision": "block",
                "engineering_status": "fail",
                "exit_code": 1,
                "diagnostic": lint_negative["observed_diagnostic"],
            },
        },
        "replays": {
            "negative_replays": [negative, lint_negative],
            "tamper_replays": [tamper, lint_tamper],
        },
        "trust_chain": {
            "oracle": _file_record(ORACLE_PATH, "conformance/ihp-sar-rtl/semantic-oracle.json"),
            "normalized": _file_record(
                NORMALIZED_PATH, "conformance/ihp-sar-rtl/semantic-normalized.json"
            ),
            "decision": _file_record(
                DECISION_PATH, "conformance/ihp-sar-rtl/semantic-decision.json"
            ),
            "native_artifacts": native_records,
        },
        "limitations": LIMITATIONS,
        "standards": {
            "ieee_measurement_standard": {
                "status": "not-applicable",
                "basis": "This chain performs RTL elaboration, structural checks, and lint; it computes no electrical or signal-quality measurement such as SNR.",
            },
            "hdl_language": {
                "standard": "IEEE 1800-2023",
                "official_url": "https://standards.ieee.org/ieee/1800/7743/",
                "status": "context-only",
                "basis": "The drivers invoke Yosys read_verilog -sv and independently replay Verilator with declared 1800-2017 and 1800-2023 selectors; the evidence certifies only those pinned implementations and not full language-standard compliance.",
            },
        },
    }
    _write_json(EVIDENCE_PATH, evidence_document)
    _write_json(
        PROBES_PATH,
        {
            "schema": "openada.semantic-chain-probes/v0alpha1",
            "chain_id": CHAIN_ID,
            "status": "pass",
            "negative_replays": [negative, lint_negative],
            "tamper_replays": [tamper, lint_tamper],
        },
    )
    contract_document = _contract_document()
    _write_json(CONTRACT_PATH, contract_document)
    _write_chain_run(run["source_attestation"])
    _expect(_run_contract_tests(), contract_document, "fresh contract-test report")
    verify_publication()


def _verify_file_record(record: Any, expected_path: Path, repository_path: str) -> None:
    if not isinstance(record, dict):
        raise ConformanceError(f"trust record for {repository_path} must be an object")
    _expect(record.get("repository_path"), repository_path, f"{repository_path}.repository_path")
    _expect(record.get("bytes"), expected_path.stat().st_size, f"{repository_path}.bytes")
    _expect(record.get("sha256"), sha256_file(expected_path), f"{repository_path}.sha256")


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


def _chain_artifact_definitions() -> list[tuple[Path, str, str | None, str | None, str | None]]:
    definitions: list[
        tuple[Path, str, str | None, str | None, str | None]
    ] = [
        (CONTRACT_PATH, "contract-test", "contract-tests", "contract-test-verdict", None),
        (
            BUNDLE / "design-provenance.json",
            "design-provenance",
            "materialize-pinned-sources",
            "design-provenance",
            None,
        ),
        (
            BUNDLE / "positive/rtl-check.result.json",
            "native-artifact",
            "rtl-structural-check",
            "positive-rtl-result",
            None,
        ),
        (
            BUNDLE / "positive/rtl-check.ys",
            "native-artifact",
            "rtl-structural-check",
            "positive-yosys-script",
            None,
        ),
        (
            BUNDLE / "positive/sar_logic.json",
            "native-artifact",
            "rtl-structural-check",
            "positive-yosys-json",
            None,
        ),
        (
            BUNDLE / "positive/yosys.transcript.json",
            "native-artifact",
            "rtl-structural-check",
            "positive-yosys-transcript",
            None,
        ),
        (
            BUNDLE / "negative/rtl-check.result.json",
            "native-artifact",
            "missing-top-native",
            "negative-rtl-result",
            None,
        ),
        (
            BUNDLE / "negative/rtl-check.ys",
            "native-artifact",
            "missing-top-native",
            "negative-yosys-script",
            None,
        ),
        (
            BUNDLE / "negative/yosys.transcript.json",
            "native-artifact",
            "missing-top-native",
            "negative-yosys-transcript",
            None,
        ),
        (
            BUNDLE / "positive/rtl-lint.result.json",
            "native-artifact",
            "rtl-lint-clean",
            "positive-lint-result",
            None,
        ),
        (
            BUNDLE / "positive/rtl-lint.log",
            "native-artifact",
            "rtl-lint-clean",
            "positive-verilator-transcript",
            None,
        ),
        (
            BUNDLE / "positive-2023/rtl-lint.result.json",
            "native-artifact",
            "rtl-lint-clean-2023",
            "positive-lint-2023-result",
            None,
        ),
        (
            BUNDLE / "positive-2023/rtl-lint.log",
            "native-artifact",
            "rtl-lint-clean-2023",
            "positive-verilator-2023-transcript",
            None,
        ),
        (
            BUNDLE / "negative/rtl-lint.result.json",
            "native-artifact",
            "lint-missing-top-native",
            "negative-lint-result",
            None,
        ),
        (
            BUNDLE / "negative/rtl-lint.log",
            "native-artifact",
            "lint-missing-top-native",
            "negative-verilator-transcript",
            None,
        ),
        (
            BUNDLE / "run.json",
            "native-artifact",
            "rtl-structural-check",
            "native-run-receipt",
            None,
        ),
        (ORACLE_PATH, "independent-oracle", "independent-verifier", "independent-oracle-verdict", None),
        (NORMALIZED_PATH, "normalized-evidence", "normalize-rtl-evidence", "normalized-rtl-evidence", None),
        (DECISION_PATH, "downstream-decision", "engineering-decision", "downstream-rtl-decision", None),
        (EVIDENCE_PATH, "agent-visible-evidence", "agent-evidence", "agent-visible-rtl-evidence", None),
        (
            REPLAY_DIR / f"{NEGATIVE_ID}.json",
            "negative-replay",
            None,
            None,
            NEGATIVE_ID,
        ),
        (
            REPLAY_DIR / f"{TAMPER_ID}.json",
            "tamper-replay",
            None,
            None,
            TAMPER_ID,
        ),
        (
            REPLAY_DIR / f"{LINT_NEGATIVE_ID}.json",
            "negative-replay",
            None,
            None,
            LINT_NEGATIVE_ID,
        ),
        (
            REPLAY_DIR / f"{LINT_TAMPER_ID}.json",
            "tamper-replay",
            None,
            None,
            LINT_TAMPER_ID,
        ),
    ]
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
            "org.openada.rtl": {
                "receipt_status": source_receipt["receipt_class"],
                "standards_scope": "IEEE 1800-2023 context only; no language conformance claim",
            }
        },
    }
    _write_json(CHAIN_RUN_PATH, document)


def _verify_chain_run() -> None:
    manifest = _read_json(CHAIN_MANIFEST_PATH, label="semantic chain manifest")
    run = _read_json(CHAIN_RUN_PATH, label="semantic chain run")
    for document, schema_path, label in (
        (
            manifest,
            REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json",
            "semantic chain manifest",
        ),
        (
            run,
            REPOSITORY_ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json",
            "semantic chain run",
        ),
    ):
        schema = _read_json(schema_path, label=f"{label} schema")
        errors = sorted(
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ConformanceError(f"{label} violates its schema: {errors[0].message}")
    _expect(run["chain_id"], manifest["id"], "chain run ID")
    _expect(
        run["chain_manifest_sha256"],
        sha256_file(CHAIN_MANIFEST_PATH),
        "chain run manifest digest",
    )
    subject = semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )
    _expect(run["semantic_subject_sha256"], subject, "chain run semantic subject")
    _expect(
        run["source_attestation"]["semantic_subject_sha256"],
        subject,
        "chain run source subject",
    )
    _expect(
        run["source_attestation"]["state_unchanged"],
        True,
        "chain run source state",
    )
    expected = _chain_artifact_definitions()
    _expect(len(run["artifacts"]), len(expected), "chain run artifact count")
    for index, (record, definition) in enumerate(
        zip(run["artifacts"], expected, strict=True)
    ):
        path, role, step, output, replay = definition
        expected_record = _chain_artifact(path, role, step, output, replay)
        _expect(record, expected_record, f"chain run artifact {index}")


def verify_publication() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    verified = independent_verifier.verify_evidence(
        manifest, BUNDLE, manifest_sha256=sha256_file(MANIFEST_PATH)
    )
    tamper = _run_tamper_probe(manifest, BUNDLE)
    lint_tamper = _run_lint_tamper_probe(manifest, BUNDLE)
    oracle = _read_json(ORACLE_PATH, label="semantic oracle")
    normalized = _read_json(NORMALIZED_PATH, label="semantic normalized evidence")
    decision = _read_json(DECISION_PATH, label="semantic downstream decision")
    agent = _read_json(EVIDENCE_PATH, label="semantic agent evidence")
    probes = _read_json(PROBES_PATH, label="semantic probes")
    contract = _read_json(CONTRACT_PATH, label="semantic contract")
    for label, document in (
        ("oracle", oracle), ("normalized", normalized), ("decision", decision),
        ("agent", agent), ("probes", probes), ("contract", contract),
    ):
        _expect(document.get("chain_id"), CHAIN_ID, f"{label}.chain_id")
    _expect(oracle.get("verdict"), "pass", "oracle.verdict")
    _expect(normalized.get("coverage"), ROWS, "normalized.coverage")
    _expect(normalized["operation"].get("engineering_status"), "pass", "normalized operation status")
    _expect(
        normalized["lint_operation"].get("engineering_status"),
        "pass",
        "normalized lint operation status",
    )
    _expect(
        normalized["lint_operation"]["evidence"].get("language"),
        "1800-2017",
        "normalized 2017 lint language",
    )
    _expect(
        normalized["lint_2023_operation"].get("engineering_status"),
        "pass",
        "normalized 2023 lint operation status",
    )
    _expect(
        normalized["lint_2023_operation"]["evidence"].get("language"),
        "1800-2023",
        "normalized 2023 lint language",
    )
    _expect(decision.get("decision"), "proceed", "decision.decision")
    _expect(agent.get("decision"), "proceed", "agent.decision")
    _expect(agent["operations"]["missing_top_negative"].get("decision"), "block", "agent missing-top decision")
    _expect(
        agent["operations"]["lint_missing_top_negative"].get("decision"),
        "block",
        "agent lint missing-top decision",
    )
    _expect(agent["replays"]["tamper_replays"][0].get("observed_diagnostic"), tamper["observed_diagnostic"], "agent tamper diagnostic")
    _expect(
        agent["replays"]["tamper_replays"][1].get("observed_diagnostic"),
        lint_tamper["observed_diagnostic"],
        "agent lint tamper diagnostic",
    )
    _expect(probes.get("status"), "pass", "probes.status")
    _expect(contract, _contract_document(), "contract evidence")
    trust = agent["trust_chain"]
    _verify_file_record(
        trust["oracle"], ORACLE_PATH, "conformance/ihp-sar-rtl/semantic-oracle.json"
    )
    _verify_file_record(
        trust["normalized"], NORMALIZED_PATH,
        "conformance/ihp-sar-rtl/semantic-normalized.json",
    )
    _verify_file_record(
        trust["decision"], DECISION_PATH,
        "conformance/ihp-sar-rtl/semantic-decision.json",
    )
    native = trust.get("native_artifacts")
    if not isinstance(native, list) or len(native) != len(NATIVE_FILES):
        raise ConformanceError("agent trust chain does not bind every native artifact")
    for record, relative in zip(native, NATIVE_FILES, strict=True):
        _verify_file_record(
            record,
            BUNDLE / relative,
            f"conformance/ihp-sar-rtl/semantic-artifacts/{relative}",
        )
    distinct = {
        sha256_file(ORACLE_PATH), sha256_file(NORMALIZED_PATH),
        sha256_file(DECISION_PATH), sha256_file(EVIDENCE_PATH),
    }
    if len(distinct) != 4:
        raise ConformanceError("oracle, normalized, downstream, and agent evidence digests must differ")
    _expect(verified["positive"]["structure"]["cell_count"], 18, "published cell count")
    _expect(
        verified["lint_positive"]["result"]["data"]["diagnostic_count"],
        0,
        "published strict lint diagnostic count",
    )
    _expect(
        verified["lint_positive_2023"]["result"]["data"]["diagnostic_count"],
        0,
        "published strict 2023 lint diagnostic count",
    )
    _verify_chain_run()


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
            print(f"Published verified SAR RTL agent evidence: {EVIDENCE_PATH}")
        else:
            if args.evidence_dir is not None:
                raise ConformanceError("--evidence-dir is only valid with --publish")
            verify_publication()
            print(f"Verified published SAR RTL agent evidence: {EVIDENCE_PATH}")
    except ConformanceError as exc:
        print(f"semantic evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
