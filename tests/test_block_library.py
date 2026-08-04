"""Pure unit tests for the reviewed behavioral-block library authority.

No simulator is launched here.  Temporary libraries are built directly under
``tmp_path`` with manifests whose digests are recomputed with :mod:`hashlib`,
so every refusal below is provoked by real bytes rather than by mocking the
loader's own verification.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

import openada.block_library as block_library
from openada.block_library import (
    BlockLibraryError,
    compose_blocks,
    list_block_libraries,
    load_block_library,
    parse_blocks_selection,
)
from openada.engines.simra_artifact import MODELS_FORBIDDEN_RE


ROOT = Path(__file__).parents[1]
BHV_CORE = ROOT / "blocks" / "bhv-core"


def _contract(
    block_id: str,
    *,
    abi: int = 1,
    families: tuple[str, ...] = ("spice-primitive",),
    depends: tuple[str, ...] = (),
    wrapper: str | None = None,
) -> dict:
    contract: dict = {
        "schema": "openada.behavioral-block/v0alpha1",
        "block_id": block_id,
        "contract_version": "0.1.0",
        "abi_version": abi,
        "implementation_version": "0.1.0",
        "title": "Minimal test block",
        "category": "infrastructure",
        "description": "A minimal block used only by unit tests.",
        "ports": [
            {
                "ordinal": 0,
                "name": "a",
                "domain": "electrical",
                "role": "signal",
                "reference_port": "b",
                "input_loading": None,
                "output_drive": None,
            },
            {
                "ordinal": 1,
                "name": "b",
                "domain": "electrical",
                "role": "reference",
                "reference_port": None,
                "input_loading": None,
                "output_drive": None,
            },
        ],
        "parameters": [],
        "state": [],
        "analyses": {"supported": ["op", "dc", "ac", "tran"], "unsupported": []},
        "regularization": [],
        "limitations": [],
        "backends": {
            "ngspice-native": {
                "file": f"{block_id}.ngspice.sp",
                "wrapper": wrapper or f"bhv_{block_id}_v{abi}",
                "element_families": list(families),
            }
        },
        "golden_cases": ["smoke"],
    }
    if depends:
        contract["depends"] = list(depends)
    return contract


def _source(block_id: str, body: str = "R1 a b 1k", *, abi: int = 1) -> str:
    return f".subckt bhv_{block_id}_v{abi} a b\n{body}\n.ends\n"


def _install_library(
    tmp_path: Path,
    blocks: dict[str, dict],
    *,
    library_id: str = "tmplib",
    mutate_manifest=None,
) -> Path:
    """Write one complete library tree and its digest-correct manifest."""

    root = tmp_path / library_id
    for block_id, spec in blocks.items():
        directory = root / "blocks" / block_id
        directory.mkdir(parents=True)
        contract = spec["contract"]
        (directory / "block.json").write_text(
            json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        native = contract["backends"]["ngspice-native"]
        (directory / native["file"]).write_text(spec["source"], encoding="utf-8")

    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        files.append(
            {
                "path": relative,
                "role": "contract" if relative.endswith("block.json") else "ngspice-native",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": "openada.behavioral-block-library/v0alpha1",
        "library_id": library_id,
        "library_version": "0.1.0",
        "title": "Unit-test library",
        "description": "Built by tests/test_block_library.py.",
        "backend_requirements": {
            "simulators": [{"backend": "ngspice", "capabilities": []}]
        },
        "blocks": [
            {"block_id": block_id, "contract": f"blocks/{block_id}/block.json"}
            for block_id in sorted(blocks)
        ],
        "files": files,
    }
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    (root / "library-manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def tmp_roots(tmp_path, monkeypatch):
    # tmp_path is the trusted discovery base with an empty relative chain, so
    # the library directory itself is still walked O_NOFOLLOW.
    monkeypatch.setattr(block_library, "_blocks_roots", lambda: [(tmp_path, ())])
    return tmp_path


def _error(callable_):
    with pytest.raises(BlockLibraryError) as caught:
        callable_()
    return caught.value


# ---------------------------------------------------------------------------
# Happy path and installed-data resolution
# ---------------------------------------------------------------------------


def test_repo_tree_resolves_the_packaged_core_library():
    assert "bhv-core" in list_block_libraries()


def test_load_happy_path_digests_are_stable_across_loads():
    first = load_block_library("bhv-core")
    second = load_block_library("bhv-core")

    assert sorted(first.blocks) == [
        "comparator_clocked",
        "comparator_clocked_phys",
        "opamp_1p",
        "sw_bbm_pair",
    ]
    assert first.library_digest == second.library_digest

    # native composition covers the blocks with native backends; the
    # verilog-a-only block composes through the OSDI path instead
    selection = tuple(
        sorted(bid for bid, b in first.blocks.items() if b.native is not None)
    )
    composed_first = compose_blocks(first, selection)
    composed_second = compose_blocks(second, selection)
    assert composed_first.text == composed_second.text
    assert composed_first.text_sha256 == composed_second.text_sha256
    assert composed_first.closure_digest == composed_second.closure_digest
    assert (
        hashlib.sha256(composed_first.text.encode("utf-8")).hexdigest()
        == composed_first.text_sha256
    )

    record = composed_first.record()
    assert record["kind"] == "behavioral-block-composition"
    assert record["composition_sha256"] == composed_first.text_sha256
    assert record["library_digest"] == first.library_digest


def test_composed_text_passes_the_bounded_model_library_gate():
    library = load_block_library("bhv-core")
    # native composition covers every block WITH a native backend; verilog-a-
    # only blocks (comparator_clocked_phys) compose through the OSDI path
    native_ids = tuple(
        sorted(bid for bid, b in library.blocks.items() if b.native is not None)
    )
    composed = compose_blocks(library, native_ids)

    offending = [
        line for line in composed.text.splitlines() if MODELS_FORBIDDEN_RE.match(line)
    ]
    assert offending == []


# ---------------------------------------------------------------------------
# Manifest verification refusals
# ---------------------------------------------------------------------------


def test_schema_violation_is_refused(tmp_roots):
    def drop_title(manifest):
        del manifest["title"]

    _install_library(
        tmp_roots,
        {"blk": {"contract": _contract("blk"), "source": _source("blk")}},
        mutate_manifest=drop_title,
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.library.schema"


def test_flipping_one_source_byte_is_tampering(tmp_roots):
    shutil.copytree(BHV_CORE, tmp_roots / "bhv-core")
    victim = tmp_roots / "bhv-core" / "blocks" / "opamp_1p" / "opamp_1p.ngspice.sp"
    data = bytearray(victim.read_bytes())
    data[64] ^= 0x01
    victim.write_bytes(bytes(data))

    error = _error(lambda: load_block_library("bhv-core"))
    assert error.code == "blocks.library.file_tampered"


def test_removing_an_enumerated_file_is_refused(tmp_roots):
    shutil.copytree(BHV_CORE, tmp_roots / "bhv-core")
    (
        tmp_roots
        / "bhv-core"
        / "blocks"
        / "comparator_clocked"
        / "comparator_clocked.va"
    ).unlink()

    error = _error(lambda: load_block_library("bhv-core"))
    assert error.code == "blocks.library.file_missing"


def test_casefolded_path_collision_is_refused(tmp_roots, monkeypatch):
    # The manifest schema's lowercase path pattern already excludes mixed-case
    # paths, so the loader's own collision check is defence in depth behind
    # it.  Neutralize only the schema gate to prove the second lock holds.
    monkeypatch.setattr(block_library, "_schema_issues", lambda *args: [])
    root = tmp_roots / "tmplib"
    root.mkdir()
    (root / "library-manifest.json").write_text(
        json.dumps(
            {
                "schema": "openada.behavioral-block-library/v0alpha1",
                "library_id": "tmplib",
                "library_version": "0.1.0",
                "title": "collision",
                "description": "collision",
                "backend_requirements": {"simulators": []},
                "blocks": [],
                "files": [
                    {
                        "path": "blocks/x/part.sp",
                        "role": "ngspice-native",
                        "bytes": 1,
                        "sha256": "0" * 64,
                    },
                    {
                        "path": "blocks/x/PART.sp",
                        "role": "ngspice-native",
                        "bytes": 1,
                        "sha256": "0" * 64,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.library.path_collision"


def test_unknown_library_identity_is_refused(tmp_roots):
    error = _error(lambda: load_block_library("no-such-library"))
    assert error.code == "blocks.library.not_found"
    assert error.hint is not None


# ---------------------------------------------------------------------------
# Source vocabulary refusals
# ---------------------------------------------------------------------------


def test_embedded_stimulus_source_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": _source("blk", "V1 a b DC 1"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.embedded_stimulus"


def test_forbidden_directive_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": ".option reltol=1e-3\n" + _source("blk"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.directive_forbidden"


def test_undeclared_element_family_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                # spice-primitive only: the xspice a-device needs a family the
                # contract does not declare.
                "contract": _contract("blk", families=("spice-primitive",)),
                "source": _source("blk", "A1 [a] [b] bhv_blk_bridge"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.family_undeclared"


def test_missing_public_wrapper_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                # bhv_blk_v2 carries the block namespace, so it is accepted as
                # an internal symbol; the declared v1 wrapper is then absent.
                "source": _source("blk", abi=2),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.wrapper_missing"


def test_contract_wrapper_mismatch_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", wrapper="bhv_other_v1"),
                "source": _source("blk"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.wrapper_mismatch"


def test_process_backed_model_type_is_refused(tmp_roots):
    # d_process would hand ngspice an executable; the closed allowlist refuses
    # it no matter which families the contract declares.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("xspice-digital",)),
                "source": (
                    ".subckt bhv_blk_v1 a b\n"
                    '.model bhv_blk_p d_process(process_file="/bin/sh")\n'
                    "Abad a b bhv_blk_p\n"
                    ".ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.model_type_forbidden"


def test_model_type_outside_declared_families_is_refused(tmp_roots):
    # aswitch is a legal xspice-analog type, but this contract only declares
    # xspice-digital, so the capability map does not permit it.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("xspice-digital",)),
                "source": (
                    ".subckt bhv_blk_v1 a b\n"
                    ".model bhv_blk_sw aswitch(cntl_off=0 cntl_on=1)\n"
                    "Asw %v(a) %gd(a b) bhv_blk_sw\n"
                    ".ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.model_type_forbidden"


def test_unresolved_a_device_model_reference_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("xspice-digital",)),
                "source": (
                    ".subckt bhv_blk_v1 a b\n"
                    "Ainv a b bhv_blk_missing\n"
                    ".ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.reference_unresolved"


def test_unresolved_x_card_child_is_refused(tmp_roots):
    # The child is neither defined in this source nor the wrapper of a block
    # listed in `depends`, so it would resolve from the caller's deck.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": _source("blk", "X1 a b bhv_other_v1"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.reference_unresolved"


def test_symlinked_path_component_is_refused(tmp_roots):
    # The bytes behind the link are identical, so the digest would pass; the
    # per-component lstat traversal must refuse the link itself.
    shutil.copytree(BHV_CORE, tmp_roots / "bhv-core")
    directory = tmp_roots / "bhv-core" / "blocks" / "opamp_1p"
    relocated = tmp_roots / "relocated-opamp"
    shutil.move(directory, relocated)
    directory.symlink_to(relocated)

    error = _error(lambda: load_block_library("bhv-core"))
    assert error.code == "blocks.library.symlink"


def test_file_grown_past_its_manifest_byte_count_is_refused(tmp_roots):
    # The size gate fires on fstat before any content is read, so an
    # arbitrarily large replacement cannot force an unbounded allocation.
    shutil.copytree(BHV_CORE, tmp_roots / "bhv-core")
    victim = tmp_roots / "bhv-core" / "blocks" / "opamp_1p" / "opamp_1p.ngspice.sp"
    victim.write_bytes(victim.read_bytes() + b"*" * 1_000_000)

    error = _error(lambda: load_block_library("bhv-core"))
    assert error.code == "blocks.library.file_tampered"


def test_wrapper_pin_order_must_equal_the_contract_ports(tmp_roots):
    # Contract declares (a, b); the wrapper swaps them. Same names, same set,
    # different ABI.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": ".subckt bhv_blk_v1 b a\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.abi_mismatch"


def test_wrapper_parameter_vocabulary_must_equal_the_contract(tmp_roots):
    # The contract declares no parameters, but the wrapper header smuggles a
    # `gain` default the contract never reviewed.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": ".subckt bhv_blk_v1 a b gain=2\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.parameter_mismatch"


def test_wrapper_parameter_default_must_match_the_contract(tmp_roots):
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "gain",
            "type": "real",
            "default": 2.0,
            "units": "V/V",
            "scope": "contract",
            "description": "Test gain.",
        }
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": ".subckt bhv_blk_v1 a b gain=3\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.parameter_mismatch"


def test_contract_port_ordinal_gap_is_refused(tmp_roots):
    contract = _contract("blk")
    contract["ports"][1]["ordinal"] = 2

    _install_library(
        tmp_roots,
        {"blk": {"contract": contract, "source": _source("blk")}},
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.contract_invalid"


def test_contract_default_outside_declared_range_is_refused(tmp_roots):
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "gain",
            "type": "real",
            "default": 0.5,
            "units": "V/V",
            "scope": "contract",
            "range": {"minimum": 1.0},
            "description": "Default below its own minimum.",
        }
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": ".subckt bhv_blk_v1 a b gain=0.5\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.contract_invalid"


def test_nested_subckt_definition_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": (
                    ".subckt bhv_blk_v1 a b\n"
                    ".subckt bhv_blk_inner x y\n"
                    ".ends\n"
                    ".ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"


def test_mismatched_named_ends_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": ".subckt bhv_blk_v1 a b\nR1 a b 1k\n.ends bhv_blk_other\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"


def test_top_level_param_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk"),
                "source": ".param bhv_blk_x=1\n" + _source("blk"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"


def test_duplicate_symbol_within_one_block_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("spice-primitive", "diode")),
                "source": (
                    ".subckt bhv_blk_v1 a b\n"
                    ".model bhv_blk_m d\n"
                    ".model bhv_blk_m d\n"
                    "D1 a b bhv_blk_m\n"
                    ".ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.compose.symbol_collision"


def test_duplicate_library_roots_with_differing_content_are_ambiguous(
    tmp_path, monkeypatch
):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    _install_library(
        root_a, {"blk": {"contract": _contract("blk"), "source": _source("blk")}}
    )
    _install_library(
        root_b,
        {"blk": {"contract": _contract("blk"), "source": _source("blk", "R1 a b 2k")}},
    )
    monkeypatch.setattr(
        block_library, "_blocks_roots", lambda: [(root_a, ()), (root_b, ())]
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.library.ambiguous"


def test_duplicate_library_roots_with_identical_content_load(tmp_path, monkeypatch):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    _install_library(
        root_a, {"blk": {"contract": _contract("blk"), "source": _source("blk")}}
    )
    _install_library(
        root_b, {"blk": {"contract": _contract("blk"), "source": _source("blk")}}
    )
    monkeypatch.setattr(
        block_library, "_blocks_roots", lambda: [(root_a, ()), (root_b, ())]
    )

    library = load_block_library("tmplib")
    assert sorted(library.blocks) == ["blk"]


def test_unresolved_j_device_model_reference_is_refused(tmp_roots):
    # The review probe: `J1 ... caller_model` must never resolve from the
    # caller's deck. The model token sits after exactly three nodes.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("spice-primitive",)),
                "source": _source("blk", "J1 a b b caller_model"),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.reference_unresolved"


def test_x_card_must_name_the_dependency_exact_wrapper(tmp_roots):
    # `leaf` exists and is declared as a dependency, but its public wrapper is
    # bhv_leaf_v1; a wrapper-shaped bhv_leaf_v999 would resolve from the
    # caller's deck and must be refused.
    _install_library(
        tmp_roots,
        {
            "leaf": {
                "contract": _contract("leaf"),
                "source": _source("leaf"),
            },
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", "X1 a b bhv_leaf_v999"),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.reference_unresolved"
    assert "bhv_leaf_v999" in error.message


def test_x_card_params_keyword_cannot_smuggle_the_child(tmp_roots):
    # The exact round-3 bypass: ngspice invokes `caller_model` here, while the
    # last '='-free token is the dependency's exact wrapper. The non-compact
    # parameter grammar (a params: keyword) is refused outright, so the card
    # can never load, let alone resolve from the caller's deck.
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source(
                    "top", "X1 a b caller_model PARAMS: p = bhv_leaf_v1"
                ),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"
    assert "name=value" in error.message


def test_x_card_bare_equals_parameter_grammar_is_refused(tmp_roots):
    # `name = value` with a bare '=' token is the other non-compact spelling;
    # a reviewed block source must use the compact name=value form.
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", "X1 a b bhv_leaf_v1 tdead = 10n"),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"
    assert "name=value" in error.message


def test_x_card_compact_parameter_form_still_loads(tmp_roots):
    # The legitimate compact spelling keeps working: the child is the token
    # immediately before the first name=value parameter.
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", "X1 a b bhv_leaf_v1 tdead=10n"),
            },
        },
    )

    library = load_block_library("tmplib")
    assert set(library.blocks) == {"leaf", "top"}


@pytest.mark.parametrize(
    "control",
    ("\x0b", "\x0c", "\r", "\x85", " ", " ", "\x00", "\x7f"),
)
def test_control_characters_that_splitlines_treats_as_breaks_are_refused(
    tmp_roots, control
):
    # str.splitlines() treats VT/FF/CR/NEL/LS/PS as line boundaries; a control
    # byte could otherwise smuggle a comment marker onto a synthetic sub-line
    # that never reaches the marker gate while ngspice parses the original
    # byte. The source is split on LF alone and every other control character
    # is refused before comment classification.
    smuggled = f"X1 a b caller_model{control}* $ bhv_leaf_v1"
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", smuggled),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"
    assert "control character" in error.message


@pytest.mark.parametrize(
    "card",
    (
        "X1 a b caller_model ; bhv_leaf_v1",
        "X1 a b caller_model // bhv_leaf_v1",
        "X1 a b bhv_leaf_v1 tdead=10n $ trailing comment",
    ),
)
def test_inline_comment_characters_are_refused_outright(tmp_roots, card):
    # ngspice's inline-comment semantics (';', '$', '//') depend on the
    # startup compatibility configuration (under ngbehavior=ps a '$' is
    # ordinary text), so the library grammar refuses the whole class instead
    # of emulating a moving target: block sources carry full-line '*'
    # comments only. Each marker is refused wherever it appears.
    _install_library(
        tmp_roots,
        {
            "leaf": {
                "contract": _contract("leaf"),
                "source": _source("leaf"),
            },
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", card),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid", card
    assert "inline-comment" in error.message, card


@pytest.mark.parametrize(
    "star_line",
    (
        "*# echo smuggled",       # '*#' with a space
        "*#shell ls",             # '*#' with no separator
        "  *# source /etc/x",     # leading whitespace before '*#'
    ),
)
def test_hash_star_control_line_is_refused_not_treated_as_comment(
    tmp_roots, star_line
):
    # ngspice reads a '*#'-prefixed line as a control-mode COMMAND (it strips
    # the '*#' and runs the rest, in batch too) -- a convention that hides the
    # command from other simulators. Dropping it as a comment would let a block
    # source smuggle an arbitrary control command past the element and
    # directive allowlists. The trigger is '*' immediately followed by '#'.
    _install_library(
        tmp_roots,
        {
            "leaf": {
                "contract": _contract("leaf"),
                "source": _source("leaf", f"{star_line}\nR1 a b 1k"),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid", star_line
    assert "control command" in error.message, star_line


@pytest.mark.parametrize(
    "comment_line",
    (
        "* an ordinary comment",
        "* # a hash after a separator stays inert",
        "*--------------------------------",
    ),
)
def test_plain_star_comments_remain_inert(tmp_roots, comment_line):
    # The '*#' refusal must not tighten ordinary full-line '*' comments, which
    # ngspice ignores; only the exact '*#' sequence is a command.
    _install_library(
        tmp_roots,
        {
            "leaf": {
                "contract": _contract("leaf"),
                "source": _source("leaf", f"{comment_line}\nR1 a b 1k"),
            },
        },
    )

    library = load_block_library("tmplib")
    assert library is not None


def test_x_card_tail_assignment_with_empty_name_is_refused(tmp_roots):
    # '=10n' is not a compact name=value token (the name is empty); it must
    # never be waved through as a parameter assignment.
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", "X1 a b bhv_leaf_v1 =10n"),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"
    assert "name=value" in error.message


def test_x_card_late_params_keyword_after_an_assignment_is_refused(tmp_roots):
    # The COMPLETE tail is validated: a params: keyword hiding after one
    # legitimate compact assignment is still refused, not just a keyword that
    # happens to be the first terminator.
    _install_library(
        tmp_roots,
        {
            "leaf": {"contract": _contract("leaf"), "source": _source("leaf")},
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source(
                    "top", "X1 a b bhv_leaf_v1 tdead=10n params: q=1"
                ),
            },
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.source.invalid"
    assert "name=value" in error.message


def test_b_source_bare_equals_stays_legal_without_comments(tmp_roots):
    # The inline-comment refusal never tightens the bare '=' spelling on
    # non-X cards: a comment-free B-source keeps parsing.
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": _contract("blk", families=("b-source",)),
                "source": _source("blk", "B1 a b V = V(a)"),
            }
        },
    )

    library = load_block_library("tmplib")
    assert set(library.blocks) == {"blk"}


def test_x_card_child_extraction_scans_for_the_first_terminator():
    # Parse-level guarantees behind the refusal: the node/child list ends at
    # the FIRST parameter terminator (params: keyword, bare '=', or any
    # name=value token), and the bare '=' form skips the parameter name that
    # precedes it. Filtering '='-free tokens and taking the last one is
    # exactly the heuristic the round-3 bypass defeated.
    assert block_library._x_card_child(
        "X1 a b caller_model PARAMS: p = bhv_leaf_v1".split()
    ) == ("caller_model", True, ("PARAMS:", "p", "=", "bhv_leaf_v1"))
    assert block_library._x_card_child(
        "X1 a b caller_model p = bhv_leaf_v1".split()
    ) == ("caller_model", True, ("p", "=", "bhv_leaf_v1"))
    assert block_library._x_card_child(
        "X1 a b bhv_leaf_v1 tdead=10n".split()
    ) == ("bhv_leaf_v1", False, ("tdead=10n",))
    assert block_library._x_card_child("X1 a b bhv_leaf_v1".split()) == (
        "bhv_leaf_v1",
        False,
        (),
    )
    assert block_library._x_card_child("X1".split()) == (None, False, ())
    # The tail is COMPLETE: tokens after the first terminator are returned
    # too, so the caller's whole-tail validation can refuse a late params:
    # keyword or a malformed assignment hiding behind a legitimate one.
    assert block_library._x_card_child(
        "X1 a b bhv_leaf_v1 tdead=10n params: q=1".split()
    ) == ("bhv_leaf_v1", False, ("tdead=10n", "params:", "q=1"))
    assert block_library._x_card_child(
        "X1 a b bhv_leaf_v1 =10n".split()
    ) == ("bhv_leaf_v1", False, ("=10n",))


def test_symlinked_library_root_is_refused(tmp_roots):
    # The bytes behind the link are pristine, so every digest would pass; the
    # root itself must still be refused before any descriptor-relative walk.
    shutil.copytree(BHV_CORE, tmp_roots / "bhv-core-real")
    (tmp_roots / "bhv-core").symlink_to(tmp_roots / "bhv-core-real")

    error = _error(lambda: load_block_library("bhv-core"))
    assert error.code == "blocks.library.symlink"


def test_symlinked_installed_root_is_discovered_unresolved_and_refused(
    tmp_path, monkeypatch
):
    # Discovery must derive the installed-distribution root WITHOUT resolving
    # symlinks: resolving would follow a symlinked library root into its
    # target before the loader's root lstat + O_NOFOLLOW walk (the only
    # symlink authority) could refuse it. Here the installed layout's
    # `fakelib` root is a symlink to a pristine tree elsewhere; the loader
    # must see the SYMLINK path and refuse it, never the resolved target.
    import importlib.metadata as importlib_metadata

    _install_library(tmp_path / "elsewhere", {
        "blk": {"contract": _contract("blk"), "source": _source("blk")},
    }, library_id="fakelib")
    layout = tmp_path / "dist" / "share" / "openada" / "blocks"
    layout.mkdir(parents=True)
    (layout / "fakelib").symlink_to(tmp_path / "elsewhere" / "fakelib")

    class _Entry:
        @staticmethod
        def as_posix():
            return "share/openada/blocks/fakelib/library-manifest.json"

    class _Distribution:
        files = [_Entry]

        @staticmethod
        def locate_file(item):
            return tmp_path / "dist" / item.as_posix()

    monkeypatch.setattr(
        importlib_metadata, "distribution", lambda name: _Distribution
    )

    # Discovery reports the trusted distribution base plus the UNRESOLVED
    # relative chain down to the blocks directory.
    assert (tmp_path / "dist", ("share", "openada", "blocks")) in (
        block_library._blocks_roots()
    )

    error = _error(lambda: load_block_library("fakelib"))
    assert error.code == "blocks.library.symlink"


def test_symlinked_intermediate_chain_component_is_refused(tmp_path, monkeypatch):
    # The descriptor walk starts at the TRUSTED discovery base and covers
    # every relative component below it; a symlinked `blocks` directory (an
    # ANCESTOR of the library root, exactly the round-4 bypass) is refused
    # even though the final `tmplib` component itself is a real directory.
    real = tmp_path / "real-blocks"
    _install_library(
        real, {"blk": {"contract": _contract("blk"), "source": _source("blk")}}
    )
    base = tmp_path / "dist"
    (base / "share" / "openada").mkdir(parents=True)
    (base / "share" / "openada" / "blocks").symlink_to(real)
    monkeypatch.setattr(
        block_library,
        "_blocks_roots",
        lambda: [(base, ("share", "openada", "blocks"))],
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.library.symlink"


def test_installed_layout_without_symlinks_loads_through_the_chain(
    tmp_path, monkeypatch
):
    # The same wheel-shaped layout with real directories at every chain
    # component keeps loading: the O_NOFOLLOW walk only refuses symlinks.
    base = tmp_path / "dist"
    layout = base / "share" / "openada" / "blocks"
    layout.mkdir(parents=True)
    _install_library(
        layout, {"blk": {"contract": _contract("blk"), "source": _source("blk")}}
    )
    monkeypatch.setattr(
        block_library,
        "_blocks_roots",
        lambda: [(base, ("share", "openada", "blocks"))],
    )

    library = load_block_library("tmplib")
    assert sorted(library.blocks) == ["blk"]


def test_spice_literal_follows_ngspice_scale_and_unit_semantics():
    # Scale factors are matched longest-first and trailing unit letters are
    # ignored per ngspice; non-finite parses are refused as non-literal.
    assert block_library._spice_literal("1us") == pytest.approx(1e-6)
    assert block_library._spice_literal("1kohm") == pytest.approx(1e3)
    assert block_library._spice_literal("100mohm") == pytest.approx(0.1)
    assert block_library._spice_literal("1mil") == pytest.approx(25.4e-6)
    assert block_library._spice_literal("2a") == pytest.approx(2e-18)
    assert block_library._spice_literal("10meg") == pytest.approx(1e7)
    assert block_library._spice_literal("3ohm") == pytest.approx(3.0)
    assert block_library._spice_literal("1e999") is None
    assert block_library._spice_literal("1u s") is None
    assert block_library._spice_literal("1x2") is None
    assert not block_library._defaults_match(float("inf"), float("inf"))


def test_unit_suffixed_wrapper_default_matches_the_contract(tmp_roots):
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "td",
            "type": "real",
            "default": 1e-6,
            "units": "s",
            "scope": "contract",
            "description": "Delay with an ngspice unit-annotated default.",
        }
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": ".subckt bhv_blk_v1 a b td=1us\nR1 a b 1k\n.ends\n",
            }
        },
    )

    library = load_block_library("tmplib")
    assert sorted(library.blocks) == ["blk"]


def test_overflowing_wrapper_default_is_refused_as_non_literal(tmp_roots):
    # 1e999 parses to infinity; an infinite default can never be compared
    # against the finite contract default and must fail closed.
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "gain",
            "type": "real",
            "default": 2.0,
            "units": "V/V",
            "scope": "contract",
            "description": "Test gain.",
        }
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": ".subckt bhv_blk_v1 a b gain=1e999\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.parameter_mismatch"


def test_boolean_as_integer_default_must_be_exactly_zero_or_one(tmp_roots):
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "enable",
            "type": "boolean-as-integer",
            "default": 0.5,
            "units": "",
            "scope": "contract",
            "description": "Half-true is not a boolean.",
        }
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": ".subckt bhv_blk_v1 a b enable=0.5\nR1 a b 1k\n.ends\n",
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.contract_invalid"


def test_wrapper_parameter_order_must_equal_the_contract(tmp_roots):
    # Same names, same defaults, reversed order: the header order is the ABI.
    contract = _contract("blk")
    contract["parameters"] = [
        {
            "name": "gain",
            "type": "real",
            "default": 2.0,
            "units": "V/V",
            "scope": "contract",
            "description": "First declared parameter.",
        },
        {
            "name": "rout",
            "type": "real",
            "default": 100.0,
            "units": "ohm",
            "scope": "contract",
            "description": "Second declared parameter.",
        },
    ]
    _install_library(
        tmp_roots,
        {
            "blk": {
                "contract": contract,
                "source": (
                    ".subckt bhv_blk_v1 a b rout=100 gain=2\nR1 a b 1k\n.ends\n"
                ),
            }
        },
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.parameter_mismatch"


def test_empty_port_loading_object_is_a_schema_violation(tmp_roots):
    # input_loading may be null (undeclared) or a non-empty object; an empty
    # object silently claims "declared" while stating nothing.
    contract = _contract("blk")
    contract["ports"][0]["input_loading"] = {}

    _install_library(
        tmp_roots,
        {"blk": {"contract": contract, "source": _source("blk")}},
    )

    error = _error(lambda: load_block_library("tmplib"))
    assert error.code == "blocks.block.schema"


# ---------------------------------------------------------------------------
# Composition refusals
# ---------------------------------------------------------------------------


def test_symbol_collision_across_blocks_is_refused(tmp_roots):
    # Both blocks legitimately pass the per-block namespace check ("col_two"
    # names start with "bhv_col" too) yet collide in the shared simulator
    # namespace once composed together.
    # The "diode" family is declared so the `.model ... d` definitions pass
    # the closed model-type allowlist; the collision under test is the shared
    # simulator namespace, not the capability gate.
    _install_library(
        tmp_roots,
        {
            "col": {
                "contract": _contract("col", families=("spice-primitive", "diode")),
                "source": _source("col") + ".model bhv_col_two_m d\n",
            },
            "col_two": {
                "contract": _contract(
                    "col_two", families=("spice-primitive", "diode")
                ),
                "source": _source("col_two") + ".model bhv_col_two_m d\n",
            },
        },
    )
    library = load_block_library("tmplib")

    error = _error(lambda: compose_blocks(library, ("col", "col_two")))
    assert error.code == "blocks.compose.symbol_collision"


def test_unknown_block_selection_is_refused():
    library = load_block_library("bhv-core")

    error = _error(lambda: compose_blocks(library, ("does_not_exist",)))
    assert error.code == "blocks.compose.unknown_block"


def test_dependency_cycle_is_refused(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "cyc_a": {
                "contract": _contract("cyc_a", depends=("cyc_b",)),
                "source": _source("cyc_a"),
            },
            "cyc_b": {
                "contract": _contract("cyc_b", depends=("cyc_a",)),
                "source": _source("cyc_b"),
            },
        },
    )
    library = load_block_library("tmplib")

    error = _error(lambda: compose_blocks(library, ("cyc_a",)))
    assert error.code == "blocks.compose.dependency_cycle"


def test_dependencies_are_composed_in_topological_order(tmp_roots):
    _install_library(
        tmp_roots,
        {
            "leaf": {
                "contract": _contract("leaf"),
                "source": _source("leaf"),
            },
            "top": {
                "contract": _contract("top", depends=("leaf",)),
                "source": _source("top", "X1 a b bhv_leaf_v1"),
            },
        },
    )
    library = load_block_library("tmplib")

    composed = compose_blocks(library, ("top",))
    assert composed.requested == ("top",)
    assert composed.closure == ("leaf", "top")


# ---------------------------------------------------------------------------
# CLI selection grammar
# ---------------------------------------------------------------------------


def test_parse_blocks_selection_accepts_the_documented_grammar():
    assert parse_blocks_selection("bhv-core:opamp_1p") == (
        "bhv-core",
        ("opamp_1p",),
    )
    assert parse_blocks_selection("bhv-core: opamp_1p , sw_bbm_pair ") == (
        "bhv-core",
        ("opamp_1p", "sw_bbm_pair"),
    )


@pytest.mark.parametrize("value", ["bhv-core", "bhv-core:", "bhv-core: ,,"])
def test_parse_blocks_selection_refuses_malformed_values(value):
    error = _error(lambda: parse_blocks_selection(value))
    assert error.code == "blocks.selection.invalid"
