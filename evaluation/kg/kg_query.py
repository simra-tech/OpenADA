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
        frozenset({"Parameter", "Topology", "Condition", "Corner", "Mechanism"}),
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
ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,159}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

NODE_ATTRIBUTE_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "Task": (
        frozenset({"task_id", "task_version", "schema_version", "harness", "pdk", "simulator", "status", "image_digest"}),
        frozenset({"task_id", "task_version", "schema_version", "harness", "pdk", "simulator", "status", "image_digest"}),
    ),
    "Device": (
        frozenset({"task_id", "device_class", "instances", "model_name"}),
        frozenset({"task_id", "device_class", "instances"}),
    ),
    "Block": (frozenset({"task_id", "role"}), frozenset({"task_id", "role"})),
    "Topology": (
        frozenset({"task_id", "topology_id", "fixed", "template_hash"}),
        frozenset({"task_id", "topology_id", "fixed"}),
    ),
    "Parameter": (
        frozenset({"task_id", "semantic_name", "value_type", "targets", "domain", "reference"}),
        frozenset({"task_id", "semantic_name", "value_type", "targets", "domain"}),
    ),
    "Measurement": (
        frozenset({"task_id", "semantic_name", "unit", "method_summary"}),
        frozenset({"task_id", "semantic_name", "unit"}),
    ),
    "SpecRow": (
        frozenset({"task_id", "semantic_name", "measurement", "condition", "operator", "unit", "report_only", "limits", "qualifiers"}),
        frozenset({"task_id", "semantic_name", "measurement", "operator", "unit", "report_only", "limits", "qualifiers"}),
    ),
    "Corner": (
        frozenset({"corner_id", "process", "temperature_c", "supply_v", "model_bundle_hash", "status", "task_ids"}),
        frozenset({"corner_id", "process", "temperature_c", "supply_v", "model_bundle_hash", "status", "task_ids"}),
    ),
    "Condition": (
        frozenset({"task_id", "condition_id", "role", "values"}),
        frozenset({"task_id", "condition_id", "role", "values"}),
    ),
    "Mechanism": (
        frozenset({"task_id", "category", "summary"}),
        frozenset({"task_id", "category", "summary"}),
    ),
    "Tradeoff": (
        frozenset({"task_id", "summary", "observed"}),
        frozenset({"task_id", "summary", "observed"}),
    ),
    "RecipeStage": (
        frozenset({"task_id", "stage_id", "order", "analysis", "deck", "outputs"}),
        frozenset({"task_id", "stage_id", "order", "analysis", "deck", "outputs"}),
    ),
    "ValidityGate": (
        frozenset({"task_id", "gate_id", "predicate", "failure_state", "thresholds"}),
        frozenset({"task_id", "gate_id", "predicate", "failure_state", "thresholds"}),
    ),
    "Trap": (
        frozenset({"task_id", "claim", "correction"}),
        frozenset({"task_id", "claim", "correction"}),
    ),
    "ModelArtifact": (
        frozenset({"task_id", "artifact_name", "state", "version", "digest"}),
        frozenset({"task_id", "artifact_name", "state", "version"}),
    ),
}

EDGE_ATTRIBUTE_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "contains": (frozenset({"role"}), frozenset()),
    "implements": (frozenset(), frozenset()),
    "targets": (frozenset({"target_paths"}), frozenset({"target_paths"})),
    "specifies": (frozenset(), frozenset()),
    "evaluated_under": (frozenset({"role"}), frozenset({"role"})),
    "influences": (
        frozenset({"sign", "strength", "quantification"}),
        frozenset({"sign", "strength", "quantification"}),
    ),
    "trades_off": (
        frozenset({"tradeoff", "mechanism", "coupling", "quantification"}),
        frozenset({"tradeoff", "mechanism", "coupling", "quantification"}),
    ),
    "measured_by": (frozenset({"binding"}), frozenset({"binding"})),
    "depends_on": (frozenset({"bindings"}), frozenset({"bindings"})),
    "models": (frozenset({"model_role"}), frozenset({"model_role"})),
    "valid_when": (
        frozenset({"predicate", "conclusion", "validation", "mechanism"}),
        frozenset({"predicate", "conclusion", "validation"}),
    ),
    "invalid_when": (
        frozenset({"predicate", "conclusion", "validation", "mechanism"}),
        frozenset({"predicate", "conclusion", "validation"}),
    ),
    "repairs": (
        frozenset({"change", "validation"}),
        frozenset({"change", "validation"}),
    ),
    "guards": (frozenset({"on_fail"}), frozenset({"on_fail"})),
    "catches": (frozenset({"rationale"}), frozenset({"rationale"})),
}

EVIDENCE_POLICIES: dict[str, frozenset[tuple[str, str]]] = {
    "contains": frozenset({("derived", "task_contract")}),
    "implements": frozenset({("derived", "task_contract")}),
    "targets": frozenset({("derived", "task_contract")}),
    "specifies": frozenset({("derived", "task_contract")}),
    "evaluated_under": frozenset({
        ("derived", "task_contract"), ("derived", "recipe_contract"),
        ("measured", "simulation_sweep"),
    }),
    "influences": frozenset({
        ("measured", "simulation_sweep"), ("derived", "calculation"),
        ("derived", "causal_interpretation"), ("textbook", "textbook_prior"),
    }),
    "trades_off": frozenset({
        ("measured", "simulation_sweep"), ("derived", "causal_interpretation"),
        ("textbook", "textbook_prior"),
    }),
    "measured_by": frozenset({("derived", "recipe_contract")}),
    "depends_on": frozenset({("derived", "recipe_contract")}),
    "models": frozenset({
        ("derived", "task_contract"), ("derived", "causal_interpretation"),
        ("derived", "model_debug"),
    }),
    "valid_when": frozenset({
        ("measured", "model_debug"), ("derived", "model_debug"),
        ("derived", "calculation"),
    }),
    "invalid_when": frozenset({
        ("measured", "model_debug"), ("derived", "model_debug"),
        ("derived", "calculation"), ("derived", "causal_interpretation"),
    }),
    "repairs": frozenset({
        ("measured", "model_debug"), ("derived", "model_debug"),
        ("derived", "causal_interpretation"),
    }),
    "guards": frozenset({
        ("derived", "recipe_contract"), ("derived", "causal_interpretation"),
    }),
    "catches": frozenset({("derived", "causal_interpretation")}),
}

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
SCOPE_RANK = {"compatible": 0, "requires_review": 1, "incompatible": 2}


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
    if len(value) > 512:
        raise GraphError(f"{path}: string exceeds 512 characters")
    return value


def _require_bounded_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise GraphError(f"{path}: expected a string of at most 4000 characters")
    return value


def _require_list(value: Any, *, path: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise GraphError(f"{path}: expected an array")
    if nonempty and not value:
        raise GraphError(f"{path}: array must not be empty")
    return value


def _require_id(value: Any, *, path: str) -> str:
    identifier = _require_string(value, path=path)
    if not ID_RE.fullmatch(identifier):
        raise GraphError(f"{path}: invalid graph identifier {identifier!r}")
    return identifier


def _require_number(value: Any, *, path: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphError(f"{path}: expected a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise GraphError(f"{path}: expected a finite number")
    return value


def _require_integer(value: Any, *, path: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphError(f"{path}: expected an integer")
    if minimum is not None and value < minimum:
        raise GraphError(f"{path}: expected integer >= {minimum}")
    return value


def _require_bool(value: Any, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise GraphError(f"{path}: expected a boolean")
    return value


def _require_enum(value: Any, allowed: Iterable[str], *, path: str) -> str:
    if value not in allowed:
        raise GraphError(f"{path}: expected one of {', '.join(sorted(allowed))}")
    return value


def _require_string_list(
    value: Any,
    *,
    path: str,
    nonempty: bool = False,
    identifiers: bool = False,
) -> list[str]:
    items = _require_list(value, path=path, nonempty=nonempty)
    result: list[str] = []
    for index, item in enumerate(items):
        validator = _require_id if identifiers else _require_string
        result.append(validator(item, path=f"{path}[{index}]"))
    if len(set(result)) != len(result):
        raise GraphError(f"{path}: duplicate array items are forbidden")
    return result


def _require_sha256(value: Any, *, path: str) -> str:
    digest = _require_string(value, path=path)
    if not SHA256_RE.fullmatch(digest):
        raise GraphError(f"{path}: expected sha256:<64 lowercase hex characters>")
    return digest


def _validate_scalar(value: Any, *, path: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        _require_number(value, path=path)
        return
    if isinstance(value, str):
        if len(value) > 512:
            raise GraphError(f"{path}: string exceeds 512 characters")
        return
    if isinstance(value, list):
        if len(value) > 256:
            raise GraphError(f"{path}: array exceeds 256 items")
        if not value:
            # Empty lists satisfy both homogeneous array branches in JSON
            # Schema's oneOf and are therefore intentionally rejected.
            raise GraphError(f"{path}: empty scalar arrays are ambiguous")
        item_type = str if isinstance(value[0], str) else type(value[0])
        for index, item in enumerate(value):
            if item_type is str:
                if not isinstance(item, str) or len(item) > 256:
                    raise GraphError(f"{path}[{index}]: expected bounded string")
            else:
                _require_number(item, path=f"{path}[{index}]")
        return
    raise GraphError(f"{path}: invalid scalar value")


def _validate_named_value(value: Any, *, path: str) -> None:
    if not isinstance(value, dict):
        raise GraphError(f"{path}: expected an object")
    _require_exact_fields(
        value,
        allowed=frozenset({"name", "value", "unit"}),
        required=frozenset({"name", "value"}),
        path=path,
    )
    _require_string(value.get("name"), path=f"{path}.name")
    _validate_scalar(value.get("value"), path=f"{path}.value")
    if "unit" in value:
        _require_string(value["unit"], path=f"{path}.unit")


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
        _require_id(self.payload.get("graph_id"), path="$.graph_id")
        graph_version = _require_string(
            self.payload.get("graph_version"), path="$.graph_version"
        )
        if len(graph_version) > 32 or not SEMVER_RE.fullmatch(graph_version):
            raise GraphError("$.graph_version: expected numeric semantic version")
        _require_string(self.payload.get("title"), path="$.title")
        _require_bounded_string(self.payload.get("description"), path="$.description")
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
        _require_string(
            source_snapshot.get("repository"), path="$.source_snapshot.repository"
        )
        if not GIT_REVISION_RE.fullmatch(revision):
            raise GraphError("$.source_snapshot.revision: expected a full Git SHA")
        if source_snapshot.get("policy") != "tracked-public-files-only":
            raise GraphError("$.source_snapshot.policy: unsupported source policy")
        nodes = _require_list(self.payload.get("nodes"), path="$.nodes", nonempty=True)
        edges = _require_list(self.payload.get("edges"), path="$.edges", nonempty=True)
        if len(nodes) > 4096:
            raise GraphError("$.nodes: exceeds 4096 items")
        if len(edges) > 16384:
            raise GraphError("$.edges: exceeds 16384 items")

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
            node_id = _require_id(raw_node.get("id"), path=f"{path}.id")
            kind = _require_string(raw_node.get("kind"), path=f"{path}.kind")
            if kind not in NODE_KINDS:
                raise GraphError(f"{path}.kind: unknown node kind {kind!r}")
            _require_string(raw_node.get("name"), path=f"{path}.name")
            _require_bounded_string(
                raw_node.get("description"), path=f"{path}.description"
            )
            if not isinstance(raw_node.get("attributes"), dict):
                raise GraphError(f"{path}.attributes: expected an object")
            self._validate_node_attributes(
                kind, raw_node["attributes"], path=f"{path}.attributes"
            )
            if node_id in self.nodes_by_id:
                raise GraphError(f"duplicate node id: {node_id}")
            node = dict(raw_node)
            self.nodes_by_id[node_id] = node
            self._add_aliases(node)

    def _validate_node_attributes(
        self, kind: str, attributes: Mapping[str, Any], *, path: str
    ) -> None:
        allowed, required = NODE_ATTRIBUTE_SHAPES[kind]
        _require_exact_fields(
            attributes, allowed=allowed, required=required, path=path
        )

        def strings(*fields: str) -> None:
            for field in fields:
                if field in attributes:
                    _require_string(attributes[field], path=f"{path}.{field}")

        if kind == "Task":
            strings("task_id", "schema_version", "harness", "pdk", "simulator")
            _require_integer(
                attributes["task_version"], path=f"{path}.task_version", minimum=0
            )
            _require_enum(
                attributes["status"], {"active", "draft"}, path=f"{path}.status"
            )
            _require_sha256(attributes["image_digest"], path=f"{path}.image_digest")
        elif kind == "Device":
            strings("task_id", "model_name")
            _require_enum(
                attributes["device_class"],
                {"matched_group", "transistor", "passive", "model_element"},
                path=f"{path}.device_class",
            )
            _require_string_list(
                attributes["instances"], path=f"{path}.instances", nonempty=True
            )
        elif kind == "Block":
            strings("task_id", "role")
        elif kind == "Topology":
            strings("task_id", "topology_id")
            _require_bool(attributes["fixed"], path=f"{path}.fixed")
            if "template_hash" in attributes:
                _require_sha256(
                    attributes["template_hash"], path=f"{path}.template_hash"
                )
        elif kind == "Parameter":
            strings("task_id", "semantic_name")
            value_type = _require_enum(
                attributes["value_type"],
                {"continuous", "integer"},
                path=f"{path}.value_type",
            )
            _require_string_list(
                attributes["targets"], path=f"{path}.targets", nonempty=True
            )
            domain = attributes["domain"]
            if not isinstance(domain, dict):
                raise GraphError(f"{path}.domain: expected an object")
            if value_type == "continuous":
                _require_exact_fields(
                    domain,
                    allowed=frozenset({"minimum", "maximum", "quantum", "unit"}),
                    required=frozenset({"minimum", "maximum", "quantum"}),
                    path=f"{path}.domain",
                )
                minimum = _require_number(
                    domain["minimum"], path=f"{path}.domain.minimum"
                )
                maximum = _require_number(
                    domain["maximum"], path=f"{path}.domain.maximum"
                )
                quantum = _require_number(
                    domain["quantum"], path=f"{path}.domain.quantum"
                )
                if minimum > maximum:
                    raise GraphError(f"{path}.domain: minimum exceeds maximum")
                if quantum <= 0:
                    raise GraphError(f"{path}.domain.quantum: expected > 0")
                if "unit" in domain:
                    _require_string(domain["unit"], path=f"{path}.domain.unit")
                if "reference" in attributes:
                    reference = _require_number(
                        attributes["reference"], path=f"{path}.reference"
                    )
                    if not minimum <= reference <= maximum:
                        raise GraphError(f"{path}.reference: outside continuous domain")
            else:
                _require_exact_fields(
                    domain,
                    allowed=frozenset({"allowed"}),
                    required=frozenset({"allowed"}),
                    path=f"{path}.domain",
                )
                raw_allowed = _require_list(
                    domain["allowed"], path=f"{path}.domain.allowed", nonempty=True
                )
                allowed_values = [
                    _require_integer(item, path=f"{path}.domain.allowed[{index}]")
                    for index, item in enumerate(raw_allowed)
                ]
                if len(set(allowed_values)) != len(allowed_values):
                    raise GraphError(f"{path}.domain.allowed: duplicate values")
                if "reference" in attributes:
                    reference = _require_integer(
                        attributes["reference"], path=f"{path}.reference"
                    )
                    if reference not in allowed_values:
                        raise GraphError(f"{path}.reference: outside integer domain")
        elif kind == "Measurement":
            strings("task_id", "semantic_name", "unit")
            if "method_summary" in attributes:
                _require_bounded_string(
                    attributes["method_summary"], path=f"{path}.method_summary"
                )
        elif kind == "SpecRow":
            strings("task_id", "semantic_name", "unit")
            _require_id(attributes["measurement"], path=f"{path}.measurement")
            if "condition" in attributes:
                _require_id(attributes["condition"], path=f"{path}.condition")
            operator = _require_enum(
                attributes["operator"],
                {"<=", ">=", "between", "report_only"},
                path=f"{path}.operator",
            )
            report_only = _require_bool(
                attributes["report_only"], path=f"{path}.report_only"
            )
            limits = _require_list(attributes["limits"], path=f"{path}.limits")
            if len(limits) > 64:
                raise GraphError(f"{path}.limits: exceeds 64 items")
            if report_only:
                if operator != "report_only" or limits:
                    raise GraphError(
                        f"{path}: report-only rows require report_only operator and no limits"
                    )
            elif operator == "report_only" or "condition" not in attributes or not limits:
                raise GraphError(
                    f"{path}: scored rows require a condition, scored operator, and limits"
                )
            for index, limit in enumerate(limits):
                limit_path = f"{path}.limits[{index}]"
                if not isinstance(limit, dict):
                    raise GraphError(f"{limit_path}: expected an object")
                if operator == "between":
                    _require_exact_fields(
                        limit,
                        allowed=frozenset({"corner", "minimum", "maximum"}),
                        required=frozenset({"minimum", "maximum"}),
                        path=limit_path,
                    )
                    minimum = _require_number(
                        limit["minimum"], path=f"{limit_path}.minimum"
                    )
                    maximum = _require_number(
                        limit["maximum"], path=f"{limit_path}.maximum"
                    )
                    if minimum > maximum:
                        raise GraphError(f"{limit_path}: minimum exceeds maximum")
                else:
                    _require_exact_fields(
                        limit,
                        allowed=frozenset({"corner", "value"}),
                        required=frozenset({"value"}),
                        path=limit_path,
                    )
                    _require_number(limit["value"], path=f"{limit_path}.value")
                if "corner" in limit:
                    _require_id(limit["corner"], path=f"{limit_path}.corner")
            qualifiers = _require_list(
                attributes["qualifiers"], path=f"{path}.qualifiers"
            )
            if len(qualifiers) > 32:
                raise GraphError(f"{path}.qualifiers: exceeds 32 items")
            for index, qualifier in enumerate(qualifiers):
                _validate_named_value(
                    qualifier, path=f"{path}.qualifiers[{index}]"
                )
        elif kind == "Corner":
            strings("corner_id", "process")
            _require_number(attributes["temperature_c"], path=f"{path}.temperature_c")
            _require_number(attributes["supply_v"], path=f"{path}.supply_v")
            _require_sha256(
                attributes["model_bundle_hash"], path=f"{path}.model_bundle_hash"
            )
            _require_enum(
                attributes["status"], {"active", "historical"}, path=f"{path}.status"
            )
            _require_string_list(
                attributes["task_ids"], path=f"{path}.task_ids", nonempty=True
            )
        elif kind == "Condition":
            strings("task_id", "condition_id")
            _require_enum(
                attributes["role"],
                {"condition_set", "operating_point", "validity_predicate"},
                path=f"{path}.role",
            )
            values = _require_list(
                attributes["values"], path=f"{path}.values", nonempty=True
            )
            if len(values) > 128:
                raise GraphError(f"{path}.values: exceeds 128 items")
            for index, value in enumerate(values):
                _validate_named_value(value, path=f"{path}.values[{index}]")
        elif kind == "Mechanism":
            strings("task_id")
            _require_enum(
                attributes["category"],
                {"physical", "model", "measurement", "structural"},
                path=f"{path}.category",
            )
            _require_bounded_string(attributes["summary"], path=f"{path}.summary")
        elif kind == "Tradeoff":
            strings("task_id")
            _require_bounded_string(attributes["summary"], path=f"{path}.summary")
            _require_bool(attributes["observed"], path=f"{path}.observed")
        elif kind == "RecipeStage":
            strings("task_id", "stage_id", "deck")
            _require_integer(attributes["order"], path=f"{path}.order", minimum=0)
            _require_enum(
                attributes["analysis"],
                {"dc", "tran", "ac", "derived", "gate"},
                path=f"{path}.analysis",
            )
            _require_string_list(
                attributes["outputs"], path=f"{path}.outputs", nonempty=True
            )
        elif kind == "ValidityGate":
            strings("task_id", "gate_id")
            _require_bounded_string(attributes["predicate"], path=f"{path}.predicate")
            _require_enum(
                attributes["failure_state"],
                {"unknown", "invalid", "fail"},
                path=f"{path}.failure_state",
            )
            thresholds = _require_list(
                attributes["thresholds"], path=f"{path}.thresholds"
            )
            if len(thresholds) > 64:
                raise GraphError(f"{path}.thresholds: exceeds 64 items")
            for index, threshold in enumerate(thresholds):
                _validate_named_value(
                    threshold, path=f"{path}.thresholds[{index}]"
                )
        elif kind == "Trap":
            strings("task_id")
            _require_bounded_string(attributes["claim"], path=f"{path}.claim")
            _require_bounded_string(
                attributes["correction"], path=f"{path}.correction"
            )
        elif kind == "ModelArtifact":
            strings("task_id", "artifact_name", "version")
            _require_enum(
                attributes["state"],
                {"unpatched", "patched", "base", "unresolved"},
                path=f"{path}.state",
            )
            if "digest" in attributes:
                _require_sha256(attributes["digest"], path=f"{path}.digest")

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
            edge_id = _require_id(raw_edge.get("id"), path=f"{path}.id")
            kind = _require_string(raw_edge.get("kind"), path=f"{path}.kind")
            if kind not in EDGE_ENDPOINT_KINDS:
                raise GraphError(f"{path}.kind: unknown edge kind {kind!r}")
            source = _require_id(raw_edge.get("source"), path=f"{path}.source")
            target = _require_id(raw_edge.get("target"), path=f"{path}.target")
            if edge_id in self.edges_by_id:
                raise GraphError(f"duplicate edge id: {edge_id}")
            if edge_id in self.nodes_by_id:
                raise GraphError(f"graph id collides across node and edge: {edge_id}")
            if source not in self.nodes_by_id:
                raise GraphError(f"{path}.source: missing node {source!r}")
            if target not in self.nodes_by_id:
                raise GraphError(f"{path}.target: missing node {target!r}")
            if not isinstance(raw_edge.get("attributes"), dict):
                raise GraphError(f"{path}.attributes: expected an object")
            self._validate_evidence(
                raw_edge.get("evidence"), kind=kind, path=f"{path}.evidence"
            )
            if "scope" in raw_edge:
                self._validate_scope(raw_edge["scope"], path=f"{path}.scope")
            elif kind == "influences":
                raise GraphError(f"{path}.scope: influences edges require explicit scope")
            if kind == "influences" and "extrapolation" not in raw_edge["scope"]:
                raise GraphError(
                    f"{path}.scope.extrapolation: influences edges require an explicit policy"
                )
            source_kind = self.nodes_by_id[source]["kind"]
            target_kind = self.nodes_by_id[target]["kind"]
            allowed_sources, allowed_targets = EDGE_ENDPOINT_KINDS[kind]
            if source_kind not in allowed_sources or target_kind not in allowed_targets:
                raise GraphError(
                    f"{path}: {kind} forbids endpoint kinds "
                    f"{source_kind}->{target_kind}"
                )
            self._validate_edge_attributes(
                kind, raw_edge["attributes"], path=f"{path}.attributes"
            )
            if (
                kind in {"influences", "trades_off"}
                and raw_edge["evidence"]["grade"] == "measured"
                and not raw_edge["attributes"]["quantification"]
            ):
                raise GraphError(
                    f"{path}.attributes.quantification: measured {kind} requires numeric observations"
                )
            edge = dict(raw_edge)
            self.edges_by_id[edge_id] = edge
            self.out_edges[source].append(edge)
            self.in_edges[target].append(edge)
        for edges in self.out_edges.values():
            edges.sort(key=lambda edge: edge["id"])
        for edges in self.in_edges.values():
            edges.sort(key=lambda edge: edge["id"])

    def _validate_evidence(self, value: Any, *, kind: str, path: str) -> None:
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
        if (grade, basis) not in EVIDENCE_POLICIES[kind]:
            raise GraphError(
                f"{path}: evidence {grade}/{basis} is forbidden for {kind}"
            )
        if "summary" in value:
            _require_bounded_string(value["summary"], path=f"{path}.summary")
        pointers = _require_list(
            value.get("pointers"), path=f"{path}.pointers", nonempty=True
        )
        if len(pointers) > 16:
            raise GraphError(f"{path}.pointers: exceeds 16 items")
        seen_pointers: set[str] = set()
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
            for field in ("repository", "revision", "section"):
                _require_string(pointer.get(field), path=f"{pointer_path}.{field}")
            if not GIT_REVISION_RE.fullmatch(pointer["revision"]):
                raise GraphError(f"{pointer_path}.revision: expected a full Git SHA")
            snapshot = self.payload["source_snapshot"]
            if pointer["repository"] != snapshot["repository"]:
                raise GraphError(
                    f"{pointer_path}.repository: does not match source_snapshot"
                )
            if pointer["revision"] != snapshot["revision"]:
                raise GraphError(
                    f"{pointer_path}.revision: does not match source_snapshot"
                )
            source_path = pointer.get("path")
            if not isinstance(source_path, str) or not source_path:
                raise GraphError(f"{pointer_path}.path: expected a nonempty string")
            if len(source_path) > 1024:
                raise GraphError(f"{pointer_path}.path: exceeds 1024 characters")
            path_parts = source_path.split("/")
            if source_path.startswith("/") or any(
                part in {"", ".", ".."} for part in path_parts
            ):
                raise GraphError(
                    f"{pointer_path}.path: expected a normalized repository-relative path"
                )
            if "locator" in pointer:
                _require_string(pointer["locator"], path=f"{pointer_path}.locator")
            canonical_pointer = json.dumps(
                pointer, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if canonical_pointer in seen_pointers:
                raise GraphError(f"{path}.pointers: duplicate pointers are forbidden")
            seen_pointers.add(canonical_pointer)

    def _validate_scope(self, value: Any, *, path: str) -> None:
        if not isinstance(value, dict) or not value:
            raise GraphError(f"{path}: expected a nonempty object")
        unknown = sorted(set(value) - SCOPE_FIELDS)
        if unknown:
            raise GraphError(f"{path}: unknown fields: {', '.join(unknown)}")
        if "extrapolation" in value and value["extrapolation"] not in {
            "forbidden",
            "bounded",
            "not_applicable",
        }:
            raise GraphError(f"{path}.extrapolation: unknown policy")
        for field in ("condition_ids", "corner_ids", "co_varied_parameters"):
            if field in value:
                _require_string_list(
                    value[field],
                    path=f"{path}.{field}",
                    nonempty=True,
                    identifiers=True,
                )
        for field in ("design_point", "intervention"):
            if field in value:
                _require_string(value[field], path=f"{path}.{field}")

    def _validate_edge_attributes(
        self, kind: str, attributes: Mapping[str, Any], *, path: str
    ) -> None:
        allowed, required = EDGE_ATTRIBUTE_SHAPES[kind]
        _require_exact_fields(
            attributes, allowed=allowed, required=required, path=path
        )
        if kind == "contains":
            if "role" in attributes:
                _require_string(attributes["role"], path=f"{path}.role")
        elif kind == "targets":
            _require_string_list(
                attributes["target_paths"],
                path=f"{path}.target_paths",
                nonempty=True,
            )
        elif kind == "evaluated_under":
            _require_enum(
                attributes["role"],
                {"condition_set", "corner", "observed_at", "limit"},
                path=f"{path}.role",
            )
        elif kind == "influences":
            _require_enum(attributes["sign"], SIGN_VALUES, path=f"{path}.sign")
            _require_enum(
                attributes["strength"], STRENGTH_VALUES, path=f"{path}.strength"
            )
            self._validate_observations(
                attributes["quantification"], path=f"{path}.quantification"
            )
        elif kind == "trades_off":
            _require_id(attributes["tradeoff"], path=f"{path}.tradeoff")
            _require_id(attributes["mechanism"], path=f"{path}.mechanism")
            _require_enum(
                attributes["coupling"],
                {"antagonistic", "coupled"},
                path=f"{path}.coupling",
            )
            self._validate_observations(
                attributes["quantification"],
                path=f"{path}.quantification",
            )
        elif kind == "measured_by":
            _require_string(attributes["binding"], path=f"{path}.binding")
        elif kind == "depends_on":
            _require_string_list(
                attributes["bindings"], path=f"{path}.bindings", nonempty=True
            )
        elif kind == "models":
            _require_string(attributes["model_role"], path=f"{path}.model_role")
        elif kind in {"valid_when", "invalid_when"}:
            for field in ("predicate", "conclusion", "validation"):
                _require_bounded_string(attributes[field], path=f"{path}.{field}")
            if "mechanism" in attributes:
                _require_id(attributes["mechanism"], path=f"{path}.mechanism")
        elif kind == "repairs":
            for field in ("change", "validation"):
                _require_bounded_string(attributes[field], path=f"{path}.{field}")
        elif kind == "guards":
            _require_enum(
                attributes["on_fail"],
                {"unavailable", "invalid", "fail", "unknown"},
                path=f"{path}.on_fail",
            )
        elif kind == "catches":
            _require_bounded_string(attributes["rationale"], path=f"{path}.rationale")

    def _validate_observations(
        self, value: Any, *, path: str, nonempty: bool = False
    ) -> None:
        observations = _require_list(value, path=path, nonempty=nonempty)
        if len(observations) > 64:
            raise GraphError(f"{path}: exceeds 64 items")
        numeric_fields = {
            "baseline", "observed", "delta", "ratio", "minimum", "maximum"
        }
        for index, observation in enumerate(observations):
            observation_path = f"{path}[{index}]"
            if not isinstance(observation, dict):
                raise GraphError(f"{observation_path}: expected an object")
            _require_exact_fields(
                observation,
                allowed=frozenset(
                    {"quantity", "intervention", "unit", "note"} | numeric_fields
                ),
                required=frozenset({"quantity", "intervention", "unit"}),
                path=observation_path,
            )
            for field in ("quantity", "intervention", "unit"):
                _require_string(
                    observation[field], path=f"{observation_path}.{field}"
                )
            present_numeric = numeric_fields & set(observation)
            if not present_numeric:
                raise GraphError(
                    f"{observation_path}: expected at least one numeric observation"
                )
            for field in present_numeric:
                _require_number(
                    observation[field], path=f"{observation_path}.{field}"
                )
            if "note" in observation:
                _require_bounded_string(
                    observation["note"], path=f"{observation_path}.note"
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
            if "condition" in attributes:
                self._require_node_kind(
                    attributes["condition"],
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
            if edge["kind"] in {"valid_when", "invalid_when"}:
                mechanism = edge["attributes"].get("mechanism")
                if mechanism is not None:
                    self._require_node_kind(
                        mechanism,
                        {"Mechanism"},
                        context=f"{edge['id']}.attributes.mechanism",
                    )

        for node in self.nodes_by_id.values():
            if node["kind"] == "SpecRow":
                attributes = node["attributes"]
                specifies = [
                    edge
                    for edge in self.out_edges.get(node["id"], [])
                    if edge["kind"] == "specifies"
                ]
                if len(specifies) != 1 or specifies[0]["target"] != attributes["measurement"]:
                    raise GraphError(
                        f"{node['id']}: measurement attribute must match exactly one specifies edge"
                    )
                condition_edges = [
                    edge
                    for edge in self.out_edges.get(node["id"], [])
                    if edge["kind"] == "evaluated_under"
                    and edge["attributes"]["role"] == "condition_set"
                ]
                condition = attributes.get("condition")
                if condition is None:
                    if condition_edges:
                        raise GraphError(
                            f"{node['id']}: conditionless row has a condition_set edge"
                        )
                elif len(condition_edges) != 1 or condition_edges[0]["target"] != condition:
                    raise GraphError(
                        f"{node['id']}: condition attribute must match exactly one condition_set edge"
                    )
            elif node["kind"] == "Parameter":
                target_edges = [
                    edge
                    for edge in self.out_edges.get(node["id"], [])
                    if edge["kind"] == "targets"
                ]
                bound_paths = {
                    target_path
                    for edge in target_edges
                    for target_path in edge["attributes"]["target_paths"]
                }
                if bound_paths != set(node["attributes"]["targets"]):
                    raise GraphError(
                        f"{node['id']}: targets attribute disagrees with targets edges"
                    )

    def _require_node_kind(
        self, value: Any, allowed: set[str], *, context: str
    ) -> dict[str, Any]:
        node_id = _require_id(value, path=context)
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

    def _influence_paths(self, source_id: str) -> list[dict[str, Any]]:
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

        walk(source_id, [], frozenset({source_id}))
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
        scope_compatibility, scope_warning = _assess_path_scope(path)
        return {
            "measurement": _node_view(measurement),
            "spec_rows": self._bound_specs(measurement["id"]),
            "sign": _compose_signs(edge["attributes"]["sign"] for edge in path),
            "strength": _compose_strength(strengths),
            "evidence_grade": _compose_grade(grades),
            "mechanism_path": [_node_view(node) for node in mechanism_nodes],
            "path": [_edge_view(edge) for edge in path],
            "extrapolation_policies": extrapolation,
            "scope_compatibility": scope_compatibility,
            "scope_warning": scope_warning,
        }

    def influences(self, entity: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        source = self.resolve_node(
            entity, expected_kinds={"Parameter", "Topology", "Condition", "Corner"}
        )
        return source, self._influence_paths(source["id"])

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
            co_varied_ids = sorted(
                {
                    node_id
                    for edge in best["path"]
                    for node_id in edge.get("scope", {}).get(
                        "co_varied_parameters", []
                    )
                    if node_id != parameter["id"]
                }
            )
            candidates.append(
                {
                    "parameter": _node_view(parameter),
                    "sign": best["sign"],
                    "strength": best["strength"],
                    "evidence_grade": best["evidence_grade"],
                    "best_path": best,
                    "alternative_paths": paths[1:],
                    "requires_co_variation": bool(co_varied_ids),
                    "co_varied_parameters": [
                        _node_view(self.nodes_by_id[node_id])
                        for node_id in co_varied_ids
                    ],
                    "joint_intervention_warning": (
                        "Rank and sign are conditional on the recorded joint intervention; "
                        "this is not an isolated parameter sensitivity."
                        if co_varied_ids
                        else None
                    ),
                    "rank_basis": {
                        "scope_compatibility": best["scope_compatibility"],
                        "evidence_grade": best["evidence_grade"],
                        "strength": best["strength"],
                        "path_length": len(best["path"]),
                    },
                }
            )
        candidates.sort(
            key=lambda item: (
                SCOPE_RANK[item["best_path"]["scope_compatibility"]],
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
                    "quantification": edge["attributes"]["quantification"],
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
                    "mechanism": (
                        _node_view(
                            self.nodes_by_id[edge["attributes"]["mechanism"]]
                        )
                        if "mechanism" in edge["attributes"]
                        else None
                    ),
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
    # A no-effect segment absorbs the chain.  Otherwise uncertainty takes
    # precedence over interval-dependent direction before parity composition.
    if "none" in values:
        return "none"
    if "unknown" in values:
        return "unknown"
    if "mixed" in values:
        return "mixed"
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


def _assess_path_scope(
    path: Sequence[Mapping[str, Any]],
) -> tuple[str, str | None]:
    """Conservatively assess whether scoped claims may be composed.

    Condition/corner identifiers are machine identities, so disjoint explicit
    sets are incompatible.  Design-point and intervention prose are not a
    formal algebra: differing strings trigger review instead of being silently
    treated as the same experiment or rejected as provably disjoint.
    """

    corner_sets = [
        set(edge.get("scope", {}).get("corner_ids", []))
        for edge in path
        if edge.get("scope", {}).get("corner_ids")
    ]
    if corner_sets and not set.intersection(*corner_sets):
        return (
            "incompatible",
            "Path joins disjoint corner_ids; it is returned only as a caveat and must not be extrapolated.",
        )
    condition_sets = [
        set(edge.get("scope", {}).get("condition_ids", []))
        for edge in path
        if edge.get("scope", {}).get("condition_ids")
    ]
    conditions_require_review = bool(
        condition_sets and not set.intersection(*condition_sets)
    )
    design_points = {
        edge.get("scope", {}).get("design_point")
        for edge in path
        if edge.get("scope", {}).get("design_point")
    }
    interventions = {
        edge.get("scope", {}).get("intervention")
        for edge in path
        if edge.get("scope", {}).get("intervention")
    }
    if conditions_require_review or len(design_points) > 1 or len(interventions) > 1:
        return (
            "requires_review",
            "Path edges use distinct condition sets, prose-scoped design points, or interventions; composition is a reasoning prior, not a measured end-to-end sensitivity.",
        )
    return "compatible", None


def _lever_path_score(path: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        SCOPE_RANK[path["scope_compatibility"]],
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
