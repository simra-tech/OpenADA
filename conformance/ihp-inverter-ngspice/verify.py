#!/usr/bin/env python3
"""Independently verify pinned Xschem/ngspice results and binary waveforms."""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import struct
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from common import ConformanceError, load_manifest, sha256_file


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "result-v0alpha1.schema.json"
RUN_SCHEMA_PATH = HERE / "run.schema.json"
RESULT_SCHEMA = "openada.result/v0alpha1"
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_RAW_HEADER_BYTES = 1024 * 1024
MAX_RAW_LINE_BYTES = 65_536
XSCHEM_TEMP_RE = re.compile(r"/tmp/openada-xschem-[A-Za-z0-9_-]+")
NGSPICE_TEMP_LOG_RE = re.compile(
    r"/tmp/openada-ngspice-[A-Za-z0-9_-]+/simulation\.log"
)
CONTAINER_NAME_RE = re.compile(r"openada-ihp-ngspice-[1-9][0-9]*-[0-9a-f]{8}")
CONTROL_SCRIPT = (
    b"*ng_script_with_params\n"
    b"set noaskquit\n"
    b"source /foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit\n"
    b"source /evidence/work/inverter_tb.spice\n"
    b"quit\n"
)
NATIVE_LOG_ERROR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"simulation interrupted due to error",
        r"run simulation not started",
        r"fatal error",
        r"^\s*error on line\b",
        r"^\s*error:\s+no such vector\b",
        r"unknown subckt",
        r"could not find a valid modelname",
        r"cannot find model",
        r"^\s*(?:fatal\s+)?error(?:\s*:|\s+on\b)",
    )
)
HARMLESS_GRAPHICS_ERROR_RE = re.compile(
    r"^\s*error:\s*\(external\)\s+no graphics interface\b", re.IGNORECASE
)


def _expect_equal(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _require_regular_file(path: Path, *, label: str, maximum_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ConformanceError(f"{label} is not a regular, non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must have exactly one hard link: {path}")
    if metadata.st_size <= 0:
        raise ConformanceError(f"{label} is empty: {path}")
    if metadata.st_size > maximum_bytes:
        raise ConformanceError(
            f"{label} exceeds the {maximum_bytes}-byte verification limit: {path}"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular_file(path, label=label, maximum_bytes=MAX_JSON_BYTES)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def _load_validator(path: Path, *, label: str) -> Draft202012Validator:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} schema {path}: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConformanceError(f"invalid {label} schema: {exc.message}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_schema(
    document: dict[str, Any], validator: Draft202012Validator, *, label: str
) -> None:
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "<root>"
    raise ConformanceError(f"{label} violates its JSON Schema at {location}: {error.message}")


def _record_map(records: Any, location: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ConformanceError(f"{location} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConformanceError(f"{location}[{index}] must be a file record with a path")
        path = record["path"]
        if path in mapped:
            raise ConformanceError(f"{location} contains duplicate path {path!r}")
        mapped[path] = record
    return mapped


def _verify_file_record(
    record: dict[str, Any],
    path: Path,
    expected: dict[str, Any],
    *,
    location: str,
) -> None:
    _expect_equal(record.get("exists"), True, f"{location}.exists")
    _expect_equal(record.get("kind"), expected["kind"], f"{location}.kind")
    _expect_equal(record.get("role"), expected["role"], f"{location}.role")
    size = _require_regular_file(path, label=location, maximum_bytes=MAX_ARTIFACT_BYTES)
    _expect_equal(record.get("bytes"), size, f"{location}.bytes")
    _expect_equal(record.get("sha256"), sha256_file(path), f"{location}.sha256")


def _verify_error_free_result(result: dict[str, Any], operation: str) -> None:
    _expect_equal(result.get("schema"), RESULT_SCHEMA, f"{operation}.schema")
    _expect_equal(result.get("operation"), operation, f"{operation}.operation")
    _expect_equal(result["execution"].get("status"), "completed", f"{operation}.execution.status")
    _expect_equal(result["execution"].get("exit_code"), 0, f"{operation}.execution.exit_code")
    _expect_equal(result["engineering"].get("status"), "pass", f"{operation}.engineering.status")
    errors = [item for item in result["diagnostics"] if item.get("severity") == "error"]
    if errors:
        raise ConformanceError(f"{operation} contains error diagnostics: {errors!r}")


def _verify_active_netlist_semantics(netlist_text: str) -> None:
    active_lines = [
        line.strip()
        for line in netlist_text.splitlines()
        if line.strip() and not line.lstrip().startswith(("*", ";", "$"))
    ]
    patterns = {
        "model corner": r"^\.lib\s+cornerMOSlv\.lib\s+mos_tt(?:\s|$)",
        "inverter subcircuit": r"^\.subckt\s+inverter\s+Vdd\s+Vin\s+Vout\s+Gnd(?:\s|$)",
        "NMOS instance": r"^XM1\s+Gnd\s+Vin\s+Vout\s+Gnd\s+sg13_lv_nmos(?:\s|$)",
        "PMOS instance": r"^XM2\s+Vout\s+Vin\s+Vdd\s+Vdd\s+sg13_lv_pmos(?:\s|$)",
    }
    missing = [
        label
        for label, pattern in patterns.items()
        if not any(re.match(pattern, line, re.IGNORECASE) for line in active_lines)
    ]

    in_control = False
    control_blocks = 0
    in_control_writes = 0
    malformed_control = False
    for line in active_lines:
        if re.match(r"^\.control(?:\s|$)", line, re.IGNORECASE):
            if in_control:
                malformed_control = True
            in_control = True
            control_blocks += 1
            continue
        if re.match(r"^\.endc(?:\s|$)", line, re.IGNORECASE):
            if not in_control:
                malformed_control = True
            in_control = False
            continue
        if re.match(r"^write\s+test_inverter\.raw(?:\s|$)", line, re.IGNORECASE):
            if in_control:
                in_control_writes += 1
            else:
                malformed_control = True
    if in_control:
        malformed_control = True
    if malformed_control or control_blocks != 1 or in_control_writes != 1:
        missing.append("active control-block write of test_inverter.raw")
    if missing:
        raise ConformanceError(
            f"generated netlist lacks reviewed active inverter/deck records: {missing!r}"
        )


def _verify_netlist(
    manifest: dict[str, Any], result: dict[str, Any], evidence: Path
) -> tuple[Path, str]:
    operation = manifest["workflow"]["netlist"]
    identity = manifest["tools"]["xschem"]
    _verify_error_free_result(result, "netlist")
    _expect_equal(result["tool"].get("name"), "xschem", "netlist.tool.name")
    _expect_equal(result["tool"].get("path"), identity["path"], "netlist.tool.path")
    _expect_equal(result["tool"].get("version"), identity["version"], "netlist.tool.version")

    arguments = operation["arguments"]
    command = result["execution"].get("command")
    if not isinstance(command, list) or len(command) != 10:
        raise ConformanceError(f"netlist.execution.command has unexpected shape: {command!r}")
    expected_prefix = [
        identity["path"],
        "--rcfile",
        arguments["rcfile"],
        "-n",
        "-s",
        "-q",
        "-x",
        "-o",
    ]
    if command[:8] != expected_prefix or not XSCHEM_TEMP_RE.fullmatch(command[8]):
        raise ConformanceError(f"netlist.execution.command differs from reviewed argv: {command!r}")
    _expect_equal(command[9], arguments["schematic"], "netlist.execution.command.schematic")

    schematic_parent = str(Path(arguments["schematic"]).parent)
    _expect_equal(result["execution"].get("cwd"), schematic_parent, "netlist.execution.cwd")

    actual_inputs = _record_map(result.get("inputs"), "netlist.inputs")
    expected_inputs = {item["path"]: item for item in operation["inputs"]}
    _expect_equal(set(actual_inputs), set(expected_inputs), "netlist.inputs.paths")
    for path, expected in expected_inputs.items():
        actual = actual_inputs[path]
        _expect_equal(actual.get("exists"), True, f"netlist.inputs[{path}].exists")
        _expect_equal(actual.get("kind"), expected["kind"], f"netlist.inputs[{path}].kind")
        _expect_equal(actual.get("role"), expected["role"], f"netlist.inputs[{path}].role")
        _expect_equal(actual.get("sha256"), expected["sha256"], f"netlist.inputs[{path}].sha256")
        if not isinstance(actual.get("bytes"), int) or actual["bytes"] <= 0:
            raise ConformanceError(f"netlist.inputs[{path}].bytes must be positive")

    artifact = operation["artifact"]
    actual_artifacts = _record_map(result.get("artifacts"), "netlist.artifacts")
    _expect_equal(set(actual_artifacts), {artifact["path"]}, "netlist.artifacts.paths")
    netlist_path = evidence / artifact["filename"]
    _verify_file_record(
        actual_artifacts[artifact["path"]],
        netlist_path,
        artifact,
        location="netlist.artifact",
    )
    netlist_text = netlist_path.read_text(encoding="utf-8", errors="replace")
    _verify_active_netlist_semantics(netlist_text)
    if re.search(r"\bIS\s+MISSING\b", netlist_text, re.IGNORECASE):
        raise ConformanceError("generated netlist independently contains an unresolved-symbol marker")
    _expect_equal(result["data"].get("missing_symbol_count"), 0, "netlist.data.missing_symbol_count")
    _expect_equal(result["data"].get("missing_symbols"), [], "netlist.data.missing_symbols")
    _expect_equal(
        result["data"].get("missing_symbols_truncated"),
        False,
        "netlist.data.missing_symbols_truncated",
    )
    digest = sha256_file(netlist_path)
    return netlist_path, digest


def _verify_simulation(
    manifest: dict[str, Any],
    result: dict[str, Any],
    evidence: Path,
    netlist_sha256: str,
    runtime_observation: dict[str, Any],
) -> Path:
    operation = manifest["workflow"]["simulate"]
    identity = manifest["tools"]["ngspice"]
    arguments = operation["arguments"]
    _verify_error_free_result(result, "simulate")
    _expect_equal(result["tool"].get("name"), "ngspice", "simulate.tool.name")
    _expect_equal(result["tool"].get("path"), identity["path"], "simulate.tool.path")
    _expect_equal(result["tool"].get("version"), identity["version"], "simulate.tool.version")
    _expect_equal(result["execution"].get("cwd"), arguments["workdir"], "simulate.execution.cwd")
    command = result["execution"].get("command")
    if not isinstance(command, list) or len(command) != 6:
        raise ConformanceError(f"simulate.execution.command has unexpected shape: {command!r}")
    expected = [
        identity["path"],
        "-i",
        "-n",
        "-o",
        None,
        "/evidence/simulation/inverter_tb.openada-control.sp",
    ]
    if command[:4] != expected[:4] or not NGSPICE_TEMP_LOG_RE.fullmatch(command[4]):
        raise ConformanceError(f"simulate.execution.command differs from reviewed argv: {command!r}")
    _expect_equal(command[5:], expected[5:], "simulate.execution.command.arguments")

    actual_inputs = _record_map(result.get("inputs"), "simulate.inputs")
    expected_inputs = {item["path"]: item for item in operation["inputs"]}
    _expect_equal(set(actual_inputs), set(expected_inputs), "simulate.inputs.paths")
    source_record = actual_inputs[arguments["spice_file"]]
    _expect_equal(source_record.get("exists"), True, "simulate.netlist_input.exists")
    _expect_equal(source_record.get("kind"), "spice-netlist", "simulate.netlist_input.kind")
    _expect_equal(source_record.get("role"), "input", "simulate.netlist_input.role")
    _expect_equal(source_record.get("sha256"), netlist_sha256, "simulate.netlist_input.sha256")
    _expect_equal(
        source_record.get("bytes"),
        (evidence / "work/inverter_tb.spice").stat().st_size,
        "simulate.netlist_input.bytes",
    )
    init_record = actual_inputs[arguments["init_file"]]
    init_expected = expected_inputs[arguments["init_file"]]
    for field in ("kind", "role", "sha256"):
        _expect_equal(init_record.get(field), init_expected[field], f"simulate.init_input.{field}")
    _expect_equal(init_record.get("exists"), True, "simulate.init_input.exists")
    _expect_equal(
        init_record.get("bytes"),
        runtime_observation["pdk"]["ngspice_init"]["bytes"],
        "simulate.init_input.bytes",
    )
    system_init_record = actual_inputs[arguments["system_init_file"]]
    system_init_expected = expected_inputs[arguments["system_init_file"]]
    for field in ("kind", "role", "sha256"):
        _expect_equal(
            system_init_record.get(field),
            system_init_expected[field],
            f"simulate.system_init_input.{field}",
        )
    _expect_equal(
        system_init_record.get("exists"), True, "simulate.system_init_input.exists"
    )
    _expect_equal(
        system_init_record.get("bytes"),
        runtime_observation["ngspice_system_init"]["bytes"],
        "simulate.system_init_input.bytes",
    )

    expected_artifacts = {item["path"]: item for item in operation["artifacts"]}
    actual_artifacts = _record_map(result.get("artifacts"), "simulate.artifacts")
    _expect_equal(set(actual_artifacts), set(expected_artifacts), "simulate.artifacts.paths")
    for path, expected_artifact in expected_artifacts.items():
        local_path = evidence / expected_artifact["filename"]
        _verify_file_record(
            actual_artifacts[path],
            local_path,
            expected_artifact,
            location=f"simulate.artifacts[{path}]",
        )

    script_path = evidence / "simulation/inverter_tb.openada-control.sp"
    if script_path.read_bytes() != CONTROL_SCRIPT:
        raise ConformanceError("generated ngspice control script differs from the reviewed launcher")
    log_path = evidence / "simulation/inverter_tb.log"
    log = log_path.read_text(encoding="utf-8", errors="replace")
    if "binary raw file \"test_inverter.raw\"" not in log or "ngspice-46 done" not in log:
        raise ConformanceError("ngspice log lacks binary-output and clean-completion evidence")
    if re.search(r"(?:failed to converge|timestep too small|singular matrix|fatal error)", log, re.I):
        raise ConformanceError("ngspice log contains convergence or fatal-error evidence")
    unexpected_errors = [
        line
        for line in log.splitlines()
        if any(pattern.search(line) for pattern in NATIVE_LOG_ERROR_PATTERNS)
        and HARMLESS_GRAPHICS_ERROR_RE.search(line) is None
    ]
    if unexpected_errors:
        raise ConformanceError(
            f"ngspice log contains native error evidence: {unexpected_errors[0][:1_000]!r}"
        )

    data = result["data"]
    _expect_equal(data.get("execution_mode"), "control", "simulate.data.execution_mode")
    _expect_equal(data.get("working_directory"), arguments["workdir"], "simulate.data.working_directory")
    _expect_equal(data.get("working_directory_is_sandbox"), False, "simulate.data.working_directory_is_sandbox")
    _expect_equal(data.get("transitive_inputs_enumerated"), False, "simulate.data.transitive_inputs_enumerated")
    _expect_equal(data.get("transitive_include_detected"), True, "simulate.data.transitive_include_detected")
    _expect_equal(data.get("converged"), True, "simulate.data.converged")
    _expect_equal(data.get("inputs_stable"), True, "simulate.data.inputs_stable")
    _expect_equal(data.get("measurements"), [], "simulate.data.measurements")
    _expect_equal(
        data.get("measurements_truncated"), False, "simulate.data.measurements_truncated"
    )
    _expect_equal(data.get("missing_measurements"), [], "simulate.data.missing_measurements")
    _expect_equal(data.get("duplicate_measurements"), [], "simulate.data.duplicate_measurements")
    _expect_equal(data.get("measurement_section_count"), 0, "simulate.data.measurement_section_count")
    _expect_equal(data.get("solver_warning_count"), 0, "simulate.data.solver_warning_count")
    _expect_equal(data.get("solver_warning_examples"), [], "simulate.data.solver_warning_examples")
    _expect_equal(
        data.get("solver_warning_examples_truncated"),
        False,
        "simulate.data.solver_warning_examples_truncated",
    )
    _expect_equal(
        data.get("expected_outputs"),
        [
            {
                "kind": "raw",
                "declared_path": "test_inverter.raw",
                "path": "/evidence/work/test_inverter.raw",
            }
        ],
        "simulate.data.expected_outputs",
    )
    _expect_equal(
        data.get("initialization"),
        {
            "policy": "explicit",
            "file": arguments["init_file"],
            "local_user_spiceinit": "disabled",
            "system_spinit": {
                "policy": "explicit",
                "file": arguments["system_init_file"],
            },
            "ambient_startup_files_enumerated": True,
        },
        "simulate.data.initialization",
    )
    _expect_equal(
        data.get("environment"),
        {
            "PDK": "ihp-sg13g2",
            "PDK_ROOT": "/foss/pdks",
            "SPICE_ASCIIRAWFILE": None,
            "SPICE_LIB_DIR": None,
            "SPICE_SCRIPTS": "/foss/tools/ngspice/share/ngspice/scripts",
            "NGSPICE_INPUT_DIR": None,
        },
        "simulate.data.environment",
    )
    _expect_equal(
        data.get("environment_overrides"),
        {"SPICE_SCRIPTS": "/foss/tools/ngspice/share/ngspice/scripts"},
        "simulate.data.environment_overrides",
    )
    _expect_equal(
        data.get("analysis_evidence"),
        {"raw": True, "completed_log_record": True},
        "simulate.data.analysis_evidence",
    )
    captures = data.get("output_captures")
    if not isinstance(captures, list) or len(captures) != 1:
        raise ConformanceError("simulate.data.output_captures must contain exactly the deck raw")
    capture = captures[0]
    for field, expected in {
        "kind": "raw",
        "origin": "deck",
        "declared_path": "test_inverter.raw",
        "path": "/evidence/work/test_inverter.raw",
        "status": "valid",
        "parent_anchored": True,
    }.items():
        _expect_equal(capture.get(field), expected, f"simulate.data.output_captures[0].{field}")
    if capture.get("validation", {}).get("valid") is not True:
        raise ConformanceError("OpenADA did not structurally validate the deck-owned raw file")
    raw_path = evidence / "work/test_inverter.raw"
    _expect_equal(capture.get("bytes"), raw_path.stat().st_size, "simulate.data.output_captures[0].bytes")
    _expect_equal(capture.get("sha256"), sha256_file(raw_path), "simulate.data.output_captures[0].sha256")
    script_capture = data.get("control_script_capture", {})
    if script_capture.get("status") != "valid":
        raise ConformanceError("OpenADA did not capture an unchanged control launcher")
    _expect_equal(script_capture.get("path"), "/evidence/simulation/inverter_tb.openada-control.sp", "simulate.data.control_script_capture.path")
    _expect_equal(script_capture.get("bytes"), len(CONTROL_SCRIPT), "simulate.data.control_script_capture.bytes")
    _expect_equal(script_capture.get("sha256"), sha256_file(script_path), "simulate.data.control_script_capture.sha256")
    log_capture = data.get("log_capture", {})
    if log_capture.get("status") != "valid":
        raise ConformanceError("OpenADA did not capture a valid simulation log")
    _expect_equal(log_capture.get("path"), "/evidence/simulation/inverter_tb.log", "simulate.data.log_capture.path")
    _expect_equal(log_capture.get("bytes"), log_path.stat().st_size, "simulate.data.log_capture.bytes")
    _expect_equal(log_capture.get("sha256"), sha256_file(log_path), "simulate.data.log_capture.sha256")
    _expect_equal(data.get("log_tail"), log[-4_000:], "simulate.data.log_tail")
    return raw_path


def _read_bounded_line(handle: BytesIO, consumed: int) -> tuple[bytes, int]:
    line = handle.readline(MAX_RAW_LINE_BYTES + 1)
    if len(line) > MAX_RAW_LINE_BYTES:
        raise ConformanceError("binary raw header contains an overlong line")
    consumed += len(line)
    if consumed > MAX_RAW_HEADER_BYTES:
        raise ConformanceError("binary raw header exceeds the verification bound")
    return line, consumed


def _parse_binary_raw(path: Path, waveform: dict[str, Any]) -> dict[str, Any]:
    size = _require_regular_file(path, label="binary ngspice raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    payload = path.read_bytes()
    if len(payload) != size:
        raise ConformanceError("binary raw file changed while being read")
    handle = BytesIO(payload)
    consumed = 0
    header: dict[str, str] = {}
    first, consumed = _read_bounded_line(handle, consumed)
    if not first.startswith(b"Title:"):
        raise ConformanceError("binary raw file does not begin with a Title header")
    while True:
        line, consumed = _read_bounded_line(handle, consumed)
        if not line:
            raise ConformanceError("binary raw header is truncated")
        if line.strip().lower() == b"variables:":
            break
        key, separator, value = line.partition(b":")
        if not separator:
            raise ConformanceError("binary raw header contains an invalid line")
        normalized = b" ".join(key.strip().lower().split()).decode("ascii", errors="replace")
        if normalized in header:
            raise ConformanceError(f"binary raw header repeats {normalized!r}")
        header[normalized] = value.strip().decode("utf-8", errors="replace")
    for required in ("plotname", "flags", "no. variables", "no. points"):
        if required not in header:
            raise ConformanceError(f"binary raw header lacks {required!r}")
    _expect_equal(header["plotname"], waveform["plotname"], "raw.plotname")
    _expect_equal(header["flags"].casefold(), waveform["flags"], "raw.flags")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except ValueError as exc:
        raise ConformanceError("binary raw dimensions are not integers") from exc
    if not 1 <= variable_count <= 1024:
        raise ConformanceError(f"binary raw variable count is out of bounds: {variable_count}")
    if point_count not in waveform["acceptable_point_counts"]:
        raise ConformanceError(
            f"binary raw point count {point_count} is not one of {waveform['acceptable_point_counts']}"
        )

    variables: list[str] = []
    for index in range(variable_count):
        line, consumed = _read_bounded_line(handle, consumed)
        fields = line.decode("utf-8", errors="replace").split()
        if len(fields) < 3 or fields[0] != str(index):
            raise ConformanceError(f"binary raw variable table is invalid at index {index}")
        variables.append(fields[1].casefold())
    marker, consumed = _read_bounded_line(handle, consumed)
    if marker.strip().lower() != b"binary:":
        raise ConformanceError("ngspice raw evidence is not binary encoded")
    if len(set(variables)) != len(variables):
        raise ConformanceError("binary raw variable names are not unique")
    missing = sorted(set(waveform["required_variables"]) - set(variables))
    if missing:
        raise ConformanceError(f"binary raw file lacks required variables: {missing}")

    binary = handle.read()
    expected_bytes = point_count * variable_count * 8
    if len(binary) != expected_bytes:
        raise ConformanceError(
            f"binary raw payload has {len(binary)} bytes, expected {expected_bytes}"
        )
    values = struct.unpack(f"<{point_count * variable_count}d", binary)
    if not all(math.isfinite(value) for value in values):
        raise ConformanceError("binary raw payload contains a non-finite value")
    columns = {
        name: [values[row * variable_count + column] for row in range(point_count)]
        for column, name in enumerate(variables)
        if name in waveform["required_variables"]
    }
    times = columns["time"]
    if not math.isclose(times[0], waveform["start_seconds"], rel_tol=0.0, abs_tol=1e-18):
        raise ConformanceError(f"transient starts at {times[0]!r}, expected zero")
    if not math.isclose(times[-1], waveform["stop_seconds"], rel_tol=0.0, abs_tol=5e-12):
        raise ConformanceError(
            f"transient stops at {times[-1]!r}, expected {waveform['stop_seconds']!r}"
        )
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise ConformanceError("transient time values are not strictly increasing")
    vdd = columns["v(vdd)"]
    if any(not waveform["vdd_min"] <= value <= waveform["vdd_max"] for value in vdd):
        raise ConformanceError("VDD leaves the reviewed 1.19..1.21 V range")

    vin = columns["v(vin)"]
    vout = columns["v(vout)"]
    for index, window in enumerate(waveform["settled_windows"]):
        samples = [
            row
            for row, time in enumerate(times)
            if window["start_seconds"] <= time <= window["stop_seconds"]
        ]
        if not samples:
            raise ConformanceError(f"settled inversion window {index} contains no sample")
        for row in samples:
            checks = (
                ("vin_min", vin[row] >= window.get("vin_min", -math.inf)),
                ("vin_max", vin[row] <= window.get("vin_max", math.inf)),
                ("vout_min", vout[row] >= window.get("vout_min", -math.inf)),
                ("vout_max", vout[row] <= window.get("vout_max", math.inf)),
            )
            failed = [name for name, passed in checks if not passed]
            if failed:
                raise ConformanceError(
                    f"settled inversion window {index} violates {failed} at t={times[row]:.9g}"
                )
    return {"points": point_count, "variables": variable_count, "names": variables}


def _verify_checkout(metadata: dict[str, Any]) -> None:
    checkout = metadata["openada_checkout"]
    before = checkout["before"]
    after = checkout["after"]
    for label, state in (("before", before), ("after", after)):
        if state["commit"] is None:
            if any(
                state[field] is not None
                for field in (
                    "tracked_files_modified",
                    "untracked_files_present",
                    "working_tree_modified",
                    "status_entry_count",
                    "status_sha256",
                )
            ):
                raise ConformanceError(f"run.openada_checkout.{label} mixes unavailable Git state")
            continue
        available_values = (
            state["tracked_files_modified"],
            state["untracked_files_present"],
            state["working_tree_modified"],
            state["status_entry_count"],
            state["status_sha256"],
        )
        if any(value is None for value in available_values):
            raise ConformanceError(
                f"run.openada_checkout.{label} has a commit but incomplete Git state"
            )
        _expect_equal(
            state["working_tree_modified"],
            state["status_entry_count"] > 0,
            f"run.openada_checkout.{label}.working_tree_modified",
        )
        _expect_equal(
            state["working_tree_modified"],
            state["tracked_files_modified"] or state["untracked_files_present"],
            f"run.openada_checkout.{label}.status_classes",
        )
        if state["status_entry_count"] == 0:
            _expect_equal(
                state["status_sha256"],
                hashlib.sha256(b"").hexdigest(),
                f"run.openada_checkout.{label}.status_sha256",
            )
    available = before["commit"] is not None and after["commit"] is not None
    unchanged = before == after if available else None
    _expect_equal(checkout["state_unchanged"], unchanged, "run.openada_checkout.state_unchanged")
    expected_exact = bool(
        unchanged
        and before["working_tree_modified"] is False
        and after["working_tree_modified"] is False
    )
    _expect_equal(checkout["commit_exact"], expected_exact, "run.openada_checkout.commit_exact")


def _verify_container_command(command: Any, manifest: dict[str, Any]) -> None:
    if not isinstance(command, list) or len(command) != 45:
        raise ConformanceError(f"run.container_command has unexpected shape: {command!r}")
    if CONTAINER_NAME_RE.fullmatch(command[4]) is None:
        raise ConformanceError(f"run.container_command has invalid container name: {command[4]!r}")
    if re.fullmatch(r"[0-9]+:[0-9]+", command[18]) is None:
        raise ConformanceError(f"run.container_command has invalid user identity: {command[18]!r}")
    mounts = command[32], command[34], command[36]
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/openada,readonly", mounts[0]) is None:
        raise ConformanceError("OpenADA bind mount is not recorded read-only")
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/design,readonly", mounts[1]) is None:
        raise ConformanceError("design bind mount is not recorded read-only")
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/evidence", mounts[2]) is None:
        raise ConformanceError("evidence bind mount is not the reviewed writable target")
    expected = [
        command[0],
        "run",
        "--rm",
        "--name",
        command[4],
        "--pull=never",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--user",
        command[18],
        "--env",
        "HOME=/tmp/openada-home",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PDK_ROOT=/foss/pdks",
        "--env",
        "PDK=ihp-sg13g2",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--workdir",
        "/evidence",
        "--mount",
        mounts[0],
        "--mount",
        mounts[1],
        "--mount",
        mounts[2],
        "--entrypoint",
        "/usr/bin/python3",
        manifest["runtime"]["image"]["reference"],
        "/openada/conformance/ihp-inverter-ngspice/inside.py",
        "--manifest",
        "/openada/conformance/ihp-inverter-ngspice/manifest.json",
        "--evidence",
        "/evidence",
    ]
    _expect_equal(command, expected, "run.container_command")


def _expected_openada_invocations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = [
        "/usr/bin/python3",
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
    ]
    net = manifest["workflow"]["netlist"]["arguments"]
    sim = manifest["workflow"]["simulate"]["arguments"]
    return [
        {
            "operation": "netlist",
            "cwd": "/design/modules/module_0_foundations/inverter",
            "argv": [
                *prefix,
                "netlist",
                net["schematic"],
                "--output",
                net["output"],
                "--rcfile",
                net["rcfile"],
                "--timeout",
                str(net["timeout_seconds"]),
            ],
        },
        {
            "operation": "simulate",
            "cwd": "/evidence/work",
            "argv": [
                *prefix,
                "simulate",
                sim["spice_file"],
                "--output-dir",
                sim["output_dir"],
                "--workdir",
                sim["workdir"],
                "--execution-mode",
                sim["execution_mode"],
                "--expect-output",
                sim["expect_output"],
                "--init-file",
                sim["init_file"],
                "--system-init-file",
                sim["system_init_file"],
                "--timeout",
                str(sim["timeout_seconds"]),
            ],
        },
    ]


def _verify_run_metadata(
    manifest: dict[str, Any], metadata: dict[str, Any], manifest_sha256: str
) -> None:
    _expect_equal(metadata["conformance_id"], manifest["id"], "run.conformance_id")
    _expect_equal(
        metadata["conformance_manifest_sha256"], manifest_sha256, "run.conformance_manifest_sha256"
    )
    _expect_equal(metadata["design_revision"], manifest["design"]["revision"], "run.design_revision")
    _expect_equal(metadata["image"]["reference"], manifest["runtime"]["image"]["reference"], "run.image.reference")
    _expect_equal(metadata["image"]["id"], manifest["runtime"]["image"]["config_digest"], "run.image.id")
    _verify_checkout(metadata)
    _verify_container_command(metadata["container_command"], manifest)
    observation = metadata["runtime_observation"]
    pdk_observed = observation["pdk"]
    pdk_expected = manifest["runtime"]["pdk"]
    _expect_equal(pdk_observed["name"], pdk_expected["name"], "run.runtime_observation.pdk.name")
    _expect_equal(pdk_observed["revision"], pdk_expected["revision"], "run.runtime_observation.pdk.revision")
    for name in ("commit_file", "xschem_rcfile", "ngspice_init"):
        observed = pdk_observed[name]
        expected = pdk_expected[name]
        expected_keys = {"path", "bytes", "sha256", "value"} if name == "commit_file" else {
            "path",
            "bytes",
            "sha256",
        }
        _expect_equal(set(observed), expected_keys, f"run.runtime_observation.pdk.{name}.keys")
        _expect_equal(observed["path"], expected["path"], f"run.runtime_observation.pdk.{name}.path")
        _expect_equal(observed["sha256"], expected["sha256"], f"run.runtime_observation.pdk.{name}.sha256")
        if not isinstance(observed["bytes"], int) or observed["bytes"] <= 0:
            raise ConformanceError(f"run.runtime_observation.pdk.{name}.bytes must be positive")
    _expect_equal(
        pdk_observed["commit_file"]["value"],
        pdk_expected["revision"],
        "run.runtime_observation.pdk.commit_file.value",
    )
    system_init_observed = observation["ngspice_system_init"]
    system_init_expected = manifest["runtime"]["ngspice_system_init"]
    _expect_equal(
        set(system_init_observed),
        {"path", "bytes", "sha256"},
        "run.runtime_observation.ngspice_system_init.keys",
    )
    _expect_equal(
        system_init_observed["path"],
        system_init_expected["path"],
        "run.runtime_observation.ngspice_system_init.path",
    )
    _expect_equal(
        system_init_observed["sha256"],
        system_init_expected["sha256"],
        "run.runtime_observation.ngspice_system_init.sha256",
    )
    if not isinstance(system_init_observed["bytes"], int) or system_init_observed["bytes"] <= 0:
        raise ConformanceError("run.runtime_observation.ngspice_system_init.bytes must be positive")
    _expect_equal(
        observation["openada_invocations"],
        _expected_openada_invocations(manifest),
        "run.runtime_observation.openada_invocations",
    )
    _expect_equal(
        observation["completed_operations"],
        ["netlist", "simulate"],
        "run.runtime_observation.completed_operations",
    )


def _verify_evidence_tree(evidence: Path) -> None:
    try:
        root_mode = evidence.lstat().st_mode
    except OSError as exc:
        raise ConformanceError(f"cannot stat evidence directory {evidence}: {exc}") from exc
    if not stat.S_ISDIR(root_mode):
        raise ConformanceError(f"evidence path is not a real, non-symlink directory: {evidence}")
    expected_root = {"run.json", "netlist.json", "simulate.json", "work", "simulation"}
    _expect_equal({entry.name for entry in evidence.iterdir()}, expected_root, "evidence.root_entries")
    expected_children = {
        "work": {"inverter_tb.spice", "test_inverter.raw"},
        "simulation": {"inverter_tb.log", "inverter_tb.openada-control.sp"},
    }
    for directory_name, names in expected_children.items():
        directory = evidence / directory_name
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ConformanceError(f"evidence {directory_name} is not a real directory")
        _expect_equal({entry.name for entry in directory.iterdir()}, names, f"evidence.{directory_name}")


def verify_evidence(
    manifest: dict[str, Any], evidence: Path, *, manifest_sha256: str
) -> None:
    _verify_evidence_tree(evidence)
    evidence = evidence.resolve()
    result_validator = _load_validator(RESULT_SCHEMA_PATH, label="OpenADA result")
    run_validator = _load_validator(RUN_SCHEMA_PATH, label="conformance run")
    metadata = _read_json(evidence / "run.json", label="run metadata")
    _validate_schema(metadata, run_validator, label="run metadata")
    _verify_run_metadata(manifest, metadata, manifest_sha256)

    netlist = _read_json(evidence / "netlist.json", label="netlist result")
    simulation = _read_json(evidence / "simulate.json", label="simulate result")
    _validate_schema(netlist, result_validator, label="netlist result")
    _validate_schema(simulation, result_validator, label="simulate result")
    _, netlist_sha256 = _verify_netlist(manifest, netlist, evidence)
    raw_path = _verify_simulation(
        manifest,
        simulation,
        evidence,
        netlist_sha256,
        metadata["runtime_observation"],
    )
    raw = _parse_binary_raw(raw_path, manifest["waveform"])
    capture = simulation["data"]["output_captures"][0]
    validation = capture["validation"]
    _expect_equal(validation.get("reason"), "valid", "simulate.raw_validation.reason")
    metadata = validation["metadata"]
    _expect_equal(metadata.get("format"), "ngspice-raw", "simulate.raw_validation.format")
    _expect_equal(metadata.get("bytes"), raw_path.stat().st_size, "simulate.raw_validation.bytes")
    _expect_equal(metadata.get("plot_count"), 1, "simulate.raw_validation.plot_count")
    _expect_equal(metadata.get("analysis_plot_count"), 1, "simulate.raw_validation.analysis_plot_count")
    _expect_equal(metadata.get("has_analysis_plot"), True, "simulate.raw_validation.has_analysis_plot")
    plots = metadata.get("plots")
    if not isinstance(plots, list) or len(plots) != 1:
        raise ConformanceError("simulate.raw_validation.plots must contain exactly one plot")
    plot = plots[0]
    _expect_equal(plot.get("plotname"), "Transient Analysis", "simulate.raw_validation.plotname")
    _expect_equal(plot.get("encoding"), "binary", "simulate.raw_validation.encoding")
    _expect_equal(plot.get("numeric_type"), "real", "simulate.raw_validation.numeric_type")
    _expect_equal(plot.get("unpadded"), False, "simulate.raw_validation.unpadded")
    _expect_equal(plot.get("points"), raw["points"], "simulate.raw_validation.points")
    _expect_equal(plot.get("variables"), raw["variables"], "simulate.raw_validation.variables")
    expected_values = raw["points"] * raw["variables"]
    _expect_equal(plot.get("values"), expected_values, "simulate.raw_validation.values")
    _expect_equal(metadata.get("value_count"), expected_values, "simulate.raw_validation.value_count")
    _expect_equal(metadata.get("numeric_scalar_count"), expected_values, "simulate.raw_validation.numeric_scalar_count")
    row_match = re.search(r"No\. of Data Rows\s*:\s*([0-9]+)", simulation["data"]["log_tail"])
    if row_match is None or int(row_match.group(1)) != raw["points"]:
        raise ConformanceError("ngspice log row count does not match independently parsed raw data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify pinned IHP Xschem/ngspice conformance evidence."
    )
    parser.add_argument("evidence", type=Path, nargs="?")
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--manifest-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        if not args.manifest_only:
            if args.evidence is None:
                raise ConformanceError("an evidence directory is required unless --manifest-only is used")
            evidence = args.evidence.expanduser()
            if not evidence.is_absolute():
                evidence = Path.cwd() / evidence
            verify_evidence(manifest, evidence, manifest_sha256=sha256_file(manifest_path))
    except ConformanceError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    if args.manifest_only:
        print(f"Manifest verified: {manifest['id']}")
    else:
        print(f"Conformance verified: {manifest['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
