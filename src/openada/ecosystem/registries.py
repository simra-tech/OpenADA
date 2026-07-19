"""Explicit immutable registries for validators, mappings, and capabilities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from .canonical import canonical_json_bytes
from .contracts import SchemaCatalog


class RegistryError(ValueError):
    """A registry entry is invalid, conflicting, missing, or mutated."""


IssueCheck = Callable[[Mapping[str, Any]], Sequence[str]]
CrossCheck = Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[str]]


class OperationValidator(Protocol):
    def validate_request(self, request: Mapping[str, Any]) -> Sequence[str]: ...

    def validate_semantics(self, request: Mapping[str, Any]) -> Sequence[str]: ...

    def validate_result(self, result: Mapping[str, Any]) -> Sequence[str]: ...

    def validate_evidence(self, result: Mapping[str, Any]) -> Sequence[str]: ...

    def validate_cross_artifacts(
        self, request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class ValidatorKey:
    profile_identity: str
    profile_revision: str
    profile_sha256: str
    validator_identity: str
    validator_revision: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    request_issues: tuple[str, ...]
    semantic_issues: tuple[str, ...]
    result_issues: tuple[str, ...]
    evidence_issues: tuple[str, ...]
    cross_artifact_issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not any(
            (
                self.request_issues,
                self.semantic_issues,
                self.result_issues,
                self.evidence_issues,
                self.cross_artifact_issues,
            )
        )


def _issues(call: Callable[..., Sequence[str]], *arguments: Any) -> tuple[str, ...]:
    values = call(*arguments)
    if isinstance(values, (str, bytes)):
        raise RegistryError("validator checks must return a sequence of issue strings")
    bounded = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4_000:
            raise RegistryError("validator returned a malformed issue")
        bounded.append(value)
        if len(bounded) > 256:
            raise RegistryError("validator returned more than 256 issues")
    return tuple(bounded)


class OperationValidatorRegistry:
    """Register executable validators only through a host-trusted direct call."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._validators: dict[ValidatorKey, OperationValidator] = {}

    def register(self, key: ValidatorKey, validator: OperationValidator) -> None:
        required = (
            "validate_request",
            "validate_semantics",
            "validate_result",
            "validate_evidence",
            "validate_cross_artifacts",
        )
        if not all(callable(getattr(validator, name, None)) for name in required):
            raise RegistryError("validator does not implement every independent check")
        if len(key.profile_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in key.profile_sha256
        ):
            raise RegistryError("validator profile digest is not lowercase SHA-256")
        with self._lock:
            previous = self._validators.get(key)
            if previous is not None and previous is not validator:
                raise RegistryError(f"validator key is already registered: {key}")
            self._validators[key] = validator

    def has(self, identity: str, revision: str) -> bool:
        with self._lock:
            return any(
                key.validator_identity == identity
                and key.validator_revision == revision
                for key in self._validators
            )

    def keys(self) -> tuple[ValidatorKey, ...]:
        with self._lock:
            return tuple(sorted(self._validators, key=lambda item: repr(item)))

    def resolve(self, key: ValidatorKey) -> OperationValidator:
        with self._lock:
            validator = self._validators.get(key)
        if validator is None:
            raise RegistryError(f"no exact operation validator is registered for {key}")
        return validator

    def validate(
        self,
        key: ValidatorKey,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> ValidationReport:
        validator = self.resolve(key)
        return ValidationReport(
            request_issues=_issues(validator.validate_request, request),
            semantic_issues=_issues(validator.validate_semantics, request),
            result_issues=_issues(validator.validate_result, result),
            evidence_issues=_issues(validator.validate_evidence, result),
            cross_artifact_issues=_issues(
                validator.validate_cross_artifacts, request, result
            ),
        )


def _document_digest(document: Mapping[str, Any]) -> tuple[str, bytes]:
    encoded = canonical_json_bytes(document)
    return hashlib.sha256(encoded).hexdigest(), encoded


class DriverMappingRegistry:
    """Immutable exact-digest registry for normalized driver mappings."""

    def __init__(self, schemas: SchemaCatalog | None = None) -> None:
        self._schemas = schemas or SchemaCatalog()
        self._lock = RLock()
        self._entries: dict[tuple[str, str], tuple[str, bytes, dict[str, Any]]] = {}

    def register(self, mapping: Mapping[str, Any]) -> str:
        self._schemas.validate(mapping)
        if mapping.get("schema") != "openada.driver-mapping/v0alpha1":
            raise RegistryError("mapping has an unsupported schema revision")
        key = (str(mapping["id"]), str(mapping["revision"]))
        seen_steps: set[str] = set()
        for step in mapping["steps"]:
            identity = step["id"]
            if identity in seen_steps:
                raise RegistryError(f"mapping repeats step identity: {identity}")
            missing = set(step["depends_on"]) - seen_steps
            if missing:
                raise RegistryError(
                    f"mapping step has missing or forward dependencies: {sorted(missing)}"
                )
            seen_steps.add(identity)
        digest, encoded = _document_digest(mapping)
        document = deepcopy(dict(mapping))
        with self._lock:
            previous = self._entries.get(key)
            if previous is not None and previous[:2] != (digest, encoded):
                raise RegistryError(f"mapping identity has conflicting content: {key}")
            self._entries[key] = (digest, encoded, document)
        return digest

    def resolve(self, identity: str, revision: str, sha256: str) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get((identity, revision))
        if entry is None:
            raise RegistryError(f"mapping is not registered: {(identity, revision)}")
        if entry[0] != sha256:
            raise RegistryError("mapping digest does not match the registered content")
        return deepcopy(entry[2])

    def records(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            return tuple(
                (identity, revision, entry[0])
                for (identity, revision), entry in sorted(self._entries.items())
            )


class CapabilityRegistry:
    """Validate capability manifests without claiming runtime readiness."""

    def __init__(self, schemas: SchemaCatalog | None = None) -> None:
        self._schemas = schemas or SchemaCatalog()
        self._lock = RLock()
        self._capabilities: dict[str, tuple[str, dict[str, Any], str]] = {}

    def register_manifest(self, manifest: Mapping[str, Any]) -> str:
        self._schemas.validate(manifest)
        if manifest.get("schema") != "openada.capability-manifest/v0alpha1":
            raise RegistryError("capability manifest has an unsupported schema")
        manifest_digest, _ = _document_digest(manifest)
        provider_id = str(manifest["provider_id"])
        seen: set[str] = set()
        with self._lock:
            for capability in manifest["capabilities"]:
                identity = str(capability["id"])
                if identity in seen:
                    raise RegistryError(f"capability is duplicated in manifest: {identity}")
                seen.add(identity)
                feature_ids = [feature["id"] for feature in capability["features"]]
                if len(feature_ids) != len(set(feature_ids)):
                    raise RegistryError(
                        f"capability repeats a feature identity: {identity}"
                    )
                overlap = set(feature_ids) & set(capability["non_advertised_features"])
                if overlap:
                    raise RegistryError(
                        f"capability both advertises and withholds features: {sorted(overlap)}"
                    )
                previous = self._capabilities.get(identity)
                document = deepcopy(dict(capability))
                candidate = (provider_id, document, manifest_digest)
                if previous is not None and previous != candidate:
                    raise RegistryError(
                        f"capability identity has conflicting content: {identity}"
                    )
                self._capabilities[identity] = candidate
        return manifest_digest

    def resolve(
        self,
        capability_id: str,
        *,
        profile_sha256: str,
        mapping_sha256: str,
        required_features: Sequence[str] = (),
        transport_revision: str,
        result_revision: str,
        observed_backend_identity: str | None = None,
        observed_backend_version: str | None = None,
        conformance_receipt_sha256: Sequence[str] = (),
    ) -> dict[str, Any]:
        with self._lock:
            entry = self._capabilities.get(capability_id)
        if entry is None:
            raise RegistryError(f"capability is not registered: {capability_id}")
        capability = entry[1]
        if capability["profile_sha256"] != profile_sha256:
            raise RegistryError("capability profile digest mismatch")
        if capability["mapping_sha256"] != mapping_sha256:
            raise RegistryError("capability mapping digest mismatch")
        maturity = {feature["id"] for feature in capability["features"]}
        missing = sorted(set(required_features) - maturity)
        if missing:
            raise RegistryError(f"capability does not advertise features: {missing}")
        if transport_revision not in capability["transport_revisions"]:
            raise RegistryError("capability does not support the selected transport")
        if result_revision not in capability["result_revisions"]:
            raise RegistryError("capability does not support the selected result revision")
        if capability["runtime_probe_policy"] == "required" and (
            not observed_backend_identity or not observed_backend_version
        ):
            raise RegistryError("capability requires an observed backend identity and version")
        if capability["tested_versions"] and observed_backend_version not in capability["tested_versions"]:
            raise RegistryError("observed backend version is outside the tested capability set")
        required_receipts = {
            receipt["sha256"] for receipt in capability["conformance_receipts"]
        }
        if not required_receipts.issubset(set(conformance_receipt_sha256)):
            raise RegistryError("required immutable conformance receipts are missing")
        return deepcopy(capability)

    def records(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            return tuple(
                (identity, value[0], value[2])
                for identity, value in sorted(self._capabilities.items())
            )
