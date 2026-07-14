"""Standalone, JSON-first OpenADA command line interface."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import stat
import sys

from . import __version__
from .contract import diagnostic, result, static_execution
from .discovery import DiscoveryManager, TOOL_SPECS
from .engines import (
    KLayoutDriver,
    NetgenDriver,
    NgspiceDriver,
    NgspiceOutput,
    XschemDriver,
    YosysDriver,
)
from .operations import simulate_circuit_profile
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
    "drc": "drc",
    "lvs": "lvs",
    "rtl-check": "rtl-check",
}

MAX_PREFLIGHT_PATH_CHARS = 4_095
MAX_PREFLIGHT_PDK_ROOTS = 64
MAX_PREFLIGHT_TOOL_OVERRIDES = 64
MAX_PREFLIGHT_VERSION_TIMEOUT_SECONDS = 30.0


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
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
            "Select the shared transient-profile backend. Without this option, "
            "the legacy ngspice interface remains active."
        ),
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
            return _invalid_request(
                "simulate",
                "--backend and the legacy --tool selector disagree",
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
                return _invalid_request(
                    "simulate",
                    "The shared transient profile does not accept legacy ngspice option(s): "
                    + ", ".join(profile_only_options),
                )
            return simulate_circuit_profile(
                source,
                output_dir,
                backend=args.backend,
                discovery=discovery,
                workdir=args.workdir,
                timeout=args.timeout,
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


def _invalid_request(operation: str, message: str) -> dict:
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
        payload = _invalid_request(_requested_operation(raw_argv), str(exc))
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
