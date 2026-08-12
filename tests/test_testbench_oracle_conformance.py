from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
BUNDLE = ROOT / "conformance" / "testbench-oracle-v1"


def _load_verifier():
    specification = importlib.util.spec_from_file_location(
        "testbench_oracle_conformance_verify",
        BUNDLE / "verify.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VERIFY = _load_verifier()


def _write(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_manifest_binds_all_ratified_rows_contracts_and_tampers() -> None:
    manifest = VERIFY.load_manifest()
    fixtures = VERIFY.load_fixtures(manifest)

    assert manifest["operation_profile"] == (
        "openada.operation/testbench.oracle.compare/v1alpha1"
    )
    assert tuple(manifest["metric_rows"]) == VERIFY.METRIC_ROWS
    assert len(manifest["metric_rows"]) == 12
    assert tuple(manifest["tamper_cases"]) == VERIFY.TAMPER_CASES
    assert len(fixtures["expected_comparison"]["metrics"]) == 12
    assert fixtures["expected_comparison"]["status"] == "PASS"


def test_standalone_verifier_source_has_no_openada_import() -> None:
    tree = ast.parse((BUNDLE / "verify.py").read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert "openada" not in imported_roots


def test_runner_record_passes_independent_verification_and_tampers_fail(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "testbench-oracle.json"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "run.py"),
            "--evidence-file",
            str(evidence),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    verification = VERIFY.verify_evidence(evidence)
    assert verification["status"] == "pass"
    assert verification["verified_metrics"] == 12
    assert verification["verified_tamper_cases"] == 4

    record = json.loads(evidence.read_text(encoding="utf-8"))
    stale_digest = deepcopy(record)
    stale_digest["comparison"]["metrics"][1]["value"] = 1e-6
    stale_path = tmp_path / "stale-comparison-digest.json"
    _write(stale_path, stale_digest)
    with pytest.raises(VERIFY.ConformanceError, match="comparison_sha256"):
        VERIFY.verify_evidence(stale_path)

    rebound = deepcopy(stale_digest)
    rebound["comparison_sha256"] = VERIFY._canonical_sha256(
        rebound["comparison"]
    )
    rebound_path = tmp_path / "rebound-tamper.json"
    _write(rebound_path, rebound)
    with pytest.raises(VERIFY.ConformanceError, match="evidence.comparison"):
        VERIFY.verify_evidence(rebound_path)


def test_runner_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "already-present.json"
    evidence.write_text("{}\n", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(BUNDLE / "run.py"),
            "--evidence-file",
            str(evidence),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode != 0
    assert evidence.read_text(encoding="utf-8") == "{}\n"
