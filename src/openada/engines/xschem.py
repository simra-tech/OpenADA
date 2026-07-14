"""Xschem netlisting driver."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile

from ..contract import diagnostic, file_record, result, static_execution, tool_record
from ..discovery import DiscoveryManager
from ..process import run_process


def _excerpt(value: str, limit: int = 2_000) -> str:
    return value[-limit:]


MISSING_SYMBOL_RE = re.compile(
    r"^\s*\*\s+\S+\s+-\s+.*\bIS\s+MISSING\b",
    re.IGNORECASE,
)
MAX_MISSING_SYMBOLS = 50


def _scan_missing_symbols(path: Path) -> tuple[int, list[str]]:
    count = 0
    excerpts: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not MISSING_SYMBOL_RE.search(line):
                continue
            count += 1
            if len(excerpts) < MAX_MISSING_SYMBOLS:
                excerpts.append(line.strip()[:1_000])
    return count, excerpts


class XschemDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"xschem": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("xschem")

    def _supports_output_directory(self, version: str | None) -> bool:
        if not self.binary:
            return False
        version_match = re.search(r"\bV?(\d+)\.", version or "", re.IGNORECASE)
        if version_match and int(version_match.group(1)) >= 3:
            return True
        with tempfile.TemporaryDirectory(prefix="openada-xschem-probe-") as probe_dir:
            for help_arg in ("--help", "-h"):
                help_result = run_process(
                    [self.binary, help_arg], cwd=probe_dir, timeout=5.0
                )
                help_text = "\n".join((help_result.stdout, help_result.stderr))
                if re.search(r"(?m)^\s*-o(?:\s|,)", help_text):
                    return True
        return False

    def netlist(
        self,
        schematic_path: str | Path,
        output_path: str | Path,
        *,
        rcfile: str | Path | None = None,
        timeout: float = 60.0,
    ) -> dict:
        schematic = Path(schematic_path).expanduser().resolve()
        output = Path(output_path).expanduser().resolve()
        rcfile_path = Path(rcfile).expanduser().resolve() if rcfile else None
        info = self.discovery.inspect_tool("xschem")
        tool = tool_record("xschem", path=self.binary, version=info["version"])
        inputs = [file_record(schematic, kind="xschem-schematic", role="input")]
        if rcfile_path:
            inputs.append(file_record(rcfile_path, kind="xschem-rcfile", role="configuration"))

        missing = [record["path"] for record in inputs if not record["exists"]]
        if missing:
            return result(
                "netlist",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="One or more Xschem inputs do not exist.",
                inputs=inputs,
                diagnostics=[
                    diagnostic("error", "input.missing", f"File not found: {path}")
                    for path in missing
                ],
            )
        if output in {schematic, rcfile_path} or (output.exists() and output.is_dir()):
            if output == schematic:
                message = "The netlist output must not overwrite the source schematic."
            elif rcfile_path and output == rcfile_path:
                message = "The netlist output must not overwrite the Xschem rcfile."
            else:
                message = "The requested netlist output path is a directory."
            return result(
                "netlist",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary=message,
                inputs=inputs,
                diagnostics=[diagnostic("error", "output.invalid", message)],
            )
        if not self.binary:
            return result(
                "netlist",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="Xschem is not available in the selected runtime.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "tool.missing",
                        "Xschem was not found.",
                        hint="Install xschem, add it to PATH, or select the IIC-OSIC-TOOLS profile.",
                    )
                ],
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        produced = False
        artifact_empty = False
        missing_symbol_count = 0
        missing_symbols: list[str] = []
        with tempfile.TemporaryDirectory(prefix="openada-xschem-") as temp_dir:
            command = [self.binary]
            if rcfile_path:
                # Keep startup configuration explicit and ahead of batch flags
                # as the canonical invocation across Xschem versions.
                command.extend(("--rcfile", str(rcfile_path)))
            command.extend(("-n", "-s", "-q", "-x"))
            supports_output_directory = self._supports_output_directory(info["version"])
            if supports_output_directory:
                command.extend(("-o", temp_dir))
            command.append(str(schematic))
            process = run_process(
                command,
                cwd=schematic.parent if supports_output_directory else temp_dir,
                timeout=timeout,
            )
            generated = Path(temp_dir) / f"{schematic.stem}.spice"
            if process.status == "completed" and process.exit_code == 0 and generated.is_file():
                shutil.move(str(generated), str(output))
                produced = True
                artifact_empty = output.stat().st_size == 0
                if not artifact_empty:
                    missing_symbol_count, missing_symbols = _scan_missing_symbols(output)

        succeeded = (
            process.status == "completed"
            and process.exit_code == 0
            and produced
            and not artifact_empty
            and missing_symbol_count == 0
        )
        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(
                diagnostic("error", f"execution.{process.status}", process.error or "Xschem did not complete.")
            )
        elif process.exit_code != 0:
            diagnostics.append(
                diagnostic("error", "xschem.nonzero_exit", f"Xschem exited with code {process.exit_code}.")
            )
        elif not produced:
            diagnostics.append(
                diagnostic("error", "artifact.missing", "Xschem completed without producing the expected netlist.")
            )
        elif artifact_empty:
            diagnostics.append(
                diagnostic("error", "artifact.empty", "Xschem produced an empty SPICE netlist.")
            )
        elif missing_symbol_count:
            diagnostics.append(
                diagnostic(
                    "error",
                    "xschem.missing_symbol",
                    f"The generated netlist contains {missing_symbol_count} unresolved symbol(s).",
                    hint="Select the project/PDK rcfile and verify its library search paths.",
                )
            )
        if process.stderr:
            diagnostics.append(diagnostic("warning", "xschem.stderr", _excerpt(process.stderr)))

        if succeeded:
            engineering_status = "pass"
        elif missing_symbol_count:
            # A retained, non-empty netlist containing Xschem's recognized
            # unresolved-symbol marker is affirmative engineering evidence.
            engineering_status = "fail"
        else:
            # A non-zero exit, missing output, or empty output does not support
            # a conclusion about the design itself.
            engineering_status = "unknown"
        return result(
            "netlist",
            tool=tool,
            execution=process,
            engineering_status=engineering_status,
            summary=(
                "Xschem generated a SPICE netlist."
                if succeeded
                else (
                    "Xschem generated a netlist with unresolved symbols."
                    if missing_symbol_count
                    else "Xschem did not generate the requested SPICE netlist."
                )
            ),
            inputs=inputs,
            artifacts=[file_record(output, kind="spice-netlist", role="output")] if produced else [],
            diagnostics=diagnostics,
            data={
                "stdout_tail": _excerpt(process.stdout),
                "stderr_tail": _excerpt(process.stderr),
                "missing_symbol_count": missing_symbol_count,
                "missing_symbols": missing_symbols,
                "missing_symbols_truncated": missing_symbol_count > len(missing_symbols),
            },
        )


XschemEngine = XschemDriver
