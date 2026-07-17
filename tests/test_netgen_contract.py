from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import openada.engines.netgen as netgen_engine
import openada.engines.netgen_outputs as netgen_outputs
from openada.engines.netgen import NetgenDriver


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    layout = tmp_path / "layout.spice"
    schematic = tmp_path / "schematic.spice"
    setup = tmp_path / "setup.tcl"
    provenance = tmp_path / "PDK-COMMIT"
    layout.write_text(".subckt top a y\n.ends top\n", encoding="utf-8")
    schematic.write_text(".subckt top a y\n.ends top\n", encoding="utf-8")
    setup.write_text("# reviewed setup\n", encoding="utf-8")
    provenance.write_text("pdk-revision-before\n", encoding="utf-8")
    return layout, schematic, setup, provenance


def _pass_json(*, cell: str = "top") -> bytes:
    return json.dumps(
        [
            {
                "name": [cell, cell],
                "devices": [[['nmos', 1]], [['nmos', 1]]],
                "nets": [2, 2],
                "badnets": [],
                "badelements": [],
                "pins": [["a", "y"], ["a", "y"]],
            }
        ]
    ).encode()


def _mismatch_json(*, cell: str = "top", property_error: bool = False) -> bytes:
    comparison = {
        "name": [cell, cell],
        "devices": [[['nmos', 1]], [['nmos', 1]]],
        "nets": [2, 2],
        "badnets": [
            [
                [["a", [["nmos", "gate", 1]]]],
                [["y", [["nmos", "gate", 1]]]],
            ]
        ],
        "badelements": [],
        "pins": [["a", "y"], ["a", "y"]],
    }
    if property_error:
        comparison["badnets"] = []
        comparison["properties"] = [["M1", "w", "1u", "2u"]]
    return json.dumps([comparison]).encode()


def _pass_report(*, final_result: bool) -> bytes:
    terminal = (
        "\nFinal result: Circuits match uniquely.\n.\n"
        if final_result
        else "\nCircuits match uniquely.\n"
    )
    return (
        "\nSubcircuit summary:\n"
        "Circuit 1: top                             |Circuit 2: top\n"
        "-------------------------------------------|-------------------------------------------\n"
        "nmos (1)                                   |nmos (1)\n"
        "Number of devices: 1                       |Number of devices: 1\n"
        "Number of nets: 2                          |Number of nets: 2\n"
        "---------------------------------------------------------------------------------------\n"
        "Netlists match uniquely.\n\n"
        "Subcircuit pins:\n"
        "Circuit 1: top                             |Circuit 2: top\n"
        "-------------------------------------------|-------------------------------------------\n"
        "a                                          |a\n"
        "y                                          |y\n"
        "---------------------------------------------------------------------------------------\n"
        "Cell pin lists are equivalent.\n"
        "Device classes top and top are equivalent.\n"
        + terminal
    ).encode()


def _hierarchical_pass_report(*, duplicate_top: bool = False) -> bytes:
    child = (
        "\nSubcircuit summary:\n"
        "Circuit 1: leaf                            |Circuit 2: leaf\n"
        "nmos (1)                                   |nmos (1)\n"
        "Number of devices: 7                       |Number of devices: 7\n"
        "Number of nets: 9                          |Number of nets: 9\n"
        "Circuits match uniquely.\n"
        "Netlists match uniquely.\n\n"
        "Subcircuit pins:\n"
        "Circuit 1: leaf                            |Circuit 2: leaf\n"
        "a                                          |a\n"
        "y                                          |y\n"
        "Cell pin lists are equivalent.\n"
        "Device classes leaf and leaf are equivalent.\n"
    ).encode()
    top = _pass_report(final_result=True)
    return child + top + (top if duplicate_top else b"")


def _hierarchical_pass_json() -> bytes:
    top = json.loads(_pass_json())[0]
    child = {
        "name": ["leaf", "leaf"],
        "devices": [[["nmos", 7]], [["nmos", 7]]],
        "nets": [9, 9],
        "badnets": [],
        "badelements": [],
        "pins": [["a", "y"], ["a", "y"]],
    }
    return json.dumps([child, top]).encode()


def _mismatch_report() -> bytes:
    return (
        "\nSubcircuit summary:\n"
        "Circuit 1: top                             |Circuit 2: top\n"
        "-------------------------------------------|-------------------------------------------\n"
        "nmos (1)                                   |nmos (1)\n"
        "Number of devices: 1                       |Number of devices: 1\n"
        "Number of nets: 2                          |Number of nets: 2\n"
        "---------------------------------------------------------------------------------------\n"
        "NET mismatches: Class fragments follow (with fanout counts):\n"
        "Circuit 1: top                             |Circuit 2: top\n"
        "Netlists do not match.\n"
        "Netlists do not match.\n"
    ).encode()


def _fake_netgen(
    path: Path,
    *,
    report_body: bytes | None = None,
    json_body: bytes | None = None,
    report_mode: str = "write",
    json_mode: str = "write",
    stdout_code: str = "",
    stderr_text: str = "",
    mutate_path: Path | None = None,
    run_marker: Path | None = None,
    transcript_collision: str | None = None,
) -> None:
    report_body = _pass_report(final_result=False) if report_body is None else report_body
    json_body = _pass_json() if json_body is None else json_body
    mutation = (
        f"pathlib.Path({str(mutate_path)!r}).write_text('pdk-revision-after\\n', encoding='utf-8')"
        if mutate_path is not None
        else ""
    )
    marker = (
        f"pathlib.Path({str(run_marker)!r}).write_text('ran\\n', encoding='utf-8')"
        if run_marker is not None
        else ""
    )
    transcript_action = ""
    if transcript_collision is not None:
        transcript_action = f"emit(report.with_name(report.name + '.openada.log'), b'collision', {transcript_collision!r})"
    _write_executable(
        path,
        f"""import os, pathlib, sys
if sys.argv[1:] == ['-batch'] or '-version' in sys.argv or '--version' in sys.argv:
    print('Netgen 1.5 compiled on test date')
    raise SystemExit(0)

def emit(output, body, mode):
    if mode == 'missing':
        return
    if mode == 'write':
        output.write_bytes(body)
        return
    target = output.with_name(output.name + '.target')
    target.write_bytes(body)
    if mode == 'symlink':
        output.symlink_to(target)
    elif mode == 'hardlink':
        os.link(target, output)
    else:
        raise AssertionError(mode)

report = pathlib.Path(sys.argv[-2])
native_json = report.with_suffix('.json')
emit(report, {report_body!r}, {report_mode!r})
emit(native_json, {json_body!r}, {json_mode!r})
{transcript_action}
{mutation}
{marker}
print('Reading setup file ' + sys.argv[-3])
{stdout_code}
if {stderr_text!r}:
    print({stderr_text!r}, file=sys.stderr)
print('LVS Done.')
""",
    )


def _invoke(
    tmp_path: Path,
    binary: Path,
    *,
    report: Path | None = None,
    provenance_inputs: tuple[Path, ...] = (),
) -> dict:
    layout, schematic, setup, _provenance = _inputs(tmp_path)
    return NetgenDriver(str(binary)).lvs(
        layout,
        schematic,
        "top",
        setup,
        report or tmp_path / "evidence" / "run.comp",
        provenance_inputs=provenance_inputs,
    )


@pytest.mark.parametrize("final_result", [False, True], ids=["netgen-1.5.133", "netgen-1.5.321"])
def test_clean_match_accepts_both_reviewed_report_grammars(tmp_path: Path, final_result: bool) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_body=_pass_report(final_result=final_result))

    payload = _invoke(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["execution"]["command"][-1] == "-json"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["lvs_match"] is True
    assert payload["data"]["inputs_stable"] is True
    assert payload["data"]["transcript"]["assessment"]["clean"] is True
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "netgen-comparison",
        "netgen-comparison-json",
        "netgen-transcript",
    }


def test_clean_native_mismatch_is_engineering_fail_not_execution_failure(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_body=_mismatch_report(), json_body=_mismatch_json())

    payload = _invoke(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["lvs_match"] is False
    assert payload["data"]["comparison"]["report_outcome"] == "fail"
    assert payload["data"]["comparison"]["json_outcome"] == "fail"


def test_hierarchical_report_selects_requested_top_without_mixing_child_counts(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(
        binary,
        report_body=_hierarchical_pass_report(),
        json_body=_hierarchical_pass_json(),
    )

    payload = _invoke(tmp_path, binary)

    report = payload["data"]["comparison"]["report"]
    assert payload["engineering"]["status"] == "pass"
    assert report["comparison_count"] == 2
    assert report["top_comparison_count"] == 1
    assert report["summary_binding"] == ["top", "top"]
    assert report["pins_binding"] == ["top", "top"]
    assert report["device_classes_binding"] == ["top", "top"]
    assert report["device_counts"] == [1, 1]
    assert report["node_counts"] == [2, 2]
    assert payload["data"]["comparison"]["structural_counts_agree"] is True


def test_unnamed_equivalent_pin_record_does_not_obscure_unique_top(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "netgen"
    document = json.loads(_pass_json())
    document.insert(0, {"pins": [["VDD", "VSS"], ["VDD", "VSS"]]})
    _fake_netgen(binary, json_body=json.dumps(document).encode())

    payload = _invoke(tmp_path, binary)

    comparison = payload["data"]["comparison"]
    assert payload["engineering"]["status"] == "pass"
    assert comparison["json_outcome"] == "pass"
    assert comparison["validation"]["valid"] is True
    assert comparison["comparison_count"] == 2
    assert comparison["top_comparison_count"] == 1
    assert comparison["device_counts"] == [[["nmos", 1]], [["nmos", 1]]]
    assert comparison["node_counts"] == [2, 2]
    assert comparison["mismatch_count"] == 0


@pytest.mark.parametrize(
    ("auxiliary", "detail"),
    [
        (
            {"pins": [["VDD", "VSS"], ["VDD", "VSSA"]]},
            "unnamed pin comparison is not equivalent",
        ),
        (
            {"pins": [["VDD", "VSS"], ["VDD", "VSS"]], "goodnets": []},
            "lacks two cell names",
        ),
    ],
    ids=["unequal-pins", "partial-known-shape"],
)
def test_ambiguous_unnamed_pin_record_forces_unknown(
    tmp_path: Path,
    auxiliary: dict[str, object],
    detail: str,
) -> None:
    binary = tmp_path / "netgen"
    document = json.loads(_pass_json())
    document.insert(0, auxiliary)
    _fake_netgen(binary, json_body=json.dumps(document).encode())

    payload = _invoke(tmp_path, binary)

    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert payload["engineering"]["status"] == "unknown"
    assert validation["reason"] == "json.invalid"
    assert detail in validation["detail"]


def test_duplicate_requested_top_report_sections_force_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(
        binary,
        report_body=_hierarchical_pass_report(duplicate_top=True),
        json_body=_hierarchical_pass_json(),
    )

    payload = _invoke(tmp_path, binary)

    report = payload["data"]["comparison"]["report"]
    assert payload["engineering"]["status"] == "unknown"
    assert report["outcome"] == "unknown"
    assert report["top_comparison_count"] == 2
    assert report["validation"]["reason"] == "report.unbound_top_cell"


def test_ignored_setup_tcl_error_cannot_be_promoted_to_pass(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _layout, _schematic, setup, _provenance = _inputs(tmp_path)
    _fake_netgen(
        binary,
        stdout_code="print('Warning:  There were errors reading the setup file')",
        stderr_text=f'Error {setup}:2 (ignoring), invalid command name "bad_setup_command"',
    )

    payload = NetgenDriver(str(binary)).lvs(
        tmp_path / "layout.spice",
        tmp_path / "schematic.spice",
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["lvs_match"] is None
    assessment = payload["data"]["transcript"]["assessment"]
    assert assessment["complete"] is True
    assert assessment["setup_error"] is True
    assert assessment["clean"] is False
    assert any(item["code"] == "netgen.setup_or_transcript_error" for item in payload["diagnostics"])


def test_setup_error_before_more_than_legacy_tail_bound_is_still_detected(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _layout, _schematic, setup, _provenance = _inputs(tmp_path)
    _fake_netgen(
        binary,
        stdout_code=(
            "print('Warning:  There were errors reading the setup file')\n"
            "print('x' * 13000)"
        ),
        stderr_text=f'Error {setup}:2 (ignoring), invalid command name "bad_setup_command"',
    )

    payload = NetgenDriver(str(binary)).lvs(
        tmp_path / "layout.spice",
        tmp_path / "schematic.spice",
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
    )

    transcript = payload["data"]["transcript"]
    assert transcript["stdout_observed_bytes"] > 12_000
    assert transcript["stdout_truncated"] is False
    assert transcript["assessment"]["setup_error"] is True
    assert payload["engineering"]["status"] == "unknown"


def test_stdout_error_word_without_colon_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, stdout_code="print('Error setup callback failed')")

    payload = _invoke(tmp_path, binary)

    assessment = payload["data"]["transcript"]["assessment"]
    assert payload["engineering"]["status"] == "unknown"
    assert assessment["stdout_error"] is True
    assert assessment["clean"] is False


def test_reviewed_netgen_permute_stderr_warnings_are_exposed_but_accepted(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(
        binary,
        stderr_text=(
            "Unable to permute model ptap1 pins 1, 2.\n"
            "Unable to permute model sg13_lv_nmos pins 1, 3."
        ),
    )

    payload = _invoke(tmp_path, binary)

    assessment = payload["data"]["transcript"]["assessment"]
    assert payload["engineering"]["status"] == "pass"
    assert assessment["stderr_empty"] is False
    assert assessment["stderr_accepted"] is True
    assert assessment["stderr_reviewed_warning_count"] == 2
    assert assessment["stderr_unrecognized_count"] == 0
    assert any(
        item["code"] == "netgen.stderr_reviewed_warning"
        and item["severity"] == "warning"
        for item in payload["diagnostics"]
    )


def test_unrecognized_stderr_remains_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, stderr_text="Unable to permute arbitrary unreviewed text")

    payload = _invoke(tmp_path, binary)

    assessment = payload["data"]["transcript"]["assessment"]
    assert payload["engineering"]["status"] == "unknown"
    assert assessment["stderr_accepted"] is False
    assert assessment["stderr_unrecognized_count"] == 1


def test_reviewed_stderr_grammar_rejects_non_ascii_identifier_syntax(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(
        binary,
        stderr_text="Unable to permute model {ptap1} pins 1, 2.",
    )

    payload = _invoke(tmp_path, binary)

    assessment = payload["data"]["transcript"]["assessment"]
    assert payload["engineering"]["status"] == "unknown"
    assert assessment["stderr_accepted"] is False
    assert assessment["stderr_reviewed_warning_count"] == 0


def test_truncated_process_stream_forces_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "netgen"
    monkeypatch.setattr(netgen_engine, "MAX_CAPTURE_LIMIT_BYTES", 1_024)
    _fake_netgen(binary, stdout_code="print('x' * 2048)")

    payload = _invoke(tmp_path, binary)

    transcript = payload["data"]["transcript"]
    assert transcript["stdout_truncated"] is True
    assert transcript["assessment"]["complete"] is False
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "transcript.incomplete" for item in payload["diagnostics"])


@pytest.mark.parametrize("which", ["report", "json", "transcript"])
@pytest.mark.parametrize("mode", ["regular", "symlink", "hardlink"])
def test_every_preexisting_evidence_entry_is_rejected_before_launch(
    tmp_path: Path,
    which: str,
    mode: str,
) -> None:
    binary = tmp_path / "netgen"
    marker = tmp_path / "actual-run"
    _fake_netgen(binary, run_marker=marker)
    report = tmp_path / "evidence" / "run.comp"
    report.parent.mkdir()
    paths = {
        "report": report,
        "json": report.with_suffix(".json"),
        "transcript": report.with_name(report.name + ".openada.log"),
    }
    output = paths[which]
    target = tmp_path / f"{which}-target"
    target.write_text("sentinel\n", encoding="utf-8")
    if mode == "regular":
        output.write_text("stale\n", encoding="utf-8")
    elif mode == "symlink":
        output.symlink_to(target)
    else:
        os.link(target, output)

    payload = _invoke(tmp_path, binary, report=report)

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "output.not_fresh" for item in payload["diagnostics"])
    assert not marker.exists()
    assert target.read_text(encoding="utf-8") == "sentinel\n"


@pytest.mark.parametrize("which", ["report", "json"])
@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_native_symlink_or_hardlink_evidence_is_never_trusted(
    tmp_path: Path,
    which: str,
    mode: str,
) -> None:
    binary = tmp_path / "netgen"
    kwargs = {f"{which}_mode": mode}
    _fake_netgen(binary, **kwargs)

    payload = _invoke(tmp_path, binary)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    capture = payload["data"][f"{which}_output"]["capture"]
    assert capture["status"] in {"not_regular", "hardlinked"}
    assert all(
        artifact["kind"] != f"netgen-comparison{'-json' if which == 'json' else ''}"
        for artifact in payload["artifacts"]
    )


@pytest.mark.parametrize(
    ("report_mode", "json_mode"),
    [("missing", "write"), ("write", "missing")],
)
def test_missing_native_evidence_forces_unknown(
    tmp_path: Path,
    report_mode: str,
    json_mode: str,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_mode=report_mode, json_mode=json_mode)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["lvs_match"] is None
    assert any(
        item["code"] in {"netgen.report_invalid", "netgen.json_invalid"}
        for item in payload["diagnostics"]
    )


@pytest.mark.parametrize(
    ("report_body", "json_body"),
    [
        (b"not a native comparison\n", _pass_json()),
        (_pass_report(final_result=False), b"{not-json"),
        (b"\xff\n", _pass_json()),
        (_pass_report(final_result=False), b""),
    ],
    ids=["malformed-report", "malformed-json", "invalid-report-utf8", "empty-json"],
)
def test_malformed_or_empty_native_evidence_forces_unknown(
    tmp_path: Path,
    report_body: bytes,
    json_body: bytes,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_body=report_body, json_body=json_body)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["lvs_match"] is None


@pytest.mark.parametrize(
    ("report_body", "json_body"),
    [
        (_pass_report(final_result=False), _mismatch_json()),
        (_mismatch_report(), _pass_json()),
    ],
    ids=["report-pass-json-fail", "report-fail-json-pass"],
)
def test_report_json_disagreement_forces_unknown(
    tmp_path: Path,
    report_body: bytes,
    json_body: bytes,
) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_body=report_body, json_body=json_body)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["comparison"]["evidence_agrees"] is False
    assert any(item["code"] == "netgen.evidence_conflict" for item in payload["diagnostics"])


def test_pass_report_with_unequal_native_counts_is_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    report = _pass_report(final_result=True).replace(
        b"Number of devices: 1",
        b"Number of devices: 2",
        1,
    )
    _fake_netgen(binary, report_body=report)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["comparison"]["report_outcome"] == "unknown"
    assert payload["data"]["comparison"]["structural_counts_agree"] is False


def test_report_json_structural_count_disagreement_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    report = _pass_report(final_result=True).replace(
        b"Number of devices: 1",
        b"Number of devices: 2",
    )
    _fake_netgen(binary, report_body=report)

    payload = _invoke(tmp_path, binary)

    comparison = payload["data"]["comparison"]
    assert payload["engineering"]["status"] == "unknown"
    assert comparison["report_outcome"] == "pass"
    assert comparison["json_outcome"] == "pass"
    assert comparison["outcomes_agree"] is True
    assert comparison["structural_counts_agree"] is False
    assert comparison["evidence_agrees"] is False
    assert any(item["code"] == "netgen.evidence_conflict" for item in payload["diagnostics"])


def test_unique_match_phrase_without_native_structure_is_not_a_pass(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, report_body=b"Circuits match uniquely.\n")

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["comparison"]["report_outcome"] == "unknown"


def test_mismatch_phrase_without_conclusive_native_structure_is_not_a_fail(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(
        binary,
        report_body=b"NET mismatches: injected phrase without a completed report\n",
        json_body=_mismatch_json(),
    )

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["comparison"]["report_outcome"] == "unknown"


def test_conflicting_terminal_results_are_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    report = (
        _pass_report(final_result=True)
        + b"Final result: Circuits do not match.\n"
    )
    _fake_netgen(binary, report_body=report, json_body=_mismatch_json())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["comparison"]["report_outcome"] == "unknown"


def test_property_error_is_a_clean_mismatch_when_json_corroborates_it(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    report = _pass_report(final_result=True).replace(
        b"Final result: Circuits match uniquely.",
        b"Final result: Property errors were found.",
    )
    _fake_netgen(
        binary,
        report_body=report,
        json_body=_mismatch_json(property_error=True),
    )

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["lvs_match"] is False


def test_legacy_property_error_is_a_clean_mismatch_when_json_corroborates_it(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "netgen"
    report = (
        _pass_report(final_result=False)
        + b"Property errors were found.\n"
        + b"The following cells had property errors: top\n"
    )
    _fake_netgen(
        binary,
        report_body=report,
        json_body=_mismatch_json(property_error=True),
    )

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["lvs_match"] is False


@pytest.mark.parametrize(
    "report_line",
    [
        b"Circuits match uniquely with port errors.",
        b"Top level cell failed pin matching.",
        b"Subcell(s) failed matching.",
    ],
)
def test_explicit_native_failure_forms_are_fail_when_json_corroborates(
    tmp_path: Path,
    report_line: bytes,
) -> None:
    binary = tmp_path / "netgen"
    report = (
        b"Subcircuit summary:\n"
        b"Circuit 1: top |Circuit 2: top\n"
        b"Number of devices: 1 |Number of devices: 1\n"
        b"Number of nets: 2 |Number of nets: 2\n"
        b"Final result: "
        + report_line
        + b"\n"
    )
    _fake_netgen(binary, report_body=report, json_body=_mismatch_json())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["lvs_match"] is False


def test_duplicate_json_keys_force_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    duplicate = (
        b'[{"name":["top","top"],"name":["spoof","spoof"],'
        b'"devices":[[["nmos",1]],[["nmos",1]]],"nets":[2,2],'
        b'"badnets":[],"badelements":[],"pins":[["a","y"],["a","y"]]}]'
    )
    _fake_netgen(binary, json_body=duplicate)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["json_output"]["capture"]["validation"]["reason"] == "json.invalid"
    assert "duplicate JSON key" in payload["data"]["json_output"]["capture"]["validation"]["detail"]


def test_deep_json_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    comparison = json.loads(_pass_json())[0]
    nested: object = 0
    for _ in range(netgen_outputs.MAX_JSON_DEPTH + 2):
        nested = [nested]
    comparison["goodnets"] = nested
    _fake_netgen(binary, json_body=json.dumps([comparison]).encode())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert validation["reason"] == "json.invalid"
    assert "depth bound" in validation["detail"]


def test_overlong_nested_json_key_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    comparison = json.loads(_pass_json())[0]
    comparison["goodnets"] = [{"x" * (netgen_outputs.MAX_JSON_STRING_CHARS + 1): 0}]
    _fake_netgen(binary, json_body=json.dumps([comparison]).encode())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert validation["reason"] == "json.invalid"
    assert "overlong string" in validation["detail"]


@pytest.mark.parametrize("tamper", ["duplicate-device", "duplicate-pin"])
def test_duplicate_native_json_identity_forces_unknown(
    tmp_path: Path,
    tamper: str,
) -> None:
    binary = tmp_path / "netgen"
    comparison = json.loads(_pass_json())[0]
    if tamper == "duplicate-device":
        comparison["devices"][0].append(["nmos", 0])
        comparison["devices"][1].append(["nmos", 0])
    else:
        comparison["pins"][0].append("a")
        comparison["pins"][1].append("a")
    _fake_netgen(binary, json_body=json.dumps([comparison]).encode())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert validation["reason"] == "json.invalid"


def test_oversized_json_forces_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "netgen"
    monkeypatch.setattr(netgen_outputs, "MAX_JSON_BYTES", 64)
    _fake_netgen(binary, json_body=_pass_json())

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["json_output"]["capture"]["status"] == "too_large"


def test_wrong_requested_top_in_json_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, json_body=_pass_json(cell="other"))

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert "requested top comparison" in validation["detail"]


def test_nonfinite_numeric_json_value_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    body = _pass_json().replace(b'"badelements": []', b'"badelements": [], "goodnets": [1e999]')
    _fake_netgen(binary, json_body=body)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["json_output"]["capture"]["validation"]
    assert validation["reason"] == "json.invalid"


def test_overlong_report_line_forces_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "netgen"
    monkeypatch.setattr(netgen_outputs, "MAX_REPORT_LINE_BYTES", 64)
    _fake_netgen(binary, report_body=b"x" * 65 + b"\nCircuits match uniquely.\n")

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    validation = payload["data"]["report_output"]["capture"]["validation"]
    assert "overlong line" in validation["detail"]


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_standalone_report_parser_rejects_linked_evidence(tmp_path: Path, mode: str) -> None:
    target = tmp_path / "target.comp"
    target.write_bytes(_pass_report(final_result=True))
    linked = tmp_path / "linked.comp"
    if mode == "symlink":
        linked.symlink_to(target)
    else:
        os.link(target, linked)

    parsed = NetgenDriver.parse_report(linked)

    assert parsed["validation"]["valid"] is False
    assert parsed["outcome"] == "unknown"


def test_invalid_report_path_type_returns_contract_result(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary)
    layout, schematic, setup, _provenance = _inputs(tmp_path)

    payload = NetgenDriver(str(binary)).lvs(
        layout,
        schematic,
        "top",
        setup,
        object(),
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "output.invalid" for item in payload["diagnostics"])


def test_tcl_list_unsafe_netlist_path_is_rejected_before_launch(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    marker = tmp_path / "actual-run"
    _fake_netgen(binary, run_marker=marker)
    layout, schematic, setup, _provenance = _inputs(tmp_path)
    unsafe_layout = tmp_path / "layout{alias}.spice"
    layout.rename(unsafe_layout)

    payload = NetgenDriver(str(binary)).lvs(
        unsafe_layout,
        schematic,
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "path.tcl_list_unsafe" for item in payload["diagnostics"])
    assert not marker.exists()


def test_over_limit_declared_input_is_rejected_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "netgen"
    marker = tmp_path / "actual-run"
    _fake_netgen(binary, run_marker=marker)
    layout, schematic, setup, _provenance = _inputs(tmp_path)
    monkeypatch.setattr(netgen_outputs, "MAX_INPUT_BYTES", 4)

    payload = NetgenDriver(str(binary)).lvs(
        layout,
        schematic,
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "input.too_large" for item in payload["diagnostics"])
    assert not marker.exists()


def test_provenance_change_during_netgen_run_forces_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    layout, schematic, setup, provenance = _inputs(tmp_path)
    _fake_netgen(binary, mutate_path=provenance)

    payload = NetgenDriver(str(binary)).lvs(
        layout,
        schematic,
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
        provenance_inputs=(provenance,),
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert str(provenance) in payload["data"]["changed_inputs"]
    assert any(item["code"] == "input.changed" for item in payload["diagnostics"])


def test_duplicate_provenance_input_is_invalid_request(tmp_path: Path) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary)
    layout, schematic, setup, _provenance = _inputs(tmp_path)

    payload = NetgenDriver(str(binary)).lvs(
        layout,
        schematic,
        "top",
        setup,
        tmp_path / "evidence" / "run.comp",
        provenance_inputs=(setup,),
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "input.duplicate" for item in payload["diagnostics"])


@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_native_transcript_collision_forces_unknown(tmp_path: Path, mode: str) -> None:
    binary = tmp_path / "netgen"
    _fake_netgen(binary, transcript_collision=mode)

    payload = _invoke(tmp_path, binary)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["transcript"]["status"] == "collision"
    assert all(artifact["kind"] != "netgen-transcript" for artifact in payload["artifacts"])
