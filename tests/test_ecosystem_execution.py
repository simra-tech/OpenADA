from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from openada.ecosystem.canonical import canonical_json_bytes
from openada.ecosystem.conformance import ConformanceSuite, fake_backend_cases
from openada.ecosystem.fakes import FakeProviderBackend
from openada.ecosystem.locators import LocatorError, LocatorResolver
from openada.ecosystem.results import ResultSemanticError, validate_result_semantics
from openada.ecosystem.transports import (
    AgentSessionTransport,
    DeterministicFakeScheduler,
    TransportError,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def dimensions(*, execution="completed", engineering="pass") -> dict:
    return {
        "dependency_readiness": "ready",
        "execution_state": execution,
        "artifact_readiness": "ready",
        "engineering_conclusion": engineering,
        "workflow_review": "unreviewed",
        "signoff_approval": "not-requested",
        "extensions": {},
    }


def locator(kind: str, value: str, identity_kind: str, identity_value: str, *, root_id=None, intent="read", mutation=None) -> dict:
    return {
        "schema": "openada.locator/v0alpha1",
        "kind": kind,
        "intent": intent,
        "root_id": root_id,
        "value": value,
        "identity": {"kind": identity_kind, "value": identity_value, "extensions": {}},
        "mutation": mutation,
        "extensions": {},
    }


def test_locator_regular_file_containment_and_revisioned_native_mutation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    content = b"org.example fixture\n"
    (root / "input.txt").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    resolver = LocatorResolver({"fixture": root})
    resolved = resolver.resolve(locator("regular-file", "input.txt", "sha256", digest, root_id="fixture"))
    assert resolved.contained and resolved.metadata["size"] == len(content)
    with pytest.raises(LocatorError, match="identity mismatch"):
        resolver.resolve(locator("regular-file", "input.txt", "sha256", DIGEST_A, root_id="fixture"))

    resolver.native_objects.create("example-object", "revision-1", {"value": 1})
    mutation = {"precondition": "revision-1", "postcondition": "revision-2", "extensions": {}}
    native = locator(
        "native-object", "example-object", "native-revision", "revision-1",
        intent="mutate", mutation=mutation,
    )
    assert resolver.mutate_native(native, lambda value: {"value": value["value"] + 1}) == {"value": 2}
    assert resolver.verify_postcondition(native).identity_value == "revision-2"
    with pytest.raises(LocatorError, match="precondition"):
        resolver.mutate_native(native, lambda value: value)


def test_directory_snapshot_rejects_links_and_verifies_workspace_postcondition(tmp_path: Path) -> None:
    root = tmp_path / "root"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    first = workspace / "a.txt"
    first.write_bytes(b"one")

    def snapshot_digest(payload: bytes) -> str:
        entries = [{
            "path": "a.txt",
            "kind": "regular-file",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": stat.S_IMODE(first.stat().st_mode),
        }]
        return hashlib.sha256(canonical_json_bytes(entries)).hexdigest()

    before = snapshot_digest(b"one")
    after = snapshot_digest(b"two")
    mutation = {"precondition": before, "postcondition": after, "extensions": {}}
    resolver = LocatorResolver({"fixture": root})
    workspace_locator = locator(
        "workspace", "workspace", "snapshot", before,
        root_id="fixture", intent="mutate", mutation=mutation,
    )
    assert resolver.resolve(workspace_locator).identity_value == before
    first.write_bytes(b"two")
    assert resolver.verify_postcondition(workspace_locator).identity_value == after
    (workspace / "link").symlink_to(first)
    with pytest.raises(LocatorError, match="symbolic link"):
        resolver.resolve(locator("directory-tree", "workspace", "snapshot", after, root_id="fixture"))


def test_locator_artifact_session_and_approved_uri_are_closed() -> None:
    resolver = LocatorResolver(approved_uri_origins=["https://fixtures.example.org"])
    resolver.register_artifact("artifact-1", DIGEST_A, {"value": 1})
    resolver.register_session("session-1", "revision-1", {"state": "ready"})
    assert resolver.resolve(locator("artifact-reference", "artifact-1", "sha256", DIGEST_A)).contained
    assert resolver.resolve(locator("session", "session-1", "opaque", "revision-1")).contained
    approved = resolver.resolve(
        locator("approved-uri", "https://fixtures.example.org/data", "opaque", "fixture-uri")
    )
    assert approved.metadata["origin"] == "https://fixtures.example.org"
    with pytest.raises(LocatorError, match="not approved"):
        resolver.resolve(locator("approved-uri", "https://other.example.org/data", "opaque", "fixture-uri"))


def test_agent_session_ownership_sequence_replay_and_cleanup() -> None:
    transport = AgentSessionTransport()
    handle = transport.start("fixture-owner", "org.example.backend/v1", lambda request: {"value": request["value"] + 1})
    first = transport.invoke(
        handle.session_id,
        "fixture-owner",
        handle.ownership_token,
        sequence=1,
        idempotency_key="fixture-invocation-1",
        request={"value": 2},
    )
    assert first == {"value": 3}
    replay = transport.invoke(
        handle.session_id,
        "fixture-owner",
        handle.ownership_token,
        sequence=1,
        idempotency_key="fixture-invocation-1",
        request={"value": 2},
    )
    assert replay == first
    assert transport.collect(
        handle.session_id,
        "fixture-owner",
        handle.ownership_token,
        "fixture-invocation-1",
    ) == first
    assert transport.resolve(
        handle.session_id, "fixture-owner", handle.ownership_token
    )["state"] == "ready"
    with pytest.raises(TransportError, match="different work"):
        transport.invoke(
            handle.session_id,
            "fixture-owner",
            handle.ownership_token,
            sequence=1,
            idempotency_key="fixture-invocation-1",
            request={"value": 9},
        )
    with pytest.raises(TransportError, match="ownership"):
        transport.close(handle.session_id, "other-owner", handle.ownership_token)
    assert transport.heartbeat(handle.session_id, "fixture-owner", handle.ownership_token, 1)["last_heartbeat_sequence"] == 1
    closed = transport.close(handle.session_id, "fixture-owner", handle.ownership_token)
    assert closed["state"] == "closed" and closed["cleanup"] == "complete"
    assert handle.ownership_token not in json.dumps(closed)
    assert "ownership_token_sha256" in closed


def test_remote_job_idempotency_restart_cancel_and_collection() -> None:
    scheduler = DeterministicFakeScheduler(lambda payload: {"value": payload["value"] * 2})
    submitted = scheduler.submit({"value": 4}, ingress_sha256=DIGEST_A)
    repeated = scheduler.submit({"value": 4}, ingress_sha256=DIGEST_A)
    assert repeated["job_id"] == submitted["job_id"]
    scheduler.advance(submitted["job_id"])
    scheduler.mark_orphaned(submitted["job_id"])
    scheduler = DeterministicFakeScheduler.restore(
        lambda payload: {"value": payload["value"] * 2}, scheduler.export_state()
    )
    reconnected = scheduler.reconnect(submitted["job_id"], submitted["idempotency_key"])
    assert reconnected["state"] == "running"
    completed = scheduler.advance(submitted["job_id"])
    assert completed["state"] == "completed"
    result, receipt = scheduler.collect(submitted["job_id"])
    assert result == {"value": 8}
    assert receipt["artifacts"]["egress_sha256"] == hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    assert scheduler.cleanup(submitted["job_id"])["cleanup"] == "complete"

    cancelled = scheduler.submit({"value": 5}, ingress_sha256=DIGEST_B)
    cancelled = scheduler.cancel(cancelled["job_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["cancellation"] == "acknowledged"
    assert cancelled["orphan_state"] == "not-orphaned"


def test_public_fake_backend_and_conformance_receipt_are_executable() -> None:
    backend = FakeProviderBackend()
    network = backend.invoke({
        "operation": "openada.operation/network.parameters.extract/v1alpha1",
        "parameters": {"ports": 1, "rows": [[1, 0, 0], [2, 1, 0]]},
    })
    assert network["points"] == 2
    transform = backend.invoke({
        "operation": "openada.operation/artifact.transform/v1alpha1",
        "parameters": {"input_hex": "6578616d706c65", "transform": "ascii-upper"},
    })
    assert bytes.fromhex(transform["output_hex"]) == b"EXAMPLE"
    receipt = ConformanceSuite().run(
        receipt_id="org.example.conformance.fake",
        profile={"identity": "openada.operation/artifact.transform/v1alpha1", "revision": "v1alpha1", "sha256": DIGEST_A},
        mapping={"identity": "org.example.mapping.fake", "revision": "v1alpha1", "sha256": DIGEST_B},
        capability_id="org.example.capability.fake",
        cases=fake_backend_cases(backend),
        limitations=["Deterministic public self-attestation only."],
    )
    assert len(receipt["cases"]) == 20
    assert {case["category"] for case in receipt["cases"]} >= {
        "dependency", "process", "artifact", "correlation", "tamper",
        "containment", "bounds", "cancellation", "isolation", "redaction",
        "ownership", "restart", "cleanup", "replay",
    }
    assert receipt["readiness"] == {
        "protocol": "ready",
        "artifact": "ready",
        "semantic": "ready",
        "transport": "ready",
        "review": "not-reviewed",
        "extensions": {},
    }
    assert receipt["self_attestation"] is True


def test_multistep_result_keeps_dependencies_and_truth_dimensions_consistent() -> None:
    result = {
        "schema": "openada.result/v0alpha2",
        "request_id": "00000000-0000-0000-0000-000000000001",
        "request_sha256": DIGEST_A,
        "profile": {"identity": "org.example.operation/alpha/v1", "revision": "v1", "sha256": DIGEST_A, "extensions": {}},
        "mapping": {"identity": "org.example.mapping.alpha", "revision": "v1", "sha256": DIGEST_B, "extensions": {}},
        "capability_id": "org.example.capability.alpha",
        "steps": [
            {
                "id": "prepare", "role": "prepare", "depends_on": [],
                "capability_id": "org.example.capability.alpha", "mapping_sha256": DIGEST_B,
                "transport_identity": "org.example.transport.fake", "action_identity": "prepare",
                "bounds": {"items": 1}, "termination": "completed", "inputs": [], "outputs": [DIGEST_A],
                "parser_identity": None, "validator_identity": "org.example.validator.alpha",
                "dimensions": dimensions(), "diagnostics": [], "extensions": {},
            },
            {
                "id": "collect", "role": "collect", "depends_on": ["prepare"],
                "capability_id": "org.example.capability.alpha", "mapping_sha256": DIGEST_B,
                "transport_identity": "org.example.transport.fake", "action_identity": "collect",
                "bounds": {"items": 1}, "termination": "completed", "inputs": [DIGEST_A], "outputs": [DIGEST_B],
                "parser_identity": "org.example.parser.alpha", "validator_identity": "org.example.validator.alpha",
                "dimensions": dimensions(), "diagnostics": [], "extensions": {},
            },
        ],
        "overall": dimensions(),
        "artifacts": [
            {"role": "prepared", "sha256": DIGEST_A, "bytes": 1, "freshness": "fresh", "extensions": {}},
            {"role": "collected", "sha256": DIGEST_B, "bytes": 1, "freshness": "fresh", "extensions": {}},
        ],
        "diagnostics": [],
        "extensions": {},
    }
    validate_result_semantics(result)
    invalid = json.loads(json.dumps(result))
    invalid["steps"][0]["depends_on"] = ["collect"]
    with pytest.raises(ResultSemanticError, match="forward dependencies"):
        validate_result_semantics(invalid)
    invalid = json.loads(json.dumps(result))
    invalid["overall"]["artifact_readiness"] = "partial"
    with pytest.raises(ResultSemanticError, match="cannot claim engineering pass"):
        validate_result_semantics(invalid)
