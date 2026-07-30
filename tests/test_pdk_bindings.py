from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from openada import conformance
from openada.contract import file_record
from openada.discovery import DiscoveryManager
from openada.engines.ngspice_outputs import extract_analysis_raw
from openada.engines.spice import NgspiceDriver
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
    join_spice_continuations,
    scan_deck_families,
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
    rewrite_device_card,
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
    sections_by_path: dict[Path, list[str | None]] = {}
    for corner in binding.corners:
        for entry in binding.library_entries:
            relative, section = entry.resolve(corner)
            sections_by_path.setdefault(root / relative, []).append(section)
    for path, sections in sections_by_path.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        unique_sections = tuple(dict.fromkeys(sections))
        path.write_text(
            "".join(
                (
                    f".LIB {section}\n.ENDL {section}\n"
                    if section is not None
                    else "* model cards\n"
                )
                for section in unique_sections
            ),
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
# extended-role device rewriting (diode / BJT / PDK resistor / PDK capacitor)
# --------------------------------------------------------------------------- #
def test_a_diode_on_a_model_card_pdk_keeps_the_d_letter(tmp_path):
    # gf180 ships diode_nd2ps_03v3 as a plain .model d card, so the native D
    # instance letter survives and area/m pass through in ngspice's own
    # area semantics, unscaled.
    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_device_card("D1 A K diode.core area=1p m=2", resolved)
    assert card.rewritten is True
    assert card.text == "D1 A K diode_nd2ps_03v3 area=1p m=2"


def test_a_diode_role_translates_per_pdk_and_area_rescales_squared(tmp_path):
    # sky130 writes diode area/pj in its own micron convention, like its MOS
    # geometry: an area rescales by the divisor squared, a perimeter linearly.
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card("D1 A K diode.core area=1p pj=4u m=2", resolved)
    assert card.target_model == "sky130_fd_pr__diode_pw2nd_05v5"
    assert card.role == "diode.core"
    assert card.text == "D1 A K sky130_fd_pr__diode_pw2nd_05v5 area=1 pj=4 m=2"


def test_a_subckt_diode_gains_the_x_prefix_and_si_geometry_passes_through(tmp_path):
    # IHP's dantenna is a subcircuit sized by w/l; the profile's geometry
    # divisor is 1, so SI values pass through untouched.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card("D1 A K diode.core W=1u L=1u", resolved)
    assert card.rewritten is True
    assert card.text == "xD1 A K dantenna w=1u l=1u"


def test_a_bjt_carries_its_author_stated_substrate_node(tmp_path):
    # The canonical BJT card is C B E SUB — the author states the substrate
    # tie exactly like MOS bulk; npn13G2 (c b e bn) takes all four. The
    # canonical multiplier M is functional multiplicity, so it emits as the
    # hierarchical instance ``m`` — NEVER as the HBT's Nx, which is finger
    # geometry (sg13g2_hbt_mod.lib scales cbeo/cje by Nx**0.975 and cjcp by
    # Nx**0.8, not linearly).
    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card("Q1 C B E VSUB bjt.npn M=2", resolved)
    assert card.rewritten is True
    assert card.text == "xQ1 C B E VSUB npn13G2 m=2"
    assert "Nx" not in card.text

    # pnpMPA models no substrate terminal: the canonical SUB is dropped and
    # REPORTED, never silently rewired.
    pnp = rewrite_device_card("Q2 C B E VSUB bjt.pnp", resolved)
    assert pnp.text == "xQ2 C B E pnpMPA"
    assert pnp.dropped_nodes == ("VSUB",)

    # A 3-node BJT card is the old, ambiguous form: typed refusal.
    with pytest.raises(PdkBindingError) as caught:
        rewrite_device_card("Q3 C B E bjt.npn", resolved)
    assert caught.value.code == "pdk.device.unbindable"


def test_a_pdk_resistor_rescales_geometry_with_its_stated_body(tmp_path):
    # The canonical physical-resistor card is n1 n2 BODY — author-stated,
    # like MOS bulk. sky130's res_high_po takes micron w/l (the PDK sets
    # scale=1u); canonical M is the hierarchical instance m (nominal
    # strength) AND mirrors into the body-read mult (mismatch scaling).
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card(
        "R1 A B VSS resistor.poly W=1u L=20u M=2", resolved
    )
    assert card.rewritten is True
    assert card.text == (
        "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 m=2 mult=2"
    )


def test_a_mim_capacitor_uses_the_pdks_parameter_spelling(tmp_path):
    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_device_card("C1 A B cap.mim W=10u L=10u", resolved)
    assert card.rewritten is True
    assert card.text == "xC1 A B cap_mim_2f0_m2m3_noshield c_width=10u c_length=10u"


@pytest.mark.parametrize("line", ["R1 A B 10k", "C1 A B 1p"])
def test_ideal_passives_are_never_touched_by_binding(tmp_path, line):
    # A numeric third token is a value, not a model: the card stays ideal,
    # byte for byte.
    resolved = _resolved(tmp_path, GF180MCUD)
    card = rewrite_device_card(line, resolved)
    assert (card.text, card.rewritten) == (line, False)


def test_an_extended_role_the_pdk_does_not_ship_is_refused_by_role(tmp_path):
    # FreePDK45 deliberately ships no non-MOS device table, so the refusal
    # must be typed, not a pass-through into a cryptic ngspice error.
    resolved = _resolved(tmp_path, FREEPDK45)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_device_card("D1 A K diode.core area=1p", resolved)
    assert excinfo.value.code == "pdk.model.unavailable"


def test_an_extended_role_with_the_wrong_arity_is_refused(tmp_path):
    resolved = _resolved(tmp_path, GF180MCUD)
    with pytest.raises(PdkBindingError) as excinfo:
        rewrite_device_card("D1 A K X diode.core", resolved)
    assert excinfo.value.code == "pdk.device.unbindable"


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
    assert direct.source_root == parent.source_root
    assert direct.closure_root_sha256 == parent.closure_root_sha256
    assert direct.snapshot_root_sha256 == parent.snapshot_root_sha256
    assert direct.root != parent.root


def test_every_referenced_pdk_file_is_content_bound(tmp_path):
    resolved = _resolved(tmp_path)
    roles = {record["role"] for record in resolved.input_records}
    assert roles == {
        "pdk.snapshot",
        "pdk.corner-library",
        "pdk.osdi-module",
        "pdk.identity",
    }
    for record in resolved.input_records:
        assert len(record["sha256"]) == 64


def test_snapshot_verification_refuses_a_writable_inner_directory(tmp_path):
    resolved = _resolved(tmp_path)
    inner = resolved.library_paths[0].parent
    assert inner not in {resolved.snapshot_root, resolved.root}

    inner.chmod(0o700)
    try:
        with pytest.raises(PdkBindingError) as caught:
            resolved.verify_snapshot()
        assert caught.value.code == "pdk.snapshot.unstable"
        assert "non-writable directory" in caught.value.message
    finally:
        inner.chmod(0o500)


def test_snapshot_verification_refuses_an_undeclared_inner_file(tmp_path):
    resolved = _resolved(tmp_path)
    inner = resolved.library_paths[0].parent
    extra = inner / "undeclared-after-publication.spice"

    inner.chmod(0o700)
    extra.write_text("* not in the snapshot manifest\n", encoding="utf-8")
    inner.chmod(0o500)
    try:
        with pytest.raises(PdkBindingError) as caught:
            resolved.verify_snapshot()
        assert caught.value.code == "pdk.snapshot.unstable"
        assert "undeclared or missing entries" in caught.value.message
    finally:
        inner.chmod(0o700)
        extra.unlink()
        inner.chmod(0o500)


def test_selected_transitive_closure_is_snapshotted_and_namespaced(tmp_path):
    """Only selected sections define semantics, while ngspice parser inputs stay private."""

    pdk_parent = _fake_pdk(tmp_path)
    source_root = pdk_parent / IHP_SG13G2.pdk_id
    low_entry, selected_section = IHP_SG13G2.library_entries[0].resolve(
        IHP_SG13G2.default_corner
    )
    assert selected_section == "mos_tt"
    low_library = source_root / low_entry
    model_directory = low_library.parent
    (model_directory / "top.spice").write_text(
        ".global PDK_GLOBAL\n", encoding="utf-8"
    )
    (model_directory / "selected-a.spice").write_text(
        ".subckt nmoscl_2 d g s b\n.ends nmoscl_2\n",
        encoding="utf-8",
    )
    (model_directory / "selected-b.spice").write_text(
        ".model selected_model nmos level=1\n", encoding="utf-8"
    )
    (model_directory / "inactive.spice").write_text(
        ".subckt inactive_collision a b\n.ends inactive_collision\n",
        encoding="utf-8",
    )
    low_library.write_text(
        (
            ".include top.spice\n"
            ".lib mos_tt\n"
            ".include selected-a.spice\n"
            ".include selected-b.spice\n"
            ".endl mos_tt\n"
            ".lib mos_ss\n"
            ".include inactive.spice\n"
            ".endl mos_ss\n"
            ".lib mos_ff\n.endl mos_ff\n"
            ".lib mos_sf\n.endl mos_sf\n"
            ".lib mos_fs\n.endl mos_fs\n"
        ),
        encoding="utf-8",
    )

    resolved = resolve_pdk_binding(IHP_SG13G2.pdk_id, pdk_parent)
    active_paths = [
        record["relative_path"] for record in resolved.closure_records
    ]
    assert active_paths[:4] == [
        low_entry,
        "libs.tech/ngspice/models/top.spice",
        "libs.tech/ngspice/models/selected-a.spice",
        "libs.tech/ngspice/models/selected-b.spice",
    ]
    assert "libs.tech/ngspice/models/inactive.spice" not in active_paths
    assert "nmoscl_2" in resolved.namespace_model_names
    assert "selected_model" in resolved.namespace_model_names
    assert "pdk_global" in resolved.namespace_global_nodes
    assert "inactive_collision" not in resolved.namespace_model_names

    # Ngspice preprocesses includes in unselected sections, so inactive.spice
    # is retained as parser transport, but it never enters the active closure
    # digest or namespace.
    inactive_record = next(
        record
        for record in resolved.input_records
        if str(record["path"]).endswith("/inactive.spice")
    )
    assert inactive_record["role"] == "pdk.parser-library"

    for record in resolved.input_records:
        assert Path(record["path"]).is_relative_to(resolved.snapshot_root)
        assert not Path(record["path"]).is_relative_to(resolved.source_root)
    assert all(path.is_relative_to(resolved.snapshot_root) for path in resolved.library_paths)
    assert all(path.is_relative_to(resolved.snapshot_root) for path in resolved.osdi_paths)

    original_closure_root = resolved.closure_root_sha256
    original_snapshot_root = resolved.snapshot_root_sha256
    captured_selected = next(
        Path(record["path"])
        for record in resolved.input_records
        if str(record["path"]).endswith("/selected-a.spice")
    )
    (model_directory / "selected-a.spice").write_text(
        ".subckt changed_live_tree a b\n.ends changed_live_tree\n",
        encoding="utf-8",
    )
    assert "nmoscl_2" in captured_selected.read_text(encoding="utf-8")
    resolved.verify_snapshot()

    # Changing a parser-only inactive file changes the complete snapshot
    # identity but not the selected semantic closure root.
    (model_directory / "inactive.spice").write_text(
        ".subckt another_inactive a b\n.ends another_inactive\n",
        encoding="utf-8",
    )
    (model_directory / "selected-a.spice").write_text(
        ".subckt nmoscl_2 d g s b\n.ends nmoscl_2\n",
        encoding="utf-8",
    )
    recaptured = resolve_pdk_binding(IHP_SG13G2.pdk_id, pdk_parent)
    assert recaptured.closure_root_sha256 == original_closure_root
    assert recaptured.snapshot_root_sha256 != original_snapshot_root


def _write_freepdk_level1_models(pdk_parent: Path, *, kp: str) -> Path:
    source_root = pdk_parent / FREEPDK45.pdk_id
    for entry in FREEPDK45.library_entries:
        relative, section = entry.resolve(FREEPDK45.default_corner)
        assert section is None
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        model = path.stem
        polarity = "pmos" if model.upper().startswith("PMOS") else "nmos"
        selected_kp = kp if model == "NMOS_VTG" else "1m"
        path.write_text(
            f".model {model} {polarity} level=1 vto=0.4 kp={selected_kp}\n",
            encoding="utf-8",
        )
    return pdk_parent


def _op_output(payload: dict, output_dir: Path) -> float:
    raw = next(
        record
        for record in payload["artifacts"]
        if record["role"] == "simulation.result"
    )
    extracted = extract_analysis_raw(
        output_dir / "decks" / "gain.raw",
        backend="ngspice",
        analysis={
            "type": "dc",
            "source_name": "VIN",
            "start": 0.8,
            "stop": 0.81,
            "step": 0.01,
        },
        selected_variables=("v(out)",),
        expected_bytes=raw["bytes"],
        expected_sha256=raw["sha256"],
    )
    assert extracted.valid, extracted
    return extracted.signals[0].real_values[0]


@pytest.mark.skipif(NGSPICE is None, reason="ngspice is not installed")
def test_live_model_swap_after_capture_cannot_change_native_answer(
    tmp_path, monkeypatch
):
    """The launch consumes captured A even after the installed path becomes B."""

    deck = tmp_path / "gain.spice"
    deck.write_text(
        (
            "* immutable PDK capture race\n"
            "VDD vdd 0 1.8\n"
            "VIN in 0 DC 0.8\n"
            "RLOAD vdd out 10k\n"
            "M1 out in 0 0 nmos.core W=1u L=1u M=1\n"
            ".save out\n"
            ".dc VIN 0.8 0.81 0.01\n"
            ".end\n"
        ),
        encoding="utf-8",
    )
    root_a = _write_freepdk_level1_models(tmp_path / "control-a", kp="1m")
    root_b = _write_freepdk_level1_models(tmp_path / "control-b", kp="10u")
    control_a_dir = tmp_path / "evidence-control-a"
    control_b_dir = tmp_path / "evidence-control-b"
    control_a = simulate(
        deck,
        control_a_dir,
        discovery=DiscoveryManager(),
        pdk=FREEPDK45.pdk_id,
        pdk_root=root_a,
    )
    control_b = simulate(
        deck,
        control_b_dir,
        discovery=DiscoveryManager(),
        pdk=FREEPDK45.pdk_id,
        pdk_root=root_b,
    )
    value_a = _op_output(control_a, control_a_dir)
    value_b = _op_output(control_b, control_b_dir)
    assert abs(value_a - value_b) > 0.1

    race_root = _write_freepdk_level1_models(tmp_path / "race", kp="1m")
    race_dir = tmp_path / "evidence-race"
    captured_binding = resolve_pdk_binding(
        FREEPDK45.pdk_id,
        race_root,
        snapshot_parent=race_dir / "pdk-snapshots",
    )
    live_model = (
        race_root
        / FREEPDK45.pdk_id
        / "ncsu_basekit/models/hspice/tran_models/models_nom/NMOS_VTG.inc"
    )
    a_body = live_model.read_bytes()
    original_simulate = NgspiceDriver.simulate
    swapped = False

    def swap_then_launch(self, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            live_model.write_text(
                ".model NMOS_VTG nmos level=1 vto=0.4 kp=10u\n",
                encoding="utf-8",
            )
        return original_simulate(self, *args, **kwargs)

    monkeypatch.setattr(NgspiceDriver, "simulate", swap_then_launch)
    monkeypatch.setattr(
        importlib.import_module("openada.operations.simulate"),
        "resolve_pdk_binding",
        lambda *_args, **_kwargs: pytest.fail(
            "an already captured binding must never be resolved from live paths"
        ),
    )
    raced = simulate(
        deck,
        race_dir,
        discovery=DiscoveryManager(),
        resolved_pdk_binding=captured_binding,
    )
    assert swapped is True
    assert raced["engineering"]["status"] == "pass", raced["diagnostics"]
    assert _op_output(raced, race_dir) == pytest.approx(value_a, rel=1e-9)
    assert _op_output(raced, race_dir) != pytest.approx(value_b, rel=1e-3)

    retained_model = next(
        record
        for record in raced["inputs"]
        if str(record["path"]).endswith("/NMOS_VTG.inc")
    )
    assert retained_model["sha256"] == hashlib.sha256(a_body).hexdigest()
    assert retained_model["sha256"] != hashlib.sha256(live_model.read_bytes()).hexdigest()
    binding = raced["data"]["extensions"][PDK_BINDING_EXTENSION]
    assert binding["closure_root_sha256"]
    assert Path(binding["snapshot_root"]).is_relative_to(race_dir)


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
    on every model card, so the order is load-bearing, not cosmetic. The
    non-MOS families follow: their per-class TYPICAL sections bind after the
    MOS corner, in the profile's declared order.
    """

    resolved = _resolved(tmp_path, GF180MCUD)
    text, _ = bind_deck("* t\n.END\n", resolved)
    lines = [line for line in text.splitlines() if line.startswith((".lib", ".include"))]
    assert lines == [
        f".include {resolved.library_paths[0]}",
        f".lib {resolved.library_paths[1]} typical",
        f".lib {resolved.library_paths[2]} diode_typical",
        f".lib {resolved.library_paths[3]} bjt_typical",
        f".lib {resolved.library_paths[4]} res_typical",
        f".lib {resolved.library_paths[5]} mimcap_typical",
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


# --------------------------------------------------------------------------- #
# extended-card production boundaries (review round 2)
# --------------------------------------------------------------------------- #


def test_gf180_multiplicity_emits_the_hierarchical_instance_m(tmp_path):
    # ngspice's hierarchical instance ``m`` (not a formal on any of these
    # subckts, therefore inherited) is the nominal-multiplicity spelling the
    # M=2 divider probe verified live at exactly 0.400 V. ``par`` is read
    # only by the pnp body (sm141064.ngspice:47324,47336 divide the is/bf
    # mismatch terms by sqrt(par)), so ONLY the pnp mirrors M into par; the
    # npn (47432-47481), ppolyf_u (38723) and cap_mim bodies never read it.
    # The diode is a .model card whose instance multiplier really is m.
    resolved = _resolved(tmp_path, GF180MCUD)
    emitted = {
        line: rewrite_device_card(line, resolved).text
        for line in (
            "D1 A K diode.core AREA=44.18p PJ=26.6u M=2",
            "Q1 C B E SUB bjt.npn M=2",
            "Q2 C B E SUB bjt.pnp M=2",
            "R1 A B BODY resistor.poly W=1u L=20u M=2",
            "C1 T B cap.mim W=10u L=10u M=2",
        )
    }
    assert emitted["D1 A K diode.core AREA=44.18p PJ=26.6u M=2"] == (
        "D1 A K diode_nd2ps_03v3 area=44.18p pj=26.6u m=2"
    )
    assert emitted["Q1 C B E SUB bjt.npn M=2"] == (
        "xQ1 C B E SUB npn_10p00x10p00 m=2"
    )
    assert emitted["Q2 C B E SUB bjt.pnp M=2"] == (
        "xQ2 C B E pnp_10p00x10p00 m=2 par=2"
    )
    assert emitted["R1 A B BODY resistor.poly W=1u L=20u M=2"] == (
        "xR1 A B BODY ppolyf_u r_width=1u r_length=20u m=2"
    )
    assert emitted["C1 T B cap.mim W=10u L=10u M=2"] == (
        "xC1 T B cap_mim_2f0_m2m3_noshield c_width=10u c_length=10u m=2"
    )


def test_sky130_multiplicity_emits_m_plus_the_mismatch_companion(tmp_path):
    # Mapping M ONLY onto res_high_po's `mult` or cap_mim's `mf` silently
    # drops the nominal multiplicity (round-1 matrix read 0.6 V where 0.4 V
    # was correct): those parameters feed the Monte-Carlo mismatch terms.
    # Round 3: hierarchical instance ``m`` carries the nominal strength AND
    # the body-read mismatch parameter mirrors the same value, because the
    # sqrt(mult)/sqrt(wc*lc*mf) Pelgrom scaling is real
    # (sky130_fd_pr__res_high_po.model.spice:35, cap_mim_m3_1:25, npn:41,
    # pnp W0p68L0p68:28-30).
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card(
        "R1 A B VSS resistor.poly W=1u L=20u M=2", resolved
    )
    assert card.text == (
        "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 m=2 mult=2"
    )
    cap = rewrite_device_card("C1 T B cap.mim W=10u L=10u M=4", resolved)
    assert cap.text.endswith("m=4 mf=4")
    npn = rewrite_device_card("Q1 C B E S bjt.npn M=2", resolved)
    assert npn.text.endswith("m=2 mult=2")
    pnp = rewrite_device_card("Q2 C B E S bjt.pnp M=3", resolved)
    assert pnp.text.endswith("m=3 mult=3")
    # The diode is a plain .model card: native m alone is already exact.
    diode = rewrite_device_card("D1 A K diode.core AREA=1p M=2", resolved)
    assert diode.text.endswith("m=2")
    assert "mult" not in diode.text and "mf" not in diode.text


def test_continuation_lines_join_before_any_rewrite():
    joined = join_spice_continuations(
        "R1 A B VSS resistor.poly\n+ W=1u L=20u M=2\nC1 X Y 1p\n"
    )
    assert "R1 A B VSS resistor.poly W=1u L=20u M=2\n" in joined
    assert "C1 X Y 1p\n" in joined
    # A continuation with its own trailing comment keeps the card intact.
    with_comment = join_spice_continuations(
        "Q1 C B E SUB bjt.npn\n+ M=2 ; from Table I\n"
    )
    assert with_comment.startswith("Q1 C B E SUB bjt.npn M=2")


def test_a_split_sky130_resistor_still_rescales_the_continued_geometry(tmp_path):
    # Sky130 geometry is written in microns under .option scale=1u; a W/L
    # that arrives on a + continuation must be rescaled exactly like an
    # inline one, or the device silently keeps an extra 1e-6.
    resolved = _resolved(tmp_path, SKY130A)
    deck = (
        "* split card\n"
        "R1 A B VSS resistor.poly\n"
        "+ W=1u L=20u M=2\n"
        ".END\n"
    )
    text, _ = bind_deck(deck, resolved)
    assert "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 m=2" in text


def test_trailing_comments_and_indentation_survive_a_device_rewrite(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card(
        "  R1 A B VSS resistor.poly W=1u L=20u ; paper value", resolved
    )
    assert card.rewritten is True
    assert card.text == (
        "  xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 ; paper value"
    )
    dollar = rewrite_device_card(
        "D1 A K diode.core AREA=100p $ Fig. 3", resolved
    )
    assert dollar.text.endswith(" $ Fig. 3")
    assert dollar.rewritten is True


def test_roles_are_case_insensitive_end_to_end(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    upper = rewrite_device_card("D1 A K DIODE.CORE W=780n L=780n", resolved)
    lower = rewrite_device_card("D1 A K diode.core W=780n L=780n", resolved)
    assert upper.text == lower.text
    # The malformed-arity refusal must also fire for uppercase roles.
    with pytest.raises(PdkBindingError) as caught:
        rewrite_device_card("Q1 C B BJT.NPN", resolved)
    assert caught.value.code == "pdk.device.unbindable"


def test_scan_deck_families_matches_the_binders_full_vocabulary():
    deck = (
        "X1 A B StrongArm\n"
        "D1 X Y diode.core AREA=1p\n"
        "R1 A B VSS RESISTOR.POLY W=1u L=2u\r\n"
        "* a comment naming cap.mim still counts: over-approximation is safe\n"
    )
    assert scan_deck_families(deck) == {"diode.core", "resistor.poly", "cap.mim"}
    assert scan_deck_families("M1 D G S B nmos.core W=1u L=1u\n") == frozenset()
    # Substring lookalikes do not count.
    assert scan_deck_families("R1 A B mydiode.corex 1k\n") == frozenset()
    # The binder accepts target-native and cross-PDK aliases through
    # translate_model, so the scanner must recognise exactly the same
    # vocabulary or an aliased deck binds after its library was gated out
    # (round-2 blocking finding 1).
    assert scan_deck_families("D1 A K dantenna W=1u L=1u\n") == {"diode.core"}
    assert scan_deck_families(
        "D1 A K sky130_fd_pr__diode_pw2nd_05v5 AREA=1p\n"
    ) == {"diode.core"}
    assert scan_deck_families("Q1 C B E S npn_10p00x10p00 M=2\n") == {"bjt.npn"}
    assert scan_deck_families("Q1 C B E PNPMPA\n") == {"bjt.pnp"}
    assert scan_deck_families("R1 A B VSS rppd W=1u L=2u\n") == {
        "resistor.poly"
    }
    # A lookalike of an alias does not count either.
    assert scan_deck_families("D1 A K dpantenna W=1u L=1u\n") == frozenset()


def test_family_tagged_libraries_load_only_for_decks_that_use_them(tmp_path):
    mos_only = "M1 D G S B nmos.core W=1u L=0.13u\n.END\n"
    resolved = resolve_pdk_binding(
        IHP_SG13G2.pdk_id,
        _fake_pdk(tmp_path, IHP_SG13G2),
        deck_text=mos_only,
    )
    joined = "\n".join(resolved.library_cards)
    # MOS corner libraries always load; every family-tagged one is gated out,
    # diodes.lib especially (its bare include installs generic model names).
    assert "cornerMOSlv" in joined and "cornerMOShv" in joined
    assert "diodes.lib" not in joined
    assert "cornerRES" not in joined
    assert "cornerCAP" not in joined
    assert "cornerHBT" not in joined

    diode_deck = "D1 A K diode.core W=780n L=780n\n.END\n"
    with_diode = resolve_pdk_binding(
        IHP_SG13G2.pdk_id,
        _fake_pdk(tmp_path / "again", IHP_SG13G2),
        deck_text=diode_deck,
    )
    joined = "\n".join(with_diode.library_cards)
    assert "diodes.lib" in joined
    assert "cornerHBT" not in joined

    # No deck text means the full, conservative closure.
    full = _resolved(tmp_path / "full", IHP_SG13G2)
    assert "diodes.lib" in "\n".join(full.library_cards)


def test_nondefault_corners_report_typical_only_device_families(tmp_path):
    # IHP pins res_typ/cap_typ/hbt_typ and a bare diodes.lib for every corner:
    # only the MOS roles follow mos_ff, and the binding facts must say so, so
    # simulate can emit pdk.corner.partial instead of silently mixing corners.
    resolved = _resolved(tmp_path, IHP_SG13G2, corner="mos_ff")
    deck = "* diode at a skewed MOS corner\nD1 A K diode.core W=780n L=780n\n.END\n"
    _, facts = bind_deck(deck, resolved)
    assert facts["corner_skewed_roles"] == [
        "nmos.core",
        "nmos.io",
        "pmos.core",
        "pmos.io",
    ]
    assert "diode.core" in facts["roles_bound"]


# --------------------------------------------------------------------------- #
# review round 3: alias gating, snapshot reuse, M semantics, dropped nodes,
# diode geometry conversion, corner honesty, CRLF, comment grammar, binder
# validation, card-specific advisory hints
# --------------------------------------------------------------------------- #


def test_an_alias_deck_loads_the_family_library_it_needs(tmp_path):
    # `D1 A K dantenna ...` binds through translate_model exactly like
    # `diode.core`, so it must gate diodes.lib IN — round 2 rewrote the card
    # after the library had been gated out, leaving an undefined subcircuit.
    alias_deck = "* alias\nD1 A K dantenna W=1u L=1u\n.END\n"
    resolved = resolve_pdk_binding(
        IHP_SG13G2.pdk_id,
        _fake_pdk(tmp_path, IHP_SG13G2),
        deck_text=alias_deck,
    )
    assert "diodes.lib" in "\n".join(resolved.library_cards)
    # A foreign PDK's model name for the same family gates identically.
    foreign = resolve_pdk_binding(
        IHP_SG13G2.pdk_id,
        _fake_pdk(tmp_path / "foreign", IHP_SG13G2),
        deck_text="* x\nD1 A K sky130_fd_pr__diode_pw2nd_05v5 AREA=1p\n.END\n",
    )
    assert "diodes.lib" in "\n".join(foreign.library_cards)


def test_a_gated_snapshot_refuses_a_deck_needing_an_uncaptured_family(tmp_path):
    mos_only = "* mos\nM1 D G S B nmos.core W=1u L=0.13u\n.END\n"
    resolved = resolve_pdk_binding(
        IHP_SG13G2.pdk_id,
        _fake_pdk(tmp_path, IHP_SG13G2),
        deck_text=mos_only,
    )
    assert resolved.captured_families is not None
    assert "diode.core" not in resolved.captured_families
    diode_deck = "* diode\nD1 A K diode.core W=780n L=780n\n.END\n"
    with pytest.raises(PdkBindingError) as caught:
        bind_deck(diode_deck, resolved)
    assert caught.value.code == "pdk.snapshot.family_missing"
    assert "diode.core" in caught.value.message
    assert "re-resolve" in (caught.value.hint or "").lower()
    # The aliased spelling of the same family is refused identically.
    with pytest.raises(PdkBindingError) as aliased:
        bind_deck("* d\nD1 A K dantenna W=1u L=1u\n.END\n", resolved)
    assert aliased.value.code == "pdk.snapshot.family_missing"


def test_snapshot_family_compatibility_is_exact_not_scan_shaped(tmp_path):
    # sky130 reaches every extended family through its always-loaded corner
    # library — nothing is gated — so a MOS-only sky130 snapshot must still
    # bind a diode deck: the guard records what the snapshot SERVES, not what
    # the original deck happened to mention.
    mos_only = "* mos\nM1 D G S B nmos.core W=1 L=0.5\n.END\n"
    resolved = resolve_pdk_binding(
        SKY130A.pdk_id,
        _fake_pdk(tmp_path, SKY130A),
        deck_text=mos_only,
    )
    assert resolved.captured_families == frozenset(
        (
            "diode.core",
            "bjt.npn",
            "bjt.pnp",
            "resistor.poly",
            "cap.mim",
        )
    )
    text, facts = bind_deck("* d\nD1 A K diode.core AREA=1p\n.END\n", resolved)
    assert "sky130_fd_pr__diode_pw2nd_05v5" in text
    # A full-closure resolution records no gating at all.
    full = _resolved(tmp_path / "full", IHP_SG13G2)
    assert full.captured_families is None
    assert full.facts()["captured_families"] is None


def test_every_pdk_times_device_card_preserves_m_2(tmp_path):
    # One preserved meaning: functional multiplicity. Every subckt-kind card
    # emits the hierarchical instance m=2; the PDK's mismatch-scaling
    # parameter mirrors the value exactly where the subckt body reads it
    # (sky130 mult/mf everywhere, gf180 par on the pnp only); model-kind
    # diodes keep the native m. Nothing maps M onto IHP's Nx (finger
    # geometry) and nothing drops it.
    cards = {
        "diode.core": "D1 A K diode.core AREA=1p PJ=4u M=2",
        "bjt.npn": "Q1 C B E S bjt.npn M=2",
        "bjt.pnp": "Q2 C B E S bjt.pnp M=2",
        "resistor.poly": "R1 A B S resistor.poly W=1u L=20u M=2",
        "cap.mim": "C1 T B cap.mim W=10u L=10u M=2",
    }
    expected_tail = {
        ("ihp-sg13g2", "diode.core"): "m=2",
        ("ihp-sg13g2", "bjt.npn"): "m=2",
        ("ihp-sg13g2", "bjt.pnp"): "m=2",
        ("ihp-sg13g2", "resistor.poly"): "m=2",
        ("ihp-sg13g2", "cap.mim"): "m=2",
        ("sky130A", "diode.core"): "m=2",
        ("sky130A", "bjt.npn"): "m=2 mult=2",
        ("sky130A", "bjt.pnp"): "m=2 mult=2",
        ("sky130A", "resistor.poly"): "m=2 mult=2",
        ("sky130A", "cap.mim"): "m=2 mf=2",
        ("gf180mcuD", "diode.core"): "m=2",
        ("gf180mcuD", "bjt.npn"): "m=2",
        ("gf180mcuD", "bjt.pnp"): "m=2 par=2",
        ("gf180mcuD", "resistor.poly"): "m=2",
        ("gf180mcuD", "cap.mim"): "m=2",
    }
    for binding in (IHP_SG13G2, SKY130A, GF180MCUD):
        resolved = _resolved(tmp_path / binding.pdk_id, binding)
        for role, line in cards.items():
            card = rewrite_device_card(line, resolved)
            assert card.rewritten, (binding.pdk_id, role)
            tail = expected_tail[(binding.pdk_id, role)]
            assert card.text.endswith(tail), (
                binding.pdk_id,
                role,
                card.text,
            )
            assert "m" not in card.dropped_parameters, (binding.pdk_id, role)
            assert "Nx" not in card.text


def test_dropped_substrate_nodes_reach_the_binding_facts(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = (
        "* pnp with a stated substrate the device cannot model\n"
        "Q2 C B E VSUB bjt.pnp M=1\n"
        ".END\n"
    )
    text, facts = bind_deck(deck, resolved)
    assert "xQ2 C B E pnpMPA" in text
    assert facts["dropped_nodes"] == {"Q2": ["VSUB"]}
    # A deck whose devices keep every terminal reports an empty mapping.
    _, kept = bind_deck(
        "* npn keeps all four\nQ1 C B E VSUB bjt.npn M=1\n.END\n", resolved
    )
    assert kept["dropped_nodes"] == {}


def test_dropped_nodes_and_derivations_become_warning_advisories(tmp_path):
    from openada.operations.simulate import _binding_advisories

    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = (
        "* both advisory sources at once\n"
        "Q2 C B E VSUB bjt.pnp M=1\n"
        "D1 A K diode.core AREA=1p PJ=4u\n"
        ".END\n"
    )
    _, facts = bind_deck(deck, resolved)
    notes = _binding_advisories(
        facts,
        deck_text=deck,
        corner=resolved.corner,
        default_corner=resolved.binding.default_corner,
    )
    by_code = {note["code"]: note for note in notes}
    node_note = by_code["pdk.device.node_dropped"]
    assert node_note["severity"] == "warning"
    assert "Q2" in node_note["message"] and "VSUB" in node_note["message"]
    assert "models no such terminal" in node_note["message"]
    derived_note = by_code["pdk.device.geometry_derived"]
    assert derived_note["severity"] == "warning"
    assert "D1" in derived_note["message"]
    assert "W=" in derived_note["message"] and "L=" in derived_note["message"]


def test_diode_area_pj_converts_to_the_exact_rectangle_on_a_wl_pdk(tmp_path):
    from decimal import Decimal

    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card(
        "D1 OUT VSS diode.core AREA=44.18p PJ=26.6u M=2", resolved
    )
    assert card.rewritten is True
    parameters = dict(
        token.split("=") for token in card.text.split()[4:]
    )
    width = Decimal(parameters["w"])
    length = Decimal(parameters["l"])
    # Exact rectangle: w*l = AREA and 2(w+l) = PJ, to conversion precision.
    assert abs(width * length - Decimal("44.18e-12")) < Decimal("1e-22")
    assert abs(2 * (width + length) - Decimal("26.6e-6")) < Decimal("1e-16")
    assert parameters["m"] == "2"
    assert card.geometry_derived is not None
    assert "exact rectangle" in card.geometry_derived
    assert card.dropped_parameters == ()


def test_diode_area_without_pj_assumes_a_square_and_says_so(tmp_path):
    from decimal import Decimal

    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card("D1 A K diode.core AREA=100p", resolved)
    parameters = dict(token.split("=") for token in card.text.split()[4:])
    assert Decimal(parameters["w"]) == Decimal(parameters["l"]) == Decimal(
        "1e-5"
    )
    assert "assumed square" in (card.geometry_derived or "")
    assert "no PJ" in card.geometry_derived
    # An inconsistent PJ (shorter than any rectangle of that area) also
    # falls back to the square, and the advisory says why.
    bad = rewrite_device_card("D1 A K diode.core AREA=100p PJ=1u", resolved)
    parameters = dict(token.split("=") for token in bad.text.split()[4:])
    assert Decimal(parameters["w"]) == Decimal("1e-5")
    assert "assumed square" in (bad.geometry_derived or "")
    assert "PJ" in bad.geometry_derived


def test_diode_wl_converts_to_area_pj_with_the_units_rescaled(tmp_path):
    # sky130 diodes take area/pj in the PDK's micron convention: the derived
    # SI area rescales by the divisor squared, the perimeter linearly.
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card("D1 A K diode.core W=10u L=10u M=2", resolved)
    assert card.text == (
        "D1 A K sky130_fd_pr__diode_pw2nd_05v5 area=100 pj=40 m=2"
    )
    assert "AREA=W*L" in (card.geometry_derived or "")
    assert card.dropped_parameters == ()
    # gf180's diode card is unscaled SI: same conversion, no rescale.
    gf = _resolved(tmp_path / "gf", GF180MCUD)
    converted = rewrite_device_card("D1 A K diode.core W=10u L=10u", gf)
    parameters = dict(token.split("=") for token in converted.text.split()[4:])
    from decimal import Decimal

    assert Decimal(parameters["area"]) == Decimal("1e-10")
    assert Decimal(parameters["pj"]) == Decimal("4e-5")


def test_a_diode_with_unmappable_geometry_is_refused_never_unsized(tmp_path):
    for binding in (IHP_SG13G2, SKY130A, GF180MCUD):
        resolved = _resolved(tmp_path / binding.pdk_id, binding)
        with pytest.raises(PdkBindingError) as caught:
            rewrite_device_card("D1 A K diode.core M=2", resolved)
        assert caught.value.code == "pdk.device.geometry_missing", binding.pdk_id
    # Half of the W/L pair alone cannot size a device either.
    resolved = _resolved(tmp_path / "half", IHP_SG13G2)
    with pytest.raises(PdkBindingError) as half:
        rewrite_device_card("D1 A K diode.core W=1u", resolved)
    assert half.value.code == "pdk.device.geometry_missing"


def test_geometry_derivations_reach_the_binding_facts(tmp_path):
    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = "* derive\nD1 OUT VSS diode.core AREA=44.18p PJ=26.6u M=2\n.END\n"
    _, facts = bind_deck(deck, resolved)
    assert set(facts["geometry_derived"]) == {"D1"}
    assert "W=" in facts["geometry_derived"]["D1"]
    # Geometry already in the target convention derives nothing.
    _, native = bind_deck(
        "* native\nD1 OUT VSS diode.core W=1u L=1u\n.END\n", resolved
    )
    assert native["geometry_derived"] == {}


def test_sky130_sf_and_fs_corners_do_not_claim_a_skewed_npn(tmp_path):
    # corners/sf.spice:29 and fs.spice:29 include the TYPICAL npn_05v5__t
    # while ff/ss include __f/__s: the skew set is a per-corner fact.
    assert "bjt.npn" in SKY130A.skewed_roles_for("ff")
    assert "bjt.npn" in SKY130A.skewed_roles_for("ss")
    assert "bjt.npn" in SKY130A.skewed_roles_for("tt")
    assert "bjt.npn" not in SKY130A.skewed_roles_for("sf")
    assert "bjt.npn" not in SKY130A.skewed_roles_for("fs")
    # The FETs still follow every corner.
    assert "nmos.core" in SKY130A.skewed_roles_for("sf")

    from openada.operations.simulate import _binding_advisories

    deck = "* npn at sf\nQ1 C B E S bjt.npn M=1\n.END\n"
    for corner, expect_partial in (("sf", True), ("ff", False)):
        resolved = resolve_pdk_binding(
            SKY130A.pdk_id,
            _fake_pdk(tmp_path / corner, SKY130A),
            corner=corner,
        )
        _, facts = bind_deck(deck, resolved)
        notes = _binding_advisories(
            facts,
            deck_text=deck,
            corner=corner,
            default_corner=SKY130A.default_corner,
        )
        partial = [n for n in notes if n["code"] == "pdk.corner.partial"]
        if expect_partial:
            assert partial and "bjt.npn" in partial[0]["message"]
        else:
            assert partial == []


def test_a_crlf_deck_binds_end_to_end(tmp_path):
    resolved = _resolved(tmp_path, SKY130A)
    deck = (
        "* CRLF deck\r\n"
        "M1 D G S B nmos.core W=1u L=0.5u\r\n"
        "R1 A B VSS resistor.poly\r\n"
        "+ W=1u L=20u M=2\r\n"
        "V1 D 0 DC 1.8\r\n"
        ".OP\r\n"
        ".END\r\n"
    )
    text, facts = bind_deck(deck, resolved, raw_name="out.raw")
    assert facts["rewritten_device_count"] == 2
    assert "xM1 D G S B sky130_fd_pr__nfet_01v8 w=1 l=0.5" in text
    assert "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 m=2 mult=2" in text
    # No carriage return survives inside any logical card.
    for line in text.splitlines():
        assert "\r" not in line


def test_unspaced_semicolon_and_bare_dollar_comments_match_simra(tmp_path):
    # Simra's netlist tool splits on the first ';' with no whitespace
    # required; the binder must accept the same grammar or a lint-clean deck
    # dies in the sky130 rescale (round-2 should-fix 3).
    resolved = _resolved(tmp_path, SKY130A)
    card = rewrite_device_card(
        "R1 A B VSS resistor.poly W=1u L=20u;paper", resolved
    )
    assert card.rewritten is True
    assert card.text == (
        "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20;paper"
    )
    bare = rewrite_device_card("D1 A K diode.core AREA=100p $", resolved)
    assert bare.rewritten is True
    assert bare.text.endswith(" $")
    mos = rewrite_mos_card("M1 D G S B nmos.core W=1u L=0.5u;from Fig. 2", resolved)
    assert mos.rewritten is True
    assert mos.text.endswith(";from Fig. 2")
    assert "w=1 l=0.5" in mos.text
    # On a continuation line, through the joiner and the full bind.
    deck = (
        "* continued comment\n"
        "R1 A B VSS resistor.poly\n"
        "+ W=1u L=20u;paper\n"
        ".END\n"
    )
    text, _ = bind_deck(deck, resolved)
    assert "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20;paper" in text


@pytest.mark.parametrize(
    ("card", "fragment"),
    [
        ("D1 A K diode.core AREA=1p AREA=2p", "twice"),
        ("D1 A K diode.core AREA=0", "positive"),
        ("D1 A K diode.core W=-1u L=1u", "positive"),
        ("R1 A B S resistor.poly W=1u L=0 M=1", "positive"),
        ("Q1 C B E S bjt.npn M=1.5", "whole number"),
        ("Q1 C B E S bjt.npn M=0", "whole number"),
        ("C1 A B cap.mim W=10u L=10u M=-2", "whole number"),
    ],
)
def test_the_binder_enforces_the_canonical_parameter_contract(
    tmp_path, card, fragment
):
    # A deck that bypasses Simra's lint meets the same contract here.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    with pytest.raises(PdkBindingError) as caught:
        rewrite_device_card(card, resolved)
    assert caught.value.code == "pdk.parameter.invalid"
    assert fragment in caught.value.message


def test_dropped_parameter_hints_describe_the_actual_card(tmp_path):
    from openada.operations.simulate import _binding_advisories

    # IHP's diode takes w/l/m; a card also carrying area/pj drops those two,
    # and the advisory must list the DIODE's accepted keys, not the MOS
    # binding vocabulary (which would falsely include nf).
    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = (
        "* both spellings\n"
        "D1 A K diode.core AREA=100p PJ=40u W=10u L=10u M=2\n"
        ".END\n"
    )
    _, facts = bind_deck(deck, resolved)
    assert facts["dropped_parameters"] == ["area", "pj"]
    assert {
        (record["instance"], record["parameter"], tuple(record["accepted"]))
        for record in facts["dropped_parameter_records"]
    } == {
        ("D1", "area", ("l", "m", "w")),
        ("D1", "pj", ("l", "m", "w")),
    }
    notes = _binding_advisories(
        facts,
        deck_text=deck,
        corner=resolved.corner,
        default_corner=resolved.binding.default_corner,
    )
    dropped_note = next(
        note for note in notes if note["code"] == "pdk.parameter.dropped"
    )
    assert "l, m, w" in dropped_note["hint"]
    assert "nf" not in dropped_note["hint"]


def test_dropped_parameter_records_stay_per_card_type(tmp_path):
    from openada.operations.simulate import _binding_advisories

    # Round-3 should-fix 1: a MOS and a diode card both dropping ``area``
    # must NOT union their accepted vocabularies — the diode author must
    # never be told nf is accepted, and the MOS author must still be.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = (
        "* same key dropped by two card types\n"
        "M1 D G S B nmos.core W=1u L=0.13u AREA=1p\n"
        "D1 A K diode.core AREA=100p PJ=40u W=10u L=10u\n"
        ".END\n"
    )
    _, facts = bind_deck(deck, resolved)
    area_records = {
        record["instance"]: tuple(record["accepted"])
        for record in facts["dropped_parameter_records"]
        if record["parameter"] == "area"
    }
    assert area_records == {
        "M1": ("l", "m", "nf", "w"),
        "D1": ("l", "m", "w"),
    }
    notes = _binding_advisories(
        facts,
        deck_text=deck,
        corner=resolved.corner,
        default_corner=resolved.binding.default_corner,
    )
    hint = next(
        note for note in notes if note["code"] == "pdk.parameter.dropped"
    )["hint"]
    # Two distinct per-card entries, each with its own vocabulary.
    assert "area (M1): accepted on that card are l, m, nf, w" in hint
    assert "area (D1): accepted on that card are l, m, w" in hint


def test_the_role_index_refuses_an_ambiguous_model_name(monkeypatch):
    import dataclasses as _dc

    conflicting = _dc.replace(
        FREEPDK45,
        pdk_id="conflicting-fake",
        device_models={"bjt.npn": "dantenna"},
    )
    monkeypatch.setitem(REGISTRY, "conflicting-fake", conflicting)
    with pytest.raises(ValueError) as caught:
        device_role_index()
    assert "dantenna" in str(caught.value)


# --------------------------------------------------------------------------- #
# review round 4: numeric robustness of the new Decimal paths, and scoped
# occurrence identity in the binding facts
# --------------------------------------------------------------------------- #


def test_ill_conditioned_rectangle_recovery_never_emits_zero_geometry(tmp_path):
    from decimal import Decimal

    # Round-3 blocking 2a: (half - root)/2 cancels catastrophically for
    # AREA << PJ^2; the lint-clean AREA=1e-30 PJ=1 card emitted w=0.5 l=0 —
    # an unsized device. The stable small root is AREA / large_root.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card("D1 A K diode.core AREA=1e-30 PJ=1", resolved)
    parameters = dict(token.split("=") for token in card.text.split()[4:])
    width = Decimal(parameters["w"])
    length = Decimal(parameters["l"])
    assert width > 0 and length > 0
    # The recovered rectangle still means the stated area.
    assert abs(width * length - Decimal("1e-30")) <= Decimal("1e-36")
    assert abs(2 * (width + length) - Decimal(1)) <= Decimal("1e-6")


def test_extreme_but_lint_legal_values_bind_or_refuse_typed(tmp_path):
    # Round-3 blocking 2b: values inside the inclusive 1e-60..1e60 window
    # crashed the binder with uncaught decimal.InvalidOperation, from
    # _format_number's default 28-digit context.
    ihp = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card("D1 A K diode.core AREA=1e60 PJ=1e60", ihp)
    from decimal import Decimal

    parameters = dict(token.split("=") for token in card.text.split()[4:])
    width = Decimal(parameters["w"])
    length = Decimal(parameters["l"])
    assert width > 0 and length > 0
    assert abs(width * length - Decimal("1e60")) <= Decimal("1e54")

    sky = _resolved(tmp_path / "sky", SKY130A)
    rescaled = rewrite_device_card(
        "R1 A B VSS resistor.poly W=1e60 L=1u M=1", sky
    )
    assert "w=1E+66" in rescaled.text or "w=1" in rescaled.text.lower()

    # Values beyond the canonical window are typed binder refusals, never
    # bare decimal exceptions.
    with pytest.raises(PdkBindingError) as caught:
        rewrite_device_card("D1 A K diode.core AREA=1e70", ihp)
    assert caught.value.code == "pdk.parameter.invalid"
    with pytest.raises(PdkBindingError) as tiny:
        rewrite_device_card("C1 A B cap.mim W=1e-70 L=1u", ihp)
    assert tiny.value.code == "pdk.parameter.invalid"


def test_format_number_is_context_independent_or_typed():
    from decimal import Decimal

    from openada.pdk_bindings import _format_number

    # The direct round-3 crash reproduction: quantize past 28 digits.
    assert _format_number(Decimal("1e28")) == str(10**28)
    assert _format_number(Decimal("1e60")) == str(10**60)
    # A value even the wide context cannot render is a typed refusal.
    with pytest.raises(PdkBindingError) as caught:
        _format_number(Decimal("1e500"))
    assert caught.value.code == "pdk.parameter.invalid"


def test_parse_spice_number_contains_arithmetic_escapes():
    # Regex-valid literals whose exponents defeat Decimal arithmetic must be
    # typed refusals, not decimal.Overflow / InvalidOperation escapes.
    for text in ("9e999999999999999998t", "1e999999999999999999999"):
        with pytest.raises(PdkBindingError) as caught:
            parse_spice_number(text)
        assert caught.value.code == "pdk.parameter.unparsable", text


def test_occurrence_facts_are_subckt_scoped(tmp_path):
    from openada.operations.simulate import _binding_advisories

    # Round-3 should-fix 2: two subcircuits each containing Q2 must stay two
    # occurrences in the facts and advisories, never merged by bare name.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    deck = (
        "* two subckts, one instance name\n"
        ".SUBCKT A C B E S\n"
        "Q2 C B E S bjt.pnp M=1\n"
        ".ENDS A\n"
        ".SUBCKT B C B E S\n"
        "Q2 C B E S bjt.pnp M=1\n"
        "D1 C B diode.core AREA=1p\n"
        ".ENDS B\n"
        ".END\n"
    )
    _, facts = bind_deck(deck, resolved)
    assert facts["dropped_nodes"] == {"A/Q2": ["S"], "B/Q2": ["S"]}
    assert set(facts["geometry_derived"]) == {"B/D1"}
    notes = _binding_advisories(
        facts,
        deck_text=deck,
        corner=resolved.corner,
        default_corner=resolved.binding.default_corner,
    )
    node_messages = [
        note["message"]
        for note in notes
        if note["code"] == "pdk.device.node_dropped"
    ]
    assert len(node_messages) == 2
    assert any("A/Q2" in message for message in node_messages)
    assert any("B/Q2" in message for message in node_messages)
    # Top-level cards keep their bare names.
    _, top = bind_deck("* top\nQ2 C B E S bjt.pnp M=1\n.END\n", resolved)
    assert top["dropped_nodes"] == {"Q2": ["S"]}


# --------------------------------------------------------------------------- #
# review round 5: derived-geometry window enforcement and ambient-context
# independence of the numeric binding path
# --------------------------------------------------------------------------- #


def test_derived_rectangle_outside_the_window_falls_back_to_the_square(
    tmp_path,
):
    from decimal import Decimal

    # Round-4 should-fix 1: AREA=1e-60 PJ=1e60 (both individually
    # lint-legal) demanded l~=2e-120 — sixty orders below the declared
    # believable minimum. The in-window square is the honest fallback, and
    # the advisory says why.
    resolved = _resolved(tmp_path, IHP_SG13G2)
    card = rewrite_device_card(
        "D1 A K diode.core AREA=1e-60 PJ=1e60", resolved
    )
    parameters = dict(token.split("=") for token in card.text.split()[4:])
    assert Decimal(parameters["w"]) == Decimal(parameters["l"]) == Decimal(
        "1e-30"
    )
    assert "1e-60..1e60 window" in (card.geometry_derived or "")
    assert "assumed square" in card.geometry_derived


def test_derived_area_outside_the_window_is_a_typed_refusal(tmp_path):
    # The other direction has no square to fall back to: two in-window
    # lengths whose product leaves the window are refused typed, never
    # emitted (gf180's diode takes SI area, so no rescale hides it).
    resolved = _resolved(tmp_path, GF180MCUD)
    with pytest.raises(PdkBindingError) as caught:
        rewrite_device_card("D1 A K diode.core W=1e40 L=1e40", resolved)
    assert caught.value.code == "pdk.device.geometry_missing"
    assert "1e-60..1e60" in caught.value.message
    # A comfortably in-window product still derives normally.
    ok = rewrite_device_card("D1 A K diode.core W=10u L=10u", resolved)
    assert ok.rewritten and "area=" in ok.text


def test_numeric_binding_path_is_ambient_context_independent(tmp_path):
    from decimal import Context, Decimal, getcontext, localcontext, setcontext

    from openada.pdk_bindings import _format_number

    # Round-4 should-fix 2: parse_spice_number multiplied under the AMBIENT
    # context — a caller-narrowed precision rounded long significands before
    # the wide formatter ever saw them, and an ambient Emax=9 misclassified
    # a plain 1e60 as unparsable.
    long_significand = "1." + "1" * 149  # 150 significant digits
    with localcontext(Context(prec=400)):
        exact = Decimal(long_significand) * Decimal("1e-6")

    saved = getcontext().copy()
    setcontext(Context(prec=5, Emax=9, Emin=-9))
    try:
        assert parse_spice_number("1e60") == Decimal("1e60")
        parsed = parse_spice_number(long_significand + "u")
        assert parsed == exact  # every one of the 150 digits survives
        assert _format_number(Decimal("1e28")) == str(10**28)
        # The rescale arithmetic is context-safe too: a full sky130 rewrite
        # (divide by 1e-6) under the hostile ambient context.
        resolved = _resolved(tmp_path, SKY130A)
        card = rewrite_device_card(
            "R1 A B VSS resistor.poly W=1u L=20u M=2", resolved
        )
        assert card.text == (
            "xR1 A B VSS sky130_fd_pr__res_high_po w=1 l=20 m=2 mult=2"
        )
    finally:
        setcontext(saved)


# --------------------------------------------------------------------------- #
# review round 6: significands beyond exact preservation are refused typed
# --------------------------------------------------------------------------- #


def test_a_251_digit_significand_is_refused_never_silently_rounded():
    with pytest.raises(PdkBindingError) as caught:
        parse_spice_number("1." + "1" * 250 + "u")
    assert caught.value.code == "pdk.parameter.unparsable"
    assert "200 significant digits" in caught.value.message


def test_a_boundary_hugging_long_significand_cannot_round_onto_the_boundary(
    tmp_path,
):
    # The sneaky case: a sky130 MOS width of 1.000...01e-4 (251 significant
    # digits, epsilon above the 1e-4 binning boundary) would round to
    # exactly 1e-4 under any finite precision — silently landing ON the
    # boundary the author wrote it against. Typed refusal, not a silent
    # round-to-boundary bind.
    resolved = _resolved(tmp_path, SKY130A)
    with pytest.raises(PdkBindingError) as caught:
        rewrite_mos_card(
            "M1 D G S B nmos.core W=" + "1." + "0" * 249 + "1e-4" + " L=0.5u",
            resolved,
        )
    assert caught.value.code == "pdk.parameter.unparsable"


def test_trailing_zeros_do_not_count_toward_the_significand_limit():
    from decimal import Decimal

    # 251 characters of mantissa, but only 151 significant digits once the
    # trailing zeros (which round away exactly) are stripped.
    text = "1." + "1" * 150 + "0" * 100
    parsed = parse_spice_number(text)
    assert parsed == Decimal(text)
