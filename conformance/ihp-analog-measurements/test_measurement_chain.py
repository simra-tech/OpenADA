"""Focused static, independent, and opt-in native measurement-chain tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common  # noqa: E402
import run  # noqa: E402
import verify  # noqa: E402


def _coverage_module():
    path = ROOT / "tools/verify_semantic_coverage.py"
    spec = importlib.util.spec_from_file_location(
        "_openada_measurement_chain_test_coverage", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_has_exact_honest_surface_and_step_local_covers() -> None:
    manifest = common.load_manifest(HERE / "manifest.json")
    semantic_steps = [
        step for step in manifest["steps"] if step["kind"] == "semantic-command"
    ]
    exercised = {row for step in semantic_steps for row in step["covers"]}
    measurement_rows = {
        row
        for row in manifest["covers"]
        if "result.spectral.measure" in row
        or "result.transfer.measure" in row
        or "cli.spectral" in row
        or "cli.transfer" in row
    }

    assert len(manifest["covers"]) == 47
    assert len(measurement_rows) == 30
    assert set(manifest["covers"]) == exercised
    assert all(step["covers"] for step in semantic_steps)
    assert len(manifest["negative_replays"]) == 13
    assert len(manifest["tamper_replays"]) == 5
    assert all(not step["native_execution"] for step in semantic_steps if step["id"].startswith(("transfer-", "spectral-", "extract-")))
    assert all(step["native_execution"] for step in semantic_steps if step["id"].startswith(("netlist-", "provider-")))


def test_manifest_passes_positive_coverage_and_every_row_has_adversarial_evidence() -> None:
    coverage = _coverage_module()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    report = coverage.build_report(
        coverage.DEFAULT_CATALOG.resolve(),
        coverage.DEFAULT_CHAIN_INDEX.resolve(),
        mode="audit",
    )
    rows = {row["row_id"]: row for row in report["rows"]}
    assert coverage._positive_coverage_issues(
        manifest, rows, label="ihp-analog-measurements"
    ) == []
    negative = {
        row for replay in manifest["negative_replays"] for row in replay["covers"]
    }
    tamper = {
        row for replay in manifest["tamper_replays"] for row in replay["covers"]
    }
    assert negative == set(manifest["covers"])
    assert tamper == set(manifest["covers"])


def test_provisional_receipt_is_independently_reproducible() -> None:
    report = verify.verify_evidence(HERE / "semantic-artifacts")
    assert report["status"] == "pass"
    assert report["oracle"]["transfer"]["metrics"]["low_frequency_gain_db"] == pytest.approx(70.1197413867)
    assert report["oracle"]["spectral"]["metrics"]["snr"] == pytest.approx(93.2858420431)
    assert report["agent_evidence"]["status"] == "proceed-to-requirements-and-pvt-review"
    assert report["agent_evidence"]["standards"]["conformance_claim"] is False


def test_provisional_chain_run_passes_temporary_agent_ready_index(tmp_path: Path) -> None:
    validation = run._validate_temp_index(
        HERE / "manifest.json", HERE / "semantic-chain-run.json"
    )
    assert validation["chain_rows"] == 47
    assert validation["chain_agent_ready_rows"] == 47


def test_retained_tamper_receipts_are_unique_and_pass() -> None:
    digests = set()
    for replay_id in verify.TAMPER_IDS:
        path = HERE / "semantic-artifacts" / "tamper" / f"{replay_id}.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        assert receipt["id"] == replay_id
        assert receipt["status"] == "pass"
        digests.add(common.sha256_file(path))
    assert len(digests) == len(verify.TAMPER_IDS)


@pytest.mark.conformance
@pytest.mark.skipif(
    os.environ.get("OPENADA_RUN_IHP_ANALOG_MEASUREMENTS") != "1",
    reason="set OPENADA_RUN_IHP_ANALOG_MEASUREMENTS=1 after pinned setup",
)
def test_real_replay(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(HERE / "run.py"),
        "--evidence-dir",
        str(tmp_path / "evidence"),
        "--container-engine",
        os.environ.get("OPENADA_CONTAINER_ENGINE", "docker"),
    ]
    cache = os.environ.get("OPENADA_IHP_ANALOG_MEASUREMENTS_CACHE_DIR")
    if cache:
        command.extend(["--cache-dir", cache])
    replay = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    result = json.loads(replay.stdout)
    assert result["status"] == "pass"
    assert result["spectral"]["snr"] == pytest.approx(93.2858420431)

