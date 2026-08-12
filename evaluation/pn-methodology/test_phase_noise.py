#!/usr/bin/env python3
"""Focused tests for the OpenADA#7 research prototype."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase_noise as pn  # noqa: E402


class CrossingTests(unittest.TestCase):
    def test_rising_crossing_uses_linear_interpolation(self) -> None:
        times = np.array([0.0, 0.4, 1.0, 1.4, 2.0])
        values = np.array([-1.0, 1.0, -1.0, 1.0, -1.0])
        observed = pn.extract_rising_crossings(times, values)
        np.testing.assert_allclose(observed, [0.2, 1.2], rtol=0.0, atol=1e-15)

    def test_crop_is_start_inclusive_stop_exclusive(self) -> None:
        times = np.array([0.9, 1.1, 1.9, 2.1, 2.9, 3.1])
        values = np.array([-1.0, 1.0, -1.0, 1.0, -1.0, 1.0])
        observed = pn.extract_rising_crossings(
            times, values, crop_start_s=1.0, crop_stop_s=3.0
        )
        np.testing.assert_allclose(observed, [1.0, 2.0], atol=1e-15)

    def test_nonmonotone_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "strictly increasing"):
            pn.extract_rising_crossings([0.0, 1.0, 0.5], [-1.0, 1.0, -1.0])

    def test_nonfinite_signal_is_rejected(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "non-finite"):
            pn.extract_rising_crossings([0.0, 1.0], [-1.0, float("nan")])

    def test_missing_crossings_are_rejected(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "no rising"):
            pn.extract_rising_crossings([0.0, 1.0, 2.0], [1.0, 1.0, 1.0])

    def test_expected_crossing_count_fails_closed(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "expected 3"):
            pn.extract_rising_crossings(
                [0.0, 0.4, 1.0, 1.4, 2.0],
                [-1.0, 1.0, -1.0, 1.0, -1.0],
                expected_count=3,
            )


class PhaseTests(unittest.TestCase):
    def test_affine_crossings_have_zero_phase(self) -> None:
        crossings = 2.5e-9 + np.arange(1024) * 100e-9
        record = pn.phase_from_crossings(crossings)
        self.assertAlmostEqual(record.sample_rate_hz, 10e6, places=3)
        self.assertLess(float(np.max(np.abs(record.phase_rad))), 2e-9)

    def test_period_ratio_gate_rejects_missing_cycle_signature(self) -> None:
        crossings = np.array([0.0, 1.0, 2.0, 4.0, 5.0])
        with self.assertRaisesRegex(pn.MethodInvalid, "above the allowed"):
            pn.phase_from_crossings(crossings, maximum_period_ratio=1.5)

    def test_c1_waveform_recovers_constructed_phase(self) -> None:
        count = 4096
        carrier = 10_000.0
        index = np.arange(count)
        phase = 0.05 * np.sin(2.0 * math.pi * index / 128.0)
        phase = pn._affine_detrend(phase)
        events = pn.event_times_from_phase(phase, carrier)
        oracle = pn.phase_from_crossings(events)
        times, values, crop_start, crop_stop = pn.synthesize_c1_event_waveform(
            events, carrier, 64
        )
        extracted = pn.extract_rising_crossings(
            times,
            values,
            crop_start_s=crop_start,
            crop_stop_s=crop_stop,
            expected_count=count,
        )
        recovered = pn.phase_from_crossings(extracted)
        error = recovered.phase_rad - oracle.phase_rad
        self.assertLess(float(np.sqrt(np.mean(error**2))), 1e-4)


class WelchTests(unittest.TestCase):
    def test_white_noise_density_scaling(self) -> None:
        sample_rate = 10_000.0
        sigma = 0.1
        rng = np.random.Generator(np.random.PCG64(1234))
        samples = rng.normal(0.0, sigma, 32768)
        estimate = pn.welch_one_sided_psd(samples, sample_rate, 4096)
        truth = 2.0 * sigma**2 / sample_rate
        ratio_db = 10.0 * np.log10(estimate.psd_per_hz[16:411] / truth)
        self.assertLess(abs(float(np.median(ratio_db))), 0.75)
        self.assertEqual(estimate.segment_count, 15)
        self.assertEqual(estimate.hop_length, 2048)
        self.assertAlmostEqual(estimate.enbw_hz, 1.5 * estimate.bin_spacing_hz)

    def test_invalid_segment_shapes_are_rejected(self) -> None:
        samples = np.ones(100)
        for segment in (7, 12, 128):
            with self.subTest(segment=segment):
                with self.assertRaises(pn.MethodInvalid):
                    pn.welch_one_sided_psd(samples, 1.0, segment)

    def test_linear_aggregation_precedes_db(self) -> None:
        values = np.array([2e-6, 8e-6])
        correct = float(pn.phase_noise_db(float(np.mean(values))))
        biased = float(np.mean(pn.phase_noise_db(values)))
        self.assertNotAlmostEqual(correct, biased)

    def test_named_bin_tie_goes_lower(self) -> None:
        self.assertEqual(pn.nearest_offset_bin(2.5, 1.0, 10), 2)

    def test_offset_above_admissible_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "outside"):
            pn.nearest_offset_bin(10.0, 1.0, 9)

    def test_known_effective_dof_and_interval(self) -> None:
        dof = pn.effective_welch_dof(8)
        self.assertAlmostEqual(dof, 15.258278145695364)
        interval = pn.chi_square_intervals_db(dof)
        np.testing.assert_allclose(
            interval["true_over_estimate_db"],
            [-2.6116386395, 3.7544648888],
            atol=1e-9,
        )


class TimeDomainCorrelationTests(unittest.TestCase):
    def test_wiener_period_jitter_and_allan_variance(self) -> None:
        count = 32768
        carrier = 10_000.0
        reference = 40 * carrier / 4096
        phase, metadata = pn.synthetic_wiener_phase(
            count, carrier, reference, -80.0, seed=9
        )
        record = pn.phase_from_crossings(pn.event_times_from_phase(phase, carrier))
        q = metadata["increment_variance_rad2"]
        expected_period = math.sqrt(q) / (2.0 * math.pi * carrier)
        observed_period = pn.jitter_metrics(record)["period_jitter_rms_s"]
        self.assertLess(abs(observed_period / expected_period - 1.0), 0.03)

        observed_allan = pn.overlapping_allan_variance(
            record.time_error_s, record.fitted_period_s, [1]
        )[0]["allan_variance"]
        expected_allan = q / (4.0 * math.pi**2)
        self.assertLess(abs(float(observed_allan) / expected_allan - 1.0), 0.05)

    def test_allan_record_too_short_is_rejected(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "too short"):
            pn.overlapping_allan_variance(np.zeros(8), 1.0, [4])


class ClosureConfigurationTests(unittest.TestCase):
    def test_duplicate_seeds_cannot_inflate_degrees_of_freedom(self) -> None:
        with self.assertRaisesRegex(pn.MethodInvalid, "distinct"):
            pn.run_synthetic_closure(
                event_count=1024,
                segment_length=1024,
                samples_per_cycle=8,
                seeds=[1, 1],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
