from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

import openada.cli as cli
from openada.cli import main
from openada.contract import MAX_CONTRACT_TEXT_CHARS, result, static_execution


ROOT = Path(__file__).parents[1]


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _typed_series() -> dict:
    axis = {"name": "time", "unit": "s", "values": [0.0, 1.0, 2.0]}
    signals = [
        {"name": "vout", "unit": "V", "values": [0.0, 0.5, 1.0]}
    ]
    conditions = [{"name": "temperature", "value": 27.0, "unit": "degC"}]
    content = {"axis": axis, "signals": signals, "conditions": conditions}
    return {
        "source": {
            "operation": "simulation.result.normalize",
            "request_id": "00000000-0000-4000-8000-000000000001",
            "artifact_role": "measurement.source",
            "artifact_sha256": _canonical_digest(content),
        },
        **content,
        "extensions": {},
    }


def _extraction_envelope() -> dict:
    base = _typed_series()
    series = {
        **base,
        "source": {
            "operation": "openada.operation/result.series.extract/v1alpha1",
            "request_id": "00000000-0000-4000-8000-000000000011",
            "artifact_role": "measurement.source",
            "artifact_sha256": base["source"]["artifact_sha256"],
            "lineage": {
                "operation": "circuit.simulate",
                "request_id": "00000000-0000-4000-8000-000000000010",
                "artifact_role": "simulation.result",
                "artifact_sha256": "a" * 64,
                "binding": "unverified",
            },
        },
    }
    artifact = {
        "kind": "ngspice-raw",
        "role": "simulation.result",
        "path": "/tmp/openada-cli-test.raw",
        "exists": True,
        "bytes": 100,
        "sha256": "a" * 64,
    }
    return result(
        "result.series.extract",
        tool=None,
        execution=static_execution(),
        engineering_status="pass",
        summary="Extracted test series.",
        inputs=[artifact],
        data={
            "protocol": {
                "request_id": "00000000-0000-4000-8000-000000000011",
                "operation_profile": "openada.operation/result.series.extract/v1alpha1",
                "assertion_profile": "openada.assertion/series.extraction.valid/v1alpha1",
                "implementation_id": "org.openada.kernel.spice3-series",
                "implementation_version": "1.0.0",
            },
            "extraction": {
                "status": "extracted",
                "request_sha256": "b" * 64,
                "source": {
                    "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
                    "request_id": "00000000-0000-4000-8000-000000000010",
                    "driver_id": "org.openada.driver.ngspice",
                    "driver_version": "0.4.0",
                    "backend": "ngspice",
                    "analysis_type": "tran",
                    "artifact": artifact,
                    "binding": "verified",
                },
                "plot": {
                    "plotname": "Transient Analysis",
                    "encoding": "binary",
                    "numeric_type": "real",
                    "point_count": 3,
                    "native_axis_name": "time",
                    "native_axis_type": "time",
                    "extensions": {},
                },
                "series": series,
                "extensions": {},
            },
            "extensions": {},
        },
    )


def test_doctor_emits_one_contract_object(capsys):
    exit_code = main(["--compact", "doctor", "--tool", "ngspice"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema"] == "openada.result/v0alpha1"
    assert payload["operation"] == "doctor"
    assert set(payload["data"]["tools"]) == {"ngspice"}


def test_capabilities_exposes_semantic_provider_records(capsys):
    exit_code = main(["--compact", "capabilities", "--tool", "ngspice"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    records = payload["data"]["semantic_capabilities"]
    by_provider = {record["provider_id"]: record for record in records}

    ngspice = by_provider["org.openada.driver.ngspice"]
    assert ngspice["provider_id"] == "org.openada.driver.ngspice"
    assert ngspice["transports"] == ["local-cli"]
    assert {feature["id"] for feature in ngspice["features"]} == {
        "openada.feature/simulation.analysis.op/v1alpha1",
        "openada.feature/simulation.analysis.dc/v1alpha1",
        "openada.feature/simulation.analysis.ac/v1alpha1",
        "openada.feature/simulation.analysis.tran/v1alpha1",
    }
    assert {
        feature["id"]: feature["maturity"] for feature in ngspice["features"]
    } == {
        "openada.feature/simulation.analysis.op/v1alpha1": "structured",
        "openada.feature/simulation.analysis.dc/v1alpha1": "structured",
        "openada.feature/simulation.analysis.ac/v1alpha1": "structured",
        "openada.feature/simulation.analysis.tran/v1alpha1": "workflow-validated",
    }
    assert all(feature["conformance_ids"] for feature in ngspice["features"])

    measurement = next(
        record
        for record in records
        if record["operation_profile"] == "openada.operation/result.measure/v1alpha1"
    )
    assert measurement["operation_profile_schema"] == (
        "openada.operation-profile/v0alpha2"
    )
    assert measurement["assertion_profile"] == (
        "openada.assertion/measurement.valid/v1alpha1"
    )
    assert all(feature["maturity"] == "structured" for feature in measurement["features"])
    assert {
        conformance_id
        for feature in measurement["features"]
        for conformance_id in feature["conformance_ids"]
    } == {"typed-evidence-measurement-specification-v0alpha1"}

    specification = next(
        record
        for record in records
        if record["operation_profile"]
        == "openada.operation/specification.evaluate/v1alpha1"
    )
    assert all(feature["maturity"] == "structured" for feature in specification["features"])
    assert all(
        feature["conformance_ids"]
        == ["typed-evidence-measurement-specification-v0alpha1"]
        for feature in specification["features"]
    )
    extraction = next(
        record
        for record in records
        if record["operation_profile"]
        == "openada.operation/result.series.extract/v1alpha1"
    )
    assert extraction["provider_id"] == "org.openada.kernel.spice3-series"
    assert extraction["features"] == []
    spectral = next(
        record
        for record in records
        if record["operation_profile"]
        == "openada.operation/result.spectral.measure/v1alpha1"
    )
    assert {feature["id"] for feature in spectral["features"]} == {
        "openada.feature/spectral.snr/v1alpha1",
        "openada.feature/spectral.sinad/v1alpha1",
        "openada.feature/spectral.thd/v1alpha1",
        "openada.feature/spectral.sfdr/v1alpha1",
    }
    transfer = next(
        record
        for record in records
        if record["operation_profile"]
        == "openada.operation/result.transfer.measure/v1alpha1"
    )
    assert {feature["id"] for feature in transfer["features"]} == {
        "openada.feature/transfer.low-frequency-gain/v1alpha1",
        "openada.feature/transfer.bandwidth-3db/v1alpha1",
        "openada.feature/transfer.unity-gain-frequency/v1alpha1",
        "openada.feature/transfer.phase-margin/v1alpha1",
    }
    assert {
        provider: by_provider[provider]["features"]
        for provider in (
            "org.openada.driver.verilator",
            "org.openada.driver.yosys",
            "org.openada.driver.opensta",
        )
    } == {
        "org.openada.driver.verilator": [
            {
                "id": "openada.feature/rtl.lint.systemverilog/v1alpha1",
                "maturity": "workflow-validated",
                "conformance_ids": ["ihp-sar-rtl-check"],
            }
        ],
        "org.openada.driver.yosys": [
            {
                "id": "openada.feature/synthesis.asic-liberty/v1alpha1",
                "maturity": "workflow-validated",
                "conformance_ids": ["orfs-ibex-synthesis-timing"],
            }
        ],
        "org.openada.driver.opensta": [
            {
                "id": "openada.feature/timing.setup-hold/v1alpha1",
                "maturity": "workflow-validated",
                "conformance_ids": ["orfs-ibex-synthesis-timing"],
            }
        ],
    }


def test_digital_commands_dispatch_every_declared_semantic_option(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, tuple[tuple, dict]] = {}

    class FakeVerilator:
        def __init__(self, *, discovery):
            assert discovery is not None

        def rtl_lint(self, *args, **kwargs):
            calls["rtl-lint"] = (args, kwargs)
            return result(
                "rtl-lint",
                tool=None,
                execution=static_execution(),
                engineering_status="pass",
                summary="fake lint",
            )

    class FakeYosys:
        def __init__(self, *, discovery):
            assert discovery is not None

        def synthesize(self, *args, **kwargs):
            calls["synthesize"] = (args, kwargs)
            return result(
                "synthesize",
                tool=None,
                execution=static_execution(),
                engineering_status="pass",
                summary="fake synthesis",
            )

    class FakeOpenSTA:
        def __init__(self, *, discovery):
            assert discovery is not None

        def timing_analyze(self, *args, **kwargs):
            calls["timing-analyze"] = (args, kwargs)
            return result(
                "timing-analyze",
                tool=None,
                execution=static_execution(),
                engineering_status="pass",
                summary="fake timing",
            )

    monkeypatch.setattr(cli, "VerilatorDriver", FakeVerilator)
    monkeypatch.setattr(cli, "YosysDriver", FakeYosys)
    monkeypatch.setattr(cli, "OpenSTADriver", FakeOpenSTA)
    source = tmp_path / "top.sv"
    include = tmp_path / "include"
    liberty = tmp_path / "cells.lib"
    techmap = tmp_path / "map.v"
    constraint = tmp_path / "abc.constr"
    netlist = tmp_path / "mapped.v"
    sdc = tmp_path / "top.sdc"

    assert main(
        [
            "--compact",
            "rtl-lint",
            str(source),
            "--top",
            "top",
            "--include-dir",
            str(include),
            "--define",
            "ASIC=1",
            "--language",
            "1800-2023",
            "--output-dir",
            str(tmp_path / "lint"),
            "--timeout",
            "17",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "rtl-lint"
    assert main(
        [
            "--compact",
            "synthesize",
            str(source),
            "--top",
            "top",
            "--liberty",
            str(liberty),
            "--frontend",
            "slang",
            "--include-dir",
            str(include),
            "--define",
            "ASIC=1",
            "--language",
            "1800-2023",
            "--techmap",
            str(techmap),
            "--dont-use",
            "CLKGATE_*",
            "--abc-delay-target-ns",
            "2.2",
            "--abc-constraint",
            str(constraint),
            "--output-dir",
            str(tmp_path / "synth"),
            "--timeout",
            "31",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "synthesize"
    assert main(
        [
            "--compact",
            "timing-analyze",
            str(netlist),
            "--top",
            "top",
            "--liberty",
            str(liberty),
            "--sdc",
            str(sdc),
            "--output-dir",
            str(tmp_path / "timing"),
            "--timeout",
            "43",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "timing-analyze"

    assert calls["rtl-lint"] == (
        ([str(source)], (tmp_path / "lint").resolve()),
        {
            "top": "top",
            "include_dirs": [str(include)],
            "defines": ["ASIC=1"],
            "language": "1800-2023",
            "timeout": 17.0,
        },
    )
    assert calls["synthesize"] == (
        ([str(source)], str(liberty), (tmp_path / "synth").resolve()),
        {
            "top": "top",
            "frontend": "slang",
            "include_dirs": [str(include)],
            "defines": ["ASIC=1"],
            "language": "1800-2023",
            "techmaps": [str(techmap)],
            "dont_use": ["CLKGATE_*"],
            "abc_delay_target_ns": 2.2,
            "abc_constraint": str(constraint),
            "timeout": 31.0,
        },
    )
    assert calls["timing-analyze"] == (
        (str(netlist), str(liberty), str(sdc), (tmp_path / "timing").resolve()),
        {"top": "top", "timeout": 43.0},
    )


def test_capabilities_help_uses_the_invoked_command_name(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["capabilities", "--help"])

    assert caught.value.code == 0
    assert capsys.readouterr().out.startswith("usage: openada capabilities ")


def test_missing_input_is_unknown_not_engineering_fail(tmp_path, capsys):
    exit_code = main(
        [
            "netlist",
            str(tmp_path / "missing.sch"),
            "--output",
            str(tmp_path / "out.spice"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"


@pytest.mark.parametrize(
    ("argv", "operation"),
    [
        ([], "openada.invalid_request"),
        (["netlist"], "netlist"),
        (["doctor", "--tool", "not-a-tool"], "doctor"),
        (["not-a-command"], "openada.invalid_request"),
        (["--tool-path", "not-an-override", "doctor"], "doctor"),
        (["measure"], "result.measure"),
        (["extract"], "result.series.extract"),
        (["spectral"], "result.spectral.measure"),
        (["transfer"], "result.transfer.measure"),
        (["evaluate"], "specification.evaluate"),
        (["provider"], "provider"),
        (["profile"], "profile"),
    ],
)
def test_malformed_invocation_emits_one_invalid_request(argv, operation, capsys):
    exit_code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert payload["schema"] == "openada.result/v0alpha1"
    assert payload["operation"] == operation
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert len(payload["diagnostics"]) == 1
    expected_code = {
        "result.measure": "measurement.request.invalid",
        "result.series.extract": "series.request.invalid",
        "result.spectral.measure": "spectral.request.invalid",
        "result.transfer.measure": "transfer.request.invalid",
        "specification.evaluate": "specification.request.invalid",
    }.get(operation, "request.invalid")
    assert payload["diagnostics"][0]["code"] == expected_code
    if operation == "result.measure":
        assert payload["data"]["measurement"]["status"] == "unknown"
    if operation == "result.series.extract":
        assert payload["data"]["extraction"]["status"] == "unknown"
    if operation == "result.spectral.measure":
        assert payload["data"]["measurement"]["status"] == "unknown"
    if operation == "result.transfer.measure":
        assert payload["data"]["measurement"]["status"] == "unknown"
    if operation == "specification.evaluate":
        assert payload["data"]["evaluation"]["status"] == "unknown"


def test_malformed_compact_invocation_is_one_json_line(capsys):
    exit_code = main(["--compact", "drc"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["operation"] == "drc"


def test_malformed_invocation_bounds_argparse_error(capsys):
    exit_code = main(["doctor", "--tool", "x" * 100_000])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert len(payload["diagnostics"][0]["message"]) <= MAX_CONTRACT_TEXT_CHARS
    assert len(captured.out) < 10_000


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_help_and_version_keep_normal_argparse_output(option, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main([option])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("analysis_args", "expected"),
    [
        (["--analysis", "op"], {"type": "op", "extensions": {}}),
        (
            [
                "--analysis",
                "dc",
                "--source-name",
                "VSWEEP",
                "--source-unit",
                "V",
                "--start",
                "-0.2",
                "--stop",
                "1.2",
                "--step",
                "0.01",
            ],
            {
                "type": "dc",
                "source_name": "VSWEEP",
                "source_unit": "V",
                "start": -0.2,
                "stop": 1.2,
                "step": 0.01,
                "extensions": {},
            },
        ),
        (
            [
                "--analysis",
                "ac",
                "--sweep",
                "dec",
                "--points",
                "20",
                "--start-hz",
                "10",
                "--stop-hz",
                "1e9",
            ],
            {
                "type": "ac",
                "sweep": "dec",
                "points": 20,
                "start_hz": 10.0,
                "stop_hz": 1e9,
                "extensions": {},
            },
        ),
        (
            [
                "--analysis",
                "tran",
                "--step-s",
                "1e-9",
                "--stop-s",
                "1e-6",
                "--start-s",
                "2e-9",
                "--max-step-s",
                "1e-8",
            ],
            {
                "type": "tran",
                "step_s": 1e-9,
                "stop_s": 1e-6,
                "start_s": 2e-9,
                "max_step_s": 1e-8,
                "extensions": {},
            },
        ),
    ],
)
def test_shared_simulation_cli_forwards_closed_typed_parameters(
    tmp_path, capsys, monkeypatch, analysis_args, expected
):
    source = tmp_path / "fixture.cir"
    source.write_text("typed simulation fixture\n.end\n", encoding="utf-8")
    captured = {}

    def fake_simulate(*args, **kwargs):
        captured.update(kwargs)
        return result(
            "circuit.simulate",
            tool=None,
            execution=static_execution(),
            engineering_status="not_applicable",
            summary="Typed CLI dispatch captured.",
        )

    monkeypatch.setattr(cli, "simulate_circuit_profile", fake_simulate)
    exit_code = main(
        ["simulate", str(source), "--backend", "ngspice", *analysis_args]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["operation"] == "circuit.simulate"
    assert captured["parameters"] == {"analysis": expected, "extensions": {}}


@pytest.mark.parametrize(
    "analysis_args",
    [
        ["--start-hz", "1"],
        ["--analysis", "dc", "--source-name", "V1"],
        [
            "--analysis",
            "ac",
            "--sweep",
            "dec",
            "--points",
            "10",
            "--start-hz",
            "100",
            "--stop-hz",
            "10",
        ],
        [
            "--analysis",
            "tran",
            "--step-s",
            "1e-9",
            "--stop-s",
            "1e-6",
            "--start-s",
            "1e-6",
        ],
    ],
)
def test_typed_simulation_cli_rejects_incomplete_or_incoherent_parameters(
    tmp_path, capsys, analysis_args
):
    source = tmp_path / "fixture.cir"
    source.write_text("typed simulation fixture\n.end\n", encoding="utf-8")

    exit_code = main(
        ["simulate", str(source), "--backend", "ngspice", *analysis_args]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "simulation.request.invalid"
    assert payload["data"]["protocol"]["operation_profile"] == (
        "openada.operation/circuit.simulate/v1alpha2"
    )
    assert payload["data"]["analysis"]["completion"] == "unproven"


def test_typed_simulation_parameters_require_the_shared_backend(tmp_path, capsys):
    source = tmp_path / "fixture.cir"
    source.write_text("typed simulation fixture\n.end\n", encoding="utf-8")

    exit_code = main(["simulate", str(source), "--analysis", "op"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert "require --backend" in payload["diagnostics"][0]["message"]
    assert payload["diagnostics"][0]["code"] == "request.invalid"
    assert payload["data"] == {}


def test_measure_and_evaluate_cli_form_a_typed_evidence_chain(tmp_path, capsys):
    series_path = tmp_path / "series.json"
    measurement_request_path = tmp_path / "measurement-request.json"
    measurement_result_path = tmp_path / "measurement-result.json"
    specification_path = tmp_path / "specification.json"

    series_path.write_text(json.dumps(_typed_series()), encoding="utf-8")
    measurement_request_path.write_text(
        json.dumps(
            {
                "measurement_id": "vout.maximum",
                "kind": "maximum",
                "signal": "vout",
                "parameters": {},
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )

    measure_exit = main(
        [
            "--compact",
            "measure",
            "--series",
            str(series_path),
            "--measurement",
            str(measurement_request_path),
            "--request-id",
            "00000000-0000-4000-8000-000000000002",
        ]
    )
    measured = json.loads(capsys.readouterr().out)

    assert measure_exit == 0
    assert measured["operation"] == "result.measure"
    assert measured["engineering"]["status"] == "pass"
    assert measured["data"]["measurement"]["value"] == 1.0
    assert measured["data"]["measurement"]["source"]["artifact_sha256"] == (
        measured["data"]["measurement"]["source"]["series_sha256"]
    )

    measurement_result_path.write_text(json.dumps(measured), encoding="utf-8")
    specification_path.write_text(
        json.dumps(
            {
                "specification_id": "vout.maximum.limit",
                "measurement_id": "vout.maximum",
                "limits": {
                    "upper": {"value": 1.1, "unit": "V", "inclusive": True}
                },
                "conditions": [
                    {"name": "temperature", "value": 27.0, "unit": "degC"}
                ],
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )

    evaluate_exit = main(
        [
            "--compact",
            "evaluate",
            "--measurement",
            str(measurement_result_path),
            "--specification",
            str(specification_path),
            "--request-id",
            "00000000-0000-4000-8000-000000000003",
        ]
    )
    evaluated = json.loads(capsys.readouterr().out)

    assert evaluate_exit == 0
    assert evaluated["operation"] == "specification.evaluate"
    assert evaluated["engineering"]["status"] == "pass"
    assert evaluated["data"]["evaluation"]["margin"] == {
        "relative_to": "upper",
        "unit": "V",
        "value": pytest.approx(0.1),
    }


def test_measure_cli_accepts_a_complete_passing_extraction_envelope(
    tmp_path, capsys
) -> None:
    extraction_path = tmp_path / "series-extraction.json"
    request_path = tmp_path / "measurement.json"
    extraction_path.write_text(
        json.dumps(_extraction_envelope()), encoding="utf-8"
    )
    request_path.write_text(
        json.dumps(
            {
                "measurement_id": "vout.maximum",
                "kind": "maximum",
                "signal": "vout",
                "parameters": {},
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--compact",
            "measure",
            "--series",
            str(extraction_path),
            "--measurement",
            str(request_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["data"]["measurement"]["value"] == 1.0
    assert payload["data"]["measurement"]["source"]["lineage"]["operation"] == (
        "circuit.simulate"
    )


def test_series_handoff_rejects_a_nonpassing_extraction_envelope(
    tmp_path, capsys
) -> None:
    envelope = _extraction_envelope()
    envelope["engineering"]["status"] = "unknown"
    extraction_path = tmp_path / "series-extraction.json"
    request_path = tmp_path / "measurement.json"
    extraction_path.write_text(json.dumps(envelope), encoding="utf-8")
    request_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "measure",
            "--series",
            str(extraction_path),
            "--measurement",
            str(request_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert "complete passing result" in payload["diagnostics"][0]["message"]


def test_profile_cli_lists_and_shows_packaged_contracts(capsys) -> None:
    list_exit = main(["--compact", "profile", "list"])
    listed = json.loads(capsys.readouterr().out)
    profile_ids = {
        item["operation_profile"] for item in listed["data"]["profiles"]
    }

    assert list_exit == 0
    assert "openada.operation/result.transfer.measure/v1alpha1" in profile_ids
    assert "openada.operation/result.spectral.measure/v1alpha1" in profile_ids

    show_exit = main(
        [
            "--compact",
            "profile",
            "show",
            "openada.operation/result.transfer.measure/v1alpha1",
        ]
    )
    shown = json.loads(capsys.readouterr().out)

    assert show_exit == 0
    assert shown["data"]["profile"]["operation"]["id"] == (
        "openada.operation/result.transfer.measure/v1alpha1"
    )


def test_source_launcher_without_site_packages_has_stable_dependency_boundary() -> None:
    doctor = subprocess.run(
        [sys.executable, "-S", str(ROOT / "bin" / "openada"), "--compact", "doctor"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert doctor.returncode == 0
    assert json.loads(doctor.stdout)["execution"]["status"] == "completed"

    profiles = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "bin" / "openada"),
            "--compact",
            "profile",
            "list",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(profiles.stdout)
    assert profiles.returncode == 2
    assert payload["execution"]["status"] == "failed"
    assert payload["diagnostics"][0]["code"] == "provider.validation.unavailable"
    assert "jsonschema" in payload["diagnostics"][0]["message"]


def test_evaluate_cli_requires_the_complete_measurement_envelope(tmp_path, capsys):
    measurement_path = tmp_path / "measurement.json"
    specification_path = tmp_path / "specification.json"
    measurement_path.write_text("{}", encoding="utf-8")
    specification_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "evaluate",
            "--measurement",
            str(measurement_path),
            "--specification",
            str(specification_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["operation"] == "specification.evaluate"
    assert payload["execution"]["status"] == "invalid_request"
    assert "complete openada.result/v0alpha1 envelope" in (
        payload["diagnostics"][0]["message"]
    )


@pytest.mark.parametrize(
    "tamper",
    [
        lambda payload: payload["execution"].__setitem__("undeclared", True),
        lambda payload: payload["provenance"]["host"].__setitem__("undeclared", True),
        lambda payload: payload["data"]["protocol"].__setitem__(
            "request_id", "00000000-0000-4000-8000-00000000000A"
        ),
    ],
)
def test_measurement_envelope_validation_is_closed(tamper) -> None:
    envelope = cli.measure_result(
        _typed_series(),
        {
            "measurement_id": "output.maximum",
            "kind": "maximum",
            "signal": "vout",
            "parameters": {},
            "extensions": {},
        },
    )
    tamper(envelope)

    with pytest.raises(ValueError):
        cli._measurement_record(envelope)


@pytest.mark.parametrize(
    ("operation", "operation_profile", "assertion_profile", "implementation_id", "evidence_field"),
    [
        (
            "result.spectral.measure",
            "openada.operation/result.spectral.measure/v1alpha1",
            "openada.assertion/spectral.measurement.valid/v1alpha1",
            "org.openada.kernel.spectral-evidence",
            "spectral",
        ),
        (
            "result.transfer.measure",
            "openada.operation/result.transfer.measure/v1alpha1",
            "openada.assertion/transfer.measurement.valid/v1alpha1",
            "org.openada.kernel.transfer-evidence",
            "transfer",
        ),
    ],
)
def test_measurement_envelope_rejects_malformed_typed_supporting_evidence(
    operation: str,
    operation_profile: str,
    assertion_profile: str,
    implementation_id: str,
    evidence_field: str,
) -> None:
    envelope = cli.measure_result(
        _typed_series(),
        {
            "measurement_id": "output.maximum",
            "kind": "maximum",
            "signal": "vout",
            "parameters": {},
            "extensions": {},
        },
    )
    envelope["operation"] = operation
    envelope["data"]["protocol"].update(
        operation_profile=operation_profile,
        assertion_profile=assertion_profile,
        implementation_id=implementation_id,
    )
    envelope["data"][evidence_field] = {}

    with pytest.raises(ValueError, match="packaged profile"):
        cli._measurement_record(envelope)


def test_operation_json_loader_rejects_duplicate_keys(tmp_path, capsys):
    series_path = tmp_path / "series.json"
    request_path = tmp_path / "measurement.json"
    series_path.write_text(json.dumps(_typed_series()), encoding="utf-8")
    request_path.write_text(
        '{"measurement_id":"first","measurement_id":"second"}',
        encoding="utf-8",
    )

    exit_code = main(
        [
            "measure",
            "--series",
            str(series_path),
            "--measurement",
            str(request_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["operation"] == "result.measure"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["data"]["measurement"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "measurement.request.invalid"
    assert "duplicate JSON object key" in payload["diagnostics"][0]["message"]


def test_operation_json_loader_rejects_non_regular_files_without_blocking(
    tmp_path,
    capsys,
):
    fifo_path = tmp_path / "series.fifo"
    request_path = tmp_path / "measurement.json"
    os.mkfifo(fifo_path)
    request_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "measure",
            "--series",
            str(fifo_path),
            "--measurement",
            str(request_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["data"]["measurement"]["status"] == "unknown"
    assert "regular file" in payload["diagnostics"][0]["message"]


def test_evaluate_rejects_a_three_field_pseudo_envelope(tmp_path, capsys):
    measurement_path = tmp_path / "measurement.json"
    specification_path = tmp_path / "specification.json"
    measurement_path.write_text(
        json.dumps(
            {
                "schema": "openada.result/v0alpha1",
                "operation": "result.measure",
                "data": {"measurement": {}},
            }
        ),
        encoding="utf-8",
    )
    specification_path.write_text("{}", encoding="utf-8")

    exit_code = main(
        [
            "evaluate",
            "--measurement",
            str(measurement_path),
            "--specification",
            str(specification_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["operation"] == "specification.evaluate"
    assert payload["data"]["evaluation"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "specification.request.invalid"


def test_spectral_cli_result_feeds_specification_evaluation(tmp_path, capsys):
    count = 8
    sample_rate = 8.0
    axis = {"name": "time", "unit": "s", "values": [i / sample_rate for i in range(count)]}
    signals = [{
        "name": "vout",
        "unit": "V",
        "values": [
            math.sin(2 * math.pi * i / count)
            + 0.1 * math.sin(2 * math.pi * 2 * i / count)
            + 0.01 * math.sin(2 * math.pi * 3 * i / count)
            for i in range(count)
        ],
    }]
    conditions = [{"name": "temperature", "value": 27.0, "unit": "degC"}]
    series = {
        "source": {
            "operation": "result.series.extract",
            "request_id": "00000000-0000-4000-8000-000000000010",
            "artifact_role": "measurement.source",
            "artifact_sha256": _canonical_digest(
                {"axis": axis, "signals": signals, "conditions": conditions}
            ),
        },
        "axis": axis,
        "signals": signals,
        "conditions": conditions,
        "extensions": {},
    }
    request = {
        "measurement_id": "vout.sfdr",
        "signal": "vout",
        "method": {
            "id": "openada.method/coherent-single-tone-fft/v1alpha1",
            "dft_length": count,
            "uniformity_relative_tolerance": 1e-12,
            "coherent_bin_tolerance": 1e-12,
            "coherent_sampling": "required",
            "window": "rectangular",
            "detrend": "mean",
            "sidedness": "one-sided",
            "scaling": "mean-square-per-bin",
            "averaging": "none",
            "missing_samples": "reject",
            "clipping": "not-assessed",
        },
        "band": {"lower": {"value": 0, "unit": "Hz"}, "upper": {"value": 4, "unit": "Hz"}},
        "fundamental": {"method": "declared-coherent-bin", "frequency": {"value": 1, "unit": "Hz"}, "integration_half_width_bins": 0},
        "harmonics": {"orders": [2], "aliasing": "fold-first-nyquist", "collision": "reject", "out_of_band": "exclude", "integration_half_width_bins": 0},
        "metric": {"kind": "sfdr", "unit": "dB"},
        "standards_context": {"domain": "generic-sampled-waveform", "reference": "none", "alignment": "openada-definition"},
        "extensions": {},
    }
    series_path = tmp_path / "series.json"
    request_path = tmp_path / "spectral.json"
    result_path = tmp_path / "spectral-result.json"
    specification_path = tmp_path / "specification.json"
    series_path.write_text(json.dumps(series), encoding="utf-8")
    request_path.write_text(json.dumps(request), encoding="utf-8")

    exit_code = main(["--compact", "spectral", "--series", str(series_path), "--measurement", str(request_path)])
    measured = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert measured["data"]["measurement"]["value"] == pytest.approx(20.0)
    result_path.write_text(json.dumps(measured), encoding="utf-8")
    specification_path.write_text(
        json.dumps({
            "specification_id": "vout.sfdr.limit",
            "measurement_id": "vout.sfdr",
            "limits": {"lower": {"value": 18.0, "unit": "dB", "inclusive": True}},
            "conditions": conditions,
            "extensions": {},
        }),
        encoding="utf-8",
    )

    evaluation_exit = main(["evaluate", "--measurement", str(result_path), "--specification", str(specification_path)])
    evaluated = json.loads(capsys.readouterr().out)

    assert evaluation_exit == 0
    assert evaluated["engineering"]["status"] == "pass"
