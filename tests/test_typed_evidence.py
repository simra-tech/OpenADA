from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.operations import (
    MEASUREMENT_KINDS,
    SPECIFICATION_LIMIT_KINDS,
    evaluate_specification,
    measure_result,
    normalized_series_sha256,
)


ROOT = Path(__file__).parents[1]
RESULT_SCHEMA = json.loads(
    (ROOT / "schemas" / "result-v0alpha1.schema.json").read_text(encoding="utf-8")
)
RESULT_VALIDATOR = Draft202012Validator(RESULT_SCHEMA, format_checker=FormatChecker())
MEASUREMENT_PROFILE = json.loads(
    (ROOT / "profiles" / "result.measure-v1alpha1.json").read_text(encoding="utf-8")
)
SPECIFICATION_PROFILE = json.loads(
    (ROOT / "profiles" / "specification.evaluate-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
MEASUREMENT_DATA_VALIDATOR = Draft202012Validator(
    MEASUREMENT_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)
SPECIFICATION_DATA_VALIDATOR = Draft202012Validator(
    SPECIFICATION_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)


def _canonical_digest(axis: dict, signals: list[dict], conditions: list[dict]) -> str:
    return normalized_series_sha256(
        axis=axis,
        signals=signals,
        conditions=conditions,
    )


def _series(
    *,
    axis_values: list[float] | None = None,
    signal_values: list[float] | None = None,
) -> dict:
    axis = {
        "name": "time",
        "unit": "s",
        "values": axis_values or [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    }
    signals = [
        {
            "name": "v(out)",
            "unit": "V",
            "values": signal_values or [0.0, 0.5, 1.0, 0.5, 0.0, 0.5, 1.0],
        }
    ]
    conditions = [
        {"name": "temperature", "value": 27.0, "unit": "degC"},
        {"name": "corner", "value": "tt", "unit": "1"},
    ]
    try:
        digest = _canonical_digest(axis, signals, conditions)
    except ValueError:
        # Deliberately malformed-source tests never reach digest comparison.
        digest = "0" * 64
    return {
        "source": {
            "operation": "simulation.result.normalize",
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


def _request(kind: str, parameters: dict) -> dict:
    return {
        "measurement_id": f"output.{kind}",
        "kind": kind,
        "signal": "v(out)",
        "parameters": parameters,
        "extensions": {},
    }


def _assert_envelope(payload: dict) -> None:
    errors = sorted(RESULT_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)


def _assert_operation_data(payload: dict, validator: Draft202012Validator) -> None:
    errors = sorted(validator.iter_errors(payload["data"]), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)


@pytest.mark.parametrize(
    ("kind", "parameters", "expected", "unit"),
    [
        (
            "sample_at",
            {"at": {"value": 0.5, "unit": "s"}, "interpolation": "linear"},
            0.25,
            "V",
        ),
        ("minimum", {}, 0.0, "V"),
        ("maximum", {}, 1.0, "V"),
        ("mean", {}, 0.5, "V"),
        ("rms", {}, math.sqrt(2.75 / 7.0), "V"),
        (
            "crossing",
            {
                "threshold": {"value": 0.75, "unit": "V"},
                "direction": "rising",
                "occurrence": 1,
            },
            1.5,
            "s",
        ),
        (
            "rise_time",
            {
                "lower_threshold": {"value": 0.2, "unit": "V"},
                "upper_threshold": {"value": 0.8, "unit": "V"},
                "occurrence": 1,
            },
            1.2,
            "s",
        ),
        (
            "fall_time",
            {
                "lower_threshold": {"value": 0.2, "unit": "V"},
                "upper_threshold": {"value": 0.8, "unit": "V"},
                "occurrence": 1,
            },
            1.2,
            "s",
        ),
    ],
)
def test_closed_measurement_vocabulary_returns_typed_values(
    kind: str,
    parameters: dict,
    expected: float,
    unit: str,
) -> None:
    payload = measure_result(_series(), _request(kind, parameters))

    assert payload["engineering"]["status"] == "pass"
    measured = payload["data"]["measurement"]
    assert measured["status"] == "measured"
    assert measured["value"] == pytest.approx(expected)
    assert measured["unit"] == unit
    assert measured["algorithm"]["version"] == "1.0.0"
    assert len(measured["request_sha256"]) == 64
    assert measured["source"]["artifact_sha256"] == measured["source"]["series_sha256"]
    assert measured["source"]["lineage"]["binding"] == "unverified"
    _assert_envelope(payload)
    _assert_operation_data(payload, MEASUREMENT_DATA_VALIDATOR)


def test_settling_time_requires_the_explicit_hold_duration() -> None:
    series = _series(
        axis_values=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        signal_values=[0.0, 0.7, 0.96, 1.01, 1.0, 1.0],
    )
    request = _request(
        "settling_time",
        {
            "target": {"value": 1.0, "unit": "V"},
            "tolerance": {"value": 0.05, "unit": "V"},
            "reference": {"value": 0.0, "unit": "s"},
            "hold_for": {"value": 2.0, "unit": "s"},
        },
    )

    payload = measure_result(series, request)

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurement"]["value"] == 2.0


def test_absent_event_is_measurement_fail_not_unknown() -> None:
    request = _request(
        "crossing",
        {
            "threshold": {"value": 2.0, "unit": "V"},
            "direction": "rising",
            "occurrence": 1,
        },
    )

    payload = measure_result(_series(), request)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["measurement"]["status"] == "not_found"
    assert payload["diagnostics"][0]["code"] == "measurement.value.not_found"
    _assert_operation_data(payload, MEASUREMENT_DATA_VALIDATOR)


def test_series_content_tampering_cannot_reuse_an_unrelated_digest() -> None:
    series = _series()
    series["signals"][0]["values"][1] = 0.75

    payload = measure_result(series, _request("maximum", {}))

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "measurement.source.digest_mismatch"
    assert payload["data"]["measurement"]["source"] is None
    _assert_operation_data(payload, MEASUREMENT_DATA_VALIDATOR)


def test_public_series_digest_helper_uses_measurement_normalization_rules() -> None:
    series = _series()

    assert normalized_series_sha256(
        axis=series["axis"],
        signals=series["signals"],
        conditions=series["conditions"],
    ) == series["source"]["artifact_sha256"]

    with pytest.raises(ValueError, match="strictly increasing"):
        normalized_series_sha256(
            axis={"name": "time", "unit": "s", "values": [0.0, 0.0]},
            signals=[{"name": "v(out)", "unit": "V", "values": [0.0, 1.0]}],
            conditions=[],
        )


def test_measurement_rejects_unit_guessing_and_nonempty_extensions() -> None:
    mismatch = _request(
        "crossing",
        {
            "threshold": {"value": 0.5, "unit": "mV"},
            "direction": "rising",
            "occurrence": 1,
        },
    )
    extended = _request("maximum", {})
    extended["extensions"] = {"org.example.payload": {"deep": {"unbounded": "x" * 10_000}}}

    mismatch_payload = measure_result(_series(), mismatch)
    extension_payload = measure_result(_series(), extended)

    assert mismatch_payload["engineering"]["status"] == "unknown"
    assert mismatch_payload["diagnostics"][0]["code"] == "measurement.unit.mismatch"
    assert extension_payload["engineering"]["status"] == "unknown"
    assert "must be empty" in extension_payload["diagnostics"][0]["message"]


def test_measurement_request_with_non_string_mapping_key_fails_closed() -> None:
    request = _request("maximum", {})
    request[7] = "not-a-json-field-name"

    payload = measure_result(_series(), request)

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["measurement"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "measurement.request.invalid"
    assert "field names must all be strings" in payload["diagnostics"][0]["message"]


def test_condition_strings_are_bounded_for_mapping_callers() -> None:
    series = _series()
    series["conditions"][1]["value"] = "x" * 257

    payload = measure_result(series, _request("maximum", {}))

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"


def test_large_json_integers_and_reducer_overflow_fail_closed() -> None:
    huge_axis = _series(axis_values=[0.0, 10**400], signal_values=[0.0, 1.0])
    overflowing = _series(
        axis_values=[0.0, 1.0],
        signal_values=[1e308, 1e308],
    )

    huge_payload = measure_result(huge_axis, _request("maximum", {}))
    mean_payload = measure_result(overflowing, _request("mean", {}))

    assert huge_payload["engineering"]["status"] == "unknown"
    assert huge_payload["execution"]["status"] == "invalid_request"
    assert mean_payload["engineering"]["status"] == "unknown"
    assert mean_payload["diagnostics"][0]["code"] == "measurement.value.non_finite"


def test_settling_time_handles_the_advertised_large_series_linearly() -> None:
    points = 20_000
    series = _series(
        axis_values=[float(index) for index in range(points)],
        signal_values=[1.0] * (points - 1) + [0.0],
    )
    request = _request(
        "settling_time",
        {
            "target": {"value": 1.0, "unit": "V"},
            "tolerance": {"value": 0.05, "unit": "V"},
            "reference": {"value": 0.0, "unit": "s"},
            "hold_for": {"value": 10.0, "unit": "s"},
        },
    )

    payload = measure_result(series, request)

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["measurement"]["status"] == "not_found"


def _measurement(kind: str = "maximum", parameters: dict | None = None) -> dict:
    result = measure_result(_series(), _request(kind, parameters or {}))
    assert result["data"]["measurement"]["source"] is not None
    return result["data"]["measurement"]


def _specification(*, upper: float = 1.1, inclusive: bool = True) -> dict:
    return {
        "specification_id": "output.maximum.limit",
        "measurement_id": "output.maximum",
        "limits": {
            "lower": {"value": 0.9, "unit": "V", "inclusive": True},
            "upper": {"value": upper, "unit": "V", "inclusive": inclusive},
        },
        "conditions": [
            {"name": "temperature", "value": 27.0, "unit": "degC"},
            {"name": "corner", "value": "tt", "unit": "1"},
        ],
        "extensions": {},
    }


def test_specification_pass_retains_limits_conditions_margin_and_source() -> None:
    payload = evaluate_specification(_measurement(), _specification())

    assert payload["engineering"]["status"] == "pass"
    evaluation = payload["data"]["evaluation"]
    assert evaluation["status"] == "pass"
    assert evaluation["conditions"] == {
        "status": "matched",
        "required_count": 2,
        "matched_count": 2,
    }
    assert evaluation["margin"] == {
        "value": pytest.approx(0.1),
        "unit": "V",
        "relative_to": "lower",
    }
    assert len(evaluation["source"]["measurement_sha256"]) == 64
    assert len(evaluation["source"]["specification_sha256"]) == 64
    assert evaluation["source"]["specification"] == _specification()
    _assert_envelope(payload)
    _assert_operation_data(payload, SPECIFICATION_DATA_VALIDATOR)


@pytest.mark.parametrize(
    "specification",
    [
        _specification(upper=0.95),
        _specification(upper=1.0, inclusive=False),
    ],
)
def test_specification_outside_or_on_exclusive_boundary_is_fail(
    specification: dict,
) -> None:
    payload = evaluate_specification(_measurement(), specification)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["evaluation"]["status"] == "fail"
    assert payload["diagnostics"][0]["code"] == "specification.limit.violated"
    _assert_operation_data(payload, SPECIFICATION_DATA_VALIDATOR)


def test_unknown_measurement_propagates_to_unknown_specification() -> None:
    measurement = _measurement(
        "crossing",
        {
            "threshold": {"value": 2.0, "unit": "V"},
            "direction": "rising",
            "occurrence": 1,
        },
    )
    specification = _specification()
    specification["measurement_id"] = measurement["measurement_id"]

    payload = evaluate_specification(measurement, specification)

    assert measurement["status"] == "not_found"
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "specification.measurement.unknown"
    _assert_operation_data(payload, SPECIFICATION_DATA_VALIDATOR)


@pytest.mark.parametrize("mismatch", ["unit", "condition"])
def test_specification_cannot_decide_with_unbound_units_or_conditions(
    mismatch: str,
) -> None:
    specification = _specification()
    if mismatch == "unit":
        specification["limits"]["upper"]["unit"] = "mV"
    else:
        specification["conditions"][0]["value"] = 125.0

    payload = evaluate_specification(_measurement(), specification)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["evaluation"]["status"] == "unknown"
    expected = (
        "specification.unit.mismatch"
        if mismatch == "unit"
        else "specification.condition.unproven"
    )
    assert payload["diagnostics"][0]["code"] == expected


def test_specification_rejects_tampered_condition_bindings() -> None:
    measurement = _measurement()
    measurement["source"]["conditions"][0]["value"] = 125.0
    specification = _specification()
    specification["conditions"][0]["value"] = 125.0

    payload = evaluate_specification(measurement, specification)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "specification.source.invalid"


def test_specification_with_non_string_mapping_key_fails_closed() -> None:
    specification = _specification()
    specification[7] = "not-a-json-field-name"

    payload = evaluate_specification(_measurement(), specification)

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["evaluation"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "specification.request.invalid"
    assert "field names must all be strings" in payload["diagnostics"][0]["message"]


def test_specification_margin_overflow_is_unknown_and_json_safe() -> None:
    measurement = _measurement()
    measurement["value"] = 1e308
    specification = {
        "specification_id": "output.maximum.limit",
        "measurement_id": "output.maximum",
        "limits": {
            "lower": {"value": -1e308, "unit": "V", "inclusive": True}
        },
        "conditions": deepcopy(measurement["source"]["conditions"]),
        "extensions": {},
    }

    payload = evaluate_specification(measurement, specification)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "specification.value.non_finite"
    json.dumps(payload, allow_nan=False)


def test_typed_evidence_capability_metadata_is_closed() -> None:
    assert MEASUREMENT_KINDS == (
        "sample_at",
        "minimum",
        "maximum",
        "mean",
        "rms",
        "crossing",
        "rise_time",
        "fall_time",
        "settling_time",
    )
    assert SPECIFICATION_LIMIT_KINDS == ("lower", "upper")


def test_specification_input_is_not_mutated() -> None:
    measurement = _measurement()
    specification = _specification()
    before_measurement = deepcopy(measurement)
    before_specification = deepcopy(specification)

    evaluate_specification(measurement, specification)

    assert measurement == before_measurement
    assert specification == before_specification
