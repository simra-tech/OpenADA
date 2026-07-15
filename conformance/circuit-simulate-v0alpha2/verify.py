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
RUN_SCHEMA = "openada.circuit-simulate-conformance-run/v0alpha2"


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
            "contracts",
            "fixture",
            "runtime",
            "policy",
            "operation",
            "backends",
            "capability_cases",
            "waveform",
        },
        "manifest.keys",
    )
    _expect(
        manifest["schema"],
        "openada.circuit-simulate-conformance/v0alpha2",
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

    contracts = manifest["contracts"]
    _expect(set(contracts), {"operation_profile"}, "manifest.contracts")
    profile_contract = contracts["operation_profile"]
    _expect(
        set(profile_contract),
        {"id", "repository_path", "sha256"},
        "manifest.contracts.operation_profile.keys",
    )
    _expect(
        profile_contract["id"],
        operation["profile"],
        "manifest.contracts.operation_profile.id",
    )
    repository_path = profile_contract["repository_path"]
    if (
        not isinstance(repository_path, str)
        or not repository_path
        or Path(repository_path).is_absolute()
    ):
        raise ConformanceError(
            "manifest.contracts.operation_profile.repository_path must be repository-relative"
        )
    profile_path = (REPOSITORY_ROOT / repository_path).resolve()
    try:
        profile_path.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise ConformanceError(
            "manifest.contracts.operation_profile.repository_path escapes the repository"
        ) from exc
    _require_regular(
        profile_path,
        label="circuit.simulate operation profile",
        maximum_bytes=MAX_JSON_BYTES,
    )
    profile_sha256 = profile_contract["sha256"]
    if not isinstance(profile_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", profile_sha256
    ) is None:
        raise ConformanceError(
            "manifest.contracts.operation_profile.sha256 is malformed"
        )
    _expect(
        _sha256(profile_path),
        profile_sha256,
        "manifest.contracts.operation_profile.sha256",
    )
    profile = _read_json(profile_path, label="circuit.simulate operation profile")
    _expect(
        profile["operation"]["id"],
        operation["profile"],
        "operation_profile.operation.id",
    )
    _expect(
        profile["assertion"]["id"],
        operation["assertion"],
        "operation_profile.assertion.id",
    )

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

    cases = manifest["capability_cases"]
    _expect(set(cases), {"op", "dc", "ac"}, "manifest.capability_cases")
    expected_case_backends = {
        "op": {"ngspice"},
        "dc": {"ngspice", "xyce"},
        "ac": {"ngspice", "xyce"},
    }
    for analysis_type, case in cases.items():
        _expect(
            set(case),
            {"fixture", "parameters", "backends"},
            f"manifest.capability_cases.{analysis_type}.keys",
        )
        case_fixture = case["fixture"]
        case_fixture_path = REPOSITORY_ROOT / case_fixture["repository_path"]
        _require_regular(
            case_fixture_path,
            label=f"{analysis_type} SPICE fixture",
            maximum_bytes=MAX_ARTIFACT_BYTES,
        )
        _expect(
            _sha256(case_fixture_path),
            case_fixture["sha256"],
            f"manifest.capability_cases.{analysis_type}.fixture.sha256",
        )
        _expect(
            case_fixture["container_path"],
            f"/openada/{case_fixture['repository_path']}",
            f"manifest.capability_cases.{analysis_type}.fixture.container_path",
        )
        _expect(
            case["parameters"]["analysis"]["type"],
            analysis_type,
            f"manifest.capability_cases.{analysis_type}.parameters.analysis.type",
        )
        _expect(
            set(case["backends"]),
            expected_case_backends[analysis_type],
            f"manifest.capability_cases.{analysis_type}.backends",
        )
        for backend, specification in case["backends"].items():
            for count_name in (
                "point_count",
                "dependent_variable_count",
                "finite_value_count",
            ):
                count = specification[count_name]
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    raise ConformanceError(
                        f"manifest.capability_cases.{analysis_type}.backends.{backend}."
                        f"{count_name} must be positive"
                    )
            _expect(
                {item["role"] for item in specification["artifacts"]},
                expected_roles,
                f"manifest.capability_cases.{analysis_type}.backends.{backend}.artifact_roles",
            )
            for artifact in specification["artifacts"]:
                _expect(
                    artifact["path"],
                    f"/evidence/{artifact['filename']}",
                    f"manifest.capability_cases.{analysis_type}.backends.{backend}.artifacts.path",
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


def _plotname_matches(plotname: str, analysis_type: str) -> bool:
    normalized = " ".join(plotname.strip().casefold().split())
    if analysis_type == "op":
        return normalized == "operating point"
    if analysis_type == "dc":
        return normalized == "dc transfer characteristic" or (
            normalized.startswith("dc sweep:")
            and normalized.endswith("dc transfer characteristic")
        )
    if analysis_type == "ac":
        return normalized == "ac analysis"
    return analysis_type == "tran" and normalized == "transient analysis"


def _read_raw_header(
    handle: BinaryIO,
    *,
    analysis_type: str,
) -> tuple[dict[str, str], list[str], int, int, int]:
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
    if not _plotname_matches(header["plotname"], analysis_type):
        raise ConformanceError(
            f"raw.plotname {header['plotname']!r} does not prove {analysis_type!r}"
        )
    expected_flags = "complex" if analysis_type == "ac" else "real"
    _expect(header["flags"].casefold(), expected_flags, "raw.flags")
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


def parse_ngspice_binary(
    path: Path,
    *,
    analysis_type: str = "tran",
) -> tuple[list[str], list[list[float | complex]]]:
    size = _require_regular(path, label="ngspice binary raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    payload = path.read_bytes()
    if len(payload) != size:
        raise ConformanceError("ngspice raw changed while being read")
    handle = BytesIO(payload)
    header, variables, variable_count, point_count, consumed = _read_raw_header(
        handle,
        analysis_type=analysis_type,
    )
    marker, consumed = _read_line(handle, consumed)
    if marker.strip().lower() != b"binary:":
        raise ConformanceError("ngspice raw is not binary encoded")
    binary = handle.read()
    scalar_width = 2 if header["flags"].casefold() == "complex" else 1
    expected_bytes = point_count * variable_count * scalar_width * 8
    if len(binary) != expected_bytes:
        raise ConformanceError(
            f"ngspice binary payload has {len(binary)} bytes, expected {expected_bytes}"
        )
    flat = struct.unpack(f"<{point_count * variable_count * scalar_width}d", binary)
    rows: list[list[float | complex]] = []
    row_width = variable_count * scalar_width
    for index in range(point_count):
        scalars = flat[index * row_width : (index + 1) * row_width]
        if scalar_width == 1:
            rows.append(list(scalars))
        else:
            rows.append(
                [complex(scalars[offset], scalars[offset + 1]) for offset in range(0, row_width, 2)]
            )
    return variables, rows


def _ascii_value(token: str, *, complex_values: bool) -> float | complex:
    stripped = token.strip().strip("()")
    if complex_values:
        fields = stripped.split(",")
        if len(fields) != 2:
            raise ValueError("complex raw value must contain real and imaginary fields")
        return complex(float(fields[0]), float(fields[1]))
    return float(stripped)


def parse_xyce_ascii(
    path: Path,
    *,
    analysis_type: str = "tran",
) -> tuple[list[str], list[list[float | complex]]]:
    size = _require_regular(path, label="Xyce ASCII raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    payload = path.read_bytes()
    if len(payload) != size:
        raise ConformanceError("Xyce raw changed while being read")
    handle = BytesIO(payload)
    header, variables, variable_count, point_count, consumed = _read_raw_header(
        handle,
        analysis_type=analysis_type,
    )
    marker, consumed = _read_line(handle, consumed)
    if marker.strip().lower() != b"values:":
        raise ConformanceError("Xyce raw is not ASCII Values encoded")
    try:
        value_lines = handle.read().decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ConformanceError("Xyce Values payload is not ASCII") from exc
    cursor = 0
    complex_values = header["flags"].casefold() == "complex"
    rows: list[list[float | complex]] = []
    for point_index in range(point_count):
        while cursor < len(value_lines) and not value_lines[cursor].strip():
            cursor += 1
        if cursor >= len(value_lines):
            raise ConformanceError("Xyce Values payload is truncated")
        fields = value_lines[cursor].strip().split(maxsplit=1)
        cursor += 1
        if len(fields) != 2 or fields[0] != str(point_index):
            raise ConformanceError(f"Xyce point index {point_index} is malformed")
        try:
            row = [_ascii_value(fields[1], complex_values=complex_values)]
            for _ in range(variable_count - 1):
                if cursor >= len(value_lines):
                    raise ConformanceError("Xyce Values payload is truncated")
                value = value_lines[cursor].strip()
                cursor += 1
                if not value:
                    raise ConformanceError("Xyce dependent value line is malformed")
                row.append(_ascii_value(value, complex_values=complex_values))
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


def _finite_native_value(value: float | complex) -> bool:
    if isinstance(value, complex):
        return math.isfinite(value.real) and math.isfinite(value.imag)
    return math.isfinite(value)


def _node_columns(variables: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, name in enumerate(variables):
        semantic = {
            "v(in)": "in",
            "in": "in",
            "v(out)": "out",
            "out": "out",
        }.get(name.casefold())
        if semantic is not None:
            if semantic in columns:
                raise ConformanceError(f"native result repeats semantic node {semantic!r}")
            columns[semantic] = index
    _expect(set(columns), {"in", "out"}, "native.semantic_nodes")
    return columns


def _axis_value_matches(observed: float, expected: float) -> bool:
    """Use the same numeric tolerance as the runtime request binder."""

    return math.isclose(
        observed,
        expected,
        rel_tol=1e-8,
        abs_tol=max(1e-15, abs(expected) * 1e-12),
    )


def _grid_intervals(span_in_steps: float) -> int:
    """Mirror the native-output binder's tolerance around integral spans."""

    if not math.isfinite(span_in_steps) or span_in_steps < 0:
        raise ConformanceError("sweep span does not define a finite positive grid")
    nearest = round(span_in_steps)
    if math.isclose(span_in_steps, nearest, rel_tol=1e-11, abs_tol=1e-11):
        return int(nearest)
    return math.floor(span_in_steps)


def _expected_dc_axis(analysis: dict[str, Any]) -> list[float]:
    try:
        start = float(analysis["start"])
        stop = float(analysis["stop"])
        step = float(analysis["step"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ConformanceError("DC sweep parameters are not finite numbers") from exc
    if not all(math.isfinite(value) for value in (start, stop, step)):
        raise ConformanceError("DC sweep parameters are not finite numbers")
    if stop <= start or step <= 0:
        raise ConformanceError("DC sweep bounds do not define an increasing grid")
    intervals = _grid_intervals((stop - start) / step)
    return [start + index * step for index in range(intervals + 1)]


def _expected_ac_axis(analysis: dict[str, Any], *, backend: str) -> list[float]:
    try:
        start = float(analysis["start_hz"])
        stop = float(analysis["stop_hz"])
        points = analysis["points"]
        sweep = analysis["sweep"]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ConformanceError("AC sweep parameters are malformed") from exc
    if (
        not math.isfinite(start)
        or not math.isfinite(stop)
        or start <= 0
        or stop <= start
        or isinstance(points, bool)
        or not isinstance(points, int)
        or points <= 0
        or sweep not in {"lin", "dec", "oct"}
        or backend not in {"ngspice", "xyce"}
    ):
        raise ConformanceError("AC sweep parameters do not define a supported grid")

    if sweep == "lin":
        if points == 1:
            return [start]
        step = (stop - start) / (points - 1)
        return [start + index * step for index in range(points)]

    base = 10.0 if sweep == "dec" else 2.0
    intervals = _grid_intervals(points * math.log(stop / start, base))
    if intervals == 0:
        return [start]
    if sweep == "dec" and backend == "ngspice":
        # ngspice retains the floor-derived point count for DEC but
        # redistributes the points uniformly in log space to land on stop.
        ratio = (stop / start) ** (1.0 / intervals)
    else:
        # Xyce DEC and both backends' OCT sweeps retain the declared
        # points-per-decade/octave ratio, so a non-aligned stop is not emitted.
        ratio = base ** (1.0 / points)
    return [start * ratio**index for index in range(intervals + 1)]


def _verify_sweep_axis(
    observed: list[float],
    expected: list[float],
    *,
    backend: str,
    analysis_type: str,
) -> None:
    if len(observed) != len(expected):
        raise ConformanceError(
            f"{backend} {analysis_type.upper()} sweep grid has {len(observed)} points, "
            f"expected {len(expected)}"
        )
    for index, (actual, requested) in enumerate(zip(observed, expected)):
        if not _axis_value_matches(actual, requested):
            raise ConformanceError(
                f"{backend} {analysis_type.upper()} sweep grid differs from the "
                f"declared {analysis_type.upper()} grid at point {index}: "
                f"expected {requested!r}, got {actual!r}"
            )


def verify_analysis_fixture(
    variables: list[str],
    rows: list[list[float | complex]],
    parameters: dict[str, Any],
    expected: dict[str, Any],
    *,
    backend: str,
) -> dict[str, float | int | str]:
    analysis = parameters["analysis"]
    analysis_type = analysis["type"]
    if not rows or any(len(row) != len(variables) for row in rows):
        raise ConformanceError(f"{backend} {analysis_type} result dimensions are inconsistent")
    if not all(_finite_native_value(value) for row in rows for value in row):
        raise ConformanceError(f"{backend} {analysis_type} result contains a non-finite value")
    _expect(len(rows), expected["point_count"], f"{backend}.{analysis_type}.point_count")
    dependent_count = len(variables) if analysis_type == "op" else len(variables) - 1
    _expect(
        dependent_count,
        expected["dependent_variable_count"],
        f"{backend}.{analysis_type}.dependent_variable_count",
    )
    finite_count = len(rows) * dependent_count * (2 if analysis_type == "ac" else 1)
    _expect(
        finite_count,
        expected["finite_value_count"],
        f"{backend}.{analysis_type}.finite_value_count",
    )
    columns = _node_columns(variables)

    if analysis_type == "op":
        vin = float(rows[0][columns["in"]])
        vout = float(rows[0][columns["out"]])
        if not math.isclose(vin, 1.0, rel_tol=1e-10, abs_tol=1e-12):
            raise ConformanceError(f"{backend} OP input does not equal the 1 V fixture bias")
        if not math.isclose(vout, 0.5, rel_tol=1e-8, abs_tol=1e-10):
            raise ConformanceError(f"{backend} OP output violates the resistor-divider relation")
        return {"analysis": analysis_type, "points": len(rows), "output": vout}

    axis = [complex(row[0]).real for row in rows]
    if any(current <= previous for previous, current in zip(axis, axis[1:])):
        raise ConformanceError(f"{backend} {analysis_type} axis is not strictly increasing")
    if analysis_type == "dc":
        _verify_sweep_axis(
            axis,
            _expected_dc_axis(analysis),
            backend=backend,
            analysis_type="dc",
        )
        for index, row in enumerate(rows):
            vin = float(row[columns["in"]])
            vout = float(row[columns["out"]])
            if not math.isclose(vin, axis[index], rel_tol=1e-9, abs_tol=1e-12):
                raise ConformanceError(f"{backend} DC source value differs from its sweep axis")
            if not math.isclose(vout, vin / 2.0, rel_tol=1e-8, abs_tol=1e-10):
                raise ConformanceError(f"{backend} DC output violates the resistor-divider relation")
        return {
            "analysis": analysis_type,
            "points": len(rows),
            "axis_first": axis[0],
            "axis_last": axis[-1],
        }

    if analysis_type != "ac":
        raise ConformanceError(f"unsupported capability fixture analysis: {analysis_type!r}")
    _verify_sweep_axis(
        axis,
        _expected_ac_axis(analysis, backend=backend),
        backend=backend,
        analysis_type="ac",
    )
    for index, row in enumerate(rows):
        vin = complex(row[columns["in"]])
        vout = complex(row[columns["out"]])
        if not math.isclose(vin.real, 1.0, rel_tol=1e-8, abs_tol=1e-10) or not math.isclose(
            vin.imag,
            0.0,
            rel_tol=0.0,
            abs_tol=1e-10,
        ):
            raise ConformanceError(f"{backend} AC source differs from the declared unit phasor")
        expected_transfer = 1.0 / complex(1.0, 2.0 * math.pi * axis[index] * 1e-3)
        if not math.isclose(vout.real, expected_transfer.real, rel_tol=2e-6, abs_tol=2e-8) or not math.isclose(
            vout.imag,
            expected_transfer.imag,
            rel_tol=2e-6,
            abs_tol=2e-8,
        ):
            raise ConformanceError(f"{backend} AC output violates the reviewed RC transfer function")
    return {
        "analysis": analysis_type,
        "points": len(rows),
        "axis_first": axis[0],
        "axis_last": axis[-1],
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
    *,
    fixture: dict[str, Any] | None = None,
) -> None:
    fixture_path = re.escape((fixture or manifest["fixture"])["container_path"])
    tool_path = re.escape(manifest["backends"][backend]["tool"]["path"])
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ConformanceError(f"{backend}.execution.command must be an argv array")
    joined = "\0".join(command)
    if backend == "ngspice":
        pattern = (
            rf"{tool_path}\0-b\0-r\0/tmp/openada-ngspice-[^/\0]+/simulation\.raw"
            rf"\0-o\0/tmp/openada-ngspice-[^/\0]+/simulation\.log\0{fixture_path}"
        )
    else:
        pattern = (
            rf"{tool_path}\0-l\0/tmp/openada-xyce-[^/\0]+/simulation\.log"
            rf"\0-r\0/tmp/openada-xyce-[^/\0]+/simulation\.raw\0-a\0{fixture_path}"
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


def _verify_capability_result(
    manifest: dict[str, Any],
    evidence: Path,
    analysis_type: str,
    backend: str,
) -> dict[str, float | int | str]:
    case = manifest["capability_cases"][analysis_type]
    fixture = case["fixture"]
    specification = case["backends"][backend]
    backend_identity = manifest["backends"][backend]
    location = f"capability_cases.{analysis_type}.{backend}"
    result_path = evidence / specification["result_filename"]
    result = _read_json(result_path, label=f"{analysis_type} {backend} result")
    _validate_result_schema(result, backend=f"{analysis_type}.{backend}")
    _expect(result["schema"], RESULT_SCHEMA, f"{location}.schema")
    _expect(result["operation"], "simulate", f"{location}.operation")
    _expect(result["tool"]["name"], backend, f"{location}.tool.name")
    _expect(
        result["tool"]["path"],
        backend_identity["tool"]["path"],
        f"{location}.tool.path",
    )
    _expect(
        result["tool"]["version"],
        backend_identity["tool"]["version"],
        f"{location}.tool.version",
    )
    _expect(result["execution"]["status"], "completed", f"{location}.execution.status")
    _expect(result["execution"]["exit_code"], 0, f"{location}.execution.exit_code")
    _expect(
        result["execution"]["cwd"],
        str(Path(fixture["container_path"]).parent),
        f"{location}.execution.cwd",
    )
    _verify_native_command(
        result["execution"]["command"],
        manifest,
        backend,
        fixture=fixture,
    )
    _expect(result["engineering"]["status"], "pass", f"{location}.engineering.status")
    if result["diagnostics"]:
        raise ConformanceError(f"{location} contains diagnostics: {result['diagnostics']!r}")

    inputs = result["inputs"]
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise ConformanceError(f"{location}.inputs must contain exactly the fixture")
    fixture_record = inputs[0]
    fixture_path = REPOSITORY_ROOT / fixture["repository_path"]
    for field, expected in {
        "path": fixture["container_path"],
        "kind": "spice-netlist",
        "role": "input",
        "exists": True,
        "bytes": fixture_path.stat().st_size,
        "sha256": fixture["sha256"],
    }.items():
        _expect(fixture_record.get(field), expected, f"{location}.input.{field}")

    artifacts = result["artifacts"]
    expected_artifacts = {item["path"]: item for item in specification["artifacts"]}
    actual_artifacts = {item["path"]: item for item in artifacts}
    if len(actual_artifacts) != len(artifacts):
        raise ConformanceError(f"{location}.artifacts contains a duplicate path")
    _expect(set(actual_artifacts), set(expected_artifacts), f"{location}.artifacts.paths")
    for path, expected in expected_artifacts.items():
        _verify_record(
            actual_artifacts[path],
            expected,
            evidence / expected["filename"],
            location=f"{location}.artifacts[{path}]",
        )

    data = result["data"]
    protocol = data["protocol"]
    _expect(
        protocol["operation_profile"],
        manifest["operation"]["profile"],
        f"{location}.protocol.operation_profile",
    )
    _expect(
        protocol["assertion_profile"],
        manifest["operation"]["assertion"],
        f"{location}.protocol.assertion_profile",
    )
    _expect(
        protocol["driver_id"],
        backend_identity["driver_id"],
        f"{location}.protocol.driver_id",
    )
    _expect(
        protocol["driver_version"],
        backend_identity["driver_version"],
        f"{location}.protocol.driver_version",
    )
    analysis = data["analysis"]
    for field, expected in {
        "type": analysis_type,
        "completion": "completed",
        "convergence": "converged",
        "point_count": specification["point_count"],
        "dependent_variable_count": specification["dependent_variable_count"],
        "finite_value_count": specification["finite_value_count"],
    }.items():
        _expect(analysis[field], expected, f"{location}.analysis.{field}")
    normalized_evidence = data["evidence"]
    for field, expected in {
        "request_binding": "exact",
        "freshness": "fresh",
        "structure": "valid",
        "artifact_roles_present": ["simulation.log", "simulation.result"],
        "provenance": "bounded",
    }.items():
        _expect(normalized_evidence[field], expected, f"{location}.evidence.{field}")

    extension = data["extensions"]["org.openada"]
    _expect(extension["backend"], backend, f"{location}.extension.backend")
    _expect(extension["parameters"], case["parameters"], f"{location}.extension.parameters")
    native = extension["native_data"]
    _expect(native["converged"], True, f"{location}.native.converged")
    _expect(native["inputs_stable"], True, f"{location}.native.inputs_stable")
    captures = native["output_captures"]
    if not isinstance(captures, list) or len(captures) != 1:
        raise ConformanceError(f"{location}.native.output_captures must contain one raw file")
    capture = captures[0]
    raw_expected = next(
        item for item in specification["artifacts"] if item["role"] == "simulation.result"
    )
    raw_path = evidence / raw_expected["filename"]
    for field, expected in {
        "path": raw_expected["path"],
        "status": "valid",
        "bytes": raw_path.stat().st_size,
        "sha256": _sha256(raw_path),
    }.items():
        _expect(capture[field], expected, f"{location}.native.capture.{field}")
    _expect(capture["validation"]["valid"], True, f"{location}.native.capture.validation")

    log_expected = next(
        item for item in specification["artifacts"] if item["role"] == "simulation.log"
    )
    log_path = evidence / log_expected["filename"]
    log_capture = native["log_capture"]
    for field, expected in {
        "path": log_expected["path"],
        "status": "valid",
        "bytes": log_path.stat().st_size,
        "sha256": _sha256(log_path),
    }.items():
        _expect(log_capture[field], expected, f"{location}.native.log_capture.{field}")
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if backend == "xyce":
        if "End of Xyce(TM) Simulation" not in log_text:
            raise ConformanceError(f"{location} log lacks the native completion marker")
    elif "No. of Data Rows" not in log_text:
        raise ConformanceError(f"{location} log lacks the native data-row record")
    if re.search(r"(?:fatal error|failed to converge|time step too small|xyce abort)", log_text, re.I):
        raise ConformanceError(f"{location} log contains native failure evidence")

    if specification["raw_encoding"] == "binary":
        variables, rows = parse_ngspice_binary(raw_path, analysis_type=analysis_type)
    else:
        variables, rows = parse_xyce_ascii(raw_path, analysis_type=analysis_type)
    return verify_analysis_fixture(
        variables,
        rows,
        case["parameters"],
        specification,
        backend=backend,
    )


def _option_value(command: list[str], option: str) -> str:
    indices = [index for index, value in enumerate(command) if value == option]
    if len(indices) != 1 or indices[0] + 1 >= len(command):
        raise ConformanceError(f"container command must contain one {option}")
    return command[indices[0] + 1]


def _analysis_cli_arguments(analysis: dict[str, Any]) -> list[str]:
    arguments = ["--analysis", analysis["type"]]
    if analysis["type"] == "dc":
        arguments.extend(
            [
                "--source-name",
                analysis["source_name"],
                "--source-unit",
                analysis["source_unit"],
                "--start",
                str(analysis["start"]),
                "--stop",
                str(analysis["stop"]),
                "--step",
                str(analysis["step"]),
            ]
        )
    elif analysis["type"] == "ac":
        arguments.extend(
            [
                "--sweep",
                analysis["sweep"],
                "--points",
                str(analysis["points"]),
                "--start-hz",
                str(analysis["start_hz"]),
                "--stop-hz",
                str(analysis["stop_hz"]),
            ]
        )
    return arguments


def _verify_container_command(
    command: Any,
    manifest: dict[str, Any],
    backend: str,
    *,
    case_name: str | None = None,
) -> None:
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
    if case_name is None:
        fixture = manifest["fixture"]
        specification = manifest["backends"][backend]
        analysis_arguments: list[str] = []
        location = backend
    else:
        case = manifest["capability_cases"][case_name]
        fixture = case["fixture"]
        specification = case["backends"][backend]
        analysis_arguments = _analysis_cli_arguments(case["parameters"]["analysis"])
        location = f"{case_name}.{backend}"
    _expect(
        _option_value(command, "--workdir"),
        str(Path(fixture["container_path"]).parent),
        f"{location}.container.workdir",
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
        fixture["container_path"],
        "--backend",
        backend,
        *analysis_arguments,
        "--output-dir",
        specification["output_directory"],
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
    expected_command_keys = {
        "ngspice",
        "xyce",
        "op.ngspice",
        "dc.ngspice",
        "dc.xyce",
        "ac.ngspice",
        "ac.xyce",
    }
    _expect(set(run["container_commands"]), expected_command_keys, "run.container_commands")
    for backend in ("ngspice", "xyce"):
        _verify_container_command(run["container_commands"][backend], manifest, backend)
    for analysis_type, case in manifest["capability_cases"].items():
        for backend in case["backends"]:
            _verify_container_command(
                run["container_commands"][f"{analysis_type}.{backend}"],
                manifest,
                backend,
                case_name=analysis_type,
            )

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
    capability_summaries = {
        analysis_type: {
            backend: _verify_capability_result(
                manifest,
                evidence,
                analysis_type,
                backend,
            )
            for backend in case["backends"]
        }
        for analysis_type, case in manifest["capability_cases"].items()
    }
    return {
        "schema": "openada.circuit-simulate-conformance-verification/v0alpha2",
        "conformance_id": manifest["id"],
        "status": "pass",
        "backends": summaries,
        "capability_cases": capability_summaries,
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
