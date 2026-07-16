#!/usr/bin/env python3
"""Run the public CLI chain inside the pinned, network-disabled container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

from common import ConformanceError, load_manifest, sha256_file


CONTAINER_MANIFEST = Path(
    "/openada/conformance/ihp-inverter-agent-chain/manifest.json"
)
MAX_RESULT_BYTES = 16 * 1024 * 1024
PROVIDER_REQUEST_ID = "10000000-0000-4000-8000-000000000001"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the pinned agent evidence chain.")
    parser.add_argument("--manifest", type=Path, default=CONTAINER_MANIFEST)
    parser.add_argument("--evidence", type=Path, default=Path("/evidence"))
    return parser


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _regular_file_record(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat pinned runtime input {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise ConformanceError(f"pinned runtime input is not a nonempty regular file: {path}")
    return {
        "path": str(path),
        "bytes": metadata.st_size,
        "sha256": sha256_file(path),
    }


def _assert_pinned_file(record: dict[str, Any], expected: dict[str, str], label: str) -> None:
    if record["path"] != expected["path"] or record["sha256"] != expected["sha256"]:
        raise ConformanceError(
            f"{label} identity differs: expected {expected!r}, observed {record!r}"
        )


def _invoke(
    argv: list[str],
    result_path: Path,
    *,
    cwd: Path,
    allowed_returncodes: set[int] = frozenset({0}),
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PWD"] = str(cwd)
    path_entries = environment.get("PATH", "").split(os.pathsep)
    if "/openada/bin" not in path_entries:
        environment["PATH"] = os.pathsep.join(["/openada/bin", *path_entries])
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConformanceError(f"cannot complete OpenADA invocation {argv!r}: {exc}") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        raise ConformanceError("OpenADA result exceeded the inside-runner size bound")
    if completed.stderr:
        raise ConformanceError(
            f"OpenADA invocation emitted ambient stderr: {completed.stderr[-4_000:]!r}"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"OpenADA returned non-JSON output: {exc}; stdout={completed.stdout[-1_000:]!r}"
        ) from exc
    if not isinstance(document, dict):
        raise ConformanceError("OpenADA result root must be one JSON object")
    _write_json(result_path, document)
    if completed.returncode not in allowed_returncodes:
        raise ConformanceError(
            f"OpenADA invocation exited with code {completed.returncode}; "
            f"result retained at {result_path}"
        )
    return document


def _file_identity(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def _provider_configuration(runtime: dict[str, Any]) -> dict[str, Any]:
    pins = runtime["extensions"]["org.openada"]
    return {
        "schema": "openada.ngspice-provider-config/v0alpha1",
        "init_file": dict(pins["pdk"]["ngspice_init"]),
        "system_init_file": dict(pins["ngspice_system_init"]),
        "environment": {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
        "extensions": {},
    }


def _provider_request(
    manifest: dict[str, Any],
    deck: Path,
    configuration: Path,
    *,
    request_id: str = PROVIDER_REQUEST_ID,
    analysis: dict[str, Any] | None = None,
    evidence_destination: str | None = None,
) -> dict[str, Any]:
    details = manifest["extensions"]["org.openada"]
    provider = details["provider"]
    pdk_commit = manifest["runtime"]["extensions"]["org.openada"]["pdk"][
        "commit_file"
    ]
    return {
        "schema": "openada.request/v0alpha1",
        "request_id": request_id,
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "target": {
            "kind": "testbench",
            "locator": {
                "type": "filesystem",
                **_file_identity(deck),
                "extensions": {},
            },
            "extensions": {},
        },
        "configuration": [
            {
                "role": "simulator-configuration",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    **_file_identity(configuration),
                    "extensions": {},
                },
                "extensions": {},
            },
            {
                "role": "pdk",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    "path": pdk_commit["path"],
                    "sha256": pdk_commit["sha256"],
                    "extensions": {},
                },
                "extensions": {},
            },
        ],
        "parameters": {
            "analysis": dict(analysis if analysis is not None else provider["analysis"]),
            "extensions": {},
        },
        "evidence_policy": {
            "required_artifact_roles": ["simulation.result", "simulation.log"],
            "retain_native_artifacts": True,
            "retain_native_logs": True,
            "provenance": "bounded",
            "identity_requirement": "content-digest",
            "extensions": {},
        },
        "evidence_destination": {
            "locator": {
                "type": "filesystem",
                "path": evidence_destination or provider["evidence_destination"],
                "extensions": {},
            },
            "collision_policy": "fail-if-present",
            "extensions": {},
        },
        "execution_constraints": {
            "completion": "wait",
            "timeout_ms": 180000,
            "max_log_bytes": 16777216,
            "max_artifact_bytes": 268435456,
            "side_effects": "evidence-only",
            "extensions": {},
        },
        "driver_selector": {
            "driver_id": provider["driver_id"],
            "driver_version": provider["driver_version"],
            "transport_id": provider["transport_id"],
            "required_features": [
                "openada.feature/simulation.analysis.tran/v1alpha1"
            ],
            "extensions": {},
        },
        "extensions": {},
    }


def _terminal_nonconvergence_deck(source: Path, destination: Path) -> None:
    """Materialize the reviewed isolated ngspice engine-boundary injection."""

    text = source.read_text(encoding="utf-8", errors="strict")
    records = (
        "I_PROBE 0 __openada_fail pulse -250m 250m 100u 10u 10u 90u 200u\n"
        "R_PROBE __openada_fail 0 1K\n"
        "B_PROBE_1 __openada_fail 0 I=V(__openada_fail,0)*max(1u,min(1,100*(V(__openada_fail,0)-10)))\n"
        "B_PROBE_2 0 __openada_fail I=V(0,__openada_fail)*max(1u,min(1,100*(V(0,__openada_fail)-10)))\n"
    )
    if hashlib.sha256(records.encode("ascii")).hexdigest() != (
        "f32de2d66578ae881b789d91681724a8050380a637013219d4020007b280c4dd"
    ):
        raise ConformanceError("internal terminal-failure injection identity drifted")
    if re.search(r"\b__openada_fail\b", text, re.IGNORECASE):
        raise ConformanceError("generated DUT deck already uses the reserved injection node")
    text, tran_count = re.subn(
        r"(?im)^(\s*)tran\s+50n\s+2u\s*$",
        r"\1tran 1u 1m",
        text,
    )
    text, write_count = re.subn(
        r"(?im)^(\s*)write\s+test_inverter\.raw\s*$",
        r"\1write test_inverter_fail.raw",
        text,
    )
    match = re.search(r"(?im)^\s*\.control\s*$", text)
    if tran_count != 1 or write_count != 1 or match is None:
        raise ConformanceError("generated DUT deck cannot be deterministically transformed for terminal failure")
    transformed = (
        "** Deliberate ngspice engine-boundary injection; the inverter DUT remains unchanged.\n"
        + text[: match.start()]
        + records
        + text[match.start() :]
    )
    destination.write_text(transformed, encoding="utf-8")


def _missing_symbol_schematic(source: Path, destination: Path) -> None:
    """Create one isolated Xschem negative from the pinned real schematic."""

    text = source.read_text(encoding="utf-8", errors="strict")
    marker = (
        "C {__openada_missing_symbol__.sym} 620 -100 0 0 "
        "{name=x_openada_missing}\n"
    )
    if "__openada_missing_symbol__" in text:
        raise ConformanceError("pinned schematic already uses the reserved missing symbol")
    destination.write_text(text + marker, encoding="utf-8")


def _shared_profile_deck(
    source: Path,
    destination: Path,
    model_files: dict[str, Path],
    *,
    step_s: float,
    stop_s: float,
    terminal_nonconvergence: bool,
) -> None:
    """Flatten the pinned mos_tt model closure for the shared batch profile."""

    source_text = source.read_text(encoding="utf-8", errors="strict")
    corner_text = model_files["corner_moslv"].read_text(
        encoding="utf-8", errors="strict"
    )
    parameter_text = model_files["moslv_parameters"].read_text(
        encoding="utf-8", errors="strict"
    )
    module_text = model_files["moslv_modules"].read_text(
        encoding="utf-8", errors="strict"
    )

    corner_match = re.search(
        r"(?ims)^\s*\.LIB\s+mos_tt\s*$\n(?P<body>.*?)"
        r"^\s*\.include\s+sg13g2_moslv_mod\.lib\s*$\n"
        r"^\s*\.ENDL\s+mos_tt\s*$",
        corner_text,
    )
    if corner_match is None:
        raise ConformanceError("pinned mos_tt corner cannot be extracted exactly")
    corner_body = corner_match.group("body")

    parameter_embedding = (
        "** begin inlined pinned sg13g2_moslv_parm.lib\n"
        + parameter_text.rstrip()
        + "\n** end inlined pinned sg13g2_moslv_parm.lib\n"
    )
    flattened_modules, include_count = re.subn(
        r"(?im)^\s*\.include\s+sg13g2_moslv_parm\.lib\s*\n",
        parameter_embedding,
        module_text,
    )
    if include_count != 4:
        raise ConformanceError(
            "pinned MOS module closure no longer has four reviewed parameter includes"
        )
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib)\b", parameter_text):
        raise ConformanceError("pinned MOS parameter file gained a transitive include")
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib)\b", flattened_modules):
        raise ConformanceError("flattened MOS module closure still contains an include")

    without_control, control_count = re.subn(
        r"(?ims)^\s*\.control\s*$.*?^\s*\.endc\s*$\n?",
        "",
        source_text,
    )
    without_library, library_count = re.subn(
        r"(?im)^\s*\.lib\s+cornerMOSlv\.lib\s+mos_tt\s*$\n?",
        "",
        without_control,
    )
    if control_count != 1 or library_count != 1:
        raise ConformanceError(
            "generated control deck cannot be mapped to the reviewed shared profile"
        )
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib|control)\b", without_library):
        raise ConformanceError("generated DUT contains an unreviewed transitive directive")

    injection = ""
    if terminal_nonconvergence:
        injection = (
            "** Deliberate isolated ngspice engine-boundary injection.\n"
            "I_PROBE 0 __openada_fail pulse -250m 250m 100u 10u 10u 90u 200u\n"
            "R_PROBE __openada_fail 0 1K\n"
            "B_PROBE_1 __openada_fail 0 "
            "I=V(__openada_fail,0)*max(1u,min(1,100*(V(__openada_fail,0)-10)))\n"
            "B_PROBE_2 0 __openada_fail "
            "I=V(0,__openada_fail)*max(1u,min(1,100*(V(0,__openada_fail)-10)))\n"
        )
        records = injection.split("\n", 1)[1]
        if hashlib.sha256(records.encode("ascii")).hexdigest() != (
            "f32de2d66578ae881b789d91681724a8050380a637013219d4020007b280c4dd"
        ):
            raise ConformanceError("internal shared-profile failure injection drifted")
        if re.search(r"\b__openada_fail\b", without_library, re.IGNORECASE):
            raise ConformanceError("generated DUT already uses the reserved injection node")

    closure = (
        "\n** OpenADA shared-profile flattening of pinned IHP mos_tt model closure.\n"
        + corner_body
        + "\n"
        + flattened_modules.rstrip()
        + "\n"
        + injection
        + f".tran {step_s:.17g} {stop_s:.17g}\n"
    )
    transformed, end_count = re.subn(
        r"(?im)^\s*\.end\s*$", closure + ".end", without_library
    )
    if end_count != 1:
        raise ConformanceError("generated DUT does not have one unambiguous top-level .end")
    destination.write_text(transformed, encoding="utf-8")


def _builtin_simulate_argv(
    prefix: list[str],
    *,
    deck: str,
    output_dir: str,
    workdir: str,
    step_s: float,
    stop_s: float,
) -> list[str]:
    return [
        *prefix,
        "simulate",
        deck,
        "--backend",
        "ngspice",
        "--analysis",
        "tran",
        "--step-s",
        f"{step_s:.17g}",
        "--stop-s",
        f"{stop_s:.17g}",
        "--output-dir",
        output_dir,
        "--workdir",
        workdir,
        "--timeout",
        "180",
    ]


def _specification(
    measurement: dict[str, Any],
    conditions: list[dict[str, Any]],
    limits: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    request = measurement["request"]
    return {
        "specification_id": f"{request['measurement_id']}.{decision}-limit",
        "measurement_id": request["measurement_id"],
        "limits": limits,
        "conditions": conditions,
        "extensions": {},
    }


def _correlation(prefix: str, index: int) -> str:
    return f"{prefix}000000-0000-4000-8000-{index:012d}"


def _negative_measurement(definition: dict[str, Any]) -> tuple[dict[str, Any], str, str, int]:
    """Return one honest, kind-specific public-CLI negative replay."""

    request = json.loads(json.dumps(definition["request"]))
    kind = request["kind"]
    if kind == "sample_at":
        request["parameters"] = {
            "at": {"value": 3e-6, "unit": "s"},
            "interpolation": "linear",
        }
        return request, "unknown", "measurement.domain.invalid", 2
    if kind in {"minimum", "maximum", "mean", "rms"}:
        request["parameters"] = {
            "window": {
                "start": {"value": 3e-6, "unit": "s"},
                "stop": {"value": 4e-6, "unit": "s"},
            }
        }
        return request, "fail", "measurement.value.not_found", 1
    if kind == "crossing":
        request["parameters"] = {
            "threshold": {"value": 2.0, "unit": "V"},
            "direction": "rising",
            "occurrence": 1,
        }
        return request, "fail", "measurement.value.not_found", 1
    if kind in {"rise_time", "fall_time"}:
        request["parameters"] = {
            "lower_threshold": {"value": 2.0, "unit": "V"},
            "upper_threshold": {"value": 3.0, "unit": "V"},
            "occurrence": 1,
        }
        return request, "fail", "measurement.value.not_found", 1
    if kind == "settling_time":
        request["parameters"] = {
            "target": {"value": 10.0, "unit": "V"},
            "tolerance": {"value": 0.01, "unit": "V"},
            "reference": {"value": 1.5e-6, "unit": "s"},
            "hold_for": {"value": 2e-7, "unit": "s"},
        }
        return request, "fail", "measurement.value.not_found", 1
    raise ConformanceError(f"no reviewed negative replay exists for measurement kind {kind!r}")


def _build_agent_evidence(
    manifest: dict[str, Any],
    netlist_negative: dict[str, Any],
    provider_result: dict[str, Any],
    terminal_result: dict[str, Any],
    builtin_result: dict[str, Any],
    builtin_terminal_result: dict[str, Any],
    extraction: dict[str, Any],
    measurement_results: list[tuple[dict[str, Any], dict[str, Any]]],
    evaluations: list[dict[str, Any]],
    negative_results: list[dict[str, Any]],
) -> dict[str, Any]:
    extracted = extraction["data"]["extraction"]
    series = extracted["series"]
    raw = extracted["source"]["artifact"]
    terminal_raw = next(
        item
        for item in terminal_result["artifacts"]
        if item.get("role") == "simulation.result"
    )
    builtin_raw = next(
        item for item in builtin_result["artifacts"] if item.get("role") == "simulation.result"
    )
    builtin_terminal_raw = next(
        item
        for item in builtin_terminal_result["artifacts"]
        if item.get("role") == "simulation.result"
    )
    metrics: list[dict[str, Any]] = []
    for definition, result in measurement_results:
        measured = result["data"]["measurement"]
        metrics.append(
            {
                "id": definition["id"],
                "kind": measured["kind"],
                "signal": measured["signal"],
                "status": measured["status"],
                "value": measured["value"],
                "unit": measured["unit"],
                "location": measured["location"],
                "algorithm": measured["algorithm"],
                "measurement_request_sha256": measured["request_sha256"],
                "result_sha256": hashlib.sha256(
                    json.dumps(
                        result,
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                "interpretation": definition["interpretation"],
            }
        )
    decisions = []
    for result in evaluations:
        evaluation = result["data"]["evaluation"]
        decisions.append(
            {
                "specification_id": evaluation["specification_id"],
                "measurement_id": evaluation["measurement_id"],
                "status": evaluation["status"],
                "measured": evaluation["measured"],
                "limits": evaluation["limits"],
                "margin": evaluation["margin"],
                "conditions": evaluation["conditions"],
            }
        )
    return {
        "schema": "openada.agent-evidence/v0alpha1",
        "chain_id": manifest["id"],
        "provider": {
            "request_id": provider_result["data"]["protocol"]["request_id"],
            "driver_id": provider_result["data"]["protocol"]["driver_id"],
            "driver_version": provider_result["data"]["protocol"]["driver_version"],
            "engineering_status": provider_result["engineering"]["status"],
        },
        "netlist_negative": {
            "engineering_status": netlist_negative["engineering"]["status"],
            "diagnostic_codes": [item["code"] for item in netlist_negative["diagnostics"]],
            "missing_symbol_count": netlist_negative["data"]["missing_symbol_count"],
            "artifact": netlist_negative["artifacts"][0],
        },
        "builtin_ngspice": {
            "pass": {
                "driver_id": builtin_result["data"]["protocol"]["driver_id"],
                "driver_version": builtin_result["data"]["protocol"]["driver_version"],
                "engineering_status": builtin_result["engineering"]["status"],
                "native_artifact": builtin_raw,
            },
            "terminal_nonconvergence": {
                "engineering_status": builtin_terminal_result["engineering"]["status"],
                "diagnostic_codes": [
                    item["code"] for item in builtin_terminal_result["diagnostics"]
                ],
                "native_artifact": builtin_terminal_raw,
            },
        },
        "terminal_nonconvergence": {
            "purpose": manifest["extensions"]["org.openada"]["terminal_nonconvergence"]["purpose"],
            "engineering_status": terminal_result["engineering"]["status"],
            "diagnostic_codes": [item["code"] for item in terminal_result["diagnostics"]],
            "native_artifact": {
                "path": terminal_raw["path"],
                "bytes": terminal_raw["bytes"],
                "sha256": terminal_raw["sha256"],
            },
        },
        "native_artifact": {
            "path": raw["path"],
            "bytes": raw["bytes"],
            "sha256": raw["sha256"],
            "role": raw["role"],
            "kind": raw["kind"],
        },
        "series": {
            "request_id": series["source"]["request_id"],
            "sha256": series["source"]["artifact_sha256"],
            "axis": series["axis"]["name"],
            "axis_unit": series["axis"]["unit"],
            "point_count": len(series["axis"]["values"]),
            "signals": [item["name"] for item in series["signals"]],
            "conditions": series["conditions"],
        },
        "measurements": metrics,
        "specifications": {
            "pass_count": sum(item["status"] == "pass" for item in decisions),
            "fail_count": sum(item["status"] == "fail" for item in decisions),
            "decisions": decisions,
        },
        "negative_replays": negative_results,
        "limitations": [
            "This is preview evidence, not foundry signoff.",
            "The IHP mos_tt model closure and PSP103 OSDI module are individually content-bound and retained for independent reconstruction.",
            "The shared CLI result reports native-default startup as unenumerated; the isolated startup and OSDI bytes are independently verified by this chain.",
            "Mean and RMS are arithmetic statistics over retained adaptive samples, not time-weighted electrical measurements.",
            "The normalized measurement profile intentionally labels native-artifact lineage unverified downstream; this verifier independently checks that edge.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation: dict[str, Any] = {
        "schema": "openada.ihp-agent-chain-container-observation/v0alpha1",
        "runtime_inputs": {},
        "openada_invocations": [],
        "completed_operations": [],
    }
    try:
        manifest = load_manifest(args.manifest.resolve())
        evidence = args.evidence.resolve()
        if evidence != Path("/evidence"):
            raise ConformanceError("inside runner requires the reviewed /evidence mount target")
        if Path(os.environ.get("HOME", "")) != Path("/tmp/openada-home"):
            raise ConformanceError("inside runner requires HOME=/tmp/openada-home")
        home = Path("/tmp/openada-home")
        home.mkdir(mode=0o700)
        if home.is_symlink() or stat.S_IMODE(home.lstat().st_mode) != 0o700:
            raise ConformanceError("isolated tool HOME is not a mode-0700 directory")
        builtin_details = manifest["extensions"]["org.openada"]["builtin_ngspice"]
        isolated_startup = builtin_details["isolated_startup"]
        startup_path = Path(isolated_startup["path"])
        if startup_path != home / ".spiceinit":
            raise ConformanceError("shared-profile startup must be isolated under tool HOME")
        startup_path.write_text(isolated_startup["content"], encoding="ascii")
        if sha256_file(startup_path) != isolated_startup["sha256"]:
            raise ConformanceError("isolated shared-profile startup identity drifted")
        work = evidence / "work"
        requests = evidence / "requests"
        measurements_dir = evidence / "measurements"
        specifications_dir = evidence / "specifications"
        negative_dir = evidence / "negative"
        retained_runtime = evidence / "runtime"
        builtin_work = evidence / "builtin/work"
        builtin_fail_work = evidence / "builtin-fail/work"
        for directory in (
            work,
            requests,
            measurements_dir,
            specifications_dir,
            negative_dir,
            retained_runtime,
            builtin_work,
            builtin_fail_work,
        ):
            directory.mkdir(mode=0o700, parents=True)

        runtime_pins = manifest["runtime"]["extensions"]["org.openada"]
        pdk = runtime_pins["pdk"]
        records = {
            "pdk_commit": _regular_file_record(Path(pdk["commit_file"]["path"])),
            "xschem_rcfile": _regular_file_record(Path(pdk["xschem_rcfile"]["path"])),
            "ngspice_init": _regular_file_record(Path(pdk["ngspice_init"]["path"])),
            "ngspice_system_init": _regular_file_record(
                Path(runtime_pins["ngspice_system_init"]["path"])
            ),
            "ngspice_executable": _regular_file_record(
                Path(runtime_pins["ngspice_executable"]["path"])
            ),
            "corner_moslv": _regular_file_record(
                Path(runtime_pins["model_files"]["corner_moslv"]["path"])
            ),
            "moslv_modules": _regular_file_record(
                Path(runtime_pins["model_files"]["moslv_modules"]["path"])
            ),
            "moslv_parameters": _regular_file_record(
                Path(runtime_pins["model_files"]["moslv_parameters"]["path"])
            ),
            "psp103_osdi": _regular_file_record(
                Path(runtime_pins["psp103_osdi"]["path"])
            ),
            "provider_manifest": _regular_file_record(
                Path(manifest["extensions"]["org.openada"]["provider"]["manifest_path"])
            ),
        }
        _assert_pinned_file(records["pdk_commit"], pdk["commit_file"], "PDK COMMIT")
        _assert_pinned_file(records["xschem_rcfile"], pdk["xschem_rcfile"], "Xschem rcfile")
        _assert_pinned_file(records["ngspice_init"], pdk["ngspice_init"], "ngspice init")
        _assert_pinned_file(
            records["ngspice_system_init"],
            runtime_pins["ngspice_system_init"],
            "ngspice system init",
        )
        _assert_pinned_file(
            records["ngspice_executable"],
            runtime_pins["ngspice_executable"],
            "ngspice executable",
        )
        for model_name in ("corner_moslv", "moslv_modules", "moslv_parameters"):
            _assert_pinned_file(
                records[model_name],
                runtime_pins["model_files"][model_name],
                f"IHP model file {model_name}",
            )
        _assert_pinned_file(
            records["psp103_osdi"],
            runtime_pins["psp103_osdi"],
            "IHP PSP103 OSDI module",
        )
        records["isolated_ngspice_startup"] = _regular_file_record(startup_path)
        commit_value = Path(pdk["commit_file"]["path"]).read_text(encoding="ascii").strip()
        if commit_value != manifest["runtime"]["pdk_revision"]:
            raise ConformanceError("PDK COMMIT content differs from the pinned revision")
        records["pdk_commit"]["value"] = commit_value
        observation["runtime_inputs"] = records
        for retained_name, runtime_name in (
            ("cornerMOSlv.lib", "corner_moslv"),
            ("sg13g2_moslv_mod.lib", "moslv_modules"),
            ("sg13g2_moslv_parm.lib", "moslv_parameters"),
        ):
            (retained_runtime / retained_name).write_bytes(
                Path(records[runtime_name]["path"]).read_bytes()
            )
        (retained_runtime / "psp103.osdi").write_bytes(
            Path(records["psp103_osdi"]["path"]).read_bytes()
        )
        (retained_runtime / "shared.spiceinit").write_bytes(startup_path.read_bytes())

        python = "/usr/bin/python3"
        openada = "/openada/bin/openada"
        prefix = [python, openada, "--profile", "iic-osic-tools", "--compact"]
        workflow = manifest["extensions"]["org.openada"]["workflow"]

        netlist_argv = [
            *prefix,
            "netlist",
            workflow["schematic"],
            "--output",
            workflow["generated_deck"],
            "--rcfile",
            pdk["xschem_rcfile"]["path"],
            "--timeout",
            "120",
        ]
        netlist_cwd = Path(workflow["schematic"]).parent
        observation["openada_invocations"].append(
            {"operation": "netlist", "cwd": str(netlist_cwd), "argv": netlist_argv}
        )
        netlist_result = _invoke(
            netlist_argv, evidence / workflow["netlist_result"], cwd=netlist_cwd
        )
        if netlist_result.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("netlist did not report engineering pass")
        observation["completed_operations"].append("netlist")

        missing_schematic = work / "inverter_missing_symbol.sch"
        missing_deck = work / "inverter_missing_symbol.spice"
        local_symbol = work / "inverter.sym"
        local_symbol.write_bytes(
            (Path(workflow["schematic"]).parent / "inverter.sym").read_bytes()
        )
        (work / "inverter.sch").write_bytes(
            (Path(workflow["schematic"]).parent / "inverter.sch").read_bytes()
        )
        _missing_symbol_schematic(Path(workflow["schematic"]), missing_schematic)
        negative_netlist_argv = [
            *prefix,
            "netlist",
            str(missing_schematic),
            "--output",
            str(missing_deck),
            "--rcfile",
            pdk["xschem_rcfile"]["path"],
            "--timeout",
            "120",
        ]
        observation["openada_invocations"].append(
            {
                "operation": "netlist:missing-symbol",
                "cwd": "/evidence/work",
                "argv": negative_netlist_argv,
            }
        )
        netlist_negative = _invoke(
            negative_netlist_argv,
            negative_dir / "netlist-missing-symbol.json",
            cwd=work,
            allowed_returncodes={1},
        )
        if (
            netlist_negative.get("engineering", {}).get("status") != "fail"
            or "xschem.missing_symbol"
            not in {item.get("code") for item in netlist_negative.get("diagnostics", [])}
            or len(netlist_negative.get("artifacts", [])) != 1
        ):
            raise ConformanceError(
                "isolated Xschem missing symbol did not produce typed unusable-output evidence"
            )
        observation["completed_operations"].append("netlist:missing-symbol")

        config_path = evidence / workflow["provider_configuration"]
        _write_json(config_path, _provider_configuration(manifest["runtime"]))
        request_path = evidence / workflow["provider_request"]
        _write_json(
            request_path,
            _provider_request(manifest, Path(workflow["generated_deck"]), config_path),
        )
        provider = manifest["extensions"]["org.openada"]["provider"]
        provider_argv = [
            *prefix,
            "provider",
            "invoke",
            "--manifest",
            provider["manifest_path"],
            "--cwd",
            "/openada",
            str(request_path),
        ]
        observation["openada_invocations"].append(
            {"operation": "provider.invoke", "cwd": "/evidence", "argv": provider_argv}
        )
        provider_result = _invoke(
            provider_argv, evidence / workflow["provider_result"], cwd=evidence
        )
        if provider_result.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("external provider did not report engineering pass")
        observation["completed_operations"].append("provider.invoke")

        terminal = manifest["extensions"]["org.openada"]["terminal_nonconvergence"]
        terminal_deck = Path(terminal["generated_deck"])
        _terminal_nonconvergence_deck(
            Path(workflow["generated_deck"]), terminal_deck
        )
        terminal_request_path = evidence / terminal["provider_request"]
        _write_json(
            terminal_request_path,
            _provider_request(
                manifest,
                terminal_deck,
                config_path,
                request_id=terminal["request_id"],
                analysis=terminal["analysis"],
                evidence_destination=terminal["evidence_destination"],
            ),
        )
        terminal_argv = [
            *prefix,
            "provider",
            "invoke",
            "--manifest",
            provider["manifest_path"],
            "--cwd",
            "/openada",
            str(terminal_request_path),
        ]
        observation["openada_invocations"].append(
            {
                "operation": "provider.invoke:terminal-nonconvergence",
                "cwd": "/evidence",
                "argv": terminal_argv,
            }
        )
        terminal_result = _invoke(
            terminal_argv,
            evidence / terminal["provider_result"],
            cwd=evidence,
            allowed_returncodes={1},
        )
        if terminal_result.get("engineering", {}).get("status") != "fail":
            raise ConformanceError(
                "isolated engine-boundary injection did not produce typed engineering fail"
            )
        terminal_codes = {
            item.get("code") for item in terminal_result.get("diagnostics", [])
        }
        if terminal["expected"]["diagnostic"] not in terminal_codes:
            raise ConformanceError(
                "terminal provider failure lacks the normalized nonconvergence diagnostic"
            )
        observation["completed_operations"].append(
            "provider.invoke:terminal-nonconvergence"
        )

        builtin = builtin_details
        shared_model_files = {
            name: Path(record["path"])
            for name, record in runtime_pins["model_files"].items()
        }
        shared_pass_deck = Path(builtin["pass"]["deck"])
        _shared_profile_deck(
            Path(workflow["generated_deck"]),
            shared_pass_deck,
            shared_model_files,
            step_s=provider["analysis"]["step_s"],
            stop_s=provider["analysis"]["stop_s"],
            terminal_nonconvergence=False,
        )
        builtin_pass_argv = _builtin_simulate_argv(
            prefix,
            deck=str(shared_pass_deck),
            output_dir=builtin["pass"]["output_dir"],
            workdir=builtin["pass"]["workdir"],
            step_s=provider["analysis"]["step_s"],
            stop_s=provider["analysis"]["stop_s"],
        )
        observation["openada_invocations"].append(
            {
                "operation": "simulate:shared-ngspice",
                "cwd": "/evidence",
                "argv": builtin_pass_argv,
            }
        )
        builtin_result = _invoke(
            builtin_pass_argv,
            evidence / builtin["pass"]["result"],
            cwd=evidence,
        )
        if builtin_result.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("built-in shared-ngspice path did not report pass")
        observation["completed_operations"].append("simulate:shared-ngspice")

        shared_terminal_deck = Path(builtin["terminal_fail"]["deck"])
        _shared_profile_deck(
            Path(workflow["generated_deck"]),
            shared_terminal_deck,
            shared_model_files,
            step_s=terminal["analysis"]["step_s"],
            stop_s=terminal["analysis"]["stop_s"],
            terminal_nonconvergence=True,
        )
        builtin_fail_argv = _builtin_simulate_argv(
            prefix,
            deck=str(shared_terminal_deck),
            output_dir=builtin["terminal_fail"]["output_dir"],
            workdir=builtin["terminal_fail"]["workdir"],
            step_s=terminal["analysis"]["step_s"],
            stop_s=terminal["analysis"]["stop_s"],
        )
        observation["openada_invocations"].append(
            {
                "operation": "simulate:shared-ngspice:terminal-nonconvergence",
                "cwd": "/evidence",
                "argv": builtin_fail_argv,
            }
        )
        builtin_terminal_result = _invoke(
            builtin_fail_argv,
            evidence / builtin["terminal_fail"]["result"],
            cwd=evidence,
            allowed_returncodes={1},
        )
        if (
            builtin_terminal_result.get("engineering", {}).get("status") != "fail"
            or terminal["expected"]["diagnostic"]
            not in {
                item.get("code")
                for item in builtin_terminal_result.get("diagnostics", [])
            }
        ):
            raise ConformanceError(
                "built-in shared-ngspice terminal replay did not produce typed fail"
            )
        observation["completed_operations"].append(
            "simulate:shared-ngspice:terminal-nonconvergence"
        )

        selection_path = evidence / workflow["extract_selection"]
        _write_json(
            selection_path,
            {
                "selectors": workflow["selectors"],
                "conditions": workflow["conditions"],
                "extensions": {},
            },
        )
        extract_argv = [
            *prefix,
            "extract",
            "--simulation",
            str(evidence / workflow["provider_result"]),
            "--artifact",
            provider["raw_artifact"],
            "--selection",
            str(selection_path),
            "--request-id",
            workflow["extract_request_id"],
        ]
        observation["openada_invocations"].append(
            {"operation": "result.series.extract", "cwd": "/evidence", "argv": extract_argv}
        )
        extraction = _invoke(
            extract_argv, evidence / workflow["extract_result"], cwd=evidence
        )
        if extraction.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("typed extraction did not report engineering pass")
        observation["completed_operations"].append("result.series.extract")

        missing_selection_path = requests / "extract-missing-selector.json"
        _write_json(
            missing_selection_path,
            {
                "selectors": [
                    {
                        "native_name": "v(__openada_missing__)",
                        "output_name": "v(__openada_missing__)",
                        "unit": "V",
                        "component": "real",
                    }
                ],
                "conditions": workflow["conditions"],
                "extensions": {},
            },
        )
        negative_extract_argv = [
            *prefix,
            "extract",
            "--simulation",
            str(evidence / workflow["provider_result"]),
            "--artifact",
            provider["raw_artifact"],
            "--selection",
            str(missing_selection_path),
            "--request-id",
            "15000000-0000-4000-8000-000000000001",
        ]
        observation["openada_invocations"].append(
            {
                "operation": "result.series.extract:missing-selector",
                "cwd": "/evidence",
                "argv": negative_extract_argv,
            }
        )
        negative_extract = _invoke(
            negative_extract_argv,
            negative_dir / "extract-missing-selector.json",
            cwd=evidence,
            allowed_returncodes={2},
        )
        if negative_extract.get("engineering", {}).get("status") != "unknown" or (
            "series.selector.missing"
            not in {item.get("code") for item in negative_extract.get("diagnostics", [])}
        ):
            raise ConformanceError("missing extraction selector did not produce the reviewed typed unknown")
        observation["completed_operations"].append(
            "result.series.extract:missing-selector"
        )

        measurement_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        evaluations: list[dict[str, Any]] = []
        negative_summaries: list[dict[str, Any]] = [
            {
                "id": "netlist-missing-symbol",
                "operation": "netlist",
                "engineering_status": "fail",
                "diagnostic": "xschem.missing_symbol",
            },
            {
                "id": "isolated-builtin-terminal-nonconvergence",
                "operation": "simulate",
                "engineering_status": "fail",
                "diagnostic": terminal["expected"]["diagnostic"],
            },
            {
                "id": "extract-missing-selector",
                "operation": "result.series.extract",
                "engineering_status": "unknown",
                "diagnostic": "series.selector.missing",
            },
            {
                "id": "isolated-terminal-nonconvergence",
                "operation": "simulate",
                "engineering_status": "fail",
                "diagnostic": terminal["expected"]["diagnostic"],
            },
        ]
        for index, definition in enumerate(
            manifest["extensions"]["org.openada"]["measurements"], start=1
        ):
            measurement_request = requests / f"measure-{definition['id']}.json"
            _write_json(measurement_request, definition["request"])
            measurement_result_path = measurements_dir / f"{definition['id']}.json"
            measure_argv = [
                *prefix,
                "measure",
                "--series",
                str(evidence / workflow["extract_result"]),
                "--measurement",
                str(measurement_request),
                "--request-id",
                definition["request_id"],
            ]
            observation["openada_invocations"].append(
                {"operation": f"result.measure:{definition['id']}", "cwd": "/evidence", "argv": measure_argv}
            )
            measured = _invoke(measure_argv, measurement_result_path, cwd=evidence)
            if measured.get("engineering", {}).get("status") != "pass":
                raise ConformanceError(
                    f"measurement {definition['id']!r} is not honestly observable from the native waveform; "
                    f"result retained at {measurement_result_path}"
                )
            measurement_results.append((definition, measured))
            observation["completed_operations"].append(
                f"result.measure:{definition['id']}"
            )

            negative_request, negative_status, negative_diagnostic, negative_exit = (
                _negative_measurement(definition)
            )
            negative_request_path = requests / f"measure-{definition['id']}-negative.json"
            _write_json(negative_request_path, negative_request)
            negative_result_path = negative_dir / f"measure-{definition['id']}.json"
            negative_measure_argv = [
                *prefix,
                "measure",
                "--series",
                str(evidence / workflow["extract_result"]),
                "--measurement",
                str(negative_request_path),
                "--request-id",
                _correlation("16", index),
            ]
            observation["openada_invocations"].append(
                {
                    "operation": f"result.measure:{definition['id']}:negative",
                    "cwd": "/evidence",
                    "argv": negative_measure_argv,
                }
            )
            negative_measured = _invoke(
                negative_measure_argv,
                negative_result_path,
                cwd=evidence,
                allowed_returncodes={negative_exit},
            )
            negative_codes = {
                item.get("code") for item in negative_measured.get("diagnostics", [])
            }
            if (
                negative_measured.get("engineering", {}).get("status") != negative_status
                or negative_diagnostic not in negative_codes
            ):
                raise ConformanceError(
                    f"negative {definition['id']!r} replay did not produce "
                    f"{negative_status!r}/{negative_diagnostic!r}"
                )
            negative_summaries.append(
                {
                    "id": f"measure-{definition['id']}",
                    "operation": "result.measure",
                    "engineering_status": negative_status,
                    "diagnostic": negative_diagnostic,
                }
            )
            observation["completed_operations"].append(
                f"result.measure:{definition['id']}:negative"
            )

            for decision, prefix_id, expected_code in (
                ("pass", "12", 0),
                ("fail", "13", 1),
            ):
                specification = _specification(
                    definition,
                    workflow["conditions"],
                    definition[f"{decision}_limits"],
                    decision,
                )
                specification_request = requests / f"spec-{definition['id']}-{decision}.json"
                _write_json(specification_request, specification)
                result_path = specifications_dir / f"{definition['id']}-{decision}.json"
                evaluate_argv = [
                    *prefix,
                    "evaluate",
                    "--measurement",
                    str(measurement_result_path),
                    "--specification",
                    str(specification_request),
                    "--request-id",
                    _correlation(prefix_id, index),
                ]
                observation["openada_invocations"].append(
                    {
                        "operation": f"specification.evaluate:{definition['id']}:{decision}",
                        "cwd": "/evidence",
                        "argv": evaluate_argv,
                    }
                )
                evaluated = _invoke(
                    evaluate_argv,
                    result_path,
                    cwd=evidence,
                    allowed_returncodes={expected_code},
                )
                if evaluated.get("engineering", {}).get("status") != decision:
                    raise ConformanceError(
                        f"{definition['id']} {decision} specification returned "
                        f"{evaluated.get('engineering', {}).get('status')!r}"
                    )
                evaluations.append(evaluated)
                observation["completed_operations"].append(
                    f"specification.evaluate:{definition['id']}:{decision}"
                )

            if index == 1:
                mismatched_conditions = json.loads(json.dumps(workflow["conditions"]))
                mismatched_conditions[1]["value"] = 1.1
                mismatch_specification = _specification(
                    definition,
                    mismatched_conditions,
                    definition["pass_limits"],
                    "condition-mismatch",
                )
                mismatch_request_path = requests / "spec-sample-at-condition-mismatch.json"
                _write_json(mismatch_request_path, mismatch_specification)
                mismatch_argv = [
                    *prefix,
                    "evaluate",
                    "--measurement",
                    str(measurement_result_path),
                    "--specification",
                    str(mismatch_request_path),
                    "--request-id",
                    "17000000-0000-4000-8000-000000000001",
                ]
                observation["openada_invocations"].append(
                    {
                        "operation": "specification.evaluate:condition-mismatch",
                        "cwd": "/evidence",
                        "argv": mismatch_argv,
                    }
                )
                mismatch_result = _invoke(
                    mismatch_argv,
                    negative_dir / "spec-sample-at-condition-mismatch.json",
                    cwd=evidence,
                    allowed_returncodes={2},
                )
                if (
                    mismatch_result.get("engineering", {}).get("status") != "unknown"
                    or "specification.condition.unproven"
                    not in {item.get("code") for item in mismatch_result.get("diagnostics", [])}
                ):
                    raise ConformanceError(
                        "mismatched operating condition did not produce typed unknown evidence"
                    )
                negative_summaries.append(
                    {
                        "id": "specification-condition-mismatch",
                        "operation": "specification.evaluate",
                        "engineering_status": "unknown",
                        "diagnostic": "specification.condition.unproven",
                    }
                )
                observation["completed_operations"].append(
                    "specification.evaluate:condition-mismatch"
                )

        negative_summaries.append(
            {
                "id": "deliberately-violated-limits",
                "operation": "specification.evaluate",
                "engineering_status": "fail",
                "diagnostic": "specification.limit.violated",
            }
        )
        agent_evidence = _build_agent_evidence(
            manifest,
            netlist_negative,
            provider_result,
            terminal_result,
            builtin_result,
            builtin_terminal_result,
            extraction,
            measurement_results,
            evaluations,
            negative_summaries,
        )
        _write_json(evidence / workflow["agent_evidence"], agent_evidence)
        observation["completed_operations"].append("agent.evidence")
    except (ConformanceError, OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        observation["error"] = str(exc)[:4_000]
        print(json.dumps(observation, allow_nan=False, sort_keys=True))
        print(f"inside conformance run failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(observation, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
