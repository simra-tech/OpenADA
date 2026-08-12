#!/usr/bin/env python3
"""Deterministic stdlib-only queries over the KG research-spike graph.

This module is intentionally evaluation-local.  It is not an OpenADA operation,
does not import ``src/openada``, and does not replace formal JSON Schema
validation.  It performs the graph-wide semantic checks that JSON Schema cannot
express and supplies the five v0 query kinds described in the design memo.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


GRAPH_SCHEMA = "openada.eval/analog-knowledge-graph/v0"
RESULT_SCHEMA = "openada.eval/kg-query-result/v0"
IMPLEMENTATION_ID = "openada.eval/kg-query-python"
IMPLEMENTATION_VERSION = "0.1.0"

NODE_KINDS = frozenset(
    {
        "Task",
        "Device",
        "Block",
        "Topology",
        "Parameter",
        "Measurement",
        "SpecRow",
        "Corner",
        "Condition",
        "Mechanism",
        "Tradeoff",
        "RecipeStage",
        "ValidityGate",
        "Trap",
        "ModelArtifact",
    }
)

EDGE_ENDPOINT_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "contains": (
        frozenset({"Task", "Block", "Topology"}),
        NODE_KINDS - frozenset({"Task"}),
    ),
    "implements": (frozenset({"Topology"}), frozenset({"Block"})),
    "targets": (frozenset({"Parameter"}), frozenset({"Device"})),
    "specifies": (frozenset({"SpecRow"}), frozenset({"Measurement"})),
    "evaluated_under": (
        frozenset({"SpecRow", "Measurement", "RecipeStage"}),
        frozenset({"Condition", "Corner"}),
    ),
    "influences": (
        frozenset({"Parameter", "Condition", "Corner", "Mechanism"}),
        frozenset({"Mechanism", "Measurement"}),
    ),
    "trades_off": (frozenset({"SpecRow"}), frozenset({"SpecRow"})),
    "measured_by": (frozenset({"Measurement"}), frozenset({"RecipeStage"})),
    "depends_on": (frozenset({"RecipeStage"}), frozenset({"RecipeStage"})),
    "models": (frozenset({"ModelArtifact"}), frozenset({"Device"})),
    "valid_when": (
        frozenset({"ModelArtifact"}),
        frozenset({"Condition", "Corner"}),
    ),
    "invalid_when": (
        frozenset({"ModelArtifact"}),
        frozenset({"Condition", "Corner"}),
    ),
    "repairs": (frozenset({"ModelArtifact"}), frozenset({"ModelArtifact"})),
    "guards": (
        frozenset({"ValidityGate"}),
        frozenset({"RecipeStage", "Measurement", "SpecRow", "Corner"}),
    ),
    "catches": (
        frozenset({"SpecRow", "ValidityGate"}),
        frozenset({"Trap"}),
    ),
}

NODE_FIELDS = frozenset({"id", "kind", "name", "description", "attributes"})
EDGE_FIELDS = frozenset(
    {"id", "kind", "source", "target", "evidence", "scope", "attributes"}
)
EDGE_REQUIRED_FIELDS = frozenset(
    {"id", "kind", "source", "target", "evidence", "attributes"}
)
EVIDENCE_FIELDS = frozenset({"grade", "basis", "pointers", "summary"})
EVIDENCE_REQUIRED_FIELDS = frozenset({"grade", "basis", "pointers"})
POINTER_FIELDS = frozenset(
    {"repository", "revision", "path", "section", "locator"}
)
POINTER_REQUIRED_FIELDS = frozenset(
    {"repository", "revision", "path", "section"}
)
SCOPE_FIELDS = frozenset(
    {
        "condition_ids",
        "corner_ids",
        "design_point",
        "intervention",
        "co_varied_parameters",
        "extrapolation",
    }
)

SIGN_VALUES = frozenset({"positive", "negative", "mixed", "none", "unknown"})
STRENGTH_VALUES = frozenset(
    {"structural", "strong", "moderate", "weak", "unknown"}
)
GRADE_VALUES = frozenset({"measured", "derived", "textbook"})
BASIS_VALUES = frozenset(
    {
        "simulation_sweep",
        "task_contract",
        "recipe_contract",
        "calculation",
        "causal_interpretation",
        "textbook_prior",
        "model_debug",
    }
)
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# Lower is a better search priority.  The final ID tie-break makes every order
# total and reproducible.  ``structural`` means an exact dependency/identity,
# not a larger normalized physical sensitivity.
GRADE_RANK = {"measured": 0, "derived": 1, "textbook": 2}
STRENGTH_RANK = {
    "structural": 0,
    "strong": 1,
    "moderate": 2,
    "weak": 3,
    "unknown": 4,
}


class GraphError(ValueError):
    """The graph cannot be trusted or queried."""


class QueryError(ValueError):
    """The typed query cannot be resolved unambiguously."""


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GraphError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise GraphError(f"non-finite JSON number is forbidden: {value}")


def load_json(path: Path | str) -> dict[str, Any]:
    """Load strict JSON, rejecting duplicate keys and NaN/infinity."""

    graph_path = Path(path)
    try:
        with graph_path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
    except OSError as exc:
        raise GraphError(f"cannot read graph {graph_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GraphError(
            f"invalid JSON in {graph_path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise GraphError("graph root must be an object")
    _check_finite(value, path="$")
    return value


def _check_finite(value: Any, *, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise GraphError(f"{path}: non-finite number is forbidden")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_finite(item, path=f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, path=f"{path}.{key}")


def _require_exact_fields(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    path: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise GraphError(f"{path}: unknown fields: {', '.join(unknown)}")
    if missing:
        raise GraphError(f"{path}: missing fields: {', '.join(missing)}")


def _require_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise GraphError(f"{path}: expected a nonempty string")
    return value


def _require_list(value: Any, *, path: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise GraphError(f"{path}: expected an array")
    if nonempty and not value:
        raise GraphError(f"{path}: array must not be empty")
    return value


def _node_view(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "kind": node["kind"],
        "name": node["name"],
        "description": node["description"],
        "attributes": node["attributes"],
    }


def _edge_view(edge: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        "id": edge["id"],
        "kind": edge["kind"],
        "source": edge["source"],
        "target": edge["target"],
        "attributes": edge["attributes"],
        "evidence": edge["evidence"],
    }
    if "scope" in edge:
        view["scope"] = edge["scope"]
    return view


class KnowledgeGraph:
    """Validated in-memory indexes and deterministic typed queries."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self._validate_root()
        self.nodes_by_id: dict[str, dict[str, Any]] = {}
        self.edges_by_id: dict[str, dict[str, Any]] = {}
        self.out_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.in_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.aliases: dict[str, set[str]] = defaultdict(set)
        self._index_nodes()
        self._index_edges()
        self._validate_node_references()
        self._validate_edge_references()
        self._validate_recipe_acyclic()

    @classmethod
    def from_path(cls, path: Path | str) -> "KnowledgeGraph":
        return cls(load_json(path))

    def _validate_root(self) -> None:
        root_fields = frozenset(
            {
                "schema",
                "graph_id",
                "graph_version",
                "title",
                "description",
                "source_snapshot",
                "nodes",
                "edges",
            }
        )
        _require_exact_fields(
            self.payload,
            allowed=root_fields,
            required=root_fields,
            path="$",
        )
        if self.payload.get("schema") != GRAPH_SCHEMA:
            raise GraphError(
                f"unsupported graph schema: {self.payload.get('schema')!r}"
            )
        for field in ("graph_id", "graph_version", "title"):
            _require_string(self.payload.get(field), path=f"$.{field}")
        if not isinstance(self.payload.get("description"), str):
            raise GraphError("$.description: expected a string")
        source_snapshot = self.payload.get("source_snapshot")
        if not isinstance(source_snapshot, dict):
            raise GraphError("$.source_snapshot: expected an object")
        _require_exact_fields(
            source_snapshot,
            allowed=frozenset({"repository", "revision", "policy"}),
            required=frozenset({"repository", "revision", "policy"}),
            path="$.source_snapshot",
        )
        revision = _require_string(
            source_snapshot.get("revision"), path="$.source_snapshot.revision"
        )
        if not GIT_REVISION_RE.fullmatch(revision):
            raise GraphError("$.source_snapshot.revision: expected a full Git SHA")
        if source_snapshot.get("policy") != "tracked-public-files-only":
            raise GraphError("$.source_snapshot.policy: unsupported source policy")
        _require_list(self.payload.get("nodes"), path="$.nodes", nonempty=True)
        _require_list(self.payload.get("edges"), path="$.edges", nonempty=True)

    def _index_nodes(self) -> None:
        for index, raw_node in enumerate(self.payload["nodes"]):
            path = f"$.nodes[{index}]"
            if not isinstance(raw_node, dict):
                raise GraphError(f"{path}: expected an object")
            _require_exact_fields(
                raw_node,
                allowed=NODE_FIELDS,
                required=NODE_FIELDS,
                path=path,
            )
            node_id = _require_string(raw_node.get("id"), path=f"{path}.id")
            kind = _require_string(raw_node.get("kind"), path=f"{path}.kind")
            if kind not in NODE_KINDS:
                raise GraphError(f"{path}.kind: unknown node kind {kind!r}")
            _require_string(raw_node.get("name"), path=f"{path}.name")
            if not isinstance(raw_node.get("description"), str):
                raise GraphError(f"{path}.description: expected a string")
            if not isinstance(raw_node.get("attributes"), dict):
                raise GraphError(f"{path}.attributes: expected an object")
            if node_id in self.nodes_by_id:
                raise GraphError(f"duplicate node id: {node_id}")
            node = dict(raw_node)
            self.nodes_by_id[node_id] = node
            self._add_aliases(node)

    def _add_aliases(self, node: Mapping[str, Any]) -> None:
        candidates: set[str] = {node["id"], node["name"]}
        attributes = node["attributes"]
        for key in (
            "semantic_name",
            "task_id",
            "condition_id",
            "corner_id",
            "topology_id",
            "stage_id",
            "gate_id",
        ):
            value = attributes.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
        for alias in candidates:
            self.aliases[alias].add(node["id"])

    def _index_edges(self) -> None:
        for index, raw_edge in enumerate(self.payload["edges"]):
            path = f"$.edges[{index}]"
            if not isinstance(raw_edge, dict):
                raise GraphError(f"{path}: expected an object")
            _require_exact_fields(
                raw_edge,
                allowed=EDGE_FIELDS,
                required=EDGE_REQUIRED_FIELDS,
                path=path,
            )
            edge_id = _require_string(raw_edge.get("id"), path=f"{path}.id")
            kind = _require_string(raw_edge.get("kind"), path=f"{path}.kind")
            if kind not in EDGE_ENDPOINT_KINDS:
                raise GraphError(f"{path}.kind: unknown edge kind {kind!r}")
            source = _require_string(raw_edge.get("source"), path=f"{path}.source")
            target = _require_string(raw_edge.get("target"), path=f"{path}.target")
            if edge_id in self.edges_by_id:
                raise GraphError(f"duplicate edge id: {edge_id}")
            if source not in self.nodes_by_id:
                raise GraphError(f"{path}.source: missing node {source!r}")
            if target not in self.nodes_by_id:
                raise GraphError(f"{path}.target: missing node {target!r}")
            if not isinstance(raw_edge.get("attributes"), dict):
                raise GraphError(f"{path}.attributes: expected an object")
            self._validate_evidence(raw_edge.get("evidence"), path=f"{path}.evidence")
            if "scope" in raw_edge:
                self._validate_scope(raw_edge["scope"], path=f"{path}.scope")
            source_kind = self.nodes_by_id[source]["kind"]
            target_kind = self.nodes_by_id[target]["kind"]
            allowed_sources, allowed_targets = EDGE_ENDPOINT_KINDS[kind]
            if source_kind not in allowed_sources or target_kind not in allowed_targets:
                raise GraphError(
                    f"{path}: {kind} forbids endpoint kinds "
                    f"{source_kind}->{target_kind}"
                )
            if kind == "influences":
                self._validate_influence(raw_edge, path=path)
            edge = dict(raw_edge)
            self.edges_by_id[edge_id] = edge
            self.out_edges[source].append(edge)
            self.in_edges[target].append(edge)
        for edges in self.out_edges.values():
            edges.sort(key=lambda edge: edge["id"])
        for edges in self.in_edges.values():
            edges.sort(key=lambda edge: edge["id"])

    def _validate_evidence(self, value: Any, *, path: str) -> None:
        if not isinstance(value, dict):
            raise GraphError(f"{path}: expected an object")
        _require_exact_fields(
            value,
            allowed=EVIDENCE_FIELDS,
            required=EVIDENCE_REQUIRED_FIELDS,
            path=path,
        )
        grade = value.get("grade")
        basis = value.get("basis")
        if grade not in GRADE_VALUES:
            raise GraphError(f"{path}.grade: unknown evidence grade {grade!r}")
        if basis not in BASIS_VALUES:
            raise GraphError(f"{path}.basis: unknown evidence basis {basis!r}")
        if grade == "textbook" and basis != "textbook_prior":
            raise GraphError(f"{path}: textbook evidence requires textbook_prior basis")
        if basis == "textbook_prior" and grade != "textbook":
            raise GraphError(f"{path}: textbook_prior basis requires textbook grade")
        pointers = _require_list(
            value.get("pointers"), path=f"{path}.pointers", nonempty=True
        )
        for index, pointer in enumerate(pointers):
            pointer_path = f"{path}.pointers[{index}]"
            if not isinstance(pointer, dict):
                raise GraphError(f"{pointer_path}: expected an object")
            _require_exact_fields(
                pointer,
                allowed=POINTER_FIELDS,
                required=POINTER_REQUIRED_FIELDS,
                path=pointer_path,
            )
            for field in POINTER_REQUIRED_FIELDS:
                _require_string(pointer.get(field), path=f"{pointer_path}.{field}")
            if not GIT_REVISION_RE.fullmatch(pointer["revision"]):
                raise GraphError(f"{pointer_path}.revision: expected a full Git SHA")

    def _validate_scope(self, value: Any, *, path: str) -> None:
        if not isinstance(value, dict) or not value:
            raise GraphError(f"{path}: expected a nonempty object")
        unknown = sorted(set(value) - SCOPE_FIELDS)
        if unknown:
            raise GraphError(f"{path}: unknown fields: {', '.join(unknown)}")
        if value.get("extrapolation") not in {
            None,
            "forbidden",
            "bounded",
            "not_applicable",
        }:
            raise GraphError(f"{path}.extrapolation: unknown policy")

    def _validate_influence(self, edge: Mapping[str, Any], *, path: str) -> None:
        attributes = edge["attributes"]
        sign = attributes.get("sign")
        strength = attributes.get("strength")
        quantification = attributes.get("quantification")
        if sign not in SIGN_VALUES:
            raise GraphError(f"{path}.attributes.sign: unknown sign {sign!r}")
        if strength not in STRENGTH_VALUES:
            raise GraphError(
                f"{path}.attributes.strength: unknown strength {strength!r}"
            )
        _require_list(
            quantification,
            path=f"{path}.attributes.quantification",
            nonempty=False,
        )

    def _validate_node_references(self) -> None:
        for node in self.nodes_by_id.values():
            if node["kind"] != "SpecRow":
                continue
            attributes = node["attributes"]
            self._require_node_kind(
                attributes.get("measurement"),
                {"Measurement"},
                context=f"{node['id']}.attributes.measurement",
            )
            self._require_node_kind(
                attributes.get("condition"),
                {"Condition"},
                context=f"{node['id']}.attributes.condition",
            )
            for index, limit in enumerate(attributes.get("limits", [])):
                if not isinstance(limit, dict):
                    raise GraphError(
                        f"{node['id']}.attributes.limits[{index}]: expected object"
                    )
                if "corner" in limit:
                    self._require_node_kind(
                        limit["corner"],
                        {"Corner"},
                        context=f"{node['id']}.attributes.limits[{index}].corner",
                    )

    def _validate_edge_references(self) -> None:
        for edge in self.edges_by_id.values():
            scope = edge.get("scope", {})
            for field, kind in (
                ("condition_ids", "Condition"),
                ("corner_ids", "Corner"),
                ("co_varied_parameters", "Parameter"),
            ):
                for index, node_id in enumerate(scope.get(field, [])):
                    self._require_node_kind(
                        node_id,
                        {kind},
                        context=f"{edge['id']}.scope.{field}[{index}]",
                    )
            if edge["kind"] == "trades_off":
                self._require_node_kind(
                    edge["attributes"].get("tradeoff"),
                    {"Tradeoff"},
                    context=f"{edge['id']}.attributes.tradeoff",
                )
                self._require_node_kind(
                    edge["attributes"].get("mechanism"),
                    {"Mechanism"},
                    context=f"{edge['id']}.attributes.mechanism",
                )
            if edge["kind"] == "targets":
                target_paths = edge["attributes"].get("target_paths")
                if not isinstance(target_paths, list) or not target_paths:
                    raise GraphError(
                        f"{edge['id']}.attributes.target_paths: expected nonempty array"
                    )
            if edge["kind"] == "depends_on":
                bindings = edge["attributes"].get("bindings")
                if not isinstance(bindings, list) or not bindings:
                    raise GraphError(
                        f"{edge['id']}.attributes.bindings: expected nonempty array"
                    )

    def _require_node_kind(
        self, value: Any, allowed: set[str], *, context: str
    ) -> dict[str, Any]:
        node_id = _require_string(value, path=context)
        node = self.nodes_by_id.get(node_id)
        if node is None:
            raise GraphError(f"{context}: missing node {node_id!r}")
        if node["kind"] not in allowed:
            raise GraphError(
                f"{context}: expected {sorted(allowed)}, got {node['kind']}"
            )
        return node

    def _validate_recipe_acyclic(self) -> None:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(stage_id: str) -> None:
            marker = state.get(stage_id, 0)
            if marker == 2:
                return
            if marker == 1:
                start = stack.index(stage_id)
                cycle = stack[start:] + [stage_id]
                raise GraphError(f"recipe dependency cycle: {' -> '.join(cycle)}")
            state[stage_id] = 1
            stack.append(stage_id)
            dependencies = [
                edge
                for edge in self.out_edges.get(stage_id, [])
                if edge["kind"] == "depends_on"
            ]
            dependencies.sort(key=lambda edge: edge["target"])
            for edge in dependencies:
                visit(edge["target"])
            stack.pop()
            state[stage_id] = 2

        for stage_id in sorted(
            node_id
            for node_id, node in self.nodes_by_id.items()
            if node["kind"] == "RecipeStage"
        ):
            visit(stage_id)

    def resolve_node(
        self, reference: str, *, expected_kinds: Iterable[str]
    ) -> dict[str, Any]:
        allowed = frozenset(expected_kinds)
        direct = self.nodes_by_id.get(reference)
        if direct is not None and direct["kind"] in allowed:
            return direct
        matches = sorted(
            node_id
            for node_id in self.aliases.get(reference, set())
            if self.nodes_by_id[node_id]["kind"] in allowed
        )
        if not matches:
            raise QueryError(
                f"no {', '.join(sorted(allowed))} matches {reference!r}"
            )
        if len(matches) > 1:
            raise QueryError(
                f"ambiguous {reference!r}; use one of: {', '.join(matches)}"
            )
        return self.nodes_by_id[matches[0]]

    def query(self, kind: str, entity: str) -> dict[str, Any]:
        dispatch = {
            "influences": self.influences,
            "levers": self.levers,
            "tradeoffs": self.tradeoffs,
            "recipe": self.recipe,
            "validity": self.validity,
        }
        try:
            handler = dispatch[kind]
        except KeyError as exc:
            raise QueryError(f"unknown query kind: {kind!r}") from exc
        resolved, results = handler(entity)
        return {
            "schema": RESULT_SCHEMA,
            "implementation": {
                "id": IMPLEMENTATION_ID,
                "version": IMPLEMENTATION_VERSION,
            },
            "status": "pass",
            "graph": {
                "id": self.payload["graph_id"],
                "version": self.payload["graph_version"],
                "source_snapshot": self.payload["source_snapshot"],
            },
            "query": {
                "kind": kind,
                "entity": entity,
                "resolved_id": resolved["id"],
            },
            "results": results,
            "limitations": [
                "Ranking is a deterministic search priority, not normalized sensitivity.",
                "Measured magnitudes are valid only in each returned scope; extrapolation is never implicit.",
                "A query result is design knowledge, not a fresh specification or signoff result.",
            ],
        }

    def _bound_specs(self, measurement_id: str) -> list[dict[str, Any]]:
        specs = [
            self.nodes_by_id[edge["source"]]
            for edge in self.in_edges.get(measurement_id, [])
            if edge["kind"] == "specifies"
        ]
        return [_node_view(node) for node in sorted(specs, key=lambda item: item["id"])]

    def _influence_paths(self, parameter_id: str) -> list[dict[str, Any]]:
        found: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

        def walk(
            current_id: str,
            path: list[dict[str, Any]],
            seen: frozenset[str],
        ) -> None:
            outgoing = [
                edge
                for edge in self.out_edges.get(current_id, [])
                if edge["kind"] == "influences"
            ]
            for edge in outgoing:
                target_id = edge["target"]
                if target_id in seen:
                    continue
                target = self.nodes_by_id[target_id]
                next_path = path + [edge]
                if target["kind"] == "Measurement":
                    found.append((target, next_path))
                elif target["kind"] == "Mechanism":
                    walk(target_id, next_path, seen | {target_id})

        walk(parameter_id, [], frozenset({parameter_id}))
        results = [self._format_influence_path(measurement, path) for measurement, path in found]
        results.sort(
            key=lambda item: (
                item["measurement"]["id"],
                tuple(node["id"] for node in item["mechanism_path"]),
                tuple(edge["id"] for edge in item["path"]),
            )
        )
        return results

    def _format_influence_path(
        self, measurement: Mapping[str, Any], path: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        mechanism_nodes = [
            self.nodes_by_id[edge["target"]]
            for edge in path
            if self.nodes_by_id[edge["target"]]["kind"] == "Mechanism"
        ]
        grades = [edge["evidence"]["grade"] for edge in path]
        strengths = [edge["attributes"]["strength"] for edge in path]
        extrapolation = sorted(
            {
                edge.get("scope", {}).get("extrapolation")
                for edge in path
                if edge.get("scope", {}).get("extrapolation") is not None
            }
        )
        return {
            "measurement": _node_view(measurement),
            "spec_rows": self._bound_specs(measurement["id"]),
            "sign": _compose_signs(edge["attributes"]["sign"] for edge in path),
            "strength": _compose_strength(strengths),
            "evidence_grade": _compose_grade(grades),
            "mechanism_path": [_node_view(node) for node in mechanism_nodes],
            "path": [_edge_view(edge) for edge in path],
            "extrapolation_policies": extrapolation,
        }

    def influences(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        parameter = self.resolve_node(entity, expected_kinds={"Parameter"})
        return parameter, self._influence_paths(parameter["id"])

    def levers(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        measurement = self.resolve_node(entity, expected_kinds={"Measurement"})
        candidates: list[dict[str, Any]] = []
        parameters = sorted(
            (
                node
                for node in self.nodes_by_id.values()
                if node["kind"] == "Parameter"
            ),
            key=lambda node: node["id"],
        )
        for parameter in parameters:
            paths = [
                path
                for path in self._influence_paths(parameter["id"])
                if path["measurement"]["id"] == measurement["id"]
            ]
            if not paths:
                continue
            paths.sort(key=_lever_path_score)
            best = paths[0]
            candidates.append(
                {
                    "parameter": _node_view(parameter),
                    "sign": best["sign"],
                    "strength": best["strength"],
                    "evidence_grade": best["evidence_grade"],
                    "best_path": best,
                    "alternative_paths": paths[1:],
                    "rank_basis": {
                        "evidence_grade": best["evidence_grade"],
                        "strength": best["strength"],
                        "path_length": len(best["path"]),
                    },
                }
            )
        candidates.sort(
            key=lambda item: (
                GRADE_RANK[item["evidence_grade"]],
                STRENGTH_RANK[item["strength"]],
                len(item["best_path"]["path"]),
                item["parameter"]["id"],
            )
        )
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return measurement, candidates

    def tradeoffs(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        spec = self.resolve_node(entity, expected_kinds={"SpecRow"})
        matching = [
            edge
            for edge in self.edges_by_id.values()
            if edge["kind"] == "trades_off"
            and spec["id"] in {edge["source"], edge["target"]}
        ]
        results = []
        for edge in matching:
            other_id = edge["target"] if edge["source"] == spec["id"] else edge["source"]
            results.append(
                {
                    "spec_row": _node_view(self.nodes_by_id[other_id]),
                    "tradeoff": _node_view(
                        self.nodes_by_id[edge["attributes"]["tradeoff"]]
                    ),
                    "mechanism": _node_view(
                        self.nodes_by_id[edge["attributes"]["mechanism"]]
                    ),
                    "coupling": edge["attributes"]["coupling"],
                    "evidence": edge["evidence"],
                    "scope": edge.get("scope"),
                    "edge_id": edge["id"],
                }
            )
        results.sort(
            key=lambda item: (
                item["spec_row"]["id"],
                item["mechanism"]["id"],
                item["edge_id"],
            )
        )
        return spec, results

    def recipe(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        measurement = self.resolve_node(entity, expected_kinds={"Measurement"})
        producers = [
            edge
            for edge in self.out_edges.get(measurement["id"], [])
            if edge["kind"] == "measured_by"
        ]
        chains = []
        for producer_edge in producers:
            producer_id = producer_edge["target"]
            ordered: list[str] = []
            emitted: set[str] = set()

            def append_dependencies(stage_id: str) -> None:
                dependencies = [
                    edge
                    for edge in self.out_edges.get(stage_id, [])
                    if edge["kind"] == "depends_on"
                ]
                dependencies.sort(
                    key=lambda edge: (
                        self.nodes_by_id[edge["target"]]["attributes"]["order"],
                        edge["target"],
                    )
                )
                for dependency in dependencies:
                    append_dependencies(dependency["target"])
                if stage_id not in emitted:
                    emitted.add(stage_id)
                    ordered.append(stage_id)

            append_dependencies(producer_id)
            stages = []
            for stage_id in ordered:
                dependency_edges = [
                    edge
                    for edge in self.out_edges.get(stage_id, [])
                    if edge["kind"] == "depends_on"
                ]
                dependency_edges.sort(key=lambda edge: edge["target"])
                stages.append(
                    {
                        "stage": _node_view(self.nodes_by_id[stage_id]),
                        "depends_on": [
                            {
                                "stage_id": edge["target"],
                                "bindings": edge["attributes"]["bindings"],
                                "evidence": edge["evidence"],
                            }
                            for edge in dependency_edges
                        ],
                    }
                )
            chains.append(
                {
                    "producer_stage": producer_id,
                    "measurement_binding": producer_edge["attributes"]["binding"],
                    "measurement_evidence": producer_edge["evidence"],
                    "stages": stages,
                }
            )
        chains.sort(key=lambda chain: chain["producer_stage"])
        return measurement, chains

    def validity(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        condition = self.resolve_node(entity, expected_kinds={"Condition", "Corner"})
        assertions = [
            edge
            for edge in self.in_edges.get(condition["id"], [])
            if edge["kind"] in {"valid_when", "invalid_when"}
        ]
        guard_edges = [
            edge
            for edge in self.in_edges.get(condition["id"], [])
            if edge["kind"] == "guards"
        ]
        guards = [
            {
                "gate": _node_view(self.nodes_by_id[edge["source"]]),
                "on_fail": edge["attributes"]["on_fail"],
                "evidence": edge["evidence"],
            }
            for edge in guard_edges
        ]
        guards.sort(key=lambda item: item["gate"]["id"])
        results = []
        for edge in assertions:
            artifact = self.nodes_by_id[edge["source"]]
            affected_devices = [
                _node_view(self.nodes_by_id[model_edge["target"]])
                for model_edge in self.out_edges.get(artifact["id"], [])
                if model_edge["kind"] == "models"
            ]
            repair_edges = [
                repair
                for repair in self.edges_by_id.values()
                if repair["kind"] == "repairs"
                and artifact["id"] in {repair["source"], repair["target"]}
            ]
            repairs = []
            for repair in sorted(repair_edges, key=lambda item: item["id"]):
                other_id = (
                    repair["target"]
                    if repair["source"] == artifact["id"]
                    else repair["source"]
                )
                repairs.append(
                    {
                        "artifact": _node_view(self.nodes_by_id[other_id]),
                        "direction": (
                            "repairs_this"
                            if repair["target"] == artifact["id"]
                            else "repairs_other"
                        ),
                        "attributes": repair["attributes"],
                        "evidence": repair["evidence"],
                    }
                )
            results.append(
                {
                    "assessment": "invalid" if edge["kind"] == "invalid_when" else "valid",
                    "artifact": _node_view(artifact),
                    "affected_devices": sorted(
                        affected_devices, key=lambda item: item["id"]
                    ),
                    "predicate": edge["attributes"]["predicate"],
                    "conclusion": edge["attributes"]["conclusion"],
                    "validation": edge["attributes"]["validation"],
                    "scope": edge.get("scope"),
                    "evidence": edge["evidence"],
                    "repairs": repairs,
                    "guards": guards,
                    "edge_id": edge["id"],
                }
            )
        results.sort(
            key=lambda item: (
                0 if item["assessment"] == "invalid" else 1,
                item["artifact"]["id"],
                item["edge_id"],
            )
        )
        return condition, results


def _compose_signs(signs: Iterable[str]) -> str:
    values = list(signs)
    if "unknown" in values:
        return "unknown"
    if "mixed" in values:
        return "mixed"
    if "none" in values:
        return "none"
    negative_count = sum(value == "negative" for value in values)
    return "negative" if negative_count % 2 else "positive"


def _compose_strength(strengths: Iterable[str]) -> str:
    values = list(strengths)
    if not values:
        return "unknown"
    return max(values, key=lambda value: STRENGTH_RANK[value])


def _compose_grade(grades: Iterable[str]) -> str:
    values = list(grades)
    if not values:
        return "textbook"
    return max(values, key=lambda value: GRADE_RANK[value])


def _lever_path_score(path: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        GRADE_RANK[path["evidence_grade"]],
        STRENGTH_RANK[path["strength"]],
        len(path["path"]),
        tuple(edge["id"] for edge in path["path"]),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        indent=2,
        sort_keys=True,
        allow_nan=False,
        ensure_ascii=False,
    ) + "\n"


def _error_result(kind: str | None, entity: str | None, error: Exception) -> dict[str, Any]:
    code = "kg.query.invalid_graph" if isinstance(error, GraphError) else "kg.query.invalid_request"
    return {
        "schema": RESULT_SCHEMA,
        "implementation": {
            "id": IMPLEMENTATION_ID,
            "version": IMPLEMENTATION_VERSION,
        },
        "status": "unknown",
        "query": {"kind": kind, "entity": entity},
        "diagnostics": [{"code": code, "message": str(error)}],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query the evaluation-local analog-design knowledge graph."
    )
    parser.add_argument(
        "kind",
        choices=("influences", "levers", "tradeoffs", "recipe", "validity"),
        help="typed query kind",
    )
    parser.add_argument("entity", help="full node ID or unambiguous semantic name")
    parser.add_argument(
        "--graph",
        type=Path,
        default=Path(__file__).with_name("seed-pll.json"),
        help="graph JSON path (default: evaluation seed next to this script)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact canonical JSON rather than indented JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        graph = KnowledgeGraph.from_path(args.graph)
        result = graph.query(args.kind, args.entity)
    except (GraphError, QueryError) as exc:
        result = _error_result(args.kind, args.entity, exc)
        sys.stdout.write(canonical_json(result))
        return 2
    if args.compact:
        sys.stdout.write(
            json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                ensure_ascii=False,
            )
            + "\n"
        )
    else:
        sys.stdout.write(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
