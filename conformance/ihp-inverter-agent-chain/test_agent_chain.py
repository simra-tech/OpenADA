"""Focused static and opt-in native tests for the IHP agent chain."""

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
import run as chain_run  # noqa: E402
import verify  # noqa: E402


def _coverage_module():
    path = REPOSITORY_ROOT / "tools/verify_semantic_coverage.py"
    spec = importlib.util.spec_from_file_location(
        "_openada_ihp_agent_chain_coverage", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_closes_declared_positive_surface() -> None:
    manifest = common.load_manifest(HERE / "manifest.json")
    semantic_steps = [
        step for step in manifest["steps"] if step["kind"] == "semantic-command"
    ]
    exercised = {
        row_id for step in semantic_steps for row_id in step["covers"]
    }

    assert len(manifest["covers"]) == 57
    assert set(manifest["covers"]) == exercised
    assert all(step["covers"] for step in semantic_steps)
    assert len(manifest["steps"]) == 23
    assert len(manifest["negative_replays"]) == 15
    assert len(manifest["tamper_replays"]) == 19
    assert (
        manifest["extensions"]["org.openada"]["provider"]["driver_version"]
        == "0.5.0"
    )


def test_manifest_passes_positive_coverage_model() -> None:
    coverage = _coverage_module()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    report = coverage.build_report(
        coverage.DEFAULT_CATALOG.resolve(),
        coverage.DEFAULT_CHAIN_INDEX.resolve(),
        mode="audit",
    )
    rows_by_id = {row["row_id"]: row for row in report["rows"]}

    assert coverage._positive_coverage_issues(
        manifest, rows_by_id, label="ihp-inverter-agent-chain"
    ) == []


def test_runner_verifier_and_gate_bind_same_semantic_subject() -> None:
    coverage = _coverage_module()
    expected = coverage._semantic_subject(coverage.DEFAULT_CATALOG)

    assert chain_run._semantic_subject() == expected
    assert verify._semantic_subject() == expected


@pytest.mark.conformance
@pytest.mark.skipif(
    os.environ.get("OPENADA_RUN_IHP_AGENT_CHAIN") != "1",
    reason="set OPENADA_RUN_IHP_AGENT_CHAIN=1 after running pinned setup",
)
def test_real_replay(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    cache_dir = os.environ.get("OPENADA_IHP_AGENT_CHAIN_CACHE_DIR")
    engine = os.environ.get("OPENADA_CONTAINER_ENGINE", "docker")
    command = [
        sys.executable,
        str(HERE / "run.py"),
        "--container-engine",
        engine,
        "--evidence-dir",
        str(evidence),
    ]
    if cache_dir:
        command.extend(["--cache-dir", cache_dir])
    replay = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
        check=False,
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
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    assert independent.returncode == 0, independent.stdout + independent.stderr
    report = json.loads(independent.stdout)
    assert report["status"] == "pass"
    assert len(report["tamper_replays"]) == 19
