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
    (ROOT / "profiles" / "result.transfer.measure-v1alpha2.json").read_text(
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
        "low_frequency_impedance": "Ohm",
        "ac_magnitude_at_frequency": "dB",
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
            "magnitude": 10.0,
            "unit": "1",
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


# --------------------------------------------------------------------------
# Derived ratios: the two shapes a live sweep asked for and could not express.
#
# A cascode mirror's output impedance and a differential pair's differential
# gain both stalled at `extract` on the deployed release, because every metric
# was a dB threshold on one identical unit and every operand was a single
# terminal. Both are ratios; neither was expressible.
# --------------------------------------------------------------------------


def _cartesian_series(
    signals: dict[str, tuple[str, list[complex]]],
    *,
    frequencies_hz: tuple[float, ...] = (1.0, 10.0, 100.0, 1000.0),
) -> dict:
    """A normalized AC series from ``{stem: (unit, phasors)}``."""

    axis = {"name": "frequency", "unit": "Hz", "values": list(frequencies_hz)}
    vectors = []
    for stem, (unit, values) in signals.items():
        assert len(values) == len(frequencies_hz)
        vectors.append(
            {"name": f"{stem}.real", "unit": unit, "values": [v.real for v in values]}
        )
        vectors.append(
            {"name": f"{stem}.imag", "unit": unit, "values": [v.imag for v in values]}
        )
    conditions = [{"name": "corner", "value": "tt", "unit": "1"}]
    return {
        "source": {
            "operation": "result.series.extract",
            "request_id": str(uuid.uuid4()),
            "artifact_role": "measurement.source",
            "artifact_sha256": normalized_series_sha256(
                axis=axis, signals=vectors, conditions=conditions
            ),
            "lineage": {
                "operation": "simulate",
                "request_id": str(uuid.uuid4()),
                "artifact_role": "simulation.result",
                "artifact_sha256": "b" * 64,
                "binding": "unverified",
            },
        },
        "axis": axis,
        "signals": vectors,
        "conditions": conditions,
        "extensions": {},
    }


def _operand(stem: str, negative: str | None = None) -> dict:
    operand = {"real": f"{stem}.real", "imaginary": f"{stem}.imag"}
    if negative is not None:
        operand["negative_real"] = f"{negative}.real"
        operand["negative_imaginary"] = f"{negative}.imag"
    return operand


def _impedance_series(magnitude_ohm: float = 1.5e6) -> dict:
    points = 4
    return _cartesian_series(
        {
            "vout": ("V", [complex(1.0, 0.0)] * points),
            "iout": ("A", [complex(1.0 / magnitude_ohm, 0.0)] * points),
        }
    )


def test_low_frequency_impedance_is_a_linear_ohm_magnitude_not_a_db_ratio() -> None:
    request = _request("low_frequency_impedance")
    request["input"] = _operand("iout")
    request["output"] = _operand("vout")

    payload = measure_transfer(_impedance_series(), request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    measurement = payload["data"]["measurement"]
    assert measurement["status"] == "measured"
    assert measurement["unit"] == "Ohm"
    assert measurement["value"] == pytest.approx(1.5e6)
    assert measurement["location"] == {"value": 1.0, "unit": "Hz"}
    assert measurement["algorithm"]["id"] == (
        "openada.algorithm/transfer.low-frequency-impedance/v1alpha1"
    )
    reference = payload["data"]["transfer"]["reference"]
    assert reference["magnitude"] == pytest.approx(1.5e6)
    assert reference["unit"] == "Ohm"
    assert payload["data"]["transfer"]["signals"]["output"]["unit"] == "V"
    assert payload["data"]["transfer"]["signals"]["input"]["unit"] == "A"


def test_impedance_request_schema_admits_the_ohm_metric() -> None:
    request = _request("low_frequency_impedance")
    request["input"] = _operand("iout")
    request["output"] = _operand("vout")
    parameters = {
        "series": _impedance_series(),
        "transfer": request,
        "extensions": {},
    }

    errors = sorted(
        REQUEST_VALIDATOR.iter_errors(parameters), key=lambda item: list(item.path)
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_impedance_refuses_operands_that_are_not_volts_over_amperes() -> None:
    request = _request("low_frequency_impedance")
    request["input"] = _operand("vout")
    request["output"] = _operand("iout")

    payload = measure_transfer(_impedance_series(), request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["data"]["measurement"]["value"] is None
    assert payload["diagnostics"][0]["code"] == "transfer.unit.mismatch"


def test_a_db_gain_still_refuses_unlike_operand_units() -> None:
    request = _request("low_frequency_gain_db")
    request["input"] = _operand("iout")
    request["output"] = _operand("vout")

    payload = measure_transfer(_impedance_series(), request)

    _assert_envelope(payload)
    assert payload["diagnostics"][0]["code"] == "transfer.unit.mismatch"
    assert "impedance" in payload["diagnostics"][0]["message"]


def test_differential_operands_measure_the_difference_of_two_terminals() -> None:
    # A single-ended-driven pair: vinp swings, vinn is AC ground, and the
    # outputs move by -2 and +2. Single-ended is 6.02 dB; differential is 12.04.
    points = 4
    series = _cartesian_series(
        {
            "vinp": ("V", [complex(1.0, 0.0)] * points),
            "vinn": ("V", [complex(0.0, 0.0)] * points),
            "voutp": ("V", [complex(2.0, 0.0)] * points),
            "voutn": ("V", [complex(-2.0, 0.0)] * points),
        }
    )
    request = _request("low_frequency_gain_db")
    request["input"] = _operand("vinp", "vinn")
    request["output"] = _operand("voutp", "voutn")

    payload = measure_transfer(series, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    measurement = payload["data"]["measurement"]
    assert measurement["value"] == pytest.approx(20.0 * math.log10(4.0))
    assert measurement["signal"] == "complex-differential-output-over-input"
    signals = payload["data"]["transfer"]["signals"]
    assert signals["output"]["negative_real"] == "voutn.real"
    assert signals["output"]["negative_imaginary"] == "voutn.imag"

    single_ended = _request("low_frequency_gain_db")
    single_ended["input"] = _operand("vinp")
    single_ended["output"] = _operand("voutp")
    single = measure_transfer(series, single_ended)
    assert single["data"]["measurement"]["value"] == pytest.approx(
        20.0 * math.log10(2.0)
    )
    assert single["data"]["measurement"]["signal"] == "complex-output-over-input"


def test_differential_request_schema_admits_both_negative_components() -> None:
    points = 4
    series = _cartesian_series(
        {
            "vinp": ("V", [complex(1.0, 0.0)] * points),
            "vinn": ("V", [complex(0.0, 0.0)] * points),
            "voutp": ("V", [complex(2.0, 0.0)] * points),
            "voutn": ("V", [complex(-2.0, 0.0)] * points),
        }
    )
    request = _request("low_frequency_gain_db")
    request["input"] = _operand("vinp", "vinn")
    request["output"] = _operand("voutp", "voutn")

    errors = sorted(
        REQUEST_VALIDATOR.iter_errors(
            {"series": series, "transfer": request, "extensions": {}}
        ),
        key=lambda item: list(item.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_half_a_differential_terminal_is_refused_by_code_and_by_schema() -> None:
    request = _request("low_frequency_gain_db")
    request["output"] = _operand("vout", "vin")
    del request["output"]["negative_imaginary"]

    payload = measure_transfer(_series(), request)

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "transfer.request.invalid"
    assert "negative_imaginary" in payload["diagnostics"][0]["message"]

    errors = list(
        REQUEST_VALIDATOR.iter_errors(
            {"series": _series(), "transfer": request, "extensions": {}}
        )
    )
    assert errors


def test_a_differential_operand_may_not_reuse_one_series_twice() -> None:
    request = _request("low_frequency_gain_db")
    request["output"] = _operand("vout", "vout")

    payload = measure_transfer(_series(), request)

    _assert_envelope(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "transfer.request.invalid"


def test_every_advertised_metric_kind_has_a_declared_feature_and_unit() -> None:
    from openada.operations.result_transfer_measure import (
        TRANSFER_METRIC_KINDS,
        _METRIC_UNITS,
    )

    declared = {
        value
        for feature in TRANSFER_PROFILE["features"]
        if feature["parameter_path"] == "transfer.metric.kind"
        for value in feature["parameter_values"]
    }
    assert declared == set(TRANSFER_METRIC_KINDS)
    assert set(_METRIC_UNITS) == set(TRANSFER_METRIC_KINDS)


def _at_request(at_hz: float) -> dict:
    request = _request("ac_magnitude_at_frequency")
    request["metric"]["at"] = {"value": at_hz, "unit": "Hz"}
    return request


def test_ac_magnitude_exact_axis_hit_returns_simulated_value() -> None:
    payload = measure_transfer(_series(), _at_request(100.0))
    measured = payload["data"]["measurement"]
    assert payload["engineering"]["status"] == "pass"
    assert measured["value"] == pytest.approx(-5.0)
    assert measured["unit"] == "dB"
    assert measured["location"] == {"value": 100.0, "unit": "Hz"}
    _assert_envelope(payload)


def test_ac_magnitude_interpolates_db_over_log10_frequency() -> None:
    # The log midpoint of 10 and 100 Hz interpolates the dB midpoint of
    # 15 and -5 dB, exactly as the frozen method's interpolation declares.
    payload = measure_transfer(_series(), _at_request(math.sqrt(10.0 * 100.0)))
    measured = payload["data"]["measurement"]
    assert payload["engineering"]["status"] == "pass"
    assert measured["value"] == pytest.approx(5.0)
    _assert_envelope(payload)


def test_ac_magnitude_domain_endpoints_are_in_domain() -> None:
    for at_hz, expected_db in ((1.0, 20.0), (1000.0, -20.0)):
        payload = measure_transfer(_series(), _at_request(at_hz))
        assert payload["engineering"]["status"] == "pass"
        assert payload["data"]["measurement"]["value"] == pytest.approx(expected_db)


@pytest.mark.parametrize("at_hz", [0.5, 2000.0])
def test_ac_magnitude_out_of_domain_is_invalid_not_not_found(at_hz: float) -> None:
    payload = measure_transfer(_series(), _at_request(at_hz))
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "transfer.domain.invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["metric"].pop("at"),
        lambda request: request["metric"]["at"].update({"unit": "kHz"}),
        lambda request: request["metric"]["at"].update({"value": 0.0}),
        lambda request: request["metric"]["at"].update({"value": -10.0}),
        lambda request: request["metric"]["at"].update({"value": True}),
    ],
)
def test_ac_magnitude_malformed_at_is_refused(mutate) -> None:
    request = _at_request(100.0)
    mutate(request)
    payload = measure_transfer(_series(), request)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] in {
        "transfer.request.invalid",
        "transfer.unit.mismatch",
    }


def test_at_is_forbidden_for_every_other_metric_kind() -> None:
    request = _request("low_frequency_gain_db")
    request["metric"]["at"] = {"value": 100.0, "unit": "Hz"}
    payload = measure_transfer(_series(), request)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "transfer.request.invalid"


def test_ac_magnitude_request_digest_includes_at() -> None:
    a = measure_transfer(_series(), _at_request(100.0))
    b = measure_transfer(_series(), _at_request(10.0))
    assert (
        a["data"]["measurement"]["request_sha256"]
        != b["data"]["measurement"]["request_sha256"]
    )


def test_unrepresentable_integer_at_is_an_invalid_request() -> None:
    request = _at_request(100.0)
    request["metric"]["at"]["value"] = 10**309
    payload = measure_transfer(_series(), request)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "transfer.request.invalid"


def test_log_colliding_adjacent_frequencies_still_interpolate() -> None:
    # Adjacent frequencies near 1e300 whose log10 values round to the same
    # double: the difference form divides by zero; the log-RATIO form keeps
    # the tiny nonzero spacing and interpolation stays defined.
    f0 = 1e300
    f1 = 1e300 * (1.0 + 8e-16)
    at = 1e300 * (1.0 + 4e-16)
    assert f0 < at < f1
    series = _series(
        magnitudes_db=(0.0, 6.0),
        phases_deg=(0.0, 0.0),
        frequencies_hz=(f0, f1),
    )
    payload = measure_transfer(series, _at_request(at))
    measured = payload["data"]["measurement"]
    assert payload["engineering"]["status"] == "pass"
    assert math.isfinite(measured["value"])
    assert 0.0 <= measured["value"] <= 6.0


def test_close_collision_interpolation_matches_the_expected_fraction() -> None:
    f0 = 1e300
    f1 = 1e300 * (1.0 + 8e-16)
    at = 1e300 * (1.0 + 4e-16)
    series = _series(
        magnitudes_db=(0.0, 6.0), phases_deg=(0.0, 0.0), frequencies_hz=(f0, f1)
    )
    payload = measure_transfer(series, _at_request(at))
    expected = 6.0 * (math.log10(at / f0) / math.log10(f1 / f0))
    assert payload["data"]["measurement"]["value"] == pytest.approx(
        expected, rel=1e-6
    )


def test_wide_span_interpolation_does_not_overflow() -> None:
    # The pure ratio form overflows f1/f0 here and silently returned the
    # left endpoint; the hybrid must interpolate the true midpoint.
    series = _series(
        magnitudes_db=(0.0, 6.0),
        phases_deg=(0.0, 0.0),
        frequencies_hz=(1e-300, 1e300),
    )
    payload = measure_transfer(series, _at_request(1.0))
    assert payload["data"]["measurement"]["value"] == pytest.approx(3.0)


def test_ordinary_spacing_agrees_with_the_difference_form() -> None:
    f0, f1, at = 10.0, 100.0, 31.622776601683793
    series = _series(
        magnitudes_db=(0.0, 6.0), phases_deg=(0.0, 0.0), frequencies_hz=(f0, f1)
    )
    payload = measure_transfer(series, _at_request(at))
    expected = 6.0 * (
        (math.log10(at) - math.log10(f0)) / (math.log10(f1) - math.log10(f0))
    )
    assert payload["data"]["measurement"]["value"] == pytest.approx(
        expected, rel=1e-12
    )
