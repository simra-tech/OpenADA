"""Execution-receipt tests for the closed testbench-plan runner."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from openada.operations.testbench_plan import validate_testbench_plan
from openada.operations.testbench_plan_ngspice import (
    CompiledCondition,
    PreparedNgspiceCompilation,
    SealedDut,
    prepare_testbench_plan_ngspice,
)
import openada.operations.testbench_plan_runner as runner
from openada.operations.testbench_plan_runner import (
    HostNgspiceExecutor,
    SimulatorExecution,
    execute_testbench_plan_ngspice,
)


ROOT = Path(__file__).parents[1]


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _dut_body() -> bytes:
    return (
        b"* SPDX-License-Identifier: MIT\n"
        b".SUBCKT RC_DUT IN OUT VDD VSS\n"
        b"R_DUT IN OUT 1k\n"
        b"C_DUT OUT VSS 1p\n"
        b".ENDS RC_DUT\n"
    )


def _plan_document(artifact: Path, *, dc: bool = False) -> dict:
    dut = _dut_body()
    stimulus = (
        {
            "id": "input_dc",
            "kind": "dc_state",
            "target_port": "IN",
            "supply_id": "core_supply",
            "state": "high",
            "level": {
                "kind": "supply_scaled",
                "supply_id": "core_supply",
                "fraction": 1.0,
            },
        }
        if dc
        else {
            "id": "input_pulses",
            "kind": "pulse_train",
            "target_port": "IN",
            "supply_id": "core_supply",
            "polarity": "active_high",
            "low_level": {
                "kind": "supply_scaled",
                "supply_id": "core_supply",
                "fraction": 0.0,
            },
            "high_level": {
                "kind": "supply_scaled",
                "supply_id": "core_supply",
                "fraction": 1.0,
            },
            "delay": {"value": 2e-10, "unit": "s"},
            "rise_time": {"value": 1e-10, "unit": "s"},
            "fall_time": {"value": 1e-10, "unit": "s"},
            "pulse_width": {"value": 4e-10, "unit": "s"},
            "period": {"value": 1e-9, "unit": "s"},
            "count": 2,
        }
    )
    stimulus_id = str(stimulus["id"])
    analysis = (
        {
            "kind": "dc_sweep",
            "source_stimulus_id": stimulus_id,
            "start": {"value": 0.0, "unit": "V"},
            "stop": {"value": 1.0, "unit": "V"},
            "step": {"value": 0.5, "unit": "V"},
        }
        if dc
        else {
            "kind": "pulse_train_transient",
            "stimulus_id": stimulus_id,
            "step": {"value": 1e-10, "unit": "s"},
            "stop": {"value": 3e-9, "unit": "s"},
        }
    )
    return {
        "schema": "simra.testbench-plan/v1",
        "id": "runner_rc_plan",
        "dut": {
            "artifact": str(artifact),
            "sha256": _sha(dut),
            "namespace": "runner_fixture",
            "top": "RC_DUT",
            "ports": [
                {"name": "IN", "direction": "input", "internal_nodes": []},
                {"name": "OUT", "direction": "output", "internal_nodes": []},
                {"name": "VDD", "direction": "supply", "internal_nodes": []},
                {"name": "VSS", "direction": "reference", "internal_nodes": []},
            ],
            "connections": {"IN": "IN", "OUT": "OUT", "VDD": "VDD", "VSS": "0"},
            "immutable": True,
        },
        "supplies": [
            {
                "id": "core_supply",
                "positive": "VDD",
                "negative": "0",
                "voltage": {"kind": "corner_value", "value_id": "vdd", "unit": "V"},
            }
        ],
        "corner_bindings": [
            {
                "id": "tt",
                "temperature": {"value": 27, "unit": "degC"},
                "values": [{"id": "vdd", "value": {"value": 1.2, "unit": "V"}}],
            }
        ],
        "stimuli": [stimulus],
        "probes": [
            {
                "id": "output_voltage",
                "kind": "dut_port_voltage",
                "unit": "V",
                "port": "OUT",
                "reference_port": "VSS",
            }
        ],
        "stages": [
            {
                "id": "characterize",
                "depends_on": [],
                "inputs": [],
                "points": [
                    {
                        "id": "nominal",
                        "condition": {
                            "corner": "tt",
                            "temperature": {"value": 27, "unit": "degC"},
                            "parameters": [],
                        },
                        "state_policy": {
                            "kind": "fresh",
                            "initial_node_voltages": [
                                {"kind": "port", "port": "OUT", "value": {"value": 0, "unit": "V"}}
                            ],
                        },
                        "settle_policy": {
                            "kind": "fixed_time",
                            "duration": {"value": 0, "unit": "s"},
                        },
                        "active_stimulus_ids": [stimulus_id],
                        "analysis": analysis,
                        "measurements": [
                            {
                                "id": "output_curve",
                                "kind": "curve",
                                "unit": "V",
                                "probe_id": "output_voltage",
                                "axis": {"kind": "analysis_axis"},
                            }
                        ],
                        "validity_rules": [
                            {
                                "id": "waveform_finite",
                                "kind": "finite",
                                "measurement_ids": ["output_curve"],
                                "on_fail": {"verdict": "invalid", "reason": "non_finite"},
                            }
                        ],
                    }
                ],
                "reductions": [],
                "validity_rules": [],
            }
        ],
        "bindings": [],
        "observables": [
            {
                "id": "output_curve",
                "source": {
                    "kind": "measurement",
                    "stage_id": "characterize",
                    "point_id": "nominal",
                    "measurement_id": "output_curve",
                },
                "unit": "V",
                "shape": "curve",
                "lineage": {
                    "kind": "condition_receipt",
                    "required_digests": [
                        "plan_sha256",
                        "dut_sha256",
                        "compiled_deck_sha256",
                        "waveform_sha256",
                    ],
                    "one_per_condition": True,
                },
            }
        ],
    }


def _prepared_plan(tmp_path: Path, *, dc: bool = False):
    artifact = tmp_path / "rc_dut.spice"
    artifact.write_bytes(_dut_body())
    prepared, issues = validate_testbench_plan(_plan_document(artifact, dc=dc))
    assert issues == []
    assert prepared is not None
    return prepared


def _condition(
    identifier: str,
    *,
    sample_index: int | None = None,
    sample_value: float | None = None,
) -> CompiledCondition:
    semantics = {"condition_id": identifier, "fresh": True}
    analysis: dict[str, object] = {"kind": "pulse_train_transient"}
    if sample_index is not None:
        analysis = {
            "kind": "dc_sweep",
            "sample_index": sample_index,
            "sample_value": {"value": str(sample_value), "unit": "V"},
        }
    condition_sha = _sha(_canonical(semantics))
    deck = f"* {identifier}\n.end\n".encode()
    receipt = {
        "condition_id": identifier,
        "condition_sha256": condition_sha,
        "stage_id": "characterize",
        "point_id": "nominal",
        "condition": semantics,
        "analysis": analysis,
    }
    return CompiledCondition(
        stage_id="characterize",
        point_id="nominal",
        condition_id=identifier,
        condition_sha256=condition_sha,
        relative_deck_path=f"conditions/{identifier}.spice",
        deck_bytes=deck,
        deck_sha256=_sha(deck),
        expected_probes=(
            {
                "id": "output_voltage",
                "native_vectors": ["v(out)"],
                "polarity_multiplier": 1,
            },
        ),
        receipt=receipt,
    )


def _compilation(
    plan, conditions, *, dut_bytes: bytes | None = None
) -> PreparedNgspiceCompilation:
    dut = _dut_body() if dut_bytes is None else dut_bytes
    sealed = SealedDut(
        raw_bytes=dut,
        raw_sha256=_sha(dut),
        canonical_bytes=dut,
        canonical_sha256=_sha(dut),
        original_top="RC_DUT",
        sealed_top="OPENADA_runner_fixture_RC_DUT",
    )
    return PreparedNgspiceCompilation(
        plan_bytes=_canonical(plan.document),
        sealed_dut=sealed,
        conditions=tuple(conditions),
        receipt={"schema": "simra.testbench-plan-compile/v1"},
    )


def test_fake_executor_emits_typed_envelope_and_exact_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path)
    condition = _condition("characterize.nominal")
    raw = b"exact native raw bytes\x00\xff"
    calls: list[str] = []

    def fake_executor(item, *, timeout_s):
        calls.append(item.condition_id)
        assert timeout_s == 7
        return SimulatorExecution(0, b"stdout", b"stderr", raw, "ngspice-46 fixture")

    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform(
            "time", (0.0, 1e-9, 2e-9), {"v(out)": (0.0, 0.7, 1.0)}
        ),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=fake_executor,
        timeout_s=7,
        _prepared_compilation=_compilation(plan, [condition]),
    )
    assert calls == ["characterize.nominal"]
    assert result.attempts[0].waveform_sha256 == _sha(raw)
    assert result.attempts[0].compiled_deck_sha256 == _sha(condition.deck_bytes)
    assert result.attempts[0].condition_sha256 == condition.condition_sha256
    assert result.attempts[0].simulator_identity == "ngspice-46 fixture"
    assert result.observables["observables"]["output_curve"] == {
        "x": [0.0, 1e-9, 2e-9],
        "y": [0.0, 0.7, 1.0],
    }
    assert result.observables["validity"] == {"waveform_finite": "VALID"}
    condition_metadata = result.observables["metadata"]["conditions"][0]
    assert condition_metadata["observables"] == ["output_curve"]
    assert result.observables["metadata"]["lineage"] == [
        {"observable": "output_curve", "condition_ids": ["characterize.nominal"]}
    ]
    schema = json.loads(
        (ROOT / "schemas" / "testbench-observables-v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(result.observables)
    assert result.receipt["expected_condition_count"] == 1
    assert result.receipt["attempted_condition_count"] == 1
    assert result.receipt["simulator_invocation_count"] == 1
    assert result.receipt["completed_condition_count"] == 1
    assert result.receipt["condition_inventory_complete"] is True
    assert result.receipt["compiler_receipts"] == [
        {"schema": "simra.testbench-plan-compile/v1"}
    ]
    assert result.receipt["environment"]["ambient_inherited"] is False
    assert "HOME" not in result.receipt["environment"]


def test_every_condition_is_attempted_after_failure_and_no_partial_curve_is_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path, dc=True)
    conditions = [
        _condition(f"characterize.nominal.dc_{index}", sample_index=index, sample_value=value)
        for index, value in enumerate((0.0, 0.5, 1.0))
    ]
    calls: list[str] = []

    def fake_executor(item, *, timeout_s):
        calls.append(item.condition_id)
        if len(calls) == 2:
            raise RuntimeError("hidden variant convergence failure")
        return SimulatorExecution(0, b"", b"", item.condition_id.encode(), "ngspice-46")

    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda condition, *_: runner._Waveform(
            "const", (0.0,), {"v(out)": (float(condition.receipt["analysis"]["sample_value"]["value"]),)}
        ),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=fake_executor,
        _prepared_compilation=_compilation(plan, conditions),
    )
    assert calls == [item.condition_id for item in conditions]
    assert [item.status for item in result.attempts] == [
        "completed",
        "invalid",
        "completed",
    ]
    assert "output_curve" not in result.observables["observables"]
    assert result.observables["validity"]["waveform_finite"].startswith(
        "UNKNOWN(runner:"
    )
    assert result.observables["metadata"]["lineage"] == []
    assert result.receipt["expected_condition_count"] == 3
    assert result.receipt["attempted_condition_count"] == 3


def test_fresh_dc_samples_are_independent_and_all_receipts_contribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path, dc=True)
    sample_values = (0.0, 0.5, 1.0)
    conditions = [
        _condition(f"characterize.nominal.dc_{index}", sample_index=index, sample_value=value)
        for index, value in enumerate(sample_values)
    ]

    def fake_executor(item, *, timeout_s):
        value = float(item.receipt["analysis"]["sample_value"]["value"])
        return SimulatorExecution(0, b"", b"", f"raw:{value}".encode(), "ngspice-46")

    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda condition, *_: runner._Waveform(
            "const",
            (0.0,),
            {"v(out)": (2 * float(condition.receipt["analysis"]["sample_value"]["value"]),)},
        ),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=fake_executor,
        _prepared_compilation=_compilation(plan, conditions),
    )
    assert result.observables["observables"]["output_curve"] == {
        "x": [0.0, 0.5, 1.0],
        "y": [0.0, 1.0, 2.0],
    }
    expected_ids = sorted(item.condition_id for item in conditions)
    assert result.observables["metadata"]["lineage"] == [
        {"observable": "output_curve", "condition_ids": expected_ids}
    ]
    for condition in result.observables["metadata"]["conditions"]:
        assert condition["observables"] == ["output_curve"]


def test_compiled_deck_digest_mismatch_refuses_before_executor_but_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path, dc=True)
    first = _condition("characterize.nominal.dc_0", sample_index=0, sample_value=0.0)
    corrupt = CompiledCondition(
        first.stage_id,
        first.point_id,
        first.condition_id,
        first.condition_sha256,
        first.relative_deck_path,
        first.deck_bytes + b"* changed\n",
        first.deck_sha256,
        first.expected_probes,
        first.receipt,
    )
    second = _condition("characterize.nominal.dc_1", sample_index=1, sample_value=0.5)
    calls: list[str] = []

    def fake_executor(item, *, timeout_s):
        calls.append(item.condition_id)
        return SimulatorExecution(0, b"", b"", b"raw", "ngspice-46")

    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform("const", (0.0,), {"v(out)": (0.5,)}),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=fake_executor,
        _prepared_compilation=_compilation(plan, [corrupt, second]),
    )
    assert calls == [second.condition_id]
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "invalid"
    assert "do not match compiler digest" in result.attempts[0].reason


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is unavailable")
def test_native_ngspice_rc_execution_smoke(tmp_path: Path) -> None:
    plan = _prepared_plan(tmp_path)
    compilation = prepare_testbench_plan_ngspice(plan, corner="tt")
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=HostNgspiceExecutor(),
        timeout_s=30,
        _prepared_compilation=compilation,
    )
    assert len(result.attempts) == len(compilation.conditions) == 1
    assert result.attempts[0].returncode == 0
    assert result.attempts[0].waveform_sha256 != _sha(b"")
    assert result.observables["observables"].get("output_curve")


def test_until_delta_and_transient_fixed_settle_fail_closed_before_execution(
    tmp_path: Path
) -> None:
    plan = _prepared_plan(tmp_path)
    point = plan.document["stages"][0]["points"][0]
    point["settle_policy"] = {
        "kind": "until_delta",
        "probe_id": "output_voltage",
        "tolerance": {"value": 1e-6, "unit": "V"},
        "hold_for": {"value": 1e-10, "unit": "s"},
        "maximum": {"value": 1e-9, "unit": "s"},
    }
    condition = _condition("characterize.nominal")
    calls: list[str] = []

    def executor(item, *, timeout_s):
        calls.append(item.condition_id)
        raise AssertionError("settle refusal must precede simulator execution")

    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=executor,
        _prepared_compilation=_compilation(plan, [condition]),
    )
    assert calls == []
    assert result.attempts[0].status == "invalid"
    assert "until_delta" in result.attempts[0].reason

    point["settle_policy"] = {
        "kind": "fixed_time",
        "duration": {"value": 1e-9, "unit": "s"},
    }
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=executor,
        _prepared_compilation=_compilation(plan, [condition]),
    )
    assert calls == []
    assert "nonzero transient fixed_time" in result.attempts[0].reason


def test_validity_names_are_unambiguous_when_rule_ids_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path)
    first = plan.document["stages"][0]["points"][0]
    second = deepcopy(first)
    second["id"] = "second"
    plan.document["stages"][0]["points"].append(second)
    first_condition = _condition("characterize.nominal")
    second_condition = _condition("characterize.second")
    second_condition = CompiledCondition(
        stage_id="characterize",
        point_id="second",
        condition_id=second_condition.condition_id,
        condition_sha256=second_condition.condition_sha256,
        relative_deck_path=second_condition.relative_deck_path,
        deck_bytes=second_condition.deck_bytes,
        deck_sha256=second_condition.deck_sha256,
        expected_probes=second_condition.expected_probes,
        receipt={
            **second_condition.receipt,
            "point_id": "second",
        },
    )
    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform("time", (0.0, 1.0), {"v(out)": (0.0, 1.0)}),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=lambda *_args, **_kwargs: SimulatorExecution(0, b"", b"", b"raw", "ngspice-46"),
        _prepared_compilation=_compilation(plan, [first_condition, second_condition]),
    )
    assert set(result.observables["validity"]) == {
        "characterize.nominal.waveform_finite",
        "characterize.second.waveform_finite",
    }


def test_atomic_output_publication_is_new_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path)
    condition = _condition("characterize.nominal")
    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform("time", (0.0, 1.0), {"v(out)": (0.0, 1.0)}),
    )
    target = tmp_path / "published"
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=lambda *_args, **_kwargs: SimulatorExecution(0, b"", b"", b"raw", "ngspice-46"),
        _prepared_compilation=_compilation(plan, [condition]),
        output_dir=target,
    )
    assert json.loads((target / "observables.json").read_text()) == result.observables
    assert json.loads((target / "run-receipt.json").read_text()) == result.receipt
    with pytest.raises(ValueError, match="absent or empty"):
        runner.publish_testbench_plan_run(result, target)

    linked = tmp_path / "linked-output"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        runner.publish_testbench_plan_run(result, linked)


def test_production_runner_compiles_stages_topologically_with_receipted_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path)
    upstream = plan.document["stages"][0]
    upstream["points"][0]["measurements"].append(
        {
            "id": "half_crossing",
            "kind": "crossing",
            "unit": "s",
            "input_measurement_id": "output_curve",
            "threshold": {"value": 0.5, "unit": "V"},
            "direction": "rising",
            "occurrence": 1,
        }
    )
    downstream = deepcopy(upstream)
    downstream["id"] = "dependent"
    downstream["depends_on"] = ["characterize"]
    downstream["inputs"] = [{"id": "center", "unit": "s"}]
    downstream["points"][0]["condition"]["parameters"] = [
        {"name": "center", "value": {"input_id": "center", "unit": "s"}}
    ]
    downstream["points"][0]["measurements"] = [
        downstream["points"][0]["measurements"][0]
    ]
    plan.document["stages"].append(downstream)
    plan.document["bindings"] = [
        {
            "id": "bind_center",
            "from": {
                "stage_id": "characterize",
                "point_id": "nominal",
                "measurement_id": "half_crossing",
                "component": "crossing",
            },
            "to": {"stage_id": "dependent", "input_id": "center"},
            "unit": "s",
            "transform": {"kind": "identity"},
        }
    ]
    plan.document["observables"].append(
        {
            "id": "dependent_curve",
            "source": {
                "kind": "measurement",
                "stage_id": "dependent",
                "point_id": "nominal",
                "measurement_id": "output_curve",
            },
            "unit": "V",
            "shape": "curve",
            "lineage": deepcopy(plan.document["observables"][0]["lineage"]),
        }
    )
    compiled_stage_ids: list[str] = []
    received_bindings = []

    def fake_prepare(_plan, *, stage_ids, binding_values, **_kwargs):
        stage_id = stage_ids[0]
        compiled_stage_ids.append(stage_id)
        received_bindings.append(tuple(binding_values))
        condition = _condition(f"{stage_id}.nominal")
        condition = CompiledCondition(
            stage_id=stage_id,
            point_id="nominal",
            condition_id=condition.condition_id,
            condition_sha256=condition.condition_sha256,
            relative_deck_path=condition.relative_deck_path,
            deck_bytes=condition.deck_bytes,
            deck_sha256=condition.deck_sha256,
            expected_probes=condition.expected_probes,
            receipt={**condition.receipt, "stage_id": stage_id},
        )
        return _compilation(plan, [condition])

    monkeypatch.setattr(runner, "prepare_testbench_plan_ngspice", fake_prepare)
    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform("time", (0.0, 1.0), {"v(out)": (0.0, 1.0)}),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=lambda *_args, **_kwargs: SimulatorExecution(0, b"", b"", b"raw", "ngspice-46"),
    )
    assert compiled_stage_ids == ["characterize", "dependent"]
    assert received_bindings[0] == ()
    assert len(received_bindings[1]) == 1
    assert received_bindings[1][0].binding_id == "bind_center"
    assert received_bindings[1][0].value == pytest.approx(0.5)
    assert len(received_bindings[1][0].source_receipt_sha256) == 64
    assert len(result.attempts) == 2
    binding_receipt = result.receipt["binding_receipts"][0]
    assert binding_receipt["sha256"] == _sha(
        _canonical(binding_receipt["receipt"])
    )
    assert (
        received_bindings[1][0].source_receipt_sha256
        == binding_receipt["sha256"]
    )
    dependent_lineage = next(
        row for row in result.observables["metadata"]["lineage"]
        if row["observable"] == "dependent_curve"
    )
    assert dependent_lineage["condition_ids"] == [
        "characterize.nominal",
        "dependent.nominal",
    ]
    condition_rows = {
        row["id"]: row for row in result.observables["metadata"]["conditions"]
    }
    assert "dependent_curve" in condition_rows["characterize.nominal"]["observables"]
    assert "dependent_curve" in condition_rows["dependent.nominal"]["observables"]


def test_envelope_uses_exact_captured_hidden_dut_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path)
    hidden_dut = _dut_body() + b"* hidden variant\n"
    compilation = _compilation(
        plan, [_condition("characterize.nominal")], dut_bytes=hidden_dut
    )
    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform(
            "time", (0.0, 1.0), {"v(out)": (0.0, 1.0)}
        ),
    )
    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=lambda *_args, **_kwargs: SimulatorExecution(
            0, b"", b"", b"raw", "ngspice-46"
        ),
        _prepared_compilation=compilation,
    )
    assert result.observables["dut_sha256"] == _sha(hidden_dut)
    assert result.receipt["dut_sha256"] == _sha(hidden_dut)
    assert result.observables["dut_sha256"] != plan.dut.sha256


def test_host_executor_environment_has_no_home_or_ambient_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environments: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        environment = dict(kwargs["env"])
        environments.append(environment)
        assert "HOME" not in environment
        if "--version-small" in argv:
            return SimpleNamespace(stdout=b"ngspice-46", stderr=b"", returncode=0)
        waveform = Path(argv[argv.index("-r") + 1])
        waveform.write_bytes(b"raw waveform")
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(runner.shutil, "which", lambda _binary: str(tmp_path / "ngspice"))
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    executor = HostNgspiceExecutor()
    execution = executor(_condition("characterize.nominal"), timeout_s=3)
    assert execution.waveform_bytes == b"raw waveform"
    assert len(environments) == 2
    assert set(environments[0]) == {"LANG", "LC_ALL", "PATH"}
    assert set(environments[1]) == {
        "LANG", "LC_ALL", "PATH", "SPICE_ASCIIRAWFILE", "TMPDIR"
    }


def test_stage_compile_failure_receipts_every_literal_fresh_dc_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _prepared_plan(tmp_path, dc=True)
    calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "prepare_testbench_plan_ngspice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("binding unavailable")
        ),
    )

    def should_not_execute(item, *, timeout_s):
        calls.append(item.condition_id)
        raise AssertionError("an uncompiled condition cannot execute")

    result = execute_testbench_plan_ngspice(
        plan, corner="tt", executor=should_not_execute
    )
    assert calls == []
    assert len(result.attempts) == 3
    assert all(not item.simulator_invoked for item in result.attempts)
    assert all(item.status == "invalid" for item in result.attempts)
    assert result.receipt["condition_inventory_complete"] is True
    assert result.receipt["expected_condition_count"] == 3
    assert result.receipt["simulator_invocation_count"] == 0
    assert result.receipt["not_executed_condition_count"] == 3
    assert "output_curve" not in result.observables["observables"]


def test_duplicate_compiler_condition_ids_refuse_before_execution(
    tmp_path: Path
) -> None:
    plan = _prepared_plan(tmp_path)
    condition = _condition("characterize.nominal")
    calls: list[str] = []

    def executor(item, *, timeout_s):
        calls.append(item.condition_id)
        return SimulatorExecution(0, b"", b"", b"raw", "ngspice-46")

    with pytest.raises(ValueError, match="duplicate condition"):
        execute_testbench_plan_ngspice(
            plan,
            corner="tt",
            executor=executor,
            _prepared_compilation=_compilation(plan, [condition, condition]),
        )
    assert calls == []


def test_unsupported_validity_is_unknown_and_cannot_look_like_dut_invalidity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "rc_dut.spice"
    artifact.write_bytes(_dut_body())
    document = _plan_document(artifact)
    document["stages"][0]["points"][0]["validity_rules"] = [
        {
            "id": "observed_pulse_count",
            "kind": "pulse_count",
            "stimulus_id": "input_pulses",
            "minimum_count": 2,
            "maximum_count": 2,
            "on_fail": {"verdict": "invalid", "reason": "pulse_count"},
        }
    ]
    plan, issues = validate_testbench_plan(document)
    assert issues == [] and plan is not None
    condition = _condition("characterize.nominal")
    monkeypatch.setattr(
        runner,
        "_extract_waveform",
        lambda *_: runner._Waveform(
            "time", (0.0, 1.0), {"v(out)": (0.0, 1.0)}
        ),
    )

    result = execute_testbench_plan_ngspice(
        plan,
        corner="tt",
        executor=lambda *_args, **_kwargs: SimulatorExecution(
            0, b"", b"", b"raw", "ngspice-46"
        ),
        _prepared_compilation=_compilation(plan, [condition]),
    )

    assert result.observables["validity"]["observed_pulse_count"].startswith(
        "UNKNOWN(runner:"
    )
    assert any(
        refusal.code == "testbench_plan.runner.validity_unsupported"
        for refusal in result.refusals
    )
