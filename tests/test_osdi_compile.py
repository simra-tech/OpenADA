"""OSDI compile-and-preload path (Step 2): compile a reviewed Verilog-A block
to an OSDI module and prove the generated wrapper is a drop-in for the
ngspice-native backend, with correct physics and fail-closed refusals.

The compile+simulate tests need an OpenVAF compiler and an OSDI-capable ngspice;
they skip when either is absent (as the block-library native tests skip without
ngspice). The refusal tests are pure and always run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openada import osdi_compile as oc  # noqa: E402

OPENVAF = shutil.which("openvaf-r") or shutil.which("openvaf")
NGSPICE = shutil.which("ngspice")
OPAMP_VA = ROOT / "blocks/bhv-core/blocks/opamp_1p/opamp_1p.va"
OPAMP_PORTS = ["inp", "inn", "out", "vss"]
OPAMP_DEFAULTS = {
    "av0": 100000.0,
    "gbw": 10000000.0,
    "slew": 10000000.0,
    "vhi": 1.65,
    "vlo": -1.65,
    "rout": 100.0,
}

native = pytest.mark.skipif(
    OPENVAF is None or NGSPICE is None,
    reason="OSDI path needs an OpenVAF compiler and an OSDI-capable ngspice",
)


def _run_opamp(parameters, vinp):
    module = oc.compile_verilog_a(
        OPAMP_VA.read_text(), "bhv_opamp_1p_v1", Path(tempfile.mkdtemp())
    )
    prelude = oc.osdi_preload_prelude([(module, OPAMP_PORTS, parameters)])
    deck = (
        "* osdi wrapper drop-in\n"
        + prelude
        + "X1 inp inn out 0 bhv_opamp_1p_v1\n"
        + f"vinp inp 0 {vinp}\n"
        + "vinn inn 0 0\n"
        + "rl out 0 1meg\n"
        + ".control\nop\nprint v(out)\n.endc\n.end\n"
    )
    path = Path(tempfile.mktemp(suffix=".cir"))
    path.write_text(deck)
    completed = subprocess.run(
        [NGSPICE, "-b", str(path)], capture_output=True, text=True, timeout=60
    )
    match = re.search(r"v\(out\)\s*=\s*([-\d.eE+]+)", completed.stdout + completed.stderr)
    return float(match.group(1)) if match else None


@native
def test_compiled_osdi_opamp_matches_expected_physics():
    # Saturation: av0*10mV = 1000 >> vhi, so the output clamps at the rail.
    assert abs(_run_opamp(OPAMP_DEFAULTS, 0.01) - 1.6498) < 0.01
    # Linear region: 1uV * av0(100k) = 0.1 V, well below the rail.
    assert abs(_run_opamp(OPAMP_DEFAULTS, 1e-6) - 0.1) < 0.005


@native
def test_wrapper_parameter_override_reaches_the_osdi_model():
    # Lowering the rail parameter must change the saturated output — proving the
    # subckt header forwards the override onto the .model line.
    assert abs(_run_opamp({**OPAMP_DEFAULTS, "vhi": 1.0}, 0.01) - 1.0) < 0.01


@native
def test_compile_records_digest_provenance():
    module = oc.compile_verilog_a(
        OPAMP_VA.read_text(), "bhv_opamp_1p_v1", Path(tempfile.mkdtemp())
    )
    assert module.module_name == "bhv_opamp_1p_v1"
    assert re.fullmatch(r"[0-9a-f]{64}", module.source_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", module.osdi_sha256)
    assert module.compiler in ("openvaf-r", "openvaf")
    assert module.compiler_version and module.compiler_version != "unknown"
    assert module.osdi_path.is_file()


@pytest.mark.parametrize(
    "module_name",
    ["not_a_wrapper", "bhv_x", "opamp_1p", "bhv_opamp_1p", "bhv_opamp_1p_v1; rm"],
)
def test_compile_refuses_non_wrapper_module_names(module_name):
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compile_verilog_a("module x; endmodule", module_name, Path(tempfile.mkdtemp()))
    assert caught.value.code == "osdi.module.invalid"


def test_resolve_openvaf_refuses_when_absent():
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.resolve_openvaf(override="/nonexistent/openvaf-binary")
    assert caught.value.code == "osdi.compiler.unavailable"


@pytest.mark.skipif(OPENVAF is None, reason="needs a compiler to build a module to preload")
@pytest.mark.parametrize(
    "parameters, ports, code",
    [
        ({"av0": "1e5; rm -rf /"}, OPAMP_PORTS, "osdi.parameter.invalid"),
        ({"av0": "{expr}"}, OPAMP_PORTS, "osdi.parameter.invalid"),
        ({"a b": "1"}, OPAMP_PORTS, "osdi.parameter.invalid"),
        ({"av0": True}, OPAMP_PORTS, "osdi.parameter.invalid"),
        ({}, ["in p"], "osdi.interface.invalid"),
        ({}, ["inp", "inp"], "osdi.interface.invalid"),
        ({}, [], "osdi.interface.invalid"),
    ],
)
def test_preload_refuses_unsafe_ports_and_parameters(parameters, ports, code):
    module = oc.compile_verilog_a(
        OPAMP_VA.read_text(), "bhv_opamp_1p_v1", Path(tempfile.mkdtemp())
    )
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.osdi_preload_prelude([(module, ports, parameters)])
    assert caught.value.code == code


def test_preload_refuses_empty_module_set():
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.osdi_preload_prelude([])
    assert caught.value.code == "osdi.preload.empty"


@pytest.mark.parametrize("bad_dir", ["/tmp/has space", "/tmp/has\nnewline", "/tmp/has\ttab"])
def test_compile_refuses_workdir_that_would_break_the_pre_osdi_line(bad_dir):
    # The compiled path is emitted onto a bare `pre_osdi <path>` line; a work_dir
    # with whitespace/newline would split that command, so it is refused up front.
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compile_verilog_a("module x; endmodule", "bhv_x_v1", Path(bad_dir))
    assert caught.value.code == "osdi.workdir.unsafe"


def test_preload_refuses_module_with_unsafe_osdi_path():
    unsafe = oc.OsdiModule(
        module_name="bhv_x_v1",
        osdi_path=Path("/tmp/evil path/mod.osdi"),
        source_sha256="0" * 64,
        osdi_sha256="0" * 64,
        compiler="openvaf",
        compiler_version="test",
    )
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.osdi_preload_prelude([(unsafe, ["a", "b"], {})])
    assert caught.value.code == "osdi.preload.unsafe_path"


def test_compile_refuses_non_text_source():
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compile_verilog_a(b"module x; endmodule", "bhv_x_v1", Path(tempfile.mkdtemp()))
    assert caught.value.code == "osdi.source.invalid"


# --- library -> OSDI composition (the OSDI analog of compose_blocks) ---

def _load_bhv_core():
    from openada.block_library import load_block_library
    return load_block_library("bhv-core")


@native
def test_compose_opamp_block_to_osdi_matches_native_physics():
    lib = _load_bhv_core()
    comp = oc.compose_blocks_osdi(lib, ["opamp_1p"], Path(tempfile.mkdtemp()))
    assert comp.library_id == "bhv-core"
    assert [m.module_name for m in comp.modules] == ["bhv_opamp_1p_v1"]
    deck = (
        "* composed\n" + comp.prelude_text
        + "X1 inp inn out 0 bhv_opamp_1p_v1\n"
        + "vinp inp 0 0.01\nvinn inn 0 0\nrl out 0 1meg\n"
        + ".control\nop\nprint v(out)\n.endc\n.end\n"
    )
    p = Path(tempfile.mktemp(suffix=".cir")); p.write_text(deck)
    r = subprocess.run([NGSPICE, "-b", str(p)], capture_output=True, text=True, timeout=60)
    m = re.search(r"v\(out\)\s*=\s*([-\d.eE+]+)", r.stdout + r.stderr)
    assert m and abs(float(m.group(1)) - 1.6498) < 0.01


@native
def test_compose_refuses_block_whose_veriloga_openvaf_cannot_compile():
    # sw_bbm_pair uses transition() (unsupported by OpenVAF) — fail closed.
    lib = _load_bhv_core()
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compose_blocks_osdi(lib, ["sw_bbm_pair"], Path(tempfile.mkdtemp()))
    assert caught.value.code == "osdi.compile.failed"


@pytest.mark.parametrize(
    "block_ids, code",
    [
        ([], "osdi.compose.empty"),
        (["no_such_block"], "osdi.compose.unknown"),
        (["opamp_1p", "opamp_1p"], "osdi.compose.duplicate"),
    ],
)
def test_compose_input_refusals(block_ids, code):
    lib = _load_bhv_core()
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compose_blocks_osdi(lib, block_ids, Path(tempfile.mkdtemp()))
    assert caught.value.code == code
