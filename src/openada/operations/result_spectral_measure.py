"""Closed spectral measurements over provenance-bound normalized real series."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import cmath
import hashlib
import json
import math
import re
from typing import Any
import uuid

from ..contract import diagnostic, result, static_execution
from .result_measure import _InvalidRequest as _SeriesInvalidRequest
from .result_measure import _normalize_series


OPERATION_PROFILE = "openada.operation/result.spectral.measure/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/spectral.measurement.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.spectral-evidence"
IMPLEMENTATION_VERSION = "1.0.0"
METHOD_ID = "openada.method/coherent-single-tone-fft/v1alpha1"
MAX_FFT_POINTS = 65_536
MAX_HARMONIC_ORDER = 64

SPECTRAL_METRIC_KINDS = ("snr", "sinad", "thd", "sfdr")
_METRICS = frozenset(SPECTRAL_METRIC_KINDS)
_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_STANDARD_REFERENCES = {
    "generic-sampled-waveform": "none",
    "adc": "ieee-1241-2023",
    "dac": "ieee-1658-2023",
    "waveform-recorder": "ieee-1057-2017",
}


class _InvalidSpectralRequest(ValueError):
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
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} must be an object."
        )
    keys = set(value)
    if any(not isinstance(key, str) for key in value):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} field names must be strings."
        )
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if extra:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(extra))}.",
        )
    return value


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} must be a JSON number."
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} must be finite."
        )
    return parsed


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} must be an integer."
        )
    if not minimum <= value <= maximum:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            f"{label} must be between {minimum} and {maximum}.",
        )
    return value


def _expect(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise _InvalidSpectralRequest(
            "spectral.method.unsupported",
            f"{label} must be exactly {expected!r} for {METHOD_ID}.",
        )


def _extensions(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required=set())
    if item:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", f"{label} must be empty in v1alpha1."
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


def _request_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", "request_id must be a canonical UUID."
        ) from exc
    if str(parsed) != value:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", "request_id must be a canonical lowercase UUID."
        )
    return value


def _quantity_hz(value: object, label: str) -> float:
    item = _closed_object(value, label, required={"value", "unit"})
    if item["unit"] != "Hz":
        raise _InvalidSpectralRequest(
            "spectral.unit.mismatch", f"{label}.unit must be exactly 'Hz'."
        )
    return _finite(item["value"], f"{label}.value")


def _normalize_request(
    request: object,
    *,
    point_count: int,
) -> dict[str, Any]:
    root = _closed_object(
        request,
        "spectral",
        required={
            "measurement_id",
            "signal",
            "method",
            "band",
            "fundamental",
            "harmonics",
            "metric",
            "standards_context",
            "extensions",
        },
    )
    _extensions(root["extensions"], "spectral.extensions")
    measurement_id = _text(root["measurement_id"], "spectral.measurement_id", limit=120)
    if not _ROLE_RE.fullmatch(measurement_id):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            "spectral.measurement_id is not a canonical identifier.",
        )

    method = _closed_object(
        root["method"],
        "spectral.method",
        required={
            "id",
            "dft_length",
            "uniformity_relative_tolerance",
            "coherent_bin_tolerance",
            "coherent_sampling",
            "window",
            "detrend",
            "sidedness",
            "scaling",
            "averaging",
            "missing_samples",
            "clipping",
        },
    )
    _expect(method["id"], METHOD_ID, "spectral.method.id")
    dft_length = _integer(
        method["dft_length"],
        "spectral.method.dft_length",
        minimum=8,
        maximum=MAX_FFT_POINTS,
    )
    if dft_length != point_count:
        raise _InvalidSpectralRequest(
            "spectral.method.record_length_mismatch",
            "spectral.method.dft_length must equal the exact source record length.",
        )
    if dft_length & (dft_length - 1):
        raise _InvalidSpectralRequest(
            "spectral.method.unsupported",
            "v1alpha1 requires a power-of-two DFT length.",
        )
    uniformity_tolerance = _finite(
        method["uniformity_relative_tolerance"],
        "spectral.method.uniformity_relative_tolerance",
    )
    coherent_tolerance = _finite(
        method["coherent_bin_tolerance"],
        "spectral.method.coherent_bin_tolerance",
    )
    if not 0 <= uniformity_tolerance <= 1e-3:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            "uniformity_relative_tolerance must be between 0 and 0.001.",
        )
    if not 0 <= coherent_tolerance <= 1e-6:
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            "coherent_bin_tolerance must be between 0 and 0.000001 bins.",
        )
    for name, expected in {
        "coherent_sampling": "required",
        "window": "rectangular",
        "detrend": "mean",
        "sidedness": "one-sided",
        "scaling": "mean-square-per-bin",
        "averaging": "none",
        "missing_samples": "reject",
        "clipping": "not-assessed",
    }.items():
        _expect(method[name], expected, f"spectral.method.{name}")

    band = _closed_object(root["band"], "spectral.band", required={"lower", "upper"})
    lower_hz = _quantity_hz(band["lower"], "spectral.band.lower")
    upper_hz = _quantity_hz(band["upper"], "spectral.band.upper")
    if lower_hz < 0 or upper_hz <= lower_hz:
        raise _InvalidSpectralRequest(
            "spectral.band.invalid",
            "The closed spectral band must have 0 <= lower < upper.",
        )

    fundamental = _closed_object(
        root["fundamental"],
        "spectral.fundamental",
        required={"method", "frequency", "integration_half_width_bins"},
    )
    _expect(
        fundamental["method"],
        "declared-coherent-bin",
        "spectral.fundamental.method",
    )
    _integer(
        fundamental["integration_half_width_bins"],
        "spectral.fundamental.integration_half_width_bins",
        minimum=0,
        maximum=0,
    )
    fundamental_hz = _quantity_hz(
        fundamental["frequency"], "spectral.fundamental.frequency"
    )
    if fundamental_hz <= 0:
        raise _InvalidSpectralRequest(
            "spectral.fundamental.invalid",
            "The declared fundamental frequency must be greater than zero.",
        )

    harmonics = _closed_object(
        root["harmonics"],
        "spectral.harmonics",
        required={
            "orders",
            "aliasing",
            "collision",
            "out_of_band",
            "integration_half_width_bins",
        },
    )
    if not isinstance(harmonics["orders"], Sequence) or isinstance(
        harmonics["orders"], (str, bytes, bytearray)
    ):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid", "spectral.harmonics.orders must be an array."
        )
    if len(harmonics["orders"]) > MAX_HARMONIC_ORDER - 1:
        raise _InvalidSpectralRequest(
            "spectral.request.over_limit", "Too many harmonic orders were requested."
        )
    orders = [
        _integer(
            item,
            f"spectral.harmonics.orders[{index}]",
            minimum=2,
            maximum=MAX_HARMONIC_ORDER,
        )
        for index, item in enumerate(harmonics["orders"])
    ]
    if orders != sorted(set(orders)):
        raise _InvalidSpectralRequest(
            "spectral.request.invalid",
            "spectral.harmonics.orders must be unique and strictly increasing.",
        )
    _expect(harmonics["aliasing"], "fold-first-nyquist", "spectral.harmonics.aliasing")
    _expect(harmonics["collision"], "reject", "spectral.harmonics.collision")
    _expect(harmonics["out_of_band"], "exclude", "spectral.harmonics.out_of_band")
    _integer(
        harmonics["integration_half_width_bins"],
        "spectral.harmonics.integration_half_width_bins",
        minimum=0,
        maximum=0,
    )

    metric = _closed_object(root["metric"], "spectral.metric", required={"kind", "unit"})
    metric_kind = _text(metric["kind"], "spectral.metric.kind", limit=16)
    if metric_kind not in _METRICS:
        raise _InvalidSpectralRequest(
            "spectral.metric.unsupported",
            f"spectral.metric.kind must be one of {', '.join(SPECTRAL_METRIC_KINDS)}.",
        )
    _expect(metric["unit"], "dB", "spectral.metric.unit")

    context = _closed_object(
        root["standards_context"],
        "spectral.standards_context",
        required={"domain", "reference", "alignment"},
    )
    domain = _text(context["domain"], "spectral.standards_context.domain", limit=40)
    if domain not in _STANDARD_REFERENCES:
        raise _InvalidSpectralRequest(
            "spectral.standard_context.invalid",
            "The standards context domain is unsupported.",
        )
    expected_reference = _STANDARD_REFERENCES[domain]
    if context["reference"] != expected_reference:
        raise _InvalidSpectralRequest(
            "spectral.standard_context.invalid",
            f"The {domain!r} domain requires reference {expected_reference!r}.",
        )
    alignment = _text(
        context["alignment"], "spectral.standards_context.alignment", limit=32
    )
    expected_alignment = (
        "openada-definition" if domain == "generic-sampled-waveform" else "candidate"
    )
    if alignment != expected_alignment:
        raise _InvalidSpectralRequest(
            "spectral.standard_context.invalid",
            f"The public-scope standards context permits only {expected_alignment!r} alignment.",
        )

    return {
        "measurement_id": measurement_id,
        "signal": _text(root["signal"], "spectral.signal"),
        "method": {
            "id": METHOD_ID,
            "dft_length": dft_length,
            "uniformity_relative_tolerance": uniformity_tolerance,
            "coherent_bin_tolerance": coherent_tolerance,
            "coherent_sampling": "required",
            "window": "rectangular",
            "detrend": "mean",
            "sidedness": "one-sided",
            "scaling": "mean-square-per-bin",
            "averaging": "none",
            "missing_samples": "reject",
            "clipping": "not-assessed",
        },
        "band": {"lower_hz": lower_hz, "upper_hz": upper_hz},
        "fundamental": {
            "method": "declared-coherent-bin",
            "frequency_hz": fundamental_hz,
            "integration_half_width_bins": 0,
        },
        "harmonics": {
            "orders": orders,
            "aliasing": "fold-first-nyquist",
            "collision": "reject",
            "out_of_band": "exclude",
            "integration_half_width_bins": 0,
        },
        "metric": {"kind": metric_kind, "unit": "dB"},
        "standards_context": {
            "domain": domain,
            "reference": expected_reference,
            "alignment": expected_alignment,
        },
        "extensions": {},
    }


def _fft(values: Sequence[float]) -> list[complex]:
    """Return an unnormalized radix-2 DFT with deterministic operation order."""

    transformed = [complex(value, 0.0) for value in values]
    count = len(transformed)
    target = 0
    for source in range(1, count):
        bit = count >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if source < target:
            transformed[source], transformed[target] = (
                transformed[target],
                transformed[source],
            )
    width = 2
    while width <= count:
        root = cmath.exp(-2j * math.pi / width)
        half = width // 2
        for start in range(0, count, width):
            factor = 1.0 + 0.0j
            for offset in range(half):
                even = transformed[start + offset]
                odd = transformed[start + offset + half] * factor
                transformed[start + offset] = even + odd
                transformed[start + offset + half] = even - odd
                factor *= root
        width *= 2
    return transformed


def _bin_ranges(indices: Sequence[int]) -> list[dict[str, int]]:
    if not indices:
        return []
    ranges: list[dict[str, int]] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index != previous + 1:
            ranges.append({"start": start, "stop": previous})
            start = index
        previous = index
    ranges.append({"start": start, "stop": previous})
    return ranges


def _fold_frequency(frequency_hz: float, sample_rate_hz: float) -> float:
    wrapped = frequency_hz % sample_rate_hz
    return min(wrapped, sample_rate_hz - wrapped)


def _nearest_bin(
    frequency_hz: float,
    *,
    bin_width_hz: float,
    tolerance_bins: float,
    label: str,
) -> int:
    coordinate = frequency_hz / bin_width_hz
    index = int(math.floor(coordinate + 0.5))
    if abs(coordinate - index) > tolerance_bins:
        raise _InvalidSpectralRequest(
            "spectral.coherence.not_established",
            f"{label} is not on an exact DFT bin within the declared tolerance.",
        )
    return index


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
            "id": (
                f"openada.algorithm/spectral.{kind}/v1alpha1" if kind else METHOD_ID
            ),
            "version": IMPLEMENTATION_VERSION,
        },
        "sample_count": 0,
        "source": source,
        "extensions": {},
    }


def _empty_spectral() -> dict[str, Any]:
    return {
        "status": "unknown",
        "request_sha256": None,
        "method": None,
        "acquisition": None,
        "band": None,
        "fundamental": None,
        "harmonics": [],
        "partition": None,
        "standards_context": None,
        "extensions": {},
    }


def _payload(
    correlation_id: str,
    measurement: dict[str, Any],
    spectral: dict[str, Any],
    *,
    engineering_status: str,
    summary: str,
    execution_status: str = "completed",
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "result.spectral.measure",
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
            "spectral": spectral,
            "extensions": {},
        },
    )


def measure_spectrum(
    series: Mapping[str, object],
    spectral: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Measure one closed single-tone spectral ratio without EDA expressions."""

    try:
        correlation_id = _request_id(request_id)
    except _InvalidSpectralRequest as exc:
        correlation_id = str(uuid.uuid4())
        return _payload(
            correlation_id,
            _measurement_template(
                measurement_id=None, kind=None, signal=None, source=None
            ),
            _empty_spectral(),
            engineering_status="unknown",
            summary="The spectral correlation identity is invalid.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    normalized: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    try:
        normalized = _normalize_series(series)
        axis = normalized["axis"]
        if axis["unit"] != "s":
            raise _InvalidSpectralRequest(
                "spectral.unit.mismatch",
                "The coherent sampled-waveform method requires an axis unit of exactly 's'.",
            )
        request = _normalize_request(spectral, point_count=len(axis["values"]))
        selected = [
            signal
            for signal in normalized["signals"]
            if signal["name"] == request["signal"]
        ]
        if not selected:
            raise _InvalidSpectralRequest(
                "spectral.signal.missing",
                f"Signal {request['signal']!r} is absent from the normalized series.",
            )
        signal = selected[0]
        count = len(axis["values"])
        if count < 2:
            raise _InvalidSpectralRequest(
                "spectral.acquisition.invalid", "At least two time samples are required."
            )
        interval = (axis["values"][-1] - axis["values"][0]) / (count - 1)
        if not math.isfinite(interval) or interval <= 0:
            raise _InvalidSpectralRequest(
                "spectral.acquisition.invalid", "The sampling interval is invalid."
            )
        relative_errors = [
            abs((right - left) - interval) / interval
            for left, right in zip(axis["values"], axis["values"][1:])
        ]
        max_relative_error = max(relative_errors, default=0.0)
        if max_relative_error > request["method"]["uniformity_relative_tolerance"]:
            raise _InvalidSpectralRequest(
                "spectral.sampling.non_uniform",
                "The source axis exceeds the declared uniform-sampling tolerance.",
            )

        sample_rate_hz = 1.0 / interval
        nyquist_hz = sample_rate_hz / 2.0
        bin_width_hz = sample_rate_hz / count
        if request["band"]["upper_hz"] > nyquist_hz:
            raise _InvalidSpectralRequest(
                "spectral.band.invalid",
                "The requested upper band edge exceeds the first Nyquist frequency.",
            )
        tolerance = request["method"]["coherent_bin_tolerance"]
        fundamental_bin = _nearest_bin(
            request["fundamental"]["frequency_hz"],
            bin_width_hz=bin_width_hz,
            tolerance_bins=tolerance,
            label="The declared fundamental",
        )
        if fundamental_bin <= 0 or fundamental_bin >= count // 2:
            raise _InvalidSpectralRequest(
                "spectral.fundamental.invalid",
                "The fundamental must be above DC and below the Nyquist bin.",
            )
        fundamental_frequency_hz = fundamental_bin * bin_width_hz
        if not (
            request["band"]["lower_hz"]
            <= fundamental_frequency_hz
            <= request["band"]["upper_hz"]
        ):
            raise _InvalidSpectralRequest(
                "spectral.fundamental.invalid",
                "The declared fundamental is outside the closed analysis band.",
            )

        mean = math.fsum(signal["values"]) / count
        centered = [value - mean for value in signal["values"]]
        transformed = _fft(centered)
        powers: list[float] = []
        for index in range(count // 2 + 1):
            scale = 1.0 if index in {0, count // 2} else 2.0
            power = scale * (abs(transformed[index]) ** 2) / (count * count)
            if not math.isfinite(power) or power < 0:
                raise _InvalidSpectralRequest(
                    "spectral.value.non_finite",
                    "The deterministic transform produced invalid bin power.",
                )
            powers.append(power)

        band_bins = [
            index
            for index in range(len(powers))
            if request["band"]["lower_hz"]
            <= index * bin_width_hz
            <= request["band"]["upper_hz"]
        ]
        if fundamental_bin not in band_bins:
            raise _InvalidSpectralRequest(
                "spectral.fundamental.invalid",
                "The fundamental bin is absent from the retained band.",
            )

        harmonic_records: list[dict[str, Any]] = []
        occupied = {fundamental_bin}
        harmonic_bins: set[int] = set()
        for order in request["harmonics"]["orders"]:
            source_frequency = order * fundamental_frequency_hz
            folded_frequency = _fold_frequency(source_frequency, sample_rate_hz)
            index = _nearest_bin(
                folded_frequency,
                bin_width_hz=bin_width_hz,
                tolerance_bins=tolerance,
                label=f"Harmonic order {order}",
            )
            if index in occupied or index == 0:
                raise _InvalidSpectralRequest(
                    "spectral.harmonic.collision",
                    f"Harmonic order {order} collides with DC or another declared component.",
                )
            occupied.add(index)
            included = index in band_bins
            if included:
                harmonic_bins.add(index)
            harmonic_records.append(
                {
                    "order": order,
                    "source_frequency_hz": source_frequency,
                    "folded_frequency_hz": folded_frequency,
                    "bin": index,
                    "included": included,
                    "power": powers[index] if included else None,
                }
            )

        dc_bins = {0} & set(band_bins)
        residual_bins = sorted(set(band_bins) - dc_bins - {fundamental_bin})
        noise_bins = sorted(set(residual_bins) - harmonic_bins)
        sfdr_bins = residual_bins
        fundamental_power = powers[fundamental_bin]
        harmonic_power = math.fsum(powers[index] for index in sorted(harmonic_bins))
        residual_power = math.fsum(powers[index] for index in residual_bins)
        noise_power = math.fsum(powers[index] for index in noise_bins)
        winning_spur_bin = (
            min(sfdr_bins, key=lambda index: (-powers[index], index))
            if sfdr_bins
            else None
        )
        winning_spur_power = (
            powers[winning_spur_bin] if winning_spur_bin is not None else 0.0
        )

        metric_kind = request["metric"]["kind"]
        denominator = {
            "snr": noise_power,
            "sinad": residual_power,
            "thd": fundamental_power,
            "sfdr": winning_spur_power,
        }[metric_kind]
        numerator = harmonic_power if metric_kind == "thd" else fundamental_power
        request_sha256 = _canonical_sha256(request)
        partition_definition = {
            "band_bins": band_bins,
            "dc_bins": sorted(dc_bins),
            "fundamental_bins": [fundamental_bin],
            "harmonic_bins": sorted(harmonic_bins),
            "noise_bins": noise_bins,
            "residual_bins": residual_bins,
            "sfdr_candidate_bins": sfdr_bins,
        }
        partition_sha256 = _canonical_sha256(partition_definition)
        partition = {
            "sha256": partition_sha256,
            "band_bin_ranges": _bin_ranges(band_bins),
            "dc_bins": sorted(dc_bins),
            "fundamental_bins": [fundamental_bin],
            "harmonic_bins": sorted(harmonic_bins),
            "noise_bin_ranges": _bin_ranges(noise_bins),
            "noise_bin_count": len(noise_bins),
            "residual_bin_ranges": _bin_ranges(residual_bins),
            "residual_bin_count": len(residual_bins),
            "sfdr_candidate_bin_ranges": _bin_ranges(sfdr_bins),
            "sfdr_candidate_bin_count": len(sfdr_bins),
            "powers": {
                "fundamental": fundamental_power,
                "harmonics": harmonic_power,
                "noise": noise_power,
                "residual": residual_power,
                "winning_spur": winning_spur_power,
            },
            "winning_spur": (
                {
                    "bin": winning_spur_bin,
                    "frequency_hz": winning_spur_bin * bin_width_hz,
                    "power": winning_spur_power,
                }
                if winning_spur_bin is not None
                else None
            ),
        }
        spectral_record = {
            "status": "analyzed",
            "request_sha256": request_sha256,
            "method": request["method"],
            "acquisition": {
                "sample_count": count,
                "sample_rate_hz": sample_rate_hz,
                "nyquist_hz": nyquist_hz,
                "bin_width_hz": bin_width_hz,
                "max_uniformity_relative_error": max_relative_error,
                "missing_samples": "rejected",
                "clipping": "not_assessed",
            },
            "band": request["band"],
            "fundamental": {
                "requested_frequency_hz": request["fundamental"]["frequency_hz"],
                "frequency_hz": fundamental_frequency_hz,
                "bin": fundamental_bin,
                "power": fundamental_power,
            },
            "harmonics": harmonic_records,
            "partition": partition,
            "standards_context": request["standards_context"],
            "extensions": {},
        }
        measurement = _measurement_template(
            measurement_id=request["measurement_id"],
            kind=metric_kind,
            signal=request["signal"],
            source=normalized["source"],
        )
        measurement.update(
            {
                "request_sha256": request_sha256,
                "unit": "dB",
                "location": {
                    "value": (
                        winning_spur_bin * bin_width_hz
                        if metric_kind == "sfdr" and winning_spur_bin is not None
                        else fundamental_frequency_hz
                    ),
                    "unit": "Hz",
                },
                "sample_count": count,
            }
        )
        if fundamental_power <= 0:
            measurement["status"] = "not_found"
            measurement["value"] = None
            spectral_record["status"] = "fundamental_not_found"
            return _payload(
                correlation_id,
                measurement,
                spectral_record,
                engineering_status="fail",
                summary="The valid bounded record contains no power at the declared fundamental bin.",
                diagnostics=[
                    diagnostic(
                        "error",
                        "spectral.fundamental.not_found",
                        "The declared coherent fundamental bin has zero measured power in the selected record.",
                    )
                ],
            )
        if numerator <= 0 or denominator <= 0:
            measurement["status"] = "unknown"
            measurement["value"] = None
            spectral_record["status"] = "metric_unbounded"
            return _payload(
                correlation_id,
                measurement,
                spectral_record,
                engineering_status="unknown",
                summary="The spectral partition is valid, but the requested finite ratio is unbounded.",
                diagnostics=[
                    diagnostic(
                        "warning",
                        "spectral.metric.unbounded",
                        "The selected numerator or denominator power is zero; v1alpha1 does not emit infinity or a guessed numeric floor.",
                    )
                ],
            )
        ratio = 10.0 * math.log10(numerator / denominator)
        if not math.isfinite(ratio):
            raise _InvalidSpectralRequest(
                "spectral.value.non_finite",
                "The requested ratio did not produce a finite value.",
            )
        measurement["status"] = "measured"
        measurement["value"] = ratio
        return _payload(
            correlation_id,
            measurement,
            spectral_record,
            engineering_status="pass",
            summary=f"The declared coherent single-tone {metric_kind.upper()} measurement was derived.",
        )
    except _SeriesInvalidRequest as exc:
        error = _InvalidSpectralRequest("spectral.source.invalid", str(exc))
    except _InvalidSpectralRequest as exc:
        error = exc
    except (OverflowError, ValueError) as exc:
        error = _InvalidSpectralRequest(
            "spectral.value.non_finite", f"The spectral calculation failed safely: {exc}"
        )

    measurement_id = request["measurement_id"] if request is not None else None
    kind = request["metric"]["kind"] if request is not None else None
    signal_name = request["signal"] if request is not None else None
    source = normalized["source"] if normalized is not None else None
    return _payload(
        correlation_id,
        _measurement_template(
            measurement_id=measurement_id,
            kind=kind,
            signal=signal_name,
            source=source,
        ),
        _empty_spectral(),
        engineering_status="unknown",
        summary="The spectral measurement could not be established.",
        execution_status="invalid_request",
        diagnostics=[diagnostic("error", error.code, str(error))],
    )


__all__ = ["SPECTRAL_METRIC_KINDS", "measure_spectrum"]
