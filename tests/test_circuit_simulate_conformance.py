from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "conformance" / "circuit-simulate-v0alpha2"
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


def _ac_rows(*, corrupt_output: bool = False) -> list[list[complex]]:
    rows = []
    for frequency in (10.0, 10_000.0):
        output = 1.0 / complex(1.0, 2.0 * math.pi * frequency * 1e-3)
        rows.append([complex(frequency, 0.0), complex(1.0, 0.0), output])
    if corrupt_output:
        rows[-1][-1] = complex(0.5, 0.5)
    return rows


def _ac_fixture_rows(axis: list[float]) -> list[list[complex]]:
    return [
        [
            complex(frequency, 0.0),
            complex(1.0, 0.0),
            1.0 / complex(1.0, 2.0 * math.pi * frequency * 1e-3),
        ]
        for frequency in axis
    ]


def _dc_fixture_rows(axis: list[float]) -> list[list[float]]:
    return [[sweep, sweep, sweep / 2.0] for sweep in axis]


def _analysis_counts(axis: list[float], *, complex_values: bool) -> dict[str, int]:
    dependent_variables = 2
    scalar_width = 2 if complex_values else 1
    return {
        "point_count": len(axis),
        "dependent_variable_count": dependent_variables,
        "finite_value_count": len(axis) * dependent_variables * scalar_width,
    }


def _ngspice_ac_raw(rows: list[list[complex]]) -> bytes:
    variables = ["frequency", "v(in)", "v(out)"]
    header = (
        "Title: synthetic RC AC proof\n"
        "Date: fixture\n"
        "Plotname: AC Analysis\n"
        "Flags: complex\n"
        f"No. Variables: {len(variables)}\n"
        f"No. Points: {len(rows)}\n"
        "Variables:\n"
        "\t0\tfrequency\tfrequency\n"
        "\t1\tv(in)\tvoltage\n"
        "\t2\tv(out)\tvoltage\n"
        "Binary:\n"
    ).encode("ascii")
    scalars = [scalar for row in rows for value in row for scalar in (value.real, value.imag)]
    return header + struct.pack(f"<{len(scalars)}d", *scalars)


def _xyce_ac_raw(rows: list[list[complex]]) -> bytes:
    lines = [
        "Title: synthetic RC AC proof",
        "Date: fixture",
        "Plotname: AC Analysis",
        "Flags: complex",
        "No. Variables: 3",
        f"No. Points: {len(rows)}",
        "Variables:",
        "\t0\tfrequency\tfrequency",
        "\t1\tIN\tvoltage",
        "\t2\tOUT\tvoltage",
        "Values:",
    ]
    for index, row in enumerate(rows):
        lines.append(f"{index}\t{row[0].real:.12e}, {row[0].imag:.12e}")
        lines.extend(f"\t{value.real:.12e}, {value.imag:.12e}" for value in row[1:])
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
    assert completed.stdout.strip() == (
        '{"conformance_id": "model-free-op-dc-ac-tran-ngspice-xyce-v0alpha2", '
        '"status": "pass"}'
    )

    manifest = VERIFY.load_manifest()
    assert manifest["contracts"]["operation_profile"]["id"] == (
        "openada.operation/circuit.simulate/v1alpha2"
    )
    assert manifest["contracts"]["operation_profile"]["repository_path"] == (
        "profiles/circuit.simulate-v1alpha2.json"
    )
    assert set(manifest["capability_cases"]) == {"op", "dc", "ac"}
    assert set(manifest["capability_cases"]["op"]["backends"]) == {"ngspice"}
    assert set(manifest["capability_cases"]["dc"]["backends"]) == {
        "ngspice",
        "xyce",
    }
    assert set(manifest["capability_cases"]["ac"]["backends"]) == {
        "ngspice",
        "xyce",
    }


def test_manifest_rejects_operation_profile_digest_mismatch(tmp_path: Path):
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    manifest["contracts"]["operation_profile"]["sha256"] = "0" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        VERIFY.ConformanceError,
        match=r"manifest\.contracts\.operation_profile\.sha256",
    ):
        VERIFY.load_manifest(manifest_path)


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


def test_independent_complex_raw_parsers_agree_on_ac_fixture_semantics(tmp_path: Path):
    rows = _ac_rows()
    ngspice_path = tmp_path / "ngspice-ac.raw"
    xyce_path = tmp_path / "xyce-ac.raw"
    ngspice_path.write_bytes(_ngspice_ac_raw(rows))
    xyce_path.write_bytes(_xyce_ac_raw(rows))
    parameters = {
        "analysis": {
            "type": "ac",
            "sweep": "lin",
            "points": 2,
            "start_hz": 10.0,
            "stop_hz": 10_000.0,
            "extensions": {},
        },
        "extensions": {},
    }
    expected = {
        "point_count": 2,
        "dependent_variable_count": 2,
        "finite_value_count": 8,
    }

    ngspice = VERIFY.verify_analysis_fixture(
        *VERIFY.parse_ngspice_binary(ngspice_path, analysis_type="ac"),
        parameters,
        expected,
        backend="ngspice",
    )
    xyce = VERIFY.verify_analysis_fixture(
        *VERIFY.parse_xyce_ascii(xyce_path, analysis_type="ac"),
        parameters,
        expected,
        backend="xyce",
    )

    assert ngspice == xyce == {
        "analysis": "ac",
        "points": 2,
        "axis_first": 10.0,
        "axis_last": 10_000.0,
    }


def test_independent_ac_verifier_rejects_transfer_function_tampering(tmp_path: Path):
    raw_path = tmp_path / "bad-ac.raw"
    raw_path.write_bytes(_xyce_ac_raw(_ac_rows(corrupt_output=True)))
    variables, rows = VERIFY.parse_xyce_ascii(raw_path, analysis_type="ac")

    with pytest.raises(VERIFY.ConformanceError, match="transfer function"):
        VERIFY.verify_analysis_fixture(
            variables,
            rows,
            {
                "analysis": {
                    "type": "ac",
                    "sweep": "lin",
                    "points": 2,
                    "start_hz": 10.0,
                    "stop_hz": 10_000.0,
                    "extensions": {},
                },
                "extensions": {},
            },
            {
                "point_count": 2,
                "dependent_variable_count": 2,
                "finite_value_count": 8,
            },
            backend="xyce",
        )


def test_independent_dc_verifier_accepts_exact_non_aligned_grid():
    axis = [0.0, 0.3, 0.6, 0.9]
    result = VERIFY.verify_analysis_fixture(
        ["sweep", "v(in)", "v(out)"],
        _dc_fixture_rows(axis),
        {
            "analysis": {
                "type": "dc",
                "source_name": "VSWEEP",
                "source_unit": "V",
                "start": 0.0,
                "stop": 1.0,
                "step": 0.3,
                "extensions": {},
            },
            "extensions": {},
        },
        _analysis_counts(axis, complex_values=False),
        backend="ngspice",
    )

    assert result == {
        "analysis": "dc",
        "points": 4,
        "axis_first": 0.0,
        "axis_last": 0.9,
    }


def test_independent_dc_verifier_rejects_arbitrary_monotonic_interior():
    axis = [0.0, 0.3, 0.65, 0.9]

    with pytest.raises(VERIFY.ConformanceError, match="DC sweep grid differs"):
        VERIFY.verify_analysis_fixture(
            ["sweep", "v(in)", "v(out)"],
            _dc_fixture_rows(axis),
            {
                "analysis": {
                    "type": "dc",
                    "source_name": "VSWEEP",
                    "source_unit": "V",
                    "start": 0.0,
                    "stop": 1.0,
                    "step": 0.3,
                    "extensions": {},
                },
                "extensions": {},
            },
            _analysis_counts(axis, complex_values=False),
            backend="ngspice",
        )


def _reviewed_ac_axis(backend: str, sweep: str) -> tuple[dict[str, object], list[float]]:
    if sweep == "lin":
        analysis: dict[str, object] = {
            "type": "ac",
            "sweep": "lin",
            "points": 5,
            "start_hz": 10.0,
            "stop_hz": 650.0,
            "extensions": {},
        }
        return analysis, [10.0, 170.0, 330.0, 490.0, 650.0]

    points = 3
    start = 10.0
    stop = 650.0 if sweep == "dec" else 100.0
    base = 10.0 if sweep == "dec" else 2.0
    intervals = math.floor(points * math.log(stop / start, base))
    if sweep == "dec" and backend == "ngspice":
        ratio = (stop / start) ** (1.0 / intervals)
    else:
        ratio = base ** (1.0 / points)
    analysis = {
        "type": "ac",
        "sweep": sweep,
        "points": points,
        "start_hz": start,
        "stop_hz": stop,
        "extensions": {},
    }
    return analysis, [start * ratio**index for index in range(intervals + 1)]


@pytest.mark.parametrize("backend", ["ngspice", "xyce"])
@pytest.mark.parametrize("sweep", ["lin", "dec", "oct"])
def test_independent_ac_verifier_accepts_backend_specific_grid(
    backend: str,
    sweep: str,
):
    analysis, axis = _reviewed_ac_axis(backend, sweep)

    result = VERIFY.verify_analysis_fixture(
        ["frequency", "v(in)", "v(out)"],
        _ac_fixture_rows(axis),
        {"analysis": analysis, "extensions": {}},
        _analysis_counts(axis, complex_values=True),
        backend=backend,
    )

    assert result["analysis"] == "ac"
    assert result["points"] == len(axis)
    assert math.isclose(result["axis_first"], axis[0])
    assert math.isclose(result["axis_last"], axis[-1])


@pytest.mark.parametrize("backend", ["ngspice", "xyce"])
@pytest.mark.parametrize("sweep", ["lin", "dec", "oct"])
def test_independent_ac_verifier_rejects_arbitrary_monotonic_interior(
    backend: str,
    sweep: str,
):
    analysis, axis = _reviewed_ac_axis(backend, sweep)
    tampered = list(axis)
    middle = len(tampered) // 2
    tampered[middle] = tampered[middle - 1] + 0.35 * (
        tampered[middle + 1] - tampered[middle - 1]
    )

    with pytest.raises(VERIFY.ConformanceError, match="AC sweep grid differs"):
        VERIFY.verify_analysis_fixture(
            ["frequency", "v(in)", "v(out)"],
            _ac_fixture_rows(tampered),
            {"analysis": analysis, "extensions": {}},
            _analysis_counts(tampered, complex_values=True),
            backend=backend,
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
