#!/usr/bin/env python3
"""Research prototype for event-phase extraction and phase-noise PSD estimates.

This module is deliberately outside ``src/openada``.  It implements a candidate
method for OpenADA#7, not a versioned OpenADA operation.  Invalid evidence is
rejected with ``MethodInvalid`` rather than repaired silently.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import scipy
from scipy.stats import chi2


PROTOTYPE_ID = "research.openada.pn-zero-crossing-welch/0"
PROTOTYPE_VERSION = "0.1.0"
DEFAULT_SEEDS = tuple(range(8))


class MethodInvalid(ValueError):
    """The supplied record cannot support the declared candidate method."""


@dataclass(frozen=True)
class PhaseRecord:
    crossings_s: np.ndarray
    fitted_epoch_s: float
    fitted_period_s: float
    time_error_s: np.ndarray
    phase_rad: np.ndarray

    @property
    def sample_rate_hz(self) -> float:
        return 1.0 / self.fitted_period_s


@dataclass(frozen=True)
class WelchEstimate:
    frequencies_hz: np.ndarray
    psd_per_hz: np.ndarray
    sample_rate_hz: float
    segment_length: int
    hop_length: int
    segment_count: int
    trailing_samples: int
    bin_spacing_hz: float
    enbw_hz: float


def _finite_1d(name: str, values: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise MethodInvalid(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise MethodInvalid(f"{name} contains a non-finite value")
    return array


def _power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def extract_rising_crossings(
    times_s: Sequence[float] | np.ndarray,
    values: Sequence[float] | np.ndarray,
    *,
    threshold: float = 0.0,
    min_slope_per_s: float = 0.0,
    crop_start_s: float | None = None,
    crop_stop_s: float | None = None,
    expected_count: int | None = None,
) -> np.ndarray:
    """Return linearly interpolated ``v < threshold <= v_next`` events.

    Actual native timestamps are authoritative.  The function does not
    resample, fill gaps, merge events, or infer missing cycles.
    """

    times = _finite_1d("times_s", times_s)
    signal = _finite_1d("values", values)
    if len(times) != len(signal):
        raise MethodInvalid("times_s and values have different lengths")
    if len(times) < 2:
        raise MethodInvalid("at least two waveform samples are required")
    if not math.isfinite(threshold):
        raise MethodInvalid("threshold must be finite")
    if not math.isfinite(min_slope_per_s) or min_slope_per_s < 0.0:
        raise MethodInvalid("min_slope_per_s must be finite and non-negative")

    deltas = np.diff(times)
    if np.any(deltas <= 0.0):
        raise MethodInvalid("waveform timestamps are not strictly increasing")

    bracket = (signal[:-1] < threshold) & (signal[1:] >= threshold)
    indices = np.flatnonzero(bracket)
    if len(indices) == 0:
        raise MethodInvalid("no rising threshold crossings were found")

    dv = signal[indices + 1] - signal[indices]
    dt = deltas[indices]
    slopes = dv / dt
    if np.any(dv <= 0.0) or np.any(slopes < min_slope_per_s):
        raise MethodInvalid("a retained crossing has insufficient positive slope")

    fraction = (threshold - signal[indices]) / dv
    crossings = times[indices] + fraction * dt

    if crop_start_s is not None:
        if not math.isfinite(crop_start_s):
            raise MethodInvalid("crop_start_s must be finite")
        crossings = crossings[crossings >= crop_start_s]
    if crop_stop_s is not None:
        if not math.isfinite(crop_stop_s):
            raise MethodInvalid("crop_stop_s must be finite")
        # The frozen prototype crop is half-open at the stop endpoint.
        crossings = crossings[crossings < crop_stop_s]
    if crop_start_s is not None and crop_stop_s is not None:
        if crop_stop_s <= crop_start_s:
            raise MethodInvalid("crop_stop_s must be greater than crop_start_s")

    if len(crossings) < 2:
        raise MethodInvalid("fewer than two crossings remain after the crop")
    if np.any(np.diff(crossings) <= 0.0):
        raise MethodInvalid("interpolated crossings are not strictly increasing")
    if expected_count is not None and len(crossings) != expected_count:
        raise MethodInvalid(
            f"expected {expected_count} crossings, observed {len(crossings)}"
        )
    return crossings


def phase_from_crossings(
    crossings_s: Sequence[float] | np.ndarray,
    *,
    minimum_period_ratio: float | None = None,
    maximum_period_ratio: float | None = None,
) -> PhaseRecord:
    """Fit one global affine ephemeris and return unwrapped event phase."""

    crossings = _finite_1d("crossings_s", crossings_s)
    if len(crossings) < 3:
        raise MethodInvalid("at least three crossings are required")
    intervals = np.diff(crossings)
    if np.any(intervals <= 0.0):
        raise MethodInvalid("crossings are not strictly increasing")

    index = np.arange(len(crossings), dtype=np.float64)
    centered_index = index - np.mean(index)
    centered_time = crossings - np.mean(crossings)
    denominator = float(np.dot(centered_index, centered_index))
    period = float(np.dot(centered_index, centered_time) / denominator)
    epoch = float(np.mean(crossings) - period * np.mean(index))
    if not math.isfinite(period) or period <= 0.0:
        raise MethodInvalid("the fitted carrier period is not positive and finite")

    if minimum_period_ratio is not None:
        if not 0.0 < minimum_period_ratio <= 1.0:
            raise MethodInvalid("minimum_period_ratio must lie in (0, 1]")
        if float(np.min(intervals / period)) < minimum_period_ratio:
            raise MethodInvalid("a crossing interval is below the allowed ratio")
    if maximum_period_ratio is not None:
        if maximum_period_ratio < 1.0 or not math.isfinite(maximum_period_ratio):
            raise MethodInvalid("maximum_period_ratio must be finite and at least 1")
        if float(np.max(intervals / period)) > maximum_period_ratio:
            raise MethodInvalid("a crossing interval is above the allowed ratio")

    residual = crossings - (epoch + period * index)
    phase = -2.0 * math.pi * residual / period
    return PhaseRecord(
        crossings_s=crossings.copy(),
        fitted_epoch_s=epoch,
        fitted_period_s=period,
        time_error_s=residual,
        phase_rad=phase,
    )


def periodic_hann(length: int) -> np.ndarray:
    if length < 2:
        raise MethodInvalid("window length must be at least two")
    index = np.arange(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * math.pi * index / length)


def welch_one_sided_psd(
    samples: Sequence[float] | np.ndarray,
    sample_rate_hz: float,
    segment_length: int,
) -> WelchEstimate:
    """Periodic-Hann, 50%-overlap, arithmetic-mean-detrended Welch PSD."""

    values = _finite_1d("samples", samples)
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise MethodInvalid("sample_rate_hz must be positive and finite")
    if not isinstance(segment_length, int):
        raise MethodInvalid("segment_length must be an integer")
    if segment_length < 8 or not _power_of_two(segment_length):
        raise MethodInvalid("segment_length must be a power of two of at least 8")
    if segment_length % 2:
        raise MethodInvalid("segment_length must be even")
    if len(values) < segment_length:
        raise MethodInvalid("record is shorter than one complete Welch segment")

    hop = segment_length // 2
    segment_count = 1 + (len(values) - segment_length) // hop
    used = segment_length + (segment_count - 1) * hop
    trailing = len(values) - used
    window = periodic_hann(segment_length)
    window_power = float(np.dot(window, window))
    accumulator = np.zeros(segment_length // 2 + 1, dtype=np.float64)

    for segment_index in range(segment_count):
        start = segment_index * hop
        segment = values[start : start + segment_length].copy()
        # This is the arithmetic mean of the unwindowed segment, matching
        # scipy.signal.welch(detrend="constant"), not a Hann-weighted mean.
        segment -= float(np.mean(segment))
        transform = np.fft.rfft(segment * window)
        periodogram = np.abs(transform) ** 2 / (sample_rate_hz * window_power)
        if segment_length % 2 == 0:
            periodogram[1:-1] *= 2.0
        else:  # Defensive; odd lengths are rejected above.
            periodogram[1:] *= 2.0
        accumulator += periodogram

    psd = accumulator / segment_count
    frequencies = np.fft.rfftfreq(segment_length, d=1.0 / sample_rate_hz)
    bin_spacing = sample_rate_hz / segment_length
    enbw = sample_rate_hz * window_power / float(np.sum(window) ** 2)
    return WelchEstimate(
        frequencies_hz=frequencies,
        psd_per_hz=psd,
        sample_rate_hz=sample_rate_hz,
        segment_length=segment_length,
        hop_length=hop,
        segment_count=segment_count,
        trailing_samples=trailing,
        bin_spacing_hz=bin_spacing,
        enbw_hz=enbw,
    )


def effective_welch_dof(segment_count: int, independent_records: int = 1) -> float:
    """Locally-white Gaussian DOF approximation for 50%-overlap periodic Hann."""

    if segment_count < 1 or independent_records < 1:
        raise MethodInvalid("segment and independent-record counts must be positive")
    per_record = 36.0 * segment_count**2 / (19.0 * segment_count - 1.0)
    return independent_records * per_record


def chi_square_intervals_db(
    degrees_of_freedom: float, confidence: float = 0.95
) -> dict[str, list[float]]:
    """Return both estimate/truth sampling bounds and true/estimate CI bounds."""

    if not math.isfinite(degrees_of_freedom) or degrees_of_freedom <= 0.0:
        raise MethodInvalid("degrees_of_freedom must be positive and finite")
    if not 0.0 < confidence < 1.0:
        raise MethodInvalid("confidence must lie in (0, 1)")
    alpha = 1.0 - confidence
    low_quantile = float(chi2.ppf(alpha / 2.0, degrees_of_freedom))
    high_quantile = float(chi2.ppf(1.0 - alpha / 2.0, degrees_of_freedom))
    estimate_over_truth = [
        10.0 * math.log10(low_quantile / degrees_of_freedom),
        10.0 * math.log10(high_quantile / degrees_of_freedom),
    ]
    true_over_estimate = [
        10.0 * math.log10(degrees_of_freedom / high_quantile),
        10.0 * math.log10(degrees_of_freedom / low_quantile),
    ]
    return {
        "estimate_over_true_db": estimate_over_truth,
        "true_over_estimate_db": true_over_estimate,
    }


def nearest_offset_bin(
    requested_hz: float,
    bin_spacing_hz: float,
    maximum_bin: int,
    *,
    minimum_bin: int = 1,
) -> int:
    """Choose the nearest admissible bin; an exact tie selects the lower bin."""

    if not math.isfinite(requested_hz) or requested_hz <= 0.0:
        raise MethodInvalid("requested offset must be positive and finite")
    if not math.isfinite(bin_spacing_hz) or bin_spacing_hz <= 0.0:
        raise MethodInvalid("bin spacing must be positive and finite")
    if minimum_bin < 1 or maximum_bin < minimum_bin:
        raise MethodInvalid("the admissible bin range is empty")
    position = requested_hz / bin_spacing_hz
    lower = math.floor(position)
    upper = math.ceil(position)
    candidates = [
        item for item in (lower, upper) if minimum_bin <= item <= maximum_bin
    ]
    if not candidates:
        raise MethodInvalid("requested offset lies outside the admissible bin range")
    return min(candidates, key=lambda item: (abs(position - item), item))


def phase_noise_db(psd_phase_rad2_per_hz: np.ndarray | float) -> np.ndarray:
    psd = np.asarray(psd_phase_rad2_per_hz, dtype=np.float64)
    if np.any(~np.isfinite(psd)) or np.any(psd <= 0.0):
        raise MethodInvalid("phase PSD must be positive and finite before dB conversion")
    return 10.0 * np.log10(psd / 2.0)


def jitter_metrics(record: PhaseRecord) -> dict[str, float]:
    period_error = np.diff(record.time_error_s)
    cycle_error = np.diff(period_error)
    return {
        "time_error_rms_s": float(np.sqrt(np.mean(record.time_error_s**2))),
        "period_jitter_rms_s": float(np.sqrt(np.mean(period_error**2))),
        "cycle_to_cycle_jitter_rms_s": float(np.sqrt(np.mean(cycle_error**2))),
    }


def overlapping_allan_variance(
    time_error_s: Sequence[float] | np.ndarray,
    nominal_period_s: float,
    m_values: Iterable[int],
) -> list[dict[str, float | int]]:
    """Overlapping Allan variance from equally indexed time-error events."""

    errors = _finite_1d("time_error_s", time_error_s)
    if not math.isfinite(nominal_period_s) or nominal_period_s <= 0.0:
        raise MethodInvalid("nominal_period_s must be positive and finite")
    rows: list[dict[str, float | int]] = []
    for raw_m in m_values:
        if not isinstance(raw_m, (int, np.integer)) or int(raw_m) < 1:
            raise MethodInvalid("every Allan m value must be a positive integer")
        m = int(raw_m)
        count = len(errors) - 2 * m
        if count < 1:
            raise MethodInvalid(f"record is too short for Allan m={m}")
        second = errors[2 * m :] - 2.0 * errors[m:-m] + errors[: -2 * m]
        tau = m * nominal_period_s
        variance = float(np.sum(second**2) / (2.0 * tau**2 * count))
        rows.append(
            {
                "m": m,
                "tau_s": tau,
                "term_count": count,
                "allan_variance": variance,
                "allan_deviation": math.sqrt(variance),
            }
        )
    return rows


def period_psd_to_phase_noise(
    period_error_psd_s2_per_hz: np.ndarray,
    frequencies_hz: np.ndarray,
    carrier_hz: float,
    event_sample_rate_hz: float,
) -> np.ndarray:
    """Convert period-error PSD to L(f); DC is returned as NaN."""

    psd = _finite_1d("period_error_psd_s2_per_hz", period_error_psd_s2_per_hz)
    frequencies = _finite_1d("frequencies_hz", frequencies_hz)
    if len(psd) != len(frequencies):
        raise MethodInvalid("period PSD and frequency axes have different lengths")
    if carrier_hz <= 0.0 or event_sample_rate_hz <= 0.0:
        raise MethodInvalid("carrier and event sample rates must be positive")
    denominator = 2.0 * np.sin(math.pi * frequencies / event_sample_rate_hz) ** 2
    result = np.full_like(psd, np.nan)
    nonzero = denominator > 0.0
    result[nonzero] = math.pi**2 * carrier_hz**2 * psd[nonzero] / denominator[nonzero]
    return result


def _affine_detrend(values: np.ndarray) -> np.ndarray:
    index = np.arange(len(values), dtype=np.float64)
    centered = index - np.mean(index)
    slope = float(np.dot(centered, values - np.mean(values)) / np.dot(centered, centered))
    intercept = float(np.mean(values) - slope * np.mean(index))
    return values - (intercept + slope * index)


def synthetic_white_phase(
    event_count: int, carrier_hz: float, l_db_per_hz: float, seed: int
) -> tuple[np.ndarray, dict[str, float]]:
    if event_count < 3 or carrier_hz <= 0.0:
        raise MethodInvalid("invalid synthetic white-phase shape")
    l_linear = 10.0 ** (l_db_per_hz / 10.0)
    sigma_phase = math.sqrt(l_linear * carrier_hz)
    rng = np.random.Generator(np.random.PCG64(seed))
    phase = _affine_detrend(rng.normal(0.0, sigma_phase, event_count))
    return phase, {
        "target_l_db_per_hz": l_db_per_hz,
        "phase_sigma_before_detrend_rad": sigma_phase,
        "one_sided_s_phi_rad2_per_hz": 2.0 * sigma_phase**2 / carrier_hz,
    }


def synthetic_wiener_phase(
    event_count: int,
    carrier_hz: float,
    reference_offset_hz: float,
    reference_l_db_per_hz: float,
    seed: int,
) -> tuple[np.ndarray, dict[str, float]]:
    if event_count < 3 or carrier_hz <= 0.0:
        raise MethodInvalid("invalid synthetic Wiener-phase shape")
    if not 0.0 < reference_offset_hz < carrier_hz / 2.0:
        raise MethodInvalid("Wiener reference offset is outside event Nyquist")
    l_reference = 10.0 ** (reference_l_db_per_hz / 10.0)
    q = (
        4.0
        * carrier_hz
        * math.sin(math.pi * reference_offset_hz / carrier_hz) ** 2
        * l_reference
    )
    rng = np.random.Generator(np.random.PCG64(seed))
    increments = rng.normal(0.0, math.sqrt(q), event_count - 1)
    phase = np.concatenate(([0.0], np.cumsum(increments)))
    phase = _affine_detrend(phase)
    return phase, {
        "reference_offset_hz": reference_offset_hz,
        "reference_l_db_per_hz": reference_l_db_per_hz,
        "increment_variance_rad2": q,
        "increment_sigma_rad": math.sqrt(q),
    }


def event_times_from_phase(phase_rad: np.ndarray, carrier_hz: float) -> np.ndarray:
    phase = _finite_1d("phase_rad", phase_rad)
    index = np.arange(len(phase), dtype=np.float64)
    events = index / carrier_hz - phase / (2.0 * math.pi * carrier_hz)
    if np.any(np.diff(events) <= 0.0):
        raise MethodInvalid("synthetic phase produces nonmonotone event times")
    # Shift away from t=0 so the waveform has a real guard interval.
    return events + 2.0 / carrier_hz


def synthesize_c1_event_waveform(
    retained_events_s: np.ndarray,
    carrier_hz: float,
    samples_per_cycle: int,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Create a C1 sine waveform whose retained rising events are exact.

    One nominal guard event is added on each side.  Within an event interval,
    a cubic cycle coordinate hits both events exactly and has nominal carrier
    slope at both endpoints.  This avoids constructing a slope cusp at the
    crossing being used to test interpolation.
    """

    retained = _finite_1d("retained_events_s", retained_events_s)
    if len(retained) < 3 or np.any(np.diff(retained) <= 0.0):
        raise MethodInvalid("synthetic retained events must increase")
    if carrier_hz <= 0.0 or samples_per_cycle < 8:
        raise MethodInvalid("invalid synthetic waveform sample policy")

    period = 1.0 / carrier_hz
    events = np.concatenate(
        ([retained[0] - period], retained, [retained[-1] + period])
    )
    intervals = np.diff(events)
    m = carrier_hz * intervals
    if np.any(m <= 0.0) or np.any(m >= 3.0):
        raise MethodInvalid("C1 synthetic event map is not monotone")

    dt = period / samples_per_cycle
    count = int(math.floor((events[-1] - events[0]) / dt)) + 1
    times = events[0] + np.arange(count, dtype=np.float64) * dt
    if times[-1] < events[-1]:
        times = np.append(times, events[-1])
    else:
        times[-1] = min(times[-1], events[-1])

    interval_index = np.searchsorted(events, times, side="right") - 1
    interval_index = np.clip(interval_index, 0, len(events) - 2)
    left = events[interval_index]
    local_interval = intervals[interval_index]
    u = (times - left) / local_interval
    local_m = carrier_hz * local_interval
    cycle_coordinate = (
        interval_index.astype(np.float64)
        + local_m * u
        + (1.0 - local_m) * (3.0 * u**2 - 2.0 * u**3)
    )
    values = np.sin(2.0 * math.pi * cycle_coordinate)
    crop_start = 0.5 * (events[0] + retained[0])
    crop_stop = 0.5 * (retained[-1] + events[-1])
    return times, values, crop_start, crop_stop


def _truth_white_s_phi(sigma_phase_rad: float, sample_rate_hz: float, length: int) -> np.ndarray:
    return np.full(length, 2.0 * sigma_phase_rad**2 / sample_rate_hz)


def _truth_wiener_s_phi(
    q_rad2: float, sample_rate_hz: float, segment_length: int
) -> np.ndarray:
    bins = np.arange(segment_length // 2 + 1, dtype=np.float64)
    truth = np.full_like(bins, np.nan)
    nonzero = bins > 0.0
    truth[nonzero] = q_rad2 / (
        2.0
        * sample_rate_hz
        * np.sin(math.pi * bins[nonzero] / segment_length) ** 2
    )
    return truth


def _band_metrics(
    estimate: np.ndarray,
    truth: np.ndarray,
    frequencies: np.ndarray,
    start_bin: int,
    stop_bin_inclusive: int,
    sampling_bounds_db: tuple[float, float],
) -> dict[str, float | int]:
    indices = np.arange(start_bin, stop_bin_inclusive + 1)
    error_db = 10.0 * np.log10(estimate[indices] / truth[indices])
    exponent = float(
        np.polyfit(
            np.log10(frequencies[indices]),
            np.log10(estimate[indices] / 2.0),
            1,
        )[0]
    )
    inside = (error_db >= sampling_bounds_db[0]) & (
        error_db <= sampling_bounds_db[1]
    )
    return {
        "first_bin": start_bin,
        "last_bin": stop_bin_inclusive,
        "bin_count": len(indices),
        "median_error_db": float(np.median(error_db)),
        "linear_mean_error_db": float(
            10.0 * math.log10(float(np.mean(estimate[indices] / truth[indices])))
        ),
        "minimum_error_db": float(np.min(error_db)),
        "maximum_error_db": float(np.max(error_db)),
        "fitted_power_law_exponent": exponent,
        "fitted_db_per_decade": 10.0 * exponent,
        "pointwise_interval_coverage_fraction": float(np.mean(inside)),
    }


def _closure_process(
    kind: str,
    *,
    event_count: int,
    carrier_hz: float,
    segment_length: int,
    samples_per_cycle: int,
    seeds: Sequence[int],
    named_bin: int,
) -> dict[str, Any]:
    extracted_psds: list[np.ndarray] = []
    oracle_psds: list[np.ndarray] = []
    truth_psds: list[np.ndarray] = []
    recovered_rates: list[float] = []
    phase_rms_errors: list[float] = []
    phase_max_errors: list[float] = []
    psd_max_deltas_db: list[float] = []
    allan_m1: list[float] = []
    period_jitter: list[float] = []
    reconstructed_l: list[np.ndarray] = []
    process_metadata: dict[str, float] | None = None
    first_welch: WelchEstimate | None = None

    reference_offset = named_bin * carrier_hz / segment_length
    for seed in seeds:
        if kind == "white_pm":
            phase, metadata = synthetic_white_phase(
                event_count, carrier_hz, -60.0, seed
            )
        elif kind == "wiener_phase":
            phase, metadata = synthetic_wiener_phase(
                event_count, carrier_hz, reference_offset, -80.0, seed
            )
        else:
            raise AssertionError(kind)
        process_metadata = metadata

        exact_events = event_times_from_phase(phase, carrier_hz)
        oracle_record = phase_from_crossings(exact_events)
        times, values, crop_start, crop_stop = synthesize_c1_event_waveform(
            exact_events, carrier_hz, samples_per_cycle
        )
        extracted_events = extract_rising_crossings(
            times,
            values,
            threshold=0.0,
            crop_start_s=crop_start,
            crop_stop_s=crop_stop,
            expected_count=event_count,
        )
        extracted_record = phase_from_crossings(extracted_events)
        recovered_rates.append(extracted_record.sample_rate_hz)

        phase_difference = extracted_record.phase_rad - oracle_record.phase_rad
        phase_rms_errors.append(float(np.sqrt(np.mean(phase_difference**2))))
        phase_max_errors.append(float(np.max(np.abs(phase_difference))))

        extracted_welch = welch_one_sided_psd(
            extracted_record.phase_rad,
            extracted_record.sample_rate_hz,
            segment_length,
        )
        oracle_welch = welch_one_sided_psd(
            oracle_record.phase_rad, oracle_record.sample_rate_hz, segment_length
        )
        first_welch = extracted_welch
        extracted_psds.append(extracted_welch.psd_per_hz)
        oracle_psds.append(oracle_welch.psd_per_hz)
        scored = slice(8 if kind == "wiener_phase" else 16, 411)
        delta = 10.0 * np.log10(
            extracted_welch.psd_per_hz[scored]
            / oracle_welch.psd_per_hz[scored]
        )
        psd_max_deltas_db.append(float(np.max(np.abs(delta))))

        if kind == "white_pm":
            truth = _truth_white_s_phi(
                metadata["phase_sigma_before_detrend_rad"],
                extracted_record.sample_rate_hz,
                len(extracted_welch.psd_per_hz),
            )
        else:
            truth = _truth_wiener_s_phi(
                metadata["increment_variance_rad2"],
                extracted_record.sample_rate_hz,
                segment_length,
            )
            allan_m1.append(
                float(
                    overlapping_allan_variance(
                        extracted_record.time_error_s,
                        extracted_record.fitted_period_s,
                        [1],
                    )[0]["allan_variance"]
                )
            )
            period_jitter.append(jitter_metrics(extracted_record)["period_jitter_rms_s"])
            period_error = np.diff(extracted_record.time_error_s)
            period_welch = welch_one_sided_psd(
                period_error, extracted_record.sample_rate_hz, segment_length
            )
            reconstructed_l.append(
                period_psd_to_phase_noise(
                    period_welch.psd_per_hz,
                    period_welch.frequencies_hz,
                    extracted_record.sample_rate_hz,
                    extracted_record.sample_rate_hz,
                )
            )
        truth_psds.append(truth)

    assert first_welch is not None and process_metadata is not None
    estimate = np.mean(np.stack(extracted_psds), axis=0)
    oracle = np.mean(np.stack(oracle_psds), axis=0)
    truth_stack = np.stack(truth_psds)
    if kind == "wiener_phase":
        truth = np.empty(truth_stack.shape[1], dtype=np.float64)
        truth[0] = np.nan
        truth[1:] = np.mean(truth_stack[:, 1:], axis=0)
    else:
        truth = np.mean(truth_stack, axis=0)
    frequencies = first_welch.frequencies_hz
    dof = effective_welch_dof(first_welch.segment_count, len(seeds))
    intervals = chi_square_intervals_db(dof)
    sampling_bounds = tuple(intervals["estimate_over_true_db"])
    start_bin = 16 if kind == "white_pm" else 8
    metrics = _band_metrics(
        estimate, truth, frequencies, start_bin, 410, sampling_bounds
    )
    named_error = float(
        10.0 * math.log10(estimate[named_bin] / truth[named_bin])
    )
    metrics.update(
        {
            "named_bin": named_bin,
            "named_offset_hz": float(frequencies[named_bin]),
            "named_estimate_db_per_hz": float(phase_noise_db(estimate[named_bin])),
            "named_truth_db_per_hz": float(phase_noise_db(truth[named_bin])),
            "named_error_db": named_error,
        }
    )

    if kind == "white_pm":
        passes = (
            abs(float(metrics["median_error_db"])) <= 0.5
            and abs(float(metrics["fitted_power_law_exponent"])) <= 0.1
            and float(metrics["pointwise_interval_coverage_fraction"]) >= 0.90
            and abs(named_error) <= 1.5
            and max(phase_rms_errors) <= 1.0e-4
            and max(psd_max_deltas_db) <= 0.05
        )
    else:
        exponent = float(metrics["fitted_power_law_exponent"])
        passes = (
            abs(float(metrics["median_error_db"])) <= 0.5
            and -2.1 <= exponent <= -1.9
            and float(metrics["pointwise_interval_coverage_fraction"]) >= 0.90
            and abs(named_error) <= 1.5
            and max(phase_rms_errors) <= 1.0e-4
            and max(psd_max_deltas_db) <= 0.05
        )

    payload: dict[str, Any] = {
        "kind": kind,
        "status": "pass" if passes else "fail",
        "construction": process_metadata,
        "event_count_per_seed": event_count,
        "seed_count": len(seeds),
        "samples_per_cycle": samples_per_cycle,
        "recovered_event_sample_rate_hz": {
            "minimum": min(recovered_rates),
            "maximum": max(recovered_rates),
            "mean": float(np.mean(recovered_rates)),
        },
        "welch": {
            "segment_length": segment_length,
            "hop_length": first_welch.hop_length,
            "segment_count_per_seed": first_welch.segment_count,
            "bin_spacing_hz": first_welch.bin_spacing_hz,
            "enbw_hz": first_welch.enbw_hz,
            "window": "periodic-hann",
            "detrend": "unwindowed-arithmetic-mean-per-segment",
            "effective_degrees_of_freedom": dof,
            "confidence": 0.95,
            "chi_square_local_white_approximation": intervals,
        },
        "crossing_closure": {
            "maximum_phase_rms_error_rad": max(phase_rms_errors),
            "maximum_phase_absolute_error_rad": max(phase_max_errors),
            "maximum_scored_band_psd_delta_db": max(psd_max_deltas_db),
        },
        "spectral_closure": metrics,
        "acceptance": {
            "band_median_absolute_error_db_max": 0.5,
            "pointwise_interval_coverage_min": 0.90,
            "named_bin_absolute_error_db_max_regression_not_ci": 1.5,
            "crossing_phase_rms_error_rad_max": 1.0e-4,
            "extracted_oracle_psd_delta_db_max": 0.05,
            "power_law_exponent_range": (
                [-0.1, 0.1] if kind == "white_pm" else [-2.1, -1.9]
            ),
        },
    }
    if kind == "wiener_phase":
        q = process_metadata["increment_variance_rad2"]
        expected_period_jitter = math.sqrt(q) / (2.0 * math.pi * carrier_hz)
        expected_allan = q / (4.0 * math.pi**2)
        reconstructed_stack = np.stack(reconstructed_l)
        reconstructed = np.empty(reconstructed_stack.shape[1], dtype=np.float64)
        reconstructed[0] = np.nan
        reconstructed[1:] = np.mean(reconstructed_stack[:, 1:], axis=0)
        reconstruction_error = 10.0 * np.log10(
            reconstructed[8:411] / (truth[8:411] / 2.0)
        )
        period_error_percent = 100.0 * (
            float(np.mean(period_jitter)) / expected_period_jitter - 1.0
        )
        allan_error_percent = 100.0 * (
            float(np.mean(allan_m1)) / expected_allan - 1.0
        )
        reconstruction_median = float(np.median(reconstruction_error))
        correlation_pass = (
            abs(period_error_percent) <= 2.0
            and abs(allan_error_percent) <= 2.0
            and abs(reconstruction_median) <= 0.5
        )
        payload["time_domain_correlation"] = {
            "status": "pass" if correlation_pass else "fail",
            "period_jitter_rms_s_mean": float(np.mean(period_jitter)),
            "period_jitter_rms_s_theory": expected_period_jitter,
            "period_jitter_error_percent": period_error_percent,
            "allan_variance_tau_one_period_mean": float(np.mean(allan_m1)),
            "allan_variance_tau_one_period_theory": expected_allan,
            "allan_variance_error_percent": allan_error_percent,
            "period_psd_reconstructed_l_median_error_db": reconstruction_median,
            "period_psd_reconstructed_l_max_abs_error_db": float(
                np.max(np.abs(reconstruction_error))
            ),
            "acceptance": {
                "period_jitter_absolute_error_percent_max": 2.0,
                "allan_variance_absolute_error_percent_max": 2.0,
                "period_psd_reconstruction_median_absolute_error_db_max": 0.5,
            },
            "note": (
                "Period-PSD inversion retains spectral information; scalar RMS "
                "period jitter and Allan variance do not identify point L(f)."
            ),
        }
        if not correlation_pass:
            payload["status"] = "fail"
    return payload


def _sampling_convergence(
    *,
    event_count: int,
    carrier_hz: float,
    segment_length: int,
    named_bin: int,
) -> dict[str, Any]:
    rows: list[dict[str, float | int]] = []
    reference_offset = named_bin * carrier_hz / segment_length
    phase, _ = synthetic_wiener_phase(
        event_count, carrier_hz, reference_offset, -80.0, seed=0
    )
    exact_events = event_times_from_phase(phase, carrier_hz)
    oracle = phase_from_crossings(exact_events)
    oracle_psd = welch_one_sided_psd(
        oracle.phase_rad, oracle.sample_rate_hz, segment_length
    ).psd_per_hz
    extracted_by_ppc: dict[int, np.ndarray] = {}
    for ppc in (32, 64, 128):
        times, values, crop_start, crop_stop = synthesize_c1_event_waveform(
            exact_events, carrier_hz, ppc
        )
        events = extract_rising_crossings(
            times,
            values,
            crop_start_s=crop_start,
            crop_stop_s=crop_stop,
            expected_count=event_count,
        )
        record = phase_from_crossings(events)
        psd = welch_one_sided_psd(
            record.phase_rad, record.sample_rate_hz, segment_length
        ).psd_per_hz
        extracted_by_ppc[ppc] = psd
        delta = 10.0 * np.log10(psd[8:411] / oracle_psd[8:411])
        rows.append(
            {
                "samples_per_cycle": ppc,
                "phase_rms_error_rad": float(
                    np.sqrt(np.mean((record.phase_rad - oracle.phase_rad) ** 2))
                ),
                "scored_band_median_psd_delta_db": float(np.median(delta)),
                "scored_band_max_abs_psd_delta_db": float(np.max(np.abs(delta))),
            }
        )
    delta_32_64 = 10.0 * np.log10(
        extracted_by_ppc[32][8:411] / extracted_by_ppc[64][8:411]
    )
    passed = float(np.max(np.abs(delta_32_64))) <= 0.05
    return {
        "status": "pass" if passed else "fail",
        "process": "wiener_phase_seed_0",
        "rows": rows,
        "max_abs_psd_delta_32_to_64_db": float(np.max(np.abs(delta_32_64))),
        "acceptance_max_abs_psd_delta_32_to_64_db": 0.05,
    }


def run_synthetic_closure(
    *,
    event_count: int = 32768,
    carrier_hz: float = 10_000.0,
    segment_length: int = 4096,
    samples_per_cycle: int = 64,
    seeds: Sequence[int] = DEFAULT_SEEDS,
) -> dict[str, Any]:
    """Run deterministic white-PM and Wiener-phase construction closure."""

    if not _power_of_two(segment_length) or segment_length < 1024:
        raise MethodInvalid(
            "synthetic segment_length must be a power of two of at least 1024"
        )
    if event_count < segment_length:
        raise MethodInvalid("synthetic event count is shorter than a segment")
    if not math.isfinite(carrier_hz) or carrier_hz <= 0.0:
        raise MethodInvalid("synthetic carrier_hz must be positive and finite")
    if samples_per_cycle < 8:
        raise MethodInvalid("synthetic samples_per_cycle must be at least 8")
    if not seeds:
        raise MethodInvalid("at least one synthetic seed is required")
    if any(not isinstance(seed, (int, np.integer)) or int(seed) < 0 for seed in seeds):
        raise MethodInvalid("synthetic seeds must be non-negative integers")
    if len(set(int(seed) for seed in seeds)) != len(seeds):
        raise MethodInvalid("synthetic seeds must be distinct independent records")
    named_bin = 40
    started = time.perf_counter()
    white = _closure_process(
        "white_pm",
        event_count=event_count,
        carrier_hz=carrier_hz,
        segment_length=segment_length,
        samples_per_cycle=samples_per_cycle,
        seeds=seeds,
        named_bin=named_bin,
    )
    wiener = _closure_process(
        "wiener_phase",
        event_count=event_count,
        carrier_hz=carrier_hz,
        segment_length=segment_length,
        samples_per_cycle=samples_per_cycle,
        seeds=seeds,
        named_bin=named_bin,
    )
    convergence = _sampling_convergence(
        event_count=event_count,
        carrier_hz=carrier_hz,
        segment_length=segment_length,
        named_bin=named_bin,
    )
    elapsed = time.perf_counter() - started
    passed = all(item["status"] == "pass" for item in (white, wiener, convergence))
    script_path = Path(__file__).resolve()
    return {
        "prototype": PROTOTYPE_ID,
        "prototype_version": PROTOTYPE_VERSION,
        "claim": "synthetic method closure only; no physical oscillator PN or signoff",
        "status": "pass" if passed else "fail",
        "configuration": {
            "event_count_per_seed": event_count,
            "carrier_hz": carrier_hz,
            "waveform_sample_rate_hz": carrier_hz * samples_per_cycle,
            "samples_per_cycle": samples_per_cycle,
            "segment_length_events": segment_length,
            "seeds": list(seeds),
            "named_bin": named_bin,
            "named_offset_hz": named_bin * carrier_hz / segment_length,
            "prng": "numpy.random.PCG64",
        },
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "script_path": str(script_path),
            "script_sha256": _sha256(script_path),
        },
        "white_pm": white,
        "wiener_phase": wiener,
        "sampling_convergence": convergence,
        "analysis_wallclock_s": elapsed,
        "limitations": [
            "The chi-square interval is a locally-white Gaussian approximation.",
            "Pointwise coverage is a diagnostic, not a simultaneous confidence band.",
            "Wiener phase is nonstationary; global OLS and window leakage bias the lowest bins.",
            "The C1 waveform is a synthetic event-timing construct, not a device-noise model.",
        ],
    }


def _read_csv(path: Path, time_column: str, value_column: str) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[float] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise MethodInvalid("CSV has no header")
        if time_column not in reader.fieldnames or value_column not in reader.fieldnames:
            raise MethodInvalid(
                f"CSV must contain columns {time_column!r} and {value_column!r}"
            )
        for row_index, row in enumerate(reader, start=2):
            try:
                times.append(float(row[time_column]))
                values.append(float(row[value_column]))
            except (TypeError, ValueError) as exc:
                raise MethodInvalid(f"invalid numeric CSV row {row_index}") from exc
    return np.asarray(times), np.asarray(values)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_csv(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input).resolve()
    times, values = _read_csv(path, args.time_column, args.value_column)
    crossings = extract_rising_crossings(
        times,
        values,
        threshold=args.threshold,
        min_slope_per_s=args.min_slope,
        crop_start_s=args.crop_start,
        crop_stop_s=args.crop_stop,
        expected_count=args.expected_crossings,
    )
    record = phase_from_crossings(
        crossings,
        minimum_period_ratio=args.minimum_period_ratio,
        maximum_period_ratio=args.maximum_period_ratio,
    )
    estimate = welch_one_sided_psd(
        record.phase_rad, record.sample_rate_hz, args.segment_length
    )
    dof = effective_welch_dof(estimate.segment_count)
    intervals = chi_square_intervals_db(dof)
    offsets = []
    # One-event-per-cycle sampling does not admit a scored point at Nyquist.
    maximum_bin = len(estimate.frequencies_hz) - 2
    if (
        not math.isfinite(args.maximum_bin_mismatch)
        or args.maximum_bin_mismatch < 0.0
    ):
        raise MethodInvalid("maximum_bin_mismatch must be finite and non-negative")
    for requested in args.offset:
        bin_index = nearest_offset_bin(
            requested,
            estimate.bin_spacing_hz,
            maximum_bin,
            minimum_bin=args.minimum_bin,
        )
        actual = float(estimate.frequencies_hz[bin_index])
        mismatch = abs(actual - requested) / requested
        if mismatch > args.maximum_bin_mismatch:
            raise MethodInvalid(
                f"offset {requested:g} Hz maps to {actual:g} Hz, mismatch {mismatch:.3%}"
            )
        value_db = float(phase_noise_db(estimate.psd_per_hz[bin_index]))
        offsets.append(
            {
                "requested_hz": requested,
                "actual_hz": actual,
                "relative_mismatch": mismatch,
                "bin": bin_index,
                "s_phi_rad2_per_hz": float(estimate.psd_per_hz[bin_index]),
                "l_db_per_hz": value_db,
                "approximate_95_percent_true_l_interval_db_per_hz": [
                    value_db + intervals["true_over_estimate_db"][0],
                    value_db + intervals["true_over_estimate_db"][1],
                ],
            }
        )

    allan_m = args.allan_m or [1, 2, 4, 8]
    allan = overlapping_allan_variance(
        record.time_error_s, record.fitted_period_s, allan_m
    )
    valid_density = estimate.psd_per_hz[args.minimum_bin:-1]
    if len(valid_density) == 0:
        raise MethodInvalid("minimum_bin leaves no density below event Nyquist")
    integrated_phase = math.sqrt(
        float(np.sum(valid_density) * estimate.bin_spacing_hz)
    )
    return {
        "prototype": PROTOTYPE_ID,
        "prototype_version": PROTOTYPE_VERSION,
        "status": "pass",
        "claim": "research estimate; no OpenADA operation, physical-model validation, or signoff",
        "source": {
            "path": str(path),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "time_column": args.time_column,
            "value_column": args.value_column,
            "value_unit": args.value_unit,
            "signal_mode": args.signal_mode,
            "signal_expression": args.signal_expression,
            "sample_count": len(times),
            "time_start_s": float(times[0]),
            "time_stop_s": float(times[-1]),
        },
        "crossing_method": {
            "polarity": "rising",
            "threshold": args.threshold,
            "interpolation": "adjacent-sample-linear",
            "crop_start_s_inclusive": args.crop_start,
            "crop_stop_s_exclusive": args.crop_stop,
            "crossing_count": len(crossings),
            "expected_crossing_count": args.expected_crossings,
            "minimum_slope_per_s": args.min_slope,
            "minimum_period_ratio_limit": args.minimum_period_ratio,
            "maximum_period_ratio_limit": args.maximum_period_ratio,
            "observed_minimum_period_ratio": float(
                np.min(np.diff(crossings) / record.fitted_period_s)
            ),
            "observed_maximum_period_ratio": float(
                np.max(np.diff(crossings) / record.fitted_period_s)
            ),
        },
        "phase_method": {
            "detrend": "one-global-affine-crossing-ephemeris",
            "fitted_epoch_s": record.fitted_epoch_s,
            "fitted_period_s": record.fitted_period_s,
            "fitted_carrier_hz": record.sample_rate_hz,
            "unwrapped": True,
        },
        "welch_method": {
            "window": "periodic-hann",
            "segment_detrend": "unwindowed-arithmetic-mean",
            "segment_length_events": estimate.segment_length,
            "hop_length_events": estimate.hop_length,
            "segment_count": estimate.segment_count,
            "trailing_events_ignored": estimate.trailing_samples,
            "bin_spacing_hz": estimate.bin_spacing_hz,
            "enbw_hz": estimate.enbw_hz,
            "effective_degrees_of_freedom": dof,
            "confidence_model": "local-white-Gaussian-Hann-overlap-approximation",
            "confidence_intervals_db": intervals,
            "minimum_usable_bin": args.minimum_bin,
            "maximum_bin_mismatch": args.maximum_bin_mismatch,
        },
        "offsets": offsets,
        "jitter": jitter_metrics(record),
        "overlapping_allan": allan,
        "integrated_phase_rad_rms": {
            "value": integrated_phase,
            "lower_offset_hz": float(estimate.frequencies_hz[args.minimum_bin]),
            "upper_offset_hz_inclusive": float(estimate.frequencies_hz[-2]),
            "event_nyquist_excluded": True,
        },
        "small_phase_rf_power_ratio_interpretation": "not evaluated",
        "uncertainty_limitations": [
            "Statistical interval excludes waveform/model/timestep/threshold systematics.",
            "The local-white chi-square approximation is not exact for red phase noise.",
            "No stationarity, spur, or physical transient-noise-model gate is automated here.",
        ],
    }


def _write_json(payload: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=PROTOTYPE_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_test = subparsers.add_parser(
        "self-test", help="run deterministic synthetic closure and emit JSON"
    )
    self_test.add_argument("--output", help="write JSON to this path")
    self_test.add_argument("--event-count", type=int, default=32768)
    self_test.add_argument("--carrier-hz", type=float, default=10_000.0)
    self_test.add_argument("--segment-length", type=int, default=4096)
    self_test.add_argument("--samples-per-cycle", type=int, default=64)
    self_test.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEEDS),
        help="explicit independent NumPy PCG64 seeds",
    )

    analyze = subparsers.add_parser(
        "analyze", help="analyze a CSV with native time and waveform columns"
    )
    analyze.add_argument("--input", required=True)
    analyze.add_argument("--time-column", default="time_s")
    analyze.add_argument("--value-column", default="value")
    analyze.add_argument("--value-unit", required=True)
    analyze.add_argument(
        "--signal-mode",
        choices=("single-ended", "differential", "declared-expression"),
        required=True,
    )
    analyze.add_argument(
        "--signal-expression",
        required=True,
        help="exact native vector or mathematical expression exported to value-column",
    )
    analyze.add_argument("--threshold", type=float, default=0.0)
    analyze.add_argument("--min-slope", type=float, default=0.0)
    analyze.add_argument("--crop-start", type=float)
    analyze.add_argument("--crop-stop", type=float)
    analyze.add_argument("--expected-crossings", type=int)
    analyze.add_argument("--minimum-period-ratio", type=float, default=0.5)
    analyze.add_argument("--maximum-period-ratio", type=float, default=1.5)
    analyze.add_argument("--segment-length", type=int, required=True)
    analyze.add_argument("--minimum-bin", type=int, default=4)
    analyze.add_argument("--maximum-bin-mismatch", type=float, default=0.05)
    analyze.add_argument("--offset", type=float, action="append", required=True)
    analyze.add_argument("--allan-m", type=int, action="append")
    analyze.add_argument("--output", help="write JSON to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "self-test":
            payload = run_synthetic_closure(
                event_count=args.event_count,
                carrier_hz=args.carrier_hz,
                segment_length=args.segment_length,
                samples_per_cycle=args.samples_per_cycle,
                seeds=tuple(args.seeds),
            )
        elif args.command == "analyze":
            payload = analyze_csv(args)
        else:  # pragma: no cover
            raise AssertionError(args.command)
        _write_json(payload, args.output)
        return 0 if payload["status"] == "pass" else 1
    except (MethodInvalid, OSError) as exc:
        failure = {
            "prototype": PROTOTYPE_ID,
            "prototype_version": PROTOTYPE_VERSION,
            "status": "unknown",
            "diagnostic": str(exc),
        }
        _write_json(failure, getattr(args, "output", None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
