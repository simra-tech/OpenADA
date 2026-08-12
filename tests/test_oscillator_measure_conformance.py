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
        "request_rejection_cases",
    )] == [5, 3, 2, 4, 2]


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
        "receipt_rejection": 4,
        "request_rejection": 2,
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

    rejection_codes = [
        rejection["result"]["diagnostics"][0]["code"]
        for rejection in record["receipt_rejections"]
    ]
    assert rejection_codes == [
        "oscillator.receipt.digest_mismatch",
        "oscillator.receipt.invalid",
        "oscillator.receipt.invalid",
        "oscillator.receipt.invalid",
    ]
    assert all(
        rejection["result"]["execution"]["status"] == "invalid_request"
        for rejection in record["receipt_rejections"]
    )
    for rejection in record["receipt_rejections"][1:]:
        forged = rejection["receipts"][0]
        assert forged["sha256"] == VERIFY._canonical_sha256(
            {key: value for key, value in forged.items() if key != "sha256"}
        )
    assert [
        rejection["result"]["diagnostics"][0]["code"]
        for rejection in record["request_rejections"]
    ] == ["oscillator.quality.invalid", "oscillator.quality.invalid"]

    original_receipt = record["transients"][0]["result"]["data"]["receipt"]

    def rehash(receipt: dict) -> None:
        receipt["sha256"] = VERIFY._canonical_sha256(
            {key: value for key, value in receipt.items() if key != "sha256"}
        )

    receipt_mutations = []
    changed_producer = deepcopy(original_receipt)
    changed_producer["producer"]["implementation_id"] = "org.example.forged"
    rehash(changed_producer)
    receipt_mutations.append((changed_producer, r"producer"))

    changed_request = deepcopy(original_receipt)
    changed_request["request"]["quality"]["maximum_period_relative_deviation"] = 0.003
    rehash(changed_request)
    receipt_mutations.append((changed_request, r"request_sha256"))

    changed_method = deepcopy(original_receipt)
    changed_method["method_sha256"] = "0" * 64
    rehash(changed_method)
    receipt_mutations.append((changed_method, r"method_sha256"))

    changed_startup = deepcopy(original_receipt)
    changed_startup["startup"]["started_at"] = None
    changed_startup["startup"]["time"] = None
    rehash(changed_startup)
    receipt_mutations.append((changed_startup, r"startup"))

    changed_quality = deepcopy(original_receipt)
    changed_quality["quality"]["status"] = "fail"
    rehash(changed_quality)
    receipt_mutations.append((changed_quality, r"quality\.status"))

    for forged, pattern in receipt_mutations:
        with pytest.raises(VERIFY.ConformanceError, match=pattern):
            VERIFY._verify_receipt(forged, original_receipt, "forged.receipt")


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
