#!/usr/bin/env python3
"""Execute the closed public SPICE portability matrix inside the pinned image."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any

from common import ConformanceError, load_manifest, load_requests, sha256_file


CONTAINER_MANIFEST = Path("/openada/conformance/public-spice-portability/manifest.json")
MAX_RESULT_BYTES = 16 * 1024 * 1024
OPENADA = "/openada/bin/openada"
PYTHON = "/usr/bin/python3"
TOOL_PATHS = {
    "xschem": "/foss/tools/xschem/bin/xschem",
    "ngspice": "/foss/tools/ngspice/bin/ngspice",
    "xyce": "/foss/tools/xyce/bin/Xyce",
}
EXTRACT_REQUEST_IDS = {
    "ngspice-op": "21000000-0000-4000-8000-000000000001",
    "ngspice-dc": "21000000-0000-4000-8000-000000000002",
    "ngspice-ac": "21000000-0000-4000-8000-000000000003",
    "xyce-dc": "21000000-0000-4000-8000-000000000004",
    "xyce-ac": "21000000-0000-4000-8000-000000000005",
    "xyce-tran": "21000000-0000-4000-8000-000000000006",
}
MISSING_SELECTOR_REQUEST_ID = "22000000-0000-4000-8000-000000000001"
STARTUP_CONTENT = (
    "osdi /foss/pdks/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi\n"
)
STARTUP_SHA256 = "168ff70c9c37e8a2d687e782cb92b9df81e9f35ed1eb1d1ef14c4e02a27c082d"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=CONTAINER_MANIFEST)
    parser.add_argument("--evidence", type=Path, default=Path("/evidence"))
    return parser


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_record(path: Path, *, recorded_path: str | None = None) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat evidence input {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise ConformanceError(f"evidence input is not a nonempty regular file: {path}")
    return {
        "path": recorded_path or str(path),
        "bytes": metadata.st_size,
        "sha256": sha256_file(path),
    }


def _copy_exact(source: Path, destination: Path, expected_sha256: str) -> dict[str, Any]:
    record = _file_record(source)
    if record["sha256"] != expected_sha256:
        raise ConformanceError(f"pinned source digest differs: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_bytes(source.read_bytes())
    copied = _file_record(destination)
    if copied["sha256"] != expected_sha256:
        raise ConformanceError(f"copied source digest differs: {destination}")
    return copied


def _prefix() -> list[str]:
    return [
        PYTHON,
        OPENADA,
        "--compact",
        "--tool-path",
        f"xschem={TOOL_PATHS['xschem']}",
        "--tool-path",
        f"ngspice={TOOL_PATHS['ngspice']}",
        "--tool-path",
        f"xyce={TOOL_PATHS['xyce']}",
    ]


def _invoke(
    argv: list[str],
    result_path: Path,
    *,
    cwd: Path,
    allowed_returncodes: set[int] = frozenset({0}),
) -> tuple[dict[str, Any], int]:
    environment = os.environ.copy()
    environment["PWD"] = str(cwd)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
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
            f"OpenADA invocation exited {completed.returncode}, expected "
            f"{sorted(allowed_returncodes)}; result retained at {result_path}"
        )
    return document, completed.returncode


def _assert_result(
    result: dict[str, Any],
    *,
    operation: str,
    engineering: str,
    diagnostic: str | None = None,
) -> None:
    if result.get("schema") != "openada.result/v0alpha1":
        raise ConformanceError(f"{operation} did not return the result contract")
    if result.get("operation") != operation:
        raise ConformanceError(f"expected operation {operation!r}, got {result.get('operation')!r}")
    if result.get("engineering", {}).get("status") != engineering:
        raise ConformanceError(
            f"{operation} engineering status differs: {result.get('engineering')!r}"
        )
    codes = {item.get("code") for item in result.get("diagnostics", [])}
    if diagnostic is not None and diagnostic not in codes:
        raise ConformanceError(f"{operation} lacks required diagnostic {diagnostic!r}")


def _model_closure(
    source: Path,
    destination: Path,
    *,
    models: dict[str, Path],
    analysis_directive: str,
    expected_control_blocks: int,
) -> dict[str, Any]:
    """Map one actual Xschem deck into the include-free shared batch profile."""

    source_text = source.read_text(encoding="utf-8", errors="strict")
    corner_text = models["corner_moslv"].read_text(encoding="utf-8", errors="strict")
    parameter_text = models["moslv_parameters"].read_text(
        encoding="utf-8", errors="strict"
    )
    module_text = models["moslv_modules"].read_text(encoding="utf-8", errors="strict")
    corner_match = re.search(
        r"(?ims)^\s*\.LIB\s+mos_tt\s*$\n(?P<body>.*?)"
        r"^\s*\.include\s+sg13g2_moslv_mod\.lib\s*$\n"
        r"^\s*\.ENDL\s+mos_tt\s*$",
        corner_text,
    )
    if corner_match is None:
        raise ConformanceError("pinned mos_tt corner cannot be extracted exactly")
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
        raise ConformanceError("MOS model closure no longer has four reviewed includes")
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib)\b", parameter_text):
        raise ConformanceError("MOS parameter file gained a transitive include")
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib)\b", flattened_modules):
        raise ConformanceError("flattened MOS modules retain an include")
    without_control, control_count = re.subn(
        r"(?ims)^\s*\.control\s*$.*?^\s*\.endc\s*$\n?", "", source_text
    )
    without_library, library_count = re.subn(
        r"(?im)^\s*\.lib\s+cornerMOSlv\.lib\s+mos_tt\s*$\n?",
        "",
        without_control,
    )
    if control_count != expected_control_blocks or library_count != 1:
        raise ConformanceError(
            "Xschem deck cannot be mapped to the reviewed shared simulation profile"
        )
    if re.search(r"(?im)^\s*\.(?:inc(?:lude)?|lib|control)\b", without_library):
        raise ConformanceError("DUT deck contains an unreviewed transitive directive")
    closure = (
        "\n** OpenADA reviewed flattening of pinned IHP mos_tt model closure.\n"
        + corner_match.group("body")
        + "\n"
        + flattened_modules.rstrip()
        + "\n"
        + analysis_directive
        + "\n"
    )
    transformed, end_count = re.subn(
        r"(?im)^\s*\.end\s*$", closure + ".end", without_library
    )
    if end_count != 1:
        raise ConformanceError("Xschem deck lacks one unambiguous top-level .end")
    destination.write_text(transformed, encoding="utf-8")
    return {
        "source_sha256": sha256_file(source),
        "derived_sha256": sha256_file(destination),
        "operation": "remove reviewed control/library directives; inline pinned mos_tt closure; inject one typed analysis",
        "analysis_directive": analysis_directive,
        "removed_control_blocks": control_count,
        "removed_library_directives": library_count,
        "inlined_parameter_include_count": include_count,
    }


def _derive_xyce_ac(source: Path, destination: Path) -> dict[str, Any]:
    text = source.read_text(encoding="utf-8", errors="strict")
    transformed, count = re.subn(r"(?im)^\.print ac v\(1\)\s*\n", "", text)
    if count != 1:
        raise ConformanceError("Xyce AC fixture no longer has one active presentation directive")
    destination.write_text(transformed, encoding="utf-8")
    if sha256_file(destination) != "5e1258932db2b737a94e9b1c61bfa7e6c37ef5da086ad68e22a452351f014cf0":
        raise ConformanceError("reviewed Xyce AC derivation digest drifted")
    return {
        "source_sha256": sha256_file(source),
        "derived_sha256": sha256_file(destination),
        "operation": "remove exactly one active presentation directive",
        "removed_line": ".print ac v(1)",
        "removed_count": count,
    }


def _derive_xyce_op(source: Path, destination: Path) -> dict[str, Any]:
    text = source.read_text(encoding="utf-8", errors="strict")
    transformed, count = re.subn(r"(?im)^\.dc v1 0 10 \.1\s*$", ".op", text)
    if count != 1:
        raise ConformanceError("Xyce DC fixture cannot be transformed into the OP negative")
    destination.write_text(transformed, encoding="utf-8")
    return {
        "source_sha256": sha256_file(source),
        "derived_sha256": sha256_file(destination),
        "operation": "replace exactly one .dc directive with .op to probe advertised support",
        "replacement_count": count,
    }


def _native_artifact(result: dict[str, Any], *, legacy: bool = False) -> dict[str, Any]:
    expected_role = "output" if legacy else "simulation.result"
    matches = [item for item in result.get("artifacts", []) if item.get("role") == expected_role]
    if len(matches) != 1:
        raise ConformanceError(f"simulation result does not retain exactly one {expected_role!r}")
    artifact = matches[0]
    path = Path(artifact["path"])
    record = _file_record(path)
    for field in ("bytes", "sha256"):
        if artifact.get(field) != record[field]:
            raise ConformanceError(f"native artifact {field} differs from retained bytes")
    return artifact


def _simulate_argv(definition: dict[str, Any]) -> list[str]:
    return [
        *_prefix(),
        "simulate",
        definition["deck"],
        *definition["arguments"],
        "--output-dir",
        definition["output_dir"],
        "--workdir",
        "/evidence",
        "--timeout",
        "180",
    ]


def _summary_hash(path: Path) -> dict[str, Any]:
    record = _file_record(path)
    return {"path": str(path), "bytes": record["bytes"], "sha256": record["sha256"]}


def _build_agent_evidence(
    manifest: dict[str, Any],
    simulations: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    extractions: dict[str, dict[str, Any]],
    admin: dict[str, dict[str, Any]],
    negatives: dict[str, dict[str, Any]],
    derivations: dict[str, Any],
    source_identity_path: Path,
    runtime_identity_path: Path,
) -> dict[str, Any]:
    matrix: list[dict[str, Any]] = []
    for definition, result, raw in simulations:
        analysis = result["data"]["analysis"]
        protocol = result["data"]["protocol"]
        extraction_path = Path(f"/evidence/results/extract/{definition['id']}.json")
        matrix.append(
            {
                "id": definition["id"],
                "backend": definition["backend"],
                "analysis": definition["analysis"],
                "driver_id": protocol["driver_id"],
                "driver_version": protocol["driver_version"],
                "engineering_status": result["engineering"]["status"],
                "point_count": analysis["point_count"],
                "dependent_variable_count": analysis["dependent_variable_count"],
                "finite_value_count": analysis["finite_value_count"],
                "simulation_result": _summary_hash(
                    Path(f"/evidence/results/sim/{definition['id']}.json")
                ),
                "native_artifact": {
                    "path": raw["path"],
                    "bytes": raw["bytes"],
                    "sha256": raw["sha256"],
                },
                "extraction_result": _summary_hash(extraction_path),
                "selected_series": [
                    item["name"]
                    for item in extractions[definition["id"]]["data"]["extraction"]["series"]["signals"]
                ],
            }
        )
    admin_summary = [
        {
            "id": identifier,
            "operation": result["operation"],
            "engineering_status": result["engineering"]["status"],
            "result": _summary_hash(Path(f"/evidence/results/admin/{identifier}.json")),
        }
        for identifier, result in admin.items()
    ]
    negative_summary = [
        {
            "id": identifier,
            "operation": result["operation"],
            "engineering_status": result["engineering"]["status"],
            "diagnostic_codes": [item["code"] for item in result["diagnostics"]],
            "result": _summary_hash(Path(f"/evidence/results/negative/{identifier}.json")),
        }
        for identifier, result in negatives.items()
    ]
    return {
        "schema": "openada.public-spice-portability-agent-evidence/v0alpha1",
        "chain_id": manifest["id"],
        "conclusion": "portable-for-reviewed-analysis-matrix",
        "matrix": matrix,
        "admin": admin_summary,
        "negative_replays": negative_summary,
        "source_derivations": derivations,
        "source_identity": _summary_hash(source_identity_path),
        "runtime_identity": _summary_hash(runtime_identity_path),
        "decision_basis": {
            "positive_simulations": len(matrix),
            "typed_extractions": len(extractions),
            "admin_surfaces": len(admin),
            "typed_negative_replays": len(negatives),
            "independent_oracle_required": True,
        },
        "limitations": [
            "The result establishes portability only for the six reviewed analysis/backend cells and exact pinned public fixtures.",
            "The IHP model closure and PSP103 OSDI executable module are content-bound; native tool defaults and host runtime libraries remain bounded provenance.",
            "The Xyce AC source is minimally derived by removing one presentation-only .print directive; both source and derived bytes are retained.",
            "The shared ngspice startup report remains native-default-unenumerated; this chain independently binds the isolated HOME startup bytes.",
            "These checks are engineering evidence for agent decisions, not foundry electrical signoff.",
        ],
        "extensions": {},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation: dict[str, Any] = {
        "schema": "openada.public-spice-portability-container-observation/v0alpha1",
        "openada_invocations": [],
        "completed_operations": [],
        "runtime_inputs": {},
    }
    try:
        manifest = load_manifest(args.manifest.resolve())
        definitions = load_requests()
        evidence = args.evidence.resolve()
        if evidence != Path("/evidence"):
            raise ConformanceError("inside runner requires the reviewed /evidence target")
        if Path(os.environ.get("HOME", "")) != Path("/tmp/openada-home"):
            raise ConformanceError("inside runner requires isolated HOME=/tmp/openada-home")
        home = Path("/tmp/openada-home")
        home.mkdir(mode=0o700)
        startup = home / ".spiceinit"
        startup.write_text(STARTUP_CONTENT, encoding="ascii")
        if sha256_file(startup) != STARTUP_SHA256:
            raise ConformanceError("isolated ngspice startup bytes drifted")

        work = evidence / "work"
        sources = evidence / "sources"
        runtime_dir = evidence / "runtime"
        results = evidence / "results"
        selections = evidence / "selections"
        for directory in (work, sources, runtime_dir, results, selections):
            directory.mkdir(parents=True, mode=0o700)
        (evidence / "request-contract.json").write_bytes(
            Path("/openada/conformance/public-spice-portability/requests.json").read_bytes()
        )

        # Bind and retain every public source byte declared by the manifest.
        source_records: list[dict[str, Any]] = []
        for record in manifest["design"]["inputs"]:
            destination = sources / "xyce" / record["path"]
            copied = _copy_exact(Path("/xyce") / record["path"], destination, record["sha256"])
            source_records.append({"repository": "Xyce_Regression", "source_path": record["path"], **copied})
        license_record = manifest["design"]["license"]
        destination = sources / "xyce" / license_record["path"]
        copied = _copy_exact(Path("/xyce") / license_record["path"], destination, license_record["sha256"])
        source_records.append({"repository": "Xyce_Regression", "source_path": license_record["path"], **copied})
        secondary = manifest["design"]["extensions"]["org.openada"]["secondary_design"]
        for record in secondary["inputs"]:
            destination = sources / "ihp" / record["path"]
            copied = _copy_exact(Path("/ihp") / record["path"], destination, record["sha256"])
            source_records.append({"repository": "IHP-AnalogAcademy", "source_path": record["path"], **copied})
        license_record = secondary["license"]
        destination = sources / "ihp" / license_record["path"]
        copied = _copy_exact(Path("/ihp") / license_record["path"], destination, license_record["sha256"])
        source_records.append({"repository": "IHP-AnalogAcademy", "source_path": license_record["path"], **copied})
        source_identity_path = evidence / "source-identities.json"
        _write_json(
            source_identity_path,
            {
                "schema": "openada.public-source-identities/v0alpha1",
                "xyce_revision": manifest["design"]["revision"],
                "xyce_tag": manifest["design"]["extensions"]["org.openada"]["tag"],
                "ihp_revision": secondary["revision"],
                "files": source_records,
            },
        )

        # Bind the three actual tools plus the PDK/model runtime and retain reconstruction bytes.
        runtime_pins = manifest["runtime"]["extensions"]["org.openada"]
        runtime_records: dict[str, dict[str, Any]] = {}
        for tool_id, expected_hash in runtime_pins["tool_sha256"].items():
            record = _file_record(Path(TOOL_PATHS[tool_id]))
            if record["sha256"] != expected_hash:
                raise ConformanceError(f"{tool_id} executable digest differs")
            runtime_records[f"tool:{tool_id}"] = record
        pdk = runtime_pins["pdk"]
        pdk_files = {
            "pdk_commit": pdk["commit_file"],
            "xschem_rcfile": pdk["xschem_rcfile"],
            "corner_moslv": pdk["model_files"]["corner_moslv"],
            "moslv_modules": pdk["model_files"]["moslv_modules"],
            "moslv_parameters": pdk["model_files"]["moslv_parameters"],
            "psp103_osdi": pdk["psp103_osdi"],
        }
        for identifier, expected in pdk_files.items():
            record = _file_record(Path(expected["path"]))
            if record["sha256"] != expected["sha256"]:
                raise ConformanceError(f"runtime input digest differs: {identifier}")
            runtime_records[identifier] = record
        if Path(pdk["commit_file"]["path"]).read_text(encoding="ascii").strip() != manifest["runtime"]["pdk_revision"]:
            raise ConformanceError("PDK COMMIT content differs from the manifest revision")
        retained_names = {
            "corner_moslv": "cornerMOSlv.lib",
            "moslv_modules": "sg13g2_moslv_mod.lib",
            "moslv_parameters": "sg13g2_moslv_parm.lib",
            "psp103_osdi": "psp103.osdi",
        }
        for identifier, retained_name in retained_names.items():
            destination = runtime_dir / retained_name
            destination.write_bytes(Path(runtime_records[identifier]["path"]).read_bytes())
        (runtime_dir / "isolated.spiceinit").write_bytes(startup.read_bytes())
        runtime_records["isolated_spiceinit"] = _file_record(startup)
        runtime_identity_path = evidence / "runtime-identities.json"
        _write_json(
            runtime_identity_path,
            {
                "schema": "openada.public-runtime-identities/v0alpha1",
                "image_reference": manifest["runtime"]["image_reference"],
                "platform": manifest["runtime"]["platform"],
                "records": runtime_records,
            },
        )
        observation["runtime_inputs"] = runtime_records

        # Materialize local symbol closures and use the real Xschem command twice.
        inverter_dir = work / "ihp-inverter"
        ota_dir = work / "ihp-ota"
        inverter_dir.mkdir(mode=0o700)
        ota_dir.mkdir(mode=0o700)
        inverter_root = Path("/ihp/modules/module_0_foundations/inverter")
        for name in ("inverter_tb.sch", "inverter.sym", "inverter.sch"):
            (inverter_dir / name).write_bytes((inverter_root / name).read_bytes())
        ota_testbench = Path("/ihp/modules/module_1_bandgap_reference/part_1_OTA/gmid_example/testbenches/ota_testbench.sch")
        ota_schematic = Path("/ihp/modules/module_1_bandgap_reference/part_1_OTA/gmid_example/schematic")
        (ota_dir / "ota_testbench.sch").write_bytes(ota_testbench.read_bytes())
        for name in ("two_stage_OTA.sym", "two_stage_OTA.sch"):
            (ota_dir / name).write_bytes((ota_schematic / name).read_bytes())
        netlist_jobs = [
            ("inverter", inverter_dir / "inverter_tb.sch", work / "inverter-xschem.spice", inverter_dir),
            ("ota", ota_dir / "ota_testbench.sch", work / "ota-xschem.spice", ota_dir),
        ]
        for identifier, schematic, output, cwd in netlist_jobs:
            argv2 = [
                *_prefix(), "netlist", str(schematic), "--output", str(output),
                "--rcfile", pdk["xschem_rcfile"]["path"], "--timeout", "120",
            ]
            observation["openada_invocations"].append({"operation": f"netlist:{identifier}", "cwd": str(cwd), "argv": argv2})
            result, _ = _invoke(argv2, results / "netlist" / f"{identifier}.json", cwd=cwd)
            _assert_result(result, operation="netlist", engineering="pass")
            observation["completed_operations"].append(f"netlist:{identifier}")

        models = {
            name: Path(runtime_records[name]["path"])
            for name in ("corner_moslv", "moslv_modules", "moslv_parameters")
        }
        derivations: dict[str, Any] = {}
        derivations["inverter-op"] = _model_closure(
            work / "inverter-xschem.spice", work / "inverter-op.spice",
            models=models, analysis_directive=".op", expected_control_blocks=1,
        )
        derivations["inverter-dc"] = _model_closure(
            work / "inverter-xschem.spice", work / "inverter-dc.spice",
            models=models, analysis_directive=".dc V1 0 1.2 0.1", expected_control_blocks=1,
        )
        derivations["ota-ac"] = _model_closure(
            work / "ota-xschem.spice", work / "ota-ac.spice",
            models=models, analysis_directive=".ac dec 10 1 100000", expected_control_blocks=2,
        )
        xyce_sources = {
            "xyce-dc": sources / "xyce/Netlists/Output/DC/dc-noprn.cir",
            "xyce-ac-source": sources / "xyce/Netlists/ACtests/RC_simple.cir",
            "xyce-tran": sources / "xyce/Netlists/Output/TRAN/tran-raw-override-noprint.cir",
        }
        for identifier, source in xyce_sources.items():
            destination_name = {
                "xyce-dc": "xyce-dc.cir",
                "xyce-ac-source": "xyce-ac-source.cir",
                "xyce-tran": "xyce-tran.cir",
            }[identifier]
            (work / destination_name).write_bytes(source.read_bytes())
        derivations["xyce-ac"] = _derive_xyce_ac(work / "xyce-ac-source.cir", work / "xyce-ac-derived.cir")
        derivations["xyce-op-unsupported"] = _derive_xyce_op(work / "xyce-dc.cir", work / "xyce-op-unsupported.cir")
        _write_json(evidence / "derivations.json", {"schema": "openada.public-source-derivations/v0alpha1", "records": derivations})

        provider_manifest = json.loads(
            Path("/openada/providers/ngspice-pdk-control/driver-manifest.json").read_text(encoding="utf-8")
        )
        provider_manifest["driver"]["version"] = 42
        _write_json(work / "provider-invalid.json", provider_manifest)

        simulations: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        simulation_by_id: dict[str, dict[str, Any]] = {}
        raw_by_id: dict[str, dict[str, Any]] = {}
        for definition in definitions["simulations"]:
            argv2 = _simulate_argv(definition)
            observation["openada_invocations"].append({"operation": f"simulate:{definition['id']}", "cwd": "/evidence", "argv": argv2})
            result_path = results / "sim" / f"{definition['id']}.json"
            result, _ = _invoke(argv2, result_path, cwd=evidence)
            _assert_result(result, operation="simulate", engineering="pass")
            protocol = result.get("data", {}).get("protocol", {})
            expected_driver = f"org.openada.driver.{definition['backend']}"
            if protocol.get("driver_id") != expected_driver:
                raise ConformanceError(f"{definition['id']} dispatched to the wrong driver")
            if result["data"]["analysis"].get("type") != definition["analysis"]:
                raise ConformanceError(f"{definition['id']} normalized the wrong analysis")
            raw = _native_artifact(result)
            simulations.append((definition, result, raw))
            simulation_by_id[definition["id"]] = result
            raw_by_id[definition["id"]] = raw
            observation["completed_operations"].append(f"simulate:{definition['id']}")

        legacy = definitions["legacy_simulation"]
        legacy_argv = [
            *_prefix(), "simulate", legacy["deck"], "--output-dir", legacy["output_dir"],
            "--workdir", "/evidence", "--timeout", "180",
        ]
        observation["openada_invocations"].append({"operation": "simulate:legacy-ngspice-op", "cwd": "/evidence", "argv": legacy_argv})
        legacy_result, _ = _invoke(legacy_argv, results / "legacy-ngspice-op.json", cwd=evidence)
        _assert_result(legacy_result, operation="simulate", engineering="pass")
        _native_artifact(legacy_result, legacy=True)
        observation["completed_operations"].append("simulate:legacy-ngspice-op")

        extractions: dict[str, dict[str, Any]] = {}
        for definition in definitions["simulations"]:
            identifier = definition["id"]
            selection_path = selections / f"{identifier}.json"
            _write_json(
                selection_path,
                {"selectors": definition["selectors"], "conditions": definition["conditions"], "extensions": {}},
            )
            extract_argv = [
                *_prefix(), "extract",
                "--simulation", f"/evidence/results/sim/{identifier}.json",
                "--artifact", raw_by_id[identifier]["path"],
                "--selection", str(selection_path),
                "--request-id", EXTRACT_REQUEST_IDS[identifier],
            ]
            observation["openada_invocations"].append({"operation": f"extract:{identifier}", "cwd": "/evidence", "argv": extract_argv})
            extracted, _ = _invoke(extract_argv, results / "extract" / f"{identifier}.json", cwd=evidence)
            _assert_result(extracted, operation="result.series.extract", engineering="pass")
            series = extracted["data"]["extraction"]["series"]
            if (
                series["source"]["lineage"]["artifact_sha256"]
                != raw_by_id[identifier]["sha256"]
            ):
                raise ConformanceError(f"{identifier} extraction lost native-artifact lineage")
            extractions[identifier] = extracted
            observation["completed_operations"].append(f"extract:{identifier}")

        admin: dict[str, dict[str, Any]] = {}
        for definition in definitions["admin_commands"]:
            identifier = definition["id"]
            admin_argv = [*_prefix(), *definition["arguments"]]
            observation["openada_invocations"].append({"operation": f"admin:{identifier}", "cwd": "/evidence", "argv": admin_argv})
            result, _ = _invoke(admin_argv, results / "admin" / f"{identifier}.json", cwd=evidence)
            expected = "not_applicable" if identifier in {"capabilities", "doctor"} else "pass"
            expected_operation = "doctor" if identifier in {"capabilities", "doctor"} else identifier.replace("-", ".")
            _assert_result(result, operation=expected_operation, engineering=expected)
            admin[identifier] = result
            observation["completed_operations"].append(f"admin:{identifier}")

        negatives: dict[str, dict[str, Any]] = {}
        negative_specs = {item["id"]: item for item in definitions["negative_commands"]}
        negative_expectations = {
            "xyce-ac-presentation-rejected": ("simulate", "unknown", "simulation.request.invalid", {2}),
            "xyce-op-unsupported": ("simulate", "unknown", "simulation.analysis.unsupported", {2}),
            "ngspice-analysis-mismatch": ("simulate", "unknown", "simulation.request.invalid", {2}),
            "extract-missing-selector": ("result.series.extract", "unknown", "series.selector.missing", {2}),
            "admin-unknown-profile": ("profile.show", "fail", "profile.not_found", {1}),
            "admin-invalid-provider": ("provider.validate", "unknown", "provider.manifest.invalid", {2}),
        }
        for identifier in [item["id"] for item in definitions["negative_commands"]]:
            definition = negative_specs[identifier]
            operation, engineering, diagnostic, returncodes = negative_expectations[identifier]
            if definition["kind"] == "simulate":
                output_dir = f"/evidence/negative-native/{identifier}"
                negative_argv = [
                    *_prefix(), "simulate", definition["deck"], *definition["arguments"],
                    "--output-dir", output_dir, "--workdir", "/evidence", "--timeout", "180",
                ]
            elif definition["kind"] == "extract":
                selection_path = selections / "missing-selector.json"
                _write_json(
                    selection_path,
                    {
                        "selectors": [{"native_name": "does_not_exist", "output_name": "missing", "unit": "V", "component": "real"}],
                        "conditions": definitions["simulations"][3]["conditions"],
                        "extensions": {},
                    },
                )
                negative_argv = [
                    *_prefix(), "extract",
                    "--simulation", "/evidence/results/sim/xyce-dc.json",
                    "--artifact", raw_by_id["xyce-dc"]["path"],
                    "--selection", str(selection_path),
                    "--request-id", MISSING_SELECTOR_REQUEST_ID,
                ]
            else:
                negative_argv = [*_prefix(), *definition["arguments"]]
            observation["openada_invocations"].append({"operation": f"negative:{identifier}", "cwd": "/evidence", "argv": negative_argv})
            result, _ = _invoke(
                negative_argv,
                results / "negative" / f"{identifier}.json",
                cwd=evidence,
                allowed_returncodes=returncodes,
            )
            _assert_result(result, operation=operation, engineering=engineering, diagnostic=diagnostic)
            negatives[identifier] = result
            observation["completed_operations"].append(f"negative:{identifier}")

        agent = _build_agent_evidence(
            manifest, simulations, extractions, admin, negatives, derivations,
            source_identity_path, runtime_identity_path,
        )
        _write_json(evidence / "agent-evidence.json", agent)
        observation["matrix"] = {
            "simulation_count": len(simulations),
            "extraction_count": len(extractions),
            "admin_count": len(admin),
            "negative_count": len(negatives),
        }
    except ConformanceError as exc:
        observation["error"] = str(exc)
        print(json.dumps(observation, allow_nan=False, sort_keys=True))
        return 1
    print(json.dumps(observation, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
