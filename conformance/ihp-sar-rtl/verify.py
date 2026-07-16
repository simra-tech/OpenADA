#!/usr/bin/env python3
"""Independently verify retained OpenADA and native Yosys SAR RTL evidence."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from common import (
    ConformanceError,
    NATIVE_YOSYS_PATH,
    RESULT_SCHEMA,
    SOURCE_BYTES,
    SOURCE_PATH,
    SOURCE_SHA256,
    WRAPPER_PATH,
    YOSYS_VERSION,
    load_manifest,
    sha256_file,
)


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402

RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas/result-v0alpha1.schema.json"
RUN_SCHEMA_PATH = HERE / "run.schema.json"
DESIGN_PROVENANCE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas/design-provenance-v0alpha1.schema.json"
)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_NATIVE_BYTES = 64 * 1024 * 1024
TEMP_CWD_RE = re.compile(r"^/evidence/(positive|negative)/\.openada-yosys-[A-Za-z0-9_-]+$")
EXPECTED_FILES = {
    "design-provenance.json",
    "run.json",
    "positive/rtl-check.result.json",
    "positive/rtl-check.ys",
    "positive/sar_logic.json",
    "positive/yosys.transcript.json",
    "negative/rtl-check.result.json",
    "negative/rtl-check.ys",
    "negative/yosys.transcript.json",
}
EXPECTED_PORTS = {
    "clk": ("input", 1),
    "Op": ("input", 1),
    "En": ("input", 1),
    "Om": ("input", 1),
    "rst": ("input", 1),
    "B": ("output", 8),
    "BN": ("output", 8),
    "D": ("output", 8),
}
EXPECTED_CELL_COUNTS = {
    "$add": 1,
    "$and": 2,
    "$eq": 1,
    "$logic_and": 2,
    "$neg": 1,
    "$not": 2,
    "$or": 2,
    "$sdffe": 3,
    "$shift": 3,
    "$xor": 1,
}


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _require_regular(path: Path, *, label: str, maximum: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum:
        raise ConformanceError(f"{label} size is outside 1..{maximum} bytes: {path}")
    return metadata.st_size


def _read_json(path: Path, *, label: str, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    _require_regular(path, label=label, maximum=maximum)
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


def _validator(path: Path, *, label: str) -> Draft202012Validator:
    schema = _read_json(path, label=f"{label} schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConformanceError(f"invalid {label} schema: {exc.message}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate(document: dict[str, Any], validator: Draft202012Validator, *, label: str) -> None:
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label} violates its JSON Schema at {location}: {error.message}")


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _artifact_map(records: Any, location: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ConformanceError(f"{location} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConformanceError(f"{location}[{index}] is not a file record")
        if record["path"] in mapped:
            raise ConformanceError(f"{location} contains duplicate path {record['path']!r}")
        mapped[record["path"]] = record
    return mapped


def _expected_script(top: str) -> str:
    return "\n".join(
        (
            f'read_verilog -sv "{SOURCE_PATH}"',
            f"hierarchy -check -top {top}",
            "proc",
            "opt",
            "check -assert",
            'write_json "netlist.json"',
            "",
        )
    )


def _verify_script(path: Path, *, top: str) -> None:
    _require_regular(path, label="native Yosys script", maximum=16 * 1024)
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConformanceError(f"cannot read native Yosys script {path}: {exc}") from exc
    _expect(body, _expected_script(top), f"{top} native Yosys script")


def _stream_bytes(record: Any, location: str) -> bytes:
    if not isinstance(record, dict) or set(record) != {"base64", "bytes", "sha256"}:
        raise ConformanceError(f"{location} is not a closed native stream record")
    encoded = record.get("base64")
    if not isinstance(encoded, str):
        raise ConformanceError(f"{location}.base64 must be a string")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConformanceError(f"{location}.base64 is invalid: {exc}") from exc
    if len(body) > 8 * 1024 * 1024:
        raise ConformanceError(f"{location} exceeds the native transcript bound")
    _expect(record.get("bytes"), len(body), f"{location}.bytes")
    _expect(record.get("sha256"), hashlib.sha256(body).hexdigest(), f"{location}.sha256")
    return body


def _verify_transcript(
    path: Path,
    *,
    script_path: str,
    expected_exit: int,
    expected_cwd_kind: str,
) -> dict[str, Any]:
    transcript = _read_json(path, label="native Yosys transcript", maximum=24 * 1024 * 1024)
    _expect(
        set(transcript),
        {"schema", "command", "cwd", "exit_code", "stdout", "stderr"},
        "native Yosys transcript keys",
    )
    _expect(transcript["schema"], "openada.yosys-native-transcript/v1", "transcript.schema")
    _expect(
        transcript["command"],
        [NATIVE_YOSYS_PATH, "-q", "-s", script_path],
        "transcript.command",
    )
    cwd = transcript.get("cwd")
    if not isinstance(cwd, str) or TEMP_CWD_RE.fullmatch(cwd) is None:
        raise ConformanceError(f"transcript.cwd is not a bounded Yosys temporary path: {cwd!r}")
    if not cwd.startswith(f"/evidence/{expected_cwd_kind}/"):
        raise ConformanceError(f"transcript.cwd is not under {expected_cwd_kind!r}")
    _expect(transcript["exit_code"], expected_exit, "transcript.exit_code")
    stdout = _stream_bytes(transcript["stdout"], "transcript.stdout")
    stderr = _stream_bytes(transcript["stderr"], "transcript.stderr")
    return {"cwd": cwd, "stdout": stdout, "stderr": stderr}


def _verify_result_common(
    result: dict[str, Any],
    *,
    kind: str,
    top: str,
    expected_exit: int,
    expected_status: str,
    expected_summary: str,
    expected_json_validation: str,
) -> None:
    _expect(result.get("schema"), RESULT_SCHEMA, f"{kind}.schema")
    _expect(result.get("operation"), "rtl-check", f"{kind}.operation")
    _expect(
        result.get("tool"),
        {"name": "yosys", "path": WRAPPER_PATH, "version": YOSYS_VERSION},
        f"{kind}.tool",
    )
    execution = result.get("execution")
    if not isinstance(execution, dict):
        raise ConformanceError(f"{kind}.execution must be an object")
    _expect(execution.get("status"), "completed", f"{kind}.execution.status")
    _expect(execution.get("exit_code"), expected_exit, f"{kind}.execution.exit_code")
    _expect(
        execution.get("command"),
        [WRAPPER_PATH, "-q", "-s", f"/evidence/{kind}/rtl-check.ys"],
        f"{kind}.execution.command",
    )
    cwd = execution.get("cwd")
    if not isinstance(cwd, str) or TEMP_CWD_RE.fullmatch(cwd) is None or not cwd.startswith(f"/evidence/{kind}/"):
        raise ConformanceError(f"{kind}.execution.cwd is not the reviewed temporary directory: {cwd!r}")
    _expect(
        result.get("engineering"),
        {"status": expected_status, "summary": expected_summary},
        f"{kind}.engineering",
    )
    inputs = result.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise ConformanceError(f"{kind}.inputs must contain exactly the pinned source")
    _expect(
        inputs[0],
        {
            "path": SOURCE_PATH,
            "exists": True,
            "bytes": SOURCE_BYTES,
            "sha256": SOURCE_SHA256,
            "kind": "hdl-source",
            "role": "input",
        },
        f"{kind}.inputs[0]",
    )
    data = result.get("data")
    if not isinstance(data, dict):
        raise ConformanceError(f"{kind}.data must be an object")
    _expect(data.get("top"), top, f"{kind}.data.top")
    _expect(data.get("json_validation"), expected_json_validation, f"{kind}.data.json_validation")
    _expect(data.get("errors_truncated"), False, f"{kind}.data.errors_truncated")
    _expect(data.get("warnings_truncated"), False, f"{kind}.data.warnings_truncated")
    _expect(data.get("warnings"), [], f"{kind}.data.warnings")


def _verify_artifact_record(
    record: dict[str, Any],
    *,
    evidence: Path,
    relative: str,
    kind: str,
    role: str,
) -> None:
    path = evidence / relative
    size = _require_regular(path, label=kind, maximum=MAX_NATIVE_BYTES)
    _expect(record.get("exists"), True, f"{relative}.exists")
    _expect(record.get("kind"), kind, f"{relative}.kind")
    _expect(record.get("role"), role, f"{relative}.role")
    _expect(record.get("bytes"), size, f"{relative}.bytes")
    _expect(record.get("sha256"), sha256_file(path), f"{relative}.sha256")


def _binary_width(value: Any, location: str) -> int:
    if not isinstance(value, str) or not value or set(value) - {"0", "1"}:
        raise ConformanceError(f"{location} is not a binary parameter")
    return int(value, 2)


def _verify_structure(path: Path) -> dict[str, Any]:
    document = _read_json(path, label="positive native Yosys JSON", maximum=MAX_NATIVE_BYTES)
    _expect(document.get("creator"), YOSYS_VERSION, "positive Yosys JSON creator")
    modules = document.get("modules")
    if not isinstance(modules, dict):
        raise ConformanceError("positive Yosys JSON modules must be an object")
    if set(modules) != {"sar_logic"}:
        raise ConformanceError(
            "positive Yosys JSON modules differ; "
            f"expected={['sar_logic']!r}, got={sorted(modules)!r}"
        )
    module = modules["sar_logic"]
    if not isinstance(module, dict):
        raise ConformanceError("positive Yosys JSON sar_logic module must be an object")
    attributes = module.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("top") != "00000000000000000000000000000001":
        raise ConformanceError("positive Yosys JSON sar_logic is not marked as the unique top")
    ports = module.get("ports")
    if not isinstance(ports, dict):
        raise ConformanceError("positive Yosys JSON ports must be an object")
    if set(ports) != set(EXPECTED_PORTS):
        raise ConformanceError(
            "positive Yosys JSON ports differ; "
            f"expected={sorted(EXPECTED_PORTS)!r}, got={sorted(ports)!r}"
        )
    normalized_ports: dict[str, dict[str, Any]] = {}
    for name, (direction, width) in EXPECTED_PORTS.items():
        port = ports[name]
        if not isinstance(port, dict) or set(port) != {"direction", "bits"}:
            raise ConformanceError(f"positive Yosys JSON port {name} is not a closed port record")
        _expect(port["direction"], direction, f"positive Yosys JSON port {name}.direction")
        bits = port["bits"]
        if not isinstance(bits, list) or len(bits) != width or any(isinstance(bit, bool) or not isinstance(bit, (int, str)) for bit in bits):
            raise ConformanceError(f"positive Yosys JSON port {name} width or bit encoding differs")
        normalized_ports[name] = {"direction": direction, "width": width, "bits": list(bits)}
    _expect(ports["B"]["bits"], ports["D"]["bits"], "positive Yosys JSON B/D alias")
    if set(ports["BN"]["bits"]) & set(ports["B"]["bits"]):
        raise ConformanceError("positive Yosys JSON BN overlaps the B/D output state")
    cells = module.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ConformanceError("positive Yosys JSON cells must be a non-empty object")
    counts: Counter[str] = Counter()
    sequential_widths: list[int] = []
    sequential_outputs: list[list[Any]] = []
    for name, cell in cells.items():
        if not isinstance(name, str) or not isinstance(cell, dict):
            raise ConformanceError("positive Yosys JSON contains an invalid cell record")
        cell_type = cell.get("type")
        if not isinstance(cell_type, str):
            raise ConformanceError(f"positive Yosys JSON cell {name} has no type")
        counts[cell_type] += 1
        cell_attributes = cell.get("attributes", {})
        if isinstance(cell_attributes, dict) and cell_attributes.get("blackbox") not in (None, 0, "0" * 32):
            raise ConformanceError(f"positive Yosys JSON cell {name} is black-boxed")
        if cell_type == "$sdffe":
            parameters = cell.get("parameters")
            connections = cell.get("connections")
            if not isinstance(parameters, dict) or not isinstance(connections, dict):
                raise ConformanceError(f"positive Yosys JSON state cell {name} is incomplete")
            sequential_widths.append(_binary_width(parameters.get("WIDTH"), f"{name}.WIDTH"))
            output = connections.get("Q")
            if not isinstance(output, list):
                raise ConformanceError(f"positive Yosys JSON state cell {name} has no Q connection")
            sequential_outputs.append(output)
    _expect(dict(sorted(counts.items())), EXPECTED_CELL_COUNTS, "positive Yosys JSON cell types")
    _expect(sorted(sequential_widths), [4, 8, 8], "positive Yosys JSON state widths")
    netnames = module.get("netnames")
    if not isinstance(netnames, dict) or "counter" not in netnames:
        raise ConformanceError("positive Yosys JSON does not retain the counter net")
    counter = netnames["counter"]
    if not isinstance(counter, dict) or not isinstance(counter.get("bits"), list):
        raise ConformanceError("positive Yosys JSON counter net is invalid")
    _expect(len(counter["bits"]), 4, "positive Yosys JSON counter width")
    expected_state = [counter["bits"], ports["BN"]["bits"], ports["D"]["bits"]]
    if sorted(map(repr, sequential_outputs)) != sorted(map(repr, expected_state)):
        raise ConformanceError("positive Yosys JSON state outputs do not bind counter, BN, and D")
    return {
        "creator": document["creator"],
        "module_names": ["sar_logic"],
        "ports": normalized_ports,
        "cell_count": len(cells),
        "cell_type_counts": dict(sorted(counts.items())),
        "state_widths": sorted(sequential_widths),
        "counter_width": 4,
        "b_aliases_d": True,
        "blackbox_cells": 0,
    }


def _verify_positive(
    manifest: dict[str, Any],
    evidence: Path,
    result_validator: Draft202012Validator,
) -> dict[str, Any]:
    operation = manifest["operations"]["rtl_check"]
    result = _read_json(evidence / operation["result_filename"], label="positive OpenADA result")
    _validate(result, result_validator, label="positive OpenADA result")
    _verify_result_common(
        result,
        kind="positive",
        top=operation["top"],
        expected_exit=0,
        expected_status="pass",
        expected_summary=operation["expect"]["summary"],
        expected_json_validation="parsed",
    )
    _expect(result["data"].get("errors"), [], "positive.data.errors")
    _expect(result.get("diagnostics"), [], "positive.diagnostics")
    artifacts = _artifact_map(result.get("artifacts"), "positive.artifacts")
    _expect(set(artifacts), {operation["script"]["path"], operation["netlist"]["path"]}, "positive.artifact paths")
    _verify_artifact_record(
        artifacts[operation["script"]["path"]], evidence=evidence,
        relative=operation["script"]["filename"], kind="yosys-script", role="evidence",
    )
    _verify_artifact_record(
        artifacts[operation["netlist"]["path"]], evidence=evidence,
        relative=operation["netlist"]["filename"], kind="yosys-json", role="output",
    )
    _verify_script(evidence / operation["script"]["filename"], top=operation["top"])
    transcript = _verify_transcript(
        evidence / operation["transcript"]["filename"],
        script_path=operation["script"]["path"], expected_exit=0, expected_cwd_kind="positive",
    )
    _expect(transcript["cwd"], result["execution"]["cwd"], "positive transcript/result cwd")
    if b"ERROR:" in transcript["stdout"] + transcript["stderr"]:
        raise ConformanceError("positive native Yosys transcript contains an error")
    return {"result": result, "structure": _verify_structure(evidence / operation["netlist"]["filename"])}


def _verify_negative(
    manifest: dict[str, Any],
    evidence: Path,
    result_validator: Draft202012Validator,
) -> dict[str, Any]:
    operation = manifest["operations"]["missing_top"]
    result = _read_json(evidence / operation["result_filename"], label="negative OpenADA result")
    _validate(result, result_validator, label="negative OpenADA result")
    _verify_result_common(
        result,
        kind="negative",
        top=operation["top"], expected_exit=1, expected_status="fail",
        expected_summary=operation["expect"]["summary"], expected_json_validation="missing",
    )
    diagnostic = operation["expect"]["diagnostic"]
    _expect(result["data"].get("errors"), [diagnostic], "negative.data.errors")
    _expect(
        result.get("diagnostics"),
        [
            {"severity": "error", "code": "yosys.nonzero_exit", "message": "Yosys exited with code 1."},
            {"severity": "error", "code": "yosys.error", "message": diagnostic},
        ],
        "negative.diagnostics",
    )
    artifacts = _artifact_map(result.get("artifacts"), "negative.artifacts")
    _expect(set(artifacts), {operation["script"]["path"]}, "negative.artifact paths")
    _verify_artifact_record(
        artifacts[operation["script"]["path"]], evidence=evidence,
        relative=operation["script"]["filename"], kind="yosys-script", role="evidence",
    )
    if (evidence / "negative/missing_sar_logic.json").exists():
        raise ConformanceError("negative missing-top replay unexpectedly produced a JSON netlist")
    _verify_script(evidence / operation["script"]["filename"], top=operation["top"])
    transcript = _verify_transcript(
        evidence / operation["transcript"]["filename"],
        script_path=operation["script"]["path"], expected_exit=1, expected_cwd_kind="negative",
    )
    _expect(transcript["cwd"], result["execution"]["cwd"], "negative transcript/result cwd")
    try:
        stderr = transcript["stderr"].decode("utf-8")
    except UnicodeError as exc:
        raise ConformanceError(f"negative native Yosys stderr is not UTF-8: {exc}") from exc
    if diagnostic not in stderr:
        raise ConformanceError("negative native Yosys transcript lacks the missing-top diagnostic")
    return {"result": result, "diagnostic": diagnostic, "native_stderr": stderr}


def _verify_run(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
    run_validator: Draft202012Validator,
) -> dict[str, Any]:
    run = _read_json(evidence / "run.json", label="conformance run metadata")
    _validate(run, run_validator, label="conformance run metadata")
    _expect(run["conformance_manifest_sha256"], manifest_sha256, "run.conformance_manifest_sha256")
    checkout = run["openada_checkout"]
    _expect(checkout["state_unchanged"], True, "run.openada_checkout.state_unchanged")
    _expect(checkout["before"], checkout["after"], "run.openada_checkout before/after")
    source = run.get("source_attestation")
    if source is not None:
        subject = semantic_subject(
            REPOSITORY_ROOT,
            REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
        )
        _expect(source["semantic_subject_sha256"], subject, "run.source_attestation.semantic_subject_sha256")
        _expect(source["state_unchanged"], True, "run.source_attestation.state_unchanged")
        if source["receipt_class"] == "release":
            _expect(source["clean_before"], True, "run.source_attestation.clean_before")
            _expect(source["clean_after"], True, "run.source_attestation.clean_after")
    _expect(
        run["tool"]["wrapper_sha256"],
        sha256_file(HERE / "yosys_wrapper.py"),
        "run.tool.wrapper_sha256",
    )
    records = _artifact_map(run["native_artifacts"], "run.native_artifacts")
    expected = EXPECTED_FILES - {"run.json", "design-provenance.json"}
    _expect(set(records), expected, "run.native_artifacts paths")
    for relative, record in records.items():
        path = evidence / relative
        size = _require_regular(path, label="run-bound native artifact", maximum=MAX_NATIVE_BYTES)
        _expect(record.get("bytes"), size, f"run.native_artifacts[{relative}].bytes")
        _expect(record.get("sha256"), sha256_file(path), f"run.native_artifacts[{relative}].sha256")
    return run


def _verify_design_provenance(manifest: dict[str, Any], evidence: Path) -> None:
    provenance = _read_json(
        evidence / "design-provenance.json", label="design provenance"
    )
    _validate(
        provenance,
        _validator(DESIGN_PROVENANCE_SCHEMA_PATH, label="design provenance"),
        label="design provenance",
    )
    expected = {
        "repository": manifest["design"]["repository"],
        "revision": manifest["design"]["revision"],
        "tree": "2a710fd503226e9642e4337a324e6c192a9d8a31",
    }
    for field, value in expected.items():
        _expect(provenance[field], value, f"design provenance {field}")
    _expect(
        {key: provenance["license"][key] for key in ("path", "sha256")},
        {key: manifest["design"]["license"][key] for key in ("path", "sha256")},
        "design provenance license",
    )
    _expect(
        [
            {key: item[key] for key in ("path", "sha256")}
            for item in provenance["inputs"]
        ],
        [
            {
                "path": manifest["source"]["repository_path"],
                "sha256": manifest["source"]["sha256"],
            }
        ],
        "design provenance inputs",
    )


def _verify_file_set(evidence: Path) -> None:
    if evidence.is_symlink() or not evidence.is_dir():
        raise ConformanceError(f"evidence root is not a real directory: {evidence}")
    actual: set[str] = set()
    for path in evidence.rglob("*"):
        relative = path.relative_to(evidence).as_posix()
        if path.is_symlink():
            raise ConformanceError(f"evidence contains a symbolic link: {relative}")
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir():
            raise ConformanceError(f"evidence contains a non-file entry: {relative}")
    base = EXPECTED_FILES - {"design-provenance.json"}
    if actual not in (base, EXPECTED_FILES):
        _expect(actual, EXPECTED_FILES, "evidence file set")


def verify_evidence(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    evidence = evidence.expanduser().resolve()
    _verify_file_set(evidence)
    if (evidence / "design-provenance.json").is_file():
        _verify_design_provenance(manifest, evidence)
    result_validator = _validator(RESULT_SCHEMA_PATH, label="OpenADA result")
    run_validator = _validator(RUN_SCHEMA_PATH, label="conformance run")
    positive = _verify_positive(manifest, evidence, result_validator)
    negative = _verify_negative(manifest, evidence, result_validator)
    run = _verify_run(
        manifest, evidence, manifest_sha256=manifest_sha256, run_validator=run_validator
    )
    return {
        "verified": True,
        "positive": positive,
        "negative": negative,
        "run": run,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        summary = verify_evidence(
            manifest,
            args.evidence,
            manifest_sha256=sha256_file(manifest_path),
        )
    except ConformanceError as exc:
        print(f"evidence verification failed: {exc}", file=sys.stderr)
        return 1
    structure = summary["positive"]["structure"]
    print(
        "Evidence verified: sar_logic elaborated with "
        f"{structure['cell_count']} cells and the real missing-top replay was rejected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
