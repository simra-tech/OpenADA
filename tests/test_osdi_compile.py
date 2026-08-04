"""OSDI compile-and-preload path (Step 2): compile a reviewed Verilog-A block
to an OSDI module and prove the generated wrapper is a drop-in for the
ngspice-native backend, with correct physics and fail-closed refusals.

The compile+simulate tests need an OpenVAF compiler and an OSDI-capable ngspice;
they skip when either is absent (as the block-library native tests skip without
ngspice). The refusal tests are pure and always run.
"""

from __future__ import annotations

import hashlib
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


def test_compose_refuses_block_whose_veriloga_openvaf_cannot_compile():
    # sw_bbm_pair uses @(cross)/transition(). Classic openvaf rejects them at
    # compile; openvaf-r (the prod-image compiler) COMPILES them but ngspice
    # silently does not honor the event gating (measured: continuously
    # evaluated bodies). The source screen refuses them deterministically on
    # both compiler generations, before any compiler runs — so this test is
    # pure and needs no toolchain.
    lib = _load_bhv_core()
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compose_blocks_osdi(lib, ["sw_bbm_pair"], Path(tempfile.mkdtemp()))
    assert caught.value.code == "osdi.source.event_constructs"


def test_compile_refuses_event_constructs_even_in_expressions():
    # The screen catches transition()/@(cross)/absdelay outside comments;
    # commented mentions must NOT trigger it (comparator_clocked's header
    # names its feature profile in a comment).
    ok_commented = (
        '`include "disciplines.vams"\n'
        "// feature profile: event-cross, transition, hidden-state\n"
        "/* transition( absdelay( @(cross ... in a block comment */\n"
        "module bhv_pure_v1(p, n);\n"
        "  inout p, n; electrical p, n;\n"
        "  analog V(p, n) <+ 1.0;\n"
        "endmodule\n"
    )
    try:
        # compiles on a native host; on a toolchain-less host it fails later
        # (toolchain resolution) — either way the screen must not fire.
        oc.compile_verilog_a(ok_commented, "bhv_pure_v1", Path(tempfile.mkdtemp()))
    except oc.OsdiCompileError as exc:
        assert exc.code != "osdi.source.event_constructs"
    for construct in ("transition(vc, 1n, 1n)", "absdelay(V(p,n), 1n)"):
        src = (
            '`include "disciplines.vams"\n'
            "module bhv_evexpr_v1(p, n);\n"
            "  inout p, n; electrical p, n;\n"
            "  real vc;\n"
            f"  analog V(p, n) <+ {construct};\n"
            "endmodule\n"
        )
        with pytest.raises(oc.OsdiCompileError) as caught:
            oc.compile_verilog_a(src, "bhv_evexpr_v1", Path(tempfile.mkdtemp()))
        assert caught.value.code == "osdi.source.event_constructs"


@native
def test_compile_fails_closed_on_source_no_compiler_accepts():
    # Generation-independent fail-closed coverage: a syntactically broken
    # source must be refused by every OpenVAF vintage.
    broken = (
        '`include "disciplines.vams"\n'
        "module bhv_broken_v1(p);\n"
        "  this is not verilog;\n"
        "endmodule\n"
    )
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.compile_verilog_a(broken, "bhv_broken_v1", Path(tempfile.mkdtemp()))
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


# --- validate_osdi_preload: the structural exemption gate (pure, no toolchain) ---

_GOOD_WRAPPER = (
    ".subckt bhv_opamp_1p_v1 inp inn out vss\n"
    ".model bhv_opamp_1p_v1__osdi bhv_opamp_1p_v1\n"
    "N1 inp inn out vss bhv_opamp_1p_v1__osdi\n"
    ".ends bhv_opamp_1p_v1\n"
)


def _preload(control_body: str, wrapper: str = _GOOD_WRAPPER) -> str:
    return ".control\n" + control_body + ".endc\n" + wrapper


def test_validate_preload_accepts_a_pre_osdi_only_control_block(tmp_path):
    osdi = tmp_path / "bhv_opamp_1p_v1.osdi"
    osdi.write_bytes(b"fake-osdi-bytes")
    text = _preload(f"pre_osdi {osdi}\n")
    verified = oc.validate_osdi_preload(text)
    assert [p for p, _ in verified.modules] == [str(osdi)]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for _, h in verified.modules)


@pytest.mark.parametrize(
    "control_body",
    [
        "pre_osdi /x.osdi\nshell rm -rf /\n",     # arbitrary shell in .control
        "pre_osdi /x.osdi\nsystem echo pwned\n",  # arbitrary system command
        "pre_osdi /x.osdi\nsource /etc/passwd\n", # arbitrary source
        "pre_osdi /x.osdi\nrun\n",                # a stray analysis run
        "op\n",                                    # no pre_osdi at all, bare cmd
        "pre_osdi /x.osdi\n+ shell id\n",         # `+` continuation smuggling
        "pre_osdi /x.osdi ; shell id\n",          # inline `;` command after path
        "pre_osdi /x.osdi shell id\n",            # extra tokens after the path
        "PRE_OSDI /x.osdi\nSHELL id\n",           # case-variant keywords
        "echo hi\npre_osdi /x.osdi\n",            # command before the load
    ],
)
def test_validate_preload_refuses_arbitrary_control_commands(control_body):
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.validate_osdi_preload(_preload(control_body))
    assert caught.value.code in {"osdi.preload.unsafe_control", "osdi.preload.empty"}


def test_validate_preload_refuses_a_second_control_block_below_endc():
    text = _preload("pre_osdi /x.osdi\n", _GOOD_WRAPPER + ".control\nshell id\n.endc\n")
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.validate_osdi_preload(text)
    assert caught.value.code == "osdi.preload.unsafe_control"


def test_validate_preload_refuses_a_control_char_in_the_osdi_path():
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.validate_osdi_preload(_preload("pre_osdi /a\x01b.osdi\n"))
    assert caught.value.code == "osdi.preload.unsafe_path"


def test_validate_preload_refuses_a_nonsubckt_card_in_the_wrapper_section():
    text = _preload("pre_osdi /x.osdi\n", "vinp inp 0 1.0\n")
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.validate_osdi_preload(text)
    assert caught.value.code == "osdi.preload.unsafe_control"


def test_validate_preload_refuses_a_missing_module(tmp_path):
    text = _preload(f"pre_osdi {tmp_path / 'gone.osdi'}\n")
    with pytest.raises(oc.OsdiCompileError) as caught:
        oc.validate_osdi_preload(text)
    assert caught.value.code == "osdi.preload.missing_module"


def test_validate_preload_binds_each_module_to_its_reviewed_digest(tmp_path):
    osdi = tmp_path / "m.osdi"
    osdi.write_bytes(b"real-bytes")
    good = hashlib.sha256(b"real-bytes").hexdigest()
    text = _preload(f"pre_osdi {osdi}\n")
    # Correct digest -> accepted.
    oc.validate_osdi_preload(text, expected_osdi_sha256={str(osdi): good})
    # A module without a reviewed digest in the map -> refused (unbound).
    with pytest.raises(oc.OsdiCompileError) as unbound:
        oc.validate_osdi_preload(text, expected_osdi_sha256={})
    assert unbound.value.code == "osdi.preload.unbound_module"
    # A swapped module (digest mismatch) -> tampered.
    osdi.write_bytes(b"swapped-bytes")
    with pytest.raises(oc.OsdiCompileError) as tampered:
        oc.validate_osdi_preload(text, expected_osdi_sha256={str(osdi): good})
    assert tampered.value.code == "blocks.materialize.tampered"


# --- operation-level end-to-end: `simulate --blocks --osdi` through the
#     sanctioned control-mode runner (gates 4-5). The physics value is proven
#     above at the module level with the identical prelude; this proves the prod
#     simulate OPERATION drives the control-mode run and returns valid evidence.

def _opamp_op_deck(tmp_path: Path) -> Path:
    # Control-free caller deck: the profile gate inspects THIS, while the run
    # deck (title + reviewed pre_osdi preload + this) executes in control mode.
    deck = tmp_path / "opamp_op.cir"
    deck.write_text(
        "* opamp OSDI op-point testbench\n"
        "X1 inp inn out 0 bhv_opamp_1p_v1\n"
        "vinp inp 0 0.01\n"
        "vinn inn 0 0\n"
        "rl out 0 1meg\n"
        ".op\n"
        ".end\n",
        encoding="utf-8",
    )
    return deck


@native
def test_simulate_blocks_osdi_operation_runs_control_mode_and_passes(tmp_path, capsys):
    import json
    from openada.cli import main

    deck = _opamp_op_deck(tmp_path)
    out = tmp_path / "evidence"
    exit_code = main(
        [
            "simulate",
            str(deck),
            "--blocks",
            "bhv-core:opamp_1p",
            "--osdi",
            "--analysis",
            "op",
            "--output-dir",
            str(out),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    # The control-mode runner produced a valid op result end to end.
    assert exit_code == 0, payload
    assert payload["operation"] == "simulate"
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"

    data = payload["data"]
    assert data["analysis"]["type"] == "op"
    assert data["analysis"]["point_count"] == 1
    assert data["analysis"]["finite_value_count"] >= 1
    # Request binding is exact only when the run deck is content-bound and the
    # retained op raw structurally matches the requested analysis.
    assert data["evidence"]["request_binding"] == "exact"
    assert data["evidence"]["structure"] == "valid"
    assert "simulation.result" in data["evidence"]["artifact_roles_present"]

    # Provenance states the OSDI preload + managed-startup story, not the
    # default model-free note.
    limitations = " ".join(data["evidence"]["provenance_limitations"])
    assert "pre_osdi" in limitations
    assert ".spiceinit" in limitations

    # The digest-bound OSDI provenance rode through into the retained envelope.
    osdi = data["extensions"]["org.openada.behavioral-blocks-osdi"]
    assert osdi["library"] == "bhv-core"
    assert osdi["requested"] == ["opamp_1p"]
    assert [m["module"] for m in osdi["modules"]] == ["bhv_opamp_1p_v1"]
    assert all(len(m["osdi_sha256"]) == 64 for m in osdi["modules"])


def _publish_opamp_artifact(
    directory: Path,
    *,
    deck_body: str,
    analyses: list[dict[str, object]],
) -> Path:
    """Write a minimal-but-loadable Simra testbench artifact whose netlist
    instantiates the behavioral opamp block.

    `load_simra_testbench` reads the descriptor + the netlist + the typed view's
    `testbench.analyses`/`save`; it does not require the full compiler view, so a
    compact synthetic view is sufficient to exercise the artifact -> OSDI path.
    The published netlist references `bhv_opamp_1p_v1`, which only the reviewed
    OSDI preload defines -- exactly the artifact that needs behavioral-block
    collateral to run.
    """
    import json

    directory.mkdir(parents=True, exist_ok=True)
    netlist = directory / "design.spice"
    netlist.write_text(deck_body, encoding="utf-8")
    view = directory / "schematic.simra.json"
    view.write_text(
        json.dumps(
            {
                "schema": "simra.schematic/v2",
                "testbench": {"analyses": analyses, "save": []},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    handoff = "direct" if len(analyses) == 1 else "split_required"
    descriptor = directory / "schematic.artifact.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema": "simra.schematic-artifact/v2",
                "kind": "testbench",
                "id": "opamp-osdi-artifact-e2e",
                "label": "opamp OSDI artifact e2e",
                "top": "OPAMP_OSDI_TB",
                "netlist": "design.spice",
                "view": "schematic.simra.json",
                "netlistable": True,
                "hashes": {
                    "netlist_sha256": _digest(netlist),
                    "view_sha256": _digest(view),
                },
                "validation": {
                    "netlistable": True,
                    "parameters": "resolved",
                    # The behavioral block is external collateral, so the
                    # published deck is not self-contained -- the OSDI preload
                    # supplies what the artifact declares it needs.
                    "simulation_ready": False,
                    "simulation_handoff": handoff,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return descriptor


@native
def test_simulate_blocks_osdi_artifact_op_runs_and_passes(tmp_path, capsys):
    # The artifact-target counterpart of the bare-deck OSDI op test: a published
    # testbench whose netlist instantiates the block runs the SAME control-mode
    # OSDI launch, because `derive_single_analysis_decks` now emits a control-free
    # per-analysis collateral deck for the profile gate to inspect.
    import json
    from openada.cli import main

    descriptor = _publish_opamp_artifact(
        tmp_path / "art",
        deck_body=(
            "* opamp OSDI op-point testbench (published artifact)\n"
            "X1 inp inn out 0 bhv_opamp_1p_v1\n"
            "vinp inp 0 0.01\n"
            "vinn inn 0 0\n"
            "rl out 0 1meg\n"
            ".op\n"
            ".end\n"
        ),
        analyses=[{"kind": "op"}],
    )
    out = tmp_path / "evidence"
    exit_code = main(
        [
            "simulate",
            str(descriptor),
            "--blocks",
            "bhv-core:opamp_1p",
            "--osdi",
            "--output-dir",
            str(out),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"

    data = payload["data"]
    assert data["analysis"]["type"] == "op"
    assert data["analysis"]["finite_value_count"] >= 1
    assert data["evidence"]["structure"] == "valid"
    # The OSDI preload story rode through -- the artifact ran the composed deck,
    # not a model-free one.
    limitations = " ".join(data["evidence"]["provenance_limitations"])
    assert "pre_osdi" in limitations
    osdi = data["extensions"]["org.openada.behavioral-blocks-osdi"]
    assert [m["module"] for m in osdi["modules"]] == ["bhv_opamp_1p_v1"]


@native
def test_simulate_blocks_osdi_artifact_multi_analysis_runs_and_passes(tmp_path, capsys):
    # A split (multi-analysis) artifact: each declared analysis is derived into
    # its own single-analysis deck, the OSDI preload is composed into each, and
    # every one runs through the sanctioned control-mode launch.
    import json
    from openada.cli import main

    descriptor = _publish_opamp_artifact(
        tmp_path / "art",
        deck_body=(
            "* opamp OSDI multi-analysis testbench (published artifact)\n"
            "X1 inp inn out 0 bhv_opamp_1p_v1\n"
            "vinp inp 0 dc 0.01 ac 1\n"
            "vinn inn 0 0\n"
            "rl out 0 1meg\n"
            ".op\n"
            ".ac dec 5 1 1meg\n"
            ".end\n"
        ),
        analyses=[{"kind": "op"}, {"kind": "ac"}],
    )
    out = tmp_path / "evidence"
    exit_code = main(
        [
            "simulate",
            str(descriptor),
            "--blocks",
            "bhv-core:opamp_1p",
            "--osdi",
            "--output-dir",
            str(out),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    # Both analyses were derived, dispatched as their own OSDI control-mode runs,
    # and passed -- the split path composed the preload into each per-analysis
    # deck.
    dispatch = payload["data"]["extensions"]["org.openada.simulation-dispatch"]
    assert dispatch["mode"] == "split"
    assert dispatch["declared_analysis_count"] == 2
    assert dispatch["completed_analysis_count"] == 2
    assert dispatch["passing_analysis_count"] == 2
    # The per-analysis children were retained as their own result artifacts.
    child_roles = [
        a.get("role")
        for a in payload.get("artifacts", [])
        if a.get("role") == "simulation.analysis-result"
    ]
    assert len(child_roles) == 2, payload["artifacts"]


def test_simulate_osdi_preload_digest_mismatch_refuses_before_launch(tmp_path):
    """The operation pins the OSDI preload to its reviewed composition digest: a
    preload that does not hash to the declared sha256 is a pre-launch
    blocks.materialize.tampered refusal, so no simulator runs. Needs no native
    tools — the guard fires before any ngspice launch."""
    from openada.discovery import DiscoveryManager
    from openada.operations.simulate import simulate

    deck = _opamp_op_deck(tmp_path)
    preload = (
        ".control\npre_osdi /nonexistent/bhv_opamp_1p_v1.osdi\n.endc\n"
        ".subckt bhv_opamp_1p_v1 inp inn out vss\n"
        ".model bhv_opamp_1p_v1__osdi bhv_opamp_1p_v1\n"
        "N1 inp inn out vss bhv_opamp_1p_v1__osdi\n"
        ".ends bhv_opamp_1p_v1\n"
    )
    payload = simulate(
        deck,
        tmp_path / "out",
        discovery=DiscoveryManager(),
        backend="ngspice",
        parameters={"analysis": {"type": "op", "extensions": {}}, "extensions": {}},
        osdi_preload_text=preload,
        osdi_preload_sha256="0" * 64,  # deliberately not the preload's digest
    )
    codes = [d.get("code") for d in payload.get("diagnostics", [])]
    assert "blocks.materialize.tampered" in codes, payload
    assert payload["engineering"]["status"] != "pass"
