#!/usr/bin/env python3
"""Condition-blind semantic artifact scorer for the paired IHP inverter task.

This independently interprets retained native bytes. It is not cryptographic
proof that the reported processes generated those bytes; campaign protocol
attestation and replay authority are separate eligibility gates.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "manifest.json"
SUBMISSION_SCHEMA = HERE / "submission.schema.json"
SCORE_SCHEMA = "openada.eval.score/v0alpha1"
SUBMISSION_SCHEMA_ID = "openada.eval.submission/v0alpha1"
EXPECTED_DESIGN = {
    "repository": "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git",
    "revision": "133ecf657572e021b5921b5a1b7693abfb209623",
    "license": "Apache-2.0",
}
EXPECTED_RUNTIME = {
    "image_reference": "hpretl/iic-osic-tools@sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0",
    "image_config_digest": "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb",
    "platform": "linux/amd64",
    "pdk": "ihp-sg13g2",
    "pdk_revision": "144f811cdffda49b71d28f64e8a92b697b61cf06",
}
EXPECTED_TOOLS = [
    {
        "name": "xschem",
        "path": "/foss/tools/xschem/bin/xschem",
        "version": "XSCHEM V3.4.8RC",
        "binary_sha256": "c960e786685939e03fb76619bad6aed886190b0d5d1eed2941b745565eb95c22",
    },
    {
        "name": "ngspice",
        "path": "/foss/tools/ngspice/bin/ngspice",
        "version": "** ngspice-46 : Circuit level simulation program",
        "binary_sha256": "6aacaca88f656e5e19074ac070fb410bf6cc437df1de88ec28d50a24c6239a1b",
    },
]
EXPECTED_INPUTS = [
    {
        "role": "schematic",
        "path": "/design/modules/module_0_foundations/inverter/inverter_tb.sch",
        "bytes": 1606,
        "sha256": "521464a42c5352cad371a8b091d71d9a083686749ef49c69b3f07ec838a3cb82",
    },
    {
        "role": "xschem-rcfile",
        "path": "/foss/pdks/ihp-sg13g2/libs.tech/xschem/xschemrc",
        "bytes": 19794,
        "sha256": "d6d8fa5157ad2072e6d1ce63bda5f5d593ef4eb84631f23eed5e9ae3886f18b5",
    },
    {
        "role": "pdk-revision",
        "path": "/foss/pdks/ihp-sg13g2/COMMIT",
        "bytes": 41,
        "sha256": "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5",
    },
    {
        "role": "ngspice-init",
        "path": "/foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit",
        "bytes": 957,
        "sha256": "56ec1880a943fa481c3c321d62857b6240387e39d5aa8ded403835c34edb515d",
    },
    {
        "role": "ngspice-system-init",
        "path": "/foss/tools/ngspice/share/ngspice/scripts/spinit",
        "bytes": 1509,
        "sha256": "b088c11a27e21ceadb14abbf9dff877105177bd025ca37750877d71e7f6f87af",
    },
]
EXPECTED_ARTIFACTS = [
    {
        "role": "generated-netlist",
        "path": "work/inverter_tb.spice",
        "maximum_bytes": 16777216,
    },
    {
        "role": "simulation-raw",
        "path": "work/test_inverter.raw",
        "maximum_bytes": 104857600,
    },
    {
        "role": "simulation-log",
        "path": "evidence/simulation/inverter_tb.log",
        "maximum_bytes": 16777216,
    },
]
EXPECTED_WAVEFORM = {
    "plotname": "Transient Analysis",
    "flags": "real",
    "acceptable_point_counts": [80, 81],
    "start_seconds": 0.0,
    "stop_seconds": 0.000002,
    "required_variables": ["time", "v(vdd)", "v(vin)", "v(vout)"],
    "vdd_min": 1.19,
    "vdd_max": 1.21,
    "settled_windows": [
        {
            "start_seconds": 0.0000002,
            "stop_seconds": 0.00000045,
            "vin_max": 0.05,
            "vout_min": 1.1,
        },
        {
            "start_seconds": 0.0000007,
            "stop_seconds": 0.0000013,
            "vin_min": 1.15,
            "vout_max": 0.1,
        },
        {
            "start_seconds": 0.0000017,
            "stop_seconds": 0.00000195,
            "vin_max": 0.05,
            "vout_min": 1.1,
        },
    ],
}
EXPECTED_LIMITS = {
    "submission_bytes": 1048576,
    "raw_header_bytes": 1048576,
    "raw_line_bytes": 65536,
    "diagnostics": 32,
    "diagnostic_message_characters": 1000,
}
MAX_SETUP_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_INTEGER_CHARACTERS = 20
MAX_JSON_DEPTH = 32


class ScoreSetupError(RuntimeError):
    """The scorer itself or its immutable task input is invalid."""


class EvidenceError(RuntimeError):
    """Native evidence is missing, malformed, or otherwise uninterpretable."""


def _bounded_text(value: object, maximum: int) -> str:
    text = str(value)
    if len(text) <= maximum:
        return text
    keep = max(0, (maximum - 32) // 2)
    return f"{text[:keep]}...[bounded {len(text)} chars]...{text[-keep:]}"[:maximum]


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key is not allowed")
        document[key] = value
    return document


def _parse_json_int(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > MAX_JSON_INTEGER_CHARACTERS:
        raise ValueError("JSON integer is outside the reviewed bound")
    parsed = int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        raise ValueError("JSON integer is outside the reviewed bound")
    return parsed


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number is not allowed")
    return parsed


def _strict_json_loads(payload: str) -> Any:
    document = json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
        parse_int=_parse_json_int,
        parse_float=_parse_json_float,
    )
    stack: list[tuple[Any, int]] = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting exceeds the reviewed bound")
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return document


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_setup_json(path: Path, *, label: str) -> str:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ScoreSetupError(f"{label} is not a single-link regular file")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SETUP_JSON_BYTES:
            raise ScoreSetupError(f"{label} size is outside the reviewed bound")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if _identity(before) != _identity(metadata):
            raise ScoreSetupError(f"{label} changed while being opened")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65536, MAX_SETUP_JSON_BYTES + 1 - observed),
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > MAX_SETUP_JSON_BYTES:
                raise ScoreSetupError(f"{label} size is outside the reviewed bound")
        after = os.fstat(descriptor)
        payload = b"".join(chunks)
        if _identity(before) != _identity(after) or len(payload) != after.st_size:
            raise ScoreSetupError(f"{label} changed while being read")
        return payload.decode("utf-8")
    except ScoreSetupError:
        raise
    except FileNotFoundError as exc:
        raise ScoreSetupError(f"{label} is missing") from exc
    except (OSError, UnicodeError) as exc:
        raise ScoreSetupError(f"cannot read strict {label}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        document = _strict_json_loads(_read_setup_json(path, label=label))
    except ScoreSetupError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ScoreSetupError(f"cannot parse strict {label}") from exc
    if not isinstance(document, dict):
        raise ScoreSetupError(f"{label} root must be an object")
    return document


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json_object(path, label="task manifest")
    if set(manifest) != {
        "schema",
        "id",
        "design",
        "runtime",
        "tools",
        "inputs",
        "artifacts",
        "waveform",
        "limits",
    }:
        raise ScoreSetupError("task manifest top-level fields differ from the reviewed set")
    if manifest.get("schema") != "openada.eval.task/v0alpha1":
        raise ScoreSetupError("task manifest has an unsupported schema")
    if manifest.get("id") != "ihp-inverter-xschem-ngspice-paired":
        raise ScoreSetupError("task manifest has an unexpected task id")
    expected_sections = {
        "design": EXPECTED_DESIGN,
        "runtime": EXPECTED_RUNTIME,
        "tools": EXPECTED_TOOLS,
        "inputs": EXPECTED_INPUTS,
        "artifacts": EXPECTED_ARTIFACTS,
        "waveform": EXPECTED_WAVEFORM,
        "limits": EXPECTED_LIMITS,
    }
    for name, expected in expected_sections.items():
        if manifest.get(name) != expected:
            raise ScoreSetupError(f"task manifest {name} differs from the reviewed semantics")
    for record in manifest["artifacts"]:
        path_value = record["path"]
        parts = PurePosixPath(path_value).parts
        if not parts or path_value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ScoreSetupError("task manifest contains an unsafe artifact path")
    return manifest


def _load_submission_validator() -> Draft202012Validator:
    schema = _load_json_object(SUBMISSION_SCHEMA, label="submission schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ScoreSetupError(f"submission schema is invalid: {exc.message}") from exc
    return Draft202012Validator(schema)


def _empty_artifact(specification: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": specification["role"],
        "path": specification["path"],
        "status": "missing",
        "bytes": None,
        "sha256": None,
        "reported_hash_correct": None,
    }


def _read_workspace_artifact(
    workspace: Path, specification: dict[str, Any]
) -> tuple[dict[str, Any], bytes | None]:
    """Read through no-follow directory descriptors and enforce a stable bound."""

    record = _empty_artifact(specification)
    parts = PurePosixPath(specification["path"]).parts
    descriptors: list[int] = []
    try:
        current = os.open(
            workspace,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        descriptors.append(current)
        for component in parts[:-1]:
            metadata = os.stat(component, dir_fd=current, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                record["status"] = "unsafe_parent"
                return record, None
            current = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=current,
            )
            descriptors.append(current)
        metadata = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        record["bytes"] = metadata.st_size
        if not stat.S_ISREG(metadata.st_mode):
            record["status"] = "invalid_type"
            return record, None
        if metadata.st_nlink != 1:
            record["status"] = "hardlinked"
            return record, None
        if metadata.st_size <= 0:
            record["status"] = "empty"
            return record, None
        if metadata.st_size > specification["maximum_bytes"]:
            record["status"] = "oversized"
            return record, None
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=current,
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if _identity(before) != _identity(metadata):
            record["status"] = "unstable"
            return record, None
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, specification["maximum_bytes"] + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > specification["maximum_bytes"]:
                record["status"] = "oversized"
                return record, None
        after = os.fstat(descriptor)
        payload = b"".join(chunks)
        if _identity(before) != _identity(after) or len(payload) != after.st_size:
            record["status"] = "unstable"
            return record, None
        record["bytes"] = len(payload)
        record["sha256"] = hashlib.sha256(payload).hexdigest()
        record["status"] = "captured"
        return record, payload
    except FileNotFoundError:
        return record, None
    except OSError:
        record["status"] = "unsafe"
        return record, None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_submission(
    path: Path | None,
    maximum_bytes: int,
    validator: Draft202012Validator,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    if path is None:
        return None, False, "submission path was not provided"
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return None, False, "submission is not a single-link regular file"
        if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
            return None, False, "submission size is outside the reviewed bound"
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        payload = b""
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(65536, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(payload) != after.st_size:
            return None, False, "submission changed while being read"
        document = _strict_json_loads(payload.decode("utf-8"))
    except FileNotFoundError:
        return None, False, "submission is missing"
    except OSError:
        return None, False, "submission is unreadable"
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None, False, "submission is not strict finite JSON"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(document, dict):
        return None, False, "submission root must be an object"
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        first = errors[0]
        safe_parts = [
            str(item) if isinstance(item, int) else "field"
            for item in first.absolute_path
        ]
        location = ".".join(safe_parts) or "root"
        return document, False, f"submission schema violation at {location}"
    return document, True, None


def _netlist_semantics(payload: bytes) -> list[str]:
    try:
        text = payload.decode("ascii")
    except UnicodeError as exc:
        raise EvidenceError(f"generated netlist is not ASCII: {exc}") from exc
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("*", ";", "$"))
    ]
    # This task freezes one Xschem-generated deck.  Validate the complete
    # active statement sequence, not a few searchable tokens: otherwise a
    # fabricated or incomplete deck could borrow four plausible lines and be
    # called valid.  Comments and whitespace may vary, but every electrical,
    # control, subcircuit-scope, stimulus, termination, and output statement is
    # required exactly once and in its reviewed scope.
    expected = (
        (
            "input pulse source",
            r"V1\s+Vin\s+GND\s+PULSE\(\s*0\s+1\.2\s+0\.5u\s+10n\s+10n\s+1u\s+2u\s+1\s*\)",
        ),
        ("supply source", r"V2\s+Vdd\s+GND\s+1\.2"),
        ("top-level inverter instance", r"x1\s+Vdd\s+Vin\s+Vout\s+GND\s+inverter"),
        ("control start", r"\.control"),
        ("save directive", r"save\s+all"),
        ("transient directive", r"tran\s+50n\s+2u"),
        ("deck-owned raw write", r"write\s+test_inverter\.raw"),
        ("control end", r"\.endc"),
        ("model corner", r"\.lib\s+cornerMOSlv\.lib\s+mos_tt"),
        ("inverter subcircuit", r"\.subckt\s+inverter\s+Vdd\s+Vin\s+Vout\s+Gnd"),
        (
            "NMOS instance",
            r"XM1\s+Gnd\s+Vin\s+Vout\s+Gnd\s+sg13_lv_nmos\s+w=1\.0u\s+l=0\.45u\s+ng=1\s+m=1\s+mm_ok=1",
        ),
        (
            "PMOS instance",
            r"XM2\s+Vout\s+Vin\s+Vdd\s+Vdd\s+sg13_lv_pmos\s+w=2\.0u\s+l=0\.45u\s+ng=1\s+m=1\s+mm_ok=1",
        ),
        ("subcircuit end", r"\.ends"),
        ("global ground", r"\.GLOBAL\s+GND"),
        ("deck end", r"\.end"),
    )
    failures: list[str] = []
    if len(active) != len(expected):
        failures.append("active netlist statement set differs from the reviewed deck")
    for index, (line, (label, pattern)) in enumerate(zip(active, expected), start=1):
        if re.fullmatch(pattern, line, re.IGNORECASE | re.ASCII) is None:
            failures.append(f"active statement {index} is not the reviewed {label}")
    if re.search(r"\bIS\s+MISSING\b", text, re.IGNORECASE | re.ASCII):
        failures.append("netlist contains an unresolved-symbol marker")
    return failures


def _read_raw_line(handle: BytesIO, consumed: int, limits: dict[str, int]) -> tuple[bytes, int]:
    line = handle.readline(limits["raw_line_bytes"] + 1)
    if len(line) > limits["raw_line_bytes"]:
        raise EvidenceError("binary raw header contains an overlong line")
    consumed += len(line)
    if consumed > limits["raw_header_bytes"]:
        raise EvidenceError("binary raw header exceeds the reviewed bound")
    return line, consumed


def _raw_semantics(payload: bytes, waveform: dict[str, Any], limits: dict[str, int]) -> dict[str, Any]:
    handle = BytesIO(payload)
    consumed = 0
    first, consumed = _read_raw_line(handle, consumed, limits)
    if not first.startswith(b"Title:"):
        raise EvidenceError("binary raw file does not begin with a Title header")
    header: dict[str, str] = {}
    while True:
        line, consumed = _read_raw_line(handle, consumed, limits)
        if not line:
            raise EvidenceError("binary raw header is truncated")
        if line.strip().lower() == b"variables:":
            break
        key, separator, value = line.partition(b":")
        if not separator:
            raise EvidenceError("binary raw header contains an invalid line")
        try:
            normalized = b" ".join(key.strip().lower().split()).decode("ascii")
            decoded = value.strip().decode("utf-8")
        except UnicodeError as exc:
            raise EvidenceError("binary raw header is not valid text") from exc
        if normalized in header:
            # Header names come from participant-controlled native bytes.  Do
            # not reflect them into the publishable score diagnostics.
            raise EvidenceError("binary raw header repeats a field")
        header[normalized] = decoded
    for required in ("plotname", "flags", "no. variables", "no. points"):
        if required not in header:
            raise EvidenceError(f"binary raw header lacks {required!r}")
    if header["plotname"] != waveform["plotname"]:
        raise EvidenceError("binary raw plot is not the reviewed transient analysis")
    if header["flags"].casefold() != waveform["flags"]:
        raise EvidenceError("binary raw numeric type is not real")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except ValueError as exc:
        raise EvidenceError("binary raw dimensions are not integers") from exc
    if not 1 <= variable_count <= 1024:
        raise EvidenceError("binary raw variable count is outside the reviewed bound")
    if point_count not in waveform["acceptable_point_counts"]:
        raise EvidenceError("binary raw point count is outside the reviewed set")
    variables: list[str] = []
    for index in range(variable_count):
        line, consumed = _read_raw_line(handle, consumed, limits)
        try:
            fields = line.decode("utf-8").split()
        except UnicodeError as exc:
            raise EvidenceError("binary raw variable table is not valid UTF-8") from exc
        if len(fields) < 3 or fields[0] != str(index):
            raise EvidenceError(f"binary raw variable table is invalid at index {index}")
        variables.append(fields[1].casefold())
    marker, consumed = _read_raw_line(handle, consumed, limits)
    if marker.strip().lower() != b"binary:":
        raise EvidenceError("ngspice raw evidence is not binary encoded")
    if len(set(variables)) != len(variables):
        raise EvidenceError("binary raw variable names are not unique")
    missing = sorted(set(waveform["required_variables"]) - set(variables))
    if missing:
        raise EvidenceError(f"binary raw file lacks required variables: {missing}")
    binary = handle.read()
    expected_bytes = point_count * variable_count * 8
    if len(binary) != expected_bytes:
        raise EvidenceError(
            f"binary raw payload has {len(binary)} bytes, expected {expected_bytes}"
        )
    try:
        values = struct.unpack(f"<{point_count * variable_count}d", binary)
    except struct.error as exc:
        raise EvidenceError("binary raw payload cannot be decoded") from exc
    if not all(math.isfinite(value) for value in values):
        raise EvidenceError("binary raw payload contains a non-finite value")
    columns = {
        name: [values[row * variable_count + column] for row in range(point_count)]
        for column, name in enumerate(variables)
        if name in waveform["required_variables"]
    }
    times = columns["time"]
    if not math.isclose(times[0], waveform["start_seconds"], rel_tol=0.0, abs_tol=1e-18):
        raise EvidenceError("transient does not start at the reviewed time")
    if not math.isclose(times[-1], waveform["stop_seconds"], rel_tol=0.0, abs_tol=5e-12):
        raise EvidenceError("transient does not stop at the reviewed time")
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise EvidenceError("transient time values are not strictly increasing")
    failures: list[str] = []
    vdd = columns["v(vdd)"]
    if any(not waveform["vdd_min"] <= value <= waveform["vdd_max"] for value in vdd):
        failures.append("VDD leaves the reviewed 1.19 through 1.21 V range")
    vin = columns["v(vin)"]
    vout = columns["v(vout)"]
    for index, window in enumerate(waveform["settled_windows"]):
        samples = [
            row
            for row, time in enumerate(times)
            if window["start_seconds"] <= time <= window["stop_seconds"]
        ]
        if not samples:
            raise EvidenceError(f"settled inversion window {index} contains no sample")
        failed = False
        for row in samples:
            if (
                vin[row] < window.get("vin_min", -math.inf)
                or vin[row] > window.get("vin_max", math.inf)
                or vout[row] < window.get("vout_min", -math.inf)
                or vout[row] > window.get("vout_max", math.inf)
            ):
                failed = True
                break
        if failed:
            failures.append(f"settled inversion window {index} violates the reviewed levels")
    return {
        "points": point_count,
        "variables": variable_count,
        "semantic_failures": failures,
    }


_NATIVE_LOG_ERRORS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"simulation interrupted due to error",
        r"run simulation not started",
        r"fatal error",
        r"^\s*error on line\b",
        r"failed to converge",
        r"timestep too small",
        r"singular matrix",
    )
)


def _log_semantics(payload: bytes, point_count: int | None) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise EvidenceError(f"simulation log is not valid UTF-8: {exc}") from exc
    if point_count is None:
        raise EvidenceError("simulation log cannot be bound to a structurally valid raw file")
    if 'binary raw file "test_inverter.raw"' not in text:
        raise EvidenceError("simulation log lacks the required binary-output record")
    if "ngspice-46 done" not in text:
        raise EvidenceError("simulation log lacks the pinned clean-completion record")
    if any(pattern.search(text) for pattern in _NATIVE_LOG_ERRORS):
        raise EvidenceError("simulation log contains native fatal or convergence evidence")
    rows = re.findall(r"No\. of Data Rows\s*:\s*([0-9]+)", text)
    if len(rows) != 1 or int(rows[0]) != point_count:
        raise EvidenceError("simulation log row count does not match the raw evidence")


def _record_map(records: Any, key: str) -> dict[str, dict[str, Any]] | None:
    if not isinstance(records, list):
        return None
    mapped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get(key), str):
            return None
        value = record[key]
        if value in mapped:
            return None
        mapped[value] = record
    return mapped


def _expanded_absolute(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise ScoreSetupError(f"cannot resolve the requested {label}") from exc


def score_workspace(
    workspace: Path,
    submission_path: Path | None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Score only fixed native files and a condition-neutral submission."""

    manifest = _load_manifest(
        _expanded_absolute(manifest_path or DEFAULT_MANIFEST, label="task manifest")
    )
    workspace = _expanded_absolute(workspace, label="workspace")
    try:
        root_metadata = workspace.lstat()
    except OSError as exc:
        raise ScoreSetupError("cannot stat the requested workspace") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or workspace.is_symlink():
        raise ScoreSetupError("workspace must be a real, non-symlink directory")
    workspace = workspace.resolve()
    limits = manifest["limits"]
    diagnostics: list[dict[str, str]] = []

    def diagnose(code: str, severity: str, message: object) -> None:
        if len(diagnostics) >= limits["diagnostics"]:
            return
        diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "message": _bounded_text(message, limits["diagnostic_message_characters"]),
            }
        )

    artifacts: list[dict[str, Any]] = []
    payloads: dict[str, bytes | None] = {}
    for specification in manifest["artifacts"]:
        record, payload = _read_workspace_artifact(workspace, specification)
        artifacts.append(record)
        payloads[record["role"]] = payload
        if record["status"] != "captured":
            diagnose(
                f"artifact.{record['role']}.{record['status']}",
                "error",
                f"{record['path']} is {record['status']}",
            )
    artifact_by_role = {record["role"]: record for record in artifacts}
    netlist_payload = payloads["generated-netlist"]
    if netlist_payload is not None:
        try:
            failures = _netlist_semantics(netlist_payload)
        except EvidenceError as exc:
            artifact_by_role["generated-netlist"]["status"] = "malformed"
            diagnose("netlist.malformed", "error", exc)
        else:
            if failures:
                artifact_by_role["generated-netlist"]["status"] = "semantic_fail"
                for failure in failures:
                    diagnose("netlist.semantic_failure", "error", failure)
            else:
                artifact_by_role["generated-netlist"]["status"] = "valid"

    point_count: int | None = None
    raw_payload = payloads["simulation-raw"]
    if raw_payload is not None:
        try:
            raw = _raw_semantics(raw_payload, manifest["waveform"], limits)
        except EvidenceError as exc:
            artifact_by_role["simulation-raw"]["status"] = "malformed"
            diagnose("raw.malformed", "error", exc)
        else:
            point_count = raw["points"]
            if raw["semantic_failures"]:
                artifact_by_role["simulation-raw"]["status"] = "semantic_fail"
                for failure in raw["semantic_failures"]:
                    diagnose("waveform.semantic_failure", "error", failure)
            else:
                artifact_by_role["simulation-raw"]["status"] = "valid"

    log_payload = payloads["simulation-log"]
    if log_payload is not None:
        try:
            _log_semantics(log_payload, point_count)
        except EvidenceError as exc:
            artifact_by_role["simulation-log"]["status"] = "malformed"
            diagnose("log.malformed", "error", exc)
        else:
            artifact_by_role["simulation-log"]["status"] = "valid"

    raw_status = artifact_by_role["simulation-raw"]["status"]
    if raw_status == "semantic_fail":
        engineering_verdict = "fail"
    elif raw_status == "valid":
        engineering_verdict = "pass"
    else:
        engineering_verdict = "unknown"

    validator = _load_submission_validator()
    expanded_submission = (
        _expanded_absolute(submission_path, label="submission")
        if submission_path is not None
        else None
    )
    submission, submission_valid, submission_error = _read_submission(
        expanded_submission,
        limits["submission_bytes"],
        validator,
    )
    if submission_error is not None:
        diagnose("submission.invalid", "error", submission_error)

    reported_status_correct: bool | None = None
    tools_exact = False
    inputs_exact = False
    artifact_set_exact = False
    artifact_hashes_exact = False
    limitations_reported = False
    process_completed_reported_correctly = False
    if submission_valid and submission is not None:
        reported_status_correct = submission["status"] == engineering_verdict
        if not reported_status_correct:
            diagnose(
                "submission.status_mismatch",
                "error",
                f"reported {submission['status']!r}, independently scored {engineering_verdict!r}",
            )
        expected_tools = {item["name"]: item for item in manifest["tools"]}
        actual_tools = _record_map(submission["tools"], "name")
        tools_exact = actual_tools == expected_tools
        expected_inputs = {item["role"]: item for item in manifest["inputs"]}
        actual_inputs = _record_map(submission["inputs"], "role")
        inputs_exact = actual_inputs == expected_inputs
        expected_artifact_paths = {
            item["role"]: item["path"] for item in manifest["artifacts"]
        }
        actual_artifacts = _record_map(submission["artifacts"], "role")
        artifact_set_exact = bool(
            actual_artifacts is not None
            and set(actual_artifacts) == set(expected_artifact_paths)
            and all(
                actual_artifacts[role].get("path") == path
                for role, path in expected_artifact_paths.items()
            )
        )
        hash_results: list[bool] = []
        for artifact in artifacts:
            reported = actual_artifacts.get(artifact["role"]) if actual_artifacts else None
            if reported is None or artifact["sha256"] is None:
                artifact["reported_hash_correct"] = False
                hash_results.append(False)
                continue
            hash_correct = reported.get("sha256") == artifact["sha256"]
            artifact["reported_hash_correct"] = hash_correct
            hash_results.append(
                hash_correct and reported.get("bytes") == artifact["bytes"]
            )
        artifact_hashes_exact = artifact_set_exact and all(hash_results)
        limitations_reported = bool(submission["limitations"])
        process_evidence_complete = all(
            artifact_by_role[role]["status"] in {"valid", "semantic_fail"}
            for role in ("generated-netlist", "simulation-raw")
        ) and artifact_by_role["simulation-log"]["status"] == "valid"
        expected_process_completed: bool | None = True if process_evidence_complete else None
        process_completed_reported_correctly = (
            submission["process_completed"] is expected_process_completed
        )
        for code, value in (
            ("submission.tools_mismatch", tools_exact),
            ("submission.inputs_mismatch", inputs_exact),
            ("submission.artifact_set_mismatch", artifact_set_exact),
            ("submission.artifact_hash_mismatch", artifact_hashes_exact),
            (
                "submission.process_completion_unverified",
                process_completed_reported_correctly,
            ),
        ):
            if not value:
                diagnose(code, "error", code.replace("submission.", "").replace("_", " "))

    provenance = {
        "submission_valid": submission_valid,
        "process_completed_reported_correctly": process_completed_reported_correctly,
        "tools_exact": tools_exact,
        "inputs_exact": inputs_exact,
        "artifact_set_exact": artifact_set_exact,
        "artifact_hashes_exact": artifact_hashes_exact,
        "limitations_reported": limitations_reported,
    }
    all_native_artifacts_valid = all(item["status"] == "valid" for item in artifacts)
    verified_artifact_complete = bool(
        engineering_verdict == "pass"
        and all_native_artifacts_valid
        and reported_status_correct is True
        and all(provenance.values())
    )
    return {
        "schema": SCORE_SCHEMA,
        "task_id": manifest["id"],
        "engineering_verdict": engineering_verdict,
        "verified_artifact_complete": verified_artifact_complete,
        "reported_status_correct": reported_status_correct,
        "artifacts": artifacts,
        "provenance": provenance,
        "diagnostics": diagnostics,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Condition-blind native scorer for the paired IHP inverter task."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        score = score_workspace(args.workspace, args.submission, args.manifest)
    except ScoreSetupError as exc:
        print(f"scoring failed: {_bounded_text(exc, 4000)}", file=sys.stderr)
        return 2
    if args.compact:
        print(json.dumps(score, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(score, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
