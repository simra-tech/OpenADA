from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator

from openada.discovery import DiscoveryManager
from openada.operations.testbench_simulate import simulate_testbench
from openada.pdk_bindings import (
    IHP_SG13G2,
    REGISTRY,
    SKY130A,
    PdkBindingError,
    available_pdk_ids,
    bind_deck,
    resolve_pdk_binding,
    rewrite_mos_card,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "conformance" / "testbench-simulate-v0alpha1" / "fixtures"
NMOS_SOURCE = FIXTURES / "nmos-common-source"
IHP_INVERTER = FIXTURES / "ihp-sg13g2-inverter"
PROFILE_PATH = ROOT / "profiles" / "testbench.simulate-v1alpha1.json"
RESULT_SCHEMA_PATH = ROOT / "schemas" / "result-v0alpha1.schema.json"

PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
RESULT_VALIDATOR = Draft202012Validator(
    json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
)
DATA_VALIDATOR = Draft202012Validator(PROFILE["normalized_result"]["data_schema"])

NGSPICE = shutil.which("ngspice")


def _assert_contract(payload: dict) -> None:
    assert not list(RESULT_VALIDATOR.iter_errors(payload))
    assert not list(DATA_VALIDATOR.iter_errors(payload["data"]))

#: Simra emits exactly this shape for a four-terminal MOS
#: (``plugins/schematic/compiler/netlist_v2.py``).
SIMRA_MOS_CARD = "M_PD Y A VSS VSS sg13_lv_nmos W=2u L=130n M=1 NF=1"


def _fake_pdk(tmp_path: Path, binding=IHP_SG13G2) -> Path:
    """Build a directory tree with the file layout one binding profile expects."""

    root = tmp_path / "pdks" / binding.pdk_id
    library = root / binding.library_relative_path
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text(f".LIB {binding.default_corner}\n.ENDL\n", encoding="utf-8")
    for relative in binding.osdi_relative_paths:
        module = root / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes(b"\x7fELF-not-a-real-osdi-module")
    if binding.identity_relative_path:
        (root / binding.identity_relative_path).write_text("abc123\n", encoding="utf-8")
    return tmp_path / "pdks"


def _resolved(tmp_path: Path, binding=IHP_SG13G2, corner: str | None = None):
    return resolve_pdk_binding(binding.pdk_id, _fake_pdk(tmp_path, binding), corner=corner)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_every_registered_binding_declares_its_default_corner():
    assert available_pdk_ids() == tuple(sorted(REGISTRY))
    for binding in REGISTRY.values():
        assert binding.default_corner in binding.corners
        assert binding.device_prefix in {"x", "m"}
        # Every Simra-emitted parameter must have a declared spelling, otherwise
        # it is silently dropped from the deck.
        assert set(binding.parameter_names) == {"w", "l", "m", "nf"}


# --------------------------------------------------------------------------- #
# device rewriting
# --------------------------------------------------------------------------- #
def test_subckt_pdk_prefixes_the_instance_and_renames_the_finger_count(tmp_path):
    resolved = _resolved(tmp_path)
    line, rewritten = rewrite_mos_card(SIMRA_MOS_CARD, resolved)
    assert rewritten is True
    assert line == "xM_PD Y A VSS VSS sg13_lv_nmos w=2u l=130n m=1 ng=1"


def test_prefix_is_prepended_so_it_cannot_collide_with_a_subcircuit_instance(tmp_path):
    resolved = _resolved(tmp_path)
    # Simra also emits ``X_DUT`` for subcircuit instances; substituting the
    # prefix of ``M_DUT`` would collide with it.
    line, _ = rewrite_mos_card("M_DUT Y A VSS VSS sg13_lv_nmos W=1u L=1u M=1 NF=1", resolved)
    assert line.split()[0] == "xM_DUT"


def test_model_card_pdk_keeps_the_emitted_prefix_and_spelling(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    line, rewritten = rewrite_mos_card(
        "M_PD Y A VSS VSS sky130_fd_pr__nfet_01v8 W=2u L=150n M=1 NF=1", resolved
    )
    assert rewritten is True
    assert line == "M_PD Y A VSS VSS sky130_fd_pr__nfet_01v8 w=2u l=150n m=1 nf=1"


@pytest.mark.parametrize(
    "line",
    [
        ".SUBCKT Inverter A Y VDD VSS\n",
        "* a comment\n",
        "V_DD VDD VSS DC 1.2\n",
        "X_DUT VIN VOUT VDD VSS Inverter\n",
        ".TRAN 0.1p 200p\n",
    ],
)
def test_non_mos_lines_are_returned_unchanged(tmp_path, line):
    resolved = _resolved(tmp_path)
    assert rewrite_mos_card(line, resolved) == (line, False)


def test_line_endings_survive_a_rewrite(tmp_path):
    resolved = _resolved(tmp_path)
    line, rewritten = rewrite_mos_card(SIMRA_MOS_CARD + "\n", resolved)
    assert rewritten is True
    assert line.endswith("\n")


def test_a_parameter_the_pdk_does_not_declare_is_dropped(tmp_path):
    resolved = _resolved(tmp_path)
    # ngspice reports "unknown parameter (nf)" as a hard error, so an unmapped
    # key must never be passed through.
    line, _ = rewrite_mos_card(
        "M_PD Y A VSS VSS sg13_lv_nmos W=2u L=130n M=1 NF=1 AD=1p", resolved
    )
    assert "ad=" not in line.lower()


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_unknown_pdk_is_refused(tmp_path):
    with pytest.raises(PdkBindingError) as excinfo:
        resolve_pdk_binding("not-a-pdk", tmp_path)
    assert excinfo.value.code == "pdk.unknown"


def test_relative_root_is_refused():
    with pytest.raises(PdkBindingError) as excinfo:
        resolve_pdk_binding(IHP_SG13G2.pdk_id, Path("relative/pdks"))
    assert excinfo.value.code == "pdk.root.invalid"


def test_missing_corner_library_is_refused(tmp_path):
    root = tmp_path / "pdks" / IHP_SG13G2.pdk_id
    root.mkdir(parents=True)
    with pytest.raises(PdkBindingError) as excinfo:
        resolve_pdk_binding(IHP_SG13G2.pdk_id, tmp_path / "pdks")
    assert excinfo.value.code == "pdk.library.missing"


def test_missing_osdi_module_is_refused(tmp_path):
    root = _fake_pdk(tmp_path)
    (root / IHP_SG13G2.pdk_id / IHP_SG13G2.osdi_relative_paths[0]).unlink()
    with pytest.raises(PdkBindingError) as excinfo:
        resolve_pdk_binding(IHP_SG13G2.pdk_id, root)
    assert excinfo.value.code == "pdk.osdi.missing"


def test_undeclared_corner_is_refused(tmp_path):
    with pytest.raises(PdkBindingError) as excinfo:
        _resolved(tmp_path, corner="mos_not_a_corner")
    assert excinfo.value.code == "pdk.corner.unknown"


def test_root_may_name_the_pdk_tree_itself(tmp_path):
    root = _fake_pdk(tmp_path)
    direct = resolve_pdk_binding(IHP_SG13G2.pdk_id, root / IHP_SG13G2.pdk_id)
    parent = resolve_pdk_binding(IHP_SG13G2.pdk_id, root)
    assert direct.root == parent.root


def test_every_referenced_pdk_file_is_content_bound(tmp_path):
    resolved = _resolved(tmp_path)
    roles = {record["role"] for record in resolved.input_records}
    assert roles == {"pdk.corner-library", "pdk.osdi-module", "pdk.identity"}
    for record in resolved.input_records:
        assert len(record["sha256"]) == 64


# --------------------------------------------------------------------------- #
# deck binding
# --------------------------------------------------------------------------- #
def test_bound_deck_orders_title_then_osdi_then_library(tmp_path):
    resolved = _resolved(tmp_path)
    deck = "* title\n.TITLE T\nM_PD Y A VSS VSS sg13_lv_nmos W=2u L=1u M=1 NF=1\n.END\n"
    text, facts = bind_deck(deck, resolved)
    lines = text.splitlines()
    assert lines[0] == "* title"
    assert lines[1] == ".control"
    assert lines[2].startswith("pre_osdi ")
    assert ".endc" in lines[:6]
    library_index = next(i for i, line in enumerate(lines) if line.startswith(".lib "))
    assert lines[library_index].endswith(f" {resolved.corner}")
    assert facts["rewritten_device_count"] == 1


def test_library_is_bound_with_the_two_argument_form(tmp_path):
    # A bare ``.include`` of a corner library fails with
    # "unimplemented dot command '.lib'" because no section is selected.
    resolved = _resolved(tmp_path)
    text, _ = bind_deck("* t\n.END\n", resolved)
    library_line = next(line for line in text.splitlines() if line.startswith(".lib "))
    assert len(library_line.split()) == 3


def test_a_model_card_pdk_emits_no_osdi_preload(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    text, _ = bind_deck("* t\n.END\n", resolved)
    assert "pre_osdi" not in text
    assert ".control" not in text


def test_unresolved_publisher_placeholders_are_refused(tmp_path):
    resolved = _resolved(tmp_path)
    deck = "* t\nM_PD Y A VSS VSS sg13_lv_nmos W=2u L=1u M=1 NF={SIMRA_UNRESOLVED_M_PD_NF}\n.END\n"
    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck(deck, resolved)
    assert excinfo.value.code == "pdk.deck.unresolved"


def test_a_requested_raw_output_closes_a_control_block_before_end(tmp_path):
    resolved = _resolved(tmp_path)
    text, facts = bind_deck(
        "* t\n.TRAN 1p 10p\n.END\n", resolved, raw_name="a.raw", saved_nets=("VOUT",)
    )
    lines = [line.strip() for line in text.splitlines()]
    assert "run" in lines
    assert "write a.raw v(VOUT)" in lines
    assert lines.index(".endc", lines.index("run")) < lines.index(".END")
    assert facts["raw_output"] == "a.raw"


def test_an_unbounded_raw_name_is_refused(tmp_path):
    resolved = _resolved(tmp_path)
    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck("* t\n.END\n", resolved, raw_name="../escape.raw")
    assert excinfo.value.code == "pdk.raw_name.invalid"


# --------------------------------------------------------------------------- #
# operation wiring
# --------------------------------------------------------------------------- #
def test_a_pdk_and_a_model_file_together_are_refused(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=tmp_path,
        models_file=NMOS_SOURCE / "nmos_lv.models",
    )
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "testbench.models.ambiguous"


def test_a_pdk_without_a_root_is_refused(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
    )
    assert payload["diagnostics"][0]["code"] == "pdk.root.required"


def test_a_corner_without_a_pdk_is_refused(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        corner="mos_tt",
    )
    assert payload["diagnostics"][0]["code"] == "pdk.corner.unbound"


def test_the_missing_models_hint_now_names_the_pdk_path(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
    )
    assert payload["diagnostics"][0]["code"] == "testbench.models.required"
    assert "--pdk" in payload["diagnostics"][0]["hint"]


# --------------------------------------------------------------------------- #
# live run against a real installed PDK
# --------------------------------------------------------------------------- #
def _installed_ihp_root() -> Path | None:
    """Return a root holding a real IHP SG13G2 tree, or None when absent."""

    explicit = os.environ.get("OPENADA_TEST_PDK_ROOT")
    candidates = [Path(explicit)] if explicit else []
    candidates.append(Path("/foss/pdks"))
    cache = Path.home() / ".cache" / "openada" / "pdks" / IHP_SG13G2.pdk_id
    if cache.is_dir():
        candidates.extend(sorted(cache.iterdir()))
    for candidate in candidates:
        library = (
            candidate / IHP_SG13G2.pdk_id / IHP_SG13G2.library_relative_path
        )
        if library.is_file():
            return candidate
    return None


IHP_ROOT = _installed_ihp_root()


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(IHP_ROOT is None, reason="no installed IHP SG13G2 PDK")
def test_a_published_ihp_testbench_simulates_against_the_real_pdk(tmp_path):
    """The end-to-end claim: a model-free published artifact simulates.

    ``IHP_INVERTER`` is a real Simra v2 bundle published by an agent job. Its
    deck names ``sg13_lv_nmos``/``sg13_lv_pmos`` and carries no model
    collateral, which is exactly the shape that could not be simulated before
    a binding profile existed.
    """

    payload = simulate_testbench(
        IHP_INVERTER / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=IHP_ROOT,
    )
    _assert_contract(payload)

    binding = payload["data"]["extensions"]["org.openada.pdk_binding"]
    assert binding["pdk_id"] == IHP_SG13G2.pdk_id
    assert binding["corner"] == IHP_SG13G2.default_corner
    assert binding["device_prefix"] == "x"
    assert binding["rewritten_device_count"] == 2

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"

    deck_text = (
        tmp_path / "evidence" / "decks" / "analysis-01-tran.spice"
    ).read_text(encoding="utf-8")
    assert f" {IHP_SG13G2.default_corner}\n" in deck_text
    assert "pre_osdi " in deck_text
    assert "xM_PD" in deck_text and "ng=1" in deck_text
    # The Simra-emitted spelling must not survive the binding.
    assert "NF=" not in deck_text

    raw = tmp_path / "evidence" / "decks" / "analysis-01-tran.raw"
    assert raw.is_file() and raw.stat().st_size > 0


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(IHP_ROOT is None, reason="no installed IHP SG13G2 PDK")
def test_an_undeclared_corner_never_reaches_the_simulator(tmp_path):
    payload = simulate_testbench(
        IHP_INVERTER / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=IHP_ROOT,
        corner="mos_typical",
    )
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "pdk.corner.unknown"
