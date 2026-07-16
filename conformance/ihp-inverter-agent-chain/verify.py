#!/usr/bin/env python3
"""Independently verify the IHP native-to-agent evidence chain.

This module deliberately imports no ``openada`` package code.  It reparses the
native Spice3 raw file and reimplements the closed extraction, measurement, and
specification arithmetic from the published contracts.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Any, Callable
import uuid

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from common import ConformanceError, load_manifest, sha256_file


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MANIFEST_PATH = HERE / "manifest.json"
RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas/result-v0alpha1.schema.json"
CHAIN_RUN_SCHEMA_PATH = REPOSITORY_ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json"
DESIGN_PROVENANCE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas/design-provenance-v0alpha1.schema.json"
)
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_RAW_HEADER_BYTES = 1024 * 1024
MAX_RAW_LINE_BYTES = 65_536
PROVIDER_REQUEST_ID = "10000000-0000-4000-8000-000000000001"
CONTAINER_NAME_RE = re.compile(r"openada-ihp-agent-[1-9][0-9]*-[0-9a-f]{8}")
XSCHEM_TEMP_RE = re.compile(r"/tmp/openada-xschem-[A-Za-z0-9_-]+")
NGSPICE_TEMP_LOG_RE = re.compile(
    r"/tmp/(?:openada-provider-environment-[A-Za-z0-9_-]+/tmp/)?"
    r"openada-ngspice-[A-Za-z0-9_-]+/simulation\.log"
)
NGSPICE_TEMP_RAW_RE = re.compile(
    r"/tmp/openada-ngspice-[A-Za-z0-9_-]+/simulation\.raw"
)
NATIVE_ERROR_RE = re.compile(
    r"(?:failed to converge|timestep too small|singular matrix|fatal error|"
    r"unknown subckt|cannot find model|could not find a valid modelname|"
    r"simulation interrupted due to error|run simulation not started)",
    re.IGNORECASE,
)


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _close(actual: Any, expected: float, location: str, *, atol: float = 1e-15) -> None:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ConformanceError(f"{location} must be a finite number")
    value = float(actual)
    if not math.isfinite(value) or not math.isclose(value, expected, rel_tol=1e-12, abs_tol=atol):
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON object key {key!r}")
        output[key] = value
    return output


def _require_regular_file(path: Path, *, label: str, maximum_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ConformanceError(f"{label} is not a regular, non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must have exactly one hard link: {path}")
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise ConformanceError(
            f"{label} size {metadata.st_size} is outside 1..{maximum_bytes}: {path}"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular_file(path, label=label, maximum_bytes=MAX_JSON_BYTES)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be one JSON object")
    return document


def _load_validator(path: Path, *, label: str) -> Draft202012Validator:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, ValueError, SchemaError) as exc:
        raise ConformanceError(f"cannot load {label} schema {path}: {exc}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate(document: dict[str, Any], validator: Draft202012Validator, *, label: str) -> None:
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label} violates its schema at {location}: {error.message}")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _file_record(record: Any, path: Path, *, role: str | None = None, kind: str | None = None) -> None:
    if not isinstance(record, dict):
        raise ConformanceError(f"file record for {path} must be an object")
    _expect(record.get("exists"), True, f"file[{path}].exists")
    if role is not None:
        _expect(record.get("role"), role, f"file[{path}].role")
    if kind is not None:
        _expect(record.get("kind"), kind, f"file[{path}].kind")
    size = _require_regular_file(path, label=f"artifact {path}", maximum_bytes=MAX_ARTIFACT_BYTES)
    _expect(record.get("bytes"), size, f"file[{path}].bytes")
    _expect(record.get("sha256"), sha256_file(path), f"file[{path}].sha256")


def _records_by_path(records: Any, location: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ConformanceError(f"{location} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConformanceError(f"{location}[{index}] is not a file record")
        if record["path"] in mapped:
            raise ConformanceError(f"{location} repeats {record['path']!r}")
        mapped[record["path"]] = record
    return mapped


def _verify_result_base(
    result: dict[str, Any],
    *,
    operation: str,
    engineering: str,
    validator: Draft202012Validator,
) -> None:
    _validate(result, validator, label=operation)
    _expect(result.get("schema"), "openada.result/v0alpha1", f"{operation}.schema")
    _expect(result.get("operation"), operation, f"{operation}.operation")
    _expect(result["execution"].get("status"), "completed", f"{operation}.execution.status")
    _expect(result["execution"].get("exit_code"), 0, f"{operation}.execution.exit_code")
    _expect(result["engineering"].get("status"), engineering, f"{operation}.engineering.status")
    errors = [item for item in result["diagnostics"] if item.get("severity") == "error"]
    if engineering == "pass" and errors:
        raise ConformanceError(f"{operation} pass contains error diagnostics: {errors!r}")


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
        normalized = b" ".join(key.strip().lower().split()).decode("ascii", errors="strict")
        if normalized in header:
            raise ConformanceError(f"binary raw header repeats {normalized!r}")
        header[normalized] = value.strip().decode("utf-8", errors="strict")
    for required in ("plotname", "flags", "no. variables", "no. points"):
        if required not in header:
            raise ConformanceError(f"binary raw header lacks {required!r}")
    _expect(header["plotname"], waveform["plotname"], "raw.plotname")
    _expect(header["flags"].casefold(), waveform["numeric_type"], "raw.flags")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except ValueError as exc:
        raise ConformanceError("binary raw dimensions are not integers") from exc
    if variable_count != 12 or point_count not in waveform["acceptable_point_counts"]:
        raise ConformanceError(
            f"raw dimensions {variable_count}x{point_count} differ from the reviewed inverter run"
        )
    variables: list[tuple[str, str]] = []
    for index in range(variable_count):
        line, consumed = _read_bounded_line(handle, consumed)
        fields = line.decode("utf-8", errors="strict").split()
        if len(fields) < 3 or fields[0] != str(index):
            raise ConformanceError(f"binary raw variable table is invalid at index {index}")
        variables.append((fields[1].casefold(), fields[2].casefold()))
    marker, consumed = _read_bounded_line(handle, consumed)
    if marker.strip().lower() != b"binary:":
        raise ConformanceError("ngspice raw evidence is not padded binary Spice3 data")
    names = [item[0] for item in variables]
    if len(set(names)) != len(names):
        raise ConformanceError("binary raw variable names are not unique")
    missing = sorted(set(waveform["required_variables"]) - set(names))
    if missing:
        raise ConformanceError(f"binary raw file lacks required variables: {missing}")
    expected_types = {
        "time": "time",
        "v(vdd)": "voltage",
        "v(vin)": "voltage",
        "v(vout)": "voltage",
    }
    for name, native_type in variables:
        if name in expected_types:
            _expect(native_type, expected_types[name], f"raw.variable[{name}].type")

    binary = handle.read()
    expected_bytes = point_count * variable_count * 8
    if len(binary) != expected_bytes:
        raise ConformanceError(
            f"binary raw payload has {len(binary)} bytes, expected {expected_bytes}"
        )
    values = struct.unpack(f"={point_count * variable_count}d", binary)
    if not all(math.isfinite(value) for value in values):
        raise ConformanceError("binary raw payload contains a non-finite value")
    columns = {
        name: [values[row * variable_count + column] for row in range(point_count)]
        for column, (name, _native_type) in enumerate(variables)
    }
    times = columns["time"]
    _close(times[0], waveform["start_s"], "raw.time[0]", atol=1e-18)
    _close(times[-1], waveform["stop_s"], "raw.time[-1]", atol=5e-12)
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ConformanceError("transient time values are not strictly increasing")
    if any(not 1.19 <= value <= 1.21 for value in columns["v(vdd)"]):
        raise ConformanceError("VDD leaves the reviewed 1.19..1.21 V range")
    vin, vout = columns["v(vin)"], columns["v(vout)"]
    for index, window in enumerate(waveform["settled_windows"]):
        selected = [
            row
            for row, time in enumerate(times)
            if window["start_s"] <= time <= window["stop_s"]
        ]
        if not selected:
            raise ConformanceError(f"settled inversion window {index} contains no sample")
        for row in selected:
            if (
                vin[row] < window.get("vin_min", -math.inf)
                or vin[row] > window.get("vin_max", math.inf)
                or vout[row] < window.get("vout_min", -math.inf)
                or vout[row] > window.get("vout_max", math.inf)
            ):
                raise ConformanceError(
                    f"settled inversion window {index} is violated at t={times[row]:.9g}"
                )
    return {
        "header": header,
        "variables": variables,
        "point_count": point_count,
        "columns": columns,
    }


def _verify_manifest(manifest: dict[str, Any], manifest_sha256: str) -> None:
    """Check pins not covered by shape validation and bind the provider record."""

    _expect(manifest["id"], "openada.chain/ihp-inverter-agent-chain/v1", "manifest.id")
    details = manifest["extensions"]["org.openada"]
    _expect(details["policy"]["eda_network"], "none", "manifest.policy.eda_network")
    _expect(details["policy"]["openada_mount"], "read-only", "manifest.policy.openada_mount")
    _expect(details["policy"]["design_mount"], "read-only", "manifest.policy.design_mount")
    _expect(
        details["provider"]["manifest_path"],
        "/openada/providers/ngspice-pdk-control/driver-manifest.json",
        "manifest.provider.manifest_path",
    )
    _expect(details["provider"]["driver_id"], "org.openada.driver.ngspice-pdk-control", "manifest.provider.driver_id")
    _expect(details["provider"]["driver_version"], "0.5.0", "manifest.provider.driver_version")
    _expect(
        details["provider"]["environment"],
        {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
        "manifest.provider.environment",
    )
    _expect(
        details["provider"]["analysis"],
        {"type": "tran", "step_s": 5e-8, "stop_s": 2e-6, "extensions": {}},
        "manifest.provider.analysis",
    )
    kinds = [item["request"]["kind"] for item in details["measurements"]]
    _expect(
        kinds,
        ["sample_at", "minimum", "maximum", "mean", "rms", "crossing", "rise_time", "fall_time", "settling_time"],
        "manifest.measurement_kinds",
    )
    if "not a time-weighted electrical average" not in details["measurements"][3]["interpretation"]:
        raise ConformanceError("manifest must disclose adaptive-sample mean semantics")
    if "not a time-weighted electrical RMS" not in details["measurements"][4]["interpretation"]:
        raise ConformanceError("manifest must disclose adaptive-sample RMS semantics")

    provider_manifest_path = REPOSITORY_ROOT / "providers/ngspice-pdk-control/driver-manifest.json"
    provider_manifest = _read_json(provider_manifest_path, label="provider manifest")
    _expect(provider_manifest["driver"]["id"], details["provider"]["driver_id"], "provider_manifest.driver.id")
    _expect(provider_manifest["driver"]["version"], details["provider"]["driver_version"], "provider_manifest.driver.version")
    capabilities = provider_manifest.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) != 1:
        raise ConformanceError("provider manifest must advertise exactly one reviewed capability")
    capability = capabilities[0]
    _expect(capability["operation_profile"], "openada.operation/circuit.simulate/v1alpha2", "provider_manifest.capability.operation_profile")
    _expect(
        set(capability["features"]),
        {
            "openada.feature/simulation.analysis.op/v1alpha1",
            "openada.feature/simulation.analysis.dc/v1alpha1",
            "openada.feature/simulation.analysis.ac/v1alpha1",
            "openada.feature/simulation.analysis.tran/v1alpha1",
        },
        "provider_manifest.capability.features",
    )
    records = provider_manifest.get("conformance_records")
    if not isinstance(records, list) or len(records) != 1:
        raise ConformanceError("provider manifest must contain one chain conformance record")
    record = records[0]
    _expect(
        record["record_id"],
        "org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1",
        "provider_manifest.conformance.record_id",
    )
    _expect(record["driver_version"], "0.5.0", "provider_manifest.conformance.driver_version")
    _expect(record["status"], "pass", "provider_manifest.conformance.status")
    _expect(
        record["evidence"]["uri"],
        "https://github.com/simra-tech/OpenADA/tree/main/conformance/ihp-ngspice-provider-analyses",
        "provider_manifest.conformance.evidence.uri",
    )
    claim_digest = record["evidence"]["sha256"]
    if not isinstance(claim_digest, str) or re.fullmatch(r"[0-9a-f]{64}", claim_digest) is None:
        raise ConformanceError("provider conformance evidence digest is not a SHA-256 value")
    # A zero digest is the deliberately unresolved pre-publication state.  Once
    # published, the coverage index binds this field to retained chain-run bytes;
    # binding it to the manifest itself would be circular and incorrect.


def _expected_evidence_files(manifest: dict[str, Any], require_chain_run: bool) -> set[str]:
    files = {
        "design-provenance.json",
        "run.json",
        "netlist.json",
        "builtin-result.json",
        "builtin-fail-result.json",
        "provider-config.json",
        "provider-request.json",
        "provider-result.json",
        "provider-fail-request.json",
        "provider-fail-result.json",
        "extract-selection.json",
        "extract.json",
        "agent-evidence.json",
        "negative/netlist-missing-symbol.json",
        "work/inverter.sym",
        "work/inverter.sch",
        "work/inverter_missing_symbol.sch",
        "work/inverter_missing_symbol.spice",
        "work/inverter_tb.spice",
        "work/inverter_terminal_nonconvergence.spice",
        "work/inverter_shared.spice",
        "work/inverter_shared_terminal_nonconvergence.spice",
        "runtime/cornerMOSlv.lib",
        "runtime/sg13g2_moslv_mod.lib",
        "runtime/sg13g2_moslv_parm.lib",
        "runtime/psp103.osdi",
        "runtime/shared.spiceinit",
        "builtin/simulation/inverter_shared.raw",
        "builtin/simulation/inverter_shared.log",
        "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.raw",
        "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.log",
        "provider/work/test_inverter.raw",
        "provider/work/openada-native-ngspice",
        "provider/simulation/inverter_tb.log",
        "provider/simulation/inverter_tb.openada-control.sp",
        "provider-fail/work/test_inverter_fail.raw",
        "provider-fail/work/openada-native-ngspice",
        "provider-fail/simulation/inverter_terminal_nonconvergence.log",
        "provider-fail/simulation/inverter_terminal_nonconvergence.openada-control.sp",
    }
    for position, definition in enumerate(
        manifest["extensions"]["org.openada"]["measurements"], start=1
    ):
        identifier = definition["id"]
        files.add(f"requests/measure-{identifier}.json")
        files.add(f"requests/measure-{identifier}-negative.json")
        files.add(f"measurements/{identifier}.json")
        files.add(f"negative/measure-{identifier}.json")
        for decision in ("pass", "fail"):
            files.add(f"requests/spec-{identifier}-{decision}.json")
            files.add(f"specifications/{identifier}-{decision}.json")
    files.add("requests/extract-missing-selector.json")
    files.add("negative/extract-missing-selector.json")
    files.add("requests/spec-sample-at-condition-mismatch.json")
    files.add("negative/spec-sample-at-condition-mismatch.json")
    if require_chain_run:
        files.update(
            {"chain-run.json", "independent-verification.json", "contract-tests.json"}
        )
        files.update(f"tamper/{identifier}.json" for identifier in TAMPER_PROBE_IDS)
    return files


def _verify_evidence_tree(evidence: Path, manifest: dict[str, Any], require_chain_run: bool) -> None:
    try:
        metadata = evidence.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat evidence directory {evidence}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ConformanceError("evidence root must be a real, non-symlink directory")
    actual_files: set[str] = set()
    for path in evidence.rglob("*"):
        relative = path.relative_to(evidence).as_posix()
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode):
            raise ConformanceError(f"evidence contains a symbolic link: {relative}")
        if stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                raise ConformanceError(f"evidence file has multiple hard links: {relative}")
            actual_files.add(relative)
        elif not stat.S_ISDIR(item.st_mode):
            raise ConformanceError(f"evidence contains a non-file object: {relative}")
    expected = _expected_evidence_files(manifest, require_chain_run)
    if actual_files != expected:
        raise ConformanceError(
            f"evidence file set differs; missing={sorted(expected-actual_files)}, "
            f"unexpected={sorted(actual_files-expected)}"
        )


def _verify_checkout(metadata: dict[str, Any]) -> None:
    checkout = metadata["openada_checkout"]
    before, after = checkout["before"], checkout["after"]
    for label, state in (("before", before), ("after", after)):
        if state["commit"] is None:
            raise ConformanceError(f"run.openada_checkout.{label} did not bind a Git commit")
        if not re.fullmatch(r"[0-9a-f]{40}", state["commit"]):
            raise ConformanceError(f"run.openada_checkout.{label}.commit is invalid")
        _expect(state["tracked_files_modified"], False, f"run.openada_checkout.{label}.tracked_files_modified")
        _expect(state["untracked_files_present"], False, f"run.openada_checkout.{label}.untracked_files_present")
        _expect(state["working_tree_modified"], False, f"run.openada_checkout.{label}.working_tree_modified")
        _expect(state["status_entry_count"], 0, f"run.openada_checkout.{label}.status_entry_count")
        _expect(state["status_sha256"], hashlib.sha256(b"").hexdigest(), f"run.openada_checkout.{label}.status_sha256")
    _expect(before, after, "run.openada_checkout.before_after")
    _expect(checkout["state_unchanged"], True, "run.openada_checkout.state_unchanged")
    _expect(checkout["commit_exact"], True, "run.openada_checkout.commit_exact")


def _verify_container_command(command: Any, manifest: dict[str, Any]) -> None:
    if not isinstance(command, list) or len(command) != 43:
        raise ConformanceError(f"run.container_command has unexpected shape: {command!r}")
    if CONTAINER_NAME_RE.fullmatch(command[4]) is None:
        raise ConformanceError("run.container_command container name is invalid")
    if re.fullmatch(r"[0-9]+:[0-9]+", command[18]) is None:
        raise ConformanceError("run.container_command user identity is invalid")
    mounts = command[30], command[32], command[34]
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/openada,readonly", mounts[0]) is None:
        raise ConformanceError("OpenADA bind mount is not read-only")
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/design,readonly", mounts[1]) is None:
        raise ConformanceError("design bind mount is not read-only")
    if re.fullmatch(r"type=bind,source=/[^,]+,target=/evidence", mounts[2]) is None:
        raise ConformanceError("evidence bind mount is not the sole writable bind")
    expected = [
        command[0], "run", "--rm", "--name", command[4], "--pull=never",
        "--platform", "linux/amd64", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "512", "--user", command[18],
        "--env", "HOME=/tmp/openada-home", "--env", "TMPDIR=/tmp",
        "--env", "PATH=/openada/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=512m", "--workdir", "/evidence",
        "--mount", mounts[0], "--mount", mounts[1], "--mount", mounts[2],
        "--entrypoint", "/usr/bin/python3", manifest["runtime"]["image_reference"],
        "/openada/conformance/ihp-inverter-agent-chain/inside.py",
        "--manifest", "/openada/conformance/ihp-inverter-agent-chain/manifest.json",
        "--evidence", "/evidence",
    ]
    _expect(command, expected, "run.container_command")


def _expected_invocations(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    prefix = [
        "/usr/bin/python3", "/openada/bin/openada", "--profile", "iic-osic-tools", "--compact"
    ]
    details = manifest["extensions"]["org.openada"]
    workflow, provider = details["workflow"], details["provider"]
    pdk = manifest["runtime"]["extensions"]["org.openada"]["pdk"]
    invocations: list[dict[str, Any]] = []
    completed: list[str] = []
    netlist = [
        *prefix, "netlist", workflow["schematic"], "--output", workflow["generated_deck"],
        "--rcfile", pdk["xschem_rcfile"]["path"], "--timeout", "120",
    ]
    invocations.append({"operation": "netlist", "cwd": "/design/modules/module_0_foundations/inverter", "argv": netlist})
    completed.append("netlist")
    negative_netlist = [
        *prefix, "netlist", "/evidence/work/inverter_missing_symbol.sch",
        "--output", "/evidence/work/inverter_missing_symbol.spice",
        "--rcfile", pdk["xschem_rcfile"]["path"], "--timeout", "120",
    ]
    invocations.append(
        {
            "operation": "netlist:missing-symbol",
            "cwd": "/evidence/work",
            "argv": negative_netlist,
        }
    )
    completed.append("netlist:missing-symbol")
    provider_argv = [
        *prefix, "provider", "invoke", "--manifest", provider["manifest_path"],
        "--cwd", "/openada", "/evidence/provider-request.json",
    ]
    invocations.append({"operation": "provider.invoke", "cwd": "/evidence", "argv": provider_argv})
    completed.append("provider.invoke")
    terminal = details["terminal_nonconvergence"]
    terminal_argv = [
        *prefix, "provider", "invoke", "--manifest", provider["manifest_path"],
        "--cwd", "/openada", "/evidence/provider-fail-request.json",
    ]
    invocations.append(
        {
            "operation": "provider.invoke:terminal-nonconvergence",
            "cwd": "/evidence",
            "argv": terminal_argv,
        }
    )
    completed.append("provider.invoke:terminal-nonconvergence")
    builtin = details["builtin_ngspice"]
    builtin_pass = [
        *prefix, "simulate", builtin["pass"]["deck"], "--backend", "ngspice",
        "--analysis", "tran", "--step-s", f"{provider['analysis']['step_s']:.17g}",
        "--stop-s", f"{provider['analysis']['stop_s']:.17g}",
        "--output-dir", builtin["pass"]["output_dir"],
        "--workdir", builtin["pass"]["workdir"], "--timeout", "180",
    ]
    invocations.append(
        {
            "operation": "simulate:shared-ngspice",
            "cwd": "/evidence",
            "argv": builtin_pass,
        }
    )
    completed.append("simulate:shared-ngspice")
    builtin_fail = [
        *prefix, "simulate", builtin["terminal_fail"]["deck"],
        "--backend", "ngspice", "--analysis", "tran",
        "--step-s", f"{terminal['analysis']['step_s']:.17g}",
        "--stop-s", f"{terminal['analysis']['stop_s']:.17g}",
        "--output-dir", builtin["terminal_fail"]["output_dir"],
        "--workdir", builtin["terminal_fail"]["workdir"], "--timeout", "180",
    ]
    invocations.append(
        {
            "operation": "simulate:shared-ngspice:terminal-nonconvergence",
            "cwd": "/evidence",
            "argv": builtin_fail,
        }
    )
    completed.append("simulate:shared-ngspice:terminal-nonconvergence")
    extract = [
        *prefix, "extract", "--simulation", "/evidence/provider-result.json",
        "--artifact", provider["raw_artifact"], "--selection", "/evidence/extract-selection.json",
        "--request-id", workflow["extract_request_id"],
    ]
    invocations.append({"operation": "result.series.extract", "cwd": "/evidence", "argv": extract})
    completed.append("result.series.extract")
    negative_extract = [
        *prefix, "extract", "--simulation", "/evidence/provider-result.json",
        "--artifact", provider["raw_artifact"], "--selection",
        "/evidence/requests/extract-missing-selector.json", "--request-id",
        "15000000-0000-4000-8000-000000000001",
    ]
    invocations.append(
        {
            "operation": "result.series.extract:missing-selector",
            "cwd": "/evidence",
            "argv": negative_extract,
        }
    )
    completed.append("result.series.extract:missing-selector")
    for index, definition in enumerate(details["measurements"], start=1):
        identifier = definition["id"]
        measure = [
            *prefix, "measure", "--series", "/evidence/extract.json", "--measurement",
            f"/evidence/requests/measure-{identifier}.json", "--request-id", definition["request_id"],
        ]
        operation = f"result.measure:{identifier}"
        invocations.append({"operation": operation, "cwd": "/evidence", "argv": measure})
        completed.append(operation)
        negative_measure = [
            *prefix, "measure", "--series", "/evidence/extract.json", "--measurement",
            f"/evidence/requests/measure-{identifier}-negative.json", "--request-id",
            f"16000000-0000-4000-8000-{index:012d}",
        ]
        negative_operation = f"result.measure:{identifier}:negative"
        invocations.append(
            {"operation": negative_operation, "cwd": "/evidence", "argv": negative_measure}
        )
        completed.append(negative_operation)
        for decision, correlation_prefix in (("pass", "12"), ("fail", "13")):
            evaluate = [
                *prefix, "evaluate", "--measurement", f"/evidence/measurements/{identifier}.json",
                "--specification", f"/evidence/requests/spec-{identifier}-{decision}.json",
                "--request-id", f"{correlation_prefix}000000-0000-4000-8000-{index:012d}",
            ]
            operation = f"specification.evaluate:{identifier}:{decision}"
            invocations.append({"operation": operation, "cwd": "/evidence", "argv": evaluate})
            completed.append(operation)
        if index == 1:
            mismatch = [
                *prefix, "evaluate", "--measurement", "/evidence/measurements/sample-at.json",
                "--specification", "/evidence/requests/spec-sample-at-condition-mismatch.json",
                "--request-id", "17000000-0000-4000-8000-000000000001",
            ]
            invocations.append(
                {
                    "operation": "specification.evaluate:condition-mismatch",
                    "cwd": "/evidence",
                    "argv": mismatch,
                }
            )
            completed.append("specification.evaluate:condition-mismatch")
    completed.append("agent.evidence")
    return invocations, completed


def _verify_run(
    manifest: dict[str, Any],
    run: dict[str, Any],
    manifest_sha256: str,
    provider_manifest_sha256: str,
) -> None:
    _expect(run["schema"], "openada.ihp-agent-chain-run-metadata/v0alpha1", "run.schema")
    _expect(run["chain_id"], manifest["id"], "run.chain_id")
    _expect(run["chain_manifest_sha256"], manifest_sha256, "run.chain_manifest_sha256")
    _expect(run["design_revision"], manifest["design"]["revision"], "run.design_revision")
    _expect(run["image"]["reference"], manifest["runtime"]["image_reference"], "run.image.reference")
    _expect(run["image"]["id"], manifest["runtime"]["image_config_digest"], "run.image.id")
    _expect(run["image"]["os"], "linux", "run.image.os")
    _expect(run["image"]["architecture"], "amd64", "run.image.architecture")
    _expect(run["network"], "none during EDA execution", "run.network")
    _verify_checkout(run)
    _verify_container_command(run["container_command"], manifest)
    observation = run["runtime_observation"]
    _expect(observation["schema"], "openada.ihp-agent-chain-container-observation/v0alpha1", "run.observation.schema")
    records = observation["runtime_inputs"]
    pins = manifest["runtime"]["extensions"]["org.openada"]
    expected = {
        "pdk_commit": pins["pdk"]["commit_file"],
        "xschem_rcfile": pins["pdk"]["xschem_rcfile"],
        "ngspice_init": pins["pdk"]["ngspice_init"],
        "ngspice_system_init": pins["ngspice_system_init"],
        "ngspice_executable": pins["ngspice_executable"],
        "corner_moslv": pins["model_files"]["corner_moslv"],
        "moslv_modules": pins["model_files"]["moslv_modules"],
        "moslv_parameters": pins["model_files"]["moslv_parameters"],
        "psp103_osdi": pins["psp103_osdi"],
    }
    for name, identity in expected.items():
        _expect(records[name]["path"], identity["path"], f"run.runtime_inputs.{name}.path")
        _expect(records[name]["sha256"], identity["sha256"], f"run.runtime_inputs.{name}.sha256")
        if not isinstance(records[name].get("bytes"), int) or records[name]["bytes"] <= 0:
            raise ConformanceError(f"run.runtime_inputs.{name}.bytes must be positive")
    _expect(records["pdk_commit"]["value"], manifest["runtime"]["pdk_revision"], "run.runtime_inputs.pdk_commit.value")
    _expect(records["provider_manifest"]["path"], "/openada/providers/ngspice-pdk-control/driver-manifest.json", "run.runtime_inputs.provider_manifest.path")
    _expect(records["provider_manifest"]["sha256"], provider_manifest_sha256, "run.runtime_inputs.provider_manifest.sha256")
    startup = manifest["extensions"]["org.openada"]["builtin_ngspice"]["isolated_startup"]
    _expect(
        records["isolated_ngspice_startup"]["path"],
        startup["path"],
        "run.runtime_inputs.isolated_ngspice_startup.path",
    )
    _expect(
        records["isolated_ngspice_startup"]["sha256"],
        startup["sha256"],
        "run.runtime_inputs.isolated_ngspice_startup.sha256",
    )
    _expect(
        records["isolated_ngspice_startup"]["bytes"],
        len(startup["content"].encode("ascii")),
        "run.runtime_inputs.isolated_ngspice_startup.bytes",
    )
    invocations, completed = _expected_invocations(manifest)
    _expect(observation["openada_invocations"], invocations, "run.observation.openada_invocations")
    _expect(observation["completed_operations"], completed, "run.observation.completed_operations")


def _verify_active_netlist(deck: Path) -> str:
    _require_regular_file(deck, label="generated inverter deck", maximum_bytes=16 * 1024 * 1024)
    text = deck.read_text(encoding="utf-8", errors="strict")
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("*", ";", "$"))
    ]
    required = {
        "model corner": r"^\.lib\s+cornerMOSlv\.lib\s+mos_tt(?:\s|$)",
        "inverter subcircuit": r"^\.subckt\s+inverter\s+Vdd\s+Vin\s+Vout\s+Gnd(?:\s|$)",
        "NMOS": r"^XM1\s+Gnd\s+Vin\s+Vout\s+Gnd\s+sg13_lv_nmos(?:\s|$)",
        "PMOS": r"^XM2\s+Vout\s+Vin\s+Vdd\s+Vdd\s+sg13_lv_pmos(?:\s|$)",
        "input pulse": r'^V1\s+Vin\s+GND\s+"?PULSE\(0\s+1\.2\s+0\.5u\s+10n\s+10n\s+1u\s+2u\s+1\)"?(?:\s|$)',
        "supply": r"^V2\s+Vdd\s+GND\s+1\.2(?:\s|$)",
    }
    missing = [
        label
        for label, pattern in required.items()
        if not any(re.match(pattern, line, re.IGNORECASE) for line in active)
    ]
    in_control = False
    commands: list[str] = []
    blocks = 0
    for line in active:
        if line.casefold() == ".control":
            if in_control:
                raise ConformanceError("generated deck nests control blocks")
            in_control = True
            blocks += 1
        elif line.casefold() == ".endc":
            if not in_control:
                raise ConformanceError("generated deck has unmatched .endc")
            in_control = False
        elif in_control:
            commands.append(" ".join(line.split()).casefold())
    if in_control or blocks != 1 or commands != ["save all", "tran 50n 2u", "write test_inverter.raw"]:
        missing.append("one closed save/tran/write control block")
    if missing:
        raise ConformanceError(f"generated deck lacks reviewed active records: {missing!r}")
    if re.search(r"\bIS\s+MISSING\b", text, re.IGNORECASE):
        raise ConformanceError("generated netlist contains an unresolved-symbol marker")
    return sha256_file(deck)


def _verify_netlist_result(
    manifest: dict[str, Any],
    evidence: Path,
    result: dict[str, Any],
    validator: Draft202012Validator,
) -> str:
    _verify_result_base(result, operation="netlist", engineering="pass", validator=validator)
    xschem = next(item for item in manifest["runtime"]["tools"] if item["id"] == "xschem")
    _expect(result["tool"], {"name": "xschem", "path": xschem["path"], "version": xschem["version"]}, "netlist.tool")
    workflow = manifest["extensions"]["org.openada"]["workflow"]
    rcfile = manifest["runtime"]["extensions"]["org.openada"]["pdk"]["xschem_rcfile"]
    command = result["execution"]["command"]
    if not isinstance(command, list) or len(command) != 10:
        raise ConformanceError("netlist execution command has an unexpected shape")
    expected_prefix = [xschem["path"], "--rcfile", rcfile["path"], "-n", "-s", "-q", "-x", "-o"]
    if command[:8] != expected_prefix or XSCHEM_TEMP_RE.fullmatch(command[8]) is None:
        raise ConformanceError("netlist execution command differs from the reviewed Xschem argv")
    _expect(command[9], workflow["schematic"], "netlist.command.schematic")
    _expect(result["execution"]["cwd"], str(Path(workflow["schematic"]).parent), "netlist.execution.cwd")
    inputs = _records_by_path(result["inputs"], "netlist.inputs")
    schematic = manifest["design"]["inputs"][0]
    expected_input_hashes = {
        workflow["schematic"]: schematic["sha256"],
        rcfile["path"]: rcfile["sha256"],
    }
    _expect(set(inputs), set(expected_input_hashes), "netlist.inputs.paths")
    for path, digest in expected_input_hashes.items():
        _expect(inputs[path]["sha256"], digest, f"netlist.inputs[{path}].sha256")
        _expect(inputs[path]["exists"], True, f"netlist.inputs[{path}].exists")
    artifacts = _records_by_path(result["artifacts"], "netlist.artifacts")
    _expect(set(artifacts), {workflow["generated_deck"]}, "netlist.artifacts.paths")
    deck = evidence / "work/inverter_tb.spice"
    _file_record(artifacts[workflow["generated_deck"]], deck, role="output", kind="spice-netlist")
    digest = _verify_active_netlist(deck)
    _expect(result["data"]["missing_symbol_count"], 0, "netlist.data.missing_symbol_count")
    _expect(result["data"]["missing_symbols"], [], "netlist.data.missing_symbols")
    return digest


def _verify_negative_netlist(
    manifest: dict[str, Any],
    evidence: Path,
    result: dict[str, Any],
    validator: Draft202012Validator,
) -> None:
    _verify_result_base(
        result, operation="netlist", engineering="fail", validator=validator
    )
    xschem = next(item for item in manifest["runtime"]["tools"] if item["id"] == "xschem")
    _expect(
        result["tool"],
        {"name": "xschem", "path": xschem["path"], "version": xschem["version"]},
        "negative_netlist.tool",
    )
    rcfile = manifest["runtime"]["extensions"]["org.openada"]["pdk"]["xschem_rcfile"]
    command = result["execution"]["command"]
    if not isinstance(command, list) or len(command) != 10:
        raise ConformanceError("negative netlist command has an unexpected shape")
    expected_prefix = [xschem["path"], "--rcfile", rcfile["path"], "-n", "-s", "-q", "-x", "-o"]
    if command[:8] != expected_prefix or XSCHEM_TEMP_RE.fullmatch(command[8]) is None:
        raise ConformanceError("negative netlist command differs from reviewed Xschem argv")
    _expect(
        command[9],
        "/evidence/work/inverter_missing_symbol.sch",
        "negative_netlist.command.schematic",
    )
    _expect(result["execution"]["cwd"], "/evidence/work", "negative_netlist.execution.cwd")

    marker = (
        "C {__openada_missing_symbol__.sym} 620 -100 0 0 "
        "{name=x_openada_missing}\n"
    )
    schematic = evidence / "work/inverter_missing_symbol.sch"
    schematic_text = schematic.read_text(encoding="utf-8", errors="strict")
    if schematic_text.count(marker) != 1 or not schematic_text.endswith(marker):
        raise ConformanceError("negative schematic does not contain one isolated reviewed marker")
    original = schematic_text[: -len(marker)].encode("utf-8")
    _expect(
        hashlib.sha256(original).hexdigest(),
        manifest["design"]["inputs"][0]["sha256"],
        "negative_netlist.original_schematic_sha256",
    )
    for filename, position in (("inverter.sym", 1), ("inverter.sch", 2)):
        path = evidence / "work" / filename
        _expect(
            sha256_file(path),
            manifest["design"]["inputs"][position]["sha256"],
            f"negative_netlist.{filename}.sha256",
        )

    inputs = _records_by_path(result["inputs"], "negative_netlist.inputs")
    expected_inputs = {
        "/evidence/work/inverter_missing_symbol.sch": sha256_file(schematic),
        rcfile["path"]: rcfile["sha256"],
    }
    _expect(set(inputs), set(expected_inputs), "negative_netlist.inputs.paths")
    for path, digest in expected_inputs.items():
        _expect(inputs[path]["sha256"], digest, f"negative_netlist.inputs[{path}].sha256")
        _expect(inputs[path]["exists"], True, f"negative_netlist.inputs[{path}].exists")

    deck = evidence / "work/inverter_missing_symbol.spice"
    artifacts = _records_by_path(result["artifacts"], "negative_netlist.artifacts")
    _expect(
        set(artifacts),
        {"/evidence/work/inverter_missing_symbol.spice"},
        "negative_netlist.artifacts.paths",
    )
    _file_record(
        artifacts["/evidence/work/inverter_missing_symbol.spice"],
        deck,
        role="output",
        kind="spice-netlist",
    )
    missing = "*  x_openada_missing -  __openada_missing_symbol__  IS MISSING !!!!"
    deck_text = deck.read_text(encoding="utf-8", errors="strict")
    if deck_text.count(missing) != 1:
        raise ConformanceError("negative netlist lacks one exact unresolved-symbol record")
    for required in (".subckt inverter Vdd Vin Vout Gnd", "sg13_lv_nmos", "sg13_lv_pmos"):
        if required not in deck_text:
            raise ConformanceError(
                f"negative netlist lost resolved inverter topology record {required!r}"
            )
    _expect(result["data"]["missing_symbol_count"], 1, "negative_netlist.missing_symbol_count")
    _expect(result["data"]["missing_symbols"], [missing], "negative_netlist.missing_symbols")
    _expect(
        [item.get("code") for item in result["diagnostics"]],
        ["xschem.missing_symbol"],
        "negative_netlist.diagnostics",
    )


def _verify_requests_template(manifest: dict[str, Any]) -> None:
    template_path = HERE / "requests.json"
    template = _read_json(template_path, label="request definitions")
    _expect(template["schema"], "openada.ihp-inverter-agent-chain-requests/v0alpha1", "requests.schema")
    _expect(template["chain_id"], manifest["id"], "requests.chain_id")
    details = manifest["extensions"]["org.openada"]
    definition = {
        key: details[key]
        for key in ("provider", "terminal_nonconvergence", "workflow", "measurements")
    }
    _expect(template["definition_sha256"], _canonical_sha256(definition), "requests.definition_sha256")
    for step in manifest["steps"]:
        if step["kind"] == "semantic-command":
            _expect(step["request"]["repository_path"], "conformance/ihp-inverter-agent-chain/requests.json", f"manifest.steps[{step['id']}].request.path")
            _expect(step["request"]["sha256"], sha256_file(template_path), f"manifest.steps[{step['id']}].request.sha256")


def _provider_configuration(manifest: dict[str, Any]) -> dict[str, Any]:
    pins = manifest["runtime"]["extensions"]["org.openada"]
    return {
        "schema": "openada.ngspice-provider-config/v0alpha1",
        "init_file": pins["pdk"]["ngspice_init"],
        "system_init_file": pins["ngspice_system_init"],
        "environment": {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
        "extensions": {},
    }


def _verify_provider_configuration(manifest: dict[str, Any], evidence: Path) -> str:
    path = evidence / "provider-config.json"
    configuration = _read_json(path, label="provider configuration")
    _expect(configuration, _provider_configuration(manifest), "provider_configuration")
    schema_path = REPOSITORY_ROOT / "providers/ngspice-pdk-control/provider-config-v0alpha1.schema.json"
    _validate(configuration, _load_validator(schema_path, label="provider configuration"), label="provider configuration")
    return sha256_file(path)


def _verify_provider_request(
    manifest: dict[str, Any],
    request: dict[str, Any],
    *,
    deck_path: str,
    deck_sha256: str,
    config_sha256: str,
    request_id: str,
    analysis: dict[str, Any],
    destination: str,
    location: str,
) -> None:
    details = manifest["extensions"]["org.openada"]
    provider = details["provider"]
    pdk_commit = manifest["runtime"]["extensions"]["org.openada"]["pdk"]["commit_file"]
    _expect(set(request), {
        "schema", "request_id", "operation_profile", "assertion_profile", "target",
        "configuration", "parameters", "evidence_policy", "evidence_destination",
        "execution_constraints", "driver_selector", "extensions",
    }, f"{location}.keys")
    _expect(request["schema"], "openada.request/v0alpha1", f"{location}.schema")
    _expect(request["request_id"], request_id, f"{location}.request_id")
    _expect(request["operation_profile"], "openada.operation/circuit.simulate/v1alpha2", f"{location}.operation_profile")
    _expect(request["assertion_profile"], "openada.assertion/simulation.evidence.valid/v1alpha1", f"{location}.assertion_profile")
    _expect(request["target"], {
        "kind": "testbench",
        "locator": {"type": "filesystem", "path": deck_path, "sha256": deck_sha256, "extensions": {}},
        "extensions": {},
    }, f"{location}.target")
    _expect(request["configuration"], [
        {
            "role": "simulator-configuration", "required": True,
            "locator": {"type": "filesystem", "path": "/evidence/provider-config.json", "sha256": config_sha256, "extensions": {}},
            "extensions": {},
        },
        {
            "role": "pdk", "required": True,
            "locator": {"type": "filesystem", "path": pdk_commit["path"], "sha256": pdk_commit["sha256"], "extensions": {}},
            "extensions": {},
        },
    ], f"{location}.configuration")
    _expect(request["parameters"], {"analysis": analysis, "extensions": {}}, f"{location}.parameters")
    _expect(request["evidence_policy"], {
        "required_artifact_roles": ["simulation.result", "simulation.log"],
        "retain_native_artifacts": True,
        "retain_native_logs": True,
        "provenance": "bounded",
        "identity_requirement": "content-digest",
        "extensions": {},
    }, f"{location}.evidence_policy")
    _expect(request["evidence_destination"], {
        "locator": {"type": "filesystem", "path": destination, "extensions": {}},
        "collision_policy": "fail-if-present", "extensions": {},
    }, f"{location}.evidence_destination")
    _expect(request["execution_constraints"], {
        "completion": "wait", "timeout_ms": 180000, "max_log_bytes": 16777216,
        "max_artifact_bytes": 268435456, "side_effects": "evidence-only", "extensions": {},
    }, f"{location}.execution_constraints")
    _expect(request["driver_selector"], {
        "driver_id": provider["driver_id"], "driver_version": provider["driver_version"],
        "transport_id": provider["transport_id"],
        "required_features": ["openada.feature/simulation.analysis.tran/v1alpha1"],
        "extensions": {},
    }, f"{location}.driver_selector")
    _expect(request["extensions"], {}, f"{location}.extensions")


def _verify_terminal_deck(manifest: dict[str, Any], evidence: Path, source_text: str) -> str:
    terminal = manifest["extensions"]["org.openada"]["terminal_nonconvergence"]
    path = evidence / "work/inverter_terminal_nonconvergence.spice"
    _require_regular_file(path, label="terminal-nonconvergence deck", maximum_bytes=16 * 1024 * 1024)
    text = path.read_text(encoding="utf-8", errors="strict")
    records = (
        "I_PROBE 0 __openada_fail pulse -250m 250m 100u 10u 10u 90u 200u\n"
        "R_PROBE __openada_fail 0 1K\n"
        "B_PROBE_1 __openada_fail 0 I=V(__openada_fail,0)*max(1u,min(1,100*(V(__openada_fail,0)-10)))\n"
        "B_PROBE_2 0 __openada_fail I=V(0,__openada_fail)*max(1u,min(1,100*(V(0,__openada_fail)-10)))\n"
    )
    _expect(hashlib.sha256(records.encode("ascii")).hexdigest(), terminal["mapping"]["injected_records_sha256"], "terminal.mapping.injected_records_sha256")
    if text.count(records) != 1:
        raise ConformanceError("terminal deck does not contain exactly one reviewed isolated injection")
    if any(name in records.casefold() for name in ("vin", "vout", "vdd")):
        raise ConformanceError("terminal injection is not isolated from DUT signal names")
    normalized = re.sub(r"(?im)^\s*tran\s+1u\s+1m\s*$", "tran 50n 2u", text, count=1)
    normalized = re.sub(r"(?im)^\s*write\s+test_inverter_fail\.raw\s*$", "write test_inverter.raw", normalized, count=1)
    normalized = normalized.replace(records, "", 1)
    normalized = normalized.split("\n", 1)[1]
    if normalized != source_text:
        raise ConformanceError("terminal deck changes the inverter beyond the reviewed isolated injection and control output")
    return sha256_file(path)


def _retained_shared_runtime(manifest: dict[str, Any], evidence: Path) -> dict[str, Path]:
    pins = manifest["runtime"]["extensions"]["org.openada"]
    retained = {
        "corner_moslv": evidence / "runtime/cornerMOSlv.lib",
        "moslv_modules": evidence / "runtime/sg13g2_moslv_mod.lib",
        "moslv_parameters": evidence / "runtime/sg13g2_moslv_parm.lib",
        "psp103_osdi": evidence / "runtime/psp103.osdi",
    }
    for name, path in retained.items():
        identity = (
            pins["model_files"][name]
            if name in pins["model_files"]
            else pins["psp103_osdi"]
        )
        _require_regular_file(
            path, label=f"retained shared runtime {name}", maximum_bytes=MAX_ARTIFACT_BYTES
        )
        _expect(sha256_file(path), identity["sha256"], f"retained_runtime.{name}.sha256")
    startup = manifest["extensions"]["org.openada"]["builtin_ngspice"]["isolated_startup"]
    retained_startup = evidence / "runtime/shared.spiceinit"
    _require_regular_file(
        retained_startup, label="retained shared startup", maximum_bytes=MAX_JSON_BYTES
    )
    _expect(retained_startup.read_text(encoding="ascii"), startup["content"], "retained_runtime.startup.content")
    _expect(sha256_file(retained_startup), startup["sha256"], "retained_runtime.startup.sha256")
    osdi_path = pins["psp103_osdi"]["path"]
    _expect(startup["content"], f"osdi {osdi_path}\n", "builtin_ngspice.isolated_startup.content")
    return retained


def _expected_shared_deck(
    source_text: str,
    retained: dict[str, Path],
    *,
    step_s: float,
    stop_s: float,
    terminal_nonconvergence: bool,
) -> str:
    corner_text = retained["corner_moslv"].read_text(encoding="utf-8", errors="strict")
    module_text = retained["moslv_modules"].read_text(encoding="utf-8", errors="strict")
    parameter_text = retained["moslv_parameters"].read_text(encoding="utf-8", errors="strict")
    corner_match = re.search(
        r"(?ims)^\s*\.LIB\s+mos_tt\s*$\n(?P<body>.*?)"
        r"^\s*\.include\s+sg13g2_moslv_mod\.lib\s*$\n"
        r"^\s*\.ENDL\s+mos_tt\s*$",
        corner_text,
    )
    if corner_match is None:
        raise ConformanceError("retained mos_tt corner has no reviewed closure")
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib)\b", parameter_text):
        raise ConformanceError("retained MOS parameter file contains a transitive include")
    embedded = (
        "** begin inlined pinned sg13g2_moslv_parm.lib\n"
        + parameter_text.rstrip()
        + "\n** end inlined pinned sg13g2_moslv_parm.lib\n"
    )
    flattened_modules, includes = re.subn(
        r"(?im)^\s*\.include\s+sg13g2_moslv_parm\.lib\s*\n",
        embedded,
        module_text,
    )
    _expect(includes, 4, "shared_deck.parameter_include_count")
    without_control, controls = re.subn(
        r"(?ims)^\s*\.control\s*$.*?^\s*\.endc\s*$\n?", "", source_text
    )
    without_library, libraries = re.subn(
        r"(?im)^\s*\.lib\s+cornerMOSlv\.lib\s+mos_tt\s*$\n?",
        "",
        without_control,
    )
    _expect((controls, libraries), (1, 1), "shared_deck.source_transform_counts")
    injection = ""
    if terminal_nonconvergence:
        records = (
            "I_PROBE 0 __openada_fail pulse -250m 250m 100u 10u 10u 90u 200u\n"
            "R_PROBE __openada_fail 0 1K\n"
            "B_PROBE_1 __openada_fail 0 I=V(__openada_fail,0)*max(1u,min(1,100*(V(__openada_fail,0)-10)))\n"
            "B_PROBE_2 0 __openada_fail I=V(0,__openada_fail)*max(1u,min(1,100*(V(0,__openada_fail)-10)))\n"
        )
        _expect(
            hashlib.sha256(records.encode("ascii")).hexdigest(),
            "f32de2d66578ae881b789d91681724a8050380a637013219d4020007b280c4dd",
            "shared_deck.injection_sha256",
        )
        injection = "** Deliberate isolated ngspice engine-boundary injection.\n" + records
    closure = (
        "\n** OpenADA shared-profile flattening of pinned IHP mos_tt model closure.\n"
        + corner_match.group("body")
        + "\n"
        + flattened_modules.rstrip()
        + "\n"
        + injection
        + f".tran {step_s:.17g} {stop_s:.17g}\n"
    )
    transformed, ends = re.subn(
        r"(?im)^\s*\.end\s*$", closure + ".end", without_library
    )
    _expect(ends, 1, "shared_deck.top_level_end_count")
    return transformed


def _verify_shared_decks(
    manifest: dict[str, Any], evidence: Path, source_text: str
) -> tuple[str, str]:
    retained = _retained_shared_runtime(manifest, evidence)
    details = manifest["extensions"]["org.openada"]
    provider = details["provider"]
    terminal = details["terminal_nonconvergence"]
    records = (
        (
            evidence / "work/inverter_shared.spice",
            provider["analysis"],
            False,
        ),
        (
            evidence / "work/inverter_shared_terminal_nonconvergence.spice",
            terminal["analysis"],
            True,
        ),
    )
    digests: list[str] = []
    for path, analysis, terminal_failure in records:
        _require_regular_file(path, label="shared-profile deck", maximum_bytes=16 * 1024 * 1024)
        actual = path.read_text(encoding="utf-8", errors="strict")
        expected = _expected_shared_deck(
            source_text,
            retained,
            step_s=analysis["step_s"],
            stop_s=analysis["stop_s"],
            terminal_nonconvergence=terminal_failure,
        )
        _expect(actual, expected, f"shared_deck[{path.name}].bytes")
        active_forbidden = re.findall(
            r"(?im)^\s*\.(?:inc(?:lude)?|lib|control|measure|meas|print|four|fft|step)\b",
            actual,
        )
        _expect(active_forbidden, [], f"shared_deck[{path.name}].forbidden_directives")
        analyses = re.findall(r"(?im)^\s*\.(?:op|dc|ac|tran)\b", actual)
        _expect(analyses, [".tran"], f"shared_deck[{path.name}].analyses")
        digests.append(sha256_file(path))
    return digests[0], digests[1]


def _verify_builtin_result(
    manifest: dict[str, Any],
    evidence: Path,
    result: dict[str, Any],
    result_validator: Draft202012Validator,
    data_validator: Draft202012Validator,
    *,
    expected_status: str,
    deck_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    _validate(result, result_validator, label=f"built-in {expected_status} result")
    _validate(result["data"], data_validator, label=f"built-in {expected_status} data")
    _expect(result["operation"], "simulate", f"builtin_{expected_status}.operation")
    _expect(result["engineering"]["status"], expected_status, f"builtin_{expected_status}.engineering")
    _expect(result["execution"]["status"], "completed", f"builtin_{expected_status}.execution.status")
    _expect(result["execution"]["exit_code"], 0 if expected_status == "pass" else 1, f"builtin_{expected_status}.execution.exit_code")
    ngspice = next(item for item in manifest["runtime"]["tools"] if item["id"] == "ngspice")
    _expect(
        result["tool"],
        {"name": "ngspice", "path": ngspice["path"], "version": ngspice["version"]},
        f"builtin_{expected_status}.tool",
    )
    builtin = manifest["extensions"]["org.openada"]["builtin_ngspice"]
    branch = builtin["pass" if expected_status == "pass" else "terminal_fail"]
    command = result["execution"]["command"]
    if (
        not isinstance(command, list)
        or len(command) != 7
        or command[:2] != [ngspice["path"], "-b"]
        or command[2] != "-r"
        or NGSPICE_TEMP_RAW_RE.fullmatch(command[3]) is None
        or command[4] != "-o"
        or NGSPICE_TEMP_LOG_RE.fullmatch(command[5]) is None
        or command[6] != branch["deck"]
    ):
        raise ConformanceError("built-in ngspice command differs from the reviewed batch mapping")
    _expect(result["execution"]["cwd"], branch["workdir"], f"builtin_{expected_status}.cwd")
    try:
        request_id = str(uuid.UUID(result["data"]["protocol"]["request_id"]))
    except (AttributeError, ValueError) as exc:
        raise ConformanceError("built-in simulation request ID is not a canonical UUID") from exc
    _expect(request_id, result["data"]["protocol"]["request_id"], f"builtin_{expected_status}.request_id")
    _expect(
        {key: value for key, value in result["data"]["protocol"].items() if key != "request_id"},
        {
            "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
            "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
            "driver_id": builtin["driver_id"],
            "driver_version": builtin["driver_version"],
        },
        f"builtin_{expected_status}.protocol",
    )
    analysis_parameters = (
        manifest["extensions"]["org.openada"]["provider"]["analysis"]
        if expected_status == "pass"
        else manifest["extensions"]["org.openada"]["terminal_nonconvergence"]["analysis"]
    )
    analysis = result["data"]["analysis"]
    expected_dimensions = (81, 11, 891) if expected_status == "pass" else (162, 12, 1944)
    _expect(
        (analysis["point_count"], analysis["dependent_variable_count"], analysis["finite_value_count"]),
        expected_dimensions,
        f"builtin_{expected_status}.analysis.dimensions",
    )
    _expect(analysis["type"], "tran", f"builtin_{expected_status}.analysis.type")
    _expect(analysis["completion"], "completed" if expected_status == "pass" else "terminal-failure", f"builtin_{expected_status}.analysis.completion")
    _expect(analysis["convergence"], "converged" if expected_status == "pass" else "non-converged", f"builtin_{expected_status}.analysis.convergence")
    evidence_record = result["data"]["evidence"]
    for key, expected in (
        ("request_binding", "exact"), ("freshness", "fresh"),
        ("structure", "valid"), ("provenance", "bounded"),
    ):
        _expect(evidence_record[key], expected, f"builtin_{expected_status}.evidence.{key}")
    extension = result["data"]["extensions"]["org.openada"]
    _expect(extension["backend"], "ngspice", f"builtin_{expected_status}.backend")
    _expect(extension["parameters"], {"analysis": analysis_parameters, "extensions": {}}, f"builtin_{expected_status}.parameters")
    initialization = extension["native_data"]["initialization"]
    _expect(initialization["ambient_startup_files_enumerated"], False, f"builtin_{expected_status}.ambient_startup_disclosure")
    _expect(initialization["local_user_spiceinit"], "native-default", f"builtin_{expected_status}.startup_policy")

    inputs = _records_by_path(result["inputs"], f"builtin_{expected_status}.inputs")
    _expect(set(inputs), {branch["deck"]}, f"builtin_{expected_status}.inputs.paths")
    deck_path = evidence / branch["deck"].removeprefix("/evidence/")
    _file_record(inputs[branch["deck"]], deck_path, role="input", kind="spice-netlist")
    _expect(inputs[branch["deck"]]["sha256"], deck_sha256, f"builtin_{expected_status}.deck_sha256")
    artifacts = _records_by_path(result["artifacts"], f"builtin_{expected_status}.artifacts")
    _expect(set(artifacts), {branch["log_artifact"], branch["raw_artifact"]}, f"builtin_{expected_status}.artifacts.paths")
    log_path = evidence / branch["log_artifact"].removeprefix("/evidence/")
    raw_path = evidence / branch["raw_artifact"].removeprefix("/evidence/")
    _file_record(artifacts[branch["log_artifact"]], log_path, role="simulation.log", kind="simulation-log")
    _file_record(artifacts[branch["raw_artifact"]], raw_path, role="simulation.result", kind="ngspice-raw")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    if expected_status == "pass":
        if NATIVE_ERROR_RE.search(log) or "No. of Data Rows : 81" not in log:
            raise ConformanceError("built-in pass log lacks clean completed-analysis evidence")
        parsed = _parse_binary_raw(raw_path, manifest["extensions"]["org.openada"]["waveform"])
    else:
        for marker in manifest["extensions"]["org.openada"]["terminal_nonconvergence"]["expected"]["log_markers"]:
            if marker not in log:
                raise ConformanceError(f"built-in terminal log lacks marker {marker!r}")
        _expect(
            "simulation.analysis.non_convergent" in {item.get("code") for item in result["diagnostics"]},
            True,
            "builtin_fail.nonconvergence_diagnostic",
        )
        parsed = _parse_partial_failure_raw(raw_path)
    return raw_path, parsed


def _verify_native_artifacts(
    result: dict[str, Any], evidence: Path, *, destination: str, stem: str, raw_name: str
) -> tuple[Path, Path, Path]:
    expected_paths = {
        f"{destination}/simulation/{stem}.log": ("simulation.log", None),
        f"{destination}/simulation/{stem}.openada-control.sp": ("simulation.launcher", None),
        f"{destination}/work/{raw_name}": ("simulation.result", "ngspice-raw"),
    }
    artifacts = _records_by_path(result["artifacts"], "provider_result.artifacts")
    _expect(set(artifacts), set(expected_paths), "provider_result.artifacts.paths")
    paths: list[Path] = []
    for recorded_path, (role, kind) in expected_paths.items():
        local = evidence / recorded_path.removeprefix("/evidence/")
        _file_record(artifacts[recorded_path], local, role=role, kind=kind)
        paths.append(local)
    return paths[0], paths[1], paths[2]


def _parse_partial_failure_raw(path: Path) -> dict[str, Any]:
    size = _require_regular_file(path, label="partial nonconvergence raw", maximum_bytes=MAX_ARTIFACT_BYTES)
    if not 17_000 <= size <= 18_000:
        raise ConformanceError(
            f"partial nonconvergence raw has implausible bounded size {size} bytes"
        )
    body = path.read_bytes()
    handle = BytesIO(body)
    consumed = 0
    first, consumed = _read_bounded_line(handle, consumed)
    if not first.startswith(b"Title:"):
        raise ConformanceError("partial raw lacks a Spice3 Title header")
    header: dict[str, str] = {}
    while True:
        line, consumed = _read_bounded_line(handle, consumed)
        if not line:
            raise ConformanceError("partial raw header is truncated")
        if line.strip().lower() == b"variables:":
            break
        key, separator, value = line.partition(b":")
        if not separator:
            raise ConformanceError("partial raw header line is malformed")
        normalized = b" ".join(key.strip().lower().split()).decode("ascii")
        if normalized in header:
            raise ConformanceError(f"partial raw repeats header {normalized!r}")
        header[normalized] = value.strip().decode("utf-8", errors="strict")
    _expect(header.get("plotname"), "Transient Analysis", "partial_raw.plotname")
    _expect(header.get("flags", "").casefold(), "real", "partial_raw.flags")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except (KeyError, ValueError) as exc:
        raise ConformanceError("partial raw dimensions are invalid") from exc
    _expect(point_count, 162, "partial_raw.point_count")
    _expect(variable_count, 13, "partial_raw.variable_count")
    variables: list[tuple[str, str]] = []
    for index in range(variable_count):
        line, consumed = _read_bounded_line(handle, consumed)
        fields = line.decode("utf-8", errors="strict").split()
        if len(fields) < 3 or fields[0] != str(index):
            raise ConformanceError(f"partial raw variable row {index} is malformed")
        variables.append((fields[1].casefold(), fields[2].casefold()))
    marker, consumed = _read_bounded_line(handle, consumed)
    if marker.strip().lower() != b"binary:":
        raise ConformanceError("partial raw is not binary Spice3 data")
    names = [item[0] for item in variables]
    for required in ("time", "v(vin)", "v(vout)", "v(__openada_fail)"):
        if required not in names:
            raise ConformanceError(f"partial raw lacks {required!r}")
    binary = handle.read()
    expected = variable_count * point_count * 8
    if len(binary) != expected:
        raise ConformanceError(
            f"partial raw payload has {len(binary)} bytes, expected {expected}"
        )
    values = struct.unpack(f"={variable_count * point_count}d", binary)
    if not all(math.isfinite(value) for value in values):
        raise ConformanceError("partial raw contains a non-finite scalar")
    time_index = names.index("time")
    times = [values[row * variable_count + time_index] for row in range(point_count)]
    _close(times[0], 0.0, "partial_raw.time[0]", atol=1e-18)
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ConformanceError("partial raw time values are not strictly increasing")
    if not 1e-4 < times[-1] < 1.05e-4:
        raise ConformanceError(
            f"partial raw does not stop at the reviewed post-start failure boundary: {times[-1]!r}"
        )
    return {
        "bytes": size,
        "point_count": point_count,
        "variable_count": variable_count,
        "last_time_s": times[-1],
        "variables": variables,
    }


def _verify_provider_result(
    manifest: dict[str, Any],
    evidence: Path,
    result: dict[str, Any],
    result_validator: Draft202012Validator,
    data_validator: Draft202012Validator,
    *,
    expected_status: str,
    request: dict[str, Any],
    deck_sha256: str,
    destination: str,
    stem: str,
    raw_name: str,
) -> tuple[Path, dict[str, Any] | None]:
    _validate(result, result_validator, label=f"provider {expected_status} result")
    _validate(result["data"], data_validator, label=f"provider {expected_status} data")
    _expect(result["operation"], "simulate", f"provider_{expected_status}.operation")
    _expect(result["engineering"]["status"], expected_status, f"provider_{expected_status}.engineering.status")
    _expect(result["execution"]["status"], "completed", f"provider_{expected_status}.execution.status")
    if expected_status == "pass":
        _expect(result["execution"]["exit_code"], 0, "provider_pass.execution.exit_code")
    else:
        # ngspice reports this terminal timestep failure in its transcript while
        # exiting zero; the typed engineering status is deliberately derived
        # from the retained native log/raw pair rather than the process code.
        _expect(result["execution"]["exit_code"], 0, "provider_fail.execution.exit_code")
    ngspice = next(item for item in manifest["runtime"]["tools"] if item["id"] == "ngspice")
    executable_snapshot = f"{destination}/work/openada-native-ngspice"
    _expect(
        result["tool"],
        {
            "name": "ngspice",
            "path": executable_snapshot,
            "version": ngspice["version"],
        },
        f"provider_{expected_status}.tool",
    )
    command = result["execution"]["command"]
    if not isinstance(command, list) or len(command) != 6:
        raise ConformanceError("provider native ngspice command has an unexpected shape")
    expected_script = f"{destination}/simulation/{stem}.openada-control.sp"
    if command[:4] != [executable_snapshot, "-i", "-n", "-o"] or NGSPICE_TEMP_LOG_RE.fullmatch(command[4]) is None:
        raise ConformanceError("provider native ngspice command differs from the reviewed launcher")
    _expect(command[5], expected_script, f"provider_{expected_status}.command.script")
    _expect(result["execution"]["cwd"], f"{destination}/work", f"provider_{expected_status}.execution.cwd")

    protocol = result["data"]["protocol"]
    provider = manifest["extensions"]["org.openada"]["provider"]
    _expect(protocol, {
        "request_id": request["request_id"],
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "driver_id": provider["driver_id"],
        "driver_version": provider["driver_version"],
    }, f"provider_{expected_status}.protocol")
    analysis = result["data"]["analysis"]
    _expect(analysis["type"], "tran", f"provider_{expected_status}.analysis.type")
    if expected_status == "pass":
        _expect(analysis["completion"], "completed", "provider_pass.analysis.completion")
        _expect(analysis["convergence"], "converged", "provider_pass.analysis.convergence")
    else:
        _expect(analysis["completion"], "terminal-failure", "provider_fail.analysis.completion")
        _expect(analysis["convergence"], "non-converged", "provider_fail.analysis.convergence")
    evidence_record = result["data"]["evidence"]
    _expect(evidence_record["request_binding"], "exact", f"provider_{expected_status}.evidence.request_binding")
    _expect(evidence_record["freshness"], "fresh", f"provider_{expected_status}.evidence.freshness")
    _expect(evidence_record["provenance"], "bounded", f"provider_{expected_status}.evidence.provenance")
    _expect(evidence_record["structure"], "valid", f"provider_{expected_status}.evidence.structure")
    extension = result["data"]["extensions"]["org.openada"]
    _expect(extension["backend"], "ngspice", f"provider_{expected_status}.extension.backend")
    _expect(extension["parameters"], request["parameters"], f"provider_{expected_status}.extension.parameters")

    log_path, script_path, raw_path = _verify_native_artifacts(
        result, evidence, destination=destination, stem=stem, raw_name=raw_name
    )
    script = (
        "*ng_script_with_params\n"
        "set noaskquit\n"
        "source /foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit\n"
        f"source {request['target']['locator']['path']}\n"
        "quit\n"
    ).encode("ascii")
    if script_path.read_bytes() != script:
        raise ConformanceError("provider control launcher differs from the reviewed explicit startup chain")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    if expected_status == "pass":
        if NATIVE_ERROR_RE.search(log):
            raise ConformanceError("passing provider log contains native error evidence")
        if f'binary raw file "{raw_name}"' not in log or "ngspice-46 done" not in log:
            raise ConformanceError("passing provider log lacks raw-write and completion evidence")
        parsed = _parse_binary_raw(
            raw_path, manifest["extensions"]["org.openada"]["waveform"]
        )
        _expect(analysis["point_count"], parsed["point_count"], "provider_pass.analysis.point_count")
        _expect(analysis["dependent_variable_count"], len(parsed["variables"]) - 1, "provider_pass.analysis.dependent_variable_count")
        _expect(analysis["finite_value_count"], parsed["point_count"] * (len(parsed["variables"]) - 1), "provider_pass.analysis.finite_value_count")
    else:
        markers = manifest["extensions"]["org.openada"]["terminal_nonconvergence"]["expected"]["log_markers"]
        for marker in markers:
            if marker not in log:
                raise ConformanceError(f"terminal nonconvergence log lacks marker {marker!r}")
        codes = {item.get("code") for item in result["diagnostics"]}
        if "simulation.analysis.non_convergent" not in codes:
            raise ConformanceError("provider fail lacks normalized terminal nonconvergence diagnostic")
        parsed = _parse_partial_failure_raw(raw_path)
        _expect(analysis["point_count"], parsed["point_count"], "provider_fail.analysis.point_count")
        _expect(
            analysis["dependent_variable_count"],
            parsed["variable_count"] - 1,
            "provider_fail.analysis.dependent_variable_count",
        )
        _expect(
            analysis["finite_value_count"],
            parsed["point_count"] * (parsed["variable_count"] - 1),
            "provider_fail.analysis.finite_value_count",
        )

    inputs = _records_by_path(result["inputs"], f"provider_{expected_status}.inputs")
    native_executable_sha256 = manifest["runtime"]["extensions"]["org.openada"][
        "ngspice_executable"
    ]["sha256"]
    expected_hashes = {
        request["target"]["locator"]["path"]: deck_sha256,
        "/evidence/provider-config.json": request["configuration"][0]["locator"]["sha256"],
        "/foss/pdks/ihp-sg13g2/COMMIT": request["configuration"][1]["locator"]["sha256"],
        "/foss/tools/ngspice/bin/ngspice": native_executable_sha256,
        executable_snapshot: native_executable_sha256,
        "/foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit": manifest["runtime"]["extensions"]["org.openada"]["pdk"]["ngspice_init"]["sha256"],
        "/foss/tools/ngspice/share/ngspice/scripts/spinit": manifest["runtime"]["extensions"]["org.openada"]["ngspice_system_init"]["sha256"],
    }
    _expect(set(inputs), set(expected_hashes), f"provider_{expected_status}.inputs.paths")
    for path, digest in expected_hashes.items():
        _expect(inputs[path]["sha256"], digest, f"provider_{expected_status}.inputs[{path}].sha256")
        _expect(inputs[path]["exists"], True, f"provider_{expected_status}.inputs[{path}].exists")
    snapshot_path = evidence / executable_snapshot.removeprefix("/evidence/")
    _file_record(
        inputs[executable_snapshot],
        snapshot_path,
        role="configuration",
        kind="eda-executable-snapshot",
    )
    _expect(
        inputs[executable_snapshot]["bytes"],
        inputs["/foss/tools/ngspice/bin/ngspice"]["bytes"],
        f"provider_{expected_status}.executable_snapshot.bytes",
    )
    return raw_path, parsed


def _static_protocol(result: dict[str, Any], *, operation: str, request_id: str) -> None:
    expected = {
        "result.series.extract": (
            "openada.operation/result.series.extract/v1alpha1",
            "openada.assertion/series.extraction.valid/v1alpha1",
            "org.openada.kernel.spice3-series",
        ),
        "result.measure": (
            "openada.operation/result.measure/v1alpha1",
            "openada.assertion/measurement.valid/v1alpha1",
            "org.openada.kernel.typed-evidence",
        ),
        "specification.evaluate": (
            "openada.operation/specification.evaluate/v1alpha1",
            "openada.assertion/specification.satisfied/v1alpha1",
            "org.openada.kernel.typed-evidence",
        ),
    }[operation]
    _expect(
        result["data"]["protocol"],
        {
            "request_id": request_id,
            "operation_profile": expected[0],
            "assertion_profile": expected[1],
            "implementation_id": expected[2],
            "implementation_version": "1.0.0",
        },
        f"{operation}.protocol",
    )
    _expect(result["tool"], None, f"{operation}.tool")
    _expect(result["inputs"], [] if operation != "result.series.extract" else result["inputs"], f"{operation}.inputs")
    _expect(result["artifacts"], [], f"{operation}.artifacts")


def _normalized_series(
    manifest: dict[str, Any], parsed: dict[str, Any], raw_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = manifest["extensions"]["org.openada"]["workflow"]
    columns = parsed["columns"]
    content = {
        "axis": {"name": "time", "unit": "s", "values": columns["time"]},
        "signals": [
            {"name": "v(vin)", "unit": "V", "values": columns["v(vin)"]},
            {"name": "v(vout)", "unit": "V", "values": columns["v(vout)"]},
        ],
        "conditions": workflow["conditions"],
    }
    series_sha256 = _canonical_sha256(content)
    series = {
        "source": {
            "operation": "openada.operation/result.series.extract/v1alpha1",
            "request_id": workflow["extract_request_id"],
            "artifact_role": "measurement.source",
            "artifact_sha256": series_sha256,
            "lineage": {
                "operation": "circuit.simulate",
                "request_id": PROVIDER_REQUEST_ID,
                "artifact_role": "simulation.result",
                "artifact_sha256": raw_sha256,
                "binding": "unverified",
            },
        },
        **content,
        "extensions": {},
    }
    measurement_source = {
        **series["source"],
        "series_sha256": series_sha256,
        "conditions_sha256": _canonical_sha256(workflow["conditions"]),
        "conditions": workflow["conditions"],
    }
    return series, measurement_source


def _verify_extraction(
    manifest: dict[str, Any],
    evidence: Path,
    result: dict[str, Any],
    parsed: dict[str, Any],
    raw_path: Path,
    validator: Draft202012Validator,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _verify_result_base(
        result,
        operation="result.series.extract",
        engineering="pass",
        validator=validator,
    )
    workflow = manifest["extensions"]["org.openada"]["workflow"]
    _static_protocol(
        result,
        operation="result.series.extract",
        request_id=workflow["extract_request_id"],
    )
    raw_sha256 = sha256_file(raw_path)
    inputs = _records_by_path(result["inputs"], "extract.inputs")
    _expect(set(inputs), {str(raw_path).replace(str(evidence), "/evidence", 1)}, "extract.inputs.paths")
    _file_record(
        next(iter(inputs.values())), raw_path, role="simulation.result", kind="ngspice-raw"
    )
    selection = _read_json(evidence / "extract-selection.json", label="extract selection")
    _expect(
        selection,
        {
            "selectors": workflow["selectors"],
            "conditions": workflow["conditions"],
            "extensions": {},
        },
        "extract.selection",
    )
    expected_request_sha256 = _canonical_sha256(
        {
            "simulation": {
                "request_id": PROVIDER_REQUEST_ID,
                "artifact_sha256": raw_sha256,
            },
            "selectors": workflow["selectors"],
            "conditions": workflow["conditions"],
        }
    )
    expected_series, measurement_source = _normalized_series(
        manifest, parsed, raw_sha256
    )
    extraction = result["data"]["extraction"]
    _expect(extraction["status"], "extracted", "extract.status")
    _expect(extraction["request_sha256"], expected_request_sha256, "extract.request_sha256")
    _expect(extraction["series"], expected_series, "extract.series")
    source = extraction["source"]
    _expect(source["binding"], "verified", "extract.source.binding")
    _expect(source["request_id"], PROVIDER_REQUEST_ID, "extract.source.request_id")
    _expect(source["driver_id"], "org.openada.driver.ngspice-pdk-control", "extract.source.driver_id")
    _expect(source["driver_version"], "0.5.0", "extract.source.driver_version")
    _expect(source["analysis_type"], "tran", "extract.source.analysis_type")
    _expect(source["artifact"]["sha256"], raw_sha256, "extract.source.artifact.sha256")
    _expect(
        extraction["plot"],
        {
            "plotname": "Transient Analysis",
            "encoding": "binary",
            "numeric_type": "real",
            "point_count": parsed["point_count"],
            "native_axis_name": "time",
            "native_axis_type": "time",
            "extensions": {},
        },
        "extract.plot",
    )
    return expected_series, measurement_source


def _crossing_points(
    axis: list[float], values: list[float], threshold: float, direction: str
) -> list[float]:
    found: list[float] = []
    for x0, x1, y0, y1 in zip(axis, axis[1:], values, values[1:]):
        rising = y0 < threshold <= y1 and y1 > y0
        falling = y0 > threshold >= y1 and y1 < y0
        if (direction in {"rising", "either"} and rising) or (
            direction in {"falling", "either"} and falling
        ):
            found.append(x0 + (threshold - y0) * (x1 - x0) / (y1 - y0))
    return found


def _ordinary_measurement(
    request: dict[str, Any], series: dict[str, Any]
) -> tuple[float | None, str, float | None, int]:
    axis = series["axis"]["values"]
    signal = next(item for item in series["signals"] if item["name"] == request["signal"])
    values = signal["values"]
    kind, parameters = request["kind"], request["parameters"]
    if kind == "sample_at":
        at = float(parameters["at"]["value"])
        if not axis[0] <= at <= axis[-1]:
            raise ConformanceError("measurement.domain.invalid")
        for index, coordinate in enumerate(axis):
            if coordinate == at:
                return values[index], signal["unit"], at, 1
            if coordinate > at:
                if parameters["interpolation"] == "linear" and index > 0:
                    x0, x1 = axis[index - 1], coordinate
                    y0, y1 = values[index - 1], values[index]
                    return y0 + (y1 - y0) * ((at - x0) / (x1 - x0)), signal["unit"], at, 1
                return None, signal["unit"], None, 0
        return None, signal["unit"], None, 0
    if kind in {"minimum", "maximum", "mean", "rms"}:
        if "window" in parameters:
            start = float(parameters["window"]["start"]["value"])
            stop = float(parameters["window"]["stop"]["value"])
            indices = [index for index, value in enumerate(axis) if start <= value <= stop]
        else:
            indices = list(range(len(axis)))
        if not indices:
            return None, signal["unit"], None, 0
        samples = [values[index] for index in indices]
        if kind == "minimum":
            value = min(samples)
            return value, signal["unit"], axis[indices[samples.index(value)]], len(indices)
        if kind == "maximum":
            value = max(samples)
            return value, signal["unit"], axis[indices[samples.index(value)]], len(indices)
        if kind == "mean":
            return math.fsum(samples) / len(samples), signal["unit"], None, len(indices)
        return math.sqrt(math.fsum(value * value for value in samples) / len(samples)), signal["unit"], None, len(indices)
    if kind == "crossing":
        crossings = _crossing_points(
            axis,
            values,
            float(parameters["threshold"]["value"]),
            parameters["direction"],
        )
        occurrence = int(parameters["occurrence"])
        value = crossings[occurrence - 1] if len(crossings) >= occurrence else None
        return value, series["axis"]["unit"], value, len(axis)
    if kind in {"rise_time", "fall_time"}:
        direction = "rising" if kind == "rise_time" else "falling"
        lower = float(parameters["lower_threshold"]["value"])
        upper = float(parameters["upper_threshold"]["value"])
        starts = _crossing_points(axis, values, lower if direction == "rising" else upper, direction)
        ends = _crossing_points(axis, values, upper if direction == "rising" else lower, direction)
        occurrence = int(parameters["occurrence"])
        value: float | None = None
        location: float | None = None
        if len(starts) >= occurrence:
            start = starts[occurrence - 1]
            next_start = starts[occurrence] if len(starts) > occurrence else math.inf
            location = next((item for item in ends if start <= item < next_start), None)
            value = None if location is None else location - start
        return value, series["axis"]["unit"], location, len(axis)
    target = float(parameters["target"]["value"])
    tolerance = float(parameters["tolerance"]["value"])
    reference = float(parameters["reference"]["value"])
    hold_for = float(parameters["hold_for"]["value"])
    if not axis[0] <= reference <= axis[-1]:
        raise ConformanceError("measurement.domain.invalid")
    indices = [index for index, coordinate in enumerate(axis) if coordinate >= reference]
    settled: int | None = None
    suffix_inside = True
    if indices:
        final = indices[-1]
        for index in reversed(indices):
            suffix_inside = suffix_inside and abs(values[index] - target) <= tolerance
            if suffix_inside and axis[final] - axis[index] >= hold_for:
                settled = index
    if settled is None:
        return None, series["axis"]["unit"], None, len(indices)
    return axis[settled] - reference, series["axis"]["unit"], axis[settled], len(indices)


def _expected_measurement(
    request: dict[str, Any],
    series: dict[str, Any],
    source: dict[str, Any],
    *,
    allow_domain_failure: bool = False,
) -> tuple[dict[str, Any], str, str]:
    try:
        value, unit, location, sample_count = _ordinary_measurement(request, series)
    except ConformanceError as exc:
        if not allow_domain_failure or str(exc) != "measurement.domain.invalid":
            raise
        return (
            {
                "measurement_id": request["measurement_id"],
                "kind": request["kind"],
                "status": "unknown",
                "request_sha256": None,
                "value": None,
                "unit": None,
                "signal": request["signal"],
                "location": None,
                "algorithm": {
                    "id": f"openada.algorithm/measurement.{request['kind'].replace('_', '-')}/v1",
                    "version": "1.0.0",
                },
                "sample_count": 0,
                "source": source,
                "extensions": {},
            },
            "unknown",
            "measurement.domain.invalid",
        )
    record = {
        "measurement_id": request["measurement_id"],
        "kind": request["kind"],
        "status": "measured" if value is not None else "not_found",
        "request_sha256": _canonical_sha256(request),
        "value": value,
        "unit": unit,
        "signal": request["signal"],
        "location": (
            {"value": location, "unit": series["axis"]["unit"]}
            if location is not None
            else None
        ),
        "algorithm": {
            "id": f"openada.algorithm/measurement.{request['kind'].replace('_', '-')}/v1",
            "version": "1.0.0",
        },
        "sample_count": sample_count,
        "source": source,
        "extensions": {},
    }
    return (
        record,
        "pass" if value is not None else "fail",
        "" if value is not None else "measurement.value.not_found",
    )


def _verify_measurement_result(
    result: dict[str, Any],
    request: dict[str, Any],
    expected: dict[str, Any],
    *,
    request_id: str,
    engineering: str,
    diagnostic_code: str,
    validator: Draft202012Validator,
) -> None:
    _validate(result, validator, label=f"measurement {request['measurement_id']}")
    _expect(result["operation"], "result.measure", "measurement.operation")
    _expect(result["engineering"]["status"], engineering, "measurement.engineering.status")
    _expect(
        result["execution"]["status"],
        "invalid_request" if diagnostic_code == "measurement.domain.invalid" else "completed",
        "measurement.execution.status",
    )
    _expect(
        result["execution"]["exit_code"],
        None if diagnostic_code == "measurement.domain.invalid" else 0,
        "measurement.execution.exit_code",
    )
    _static_protocol(result, operation="result.measure", request_id=request_id)
    _expect(result["data"]["measurement"], expected, "measurement.record")
    codes = [item.get("code") for item in result["diagnostics"]]
    if diagnostic_code:
        _expect(codes, [diagnostic_code], "measurement.diagnostic_codes")
    else:
        _expect(codes, [], "measurement.diagnostic_codes")


def _negative_measurement_request(definition: dict[str, Any]) -> dict[str, Any]:
    request = deepcopy(definition["request"])
    kind = request["kind"]
    if kind == "sample_at":
        request["parameters"] = {"at": {"value": 3e-6, "unit": "s"}, "interpolation": "linear"}
    elif kind in {"minimum", "maximum", "mean", "rms"}:
        request["parameters"] = {
            "window": {
                "start": {"value": 3e-6, "unit": "s"},
                "stop": {"value": 4e-6, "unit": "s"},
            }
        }
    elif kind == "crossing":
        request["parameters"] = {"threshold": {"value": 2.0, "unit": "V"}, "direction": "rising", "occurrence": 1}
    elif kind in {"rise_time", "fall_time"}:
        request["parameters"] = {
            "lower_threshold": {"value": 2.0, "unit": "V"},
            "upper_threshold": {"value": 3.0, "unit": "V"},
            "occurrence": 1,
        }
    else:
        request["parameters"] = {
            "target": {"value": 10.0, "unit": "V"},
            "tolerance": {"value": 0.01, "unit": "V"},
            "reference": {"value": 1.5e-6, "unit": "s"},
            "hold_for": {"value": 2e-7, "unit": "s"},
        }
    return request


def _verify_negative_extraction(
    manifest: dict[str, Any],
    evidence: Path,
    raw_path: Path,
    result: dict[str, Any],
    validator: Draft202012Validator,
) -> None:
    workflow = manifest["extensions"]["org.openada"]["workflow"]
    selection = {
        "selectors": [
            {
                "native_name": "v(__openada_missing__)",
                "output_name": "v(__openada_missing__)",
                "unit": "V",
                "component": "real",
            }
        ],
        "conditions": workflow["conditions"],
        "extensions": {},
    }
    _expect(
        _read_json(evidence / "requests/extract-missing-selector.json", label="negative extraction selection"),
        selection,
        "negative_extract.selection",
    )
    _validate(result, validator, label="negative extraction result")
    _expect(result["operation"], "result.series.extract", "negative_extract.operation")
    _expect(result["engineering"]["status"], "unknown", "negative_extract.engineering.status")
    _expect(result["execution"]["status"], "completed", "negative_extract.execution.status")
    _static_protocol(
        result,
        operation="result.series.extract",
        request_id="15000000-0000-4000-8000-000000000001",
    )
    _expect([item.get("code") for item in result["diagnostics"]], ["series.selector.missing"], "negative_extract.diagnostics")
    raw_sha256 = sha256_file(raw_path)
    expected_digest = _canonical_sha256(
        {
            "simulation": {"request_id": PROVIDER_REQUEST_ID, "artifact_sha256": raw_sha256},
            "selectors": selection["selectors"],
            "conditions": selection["conditions"],
        }
    )
    _expect(result["data"]["extraction"]["request_sha256"], expected_digest, "negative_extract.request_sha256")
    _expect(result["data"]["extraction"]["status"], "unknown", "negative_extract.status")
    _expect(result["data"]["extraction"]["series"], None, "negative_extract.series")


def _normalized_specification(request: dict[str, Any]) -> dict[str, Any]:
    conditions = [
        {
            "name": item["name"],
            "value": (
                float(item["value"])
                if isinstance(item["value"], (int, float)) and not isinstance(item["value"], bool)
                else item["value"]
            ),
            "unit": item["unit"],
        }
        for item in request["conditions"]
    ]
    limits = {
        name: {
            "value": float(bound["value"]),
            "unit": bound["unit"],
            "inclusive": bound["inclusive"],
        }
        for name, bound in request["limits"].items()
    }
    return {
        "specification_id": request["specification_id"],
        "measurement_id": request["measurement_id"],
        "limits": limits,
        "conditions": conditions,
        "extensions": {},
    }


def _verify_specification_result(
    result: dict[str, Any],
    measurement: dict[str, Any],
    request: dict[str, Any],
    *,
    request_id: str,
    expected_status: str,
    validator: Draft202012Validator,
) -> dict[str, Any]:
    _validate(result, validator, label=f"specification {request['specification_id']}")
    _expect(result["operation"], "specification.evaluate", "specification.operation")
    _expect(result["engineering"]["status"], expected_status, "specification.engineering.status")
    _expect(result["execution"]["status"], "completed", "specification.execution.status")
    _expect(result["execution"]["exit_code"], 0, "specification.execution.exit_code")
    _static_protocol(result, operation="specification.evaluate", request_id=request_id)
    normalized = _normalized_specification(request)
    measured_value = float(measurement["value"])
    margins: list[tuple[float, str]] = []
    violations: list[str] = []
    for name, bound in normalized["limits"].items():
        margin = measured_value - bound["value"] if name == "lower" else bound["value"] - measured_value
        margins.append((margin, name))
        if name == "lower":
            violated = measured_value < bound["value"] or (measured_value == bound["value"] and not bound["inclusive"])
        else:
            violated = measured_value > bound["value"] or (measured_value == bound["value"] and not bound["inclusive"])
        if violated:
            violations.append(name)
    limiting_margin, limiting_bound = min(margins, key=lambda item: item[0])
    oracle_status = "fail" if violations else "pass"
    _expect(oracle_status, expected_status, "specification.oracle_status")
    expected_evaluation = {
        "specification_id": normalized["specification_id"],
        "measurement_id": measurement["measurement_id"],
        "status": expected_status,
        "measured": {"value": measured_value, "unit": measurement["unit"]},
        "limits": [
            {"kind": name, **bound} for name, bound in normalized["limits"].items()
        ],
        "conditions": {
            "status": "matched",
            "required_count": len(normalized["conditions"]),
            "matched_count": len(normalized["conditions"]),
        },
        "margin": {
            "value": limiting_margin,
            "unit": measurement["unit"],
            "relative_to": limiting_bound,
        },
        "algorithm": {
            "id": "openada.algorithm/specification.closed-interval/v1",
            "version": "1.0.0",
        },
        "source": {
            "measurement_sha256": _canonical_sha256(measurement),
            "measurement_source": measurement["source"],
            "specification_sha256": _canonical_sha256(normalized),
            "specification": normalized,
        },
        "extensions": {},
    }
    _expect(result["data"]["evaluation"], expected_evaluation, "specification.evaluation")
    codes = [item.get("code") for item in result["diagnostics"]]
    _expect(codes, ["specification.limit.violated"] if expected_status == "fail" else [], "specification.diagnostics")
    return expected_evaluation


def _verify_condition_mismatch(
    result: dict[str, Any],
    measurement: dict[str, Any],
    request: dict[str, Any],
    validator: Draft202012Validator,
) -> None:
    _validate(result, validator, label="condition-mismatch specification")
    _expect(result["operation"], "specification.evaluate", "condition_mismatch.operation")
    _expect(result["engineering"]["status"], "unknown", "condition_mismatch.engineering.status")
    _expect(result["execution"]["status"], "completed", "condition_mismatch.execution.status")
    _expect(result["execution"]["exit_code"], 0, "condition_mismatch.execution.exit_code")
    _static_protocol(
        result,
        operation="specification.evaluate",
        request_id="17000000-0000-4000-8000-000000000001",
    )
    normalized = _normalized_specification(request)
    expected = {
        "specification_id": normalized["specification_id"],
        "measurement_id": measurement["measurement_id"],
        "status": "unknown",
        "measured": {"value": measurement["value"], "unit": measurement["unit"]},
        "limits": [
            {"kind": name, **bound} for name, bound in normalized["limits"].items()
        ],
        "conditions": {
            "status": "not-established",
            "required_count": 2,
            "matched_count": 1,
        },
        "margin": None,
        "algorithm": {
            "id": "openada.algorithm/specification.closed-interval/v1",
            "version": "1.0.0",
        },
        "source": {
            "measurement_sha256": _canonical_sha256(measurement),
            "measurement_source": measurement["source"],
            "specification_sha256": _canonical_sha256(normalized),
            "specification": normalized,
        },
        "extensions": {},
    }
    _expect(result["data"]["evaluation"], expected, "condition_mismatch.evaluation")
    _expect(
        [item.get("code") for item in result["diagnostics"]],
        ["specification.condition.unproven"],
        "condition_mismatch.diagnostics",
    )


def _verify_agent_evidence(
    manifest: dict[str, Any],
    agent: dict[str, Any],
    netlist_negative: dict[str, Any],
    provider: dict[str, Any],
    terminal: dict[str, Any],
    builtin: dict[str, Any],
    builtin_terminal: dict[str, Any],
    extraction: dict[str, Any],
    measurement_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    evaluations: list[dict[str, Any]],
) -> None:
    _expect(set(agent), {
        "schema", "chain_id", "provider", "terminal_nonconvergence",
        "native_artifact", "series", "measurements", "specifications",
        "negative_replays", "limitations", "netlist_negative", "builtin_ngspice",
    }, "agent_evidence.keys")
    _expect(agent["schema"], "openada.agent-evidence/v0alpha1", "agent_evidence.schema")
    _expect(agent["chain_id"], manifest["id"], "agent_evidence.chain_id")
    protocol = provider["data"]["protocol"]
    _expect(agent["provider"], {
        "request_id": protocol["request_id"],
        "driver_id": protocol["driver_id"],
        "driver_version": protocol["driver_version"],
        "engineering_status": "pass",
    }, "agent_evidence.provider")
    negative_netlist_artifact = netlist_negative["artifacts"][0]
    _expect(
        agent["netlist_negative"],
        {
            "engineering_status": "fail",
            "diagnostic_codes": [item["code"] for item in netlist_negative["diagnostics"]],
            "missing_symbol_count": 1,
            "artifact": negative_netlist_artifact,
        },
        "agent_evidence.netlist_negative",
    )
    builtin_raw = next(item for item in builtin["artifacts"] if item["role"] == "simulation.result")
    builtin_terminal_raw = next(
        item for item in builtin_terminal["artifacts"] if item["role"] == "simulation.result"
    )
    _expect(
        agent["builtin_ngspice"],
        {
            "pass": {
                "driver_id": builtin["data"]["protocol"]["driver_id"],
                "driver_version": builtin["data"]["protocol"]["driver_version"],
                "engineering_status": "pass",
                "native_artifact": builtin_raw,
            },
            "terminal_nonconvergence": {
                "engineering_status": "fail",
                "diagnostic_codes": [item["code"] for item in builtin_terminal["diagnostics"]],
                "native_artifact": builtin_terminal_raw,
            },
        },
        "agent_evidence.builtin_ngspice",
    )
    raw = extraction["data"]["extraction"]["source"]["artifact"]
    _expect(agent["native_artifact"], {
        "path": raw["path"], "bytes": raw["bytes"], "sha256": raw["sha256"],
        "role": raw["role"], "kind": raw["kind"],
    }, "agent_evidence.native_artifact")
    series = extraction["data"]["extraction"]["series"]
    _expect(agent["series"], {
        "request_id": series["source"]["request_id"],
        "sha256": series["source"]["artifact_sha256"],
        "axis": "time", "axis_unit": "s",
        "point_count": len(series["axis"]["values"]),
        "signals": ["v(vin)", "v(vout)"],
        "conditions": series["conditions"],
    }, "agent_evidence.series")
    expected_metrics: list[dict[str, Any]] = []
    for definition, result in measurement_pairs:
        measured = result["data"]["measurement"]
        expected_metrics.append({
            "id": definition["id"], "kind": measured["kind"],
            "signal": measured["signal"], "status": measured["status"],
            "value": measured["value"], "unit": measured["unit"],
            "location": measured["location"], "algorithm": measured["algorithm"],
            "measurement_request_sha256": measured["request_sha256"],
            "result_sha256": _canonical_sha256(result),
            "interpretation": definition["interpretation"],
        })
    _expect(agent["measurements"], expected_metrics, "agent_evidence.measurements")
    decisions = [
        {
            "specification_id": item["data"]["evaluation"]["specification_id"],
            "measurement_id": item["data"]["evaluation"]["measurement_id"],
            "status": item["data"]["evaluation"]["status"],
            "measured": item["data"]["evaluation"]["measured"],
            "limits": item["data"]["evaluation"]["limits"],
            "margin": item["data"]["evaluation"]["margin"],
            "conditions": item["data"]["evaluation"]["conditions"],
        }
        for item in evaluations
    ]
    _expect(agent["specifications"], {
        "pass_count": 9, "fail_count": 9, "decisions": decisions,
    }, "agent_evidence.specifications")
    negative_expected = [
        {"id": "netlist-missing-symbol", "operation": "netlist", "engineering_status": "fail", "diagnostic": "xschem.missing_symbol"},
        {"id": "isolated-builtin-terminal-nonconvergence", "operation": "simulate", "engineering_status": "fail", "diagnostic": "simulation.analysis.non_convergent"},
        {"id": "extract-missing-selector", "operation": "result.series.extract", "engineering_status": "unknown", "diagnostic": "series.selector.missing"},
        {"id": "isolated-terminal-nonconvergence", "operation": "simulate", "engineering_status": "fail", "diagnostic": "simulation.analysis.non_convergent"},
    ]
    for position, definition in enumerate(
        manifest["extensions"]["org.openada"]["measurements"], start=1
    ):
        diagnostic = "measurement.domain.invalid" if definition["id"] == "sample-at" else "measurement.value.not_found"
        status = "unknown" if definition["id"] == "sample-at" else "fail"
        negative_expected.append({
            "id": f"measure-{definition['id']}", "operation": "result.measure",
            "engineering_status": status, "diagnostic": diagnostic,
        })
        if position == 1:
            negative_expected.append(
                {
                    "id": "specification-condition-mismatch",
                    "operation": "specification.evaluate",
                    "engineering_status": "unknown",
                    "diagnostic": "specification.condition.unproven",
                }
            )
    negative_expected.append(
        {
            "id": "deliberately-violated-limits",
            "operation": "specification.evaluate",
            "engineering_status": "fail",
            "diagnostic": "specification.limit.violated",
        }
    )
    _expect(agent["negative_replays"], negative_expected, "agent_evidence.negative_replays")
    terminal_artifact = next(item for item in terminal["artifacts"] if item["role"] == "simulation.result")
    _expect(agent["terminal_nonconvergence"], {
        "purpose": manifest["extensions"]["org.openada"]["terminal_nonconvergence"]["purpose"],
        "engineering_status": "fail",
        "diagnostic_codes": [item["code"] for item in terminal["diagnostics"]],
        "native_artifact": {
            "path": terminal_artifact["path"], "bytes": terminal_artifact["bytes"],
            "sha256": terminal_artifact["sha256"],
        },
    }, "agent_evidence.terminal_nonconvergence")
    _expect(agent["limitations"], [
        "This is preview evidence, not foundry signoff.",
        "The IHP mos_tt model closure and PSP103 OSDI module are individually content-bound and retained for independent reconstruction.",
        "The shared CLI result reports native-default startup as unenumerated; the isolated startup and OSDI bytes are independently verified by this chain.",
        "Mean and RMS are arithmetic statistics over retained adaptive samples, not time-weighted electrical measurements.",
        "The normalized measurement profile intentionally labels native-artifact lineage unverified downstream; this verifier independently checks that edge.",
    ], "agent_evidence.limitations")


TAMPER_PROBE_IDS = (
    "generated-netlist-byte",
    "native-raw-byte",
    "terminal-partial-raw-byte",
    "terminal-native-log-byte",
    "builtin-native-raw-byte",
    "builtin-terminal-partial-raw-byte",
    "builtin-terminal-native-log-byte",
    "normalized-series-digest",
    "measurement-sample-at-value",
    "measurement-minimum-value",
    "measurement-maximum-value",
    "measurement-mean-value",
    "measurement-rms-value",
    "measurement-crossing-value",
    "measurement-rise-time-value",
    "measurement-fall-time-value",
    "measurement-settling-time-value",
    "specification-margin",
    "specification-condition-binding",
)


def _verification_report(
    manifest_sha256: str,
    chain_id: str,
    tamper_replays: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "openada.independent-chain-verification/v0alpha1",
        "chain_id": chain_id,
        "chain_manifest_sha256": manifest_sha256,
        "status": "pass",
        "independent_oracle": "conformance/ihp-inverter-agent-chain/verify.py",
        "imports_openada": False,
        "checks": [
            "native-artifact-reparsed",
            "lineage-recomputed",
            "nine-measurements-recomputed",
            "eighteen-specification-decisions-recomputed",
            "negative-replays-verified",
            "tamper-replays-rejected",
            "agent-evidence-cross-checked",
        ],
        "tamper_probes": list(TAMPER_PROBE_IDS),
        "tamper_replays": tamper_replays or [],
    }


def _run_contract_tests(chain_id: str) -> dict[str, Any]:
    suite = HERE / "test_agent_chain.py"
    environment = os.environ.copy()
    environment.pop("OPENADA_RUN_IHP_AGENT_CHAIN", None)
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            str(suite.relative_to(REPOSITORY_ROOT)),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ConformanceError(
            "agent-chain contract tests failed: " + completed.stdout[-4_000:]
        )
    passed = re.findall(r"(?:^|\s)([0-9]+) passed", completed.stdout)
    skipped = re.findall(r"(?:^|\s)([0-9]+) skipped", completed.stdout)
    if len(passed) != 1 or len(skipped) > 1:
        raise ConformanceError("cannot parse the focused agent-chain test summary")
    return {
        "schema": "openada.contract-test-report/ihp-inverter-agent-chain/v1",
        "chain_id": chain_id,
        "status": "pass",
        "suite": {
            "repository_path": suite.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": sha256_file(suite),
            "passed": int(passed[0]),
            "skipped": int(skipped[0]) if skipped else 0,
            "failed": 0,
        },
        "extensions": {},
    }


def _semantic_subject() -> str:
    return semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )


def _verify_design_provenance(manifest: dict[str, Any], evidence: Path) -> None:
    provenance = _read_json(
        evidence / "design-provenance.json", label="design provenance"
    )
    _validate(
        provenance,
        _load_validator(DESIGN_PROVENANCE_SCHEMA_PATH, label="design provenance"),
        label="design provenance",
    )
    design = manifest["design"]
    for field in ("repository", "revision", "tree"):
        _expect(provenance[field], design[field], f"design_provenance.{field}")
    _expect(
        {key: provenance["license"][key] for key in ("path", "sha256")},
        {key: design["license"][key] for key in ("path", "sha256")},
        "design_provenance.license",
    )
    _expect(
        [{key: item[key] for key in ("path", "sha256")} for item in provenance["inputs"]],
        design["inputs"],
        "design_provenance.inputs",
    )


def _chain_artifact_path(evidence: Path, repository_path: str) -> Path:
    prefix = "conformance/ihp-inverter-agent-chain/evidence/"
    if repository_path.startswith(prefix):
        return evidence / repository_path.removeprefix(prefix)
    return REPOSITORY_ROOT / repository_path


def _verify_chain_run(
    manifest: dict[str, Any], evidence: Path, run: dict[str, Any], manifest_sha256: str
) -> None:
    _validate(run, _load_validator(CHAIN_RUN_SCHEMA_PATH, label="semantic chain run"), label="semantic chain run")
    _expect(run["chain_id"], manifest["id"], "chain_run.chain_id")
    _expect(run["chain_manifest_sha256"], manifest_sha256, "chain_run.chain_manifest_sha256")
    _expect(run["semantic_subject_sha256"], _semantic_subject(), "chain_run.semantic_subject_sha256")
    _expect(run["source_attestation"]["receipt_class"], "release", "chain_run.source_attestation.receipt_class")
    _expect(run["source_attestation"]["semantic_subject_sha256"], run["semantic_subject_sha256"], "chain_run.source_attestation.semantic_subject_sha256")
    _expect(run["source_attestation"]["clean_before"], True, "chain_run.source_attestation.clean_before")
    _expect(run["source_attestation"]["clean_after"], True, "chain_run.source_attestation.clean_after")
    _expect(run["source_attestation"]["state_unchanged"], True, "chain_run.source_attestation.state_unchanged")
    _expect(run["status"], "pass", "chain_run.status")
    if not all(run["checks"].values()):
        raise ConformanceError("chain run contains a false conformance check")

    negative_paths = {
        "netlist-missing-symbol": "negative/netlist-missing-symbol.json",
        "isolated-terminal-nonconvergence": "provider-fail-result.json",
        "isolated-builtin-terminal-nonconvergence": "builtin-fail-result.json",
        "extract-missing-selector": "negative/extract-missing-selector.json",
        **{
            f"measure-{identifier}": f"negative/measure-{identifier}.json"
            for identifier in (
                "sample-at", "minimum", "maximum", "mean", "rms", "crossing",
                "rise-time", "fall-time", "settling-time",
            )
        },
        "deliberately-violated-limits": "specifications/sample-at-fail.json",
        "specification-condition-mismatch": "negative/spec-sample-at-condition-mismatch.json",
    }
    declared_negative = [item["id"] for item in manifest["negative_replays"]]
    declared_tamper = [item["id"] for item in manifest["tamper_replays"]]
    _expect(list(negative_paths), declared_negative, "chain_run.negative_replay_order")
    _expect(declared_tamper, list(TAMPER_PROBE_IDS), "chain_run.tamper_replay_order")

    expected_origins: list[tuple[str, str, str | None, str | None, str | None]] = [
        (
            "conformance/ihp-inverter-agent-chain/evidence/contract-tests.json",
            "contract-test",
            "contract-tests",
            "contract-test-verdict",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/design-provenance.json",
            "design-provenance",
            "materialize-pinned-sources",
            "design-provenance",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/provider/work/test_inverter.raw",
            "native-artifact",
            "provider-invoke",
            "native-raw",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/independent-verification.json",
            "independent-oracle",
            "independent-verifier",
            "independent-chain-verdict",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/extract.json",
            "normalized-evidence",
            "extract",
            "normalized-vin-vout-series",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/specifications/sample-at-pass.json",
            "downstream-decision",
            "evaluate-pass",
            "nine-passing-specification-decisions",
            None,
        ),
        (
            "conformance/ihp-inverter-agent-chain/evidence/agent-evidence.json",
            "agent-visible-evidence",
            "agent-decision",
            "agent-evidence",
            None,
        ),
    ]
    expected_origins.extend(
        (
            f"conformance/ihp-inverter-agent-chain/evidence/{relative}",
            "negative-replay",
            None,
            None,
            replay_id,
        )
        for replay_id, relative in negative_paths.items()
    )
    expected_origins.extend(
        (
            f"conformance/ihp-inverter-agent-chain/evidence/tamper/{replay_id}.json",
            "tamper-replay",
            None,
            None,
            replay_id,
        )
        for replay_id in declared_tamper
    )
    actual_origins = [
        (
            artifact["repository_path"],
            artifact["role"],
            artifact["source_step"],
            artifact["source_output"],
            artifact["replay_id"],
        )
        for artifact in run["artifacts"]
    ]
    _expect(actual_origins, expected_origins, "chain_run.artifact_origins")

    repository_paths = [artifact["repository_path"] for artifact in run["artifacts"]]
    _expect(
        len(repository_paths),
        len(set(repository_paths)),
        "chain_run.unique_repository_paths",
    )
    trust_digests = [artifact["sha256"] for artifact in run["artifacts"]]
    _expect(
        len(trust_digests),
        len(set(trust_digests)),
        "chain_run.unique_trust_artifact_sha256",
    )

    steps = {step["id"]: step for step in manifest["steps"]}
    origin_requirements = {
        "contract-test": ("independent-oracle", False),
        "native-artifact": ("semantic-command", True),
        "independent-oracle": ("independent-oracle", False),
        "normalized-evidence": ("semantic-command", False),
        "downstream-decision": ("semantic-command", False),
        "agent-visible-evidence": ("independent-decision", False),
    }
    for index, artifact in enumerate(run["artifacts"]):
        path = _chain_artifact_path(evidence, artifact["repository_path"])
        size = _require_regular_file(path, label=f"chain artifact {index}", maximum_bytes=MAX_ARTIFACT_BYTES)
        _expect(artifact["bytes"], size, f"chain_run.artifacts[{index}].bytes")
        _expect(artifact["sha256"], sha256_file(path), f"chain_run.artifacts[{index}].sha256")
        role = artifact["role"]
        if role in {"negative-replay", "tamper-replay"}:
            continue
        step = steps.get(artifact["source_step"])
        if step is None:
            raise ConformanceError(
                f"chain_run.artifacts[{index}] references an unknown source step"
            )
        _expect(
            artifact["source_output"] in step["produces"],
            True,
            f"chain_run.artifacts[{index}].source_output",
        )
        required_kind, required_native = origin_requirements[role]
        _expect(step["kind"], required_kind, f"chain_run.artifacts[{index}].source_step.kind")
        _expect(
            step["native_execution"],
            required_native,
            f"chain_run.artifacts[{index}].source_step.native_execution",
        )
        if role == "agent-visible-evidence":
            _expect(
                artifact["source_step"],
                manifest["agent_evidence"]["result_step"],
                "chain_run.agent_visible_evidence.result_step",
            )
    _expect(run["extensions"]["org.openada"]["tamper_probe_count"], len(TAMPER_PROBE_IDS), "chain_run.tamper_probe_count")


def _mutate_json(path: Path, mutation: Callable[[dict[str, Any]], None]) -> None:
    document = _read_json(path, label="tamper candidate")
    mutation(document)
    path.write_text(json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_tamper_probes(
    manifest: dict[str, Any], evidence: Path, manifest_sha256: str
) -> list[dict[str, Any]]:
    mutations: list[tuple[str, str, Callable[[Path], None]]] = [
        (
            "generated-netlist-byte",
            "work/inverter_tb.spice",
            lambda root: (root / "work/inverter_tb.spice").write_bytes(
                (root / "work/inverter_tb.spice").read_bytes() + b"\n*TAMPER\n"
            ),
        ),
        (
            "native-raw-byte",
            "provider/work/test_inverter.raw",
            lambda root: (root / "provider/work/test_inverter.raw").write_bytes(
                (root / "provider/work/test_inverter.raw").read_bytes()[:-1]
                + bytes([(root / "provider/work/test_inverter.raw").read_bytes()[-1] ^ 1])
            ),
        ),
        (
            "terminal-partial-raw-byte",
            "provider-fail/work/test_inverter_fail.raw",
            lambda root: (root / "provider-fail/work/test_inverter_fail.raw").write_bytes(
                (root / "provider-fail/work/test_inverter_fail.raw").read_bytes()[:-1]
                + bytes([(root / "provider-fail/work/test_inverter_fail.raw").read_bytes()[-1] ^ 1])
            ),
        ),
        (
            "terminal-native-log-byte",
            "provider-fail/simulation/inverter_terminal_nonconvergence.log",
            lambda root: (root / "provider-fail/simulation/inverter_terminal_nonconvergence.log").write_bytes(
                (root / "provider-fail/simulation/inverter_terminal_nonconvergence.log").read_bytes() + b"\nTAMPER\n"
            ),
        ),
        (
            "builtin-native-raw-byte",
            "builtin/simulation/inverter_shared.raw",
            lambda root: (root / "builtin/simulation/inverter_shared.raw").write_bytes(
                (root / "builtin/simulation/inverter_shared.raw").read_bytes()[:-1]
                + bytes([(root / "builtin/simulation/inverter_shared.raw").read_bytes()[-1] ^ 1])
            ),
        ),
        (
            "builtin-terminal-partial-raw-byte",
            "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.raw",
            lambda root: (root / "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.raw").write_bytes(
                (root / "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.raw").read_bytes()[:-1]
                + bytes([(root / "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.raw").read_bytes()[-1] ^ 1])
            ),
        ),
        (
            "builtin-terminal-native-log-byte",
            "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.log",
            lambda root: (root / "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.log").write_bytes(
                (root / "builtin-fail/simulation/inverter_shared_terminal_nonconvergence.log").read_bytes()
                + b"\nTAMPER\n"
            ),
        ),
        (
            "normalized-series-digest",
            "extract.json",
            lambda root: _mutate_json(
                root / "extract.json",
                lambda document: document["data"]["extraction"]["series"]["source"].__setitem__("artifact_sha256", "0" * 64),
            ),
        ),
    ]
    for identifier in (
        "sample-at", "minimum", "maximum", "mean", "rms", "crossing",
        "rise-time", "fall-time", "settling-time",
    ):
        relative = f"measurements/{identifier}.json"
        mutations.append(
            (
                f"measurement-{identifier}-value",
                relative,
                lambda root, relative=relative: _mutate_json(
                    root / relative,
                    lambda document: document["data"]["measurement"].__setitem__(
                        "value", document["data"]["measurement"]["value"] + 0.1
                    ),
                ),
            )
        )
    mutations.extend(
        [
            (
                "specification-margin",
                "specifications/maximum-pass.json",
                lambda root: _mutate_json(
                    root / "specifications/maximum-pass.json",
                    lambda document: document["data"]["evaluation"]["margin"].__setitem__(
                        "value", document["data"]["evaluation"]["margin"]["value"] + 0.1
                    ),
                ),
            ),
            (
                "specification-condition-binding",
                "negative/spec-sample-at-condition-mismatch.json",
                lambda root: _mutate_json(
                    root / "negative/spec-sample-at-condition-mismatch.json",
                    lambda document: document["data"]["evaluation"]["conditions"].__setitem__(
                        "matched_count", 2
                    ),
                ),
            ),
        ]
    )
    _expect(
        [identifier for identifier, _relative, _mutation in mutations],
        list(TAMPER_PROBE_IDS),
        "tamper_probe.order",
    )
    declared = {item["id"]: item for item in manifest["tamper_replays"]}
    receipts: list[dict[str, Any]] = []
    for identifier, relative, mutation in mutations:
        baseline = evidence / relative
        baseline_sha256 = sha256_file(baseline)
        with tempfile.TemporaryDirectory(prefix=f"openada-tamper-{identifier}-") as temporary:
            candidate = Path(temporary) / "evidence"
            shutil.copytree(evidence, candidate)
            for final_receipt in (
                "chain-run.json",
                "independent-verification.json",
                "contract-tests.json",
            ):
                receipt_path = candidate / final_receipt
                if receipt_path.exists():
                    receipt_path.unlink()
            retained_tamper_dir = candidate / "tamper"
            if retained_tamper_dir.exists():
                shutil.rmtree(retained_tamper_dir)
            mutation(candidate)
            mutated_sha256 = sha256_file(candidate / relative)
            if mutated_sha256 == baseline_sha256:
                raise ConformanceError(f"tamper probe {identifier!r} did not change its target")
            try:
                verify_evidence(
                    manifest,
                    candidate,
                    manifest_sha256=manifest_sha256,
                    require_chain_run=False,
                    run_tamper_probes=False,
                )
            except ConformanceError as exc:
                replay = declared[identifier]
                rejection_message = str(exc).replace(
                    str(candidate), "/tampered-evidence"
                )[:2_000]
                receipts.append(
                    {
                        "schema": "openada.tamper-replay/v0alpha1",
                        "replay_id": identifier,
                        "status": replay["expected_status"],
                        "diagnostic": replay["required_diagnostic"],
                        "mutation": {
                            "target": relative,
                            "baseline_sha256": baseline_sha256,
                            "mutated_sha256": mutated_sha256,
                        },
                        "independent_rejection": {
                            "exception": "ConformanceError",
                            "message": rejection_message,
                        },
                    }
                )
                continue
            raise ConformanceError(f"tamper probe {identifier!r} was not rejected")
    return receipts


def verify_evidence(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
    require_chain_run: bool,
    run_tamper_probes: bool,
) -> dict[str, Any]:
    """Verify one fresh chain replay without importing OpenADA implementation code."""

    evidence = evidence.resolve()
    _expect(manifest_sha256, sha256_file(MANIFEST_PATH), "manifest_sha256")
    _verify_manifest(manifest, manifest_sha256)
    _verify_evidence_tree(evidence, manifest, require_chain_run)
    _verify_design_provenance(manifest, evidence)
    result_validator = _load_validator(RESULT_SCHEMA_PATH, label="result")
    profile = _read_json(REPOSITORY_ROOT / "profiles/circuit.simulate-v1alpha2.json", label="circuit profile")
    data_schema = profile["normalized_result"]["data_schema"]
    Draft202012Validator.check_schema(data_schema)
    data_validator = Draft202012Validator(data_schema, format_checker=FormatChecker())
    provider_manifest_sha256 = sha256_file(
        REPOSITORY_ROOT / "providers/ngspice-pdk-control/driver-manifest.json"
    )
    run = _read_json(evidence / "run.json", label="run metadata")
    _verify_run(manifest, run, manifest_sha256, provider_manifest_sha256)

    netlist = _read_json(evidence / "netlist.json", label="netlist result")
    deck_sha256 = _verify_netlist_result(manifest, evidence, netlist, result_validator)
    negative_netlist = _read_json(
        evidence / "negative/netlist-missing-symbol.json",
        label="negative netlist result",
    )
    _verify_negative_netlist(manifest, evidence, negative_netlist, result_validator)
    source_text = (evidence / "work/inverter_tb.spice").read_text(encoding="utf-8")
    shared_deck_sha256, shared_terminal_deck_sha256 = _verify_shared_decks(
        manifest, evidence, source_text
    )
    config_sha256 = _verify_provider_configuration(manifest, evidence)
    provider_request = _read_json(evidence / "provider-request.json", label="provider request")
    provider = manifest["extensions"]["org.openada"]["provider"]
    _verify_provider_request(
        manifest, provider_request,
        deck_path="/evidence/work/inverter_tb.spice", deck_sha256=deck_sha256,
        config_sha256=config_sha256, request_id=PROVIDER_REQUEST_ID,
        analysis=provider["analysis"], destination="/evidence/provider",
        location="provider_request",
    )
    terminal = manifest["extensions"]["org.openada"]["terminal_nonconvergence"]
    terminal_deck_sha256 = _verify_terminal_deck(manifest, evidence, source_text)
    terminal_request = _read_json(evidence / "provider-fail-request.json", label="terminal provider request")
    _verify_provider_request(
        manifest, terminal_request,
        deck_path=terminal["generated_deck"], deck_sha256=terminal_deck_sha256,
        config_sha256=config_sha256, request_id=terminal["request_id"],
        analysis=terminal["analysis"], destination=terminal["evidence_destination"],
        location="terminal_provider_request",
    )
    provider_result = _read_json(evidence / "provider-result.json", label="provider result")
    raw_path, parsed = _verify_provider_result(
        manifest, evidence, provider_result, result_validator, data_validator,
        expected_status="pass", request=provider_request, deck_sha256=deck_sha256,
        destination="/evidence/provider", stem="inverter_tb", raw_name="test_inverter.raw",
    )
    assert parsed is not None
    terminal_result = _read_json(evidence / "provider-fail-result.json", label="terminal provider result")
    _verify_provider_result(
        manifest, evidence, terminal_result, result_validator, data_validator,
        expected_status="fail", request=terminal_request, deck_sha256=terminal_deck_sha256,
        destination="/evidence/provider-fail", stem="inverter_terminal_nonconvergence",
        raw_name="test_inverter_fail.raw",
    )
    builtin_result = _read_json(evidence / "builtin-result.json", label="built-in result")
    _builtin_raw_path, builtin_parsed = _verify_builtin_result(
        manifest,
        evidence,
        builtin_result,
        result_validator,
        data_validator,
        expected_status="pass",
        deck_sha256=shared_deck_sha256,
    )
    builtin_terminal_result = _read_json(
        evidence / "builtin-fail-result.json", label="built-in terminal result"
    )
    _verify_builtin_result(
        manifest,
        evidence,
        builtin_terminal_result,
        result_validator,
        data_validator,
        expected_status="fail",
        deck_sha256=shared_terminal_deck_sha256,
    )
    for signal in ("time", "v(vdd)", "v(vin)", "v(vout)"):
        _expect(
            builtin_parsed["columns"][signal],
            parsed["columns"][signal],
            f"builtin_provider_waveform_equivalence[{signal}]",
        )
    extraction = _read_json(evidence / "extract.json", label="extraction result")
    series, measurement_source = _verify_extraction(
        manifest, evidence, extraction, parsed, raw_path, result_validator
    )
    negative_extraction = _read_json(evidence / "negative/extract-missing-selector.json", label="negative extraction")
    _verify_negative_extraction(manifest, evidence, raw_path, negative_extraction, result_validator)

    measurement_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    evaluations: list[dict[str, Any]] = []
    for index, definition in enumerate(manifest["extensions"]["org.openada"]["measurements"], start=1):
        identifier = definition["id"]
        request = _read_json(evidence / f"requests/measure-{identifier}.json", label=f"{identifier} measurement request")
        _expect(request, definition["request"], f"measurement_request[{identifier}]")
        expected, status, diagnostic = _expected_measurement(request, series, measurement_source)
        _expect(status, "pass", f"measurement_oracle[{identifier}].status")
        result = _read_json(evidence / f"measurements/{identifier}.json", label=f"{identifier} measurement")
        _verify_measurement_result(
            result, request, expected, request_id=definition["request_id"],
            engineering="pass", diagnostic_code=diagnostic, validator=result_validator,
        )
        measurement_pairs.append((definition, result))

        negative_request = _read_json(evidence / f"requests/measure-{identifier}-negative.json", label=f"negative {identifier} request")
        _expect(negative_request, _negative_measurement_request(definition), f"negative_measurement_request[{identifier}]")
        negative_expected, negative_status, negative_diagnostic = _expected_measurement(
            negative_request, series, measurement_source, allow_domain_failure=True
        )
        negative_result = _read_json(evidence / f"negative/measure-{identifier}.json", label=f"negative {identifier} result")
        _verify_measurement_result(
            negative_result, negative_request, negative_expected,
            request_id=f"16000000-0000-4000-8000-{index:012d}",
            engineering=negative_status, diagnostic_code=negative_diagnostic,
            validator=result_validator,
        )
        measurement_record = result["data"]["measurement"]
        for decision, prefix in (("pass", "12"), ("fail", "13")):
            specification = _read_json(evidence / f"requests/spec-{identifier}-{decision}.json", label=f"{identifier} {decision} specification")
            expected_specification = {
                "specification_id": f"{request['measurement_id']}.{decision}-limit",
                "measurement_id": request["measurement_id"],
                "limits": definition[f"{decision}_limits"],
                "conditions": manifest["extensions"]["org.openada"]["workflow"]["conditions"],
                "extensions": {},
            }
            _expect(specification, expected_specification, f"specification_request[{identifier}:{decision}]")
            evaluation_result = _read_json(evidence / f"specifications/{identifier}-{decision}.json", label=f"{identifier} {decision} evaluation")
            _verify_specification_result(
                evaluation_result, measurement_record, specification,
                request_id=f"{prefix}000000-0000-4000-8000-{index:012d}",
                expected_status=decision, validator=result_validator,
            )
            evaluations.append(evaluation_result)
        if index == 1:
            mismatch_conditions = deepcopy(
                manifest["extensions"]["org.openada"]["workflow"]["conditions"]
            )
            mismatch_conditions[1]["value"] = 1.1
            expected_mismatch_request = {
                "specification_id": f"{request['measurement_id']}.condition-mismatch-limit",
                "measurement_id": request["measurement_id"],
                "limits": definition["pass_limits"],
                "conditions": mismatch_conditions,
                "extensions": {},
            }
            mismatch_request = _read_json(
                evidence / "requests/spec-sample-at-condition-mismatch.json",
                label="condition mismatch specification request",
            )
            _expect(
                mismatch_request,
                expected_mismatch_request,
                "condition_mismatch.request",
            )
            mismatch_result = _read_json(
                evidence / "negative/spec-sample-at-condition-mismatch.json",
                label="condition mismatch specification result",
            )
            _verify_condition_mismatch(
                mismatch_result, measurement_record, mismatch_request, result_validator
            )

    agent = _read_json(evidence / "agent-evidence.json", label="agent evidence")
    _verify_agent_evidence(
        manifest,
        agent,
        negative_netlist,
        provider_result,
        terminal_result,
        builtin_result,
        builtin_terminal_result,
        extraction,
        measurement_pairs,
        evaluations,
    )
    retained_tamper: list[dict[str, Any]] | None = None
    if require_chain_run:
        report = _read_json(evidence / "independent-verification.json", label="independent verification report")
        contract_report = _read_json(
            evidence / "contract-tests.json", label="contract-test report"
        )
        _expect(
            contract_report,
            _run_contract_tests(manifest["id"]),
            "contract_tests",
        )
        retained_tamper = [
            _read_json(evidence / f"tamper/{identifier}.json", label=f"tamper replay {identifier}")
            for identifier in TAMPER_PROBE_IDS
        ]
        _expect(
            report,
            _verification_report(manifest_sha256, manifest["id"], retained_tamper),
            "independent_verification",
        )
        chain_run = _read_json(evidence / "chain-run.json", label="semantic chain run")
        _verify_chain_run(manifest, evidence, chain_run, manifest_sha256)
    tamper_replays: list[dict[str, Any]] = []
    if run_tamper_probes:
        tamper_replays = _run_tamper_probes(manifest, evidence, manifest_sha256)
        if retained_tamper is not None:
            _expect(tamper_replays, retained_tamper, "retained_tamper_replays")
    return _verification_report(manifest_sha256, manifest["id"], tamper_replays)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independently verify retained IHP agent-chain evidence.")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--without-chain-run", action="store_true")
    parser.add_argument("--run-tamper-probes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.resolve()
        manifest = load_manifest(manifest_path)
        report = verify_evidence(
            manifest,
            args.evidence,
            manifest_sha256=sha256_file(manifest_path),
            require_chain_run=not args.without_chain_run,
            run_tamper_probes=args.run_tamper_probes,
        )
    except ConformanceError as exc:
        print(f"independent verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
