#!/usr/bin/env python3
"""Run the model-free typed-evidence conformance cases in this checkout."""

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

# The bundle verifies the implementation in this exact checkout.  It does not
# resolve an ambient installed package with potentially different profiles.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from openada.operations import (  # noqa: E402
    evaluate_specification,
    measure_result,
    normalized_series_sha256,
)
from openada.operations.result_measure import (  # noqa: E402
    IMPLEMENTATION_ID,
    IMPLEMENTATION_VERSION,
)
from verify import load_cases, load_manifest, verify_evidence  # noqa: E402


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _materialize_series(definition: dict[str, Any]) -> dict[str, Any]:
    series = deepcopy(definition)
    digest = normalized_series_sha256(
        axis=series["axis"],
        signals=series["signals"],
        conditions=series["conditions"],
    )
    series["source"]["artifact_sha256"] = digest
    return series


def _measurement_input(
    measurement: dict[str, Any],
    mutation: str | None,
) -> dict[str, Any]:
    selected = deepcopy(measurement)
    if mutation is None:
        return selected
    if mutation == "replace-first-condition-with-125":
        selected["source"]["conditions"][0]["value"] = 125.0
        return selected
    raise ValueError(f"unsupported fixture measurement mutation {mutation!r}")


def run_suite(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(manifest)

    measurement_records: list[dict[str, Any]] = []
    measurement_results: dict[str, dict[str, Any]] = {}
    for case in cases["measurement_cases"]:
        series = _materialize_series(cases["series"][case["series"]])
        request = {
            "series": series,
            "measurement": case["measurement"],
            "extensions": {},
        }
        measured = measure_result(
            series,
            case["measurement"],
            request_id=case["request_id"],
        )
        measurement_results[case["id"]] = measured
        measurement_records.append(
            {
                "id": case["id"],
                "feature_id": case["feature_id"],
                "request_sha256": _canonical_sha256(request),
                "series_sha256": series["source"]["artifact_sha256"],
                "result": measured,
            }
        )

    specification_records: list[dict[str, Any]] = []
    for case in cases["specification_cases"]:
        producer = measurement_results[case["measurement_case"]]
        measurement = _measurement_input(
            producer["data"]["measurement"],
            case["measurement_mutation"],
        )
        specification = deepcopy(case["specification"])
        request = {
            "measurement": measurement,
            "specification": specification,
            "extensions": {},
        }
        evaluated = evaluate_specification(
            measurement,
            specification,
            request_id=case["request_id"],
        )
        specification_records.append(
            {
                "id": case["id"],
                "feature_ids": case["feature_ids"],
                "measurement_case": case["measurement_case"],
                "measurement_mutation": case["measurement_mutation"],
                "request_sha256": _canonical_sha256(request),
                "measurement_sha256": _canonical_sha256(measurement),
                "specification_sha256": _canonical_sha256(specification),
                "result": evaluated,
            }
        )

    return {
        "schema": "openada.typed-evidence-conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "implementation": {
            "id": IMPLEMENTATION_ID,
            "version": IMPLEMENTATION_VERSION,
        },
        "fixture_sha256": manifest["fixture"]["sha256"],
        "measurements": measurement_records,
        "specifications": specification_records,
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
    ) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-file", type=Path, required=True)
    arguments = parser.parse_args(argv)

    evidence_path = arguments.evidence_file.resolve()
    record = run_suite(arguments.manifest.resolve())
    _write_new(evidence_path, record)
    verification = verify_evidence(
        evidence_path,
        manifest_path=arguments.manifest.resolve(),
    )
    print(json.dumps(verification, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
