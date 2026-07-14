from __future__ import annotations

import json

import pytest

from openada.cli import main
from openada.contract import MAX_CONTRACT_TEXT_CHARS


def test_doctor_emits_one_contract_object(capsys):
    exit_code = main(["--compact", "doctor", "--tool", "ngspice"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema"] == "openada.result/v0alpha1"
    assert payload["operation"] == "doctor"
    assert set(payload["data"]["tools"]) == {"ngspice"}


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
    assert payload["diagnostics"][0]["code"] == "request.invalid"


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
