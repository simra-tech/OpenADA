"""The simulator's startup file, and what a log line is allowed to mean.

Three defects are pinned here, all three observed in live container jobs:

1. A binding exported ``PDK`` and ``PDK_ROOT`` and then let ngspice read an
   ambient ``.spiceinit``. IHP ships one, the workstation image installs it in
   the user's home, and it expands exactly those two variables - so every
   sky130A run preloaded *IHP's* PSP103 from *SkyWater's* tree, where it has
   never existed.
2. The resulting log lines were read as a generic native error and canonicalized
   to ``simulation.result.malformed`` - "the output is unreadable" - on a run
   that had converged over 181 points and written a valid 8947-byte raw file.
3. Nothing in the envelope ever said which numbers were reportable, so a number
   parsed out of a raw file by hand kept being written into answers as
   "Measured".
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

import pytest

from openada.discovery import DiscoveryManager
from openada.engines.spice import _scan_line
from openada.operations.simulate import (
    PDK_BINDING_EXTENSION,
    RETAINED_RESULT_NAME,
    SELECTION_TEMPLATE_NAME,
    _write_selection_template,
    simulate,
    simulate_legacy_native,
)
from openada.pdk_bindings import SKY130A, resolve_pdk_binding
from openada.pdk_collateral import collateral_references, inspect_deck_collateral
from openada.pdk_startup import (
    MANAGED_STARTUP_NAME,
    managed_startup_text,
    write_managed_startup,
)


NGSPICE = shutil.which("ngspice")


def _installed_root(binding) -> Path | None:
    variable = f"OPENADA_TEST_{binding.pdk_id.upper().replace('-', '_')}_ROOT"
    candidates: list[Path] = []
    explicit = os.environ.get(variable)
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/foss/pdks"))
    cache = Path.home() / ".cache" / "openada" / "pdks" / binding.pdk_id
    if cache.is_dir():
        candidates.extend(sorted(cache.iterdir()))
    probe, _ = binding.library_entries[0].resolve(binding.default_corner)
    for candidate in candidates:
        for root in (candidate / binding.pdk_id, candidate):
            if (root / probe).is_file():
                return candidate
    return None


SKY130_ROOT = _installed_root(SKY130A)

#: The startup file IHP actually ships, reduced to the four cards that matter.
#: ``$PDK_ROOT`` and ``$PDK`` are expanded by ngspice at run time, which is the
#: whole mechanism: the modules are IHP's, the tree is whichever one this run
#: bound.
IHP_SHIPPED_SPICEINIT = """\
* a custom spiceinit file for IHP-Open-PDK
osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/psp103.osdi'
osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/psp103_nqs.osdi'
osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/r3_cmc.osdi'
osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/mosvar.osdi'
"""

MODEL_FREE_DECK = """\
* common source, canonical roles only
V1 vdd 0 DC 1.8
V2 vin 0 DC 0.8 AC 1
M1 vout vin 0 0 nmos.core W=16u L=1u
M2 vdd vdd vout 0 nmos.core W=1u L=1u
.op
.end
"""


# --------------------------------------------------------------------------- #
# the startup file OpenADA writes
# --------------------------------------------------------------------------- #
def test_managed_startup_binds_nothing_and_names_the_binding():
    if SKY130_ROOT is None:
        pytest.skip("no installed sky130A PDK")
    resolved = resolve_pdk_binding(SKY130A.pdk_id, SKY130_ROOT)
    text = managed_startup_text(resolved)
    assert SKY130A.pdk_id in text
    assert resolved.corner in text
    # It must reach outside itself for nothing at all: the deck carries the
    # prelude, and a startup file that bound collateral would be exactly the
    # invisibility this exists to remove.
    assert collateral_references(text) == ()


@pytest.mark.skipif(SKY130_ROOT is None, reason="no installed sky130A PDK")
def test_write_managed_startup_creates_the_file(tmp_path):
    resolved = resolve_pdk_binding(SKY130A.pdk_id, SKY130_ROOT)
    path = write_managed_startup(tmp_path / "evidence", resolved)
    assert path.name == MANAGED_STARTUP_NAME
    assert path.is_file()


# --------------------------------------------------------------------------- #
# a startup file binds collateral exactly as a deck does
# --------------------------------------------------------------------------- #
def test_the_osdi_card_spelling_is_recognised():
    """IHP's file writes ``osdi``; a deck writes ``pre_osdi``. Same act."""

    references = collateral_references(IHP_SHIPPED_SPICEINIT)
    assert len(references) == 4
    assert {reference.card for reference in references} == {"pre_osdi"}
    assert references[0].raw_path.endswith("psp103.osdi")


def test_a_startup_file_naming_another_pdks_module_is_foreign():
    """The production shape, with the variables already expanded."""

    text = (
        "osdi '/foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi'\n"
    )
    codes = [finding.code for finding in inspect_deck_collateral(text)]
    assert "pdk.collateral.foreign" in codes
    message = next(
        finding.message
        for finding in inspect_deck_collateral(text)
        if finding.code == "pdk.collateral.foreign"
    )
    # Both PDKs named: the one whose tree was reached into, and the one whose
    # collateral it actually is.
    assert "sky130A" in message and "ihp-sg13g2" in message


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_an_explicit_init_file_is_held_to_the_deck_rules(tmp_path):
    """``--init-file`` is a caller's choice, so the same refusal applies."""

    init = tmp_path / "project.spiceinit"
    init.write_text(
        "osdi /foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi\n",
        encoding="utf-8",
    )
    deck = tmp_path / "d.spice"
    deck.write_text("* t\nV1 a 0 1\nR1 a 0 1k\n.op\n.end\n", encoding="utf-8")
    payload = simulate_legacy_native(
        deck,
        tmp_path / "out",
        discovery=DiscoveryManager(),
        execution_mode="control",
        init_file=init,
    )
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "pdk.collateral.foreign"
    assert str(init) in payload["diagnostics"][0]["message"]


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_unmanaged_collateral_still_permits_an_explicit_init_file(tmp_path):
    """The one documented escape keeps working; a script depends on it."""

    init = tmp_path / "project.spiceinit"
    init.write_text(
        "osdi /foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi\n",
        encoding="utf-8",
    )
    deck = tmp_path / "d.spice"
    deck.write_text("* t\nV1 a 0 1\nR1 a 0 1k\n.op\n.end\n", encoding="utf-8")
    payload = simulate_legacy_native(
        deck,
        tmp_path / "out",
        discovery=DiscoveryManager(),
        execution_mode="control",
        init_file=init,
        unmanaged_collateral=True,
    )
    assert payload["execution"]["status"] != "invalid_request"


# --------------------------------------------------------------------------- #
# a preload failure is a fact about collateral, not about the output
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line",
    [
        'Error opening osdi lib "/foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi": No such file or directory!',
        "Error: Library /foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi couldn't be loaded!",
    ],
)
def test_a_preload_failure_is_never_a_native_error(line):
    convergence, solver, native, measurement, analysis, collateral = _scan_line(line)
    assert native is None, "a missing model library is not a malformed result"
    assert collateral == line


def test_an_ordinary_deck_error_is_still_a_native_error():
    _, _, native, _, _, collateral = _scan_line(
        "Error on line 5 or its substitute:"
    )
    assert native is not None and collateral is None


# --------------------------------------------------------------------------- #
# live: the container failure, reproduced and then absent
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(SKY130_ROOT is None, reason="no installed sky130A PDK")
def test_an_ambient_spiceinit_cannot_reach_a_bound_run(tmp_path, monkeypatch):
    """The whole defect, end to end.

    IHP's shipped startup file is installed where ngspice will find it, the run
    binds sky130A, and the run must still pass - because OpenADA writes the
    startup file and ngspice therefore reads no other one.
    """

    home = tmp_path / "home"
    home.mkdir()
    (home / ".spiceinit").write_text(IHP_SHIPPED_SPICEINIT, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    deck = tmp_path / "cs.spice"
    deck.write_text(MODEL_FREE_DECK, encoding="utf-8")
    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=SKY130A.pdk_id,
        pdk_root=SKY130_ROOT,
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    codes = {entry["code"] for entry in payload["diagnostics"]}
    assert "simulation.result.malformed" not in codes
    assert "simulation.collateral.unloadable" not in codes
    assert PDK_BINDING_EXTENSION in payload["data"]["extensions"]
    assert (tmp_path / "evidence" / "cs" / MANAGED_STARTUP_NAME).is_file()


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_a_preload_failure_beside_valid_evidence_is_a_warning(tmp_path, monkeypatch):
    """The native path still inherits the ambient file - and must survive it.

    A model-free RC deck needs no Verilog-A module at all, so a failed preload
    changes nothing about the answer. Before, it made the run ``unknown`` and
    called the perfectly readable output malformed.
    """

    home = tmp_path / "home"
    home.mkdir()
    (home / ".spiceinit").write_text(
        "osdi '/nonexistent/psp103.osdi'\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PDK_ROOT", str(tmp_path))
    monkeypatch.setenv("PDK", "sky130A")

    deck = tmp_path / "rc.spice"
    deck.write_text(
        "* rc\nV1 a 0 1\nR1 a b 1k\nC1 b 0 1n\n.op\n"
        ".control\nrun\nwrite rc.raw\n.endc\n.end\n",
        encoding="utf-8",
    )
    payload = simulate_legacy_native(
        deck,
        tmp_path / "out",
        discovery=DiscoveryManager(),
        execution_mode="control",
        expected_outputs=[__import__("openada").engines.spice.NgspiceOutput(kind="raw", path="rc.raw")],
    )
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    unloadable = [
        entry
        for entry in payload["diagnostics"]
        if entry["code"] == "simulation.collateral.unloadable"
    ]
    assert len(unloadable) == 1
    assert unloadable[0]["severity"] == "warning"
    assert "psp103.osdi" in unloadable[0]["message"]


# --------------------------------------------------------------------------- #
# what may be reported as measured
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(SKY130_ROOT is None, reason="no installed sky130A PDK")
def test_a_completed_run_hands_back_a_runnable_typed_chain(tmp_path):
    """The typed chain must be the *shorter* path, not the longer one.

    Every ingredient ``extract`` needs is produced by the run that would
    otherwise have been hand-parsed: the envelope as a file, the raw's own
    vector names, and a selection whose units are already right.
    """

    deck = tmp_path / "cs.spice"
    deck.write_text(MODEL_FREE_DECK, encoding="utf-8")
    destination = tmp_path / "evidence"
    payload = simulate(
        deck,
        destination,
        discovery=DiscoveryManager(),
        pdk=SKY130A.pdk_id,
        pdk_root=SKY130_ROOT,
    )
    assert payload["engineering"]["status"] == "pass"

    claim = payload["diagnostics"][0]
    assert claim["code"] == "claim.measurement.typed_chain"
    assert "parsed out of the raw file by hand is not one" in claim["message"]

    retained = destination / RETAINED_RESULT_NAME
    selection = destination / SELECTION_TEMPLATE_NAME
    assert retained.is_file() and selection.is_file()
    assert str(retained) in claim["message"]
    assert str(selection) in claim["message"]

    # The chain the diagnostic names actually runs.
    import json

    from openada.operations.result_series_extract import extract_result_series

    raw = next(
        record["path"]
        for record in payload["artifacts"]
        if record.get("role") == "simulation.result"
    )
    request = json.loads(selection.read_text(encoding="utf-8"))
    series = extract_result_series(
        json.loads(retained.read_text(encoding="utf-8")),
        Path(raw),
        request["selectors"],
        conditions=request["conditions"],
    )
    assert series["execution"]["status"] == "completed"
    assert series["engineering"]["status"] == "pass"


#: The testbench half of a live job that reported a 280 MOhm cascode output
#: impedance. ``V_OUT_STIM`` carries a DC value and no AC magnitude, so the AC
#: sweep drives nothing; the extracted series is zero at all 181 points, and the
#: run is still a legitimate ``pass`` - the tool ran and the evidence is
#: structurally valid. The question was empty, which is a different failure and
#: had no name.
UNDRIVEN_AC_DECK = """\
* cascode mirror testbench, as published
V_GND VSS 0 DC 0
I_REF VDD IREF DC 10u
V_DD VDD VSS DC 1.5
V_OUT_STIM VOUT VSS DC 1.5
M_REF IREF IREF VSS VSS nmos.core W=5u L=500n
M_OUT VOUT IREF VSS VSS nmos.core W=5u L=500n
.AC DEC 20 1 1G
.END
"""


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(SKY130_ROOT is None, reason="no installed sky130A PDK")
def test_an_ac_sweep_with_nothing_driving_it_is_named(tmp_path):
    deck = tmp_path / "undriven.spice"
    deck.write_text(UNDRIVEN_AC_DECK, encoding="utf-8")
    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=SKY130A.pdk_id,
        pdk_root=SKY130_ROOT,
    )
    # Still a pass: execution and engineering are about the run, not the
    # experiment. The warning is what makes the empty question visible.
    assert payload["engineering"]["status"] == "pass"
    absent = [
        entry
        for entry in payload["diagnostics"]
        if entry["code"] == "simulation.stimulus.absent"
    ]
    assert len(absent) == 1
    assert absent[0]["severity"] == "warning"


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(SKY130_ROOT is None, reason="no installed sky130A PDK")
def test_a_driven_ac_sweep_is_not_accused(tmp_path):
    deck = tmp_path / "driven.spice"
    deck.write_text(
        MODEL_FREE_DECK.replace(".op\n", ".ac dec 20 1 1e9\n"), encoding="utf-8"
    )
    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=SKY130A.pdk_id,
        pdk_root=SKY130_ROOT,
    )
    codes = {entry["code"] for entry in payload["diagnostics"]}
    assert "simulation.stimulus.absent" not in codes


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_an_operating_point_has_no_axis_to_exclude(tmp_path):
    """Every other analysis puts its axis first; an OP has none.

    Dropping the first vector unconditionally silently removed a real signal
    from the template and called it the axis in the diagnostic.
    """

    import json

    deck = tmp_path / "op.spice"
    deck.write_text(
        "* op\nV1 a 0 1\nR1 a b 1k\nR2 b 0 2k\n.op\n.end\n", encoding="utf-8"
    )
    destination = tmp_path / "evidence"
    payload = simulate(
        deck, destination, discovery=DiscoveryManager(), backend="ngspice"
    )
    assert payload["engineering"]["status"] == "pass"
    selection = json.loads(
        (destination / SELECTION_TEMPLATE_NAME).read_text(encoding="utf-8")
    )
    names = [entry["native_name"] for entry in selection["selectors"]]
    assert names == ["v(a)", "v(b)", "i(v1)"]
    assert [entry["unit"] for entry in selection["selectors"]] == ["V", "V", "A"]
    assert "no axis" in payload["diagnostics"][0]["message"]


def test_a_repeated_vector_name_is_selected_once(tmp_path):
    """ngspice repeats a saved vector; ``extract`` refuses a repeated selector.

    A published testbench in a real harness run produced an OP raw declaring
    ``v(vin)`` twice, and a template reproducing that duplicate is refused on
    its first use - which is precisely the friction the template removes.
    """

    import json

    path = _write_selection_template(tmp_path, ("v(vin)", "v(vin)", "v(vout)"))
    assert path is not None
    selection = json.loads(path.read_text(encoding="utf-8"))
    names = [entry["native_name"] for entry in selection["selectors"]]
    assert names == ["v(vin)", "v(vout)"]
    outputs = [entry["output_name"] for entry in selection["selectors"]]
    assert len(outputs) == len(set(outputs))


def test_a_selection_template_names_only_vectors_it_can_type(tmp_path):
    assert _write_selection_template(tmp_path, ("frequency", "sweep")) is None


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_a_run_that_proves_nothing_forbids_the_word_measured(tmp_path):
    deck = tmp_path / "broken.spice"
    deck.write_text(
        "* no models anywhere\nV1 d 0 1\nM1 d g 0 0 not_a_model W=1u L=1u\n.op\n.end\n",
        encoding="utf-8",
    )
    payload = simulate(
        deck,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        backend="ngspice",
    )
    assert payload["engineering"]["status"] != "pass"
    claim = payload["diagnostics"][0]
    assert claim["code"] == "claim.measurement.unsupported"
    assert claim["severity"] == "error"
    assert "may be reported as measured" in claim["message"]
