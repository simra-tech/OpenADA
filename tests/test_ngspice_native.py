from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from openada.engines.spice import NgspiceDriver, NgspiceOutput


NGSPICE = shutil.which("ngspice")
pytestmark = pytest.mark.skipif(NGSPICE is None, reason="native ngspice is not installed")


def _plain_transient(path: Path, *, measurement: bool) -> None:
    measure = ".measure tran peak max v(out)\n" if measurement else ""
    path.write_text(
        "* native OpenADA control smoke\n"
        "V1 in 0 pulse(0 1 0 1n 1n 5n 10n)\n"
        "R1 in out 1k\n"
        "C1 out 0 1p\n"
        ".tran 0.1n 20n\n"
        f"{measure}"
        ".end\n",
        encoding="utf-8",
    )


def test_native_batch_streams_and_validates_wrapper_raw(tmp_path):
    source = tmp_path / "batch.cir"
    _plain_transient(source, measurement=False)

    payload = NgspiceDriver(NGSPICE).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["execution"]["command"][1:3] == ["-b", "-r"]
    capture = payload["data"]["output_captures"][0]
    assert capture["status"] == "valid"
    assert capture["validation"]["metadata"]["has_analysis_plot"] is True


def test_native_control_evaluates_measure_and_explicit_init(tmp_path):
    source = tmp_path / "measure.cir"
    _plain_transient(source, measurement=True)
    init_file = tmp_path / "project.spiceinit"
    init_file.write_text("set numdgt=12\n", encoding="utf-8")

    payload = NgspiceDriver(NGSPICE).simulate(
        source,
        tmp_path / "evidence",
        execution_mode="control",
        init_file=init_file,
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert "-n" in payload["execution"]["command"]
    assert "-b" not in payload["execution"]["command"]
    assert "-r" not in payload["execution"]["command"]
    assert payload["data"]["measurements"][0]["name"] == "peak"
    assert 0.99 < payload["data"]["measurements"][0]["value"] <= 1.0


def test_native_control_captures_declared_raw_and_wrdata_without_deck_quit(tmp_path):
    source = tmp_path / "deck.cir"
    source.write_text(
        "* native deck-owned outputs\n"
        "V1 in 0 pulse(0 1 0 1n 1n 5n 10n)\n"
        "R1 in out 1k\n"
        "C1 out 0 1p\n"
        ".tran 0.1n 20n\n"
        ".control\n"
        "run\n"
        "write deck.raw\n"
        "wrdata deck.dat v(out)\n"
        ".endc\n"
        ".end\n",
        encoding="utf-8",
    )
    init_file = tmp_path / "project.spiceinit"
    init_file.write_text("set numdgt=12\n", encoding="utf-8")

    payload = NgspiceDriver(NGSPICE).simulate(
        source,
        tmp_path / "evidence",
        execution_mode="control",
        init_file=init_file,
        expected_outputs=[
            NgspiceOutput("raw", "deck.raw"),
            NgspiceOutput("wrdata", "deck.dat"),
        ],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert [item["status"] for item in payload["data"]["output_captures"]] == [
        "valid",
        "valid",
    ]
    assert (tmp_path / "deck.raw").is_file()
    assert (tmp_path / "deck.dat").is_file()


def test_native_batch_hidden_measure_warning_never_passes(tmp_path):
    source = tmp_path / "hidden-measure.cir"
    source.write_text(
        "* top-level source intentionally hides its measurement\n"
        "V1 in 0 1\n"
        "R1 in out 1k\n"
        ".include measurement.inc\n"
        ".op\n"
        ".end\n",
        encoding="utf-8",
    )
    (tmp_path / "measurement.inc").write_text(
        ".measure op voltage find v(out)\n",
        encoding="utf-8",
    )

    payload = NgspiceDriver(NGSPICE).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(
        item["code"] == "input.transitive_uninspected"
        for item in payload["diagnostics"]
    )


def test_native_constants_only_deck_raw_is_retained_but_unknown(tmp_path):
    source = tmp_path / "constants.cir"
    source.write_text(
        "* writes the built-in constants plot without an analysis\n"
        ".control\n"
        "write constants.raw\n"
        ".endc\n"
        ".end\n",
        encoding="utf-8",
    )

    payload = NgspiceDriver(NGSPICE).simulate(
        source,
        tmp_path / "evidence",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "constants.raw")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    capture = payload["data"]["output_captures"][0]
    assert capture["status"] == "invalid"
    assert capture["validation"]["reason"] == "raw.constants_only"
    assert any(
        item["kind"] == "ngspice-raw" for item in payload["artifacts"]
    )


def test_native_circuit_title_cannot_spoof_completed_analysis(tmp_path):
    source = tmp_path / "spoofed-analysis-title.cir"
    source.write_text(
        "* No. of Data Rows : 2\n"
        ".control\n"
        "wrdata deck.dat pi\n"
        ".endc\n"
        ".end\n",
        encoding="utf-8",
    )

    payload = NgspiceDriver(NGSPICE).simulate(
        source,
        tmp_path / "evidence",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("wrdata", "deck.dat")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["data"]["output_captures"][0]["status"] == "valid"
    assert payload["data"]["analysis_evidence"] == {
        "raw": False,
        "completed_log_record": False,
    }
    assert payload["engineering"]["status"] == "unknown"
    assert any(
        item["code"] == "simulation.analysis_unproven"
        for item in payload["diagnostics"]
    )


def test_native_circuit_title_cannot_spoof_terminal_convergence_failure(tmp_path):
    source = tmp_path / "spoofed-convergence-title.cir"
    source.write_text(
        "* timestep too small characterization\n"
        "V1 out 0 1\n"
        "R1 out 0 1k\n"
        ".op\n"
        ".end\n",
        encoding="utf-8",
    )

    payload = NgspiceDriver(NGSPICE).simulate(source, tmp_path / "evidence")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["converged"] is True
    assert not any(
        item["code"] == "simulation.nonconvergent"
        for item in payload["diagnostics"]
    )
