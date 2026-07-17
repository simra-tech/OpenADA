from __future__ import annotations

import json
import os
from pathlib import Path
import textwrap

import pytest

import openada.engines.klayout_engine as klayout_engine
from openada.cli import main
from openada.engines.klayout_engine import KLayoutDriver


def _write_executable(path: Path, body: str) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _inputs(tmp_path: Path, body: str) -> tuple[Path, Path, Path]:
    binary = tmp_path / "klayout"
    _write_executable(binary, body)
    gds = tmp_path / "layout.gds"
    deck = tmp_path / "rules.drc"
    gds.write_bytes(b"gds")
    deck.write_text("report($report)\n", encoding="utf-8")
    return binary, gds, deck


def _fake_body(action: str) -> str:
    return f"""
import pathlib, sys
if '-v' in sys.argv or '--version' in sys.argv:
    print('KLayout 1.0')
    raise SystemExit(0)
deck = pathlib.Path(sys.argv[sys.argv.index('-r') + 1]).resolve()
def variable(name):
    value = next(item for item in sys.argv if item.startswith(name + '='))
    return value.split('=', 1)[1]
def write_report(path, *, top='TOP', multiplicity=None, generator=None, description='width', tags='', category_declarations=None):
    item = '' if multiplicity is None else f\"<item><tags>{{tags}}</tags><category>'M1.W'</category><cell>{{top}}</cell><multiplicity>{{multiplicity}}</multiplicity><values><value>box: (0,0;1,1)</value></values></item>\"
    generator = generator or f\"drc: script='{{deck}}'\"
    category_declarations = category_declarations or f\"<category><name>M1.W</name><description>{{description}}</description></category>\"
    pathlib.Path(path).write_text(f\"\"\"<report-database>
<description>test</description><generator>{{generator}}</generator><top-cell>{{top}}</top-cell>
<categories>{{category_declarations}}</categories>
<cells><cell><name>{{top}}</name></cell></cells><items>{{item}}</items>
</report-database>\"\"\", encoding='utf-8')
{textwrap.dedent(action)}
"""


def _codes(payload: dict) -> set[str]:
    return {item["code"] for item in payload["diagnostics"]}


def test_zero_exit_minimal_spoof_is_unknown_but_regular_evidence_is_retained(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            "pathlib.Path(variable('report')).write_text(\n"
            "    '<report-database><categories/><items/></report-database>', encoding='utf-8'\n"
            ")"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["report_output"]["capture"]["status"] == "invalid"
    assert payload["data"]["report"]["validation"]["reason"] == "report.generator_missing"
    assert {item["kind"] for item in payload["artifacts"]} == {
        "klayout-lyrdb",
        "klayout-transcript",
    }


@pytest.mark.parametrize("link_function", ["symlink_to", "hardlink_to"])
def test_linked_report_cannot_be_engineering_evidence(
    tmp_path: Path,
    link_function: str,
) -> None:
    target = tmp_path / "old.lyrdb"
    target.write_text("<report-database />", encoding="utf-8")
    action = (
        f"pathlib.Path(variable('report')).{link_function}(pathlib.Path({str(target)!r}))"
    )
    binary, gds, deck = _inputs(tmp_path, _fake_body(action))

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["report_output"]["capture"]["status"] in {
        "not_regular",
        "hardlinked",
    }
    assert all(item["kind"] != "klayout-lyrdb" for item in payload["artifacts"])


def test_multiplicity_is_the_engineering_violation_count(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'), multiplicity=37)"),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["report"]["item_count"] == 1
    assert payload["data"]["report"]["total_violations"] == 37
    assert payload["data"]["report"]["violations"][0]["multiplicity"] == 37


def test_duplicate_native_categories_preserve_engineering_violation_count(
    tmp_path: Path,
) -> None:
    categories = (
        "<category><name>M1.W</name><description>width</description></category>"
        "<category><name>M1.W</name><description>width</description></category>"
        "<category><name>unused</name><description>first check</description></category>"
        "<category><name>unused</name><description>second check</description></category>"
    )
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            f"write_report(variable('report'), multiplicity=18, "
            f"category_declarations={categories!r})"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["report"]["category_count"] == 2
    assert payload["data"]["report"]["item_count"] == 1
    assert payload["data"]["report"]["total_violations"] == 18


def test_waived_markers_remain_engineering_violations(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'), multiplicity=7, tags='waived')"),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["report"]["total_violations"] == 7
    assert payload["data"]["report"]["waived_violations"] == 7
    assert payload["data"]["report"]["violations"][0]["tags"] == ["waived"]


@pytest.mark.parametrize("report_variable", ["input", "topcell"])
def test_report_variable_cannot_shadow_dedicated_bindings(
    tmp_path: Path,
    report_variable: str,
) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        report_variable=report_variable,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"report_variable.invalid"}
    assert not marker.exists()


def test_empty_top_cell_is_rejected_before_launch(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        top_cell="",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"top_cell.invalid"}
    assert not marker.exists()


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_code"),
    [
        ("workdir", "\0", "workdir.invalid"),
        ("workdir", object(), "workdir.invalid"),
        ("report", "\0", "deck_output.invalid"),
        ("report", object(), "deck_output.invalid"),
        ("provenance", "\0", "provenance_input.invalid"),
        ("provenance", object(), "provenance_input.invalid"),
    ],
)
def test_malformed_public_path_arguments_never_escape_the_contract(
    tmp_path: Path,
    field: str,
    invalid_value: object,
    expected_code: str,
) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )
    kwargs: dict[str, object] = {}
    report: object = tmp_path / "result.lyrdb"
    if field == "workdir":
        kwargs["workdir"] = invalid_value
    elif field == "report":
        report = invalid_value
    else:
        kwargs["provenance_inputs"] = [invalid_value]

    payload = KLayoutDriver(str(binary)).drc(gds, deck, report, **kwargs)

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == {expected_code}
    assert not marker.exists()


def test_script_owned_report_is_exact_relative_to_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    report_parent = workdir / "results"
    report_parent.mkdir(parents=True)
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(pathlib.Path.cwd() / 'results' / 'run.lyrdb')"),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        expected_report="results/run.lyrdb",
        workdir=workdir,
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["report_output"]["ownership"] == "script"
    assert not any(item.startswith("report=") for item in payload["execution"]["command"])
    assert payload["artifacts"][0]["path"] == str(report_parent / "run.lyrdb")


def test_variable_top_cell_deck_variables_and_provenance_are_recorded(tmp_path: Path) -> None:
    provenance = tmp_path / "rules.json"
    provenance.write_text('{"corner":"nominal"}\n', encoding="utf-8")
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('output'), top=variable('topcell'))"),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        top_cell="MY_TOP",
        report_variable="output",
        deck_variables={"threads": "2", "run_mode": "flat"},
        provenance_inputs=[provenance],
    )

    command = payload["execution"]["command"]
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["report"]["top_cell"] == "MY_TOP"
    assert "output=" + str(tmp_path / "result.lyrdb") in command
    assert "topcell=MY_TOP" in command
    assert "run_mode=flat" in command
    assert "threads=2" in command
    assert any(item["role"] == "rules-dependency" for item in payload["inputs"])
    assert payload["data"]["transitive_rule_inputs_enumerated"] is False


def test_literal_report_name_remains_reserved_with_custom_report_variable(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        report_variable="output",
        deck_variables={"report": "/tmp/other.lyrdb"},
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"deck_variable.reserved"}
    assert not marker.exists()


def test_cli_preserves_an_explicit_empty_deck_variable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'))"),
    )
    report = tmp_path / "result.lyrdb"

    exit_code = main(
        [
            "--compact",
            "--tool-path",
            f"klayout={binary}",
            "drc",
            str(gds),
            "--rules",
            str(deck),
            "--report",
            str(report),
            "--deck-var",
            "option=",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["engineering"]["status"] == "pass"
    assert "option=" in payload["execution"]["command"]
    assert payload["data"]["deck_variables"] == [{"name": "option", "value": ""}]


def test_changed_declared_input_forces_unknown(tmp_path: Path) -> None:
    provenance = tmp_path / "rules.json"
    provenance.write_text("before\n", encoding="utf-8")
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            f"pathlib.Path({str(provenance)!r}).write_text('after\\n', encoding='utf-8')\n"
            "write_report(variable('report'))"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        provenance_inputs=[provenance],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert str(provenance) in payload["data"]["changed_inputs"]
    assert "input.changed" in _codes(payload)


def test_gds_and_deck_must_resolve_to_distinct_inputs(tmp_path: Path) -> None:
    binary, gds, _deck = _inputs(tmp_path, _fake_body("raise SystemExit(99)"))

    payload = KLayoutDriver(str(binary)).drc(gds, gds, tmp_path / "result.lyrdb")

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"input.duplicate"}
    paths = [record["path"] for record in payload["inputs"]]
    assert len(paths) == len(set(paths))


def test_initial_input_hash_error_is_a_normalized_invalid_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, gds, deck = _inputs(tmp_path, _fake_body("raise SystemExit(99)"))
    original = klayout_engine.file_record

    def unreadable(path: str | Path, *, kind: str, role: str) -> dict:
        if Path(path) == deck:
            raise PermissionError("simulated unreadable deck")
        return original(path, kind=kind, role=role)

    monkeypatch.setattr(klayout_engine, "file_record", unreadable)
    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == {"input.unreadable"}


def test_input_rehash_error_after_execution_is_normalized_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provenance = tmp_path / "rules.json"
    provenance.write_text("before\n", encoding="utf-8")
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            f"pathlib.Path({str(provenance)!r}).unlink()\n"
            "write_report(variable('report'))"
        ),
    )
    original = klayout_engine.file_record

    def rehash(path: str | Path, *, kind: str, role: str) -> dict:
        if Path(path) == provenance and not provenance.exists():
            raise PermissionError("simulated post-run rehash failure")
        return original(path, kind=kind, role=role)

    monkeypatch.setattr(klayout_engine, "file_record", rehash)

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        provenance_inputs=[provenance],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert str(provenance) in payload["data"]["changed_inputs"]
    assert "input.changed" in _codes(payload)


def test_ambient_waiver_is_rejected_and_explicit_sidecar_is_hashed(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'))"),
    )
    report = tmp_path / "result.lyrdb"
    waiver = tmp_path / "result.lyrdb.w"
    waiver.write_text("waiver evidence\n", encoding="utf-8")

    undeclared = KLayoutDriver(str(binary)).drc(gds, deck, report)
    explicit = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        report,
        waiver_file=waiver,
    )

    assert undeclared["execution"]["status"] == "invalid_request"
    assert _codes(undeclared) == {"waiver.undeclared"}
    assert explicit["engineering"]["status"] == "pass"
    waiver_records = [
        item for item in explicit["inputs"] if item["kind"] == "klayout-waiver-database"
    ]
    assert len(waiver_records) == 1
    assert waiver_records[0]["sha256"]


def test_explicit_waiver_cannot_duplicate_a_provenance_input(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'))"),
    )
    report = tmp_path / "result.lyrdb"
    waiver = tmp_path / "result.lyrdb.w"
    waiver.write_text("waiver evidence\n", encoding="utf-8")

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        report,
        provenance_inputs=[waiver],
        waiver_file=waiver,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"waiver.invalid"}
    paths = [record["path"] for record in payload["inputs"]]
    assert len(paths) == len(set(paths))


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_explicit_waiver_must_be_anchored_single_link(
    tmp_path: Path,
    link_kind: str,
) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("write_report(variable('report'))"),
    )
    report = tmp_path / "result.lyrdb"
    target = tmp_path / "waiver-target"
    target.write_text("waiver evidence\n", encoding="utf-8")
    waiver = tmp_path / "result.lyrdb.w"
    if link_kind == "symlink":
        waiver.symlink_to(target)
    else:
        os.link(target, waiver)

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        report,
        waiver_file=waiver,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert "waiver.invalid" in _codes(payload)


def test_changed_explicit_waiver_forces_unknown(tmp_path: Path) -> None:
    report = tmp_path / "result.lyrdb"
    waiver = tmp_path / "result.lyrdb.w"
    waiver.write_text("before\n", encoding="utf-8")
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            "write_report(variable('report'))\n"
            f"pathlib.Path({str(waiver)!r}).write_text('after\\n', encoding='utf-8')"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        report,
        waiver_file=waiver,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["waiver_database"]["status"] == "changed_during_run"
    assert str(waiver) in payload["data"]["changed_inputs"]


def test_identical_waiver_inode_replacement_forces_unknown(tmp_path: Path) -> None:
    report = tmp_path / "result.lyrdb"
    waiver = tmp_path / "result.lyrdb.w"
    waiver.write_text("same bytes\n", encoding="utf-8")
    replacement = tmp_path / "replacement.w"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            "write_report(variable('report'))\n"
            f"replacement = pathlib.Path({str(replacement)!r})\n"
            "replacement.write_text('same bytes\\n', encoding='utf-8')\n"
            f"replacement.replace(pathlib.Path({str(waiver)!r}))"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        report,
        waiver_file=waiver,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["waiver_database"]["status"] == "changed_during_run"
    assert str(waiver) in payload["data"]["changed_inputs"]
    assert "waiver.changed" in _codes(payload)


def test_waiver_sidecar_appearing_during_run_forces_unknown(tmp_path: Path) -> None:
    report = tmp_path / "result.lyrdb"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            "write_report(variable('report'))\n"
            f"pathlib.Path({str(report) + '.w'!r}).write_text('late waiver')"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, report)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["waiver_database"]["status"] == "appeared_during_run"
    assert "waiver.changed" in _codes(payload)


def test_bounded_transcript_records_native_output_truncation(tmp_path: Path) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body("print('x' * 50000)\nwrite_report(variable('report'))"),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    transcript = payload["data"]["transcript"]
    assert payload["engineering"]["status"] == "pass"
    assert transcript["stdout_observed_bytes"] > 50_000
    assert transcript["stdout_retained_bytes"] == 12_000
    assert transcript["stdout_truncated"] is True
    assert len(transcript["stdout_tail"]) <= 4_000
    artifact = next(item for item in payload["artifacts"] if item["kind"] == "klayout-transcript")
    assert artifact["bytes"] <= 25_000


@pytest.mark.parametrize(
    ("raw_bytes", "expected_truncated"),
    [(1, False), (13_000, True)],
)
def test_transcript_distinguishes_raw_counts_from_normalized_utf8(
    tmp_path: Path,
    raw_bytes: int,
    expected_truncated: bool,
) -> None:
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(
            f"sys.stdout.buffer.write(b'\\xff' * {raw_bytes})\n"
            "sys.stdout.buffer.flush()\n"
            "write_report(variable('report'))"
        ),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    transcript = payload["data"]["transcript"]
    assert payload["engineering"]["status"] == "pass"
    assert transcript["stdout_observed_bytes"] == raw_bytes
    assert transcript["stdout_truncated"] is expected_truncated
    assert 0 < transcript["stdout_retained_bytes"] <= 12_000
    if expected_truncated:
        assert transcript["stdout_retained_bytes"] >= 11_997
    else:
        assert transcript["stdout_retained_bytes"] > transcript["stdout_observed_bytes"]
    transcript_path = next(
        Path(item["path"])
        for item in payload["artifacts"]
        if item["kind"] == "klayout-transcript"
    )
    body = transcript_path.read_bytes()
    body.decode("utf-8", errors="strict")
    assert len(body) <= 25_000


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("write_report(variable('report'), top='OTHER')", "report.top_cell_mismatch"),
        (
            "write_report(variable('report'), generator=\"drc: script='/wrong/deck.drc'\")",
            "report.generator_mismatch",
        ),
    ],
)
def test_report_identity_mismatch_is_unknown(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    binary, gds, deck = _inputs(tmp_path, _fake_body(mutation))

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        tmp_path / "result.lyrdb",
        top_cell="EXPECTED" if reason == "report.top_cell_mismatch" else None,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["report"]["validation"]["reason"] == reason


def test_symlinked_output_parent_is_rejected_before_launch(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )

    payload = KLayoutDriver(str(binary)).drc(gds, deck, linked / "result.lyrdb")

    assert payload["execution"]["status"] == "invalid_request"
    assert "deck_output.anchor_failed" in _codes(payload)
    assert not marker.exists()


def test_derived_sidecar_name_must_fit_name_max(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )
    report = tmp_path / ("r" * 250)

    payload = KLayoutDriver(str(binary)).drc(gds, deck, report)

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"deck_output.invalid"}
    assert not marker.exists()


def test_derived_names_obey_anchored_filesystem_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "launched"
    binary, gds, deck = _inputs(
        tmp_path,
        _fake_body(f"pathlib.Path({str(marker)!r}).touch()"),
    )

    def constrained_limit(_descriptor: int, name: str) -> int:
        return 16 if name == "PC_NAME_MAX" else 4_096

    monkeypatch.setattr(klayout_engine.os, "fpathconf", constrained_limit)
    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "result.lyrdb")

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"deck_output.invalid"}
    assert "anchored filesystem" in payload["diagnostics"][0]["message"]
    assert not marker.exists()


@pytest.mark.parametrize(
    "expected",
    [
        "../escape.lyrdb",
        "/absolute.lyrdb",
        "*.lyrdb",
        "./result.lyrdb",
        "results//run.lyrdb",
        "results/",
    ],
)
def test_script_report_must_be_one_safe_relative_path(tmp_path: Path, expected: str) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    binary, gds, deck = _inputs(tmp_path, _fake_body("raise SystemExit(99)"))

    payload = KLayoutDriver(str(binary)).drc(
        gds,
        deck,
        expected_report=expected,
        workdir=workdir,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert "deck_output.invalid" in _codes(payload)


def test_parser_bounds_repeated_text_and_uses_weighted_count(tmp_path: Path) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    description = "x" * 100_000
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"""<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories><category><name>A</name><description>{description}</description></category></categories>
<cells><cell><name>TOP</name></cell></cells>
<items><item><tags/><category>'A'</category><cell>TOP</cell><multiplicity>37</multiplicity>
<values><value>box: (0,0;1,1)</value></values></item></items></report-database>""",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)
    encoded = json.dumps(parsed)

    assert parsed["validation"]["valid"] is True
    assert parsed["item_count"] == 1
    assert parsed["total_violations"] == 37
    assert len(parsed["violations"][0]["description"]) <= 1_000
    assert len(encoded) < 20_000


def test_parser_preserves_hierarchical_categories_and_native_tags(tmp_path: Path) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"""<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories>
 <category><name>P1</name><categories><category><name>SAME</name></category></categories></category>
 <category><name>P2</name><categories><category><name>SAME</name></category></categories></category>
 <category><name>1/0</name><categories><category><name>A</name></category></categories></category>
</categories><cells><cell><name>TOP</name></cell></cells>
<items><item><tags>waived,important</tags><category>'1/0'.A</category><cell>TOP</cell>
<multiplicity>7</multiplicity><values/></item></items></report-database>""",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is True
    assert parsed["category_count"] == 6
    assert parsed["total_violations"] == 7
    assert parsed["waived_violations"] == 7
    assert parsed["violations"][0]["category_path"] == ["1/0", "A"]


def test_parser_rejects_item_referencing_conflicting_duplicate_category(
    tmp_path: Path,
) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"""<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories>
 <category><name>A</name><description>first check</description></category>
 <category><name>A</name><description>second check</description></category>
</categories><cells><cell><name>TOP</name></cell></cells>
<items><item><tags/><category>A</category><cell>TOP</cell><multiplicity>1</multiplicity>
</item></items></report-database>""",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is False
    assert parsed["validation"]["reason"] == "report.item_category_ambiguous"
    assert "category path 'A'" in parsed["error"]


def test_duplicate_category_declarations_remain_bounded(tmp_path: Path) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    declarations = "".join(
        "<category><name>A</name><description>same</description></category>"
        for _ in range(4_097)
    )
    report.write_text(
        f"<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP"
        f"</top-cell><categories>{declarations}</categories>"
        "<cells><cell><name>TOP</name></cell></cells><items/></report-database>",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is False
    assert parsed["validation"]["reason"] == "report.too_many_categories"


def test_parser_accepts_cell_variants_and_global_dummy_cell(tmp_path: Path) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"""<report-database><generator>drc: script='{deck}'</generator><top-cell>RINGO</top-cell>
<categories><category><name>A</name></category></categories>
<cells><cell><name>RINGO</name><variant>1</variant></cell><cell><name/><variant/></cell></cells>
<items>
 <item><tags/><category>A</category><cell>RINGO:1</cell><multiplicity>2</multiplicity></item>
 <item><tags/><category>A</category><cell/><multiplicity>3</multiplicity></item>
</items></report-database>""",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is True
    assert parsed["cell_count"] == 2
    assert parsed["total_violations"] == 5
    assert {item["cell"] for item in parsed["violations"]} == {"RINGO:1", ""}


def test_parser_decodes_native_escaped_generator_path(tmp_path: Path) -> None:
    quoted = tmp_path / "quo'te"
    quoted.mkdir()
    deck = quoted / "rules.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    encoded_deck = str(deck).replace("\\", "\\\\").replace("'", "\\'")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"<report-database><generator>drc: script='{encoded_deck}'</generator>"
        "<top-cell>TOP</top-cell><categories><category><name>A</name></category>"
        "</categories><cells><cell><name>TOP</name></cell></cells><items/>"
        "</report-database>",
        encoding="utf-8",
    )

    parsed = klayout_engine.parse_lyrdb(report, expected_deck=deck)

    assert parsed["validation"]["valid"] is True
    assert parsed["generator_script"] == str(deck)


@pytest.mark.parametrize(
    ("directory_name", "native_name"),
    [
        ("line\nbreak", r"line\nbreak"),
        (r"literal\ntext", r"literal\\ntext"),
    ],
)
def test_parser_distinguishes_native_control_escape_from_literal_backslash(
    tmp_path: Path,
    directory_name: str,
    native_name: str,
) -> None:
    directory = tmp_path / directory_name
    directory.mkdir()
    deck = directory / "rules.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    encoded_prefix = str(tmp_path).replace("\\", "\\\\").replace("'", "\\'")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"<report-database><generator>drc: script='{encoded_prefix}/{native_name}/rules.drc'"
        "</generator><top-cell>TOP</top-cell><categories><category><name>A</name>"
        "</category></categories><cells><cell><name>TOP</name></cell></cells><items/>"
        "</report-database>",
        encoding="utf-8",
    )

    parsed = klayout_engine.parse_lyrdb(report, expected_deck=deck)

    assert parsed["validation"]["valid"] is True
    assert parsed["generator_script"] == str(deck)


@pytest.mark.parametrize(
    ("item_tail", "reason"),
    [
        ("", "report.item_structure_invalid"),
        ("<multiplicity>0</multiplicity>", "report.multiplicity_invalid"),
        ("<multiplicity> 1 </multiplicity><multiplicity>1</multiplicity>", "report.item_structure_invalid"),
        (
            "<multiplicity>1</multiplicity><tags>waived,waived</tags>",
            "report.item_tags_invalid",
        ),
    ],
)
def test_parser_requires_one_positive_multiplicity_and_unique_tags(
    tmp_path: Path,
    item_tail: str,
    reason: str,
) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    tags = "<tags/>" if "<tags>" not in item_tail else ""
    report.write_text(
        f"<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>"
        "<categories><category><name>A</name></category></categories>"
        "<cells><cell><name>TOP</name></cell></cells><items><item>"
        f"{tags}<category>A</category><cell>TOP</cell>{item_tail}</item></items>"
        "</report-database>",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is False
    assert parsed["validation"]["reason"] == reason


@pytest.mark.parametrize(
    ("report_body", "reason"),
    [
        (
            "<categories/><other><category><name>A</name></category></other>"
            "<cells><cell><name>TOP</name></cell></cells><items/>",
            "report.category_structure_invalid",
        ),
        (
            "<categories><category><name>A</name></category></categories>"
            "<cells/><other><cell><name>TOP</name></cell></other><items/>",
            "report.cell_structure_invalid",
        ),
        (
            "<categories><category><name>A</name></category></categories>"
            "<cells><cell><name>TOP</name></cell></cells>"
            "<items><item><tags/><category>B</category><cell>TOP</cell>"
            "<multiplicity>1</multiplicity></item></items>",
            "report.item_category_unknown",
        ),
        (
            "<categories><category><name>A</name></category></categories>"
            "<cells><cell><name>TOP</name></cell></cells>"
            "<items><item><tags/><category>A</category><cell>OTHER</cell>"
            "<multiplicity>1</multiplicity></item></items>",
            "report.item_cell_unknown",
        ),
        (
            "<categories><category><name>A</name></category>"
            "<junk><categories><category><name>B</name></category></categories></junk>"
            "</categories><cells><cell><name>TOP</name></cell></cells><items/>",
            "report.category_structure_invalid",
        ),
        (
            "<categories><category><name>A</name></category></categories>"
            "<cells><cell><name>TOP</name></cell><cell><name/><variant>1</variant>"
            "</cell></cells><items/>",
            "report.cell_invalid",
        ),
    ],
)
def test_parser_rejects_non_native_declarations_and_unknown_references(
    tmp_path: Path,
    report_body: str,
    reason: str,
) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"<report-database><generator>drc: script='{deck}'</generator>"
        f"<top-cell>TOP</top-cell>{report_body}</report-database>",
        encoding="utf-8",
    )

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is False
    assert parsed["validation"]["reason"] == reason


@pytest.mark.parametrize(
    "body",
    [
        (
            "<report-database xmlns='urn:not-klayout'><generator>drc: script='{deck}'"
            "</generator><top-cell>TOP</top-cell><categories><category><name>A</name>"
            "</category></categories><cells><cell><name>TOP</name></cell></cells>"
            "<items/></report-database>"
        ),
        (
            "<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP"
            "</top-cell><categories xmlns='urn:not-klayout'><category><name>A</name>"
            "</category></categories><cells><cell><name>TOP</name></cell></cells>"
            "<items/></report-database>"
        ),
    ],
)
def test_parser_rejects_xml_namespaces(tmp_path: Path, body: str) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(body.format(deck=deck), encoding="utf-8")

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is False
    assert parsed["validation"]["reason"] == "report.namespace_unsupported"


def test_standalone_parser_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    target = tmp_path / "target.lyrdb"
    target.write_text("<report-database />", encoding="utf-8")
    symlink = tmp_path / "symlink.lyrdb"
    symlink.symlink_to(target)
    hardlink = tmp_path / "hardlink.lyrdb"
    os.link(target, hardlink)

    assert KLayoutDriver.parse_lyrdb(symlink)["validation"]["reason"] == "file.not_regular"
    assert KLayoutDriver.parse_lyrdb(hardlink)["validation"]["reason"] == "file.hardlinked"


@pytest.mark.parametrize("path", ["\0", object()])
def test_standalone_parser_normalizes_malformed_paths(path: object) -> None:
    parsed = KLayoutDriver.parse_lyrdb(path)

    assert parsed["validation"] == {"valid": False, "reason": "file.unreadable"}
    assert len(parsed["error"]) <= 4_000


def test_standalone_parser_opens_nonblocking_before_fstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deck = tmp_path / "deck.drc"
    deck.write_text("# deck\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    report.write_text(
        f"<report-database><generator>drc: script='{deck}'</generator><top-cell>TOP"
        "</top-cell><categories><category><name>A</name></category></categories>"
        "<cells><cell><name>TOP</name></cell></cells><items/></report-database>",
        encoding="utf-8",
    )
    original_open = klayout_engine.os.open
    observed_flags: list[int] = []

    def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path) == report:
            observed_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(klayout_engine.os, "open", recording_open)
    parsed = KLayoutDriver.parse_lyrdb(report)

    assert parsed["validation"]["valid"] is True
    assert observed_flags
    assert all(flags & os.O_NONBLOCK for flags in observed_flags)
