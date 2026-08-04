from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any

import pytest

from openada.discovery import DiscoveryManager
from openada.operations.experiment import run_experiment


SOURCE_FOLLOWER_BUNDLE = Path(
    os.environ.get(
        "OPENADA_DUT_BUNDLE",
        Path(__file__).parent / "fixtures" / "dut-source-follower",
    )
)
PDK_ROOT = Path.home() / ".cache" / "openada" / "pdk-root"

SOURCE_FOLLOWER_DIGESTS = {
    "descriptor_sha256": (
        "cddbb080806379d929aae3ca211c799010a5c96552526247a419250ebbc99ed5"
    ),
    "source_sha256": (
        "69a01a488db517b5860688f0e59cc38baf4e194521da2189d7cb6107791856e8"
    ),
    "view_sha256": (
        "e26e7fbe8f19fa49c864e2ea3527e924a016f133c6190ce4328bd405b1976516"
    ),
    "netlist_sha256": (
        "5ff9c3e3ff17f5f11830d66094c46ea8a5b8834a451fda5bfbb5d0cab2ce38d3"
    ),
    "cdl_sha256": (
        "4083dfd1acb70f66a7ad0d0ab97a0d1840540dcb45bf056423061914a61783d2"
    ),
}


def _crossing_request(
    measurement_id: str, signal: str, threshold_v: float
) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "kind": "crossing",
        "signal": signal,
        "parameters": {
            "threshold": {"value": threshold_v, "unit": "V"},
            "direction": "rising",
            "occurrence": 1,
        },
        "extensions": {},
    }


def _derivation_spec() -> dict[str, Any]:
    return {
        "schema": "simra.experiment/v1",
        "id": "sf_transient_delay",
        "dut": {
            "artifact": str(SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json"),
            "bundle": SOURCE_FOLLOWER_DIGESTS,
            "top": "SourceFollower",
            "connections": {
                "A": "IN",
                "Y": "OUT",
                "VBIAS": "NBIAS",
                "VDD": "NVDD",
                "VSS": "0",
            },
        },
        "elements": [
            {
                "name": "V_DD",
                "kind": "vdc",
                "plus": "NVDD",
                "minus": "0",
                "parameters": {"dc": "1.2"},
            },
            {
                "name": "V_BIAS",
                "kind": "vdc",
                "plus": "NBIAS",
                "minus": "0",
                "parameters": {"dc": "0.7"},
            },
            {
                "name": "V_IN",
                "kind": "vpulse",
                "plus": "IN",
                "minus": "0",
                "parameters": {
                    "initial_value": "0.7",
                    "pulsed_value": "1.1",
                    "delay_time": "2n",
                    "rise_time": "100p",
                    "fall_time": "100p",
                    "pulse_width": "5n",
                    "period": "10n",
                },
            },
            {
                "name": "C_LOAD",
                "kind": "capacitor",
                "plus": "OUT",
                "minus": "0",
                "parameters": {"c": "1p"},
            },
        ],
        "analyses": [
            {
                "id": "edge",
                "kind": "tran",
                "step": "20p",
                "stop": "8n",
                "max_step": "20p",
            }
        ],
        "observations": [
            {
                "id": "input_v",
                "analysis_id": "edge",
                "quantity": {"kind": "node_voltage", "net": "IN"},
            },
            {
                "id": "output_v",
                "analysis_id": "edge",
                "quantity": {"kind": "node_voltage", "net": "OUT"},
            },
        ],
        "measurements": [
            {
                "id": "input_crossing",
                "analysis_id": "edge",
                "operation_profile": "openada.operation/result.measure/v1alpha2",
                "request": _crossing_request("input_crossing", "input_v", 0.9),
            },
            {
                "id": "output_crossing",
                "analysis_id": "edge",
                "operation_profile": "openada.operation/result.measure/v1alpha2",
                "request": _crossing_request("output_crossing", "output_v", 0.24),
            },
        ],
        "derivations": [
            {
                "id": "propagation_delay",
                "kind": "subtract",
                "analysis_id": "edge",
                "parents": ["output_crossing", "input_crossing"],
            }
        ],
        "conditions": {
            "pdk": {
                "id": "ihp-sg13g2",
                "corner": "mos_tt",
            }
        },
    }


def _acceptance_assets_available() -> bool:
    return (
        shutil.which("ngspice") is not None
        and (PDK_ROOT / "ihp-sg13g2").exists()
        and (SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json").is_file()
    )


def test_real_transient_crossings_produce_digest_bound_subtract_derivation(
    tmp_path: Path,
) -> None:
    if not _acceptance_assets_available():
        pytest.skip("the real IHP PDK and SourceFollower acceptance bundle are required")

    spec_path = tmp_path / "delay.experiment.json"
    spec_path.write_text(
        json.dumps(_derivation_spec(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "evidence"

    payload = run_experiment(
        spec_path,
        output_dir,
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        timeout=300.0,
    )

    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]
    manifest = payload["data"]["manifest"]
    assert manifest["status"] == "pass"
    assert [analysis["id"] for analysis in manifest["analyses"]] == ["edge"]
    assert manifest["analyses"][0]["status"] == "pass"

    measurements = {
        measurement["id"]: measurement
        for measurement in manifest["analyses"][0]["measurements"]
    }
    assert set(measurements) == {"input_crossing", "output_crossing"}
    measurement_digests = {
        measurement_id: measurement["result_sha256"]
        for measurement_id, measurement in measurements.items()
    }
    for measurement_id, measurement in measurements.items():
        assert measurement["engineering_status"] == "pass"
        assert measurement["unit"] == "s"
        result_path = Path(measurement["result_path"])
        assert result_path.is_file()
        assert (
            hashlib.sha256(result_path.read_bytes()).hexdigest()
            == measurement_digests[measurement_id]
        )

    assert len(manifest["derivations"]) == 1
    derivation = manifest["derivations"][0]
    assert derivation["id"] == "propagation_delay"
    assert derivation["unit"] == "s"
    assert math.isfinite(derivation["value"])
    assert {
        parent["measurement_id"]: parent["result_sha256"]
        for parent in derivation["parents"]
    } == measurement_digests

    retained = json.loads(
        Path(derivation["result_path"]).read_text(encoding="utf-8")
    )
    assert retained["kind"] == "derivation"
    assert retained["operation"] == "subtract"
    assert retained["status"] == "derived"
    assert retained["unit"] == "s"
    assert math.isfinite(retained["value"])
    assert {
        parent["measurement_id"]: parent["result_sha256"]
        for parent in retained["parents"]
    } == measurement_digests
