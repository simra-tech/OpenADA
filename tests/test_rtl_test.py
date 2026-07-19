from __future__ import annotations

from pathlib import Path
import json

from jsonschema import Draft202012Validator
from openada.discovery import DiscoveryManager
from openada.engines.rtl_test import RTLTestDriver


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _driver(
    tmp_path: Path, *, compile_exit: int = 0, run_exit: int = 0, run_sleep: float = 0
) -> RTLTestDriver:
    iverilog = tmp_path / "iverilog"
    vvp = tmp_path / "vvp"
    _executable(
        iverilog,
        f"""import pathlib, sys
if '-V' in sys.argv:
    print('Icarus Verilog version 13.0')
else:
    if {compile_exit} == 0:
        pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('compiled')
    raise SystemExit({compile_exit})
""",
    )
    _executable(
        vvp,
        f"""import sys, time
if '-V' in sys.argv:
    print('Icarus Verilog runtime version 13.0')
else:
    time.sleep({run_sleep})
    raise SystemExit({run_exit})
""",
    )
    discovery = DiscoveryManager(binary_overrides={"iverilog": iverilog, "vvp": vvp})
    return RTLTestDriver(discovery=discovery)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "tb.sv"
    path.write_text("module tb; initial begin $finish; end endmodule\n", encoding="utf-8")
    return path


def test_iverilog_self_checking_test_pass(tmp_path: Path) -> None:
    payload = _driver(tmp_path).rtl_test([_source(tmp_path)], tmp_path / "out", top="tb")

    assert payload["engineering"]["status"] == "pass"
    assert [stage["exit_code"] for stage in payload["data"]["stages"]] == [0, 0]
    assert payload["data"]["pass_policy"] == "self-checking-exit-zero"
    assert {item["role"] for item in payload["artifacts"]} == {
        "rtl.test.compile.log", "rtl.test.run.log", "rtl.test.executable"
    }
    profile = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles/rtl.test-v1alpha1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(profile["normalized_result"]["data_schema"]).validate(payload["data"])


def test_compile_error_is_engineering_fail(tmp_path: Path) -> None:
    payload = _driver(tmp_path, compile_exit=2).rtl_test(
        [_source(tmp_path)], tmp_path / "out", top="tb"
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "fail"
    assert len(payload["data"]["stages"]) == 1


def test_nonzero_self_checking_test_is_engineering_fail(tmp_path: Path) -> None:
    payload = _driver(tmp_path, run_exit=1).rtl_test(
        [_source(tmp_path)], tmp_path / "out", top="tb"
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["stages"][1]["exit_code"] == 1


def test_stale_transcript_is_rejected_without_execution(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "rtl-test.compile.log").write_text("stale", encoding="utf-8")

    payload = _driver(tmp_path).rtl_test([_source(tmp_path)], out, top="tb")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"


def test_runtime_timeout_is_unknown_not_fail(tmp_path: Path) -> None:
    payload = _driver(tmp_path, run_sleep=1).rtl_test(
        [_source(tmp_path)], tmp_path / "out", top="tb", timeout=0.02
    )

    assert payload["execution"]["status"] == "timed_out"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "evidence.incomplete"


def test_verilator_binary_mapping_passes(tmp_path: Path) -> None:
    verilator = tmp_path / "verilator"
    _executable(
        verilator,
        """import os, pathlib, sys
if '--version' in sys.argv:
    print('Verilator 5.050')
else:
    build = pathlib.Path(sys.argv[sys.argv.index('--Mdir') + 1])
    output = build / sys.argv[sys.argv.index('-o') + 1]
    output.write_text('#!/usr/bin/python3\\nprint("self-test passed")\\n')
    output.chmod(0o755)
""",
    )
    discovery = DiscoveryManager(binary_overrides={"verilator": verilator})

    payload = RTLTestDriver(discovery=discovery).rtl_test(
        [_source(tmp_path)], tmp_path / "out", top="tb", backend="verilator"
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["backend"] == "verilator"
    assert payload["data"]["runtime_tool"] is None
