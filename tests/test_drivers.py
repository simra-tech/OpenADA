from __future__ import annotations

import re

from openada.engines.klayout_engine import KLayoutDriver
from openada.engines.netgen import NetgenDriver
from openada.engines.spice import NgspiceDriver
from openada.engines.xschem import XschemDriver
from openada.engines.yosys import MAX_JSON_PARSE_BYTES, YosysDriver, _validate_json_netlist


def _write_executable(path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_xschem_driver_moves_expected_native_netlist(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--help' in sys.argv:
    print('  -o <dir> output directory')
    raise SystemExit(0)
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V1.0')
    raise SystemExit(0)
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
out.mkdir(parents=True, exist_ok=True)
(out / (source.stem + '.spice')).write_text('* generated\\n', encoding='utf-8')
""",
    )
    schematic = tmp_path / "design.sch"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")
    output = tmp_path / "evidence" / "design.spice"

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert output.read_text(encoding="utf-8") == "* generated\n"
    assert payload["artifacts"][0]["sha256"]


def test_xschem_legacy_cli_netlists_in_temporary_working_directory(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--help' in sys.argv:
    print('usage: xschem [-n] schematic')
    raise SystemExit(0)
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V2.8.1')
    raise SystemExit(0)
source = pathlib.Path(sys.argv[-1])
(pathlib.Path.cwd() / (source.stem + '.spice')).write_text('* generated legacy\\n')
""",
    )
    schematic = tmp_path / "design.sch"
    output = tmp_path / "evidence" / "design.spice"
    schematic.write_text("v {xschem version=2.8.1}\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["engineering"]["status"] == "pass"
    assert "-o" not in payload["execution"]["command"]
    assert output.read_text(encoding="utf-8") == "* generated legacy\n"


def test_xschem_modern_cli_runs_from_schematic_directory(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--help' in sys.argv:
    raise SystemExit(0)
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V3.4.8')
    raise SystemExit(0)
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
if pathlib.Path.cwd() != source.parent:
    raise SystemExit(10)
(out / (source.stem + '.spice')).write_text('* local symbols resolved\\n')
""",
    )
    project = tmp_path / "project"
    project.mkdir()
    schematic = project / "design.sch"
    output = tmp_path / "evidence" / "design.spice"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["engineering"]["status"] == "pass"
    assert output.read_text(encoding="utf-8") == "* local symbols resolved\n"


def test_xschem_passes_and_hashes_explicit_rcfile(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V3.4.8')
    raise SystemExit(0)
if '--help' in sys.argv or '-h' in sys.argv:
    raise SystemExit(0)
rcfile = pathlib.Path(sys.argv[sys.argv.index('--rcfile') + 1])
assert rcfile.read_text() == 'set library_path /pdk\\n'
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
(out / (source.stem + '.spice')).write_text('* pdk symbols resolved\\n')
""",
    )
    schematic = tmp_path / "design.sch"
    rcfile = tmp_path / "xschemrc"
    output = tmp_path / "evidence" / "design.spice"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")
    rcfile.write_text("set library_path /pdk\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(
        schematic,
        output,
        rcfile=rcfile,
    )

    assert payload["engineering"]["status"] == "pass"
    assert "--rcfile" in payload["execution"]["command"]
    assert payload["execution"]["command"][1:3] == ["--rcfile", str(rcfile.resolve())]
    config = next(item for item in payload["inputs"] if item["kind"] == "xschem-rcfile")
    assert config["sha256"]


def test_xschem_missing_symbols_are_engineering_failure_with_artifact(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V3.4.8')
    raise SystemExit(0)
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
(out / (source.stem + '.spice')).write_text(
    '* M1 - sg13_lv_nmos IS MISSING !!!!\\n', encoding='utf-8'
)
""",
    )
    schematic = tmp_path / "design.sch"
    output = tmp_path / "evidence" / "design.spice"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["missing_symbol_count"] == 1
    assert payload["artifacts"][0]["sha256"]
    assert any(
        item["code"] == "xschem.missing_symbol" for item in payload["diagnostics"]
    )


def test_xschem_bounds_missing_symbol_examples(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V3.4.8')
    raise SystemExit(0)
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
lines = [f'* M{index} - device IS MISSING !!!!' for index in range(55)]
(out / (source.stem + '.spice')).write_text('\\n'.join(lines) + '\\n')
""",
    )
    schematic = tmp_path / "design.sch"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(
        schematic,
        tmp_path / "evidence" / "design.spice",
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["missing_symbol_count"] == 55
    assert len(payload["data"]["missing_symbols"]) == 50
    assert payload["data"]["missing_symbols_truncated"] is True


def test_xschem_does_not_accept_stale_output(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import sys
if '--help' in sys.argv:
    print('  -o <dir> output directory')
elif '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V1.0')
""",
    )
    schematic = tmp_path / "design.sch"
    output = tmp_path / "design.spice"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")
    output.write_text("* stale\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["artifacts"] == []
    assert output.read_text(encoding="utf-8") == "* stale\n"


def test_xschem_empty_netlist_is_not_a_pass(tmp_path):
    binary = tmp_path / "xschem"
    _write_executable(
        binary,
        """import pathlib, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('XSCHEM V3.4.8')
    raise SystemExit(0)
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
source = pathlib.Path(sys.argv[-1])
(out / (source.stem + '.spice')).write_bytes(b'')
""",
    )
    schematic = tmp_path / "design.sch"
    output = tmp_path / "evidence" / "design.spice"
    schematic.write_text("v {xschem version=3.4.8}\n", encoding="utf-8")

    payload = XschemDriver(str(binary)).netlist(schematic, output)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["artifacts"][0]["bytes"] == 0
    assert any(item["code"] == "artifact.empty" for item in payload["diagnostics"])


def test_ngspice_driver_extracts_only_declared_measurements(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
    raise SystemExit(0)
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
script = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')
raw = pathlib.Path(re.findall(r'^\\s*write\\s+(\\S+)\\s*$', script, re.MULTILINE)[-1])
raw.write_text('Title: fake\\nPlotname: Transient Analysis\\nFlags: real\\nNo. Variables: 1\\nNo. Points: 1\\nVariables:\\n0 v(out) voltage\\nValues:\\n0 1.0\\n')
log.write_text('No. of Data Rows : 1\\nMeasurements for Transient Analysis\\n\\ndelay = 1.25e-09\\n\\nStack = 0 bytes\\n', encoding='utf-8')
""",
    )
    source = tmp_path / "tb.spice"
    source.write_text(".measure tran delay trig v(a) val=0.5 rise=1 targ v(b) val=0.5 rise=1\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurements"] == [
        {"name": "delay", "value": 1.25e-09, "raw": "1.25e-09"}
    ]
    assert len(payload["artifacts"]) == 3
    assert "-b" not in payload["execution"]["command"]
    assert "-r" not in payload["execution"]["command"]


def test_ngspice_uses_and_records_explicit_working_directory(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        """import pathlib, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
    raise SystemExit(0)
if not pathlib.Path('model.lib').is_file():
    raise SystemExit(10)
raw = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
raw.write_text('Title: fake\\nPlotname: Operating Point\\nFlags: real\\nNo. Variables: 1\\nNo. Points: 1\\nVariables:\\n0 v(out) voltage\\nValues:\\n0 1.0\\n')
log.write_text('project model resolved\\n', encoding='utf-8')
""",
    )
    source_dir = tmp_path / "generated"
    source_dir.mkdir()
    source = source_dir / "tb.spice"
    source.write_text(".op\n.end\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "model.lib").write_text("* model\n", encoding="utf-8")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        workdir=project,
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["working_directory"] == str(project.resolve())


def test_ngspice_rejects_invalid_working_directory(tmp_path):
    source = tmp_path / "tb.spice"
    source.write_text(".op\n.end\n", encoding="utf-8")
    not_a_directory = tmp_path / "model.lib"
    not_a_directory.write_text("* model\n", encoding="utf-8")

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
        workdir=not_a_directory,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "workdir.invalid" for item in payload["diagnostics"])


def test_ngspice_does_not_accept_stale_outputs(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
""",
    )
    source = tmp_path / "tb.spice"
    out = tmp_path / "out"
    source.write_text(".op\n.end\n", encoding="utf-8")
    out.mkdir()
    (out / "tb.log").write_text("old log\n", encoding="utf-8")
    (out / "tb.raw").write_bytes(b"old raw")

    payload = NgspiceDriver(str(binary)).simulate(source, out)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["artifacts"] == []
    assert any(item["code"] == "artifact.missing" for item in payload["diagnostics"])


def test_ngspice_empty_outputs_are_not_a_pass(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
    raise SystemExit(0)
pathlib.Path(sys.argv[sys.argv.index('-r') + 1]).write_bytes(b'')
pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes(b'')
""",
    )
    source = tmp_path / "tb.spice"
    source.write_text(".op\n.end\n", encoding="utf-8")

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["engineering"]["status"] == "unknown"
    assert len(payload["artifacts"]) == 2
    assert all(item["bytes"] == 0 for item in payload["artifacts"])
    assert any(item["code"] == "artifact.empty" for item in payload["diagnostics"])


def test_ngspice_rejects_output_that_would_overwrite_input(tmp_path):
    source = tmp_path / "tb.spice"
    source.write_text(".op\n.end\n", encoding="utf-8")

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
        raw_file=source,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert source.read_text(encoding="utf-8") == ".op\n.end\n"


def test_ngspice_nonfinite_measurement_prevents_engineering_pass(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
    raise SystemExit(0)
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
script = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')
raw = pathlib.Path(re.findall(r'^\\s*write\\s+(\\S+)\\s*$', script, re.MULTILINE)[-1])
raw.write_text('Title: fake\\nPlotname: AC Analysis\\nFlags: real\\nNo. Variables: 1\\nNo. Points: 1\\nVariables:\\n0 frequency frequency\\nValues:\\n0 1000.0\\n')
log.write_text('No. of Data Rows : 1\\nMeasurements for AC Analysis\\n\\ngain = 1e999\\n', encoding='utf-8')
""",
    )
    source = tmp_path / "tb.spice"
    source.write_text(".measure ac gain find v(out) at=1k\n.end\n", encoding="utf-8")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["measurements"] == []
    assert any(item["code"] == "measurement.nonfinite" for item in payload["diagnostics"])


def test_klayout_process_can_complete_while_drc_fails(tmp_path):
    binary = tmp_path / "klayout"
    _write_executable(
        binary,
        """import pathlib, sys
if '-v' in sys.argv or '--version' in sys.argv:
    print('KLayout 1.0')
    raise SystemExit(0)
report_arg = next(value for value in sys.argv if value.startswith('report='))
path = pathlib.Path(report_arg.split('=', 1)[1])
deck = pathlib.Path(sys.argv[sys.argv.index('-r') + 1]).resolve()
path.write_text(f\"\"\"<report-database>
<generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories><category><name>M1.W</name><description>width</description></category></categories>
<cells><cell><name>TOP</name></cell></cells>
<items><item><tags/><category>'M1.W'</category><cell>TOP</cell><multiplicity>1</multiplicity><values><value>box: (0,0;1,1)</value></values></item></items>
</report-database>\"\"\", encoding='utf-8')
""",
    )
    gds = tmp_path / "layout.gds"
    deck = tmp_path / "rules.drc"
    gds.write_bytes(b"gds")
    deck.write_text("report($report)\n", encoding="utf-8")

    payload = KLayoutDriver(str(binary)).drc(gds, deck, tmp_path / "drc.lyrdb")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["drc_clean"] is False
    assert payload["data"]["report"]["total_violations"] == 1


def test_klayout_does_not_parse_stale_report(tmp_path):
    binary = tmp_path / "klayout"
    _write_executable(
        binary,
        """import sys
if '-v' in sys.argv or '--version' in sys.argv:
    print('KLayout 1.0')
""",
    )
    gds = tmp_path / "layout.gds"
    deck = tmp_path / "rules.drc"
    report = tmp_path / "drc.lyrdb"
    gds.write_bytes(b"gds")
    deck.write_text("report($report)\n", encoding="utf-8")
    report.write_text("<report-database><items /></report-database>", encoding="utf-8")

    payload = KLayoutDriver(str(binary)).drc(gds, deck, report)

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["artifacts"] == []
    assert payload["data"]["report_output"]["fresh_required"] is True
    assert report.read_text(encoding="utf-8") == "<report-database><items /></report-database>"


def test_klayout_rejects_well_formed_foreign_xml(tmp_path):
    report = tmp_path / "not-a-report.xml"
    report.write_text("<not-a-report />", encoding="utf-8")

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert "error" in parsed


def test_klayout_rejects_incomplete_report_database(tmp_path):
    report = tmp_path / "incomplete.lyrdb"
    report.write_text("<report-database />", encoding="utf-8")

    parsed = KLayoutDriver.parse_lyrdb(report)

    assert "error" in parsed


def test_klayout_bounds_geometry_and_rejects_nonfinite_numbers(tmp_path):
    coordinates = " ".join(f"{index},{index}" for index in range(205))
    geometry = KLayoutDriver._parse_geometry("polygon: " + coordinates)
    nonfinite = KLayoutDriver._parse_geometry("box: (1e999,0;1,1)")

    assert geometry is not None
    assert len(geometry["coordinates"]) == 64
    assert geometry["coordinates_truncated"] is True
    assert nonfinite == {"type": "unknown", "raw": "box: (1e999,0;1,1)"}


def test_netgen_match_is_engineering_pass(tmp_path):
    binary = tmp_path / "netgen"
    _write_executable(
        binary,
        """import json, pathlib, sys
if sys.argv[1:] == ['-batch'] or '-version' in sys.argv or '--version' in sys.argv:
    print('Netgen 1.0 compiled on test date')
    raise SystemExit(0)
report = pathlib.Path(sys.argv[-2])
report.write_text('Subcircuit summary:\\nCircuit 1: top |Circuit 2: top\\nNumber of devices: 1 |Number of devices: 1\\nNumber of nets: 2 |Number of nets: 2\\nSubcircuit pins:\\nCircuit 1: top |Circuit 2: top\\nCell pin lists are equivalent.\\nDevice classes top and top are equivalent.\\nCircuits match uniquely.\\n')
report.with_suffix('.json').write_text(json.dumps([{
    'name': ['top', 'top'],
    'devices': [[['nmos', 1]], [['nmos', 1]]],
    'nets': [2, 2],
    'badnets': [],
    'badelements': [],
    'pins': [['a', 'y'], ['a', 'y']],
}]))
print('Reading setup file ' + sys.argv[-3])
print('LVS Done.')
""",
    )
    layout = tmp_path / "layout.spice"
    schematic = tmp_path / "schematic.spice"
    setup = tmp_path / "setup.tcl"
    for path in (layout, schematic, setup):
        path.write_text("# fixture\n", encoding="utf-8")

    payload = NetgenDriver(str(binary)).lvs(
        layout, schematic, "top", setup, tmp_path / "lvs.comp"
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["lvs_match"] is True


def test_netgen_empty_report_is_unknown_not_mismatch(tmp_path):
    binary = tmp_path / "netgen"
    _write_executable(
        binary,
        """import json, pathlib, sys
if sys.argv[1:] == ['-batch'] or '-version' in sys.argv or '--version' in sys.argv:
    print('Netgen 1.0 compiled on test date')
    raise SystemExit(0)
report = pathlib.Path(sys.argv[-2])
report.write_text('', encoding='utf-8')
report.with_suffix('.json').write_text(json.dumps([{
    'name': ['top', 'top'],
    'devices': [[['nmos', 1]], [['nmos', 1]]],
    'nets': [2, 2],
    'badnets': [],
    'badelements': [],
    'pins': [['a', 'y'], ['a', 'y']],
}]))
print('Reading setup file ' + sys.argv[-3])
print('LVS Done.')
""",
    )
    layout = tmp_path / "layout.spice"
    schematic = tmp_path / "schematic.spice"
    setup = tmp_path / "setup.tcl"
    for path in (layout, schematic, setup):
        path.write_text("# fixture\n", encoding="utf-8")

    payload = NetgenDriver(str(binary)).lvs(
        layout, schematic, "top", setup, tmp_path / "lvs.comp"
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["lvs_match"] is None


def test_netgen_negated_mismatch_text_does_not_report_failure(tmp_path):
    report = tmp_path / "lvs.comp"
    report.write_text(
        "Subcircuit summary:\n"
        "Circuit 1: top |Circuit 2: top\n"
        "Number of devices: 1 |Number of devices: 1\n"
        "Number of nets: 2 |Number of nets: 2\n"
        "Mismatch count: 0\n"
        "No mismatches found.\n"
        "Subcircuit pins:\n"
        "Circuit 1: top |Circuit 2: top\n"
        "Cell pin lists are equivalent.\n"
        "Device classes top and top are equivalent.\n"
        "Netlists match uniquely.\n",
        encoding="utf-8",
    )

    parsed = NetgenDriver.parse_report(report)

    assert parsed["lvs_match"] is True
    assert parsed["mismatch_count"] == 0


def test_yosys_driver_retains_script_and_json_artifacts(tmp_path):
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 1.0')
    raise SystemExit(0)
script = pathlib.Path(sys.argv[sys.argv.index('-s') + 1]).read_text()
match = re.search(r'write_json \\\"([^\\\"]+)\\\"', script)
pathlib.Path(match.group(1)).write_text('{"modules": {"top": {}}}', encoding='utf-8')
""",
    )
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    payload = YosysDriver(str(binary)).rtl_check([source], tmp_path / "out", top="top")

    assert payload["engineering"]["status"] == "pass"
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "yosys-script",
        "yosys-json",
    }
    script = (tmp_path / "out" / "rtl-check.ys").read_text(encoding="utf-8")
    assert re.search(r"hierarchy -check -top top", script)
    assert 'write_json "netlist.json"' in script
    assert ".openada-yosys-" not in script
    assert payload["execution"]["cwd"]


def test_yosys_does_not_accept_stale_json_netlist(tmp_path):
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        """import sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 1.0')
""",
    )
    source = tmp_path / "top.sv"
    out = tmp_path / "out"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    out.mkdir()
    (out / "rtl-check.json").write_text('{"stale": true}', encoding="utf-8")

    payload = YosysDriver(str(binary)).rtl_check([source], out, top="top")

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {"yosys-script"}


def test_yosys_error_text_prevents_pass_even_with_json_artifact(tmp_path):
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 1.0')
    raise SystemExit(0)
script = pathlib.Path(sys.argv[sys.argv.index('-s') + 1]).read_text()
path = re.search(r'write_json \"([^\"]+)\"', script).group(1)
pathlib.Path(path).write_text('{"modules": {"top": {}}}', encoding='utf-8')
print('ERROR: structural check failed', file=sys.stderr)
""",
    )
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    payload = YosysDriver(str(binary)).rtl_check([source], tmp_path / "out", top="top")

    assert payload["engineering"]["status"] == "fail"
    assert any(item["code"] == "yosys.error" for item in payload["diagnostics"])


def test_yosys_malformed_json_is_unknown(tmp_path):
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        """import pathlib, re, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 1.0')
    raise SystemExit(0)
script = pathlib.Path(sys.argv[sys.argv.index('-s') + 1]).read_text()
path = re.search(r'write_json \"([^\"]+)\"', script).group(1)
pathlib.Path(path).write_text('{not-json', encoding='utf-8')
""",
    )
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    payload = YosysDriver(str(binary)).rtl_check([source], tmp_path / "out", top="top")

    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "artifact.invalid_json" for item in payload["diagnostics"])


def test_yosys_json_validation_requires_a_nonempty_modules_object(tmp_path):
    netlist = tmp_path / "netlist.json"
    netlist.write_text("{}", encoding="utf-8")

    assert _validate_json_netlist(netlist) == (False, "missing-modules")


def test_yosys_oversized_json_is_not_promoted_by_a_brace_check(tmp_path):
    netlist = tmp_path / "large-netlist.json"
    netlist.write_bytes(b"{" + (b" " * MAX_JSON_PARSE_BYTES) + b"}")

    assert _validate_json_netlist(netlist) == (False, "too-large-to-validate")
