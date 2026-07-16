"""OpenSTA setup/hold timing analysis with bounded native evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

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
from .hdl import valid_hdl_identifier, write_process_transcript


MAX_NETLIST_BYTES = 512 * 1024 * 1024
MAX_LIBERTY_BYTES = 512 * 1024 * 1024
MAX_SDC_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_CHECK_SETUP_BYTES = 4 * 1024 * 1024
MAX_PATHS = 4_096
MAX_ENDPOINT_CHARS = 1_000
MAX_PATH_GROUP_CHARS = 4_096
MAX_SDC_COMMANDS = 4_096
MAX_SDC_LINE_CHARS = 16_384
MAX_INPUT_SCAN_OVERLAP_BYTES = 512
_ENVIRONMENT_POLICY = "closed-opensta-runtime-v1"

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_UNIT_NUMBER = r"\d+(?:\.\d+)?"
_TIME_SCALES = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "ns": 1e-9,
    "ps": 1e-12,
    "fs": 1e-15,
}
_OUTPUTS = {
    "check-setup.txt": ("opensta-check-setup", "timing.constraint-check"),
    "setup-paths.json": ("opensta-report-json", "timing.setup-paths"),
    "hold-paths.json": ("opensta-report-json", "timing.hold-paths"),
}
_SDC_TOP_COMMANDS = {
    "create_clock",
    "create_generated_clock",
    "current_design",
    "group_path",
    "set",
    "set_case_analysis",
    "set_clock_groups",
    "set_clock_latency",
    "set_clock_transition",
    "set_clock_uncertainty",
    "set_disable_timing",
    "set_driving_cell",
    "set_false_path",
    "set_ideal_network",
    "set_input_delay",
    "set_load",
    "set_max_capacitance",
    "set_max_delay",
    "set_max_fanout",
    "set_max_transition",
    "set_min_delay",
    "set_multicycle_path",
    "set_output_delay",
    "set_propagated_clock",
}
_SDC_QUERY_COMMANDS = {
    "all_clocks",
    "all_inputs",
    "all_outputs",
    "all_registers",
    "concat",
    "expr",
    "filter_collection",
    "get_cells",
    "get_clocks",
    "get_nets",
    "get_pins",
    "get_ports",
    "lindex",
    "list",
}
_SDC_COMMAND = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b")
_SDC_VARIABLE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
_SDC_SET = re.compile(r"^set[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+(.+?)\s*$")
_SDC_RESERVED_VARIABLES = {
    "argc",
    "argv",
    "argv0",
    "env",
    "errorCode",
    "errorInfo",
    "tcl_interactive",
    "tcl_library",
    "tcl_patchLevel",
    "tcl_platform",
    "tcl_version",
}
_SDC_RESERVED_VARIABLE_PREFIXES = ("auto_", "sta_", "tcl_")
_VERILOG_TRANSITIVE_READ = re.compile(rb"`include\b", re.IGNORECASE)
_LIBERTY_TRANSITIVE_READ = re.compile(
    rb"(?<![A-Za-z0-9_])(?:include|include_file)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _tcl_quote(value: str | Path) -> str:
    """Return one Tcl word without permitting substitution or line injection."""
    text = str(value)
    if _has_ascii_control(text):
        raise ValueError("OpenSTA paths and identifiers may not contain ASCII controls")
    escaped = (
        text.replace("\\", "\\\\")
        .replace("$", "\\$")
        .replace("[", "\\[")
        .replace('"', '\\"')
    )
    return f'"{escaped}"'


def _has_ascii_control(value: str | Path) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in str(value))


def _closed_opensta_environment(binary: str | None) -> dict[str, str]:
    """Build the fixed OpenSTA probe and execution runtime without ambient hooks."""
    search_directories: list[str] = []
    if binary:
        search_directories.append(str(Path(binary).parent))
    search_directories.extend(
        directory for directory in os.defpath.split(os.pathsep) if directory
    )
    environment = {
        "PATH": os.pathsep.join(dict.fromkeys(search_directories)),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
    return environment


def _read_bounded(
    path: Path, maximum_bytes: int
) -> tuple[bytes | None, str, str | None]:
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size > maximum_bytes:
                return None, "too-large", None
            payload = handle.read(maximum_bytes + 1)
            if len(payload) != opened.st_size:
                return None, "changed-or-incomplete", None
    except (FileRecordError, OSError):
        return None, "missing-or-unsafe", None
    return payload, "captured", hashlib.sha256(payload).hexdigest()


def _scan_self_contained_input(
    path: Path,
    maximum_bytes: int,
    transitive_read: re.Pattern[bytes],
) -> tuple[str, str | None]:
    """Hash a stable input while rejecting obvious native include constructs."""
    digest = hashlib.sha256()
    observed = 0
    tail = b""
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size == 0:
                return "empty", None
            if opened.st_size > maximum_bytes:
                return "too-large", None
            while chunk := handle.read(1024 * 1024):
                observed += len(chunk)
                if observed > maximum_bytes:
                    return "too-large", None
                digest.update(chunk)
                window = tail + chunk
                if transitive_read.search(window):
                    return "transitive-include-directive", None
                tail = window[-MAX_INPUT_SCAN_OVERLAP_BYTES:]
            if observed != opened.st_size:
                return "changed-or-incomplete", None
    except (FileRecordError, OSError):
        return "missing-or-unsafe", None
    return "self-contained", digest.hexdigest()


def _write_exclusive(path: Path, payload: bytes, maximum_bytes: int) -> None:
    """Create one bounded evidence file without following or replacing a path."""
    if len(payload) > maximum_bytes:
        raise ValueError(f"evidence payload exceeds {maximum_bytes} bytes")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("exclusive evidence write did not make progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sdc_variable_name(token: str) -> str:
    return token[2:-1] if token.startswith("${") else token[1:]


def _sdc_variable_is_reserved(name: str) -> bool:
    return name in _SDC_RESERVED_VARIABLES or name.startswith(
        _SDC_RESERVED_VARIABLE_PREFIXES
    )


def _load_safe_sdc(
    path: Path, top: str
) -> tuple[bytes | None, str, str | None]:
    """Accept a closed, declarative one-command-per-line SDC subset."""
    raw, capture, digest = _read_bounded(path, MAX_SDC_BYTES)
    if raw is None:
        return None, capture, digest
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return None, "invalid-utf8", digest
    if not text.strip():
        return None, "empty", digest

    command_count = 0
    assigned_variables: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        command_count += 1
        if command_count > MAX_SDC_COMMANDS:
            return None, "too-many-commands", digest
        if len(line) > MAX_SDC_LINE_CHARS:
            return None, f"line-{line_number}-too-long", digest
        if any(ord(character) < 32 and character != "\t" for character in line):
            return None, f"line-{line_number}-control-character", digest
        if any(character in line for character in (";", "\\", "{", "}")):
            return None, f"line-{line_number}-unsupported-tcl-syntax", digest

        quote_open = False
        brackets: list[int] = []
        for index, character in enumerate(line):
            if character == '"':
                quote_open = not quote_open
            elif character == "[":
                brackets.append(index)
            elif character == "]":
                if not brackets:
                    return None, f"line-{line_number}-unbalanced-bracket", digest
                start = brackets.pop()
                nested = line[start + 1 : index].strip()
                match = _SDC_COMMAND.match(nested)
                if match is None or match.group(1) not in _SDC_QUERY_COMMANDS:
                    return None, f"line-{line_number}-unsafe-subcommand", digest
        if quote_open:
            return None, f"line-{line_number}-unbalanced-quote", digest
        if brackets:
            return None, f"line-{line_number}-unbalanced-bracket", digest

        for variable in re.finditer(r"\$", line):
            variable_match = _SDC_VARIABLE.match(line, variable.start())
            if variable_match is None or (
                variable_match.end() < len(line)
                and line[variable_match.end()] in "(:"
            ):
                return None, f"line-{line_number}-unsafe-variable", digest
            variable_name = _sdc_variable_name(variable_match.group(0))
            if variable_name not in assigned_variables:
                return None, f"line-{line_number}-undeclared-variable", digest

        match = _SDC_COMMAND.match(stripped)
        if match is None or match.group(1) not in _SDC_TOP_COMMANDS:
            return None, f"line-{line_number}-command-not-allowed", digest
        command = match.group(1)
        if command == "set":
            set_match = _SDC_SET.fullmatch(stripped)
            if set_match is None:
                return None, f"line-{line_number}-unsafe-set-variable", digest
            variable_name = set_match.group(1)
            if _sdc_variable_is_reserved(variable_name):
                return None, f"line-{line_number}-reserved-set-variable", digest
            assigned_variables.add(variable_name)
        if command == "current_design" and re.fullmatch(
            rf'current_design\s+"?{re.escape(top)}"?', stripped
        ) is None:
            return None, f"line-{line_number}-current-design-mismatch", digest
    if command_count == 0:
        return None, "no-commands", digest
    return raw, "parsed-safe-subset", digest


def _load_check_setup(path: Path) -> tuple[bool | None, str, str | None]:
    raw, capture, digest = _read_bounded(path, MAX_CHECK_SETUP_BYTES)
    if raw is None:
        return None, capture, digest
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return None, "invalid-utf8", digest
    return (
        not bool(text.strip()),
        "complete" if not text.strip() else "issues-reported",
        digest,
    )


def _load_path_report(
    path: Path, expected_path_type: str
) -> tuple[dict[str, Any] | None, str, str | None]:
    raw, capture, digest = _read_bounded(path, MAX_REPORT_BYTES)
    if raw is None:
        return None, capture, digest
    if not raw:
        return None, "empty", digest
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return None, "malformed-json", digest
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list) or not checks or len(checks) > MAX_PATHS:
        return None, "missing-or-excessive-checks", digest

    normalized: list[dict[str, Any]] = []
    for item in checks:
        if not isinstance(item, dict) or item.get("path_type") != expected_path_type:
            return None, "invalid-path-type", digest
        slack = item.get("slack")
        if (
            not isinstance(slack, (int, float))
            or isinstance(slack, bool)
            or not math.isfinite(slack)
        ):
            return None, "invalid-slack", digest
        names: dict[str, str | None] = {}
        for key in ("type", "path_group", "startpoint", "endpoint"):
            value = item.get(key)
            maximum = (
                MAX_PATH_GROUP_CHARS if key in {"type", "path_group"} else MAX_ENDPOINT_CHARS
            )
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > maximum
            ):
                return None, f"invalid-{key.replace('_', '-')}", digest
            names[key] = value
        if names["startpoint"] is None or names["endpoint"] is None:
            return None, "missing-endpoints", digest
        normalized.append(
            {
                "startpoint": names["startpoint"],
                "endpoint": names["endpoint"],
                "path_group": names["path_group"],
                "slack_s": float(slack),
            }
        )

    critical = min(normalized, key=lambda item: item["slack_s"])
    return {
        "path_count": len(normalized),
        "critical_path": critical,
        "minimum_reported_slack_s": critical["slack_s"],
    }, "parsed", digest


def _marker_block(text: str, name: str) -> str | None:
    begin = f"OPENADA_{name}_BEGIN"
    end = f"OPENADA_{name}_END"
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    start = text.find(begin) + len(begin)
    finish = text.find(end, start)
    if finish < start:
        return None
    return text[start:finish]


def _parse_metrics(text: str) -> tuple[dict[str, Any] | None, str]:
    units = _marker_block(text, "UNITS")
    if units is None:
        return None, "missing-or-duplicate-units-markers"
    unit_matches = re.findall(
        rf"(?im)^\s*time\s+({_UNIT_NUMBER})\s*(s|ms|us|ns|ps|fs)\s*$",
        units,
    )
    if len(unit_matches) != 1:
        return None, "unparseable-time-unit"
    coefficient_text = unit_matches[0][0]
    coefficient = float(coefficient_text)
    unit_name = unit_matches[0][1].lower()
    if not math.isfinite(coefficient) or coefficient <= 0:
        return None, "invalid-time-unit"
    unit_seconds = coefficient * _TIME_SCALES[unit_name]

    observed: dict[str, float] = {}
    specifications = {
        "setup_wns": ("SETUP_WNS", "worst slack", "max"),
        "setup_tns": ("SETUP_TNS", "tns", "max"),
        "hold_wns": ("HOLD_WNS", "worst slack", "min"),
        "hold_tns": ("HOLD_TNS", "tns", "min"),
    }
    for key, (marker, label, mode) in specifications.items():
        block = _marker_block(text, marker)
        if block is None:
            return None, f"missing-or-duplicate-{key.replace('_', '-')}-markers"
        matches = re.findall(
            rf"(?im)^\s*{re.escape(label)}\s+{mode}\s+({_NUMBER})\s*$",
            block,
        )
        if len(matches) != 1:
            return None, f"unparseable-{key.replace('_', '-')}"
        value = float(matches[0]) * unit_seconds
        if not math.isfinite(value):
            return None, f"nonfinite-{key.replace('_', '-')}"
        observed[f"{key}_s"] = value

    if text.count("OPENADA_ANALYSIS_COMPLETE") != 1:
        return None, "missing-or-duplicate-completion-marker"
    return {
        "time_unit": f"{coefficient_text}{unit_name}",
        "time_unit_seconds": unit_seconds,
        **observed,
    }, "parsed"


def _metric_consistency(metrics: dict[str, Any]) -> tuple[bool, str]:
    tolerance = metrics["time_unit_seconds"] * 5e-9
    for mode in ("setup", "hold"):
        wns = metrics[f"{mode}_wns_s"]
        tns = metrics[f"{mode}_tns_s"]
        if tns > tolerance:
            return False, f"{mode} TNS is positive"
        if wns >= 0 and abs(tns) > tolerance:
            return False, f"{mode} TNS is nonzero despite nonnegative WNS"
        if wns < 0 and tns > -tolerance:
            return False, f"{mode} TNS is zero despite negative WNS"
    return True, "consistent"


def _slack_agrees(metric: float, report: dict[str, Any], time_unit_seconds: float) -> bool:
    reported = report["minimum_reported_slack_s"]
    # OpenSTA JSON paths use four significant digits while report_worst_slack
    # is requested with nine digits. Bound the cross-format rounding allowance.
    tolerance = max(time_unit_seconds * 5e-4, abs(metric) * 5e-4, 1e-18)
    same_violation_class = (metric < 0) == (reported < 0)
    return same_violation_class and math.isclose(
        metric, reported, rel_tol=0.0, abs_tol=tolerance
    )


class OpenSTADriver:
    """Run one single-corner, ideal-interconnect setup/hold timing analysis."""

    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"sta": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("sta")

    def timing_analyze(
        self,
        netlist: str | Path,
        liberty: str | Path,
        sdc: str | Path,
        output_dir: str | Path,
        *,
        top: str,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Analyze setup and hold timing against one Liberty/SDC corner."""
        top_value = (
            top
            if isinstance(top, str) and len(top) <= 256 and valid_hdl_identifier(top)
            else None
        )
        base_data: dict[str, Any] = {
            "protocol": {
                "operation_profile": "openada.operation/timing.analyze/v1alpha1",
                "assertion_profile": "openada.assertion/timing.constraints-satisfied/v1alpha1",
                "implementation_id": "org.openada.driver.opensta",
                "implementation_version": "1.0.0",
            },
            "top": top_value,
            "analysis_model": "single_corner_ideal_interconnect_no_spef",
            "interconnect_model": "ideal",
            "spef_supplied": False,
            "signoff_level": False,
            "environment_policy": _ENVIRONMENT_POLICY,
            "sdc_policy": "openada-sdc-v1",
            "sdc_validation": "not-run",
            "netlist_validation": "not-run",
            "liberty_validation": "not-run",
            "limitations": [
                "No SPEF parasitics are read; interconnect is ideal.",
                "Only one Liberty/SDC corner is analyzed; this is not MCMM signoff.",
                "Mapped Verilog and Liberty inputs must be self-contained; "
                "transitive include directives are rejected.",
            ],
            "constraints_complete": None,
            "constraint_check_validation": "not-run",
            "reports_complete": False,
            "metrics_validation": "not-run",
            "metric_consistency": "not-run",
            "path_reports_agree_with_wns": False,
            "inputs_stable": None,
            "tool_identity_stable": None,
            "changed_inputs": [],
            "setup": None,
            "hold": None,
            "timing_constraints_satisfied": None,
        }
        try:
            netlist_path = Path(netlist).expanduser().resolve()
            liberty_path = Path(liberty).expanduser().resolve()
            sdc_path = Path(sdc).expanduser().resolve()
            out_dir = Path(output_dir).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return result(
                "timing-analyze",
                tool=tool_record("sta", path=self.binary, version=None),
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="The OpenSTA timing request contains an invalid path.",
                diagnostics=[diagnostic("error", "path.invalid", str(exc))],
                data=base_data,
            )

        declarations = (
            (netlist_path, "verilog-netlist", "timing.netlist", MAX_NETLIST_BYTES),
            (liberty_path, "liberty", "technology.liberty", MAX_LIBERTY_BYTES),
            (sdc_path, "sdc", "timing.sdc", MAX_SDC_BYTES),
        )
        inputs: list[dict[str, Any]] = []
        input_errors = [
            f"{role} canonical path contains an ASCII control character or DEL"
            for path, _kind, role, _maximum in declarations
            if _has_ascii_control(path)
        ]
        if _has_ascii_control(out_dir):
            input_errors.append(
                "timing output canonical path contains an ASCII control character or DEL"
            )
        for path, kind, role, maximum in declarations:
            try:
                inputs.append(
                    file_record(path, kind=kind, role=role, maximum_bytes=maximum)
                )
            except FileRecordError as exc:
                input_errors.append(str(exc))
        input_errors.extend(
            f"input file does not exist or is not a regular file: {record['path']}"
            for record in inputs
            if not record["exists"]
        )
        records_by_role = {record["role"]: record for record in inputs}
        for path, role, field, maximum, transitive_read in (
            (
                netlist_path,
                "timing.netlist",
                "netlist_validation",
                MAX_NETLIST_BYTES,
                _VERILOG_TRANSITIVE_READ,
            ),
            (
                liberty_path,
                "technology.liberty",
                "liberty_validation",
                MAX_LIBERTY_BYTES,
                _LIBERTY_TRANSITIVE_READ,
            ),
        ):
            record = records_by_role.get(role)
            if record is None or not record.get("exists"):
                continue
            validation, scanned_digest = _scan_self_contained_input(
                path, maximum, transitive_read
            )
            base_data[field] = validation
            if validation != "self-contained":
                input_errors.append(
                    f"{role} is not a stable self-contained input: {validation}"
                )
            elif scanned_digest != record.get("sha256"):
                input_errors.append(
                    f"{role} changed while its self-contained syntax was validated"
                )
        sdc_payload: bytes | None = None
        if len(inputs) == len(declarations) and inputs[-1]["exists"] and top_value:
            sdc_payload, sdc_validation, sdc_digest = _load_safe_sdc(
                sdc_path, top_value
            )
            base_data["sdc_validation"] = sdc_validation
            if sdc_payload is None:
                input_errors.append(
                    "SDC is outside the openada-sdc-v1 declarative subset: "
                    + sdc_validation
                )
            elif sdc_digest != inputs[-1].get("sha256"):
                input_errors.append("the SDC changed while its safe subset was validated")
        if len({netlist_path, liberty_path, sdc_path}) != 3:
            input_errors.append("the netlist, Liberty, and SDC inputs must be distinct")
        if (
            not isinstance(top, str)
            or len(top) > 256
            or not valid_hdl_identifier(top)
        ):
            input_errors.append(f"unsupported top-module name: {top}")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            input_errors.append("timeout must be finite and greater than zero")

        script_path = out_dir / "timing-analyze.tcl"
        transcript_path = out_dir / "timing-analyze.log"
        sdc_snapshot_path = out_dir / "timing-input.sdc"
        output_paths = {
            script_path,
            transcript_path,
            sdc_snapshot_path,
            *(out_dir / name for name in _OUTPUTS),
        }
        if out_dir.is_file():
            input_errors.append("the timing output directory resolves to a file")
        if output_paths.intersection({netlist_path, liberty_path, sdc_path}):
            input_errors.append("timing evidence paths must not overwrite an input")
        if any(os.path.lexists(path) for path in output_paths):
            input_errors.append("timing evidence paths must be absent before launch")

        runtime_environment = _closed_opensta_environment(self.binary)
        tool_identity_before = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        try:
            info = self.discovery.inspect_tool(
                "sta", probe_environment=runtime_environment
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            info = {}
        tool_identity_after_inspection = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        inspected_binary = info.get("binary")
        version = info.get("version")
        tool_usable = bool(
            info.get("status") == "available"
            and isinstance(inspected_binary, str)
            and inspected_binary == self.binary
            and isinstance(version, str)
            and version
            and tool_identity_before is not None
            and tool_identity_before == tool_identity_after_inspection
        )
        tool = tool_record(
            "sta",
            path=inspected_binary if isinstance(inspected_binary, str) else self.binary,
            version=version if isinstance(version, str) else None,
        )
        if input_errors:
            return result(
                "timing-analyze",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="The OpenSTA timing request is incomplete or unsafe.",
                inputs=inputs,
                diagnostics=[
                    diagnostic("error", "input.invalid", message)
                    for message in input_errors[:100]
                ],
                data=base_data,
            )
        if not tool_usable:
            return result(
                "timing-analyze",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary=(
                    "OpenSTA is not available as an identified usable tool in the "
                    "selected runtime."
                ),
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "tool.missing",
                        "OpenSTA was not found or its version/identity probe was not accepted.",
                    )
                ],
                data=base_data,
            )
        assert isinstance(inspected_binary, str)

        try:
            if sdc_payload is None:
                raise ValueError("validated SDC snapshot is unavailable")
            out_dir.mkdir(parents=True, exist_ok=True)
            _write_exclusive(sdc_snapshot_path, sdc_payload, MAX_SDC_BYTES)
            sdc_snapshot_before = file_record(
                sdc_snapshot_path,
                kind="sdc",
                role="timing.sdc-snapshot",
                maximum_bytes=MAX_SDC_BYTES,
            )
            if sdc_snapshot_before.get("sha256") != inputs[-1].get("sha256"):
                raise ValueError("SDC snapshot digest differs from the declared input")
            script = "\n".join(
                (
                    f"read_liberty {_tcl_quote(liberty_path)}",
                    f"read_verilog {_tcl_quote(netlist_path)}",
                    f"link_design {_tcl_quote(top)}",
                    f"read_sdc {_tcl_quote(sdc_snapshot_path)}",
                    "puts OPENADA_UNITS_BEGIN",
                    "report_units",
                    "puts OPENADA_UNITS_END",
                    "check_setup -verbose -unconstrained_endpoints > check-setup.txt",
                    (
                        "report_checks -path_delay max -group_path_count 10 "
                        "-endpoint_path_count 1 -format json > setup-paths.json"
                    ),
                    (
                        "report_checks -path_delay min -group_path_count 10 "
                        "-endpoint_path_count 1 -format json > hold-paths.json"
                    ),
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
            _write_exclusive(script_path, script.encode("utf-8"), MAX_SDC_BYTES)
            script_before = file_record(
                script_path,
                kind="opensta-script",
                role="timing.script",
                maximum_bytes=MAX_SDC_BYTES,
            )
        except (OSError, ValueError) as exc:
            return result(
                "timing-analyze",
                tool=tool,
                execution=static_execution("failed"),
                engineering_status="unknown",
                summary="OpenADA could not prepare the OpenSTA timing script.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "output.unavailable", str(exc))],
                data=base_data,
            )

        produced: dict[str, bool] = {name: False for name in _OUTPUTS}
        native_output_digests: dict[str, str] = {}
        output_errors: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix=".openada-sta-", dir=out_dir) as work:
                process = run_process(
                    [inspected_binary, "-no_init", "-exit", str(script_path)],
                    cwd=work,
                    timeout=timeout,
                    env=runtime_environment,
                    capture_limit_bytes=4 * 1024 * 1024,
                )
                work_path = Path(work)
                for name in _OUTPUTS:
                    candidate = work_path / name
                    maximum = (
                        MAX_CHECK_SETUP_BYTES
                        if name == "check-setup.txt"
                        else MAX_REPORT_BYTES
                    )
                    try:
                        metadata = candidate.lstat()
                    except OSError:
                        continue
                    if (
                        candidate.is_symlink()
                        or not candidate.is_file()
                        or metadata.st_nlink != 1
                    ):
                        output_errors.append(
                            f"native output is linked or not a regular file: {name}"
                        )
                        continue
                    payload, capture, digest = _read_bounded(candidate, maximum)
                    if payload is None or digest is None:
                        output_errors.append(
                            f"native output cannot be captured safely: {name}: {capture}"
                        )
                        continue
                    try:
                        _write_exclusive(out_dir / name, payload, maximum)
                    except (OSError, ValueError) as exc:
                        output_errors.append(
                            f"native output cannot be retained exclusively: {name}: {exc}"
                        )
                        continue
                    produced[name] = True
                    native_output_digests[name] = digest
        except OSError as exc:
            process = static_execution("failed")
            process["error"] = str(exc)
            output_errors.append(f"cannot isolate or retain native timing outputs: {exc}")

        if isinstance(process, dict):
            # The temporary workspace failed before a ProcessResult existed.
            transcript_error = process.get("error", "OpenSTA did not launch.")
        else:
            transcript_error = None
            try:
                write_process_transcript(transcript_path, process)
            except (OSError, ValueError) as exc:
                transcript_error = str(exc)
                output_errors.append(f"cannot retain the OpenSTA transcript: {exc}")

        changed_inputs: list[str] = []
        for before, (path, kind, role, maximum) in zip(inputs, declarations, strict=True):
            try:
                after = file_record(path, kind=kind, role=role, maximum_bytes=maximum)
            except FileRecordError:
                changed_inputs.append(str(path))
                continue
            if any(before.get(key) != after.get(key) for key in ("exists", "bytes", "sha256")):
                changed_inputs.append(str(path))
        inputs_stable = not changed_inputs
        base_data["inputs_stable"] = inputs_stable
        tool_identity_stable = bool(
            tool_identity_before is not None
            and tool_identity_before == self.discovery._binary_identity(inspected_binary)
        )
        base_data["tool_identity_stable"] = tool_identity_stable
        base_data["changed_inputs"] = changed_inputs

        constraints_complete, check_validation, check_digest = (
            _load_check_setup(out_dir / "check-setup.txt")
            if produced["check-setup.txt"]
            else (None, "missing", None)
        )
        setup_report, setup_validation, setup_digest = (
            _load_path_report(out_dir / "setup-paths.json", "max")
            if produced["setup-paths.json"]
            else (None, "missing", None)
        )
        hold_report, hold_validation, hold_digest = (
            _load_path_report(out_dir / "hold-paths.json", "min")
            if produced["hold-paths.json"]
            else (None, "missing", None)
        )
        base_data["constraints_complete"] = constraints_complete
        base_data["constraint_check_validation"] = check_validation

        metrics: dict[str, Any] | None = None
        metrics_validation = "execution-not-captured"
        capture_complete = False
        native_issues: list[tuple[str, str]] = []
        if not isinstance(process, dict):
            capture_complete = (
                not process.stdout_truncated
                and not process.stderr_truncated
                and process.stdout_utf8_valid
                and process.stderr_utf8_valid
            )
            native_text = "\n".join((process.stdout, process.stderr))
            metrics, metrics_validation = _parse_metrics(native_text)
            for line in native_text.splitlines():
                match = re.match(
                    r"^\s*(Error|Warning)(?::|\s)(.*)$",
                    line,
                    flags=re.IGNORECASE,
                )
                if match:
                    native_issues.append((match.group(1).lower(), line.strip()[:1_000]))

        metric_consistent = False
        metric_consistency = "metrics-unavailable"
        reports_agree = False
        if metrics is not None:
            metric_consistent, metric_consistency = _metric_consistency(metrics)
            if setup_report is not None and hold_report is not None:
                reports_agree = _slack_agrees(
                    metrics["setup_wns_s"], setup_report, metrics["time_unit_seconds"]
                ) and _slack_agrees(
                    metrics["hold_wns_s"], hold_report, metrics["time_unit_seconds"]
                )

        reports_complete = setup_report is not None and hold_report is not None
        base_data["reports_complete"] = reports_complete
        base_data["metrics_validation"] = metrics_validation
        base_data["metric_consistency"] = metric_consistency
        base_data["path_reports_agree_with_wns"] = reports_agree
        if metrics is not None:
            base_data["time_unit"] = metrics["time_unit"]
            base_data["time_unit_seconds"] = metrics["time_unit_seconds"]
            base_data["setup"] = {
                "wns_s": metrics["setup_wns_s"],
                "tns_s": metrics["setup_tns_s"],
                "path_count": setup_report["path_count"] if setup_report else None,
                "critical_path": setup_report["critical_path"] if setup_report else None,
            }
            base_data["hold"] = {
                "wns_s": metrics["hold_wns_s"],
                "tns_s": metrics["hold_tns_s"],
                "path_count": hold_report["path_count"] if hold_report else None,
                "critical_path": hold_report["critical_path"] if hold_report else None,
            }

        artifact_errors: list[str] = []
        for name, parsed_digest in (
            ("check-setup.txt", check_digest),
            ("setup-paths.json", setup_digest),
            ("hold-paths.json", hold_digest),
        ):
            captured_digest = native_output_digests.get(name)
            if captured_digest is not None and parsed_digest != captured_digest:
                artifact_errors.append(
                    f"captured native output changed before validation: {name}"
                )

        expected_digests = {
            "timing.script": script_before.get("sha256"),
            "timing.sdc-snapshot": sdc_snapshot_before.get("sha256"),
            "timing.constraint-check": native_output_digests.get("check-setup.txt"),
            "timing.setup-paths": native_output_digests.get("setup-paths.json"),
            "timing.hold-paths": native_output_digests.get("hold-paths.json"),
        }
        artifacts: list[dict[str, Any]] = []
        native_role_names = {role: name for name, (_kind, role) in _OUTPUTS.items()}
        for path, kind, role, maximum in (
            (script_path, "opensta-script", "timing.script", MAX_SDC_BYTES),
            (sdc_snapshot_path, "sdc", "timing.sdc-snapshot", MAX_SDC_BYTES),
            (transcript_path, "opensta-log", "timing.log", 16 * 1024 * 1024),
            *(
                (
                    out_dir / name,
                    kind,
                    role,
                    MAX_CHECK_SETUP_BYTES if name == "check-setup.txt" else MAX_REPORT_BYTES,
                )
                for name, (kind, role) in _OUTPUTS.items()
            ),
        ):
            if role == "timing.log" and transcript_error is not None:
                continue
            native_name = native_role_names.get(role)
            if native_name is not None and not produced[native_name]:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            try:
                artifact = file_record(path, kind=kind, role=role, maximum_bytes=maximum)
            except FileRecordError as exc:
                artifact_errors.append(f"{path}: {exc}")
                continue
            artifacts.append(artifact)
            expected_digest = expected_digests.get(role)
            if expected_digest is not None and artifact.get("sha256") != expected_digest:
                artifact_errors.append(
                    f"captured {role} changed after it was parsed or prepared"
                )
        captured_roles = {artifact["role"] for artifact in artifacts}
        required_roles = {
            "timing.script",
            "timing.sdc-snapshot",
            "timing.log",
            "timing.constraint-check",
            "timing.setup-paths",
            "timing.hold-paths",
        }
        missing_artifact_roles = sorted(required_roles - captured_roles)
        if missing_artifact_roles:
            artifact_errors.append(
                "missing retained evidence roles: " + ", ".join(missing_artifact_roles)
            )

        execution_complete = (
            not isinstance(process, dict)
            and process.status == "completed"
            and process.exit_code == 0
        )
        evidence_complete = (
            execution_complete
            and capture_complete
            and transcript_error is None
            and not output_errors
            and not artifact_errors
            and inputs_stable
            and tool_identity_stable
            and constraints_complete is True
            and reports_complete
            and metrics is not None
            and metric_consistent
            and reports_agree
            and not native_issues
        )
        if evidence_complete and metrics is not None:
            satisfied = metrics["setup_wns_s"] >= 0 and metrics["hold_wns_s"] >= 0
            base_data["timing_constraints_satisfied"] = satisfied
            if satisfied:
                status = "pass"
                summary = "OpenSTA found nonnegative setup and hold slack for the supplied corner."
            else:
                status = "fail"
                summary = "OpenSTA found a setup or hold timing violation for the supplied corner."
        else:
            status = "unknown"
            summary = "OpenSTA did not yield complete trustworthy setup/hold timing evidence."

        diagnostics: list[dict[str, str]] = []
        execution_status = process.get("status") if isinstance(process, dict) else process.status
        execution_exit = (
            process.get("exit_code") if isinstance(process, dict) else process.exit_code
        )
        execution_error = process.get("error") if isinstance(process, dict) else process.error
        if execution_status != "completed":
            diagnostics.append(
                diagnostic(
                    "error",
                    f"execution.{execution_status}",
                    execution_error or "OpenSTA did not complete.",
                )
            )
        elif execution_exit != 0:
            diagnostics.append(
                diagnostic(
                    "error",
                    "opensta.nonzero_exit",
                    f"OpenSTA exited with code {execution_exit}.",
                )
            )
        if not capture_complete and not isinstance(process, dict):
            diagnostics.append(
                diagnostic(
                    "error",
                    "evidence.incomplete",
                    "OpenSTA output was truncated or was not valid UTF-8.",
                )
            )
        if not tool_identity_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "tool.changed",
                    "The version-validated OpenSTA executable identity changed "
                    "during timing analysis.",
                )
            )
        if changed_inputs:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.changed",
                    "A timing input changed while OpenSTA was running: "
                    + ", ".join(changed_inputs[:10]),
                )
            )
        diagnostics.extend(
            diagnostic("error", "artifact.invalid", message) for message in output_errors[:20]
        )
        diagnostics.extend(
            diagnostic("error", "artifact.uncaptured", message)
            for message in artifact_errors[:20]
        )
        if constraints_complete is not True:
            diagnostics.append(
                diagnostic(
                    "error",
                    "timing.constraints_incomplete",
                    f"OpenSTA check_setup validation: {check_validation}.",
                )
            )
        if setup_report is None:
            diagnostics.append(
                diagnostic(
                    "error",
                    "timing.setup_report_invalid",
                    f"Setup path report validation: {setup_validation}.",
                )
            )
        if hold_report is None:
            diagnostics.append(
                diagnostic(
                    "error",
                    "timing.hold_report_invalid",
                    f"Hold path report validation: {hold_validation}.",
                )
            )
        if metrics is None:
            diagnostics.append(
                diagnostic(
                    "error",
                    "timing.metrics_invalid",
                    f"OpenSTA metric transcript validation: {metrics_validation}.",
                )
            )
        elif not metric_consistent:
            diagnostics.append(
                diagnostic("error", "timing.metrics_inconsistent", metric_consistency)
            )
        if reports_complete and metrics is not None and not reports_agree:
            diagnostics.append(
                diagnostic(
                    "error",
                    "timing.evidence_disagrees",
                    "Path-report critical slack disagrees with report_worst_slack.",
                )
            )
        diagnostics.extend(
            diagnostic(
                "error" if severity == "error" else "warning",
                f"opensta.{severity}",
                message,
            )
            for severity, message in native_issues[:20]
        )
        if status == "fail" and metrics is not None:
            if metrics["setup_wns_s"] < 0:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "timing.setup_violation",
                        f"Setup WNS is {metrics['setup_wns_s']:.12g} s.",
                    )
                )
            if metrics["hold_wns_s"] < 0:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "timing.hold_violation",
                        f"Hold WNS is {metrics['hold_wns_s']:.12g} s.",
                    )
                )
        diagnostics.append(
            diagnostic(
                "warning",
                "timing.ideal_interconnect",
                "No SPEF parasitics were supplied; this single-corner result is not "
                "signoff timing.",
            )
        )

        return result(
            "timing-analyze",
            tool=tool,
            execution=process,
            engineering_status=status,
            summary=summary,
            inputs=inputs,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data=base_data,
        )
