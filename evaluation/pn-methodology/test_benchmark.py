#!/usr/bin/env python3
"""Focused deterministic tests for the OpenADA#7 cost harness."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark as bench  # noqa: E402


class CostFormulaTests(unittest.TestCase):
    def test_eight_half_overlapped_segments_need_four_point_five_m(self) -> None:
        self.assertEqual(bench._event_count_for_segments(4096, 8), 18_432)

    def test_one_megahertz_power_of_two_bin_mapping(self) -> None:
        spacing = 2.4e9 / 16384
        rows = bench._named_offset_bins(spacing, 8191, 4)
        one_meg = rows[0]
        self.assertEqual(one_meg["status"], "available")
        self.assertEqual(one_meg["bin"], 7)
        self.assertAlmostEqual(one_meg["actual_hz"], 1_025_390.625)
        self.assertTrue(one_meg["within_candidate_five_percent"])
        self.assertEqual(rows[1]["status"], "unavailable")

    def test_wrdata_parser_rejects_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "wave.dat"
            path.write_text("time v(out) v(phi)\n0 0 0\n1 nan 0\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "non-finite"):
                bench._parse_wrdata(path)

    def test_template_has_exactly_the_resolved_placeholders(self) -> None:
        text = (bench.SCRIPT_DIR / "behavioral-noisy-oscillator.cir").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            {word for word in text.split() if word.startswith("__")},
            {"__TSTEP_S__", "__TSTOP_S__", "__TMAX_S__", "__OUTPUT_PATH__"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
