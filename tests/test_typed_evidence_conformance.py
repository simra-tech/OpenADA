from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
BUNDLE = ROOT / "conformance" / "typed-evidence-v0alpha1"


def _load_verifier():
    specification = importlib.util.spec_from_file_location(
        "typed_evidence_conformance_verify",
        BUNDLE / "verify.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VERIFY = _load_verifier()


def test_manifest_binds_every_typed_evidence_feature() -> None:
    manifest = VERIFY.load_manifest()
    cases = VERIFY.load_cases(manifest)

    assert manifest["id"] == "typed-evidence-measurement-specification-v0alpha1"
    assert len(manifest["features"]["measurement"]) == 9
    assert len(manifest["features"]["specification"]) == 2
    assert len(cases["measurement_cases"]) == 10
    assert len(cases["specification_cases"]) == 7


def test_runner_record_passes_independent_verification_and_tamper_fails(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "typed-evidence.json"
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
    assert verification["verified_cases"] == {
        "measurement": 10,
        "specification": 7,
    }

    record = json.loads(evidence.read_text(encoding="utf-8"))
    tampered = deepcopy(record)
    tampered["measurements"][2]["result"]["data"]["measurement"]["value"] = 0.5
    tampered_path = tmp_path / "typed-evidence-tampered.json"
    tampered_path.write_text(
        json.dumps(tampered, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(VERIFY.ConformanceError, match=r"measurements\[2\].value"):
        VERIFY.verify_evidence(tampered_path)
