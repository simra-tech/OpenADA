"""Closed AC complex-ratio measurements over provenance-bound real series."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any
import uuid

from ..contract import diagnostic, result, static_execution
from .result_measure import _InvalidRequest as _SeriesInvalidRequest
from .result_measure import _normalize_series


OPERATION_PROFILE = "openada.operation/result.transfer.measure/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/transfer.measurement.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.transfer-evidence"
IMPLEMENTATION_VERSION = "1.0.0"
METHOD_ID = "openada.method/ac-complex-ratio-log-interpolation/v1alpha1"

TRANSFER_METRIC_KINDS = (
    "low_frequency_gain_db",
    "bandwidth_3db",
    "unity_gain_frequency",
    "phase_margin",
)
_METRIC_UNITS = {
    "low_frequency_gain_db": "dB",
    "bandwidth_3db": "Hz",
    "unity_gain_frequency": "Hz",
    "phase_margin": "deg",
}
_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class _InvalidTransferRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _UnresolvedTransfer(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _closed_object(
    value: object,
    label: str,
    *,
    required: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidTransferRequest(
            "transfer.request.invalid", f"{label} must be an object."
        )
    if any(not isinstance(key, str) for key in value):
        raise _InvalidTransferRequest(
            "transfer.request.invalid", f"{label} field names must be strings."
        )
    keys = set(value)
    missing = required - keys
    extra = keys - required
    if missing:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if extra:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(extra))}.",
        )
    return value


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _extensions(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required=set())
    if item:
        raise _InvalidTransferRequest(
            "transfer.request.invalid", f"{label} must be empty in v1alpha1."
        )
    return {}


def _expect(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise _InvalidTransferRequest(
            "transfer.method.unsupported",
            f"{label} must be exactly {expected!r} for {METHOD_ID}.",
        )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise _InvalidTransferRequest(
            "transfer.request.invalid", "request_id must be a canonical UUID."
        ) from exc
    if str(parsed) != value:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            "request_id must be a canonical lowercase UUID.",
        )
    return value


def _signal_pair(value: object, label: str) -> dict[str, str]:
    pair = _closed_object(value, label, required={"real", "imaginary"})
    real = _text(pair["real"], f"{label}.real")
    imaginary = _text(pair["imaginary"], f"{label}.imaginary")
    if real == imaginary:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label}.real and {label}.imaginary must name different series.",
        )
    return {"real": real, "imaginary": imaginary}


def _normalize_request(value: object) -> dict[str, Any]:
    root = _closed_object(
        value,
        "transfer",
        required={
            "measurement_id",
            "input",
            "output",
            "interpretation",
            "method",
            "metric",
            "extensions",
        },
    )
    _extensions(root["extensions"], "transfer.extensions")
    measurement_id = _text(root["measurement_id"], "transfer.measurement_id", limit=120)
    if not _ROLE_RE.fullmatch(measurement_id):
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            "transfer.measurement_id is not a canonical identifier.",
        )

    input_pair = _signal_pair(root["input"], "transfer.input")
    output_pair = _signal_pair(root["output"], "transfer.output")
    signal_names = [*input_pair.values(), *output_pair.values()]
    if len(signal_names) != len(set(signal_names)):
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            "The four input/output Cartesian component series must have unique names.",
        )

    interpretation = _text(
        root["interpretation"], "transfer.interpretation", limit=48
    )
    if interpretation not in {"forward", "loop-gain-negative-feedback"}:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            "transfer.interpretation must be 'forward' or 'loop-gain-negative-feedback'.",
        )

    method = _closed_object(
        root["method"],
        "transfer.method",
        required={
            "id",
            "ratio",
            "phase_unwrap",
            "first_phase_range",
            "interpolation",
            "crossing_policy",
            "bandwidth_reference",
            "bandwidth_drop_db",
            "phase_margin_definition",
        },
    )
    expected_method = {
        "id": METHOD_ID,
        "ratio": "output-over-input",
        "phase_unwrap": "first-principal-then-nearest-delta",
        "first_phase_range": "[-180,180)",
        "interpolation": "linear-value-over-log10-frequency",
        "crossing_policy": "require-single-falling",
        "bandwidth_reference": "first-simulated-frequency-magnitude",
        "bandwidth_drop_db": 3.0,
        "phase_margin_definition": "180deg-plus-unwrapped-loop-phase-at-unity",
    }
    for name, expected in expected_method.items():
        _expect(method[name], expected, f"transfer.method.{name}")

    metric = _closed_object(root["metric"], "transfer.metric", required={"kind", "unit"})
    kind = _text(metric["kind"], "transfer.metric.kind", limit=48)
    if kind not in _METRIC_UNITS:
        raise _InvalidTransferRequest(
            "transfer.metric.unsupported", f"Unsupported transfer metric {kind!r}."
        )
    expected_unit = _METRIC_UNITS[kind]
    if metric["unit"] != expected_unit:
        raise _InvalidTransferRequest(
            "transfer.unit.mismatch",
            f"transfer.metric.unit must be exactly {expected_unit!r} for {kind!r}.",
        )
    if kind == "phase_margin" and interpretation != "loop-gain-negative-feedback":
        raise _InvalidTransferRequest(
            "transfer.phase_margin.invalid_context",
            "phase_margin requires interpretation 'loop-gain-negative-feedback'.",
        )

    return {
        "measurement_id": measurement_id,
        "input": input_pair,
        "output": output_pair,
        "interpretation": interpretation,
        "method": expected_method,
        "metric": {"kind": kind, "unit": expected_unit},
        "extensions": {},
    }


def _measurement_template(
    *,
    measurement_id: str | None,
    kind: str | None,
    signal: str | None,
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "kind": kind,
        "status": "unknown",
        "request_sha256": None,
        "value": None,
        "unit": _METRIC_UNITS.get(kind) if kind else None,
        "signal": signal,
        "location": None,
        "algorithm": {
            "id": (
                f"openada.algorithm/transfer.{kind.replace('_', '-')}/v1alpha1"
                if kind
                else METHOD_ID
            ),
            "version": IMPLEMENTATION_VERSION,
        },
        "sample_count": 0,
        "source": source,
        "extensions": {},
    }


def _empty_transfer() -> dict[str, Any]:
    return {
        "status": "unknown",
        "request_sha256": None,
        "method": None,
        "interpretation": None,
        "signals": None,
        "reference": None,
        "trace": None,
        "crossings": None,
        "excluded_metrics": [
            {
                "metric": "gain_margin",
                "reason": "v1alpha1 does not infer a phase crossing or gain margin.",
            }
        ],
        "extensions": {},
    }


def _payload(
    correlation_id: str,
    measurement: dict[str, Any],
    transfer: dict[str, Any],
    *,
    engineering_status: str,
    summary: str,
    execution_status: str = "completed",
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "result.transfer.measure",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=engineering_status,
        summary=summary,
        diagnostics=diagnostics,
        data={
            "protocol": {
                "request_id": correlation_id,
                "operation_profile": OPERATION_PROFILE,
                "assertion_profile": ASSERTION_PROFILE,
                "implementation_id": IMPLEMENTATION_ID,
                "implementation_version": IMPLEMENTATION_VERSION,
            },
            "measurement": measurement,
            "transfer": transfer,
            "extensions": {},
        },
    )


def _principal_phase_degrees(value: complex) -> float:
    phase = math.degrees(math.atan2(value.imag, value.real))
    if phase >= 180.0:
        phase -= 360.0
    return phase


def _unwrap_phase(values: Sequence[complex]) -> list[float]:
    phases = [_principal_phase_degrees(value) for value in values]
    unwrapped = [phases[0]]
    for phase in phases[1:]:
        candidate = phase
        previous = unwrapped[-1]
        while candidate - previous >= 180.0:
            candidate -= 360.0
        while candidate - previous < -180.0:
            candidate += 360.0
        if not math.isfinite(candidate):
            raise _UnresolvedTransfer(
                "transfer.value.non_finite", "Phase unwrapping produced a non-finite value."
            )
        unwrapped.append(candidate)
    return unwrapped


def _falling_crossings(
    frequencies: Sequence[float],
    magnitudes_db: Sequence[float],
    phases_deg: Sequence[float],
    threshold_db: float,
) -> list[dict[str, float]]:
    crossings: list[dict[str, float]] = []
    for index in range(len(frequencies) - 1):
        y0 = magnitudes_db[index]
        y1 = magnitudes_db[index + 1]
        if not (y0 > threshold_db and y1 <= threshold_db):
            continue
        fraction = (threshold_db - y0) / (y1 - y0)
        log_frequency = math.log10(frequencies[index]) + fraction * (
            math.log10(frequencies[index + 1]) - math.log10(frequencies[index])
        )
        frequency_hz = 10.0**log_frequency
        phase_deg = phases_deg[index] + fraction * (
            phases_deg[index + 1] - phases_deg[index]
        )
        if not math.isfinite(frequency_hz) or not math.isfinite(phase_deg):
            raise _UnresolvedTransfer(
                "transfer.value.non_finite",
                "Log-frequency crossing interpolation produced a non-finite value.",
            )
        crossings.append(
            {
                "frequency_hz": frequency_hz,
                "magnitude_db": threshold_db,
                "phase_deg": phase_deg,
            }
        )
    return crossings


def _crossing_record(
    crossings: Sequence[dict[str, float]], *, threshold_db: float
) -> dict[str, Any]:
    if len(crossings) == 1:
        status = "measured"
    elif crossings:
        status = "ambiguous"
    else:
        status = "not_found"
    return {
        "status": status,
        "threshold_db": threshold_db,
        "count": len(crossings),
        "candidates": list(crossings),
    }


def measure_transfer(
    series: Mapping[str, object],
    transfer: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Derive one scalar and its closed AC output-over-input ratio trace."""

    try:
        correlation_id = _request_id(request_id)
    except _InvalidTransferRequest as exc:
        correlation_id = str(uuid.uuid4())
        return _payload(
            correlation_id,
            _measurement_template(
                measurement_id=None, kind=None, signal=None, source=None
            ),
            _empty_transfer(),
            engineering_status="unknown",
            summary="The transfer correlation identity is invalid.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    normalized: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    transfer_record: dict[str, Any] = _empty_transfer()
    try:
        normalized = _normalize_series(series)
        axis = normalized["axis"]
        frequencies = axis["values"]
        if axis["unit"] != "Hz":
            raise _InvalidTransferRequest(
                "transfer.unit.mismatch", "The AC transfer axis unit must be exactly 'Hz'."
            )
        if len(frequencies) < 2:
            raise _InvalidTransferRequest(
                "transfer.source.invalid", "The AC transfer record needs at least two points."
            )
        if frequencies[0] <= 0:
            raise _InvalidTransferRequest(
                "transfer.domain.invalid",
                "Every AC frequency must be positive for log-frequency interpolation.",
            )

        request = _normalize_request(transfer)
        by_name = {signal["name"]: signal for signal in normalized["signals"]}
        requested_names = [
            request["input"]["real"],
            request["input"]["imaginary"],
            request["output"]["real"],
            request["output"]["imaginary"],
        ]
        missing = [name for name in requested_names if name not in by_name]
        if missing:
            raise _InvalidTransferRequest(
                "transfer.signal.missing",
                f"The normalized series does not contain: {', '.join(missing)}.",
            )
        units = {by_name[name]["unit"] for name in requested_names}
        if len(units) != 1:
            raise _InvalidTransferRequest(
                "transfer.unit.mismatch",
                "All four Cartesian component series must use the same unit for a dimensionless dB ratio.",
            )
        signal_unit = next(iter(units))

        input_values = [
            complex(real, imaginary)
            for real, imaginary in zip(
                by_name[request["input"]["real"]]["values"],
                by_name[request["input"]["imaginary"]]["values"],
            )
        ]
        output_values = [
            complex(real, imaginary)
            for real, imaginary in zip(
                by_name[request["output"]["real"]]["values"],
                by_name[request["output"]["imaginary"]]["values"],
            )
        ]
        ratios: list[complex] = []
        magnitudes_db: list[float] = []
        for index, (input_value, output_value) in enumerate(
            zip(input_values, output_values)
        ):
            if input_value == 0j or output_value == 0j:
                raise _UnresolvedTransfer(
                    "transfer.ratio.undefined",
                    f"A finite magnitude/phase trace cannot be represented at AC point {index}; zero input or output magnitude is not floored in v1alpha1.",
                )
            ratio = output_value / input_value
            magnitude_db = 20.0 * math.log10(abs(ratio))
            if not (
                math.isfinite(ratio.real)
                and math.isfinite(ratio.imag)
                and math.isfinite(magnitude_db)
            ):
                raise _UnresolvedTransfer(
                    "transfer.value.non_finite",
                    f"The complex ratio produced a non-finite value at AC point {index}.",
                )
            ratios.append(ratio)
            magnitudes_db.append(magnitude_db)
        phases_deg = _unwrap_phase(ratios)

        request_sha256 = _canonical_sha256(request)
        low_frequency_gain_db = magnitudes_db[0]
        bandwidth_threshold_db = low_frequency_gain_db - 3.0
        bandwidth_crossings = _falling_crossings(
            frequencies, magnitudes_db, phases_deg, bandwidth_threshold_db
        )
        unity_crossings = _falling_crossings(
            frequencies, magnitudes_db, phases_deg, 0.0
        )
        bandwidth_record = _crossing_record(
            bandwidth_crossings, threshold_db=bandwidth_threshold_db
        )
        unity_record = _crossing_record(unity_crossings, threshold_db=0.0)
        signal_expression = "complex-output-over-input"
        transfer_record = {
            "status": "analyzed",
            "request_sha256": request_sha256,
            "method": request["method"],
            "interpretation": request["interpretation"],
            "signals": {
                "input": {**request["input"], "unit": signal_unit},
                "output": {**request["output"], "unit": signal_unit},
                "ratio": "output-over-input",
            },
            "reference": {
                "kind": "first-simulated-frequency-not-dc",
                "frequency_hz": frequencies[0],
                "magnitude_db": low_frequency_gain_db,
            },
            "trace": {
                "frequency_hz": frequencies,
                "magnitude_db": magnitudes_db,
                "phase_deg": phases_deg,
                "phase_representation": "unwrapped-degrees",
            },
            "crossings": {
                "bandwidth_3db": bandwidth_record,
                "unity_gain": unity_record,
            },
            "excluded_metrics": [
                {
                    "metric": "gain_margin",
                    "reason": "v1alpha1 does not infer a phase crossing or gain margin.",
                }
            ],
            "extensions": {},
        }
        kind = request["metric"]["kind"]
        measurement = _measurement_template(
            measurement_id=request["measurement_id"],
            kind=kind,
            signal=signal_expression,
            source=normalized["source"],
        )
        measurement.update(
            {
                "request_sha256": request_sha256,
                "sample_count": len(frequencies),
            }
        )

        selected_crossing: dict[str, Any] | None = None
        if kind == "low_frequency_gain_db":
            value = low_frequency_gain_db
            location_hz = frequencies[0]
        elif kind == "bandwidth_3db":
            selected_crossing = bandwidth_record
            value = (
                bandwidth_crossings[0]["frequency_hz"]
                if len(bandwidth_crossings) == 1
                else None
            )
            location_hz = value
        else:
            selected_crossing = unity_record
            if len(unity_crossings) == 1:
                location_hz = unity_crossings[0]["frequency_hz"]
                value = (
                    location_hz
                    if kind == "unity_gain_frequency"
                    else 180.0 + unity_crossings[0]["phase_deg"]
                )
            else:
                value = None
                location_hz = None

        if selected_crossing is not None and selected_crossing["status"] == "not_found":
            measurement["status"] = "not_found"
            transfer_record["status"] = "crossing_not_found"
            return _payload(
                correlation_id,
                measurement,
                transfer_record,
                engineering_status="fail",
                summary="The valid AC trace contains no requested falling crossing.",
                diagnostics=[
                    diagnostic(
                        "error",
                        "transfer.crossing.not_found",
                        "No falling crossing satisfies the declared threshold and closed crossing policy.",
                    )
                ],
            )
        if selected_crossing is not None and selected_crossing["status"] == "ambiguous":
            measurement["status"] = "unknown"
            transfer_record["status"] = "crossing_ambiguous"
            return _payload(
                correlation_id,
                measurement,
                transfer_record,
                engineering_status="unknown",
                summary="Multiple falling crossings make the requested transfer scalar ambiguous.",
                diagnostics=[
                    diagnostic(
                        "error",
                        "transfer.crossing.ambiguous",
                        "The require-single-falling policy found multiple candidate crossings; v1alpha1 does not select one implicitly.",
                    )
                ],
            )
        if value is None or not math.isfinite(value):
            raise _UnresolvedTransfer(
                "transfer.value.non_finite",
                "The requested transfer scalar is not finite.",
            )
        measurement["status"] = "measured"
        measurement["value"] = value
        measurement["location"] = {"value": location_hz, "unit": "Hz"}
        return _payload(
            correlation_id,
            measurement,
            transfer_record,
            engineering_status="pass",
            summary=f"The closed {kind!r} AC transfer measurement was derived.",
        )
    except _SeriesInvalidRequest as exc:
        error: _InvalidTransferRequest | _UnresolvedTransfer = _InvalidTransferRequest(
            "transfer.source.invalid", str(exc)
        )
        execution_status = "invalid_request"
    except _InvalidTransferRequest as exc:
        error = exc
        execution_status = "invalid_request"
    except _UnresolvedTransfer as exc:
        error = exc
        execution_status = "completed"
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        error = _UnresolvedTransfer(
            "transfer.value.non_finite", f"The transfer calculation failed safely: {exc}"
        )
        execution_status = "completed"

    measurement_id = request["measurement_id"] if request is not None else None
    kind = request["metric"]["kind"] if request is not None else None
    source = normalized["source"] if normalized is not None else None
    signal = None
    if request is not None:
        signal = "complex-output-over-input"
    return _payload(
        correlation_id,
        _measurement_template(
            measurement_id=measurement_id,
            kind=kind,
            signal=signal,
            source=source,
        ),
        transfer_record,
        engineering_status="unknown",
        summary="The AC transfer measurement could not be established.",
        execution_status=execution_status,
        diagnostics=[diagnostic("error", error.code, str(error))],
    )


__all__ = ["TRANSFER_METRIC_KINDS", "measure_transfer"]
