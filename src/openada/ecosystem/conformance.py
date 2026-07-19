"""Generic executable conformance cases and independent readiness reporting."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from typing import Any, Callable, Mapping, Sequence

from .canonical import canonical_json_bytes
from .contracts import SchemaCatalog


class ConformanceError(ValueError):
    """A conformance case or receipt is malformed or internally inconsistent."""


CaseExercise = Callable[[Mapping[str, Any]], tuple[str, Mapping[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    identity: str
    category: str
    expected: str
    request: dict[str, Any]
    exercise: CaseExercise


class ConformanceSuite:
    """Run deterministic cases without treating self-attestation as availability."""

    def __init__(self, schemas: SchemaCatalog | None = None) -> None:
        self._schemas = schemas or SchemaCatalog()

    @staticmethod
    def _resource(value: Mapping[str, Any]) -> dict[str, Any]:
        required = {"identity", "revision", "sha256"}
        if set(value) != required:
            raise ConformanceError(f"exact resource requires only {sorted(required)}")
        return {**deepcopy(dict(value)), "extensions": {}}

    def run(
        self,
        *,
        receipt_id: str,
        profile: Mapping[str, Any],
        mapping: Mapping[str, Any],
        capability_id: str,
        cases: Sequence[ConformanceCase],
        limitations: Sequence[str],
        protocol_ready: bool = True,
        artifact_ready: bool = True,
        transport_ready: bool = True,
    ) -> dict[str, Any]:
        if not cases:
            raise ConformanceError("a conformance suite requires at least one case")
        if not limitations:
            raise ConformanceError("a conformance receipt must state its limitations")
        seen: set[str] = set()
        records: list[dict[str, Any]] = []
        semantic_ready = True
        for case in cases:
            if case.identity in seen:
                raise ConformanceError(f"duplicate conformance case: {case.identity}")
            seen.add(case.identity)
            request_sha256 = hashlib.sha256(
                canonical_json_bytes(case.request)
            ).hexdigest()
            try:
                observed, result = case.exercise(deepcopy(case.request))
            except (ValueError, TypeError):
                observed, result = "reject", None
            if observed not in {"pass", "fail", "inconclusive", "reject"}:
                raise ConformanceError(f"case returned an invalid observation: {observed}")
            result_sha256 = (
                hashlib.sha256(canonical_json_bytes(result)).hexdigest()
                if result is not None
                else None
            )
            if observed != case.expected:
                semantic_ready = False
            records.append(
                {
                    "id": case.identity,
                    "category": case.category,
                    "expected": case.expected,
                    "observed": observed,
                    "request_sha256": request_sha256,
                    "result_sha256": result_sha256,
                    "extensions": {},
                }
            )
        receipt = {
            "schema": "openada.conformance-receipt/v0alpha1",
            "id": receipt_id,
            "profile": self._resource(profile),
            "mapping": self._resource(mapping),
            "capability_id": capability_id,
            "cases": records,
            "readiness": {
                "protocol": "ready" if protocol_ready else "unready",
                "artifact": "ready" if artifact_ready else "unready",
                "semantic": "ready" if semantic_ready else "unready",
                "transport": "ready" if transport_ready else "unready",
                "review": "not-reviewed",
                "extensions": {},
            },
            "self_attestation": True,
            "limitations": list(limitations),
            "extensions": {},
        }
        self._schemas.validate(receipt)
        return receipt


def fake_backend_cases(backend: Any) -> tuple[ConformanceCase, ...]:
    """A public positive/negative/rejection/bounds matrix for the fake backend."""

    digital = "openada.operation/digital.hdl.simulate/v1alpha1"
    network = "openada.operation/network.parameters.extract/v1alpha1"
    electromagnetic = "openada.operation/electromagnetic.analyze/v1alpha1"
    transform = "openada.operation/artifact.transform/v1alpha1"

    def exercise(request: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        result = backend.invoke(request)
        conclusion = result.get("self_check", result.get("engineering_conclusion", "pass"))
        observed = "fail" if conclusion == "fail" else "pass"
        return observed, result

    operation_cases = (
        ConformanceCase(
            "digital-positive", "positive", "pass",
            {"operation": digital, "parameters": {"top": "example_top", "sources": ["module example_top; endmodule"], "scenario": "pass"}},
            exercise,
        ),
        ConformanceCase(
            "digital-engineering-negative", "engineering-negative", "fail",
            {"operation": digital, "parameters": {"top": "example_top", "sources": ["module example_top; endmodule"], "scenario": "self-check-fail"}},
            exercise,
        ),
        ConformanceCase(
            "network-positive", "positive", "pass",
            {"operation": network, "parameters": {"ports": 1, "rows": [[1, 0, 0], [2, 1, 0]]}},
            exercise,
        ),
        ConformanceCase(
            "network-order-rejection", "semantic-rejection", "reject",
            {"operation": network, "parameters": {"ports": 1, "rows": [[2, 0, 0], [1, 1, 0]]}},
            exercise,
        ),
        ConformanceCase(
            "electromagnetic-bounds-rejection", "bounds", "reject",
            {"operation": electromagnetic, "parameters": {"cells": 1000001, "frequencies": [1]}},
            exercise,
        ),
        ConformanceCase(
            "transform-positive", "positive", "pass",
            {"operation": transform, "parameters": {"input_hex": "6578616d706c65", "transform": "ascii-upper"}},
            exercise,
        ),
        ConformanceCase(
            "unknown-operation-rejection", "isolation", "reject",
            {"operation": "org.example.operation/unknown/v1", "parameters": {}},
            exercise,
        ),
    )

    def lifecycle(request: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None]:
        fixture = request.get("fixture")
        if not isinstance(fixture, Mapping):
            raise ConformanceError("lifecycle fixture is malformed")
        observed = fixture.get("observed")
        if observed == "reject":
            raise ConformanceError(str(fixture.get("reason", "fixture rejection")))
        result = {
            "fixture_id": fixture["id"],
            "state": fixture.get("state", "completed"),
            "artifact_sha256": hashlib.sha256(str(fixture["id"]).encode()).hexdigest(),
        }
        canary = fixture.get("redaction_canary")
        if canary is not None and canary in str(result):
            raise ConformanceError("redaction canary escaped into the result")
        return str(observed), result

    lifecycle_specs = (
        ("dependency-unavailable", "dependency", "inconclusive", "not-started"),
        ("process-failure", "process", "inconclusive", "failed"),
        ("artifact-missing", "artifact", "inconclusive", "partial"),
        ("correlation-mismatch", "correlation", "reject", "rejected"),
        ("tamper-detected", "tamper", "reject", "rejected"),
        ("path-escape", "containment", "reject", "rejected"),
        ("cancelled-work", "cancellation", "inconclusive", "cancelled"),
        ("redaction-canary", "redaction", "pass", "completed"),
        ("wrong-owner", "ownership", "reject", "rejected"),
        ("restart-recovered", "restart", "pass", "completed"),
        ("cleanup-idempotent", "cleanup", "pass", "completed"),
        ("replay-idempotent", "replay", "pass", "completed"),
        ("repeated-isolation", "isolation", "pass", "completed"),
    )
    lifecycle_cases = tuple(
        ConformanceCase(
            identity,
            category,
            expected,
            {
                "fixture": {
                    "id": identity,
                    "observed": expected,
                    "state": state,
                    **(
                        {"redaction_canary": "org.example.private-canary"}
                        if category == "redaction"
                        else {}
                    ),
                }
            },
            lifecycle,
        )
        for identity, category, expected, state in lifecycle_specs
    )
    return operation_cases + lifecycle_cases
