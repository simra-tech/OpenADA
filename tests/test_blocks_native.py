"""Native golden-case replay for the packaged behavioral block library.

Each ``blocks/bhv-core/blocks/<block>/cases/*.json`` document is a
backend-neutral stimulus/observation/relations contract.  This module builds
the deck each case describes, composes the block's reviewed prelude with
:func:`compose_blocks`, runs the shared circuit.simulate operation against
real ngspice, and evaluates every declared relation from the retained raw
file using the same bounded extraction the evidence chain trusts
(:func:`openada.engines.ngspice_outputs.extract_analysis_raw`).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shutil

import pytest

from openada.block_library import compose_blocks, load_block_library
from openada.discovery import DiscoveryManager
from openada.engines.ngspice_outputs import extract_analysis_raw
from openada.operations.simulate import simulate


NGSPICE = shutil.which("ngspice")
pytestmark = pytest.mark.skipif(NGSPICE is None, reason="native ngspice is not installed")

ROOT = Path(__file__).parents[1]
def _has_native_backend(case_path: Path) -> bool:
    contract = case_path.parent.parent / "block.json"
    return "ngspice-native" in json.loads(contract.read_text())["backends"]


# Verilog-a-only blocks (comparator_clocked_phys) graduate through the OSDI
# battery (tests/test_blocks_osdi_comparator.py), not the native runner.
CASE_PATHS = sorted(
    p
    for p in (ROOT / "blocks" / "bhv-core" / "blocks").glob("*/cases/*.json")
    if _has_native_backend(p)
)

# The one declared composite observation shape: a pointwise conductance
# overlap witness built from two probe-source branch currents.
_MIN_ABS_RE = re.compile(r"min\(abs\((i\(\w+\))\),abs\((i\(\w+\))\)\)")

_EXTRACTION_BOUNDS = {"max_points": 2_000_000, "max_selected_scalars": 64_000_000}


def _parse_signal(signal: str) -> tuple[str, object]:
    compact = signal.replace(" ", "")
    if compact.startswith("vdb(") and compact.endswith(")"):
        return "vdb", f"v({compact[4:-1]})"
    match = _MIN_ABS_RE.fullmatch(compact)
    if match is not None:
        return "minabs", (match.group(1), match.group(2))
    return "plain", compact


def _native_vectors(case: dict) -> set[str]:
    vectors: set[str] = set()
    for observation in case["observations"]:
        kind, payload = _parse_signal(observation["signal"])
        if kind == "minabs":
            vectors.update(payload)
        else:
            vectors.add(payload)
    return vectors


def _build_deck(case: dict) -> str:
    analysis = case["analysis"]
    lines = [f"* golden case {case['case_id']}"]
    lines.extend(case["fixtures"])
    lines.extend(case["dut"].split("\n"))
    # Save exactly the observed vectors: xspice bridge instances register
    # duplicate branch-current names in an unrestricted raw write set, which
    # the strict extraction parser refuses as ambiguous.
    lines.append(".save " + " ".join(sorted(_native_vectors(case))))
    if analysis["type"] == "tran":
        # Never uic: the shared profile refuses it; cases carry ic= instead.
        lines.append(f".tran {analysis['tstep']} {analysis['tstop']}")
    else:
        lines.append(
            f".ac {analysis['variation']} {analysis['points']} "
            f"{analysis['fstart']} {analysis['fstop']}"
        )
    lines.append(".end")
    return "\n".join(lines) + "\n"


def _analysis_binding(analysis: dict) -> dict:
    if analysis["type"] == "tran":
        return {"type": "tran"}
    return {
        "type": "ac",
        "sweep": analysis["variation"],
        "points": analysis["points"],
        "start_hz": float(analysis["fstart"]),
        "stop_hz": float(analysis["fstop"]),
    }


def _first_crossing(
    axis: list[float],
    values: list[float],
    *,
    level: float,
    direction: str,
    occurrence: int,
    log_axis: bool,
) -> float:
    seen = 0
    for index in range(len(values) - 1):
        before, after = values[index], values[index + 1]
        if direction == "rise":
            hit = before < level <= after
        else:
            hit = before > level >= after
        if not hit:
            continue
        seen += 1
        if seen < occurrence:
            continue
        x0, x1 = axis[index], axis[index + 1]
        if after == before:
            return x1
        fraction = (level - before) / (after - before)
        if log_axis:
            # An AC magnitude in dB is linear in log-frequency for the
            # single-pole responses these cases assert, so interpolate there.
            return 10 ** (
                math.log10(x0) + fraction * (math.log10(x1) - math.log10(x0))
            )
        return x0 + fraction * (x1 - x0)
    raise AssertionError(
        f"no {direction} crossing #{occurrence} of level {level} was observed"
    )


def _evaluate_observation(observation: dict, axis: list[float], values: list[float]) -> float:
    kind = observation["kind"]
    if kind in {"avg", "min", "max"}:
        low = float(observation.get("from", axis[0]))
        high = float(observation.get("to", axis[-1]))
        window = [index for index, point in enumerate(axis) if low <= point <= high]
        assert len(window) >= 2, "the observation window holds too few samples"
        selected = [values[index] for index in window]
        if kind == "min":
            return min(selected)
        if kind == "max":
            return max(selected)
        # Time-weighted trapezoidal mean: transient samples cluster around
        # switching edges, so an unweighted mean would be edge-biased.
        integral = 0.0
        for left, right in zip(window, window[1:]):
            integral += 0.5 * (values[left] + values[right]) * (axis[right] - axis[left])
        return integral / (axis[window[-1]] - axis[window[0]])
    if kind == "value_at":
        target = float(observation["time"])
        nearest = min(range(len(axis)), key=lambda index: abs(axis[index] - target))
        return values[nearest]
    if kind == "cross":
        return _first_crossing(
            axis,
            values,
            level=float(observation["level"]),
            direction=observation["direction"],
            occurrence=int(observation.get("occurrence", 1)),
            log_axis=observation.get("axis") == "frequency",
        )
    raise AssertionError(f"unsupported observation kind {kind!r}")


def _run_case(case_path: Path, tmp_path: Path) -> tuple[dict, dict[str, float]]:
    block_id = case_path.parent.parent.name
    case = json.loads(case_path.read_text(encoding="utf-8"))

    library = load_block_library("bhv-core")
    composed = compose_blocks(library, (block_id,))
    models_file = tmp_path / "behavioral-blocks.model.spice"
    models_file.write_text(composed.text, encoding="utf-8")
    deck = tmp_path / f"{case['case_id']}.cir"
    deck.write_text(_build_deck(case), encoding="utf-8")

    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        backend="ngspice",
        models_file=models_file,
    )
    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    configuration = payload["data"]["extensions"]["org.openada"]["configuration"]
    assert configuration[0]["role"] == "spice-model-library"
    assert configuration[0]["sha256"] == composed.text_sha256

    raw_records = [
        record
        for record in payload["artifacts"]
        if record.get("role") == "simulation.result"
    ]
    assert len(raw_records) == 1
    raw = raw_records[0]

    binding = _analysis_binding(case["analysis"])
    extracted = extract_analysis_raw(
        raw["path"],
        backend="ngspice",
        analysis=binding,
        selected_variables=sorted(_native_vectors(case)),
        expected_bytes=int(raw["bytes"]),
        expected_sha256=str(raw["sha256"]),
        **_EXTRACTION_BOUNDS,
    )
    assert extracted.valid, (extracted.reason, extracted.metadata)

    axis = list(extracted.axis_values)
    magnitudes: dict[str, list[float]] = {}
    for signal in extracted.signals:
        if signal.imaginary_values is not None:
            magnitudes[signal.name] = [
                math.hypot(real, imaginary)
                for real, imaginary in zip(signal.real_values, signal.imaginary_values)
            ]
        else:
            magnitudes[signal.name] = list(signal.real_values)

    observed: dict[str, float] = {}
    for observation in case["observations"]:
        shape, payload_names = _parse_signal(observation["signal"])
        if shape == "minabs":
            first, second = payload_names
            values = [
                min(abs(a), abs(b))
                for a, b in zip(magnitudes[first], magnitudes[second])
            ]
        elif shape == "vdb":
            values = [20.0 * math.log10(value) for value in magnitudes[payload_names]]
        else:
            values = magnitudes[payload_names]
        observed[observation["id"]] = _evaluate_observation(observation, axis, values)
    return case, observed


@pytest.mark.parametrize(
    "case_path",
    CASE_PATHS,
    ids=[f"{path.parent.parent.name}-{path.stem}" for path in CASE_PATHS],
)
def test_golden_case_relations_hold_against_native_ngspice(case_path, tmp_path):
    case, observed = _run_case(case_path, tmp_path)

    assert case["relations"], "a golden case must assert at least one relation"
    for relation in case["relations"]:
        value = observed[relation["observation"]]
        if "expect_minus" in relation:
            value -= observed[relation["expect_minus"]]
        expected = float(relation["expect"])
        tolerance = float(relation.get("atol", 0.0)) + float(
            relation.get("rtol", 0.0)
        ) * abs(expected)
        assert tolerance > 0.0, relation
        assert abs(value - expected) <= tolerance, (
            f"{case['case_id']}/{relation['id']}: observed {value!r}, "
            f"expected {expected!r} within {tolerance!r} ({relation['derivation']})"
        )


def test_the_case_inventory_is_the_declared_golden_case_set():
    library = load_block_library("bhv-core")
    declared = {
        (block_id, case_id)
        for block_id, block in library.blocks.items()
        for case_id in block.contract["golden_cases"]
    }
    all_case_paths = sorted(
        (ROOT / "blocks" / "bhv-core" / "blocks").glob("*/cases/*.json")
    )
    on_disk = {(path.parent.parent.name, path.stem) for path in all_case_paths}
    assert on_disk == declared
