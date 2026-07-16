"""Verilator RTL lint driver with strict, evidence-backed warning semantics."""

from __future__ import annotations

from pathlib import Path
import math
import os
import re
import tempfile

from ..contract import (
    FileRecordError,
    diagnostic,
    file_record,
    result,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..process import run_process
from .hdl import (
    changed_input_paths,
    hdl_closure_stability,
    resolve_hdl_inputs,
    valid_hdl_identifier,
    write_process_transcript,
)


_DEFINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@'\-]*)?")
_DIAGNOSTIC = re.compile(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?:\s*(.*)$")
_LANGUAGES = {"1800-2017", "1800-2023"}
_ENVIRONMENT_POLICY = "closed-verilator-runtime-v1"
_DESIGN_WARNING_CODES = {
    "ALWCOMBORDER",
    "ASCRANGE",
    "CASEINCOMPLETE",
    "CASEOVERLAP",
    "CASEWITHX",
    "CMPCONST",
    "COMBDLY",
    "DECLFILENAME",
    "EOFNEWLINE",
    "GENUNNAMED",
    "IMPLICIT",
    "IMPORTSTAR",
    "INCABSPATH",
    "LATCH",
    "LITENDIAN",
    "MODDUP",
    "MULTIDRIVEN",
    "PINCONNECTEMPTY",
    "PINMISSING",
    "PINNOCONNECT",
    "REALCVT",
    "SELRANGE",
    "SYNCASYNCNET",
    "UNDRIVEN",
    "UNOPTFLAT",
    "UNSIGNED",
    "UNUSED",
    "UNUSEDGENVAR",
    "UNUSEDPARAM",
    "UNUSEDSIGNAL",
    "VARHIDDEN",
    "WIDTH",
    "WIDTHEXPAND",
    "WIDTHTRUNC",
}
_DESIGN_ERROR_CODES = {
    "ASSIGNIN",
    "BLKANDNBLK",
    "DUPMOD",
    "ENUMVALUE",
    "MODNOTFOUND",
    "PINNOTFOUND",
    "PKGNODECL",
}
_MISSING_TOP_ERROR_TEXT = re.compile(
    r"^specified --top-module .{1,512} was not found in design\.?$",
    re.IGNORECASE,
)
_LOCATED_DESIGN_ERROR_TEXT = re.compile(
    r"^.+:\d+:\d+:\s*(?:"
    r"syntax error\b|duplicate (?:declaration|module)\b|"
    r"can't find definition of (?:signal|variable|task|function)\b)",
    re.IGNORECASE,
)


def _native_classification(severity: str, code: str, message: str) -> str:
    if severity == "warning" and code in _DESIGN_WARNING_CODES:
        return "design-finding"
    if severity == "error" and (
        code in _DESIGN_ERROR_CODES
        or _MISSING_TOP_ERROR_TEXT.fullmatch(message)
        or _LOCATED_DESIGN_ERROR_TEXT.match(message)
    ):
        return "design-finding"
    return "unclassified"


def _sanitized_environment(binary: str | None) -> dict[str, str]:
    """Build one closed runtime without ambient tool/interpreter injection."""
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


class VerilatorDriver:
    """Normalize strict Verilator lint without hiding warnings or accepting logs."""

    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"verilator": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("verilator")

    def rtl_lint(
        self,
        sources: list[str | Path],
        output_dir: str | Path,
        *,
        top: str,
        include_dirs: list[str | Path] | None = None,
        defines: list[str] | None = None,
        language: str = "1800-2017",
        timeout: float = 120.0,
    ) -> dict:
        include_dirs = list(include_dirs or ())
        defines = list(defines or ())
        out_dir = Path(output_dir).expanduser().resolve()
        transcript = out_dir / "rtl-lint.log"
        (
            source_paths,
            dependencies,
            inputs,
            input_errors,
            unresolved_includes,
        ) = resolve_hdl_inputs(sources, include_dirs)
        environment = _sanitized_environment(self.binary)
        inspected_identity_before = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        info = self.discovery.inspect_tool(
            "verilator", probe_environment=environment
        )
        inspected_identity_after = (
            self.discovery._binary_identity(self.binary) if self.binary else None
        )
        inspected_tool_identity_stable = (
            self.binary is not None
            and info.get("binary") == self.binary
            and inspected_identity_before is not None
            and inspected_identity_before == inspected_identity_after
        )
        tool = tool_record(
            "verilator",
            path=self.binary,
            version=(
                info.get("version") if inspected_tool_identity_stable else None
            ),
        )

        top_valid = isinstance(top, str) and valid_hdl_identifier(top)
        language_valid = isinstance(language, str) and language in _LANGUAGES
        base_data = {
            "protocol": {
                "operation_profile": "openada.operation/rtl.lint/v1alpha1",
                "assertion_profile": "openada.assertion/rtl.lint.clean/v1alpha1",
                "implementation_id": "org.openada.driver.verilator",
                "implementation_version": "1.0.0",
            },
            "top": top if top_valid else None,
            "language": language if language_valid else None,
            "warning_policy": "strict",
            "environment_policy": _ENVIRONMENT_POLICY,
            "ordered_sources": [str(path) for path in source_paths],
            "include_dependencies": [str(path) for path in dependencies],
            "unresolved_literal_includes": unresolved_includes[:100],
            "unresolved_literal_includes_truncated": len(unresolved_includes) > 100,
            "inputs_stable": None,
            "dependency_closure_stable": None,
            "changed_inputs": [],
            "changed_inputs_truncated": False,
            "tool_identity_stable": None,
            "warning_count": 0,
            "error_count": 0,
            "diagnostic_count": 0,
            "unclassified_diagnostic_count": 0,
            "diagnostics": [],
            "diagnostics_truncated": False,
        }

        request_errors = list(input_errors)
        if not source_paths:
            request_errors.append("provide at least one RTL source")
        if not top_valid:
            request_errors.append(f"unsupported top-module name: {top}")
        if not language_valid:
            request_errors.append(f"unsupported SystemVerilog language revision: {language}")
        invalid_defines = [
            value
            for value in defines
            if not isinstance(value, str)
            or len(value) > 1_024
            or not _DEFINE.fullmatch(value)
        ]
        request_errors.extend(f"invalid preprocessor define: {value}" for value in invalid_defines)
        if len(defines) > 256:
            request_errors.append("no more than 256 preprocessor defines are allowed")
        if len({value for value in defines if isinstance(value, str)}) != len(defines):
            request_errors.append("preprocessor defines must be unique")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            request_errors.append("timeout must be finite and greater than zero")
        if out_dir.is_file() or transcript in source_paths or transcript in dependencies:
            request_errors.append("the output directory must not overwrite an HDL input")
        if os.path.lexists(transcript):
            request_errors.append("the lint transcript must be absent before launch")
        if request_errors:
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("invalid_request"),
                engineering_status="unknown",
                summary="The RTL lint request is incomplete or unsafe.",
                inputs=inputs,
                diagnostics=[
                    diagnostic("error", "input.invalid", message)
                    for message in request_errors[:100]
                ],
                data=base_data,
            )
        if not self.binary:
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="Verilator is not available in the selected runtime.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "tool.missing", "Verilator was not found.")],
                data=base_data,
            )
        if not inspected_tool_identity_stable:
            base_data["tool_identity_stable"] = False
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="The selected Verilator executable changed during identity validation.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "tool.changed",
                        "The version probe was not bound to the selected Verilator executable identity.",
                    )
                ],
                data=base_data,
            )

        if info.get("status") != "available" or not info.get("version"):
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="The selected Verilator executable failed identity/version validation.",
                inputs=inputs,
                diagnostics=[
                    diagnostic(
                        "error",
                        "tool.unusable",
                        "Verilator exists but its bounded version probe did not match the supported product identity.",
                    )
                ],
                data=base_data,
            )

        tool_identity_before = inspected_identity_before
        if tool_identity_before is None:
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="The selected Verilator executable identity is unavailable.",
                inputs=inputs,
                diagnostics=[
                    diagnostic("error", "tool.unusable", "Verilator cannot be stat-bound before launch.")
                ],
                data=base_data,
            )
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("failed"),
                engineering_status="unknown",
                summary="OpenADA could not prepare the RTL lint evidence directory.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "output.unavailable", str(exc))],
                data=base_data,
            )
        command = [
            self.binary,
            "--lint-only",
            "--timing",
            "--Wall",
            "-Wno-fatal",
            "--relative-includes",
            "--default-language",
            language,
            "--top-module",
            top,
        ]
        command.extend(
            f"+{language}ext+{extension}"
            for extension in ("v", "sv", "vh", "svh")
        )
        command.extend(f"-I{Path(item).expanduser().resolve()}" for item in include_dirs)
        command.extend(f"-D{value}" for value in defines)
        command.extend(str(path) for path in source_paths)

        try:
            with tempfile.TemporaryDirectory(prefix=".openada-verilator-", dir=out_dir) as work:
                process = run_process(
                    command,
                    cwd=work,
                    timeout=timeout,
                    env=environment,
                    capture_limit_bytes=1024 * 1024,
                )
        except OSError as exc:
            return result(
                "rtl-lint",
                tool=tool,
                execution=static_execution("failed"),
                engineering_status="unknown",
                summary="OpenADA could not create an isolated Verilator workspace.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "output.unavailable", str(exc))],
                data=base_data,
            )
        transcript_error: str | None = None
        try:
            write_process_transcript(transcript, process)
        except (OSError, ValueError) as exc:
            transcript_error = str(exc)

        changed_inputs = changed_input_paths(inputs)
        inputs_stable = not changed_inputs
        dependency_closure_stable, closure_changes = hdl_closure_stability(
            sources,
            include_dirs,
            expected_sources=source_paths,
            expected_dependencies=dependencies,
            expected_unresolved=unresolved_includes,
        )
        base_data["inputs_stable"] = inputs_stable
        base_data["dependency_closure_stable"] = dependency_closure_stable
        base_data["changed_inputs"] = changed_inputs[:100]
        base_data["changed_inputs_truncated"] = len(changed_inputs) > 100
        tool_identity_stable = (
            tool_identity_before == self.discovery._binary_identity(self.binary)
        )
        base_data["tool_identity_stable"] = tool_identity_stable

        native_records: list[dict] = []
        for line in "\n".join((process.stdout, process.stderr)).splitlines():
            native_line = line.strip()
            match = _DIAGNOSTIC.match(native_line)
            if match:
                severity = match.group(1).lower()
                native_code = match.group(2) or "UNCLASSIFIED"
                if len(native_code) > 120:
                    native_code = "UNCLASSIFIED"
                message = match.group(3).strip()[:1_000]
                if not message:
                    message = "Verilator emitted an empty native diagnostic."
                native_records.append(
                    {
                        "severity": severity,
                        "code": native_code,
                        "message": message,
                        "classification": _native_classification(
                            severity, native_code, message
                        ),
                    }
                )
            elif native_line.startswith(("%Warning", "%Error")):
                severity = (
                    "warning" if native_line.startswith("%Warning") else "error"
                )
                native_records.append(
                    {
                        "severity": severity,
                        "code": "UNCLASSIFIED",
                        "message": native_line[:1_000],
                        "classification": "unclassified",
                    }
                )
        if any(
            item["classification"] == "design-finding" and item["severity"] == "error"
            for item in native_records
        ):
            for item in native_records:
                if re.fullmatch(r"Exiting due to \d+ error\(s\)", item["message"]):
                    item["classification"] = "design-finding"
        warnings = [item for item in native_records if item["severity"] == "warning"]
        errors = [item for item in native_records if item["severity"] == "error"]
        unclassified = [
            item for item in native_records if item["classification"] == "unclassified"
        ]
        capture_complete = (
            not process.stdout_truncated
            and not process.stderr_truncated
            and process.stdout_utf8_valid
            and process.stderr_utf8_valid
        )
        if (
            process.status == "completed"
            and capture_complete
            and inputs_stable
            and dependency_closure_stable
            and tool_identity_stable
            and transcript_error is None
            and not native_records
            and process.exit_code == 0
        ):
            status = "pass"
            summary = "Verilator completed strict RTL lint with no warnings or errors."
        elif (
            process.status == "completed"
            and capture_complete
            and inputs_stable
            and dependency_closure_stable
            and tool_identity_stable
            and transcript_error is None
            and native_records
            and not unclassified
        ):
            status = "fail"
            summary = "Verilator reported RTL lint warnings or errors under the strict policy."
        else:
            status = "unknown"
            summary = "RTL lint did not yield complete trustworthy diagnostic evidence."

        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(diagnostic("error", f"execution.{process.status}", process.error or "Verilator did not complete."))
        if not capture_complete:
            diagnostics.append(diagnostic("error", "evidence.incomplete", "Verilator output was truncated or was not valid UTF-8."))
        if changed_inputs:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.changed",
                    "An RTL input changed while Verilator was running: "
                    + ", ".join(changed_inputs[:10]),
                )
            )
        if transcript_error is not None:
            diagnostics.append(
                diagnostic("error", "artifact.uncaptured", transcript_error)
            )
        if not tool_identity_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "tool.changed",
                    "The Verilator executable identity changed during invocation.",
                )
            )
        if not dependency_closure_stable:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.dependency_closure_changed",
                    "The literal HDL dependency closure changed while Verilator was "
                    "running: " + "; ".join(closure_changes[:10]),
                )
            )
        if process.status == "completed" and process.exit_code not in (0, None) and not native_records:
            diagnostics.append(diagnostic("error", "verilator.unclassified_exit", f"Verilator exited with code {process.exit_code} without a recognized diagnostic."))
        retained_native_records = native_records[:100]
        for item in retained_native_records:
            if item["classification"] == "design-finding":
                diagnostics.append(
                    diagnostic(
                        item["severity"],
                        f"verilator.native-{item['severity']}",
                        item["message"],
                    )
                )
            else:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "verilator.unclassified_diagnostic",
                        f"Native {item['severity']} {item['code']}: {item['message']}",
                    )
                )
        if unclassified and not any(
            item["classification"] == "unclassified"
            for item in retained_native_records
        ):
            diagnostics.append(
                diagnostic(
                    "error",
                    "verilator.unclassified_diagnostic",
                    f"{len(unclassified)} unclassified native diagnostic record(s) "
                    "occurred after the first 100 retained records; inspect the "
                    "complete transcript.",
                )
            )
        artifacts: list[dict] = []
        if transcript_error is None:
            try:
                transcript_record = file_record(
                    transcript,
                    kind="verilator-log",
                    role="rtl.lint.log",
                    maximum_bytes=4 * 1024 * 1024,
                )
                if transcript_record["exists"]:
                    artifacts.append(transcript_record)
                else:
                    transcript_error = "the retained lint transcript disappeared"
            except (FileRecordError, OSError, ValueError) as exc:
                transcript_error = str(exc)
        if transcript_error is not None and status != "unknown":
            status = "unknown"
            summary = "RTL lint did not retain complete trustworthy transcript evidence."
        if transcript_error is not None and not any(
            item.get("code") == "artifact.uncaptured" for item in diagnostics
        ):
            diagnostics.append(
                diagnostic("error", "artifact.uncaptured", transcript_error)
            )
        return result(
            "rtl-lint",
            tool=tool,
            execution=process,
            engineering_status=status,
            summary=summary,
            inputs=inputs,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data={
                **base_data,
                "warning_count": len(warnings),
                "error_count": len(errors),
                "diagnostic_count": len(native_records),
                "unclassified_diagnostic_count": len(unclassified),
                "diagnostics": retained_native_records,
                "diagnostics_truncated": len(native_records) > 100,
            },
        )
