from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from openada.ecosystem.bundles import BundleError, ProviderBundleRegistry
from openada.ecosystem.canonical import (
    CanonicalJSONError,
    RequestBindingError,
    RequestIdentityRegistry,
    bind_request,
    canonical_json_bytes,
    canonical_request_bytes,
)
from openada.ecosystem.contexts import ContextResolutionError, HostContextResolver
from openada.ecosystem.contracts import SchemaCatalog
from openada.ecosystem.fakes import FakeOperationValidator
from openada.ecosystem.registries import (
    CapabilityRegistry,
    DriverMappingRegistry,
    OperationValidatorRegistry,
    RegistryError,
    ValidatorKey,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
PROFILE_ID = "openada.operation/artifact.transform/v1alpha1"


def request_document() -> dict:
    return {
        "schema": "openada.request/v0alpha2",
        "request_id": str(uuid.UUID(int=1)),
        "canonical": {
            "algorithm": "openada.canonical-json/v1",
            "sha256": "0" * 64,
            "extensions": {},
        },
        "profile": {"identity": PROFILE_ID, "revision": "v1alpha1", "sha256": DIGEST_A, "extensions": {}},
        "mapping": {"identity": "org.example.mapping.fake", "revision": "v1alpha1", "sha256": DIGEST_B, "extensions": {}},
        "capability_id": "org.example.capability.fake",
        "inputs": [],
        "result_revision": "openada.result/v0alpha2",
        "transport_policy": {"allowed": ["fake"], "timeout_ms": 1000, "extensions": {}},
        "context_name": None,
        "parameters": {"mode": "identity", "samples": [1, 2, 3]},
        "extensions": {},
    }


def mapping_document() -> dict:
    return {
        "schema": "openada.driver-mapping/v0alpha1",
        "id": "org.example.mapping.fake",
        "revision": "v1alpha1",
        "profile": {"identity": PROFILE_ID, "revision": "v1alpha1", "sha256": DIGEST_A, "extensions": {}},
        "provider_id": "org.example.provider.fake",
        "driver_id": "org.example.driver.fake",
        "native_family": "fixture",
        "capability_id": "org.example.capability.fake",
        "features": ["identity-transform"],
        "locator_kinds": ["artifact-reference"],
        "transport_kinds": ["fake"],
        "steps": [{"id": "transform", "role": "transform", "depends_on": [], "action_id": "transform", "input_roles": ["input"], "output_roles": ["output"], "extensions": {}}],
        "artifact_roles": ["input", "output"],
        "compatible_contract_revisions": ["openada.request/v0alpha2", "openada.result/v0alpha2"],
        "version_policy": {"kind": "exact", "constraint": "v1alpha1", "extensions": {}},
        "conformance_requirements": ["org.example.conformance.fake"],
        "extensions": {},
    }


def test_canonical_vectors_and_request_reuse_fail_closed() -> None:
    assert canonical_json_bytes({"z": [True, None, "µ"], "a": -2}) == b'{"a":-2,"z":[true,null,"\xc2\xb5"]}'
    with pytest.raises(CanonicalJSONError, match="floating point"):
        canonical_json_bytes({"value": 0.1})
    request = bind_request(request_document())
    SchemaCatalog().validate(request)
    expected = hashlib.sha256(canonical_request_bytes(request)).hexdigest()
    assert request["canonical"]["sha256"] == expected
    registry = RequestIdentityRegistry()
    assert registry.register(request) == expected
    assert registry.register(deepcopy(request)) == expected
    changed = deepcopy(request)
    changed["parameters"]["mode"] = "reverse"
    changed = bind_request(changed)
    with pytest.raises(RequestBindingError, match="already registered"):
        registry.register(changed)
    injected = request_document()
    injected["parameters"]["environment"] = {"EXAMPLE": "value"}
    with pytest.raises(RequestBindingError, match="host context"):
        bind_request(injected)


def test_every_additive_schema_is_discoverable_and_readiness_is_not_a_result() -> None:
    catalog = SchemaCatalog()
    expected = {
        "openada.provider-bundle/v0alpha1",
        "openada.driver-mapping/v0alpha1",
        "openada.capability-manifest/v0alpha1",
        "openada.request/v0alpha2",
        "openada.readiness/v0alpha1",
        "openada.invocation-context/v0alpha1",
        "openada.result/v0alpha2",
        "openada.locator/v0alpha1",
        "openada.session-receipt/v0alpha1",
        "openada.job-receipt/v0alpha1",
        "openada.conformance-receipt/v0alpha1",
    }
    discovered = {
        document["properties"]["schema"]["const"]
        for document in catalog.documents()
        if "schema" in document.get("properties", {})
        and "const" in document["properties"]["schema"]
    }
    assert expected <= discovered
    check = lambda state, identity=None, reason=None: {
        "state": state,
        "observed_identity": identity,
        "reason": reason,
        "extensions": {},
    }
    readiness = {
        "schema": "openada.readiness/v0alpha1",
        "request_sha256": DIGEST_A,
        "driver": check("ready", "org.example.driver.fake"),
        "binary_version": check("unknown", reason="No external runtime was probed."),
        "site_setup": check("not-applicable"),
        "authorization": check("not-applicable"),
        "session_endpoint": check("not-applicable"),
        "target": check("ready", DIGEST_B),
        "artifacts": check("unknown", reason="Execution has not started."),
        "extensions": {},
    }
    catalog.validate(readiness)
    assert "engineering_conclusion" not in readiness


def test_mapping_capability_validator_and_context_registries() -> None:
    mappings = DriverMappingRegistry()
    mapping = mapping_document()
    mapping_sha256 = mappings.register(mapping)
    assert mappings.resolve(mapping["id"], "v1alpha1", mapping_sha256) == mapping
    with pytest.raises(RegistryError, match="digest"):
        mappings.resolve(mapping["id"], "v1alpha1", DIGEST_A)

    capabilities = CapabilityRegistry()
    manifest = {
        "schema": "openada.capability-manifest/v0alpha1",
        "provider_id": "org.example.provider.fake",
        "manifest_revision": "v1alpha1",
        "capabilities": [{
            "id": "org.example.capability.fake",
            "profile_sha256": DIGEST_A,
            "mapping_sha256": mapping_sha256,
            "features": [{"id": "identity-transform", "maturity": "experimental", "extensions": {}}],
            "locator_revisions": ["openada.locator/v0alpha1"],
            "transport_revisions": ["org.openada.transport.fake/v1alpha1"],
            "result_revisions": ["openada.result/v0alpha2"],
            "tested_versions": [],
            "runtime_probe_policy": "forbidden",
            "conformance_receipts": [],
            "limitations": ["Public deterministic fixture only."],
            "non_advertised_features": ["external-runtime-availability"],
            "extensions": {},
        }],
        "extensions": {},
    }
    capabilities.register_manifest(manifest)
    resolved = capabilities.resolve(
        "org.example.capability.fake",
        profile_sha256=DIGEST_A,
        mapping_sha256=mapping_sha256,
        required_features=["identity-transform"],
        transport_revision="org.openada.transport.fake/v1alpha1",
        result_revision="openada.result/v0alpha2",
    )
    assert resolved["runtime_probe_policy"] == "forbidden"

    validators = OperationValidatorRegistry()
    key = ValidatorKey(PROFILE_ID, "v1alpha1", DIGEST_A, "org.example.validator.fake", "v1alpha1")
    validators.register(key, FakeOperationValidator(PROFILE_ID))
    result = {"operation": "artifact.transform", "output_sha256": DIGEST_B}
    assert validators.validate(key, {"operation": PROFILE_ID, "parameters": {}}, result).ok

    contexts = HostContextResolver()
    context = {
        "schema": "openada.invocation-context/v0alpha1",
        "context_name": "fixture",
        "provider_id": "org.example.provider.fake",
        "filtered_environment": {"EXAMPLE_MODE": "bounded"},
        "secret_handles": ["fixture-token"],
        "setup_identity": DIGEST_A,
        "authorization_identity": DIGEST_B,
        "extensions": {},
    }
    contexts.register(context)
    assert contexts.resolve("fixture", "org.example.provider.fake") == context
    with pytest.raises(ContextResolutionError, match="this provider"):
        contexts.resolve("fixture", "org.example.provider.other")


def test_validator_registry_dispatches_two_unrelated_fake_profiles() -> None:
    registry = OperationValidatorRegistry()
    alpha = ValidatorKey(
        "org.example.operation/alpha/v1", "v1", DIGEST_A,
        "org.example.validator.alpha", "v1",
    )
    beta = ValidatorKey(
        "org.example.operation/beta/v1", "v1", DIGEST_B,
        "org.example.validator.beta", "v1",
    )
    registry.register(alpha, FakeOperationValidator(alpha.profile_identity))
    registry.register(beta, FakeOperationValidator(beta.profile_identity))
    assert registry.validate(
        alpha,
        {"operation": alpha.profile_identity, "parameters": {}},
        {"operation": "alpha", "artifact_sha256": DIGEST_A},
    ).ok
    assert registry.validate(
        beta,
        {"operation": beta.profile_identity, "parameters": {}},
        {"operation": "beta", "artifact_sha256": DIGEST_B},
    ).ok
    with pytest.raises(RegistryError, match="no exact"):
        registry.resolve(ValidatorKey(
            alpha.profile_identity, "v1", DIGEST_B,
            alpha.validator_identity, "v1",
        ))


def test_provider_bundle_is_explicit_digest_bound_and_immutable(tmp_path: Path) -> None:
    validators = OperationValidatorRegistry()
    key = ValidatorKey(PROFILE_ID, "v1alpha1", DIGEST_A, "org.example.validator.fake", "v1alpha1")
    validators.register(key, FakeOperationValidator(PROFILE_ID))
    profile = {
        "operation": {"id": PROFILE_ID},
        "assertion": {"id": "openada.assertion/artifact.transform.valid/v1alpha1"},
    }
    profile_bytes = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    profile_path = tmp_path / "profile.json"
    profile_path.write_bytes(profile_bytes)
    profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    resource = {"path": "profile.json", "sha256": profile_sha256, "extensions": {}}
    manifest = {
        "schema": "openada.provider-bundle/v0alpha1",
        "bundle": {"id": "org.example.bundle.fake", "version": "v1alpha1", "extensions": {}},
        "contract_range": {"minimum": "v1alpha1", "maximum_exclusive": "v2", "extensions": {}},
        "source_distribution": {"name": "example-provider", "version": "1.0", "extensions": {}},
        "profiles": [{**resource, "identity": PROFILE_ID, "revision": "v1alpha1"}],
        "assertions": [{**resource, "identity": "openada.assertion/artifact.transform.valid/v1alpha1", "revision": "v1alpha1"}],
        "validators": [{"identity": "org.example.validator.fake", "revision": "v1alpha1", "extensions": {}}],
        "extensions": {},
    }
    manifest_path = tmp_path / "bundle.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    registry = ProviderBundleRegistry([tmp_path], validators)
    loaded = registry.load(manifest_path)
    assert loaded.identity == "org.example.bundle.fake"
    registry.verify_unchanged(loaded.identity, loaded.version)
    profile_path.write_text("{}")
    with pytest.raises(BundleError, match="changed after registration"):
        registry.verify_unchanged(loaded.identity, loaded.version)

    profile_path.write_bytes(profile_bytes)
    incompatible = ProviderBundleRegistry(
        [tmp_path], validators, supported_contract_version="v2"
    )
    with pytest.raises(BundleError, match="incompatible"):
        incompatible.load(manifest_path)


def test_provider_bundle_rejects_untrusted_validator_and_symlink(tmp_path: Path) -> None:
    validators = OperationValidatorRegistry()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    registry = ProviderBundleRegistry([tmp_path], validators)
    with pytest.raises(BundleError, match="securely open"):
        registry.load(link)
