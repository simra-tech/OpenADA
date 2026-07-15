"""Deterministic, backend-independent measurements over normalized real series."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any
import uuid

from ..contract import diagnostic, result, static_execution


OPERATION_PROFILE = "openada.operation/result.measure/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/measurement.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.typed-evidence"
IMPLEMENTATION_VERSION = "1.0.0"
MAX_POINTS = 100_000
MAX_SIGNALS = 32
MAX_CONDITIONS = 64

_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEASUREMENT_KINDS = (
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
_KINDS = frozenset(MEASUREMENT_KINDS)


class _InvalidRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidRequest("measurement.request.invalid", f"{label} must be a JSON number.")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} must be representable as a finite JSON number.",
        ) from exc
    if not math.isfinite(parsed):
        raise _InvalidRequest("measurement.request.invalid", f"{label} must be finite.")
    return parsed


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _unit(value: object, label: str) -> str:
    return _text(value, label, limit=64)


def _closed_object(
    value: object,
    label: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidRequest("measurement.request.invalid", f"{label} must be an object.")
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} field names must all be strings.",
        )
    key_set = set(keys)
    missing = required - key_set
    unexpected = key_set - required - optional
    if missing:
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if unexpected:
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(unexpected))}.",
        )
    return value


def _extensions(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _InvalidRequest("measurement.request.invalid", f"{label} must be an object.")
    if value:
        raise _InvalidRequest(
            "measurement.request.invalid",
            f"{label} must be empty in v1alpha1.",
        )
    return {}


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _uuid_text(value: object, label: str) -> str:
    text = _text(value, label, limit=36)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise _InvalidRequest("measurement.source.invalid", f"{label} must be a UUID.") from exc
    if str(parsed) != text:
        raise _InvalidRequest(
            "measurement.source.invalid",
            f"{label} must use canonical lowercase UUID form.",
        )
    return text


def _correlation_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    return _uuid_text(value, "request_id")


def _condition(value: object, index: int) -> dict[str, object]:
    item = _closed_object(
        value,
        f"series.conditions[{index}]",
        required={"name", "value", "unit"},
    )
    condition_value = item["value"]
    if isinstance(condition_value, (dict, list)) or condition_value is None:
        raise _InvalidRequest(
            "measurement.source.invalid",
            f"series.conditions[{index}].value must be a finite number, string, or boolean.",
        )
    if isinstance(condition_value, (int, float)) and not isinstance(condition_value, bool):
        condition_value = _finite_number(
            condition_value,
            f"series.conditions[{index}].value",
        )
    elif isinstance(condition_value, str):
        condition_value = _text(
            condition_value,
            f"series.conditions[{index}].value",
        )
    elif not isinstance(condition_value, bool):
        raise _InvalidRequest(
            "measurement.source.invalid",
            f"series.conditions[{index}].value must be a finite number, string, or boolean.",
        )
    return {
        "name": _text(item["name"], f"series.conditions[{index}].name"),
        "value": condition_value,
        "unit": _unit(item["unit"], f"series.conditions[{index}].unit"),
    }


def _normalize_series(
    series: object,
    *,
    verify_digest: bool = True,
) -> dict[str, Any]:
    root = _closed_object(
        series,
        "series",
        required={"source", "axis", "signals", "conditions", "extensions"},
    )
    _extensions(root["extensions"], "series.extensions")

    source = _closed_object(
        root["source"],
        "series.source",
        required={"operation", "request_id", "artifact_role", "artifact_sha256"},
        optional={"lineage"},
    )
    artifact_sha256 = _text(
        source["artifact_sha256"], "series.source.artifact_sha256", limit=64
    )
    if not _SHA256_RE.fullmatch(artifact_sha256):
        raise _InvalidRequest(
            "measurement.source.invalid",
            "series.source.artifact_sha256 must be a lowercase SHA-256 digest.",
        )
    artifact_role = _text(source["artifact_role"], "series.source.artifact_role", limit=120)
    if not _ROLE_RE.fullmatch(artifact_role):
        raise _InvalidRequest(
            "measurement.source.invalid",
            "series.source.artifact_role is not a canonical role.",
        )
    if artifact_role != "measurement.source":
        raise _InvalidRequest(
            "measurement.source.invalid",
            "series.source.artifact_role must be 'measurement.source'; native artifacts belong only in unverified lineage.",
        )
    lineage: dict[str, object] | None = None
    if "lineage" in source:
        raw_lineage = _closed_object(
            source["lineage"],
            "series.source.lineage",
            required={
                "operation",
                "request_id",
                "artifact_role",
                "artifact_sha256",
                "binding",
            },
        )
        lineage_digest = _text(
            raw_lineage["artifact_sha256"],
            "series.source.lineage.artifact_sha256",
            limit=64,
        )
        if not _SHA256_RE.fullmatch(lineage_digest):
            raise _InvalidRequest(
                "measurement.source.invalid",
                "series.source.lineage.artifact_sha256 must be a lowercase SHA-256 digest.",
            )
        lineage_role = _text(
            raw_lineage["artifact_role"],
            "series.source.lineage.artifact_role",
            limit=120,
        )
        if not _ROLE_RE.fullmatch(lineage_role):
            raise _InvalidRequest(
                "measurement.source.invalid",
                "series.source.lineage.artifact_role is not a canonical role.",
            )
        if raw_lineage["binding"] != "unverified":
            raise _InvalidRequest(
                "measurement.source.invalid",
                "series.source.lineage.binding must be exactly 'unverified'.",
            )
        lineage = {
            "operation": _text(
                raw_lineage["operation"], "series.source.lineage.operation"
            ),
            "request_id": _uuid_text(
                raw_lineage["request_id"], "series.source.lineage.request_id"
            ),
            "artifact_role": lineage_role,
            "artifact_sha256": lineage_digest,
            "binding": "unverified",
        }
    normalized_source = {
        "operation": _text(source["operation"], "series.source.operation"),
        "request_id": _uuid_text(source["request_id"], "series.source.request_id"),
        "artifact_role": artifact_role,
        "artifact_sha256": artifact_sha256,
    }

    axis = _closed_object(
        root["axis"],
        "series.axis",
        required={"name", "unit", "values"},
    )
    if not _is_sequence(axis["values"]):
        raise _InvalidRequest("measurement.source.invalid", "series.axis.values must be an array.")
    if not 1 <= len(axis["values"]) <= MAX_POINTS:
        raise _InvalidRequest(
            "measurement.source.over_limit",
            f"series.axis.values must contain between 1 and {MAX_POINTS} points.",
        )
    axis_values = [
        _finite_number(value, f"series.axis.values[{index}]")
        for index, value in enumerate(axis["values"])
    ]
    if any(right <= left for left, right in zip(axis_values, axis_values[1:])):
        raise _InvalidRequest(
            "measurement.source.invalid",
            "series.axis.values must be strictly increasing without duplicates.",
        )
    normalized_axis = {
        "name": _text(axis["name"], "series.axis.name"),
        "unit": _unit(axis["unit"], "series.axis.unit"),
        "values": axis_values,
    }

    if not _is_sequence(root["signals"]) or not 1 <= len(root["signals"]) <= MAX_SIGNALS:
        raise _InvalidRequest(
            "measurement.source.over_limit",
            f"series.signals must contain between 1 and {MAX_SIGNALS} signals.",
        )
    signals: list[dict[str, Any]] = []
    signal_names: set[str] = set()
    for index, raw_signal in enumerate(root["signals"]):
        signal = _closed_object(
            raw_signal,
            f"series.signals[{index}]",
            required={"name", "unit", "values"},
        )
        name = _text(signal["name"], f"series.signals[{index}].name")
        if name in signal_names:
            raise _InvalidRequest(
                "measurement.source.invalid",
                f"series.signals contains duplicate name {name!r}.",
            )
        signal_names.add(name)
        if not _is_sequence(signal["values"]) or len(signal["values"]) != len(axis_values):
            raise _InvalidRequest(
                "measurement.source.invalid",
                f"series.signals[{index}].values must match the axis length exactly.",
            )
        signals.append(
            {
                "name": name,
                "unit": _unit(signal["unit"], f"series.signals[{index}].unit"),
                "values": [
                    _finite_number(value, f"series.signals[{index}].values[{point}]")
                    for point, value in enumerate(signal["values"])
                ],
            }
        )

    if not _is_sequence(root["conditions"]) or len(root["conditions"]) > MAX_CONDITIONS:
        raise _InvalidRequest(
            "measurement.source.over_limit",
            f"series.conditions must be an array of at most {MAX_CONDITIONS} entries.",
        )
    conditions = [_condition(item, index) for index, item in enumerate(root["conditions"])]
    condition_names = [item["name"] for item in conditions]
    if len(condition_names) != len(set(condition_names)):
        raise _InvalidRequest(
            "measurement.source.invalid",
            "series.conditions names must be unique.",
        )

    content = {
        "axis": normalized_axis,
        "signals": signals,
        "conditions": conditions,
    }
    content_sha256 = _canonical_sha256(content)
    if verify_digest and artifact_sha256 != content_sha256:
        raise _InvalidRequest(
            "measurement.source.digest_mismatch",
            "series.source.artifact_sha256 does not match the canonical normalized axis, signals, and conditions content.",
        )
    return {
        **content,
        "source": {
            **normalized_source,
            "series_sha256": content_sha256,
            "conditions_sha256": _canonical_sha256(conditions),
            "conditions": conditions,
            "lineage": lineage,
        },
    }


def normalized_series_sha256(
    *,
    axis: Mapping[str, object],
    signals: Sequence[Mapping[str, object]],
    conditions: Sequence[Mapping[str, object]],
) -> str:
    """Return the canonical digest expected by ``result.measure``.

    Validation and numeric normalization are identical to ``measure_result``;
    callers therefore do not need to reproduce private JSON serialization
    details when preparing a normalized-series artifact.
    """

    normalized = _normalize_series(
        {
            "source": {
                "operation": "normalized.series.digest",
                "request_id": "00000000-0000-4000-8000-000000000000",
                "artifact_role": "measurement.source",
                "artifact_sha256": "0" * 64,
            },
            "axis": axis,
            "signals": signals,
            "conditions": conditions,
            "extensions": {},
        },
        verify_digest=False,
    )
    return str(normalized["source"]["series_sha256"])


def _quantity(value: object, label: str, expected_unit: str) -> float:
    item = _closed_object(value, label, required={"value", "unit"})
    unit = _unit(item["unit"], f"{label}.unit")
    if unit != expected_unit:
        raise _InvalidRequest(
            "measurement.unit.mismatch",
            f"{label}.unit is {unit!r}; expected exact unit {expected_unit!r}.",
        )
    return _finite_number(item["value"], f"{label}.value")


def _window(value: object, axis_unit: str) -> tuple[float, float]:
    item = _closed_object(value, "measurement.parameters.window", required={"start", "stop"})
    start = _quantity(item["start"], "measurement.parameters.window.start", axis_unit)
    stop = _quantity(item["stop"], "measurement.parameters.window.stop", axis_unit)
    if stop <= start:
        raise _InvalidRequest(
            "measurement.request.invalid",
            "measurement.parameters.window.stop must be greater than start.",
        )
    return start, stop


def _indices_in_window(axis: list[float], window: tuple[float, float] | None) -> list[int]:
    if window is None:
        return list(range(len(axis)))
    start, stop = window
    return [index for index, value in enumerate(axis) if start <= value <= stop]


def _interpolate(x0: float, y0: float, x1: float, y1: float, x: float) -> float:
    return y0 + (y1 - y0) * ((x - x0) / (x1 - x0))


def _sample_at(axis: list[float], values: list[float], at: float, interpolation: str) -> float | None:
    for index, coordinate in enumerate(axis):
        if coordinate == at:
            return values[index]
        if coordinate > at:
            if interpolation == "linear" and index > 0:
                return _interpolate(axis[index - 1], values[index - 1], coordinate, values[index], at)
            return None
    return None


def _crossings(
    axis: list[float],
    values: list[float],
    threshold: float,
    direction: str,
    window: tuple[float, float] | None,
) -> list[float]:
    found: list[float] = []
    for index in range(len(axis) - 1):
        x0, x1 = axis[index], axis[index + 1]
        if window is not None and (x0 < window[0] or x1 > window[1]):
            continue
        y0, y1 = values[index], values[index + 1]
        rising = y0 < threshold <= y1 and y1 > y0
        falling = y0 > threshold >= y1 and y1 < y0
        if (direction in {"rising", "either"} and rising) or (
            direction in {"falling", "either"} and falling
        ):
            found.append(x0 + (threshold - y0) * (x1 - x0) / (y1 - y0))
    return found


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
        "unit": None,
        "signal": signal,
        "location": None,
        "algorithm": {
            "id": f"openada.algorithm/measurement.{kind.replace('_', '-')}/v1" if kind else None,
            "version": IMPLEMENTATION_VERSION,
        },
        "sample_count": 0,
        "source": source,
        "extensions": {},
    }


def _payload(
    correlation_id: str,
    measurement: dict[str, Any],
    *,
    status: str,
    summary: str,
    execution_status: str = "completed",
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "result.measure",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=status,
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
            "extensions": {},
        },
    )


def measure_result(
    series: Mapping[str, object],
    measurement: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Measure one scalar using a closed, versioned algorithm vocabulary.

    The function performs no native EDA access.  It accepts only a bounded real-valued
    series whose producer request and authoritative artifact digest are explicit.
    """

    try:
        correlation_id = _correlation_id(request_id)
    except _InvalidRequest as exc:
        correlation_id = str(uuid.uuid4())
        empty = _measurement_template(
            measurement_id=None,
            kind=None,
            signal=None,
            source=None,
        )
        return _payload(
            correlation_id,
            empty,
            status="unknown",
            summary="The result.measure correlation identity is invalid.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    normalized: dict[str, Any] | None = None
    measurement_id: str | None = None
    kind: str | None = None
    signal_name: str | None = None
    try:
        normalized = _normalize_series(series)
        request = _closed_object(
            measurement,
            "measurement",
            required={"measurement_id", "kind", "signal", "parameters", "extensions"},
        )
        _extensions(request["extensions"], "measurement.extensions")
        measurement_id = _text(request["measurement_id"], "measurement.measurement_id", limit=120)
        if not _ROLE_RE.fullmatch(measurement_id):
            raise _InvalidRequest(
                "measurement.request.invalid",
                "measurement.measurement_id is not a canonical identifier.",
            )
        kind = _text(request["kind"], "measurement.kind", limit=40)
        if kind not in _KINDS:
            raise _InvalidRequest(
                "measurement.kind.unsupported",
                f"Unsupported measurement kind {kind!r}.",
            )
        signal_name = _text(request["signal"], "measurement.signal")
        matching = [item for item in normalized["signals"] if item["name"] == signal_name]
        if len(matching) != 1:
            raise _InvalidRequest(
                "measurement.signal.missing",
                f"The normalized series has no signal named {signal_name!r}.",
            )
        selected = matching[0]
        axis = normalized["axis"]
        x_values: list[float] = axis["values"]
        y_values: list[float] = selected["values"]
        axis_unit: str = axis["unit"]
        signal_unit: str = selected["unit"]
        parameters = request["parameters"]

        output = _measurement_template(
            measurement_id=measurement_id,
            kind=kind,
            signal=signal_name,
            source=normalized["source"],
        )
        value: float | None = None
        unit: str
        location: float | None = None
        used_count = 0
        not_found_message = "The requested measurement was not observed in the declared series domain."

        if kind == "sample_at":
            params = _closed_object(
                parameters,
                "measurement.parameters",
                required={"at", "interpolation"},
            )
            at = _quantity(params["at"], "measurement.parameters.at", axis_unit)
            interpolation = _text(params["interpolation"], "measurement.parameters.interpolation", limit=20)
            if interpolation not in {"exact", "linear"}:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    "measurement.parameters.interpolation must be 'exact' or 'linear'.",
                )
            if not x_values[0] <= at <= x_values[-1]:
                raise _InvalidRequest(
                    "measurement.domain.invalid",
                    "measurement.parameters.at lies outside the declared axis domain.",
                )
            value = _sample_at(x_values, y_values, at, interpolation)
            unit = signal_unit
            location = at if value is not None else None
            used_count = 1 if value is not None else 0
            not_found_message = "No exact axis sample exists at the requested coordinate."
        elif kind in {"minimum", "maximum", "mean", "rms"}:
            params = _closed_object(
                parameters,
                "measurement.parameters",
                required=set(),
                optional={"window"},
            )
            window = _window(params["window"], axis_unit) if "window" in params else None
            indices = _indices_in_window(x_values, window)
            used_count = len(indices)
            unit = signal_unit
            if indices:
                samples = [y_values[index] for index in indices]
                if kind == "minimum":
                    value = min(samples)
                    location = x_values[indices[samples.index(value)]]
                elif kind == "maximum":
                    value = max(samples)
                    location = x_values[indices[samples.index(value)]]
                elif kind == "mean":
                    try:
                        value = math.fsum(samples) / len(samples)
                    except OverflowError as exc:
                        raise _InvalidRequest(
                            "measurement.value.non_finite",
                            "The arithmetic mean overflowed the finite result range.",
                        ) from exc
                else:
                    try:
                        value = math.sqrt(
                            math.fsum(item * item for item in samples) / len(samples)
                        )
                    except OverflowError as exc:
                        raise _InvalidRequest(
                            "measurement.value.non_finite",
                            "The RMS calculation overflowed the finite result range.",
                        ) from exc
            not_found_message = "The declared window contains no source samples."
        elif kind == "crossing":
            params = _closed_object(
                parameters,
                "measurement.parameters",
                required={"threshold", "direction", "occurrence"},
                optional={"window"},
            )
            threshold = _quantity(params["threshold"], "measurement.parameters.threshold", signal_unit)
            direction = _text(params["direction"], "measurement.parameters.direction", limit=20)
            if direction not in {"rising", "falling", "either"}:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    "measurement.parameters.direction must be rising, falling, or either.",
                )
            occurrence = params["occurrence"]
            if isinstance(occurrence, bool) or not isinstance(occurrence, int) or not 1 <= occurrence <= MAX_POINTS:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    f"measurement.parameters.occurrence must be an integer from 1 to {MAX_POINTS}.",
                )
            window = _window(params["window"], axis_unit) if "window" in params else None
            crossings = _crossings(x_values, y_values, threshold, direction, window)
            used_count = len(_indices_in_window(x_values, window))
            if len(crossings) >= occurrence:
                value = crossings[occurrence - 1]
                location = value
            unit = axis_unit
        elif kind in {"rise_time", "fall_time"}:
            params = _closed_object(
                parameters,
                "measurement.parameters",
                required={"lower_threshold", "upper_threshold", "occurrence"},
                optional={"window"},
            )
            lower = _quantity(
                params["lower_threshold"], "measurement.parameters.lower_threshold", signal_unit
            )
            upper = _quantity(
                params["upper_threshold"], "measurement.parameters.upper_threshold", signal_unit
            )
            if upper <= lower:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    "upper_threshold must be greater than lower_threshold.",
                )
            occurrence = params["occurrence"]
            if isinstance(occurrence, bool) or not isinstance(occurrence, int) or not 1 <= occurrence <= MAX_POINTS:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    f"measurement.parameters.occurrence must be an integer from 1 to {MAX_POINTS}.",
                )
            window = _window(params["window"], axis_unit) if "window" in params else None
            used_count = len(_indices_in_window(x_values, window))
            direction = "rising" if kind == "rise_time" else "falling"
            start_threshold = lower if kind == "rise_time" else upper
            end_threshold = upper if kind == "rise_time" else lower
            starts = _crossings(x_values, y_values, start_threshold, direction, window)
            ends = _crossings(x_values, y_values, end_threshold, direction, window)
            if len(starts) >= occurrence:
                start = starts[occurrence - 1]
                next_start = starts[occurrence] if len(starts) > occurrence else math.inf
                matching_end = next((item for item in ends if start <= item < next_start), None)
                if matching_end is not None:
                    value = matching_end - start
                    location = matching_end
            unit = axis_unit
        else:
            params = _closed_object(
                parameters,
                "measurement.parameters",
                required={"target", "tolerance", "reference", "hold_for"},
                optional={"window"},
            )
            target = _quantity(params["target"], "measurement.parameters.target", signal_unit)
            tolerance = _quantity(
                params["tolerance"], "measurement.parameters.tolerance", signal_unit
            )
            reference = _quantity(
                params["reference"], "measurement.parameters.reference", axis_unit
            )
            hold_for = _quantity(params["hold_for"], "measurement.parameters.hold_for", axis_unit)
            if tolerance <= 0 or hold_for <= 0:
                raise _InvalidRequest(
                    "measurement.request.invalid",
                    "settling_time tolerance and hold_for must be greater than zero.",
                )
            window = _window(params["window"], axis_unit) if "window" in params else None
            if not x_values[0] <= reference <= x_values[-1]:
                raise _InvalidRequest(
                    "measurement.domain.invalid",
                    "settling_time reference lies outside the declared axis domain.",
                )
            indices = [
                index
                for index in _indices_in_window(x_values, window)
                if x_values[index] >= reference
            ]
            used_count = len(indices)
            settled_index: int | None = None
            suffix_inside = True
            if indices:
                final_index = indices[-1]
                for index in reversed(indices):
                    suffix_inside = suffix_inside and (
                        abs(y_values[index] - target) <= tolerance
                    )
                    if (
                        suffix_inside
                        and x_values[final_index] - x_values[index] >= hold_for
                    ):
                        settled_index = index
            if settled_index is not None:
                value = x_values[settled_index] - reference
                location = x_values[settled_index]
            unit = axis_unit

        output["request_sha256"] = _canonical_sha256(request)
        output["sample_count"] = used_count
        output["unit"] = unit
        if value is None:
            output["status"] = "not_found"
            return _payload(
                correlation_id,
                output,
                status="fail",
                summary=not_found_message,
                diagnostics=[
                    diagnostic(
                        "error",
                        "measurement.value.not_found",
                        not_found_message,
                        hint="Review the declared domain, units, event direction, thresholds, and observation duration.",
                    )
                ],
            )

        if not math.isfinite(value):
            raise _InvalidRequest(
                "measurement.value.non_finite",
                "The selected algorithm did not produce a finite scalar value.",
            )
        output["status"] = "measured"
        output["value"] = value
        if location is not None:
            output["location"] = {"value": location, "unit": axis_unit}
        return _payload(
            correlation_id,
            output,
            status="pass",
            summary=f"Derived {measurement_id!r} using the closed {kind} algorithm.",
        )
    except _InvalidRequest as exc:
        empty = _measurement_template(
            measurement_id=measurement_id,
            kind=kind,
            signal=signal_name,
            source=normalized["source"] if normalized is not None else None,
        )
        return _payload(
            correlation_id,
            empty,
            status="unknown",
            summary="The result.measure request could not be evaluated safely.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )


__all__ = [
    "ASSERTION_PROFILE",
    "IMPLEMENTATION_ID",
    "IMPLEMENTATION_VERSION",
    "MAX_POINTS",
    "MEASUREMENT_KINDS",
    "OPERATION_PROFILE",
    "measure_result",
    "normalized_series_sha256",
]
