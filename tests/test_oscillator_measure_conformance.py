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
BUNDLE = ROOT / "conformance" / "oscillator-measure-v0alpha1"


def _load_verifier():
    specification = importlib.util.spec_from_file_location(
        "oscillator_measure_conformance_verify",
        BUNDLE / "verify.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VERIFY = _load_verifier()


def test_manifest_binds_profile_features_and_deterministic_cases() -> None:
    manifest = VERIFY.load_manifest()
    cases = VERIFY.load_cases(manifest)

    assert manifest["id"] == "oscillator-measurement-primitives-v0alpha1"
    assert manifest["implementation"] == {
        "id": "org.openada.kernel.oscillator-evidence",
        "runtime": "python",
        "version": "1.0.0",
    }
    assert len(manifest["features"]) == 7
    assert [len(cases[key]) for key in (
        "transient_cases",
        "grid_cases",
        "shift_cases",
        "receipt_rejection_cases",
    )] == [5, 3, 2, 1]


def test_runner_passes_independent_verification_and_tampering_fails(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "oscillator-evidence.json"
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
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    verification = VERIFY.verify_evidence(evidence)
    assert verification["status"] == "pass"
    assert verification["verified_cases"] == {
        "transient": 5,
        "grid": 3,
        "shift": 2,
        "receipt_rejection": 1,
    }

    record = json.loads(evidence.read_text(encoding="utf-8"))
    mutations = []

    changed_value = deepcopy(record)
    changed_value["transients"][0]["result"]["data"]["transient"]["frequency"]["value"] += 1.0
    mutations.append(("value", changed_value, r"transient\.frequency\.value"))

    changed_status = deepcopy(record)
    changed_status["transients"][0]["result"]["data"]["transient"]["status"] = "collapsed"
    mutations.append(("status", changed_status, r"transient\.status"))

    changed_window = deepcopy(record)
    changed_window["transients"][0]["result"]["data"]["transient"]["window"]["window_sha256"] = "0" * 64
    mutations.append(("window", changed_window, r"window\.window_sha256"))

    changed_receipt = deepcopy(record)
    changed_receipt["transients"][0]["result"]["data"]["receipt"]["sha256"] = "0" * 64
    mutations.append(("receipt", changed_receipt, r"receipt\.sha256"))

    for name, tampered, pattern in mutations:
        tampered_path = tmp_path / f"oscillator-{name}-tampered.json"
        tampered_path.write_text(
            json.dumps(tampered, allow_nan=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(VERIFY.ConformanceError, match=pattern):
            VERIFY.verify_evidence(tampered_path)

    rejection = record["receipt_rejections"][0]
    assert rejection["result"]["execution"]["status"] == "invalid_request"
    assert rejection["result"]["diagnostics"][0]["code"] == (
        "oscillator.receipt.digest_mismatch"
    )


@pytest.mark.parametrize(
    ("encoded", "pattern"),
    [
        ('{"schema":"first","schema":"second"}\n', "duplicate JSON object key"),
        ('{"value":NaN}\n', "non-finite JSON constant"),
    ],
)
def test_verifier_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path: Path,
    encoded: str,
    pattern: str,
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text(encoded, encoding="utf-8")

    with pytest.raises(VERIFY.ConformanceError, match=pattern):
        VERIFY._read_json(malformed, label="malformed evidence")
