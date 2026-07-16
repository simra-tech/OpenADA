"""Small Yosys RTL elaboration and structural-check driver."""

from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from decimal import Decimal, InvalidOperation

from ..contract import (
    FileRecordError,
    diagnostic,
    file_record,
    result,
    stable_regular_file,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..process import run_process
from .hdl import (
    MAX_HDL_PARSE_BYTES,
    changed_input_paths,
    hdl_closure_stability,
    resolve_hdl_inputs,
    valid_hdl_identifier,
    write_process_transcript,
)


MAX_JSON_PARSE_BYTES = 5 * 1024 * 1024
MAX_SYNTHESIS_JSON_BYTES = 32 * 1024 * 1024
MAX_LIBERTY_PARSE_BYTES = 128 * 1024 * 1024
MAX_CELL_TYPES = 8_192
MAX_NATIVE_NETLIST_BYTES = 128 * 1024 * 1024
MAX_ABC_EXECUTABLE_BYTES = 128 * 1024 * 1024
MAX_ABC_DELAY_PS = 2_147_483_647
MAX_JSON_CELLS = 2_000_000
_ENVIRONMENT_POLICY_ID = "closed-yosys-abc-v1"
_DEFINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@'\-]*)?")
_CELL_GLOB = re.compile(r"[A-Za-z0-9_.$*?\[\]-]+")
_SLANG_TOKEN = re.compile(r"[-A-Za-z0-9_./:+@'=]+")
_TECHMAP_INCLUDE = re.compile(r"(?m)^\s*`include\b")
_LIBERTY_TRANSITIVE_READ = re.compile(
    rb"(?<![A-Za-z0-9_])(?:include|include_file)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_ENGINEERING_ERROR = re.compile(
    r"^ERROR:\s+(?:"
    r"Module\s+.+\s+not\s+found!|"
    r"'[A-Za-z_][A-Za-z0-9_$]*'\s+is\s+not\s+a\s+valid\s+top-level\s+module|"
    r"Parser\s+error\s+in\s+line\s+.+|"
    r"Found\s+logic\s+loop\s+in\s+module\s+.+|"
    r"Multiple\s+conflicting\s+drivers\s+for\s+.+|"
    r"Latch\s+inferred\s+for\s+signal\s+.+"
    r")$",
    re.IGNORECASE,
)


def _closed_synthesis_environment(
    yosys_binary: str | None,
    abc_binary: str | None,
) -> tuple[dict[str, str], dict]:
    search_directories: list[str] = []
    for binary in (yosys_binary, abc_binary):
        if binary:
            parent = str(Path(binary).parent)
            if parent not in search_directories:
                search_directories.append(parent)
    for directory in ("/usr/bin", "/bin"):
        if directory not in search_directories:
            search_directories.append(directory)
    variables = {
        "PATH": os.pathsep.join(search_directories),
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "TMPDIR": "/tmp",
    }
    if yosys_binary:
        launcher = Path(yosys_binary)
        if launcher.name == "yosys" and launcher.parent.name == "bin":
            candidate_root = launcher.parent.parent
            direct_yosys = candidate_root / "yosys" / "bin" / "yosys"
            try:
                root_text = str(candidate_root.resolve(strict=True))
                direct_metadata = direct_yosys.stat()
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                if (
                    candidate_root.is_absolute()
                    and len(root_text) <= 4_096
                    and not any(
                        ord(character) < 32 or ord(character) == 127
                        for character in root_text
                    )
                    and direct_yosys.is_file()
                    and os.access(direct_yosys, os.X_OK)
                    and direct_metadata.st_size > 0
                ):
                    # IIC-OSIC-TOOLS deliberately exposes /foss/tools/bin/yosys
                    # as a wrapper that loads its pinned GHDL and Slang plugins.
                    # Bind only the canonical installation root it structurally
                    # requires; never inherit an ambient TOOLS value.
                    variables["TOOLS"] = root_text
    return variables, {
        "id": _ENVIRONMENT_POLICY_ID,
        "inherit_parent": False,
        "variables": dict(variables),
    }


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _capture_native_output(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
) -> tuple[dict | None, str]:
    """Copy one native output into a fresh, bounded, OpenADA-owned file.

    The native pathname is opened without following symlinks, must have exactly
    one link, and is rechecked after the bounded copy.  Copying into an
    O_EXCL-created destination breaks any writer's remaining handle to the
    native inode and makes the retained artifact stable after capture.
    """
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_created = False
    capture_complete = False
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None, "not-regular"
        if opened.st_nlink != 1:
            return None, "multiple-hard-links"
        if opened.st_size > maximum_bytes:
            return None, "too-large"
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        destination_created = True
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = os.read(source_descriptor, min(1024 * 1024, maximum_bytes - observed + 1))
            if not chunk:
                break
            observed += len(chunk)
            if observed > maximum_bytes:
                return None, "too-large"
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("short native-output capture write")
                offset += written
        os.fsync(destination_descriptor)
        finished = os.fstat(source_descriptor)
        current = os.stat(source, follow_symlinks=False)
        if (
            observed != opened.st_size
            or _stat_identity(finished) != _stat_identity(opened)
            or _stat_identity(current) != _stat_identity(opened)
        ):
            return None, "changed-during-capture"
        retained = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(retained.st_mode)
            or retained.st_nlink != 1
            or retained.st_size != observed
        ):
            return None, "unsafe-destination"
        os.close(destination_descriptor)
        destination_descriptor = None
        record = file_record(
            destination,
            kind="native-output",
            role="native-output",
            maximum_bytes=maximum_bytes,
        )
        if (
            not record.get("exists")
            or record.get("bytes") != observed
            or record.get("sha256") != digest.hexdigest()
        ):
            return None, "changed-after-capture"
        capture_complete = True
        return record, "captured"
    except FileNotFoundError:
        return None, "missing"
    except (FileRecordError, OSError, ValueError):
        return None, "unsafe-or-unavailable"
    finally:
        if destination_descriptor is not None:
            try:
                os.close(destination_descriptor)
            except OSError:
                pass
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if destination_created and not capture_complete:
            try:
                destination.unlink()
            except OSError:
                pass


def _abc_delay_picoseconds(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, False
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None, False
    if not numeric.is_finite():
        return None, False
    picoseconds = numeric * Decimal(1000)
    integral = picoseconds.to_integral_value()
    if picoseconds != integral or integral < 1 or integral > MAX_ABC_DELAY_PS:
        return None, False
    return int(integral), True


def _techmap_self_contained(path: Path) -> tuple[bool, str]:
    """Conservatively gate v1 techmaps to one captured regular file."""
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size > MAX_HDL_PARSE_BYTES:
                return False, "too-large"
            text = handle.read(MAX_HDL_PARSE_BYTES + 1).decode("utf-8", errors="strict")
    except (FileRecordError, OSError, UnicodeError):
        return False, "unparseable"
    if _TECHMAP_INCLUDE.search(text):
        return False, "contains-include"
    return True, "self-contained"


def _write_exclusive_evidence(
    path: Path,
    content: str,
    *,
    kind: str,
    role: str,
    maximum_bytes: int,
) -> dict:
    encoded = content.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{path} exceeds the {maximum_bytes}-byte evidence limit")
    descriptor: int | None = None
    created = False
    completed = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short write while retaining generated evidence")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        record = file_record(
            path,
            kind=kind,
            role=role,
            maximum_bytes=maximum_bytes,
        )
        if not record.get("exists") or record.get("bytes") != len(encoded):
            raise FileRecordError(f"{path} changed after exclusive evidence creation")
        completed = True
        return record
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not completed:
            try:
                path.unlink()
            except OSError:
                pass


def _yosys_quote(path: Path) -> str:
    value = str(path)
    if len(value) > 4_096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("Yosys paths must be bounded and may not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_json_netlist(
    path: Path, *, max_parse_bytes: int = MAX_JSON_PARSE_BYTES
) -> tuple[bool, str]:
    size = path.stat().st_size
    if size == 0:
        return False, "empty"
    if size <= max_parse_bytes:
        try:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, "invalid"
        if not isinstance(payload, dict):
            return False, "invalid-root"
        modules = payload.get("modules")
        if not isinstance(modules, dict) or not modules:
            return False, "missing-modules"
        return True, "parsed"

    # A brace check is not evidence that a large JSON document is complete or
    # that it is a Yosys netlist. Until this path has a streaming validator,
    # report the result as unknown instead of promoting it to an engineering
    # pass.
    return False, "too-large-to-validate"


def _validate_statistics_record(value: object) -> tuple[dict | None, str]:
    if not isinstance(value, dict):
        return None, "invalid-shape"
    required = {
        "num_cells",
        "num_memories",
        "num_memory_bits",
        "num_processes",
        "num_cells_by_type",
    }
    if not required.issubset(value):
        return None, "invalid-shape"
    if any(
        not isinstance(value[key], int)
        or isinstance(value[key], bool)
        or value[key] < 0
        for key in ("num_cells", "num_memories", "num_memory_bits", "num_processes")
    ):
        return None, "invalid-count"
    histogram = value["num_cells_by_type"]
    if (
        not isinstance(histogram, dict)
        or len(histogram) > MAX_CELL_TYPES
        or any(
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for name, count in histogram.items()
        )
    ):
        return None, "invalid-histogram"
    if sum(histogram.values()) != value["num_cells"]:
        return None, "inconsistent-cell-count"
    for key in ("area", "sequential_area"):
        number = value.get(key)
        if number is not None and (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(number)
            or number < 0
        ):
            return None, f"invalid-{key.replace('_', '-')}"
    return value, "parsed"


def _load_synthesis_stats(path: Path, *, requested_top: str) -> tuple[dict | None, str]:
    """Load one bounded Yosys stat -json design record."""
    try:
        size = path.stat().st_size
        if size == 0:
            return None, "empty"
        if size > MAX_SYNTHESIS_JSON_BYTES:
            return None, "too-large"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(payload, dict):
        return None, "invalid-shape"
    design, validation = _validate_statistics_record(payload.get("design"))
    if design is None:
        return None, validation
    modules = payload.get("modules")
    if not isinstance(modules, dict) or not modules or len(modules) > 4_096:
        return None, "invalid-modules"
    top_keys = [name for name in (requested_top, f"\\{requested_top}") if name in modules]
    if len(top_keys) != 1:
        return None, "top-mismatch"
    top_record, validation = _validate_statistics_record(modules[top_keys[0]])
    if top_record is None:
        return None, f"top-{validation}"
    for key in (
        "num_cells",
        "num_memories",
        "num_memory_bits",
        "num_processes",
        "num_cells_by_type",
    ):
        if top_record[key] != design[key]:
            return None, "top-design-mismatch"
    return design, "parsed"


def _yosys_top_attribute(value: object) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and bool(re.fullmatch(r"0*1", value))


def _load_mapped_structure(
    path: Path,
    *,
    requested_top: str,
) -> tuple[dict | None, str]:
    try:
        size = path.stat().st_size
        if size == 0:
            return None, "empty"
        if size > MAX_SYNTHESIS_JSON_BYTES:
            return None, "too-large-to-validate"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    modules = payload.get("modules") if isinstance(payload, dict) else None
    if not isinstance(modules, dict) or not modules:
        return None, "missing-modules"
    top_module = modules.get(requested_top)
    if not isinstance(top_module, dict):
        return None, "missing-requested-top"
    attributes = top_module.get("attributes")
    if not isinstance(attributes, dict) or not _yosys_top_attribute(attributes.get("top")):
        return None, "incoherent-top-identity"
    cells = top_module.get("cells")
    if not isinstance(cells, dict) or len(cells) > MAX_JSON_CELLS:
        return None, "invalid-top-cells"
    histogram: dict[str, int] = {}
    for cell_name, cell in cells.items():
        if (
            not isinstance(cell_name, str)
            or not cell_name
            or len(cell_name) > 4_096
            or not isinstance(cell, dict)
        ):
            return None, "invalid-top-cell"
        cell_type = cell.get("type")
        if not isinstance(cell_type, str) or not cell_type or len(cell_type) > 256:
            return None, "invalid-top-cell-type"
        histogram[cell_type] = histogram.get(cell_type, 0) + 1
        if len(histogram) > MAX_CELL_TYPES:
            return None, "excessive-top-cell-types"
    return {
        "top": requested_top,
        "num_cells": len(cells),
        "num_cells_by_type": histogram,
    }, "parsed"


def _liberty_cells(path: Path) -> tuple[set[str] | None, str]:
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size == 0:
                return None, "empty"
            if opened.st_size > MAX_LIBERTY_PARSE_BYTES:
                return None, "too-large"
            payload = handle.read(MAX_LIBERTY_PARSE_BYTES + 1)
            if len(payload) != opened.st_size:
                return None, "changed-or-incomplete"
    except (FileRecordError, OSError):
        return None, "invalid"
    if _LIBERTY_TRANSITIVE_READ.search(payload):
        return None, "transitive-include-directive"
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        return None, "invalid-utf8"
    cells = {
        match.strip().strip('"')
        for match in re.findall(r"(?m)^\s*cell\s*\(\s*([^()]+?)\s*\)\s*\{", text)
    }
    if not cells or len(cells) > MAX_CELL_TYPES:
        return None, "missing-or-excessive-cells"
    return cells, "parsed"


class YosysDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        abc_binary_path: str | None = None,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        overrides = {}
        if binary_path:
            overrides["yosys"] = binary_path
        if abc_binary_path:
            overrides["abc"] = abc_binary_path
        self.discovery = discovery or DiscoveryManager(
            binary_overrides=overrides or None
        )
        self.binary = self.discovery.find_binary("yosys")
        self.abc_binary = self.discovery.find_binary("abc")

    def rtl_check(
        self,
        sources: list[str | Path],
        output_dir: str | Path,
        *,
        top: str | None = None,
        json_netlist: str | Path | None = None,
        timeout: float = 120.0,
    ) -> dict:
        source_paths = [Path(source).expanduser().resolve() for source in sources]
        out_dir = Path(output_dir).expanduser().resolve()
        script_path = out_dir / "rtl-check.ys"
        netlist_path = (
            Path(json_netlist).expanduser().resolve()
            if json_netlist
            else out_dir / "rtl-check.json"
        )
        info = self.discovery.inspect_tool("yosys")
        tool = tool_record("yosys", path=self.binary, version=info["version"])
        inputs = [file_record(path, kind="hdl-source", role="input") for path in source_paths]

        if not source_paths:
            return result(
                "rtl-check",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="No RTL sources were provided.",
                diagnostics=[diagnostic("error", "input.missing", "Provide at least one RTL source.")],
            )
        missing = [record["path"] for record in inputs if not record["exists"]]
        if missing:
            return result(
                "rtl-check",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="One or more RTL sources do not exist.",
                inputs=inputs,
                diagnostics=[
                    diagnostic("error", "input.missing", f"File not found: {path}") for path in missing
                ],
            )
        output_paths = {script_path, netlist_path}
        invalid_output = (
            out_dir.is_file()
            or bool(output_paths.intersection(source_paths))
            or script_path == netlist_path
            or any(path.exists() and path.is_dir() for path in output_paths)
        )
        if invalid_output:
            return result(
                "rtl-check",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="RTL outputs must be distinct files and must not overwrite source files.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "output.invalid",
                        "Choose an output directory and JSON netlist path distinct from every RTL source.",
                    )
                ],
            )
        if top and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$]*", top):
            return result(
                "rtl-check",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="The requested top-module name is invalid.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "top.invalid", f"Unsupported top module name: {top}")],
            )
        if not self.binary:
            return result(
                "rtl-check",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="Yosys is not available in the selected runtime.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "tool.missing", "Yosys was not found.")],
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        netlist_path.parent.mkdir(parents=True, exist_ok=True)
        produced_netlist = False
        with tempfile.TemporaryDirectory(prefix=".openada-yosys-", dir=netlist_path.parent) as temp_dir:
            temp_netlist = Path(temp_dir) / "netlist.json"
            read_line = "read_verilog -sv " + " ".join(_yosys_quote(path) for path in source_paths)
            hierarchy_line = f"hierarchy -check -top {top}" if top else "hierarchy -auto-top"
            script = "\n".join(
                (
                    read_line,
                    hierarchy_line,
                    "proc",
                    "opt",
                    "check -assert",
                    f"write_json {_yosys_quote(Path('netlist.json'))}",
                    "",
                )
            )
            script_path.write_text(script, encoding="utf-8")
            process = run_process(
                [self.binary, "-q", "-s", str(script_path)],
                cwd=temp_dir,
                timeout=timeout,
            )
            if temp_netlist.is_file():
                shutil.move(str(temp_netlist), str(netlist_path))
                produced_netlist = True

        combined = "\n".join(part for part in (process.stdout, process.stderr) if part)
        errors = [line.strip()[:1_000] for line in combined.splitlines() if "error:" in line.lower()]
        warnings = [line.strip()[:1_000] for line in combined.splitlines() if "warning:" in line.lower()]
        json_valid, json_validation = (
            _validate_json_netlist(netlist_path) if produced_netlist else (False, "missing")
        )
        passed = (
            process.status == "completed"
            and process.exit_code == 0
            and produced_netlist
            and json_valid
            and not errors
        )

        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(
                diagnostic("error", f"execution.{process.status}", process.error or "Yosys did not complete.")
            )
        elif process.exit_code != 0:
            diagnostics.append(
                diagnostic("error", "yosys.nonzero_exit", f"Yosys exited with code {process.exit_code}.")
            )
        if errors:
            diagnostics.append(diagnostic("error", "yosys.error", errors[0]))
        if process.status == "completed" and process.exit_code == 0 and not produced_netlist:
            diagnostics.append(
                diagnostic("error", "artifact.missing", "Yosys completed without writing the JSON netlist.")
            )
        elif produced_netlist and not json_valid:
            if json_validation == "too-large-to-validate":
                diagnostics.append(
                    diagnostic(
                        "error",
                        "artifact.unvalidated",
                        "The Yosys JSON netlist exceeds the bounded "
                        f"{MAX_JSON_PARSE_BYTES}-byte validation limit.",
                    )
                )
            else:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "artifact.invalid_json",
                        "Yosys produced an empty, malformed, or structurally invalid JSON netlist.",
                    )
                )

        artifacts = [file_record(script_path, kind="yosys-script", role="evidence")]
        if produced_netlist:
            artifacts.append(file_record(netlist_path, kind="yosys-json", role="output"))

        if passed:
            engineering_status = "pass"
            summary = "Yosys elaborated the RTL and completed structural checks."
        elif process.status == "completed" and errors:
            engineering_status = "fail"
            summary = "Yosys reported an RTL elaboration or structural-check error."
        else:
            engineering_status = "unknown"
            summary = "The RTL check did not yield enough evidence for an engineering conclusion."

        return result(
            "rtl-check",
            tool=tool,
            execution=process,
            engineering_status=engineering_status,
            summary=summary,
            inputs=inputs,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data={
                "top": top,
                "errors": errors[:100],
                "warnings": warnings[:100],
                "errors_truncated": len(errors) > 100,
                "warnings_truncated": len(warnings) > 100,
                "json_validation": json_validation,
            },
        )

    def synthesize(
        self,
        sources: list[str | Path],
        liberty: str | Path,
        output_dir: str | Path,
        *,
        top: str,
        frontend: str = "verilog",
        include_dirs: list[str | Path] | None = None,
        defines: list[str] | None = None,
        language: str | None = None,
        techmaps: list[str | Path] | None = None,
        dont_use: list[str] | None = None,
        abc_delay_target_ns: float | None = None,
        abc_constraint: str | Path | None = None,
        timeout: float = 300.0,
    ) -> dict:
        """Synthesize and validate one flattened Liberty-mapped ASIC netlist."""
        include_dirs = list(include_dirs or ())
        defines = list(defines or ())
        techmaps = list(techmaps or ())
        dont_use = list(dont_use or ())
        if language is None:
            language = "yosys-sv" if frontend == "verilog" else "1800-2017"
        out_dir = Path(output_dir).expanduser().resolve()
        liberty_path = Path(liberty).expanduser().resolve()
        techmap_paths = [Path(item).expanduser().resolve() for item in techmaps]
        constraint_path = (
            Path(abc_constraint).expanduser().resolve() if abc_constraint else None
        )
        (
            source_paths,
            include_dependencies,
            inputs,
            input_errors,
            unresolved_includes,
        ) = resolve_hdl_inputs(sources, include_dirs)
        for path, kind, role in [
            (liberty_path, "liberty", "technology.liberty"),
            *((path, "yosys-techmap", "synthesis.techmap") for path in techmap_paths),
            *(((constraint_path, "abc-constraint", "synthesis.abc-constraint"),) if constraint_path else ()),
        ]:
            try:
                inputs.append(
                    file_record(path, kind=kind, role=role, maximum_bytes=MAX_LIBERTY_PARSE_BYTES)
                )
            except FileRecordError as exc:
                input_errors.append(str(exc))

        execution_environment, environment_policy = _closed_synthesis_environment(
            self.binary,
            self.abc_binary,
        )
        abc_input_record: dict | None = None
        abc_record_error: str | None = None
        if self.abc_binary:
            try:
                candidate = file_record(
                    self.abc_binary,
                    kind="eda-executable",
                    role="synthesis.abc-executable",
                    maximum_bytes=MAX_ABC_EXECUTABLE_BYTES,
                )
                if candidate.get("exists"):
                    abc_input_record = candidate
                    inputs.append(candidate)
                else:
                    abc_record_error = "the resolved ABC executable is not a regular file"
            except (FileRecordError, OSError, ValueError) as exc:
                abc_record_error = str(exc)
        tool_identity_before = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        abc_identity_before = (
            self.discovery._binary_identity(self.abc_binary)
            if self.abc_binary
            else None
        )
        info = self.discovery.inspect_tool(
            "yosys", probe_environment=execution_environment
        )
        abc_info = self.discovery.inspect_tool(
            "abc", probe_environment=execution_environment
        )
        tool_identity_after_inspection = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        abc_identity_after_inspection = (
            self.discovery._binary_identity(self.abc_binary)
            if self.abc_binary
            else None
        )
        inspected_tool_identity_stable = bool(
            tool_identity_before is not None
            and tool_identity_before == tool_identity_after_inspection
        )
        inspected_abc_identity_stable = bool(
            abc_identity_before is not None
            and abc_identity_before == abc_identity_after_inspection
        )
        abc_inspection_bound = bool(
            abc_info.get("status") == "available"
            and self.abc_binary
            and abc_info.get("binary") == self.abc_binary
            and inspected_abc_identity_stable
            and abc_input_record is not None
            and abc_record_error is None
        )
        tool = tool_record("yosys", path=self.binary, version=info["version"])
        top_valid = isinstance(top, str) and valid_hdl_identifier(top)
        frontend_valid = frontend in {"verilog", "slang"}
        language_valid = (
            frontend == "verilog" and language == "yosys-sv"
        ) or (
            frontend == "slang" and language in {"1800-2017", "1800-2023"}
        )
        abc_delay_ps, delay_target_valid = _abc_delay_picoseconds(
            abc_delay_target_ns
        )
        valid_dont_use = [
            value
            for value in dont_use
            if isinstance(value, str)
            and len(value) <= 256
            and _CELL_GLOB.fullmatch(value)
        ]
        normalized_dont_use = list(dict.fromkeys(valid_dont_use))
        base_data = {
            "protocol": {
                "operation_profile": "openada.operation/logic.synthesize/v1alpha1",
                "assertion_profile": "openada.assertion/synthesized-netlist.valid/v1alpha1",
                "implementation_id": "org.openada.driver.yosys",
                "implementation_version": "1.0.0",
            },
            "top": top if top_valid else None,
            "frontend": frontend if frontend_valid else None,
            "language": language if language_valid else None,
            "ordered_sources": [str(path) for path in source_paths],
            "include_dependencies": [str(path) for path in include_dependencies],
            "unresolved_literal_includes": unresolved_includes[:100],
            "unresolved_literal_includes_truncated": len(unresolved_includes) > 100,
            "inputs_stable": None,
            "dependency_closure_stable": None,
            "tool_identity_stable": None,
            "abc_tool": {
                "name": "abc",
                "path": self.abc_binary,
                "version": (
                    abc_info.get("version") if abc_inspection_bound else None
                ),
                "bytes": (
                    abc_input_record.get("bytes") if abc_input_record else None
                ),
                "sha256": (
                    abc_input_record.get("sha256") if abc_input_record else None
                ),
            },
            "abc_tool_identity_stable": None,
            "environment_policy": environment_policy,
            "changed_inputs": [],
            "changed_inputs_truncated": False,
            "mapping_policy": {
                "flatten": True,
                "set_undefined_to_zero": True,
                "dont_use": normalized_dont_use,
                "abc_delay_target_ns": abc_delay_target_ns if delay_target_valid else None,
                "abc_constraint_supplied": constraint_path is not None,
            },
            "stats": None,
            "inference_stats": None,
            "inference_stats_validation": "not-run",
            "stats_validation": "not-run",
            "mapped_json_validation": "not-run",
            "mapped_structure": None,
            "unmapped_cell_types": [],
            "unmapped_cell_types_truncated": False,
            "mapping_complete": False,
            "warning_count": 0,
            "warnings": [],
            "warnings_truncated": False,
        }
        errors = list(input_errors)
        missing = [record["path"] for record in inputs if not record["exists"]]
        errors.extend(f"input file does not exist: {path}" for path in missing)
        if not source_paths:
            errors.append("provide at least one RTL source")
        if not top_valid:
            errors.append(f"unsupported top-module name: {top}")
        if not frontend_valid:
            errors.append(f"unsupported synthesis frontend: {frontend}")
        if not language_valid:
            errors.append(f"unsupported SystemVerilog language revision: {language}")
        invalid_defines = [
            value
            for value in defines
            if not isinstance(value, str)
            or len(value) > 1_024
            or not _DEFINE.fullmatch(value)
        ]
        errors.extend(f"invalid preprocessor define: {value}" for value in invalid_defines)
        if len(defines) > 256:
            errors.append("no more than 256 preprocessor defines are allowed")
        if len({value for value in defines if isinstance(value, str)}) != len(defines):
            errors.append("preprocessor defines must be unique")
        if frontend == "slang":
            slang_tokens = [str(path) for path in source_paths]
            for item in include_dirs:
                try:
                    slang_tokens.append(str(Path(item).expanduser().resolve()))
                except (OSError, RuntimeError, TypeError, ValueError):
                    continue
            slang_tokens.extend(value for value in defines if isinstance(value, str))
            errors.extend(
                f"Slang v1 frontend paths and defines must use portable token characters: {value}"
                for value in slang_tokens
                if not _SLANG_TOKEN.fullmatch(value)
            )
        errors.extend(
            f"invalid dont-use cell glob: {value}"
            for value in dont_use
            if not isinstance(value, str)
            or len(value) > 256
            or not _CELL_GLOB.fullmatch(value)
        )
        if len(dont_use) > 1_024:
            errors.append("no more than 1024 dont-use patterns are allowed")
        if len({value for value in dont_use if isinstance(value, str)}) != len(dont_use):
            errors.append("dont-use patterns must be unique")
        if not delay_target_valid:
            errors.append(
                "ABC delay target must resolve exactly to 1 through "
                f"{MAX_ABC_DELAY_PS} whole picoseconds"
            )
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            errors.append("timeout must be finite and greater than zero")
        output_paths = {
            out_dir / "synthesize.ys",
            out_dir / "synthesize.log",
            out_dir / "mapped.v",
            out_dir / "mapped.json",
            out_dir / "mapped-stats.json",
            out_dir / "inference-stats.json",
            out_dir / "rtl-inputs.json",
        }
        protected_inputs = source_paths + include_dependencies + [liberty_path, *techmap_paths]
        if constraint_path is not None:
            protected_inputs.append(constraint_path)
        if out_dir.is_file() or output_paths.intersection(protected_inputs):
            errors.append("synthesis outputs must not overwrite an input")
        if any(os.path.lexists(path) for path in output_paths):
            errors.append("synthesis evidence paths must be absent before launch")
        liberty_cell_names, liberty_validation = (
            _liberty_cells(liberty_path) if liberty_path.is_file() else (None, "missing")
        )
        if liberty_path.is_file() and liberty_cell_names is None:
            errors.append(f"Liberty cell inventory is not safely parseable: {liberty_validation}")
        for techmap_path in techmap_paths:
            if not techmap_path.is_file():
                continue
            self_contained, techmap_validation = _techmap_self_contained(techmap_path)
            if not self_contained:
                errors.append(
                    f"technology map must be a self-contained bounded file without "
                    f"include directives: {techmap_path} ({techmap_validation})"
                )
        if errors:
            return result(
                "synthesize",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="The ASIC synthesis request is incomplete or unsafe.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "input.invalid", message) for message in errors[:100]],
                data=base_data,
            )
        if (
            info.get("status") != "available"
            or not self.binary
            or info.get("binary") != self.binary
            or not inspected_tool_identity_stable
        ):
            unavailable_code = "tool.unusable" if self.binary else "tool.missing"
            return result(
                "synthesize",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="Yosys is not inspectable in the selected runtime.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        unavailable_code,
                        "Yosys must have a stable executable identity and an accepted "
                        "version probe before synthesis can run.",
                    )
                ],
                data=base_data,
            )
        if not abc_inspection_bound:
            unavailable_code = "abc.unusable" if self.abc_binary else "abc.missing"
            detail = f" ({abc_record_error})" if abc_record_error else ""
            return result(
                "synthesize",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="The external ABC mapper is not inspectable in the selected runtime.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        unavailable_code,
                        "ABC must have an exact stable executable path, a bounded "
                        "content digest, and an accepted '-c version' probe before "
                        f"synthesis can run{detail}.",
                    )
                ],
                data=base_data,
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        script_path = out_dir / "synthesize.ys"
        transcript_path = out_dir / "synthesize.log"
        final_netlist = out_dir / "mapped.v"
        final_json = out_dir / "mapped.json"
        final_stats = out_dir / "mapped-stats.json"
        final_inference_stats = out_dir / "inference-stats.json"
        final_dependencies = out_dir / "rtl-inputs.json"
        liberty_arg = _yosys_quote(liberty_path)
        lines = [f"read_liberty -lib -ignore_miss_dir -setattr blackbox {liberty_arg}"]
        if frontend == "slang":
            parts = ["read_slang", "--std", language, "--top", top]
            for directory in include_dirs:
                parts.extend(("-I", str(Path(directory).expanduser().resolve())))
            for define in defines:
                parts.extend(("-D", define))
            parts.extend(str(path) for path in source_paths)
            lines.append(" ".join(parts))
        else:
            parts = ["read_verilog", "-sv"]
            parts.extend(f"-I{_yosys_quote(Path(directory).expanduser().resolve())}" for directory in include_dirs)
            parts.extend(f"-D{define}" for define in defines)
            parts.extend(_yosys_quote(path) for path in source_paths)
            lines.append(" ".join(parts))
        lines.extend(
            (
                f"hierarchy -check -top {top}",
                f"synth -top {top} -flatten -noabc",
                f"tee -o inference-stats.json stat -json -top {top}",
            )
        )
        lines.extend(f"techmap -map {_yosys_quote(path)}" for path in techmap_paths)
        dont_use_args = "".join(f" -dont_use {_yosys_quote(Path(value))}" for value in dont_use)
        lines.extend((f"dfflibmap -liberty {liberty_arg}{dont_use_args}", "opt", "setundef -zero"))
        abc = (
            f"abc -exe {_yosys_quote(Path(self.abc_binary))} "
            f"-liberty {liberty_arg}{dont_use_args}"
        )
        if constraint_path:
            abc += f" -constr {_yosys_quote(constraint_path)}"
        if abc_delay_ps is not None:
            abc += f" -D {abc_delay_ps}"
        lines.extend(
            (
                abc,
                "splitnets",
                "opt_clean -purge",
                "check -assert",
                f"tee -o mapped-stats.json stat -json -top {top} -liberty {liberty_arg}",
                f"write_verilog -noattr -noexpr {_yosys_quote(Path('mapped.v'))}",
                f"write_json {_yosys_quote(Path('mapped.json'))}",
                "",
            )
        )
        dependencies_text = (
            json.dumps(
                {
                    "schema": "openada.hdl-inputs/v1",
                    "ordered_sources": [str(path) for path in source_paths],
                    "resolved_literal_includes": [str(path) for path in include_dependencies],
                    "unresolved_literal_includes": unresolved_includes,
                    "declared_include_directories": [
                        str(Path(item).expanduser().resolve()) for item in include_dirs
                    ],
                    "defines": defines,
                    "input_records": inputs,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        generated_artifacts: list[dict] = []
        try:
            script_before = _write_exclusive_evidence(
                script_path,
                "\n".join(lines),
                kind="yosys-script",
                role="synthesis.script",
                maximum_bytes=MAX_HDL_PARSE_BYTES,
            )
            generated_artifacts.append(script_before)
            dependencies_before = _write_exclusive_evidence(
                final_dependencies,
                dependencies_text,
                kind="hdl-input-manifest",
                role="rtl.dependencies",
                maximum_bytes=MAX_HDL_PARSE_BYTES,
            )
            generated_artifacts.append(dependencies_before)
        except (FileRecordError, OSError, UnicodeError, ValueError) as exc:
            return result(
                "synthesize",
                tool=tool,
                execution=static_execution("failed"),
                engineering_status="unknown",
                summary="The synthesis evidence files could not be created safely.",
                inputs=inputs,
                artifacts=generated_artifacts,
                diagnostics=[
                    diagnostic(
                        "error",
                        "artifact.unsafe_capture",
                        f"Generated synthesis evidence creation failed: {exc}",
                    )
                ],
                data=base_data,
            )

        native_records: dict[str, dict] = {}
        native_capture_validation: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix=".openada-synthesize-", dir=out_dir) as work:
            process = run_process(
                [self.binary, "-Q", "-T", "-s", str(script_path)],
                cwd=work,
                timeout=timeout,
                env=execution_environment,
                capture_limit_bytes=15 * 1024 * 1024,
            )
            work_path = Path(work)
            for name, destination, maximum_bytes in (
                ("mapped.v", final_netlist, MAX_NATIVE_NETLIST_BYTES),
                ("mapped.json", final_json, MAX_SYNTHESIS_JSON_BYTES),
                ("mapped-stats.json", final_stats, MAX_SYNTHESIS_JSON_BYTES),
                (
                    "inference-stats.json",
                    final_inference_stats,
                    MAX_SYNTHESIS_JSON_BYTES,
                ),
            ):
                record, validation = _capture_native_output(
                    work_path / name,
                    destination,
                    maximum_bytes=maximum_bytes,
                )
                native_capture_validation[name] = validation
                if record is not None:
                    native_records[name] = record
        transcript_before: dict | None = None
        transcript_error: str | None = None
        try:
            write_process_transcript(transcript_path, process)
            transcript_before = file_record(
                transcript_path,
                kind="yosys-log",
                role="synthesis.log",
                maximum_bytes=32 * 1024 * 1024,
            )
            if not transcript_before.get("exists"):
                transcript_error = "the transcript is absent after creation"
        except (FileRecordError, OSError, UnicodeError, ValueError) as exc:
            transcript_error = str(exc)

        changed_inputs = changed_input_paths(
            inputs,
            maximum_bytes_by_kind={
                "hdl-source": MAX_HDL_PARSE_BYTES,
                "hdl-include": MAX_HDL_PARSE_BYTES,
                "liberty": MAX_LIBERTY_PARSE_BYTES,
                "yosys-techmap": MAX_LIBERTY_PARSE_BYTES,
                "abc-constraint": MAX_LIBERTY_PARSE_BYTES,
                "eda-executable": MAX_ABC_EXECUTABLE_BYTES,
            },
        )
        inputs_stable = not changed_inputs
        dependency_closure_stable, closure_changes = hdl_closure_stability(
            sources,
            include_dirs,
            expected_sources=source_paths,
            expected_dependencies=include_dependencies,
            expected_unresolved=unresolved_includes,
        )
        base_data["inputs_stable"] = inputs_stable
        base_data["dependency_closure_stable"] = dependency_closure_stable
        tool_identity_stable = bool(
            inspected_tool_identity_stable
            and tool_identity_before == self.discovery._binary_identity(self.binary)
        )
        base_data["tool_identity_stable"] = tool_identity_stable
        abc_tool_identity_stable = bool(
            abc_inspection_bound
            and abc_identity_before
            == self.discovery._binary_identity(self.abc_binary)
            and self.abc_binary not in changed_inputs
        )
        base_data["abc_tool_identity_stable"] = abc_tool_identity_stable
        base_data["changed_inputs"] = changed_inputs[:100]
        base_data["changed_inputs_truncated"] = len(changed_inputs) > 100
        generated_evidence_stable = True
        for before, path in (
            (script_before, script_path),
            (dependencies_before, final_dependencies),
        ):
            try:
                after = file_record(
                    path,
                    kind=before["kind"],
                    role=before["role"],
                    maximum_bytes=MAX_HDL_PARSE_BYTES,
                )
            except (FileRecordError, OSError, ValueError):
                generated_evidence_stable = False
                break
            if any(
                before.get(key) != after.get(key)
                for key in ("exists", "bytes", "sha256")
            ):
                generated_evidence_stable = False
                break

        native = "\n".join((process.stdout, process.stderr))
        native_errors = [
            line.strip()[:1_000]
            for line in native.splitlines()
            if "error:" in line.lower()
        ]
        engineering_native_errors = [
            message for message in native_errors if _ENGINEERING_ERROR.fullmatch(message)
        ]
        unclassified_native_errors = [
            message for message in native_errors if message not in engineering_native_errors
        ]
        native_warnings = [line.strip()[:1_000] for line in native.splitlines() if "warning:" in line.lower()]
        process_capture_complete = (
            not process.stdout_truncated
            and not process.stderr_truncated
            and process.stdout_utf8_valid
            and process.stderr_utf8_valid
            and transcript_before is not None
            and transcript_error is None
        )
        stats, stats_validation = (
            _load_synthesis_stats(final_stats, requested_top=top)
            if "mapped-stats.json" in native_records
            else (None, "missing")
        )
        mapped_structure, json_validation = (
            _load_mapped_structure(final_json, requested_top=top)
            if "mapped.json" in native_records
            else (None, "missing")
        )
        inference_stats, inference_stats_validation = (
            _load_synthesis_stats(final_inference_stats, requested_top=top)
            if "inference-stats.json" in native_records
            else (None, "missing")
        )
        if mapped_structure is not None and stats is not None and (
            mapped_structure["num_cells"] != stats["num_cells"]
            or mapped_structure["num_cells_by_type"] != stats["num_cells_by_type"]
        ):
            json_validation = "mapped-stats-mismatch"
        cell_types = set(stats.get("num_cells_by_type", {})) if stats else set()
        unmapped = sorted(
            name for name in cell_types if name.startswith("$") or name not in (liberty_cell_names or set())
        )
        netlist_nonempty = bool(
            native_records.get("mapped.v")
            and native_records["mapped.v"].get("bytes", 0) > 0
        )
        mapped_outputs_complete = bool(
            stats
            and inference_stats
            and mapped_structure
            and json_validation == "parsed"
            and netlist_nonempty
            and set(native_records) == {
                "mapped.v",
                "mapped.json",
                "mapped-stats.json",
                "inference-stats.json",
            }
        )
        structural_mapping_failure = bool(
            mapped_outputs_complete
            and stats
            and (
                stats["num_processes"] != 0
                or stats["num_memories"] != 0
                or stats["num_memory_bits"] != 0
                or unmapped
            )
        )
        native_capture_safe = all(
            validation in {"captured", "missing"}
            for validation in native_capture_validation.values()
        )

        artifacts: list[dict] = []
        artifact_records_stable = True

        def retain_current(
            before: dict,
            path: Path,
            *,
            kind: str,
            role: str,
            maximum_bytes: int,
        ) -> None:
            nonlocal artifact_records_stable
            try:
                current = file_record(
                    path,
                    kind=kind,
                    role=role,
                    maximum_bytes=maximum_bytes,
                )
            except (FileRecordError, OSError, ValueError):
                artifact_records_stable = False
                return
            if (
                not current.get("exists")
                or any(
                    before.get(key) != current.get(key)
                    for key in ("bytes", "sha256")
                )
            ):
                artifact_records_stable = False
                return
            artifacts.append(current)

        retain_current(
            script_before,
            script_path,
            kind="yosys-script",
            role="synthesis.script",
            maximum_bytes=MAX_HDL_PARSE_BYTES,
        )
        if transcript_before is not None:
            retain_current(
                transcript_before,
                transcript_path,
                kind="yosys-log",
                role="synthesis.log",
                maximum_bytes=32 * 1024 * 1024,
            )
        else:
            artifact_records_stable = False
        for name, path, kind, role, maximum_bytes in (
            (
                "inference-stats.json",
                final_inference_stats,
                "yosys-stat-json",
                "synthesis.inference-statistics",
                MAX_SYNTHESIS_JSON_BYTES,
            ),
            (
                "mapped-stats.json",
                final_stats,
                "yosys-stat-json",
                "synthesis.statistics",
                MAX_SYNTHESIS_JSON_BYTES,
            ),
            (
                "mapped.v",
                final_netlist,
                "verilog-netlist",
                "synthesis.netlist",
                MAX_NATIVE_NETLIST_BYTES,
            ),
            (
                "mapped.json",
                final_json,
                "yosys-json",
                "synthesis.netlist-structure",
                MAX_SYNTHESIS_JSON_BYTES,
            ),
        ):
            if name in native_records:
                retain_current(
                    native_records[name],
                    path,
                    kind=kind,
                    role=role,
                    maximum_bytes=maximum_bytes,
                )
        retain_current(
            dependencies_before,
            final_dependencies,
            kind="hdl-input-manifest",
            role="rtl.dependencies",
            maximum_bytes=MAX_HDL_PARSE_BYTES,
        )

        trustworthy_execution_evidence = bool(
            process.status == "completed"
            and process_capture_complete
            and inputs_stable
            and dependency_closure_stable
            and tool_identity_stable
            and abc_tool_identity_stable
            and generated_evidence_stable
            and artifact_records_stable
            and native_capture_safe
        )
        mapping_complete = bool(
            trustworthy_execution_evidence
            and process.exit_code == 0
            and not native_errors
            and mapped_outputs_complete
            and not structural_mapping_failure
        )
        complete = (
            mapping_complete
        )
        if complete:
            status = "pass"
            summary = "Yosys produced and independently validated a complete Liberty-mapped ASIC netlist."
        elif (
            trustworthy_execution_evidence
            and (
                bool(engineering_native_errors)
                or structural_mapping_failure
            )
        ):
            status = "fail"
            summary = "Yosys reported or produced evidence of an incomplete ASIC technology mapping."
        else:
            status = "unknown"
            summary = "ASIC synthesis did not yield complete trustworthy mapped-netlist evidence."

        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(diagnostic("error", f"execution.{process.status}", process.error or "Yosys did not complete."))
        if (
            process.status == "completed"
            and process.exit_code not in (0, None)
            and not engineering_native_errors
        ):
            diagnostics.append(diagnostic("error", "yosys.unclassified_exit", f"Yosys exited with code {process.exit_code} without a recognized error."))
        if not process_capture_complete:
            diagnostics.append(diagnostic("error", "evidence.incomplete", "Yosys output was truncated or was not valid UTF-8."))
        if transcript_error is not None:
            diagnostics.append(
                diagnostic(
                    "error",
                    "artifact.unsafe_capture",
                    f"The Yosys transcript could not be retained safely: {transcript_error}",
                )
            )
        if changed_inputs:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.changed",
                    "A synthesis input changed while Yosys was running: "
                    + ", ".join(changed_inputs[:10]),
                )
            )
        if not dependency_closure_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.dependency_closure_changed",
                    "The literal HDL dependency closure changed while Yosys was "
                    "running: " + "; ".join(closure_changes[:10]),
                )
            )
        if not tool_identity_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "tool.changed",
                    "The version-validated Yosys executable identity changed during synthesis.",
                )
            )
        if not abc_tool_identity_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "abc.changed",
                    "The version- and digest-validated external ABC executable "
                    "changed during synthesis.",
                )
            )
        if not generated_evidence_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "artifact.changed",
                    "The generated synthesis script or dependency manifest changed "
                    "during execution.",
                )
            )
        if not artifact_records_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "artifact.changed",
                    "One or more retained synthesis artifacts changed during validation.",
                )
            )
        for name, validation in native_capture_validation.items():
            if validation not in {"captured", "missing"}:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "artifact.unsafe_capture",
                        f"Native output {name} was rejected during stable bounded "
                        f"regular-file capture: {validation}.",
                    )
                )
        if stats is None:
            diagnostics.append(diagnostic("error", "artifact.invalid_stats", f"Mapped statistics validation: {stats_validation}."))
        if inference_stats is None:
            diagnostics.append(diagnostic("error", "artifact.invalid_stats", f"Inference statistics validation: {inference_stats_validation}."))
        if mapped_structure is None or json_validation != "parsed":
            diagnostics.append(diagnostic("error", "artifact.invalid_json", f"Mapped JSON validation: {json_validation}."))
        if not netlist_nonempty:
            diagnostics.append(
                diagnostic(
                    "error",
                    "artifact.invalid_netlist",
                    "The fresh mapped Verilog netlist is missing or empty.",
                )
            )
        if unmapped:
            diagnostics.append(diagnostic("error", "synthesis.unmapped_cells", "Mapped netlist contains cell types absent from the declared Liberty: " + ", ".join(unmapped[:20])))
        diagnostics.extend(diagnostic("error", "yosys.error", message) for message in engineering_native_errors[:20])
        diagnostics.extend(
            diagnostic("error", "yosys.unclassified_error", message)
            for message in unclassified_native_errors[:20]
        )
        diagnostics.extend(diagnostic("warning", "yosys.warning", message) for message in native_warnings[:20])
        def normalized_statistics(value: dict | None) -> dict | None:
            if value is None:
                return None
            return {
                key: value.get(key)
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
        normalized_stats = normalized_statistics(stats)
        normalized_inference_stats = normalized_statistics(inference_stats)
        return result(
            "synthesize",
            tool=tool,
            execution=process,
            engineering_status=status,
            summary=summary,
            inputs=inputs,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data={
                **base_data,
                "stats": normalized_stats,
                "inference_stats": normalized_inference_stats,
                "inference_stats_validation": inference_stats_validation,
                "stats_validation": stats_validation,
                "mapped_json_validation": json_validation,
                "mapped_structure": mapped_structure,
                "unmapped_cell_types": unmapped[:100],
                "unmapped_cell_types_truncated": len(unmapped) > 100,
                "mapping_complete": mapping_complete,
                "warning_count": len(native_warnings),
                "warnings": native_warnings[:100],
                "warnings_truncated": len(native_warnings) > 100,
            },
        )
