from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any


# The vendored bundle is the compiled form of simra's shipped
# plugins/schematic/examples/sized-source-follower.ord (deterministic
# compilation; the digests below pin it). OPENADA_DUT_BUNDLE overrides for
# cross-checking against a freshly compiled copy.
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

TRANSFER_METHOD = {
    "id": "openada.method/ac-complex-ratio-log-interpolation/v1alpha1",
    "ratio": "output-over-input",
    "phase_unwrap": "first-principal-then-nearest-delta",
    "first_phase_range": "[-180,180)",
    "interpolation": "linear-value-over-log10-frequency",
    "crossing_policy": "require-single-falling",
    "bandwidth_reference": "first-simulated-frequency-magnitude",
    "bandwidth_drop_db": 3.0,
    "phase_margin_definition": "180deg-plus-unwrapped-loop-phase-at-unity",
}


def gain_spec() -> dict[str, Any]:
    return {
        "schema": "simra.experiment/v1",
        "id": "sf_ac_gain",
        "dut": {
            "artifact": str(SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json"),
            "bundle": dict(SOURCE_FOLLOWER_DIGESTS),
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
                "kind": "vdc",
                "plus": "IN",
                "minus": "0",
                "parameters": {"dc": "0.9", "ac_mag": "1", "ac_phase": "0"},
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
                "id": "ac_gain",
                "kind": "ac",
                "sweep": "dec",
                "points": 20,
                "start": "1",
                "stop": "1e9",
                "ac_excitation": ["V_IN"],
            }
        ],
        "observations": [
            {
                "id": "in_real",
                "analysis_id": "ac_gain",
                "quantity": {"kind": "node_voltage", "net": "IN"},
                "component": "real",
            },
            {
                "id": "in_imaginary",
                "analysis_id": "ac_gain",
                "quantity": {"kind": "node_voltage", "net": "IN"},
                "component": "imaginary",
            },
            {
                "id": "out_real",
                "analysis_id": "ac_gain",
                "quantity": {"kind": "node_voltage", "net": "OUT"},
                "component": "real",
            },
            {
                "id": "out_imaginary",
                "analysis_id": "ac_gain",
                "quantity": {"kind": "node_voltage", "net": "OUT"},
                "component": "imaginary",
            },
        ],
        "measurements": [
            {
                "id": "low_frequency_gain",
                "analysis_id": "ac_gain",
                "operation_profile": (
                    "openada.operation/result.transfer.measure/v1alpha2"
                ),
                "request": {
                    "measurement_id": "low_frequency_gain",
                    "input": {
                        "real": "in_real",
                        "imaginary": "in_imaginary",
                    },
                    "output": {
                        "real": "out_real",
                        "imaginary": "out_imaginary",
                    },
                    "interpretation": "forward",
                    "method": dict(TRANSFER_METHOD),
                    "metric": {
                        "kind": "low_frequency_gain_db",
                        "unit": "dB",
                    },
                    "extensions": {},
                },
            }
        ],
        "derivations": [],
        "conditions": {
            "pdk": {
                "id": "ihp-sg13g2",
                "corner": "mos_tt",
            }
        },
    }


def acceptance_assets_available() -> bool:
    return (
        shutil.which("ngspice") is not None
        and (PDK_ROOT / "ihp-sg13g2").exists()
        and (SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json").is_file()
    )
