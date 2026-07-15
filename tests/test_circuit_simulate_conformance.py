from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "conformance" / "circuit-simulate"
VERIFY_PATH = BUNDLE / "verify.py"
RUN_PATH = BUNDLE / "run.py"


def _load_verifier():
    specification = importlib.util.spec_from_file_location(
        "openada_circuit_simulate_verify",
        VERIFY_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VERIFY = _load_verifier()


def _rows(*, corrupt_output: bool = False) -> list[list[float]]:
    times = [0.0, 0.5e-6, 1.0e-6, 1.5e-6, 2.0e-6]
    inputs = [0.0, 1.0, 1.0, 1.0, 1.0]
    outputs = [0.0, 0.39346934, 0.63212056, 0.77686984, 0.86466472]
    if corrupt_output:
        outputs[3] = 0.2
    return [
        [time, vin, vout, -(vin - vout) / 1000.0]
        for time, vin, vout in zip(times, inputs, outputs)
    ]


def _ngspice_raw(rows: list[list[float]]) -> bytes:
    variables = ["time", "v(in)", "v(out)", "i(vstep)"]
    header = (
        "Title: synthetic RC portability proof\n"
        "Date: fixture\n"
        "Command: ngspice synthetic\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        f"No. Variables: {len(variables)}\n"
        f"No. Points: {len(rows)}\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tv(in)\tvoltage\n"
        "\t2\tv(out)\tvoltage\n"
        "\t3\ti(vstep)\tcurrent\n"
        "Binary:\n"
    ).encode("ascii")
    flat = [value for row in rows for value in row]
    return header + struct.pack(f"<{len(flat)}d", *flat)


def _xyce_raw(rows: list[list[float]]) -> bytes:
    lines = [
        "Title: synthetic RC portability proof",
        "Date: fixture",
        "Plotname: Transient Analysis",
        "Flags: real",
        "No. Variables: 4",
        f"No. Points: {len(rows)}",
        "Variables:",
        "\t0\ttime\ttime",
        "\t1\tIN\tvoltage",
        "\t2\tOUT\tvoltage",
        "\t3\tVSTEP#branch\tcurrent",
        "Values:",
    ]
    for index, row in enumerate(rows):
        lines.append(f"{index}\t{row[0]:.12e}")
        lines.extend(f"\t{value:.12e}" for value in row[1:])
        lines.append("")
    return ("\n".join(lines) + "\n").encode("ascii")


def test_pinned_portability_manifest_is_self_consistent():
    completed = subprocess.run(
        [sys.executable, str(VERIFY_PATH), "--manifest-only"],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "model-free-rc-transient-ngspice-xyce" in completed.stdout


def test_independent_native_raw_parsers_agree_on_rc_semantics(tmp_path: Path):
    manifest = VERIFY.load_manifest()
    rows = _rows()
    ngspice_path = tmp_path / "ngspice.raw"
    xyce_path = tmp_path / "xyce.raw"
    ngspice_path.write_bytes(_ngspice_raw(rows))
    xyce_path.write_bytes(_xyce_raw(rows))

    ngspice_variables, ngspice_rows = VERIFY.parse_ngspice_binary(ngspice_path)
    xyce_variables, xyce_rows = VERIFY.parse_xyce_ascii(xyce_path)
    ngspice = VERIFY.verify_rc_waveform(
        ngspice_variables,
        ngspice_rows,
        manifest["waveform"],
        backend="ngspice",
    )
    xyce = VERIFY.verify_rc_waveform(
        xyce_variables,
        xyce_rows,
        manifest["waveform"],
        backend="xyce",
    )

    assert math.isclose(ngspice["final_output"], xyce["final_output"])
    assert ngspice["points"] == xyce["points"] == len(rows)


def test_independent_verifier_rejects_non_rc_waveform(tmp_path: Path):
    manifest = VERIFY.load_manifest()
    raw_path = tmp_path / "bad.raw"
    raw_path.write_bytes(_xyce_raw(_rows(corrupt_output=True)))
    variables, rows = VERIFY.parse_xyce_ascii(raw_path)

    with pytest.raises(VERIFY.ConformanceError, match="not monotonic"):
        VERIFY.verify_rc_waveform(
            variables,
            rows,
            manifest["waveform"],
            backend="xyce",
        )


def test_independent_verifier_rejects_branch_current_tampering(tmp_path: Path):
    manifest = VERIFY.load_manifest()
    rows = _rows()
    rows[2][3] = 0.0
    raw_path = tmp_path / "bad-current.raw"
    raw_path.write_bytes(_ngspice_raw(rows))
    variables, parsed_rows = VERIFY.parse_ngspice_binary(raw_path)

    with pytest.raises(VERIFY.ConformanceError, match="branch relation"):
        VERIFY.verify_rc_waveform(
            variables,
            parsed_rows,
            manifest["waveform"],
            backend="ngspice",
        )


@pytest.mark.conformance
@pytest.mark.skipif(
    os.environ.get("OPENADA_RUN_CIRCUIT_SIMULATE_CONFORMANCE") != "1",
    reason="set OPENADA_RUN_CIRCUIT_SIMULATE_CONFORMANCE=1 for the pinned Docker replay",
)
def test_native_ngspice_xyce_portability_replay(tmp_path: Path):
    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_PATH),
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert '"status": "pass"' in completed.stdout
