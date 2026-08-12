"""Closed-contract tests for ``simra.testbench-plan/v1``."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from openada.operations.testbench_plan import (
    TESTBENCH_PLAN_SCHEMA,
    load_testbench_plan_schema,
    validate_testbench_plan,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "testbench-plan"
    / "closed_multistage_plan.json"
)


def plan() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def loop_plan() -> dict:
    """Return the full staged characterization and loop-grading fixture."""

    return plan()


def codes(document: dict) -> set[str]:
    prepared, issues = validate_testbench_plan(document)
    assert prepared is None
    return {issue.code for issue in issues}


def test_published_schema_and_multistage_fixture_validate() -> None:
    schema = load_testbench_plan_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator(schema).validate(plan())


def test_validator_prepares_typed_graph_and_stable_digests() -> None:
    document = plan()
    prepared_a, issues_a = validate_testbench_plan(document)
    prepared_b, issues_b = validate_testbench_plan(deepcopy(document))
    assert issues_a == issues_b == []
    assert prepared_a is not None and prepared_b is not None
    assert prepared_a.identifier == "charge_pump_characterization"
    assert prepared_a.document["schema"] == TESTBENCH_PLAN_SCHEMA
    assert [stage.identifier for stage in prepared_a.stages] == [
        "dc_characterize",
        "pulse_characterize",
        "phase_characterize",
        "loop_grade",
    ]
    assert prepared_a.canonical_sha256 == prepared_b.canonical_sha256
    assert prepared_a.dut_binding_canonical_sha256 == prepared_b.dut_binding_canonical_sha256


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ((), "simulator_options"),
        (("stages", 0, "points", 0, "analysis"), "raw_spice"),
        (("stimuli", 0), "behavioral_source"),
        (("dut",), "include_text"),
    ],
)
def test_unknown_cheat_surface_fields_are_rejected(location, field) -> None:
    document = plan()
    target = document
    for key in location:
        target = target[key]
    target[field] = "alter all"
    assert "testbench_plan.document.unknown_field" in codes(document)


def test_strict_file_loader_rejects_duplicate_keys_and_nonfinite(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    prepared, issues = validate_testbench_plan(duplicate)
    assert prepared is None
    assert issues[0].code == "testbench_plan.document.invalid"
    assert "duplicate JSON object key" in issues[0].message

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    prepared, issues = validate_testbench_plan(nonfinite)
    assert prepared is None
    assert "non-finite JSON number" in issues[0].message


def test_mapping_with_unbounded_number_returns_issue_instead_of_crashing() -> None:
    document = plan()
    document["supplies"][0]["voltage"]["escape"] = 10**10000
    assert "testbench_plan.document.invalid" in codes(document)

    document = plan()
    document["corner_bindings"][0]["values"][0]["value"]["value"] = 10**10000
    assert "testbench_plan.document.invalid" in codes(document)


def test_delivered_charge_cannot_probe_the_command_source() -> None:
    document = plan()
    measurement = document["stages"][1]["points"][0]["measurements"][1]
    measurement["probe_id"] = "command_current"
    assert "testbench_plan.measurement.probe_cheat" in codes(document)


def test_internal_probe_requires_explicit_dut_abi_exposure() -> None:
    document = plan()
    document["probes"][2]["node"] = "SECRET_NODE"
    assert "testbench_plan.probe.internal_node_unexposed" in codes(document)


def test_stimulus_current_probe_names_one_exact_physical_branch() -> None:
    document = plan()
    probe = document["probes"][3]
    probe["stimulus_id"] = "phase_pair"
    assert "testbench_plan.probe.branch_incompatible" in codes(document)

    probe["branch"] = "reference"
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None

    probe["stimulus_id"] = "up_pulses"
    assert "testbench_plan.probe.branch_incompatible" in codes(document)


def test_dut_override_can_change_only_locator_and_digest() -> None:
    document = plan()
    override = deepcopy(document["dut"])
    override["artifact"] = "/hidden/variant_17.spice"
    override["sha256"] = "f" * 64
    prepared, issues = validate_testbench_plan(document, dut_binding=override)
    assert issues == []
    assert prepared is not None
    assert prepared.dut.artifact == "/hidden/variant_17.spice"
    assert prepared.dut.sha256 == "f" * 64

    override["top"] = "SHADOW_DUT"
    assert "testbench_plan.dut.abi_mismatch" in codes_with_override(document, override)


def codes_with_override(document: dict, override: dict) -> set[str]:
    prepared, issues = validate_testbench_plan(document, dut_binding=override)
    assert prepared is None
    return {issue.code for issue in issues}


def test_active_stimuli_select_exact_sources_and_reject_target_conflicts() -> None:
    document = plan()
    bias = {
        "id": "output_bias",
        "kind": "dc_state",
        "target_port": "OUT",
        "supply_id": "core_supply",
        "state": "bias",
        "level": {
            "kind": "supply_scaled",
            "supply_id": "core_supply",
            "fraction": 0.5,
        },
    }
    document["stimuli"].append(bias)
    document["stages"][0]["points"][0]["active_stimulus_ids"].append("output_bias")
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None

    duplicate = deepcopy(bias)
    duplicate["id"] = "other_output_bias"
    duplicate["level"]["fraction"] = 0.75
    document["stimuli"].append(duplicate)
    document["stages"][0]["points"][0]["active_stimulus_ids"].append(
        "other_output_bias"
    )
    assert "testbench_plan.stimulus.target_conflict" in codes(document)


def test_supply_scaled_threshold_and_independent_phase_polarities_validate() -> None:
    document = plan()
    crossing_rule = document["stages"][1]["points"][0]["validity_rules"][2]
    crossing_rule["threshold"] = {
        "kind": "supply_scaled",
        "supply_id": "core_supply",
        "fraction": 0.5,
    }
    phase = document["stimuli"][2]
    assert phase["reference_polarity"] != phase["offset_polarity"]
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None


def test_settle_policy_distinguishes_dc_solver_from_elapsed_time() -> None:
    document = plan()
    dc_point = document["stages"][0]["points"][0]
    dc_point["settle_policy"] = {
        "kind": "fixed_time",
        "duration": {"value": 0, "unit": "s"},
    }
    assert "testbench_plan.settle.analysis_incompatible" in codes(document)

    document = plan()
    transient_point = document["stages"][1]["points"][0]
    transient_point["settle_policy"] = {"kind": "operating_point"}
    assert "testbench_plan.settle.analysis_incompatible" in codes(document)


def test_point_curve_cannot_mislabel_analysis_axis_as_condition_parameter() -> None:
    document = plan()
    curve = document["stages"][2]["points"][0]["measurements"][0]
    curve["axis"] = {"kind": "condition_parameter", "parameter": "phase_offset"}

    assert "testbench_plan.document.unknown_field" in codes(document)


def test_explicit_pulse_normalization_count_closes_measurement_window() -> None:
    document = plan()
    measurement = document["stages"][1]["points"][0]["measurements"][1]
    measurement["normalization"]["count"] = 7
    assert "testbench_plan.measurement.normalization_invalid" in codes(document)


def test_stage_dependency_cycle_and_unbound_input_are_rejected() -> None:
    document = plan()
    document["stages"][0]["depends_on"] = ["phase_characterize"]
    assert "testbench_plan.graph.cycle" in codes(document)

    document = plan()
    document["bindings"] = []
    assert "testbench_plan.binding.input_unbound" in codes(document)


def test_reduction_collection_selection_and_closed_unit_algebra() -> None:
    document = plan()
    stage = document["stages"][1]
    stage["reductions"] = [
        {
            "id": "charge_samples",
            "kind": "collect_array",
            "unit": "C",
            "items": [
                {
                    "source": {
                        "point_id": "nominal",
                        "measurement_id": "delivered_charge",
                        "component": "value",
                    },
                    "condition_values": [
                        {"name": "output_bias", "value": {"value": 0.6, "unit": "V"}}
                    ],
                }
            ],
        },
        {
            "id": "qnom",
            "kind": "select",
            "unit": "C",
            "input_reduction_id": "charge_samples",
            "selector": {
                "kind": "condition_value",
                "name": "output_bias",
                "equals": {"value": 0.6, "unit": "V"},
            },
        },
        {
            "id": "q_scaled",
            "kind": "arithmetic",
            "unit": "C",
            "operator": "multiply",
            "operands": [
                {"value": 0.1, "unit": "1"},
                {"kind": "reduction", "reduction_id": "qnom", "component": "value", "unit": "C"},
            ],
        },
    ]
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None
    assert [item.identifier for item in prepared.stages[1].reductions] == [
        "charge_samples",
        "qnom",
        "q_scaled",
    ]

    stage["reductions"][-1]["unit"] = "V"
    assert "testbench_plan.unit.unsupported_algebra" in codes(document)


def test_fresh_policy_is_explicit_for_independent_analysis_samples() -> None:
    """Fresh means reinitialize every generated sample; carryover opts into order."""

    document = plan()
    point = document["stages"][0]["points"][0]
    assert point["state_policy"]["kind"] == "fresh"
    point["state_policy"] = {
        "kind": "carryover",
        "from": {"stage_id": "dc_characterize", "point_id": "nominal"},
    }
    assert "testbench_plan.graph.cycle" in codes(document)


def test_linear_ac_loop_stage_is_closed_typed_and_bound_from_local_fit() -> None:
    document = loop_plan()
    schema = load_testbench_plan_schema()
    Draft202012Validator(schema).validate(document)
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None
    assert [stage.identifier for stage in prepared.stages][-2:] == [
        "phase_characterize",
        "loop_grade",
    ]
    loop = prepared.stages[-1].points[0]
    assert loop.document["analysis"]["kind"] == "linear_ac"
    assert [item.kind for item in loop.measurements] == [
        "loop_transfer",
        "unity_frequency",
        "negative_feedback_phase_margin",
    ]
    assert document["bindings"][-1]["from"] == {
        "stage_id": "phase_characterize",
        "reduction_id": "local_phase_fit",
        "component": "slope",
    }


def test_linear_ac_rejects_wrong_units_sources_and_loop_identities() -> None:
    document = loop_plan()
    loop_point = document["stages"][-1]["points"][0]
    loop_point["analysis"]["start"]["unit"] = "s"
    assert "testbench_plan.unit.mismatch" in codes(document)

    document = loop_plan()
    document["probes"][-1]["response_probe_id"] = "command_current"
    assert "testbench_plan.probe.response_incompatible" in codes(document)

    document = loop_plan()
    document["probes"][-1]["construction"]["vco_gain"]["unit"] = "A"
    assert "testbench_plan.unit.mismatch" in codes(document)

    document = loop_plan()
    loop_measurements = document["stages"][-1]["points"][0]["measurements"]
    loop_measurements[2]["unity_frequency_measurement_id"] = "open_loop_transfer"
    assert "testbench_plan.measurement.parent_incompatible" in codes(document)

    document = loop_plan()
    document["stages"][-1]["points"][0]["validity_rules"][0][
        "measurement_id"
    ] = "loop_unity_frequency"
    assert "testbench_plan.validity.measurement_incompatible" in codes(document)


def test_small_signal_ac_participates_in_active_target_conflict_checks() -> None:
    document = loop_plan()
    document["stimuli"].append(
        {
            "id": "output_clamp",
            "kind": "dc_state",
            "target_port": "OUT",
            "supply_id": "core_supply",
            "state": "bias",
            "level": {
                "kind": "supply_scaled",
                "supply_id": "core_supply",
                "fraction": 0.5,
            },
        }
    )
    document["stages"][-1]["points"][0]["active_stimulus_ids"].append(
        "output_clamp"
    )
    assert "testbench_plan.stimulus.target_conflict" in codes(document)


def test_compliance_intersection_uses_independent_signed_references() -> None:
    document = plan()
    stage = document["stages"][2]
    stage["reductions"].append(
        {
            "id": "source_curve",
            "kind": "collect_curve",
            "unit": "C",
            "axis_unit": "s",
            "samples": [
                {
                    "x": {"value": -1.0, "unit": "s"},
                    "source": {
                        "point_id": "negative_offset",
                        "measurement_id": "phase_charge",
                        "component": "value",
                    },
                },
                {
                    "x": {"value": 1.0, "unit": "s"},
                    "source": {
                        "point_id": "negative_offset",
                        "measurement_id": "source_charge_for_compliance",
                        "component": "value",
                    },
                },
            ],
        }
    )
    # Use two distinct scalar measurements so each explicit curve has a closed,
    # unique source list without inventing samples or interpolation.
    point = stage["points"][0]
    companion = deepcopy(point["measurements"][1])
    companion["id"] = "source_charge_for_compliance"
    point["measurements"].append(companion)
    stage["reductions"].extend(
        [
            {
                "id": "sink_curve",
                "kind": "collect_curve",
                "unit": "C",
                "axis_unit": "s",
                "samples": [
                    {
                        "x": {"value": -1.0, "unit": "s"},
                        "source": {
                            "point_id": "negative_offset",
                            "measurement_id": "source_charge_for_compliance",
                            "component": "value",
                        },
                    },
                    {
                        "x": {"value": 1.0, "unit": "s"},
                        "source": {
                            "point_id": "negative_offset",
                            "measurement_id": "phase_charge",
                            "component": "value",
                        },
                    },
                ],
            },
            {
                "id": "compliance",
                "kind": "compliance_intersection",
                "unit": "s",
                "positive_curve_id": "source_curve",
                "negative_curve_id": "sink_curve",
                "positive_reference": {"value": 70e-15, "unit": "C"},
                "negative_reference": {"value": -65e-15, "unit": "C"},
                "relative_tolerance": {"value": 0.1, "unit": "1"},
            },
        ]
    )
    prepared, issues = validate_testbench_plan(document)
    assert issues == [] and prepared is not None

    stage["reductions"][-1]["negative_reference"]["unit"] = "A"
    assert "testbench_plan.unit.mismatch" in codes(document)
