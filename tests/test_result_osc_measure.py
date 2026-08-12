from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.operations.result_measure import normalized_series_sha256
from openada.operations.result_osc_measure import (
    _average_power,
    _hysteretic_crossings,
    _startup_candidate,
    measure_oscillator,
    oscillator_receipt_sha256,
)


ROOT = Path(__file__).parents[1]
RESULT_SCHEMA = json.loads(
    (ROOT / "schemas" / "result-v0alpha1.schema.json").read_text(encoding="utf-8")
)
RESULT_VALIDATOR = Draft202012Validator(RESULT_SCHEMA, format_checker=FormatChecker())
OSCILLATOR_PROFILE = json.loads(
    (ROOT / "profiles" / "result.osc.measure-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
OSCILLATOR_DATA_VALIDATOR = Draft202012Validator(
    OSCILLATOR_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)

REFERENCE_FREQUENCY_HZ = 2.4168e9
REFERENCE_WINDOW_START_S = 250e-9
REFERENCE_WINDOW_STOP_S = 300e-9


def _time_grid(
    frequency_hz: float,
    stop_s: float,
    *,
    samples_per_cycle: int = 8,
    extra_times: list[float] | None = None,
) -> list[float]:
    step = 1.0 / (frequency_hz * samples_per_cycle)
    count = math.ceil(stop_s / step)
    values = [index * step for index in range(count + 1)]
    if extra_times:
        values.extend(extra_times)
    return sorted(set(values))


def _series(
    differential: Callable[[float], float],
    *,
    frequency_hz: float = REFERENCE_FREQUENCY_HZ,
    stop_s: float = REFERENCE_WINDOW_STOP_S,
    samples_per_cycle: int = 8,
    extra_times: list[float] | None = None,
    supply_voltage: Callable[[float], float] = lambda _time: 1.2,
    supply_current: Callable[[float], float] = lambda _time: 700e-6,
    conditions: list[dict] | None = None,
) -> dict:
    axis_values = _time_grid(
        frequency_hz,
        stop_s,
        samples_per_cycle=samples_per_cycle,
        extra_times=extra_times,
    )
    differential_values = [differential(time) for time in axis_values]
    axis = {"name": "time", "unit": "s", "values": axis_values}
    signals = [
        {
            "name": "v(outp)",
            "unit": "V",
            "values": [value / 2.0 for value in differential_values],
        },
        {
            "name": "v(outn)",
            "unit": "V",
            "values": [-value / 2.0 for value in differential_values],
        },
        {
            "name": "v(vdd)",
            "unit": "V",
            "values": [supply_voltage(time) for time in axis_values],
        },
        {
            "name": "i(vdd)",
            "unit": "A",
            "values": [supply_current(time) for time in axis_values],
        },
    ]
    if conditions is None:
        conditions = [
            {"name": "temperature", "value": 27.0, "unit": "degC"},
            {"name": "corner", "value": "tt", "unit": "1"},
        ]
    digest = normalized_series_sha256(
        axis=axis,
        signals=signals,
        conditions=conditions,
    )
    return {
        "source": {
            "operation": "result.series.extract",
            "request_id": "11111111-1111-4111-8111-111111111111",
            "artifact_role": "measurement.source",
            "artifact_sha256": digest,
            "lineage": {
                "operation": "circuit.simulate",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "artifact_role": "simulation.result",
                "artifact_sha256": "a" * 64,
                "binding": "unverified",
            },
        },
        "axis": axis,
        "signals": signals,
        "conditions": conditions,
        "extensions": {},
    }


def _sine(frequency_hz: float, amplitude: float = 0.8) -> Callable[[float], float]:
    return lambda time: amplitude * math.sin(2.0 * math.pi * frequency_hz * time)


def _request(
    *,
    window_start_s: float = REFERENCE_WINDOW_START_S,
    window_stop_s: float = REFERENCE_WINDOW_STOP_S,
    cycle_count: int = 100,
    search_start_s: float = 0.0,
    hold_for_s: float = 5e-9,
    minimum_peak_to_peak_v: float = 0.8,
    hysteresis_v: float = 0.1,
    maximum_period_relative_deviation: float = 2e-3,
    maximum_amplitude_relative_deviation: float = 2e-2,
    minimum_samples_per_cycle: int = 6,
    current_orientation: str = "positive_into_load",
) -> dict:
    return {
        "measurement_id": "vco.transient",
        "kind": "transient",
        "signals": {
            "positive": "v(outp)",
            "negative": "v(outn)",
            "supply_voltage": "v(vdd)",
            "supply_current": "i(vdd)",
        },
        "window": {
            "start": {"value": window_start_s, "unit": "s"},
            "stop": {"value": window_stop_s, "unit": "s"},
            "cycle_count": cycle_count,
        },
        "startup": {
            "search_start": {"value": search_start_s, "unit": "s"},
            "hold_for": {"value": hold_for_s, "unit": "s"},
            "minimum_peak_to_peak": {
                "value": minimum_peak_to_peak_v,
                "unit": "V",
            },
        },
        "crossing": {
            "threshold": {"value": 0.0, "unit": "V"},
            "hysteresis": {"value": hysteresis_v, "unit": "V"},
            "direction": "rising",
        },
        "quality": {
            "maximum_period_relative_deviation": maximum_period_relative_deviation,
            "maximum_amplitude_relative_deviation": maximum_amplitude_relative_deviation,
            "minimum_samples_per_cycle": minimum_samples_per_cycle,
        },
        "power": {"current_orientation": current_orientation},
        "extensions": {},
    }


def _short_request(**overrides) -> dict:
    defaults = {
        "window_start_s": 20e-9,
        "window_stop_s": 60e-9,
        "cycle_count": 50,
        "hold_for_s": 3e-9,
    }
    defaults.update(overrides)
    return _request(**defaults)


def _rehash(series: dict) -> None:
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"],
        signals=series["signals"],
        conditions=series["conditions"],
    )


def _assert_envelope(payload: dict) -> None:
    errors = sorted(RESULT_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)
    data_errors = sorted(
        OSCILLATOR_DATA_VALIDATOR.iter_errors(payload["data"]),
        key=lambda item: list(item.path),
    )
    assert not data_errors, "\n".join(error.message for error in data_errors)
    json.dumps(payload, allow_nan=False)


def _conditions(
    *,
    control_v: float | None = None,
    vdd_v: float | None = None,
    temperature_c: float = 27.0,
) -> list[dict]:
    result = [
        {"name": "temperature", "value": temperature_c, "unit": "degC"},
        {"name": "corner", "value": "tt", "unit": "1"},
    ]
    if control_v is not None:
        result.append({"name": "vctrl", "value": control_v, "unit": "V"})
    if vdd_v is not None:
        result.append({"name": "vdd", "value": vdd_v, "unit": "V"})
    return result


def _receipt_for(
    frequency_hz: float,
    *,
    conditions: list[dict],
) -> dict:
    series = _series(
        _sine(frequency_hz),
        frequency_hz=frequency_hz,
        stop_s=12e-9,
        conditions=conditions,
    )
    request = _request(
        window_start_s=2e-9,
        window_stop_s=12e-9,
        cycle_count=10,
        hold_for_s=1.5e-9,
    )
    payload = measure_oscillator(series, request)
    assert payload["engineering"]["status"] == "pass", payload
    return payload["data"]["receipt"]


def _flat_receipt(*, conditions: list[dict]) -> dict:
    series = _series(
        lambda _time: 0.0,
        stop_s=12e-9,
        conditions=conditions,
    )
    payload = measure_oscillator(
        series,
        _request(
            window_start_s=2e-9,
            window_stop_s=12e-9,
            cycle_count=10,
            hold_for_s=1.5e-9,
        ),
    )
    assert payload["data"]["receipt"]["status"] == "never_started"
    return payload["data"]["receipt"]


def test_reference_24168_ghz_uses_100_late_cycles_and_one_provenance_window() -> None:
    series = _series(_sine(REFERENCE_FREQUENCY_HZ))

    payload = measure_oscillator(
        series,
        _request(),
        request_id="10000000-0000-4000-8000-000000000001",
    )

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurement"] == {
        "measurement_id": "vco.transient",
        "kind": "transient",
        "status": "sustained",
        "request_sha256": payload["data"]["measurement"]["request_sha256"],
        "algorithm": {
            "id": "openada.method/oscillator-transient-hysteretic/v1alpha1",
            "version": "1.0.0",
        },
        "source_count": 1,
        "extensions": {},
    }
    transient = payload["data"]["transient"]
    assert transient["status"] == "sustained"
    assert transient["frequency"]["status"] == "measured"
    assert transient["frequency"]["value"] == pytest.approx(
        REFERENCE_FREQUENCY_HZ, rel=2e-10
    )
    assert transient["period"]["value"] == pytest.approx(
        1.0 / REFERENCE_FREQUENCY_HZ, rel=2e-10
    )
    assert len(transient["crossings"]) == 101
    assert len(transient["periods"]) == 100
    assert transient["differential_peak_to_peak"]["value"] == pytest.approx(1.6)
    assert transient["average_supply_power"]["value"] == pytest.approx(840e-6)
    assert transient["startup"]["status"] == "sustained"
    assert transient["startup"]["started_at"]["value"] < 2e-9
    assert transient["quality"]["status"] == "pass"
    assert transient["window"]["start"] == {
        "value": REFERENCE_WINDOW_START_S,
        "unit": "s",
    }
    assert transient["window"]["stop"] == {
        "value": REFERENCE_WINDOW_STOP_S,
        "unit": "s",
    }

    receipt = payload["data"]["receipt"]
    window_sha256 = transient["window"]["window_sha256"]
    assert receipt["sha256"] == oscillator_receipt_sha256(receipt)
    assert receipt["series_sha256"] == series["source"]["artifact_sha256"]
    assert receipt["window_sha256"] == window_sha256
    assert receipt["window"]["window_sha256"] == window_sha256
    for name in (
        "frequency",
        "period",
        "differential_peak_to_peak",
        "average_supply_power",
    ):
        assert transient[name]["window_sha256"] == window_sha256
        assert receipt[name]["window_sha256"] == window_sha256


def test_flat_waveform_is_typed_never_started_not_a_numeric_frequency() -> None:
    payload = measure_oscillator(
        _series(lambda _time: 0.0, stop_s=60e-9),
        _short_request(),
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["measurement"]["status"] == "never_started"
    transient = payload["data"]["transient"]
    assert transient["status"] == "never_started"
    assert transient["startup"]["started_at"] is None
    assert transient["frequency"] == {
        "status": "never_started",
        "value": None,
        "unit": "Hz",
        "window_sha256": transient["window"]["window_sha256"],
        "extensions": {},
    }
    assert payload["diagnostics"][0]["code"] == "oscillator.never_started"


def test_decaying_startup_ringing_is_not_sustained() -> None:
    frequency = REFERENCE_FREQUENCY_HZ

    def decaying(time: float) -> float:
        return 0.8 * math.exp(-time / 1e-9) * math.sin(2.0 * math.pi * frequency * time)

    payload = measure_oscillator(
        _series(decaying, stop_s=60e-9),
        _short_request(hold_for_s=5e-9, minimum_peak_to_peak_v=0.4),
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "fail"
    transient = payload["data"]["transient"]
    assert transient["status"] == "not_sustained"
    assert transient["startup"]["started_at"] is None
    assert transient["frequency"]["status"] == "not_sustained"
    assert transient["frequency"]["value"] is None
    assert "startup_hold_not_met" in transient["quality"]["flags"]


def test_started_then_collapsed_is_distinct_from_never_started() -> None:
    frequency = REFERENCE_FREQUENCY_HZ

    def collapsing(time: float) -> float:
        envelope = 0.8 if time <= 40e-9 else 0.8 * math.exp(-(time - 40e-9) / 0.5e-9)
        return envelope * math.sin(2.0 * math.pi * frequency * time)

    payload = measure_oscillator(
        _series(collapsing, stop_s=60e-9),
        _short_request(cycle_count=60),
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "fail"
    transient = payload["data"]["transient"]
    assert transient["status"] == "collapsed"
    assert transient["startup"]["started_at"] is not None
    assert transient["startup"]["collapse_at"] is not None
    assert transient["frequency"]["status"] == "collapsed"
    assert transient["frequency"]["value"] is None
    assert "late_crossings_insufficient" in transient["quality"]["flags"]


def test_two_tone_beating_is_multimode_qc_and_frequency_is_withheld() -> None:
    primary = REFERENCE_FREQUENCY_HZ
    secondary = 2.31e9

    def two_tone(time: float) -> float:
        primary_tone = 0.55 * math.sin(2.0 * math.pi * primary * time)
        secondary_tone = (
            0.25 * math.sin(2.0 * math.pi * secondary * time)
            if time >= 10e-9
            else 0.0
        )
        return primary_tone + secondary_tone

    payload = measure_oscillator(
        _series(
            two_tone,
            frequency_hz=primary,
            stop_s=60e-9,
            samples_per_cycle=24,
        ),
        _short_request(
            cycle_count=80,
            minimum_peak_to_peak_v=0.2,
            hysteresis_v=0.03,
            maximum_period_relative_deviation=1e-4,
            maximum_amplitude_relative_deviation=1e-3,
            minimum_samples_per_cycle=12,
        ),
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    transient = payload["data"]["transient"]
    assert transient["status"] == "multimode"
    assert transient["frequency"]["status"] == "multimode"
    assert transient["frequency"]["value"] is None
    assert {"period_inconsistent", "amplitude_inconsistent"} & set(
        transient["quality"]["flags"]
    )
    assert payload["diagnostics"][0]["severity"] == "warning"
    assert payload["diagnostics"][0]["code"] == "oscillator.quality.multimode"


def test_full_band_hysteresis_rejects_zero_chatter_without_biasing_frequency() -> None:
    frequency = 100e6
    harmonic = 49

    def chattering(time: float) -> float:
        phase = 2.0 * math.pi * frequency * time
        return 0.8 * math.sin(phase) + 0.08 * math.sin(harmonic * phase)

    series = _series(
        chattering,
        frequency_hz=frequency,
        stop_s=200e-9,
        samples_per_cycle=400,
    )
    payload = measure_oscillator(
        series,
        _request(
            window_start_s=50e-9,
            window_stop_s=200e-9,
            cycle_count=10,
            hold_for_s=20e-9,
            hysteresis_v=0.15,
            minimum_samples_per_cycle=300,
        ),
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    transient = payload["data"]["transient"]
    assert transient["frequency"]["value"] == pytest.approx(frequency, rel=1e-9)
    assert len(transient["crossings"]) == 11

    axis = series["axis"]["values"]
    differential = [
        left - right
        for left, right in zip(
            series["signals"][0]["values"], series["signals"][1]["values"]
        )
    ]
    raw_rising_crossings = sum(
        left_t >= 50e-9 and left < 0.0 <= right
        for left_t, left, right in zip(axis, differential, differential[1:])
    )
    assert raw_rising_crossings > 3 * len(transient["crossings"])


def test_adaptive_grid_power_is_time_weighted_trapezoidal_and_orientation_is_explicit() -> None:
    frequency = 200e6
    window_start = 20e-9
    window_stop = 100e-9
    extra_times = [window_start + index * 5e-12 for index in range(1001)]

    def current(time: float) -> float:
        return 0.5e-3 + 20_000.0 * time

    series = _series(
        _sine(frequency),
        frequency_hz=frequency,
        stop_s=window_stop,
        samples_per_cycle=12,
        extra_times=extra_times,
        supply_voltage=lambda _time: 1.2,
        supply_current=current,
    )
    request = _request(
        window_start_s=window_start,
        window_stop_s=window_stop,
        cycle_count=10,
        hold_for_s=20e-9,
        minimum_samples_per_cycle=10,
    )

    into_load = measure_oscillator(series, request)
    expected = 1.2 * (current(window_start) + current(window_stop)) / 2.0

    _assert_envelope(into_load)
    assert into_load["engineering"]["status"] == "pass"
    assert into_load["data"]["transient"]["average_supply_power"][
        "value"
    ] == pytest.approx(expected, rel=1e-13)
    sampled_powers = [
        voltage * amps
        for time, voltage, amps in zip(
            series["axis"]["values"],
            series["signals"][2]["values"],
            series["signals"][3]["values"],
        )
        if window_start <= time <= window_stop
    ]
    arithmetic_sample_mean = math.fsum(sampled_powers) / len(sampled_powers)
    assert abs(arithmetic_sample_mean - expected) > 1e-4

    source_orientation_request = deepcopy(request)
    source_orientation_request["power"]["current_orientation"] = "positive_into_source"
    into_source = measure_oscillator(series, source_orientation_request)
    assert into_source["engineering"]["status"] == "pass"
    assert into_source["data"]["transient"]["average_supply_power"][
        "value"
    ] == pytest.approx(-expected, rel=1e-13)
    assert (
        into_source["data"]["receipt"]["window_sha256"]
        == into_load["data"]["receipt"]["window_sha256"]
    )
    assert (
        into_source["data"]["receipt"]["sha256"]
        != into_load["data"]["receipt"]["sha256"]
    )


def test_insufficient_sampling_is_a_typed_unknown_not_a_frequency() -> None:
    series = _series(
        _sine(REFERENCE_FREQUENCY_HZ),
        stop_s=60e-9,
        samples_per_cycle=4,
    )

    payload = measure_oscillator(
        series,
        _short_request(minimum_samples_per_cycle=8),
    )

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    transient = payload["data"]["transient"]
    assert transient["status"] == "unknown"
    assert transient["frequency"]["status"] == "unknown"
    assert transient["frequency"]["value"] is None
    assert "sampling_resolution_insufficient" in transient["quality"]["flags"]
    assert payload["diagnostics"][0]["code"] == (
        "oscillator.source.resolution_insufficient"
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("series_digest", "oscillator.source.digest_mismatch"),
        ("axis_unit", "oscillator.unit.mismatch"),
        ("current_unit", "oscillator.unit.mismatch"),
    ],
)
def test_invalid_source_digest_and_units_fail_closed(
    mutation: str,
    expected_code: str,
) -> None:
    series = _series(_sine(REFERENCE_FREQUENCY_HZ), stop_s=60e-9)
    if mutation == "series_digest":
        series["signals"][0]["values"][3] += 0.01
    elif mutation == "axis_unit":
        series["axis"]["unit"] = "ns"
        _rehash(series)
    else:
        series["signals"][3]["unit"] = "mA"
        _rehash(series)

    payload = measure_oscillator(series, _short_request())

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["receipt"] is None
    assert payload["diagnostics"][0]["code"] == expected_code


def test_missing_source_bad_request_id_and_phase_noise_kind_are_typed_invalid_requests() -> None:
    missing = measure_oscillator(None, _short_request())
    bad_request_id = measure_oscillator(
        _series(_sine(REFERENCE_FREQUENCY_HZ), stop_s=60e-9),
        _short_request(),
        request_id="NOT-A-UUID",
    )
    phase_noise = measure_oscillator(
        None,
        {
            "measurement_id": "vco.phase-noise",
            "kind": "phase_noise",
            "extensions": {},
        },
    )

    assert missing["diagnostics"][0]["code"] == "oscillator.source.missing"
    assert bad_request_id["diagnostics"][0]["code"] == "oscillator.request.invalid"
    assert phase_noise["diagnostics"][0]["code"] == "oscillator.kind.unsupported"
    for payload in (missing, bad_request_id, phase_noise):
        _assert_envelope(payload)
        assert payload["execution"]["status"] == "invalid_request"
        assert payload["engineering"]["status"] == "unknown"


def test_irregular_grid_returns_every_local_kvco_and_reference_span() -> None:
    controls = [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0, 1.2]
    frequencies = [
        2.3847e9,
        2.3913e9,
        2.4014e9,
        2.4083e9,
        2.4168e9,
        2.4270e9,
        2.4387e9,
        2.4648e9,
        2.4906e9,
    ]
    expected_kvco = [
        33e6,
        41.75e6,
        62.83333333333333e6,
        77e6,
        93.5e6,
        109.5e6,
        121.5e6,
        129.75e6,
        129e6,
    ]
    receipts = [
        _receipt_for(frequency, conditions=_conditions(control_v=control))
        for control, frequency in zip(controls, frequencies)
    ]
    request = {
        "measurement_id": "vco.tuning-grid",
        "kind": "tuning_grid",
        "control_condition": "vctrl",
        "control_unit": "V",
        "expected_monotonicity": "nondecreasing",
        "points": [
            {
                "control": {"value": control, "unit": "V"},
                "receipt": receipt,
            }
            for control, receipt in zip(controls, receipts)
        ],
        "extensions": {},
    }

    payload = measure_oscillator(None, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurement"]["status"] == "measured"
    grid = payload["data"]["grid"]
    assert grid["status"] == "measured"
    assert grid["observed_monotonicity"] == "nondecreasing"
    assert grid["monotonicity_check"] == "pass"
    assert grid["span"]["value"] == pytest.approx(105.9e6, abs=2.0)
    assert [
        point["local_tuning_gain"]["value"] for point in grid["points"]
    ] == pytest.approx(expected_kvco, rel=2e-8)
    assert [point["stencil"] for point in grid["points"]] == [
        "forward",
        *(["central_nonuniform_quadratic"] * 7),
        "backward",
    ]
    assert [point["receipt_sha256"] for point in grid["points"]] == [
        receipt["sha256"] for receipt in receipts
    ]
    assert len(grid["grid_sha256"]) == 64


def test_monotonic_reversal_is_flagged_without_hiding_the_curve() -> None:
    controls = [0.0, 0.5, 1.0]
    frequencies = [2.40e9, 2.50e9, 2.45e9]
    receipts = [
        _receipt_for(frequency, conditions=_conditions(control_v=control))
        for control, frequency in zip(controls, frequencies)
    ]
    request = {
        "measurement_id": "vco.reversing-grid",
        "kind": "tuning_grid",
        "control_condition": "vctrl",
        "control_unit": "V",
        "expected_monotonicity": "nondecreasing",
        "points": [
            {
                "control": {"value": control, "unit": "V"},
                "receipt": receipt,
            }
            for control, receipt in zip(controls, receipts)
        ],
        "extensions": {},
    }

    payload = measure_oscillator(None, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    grid = payload["data"]["grid"]
    assert grid["status"] == "measured"
    assert grid["observed_monotonicity"] == "non_monotonic"
    assert grid["monotonicity_check"] == "fail"
    assert all(
        point["local_tuning_gain"]["status"] == "measured"
        for point in grid["points"]
    )
    assert payload["diagnostics"][0]["code"] == "oscillator.grid.non_monotonic"


def test_non_sustained_receipt_propagates_unknown_through_grid_and_shift() -> None:
    first = _receipt_for(2.40e9, conditions=_conditions(control_v=0.0, vdd_v=1.2))
    missing = _flat_receipt(conditions=_conditions(control_v=0.5, vdd_v=1.2))
    last = _receipt_for(2.50e9, conditions=_conditions(control_v=1.0, vdd_v=1.2))
    grid_request = {
        "measurement_id": "vco.incomplete-grid",
        "kind": "tuning_grid",
        "control_condition": "vctrl",
        "control_unit": "V",
        "expected_monotonicity": "nondecreasing",
        "points": [
            {"control": {"value": 0.0, "unit": "V"}, "receipt": first},
            {"control": {"value": 0.5, "unit": "V"}, "receipt": missing},
            {"control": {"value": 1.0, "unit": "V"}, "receipt": last},
        ],
        "extensions": {},
    }

    grid_payload = measure_oscillator(None, grid_request)

    _assert_envelope(grid_payload)
    assert grid_payload["engineering"]["status"] == "unknown"
    assert grid_payload["data"]["grid"]["status"] == "unknown"
    assert grid_payload["data"]["grid"]["span"]["value"] is None
    assert all(
        point["local_tuning_gain"]["value"] is None
        for point in grid_payload["data"]["grid"]["points"]
    )
    assert grid_payload["diagnostics"][0]["code"] == "oscillator.grid.incomplete"

    reference = _receipt_for(
        2.4399e9,
        conditions=_conditions(control_v=0.81, vdd_v=1.2),
    )
    perturbed_missing = _flat_receipt(
        conditions=_conditions(control_v=0.81, vdd_v=1.26)
    )
    shift_payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.incomplete-shift",
            "kind": "frequency_shift",
            "perturbation_condition": "vdd",
            "reference": {
                "condition": {"value": 1.2, "unit": "V"},
                "receipt": reference,
            },
            "perturbed": {
                "condition": {"value": 1.26, "unit": "V"},
                "receipt": perturbed_missing,
            },
            "extensions": {},
        },
    )
    assert shift_payload["engineering"]["status"] == "unknown"
    assert shift_payload["data"]["shift"]["signed_shift"]["value"] is None
    assert shift_payload["data"]["shift"]["absolute_shift"]["value"] is None
    assert shift_payload["diagnostics"][0]["code"] == "oscillator.shift.incomplete"


def test_frequency_shift_preserves_sign_absolute_value_and_pair_identity() -> None:
    reference = _receipt_for(
        2.4399e9,
        conditions=_conditions(control_v=0.81, vdd_v=1.20),
    )
    perturbed = _receipt_for(
        2.4329e9,
        conditions=_conditions(control_v=0.81, vdd_v=1.26),
    )
    request = {
        "measurement_id": "vco.supply-pushing",
        "kind": "frequency_shift",
        "perturbation_condition": "vdd",
        "reference": {
            "condition": {"value": 1.20, "unit": "V"},
            "receipt": reference,
        },
        "perturbed": {
            "condition": {"value": 1.26, "unit": "V"},
            "receipt": perturbed,
        },
        "extensions": {},
    }

    payload = measure_oscillator(None, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    shift = payload["data"]["shift"]
    assert shift["status"] == "measured"
    assert shift["signed_shift"]["value"] == pytest.approx(-7.0e6, abs=2.0)
    assert shift["absolute_shift"]["value"] == pytest.approx(7.0e6, abs=2.0)
    assert shift["reference_receipt_sha256"] == reference["sha256"]
    assert shift["perturbed_receipt_sha256"] == perturbed["sha256"]
    assert len(shift["pair_sha256"]) == 64


def test_frequency_shift_rejects_a_changed_undeclared_context() -> None:
    reference = _receipt_for(
        2.4399e9,
        conditions=_conditions(control_v=0.81, vdd_v=1.20),
    )
    changed_control = _receipt_for(
        2.4329e9,
        conditions=_conditions(control_v=0.80, vdd_v=1.26),
    )

    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.invalid-pushing-context",
            "kind": "frequency_shift",
            "perturbation_condition": "vdd",
            "reference": {
                "condition": {"value": 1.20, "unit": "V"},
                "receipt": reference,
            },
            "perturbed": {
                "condition": {"value": 1.26, "unit": "V"},
                "receipt": changed_control,
            },
            "extensions": {},
        },
    )

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["shift"] is None
    assert payload["diagnostics"][0]["code"] == "oscillator.condition.mismatch"


def test_receipt_digest_tampering_is_rejected_and_helper_is_public() -> None:
    receipts = [
        _receipt_for(2.40e9, conditions=_conditions(control_v=0.0)),
        _receipt_for(2.45e9, conditions=_conditions(control_v=0.5)),
        _receipt_for(2.50e9, conditions=_conditions(control_v=1.0)),
    ]
    assert oscillator_receipt_sha256(receipts[0]) == receipts[0]["sha256"]
    with pytest.raises(ValueError, match="must be an object"):
        oscillator_receipt_sha256([])  # type: ignore[arg-type]

    tampered = deepcopy(receipts[1])
    tampered["frequency"]["value"] += 1.0
    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.tampered-grid",
            "kind": "tuning_grid",
            "control_condition": "vctrl",
            "control_unit": "V",
            "expected_monotonicity": "nondecreasing",
            "points": [
                {"control": {"value": 0.0, "unit": "V"}, "receipt": receipts[0]},
                {"control": {"value": 0.5, "unit": "V"}, "receipt": tampered},
                {"control": {"value": 1.0, "unit": "V"}, "receipt": receipts[2]},
            ],
            "extensions": {},
        },
    )

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == (
        "oscillator.receipt.digest_mismatch"
    )


def test_pending_hysteresis_candidate_is_cancelled_before_a_later_rise() -> None:
    assert _hysteretic_crossings(
        [0.0, 1.0, 2.0, 3.0, 4.0],
        [-2.0, 0.5, -2.0, 2.0, -2.0],
        threshold=0.0,
        hysteresis=1.0,
    ) == [pytest.approx(2.5)]


def test_clean_but_under_counted_window_is_not_reported_as_collapse() -> None:
    frequency = 100e6
    payload = measure_oscillator(
        _series(_sine(frequency), frequency_hz=frequency, stop_s=100e-9),
        _request(
            window_start_s=20e-9,
            window_stop_s=100e-9,
            cycle_count=50,
            hold_for_s=20e-9,
        ),
    )

    _assert_envelope(payload)
    transient = payload["data"]["transient"]
    assert transient["status"] == "not_sustained"
    assert transient["startup"]["started_at"] is not None
    assert transient["startup"]["collapse_at"] is None
    assert transient["frequency"]["value"] is None
    assert "late_crossings_insufficient" in transient["quality"]["flags"]


@pytest.mark.parametrize("window_start_s", [40e-9, 41e-9, 44e-9])
def test_partial_cycle_crop_is_never_positive_collapse_evidence(
    window_start_s: float,
) -> None:
    frequency = 100e6
    payload = measure_oscillator(
        _series(
            _sine(frequency),
            frequency_hz=frequency,
            stop_s=100e-9,
            samples_per_cycle=100,
        ),
        _request(
            window_start_s=window_start_s,
            window_stop_s=window_start_s + 2e-9,
            cycle_count=2,
            hold_for_s=20e-9,
            minimum_peak_to_peak_v=0.8,
            minimum_samples_per_cycle=20,
        ),
    )
    _assert_envelope(payload)
    assert payload["data"]["transient"]["status"] == "not_sustained"
    assert payload["data"]["transient"]["startup"]["collapse_at"] is None


def test_high_hysteresis_missing_tail_confirmation_is_not_collapse() -> None:
    frequency = 100e6
    payload = measure_oscillator(
        _series(
            _sine(frequency),
            frequency_hz=frequency,
            stop_s=102e-9,
            samples_per_cycle=400,
        ),
        _request(
            window_start_s=20e-9,
            window_stop_s=102e-9,
            cycle_count=5,
            hold_for_s=20e-9,
            hysteresis_v=0.79,
            minimum_samples_per_cycle=300,
        ),
    )
    _assert_envelope(payload)
    assert payload["data"]["transient"]["status"] == "not_sustained"
    assert payload["data"]["transient"]["startup"]["collapse_at"] is None


def test_late_growth_is_not_misclassified_as_a_pre_onset_collapse() -> None:
    frequency = REFERENCE_FREQUENCY_HZ

    def growing(time: float) -> float:
        amplitude = 0.1 if time < 30e-9 else 0.8
        return amplitude * math.sin(2.0 * math.pi * frequency * time)

    payload = measure_oscillator(
        _series(growing, stop_s=60e-9, samples_per_cycle=16),
        _short_request(
            cycle_count=50,
            minimum_peak_to_peak_v=0.8,
            minimum_samples_per_cycle=10,
        ),
    )

    _assert_envelope(payload)
    transient = payload["data"]["transient"]
    assert transient["status"] == "not_sustained"
    assert transient["startup"]["started_at"]["value"] >= 30e-9
    assert transient["startup"]["collapse_at"] is None


def test_startup_hold_requires_at_least_three_complete_cycles() -> None:
    crossings = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    started, activity = _startup_candidate(
        crossings,
        [0.0, 0.05, 0.0, -0.05, 1.0, -1.0],
        crossings,
        hold_for=0.5,
        minimum_amplitude=0.5,
        maximum_period_deviation=0.05,
        maximum_amplitude_deviation=0.2,
        minimum_samples=2,
    )
    assert activity
    assert started is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt["startup"].update(
            started_at={"value": 20e-9, "unit": "s"},
            time={"value": 20e-9, "unit": "s"},
        ),
        lambda receipt: receipt["quality"].update(
            period_relative_deviation=0.9
        ),
        lambda receipt: receipt["differential_peak_to_peak"].update(value=0.01),
        lambda receipt: receipt["quality"].update(flags=[{}]),
    ],
)
def test_rehashed_semantically_forged_receipt_is_rejected(mutate) -> None:
    receipts = [
        _receipt_for(2.40e9, conditions=_conditions(control_v=0.0)),
        _receipt_for(2.45e9, conditions=_conditions(control_v=0.5)),
        _receipt_for(2.50e9, conditions=_conditions(control_v=1.0)),
    ]
    mutate(receipts[1])
    receipts[1]["sha256"] = oscillator_receipt_sha256(receipts[1])
    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.forged-grid",
            "kind": "tuning_grid",
            "control_condition": "vctrl",
            "control_unit": "V",
            "expected_monotonicity": "nondecreasing",
            "points": [
                {
                    "control": {"value": control, "unit": "V"},
                    "receipt": receipt,
                }
                for control, receipt in zip((0.0, 0.5, 1.0), receipts)
            ],
            "extensions": {},
        },
    )
    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "oscillator.receipt.invalid"


def test_quality_caps_cannot_be_relaxed_to_hide_beating() -> None:
    request = _short_request()
    request["quality"]["maximum_period_relative_deviation"] = 0.051
    request["quality"]["maximum_amplitude_relative_deviation"] = 0.201
    payload = measure_oscillator(
        _series(_sine(REFERENCE_FREQUENCY_HZ), stop_s=60e-9), request
    )
    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "oscillator.quality.invalid"


def test_extreme_finite_arithmetic_fails_typed_instead_of_escaping() -> None:
    assert _average_power(
        [0.0, 1.0, 2.0, 3.0],
        [1.0, 1.0, 1.0, 1.0],
        [1e308, 1e308, -1e308, -1e308],
        "positive_into_load",
    ) == pytest.approx(0.0)
    assert _hysteretic_crossings(
        [0.0, 1.0],
        [-1e308, 1e308],
        threshold=0.0,
        hysteresis=1.0,
    ) == [pytest.approx(0.5)]
    assert _hysteretic_crossings(
        [-1e308, 1e308],
        [-1e308, 1e308],
        threshold=0.0,
        hysteresis=1.0,
    ) == [pytest.approx(0.0)]


def test_subnormal_control_spacing_returns_a_typed_nonfinite_result() -> None:
    controls = (0.0, 5e-324, 1e-323)
    receipts = [
        _receipt_for(
            frequency,
            conditions=_conditions(control_v=control),
        )
        for control, frequency in zip(controls, (2.40e9, 2.45e9, 2.50e9))
    ]
    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.subnormal-grid",
            "kind": "tuning_grid",
            "control_condition": "vctrl",
            "control_unit": "V",
            "expected_monotonicity": "nondecreasing",
            "points": [
                {
                    "control": {"value": control, "unit": "V"},
                    "receipt": receipt,
                }
                for control, receipt in zip(controls, receipts)
            ],
            "extensions": {},
        },
    )
    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "oscillator.value.non_finite"


def test_context_comparison_distinguishes_boolean_from_number() -> None:
    conditions = _conditions(control_v=0.0)
    conditions.append({"name": "enabled", "value": True, "unit": "1"})
    first = _receipt_for(2.40e9, conditions=conditions)
    numeric_conditions = _conditions(control_v=0.5)
    numeric_conditions.append({"name": "enabled", "value": 1.0, "unit": "1"})
    second = _receipt_for(2.45e9, conditions=numeric_conditions)
    last_conditions = _conditions(control_v=1.0)
    last_conditions.append({"name": "enabled", "value": True, "unit": "1"})
    last = _receipt_for(2.50e9, conditions=last_conditions)
    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.typed-context-grid",
            "kind": "tuning_grid",
            "control_condition": "vctrl",
            "control_unit": "V",
            "expected_monotonicity": "nondecreasing",
            "points": [
                {"control": {"value": 0.0, "unit": "V"}, "receipt": first},
                {"control": {"value": 0.5, "unit": "V"}, "receipt": second},
                {"control": {"value": 1.0, "unit": "V"}, "receipt": last},
            ],
            "extensions": {},
        },
    )
    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "oscillator.condition.mismatch"


def test_flat_grid_passes_either_monotonic_direction() -> None:
    receipts = [
        _receipt_for(2.40e9, conditions=_conditions(control_v=control))
        for control in (0.0, 0.5, 1.0)
    ]
    payload = measure_oscillator(
        None,
        {
            "measurement_id": "vco.flat-grid",
            "kind": "tuning_grid",
            "control_condition": "vctrl",
            "control_unit": "V",
            "expected_monotonicity": "nonincreasing",
            "points": [
                {
                    "control": {"value": control, "unit": "V"},
                    "receipt": receipt,
                }
                for control, receipt in zip((0.0, 0.5, 1.0), receipts)
            ],
            "extensions": {},
        },
    )
    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["grid"]["observed_monotonicity"] == "constant"
    assert payload["data"]["grid"]["monotonicity_check"] == "pass"
