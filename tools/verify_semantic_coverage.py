#!/usr/bin/env python3
"""Audit the repository's exposed semantic surface and evidence coverage.

Audit mode always reports the complete deterministic gap matrix. Inventory or
schema errors exit 2. Coverage gaps exit 1 only in agent-ready/release mode or
when --fail-on-gaps is explicit; this lets development expose debt without
allowing a release gate to mistake implementation maturity for evidence.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from semantic_receipts import (
    SemanticReceiptError,
    provider_manifest_semantic_sha256_bytes,
    semantic_subject as shared_semantic_subject,
    semantic_subject_relative_paths as shared_semantic_subject_relative_paths,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from openada.cli import _semantic_capability_records, build_parser  # noqa: E402
from openada.preflight import PREFLIGHT_SPECS  # noqa: E402
from openada.provider_runtime import provider_manifest_issues  # noqa: E402


DEFAULT_CATALOG = ROOT / "catalog" / "semantic-surfaces-v0alpha1.json"
DEFAULT_CHAIN_INDEX = ROOT / "conformance" / "semantic-chains" / "index.json"
CATALOG_SCHEMA = ROOT / "schemas" / "semantic-surface-catalog-v0alpha1.schema.json"
CHAIN_SCHEMA = ROOT / "schemas" / "semantic-chain-manifest-v0alpha1.schema.json"
RUN_SCHEMA = ROOT / "schemas" / "semantic-chain-run-v0alpha1.schema.json"
DESIGN_PROVENANCE_SCHEMA = ROOT / "schemas" / "design-provenance-v0alpha1.schema.json"

MAX_JSON_BYTES = 16 * 1024 * 1024
LEVELS = (
    "unverified",
    "contract-tested",
    "native-replayed",
    "workflow-validated",
    "agent-ready",
)
LEVEL_RANK = {value: index for index, value in enumerate(LEVELS)}
EVIDENCE_ORDER = (
    "contract-test",
    "native-run",
    "independent-artifact-check",
    "pinned-real-design",
    "normalized-evidence",
    "downstream-decision",
    "negative-replay",
    "tamper-replay",
    "agent-visible-evidence",
)
CHECK_TO_EVIDENCE = {
    "contract_test": "contract-test",
    "native_run": "native-run",
    "independent_artifact_check": "independent-artifact-check",
    "pinned_real_design": "pinned-real-design",
    "normalized_evidence": "normalized-evidence",
    "downstream_decision": "downstream-decision",
    "negative_replay": "negative-replay",
    "tamper_replay": "tamper-replay",
    "agent_visible_evidence": "agent-visible-evidence",
}
TRUST_ARTIFACT_ROLES = frozenset(
    {
        "contract-test",
        "design-provenance",
        "native-artifact",
        "independent-oracle",
        "normalized-evidence",
        "downstream-decision",
        "negative-replay",
        "tamper-replay",
        "agent-visible-evidence",
    }
)
REQUIRED_EVIDENCE = {
    "unverified": (),
    "contract-tested": ("contract-test",),
    "native-replayed": (
        "contract-test",
        "native-run",
        "independent-artifact-check",
    ),
    "workflow-validated": (
        "contract-test",
        "native-run",
        "independent-artifact-check",
        "pinned-real-design",
        "normalized-evidence",
        "downstream-decision",
    ),
    "agent-ready": EVIDENCE_ORDER,
}

# These classifications describe the implementation boundary of each shipped
# CLI leaf.  They are deliberately not inferred from the mutable catalog: a
# catalog edit must never turn an EDA operation into an administrative row and
# thereby weaken its release obligation.
SURFACE_CLASSIFICATIONS: dict[tuple[str, ...], str] = {
    ("capabilities",): "discovery",
    ("doctor",): "discovery",
    ("drc",): "semantic-execution",
    ("drc-compare",): "semantic-execution",
    ("drc-review",): "semantic-execution",
    ("evaluate",): "semantic-execution",
    ("extract",): "semantic-execution",
    ("lvs",): "semantic-execution",
    ("measure",): "semantic-execution",
    ("netlist",): "semantic-execution",
    ("profile", "list"): "administrative",
    ("profile", "show"): "administrative",
    ("provider", "invoke"): "transport-execution",
    ("provider", "list"): "administrative",
    ("provider", "validate"): "administrative",
    ("rtl-check",): "semantic-execution",
    ("rtl-lint",): "semantic-execution",
    ("simulate",): "semantic-execution",
    ("synthesize",): "semantic-execution",
    ("timing-analyze",): "semantic-execution",
    ("spectral",): "semantic-execution",
    ("transfer",): "semantic-execution",
}
NATIVE_EDA_SURFACES = frozenset(
    {
        "drc",
        "lvs",
        "netlist",
        "provider-invoke",
        "rtl-check",
        "rtl-lint",
        "simulate",
        "synthesize",
        "timing-analyze",
    }
)
ARTIFACT_KERNEL_SURFACES = frozenset(
    {"evaluate", "extract", "measure", "spectral", "transfer"}
)
NATIVE_EDA_OPERATIONS = frozenset(
    {
        "openada.operation/circuit.simulate/v1alpha1",
        "openada.operation/circuit.simulate/v1alpha2",
        "openada.operation/logic.synthesize/v1alpha1",
        "openada.operation/rtl.lint/v1alpha1",
        "openada.operation/timing.analyze/v1alpha1",
    }
)


class CoverageInputError(RuntimeError):
    """A bounded repository input cannot be read safely."""


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CoverageInputError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CoverageInputError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= MAX_JSON_BYTES:
        raise CoverageInputError(
            f"{label} size {metadata.st_size} is outside 1..{MAX_JSON_BYTES} bytes"
        )
    try:
        encoded = path.read_bytes()
        if len(encoded) != metadata.st_size:
            raise CoverageInputError(f"{label} changed while being read: {path}")
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CoverageInputError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CoverageInputError(f"{label} root must be an object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _repository_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise CoverageInputError(f"{label} must be a nonempty repository-relative path")
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise CoverageInputError(f"{label} escapes the repository root: {value!r}") from exc
    return candidate


def _schema_issues(payload: object, schema_path: Path, *, label: str) -> list[str]:
    schema = _load_json(schema_path, label=f"{label} schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    issues: list[str] = []
    for error in sorted(
        validator.iter_errors(payload),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        pointer = "#" + "".join(f"/{part}" for part in error.absolute_path)
        issues.append(f"{label} {pointer}: {error.message}")
        if len(issues) == 128:
            issues.append(f"{label}: additional schema issues omitted")
            break
    return issues


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _parser_leaves() -> list[tuple[str, ...]]:
    import argparse as argparse_module

    def visit(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
        subparser_actions = [
            action
            for action in parser._actions
            if isinstance(action, argparse_module._SubParsersAction)
        ]
        if not subparser_actions:
            return [prefix]
        leaves: list[tuple[str, ...]] = []
        for action in subparser_actions:
            for name, child in sorted(action.choices.items()):
                leaves.extend(visit(child, (*prefix, name)))
        return leaves

    return sorted(visit(build_parser(), ()))


def _normalize_capability(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_id": record["provider_id"],
        "provider_kind": record["provider_kind"],
        "native_product": record["native_product"],
        "operation_profile": record["operation_profile"],
        "assertion_profile": record["assertion_profile"],
        "operation_maturity": None,
        "features": sorted(
            (
                {
                    "id": feature["id"],
                    "implementation_maturity": feature["maturity"],
                    "conformance_ids": sorted(feature["conformance_ids"]),
                    "extensions": {},
                }
                for feature in record["features"]
            ),
            key=lambda item: item["id"],
        ),
        "extensions": {},
    }


def _mapping_key(record: dict[str, Any]) -> str:
    return f"{record['provider_id']}|{record['operation_profile']}"


def _profile_inventory(catalog: dict[str, Any], issues: list[str]) -> dict[str, dict[str, Any]]:
    records = catalog.get("profiles", [])
    paths = [record.get("repository_path", "") for record in records if isinstance(record, dict)]
    for duplicate in _duplicates(paths):
        issues.append(f"catalog profile path is duplicated: {duplicate}")

    actual_paths = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "profiles").glob("*.json")
    )
    expected_paths = sorted(paths)
    for missing in sorted(set(actual_paths) - set(expected_paths)):
        issues.append(f"packaged profile is not cataloged: {missing}")
    for unexpected in sorted(set(expected_paths) - set(actual_paths)):
        issues.append(f"catalog profile does not exist: {unexpected}")

    inventory: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("repository_path"), str):
            continue
        relative = record["repository_path"]
        try:
            path = _repository_path(relative, label="catalog profile path")
            profile = _load_json(path, label="operation profile")
        except CoverageInputError as exc:
            issues.append(str(exc))
            continue
        profile_schema_id = profile.get("schema")
        profile_schema_paths = {
            "openada.operation-profile/v0alpha1": ROOT
            / "schemas"
            / "operation-profile-v0alpha1.schema.json",
            "openada.operation-profile/v0alpha2": ROOT
            / "schemas"
            / "operation-profile-v0alpha2.schema.json",
        }
        profile_schema_path = profile_schema_paths.get(profile_schema_id)
        if profile_schema_path is None:
            issues.append(f"profile has an unsupported schema: {relative}")
            continue
        validation_issues = _schema_issues(
            profile,
            profile_schema_path,
            label=f"operation profile {relative}",
        )
        issues.extend(validation_issues)
        if validation_issues:
            continue
        operation = profile.get("operation", {}).get("id")
        assertion = profile.get("assertion", {}).get("id")
        if not isinstance(operation, str) or not isinstance(assertion, str):
            issues.append(f"profile lacks an operation/assertion identity: {relative}")
            continue
        if operation in inventory:
            issues.append(f"operation profile identity is duplicated: {operation}")
            continue
        inventory[operation] = {
            "path": relative,
            "sha256": _sha256(path),
            "lifecycle": record.get("lifecycle"),
            "dispatchable": record.get("dispatchable"),
            "operation_profile": operation,
            "assertion_profile": assertion,
            "features": [item.get("id") for item in profile.get("features", [])],
            "native_mappings": profile.get("native_mappings", []),
        }
    return inventory


def _surface_inventory(
    catalog: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    issues: list[str],
) -> dict[str, dict[str, Any]]:
    records = catalog.get("surfaces", [])
    ids = [record.get("surface_id", "") for record in records if isinstance(record, dict)]
    for duplicate in _duplicates(ids):
        issues.append(f"catalog surface ID is duplicated: {duplicate}")
    command_paths = [
        tuple(record.get("command_path", []))
        for record in records
        if isinstance(record, dict)
    ]
    for duplicate in _duplicates(" ".join(path) for path in command_paths):
        issues.append(f"catalog command leaf is classified more than once: {duplicate}")

    parser_paths = set(_parser_leaves())
    catalog_paths = set(command_paths)
    for missing in sorted(parser_paths - catalog_paths):
        issues.append(f"CLI leaf is not classified: {' '.join(missing)}")
    for unexpected in sorted(catalog_paths - parser_paths):
        issues.append(f"catalog command path is not a CLI leaf: {' '.join(unexpected)}")

    implementation_paths = set(SURFACE_CLASSIFICATIONS)
    for missing in sorted(parser_paths - implementation_paths):
        issues.append(
            "CLI leaf lacks an implementation-owned classification: "
            + " ".join(missing)
        )
    for stale in sorted(implementation_paths - parser_paths):
        issues.append(
            "implementation-owned classification is not a CLI leaf: "
            + " ".join(stale)
        )
    for record in records:
        if not isinstance(record, dict):
            continue
        command_path = tuple(record.get("command_path", []))
        expected_classification = SURFACE_CLASSIFICATIONS.get(command_path)
        if (
            expected_classification is not None
            and record.get("classification") != expected_classification
        ):
            issues.append(
                f"catalog command {' '.join(command_path)} classification differs "
                f"from implementation: catalog={record.get('classification')!r}, "
                f"implementation={expected_classification!r}"
            )

    inventory: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("surface_id"), str):
            continue
        surface_id = record["surface_id"]
        inventory[surface_id] = record
        for binding in record.get("profile_bindings", []):
            profile = profiles.get(binding)
            if profile is None:
                issues.append(f"surface {surface_id} references unknown profile {binding}")
            elif not profile["dispatchable"]:
                issues.append(f"surface {surface_id} binds non-dispatchable profile {binding}")
        variants = record.get("variants", [])
        variant_ids = [item.get("variant_id", "") for item in variants if isinstance(item, dict)]
        for duplicate in _duplicates(variant_ids):
            issues.append(f"surface {surface_id} duplicates variant {duplicate}")
        for variant in variants:
            operation = variant.get("operation_profile") if isinstance(variant, dict) else None
            if operation is not None and operation not in profiles:
                issues.append(
                    f"surface {surface_id} variant references unknown profile {operation}"
                )
    bound_profiles = {
        binding
        for record in records
        if isinstance(record, dict)
        for binding in record.get("profile_bindings", [])
    }
    for operation, profile in sorted(profiles.items()):
        if profile["lifecycle"] == "active" and profile["dispatchable"]:
            if operation not in bound_profiles:
                issues.append(f"active dispatchable profile has no semantic surface: {operation}")
    return inventory


def _preflight_inventory(
    catalog: dict[str, Any],
    surfaces: dict[str, dict[str, Any]],
    issues: list[str],
) -> None:
    records = catalog.get("preflight_assertions", [])
    by_assertion = {
        record.get("assertion"): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("assertion"), str)
    }
    if len(by_assertion) != len(records):
        issues.append("catalog preflight assertions contain a duplicate or malformed row")
    actual = set(PREFLIGHT_SPECS)
    expected = set(by_assertion)
    for missing in sorted(actual - expected):
        issues.append(f"preflight assertion is not cataloged: {missing}")
    for unexpected in sorted(expected - actual):
        issues.append(f"catalog preflight assertion is not implemented: {unexpected}")
    for assertion in sorted(actual & expected):
        record = by_assertion[assertion]
        spec = PREFLIGHT_SPECS[assertion]
        if record.get("target_operation") != spec.operation:
            issues.append(
                f"preflight {assertion} target differs: catalog={record.get('target_operation')!r}, "
                f"implementation={spec.operation!r}"
            )
        surface = surfaces.get(record.get("target_surface_id"))
        if surface is None:
            issues.append(f"preflight {assertion} references an unknown target surface")
        elif surface.get("command_path", [None])[0] != spec.operation:
            issues.append(f"preflight {assertion} target surface does not route to {spec.operation}")


def _provider_inventory(
    catalog: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    issues: list[str],
) -> list[dict[str, Any]]:
    expected_records = catalog.get("provider_mappings", [])
    actual_records = [
        _normalize_capability(record) for record in _semantic_capability_records({})
    ]
    expected_by_key: dict[str, dict[str, Any]] = {}
    actual_by_key: dict[str, dict[str, Any]] = {}
    for label, records, destination in (
        ("catalog", expected_records, expected_by_key),
        ("implementation", actual_records, actual_by_key),
    ):
        for record in records:
            if not isinstance(record, dict):
                continue
            key = _mapping_key(record)
            if key in destination:
                issues.append(f"{label} provider mapping is duplicated: {key}")
            destination[key] = record
    for missing in sorted(set(actual_by_key) - set(expected_by_key)):
        issues.append(f"exposed provider mapping is not cataloged: {missing}")
    for unexpected in sorted(set(expected_by_key) - set(actual_by_key)):
        issues.append(f"catalog provider mapping is not exposed: {unexpected}")
    for key in sorted(set(expected_by_key) & set(actual_by_key)):
        expected = expected_by_key[key]
        actual = actual_by_key[key]
        if _canonical_sha256(expected) != _canonical_sha256(actual):
            for field in (
                "provider_kind",
                "native_product",
                "operation_profile",
                "assertion_profile",
                "operation_maturity",
            ):
                if expected.get(field) != actual.get(field):
                    issues.append(
                        f"provider mapping {key} differs at {field}: "
                        f"catalog={expected.get(field)!r}, implementation={actual.get(field)!r}"
                    )
            expected_features = {item["id"]: item for item in expected.get("features", [])}
            actual_features = {item["id"]: item for item in actual.get("features", [])}
            for missing in sorted(set(actual_features) - set(expected_features)):
                issues.append(f"provider mapping {key} feature is not cataloged: {missing}")
            for unexpected in sorted(set(expected_features) - set(actual_features)):
                issues.append(f"catalog provider mapping {key} feature is not exposed: {unexpected}")
            for feature in sorted(set(expected_features) & set(actual_features)):
                if _canonical_sha256(expected_features[feature]) != _canonical_sha256(
                    actual_features[feature]
                ):
                    issues.append(f"provider mapping {key} feature metadata differs: {feature}")
        profile = profiles.get(expected.get("operation_profile"))
        if profile is None:
            issues.append(f"provider mapping {key} references an unknown profile")
        elif profile["assertion_profile"] != expected.get("assertion_profile"):
            issues.append(f"provider mapping {key} assertion does not match its profile")
        profile_features = set(profile["features"]) if profile is not None else set()
        for feature in expected.get("features", []):
            if feature.get("id") not in profile_features:
                issues.append(
                    f"provider mapping {key} advertises a feature absent from its profile: "
                    f"{feature.get('id')}"
                )
    return sorted(expected_records, key=_mapping_key)


def _repository_provider_inventory(
    catalog: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    issues: list[str],
) -> list[dict[str, Any]]:
    catalog_records = catalog.get("provider_manifests", [])
    expected_paths = [
        record.get("repository_path", "")
        for record in catalog_records
        if isinstance(record, dict)
    ]
    for duplicate in _duplicates(expected_paths):
        issues.append(f"catalog provider manifest path is duplicated: {duplicate}")
    actual_paths = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "providers").glob("*/driver-manifest.json")
    )
    for missing in sorted(set(actual_paths) - set(expected_paths)):
        issues.append(f"shipped provider manifest is not cataloged: {missing}")
    for unexpected in sorted(set(expected_paths) - set(actual_paths)):
        issues.append(f"catalog provider manifest does not exist: {unexpected}")

    inventories: list[dict[str, Any]] = []
    driver_ids: list[str] = []
    for catalog_record in catalog_records:
        if not isinstance(catalog_record, dict):
            continue
        relative = catalog_record.get("repository_path")
        if not isinstance(relative, str):
            continue
        try:
            path = _repository_path(relative, label="provider manifest path")
            manifest = _load_json(path, label="shipped provider manifest")
        except CoverageInputError as exc:
            issues.append(str(exc))
            continue
        manifest_issues = provider_manifest_issues(manifest)
        for issue in manifest_issues:
            issues.append(f"provider manifest {relative} {issue}")
        if manifest_issues:
            continue
        driver_id = manifest["driver"]["id"]
        driver_ids.append(driver_id)
        for position, capability in enumerate(manifest["capabilities"]):
            operation = capability["operation_profile"]
            profile = profiles.get(operation)
            if profile is None:
                issues.append(
                    f"provider manifest {relative} capability {position} references "
                    f"unknown profile {operation}"
                )
                continue
            if not profile["dispatchable"] or profile["lifecycle"] != "active":
                issues.append(
                    f"provider manifest {relative} capability {position} references "
                    f"a non-active profile {operation}"
                )
            if profile["assertion_profile"] not in capability["assertion_profiles"]:
                issues.append(
                    f"provider manifest {relative} capability {position} omits the "
                    f"profile assertion {profile['assertion_profile']}"
                )
            unexpected_features = set(capability["features"]) - set(profile["features"])
            for feature in sorted(unexpected_features):
                issues.append(
                    f"provider manifest {relative} capability {position} advertises "
                    f"a feature absent from its profile: {feature}"
                )
        inventories.append(
            {
                "repository_path": relative,
                "sha256": _sha256(path),
                "lifecycle": catalog_record["lifecycle"],
                "driver_id": driver_id,
                "driver_version": manifest["driver"]["version"],
                "capabilities": manifest["capabilities"],
                "conformance_records": manifest["conformance_records"],
            }
        )
    for duplicate in _duplicates(driver_ids):
        issues.append(f"shipped provider driver ID is duplicated: {duplicate}")
    return sorted(inventories, key=lambda item: item["driver_id"])


def _validate_native_mapping_providers(
    profiles: Mapping[str, Mapping[str, Any]],
    providers: Iterable[Mapping[str, Any]],
    repository_providers: Iterable[Mapping[str, Any]],
    issues: list[str],
) -> None:
    """Require active native mappings to name an exposed operation provider.

    A native mapping is an implementation claim, not merely a descriptive
    backend label.  Binding its ``driver_id`` to the provider inventory keeps
    provider kind, execution-boundary policy, and coverage identity aligned.
    """

    exposed = {
        (record["operation_profile"], record["provider_id"])
        for record in providers
    }
    exposed.update(
        (capability["operation_profile"], provider["driver_id"])
        for provider in repository_providers
        for capability in provider["capabilities"]
    )
    for operation, profile in sorted(profiles.items()):
        if profile.get("lifecycle") != "active":
            continue
        for mapping in profile.get("native_mappings", []):
            driver_id = mapping.get("driver_id")
            if (operation, driver_id) not in exposed:
                issues.append(
                    f"active native mapping {operation}|{driver_id} does not name "
                    "an exposed provider for that operation"
                )


def _required_level(classification: str, lifecycle: str, policy: dict[str, Any]) -> str | None:
    if lifecycle not in policy.get("active_lifecycles", []):
        return None
    return policy.get("required_levels", {}).get(classification)


def _base_row(
    row_id: str,
    kind: str,
    *,
    lifecycle: str,
    classification: str,
    policy: dict[str, Any],
    operation_profile: str | None = None,
    assertion_profile: str | None = None,
    feature_id: str | None = None,
    provider_id: str | None = None,
    provider_kind: str | None = None,
    implementation_maturity: str | None = None,
    native_mapping: str | None = None,
    conformance_record_ids: list[str] | None = None,
    conformance_claim_digest: str | None = None,
    conformance_claim_uri: str | None = None,
    provider_manifest_path: str | None = None,
    execution_class: str | None = None,
) -> dict[str, Any]:
    if execution_class is None:
        if provider_kind == "eda-driver" or operation_profile in NATIVE_EDA_OPERATIONS:
            execution_class = "native-eda"
        elif provider_kind == "evidence-kernel" or operation_profile is not None:
            execution_class = "artifact-kernel"
        elif classification == "transport-execution":
            execution_class = "native-eda"
        elif classification == "semantic-execution":
            # Unknown semantic execution fails closed at the native boundary.
            execution_class = "native-eda"
        else:
            execution_class = "administrative-routing"
    return {
        "row_id": row_id,
        "kind": kind,
        "lifecycle": lifecycle,
        "classification": classification,
        "execution_class": execution_class,
        "operation_profile": operation_profile,
        "assertion_profile": assertion_profile,
        "feature_id": feature_id,
        "provider_id": provider_id,
        "provider_kind": provider_kind,
        "native_mapping": native_mapping,
        "implementation_maturity": implementation_maturity,
        "conformance_record_ids": sorted(conformance_record_ids or []),
        "conformance_claim_digest": conformance_claim_digest,
        "conformance_claim_uri": conformance_claim_uri,
        "provider_manifest_path": provider_manifest_path,
        "conformance_resolution": None,
        "required_coverage_level": _required_level(classification, lifecycle, policy),
        "coverage_level": "unverified",
        "coverage_record_ids": [],
        "missing_evidence": [],
        "gap": False,
    }


def _coverage_rows(
    catalog: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    surfaces: dict[str, dict[str, Any]],
    providers: list[dict[str, Any]],
    repository_providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    policy = catalog["policy"]
    provider_kinds = {
        record["provider_id"]: record["provider_kind"]
        for record in providers
        if isinstance(record.get("provider_id"), str)
        and isinstance(record.get("provider_kind"), str)
    }
    provider_kinds.update(
        {
            provider["driver_id"]: "eda-driver"
            for provider in repository_providers
        }
    )
    rows: list[dict[str, Any]] = []
    for surface_id, surface in sorted(surfaces.items()):
        command_id = "-".join(surface["command_path"])
        if command_id in NATIVE_EDA_SURFACES:
            surface_execution_class = "native-eda"
        elif command_id in ARTIFACT_KERNEL_SURFACES:
            surface_execution_class = "artifact-kernel"
        else:
            surface_execution_class = "administrative-routing"
        rows.append(
            _base_row(
                f"surface|{surface_id}",
                "surface",
                lifecycle=surface["lifecycle"],
                classification=surface["classification"],
                policy=policy,
                execution_class=surface_execution_class,
            )
        )
        for variant in sorted(surface.get("variants", []), key=lambda item: item["variant_id"]):
            rows.append(
                _base_row(
                    f"surface-variant|{surface_id}|{variant['variant_id']}",
                    "surface-variant",
                    lifecycle=surface["lifecycle"],
                    classification=surface["classification"],
                    policy=policy,
                    operation_profile=variant.get("operation_profile"),
                    provider_id=variant.get("provider_id"),
                    provider_kind=provider_kinds.get(variant.get("provider_id")),
                    execution_class=surface_execution_class,
                )
            )

    for operation, profile in sorted(profiles.items()):
        lifecycle = profile["lifecycle"]
        assertion = profile["assertion_profile"]
        rows.append(
            _base_row(
                f"profile|{operation}",
                "profile",
                lifecycle=lifecycle,
                classification="semantic-execution",
                policy=policy,
                operation_profile=operation,
                assertion_profile=assertion,
            )
        )
        rows.append(
            _base_row(
                f"assertion|{operation}|{assertion}",
                "assertion",
                lifecycle=lifecycle,
                classification="semantic-execution",
                policy=policy,
                operation_profile=operation,
                assertion_profile=assertion,
            )
        )
        for feature in sorted(profile["features"]):
            rows.append(
                _base_row(
                    f"feature|{operation}|{feature}",
                    "feature",
                    lifecycle=lifecycle,
                    classification="semantic-execution",
                    policy=policy,
                    operation_profile=operation,
                    assertion_profile=assertion,
                    feature_id=feature,
                )
            )
        for mapping in sorted(
            profile["native_mappings"],
            key=lambda item: (item.get("driver_id", ""), item.get("native_product_id", "")),
        ):
            mapping_id = (
                f"{mapping.get('driver_id', '@unknown')}|"
                f"{mapping.get('native_product_id', '@unknown')}"
            )
            slices: list[tuple[str, str | None]] = []
            for feature in mapping.get("supported_features", []):
                slices.append((feature, feature))
            for analysis in mapping.get("supported_analyses", []):
                slices.append((f"analysis:{analysis}", None))
            if not slices:
                slices.append(("@operation", None))
            for slice_id, feature in sorted(slices):
                rows.append(
                    _base_row(
                        f"native-mapping|{operation}|{mapping_id}|{slice_id}",
                        "native-mapping",
                        lifecycle=lifecycle,
                        classification="semantic-execution",
                        policy=policy,
                        operation_profile=operation,
                        assertion_profile=assertion,
                        feature_id=feature,
                        provider_id=mapping.get("driver_id"),
                        provider_kind=provider_kinds.get(mapping.get("driver_id")),
                        native_mapping=mapping_id,
                    )
                )

    for record in providers:
        profile = profiles.get(record["operation_profile"])
        lifecycle = profile["lifecycle"] if profile is not None else "active"
        features = record.get("features", [])
        if not features:
            features = [
                {
                    "id": None,
                    "implementation_maturity": record.get("operation_maturity"),
                }
            ]
        for feature in features:
            feature_id = feature.get("id")
            slice_id = feature_id or "@operation"
            rows.append(
                _base_row(
                    f"provider|{record['provider_id']}|{record['operation_profile']}|{slice_id}",
                    "provider-mapping",
                    lifecycle=lifecycle,
                    classification="semantic-execution",
                    policy=policy,
                    operation_profile=record["operation_profile"],
                    assertion_profile=record["assertion_profile"],
                    feature_id=feature_id,
                    provider_id=record["provider_id"],
                    provider_kind=record["provider_kind"],
                    implementation_maturity=feature.get("implementation_maturity"),
                    conformance_record_ids=feature.get("conformance_ids", []),
                )
            )

    for provider in repository_providers:
        for capability_position, capability in enumerate(provider["capabilities"]):
            features = capability["features"] or [None]
            for assertion in sorted(capability["assertion_profiles"]):
                for feature in sorted(features, key=lambda item: item or ""):
                    slice_id = feature or "@operation"
                    rows.append(
                        _base_row(
                            f"repository-provider|{provider['driver_id']}|"
                            f"{capability['operation_profile']}|{assertion}|{slice_id}",
                            "repository-provider-mapping",
                            lifecycle=provider["lifecycle"],
                            classification="semantic-execution",
                            policy=policy,
                            operation_profile=capability["operation_profile"],
                            assertion_profile=assertion,
                            feature_id=feature,
                            provider_id=provider["driver_id"],
                            provider_kind="eda-driver",
                            implementation_maturity=capability["maturity"],
                            native_mapping=f"capability:{capability_position}",
                            conformance_record_ids=capability["conformance_record_ids"],
                        )
                    )
        for record in provider["conformance_records"]:
            rows.append(
                _base_row(
                    f"provider-conformance|{provider['driver_id']}|{record['record_id']}",
                    "provider-conformance-claim",
                    lifecycle=provider["lifecycle"],
                    classification="semantic-execution",
                    policy=policy,
                    operation_profile=record["operation_profile"],
                    assertion_profile=record["assertion_profile"],
                    provider_id=provider["driver_id"],
                    provider_kind="eda-driver",
                    implementation_maturity=record["level"],
                    conformance_record_ids=[record["record_id"]],
                    conformance_claim_digest=record["evidence"]["sha256"],
                    conformance_claim_uri=record["evidence"]["uri"],
                    provider_manifest_path=provider["repository_path"],
                )
            )

    for record in sorted(catalog["preflight_assertions"], key=lambda item: item["assertion"]):
        rows.append(
            _base_row(
                f"preflight|{record['assertion']}",
                "preflight",
                lifecycle="active",
                classification="routing",
                policy=policy,
                assertion_profile=record["assertion"],
            )
        )
    return sorted(rows, key=lambda item: item["row_id"])


def _provider_manifest_semantic_sha256_bytes(encoded: bytes) -> str:
    """Hash provider semantics while detaching the run-receipt claim digest.

    The provider claim points back to a semantic-chain run, whose subject must
    already bind provider behavior.  Normalizing only that detached digest
    breaks the otherwise unavoidable provider-manifest -> run -> subject ->
    provider-manifest cycle; capability, transport, version, environment, and
    every other conformance field remain byte-semantically bound.
    """

    try:
        return provider_manifest_semantic_sha256_bytes(encoded)
    except SemanticReceiptError as exc:
        raise CoverageInputError(str(exc)) from exc


def _provider_manifest_semantic_sha256(path: Path) -> str:
    return _provider_manifest_semantic_sha256_bytes(path.read_bytes())


def _semantic_subject_relative_paths(catalog_path: Path, *, root: Path) -> set[str]:
    try:
        return shared_semantic_subject_relative_paths(root, catalog_path)
    except SemanticReceiptError as exc:
        raise CoverageInputError(str(exc)) from exc


def _semantic_subject(catalog_path: Path, *, root: Path = ROOT) -> str:
    try:
        return shared_semantic_subject(root, catalog_path)
    except SemanticReceiptError as exc:
        raise CoverageInputError(str(exc)) from exc


def _run_git(root: Path, arguments: list[str], *, label: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoverageInputError(f"cannot inspect {label}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CoverageInputError(f"cannot inspect {label}: {detail[-2000:]}")
    return completed.stdout


def _semantic_subject_at_revision(
    catalog_path: Path,
    revision: str,
    *,
    root: Path = ROOT,
) -> tuple[str, str]:
    """Rebuild the semantic subject from committed Git blobs, not worktree bytes."""

    root = root.resolve()
    try:
        relative_catalog = catalog_path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise CoverageInputError(
            "release catalog must be inside the attested repository"
        ) from exc
    current_paths = _semantic_subject_relative_paths(catalog_path, root=root)
    listed = _run_git(
        root,
        [
            "ls-tree",
            "-r",
            "--name-only",
            revision,
            "--",
            "src/openada",
            "profiles",
            "schemas",
            "providers",
            "bin",
            "catalog",
            "tools",
            "pyproject.toml",
        ],
        label=f"semantic subject tree at {revision}",
    ).decode("utf-8").splitlines()
    revision_paths = {
        path
        for path in listed
        if (
            (path.startswith("src/openada/") and path.endswith(".py"))
            or (path.startswith("profiles/") and path.endswith(".json"))
            or (path.startswith("schemas/") and path.endswith(".json"))
            or (path.startswith("providers/") and path.endswith(".json"))
            or path.startswith("bin/openada")
            or path == relative_catalog
            or (
                path.startswith("tools/semantic_")
                and path.endswith(".py")
            )
            or path == "tools/verify_semantic_coverage.py"
            or path == "pyproject.toml"
        )
    }
    if revision_paths != current_paths:
        missing = sorted(current_paths - revision_paths)
        extra = sorted(revision_paths - current_paths)
        raise CoverageInputError(
            "semantic subject file set differs from attested revision"
            + (f"; missing={missing[:8]}" if missing else "")
            + (f"; extra={extra[:8]}" if extra else "")
        )
    entries: list[dict[str, Any]] = []
    for relative in sorted(revision_paths):
        encoded = _run_git(
            root,
            ["show", f"{revision}:{relative}"],
            label=f"semantic subject blob {relative} at {revision}",
        )
        provider_manifest = relative.startswith("providers/") and relative.endswith(
            "/driver-manifest.json"
        )
        entry: dict[str, Any] = {
            "path": relative,
            "sha256": (
                _provider_manifest_semantic_sha256_bytes(encoded)
                if provider_manifest
                else hashlib.sha256(encoded).hexdigest()
            ),
            "digest_policy": (
                "provider-semantics-detached-run-digest"
                if provider_manifest
                else "exact-bytes"
            ),
        }
        if relative.startswith("bin/openada"):
            record = _run_git(
                root,
                ["ls-tree", revision, "--", relative],
                label=f"semantic subject mode {relative} at {revision}",
            ).decode("utf-8")
            mode = record.split(None, 1)[0]
            entry["executable_mode"] = int(mode, 8) & 0o777
        entries.append(entry)
    tree = _run_git(
        root,
        ["rev-parse", f"{revision}^{{tree}}"],
        label=f"repository tree at {revision}",
    ).decode("ascii").strip()
    return _canonical_sha256(entries), tree


def _validate_file_ref(record: dict[str, Any], *, label: str, issues: list[str]) -> Path | None:
    try:
        path = _repository_path(record.get("repository_path"), label=f"{label}.repository_path")
        metadata = path.lstat()
    except (CoverageInputError, OSError) as exc:
        issues.append(str(exc))
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        issues.append(f"{label} must be a regular, non-linked file: {path}")
        return None
    if not 1 <= metadata.st_size <= MAX_JSON_BYTES:
        issues.append(
            f"{label} size {metadata.st_size} is outside 1..{MAX_JSON_BYTES} bytes"
        )
        return None
    if _sha256(path) != record.get("sha256"):
        issues.append(f"{label} SHA-256 does not match: {record.get('repository_path')}")
        return None
    return path


def _record_evidence(
    manifest: dict[str, Any],
    run: dict[str, Any],
    row_id: str,
) -> set[str]:
    evidence = {
        CHECK_TO_EVIDENCE[key]
        for key, value in run["checks"].items()
        if value and key in CHECK_TO_EVIDENCE
    }
    negative_covers = {
        covered
        for replay in manifest["negative_replays"]
        for covered in replay["covers"]
    }
    tamper_covers = {
        covered
        for replay in manifest["tamper_replays"]
        for covered in replay["covers"]
    }
    if row_id not in negative_covers:
        evidence.discard("negative-replay")
    if row_id not in tamper_covers:
        evidence.discard("tamper-replay")
    return evidence


def _row_requires_native_positive_step(row: Mapping[str, Any]) -> bool:
    """Return whether the implementation-owned row class is native EDA."""

    return _row_execution_class(row) == "native-eda"


def _row_execution_class(row: Mapping[str, Any]) -> str:
    declared = row.get("execution_class")
    if isinstance(declared, str):
        return declared
    if row.get("provider_kind") == "eda-driver":
        return "native-eda"
    if row.get("provider_kind") == "evidence-kernel":
        return "artifact-kernel"
    if row.get("operation_profile") in NATIVE_EDA_OPERATIONS:
        return "native-eda"
    if row.get("operation_profile") is not None:
        return "artifact-kernel"
    if row.get("classification") == "transport-execution":
        return "native-eda"
    if row.get("classification") == "semantic-execution":
        return "native-eda"
    return "administrative-routing"


def _step_ancestor_ids(steps: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Build role-mediated step ancestry for a topologically ordered DAG."""

    producer_by_role: dict[str, str] = {}
    ancestors: dict[str, set[str]] = {}
    for step in steps:
        step_id = step["id"]
        lineage: set[str] = set()
        for role in step.get("consumes", []):
            producer = producer_by_role.get(role)
            if producer is not None:
                lineage.add(producer)
                lineage.update(ancestors.get(producer, set()))
        ancestors[step_id] = lineage
        for role in step.get("produces", []):
            producer_by_role.setdefault(role, step_id)
    return ancestors


def _positive_coverage_issues(
    manifest: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    *,
    label: str,
) -> list[str]:
    """Bind every globally claimed row to a positive semantic DAG step."""

    semantic_steps = [
        step for step in manifest["steps"] if step["kind"] == "semantic-command"
    ]
    steps_by_id = {step["id"]: step for step in manifest["steps"]}
    ancestors = _step_ancestor_ids(manifest["steps"])
    native_step_ids = {
        step["id"]
        for step in semantic_steps
        if step.get("native_execution") is True
    }
    agent_step_id = manifest.get("agent_evidence", {}).get("result_step")
    agent_ancestors = ancestors.get(agent_step_id, set())
    declared = set(manifest["covers"])
    exercised = {
        row_id for step in semantic_steps for row_id in step.get("covers", [])
    }
    issues: list[str] = []
    unexercised = sorted(declared - exercised)
    if unexercised:
        issues.append(
            f"{label} manifest.covers rows lack a positive semantic-command step: "
            + ", ".join(unexercised)
        )
    undeclared = sorted(exercised - declared)
    if undeclared:
        issues.append(
            f"{label} semantic-command steps cover rows absent from manifest.covers: "
            + ", ".join(undeclared)
        )
    for row_id in sorted(declared & exercised):
        row = rows_by_id.get(row_id)
        if row is None:
            continue
        covering = [
            step for step in semantic_steps if row_id in step.get("covers", [])
        ]
        execution_class = _row_execution_class(row)
        if execution_class == "native-eda" and not any(
            step.get("native_execution") is True for step in covering
        ):
            issues.append(
                f"{label} native EDA row lacks a covering native "
                f"semantic-command step: {row_id}"
            )
        elif execution_class == "artifact-kernel" and not any(
            step.get("native_execution") is False
            and bool(ancestors.get(step["id"], set()) & native_step_ids)
            for step in covering
        ):
            issues.append(
                f"{label} artifact-kernel row lacks a covering nonnative step "
                f"transitively consuming native evidence: {row_id}"
            )
        elif execution_class not in {
            "native-eda",
            "artifact-kernel",
            "administrative-routing",
        }:
            issues.append(
                f"{label} row has an unknown execution class "
                f"{execution_class!r}: {row_id}"
            )
        if agent_step_id is not None and (
            agent_step_id not in steps_by_id
            or not any(step["id"] in agent_ancestors for step in covering)
        ):
            issues.append(
                f"{label} row's positive output is not transitively consumed by "
                f"agent_evidence.result_step: {row_id}"
            )
    return issues


def _run_artifact_issues(
    run: dict[str, Any],
    manifest: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    issues: list[str] = []
    artifacts = run["artifacts"]
    steps_by_id = {step["id"]: step for step in manifest.get("steps", [])}
    negative_replay_ids = [
        replay["id"] for replay in manifest.get("negative_replays", [])
    ]
    tamper_replay_ids = [
        replay["id"] for replay in manifest.get("tamper_replays", [])
    ]
    negative_replays = set(negative_replay_ids)
    tamper_replays = set(tamper_replay_ids)

    for replay_kind, replay_ids in (
        ("negative", negative_replay_ids),
        ("tamper", tamper_replay_ids),
    ):
        for duplicate in _duplicates(replay_ids):
            issues.append(f"{label} duplicates {replay_kind} replay ID {duplicate}")

    invalid_positions: set[int] = set()
    path_positions: dict[str, list[int]] = {}
    digest_positions: dict[str, list[int]] = {}
    for position, artifact in enumerate(artifacts):
        path_positions.setdefault(artifact["repository_path"], []).append(position)
        if artifact["role"] in TRUST_ARTIFACT_ROLES:
            digest_positions.setdefault(artifact["sha256"], []).append(position)
    for repository_path, positions in sorted(path_positions.items()):
        if len(positions) > 1:
            invalid_positions.update(positions)
            rendered = ", ".join(str(position) for position in positions)
            issues.append(
                f"{label} artifacts {rendered} reuse repository path "
                f"{repository_path!r}"
            )
    for digest, positions in sorted(digest_positions.items()):
        if len(positions) > 1:
            invalid_positions.update(positions)
            rendered = ", ".join(
                f"{position} ({artifacts[position]['role']})"
                for position in positions
            )
            issues.append(
                f"{label} trust artifacts {rendered} reuse SHA-256 {digest}"
            )

    roles: set[str] = set()
    replay_artifact_counts: Counter[tuple[str, str]] = Counter()
    for position, artifact in enumerate(artifacts):
        artifact_label = f"{label} artifact {position}"
        content_valid = True
        try:
            path = _repository_path(
                artifact["repository_path"],
                label=f"{artifact_label}.repository_path",
            )
            unresolved_path = ROOT / artifact["repository_path"]
            if unresolved_path.absolute() != path:
                issues.append(
                    f"{artifact_label} must use a canonical, non-symlink repository path"
                )
                content_valid = False
            metadata = path.lstat()
        except (CoverageInputError, OSError) as exc:
            issues.append(str(exc))
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            issues.append(f"{artifact_label} must be a regular, non-linked file")
            content_valid = False
        if metadata.st_size != artifact["bytes"]:
            issues.append(f"{artifact_label} byte count does not match")
            content_valid = False
        if _sha256(path) != artifact["sha256"]:
            issues.append(f"{artifact_label} SHA-256 does not match")
            content_valid = False

        role = artifact["role"]
        source_step = artifact.get("source_step")
        source_output = artifact.get("source_output")
        replay_id = artifact.get("replay_id")
        origin_valid = True
        if role in {"negative-replay", "tamper-replay"}:
            expected_replays = (
                negative_replays if role == "negative-replay" else tamper_replays
            )
            if replay_id not in expected_replays:
                issues.append(
                    f"{artifact_label} {role} does not identify a declared replay: "
                    f"{replay_id!r}"
                )
                origin_valid = False
        else:
            step = steps_by_id.get(source_step)
            if step is None:
                issues.append(
                    f"{artifact_label} references unknown source step {source_step!r}"
                )
                origin_valid = False
            elif source_output not in step["produces"]:
                issues.append(
                    f"{artifact_label} source output {source_output!r} is not produced "
                    f"by step {source_step!r}"
                )
                origin_valid = False
            elif role == "design-provenance" and step["kind"] != "source-materialize":
                issues.append(
                    f"{artifact_label} design-provenance must come from a "
                    "source-materialize step"
                )
                origin_valid = False
            elif role == "native-artifact" and not (
                step["kind"] == "semantic-command" and step["native_execution"]
            ):
                issues.append(
                    f"{artifact_label} native-artifact must come from a native "
                    "semantic-command step"
                )
                origin_valid = False
            elif role == "independent-oracle" and step["kind"] != "independent-oracle":
                issues.append(
                    f"{artifact_label} independent-oracle must come from an "
                    "independent-oracle step"
                )
                origin_valid = False
            elif role == "normalized-evidence" and not (
                step["kind"] == "semantic-command" and not step["native_execution"]
            ):
                issues.append(
                    f"{artifact_label} normalized-evidence must come from a nonnative "
                    "semantic-command step"
                )
                origin_valid = False
            elif role == "downstream-decision" and not (
                step["kind"] == "semantic-command" and not step["native_execution"]
            ):
                issues.append(
                    f"{artifact_label} downstream-decision must come from a "
                    "nonnative semantic-command step"
                )
                origin_valid = False
            elif role == "agent-visible-evidence":
                if source_step != manifest.get("agent_evidence", {}).get(
                    "result_step"
                ):
                    issues.append(
                        f"{artifact_label} agent-visible-evidence must come from the "
                        "declared agent_evidence.result_step"
                    )
                    origin_valid = False
                elif step["kind"] != "independent-decision":
                    issues.append(
                        f"{artifact_label} agent-visible-evidence must come from an "
                        "independent-decision step"
                    )
                    origin_valid = False

        if content_valid and origin_valid and position not in invalid_positions:
            roles.add(role)
            if role in {"negative-replay", "tamper-replay"}:
                replay_artifact_counts[(role, replay_id)] += 1

    required_roles = {
        "contract_test": "contract-test",
        "pinned_real_design": "design-provenance",
        "native_run": "native-artifact",
        "independent_artifact_check": "independent-oracle",
        "normalized_evidence": "normalized-evidence",
        "downstream_decision": "downstream-decision",
        "negative_replay": "negative-replay",
        "tamper_replay": "tamper-replay",
        "agent_visible_evidence": "agent-visible-evidence",
    }
    for check, role in required_roles.items():
        if run["checks"][check] and role not in roles:
            issues.append(f"{label} check {check} lacks a verified {role!r} artifact")
    for check, role, replay_ids in (
        ("negative_replay", "negative-replay", negative_replay_ids),
        ("tamper_replay", "tamper-replay", tamper_replay_ids),
    ):
        if not run["checks"][check]:
            continue
        for replay_id in replay_ids:
            count = replay_artifact_counts[(role, replay_id)]
            if count != 1:
                issues.append(
                    f"{label} check {check} requires exactly one verified {role!r} "
                    f"artifact for replay {replay_id!r}; found {count}"
                )
    real_design = manifest["design"]["class"] in {"public-design", "public-tapeout"}
    if run["checks"]["pinned_real_design"] != real_design:
        issues.append(
            f"{label} pinned_real_design check does not match manifest design class"
        )
    return issues


def _json_pointer_exists(document: object, pointer: str) -> bool:
    current = document
    for encoded_token in pointer[1:].split("/"):
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                return False
            index = int(token)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _agent_pointer_issues(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    label: str,
) -> list[str]:
    issues: list[str] = []
    candidates = [
        artifact
        for artifact in run.get("artifacts", [])
        if artifact.get("role") == "agent-visible-evidence"
    ]
    if len(candidates) != 1:
        return [
            f"{label} must contain exactly one agent-visible-evidence artifact; "
            f"found {len(candidates)}"
        ]
    try:
        path = _repository_path(
            candidates[0]["repository_path"],
            label=f"{label} agent-visible-evidence repository path",
        )
        document = _load_json(path, label=f"{label} agent-visible-evidence")
    except (CoverageInputError, KeyError) as exc:
        return [str(exc)]
    for pointer in manifest["agent_evidence"]["required_json_pointers"]:
        if not _json_pointer_exists(document, pointer):
            issues.append(
                f"{label} agent-visible evidence lacks required JSON pointer {pointer!r}"
            )
    return issues


def _design_provenance_issues(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    label: str,
) -> list[str]:
    design = manifest["design"]
    issues: list[str] = []
    if design["class"] not in {"public-design", "public-tapeout"}:
        return issues
    if not design["repository"].startswith("https://"):
        issues.append(f"{label} public design repository must use HTTPS")
    if design["revision"] == "0" * 40 or design["tree"] == "0" * 40:
        issues.append(f"{label} public design revision/tree may not be placeholders")
    spdx = design["license"]["spdx"]
    if spdx in {"NONE", "NOASSERTION"} or not all(
        character.isalnum() or character in ".+-" for character in spdx
    ):
        issues.append(f"{label} public design license is not one SPDX identifier")
    design_paths = [design["license"]["path"], *[item["path"] for item in design["inputs"]]]
    for duplicate in _duplicates(design_paths):
        issues.append(f"{label} public design path is duplicated: {duplicate}")

    candidates = [
        artifact
        for artifact in run.get("artifacts", [])
        if artifact.get("role") == "design-provenance"
    ]
    if len(candidates) != 1:
        issues.append(
            f"{label} must contain exactly one design-provenance artifact; "
            f"found {len(candidates)}"
        )
        return issues
    artifact = candidates[0]
    try:
        path = _repository_path(
            artifact["repository_path"],
            label=f"{label} design-provenance repository path",
        )
        provenance = _load_json(path, label=f"{label} design provenance")
    except (CoverageInputError, KeyError) as exc:
        issues.append(str(exc))
        return issues
    schema_issues = _schema_issues(
        provenance,
        DESIGN_PROVENANCE_SCHEMA,
        label=f"{label} design provenance",
    )
    issues.extend(schema_issues)
    if schema_issues:
        return issues
    for field in ("repository", "revision", "tree"):
        if provenance[field] != design[field]:
            issues.append(f"{label} design provenance {field} differs from manifest")
    expected_license = {
        "path": design["license"]["path"],
        "sha256": design["license"]["sha256"],
    }
    observed_license = {
        key: provenance["license"][key] for key in ("path", "sha256")
    }
    if observed_license != expected_license:
        issues.append(f"{label} design provenance license differs from manifest")
    expected_inputs = sorted(
        ({"path": item["path"], "sha256": item["sha256"]} for item in design["inputs"]),
        key=lambda item: item["path"],
    )
    observed_inputs = sorted(
        ({"path": item["path"], "sha256": item["sha256"]} for item in provenance["inputs"]),
        key=lambda item: item["path"],
    )
    if observed_inputs != expected_inputs:
        issues.append(f"{label} design provenance inputs differ from manifest")
    return issues


def _source_attestation_issues(
    run: Mapping[str, Any],
    semantic_subject: str,
    catalog_path: Path,
    *,
    label: str,
    mode: str,
) -> list[str]:
    source = run["source_attestation"]
    issues: list[str] = []
    if source["semantic_subject_sha256"] != run["semantic_subject_sha256"]:
        issues.append(f"{label} source attestation does not bind the run semantic subject")
    if source["semantic_subject_sha256"] != semantic_subject:
        issues.append(f"{label} source attestation is stale for current semantics")
    if mode != "release":
        return issues
    if source["receipt_class"] != "release":
        issues.append(f"{label} is provisional and cannot enter a release index")
    if not (
        source["clean_before"]
        and source["clean_after"]
        and source["state_unchanged"]
    ):
        issues.append(f"{label} release receipt was not replayed from unchanged clean source")
    try:
        committed_subject, committed_tree = _semantic_subject_at_revision(
            catalog_path,
            source["repository_revision"],
        )
    except CoverageInputError as exc:
        issues.append(f"{label} source revision cannot be verified: {exc}")
    else:
        if committed_subject != semantic_subject:
            issues.append(
                f"{label} attested revision does not contain the current semantic subject"
            )
        if committed_tree != source["repository_tree"]:
            issues.append(f"{label} source attestation repository tree differs from Git")
    return issues


def _release_verification_issues(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    label: str,
    mode: str,
) -> list[str]:
    verification = manifest["release_verification"]
    issues: list[str] = []
    implementation = verification["implementation"]
    path = _validate_file_ref(
        implementation,
        label=f"{label} release verifier",
        issues=issues,
    )
    trusted_implementations = {
        (step["implementation"]["repository_path"], step["implementation"]["sha256"])
        for step in manifest["steps"]
        if step["kind"] in {"independent-oracle", "independent-decision"}
        and "implementation" in step
    }
    if (implementation["repository_path"], implementation["sha256"]) not in trusted_implementations:
        issues.append(
            f"{label} release verifier is not a hash-bound independent step implementation"
        )
    if path is not None and path.suffix != ".py":
        issues.append(f"{label} release verifier must be a Python source file")
    if issues or mode != "release" or path is None:
        return issues

    stable_paths = [path]
    for artifact in run["artifacts"]:
        try:
            stable_paths.append(
                _repository_path(
                    artifact["repository_path"],
                    label=f"{label} verifier stability artifact",
                )
            )
        except CoverageInputError as exc:
            issues.append(str(exc))
            return issues
    before = {candidate: _sha256(candidate) for candidate in stable_paths}
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OPENADA_OFFLINE_VERIFY": "1",
        "PATH": "/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(SRC),
    }
    try:
        completed = subprocess.run(
            [sys.executable, str(path), *verification["arguments"]],
            cwd=ROOT,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=verification["timeout_seconds"],
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        issues.append(f"{label} offline release verifier did not complete safely: {exc}")
        return issues
    output_size = len(completed.stdout) + len(completed.stderr)
    if output_size > 1024 * 1024:
        issues.append(f"{label} offline release verifier output exceeded 1 MiB")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        issues.append(
            f"{label} offline release verifier failed with exit "
            f"{completed.returncode}: {detail[-2000:]}"
        )
    for candidate, digest in before.items():
        try:
            observed = _sha256(candidate)
        except OSError as exc:
            issues.append(f"{label} offline verifier removed a bound file: {exc}")
            continue
        if observed != digest:
            issues.append(
                f"{label} offline verifier changed bound file "
                f"{candidate.relative_to(ROOT)}"
            )
    return issues


def _level_for_evidence(evidence: set[str]) -> str:
    level = "unverified"
    for candidate in LEVELS[1:]:
        if set(REQUIRED_EVIDENCE[candidate]).issubset(evidence):
            level = candidate
    return level


def _provider_claim_contract_cycle_issues(
    manifest: Mapping[str, Any],
    conformance_ids: Iterable[str],
    provider_manifest_paths: Mapping[str, str],
    *,
    label: str,
) -> list[str]:
    """Reject a receipt cycle through its own provider-manifest claim.

    Provider semantics are already bound by the semantic subject, which
    normalizes only ``conformance_records[*].evidence.sha256``. A chain that
    also file-hashes that same provider manifest cannot be published: writing
    the final run digest into the claim would stale the chain manifest and run
    that produced it.
    """

    contract_paths = {
        contract.get("repository_path")
        for contract in manifest.get("contracts", [])
        if isinstance(contract, Mapping)
    }
    return [
        f"{label} directly hashes provider manifest {path!r} for conformance "
        f"record {conformance_id!r}, creating a manifest/run/claim digest cycle"
        for conformance_id in conformance_ids
        if (path := provider_manifest_paths.get(conformance_id)) in contract_paths
    ]


def _load_coverage_records(
    index_path: Path,
    rows: list[dict[str, Any]],
    semantic_subject: str,
    issues: list[str],
    *,
    catalog_path: Path,
    mode: str,
) -> tuple[
    dict[str, list[tuple[str, str, set[str]]]],
    dict[str, dict[str, str]],
]:
    try:
        index = _load_json(index_path, label="semantic chain index")
    except CoverageInputError as exc:
        issues.append(str(exc))
        return {}, {}
    expected_keys = {"schema", "records", "extensions"}
    if set(index) != expected_keys:
        issues.append(
            "semantic chain index keys must be exactly schema, records, extensions"
        )
        return {}, {}
    if index.get("schema") != "openada.semantic-chain-index/v0alpha1":
        issues.append("semantic chain index has an unsupported schema")
    records = index.get("records")
    if not isinstance(records, list) or len(records) > 512:
        issues.append("semantic chain index records must be an array of at most 512 items")
        return {}, {}
    rows_by_id = {row["row_id"]: row for row in rows}
    known_rows = set(rows_by_id)
    known_conformance_ids = {
        conformance_id
        for row in rows
        if row["kind"] == "provider-conformance-claim"
        for conformance_id in row["conformance_record_ids"]
    }
    provider_manifest_paths = {
        conformance_id: row["provider_manifest_path"]
        for row in rows
        if row["kind"] == "provider-conformance-claim"
        and isinstance(row.get("provider_manifest_path"), str)
        for conformance_id in row["conformance_record_ids"]
    }
    evidence_by_row: dict[str, list[tuple[str, str, set[str]]]] = {}
    conformance_registry: dict[str, dict[str, str]] = {}
    record_ids: list[str] = []
    registered_conformance_ids: list[str] = []
    for position, record in enumerate(records):
        label = f"semantic chain index record {position}"
        record_issue_count = len(issues)
        if not isinstance(record, dict) or set(record) != {
            "id",
            "conformance_record_ids",
            "manifest",
            "run",
            "extensions",
        }:
            issues.append(f"{label} has invalid keys")
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id or len(record_id) > 256:
            issues.append(f"{label} has an invalid ID")
            continue
        record_ids.append(record_id)
        conformance_ids = record.get("conformance_record_ids")
        if (
            not isinstance(conformance_ids, list)
            or len(conformance_ids) > 256
            or any(
                not isinstance(item, str) or not item or len(item) > 256
                for item in conformance_ids
            )
        ):
            issues.append(f"{label} conformance_record_ids must be a bounded string array")
            continue
        for duplicate in _duplicates(conformance_ids):
            issues.append(f"{label} duplicates conformance record ID {duplicate}")
        for conformance_id in conformance_ids:
            registered_conformance_ids.append(conformance_id)
            if conformance_id not in known_conformance_ids:
                issues.append(
                    f"{label} registers an unknown provider conformance record: "
                    f"{conformance_id}"
                )
        if not isinstance(record.get("extensions"), dict):
            issues.append(f"{label} extensions must be an object")
        manifest_ref = record.get("manifest")
        run_ref = record.get("run")
        if not isinstance(manifest_ref, dict) or not isinstance(run_ref, dict):
            issues.append(f"{label} manifest and run must be file references")
            continue
        expected_ref_keys = {"repository_path", "sha256", "extensions"}
        if set(manifest_ref) != expected_ref_keys or set(run_ref) != expected_ref_keys:
            issues.append(f"{label} manifest and run file references have invalid keys")
            continue
        if not isinstance(manifest_ref["extensions"], dict) or not isinstance(
            run_ref["extensions"], dict
        ):
            issues.append(f"{label} manifest and run extensions must be objects")
            continue
        manifest_path = _validate_file_ref(manifest_ref, label=f"{label} manifest", issues=issues)
        run_path = _validate_file_ref(run_ref, label=f"{label} run", issues=issues)
        if manifest_path is None or run_path is None:
            continue
        try:
            manifest = _load_json(manifest_path, label=f"{label} manifest")
            run = _load_json(run_path, label=f"{label} run")
        except CoverageInputError as exc:
            issues.append(str(exc))
            continue
        manifest_schema_issues = _schema_issues(manifest, CHAIN_SCHEMA, label=f"{label} manifest")
        run_schema_issues = _schema_issues(run, RUN_SCHEMA, label=f"{label} run")
        issues.extend(manifest_schema_issues)
        issues.extend(run_schema_issues)
        if manifest_schema_issues or run_schema_issues:
            continue
        issues.extend(
            _provider_claim_contract_cycle_issues(
                manifest,
                conformance_ids,
                provider_manifest_paths,
                label=label,
            )
        )
        if run["chain_id"] != manifest["id"]:
            issues.append(f"{label} run chain ID does not match its manifest")
            continue
        if run["chain_manifest_sha256"] != _sha256(manifest_path):
            issues.append(f"{label} run does not bind its manifest bytes")
            continue
        if run["semantic_subject_sha256"] != semantic_subject:
            issues.append(f"{label} run is stale for the current semantic subject")
            continue
        issues.extend(
            _source_attestation_issues(
                run,
                semantic_subject,
                catalog_path,
                label=label,
                mode=mode,
            )
        )
        artifact_issues = _run_artifact_issues(run, manifest, label=label)
        issues.extend(artifact_issues)
        if artifact_issues:
            continue
        issues.extend(_design_provenance_issues(run, manifest, label=label))
        issues.extend(_agent_pointer_issues(run, manifest, label=label))
        for contract_position, contract in enumerate(manifest["contracts"]):
            _validate_file_ref(
                contract,
                label=f"{label} contract {contract_position}",
                issues=issues,
            )
        step_ids = [step["id"] for step in manifest["steps"]]
        for duplicate in _duplicates(step_ids):
            issues.append(f"{label} duplicates step ID {duplicate}")
        if manifest["agent_evidence"]["result_step"] not in set(step_ids):
            issues.append(f"{label} agent evidence references an unknown result step")
        result_steps = {
            step["id"]: step for step in manifest["steps"]
        }
        agent_result_step = result_steps.get(manifest["agent_evidence"]["result_step"])
        if agent_result_step is not None and agent_result_step["kind"] != "independent-decision":
            issues.append(
                f"{label} agent-visible result must come from an independent-decision step"
            )
        if not any(step["kind"] == "source-materialize" for step in manifest["steps"]):
            issues.append(f"{label} has no source-materialize step")
        if not any(
            step["kind"] == "semantic-command" and step["native_execution"]
            for step in manifest["steps"]
        ):
            issues.append(f"{label} has no native semantic-command step")
        if not any(step["kind"] == "independent-oracle" for step in manifest["steps"]):
            issues.append(f"{label} has no independent oracle step")

        available_roles: set[str] = set()
        native_roles: set[str] = set()
        oracle_consumes_native = False
        for step_position, step in enumerate(manifest["steps"]):
            step_label = f"{label} step {step_position} ({step['id']})"
            for reference_name in ("request", "implementation"):
                reference = step.get(reference_name)
                if reference is not None:
                    _validate_file_ref(
                        reference,
                        label=f"{step_label} {reference_name}",
                        issues=issues,
                    )
            missing_inputs = sorted(set(step["consumes"]) - available_roles)
            if missing_inputs:
                issues.append(
                    f"{step_label} consumes roles before they are produced: "
                    + ", ".join(missing_inputs)
                )
            duplicate_outputs = sorted(set(step["produces"]) & available_roles)
            if duplicate_outputs:
                issues.append(
                    f"{step_label} redefines evidence roles: "
                    + ", ".join(duplicate_outputs)
                )
            if step["kind"] == "semantic-command" and step["native_execution"]:
                native_roles.update(step["produces"])
            if step["kind"] == "independent-oracle":
                if set(step["consumes"]) & native_roles:
                    oracle_consumes_native = True
                implementation = step.get("implementation")
                if implementation is not None:
                    implementation_path: Path | None = None
                    try:
                        implementation_path = _repository_path(
                            implementation["repository_path"],
                            label=f"{step_label} implementation.repository_path",
                        )
                        implementation_path.relative_to((ROOT / "src" / "openada").resolve())
                    except ValueError:
                        pass
                    except CoverageInputError as exc:
                        issues.append(str(exc))
                    else:
                        issues.append(f"{step_label} oracle implementation is inside openada")
                    if (
                        implementation_path is not None
                        and implementation_path.suffix == ".py"
                        and implementation_path.is_file()
                    ):
                        try:
                            tree = ast.parse(
                                implementation_path.read_text(encoding="utf-8"),
                                filename=str(implementation_path),
                            )
                        except (OSError, UnicodeError, SyntaxError) as exc:
                            issues.append(f"{step_label} oracle cannot be inspected: {exc}")
                        else:
                            for node in ast.walk(tree):
                                names: list[str] = []
                                if isinstance(node, ast.Import):
                                    names = [alias.name for alias in node.names]
                                elif isinstance(node, ast.ImportFrom) and node.module:
                                    names = [node.module]
                                if any(name == "openada" or name.startswith("openada.") for name in names):
                                    issues.append(f"{step_label} oracle imports openada")
                                    break
            available_roles.update(step["produces"])
        if not oracle_consumes_native:
            issues.append(f"{label} independent oracle does not consume a native-step artifact")

        issues.extend(
            _positive_coverage_issues(manifest, rows_by_id, label=label)
        )

        declared_coverage = set(manifest["covers"])
        for replay_kind in ("negative_replays", "tamper_replays"):
            for replay in manifest[replay_kind]:
                for covered in replay["covers"]:
                    if covered not in declared_coverage:
                        issues.append(
                            f"{label} {replay_kind} {replay['id']} covers a row absent from manifest.covers: {covered}"
                        )
        if len(issues) == record_issue_count:
            issues.extend(
                _release_verification_issues(
                    run,
                    manifest,
                    label=label,
                    mode=mode,
                )
            )
        record_evidence: list[tuple[str, str, set[str]]] = []
        for covered in manifest["covers"]:
            if covered not in known_rows:
                issues.append(f"{label} covers an unknown semantic row: {covered}")
                continue
            evidence = _record_evidence(manifest, run, covered)
            level = _level_for_evidence(evidence)
            record_evidence.append((covered, level, evidence))
        if len(issues) != record_issue_count:
            continue
        for covered, level, evidence in record_evidence:
            evidence_by_row.setdefault(covered, []).append((record_id, level, evidence))
        for conformance_id in conformance_ids:
            conformance_registry[conformance_id] = {
                "chain_record_id": record_id,
                "chain_id": manifest["id"],
                "evidence_sha256": run_ref["sha256"],
            }
    for duplicate in _duplicates(record_ids):
        issues.append(f"semantic chain record ID is duplicated: {duplicate}")
    for duplicate in _duplicates(registered_conformance_ids):
        issues.append(f"provider conformance record is registered more than once: {duplicate}")
    return evidence_by_row, conformance_registry


def _apply_coverage(
    rows: list[dict[str, Any]],
    records: dict[str, list[tuple[str, str, set[str]]]],
) -> None:
    for row in rows:
        candidates = records.get(row["row_id"], [])
        if candidates:
            best_rank = max(LEVEL_RANK[level] for _, level, _ in candidates)
            best = [item for item in candidates if LEVEL_RANK[item[1]] == best_rank]
            row["coverage_level"] = LEVELS[best_rank]
            row["coverage_record_ids"] = sorted(item[0] for item in best)
            evidence: set[str] = set()
            for _, _, item_evidence in best:
                evidence.update(item_evidence)
        else:
            evidence = set()
        required = row["required_coverage_level"]
        if required is None:
            row["missing_evidence"] = []
            row["gap"] = False
            continue
        row["missing_evidence"] = [
            item for item in REQUIRED_EVIDENCE[required] if item not in evidence
        ]
        row["gap"] = LEVEL_RANK[row["coverage_level"]] < LEVEL_RANK[required]


def _resolve_provider_conformance_claims(
    rows: list[dict[str, Any]],
    registry: dict[str, dict[str, str]],
) -> None:
    """Require provider claims to bind the bytes of a validated chain run.

    A provider manifest's maturity is descriptive metadata.  The claim becomes
    trustworthy only when its declared digest equals the run-file digest in a
    validated semantic-chain index record.  Resolution never promotes the
    row's coverage level; the chain must also explicitly cover that row.
    """

    for row in rows:
        if row["kind"] != "provider-conformance-claim":
            continue
        conformance_id = row["conformance_record_ids"][0]
        claimed_digest = row["conformance_claim_digest"]
        registered = registry.get(conformance_id)
        placeholder_digest = claimed_digest == "0" * 64
        if placeholder_digest:
            status = "placeholder-digest"
        elif registered is None:
            status = "unresolved"
        elif claimed_digest != registered["evidence_sha256"]:
            status = "digest-mismatch"
        else:
            status = "resolved"
        if registered is None:
            resolution = {
                "status": status,
                "conformance_record_id": conformance_id,
                "claimed_evidence_sha256": claimed_digest,
                "claimed_evidence_uri": row["conformance_claim_uri"],
                "registered_chain_record_id": None,
                "registered_chain_id": None,
                "registered_evidence_sha256": None,
            }
        else:
            resolution = {
                "status": status,
                "conformance_record_id": conformance_id,
                "claimed_evidence_sha256": claimed_digest,
                "claimed_evidence_uri": row["conformance_claim_uri"],
                "registered_chain_record_id": registered["chain_record_id"],
                "registered_chain_id": registered["chain_id"],
                "registered_evidence_sha256": registered["evidence_sha256"],
            }
        row["conformance_resolution"] = resolution
        if resolution["status"] != "resolved" and row["required_coverage_level"] is not None:
            if (
                placeholder_digest
                and "non-placeholder-conformance-digest" not in row["missing_evidence"]
            ):
                row["missing_evidence"].append("non-placeholder-conformance-digest")
            if (
                registered is None or claimed_digest != registered["evidence_sha256"]
            ) and "registered-conformance-digest" not in row["missing_evidence"]:
                row["missing_evidence"].append("registered-conformance-digest")
            row["gap"] = True


def build_report(catalog_path: Path, index_path: Path, *, mode: str) -> dict[str, Any]:
    issues: list[str] = []
    try:
        catalog = _load_json(catalog_path, label="semantic surface catalog")
    except CoverageInputError as exc:
        return {
            "schema": "openada.semantic-coverage-report/v0alpha1",
            "mode": mode,
            "status": "invalid",
            "catalog": {"path": str(catalog_path), "sha256": None},
            "chain_index": {"path": str(index_path), "sha256": None},
            "semantic_subject_sha256": None,
            "inventory": {},
            "summary": {"row_count": 0, "active_row_count": 0, "gap_count": 0},
            "rows": [],
            "gaps": [],
            "issues": [str(exc)],
        }
    issues.extend(_schema_issues(catalog, CATALOG_SCHEMA, label="semantic surface catalog"))
    profiles = _profile_inventory(catalog, issues)
    surfaces = _surface_inventory(catalog, profiles, issues)
    _preflight_inventory(catalog, surfaces, issues)
    providers = _provider_inventory(catalog, profiles, issues)
    repository_providers = _repository_provider_inventory(catalog, profiles, issues)
    _validate_native_mapping_providers(
        profiles,
        providers,
        repository_providers,
        issues,
    )
    rows = _coverage_rows(
        catalog,
        profiles,
        surfaces,
        providers,
        repository_providers,
    )
    row_ids = [row["row_id"] for row in rows]
    for duplicate in _duplicates(row_ids):
        issues.append(f"derived semantic coverage row is duplicated: {duplicate}")
    semantic_subject = _semantic_subject(catalog_path)
    coverage_records, conformance_registry = _load_coverage_records(
        index_path,
        rows,
        semantic_subject,
        issues,
        catalog_path=catalog_path,
        mode=mode,
    )
    _apply_coverage(rows, coverage_records)
    _resolve_provider_conformance_claims(rows, conformance_registry)
    gaps = [row["row_id"] for row in rows if row["gap"]]
    active_rows = [row for row in rows if row["required_coverage_level"] is not None]
    if mode == "release" and not active_rows:
        issues.append("release policy derived zero active semantic obligations")
    status = "invalid" if issues else ("gaps" if gaps else "pass")
    kind_counts = Counter(row["kind"] for row in rows)
    level_counts = Counter(row["coverage_level"] for row in rows)
    return {
        "schema": "openada.semantic-coverage-report/v0alpha1",
        "mode": mode,
        "status": status,
        "catalog": {"path": str(catalog_path), "sha256": _sha256(catalog_path)},
        "chain_index": {
            "path": str(index_path),
            "sha256": _sha256(index_path) if index_path.is_file() else None,
        },
        "semantic_subject_sha256": semantic_subject,
        "inventory": {
            "cli_leaf_count": len(_parser_leaves()),
            "surface_count": len(surfaces),
            "profile_count": len(profiles),
            "active_profile_count": sum(
                profile["lifecycle"] == "active" for profile in profiles.values()
            ),
            "profile_feature_count": sum(len(profile["features"]) for profile in profiles.values()),
            "provider_mapping_count": len(providers)
            + sum(len(provider["capabilities"]) for provider in repository_providers),
            "builtin_provider_mapping_count": len(providers),
            "shipped_provider_manifest_count": len(repository_providers),
            "shipped_provider_capability_count": sum(
                len(provider["capabilities"]) for provider in repository_providers
            ),
            "preflight_assertion_count": len(catalog.get("preflight_assertions", [])),
        },
        "summary": {
            "row_count": len(rows),
            "active_row_count": len(active_rows),
            "gap_count": len(gaps),
            "rows_by_kind": dict(sorted(kind_counts.items())),
            "rows_by_coverage_level": dict(sorted(level_counts.items())),
        },
        "rows": rows,
        "gaps": gaps,
        "issues": sorted(set(issues)),
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit and optionally enforce OpenADA semantic coverage gaps."
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="Semantic surface catalog (default: repository catalog).",
    )
    parser.add_argument(
        "--chain-index",
        type=Path,
        default=DEFAULT_CHAIN_INDEX,
        help="Content-addressed semantic-chain record index.",
    )
    parser.add_argument(
        "--mode",
        choices=("audit", "agent-ready", "release"),
        default="audit",
        help="audit reports gaps; agent-ready and release enforce all active rows.",
    )
    parser.add_argument(
        "--fail-on-gaps",
        action="store_true",
        help="Make audit mode exit 1 when any active row is incomplete.",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(args.catalog.resolve(), args.chain_index.resolve(), mode=args.mode)
    if args.compact:
        encoded = json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True)
    else:
        encoded = json.dumps(report, allow_nan=False, indent=2, sort_keys=True)
    sys.stdout.write(encoded + "\n")
    if report["status"] == "invalid":
        return 2
    if report["gaps"] and (args.fail_on_gaps or args.mode in {"agent-ready", "release"}):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
