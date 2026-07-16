#!/usr/bin/env python3
"""Independent native-artifact oracle; intentionally imports no OpenADA code."""

from __future__ import annotations

import cmath
import hashlib
import json
import math
from pathlib import Path
import stat
import struct
from typing import Any


class OracleError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= 32 * 1024 * 1024
    ):
        raise OracleError(f"{path} is not a bounded regular single-link JSON file")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_closed_object,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant {token!r}")
        ),
    )
    if not isinstance(value, dict):
        raise OracleError(f"{path} must contain one JSON object")
    return value


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_inventory(root: Path) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise OracleError(f"native replay contains a symbolic link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OracleError(f"native replay contains an unsafe file: {relative}")
        if not 1 <= metadata.st_size <= 512 * 1024 * 1024:
            raise OracleError(f"native replay file is outside the size bound: {relative}")
        records.append(
            {
                "path": relative,
                "bytes": metadata.st_size,
                "sha256": sha256(path),
            }
        )
    if not records:
        raise OracleError("native replay contains no retained files")
    return records, _canonical_sha256(records)


def raw_plot(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= 512 * 1024 * 1024
    ):
        raise OracleError(f"{path} is not a bounded regular single-link raw file")
    body = path.read_bytes()
    marker = b"Binary:\n"
    offset = body.find(marker)
    if offset < 0 or body.find(marker, offset + 1) >= 0:
        raise OracleError(f"{path} does not contain one binary plot")
    header = body[:offset].decode("ascii", errors="strict")
    fields: dict[str, str] = {}
    lines = header.splitlines()
    variables_at = None
    for index, line in enumerate(lines):
        if line.strip().casefold() == "variables:":
            variables_at = index + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip()
    if variables_at is None:
        raise OracleError(f"{path} has no variable table")
    try:
        count = int(fields["no. variables"])
        points = int(fields["no. points"])
        plotname = fields["plotname"]
        flags = set(fields["flags"].casefold().split())
    except (KeyError, ValueError) as exc:
        raise OracleError(f"{path} has an invalid closed header") from exc
    variables: list[str] = []
    for line in lines[variables_at : variables_at + count]:
        tokens = line.split()
        if len(tokens) < 3 or int(tokens[0]) != len(variables):
            raise OracleError(f"{path} has a malformed variable row")
        variables.append(tokens[1].casefold())
    complex_values = "complex" in flags
    width = 2 if complex_values else 1
    payload = body[offset + len(marker) :]
    scalar_count = points * count * width
    if len(payload) != scalar_count * 8:
        raise OracleError(
            f"{path} binary size {len(payload)} does not match {scalar_count} doubles"
        )
    scalars = struct.unpack(f"={scalar_count}d", payload)
    if not all(math.isfinite(value) for value in scalars):
        raise OracleError(f"{path} contains a non-finite scalar")
    columns: dict[str, list[complex | float]] = {name: [] for name in variables}
    position = 0
    for _ in range(points):
        for name in variables:
            if complex_values:
                columns[name].append(complex(scalars[position], scalars[position + 1]))
                position += 2
            else:
                columns[name].append(scalars[position])
                position += 1
    return {
        "path": str(path),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "plotname": plotname,
        "points": points,
        "variables": variables,
        "complex": complex_values,
        "columns": columns,
    }


def _real(values: list[complex | float]) -> list[float]:
    output = []
    for value in values:
        if isinstance(value, complex):
            if value.imag != 0:
                raise OracleError("expected real native data")
            output.append(value.real)
        else:
            output.append(value)
    return output


def _column(plot: dict[str, Any], *names: str) -> list[complex | float]:
    for name in names:
        if name.casefold() in plot["columns"]:
            return plot["columns"][name.casefold()]
    raise OracleError(f"none of {names!r} exists in {plot['variables']!r}")


def _artifact(result: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in result.get("artifacts", []) if item.get("role") == role]
    if len(matches) != 1:
        raise OracleError(f"result has {len(matches)} {role!r} artifacts")
    return matches[0]


def _bind_result(root: Path, analysis: str, plot: dict[str, Any]) -> dict[str, Any]:
    result = load_json(root / f"results/{analysis}.json")
    request = load_json(root / f"requests/{analysis}.json")
    if (
        result.get("engineering", {}).get("status") != "pass"
        or result.get("data", {}).get("analysis", {}).get("type") != analysis
        or result.get("data", {}).get("evidence", {}).get("request_binding") != "exact"
        or result.get("data", {}).get("evidence", {}).get("structure") != "valid"
        or result.get("data", {}).get("protocol", {}).get("driver_version") != "0.5.0"
    ):
        raise OracleError(f"{analysis} result is not exact provider pass evidence")
    feature = f"openada.feature/simulation.analysis.{analysis}/v1alpha1"
    if request["driver_selector"]["required_features"] != [feature]:
        raise OracleError(f"{analysis} request feature is not exact")
    deck_path = root / f"decks/{analysis}.spice"
    if request.get("target", {}).get("locator", {}).get("sha256") != sha256(deck_path):
        raise OracleError(f"{analysis} request does not bind deck bytes")
    raw_record = _artifact(result, "simulation.result")
    log_record = _artifact(result, "simulation.log")
    launcher_record = _artifact(result, "simulation.launcher")
    if raw_record["sha256"] != plot["sha256"] or raw_record["bytes"] != plot["bytes"]:
        raise OracleError(f"{analysis} result does not bind raw bytes")
    log_path = root / f"native/{analysis}/simulation/{analysis}.log"
    if log_record["sha256"] != sha256(log_path):
        raise OracleError(f"{analysis} result does not bind log bytes")
    launcher_path = root / f"native/{analysis}/simulation/{analysis}.openada-control.sp"
    if launcher_record["sha256"] != sha256(launcher_path):
        raise OracleError(f"{analysis} result does not bind launcher bytes")
    counts = result["data"]["analysis"]
    if counts["point_count"] != plot["points"]:
        raise OracleError(f"{analysis} normalized point count differs from raw")
    return {
        "request_id": result["data"]["protocol"]["request_id"],
        "raw_sha256": plot["sha256"],
        "log_sha256": sha256(log_path),
        "deck_sha256": sha256(deck_path),
        "launcher_sha256": sha256(launcher_path),
        "point_count": plot["points"],
        "dependent_variable_count": counts["dependent_variable_count"],
        "finite_value_count": counts["finite_value_count"],
        "plotname": plot["plotname"],
    }


def _ac_metrics(plot: dict[str, Any]) -> dict[str, Any]:
    frequencies = [value.real for value in _column(plot, "frequency")]
    output = [complex(value) for value in _column(plot, "v(vout)")]
    input_values = [complex(value) for value in _column(plot, "v(vp)")]
    transfer = [out / inp for out, inp in zip(output, input_values, strict=True)]
    magnitudes = [abs(value) for value in transfer]
    phases = [math.degrees(cmath.phase(value)) for value in transfer]
    crossing = None
    phase_margin = None
    for index in range(1, len(magnitudes)):
        if magnitudes[index - 1] >= 1.0 > magnitudes[index]:
            fraction = (0.0 - math.log(magnitudes[index - 1])) / (
                math.log(magnitudes[index]) - math.log(magnitudes[index - 1])
            )
            crossing = math.exp(
                math.log(frequencies[index - 1])
                + fraction
                * (math.log(frequencies[index]) - math.log(frequencies[index - 1]))
            )
            phase_at_crossing = phases[index - 1] + fraction * (
                phases[index] - phases[index - 1]
            )
            phase_margin = 180.0 + phase_at_crossing
            break
    return {
        "frequency_first_hz": frequencies[0],
        "frequency_last_hz": frequencies[-1],
        "low_frequency_gain_db": 20.0 * math.log10(magnitudes[0]),
        "unity_gain_frequency_hz": crossing,
        "phase_margin_deg": phase_margin,
        "unity_crossing_in_band": crossing is not None,
    }


def verify(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = root.resolve()
    native_files, native_tree_sha256 = _native_inventory(root)
    plots = {
        name: raw_plot(root / f"native/{name}/work/{name}.raw")
        for name in ("op", "dc", "ac", "tran")
    }
    expected_plots = {
        "op": "Operating Point",
        "dc": "DC transfer characteristic",
        "ac": "AC Analysis",
        "tran": "Transient Analysis (linearized)",
    }
    for name, expected in expected_plots.items():
        if plots[name]["plotname"] != expected:
            raise OracleError(f"{name} plot is {plots[name]['plotname']!r}, expected {expected!r}")
    bindings = {name: _bind_result(root, name, plots[name]) for name in plots}

    op_vout = float(_real(_column(plots["op"], "v(vout)"))[0])
    dc_axis = _real(_column(plots["dc"], "v(v-sweep)"))
    dc_vout = _real(_column(plots["dc"], "v(vout)"))
    if not all(right > left for left, right in zip(dc_axis, dc_axis[1:])):
        raise OracleError("DC axis is not strictly increasing")
    threshold_index = min(range(len(dc_vout)), key=lambda index: abs(dc_vout[index] - 0.6))
    tran_time = _real(_column(plots["tran"], "time"))
    tran_in = _real(_column(plots["tran"], "v(vin)"))
    tran_out = _real(_column(plots["tran"], "v(vout)"))
    step = tran_time[1] - tran_time[0]
    if not all(math.isclose(b - a, step, rel_tol=1e-8, abs_tol=1e-15) for a, b in zip(tran_time, tran_time[1:])):
        raise OracleError("linearized transient axis is not uniform")
    ac = _ac_metrics(plots["ac"])

    negatives = {}
    negative_files = {
        "op-unsafe-command": "op-unsafe-result.json",
        "dc-parameter-mismatch": "dc-mismatch-result.json",
        "ac-feature-mismatch": "ac-feature-result.json",
        "tran-duplicate-write": "tran-duplicate-result.json",
        "tran-native-error": "tran-native-error-result.json",
    }
    for replay_id, filename in negative_files.items():
        value = load_json(root / "negative" / filename)
        codes = sorted(item.get("code") for item in value.get("diagnostics", []))
        if value.get("engineering", {}).get("status") != "unknown" or not codes:
            raise OracleError(f"negative {replay_id} is not typed unknown evidence")
        negatives[replay_id] = {
            "engineering_status": "unknown",
            "execution_status": value.get("execution", {}).get("status"),
            "diagnostic_codes": codes,
            "native_launched": bool(value.get("execution", {}).get("command")),
        }
    if negatives["tran-native-error"]["native_launched"] is not True:
        raise OracleError("native-error replay did not launch ngspice")
    if any(
        negatives[name]["native_launched"]
        for name in negative_files
        if name != "tran-native-error"
    ):
        raise OracleError("request-boundary negative unexpectedly launched native EDA")

    oracle = {
        "schema": "openada.independent-oracle/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "bindings": bindings,
        "native_files": native_files,
        "native_tree_sha256": native_tree_sha256,
        "negative_replays": negatives,
        "extensions": {},
    }
    normalized = {
        "schema": "openada.normalized-evidence/ihp-ngspice-provider-analyses/v1",
        "provider": {
            "driver_id": "org.openada.driver.ngspice-pdk-control",
            "driver_version": "0.5.0",
            "native_tool": "ngspice-46",
            "pdk_revision": "144f811cdffda49b71d28f64e8a92b697b61cf06",
        },
        "analyses": {
            "op": {**bindings["op"], "vout_v": op_vout},
            "dc": {
                **bindings["dc"],
                "axis_first_v": dc_axis[0],
                "axis_last_v": dc_axis[-1],
                "inverter_half_supply_crossing_input_v": dc_axis[threshold_index],
                "inverter_half_supply_crossing_output_v": dc_vout[threshold_index],
            },
            "ac": {**bindings["ac"], **ac},
            "tran": {
                **bindings["tran"],
                "time_first_s": tran_time[0],
                "time_last_s": tran_time[-1],
                "uniform_step_s": step,
                "vin_min_v": min(tran_in),
                "vin_max_v": max(tran_in),
                "vout_min_v": min(tran_out),
                "vout_max_v": max(tran_out),
            },
        },
        "standards_assessment": {
            "status": "no-applicable-ieee-conformance-claim",
            "reason": (
                "This chain validates nominal analog OP/DC/AC/TRAN provider evidence. "
                "It does not measure an ADC, DAC, waveform-recorder spectral figure, "
                "or a transition-time parameter."
            ),
            "reviewed_not_applied": [
                "IEEE 1241-2023",
                "IEEE 1658-2023",
                "IEEE 1057-2017",
                "IEEE 181-2025",
            ],
            "extensions": {},
        },
        "extensions": {},
    }
    decisions = {
        "schema": "openada.engineering-decision/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "decisions": [
            {
                "analysis": "op",
                "decision": "usable-for-bias-state-inspection",
                "evidence": {"vout_v": op_vout},
                "scope": "single mos_tt nominal operating point; no PVT or specification claim",
            },
            {
                "analysis": "dc",
                "decision": "usable-for-static-inverter-transfer-inspection",
                "evidence": normalized["analyses"]["dc"],
                "scope": "0 V to 1.2 V V1 sweep at nominal mos_tt only",
            },
            {
                "analysis": "ac",
                "decision": (
                    "unity-crossing-and-phase-margin-estimated"
                    if ac["unity_crossing_in_band"]
                    else "unity-crossing-not-established-in-simulated-band"
                ),
                "evidence": ac,
                "scope": "small-signal OTA testbench, 1 Hz to 10 MHz, nominal mos_tt; not signoff",
            },
            {
                "analysis": "tran",
                "decision": "usable-for-uniformly-sampled-inverter-switching-inspection",
                "evidence": normalized["analyses"]["tran"],
                "scope": "nominal 32 us waveform; no timing specification or corner claim",
            },
        ],
        "limitations": [
            "These decisions demonstrate provider analysis evidence, not foundry signoff.",
            "No corners, Monte Carlo, mismatch, extracted parasitics, or specification limits were evaluated.",
            "The AC figures are independently calculated from retained complex samples and are scoped only to the pinned public OTA testbench.",
        ],
        "standards_assessment": normalized["standards_assessment"],
        "extensions": {},
    }
    return oracle, normalized, decisions


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    documents = verify(args.evidence)
    print(json.dumps({"oracle": documents[0], "normalized": documents[1], "decision": documents[2]}, indent=2))
