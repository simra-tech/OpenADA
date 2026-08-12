#!/usr/bin/env python3
"""Measure OpenADA#7 prototype and ngspice-46 research-spike costs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any, Sequence

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import phase_noise as pn  # noqa: E402


BENCHMARK_ID = "research.openada.pn-cost-scaling/0"
BENCHMARK_VERSION = "0.1.0"
DEFAULT_CARRIER_HZ = 2.4e9
DEFAULT_SEGMENT_COUNT = 8
DEFAULT_MINIMUM_BIN = 4
DEFAULT_SAMPLES_PER_CYCLE = 20
EVENT_SEGMENTS = tuple(2**power for power in range(10, 21))
WAVEFORM_SEGMENTS = tuple(2**power for power in range(10, 16))
NGSPICE_DURATIONS_S = (0.5e-6, 1e-6, 2e-6, 4e-6, 8e-6, 18e-6)
NAMED_OFFSETS_HZ = (1e6, 100e3, 10e3)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(payload: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)


def _maximum_rss_bytes(who: int = resource.RUSAGE_SELF) -> int:
    # Linux ru_maxrss is KiB.  This spike is explicitly a Linux-host measure.
    return int(resource.getrusage(who).ru_maxrss * 1024)


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        environment[name] = "1"
    return environment


def _cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _event_count_for_segments(segment_length: int, segment_count: int) -> int:
    hop = segment_length // 2
    return segment_length + (segment_count - 1) * hop


def _named_offset_bins(
    bin_spacing_hz: float, maximum_bin: int, minimum_bin: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for requested_hz in NAMED_OFFSETS_HZ:
        try:
            bin_index = pn.nearest_offset_bin(
                requested_hz,
                bin_spacing_hz,
                maximum_bin,
                minimum_bin=minimum_bin,
            )
        except pn.MethodInvalid as exc:
            rows.append(
                {
                    "requested_hz": requested_hz,
                    "status": "unavailable",
                    "diagnostic": str(exc),
                }
            )
            continue
        actual_hz = bin_index * bin_spacing_hz
        mismatch_percent = 100.0 * (actual_hz / requested_hz - 1.0)
        rows.append(
            {
                "requested_hz": requested_hz,
                "status": "available",
                "bin": bin_index,
                "actual_hz": actual_hz,
                "mismatch_percent": mismatch_percent,
                "within_candidate_five_percent": abs(mismatch_percent) <= 5.0,
            }
        )
    return rows


def synthetic_worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    event_count = _event_count_for_segments(args.segment_length, args.segment_count)
    lowest_offset = args.minimum_bin * args.carrier_hz / args.segment_length
    phase, construction = pn.synthetic_wiener_phase(
        event_count,
        args.carrier_hz,
        lowest_offset,
        -100.0,
        seed=args.seed,
    )
    exact_events = pn.event_times_from_phase(phase, args.carrier_hz)
    generated_at = time.perf_counter()

    waveform_sample_count = None
    crossing_time = 0.0
    if args.mode == "waveform":
        times, values, crop_start, crop_stop = pn.synthesize_c1_event_waveform(
            exact_events, args.carrier_hz, args.samples_per_cycle
        )
        waveform_sample_count = len(times)
        waveform_generated_at = time.perf_counter()
        crossings = pn.extract_rising_crossings(
            times,
            values,
            crop_start_s=crop_start,
            crop_stop_s=crop_stop,
            expected_count=event_count,
        )
        crossing_time = time.perf_counter() - waveform_generated_at
        waveform_generation_time = waveform_generated_at - generated_at
        del times, values
    else:
        crossings = exact_events
        waveform_generation_time = 0.0

    phase_started = time.perf_counter()
    record = pn.phase_from_crossings(crossings)
    phase_finished = time.perf_counter()
    estimate = pn.welch_one_sided_psd(
        record.phase_rad, record.sample_rate_hz, args.segment_length
    )
    estimate_finished = time.perf_counter()

    # This oracle work validates that the timed candidate pipeline did not
    # change semantics.  It is deliberately outside candidate_pipeline_total.
    validation_started = estimate_finished
    oracle = pn.phase_from_crossings(exact_events)
    oracle_estimate = pn.welch_one_sided_psd(
        oracle.phase_rad, oracle.sample_rate_hz, args.segment_length
    )
    finished = time.perf_counter()
    if estimate.segment_count != args.segment_count:
        raise pn.MethodInvalid(
            f"expected {args.segment_count} Welch segments, got {estimate.segment_count}"
        )
    if not np.all(np.isfinite(estimate.psd_per_hz)):
        raise pn.MethodInvalid("synthetic benchmark PSD is non-finite")
    comparison_stop = min(args.segment_length // 10, len(estimate.psd_per_hz) - 1)
    comparison = slice(args.minimum_bin, comparison_stop + 1)
    psd_ratio_db = 10.0 * np.log10(
        estimate.psd_per_hz[comparison]
        / oracle_estimate.psd_per_hz[comparison]
    )
    phase_error_rms = float(
        np.sqrt(np.mean((record.phase_rad - oracle.phase_rad) ** 2))
    )
    psd_ratio_abs_p99 = float(np.percentile(np.abs(psd_ratio_db), 99.0))
    if abs(float(np.median(psd_ratio_db))) > 0.1 or psd_ratio_abs_p99 > 0.15:
        raise pn.MethodInvalid(
            "waveform benchmark no longer closes to event oracle: "
            f"median={float(np.median(psd_ratio_db)):.6g} dB, "
            f"p99_abs={psd_ratio_abs_p99:.6g} dB, "
            f"max_abs={float(np.max(np.abs(psd_ratio_db))):.6g} dB"
        )

    return {
        "status": "pass",
        "seed": args.seed,
        "mode": args.mode,
        "carrier_hz": args.carrier_hz,
        "segment_length_events": args.segment_length,
        "segment_count": args.segment_count,
        "minimum_usable_bin": args.minimum_bin,
        "lowest_usable_offset_hz": lowest_offset,
        "bin_spacing_hz": estimate.bin_spacing_hz,
        "enbw_hz": estimate.enbw_hz,
        "named_offset_bins": _named_offset_bins(
            estimate.bin_spacing_hz,
            len(estimate.psd_per_hz) - 2,
            args.minimum_bin,
        ),
        "event_count": event_count,
        "welch_record_support_s": event_count / args.carrier_hz,
        "exact_event_span_s": float(crossings[-1] - crossings[0]),
        "samples_per_cycle": (
            args.samples_per_cycle if args.mode == "waveform" else None
        ),
        "waveform_sample_count": waveform_sample_count,
        "construction": construction,
        "extraction_vs_event_oracle": {
            "phase_error_rms_rad": phase_error_rms,
            "psd_ratio_db_band_first_bin": args.minimum_bin,
            "psd_ratio_db_band_last_bin": comparison_stop,
            "psd_ratio_db_median": float(np.median(psd_ratio_db)),
            "psd_ratio_db_abs_p99": psd_ratio_abs_p99,
            "psd_ratio_db_max_abs": float(np.max(np.abs(psd_ratio_db))),
        },
        "timing_s": {
            "phase_process_and_events": generated_at - started,
            "waveform_generation": waveform_generation_time,
            "crossing_extraction": crossing_time,
            "global_phase_fit": phase_finished - phase_started,
            "welch_psd": estimate_finished - phase_finished,
            "candidate_pipeline_total": (
                crossing_time
                + (phase_finished - phase_started)
                + (estimate_finished - phase_finished)
            ),
            "oracle_validation": finished - validation_started,
            "fixture_total": finished - started,
        },
        "worker_fixture_maximum_rss_bytes": _maximum_rss_bytes(),
    }


def _run_json_child(command: list[str], timeout_s: float) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s,
        env=_child_environment(),
    )
    wall = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"child failed ({completed.returncode}): "
            f"{(completed.stdout + completed.stderr)[-4000:]}"
        )
    payload = json.loads(completed.stdout)
    if payload.get("status") != "pass":
        raise RuntimeError(f"child returned non-pass: {payload}")
    payload["process_wallclock_s"] = wall
    payload["child_stderr"] = completed.stderr
    return payload, wall


def _summarize_repeats(repeats: list[dict[str, Any]]) -> dict[str, Any]:
    analysis = [item["timing_s"]["candidate_pipeline_total"] for item in repeats]
    process = [item["process_wallclock_s"] for item in repeats]
    rss = [item["worker_fixture_maximum_rss_bytes"] for item in repeats]
    first = repeats[0]
    fixed_fields = (
        "status",
        "mode",
        "carrier_hz",
        "segment_length_events",
        "segment_count",
        "minimum_usable_bin",
        "lowest_usable_offset_hz",
        "event_count",
        "welch_record_support_s",
        "samples_per_cycle",
    )
    result = {key: first[key] for key in fixed_fields}
    for item in repeats[1:]:
        for key in fixed_fields:
            if item[key] != result[key]:
                raise RuntimeError(f"repeat changed fixed benchmark field {key}")
    waveform_counts = [
        item["waveform_sample_count"]
        for item in repeats
        if item["waveform_sample_count"] is not None
    ]
    result["scale_count"] = (
        int(median(waveform_counts)) if waveform_counts else first["event_count"]
    )
    result["scale_count_kind"] = (
        "waveform samples" if waveform_counts else "phase events"
    )
    if waveform_counts:
        result["waveform_sample_count"] = {
            "minimum": min(waveform_counts),
            "median": int(median(waveform_counts)),
            "maximum": max(waveform_counts),
        }
    result["exact_event_span_s"] = {
        "minimum": min(item["exact_event_span_s"] for item in repeats),
        "median": median(item["exact_event_span_s"] for item in repeats),
        "maximum": max(item["exact_event_span_s"] for item in repeats),
    }
    for key in ("bin_spacing_hz", "enbw_hz"):
        values = [item[key] for item in repeats]
        result[key] = {
            "minimum": min(values),
            "median": median(values),
            "maximum": max(values),
        }
    result["repeat_count"] = len(repeats)
    result["candidate_pipeline_wallclock_s"] = {
        "minimum": min(analysis),
        "median": median(analysis),
        "maximum": max(analysis),
    }
    result["process_wallclock_s"] = {
        "minimum": min(process),
        "median": median(process),
        "maximum": max(process),
    }
    result["worker_fixture_maximum_rss_bytes"] = {
        "minimum": min(rss),
        "median": int(median(rss)),
        "maximum": max(rss),
    }
    result["stage_wallclock_s_median"] = {
        stage: median([item["timing_s"][stage] for item in repeats])
        for stage in repeats[0]["timing_s"]
        if stage not in {"candidate_pipeline_total", "fixture_total"}
    }
    result["repeat_measurements"] = [
        {
            "seed": item["seed"],
            "construction": item["construction"],
            "exact_event_span_s": item["exact_event_span_s"],
            "waveform_sample_count": item["waveform_sample_count"],
            "bin_spacing_hz": item["bin_spacing_hz"],
            "enbw_hz": item["enbw_hz"],
            "named_offset_bins": item["named_offset_bins"],
            "extraction_vs_event_oracle": item["extraction_vs_event_oracle"],
            "timing_s": item["timing_s"],
            "process_wallclock_s": item["process_wallclock_s"],
            "worker_fixture_maximum_rss_bytes": item[
                "worker_fixture_maximum_rss_bytes"
            ],
        }
        for item in repeats
    ]
    return result


def _power_law_fit(rows: list[dict[str, Any]], size_key: str) -> dict[str, float]:
    selected = rows[-min(5, len(rows)) :]
    x = np.log10([float(item[size_key]) for item in selected])
    y = np.log10(
        [float(item["candidate_pipeline_wallclock_s"]["median"]) for item in selected]
    )
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    residual = float(np.sum((y - predicted) ** 2))
    total = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 - residual / total if total > 0.0 else 1.0
    return {
        "fit_point_count": len(selected),
        "runtime_power_law_exponent": float(slope),
        "log10_intercept": float(intercept),
        "r_squared": r_squared,
        "note": "descriptive fit to largest measured cases; not a simulator forecast",
    }


def run_synthetic_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeats < 1:
        raise RuntimeError("--repeats must be at least one")
    if not math.isfinite(args.timeout) or args.timeout <= 0.0:
        raise RuntimeError("--timeout must be finite and positive")
    if not math.isfinite(args.carrier_hz) or args.carrier_hz <= 0.0:
        raise RuntimeError("--carrier-hz must be finite and positive")
    if args.segment_count < 2:
        raise RuntimeError("--segment-count must be at least two")
    if args.minimum_bin < 2:
        raise RuntimeError("--minimum-bin must be at least two")
    if args.samples_per_cycle < 8:
        raise RuntimeError("--samples-per-cycle must be at least eight")
    script = Path(__file__).resolve()
    groups: dict[str, list[dict[str, Any]]] = {}
    for mode, segment_lengths in (
        ("event-only", EVENT_SEGMENTS),
        ("waveform", WAVEFORM_SEGMENTS),
    ):
        rows: list[dict[str, Any]] = []
        for segment_length in segment_lengths:
            repeats = []
            for repeat in range(args.repeats):
                command = [
                    sys.executable,
                    str(script),
                    "_synthetic-worker",
                    "--mode",
                    mode,
                    "--carrier-hz",
                    repr(args.carrier_hz),
                    "--segment-length",
                    str(segment_length),
                    "--segment-count",
                    str(args.segment_count),
                    "--minimum-bin",
                    str(args.minimum_bin),
                    "--samples-per-cycle",
                    str(args.samples_per_cycle),
                    "--seed",
                    str(1000 + repeat),
                ]
                payload, _ = _run_json_child(command, args.timeout)
                repeats.append(payload)
            rows.append(_summarize_repeats(repeats))
        groups[mode] = rows

    return {
        "benchmark": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "status": "pass",
        "claim": "measured synthetic Python cost on this host; no ngspice or physical-PN claim",
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": pn.scipy.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_model": _cpu_model(),
            "script_path": str(script),
            "script_sha256": _sha256(script),
            "phase_noise_script_sha256": _sha256(SCRIPT_DIR / "phase_noise.py"),
            "cpu_count_visible": os.cpu_count(),
            "child_thread_environment": {
                name: "1"
                for name in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                )
            },
        },
        "policy": {
            "carrier_hz": args.carrier_hz,
            "periodic_hann_overlap_fraction": 0.5,
            "segment_count": args.segment_count,
            "minimum_usable_bin": args.minimum_bin,
            "samples_per_cycle_for_waveform": args.samples_per_cycle,
            "record_event_count_formula": "M + (K-1)*(M/2)",
            "lowest_usable_offset_formula": "k_min*f_carrier/M",
        },
        "event_only": groups["event-only"],
        "waveform": groups["waveform"],
        "scaling_fits": {
            "event_only": _power_law_fit(groups["event-only"], "scale_count"),
            "waveform": _power_law_fit(groups["waveform"], "scale_count"),
        },
        "limitations": [
            "Each repeat is a fresh process; worker timing excludes Python import startup.",
            "Candidate-pipeline timing includes crossings (waveform mode), one global phase fit, and one Welch PSD; fixture generation and oracle validation are separate.",
            "Worker-fixture maximum RSS includes Python/NumPy/SciPy, synthetic construction, candidate analysis, and oracle validation; it is not an isolated estimator-memory measurement.",
            "Waveform cases stop before memory-heavy low-offset cases; event-only cases continue.",
            "Synthetic cost is not a transistor-level ngspice cost model.",
        ],
    }


def _resolve_ngspice(requested: str | None) -> Path:
    candidate = requested or shutil.which("ngspice")
    if not candidate:
        raise RuntimeError("ngspice is not on PATH")
    path = Path(candidate).resolve()
    if not path.is_file():
        raise RuntimeError(f"ngspice path is not a file: {path}")
    return path


def _parse_wrdata(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.loadtxt(path, skiprows=1)
    if data.ndim != 2 or data.shape[1] != 3:
        raise RuntimeError(f"unexpected wrdata shape {data.shape}")
    if not np.all(np.isfinite(data)):
        raise RuntimeError("wrdata output contains a non-finite value")
    return data[:, 0], data[:, 1], data[:, 2]


def ngspice_worker(args: argparse.Namespace) -> dict[str, Any]:
    ngspice = _resolve_ngspice(args.ngspice)
    template_path = SCRIPT_DIR / "behavioral-noisy-oscillator.cir"
    template = template_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="openada-pn-ngspice-") as temp_name:
        temp = Path(temp_name)
        waveform = temp / "waveform.dat"
        deck = temp / "run.cir"
        tstep_s = 1.0 / (20.0 * DEFAULT_CARRIER_HZ)
        tmax_s = 1.0 / (40.0 * DEFAULT_CARRIER_HZ)
        resolved = (
            template.replace("__TSTEP_S__", f"{tstep_s:.17g}")
            .replace("__TSTOP_S__", f"{args.stop_s:.17g}")
            .replace("__TMAX_S__", f"{tmax_s:.17g}")
            .replace("__OUTPUT_PATH__", waveform.name)
        )
        deck.write_text(resolved, encoding="utf-8")
        started = time.perf_counter()
        completed = subprocess.run(
            [str(ngspice), "-n", "-b", str(deck)],
            cwd=temp,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=args.timeout,
            env=_child_environment(),
        )
        simulation_finished = time.perf_counter()
        if completed.returncode != 0 or not waveform.is_file():
            raise RuntimeError(
                f"ngspice failed ({completed.returncode}): "
                f"{(completed.stdout + completed.stderr)[-2000:]}"
            )
        times, values, phase_truth = _parse_wrdata(waveform)
        parsed_at = time.perf_counter()
        output_dt = np.diff(times)
        all_crossings = pn.extract_rising_crossings(times, values, threshold=0.0)
        maximum_m = len(all_crossings) / 4.5
        segment_length = 2 ** int(math.floor(math.log2(maximum_m)))
        if segment_length < 8:
            raise RuntimeError("ngspice waveform is too short for eight Welch segments")
        used_event_count = _event_count_for_segments(
            segment_length, DEFAULT_SEGMENT_COUNT
        )
        crossings = all_crossings[:used_event_count]
        record = pn.phase_from_crossings(
            crossings, minimum_period_ratio=0.5, maximum_period_ratio=1.5
        )
        estimate = pn.welch_one_sided_psd(
            record.phase_rad, record.sample_rate_hz, segment_length
        )
        if estimate.segment_count != DEFAULT_SEGMENT_COUNT:
            raise RuntimeError(
                f"expected {DEFAULT_SEGMENT_COUNT} Welch segments, "
                f"got {estimate.segment_count}"
            )
        truth_at_crossings = np.interp(crossings, times, phase_truth)
        truth_residual = pn._affine_detrend(truth_at_crossings)
        truth_residual /= DEFAULT_CARRIER_HZ * record.fitted_period_s
        phase_error = record.phase_rad - truth_residual
        truth_estimate = pn.welch_one_sided_psd(
            truth_residual, record.sample_rate_hz, segment_length
        )
        comparison_stop = min(segment_length // 10, len(estimate.psd_per_hz) - 1)
        comparison = slice(DEFAULT_MINIMUM_BIN, comparison_stop + 1)
        psd_ratio_db = 10.0 * np.log10(
            estimate.psd_per_hz[comparison] / truth_estimate.psd_per_hz[comparison]
        )
        extraction_truth = {
            "phase_error_rms_rad": float(np.sqrt(np.mean(phase_error**2))),
            "phase_error_max_abs_rad": float(np.max(np.abs(phase_error))),
            "psd_ratio_db_band_first_bin": DEFAULT_MINIMUM_BIN,
            "psd_ratio_db_band_last_bin": comparison_stop,
            "psd_ratio_db_median": float(np.median(psd_ratio_db)),
            "psd_ratio_db_max_abs": float(np.max(np.abs(psd_ratio_db))),
        }
        if (
            extraction_truth["phase_error_rms_rad"] > 1e-3
            or abs(extraction_truth["psd_ratio_db_median"]) > 0.05
            or extraction_truth["psd_ratio_db_max_abs"] > 0.1
        ):
            raise RuntimeError(
                "behavioral extraction no longer closes to simulator phase truth: "
                f"{extraction_truth}"
            )
        analyzed_at = time.perf_counter()
        minimum_bin = DEFAULT_MINIMUM_BIN
        offset_bin = min(minimum_bin, len(estimate.psd_per_hz) - 2)
        l_value = float(pn.phase_noise_db(estimate.psd_per_hz[offset_bin]))
        artifact_sha = _sha256(waveform)
        artifact_bytes = waveform.stat().st_size
        output_rows = len(times)
        deck_sha = _sha256(deck)
        terminal_phase_truth_rad = float(phase_truth[-1])

    return {
        "status": "pass",
        "stop_s": args.stop_s,
        "ngspice_and_ascii_export_wallclock_s": simulation_finished - started,
        "wrdata_parse_wallclock_s": parsed_at - simulation_finished,
        "phase_and_psd_wallclock_s": analyzed_at - parsed_at,
        "worker_total_wallclock_s": analyzed_at - started,
        "ngspice_returncode": completed.returncode,
        "ngspice_stdout_tail": completed.stdout[-1000:],
        "ngspice_stderr_tail": completed.stderr[-1000:],
        "resolved_deck_sha256": deck_sha,
        "waveform": {
            "sha256": artifact_sha,
            "bytes": artifact_bytes,
            "row_count": output_rows,
            "time_start_s": float(times[0]),
            "time_stop_s": float(times[-1]),
            "observed_timestep_s": {
                "minimum": float(np.min(output_dt)),
                "median": float(np.median(output_dt)),
                "maximum": float(np.max(output_dt)),
            },
            "retained_after_summary": False,
        },
        "terminal_phase_truth_rad": terminal_phase_truth_rad,
        "observed_crossing_count": len(all_crossings),
        "used_crossing_count": len(crossings),
        "discarded_trailing_crossings": len(all_crossings) - len(crossings),
        "fitted_carrier_hz": record.sample_rate_hz,
        "welch_segment_length_events": segment_length,
        "welch_segment_count": estimate.segment_count,
        "minimum_usable_bin": minimum_bin,
        "lowest_usable_offset_hz": minimum_bin * estimate.bin_spacing_hz,
        "bin_spacing_hz": estimate.bin_spacing_hz,
        "enbw_hz": estimate.enbw_hz,
        "named_offset_bins": _named_offset_bins(
            estimate.bin_spacing_hz,
            len(estimate.psd_per_hz) - 2,
            minimum_bin,
        ),
        "pipeline_l_at_lowest_bin_db_per_hz": l_value,
        "pipeline_l_interpretation": (
            "behavioral injector/pipeline telemetry only; not physical oscillator PN"
        ),
        "extraction_vs_simulator_phase_truth": extraction_truth,
        "maximum_child_rss_bytes": _maximum_rss_bytes(resource.RUSAGE_CHILDREN),
    }


def run_ngspice_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeats < 1:
        raise RuntimeError("--repeats must be at least one")
    if not math.isfinite(args.timeout) or args.timeout <= 0.0:
        raise RuntimeError("--timeout must be finite and positive")
    script = Path(__file__).resolve()
    ngspice = _resolve_ngspice(args.ngspice)
    version = subprocess.run(
        [str(ngspice), "--version-full"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    ).stdout
    rows = []
    for stop_s in NGSPICE_DURATIONS_S:
        repeats = []
        for _ in range(args.repeats):
            command = [
                sys.executable,
                str(script),
                "_ngspice-worker",
                "--ngspice",
                str(ngspice),
                "--stop-s",
                repr(stop_s),
                "--timeout",
                repr(args.timeout),
            ]
            payload, wall = _run_json_child(command, args.timeout + 10.0)
            payload["process_wallclock_s"] = wall
            repeats.append(payload)
        first = repeats[0]
        fixed_fields = (
            "status",
            "stop_s",
            "ngspice_returncode",
            "resolved_deck_sha256",
            "welch_segment_length_events",
            "welch_segment_count",
            "minimum_usable_bin",
            "pipeline_l_interpretation",
        )
        row = {key: first[key] for key in fixed_fields}
        for item in repeats[1:]:
            for key in fixed_fields:
                if item[key] != row[key]:
                    raise RuntimeError(f"ngspice repeat changed fixed field {key}")
        row["repeat_count"] = len(repeats)
        row["repeat_artifacts"] = [
            {
                "resolved_deck_sha256": item["resolved_deck_sha256"],
                "waveform": item["waveform"],
                "observed_crossing_count": item["observed_crossing_count"],
                "used_crossing_count": item["used_crossing_count"],
                "fitted_carrier_hz": item["fitted_carrier_hz"],
                "terminal_phase_truth_rad": item["terminal_phase_truth_rad"],
                "pipeline_l_at_lowest_bin_db_per_hz": item[
                    "pipeline_l_at_lowest_bin_db_per_hz"
                ],
                "bin_spacing_hz": item["bin_spacing_hz"],
                "enbw_hz": item["enbw_hz"],
                "lowest_usable_offset_hz": item["lowest_usable_offset_hz"],
                "named_offset_bins": item["named_offset_bins"],
                "extraction_vs_simulator_phase_truth": item[
                    "extraction_vs_simulator_phase_truth"
                ],
            }
            for item in repeats
        ]
        for key in (
            "observed_crossing_count",
            "used_crossing_count",
            "discarded_trailing_crossings",
            "fitted_carrier_hz",
            "lowest_usable_offset_hz",
            "bin_spacing_hz",
            "enbw_hz",
            "pipeline_l_at_lowest_bin_db_per_hz",
            "terminal_phase_truth_rad",
        ):
            values = [item[key] for item in repeats]
            row[key] = {
                "minimum": min(values),
                "median": median(values),
                "maximum": max(values),
            }
        row["extraction_vs_simulator_phase_truth"] = {
            key: {
                "minimum": min(
                    item["extraction_vs_simulator_phase_truth"][key]
                    for item in repeats
                ),
                "median": median(
                    item["extraction_vs_simulator_phase_truth"][key]
                    for item in repeats
                ),
                "maximum": max(
                    item["extraction_vs_simulator_phase_truth"][key]
                    for item in repeats
                ),
            }
            for key in (
                "phase_error_rms_rad",
                "phase_error_max_abs_rad",
                "psd_ratio_db_median",
                "psd_ratio_db_max_abs",
            )
        }
        for key in (
            "ngspice_and_ascii_export_wallclock_s",
            "wrdata_parse_wallclock_s",
            "phase_and_psd_wallclock_s",
            "worker_total_wallclock_s",
            "process_wallclock_s",
        ):
            values = [item[key] for item in repeats]
            row[key] = {
                "minimum": min(values),
                "median": median(values),
                "maximum": max(values),
            }
        rss = [item["maximum_child_rss_bytes"] for item in repeats]
        row["maximum_child_rss_bytes"] = {
            "minimum": min(rss),
            "median": int(median(rss)),
            "maximum": max(rss),
        }
        rows.append(row)

    x = np.asarray([row["stop_s"] for row in rows])
    y = np.asarray(
        [row["ngspice_and_ascii_export_wallclock_s"]["median"] for row in rows]
    )
    linear_slope, linear_intercept = np.polyfit(x, y, 1)
    predicted = linear_slope * x + linear_intercept
    r_squared = 1.0 - float(np.sum((y - predicted) ** 2)) / float(
        np.sum((y - np.mean(y)) ** 2)
    )
    return {
        "benchmark": "research.openada.pn-ngspice-behavioral-cost/0",
        "benchmark_version": BENCHMARK_VERSION,
        "status": "pass",
        "claim": "PDK-free behavioral TRNOISE pipeline/runtime evidence only",
        "runtime": {
            "ngspice_path": str(ngspice),
            "ngspice_sha256": _sha256(ngspice),
            "ngspice_version_full": version,
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": pn.scipy.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_model": _cpu_model(),
            "template_path": str(SCRIPT_DIR / "behavioral-noisy-oscillator.cir"),
            "template_sha256": _sha256(
                SCRIPT_DIR / "behavioral-noisy-oscillator.cir"
            ),
            "benchmark_script_sha256": _sha256(script),
            "phase_noise_script_sha256": _sha256(SCRIPT_DIR / "phase_noise.py"),
        },
        "configuration": {
            "carrier_hz": DEFAULT_CARRIER_HZ,
            "transient_requested_tstep_s": 1.0 / (20.0 * DEFAULT_CARRIER_HZ),
            "maximum_step_s": 1.0 / (40.0 * DEFAULT_CARRIER_HZ),
            "trnoise_update_s": 1.0 / (20.0 * DEFAULT_CARRIER_HZ),
            "trnoise_frequency_error_sample_rms_hz": 10e6,
            "phase_integrator_capacitance_f": 1e-9,
            "integration_method": "trap, maxord=2, uic",
            "reltol": 1e-6,
            "temperature_c": 27.0,
            "initial_condition_policy": "UIC with Cphi IC=0",
            "saved_vectors": ["time", "v(out)", "v(phi)"],
            "waveform_encoding": "ngspice wrdata ASCII, one shared time column",
            "ascii_significant_digits": 17,
            "waveform_compression": "disabled",
            "ngspice_user_init": "disabled with -n",
            "durations_s": list(NGSPICE_DURATIONS_S),
        },
        "rows": rows,
        "ngspice_and_ascii_export_linear_fit": {
            "seconds_per_simulated_second": float(linear_slope),
            "wallclock_intercept_s": float(linear_intercept),
            "r_squared": r_squared,
            "note": (
                "descriptive fit for this behavioral deck, ASCII export path, "
                "and host only"
            ),
        },
        "limitations": [
            "The oscillator uses an explicit behavioral carrier and authored phase injector.",
            "TRNOISE is experimental and does not represent compact-model device noise here.",
            "TRNOISE replay is not claimed; repeats are runtime samples, not common-seed comparisons.",
            "The reported ngspice interval includes portable ASCII wrdata export time.",
            "Large waveform artifacts are hashed and summarized, then deleted rather than committed.",
        ],
    }


def _probe_deck(
    ngspice: Path, directory: Path, name: str, text: str, *, disable_init: bool
) -> dict[str, Any]:
    deck = directory / f"{name}.cir"
    deck.write_text(text, encoding="utf-8")
    command = [str(ngspice)]
    if disable_init:
        command.append("-n")
    command.extend(["-b", str(deck)])
    completed = subprocess.run(
        command,
        cwd=directory,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    combined = completed.stdout + completed.stderr
    return {
        "name": name,
        "returncode": completed.returncode,
        "deck_sha256": _sha256(deck),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "combined_tail": combined[-2000:],
    }


def run_ngspice_probes(args: argparse.Namespace) -> dict[str, Any]:
    ngspice = _resolve_ngspice(args.ngspice)
    version = subprocess.run(
        [str(ngspice), "--version-full"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="openada-pn-probes-") as temp_name:
        temp = Path(temp_name)
        pss = _probe_deck(
            ngspice,
            temp,
            "pss",
            "PSS availability probe\nV1 out 0 sin(0 1 1k)\nR1 out 0 1k\n"
            ".pss 1k 1m out 128 5 10 1e-3\n.end\n",
            disable_init=True,
        )
        hb = _probe_deck(
            ngspice,
            temp,
            "hb",
            "HB availability probe\nV1 out 0 sin(0 1 1k)\nR1 out 0 1k\n"
            ".hb 1k 5\n.end\n",
            disable_init=True,
        )

        current_dir = temp / "current"
        current_dir.mkdir()
        current_output = current_dir / "current.dat"
        current = _probe_deck(
            ngspice,
            current_dir,
            "current",
            "Current TRNOISE probe\nINOISE 0 out DC 0 TRNOISE(1m 10p 0 0)\n"
            "RLOAD out 0 1k\n.control\nset wr_singlescale\nset wr_vecnames\n"
            "set numdgt=17\ntran 10p 50p 0 10p\n"
            "wrdata current.dat v(out)\n.endc\n.end\n",
            disable_init=True,
        )
        current_values = np.loadtxt(current_output, skiprows=1)[:, 1]
        current["output_sha256"] = _sha256(current_output)
        current["maximum_absolute_output_v"] = float(np.max(np.abs(current_values)))
        current["functional"] = bool(np.max(np.abs(current_values)) > 0.0)

        seed_deck = (
            "TRNOISE startup-seed reproducibility probe\n"
            "VNOISE out 0 DC 0 TRNOISE(1m 10p 0 0)\nRLOAD out 0 1k\n"
            ".control\nset wr_singlescale\nset wr_vecnames\n"
            "set numdgt=17\ntran 10p 50p 0 10p\n"
            "wrdata seed.dat v(out)\n.endc\n.end\n"
        )
        seed_runs = []
        for run_index, seed_value in ((1, 12345), (2, 12345), (3, 54321)):
            run_dir = temp / f"seed-{run_index}"
            run_dir.mkdir()
            init = run_dir / ".spiceinit"
            init.write_text(
                f"setseed {seed_value}\n"
                f"echo OPENADA_PN_SETSEED_{seed_value}_LOADED\n",
                encoding="utf-8",
            )
            result = _probe_deck(
                ngspice,
                run_dir,
                "seed",
                seed_deck,
                disable_init=False,
            )
            output = run_dir / "seed.dat"
            result["init_sha256"] = _sha256(init)
            result["output_sha256"] = _sha256(output)
            result["last_output_v"] = float(np.loadtxt(output, skiprows=1)[-1, 1])
            result["requested_seed"] = seed_value
            result["startup_init_loaded"] = (
                f"OPENADA_PN_SETSEED_{seed_value}_LOADED" in result["stdout"]
            )
            seed_runs.append(result)

    pss_unavailable = "unimplemented dot command '.pss'" in pss["combined_tail"]
    hb_unavailable = "unimplemented dot command '.hb'" in hb["combined_tail"]
    seed_replayed = seed_runs[0]["output_sha256"] == seed_runs[1]["output_sha256"]
    different_seed_distinct = (
        seed_runs[0]["output_sha256"] != seed_runs[2]["output_sha256"]
    )
    seed_init_loaded = all(item["startup_init_loaded"] for item in seed_runs)
    passed = (
        pss_unavailable
        and hb_unavailable
        and current["functional"]
        and seed_init_loaded
    )
    return {
        "probe": "research.openada.pn-ngspice-capability-probes/0",
        "status": "pass" if passed else "unknown",
        "status_meaning": "capability-probe evidence captured and recognized",
        "methodology_gate": (
            "pass"
            if seed_init_loaded and seed_replayed and different_seed_distinct
            else "fail"
        ),
        "runtime": {
            "ngspice_path": str(ngspice),
            "ngspice_sha256": _sha256(ngspice),
            "ngspice_version_full": version,
        },
        "pss": {
            **pss,
            "capability_state": "unavailable" if pss_unavailable else "unknown",
            "capability_available": False if pss_unavailable else None,
        },
        "harmonic_balance": {
            **hb,
            "capability_state": "unavailable" if hb_unavailable else "unknown",
            "capability_available": False if hb_unavailable else None,
        },
        "current_source_trnoise": current,
        "documented_startup_seed_replay": {
            "same_seed": 12345,
            "different_seed_control": 54321,
            "runs": seed_runs,
            "same_seed_outputs_identical": seed_replayed,
            "different_seed_output_distinct": different_seed_distinct,
            "startup_init_loaded": seed_init_loaded,
            "gate": (
                "pass"
                if seed_init_loaded and seed_replayed and different_seed_distinct
                else "fail"
            ),
        },
        "interpretation": {
            "pss_hb": "not evaluated - capability unavailable",
            "seed_contract": (
                "replay established"
                if seed_init_loaded and seed_replayed and different_seed_distinct
                else "replay gate failed"
            ),
            "current_trnoise": "functional source probe only; no calibration claim",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=BENCHMARK_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    synthetic = subparsers.add_parser("synthetic", help="measure Python scaling")
    synthetic.add_argument("--output")
    synthetic.add_argument("--carrier-hz", type=float, default=DEFAULT_CARRIER_HZ)
    synthetic.add_argument("--segment-count", type=int, default=DEFAULT_SEGMENT_COUNT)
    synthetic.add_argument("--minimum-bin", type=int, default=DEFAULT_MINIMUM_BIN)
    synthetic.add_argument(
        "--samples-per-cycle", type=int, default=DEFAULT_SAMPLES_PER_CYCLE
    )
    synthetic.add_argument("--repeats", type=int, default=3)
    synthetic.add_argument("--timeout", type=float, default=120.0)

    ngspice = subparsers.add_parser(
        "ngspice", help="measure the PDK-free behavioral noisy-transient pipeline"
    )
    ngspice.add_argument("--output")
    ngspice.add_argument("--ngspice")
    ngspice.add_argument("--repeats", type=int, default=3)
    ngspice.add_argument("--timeout", type=float, default=600.0)

    probes = subparsers.add_parser("probes", help="retain ngspice capability probes")
    probes.add_argument("--output")
    probes.add_argument("--ngspice")

    worker = subparsers.add_parser("_synthetic-worker", help=argparse.SUPPRESS)
    worker.add_argument("--mode", choices=("event-only", "waveform"), required=True)
    worker.add_argument("--carrier-hz", type=float, required=True)
    worker.add_argument("--segment-length", type=int, required=True)
    worker.add_argument("--segment-count", type=int, required=True)
    worker.add_argument("--minimum-bin", type=int, required=True)
    worker.add_argument("--samples-per-cycle", type=int, required=True)
    worker.add_argument("--seed", type=int, required=True)

    ng_worker = subparsers.add_parser("_ngspice-worker", help=argparse.SUPPRESS)
    ng_worker.add_argument("--ngspice", required=True)
    ng_worker.add_argument("--stop-s", type=float, required=True)
    ng_worker.add_argument("--timeout", type=float, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "synthetic":
            payload = run_synthetic_benchmark(args)
        elif args.command == "ngspice":
            payload = run_ngspice_benchmark(args)
        elif args.command == "probes":
            payload = run_ngspice_probes(args)
        elif args.command == "_synthetic-worker":
            payload = synthetic_worker(args)
        elif args.command == "_ngspice-worker":
            payload = ngspice_worker(args)
        else:  # pragma: no cover
            raise AssertionError(args.command)
        _write_json(payload, getattr(args, "output", None))
        return 0 if payload["status"] == "pass" else 1
    except (OSError, RuntimeError, pn.MethodInvalid, subprocess.TimeoutExpired) as exc:
        _write_json(
            {
                "benchmark": BENCHMARK_ID,
                "benchmark_version": BENCHMARK_VERSION,
                "status": "unknown",
                "diagnostic": str(exc),
            },
            getattr(args, "output", None),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
