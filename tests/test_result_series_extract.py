from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.contract import result, static_execution, tool_record
from openada.engines.ngspice_outputs import extract_analysis_raw
from openada.operations import extract_result_series, measure_result
from openada.operations.result_series_extract import (
    ASSERTION_PROFILE,
    IMPLEMENTATION_ID,
    OPERATION_PROFILE,
)


REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
SIMULATION_REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOT = Path(__file__).parents[1]
SERIES_PROFILE = json.loads(
    (ROOT / "profiles" / "result.series.extract-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
SERIES_DATA_VALIDATOR = Draft202012Validator(
    SERIES_PROFILE["normalized_result"]["data_schema"],
    format_checker=FormatChecker(),
)


def _raw_header(
    *,
    plotname: str,
    numeric_type: str,
    points: int,
    variables: list[tuple[str, str]],
    marker: str,
) -> bytes:
    table = "".join(
        f"\t{index}\t{name}\t{native_type}\n"
        for index, (name, native_type) in enumerate(variables)
    )
    return (
        "Title: result.series.extract fixture\n"
        "Date: fixture\n"
        f"Plotname: {plotname}\n"
        f"Flags: {numeric_type}\n"
        f"No. Variables: {len(variables)}\n"
        f"No. Points: {points}\n"
        "Variables:\n"
        f"{table}"
        f"{marker}:\n"
    ).encode("ascii")


def _binary_raw(
    *,
    plotname: str,
    numeric_type: str,
    variables: list[tuple[str, str]],
    rows: list[list[float | complex]],
) -> bytes:
    scalars: list[float] = []
    for row in rows:
        for value in row:
            if numeric_type == "complex":
                selected = complex(value)
                scalars.extend((selected.real, selected.imag))
            else:
                scalars.append(float(value))
    return _raw_header(
        plotname=plotname,
        numeric_type=numeric_type,
        points=len(rows),
        variables=variables,
        marker="Binary",
    ) + struct.pack(f"={len(scalars)}d", *scalars)


def _ascii_raw(
    *,
    plotname: str,
    numeric_type: str,
    variables: list[tuple[str, str]],
    rows: list[list[float | complex]],
) -> bytes:
    lines: list[str] = []
    for point, row in enumerate(rows):
        encoded: list[str] = []
        for value in row:
            if numeric_type == "complex":
                selected = complex(value)
                encoded.append(f"({selected.real:.17g},{selected.imag:.17g})")
            else:
                encoded.append(f"{float(value):.17g}")
        lines.append(f"{point}\t{encoded[0]}")
        lines.extend(f"\t{value}" for value in encoded[1:])
        lines.append("")
    return _raw_header(
        plotname=plotname,
        numeric_type=numeric_type,
        points=len(rows),
        variables=variables,
        marker="Values",
    ) + ("\n".join(lines) + "\n").encode("ascii")


def _write_raw(path: Path, body: bytes) -> dict[str, object]:
    path.write_bytes(body)
    return {
        "kind": "ngspice-raw" if path.suffix == ".raw" else "xyce-raw",
        "role": "simulation.result",
        "path": str(path.resolve()),
        "exists": True,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _simulation_result(
    artifact: dict[str, object],
    *,
    backend: str,
    analysis: dict[str, object],
    points: int,
    dependent_variables: int,
    finite_values: int,
) -> dict:
    driver_id = f"org.openada.driver.{backend}"
    log_artifact = {
        "kind": "simulation-log" if backend == "ngspice" else "xyce-log",
        "role": "simulation.log",
        "path": str(Path(str(artifact["path"])).with_suffix(".log")),
        "exists": True,
        "bytes": 1,
        "sha256": hashlib.sha256(b"\n").hexdigest(),
    }
    payload = result(
        "simulate",
        tool=tool_record(backend, path=f"/tools/{backend}", version=f"{backend} fixture"),
        execution=static_execution("completed"),
        engineering_status="pass",
        summary="The fixture produced exact native evidence.",
        artifacts=[log_artifact, artifact],
        data={
            "protocol": {
                "request_id": SIMULATION_REQUEST_ID,
                "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
                "assertion_profile": (
                    "openada.assertion/simulation.evidence.valid/v1alpha1"
                ),
                "driver_id": driver_id,
                "driver_version": "0.3.0",
            },
            "analysis": {
                "type": analysis["type"],
                "completion": "completed",
                "convergence": "converged",
                "point_count": points,
                "dependent_variable_count": dependent_variables,
                "finite_value_count": finite_values,
                "extensions": {},
            },
            "evidence": {
                "request_binding": "exact",
                "freshness": "fresh",
                "structure": "valid",
                "artifact_roles_present": ["simulation.log", "simulation.result"],
                "provenance": "bounded",
                "provenance_limitations": ["fixture runtime"],
                "extensions": {},
            },
            "extensions": {
                "org.openada": {
                    "backend": backend,
                    "parameters": {"analysis": analysis, "extensions": {}},
                    "native_data": {},
                    "native_diagnostics": [],
                }
            },
        },
    )
    return payload


def _transient_case(
    tmp_path: Path,
    backend: str,
    *,
    plotname: str = "Transient Analysis",
) -> tuple[Path, dict]:
    if backend == "ngspice":
        path = tmp_path / "transient.raw"
        variables = [("time", "time"), ("v(out)", "voltage")]
        body = _binary_raw(
            plotname=plotname,
            numeric_type="real",
            variables=variables,
            rows=[[0.0, 0.0], [1e-6, 0.5], [2e-6, 1.0]],
        )
    else:
        path = tmp_path / "transient.xyce"
        variables = [("time", "time"), ("OUT", "voltage")]
        body = _ascii_raw(
            plotname=plotname,
            numeric_type="real",
            variables=variables,
            rows=[[0.0, 0.0], [1e-6, 0.5], [2e-6, 1.0]],
        )
    artifact = _write_raw(path, body)
    if backend == "xyce":
        artifact["kind"] = "xyce-raw"
    analysis = {
        "type": "tran",
        "step_s": 1e-6,
        "stop_s": 2e-6,
        "extensions": {},
    }
    return path, _simulation_result(
        artifact,
        backend=backend,
        analysis=analysis,
        points=3,
        dependent_variables=1,
        finite_values=3,
    )


def test_extracts_exact_ngspice_linearized_transient_plot(tmp_path: Path) -> None:
    raw_path, simulation = _transient_case(
        tmp_path,
        "ngspice",
        plotname="Transient Analysis (linearized)",
    )

    extracted = extract_result_series(
        simulation,
        raw_path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
        request_id=REQUEST_ID,
    )

    assert extracted["engineering"]["status"] == "pass"
    assert extracted["data"]["extraction"]["plot"]["plotname"] == (
        "Transient Analysis (linearized)"
    )


def test_conformance_backed_external_ngspice_provider_can_feed_extraction(
    tmp_path: Path,
) -> None:
    raw_path, simulation = _transient_case(tmp_path, "ngspice")
    simulation["data"]["protocol"].update(
        {
            "driver_id": "org.openada.driver.ngspice-pdk-control",
            "driver_version": "0.4.0",
        }
    )

    extracted = extract_result_series(
        simulation,
        raw_path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
        request_id=REQUEST_ID,
    )

    assert extracted["engineering"]["status"] == "pass"
    assert extracted["data"]["extraction"]["source"]["driver_id"] == (
        "org.openada.driver.ngspice-pdk-control"
    )
    assert list(SERIES_DATA_VALIDATOR.iter_errors(extracted["data"])) == []


@pytest.mark.parametrize(
    ("backend", "native_name", "encoding"),
    [("ngspice", "v(out)", "binary"), ("xyce", "OUT", "ascii")],
)
def test_extracts_measurement_compatible_real_series(
    tmp_path: Path,
    backend: str,
    native_name: str,
    encoding: str,
) -> None:
    path, simulation = _transient_case(tmp_path, backend)

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": native_name,
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
        conditions=[{"name": "temperature", "value": 27, "unit": "degC"}],
        request_id=REQUEST_ID,
    )

    assert payload["operation"] == "result.series.extract"
    assert payload["engineering"]["status"] == "pass"
    assert payload["execution"]["status"] == "completed"
    assert payload["diagnostics"] == []
    assert not list(SERIES_DATA_VALIDATOR.iter_errors(payload["data"]))
    data = payload["data"]
    assert data["protocol"] == {
        "request_id": REQUEST_ID,
        "operation_profile": OPERATION_PROFILE,
        "assertion_profile": ASSERTION_PROFILE,
        "implementation_id": IMPLEMENTATION_ID,
        "implementation_version": "1.0.0",
    }
    extraction = data["extraction"]
    assert extraction["status"] == "extracted"
    assert extraction["source"]["binding"] == "verified"
    assert extraction["source"]["artifact"]["sha256"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    assert extraction["plot"]["encoding"] == encoding
    series = extraction["series"]
    assert series["axis"] == {
        "name": "time",
        "unit": "s",
        "values": [0.0, 1e-6, 2e-6],
    }
    assert series["signals"] == [
        {"name": "v(out)", "unit": "V", "values": [0.0, 0.5, 1.0]}
    ]
    assert series["source"]["lineage"] == {
        "operation": "circuit.simulate",
        "request_id": SIMULATION_REQUEST_ID,
        "artifact_role": "simulation.result",
        "artifact_sha256": extraction["source"]["artifact"]["sha256"],
        "binding": "unverified",
    }
    measured = measure_result(
        series,
        {
            "measurement_id": "output.maximum",
            "kind": "maximum",
            "signal": "v(out)",
            "parameters": {},
            "extensions": {},
        },
    )
    assert measured["engineering"]["status"] == "pass"
    assert measured["data"]["measurement"]["value"] == 1.0
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    ("backend", "native_name", "builder"),
    [
        ("ngspice", "v(out)", _binary_raw),
        ("xyce", "OUT", _ascii_raw),
    ],
)
def test_extracts_explicit_cartesian_ac_components(
    tmp_path: Path,
    backend: str,
    native_name: str,
    builder,
) -> None:
    path = tmp_path / ("ac.raw" if backend == "ngspice" else "ac.xyce")
    variables = [
        ("frequency", "frequency"),
        (native_name, "voltage"),
    ]
    body = builder(
        plotname="AC Analysis",
        numeric_type="complex",
        variables=variables,
        rows=[
            [complex(10.0, 9.0), complex(0.5, -0.25)],
            [complex(100.0, 8.0), complex(0.25, -0.125)],
        ],
    )
    artifact = _write_raw(path, body)
    artifact["kind"] = f"{backend}-raw"
    analysis = {
        "type": "ac",
        "sweep": "lin",
        "points": 2,
        "start_hz": 10.0,
        "stop_hz": 100.0,
        "extensions": {},
    }
    simulation = _simulation_result(
        artifact,
        backend=backend,
        analysis=analysis,
        points=2,
        dependent_variables=1,
        finite_values=4,
    )

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": native_name,
                "output_name": "v(out).real",
                "unit": "V",
                "component": "real",
            },
            {
                "native_name": native_name,
                "output_name": "v(out).imaginary",
                "unit": "V",
                "component": "imaginary",
            },
        ],
    )

    assert payload["engineering"]["status"] == "pass"
    series = payload["data"]["extraction"]["series"]
    assert series["axis"] == {
        "name": "frequency",
        "unit": "Hz",
        "values": [10.0, 100.0],
    }
    assert series["signals"] == [
        {
            "name": "v(out).real",
            "unit": "V",
            "values": [0.5, 0.25],
        },
        {
            "name": "v(out).imaginary",
            "unit": "V",
            "values": [-0.25, -0.125],
        },
    ]


def test_operating_point_uses_explicit_synthetic_sample_axis(tmp_path: Path) -> None:
    path = tmp_path / "op.raw"
    body = _binary_raw(
        plotname="Operating Point",
        numeric_type="real",
        variables=[("v(out)", "voltage")],
        rows=[[0.75]],
    )
    artifact = _write_raw(path, body)
    analysis = {"type": "op", "extensions": {}}
    simulation = _simulation_result(
        artifact,
        backend="ngspice",
        analysis=analysis,
        points=1,
        dependent_variables=1,
        finite_values=1,
    )

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["extraction"]["series"]["axis"] == {
        "name": "sample",
        "unit": "1",
        "values": [0.0],
    }


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("digest", "series.artifact.binding_mismatch"),
        ("path", "series.artifact.path_mismatch"),
        ("unproven", "series.simulation.unproven"),
    ],
)
def test_rejects_simulation_or_artifact_binding_failures(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")
    selected_path = path
    if mutation == "digest":
        simulation["artifacts"][1]["sha256"] = "0" * 64
    elif mutation == "path":
        selected_path = tmp_path / "different.raw"
        selected_path.write_bytes(path.read_bytes())
    else:
        simulation["data"]["evidence"]["request_binding"] = "not-established"

    payload = extract_result_series(
        simulation,
        selected_path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["extraction"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == code


@pytest.mark.parametrize(
    ("selector", "code"),
    [
        (
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "A",
                "component": "real",
            },
            "series.unit.mismatch",
        ),
        (
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "imaginary",
            },
            "series.selector.component_invalid",
        ),
        (
            {
                "native_name": "v(missing)",
                "output_name": "v(missing)",
                "unit": "V",
                "component": "real",
            },
            "series.selector.missing",
        ),
    ],
)
def test_selector_units_components_and_names_fail_closed(
    tmp_path: Path,
    selector: dict[str, object],
    code: str,
) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")

    payload = extract_result_series(simulation, path, [selector])

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == code


def test_engine_rejects_binary_xyce_and_selected_scalar_overflow(tmp_path: Path) -> None:
    path = tmp_path / "xyce.raw"
    body = _binary_raw(
        plotname="Transient Analysis",
        numeric_type="real",
        variables=[("time", "time"), ("OUT", "voltage")],
        rows=[[0.0, 0.0], [1.0, 1.0]],
    )
    artifact = _write_raw(path, body)
    analysis = {
        "type": "tran",
        "step_s": 1.0,
        "stop_s": 1.0,
        "extensions": {},
    }

    unsupported = extract_analysis_raw(
        path,
        backend="xyce",
        analysis=analysis,
        selected_variables=["OUT"],
        expected_bytes=int(artifact["bytes"]),
        expected_sha256=str(artifact["sha256"]),
    )
    bounded = extract_analysis_raw(
        path,
        backend="ngspice",
        analysis=analysis,
        selected_variables=["OUT"],
        expected_bytes=int(artifact["bytes"]),
        expected_sha256=str(artifact["sha256"]),
        max_selected_scalars=3,
    )

    assert unsupported.valid is False
    assert unsupported.reason == "raw.encoding_unsupported"
    assert bounded.valid is False
    assert bounded.reason == "raw.extraction_over_limit"


def test_non_string_mapping_keys_return_invalid_request_not_exception(
    tmp_path: Path,
) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
                1: "undeclared",
            }
        ],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "series.selector.invalid"


def test_duplicate_matching_analysis_plots_are_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.raw"
    plot = _binary_raw(
        plotname="Transient Analysis",
        numeric_type="real",
        variables=[("time", "time"), ("v(out)", "voltage")],
        rows=[[0.0, 0.0], [1.0, 1.0]],
    )
    continuation = plot.replace(
        b"Title: result.series.extract fixture\n"
        b"Date: fixture\n"
        b"Plotname: Transient Analysis\n",
        b"Plotname: Transient Analysis\n",
        1,
    )
    body = plot + continuation
    artifact = _write_raw(path, body)
    analysis = {
        "type": "tran",
        "step_s": 1.0,
        "stop_s": 1.0,
        "extensions": {},
    }

    extracted = extract_analysis_raw(
        path,
        backend="ngspice",
        analysis=analysis,
        selected_variables=["v(out)"],
        expected_bytes=int(artifact["bytes"]),
        expected_sha256=str(artifact["sha256"]),
    )

    assert extracted.valid is False
    assert extracted.reason == "raw.analysis_request_mismatch"


def test_source_analysis_counts_must_match_the_reparsed_native_plot(
    tmp_path: Path,
) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")
    simulation["data"]["analysis"].update(
        point_count=99,
        dependent_variable_count=77,
        finite_value_count=42,
    )

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "completed"
    assert payload["diagnostics"][0]["code"] == (
        "series.artifact.binding_mismatch"
    )


def test_source_pass_requires_retained_roles_to_match_evidence(tmp_path: Path) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")
    simulation["artifacts"] = [
        item for item in simulation["artifacts"] if item["role"] != "simulation.log"
    ]

    payload = extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "series.simulation.unproven"


def test_input_envelope_is_not_mutated(tmp_path: Path) -> None:
    path, simulation = _transient_case(tmp_path, "ngspice")
    before = deepcopy(simulation)

    extract_result_series(
        simulation,
        path,
        [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
    )

    assert simulation == before
