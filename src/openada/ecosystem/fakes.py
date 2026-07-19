"""Deterministic, vendor-neutral fake backends for public conformance tests."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes


class FakeBackendError(ValueError):
    """A public fake fixture violates its closed semantic vocabulary."""


_HDL_TOP = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,199}$")


class FakeProviderBackend:
    """Exercise four unrelated operation families without native commands."""

    identity = "org.example.provider.fake"
    revision = "v1alpha1"

    def invoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        parameters = request.get("parameters")
        if not isinstance(parameters, Mapping):
            raise FakeBackendError("fake request parameters must be an object")
        if operation == "openada.operation/digital.hdl.simulate/v1alpha1":
            return self._digital(parameters)
        if operation == "openada.operation/network.parameters.extract/v1alpha1":
            return self._network(parameters)
        if operation == "openada.operation/electromagnetic.analyze/v1alpha1":
            return self._electromagnetic(parameters)
        if operation == "openada.operation/artifact.transform/v1alpha1":
            return self._transform(parameters)
        raise FakeBackendError(f"fake backend does not implement operation {operation!r}")

    @staticmethod
    def _digital(parameters: Mapping[str, Any]) -> dict[str, Any]:
        top = parameters.get("top")
        sources = parameters.get("sources")
        scenario = parameters.get("scenario", "pass")
        if not isinstance(top, str) or not _HDL_TOP.fullmatch(top):
            raise FakeBackendError("digital fake requires a bounded HDL top identity")
        if (
            not isinstance(sources, Sequence)
            or isinstance(sources, (str, bytes))
            or not sources
            or len(sources) > 64
            or any(not isinstance(item, str) or len(item) > 100_000 for item in sources)
        ):
            raise FakeBackendError("digital fake requires one to 64 bounded source strings")
        if scenario not in {"pass", "compile-fail", "self-check-fail"}:
            raise FakeBackendError("digital fake scenario is outside the closed vocabulary")
        source_digests = [hashlib.sha256(item.encode()).hexdigest() for item in sources]
        steps = ["prepare"]
        conclusion = "pass"
        if scenario == "compile-fail":
            steps.append("compile-failed")
            conclusion = "fail"
        else:
            steps.extend(["compile", "elaborate", "execute", "collect"])
            if scenario == "self-check-fail":
                conclusion = "fail"
        evidence = canonical_json_bytes(
            {"top": top, "source_digests": source_digests, "scenario": scenario}
        )
        return {
            "operation": "digital.hdl.simulate",
            "steps": steps,
            "source_digests": source_digests,
            "self_check": conclusion,
            "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        }

    @staticmethod
    def _network(parameters: Mapping[str, Any]) -> dict[str, Any]:
        rows = parameters.get("rows")
        ports = parameters.get("ports")
        if not isinstance(ports, int) or isinstance(ports, bool) or not 1 <= ports <= 16:
            raise FakeBackendError("network fake ports must be an integer from one to 16")
        width = 1 + (2 * ports * ports)
        if (
            not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes))
            or not rows
            or len(rows) > 10_000
        ):
            raise FakeBackendError("network fake requires one to 10000 numeric rows")
        normalized: list[list[int | float]] = []
        previous = -math.inf
        for row in rows:
            if (
                not isinstance(row, Sequence)
                or isinstance(row, (str, bytes))
                or len(row) != width
            ):
                raise FakeBackendError("network row width does not match the port count")
            values: list[int | float] = []
            for value in row:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise FakeBackendError("network rows contain a non-numeric value")
                number = float(value)
                if not math.isfinite(number):
                    raise FakeBackendError("network rows contain a non-finite value")
                values.append(value)
            frequency = float(values[0])
            if frequency <= previous:
                raise FakeBackendError("network frequencies must be strictly increasing")
            previous = frequency
            normalized.append(values)
        encoded = canonical_json_bytes(normalized)
        return {
            "operation": "network.parameters.extract",
            "ports": ports,
            "points": len(normalized),
            "frequency_min": normalized[0][0],
            "frequency_max": normalized[-1][0],
            "series_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    @staticmethod
    def _electromagnetic(parameters: Mapping[str, Any]) -> dict[str, Any]:
        cells = parameters.get("cells")
        frequencies = parameters.get("frequencies")
        if not isinstance(cells, int) or isinstance(cells, bool) or not 1 <= cells <= 1_000_000:
            raise FakeBackendError("electromagnetic fake cell count is out of bounds")
        if (
            not isinstance(frequencies, Sequence)
            or isinstance(frequencies, (str, bytes))
            or not frequencies
            or len(frequencies) > 10_000
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                for value in frequencies
            )
        ):
            raise FakeBackendError("electromagnetic frequencies are invalid or over limit")
        if any(float(left) >= float(right) for left, right in zip(frequencies, frequencies[1:])):
            raise FakeBackendError("electromagnetic frequencies must be strictly increasing")
        mesh_sha256 = hashlib.sha256(f"org.example.mesh:{cells}".encode()).hexdigest()
        response_sha256 = hashlib.sha256(
            canonical_json_bytes({"mesh": mesh_sha256, "frequencies": frequencies})
        ).hexdigest()
        return {
            "operation": "electromagnetic.analyze",
            "cells": cells,
            "points": len(frequencies),
            "mesh_sha256": mesh_sha256,
            "response_sha256": response_sha256,
            "engineering_conclusion": "not-evaluated",
        }

    @staticmethod
    def _transform(parameters: Mapping[str, Any]) -> dict[str, Any]:
        input_hex = parameters.get("input_hex")
        transform = parameters.get("transform")
        if not isinstance(input_hex, str) or len(input_hex) > 2_000_000:
            raise FakeBackendError("transform fake input is malformed or over limit")
        try:
            payload = bytes.fromhex(input_hex)
        except ValueError as exc:
            raise FakeBackendError("transform fake input is not hexadecimal") from exc
        if transform == "identity":
            output = payload
        elif transform == "ascii-upper":
            try:
                output = payload.decode("ascii").upper().encode("ascii")
            except UnicodeDecodeError as exc:
                raise FakeBackendError("ascii-upper requires ASCII input") from exc
        elif transform == "reverse":
            output = payload[::-1]
        else:
            raise FakeBackendError("transform fake uses an unsupported transform")
        return {
            "operation": "artifact.transform",
            "transform": transform,
            "input_sha256": hashlib.sha256(payload).hexdigest(),
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_hex": output.hex(),
        }


class FakeOperationValidator:
    """Small executable validator used to prove registry dispatch and isolation."""

    def __init__(self, expected_operation: str) -> None:
        self.expected_operation = expected_operation

    def validate_request(self, request: Mapping[str, Any]) -> Sequence[str]:
        return () if request.get("operation") == self.expected_operation else (
            "request operation does not match the selected profile",
        )

    def validate_semantics(self, request: Mapping[str, Any]) -> Sequence[str]:
        return () if isinstance(request.get("parameters"), Mapping) else (
            "request parameters must be an object",
        )

    def validate_result(self, result: Mapping[str, Any]) -> Sequence[str]:
        return () if isinstance(result.get("operation"), str) else (
            "fake result lacks an operation identity",
        )

    def validate_evidence(self, result: Mapping[str, Any]) -> Sequence[str]:
        digests = [value for key, value in result.items() if key.endswith("sha256")]
        return () if digests else ("fake result contains no digest-bound evidence",)

    def validate_cross_artifacts(
        self, request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> Sequence[str]:
        parts = self.expected_operation.split("/")
        expected = parts[-2] if len(parts) >= 3 else self.expected_operation
        return () if result.get("operation") == expected else (
            "fake result operation does not match the request operation",
        )
