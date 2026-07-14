"""Small Yosys RTL elaboration and structural-check driver."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile

from ..contract import diagnostic, file_record, result, static_execution, tool_record
from ..discovery import DiscoveryManager
from ..process import run_process


MAX_JSON_PARSE_BYTES = 5 * 1024 * 1024


def _yosys_quote(path: Path) -> str:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise ValueError("Yosys paths may not contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_json_netlist(path: Path) -> tuple[bool, str]:
    size = path.stat().st_size
    if size == 0:
        return False, "empty"
    if size <= MAX_JSON_PARSE_BYTES:
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


class YosysDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"yosys": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("yosys")

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
