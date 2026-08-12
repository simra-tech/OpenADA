from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from openada.operations.testbench_plan_ngspice import (
    ResolvedBindingValue,
    TestbenchPlanCompileError,
    compile_testbench_plan_ngspice,
    prepare_testbench_plan_ngspice,
    seal_structural_dut,
)
from openada.operations.testbench_plan import validate_testbench_plan


FIXTURES = Path(__file__).parent / "fixtures" / "testbench-plan"


def _prepared(*, fresh_phase: bool = False):
    document = json.loads((FIXTURES / "closed_multistage_plan.json").read_text())
    if fresh_phase:
        phase_point = document["stages"][2]["points"][0]
        phase_point["state_policy"] = {
            "kind": "fresh",
            "initial_node_voltages": [
                {"kind": "port", "port": "OUT", "value": {"value": 0.6, "unit": "V"}}
            ],
        }
    dut_path = (FIXTURES / "compiler_charge_pump_dut.spice").resolve()
    override = dict(document["dut"])
    override["artifact"] = str(dut_path)
    override["sha256"] = hashlib.sha256(dut_path.read_bytes()).hexdigest()
    prepared, issues = validate_testbench_plan(document, dut_binding=override)
    assert issues == []
    assert prepared is not None
    return prepared


def _seal(body: bytes):
    return seal_structural_dut(
        body,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        namespace="fixture",
        top="RC_DUT",
        ports=("IN", "OUT", "VSS"),
    )


def test_seal_structural_dut_has_exact_canonical_form() -> None:
    raw = (FIXTURES / "compiler_rc_dut.spice").read_bytes()
    sealed = _seal(raw)

    assert sealed.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert sealed.sealed_top == "OPENADA_fixture_RC_DUT"
    assert sealed.canonical_bytes == (
        b"* SPDX-License-Identifier: MIT\n"
        b"* Synthetic RC DUT for deterministic testbench-plan compiler tests.\n"
        b".SUBCKT OPENADA_fixture_RC_DUT IN OUT VSS\n"
        b"R_DUT IN OUT 1k\n"
        b"C_DUT OUT VSS 1p\n"
        b".ENDS OPENADA_fixture_RC_DUT\n"
    )
    assert sealed.canonical_sha256 == hashlib.sha256(sealed.canonical_bytes).hexdigest()


def test_seal_structural_dut_rejects_digest_mismatch() -> None:
    raw = (FIXTURES / "compiler_rc_dut.spice").read_bytes()
    with pytest.raises(TestbenchPlanCompileError) as caught:
        seal_structural_dut(
            raw,
            expected_sha256="0" * 64,
            namespace="fixture",
            top="RC_DUT",
            ports=("IN", "OUT", "VSS"),
        )
    assert caught.value.code == "testbench_plan.compiler.dut_digest_mismatch"


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("V_CHEAT IN VSS 1", "testbench_plan.compiler.dut_element_forbidden"),
        ("B_CHEAT OUT VSS V=1", "testbench_plan.compiler.dut_element_forbidden"),
        (".OPTION reltol=1", "testbench_plan.compiler.dut_directive_forbidden"),
        ("+ hidden continuation", "testbench_plan.compiler.dut_continuation_forbidden"),
    ],
)
def test_seal_structural_dut_rejects_source_and_directive_cheats(
    line: str, code: str
) -> None:
    raw = f".SUBCKT RC_DUT IN OUT VSS\n{line}\n.ENDS RC_DUT\n".encode()
    with pytest.raises(TestbenchPlanCompileError) as caught:
        _seal(raw)
    assert caught.value.code == code


def test_seal_structural_dut_rejects_shadow_subcircuit_and_instance_collision() -> None:
    shadow = (
        b".SUBCKT RC_DUT IN OUT VSS\nR1 IN OUT 1k\n.ENDS RC_DUT\n"
        b".SUBCKT RC_DUT IN OUT VSS\nC1 OUT VSS 1p\n.ENDS RC_DUT\n"
    )
    with pytest.raises(TestbenchPlanCompileError) as caught:
        _seal(shadow)
    assert caught.value.code == "testbench_plan.compiler.dut_shadowing"

    collision = (
        b".SUBCKT RC_DUT IN OUT VSS\nRload IN OUT 1k\nrLOAD OUT VSS 2k\n"
        b".ENDS RC_DUT\n"
    )
    with pytest.raises(TestbenchPlanCompileError) as caught:
        _seal(collision)
    assert caught.value.code == "testbench_plan.compiler.dut_instance_collision"


def test_fresh_dc_sweep_expands_to_independent_exact_op_decks() -> None:
    compilation = prepare_testbench_plan_ngspice(
        _prepared(), corner="tt", stage_ids=("dc_characterize",)
    )

    assert len(compilation.conditions) == 51
    first = compilation.conditions[0]
    last = compilation.conditions[-1]
    assert first.receipt["analysis"]["sample_value"] == {"value": "0.35", "unit": "V"}
    assert last.receipt["analysis"]["sample_value"] == {"value": "0.85", "unit": "V"}
    assert first.condition_id.startswith("dc_characterize.nominal.dc_000000_")
    assert first.deck_bytes.count(b".OP\n") == 1
    assert b".DC " not in first.deck_bytes
    assert b"V_STIM_up_dc IN_UP 0 DC 0.35\n" in first.deck_bytes
    assert b"V_PROBE_PORT_OUT PUMP_OUT N_OPENADA_DUT_OUT DC 0\n" in first.deck_bytes
    assert b"X_OPENADA_DUT IN_UP IN_DOWN N_OPENADA_DUT_OUT VDD 0 OPENADA_hidden_cp_variant_CHARGE_PUMP\n" in first.deck_bytes
    assert hashlib.sha256(first.deck_bytes).hexdigest() == first.deck_sha256
    assert first.deck_sha256 == "a1e08ac93a31c09930b26412e128bdc280edaf4095e716810460c1963e2ca547"


def test_pulse_train_transient_has_exact_finite_count_and_branch_probe() -> None:
    compilation = prepare_testbench_plan_ngspice(
        _prepared(),
        corner="tt",
        stage_ids=("pulse_characterize",),
        binding_values=(
            ResolvedBindingValue(
                "bind_zero_bias", 0.61, "V", "a" * 64
            ),
        ),
    )
    assert len(compilation.conditions) == 1
    condition = compilation.conditions[0]
    assert b"V_STIM_up_pulses IN_UP 0 PULSE(0 1.2 1e-9 1e-10 1e-10 4e-9 1e-8 8)\n" in condition.deck_bytes
    assert b".TRAN 1e-10 9e-8 UIC\n" in condition.deck_bytes
    command = next(item for item in condition.expected_probes if item["id"] == "command_current")
    assert command["identity"] == {
        "stimulus_id": "up_pulses", "branch": "single", "source": "V_STIM_up_pulses"
    }
    assert command["native_vectors"] == ["i(V_STIM_up_pulses)"]
    assert condition.receipt["condition"]["resolved_bindings"][0]["source_receipt_sha256"] == "a" * 64
    assert condition.deck_sha256 == "274a597b554e30b95bfb67a4a44c7d32340ab2626d7840e7be6c7de4b0d5086e"


def test_phase_pair_transient_uses_independent_polarities_and_wrapped_offset() -> None:
    compilation = prepare_testbench_plan_ngspice(
        _prepared(fresh_phase=True),
        corner="tt",
        stage_ids=("phase_characterize",),
    )
    deck = compilation.conditions[0].deck_bytes
    assert b"V_STIM_phase_pair_REF IN_UP 0 PULSE(1.2 0 1e-9 1e-10 1e-10 4e-9 1e-8 8)\n" in deck
    assert b"V_STIM_phase_pair_OFFSET IN_DOWN 0 PULSE(0 1.2 8e-10 1e-10 1e-10 4e-9 1e-8 8)\n" in deck
    assert compilation.conditions[0].deck_sha256 == "842fe5bda2c3ab131a3561680afe6e9750c684521eb3a148e85c37f8c2824e44"


def test_compile_is_deterministic_and_publishes_only_into_empty_directory(tmp_path: Path) -> None:
    prepared = _prepared()
    first = compile_testbench_plan_ngspice(
        prepared, tmp_path / "one", corner="tt", stage_ids=("dc_characterize",)
    )
    second = compile_testbench_plan_ngspice(
        prepared, tmp_path / "two", corner="tt", stage_ids=("dc_characterize",)
    )
    assert first.receipt == second.receipt
    assert (tmp_path / "one" / "compile-receipt.json").read_bytes() == (
        tmp_path / "two" / "compile-receipt.json"
    ).read_bytes()
    assert not any("timestamp" in str(key).casefold() for key in first.receipt)

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("mine")
    with pytest.raises(TestbenchPlanCompileError) as caught:
        compile_testbench_plan_ngspice(
            prepared, occupied, corner="tt", stage_ids=("dc_characterize",)
        )
    assert caught.value.code == "testbench_plan.compiler.output_not_empty"
    assert (occupied / "keep").read_text() == "mine"

    symlink_target = tmp_path / "symlink_target"
    symlink_target.mkdir()
    symlink = tmp_path / "output_symlink"
    symlink.symlink_to(symlink_target, target_is_directory=True)
    with pytest.raises(TestbenchPlanCompileError) as caught:
        compile_testbench_plan_ngspice(
            prepared, symlink, corner="tt", stage_ids=("dc_characterize",)
        )
    assert caught.value.code == "testbench_plan.compiler.output_exists"
    assert symlink.is_symlink()


def test_compile_rejects_unknown_corner_and_unresolved_stage_binding() -> None:
    prepared = _prepared()
    with pytest.raises(TestbenchPlanCompileError) as caught:
        prepare_testbench_plan_ngspice(
            prepared, corner="ff", stage_ids=("dc_characterize",)
        )
    assert caught.value.code == "testbench_plan.compiler.corner_unknown"

    with pytest.raises(TestbenchPlanCompileError) as caught:
        prepare_testbench_plan_ngspice(
            prepared, corner="tt", stage_ids=("pulse_characterize",)
        )
    assert caught.value.code == "testbench_plan.compiler.binding_unresolved"


def test_loop_contract_does_not_poison_supported_stages_and_refuses_typed_ac() -> None:
    prepared = _prepared()

    supported = prepare_testbench_plan_ngspice(
        prepared, corner="tt", stage_ids=("dc_characterize",)
    )
    assert len(supported.conditions) == 51
    loop_probe = next(
        item
        for item in supported.conditions[0].expected_probes
        if item["id"] == "loop_gain"
    )
    assert loop_probe["available"] is False
    assert loop_probe["native_vectors"] == []

    with pytest.raises(TestbenchPlanCompileError) as caught:
        prepare_testbench_plan_ngspice(
            prepared,
            corner="tt",
            stage_ids=("loop_grade",),
            binding_values=(
                ResolvedBindingValue(
                    "bind_local_phase_gain", 270e-6, "A", "b" * 64
                ),
            ),
        )
    assert caught.value.code == "testbench_plan.compiler.analysis_unsupported"
    assert caught.value.path.endswith("/analysis/kind")


def test_compiler_rejects_mutated_prepared_typed_views() -> None:
    prepared = _prepared()
    prepared.dut.connections["UP"] = "UNDECLARED_COMMAND_NODE"

    with pytest.raises(TestbenchPlanCompileError) as caught:
        prepare_testbench_plan_ngspice(
            prepared, corner="tt", stage_ids=("dc_characterize",)
        )

    assert caught.value.code == "testbench_plan.compiler.plan_changed"
    assert caught.value.path == "/dut"
