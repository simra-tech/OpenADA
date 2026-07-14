from __future__ import annotations

import base64
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from openada.cli import main


ROOT = Path(__file__).parents[1]
PROFILE = json.loads(
    (ROOT / "profiles" / "circuit.simulate-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
FIXTURE = (
    ROOT
    / "conformance"
    / "circuit-simulate"
    / "fixtures"
    / "rc-transient.cir"
)
VALID_RAW = (
    "Title: shared profile fixture\n"
    "Date: fixture\n"
    "Plotname: Transient Analysis\n"
    "Flags: real\n"
    "No. Variables: 2\n"
    "No. Points: 2\n"
    "Variables:\n"
    "\t0\ttime\ttime\n"
    "\t1\tv(out)\tvoltage\n"
    "Values:\n"
    " 0\t0.0\n"
    "\t0.0\n"
    "\n"
    " 1\t2e-6\n"
    "\t0.8646647168\n"
    "\n"
).encode("ascii")


def _fake_simulator(path: Path, *, backend: str) -> None:
    raw = base64.b64encode(VALID_RAW).decode("ascii")
    if backend == "ngspice":
        version = "ngspice-45.2"
        run = f"""
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
result = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log.write_text('No. of Data Rows : 2\\n', encoding='utf-8')
result.write_bytes(base64.b64decode({raw!r}))
"""
    else:
        version = "Xyce Release 7.10.0-opensource"
        run = f"""
assert os.environ.get('XYCE_NO_TRACKING') == '1'
log = pathlib.Path(sys.argv[sys.argv.index('-l') + 1])
result = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log.write_text('***** End of Xyce(TM) Simulation *****\\n', encoding='utf-8')
result.write_bytes(base64.b64decode({raw!r}))
"""
    path.write_text(
        f"""#!/usr/bin/env python3
import base64
import os
import pathlib
import sys
if len(sys.argv) == 2 and sys.argv[1] in {{'-v', '--version'}}:
    print({version!r})
    raise SystemExit(0)
{run}
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_backend(tmp_path: Path, capsys, backend: str) -> dict:
    binary = tmp_path / backend / ("Xyce" if backend == "xyce" else "ngspice")
    binary.parent.mkdir()
    _fake_simulator(binary, backend=backend)
    exit_code = main(
        [
            "--compact",
            "--tool-path",
            f"{backend}={binary}",
            "simulate",
            str(FIXTURE),
            "--backend",
            backend,
            "--output-dir",
            str(tmp_path / f"{backend}-evidence"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    return payload


def test_same_request_semantics_produce_the_same_normalized_decision_facts(
    tmp_path: Path,
    capsys,
) -> None:
    results = {
        backend: _run_backend(tmp_path, capsys, backend)
        for backend in ("ngspice", "xyce")
    }

    validator = Draft202012Validator(
        PROFILE["normalized_result"]["data_schema"],
        format_checker=FormatChecker(),
    )
    for backend, payload in results.items():
        validator.validate(payload["data"])
        assert payload["engineering"]["status"] == "pass"
        assert payload["data"]["protocol"]["driver_id"].endswith(backend)
        assert payload["data"]["extensions"]["org.openada"]["backend"] == backend

    assert results["ngspice"]["data"]["analysis"] == results["xyce"]["data"]["analysis"]
    assert results["ngspice"]["data"]["evidence"] == results["xyce"]["data"]["evidence"]
    assert results["ngspice"]["execution"]["command"] != results["xyce"]["execution"]["command"]
    assert {item["kind"] for item in results["ngspice"]["artifacts"]} != {
        item["kind"] for item in results["xyce"]["artifacts"]
    }


def test_explicit_profile_never_silently_ignores_legacy_ngspice_options(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = main(
        [
            "simulate",
            str(FIXTURE),
            "--backend",
            "xyce",
            "--execution-mode",
            "batch",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "request.invalid"


def test_legacy_hidden_tool_selector_cannot_conflict_with_explicit_backend(
    capsys,
) -> None:
    exit_code = main(
        [
            "simulate",
            str(FIXTURE),
            "--backend",
            "xyce",
            "--tool",
            "ngspice",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "request.invalid"
