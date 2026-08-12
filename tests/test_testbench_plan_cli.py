"""CLI coverage for the closed testbench-plan surface."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import openada.cli as cli
import openada.operations.testbench_plan_runner as plan_runner


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "testbench-plan"
CONFORMANCE = ROOT / "conformance" / "testbench-oracle-v1" / "fixtures"
PLAN = FIXTURES / "closed_multistage_plan.json"


def _payload(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.mark.parametrize(
    ("argv", "operation"),
    [
        (["testbench-plan"], "testbench.plan.validate"),
        (["testbench-plan", "validate"], "testbench.plan.validate"),
        (["testbench-plan", "compile"], "testbench.plan.compile"),
        (["testbench-plan", "run"], "testbench.plan.run"),
        (["testbench-plan", "compare"], "testbench.oracle.compare"),
    ],
)
def test_malformed_leaves_retain_the_requested_operation(
    argv: list[str], operation: str, capsys
) -> None:
    assert cli.main(["--compact", *argv]) == 2
    emitted = _payload(capsys)

    assert emitted["operation"] == operation
    assert emitted["execution"]["status"] == "invalid_request"
    assert emitted["engineering"]["status"] == "unknown"
    if operation == "testbench.oracle.compare":
        assert emitted["data"]["comparison"] is None
        assert emitted["data"]["request_sha256"] is None
    else:
        assert emitted["data"]["refusals"]


def test_validate_reports_stable_plan_digests(capsys) -> None:
    assert cli.main(["--compact", "testbench-plan", "validate", str(PLAN)]) == 0
    emitted = _payload(capsys)

    assert emitted["operation"] == "testbench.plan.validate"
    assert emitted["engineering"]["status"] == "pass"
    assert emitted["data"]["schema"] == "simra.testbench-plan-validation/v1"
    assert emitted["data"]["raw_sha256"] == hashlib.sha256(PLAN.read_bytes()).hexdigest()
    assert emitted["data"]["refusals"] == []


def test_compile_passes_only_closed_arguments_to_the_compiler(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    def fake_compile(plan, output_dir, **kwargs):
        captured.update({"plan": plan, "output_dir": output_dir, **kwargs})
        return SimpleNamespace(
            receipt={
                "schema": "simra.testbench-plan-compile/v1",
                "conditions": [{"id": "dc.0"}],
            }
        )

    monkeypatch.setattr(cli, "compile_testbench_plan_ngspice", fake_compile)
    output = tmp_path / "compiled"
    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "compile",
            str(PLAN),
            "--corner",
            "tt",
            "--stage",
            "dc_characterize",
            "--output-dir",
            str(output),
        ]
    ) == 0
    emitted = _payload(capsys)

    assert emitted["operation"] == "testbench.plan.compile"
    assert captured["corner"] == "tt"
    assert captured["stage_ids"] == ("dc_characterize",)
    assert captured["output_dir"] == output
    assert "dut_artifact" not in captured
    assert emitted["data"]["conditions"] == [{"id": "dc.0"}]


def test_compare_emits_the_profile_normalized_envelope(capsys) -> None:
    request_id = "00000000-0000-4000-8000-000000000006"
    argv = [
        "--compact",
        "testbench-plan",
        "compare",
        "--observed",
        str(CONFORMANCE / "observed.json"),
        "--oracle",
        str(CONFORMANCE / "oracle.json"),
        "--tolerances",
        str(CONFORMANCE / "tolerances.json"),
        "--request-id",
        request_id,
    ]
    assert cli.main(argv) == 0
    emitted = _payload(capsys)

    assert emitted["operation"] == "testbench.oracle.compare"
    assert emitted["engineering"]["status"] == "pass"
    assert emitted["data"]["comparison"]["status"] == "PASS"
    assert emitted["data"]["protocol"]["request_id"] == request_id
    assert emitted["data"]["request_sha256"] is not None
    profile = json.loads(
        (ROOT / "profiles" / "testbench.oracle.compare-v1alpha1.json").read_text()
    )
    errors = list(
        Draft202012Validator(
            profile["normalized_result"]["data_schema"],
            format_checker=FormatChecker(),
        ).iter_errors(emitted["data"])
    )
    assert errors == []


def test_compare_rejects_noncanonical_request_id(capsys) -> None:
    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "compare",
            "--observed",
            str(CONFORMANCE / "observed.json"),
            "--oracle",
            str(CONFORMANCE / "oracle.json"),
            "--tolerances",
            str(CONFORMANCE / "tolerances.json"),
            "--request-id",
            "00000000-0000-4000-8000-00000000000A",
        ]
    ) == 2
    emitted = _payload(capsys)

    assert emitted["operation"] == "testbench.oracle.compare"
    assert emitted["data"]["comparison"] is None
    assert emitted["diagnostics"][0]["code"] == "testbench_oracle.request.invalid"


def test_dut_binding_flag_accepts_only_the_full_sealed_binding(
    tmp_path: Path, capsys
) -> None:
    plan = json.loads(PLAN.read_text())
    binding = deepcopy(plan["dut"])
    dut = FIXTURES / "compiler_charge_pump_dut.spice"
    binding["artifact"] = str(dut.resolve())
    binding["sha256"] = hashlib.sha256(dut.read_bytes()).hexdigest()
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")

    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "validate",
            str(PLAN),
            "--dut-binding",
            str(binding_path),
        ]
    ) == 0
    emitted = _payload(capsys)
    assert emitted["engineering"]["status"] == "pass"

    binding["top"] = "SHADOWED_DUT"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "validate",
            str(PLAN),
            "--dut-binding",
            str(binding_path),
        ]
    ) == 2
    emitted = _payload(capsys)
    assert emitted["data"]["refusals"][0]["code"] == "testbench_plan.dut.abi_mismatch"


def test_run_without_ngspice_retains_every_condition_attempt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    plan = json.loads(PLAN.read_text())
    dut = FIXTURES / "compiler_charge_pump_dut.spice"
    binding = deepcopy(plan["dut"])
    binding["artifact"] = str(dut.resolve())
    binding["sha256"] = hashlib.sha256(dut.read_bytes()).hexdigest()
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")

    monkeypatch.setattr(
        cli.DiscoveryManager,
        "find_binary",
        lambda _discovery, _tool_name: None,
    )

    def unavailable_executor():
        raise ValueError("ngspice executable 'ngspice' is unavailable")

    monkeypatch.setattr(plan_runner, "HostNgspiceExecutor", unavailable_executor)
    output = tmp_path / "run"
    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "run",
            str(PLAN),
            "--corner",
            "tt",
            "--dut-binding",
            str(binding_path),
            "--output-dir",
            str(output),
        ]
    ) == 2
    emitted = _payload(capsys)

    assert emitted["operation"] == "testbench.plan.run"
    assert emitted["execution"]["status"] == "not_available"
    assert emitted["engineering"]["status"] == "unknown"
    assert emitted["tool"] == {"name": "ngspice", "path": None, "version": None}
    assert emitted["diagnostics"][0]["code"] == "tool.missing"
    receipt = emitted["data"]["receipt"]
    assert receipt is not None
    assert receipt["condition_inventory_complete"] is True
    assert receipt["expected_condition_count"] == 55
    assert receipt["attempted_condition_count"] == 55
    assert receipt["not_executed_condition_count"] == 55
    assert receipt["simulator_identities"] == []
    assert all(
        attempt["status"] == "invalid"
        and attempt["returncode"] is None
        for attempt in receipt["attempts"]
    )
    assert emitted["data"]["observables"] is not None
    assert all(
        verdict.startswith("UNKNOWN(runner:")
        for verdict in emitted["data"]["observables"]["validity"].values()
    )
    assert json.loads((output / "run-receipt.json").read_text()) == receipt


def test_runner_unknown_validity_cannot_surface_as_engineering_pass(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    executor = object()
    monkeypatch.setattr(
        cli.DiscoveryManager,
        "find_binary",
        lambda _discovery, _tool_name: "/synthetic/ngspice",
    )
    monkeypatch.setattr(cli, "HostNgspiceExecutor", lambda _binary: executor)

    def fake_run(_plan, **kwargs):
        assert kwargs["executor"] is executor
        return SimpleNamespace(
            attempts=(SimpleNamespace(status="completed"),),
            refusals=(),
            observables={
                "schema": "simra.testbench-observables/v1",
                "validity": {
                    "observed_pulse_count": (
                        "UNKNOWN(runner: observed pulse-count extraction is unsupported)"
                    )
                },
            },
            receipt={"schema": "simra.testbench-plan-run/v1"},
        )

    monkeypatch.setattr(cli, "execute_testbench_plan_ngspice", fake_run)
    assert cli.main(
        [
            "--compact",
            "testbench-plan",
            "run",
            str(PLAN),
            "--corner",
            "tt",
            "--output-dir",
            str(tmp_path / "run"),
        ]
    ) == 2
    emitted = _payload(capsys)

    assert emitted["execution"]["status"] == "completed"
    assert emitted["engineering"]["status"] == "unknown"
    assert emitted["data"]["refusals"] == []
    assert emitted["data"]["observables"]["validity"][
        "observed_pulse_count"
    ].startswith("UNKNOWN(runner:")
