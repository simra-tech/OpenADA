from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from openada.engines.opensta import OpenSTADriver


PROFILE_PATH = Path(__file__).parents[1] / "profiles" / "timing.analyze-v1alpha1.json"


def _validate_data(payload: dict) -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(profile["normalized_result"]["data_schema"]).validate(
        payload["data"]
    )


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_sta(
    path: Path,
    *,
    setup_wns_ns: float = 0.2,
    setup_tns_ns: float = 0.0,
    hold_wns_ns: float = 0.05,
    hold_tns_ns: float = 0.0,
    check_setup: str = "",
    setup_report: str = "valid",
    hold_report: str = "valid",
    setup_report_slack_s: float | None = None,
    native_issue: str = "",
    mutate_input: Path | None = None,
    race_transcript: Path | None = None,
    mutate_tool: bool = False,
    require_closed_environment: bool = False,
) -> None:
    setup_slack = (
        setup_wns_ns * 1e-9
        if setup_report_slack_s is None
        else setup_report_slack_s
    )
    setup_payload = {
        "checks": [
            {
                "type": "check",
                "path_group": "core_clock",
                "path_type": "max",
                "startpoint": "source_ff/Q",
                "endpoint": "target_ff/D",
                "slack": setup_slack,
            }
        ]
    }
    hold_payload = {
        "checks": [
            {
                "type": "check",
                "path_group": "core_clock",
                "path_type": "min",
                "startpoint": "source_ff/Q",
                "endpoint": "target_ff/D",
                "slack": hold_wns_ns * 1e-9,
            }
        ]
    }
    expected_path = os.pathsep.join(
        dict.fromkeys(
            (
                str(path.parent),
                *(directory for directory in os.defpath.split(os.pathsep) if directory),
            )
        )
    )
    blocked_environment = (
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PERL5OPT",
        "RUBYOPT",
        "TCL_LIBRARY",
        "TCLLIBPATH",
        "TK_LIBRARY",
        "STA_SETUP_WNS_NS",
        "STA_CONTINUE_ON_ERROR",
        "OPENSTA_HOME",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "CDPATH",
        "IFS",
    )
    body = f"""import json, os, pathlib, sys
if {require_closed_environment!r}:
    assert os.environ.get('PATH') == {expected_path!r}
    assert os.environ.get('LANG') == 'C'
    assert os.environ.get('LC_ALL') == 'C'
    assert not any(name in os.environ for name in {blocked_environment!r})
if '-version' in sys.argv or '--version' in sys.argv:
    print('OpenSTA 3.1.0')
    raise SystemExit(0)
if {mutate_tool!r}:
    os.utime(__file__, None)
script = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')
assert 'check_setup -verbose -unconstrained_endpoints > check-setup.txt' in script
assert '-path_delay max' in script and '-path_delay min' in script
assert '-group_path_count 10' in script and '-endpoint_path_count 1' in script
pathlib.Path('check-setup.txt').write_text({check_setup!r}, encoding='utf-8')
setup_mode = {setup_report!r}
if setup_mode == 'valid':
    pathlib.Path('setup-paths.json').write_text(json.dumps({setup_payload!r}), encoding='utf-8')
elif setup_mode == 'malformed':
    pathlib.Path('setup-paths.json').write_text('{{not-json', encoding='utf-8')
hold_mode = {hold_report!r}
if hold_mode == 'valid':
    pathlib.Path('hold-paths.json').write_text(json.dumps({hold_payload!r}), encoding='utf-8')
elif hold_mode == 'malformed':
    pathlib.Path('hold-paths.json').write_text('{{not-json', encoding='utf-8')
mutate = {str(mutate_input) if mutate_input else ''!r}
if mutate:
    pathlib.Path(mutate).write_text('module top; wire changed; endmodule\\n', encoding='utf-8')
race_transcript = {str(race_transcript) if race_transcript else ''!r}
if race_transcript:
    pathlib.Path(race_transcript).write_text('raced transcript\\n', encoding='utf-8')
print('OPENADA_UNITS_BEGIN')
print(' time 1ns')
print(' capacitance 1fF')
print('OPENADA_UNITS_END')
effective_setup_wns_ns = float(os.environ.get('STA_SETUP_WNS_NS', {setup_wns_ns!r}))
print('OPENADA_SETUP_WNS_BEGIN')
print(f'worst slack max {{effective_setup_wns_ns:.9f}}')
print('OPENADA_SETUP_WNS_END')
print('OPENADA_SETUP_TNS_BEGIN')
print('tns max {setup_tns_ns:.9f}')
print('OPENADA_SETUP_TNS_END')
print('OPENADA_HOLD_WNS_BEGIN')
print('worst slack min {hold_wns_ns:.9f}')
print('OPENADA_HOLD_WNS_END')
print('OPENADA_HOLD_TNS_BEGIN')
print('tns min {hold_tns_ns:.9f}')
print('OPENADA_HOLD_TNS_END')
if {native_issue!r}:
    print({native_issue!r})
print('OPENADA_ANALYSIS_COMPLETE')
"""
    _write_executable(path, body)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    netlist = tmp_path / "mapped.v"
    liberty = tmp_path / "cells.lib"
    sdc = tmp_path / "constraints.sdc"
    netlist.write_text("module top(input clk); endmodule\n", encoding="utf-8")
    liberty.write_text("library(test) { time_unit : \"1ns\"; }\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")
    return netlist, liberty, sdc


def _run(tmp_path: Path, binary: Path, *, output_name: str = "timing") -> dict:
    netlist, liberty, sdc = _inputs(tmp_path)
    return OpenSTADriver(str(binary)).timing_analyze(
        netlist,
        liberty,
        sdc,
        tmp_path / output_name,
        top="top",
    )


def test_opensta_pass_retains_hashed_setup_hold_evidence(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary)

    payload = _run(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["command"][1:3] == ["-no_init", "-exit"]
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["timing_constraints_satisfied"] is True
    assert payload["data"]["constraints_complete"] is True
    assert payload["data"]["reports_complete"] is True
    assert payload["data"]["inputs_stable"] is True
    assert payload["data"]["tool_identity_stable"] is True
    assert payload["data"]["setup"] == {
        "wns_s": pytest.approx(0.2e-9),
        "tns_s": pytest.approx(0.0),
        "path_count": 1,
        "critical_path": {
            "startpoint": "source_ff/Q",
            "endpoint": "target_ff/D",
            "path_group": "core_clock",
            "slack_s": pytest.approx(0.2e-9),
        },
    }
    assert payload["data"]["analysis_model"] == "single_corner_ideal_interconnect_no_spef"
    assert payload["data"]["spef_supplied"] is False
    assert payload["data"]["signoff_level"] is False
    assert payload["data"]["environment_policy"] == "closed-opensta-runtime-v1"
    assert all(record["sha256"] for record in payload["inputs"])
    assert {record["role"] for record in payload["inputs"]} == {
        "timing.netlist",
        "technology.liberty",
        "timing.sdc",
    }
    assert {artifact["role"] for artifact in payload["artifacts"]} == {
        "timing.script",
        "timing.sdc-snapshot",
        "timing.log",
        "timing.constraint-check",
        "timing.setup-paths",
        "timing.hold-paths",
    }
    profile = json.loads(
        (Path(__file__).parents[1] / "profiles" / "timing.analyze-v1alpha1.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(profile["normalized_result"]["data_schema"]).validate(
        payload["data"]
    )
    script = (tmp_path / "timing" / "timing-analyze.tcl").read_text(encoding="utf-8")
    assert "report_worst_slack -max -digits 9" in script
    assert "report_worst_slack -min -digits 9" in script
    assert ".openada-sta-" not in script
    assert str(tmp_path / "timing" / "timing-input.sdc") in script
    snapshot = next(
        item for item in payload["artifacts"] if item["role"] == "timing.sdc-snapshot"
    )
    original = next(item for item in payload["inputs"] if item["role"] == "timing.sdc")
    assert snapshot["sha256"] == original["sha256"]
    assert payload["data"]["sdc_policy"] == "openada-sdc-v1"
    assert payload["data"]["sdc_validation"] == "parsed-safe-subset"
    assert payload["data"]["netlist_validation"] == "self-contained"
    assert payload["data"]["liberty_validation"] == "self-contained"
    assert payload["data"]["constraint_check_validation"] == "complete"
    assert payload["data"]["metrics_validation"] == "parsed"
    assert payload["data"]["metric_consistency"] == "consistent"
    assert payload["data"]["path_reports_agree_with_wns"] is True


def test_opensta_probe_and_execution_ignore_ambient_runtime_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, require_closed_environment=True)
    injected_environment = {
        "LD_PRELOAD": str(tmp_path / "unbound-loader.so"),
        "LD_LIBRARY_PATH": str(tmp_path / "unbound-libraries"),
        "DYLD_INSERT_LIBRARIES": str(tmp_path / "unbound-loader.dylib"),
        "PYTHONHOME": str(tmp_path / "unbound-python-home"),
        "PYTHONPATH": str(tmp_path / "unbound-python-path"),
        "TCL_LIBRARY": str(tmp_path / "unbound-tcl"),
        "TCLLIBPATH": str(tmp_path / "unbound-tcl-packages"),
        "STA_SETUP_WNS_NS": "-9.0",
        "STA_CONTINUE_ON_ERROR": "1",
        "OPENSTA_HOME": str(tmp_path / "unbound-opensta"),
        "BASH_ENV": str(tmp_path / "unbound-bash-env"),
        "ENV": str(tmp_path / "unbound-shell-env"),
        "SHELLOPTS": "xtrace",
    }
    for key, value in injected_environment.items():
        monkeypatch.setenv(key, value)

    payload = _run(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["environment_policy"] == "closed-opensta-runtime-v1"
    assert payload["data"]["setup"]["wns_s"] == pytest.approx(0.2e-9)
    _validate_data(payload)


def test_opensta_executable_mutation_makes_timing_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, mutate_tool=True)

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["tool_identity_stable"] is False
    assert any(item["code"] == "tool.changed" for item in payload["diagnostics"])
    _validate_data(payload)


def test_opensta_negative_constrained_slack_is_engineering_fail(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, setup_wns_ns=-0.25, setup_tns_ns=-1.5)

    payload = _run(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["timing_constraints_satisfied"] is False
    assert payload["data"]["setup"]["wns_s"] == pytest.approx(-0.25e-9)
    assert payload["data"]["setup"]["tns_s"] == pytest.approx(-1.5e-9)
    assert any(item["code"] == "timing.setup_violation" for item in payload["diagnostics"])


def test_opensta_nonempty_check_setup_is_unknown_not_timing_fail(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(
        binary,
        setup_wns_ns=-0.25,
        setup_tns_ns=-1.5,
        check_setup="Warning: 2 unconstrained endpoints.\n",
    )

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["constraints_complete"] is False
    assert payload["data"]["timing_constraints_satisfied"] is None
    assert any(
        item["code"] == "timing.constraints_incomplete" for item in payload["diagnostics"]
    )


@pytest.mark.parametrize("mode", ["missing", "malformed"])
def test_opensta_missing_or_malformed_path_report_is_unknown(
    tmp_path: Path, mode: str
) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, setup_report=mode)

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["reports_complete"] is False
    assert payload["data"]["timing_constraints_satisfied"] is None
    assert any(
        item["code"] == "timing.setup_report_invalid" for item in payload["diagnostics"]
    )


def test_opensta_scalar_and_path_slack_disagreement_is_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, setup_report_slack_s=-0.1e-9)

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["path_reports_agree_with_wns"] is False
    assert any(
        item["code"] == "timing.evidence_disagrees"
        for item in payload["diagnostics"]
    )


def test_opensta_near_zero_cross_sign_slack_disagreement_is_unknown(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(
        binary,
        setup_wns_ns=0.0001,
        setup_report_slack_s=-0.000399e-9,
    )

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["setup"]["wns_s"] > 0
    assert payload["data"]["setup"]["critical_path"]["slack_s"] < 0
    assert payload["data"]["path_reports_agree_with_wns"] is False
    assert payload["data"]["timing_constraints_satisfied"] is None
    assert any(item["code"] == "timing.evidence_disagrees" for item in payload["diagnostics"])
    _validate_data(payload)


def test_opensta_changed_input_invalidates_otherwise_complete_result(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    netlist, liberty, sdc = _inputs(tmp_path)
    _write_fake_sta(binary, mutate_input=netlist)

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist,
        liberty,
        sdc,
        tmp_path / "timing",
        top="top",
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert payload["data"]["changed_inputs"] == [str(netlist)]
    assert any(item["code"] == "input.changed" for item in payload["diagnostics"])


def test_opensta_rejects_stale_evidence_before_launch(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary)
    netlist, liberty, sdc = _inputs(tmp_path)
    output_dir = tmp_path / "timing"
    output_dir.mkdir()
    stale = output_dir / "setup-paths.json"
    stale.write_text(json.dumps({"checks": []}), encoding="utf-8")

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist,
        liberty,
        sdc,
        output_dir,
        top="top",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert json.loads(stale.read_text(encoding="utf-8")) == {"checks": []}
    assert payload["artifacts"] == []


def test_opensta_transcript_creation_race_normalizes_to_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    transcript = tmp_path / "timing" / "timing-analyze.log"
    _write_fake_sta(binary, race_transcript=transcript)

    payload = _run(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert transcript.read_text(encoding="utf-8") == "raced transcript\n"
    assert not any(
        artifact["role"] == "timing.log" for artifact in payload["artifacts"]
    )
    assert any(
        item["code"] == "artifact.invalid"
        and "transcript" in item["message"].lower()
        for item in payload["diagnostics"]
    )
    _validate_data(payload)


@pytest.mark.parametrize(
    "native_issue",
    ["Warning: linked cell is blackboxed", "warning: future lowercase diagnostic"],
)
def test_opensta_native_warning_prevents_timing_claim(
    tmp_path: Path, native_issue: str
) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary, native_issue=native_issue)

    payload = _run(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["timing_constraints_satisfied"] is None
    assert any(item["code"] == "opensta.warning" for item in payload["diagnostics"])


@pytest.mark.parametrize(
    "unsafe_sdc",
    [
        'source "project.tcl"\n',
        'read_spef "routed.spef"\n',
        "proc report_worst_slack args { return 0 }\n",
        "set command read_spef\n$command routed.spef\n",
        "create_clock -period 10 [exec touch forged]\n",
        "set x $::env(HOME)\n",
        "set sta_continue_on_error 1\ncreate_clock -period 10 [get_ports clk]\n",
        "set argv ignored\ncreate_clock -period 10 [get_ports clk]\n",
        "create_clock -period $ambient_period [get_ports clk]\n",
    ],
)
def test_opensta_rejects_executable_or_ambient_sdc_tcl_before_launch(
    tmp_path: Path, unsafe_sdc: str
) -> None:
    binary = tmp_path / "sta"
    marker = tmp_path / "launched"
    _write_executable(
        binary,
        "import pathlib, sys\n"
        "if '-version' in sys.argv or '--version' in sys.argv:\n"
        "    print('OpenSTA 3.1.0')\n"
        "else:\n"
        f"    pathlib.Path({str(marker)!r}).write_text('launched')\n",
    )
    netlist, liberty, sdc = _inputs(tmp_path)
    sdc.write_text(unsafe_sdc, encoding="utf-8")

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist, liberty, sdc, tmp_path / "timing", top="top"
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["sdc_policy"] == "openada-sdc-v1"
    assert payload["data"]["sdc_validation"] != "parsed-safe-subset"
    assert not marker.exists()
    assert payload["artifacts"] == []
    profile = json.loads(
        (Path(__file__).parents[1] / "profiles" / "timing.analyze-v1alpha1.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(profile["normalized_result"]["data_schema"]).validate(
        payload["data"]
    )


def test_opensta_accepts_orfs_local_sdc_variables(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary)
    netlist, liberty, sdc = _inputs(tmp_path)
    netlist.write_text("module ibex_core(input clk_i); endmodule\n", encoding="utf-8")
    sdc.write_text(
        "\n".join(
            (
                "current_design ibex_core",
                "set clk_name core_clock",
                "set clk_port_name clk_i",
                "set clk_period 2.2",
                "set clk_io_pct 0.2",
                "set clk_port [get_ports $clk_port_name]",
                "create_clock -name $clk_name -period $clk_period $clk_port",
                "set clk_io_name vclk_$clk_name",
                "create_clock -name $clk_io_name -period $clk_period",
                "set_clock_latency 0.285 [get_clocks $clk_name]",
                "set_clock_latency 0.285 [get_clocks $clk_io_name]",
                "set non_clock_inputs [all_inputs -no_clocks]",
                "set_input_delay [expr $clk_period * $clk_io_pct] "
                "-clock $clk_io_name $non_clock_inputs",
                "set_output_delay [expr $clk_period * $clk_io_pct] "
                "-clock $clk_io_name [all_outputs]",
                "",
            )
        ),
        encoding="utf-8",
    )

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist,
        liberty,
        sdc,
        tmp_path / "timing",
        top="ibex_core",
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["sdc_validation"] == "parsed-safe-subset"
    _validate_data(payload)


@pytest.mark.parametrize(
    ("input_name", "contents", "validation_field"),
    [
        (
            "mapped.v",
            '`include "generated_cells.v"\nmodule top; endmodule\n',
            "netlist_validation",
        ),
        (
            "cells.lib",
            'include_file("extra.lib")\nlibrary(test) { time_unit : "1ns"; }\n',
            "liberty_validation",
        ),
    ],
)
def test_opensta_rejects_transitive_timing_inputs_before_launch(
    tmp_path: Path,
    input_name: str,
    contents: str,
    validation_field: str,
) -> None:
    binary = tmp_path / "sta"
    marker = tmp_path / "launched"
    _write_executable(
        binary,
        "import pathlib, sys\n"
        "if '-version' in sys.argv or '--version' in sys.argv:\n"
        "    print('OpenSTA 3.1.0')\n"
        "else:\n"
        f"    pathlib.Path({str(marker)!r}).write_text('launched')\n",
    )
    netlist, liberty, sdc = _inputs(tmp_path)
    {"mapped.v": netlist, "cells.lib": liberty}[input_name].write_text(
        contents, encoding="utf-8"
    )

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist, liberty, sdc, tmp_path / "timing", top="top"
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"][validation_field] == "transitive-include-directive"
    assert not marker.exists()
    _validate_data(payload)


def test_opensta_unusable_tool_is_not_launched_and_data_validates(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    marker = tmp_path / "launched"
    _write_executable(
        binary,
        "import pathlib, sys\n"
        "if '-version' in sys.argv or '--version' in sys.argv:\n"
        "    print('unrelated sta utility 9.9')\n"
        "else:\n"
        f"    pathlib.Path({str(marker)!r}).write_text('launched')\n",
    )
    netlist, liberty, sdc = _inputs(tmp_path)

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist, liberty, sdc, tmp_path / "timing", top="top"
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["tool"]["version"] is None
    assert payload["data"]["constraint_check_validation"] == "not-run"
    assert payload["data"]["metrics_validation"] == "not-run"
    assert payload["data"]["metric_consistency"] == "not-run"
    assert payload["data"]["path_reports_agree_with_wns"] is False
    assert not marker.exists()
    _validate_data(payload)


def test_opensta_timed_out_result_data_validates(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_executable(
        binary,
        "import sys, time\n"
        "if '-version' in sys.argv or '--version' in sys.argv:\n"
        "    print('OpenSTA 3.1.0')\n"
        "else:\n"
        "    time.sleep(5)\n",
    )
    netlist, liberty, sdc = _inputs(tmp_path)

    payload = OpenSTADriver(str(binary)).timing_analyze(
        netlist,
        liberty,
        sdc,
        tmp_path / "timing",
        top="top",
        timeout=0.01,
    )

    assert payload["execution"]["status"] == "timed_out"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["constraint_check_validation"] == "missing"
    assert payload["data"]["metrics_validation"] != "parsed"
    assert payload["data"]["path_reports_agree_with_wns"] is False
    _validate_data(payload)


def test_opensta_invalid_request_result_data_validates(tmp_path: Path) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary)
    _netlist, liberty, sdc = _inputs(tmp_path)

    payload = OpenSTADriver(str(binary)).timing_analyze(
        tmp_path / "missing.v",
        liberty,
        sdc,
        tmp_path / "timing",
        top="top",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["netlist_validation"] == "not-run"
    assert payload["data"]["constraint_check_validation"] == "not-run"
    assert payload["data"]["metrics_validation"] == "not-run"
    assert payload["data"]["path_reports_agree_with_wns"] is False
    _validate_data(payload)


@pytest.mark.skipif(os.name == "nt", reason="Windows rejects these path characters")
@pytest.mark.parametrize("control", ["\x01", "\x7f"])
@pytest.mark.parametrize("input_index", [0, 1, 2])
def test_opensta_rejects_ascii_controls_in_canonical_input_paths(
    tmp_path: Path, control: str, input_index: int
) -> None:
    binary = tmp_path / "sta"
    _write_fake_sta(binary)
    inputs = list(_inputs(tmp_path))
    original = inputs[input_index]
    controlled = original.with_name(f"{original.stem}{control}{original.suffix}")
    original.rename(controlled)
    inputs[input_index] = controlled

    payload = OpenSTADriver(str(binary)).timing_analyze(
        *inputs,
        tmp_path / "timing",
        top="top",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(
        item["code"] == "input.invalid"
        and "canonical path contains" in item["message"]
        for item in payload["diagnostics"]
    )
    assert not (tmp_path / "timing" / "timing-analyze.tcl").exists()
    _validate_data(payload)
