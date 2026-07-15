#!/usr/bin/env python3
"""Independently verify the pinned ngspice/Xyce portability proof."""

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
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "result-v0alpha1.schema.json"
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_LINE_BYTES = 65_536
RESULT_SCHEMA = "openada.result/v0alpha1"
RUN_SCHEMA = "openada.circuit-simulate-conformance-run/v0alpha1"


class ConformanceError(RuntimeError):
    """A pinned input, result, or native artifact violates the reviewed proof."""


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _require_regular(path: Path, *, label: str, maximum_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise ConformanceError(
            f"{label} size {metadata.st_size} is outside 1..{maximum_bytes} bytes"
        )
    return metadata.st_size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular(path, label=label, maximum_bytes=MAX_JSON_BYTES)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _read_json(path.resolve(), label="conformance manifest")
    _expect(
        set(manifest),
        {
            "schema",
            "id",
            "fixture",
            "runtime",
            "policy",
            "operation",
            "backends",
            "waveform",
        },
        "manifest.keys",
    )
    _expect(
        manifest["schema"],
        "openada.circuit-simulate-conformance/v0alpha1",
        "manifest.schema",
    )
    _expect(set(manifest["backends"]), {"ngspice", "xyce"}, "manifest.backends")
    _expect(manifest["policy"]["eda_network"], "none", "manifest.policy.eda_network")
    image = manifest["runtime"]["image"]
    if image["reference"] != f"{image['name']}@{image['manifest_digest']}":
        raise ConformanceError("manifest image reference is not bound to its digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["config_digest"]):
        raise ConformanceError("manifest image config digest is malformed")

    fixture = manifest["fixture"]
    fixture_path = REPOSITORY_ROOT / fixture["repository_path"]
    _require_regular(fixture_path, label="SPICE fixture", maximum_bytes=MAX_ARTIFACT_BYTES)
    _expect(_sha256(fixture_path), fixture["sha256"], "manifest.fixture.sha256")
    _expect(fixture["container_path"], f"/openada/{fixture['repository_path']}", "manifest.fixture.container_path")

    operation = manifest["operation"]
    _expect(operation["analysis"]["type"], "tran", "manifest.operation.analysis.type")
    if operation["timeout_seconds"] <= 0 or operation["container_timeout_seconds"] <= 0:
        raise ConformanceError("manifest timeouts must be positive")

    expected_roles = {"simulation.log", "simulation.result"}
    for backend, specification in manifest["backends"].items():
        _expect(specification["driver_id"], f"org.openada.driver.{backend}", f"manifest.backends.{backend}.driver_id")
        if specification["point_count"] <= 0:
            raise ConformanceError(f"manifest.backends.{backend}.point_count must be positive")
        roles = {item["role"] for item in specification["artifacts"]}
        _expect(roles, expected_roles, f"manifest.backends.{backend}.artifact_roles")
        for artifact in specification["artifacts"]:
            _expect(
                artifact["path"],
                f"/evidence/{artifact['filename']}",
                f"manifest.backends.{backend}.artifacts.path",
            )
    return manifest


def _read_line(handle: BinaryIO, consumed: int) -> tuple[bytes, int]:
    line = handle.readline(MAX_LINE_BYTES + 1)
    if len(line) > MAX_LINE_BYTES:
        raise ConformanceError("native raw header contains an overlong line")
    consumed += len(line)
    if consumed > MAX_HEADER_BYTES:
        raise ConformanceError("native raw header exceeds the verification bound")
    return line, consumed


def _read_raw_header(handle: BinaryIO) -> tuple[dict[str, str], list[str], int, int, int]:
    consumed = 0
    first, consumed = _read_line(handle, consumed)
    if not first.startswith(b"Title:"):
        raise ConformanceError("native raw file does not begin with a Title header")
    header: dict[str, str] = {}
    while True:
        line, consumed = _read_line(handle, consumed)
        if not line:
            raise ConformanceError("native raw header is truncated")
        if line.strip().lower() == b"variables:":
            break
        key, separator, value = line.partition(b":")
        if not separator:
            raise ConformanceError("native raw header contains an invalid line")
        name = b" ".join(key.strip().lower().split()).decode("ascii", errors="replace")
        if name in header:
            raise ConformanceError(f"native raw header repeats {name!r}")
        header[name] = value.strip().decode("utf-8", errors="replace")
    for field in ("plotname", "flags", "no. variables", "no. points"):
        if field not in header:
            raise ConformanceError(f"native raw header lacks {field!r}")
    _expect(header["plotname"], "Transient Analysis", "raw.plotname")
    _expect(header["flags"].casefold(), "real", "raw.flags")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except ValueError as exc:
        raise ConformanceError("native raw dimensions are not integers") from exc
    if not 2 <= variable_count <= 64 or not 1 <= point_count <= 1_000_000:
        raise ConformanceError("native raw dimensions exceed the proof bounds")
    variables: list[str] = []
    for index in range(variable_count):
        line, consumed = _read_line(handle, consumed)
        fields = line.decode("utf-8", errors="replace").split()
        if len(fields) < 3 or fields[0] != str(index):
            raise ConformanceError(f"native raw variable table is invalid at index {index}")
        variables.append(fields[1].casefold())
    if len(set(variables)) != len(variables):
        raise ConformanceError("native raw variable names are not unique")
    return header, variables, variable_count, point_count, consumed


def parse_ngspice_binary(path: Path) -> tuple[list[str], list[list[float]]]:
    size = _require_regular(path, label="ngspice binary raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    payload = path.read_bytes()
    if len(payload) != size:
        raise ConformanceError("ngspice raw changed while being read")
    handle = BytesIO(payload)
    _, variables, variable_count, point_count, consumed = _read_raw_header(handle)
    marker, consumed = _read_line(handle, consumed)
    if marker.strip().lower() != b"binary:":
        raise ConformanceError("ngspice raw is not binary encoded")
    binary = handle.read()
    expected_bytes = point_count * variable_count * 8
    if len(binary) != expected_bytes:
        raise ConformanceError(
            f"ngspice binary payload has {len(binary)} bytes, expected {expected_bytes}"
        )
    flat = struct.unpack(f"<{point_count * variable_count}d", binary)
    rows = [
        list(flat[index * variable_count : (index + 1) * variable_count])
        for index in range(point_count)
    ]
    return variables, rows


def parse_xyce_ascii(path: Path) -> tuple[list[str], list[list[float]]]:
    size = _require_regular(path, label="Xyce ASCII raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    payload = path.read_bytes()
    if len(payload) != size:
        raise ConformanceError("Xyce raw changed while being read")
    handle = BytesIO(payload)
    _, variables, variable_count, point_count, consumed = _read_raw_header(handle)
    marker, consumed = _read_line(handle, consumed)
    if marker.strip().lower() != b"values:":
        raise ConformanceError("Xyce raw is not ASCII Values encoded")
    try:
        value_lines = handle.read().decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ConformanceError("Xyce Values payload is not ASCII") from exc
    cursor = 0
    rows: list[list[float]] = []
    for point_index in range(point_count):
        while cursor < len(value_lines) and not value_lines[cursor].strip():
            cursor += 1
        if cursor >= len(value_lines):
            raise ConformanceError("Xyce Values payload is truncated")
        fields = value_lines[cursor].split()
        cursor += 1
        if len(fields) != 2 or fields[0] != str(point_index):
            raise ConformanceError(f"Xyce point index {point_index} is malformed")
        try:
            row = [float(fields[1])]
            for _ in range(variable_count - 1):
                if cursor >= len(value_lines):
                    raise ConformanceError("Xyce Values payload is truncated")
                value = value_lines[cursor].split()
                cursor += 1
                if len(value) != 1:
                    raise ConformanceError("Xyce dependent value line is malformed")
                row.append(float(value[0]))
        except ValueError as exc:
            raise ConformanceError("Xyce Values payload contains a non-number") from exc
        rows.append(row)
    if any(line.strip() for line in value_lines[cursor:]):
        raise ConformanceError("Xyce Values payload contains trailing records")
    return variables, rows


def _semantic_signal_name(native_name: str) -> str | None:
    return {
        "time": "time",
        "v(in)": "in",
        "in": "in",
        "v(out)": "out",
        "out": "out",
        "i(vstep)": "source_current",
        "vstep#branch": "source_current",
    }.get(native_name.casefold())


def verify_rc_waveform(
    variables: list[str],
    rows: list[list[float]],
    waveform: dict[str, Any],
    *,
    backend: str,
) -> dict[str, float | int]:
    if not rows or any(len(row) != len(variables) for row in rows):
        raise ConformanceError(f"{backend} waveform dimensions are inconsistent")
    if not all(math.isfinite(value) for row in rows for value in row):
        raise ConformanceError(f"{backend} waveform contains a non-finite value")
    mapping: dict[str, int] = {}
    for index, native_name in enumerate(variables):
        semantic_name = _semantic_signal_name(native_name)
        if semantic_name is not None:
            if semantic_name in mapping:
                raise ConformanceError(f"{backend} repeats semantic signal {semantic_name!r}")
            mapping[semantic_name] = index
    _expect(set(mapping), set(waveform["required_signals"]), f"{backend}.waveform.signals")
    columns = {
        name: [row[index] for row in rows]
        for name, index in mapping.items()
    }
    times = columns["time"]
    if not math.isclose(times[0], waveform["start_seconds"], rel_tol=0.0, abs_tol=1e-18):
        raise ConformanceError(f"{backend} transient does not start at zero")
    if not math.isclose(times[-1], waveform["stop_seconds"], rel_tol=0.0, abs_tol=5e-12):
        raise ConformanceError(f"{backend} transient stops at {times[-1]!r}")
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise ConformanceError(f"{backend} transient time is not strictly increasing")

    vin = columns["in"]
    vout = columns["out"]
    source_current = columns["source_current"]
    if not waveform["input_final_min"] <= vin[-1] <= waveform["input_final_max"]:
        raise ConformanceError(f"{backend} final input voltage is outside the reviewed range")
    if any(value < -1e-9 or value > 1.001 for value in vin):
        raise ConformanceError(f"{backend} input voltage leaves the reviewed source range")
    if any(value < -1e-9 or value > 1.001 for value in vout):
        raise ConformanceError(f"{backend} output voltage leaves the reviewed RC range")
    if any(current + 1e-8 < previous for previous, current in zip(vout, vout[1:])):
        raise ConformanceError(f"{backend} RC output is not monotonic")
    for index, (input_value, output_value, current) in enumerate(
        zip(vin, vout, source_current)
    ):
        expected_current = -(input_value - output_value) / 1000.0
        if not math.isclose(current, expected_current, rel_tol=2e-5, abs_tol=2e-9):
            raise ConformanceError(
                f"{backend} source current violates the 1 kOhm branch relation at point {index}"
            )

    midpoint_index = min(
        range(len(times)), key=lambda index: abs(times[index] - waveform["midpoint_seconds"])
    )
    midpoint = vout[midpoint_index]
    final = vout[-1]
    if not waveform["output_midpoint_min"] <= midpoint <= waveform["output_midpoint_max"]:
        raise ConformanceError(f"{backend} midpoint RC response is outside the reviewed range")
    if not waveform["output_final_min"] <= final <= waveform["output_final_max"]:
        raise ConformanceError(f"{backend} final RC response is outside the reviewed range")
    return {
        "points": len(rows),
        "midpoint_time": times[midpoint_index],
        "midpoint_output": midpoint,
        "final_output": final,
    }


def _validate_result_schema(result: dict[str, Any], *, backend: str) -> None:
    schema = _read_json(RESULT_SCHEMA_PATH, label="OpenADA result schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(result),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(
            f"{backend} result violates the public schema at {location}: {error.message}"
        )


def _verify_record(
    record: dict[str, Any],
    expected: dict[str, Any],
    local_path: Path,
    *,
    location: str,
) -> None:
    _expect(record.get("path"), expected["path"], f"{location}.path")
    _expect(record.get("kind"), expected["kind"], f"{location}.kind")
    _expect(record.get("role"), expected["role"], f"{location}.role")
    _expect(record.get("exists"), True, f"{location}.exists")
    size = _require_regular(local_path, label=location, maximum_bytes=MAX_ARTIFACT_BYTES)
    _expect(record.get("bytes"), size, f"{location}.bytes")
    _expect(record.get("sha256"), _sha256(local_path), f"{location}.sha256")


def _verify_native_command(
    command: Any,
    manifest: dict[str, Any],
    backend: str,
) -> None:
    fixture = re.escape(manifest["fixture"]["container_path"])
    tool_path = re.escape(manifest["backends"][backend]["tool"]["path"])
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ConformanceError(f"{backend}.execution.command must be an argv array")
    joined = "\0".join(command)
    if backend == "ngspice":
        pattern = (
            rf"{tool_path}\0-b\0-r\0/tmp/openada-ngspice-[^/\0]+/simulation\.raw"
            rf"\0-o\0/tmp/openada-ngspice-[^/\0]+/simulation\.log\0{fixture}"
        )
    else:
        pattern = (
            rf"{tool_path}\0-l\0/tmp/openada-xyce-[^/\0]+/simulation\.log"
            rf"\0-r\0/tmp/openada-xyce-[^/\0]+/simulation\.raw\0-a\0{fixture}"
        )
    if re.fullmatch(pattern, joined) is None:
        raise ConformanceError(f"{backend}.execution.command differs from the reviewed argv")


def _verify_backend_result(
    manifest: dict[str, Any],
    evidence: Path,
    backend: str,
) -> dict[str, float | int]:
    specification = manifest["backends"][backend]
    result_path = evidence / specification["result_filename"]
    result = _read_json(result_path, label=f"{backend} result")
    _validate_result_schema(result, backend=backend)
    _expect(result["schema"], RESULT_SCHEMA, f"{backend}.schema")
    _expect(result["operation"], "simulate", f"{backend}.operation")
    _expect(result["tool"]["name"], backend, f"{backend}.tool.name")
    _expect(result["tool"]["path"], specification["tool"]["path"], f"{backend}.tool.path")
    _expect(result["tool"]["version"], specification["tool"]["version"], f"{backend}.tool.version")
    _expect(result["execution"]["status"], "completed", f"{backend}.execution.status")
    _expect(result["execution"]["exit_code"], 0, f"{backend}.execution.exit_code")
    _expect(result["execution"]["cwd"], str(Path(manifest["fixture"]["container_path"]).parent), f"{backend}.execution.cwd")
    _verify_native_command(result["execution"]["command"], manifest, backend)
    _expect(result["engineering"]["status"], "pass", f"{backend}.engineering.status")
    if result["diagnostics"]:
        raise ConformanceError(f"{backend} result contains diagnostics: {result['diagnostics']!r}")

    inputs = result["inputs"]
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise ConformanceError(f"{backend}.inputs must contain exactly the fixture")
    fixture_record = inputs[0]
    _expect(fixture_record.get("path"), manifest["fixture"]["container_path"], f"{backend}.input.path")
    _expect(fixture_record.get("kind"), "spice-netlist", f"{backend}.input.kind")
    _expect(fixture_record.get("role"), "input", f"{backend}.input.role")
    _expect(fixture_record.get("exists"), True, f"{backend}.input.exists")
    fixture_path = REPOSITORY_ROOT / manifest["fixture"]["repository_path"]
    _expect(fixture_record.get("bytes"), fixture_path.stat().st_size, f"{backend}.input.bytes")
    _expect(fixture_record.get("sha256"), manifest["fixture"]["sha256"], f"{backend}.input.sha256")

    artifacts = result["artifacts"]
    expected_artifacts = {item["path"]: item for item in specification["artifacts"]}
    actual_artifacts = {item["path"]: item for item in artifacts}
    if len(actual_artifacts) != len(artifacts):
        raise ConformanceError(f"{backend}.artifacts contains a duplicate path")
    _expect(set(actual_artifacts), set(expected_artifacts), f"{backend}.artifacts.paths")
    for path, expected in expected_artifacts.items():
        _verify_record(
            actual_artifacts[path],
            expected,
            evidence / expected["filename"],
            location=f"{backend}.artifacts[{path}]",
        )

    data = result["data"]
    protocol = data["protocol"]
    _expect(protocol["operation_profile"], manifest["operation"]["profile"], f"{backend}.protocol.operation_profile")
    _expect(protocol["assertion_profile"], manifest["operation"]["assertion"], f"{backend}.protocol.assertion_profile")
    _expect(protocol["driver_id"], specification["driver_id"], f"{backend}.protocol.driver_id")
    _expect(protocol["driver_version"], specification["driver_version"], f"{backend}.protocol.driver_version")
    analysis = data["analysis"]
    _expect(analysis["type"], "tran", f"{backend}.analysis.type")
    _expect(analysis["completion"], "completed", f"{backend}.analysis.completion")
    _expect(analysis["convergence"], "converged", f"{backend}.analysis.convergence")
    _expect(analysis["point_count"], specification["point_count"], f"{backend}.analysis.point_count")
    _expect(analysis["dependent_variable_count"], 3, f"{backend}.analysis.dependent_variable_count")
    _expect(analysis["finite_value_count"], specification["point_count"] * 3, f"{backend}.analysis.finite_value_count")
    normalized_evidence = data["evidence"]
    for field, expected in {
        "request_binding": "exact",
        "freshness": "fresh",
        "structure": "valid",
        "artifact_roles_present": ["simulation.log", "simulation.result"],
        "provenance": "bounded",
    }.items():
        _expect(normalized_evidence[field], expected, f"{backend}.evidence.{field}")

    extension = data["extensions"]["org.openada"]
    _expect(extension["backend"], backend, f"{backend}.extension.backend")
    parameters = extension["parameters"]["analysis"]
    _expect(parameters["type"], "tran", f"{backend}.parameters.analysis.type")
    if not math.isclose(
        parameters["step_s"],
        manifest["operation"]["analysis"]["step_seconds"],
        rel_tol=1e-12,
        abs_tol=0.0,
    ):
        raise ConformanceError(f"{backend}.parameters.analysis.step_s differs from the request")
    if not math.isclose(
        parameters["stop_s"],
        manifest["operation"]["analysis"]["stop_seconds"],
        rel_tol=1e-12,
        abs_tol=0.0,
    ):
        raise ConformanceError(f"{backend}.parameters.analysis.stop_s differs from the request")
    native = extension["native_data"]
    _expect(native["converged"], True, f"{backend}.native.converged")
    _expect(native["inputs_stable"], True, f"{backend}.native.inputs_stable")
    captures = native["output_captures"]
    if not isinstance(captures, list) or len(captures) != 1:
        raise ConformanceError(f"{backend}.native.output_captures must contain one raw file")
    capture = captures[0]
    raw_expected = next(item for item in specification["artifacts"] if item["role"] == "simulation.result")
    raw_path = evidence / raw_expected["filename"]
    _expect(capture["path"], raw_expected["path"], f"{backend}.native.capture.path")
    _expect(capture["status"], "valid", f"{backend}.native.capture.status")
    _expect(capture["bytes"], raw_path.stat().st_size, f"{backend}.native.capture.bytes")
    _expect(capture["sha256"], _sha256(raw_path), f"{backend}.native.capture.sha256")
    _expect(capture["validation"]["valid"], True, f"{backend}.native.capture.validation")

    log_expected = next(item for item in specification["artifacts"] if item["role"] == "simulation.log")
    log_path = evidence / log_expected["filename"]
    log_capture = native["log_capture"]
    _expect(log_capture["path"], log_expected["path"], f"{backend}.native.log_capture.path")
    _expect(log_capture["status"], "valid", f"{backend}.native.log_capture.status")
    _expect(log_capture["bytes"], log_path.stat().st_size, f"{backend}.native.log_capture.bytes")
    _expect(log_capture["sha256"], _sha256(log_path), f"{backend}.native.log_capture.sha256")
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if backend == "xyce":
        if "End of Xyce(TM) Simulation" not in log_text:
            raise ConformanceError("Xyce log lacks its native completion marker")
    elif "No. of Data Rows" not in log_text:
        raise ConformanceError("ngspice log lacks its native data-row record")
    if re.search(r"(?:fatal error|failed to converge|time step too small|xyce abort)", log_text, re.I):
        raise ConformanceError(f"{backend} log contains native failure evidence")

    if specification["raw_encoding"] == "binary":
        variables, rows = parse_ngspice_binary(raw_path)
    else:
        variables, rows = parse_xyce_ascii(raw_path)
    _expect(len(rows), specification["point_count"], f"{backend}.native.point_count")
    return verify_rc_waveform(variables, rows, manifest["waveform"], backend=backend)


def _option_value(command: list[str], option: str) -> str:
    indices = [index for index, value in enumerate(command) if value == option]
    if len(indices) != 1 or indices[0] + 1 >= len(command):
        raise ConformanceError(f"container command must contain one {option}")
    return command[indices[0] + 1]


def _verify_container_command(command: Any, manifest: dict[str, Any], backend: str) -> None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ConformanceError(f"run.container_commands.{backend} must be an argv array")
    for flag in ("--rm", "--pull=never", "--read-only"):
        if command.count(flag) != 1:
            raise ConformanceError(f"{backend} container command must contain {flag}")
    _expect(_option_value(command, "--network"), "none", f"{backend}.container.network")
    _expect(_option_value(command, "--platform"), manifest["runtime"]["image"]["platform"], f"{backend}.container.platform")
    _expect(_option_value(command, "--cap-drop"), "ALL", f"{backend}.container.cap_drop")
    _expect(_option_value(command, "--security-opt"), "no-new-privileges", f"{backend}.container.security")
    _expect(_option_value(command, "--pids-limit"), "512", f"{backend}.container.pids_limit")
    _expect(_option_value(command, "--tmpfs"), "/tmp:rw,nosuid,nodev,size=256m", f"{backend}.container.tmpfs")
    _expect(
        _option_value(command, "--workdir"),
        str(Path(manifest["fixture"]["container_path"]).parent),
        f"{backend}.container.workdir",
    )
    _expect(_option_value(command, "--entrypoint"), "/usr/bin/python3", f"{backend}.container.entrypoint")
    container_name = _option_value(command, "--name")
    if re.fullmatch(
        rf"openada-circuit-{backend}-[1-9][0-9]*-[0-9a-f]{{8}}",
        container_name,
    ) is None:
        raise ConformanceError(f"{backend} container name is malformed")
    if re.fullmatch(r"[0-9]+:[0-9]+", _option_value(command, "--user")) is None:
        raise ConformanceError(f"{backend} container user is malformed")
    environments = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--env"
    ]
    _expect(
        environments,
        ["HOME=/tmp/openada-home", "TMPDIR=/tmp"],
        f"{backend}.container.environment",
    )
    mounts = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--mount"]
    if len(mounts) != 2:
        raise ConformanceError(f"{backend} container command must contain two bind mounts")
    if not any(re.fullmatch(r"type=bind,source=/[^,]+,target=/openada,readonly", item) for item in mounts):
        raise ConformanceError(f"{backend} OpenADA bind mount is not read-only")
    if not any(re.fullmatch(r"type=bind,source=/[^,]+,target=/evidence", item) for item in mounts):
        raise ConformanceError(f"{backend} evidence bind mount is not the reviewed writable target")
    required_tail = [
        manifest["runtime"]["image"]["reference"],
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
        "simulate",
        manifest["fixture"]["container_path"],
        "--backend",
        backend,
        "--output-dir",
        manifest["backends"][backend]["output_directory"],
        "--timeout",
        str(manifest["operation"]["timeout_seconds"]),
    ]
    if command[-len(required_tail) :] != required_tail:
        raise ConformanceError(f"{backend} container command has an unexpected OpenADA invocation")


def verify_evidence(
    evidence: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)
    evidence = evidence.expanduser().resolve()
    run = _read_json(evidence / "run.json", label="conformance run metadata")
    _expect(run["schema"], RUN_SCHEMA, "run.schema")
    _expect(run["conformance_id"], manifest["id"], "run.conformance_id")
    _expect(run["manifest_sha256"], _sha256(manifest_path), "run.manifest_sha256")
    image = manifest["runtime"]["image"]
    _expect(run["image"]["reference"], image["reference"], "run.image.reference")
    _expect(run["image"]["id"], image["config_digest"], "run.image.id")
    _expect(run["image"]["os"], "linux", "run.image.os")
    _expect(run["image"]["architecture"], "amd64", "run.image.architecture")
    _expect(run["network"], "none during EDA execution", "run.network")
    _expect(set(run["container_commands"]), {"ngspice", "xyce"}, "run.container_commands")
    for backend in ("ngspice", "xyce"):
        _verify_container_command(run["container_commands"][backend], manifest, backend)

    summaries = {
        backend: _verify_backend_result(manifest, evidence, backend)
        for backend in ("ngspice", "xyce")
    }
    waveform = manifest["waveform"]
    final_difference = abs(
        float(summaries["ngspice"]["final_output"])
        - float(summaries["xyce"]["final_output"])
    )
    midpoint_difference = abs(
        float(summaries["ngspice"]["midpoint_output"])
        - float(summaries["xyce"]["midpoint_output"])
    )
    if final_difference > waveform["backend_final_tolerance"]:
        raise ConformanceError("backend final RC outputs differ beyond the reviewed tolerance")
    if midpoint_difference > waveform["backend_midpoint_tolerance"]:
        raise ConformanceError("backend midpoint RC outputs differ beyond the reviewed tolerance")
    return {
        "schema": "openada.circuit-simulate-conformance-verification/v0alpha1",
        "conformance_id": manifest["id"],
        "status": "pass",
        "backends": summaries,
        "comparison": {
            "final_output_difference": final_difference,
            "midpoint_output_difference": midpoint_difference,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="?", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--manifest-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(arguments.manifest)
        if arguments.manifest_only:
            print(json.dumps({"status": "pass", "conformance_id": manifest["id"]}, sort_keys=True))
            return 0
        if arguments.evidence is None:
            raise ConformanceError("an evidence directory is required unless --manifest-only is used")
        print(json.dumps(verify_evidence(arguments.evidence, manifest_path=arguments.manifest), indent=2, sort_keys=True))
        return 0
    except (ConformanceError, KeyError, TypeError, ValueError) as exc:
        print(f"conformance verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
