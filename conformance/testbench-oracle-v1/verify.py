#!/usr/bin/env python3
"""Independently verify a testbench-oracle comparator conformance record."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import stat
from typing import Any

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
MAX_JSON_BYTES = 1024 * 1024

MANIFEST_SCHEMA = "simra.testbench-oracle-conformance-manifest/v1"
RUN_SCHEMA = "simra.testbench-oracle-conformance-run/v1"
CONFORMANCE_ID = "testbench-oracle-comparator-v1"
OPERATION_PROFILE = "openada.operation/testbench.oracle.compare/v1alpha1"
METRIC_ROWS = (
    "signed_response_coverage",
    "offset_error_vs_oracle",
    "local_gain_error",
    "src_curve_error",
    "snk_curve_error",
    "mismatch_curve_error",
    "compliance_endpoint_error",
    "leak_error",
    "invalid_detection_recall",
    "false_valid_rate",
    "completeness",
    "grading_runtime",
)
TAMPER_CASES = (
    "metric-value",
    "metric-status",
    "summary-count",
    "fixture-binding",
)
EXPECTED_VALUES = {
    "signed_response_coverage": 1.0,
    "offset_error_vs_oracle": 0.0,
    "local_gain_error": 0.0,
    "src_curve_error": 0.0,
    "snk_curve_error": 0.0,
    "mismatch_curve_error": 0.0,
    "compliance_endpoint_error": 0.0,
    "leak_error": 0.0,
    "invalid_detection_recall": 1.0,
    "false_valid_rate": 0.0,
    "completeness": 1.0,
    "grading_runtime": 42.0,
}


class ConformanceError(RuntimeError):
    """A contract, fixture, binding, result, or tamper case is inconsistent."""


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(
            f"{location}: expected {expected!r}, got {actual!r}"
        )


def _expect_number(actual: Any, expected: float, location: str) -> None:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ConformanceError(f"{location}: expected a finite number")
    if not math.isfinite(float(actual)) or not math.isclose(
        float(actual), expected, rel_tol=1e-15, abs_tol=1e-18
    ):
        raise ConformanceError(
            f"{location}: expected {expected!r}, got {actual!r}"
        )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _require_regular(
    path: Path, *, label: str, maximum_bytes: int = MAX_JSON_BYTES
) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must be a regular, non-linked file: {path}")
    if not 1 <= metadata.st_size <= maximum_bytes:
        raise ConformanceError(
            f"{label} size {metadata.st_size} is outside 1..{maximum_bytes} bytes"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    expected_size = _require_regular(path, label=label)
    try:
        encoded = path.read_bytes()
        if len(encoded) != expected_size:
            raise ConformanceError(f"{label} changed while it was read: {path}")
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConformanceError(f"value is not canonical finite JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _repository_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ConformanceError(f"{label} must be a repository-relative path")
    root = REPOSITORY_ROOT.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ConformanceError(f"{label} escapes the repository root") from exc
    return candidate


def _bound_document(record: object, *, label: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(record, dict):
        raise ConformanceError(f"{label} must be an object")
    allowed = {"repository_path", "sha256"}
    extras = {"id"} if "id" in record else {"license"}
    _expect(set(record), allowed | extras, f"{label}.keys")
    path = _repository_path(record["repository_path"], label=f"{label}.repository_path")
    _require_regular(path, label=label)
    _expect(_sha256(path), record["sha256"], f"{label}.sha256")
    return path, _read_json(path, label=label)


def _validate_schema(
    document: dict[str, Any], schema: dict[str, Any], *, label: str
) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ConformanceError(f"{label} contract schema is invalid: {exc}") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label}.{location}: {error.message}")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _read_json(path.resolve(), label="testbench-oracle manifest")
    _expect(
        set(manifest),
        {
            "schema",
            "id",
            "operation_profile",
            "implementation",
            "contracts",
            "fixtures",
            "metric_rows",
            "tamper_cases",
            "policy",
        },
        "manifest.keys",
    )
    _expect(manifest["schema"], MANIFEST_SCHEMA, "manifest.schema")
    _expect(manifest["id"], CONFORMANCE_ID, "manifest.id")
    _expect(manifest["operation_profile"], OPERATION_PROFILE, "manifest.operation_profile")
    _expect(list(manifest["metric_rows"]), list(METRIC_ROWS), "manifest.metric_rows")
    _expect(list(manifest["tamper_cases"]), list(TAMPER_CASES), "manifest.tamper_cases")
    _expect(
        manifest["policy"],
        {
            "native_eda": "none",
            "network": "none",
            "input_mode": "read-only-fixtures",
            "evidence_mode": "new-file-only",
            "maximum_evidence_bytes": MAX_JSON_BYTES,
        },
        "manifest.policy",
    )

    implementation = manifest["implementation"]
    _expect(
        set(implementation),
        {"module", "callable", "repository_path", "sha256"},
        "manifest.implementation.keys",
    )
    _expect(
        implementation["module"],
        "openada.operations.testbench_oracle",
        "manifest.implementation.module",
    )
    _expect(
        implementation["callable"],
        "compare_testbench_observables",
        "manifest.implementation.callable",
    )
    source = _repository_path(
        implementation["repository_path"],
        label="manifest.implementation.repository_path",
    )
    _require_regular(source, label="comparator source")
    _expect(_sha256(source), implementation["sha256"], "manifest.implementation.sha256")

    contracts = manifest["contracts"]
    _expect(set(contracts), {"observables", "tolerances", "comparison"}, "manifest.contracts")
    expected_contracts = {
        "observables": "simra.testbench-observables/v1",
        "tolerances": "simra.testbench-oracle-tolerances/v1",
        "comparison": "simra.testbench-oracle-comparison/v1",
    }
    for name, expected_id in expected_contracts.items():
        _, schema = _bound_document(contracts[name], label=f"{name} schema")
        _expect(contracts[name]["id"], expected_id, f"manifest.contracts.{name}.id")
        Draft202012Validator.check_schema(schema)

    fixtures = manifest["fixtures"]
    _expect(
        set(fixtures),
        {"observed", "oracle", "tolerances", "expected_comparison", "tamper_cases"},
        "manifest.fixtures",
    )
    for name, record in fixtures.items():
        _expect(record.get("license"), "MIT", f"manifest.fixtures.{name}.license")
        _bound_document(record, label=f"{name} fixture")
    return manifest


def _contract_schemas(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: _bound_document(record, label=f"{name} schema")[1]
        for name, record in manifest["contracts"].items()
    }


def _fixture_sha256(manifest: dict[str, Any]) -> str:
    return _canonical_sha256(
        {
            name: record["sha256"]
            for name, record in manifest["fixtures"].items()
            if name in {"observed", "oracle", "tolerances"}
        }
    )


def load_fixtures(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fixtures = {
        name: _bound_document(record, label=f"{name} fixture")[1]
        for name, record in manifest["fixtures"].items()
    }
    schemas = _contract_schemas(manifest)
    _validate_schema(fixtures["observed"], schemas["observables"], label="observed")
    _validate_schema(fixtures["tolerances"], schemas["tolerances"], label="tolerances")
    _validate_schema(
        fixtures["expected_comparison"],
        schemas["comparison"],
        label="expected_comparison",
    )

    oracle = fixtures["oracle"]
    _expect(set(oracle), {"sizing", "corner", "validity", "observables"}, "oracle.keys")
    _expect(oracle["corner"], fixtures["observed"]["corner"], "oracle.corner")

    tolerance_names = [row["name"] for row in fixtures["tolerances"]["metrics"]]
    expected_names = [row["name"] for row in fixtures["expected_comparison"]["metrics"]]
    _expect(tolerance_names, list(METRIC_ROWS), "tolerances.metric_names")
    _expect(expected_names, list(METRIC_ROWS), "expected.metric_names")
    for index, (tolerance, expected) in enumerate(
        zip(fixtures["tolerances"]["metrics"], fixtures["expected_comparison"]["metrics"])
    ):
        label = f"expected.metrics[{index}]"
        _expect(expected["required"], True, f"{label}.required")
        _expect(expected["status"], "PASS", f"{label}.status")
        _expect(expected["limit"], tolerance["limit"], f"{label}.limit")
        _expect_number(expected["value"], EXPECTED_VALUES[expected["name"]], f"{label}.value")
    _expect(fixtures["expected_comparison"]["status"], "PASS", "expected.status")
    _expect(
        fixtures["expected_comparison"]["summary"],
        {
            "pass": 12,
            "fail": 0,
            "unknown": 0,
            "required": 12,
            "required_pass": 12,
            "required_fail": 0,
            "required_unknown": 0,
        },
        "expected.summary",
    )
    _expect(
        fixtures["expected_comparison"]["validity"],
        {
            "oracle_invalid": 1,
            "detected_invalid": 1,
            "observed_valid": 4,
            "false_valid": 0,
            "missing_or_unknown": 0,
        },
        "expected.validity",
    )

    tampers = fixtures["tamper_cases"]
    _expect(set(tampers), {"schema", "cases"}, "tamper_cases.keys")
    _expect(tampers["schema"], "simra.testbench-oracle-conformance-tampers/v1", "tamper_cases.schema")
    _expect([case["id"] for case in tampers["cases"]], list(TAMPER_CASES), "tamper_cases.ids")
    for index, case in enumerate(tampers["cases"]):
        _expect(
            set(case),
            {"id", "path", "replacement", "expected_message"},
            f"tamper_cases[{index}].keys",
        )
        if not isinstance(case["path"], list) or not case["path"]:
            raise ConformanceError(f"tamper_cases[{index}].path must be nonempty")
        if not isinstance(case["expected_message"], str) or not case["expected_message"]:
            raise ConformanceError(f"tamper_cases[{index}].expected_message must be nonempty")
    return fixtures


def _verify_record(
    record: dict[str, Any],
    manifest: dict[str, Any],
    fixtures: dict[str, dict[str, Any]],
) -> None:
    _expect(
        set(record),
        {
            "schema",
            "conformance_id",
            "operation_profile",
            "implementation_sha256",
            "fixture_sha256",
            "expected_sha256",
            "comparison_sha256",
            "comparison",
        },
        "evidence.keys",
    )
    _expect(record["schema"], RUN_SCHEMA, "evidence.schema")
    _expect(record["conformance_id"], CONFORMANCE_ID, "evidence.conformance_id")
    _expect(record["operation_profile"], OPERATION_PROFILE, "evidence.operation_profile")
    _expect(
        record["implementation_sha256"],
        manifest["implementation"]["sha256"],
        "evidence.implementation_sha256",
    )
    _expect(record["fixture_sha256"], _fixture_sha256(manifest), "evidence.fixture_sha256")
    _expect(
        record["expected_sha256"],
        manifest["fixtures"]["expected_comparison"]["sha256"],
        "evidence.expected_sha256",
    )
    comparison = record["comparison"]
    if not isinstance(comparison, dict):
        raise ConformanceError("evidence.comparison must be an object")
    _expect(
        record["comparison_sha256"],
        _canonical_sha256(comparison),
        "evidence.comparison_sha256",
    )
    _expect(comparison, fixtures["expected_comparison"], "evidence.comparison")
    schemas = _contract_schemas(manifest)
    _validate_schema(comparison, schemas["comparison"], label="comparison")


def _replace_path(document: dict[str, Any], path: list[Any], replacement: Any) -> None:
    target: Any = document
    for part in path[:-1]:
        if isinstance(part, int):
            if not isinstance(target, list) or not 0 <= part < len(target):
                raise ConformanceError(f"tamper path index {part!r} is invalid")
        elif not isinstance(part, str) or not isinstance(target, dict) or part not in target:
            raise ConformanceError(f"tamper path member {part!r} is invalid")
        target = target[part]
    final = path[-1]
    if isinstance(final, int):
        if not isinstance(target, list) or not 0 <= final < len(target):
            raise ConformanceError(f"tamper final index {final!r} is invalid")
    elif not isinstance(final, str) or not isinstance(target, dict) or final not in target:
        raise ConformanceError(f"tamper final member {final!r} is invalid")
    target[final] = replacement


def _verify_tamper_cases(
    record: dict[str, Any],
    manifest: dict[str, Any],
    fixtures: dict[str, dict[str, Any]],
) -> None:
    for case in fixtures["tamper_cases"]["cases"]:
        tampered = deepcopy(record)
        _replace_path(tampered, case["path"], case["replacement"])
        try:
            _verify_record(tampered, manifest, fixtures)
        except ConformanceError as exc:
            if case["expected_message"] not in str(exc):
                raise ConformanceError(
                    f"tamper case {case['id']!r} failed at the wrong boundary: {exc}"
                ) from exc
        else:
            raise ConformanceError(f"tamper case {case['id']!r} was not detected")


def verify_evidence(
    evidence_path: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path.resolve())
    fixtures = load_fixtures(manifest)
    record = _read_json(evidence_path.resolve(), label="testbench-oracle evidence")
    _verify_record(record, manifest, fixtures)
    _verify_tamper_cases(record, manifest, fixtures)
    return {
        "schema": "simra.testbench-oracle-conformance-verification/v1",
        "conformance_id": CONFORMANCE_ID,
        "operation_profile": OPERATION_PROFILE,
        "status": "pass",
        "verified_metrics": len(METRIC_ROWS),
        "verified_tamper_cases": len(TAMPER_CASES),
        "comparison_sha256": record["comparison_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args(argv)
    verification = verify_evidence(
        arguments.evidence_file,
        manifest_path=arguments.manifest,
    )
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
