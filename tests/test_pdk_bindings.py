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
    FREEPDK45,
    GF180MCUD,
    IHP_SG13G2,
    NANGATE45,
    REGISTRY,
    SKY130A,
    PdkBindingError,
    available_pdk_ids,
    bind_deck,
    device_role_index,
    parse_spice_number,
    resolve_pdk_binding,
    rewrite_mos_card,
    simulatable_pdk_ids,
    translate_model,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "conformance" / "testbench-simulate-v0alpha1" / "fixtures"
NMOS_SOURCE = FIXTURES / "nmos-common-source"
IHP_INVERTER = FIXTURES / "ihp-sg13g2-inverter"
PORTABLE_INVERTER = FIXTURES / "portable-inverter"
PROFILE_PATH = ROOT / "profiles" / "testbench.simulate-v1alpha1.json"
RESULT_SCHEMA_PATH = ROOT / "schemas" / "result-v0alpha1.schema.json"

PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
RESULT_VALIDATOR = Draft202012Validator(
    json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
)
DATA_VALIDATOR = Draft202012Validator(PROFILE["normalized_result"]["data_schema"])

NGSPICE = shutil.which("ngspice")

ANALOG_BINDINGS = [binding for binding in REGISTRY.values() if binding.analog]


def _assert_contract(payload: dict) -> None:
    assert not list(RESULT_VALIDATOR.iter_errors(payload))
    assert not list(DATA_VALIDATOR.iter_errors(payload["data"]))


#: Simra emits exactly this shape for a four-terminal MOS
#: (``plugins/schematic/compiler/netlist_v2.py``): an ``M`` card, SI geometry,
#: uppercase keys. Everything else is a property of the target PDK.
SIMRA_MOS_CARD = "M_PD Y A VSS VSS sg13_lv_nmos W=2u L=130n M=1 NF=1"
#: The same card written against the technology-independent role vocabulary.
CANONICAL_MOS_CARD = "M_PD Y A VSS VSS nmos.core W=2u L=500n M=1 NF=1"


def _fake_pdk(tmp_path: Path, binding=IHP_SG13G2) -> Path:
    """Build a directory tree with the file layout one binding profile expects."""

    root = tmp_path / "pdks" / binding.pdk_id
    for corner in binding.corners:
        for entry in binding.library_entries:
            relative, section = entry.resolve(corner)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f".LIB {section}\n.ENDL\n" if section else "* model cards\n",
                encoding="utf-8",
            )
    for relative in binding.osdi_relative_paths:
        module = root / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes(b"\x7fELF-not-a-real-osdi-module")
    if binding.identity_relative_path:
        identity = root / binding.identity_relative_path
        identity.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text("abc123\n", encoding="utf-8")
    return tmp_path / "pdks"


def _resolved(tmp_path: Path, binding=IHP_SG13G2, corner: str | None = None):
    return resolve_pdk_binding(
        binding.pdk_id, _fake_pdk(tmp_path, binding), corner=corner
    )


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_every_registered_binding_declares_its_default_corner():
    assert available_pdk_ids() == tuple(sorted(REGISTRY))
    for binding in ANALOG_BINDINGS:
        assert binding.default_corner in binding.corners
        assert binding.device_prefix in {"x", "m"}
        # Every Simra-emitted parameter must have a declared spelling, otherwise
        # it is silently dropped from the deck.
        assert set(binding.parameter_names) == {"w", "l", "m", "nf"}
        assert binding.library_entries
        assert {entry.form for entry in binding.library_entries} <= {"lib", "include"}


def test_every_analog_binding_covers_the_core_device_roles():
    # A deck authored against the canonical vocabulary must bind everywhere; the
    # core nmos/pmos pair is the floor for that promise.
    for binding in ANALOG_BINDINGS:
        assert {"nmos.core", "pmos.core"} <= set(binding.device_models)


def test_the_role_index_is_derived_from_the_profiles():
    index = device_role_index()
    assert index["sg13_lv_nmos"] == "nmos.core"
    assert index["sky130_fd_pr__pfet_01v8"] == "pmos.core"
    assert index["nfet_03v3"] == "nmos.core"
    assert index["nmos_vtg"] == "nmos.core"


def test_a_digital_platform_is_registered_but_refused_with_its_reason(tmp_path):
    assert NANGATE45.pdk_id in available_pdk_ids()
    assert NANGATE45.pdk_id not in simulatable_pdk_ids()
    with pytest.raises(PdkBindingError) as excinfo:
        resolve_pdk_binding(NANGATE45.pdk_id, tmp_path)
    # "unknown PDK" would send an agent looking for a different spelling; the
    # platform is installed and simply has no transistor models.
    assert excinfo.value.code == "pdk.analog.unsupported"
    assert "no transistor models" in excinfo.value.message


# --------------------------------------------------------------------------- #
# device rewriting
# --------------------------------------------------------------------------- #
def test_subckt_pdk_prefixes_the_instance_and_renames_the_finger_count(tmp_path):
    resolved = _resolved(tmp_path)
    card = rewrite_mos_card(SIMRA_MOS_CARD, resolved)
    assert card.rewritten is True
    assert card.text == "xM_PD Y A VSS VSS sg13_lv_nmos w=2u l=130n m=1 ng=1"


def test_prefix_is_prepended_so_it_cannot_collide_with_a_subcircuit_instance(tmp_path):
    resolved = _resolved(tmp_path)
    # Simra also emits ``X_DUT`` for subcircuit instances; substituting the
    # prefix of ``M_DUT`` would collide with it.
    card = rewrite_mos_card(
        "M_DUT Y A VSS VSS sg13_lv_nmos W=1u L=1u M=1 NF=1", resolved
    )
    assert card.text.split()[0] == "xM_DUT"


def test_a_model_card_pdk_keeps_the_emitted_prefix(tmp_path):
    resolved = _resolved(tmp_path, FREEPDK45)
    card = rewrite_mos_card(CANONICAL_MOS_CARD, resolved)
    assert card.rewritten is True
    assert card.text == "M_PD Y A VSS VSS NMOS_VTG w=2u l=500n m=1 nf=1"


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
    card = rewrite_mos_card(line, resolved)
    assert (card.text, card.rewritten) == (line, False)


def test_line_endings_survive_a_rewrite(tmp_path):
    resolved = _resolved(tmp_path)
    card = rewrite_mos_card(SIMRA_MOS_CARD + "\n", resolved)
    assert card.rewritten is True
    assert card.text.endswith("\n")


def test_a_parameter_the_pdk_does_not_declare_is_dropped(tmp_path):
    resolved = _resolved(tmp_path)
    # ngspice reports "unknown parameter (nf)" as a hard error, so an unmapped
    # key must never be passed through.
    card = rewrite_mos_card(
        "M_PD Y A VSS VSS sg13_lv_nmos W=2u L=130n M=1 NF=1 AD=1p", resolved
    )
    assert "ad=" not in card.text.lower()


# --------------------------------------------------------------------------- #
# geometry unit conventions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2u", "0.000002"),
        ("130n", "1.3E-7"),
        ("1.5e-7", "1.5E-7"),
        ("0.5", "0.5"),
        ("1meg", "1000000"),
        ("1uF", "0.000001"),
    ],
)
def test_spice_numbers_parse_to_their_si_value(text, expected):
    from decimal import Decimal

    assert parse_spice_number(text) == Decimal(expected)


def test_a_pdk_that_sets_scale_receives_geometry_in_its_own_units(tmp_path):
    """sky130's own collateral installs ``.option scale=1.0u``.

    ``libs.tech/ngspice/all.spice:2``, reached from ``corners/<corner>.spice``.
    An SI-valued card is therefore scaled a second time, lands outside every
    model bin, and ngspice rejects it with "could not find a valid modelname".
    Rescaling here is what makes one canonical deck runnable on this PDK.
    """

    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_mos_card(CANONICAL_MOS_CARD, resolved)
    assert card.text == "xM_PD Y A VSS VSS sky130_fd_pr__nfet_01v8 w=2 l=0.5 m=1 nf=1"


def test_a_scaling_pdk_states_its_convention_in_the_bound_deck(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    text, facts = bind_deck("* t\n.END\n", resolved)
    assert ".option scale=1e-6\n" in text
    assert facts["geometry_scale"] == "1e-6"


def test_an_si_pdk_leaves_the_geometry_untouched(tmp_path):
    resolved = _resolved(tmp_path, GF180MCUD)
    text, _ = bind_deck("* t\n.END\n", resolved)
    assert ".option scale" not in text


def test_geometry_outside_the_binning_envelope_is_refused_with_the_range(tmp_path):
    """The failure that cost two live agent jobs, now typed.

    A 130 nm channel is legal in IHP SG13G2 and below sky130's 150 nm floor.
    ngspice reports only "could not find a valid modelname", which reads as a
    missing model rather than an out-of-range device.
    """

    resolved = _resolved(tmp_path, SKY130A)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_mos_card(SIMRA_MOS_CARD, resolved)
    assert excinfo.value.code == "pdk.device.geometry_out_of_range"
    assert "1.5e-07" in excinfo.value.message


def test_a_pdk_without_binning_enforces_no_envelope(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    # PSP103 is continuous, so a 130 nm channel binds without complaint.
    assert rewrite_mos_card(SIMRA_MOS_CARD, resolved).rewritten is True


# --------------------------------------------------------------------------- #
# model vocabulary
# --------------------------------------------------------------------------- #
def test_a_canonical_role_binds_to_every_analog_pdk():
    for binding in ANALOG_BINDINGS:
        model, role, translated = translate_model("nmos.core", binding)
        assert role == "nmos.core"
        assert translated is True
        assert model == binding.device_models["nmos.core"]


def test_a_foreign_pdks_model_name_is_translated_through_its_role(tmp_path):
    # The deck was authored against IHP. Binding it to gf180mcu must not require
    # the author to have known either vocabulary.
    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_mos_card(
        "M_PD Y A VSS VSS sg13_lv_nmos W=2u L=500n M=1 NF=1", resolved
    )
    assert card.target_model == "nfet_03v3"
    assert card.role == "nmos.core"
    assert card.model_translated is True


def test_a_native_model_name_passes_through_untranslated(tmp_path):
    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_mos_card("M_PD Y A VSS VSS nfet_03v3 W=2u L=500n M=1 NF=1", resolved)
    assert card.target_model == "nfet_03v3"
    assert card.model_translated is False


def test_a_role_the_pdk_does_not_ship_is_refused_by_role(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_mos_card("M_PD Y A VSS VSS NMOS_THKOX W=2u L=1u M=1 NF=1", resolved)
    assert excinfo.value.code == "pdk.model.unavailable"
    assert "nmos.io" in excinfo.value.message


def test_an_unrecognised_model_names_the_canonical_vocabulary(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_mos_card("M_PD Y A VSS VSS made_up_fet W=2u L=1u M=1 NF=1", resolved)
    assert excinfo.value.code == "pdk.model.unknown"
    assert "nmos.core" in (excinfo.value.hint or "")


def test_translations_are_recorded_in_the_binding_facts(tmp_path):
    resolved = _resolved(tmp_path, GF180MCUD)
    deck = "* t\nM_PD Y A VSS VSS sg13_lv_nmos W=2u L=500n M=1 NF=1\n.END\n"
    _, facts = bind_deck(deck, resolved)
    assert facts["model_translations"] == {"sg13_lv_nmos": "nfet_03v3"}


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


def test_a_corner_selected_by_directory_resolves_every_flavour(tmp_path):
    """FreePDK45 has no library sections at all: the corner *is* a directory."""

    root = _fake_pdk(tmp_path, FREEPDK45)
    resolved = resolve_pdk_binding(FREEPDK45.pdk_id, root, corner="ss")
    assert resolved.corner == "ss"
    assert len(resolved.library_paths) == len(FREEPDK45.library_entries)
    for path in resolved.library_paths:
        assert path.parent.name == "models_ss"


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


def test_a_multi_entry_prelude_keeps_its_declared_order(tmp_path):
    """gf180mcu's corner library evaluates switches defined only in design.ngspice.

    Including the corner library alone yields "Undefined parameter [fnoicor]"
    on every model card, so the order is load-bearing, not cosmetic.
    """

    resolved = _resolved(tmp_path, GF180MCUD)
    text, _ = bind_deck("* t\n.END\n", resolved)
    lines = [line for line in text.splitlines() if line.startswith((".lib", ".include"))]
    assert lines[0].startswith(".include ") and lines[0].endswith("design.ngspice")
    assert lines[1].startswith(".lib ") and lines[1].endswith(" typical")


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


def test_a_digital_platform_request_is_refused_before_any_tool_runs(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=NANGATE45.pdk_id,
        pdk_root=tmp_path,
    )
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "pdk.analog.unsupported"


def test_the_missing_models_hint_now_names_the_pdk_path(tmp_path):
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
    )
    assert payload["diagnostics"][0]["code"] == "testbench.models.required"
    assert "--pdk" in payload["diagnostics"][0]["hint"]


# --------------------------------------------------------------------------- #
# live runs against real installed collateral
# --------------------------------------------------------------------------- #
def _installed_root(binding) -> Path | None:
    """Return a root holding real collateral for one binding, or None."""

    variable = f"OPENADA_TEST_{binding.pdk_id.upper().replace('-', '_')}_ROOT"
    candidates: list[Path] = []
    explicit = os.environ.get(variable)
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/foss/pdks"))
    candidates.append(Path("/foss/model-kits/freepdk45/1.4"))
    cache = Path.home() / ".cache" / "openada" / "pdks" / binding.pdk_id
    if cache.is_dir():
        candidates.extend(sorted(cache.iterdir()))
    probe, _ = binding.library_entries[0].resolve(binding.default_corner)
    for candidate in candidates:
        for root in (candidate / binding.pdk_id, candidate):
            if (root / probe).is_file():
                return candidate
    return None


IHP_ROOT = _installed_root(IHP_SG13G2)


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


PORTABLE_TARGETS = [
    pytest.param(binding, id=binding.pdk_id) for binding in ANALOG_BINDINGS
]


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.parametrize("binding", PORTABLE_TARGETS)
def test_one_canonical_testbench_simulates_on_every_installed_pdk(tmp_path, binding):
    """The owner's contract, exercised: one deck, one syntax, every PDK.

    ``PORTABLE_INVERTER`` names canonical device roles and SI geometry and knows
    nothing about any PDK. Every difference -- ``X`` versus ``M``, ``ng`` versus
    ``nf``, microns versus metres, one ``.lib`` versus eight ``.include``s, an
    OSDI preload or none -- is supplied by the profile at bind time.
    """

    root = _installed_root(binding)
    if root is None:
        pytest.skip(f"no installed collateral for {binding.pdk_id}")

    payload = simulate_testbench(
        PORTABLE_INVERTER / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=binding.pdk_id,
        pdk_root=root,
        # sky130's tt library alone takes ~95 s to parse.
        timeout=600.0,
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    facts = payload["data"]["extensions"]["org.openada.pdk_binding"]
    assert facts["pdk_id"] == binding.pdk_id
    assert facts["rewritten_device_count"] == 2

    deck_text = (
        tmp_path / "evidence" / "decks" / "analysis-01-tran.spice"
    ).read_text(encoding="utf-8")
    # No canonical role token may survive into the deck the simulator sees.
    assert "nmos.core" not in deck_text and "pmos.core" not in deck_text
    assert binding.device_models["nmos.core"] in deck_text

    raw = tmp_path / "evidence" / "decks" / "analysis-01-tran.raw"
    assert raw.is_file() and raw.stat().st_size > 0
