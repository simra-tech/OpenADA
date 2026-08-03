from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from openada.discovery import DiscoveryManager
from openada.driver_registry import BUILTIN_DRIVERS
from openada.engines.spice import NgspicePinnedInput
from openada.operations.circuit_simulate import (
    MAX_SOURCE_BYTES,
    NgspiceProfileExecution,
    inspect_simulation_deck,
    simulate_circuit_profile,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "conformance" / "circuit-simulate-v0alpha2" / "fixtures"


def _raw_plot(
    *,
    title: bool,
    plotname: str,
    numeric_type: str,
    variables: list[tuple[str, str]],
    rows: list[list[str]],
) -> str:
    lines = []
    if title:
        lines.extend(("Title: shared analysis fixture", "Date: fixture"))
    lines.extend(
        (
            f"Plotname: {plotname}",
            f"Flags: {numeric_type}",
            f"No. Variables: {len(variables)}",
            f"No. Points: {len(rows)}",
            "Variables:",
        )
    )
    lines.extend(
        f"\t{index}\t{name}\t{unit}"
        for index, (name, unit) in enumerate(variables)
    )
    lines.append("Values:")
    for index, row in enumerate(rows):
        lines.append(f" {index}\t{row[0]}")
        lines.extend(f"\t{value}" for value in row[1:])
        lines.append("")
    return "\n".join(lines) + "\n"


RAW_RESULTS = {
    "op": _raw_plot(
        title=True,
        plotname="Operating Point",
        numeric_type="real",
        variables=[("v(in)", "voltage"), ("v(out)", "voltage")],
        rows=[["1.0", "0.5"]],
    ).encode("ascii"),
    "dc": _raw_plot(
        title=True,
        plotname="DC transfer characteristic",
        numeric_type="real",
        variables=[
            ("v(v-sweep)", "voltage"),
            ("v(in)", "voltage"),
            ("v(out)", "voltage"),
        ],
        rows=[
            ["0.0", "0.0", "0.0"],
            ["0.25", "0.25", "0.125"],
            ["0.5", "0.5", "0.25"],
            ["0.75", "0.75", "0.375"],
            ["1.0", "1.0", "0.5"],
        ],
    ).encode("ascii"),
    "ac": (
        _raw_plot(
            title=True,
            plotname="DC operating point",
            numeric_type="real",
            variables=[("v(in)", "voltage"), ("v(out)", "voltage")],
            rows=[["0.0", "0.0"]],
        )
        + _raw_plot(
            title=False,
            plotname="AC Analysis",
            numeric_type="complex",
            variables=[
                ("frequency", "frequency"),
                ("v(in)", "voltage"),
                ("v(out)", "voltage"),
            ],
            rows=[
                [f"({10.0 * (10.0 ** (index / 5.0)):.17g},0)", "(1,0)", "(0.5,-0.5)"]
                for index in range(16)
            ],
        )
    ).encode("ascii"),
    "tran": _raw_plot(
        title=True,
        plotname="Transient Analysis",
        numeric_type="real",
        variables=[
            ("time", "time"),
            ("v(in)", "voltage"),
            ("v(out)", "voltage"),
        ],
        rows=[
            ["0.0", "0.0", "0.0"],
            ["1e-7", "1.0", "0.25"],
            ["2e-7", "1.0", "0.5"],
        ],
    ).encode("ascii"),
}


PARAMETERS = {
    "op": {"analysis": {"type": "op", "extensions": {}}, "extensions": {}},
    "dc": {
        "analysis": {
            "type": "dc",
            "source_name": "VSWEEP",
            "source_unit": "V",
            "start": 0.0,
            "stop": 1.0,
            "step": 0.25,
            "extensions": {},
        },
        "extensions": {},
    },
    "ac": {
        "analysis": {
            "type": "ac",
            "sweep": "dec",
            "points": 5,
            "start_hz": 10.0,
            "stop_hz": 10_000.0,
            "extensions": {},
        },
        "extensions": {},
    },
    "tran": {
        "analysis": {
            "type": "tran",
            "step_s": 1e-7,
            "stop_s": 2e-6,
            "extensions": {},
        },
        "extensions": {},
    },
}


FIXTURE_NAMES = {
    "op": "resistor-divider-op.cir",
    "dc": "resistor-divider-dc.cir",
    "ac": "rc-ac.cir",
    "tran": "rc-transient.cir",
}


def _write_fake_simulator(
    path: Path,
    *,
    backend: str,
    raw: bytes,
    marker: Path | None = None,
    log_text: str | None = None,
    exit_code: int = 0,
) -> None:
    encoded = base64.b64encode(raw).decode("ascii")
    if backend == "ngspice":
        version = "ngspice-45.2"
        ngspice_log = log_text if log_text is not None else "No. of Data Rows : 3\n"
        body = f"""
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
result = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log.write_text({ngspice_log!r}, encoding='utf-8')
result.write_bytes(base64.b64decode({encoded!r}))
"""
    else:
        version = "Xyce Release 7.10.0-opensource"
        body = f"""
assert os.environ.get('XYCE_NO_TRACKING') == '1'
log = pathlib.Path(sys.argv[sys.argv.index('-l') + 1])
result = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log.write_text('***** End of Xyce(TM) Simulation *****\\n', encoding='utf-8')
result.write_bytes(base64.b64decode({encoded!r}))
"""
    marker_statement = (
        f"pathlib.Path({str(marker)!r}).write_text('launched', encoding='utf-8')"
        if marker is not None
        else "pass"
    )
    path.write_text(
        f"""#!/usr/bin/env python3
import base64
import os
import pathlib
import sys
if len(sys.argv) == 2 and sys.argv[1] in {{'-v', '--version'}}:
    print({version!r})
    raise SystemExit(0)
{marker_statement}
{body}
raise SystemExit({exit_code})
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    backend: str,
    analysis_type: str,
    raw: bytes | None = None,
) -> dict:
    binary = tmp_path / backend / ("Xyce" if backend == "xyce" else "ngspice")
    binary.parent.mkdir(parents=True)
    _write_fake_simulator(
        binary,
        backend=backend,
        raw=raw if raw is not None else RAW_RESULTS[analysis_type],
    )
    discovery = DiscoveryManager(binary_overrides={backend: binary})
    return simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES[analysis_type],
        tmp_path / f"{backend}-{analysis_type}-evidence",
        backend=backend,
        discovery=discovery,
        parameters=PARAMETERS[analysis_type],
    )


@pytest.mark.parametrize(
    ("analysis_type", "backends"),
    [("op", ("ngspice",)), ("dc", ("ngspice", "xyce")), ("ac", ("ngspice", "xyce"))],
)
def test_shared_analysis_features_produce_request_bound_normalized_evidence(
    tmp_path: Path,
    analysis_type: str,
    backends: tuple[str, ...],
) -> None:
    results = {
        backend: _run(
            tmp_path / backend,
            backend=backend,
            analysis_type=analysis_type,
        )
        for backend in backends
    }
    for backend, payload in results.items():
        assert payload["engineering"]["status"] == "pass"
        assert payload["data"]["analysis"]["type"] == analysis_type
        assert payload["data"]["analysis"]["point_count"] >= 1
        assert payload["data"]["analysis"]["dependent_variable_count"] >= 1
        assert payload["data"]["analysis"]["finite_value_count"] >= 1
        assert payload["data"]["evidence"]["request_binding"] == "exact"
        assert payload["data"]["evidence"]["structure"] == "valid"
        assert payload["data"]["extensions"]["org.openada"]["parameters"] == PARAMETERS[
            analysis_type
        ]
    if len(results) == 2:
        assert results["ngspice"]["data"]["analysis"] == results["xyce"]["data"][
            "analysis"
        ]


def test_ac_counts_finite_real_and_imaginary_dependent_scalars(tmp_path: Path) -> None:
    payload = _run(tmp_path, backend="xyce", analysis_type="ac")

    assert payload["data"]["analysis"] == {
        "type": "ac",
        "completion": "completed",
        "convergence": "converged",
        "point_count": 16,
        "dependent_variable_count": 2,
        "finite_value_count": 64,
        "extensions": {},
    }
    plots = payload["data"]["extensions"]["org.openada"]["native_data"][
        "output_captures"
    ][0]["validation"]["metadata"]["plots"]
    assert [plot["plotname"] for plot in plots] == ["DC operating point", "AC Analysis"]


def test_ngspice_terminal_failure_retains_request_bound_partial_transient(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "ngspice"
    _write_fake_simulator(
        binary,
        backend="ngspice",
        raw=RAW_RESULTS["tran"],
        log_text=(
            "No. of Data Rows : 3\n"
            "doAnalyses: TRAN: Timestep too small; time = 2e-7\n"
        ),
        exit_code=1,
    )

    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["tran"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=DiscoveryManager(binary_overrides={"ngspice": binary}),
        parameters=PARAMETERS["tran"],
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["analysis"] == {
        "type": "tran",
        "completion": "terminal-failure",
        "convergence": "non-converged",
        "point_count": 3,
        "dependent_variable_count": 2,
        "finite_value_count": 6,
        "extensions": {},
    }
    assert payload["data"]["evidence"]["request_binding"] == "exact"
    assert payload["data"]["evidence"]["structure"] == "valid"
    assert "simulation.analysis.non_convergent" in {
        item["code"] for item in payload["diagnostics"]
    }


def test_xyce_op_is_explicitly_unsupported_and_never_launched(tmp_path: Path) -> None:
    binary = tmp_path / "Xyce"
    marker = tmp_path / "launched"
    _write_fake_simulator(binary, backend="xyce", raw=RAW_RESULTS["op"], marker=marker)

    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["op"],
        tmp_path / "evidence",
        backend="xyce",
        discovery=DiscoveryManager(binary_overrides={"xyce": binary}),
        parameters=PARAMETERS["op"],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert {item["code"] for item in payload["diagnostics"]} == {
        "simulation.analysis.unsupported"
    }
    assert payload["data"]["analysis"]["type"] == "op"
    assert not marker.exists()


def test_typed_parameters_must_match_the_deck_before_launch(tmp_path: Path) -> None:
    binary = tmp_path / "ngspice"
    marker = tmp_path / "launched"
    _write_fake_simulator(binary, backend="ngspice", raw=RAW_RESULTS["dc"], marker=marker)
    parameters = {
        **PARAMETERS["dc"],
        "analysis": {**PARAMETERS["dc"]["analysis"], "stop": 2.0},
    }

    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["dc"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=DiscoveryManager(binary_overrides={"ngspice": binary}),
        parameters=parameters,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["diagnostics"][0]["code"] == "simulation.request.invalid"
    assert not marker.exists()


@pytest.mark.parametrize("backend", ["ngspice", "xyce"])
def test_oversized_source_is_rejected_before_hash_or_launch(
    tmp_path: Path,
    backend: str,
) -> None:
    source = tmp_path / "oversized-dc.cir"
    source.write_text(
        "Oversized DC analysis\nVSWEEP in 0 0\nR1 in 0 1k\n.dc VSWEEP 0 1 0.25\n.end\n",
        encoding="utf-8",
    )
    with source.open("ab") as handle:
        handle.truncate(MAX_SOURCE_BYTES + 1)
    binary = tmp_path / ("Xyce" if backend == "xyce" else "ngspice")
    marker = tmp_path / "launched"
    _write_fake_simulator(
        binary,
        backend=backend,
        raw=RAW_RESULTS["dc"],
        marker=marker,
    )

    payload = simulate_circuit_profile(
        source,
        tmp_path / "evidence",
        backend=backend,
        discovery=DiscoveryManager(binary_overrides={backend: binary}),
        parameters=PARAMETERS["dc"],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["inputs"] == []
    assert {item["code"] for item in payload["diagnostics"]} == {
        "simulation.evidence.over_limit"
    }
    assert payload["data"]["evidence"]["request_binding"] == "not-established"
    assert not marker.exists()


@pytest.mark.parametrize("backend", ["ngspice", "xyce"])
def test_raw_sweep_bounds_must_match_the_typed_request(
    tmp_path: Path,
    backend: str,
) -> None:
    wrong_bounds = RAW_RESULTS["dc"].replace(b" 4\t1.0\n", b" 4\t0.9\n")

    payload = _run(
        tmp_path,
        backend=backend,
        analysis_type="dc",
        raw=wrong_bounds,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["analysis"]["point_count"] is None
    assert payload["data"]["evidence"]["request_binding"] == "not-established"
    assert payload["data"]["evidence"]["structure"] == "invalid"
    assert "simulation.result.malformed" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize("backend", ["ngspice", "xyce"])
def test_raw_sweep_spacing_and_point_count_are_request_bound(
    tmp_path: Path,
    backend: str,
) -> None:
    wrong_spacing = RAW_RESULTS["dc"].replace(b" 2\t0.5\n", b" 2\t0.6\n")

    payload = _run(
        tmp_path,
        backend=backend,
        analysis_type="dc",
        raw=wrong_spacing,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["evidence"]["request_binding"] == "not-established"
    assert payload["data"]["evidence"]["structure"] == "invalid"
    assert "simulation.result.malformed" in {
        item["code"] for item in payload["diagnostics"]
    }


def test_deck_inspection_maps_all_closed_analysis_parameter_shapes() -> None:
    assert inspect_simulation_deck(FIXTURES / FIXTURE_NAMES["op"])["parameters"] == PARAMETERS[
        "op"
    ]
    assert inspect_simulation_deck(FIXTURES / FIXTURE_NAMES["dc"])["parameters"] == PARAMETERS[
        "dc"
    ]
    assert inspect_simulation_deck(FIXTURES / FIXTURE_NAMES["ac"])["parameters"] == PARAMETERS[
        "ac"
    ]


def test_builtin_drivers_advertise_only_implemented_analysis_features() -> None:
    assert {feature.rsplit(".", 1)[-1].split("/", 1)[0] for feature in BUILTIN_DRIVERS[
        "ngspice"
    ].features} == {"op", "dc", "ac", "tran"}
    assert {feature.rsplit(".", 1)[-1].split("/", 1)[0] for feature in BUILTIN_DRIVERS[
        "xyce"
    ].features} == {"dc", "ac", "tran"}


def _assert_rejected_before_launch(
    tmp_path: Path,
    *,
    deck_text: str,
    parameters: dict | None = None,
    request_id: str | None = None,
) -> dict:
    source = tmp_path / "case.cir"
    source.write_text(deck_text, encoding="utf-8")
    binary = tmp_path / "ngspice"
    marker = tmp_path / "launched"
    _write_fake_simulator(binary, backend="ngspice", raw=RAW_RESULTS["op"], marker=marker)

    payload = simulate_circuit_profile(
        source,
        tmp_path / "evidence",
        backend="ngspice",
        discovery=DiscoveryManager(binary_overrides={"ngspice": binary}),
        parameters=parameters,
        request_id=request_id,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    return payload


@pytest.mark.parametrize(
    ("deck_text", "parameters"),
    [
        (
            "AC analysis\n.ac dec 5 10 10k\n.end\n",
            {
                "analysis": {
                    "type": "ac",
                    "sweep": "dec",
                    "points": 10**400,
                    "start_hz": 10.0,
                    "stop_hz": 10_000.0,
                    "extensions": {},
                },
                "extensions": {},
            },
        ),
        (
            "DC analysis\n.dc VSWEEP 0 1 0.25\n.end\n",
            {
                "analysis": {
                    "type": "dc",
                    "source_name": "V" + "x" * 256,
                    "source_unit": "V",
                    "start": 0.0,
                    "stop": 1.0,
                    "step": 0.25,
                    "extensions": {},
                },
                "extensions": {},
            },
        ),
    ],
)
def test_mapping_callers_cannot_bypass_closed_numeric_and_text_bounds(
    tmp_path: Path,
    deck_text: str,
    parameters: dict,
) -> None:
    payload = _assert_rejected_before_launch(
        tmp_path,
        deck_text=deck_text,
        parameters=parameters,
    )

    assert payload["diagnostics"][0]["code"] == "simulation.request.invalid"


@pytest.mark.parametrize(
    "deck_text",
    [
        "Oversized transient\n.tran 1f 2u\n.end\n",
        "Multiple analyses\n.op\n.tf v(out) V1\n.end\n",
    ],
)
def test_over_limit_or_additional_native_analyses_are_rejected_before_launch(
    tmp_path: Path,
    deck_text: str,
) -> None:
    payload = _assert_rejected_before_launch(tmp_path, deck_text=deck_text)

    assert payload["data"]["evidence"]["request_binding"] == "not-established"


def test_noncanonical_request_identity_is_rejected_before_launch(tmp_path: Path) -> None:
    payload = _assert_rejected_before_launch(
        tmp_path,
        deck_text="Operating point\n.op\n.end\n",
        request_id="00000000-0000-4000-8000-00000000000A",
    )

    assert payload["diagnostics"][0]["code"] == "simulation.request.invalid"
    assert payload["data"]["protocol"]["request_id"] != (
        "00000000-0000-4000-8000-00000000000A"
    )


# --------------------------------------------------------------------------
# Typed compiled-behavioral launch seam: the shared batch profile now content-
# binds EVERY OpenADA-derived deck at the driver (not just PDK/OSDI decks), and
# the former arbitrary native_control_run callback is gone. These tests drive
# the fake simulator through the real NgspiceDriver, so they prove the wiring
# (expected_source_sha256 + pinned_inputs reach the driver) without needing a
# real ngspice.
# --------------------------------------------------------------------------


def _ngspice_discovery(tmp_path: Path, *, analysis_type: str = "op", marker: Path | None = None):
    binary = tmp_path / "ngspice" / "ngspice"
    binary.parent.mkdir(parents=True)
    _write_fake_simulator(
        binary, backend="ngspice", raw=RAW_RESULTS[analysis_type], marker=marker
    )
    return DiscoveryManager(binary_overrides={"ngspice": binary})


def test_execution_rejects_a_non_typed_spec(tmp_path: Path) -> None:
    # The seam is closed: an arbitrary callable (the old native_control_run
    # surface) can no longer be smuggled in as the execution.
    discovery = _ngspice_discovery(tmp_path)
    with pytest.raises(TypeError):
        simulate_circuit_profile(
            FIXTURES / FIXTURE_NAMES["op"],
            tmp_path / "evidence",
            backend="ngspice",
            discovery=discovery,
            parameters=PARAMETERS["op"],
            execution=lambda src, out: {},  # type: ignore[arg-type]
        )


def test_batch_execution_binds_the_deck_digest_and_refuses_a_mismatch(
    tmp_path: Path,
) -> None:
    # A derived-deck digest that no longer matches the file is refused BEFORE the
    # simulator launches -- the batch profile now carries expected_source_sha256
    # to the driver.
    marker = tmp_path / "launched.marker"
    discovery = _ngspice_discovery(tmp_path, marker=marker)
    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["op"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
        execution=NgspiceProfileExecution(expected_source_sha256="0" * 64),
    )
    assert payload["engineering"]["status"] != "pass"
    codes = [d.get("code") for d in payload.get("diagnostics", [])]
    # The driver's pre-launch digest refusal surfaces at the operation layer as
    # the "changed between resolution and launch" unproven diagnostic.
    assert "simulation.analysis.unproven" in codes, payload
    assert not marker.exists(), "the simulator must not launch on a digest refusal"


def test_batch_execution_accepts_the_matching_deck_digest(tmp_path: Path) -> None:
    # The happy path: the exact digest is forwarded and the run proceeds.
    fixture = FIXTURES / FIXTURE_NAMES["op"]
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    discovery = _ngspice_discovery(tmp_path)
    payload = simulate_circuit_profile(
        fixture,
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
        execution=NgspiceProfileExecution(expected_source_sha256=digest),
    )
    assert payload["engineering"]["status"] == "pass", payload
    assert payload["data"]["analysis"]["type"] == "op"


def test_batch_execution_pins_and_refuses_a_changed_object(tmp_path: Path) -> None:
    # A pinned compiled object (a d_cosim .so) whose digest no longer matches is
    # refused by the driver launch guard -- the batch profile forwards
    # pinned_inputs, closing the operation-rehash -> launch window.
    obj = tmp_path / "core.so"
    obj.write_bytes(b"reviewed-cosim-object-bytes")
    marker = tmp_path / "launched.marker"
    discovery = _ngspice_discovery(tmp_path, marker=marker)
    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["op"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
        execution=NgspiceProfileExecution(
            pinned_inputs=(
                NgspicePinnedInput(
                    path=obj, sha256="0" * 64, kind="cosim-code-model"
                ),
            ),
        ),
    )
    assert payload["engineering"]["status"] != "pass"
    codes = [d.get("code") for d in payload.get("diagnostics", [])]
    assert "simulation.analysis.unproven" in codes, payload
    assert not marker.exists(), "the simulator must not launch on a pin refusal"


def test_batch_execution_accepts_a_matching_pinned_object(tmp_path: Path) -> None:
    # A pin whose digest matches is re-verified and the run proceeds.
    obj = tmp_path / "core.so"
    obj.write_bytes(b"reviewed-cosim-object-bytes")
    digest = hashlib.sha256(obj.read_bytes()).hexdigest()
    discovery = _ngspice_discovery(tmp_path)
    payload = simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["op"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
        execution=NgspiceProfileExecution(
            pinned_inputs=(
                NgspicePinnedInput(path=obj, sha256=digest, kind="cosim-code-model"),
            ),
        ),
    )
    assert payload["engineering"]["status"] == "pass", payload


def _fake_ngspice(tmp_path: Path, *, marker: Path | None = None) -> DiscoveryManager:
    binary = tmp_path / "ngspice" / "ngspice"
    binary.parent.mkdir(parents=True)
    _write_fake_simulator(binary, backend="ngspice", raw=RAW_RESULTS["op"], marker=marker)
    return DiscoveryManager(binary_overrides={"ngspice": binary})


def test_operation_bare_deck_binds_parsed_bytes_and_passes(tmp_path: Path) -> None:
    # End-to-end through operations.simulate(): a stable bare deck builds a batch
    # execution spec whose expected_source_sha256 IS the digest of the deck bytes
    # the operation parsed, so a well-formed run is not falsely refused.
    from openada.operations.simulate import simulate as operation_simulate

    import shutil

    deck = tmp_path / "deck.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], deck)
    payload = operation_simulate(
        deck,
        tmp_path / "evidence",
        discovery=_fake_ngspice(tmp_path),
        backend="ngspice",
        parameters=PARAMETERS["op"],
    )
    assert payload["engineering"]["status"] == "pass", payload
    assert payload["data"]["analysis"]["type"] == "op"


def test_operation_bare_deck_swapped_before_launch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bare-deck launch is bound to the digest of the bytes the operation
    # parsed and profile/collateral/stimulus-checked (deck.sha256). If the deck
    # file is atomically swapped after that parse but before the driver scans it,
    # the driver refuses -- the earlier checks can never be attributed to
    # different launched bytes.
    from openada.engines.spice import NgspiceDriver
    from openada.operations.simulate import simulate as operation_simulate

    import shutil

    deck = tmp_path / "deck.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], deck)

    marker = tmp_path / "launched.marker"
    discovery = _fake_ngspice(tmp_path, marker=marker)

    original_simulate = NgspiceDriver.simulate

    def swap_then_simulate(self, source, output_dir, **kwargs):
        # Same .op deck, different bytes: a comment line the profile still accepts
        # but that changes the file digest away from the parsed deck.sha256.
        swapped = Path(source).read_text(encoding="utf-8") + "* raced in after parse\n"
        Path(source).write_text(swapped, encoding="utf-8")
        return original_simulate(self, source, output_dir, **kwargs)

    monkeypatch.setattr(NgspiceDriver, "simulate", swap_then_simulate)

    payload = operation_simulate(
        deck,
        tmp_path / "evidence",
        discovery=discovery,
        backend="ngspice",
        parameters=PARAMETERS["op"],
    )
    assert payload["engineering"]["status"] != "pass"
    codes = [d.get("code") for d in payload.get("diagnostics", [])]
    assert "simulation.analysis.unproven" in codes, payload
    assert not marker.exists(), "a swapped bare deck must not reach the simulator"


def test_operation_bare_crlf_deck_is_not_falsely_refused(tmp_path: Path) -> None:
    # Regression guard: the launch binds to the RAW-byte digest the driver scans
    # (deck_record), NOT the text-mode-normalized deck.sha256. A CRLF deck must
    # run, not be refused for a digest that silently normalized its line endings.
    from openada.operations.simulate import simulate as operation_simulate

    deck = tmp_path / "deck.cir"
    lf = (FIXTURES / FIXTURE_NAMES["op"]).read_text(encoding="utf-8")
    deck.write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
    payload = operation_simulate(
        deck,
        tmp_path / "evidence",
        discovery=_fake_ngspice(tmp_path),
        backend="ngspice",
        parameters=PARAMETERS["op"],
    )
    assert payload["engineering"]["status"] == "pass", payload


def test_explicit_empty_provenance_suppresses_the_default_note(tmp_path: Path) -> None:
    # The three-state field is preserved: () suppresses the default model-free
    # note, None keeps it.
    suppressed = _run_with_execution(
        tmp_path / "suppressed",
        execution=NgspiceProfileExecution(provenance_limitations=()),
    )
    assert suppressed["data"]["evidence"]["provenance_limitations"] == []
    defaulted = _run_with_execution(
        tmp_path / "defaulted",
        execution=NgspiceProfileExecution(),
    )
    assert len(defaulted["data"]["evidence"]["provenance_limitations"]) >= 1


def _run_with_execution(tmp_path: Path, *, execution: NgspiceProfileExecution) -> dict:
    return simulate_circuit_profile(
        FIXTURES / FIXTURE_NAMES["op"],
        tmp_path / "evidence",
        backend="ngspice",
        discovery=_fake_ngspice(tmp_path),
        parameters=PARAMETERS["op"],
        execution=execution,
    )


def test_profile_default_execution_binds_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exported simulate_circuit_profile() is content-bound BY DEFAULT: a
    # caller that passes no execution still cannot launch a file swapped between
    # resolution and launch.
    from openada.engines.spice import NgspiceDriver

    import shutil

    deck = tmp_path / "deck.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], deck)
    marker = tmp_path / "launched.marker"
    discovery = _fake_ngspice(tmp_path, marker=marker)

    original_simulate = NgspiceDriver.simulate

    def swap_then_simulate(self, source, output_dir, **kwargs):
        Path(source).write_text(
            Path(source).read_text(encoding="utf-8") + "* raced\n", encoding="utf-8"
        )
        return original_simulate(self, source, output_dir, **kwargs)

    monkeypatch.setattr(NgspiceDriver, "simulate", swap_then_simulate)

    payload = simulate_circuit_profile(
        deck,
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
        # No execution: the default spec still binds the source.
    )
    assert payload["engineering"]["status"] != "pass"
    assert "simulation.analysis.unproven" in [
        d.get("code") for d in payload.get("diagnostics", [])
    ], payload
    assert not marker.exists()


def test_legacy_native_binds_its_top_level_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even the no-semantic-claim native interface refuses a deck swapped between
    # its collateral scan and launch.
    from openada.engines.spice import NgspiceDriver
    from openada.operations.simulate import simulate_legacy_native

    import shutil

    deck = tmp_path / "deck.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], deck)
    marker = tmp_path / "launched.marker"
    discovery = _fake_ngspice(tmp_path, marker=marker)

    original_simulate = NgspiceDriver.simulate

    def swap_then_simulate(self, source, output_dir, **kwargs):
        Path(source).write_text(
            Path(source).read_text(encoding="utf-8") + "* raced\n", encoding="utf-8"
        )
        return original_simulate(self, source, output_dir, **kwargs)

    monkeypatch.setattr(NgspiceDriver, "simulate", swap_then_simulate)

    payload = simulate_legacy_native(
        deck, tmp_path / "evidence", discovery=discovery, execution_mode="batch"
    )
    # The native interface returns the driver payload directly (no semantic
    # decoration), so the raw driver refusal code surfaces.
    assert payload["execution"]["status"] == "invalid_request"
    codes = [d.get("code") for d in payload.get("diagnostics", [])]
    assert "input.digest_mismatch" in codes, payload
    assert not marker.exists()


def test_profile_default_binds_inspected_bytes_not_relaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default bind uses the digest of the bytes the PROFILE INSPECTED, not a
    # later re-read. A deck swapped AFTER inspection but before launch is refused
    # -- so validated-A / launched-B evidence cannot be produced. (Under a launch-
    # time re-hash this would pass, launching B.)
    from openada.operations import circuit_simulate as cs

    import shutil

    deck = tmp_path / "deck.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], deck)
    marker = tmp_path / "launched.marker"
    discovery = _fake_ngspice(tmp_path, marker=marker)

    original_inspect = cs.inspect_simulation_deck
    swapped = {"done": False}

    def inspect_then_swap(path):
        result = original_inspect(path)
        if not swapped["done"] and Path(path).resolve() == deck.resolve():
            deck.write_text(
                deck.read_text(encoding="utf-8") + "* raced after inspection\n",
                encoding="utf-8",
            )
            swapped["done"] = True
        return result

    monkeypatch.setattr(cs, "inspect_simulation_deck", inspect_then_swap)

    payload = simulate_circuit_profile(
        deck,
        tmp_path / "evidence",
        backend="ngspice",
        discovery=discovery,
        parameters=PARAMETERS["op"],
    )
    assert payload["engineering"]["status"] != "pass"
    assert "simulation.analysis.unproven" in [
        d.get("code") for d in payload.get("diagnostics", [])
    ], payload
    assert not marker.exists(), "a deck swapped after inspection must not launch"


def test_split_inspection_without_a_digest_is_refused(tmp_path: Path) -> None:
    # An execution that inspects a SEPARATE control-free deck must also name the
    # launch digest; otherwise it would validate one deck and launch another.
    import shutil

    control_free = tmp_path / "control_free.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], control_free)
    run_deck = tmp_path / "run.cir"
    shutil.copyfile(FIXTURES / FIXTURE_NAMES["op"], run_deck)

    payload = simulate_circuit_profile(
        run_deck,
        tmp_path / "evidence",
        backend="ngspice",
        discovery=_fake_ngspice(tmp_path),
        parameters=PARAMETERS["op"],
        execution=NgspiceProfileExecution(inspection_source=control_free),
    )
    assert payload["engineering"]["status"] != "pass"
    assert "simulation.request.invalid" in [
        d.get("code") for d in payload.get("diagnostics", [])
    ], payload
