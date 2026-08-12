"""Conformance tests for the closed plan/compiler/native runner bundle."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from openada.operations import compare_testbench_observables


ROOT = Path(__file__).parents[1]
BUNDLE = ROOT / "conformance" / "testbench-plan-v1"


def _ngspice() -> str | None:
    binary = shutil.which("ngspice")
    if binary is None:
        return None
    completed = subprocess.run(
        [binary, "--version-small"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    identity = completed.stdout or completed.stderr
    return binary if completed.returncode == 0 and "ngspice-" in identity.casefold() else None


def test_independent_verifier_has_no_openada_import() -> None:
    source = (BUNDLE / "verify.py").read_text(encoding="utf-8")
    assert "import openada" not in source
    assert "from openada" not in source


@pytest.mark.skipif(_ngspice() is None, reason="ngspice is unavailable")
def test_native_bundle_runs_verifies_tampers_and_refuses_overwrite(tmp_path: Path) -> None:
    evidence = tmp_path / "testbench-plan-v1.json"
    command = [
        sys.executable,
        str(BUNDLE / "run.py"),
        "--ngspice",
        str(_ngspice()),
        "--evidence-file",
        str(evidence),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    verification = json.loads(completed.stdout)
    assert verification["status"] == "pass"
    assert verification["verified_conditions"] == 4
    assert verification["verified_tamper_cases"] == 4

    standalone = subprocess.run(
        [sys.executable, str(BUNDLE / "verify.py"), str(evidence)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    assert json.loads(standalone.stdout) == verification

    # Exercise the actual native runner envelope against the same closed
    # tolerance contract used by the twelve benchmark rows.  This catches
    # accidental coupling between canonical runner {x,y} fields, legacy oracle
    # axis labels, and the separate top-level corner/receipt-ID namespaces.
    record = json.loads(evidence.read_text(encoding="utf-8"))
    observed = record["execution"]["observables"]
    oracle = {
        "sizing": {"topology": "synthetic_rc", "parameters": {}},
        "corner": "tt",
        "validity": dict(observed["validity"]),
        "observables": {
            "dc_curve": {
                "voltage_v": observed["observables"]["dc_curve"]["x"],
                "response_v": observed["observables"]["dc_curve"]["y"],
            },
            "pulse_curve": {
                "time_s": observed["observables"]["pulse_curve"]["x"],
                "response_v": observed["observables"]["pulse_curve"]["y"],
            },
        },
    }
    metrics = [
        {
            "name": f"{name}_error",
            "kind": "curve",
            "required": True,
            "limit": {"op": "<=", "value": 0.0, "unit": "V"},
            "observed": name,
            "oracle": name,
            "x": x_name,
            "y": "response_v",
            "error": {"kind": "absolute"},
        }
        for name, x_name in (("dc_curve", "voltage_v"), ("pulse_curve", "time_s"))
    ]
    metrics.append(
        {
            "name": "completeness",
            "kind": "completeness",
            "required": True,
            "limit": {"op": ">=", "value": 1.0, "unit": "frac"},
            "observables": ["dc_curve", "pulse_curve"],
            "conditions": ["tt"],
        }
    )
    comparison = compare_testbench_observables(
        observed,
        oracle,
        {
            "schema": "simra.testbench-oracle-tolerances/v1",
            "lineage_required": True,
            "metrics": metrics,
            "extensions": {},
        },
    )
    assert comparison["status"] == "PASS"
    assert comparison["summary"] == {
        "pass": 3,
        "fail": 0,
        "unknown": 0,
        "required": 3,
        "required_pass": 3,
        "required_fail": 0,
        "required_unknown": 0,
    }

    original = evidence.read_bytes()
    repeated = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    assert repeated.returncode != 0
    assert evidence.read_bytes() == original


def test_manifest_and_closed_fixture_load_in_independent_verifier() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util,pathlib;"
                f"p=pathlib.Path({str(BUNDLE / 'verify.py')!r});"
                "s=importlib.util.spec_from_file_location('tbplan_verify',p);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "x=m.load_manifest();f=m._fixtures(x);"
                "assert f['plan']['schema']=='simra.testbench-plan/v1';"
                "assert len(f['expected']['conditions'])==4"
            ),
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
