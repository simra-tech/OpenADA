"""Deterministic Xyce driver for shared DC, AC, and transient analyses."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from ..contract import (
    FileRecordError,
    FileRecordLimitError,
    diagnostic,
    file_record,
    result,
    static_execution,
    stable_regular_file,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..process import run_process
from .ngspice_outputs import analysis_raw_counts, validate_xyce_raw
from .spice import (
    MAX_LOG_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_LINE_BYTES,
    _capture_file,
    _move_regular_output,
    _read_captured_text,
    _tail,
)


_ANALYSIS_RE = re.compile(r"^\s*\.(op|dc|ac|tran|noise|hb)\b", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.(?:inc(?:lude)?|lib)\b", re.IGNORECASE)
_UNSUPPORTED_DIRECTIVE_RE = re.compile(
    r"^\s*\.(control|measure|meas|print|four|fft|step)\b", re.IGNORECASE
)
_TERMINAL_NONCONVERGENCE = (
    re.compile(
        r"^\s*Time step too small near step number:\s*.+?"
        r"\s+Exiting transient loop\.\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*Newton solver failed in constant time step mode\.\s*"
        r"Exiting transient loop\.\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*DC Operating Point Failed\.\s*Exiting transient loop\.?\s*$",
        re.IGNORECASE,
    ),
)
_NATIVE_ERROR_RE = re.compile(
    r"(?:\*{3,}\s*Xyce\s+Abort\s*\*{3,}|^\s*(?:fatal\s+)?error\b)",
    re.IGNORECASE,
)


def _invalid(
    tool: dict,
    inputs: list[dict],
    *,
    summary: str,
    code: str,
    message: str,
    data: dict,
) -> dict:
    return result(
        "simulate",
        tool=tool,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary=summary,
        inputs=inputs,
        diagnostics=[diagnostic("error", code, message)],
        data=data,
    )


def _inspect_deck(path: Path) -> dict[str, object]:
    analyses: list[str] = []
    unsupported: list[str] = []
    include_detected = False
    line_too_long = False
    source_too_large = False
    source_unstable = False
    try:
        with stable_regular_file(path) as (handle, _):
            line_number = 0
            source_bytes = 0
            while True:
                raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                if not raw_line:
                    break
                source_bytes += len(raw_line)
                if source_bytes > MAX_SOURCE_BYTES:
                    source_too_large = True
                    break
                line_number += 1
                if len(raw_line) > MAX_SOURCE_LINE_BYTES and not raw_line.endswith(b"\n"):
                    line_too_long = True
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                        source_bytes += len(raw_line)
                        if source_bytes > MAX_SOURCE_BYTES:
                            source_too_large = True
                            break
                    if source_too_large:
                        break
                    continue
                # SPICE-family parsers reserve the physical first line for the
                # circuit title even when it resembles a dot directive.
                if line_number == 1:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                stripped = line.lstrip()
                if not stripped or stripped.startswith("*"):
                    continue
                analysis_match = _ANALYSIS_RE.match(line)
                if analysis_match:
                    analyses.append(analysis_match.group(1).lower())
                if _INCLUDE_RE.match(line):
                    include_detected = True
                unsupported_match = _UNSUPPORTED_DIRECTIVE_RE.match(line)
                if unsupported_match:
                    unsupported.append(unsupported_match.group(1).lower())
    except FileRecordError:
        source_unstable = True
    return {
        "analyses": analyses,
        "include_detected": include_detected,
        "unsupported_directives": sorted(set(unsupported)),
        "line_too_long": line_too_long,
        "source_too_large": source_too_large,
        "source_unstable": source_unstable,
    }


class XyceDriver:
    """Run one model-free, top-level DC, AC, or transient analysis."""

    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"xyce": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("xyce")

    def simulate(
        self,
        spice_file: str | Path,
        output_dir: str | Path,
        *,
        workdir: str | Path | None = None,
        timeout: float = 120.0,
        analysis: Mapping[str, object] | None = None,
    ) -> dict:
        source = Path(spice_file).expanduser().resolve()
        out_dir = Path(output_dir).expanduser().resolve()
        run_dir = Path(workdir).expanduser().resolve() if workdir else source.parent
        info = self.discovery.inspect_tool("xyce")
        tool = tool_record("xyce", path=self.binary, version=info["version"])
        requested_analysis = dict(analysis) if isinstance(analysis, Mapping) else None
        base_data: dict[str, object] = {
            "analysis": requested_analysis,
            "converged": None,
            "inputs_stable": False,
            "working_directory": str(run_dir),
            "working_directory_is_sandbox": False,
            "transitive_inputs_enumerated": False,
            "environment_overrides": {"XYCE_NO_TRACKING": "1"},
        }
        try:
            inputs = [
                file_record(
                    source,
                    kind="spice-netlist",
                    role="input",
                    maximum_bytes=MAX_SOURCE_BYTES,
                )
            ]
        except FileRecordLimitError:
            return _invalid(
                tool,
                [],
                summary="The SPICE input exceeds the bounded source limit.",
                code="input.too_large",
                message=f"The top-level SPICE input must not exceed {MAX_SOURCE_BYTES} bytes.",
                data=base_data,
            )
        except FileRecordError:
            return _invalid(
                tool,
                [],
                summary="The SPICE input changed during bounded capture.",
                code="input.changed",
                message=f"The top-level SPICE input was not stable: {source}",
                data=base_data,
            )

        if not inputs[0]["exists"]:
            return _invalid(
                tool,
                inputs,
                summary="The SPICE input is not a readable regular file.",
                code="input.missing",
                message=f"Regular file not found: {source}",
                data=base_data,
            )
        if not run_dir.is_dir():
            return _invalid(
                tool,
                inputs,
                summary="The simulation working directory is invalid.",
                code="workdir.invalid",
                message=f"Working directory is not a directory: {run_dir}",
                data=base_data,
            )
        if out_dir.is_file():
            return _invalid(
                tool,
                inputs,
                summary="The simulation evidence destination is invalid.",
                code="output.invalid",
                message=f"Output directory resolves to a file: {out_dir}",
                data=base_data,
            )

        deck = _inspect_deck(source)
        base_data["deck_inspection"] = deck
        if deck["source_unstable"]:
            return _invalid(
                tool,
                inputs,
                summary="The SPICE input changed or became non-regular during inspection.",
                code="input.changed",
                message="The top-level SPICE input could not be inspected as one stable regular file.",
                data=base_data,
            )
        if deck["source_too_large"]:
            return _invalid(
                tool,
                inputs,
                summary="The SPICE input exceeds the bounded source limit.",
                code="input.too_large",
                message=f"The top-level SPICE input must not exceed {MAX_SOURCE_BYTES} bytes.",
                data=base_data,
            )
        if deck["line_too_long"]:
            return _invalid(
                tool,
                inputs,
                summary="The Xyce testbench cannot be safely inspected.",
                code="input.line_too_long",
                message=(
                    f"At least one source line exceeds the {MAX_SOURCE_LINE_BYTES}-byte "
                    "inspection bound."
                ),
                data=base_data,
            )
        if deck["include_detected"]:
            return _invalid(
                tool,
                inputs,
                summary="The initial shared simulation profile requires a flattened deck.",
                code="input.transitive_uninspected",
                message=(
                    ".include, .inc, and .lib are not accepted until transitive "
                    "inputs can be attested."
                ),
                data=base_data,
            )
        if deck["unsupported_directives"]:
            directives = ", ".join(
                f".{name}" for name in deck["unsupported_directives"]
            )
            return _invalid(
                tool,
                inputs,
                summary="The testbench requests behavior outside the initial shared profile.",
                code="simulation.feature.unsupported",
                message=f"Unsupported top-level directive(s): {directives}.",
                data=base_data,
            )
        analyses = deck["analyses"]
        if len(analyses) != 1 or analyses[0] not in {"dc", "ac", "tran"}:
            return _invalid(
                tool,
                inputs,
                summary="The Xyce raw-evidence mapping requires one DC, AC, or transient analysis.",
                code="simulation.analysis.unsupported",
                message=(
                    "Expected exactly one top-level .dc, .ac, or .tran directive; "
                    f"observed {analyses!r}."
                ),
                data=base_data,
            )
        analysis_type = analyses[0]
        if requested_analysis is None:
            requested_analysis = {"type": analysis_type}
            base_data["analysis"] = requested_analysis
        elif requested_analysis.get("type") != analysis_type:
            return _invalid(
                tool,
                inputs,
                summary="The Xyce analysis request conflicts with the deck.",
                code="simulation.request.invalid",
                message=(
                    f"Requested {requested_analysis.get('type')!r} but observed one "
                    f"top-level .{analysis_type} directive."
                ),
                data=base_data,
            )

        if not self.binary:
            return result(
                "simulate",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="Xyce is not available in the selected runtime.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "simulation.tool.unavailable",
                        "Xyce was not found or did not pass bounded version discovery.",
                        hint=(
                            "Install an open-source Xyce build, add it to PATH, "
                            "or pass --tool-path xyce=PATH."
                        ),
                    )
                ],
                data=base_data,
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / f"{source.stem}.xyce.log"
        raw_path = out_dir / f"{source.stem}.xyce.raw"
        process_environment = dict(os.environ)
        process_environment["XYCE_NO_TRACKING"] = "1"

        with tempfile.TemporaryDirectory(prefix="openada-xyce-") as temp_dir:
            temp_root = Path(temp_dir)
            temp_log = temp_root / "simulation.log"
            temp_raw = temp_root / "simulation.raw"
            command = [
                self.binary,
                "-l",
                str(temp_log),
                "-r",
                str(temp_raw),
                "-a",
                str(source),
            ]
            process = run_process(
                command,
                cwd=run_dir,
                timeout=timeout,
                env=process_environment,
            )
            produced_log = _move_regular_output(
                temp_log,
                log_path,
                maximum_bytes=MAX_LOG_BYTES,
            )
            produced_raw = _move_regular_output(temp_raw, raw_path)

        log_artifact = None
        log_capture: dict = {"path": str(log_path), "status": "missing"}
        if produced_log:
            log_artifact, log_capture = _capture_file(
                log_path,
                kind="xyce-log",
                role="evidence",
                maximum_bytes=MAX_LOG_BYTES,
            )

        raw_artifact = None
        raw_capture: dict = {"path": str(raw_path), "status": "missing"}
        if produced_raw:
            raw_artifact, raw_capture = _capture_file(
                raw_path,
                kind="xyce-raw",
                role="output",
                validator=validate_xyce_raw,
            )
        raw_capture["kind"] = "raw"
        raw_capture["origin"] = "wrapper"

        log_text = ""
        if log_capture["status"] == "valid":
            captured = _read_captured_text(
                log_path,
                log_capture,
                maximum_bytes=MAX_LOG_BYTES,
            )
            if captured is None:
                log_capture["status"] = "unstable"
                log_artifact = None
            else:
                log_text = captured

        transcript = "\n".join((process.stdout, process.stderr, log_text))
        try:
            with stable_regular_file(source) as (source_handle, _):
                source_title = (
                    source_handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
        except FileRecordError:
            source_title = ""
        terminal_nonconvergence = None
        native_error = None
        for line in log_text.splitlines():
            if (
                terminal_nonconvergence is None
                and line.strip() != source_title
                and any(
                    pattern.match(line) for pattern in _TERMINAL_NONCONVERGENCE
                )
            ):
                terminal_nonconvergence = line.strip()[:1_000]
        for line in transcript.splitlines():
            if native_error is None and _NATIVE_ERROR_RE.search(line):
                native_error = line.strip()[:1_000]

        try:
            current_input = file_record(
                source,
                kind="spice-netlist",
                role="input",
                maximum_bytes=MAX_SOURCE_BYTES,
            )
        except FileRecordError:
            inputs_stable = False
        else:
            inputs_stable = all(
                current_input.get(field) == inputs[0].get(field)
                for field in ("exists", "bytes", "sha256")
            )
        base_data["inputs_stable"] = inputs_stable

        counts = (
            analysis_raw_counts(raw_capture, requested_analysis)
            if raw_capture["status"] == "valid"
            else None
        )
        valid_log = log_capture["status"] == "valid"
        valid_raw = raw_capture["status"] == "valid" and counts is not None
        captures_complete = not process.stdout_truncated and not process.stderr_truncated
        passed = (
            process.status == "completed"
            and process.exit_code == 0
            and terminal_nonconvergence is None
            and native_error is None
            and valid_log
            and valid_raw
            and inputs_stable
            and captures_complete
        )
        conclusive_nonconvergence = (
            process.status == "completed"
            and process.exit_code == 1
            and terminal_nonconvergence is not None
            and native_error is None
            and valid_log
            and inputs_stable
            and captures_complete
        )

        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(
                diagnostic(
                    "error",
                    f"execution.{process.status}",
                    process.error or "Xyce did not complete.",
                )
            )
        elif process.exit_code != 0 and not conclusive_nonconvergence:
            diagnostics.append(
                diagnostic(
                    "error",
                    "simulation.analysis.unproven",
                    f"Xyce exited with code {process.exit_code}; the analysis "
                    "assertion is unproven.",
                )
            )
        if terminal_nonconvergence:
            diagnostics.append(
                diagnostic(
                    "error",
                    "simulation.analysis.non_convergent",
                    terminal_nonconvergence,
                )
            )
        if native_error:
            diagnostics.append(
                diagnostic("error", "simulation.native_error", native_error)
            )
        if not valid_log:
            diagnostics.append(
                diagnostic(
                    "error",
                    "simulation.log.missing"
                    if log_capture["status"] == "missing"
                    else "simulation.log.malformed",
                    "The fresh bounded Xyce log is unavailable or unstable "
                    f"({log_capture['status']}).",
                )
            )
        if not valid_raw:
            diagnostics.append(
                diagnostic(
                    "error",
                    "simulation.result.missing"
                    if raw_capture["status"] == "missing"
                    else "simulation.result.malformed",
                    f"The Xyce ASCII raw file is not complete {analysis_type} evidence "
                    f"({raw_capture['status']}).",
                )
            )
        if not inputs_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.changed",
                    "The declared SPICE input changed during Xyce execution.",
                )
            )
        if not captures_complete:
            diagnostics.append(
                diagnostic(
                    "error",
                    "execution.output_truncated",
                    "Captured Xyce stdout or stderr exceeded its bounded evidence limit.",
                )
            )

        if conclusive_nonconvergence:
            engineering_status = "fail"
            summary = (
                f"Xyce produced fresh native evidence of terminal {analysis_type} "
                "non-convergence."
            )
        elif passed:
            engineering_status = "pass"
            summary = (
                f"Xyce produced fresh, structurally valid {analysis_type} simulation evidence."
            )
        else:
            engineering_status = "unknown"
            summary = (
                "Xyce did not yield enough trustworthy evidence for an "
                "engineering conclusion."
            )

        artifacts = [item for item in (log_artifact, raw_artifact) if item is not None]
        base_data.update(
            {
                "converged": False if conclusive_nonconvergence else (True if passed else None),
                "log_tail": _tail(log_text or "\n".join((process.stdout, process.stderr))),
                "log_capture": log_capture,
                "output_captures": [raw_capture],
                "analysis_evidence": {
                    "raw": valid_raw,
                    "point_count": counts[0] if counts else None,
                    "dependent_variable_count": counts[1] if counts else None,
                    "finite_value_count": counts[2] if counts else None,
                },
            }
        )
        return result(
            "simulate",
            tool=tool,
            execution=process,
            engineering_status=engineering_status,
            summary=summary,
            inputs=inputs,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data=base_data,
        )


__all__ = ["XyceDriver"]
