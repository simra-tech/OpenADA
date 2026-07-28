from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from openada import conformance
from openada.contract import file_record
from openada.discovery import DiscoveryManager
from openada.operations.result_series_extract import extract_result_series
from openada.operations.simulate import (
    OPERATION_NAME,
    PDK_BINDING_EXTENSION,
    simulate,
)
from openada.pdk_collateral import (
    blocking,
    collateral_basename_index,
    collateral_references,
    declares_option_scale,
    inspect_deck_collateral,
)
from openada.pdk_bindings import (
    FREEPDK45,
    GF180MCUD,
    IHP_SG13G2,
    MAX_PROBED_SOURCES,
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
#: A PDK-bound run is a ``circuit.simulate`` run like any other; it returns the
#: same reviewed evidence, so it is checked against the same published schema.
PROFILE_PATH = ROOT / "profiles" / "circuit.simulate-v1alpha2.json"

PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
#: ``request_id`` is ``format: uuid``, which only binds with a format checker.
DATA_VALIDATOR = Draft202012Validator(
    PROFILE["normalized_result"]["data_schema"], format_checker=FormatChecker()
)

NGSPICE = shutil.which("ngspice")

ANALOG_BINDINGS = [binding for binding in REGISTRY.values() if binding.analog]


def _assert_contract(payload: dict) -> None:
    assert payload["operation"] == OPERATION_NAME == "simulate"
    assert conformance.result_conformance_issues(payload) == ()
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
    deck = f"* SI geometry\n{CANONICAL_MOS_CARD}\n.END\n"
    text, _ = bind_deck(deck, resolved)
    lines = text.splitlines()

    # All three pieces are load-bearing. The explicit scale states the units
    # ngspice will apply, wnflag selects multi-finger bins using W/NF, and the
    # SI source card is converted to plain microns exactly once before sky130's
    # model-bin lookup sees it.
    assert lines.count(".option scale=1e-6") == 1
    assert lines.count(".option wnflag=1") == 1
    assert (
        "xM_PD Y A VSS VSS sky130_fd_pr__nfet_01v8 "
        "w=2 l=0.5 m=1 nf=1"
    ) in lines
    assert CANONICAL_MOS_CARD not in text


def test_a_scaling_pdk_states_its_convention_in_the_bound_deck(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    text, facts = bind_deck("* t\n.END\n", resolved)
    assert ".option scale=1e-6\n" in text
    assert ".option wnflag=1\n" in text
    assert text.index(".option scale=1e-6\n") < text.index(".option wnflag=1\n")
    assert text.index(".option wnflag=1\n") < text.index(".lib ")
    assert facts["geometry_scale"] == "1e-6"


@pytest.mark.parametrize(
    "binding",
    (IHP_SG13G2, GF180MCUD, FREEPDK45),
    ids=lambda binding: binding.pdk_id,
)
def test_non_sky_pdks_leave_width_normalization_untouched(tmp_path, binding):
    resolved = _resolved(tmp_path, binding)
    text, _ = bind_deck("* t\n.END\n", resolved)
    assert ".option wnflag" not in text


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


@pytest.mark.parametrize(
    ("role", "target_model"),
    [
        ("nmos.core", "nfet_03v3"),
        ("pmos.core", "pfet_03v3"),
    ],
)
def test_gf180_accepts_devices_in_its_full_legal_binning_envelope(
    tmp_path, role, target_model
):
    """The last gf180 bin extends to L=50.001 um and W=100.001 um.

    The former envelope stopped at the third bin (10 um by 20 um), so its own
    preflight refused legal devices before ngspice could select the fourth bin.
    Exercise both polarities at a point legal only in that formerly omitted
    part of the grid.
    """

    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_mos_card(
        f"M_LEGAL d g s b {role} W=100u L=50u M=1 NF=1",
        resolved,
    )
    assert card.rewritten is True
    assert card.target_model == target_model
    assert f" {target_model} w=100u l=50u m=1 nf=1" in card.text


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
    # IHP SG13G2 has no threshold-flavour split: only a 1.2 V and a 3.3 V
    # family, mapped to .core and .io. A deck naming another PDK's low-Vt
    # device is refused by *role*, naming the role rather than the token, so
    # the author learns the technology has no such device rather than that a
    # spelling was wrong.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_mos_card("M_PD Y A VSS VSS NMOS_VTL W=2u L=1u M=1 NF=1", resolved)
    assert excinfo.value.code == "pdk.model.unavailable"
    assert "nmos.lvt" in excinfo.value.message


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

    text, _ = bind_deck("* FreePDK45 ss\n.END\n", resolved)
    model_cards = [
        line
        for line in text.splitlines()
        if line.lower().startswith((".include ", ".lib "))
    ]
    # These are flat model cards, not sectioned libraries. In particular, the
    # corner token must occur only in the selected directory and must never be
    # emitted as a third `.lib` argument.
    assert model_cards == [f".include {path}" for path in resolved.library_paths]
    assert all("/models_ss/" in line for line in model_cards)
    assert not any(line.lower().startswith(".lib ") for line in model_cards)


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
    # IHP's own .spiceinit loads four OSDI modules, not two: psp103,
    # psp103_nqs, r3_cmc and mosvar. Loading only the PSP pair works for a
    # MOS-only deck and fails the moment the deck instantiates a PDK resistor.
    assert sum(1 for line in lines if line.startswith("pre_osdi ")) == 4
    assert ".endc" in lines[:8]
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
    assert lines == [
        f".include {resolved.library_paths[0]}",
        f".lib {resolved.library_paths[1]} typical",
    ]


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


_PROBE_DECK = (
    "* t\n"
    ".SUBCKT DUT IN OUT VSS\n"
    "V_INTERNAL n1 VSS DC 0\n"
    ".ENDS DUT\n"
    "V_GND VSS 0 DC 0\n"
    "I_REF VDD IN DC 20u\n"
    "V_DD VDD VSS DC 1.8\n"
    "V_TEST OUT VSS DC 1 AC 1\n"
    "X_DUT IN OUT VSS DUT\n"
    ".SAVE OUT\n"
    ".AC DEC 10 1 1G\n"
    ".END\n"
)


def test_a_narrowed_write_keeps_the_only_amperes_in_the_pipeline(tmp_path):
    """`low_frequency_impedance` is volts over amperes.

    Narrowing `write` to one `v(net)` per saved net removed every current from
    the raw file, so a testbench that named its saved nets could never produce
    a driving-point impedance. Two live jobs proved it: job_35abcfd447c0d85f
    and job_383d058ac044c601 were each asked for an output impedance, each
    built the right 1 V AC probe, each reached `extract`, and each stopped
    with nothing in amperes to name.
    """
    resolved = _resolved(tmp_path)
    text, _ = bind_deck(
        _PROBE_DECK, resolved, raw_name="a.raw", saved_nets=("OUT",)
    )
    lines = [line.strip() for line in text.splitlines()]

    assert "write a.raw v(OUT) i(v_gnd) i(v_dd) i(v_test)" in lines
    # `.save` selects what ngspice *computes*. A current named only in `write`
    # is refused with "no writable vector found" and the run loses its raw.
    assert ".SAVE OUT i(v_gnd) i(v_dd) i(v_test)" in lines
    # A source inside the device under test belongs to the DUT, not the
    # testbench, and ngspice does not name its current this way.
    assert "i(v_internal)" not in text


def test_a_deck_without_saved_nets_still_writes_every_vector(tmp_path):
    """A bare `write` already dumps the currents; nothing to narrow."""
    resolved = _resolved(tmp_path)
    text, _ = bind_deck(_PROBE_DECK, resolved, raw_name="a.raw")
    lines = [line.strip() for line in text.splitlines()]

    assert "write a.raw" in lines
    assert ".SAVE OUT i(v_gnd) i(v_dd) i(v_test)" in lines


def test_source_probes_are_added_once_and_bounded(tmp_path):
    resolved = _resolved(tmp_path)
    already = _PROBE_DECK.replace(".SAVE OUT\n", ".SAVE OUT i(v_dd)\n")
    text, _ = bind_deck(already, resolved, raw_name="a.raw", saved_nets=("OUT",))

    assert ".SAVE OUT i(v_dd) i(v_gnd) i(v_test)" in text
    assert text.count("i(v_dd)") == 2  # once in `.SAVE`, once in `write`

    crowded = (
        "* t\n"
        + "".join(f"V_{index} n{index} 0 DC 0\n" for index in range(40))
        + ".END\n"
    )
    probed, _ = bind_deck(
        crowded, resolved, raw_name="a.raw", saved_nets=("OUT",)
    )
    write_line = next(
        line for line in probed.splitlines() if line.startswith("write ")
    )
    assert write_line.count("i(") == MAX_PROBED_SOURCES


def test_an_explicit_current_set_is_exact_canonical_and_not_truncated(tmp_path):
    resolved = _resolved(tmp_path)
    deck = (
        "* t\n"
        + "".join(f"V_{index} n{index} 0 DC 0\n" for index in range(20))
        + ".SAVE n17 n19\n"
        + ".OP\n"
        + ".END\n"
    )
    text, facts = bind_deck(
        deck,
        resolved,
        raw_name="a.raw",
        saved_nets=("n17", "n19"),
        retained_current_sources=("V_19", "v_17"),
    )
    lines = [line.strip() for line in text.splitlines()]

    assert "write a.raw v(n17) v(n19) i(v_19) i(v_17)" in lines
    assert ".SAVE n17 n19 i(v_19) i(v_17)" in lines
    assert "i(v_0)" not in text
    assert facts["current_retention"] == "explicit"
    assert facts["retained_current_vectors"] == ["i(v_19)", "i(v_17)"]


def test_an_explicit_empty_current_set_is_not_legacy_auto_probe(tmp_path):
    resolved = _resolved(tmp_path)
    text, facts = bind_deck(
        _PROBE_DECK,
        resolved,
        raw_name="a.raw",
        saved_nets=("OUT",),
        retained_current_sources=(),
    )

    assert "write a.raw v(OUT)" in text
    assert "i(v_gnd)" not in text
    assert facts["current_retention"] == "explicit"
    assert facts["retained_current_vectors"] == []


def test_an_explicit_current_only_set_is_the_exact_write_set(tmp_path):
    resolved = _resolved(tmp_path)
    text, facts = bind_deck(
        _PROBE_DECK,
        resolved,
        raw_name="a.raw",
        retained_current_sources=("V_GND",),
    )

    assert "write a.raw i(v_gnd)" in text
    assert facts["current_retention"] == "explicit"
    assert facts["retained_current_vectors"] == ["i(v_gnd)"]


@pytest.mark.parametrize(
    ("sources", "code"),
    [
        (("V_MISSING",), "pdk.current_source.unknown"),
        (("I_REF",), "pdk.current_source.invalid"),
        (("V_DD", "v_dd"), "pdk.current_source.duplicate"),
    ],
)
def test_an_explicit_current_set_refuses_non_voltage_or_duplicate_names(
    tmp_path, sources, code
):
    resolved = _resolved(tmp_path)
    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck(
            _PROBE_DECK,
            resolved,
            raw_name="a.raw",
            retained_current_sources=sources,
        )
    assert excinfo.value.code == code


def test_explicit_saved_nets_are_closed_and_case_insensitively_unique(tmp_path):
    resolved = _resolved(tmp_path)
    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck(
            _PROBE_DECK,
            resolved,
            raw_name="a.raw",
            saved_nets=("OUT)",),
            retained_current_sources=(),
        )
    assert excinfo.value.code == "pdk.saved_net.invalid"

    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck(
            _PROBE_DECK,
            resolved,
            raw_name="a.raw",
            saved_nets=("OUT", "out"),
            retained_current_sources=(),
        )
    assert excinfo.value.code == "pdk.saved_net.duplicate"


def test_an_unbounded_raw_name_is_refused(tmp_path):
    resolved = _resolved(tmp_path)
    with pytest.raises(PdkBindingError) as excinfo:
        bind_deck("* t\n.END\n", resolved, raw_name="../escape.raw")
    assert excinfo.value.code == "pdk.raw_name.invalid"


# --------------------------------------------------------------------------- #
# operation wiring
# --------------------------------------------------------------------------- #
def test_a_pdk_and_a_model_file_together_are_refused(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=tmp_path,
        models_file=NMOS_SOURCE / "nmos_lv.models",
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "simulation.models.ambiguous"


def test_a_pdk_without_a_root_is_refused(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
    )
    _assert_contract(payload)
    assert payload["diagnostics"][0]["code"] == "pdk.root.required"


def test_a_corner_without_a_pdk_is_refused(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        corner="mos_tt",
    )
    _assert_contract(payload)
    assert payload["diagnostics"][0]["code"] == "pdk.corner.unbound"


def test_a_digital_platform_request_is_refused_before_any_tool_runs(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=NANGATE45.pdk_id,
        pdk_root=tmp_path,
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "pdk.analog.unsupported"


def test_the_missing_models_hint_now_names_the_pdk_path(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["diagnostics"][0]["code"] == "simulation.models.required"
    assert "--pdk" in payload["diagnostics"][0]["hint"]


def test_no_binding_facts_are_claimed_when_no_pdk_was_bound(tmp_path):
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
    )
    assert PDK_BINDING_EXTENSION not in payload["data"]["extensions"]


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_a_bare_composed_deck_retains_exact_observations_and_context(tmp_path):
    source = tmp_path / "experiment.spice"
    source.write_text(
        "* exact experiment observations\n"
        "V_A A 0 DC 1\n"
        "V_B B 0 DC 2\n"
        "R_A A 0 1k\n"
        "R_B B 0 1k\n"
        ".SAVE A B\n"
        ".OP\n"
        ".END\n",
        encoding="utf-8",
    )
    specification = tmp_path / "experiment.json"
    specification.write_text('{"schema":"simra.experiment/v1"}\n', encoding="utf-8")
    specification_record = file_record(
        specification,
        kind="experiment-specification",
        role="simulation.experiment-specification",
    )
    extension = {
        "schema": "simra.experiment/v1",
        "spec_raw_sha256": specification_record["sha256"],
        "analysis_id": "op_bias",
    }
    destination = tmp_path / "evidence"

    payload = simulate(
        source,
        destination,
        discovery=DiscoveryManager(),
        pdk=FREEPDK45.pdk_id,
        pdk_root=_fake_pdk(tmp_path, FREEPDK45),
        saved_nets=("A",),
        retained_current_sources=("V_B",),
        extra_input_records=(specification_record,),
        extra_data_extensions={"org.openada.experiment": extension},
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    bound = (destination / "decks" / "experiment.spice").read_text(
        encoding="utf-8"
    )
    assert "write experiment.raw v(A) i(v_b)" in bound
    assert ".SAVE A B i(v_b)" in bound
    assert "i(v_a)" not in bound
    facts = payload["data"]["extensions"][PDK_BINDING_EXTENSION]
    assert facts["saved_nets"] == ["A"]
    assert facts["retained_current_vectors"] == ["i(v_b)"]
    assert facts["current_retention"] == "explicit"

    assert payload["data"]["extensions"]["org.openada.experiment"] == extension
    assert specification_record in payload["inputs"]
    retained = json.loads(
        (destination / "simulate.result.json").read_text(encoding="utf-8")
    )
    assert retained["data"]["extensions"]["org.openada.experiment"] == extension
    assert specification_record in retained["inputs"]


def test_explicit_binding_observations_without_a_pdk_are_refused(tmp_path):
    source = tmp_path / "deck.spice"
    source.write_text("* t\nV_A A 0 DC 1\nR_A A 0 1k\n.OP\n.END\n")
    payload = simulate(
        source,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        saved_nets=("A",),
        retained_current_sources=(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "simulation.binding_options.unbound"


def test_simulate_refuses_reserved_or_nonfinite_additive_extensions(tmp_path):
    source = tmp_path / "deck.spice"
    source.write_text("* t\n.END\n")

    conflict = simulate(
        source,
        tmp_path / "conflict",
        discovery=DiscoveryManager(),
        extra_data_extensions={"org.openada": {}},
    )
    assert conflict["diagnostics"][0]["code"] == "simulation.extension.conflict"
    assert not (tmp_path / "conflict").exists()

    nonfinite = simulate(
        source,
        tmp_path / "nonfinite",
        discovery=DiscoveryManager(),
        extra_data_extensions={"org.openada.experiment": {"bad": float("nan")}},
    )
    assert nonfinite["diagnostics"][0]["code"] == "simulation.extension.invalid"
    assert not (tmp_path / "nonfinite").exists()


# --------------------------------------------------------------------------- #
# hand-written collateral
# --------------------------------------------------------------------------- #
def test_the_basename_index_is_derived_from_the_registered_profiles():
    index = collateral_basename_index()
    # Every analog binding contributes, and only analog bindings do: a digital
    # platform ships no transistor collateral to confuse with anyone else's.
    assert set(index) == {binding.pdk_id for binding in ANALOG_BINDINGS}
    assert NANGATE45.pdk_id not in index
    assert "psp103.osdi" in index[IHP_SG13G2.pdk_id]
    assert "sky130.lib.spice" in index[SKY130A.pdk_id]
    # The index is derived, so IHP's module is not claimed by sky130.
    assert "psp103.osdi" not in index[SKY130A.pdk_id]


def test_one_pdks_incantation_applied_to_another_is_refused_as_foreign():
    """A card inside one PDK's tree naming a file another PDK ships.

    ngspice cannot know that a path names a different PDK than the library
    beside it, so this used to surface as "could not find a valid modelname" --
    or, worse, as numbers from collateral nobody selected.
    """

    deck = (
        "* inverter\n"
        ".lib /foss/pdks/sky130A/libs.tech/ngspice/cornerMOSlv.lib mos_tt\n"
        ".END\n"
    )
    findings = inspect_deck_collateral(deck)
    assert [finding.code for finding in findings] == ["pdk.collateral.foreign"]
    assert findings[0].severity == "error"
    # Both sides are named: whose tree it is, and whose collateral it is.
    assert "sky130A" in findings[0].message
    assert "ihp-sg13g2" in findings[0].message
    assert "--pdk sky130A" in findings[0].hint
    assert findings[0] in blocking(findings)


def test_the_observed_live_failure_is_refused_before_ngspice_runs():
    """``pre_osdi <sky130 tree>/psp103.osdi`` -- IHP's recipe applied to sky130.

    Both classifications stop the run before ngspice, so this was never a
    safety hole; it was the diagnostic naming the wrong mistake. "You bound
    collateral by hand" and "you applied one PDK's incantation to another" have
    different corrections, and this deck is the second. It is the module
    docstring's own headline example, so the code that decides it and the
    document that advertises it must agree.
    """

    deck = (
        "* inverter\n"
        "pre_osdi /foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi\n"
        ".END\n"
    )
    findings = inspect_deck_collateral(deck)
    assert [finding.code for finding in findings] == ["pdk.collateral.foreign"]
    assert findings[0].severity == "error"
    assert findings[0] in blocking(findings)
    # Both PDKs are named: the tree reached into, and the one that ships it.
    assert "sky130A" in findings[0].message
    assert "ihp-sg13g2" in findings[0].message
    assert "--pdk" in findings[0].hint


def test_a_preload_from_its_own_pdks_tree_is_still_hand_bound():
    """Foreign is the sharper case, not a replacement for the general rule.

    An OSDI preload naming the module its own tree ships is not one PDK's
    incantation applied to another -- but it is still a technology encoded into
    a circuit, so the deck can only ask its question in that one technology.
    """

    deck = (
        "* inverter\n"
        "pre_osdi /foss/pdks/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi\n"
        ".END\n"
    )
    findings = inspect_deck_collateral(deck)
    assert [finding.code for finding in findings] == ["pdk.collateral.hand_bound"]
    assert findings[0] in blocking(findings)


def test_a_reference_that_does_not_exist_is_refused_before_the_simulator(tmp_path):
    """A deck that binds nothing simulates a circuit with no devices."""

    deck = f"* t\n.include {tmp_path / 'absent' / 'models.spice'}\n.END\n"
    findings = inspect_deck_collateral(deck)
    assert [finding.code for finding in findings] == ["pdk.collateral.missing"]
    assert findings[0].severity == "error"
    assert "Line 2" in findings[0].message
    assert "--pdk" in findings[0].hint


def test_a_relative_reference_is_resolved_against_the_run_directory(tmp_path):
    models = tmp_path / "models.spice"
    deck = "* t\n.include models.spice\n.END\n"

    assert [
        finding.code for finding in inspect_deck_collateral(deck, workdir=tmp_path)
    ] == ["pdk.collateral.missing"]

    models.write_text("* cards\n", encoding="utf-8")
    findings = inspect_deck_collateral(deck, workdir=tmp_path)
    # It exists and belongs to no registered PDK: reported, never refused.
    assert [finding.code for finding in findings] == ["simulation.collateral.unmanaged"]
    assert blocking(findings) == ()


@pytest.mark.parametrize(
    "card",
    [
        ".lib /somewhere/vendor.lib tt",
        ".include /somewhere/models.spice",
        "pre_osdi /somewhere/compact.osdi",
        ".option scale=1e-6",
        ".option wnflag=0",
        ".option temp=27\n+ wnflag=0",
    ],
)
def test_hand_written_collateral_handed_to_a_binding_is_a_conflict(card):
    """The driver owns the whole prelude; two preludes is a race, not a merge."""

    findings = inspect_deck_collateral(f"* t\n{card}\n.END\n", bound_pdk=SKY130A.pdk_id)
    assert [finding.code for finding in findings] == ["pdk.collateral.conflict"]
    assert findings[0].severity == "error"
    assert SKY130A.pdk_id in findings[0].message
    assert findings[0] in blocking(findings)


def test_a_model_free_deck_conflicts_with_nothing():
    deck = "* t\nM_PD Y A VSS VSS nmos.core W=2u L=500n M=1 NF=1\n.END\n"
    assert inspect_deck_collateral(deck, bound_pdk=SKY130A.pdk_id) == ()
    assert inspect_deck_collateral(deck) == ()


def test_a_deck_reaching_into_a_pdk_tree_by_hand_names_the_binding_that_owns_it():
    """A reference that resolves, and is genuinely that PDK's own, is still refused.

    Capability was never the problem: the deck would run. What it cannot do is
    be asked the same question in another technology, because it has encoded
    one -- the corner entry point, the prefix, the parameter spelling and the
    unit convention are all restated for exactly one PDK.
    """

    root = _installed_root(IHP_SG13G2)
    if root is None:
        pytest.skip("no installed IHP SG13G2 PDK")
    resolved = resolve_pdk_binding(IHP_SG13G2.pdk_id, root)

    deck = f"* t\n.lib {resolved.library_paths[0]} {resolved.corner}\n.END\n"
    findings = inspect_deck_collateral(deck)
    assert [finding.code for finding in findings] == ["pdk.collateral.hand_bound"]
    assert findings[0].severity == "error"
    assert IHP_SG13G2.pdk_id in findings[0].message
    assert "--pdk" in findings[0].hint
    # The file is real and is that PDK's own, so it is neither foreign nor
    # missing: the refusal is exactly about who owns the binding.
    assert resolved.library_paths[0].is_file()


def test_the_reference_vocabulary_is_bounded_and_ignores_comments():
    deck = (
        "* .lib /commented/out.lib tt\n"
        ".LIB /a/one.lib tt\n"
        ".inc /a/two.spice\n"
        ".INCLUDE /a/three.spice\n"
        "pre_osdi /a/four.osdi\n"
        "R1 A B 1k\n"
        ".END\n"
    )
    references = collateral_references(deck)
    assert [reference.card for reference in references] == [
        "lib",
        "include",
        "include",
        "pre_osdi",
    ]
    assert [reference.line_number for reference in references] == [2, 3, 4, 5]
    assert all(reference.resolved is not None for reference in references)

    assert declares_option_scale(deck) is False
    assert declares_option_scale("* t\n.option scale=1.0u\n.END\n") is True
    assert declares_option_scale("* t\n.options scale = 1e-6\n.END\n") is True
    assert (
        declares_option_scale("* t\n.option temp=27\n+ scale=1e-6\n.END\n") is True
    )


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

    payload = simulate(
        IHP_INVERTER / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=IHP_ROOT,
    )
    _assert_contract(payload)

    # A PDK-bound run used to return a raw native payload with no analysis or
    # evidence block at all. It now returns the same reviewed evidence as any
    # other simulation: the model source changes what the simulator is told,
    # never what the evidence means.
    assert payload["data"]["analysis"]["type"] == "tran"
    assert payload["data"]["analysis"]["completion"] == "completed"
    assert payload["data"]["evidence"]["structure"] == "valid"

    binding = payload["data"]["extensions"][PDK_BINDING_EXTENSION]
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

    # The published artifact declares VIN/VOUT as saved nets. That used to
    # narrow `write` to voltages only, silently deleting every ampere-valued
    # quantity. Prove the source branch current survived all the way into the
    # retained raw artifact and is consumable by the typed evidence pipeline.
    write_line = next(
        line for line in deck_text.splitlines() if line.startswith("write ")
    )
    assert "v(VIN)" in write_line and "v(VOUT)" in write_line
    assert "i(v_dd)" in write_line
    extracted = extract_result_series(
        payload,
        raw,
        [
            {
                "native_name": "i(v_dd)",
                "output_name": "supply_current",
                "unit": "A",
                "component": "real",
            }
        ],
    )
    assert extracted["execution"]["status"] == "completed", extracted["diagnostics"]
    assert extracted["engineering"]["status"] == "pass", extracted["diagnostics"]
    supply_current = extracted["data"]["extraction"]["series"]["signals"][0]
    assert supply_current["name"] == "supply_current"
    assert supply_current["unit"] == "A"
    assert len(supply_current["values"]) == payload["data"]["analysis"]["point_count"]


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
@pytest.mark.skipif(IHP_ROOT is None, reason="no installed IHP SG13G2 PDK")
def test_an_undeclared_corner_never_reaches_the_simulator(tmp_path):
    payload = simulate(
        IHP_INVERTER / "schematic.artifact.json",
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk=IHP_SG13G2.pdk_id,
        pdk_root=IHP_ROOT,
        corner="mos_typical",
    )
    _assert_contract(payload)
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

    payload = simulate(
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

    facts = payload["data"]["extensions"][PDK_BINDING_EXTENSION]
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
