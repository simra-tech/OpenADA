from __future__ import annotations

import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from openada.engines.verilator import VerilatorDriver
from openada.engines.yosys import YosysDriver


ROOT = Path(__file__).resolve().parents[1]


def _validate_data(profile_name: str, payload: dict) -> None:
    profile = json.loads(
        (ROOT / "profiles" / profile_name).read_text(encoding="utf-8")
    )
    Draft202012Validator(profile["normalized_result"]["data_schema"]).validate(
        payload["data"]
    )


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _rtl(tmp_path: Path) -> Path:
    source = tmp_path / "top.sv"
    source.write_text('`include "defs.svh"\nmodule top; endmodule\n', encoding="utf-8")
    (tmp_path / "defs.svh").write_text("`define WIDTH 8\n", encoding="utf-8")
    return source


def test_verilator_strict_lint_pass_hashes_literal_include_closure(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["warning_policy"] == "strict"
    assert payload["data"]["include_dependencies"] == [str(tmp_path / "defs.svh")]
    assert {item["role"] for item in payload["inputs"]} == {
        "rtl.source",
        "rtl.include",
    }
    assert payload["artifacts"][0]["role"] == "rtl.lint.log"
    assert payload["data"]["inputs_stable"] is True
    assert payload["data"]["dependency_closure_stable"] is True
    assert "--relative-includes" in payload["execution"]["command"]
    assert {
        "+1800-2017ext+v",
        "+1800-2017ext+sv",
        "+1800-2017ext+vh",
        "+1800-2017ext+svh",
    }.issubset(payload["execution"]["command"])
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_warning_is_a_strict_engineering_failure(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print('%Warning-LATCH: top.sv:1: inferred latch', file=sys.stderr)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["warning_count"] == 1
    assert payload["data"]["diagnostics"][0]["code"] == "LATCH"
    assert payload["data"]["diagnostics"][0]["classification"] == "design-finding"
    assert payload["data"]["unclassified_diagnostic_count"] == 0
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_unclassified_nonzero_exit_is_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    raise SystemExit(7)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "verilator.unclassified_exit"
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_capability_error_is_unknown_not_an_rtl_failure(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print('%Error: Invalid option: --relative-includes', file=sys.stderr)
    raise SystemExit(1)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["unclassified_diagnostic_count"] == 1
    assert payload["data"]["diagnostics"][0]["classification"] == "unclassified"
    assert any(
        item["code"] == "verilator.unclassified_diagnostic"
        for item in payload["diagnostics"]
    )
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_option_syntax_error_is_not_a_design_finding(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print('%Error: Invalid option syntax error near --relative-includes', file=sys.stderr)
    raise SystemExit(1)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["diagnostics"] == [
        {
            "severity": "error",
            "code": "UNCLASSIFIED",
            "message": "Invalid option syntax error near --relative-includes",
            "classification": "unclassified",
        }
    ]
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_unparsed_diagnostic_prefix_cannot_be_silently_clean(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print('%Warning-FUTURE-CODE: future diagnostic grammar', file=sys.stderr)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["unclassified_diagnostic_count"] == 1
    assert payload["data"]["diagnostics"] == [
        {
            "severity": "warning",
            "code": "UNCLASSIFIED",
            "message": "%Warning-FUTURE-CODE: future diagnostic grammar",
            "classification": "unclassified",
        }
    ]
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_truncation_never_hides_the_unknown_reason(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    for index in range(100):
        print(f'%Warning-WIDTH: top.sv:{index + 1}:1: width finding', file=sys.stderr)
    print('%Warning-FUTURE-CODE: future diagnostic grammar', file=sys.stderr)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["diagnostic_count"] == 101
    assert payload["data"]["unclassified_diagnostic_count"] == 1
    assert payload["data"]["diagnostics_truncated"] is True
    assert all(
        item["classification"] == "design-finding"
        for item in payload["data"]["diagnostics"]
    )
    aggregate = [
        item
        for item in payload["diagnostics"]
        if item["code"] == "verilator.unclassified_diagnostic"
    ]
    assert len(aggregate) == 1
    assert "after the first 100 retained records" in aggregate[0]["message"]
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_source_located_syntax_error_is_a_design_failure(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    source = _rtl(tmp_path)
    _write_executable(
        binary,
        f"""import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print({f'%Error: {source}:3:7: syntax error, unexpected token'!r}, file=sys.stderr)
    print('%Error: Exiting due to 1 error(s)', file=sys.stderr)
    raise SystemExit(1)
""",
    )

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["unclassified_diagnostic_count"] == 0
    assert all(
        item["classification"] == "design-finding"
        for item in payload["data"]["diagnostics"]
    )
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_ambient_flags_cannot_suppress_strict_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import os, sys
blocked = (
    'VERILATOR_TEST_FLAGS', 'VERILATOR_BIN', 'LD_PRELOAD',
    'DYLD_INSERT_LIBRARIES', 'PERL5OPT', 'PERL5LIB', 'PYTHONPATH',
    'BASH_ENV', 'ENV', 'IFS', 'CDPATH', 'SHELLOPTS',
)
if '--version' in sys.argv:
    print('Verilator 5.050')
elif any(os.environ.get(key) for key in blocked):
    pass
else:
    print('%Warning-WIDTH: top.sv:1: strict warning remained enabled', file=sys.stderr)
""",
    )
    injected_environment = {
        "VERILATOR_TEST_FLAGS": "-Wno-WIDTH -Wno-UNUSEDSIGNAL",
        "VERILATOR_BIN": str(tmp_path / "unbound-verilator-bin"),
        "LD_PRELOAD": str(tmp_path / "unbound-loader.so"),
        "DYLD_INSERT_LIBRARIES": str(tmp_path / "unbound-loader.dylib"),
        "PERL5OPT": "-Munbound_module",
        "PERL5LIB": str(tmp_path / "unbound-perl"),
        "PYTHONPATH": str(tmp_path / "unbound-python"),
        "BASH_ENV": str(tmp_path / "unbound-bash-env"),
        "ENV": str(tmp_path / "unbound-shell-env"),
        "IFS": "unbound-ifs",
        "CDPATH": str(tmp_path),
        "SHELLOPTS": "xtrace",
    }
    for key, value in injected_environment.items():
        monkeypatch.setenv(key, value)
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["environment_policy"] == (
        "closed-verilator-runtime-v1"
    )
    assert payload["data"]["diagnostics"][0]["code"] == "WIDTH"
    assert "-Wno-WIDTH" not in payload["execution"]["command"]
    assert "-Wno-UNUSEDSIGNAL" not in payload["execution"]["command"]
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_missing_top_is_a_closed_design_failure(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    print("%Error: Specified --top-module 'missing' was not found in design.", file=sys.stderr)
    print('%Error: Exiting due to 1 error(s)', file=sys.stderr)
    raise SystemExit(1)
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="missing", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["unclassified_diagnostic_count"] == 0
    assert all(
        item["classification"] == "design-finding"
        for item in payload["data"]["diagnostics"]
    )
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_wrong_banner_verilator_is_never_executed(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    marker = tmp_path / "executed"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '--version' in sys.argv:
    print('unrelated tool 5.050')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(item["code"] == "tool.unusable" for item in payload["diagnostics"])
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_version_probe_is_bound_to_the_executed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_dir = tmp_path / "selected"
    inspected_dir = tmp_path / "inspected"
    selected_dir.mkdir()
    inspected_dir.mkdir()
    selected = selected_dir / "verilator"
    inspected = inspected_dir / "verilator"
    marker = tmp_path / "selected-executed"
    _write_executable(
        selected,
        f"""import pathlib, sys
if '--version' in sys.argv:
    print('Verilator 5.049')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    _write_executable(
        inspected,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
""",
    )
    monkeypatch.setenv("PATH", str(selected_dir))
    driver = VerilatorDriver()
    monkeypatch.setenv("PATH", str(inspected_dir))
    source = _rtl(tmp_path)

    payload = driver.rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "not_available"
    assert payload["tool"] == {
        "name": "verilator",
        "path": str(selected.resolve()),
        "version": None,
    }
    assert payload["data"]["tool_identity_stable"] is False
    assert not marker.exists()
    assert any(item["code"] == "tool.changed" for item in payload["diagnostics"])
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_transcript_race_is_unknown_and_never_overwrites(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    out = tmp_path / "out"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    pathlib.Path({str(out / 'rtl-lint.log')!r}).write_text('racing writer\\n')
""",
    )
    source = _rtl(tmp_path)

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], out, top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert (out / "rtl-lint.log").read_text(encoding="utf-8") == "racing writer\n"
    assert any(
        item["code"] == "artifact.uncaptured" for item in payload["diagnostics"]
    )
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_input_mutation_is_unknown(tmp_path: Path) -> None:
    source = _rtl(tmp_path)
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    pathlib.Path({str(source)!r}).write_text('module top; wire changed; endmodule\\n')
""",
    )

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[tmp_path]
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert any(item["code"] == "input.changed" for item in payload["diagnostics"])
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_invalid_request_data_stays_in_closed_schema(tmp_path: Path) -> None:
    binary = tmp_path / "verilator"
    _write_executable(binary, "import sys\n")

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [], tmp_path / "out", top="x" * 300, defines=["D", "D"]
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_non_string_language_is_a_closed_invalid_request(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(binary, "import sys\n")

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [_rtl(tmp_path)], tmp_path / "out", top="top", language=[]  # type: ignore[arg-type]
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["language"] is None
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_rejects_control_character_source_paths_before_execution(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    marker = tmp_path / "executed"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    source = tmp_path / "top\n.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top"
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(
        "ASCII control character" in item["message"]
        for item in payload["diagnostics"]
    )
    _validate_data("rtl.lint-v1alpha1.json", payload)


def test_verilator_relative_include_evidence_wins_over_include_root_copy(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "verilator"
    _write_executable(
        binary,
        """import sys
if '--version' in sys.argv:
    print('Verilator 5.050')
elif '--relative-includes' not in sys.argv:
    raise SystemExit(9)
""",
    )
    source_dir = tmp_path / "rtl"
    include_dir = tmp_path / "include"
    source_dir.mkdir()
    include_dir.mkdir()
    source = source_dir / "top.v"
    source.write_text('`include "defs.vh"\nmodule top; endmodule\n', encoding="utf-8")
    local = source_dir / "defs.vh"
    local.write_text("`define SELECTED_LOCAL 1\n", encoding="utf-8")
    (include_dir / "defs.vh").write_text(
        "`define WRONG_INCLUDE_ROOT_COPY 1\n", encoding="utf-8"
    )

    payload = VerilatorDriver(str(binary)).rtl_lint(
        [source], tmp_path / "out", top="top", include_dirs=[include_dir]
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["include_dependencies"] == [str(local)]
    assert [item["path"] for item in payload["inputs"] if item["role"] == "rtl.include"] == [
        str(local)
    ]


def _liberty(tmp_path: Path) -> Path:
    liberty = tmp_path / "cells.lib"
    liberty.write_text(
        "library(test) {\n  cell(AND2_X1) { area : 1.0; }\n}\n",
        encoding="utf-8",
    )
    return liberty


def _stats(
    cell_type: str = "AND2_X1",
    *,
    top: str = "top",
    num_memory_bits: int = 0,
) -> dict:
    record = {
        "num_cells": 1,
        "num_memories": 0,
        "num_memory_bits": num_memory_bits,
        "num_processes": 0,
        "area": 1.0,
        "sequential_area": 0.0,
        "num_cells_by_type": {cell_type: 1},
    }
    return {"modules": {f"\\{top}": record}, "design": record}


def _mapped_json(cell_type: str = "AND2_X1", *, top: str = "top") -> dict:
    return {
        "modules": {
            top: {
                "attributes": {"top": "00000000000000000000000000000001"},
                "cells": {"u": {"type": cell_type}},
            }
        }
    }


def _fake_yosys(path: Path, *, cell_type: str = "AND2_X1") -> None:
    stats = json.dumps(_stats(cell_type))
    mapped_json = json.dumps(_mapped_json(cell_type))
    _write_executable(
        path,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )


def _fake_abc(path: Path) -> Path:
    _write_executable(
        path,
        """import sys
if sys.argv[1:] == ['-c', 'version']:
    print('UC Berkeley, ABC 1.01 (compiled OpenADA unit test)')
    raise SystemExit(0)
raise SystemExit(2)
""",
    )
    return path


def _yosys_driver(binary: Path, tmp_path: Path) -> YosysDriver:
    return YosysDriver(
        str(binary),
        abc_binary_path=str(_fake_abc(tmp_path / "bound-yosys-abc")),
    )


def test_yosys_synthesis_validates_liberty_mapping_and_normalizes_statistics(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    _fake_yosys(binary)
    source = _rtl(tmp_path)

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["mapping_complete"] is True
    assert payload["data"]["stats"]["num_cells"] == 1
    assert payload["data"]["inference_stats"] == payload["data"]["stats"]
    assert payload["data"]["mapped_structure"] == {
        "top": "top",
        "num_cells": 1,
        "num_cells_by_type": {"AND2_X1": 1},
    }
    assert payload["data"]["unmapped_cell_types"] == []
    assert {
        "synthesis.script",
        "synthesis.log",
        "synthesis.inference-statistics",
        "synthesis.statistics",
        "synthesis.netlist",
        "synthesis.netlist-structure",
    }.issubset({item["role"] for item in payload["artifacts"]})
    assert payload["data"]["inputs_stable"] is True
    assert payload["data"]["dependency_closure_stable"] is True
    assert payload["data"]["tool_identity_stable"] is True
    abc_input = next(
        item
        for item in payload["inputs"]
        if item["role"] == "synthesis.abc-executable"
    )
    assert payload["data"]["abc_tool"] == {
        "name": "abc",
        "path": abc_input["path"],
        "version": "UC Berkeley, ABC 1.01 (compiled OpenADA unit test)",
        "bytes": abc_input["bytes"],
        "sha256": abc_input["sha256"],
    }
    assert payload["data"]["abc_tool_identity_stable"] is True
    assert payload["data"]["environment_policy"]["id"] == "closed-yosys-abc-v1"
    assert payload["data"]["environment_policy"]["inherit_parent"] is False
    assert (
        f'abc -exe "{abc_input["path"]}"'
        in (tmp_path / "out" / "synthesize.ys").read_text(encoding="utf-8")
    )
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_synthesis_reports_a_trustworthy_unmapped_cell_failure(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    _fake_yosys(binary, cell_type="$lut")
    source = _rtl(tmp_path)

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["mapping_complete"] is False
    assert payload["data"]["unmapped_cell_types"] == ["$lut"]
    assert any(item["code"] == "synthesis.unmapped_cells" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_synthesis_never_accepts_stale_outputs(tmp_path: Path) -> None:
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        """import sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
""",
    )
    source = _rtl(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "mapped.v").write_text("stale\n", encoding="utf-8")
    (out / "mapped-stats.json").write_text(json.dumps(_stats()), encoding="utf-8")

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        out,
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert not any(item["role"] == "synthesis.netlist" for item in payload["artifacts"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_synthesis_input_mutation_is_unknown(tmp_path: Path) -> None:
    source = _rtl(tmp_path)
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path({str(source)!r}).write_text('module top; wire changed; endmodule\\n')
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert any(item["code"] == "input.changed" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_invalid_request_data_stays_in_closed_schema(tmp_path: Path) -> None:
    binary = tmp_path / "yosys"
    _write_executable(binary, "import sys\n")

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [],
        _liberty(tmp_path),
        tmp_path / "out",
        top="x" * 300,
        dont_use=["AND2_X1", "AND2_X1"],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_inherits_control_character_hdl_path_rejection(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    marker = tmp_path / "executed"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    source = tmp_path / "top\n.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(
        "ASCII control character" in item["message"]
        for item in payload["diagnostics"]
    )
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_rejects_an_ieee_selector_on_the_builtin_frontend(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    _write_executable(binary, "import sys\n")
    source = _rtl(tmp_path)

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        frontend="verilog",
        language="1800-2017",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["data"]["language"] is None
    assert any(
        "unsupported SystemVerilog language revision" in item["message"]
        for item in payload["diagnostics"]
    )
    _validate_data("logic.synthesize-v1alpha1.json", payload)


@pytest.mark.parametrize("define", ["NAME=1; check -assert", "NAME=hello world", 'NAME="x"'])
def test_yosys_rejects_define_tokens_that_could_change_the_generated_script(
    tmp_path: Path, define: str
) -> None:
    binary = tmp_path / "yosys"
    _write_executable(binary, "import sys\n")
    source = _rtl(tmp_path)

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [source],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        defines=[define],
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert not (tmp_path / "out" / "synthesize.ys").exists()
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_requires_an_accepted_inspection_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('unidentified synthesis executable')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(item["code"] == "tool.unusable" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_requires_an_external_abc_before_creating_synthesis_evidence(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    _fake_yosys(binary)
    driver = YosysDriver(
        str(binary),
        abc_binary_path=str(tmp_path / "missing-yosys-abc"),
    )

    payload = driver.synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not (tmp_path / "out" / "synthesize.ys").exists()
    assert any(item["code"] == "abc.missing" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_rejects_an_unidentified_external_abc_before_execution(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    marker = tmp_path / "executed"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    abc = tmp_path / "yosys-abc"
    _write_executable(abc, "print('not an ABC identity')\n")

    payload = YosysDriver(
        str(binary), abc_binary_path=str(abc)
    ).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(item["code"] == "abc.unusable" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_never_executes_after_abc_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "yosys"
    marker = tmp_path / "executed"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _fake_abc(first / "yosys-abc")
    _fake_abc(second / "yosys-abc")
    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{first}:{original_path}")
    driver = YosysDriver(str(binary))
    monkeypatch.setenv("PATH", f"{second}:{original_path}")

    payload = driver.synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(item["code"] == "abc.unusable" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_never_executes_a_different_path_than_the_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_marker = tmp_path / "first-executed"
    second_marker = tmp_path / "second-executed"
    for directory, marker, version in (
        (first, first_marker, "Yosys 0.66"),
        (second, second_marker, "Yosys 0.67"),
    ):
        _write_executable(
            directory / "yosys",
            f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print({version!r})
    raise SystemExit(0)
pathlib.Path({str(marker)!r}).write_text('executed')
""",
        )
    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{first}:{original_path}")
    driver = YosysDriver(
        abc_binary_path=str(_fake_abc(tmp_path / "bound-yosys-abc"))
    )
    monkeypatch.setenv("PATH", f"{second}:{original_path}")

    payload = driver.synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert not first_marker.exists()
    assert not second_marker.exists()
    assert any(item["code"] == "tool.unusable" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_rejects_transitive_techmap_includes_before_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    techmap = tmp_path / "map.v"
    techmap.write_text('`include "uncaptured.vh"\nmodule map; endmodule\n')

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
        techmaps=[techmap],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any("self-contained" in item["message"] for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_rejects_transitive_liberty_includes_before_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
else:
    pathlib.Path({str(marker)!r}).write_text('executed')
""",
    )
    liberty = _liberty(tmp_path)
    liberty.write_text(
        'include_file("uncaptured.lib")\n'
        'library(test) { cell(AND2_X1) { area : 1.0; } }\n',
        encoding="utf-8",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        liberty,
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    assert any(
        "transitive-include-directive" in item["message"]
        for item in payload["diagnostics"]
    )
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_executable_mutation_makes_synthesis_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import os, pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
os.utime(__file__, None)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["tool_identity_stable"] is False
    assert any(item["code"] == "tool.changed" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_abc_executable_mutation_makes_synthesis_unknown(tmp_path: Path) -> None:
    abc = _fake_abc(tmp_path / "yosys-abc")
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
abc = pathlib.Path({str(abc)!r})
abc.write_text(abc.read_text() + '\\n')
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = YosysDriver(
        str(binary), abc_binary_path=str(abc)
    ).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert payload["data"]["abc_tool_identity_stable"] is False
    assert str(abc) in payload["data"]["changed_inputs"]
    assert any(item["code"] == "abc.changed" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_and_abc_use_one_closed_environment_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "environment-audit.jsonl"
    binary = tmp_path / "yosys"
    abc = tmp_path / "yosys-abc"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import json, os, pathlib, sys
audit = pathlib.Path({str(audit)!r})
with audit.open('a') as handle:
    handle.write(json.dumps({{'tool': 'yosys', 'environment': dict(os.environ)}}, sort_keys=True) + '\\n')
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )
    _write_executable(
        abc,
        f"""import json, os, pathlib, sys
with pathlib.Path({str(audit)!r}).open('a') as handle:
    handle.write(json.dumps({{'tool': 'abc', 'environment': dict(os.environ)}}, sort_keys=True) + '\\n')
if sys.argv[1:] == ['-c', 'version']:
    print('UC Berkeley, ABC 1.01 (compiled closed environment test)')
    raise SystemExit(0)
raise SystemExit(2)
""",
    )
    for key in (
        "YOSYS_DATDIR",
        "ABC_RC",
        "LD_PRELOAD",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PERL5LIB",
        "BASH_ENV",
        "ENV",
        "IFS",
        "CDPATH",
        "SHELLOPTS",
    ):
        monkeypatch.setenv(key, f"ambient-{key}")

    payload = YosysDriver(
        str(binary), abc_binary_path=str(abc)
    ).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    expected = payload["data"]["environment_policy"]["variables"]
    observed = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
    ]
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["environment_policy"] == {
        "id": "closed-yosys-abc-v1",
        "inherit_parent": False,
        "variables": expected,
    }
    assert [item["tool"] for item in observed] == ["yosys", "abc", "yosys"]
    assert all(item["environment"] == expected for item in observed)
    assert set(expected) == {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR"}
    _validate_data("logic.synthesize-v1alpha1.json", payload)


@pytest.mark.parametrize("target_ns", [0.0004, 0.0015, 2147483.648])
def test_yosys_rejects_abc_delays_that_are_not_safe_integer_picoseconds(
    tmp_path: Path,
    target_ns: float,
) -> None:
    binary = tmp_path / "yosys"
    _fake_yosys(binary)

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
        abc_delay_target_ns=target_ns,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any("whole picoseconds" in item["message"] for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_emits_the_exact_validated_abc_delay_in_picoseconds(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    _fake_yosys(binary)
    out = tmp_path / "out"

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        out,
        top="top",
        include_dirs=[tmp_path],
        abc_delay_target_ns=2.2,
    )

    assert payload["engineering"]["status"] == "pass"
    assert " -D 2200" in (out / "synthesize.ys").read_text(encoding="utf-8")
    _validate_data("logic.synthesize-v1alpha1.json", payload)


@pytest.mark.parametrize("json_problem", ["missing-top", "histogram-mismatch"])
def test_yosys_requires_requested_top_json_to_match_mapped_statistics(
    tmp_path: Path,
    json_problem: str,
) -> None:
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    structure = (
        _mapped_json(top="other")
        if json_problem == "missing-top"
        else _mapped_json(cell_type="OTHER_X1")
    )
    mapped_json = json.dumps(structure)
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["mapping_complete"] is False
    expected = (
        "missing-requested-top"
        if json_problem == "missing-top"
        else "mapped-stats-mismatch"
    )
    assert payload["data"]["mapped_json_validation"] == expected
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_rejects_invalid_num_memory_bits_as_unknown(tmp_path: Path) -> None:
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats(num_memory_bits=-1))
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('module top; AND2_X1 u(); endmodule\\n')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["stats"] is None
    assert payload["data"]["stats_validation"] == "invalid-count"
    _validate_data("logic.synthesize-v1alpha1.json", payload)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_yosys_never_captures_linked_native_outputs(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    link_action = (
        "pathlib.Path('mapped.v').symlink_to('native.v')"
        if unsafe_kind == "symlink"
        else "os.link('native.v', 'mapped.v')"
    )
    _write_executable(
        binary,
        f"""import os, pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('native.v').write_text('module top; AND2_X1 u(); endmodule\\n')
{link_action}
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert not any(
        item["role"] == "synthesis.netlist" for item in payload["artifacts"]
    )
    assert any(
        item["code"] == "artifact.unsafe_capture" for item in payload["diagnostics"]
    )
    _validate_data("logic.synthesize-v1alpha1.json", payload)


def test_yosys_empty_native_netlist_is_unknown_not_engineering_fail(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "yosys"
    stats = json.dumps(_stats())
    mapped_json = json.dumps(_mapped_json())
    _write_executable(
        binary,
        f"""import pathlib, sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
pathlib.Path('inference-stats.json').write_text({stats!r})
pathlib.Path('mapped-stats.json').write_text({stats!r})
pathlib.Path('mapped.v').write_text('')
pathlib.Path('mapped.json').write_text({mapped_json!r})
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert any(item["code"] == "artifact.invalid_netlist" for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)


@pytest.mark.parametrize(
    ("native_error", "expected_status", "expected_code"),
    [
        ("ERROR: Module `missing_top' not found!", "fail", "yosys.error"),
        (
            "error: 'missing_ibex_core' is not a valid top-level module",
            "fail",
            "yosys.error",
        ),
        (
            "ERROR: failed to create infrastructure scratch file",
            "unknown",
            "yosys.unclassified_error",
        ),
    ],
)
def test_yosys_only_whitelisted_native_errors_are_engineering_failures(
    tmp_path: Path,
    native_error: str,
    expected_status: str,
    expected_code: str,
) -> None:
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        f"""import sys
if '-V' in sys.argv or '--version' in sys.argv:
    print('Yosys 0.67')
    raise SystemExit(0)
print({native_error!r}, file=sys.stderr)
raise SystemExit(1)
""",
    )

    payload = _yosys_driver(binary, tmp_path).synthesize(
        [_rtl(tmp_path)],
        _liberty(tmp_path),
        tmp_path / "out",
        top="top",
        include_dirs=[tmp_path],
    )

    assert payload["engineering"]["status"] == expected_status
    assert any(item["code"] == expected_code for item in payload["diagnostics"])
    _validate_data("logic.synthesize-v1alpha1.json", payload)
