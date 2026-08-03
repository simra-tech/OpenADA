"""Mixed-signal cosim path (Step 3): compile a reviewed digital Verilog core
to a d_cosim shared object and prove the composition and its gates are
fail-closed.

The compile round-trip tests need Verilator, g++, and an ngspice installation
that ships the d_cosim shim sources; they skip when any is absent (as the
OSDI tests skip without OpenVAF). The refusal tests are pure and always run.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openada import cosim_compile as cc  # noqa: E402
from openada.engines.simra_artifact import (  # noqa: E402
    SimraArtifactError,
    load_model_prelude,
)

NGSPICE = shutil.which("ngspice")
VERILATOR = shutil.which("verilator_bin") or shutil.which("verilator")
GXX = shutil.which("g++")


def _shim_available() -> bool:
    if NGSPICE is None:
        return False
    try:
        cc.resolve_cosim_shim_dir(ngspice_bin=NGSPICE)
    except cc.CosimCompileError:
        return False
    return True


toolchain = pytest.mark.skipif(
    NGSPICE is None or VERILATOR is None or GXX is None or not _shim_available(),
    reason="cosim path needs ngspice (with shim sources), Verilator, and g++",
)

CORE_V = ROOT / "blocks/bhv-core/blocks/comparator_clocked/comparator_clocked.cosim.v"
CORE_MODULE = "bhv_comparator_clocked_v1_core"
CORE_INPUTS = ["clk", "din"]
CORE_OUTPUTS = ["q"]


def _refuses(code):
    return pytest.raises(cc.CosimCompileError, match=None)


# ---------------------------------------------------------------------------
# Pure refusal tests (no toolchain required)
# ---------------------------------------------------------------------------


def test_module_name_outside_grammar_is_refused(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            "module x; endmodule", "not_a_core", ["a"], ["b"], tmp_path
        )
    assert caught.value.code == "cosim.module.invalid"


def test_non_text_source_is_refused(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            b"module", "bhv_x_v1_core", ["a"], ["b"], tmp_path
        )
    assert caught.value.code == "cosim.source.invalid"


def test_oversize_source_is_refused(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            "/" * (cc.MAX_CORE_BYTES + 1), "bhv_x_v1_core", ["a"], ["b"], tmp_path
        )
    assert caught.value.code == "cosim.source.oversize"


def test_workdir_outside_path_alphabet_is_refused(tmp_path):
    hostile = tmp_path / 'evil " dir'
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            "module m; endmodule", "bhv_x_v1_core", ["a"], ["b"], hostile
        )
    assert caught.value.code == "cosim.workdir.unsafe"


@pytest.mark.parametrize(
    "ports",
    [[], ["UPPER"], ["ok", "ok"], ["9lead"], ["sp ace"]],
)
def test_declared_port_lists_are_validated(tmp_path, ports):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            "module m; endmodule", "bhv_x_v1_core", ports, ["q"], tmp_path
        )
    assert caught.value.code == "cosim.interface.invalid"


def _module(so_path: Path) -> cc.CosimModule:
    return cc.CosimModule(
        core_module=CORE_MODULE,
        so_path=so_path,
        source_sha256="0" * 64,
        so_sha256="0" * 64,
        verilator="verilator",
        verilator_version="test",
        shim_sha256="0" * 64,
        inputs=("clk", "din"),
        outputs=("q",),
    )


def test_model_line_refuses_bad_model_name():
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.cosim_model_line(_module(Path("/tmp/x.so")), "evil")
    assert caught.value.code == "cosim.model.invalid"


@pytest.mark.parametrize(
    "path",
    [
        Path('/tmp/evil".so'),
        Path("/tmp/evil so/x.so"),
        Path("relative/x.so"),
        Path("/tmp/evil;rm/x.so"),
        Path("/tmp/evil(paren/x.so"),
    ],
)
def test_model_line_refuses_unsafe_object_paths(path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.cosim_model_line(_module(path), "bhv_x_cosim")
    assert caught.value.code in ("cosim.model.unsafe_path",)


def test_model_line_shape():
    line = cc.cosim_model_line(_module(Path("/tmp/core.so")), "bhv_x_cosim")
    assert line == (
        '.model bhv_x_cosim d_cosim(simulation="/tmp/core.so" delay=1p)'
    )


def test_parse_verilated_ports_refuses_inout(tmp_path):
    header = tmp_path / "Vlng.h"
    header.write_text("    VL_INOUT8(&pad,0,0);\n    VL_OUT8(&q,0,0);\n")
    with pytest.raises(cc.CosimCompileError) as caught:
        cc._parse_verilated_ports(header)
    assert caught.value.code == "cosim.interface.unsupported"


def test_parse_verilated_ports_refuses_buses(tmp_path):
    header = tmp_path / "Vlng.h"
    header.write_text("    VL_IN8(&clk,0,0);\n    VL_OUT8(&count,3,0);\n")
    with pytest.raises(cc.CosimCompileError) as caught:
        cc._parse_verilated_ports(header)
    assert caught.value.code == "cosim.interface.unsupported"


def test_parse_verilated_ports_returns_header_order(tmp_path):
    header = tmp_path / "Vlng.h"
    header.write_text(
        "    VL_IN8(&clk,0,0);\n    VL_IN8(&din,0,0);\n    VL_OUT8(&q,0,0);\n"
    )
    assert cc._parse_verilated_ports(header) == (("clk", "din"), ("q",))


class _FakeLibrary:
    library_id = "fake"
    library_version = "0.0.0"
    library_digest = "d" * 64

    def __init__(self, blocks):
        self.blocks = blocks


class _FakeBlock:
    def __init__(self, cosim=None, depends=()):
        self.cosim = cosim
        self.depends = tuple(depends)


def test_compose_refuses_empty_selection(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compose_blocks_cosim(_FakeLibrary({}), (), tmp_path)
    assert caught.value.code == "cosim.compose.empty"


def test_compose_refuses_unknown_block(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compose_blocks_cosim(_FakeLibrary({}), ("missing",), tmp_path)
    assert caught.value.code == "cosim.compose.unknown"


def test_compose_refuses_duplicate_request(tmp_path):
    library = _FakeLibrary({"a": _FakeBlock()})
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compose_blocks_cosim(library, ("a", "a"), tmp_path)
    assert caught.value.code == "cosim.compose.duplicate"


def test_compose_refuses_block_without_backend(tmp_path):
    library = _FakeLibrary({"a": _FakeBlock(cosim=None)})
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compose_blocks_cosim(library, ("a",), tmp_path)
    assert caught.value.code == "cosim.compose.no_backend"


def test_compose_refuses_dependencies(tmp_path):
    library = _FakeLibrary({"a": _FakeBlock(cosim=object(), depends=("b",))})
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compose_blocks_cosim(library, ("a",), tmp_path)
    assert caught.value.code == "cosim.compose.depends_unsupported"


# ---------------------------------------------------------------------------
# The executable-model gate on the model-library role
# ---------------------------------------------------------------------------


def _models_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.spice"
    path.write_text(text, encoding="utf-8")
    return path


def test_models_role_refuses_executable_model(tmp_path):
    path = _models_file(
        tmp_path, '.model evil d_cosim(simulation="/tmp/evil.so" delay=1p)\n'
    )
    with pytest.raises(SimraArtifactError) as caught:
        load_model_prelude(path)
    assert caught.value.code == "configuration.models.executable_model"


def test_models_role_refuses_continuation_split_card(tmp_path):
    path = _models_file(tmp_path, '.model evil\n+ d_process(process="/bin/sh")\n')
    with pytest.raises(SimraArtifactError) as caught:
        load_model_prelude(path)
    assert caught.value.code == "configuration.models.executable_model"


def test_models_role_accepts_only_the_named_permitted_card(tmp_path):
    text = '.model bhv_x_cosim d_cosim(simulation="/tmp/x.so" delay=1p)\n'
    path = _models_file(tmp_path, text)
    prelude, _record = load_model_prelude(
        path, permitted_executable_models=frozenset({"bhv_x_cosim"})
    )
    assert prelude == text
    with pytest.raises(SimraArtifactError) as caught:
        load_model_prelude(
            path, permitted_executable_models=frozenset({"bhv_other_cosim"})
        )
    assert caught.value.code == "configuration.models.executable_model"


def test_models_role_still_accepts_plain_cards(tmp_path):
    path = _models_file(tmp_path, ".model nfet nmos(level=1)\n")
    prelude, _record = load_model_prelude(path)
    assert "nfet" in prelude


# ---------------------------------------------------------------------------
# Compile round-trip (real toolchain)
# ---------------------------------------------------------------------------


@toolchain
def test_compile_round_trip_binds_digests_and_interface(tmp_path):
    module = cc.compile_verilog_digital(
        CORE_V.read_text(),
        CORE_MODULE,
        CORE_INPUTS,
        CORE_OUTPUTS,
        tmp_path,
    )
    assert module.core_module == CORE_MODULE
    assert module.so_path.is_file()
    assert module.inputs == ("clk", "din")
    assert module.outputs == ("q",)
    assert len(module.source_sha256) == 64
    assert len(module.so_sha256) == 64
    assert len(module.shim_sha256) == 64
    assert module.verilator_version.lower().startswith("verilator")
    line = cc.cosim_model_line(module, "bhv_comparator_clocked_cosim")
    assert str(module.so_path) in line


@toolchain
def test_compile_refuses_declared_order_mismatch(tmp_path):
    with pytest.raises(cc.CosimCompileError) as caught:
        cc.compile_verilog_digital(
            CORE_V.read_text(),
            CORE_MODULE,
            ["din", "clk"],  # swapped against the Verilated header order
            CORE_OUTPUTS,
            tmp_path,
        )
    assert caught.value.code == "cosim.interface.mismatch"


@toolchain
def test_compose_real_library_generates_binding_cards(tmp_path):
    from openada.block_library import load_block_library

    library = load_block_library("bhv-core")
    composition = cc.compose_blocks_cosim(
        library, ("comparator_clocked",), tmp_path
    )
    assert composition.model_names == ("bhv_comparator_clocked_cosim",)
    assert ".model bhv_comparator_clocked_cosim d_cosim(" in composition.text
    assert ".control" not in composition.text
    record = composition.record()
    assert record["kind"] == "behavioral-block-cosim-composition"
    assert record["generated_models"] == ["bhv_comparator_clocked_cosim"]
    core = record["cores"][0]
    assert core["source_sha256"] == composition.modules[0].source_sha256
    assert core["object_sha256"] == composition.modules[0].so_sha256


@toolchain
def test_verify_objects_refuses_swapped_artifact(tmp_path):
    from openada.block_library import load_block_library

    library = load_block_library("bhv-core")
    composition = cc.compose_blocks_cosim(
        library, ("comparator_clocked",), tmp_path
    )
    composition.verify_objects()  # pristine object passes
    module = composition.modules[0]
    module.so_path.write_bytes(b"not the compiled object")
    with pytest.raises(cc.CosimCompileError) as caught:
        composition.verify_objects()
    assert caught.value.code == "cosim.materialize.tampered"


def test_permitted_name_does_not_admit_a_different_executable_type(tmp_path):
    # The allowance is name AND type exact: reusing a permitted binding name
    # for a process-backed model must still be refused.
    path = _models_file(
        tmp_path, '.model bhv_x_cosim d_process(process="/bin/sh")\n'
    )
    with pytest.raises(SimraArtifactError) as caught:
        load_model_prelude(
            path, permitted_executable_models=frozenset({"bhv_x_cosim"})
        )
    assert caught.value.code == "configuration.models.executable_model"


def test_permitted_name_matching_is_case_insensitive(tmp_path):
    # ngspice resolves model names case-insensitively, so the gate must too:
    # an upper-cased spelling of a permitted card is the SAME card.
    path = _models_file(
        tmp_path, '.MODEL BHV_X_COSIM D_COSIM(simulation="/tmp/x.so")\n'
    )
    prelude, _record = load_model_prelude(
        path, permitted_executable_models=frozenset({"bhv_x_cosim"})
    )
    assert "BHV_X_COSIM" in prelude
