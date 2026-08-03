"""Cosim golden-case replay: the mixed-signal (XSPICE + d_cosim) backend must
reproduce the SAME backend-neutral golden cases the native backend answers.

Each event block's ``cases/*.json`` document is replayed exactly as
``test_blocks_native.py`` replays it -- same deck builder, same shared
circuit.simulate operation, same bounded raw extraction, same declared
relation bands -- but the model prelude is the compiled cosim composition
(``compose_blocks_cosim``) instead of the native one, pinned by digest and
with its generated ``d_cosim`` cards named through the sanctioned
executable-model allowance. A cosim backend that only "mostly" matches the
native physics fails here, in the declared bands, not in production.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openada import cosim_compile as cc  # noqa: E402
from openada.block_library import load_block_library  # noqa: E402
from openada.discovery import DiscoveryManager  # noqa: E402
from openada.engines.ngspice_outputs import extract_analysis_raw  # noqa: E402
from openada.operations.simulate import simulate  # noqa: E402

import test_blocks_native as native_harness  # noqa: E402

NGSPICE = shutil.which("ngspice")
VERILATOR = shutil.which("verilator_bin") or shutil.which("verilator")
GXX = shutil.which("g++")


def _shim_available() -> bool:
    if NGSPICE is None:
        return False
    try:
        cc.resolve_cosim_shim_dir(ngspice_bin=NGSPICE)
    except cc.CosimCompileError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    NGSPICE is None or VERILATOR is None or GXX is None or not _shim_available(),
    reason="cosim goldens need ngspice (with shim sources), Verilator, and g++",
)

#: Only the event/clocked blocks carry a cosim backend; opamp_1p is Step-2
#: (OSDI) territory and declares none.
COSIM_BLOCKS = ("comparator_clocked", "sw_bbm_pair")
CASE_PATHS = sorted(
    path
    for block in COSIM_BLOCKS
    for path in (ROOT / "blocks" / "bhv-core" / "blocks" / block / "cases").glob(
        "*.json"
    )
)

_COMPOSITIONS: dict[str, cc.CosimComposition] = {}


def _composition(block_id: str, tmp_path_factory) -> cc.CosimComposition:
    """Compile each block once per session; the compile is ~10 s of Verilator."""

    composition = _COMPOSITIONS.get(block_id)
    if composition is None:
        library = load_block_library("bhv-core")
        work_dir = tmp_path_factory.mktemp(f"cosim-{block_id}")
        composition = cc.compose_blocks_cosim(library, (block_id,), work_dir)
        _COMPOSITIONS[block_id] = composition
    return composition


@pytest.mark.parametrize(
    "case_path", CASE_PATHS, ids=[p.parent.parent.name + "-" + p.stem for p in CASE_PATHS]
)
def test_cosim_backend_reproduces_golden_case(case_path, tmp_path, tmp_path_factory):
    import json
    import math

    block_id = case_path.parent.parent.name
    case = json.loads(case_path.read_text(encoding="utf-8"))
    composition = _composition(block_id, tmp_path_factory)

    models_file = tmp_path / "behavioral-blocks-cosim.model.spice"
    models_file.write_text(composition.text, encoding="utf-8")
    deck = tmp_path / f"{case['case_id']}.cir"
    deck.write_text(native_harness._build_deck(case), encoding="utf-8")

    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        backend="ngspice",
        models_file=models_file,
        expected_models_sha256=composition.text_sha256,
        permitted_executable_models=composition.model_names,
    )
    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    configuration = payload["data"]["extensions"]["org.openada"]["configuration"]
    assert configuration[0]["role"] == "spice-model-library"
    assert configuration[0]["sha256"] == composition.text_sha256

    raw_records = [
        record
        for record in payload["artifacts"]
        if record.get("role") == "simulation.result"
    ]
    assert len(raw_records) == 1
    raw = raw_records[0]

    binding = native_harness._analysis_binding(case["analysis"])
    extracted = extract_analysis_raw(
        raw["path"],
        backend="ngspice",
        analysis=binding,
        selected_variables=sorted(native_harness._native_vectors(case)),
        expected_bytes=int(raw["bytes"]),
        expected_sha256=str(raw["sha256"]),
        **native_harness._EXTRACTION_BOUNDS,
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
        shape, payload_names = native_harness._parse_signal(observation["signal"])
        if shape == "minabs":
            first, second = payload_names
            values = [
                min(abs(a), abs(b))
                for a, b in zip(magnitudes[first], magnitudes[second])
            ]
        elif shape == "vdb":
            values = [
                20.0 * math.log10(value) for value in magnitudes[payload_names]
            ]
        else:
            values = magnitudes[payload_names]
        observed[observation["id"]] = native_harness._evaluate_observation(
            observation, axis, values
        )

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
            f"{case['case_id']}/{relation['id']}: cosim observed {value!r}, "
            f"expected {expected!r} within {tolerance!r} "
            f"({relation['derivation']})"
        )
