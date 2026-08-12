#!/usr/bin/env python3
"""Independently verify deterministic oscillator-measurement conformance evidence.

This verifier intentionally does not import :mod:`openada`.  It binds the
manifest, profile, base result schema, and fixture by digest, regenerates every
synthetic waveform, and recomputes the reference oscillator math locally.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import stat
import sys
from typing import Any, Sequence

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
MAX_JSON_BYTES = 8 * 1024 * 1024

FEATURES = (
    "openada.feature/oscillator.crossing-frequency/v1alpha1",
    "openada.feature/oscillator.startup-hold/v1alpha1",
    "openada.feature/oscillator.differential-amplitude/v1alpha1",
    "openada.feature/oscillator.average-supply-power/v1alpha1",
    "openada.feature/oscillator.local-tuning-gain/v1alpha1",
    "openada.feature/oscillator.frequency-span/v1alpha1",
    "openada.feature/oscillator.perturbation-shift/v1alpha1",
)
TRANSIENT_CASE_IDS = (
    "clean-sustained-reference",
    "decaying-startup-ringing",
    "never-started",
    "started-then-collapsed",
    "beating-two-tone-qc",
)
GRID_CASE_IDS = (
    "benchmark-irregular-nine-point-grid",
    "nonmonotonic-grid-qc",
    "incomplete-grid-propagates-unknown",
)
SHIFT_CASE_IDS = (
    "signed-supply-perturbation-shift",
    "incomplete-shift-propagates-unknown",
)
REJECTION_CASE_IDS = ("tampered-receipt-rejected",)


class ConformanceError(RuntimeError):
    """A pinned contract, fixture, run record, or result is inconsistent."""


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _expect_close(actual: Any, expected: Any, location: str) -> None:
    if expected is None:
        _expect(actual, None, location)
        return
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ConformanceError(f"{location}: expected a finite number, got {actual!r}")
    if not math.isfinite(float(actual)) or not math.isclose(
        float(actual), float(expected), rel_tol=2e-12, abs_tol=1e-15
    ):
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _require_regular(path: Path, *, label: str, maximum_bytes: int = MAX_JSON_BYTES) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum_bytes:
        raise ConformanceError(
            f"{label} size {metadata.st_size} is outside 1..{maximum_bytes} bytes"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    expected_size = _require_regular(path, label=label)
    try:
        encoded = path.read_bytes()
        if len(encoded) != expected_size:
            raise ConformanceError(f"{label} changed while it was read: {path}")
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConformanceError(f"value is not canonical finite JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _repository_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ConformanceError(f"{label} must be a nonempty repository-relative path")
    candidate = (REPOSITORY_ROOT / value).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise ConformanceError(f"{label} escapes the repository root") from exc
    return candidate


def _contract_document(record: dict[str, Any], *, label: str) -> dict[str, Any]:
    _expect(set(record), {"id", "repository_path", "sha256"}, f"{label}.keys")
    path = _repository_path(record["repository_path"], label=f"{label}.repository_path")
    _require_regular(path, label=label)
    _expect(_sha256(path), record["sha256"], f"{label}.sha256")
    return _read_json(path, label=label)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _read_json(path.resolve(), label="oscillator conformance manifest")
    _expect(
        set(manifest),
        {"schema", "id", "implementation", "contracts", "fixture", "features", "cases", "policy"},
        "manifest.keys",
    )
    _expect(
        manifest["schema"],
        "openada.oscillator-measure-conformance/v0alpha1",
        "manifest.schema",
    )
    _expect(manifest["id"], "oscillator-measurement-primitives-v0alpha1", "manifest.id")
    _expect(
        manifest["implementation"],
        {
            "id": "org.openada.kernel.oscillator-evidence",
            "runtime": "python",
            "version": "1.0.0",
        },
        "manifest.implementation",
    )
    _expect(manifest["features"], list(FEATURES), "manifest.features")
    _expect(
        manifest["cases"],
        {
            "transient": list(TRANSIENT_CASE_IDS),
            "grid": list(GRID_CASE_IDS),
            "shift": list(SHIFT_CASE_IDS),
            "receipt_rejection": list(REJECTION_CASE_IDS),
        },
        "manifest.cases",
    )
    _expect(
        manifest["policy"],
        {
            "native_eda": "none",
            "network": "none",
            "input_mode": "read-only-fixture",
            "evidence_mode": "new-file-only",
            "maximum_evidence_bytes": MAX_JSON_BYTES,
        },
        "manifest.policy",
    )

    contracts = manifest["contracts"]
    _expect(set(contracts), {"result_schema", "oscillator"}, "manifest.contracts")
    result_schema = _contract_document(contracts["result_schema"], label="result schema")
    _expect(contracts["result_schema"]["id"], "openada.result/v0alpha1", "result contract id")
    _expect(result_schema.get("title"), "OpenADA result v0alpha1", "result_schema.title")

    profile = _contract_document(contracts["oscillator"], label="oscillator profile")
    profile_contract = contracts["oscillator"]
    _expect(
        profile_contract["id"],
        "openada.operation/result.osc.measure/v1alpha1",
        "oscillator contract id",
    )
    _expect(profile["operation"]["id"], profile_contract["id"], "profile.operation.id")
    _expect(
        profile["assertion"]["id"],
        "openada.assertion/oscillator.measurement.valid/v1alpha1",
        "profile.assertion.id",
    )
    _expect([item["id"] for item in profile["features"]], list(FEATURES), "profile.features")
    mappings = profile["native_mappings"]
    _expect(len(mappings), 1, "profile.native_mappings.count")
    mapping = mappings[0]
    _expect(mapping["driver_id"], manifest["implementation"]["id"], "profile.mapping.driver_id")
    _expect(mapping["native_product_id"], "org.openada.core.runtime", "profile.mapping.product")
    _expect(mapping["supported_features"], list(FEATURES), "profile.mapping.features")
    _expect(
        [item["feature_id"] for item in mapping["semantic_bindings"]],
        list(FEATURES),
        "profile.mapping.semantic_bindings",
    )
    return manifest


def _case_ids(cases: dict[str, Any], key: str) -> list[Any]:
    value = cases[key]
    if not isinstance(value, list):
        raise ConformanceError(f"fixture.{key} must be an array")
    return [case.get("id") if isinstance(case, dict) else None for case in value]


def load_cases(manifest: dict[str, Any]) -> dict[str, Any]:
    fixture = manifest["fixture"]
    _expect(
        set(fixture),
        {"schema", "repository_path", "sha256", "license"},
        "manifest.fixture.keys",
    )
    _expect(
        fixture["schema"],
        "openada.oscillator-measure-conformance-cases/v0alpha1",
        "manifest.fixture.schema",
    )
    _expect(fixture["license"], "MIT", "manifest.fixture.license")
    fixture_path = _repository_path(fixture["repository_path"], label="fixture.repository_path")
    _expect(_sha256(fixture_path), fixture["sha256"], "manifest.fixture.sha256")
    cases = _read_json(fixture_path, label="oscillator conformance fixture")
    _expect(
        set(cases),
        {
            "schema",
            "source",
            "composition_series",
            "transient_methods",
            "waveforms",
            "transient_cases",
            "grid_cases",
            "shift_cases",
            "receipt_rejection_cases",
        },
        "fixture.keys",
    )
    _expect(cases["schema"], fixture["schema"], "fixture.schema")
    _expect(_case_ids(cases, "transient_cases"), list(TRANSIENT_CASE_IDS), "fixture.transient ids")
    _expect(_case_ids(cases, "grid_cases"), list(GRID_CASE_IDS), "fixture.grid ids")
    _expect(_case_ids(cases, "shift_cases"), list(SHIFT_CASE_IDS), "fixture.shift ids")
    _expect(
        _case_ids(cases, "receipt_rejection_cases"),
        list(REJECTION_CASE_IDS),
        "fixture.receipt rejection ids",
    )

    all_case_ids = (
        _case_ids(cases, "transient_cases")
        + _case_ids(cases, "grid_cases")
        + _case_ids(cases, "shift_cases")
        + _case_ids(cases, "receipt_rejection_cases")
    )
    if len(all_case_ids) != len(set(all_case_ids)):
        raise ConformanceError("fixture case identifiers must be globally unique")
    covered: set[str] = set()
    for key in ("transient_cases", "grid_cases", "shift_cases", "receipt_rejection_cases"):
        for index, case in enumerate(cases[key]):
            features = case.get("feature_ids")
            if not isinstance(features, list) or not features:
                raise ConformanceError(f"fixture.{key}[{index}].feature_ids must be nonempty")
            if len(features) != len(set(features)) or any(item not in FEATURES for item in features):
                raise ConformanceError(f"fixture.{key}[{index}].feature_ids is invalid")
            covered.update(features)
    _expect(covered, set(FEATURES), "fixture feature coverage")

    _expect(set(cases["transient_methods"]), {"benchmark_late_100_cycles", "diagnostic_short", "composition_short"}, "fixture.transient_methods")
    for name, method in cases["transient_methods"].items():
        _expect(method["kind"], "transient", f"fixture.transient_methods.{name}.kind")
        _expect(method["extensions"], {}, f"fixture.transient_methods.{name}.extensions")
    benchmark_method = cases["transient_methods"]["benchmark_late_100_cycles"]
    _expect(benchmark_method["window"], {"start": {"value": 2.5e-7, "unit": "s"}, "stop": {"value": 3e-7, "unit": "s"}, "cycle_count": 100}, "fixture benchmark window")

    reference = cases["transient_cases"][0]
    _expect(reference["waveform"], "clean_reference", "fixture reference waveform")
    _expect(reference["expected"]["frequency_hz"], 2_416_800_000.0, "fixture reference frequency")
    _expect(reference["expected"]["differential_peak_to_peak_v"], 1.6, "fixture reference amplitude")
    _expect(reference["expected"]["average_supply_power_w"], 0.00084, "fixture reference power")

    benchmark_grid = cases["grid_cases"][0]
    _expect(
        [point["control_v"] for point in benchmark_grid["points"]],
        [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2],
        "fixture benchmark controls",
    )
    _expect(
        [point["frequency_hz"] for point in benchmark_grid["points"]],
        [2.3847e9, 2.3913e9, 2.4014e9, 2.4083e9, 2.4168e9, 2.4270e9, 2.4387e9, 2.4648e9, 2.4906e9],
        "fixture benchmark frequencies",
    )
    expected_gains = [33e6, 41.75e6, 62.83333333333333e6, 77e6, 93.5e6, 109.5e6, 121.5e6, 129.75e6, 129e6]
    for index, (actual, expected) in enumerate(zip(benchmark_grid["expected"]["local_tuning_gain_hz_per_v"], expected_gains)):
        _expect_close(actual, expected, f"fixture benchmark gain[{index}]")
    _expect(benchmark_grid["expected"]["span_hz"], 105_900_000.0, "fixture benchmark span")

    expected_statuses = ["sustained", "not_sustained", "never_started", "collapsed", "multimode"]
    _expect(
        [case["expected"]["oscillator_status"] for case in cases["transient_cases"]],
        expected_statuses,
        "fixture transient statuses",
    )
    return cases


def _normalized_conditions(conditions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in conditions:
        value = item["value"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
        output.append({"name": item["name"], "value": value, "unit": item["unit"]})
    return output


def _series(
    definition: dict[str, Any], source_definition: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        else:
            raise ConformanceError(f"unsupported fixture generator {generator!r}")
        if not math.isfinite(value):
            raise ConformanceError("fixture generator produced a non-finite sample")
        differential.append(value)
    axis = {"name": "time", "unit": "s", "values": axis_values}
    supply_voltage = float(definition["supply_voltage_v"])
    supply_current = float(definition["supply_current_a"])
    signals = [
        {"name": "v(outp)", "unit": "V", "values": [value / 2.0 for value in differential]},
        {"name": "v(outn)", "unit": "V", "values": [-value / 2.0 for value in differential]},
        {"name": "v(vdd)", "unit": "V", "values": [supply_voltage for _ in axis_values]},
        {"name": "i(vdd)", "unit": "A", "values": [supply_current for _ in axis_values]},
    ]
    conditions = _normalized_conditions(definition["conditions"])
    digest = _canonical_sha256({"axis": axis, "signals": signals, "conditions": conditions})
    source = {
        **deepcopy(source_definition),
        "artifact_sha256": digest,
        "series_sha256": digest,
        "conditions_sha256": _canonical_sha256(conditions),
        "conditions": conditions,
    }
    input_series = {
        "source": {**deepcopy(source_definition), "artifact_sha256": digest},
        "axis": axis,
        "signals": signals,
        "conditions": conditions,
        "extensions": {},
    }
    return input_series, source


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


def _interpolate(axis: list[float], values: list[float], at: float) -> float:
    for index, coordinate in enumerate(axis):
        if coordinate == at:
            return values[index]
        if coordinate > at:
            left = index - 1
            return values[left] + (values[index] - values[left]) * (
                (at - axis[left]) / (coordinate - axis[left])
            )
    raise ConformanceError("reference crop boundary lies outside the series")


def _crop(
    axis: list[float], vectors: Sequence[list[float]], start: float, stop: float
) -> tuple[list[float], list[list[float]]]:
    if start < axis[0] or stop > axis[-1] or stop <= start:
        raise ConformanceError("invalid reference crop")
    coordinates = [start]
    coordinates.extend(value for value in axis if start < value < stop)
    coordinates.append(stop)
    return coordinates, [
        [_interpolate(axis, values, coordinate) for coordinate in coordinates]
        for values in vectors
    ]


def _crossings(
    axis: list[float], values: list[float], threshold: float, hysteresis: float
) -> list[float]:
    low = threshold - hysteresis
    high = threshold + hysteresis
    armed = values[0] <= low
    pending: float | None = None
    found: list[float] = []
    for index in range(len(axis) - 1):
        x0, x1 = axis[index], axis[index + 1]
        y0, y1 = values[index], values[index + 1]
        if not armed and pending is None and (y0 <= low or y1 <= low):
            armed = True
        if armed and pending is None and y0 < threshold <= y1 and y1 > y0:
            pending = x0 + (threshold - y0) * (x1 - x0) / (y1 - y0)
        if pending is not None and (y0 >= high or y1 >= high):
            found.append(pending)
            pending = None
            armed = False
    return found


def _relative_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = math.fsum(values) / len(values)
    return max(abs(value - mean) / mean for value in values)


def _cycle_assessment(
    axis: list[float], differential: list[float], crossings: Sequence[float]
) -> tuple[list[float], list[float], list[int]]:
    periods: list[float] = []
    amplitudes: list[float] = []
    sample_counts: list[int] = []
    for left, right in zip(crossings, crossings[1:]):
        cycle_axis, cropped = _crop(axis, [differential], left, right)
        periods.append(right - left)
        amplitudes.append(max(cropped[0]) - min(cropped[0]))
        sample_counts.append(len(cycle_axis))
    return periods, amplitudes, sample_counts


def _startup_candidate(
    axis: list[float],
    differential: list[float],
    crossings: list[float],
    *,
    hold_for: float,
    minimum_amplitude: float,
    maximum_period_deviation: float,
    maximum_amplitude_deviation: float,
    minimum_samples: int,
) -> tuple[float | None, bool]:
    if len(crossings) < 2:
        return None, False
    periods, amplitudes, sample_counts = _cycle_assessment(axis, differential, crossings)
    activity = any(amplitude >= minimum_amplitude for amplitude in amplitudes)
    for start_index in range(len(periods)):
        for stop_index in range(start_index + 3, len(periods) + 1):
            if crossings[stop_index] - crossings[start_index] < hold_for:
                continue
            selected_periods = periods[start_index:stop_index]
            selected_amplitudes = amplitudes[start_index:stop_index]
            selected_samples = sample_counts[start_index:stop_index]
            if (
                min(selected_amplitudes) >= minimum_amplitude
                and min(selected_samples) >= minimum_samples
                and _relative_deviation(selected_periods) <= maximum_period_deviation
                and _relative_deviation(selected_amplitudes) <= maximum_amplitude_deviation
            ):
                return crossings[start_index], activity
            break
    return None, activity


def _metric(status: str, value: float | None, unit: str, window_sha256: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "value": value,
        "unit": unit,
        "window_sha256": window_sha256,
        "extensions": {},
    }


def _reference_transient(
    series: dict[str, Any], source: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    axis = list(series["axis"]["values"])
    by_name = {signal["name"]: list(signal["values"]) for signal in series["signals"]}
    names = request["signals"]
    positive = by_name[names["positive"]]
    negative = by_name[names["negative"]]
    supply_voltage = by_name[names["supply_voltage"]]
    supply_current = by_name[names["supply_current"]]
    start = float(request["window"]["start"]["value"])
    stop = float(request["window"]["stop"]["value"])
    search_start = float(request["startup"]["search_start"]["value"])

    crop_axis, cropped = _crop(
        axis, [positive, negative, supply_voltage, supply_current], start, stop
    )
    crop_positive, crop_negative, crop_voltage, crop_current = cropped
    crop_differential = [left - right for left, right in zip(crop_positive, crop_negative)]
    amplitude_value = max(crop_differential) - min(crop_differential)
    powers = [left * right for left, right in zip(crop_voltage, crop_current)]
    if request["power"]["current_orientation"] == "positive_into_source":
        powers = [-value for value in powers]
    power_value = math.fsum(
        (right_t - left_t) * (left_p + right_p) / 2.0
        for left_t, right_t, left_p, right_p in zip(
            crop_axis, crop_axis[1:], powers, powers[1:]
        )
    ) / (crop_axis[-1] - crop_axis[0])

    search_axis, searched = _crop(axis, [positive, negative], search_start, stop)
    search_differential = [left - right for left, right in zip(searched[0], searched[1])]
    all_crossings = _crossings(
        search_axis,
        search_differential,
        float(request["crossing"]["threshold"]["value"]),
        float(request["crossing"]["hysteresis"]["value"]),
    )
    late_crossings = [value for value in all_crossings if start <= value <= stop]

    window_content = {
        "series_sha256": source["series_sha256"],
        "start": request["window"]["start"],
        "stop": request["window"]["stop"],
        "boundary_policy": "closed-linear-interpolation",
        "signals": request["signals"],
    }
    window_sha256 = _canonical_sha256(window_content)
    window = {
        "start": request["window"]["start"],
        "stop": request["window"]["stop"],
        "cycle_count": request["window"]["cycle_count"],
        "boundary_policy": "closed-linear-interpolation",
        "sample_count": len(crop_axis),
        "series_sha256": source["series_sha256"],
        "window_sha256": window_sha256,
        "signals": request["signals"],
        "extensions": {},
    }
    requested_cycles = int(request["window"]["cycle_count"])
    minimum_amplitude = float(request["startup"]["minimum_peak_to_peak"]["value"])
    maximum_period_deviation = float(request["quality"]["maximum_period_relative_deviation"])
    maximum_amplitude_deviation = float(request["quality"]["maximum_amplitude_relative_deviation"])
    minimum_samples = int(request["quality"]["minimum_samples_per_cycle"])
    started_at, activity = _startup_candidate(
        search_axis,
        search_differential,
        all_crossings,
        hold_for=float(request["startup"]["hold_for"]["value"]),
        minimum_amplitude=minimum_amplitude,
        maximum_period_deviation=maximum_period_deviation,
        maximum_amplitude_deviation=maximum_amplitude_deviation,
        minimum_samples=minimum_samples,
    )

    selected_crossings: list[float] = []
    selected_periods: list[float] = []
    all_late_periods: list[float] = []
    all_late_amplitudes: list[float] = []
    period_deviation: float | None = None
    amplitude_deviation: float | None = None
    observed_minimum_samples: int | None = None
    flags: list[str] = []
    if len(late_crossings) >= requested_cycles + 1:
        selected_crossings = late_crossings[: requested_cycles + 1]
        selected_periods = [right - left for left, right in zip(selected_crossings, selected_crossings[1:])]
        all_late_periods, all_late_amplitudes, late_samples = _cycle_assessment(
            crop_axis, crop_differential, late_crossings
        )
        period_deviation = _relative_deviation(all_late_periods)
        amplitude_deviation = _relative_deviation(all_late_amplitudes)
        observed_minimum_samples = min(late_samples)
        mean_period = math.fsum(all_late_periods) / len(all_late_periods)
        coverage_ok = late_crossings[0] - start <= 1.5 * mean_period and stop - late_crossings[-1] <= 1.5 * mean_period
        if period_deviation > maximum_period_deviation:
            flags.append("period_inconsistent")
        if amplitude_deviation > maximum_amplitude_deviation:
            flags.append("amplitude_inconsistent")
        if observed_minimum_samples < minimum_samples:
            flags.append("sampling_resolution_insufficient")
        if amplitude_value < minimum_amplitude or (
            all_late_amplitudes and min(all_late_amplitudes) < minimum_amplitude
        ):
            flags.append("amplitude_below_minimum")
        if not coverage_ok:
            flags.append("window_not_fully_covered")
        if "sampling_resolution_insufficient" in flags:
            verdict = "unknown"
        elif {"period_inconsistent", "amplitude_inconsistent"} & set(flags):
            verdict = "multimode"
        elif flags:
            verdict = "collapsed" if started_at is not None else "not_sustained"
        else:
            verdict = "sustained"
    elif started_at is not None:
        verdict = "collapsed"
        flags.append("late_crossings_insufficient")
    elif activity:
        verdict = "not_sustained"
        flags.append("startup_hold_not_met")
    else:
        verdict = "never_started"
        flags.append("oscillation_activity_absent")

    frequency_value: float | None = None
    period_value: float | None = None
    if verdict == "sustained":
        elapsed = selected_crossings[-1] - selected_crossings[0]
        frequency_value = requested_cycles / elapsed
        period_value = elapsed / requested_cycles
    metric_status = "measured" if verdict == "sustained" else verdict
    frequency = _metric(metric_status, frequency_value, "Hz", window_sha256)
    period = _metric(metric_status, period_value, "s", window_sha256)
    amplitude = _metric("measured", amplitude_value, "V", window_sha256)
    power = _metric("measured", power_value, "W", window_sha256)
    startup = {
        "status": verdict,
        "started_at": {"value": started_at, "unit": "s"} if started_at is not None else None,
        "time": {"value": started_at - search_start, "unit": "s"} if started_at is not None else None,
        "collapse_at": (
            {"value": all_crossings[-1], "unit": "s"}
            if started_at is not None and verdict == "collapsed" and all_crossings
            else None
        ),
        "search_start": request["startup"]["search_start"],
        "hold_for": request["startup"]["hold_for"],
        "minimum_peak_to_peak": request["startup"]["minimum_peak_to_peak"],
        "extensions": {},
    }
    quality = {
        "status": "pass" if verdict == "sustained" else "unknown" if verdict == "unknown" else "fail",
        "period_relative_deviation": period_deviation,
        "amplitude_relative_deviation": amplitude_deviation,
        "minimum_samples_per_cycle_observed": observed_minimum_samples,
        "maximum_period_relative_deviation": maximum_period_deviation,
        "maximum_amplitude_relative_deviation": maximum_amplitude_deviation,
        "minimum_samples_per_cycle_required": minimum_samples,
        "flags": flags,
        "extensions": {},
    }
    transient = {
        "status": verdict,
        "frequency": frequency,
        "period": period,
        "differential_peak_to_peak": amplitude,
        "average_supply_power": power,
        "startup": startup,
        "quality": quality,
        "crossings": [{"value": value, "unit": "s"} for value in selected_crossings],
        "periods": [{"value": value, "unit": "s"} for value in selected_periods],
        "window": window,
        "extensions": {},
    }
    request_sha256 = _canonical_sha256(request)
    method_sha256 = _canonical_sha256(
        {
            key: request[key]
            for key in ("kind", "signals", "window", "startup", "crossing", "quality", "power")
        }
    )
    receipt_without_hash = {
        "schema": "openada.oscillator-transient-receipt/v1alpha1",
        "measurement_id": request["measurement_id"],
        "status": verdict,
        "request_sha256": request_sha256,
        "method_sha256": method_sha256,
        "series_sha256": source["series_sha256"],
        "window_sha256": window_sha256,
        "source": source,
        "window": window,
        "frequency": frequency,
        "period": period,
        "differential_peak_to_peak": amplitude,
        "average_supply_power": power,
        "startup": startup,
        "quality": quality,
        "extensions": {},
    }
    receipt = {**receipt_without_hash, "sha256": _canonical_sha256(receipt_without_hash)}
    return {
        "status": verdict,
        "request_sha256": request_sha256,
        "frequency_value": frequency_value,
        "period_value": period_value,
        "transient": transient,
        "receipt": receipt,
    }


def _validate_schema(document: dict[str, Any], schema: dict[str, Any], *, label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label}.{location}: {error.message}")


def _diagnostic_code(result: dict[str, Any]) -> str | None:
    return result["diagnostics"][0]["code"] if result["diagnostics"] else None


def _verify_common_result(
    result: dict[str, Any],
    *,
    case: dict[str, Any],
    engineering_status: str,
    execution_status: str,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _validate_schema(result, result_schema, label=location)
    _validate_schema(result["data"], data_schema, label=f"{location}.data")
    _expect(result["operation"], "result.osc.measure", f"{location}.operation")
    _expect(result["tool"], None, f"{location}.tool")
    _expect(result["inputs"], [], f"{location}.inputs")
    _expect(result["artifacts"], [], f"{location}.artifacts")
    _expect(result["engineering"]["status"], engineering_status, f"{location}.engineering.status")
    _expect(result["execution"]["status"], execution_status, f"{location}.execution.status")
    _expect(result["execution"]["duration_ms"], 0, f"{location}.execution.duration_ms")
    _expect(result["execution"]["command"], [], f"{location}.execution.command")
    _expect(result["execution"]["exit_code"], 0 if execution_status == "completed" else None, f"{location}.execution.exit_code")
    _expect(
        result["data"]["protocol"],
        {
            "request_id": case["request_id"],
            "operation_profile": "openada.operation/result.osc.measure/v1alpha1",
            "assertion_profile": "openada.assertion/oscillator.measurement.valid/v1alpha1",
            "implementation_id": "org.openada.kernel.oscillator-evidence",
            "implementation_version": "1.0.0",
        },
        f"{location}.data.protocol",
    )


def _verify_receipt(actual: dict[str, Any], expected: dict[str, Any], location: str) -> None:
    if not isinstance(actual, dict):
        raise ConformanceError(f"{location} must be an object")
    supplied = actual.get("sha256")
    content = {key: value for key, value in actual.items() if key != "sha256"}
    _expect(supplied, _canonical_sha256(content), f"{location}.sha256")
    _expect(supplied, expected["sha256"], f"{location}.expected_sha256")
    _expect(actual, expected, location)


def _verify_metric(actual: dict[str, Any], expected: dict[str, Any], location: str) -> None:
    _expect(set(actual), set(expected), f"{location}.keys")
    for key in ("status", "unit", "window_sha256", "extensions"):
        _expect(actual[key], expected[key], f"{location}.{key}")
    _expect_close(actual["value"], expected["value"], f"{location}.value")


def _verify_transient_record(
    record: dict[str, Any],
    case: dict[str, Any],
    cases: dict[str, Any],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(set(record), {"id", "feature_ids", "request_sha256", "series_sha256", "result"}, f"{location}.keys")
    _expect(record["id"], case["id"], f"{location}.id")
    _expect(record["feature_ids"], case["feature_ids"], f"{location}.feature_ids")
    series, source = _series(cases["waveforms"][case["waveform"]], cases["source"])
    method = cases["transient_methods"][case["method"]]
    reference = _reference_transient(series, source, method)
    request = {"series": series, "measurement": method, "extensions": {}}
    _expect(record["request_sha256"], _canonical_sha256(request), f"{location}.request_sha256")
    _expect(record["series_sha256"], source["series_sha256"], f"{location}.series_sha256")
    expected = case["expected"]
    _expect(reference["status"], expected["oscillator_status"], f"{location}.fixture status")
    _expect_close(reference["frequency_value"], expected["frequency_hz"], f"{location}.fixture frequency")
    _expect_close(reference["period_value"], expected["period_s"], f"{location}.fixture period")

    result = record["result"]
    _verify_common_result(
        result,
        case=case,
        engineering_status=expected["engineering_status"],
        execution_status="completed",
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    data = result["data"]
    _expect(data["grid"], None, f"{location}.grid")
    _expect(data["shift"], None, f"{location}.shift")
    measurement = data["measurement"]
    _expect(measurement["measurement_id"], method["measurement_id"], f"{location}.measurement_id")
    _expect(measurement["kind"], "transient", f"{location}.kind")
    _expect(measurement["status"], reference["status"], f"{location}.status")
    _expect(measurement["request_sha256"], reference["request_sha256"], f"{location}.measurement.request_sha256")
    _expect(measurement["algorithm"], {"id": "openada.method/oscillator-transient-hysteretic/v1alpha1", "version": "1.0.0"}, f"{location}.algorithm")
    _expect(measurement["source_count"], 1, f"{location}.source_count")
    transient = data["transient"]
    _expect(transient["status"], reference["status"], f"{location}.transient.status")
    _verify_metric(transient["frequency"], reference["transient"]["frequency"], f"{location}.transient.frequency")
    _verify_metric(transient["period"], reference["transient"]["period"], f"{location}.transient.period")
    _verify_metric(transient["differential_peak_to_peak"], reference["transient"]["differential_peak_to_peak"], f"{location}.transient.differential_peak_to_peak")
    _verify_metric(transient["average_supply_power"], reference["transient"]["average_supply_power"], f"{location}.transient.average_supply_power")
    shared = transient["window"]["window_sha256"]
    _expect(shared, reference["transient"]["window"]["window_sha256"], f"{location}.transient.window.window_sha256")
    for name in ("frequency", "period", "differential_peak_to_peak", "average_supply_power"):
        _expect(transient[name]["window_sha256"], shared, f"{location}.transient.{name}.window_sha256")
    _expect(transient["window"], reference["transient"]["window"], f"{location}.transient.window")
    _expect(transient["startup"], reference["transient"]["startup"], f"{location}.transient.startup")
    _expect(transient["quality"], reference["transient"]["quality"], f"{location}.transient.quality")
    _expect(transient["crossings"], reference["transient"]["crossings"], f"{location}.transient.crossings")
    _expect(transient["periods"], reference["transient"]["periods"], f"{location}.transient.periods")
    _verify_receipt(data["receipt"], reference["receipt"], f"{location}.receipt")
    _expect(data["receipt"]["window_sha256"], shared, f"{location}.receipt.window_sha256")
    if case["id"] == "clean-sustained-reference":
        _expect(len(transient["crossings"]), 101, f"{location}.crossing_count")
        _expect(len(transient["periods"]), 100, f"{location}.period_count")
        _expect_close(transient["differential_peak_to_peak"]["value"], expected["differential_peak_to_peak_v"], f"{location}.amplitude")
        _expect_close(transient["average_supply_power"]["value"], expected["average_supply_power_w"], f"{location}.power")


def _composition_receipt(
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
    series, source = _series(definition, cases["source"])
    return _reference_transient(
        series, source, cases["transient_methods"][method_name]
    )["receipt"]


def _grid_expected_receipts(cases: dict[str, Any], case: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    base = deepcopy(cases["composition_series"]["base_conditions"])
    for point in case["points"]:
        conditions = base + [
            {"name": "vctrl", "value": point["control_v"], "unit": "V"},
            {"name": "vdd", "value": 1.2, "unit": "V"},
        ]
        output.append(
            _composition_receipt(
                cases,
                method_name=case["method"],
                frequency_hz=point["frequency_hz"],
                generator=point["generator"],
                conditions=conditions,
            )
        )
    return output


def _grid_request(case: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **deepcopy(case["measurement"]),
        "points": [
            {"control": {"value": point["control_v"], "unit": "V"}, "receipt": receipt}
            for point, receipt in zip(case["points"], receipts)
        ],
    }


def _local_gains(controls: Sequence[float], frequencies: Sequence[float]) -> list[float]:
    output = [(frequencies[1] - frequencies[0]) / (controls[1] - controls[0])]
    for index in range(1, len(controls) - 1):
        h0 = controls[index] - controls[index - 1]
        h1 = controls[index + 1] - controls[index]
        output.append(
            -h1 / (h0 * (h0 + h1)) * frequencies[index - 1]
            + (h1 - h0) / (h0 * h1) * frequencies[index]
            + h0 / (h1 * (h0 + h1)) * frequencies[index + 1]
        )
    output.append((frequencies[-1] - frequencies[-2]) / (controls[-1] - controls[-2]))
    return output


def _verify_grid_result(
    result: dict[str, Any],
    case: dict[str, Any],
    receipts: list[dict[str, Any]],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    expected = case["expected"]
    _verify_common_result(
        result,
        case=case,
        engineering_status=expected["engineering_status"],
        execution_status="completed",
        result_schema=result_schema,
        data_schema=data_schema,
        location=location,
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    data = result["data"]
    _expect(data["transient"], None, f"{location}.transient")
    _expect(data["shift"], None, f"{location}.shift")
    _expect(data["receipt"], None, f"{location}.receipt")
    request = _grid_request(case, receipts)
    measurement = data["measurement"]
    _expect(measurement["measurement_id"], case["measurement"]["measurement_id"], f"{location}.measurement_id")
    _expect(measurement["kind"], "tuning_grid", f"{location}.kind")
    _expect(measurement["status"], expected["grid_status"], f"{location}.measurement.status")
    _expect(measurement["request_sha256"], _canonical_sha256(request), f"{location}.measurement.request_sha256")
    _expect(measurement["algorithm"], {"id": "openada.method/oscillator-local-tuning-gain/v1alpha1", "version": "1.0.0"}, f"{location}.algorithm")
    _expect(measurement["source_count"], len(receipts), f"{location}.source_count")
    grid = data["grid"]
    _expect(grid["status"], expected["grid_status"], f"{location}.grid.status")
    _expect(grid["control_condition"], "vctrl", f"{location}.grid.control_condition")
    _expect(grid["control_unit"], "V", f"{location}.grid.control_unit")
    _expect(grid["expected_monotonicity"], case["measurement"]["expected_monotonicity"], f"{location}.grid.expected_monotonicity")
    _expect(grid["observed_monotonicity"], expected["observed_monotonicity"], f"{location}.grid.observed_monotonicity")
    _expect(grid["monotonicity_check"], expected["monotonicity_check"], f"{location}.grid.monotonicity_check")
    controls = [float(point["control_v"]) for point in case["points"]]
    frequencies = [receipt["frequency"]["value"] for receipt in receipts]
    grid_identity = {
        "control_condition": "vctrl",
        "control_unit": "V",
        "controls": controls,
        "receipt_sha256": [receipt["sha256"] for receipt in receipts],
    }
    _expect(grid["grid_sha256"], _canonical_sha256(grid_identity), f"{location}.grid.grid_sha256")
    _expect(len(grid["points"]), len(receipts), f"{location}.grid.points.count")
    complete = all(receipt["status"] == "sustained" for receipt in receipts)
    calculated_gains = _local_gains(controls, [float(value) for value in frequencies]) if complete else [None] * len(receipts)
    for index, (actual, point, receipt, calculated, declared) in enumerate(
        zip(grid["points"], case["points"], receipts, calculated_gains, expected["local_tuning_gain_hz_per_v"])
    ):
        _expect(actual["control"], {"value": point["control_v"], "unit": "V"}, f"{location}.grid.points[{index}].control")
        expected_frequency_status = "measured" if receipt["status"] == "sustained" else receipt["status"]
        _expect(actual["frequency"]["status"], expected_frequency_status, f"{location}.grid.points[{index}].frequency.status")
        _expect_close(actual["frequency"]["value"], receipt["frequency"]["value"], f"{location}.grid.points[{index}].frequency.value")
        _expect(actual["frequency"]["unit"], "Hz", f"{location}.grid.points[{index}].frequency.unit")
        _expect(actual["local_tuning_gain"]["status"], "measured" if complete else "unknown", f"{location}.grid.points[{index}].gain.status")
        _expect_close(actual["local_tuning_gain"]["value"], calculated, f"{location}.grid.points[{index}].gain.value")
        _expect_close(actual["local_tuning_gain"]["value"], declared, f"{location}.grid.points[{index}].declared_gain")
        _expect(actual["local_tuning_gain"]["unit"], "Hz/V", f"{location}.grid.points[{index}].gain.unit")
        stencil = "forward" if index == 0 else "backward" if index == len(receipts) - 1 else "central_nonuniform_quadratic"
        _expect(actual["stencil"], stencil, f"{location}.grid.points[{index}].stencil")
        _expect(actual["receipt_sha256"], receipt["sha256"], f"{location}.grid.points[{index}].receipt_sha256")
    expected_span = max(float(value) for value in frequencies) - min(float(value) for value in frequencies) if complete else None
    _expect_close(grid["span"]["value"], expected_span, f"{location}.grid.span.value")
    _expect_close(grid["span"]["value"], expected["span_hz"], f"{location}.grid.declared_span")
    _expect(grid["span"]["status"], "measured" if complete else "unknown", f"{location}.grid.span.status")
    _expect(grid["span"]["unit"], "Hz", f"{location}.grid.span.unit")
    _expect(grid["span"]["window_sha256"], None, f"{location}.grid.span.window_sha256")


def _verify_grid_record(
    record: dict[str, Any],
    case: dict[str, Any],
    cases: dict[str, Any],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(set(record), {"id", "feature_ids", "request_sha256", "receipts", "result"}, f"{location}.keys")
    _expect(record["id"], case["id"], f"{location}.id")
    _expect(record["feature_ids"], case["feature_ids"], f"{location}.feature_ids")
    expected_receipts = _grid_expected_receipts(cases, case)
    _expect(len(record["receipts"]), len(expected_receipts), f"{location}.receipts.count")
    for index, (actual, expected) in enumerate(zip(record["receipts"], expected_receipts)):
        _verify_receipt(actual, expected, f"{location}.receipts[{index}]")
    request = {"measurement": _grid_request(case, expected_receipts), "extensions": {}}
    _expect(record["request_sha256"], _canonical_sha256(request), f"{location}.request_sha256")
    _verify_grid_result(
        record["result"],
        case,
        expected_receipts,
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )


def _shift_expected_receipts(
    cases: dict[str, Any], case: dict[str, Any]
) -> list[dict[str, Any]]:
    condition_name = case["measurement"]["perturbation_condition"]
    output: list[dict[str, Any]] = []
    for member_name in ("reference", "perturbed"):
        member = case[member_name]
        conditions = deepcopy(cases["composition_series"]["base_conditions"])
        conditions.append({"name": "vctrl", "value": 0.81, "unit": "V"})
        if condition_name != "vdd":
            conditions.append({"name": "vdd", "value": 1.2, "unit": "V"})
        conditions.append(
            {
                "name": condition_name,
                "value": member["condition_value"],
                "unit": "V" if condition_name == "vdd" else "1",
            }
        )
        output.append(
            _composition_receipt(
                cases,
                method_name=case["method"],
                frequency_hz=member["frequency_hz"],
                generator=member["generator"],
                conditions=conditions,
            )
        )
    return output


def _shift_request(case: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    unit = "V" if case["measurement"]["perturbation_condition"] == "vdd" else "1"
    return {
        **deepcopy(case["measurement"]),
        "reference": {
            "condition": {"value": case["reference"]["condition_value"], "unit": unit},
            "receipt": receipts[0],
        },
        "perturbed": {
            "condition": {"value": case["perturbed"]["condition_value"], "unit": unit},
            "receipt": receipts[1],
        },
    }


def _verify_shift_record(
    record: dict[str, Any],
    case: dict[str, Any],
    cases: dict[str, Any],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(set(record), {"id", "feature_ids", "request_sha256", "receipts", "result"}, f"{location}.keys")
    _expect(record["id"], case["id"], f"{location}.id")
    _expect(record["feature_ids"], case["feature_ids"], f"{location}.feature_ids")
    receipts = _shift_expected_receipts(cases, case)
    _expect(len(record["receipts"]), 2, f"{location}.receipts.count")
    for index, (actual, expected_receipt) in enumerate(zip(record["receipts"], receipts)):
        _verify_receipt(actual, expected_receipt, f"{location}.receipts[{index}]")
    measurement_request = _shift_request(case, receipts)
    _expect(record["request_sha256"], _canonical_sha256({"measurement": measurement_request, "extensions": {}}), f"{location}.request_sha256")
    expected = case["expected"]
    result = record["result"]
    _verify_common_result(
        result,
        case=case,
        engineering_status=expected["engineering_status"],
        execution_status="completed",
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    data = result["data"]
    _expect(data["transient"], None, f"{location}.transient")
    _expect(data["grid"], None, f"{location}.grid")
    _expect(data["receipt"], None, f"{location}.receipt")
    measurement = data["measurement"]
    _expect(measurement["measurement_id"], case["measurement"]["measurement_id"], f"{location}.measurement_id")
    _expect(measurement["kind"], "frequency_shift", f"{location}.kind")
    _expect(measurement["status"], expected["shift_status"], f"{location}.measurement.status")
    _expect(measurement["request_sha256"], _canonical_sha256(measurement_request), f"{location}.measurement.request_sha256")
    _expect(measurement["algorithm"], {"id": "openada.method/oscillator-frequency-shift/v1alpha1", "version": "1.0.0"}, f"{location}.algorithm")
    _expect(measurement["source_count"], 2, f"{location}.source_count")
    shift = data["shift"]
    _expect(shift["status"], expected["shift_status"], f"{location}.shift.status")
    condition_name = case["measurement"]["perturbation_condition"]
    unit = "V" if condition_name == "vdd" else "1"
    reference_condition = {"value": case["reference"]["condition_value"], "unit": unit}
    perturbed_condition = {"value": case["perturbed"]["condition_value"], "unit": unit}
    _expect(shift["perturbation_condition"], condition_name, f"{location}.shift.perturbation_condition")
    _expect(shift["condition_unit"], unit, f"{location}.shift.condition_unit")
    _expect(shift["reference_condition"], reference_condition, f"{location}.shift.reference_condition")
    _expect(shift["perturbed_condition"], perturbed_condition, f"{location}.shift.perturbed_condition")
    _expect(shift["reference_receipt_sha256"], receipts[0]["sha256"], f"{location}.shift.reference_receipt_sha256")
    _expect(shift["perturbed_receipt_sha256"], receipts[1]["sha256"], f"{location}.shift.perturbed_receipt_sha256")
    pair_identity = {
        "perturbation_condition": condition_name,
        "reference_condition": reference_condition,
        "perturbed_condition": perturbed_condition,
        "reference_receipt_sha256": receipts[0]["sha256"],
        "perturbed_receipt_sha256": receipts[1]["sha256"],
    }
    _expect(shift["pair_sha256"], _canonical_sha256(pair_identity), f"{location}.shift.pair_sha256")
    complete = all(receipt["status"] == "sustained" for receipt in receipts)
    signed = float(receipts[1]["frequency"]["value"]) - float(receipts[0]["frequency"]["value"]) if complete else None
    _expect_close(shift["signed_shift"]["value"], signed, f"{location}.shift.signed_shift.value")
    _expect_close(shift["signed_shift"]["value"], expected["signed_shift_hz"], f"{location}.shift.declared_signed")
    _expect_close(shift["absolute_shift"]["value"], abs(signed) if signed is not None else None, f"{location}.shift.absolute_shift.value")
    _expect_close(shift["absolute_shift"]["value"], expected["absolute_shift_hz"], f"{location}.shift.declared_absolute")
    for name in ("signed_shift", "absolute_shift"):
        _expect(shift[name]["status"], "measured" if complete else "unknown", f"{location}.shift.{name}.status")
        _expect(shift[name]["unit"], "Hz", f"{location}.shift.{name}.unit")
        _expect(shift[name]["window_sha256"], None, f"{location}.shift.{name}.window_sha256")


def _verify_rejection_record(
    record: dict[str, Any],
    case: dict[str, Any],
    cases: dict[str, Any],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(
        set(record),
        {"id", "feature_ids", "mutation", "request_sha256", "original_receipt_sha256", "receipts", "result"},
        f"{location}.keys",
    )
    _expect(record["id"], case["id"], f"{location}.id")
    _expect(record["feature_ids"], case["feature_ids"], f"{location}.feature_ids")
    _expect(record["mutation"], "increment-frequency-value-without-rehash", f"{location}.mutation")
    originals = _grid_expected_receipts(cases, case)
    _expect(record["original_receipt_sha256"], [item["sha256"] for item in originals], f"{location}.original_receipt_sha256")
    expected_mutated = deepcopy(originals)
    expected_mutated[0]["frequency"]["value"] += 1.0
    _expect(record["receipts"], expected_mutated, f"{location}.receipts")
    if _canonical_sha256({key: value for key, value in record["receipts"][0].items() if key != "sha256"}) == record["receipts"][0]["sha256"]:
        raise ConformanceError(f"{location}.receipts[0] is not actually digest-invalid")
    measurement = _grid_request(case, expected_mutated)
    _expect(record["request_sha256"], _canonical_sha256({"measurement": measurement, "extensions": {}}), f"{location}.request_sha256")
    expected = case["expected"]
    result = record["result"]
    _verify_common_result(
        result,
        case=case,
        engineering_status=expected["engineering_status"],
        execution_status=expected["execution_status"],
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    data = result["data"]
    _expect(data["measurement"]["measurement_id"], case["measurement"]["measurement_id"], f"{location}.measurement_id")
    _expect(data["measurement"]["kind"], "tuning_grid", f"{location}.kind")
    _expect(data["measurement"]["status"], expected["measurement_status"], f"{location}.measurement.status")
    _expect(data["measurement"]["request_sha256"], None, f"{location}.measurement.request_sha256")
    for key in ("transient", "grid", "shift", "receipt"):
        _expect(data[key], None, f"{location}.{key}")


def verify_evidence(path: Path, *, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(manifest)
    evidence = _read_json(path.resolve(), label="oscillator conformance evidence")
    _expect(
        set(evidence),
        {"schema", "conformance_id", "implementation", "fixture_sha256", "transients", "grids", "shifts", "receipt_rejections"},
        "evidence.keys",
    )
    _expect(evidence["schema"], "openada.oscillator-measure-conformance-run/v0alpha1", "evidence.schema")
    _expect(evidence["conformance_id"], manifest["id"], "evidence.conformance_id")
    _expect(evidence["implementation"], {"id": manifest["implementation"]["id"], "version": manifest["implementation"]["version"]}, "evidence.implementation")
    _expect(evidence["fixture_sha256"], manifest["fixture"]["sha256"], "evidence.fixture_sha256")

    contracts = manifest["contracts"]
    result_schema = _contract_document(contracts["result_schema"], label="result schema")
    profile = _contract_document(contracts["oscillator"], label="oscillator profile")
    data_schema = profile["normalized_result"]["data_schema"]

    groups = (
        ("transients", "transient_cases", _verify_transient_record),
        ("grids", "grid_cases", _verify_grid_record),
        ("shifts", "shift_cases", _verify_shift_record),
        ("receipt_rejections", "receipt_rejection_cases", _verify_rejection_record),
    )
    for evidence_key, case_key, verifier in groups:
        records = evidence[evidence_key]
        expected_cases = cases[case_key]
        if not isinstance(records, list):
            raise ConformanceError(f"evidence.{evidence_key} must be an array")
        _expect([record.get("id") for record in records], [case["id"] for case in expected_cases], f"evidence.{evidence_key}.case_ids")
        for index, (record, case) in enumerate(zip(records, expected_cases)):
            verifier(
                record,
                case,
                cases,
                result_schema=result_schema,
                data_schema=data_schema,
                location=f"evidence.{evidence_key}[{index}]",
            )

    return {
        "schema": "openada.oscillator-measure-conformance-verification/v0alpha1",
        "status": "pass",
        "conformance_id": manifest["id"],
        "implementation": {
            "id": manifest["implementation"]["id"],
            "version": manifest["implementation"]["version"],
        },
        "features": list(FEATURES),
        "verified_cases": {
            "transient": len(cases["transient_cases"]),
            "grid": len(cases["grid_cases"]),
            "shift": len(cases["shift_cases"]),
            "receipt_rejection": len(cases["receipt_rejection_cases"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args(argv)
    try:
        verification = verify_evidence(
            arguments.evidence_file,
            manifest_path=arguments.manifest.resolve(),
        )
    except ConformanceError as exc:
        print(f"oscillator conformance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
