from __future__ import annotations

import hashlib
import json
import os

import pytest

import openada.cli as cli
from openada.cli import main
from openada.contract import MAX_CONTRACT_TEXT_CHARS, result, static_execution


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
        (["evaluate"], "specification.evaluate"),
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
        "specification.evaluate": "specification.request.invalid",
    }.get(operation, "request.invalid")
    assert payload["diagnostics"][0]["code"] == expected_code
    if operation == "result.measure":
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
