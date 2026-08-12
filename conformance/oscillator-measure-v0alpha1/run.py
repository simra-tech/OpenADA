#!/usr/bin/env python3
"""Generate fresh deterministic oscillator-primitives conformance evidence."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"

# Exercise this checkout, never an ambient OpenADA installation.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from openada.operations.result_measure import normalized_series_sha256  # noqa: E402
from openada.operations.result_osc_measure import (  # noqa: E402
    IMPLEMENTATION_ID,
    IMPLEMENTATION_VERSION,
    measure_oscillator,
)
from verify import load_cases, load_manifest, verify_evidence  # noqa: E402


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _series(
    definition: dict[str, Any],
    source_definition: dict[str, Any],
) -> dict[str, Any]:
    frequency_hz = float(definition["frequency_hz"])
    samples_per_cycle = int(definition["samples_per_cycle"])
    step = 1.0 / (frequency_hz * samples_per_cycle)
    count = math.ceil(float(definition["stop_s"]) / step)
    axis_values = [index * step for index in range(count + 1)]

    generator = definition["generator"]
    amplitude = float(definition["amplitude_v"])
    differential: list[float] = []
    for coordinate in axis_values:
        primary = amplitude * math.sin(2.0 * math.pi * frequency_hz * coordinate)
        if generator == "sine":
            value = primary
        elif generator == "flat":
            value = 0.0
        elif generator == "decaying_sine":
            value = primary * math.exp(-coordinate / float(definition["decay_time_s"]))
        elif generator == "collapse_sine":
            value = primary if coordinate < float(definition["collapse_at_s"]) else 0.0
        elif generator == "two_tone":
            value = primary + float(definition["second_amplitude_v"]) * math.sin(
                2.0 * math.pi * float(definition["second_frequency_hz"]) * coordinate
            )
        else:  # fixture validation rejects this before execution
            raise ValueError(f"unsupported deterministic generator {generator!r}")
        differential.append(value)

    axis = {"name": "time", "unit": "s", "values": axis_values}
    supply_voltage = float(definition["supply_voltage_v"])
    supply_current = float(definition["supply_current_a"])
    signals = [
        {
            "name": "v(outp)",
            "unit": "V",
            "values": [value / 2.0 for value in differential],
        },
        {
            "name": "v(outn)",
            "unit": "V",
            "values": [-value / 2.0 for value in differential],
        },
        {
            "name": "v(vdd)",
            "unit": "V",
            "values": [supply_voltage for _ in axis_values],
        },
        {
            "name": "i(vdd)",
            "unit": "A",
            "values": [supply_current for _ in axis_values],
        },
    ]
    conditions = deepcopy(definition["conditions"])
    digest = normalized_series_sha256(
        axis=axis,
        signals=signals,
        conditions=conditions,
    )
    return {
        "source": {
            **deepcopy(source_definition),
            "artifact_sha256": digest,
        },
        "axis": axis,
        "signals": signals,
        "conditions": conditions,
        "extensions": {},
    }


def _composition_definition(
    cases: dict[str, Any],
    *,
    frequency_hz: float,
    generator: str,
    conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    common = cases["composition_series"]
    supply_voltage = float(common["supply_voltage_v"])
    for condition in conditions:
        if condition["name"] == "vdd" and condition["unit"] == "V":
            supply_voltage = float(condition["value"])
    return {
        "generator": generator,
        "frequency_hz": frequency_hz,
        "amplitude_v": common["amplitude_v"] if generator != "flat" else 0.0,
        "stop_s": common["stop_s"],
        "samples_per_cycle": common["samples_per_cycle"],
        "supply_voltage_v": supply_voltage,
        "supply_current_a": common["supply_current_a"],
        "conditions": conditions,
    }


def _receipt(
    cases: dict[str, Any],
    *,
    method_name: str,
    frequency_hz: float,
    generator: str,
    conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    definition = _composition_definition(
        cases,
        frequency_hz=frequency_hz,
        generator=generator,
        conditions=conditions,
    )
    series = _series(definition, cases["source"])
    payload = measure_oscillator(series, cases["transient_methods"][method_name])
    receipt = payload["data"]["receipt"]
    if receipt is None:
        raise RuntimeError("fixture receipt generation did not produce a receipt")
    return receipt


def _grid_receipts(cases: dict[str, Any], case: dict[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    base = deepcopy(cases["composition_series"]["base_conditions"])
    for point in case["points"]:
        conditions = base + [
            {"name": "vctrl", "value": point["control_v"], "unit": "V"},
            {"name": "vdd", "value": 1.2, "unit": "V"},
        ]
        receipts.append(
            _receipt(
                cases,
                method_name=case["method"],
                frequency_hz=point["frequency_hz"],
                generator=point["generator"],
                conditions=conditions,
            )
        )
    return receipts


def _grid_request(case: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **deepcopy(case["measurement"]),
        "points": [
            {
                "control": {"value": point["control_v"], "unit": "V"},
                "receipt": receipt,
            }
            for point, receipt in zip(case["points"], receipts)
        ],
    }


def _shift_receipts(
    cases: dict[str, Any], case: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    name = case["measurement"]["perturbation_condition"]
    output: list[dict[str, Any]] = []
    for member_name in ("reference", "perturbed"):
        member = case[member_name]
        conditions = deepcopy(cases["composition_series"]["base_conditions"])
        conditions.append({"name": "vctrl", "value": 0.81, "unit": "V"})
        if name != "vdd":
            conditions.append({"name": "vdd", "value": 1.2, "unit": "V"})
        conditions.append(
            {"name": name, "value": member["condition_value"], "unit": "V" if name == "vdd" else "1"}
        )
        output.append(
            _receipt(
                cases,
                method_name=case["method"],
                frequency_hz=member["frequency_hz"],
                generator=member["generator"],
                conditions=conditions,
            )
        )
    return output[0], output[1]


def _shift_request(
    case: dict[str, Any],
    reference: dict[str, Any],
    perturbed: dict[str, Any],
) -> dict[str, Any]:
    condition_unit = (
        "V" if case["measurement"]["perturbation_condition"] == "vdd" else "1"
    )
    return {
        **deepcopy(case["measurement"]),
        "reference": {
            "condition": {
                "value": case["reference"]["condition_value"],
                "unit": condition_unit,
            },
            "receipt": reference,
        },
        "perturbed": {
            "condition": {
                "value": case["perturbed"]["condition_value"],
                "unit": condition_unit,
            },
            "receipt": perturbed,
        },
    }


def run_suite(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(manifest)

    transient_records: list[dict[str, Any]] = []
    for case in cases["transient_cases"]:
        series = _series(cases["waveforms"][case["waveform"]], cases["source"])
        measurement = cases["transient_methods"][case["method"]]
        request = {"series": series, "measurement": measurement, "extensions": {}}
        result = measure_oscillator(series, measurement, request_id=case["request_id"])
        transient_records.append(
            {
                "id": case["id"],
                "feature_ids": case["feature_ids"],
                "request_sha256": _canonical_sha256(request),
                "series_sha256": series["source"]["artifact_sha256"],
                "result": result,
            }
        )

    grid_records: list[dict[str, Any]] = []
    for case in cases["grid_cases"]:
        receipts = _grid_receipts(cases, case)
        measurement = _grid_request(case, receipts)
        request = {"measurement": measurement, "extensions": {}}
        result = measure_oscillator(None, measurement, request_id=case["request_id"])
        grid_records.append(
            {
                "id": case["id"],
                "feature_ids": case["feature_ids"],
                "request_sha256": _canonical_sha256(request),
                "receipts": receipts,
                "result": result,
            }
        )

    shift_records: list[dict[str, Any]] = []
    for case in cases["shift_cases"]:
        reference, perturbed = _shift_receipts(cases, case)
        measurement = _shift_request(case, reference, perturbed)
        request = {"measurement": measurement, "extensions": {}}
        result = measure_oscillator(None, measurement, request_id=case["request_id"])
        shift_records.append(
            {
                "id": case["id"],
                "feature_ids": case["feature_ids"],
                "request_sha256": _canonical_sha256(request),
                "receipts": [reference, perturbed],
                "result": result,
            }
        )

    rejection_records: list[dict[str, Any]] = []
    for case in cases["receipt_rejection_cases"]:
        original = _grid_receipts(cases, case)
        receipts = deepcopy(original)
        if case["mutation"] != "increment-frequency-value-without-rehash":
            raise ValueError(f"unsupported receipt mutation {case['mutation']!r}")
        receipts[0]["frequency"]["value"] += 1.0
        measurement = _grid_request(case, receipts)
        request = {"measurement": measurement, "extensions": {}}
        result = measure_oscillator(None, measurement, request_id=case["request_id"])
        rejection_records.append(
            {
                "id": case["id"],
                "feature_ids": case["feature_ids"],
                "mutation": case["mutation"],
                "request_sha256": _canonical_sha256(request),
                "original_receipt_sha256": [item["sha256"] for item in original],
                "receipts": receipts,
                "result": result,
            }
        )

    return {
        "schema": "openada.oscillator-measure-conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "implementation": {
            "id": IMPLEMENTATION_ID,
            "version": IMPLEMENTATION_VERSION,
        },
        "fixture_sha256": manifest["fixture"]["sha256"],
        "transients": transient_records,
        "grids": grid_records,
        "shifts": shift_records,
        "receipt_rejections": rejection_records,
    }


def _write_new(path: Path, document: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"evidence parent directory does not exist: {path.parent}")
    encoded = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-file", type=Path, required=True)
    arguments = parser.parse_args(argv)

    record = run_suite(arguments.manifest.resolve())
    evidence_path = arguments.evidence_file.resolve()
    _write_new(evidence_path, record)
    verification = verify_evidence(
        evidence_path,
        manifest_path=arguments.manifest.resolve(),
    )
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
