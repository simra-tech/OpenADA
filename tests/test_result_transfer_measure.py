from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.operations.result_measure import normalized_series_sha256
from openada.operations.result_transfer_measure import measure_transfer
from openada.operations.specification_evaluate import evaluate_specification


ROOT = Path(__file__).parents[1]
RESULT_SCHEMA = json.loads(
    (ROOT / "schemas" / "result-v0alpha1.schema.json").read_text(encoding="utf-8")
)
PROFILE_SCHEMA = json.loads(
    (ROOT / "schemas" / "operation-profile-v0alpha2.schema.json").read_text(
        encoding="utf-8"
    )
)
TRANSFER_PROFILE = json.loads(
    (ROOT / "profiles" / "result.transfer.measure-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
RESULT_VALIDATOR = Draft202012Validator(RESULT_SCHEMA, format_checker=FormatChecker())
PROFILE_VALIDATOR = Draft202012Validator(
    PROFILE_SCHEMA, format_checker=FormatChecker()
)
REQUEST_VALIDATOR = Draft202012Validator(
    TRANSFER_PROFILE["request"]["parameters_schema"],
    format_checker=FormatChecker(),
)
DATA_VALIDATOR = Draft202012Validator(
    TRANSFER_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)


def _series(
    *,
    magnitudes_db: tuple[float, ...] = (20.0, 15.0, -5.0, -20.0),
    phases_deg: tuple[float, ...] = (0.0, -45.0, -135.0, -225.0),
    frequencies_hz: tuple[float, ...] = (1.0, 10.0, 100.0, 1000.0),
    units: tuple[str, str, str, str] = ("V", "V", "V", "V"),
) -> dict:
    assert len(magnitudes_db) == len(phases_deg) == len(frequencies_hz)
    output = [
        10.0 ** (magnitude_db / 20.0)
        * complex(
            math.cos(math.radians(phase_deg)), math.sin(math.radians(phase_deg))
        )
        for magnitude_db, phase_deg in zip(magnitudes_db, phases_deg)
    ]
    axis = {"name": "frequency", "unit": "Hz", "values": list(frequencies_hz)}
    signals = [
        {"name": "vin.real", "unit": units[0], "values": [1.0] * len(output)},
        {"name": "vin.imag", "unit": units[1], "values": [0.0] * len(output)},
        {
            "name": "vout.real",
            "unit": units[2],
            "values": [value.real for value in output],
        },
        {
            "name": "vout.imag",
            "unit": units[3],
            "values": [value.imag for value in output],
        },
    ]
    conditions = [
        {"name": "temperature", "value": 27.0, "unit": "degC"},
        {"name": "corner", "value": "tt", "unit": "1"},
    ]
    digest = normalized_series_sha256(
        axis=axis, signals=signals, conditions=conditions
    )
    return {
        "source": {
            "operation": "result.series.extract",
            "request_id": str(uuid.uuid4()),
            "artifact_role": "measurement.source",
            "artifact_sha256": digest,
            "lineage": {
                "operation": "simulate",
                "request_id": str(uuid.uuid4()),
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


def _request(
    kind: str = "low_frequency_gain_db",
    *,
    interpretation: str = "forward",
) -> dict:
    units = {
        "low_frequency_gain_db": "dB",
        "bandwidth_3db": "Hz",
        "unity_gain_frequency": "Hz",
        "phase_margin": "deg",
    }
    return {
        "measurement_id": f"open_loop.{kind}",
        "input": {"real": "vin.real", "imaginary": "vin.imag"},
        "output": {"real": "vout.real", "imaginary": "vout.imag"},
        "interpretation": interpretation,
        "method": {
            "id": "openada.method/ac-complex-ratio-log-interpolation/v1alpha1",
            "ratio": "output-over-input",
            "phase_unwrap": "first-principal-then-nearest-delta",
            "first_phase_range": "[-180,180)",
            "interpolation": "linear-value-over-log10-frequency",
            "crossing_policy": "require-single-falling",
            "bandwidth_reference": "first-simulated-frequency-magnitude",
            "bandwidth_drop_db": 3.0,
            "phase_margin_definition": "180deg-plus-unwrapped-loop-phase-at-unity",
        },
        "metric": {"kind": kind, "unit": units[kind]},
        "extensions": {},
    }


def _assert_envelope(payload: dict) -> None:
    result_errors = sorted(
        RESULT_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path)
    )
    assert not result_errors, "\n".join(error.message for error in result_errors)
    data_errors = sorted(
        DATA_VALIDATOR.iter_errors(payload["data"]), key=lambda item: list(item.path)
    )
    assert not data_errors, "\n".join(error.message for error in data_errors)


def test_profile_and_closed_request_are_schema_valid() -> None:
    profile_errors = sorted(
        PROFILE_VALIDATOR.iter_errors(TRANSFER_PROFILE),
        key=lambda item: list(item.path),
    )
    assert not profile_errors, "\n".join(error.message for error in profile_errors)

    parameters = {"series": _series(), "transfer": _request(), "extensions": {}}
    request_errors = sorted(
        REQUEST_VALIDATOR.iter_errors(parameters), key=lambda item: list(item.path)
    )
    assert not request_errors, "\n".join(error.message for error in request_errors)


def test_equivalent_integer_and_float_method_literals_have_one_request_digest() -> None:
    integer_request = _request()
    integer_request["method"]["bandwidth_drop_db"] = 3

    integer_payload = measure_transfer(_series(), integer_request)
    float_payload = measure_transfer(_series(), _request())

    assert integer_payload["data"]["measurement"]["request_sha256"] == (
        float_payload["data"]["measurement"]["request_sha256"]
    )


@pytest.mark.parametrize(
    ("kind", "interpretation", "expected", "location_hz"),
    [
        ("low_frequency_gain_db", "forward", 20.0, 1.0),
        ("bandwidth_3db", "forward", 10.0**0.6, 10.0**0.6),
        ("unity_gain_frequency", "forward", 10.0**1.75, 10.0**1.75),
        ("phase_margin", "loop-gain-negative-feedback", 67.5, 10.0**1.75),
    ],
)
def test_closed_transfer_metrics(
    kind: str, interpretation: str, expected: float, location_hz: float
) -> None:
    payload = measure_transfer(
        _series(), _request(kind, interpretation=interpretation)
    )

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    measurement = payload["data"]["measurement"]
    assert measurement["status"] == "measured"
    assert measurement["value"] == pytest.approx(expected, abs=1e-12)
    assert measurement["location"] == pytest.approx(
        {"value": location_hz, "unit": "Hz"}
    )
    trace = payload["data"]["transfer"]
    assert trace["reference"] == pytest.approx(
        {
            "kind": "first-simulated-frequency-not-dc",
            "frequency_hz": 1.0,
            "magnitude_db": 20.0,
        }
    )
    assert trace["trace"]["magnitude_db"] == pytest.approx(
        [20.0, 15.0, -5.0, -20.0], abs=1e-12
    )
    assert trace["trace"]["phase_deg"] == pytest.approx(
        [0.0, -45.0, -135.0, -225.0], abs=1e-12
    )
    assert trace["excluded_metrics"][0]["metric"] == "gain_margin"


def test_multiple_unity_crossings_are_unknown_not_implicitly_selected() -> None:
    series = _series(
        magnitudes_db=(20.0, -5.0, 10.0, -5.0),
        phases_deg=(0.0, -60.0, -90.0, -150.0),
    )

    payload = measure_transfer(series, _request("unity_gain_frequency"))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "completed"
    assert payload["data"]["measurement"]["status"] == "unknown"
    assert payload["data"]["transfer"]["status"] == "crossing_ambiguous"
    assert payload["data"]["transfer"]["crossings"]["unity_gain"]["count"] == 2
    assert payload["diagnostics"][0]["code"] == "transfer.crossing.ambiguous"


def test_absent_unity_crossing_is_typed_not_found_evidence() -> None:
    series = _series(
        magnitudes_db=(20.0, 12.0, 6.0, 2.0),
        phases_deg=(0.0, -30.0, -60.0, -90.0),
    )

    payload = measure_transfer(series, _request("unity_gain_frequency"))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "fail"
    assert payload["execution"]["status"] == "completed"
    assert payload["data"]["measurement"]["status"] == "not_found"
    assert payload["data"]["transfer"]["crossings"]["unity_gain"] == {
        "status": "not_found",
        "threshold_db": 0.0,
        "count": 0,
        "candidates": [],
    }
    assert payload["diagnostics"][0]["code"] == "transfer.crossing.not_found"


def test_falling_crossing_at_right_endpoint_is_exact_and_not_duplicated() -> None:
    series = _series(
        magnitudes_db=(20.0, 0.0, -5.0, -10.0),
        phases_deg=(0.0, -90.0, -120.0, -150.0),
    )

    payload = measure_transfer(series, _request("unity_gain_frequency"))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurement"]["value"] == pytest.approx(10.0)
    assert payload["data"]["transfer"]["crossings"]["unity_gain"]["count"] == 1


@pytest.mark.parametrize(
    ("mutator", "code", "execution_status"),
    [
        (
            lambda series, request: request["method"].update(
                crossing_policy="first-falling"
            ),
            "transfer.method.unsupported",
            "invalid_request",
        ),
        (
            lambda series, request: request.update(interpretation="forward"),
            "transfer.phase_margin.invalid_context",
            "invalid_request",
        ),
        (
            lambda series, request: series["signals"][0]["values"].__setitem__(
                0, 0.0
            ),
            "transfer.source.invalid",
            "invalid_request",
        ),
    ],
)
def test_invalid_or_unsupported_inputs_fail_closed(
    mutator, code: str, execution_status: str
) -> None:
    series = _series()
    request = _request("phase_margin", interpretation="loop-gain-negative-feedback")
    mutator(series, request)

    payload = measure_transfer(series, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == execution_status
    assert payload["data"]["measurement"]["value"] is None
    assert payload["diagnostics"][0]["code"] == code


def test_zero_output_is_unknown_without_numeric_floor_or_infinity() -> None:
    series = _series(magnitudes_db=(20.0, 15.0, -5.0, -20.0))
    series["signals"][2]["values"][1] = 0.0
    series["signals"][3]["values"][1] = 0.0
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"],
        signals=series["signals"],
        conditions=series["conditions"],
    )

    payload = measure_transfer(series, _request())

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "completed"
    assert payload["diagnostics"][0]["code"] == "transfer.ratio.undefined"
    assert "Infinity" not in json.dumps(payload, allow_nan=False)


def test_cartesian_component_units_must_match_exactly() -> None:
    series = _series(units=("V", "V", "A", "A"))

    payload = measure_transfer(series, _request())

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "transfer.unit.mismatch"


def test_successful_transfer_measurement_feeds_specification_kernel() -> None:
    measured = measure_transfer(_series(), _request())["data"]["measurement"]
    evaluated = evaluate_specification(
        measured,
        {
            "specification_id": "open_loop.gain.minimum",
            "measurement_id": "open_loop.low_frequency_gain_db",
            "limits": {
                "lower": {"value": 19.0, "unit": "dB", "inclusive": True}
            },
            "conditions": [
                {"name": "temperature", "value": 27.0, "unit": "degC"},
                {"name": "corner", "value": "tt", "unit": "1"},
            ],
            "extensions": {},
        },
    )

    assert evaluated["engineering"]["status"] == "pass"


def test_inputs_are_not_mutated() -> None:
    series = _series()
    request = _request("phase_margin", interpretation="loop-gain-negative-feedback")
    series_before = deepcopy(series)
    request_before = deepcopy(request)

    measure_transfer(series, request)

    assert series == series_before
    assert request == request_before
