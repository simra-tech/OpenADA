from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from openada.cli import main
from openada.engines.xyce import XyceDriver


OPERATION_PROFILE = "openada.operation/circuit.simulate/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/simulation.evidence.valid/v1alpha1"
XYCE_DRIVER_ID = "org.openada.driver.xyce"
PUBLIC_TRANSIENT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "conformance"
    / "circuit-simulate"
    / "fixtures"
    / "rc-transient.cir"
)


VALID_TRANSIENT_RAW = (
    "Title: OpenADA Xyce fixture\n"
    "Date: fixture\n"
    "Plotname: Transient Analysis\n"
    "Flags: real\n"
    "No. Variables: 2\n"
    "No. Points: 3\n"
    "Variables:\n"
    "\t0\ttime\ttime\n"
    "\t1\tv(out)\tvoltage\n"
    "Values:\n"
    " 0\t0.0\n"
    "\t0.0\n"
    "\n"
    " 1\t1e-6\n"
    "\t0.6321205588\n"
    "\n"
    " 2\t2e-6\n"
    "\t0.8646647168\n"
    "\n"
).encode("ascii")


def _write_fake_xyce(
    path: Path,
    *,
    raw: bytes = VALID_TRANSIENT_RAW,
    write_raw: bool = True,
    log: str = "***** End of Xyce(TM) Simulation *****\n",
    exit_code: int = 0,
    launch_marker: Path | None = None,
) -> None:
    encoded_raw = base64.b64encode(raw).decode("ascii")
    body = f"""#!/usr/bin/env python3
import base64
import os
import pathlib
import sys

if len(sys.argv) == 2 and sys.argv[1] in {{'-v', '--version'}}:
    print('Xyce Release 7.9')
    raise SystemExit(0)

if {str(launch_marker) if launch_marker is not None else None!r} is not None:
    pathlib.Path({str(launch_marker) if launch_marker is not None else None!r}).write_text(
        'launched', encoding='utf-8'
    )

if (
    len(sys.argv) != 7
    or sys.argv[1] != '-l'
    or sys.argv[3] != '-r'
    or sys.argv[5] != '-a'
):
    raise SystemExit(91)

log_path = pathlib.Path(sys.argv[2])
raw_path = pathlib.Path(sys.argv[4])
source_path = pathlib.Path(sys.argv[6])
if not source_path.is_file():
    raise SystemExit(92)

if os.environ.get('XYCE_NO_TRACKING') != '1':
    log_path.write_text('OPENADA_FAKE: XYCE_NO_TRACKING was not 1\\n', encoding='utf-8')
    raise SystemExit(93)

log_path.write_text({log!r}, encoding='utf-8')
if {write_raw!r}:
    raw_path.write_bytes(base64.b64decode({encoded_raw!r}))
raise SystemExit({exit_code})
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _transient_source(tmp_path: Path) -> Path:
    source = tmp_path / "rc-transient.cir"
    source.write_bytes(PUBLIC_TRANSIENT_FIXTURE.read_bytes())
    return source


def _diagnostic_codes(payload: dict) -> set[str]:
    return {item["code"] for item in payload["diagnostics"]}


def test_xyce_pass_uses_exact_ascii_raw_invocation_and_normalizes_native_evidence(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "Xyce"
    _write_fake_xyce(binary)
    source = _transient_source(tmp_path)

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    command = payload["execution"]["command"]
    assert payload["operation"] == "simulate"
    assert payload["tool"]["name"] == "xyce"
    assert payload["tool"]["path"] == str(binary.resolve())
    assert payload["tool"]["version"] == "Xyce Release 7.9"
    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "pass"

    assert len(command) == 7
    assert command[0] == str(binary.resolve())
    assert command[1] == "-l"
    assert Path(command[2]).name == "simulation.log"
    assert command[3] == "-r"
    assert Path(command[4]).name == "simulation.raw"
    assert command[5:] == ["-a", str(source.resolve())]
    assert Path(command[2]).is_absolute()
    assert Path(command[4]).is_absolute()
    assert Path(command[2]).parent == Path(command[4]).parent

    source_record = next(item for item in payload["inputs"] if item["kind"] == "spice-netlist")
    source_bytes = source.read_bytes()
    assert source_record["bytes"] == len(source_bytes)
    assert source_record["sha256"] == hashlib.sha256(source_bytes).hexdigest()

    data = payload["data"]
    assert data["analysis"]["type"] == "tran"
    assert data["converged"] is True
    assert data["inputs_stable"] is True
    assert data["analysis_evidence"]["raw"] is True
    assert data["log_capture"]["status"] == "valid"
    assert len(data["output_captures"]) == 1
    capture = data["output_captures"][0]
    assert capture["status"] == "valid"
    assert capture["validation"]["valid"] is True
    assert capture["validation"]["metadata"]["format"] == "xyce-raw"
    assert capture["validation"]["metadata"]["analysis_plot_count"] == 1
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "xyce-log",
        "xyce-raw",
    }


def test_xyce_terminal_nonconvergence_is_engineering_fail(tmp_path: Path) -> None:
    binary = tmp_path / "Xyce"
    _write_fake_xyce(
        binary,
        write_raw=False,
        log="Time step too small near step number: 12 Exiting transient loop.\n",
        exit_code=1,
    )
    source = _transient_source(tmp_path)

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 1
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["analysis"]["type"] == "tran"
    assert payload["data"]["converged"] is False
    assert "simulation.analysis.non_convergent" in _diagnostic_codes(payload)


def test_xyce_nonzero_parse_error_is_unknown_not_engineering_fail(tmp_path: Path) -> None:
    binary = tmp_path / "Xyce"
    _write_fake_xyce(
        binary,
        write_raw=False,
        log="Netlist parse error: unexpected token on line 4\n",
        exit_code=2,
    )
    source = _transient_source(tmp_path)

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 2
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["converged"] is None
    assert "simulation.analysis.unproven" in _diagnostic_codes(payload)
    assert "simulation.analysis.non_convergent" not in _diagnostic_codes(payload)


@pytest.mark.parametrize(
    ("write_raw", "raw", "expected_code"),
    [
        (False, b"", "simulation.result.missing"),
        (True, b"not a Spice raw file\n", "simulation.result.malformed"),
    ],
    ids=["missing", "malformed"],
)
def test_xyce_end_marker_cannot_upgrade_missing_or_malformed_raw_to_pass(
    tmp_path: Path,
    write_raw: bool,
    raw: bytes,
    expected_code: str,
) -> None:
    binary = tmp_path / "Xyce"
    _write_fake_xyce(
        binary,
        write_raw=write_raw,
        raw=raw,
        log="***** End of Xyce(TM) Simulation *****\n",
        exit_code=0,
    )
    source = _transient_source(tmp_path)

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["converged"] is None
    assert payload["data"]["analysis_evidence"]["raw"] is False
    assert expected_code in _diagnostic_codes(payload)


@pytest.mark.parametrize(
    "analysis",
    [
        "",
        ".op\n",
        ".dc VSTEP 0 1 0.1\n",
        ".ac dec 10 1 1e6\n",
        ".tran 100n 2u\n.ac dec 10 1 1e6\n",
    ],
    ids=["missing", "op", "dc", "ac", "multiple"],
)
def test_xyce_rejects_analysis_outside_initial_transient_capability(
    tmp_path: Path,
    analysis: str,
) -> None:
    binary = tmp_path / "Xyce"
    marker = tmp_path / "launched"
    _write_fake_xyce(binary, launch_marker=marker)
    source = tmp_path / "unsupported.cir"
    source.write_text(
        "Unsupported analysis fixture\n"
        "V1 in 0 1\n"
        "R1 in 0 1k\n"
        f"{analysis}"
        ".end\n",
        encoding="utf-8",
    )

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {"simulation.analysis.unsupported"}
    assert not marker.exists()


@pytest.mark.parametrize(
    ("directive", "expected_code"),
    [
        (".include models.lib", "input.transitive_uninspected"),
        (".inc models.lib", "input.transitive_uninspected"),
        (".lib models.lib TT", "input.transitive_uninspected"),
        (".control\nrun\n.endc", "simulation.feature.unsupported"),
        (
            ".measure tran rise_time trig v(in) val=0.1 rise=1 "
            "targ v(out) val=0.9 rise=1",
            "simulation.feature.unsupported",
        ),
        (".meas tran peak max v(out)", "simulation.feature.unsupported"),
        (".print tran v(out)", "simulation.feature.unsupported"),
    ],
    ids=["include", "inc", "lib", "control", "measure", "meas", "print"],
)
def test_xyce_rejects_unbounded_deck_owned_semantics_before_launch(
    tmp_path: Path,
    directive: str,
    expected_code: str,
) -> None:
    binary = tmp_path / "Xyce"
    marker = tmp_path / "launched"
    _write_fake_xyce(binary, launch_marker=marker)
    source = tmp_path / "unsupported.cir"
    source.write_text(
        "Unsupported deck semantics fixture\n"
        "V1 in 0 1\n"
        "R1 in 0 1k\n"
        ".tran 100n 2u\n"
        f"{directive}\n"
        ".end\n",
        encoding="utf-8",
    )

    payload = XyceDriver(str(binary)).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {expected_code}
    assert not marker.exists()


def test_xyce_missing_binary_is_not_available_and_unknown(tmp_path: Path) -> None:
    source = _transient_source(tmp_path)

    payload = XyceDriver(str(tmp_path / "missing-Xyce")).simulate(
        source,
        tmp_path / "evidence",
    )

    assert payload["tool"]["name"] == "xyce"
    assert payload["tool"]["path"] is None
    assert payload["execution"]["status"] == "not_available"
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {"simulation.tool.unavailable"}
    assert payload["data"]["analysis"]["type"] == "tran"


def test_xyce_cli_emits_closed_circuit_simulate_profile_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binary = tmp_path / "Xyce"
    _write_fake_xyce(binary)
    source = _transient_source(tmp_path)

    exit_code = main(
        [
            "--compact",
            "--tool-path",
            f"xyce={binary}",
            "simulate",
            str(source),
            "--backend",
            "xyce",
            "--output-dir",
            str(tmp_path / "evidence"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["engineering"]["status"] == "pass"

    data = payload["data"]
    assert set(data) == {"protocol", "analysis", "evidence", "extensions"}
    UUID(data["protocol"]["request_id"])
    assert data["protocol"]["operation_profile"] == OPERATION_PROFILE
    assert data["protocol"]["assertion_profile"] == ASSERTION_PROFILE
    assert data["protocol"]["driver_id"] == XYCE_DRIVER_ID
    assert data["protocol"]["driver_version"]
    assert data["analysis"]["type"] == "tran"
    assert data["analysis"]["completion"] == "completed"
    assert data["analysis"]["convergence"] == "converged"
    assert data["analysis"]["point_count"] >= 1
    assert data["analysis"]["dependent_variable_count"] >= 1
    assert data["analysis"]["finite_value_count"] >= 1
    assert data["evidence"]["request_binding"] == "exact"
    assert data["evidence"]["freshness"] == "fresh"
    assert data["evidence"]["structure"] == "valid"
    assert set(data["evidence"]["artifact_roles_present"]) >= {
        "simulation.result",
        "simulation.log",
    }
    assert data["evidence"]["provenance"] in {"bounded", "incomplete"}
    assert data["evidence"]["provenance_limitations"]
    assert set(data["extensions"]) == {"org.openada"}
    assert data["extensions"]["org.openada"]["backend"] == "xyce"
    assert data["extensions"]["org.openada"]["native_data"]["inputs_stable"] is True
