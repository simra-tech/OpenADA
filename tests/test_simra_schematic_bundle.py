from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from openada.engines import simra_artifact
from openada.engines.simra_artifact import (
    SimraArtifactError,
    load_simra_schematic_bundle,
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
