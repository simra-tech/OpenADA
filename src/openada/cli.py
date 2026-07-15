"""Standalone, JSON-first OpenADA command line interface."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import stat
import sys
import uuid

from . import __version__
from .contract import diagnostic, result, static_execution
from .discovery import DiscoveryManager, TOOL_SPECS
from .driver_registry import BUILTIN_DRIVERS, TRANSIENT_FEATURE
from .engines import (
    KLayoutDriver,
    NetgenDriver,
    NgspiceDriver,
    NgspiceOutput,
    XschemDriver,
    YosysDriver,
)
from .operations import (
    MAX_SHARED_ANALYSIS_POINTS,
    MEASUREMENT_KINDS,
    evaluate_specification,
    invalid_circuit_simulation_request,
    measure_result,
    simulate_circuit_profile,
)
from .preflight import PREFLIGHT_SPECS


class _RequestParseError(Exception):
    """An argparse validation failure that must be returned through the contract."""


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _RequestParseError(message)


class _StoreOnce(argparse.Action):
    """Store an option once so duplicate preflight intent cannot be ambiguous."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be specified only once")
        setattr(namespace, self.dest, values)


_COMMAND_OPERATIONS = {
    "doctor": "doctor",
    "capabilities": "doctor",
    "netlist": "netlist",
    "simulate": "simulate",
    "measure": "result.measure",
    "evaluate": "specification.evaluate",
    "drc": "drc",
    "lvs": "lvs",
    "rtl-check": "rtl-check",
}

MAX_PREFLIGHT_PATH_CHARS = 4_095
MAX_PREFLIGHT_PDK_ROOTS = 64
MAX_PREFLIGHT_TOOL_OVERRIDES = 64
MAX_PREFLIGHT_VERSION_TIMEOUT_SECONDS = 30.0
MAX_OPERATION_JSON_BYTES = 64 * 1024 * 1024


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than zero")
    return parsed


def _tool_path(value: str) -> str:
    name, separator, path = value.partition("=")
    if not separator or name not in TOOL_SPECS or not path.strip():
        raise argparse.ArgumentTypeError("expected a known NAME=PATH")
    return value


def _ngspice_output(value: str) -> NgspiceOutput:
    kind, separator, path = value.partition("=")
    if not separator or kind not in {"raw", "wrdata"} or not path:
        raise argparse.ArgumentTypeError("expected raw=RELATIVE_PATH or wrdata=RELATIVE_PATH")
    return NgspiceOutput(kind=kind, path=path)


def _deck_variable(value: str) -> tuple[str, str]:
    name, separator, variable_value = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError("expected NAME=VALUE")
    return name, variable_value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("duplicate JSON object key")
        parsed[key] = value
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _load_json_object(value: str, *, role: str) -> dict:
    path = Path(value).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{role} JSON could not be read: {exc}") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError(f"{role} JSON must be a regular file")
        if initial.st_size > MAX_OPERATION_JSON_BYTES:
            raise ValueError(
                f"{role} JSON exceeds the {MAX_OPERATION_JSON_BYTES}-byte input limit"
            )
        chunks: list[bytes] = []
        remaining = MAX_OPERATION_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final = os.fstat(descriptor)
        if len(body) > MAX_OPERATION_JSON_BYTES:
            raise ValueError(
                f"{role} JSON exceeds the {MAX_OPERATION_JSON_BYTES}-byte input limit"
            )
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        if any(getattr(initial, name) != getattr(final, name) for name in identity_fields):
            raise ValueError(f"{role} JSON changed while it was being read")
        if len(body) != initial.st_size:
            raise ValueError(f"{role} JSON changed while it was being read")
        parsed = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{role} JSON is invalid: {exc}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(parsed, dict):
        raise ValueError(f"{role} JSON must contain one object")
    return parsed


def _common_tool_argument(parser: argparse.ArgumentParser, tool: str) -> None:
    parser.add_argument("--tool", choices=[tool], default=tool, help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        prog="openada",
        description="Run open-source EDA tools through a versioned agent-facing contract.",
    )
    parser.add_argument("--version", action="version", version=f"OpenADA {__version__}")
    parser.add_argument(
        "--profile",
        choices=["auto", "native", "iic-osic-tools"],
        default="auto",
        help="Runtime discovery profile (default: auto).",
    )
    parser.add_argument(
        "--pdk-root",
        action="append",
        default=[],
        help="Additional PDK root. Repeatable.",
    )
    parser.add_argument(
        "--tool-path",
        action="append",
        default=[],
        type=_tool_path,
        metavar="NAME=PATH",
        help="Override a discovered binary path. Repeatable.",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")

    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor",
        aliases=["capabilities"],
        help="Inspect available EDA binaries, versions, runtime profile, and PDK roots.",
    )
    doctor.add_argument("--tool", action="append", choices=sorted(TOOL_SPECS))
    doctor.add_argument(
        "--require",
        action="append",
        choices=sorted(TOOL_SPECS),
        default=[],
        help="Fail the engineering check when this tool is missing. Repeatable.",
    )
    doctor.add_argument("--version-timeout", type=_positive_float, default=3.0)
    doctor.add_argument(
        "--project-root",
        action=_StoreOnce,
        help="Canonical project directory for a scoped first-run preflight.",
    )
    doctor.add_argument(
        "--assertion",
        action=_StoreOnce,
        choices=sorted(PREFLIGHT_SPECS),
        help="Fixed engineering intent for a scoped first-run preflight.",
    )

    netlist = commands.add_parser("netlist", help="Generate a SPICE netlist from an Xschem schematic.")
    netlist.add_argument("schematic")
    netlist.add_argument("-o", "--output", required=True)
    netlist.add_argument("--rcfile", help="Explicit Xschem rcfile for project/PDK libraries.")
    netlist.add_argument("--timeout", type=_positive_float, default=60.0)
    _common_tool_argument(netlist, "xschem")

    simulate = commands.add_parser(
        "simulate",
        help="Run one circuit-simulation intent through ngspice or Xyce.",
    )
    simulate.add_argument("spice_file")
    simulate.add_argument(
        "--backend",
        choices=["ngspice", "xyce"],
        help=(
            "Select the shared circuit.simulate-profile backend. Without this option, "
            "the legacy ngspice interface remains active."
        ),
    )
    simulate.add_argument(
        "--analysis",
        choices=["op", "dc", "ac", "tran"],
        help=(
            "Explicit typed analysis for the shared profile. When omitted, "
            "OpenADA infers the one supported top-level analysis from the deck."
        ),
    )
    simulate.add_argument("--source-name", help="DC sweep voltage/current source name.")
    simulate.add_argument(
        "--source-unit",
        choices=["V", "A"],
        help="DC sweep source unit.",
    )
    simulate.add_argument("--start", type=_finite_float, help="DC sweep start value.")
    simulate.add_argument("--stop", type=_finite_float, help="DC sweep stop value.")
    simulate.add_argument("--step", type=_positive_float, help="DC sweep step value.")
    simulate.add_argument(
        "--sweep",
        choices=["lin", "dec", "oct"],
        help="AC frequency sweep kind.",
    )
    simulate.add_argument("--points", type=_positive_int, help="AC points per sweep.")
    simulate.add_argument(
        "--start-hz",
        type=_positive_float,
        help="AC sweep start frequency in hertz.",
    )
    simulate.add_argument(
        "--stop-hz",
        type=_positive_float,
        help="AC sweep stop frequency in hertz.",
    )
    simulate.add_argument(
        "--step-s",
        type=_positive_float,
        help="Transient suggested step in seconds.",
    )
    simulate.add_argument(
        "--stop-s",
        type=_positive_float,
        help="Transient stop time in seconds.",
    )
    simulate.add_argument(
        "--start-s",
        type=_finite_float,
        help="Transient output start time in seconds.",
    )
    simulate.add_argument(
        "--max-step-s",
        type=_positive_float,
        help="Transient maximum internal step in seconds.",
    )
    simulate.add_argument(
        "--output-dir",
        help="OpenADA-owned log/launcher evidence directory.",
    )
    simulate.add_argument(
        "--raw-file",
        help="Wrapper-owned raw destination; incompatible with declared deck-owned outputs.",
    )
    simulate.add_argument(
        "--workdir",
        help="Working directory for project-relative model and include paths (default: netlist directory).",
    )
    simulate.add_argument(
        "--execution-mode",
        choices=["batch", "control"],
        default=None,
        help="batch streams a reviewed flattened deck; control supports .measure/.control/includes (default: batch).",
    )
    simulate.add_argument(
        "--expect-output",
        action="append",
        default=[],
        type=_ngspice_output,
        metavar="KIND=RELATIVE_PATH",
        help="Required deck-owned raw or wrdata file, resolved under --workdir. Repeatable; requires control mode.",
    )
    simulate.add_argument(
        "--init-file",
        help="Explicit control-mode project/PDK init; disables local/user .spiceinit and hashes this input.",
    )
    simulate.add_argument(
        "--system-init-file",
        help="Explicit system spinit; overrides SPICE_SCRIPTS, disables local/user .spiceinit, and hashes this input.",
    )
    simulate.add_argument("--timeout", type=_positive_float, default=120.0)
    simulate.add_argument(
        "--tool",
        choices=["ngspice"],
        default=None,
        help=argparse.SUPPRESS,
    )

    measure = commands.add_parser(
        "measure",
        help="Derive one typed scalar from a provenance-bound normalized real series.",
    )
    measure.add_argument(
        "--series",
        required=True,
        help="JSON file containing the bounded normalized source series.",
    )
    measure.add_argument(
        "--measurement",
        required=True,
        help="JSON file containing one closed typed measurement request.",
    )
    measure.add_argument(
        "--request-id",
        help="Optional canonical UUID for request correlation.",
    )

    evaluate = commands.add_parser(
        "evaluate",
        help="Evaluate one typed measurement against explicit unit-bearing limits.",
    )
    evaluate.add_argument(
        "--measurement",
        required=True,
        help="JSON file containing a complete result.measure result envelope.",
    )
    evaluate.add_argument(
        "--specification",
        required=True,
        help="JSON file containing one closed specification request.",
    )
    evaluate.add_argument(
        "--request-id",
        help="Optional canonical UUID for request correlation.",
    )

    drc = commands.add_parser(
        "drc",
        help="Run a KLayout DRC deck with one exact fresh report output.",
    )
    drc.add_argument("gds_file")
    drc.add_argument("--rules", required=True)
    report_mode = drc.add_mutually_exclusive_group()
    report_mode.add_argument(
        "--report",
        help="Fresh report path passed through the selected KLayout report variable.",
    )
    report_mode.add_argument(
        "--expect-report",
        help="Exact fresh report path already owned by the script, relative to --workdir.",
    )
    drc.add_argument(
        "--workdir",
        help="Existing KLayout working directory (default: current directory).",
    )
    drc.add_argument(
        "--top-cell",
        help="Explicit top cell passed as $topcell and required in the native report.",
    )
    drc.add_argument(
        "--report-variable",
        default="report",
        help="Variable-bound mode output variable without '$' (default: report).",
    )
    drc.add_argument(
        "--deck-var",
        action="append",
        default=[],
        type=_deck_variable,
        metavar="NAME=VALUE",
        help="Additional bounded KLayout -rd deck variable. Repeatable.",
    )
    drc.add_argument(
        "--provenance-input",
        action="append",
        default=[],
        help="Additional rule/PDK file to hash and recheck. Repeatable.",
    )
    drc.add_argument(
        "--waiver-file",
        help="Explicit automatic <report>.w waiver database to hash and recheck.",
    )
    drc.add_argument("--timeout", type=_positive_float, default=180.0)
    _common_tool_argument(drc, "klayout")

    lvs = commands.add_parser("lvs", help="Compare layout and schematic netlists with Netgen.")
    lvs.add_argument("layout_netlist")
    lvs.add_argument("schematic_netlist")
    lvs.add_argument("--cell", required=True)
    lvs.add_argument("--setup", required=True)
    lvs.add_argument("--report")
    lvs.add_argument(
        "--provenance-input",
        action="append",
        default=[],
        help="Additional setup/PDK file to hash and recheck. Repeatable.",
    )
    lvs.add_argument("--timeout", type=_positive_float, default=180.0)
    _common_tool_argument(lvs, "netgen")

    rtl = commands.add_parser("rtl-check", help="Elaborate and structurally check RTL with Yosys.")
    rtl.add_argument("sources", nargs="+")
    rtl.add_argument("--top")
    rtl.add_argument("--output-dir")
    rtl.add_argument("--json-netlist")
    rtl.add_argument("--timeout", type=_positive_float, default=120.0)
    _common_tool_argument(rtl, "yosys")
    return parser


def _overrides(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or name not in TOOL_SPECS or not path.strip():
            raise ValueError(f"invalid --tool-path '{value}'; expected a known NAME=PATH")
        parsed[name] = path.strip()
    return parsed


def _semantic_capability_records(tools: dict[str, dict]) -> list[dict]:
    conformance_id = "model-free-op-dc-ac-tran-ngspice-xyce-v0alpha2"
    typed_conformance_id = "typed-evidence-measurement-specification-v0alpha1"
    records: list[dict] = []
    for alias, driver in sorted(BUILTIN_DRIVERS.items()):
        tool = tools.get(driver.native_tool)
        records.append(
            {
                "provider_id": driver.driver_id,
                "provider_version": driver.version,
                "provider_kind": "eda-driver",
                "availability": tool["status"] if tool is not None else "not-inspected",
                "native_product": driver.native_tool,
                "operation_profile": driver.operation_profile,
                "operation_profile_schema": "openada.operation-profile/v0alpha1",
                "assertion_profile": driver.assertion_profile,
                "result_schema": "openada.result/v0alpha1",
                "transports": ["local-cli"],
                "locator_types": ["filesystem"],
                "features": [
                    {
                        "id": feature,
                        "maturity": (
                            "workflow-validated"
                            if feature == TRANSIENT_FEATURE
                            else "structured"
                        ),
                        "conformance_ids": [conformance_id],
                    }
                    for feature in driver.features
                ],
            }
        )

    records.extend(
        [
            {
                "provider_id": "org.openada.kernel.typed-evidence",
                "provider_version": "1.0.0",
                "provider_kind": "evidence-kernel",
                "availability": "available",
                "native_product": None,
                "operation_profile": "openada.operation/result.measure/v1alpha1",
                "operation_profile_schema": "openada.operation-profile/v0alpha2",
                "assertion_profile": "openada.assertion/measurement.valid/v1alpha1",
                "result_schema": "openada.result/v0alpha1",
                "transports": ["local-cli", "in-process"],
                "locator_types": ["artifact"],
                "features": [
                    {
                        "id": (
                            "openada.feature/measurement."
                            f"{kind.replace('_', '-')}/v1alpha1"
                        ),
                        "maturity": "structured",
                        "conformance_ids": [typed_conformance_id],
                    }
                    for kind in MEASUREMENT_KINDS
                ],
            },
            {
                "provider_id": "org.openada.kernel.typed-evidence",
                "provider_version": "1.0.0",
                "provider_kind": "evidence-kernel",
                "availability": "available",
                "native_product": None,
                "operation_profile": (
                    "openada.operation/specification.evaluate/v1alpha1"
                ),
                "operation_profile_schema": "openada.operation-profile/v0alpha2",
                "assertion_profile": (
                    "openada.assertion/specification.satisfied/v1alpha1"
                ),
                "result_schema": "openada.result/v0alpha1",
                "transports": ["local-cli", "in-process"],
                "locator_types": ["artifact"],
                "features": [
                    {
                        "id": (
                            "openada.feature/specification.bound-evaluation/v1alpha1"
                        ),
                        "maturity": "structured",
                        "conformance_ids": [typed_conformance_id],
                    },
                    {
                        "id": (
                            "openada.feature/specification.condition-binding/v1alpha1"
                        ),
                        "maturity": "structured",
                        "conformance_ids": [typed_conformance_id],
                    },
                ],
            },
        ]
    )
    return records


def _doctor_invalid(message: str) -> dict:
    return result(
        "doctor",
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="OpenADA could not validate the doctor preflight request.",
        diagnostics=[diagnostic("error", "request.invalid", message)],
    )


def _bounded_path_text(value: str, *, option: str) -> str:
    if not value:
        raise ValueError(f"{option} must not be empty")
    if len(value) > MAX_PREFLIGHT_PATH_CHARS:
        raise ValueError(
            f"{option} must not exceed {MAX_PREFLIGHT_PATH_CHARS} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{option} must not contain control characters")
    return value


def _expandable_path_text(value: str, *, option: str) -> str:
    checked = _bounded_path_text(value, option=option)
    try:
        Path(checked).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{option} could not be expanded") from exc
    return checked


def _project_root(value: str) -> tuple[Path, tuple[int, int]]:
    checked = _bounded_path_text(value, option="--project-root")
    try:
        root = Path(checked).expanduser().resolve(strict=True)
        metadata = root.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"--project-root could not be resolved: {exc}") from exc
    if len(str(root)) > MAX_PREFLIGHT_PATH_CHARS:
        raise ValueError(
            f"canonical --project-root must not exceed {MAX_PREFLIGHT_PATH_CHARS} characters"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("--project-root must resolve to an existing directory")
    return root, (metadata.st_dev, metadata.st_ino)


def _preflight_limits(args: argparse.Namespace) -> None:
    if args.version_timeout > MAX_PREFLIGHT_VERSION_TIMEOUT_SECONDS:
        raise ValueError(
            "preflight --version-timeout must be no greater than "
            f"{MAX_PREFLIGHT_VERSION_TIMEOUT_SECONDS:g} seconds"
        )
    if len(args.pdk_root) > MAX_PREFLIGHT_PDK_ROOTS:
        raise ValueError(
            f"preflight accepts at most {MAX_PREFLIGHT_PDK_ROOTS} --pdk-root values"
        )
    if len(args.tool_path) > MAX_PREFLIGHT_TOOL_OVERRIDES:
        raise ValueError(
            "preflight accepts at most "
            f"{MAX_PREFLIGHT_TOOL_OVERRIDES} --tool-path values"
        )
    for value in args.pdk_root:
        _expandable_path_text(value, option="--pdk-root")
    for value in args.tool_path:
        _, _, path = value.partition("=")
        _expandable_path_text(path.strip(), option="--tool-path")


def _doctor(args: argparse.Namespace, discovery: DiscoveryManager) -> dict:
    preflight_requested = args.project_root is not None or args.assertion is not None
    if preflight_requested:
        if args.project_root is None or args.assertion is None:
            return _doctor_invalid(
                "--project-root and --assertion must be supplied together"
            )
        if args.tool or args.require:
            return _doctor_invalid(
                "--tool and --require cannot be combined with scoped preflight"
            )
        try:
            _preflight_limits(args)
            project_root, root_identity = _project_root(args.project_root)
        except ValueError as exc:
            return _doctor_invalid(str(exc))

        spec = PREFLIGHT_SPECS[args.assertion]
        capabilities = discovery.get_capabilities(
            [spec.tool],
            version_timeout=args.version_timeout,
            enumerate_pdks=False,
            include_probe_details=True,
        )
        tool_info = capabilities["tools"][spec.tool]
        try:
            current = project_root.stat()
            root_stable = (current.st_dev, current.st_ino) == root_identity
        except OSError:
            root_stable = False
        tool_ready = tool_info["status"] == "available"
        capabilities["preflight"] = {
            "project_root": {
                "path": str(project_root),
                "kind": "directory",
                "canonicalized": True,
                "identity_stable": root_stable,
            },
            "assertion": spec.assertion,
            "assertion_evaluated": False,
            "target": {
                "operation": spec.operation,
                "tool": spec.tool,
            },
            "tool_ready": tool_ready,
            "project_inventory_performed": False,
            "project_collateral_enumerated": False,
            "pdk": {
                "applicable": spec.pdk_applicable,
                "roots": list(capabilities["pdk_roots"]),
                "selected": None,
                "catalog_enumerated": False,
            },
            "startup": {
                "binding": "operation-time",
                "policy": spec.startup_policy,
                "supported_explicit_options": list(spec.startup_options),
                "selected_files": [],
                "ambient_files_enumerated": False,
            },
        }
        if not root_stable:
            return result(
                "doctor",
                tool=None,
                execution=static_execution(),
                engineering_status="unknown",
                summary=(
                    "The project root changed during preflight; no design assertion was executed."
                ),
                diagnostics=[
                    diagnostic(
                        "error",
                        "preflight.project_root_changed",
                        "The canonical project directory identity changed during the tool probe.",
                    )
                ],
                data=capabilities,
            )
        if tool_ready:
            return result(
                "doctor",
                tool=None,
                execution=static_execution(),
                engineering_status="pass",
                summary=(
                    f"Preflight selected '{spec.operation}' and found {spec.tool} usable; "
                    "no design assertion was executed."
                ),
                data=capabilities,
            )
        return result(
            "doctor",
            tool=None,
            execution=static_execution(),
            engineering_status="fail",
            summary=(
                f"Preflight selected '{spec.operation}', but {spec.tool} is "
                f"{tool_info['status']}; no design assertion was executed."
            ),
            diagnostics=[
                diagnostic(
                    "error",
                    "tool.required_unavailable",
                    f"Required tool is not usable: {spec.tool} ({tool_info['status']}).",
                )
            ],
            data=capabilities,
        )

    names = args.tool or list(TOOL_SPECS)
    for required in args.require:
        if required not in names:
            names.append(required)
    capabilities = discovery.get_capabilities(names, version_timeout=args.version_timeout)
    capabilities["semantic_capabilities"] = _semantic_capability_records(
        capabilities["tools"]
    )
    missing = [name for name in args.require if capabilities["tools"][name]["status"] != "available"]
    diagnostics = [
        diagnostic(
            "error",
            "tool.required_unavailable",
            f"Required tool is not usable: {name} ({capabilities['tools'][name]['status']}).",
        )
        for name in missing
    ]
    if args.require:
        engineering_status = "fail" if missing else "pass"
        summary = (
            f"{len(missing)} required tool(s) are unavailable."
            if missing
            else "All required tools are available."
        )
    else:
        engineering_status = "not_applicable"
        available = sum(tool["status"] == "available" for tool in capabilities["tools"].values())
        summary = f"Discovered {available} of {len(capabilities['tools'])} inspected tool(s)."
    return result(
        "doctor",
        tool=None,
        execution=static_execution(),
        engineering_status=engineering_status,
        summary=summary,
        diagnostics=diagnostics,
        data=capabilities,
    )


_SIMULATION_PARAMETER_OPTIONS = {
    "source_name": "--source-name",
    "source_unit": "--source-unit",
    "start": "--start",
    "stop": "--stop",
    "step": "--step",
    "sweep": "--sweep",
    "points": "--points",
    "start_hz": "--start-hz",
    "stop_hz": "--stop-hz",
    "step_s": "--step-s",
    "stop_s": "--stop-s",
    "start_s": "--start-s",
    "max_step_s": "--max-step-s",
}


def _simulation_profile_parameters(args: argparse.Namespace) -> dict | None:
    supplied = {
        name for name in _SIMULATION_PARAMETER_OPTIONS if getattr(args, name) is not None
    }
    if args.analysis is None:
        if supplied:
            options = ", ".join(_SIMULATION_PARAMETER_OPTIONS[name] for name in sorted(supplied))
            raise ValueError(f"--analysis is required when supplying {options}")
        return None

    fields = {
        "op": ((), ()),
        "dc": (
            ("source_name", "source_unit", "start", "stop", "step"),
            ("source_name", "source_unit", "start", "stop", "step"),
        ),
        "ac": (
            ("sweep", "points", "start_hz", "stop_hz"),
            ("sweep", "points", "start_hz", "stop_hz"),
        ),
        "tran": (
            ("step_s", "stop_s"),
            ("step_s", "stop_s", "start_s", "max_step_s"),
        ),
    }
    required, allowed = fields[args.analysis]
    unexpected = supplied - set(allowed)
    if unexpected:
        options = ", ".join(
            _SIMULATION_PARAMETER_OPTIONS[name] for name in sorted(unexpected)
        )
        raise ValueError(f"--analysis {args.analysis} does not accept {options}")
    missing = set(required) - supplied
    if missing:
        options = ", ".join(_SIMULATION_PARAMETER_OPTIONS[name] for name in sorted(missing))
        raise ValueError(f"--analysis {args.analysis} requires {options}")

    if args.analysis == "dc" and args.stop <= args.start:
        raise ValueError("--stop must be greater than --start for a DC analysis")
    if args.analysis == "ac":
        if args.points > MAX_SHARED_ANALYSIS_POINTS:
            raise ValueError(
                f"--points must be no greater than {MAX_SHARED_ANALYSIS_POINTS}"
            )
        if args.stop_hz <= args.start_hz:
            raise ValueError("--stop-hz must be greater than --start-hz for an AC analysis")
    if args.analysis == "tran":
        start_s = args.start_s if args.start_s is not None else 0.0
        if start_s < 0:
            raise ValueError("--start-s must be greater than or equal to zero")
        if start_s >= args.stop_s:
            raise ValueError("--start-s must be less than --stop-s for a transient analysis")
        if args.max_step_s is not None and args.max_step_s > args.stop_s - start_s:
            raise ValueError(
                "--max-step-s must not exceed --stop-s minus --start-s"
            )

    analysis = {"type": args.analysis, "extensions": {}}
    for name in allowed:
        value = getattr(args, name)
        if value is not None:
            analysis[name] = value
    return {"analysis": analysis, "extensions": {}}


def _simulation_cli_invalid(args: argparse.Namespace, message: str) -> dict:
    if args.backend is not None:
        return invalid_circuit_simulation_request(
            message,
            backend=args.backend,
            analysis_type=args.analysis,
        )
    return _invalid_request("simulate", message)


def _measurement_record(document: dict) -> dict:
    required_envelope_fields = {
        "schema",
        "operation",
        "tool",
        "execution",
        "engineering",
        "inputs",
        "artifacts",
        "diagnostics",
        "data",
        "provenance",
    }
    if set(document) != required_envelope_fields:
        raise ValueError(
            "the measurement input must contain the complete openada.result/v0alpha1 envelope with no undeclared fields"
        )
    if document.get("schema") != "openada.result/v0alpha1":
        raise ValueError(
            "the measurement input must be a complete openada.result/v0alpha1 envelope"
        )
    if document.get("operation") != "result.measure":
        raise ValueError(
            "the measurement result envelope must have operation 'result.measure'"
        )
    if document.get("tool") is not None:
        raise ValueError("a result.measure envelope must have a null tool record")
    if document.get("inputs") != [] or document.get("artifacts") != []:
        raise ValueError("a result.measure envelope must have empty inputs and artifacts")
    diagnostics = document.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("the measurement result envelope diagnostics must be an array")
    for item in diagnostics:
        if (
            not isinstance(item, dict)
            or not {"severity", "code", "message"}.issubset(item)
            or set(item) - {"severity", "code", "message", "hint"}
            or item.get("severity") not in {"info", "warning", "error"}
            or not all(
                isinstance(item.get(name), str)
                for name in ({"code", "message"} | ({"hint"} if "hint" in item else set()))
            )
        ):
            raise ValueError(
                "the measurement result envelope contains an invalid diagnostic record"
            )

    execution = document.get("execution")
    execution_required = {"status", "exit_code", "duration_ms", "command"}
    execution_allowed = execution_required | {"cwd", "error"}
    if (
        not isinstance(execution, dict)
        or not execution_required.issubset(execution)
        or set(execution) - execution_allowed
        or execution.get("status")
        not in {"completed", "timed_out", "not_available", "invalid_request", "failed"}
        or (
            execution.get("exit_code") is not None
            and (
                isinstance(execution.get("exit_code"), bool)
                or not isinstance(execution.get("exit_code"), int)
            )
        )
        or isinstance(execution.get("duration_ms"), bool)
        or not isinstance(execution.get("duration_ms"), int)
        or execution.get("duration_ms", -1) < 0
        or not isinstance(execution.get("command"), list)
        or not all(isinstance(item, str) for item in execution.get("command", []))
        or any(
            name in execution and not isinstance(execution[name], str)
            for name in ("cwd", "error")
        )
    ):
        raise ValueError("the measurement result envelope execution record is incomplete")
    engineering = document.get("engineering")
    if (
        not isinstance(engineering, dict)
        or set(engineering) != {"status", "summary"}
        or engineering.get("status") not in {"pass", "fail", "unknown", "not_applicable"}
        or not isinstance(engineering.get("summary"), str)
    ):
        raise ValueError("the measurement result envelope engineering record is incomplete")
    provenance = document.get("provenance")
    host = provenance.get("host") if isinstance(provenance, dict) else None
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"openada_version", "created_at", "host"}
        or not isinstance(provenance.get("openada_version"), str)
        or not isinstance(provenance.get("created_at"), str)
        or not isinstance(host, dict)
        or set(host) != {"system", "machine", "python"}
        or not all(isinstance(host.get(name), str) for name in host)
    ):
        raise ValueError("the measurement result envelope provenance record is incomplete")

    data = document.get("data")
    if not isinstance(data, dict) or set(data) != {
        "protocol",
        "measurement",
        "extensions",
    }:
        raise ValueError("the result.measure envelope data record is incomplete")
    if data.get("extensions") != {}:
        raise ValueError("the result.measure envelope data.extensions must be empty")
    protocol = data.get("protocol")
    expected_protocol_fields = {
        "request_id",
        "operation_profile",
        "assertion_profile",
        "implementation_id",
        "implementation_version",
    }
    if not isinstance(protocol, dict) or set(protocol) != expected_protocol_fields:
        raise ValueError("the result.measure envelope protocol record is incomplete")
    if protocol.get("operation_profile") != "openada.operation/result.measure/v1alpha1":
        raise ValueError("the measurement envelope operation profile is not result.measure/v1alpha1")
    if protocol.get("assertion_profile") != "openada.assertion/measurement.valid/v1alpha1":
        raise ValueError("the measurement envelope assertion profile is not measurement.valid/v1alpha1")
    if protocol.get("implementation_id") != "org.openada.kernel.typed-evidence":
        raise ValueError("the measurement envelope implementation identity is unsupported")
    try:
        protocol_request_id = uuid.UUID(str(protocol.get("request_id")))
    except (AttributeError, ValueError):
        raise ValueError("the measurement envelope request identity is invalid") from None
    if str(protocol_request_id) != protocol.get("request_id"):
        raise ValueError("the measurement envelope request identity is invalid")
    if not isinstance(protocol.get("implementation_version"), str) or not protocol.get(
        "implementation_version"
    ):
        raise ValueError("the measurement envelope implementation version is invalid")

    measurement = data.get("measurement") if isinstance(data, dict) else None
    if not isinstance(measurement, dict):
        raise ValueError(
            "the result.measure envelope does not contain data.measurement"
        )
    measurement_status = measurement.get("status")
    expected_engineering = {
        "measured": "pass",
        "not_found": "fail",
        "unknown": "unknown",
    }.get(measurement_status)
    if expected_engineering is None or engineering.get("status") != expected_engineering:
        raise ValueError(
            "the measurement status conflicts with the envelope engineering status"
        )
    if measurement_status in {"measured", "not_found"} and execution.get("status") != "completed":
        raise ValueError(
            "a measured or not_found result.measure envelope must have completed execution"
        )
    return measurement


def _dispatch(args: argparse.Namespace, discovery: DiscoveryManager) -> dict:
    if args.command in {"doctor", "capabilities"}:
        return _doctor(args, discovery)
    if args.command == "netlist":
        return XschemDriver(discovery=discovery).netlist(
            args.schematic,
            args.output,
            rcfile=args.rcfile,
            timeout=args.timeout,
        )
    if args.command == "simulate":
        source = Path(args.spice_file).expanduser().resolve()
        output_dir = (
            Path(args.output_dir).expanduser().resolve()
            if args.output_dir
            else source.parent / "openada-out" / source.stem
        )
        if args.backend is not None and args.tool is not None and args.backend != args.tool:
            return _simulation_cli_invalid(
                args,
                "--backend and the legacy --tool selector disagree",
            )
        try:
            parameters = _simulation_profile_parameters(args)
        except ValueError as exc:
            return _simulation_cli_invalid(args, str(exc))
        if args.backend is None and (args.analysis is not None or parameters is not None):
            return _invalid_request(
                "simulate",
                "--analysis and its typed parameters require --backend",
            )
        if args.backend is not None:
            profile_only_options = []
            if args.raw_file is not None:
                profile_only_options.append("--raw-file")
            if args.execution_mode is not None:
                profile_only_options.append("--execution-mode")
            if args.expect_output:
                profile_only_options.append("--expect-output")
            if args.init_file is not None:
                profile_only_options.append("--init-file")
            if args.system_init_file is not None:
                profile_only_options.append("--system-init-file")
            if profile_only_options:
                return _simulation_cli_invalid(
                    args,
                    "The shared circuit simulation profile does not accept legacy ngspice option(s): "
                    + ", ".join(profile_only_options),
                )
            return simulate_circuit_profile(
                source,
                output_dir,
                backend=args.backend,
                discovery=discovery,
                workdir=args.workdir,
                timeout=args.timeout,
                parameters=parameters,
            )
        return NgspiceDriver(discovery=discovery).simulate(
            source,
            output_dir,
            raw_file=args.raw_file,
            workdir=args.workdir,
            execution_mode=args.execution_mode or "batch",
            expected_outputs=args.expect_output,
            init_file=args.init_file,
            system_init_file=args.system_init_file,
            timeout=args.timeout,
        )
    if args.command == "measure":
        try:
            series = _load_json_object(args.series, role="series")
            measurement = _load_json_object(args.measurement, role="measurement")
        except ValueError as exc:
            return _invalid_request("result.measure", str(exc))
        return measure_result(series, measurement, request_id=args.request_id)
    if args.command == "evaluate":
        try:
            measurement_document = _load_json_object(
                args.measurement,
                role="measurement",
            )
            measurement = _measurement_record(measurement_document)
            specification = _load_json_object(
                args.specification,
                role="specification",
            )
        except ValueError as exc:
            return _invalid_request("specification.evaluate", str(exc))
        return evaluate_specification(
            measurement,
            specification,
            request_id=args.request_id,
        )
    if args.command == "drc":
        gds = Path(args.gds_file).expanduser().resolve()
        report = args.report
        if report is None and args.expect_report is None:
            report = gds.parent / "openada-out" / f"{gds.stem}.drc.lyrdb"
        return KLayoutDriver(discovery=discovery).drc(
            gds,
            args.rules,
            report,
            expected_report=args.expect_report,
            workdir=args.workdir,
            top_cell=args.top_cell,
            report_variable=args.report_variable,
            deck_variables=args.deck_var,
            provenance_inputs=args.provenance_input,
            waiver_file=args.waiver_file,
            timeout=args.timeout,
        )
    if args.command == "lvs":
        layout = Path(args.layout_netlist).expanduser().resolve()
        report = (
            Path(args.report).expanduser().resolve()
            if args.report
            else layout.parent / "openada-out" / f"{args.cell}.lvs.comp"
        )
        return NetgenDriver(discovery=discovery).lvs(
            layout,
            args.schematic_netlist,
            args.cell,
            args.setup,
            report,
            provenance_inputs=args.provenance_input,
            timeout=args.timeout,
        )
    if args.command == "rtl-check":
        output_dir = (
            Path(args.output_dir).expanduser().resolve()
            if args.output_dir
            else Path.cwd() / "openada-out" / "rtl-check"
        )
        return YosysDriver(discovery=discovery).rtl_check(
            args.sources,
            output_dir,
            top=args.top,
            json_netlist=args.json_netlist,
            timeout=args.timeout,
        )
    raise ValueError(f"unknown command: {args.command}")


def _exit_code(payload: dict) -> int:
    status = payload["engineering"]["status"]
    if status in {"pass", "not_applicable"}:
        return 0
    if status == "fail":
        return 1
    return 2


def _requested_operation(argv: list[str]) -> str:
    """Return a command operation only when it is unambiguous in argv."""
    value_options = {"--profile", "--pdk-root", "--tool-path"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--compact":
            index += 1
            continue
        if token == "--":
            index += 1
            if index < len(argv):
                return _COMMAND_OPERATIONS.get(argv[index], "openada.invalid_request")
            return "openada.invalid_request"
        if token in value_options:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if token.startswith("-"):
            return "openada.invalid_request"
        return _COMMAND_OPERATIONS.get(token, "openada.invalid_request")
    return "openada.invalid_request"


def _requested_shared_simulation(
    argv: list[str],
) -> tuple[bool, str | None, str | None]:
    """Recover a bounded shared-profile selection from an argparse failure."""

    if _requested_operation(argv) != "simulate":
        return False, None, None

    value_options = {"--profile", "--pdk-root", "--tool-path"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--compact":
            index += 1
            continue
        if token == "--":
            index += 1
            break
        if token in value_options:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if token == "simulate":
            index += 1
            break
        return False, None, None

    selected = False
    backend: str | None = None
    analysis_type: str | None = None
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token == "--backend":
            selected = True
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                candidate = argv[index + 1]
                backend = candidate if candidate in BUILTIN_DRIVERS else None
                index += 2
                continue
        elif token.startswith("--backend="):
            selected = True
            candidate = token.partition("=")[2]
            backend = candidate if candidate in BUILTIN_DRIVERS else None
        elif token == "--analysis":
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                candidate = argv[index + 1]
                analysis_type = (
                    candidate if candidate in {"op", "dc", "ac", "tran"} else None
                )
                index += 2
                continue
        elif token.startswith("--analysis="):
            candidate = token.partition("=")[2]
            analysis_type = (
                candidate if candidate in {"op", "dc", "ac", "tran"} else None
            )
        index += 1
    return selected, backend, analysis_type


def _invalid_request(operation: str, message: str) -> dict:
    if operation == "result.measure":
        payload = measure_result({}, {})
        payload["engineering"]["summary"] = "OpenADA could not parse the typed measurement request."
        payload["diagnostics"] = [
            diagnostic("error", "measurement.request.invalid", message)
        ]
        return payload
    if operation == "specification.evaluate":
        payload = evaluate_specification({}, {})
        payload["engineering"]["summary"] = "OpenADA could not parse the typed specification request."
        payload["diagnostics"] = [
            diagnostic("error", "specification.request.invalid", message)
        ]
        return payload
    return result(
        operation,
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="OpenADA could not parse the request.",
        diagnostics=[diagnostic("error", "request.invalid", message)],
    )


def _print_payload(payload: dict, *, compact: bool) -> None:
    print(
        json.dumps(
            payload,
            allow_nan=False,
            indent=None if compact else 2,
            sort_keys=compact,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw_argv)
    except _RequestParseError as exc:
        operation = _requested_operation(raw_argv)
        shared_profile, backend, analysis_type = _requested_shared_simulation(
            raw_argv
        )
        if shared_profile:
            payload = invalid_circuit_simulation_request(
                str(exc),
                backend=backend,
                analysis_type=analysis_type,
            )
        else:
            payload = _invalid_request(operation, str(exc))
        _print_payload(payload, compact="--compact" in raw_argv)
        return 2
    try:
        discovery = DiscoveryManager(
            profile=args.profile,
            pdk_roots=args.pdk_root,
            binary_overrides=_overrides(args.tool_path),
        )
        payload = _dispatch(args, discovery)
    except Exception as exc:
        payload = result(
            "openada.internal",
            tool=None,
            execution={
                "status": "failed",
                "exit_code": None,
                "duration_ms": 0,
                "command": [],
                "error": str(exc),
            },
            engineering_status="unknown",
            summary="OpenADA could not complete the request.",
            diagnostics=[diagnostic("error", "openada.exception", str(exc))],
        )
    _print_payload(payload, compact=args.compact)
    return _exit_code(payload)


if __name__ == "__main__":
    sys.exit(main())
