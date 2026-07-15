from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.operations.result_measure import normalized_series_sha256
from openada.operations.result_spectral_measure import measure_spectrum
from openada.operations.specification_evaluate import evaluate_specification


ROOT = Path(__file__).parents[1]
RESULT_SCHEMA = json.loads(
    (ROOT / "schemas" / "result-v0alpha1.schema.json").read_text(encoding="utf-8")
)
RESULT_VALIDATOR = Draft202012Validator(RESULT_SCHEMA, format_checker=FormatChecker())
SPECTRAL_PROFILE = json.loads(
    (ROOT / "profiles" / "result.spectral.measure-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
SPECTRAL_DATA_VALIDATOR = Draft202012Validator(
    SPECTRAL_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)


def _series(*, count: int = 1024, sample_rate_hz: float = 1024.0) -> dict:
    axis = {
        "name": "time",
        "unit": "s",
        "values": [index / sample_rate_hz for index in range(count)],
    }
    values = [
        math.sin(2.0 * math.pi * 37 * index / count)
        + 0.01 * math.sin(2.0 * math.pi * 74 * index / count)
        + 0.001 * math.sin(2.0 * math.pi * 113 * index / count)
        + 0.0005 * math.sin(2.0 * math.pi * 211 * index / count)
        for index in range(count)
    ]
    signals = [{"name": "v(out)", "unit": "V", "values": values}]
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


def _request(kind: str = "snr", *, domain: str = "generic-sampled-waveform") -> dict:
    references = {
        "generic-sampled-waveform": ("none", "openada-definition"),
        "adc": ("ieee-1241-2023", "candidate"),
        "dac": ("ieee-1658-2023", "candidate"),
        "waveform-recorder": ("ieee-1057-2017", "candidate"),
    }
    reference, alignment = references[domain]
    return {
        "measurement_id": f"output.{kind}",
        "signal": "v(out)",
        "method": {
            "id": "openada.method/coherent-single-tone-fft/v1alpha1",
            "dft_length": 1024,
            "uniformity_relative_tolerance": 1e-12,
            "coherent_bin_tolerance": 1e-12,
            "coherent_sampling": "required",
            "window": "rectangular",
            "detrend": "mean",
            "sidedness": "one-sided",
            "scaling": "mean-square-per-bin",
            "averaging": "none",
            "missing_samples": "reject",
            "clipping": "not-assessed",
        },
        "band": {
            "lower": {"value": 0.0, "unit": "Hz"},
            "upper": {"value": 512.0, "unit": "Hz"},
        },
        "fundamental": {
            "method": "declared-coherent-bin",
            "frequency": {"value": 37.0, "unit": "Hz"},
            "integration_half_width_bins": 0,
        },
        "harmonics": {
            "orders": [2],
            "aliasing": "fold-first-nyquist",
            "collision": "reject",
            "out_of_band": "exclude",
            "integration_half_width_bins": 0,
        },
        "metric": {"kind": kind, "unit": "dB"},
        "standards_context": {
            "domain": domain,
            "reference": reference,
            "alignment": alignment,
        },
        "extensions": {},
    }


def _assert_envelope(payload: dict) -> None:
    errors = sorted(RESULT_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)
    data_errors = sorted(
        SPECTRAL_DATA_VALIDATOR.iter_errors(payload["data"]),
        key=lambda item: list(item.path),
    )
    assert not data_errors, "\n".join(error.message for error in data_errors)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("snr", 10.0 * math.log10(0.5 / 0.000000625)),
        ("sinad", 10.0 * math.log10(0.5 / 0.000050625)),
        ("thd", -40.0),
        ("sfdr", 40.0),
    ],
)
def test_closed_coherent_metrics_share_one_partition(kind: str, expected: float) -> None:
    payload = measure_spectrum(_series(), _request(kind))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurement"]["status"] == "measured"
    assert payload["data"]["measurement"]["value"] == pytest.approx(expected, abs=1e-9)
    spectral = payload["data"]["spectral"]
    assert spectral["fundamental"]["bin"] == 37
    assert spectral["harmonics"][0]["bin"] == 74
    assert spectral["partition"]["winning_spur"]["bin"] == 74
    assert spectral["partition"]["sha256"]


def test_adc_context_is_candidate_not_a_conformance_claim() -> None:
    payload = measure_spectrum(_series(), _request("sinad", domain="adc"))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["spectral"]["standards_context"] == {
        "domain": "adc",
        "reference": "ieee-1241-2023",
        "alignment": "candidate",
    }


def _replace_signal(series: dict, values: list[float]) -> None:
    series["signals"][0]["values"] = values
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"],
        signals=series["signals"],
        conditions=series["conditions"],
    )


def test_harmonic_alias_folding_and_out_of_band_exclusion_are_explicit() -> None:
    count = 1024
    folded = _series()
    _replace_signal(
        folded,
        [
            math.sin(2.0 * math.pi * 300 * index / count)
            + 0.01 * math.sin(2.0 * math.pi * 424 * index / count)
            for index in range(count)
        ],
    )
    folded_request = _request("thd")
    folded_request["fundamental"]["frequency"]["value"] = 300.0

    folded_payload = measure_spectrum(folded, folded_request)

    _assert_envelope(folded_payload)
    harmonic = folded_payload["data"]["spectral"]["harmonics"][0]
    assert folded_payload["engineering"]["status"] == "pass"
    assert harmonic == pytest.approx(
        {
            "order": 2,
            "source_frequency_hz": 600.0,
            "folded_frequency_hz": 424.0,
            "bin": 424,
            "included": True,
            "power": 0.00005,
        }
    )

    excluded_request = _request("thd")
    excluded_request["harmonics"]["orders"] = [2, 3]
    excluded_request["band"]["upper"]["value"] = 100.0
    excluded_payload = measure_spectrum(_series(), excluded_request)

    _assert_envelope(excluded_payload)
    excluded = excluded_payload["data"]["spectral"]["harmonics"][1]
    assert excluded["order"] == 3
    assert excluded["bin"] == 111
    assert excluded["included"] is False
    assert excluded["power"] is None


def test_colliding_folded_harmonics_are_rejected() -> None:
    count = 1024
    series = _series()
    _replace_signal(
        series,
        [math.sin(2.0 * math.pi * 128 * index / count) for index in range(count)],
    )
    request = _request("thd")
    request["fundamental"]["frequency"]["value"] = 128.0
    request["harmonics"]["orders"] = [2, 6]

    payload = measure_spectrum(series, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "spectral.harmonic.collision"


def test_sfdr_nonharmonic_winner_tie_break_and_empty_candidate_set() -> None:
    count = 1024
    nonharmonic = _series()
    _replace_signal(
        nonharmonic,
        [
            math.sin(2.0 * math.pi * 37 * index / count)
            + 0.01 * math.sin(2.0 * math.pi * 74 * index / count)
            + 0.02 * math.sin(2.0 * math.pi * 113 * index / count)
            for index in range(count)
        ],
    )
    nonharmonic_payload = measure_spectrum(nonharmonic, _request("sfdr"))
    assert nonharmonic_payload["data"]["spectral"]["partition"]["winning_spur"][
        "bin"
    ] == 113

    tie = _series(count=8, sample_rate_hz=8.0)
    _replace_signal(
        tie,
        [
            math.sin(2.0 * math.pi * index / 8)
            + 0.1 * math.cos(2.0 * math.pi * 2 * index / 8)
            + 0.1 * math.cos(2.0 * math.pi * 3 * index / 8)
            for index in range(8)
        ],
    )
    tie_request = _request("sfdr")
    tie_request["method"]["dft_length"] = 8
    tie_request["fundamental"]["frequency"]["value"] = 1.0
    tie_request["harmonics"]["orders"] = []
    tie_request["band"]["upper"]["value"] = 4.0
    tie_payload = measure_spectrum(tie, tie_request)
    _assert_envelope(tie_payload)
    assert tie_payload["data"]["spectral"]["partition"]["winning_spur"]["bin"] == 2

    empty_request = deepcopy(tie_request)
    empty_request["band"] = {
        "lower": {"value": 0.5, "unit": "Hz"},
        "upper": {"value": 1.5, "unit": "Hz"},
    }
    empty_payload = measure_spectrum(tie, empty_request)
    _assert_envelope(empty_payload)
    assert empty_payload["engineering"]["status"] == "unknown"
    assert empty_payload["data"]["spectral"]["partition"]["winning_spur"] is None
    assert empty_payload["diagnostics"][0]["code"] == "spectral.metric.unbounded"


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda request, series: request["fundamental"]["frequency"].update(
                value=37.25
            ),
            "spectral.coherence.not_established",
        ),
        (
            lambda request, series: series["axis"]["values"].__setitem__(10, 10.25 / 1024),
            "spectral.source.invalid",
        ),
        (
            lambda request, series: request["method"].update(window="hann"),
            "spectral.method.unsupported",
        ),
        (
            lambda request, series: request["standards_context"].update(
                domain="adc", reference="ieee-1241-2023", alignment="conformant"
            ),
            "spectral.standard_context.invalid",
        ),
        (
            lambda request, series: request["harmonics"].update(orders=[2, 2]),
            "spectral.request.invalid",
        ),
    ],
)
def test_ambiguous_or_invalid_method_inputs_fail_closed(mutator, code: str) -> None:
    series = _series()
    request = _request()
    mutator(request, series)

    payload = measure_spectrum(series, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == code
    assert payload["data"]["measurement"]["value"] is None


@pytest.mark.parametrize("section", ["fundamental", "harmonics"])
def test_boolean_zero_width_is_rejected_consistently_with_the_profile(
    section: str,
) -> None:
    request = _request()
    request[section]["integration_half_width_bins"] = False

    payload = measure_spectrum(_series(), request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "spectral.request.invalid"


def test_irregular_axis_with_recomputed_digest_reaches_sampling_check() -> None:
    series = _series()
    series["axis"]["values"][10] = 10.25 / 1024
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"], signals=series["signals"], conditions=series["conditions"]
    )

    payload = measure_spectrum(series, _request())

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "spectral.sampling.non_uniform"


def test_zero_residual_is_reported_as_unbounded_not_infinity() -> None:
    series = _series()
    count = len(series["axis"]["values"])
    series["signals"][0]["values"] = [
        math.sin(2.0 * math.pi * 37 * index / count) for index in range(count)
    ]
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"], signals=series["signals"], conditions=series["conditions"]
    )

    request = _request("thd")
    request["harmonics"]["orders"] = []
    payload = measure_spectrum(series, request)

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["measurement"]["value"] is None
    assert payload["diagnostics"][0]["code"] == "spectral.metric.unbounded"


def test_absent_declared_fundamental_is_a_bounded_not_found_result() -> None:
    series = _series()
    series["signals"][0]["values"] = [0.0] * len(series["axis"]["values"])
    series["source"]["artifact_sha256"] = normalized_series_sha256(
        axis=series["axis"], signals=series["signals"], conditions=series["conditions"]
    )

    payload = measure_spectrum(series, _request("sinad"))

    _assert_envelope(payload)
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["measurement"]["status"] == "not_found"
    assert payload["diagnostics"][0]["code"] == "spectral.fundamental.not_found"


def test_spectral_measurement_can_feed_the_existing_specification_kernel() -> None:
    measured = measure_spectrum(_series(), _request("sfdr"))["data"]["measurement"]
    specification = {
        "specification_id": "linearity.sfdr",
        "measurement_id": "output.sfdr",
        "limits": {"lower": {"value": 35.0, "unit": "dB", "inclusive": True}},
        "conditions": [
            {"name": "temperature", "value": 27.0, "unit": "degC"},
            {"name": "corner", "value": "tt", "unit": "1"},
        ],
        "extensions": {},
    }

    evaluated = evaluate_specification(measured, specification)

    assert evaluated["engineering"]["status"] == "pass"
    assert evaluated["data"]["evaluation"]["status"] == "pass"


def test_request_and_source_objects_are_not_mutated() -> None:
    series = _series()
    request = _request()
    before_series = deepcopy(series)
    before_request = deepcopy(request)

    measure_spectrum(series, request)

    assert series == before_series
    assert request == before_request
