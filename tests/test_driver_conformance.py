import json
from pathlib import Path
import subprocess
import sys

import pytest

from openada.conformance import (
    MAX_CONFORMANCE_ISSUE_CHARS,
    MAX_CONFORMANCE_ISSUES,
    ResultConformanceError,
    assert_result_conforms,
    result_conformance_issues,
    result_schema_path,
)
from openada.contract import file_record, result, static_execution, tool_record


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "conformance" / "driver-kit" / "check_result.py"


def _payload(tmp_path: Path) -> tuple[dict, Path]:
    artifact = tmp_path / "native.log"
    artifact.write_text("native evidence\n", encoding="utf-8")
    payload = result(
        "demo-check",
        tool=tool_record("fake", path="/usr/bin/fake", version="fake 1.0"),
        execution=static_execution(),
        engineering_status="pass",
        summary="The fake engineering check passed.",
        artifacts=[file_record(artifact, kind="native-log", role="evidence")],
        diagnostics=[
            {
                "severity": "info",
                "code": "demo.checked",
                "message": "Native evidence was checked.",
            }
        ],
    )
    return payload, artifact


def test_result_schema_is_locatable() -> None:
    schema = json.loads(result_schema_path().read_text(encoding="utf-8"))
    assert schema["$id"].endswith("result-v0alpha1.schema.json")


def test_conformance_helper_checks_expectations_and_recorded_files(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)

    assert_result_conforms(
        payload,
        expected_operation="demo-check",
        expected_execution_status="completed",
        expected_engineering_status="pass",
        required_artifact_roles=("evidence",),
        required_diagnostic_codes=("demo.checked",),
        verify_recorded_files=True,
    )


def test_conformance_helper_reports_expectation_mismatches(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)

    issues = result_conformance_issues(
        payload,
        expected_operation="another-operation",
        expected_engineering_status="fail",
        required_artifact_roles=("layout",),
        required_diagnostic_codes=("demo.missing",),
    )

    assert any("#/operation" in issue for issue in issues)
    assert any("#/engineering/status" in issue for issue in issues)
    assert any("required existing artifact role" in issue for issue in issues)
    assert any("required diagnostic code" in issue for issue in issues)


def test_conformance_helper_detects_artifact_tampering(tmp_path: Path) -> None:
    payload, artifact = _payload(tmp_path)
    artifact.write_text("tampered evidence\n", encoding="utf-8")

    with pytest.raises(ResultConformanceError) as raised:
        assert_result_conforms(payload, verify_recorded_files=True)

    assert any("byte count mismatch" in issue for issue in raised.value.issues)
    assert any("SHA-256 mismatch" in issue for issue in raised.value.issues)


def test_conformance_helper_bounds_recorded_file_work(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)

    file_count_issues = result_conformance_issues(
        payload,
        verify_recorded_files=True,
        max_recorded_files=0,
    )
    total_size_issues = result_conformance_issues(
        payload,
        verify_recorded_files=True,
        max_total_recorded_file_bytes=1,
    )

    assert any("file verification bound" in issue for issue in file_count_issues)
    assert any("aggregate verification bound" in issue for issue in total_size_issues)


def test_conformance_helper_rejects_schema_invalid_result(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    payload["execution"]["exit_code"] = None

    issues = result_conformance_issues(payload)

    assert any("#/execution/exit_code" in issue for issue in issues)


def test_conformance_helper_bounds_schema_issues(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    payload["diagnostics"] = [{"unexpected": index} for index in range(200)]

    issues = result_conformance_issues(payload)

    assert len(issues) == MAX_CONFORMANCE_ISSUES
    assert "additional schema issues omitted" in issues[-1]


def test_conformance_helper_bounds_each_schema_issue(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    payload["execution"]["status"] = "x" * 100_000

    issues = result_conformance_issues(payload)

    assert issues
    assert all(len(issue) <= MAX_CONFORMANCE_ISSUE_CHARS for issue in issues)


def test_checkout_checker_emits_one_json_result(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    checked = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            str(result_path),
            "--expect-operation",
            "demo-check",
            "--expect-execution",
            "completed",
            "--expect-engineering",
            "pass",
            "--require-artifact-role",
            "evidence",
            "--verify-files",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert checked.returncode == 0
    assert checked.stderr == ""
    assert json.loads(checked.stdout)["status"] == "pass"


def test_checkout_checker_distinguishes_failed_expectation(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    checked = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            str(result_path),
            "--expect-engineering",
            "fail",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    output = json.loads(checked.stdout)
    assert checked.returncode == 1
    assert checked.stderr == ""
    assert output["status"] == "fail"
    assert any("#/engineering/status" in issue for issue in output["issues"])


def test_checkout_checker_rejects_result_symlink(tmp_path: Path) -> None:
    payload, _ = _payload(tmp_path)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    link_path = tmp_path / "result-link.json"
    link_path.symlink_to(result_path)

    checked = subprocess.run(
        [sys.executable, str(CHECKER), str(link_path)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    output = json.loads(checked.stdout)
    assert checked.returncode == 2
    assert checked.stderr == ""
    assert output["status"] == "error"
    assert "symbolic-link" in output["issues"][0]
