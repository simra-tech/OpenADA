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
        del candidate["edges"][0]["evidence"]
        errors = list(VALIDATOR.iter_errors(candidate))
        self.assertTrue(errors)
        self.assertTrue(
            any("evidence" in error.message and "required" in error.message for error in errors),
            [error.message for error in errors],
        )

    def test_every_pointer_binds_the_public_snapshot(self) -> None:
        revision = SEED["source_snapshot"]["revision"]
        allowed_paths = {
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
        self.assertEqual(167, len(graph.edges_by_id))

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
