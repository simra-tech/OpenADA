#!/usr/bin/env python3
"""Independently verify native closed testbench-plan conformance evidence."""

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
MAX_JSON_BYTES = 8 * 1024 * 1024
MANIFEST_SCHEMA = "simra.testbench-plan-conformance-manifest/v1"
RUN_SCHEMA = "simra.testbench-plan-conformance-run/v1"
CONFORMANCE_ID = "testbench-plan-v1"
TAMPER_CASES = (
    "plan-binding",
    "compiled-deck",
    "waveform-receipt",
    "observable-lineage",
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ConformanceError(RuntimeError):
    """A contract, fixture, receipt, lineage, or tamper case is inconsistent."""


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
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


def _bound_file(record: object, *, label: str, extras: set[str]) -> Path:
    if not isinstance(record, dict):
        raise ConformanceError(f"{label} must be an object")
    _expect(set(record), {"repository_path", "sha256"} | extras, f"{label}.keys")
    path = _repository_path(record["repository_path"], label=f"{label}.repository_path")
    _require_regular(path, label=label)
    _expect(_sha256(path), record["sha256"], f"{label}.sha256")
    return path


def _bound_json(record: object, *, label: str, extras: set[str]) -> dict[str, Any]:
    path = _bound_file(record, label=label, extras=extras)
    return _read_json(path, label=label)


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
    manifest = _read_json(path.resolve(), label="testbench-plan manifest")
    _expect(
        set(manifest),
        {
            "schema",
            "id",
            "public_api",
            "implementations",
            "contracts",
            "fixtures",
            "tamper_cases",
            "policy",
        },
        "manifest.keys",
    )
    _expect(manifest["schema"], MANIFEST_SCHEMA, "manifest.schema")
    _expect(manifest["id"], CONFORMANCE_ID, "manifest.id")
    _expect(
        manifest["public_api"],
        {
            "module": "openada.operations",
            "validator": "validate_testbench_plan",
            "compiler": "prepare_testbench_plan_ngspice",
            "runner": "execute_testbench_plan_ngspice",
        },
        "manifest.public_api",
    )
    _expect(set(manifest["implementations"]), {"validator", "compiler", "runner"}, "manifest.implementations")
    for name, record in manifest["implementations"].items():
        _bound_file(record, label=f"{name} implementation", extras=set())
    _expect(set(manifest["contracts"]), {"plan", "observables"}, "manifest.contracts")
    expected_contracts = {
        "plan": "simra.testbench-plan/v1",
        "observables": "simra.testbench-observables/v1",
    }
    for name, identifier in expected_contracts.items():
        record = manifest["contracts"][name]
        _expect(record.get("id"), identifier, f"manifest.contracts.{name}.id")
        schema = _bound_json(record, label=f"{name} schema", extras={"id"})
        Draft202012Validator.check_schema(schema)
    _expect(
        set(manifest["fixtures"]),
        {"plan", "dut", "expected", "tamper_cases"},
        "manifest.fixtures",
    )
    for name, record in manifest["fixtures"].items():
        _expect(record.get("license"), "MIT", f"manifest.fixtures.{name}.license")
        if name == "dut":
            _bound_file(record, label=f"{name} fixture", extras={"license"})
        else:
            _bound_json(record, label=f"{name} fixture", extras={"license"})
    _expect(list(manifest["tamper_cases"]), list(TAMPER_CASES), "manifest.tamper_cases")
    _expect(
        manifest["policy"],
        {
            "native_eda": "host-ngspice",
            "network": "none",
            "dut_binding": "runtime-full-override",
            "input_mode": "read-only-fixtures",
            "evidence_mode": "new-file-only",
            "maximum_evidence_bytes": MAX_JSON_BYTES,
        },
        "manifest.policy",
    )
    return manifest


def _fixtures(manifest: dict[str, Any]) -> dict[str, Any]:
    fixtures = manifest["fixtures"]
    plan = _bound_json(fixtures["plan"], label="plan fixture", extras={"license"})
    expected = _bound_json(fixtures["expected"], label="expected fixture", extras={"license"})
    tampers = _bound_json(fixtures["tamper_cases"], label="tamper fixture", extras={"license"})
    plan_schema = _bound_json(manifest["contracts"]["plan"], label="plan schema", extras={"id"})
    _validate_schema(plan, plan_schema, label="plan")
    _expect(plan.get("schema"), "simra.testbench-plan/v1", "plan.schema")
    _expect(plan["dut"]["sha256"], fixtures["dut"]["sha256"], "plan.dut.sha256")
    _expect(expected.get("schema"), "simra.testbench-plan-conformance-expected/v1", "expected.schema")
    _expect(tampers.get("schema"), "simra.testbench-plan-conformance-tampers/v1", "tampers.schema")
    _expect([case["id"] for case in tampers.get("cases", [])], list(TAMPER_CASES), "tampers.ids")
    for index, case in enumerate(tampers["cases"]):
        _expect(set(case), {"id", "path", "replacement", "expected_message"}, f"tampers[{index}].keys")
        if not isinstance(case["path"], list) or not case["path"]:
            raise ConformanceError(f"tampers[{index}].path must be nonempty")
        if not isinstance(case["expected_message"], str) or not case["expected_message"]:
            raise ConformanceError(f"tampers[{index}].expected_message must be nonempty")
    return {"plan": plan, "expected": expected, "tampers": tampers}


def _fixture_sha256(manifest: dict[str, Any]) -> str:
    return _canonical_sha256(
        {name: record["sha256"] for name, record in manifest["fixtures"].items()}
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _condition_inventory(expected: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = expected.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ConformanceError("expected.conditions must be nonempty")
    identifiers = [item.get("id") for item in conditions if isinstance(item, dict)]
    if len(identifiers) != len(conditions) or len(identifiers) != len(set(identifiers)):
        raise ConformanceError("expected.conditions identifiers must be unique")
    for index, item in enumerate(conditions):
        _expect(
            set(item),
            {"id", "stage_id", "point_id", "analysis_kind", "condition_sha256", "deck_sha256"},
            f"expected.conditions[{index}].keys",
        )
        if not _is_digest(item["condition_sha256"]) or not _is_digest(item["deck_sha256"]):
            raise ConformanceError(f"expected.conditions[{index}] has an invalid digest")
    return conditions


def _verify_record(
    record: dict[str, Any], manifest: dict[str, Any], fixtures: dict[str, Any]
) -> None:
    _expect(
        set(record),
        {"schema", "conformance_id", "fixture_sha256", "validation", "compilation", "execution"},
        "evidence.keys",
    )
    _expect(record["schema"], RUN_SCHEMA, "evidence.schema")
    _expect(record["conformance_id"], CONFORMANCE_ID, "evidence.conformance_id")
    _expect(record["fixture_sha256"], _fixture_sha256(manifest), "evidence.fixture_sha256")
    expected = fixtures["expected"]
    expected_conditions = _condition_inventory(expected)

    validation = record["validation"]
    _expect(
        set(validation),
        {"status", "plan_id", "issue_count", "plan_raw_sha256", "plan_canonical_sha256", "dut_binding_canonical_sha256", "closed_field_refusal_codes"},
        "validation.keys",
    )
    _expect(validation["status"], "PASS", "validation.status")
    _expect(validation["plan_id"], expected["plan_id"], "validation.plan_id")
    _expect(validation["issue_count"], 0, "validation.issue_count")
    _expect(validation["plan_raw_sha256"], expected["plan_raw_sha256"], "validation.plan_raw_sha256")
    _expect(validation["plan_canonical_sha256"], expected["plan_canonical_sha256"], "validation.plan_canonical_sha256")
    _expect(validation["plan_raw_sha256"], manifest["fixtures"]["plan"]["sha256"], "validation.plan_raw_fixture_sha256")
    if not _is_digest(validation["dut_binding_canonical_sha256"]):
        raise ConformanceError("validation.dut_binding_canonical_sha256 is not a digest")
    if expected["closed_field_refusal_code"] not in validation["closed_field_refusal_codes"]:
        raise ConformanceError("validation.closed_field_refusal_codes lacks unknown-field refusal")

    compilation = record["compilation"]
    _expect(
        set(compilation),
        {"status", "compiler_id", "deterministic", "receipt_sha256", "repeat_receipt_sha256", "receipt", "conditions"},
        "compilation.keys",
    )
    _expect(compilation["status"], "PASS", "compilation.status")
    _expect(compilation["compiler_id"], expected["compiler_id"], "compilation.compiler_id")
    _expect(compilation["deterministic"], True, "compilation.deterministic")
    _expect(compilation["receipt_sha256"], _canonical_sha256(compilation["receipt"]), "compilation.receipt_sha256")
    _expect(compilation["repeat_receipt_sha256"], compilation["receipt_sha256"], "compilation.repeat_receipt_sha256")
    _expect(compilation["conditions"], expected_conditions, "compilation.conditions")
    compile_receipt = compilation["receipt"]
    _expect(compile_receipt.get("compiler_id"), expected["compiler_id"], "compilation.receipt.compiler_id")
    _expect(compile_receipt.get("plan", {}).get("raw_sha256"), expected["plan_raw_sha256"], "compilation.receipt.plan.raw_sha256")
    _expect(compile_receipt.get("plan", {}).get("canonical_sha256"), expected["plan_canonical_sha256"], "compilation.receipt.plan.canonical_sha256")
    _expect(compile_receipt.get("dut", {}).get("raw_sha256"), manifest["fixtures"]["dut"]["sha256"], "compilation.receipt.dut.raw_sha256")
    _expect(compile_receipt.get("dut", {}).get("canonical_sha256"), expected["sealed_dut_sha256"], "compilation.receipt.dut.canonical_sha256")
    _expect(compile_receipt.get("corner", {}).get("id"), expected["corner"], "compilation.receipt.corner.id")
    _expect(compile_receipt.get("corner", {}).get("canonical_sha256"), expected["corner_canonical_sha256"], "compilation.receipt.corner.canonical_sha256")
    receipt_conditions = compile_receipt.get("conditions", [])
    projected = [
        {
            "id": item.get("condition_id"),
            "stage_id": item.get("stage_id"),
            "point_id": item.get("point_id"),
            "analysis_kind": item.get("analysis", {}).get("kind"),
            "condition_sha256": item.get("condition_sha256"),
            "deck_sha256": item.get("deck", {}).get("raw_sha256"),
        }
        for item in receipt_conditions
    ]
    _expect(projected, expected_conditions, "compilation.receipt.conditions")

    execution = record["execution"]
    _expect(set(execution), {"status", "receipt_sha256", "observables_sha256", "receipt", "observables"}, "execution.keys")
    _expect(execution["status"], "PASS", "execution.status")
    _expect(execution["receipt_sha256"], _canonical_sha256(execution["receipt"]), "execution.receipt_sha256")
    _expect(execution["observables_sha256"], _canonical_sha256(execution["observables"]), "execution.observables_sha256")
    receipt = execution["receipt"]
    observables = execution["observables"]
    _expect(receipt.get("runner_id"), expected["runner_id"], "execution.receipt.runner_id")
    _expect(receipt.get("plan_sha256"), expected["plan_canonical_sha256"], "execution.receipt.plan_sha256")
    _expect(receipt.get("dut_sha256"), manifest["fixtures"]["dut"]["sha256"], "execution.receipt.dut_sha256")
    _expect(receipt.get("corner"), expected["corner"], "execution.receipt.corner")
    count = len(expected_conditions)
    _expect(receipt.get("condition_inventory_complete"), True, "execution.receipt.condition_inventory_complete")
    for key in ("expected_condition_count", "attempted_condition_count", "simulator_invocation_count", "completed_condition_count"):
        _expect(receipt.get(key), count, f"execution.receipt.{key}")
    _expect(receipt.get("not_executed_condition_count"), 0, "execution.receipt.not_executed_condition_count")
    _expect(receipt.get("refusals"), [], "execution.receipt.refusals")
    identities = receipt.get("simulator_identities")
    if not isinstance(identities, list) or len(identities) != 1 or expected["simulator_identity_contains"] not in identities[0]:
        raise ConformanceError("execution.receipt.simulator_identities is not an ngspice identity")
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != count:
        raise ConformanceError("execution.receipt.attempts does not cover every condition")
    attempts_by_id = {item.get("condition_id"): item for item in attempts if isinstance(item, dict)}
    _expect(set(attempts_by_id), {item["id"] for item in expected_conditions}, "execution.receipt.attempt_ids")
    for condition in expected_conditions:
        attempt = attempts_by_id[condition["id"]]
        _expect(attempt.get("stage_id"), condition["stage_id"], f"attempt[{condition['id']}].stage_id")
        _expect(attempt.get("point_id"), condition["point_id"], f"attempt[{condition['id']}].point_id")
        _expect(attempt.get("condition_sha256"), condition["condition_sha256"], f"attempt[{condition['id']}].condition_sha256")
        _expect(attempt.get("compiled_deck_sha256"), condition["deck_sha256"], f"attempt[{condition['id']}].compiled_deck_sha256")
        _expect(attempt.get("simulator_invoked"), True, f"attempt[{condition['id']}].simulator_invoked")
        _expect(attempt.get("returncode"), 0, f"attempt[{condition['id']}].returncode")
        _expect(attempt.get("status"), "completed", f"attempt[{condition['id']}].status")
        _expect(attempt.get("reason"), "ok", f"attempt[{condition['id']}].reason")
        if not _is_digest(attempt.get("waveform_sha256")) or attempt["waveform_sha256"] == EMPTY_SHA256:
            raise ConformanceError(f"attempt[{condition['id']}].waveform_sha256 is empty or invalid")

    observables_schema = _bound_json(manifest["contracts"]["observables"], label="observables schema", extras={"id"})
    _validate_schema(observables, observables_schema, label="execution.observables")
    _expect(observables.get("plan_sha256"), expected["plan_canonical_sha256"], "execution.observables.plan_sha256")
    _expect(observables.get("dut_sha256"), manifest["fixtures"]["dut"]["sha256"], "execution.observables.dut_sha256")
    _expect(observables.get("corner"), expected["corner"], "execution.observables.corner")
    _expect(sorted(observables.get("observables", {})), sorted(expected["observables"]), "execution.observables.names")
    _expect(observables.get("validity"), expected["validity"], "execution.observables.validity")
    dc_curve = observables["observables"]["dc_curve"]
    _expect(dc_curve.get("x"), [0.0, 0.6, 1.2], "execution.observables.dc_curve.x")
    dc_y = dc_curve.get("y")
    if not isinstance(dc_y, list) or len(dc_y) != 3 or any(not math.isclose(float(a), b, rel_tol=1e-12, abs_tol=1e-15) for a, b in zip(dc_y, (0.0, 0.6, 1.2))):
        raise ConformanceError("execution.observables.dc_curve.y does not match the synthetic RC response")
    pulse_curve = observables["observables"]["pulse_curve"]
    if not isinstance(pulse_curve.get("x"), list) or not isinstance(pulse_curve.get("y"), list) or len(pulse_curve["x"]) < 20 or len(pulse_curve["x"]) != len(pulse_curve["y"]):
        raise ConformanceError("execution.observables.pulse_curve is incomplete")
    condition_metadata = observables.get("metadata", {}).get("conditions", [])
    metadata_by_id = {item.get("id"): item for item in condition_metadata if isinstance(item, dict)}
    _expect(set(metadata_by_id), set(attempts_by_id), "execution.observables.metadata.condition_ids")
    for identifier, item in metadata_by_id.items():
        attempt = attempts_by_id[identifier]
        _expect(item.get("receipt", {}).get("compiled_deck_sha256"), attempt["compiled_deck_sha256"], f"metadata[{identifier}].compiled_deck_sha256")
        _expect(item.get("receipt", {}).get("waveform_sha256"), attempt["waveform_sha256"], f"metadata[{identifier}].waveform_sha256")
    lineage = observables.get("metadata", {}).get("lineage", [])
    lineage_by_name = {item.get("observable"): item.get("condition_ids") for item in lineage if isinstance(item, dict)}
    _expect(set(lineage_by_name), set(expected["observables"]), "execution.observables.lineage.names")
    expected_dc_ids = [item["id"] for item in expected_conditions if item["point_id"] == "dc_response"]
    expected_pulse_ids = [item["id"] for item in expected_conditions if item["point_id"] == "pulse_response"]
    _expect(lineage_by_name["dc_curve"], expected_dc_ids, "execution.observables.lineage.dc_curve")
    _expect(lineage_by_name["pulse_curve"], expected_pulse_ids, "execution.observables.lineage.pulse_curve")
    _expect(receipt.get("observable_envelope_sha256"), execution["observables_sha256"], "execution.receipt.observable_envelope_sha256")


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


def _verify_tamper_cases(record: dict[str, Any], manifest: dict[str, Any], fixtures: dict[str, Any]) -> None:
    for case in fixtures["tampers"]["cases"]:
        tampered = deepcopy(record)
        _replace_path(tampered, case["path"], case["replacement"])
        try:
            _verify_record(tampered, manifest, fixtures)
        except ConformanceError as exc:
            if case["expected_message"] not in str(exc):
                raise ConformanceError(f"tamper case {case['id']!r} failed at the wrong boundary: {exc}") from exc
        else:
            raise ConformanceError(f"tamper case {case['id']!r} was not detected")


def verify_evidence(evidence_path: Path, *, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path.resolve())
    fixtures = _fixtures(manifest)
    record = _read_json(evidence_path.resolve(), label="testbench-plan evidence")
    _verify_record(record, manifest, fixtures)
    _verify_tamper_cases(record, manifest, fixtures)
    return {
        "schema": "simra.testbench-plan-conformance-verification/v1",
        "conformance_id": CONFORMANCE_ID,
        "status": "pass",
        "verified_conditions": len(fixtures["expected"]["conditions"]),
        "verified_tamper_cases": len(TAMPER_CASES),
        "execution_receipt_sha256": record["execution"]["receipt_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_file", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args(argv)
    verification = verify_evidence(arguments.evidence_file, manifest_path=arguments.manifest)
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
