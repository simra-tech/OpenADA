#!/usr/bin/env python3
"""Run the native closed testbench-plan conformance case in this checkout."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"

# Bind execution to the public package surface in this checkout.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from openada.operations import (  # noqa: E402
    HostNgspiceExecutor,
    execute_testbench_plan_ngspice,
    prepare_testbench_plan_ngspice,
    validate_testbench_plan,
)
from verify import (  # noqa: E402
    RUN_SCHEMA,
    _canonical_sha256,
    _fixture_sha256,
    _read_json,
    _sha256,
    load_manifest,
    verify_evidence,
)


def _binding(plan_document: dict[str, Any], dut_path: Path) -> dict[str, Any]:
    binding = deepcopy(plan_document["dut"])
    binding["artifact"] = str(dut_path.resolve())
    binding["sha256"] = _sha256(dut_path)
    return binding


def _compiled_conditions(compilation: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": condition.condition_id,
            "stage_id": condition.stage_id,
            "point_id": condition.point_id,
            "analysis_kind": condition.receipt["analysis"]["kind"],
            "condition_sha256": condition.condition_sha256,
            "deck_sha256": condition.deck_sha256,
        }
        for condition in compilation.conditions
    ]


def run_suite(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    ngspice_binary: str | Path = "ngspice",
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path.resolve())
    plan_path = REPOSITORY_ROOT / manifest["fixtures"]["plan"]["repository_path"]
    dut_path = REPOSITORY_ROOT / manifest["fixtures"]["dut"]["repository_path"]
    plan_document = _read_json(plan_path, label="closed RC plan")
    dut_binding = _binding(plan_document, dut_path)

    prepared, issues = validate_testbench_plan(plan_path, dut_binding=dut_binding)
    if prepared is None or issues:
        details = [issue.record() for issue in issues]
        raise RuntimeError(f"closed plan validation refused: {details!r}")

    # Exercise the schema's reject-on-unknown-field boundary with the public
    # validator rather than merely trusting JSON Schema metadata.
    tampered_plan = deepcopy(plan_document)
    tampered_plan["undeclared_source"] = {"kind": "raw_spice"}
    rejected, closed_issues = validate_testbench_plan(
        tampered_plan, dut_binding=dut_binding
    )
    closed_codes = sorted({issue.code for issue in closed_issues})
    if rejected is not None or "testbench_plan.document.unknown_field" not in closed_codes:
        raise RuntimeError("validator accepted an undeclared root field")

    first = prepare_testbench_plan_ngspice(prepared, corner="tt")
    second = prepare_testbench_plan_ngspice(prepared, corner="tt")
    first_conditions = _compiled_conditions(first)
    second_conditions = _compiled_conditions(second)
    if first.receipt != second.receipt or first_conditions != second_conditions:
        raise RuntimeError("compiler output changed across identical preparations")
    for left, right in zip(first.conditions, second.conditions):
        if left.deck_bytes != right.deck_bytes:
            raise RuntimeError(f"deck bytes changed for {left.condition_id!r}")

    executor = HostNgspiceExecutor(ngspice_binary)
    if "ngspice-" not in executor.simulator_identity.casefold():
        raise RuntimeError(
            "the native executor did not report an ngspice identity: "
            f"{executor.simulator_identity!r}"
        )
    run = execute_testbench_plan_ngspice(
        prepared,
        corner="tt",
        executor=executor,
        timeout_s=30,
    )
    receipt = dict(run.receipt)
    observables = dict(run.observables)
    return {
        "schema": RUN_SCHEMA,
        "conformance_id": manifest["id"],
        "fixture_sha256": _fixture_sha256(manifest),
        "validation": {
            "status": "PASS",
            "plan_id": prepared.identifier,
            "issue_count": 0,
            "plan_raw_sha256": prepared.raw_sha256,
            "plan_canonical_sha256": prepared.canonical_sha256,
            "dut_binding_canonical_sha256": prepared.dut_binding_canonical_sha256,
            "closed_field_refusal_codes": closed_codes,
        },
        "compilation": {
            "status": "PASS",
            "compiler_id": first.receipt["compiler_id"],
            "deterministic": True,
            "receipt_sha256": _canonical_sha256(first.receipt),
            "repeat_receipt_sha256": _canonical_sha256(second.receipt),
            "receipt": first.receipt,
            "conditions": first_conditions,
        },
        "execution": {
            "status": "PASS" if not run.refusals else "FAIL",
            "receipt_sha256": _canonical_sha256(receipt),
            "observables_sha256": _canonical_sha256(observables),
            "receipt": receipt,
            "observables": observables,
        },
    }


def _write_new(path: Path, document: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"evidence parent directory does not exist: {path.parent}")
    encoded = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    with path.open("xb") as handle:
        handle.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--ngspice", default="ngspice")
    arguments = parser.parse_args(argv)

    evidence_path = arguments.evidence_file.resolve()
    record = run_suite(
        arguments.manifest.resolve(), ngspice_binary=arguments.ngspice
    )
    _write_new(evidence_path, record)
    verification = verify_evidence(
        evidence_path, manifest_path=arguments.manifest.resolve()
    )
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
