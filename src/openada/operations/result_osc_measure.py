"""Typed oscillator measurements over provenance-bound transient series.

The operation deliberately owns a separate profile family from ``result.measure``.
Oscillation validity is a coupled conclusion over several signals and one shared
window, while tuning gain and perturbation shift compose complete oscillator
receipts rather than anonymous scalars.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
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


OPERATION_PROFILE = "openada.operation/result.osc.measure/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/oscillator.measurement.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.oscillator-evidence"
IMPLEMENTATION_VERSION = "1.0.0"

TRANSIENT_METHOD_ID = "openada.method/oscillator-transient-hysteretic/v1alpha1"
TUNING_METHOD_ID = "openada.method/oscillator-local-tuning-gain/v1alpha1"
SHIFT_METHOD_ID = "openada.method/oscillator-frequency-shift/v1alpha1"
RECEIPT_SCHEMA = "openada.oscillator-transient-receipt/v1alpha1"

OSCILLATOR_MEASUREMENT_KINDS = (
    "transient",
    "tuning_grid",
    "frequency_shift",
)
OSCILLATION_STATUSES = (
    "sustained",
    "never_started",
    "collapsed",
    "not_sustained",
    "multimode",
    "unknown",
)

MAX_GRID_POINTS = 256
MAX_RECEIPT_CROSSINGS = 100_001
MAX_CONDITIONS = 64
MAX_PERIOD_RELATIVE_DEVIATION = 0.05
MAX_AMPLITUDE_RELATIVE_DEVIATION = 0.20
_KINDS = frozenset(OSCILLATOR_MEASUREMENT_KINDS)
_VERDICTS = frozenset(OSCILLATION_STATUSES)
_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _InvalidOscillatorRequest(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        execution_status: str = "invalid_request",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.execution_status = execution_status


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _closed_object(
    value: object,
    label: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} must be an object."
        )
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} field names must all be strings.",
        )
    actual = set(keys)
    missing = required - actual
    unexpected = actual - required - optional
    if missing:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if unexpected:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(unexpected))}.",
        )
    return value


def _extensions(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required=set())
    if item:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} must be empty in v1alpha1."
        )
    return {}


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _role(value: object, label: str, *, limit: int = 120) -> str:
    parsed = _text(value, label, limit=limit)
    if not _ROLE_RE.fullmatch(parsed):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} is not a canonical identifier."
        )
    return parsed


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} must be a JSON number."
        )
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} must be representable as a finite JSON number.",
        ) from exc
    if not math.isfinite(parsed):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} must be finite."
        )
    return parsed


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", f"{label} must be an integer."
        )
    if not minimum <= value <= maximum:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"{label} must be between {minimum} and {maximum}.",
        )
    return value


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
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            f"The oscillator record is not canonical finite JSON: {exc}",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def oscillator_receipt_sha256(receipt: Mapping[str, object]) -> str:
    """Hash one oscillator receipt without its self-identifying ``sha256`` field."""

    if not isinstance(receipt, Mapping):
        raise ValueError("oscillator receipt must be an object")
    return _canonical_sha256({key: value for key, value in receipt.items() if key != "sha256"})


def _digest(value: object, label: str) -> str:
    parsed = _text(value, label, limit=64)
    if not _SHA256_RE.fullmatch(parsed):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label} must be a lowercase SHA-256 digest."
        )
    return parsed


def _request_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid", "request_id must be a canonical UUID."
        ) from exc
    if str(parsed) != value:
        raise _InvalidOscillatorRequest(
            "oscillator.request.invalid",
            "request_id must use canonical lowercase UUID form.",
        )
    return value


def _quantity(value: object, label: str, expected_unit: str | None = None) -> dict[str, object]:
    item = _closed_object(value, label, required={"value", "unit"})
    unit = _text(item["unit"], f"{label}.unit", limit=64)
    if expected_unit is not None and unit != expected_unit:
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch",
            f"{label}.unit is {unit!r}; expected exact unit {expected_unit!r}.",
        )
    return {"value": _finite(item["value"], f"{label}.value"), "unit": unit}


def _metric(
    *,
    status: str,
    value: float | None,
    unit: str,
    window_sha256: str | None,
) -> dict[str, object]:
    return {
        "status": status,
        "value": value,
        "unit": unit,
        "window_sha256": window_sha256,
        "extensions": {},
    }


def _algorithm_id(kind: str | None) -> str | None:
    return {
        "transient": TRANSIENT_METHOD_ID,
        "tuning_grid": TUNING_METHOD_ID,
        "frequency_shift": SHIFT_METHOD_ID,
    }.get(kind)


def _measurement_template(
    *,
    measurement_id: str | None,
    kind: str | None,
    status: str = "unknown",
    request_sha256: str | None = None,
    source_count: int = 0,
) -> dict[str, object]:
    return {
        "measurement_id": measurement_id,
        "kind": kind,
        "status": status,
        "request_sha256": request_sha256,
        "algorithm": {
            "id": _algorithm_id(kind),
            "version": IMPLEMENTATION_VERSION,
        },
        "source_count": source_count,
        "extensions": {},
    }


def _payload(
    correlation_id: str,
    measurement: dict[str, object],
    *,
    engineering_status: str,
    summary: str,
    transient: dict[str, object] | None = None,
    grid: dict[str, object] | None = None,
    shift: dict[str, object] | None = None,
    receipt: dict[str, object] | None = None,
    execution_status: str = "completed",
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "result.osc.measure",
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
            "transient": transient,
            "grid": grid,
            "shift": shift,
            "receipt": receipt,
            "extensions": {},
        },
    )


def _normalize_base_request(value: object) -> tuple[str, str, Mapping[str, Any]]:
    request = _closed_object(
        value,
        "measurement",
        required={"measurement_id", "kind", "extensions"},
        optional={
            "signals",
            "window",
            "startup",
            "crossing",
            "quality",
            "power",
            "control_condition",
            "control_unit",
            "expected_monotonicity",
            "points",
            "perturbation_condition",
            "reference",
            "perturbed",
        },
    )
    _extensions(request["extensions"], "measurement.extensions")
    measurement_id = _role(request["measurement_id"], "measurement.measurement_id")
    kind = _text(request["kind"], "measurement.kind", limit=40)
    if kind not in _KINDS:
        raise _InvalidOscillatorRequest(
            "oscillator.kind.unsupported", f"Unsupported oscillator kind {kind!r}."
        )
    return measurement_id, kind, request


def _normalize_transient_request(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "measurement_id",
        "kind",
        "signals",
        "window",
        "startup",
        "crossing",
        "quality",
        "power",
        "extensions",
    }
    request = _closed_object(value, "measurement", required=expected)
    _extensions(request["extensions"], "measurement.extensions")

    signals = _closed_object(
        request["signals"],
        "measurement.signals",
        required={"positive", "negative", "supply_voltage", "supply_current"},
    )
    normalized_signals = {
        name: _text(signals[name], f"measurement.signals.{name}")
        for name in ("positive", "negative", "supply_voltage", "supply_current")
    }
    if len(set(normalized_signals.values())) != 4:
        raise _InvalidOscillatorRequest(
            "oscillator.signal.invalid",
            "measurement.signals must name four distinct source signals.",
        )

    window = _closed_object(
        request["window"],
        "measurement.window",
        required={"start", "stop", "cycle_count"},
    )
    start = _quantity(window["start"], "measurement.window.start", "s")
    stop = _quantity(window["stop"], "measurement.window.stop", "s")
    if stop["value"] <= start["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.window.invalid",
            "measurement.window.stop must be greater than start.",
        )
    cycle_count = _integer(
        window["cycle_count"],
        "measurement.window.cycle_count",
        minimum=2,
        maximum=100_000,
    )

    startup = _closed_object(
        request["startup"],
        "measurement.startup",
        required={"search_start", "hold_for", "minimum_peak_to_peak"},
    )
    search_start = _quantity(
        startup["search_start"], "measurement.startup.search_start", "s"
    )
    hold_for = _quantity(startup["hold_for"], "measurement.startup.hold_for", "s")
    minimum_peak_to_peak = _quantity(
        startup["minimum_peak_to_peak"],
        "measurement.startup.minimum_peak_to_peak",
        "V",
    )
    if hold_for["value"] <= 0 or minimum_peak_to_peak["value"] <= 0:
        raise _InvalidOscillatorRequest(
            "oscillator.startup.invalid",
            "startup hold_for and minimum_peak_to_peak must be greater than zero.",
        )
    if search_start["value"] > start["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.startup.invalid",
            "startup search_start must not be later than the late-window start.",
        )
    if hold_for["value"] > stop["value"] - search_start["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.startup.invalid",
            "startup hold_for exceeds the available declared observation interval.",
        )

    crossing = _closed_object(
        request["crossing"],
        "measurement.crossing",
        required={"threshold", "hysteresis", "direction"},
    )
    threshold = _quantity(
        crossing["threshold"], "measurement.crossing.threshold", "V"
    )
    hysteresis = _quantity(
        crossing["hysteresis"], "measurement.crossing.hysteresis", "V"
    )
    if threshold["value"] != 0.0:
        raise _InvalidOscillatorRequest(
            "oscillator.crossing.invalid",
            "v1alpha1 defines differential zero crossings; threshold must be exactly 0 V.",
        )
    if hysteresis["value"] <= 0:
        raise _InvalidOscillatorRequest(
            "oscillator.crossing.invalid", "crossing hysteresis must be greater than zero."
        )
    direction = _text(crossing["direction"], "measurement.crossing.direction", limit=20)
    if direction != "rising":
        raise _InvalidOscillatorRequest(
            "oscillator.crossing.invalid",
            "v1alpha1 uses rising hysteretic zero crossings only.",
        )

    quality = _closed_object(
        request["quality"],
        "measurement.quality",
        required={
            "maximum_period_relative_deviation",
            "maximum_amplitude_relative_deviation",
            "minimum_samples_per_cycle",
        },
    )
    maximum_period_deviation = _finite(
        quality["maximum_period_relative_deviation"],
        "measurement.quality.maximum_period_relative_deviation",
    )
    maximum_amplitude_deviation = _finite(
        quality["maximum_amplitude_relative_deviation"],
        "measurement.quality.maximum_amplitude_relative_deviation",
    )
    if not 0 <= maximum_period_deviation <= MAX_PERIOD_RELATIVE_DEVIATION:
        raise _InvalidOscillatorRequest(
            "oscillator.quality.invalid",
            "maximum_period_relative_deviation must be between zero and 0.05.",
        )
    if not 0 <= maximum_amplitude_deviation <= MAX_AMPLITUDE_RELATIVE_DEVIATION:
        raise _InvalidOscillatorRequest(
            "oscillator.quality.invalid",
            "maximum_amplitude_relative_deviation must be between zero and 0.20.",
        )
    minimum_samples = _integer(
        quality["minimum_samples_per_cycle"],
        "measurement.quality.minimum_samples_per_cycle",
        minimum=3,
        maximum=100_000,
    )

    power = _closed_object(
        request["power"],
        "measurement.power",
        required={"current_orientation"},
    )
    orientation = _text(
        power["current_orientation"],
        "measurement.power.current_orientation",
        limit=40,
    )
    if orientation not in {"positive_into_load", "positive_into_source"}:
        raise _InvalidOscillatorRequest(
            "oscillator.power.invalid",
            "power current_orientation must be positive_into_load or positive_into_source.",
        )

    return {
        "measurement_id": _role(request["measurement_id"], "measurement.measurement_id"),
        "kind": "transient",
        "signals": normalized_signals,
        "window": {"start": start, "stop": stop, "cycle_count": cycle_count},
        "startup": {
            "search_start": search_start,
            "hold_for": hold_for,
            "minimum_peak_to_peak": minimum_peak_to_peak,
        },
        "crossing": {
            "threshold": threshold,
            "hysteresis": hysteresis,
            "direction": direction,
        },
        "quality": {
            "maximum_period_relative_deviation": maximum_period_deviation,
            "maximum_amplitude_relative_deviation": maximum_amplitude_deviation,
            "minimum_samples_per_cycle": minimum_samples,
        },
        "power": {"current_orientation": orientation},
        "extensions": {},
    }


def _signal(normalized: Mapping[str, Any], name: str, unit: str) -> list[float]:
    matching = [item for item in normalized["signals"] if item["name"] == name]
    if len(matching) != 1:
        raise _InvalidOscillatorRequest(
            "oscillator.signal.missing",
            f"The normalized series has no signal named {name!r}.",
        )
    if matching[0]["unit"] != unit:
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch",
            f"Signal {name!r} has unit {matching[0]['unit']!r}; expected {unit!r}.",
        )
    return list(matching[0]["values"])


def _interpolated_value(axis: list[float], values: list[float], at: float) -> float:
    index = bisect_left(axis, at)
    if index < len(axis) and axis[index] == at:
        return values[index]
    if index == 0 or index == len(axis):  # guarded by caller domain validation
        raise _InvalidOscillatorRequest(
            "oscillator.window.invalid", "A crop boundary lies outside the source domain."
        )
    x0, x1 = axis[index - 1], axis[index]
    y0, y1 = values[index - 1], values[index]
    value = y0 + (y1 - y0) * ((at - x0) / (x1 - x0))
    if not math.isfinite(value):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "Linear boundary interpolation did not produce a finite value.",
        )
    return value


def _crop(
    axis: list[float],
    vectors: Sequence[list[float]],
    start: float,
    stop: float,
) -> tuple[list[float], list[list[float]]]:
    if start < axis[0] or stop > axis[-1] or stop <= start:
        raise _InvalidOscillatorRequest(
            "oscillator.window.invalid",
            "The closed oscillator window must lie inside the source time domain.",
        )
    first = bisect_right(axis, start)
    last = bisect_left(axis, stop)
    coordinates = [start, *axis[first:last], stop]
    cropped: list[list[float]] = []
    for values in vectors:
        cropped.append([_interpolated_value(axis, values, value) for value in coordinates])
    return coordinates, cropped


def _differential(positive: Sequence[float], negative: Sequence[float]) -> list[float]:
    values: list[float] = []
    for left, right in zip(positive, negative):
        difference = left - right
        if not math.isfinite(difference):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite",
                "The differential waveform overflowed the finite result range.",
            )
        values.append(difference)
    return values


def _hysteretic_crossings(
    axis: list[float],
    values: list[float],
    *,
    threshold: float,
    hysteresis: float,
) -> list[float]:
    low = threshold - hysteresis
    high = threshold + hysteresis
    armed = values[0] <= low
    pending: float | None = None
    found: list[float] = []
    for index in range(len(axis) - 1):
        x0, x1 = axis[index], axis[index + 1]
        y0, y1 = values[index], values[index + 1]
        # A candidate is not hysteretic until it reaches the high threshold.
        # Returning to the low threshold invalidates that candidate; retaining
        # its old zero time would splice two unrelated excursions together.
        if pending is not None and (y0 <= low or y1 <= low):
            pending = None
            armed = True
        if not armed and pending is None and (y0 <= low or y1 <= low):
            armed = True
        if armed and pending is None and y0 < threshold <= y1 and y1 > y0:
            ordinate_scale = max(abs(y0), abs(y1), abs(threshold))
            if ordinate_scale == 0 or not math.isfinite(ordinate_scale):
                raise _InvalidOscillatorRequest(
                    "oscillator.value.non_finite",
                    "A hysteretic crossing could not be interpolated in finite range.",
                )
            fraction = (
                threshold / ordinate_scale - y0 / ordinate_scale
            ) / (y1 / ordinate_scale - y0 / ordinate_scale)
            if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
                raise _InvalidOscillatorRequest(
                    "oscillator.value.non_finite",
                    "A hysteretic crossing fraction is outside the finite source segment.",
                )
            span = x1 - x0
            if math.isfinite(span):
                pending = (
                    x0 + fraction * span
                    if fraction <= 0.5
                    else x1 - (1.0 - fraction) * span
                )
            else:
                # The convex form avoids an overflowing x1-x0 subtraction for
                # finite endpoints with opposite, near-limit magnitudes.
                pending = (1.0 - fraction) * x0 + fraction * x1
            if not math.isfinite(pending):
                raise _InvalidOscillatorRequest(
                    "oscillator.value.non_finite",
                    "A hysteretic crossing time is outside the finite source segment.",
                )
            pending = min(x1, max(x0, pending))
        if pending is not None and (y0 >= high or y1 >= high):
            found.append(pending)
            if len(found) > MAX_RECEIPT_CROSSINGS:
                raise _InvalidOscillatorRequest(
                    "oscillator.source.over_limit",
                    f"The source contains more than {MAX_RECEIPT_CROSSINGS} validated crossings.",
                )
            pending = None
            armed = False
    return found


def _relative_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    scale = max(values)
    if scale <= 0 or not math.isfinite(scale):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "A positive finite quality reference could not be established.",
        )
    try:
        scaled_mean = math.fsum(value / scale for value in values) / len(values)
        deviation = max(
            abs(value / scale - scaled_mean) / scaled_mean for value in values
        )
    except (OverflowError, ZeroDivisionError) as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "A quality deviation overflowed the finite result range.",
        ) from exc
    if not math.isfinite(deviation):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite", "A quality deviation is not finite."
        )
    return deviation


def _cycle_assessment(
    axis: list[float],
    differential: list[float],
    crossings: Sequence[float],
) -> tuple[list[float], list[float], list[int]]:
    periods: list[float] = []
    amplitudes: list[float] = []
    sample_counts: list[int] = []
    for left, right in zip(crossings, crossings[1:]):
        period = right - left
        if period <= 0 or not math.isfinite(period):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite", "A validated period is not positive and finite."
            )
        cycle_axis, (cycle_values,) = _crop(axis, [differential], left, right)
        amplitude = max(cycle_values) - min(cycle_values)
        if not math.isfinite(amplitude):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite", "A cycle amplitude is not finite."
            )
        periods.append(period)
        amplitudes.append(amplitude)
        sample_counts.append(len(cycle_axis))
    return periods, amplitudes, sample_counts


_CycleStats = tuple[float, float, float, float, float, float, int, int]


def _merge_cycle_stats(
    left: _CycleStats | None,
    right: _CycleStats | None,
) -> _CycleStats | None:
    if left is None:
        return right
    if right is None:
        return left
    amplitude_scale = max(left[4], right[4])
    scaled_sum = (
        left[5] * (left[4] / amplitude_scale)
        + right[5] * (right[4] / amplitude_scale)
        if amplitude_scale > 0
        else 0.0
    )
    if not math.isfinite(scaled_sum):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "A cycle-statistics reduction overflowed the finite result range.",
        )
    return (
        min(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        max(left[3], right[3]),
        amplitude_scale,
        scaled_sum,
        min(left[6], right[6]),
        left[7] + right[7],
    )


def _cycle_stats_tree(
    periods: Sequence[float],
    amplitudes: Sequence[float],
    sample_counts: Sequence[int],
) -> tuple[int, list[_CycleStats | None]]:
    size = 1
    while size < len(periods):
        size *= 2
    tree: list[_CycleStats | None] = [None] * (2 * size)
    for index, (period, amplitude, samples) in enumerate(
        zip(periods, amplitudes, sample_counts)
    ):
        tree[size + index] = (
            period,
            period,
            amplitude,
            amplitude,
            amplitude,
            1.0 if amplitude > 0 else 0.0,
            samples,
            1,
        )
    for index in range(size - 1, 0, -1):
        tree[index] = _merge_cycle_stats(tree[2 * index], tree[2 * index + 1])
    return size, tree


def _cycle_stats_range(
    tree: Sequence[_CycleStats | None],
    size: int,
    start: int,
    stop: int,
) -> _CycleStats:
    left: _CycleStats | None = None
    right: _CycleStats | None = None
    start += size
    stop += size
    while start < stop:
        if start % 2:
            left = _merge_cycle_stats(left, tree[start])
            start += 1
        if stop % 2:
            stop -= 1
            right = _merge_cycle_stats(tree[stop], right)
        start //= 2
        stop //= 2
    combined = _merge_cycle_stats(left, right)
    if combined is None:
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite", "A startup cycle range is empty."
        )
    return combined


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
    activity = False
    if len(crossings) < 2:
        return None, activity
    periods, amplitudes, sample_counts = _cycle_assessment(axis, differential, crossings)
    activity = any(amplitude >= minimum_amplitude for amplitude in amplitudes)
    if not activity:
        return None, False
    tree_size, stats_tree = _cycle_stats_tree(periods, amplitudes, sample_counts)
    # Each candidate onset uses the shortest complete-cycle hold interval.
    # Binary search plus a stable segment-tree reduction bounds the scan to
    # O(cycles log cycles), including adversarial long holds and late failures.
    for start_index in range(len(periods) - 2):
        required_stop = crossings[start_index] + hold_for
        stop_index = max(start_index + 3, bisect_left(crossings, required_stop))
        if stop_index >= len(crossings):
            break
        stats = _cycle_stats_range(
            stats_tree, tree_size, start_index, stop_index
        )
        if (
            stats[2] < minimum_amplitude
            or stats[6] < minimum_samples
        ):
            continue
        period_mean = (
            crossings[stop_index] - crossings[start_index]
        ) / stats[7]
        amplitude_mean_scaled = stats[5] / stats[7]
        if (
            period_mean <= 0
            or not math.isfinite(period_mean)
            or amplitude_mean_scaled <= 0
            or not math.isfinite(amplitude_mean_scaled)
        ):
            continue
        period_deviation = max(
            abs(stats[0] / period_mean - 1.0),
            abs(stats[1] / period_mean - 1.0),
        )
        amplitude_deviation = max(
            abs(stats[2] / stats[4] - amplitude_mean_scaled),
            abs(stats[3] / stats[4] - amplitude_mean_scaled),
        ) / amplitude_mean_scaled
        if (
            math.isfinite(period_deviation)
            and math.isfinite(amplitude_deviation)
            and period_deviation <= maximum_period_deviation
            and amplitude_deviation <= maximum_amplitude_deviation
        ):
            return crossings[start_index], activity
    return None, activity


def _average_power(
    axis: list[float],
    voltage: list[float],
    current: list[float],
    orientation: str,
) -> float:
    sign = 1.0 if orientation == "positive_into_load" else -1.0
    powers: list[float] = []
    for volts, amps in zip(voltage, current):
        product = sign * volts * amps
        if not math.isfinite(product):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite",
                "The pointwise supply-power product overflowed the finite result range.",
            )
        powers.append(product)
    duration = axis[-1] - axis[0]
    if duration <= 0 or not math.isfinite(duration):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "The supply-power crop duration is not positive and finite.",
        )
    try:
        mean = math.fsum(
            ((right_t - left_t) / duration) * (left_p / 2.0 + right_p / 2.0)
            for left_t, right_t, left_p, right_p in zip(
                axis, axis[1:], powers, powers[1:]
            )
        )
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "The trapezoidal average supply power overflowed the finite result range.",
        ) from exc
    if not math.isfinite(mean):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite", "Average supply power is not finite."
        )
    return mean


def _measure_transient(
    normalized: Mapping[str, Any],
    request: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], str, str, list[dict[str, str]]]:
    if normalized["axis"]["unit"] != "s":
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch",
            f"The oscillator axis unit must be exactly 's', not {normalized['axis']['unit']!r}.",
        )
    axis = list(normalized["axis"]["values"])
    signals = request["signals"]
    positive = _signal(normalized, signals["positive"], "V")
    negative = _signal(normalized, signals["negative"], "V")
    supply_voltage = _signal(normalized, signals["supply_voltage"], "V")
    supply_current = _signal(normalized, signals["supply_current"], "A")

    start = request["window"]["start"]["value"]
    stop = request["window"]["stop"]["value"]
    search_start = request["startup"]["search_start"]["value"]
    if search_start < axis[0]:
        raise _InvalidOscillatorRequest(
            "oscillator.window.invalid",
            "startup search_start lies before the source time domain.",
        )

    crop_axis, cropped = _crop(
        axis,
        [positive, negative, supply_voltage, supply_current],
        start,
        stop,
    )
    crop_positive, crop_negative, crop_voltage, crop_current = cropped
    crop_differential = _differential(crop_positive, crop_negative)
    differential_peak_to_peak = max(crop_differential) - min(crop_differential)
    if not math.isfinite(differential_peak_to_peak):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "Differential peak-to-peak amplitude overflowed the finite result range.",
        )
    average_supply_power = _average_power(
        crop_axis,
        crop_voltage,
        crop_current,
        request["power"]["current_orientation"],
    )

    search_axis, (search_positive, search_negative) = _crop(
        axis, [positive, negative], search_start, stop
    )
    search_differential = _differential(search_positive, search_negative)
    all_crossings = _hysteretic_crossings(
        search_axis,
        search_differential,
        threshold=request["crossing"]["threshold"]["value"],
        hysteresis=request["crossing"]["hysteresis"]["value"],
    )
    late_crossings = [value for value in all_crossings if start <= value <= stop]

    source = normalized["source"]
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

    requested_cycles = request["window"]["cycle_count"]
    minimum_amplitude = request["startup"]["minimum_peak_to_peak"]["value"]
    maximum_period_deviation = request["quality"]["maximum_period_relative_deviation"]
    maximum_amplitude_deviation = request["quality"]["maximum_amplitude_relative_deviation"]
    minimum_samples = request["quality"]["minimum_samples_per_cycle"]

    started_at, activity = _startup_candidate(
        search_axis,
        search_differential,
        all_crossings,
        hold_for=request["startup"]["hold_for"]["value"],
        minimum_amplitude=minimum_amplitude,
        maximum_period_deviation=maximum_period_deviation,
        maximum_amplitude_deviation=maximum_amplitude_deviation,
        minimum_samples=minimum_samples,
    )

    selected_crossings: list[float] = []
    selected_periods: list[float] = []
    all_late_periods: list[float] = []
    all_late_amplitudes: list[float] = []
    all_late_samples: list[int] = []
    period_relative_deviation: float | None = None
    amplitude_relative_deviation: float | None = None
    observed_minimum_samples: int | None = None
    collapse_at_value: float | None = None
    flags: list[str] = []
    verdict: str

    enough_counted_cycles = len(late_crossings) >= requested_cycles + 1
    if enough_counted_cycles:
        selected_crossings = late_crossings[: requested_cycles + 1]
        selected_periods = [
            right - left for left, right in zip(selected_crossings, selected_crossings[1:])
        ]

    leading_covered = False
    trailing_covered = False
    tail_start = start
    tail_amplitude = differential_peak_to_peak
    failed_amplitude_times: list[float] = []
    if len(late_crossings) >= 2:
        all_late_periods, all_late_amplitudes, all_late_samples = _cycle_assessment(
            crop_axis, crop_differential, late_crossings
        )
        period_relative_deviation = _relative_deviation(all_late_periods)
        amplitude_relative_deviation = _relative_deviation(all_late_amplitudes)
        observed_minimum_samples = min(all_late_samples)
        mean_period = (late_crossings[-1] - late_crossings[0]) / len(
            all_late_periods
        )
        if mean_period <= 0 or not math.isfinite(mean_period):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite",
                "The late-cycle mean period is not positive and finite.",
            )
        leading_covered = late_crossings[0] - start <= mean_period
        # The zero time is retained only after the waveform reaches +H.  A
        # crop may therefore end after a genuine final zero but before its
        # confirmation sample.  Ten percent is the closed v1alpha1 phase/
        # interpolation allowance; the terminal-period amplitude gate below
        # still rejects a real tail collapse.
        trailing_covered = stop - late_crossings[-1] <= 1.1 * mean_period
        tail_start = max(start, stop - mean_period)
        _, (tail_values,) = _crop(
            crop_axis, [crop_differential], tail_start, stop
        )
        tail_amplitude = max(tail_values) - min(tail_values)
        if not math.isfinite(tail_amplitude):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite",
                "The terminal differential amplitude overflowed the finite result range.",
            )
        if period_relative_deviation > maximum_period_deviation:
            flags.append("period_inconsistent")
        if amplitude_relative_deviation > maximum_amplitude_deviation:
            flags.append("amplitude_inconsistent")
        if observed_minimum_samples < minimum_samples:
            flags.append("sampling_resolution_insufficient")
        failed_amplitude_times = [
            late_crossings[index]
            for index, value in enumerate(all_late_amplitudes)
            if value < minimum_amplitude
        ]
        if failed_amplitude_times or tail_amplitude < minimum_amplitude:
            flags.append("amplitude_below_minimum")
        if not (leading_covered and trailing_covered):
            flags.append("window_not_fully_covered")

    elif len(all_crossings) >= 2:
        # With fewer than two late events, estimate one terminal cycle from
        # the median observed hysteretic period.  This distinguishes a clean
        # but under-counted crop from a post-start terminal amplitude loss.
        search_periods = [
            right - left for left, right in zip(all_crossings, all_crossings[1:])
        ]
        ordered_periods = sorted(search_periods)
        mean_period = ordered_periods[len(ordered_periods) // 2]
        tail_start = max(start, stop - mean_period)
        _, (tail_values,) = _crop(
            crop_axis, [crop_differential], tail_start, stop
        )
        tail_amplitude = max(tail_values) - min(tail_values)
        leading_covered = bool(late_crossings) and (
            late_crossings[0] - start <= mean_period
        )
        trailing_covered = bool(late_crossings) and (
            stop - late_crossings[-1] <= 1.1 * mean_period
        )
        if tail_amplitude < minimum_amplitude:
            flags.append("amplitude_below_minimum")
        if not (leading_covered and trailing_covered):
            flags.append("window_not_fully_covered")

    if not enough_counted_cycles:
        flags.append("late_crossings_insufficient")

    post_start_amplitude_failures = (
        [value for value in failed_amplitude_times if value >= started_at]
        if started_at is not None
        else []
    )
    # Collapse requires positive evidence from a complete post-onset cycle.
    # A missing final hysteresis confirmation or a partial-cycle crop is only
    # insufficient coverage; phase and a high but valid H must not fabricate
    # terminal amplitude loss.
    stopped_after_complete_cycle = (
        started_at is not None
        and len(late_crossings) >= 2
        and not trailing_covered
        and tail_amplitude < minimum_amplitude
        and all_late_amplitudes[-1] >= minimum_amplitude
    )
    terminal_failure = started_at is not None and (
        bool(post_start_amplitude_failures) or stopped_after_complete_cycle
    )
    if terminal_failure:
        failure_times = [*post_start_amplitude_failures]
        if stopped_after_complete_cycle:
            failure_times.append(tail_start)
        collapse_at_value = min(
            stop,
            max(started_at, min(failure_times)),
        )

    if "sampling_resolution_insufficient" in flags:
        verdict = "unknown"
    elif started_at is None:
        verdict = "not_sustained" if activity else "never_started"
        if verdict == "not_sustained" and "startup_hold_not_met" not in flags:
            flags.append("startup_hold_not_met")
    elif terminal_failure:
        verdict = "collapsed"
    elif started_at > start or not leading_covered:
        verdict = "not_sustained"
    elif not enough_counted_cycles:
        verdict = "not_sustained"
    elif not trailing_covered:
        verdict = "not_sustained"
    elif "amplitude_below_minimum" in flags:
        verdict = "not_sustained"
    elif {"period_inconsistent", "amplitude_inconsistent"} & set(flags):
        verdict = "multimode"
    else:
        verdict = "sustained"

    if verdict == "never_started" and "oscillation_activity_absent" not in flags:
        verdict = "never_started"
        flags.append("oscillation_activity_absent")

    frequency_value: float | None = None
    period_value: float | None = None
    if verdict == "sustained":
        elapsed = selected_crossings[-1] - selected_crossings[0]
        frequency_value = requested_cycles / elapsed
        period_value = elapsed / requested_cycles
        if not math.isfinite(frequency_value) or not math.isfinite(period_value):
            raise _InvalidOscillatorRequest(
                "oscillator.value.non_finite",
                "The N-cycle frequency calculation did not produce finite values.",
            )

    frequency_status = "measured" if verdict == "sustained" else verdict
    period_status = frequency_status
    frequency = _metric(
        status=frequency_status,
        value=frequency_value,
        unit="Hz",
        window_sha256=window_sha256,
    )
    period = _metric(
        status=period_status,
        value=period_value,
        unit="s",
        window_sha256=window_sha256,
    )
    amplitude = _metric(
        status="measured",
        value=differential_peak_to_peak,
        unit="V",
        window_sha256=window_sha256,
    )
    power = _metric(
        status="measured",
        value=average_supply_power,
        unit="W",
        window_sha256=window_sha256,
    )
    if started_at is None and verdict == "not_sustained" and "startup_hold_not_met" not in flags:
        flags.append("startup_hold_not_met")
    collapse_at = (
        {"value": collapse_at_value, "unit": "s"}
        if verdict == "collapsed" and collapse_at_value is not None
        else None
    )
    startup = {
        "status": verdict,
        "started_at": {"value": started_at, "unit": "s"} if started_at is not None else None,
        "time": (
            {"value": started_at - search_start, "unit": "s"}
            if started_at is not None
            else None
        ),
        "collapse_at": collapse_at,
        "search_start": request["startup"]["search_start"],
        "hold_for": request["startup"]["hold_for"],
        "minimum_peak_to_peak": request["startup"]["minimum_peak_to_peak"],
        "extensions": {},
    }
    quality_status = (
        "pass"
        if verdict == "sustained"
        else "unknown"
        if verdict == "unknown"
        else "fail"
    )
    quality = {
        "status": quality_status,
        "period_relative_deviation": period_relative_deviation,
        "amplitude_relative_deviation": amplitude_relative_deviation,
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

    method = {
        key: request[key]
        for key in (
            "kind",
            "signals",
            "window",
            "startup",
            "crossing",
            "quality",
            "power",
        )
    }
    request_sha256 = _canonical_sha256(request)
    method_sha256 = _canonical_sha256(method)
    receipt_without_hash: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "producer": {
            "operation_profile": OPERATION_PROFILE,
            "assertion_profile": ASSERTION_PROFILE,
            "implementation_id": IMPLEMENTATION_ID,
            "implementation_version": IMPLEMENTATION_VERSION,
        },
        "measurement_id": request["measurement_id"],
        "status": verdict,
        "request": request,
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
    receipt = {
        **receipt_without_hash,
        "sha256": _canonical_sha256(receipt_without_hash),
    }

    measurement = _measurement_template(
        measurement_id=request["measurement_id"],
        kind="transient",
        status=verdict,
        request_sha256=request_sha256,
        source_count=1,
    )
    diagnostics: list[dict[str, str]] = []
    if verdict == "multimode":
        diagnostics.append(
            diagnostic(
                "warning",
                "oscillator.quality.multimode",
                "The late waveform has sustained amplitude and crossings but violates the declared period or amplitude consistency bound; frequency is withheld for QC.",
                hint="Inspect the retained quality flags and waveform for beating or multimode behavior.",
            )
        )
    elif verdict == "unknown":
        diagnostics.append(
            diagnostic(
                "error",
                "oscillator.source.resolution_insufficient",
                "The late waveform does not meet the declared samples-per-cycle evidence bound.",
            )
        )
    elif verdict != "sustained":
        non_sustained_message = (
            "A qualifying startup was observed, but the declared late window did not contain enough fully covered cycles."
            if verdict == "not_sustained" and started_at is not None
            else "Oscillatory activity was observed but never satisfied the declared startup hold interval."
        )
        diagnostics.append(
            diagnostic(
                "error",
                f"oscillator.{verdict}",
                {
                    "never_started": "No amplitude-qualified hysteretic oscillation was observed.",
                    "not_sustained": non_sustained_message,
                    "collapsed": "A qualifying startup interval was observed, but oscillation did not remain valid through the declared late window.",
                }[verdict],
            )
        )
    engineering = (
        "pass"
        if verdict == "sustained"
        else "unknown"
        if verdict in {"multimode", "unknown"}
        else "fail"
    )
    summary = {
        "sustained": "Derived sustained oscillator frequency, differential amplitude, and supply power from one late window.",
        "never_started": "The waveform never established amplitude-qualified hysteretic oscillation.",
        "not_sustained": (
            "The oscillator started, but the declared late window did not contain enough fully covered cycles."
            if started_at is not None
            else "The waveform showed activity but did not satisfy the startup hold interval."
        ),
        "collapsed": "The oscillator started and then collapsed before completing the late-window assertion.",
        "multimode": "The waveform was flagged for beating or multimode QC; frequency was withheld.",
        "unknown": "The oscillator waveform resolution is insufficient for the declared method.",
    }[verdict]
    return measurement, transient, receipt, engineering, summary, diagnostics


def _normalized_receipt_condition(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required={"name", "value", "unit"})
    name = _text(item["name"], f"{label}.name")
    unit = _text(item["unit"], f"{label}.unit", limit=64)
    raw = item["value"]
    if isinstance(raw, bool):
        normalized: object = raw
    elif isinstance(raw, (int, float)):
        normalized = _finite(raw, f"{label}.value")
    elif isinstance(raw, str):
        normalized = _text(raw, f"{label}.value")
    else:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.value must be a finite number, string, or boolean.",
        )
    return {"name": name, "value": normalized, "unit": unit}


def _receipt_source(value: object, label: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    source = _closed_object(
        value,
        label,
        required={
            "operation",
            "request_id",
            "artifact_role",
            "artifact_sha256",
            "series_sha256",
            "conditions_sha256",
            "conditions",
            "lineage",
        },
    )
    operation = _text(source["operation"], f"{label}.operation")
    request_id = _text(source["request_id"], f"{label}.request_id", limit=36)
    try:
        parsed_request_id = uuid.UUID(request_id)
    except ValueError as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label}.request_id must be a canonical UUID."
        ) from exc
    if str(parsed_request_id) != request_id:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.request_id must use canonical lowercase UUID form.",
        )
    if source["artifact_role"] != "measurement.source":
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.artifact_role must be exactly 'measurement.source'.",
        )
    artifact_sha256 = _digest(source["artifact_sha256"], f"{label}.artifact_sha256")
    series_sha256 = _digest(source["series_sha256"], f"{label}.series_sha256")
    conditions_sha256 = _digest(
        source["conditions_sha256"], f"{label}.conditions_sha256"
    )
    if artifact_sha256 != series_sha256:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.artifact_sha256 and series_sha256 must identify the same canonical series.",
        )
    if not _is_sequence(source["conditions"]) or len(source["conditions"]) > MAX_CONDITIONS:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.conditions must be an array of at most {MAX_CONDITIONS} entries.",
        )
    conditions = [
        _normalized_receipt_condition(item, f"{label}.conditions[{index}]")
        for index, item in enumerate(source["conditions"])
    ]
    condition_names = [str(item["name"]) for item in conditions]
    if len(condition_names) != len(set(condition_names)):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label}.conditions names must be unique."
        )
    if _canonical_sha256(conditions) != conditions_sha256:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.conditions_sha256 does not match the condition bindings.",
        )
    lineage_value = source["lineage"]
    lineage: dict[str, object] | None
    if lineage_value is None:
        lineage = None
    else:
        raw_lineage = _closed_object(
            lineage_value,
            f"{label}.lineage",
            required={
                "operation",
                "request_id",
                "artifact_role",
                "artifact_sha256",
                "binding",
            },
        )
        lineage_request_id = _text(
            raw_lineage["request_id"], f"{label}.lineage.request_id", limit=36
        )
        try:
            parsed_lineage_id = uuid.UUID(lineage_request_id)
        except ValueError as exc:
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.lineage.request_id must be a canonical UUID.",
            ) from exc
        if str(parsed_lineage_id) != lineage_request_id or raw_lineage["binding"] != "unverified":
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.lineage must use a canonical request UUID and binding 'unverified'.",
            )
        lineage = {
            "operation": _text(
                raw_lineage["operation"], f"{label}.lineage.operation"
            ),
            "request_id": lineage_request_id,
            "artifact_role": _role(
                raw_lineage["artifact_role"], f"{label}.lineage.artifact_role"
            ),
            "artifact_sha256": _digest(
                raw_lineage["artifact_sha256"],
                f"{label}.lineage.artifact_sha256",
            ),
            "binding": "unverified",
        }
    return (
        {
            "operation": operation,
            "request_id": request_id,
            "artifact_role": "measurement.source",
            "artifact_sha256": artifact_sha256,
            "series_sha256": series_sha256,
            "conditions_sha256": conditions_sha256,
            "conditions": conditions,
            "lineage": lineage,
        },
        conditions,
    )


def _receipt_metric(
    value: object,
    label: str,
    *,
    unit: str,
    window_sha256: str,
    expected_status: str,
    nullable: bool,
) -> float | None:
    metric = _closed_object(
        value,
        label,
        required={"status", "value", "unit", "window_sha256", "extensions"},
    )
    _extensions(metric["extensions"], f"{label}.extensions")
    if (
        metric["status"] != expected_status
        or metric["unit"] != unit
        or metric["window_sha256"] != window_sha256
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label} does not retain the required status, unit, and window identity.",
        )
    if metric["value"] is None:
        if not nullable:
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid", f"{label}.value must be finite."
            )
        return None
    if nullable:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label}.value must be null for this status."
        )
    return _finite(metric["value"], f"{label}.value")


def _public_receipt(receipt: Mapping[str, Any]) -> dict[str, object]:
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"frequency_value", "conditions"}
    }


def _receipt_nullable_quantity(
    value: object,
    label: str,
    *,
    unit: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    return _quantity(value, label, unit)


def _receipt_startup_and_quality(
    *,
    startup_value: object,
    quality_value: object,
    request: Mapping[str, Any],
    status: str,
    label: str,
) -> None:
    startup = _closed_object(
        startup_value,
        f"{label}.startup",
        required={
            "status",
            "started_at",
            "time",
            "collapse_at",
            "search_start",
            "hold_for",
            "minimum_peak_to_peak",
            "extensions",
        },
    )
    _extensions(startup["extensions"], f"{label}.startup.extensions")
    if startup["status"] != status:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup.status must match the receipt verdict.",
        )
    for field, unit in (
        ("search_start", "s"),
        ("hold_for", "s"),
        ("minimum_peak_to_peak", "V"),
    ):
        observed = _quantity(
            startup[field], f"{label}.startup.{field}", unit
        )
        if observed != request["startup"][field]:
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.startup.{field} differs from the bound request.",
            )
    started_at = _receipt_nullable_quantity(
        startup["started_at"], f"{label}.startup.started_at", unit="s"
    )
    startup_time = _receipt_nullable_quantity(
        startup["time"], f"{label}.startup.time", unit="s"
    )
    collapse_at = _receipt_nullable_quantity(
        startup["collapse_at"], f"{label}.startup.collapse_at", unit="s"
    )
    if (started_at is None) != (startup_time is None):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup started_at and time must be present together.",
        )
    if started_at is not None and startup_time is not None:
        if not (
            request["startup"]["search_start"]["value"]
            <= started_at["value"]
            <= request["window"]["stop"]["value"]
        ):
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.startup.started_at lies outside the bound observation.",
            )
        if started_at["value"] > (
            request["window"]["stop"]["value"]
            - request["startup"]["hold_for"]["value"]
        ):
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.startup onset leaves no complete bound hold interval.",
            )
        expected_time = started_at["value"] - request["startup"]["search_start"]["value"]
        if expected_time < 0 or not math.isclose(
            startup_time["value"], expected_time, rel_tol=1e-12, abs_tol=1e-18
        ):
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.startup.time does not match started_at minus search_start.",
            )
    if (status == "collapsed") != (collapse_at is not None):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup.collapse_at must be present exactly for collapsed status.",
        )
    if status == "never_started" and started_at is not None:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup onset is incompatible with {status} status.",
        )
    if collapse_at is not None and (
        started_at is None
        or collapse_at["value"] < started_at["value"]
        or collapse_at["value"] > request["window"]["stop"]["value"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup.collapse_at must follow onset within the bound window.",
        )
    if status in {"sustained", "multimode"} and started_at is None:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.startup must retain a qualifying onset for {status} status.",
        )

    quality = _closed_object(
        quality_value,
        f"{label}.quality",
        required={
            "status",
            "period_relative_deviation",
            "amplitude_relative_deviation",
            "minimum_samples_per_cycle_observed",
            "maximum_period_relative_deviation",
            "maximum_amplitude_relative_deviation",
            "minimum_samples_per_cycle_required",
            "flags",
            "extensions",
        },
    )
    _extensions(quality["extensions"], f"{label}.quality.extensions")
    expected_quality_status = (
        "pass" if status == "sustained" else "unknown" if status == "unknown" else "fail"
    )
    if quality["status"] != expected_quality_status:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.quality.status contradicts the receipt verdict.",
        )
    for field in (
        "period_relative_deviation",
        "amplitude_relative_deviation",
    ):
        if quality[field] is not None and _finite(
            quality[field], f"{label}.quality.{field}"
        ) < 0:
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid",
                f"{label}.quality.{field} must be non-negative or null.",
            )
    observed_samples = quality["minimum_samples_per_cycle_observed"]
    if observed_samples is not None:
        _integer(
            observed_samples,
            f"{label}.quality.minimum_samples_per_cycle_observed",
            minimum=0,
            maximum=100_002,
        )
    if (
        quality["maximum_period_relative_deviation"]
        != request["quality"]["maximum_period_relative_deviation"]
        or quality["maximum_amplitude_relative_deviation"]
        != request["quality"]["maximum_amplitude_relative_deviation"]
        or quality["minimum_samples_per_cycle_required"]
        != request["quality"]["minimum_samples_per_cycle"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.quality limits differ from the bound request.",
        )
    allowed_flags = {
        "period_inconsistent",
        "amplitude_inconsistent",
        "sampling_resolution_insufficient",
        "amplitude_below_minimum",
        "window_not_fully_covered",
        "late_crossings_insufficient",
        "startup_hold_not_met",
        "oscillation_activity_absent",
    }
    raw_flags = quality["flags"]
    if (
        not _is_sequence(raw_flags)
        or len(raw_flags) > len(allowed_flags)
        or any(not isinstance(flag, str) for flag in raw_flags)
        or any(flag not in allowed_flags for flag in raw_flags)
        or len(set(raw_flags)) != len(raw_flags)
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label}.quality.flags is invalid."
        )
    flags = set(raw_flags)
    if status == "sustained" and flags:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.quality.flags must be empty for sustained status.",
        )
    if status == "sustained" and (
        quality["period_relative_deviation"] is None
        or quality["amplitude_relative_deviation"] is None
        or quality["minimum_samples_per_cycle_observed"] is None
        or quality["period_relative_deviation"]
        > request["quality"]["maximum_period_relative_deviation"]
        or quality["amplitude_relative_deviation"]
        > request["quality"]["maximum_amplitude_relative_deviation"]
        or quality["minimum_samples_per_cycle_observed"]
        < request["quality"]["minimum_samples_per_cycle"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.quality does not satisfy the bound sustained limits.",
        )
    if status == "multimode" and not {
        "period_inconsistent",
        "amplitude_inconsistent",
    } & flags:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.quality must retain a consistency flag for multimode status.",
        )


def _receipt(value: object, label: str) -> dict[str, Any]:
    item = _closed_object(
        value,
        label,
        required={
            "schema",
            "sha256",
            "producer",
            "measurement_id",
            "status",
            "request",
            "request_sha256",
            "method_sha256",
            "series_sha256",
            "window_sha256",
            "source",
            "window",
            "frequency",
            "period",
            "differential_peak_to_peak",
            "average_supply_power",
            "startup",
            "quality",
            "extensions",
        },
    )
    _extensions(item["extensions"], f"{label}.extensions")
    if item["schema"] != RECEIPT_SCHEMA:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.schema must be exactly {RECEIPT_SCHEMA!r}.",
        )
    expected_sha256 = _digest(item["sha256"], f"{label}.sha256")
    if oscillator_receipt_sha256(item) != expected_sha256:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.digest_mismatch",
            f"{label}.sha256 does not match the canonical receipt content.",
        )
    status = _text(item["status"], f"{label}.status", limit=40)
    if status not in _VERDICTS:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid", f"{label}.status is not a typed oscillator verdict."
        )
    measurement_id = _role(item["measurement_id"], f"{label}.measurement_id")
    for field in ("request_sha256", "method_sha256", "series_sha256", "window_sha256"):
        _digest(item[field], f"{label}.{field}")
    producer = _closed_object(
        item["producer"],
        f"{label}.producer",
        required={
            "operation_profile",
            "assertion_profile",
            "implementation_id",
            "implementation_version",
        },
    )
    expected_producer = {
        "operation_profile": OPERATION_PROFILE,
        "assertion_profile": ASSERTION_PROFILE,
        "implementation_id": IMPLEMENTATION_ID,
        "implementation_version": IMPLEMENTATION_VERSION,
    }
    if dict(producer) != expected_producer:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.producer does not identify this exact oscillator implementation.",
        )
    try:
        bound_request = _normalize_transient_request(item["request"])
    except _InvalidOscillatorRequest as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.request is not a valid closed transient request: {exc}",
        ) from exc
    method = {
        key: bound_request[key]
        for key in (
            "kind",
            "signals",
            "window",
            "startup",
            "crossing",
            "quality",
            "power",
        )
    }
    if (
        bound_request["measurement_id"] != measurement_id
        or _canonical_sha256(bound_request) != item["request_sha256"]
        or _canonical_sha256(method) != item["method_sha256"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label} request and method digests do not match the retained request.",
        )

    if status == "sustained":
        frequency_status = "measured"
        frequency_value = _receipt_metric(
            item["frequency"],
            f"{label}.frequency",
            unit="Hz",
            window_sha256=str(item["window_sha256"]),
            expected_status=frequency_status,
            nullable=False,
        )
        assert frequency_value is not None
        if frequency_value <= 0:
            raise _InvalidOscillatorRequest(
                "oscillator.receipt.invalid", f"{label}.frequency.value must be positive."
            )
    else:
        frequency_status = status
        frequency_value = _receipt_metric(
            item["frequency"],
            f"{label}.frequency",
            unit="Hz",
            window_sha256=str(item["window_sha256"]),
            expected_status=frequency_status,
            nullable=True,
        )

    # A receipt binds all coupled waveform facts to one window even though grid
    # composition consumes only frequency and condition identity.
    window = _closed_object(
        item["window"],
        f"{label}.window",
        required={
            "start",
            "stop",
            "cycle_count",
            "boundary_policy",
            "sample_count",
            "series_sha256",
            "window_sha256",
            "signals",
            "extensions",
        },
    )
    _extensions(window["extensions"], f"{label}.window.extensions")
    start = _quantity(window["start"], f"{label}.window.start", "s")
    stop = _quantity(window["stop"], f"{label}.window.stop", "s")
    if stop["value"] <= start["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.window.stop must be greater than start.",
        )
    _integer(
        window["cycle_count"],
        f"{label}.window.cycle_count",
        minimum=2,
        maximum=100_000,
    )
    _integer(
        window["sample_count"],
        f"{label}.window.sample_count",
        minimum=2,
        maximum=100_002,
    )
    if window["boundary_policy"] != "closed-linear-interpolation":
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.window.boundary_policy is unsupported.",
        )
    signals = _closed_object(
        window["signals"],
        f"{label}.window.signals",
        required={"positive", "negative", "supply_voltage", "supply_current"},
    )
    normalized_signals = {
        name: _text(signals[name], f"{label}.window.signals.{name}")
        for name in ("positive", "negative", "supply_voltage", "supply_current")
    }
    if (
        start != bound_request["window"]["start"]
        or stop != bound_request["window"]["stop"]
        or window["cycle_count"] != bound_request["window"]["cycle_count"]
        or normalized_signals != bound_request["signals"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.window differs from the retained request method.",
        )
    expected_window_sha256 = _canonical_sha256(
        {
            "series_sha256": item["series_sha256"],
            "start": start,
            "stop": stop,
            "boundary_policy": "closed-linear-interpolation",
            "signals": normalized_signals,
        }
    )
    if (
        window["series_sha256"] != item["series_sha256"]
        or window["window_sha256"] != item["window_sha256"]
        or expected_window_sha256 != item["window_sha256"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.window does not match the receipt source/window identity.",
        )
    source, conditions = _receipt_source(item["source"], f"{label}.source")
    if source["series_sha256"] != item["series_sha256"]:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.source does not match the receipt series_sha256.",
        )
    expected_period_status = "measured" if status == "sustained" else status
    period_value = _receipt_metric(
        item["period"],
        f"{label}.period",
        unit="s",
        window_sha256=str(item["window_sha256"]),
        expected_status=expected_period_status,
        nullable=status != "sustained",
    )
    amplitude_value = _receipt_metric(
        item["differential_peak_to_peak"],
        f"{label}.differential_peak_to_peak",
        unit="V",
        window_sha256=str(item["window_sha256"]),
        expected_status="measured",
        nullable=False,
    )
    _receipt_metric(
        item["average_supply_power"],
        f"{label}.average_supply_power",
        unit="W",
        window_sha256=str(item["window_sha256"]),
        expected_status="measured",
        nullable=False,
    )
    if amplitude_value is None or amplitude_value < 0:
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.differential_peak_to_peak must be non-negative.",
        )
    if status == "sustained" and (
        amplitude_value < bound_request["startup"]["minimum_peak_to_peak"]["value"]
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.differential_peak_to_peak is below the bound sustained minimum.",
        )
    if status == "sustained" and (
        period_value is None
        or period_value <= 0
        or not math.isclose(
            frequency_value * period_value,
            1.0,
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.receipt.invalid",
            f"{label}.frequency and period are not reciprocal sustained metrics.",
        )
    _receipt_startup_and_quality(
        startup_value=item["startup"],
        quality_value=item["quality"],
        request=bound_request,
        status=status,
        label=label,
    )
    return {
        **_public_receipt(item),
        "source": source,
        "status": status,
        "frequency_value": frequency_value,
        "conditions": conditions,
    }


def _condition_value(
    receipt: Mapping[str, Any],
    condition_name: str,
    unit: str,
    *,
    label: str,
) -> float:
    matches = [
        item
        for item in receipt["conditions"]
        if isinstance(item, Mapping) and item.get("name") == condition_name
    ]
    if len(matches) != 1:
        raise _InvalidOscillatorRequest(
            "oscillator.condition.mismatch",
            f"{label} must contain exactly one condition named {condition_name!r}.",
        )
    item = _closed_object(
        matches[0],
        f"{label}.condition[{condition_name}]",
        required={"name", "value", "unit"},
    )
    if item["unit"] != unit:
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch",
            f"{label} condition {condition_name!r} has unit {item['unit']!r}; expected {unit!r}.",
        )
    return _finite(item["value"], f"{label}.condition[{condition_name}].value")


def _receipt_context(
    receipt: Mapping[str, Any], *, excluding: str
) -> list[dict[str, object]]:
    return sorted(
        [
            dict(item)
            for item in receipt["conditions"]
            if isinstance(item, Mapping) and item.get("name") != excluding
        ],
        key=lambda item: str(item.get("name")),
    )


def _normalize_grid_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _closed_object(
        value,
        "measurement",
        required={
            "measurement_id",
            "kind",
            "control_condition",
            "control_unit",
            "expected_monotonicity",
            "points",
            "extensions",
        },
    )
    _extensions(request["extensions"], "measurement.extensions")
    if request["kind"] != "tuning_grid":
        raise _InvalidOscillatorRequest(
            "oscillator.kind.unsupported", "measurement.kind must be tuning_grid."
        )
    control_unit = _text(request["control_unit"], "measurement.control_unit", limit=64)
    if control_unit != "V":
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch", "v1alpha1 tuning-grid control_unit must be 'V'."
        )
    expected = _text(
        request["expected_monotonicity"],
        "measurement.expected_monotonicity",
        limit=32,
    )
    if expected not in {"nondecreasing", "nonincreasing"}:
        raise _InvalidOscillatorRequest(
            "oscillator.grid.invalid",
            "expected_monotonicity must be nondecreasing or nonincreasing.",
        )
    if not _is_sequence(request["points"]) or not 3 <= len(request["points"]) <= MAX_GRID_POINTS:
        raise _InvalidOscillatorRequest(
            "oscillator.grid.invalid",
            f"measurement.points must contain between 3 and {MAX_GRID_POINTS} points.",
        )
    points: list[dict[str, Any]] = []
    for index, raw_point in enumerate(request["points"]):
        point = _closed_object(
            raw_point,
            f"measurement.points[{index}]",
            required={"control", "receipt"},
        )
        control = _quantity(
            point["control"], f"measurement.points[{index}].control", control_unit
        )
        receipt = _receipt(point["receipt"], f"measurement.points[{index}].receipt")
        receipt_control = _condition_value(
            receipt,
            _text(request["control_condition"], "measurement.control_condition"),
            control_unit,
            label=f"measurement.points[{index}].receipt",
        )
        if receipt_control != control["value"]:
            raise _InvalidOscillatorRequest(
                "oscillator.condition.mismatch",
                f"measurement.points[{index}].control does not match its receipt condition.",
            )
        points.append({"control": control, "receipt": receipt})
    controls = [point["control"]["value"] for point in points]
    if any(right <= left for left, right in zip(controls, controls[1:])):
        raise _InvalidOscillatorRequest(
            "oscillator.grid.invalid",
            "Tuning-grid control points must be strictly increasing in declared order.",
        )
    receipt_hashes = [point["receipt"]["sha256"] for point in points]
    if len(receipt_hashes) != len(set(receipt_hashes)):
        raise _InvalidOscillatorRequest(
            "oscillator.grid.invalid", "Tuning-grid receipt identities must be unique."
        )
    method_hashes = {point["receipt"]["method_sha256"] for point in points}
    if len(method_hashes) != 1:
        raise _InvalidOscillatorRequest(
            "oscillator.grid.method_mismatch",
            "Every tuning-grid receipt must use the same transient measurement method.",
        )
    control_condition = _role(
        request["control_condition"], "measurement.control_condition"
    )
    contexts = [
        _receipt_context(point["receipt"], excluding=control_condition)
        for point in points
    ]
    context_sha256 = _canonical_sha256(contexts[0])
    if any(_canonical_sha256(context) != context_sha256 for context in contexts[1:]):
        raise _InvalidOscillatorRequest(
            "oscillator.condition.mismatch",
            "Tuning-grid receipts differ in conditions other than the declared control condition.",
        )
    return {
        "measurement_id": _role(request["measurement_id"], "measurement.measurement_id"),
        "kind": "tuning_grid",
        "control_condition": control_condition,
        "control_unit": control_unit,
        "expected_monotonicity": expected,
        "points": points,
        "extensions": {},
    }


def _local_tuning_gain(controls: Sequence[float], frequencies: Sequence[float]) -> list[float]:
    try:
        output = [(frequencies[1] - frequencies[0]) / (controls[1] - controls[0])]
        for index in range(1, len(controls) - 1):
            h0 = controls[index] - controls[index - 1]
            h1 = controls[index + 1] - controls[index]
            left_secant = (frequencies[index] - frequencies[index - 1]) / h0
            right_secant = (frequencies[index + 1] - frequencies[index]) / h1
            # This is algebraically the derivative at x_i of the unique
            # quadratic through the three neighboring points.  Expressing it
            # as weighted secants avoids cancellation between three GHz-scale
            # absolute values.
            derivative = (
                h1 * left_secant + h0 * right_secant
            ) / (h0 + h1)
            output.append(derivative)
        output.append(
            (frequencies[-1] - frequencies[-2])
            / (controls[-1] - controls[-2])
        )
    except (OverflowError, ZeroDivisionError) as exc:
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "Local tuning-gain calculation overflowed the finite result range.",
        ) from exc
    if any(not math.isfinite(value) for value in output):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "Local tuning-gain calculation did not produce finite values.",
        )
    return output


def _measure_grid(
    request: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object], str, str, list[dict[str, str]]]:
    request_for_hash = {
        **request,
        "points": [
            {
                "control": point["control"],
                "receipt": _public_receipt(point["receipt"]),
            }
            for point in request["points"]
        ],
    }
    request_sha256 = _canonical_sha256(request_for_hash)
    controls = [point["control"]["value"] for point in request["points"]]
    receipts = [point["receipt"] for point in request["points"]]
    invalid = [receipt for receipt in receipts if receipt["status"] != "sustained"]
    grid_identity = {
        "control_condition": request["control_condition"],
        "control_unit": request["control_unit"],
        "controls": controls,
        "receipt_sha256": [receipt["sha256"] for receipt in receipts],
    }
    grid_sha256 = _canonical_sha256(grid_identity)
    base_points = [
        {
            "control": point["control"],
            "frequency": {
                "status": (
                    "measured" if receipt["status"] == "sustained" else receipt["status"]
                ),
                "value": receipt["frequency_value"],
                "unit": "Hz",
            },
            "local_tuning_gain": {
                "status": "unknown",
                "value": None,
                "unit": "Hz/V",
            },
            "stencil": "forward" if index == 0 else "backward" if index == len(receipts) - 1 else "central_nonuniform_quadratic",
            "receipt_sha256": receipt["sha256"],
            "extensions": {},
        }
        for index, (point, receipt) in enumerate(zip(request["points"], receipts))
    ]
    if invalid:
        grid = {
            "status": "unknown",
            "control_condition": request["control_condition"],
            "control_unit": request["control_unit"],
            "expected_monotonicity": request["expected_monotonicity"],
            "observed_monotonicity": "unknown",
            "monotonicity_check": "unknown",
            "grid_sha256": grid_sha256,
            "points": base_points,
            "span": _metric(status="unknown", value=None, unit="Hz", window_sha256=None),
            "extensions": {},
        }
        measurement = _measurement_template(
            measurement_id=request["measurement_id"],
            kind="tuning_grid",
            status="unknown",
            request_sha256=request_sha256,
            source_count=len(receipts),
        )
        return (
            measurement,
            grid,
            "unknown",
            "The tuning grid contains one or more typed non-sustained oscillator receipts.",
            [
                diagnostic(
                    "error",
                    "oscillator.grid.incomplete",
                    "Local tuning gain and span are withheld because every declared grid point must be sustained.",
                )
            ],
        )

    frequencies = [float(receipt["frequency_value"]) for receipt in receipts]
    gains = _local_tuning_gain(controls, frequencies)
    for point, gain in zip(base_points, gains):
        point["local_tuning_gain"] = {
            "status": "measured",
            "value": gain,
            "unit": "Hz/V",
        }
    differences = [right - left for left, right in zip(frequencies, frequencies[1:])]
    if all(value == 0 for value in differences):
        observed = "constant"
    elif all(value >= 0 for value in differences):
        observed = "nondecreasing"
    elif all(value <= 0 for value in differences):
        observed = "nonincreasing"
    else:
        observed = "non_monotonic"
    check = (
        "pass"
        if observed in {"constant", request["expected_monotonicity"]}
        else "fail"
    )
    span_value = max(frequencies) - min(frequencies)
    span = _metric(status="measured", value=span_value, unit="Hz", window_sha256=None)
    grid = {
        "status": "measured",
        "control_condition": request["control_condition"],
        "control_unit": request["control_unit"],
        "expected_monotonicity": request["expected_monotonicity"],
        "observed_monotonicity": observed,
        "monotonicity_check": check,
        "grid_sha256": grid_sha256,
        "points": base_points,
        "span": span,
        "extensions": {},
    }
    measurement = _measurement_template(
        measurement_id=request["measurement_id"],
        kind="tuning_grid",
        status="measured",
        request_sha256=request_sha256,
        source_count=len(receipts),
    )
    diagnostics: list[dict[str, str]] = []
    if check == "fail":
        diagnostics.append(
            diagnostic(
                "warning",
                "oscillator.grid.non_monotonic",
                f"Observed frequency behavior is {observed}, not the declared {request['expected_monotonicity']} direction.",
                hint="Inspect every returned local tuning-gain point; the curve is retained rather than collapsed.",
            )
        )
    return (
        measurement,
        grid,
        "pass",
        "Derived the complete per-point local tuning-gain curve and frequency span.",
        diagnostics,
    )


def _normalize_shift_member(
    value: object,
    label: str,
    *,
    perturbation_condition: str,
) -> dict[str, Any]:
    item = _closed_object(value, label, required={"condition", "receipt"})
    condition = _quantity(item["condition"], f"{label}.condition")
    receipt = _receipt(item["receipt"], f"{label}.receipt")
    observed = _condition_value(
        receipt,
        perturbation_condition,
        str(condition["unit"]),
        label=f"{label}.receipt",
    )
    if observed != condition["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.condition.mismatch",
            f"{label}.condition does not match the receipt condition binding.",
        )
    return {"condition": condition, "receipt": receipt}


def _normalize_shift_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _closed_object(
        value,
        "measurement",
        required={
            "measurement_id",
            "kind",
            "perturbation_condition",
            "reference",
            "perturbed",
            "extensions",
        },
    )
    _extensions(request["extensions"], "measurement.extensions")
    perturbation_condition = _role(
        request["perturbation_condition"], "measurement.perturbation_condition"
    )
    reference = _normalize_shift_member(
        request["reference"],
        "measurement.reference",
        perturbation_condition=perturbation_condition,
    )
    perturbed = _normalize_shift_member(
        request["perturbed"],
        "measurement.perturbed",
        perturbation_condition=perturbation_condition,
    )
    if reference["condition"]["unit"] != perturbed["condition"]["unit"]:
        raise _InvalidOscillatorRequest(
            "oscillator.unit.mismatch",
            "Reference and perturbed condition units must match exactly.",
        )
    if reference["condition"]["value"] == perturbed["condition"]["value"]:
        raise _InvalidOscillatorRequest(
            "oscillator.shift.invalid",
            "Reference and perturbed condition values must differ.",
        )
    if reference["receipt"]["sha256"] == perturbed["receipt"]["sha256"]:
        raise _InvalidOscillatorRequest(
            "oscillator.shift.invalid", "Frequency shift requires two distinct receipts."
        )
    # Conditions other than the named perturbation are the comparison context
    # and must be identical; this prevents a supply-pushing result from also
    # changing control, temperature, or load without declaring another pair.
    if _canonical_sha256(
        _receipt_context(reference["receipt"], excluding=perturbation_condition)
    ) != _canonical_sha256(
        _receipt_context(perturbed["receipt"], excluding=perturbation_condition)
    ):
        raise _InvalidOscillatorRequest(
            "oscillator.condition.mismatch",
            "Frequency-shift receipts differ in conditions other than the declared perturbation.",
        )
    if reference["receipt"]["method_sha256"] != perturbed["receipt"]["method_sha256"]:
        raise _InvalidOscillatorRequest(
            "oscillator.shift.method_mismatch",
            "Frequency-shift receipts must use the same transient measurement method.",
        )
    return {
        "measurement_id": _role(request["measurement_id"], "measurement.measurement_id"),
        "kind": "frequency_shift",
        "perturbation_condition": perturbation_condition,
        "reference": reference,
        "perturbed": perturbed,
        "extensions": {},
    }


def _measure_shift(
    request: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object], str, str, list[dict[str, str]]]:
    request_sha256 = _canonical_sha256(
        {
            **request,
            "reference": {
                "condition": request["reference"]["condition"],
                "receipt": _public_receipt(request["reference"]["receipt"]),
            },
            "perturbed": {
                "condition": request["perturbed"]["condition"],
                "receipt": _public_receipt(request["perturbed"]["receipt"]),
            },
        }
    )
    reference = request["reference"]
    perturbed = request["perturbed"]
    receipts = (reference["receipt"], perturbed["receipt"])
    pair_sha256 = _canonical_sha256(
        {
            "perturbation_condition": request["perturbation_condition"],
            "reference_condition": reference["condition"],
            "perturbed_condition": perturbed["condition"],
            "reference_receipt_sha256": receipts[0]["sha256"],
            "perturbed_receipt_sha256": receipts[1]["sha256"],
        }
    )
    if any(receipt["status"] != "sustained" for receipt in receipts):
        shift = {
            "status": "unknown",
            "perturbation_condition": request["perturbation_condition"],
            "condition_unit": reference["condition"]["unit"],
            "reference_condition": reference["condition"],
            "perturbed_condition": perturbed["condition"],
            "reference_receipt_sha256": receipts[0]["sha256"],
            "perturbed_receipt_sha256": receipts[1]["sha256"],
            "pair_sha256": pair_sha256,
            "signed_shift": _metric(status="unknown", value=None, unit="Hz", window_sha256=None),
            "absolute_shift": _metric(status="unknown", value=None, unit="Hz", window_sha256=None),
            "extensions": {},
        }
        measurement = _measurement_template(
            measurement_id=request["measurement_id"],
            kind="frequency_shift",
            status="unknown",
            request_sha256=request_sha256,
            source_count=2,
        )
        return (
            measurement,
            shift,
            "unknown",
            "Frequency shift is withheld because one paired oscillator receipt is not sustained.",
            [
                diagnostic(
                    "error",
                    "oscillator.shift.incomplete",
                    "Both reference and perturbed receipts must carry measured sustained frequency.",
                )
            ],
        )
    signed = float(receipts[1]["frequency_value"]) - float(receipts[0]["frequency_value"])
    if not math.isfinite(signed):
        raise _InvalidOscillatorRequest(
            "oscillator.value.non_finite",
            "The paired frequency subtraction did not produce a finite shift.",
        )
    shift = {
        "status": "measured",
        "perturbation_condition": request["perturbation_condition"],
        "condition_unit": reference["condition"]["unit"],
        "reference_condition": reference["condition"],
        "perturbed_condition": perturbed["condition"],
        "reference_receipt_sha256": receipts[0]["sha256"],
        "perturbed_receipt_sha256": receipts[1]["sha256"],
        "pair_sha256": pair_sha256,
        "signed_shift": _metric(status="measured", value=signed, unit="Hz", window_sha256=None),
        "absolute_shift": _metric(status="measured", value=abs(signed), unit="Hz", window_sha256=None),
        "extensions": {},
    }
    measurement = _measurement_template(
        measurement_id=request["measurement_id"],
        kind="frequency_shift",
        status="measured",
        request_sha256=request_sha256,
        source_count=2,
    )
    return (
        measurement,
        shift,
        "pass",
        "Derived signed and absolute frequency shift from one explicitly paired receipt context.",
        [],
    )


def measure_oscillator(
    series: Mapping[str, object] | None,
    measurement: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run one closed oscillator transient, tuning-grid, or shift request."""

    try:
        correlation_id = _request_id(request_id)
    except _InvalidOscillatorRequest as exc:
        return _payload(
            str(uuid.uuid4()),
            _measurement_template(measurement_id=None, kind=None),
            engineering_status="unknown",
            summary="The oscillator result correlation identity is invalid.",
            execution_status=exc.execution_status,
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    measurement_id: str | None = None
    kind: str | None = None
    try:
        measurement_id, kind, base = _normalize_base_request(measurement)
        if kind == "transient":
            if series is None:
                raise _InvalidOscillatorRequest(
                    "oscillator.source.missing",
                    "Transient oscillator measurement requires one normalized series.",
                )
            try:
                normalized = _normalize_series(series)
            except _SeriesInvalidRequest as exc:
                source_code = {
                    "measurement.source.digest_mismatch": (
                        "oscillator.source.digest_mismatch"
                    ),
                    "measurement.source.over_limit": "oscillator.source.over_limit",
                }.get(exc.code, "oscillator.source.invalid")
                raise _InvalidOscillatorRequest(source_code, str(exc)) from exc
            request = _normalize_transient_request(base)
            measured, transient, receipt, engineering, summary, diagnostics = (
                _measure_transient(normalized, request)
            )
            return _payload(
                correlation_id,
                measured,
                engineering_status=engineering,
                summary=summary,
                transient=transient,
                receipt=receipt,
                diagnostics=diagnostics,
            )
        if series is not None:
            raise _InvalidOscillatorRequest(
                "oscillator.source.unexpected",
                f"{kind} composes embedded receipts and does not accept a normalized series.",
            )
        if kind == "tuning_grid":
            request = _normalize_grid_request(base)
            measured, grid, engineering, summary, diagnostics = _measure_grid(request)
            return _payload(
                correlation_id,
                measured,
                engineering_status=engineering,
                summary=summary,
                grid=grid,
                diagnostics=diagnostics,
            )
        request = _normalize_shift_request(base)
        measured, shift, engineering, summary, diagnostics = _measure_shift(request)
        return _payload(
            correlation_id,
            measured,
            engineering_status=engineering,
            summary=summary,
            shift=shift,
            diagnostics=diagnostics,
        )
    except _InvalidOscillatorRequest as exc:
        return _payload(
            correlation_id,
            _measurement_template(measurement_id=measurement_id, kind=kind),
            engineering_status="unknown",
            summary="The oscillator measurement request could not be evaluated safely.",
            execution_status=exc.execution_status,
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )


__all__ = [
    "ASSERTION_PROFILE",
    "IMPLEMENTATION_ID",
    "IMPLEMENTATION_VERSION",
    "OSCILLATION_STATUSES",
    "OSCILLATOR_MEASUREMENT_KINDS",
    "OPERATION_PROFILE",
    "RECEIPT_SCHEMA",
    "measure_oscillator",
    "oscillator_receipt_sha256",
]
