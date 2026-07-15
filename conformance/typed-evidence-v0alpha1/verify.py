#!/usr/bin/env python3
"""Independently verify the typed-evidence kernel conformance record."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import stat
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
MAX_JSON_BYTES = 5 * 1024 * 1024

MEASUREMENT_FEATURES = (
    "openada.feature/measurement.sample-at/v1alpha1",
    "openada.feature/measurement.minimum/v1alpha1",
    "openada.feature/measurement.maximum/v1alpha1",
    "openada.feature/measurement.mean/v1alpha1",
    "openada.feature/measurement.rms/v1alpha1",
    "openada.feature/measurement.crossing/v1alpha1",
    "openada.feature/measurement.rise-time/v1alpha1",
    "openada.feature/measurement.fall-time/v1alpha1",
    "openada.feature/measurement.settling-time/v1alpha1",
)
SPECIFICATION_FEATURES = (
    "openada.feature/specification.bound-evaluation/v1alpha1",
    "openada.feature/specification.condition-binding/v1alpha1",
)
KIND_FEATURES = {
    "sample_at": MEASUREMENT_FEATURES[0],
    "minimum": MEASUREMENT_FEATURES[1],
    "maximum": MEASUREMENT_FEATURES[2],
    "mean": MEASUREMENT_FEATURES[3],
    "rms": MEASUREMENT_FEATURES[4],
    "crossing": MEASUREMENT_FEATURES[5],
    "rise_time": MEASUREMENT_FEATURES[6],
    "fall_time": MEASUREMENT_FEATURES[7],
    "settling_time": MEASUREMENT_FEATURES[8],
}


class ConformanceError(RuntimeError):
    """A fixture, contract, run record, or normalized result is inconsistent."""


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _expect_close(actual: Any, expected: Any, location: str) -> None:
    if expected is None:
        _expect(actual, None, location)
        return
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ConformanceError(f"{location}: expected a finite number, got {actual!r}")
    if not math.isfinite(float(actual)) or not math.isclose(
        float(actual),
        float(expected),
        rel_tol=1e-12,
        abs_tol=1e-15,
    ):
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _require_regular(path: Path, *, label: str, maximum_bytes: int = MAX_JSON_BYTES) -> int:
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
        raise ConformanceError(f"{label} must be a nonempty repository-relative path")
    candidate = (REPOSITORY_ROOT / value).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise ConformanceError(f"{label} escapes the repository root") from exc
    return candidate


def _contract_document(record: dict[str, Any], *, label: str) -> dict[str, Any]:
    path = _repository_path(record.get("repository_path"), label=f"{label}.repository_path")
    _require_regular(path, label=label)
    _expect(_sha256(path), record.get("sha256"), f"{label}.sha256")
    return _read_json(path, label=label)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _read_json(path.resolve(), label="typed-evidence conformance manifest")
    _expect(
        set(manifest),
        {
            "schema",
            "id",
            "implementation",
            "contracts",
            "fixture",
            "features",
            "cases",
            "policy",
        },
        "manifest.keys",
    )
    _expect(manifest["schema"], "openada.typed-evidence-conformance/v0alpha1", "manifest.schema")
    _expect(manifest["id"], "typed-evidence-measurement-specification-v0alpha1", "manifest.id")
    _expect(
        manifest["implementation"],
        {
            "id": "org.openada.kernel.typed-evidence",
            "version": "1.0.0",
            "runtime": "python",
        },
        "manifest.implementation",
    )
    _expect(
        manifest["policy"],
        {
            "native_eda": "none",
            "network": "none",
            "input_mode": "read-only-fixture",
            "evidence_mode": "new-file-only",
            "maximum_evidence_bytes": MAX_JSON_BYTES,
        },
        "manifest.policy",
    )
    _expect(
        manifest["features"],
        {
            "measurement": list(MEASUREMENT_FEATURES),
            "specification": list(SPECIFICATION_FEATURES),
        },
        "manifest.features",
    )

    contracts = manifest["contracts"]
    _expect(set(contracts), {"result_schema", "measurement", "specification"}, "manifest.contracts")
    result_schema = _contract_document(contracts["result_schema"], label="result schema")
    _expect(contracts["result_schema"]["id"], "openada.result/v0alpha1", "manifest.contracts.result_schema.id")
    _expect(result_schema.get("title"), "OpenADA result v0alpha1", "result_schema.title")

    measurement_profile = _contract_document(
        contracts["measurement"],
        label="result.measure profile",
    )
    specification_profile = _contract_document(
        contracts["specification"],
        label="specification.evaluate profile",
    )
    for name, profile, expected_operation, expected_assertion, expected_features in (
        (
            "measurement",
            measurement_profile,
            "openada.operation/result.measure/v1alpha1",
            "openada.assertion/measurement.valid/v1alpha1",
            MEASUREMENT_FEATURES,
        ),
        (
            "specification",
            specification_profile,
            "openada.operation/specification.evaluate/v1alpha1",
            "openada.assertion/specification.satisfied/v1alpha1",
            SPECIFICATION_FEATURES,
        ),
    ):
        contract = contracts[name]
        _expect(contract["operation_profile"], expected_operation, f"manifest.contracts.{name}.operation_profile")
        _expect(contract["assertion_profile"], expected_assertion, f"manifest.contracts.{name}.assertion_profile")
        _expect(profile["operation"]["id"], expected_operation, f"{name}_profile.operation.id")
        _expect(profile["assertion"]["id"], expected_assertion, f"{name}_profile.assertion.id")
        _expect(
            [item["id"] for item in profile["features"]],
            list(expected_features),
            f"{name}_profile.features",
        )
        mapping = profile["native_mappings"]
        _expect(len(mapping), 1, f"{name}_profile.native_mappings.count")
        _expect(mapping[0]["driver_id"], manifest["implementation"]["id"], f"{name}_profile.native_mappings.driver_id")
        _expect(mapping[0]["supported_features"], list(expected_features), f"{name}_profile.native_mappings.supported_features")
    return manifest


def load_cases(manifest: dict[str, Any]) -> dict[str, Any]:
    fixture = manifest["fixture"]
    _expect(
        set(fixture),
        {"schema", "repository_path", "sha256", "license"},
        "manifest.fixture.keys",
    )
    _expect(fixture["schema"], "openada.typed-evidence-conformance-cases/v0alpha1", "manifest.fixture.schema")
    _expect(fixture["license"], "MIT", "manifest.fixture.license")
    fixture_path = _repository_path(fixture["repository_path"], label="manifest.fixture.repository_path")
    _require_regular(fixture_path, label="typed-evidence request fixture")
    _expect(_sha256(fixture_path), fixture["sha256"], "manifest.fixture.sha256")
    cases = _read_json(fixture_path, label="typed-evidence request fixture")
    _expect(
        set(cases),
        {"schema", "series", "measurement_cases", "specification_cases"},
        "fixture.keys",
    )
    _expect(cases["schema"], fixture["schema"], "fixture.schema")
    _expect(set(cases["series"]), {"pulse", "settling"}, "fixture.series")

    measurement_ids = [case["id"] for case in cases["measurement_cases"]]
    specification_ids = [case["id"] for case in cases["specification_cases"]]
    _expect(measurement_ids, manifest["cases"]["measurement"], "fixture.measurement_case_ids")
    _expect(specification_ids, manifest["cases"]["specification"], "fixture.specification_case_ids")
    if len(measurement_ids) != len(set(measurement_ids)) or len(specification_ids) != len(set(specification_ids)):
        raise ConformanceError("fixture case identifiers must be unique")

    covered: set[str] = set()
    for index, case in enumerate(cases["measurement_cases"]):
        label = f"fixture.measurement_cases[{index}]"
        _expect(
            set(case),
            {"id", "feature_id", "request_id", "series", "measurement", "expected"},
            f"{label}.keys",
        )
        if case["series"] not in cases["series"]:
            raise ConformanceError(f"{label}.series names an unknown fixture")
        kind = case["measurement"].get("kind")
        _expect(case["feature_id"], KIND_FEATURES.get(kind), f"{label}.feature_id")
        covered.add(case["feature_id"])
    _expect(covered, set(MEASUREMENT_FEATURES), "fixture.measurement_feature_coverage")

    available_measurements = set(measurement_ids)
    for index, case in enumerate(cases["specification_cases"]):
        label = f"fixture.specification_cases[{index}]"
        _expect(
            set(case),
            {
                "id",
                "feature_ids",
                "request_id",
                "measurement_case",
                "measurement_mutation",
                "specification",
                "expected",
            },
            f"{label}.keys",
        )
        _expect(case["feature_ids"], list(SPECIFICATION_FEATURES), f"{label}.feature_ids")
        if case["measurement_case"] not in available_measurements:
            raise ConformanceError(f"{label}.measurement_case names an unknown case")
        if case["measurement_mutation"] not in {None, "replace-first-condition-with-125"}:
            raise ConformanceError(f"{label}.measurement_mutation is unsupported")
    return cases


def _normalized_conditions(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in conditions:
        value = item["value"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
        normalized.append({"name": item["name"], "value": value, "unit": item["unit"]})
    return normalized


def _materialize_series(definition: dict[str, Any]) -> tuple[dict[str, Any], str]:
    series = deepcopy(definition)
    axis = {
        "name": series["axis"]["name"],
        "unit": series["axis"]["unit"],
        "values": [float(value) for value in series["axis"]["values"]],
    }
    signals = [
        {
            "name": signal["name"],
            "unit": signal["unit"],
            "values": [float(value) for value in signal["values"]],
        }
        for signal in series["signals"]
    ]
    conditions = _normalized_conditions(series["conditions"])
    digest = _canonical_sha256(
        {"axis": axis, "signals": signals, "conditions": conditions}
    )
    series["source"]["artifact_sha256"] = digest
    return series, digest


def _mutated_measurement(measurement: dict[str, Any], mutation: str | None) -> dict[str, Any]:
    selected = deepcopy(measurement)
    if mutation is None:
        return selected
    if mutation == "replace-first-condition-with-125":
        selected["source"]["conditions"][0]["value"] = 125.0
        return selected
    raise ConformanceError(f"unsupported measurement mutation {mutation!r}")


def _normalized_specification(specification: dict[str, Any]) -> dict[str, Any]:
    return {
        "specification_id": specification["specification_id"],
        "measurement_id": specification["measurement_id"],
        "limits": {
            kind: {
                "value": float(bound["value"]),
                "unit": bound["unit"],
                "inclusive": bound["inclusive"],
            }
            for kind, bound in specification["limits"].items()
        },
        "conditions": _normalized_conditions(specification["conditions"]),
        "extensions": {},
    }


def _validate_schema(document: dict[str, Any], schema: dict[str, Any], *, label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label}.{location}: {error.message}")


def _diagnostic_code(result: dict[str, Any]) -> str | None:
    diagnostics = result["diagnostics"]
    if not diagnostics:
        return None
    return diagnostics[0]["code"]


def _verify_common_result(
    result: dict[str, Any],
    *,
    operation: str,
    request_id: str,
    engineering_status: str,
    execution_status: str,
    operation_profile: str,
    assertion_profile: str,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _validate_schema(result, result_schema, label=location)
    _validate_schema(result["data"], data_schema, label=f"{location}.data")
    _expect(result["operation"], operation, f"{location}.operation")
    _expect(result["tool"], None, f"{location}.tool")
    _expect(result["inputs"], [], f"{location}.inputs")
    _expect(result["artifacts"], [], f"{location}.artifacts")
    _expect(result["engineering"]["status"], engineering_status, f"{location}.engineering.status")
    _expect(result["execution"]["status"], execution_status, f"{location}.execution.status")
    _expect(result["execution"]["duration_ms"], 0, f"{location}.execution.duration_ms")
    _expect(result["execution"]["command"], [], f"{location}.execution.command")
    expected_exit = 0 if execution_status == "completed" else None
    _expect(result["execution"]["exit_code"], expected_exit, f"{location}.execution.exit_code")
    protocol = result["data"]["protocol"]
    _expect(protocol["request_id"], request_id, f"{location}.protocol.request_id")
    _expect(protocol["operation_profile"], operation_profile, f"{location}.protocol.operation_profile")
    _expect(protocol["assertion_profile"], assertion_profile, f"{location}.protocol.assertion_profile")
    _expect(protocol["implementation_id"], "org.openada.kernel.typed-evidence", f"{location}.protocol.implementation_id")
    _expect(protocol["implementation_version"], "1.0.0", f"{location}.protocol.implementation_version")


def _verify_source(
    source: dict[str, Any],
    *,
    definition: dict[str, Any],
    digest: str,
    location: str,
) -> None:
    normalized_conditions = _normalized_conditions(definition["conditions"])
    expected = {
        **definition["source"],
        "artifact_sha256": digest,
        "series_sha256": digest,
        "conditions_sha256": _canonical_sha256(normalized_conditions),
        "conditions": normalized_conditions,
    }
    _expect(source, expected, location)


def _verify_measurement_record(
    record: dict[str, Any],
    case: dict[str, Any],
    series_definitions: dict[str, Any],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(
        set(record),
        {"id", "feature_id", "request_sha256", "series_sha256", "result"},
        f"{location}.keys",
    )
    _expect(record["id"], case["id"], f"{location}.id")
    _expect(record["feature_id"], case["feature_id"], f"{location}.feature_id")
    series, series_sha256 = _materialize_series(series_definitions[case["series"]])
    request = {"series": series, "measurement": case["measurement"], "extensions": {}}
    _expect(record["request_sha256"], _canonical_sha256(request), f"{location}.request_sha256")
    _expect(record["series_sha256"], series_sha256, f"{location}.series_sha256")

    expected = case["expected"]
    result = record["result"]
    _verify_common_result(
        result,
        operation="result.measure",
        request_id=case["request_id"],
        engineering_status=expected["engineering_status"],
        execution_status="completed",
        operation_profile="openada.operation/result.measure/v1alpha1",
        assertion_profile="openada.assertion/measurement.valid/v1alpha1",
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    measurement = result["data"]["measurement"]
    request_record = case["measurement"]
    kind = request_record["kind"]
    _expect(measurement["measurement_id"], request_record["measurement_id"], f"{location}.measurement_id")
    _expect(measurement["kind"], kind, f"{location}.kind")
    _expect(measurement["signal"], request_record["signal"], f"{location}.signal")
    _expect(measurement["status"], expected["measurement_status"], f"{location}.status")
    _expect(measurement["request_sha256"], _canonical_sha256(request_record), f"{location}.measurement.request_sha256")
    _expect_close(measurement["value"], expected["value"], f"{location}.value")
    _expect(measurement["unit"], expected["unit"], f"{location}.unit")
    _expect(measurement["sample_count"], expected["sample_count"], f"{location}.sample_count")
    expected_location = expected["location"]
    if expected_location is None:
        _expect(measurement["location"], None, f"{location}.location")
    else:
        _expect(measurement["location"]["unit"], expected_location["unit"], f"{location}.location.unit")
        _expect_close(measurement["location"]["value"], expected_location["value"], f"{location}.location.value")
    _expect(
        measurement["algorithm"],
        {
            "id": f"openada.algorithm/measurement.{kind.replace('_', '-')}/v1",
            "version": "1.0.0",
        },
        f"{location}.algorithm",
    )
    _expect(measurement["extensions"], {}, f"{location}.extensions")
    _verify_source(
        measurement["source"],
        definition=series_definitions[case["series"]],
        digest=series_sha256,
        location=f"{location}.source",
    )


def _verify_specification_record(
    record: dict[str, Any],
    case: dict[str, Any],
    measurement_records: dict[str, dict[str, Any]],
    *,
    result_schema: dict[str, Any],
    data_schema: dict[str, Any],
    location: str,
) -> None:
    _expect(
        set(record),
        {
            "id",
            "feature_ids",
            "measurement_case",
            "measurement_mutation",
            "request_sha256",
            "measurement_sha256",
            "specification_sha256",
            "result",
        },
        f"{location}.keys",
    )
    for field in ("id", "feature_ids", "measurement_case", "measurement_mutation"):
        _expect(record[field], case[field], f"{location}.{field}")
    source_measurement = measurement_records[case["measurement_case"]]["result"]["data"]["measurement"]
    measurement = _mutated_measurement(source_measurement, case["measurement_mutation"])
    specification = case["specification"]
    request = {"measurement": measurement, "specification": specification, "extensions": {}}
    _expect(record["request_sha256"], _canonical_sha256(request), f"{location}.request_sha256")
    _expect(record["measurement_sha256"], _canonical_sha256(measurement), f"{location}.measurement_sha256")
    _expect(record["specification_sha256"], _canonical_sha256(specification), f"{location}.specification_sha256")

    expected = case["expected"]
    result = record["result"]
    _verify_common_result(
        result,
        operation="specification.evaluate",
        request_id=case["request_id"],
        engineering_status=expected["engineering_status"],
        execution_status=expected["execution_status"],
        operation_profile="openada.operation/specification.evaluate/v1alpha1",
        assertion_profile="openada.assertion/specification.satisfied/v1alpha1",
        result_schema=result_schema,
        data_schema=data_schema,
        location=f"{location}.result",
    )
    _expect(_diagnostic_code(result), expected["diagnostic_code"], f"{location}.diagnostic_code")
    evaluation = result["data"]["evaluation"]
    _expect(evaluation["status"], expected["evaluation_status"], f"{location}.status")
    _expect(evaluation["conditions"], expected["conditions"], f"{location}.conditions")
    if expected["margin"] is None:
        _expect(evaluation["margin"], None, f"{location}.margin")
    else:
        _expect(evaluation["margin"]["unit"], expected["margin"]["unit"], f"{location}.margin.unit")
        _expect(evaluation["margin"]["relative_to"], expected["margin"]["relative_to"], f"{location}.margin.relative_to")
        _expect_close(evaluation["margin"]["value"], expected["margin"]["value"], f"{location}.margin.value")
    _expect(
        evaluation["algorithm"],
        {
            "id": "openada.algorithm/specification.closed-interval/v1",
            "version": "1.0.0",
        },
        f"{location}.algorithm",
    )
    _expect(evaluation["extensions"], {}, f"{location}.extensions")

    if case["measurement_mutation"] is not None:
        _expect(evaluation["specification_id"], None, f"{location}.specification_id")
        _expect(evaluation["measurement_id"], None, f"{location}.measurement_id")
        _expect(evaluation["source"], None, f"{location}.source")
        return

    normalized_specification = _normalized_specification(specification)
    _expect(evaluation["specification_id"], specification["specification_id"], f"{location}.specification_id")
    _expect(evaluation["measurement_id"], measurement["measurement_id"], f"{location}.measurement_id")
    _expect(
        evaluation["limits"],
        [
            {"kind": kind, **bound}
            for kind, bound in normalized_specification["limits"].items()
        ],
        f"{location}.limits",
    )
    measured = (
        {"value": measurement["value"], "unit": measurement["unit"]}
        if measurement["status"] == "measured"
        else None
    )
    _expect(evaluation["measured"], measured, f"{location}.measured")
    expected_source = {
        "measurement_sha256": _canonical_sha256(measurement),
        "measurement_source": measurement["source"],
        "specification_sha256": _canonical_sha256(normalized_specification),
        "specification": normalized_specification,
    }
    _expect(evaluation["source"], expected_source, f"{location}.source")


def verify_evidence(
    path: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(manifest)
    evidence = _read_json(path.resolve(), label="typed-evidence conformance run")
    _expect(
        set(evidence),
        {
            "schema",
            "conformance_id",
            "implementation",
            "fixture_sha256",
            "measurements",
            "specifications",
        },
        "evidence.keys",
    )
    _expect(evidence["schema"], "openada.typed-evidence-conformance-run/v0alpha1", "evidence.schema")
    _expect(evidence["conformance_id"], manifest["id"], "evidence.conformance_id")
    _expect(
        evidence["implementation"],
        {"id": manifest["implementation"]["id"], "version": manifest["implementation"]["version"]},
        "evidence.implementation",
    )
    _expect(evidence["fixture_sha256"], manifest["fixture"]["sha256"], "evidence.fixture_sha256")

    contracts = manifest["contracts"]
    result_schema = _contract_document(contracts["result_schema"], label="result schema")
    measurement_profile = _contract_document(contracts["measurement"], label="result.measure profile")
    specification_profile = _contract_document(contracts["specification"], label="specification.evaluate profile")

    measurement_cases = cases["measurement_cases"]
    _expect(
        [record.get("id") for record in evidence["measurements"]],
        [case["id"] for case in measurement_cases],
        "evidence.measurement_case_ids",
    )
    indexed_measurements: dict[str, dict[str, Any]] = {}
    for index, (record, case) in enumerate(zip(evidence["measurements"], measurement_cases)):
        _verify_measurement_record(
            record,
            case,
            cases["series"],
            result_schema=result_schema,
            data_schema=measurement_profile["normalized_result"]["data_schema"],
            location=f"evidence.measurements[{index}]",
        )
        indexed_measurements[case["id"]] = record

    specification_cases = cases["specification_cases"]
    _expect(
        [record.get("id") for record in evidence["specifications"]],
        [case["id"] for case in specification_cases],
        "evidence.specification_case_ids",
    )
    for index, (record, case) in enumerate(zip(evidence["specifications"], specification_cases)):
        _verify_specification_record(
            record,
            case,
            indexed_measurements,
            result_schema=result_schema,
            data_schema=specification_profile["normalized_result"]["data_schema"],
            location=f"evidence.specifications[{index}]",
        )

    return {
        "schema": "openada.typed-evidence-conformance-verification/v0alpha1",
        "status": "pass",
        "conformance_id": manifest["id"],
        "implementation": {
            "id": manifest["implementation"]["id"],
            "version": manifest["implementation"]["version"],
        },
        "features": {
            "measurement": list(MEASUREMENT_FEATURES),
            "specification": list(SPECIFICATION_FEATURES),
        },
        "verified_cases": {
            "measurement": len(measurement_cases),
            "specification": len(specification_cases),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args(argv)
    try:
        verification = verify_evidence(
            arguments.evidence_file,
            manifest_path=arguments.manifest.resolve(),
        )
    except ConformanceError as exc:
        print(f"typed-evidence conformance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
