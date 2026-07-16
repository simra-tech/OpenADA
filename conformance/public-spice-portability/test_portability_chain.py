"""Focused static and opt-in native tests for public SPICE portability."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common  # noqa: E402
import verify  # noqa: E402


def _coverage_module():
    path = REPOSITORY_ROOT / "tools/verify_semantic_coverage.py"
    spec = importlib.util.spec_from_file_location(
        "_openada_public_portability_coverage", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_closes_the_exact_portability_surface() -> None:
    manifest = common.load_manifest(HERE / "manifest.json")
    semantic_steps = [
        step for step in manifest["steps"] if step["kind"] == "semantic-command"
    ]
    exercised = {row for step in semantic_steps for row in step["covers"]}

    assert len(manifest["covers"]) == 29
    assert set(manifest["covers"]) == exercised
    assert len(manifest["steps"]) == 23
    assert len(manifest["negative_replays"]) == 6
    assert len(manifest["tamper_replays"]) == 9
    assert sum(step["native_execution"] for step in semantic_steps) == 7


def test_manifest_pins_two_public_source_trees_and_six_real_simulations() -> None:
    manifest = common.load_manifest(HERE / "manifest.json")
    secondary = manifest["design"]["extensions"]["org.openada"]["secondary_design"]
    requests = common.load_requests()

    assert manifest["design"]["tree"] == "bf4e9753365af33c82814e4c44aeb7a687490b96"
    assert secondary["tree"] == "2a710fd503226e9642e4337a324e6c192a9d8a31"
    assert [item["id"] for item in requests["simulations"]] == [
        "ngspice-op",
        "ngspice-dc",
        "ngspice-ac",
        "xyce-dc",
        "xyce-ac",
        "xyce-tran",
    ]


def test_manifest_passes_the_positive_coverage_model() -> None:
    coverage = _coverage_module()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    report = coverage.build_report(
        coverage.DEFAULT_CATALOG.resolve(),
        coverage.DEFAULT_CHAIN_INDEX.resolve(),
        mode="audit",
    )
    rows = {row["row_id"]: row for row in report["rows"]}

    assert coverage._positive_coverage_issues(
        manifest, rows, label="public-spice-portability"
    ) == []


def test_verifier_and_gate_bind_the_same_semantic_subject() -> None:
    coverage = _coverage_module()
    assert verify.semantic_subject_sha256() == coverage._semantic_subject(
        coverage.DEFAULT_CATALOG
    )


@pytest.mark.conformance
@pytest.mark.skipif(
    os.environ.get("OPENADA_RUN_PUBLIC_SPICE_PORTABILITY") != "1",
    reason="set OPENADA_RUN_PUBLIC_SPICE_PORTABILITY=1 after pinned setup",
)
def test_real_replay(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    command = [
        sys.executable,
        str(HERE / "run.py"),
        "--evidence-dir",
        str(evidence),
        "--container-engine",
        os.environ.get("OPENADA_CONTAINER_ENGINE", "docker"),
    ]
    cache = os.environ.get("OPENADA_PUBLIC_SPICE_PORTABILITY_CACHE_DIR")
    if cache:
        command.extend(["--cache-dir", cache])
    replay = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1200,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr

    independent = subprocess.run(
        [
            sys.executable,
            str(HERE / "verify.py"),
            str(evidence),
            "--run-tamper-probes",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    assert independent.returncode == 0, independent.stdout + independent.stderr

