"""Tests for the pure testbench-plan oracle comparator."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from openada.operations.testbench_oracle import (
    COMPARISON_SCHEMA,
    TOLERANCE_SCHEMA,
    compare_testbench_observables,
)


FIXTURES = Path(__file__).parent / "fixtures" / "testbench-plan"
ROOT = Path(__file__).parents[1]


def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _sha(character: str) -> str:
    return character * 64


def _legacy(
    observables: dict,
    *,
    validity: dict[str, str] | None = None,
    corner: str = "tt_27c_1v20",
) -> dict:
    return {
        "sizing": {"topology": "synthetic", "parameters": {}},
        "corner": corner,
        "validity": validity or {"dc": "VALID"},
        "observables": observables,
    }


def _typed(observables: dict, validity: dict[str, str] | None = None) -> dict:
    names = list(observables)
    return {
        "schema": "simra.testbench-observables/v1",
        "plan_sha256": _sha("a"),
        "dut_sha256": _sha("b"),
        "corner": "tt_27c_1v20",
        "validity": validity or {"dc": "VALID"},
        "observables": observables,
        "metadata": {
            "grading_runtime_s": 2.5,
            "conditions": [
                {
                    "id": "tt_point",
                    "observables": names,
                    "receipt": {
                        "compiled_deck_sha256": _sha("c"),
                        "waveform_sha256": _sha("d"),
                    },
                }
            ],
            "lineage": [
                {"observable": name, "condition_ids": ["tt_point"]}
                for name in names
            ],
            "extensions": {},
        },
        "extensions": {},
    }


def _spec(*rows: dict, lineage_required: bool = False) -> dict:
    return {
        "schema": TOLERANCE_SCHEMA,
        "metrics": list(rows),
        "lineage_required": lineage_required,
        "extensions": {},
    }


def _limit(value: float, unit: str, op: str = "<=") -> dict:
    return {"op": op, "value": value, "unit": unit}


def _scalar(
    name: str,
    field: str,
    *,
    limit: float,
    unit: str,
    error: dict,
    required: bool = True,
) -> dict:
    return {
        "name": name,
        "kind": "scalar",
        "required": required,
        "limit": _limit(limit, unit),
        "observed": field,
        "oracle": field,
        "error": error,
    }


def _metric(result: dict, name: str) -> dict:
    return next(item for item in result["metrics"] if item["name"] == name)


def test_absolute_error_is_stable_for_near_zero_leakage() -> None:
    oracle = _legacy({"leak_worst_a": 2.0e-12})
    observed = _legacy({"leak_worst_a": 4.0e-10})
    tolerance = _spec(
        _scalar(
            "leak_error",
            "leak_worst_a",
            limit=0.5e-9,
            unit="A",
            error={"kind": "absolute"},
        )
    )

    result = compare_testbench_observables(observed, oracle, tolerance)

    row = _metric(result, "leak_error")
    assert row["status"] == "PASS"
    assert row["value"] == pytest.approx(3.98e-10)
    assert row["details"]["absolute_error"] == pytest.approx(3.98e-10)


def test_relative_error_requires_an_explicit_positive_denominator_floor() -> None:
    tolerance = _spec(
        _scalar(
            "leak_error",
            "leak_worst_a",
            limit=0.5,
            unit="frac",
            error={"kind": "relative", "denominator_floor": 0.0},
        )
    )
    with pytest.raises(ValueError, match="denominator_floor"):
        compare_testbench_observables(
            _legacy({"leak_worst_a": 1e-12}),
            _legacy({"leak_worst_a": 0.0}),
            tolerance,
        )


def test_guarded_curve_uses_exact_grid_and_explicit_physical_floor() -> None:
    oracle = _legacy(
        {"src": {"v": [0.0, 0.5, 1.0], "a": [0.0, 10e-6, 20e-6]}}
    )
    observed = _legacy(
        {"src": {"v": [0.0, 0.5, 1.0], "a": [0.5e-6, 10.2e-6, 19e-6]}}
    )
    row = {
        "name": "src_curve_error",
        "kind": "curve",
        "required": True,
        "limit": _limit(0.05, "frac"),
        "observed": "src",
        "oracle": "src",
        "x": "v",
        "y": "a",
        "error": {"kind": "guarded_relative", "absolute_guard": 10e-6},
    }

    result = compare_testbench_observables(observed, oracle, _spec(row))

    metric = _metric(result, "src_curve_error")
    assert metric["status"] == "FAIL"
    assert metric["value"] == pytest.approx(0.05)
    assert metric["details"]["absolute_guard"] == 10e-6

    shifted = deepcopy(observed)
    shifted["observables"]["src"]["v"][1] = 0.6
    result = compare_testbench_observables(shifted, oracle, _spec(row))
    assert _metric(result, "src_curve_error")["status"] == "UNKNOWN"
    assert "no interpolation" in _metric(result, "src_curve_error")["reason"]


def test_mismatch_curve_uses_magnitudes_and_absolute_fraction_error() -> None:
    oracle = _legacy({"src_q": [10.0, 10.0], "snk_q": [8.0, 9.0]})
    observed = _legacy({"src_q": [10.0, 10.0], "snk_q": [-7.0, -9.5]})
    row = {
        "name": "mismatch_curve_error",
        "kind": "mismatch_curve",
        "required": True,
        "limit": _limit(0.11, "frac_abs"),
        "observed_source": "src_q",
        "observed_sink": "snk_q",
        "oracle_source": "src_q",
        "oracle_sink": "snk_q",
        "denominator_floor": 1e-15,
    }

    result = compare_testbench_observables(observed, oracle, _spec(row))

    metric = _metric(result, "mismatch_curve_error")
    assert metric["status"] == "PASS"
    assert metric["value"] == pytest.approx(0.1)
    assert metric["details"]["worst_index"] == 0


def test_compliance_endpoint_row_refuses_to_infer_endpoints_from_span() -> None:
    row = {
        "name": "compliance_endpoint_error",
        "kind": "compliance_endpoints",
        "required": True,
        "limit": _limit(25e-3, "V"),
        "observed": "compliance_endpoints_v",
        "oracle": "compliance_endpoints_v",
    }
    result = compare_testbench_observables(
        _legacy({"compliance_span_v": 0.865}),
        _legacy({"compliance_span_v": 0.865}),
        _spec(row),
    )

    metric = _metric(result, "compliance_endpoint_error")
    assert metric["status"] == "UNKNOWN"
    assert "endpoints" in metric["reason"]
    assert result["status"] == "UNKNOWN"


def test_signed_response_coverage_preserves_polarity() -> None:
    oracle = _legacy({"phase": {"dt": [-1.0, 0.0, 1.0], "q": [-2.0, 0.0, 2.0]}})
    observed = _legacy({"phase": {"dt": [-1.0, 0.0, 1.0], "q": [-1.0, 0.0, -1.0]}})
    row = {
        "name": "signed_response_coverage",
        "kind": "signed_response_coverage",
        "required": True,
        "limit": _limit(0.9, "frac", op=">="),
        "observed": "phase",
        "oracle": "phase",
        "x": "dt",
        "y": "q",
        "zero_epsilon": 1e-15,
    }

    result = compare_testbench_observables(observed, oracle, _spec(row))

    metric = _metric(result, "signed_response_coverage")
    assert metric["status"] == "FAIL"
    assert metric["value"] == 0.5
    assert metric["details"] == {
        "eligible": 2,
        "matching_sign": 1,
        "zero_epsilon": 1e-15,
    }


def test_validity_recall_and_false_valid_have_nonredundant_denominators() -> None:
    oracle = _legacy(
        {},
        validity={
            "good": "VALID",
            "bad_detected": "INVALID(design: rail)",
            "bad_missed": "INVALID(design: missed pulse)",
        },
    )
    observed = _legacy(
        {},
        validity={
            "good": "VALID",
            "bad_detected": "NEEDS_FINE_SWEEP(non-monotone)",
            "bad_missed": "VALID",
        },
    )
    recall = {
        "name": "invalid_detection_recall",
        "kind": "invalid_detection_recall",
        "required": True,
        "limit": _limit(0.8, "frac", op=">="),
        "denominator": "oracle_invalid",
    }
    false_valid = {
        "name": "false_valid_rate",
        "kind": "false_valid_rate",
        "required": True,
        "limit": _limit(0.1, "frac"),
        "denominator": "submitted_valid",
    }

    result = compare_testbench_observables(
        observed, oracle, _spec(recall, false_valid)
    )

    assert _metric(result, "invalid_detection_recall")["value"] == 0.5
    assert _metric(result, "false_valid_rate")["value"] == 0.5
    assert result["validity"] == {
        "oracle_invalid": 2,
        "detected_invalid": 1,
        "observed_valid": 2,
        "false_valid": 1,
        "missing_or_unknown": 0,
    }

    wrong_policy = deepcopy(recall)
    wrong_policy["denominator"] = "submitted_valid"
    with pytest.raises(ValueError, match="denominator"):
        compare_testbench_observables(
            observed, oracle, _spec(wrong_policy, false_valid)
        )


def test_lineage_required_turns_untraced_numeric_value_unknown() -> None:
    row = _scalar(
        "offset_error_vs_oracle",
        "zero_cross_offset_s",
        limit=25e-12,
        unit="s",
        error={"kind": "absolute"},
    )
    oracle = _legacy({"zero_cross_offset_s": 100e-12})
    observed = _legacy({"zero_cross_offset_s": 101e-12})

    result = compare_testbench_observables(
        observed, oracle, _spec(row, lineage_required=True)
    )
    assert _metric(result, "offset_error_vs_oracle")["status"] == "UNKNOWN"
    assert "receipt lineage" in _metric(result, "offset_error_vs_oracle")["reason"]

    traced = _typed({"zero_cross_offset_s": 101e-12})
    result = compare_testbench_observables(
        traced, oracle, _spec(row, lineage_required=True)
    )
    assert _metric(result, "offset_error_vs_oracle")["status"] == "PASS"


def test_completeness_runtime_and_lineage_rows_use_execution_metadata() -> None:
    observed = _typed({"a": 1.0, "b": 2.0})
    oracle = _legacy({})
    completeness = {
        "name": "completeness",
        "kind": "completeness",
        "required": True,
        "limit": _limit(0.95, "frac", op=">="),
        "observables": ["a", "b"],
        "conditions": ["tt_point"],
    }
    runtime = {
        "name": "grading_runtime",
        "kind": "grading_runtime",
        "required": True,
        "limit": _limit(900.0, "s"),
    }
    lineage = {
        "name": "lineage_presence",
        "kind": "lineage_presence",
        "required": True,
        "limit": _limit(1.0, "frac", op=">="),
        "observables": ["a", "b"],
        "conditions": ["tt_point"],
    }

    result = compare_testbench_observables(
        observed, oracle, _spec(completeness, runtime, lineage)
    )

    assert result["schema"] == COMPARISON_SCHEMA
    assert result["status"] == "PASS"
    assert _metric(result, "completeness")["value"] == 1.0
    assert _metric(result, "grading_runtime")["value"] == 2.5
    assert _metric(result, "lineage_presence")["value"] == 1.0
    validator = Draft202012Validator(
        _schema("testbench-oracle-comparison-v1.schema.json")
    )
    assert not list(validator.iter_errors(result))


def test_unknown_required_row_prevents_overall_pass_but_optional_unknown_does_not() -> None:
    required = _scalar(
        "offset_error",
        "missing",
        limit=1.0,
        unit="s",
        error={"kind": "absolute"},
    )
    result = compare_testbench_observables(_legacy({}), _legacy({}), _spec(required))
    assert result["status"] == "UNKNOWN"
    assert result["summary"]["required_unknown"] == 1

    required["required"] = False
    result = compare_testbench_observables(_legacy({}), _legacy({}), _spec(required))
    assert result["status"] == "PASS"


def test_contract_rejects_unknown_fields_nonfinite_values_and_duplicate_metric_names() -> None:
    base = _scalar(
        "offset_error",
        "offset",
        limit=1.0,
        unit="s",
        error={"kind": "absolute"},
    )
    unknown = deepcopy(base)
    unknown["formula"] = "abs(a-b)"
    with pytest.raises(ValueError, match="unknown field"):
        compare_testbench_observables(
            _legacy({"offset": 0.0}),
            _legacy({"offset": 0.0}),
            _spec(unknown),
        )

    with pytest.raises(ValueError, match="finite"):
        compare_testbench_observables(
            _legacy({"offset": float("nan")}),
            _legacy({"offset": 0.0}),
            _spec(base),
        )

    with pytest.raises(ValueError, match="duplicates"):
        compare_testbench_observables(
            _legacy({"offset": 0.0}),
            _legacy({"offset": 0.0}),
            _spec(base, deepcopy(base)),
        )


def test_committed_oracle_reference_shape_is_consumable_and_exposes_contract_gaps() -> None:
    fixture = json.loads(
        (FIXTURES / "oracle_reference_tt.json").read_text(encoding="utf-8")
    )
    offset = _scalar(
        "offset_error_vs_oracle",
        "zero_cross_offset_s",
        limit=25e-12,
        unit="s",
        error={"kind": "absolute"},
    )
    gain = _scalar(
        "local_gain_error",
        "local_gain_a",
        limit=0.15,
        unit="frac",
        error={"kind": "relative", "denominator_floor": 1e-12},
    )
    endpoints = {
        "name": "compliance_endpoint_error",
        "kind": "compliance_endpoints",
        "required": True,
        "limit": _limit(25e-3, "V"),
        "observed": "compliance_endpoints_v",
        "oracle": "compliance_endpoints_v",
    }

    result = compare_testbench_observables(
        fixture, fixture, _spec(offset, gain, endpoints)
    )

    assert _metric(result, "offset_error_vs_oracle")["status"] == "PASS"
    assert _metric(result, "local_gain_error")["status"] == "PASS"
    assert _metric(result, "compliance_endpoint_error")["status"] == "UNKNOWN"
    assert result["status"] == "UNKNOWN"


def test_three_closed_json_schemas_are_valid_and_reject_unknown_root_fields() -> None:
    schemas = {
        "observables": _schema("testbench-observables-v1.schema.json"),
        "tolerances": _schema("testbench-oracle-tolerances-v1.schema.json"),
        "comparison": _schema("testbench-oracle-comparison-v1.schema.json"),
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    observed = _typed({"value": 1.0})
    tolerance = _spec(
        _scalar(
            "value_error",
            "value",
            limit=0.0,
            unit="1",
            error={"kind": "absolute"},
        )
    )
    comparison = compare_testbench_observables(
        observed, _legacy({"value": 1.0}), tolerance
    )
    documents = {
        "observables": observed,
        "tolerances": tolerance,
        "comparison": comparison,
    }
    for name, document in documents.items():
        validator = Draft202012Validator(schemas[name])
        assert not list(validator.iter_errors(document))
        polluted = deepcopy(document)
        polluted["undeclared"] = True
        assert list(validator.iter_errors(polluted))


def test_ratified_tolerance_fixture_covers_all_twelve_rows_and_scores() -> None:
    tolerance = json.loads(
        (FIXTURES / "ratified-v0-tolerances.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(
        _schema("testbench-oracle-tolerances-v1.schema.json")
    )
    assert not list(validator.iter_errors(tolerance))
    assert [row["name"] for row in tolerance["metrics"]] == [
        "signed_response_coverage",
        "offset_error_vs_oracle",
        "local_gain_error",
        "src_curve_error",
        "snk_curve_error",
        "mismatch_curve_error",
        "compliance_endpoint_error",
        "leak_error",
        "invalid_detection_recall",
        "false_valid_rate",
        "completeness",
        "grading_runtime",
    ]

    oracle = json.loads(
        (FIXTURES / "oracle_reference_tt.json").read_text(encoding="utf-8")
    )
    oracle["observables"]["compliance_endpoints_v"] = {
        "lo_v": 0.2,
        "hi_v": 1.065,
    }
    oracle["observables"]["signed_charge_vs_phase_offset"] = {
        "phase_offset_s": [-1e-10, 1e-10],
        "charge_c": [-1e-14, 1e-14],
    }
    oracle["validity"]["hidden_invalid"] = "INVALID(design: missed pulse)"
    observed = _typed(deepcopy(oracle["observables"]), deepcopy(oracle["validity"]))
    observed["metadata"]["conditions"][0]["id"] = "tt_27c_1v20"
    for lineage in observed["metadata"]["lineage"]:
        lineage["condition_ids"] = ["tt_27c_1v20"]

    result = compare_testbench_observables(observed, oracle, tolerance)

    assert len(result["metrics"]) == 12
    assert {row["status"] for row in result["metrics"]} == {"PASS"}
    assert result["status"] == "PASS"
    comparison_validator = Draft202012Validator(
        _schema("testbench-oracle-comparison-v1.schema.json")
    )
    assert not list(comparison_validator.iter_errors(result))


def test_completeness_inventory_is_independent_from_lineage_coverage() -> None:
    observed = _typed({"a": 1.0})
    observed["metadata"]["lineage"] = []
    completeness = {
        "name": "completeness",
        "kind": "completeness",
        "required": True,
        "limit": _limit(1.0, "frac", op=">="),
        "observables": ["a"],
        "conditions": ["tt_point"],
    }
    lineage = {
        "name": "lineage_presence",
        "kind": "lineage_presence",
        "required": True,
        "limit": _limit(1.0, "frac", op=">="),
        "observables": ["a"],
        "conditions": ["tt_point"],
    }

    result = compare_testbench_observables(
        observed, _legacy({}), _spec(completeness, lineage)
    )

    assert _metric(result, "completeness")["status"] == "PASS"
    assert _metric(result, "lineage_presence")["status"] == "FAIL"

    claimed_without_value = deepcopy(observed)
    claimed_without_value["observables"] = {}
    result = compare_testbench_observables(
        claimed_without_value, _legacy({}), _spec(completeness)
    )
    assert _metric(result, "completeness")["status"] == "FAIL"


def test_numeric_lineage_requires_a_contributing_receipt_not_every_plan_condition() -> None:
    observed = _typed({"offset": 1.0})
    observed["metadata"]["conditions"].append(
        {
            "id": "unrelated_dc",
            "observables": [],
            "receipt": {
                "compiled_deck_sha256": _sha("e"),
                "waveform_sha256": _sha("f"),
            },
        }
    )
    row = _scalar(
        "offset_error",
        "offset",
        limit=0.0,
        unit="s",
        error={"kind": "absolute"},
    )

    result = compare_testbench_observables(
        observed,
        _legacy({"offset": 1.0}),
        _spec(row, lineage_required=True),
    )

    assert _metric(result, "offset_error")["status"] == "PASS"


def test_cross_corner_values_cannot_produce_an_overall_pass() -> None:
    row = _scalar(
        "offset_error",
        "offset",
        limit=0.0,
        unit="s",
        error={"kind": "absolute"},
    )
    result = compare_testbench_observables(
        _legacy({"offset": 1.0}, corner="ff"),
        _legacy({"offset": 1.0}, corner="ss"),
        _spec(row),
    )
    assert result["corner_match"] is False
    assert _metric(result, "offset_error")["status"] == "UNKNOWN"
    assert result["status"] == "UNKNOWN"


def test_fraction_absolute_is_typed_and_rejects_nonfractions() -> None:
    row = _scalar(
        "fraction_error",
        "mismatch",
        limit=0.03,
        unit="frac_abs",
        error={"kind": "fraction_absolute"},
    )
    result = compare_testbench_observables(
        _legacy({"mismatch": 0.22}), _legacy({"mismatch": 0.20}), _spec(row)
    )
    assert _metric(result, "fraction_error")["status"] == "PASS"
    assert _metric(result, "fraction_error")["value"] == pytest.approx(0.02)

    result = compare_testbench_observables(
        _legacy({"mismatch": 2.0}), _legacy({"mismatch": 0.20}), _spec(row)
    )
    assert _metric(result, "fraction_error")["status"] == "UNKNOWN"


def test_mismatch_curve_refuses_ambiguous_mapping_values() -> None:
    row = {
        "name": "mismatch_curve_error",
        "kind": "mismatch_curve",
        "required": True,
        "limit": _limit(0.03, "frac_abs"),
        "observed_source": "src",
        "observed_sink": "snk",
        "oracle_source": "src",
        "oracle_sink": "snk",
        "denominator_floor": 1e-18,
    }
    curves = {
        "src": {"v": [0.3, 0.6], "a": [1.0, 1.0]},
        "snk": {"v": [0.3, 0.6], "a": [0.9, 0.9]},
    }
    result = compare_testbench_observables(
        _legacy(curves), _legacy(curves), _spec(row)
    )
    assert _metric(result, "mismatch_curve_error")["status"] == "UNKNOWN"
