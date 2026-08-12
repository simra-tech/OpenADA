#!/usr/bin/env python3
"""Tests for the evaluation-local analog-design knowledge graph spike."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import kg_query  # noqa: E402


SCHEMA_PATH = HERE / "kg-schema-v0.json"
SEED_PATH = HERE / "seed-pll.json"
QUERY_PATH = HERE / "kg_query.py"


def load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


SCHEMA = load(SCHEMA_PATH)
SEED = load(SEED_PATH)
VALIDATOR = Draft202012Validator(SCHEMA)


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12(self) -> None:
        Draft202012Validator.check_schema(SCHEMA)

    def test_seed_validates_against_closed_schema(self) -> None:
        errors = sorted(VALIDATOR.iter_errors(SEED), key=lambda error: list(error.path))
        self.assertEqual([], errors)

    def test_seed_inventory_is_complete_for_both_task_surfaces(self) -> None:
        nodes = SEED["nodes"]
        pll1_parameters = [
            node
            for node in nodes
            if node["kind"] == "Parameter"
            and node["attributes"]["task_id"] == "pll1-cplf-sg13g2"
        ]
        pll3_parameters = [
            node
            for node in nodes
            if node["kind"] == "Parameter"
            and node["attributes"]["task_id"] == "pll3-vco-sg13g2"
        ]
        pll1_specs = [
            node
            for node in nodes
            if node["kind"] == "SpecRow"
            and node["attributes"]["task_id"] == "pll1-cplf-sg13g2"
        ]
        pll3_specs = [
            node
            for node in nodes
            if node["kind"] == "SpecRow"
            and node["attributes"]["task_id"] == "pll3-vco-sg13g2"
        ]
        self.assertEqual(16, len(pll1_parameters))
        self.assertEqual(9, len(pll3_parameters))
        self.assertEqual(9, len(pll1_specs))
        self.assertEqual(9, len(pll3_specs))

    def test_unknown_edge_field_is_rejected(self) -> None:
        candidate = copy.deepcopy(SEED)
        candidate["edges"][0]["surprise"] = True
        errors = list(VALIDATOR.iter_errors(candidate))
        self.assertTrue(errors)
        self.assertTrue(
            any("Unevaluated properties" in error.message for error in errors),
            [error.message for error in errors],
        )

    def test_edge_without_evidence_pointer_is_rejected(self) -> None:
        candidate = copy.deepcopy(SEED)
        del candidate["edges"][0]["evidence"]["pointers"]
        errors = list(VALIDATOR.iter_errors(candidate))
        self.assertTrue(errors)
        self.assertTrue(
            any("pointers" in error.message and "required" in error.message for error in errors),
            [error.message for error in errors],
        )

    def test_nested_unknown_attribute_is_rejected(self) -> None:
        candidate = copy.deepcopy(SEED)
        parameter = next(
            node for node in candidate["nodes"] if node["kind"] == "Parameter"
        )
        parameter["attributes"]["surprise"] = True
        self.assertTrue(list(VALIDATOR.iter_errors(candidate)))

    def test_parameter_value_type_must_match_domain_shape(self) -> None:
        candidate = copy.deepcopy(SEED)
        parameter = next(
            node
            for node in candidate["nodes"]
            if node["id"] == "parameter.pll3.w_tail_um"
        )
        parameter["attributes"]["value_type"] = "integer"
        self.assertTrue(list(VALIDATOR.iter_errors(candidate)))

    def test_spec_operator_must_match_limit_shape(self) -> None:
        candidate = copy.deepcopy(SEED)
        spec = next(
            node
            for node in candidate["nodes"]
            if node["id"] == "spec.pll3.osc_freq_lock"
        )
        spec["attributes"]["operator"] = "<="
        self.assertTrue(list(VALIDATOR.iter_errors(candidate)))

    def test_influence_requires_explicit_extrapolation_policy(self) -> None:
        candidate = copy.deepcopy(SEED)
        influence = next(
            edge for edge in candidate["edges"] if edge["kind"] == "influences"
        )
        del influence["scope"]["extrapolation"]
        self.assertTrue(list(VALIDATOR.iter_errors(candidate)))

    def test_measured_causal_edges_require_numeric_observations(self) -> None:
        candidate = copy.deepcopy(SEED)
        influence = next(
            edge
            for edge in candidate["edges"]
            if edge["kind"] == "influences"
            and edge["evidence"]["grade"] == "measured"
        )
        influence["attributes"]["quantification"] = []
        self.assertTrue(list(VALIDATOR.iter_errors(candidate)))
        with self.assertRaisesRegex(
            kg_query.GraphError, "requires numeric observations"
        ):
            kg_query.KnowledgeGraph(candidate)

    def test_derived_causal_prior_may_remain_unquantified(self) -> None:
        topology_prior = next(
            edge
            for edge in SEED["edges"]
            if edge["id"] == "edge.influence.pll1.pfd_drive1.switching"
        )
        self.assertEqual("derived", topology_prior["evidence"]["grade"])
        self.assertEqual([], topology_prior["attributes"]["quantification"])

    def test_report_only_row_may_omit_unstated_condition(self) -> None:
        phase_noise = next(
            node
            for node in SEED["nodes"]
            if node["id"] == "spec.pll3.phase_noise_1m"
        )
        self.assertNotIn("condition", phase_noise["attributes"])
        condition_edges = [
            edge
            for edge in SEED["edges"]
            if edge["kind"] == "evaluated_under"
            and edge["source"] == phase_noise["id"]
        ]
        self.assertEqual([], condition_edges)

    def test_every_pointer_binds_the_public_snapshot(self) -> None:
        revision = SEED["source_snapshot"]["revision"]
        allowed_paths = {
            "docs/task1-b-decision-options.md",
            "docs/task1-b-sweeps.md",
            "docs/task3-a-sweeps.md",
            "tasks/pll1-cplf-sg13g2/README.md",
            "tasks/pll1-cplf-sg13g2/task.yaml",
            "tasks/pll3-vco-sg13g2/task.yaml",
        }
        for edge in SEED["edges"]:
            pointers = edge["evidence"]["pointers"]
            self.assertTrue(pointers, edge["id"])
            for pointer in pointers:
                self.assertEqual(revision, pointer["revision"], edge["id"])
                self.assertIn(pointer["path"], allowed_paths, edge["id"])
                self.assertTrue(pointer["section"], edge["id"])


class SemanticValidationTests(unittest.TestCase):
    def test_seed_passes_graph_wide_semantic_validation(self) -> None:
        graph = kg_query.KnowledgeGraph(copy.deepcopy(SEED))
        self.assertEqual(134, len(graph.nodes_by_id))
        self.assertEqual(170, len(graph.edges_by_id))

    def test_runtime_rejects_nested_unknown_attribute(self) -> None:
        candidate = copy.deepcopy(SEED)
        parameter = next(
            node for node in candidate["nodes"] if node["kind"] == "Parameter"
        )
        parameter["attributes"]["surprise"] = True
        with self.assertRaisesRegex(kg_query.GraphError, "unknown fields: surprise"):
            kg_query.KnowledgeGraph(candidate)

    def test_runtime_rejects_invalid_cross_field_parameter_domain(self) -> None:
        candidate = copy.deepcopy(SEED)
        parameter = next(
            node
            for node in candidate["nodes"]
            if node["id"] == "parameter.pll3.w_tail_um"
        )
        parameter["attributes"]["domain"]["minimum"] = 900
        with self.assertRaisesRegex(kg_query.GraphError, "minimum exceeds maximum"):
            kg_query.KnowledgeGraph(candidate)

    def test_runtime_rejects_null_influence_extrapolation_policy(self) -> None:
        candidate = copy.deepcopy(SEED)
        influence = next(
            edge for edge in candidate["edges"] if edge["kind"] == "influences"
        )
        influence["scope"]["extrapolation"] = None
        with self.assertRaisesRegex(kg_query.GraphError, "unknown policy"):
            kg_query.KnowledgeGraph(candidate)

    def test_evidence_repository_and_revision_must_match_snapshot(self) -> None:
        for field, value in (
            ("repository", "private/repository"),
            ("revision", "0" * 40),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(SEED)
                candidate["edges"][0]["evidence"]["pointers"][0][field] = value
                with self.assertRaisesRegex(
                    kg_query.GraphError, "does not match source_snapshot"
                ):
                    kg_query.KnowledgeGraph(candidate)

    def test_evidence_path_must_be_normalized_repository_relative(self) -> None:
        candidate = copy.deepcopy(SEED)
        candidate["edges"][0]["evidence"]["pointers"][0]["path"] = "../private.json"
        with self.assertRaisesRegex(kg_query.GraphError, "repository-relative path"):
            kg_query.KnowledgeGraph(candidate)

    def test_runtime_enforces_evidence_pointer_bound(self) -> None:
        candidate = copy.deepcopy(SEED)
        pointer = candidate["edges"][0]["evidence"]["pointers"][0]
        candidate["edges"][0]["evidence"]["pointers"] = [
            {**pointer, "locator": f"pointer-{index}"} for index in range(17)
        ]
        with self.assertRaisesRegex(kg_query.GraphError, "exceeds 16 items"):
            kg_query.KnowledgeGraph(candidate)

    def test_runtime_rejects_duplicate_evidence_pointers(self) -> None:
        candidate = copy.deepcopy(SEED)
        pointer = candidate["edges"][0]["evidence"]["pointers"][0]
        candidate["edges"][0]["evidence"]["pointers"] = [pointer, copy.deepcopy(pointer)]
        with self.assertRaisesRegex(kg_query.GraphError, "duplicate pointers"):
            kg_query.KnowledgeGraph(candidate)

    def test_runtime_accepts_schema_bounded_long_evidence_path(self) -> None:
        candidate = copy.deepcopy(SEED)
        candidate["edges"][0]["evidence"]["pointers"][0]["path"] = (
            "docs/" + "a" * 595
        )
        kg_query.KnowledgeGraph(candidate)

    def test_relation_evidence_policy_fails_closed(self) -> None:
        candidate = copy.deepcopy(SEED)
        contains = next(edge for edge in candidate["edges"] if edge["kind"] == "contains")
        contains["evidence"]["grade"] = "textbook"
        contains["evidence"]["basis"] = "textbook_prior"
        with self.assertRaisesRegex(kg_query.GraphError, "forbidden for contains"):
            kg_query.KnowledgeGraph(candidate)

    def test_node_and_edge_ids_share_one_namespace(self) -> None:
        candidate = copy.deepcopy(SEED)
        candidate["edges"][0]["id"] = candidate["nodes"][0]["id"]
        with self.assertRaisesRegex(kg_query.GraphError, "collides across node and edge"):
            kg_query.KnowledgeGraph(candidate)

    def test_spec_measurement_edge_must_match_attribute(self) -> None:
        candidate = copy.deepcopy(SEED)
        spec = next(
            node
            for node in candidate["nodes"]
            if node["id"] == "spec.pll3.tank_swing"
        )
        spec["attributes"]["measurement"] = "measurement.pll3.osc.supply_power"
        with self.assertRaisesRegex(kg_query.GraphError, "must match exactly one specifies"):
            kg_query.KnowledgeGraph(candidate)

    def test_parameter_target_edges_must_match_attribute(self) -> None:
        candidate = copy.deepcopy(SEED)
        targets = next(
            edge
            for edge in candidate["edges"]
            if edge["id"] == "edge.targets.pll3.w_tail"
        )
        targets["attributes"]["target_paths"] = ["XM7.w"]
        with self.assertRaisesRegex(kg_query.GraphError, "disagrees with targets edges"):
            kg_query.KnowledgeGraph(candidate)

    def test_duplicate_node_id_fails_closed(self) -> None:
        candidate = copy.deepcopy(SEED)
        duplicate = copy.deepcopy(candidate["nodes"][0])
        candidate["nodes"].append(duplicate)
        with self.assertRaisesRegex(kg_query.GraphError, "duplicate node id"):
            kg_query.KnowledgeGraph(candidate)

    def test_dangling_endpoint_fails_closed(self) -> None:
        candidate = copy.deepcopy(SEED)
        candidate["edges"][0]["target"] = "measurement.does_not_exist"
        with self.assertRaisesRegex(kg_query.GraphError, "missing node"):
            kg_query.KnowledgeGraph(candidate)

    def test_ill_typed_relation_endpoint_fails_closed(self) -> None:
        candidate = copy.deepcopy(SEED)
        specifies = next(edge for edge in candidate["edges"] if edge["kind"] == "specifies")
        specifies["source"] = "parameter.pll3.w_cc_p_um"
        with self.assertRaisesRegex(kg_query.GraphError, "forbids endpoint kinds"):
            kg_query.KnowledgeGraph(candidate)

    def test_recipe_dependency_cycle_fails_closed(self) -> None:
        candidate = copy.deepcopy(SEED)
        evidence = copy.deepcopy(
            next(edge for edge in candidate["edges"] if edge["kind"] == "depends_on")[
                "evidence"
            ]
        )
        candidate["edges"].append(
            {
                "id": "edge.depends.test.cycle",
                "kind": "depends_on",
                "source": "stage.pll1.cp_pulse",
                "target": "stage.pll1.loop_linear_ac",
                "evidence": evidence,
                "attributes": {"bindings": ["cycle_for_negative_test"]},
            }
        )
        with self.assertRaisesRegex(kg_query.GraphError, "recipe dependency cycle"):
            kg_query.KnowledgeGraph(candidate)


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = kg_query.KnowledgeGraph(copy.deepcopy(SEED))

    def test_influences_reports_frequency_via_tank_loading(self) -> None:
        result = self.graph.query("influences", "w_cc_p_um")
        self.assertEqual("parameter.pll3.w_cc_p_um", result["query"]["resolved_id"])
        frequency = next(
            item
            for item in result["results"]
            if item["measurement"]["id"] == "measurement.pll3.osc.freq"
        )
        self.assertEqual("negative", frequency["sign"])
        self.assertEqual(
            ["mechanism.pll3.tank_loading"],
            [node["id"] for node in frequency["mechanism_path"]],
        )
        self.assertEqual(
            ["spec.pll3.osc_freq_lock"],
            [node["id"] for node in frequency["spec_rows"]],
        )
        observations = [
            observation
            for edge in frequency["path"]
            for observation in edge["attributes"]["quantification"]
        ]
        self.assertTrue(any(item.get("delta") == -89_000_000 for item in observations))

    def test_influences_preserves_joint_intervention_scope(self) -> None:
        result = self.graph.query("influences", "w_cc_p_um")
        frequency = next(
            item
            for item in result["results"]
            if item["measurement"]["id"] == "measurement.pll3.osc.freq"
        )
        first_scope = frequency["path"][0]["scope"]
        self.assertEqual(
            ["parameter.pll3.w_cc_n_um"],
            first_scope["co_varied_parameters"],
        )
        self.assertEqual("forbidden", first_scope["extrapolation"])
        self.assertIn(frequency["scope_compatibility"], {"compatible", "requires_review"})

    def test_influences_accepts_condition_and_corner_sources(self) -> None:
        supply = self.graph.query("influences", "supply_step")
        raw_frequency = next(
            item
            for item in supply["results"]
            if item["measurement"]["id"] == "measurement.pll3.osc.freq"
        )
        shift_magnitude = next(
            item
            for item in supply["results"]
            if item["measurement"]["id"] == "measurement.pll3.osc.freq_shift_vdd"
        )
        self.assertEqual("negative", raw_frequency["sign"])
        self.assertEqual("unknown", shift_magnitude["sign"])
        self.assertEqual(
            ["mechanism.pll3.supply_pushing"],
            [node["id"] for node in raw_frequency["mechanism_path"]],
        )
        ff = self.graph.query("influences", "ff_m20c_1v32")
        self.assertEqual(
            ["mechanism.pll3.corner_frequency_shift"],
            [node["id"] for node in ff["results"][0]["mechanism_path"]],
        )

    def test_influences_exposes_unquantified_topology_prior(self) -> None:
        result = self.graph.query("influences", "pfd_drive1")
        offset = next(
            item
            for item in result["results"]
            if item["measurement"]["id"]
            == "measurement.pll1.charge.zero_cross_offset"
        )
        self.assertEqual("unknown", offset["sign"])
        self.assertEqual("derived", offset["evidence_grade"])
        self.assertEqual(
            ["mechanism.pll1.switching_asymmetry"],
            [node["id"] for node in offset["mechanism_path"]],
        )

    def test_levers_ranks_measured_tail_width_for_startup_first(self) -> None:
        result = self.graph.query("levers", "osc.startup_time")
        self.assertEqual(
            [
                "parameter.pll3.w_tail_um",
                "parameter.pll3.w_cc_n_um",
                "parameter.pll3.w_cc_p_um",
            ],
            [item["parameter"]["id"] for item in result["results"]],
        )
        self.assertEqual("measured", result["results"][0]["evidence_grade"])
        self.assertEqual("negative", result["results"][0]["sign"])
        self.assertFalse(result["results"][0]["requires_co_variation"])
        joint = result["results"][1]
        self.assertTrue(joint["requires_co_variation"])
        self.assertEqual(
            ["parameter.pll3.w_cc_p_um"],
            [node["id"] for node in joint["co_varied_parameters"]],
        )
        self.assertIn("not an isolated", joint["joint_intervention_warning"])

    def test_unchanged_capacitor_edges_are_not_ranked_as_filter_levers(self) -> None:
        result = self.graph.query("levers", "linear.phase_margin")
        parameter_ids = {item["parameter"]["id"] for item in result["results"]}
        self.assertNotIn("parameter.pll1.c1_edge_um", parameter_ids)
        self.assertNotIn("parameter.pll1.c2_edge_um", parameter_ids)

    def test_offset_levers_route_through_switching_asymmetry(self) -> None:
        result = self.graph.query("levers", "charge.zero_cross_offset")
        w_ps = next(
            item
            for item in result["results"]
            if item["parameter"]["id"] == "parameter.pll1.w_ps_um"
        )
        self.assertEqual("positive", w_ps["sign"])
        self.assertEqual(
            [
                "mechanism.pll1.source_edge_charge",
                "mechanism.pll1.switching_asymmetry",
            ],
            [node["id"] for node in w_ps["best_path"]["mechanism_path"]],
        )
        self.assertTrue(w_ps["requires_co_variation"])

    def test_deadzone_parameter_path_does_not_hold_qnom_silently_fixed(self) -> None:
        result = self.graph.query("influences", "w_ps_um")
        deadzone = next(
            item
            for item in result["results"]
            if item["measurement"]["id"]
            == "measurement.pll1.charge.deadzone_width"
        )
        self.assertEqual("unknown", deadzone["sign"])
        self.assertEqual("requires_review", deadzone["scope_compatibility"])
        self.assertIsNotNone(deadzone["scope_warning"])

    def test_tradeoffs_find_mismatch_compliance_tension_symmetrically(self) -> None:
        forward = self.graph.query("tradeoffs", "cp_mismatch_pulse")
        reverse = self.graph.query("tradeoffs", "cp_compliance_span")
        self.assertEqual(
            "spec.pll1.cp_compliance_span",
            forward["results"][0]["spec_row"]["id"],
        )
        self.assertEqual(
            "spec.pll1.cp_mismatch_pulse",
            reverse["results"][0]["spec_row"]["id"],
        )
        self.assertEqual(
            "mechanism.pll1.switch_feed_tradeoff",
            forward["results"][0]["mechanism"]["id"],
        )
        self.assertEqual("antagonistic", forward["results"][0]["coupling"])
        observations = forward["results"][0]["quantification"]
        self.assertTrue(
            any(
                item["quantity"] == "charge.mismatch_worst"
                and item["baseline"] == 0.27
                and item["observed"] == 0.101
                for item in observations
            )
        )

    def test_tradeoffs_expose_startup_power_tension(self) -> None:
        result = self.graph.query("tradeoffs", "startup_time")
        self.assertEqual(
            ["spec.pll3.supply_power"],
            [item["spec_row"]["id"] for item in result["results"]],
        )
        self.assertEqual(
            "mechanism.pll3.tail_bias_amplitude_power",
            result["results"][0]["mechanism"]["id"],
        )

    def test_recipe_returns_phase_margin_binding_chain(self) -> None:
        result = self.graph.query("recipe", "linear.phase_margin")
        chain = result["results"][0]
        self.assertEqual(
            [
                "stage.pll1.cp_pulse",
                "stage.pll1.dz_coarse",
                "stage.pll1.dz_fine",
                "stage.pll1.loop_linear_ac",
            ],
            [item["stage"]["id"] for item in chain["stages"]],
        )
        bindings = {
            binding
            for stage in chain["stages"]
            for dependency in stage["depends_on"]
            for binding in dependency["bindings"]
        }
        self.assertEqual({"qnom_pulse", "dz_center_s", "icp_meas"}, bindings)
        self.assertIn("phase", chain["measurement_binding"])

    def test_validity_reports_unpatched_hot_failure_and_repair(self) -> None:
        result = self.graph.query("validity", "unpatched_hot_biased_svaricap")
        self.assertEqual(1, len(result["results"]))
        caveat = result["results"][0]
        self.assertEqual("invalid", caveat["assessment"])
        self.assertEqual(
            "model.pll3.svaricap_dsubw_unpatched", caveat["artifact"]["id"]
        )
        self.assertIn("52.46980", caveat["predicate"])
        self.assertIn("Temperature alone", caveat["conclusion"])
        self.assertEqual(
            "model.pll3.svaricap_dsubw_pdkfix1099",
            caveat["repairs"][0]["artifact"]["id"],
        )
        self.assertEqual("repairs_this", caveat["repairs"][0]["direction"])
        self.assertEqual(
            "mechanism.pll3.svaricap_temperature_law",
            caveat["mechanism"]["id"],
        )

    def test_no_effect_sign_absorbs_uncertain_path_segments(self) -> None:
        self.assertEqual("none", kg_query._compose_signs(["none", "unknown"]))
        self.assertEqual("none", kg_query._compose_signs(["mixed", "none"]))

    def test_lever_path_priority_prefers_scope_compatibility(self) -> None:
        compatible = {
            "scope_compatibility": "compatible",
            "evidence_grade": "derived",
            "strength": "weak",
            "path": [{"id": "edge.compatible"}, {"id": "edge.compatible.2"}],
        }
        incompatible = {
            "scope_compatibility": "incompatible",
            "evidence_grade": "measured",
            "strength": "strong",
            "path": [{"id": "edge.incompatible"}],
        }
        self.assertLess(
            kg_query._lever_path_score(compatible),
            kg_query._lever_path_score(incompatible),
        )

    def test_validity_reports_patched_ss85_bounded_not_global(self) -> None:
        result = self.graph.query("validity", "ss_85c_1v08")
        patched = next(
            item
            for item in result["results"]
            if item["artifact"]["id"] == "model.pll3.svaricap_dsubw_pdkfix1099"
        )
        self.assertEqual("valid", patched["assessment"])
        self.assertIn("not an unrestricted", patched["validation"])
        self.assertEqual("forbidden", patched["scope"]["extrapolation"])
        self.assertEqual("gate.pll3.svaricap_model", patched["guards"][0]["gate"]["id"])

    def test_query_output_is_byte_deterministic(self) -> None:
        first = kg_query.canonical_json(self.graph.query("influences", "w_ps_um"))
        second = kg_query.canonical_json(self.graph.query("influences", "w_ps_um"))
        self.assertEqual(first, second)
        json.loads(first)

    def test_cli_unknown_entity_fails_with_typed_diagnostic(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(QUERY_PATH),
                "influences",
                "not_a_public_parameter",
                "--compact",
            ],
            cwd=HERE,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertEqual("unknown", payload["status"])
        self.assertEqual(
            "kg.query.invalid_request", payload["diagnostics"][0]["code"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
