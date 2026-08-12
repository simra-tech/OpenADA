#!/usr/bin/env python3
"""Run the pure testbench-oracle comparator conformance case in this checkout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"

# Bind execution to the public module in this checkout, never an ambient install.
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from openada.operations.testbench_oracle import (  # noqa: E402
    compare_testbench_observables,
)
from verify import (  # noqa: E402
    RUN_SCHEMA,
    _canonical_sha256,
    _fixture_sha256,
    load_fixtures,
    load_manifest,
    verify_evidence,
)


def run_suite(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path.resolve())
    fixtures = load_fixtures(manifest)
    comparison = compare_testbench_observables(
        fixtures["observed"],
        fixtures["oracle"],
        fixtures["tolerances"],
    )
    return {
        "schema": RUN_SCHEMA,
        "conformance_id": manifest["id"],
        "operation_profile": manifest["operation_profile"],
        "implementation_sha256": manifest["implementation"]["sha256"],
        "fixture_sha256": _fixture_sha256(manifest),
        "expected_sha256": manifest["fixtures"]["expected_comparison"]["sha256"],
        "comparison_sha256": _canonical_sha256(comparison),
        "comparison": comparison,
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
