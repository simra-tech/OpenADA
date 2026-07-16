#!/usr/bin/env python3
"""Independently verify the pinned ORFS Ibex synthesis/timing evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from common import (
    ABC_EXECUTABLE_BYTES,
    ABC_EXECUTABLE_PATH,
    ABC_EXECUTABLE_SHA256,
    ABC_EXECUTABLE_VERSION,
    ABC_REPOSITORY_PATH,
    ABC_BYTES,
    ABC_SHA256,
    CHAIN_ID,
    ConformanceError,
    DESIGN_REVISION,
    DESIGN_TREE,
    IMAGE_CONFIG_DIGEST,
    IMAGE_REFERENCE,
    OPENSTA_PATH,
    OPENSTA_VERSION,
    UPSTREAM_REVISION,
    YOSYS_PATH,
    YOSYS_VERSION,
    canonical_inventory_sha256,
    load_manifest,
    sha256_file,
)


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402


RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas/result-v0alpha1.schema.json"
RUN_SCHEMA_PATH = HERE / "run.schema.json"
DESIGN_PROVENANCE_SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas/design-provenance-v0alpha1.schema.json"
)
SYNTHESIS_PROFILE_PATH = REPOSITORY_ROOT / "profiles/logic.synthesize-v1alpha1.json"
TIMING_PROFILE_PATH = REPOSITORY_ROOT / "profiles/timing.analyze-v1alpha1.json"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
SYNTHESIS_CWD_RE = re.compile(r"^/evidence/(?:synthesis|negative)/\.openada-synthesize-[A-Za-z0-9_-]+$")
TIMING_CWD_RE = re.compile(r"^/evidence/timing/\.openada-sta-[A-Za-z0-9_-]+$")
NUMBER_RE = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
LIBERTY_CELL_COUNT = 135
LIBERTY_CELL_INVENTORY_SHA256 = (
    "1cf511d764ec1ae8da9b566a5b71fb542021d245ff0092b48b7e15f04f366a26"
)

NATIVE_ARTIFACT_PATHS = [
    "synthesis/synthesize.result.json",
    "synthesis/synthesize.ys",
    "synthesis/synthesize.log",
    "synthesis/inference-stats.json",
    "synthesis/mapped-stats.json",
    "synthesis/mapped.v",
    "synthesis/mapped.json",
    "synthesis/rtl-inputs.json",
    "timing/timing-analyze.result.json",
    "timing/timing-analyze.tcl",
    "timing/timing-input.sdc",
    "timing/timing-analyze.log",
    "timing/check-setup.txt",
    "timing/setup-paths.json",
    "timing/hold-paths.json",
    "negative/synthesize.result.json",
    "negative/synthesize.ys",
    "negative/synthesize.log",
    "negative/rtl-inputs.json",
]
EXPECTED_FILES = {"design-provenance.json", "run.json", *NATIVE_ARTIFACT_PATHS}
UNRESOLVED_INCLUDES = [
    "/design/flow/designs/src/ibex_sv/ibex_multdiv_fast.sv:formal_tb_frag.svh",
    "/design/flow/designs/src/ibex_sv/ibex_multdiv_slow.sv:formal_tb_frag.svh",
    (
        "/design/flow/designs/src/ibex_sv/vendor/lowrisc_ip/prim/rtl/"
        "prim_assert.sv:uvm_macros.svh"
    ),
    (
        "/design/flow/designs/src/ibex_sv/vendor/lowrisc_ip/prim/rtl/"
        "prim_assert.sv:prim_assert_yosys_macros.svh"
    ),
    (
        "/design/flow/designs/src/ibex_sv/vendor/lowrisc_ip/prim/rtl/"
        "prim_assert.sv:prim_assert_standard_macros.svh"
    ),
]


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _expect(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _require_regular(
    path: Path,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = False,
) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_symlink():
        raise ConformanceError(f"{label} must be one regular non-linked file: {path}")
    minimum = 0 if allow_empty else 1
    if not minimum <= metadata.st_size <= maximum:
        raise ConformanceError(
            f"{label} size is outside {minimum}..{maximum} bytes: {path}"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    _require_regular(path, label=label, maximum=maximum)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object")
    return document


def _read_text(
    path: Path,
    *,
    label: str,
    maximum: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    _require_regular(path, label=label, maximum=maximum, allow_empty=allow_empty)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc


def _validator(path: Path, *, label: str) -> Draft202012Validator:
    schema = _read_json(path, label=f"{label} schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConformanceError(f"invalid {label} schema: {exc.message}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _profile_data_validator(path: Path, *, label: str) -> Draft202012Validator:
    profile = _read_json(path, label=label)
    try:
        schema = profile["normalized_result"]["data_schema"]
    except (KeyError, TypeError) as exc:
        raise ConformanceError(f"{label} lacks normalized_result.data_schema") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConformanceError(f"invalid {label} data schema: {exc.message}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate(document: Any, validator: Draft202012Validator, *, label: str) -> None:
    errors = sorted(
        validator.iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        raise ConformanceError(f"{label} violates its schema at {location}: {error.message}")


def _artifact_map(records: Any, location: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ConformanceError(f"{location} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConformanceError(f"{location}[{index}] is not an artifact record")
        if record["path"] in mapped:
            raise ConformanceError(f"{location} repeats artifact path {record['path']!r}")
        mapped[record["path"]] = record
    return mapped


def _verify_artifact(
    record: dict[str, Any],
    evidence: Path,
    relative: str,
    *,
    kind: str,
    role: str,
    allow_empty: bool = False,
) -> None:
    path = evidence / relative
    size = _require_regular(
        path,
        label=f"retained {role}",
        maximum=512 * 1024 * 1024,
        allow_empty=allow_empty,
    )
    _expect(record.get("path"), f"/evidence/{relative}", f"{relative}.path")
    _expect(record.get("exists"), True, f"{relative}.exists")
    _expect(record.get("kind"), kind, f"{relative}.kind")
    _expect(record.get("role"), role, f"{relative}.role")
    _expect(record.get("bytes"), size, f"{relative}.bytes")
    _expect(record.get("sha256"), sha256_file(path), f"{relative}.sha256")


def _parse_transcript(path: Path, *, expected_exit: int) -> dict[str, Any]:
    body = _read_text(path, label="native process transcript")
    header, stdout_marker, payload = body.partition("--- stdout ---\n")
    stdout, stderr_marker, stderr = payload.partition("\n--- stderr ---\n")
    if not stdout_marker or not stderr_marker:
        raise ConformanceError("native process transcript lacks closed stdout/stderr sections")
    fields: dict[str, str] = {}
    for line in header.splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key in fields:
            raise ConformanceError("native process transcript header is not closed")
        fields[key] = value
    _expect(
        set(fields),
        {
            "status",
            "exit_code",
            "stdout_bytes",
            "stderr_bytes",
            "stdout_truncated",
            "stderr_truncated",
        },
        "native transcript header keys",
    )
    _expect(fields["status"], "completed", "native transcript status")
    _expect(fields["exit_code"], str(expected_exit), "native transcript exit code")
    _expect(fields["stdout_bytes"], str(len(stdout.encode("utf-8"))), "native stdout bytes")
    _expect(fields["stderr_bytes"], str(len(stderr.encode("utf-8"))), "native stderr bytes")
    _expect(fields["stdout_truncated"], "false", "native stdout truncation")
    _expect(fields["stderr_truncated"], "false", "native stderr truncation")
    return {"stdout": stdout, "stderr": stderr}


def _design_path(relative: str) -> str:
    return f"/design/{relative}"


def _expected_synthesis_inputs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    records = {record["path"]: record for record in manifest["pinned_files"]}
    synth = manifest["operations"]["synthesize"]
    declarations: list[tuple[str, str, str, dict[str, Any]]] = []
    declarations.extend(
        (_design_path(path), "hdl-source", "rtl.source", records[path])
        for path in synth["source_paths"]
    )
    include_records = sorted(
        (record for record in manifest["pinned_files"] if record["role"] == "rtl-include"),
        key=lambda record: record["path"],
    )
    declarations.extend(
        (_design_path(record["path"]), "hdl-include", "rtl.include", record)
        for record in include_records
    )
    declarations.append(
        (_design_path(synth["liberty"]), "liberty", "technology.liberty", records[synth["liberty"]])
    )
    declarations.extend(
        (_design_path(path), "yosys-techmap", "synthesis.techmap", records[path])
        for path in synth["techmaps"]
    )
    declarations.append(
        (
            f"/openada/{ABC_REPOSITORY_PATH}",
            "abc-constraint",
            "synthesis.abc-constraint",
            {"bytes": ABC_BYTES, "sha256": ABC_SHA256},
        )
    )
    declarations.append(
        (
            ABC_EXECUTABLE_PATH,
            "eda-executable",
            "synthesis.abc-executable",
            {"bytes": ABC_EXECUTABLE_BYTES, "sha256": ABC_EXECUTABLE_SHA256},
        )
    )
    return [
        {
            "path": path,
            "exists": True,
            "bytes": record["bytes"],
            "sha256": record["sha256"],
            "kind": kind,
            "role": role,
        }
        for path, kind, role, record in declarations
    ]


def _expected_synthesis_script(manifest: dict[str, Any], top: str) -> str:
    operation = manifest["operations"]["synthesize"]
    liberty = _design_path(operation["liberty"])
    lines = [f'read_liberty -lib -ignore_miss_dir -setattr blackbox "{liberty}"']
    slang = ["read_slang", "--std", operation["language"], "--top", top]
    for directory in operation["include_directories"]:
        slang.extend(("-I", _design_path(directory)))
    slang.extend(_design_path(path) for path in operation["source_paths"])
    lines.extend(
        (
            " ".join(slang),
            f"hierarchy -check -top {top}",
            f"synth -top {top} -flatten -noabc",
            f"tee -o inference-stats.json stat -json -top {top}",
        )
    )
    lines.extend(f'techmap -map "{_design_path(path)}"' for path in operation["techmaps"])
    dont_use = "".join(f' -dont_use "{value}"' for value in operation["dont_use"])
    lines.extend(
        (
            f'dfflibmap -liberty "{liberty}"{dont_use}',
            "opt",
            "setundef -zero",
            (
                f'abc -exe "{ABC_EXECUTABLE_PATH}" -liberty "{liberty}"{dont_use} '
                f'-constr "/openada/{ABC_REPOSITORY_PATH}" -D 2200'
            ),
            "splitnets",
            "opt_clean -purge",
            "check -assert",
            f'tee -o mapped-stats.json stat -json -top {top} -liberty "{liberty}"',
            'write_verilog -noattr -noexpr "mapped.v"',
            'write_json "mapped.json"',
            "",
        )
    )
    return "\n".join(lines)


def _expected_rtl_inputs(manifest: dict[str, Any]) -> dict[str, Any]:
    operation = manifest["operations"]["synthesize"]
    return {
        "schema": "openada.hdl-inputs/v1",
        "ordered_sources": [_design_path(path) for path in operation["source_paths"]],
        "resolved_literal_includes": [
            _design_path(record["path"])
            for record in sorted(
                (
                    record
                    for record in manifest["pinned_files"]
                    if record["role"] == "rtl-include"
                ),
                key=lambda record: record["path"],
            )
        ],
        "unresolved_literal_includes": UNRESOLVED_INCLUDES,
        "declared_include_directories": [
            _design_path(path) for path in operation["include_directories"]
        ],
        "defines": [],
        "input_records": _expected_synthesis_inputs(manifest),
    }


def _normalized_stats(document: dict[str, Any], *, label: str) -> dict[str, Any]:
    design = document.get("design")
    if not isinstance(design, dict):
        raise ConformanceError(f"{label}.design must be an object")
    histogram = design.get("num_cells_by_type")
    if not isinstance(histogram, dict) or not histogram:
        raise ConformanceError(f"{label} lacks a nonempty cell histogram")
    for name, count in histogram.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise ConformanceError(f"{label} has an invalid cell histogram")
    for key in ("num_cells", "num_memories", "num_memory_bits", "num_processes"):
        value = design.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConformanceError(f"{label}.{key} is invalid")
    if sum(histogram.values()) != design["num_cells"]:
        raise ConformanceError(f"{label} cell histogram does not sum to num_cells")
    for key in ("area", "sequential_area"):
        value = design.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ConformanceError(f"{label}.{key} is invalid")
    return {
        key: design.get(key)
        for key in (
            "num_cells",
            "num_memories",
            "num_memory_bits",
            "num_processes",
            "area",
            "sequential_area",
            "num_cells_by_type",
        )
    }


def _verify_mapped_json(
    mapped: dict[str, Any],
    *,
    stats: dict[str, Any],
    liberty_cells: set[str],
) -> dict[str, Any]:
    modules = mapped.get("modules")
    if not isinstance(modules, dict) or "ibex_core" not in modules:
        raise ConformanceError("mapped Yosys JSON lacks ibex_core")
    top = modules["ibex_core"]
    if not isinstance(top, dict) or not isinstance(top.get("cells"), dict):
        raise ConformanceError("mapped Yosys JSON ibex_core has invalid cells")
    attributes = top.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("top") != "00000000000000000000000000000001":
        raise ConformanceError("mapped Yosys JSON does not identify ibex_core as top")
    cells = top["cells"]
    observed = Counter()
    for cell in cells.values():
        if not isinstance(cell, dict) or not isinstance(cell.get("type"), str):
            raise ConformanceError("mapped Yosys JSON contains an invalid cell record")
        observed[cell["type"]] += 1
    if dict(sorted(observed.items())) != dict(sorted(stats["num_cells_by_type"].items())):
        raise ConformanceError("mapped Yosys JSON cell histogram differs from mapped statistics")
    invalid = sorted(name for name in observed if name.startswith("$") or name not in liberty_cells)
    if invalid:
        raise ConformanceError("mapped Yosys JSON contains non-Liberty cells: " + ", ".join(invalid[:20]))
    ports = top.get("ports")
    if not isinstance(ports, dict) or len(ports) != 30:
        raise ConformanceError("mapped ibex_core does not retain the reviewed 30-port interface")
    port_bits = sum(
        len(record.get("bits", []))
        for record in ports.values()
        if isinstance(record, dict) and isinstance(record.get("bits"), list)
    )
    if port_bits != 264:
        raise ConformanceError("mapped ibex_core does not retain the reviewed 264 port bits")
    return {
        "module": "ibex_core",
        "port_count": len(ports),
        "port_bit_count": port_bits,
        "cell_count": len(cells),
        "cell_type_count": len(observed),
    }


def _verify_synthesis(
    manifest: dict[str, Any],
    evidence: Path,
    result_validator: Draft202012Validator,
    data_validator: Draft202012Validator,
) -> dict[str, Any]:
    operation = manifest["operations"]["synthesize"]
    result_document = _read_json(
        evidence / operation["result_filename"], label="positive synthesis result"
    )
    _validate(result_document, result_validator, label="positive synthesis result")
    _validate(result_document.get("data"), data_validator, label="positive synthesis data")
    _expect(result_document.get("schema"), "openada.result/v0alpha1", "synthesis.schema")
    _expect(result_document.get("operation"), "synthesize", "synthesis.operation")
    _expect(
        result_document.get("tool"),
        {"name": "yosys", "path": YOSYS_PATH, "version": YOSYS_VERSION},
        "synthesis.tool",
    )
    execution = result_document["execution"]
    _expect(execution.get("status"), "completed", "synthesis.execution.status")
    _expect(execution.get("exit_code"), 0, "synthesis.execution.exit_code")
    _expect(
        execution.get("command"),
        [YOSYS_PATH, "-Q", "-T", "-s", "/evidence/synthesis/synthesize.ys"],
        "synthesis.execution.command",
    )
    cwd = execution.get("cwd")
    if not isinstance(cwd, str) or SYNTHESIS_CWD_RE.fullmatch(cwd) is None or not cwd.startswith("/evidence/synthesis/"):
        raise ConformanceError(f"synthesis execution cwd is not reviewed: {cwd!r}")
    _expect(
        result_document.get("engineering"),
        {
            "status": "pass",
            "summary": operation["expect"]["summary"],
        },
        "synthesis.engineering",
    )
    expected_inputs = _expected_synthesis_inputs(manifest)
    _expect(result_document.get("inputs"), expected_inputs, "synthesis.inputs")
    data = result_document["data"]
    _expect(data.get("top"), "ibex_core", "synthesis.data.top")
    _expect(data.get("frontend"), "slang", "synthesis.data.frontend")
    _expect(data.get("language"), "1800-2017", "synthesis.data.language")
    _expect(data.get("ordered_sources"), [_design_path(path) for path in operation["source_paths"]], "synthesis.data.ordered_sources")
    expected_includes = [
        _design_path(record["path"])
        for record in sorted(
            (record for record in manifest["pinned_files"] if record["role"] == "rtl-include"),
            key=lambda record: record["path"],
        )
    ]
    _expect(data.get("include_dependencies"), expected_includes, "synthesis.data.include_dependencies")
    _expect(data.get("unresolved_literal_includes"), UNRESOLVED_INCLUDES, "synthesis.data.unresolved_literal_includes")
    _expect(data.get("unresolved_literal_includes_truncated"), False, "synthesis.data.unresolved_literal_includes_truncated")
    _expect(data.get("inputs_stable"), True, "synthesis.data.inputs_stable")
    _expect(data.get("dependency_closure_stable"), True, "synthesis.data.dependency_closure_stable")
    _expect(data.get("tool_identity_stable"), True, "synthesis.data.tool_identity_stable")
    _expect(
        data.get("abc_tool"),
        {
            "name": "abc",
            "path": ABC_EXECUTABLE_PATH,
            "version": ABC_EXECUTABLE_VERSION,
            "bytes": ABC_EXECUTABLE_BYTES,
            "sha256": ABC_EXECUTABLE_SHA256,
        },
        "synthesis.data.abc_tool",
    )
    _expect(
        data.get("abc_tool_identity_stable"),
        True,
        "synthesis.data.abc_tool_identity_stable",
    )
    _expect(
        data.get("environment_policy"),
        {
            "id": "closed-yosys-abc-v1",
            "inherit_parent": False,
            "variables": {
                "PATH": "/foss/tools/bin:/foss/tools/yosys/bin:/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "HOME": "/nonexistent",
                "TMPDIR": "/tmp",
                "TOOLS": "/foss/tools",
            },
        },
        "synthesis.data.environment_policy",
    )
    _expect(data.get("changed_inputs"), [], "synthesis.data.changed_inputs")
    _expect(data.get("changed_inputs_truncated"), False, "synthesis.data.changed_inputs_truncated")
    _expect(
        data.get("mapping_policy"),
        {
            "flatten": True,
            "set_undefined_to_zero": True,
            "dont_use": operation["dont_use"],
            "abc_delay_target_ns": 2.2,
            "abc_constraint_supplied": True,
        },
        "synthesis.data.mapping_policy",
    )
    _expect(data.get("stats_validation"), "parsed", "synthesis.data.stats_validation")
    _expect(data.get("inference_stats_validation"), "parsed", "synthesis.data.inference_stats_validation")
    _expect(data.get("mapped_json_validation"), "parsed", "synthesis.data.mapped_json_validation")
    _expect(data.get("mapping_complete"), True, "synthesis.data.mapping_complete")
    _expect(data.get("unmapped_cell_types"), [], "synthesis.data.unmapped_cell_types")
    _expect(data.get("unmapped_cell_types_truncated"), False, "synthesis.data.unmapped_cell_types_truncated")
    warnings = data.get("warnings")
    if not isinstance(warnings, list) or data.get("warning_count") != len(warnings):
        raise ConformanceError("synthesis warning count does not bind the complete warning list")
    _expect(data.get("warnings_truncated"), False, "synthesis.data.warnings_truncated")
    expected_diagnostics = [
        {"severity": "warning", "code": "yosys.warning", "message": warning}
        for warning in warnings
    ]
    _expect(result_document.get("diagnostics"), expected_diagnostics, "synthesis.diagnostics")

    expected_artifacts = {
        "/evidence/synthesis/synthesize.ys": ("synthesis/synthesize.ys", "yosys-script", "synthesis.script", False),
        "/evidence/synthesis/synthesize.log": ("synthesis/synthesize.log", "yosys-log", "synthesis.log", False),
        "/evidence/synthesis/inference-stats.json": ("synthesis/inference-stats.json", "yosys-stat-json", "synthesis.inference-statistics", False),
        "/evidence/synthesis/mapped-stats.json": ("synthesis/mapped-stats.json", "yosys-stat-json", "synthesis.statistics", False),
        "/evidence/synthesis/mapped.v": ("synthesis/mapped.v", "verilog-netlist", "synthesis.netlist", False),
        "/evidence/synthesis/mapped.json": ("synthesis/mapped.json", "yosys-json", "synthesis.netlist-structure", False),
        "/evidence/synthesis/rtl-inputs.json": ("synthesis/rtl-inputs.json", "hdl-input-manifest", "rtl.dependencies", False),
    }
    artifacts = _artifact_map(result_document.get("artifacts"), "synthesis.artifacts")
    _expect(set(artifacts), set(expected_artifacts), "synthesis artifact paths")
    for absolute, (relative, kind, role, allow_empty) in expected_artifacts.items():
        _verify_artifact(artifacts[absolute], evidence, relative, kind=kind, role=role, allow_empty=allow_empty)

    script = _read_text(evidence / "synthesis/synthesize.ys", label="positive Yosys script")
    _expect(script, _expected_synthesis_script(manifest, "ibex_core"), "positive Yosys script")
    dependencies = _read_json(evidence / "synthesis/rtl-inputs.json", label="synthesis input manifest")
    _expect(dependencies, _expected_rtl_inputs(manifest), "synthesis input manifest")
    transcript = _parse_transcript(evidence / "synthesis/synthesize.log", expected_exit=0)
    if transcript["stderr"] or "Build succeeded: 0 errors, 0 warnings" not in transcript["stdout"]:
        raise ConformanceError("positive Yosys transcript lacks a clean Slang build")
    if "ERROR:" in transcript["stdout"] or "ERROR:" in transcript["stderr"]:
        raise ConformanceError("positive Yosys transcript contains a native error")

    mapped_stats_document = _read_json(evidence / "synthesis/mapped-stats.json", label="mapped statistics")
    inference_stats_document = _read_json(evidence / "synthesis/inference-stats.json", label="inference statistics")
    mapped_stats = _normalized_stats(mapped_stats_document, label="mapped statistics")
    inference_stats = _normalized_stats(inference_stats_document, label="inference statistics")
    _expect(data.get("stats"), mapped_stats, "normalized mapped statistics")
    _expect(data.get("inference_stats"), inference_stats, "normalized inference statistics")
    _expect(
        data.get("mapped_structure"),
        {
            "top": "ibex_core",
            "num_cells": mapped_stats["num_cells"],
            "num_cells_by_type": mapped_stats["num_cells_by_type"],
        },
        "normalized mapped structure",
    )
    if not 1_000 <= mapped_stats["num_cells"] <= 100_000:
        raise ConformanceError("mapped Ibex cell count is outside the reviewed engineering bound")
    if mapped_stats["num_processes"] != 0 or mapped_stats["num_memories"] != 0:
        raise ConformanceError("mapped Ibex retains processes or memories")
    forbidden_cells = sorted(
        set(mapped_stats["num_cells_by_type"]).intersection(operation["dont_use"])
    )
    if forbidden_cells:
        raise ConformanceError(
            "mapped Ibex uses cells excluded by the declared policy: "
            + ", ".join(forbidden_cells)
        )
    if not isinstance(mapped_stats["area"], (int, float)) or mapped_stats["area"] <= 0:
        raise ConformanceError("mapped Ibex lacks positive Liberty area")
    if not isinstance(mapped_stats["sequential_area"], (int, float)) or mapped_stats["sequential_area"] <= 0:
        raise ConformanceError("mapped Ibex lacks positive sequential Liberty area")
    mapped_json = _read_json(evidence / "synthesis/mapped.json", label="mapped Yosys JSON")
    # The verifier runs outside the read-only public checkout. The identical
    # pinned Liberty is available only during native replay, so extract the
    # black-box module inventory retained in Yosys JSON and independently
    # require all instantiated types to be declared there.
    modules = mapped_json.get("modules")
    if not isinstance(modules, dict):
        raise ConformanceError("mapped Yosys JSON lacks modules")
    liberty_cells = {
        name
        for name, module in modules.items()
        if name != "ibex_core"
        and isinstance(module, dict)
        and isinstance(module.get("attributes"), dict)
        and module["attributes"].get("blackbox") == "00000000000000000000000000000001"
    }
    if not liberty_cells:
        raise ConformanceError("mapped Yosys JSON lacks retained Liberty black-box declarations")
    inventory_digest = hashlib.sha256(
        "\n".join(sorted(liberty_cells)).encode("utf-8")
    ).hexdigest()
    _expect(len(liberty_cells), LIBERTY_CELL_COUNT, "retained Liberty cell count")
    _expect(
        inventory_digest,
        LIBERTY_CELL_INVENTORY_SHA256,
        "retained Liberty cell inventory digest",
    )
    structure = _verify_mapped_json(mapped_json, stats=mapped_stats, liberty_cells=liberty_cells)
    structure["liberty_cell_inventory_count"] = len(liberty_cells)
    structure["liberty_cell_inventory_sha256"] = inventory_digest
    netlist = _read_text(evidence / "synthesis/mapped.v", label="mapped Verilog netlist")
    if re.search(r"(?m)^module\s+ibex_core\b", netlist) is None:
        raise ConformanceError("mapped Verilog netlist lacks ibex_core")
    return {
        "result": result_document,
        "stats": mapped_stats,
        "inference_stats": inference_stats,
        "structure": structure,
        "transcript": transcript,
    }


def _verify_negative_synthesis(
    manifest: dict[str, Any],
    evidence: Path,
    result_validator: Draft202012Validator,
    data_validator: Draft202012Validator,
) -> dict[str, Any]:
    operation = manifest["operations"]["missing_top"]
    result_document = _read_json(evidence / operation["result_filename"], label="missing-top synthesis result")
    _validate(result_document, result_validator, label="missing-top synthesis result")
    _validate(result_document.get("data"), data_validator, label="missing-top synthesis data")
    _expect(result_document.get("operation"), "synthesize", "missing-top.operation")
    _expect(result_document.get("tool"), {"name": "yosys", "path": YOSYS_PATH, "version": YOSYS_VERSION}, "missing-top.tool")
    execution = result_document["execution"]
    _expect(execution.get("status"), "completed", "missing-top.execution.status")
    if not isinstance(execution.get("exit_code"), int) or execution["exit_code"] == 0:
        raise ConformanceError("missing-top native Yosys execution did not fail")
    _expect(execution.get("command"), [YOSYS_PATH, "-Q", "-T", "-s", "/evidence/negative/synthesize.ys"], "missing-top.execution.command")
    cwd = execution.get("cwd")
    if not isinstance(cwd, str) or SYNTHESIS_CWD_RE.fullmatch(cwd) is None or not cwd.startswith("/evidence/negative/"):
        raise ConformanceError(f"missing-top execution cwd is not reviewed: {cwd!r}")
    _expect(result_document.get("engineering", {}).get("status"), "fail", "missing-top.engineering.status")
    _expect(result_document.get("inputs"), _expected_synthesis_inputs(manifest), "missing-top.inputs")
    data = result_document["data"]
    _expect(data.get("top"), "missing_ibex_core", "missing-top.data.top")
    _expect(data.get("mapping_complete"), False, "missing-top.data.mapping_complete")
    _expect(data.get("inputs_stable"), True, "missing-top.data.inputs_stable")
    _expect(data.get("dependency_closure_stable"), True, "missing-top.data.dependency_closure_stable")
    _expect(data.get("tool_identity_stable"), True, "missing-top.data.tool_identity_stable")
    _expect(
        data.get("abc_tool"),
        {
            "name": "abc",
            "path": ABC_EXECUTABLE_PATH,
            "version": ABC_EXECUTABLE_VERSION,
            "bytes": ABC_EXECUTABLE_BYTES,
            "sha256": ABC_EXECUTABLE_SHA256,
        },
        "missing-top.data.abc_tool",
    )
    _expect(data.get("abc_tool_identity_stable"), True, "missing-top.data.abc_tool_identity_stable")
    _expect(
        data.get("environment_policy"),
        {
            "id": "closed-yosys-abc-v1",
            "inherit_parent": False,
            "variables": {
                "PATH": "/foss/tools/bin:/foss/tools/yosys/bin:/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "HOME": "/nonexistent",
                "TMPDIR": "/tmp",
                "TOOLS": "/foss/tools",
            },
        },
        "missing-top.data.environment_policy",
    )
    artifacts = _artifact_map(result_document.get("artifacts"), "missing-top.artifacts")
    expected_artifacts = {
        "/evidence/negative/synthesize.ys": ("negative/synthesize.ys", "yosys-script", "synthesis.script"),
        "/evidence/negative/synthesize.log": ("negative/synthesize.log", "yosys-log", "synthesis.log"),
        "/evidence/negative/rtl-inputs.json": ("negative/rtl-inputs.json", "hdl-input-manifest", "rtl.dependencies"),
    }
    _expect(set(artifacts), set(expected_artifacts), "missing-top artifact paths")
    for absolute, (relative, kind, role) in expected_artifacts.items():
        _verify_artifact(artifacts[absolute], evidence, relative, kind=kind, role=role)
    _expect(_read_text(evidence / "negative/synthesize.ys", label="negative Yosys script"), _expected_synthesis_script(manifest, "missing_ibex_core"), "negative Yosys script")
    _expect(_read_json(evidence / "negative/rtl-inputs.json", label="negative input manifest"), _expected_rtl_inputs(manifest), "negative input manifest")
    transcript = _parse_transcript(evidence / "negative/synthesize.log", expected_exit=execution["exit_code"])
    diagnostic = operation["expect"]["diagnostic_substring"]
    native = transcript["stdout"] + transcript["stderr"]
    if diagnostic not in native:
        raise ConformanceError("missing-top native transcript lacks the requested top name")
    if not any(
        item.get("severity") == "error" and diagnostic in item.get("message", "")
        for item in result_document.get("diagnostics", [])
        if isinstance(item, dict)
    ):
        raise ConformanceError("missing-top normalized diagnostics do not bind the native failure")
    return {"result": result_document, "transcript": transcript, "diagnostic": diagnostic}


def _expected_timing_script(manifest: dict[str, Any]) -> str:
    operation = manifest["operations"]["timing_analyze"]
    return "\n".join(
        (
            f'read_liberty "{_design_path(operation["liberty"])}"',
            f'read_verilog "{operation["netlist"]}"',
            f'link_design "{operation["top"]}"',
            'read_sdc "/evidence/timing/timing-input.sdc"',
            "puts OPENADA_UNITS_BEGIN",
            "report_units",
            "puts OPENADA_UNITS_END",
            "check_setup -verbose -unconstrained_endpoints > check-setup.txt",
            "report_checks -path_delay max -group_path_count 10 -endpoint_path_count 1 -format json > setup-paths.json",
            "report_checks -path_delay min -group_path_count 10 -endpoint_path_count 1 -format json > hold-paths.json",
            "puts OPENADA_SETUP_WNS_BEGIN",
            "report_worst_slack -max -digits 9",
            "puts OPENADA_SETUP_WNS_END",
            "puts OPENADA_SETUP_TNS_BEGIN",
            "report_tns -max -digits 9",
            "puts OPENADA_SETUP_TNS_END",
            "puts OPENADA_HOLD_WNS_BEGIN",
            "report_worst_slack -min -digits 9",
            "puts OPENADA_HOLD_WNS_END",
            "puts OPENADA_HOLD_TNS_BEGIN",
            "report_tns -min -digits 9",
            "puts OPENADA_HOLD_TNS_END",
            "puts OPENADA_ANALYSIS_COMPLETE",
            "",
        )
    )


def _marker_value(text: str, marker: str, label: str, mode: str) -> float:
    begin = f"OPENADA_{marker}_BEGIN"
    end = f"OPENADA_{marker}_END"
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ConformanceError(f"timing transcript lacks one {marker} marker block")
    block = text.split(begin, 1)[1].split(end, 1)[0]
    matches = re.findall(rf"(?im)^\s*{re.escape(label)}\s+{mode}\s+({NUMBER_RE})\s*$", block)
    if len(matches) != 1:
        raise ConformanceError(f"timing transcript has an unparseable {marker} metric")
    value = float(matches[0])
    if not math.isfinite(value):
        raise ConformanceError(f"timing transcript has a nonfinite {marker} metric")
    return value


def _parse_path_report(path: Path, *, mode: str) -> dict[str, Any]:
    document = _read_json(path, label=f"{mode} timing path report")
    checks = document.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 1000:
        raise ConformanceError(f"{mode} timing path report lacks a bounded checks array")
    normalized: list[dict[str, Any]] = []
    for item in checks:
        if not isinstance(item, dict) or item.get("path_type") != mode:
            raise ConformanceError(f"{mode} timing path report contains the wrong path type")
        slack = item.get("slack")
        if not isinstance(slack, (int, float)) or isinstance(slack, bool) or not math.isfinite(slack):
            raise ConformanceError(f"{mode} timing path report contains an invalid slack")
        for key in ("startpoint", "endpoint", "path_group"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ConformanceError(f"{mode} timing path report lacks {key}")
        normalized.append(item)
    critical = min(normalized, key=lambda item: item["slack"])
    return {
        "path_count": len(normalized),
        "critical_path": {
            "startpoint": critical["startpoint"],
            "endpoint": critical["endpoint"],
            "path_group": critical["path_group"],
            "slack_s": critical["slack"],
        },
    }


def _verify_timing(
    manifest: dict[str, Any],
    evidence: Path,
    result_validator: Draft202012Validator,
    data_validator: Draft202012Validator,
    synthesis: dict[str, Any],
) -> dict[str, Any]:
    operation = manifest["operations"]["timing_analyze"]
    result_document = _read_json(evidence / operation["result_filename"], label="timing result")
    _validate(result_document, result_validator, label="timing result")
    _validate(result_document.get("data"), data_validator, label="timing result data")
    _expect(result_document.get("operation"), "timing-analyze", "timing.operation")
    _expect(result_document.get("tool"), {"name": "sta", "path": OPENSTA_PATH, "version": OPENSTA_VERSION}, "timing.tool")
    execution = result_document["execution"]
    _expect(execution.get("status"), "completed", "timing.execution.status")
    _expect(execution.get("exit_code"), 0, "timing.execution.exit_code")
    _expect(execution.get("command"), [OPENSTA_PATH, "-no_init", "-exit", "/evidence/timing/timing-analyze.tcl"], "timing.execution.command")
    cwd = execution.get("cwd")
    if not isinstance(cwd, str) or TIMING_CWD_RE.fullmatch(cwd) is None:
        raise ConformanceError(f"timing execution cwd is not reviewed: {cwd!r}")
    _expect(result_document.get("engineering"), {"status": "fail", "summary": operation["expect"]["summary"]}, "timing.engineering")
    pinned = {record["path"]: record for record in manifest["pinned_files"]}
    netlist = evidence / "synthesis/mapped.v"
    expected_inputs = [
        {
            "path": "/evidence/synthesis/mapped.v",
            "exists": True,
            "bytes": netlist.stat().st_size,
            "sha256": sha256_file(netlist),
            "kind": "verilog-netlist",
            "role": "timing.netlist",
        },
        {
            "path": _design_path(operation["liberty"]),
            "exists": True,
            "bytes": pinned[operation["liberty"]]["bytes"],
            "sha256": pinned[operation["liberty"]]["sha256"],
            "kind": "liberty",
            "role": "technology.liberty",
        },
        {
            "path": _design_path(operation["sdc"]),
            "exists": True,
            "bytes": pinned[operation["sdc"]]["bytes"],
            "sha256": pinned[operation["sdc"]]["sha256"],
            "kind": "sdc",
            "role": "timing.sdc",
        },
    ]
    _expect(result_document.get("inputs"), expected_inputs, "timing.inputs")
    data = result_document["data"]
    for key, expected in (
        ("top", "ibex_core"),
        ("analysis_model", "single_corner_ideal_interconnect_no_spef"),
        ("interconnect_model", "ideal"),
        ("spef_supplied", False),
        ("signoff_level", False),
        ("environment_policy", "closed-opensta-runtime-v1"),
        ("sdc_policy", "openada-sdc-v1"),
        ("sdc_validation", "parsed-safe-subset"),
        ("netlist_validation", "self-contained"),
        ("liberty_validation", "self-contained"),
        ("constraints_complete", True),
        ("reports_complete", True),
        ("inputs_stable", True),
        ("tool_identity_stable", True),
        ("changed_inputs", []),
        ("constraint_check_validation", "complete"),
        ("metrics_validation", "parsed"),
        ("metric_consistency", "consistent"),
        ("path_reports_agree_with_wns", True),
        ("time_unit", "1ns"),
        ("time_unit_seconds", 1e-9),
        ("timing_constraints_satisfied", False),
    ):
        _expect(data.get(key), expected, f"timing.data.{key}")
    _expect(
        data.get("limitations"),
        [
            "No SPEF parasitics are read; interconnect is ideal.",
            "Only one Liberty/SDC corner is analyzed; this is not MCMM signoff.",
            "Mapped Verilog and Liberty inputs must be self-contained; transitive include directives are rejected.",
        ],
        "timing.data.limitations",
    )
    setup = data.get("setup")
    hold = data.get("hold")
    if not isinstance(setup, dict) or not isinstance(hold, dict):
        raise ConformanceError("timing result lacks setup/hold metric objects")
    if not setup["wns_s"] < 0 or setup["tns_s"] >= 0:
        raise ConformanceError("timing result does not contain the expected real setup failure")
    if hold["wns_s"] < 0 or hold["tns_s"] != 0:
        raise ConformanceError("timing result does not contain the expected nonnegative hold result")
    expected_artifacts = {
        "/evidence/timing/timing-analyze.tcl": ("timing/timing-analyze.tcl", "opensta-script", "timing.script", False),
        "/evidence/timing/timing-input.sdc": ("timing/timing-input.sdc", "sdc", "timing.sdc-snapshot", False),
        "/evidence/timing/timing-analyze.log": ("timing/timing-analyze.log", "opensta-log", "timing.log", False),
        "/evidence/timing/check-setup.txt": ("timing/check-setup.txt", "opensta-check-setup", "timing.constraint-check", True),
        "/evidence/timing/setup-paths.json": ("timing/setup-paths.json", "opensta-report-json", "timing.setup-paths", False),
        "/evidence/timing/hold-paths.json": ("timing/hold-paths.json", "opensta-report-json", "timing.hold-paths", False),
    }
    artifacts = _artifact_map(result_document.get("artifacts"), "timing.artifacts")
    _expect(set(artifacts), set(expected_artifacts), "timing artifact paths")
    for absolute, (relative, kind, role, allow_empty) in expected_artifacts.items():
        _verify_artifact(artifacts[absolute], evidence, relative, kind=kind, role=role, allow_empty=allow_empty)
    snapshot = evidence / "timing/timing-input.sdc"
    _expect(snapshot.stat().st_size, pinned[operation["sdc"]]["bytes"], "SDC snapshot bytes")
    _expect(sha256_file(snapshot), pinned[operation["sdc"]]["sha256"], "SDC snapshot digest")
    _expect(_read_text(evidence / "timing/timing-analyze.tcl", label="OpenSTA script"), _expected_timing_script(manifest), "OpenSTA script")
    _expect(_read_text(evidence / "timing/check-setup.txt", label="constraint check", allow_empty=True), "", "OpenSTA constraint check")
    transcript = _parse_transcript(evidence / "timing/timing-analyze.log", expected_exit=0)
    if transcript["stderr"] or "OPENADA_ANALYSIS_COMPLETE" not in transcript["stdout"] or "OpenSTA 3.1.0" not in transcript["stdout"]:
        raise ConformanceError("OpenSTA transcript is incomplete or contains stderr")
    setup_wns = _marker_value(transcript["stdout"], "SETUP_WNS", "worst slack", "max") * 1e-9
    setup_tns = _marker_value(transcript["stdout"], "SETUP_TNS", "tns", "max") * 1e-9
    hold_wns = _marker_value(transcript["stdout"], "HOLD_WNS", "worst slack", "min") * 1e-9
    hold_tns = _marker_value(transcript["stdout"], "HOLD_TNS", "tns", "min") * 1e-9
    for observed, expected, label in (
        (setup["wns_s"], setup_wns, "setup WNS"),
        (setup["tns_s"], setup_tns, "setup TNS"),
        (hold["wns_s"], hold_wns, "hold WNS"),
        (hold["tns_s"], hold_tns, "hold TNS"),
    ):
        if not math.isclose(observed, expected, rel_tol=0, abs_tol=1e-18):
            raise ConformanceError(f"normalized {label} differs from the native transcript")
    setup_report = _parse_path_report(evidence / "timing/setup-paths.json", mode="max")
    hold_report = _parse_path_report(evidence / "timing/hold-paths.json", mode="min")
    _expect(setup.get("path_count"), setup_report["path_count"], "setup path count")
    _expect(hold.get("path_count"), hold_report["path_count"], "hold path count")
    _expect(setup.get("critical_path"), setup_report["critical_path"], "setup critical path")
    _expect(hold.get("critical_path"), hold_report["critical_path"], "hold critical path")
    # JSON path reports use fewer significant digits than report_worst_slack.
    for metric, report, label in (
        (setup["wns_s"], setup_report, "setup"),
        (hold["wns_s"], hold_report, "hold"),
    ):
        reported = report["critical_path"]["slack_s"]
        tolerance = max(5e-13, abs(metric) * 5e-4)
        if not math.isclose(metric, reported, rel_tol=0, abs_tol=tolerance):
            raise ConformanceError(f"{label} path report does not agree with normalized WNS")
    mapped_artifacts = [
        item
        for item in synthesis["result"]["artifacts"]
        if item.get("role") == "synthesis.netlist"
    ]
    if len(mapped_artifacts) != 1:
        raise ConformanceError("synthesis result does not identify one mapped netlist")
    _expect(
        expected_inputs[0]["sha256"],
        mapped_artifacts[0]["sha256"],
        "timing-to-synthesis netlist digest",
    )
    return {
        "result": result_document,
        "transcript": transcript,
        "setup_report": setup_report,
        "hold_report": hold_report,
    }


def _verify_design_provenance(manifest: dict[str, Any], evidence: Path) -> dict[str, Any]:
    document = _read_json(evidence / "design-provenance.json", label="design provenance")
    _validate(document, _validator(DESIGN_PROVENANCE_SCHEMA_PATH, label="design provenance"), label="design provenance")
    _expect(document.get("repository"), manifest["design"]["repository"], "design provenance repository")
    _expect(document.get("revision"), DESIGN_REVISION, "design provenance revision")
    _expect(document.get("tree"), DESIGN_TREE, "design provenance tree")
    _expect(document.get("checkout_clean"), True, "design provenance clean state")
    _expect(document.get("remote_url_verified"), True, "design provenance remote")
    expected_inputs = [
        {"path": record["path"], "bytes": record["bytes"], "sha256": record["sha256"]}
        for record in manifest["pinned_files"]
        if record["path"] != manifest["design"]["license"]["path"]
    ]
    _expect(document.get("inputs"), expected_inputs, "design provenance inputs")
    design_license = next(
        record
        for record in manifest["pinned_files"]
        if record["path"] == manifest["design"]["license"]["path"]
    )
    _expect(document.get("license"), {"path": design_license["path"], "bytes": design_license["bytes"], "sha256": design_license["sha256"]}, "design provenance license")
    return document


def _verify_run(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    run = _read_json(evidence / "run.json", label="conformance run")
    _validate(run, _validator(RUN_SCHEMA_PATH, label="conformance run"), label="conformance run")
    _expect(run.get("conformance_id"), manifest["id"], "run.conformance_id")
    _expect(run.get("chain_id"), CHAIN_ID, "run.chain_id")
    _expect(run.get("conformance_manifest_sha256"), manifest_sha256, "run manifest digest")
    _expect(run.get("design_revision"), DESIGN_REVISION, "run.design_revision")
    _expect(run.get("design_tree"), DESIGN_TREE, "run.design_tree")
    _expect(run.get("upstream_revision"), UPSTREAM_REVISION, "run.upstream_revision")
    _expect(run.get("input_inventory_sha256"), canonical_inventory_sha256(manifest), "run input inventory")
    _expect(run.get("image"), {"reference": IMAGE_REFERENCE, "id": IMAGE_CONFIG_DIGEST, "os": "linux", "architecture": "amd64"}, "run.image")
    _expect(
        run.get("tools"),
        {
            "yosys": {"path": YOSYS_PATH, "version": YOSYS_VERSION},
            "abc": {
                "path": ABC_EXECUTABLE_PATH,
                "version": ABC_EXECUTABLE_VERSION,
                "bytes": ABC_EXECUTABLE_BYTES,
                "sha256": ABC_EXECUTABLE_SHA256,
            },
            "opensta": {"path": OPENSTA_PATH, "version": OPENSTA_VERSION},
        },
        "run.tools",
    )
    _expect(run.get("network"), "none during EDA execution", "run.network")
    _expect(run.get("analysis_scope"), manifest["policy"]["analysis_scope"], "run.analysis_scope")
    checkout = run["openada_checkout"]
    _expect(checkout.get("state_unchanged"), True, "run checkout state")
    _expect(checkout.get("before"), checkout.get("after"), "run checkout before/after")
    expected_commit_exact = not bool(checkout["before"]["working_tree_modified"])
    _expect(checkout.get("commit_exact"), expected_commit_exact, "run checkout commit exact")
    subject = semantic_subject(REPOSITORY_ROOT, REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json")
    source = run["source_attestation"]
    _expect(source.get("semantic_subject_sha256"), subject, "run source semantic subject")
    _expect(source.get("state_unchanged"), True, "run source state")
    if source.get("receipt_class") == "release":
        _expect(source.get("clean_before"), True, "release source clean before")
        _expect(source.get("clean_after"), True, "release source clean after")
        _expect(checkout.get("commit_exact"), True, "release checkout commit exact")
    records = _artifact_map(run.get("native_artifacts"), "run.native_artifacts")
    _expect(set(records), set(NATIVE_ARTIFACT_PATHS), "run native artifact paths")
    for relative in NATIVE_ARTIFACT_PATHS:
        path = evidence / relative
        allow_empty = relative == "timing/check-setup.txt"
        size = _require_regular(path, label=f"run artifact {relative}", maximum=512 * 1024 * 1024, allow_empty=allow_empty)
        _expect(records[relative], {"path": relative, "bytes": size, "sha256": sha256_file(path)}, f"run artifact {relative}")
    return run


def _verify_closed_evidence_directory(evidence: Path) -> None:
    if not evidence.is_dir() or evidence.is_symlink():
        raise ConformanceError(f"evidence root must be a regular directory: {evidence}")
    observed = {
        path.relative_to(evidence).as_posix()
        for path in evidence.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    _expect(observed, EXPECTED_FILES, "closed evidence file inventory")


def verify_evidence(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    evidence = evidence.expanduser().resolve()
    _verify_closed_evidence_directory(evidence)
    result_validator = _validator(RESULT_SCHEMA_PATH, label="OpenADA result")
    synthesis_validator = _profile_data_validator(SYNTHESIS_PROFILE_PATH, label="synthesis profile")
    timing_validator = _profile_data_validator(TIMING_PROFILE_PATH, label="timing profile")
    synthesis = _verify_synthesis(manifest, evidence, result_validator, synthesis_validator)
    timing = _verify_timing(manifest, evidence, result_validator, timing_validator, synthesis)
    negative = _verify_negative_synthesis(manifest, evidence, result_validator, synthesis_validator)
    run = _verify_run(manifest, evidence, manifest_sha256=manifest_sha256)
    provenance = _verify_design_provenance(manifest, evidence)
    return {
        "verified": True,
        "synthesis": synthesis,
        "timing": timing,
        "negative": negative,
        "run": run,
        "design_provenance": provenance,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        verified = verify_evidence(
            manifest,
            args.evidence,
            manifest_sha256=sha256_file(manifest_path),
        )
    except ConformanceError as exc:
        print(f"evidence verification failed: {exc}", file=sys.stderr)
        return 1
    stats = verified["synthesis"]["stats"]
    setup = verified["timing"]["result"]["data"]["setup"]
    print(
        "Evidence verified: Ibex mapped to "
        f"{stats['num_cells']} Liberty cells; setup WNS is {setup['wns_s']:.12g} s "
        "and the real missing-top request was rejected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
