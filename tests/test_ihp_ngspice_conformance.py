from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "conformance" / "ihp-inverter-ngspice"
MANIFEST = BUNDLE / "manifest.json"
VERIFY = BUNDLE / "verify.py"
CONTROL_SCRIPT = (
    b"*ng_script_with_params\n"
    b"set noaskquit\n"
    b"source /foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit\n"
    b"source /evidence/work/inverter_tb.spice\n"
    b"quit\n"
)
SYNTHETIC_NETLIST = b"""* synthetic inverter
.control
save all
tran 50n 2u
write test_inverter.raw
.endc
.lib cornerMOSlv.lib mos_tt
.subckt inverter Vdd Vin Vout Gnd
XM1 Gnd Vin Vout Gnd sg13_lv_nmos w=1u l=0.45u
XM2 Vout Vin Vdd Vdd sg13_lv_pmos w=2u l=0.45u
.ends
.end
"""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _binary_raw(*, invert: bool = True) -> bytes:
    point_count = 80
    names = ["time", "v(vdd)", "v(vin)", "v(vout)"]
    header = (
        "Title: synthetic IHP inverter\n"
        "Date: fixture\n"
        "Command: ngspice-46 synthetic\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        f"No. Variables: {len(names)}\n"
        f"No. Points: {point_count}\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tv(vdd)\tvoltage\n"
        "\t2\tv(vin)\tvoltage\n"
        "\t3\tv(vout)\tvoltage\n"
        "Binary:\n"
    ).encode("ascii")
    values: list[float] = []
    for index in range(point_count):
        time = 2e-6 * index / (point_count - 1)
        vin = 1.2 if 0.5e-6 < time < 1.5e-6 else 0.0
        if invert:
            vout = 0.0 if vin > 0.6 else 1.2
        else:
            vout = vin
        values.extend((time, 1.2, vin, vout))
    return header + struct.pack(f"<{len(values)}d", *values)


def _file_record(path: str, kind: str, role: str, content: bytes) -> dict:
    return {
        "path": path,
        "kind": kind,
        "role": role,
        "exists": True,
        "bytes": len(content),
        "sha256": _sha256(content),
    }


def _provenance() -> dict:
    return {
        "openada_version": "0.1.0",
        "created_at": "2026-07-13T00:00:00Z",
        "host": {"system": "Linux", "machine": "x86_64", "python": "3.12.3"},
    }


def _netlist_result(manifest: dict, netlist: bytes) -> dict:
    operation = manifest["workflow"]["netlist"]
    arguments = operation["arguments"]
    tool = manifest["tools"]["xschem"]
    return {
        "schema": "openada.result/v0alpha1",
        "operation": "netlist",
        "tool": {"name": "xschem", "path": tool["path"], "version": tool["version"]},
        "execution": {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 1,
            "command": [
                tool["path"],
                "--rcfile",
                arguments["rcfile"],
                "-n",
                "-s",
                "-q",
                "-x",
                "-o",
                "/tmp/openada-xschem-synthetic",
                arguments["schematic"],
            ],
            "cwd": str(Path(arguments["schematic"]).parent),
        },
        "engineering": {"status": "pass", "summary": "synthetic netlist pass"},
        "inputs": [
            {
                "path": item["path"],
                "kind": item["kind"],
                "role": item["role"],
                "exists": True,
                "bytes": 1,
                "sha256": item["sha256"],
            }
            for item in operation["inputs"]
        ],
        "artifacts": [
            _file_record(
                operation["artifact"]["path"],
                operation["artifact"]["kind"],
                operation["artifact"]["role"],
                netlist,
            )
        ],
        "diagnostics": [],
        "data": {
            "stdout_tail": "",
            "stderr_tail": "",
            "missing_symbol_count": 0,
            "missing_symbols": [],
            "missing_symbols_truncated": False,
        },
        "provenance": _provenance(),
    }


def _simulate_result(
    manifest: dict, netlist: bytes, log: bytes, raw: bytes
) -> dict:
    operation = manifest["workflow"]["simulate"]
    arguments = operation["arguments"]
    tool = manifest["tools"]["ngspice"]
    artifacts = {
        item["path"]: item for item in operation["artifacts"]
    }
    raw_path = "/evidence/work/test_inverter.raw"
    script_path = "/evidence/simulation/inverter_tb.openada-control.sp"
    log_path = "/evidence/simulation/inverter_tb.log"
    raw_sha = _sha256(raw)
    script_sha = _sha256(CONTROL_SCRIPT)
    log_sha = _sha256(log)
    validation = {
        "valid": True,
        "reason": "valid",
        "metadata": {
            "format": "ngspice-raw",
            "bytes": len(raw),
            "plot_count": 1,
            "analysis_plot_count": 1,
            "has_analysis_plot": True,
            "value_count": 320,
            "numeric_scalar_count": 320,
            "plots": [
                {
                    "plotname": "Transient Analysis",
                    "encoding": "binary",
                    "numeric_type": "real",
                    "variables": 4,
                    "points": 80,
                    "values": 320,
                    "unpadded": False,
                }
            ],
        },
    }
    return {
        "schema": "openada.result/v0alpha1",
        "operation": "simulate",
        "tool": {"name": "ngspice", "path": tool["path"], "version": tool["version"]},
        "execution": {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 1,
            "command": [
                tool["path"],
                "-i",
                "-n",
                "-o",
                "/tmp/openada-ngspice-synthetic/simulation.log",
                script_path,
            ],
            "cwd": arguments["workdir"],
        },
        "engineering": {"status": "pass", "summary": "synthetic simulation pass"},
        "inputs": [
            _file_record(arguments["spice_file"], "spice-netlist", "input", netlist),
            {
                "path": arguments["init_file"],
                "kind": "ngspice-init",
                "role": "configuration",
                "exists": True,
                "bytes": 957,
                "sha256": manifest["runtime"]["pdk"]["ngspice_init"]["sha256"],
            },
            {
                "path": arguments["system_init_file"],
                "kind": "ngspice-system-init",
                "role": "configuration",
                "exists": True,
                "bytes": 1509,
                "sha256": manifest["runtime"]["ngspice_system_init"]["sha256"],
            },
        ],
        "artifacts": [
            _file_record(log_path, artifacts[log_path]["kind"], artifacts[log_path]["role"], log),
            _file_record(
                script_path,
                artifacts[script_path]["kind"],
                artifacts[script_path]["role"],
                CONTROL_SCRIPT,
            ),
            _file_record(raw_path, artifacts[raw_path]["kind"], artifacts[raw_path]["role"], raw),
        ],
        "diagnostics": [],
        "data": {
            "execution_mode": "control",
            "expected_outputs": [
                {"kind": "raw", "declared_path": "test_inverter.raw", "path": raw_path}
            ],
            "working_directory": arguments["workdir"],
            "working_directory_is_sandbox": False,
            "transitive_inputs_enumerated": False,
            "transitive_include_detected": True,
            "initialization": {
                "policy": "explicit",
                "file": arguments["init_file"],
                "local_user_spiceinit": "disabled",
                "system_spinit": {
                    "policy": "explicit",
                    "file": arguments["system_init_file"],
                },
                "ambient_startup_files_enumerated": True,
            },
            "environment": {
                "PDK": "ihp-sg13g2",
                "PDK_ROOT": "/foss/pdks",
                "SPICE_ASCIIRAWFILE": None,
                "SPICE_LIB_DIR": None,
                "SPICE_SCRIPTS": "/foss/tools/ngspice/share/ngspice/scripts",
                "NGSPICE_INPUT_DIR": None,
            },
            "environment_overrides": {
                "SPICE_SCRIPTS": "/foss/tools/ngspice/share/ngspice/scripts"
            },
            "converged": True,
            "inputs_stable": True,
            "measurements": [],
            "measurements_truncated": False,
            "missing_measurements": [],
            "duplicate_measurements": [],
            "measurement_section_count": 0,
            "solver_warning_count": 0,
            "solver_warning_examples": [],
            "solver_warning_examples_truncated": False,
            "log_tail": log.decode("utf-8"),
            "log_capture": {
                "path": log_path,
                "status": "valid",
                "bytes": len(log),
                "sha256": log_sha,
            },
            "output_captures": [
                {
                    "path": raw_path,
                    "status": "valid",
                    "bytes": len(raw),
                    "sha256": raw_sha,
                    "kind": "raw",
                    "origin": "deck",
                    "declared_path": "test_inverter.raw",
                    "parent_anchored": True,
                    "validation": validation,
                }
            ],
            "control_script_capture": {
                "path": script_path,
                "status": "valid",
                "bytes": len(CONTROL_SCRIPT),
                "sha256": script_sha,
            },
            "analysis_evidence": {"raw": True, "completed_log_record": True},
        },
        "provenance": _provenance(),
    }


def _container_command(manifest: dict) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        "openada-ihp-ngspice-123-0123abcd",
        "--pull=never",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--user",
        "1000:1000",
        "--env",
        "HOME=/tmp/openada-home",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PDK_ROOT=/foss/pdks",
        "--env",
        "PDK=ihp-sg13g2",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--workdir",
        "/evidence",
        "--mount",
        "type=bind,source=/synthetic/openada,target=/openada,readonly",
        "--mount",
        "type=bind,source=/synthetic/design,target=/design,readonly",
        "--mount",
        "type=bind,source=/synthetic/evidence,target=/evidence",
        "--entrypoint",
        "/usr/bin/python3",
        manifest["runtime"]["image"]["reference"],
        "/openada/conformance/ihp-inverter-ngspice/inside.py",
        "--manifest",
        "/openada/conformance/ihp-inverter-ngspice/manifest.json",
        "--evidence",
        "/evidence",
    ]


def _openada_invocations(manifest: dict) -> list[dict]:
    prefix = [
        "/usr/bin/python3",
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
    ]
    net = manifest["workflow"]["netlist"]["arguments"]
    sim = manifest["workflow"]["simulate"]["arguments"]
    return [
        {
            "operation": "netlist",
            "cwd": "/design/modules/module_0_foundations/inverter",
            "argv": [
                *prefix,
                "netlist",
                net["schematic"],
                "--output",
                net["output"],
                "--rcfile",
                net["rcfile"],
                "--timeout",
                str(net["timeout_seconds"]),
            ],
        },
        {
            "operation": "simulate",
            "cwd": "/evidence/work",
            "argv": [
                *prefix,
                "simulate",
                sim["spice_file"],
                "--output-dir",
                sim["output_dir"],
                "--workdir",
                sim["workdir"],
                "--execution-mode",
                sim["execution_mode"],
                "--expect-output",
                sim["expect_output"],
                "--init-file",
                sim["init_file"],
                "--system-init-file",
                sim["system_init_file"],
                "--timeout",
                str(sim["timeout_seconds"]),
            ],
        },
    ]


def _run_metadata(manifest: dict) -> dict:
    clean = {
        "commit": "0" * 40,
        "tracked_files_modified": False,
        "untracked_files_present": False,
        "working_tree_modified": False,
        "status_entry_count": 0,
        "status_sha256": hashlib.sha256(b"").hexdigest(),
    }
    pdk = manifest["runtime"]["pdk"]
    return {
        "schema": "openada.ngspice-conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "conformance_manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "created_at": "2026-07-13T00:00:00Z",
        "design_revision": manifest["design"]["revision"],
        "image": {
            "reference": manifest["runtime"]["image"]["reference"],
            "id": manifest["runtime"]["image"]["config_digest"],
            "os": "linux",
            "architecture": "amd64",
        },
        "openada_checkout": {
            "before": clean,
            "after": dict(clean),
            "state_unchanged": True,
            "commit_exact": True,
        },
        "network": "none during EDA execution",
        "container_command": _container_command(manifest),
        "runtime_observation": {
            "schema": "openada.ngspice-container-observation/v0alpha1",
            "pdk": {
                "name": pdk["name"],
                "revision": pdk["revision"],
                "commit_file": {
                    **pdk["commit_file"],
                    "bytes": 41,
                    "value": pdk["revision"],
                },
                "xschem_rcfile": {**pdk["xschem_rcfile"], "bytes": 1000},
                "ngspice_init": {**pdk["ngspice_init"], "bytes": 957},
            },
            "ngspice_system_init": {
                **manifest["runtime"]["ngspice_system_init"],
                "bytes": 1509,
            },
            "openada_invocations": _openada_invocations(manifest),
            "completed_operations": ["netlist", "simulate"],
        },
    }


def _write_evidence(evidence: Path, *, invert: bool = True) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    work = evidence / "work"
    simulation = evidence / "simulation"
    work.mkdir(parents=True)
    simulation.mkdir()
    netlist = SYNTHETIC_NETLIST
    raw = _binary_raw(invert=invert)
    log = b"No. of Data Rows : 80\nbinary raw file \"test_inverter.raw\"\nngspice-46 done\n"
    (work / "inverter_tb.spice").write_bytes(netlist)
    (work / "test_inverter.raw").write_bytes(raw)
    (simulation / "inverter_tb.log").write_bytes(log)
    (simulation / "inverter_tb.openada-control.sp").write_bytes(CONTROL_SCRIPT)
    (evidence / "netlist.json").write_text(json.dumps(_netlist_result(manifest, netlist)))
    (evidence / "simulate.json").write_text(
        json.dumps(_simulate_result(manifest, netlist, log, raw))
    )
    (evidence / "run.json").write_text(json.dumps(_run_metadata(manifest)))
    return manifest


def _verify(evidence: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY), str(evidence)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_pinned_ihp_ngspice_manifest_and_synthetic_binary_raw(tmp_path: Path) -> None:
    manifest_only = subprocess.run(
        [sys.executable, str(VERIFY), "--manifest-only"],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert manifest_only.returncode == 0, manifest_only.stderr
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)

    verified = _verify(evidence)

    assert verified.returncode == 0, verified.stderr


def test_verifier_independently_rejects_non_inverting_waveform(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    manifest = _write_evidence(evidence, invert=False)
    raw = (evidence / "work/test_inverter.raw").read_bytes()
    result_path = evidence / "simulate.json"
    result = json.loads(result_path.read_text())
    raw_path = manifest["workflow"]["simulate"]["artifacts"][2]["path"]
    artifact = next(item for item in result["artifacts"] if item["path"] == raw_path)
    artifact["sha256"] = _sha256(raw)
    artifact["bytes"] = len(raw)
    capture = result["data"]["output_captures"][0]
    capture["sha256"] = _sha256(raw)
    capture["bytes"] = len(raw)
    result_path.write_text(json.dumps(result))

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "settled inversion window" in verified.stderr


def test_verifier_rejects_raw_hash_tampering(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    raw_path = evidence / "work/test_inverter.raw"
    raw_path.write_bytes(raw_path.read_bytes() + b"tamper")

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "bytes" in verified.stderr or "sha256" in verified.stderr


def test_verifier_rejects_required_netlist_text_hidden_in_comments(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    manifest = _write_evidence(evidence)
    commented = b"\n".join(
        b"* " + line
        for line in (
            b".control",
            b"write test_inverter.raw",
            b".endc",
            b".lib cornerMOSlv.lib mos_tt",
            b".subckt inverter Vdd Vin Vout Gnd",
            b"XM1 Gnd Vin Vout Gnd sg13_lv_nmos",
            b"XM2 Vout Vin Vdd Vdd sg13_lv_pmos",
        )
    ) + b"\n.end\n"
    netlist_path = evidence / "work/inverter_tb.spice"
    netlist_path.write_bytes(commented)

    netlist_result_path = evidence / "netlist.json"
    netlist_result = json.loads(netlist_result_path.read_text())
    artifact = netlist_result["artifacts"][0]
    artifact["bytes"] = len(commented)
    artifact["sha256"] = _sha256(commented)
    netlist_result_path.write_text(json.dumps(netlist_result))

    simulation_result_path = evidence / "simulate.json"
    simulation_result = json.loads(simulation_result_path.read_text())
    source_path = manifest["workflow"]["simulate"]["arguments"]["spice_file"]
    source = next(item for item in simulation_result["inputs"] if item["path"] == source_path)
    source["bytes"] = len(commented)
    source["sha256"] = _sha256(commented)
    simulation_result_path.write_text(json.dumps(simulation_result))

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "active inverter/deck records" in verified.stderr


def test_verifier_rejects_orphan_write_between_control_blocks(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    manifest = _write_evidence(evidence)
    original = SYNTHETIC_NETLIST.decode("ascii")
    malformed = original.replace(
        ".control\nsave all\ntran 50n 2u\nwrite test_inverter.raw\n.endc",
        ".control\n.endc\nwrite test_inverter.raw\n.control\n.endc",
    ).encode("ascii")
    netlist_path = evidence / "work/inverter_tb.spice"
    netlist_path.write_bytes(malformed)

    netlist_result_path = evidence / "netlist.json"
    netlist_result = json.loads(netlist_result_path.read_text())
    netlist_result["artifacts"][0]["bytes"] = len(malformed)
    netlist_result["artifacts"][0]["sha256"] = _sha256(malformed)
    netlist_result_path.write_text(json.dumps(netlist_result))

    simulation_result_path = evidence / "simulate.json"
    simulation_result = json.loads(simulation_result_path.read_text())
    source_path = manifest["workflow"]["simulate"]["arguments"]["spice_file"]
    source = next(item for item in simulation_result["inputs"] if item["path"] == source_path)
    source["bytes"] = len(malformed)
    source["sha256"] = _sha256(malformed)
    simulation_result_path.write_text(json.dumps(simulation_result))

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "active control-block write" in verified.stderr


def test_verifier_rejects_pdk_and_command_identity_tampering(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    run_path = evidence / "run.json"
    run = json.loads(run_path.read_text())
    run["runtime_observation"]["pdk"]["ngspice_init"]["sha256"] = "f" * 64
    run_path.write_text(json.dumps(run))
    pdk_tamper = _verify(evidence)
    assert pdk_tamper.returncode == 1
    assert "ngspice_init.sha256" in pdk_tamper.stderr

    run = _run_metadata(json.loads(MANIFEST.read_text()))
    run["container_command"][9] = "bridge"
    run_path.write_text(json.dumps(run))
    command_tamper = _verify(evidence)
    assert command_tamper.returncode == 1
    assert "container_command" in command_tamper.stderr

    run = _run_metadata(json.loads(MANIFEST.read_text()))
    run["image"]["id"] = "sha256:" + ("f" * 64)
    run_path.write_text(json.dumps(run))
    image_tamper = _verify(evidence)
    assert image_tamper.returncode == 1
    assert "image.id" in image_tamper.stderr

    run = _run_metadata(json.loads(MANIFEST.read_text()))
    run["container_command"][32] = "type=volume,source=/synthetic/openada,target=/openada,readonly"
    run_path.write_text(json.dumps(run))
    mount_tamper = _verify(evidence)
    assert mount_tamper.returncode == 1
    assert "bind mount" in mount_tamper.stderr


def test_verifier_rejects_hardlinked_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    raw_path = evidence / "work/test_inverter.raw"
    external = tmp_path / "external.raw"
    external.write_bytes(raw_path.read_bytes())
    raw_path.unlink()
    raw_path.hardlink_to(external)

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "hard link" in verified.stderr


@pytest.mark.parametrize(
    "native_error",
    [
        "Error: no such vector bogus",
        "unknown subckt Xbad",
        "simulation interrupted due to error",
        "run simulation not started",
        "could not find a valid modelname",
    ],
)
def test_verifier_rejects_reconciled_native_log_error(
    tmp_path: Path, native_error: str
) -> None:
    evidence = tmp_path / "evidence"
    manifest = _write_evidence(evidence)
    log_path = evidence / "simulation/inverter_tb.log"
    log = log_path.read_bytes() + native_error.encode("ascii") + b"\n"
    log_path.write_bytes(log)
    result_path = evidence / "simulate.json"
    result = json.loads(result_path.read_text())
    expected_path = manifest["workflow"]["simulate"]["artifacts"][0]["path"]
    artifact = next(item for item in result["artifacts"] if item["path"] == expected_path)
    artifact["bytes"] = len(log)
    artifact["sha256"] = _sha256(log)
    result["data"]["log_capture"]["bytes"] = len(log)
    result["data"]["log_capture"]["sha256"] = _sha256(log)
    result["data"]["log_tail"] = log.decode("utf-8")[-4_000:]
    result_path.write_text(json.dumps(result))

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "native error evidence" in verified.stderr


def test_verifier_controls_incomplete_available_checkout_state(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    run_path = evidence / "run.json"
    run = json.loads(run_path.read_text())
    run["openada_checkout"]["before"]["status_entry_count"] = None
    run_path.write_text(json.dumps(run))

    verified = _verify(evidence)

    assert verified.returncode == 1
    assert "commit but incomplete Git state" in verified.stderr
    assert "Traceback" not in verified.stderr
