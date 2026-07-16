#!/usr/bin/env python3
"""Independent verifier for IHP transfer and spectral evidence.

This module intentionally imports no ``openada`` implementation module.  It
parses native Spice3 bytes and reimplements the transfer and spectral math.
"""

from __future__ import annotations

import argparse
import cmath
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from common import (
    ConformanceError,
    canonical_sha256,
    load_manifest,
    sha256_file,
    strict_json,
)


HERE = Path(__file__).resolve().parent
MAX_RAW_BYTES = 256 * 1024 * 1024


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load(path: Path, *, label: str) -> dict[str, Any]:
    return strict_json(path, label=label)


def _close(
    actual: float,
    expected: float,
    *,
    label: str,
    absolute: float,
    relative: float = 0.0,
) -> None:
    if not math.isclose(actual, expected, abs_tol=absolute, rel_tol=relative):
        raise ConformanceError(
            f"{label} is {actual:.17g}, expected {expected:.17g} "
            f"(atol={absolute:g}, rtol={relative:g})"
        )


def _raw_plot(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ConformanceError(f"native raw is not one regular file: {path}")
    body = path.read_bytes()
    if not 0 < len(body) <= MAX_RAW_BYTES:
        raise ConformanceError(f"native raw size is outside the reviewed bound: {path}")
    marker = b"Binary:\n"
    offset = body.find(marker)
    if offset < 0 or body.find(marker, offset + 1) >= 0:
        raise ConformanceError(f"native raw lacks exactly one binary plot: {path}")
    try:
        header_text = body[:offset].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConformanceError(f"native raw header is not ASCII: {path}") from exc
    fields: dict[str, str] = {}
    lines = header_text.splitlines()
    variables_at: int | None = None
    for index, line in enumerate(lines):
        if line.strip().casefold() == "variables:":
            variables_at = index + 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            normalized = " ".join(key.strip().casefold().split())
            if normalized in fields:
                raise ConformanceError(f"native raw repeats header {normalized!r}")
            fields[normalized] = value.strip()
    if variables_at is None:
        raise ConformanceError("native raw has no variable table")
    try:
        variable_count = int(fields["no. variables"])
        point_count = int(fields["no. points"])
        plotname = fields["plotname"]
        flags = set(fields["flags"].casefold().split())
    except (KeyError, ValueError) as exc:
        raise ConformanceError("native raw has an invalid closed header") from exc
    if not 1 <= variable_count <= 4096 or not 1 <= point_count <= 100000:
        raise ConformanceError("native raw dimensions exceed reviewed bounds")
    if flags not in ({"real"}, {"complex"}):
        raise ConformanceError(f"unsupported native raw flags: {sorted(flags)}")
    variables: list[tuple[str, str]] = []
    for line in lines[variables_at : variables_at + variable_count]:
        tokens = line.split()
        if len(tokens) < 3 or tokens[0] != str(len(variables)):
            raise ConformanceError("native raw variable table is malformed")
        variables.append((tokens[1].casefold(), tokens[2].casefold()))
    names = [name for name, _native_type in variables]
    if len(names) != variable_count or len(set(names)) != variable_count:
        raise ConformanceError("native raw variable names are incomplete or repeated")
    complex_values = flags == {"complex"}
    width = 2 if complex_values else 1
    payload = body[offset + len(marker) :]
    scalar_count = point_count * variable_count * width
    if len(payload) != scalar_count * 8:
        raise ConformanceError(
            f"native raw payload has {len(payload)} bytes, expected {scalar_count * 8}"
        )
    scalars = struct.unpack(f"={scalar_count}d", payload)
    if not all(math.isfinite(value) for value in scalars):
        raise ConformanceError("native raw contains a non-finite scalar")
    columns: dict[str, list[complex | float]] = {name: [] for name in names}
    position = 0
    for _point in range(point_count):
        for name in names:
            if complex_values:
                columns[name].append(
                    complex(scalars[position], scalars[position + 1])
                )
                position += 2
            else:
                columns[name].append(scalars[position])
                position += 1
    return {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "plotname": plotname,
        "point_count": point_count,
        "variables": variables,
        "complex": complex_values,
        "columns": columns,
    }


def _column(plot: dict[str, Any], name: str) -> list[complex | float]:
    try:
        return plot["columns"][name.casefold()]
    except KeyError as exc:
        raise ConformanceError(f"native raw lacks required vector {name!r}") from exc


def _real(values: list[complex | float], *, label: str) -> list[float]:
    output: list[float] = []
    for value in values:
        if isinstance(value, complex):
            if value.imag != 0.0:
                raise ConformanceError(f"{label} contains a nonzero imaginary component")
            output.append(value.real)
        else:
            output.append(value)
    return output


def _artifact(result: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in result.get("artifacts", []) if item.get("role") == role]
    if len(matches) != 1:
        raise ConformanceError(f"result contains {len(matches)} {role!r} artifacts")
    return matches[0]


def _resolve_evidence_path(root: Path, path_value: object) -> Path:
    if not isinstance(path_value, str) or not path_value.startswith("/evidence/"):
        raise ConformanceError(f"artifact path is outside /evidence: {path_value!r}")
    relative = Path(path_value.removeprefix("/evidence/"))
    if ".." in relative.parts:
        raise ConformanceError("artifact path contains parent traversal")
    return root / relative


def _bind_provider_result(
    root: Path,
    name: str,
    expected_analysis: dict[str, Any],
    raw: dict[str, Any],
) -> dict[str, Any]:
    result = _load(root / f"results/provider-{name}.json", label=f"provider {name} result")
    request = _load(root / f"requests/provider-{name}.json", label=f"provider {name} request")
    protocol = result.get("data", {}).get("protocol", {})
    analysis = result.get("data", {}).get("analysis", {})
    evidence = result.get("data", {}).get("evidence", {})
    if (
        result.get("engineering", {}).get("status") != "pass"
        or result.get("execution", {}).get("status") != "completed"
        or protocol.get("driver_id") != "org.openada.driver.ngspice-pdk-control"
        or protocol.get("driver_version") != "0.5.0"
        or analysis.get("type") != expected_analysis["type"]
        or evidence.get("request_binding") != "exact"
        or evidence.get("structure") != "valid"
        or request.get("parameters", {}).get("analysis") != expected_analysis
    ):
        raise ConformanceError(f"provider {name} is not exact v0.5.0 passing evidence")
    raw_record = _artifact(result, "simulation.result")
    raw_path = _resolve_evidence_path(root, raw_record.get("path"))
    if (
        raw_record.get("sha256") != raw["sha256"]
        or raw_record.get("bytes") != raw["bytes"]
        or sha256_file(raw_path) != raw["sha256"]
    ):
        raise ConformanceError(f"provider {name} result does not bind native raw bytes")
    retained: dict[str, Any] = {}
    for role in ("simulation.log", "simulation.launcher"):
        record = _artifact(result, role)
        path = _resolve_evidence_path(root, record.get("path"))
        if record.get("sha256") != sha256_file(path) or record.get("bytes") != path.stat().st_size:
            raise ConformanceError(f"provider {name} does not bind retained {role}")
        retained[role] = {
            "sha256": record["sha256"],
            "bytes": record["bytes"],
        }
    if analysis.get("point_count") != raw["point_count"]:
        raise ConformanceError(f"provider {name} normalized point count differs from raw")
    return {
        "request_id": protocol.get("request_id"),
        "analysis": expected_analysis,
        "raw_sha256": raw["sha256"],
        "raw_bytes": raw["bytes"],
        "point_count": raw["point_count"],
        "plotname": raw["plotname"],
        "retained": retained,
    }


def _series_digest(series: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "axis": series["axis"],
            "signals": series["signals"],
            "conditions": series["conditions"],
        }
    )


def _verify_extraction(
    root: Path,
    name: str,
    raw: dict[str, Any],
    requests: dict[str, Any],
) -> dict[str, Any]:
    result = _load(root / f"results/extract-{name}.json", label=f"{name} extraction")
    extraction = result.get("data", {}).get("extraction", {})
    if (
        result.get("engineering", {}).get("status") != "pass"
        or result.get("execution", {}).get("status") != "completed"
        or extraction.get("status") != "extracted"
        or extraction.get("source", {}).get("binding") != "verified"
    ):
        raise ConformanceError(f"{name} extraction is not verified passing evidence")
    series = extraction.get("series")
    if not isinstance(series, dict):
        raise ConformanceError(f"{name} extraction lacks normalized series")
    if series.get("source", {}).get("artifact_sha256") != _series_digest(series):
        raise ConformanceError(f"{name} normalized-series digest is not canonical")
    if series.get("source", {}).get("lineage", {}).get("artifact_sha256") != raw["sha256"]:
        raise ConformanceError(f"{name} normalized series does not retain raw lineage")
    expected_conditions = requests["extraction"][
        "ota_ac" if name == "ac" else "inverter_tran"
    ]["conditions"]
    if series.get("conditions") != expected_conditions:
        raise ConformanceError(f"{name} operating conditions drifted")
    signals = {record["name"]: record for record in series["signals"]}
    if name == "ac":
        if raw["plotname"] != "AC Analysis" or not raw["complex"]:
            raise ConformanceError("OTA raw is not a complex AC Analysis plot")
        frequencies = [complex(value).real for value in _column(raw, "frequency")]
        vp = [complex(value) for value in _column(raw, "v(vp)")]
        vout = [complex(value) for value in _column(raw, "v(vout)")]
        expected = {
            "vp.real": [value.real for value in vp],
            "vp.imag": [value.imag for value in vp],
            "vout.real": [value.real for value in vout],
            "vout.imag": [value.imag for value in vout],
        }
        if series["axis"]["name"] != "frequency" or series["axis"]["unit"] != "Hz":
            raise ConformanceError("OTA extraction axis is not frequency/Hz")
        if series["axis"]["values"] != frequencies:
            raise ConformanceError("OTA extraction frequencies differ from native raw")
    else:
        if raw["plotname"] != "Transient Analysis (linearized)" or raw["complex"]:
            raise ConformanceError("inverter raw is not the exact linearized transient plot")
        times = _real(_column(raw, "time"), label="inverter time")
        expected = {"v(vout)": _real(_column(raw, "v(vout)"), label="inverter output")}
        if series["axis"]["name"] != "time" or series["axis"]["unit"] != "s":
            raise ConformanceError("inverter extraction axis is not time/s")
        if series["axis"]["values"] != times:
            raise ConformanceError("inverter extraction times differ from native raw")
    if set(signals) != set(expected):
        raise ConformanceError(f"{name} extraction signal selection drifted")
    for signal_name, values in expected.items():
        if signals[signal_name]["unit"] != "V" or signals[signal_name]["values"] != values:
            raise ConformanceError(
                f"{name} extraction signal {signal_name!r} differs from native raw"
            )
    return series


def _principal(value: complex) -> float:
    phase = math.degrees(math.atan2(value.imag, value.real))
    return phase - 360.0 if phase >= 180.0 else phase


def _transfer_metrics(raw: dict[str, Any]) -> dict[str, float]:
    frequencies = [complex(value).real for value in _column(raw, "frequency")]
    output = [complex(value) for value in _column(raw, "v(vout)")]
    input_values = [complex(value) for value in _column(raw, "v(vp)")]
    ratios: list[complex] = []
    magnitudes: list[float] = []
    for index, (out, inp) in enumerate(zip(output, input_values, strict=True)):
        if out == 0j or inp == 0j:
            raise ConformanceError(f"undefined OTA ratio at AC point {index}")
        ratio = out / inp
        ratios.append(ratio)
        magnitudes.append(20.0 * math.log10(abs(ratio)))
    phases = [_principal(value) for value in ratios]
    unwrapped = [phases[0]]
    for phase in phases[1:]:
        candidate = phase
        while candidate - unwrapped[-1] >= 180.0:
            candidate -= 360.0
        while candidate - unwrapped[-1] < -180.0:
            candidate += 360.0
        unwrapped.append(candidate)

    def crossings(threshold: float) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for index in range(len(frequencies) - 1):
            y0, y1 = magnitudes[index], magnitudes[index + 1]
            if not (y0 > threshold and y1 <= threshold):
                continue
            fraction = (threshold - y0) / (y1 - y0)
            log_frequency = math.log10(frequencies[index]) + fraction * (
                math.log10(frequencies[index + 1])
                - math.log10(frequencies[index])
            )
            phase = unwrapped[index] + fraction * (
                unwrapped[index + 1] - unwrapped[index]
            )
            result.append((10.0**log_frequency, phase))
        return result

    bandwidth = crossings(magnitudes[0] - 3.0)
    unity = crossings(0.0)
    if len(bandwidth) != 1 or len(unity) != 1:
        raise ConformanceError(
            f"OTA trace has {len(bandwidth)} bandwidth and {len(unity)} unity crossings"
        )
    return {
        "low_frequency_gain_db": magnitudes[0],
        "bandwidth_3db": bandwidth[0][0],
        "unity_gain_frequency": unity[0][0],
        "phase_margin": 180.0 + unity[0][1],
    }


def _direct_dft_powers(values: list[float]) -> list[float]:
    """Independent O(N^2) DFT, intentionally unlike OpenADA's radix-2 FFT."""

    count = len(values)
    mean = math.fsum(values) / count
    centered = [value - mean for value in values]
    powers: list[float] = []
    for bin_index in range(count // 2 + 1):
        accumulator = 0j
        for sample_index, value in enumerate(centered):
            angle = -2.0 * math.pi * bin_index * sample_index / count
            accumulator += value * complex(math.cos(angle), math.sin(angle))
        scale = 1.0 if bin_index in {0, count // 2} else 2.0
        powers.append(scale * abs(accumulator) ** 2 / (count * count))
    return powers


def _spectral_metrics(raw: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    times = _real(_column(raw, "time"), label="inverter time")
    values = _real(_column(raw, "v(vout)"), label="inverter output")
    if len(times) != 1024 or len(values) != 1024:
        raise ConformanceError("inverter spectral record does not have 1024 samples")
    interval = (times[-1] - times[0]) / (len(times) - 1)
    relative_errors = [
        abs((right - left) - interval) / interval
        for left, right in zip(times, times[1:])
    ]
    if max(relative_errors) > 1e-9:
        raise ConformanceError("linearized inverter time axis is not uniformly sampled")
    sample_rate = 1.0 / interval
    bin_width = sample_rate / len(times)
    _close(bin_width, 31250.0, label="spectral bin width", absolute=1e-6)
    fundamental_bin = round(500000.0 / bin_width)
    if fundamental_bin != 16:
        raise ConformanceError(f"fundamental is at bin {fundamental_bin}, expected 16")
    harmonic_bins = {order * fundamental_bin for order in range(2, 32)}
    if max(harmonic_bins) >= len(times) // 2:
        raise ConformanceError("reviewed harmonics unexpectedly require folding")
    powers = _direct_dft_powers(values)
    # The closed request stops at 15,999,999 Hz, intentionally one hertz below
    # the retained floating-point Nyquist value.  Bin 512 is therefore outside
    # the declared band; this also prevents treating the square wave's Nyquist
    # component as generic in-band noise.
    band_bins = set(range(len(powers) - 1))
    residual = sorted(band_bins - {0, fundamental_bin})
    noise = sorted(set(residual) - harmonic_bins)
    fundamental_power = powers[fundamental_bin]
    harmonic_power = math.fsum(powers[index] for index in sorted(harmonic_bins))
    noise_power = math.fsum(powers[index] for index in noise)
    residual_power = math.fsum(powers[index] for index in residual)
    winning = min(residual, key=lambda index: (-powers[index], index))
    metrics = {
        "snr": 10.0 * math.log10(fundamental_power / noise_power),
        "sinad": 10.0 * math.log10(fundamental_power / residual_power),
        "thd": 10.0 * math.log10(harmonic_power / fundamental_power),
        "sfdr": 10.0 * math.log10(fundamental_power / powers[winning]),
    }
    partition = {
        "sample_count": len(times),
        "sample_rate_hz": sample_rate,
        "bin_width_hz": bin_width,
        "fundamental_bin": fundamental_bin,
        "harmonic_bins": sorted(harmonic_bins),
        "noise_bin_count": len(noise),
        "residual_bin_count": len(residual),
        "winning_spur_bin": winning,
        "powers": {
            "fundamental": fundamental_power,
            "harmonics": harmonic_power,
            "noise": noise_power,
            "residual": residual_power,
            "winning_spur": powers[winning],
        },
    }
    return metrics, partition


NEGATIVES = {
    "netlist-missing-symbol": ("netlist-missing-symbol.json", "fail", "xschem.missing_symbol"),
    "provider-ac-parameter-mismatch": ("provider-ac-parameter-mismatch.json", "unknown", "simulation.request.invalid"),
    "provider-tran-parameter-mismatch": ("provider-tran-parameter-mismatch.json", "unknown", "simulation.request.invalid"),
    "extract-ac-missing-selector": ("extract-ac-missing-selector.json", "unknown", "series.selector.missing"),
    "extract-tran-missing-selector": ("extract-tran-missing-selector.json", "unknown", "series.selector.missing"),
    "transfer-low-frequency-gain-invalid-unit": ("transfer-low_frequency_gain_db.json", "unknown", "transfer.unit.mismatch"),
    "transfer-bandwidth-unsupported-drop": ("transfer-bandwidth_3db.json", "unknown", "transfer.method.unsupported"),
    "transfer-unity-unsupported-policy": ("transfer-unity_gain_frequency.json", "unknown", "transfer.method.unsupported"),
    "transfer-phase-margin-invalid-context": ("transfer-phase_margin.json", "unknown", "transfer.phase_margin.invalid_context"),
    "spectral-snr-noncoherent": ("spectral-snr.json", "unknown", "spectral.coherence.not_established"),
    "spectral-sinad-record-length": ("spectral-sinad.json", "unknown", "spectral.method.record_length_mismatch"),
    "spectral-thd-unsupported-window": ("spectral-thd.json", "unknown", "spectral.method.unsupported"),
    "spectral-sfdr-invalid-standard-context": ("spectral-sfdr.json", "unknown", "spectral.standard_context.invalid"),
}


def _verify_negatives(root: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for replay_id, (filename, status, diagnostic) in NEGATIVES.items():
        result = _load(root / "negative" / filename, label=f"negative replay {replay_id}")
        codes = [item.get("code") for item in result.get("diagnostics", [])]
        if result.get("engineering", {}).get("status") != status or diagnostic not in codes:
            raise ConformanceError(
                f"negative replay {replay_id} does not retain {status}/{diagnostic}"
            )
        summary[replay_id] = {
            "engineering_status": status,
            "execution_status": result.get("execution", {}).get("status"),
            "diagnostic": diagnostic,
            "result_sha256": sha256_file(root / "negative" / filename),
        }
    return summary


def _verify_sources(
    root: Path, manifest: dict[str, Any], requests: dict[str, Any]
) -> dict[str, Any]:
    record = _load(root / "source-record.json", label="retained source record")
    expected_inputs = {item["path"]: item["sha256"] for item in manifest["design"]["inputs"]}
    for name, transform in requests["source_transforms"].items():
        upstream = root / f"sources/{name}-upstream.sch"
        derived = root / transform["derived"]
        rcfile = root / transform["rcfile"]
        if sha256_file(upstream) != expected_inputs[transform["source"]]:
            raise ConformanceError(f"retained {name} upstream source does not match public pin")
        text = upstream.read_text(encoding="utf-8")
        if name == "ota":
            if text.count(transform["old_control"]) != 1:
                raise ConformanceError("retained OTA source no longer has the reviewed control block")
            expected_text = text.replace(
                transform["old_control"], transform["new_control"], 1
            )
        else:
            if text.count(transform["old_pulse"]) != 1 or text.count(transform["old_control"]) != 1:
                raise ConformanceError("retained inverter source no longer has reviewed transform sites")
            expected_text = text.replace(
                transform["old_pulse"], transform["new_pulse"], 1
            ).replace(transform["old_control"], transform["new_control"], 1)
        if derived.read_text(encoding="utf-8") != expected_text:
            raise ConformanceError(f"derived {name} schematic is not the closed transform")
        if rcfile.read_text(encoding="utf-8") != transform["rcfile_text"]:
            raise ConformanceError(f"derived {name} rcfile drifted")
        for key, path in (("upstream", upstream), ("derived", derived), ("rcfile", rcfile)):
            entry = record.get(name, {}).get(key, {})
            if entry.get("sha256") != sha256_file(path) or entry.get("bytes") != path.stat().st_size:
                raise ConformanceError(f"source record does not bind {name} {key}")
    return record


def _verify_measurement_results(
    root: Path,
    family: str,
    independent: dict[str, float],
    contract: dict[str, Any],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    expected_standard = contract.get("standards_context")
    for metric in contract["metrics"]:
        kind = metric["kind"]
        path = root / f"results/{family}-{kind}.json"
        result = _load(path, label=f"{family} {kind} result")
        measurement = result.get("data", {}).get("measurement", {})
        if (
            result.get("engineering", {}).get("status") != "pass"
            or result.get("execution", {}).get("status") != "completed"
            or measurement.get("status") != "measured"
        ):
            raise ConformanceError(f"{family} {kind} is not passing measured evidence")
        value = measurement.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConformanceError(f"{family} {kind} value is not finite numeric evidence")
        _close(
            float(value),
            independent[kind],
            label=f"{family} {kind} independent recomputation",
            absolute=max(metric["absolute_tolerance"], 1e-9),
            relative=1e-12,
        )
        _close(
            float(value),
            metric["expected"],
            label=f"{family} {kind} reviewed reference",
            absolute=metric["absolute_tolerance"],
        )
        if family == "spectral":
            context = result.get("data", {}).get("spectral", {}).get(
                "standards_context"
            )
            if context != expected_standard:
                raise ConformanceError(
                    f"spectral {kind} standards context is not generic OpenADA"
                )
        summary[kind] = {
            "value": float(value),
            "unit": measurement.get("unit"),
            "result_sha256": sha256_file(path),
        }
    return summary


def _expected_decision(
    root: Path, requests: dict[str, Any], normalized_sha256: str
) -> dict[str, Any]:
    return {
        "schema": "openada.ihp-analog-engineering-decision/v0alpha1",
        "status": "proceed-to-requirements-and-pvt-review",
        "design_pass": False,
        "signoff": False,
        "ota": {
            "conclusion": "nominal-tt-open-loop-response-measured",
            "project_numeric_specification_present": False,
            "pvt_sweep_performed": False,
            "monte_carlo_or_mismatch_performed": False,
            "decision": "Do not claim a design pass; define requirements and review PVT and statistical coverage next.",
        },
        "inverter": {
            "conclusion": "high-harmonic-content-expected-for-square-wave",
            "converter_quality_metric": False,
            "decision": "Do not interpret SNR, SINAD, THD, or SFDR as ADC or DAC quality for this inverter waveform.",
        },
        "standards": deepcopy(requests["standards"]),
        "normalized_evidence_sha256": normalized_sha256,
        "extensions": {},
    }


def _verify_core(
    root: Path, manifest: dict[str, Any], requests: dict[str, Any]
) -> dict[str, Any]:
    _verify_sources(root, manifest, requests)
    ota_raw = _raw_plot(root / "provider-ac/work/ota_ac.raw")
    inverter_raw = _raw_plot(root / "provider-tran/work/inverter_spectral.raw")
    if ota_raw["plotname"] != "AC Analysis" or ota_raw["point_count"] != 701:
        raise ConformanceError("OTA AC raw does not retain the reviewed 701-point plot")
    if (
        inverter_raw["plotname"] != "Transient Analysis (linearized)"
        or inverter_raw["point_count"] != 1024
    ):
        raise ConformanceError("inverter raw does not retain exact 1024-point linearized plot")
    bindings = {
        "ac": _bind_provider_result(
            root,
            "ac",
            requests["native_analyses"]["ota_ac"]["analysis"],
            ota_raw,
        ),
        "tran": _bind_provider_result(
            root,
            "tran",
            requests["native_analyses"]["inverter_tran"]["analysis"],
            inverter_raw,
        ),
    }
    _verify_extraction(root, "ac", ota_raw, requests)
    _verify_extraction(root, "tran", inverter_raw, requests)
    transfer = _transfer_metrics(ota_raw)
    spectral, partition = _spectral_metrics(inverter_raw)
    normalized = _load(root / "normalized-evidence.json", label="normalized measurement evidence")
    if normalized.get("standards") != requests["standards"]:
        raise ConformanceError("normalized standards assessment drifted")
    transfer_results = _verify_measurement_results(
        root, "transfer", transfer, requests["transfer"]
    )
    spectral_results = _verify_measurement_results(
        root, "spectral", spectral, requests["spectral"]
    )
    for family, values in (("transfer", transfer_results), ("spectral", spectral_results)):
        retained = normalized.get(family)
        if not isinstance(retained, dict) or set(retained) != set(values):
            raise ConformanceError(f"normalized {family} metric set drifted")
        for kind, result in values.items():
            if retained[kind].get("value") != result["value"]:
                raise ConformanceError(f"normalized {family} {kind} value drifted")
    normalized_sha256 = sha256_file(root / "normalized-evidence.json")
    decision = _load(root / "engineering-decision.json", label="engineering decision")
    if decision != _expected_decision(root, requests, normalized_sha256):
        raise ConformanceError("engineering decision is not the reviewed bounded conclusion")
    negatives = _verify_negatives(root)
    return {
        "schema": "openada.ihp-analog-independent-oracle/v0alpha1",
        "status": "pass",
        "native_bindings": bindings,
        "transfer": {
            "metrics": transfer,
            "method": "independent native complex-ratio and log-frequency crossing recomputation",
        },
        "spectral": {
            "metrics": spectral,
            "partition": partition,
            "method": "independent direct O(N^2) DFT and explicit bin partition",
        },
        "negative_replays": negatives,
        "standards": deepcopy(requests["standards"]),
        "decision_sha256": sha256_file(root / "engineering-decision.json"),
        "extensions": {},
    }


TAMPER_IDS = (
    "native-raw-byte",
    "normalized-metric-value",
    "standards-context",
    "source-contract-hash",
    "engineering-decision",
)


def _mutate_tamper(root: Path, replay_id: str) -> None:
    if replay_id == "native-raw-byte":
        path = root / "provider-tran/work/inverter_spectral.raw"
        body = bytearray(path.read_bytes())
        body[-1] ^= 1
        path.write_bytes(body)
        return
    if replay_id == "normalized-metric-value":
        path = root / "results/transfer-low_frequency_gain_db.json"
        value = _load(path, label="tampered transfer result")
        value["data"]["measurement"]["value"] += 1.0
        _write_json(path, value)
        return
    if replay_id == "standards-context":
        path = root / "results/spectral-snr.json"
        value = _load(path, label="tampered spectral result")
        value["data"]["spectral"]["standards_context"] = {
            "domain": "adc",
            "reference": "ieee-1241-2023",
            "alignment": "candidate",
        }
        _write_json(path, value)
        return
    if replay_id == "source-contract-hash":
        path = root / "source-record.json"
        value = _load(path, label="tampered source record")
        value["ota"]["upstream"]["sha256"] = "0" * 64
        _write_json(path, value)
        return
    if replay_id == "engineering-decision":
        path = root / "engineering-decision.json"
        value = _load(path, label="tampered engineering decision")
        value["design_pass"] = True
        _write_json(path, value)
        return
    raise ConformanceError(f"unknown tamper replay {replay_id!r}")


def _run_tamper_probes(
    root: Path, manifest: dict[str, Any], requests: dict[str, Any]
) -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for replay_id in TAMPER_IDS:
        with tempfile.TemporaryDirectory(prefix=f"openada-measurement-{replay_id}-") as temporary:
            copy = Path(temporary) / "evidence"
            shutil.copytree(root, copy)
            _mutate_tamper(copy, replay_id)
            try:
                _verify_core(copy, manifest, requests)
            except ConformanceError as exc:
                rejection = str(exc)
            else:
                raise ConformanceError(f"tamper replay {replay_id} was accepted")
        receipts[replay_id] = {
            "schema": "openada.ihp-analog-tamper-replay/v0alpha1",
            "id": replay_id,
            "status": "pass",
            "expected": "independent verifier rejection",
            "observed": rejection,
            "mutation": {
                "native-raw-byte": "one retained linearized-raw payload byte",
                "normalized-metric-value": "one normalized transfer scalar",
                "standards-context": "generic inverter context relabeled as ADC candidate",
                "source-contract-hash": "retained public-source identity",
                "engineering-decision": "design_pass false changed to true",
            }[replay_id],
            "extensions": {},
        }
    return receipts


def verify_evidence(
    evidence: Path,
    *,
    manifest_path: Path = HERE / "manifest.json",
    requests_path: Path = HERE / "requests.json",
    materialize: bool = False,
    run_tamper_probes: bool = False,
) -> dict[str, Any]:
    root = evidence.expanduser().resolve()
    manifest = load_manifest(manifest_path.resolve())
    provenance_path = root / "design-provenance.json"
    if provenance_path.is_file():
        provenance = _load(provenance_path, label="design provenance")
        provenance_schema = _load(
            HERE.parents[1] / "schemas/design-provenance-v0alpha1.schema.json",
            label="design provenance schema",
        )
        errors = sorted(
            Draft202012Validator(
                provenance_schema, format_checker=FormatChecker()
            ).iter_errors(provenance),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ConformanceError(
                f"design provenance violates its schema: {errors[0].message}"
            )
        for field in ("repository", "revision", "tree"):
            if provenance[field] != manifest["design"][field]:
                raise ConformanceError(
                    f"design provenance {field} differs from manifest"
                )
        if [
            {key: item[key] for key in ("path", "sha256")}
            for item in provenance["inputs"]
        ] != manifest["design"]["inputs"]:
            raise ConformanceError("design provenance inputs differ from manifest")
    requests = _load(requests_path.resolve(), label="measurement request contract")
    oracle = _verify_core(root, manifest, requests)
    tamper = (
        _run_tamper_probes(root, manifest, requests) if run_tamper_probes else {}
    )
    agent = {
        "schema": "openada.ihp-analog-agent-evidence/v0alpha1",
        "status": "proceed-to-requirements-and-pvt-review",
        "engineering": {
            "ota": {
                "status": "nominal-tt-open-loop-response-measured",
                "metrics": oracle["transfer"]["metrics"],
                "decision": "Requirements and PVT/statistical review are required; this is not a design pass or signoff.",
            },
            "inverter": {
                "status": "expected-square-wave-harmonics-observed",
                "metrics": oracle["spectral"]["metrics"],
                "decision": "These generic ratios describe an inverter waveform and are not ADC or DAC quality evidence.",
            },
        },
        "standards": oracle["standards"],
        "limitations": [
            "OTA evidence is one nominal 27 degC mos_tt run with no project numeric specification.",
            "No PVT, Monte Carlo, mismatch, post-layout parasitics, or reliability analysis was performed.",
            "Public IEEE pages were reviewed only for lifecycle and scope; no licensed clause-level review or standards-conformance claim was made.",
        ],
        "lineage": {
            "oracle_sha256": canonical_sha256(oracle),
            "engineering_decision_sha256": oracle["decision_sha256"],
            "ota_raw_sha256": oracle["native_bindings"]["ac"]["raw_sha256"],
            "inverter_raw_sha256": oracle["native_bindings"]["tran"]["raw_sha256"],
        },
        "negative_replays": oracle["negative_replays"],
        "extensions": {},
    }
    report = {
        "schema": "openada.ihp-analog-measurement-verification/v0alpha1",
        "status": "pass",
        "oracle": oracle,
        "tamper_replays": tamper,
        "agent_evidence": agent,
        "extensions": {},
    }
    if materialize:
        _write_json(root / "independent-oracle.json", oracle)
        _write_json(root / "agent-evidence.json", agent)
        contract = {
            "schema": "openada.ihp-analog-contract-test/v0alpha1",
            "status": "pass",
            "checks": [
                "semantic-chain manifest schema",
                "result envelopes and exact operation protocol",
                "provider 0.5.0 request/artifact binding",
                "independent native Spice3 parsing",
                "independent transfer and spectral recomputation",
                "closed IEEE scope-only context",
            ],
            "requests_sha256": sha256_file(requests_path),
            "manifest_sha256": sha256_file(manifest_path),
            "extensions": {},
        }
        _write_json(root / "contract-test.json", contract)
        if not tamper:
            tamper = _run_tamper_probes(root, manifest, requests)
            report["tamper_replays"] = tamper
        for replay_id, receipt in tamper.items():
            _write_json(root / "tamper" / f"{replay_id}.json", receipt)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--requests", type=Path, default=HERE / "requests.json")
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--run-tamper-probes", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = verify_evidence(
            args.evidence,
            manifest_path=args.manifest,
            requests_path=args.requests,
            materialize=args.materialize,
            run_tamper_probes=args.run_tamper_probes,
        )
    except ConformanceError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
