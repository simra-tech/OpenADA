"""Shared validation and setup helpers for pinned IHP ngspice conformance."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


HERE = Path(__file__).resolve().parent
SHARED_COMMON = HERE.parent / "ihp-inverter" / "common.py"
MANIFEST_SCHEMA = "openada.ngspice-conformance/v0alpha1"
CONFORMANCE_ID = "ihp-inverter-xschem-ngspice-deck-output"
DESIGN_REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
PDK_REVISION = "144f811cdffda49b71d28f64e8a92b697b61cf06"
IMAGE_DIGEST = "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"


def _load_shared() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_openada_ihp_shared_common", SHARED_COMMON)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared conformance helpers: {SHARED_COMMON}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()
ConformanceError = _shared.ConformanceError
ensure_external_cache = _shared.ensure_external_cache
ensure_external_design_path = _shared.ensure_external_design_path
require_mount_safe_path = _shared.require_mount_safe_path
run_checked = _shared.run_checked
sha256_file = _shared.sha256_file


def default_cache_dir() -> Path:
    """Reuse the DRC/LVS bundle's exact pinned design checkout."""

    return _shared.default_cache_dir()


def _expect_equal(actual: Any, expected: Any, location: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{location} must be {expected!r}")


def _require_keys(value: Any, expected: set[str], location: str, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append(f"{location} must be an object")
        return {}
    if set(value) != expected:
        errors.append(
            f"{location} keys differ; missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )
    return value


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Reject drift from the reviewed golden-path identities and semantics."""

    errors: list[str] = []
    _require_keys(
        manifest,
        {"schema", "id", "design", "runtime", "policy", "tools", "workflow", "waveform"},
        "manifest",
        errors,
    )
    _expect_equal(manifest.get("schema"), MANIFEST_SCHEMA, "schema", errors)
    _expect_equal(manifest.get("id"), CONFORMANCE_ID, "id", errors)

    design = _require_keys(
        manifest.get("design"), {"repository", "revision", "license"}, "design", errors
    )
    _expect_equal(
        design.get("repository"),
        "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git",
        "design.repository",
        errors,
    )
    _expect_equal(design.get("revision"), DESIGN_REVISION, "design.revision", errors)
    license_record = _require_keys(
        design.get("license"), {"spdx", "path", "sha256"}, "design.license", errors
    )
    for field, expected in {
        "spdx": "Apache-2.0",
        "path": "LICENSE",
        "sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    }.items():
        _expect_equal(license_record.get(field), expected, f"design.license.{field}", errors)

    runtime = _require_keys(
        manifest.get("runtime"), {"image", "pdk", "ngspice_system_init"}, "runtime", errors
    )
    image = _require_keys(
        runtime.get("image"),
        {"name", "tag", "manifest_digest", "config_digest", "reference", "platform"},
        "runtime.image",
        errors,
    )
    expected_image = {
        "name": "hpretl/iic-osic-tools",
        "tag": "2026.06",
        "manifest_digest": IMAGE_DIGEST,
        "config_digest": (
            "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
        ),
        "reference": f"hpretl/iic-osic-tools@{IMAGE_DIGEST}",
        "platform": "linux/amd64",
    }
    for field, expected in expected_image.items():
        _expect_equal(image.get(field), expected, f"runtime.image.{field}", errors)

    pdk = _require_keys(
        runtime.get("pdk"),
        {"name", "revision", "commit_file", "xschem_rcfile", "ngspice_init"},
        "runtime.pdk",
        errors,
    )
    _expect_equal(pdk.get("name"), "ihp-sg13g2", "runtime.pdk.name", errors)
    _expect_equal(pdk.get("revision"), PDK_REVISION, "runtime.pdk.revision", errors)
    expected_pdk_files = {
        "commit_file": (
            "/foss/pdks/ihp-sg13g2/COMMIT",
            "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5",
        ),
        "xschem_rcfile": (
            "/foss/pdks/ihp-sg13g2/libs.tech/xschem/xschemrc",
            "d6d8fa5157ad2072e6d1ce63bda5f5d593ef4eb84631f23eed5e9ae3886f18b5",
        ),
        "ngspice_init": (
            "/foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit",
            "56ec1880a943fa481c3c321d62857b6240387e39d5aa8ded403835c34edb515d",
        ),
    }
    for name, (path, digest) in expected_pdk_files.items():
        record = _require_keys(pdk.get(name), {"path", "sha256"}, f"runtime.pdk.{name}", errors)
        _expect_equal(record.get("path"), path, f"runtime.pdk.{name}.path", errors)
        _expect_equal(record.get("sha256"), digest, f"runtime.pdk.{name}.sha256", errors)
    system_init = _require_keys(
        runtime.get("ngspice_system_init"),
        {"path", "sha256"},
        "runtime.ngspice_system_init",
        errors,
    )
    _expect_equal(
        system_init.get("path"),
        "/foss/tools/ngspice/share/ngspice/scripts/spinit",
        "runtime.ngspice_system_init.path",
        errors,
    )
    _expect_equal(
        system_init.get("sha256"),
        "b088c11a27e21ceadb14abbf9dff877105177bd025ca37750877d71e7f6f87af",
        "runtime.ngspice_system_init.sha256",
        errors,
    )

    expected_policy = {
        "setup_network": "allowed for the pinned Git fetch and image pull only",
        "eda_network": "none",
        "openada_mount": "read-only",
        "design_mount": "read-only",
        "container_root": "read-only",
        "evidence_directory": "new and writable",
        "local_user_ngspice_init": "disabled with -n",
        "system_ngspice_init": "pinned and observed",
    }
    policy = _require_keys(manifest.get("policy"), set(expected_policy), "policy", errors)
    for field, expected in expected_policy.items():
        _expect_equal(policy.get(field), expected, f"policy.{field}", errors)

    tools = _require_keys(manifest.get("tools"), {"xschem", "ngspice"}, "tools", errors)
    expected_tools = {
        "xschem": ("/foss/tools/xschem/bin/xschem", "XSCHEM V3.4.8RC"),
        "ngspice": (
            "/foss/tools/ngspice/bin/ngspice",
            "** ngspice-46 : Circuit level simulation program",
        ),
    }
    for name, (path, version) in expected_tools.items():
        record = _require_keys(tools.get(name), {"path", "version"}, f"tools.{name}", errors)
        _expect_equal(record.get("path"), path, f"tools.{name}.path", errors)
        _expect_equal(record.get("version"), version, f"tools.{name}.version", errors)

    workflow = _require_keys(
        manifest.get("workflow"), {"netlist", "simulate"}, "workflow", errors
    )
    netlist = _require_keys(
        workflow.get("netlist"),
        {"result_filename", "container_timeout_seconds", "arguments", "inputs", "artifact", "expect"},
        "workflow.netlist",
        errors,
    )
    simulate = _require_keys(
        workflow.get("simulate"),
        {"result_filename", "container_timeout_seconds", "arguments", "inputs", "artifacts", "expect"},
        "workflow.simulate",
        errors,
    )
    _expect_equal(netlist.get("result_filename"), "netlist.json", "workflow.netlist.result_filename", errors)
    _expect_equal(simulate.get("result_filename"), "simulate.json", "workflow.simulate.result_filename", errors)

    net_args = netlist.get("arguments") if isinstance(netlist.get("arguments"), dict) else {}
    sim_args = simulate.get("arguments") if isinstance(simulate.get("arguments"), dict) else {}
    expected_net_args = {
        "schematic": "/design/modules/module_0_foundations/inverter/inverter_tb.sch",
        "output": "/evidence/work/inverter_tb.spice",
        "rcfile": expected_pdk_files["xschem_rcfile"][0],
        "timeout_seconds": 120,
    }
    expected_sim_args = {
        "spice_file": "/evidence/work/inverter_tb.spice",
        "output_dir": "/evidence/simulation",
        "workdir": "/evidence/work",
        "execution_mode": "control",
        "expect_output": "raw=test_inverter.raw",
        "init_file": expected_pdk_files["ngspice_init"][0],
        "system_init_file": "/foss/tools/ngspice/share/ngspice/scripts/spinit",
        "timeout_seconds": 180,
    }
    _expect_equal(net_args, expected_net_args, "workflow.netlist.arguments", errors)
    _expect_equal(sim_args, expected_sim_args, "workflow.simulate.arguments", errors)

    expected_net_inputs = [
        {
            "path": expected_net_args["schematic"],
            "kind": "xschem-schematic",
            "role": "input",
            "sha256": "521464a42c5352cad371a8b091d71d9a083686749ef49c69b3f07ec838a3cb82",
        },
        {
            "path": expected_net_args["rcfile"],
            "kind": "xschem-rcfile",
            "role": "configuration",
            "sha256": expected_pdk_files["xschem_rcfile"][1],
        },
    ]
    expected_sim_inputs = [
        {
            "path": expected_sim_args["spice_file"],
            "kind": "spice-netlist",
            "role": "input",
        },
        {
            "path": expected_sim_args["init_file"],
            "kind": "ngspice-init",
            "role": "configuration",
            "sha256": expected_pdk_files["ngspice_init"][1],
        },
        {
            "path": expected_sim_args["system_init_file"],
            "kind": "ngspice-system-init",
            "role": "configuration",
            "sha256": (
                "b088c11a27e21ceadb14abbf9dff877105177bd025ca37750877d71e7f6f87af"
            ),
        },
    ]
    _expect_equal(netlist.get("inputs"), expected_net_inputs, "workflow.netlist.inputs", errors)
    _expect_equal(simulate.get("inputs"), expected_sim_inputs, "workflow.simulate.inputs", errors)
    _expect_equal(netlist.get("container_timeout_seconds"), 360, "workflow.netlist.container_timeout_seconds", errors)
    _expect_equal(simulate.get("container_timeout_seconds"), 360, "workflow.simulate.container_timeout_seconds", errors)
    _expect_equal(
        netlist.get("artifact"),
        {
            "path": "/evidence/work/inverter_tb.spice",
            "filename": "work/inverter_tb.spice",
            "kind": "spice-netlist",
            "role": "output",
        },
        "workflow.netlist.artifact",
        errors,
    )
    _expect_equal(
        simulate.get("artifacts"),
        [
            {
                "path": "/evidence/simulation/inverter_tb.log",
                "filename": "simulation/inverter_tb.log",
                "kind": "simulation-log",
                "role": "evidence",
            },
            {
                "path": "/evidence/simulation/inverter_tb.openada-control.sp",
                "filename": "simulation/inverter_tb.openada-control.sp",
                "kind": "ngspice-control-script",
                "role": "evidence",
            },
            {
                "path": "/evidence/work/test_inverter.raw",
                "filename": "work/test_inverter.raw",
                "kind": "ngspice-raw",
                "role": "output",
            },
        ],
        "workflow.simulate.artifacts",
        errors,
    )
    _expect_equal(
        netlist.get("expect"),
        {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "pass",
            "missing_symbol_count": 0,
        },
        "workflow.netlist.expect",
        errors,
    )
    _expect_equal(
        simulate.get("expect"),
        {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "pass",
            "converged": True,
            "output_origin": "deck",
            "output_status": "valid",
        },
        "workflow.simulate.expect",
        errors,
    )

    expected_waveform = {
        "plotname": "Transient Analysis",
        "flags": "real",
        "encoding": "binary",
        "acceptable_point_counts": [80, 81],
        "start_seconds": 0.0,
        "stop_seconds": 0.000002,
        "required_variables": ["time", "v(vdd)", "v(vin)", "v(vout)"],
        "vdd_min": 1.19,
        "vdd_max": 1.21,
        "settled_windows": [
            {
                "start_seconds": 0.0000002,
                "stop_seconds": 0.00000045,
                "vin_max": 0.05,
                "vout_min": 1.1,
            },
            {
                "start_seconds": 0.0000007,
                "stop_seconds": 0.0000013,
                "vin_min": 1.15,
                "vout_max": 0.1,
            },
            {
                "start_seconds": 0.0000017,
                "stop_seconds": 0.00000195,
                "vin_max": 0.05,
                "vout_min": 1.1,
            },
        ],
    }
    _expect_equal(manifest.get("waveform"), expected_waveform, "waveform", errors)

    if errors:
        raise ConformanceError("invalid ngspice conformance manifest:\n- " + "\n- ".join(errors))


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read conformance manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError("conformance manifest root must be an object")
    validate_manifest(document)
    return document


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Inspect the digest-pinned image and bind its exact config identity."""

    record = _shared.inspect_image(container_engine, manifest)
    expected = manifest["runtime"]["image"]["config_digest"]
    if record.get("Id") != expected:
        raise ConformanceError(
            f"local image config digest is {record.get('Id')!r}, expected {expected!r}"
        )
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    if not design_dir.is_dir() or not (design_dir / ".git").exists():
        raise ConformanceError(
            f"pinned design checkout is missing at {design_dir}; run setup.py first"
        )
    revision = manifest["design"]["revision"]
    head = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != revision:
        raise ConformanceError(f"design checkout is at {head}, expected {revision}")
    status = run_checked(
        ["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status:
        raise ConformanceError("design checkout has local changes; use a clean pinned checkout")

    expected = [manifest["design"]["license"], manifest["workflow"]["netlist"]["inputs"][0]]
    for record in expected:
        relative = record["path"].removeprefix("/design/")
        candidate = design_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"required regular design file is missing: {candidate}")
        actual = sha256_file(candidate)
        if actual != record["sha256"]:
            raise ConformanceError(
                f"design input hash mismatch for {candidate}: expected {record['sha256']}, got {actual}"
            )
    return head
