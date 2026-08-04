from __future__ import annotations

import json
from pathlib import Path

import openada.cli as cli
from openada.contract import result, static_execution


def _payload(*, execution_status: str, engineering_status: str) -> dict:
    return result(
        "experiment.run",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=engineering_status,
        summary="Synthetic experiment CLI result.",
        data={
            "schema": "simra.experiment-run/v1",
            "refusals": [],
            "manifest": None,
            "extensions": {},
        },
    )


def test_experiment_run_cli_dispatches_closed_arguments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    specification = tmp_path / "experiment.json"
    specification.write_text("{}\n", encoding="utf-8")
    pdk_root = tmp_path / "pdks"
    pdk_root.mkdir()
    output_dir = tmp_path / "evidence"
    captured: dict[str, object] = {}

    def fake_run(spec_path, destination, **kwargs):
        captured.update(
            {
                "spec_path": spec_path,
                "destination": destination,
                **kwargs,
            }
        )
        return _payload(
            execution_status="completed",
            engineering_status="pass",
        )

    monkeypatch.setattr(cli, "run_experiment", fake_run)
    exit_code = cli.main(
        [
            "--compact",
            "experiment",
            "run",
            str(specification),
            "--pdk",
            "ihp-sg13g2",
            "--pdk-root",
            str(pdk_root),
            "--output-dir",
            str(output_dir),
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert emitted["operation"] == "experiment.run"
    assert captured["spec_path"] == specification.resolve()
    assert captured["destination"] == output_dir.resolve()
    assert captured["pdk"] == "ihp-sg13g2"
    assert captured["pdk_root"] == pdk_root.resolve()


def test_experiment_refusal_has_a_nonzero_cli_exit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    specification = tmp_path / "experiment.json"
    specification.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "run_experiment",
        lambda *args, **kwargs: _payload(
            execution_status="invalid_request",
            engineering_status="unknown",
        ),
    )
    exit_code = cli.main(
        [
            "--compact",
            "experiment",
            "run",
            str(specification),
            "--pdk",
            "ihp-sg13g2",
            "--output-dir",
            str(tmp_path / "evidence"),
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert emitted["execution"]["status"] == "invalid_request"


def test_malformed_experiment_cli_is_typed_and_nonzero(capsys) -> None:
    exit_code = cli.main(["--compact", "experiment", "run"])
    emitted = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert emitted["operation"] == "experiment.run"
    assert emitted["diagnostics"][0]["code"] == "experiment.document.invalid"
    assert emitted["data"]["refusals"][0]["path"] == ""


def test_experiment_compile_cli_dispatches_closed_arguments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    template = tmp_path / "template.json"
    template.write_text("{}\n", encoding="utf-8")
    pdk_root = tmp_path / "pdks"
    pdk_root.mkdir()
    output_dir = tmp_path / "compiled"
    captured: dict[str, object] = {}

    def fake_compile(template_path, destination, **kwargs):
        captured.update(
            {
                "template_path": template_path,
                "destination": destination,
                **kwargs,
            }
        )
        return result(
            "experiment.compile",
            tool=None,
            execution=static_execution(),
            engineering_status="pass",
            summary="Synthetic compile CLI result.",
            data={
                "schema": "simra.experiment-template-compile/v1",
                "refusals": [],
                "receipt": {},
                "extensions": {},
            },
        )

    monkeypatch.setattr(cli, "compile_experiment_template", fake_compile)
    exit_code = cli.main(
        [
            "--compact",
            "experiment",
            "compile",
            str(template),
            "--pdk",
            "ihp-sg13g2",
            "--pdk-root",
            str(pdk_root),
            "--output-dir",
            str(output_dir),
            "--set",
            "vdd_nom=1.3",
            "--set",
            "temp_c=85",
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert emitted["operation"] == "experiment.compile"
    assert captured["template_path"] == template.resolve()
    assert captured["destination"] == output_dir.resolve()
    assert captured["pdk"] == "ihp-sg13g2"
    assert captured["pdk_root"] == pdk_root.resolve()
    assert captured["overrides"] == [("vdd_nom", "1.3"), ("temp_c", "85")]


def test_experiment_compile_malformed_set_is_typed_and_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    template = tmp_path / "template.json"
    template.write_text("{}\n", encoding="utf-8")
    exit_code = cli.main(
        [
            "--compact",
            "experiment",
            "compile",
            str(template),
            "--pdk",
            "ihp-sg13g2",
            "--output-dir",
            str(tmp_path / "compiled"),
            "--set",
            "vdd_nom",
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert emitted["operation"] == "experiment.compile"
    assert emitted["execution"]["status"] == "invalid_request"
    assert emitted["data"]["refusals"][0]["code"] == "template.document.invalid"
