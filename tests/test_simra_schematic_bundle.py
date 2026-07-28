from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Iterator

import pytest

from openada.engines import simra_artifact
from openada.engines.simra_artifact import (
    SimraArtifactError,
    load_simra_schematic_bundle,
)

_REAL_VALIDATOR_LOADER = simra_artifact._load_simra_bundle_validator


@pytest.fixture(autouse=True)
def _standalone_validator_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hand-authored unit bundles on the documented standalone path."""

    monkeypatch.setattr(
        simra_artifact,
        "_load_simra_bundle_validator",
        lambda: None,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _view_document() -> dict[str, Any]:
    return {
        "schema": "simra.schematic/v2",
        "view": {
            "id": "dut-publication",
            "kind": "schematic",
            "title": "DUT",
            "top": "DUT",
            "top_cell": "cell:DUT",
            "topology_id": None,
        },
        # A compiled schematic may publish this field as null. That is absence,
        # not an embedded testbench.
        "testbench": None,
        "cells": [
            {
                "id": "cell:DUT",
                "name": "DUT",
                "kind": "design",
                "port_order": ["A", "Y"],
                "entities": {
                    "ports": [
                        {
                            "id": "port:DUT/A",
                            "name": "A",
                            "net": "net:DUT/A",
                            "ordinal": 0,
                        },
                        {
                            "id": "port:DUT/Y",
                            "name": "Y",
                            "net": "net:DUT/Y",
                            "ordinal": 1,
                        },
                    ],
                    "nets": [
                        {"id": "net:DUT/A", "name": "A", "external": True},
                        {"id": "net:DUT/Y", "name": "Y", "external": True},
                    ],
                    "instances": [
                        {
                            "id": "instance:DUT/R_LOAD",
                            "name": "R_LOAD",
                            "kind": "resistor",
                            "netlistable": True,
                            "parameter_status": "resolved",
                            "parameters": {"r": "1k"},
                            "terminals": {
                                "P": "net:DUT/A",
                                "M": "net:DUT/Y",
                            },
                        }
                    ],
                },
            }
        ],
    }


def _write_bundle(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    source = tmp_path / "design.ord"
    view = tmp_path / "schematic.simra.json"
    netlist = tmp_path / "design.spice"
    cdl = tmp_path / "design.cdl"
    descriptor = tmp_path / "schematic.artifact.json"

    source.write_bytes(b"SCHEMATIC DUT\\nPORT A Y\\n")
    view.write_bytes(_json_bytes(_view_document()))
    netlist.write_bytes(
        b"* Simra DUT\n"
        b".SUBCKT DUT A Y\n"
        b"R_LOAD A Y 1k\n"
        b".ENDS DUT\n"
    )
    cdl.write_bytes(
        b"* Simra DUT CDL\n"
        b".SUBCKT DUT A Y\n"
        b"R_LOAD A Y 1k\n"
        b".ENDS DUT\n"
    )
    document = {
        "schema": "simra.schematic-artifact/v2",
        "id": "dut-publication",
        "kind": "schematic",
        "label": "DUT",
        "top": "DUT",
        "source": source.name,
        "view": view.name,
        "netlist": netlist.name,
        "cdl": cdl.name,
        "hashes": {
            "source_sha256": _sha256(source),
            "view_sha256": _sha256(view),
            "netlist_sha256": _sha256(netlist),
            "cdl_sha256": _sha256(cdl),
        },
        "netlistable": True,
        "validation": {"netlistable": True, "parameters": "resolved"},
        # Deliberately stale metadata proves that the loader rederives this
        # claim from the exact captured view and netlist instead of trusting it.
        "lifecycle": {
            "state": "display_candidate",
            "unresolved": [{"instance": "forged", "parameter": "value"}],
            "blockers": ["forged"],
        },
    }
    descriptor.write_bytes(_json_bytes(document))
    return descriptor, _actual_digests(descriptor)


def _actual_digests(descriptor: Path) -> dict[str, str]:
    document = json.loads(descriptor.read_text(encoding="utf-8"))
    return {
        "descriptor_sha256": _sha256(descriptor),
        "source_sha256": _sha256(descriptor.parent / document["source"]),
        "view_sha256": _sha256(descriptor.parent / document["view"]),
        "netlist_sha256": _sha256(descriptor.parent / document["netlist"]),
        "cdl_sha256": _sha256(descriptor.parent / document["cdl"]),
    }


def _republish_view(descriptor: Path, view_document: dict[str, Any]) -> dict[str, str]:
    descriptor_document = json.loads(descriptor.read_text(encoding="utf-8"))
    view = descriptor.parent / descriptor_document["view"]
    view.write_bytes(_json_bytes(view_document))
    descriptor_document["hashes"]["view_sha256"] = _sha256(view)
    descriptor.write_bytes(_json_bytes(descriptor_document))
    return _actual_digests(descriptor)


def _republish_netlist(descriptor: Path, netlist_bytes: bytes) -> dict[str, str]:
    descriptor_document = json.loads(descriptor.read_text(encoding="utf-8"))
    netlist = descriptor.parent / descriptor_document["netlist"]
    netlist.write_bytes(netlist_bytes)
    descriptor_document["hashes"]["netlist_sha256"] = _sha256(netlist)
    descriptor.write_bytes(_json_bytes(descriptor_document))
    return _actual_digests(descriptor)


def test_snapshot_captures_each_file_once_and_rederives_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, expected = _write_bundle(tmp_path)
    calls: list[str] = []
    original = simra_artifact.stable_regular_file

    @contextmanager
    def counted(path: str | Path) -> Iterator[Any]:
        calls.append(Path(path).name)
        with original(path) as opened:
            yield opened

    monkeypatch.setattr(simra_artifact, "stable_regular_file", counted)
    bundle = load_simra_schematic_bundle(
        descriptor,
        expected_digests=expected,
        expected_top="DUT",
    )

    assert Counter(calls) == Counter(
        {
            "schematic.artifact.json": 1,
            "design.ord": 1,
            "schematic.simra.json": 1,
            "design.spice": 1,
            "design.cdl": 1,
        }
    )
    assert bundle.top == "DUT"
    assert bundle.port_order == ("A", "Y")
    assert bundle.design_subckt_names == ("DUT",)
    assert bundle.lifecycle == {
        "state": "simulation_candidate",
        "unresolved": [],
        "blockers": [],
    }
    assert bundle.descriptor_document["lifecycle"]["state"] == "display_candidate"
    assert bundle.bundle_digests == expected
    assert len(bundle.input_records) == 5
    assert bundle.descriptor_bytes == descriptor.read_bytes()
    assert bundle.source_text == "SCHEMATIC DUT\\nPORT A Y\\n"
    assert bundle.netlist_bytes == (tmp_path / "design.spice").read_bytes()
    assert bundle.cdl_text.startswith("* Simra DUT CDL")
    with pytest.raises(FrozenInstanceError):
        bundle.top = "other"  # type: ignore[misc]


def test_testbench_field_may_be_absent(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    del view_document["testbench"]
    expected = _republish_view(descriptor, view_document)

    bundle = load_simra_schematic_bundle(
        descriptor,
        expected_digests=expected,
        expected_top="DUT",
    )
    assert bundle.lifecycle["state"] == "simulation_candidate"


def test_caller_bundle_digest_mismatch_is_typed(tmp_path: Path) -> None:
    descriptor, expected = _write_bundle(tmp_path)
    expected["netlist_sha256"] = "0" * 64

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.digest_mismatch"


def test_descriptor_published_member_digest_is_revalidated(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    document = json.loads(descriptor.read_text(encoding="utf-8"))
    document["hashes"]["cdl_sha256"] = "0" * 64
    descriptor.write_bytes(_json_bytes(document))

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=_actual_digests(descriptor),
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.digest_mismatch"


@pytest.mark.parametrize("published_name", ["schematic.artifact.json", "design.ord"])
def test_descriptor_and_members_must_not_be_symlinks(
    tmp_path: Path,
    published_name: str,
) -> None:
    descriptor, expected = _write_bundle(tmp_path)
    published = tmp_path / published_name
    target = tmp_path / f"{published_name}.real"
    published.rename(target)
    published.symlink_to(target.name)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.artifact_unstable"


def test_embedded_testbench_is_refused(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    view_document["testbench"] = {"analyses": [{"kind": "op"}]}
    expected = _republish_view(descriptor, view_document)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.embedded_testbench"


@pytest.mark.parametrize(
    ("kind", "parameters", "code"),
    [
        ("vdc", {"dc": "1"}, "experiment.dut.embedded_stimulus"),
        ("ground", {}, "experiment.dut.global_ground_internal"),
    ],
)
def test_embedded_stimulus_and_ground_are_refused(
    tmp_path: Path,
    kind: str,
    parameters: dict[str, Any],
    code: str,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    instance = view_document["cells"][0]["entities"]["instances"][0]
    instance["kind"] = kind
    instance["parameters"] = parameters
    expected = _republish_view(descriptor, view_document)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == code


def test_literal_zero_internal_net_is_refused(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    view_document["cells"][0]["entities"]["nets"][0]["name"] = "0"
    expected = _republish_view(descriptor, view_document)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.global_ground_internal"


def test_lifecycle_is_rederived_from_required_parameters(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    view_document["cells"][0]["entities"]["instances"][0]["parameters"] = {}
    expected = _republish_view(descriptor, view_document)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.not_promoted"
    assert '"parameter":"r"' in excinfo.value.message


def test_top_subckt_port_order_must_match_the_view(tmp_path: Path) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    netlist = tmp_path / "design.spice"
    netlist.write_bytes(
        netlist.read_bytes().replace(b".SUBCKT DUT A Y", b".SUBCKT DUT Y A")
    )
    document = json.loads(descriptor.read_text(encoding="utf-8"))
    document["hashes"]["netlist_sha256"] = _sha256(netlist)
    descriptor.write_bytes(_json_bytes(document))

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=_actual_digests(descriptor),
            expected_top="DUT",
        )
    assert excinfo.value.code == "experiment.dut.port_abi_mismatch"


def test_standalone_structural_replay_accepts_required_device_prefixing(
    tmp_path: Path,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    view_document = _view_document()
    view_document["cells"][0]["entities"]["instances"][0]["name"] = "LOAD"
    expected = _republish_view(descriptor, view_document)

    bundle = load_simra_schematic_bundle(
        descriptor,
        expected_digests=expected,
        expected_top="DUT",
    )

    assert bundle.netlist_text.count("R_LOAD A Y 1k") == 1


@pytest.mark.parametrize(
    "replacement",
    (
        b"R_LOAD A 0 1k",
        b"R_LOAD A Y 2k",
    ),
)
def test_standalone_structural_replay_refuses_republished_netlist_edit(
    tmp_path: Path,
    replacement: bytes,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    expected = _republish_netlist(
        descriptor,
        (tmp_path / "design.spice").read_bytes().replace(
            b"R_LOAD A Y 1k",
            replacement,
        ),
    )

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )

    assert excinfo.value.code == "experiment.dut.netlist_view_mismatch"


def test_standalone_structural_replay_refuses_netlist_only_source_card(
    tmp_path: Path,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    expected = _republish_netlist(
        descriptor,
        (tmp_path / "design.spice").read_bytes().replace(
            b".ENDS DUT",
            b"V_ATTACK A Y DC 1\n.ENDS DUT",
        ),
    )

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )

    assert excinfo.value.code == "experiment.dut.embedded_stimulus"


def test_importable_validator_receives_exact_captured_five_file_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, expected = _write_bundle(tmp_path)
    calls: list[tuple[dict[str, bytes], bytes, str]] = []

    def validator(
        files: dict[str, bytes],
        expected_source: bytes,
        expected_top: str,
    ) -> dict[str, object]:
        calls.append((files, expected_source, expected_top))
        return {"descriptor": {}, "document": {}}

    monkeypatch.setattr(
        simra_artifact,
        "_load_simra_bundle_validator",
        lambda: validator,
    )
    bundle = load_simra_schematic_bundle(
        descriptor,
        expected_digests=expected,
        expected_top="DUT",
    )

    assert bundle.top == "DUT"
    assert len(calls) == 1
    files, expected_source, expected_top = calls[0]
    assert set(files) == {
        "schematic.artifact.json",
        "design.ord",
        "schematic.simra.json",
        "design.spice",
        "design.cdl",
    }
    assert expected_source is files["design.ord"]
    assert expected_top == "DUT"
    assert files == {
        "schematic.artifact.json": bundle.descriptor_bytes,
        "design.ord": bundle.source_bytes,
        "schematic.simra.json": bundle.view_bytes,
        "design.spice": bundle.netlist_bytes,
        "design.cdl": bundle.cdl_bytes,
    }


def test_importable_validator_maps_reexport_mismatch_to_bundle_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    expected = _republish_netlist(
        descriptor,
        (tmp_path / "design.spice").read_bytes().replace(
            b"R_LOAD A Y 1k",
            b"R_LOAD A 0 1k",
        ),
    )

    def validator(
        files: dict[str, bytes],
        expected_source: bytes,
        expected_top: str,
    ) -> dict[str, object]:
        assert files["design.spice"] == (tmp_path / "design.spice").read_bytes()
        assert expected_source == (tmp_path / "design.ord").read_bytes()
        assert expected_top == "DUT"
        raise ValueError("design.spice differs from deterministic graph re-export")

    monkeypatch.setattr(
        simra_artifact,
        "_load_simra_bundle_validator",
        lambda: validator,
    )
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )

    assert excinfo.value.code == "experiment.dut.bundle_invalid"


def test_coordinated_simra_validator_refuses_real_netlist_ground_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = _REAL_VALIDATOR_LOADER()
    if validator is None:
        pytest.skip(
            "the coordinated Simra bundle validator is not importable; "
            "standalone structural replay is covered separately"
        )

    fixture = Path(__file__).parent / "fixtures" / "dut-source-follower"
    publication = tmp_path / "dut-source-follower"
    shutil.copytree(fixture, publication)
    descriptor = publication / "schematic.artifact.json"
    original = (publication / "design.spice").read_bytes()
    original_card = (
        b"M_BIAS Y VBIAS VSS VSS nmos.core W=10u L=180n M=1 NF=1"
    )
    edited_card = (
        b"M_BIAS 0 VBIAS VSS VSS nmos.core W=10u L=180n M=1 NF=1"
    )
    assert original_card in original
    expected = _republish_netlist(
        descriptor,
        original.replace(original_card, edited_card),
    )
    monkeypatch.setattr(
        simra_artifact,
        "_load_simra_bundle_validator",
        lambda: validator,
    )

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="SourceFollower",
        )

    assert excinfo.value.code == "experiment.dut.bundle_invalid"


def test_structural_literal_zero_refusal_remains_after_validator_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, _expected = _write_bundle(tmp_path)
    expected = _republish_netlist(
        descriptor,
        (tmp_path / "design.spice").read_bytes().replace(
            b"R_LOAD A Y 1k",
            b"R_LOAD A 0 1k",
        ),
    )
    monkeypatch.setattr(
        simra_artifact,
        "_load_simra_bundle_validator",
        lambda: (
            lambda _files, _source, _top: {
                "descriptor": {},
                "document": {},
            }
        ),
    )

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_schematic_bundle(
            descriptor,
            expected_digests=expected,
            expected_top="DUT",
        )

    assert excinfo.value.code == "experiment.dut.netlist_view_mismatch"
