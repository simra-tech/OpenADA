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


OPERATION_PROFILE = "openada.operation/result.transfer.measure/v1alpha2"
ASSERTION_PROFILE = "openada.assertion/transfer.measurement.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.transfer-evidence"
IMPLEMENTATION_VERSION = "1.1.0"
METHOD_ID = "openada.method/ac-complex-ratio-log-interpolation/v1alpha1"

TRANSFER_METRIC_KINDS = (
    "low_frequency_gain_db",
    "low_frequency_impedance",
    "ac_magnitude_at_frequency",
    "bandwidth_3db",
    "unity_gain_frequency",
    "phase_margin",
)
_METRIC_UNITS = {
    "low_frequency_gain_db": "dB",
    "low_frequency_impedance": "Ohm",
    "ac_magnitude_at_frequency": "dB",
    "bandwidth_3db": "Hz",
    "unity_gain_frequency": "Hz",
    "phase_margin": "deg",
}

#: ``low_frequency_impedance`` is the one metric whose operands are *not*
#: dimensionally alike. Every other kind is a dB threshold on a dimensionless
#: ratio and requires one identical unit on all components; a driving-point
#: impedance is volts over amperes and is refused unless the operands say so.
#: The pair is stated here rather than inferred, because "the numerator happens
#: to be in V" is not the same claim as "this ratio is an impedance".
_METRIC_OPERAND_UNITS = {"low_frequency_impedance": ("V", "A")}

#: The optional second terminal of an operand. Present in pairs or not at all:
#: a differential phasor is ``(real + j*imaginary) - (negative_real +
#: j*negative_imaginary)``, and half of that is not a terminal.
_DIFFERENTIAL_KEYS = ("negative_real", "negative_imaginary")

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
    optional: set[str] = frozenset(),
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
    extra = keys - required - set(optional)
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
    """One operand phasor: a single-ended terminal, or a differential pair.

    A single-ended operand names the Cartesian components of one node. A
    differential operand additionally names the negative terminal's, and the
    phasor is their difference -- which is the only way to express ``v(outp) -
    v(outn)``, and therefore the only way a fully differential stage's gain
    becomes a typed measurement rather than arithmetic in an answer.
    """

    pair = _closed_object(
        value, label, required={"real", "imaginary"}, optional=set(_DIFFERENTIAL_KEYS)
    )
    operand = {
        "real": _text(pair["real"], f"{label}.real"),
        "imaginary": _text(pair["imaginary"], f"{label}.imaginary"),
    }
    declared = [key for key in _DIFFERENTIAL_KEYS if key in pair]
    if len(declared) == 1:
        missing = next(key for key in _DIFFERENTIAL_KEYS if key not in pair)
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label} declares {declared[0]!r} without {missing!r}; a differential "
            "terminal needs both Cartesian components or neither.",
        )
    for key in declared:
        operand[key] = _text(pair[key], f"{label}.{key}")
    if len(set(operand.values())) != len(operand):
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            f"{label} must name a different series for each Cartesian component.",
        )
    return operand


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
            "Every input/output Cartesian component series must have a unique name.",
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

    metric = _closed_object(
        root["metric"], "transfer.metric", required={"kind", "unit"}, optional={"at"}
    )
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
    normalized_metric: dict[str, Any] = {"kind": kind, "unit": expected_unit}
    if kind == "ac_magnitude_at_frequency":
        if "at" not in metric:
            raise _InvalidTransferRequest(
                "transfer.request.invalid",
                "transfer.metric.at is required for 'ac_magnitude_at_frequency'.",
            )
        at_item = _closed_object(
            metric["at"], "transfer.metric.at", required={"value", "unit"}
        )
        if at_item["unit"] != "Hz":
            raise _InvalidTransferRequest(
                "transfer.unit.mismatch",
                "transfer.metric.at.unit must be exactly 'Hz'.",
            )
        at_value = at_item["value"]
        if isinstance(at_value, bool) or not isinstance(at_value, (int, float)):
            raise _InvalidTransferRequest(
                "transfer.request.invalid",
                "transfer.metric.at.value must be one finite positive number.",
            )
        try:
            at_number = float(at_value)
        except OverflowError as exc:
            # A JSON-schema-valid arbitrary-precision integer such as
            # 10**309 is not representable; that is a request defect, not
            # a completed non-finite calculation.
            raise _InvalidTransferRequest(
                "transfer.request.invalid",
                "transfer.metric.at.value must be one finite positive number.",
            ) from exc
        if not math.isfinite(at_number) or at_number <= 0:
            raise _InvalidTransferRequest(
                "transfer.request.invalid",
                "transfer.metric.at.value must be one finite positive number.",
            )
        normalized_metric["at"] = {"value": at_number, "unit": "Hz"}
    elif "at" in metric:
        raise _InvalidTransferRequest(
            "transfer.request.invalid",
            "transfer.metric.at is only declared for 'ac_magnitude_at_frequency', "
            f"not {kind!r}.",
        )

    return {
        "measurement_id": measurement_id,
        "input": input_pair,
        "output": output_pair,
        "interpretation": interpretation,
        "method": expected_method,
        "metric": normalized_metric,
        "extensions": {},
    }


def _operand_unit(
    operand: Mapping[str, str], by_name: Mapping[str, Any], label: str
) -> str:
    """The one unit every Cartesian component of ``operand`` carries."""

    units = {by_name[name]["unit"] for name in operand.values()}
    if len(units) != 1:
        raise _InvalidTransferRequest(
            "transfer.unit.mismatch",
            f"Every Cartesian component of {label} must carry the same unit; "
            f"the declared series carry {', '.join(sorted(units))}.",
        )
    return next(iter(units))


def _operand_phasors(
    operand: Mapping[str, str], by_name: Mapping[str, Any]
) -> list[complex]:
    """The operand's phasor at each AC point, differential terminals included."""

    positive = [
        complex(real, imaginary)
        for real, imaginary in zip(
            by_name[operand["real"]]["values"],
            by_name[operand["imaginary"]]["values"],
        )
    ]
    if "negative_real" not in operand:
        return positive
    negative = [
        complex(real, imaginary)
        for real, imaginary in zip(
            by_name[operand["negative_real"]]["values"],
            by_name[operand["negative_imaginary"]]["values"],
        )
    ]
    return [
        value - reference for value, reference in zip(positive, negative)
    ]


def _is_differential(request: Mapping[str, Any]) -> bool:
    return any(
        "negative_real" in request[side] for side in ("input", "output")
    )


def _log10_span(low: float, high: float) -> float:
    """log10(high) - log10(low) for 0 < low <= high, stably.

    ``log1p((high - low) / low)`` keeps full precision when the two values
    are arbitrarily close (where the plain difference of logarithms
    catastrophically cancels); when that relative offset is not
    representable — very wide spans — the plain difference is
    well-conditioned and takes over.
    """

    offset = (high - low) / low
    if math.isfinite(offset):
        return math.log1p(offset) / math.log(10.0)
    return math.log10(high) - math.log10(low)


def _magnitude_db_at(
    frequencies: list[float], magnitudes_db: list[float], at: float
) -> float:
    """dB magnitude at one in-domain frequency, interpolated per the method.

    The frozen method declares linear-value-over-log10-frequency
    interpolation between adjacent simulated points; an exact axis hit
    returns the simulated value. Callers must have verified the domain.
    """

    for index, frequency in enumerate(frequencies):
        if frequency == at:
            return magnitudes_db[index]
        if frequency > at:
            f0, f1 = frequencies[index - 1], frequency
            # Numerator and denominator each use the same stable per-pair
            # log10 spacing: log1p of the relative offset is precise for
            # arbitrarily close values, and the plain log difference takes
            # over only when that relative offset is not representable
            # (very wide spans). One formula per pair — no global branch,
            # so an interior query can never collide with an endpoint that
            # the denominator still resolves.
            denominator = _log10_span(f0, f1)
            if denominator == 0.0:
                raise _InvalidTransferRequest(
                    "transfer.domain.invalid",
                    "The adjacent simulated frequencies bracketing "
                    "transfer.metric.at are not resolvable on the log10 "
                    "axis in double precision.",
                )
            fraction = _log10_span(f0, at) / denominator
            return magnitudes_db[index - 1] + fraction * (
                magnitudes_db[index] - magnitudes_db[index - 1]
            )
    raise _UnresolvedTransfer(  # pragma: no cover - domain is pre-checked
        "transfer.value.non_finite",
        "The requested frequency was not bracketed by the simulated axis.",
    )


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
        kind = request["metric"]["kind"]
        by_name = {signal["name"]: signal for signal in normalized["signals"]}
        requested_names = [
            *request["input"].values(),
            *request["output"].values(),
        ]
        missing = [name for name in requested_names if name not in by_name]
        if missing:
            raise _InvalidTransferRequest(
                "transfer.signal.missing",
                f"The normalized series does not contain: {', '.join(missing)}.",
            )
        input_unit = _operand_unit(request["input"], by_name, "transfer.input")
        output_unit = _operand_unit(request["output"], by_name, "transfer.output")
        expected_units = _METRIC_OPERAND_UNITS.get(kind)
        if expected_units is not None:
            expected_output, expected_input = expected_units
            if output_unit != expected_output or input_unit != expected_input:
                raise _InvalidTransferRequest(
                    "transfer.unit.mismatch",
                    f"{kind!r} is a {expected_output}-over-{expected_input} "
                    "driving-point ratio: every output component must carry "
                    f"{expected_output!r} and every input component "
                    f"{expected_input!r}, not {output_unit!r} over {input_unit!r}.",
                )
            ratio_unit = _METRIC_UNITS[kind]
        elif input_unit != output_unit:
            raise _InvalidTransferRequest(
                "transfer.unit.mismatch",
                f"{kind!r} is a dimensionless dB ratio, so every Cartesian "
                f"component series must use one identical unit; the output is "
                f"{output_unit!r} and the input is {input_unit!r}. A ratio of "
                "unlike units is an impedance or a transconductance, not a gain.",
            )
        else:
            ratio_unit = "1"

        input_values = _operand_phasors(request["input"], by_name)
        output_values = _operand_phasors(request["output"], by_name)
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
        signal_expression = (
            "complex-differential-output-over-input"
            if _is_differential(request)
            else "complex-output-over-input"
        )
        transfer_record = {
            "status": "analyzed",
            "request_sha256": request_sha256,
            "method": request["method"],
            "interpretation": request["interpretation"],
            "signals": {
                "input": {**request["input"], "unit": input_unit},
                "output": {**request["output"], "unit": output_unit},
                "ratio": "output-over-input",
            },
            "reference": {
                "kind": "first-simulated-frequency-not-dc",
                "frequency_hz": frequencies[0],
                "magnitude_db": low_frequency_gain_db,
                "magnitude": abs(ratios[0]),
                "unit": ratio_unit,
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
        elif kind == "low_frequency_impedance":
            # The linear magnitude, not the dB one: an impedance is reported in
            # ohms, and 20*log10 of a V/A ratio is a number with no name.
            value = abs(ratios[0])
            location_hz = frequencies[0]
        elif kind == "ac_magnitude_at_frequency":
            at_frequency = request["metric"]["at"]["value"]
            if not frequencies[0] <= at_frequency <= frequencies[-1]:
                raise _InvalidTransferRequest(
                    "transfer.domain.invalid",
                    "transfer.metric.at lies outside the simulated frequency domain "
                    f"[{frequencies[0]}, {frequencies[-1]}] Hz.",
                )
            value = _magnitude_db_at(frequencies, magnitudes_db, at_frequency)
            location_hz = at_frequency
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
        signal = (
            "complex-differential-output-over-input"
            if _is_differential(request)
            else "complex-output-over-input"
        )
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
