"""Bounded self-checking RTL test execution through Icarus or Verilator."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re

from ..contract import FileRecordError, diagnostic, file_record, result, static_execution, tool_record
from ..discovery import DiscoveryManager
from ..process import ProcessResult, run_process
from .hdl import changed_input_paths, hdl_closure_stability, resolve_hdl_inputs, valid_hdl_identifier, write_process_transcript


_BACKENDS = {"iverilog", "verilator"}
_DEFINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@'\-]*)?")
_CAPTURE_LIMIT = 4 * 1024 * 1024
_ARTIFACT_LIMIT = 32 * 1024 * 1024


def _environment(*binaries: str | None) -> dict[str, str]:
    directories = [str(Path(item).parent) for item in binaries if item]
    directories.extend(item for item in os.defpath.split(os.pathsep) if item)
    return {"PATH": os.pathsep.join(dict.fromkeys(directories)), "LANG": "C", "LC_ALL": "C"}


def _complete(process: ProcessResult) -> bool:
    return (
        process.status == "completed"
        and not process.stdout_truncated
        and not process.stderr_truncated
        and process.stdout_utf8_valid
        and process.stderr_utf8_valid
    )


def _stage(process: ProcessResult) -> dict:
    return {
        "status": process.status,
        "exit_code": process.exit_code,
        "duration_ms": process.duration_ms,
        "command": process.command,
        "stdout_bytes": process.stdout_bytes,
        "stderr_bytes": process.stderr_bytes,
        "capture_complete": _complete(process),
    }


class RTLTestDriver:
    """Compile and run one explicitly declared self-checking HDL top."""

    def __init__(self, *, discovery: DiscoveryManager | None = None) -> None:
        self.discovery = discovery or DiscoveryManager()

    def rtl_test(
        self,
        sources: list[str | Path],
        output_dir: str | Path,
        *,
        top: str,
        backend: str = "iverilog",
        include_dirs: list[str | Path] | None = None,
        defines: list[str] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        include_dirs = list(include_dirs or ())
        defines = list(defines or ())
        out_dir = Path(output_dir).expanduser().resolve()
        compile_log = out_dir / "rtl-test.compile.log"
        run_log = out_dir / "rtl-test.run.log"
        source_paths, dependencies, inputs, input_errors, unresolved = resolve_hdl_inputs(sources, include_dirs)
        compile_tool_name = backend if backend in _BACKENDS else "iverilog"
        compile_binary = self.discovery.find_binary(compile_tool_name)
        runtime_binary = self.discovery.find_binary("vvp") if backend == "iverilog" else None
        environment = _environment(compile_binary, runtime_binary)
        compile_identity_before_probe = self.discovery._binary_identity(compile_binary) if compile_binary else None
        runtime_identity_before_probe = self.discovery._binary_identity(runtime_binary) if runtime_binary else None
        compile_info = self.discovery.inspect_tool(compile_tool_name, probe_environment=environment)
        runtime_info = self.discovery.inspect_tool("vvp", probe_environment=environment) if backend == "iverilog" else None
        compile_identity = self.discovery._binary_identity(compile_binary) if compile_binary else None
        runtime_identity = self.discovery._binary_identity(runtime_binary) if runtime_binary else None
        probe_identities_stable = (
            compile_identity_before_probe is not None
            and compile_identity_before_probe == compile_identity
            and compile_info.get("binary") == compile_binary
        )
        if backend == "iverilog":
            probe_identities_stable = (
                probe_identities_stable
                and runtime_identity_before_probe is not None
                and runtime_identity_before_probe == runtime_identity
                and runtime_info is not None
                and runtime_info.get("binary") == runtime_binary
            )
        tool = tool_record(compile_tool_name, path=compile_binary, version=compile_info.get("version"))
        data = {
            "protocol": {
                "operation_profile": "openada.operation/rtl.test/v1alpha1",
                "assertion_profile": "openada.assertion/rtl.self-test.passes/v1alpha1",
                "implementation_id": f"org.openada.driver.{compile_tool_name}.rtl-test",
                "implementation_version": "1.0.0",
            },
            "backend": backend if backend in _BACKENDS else None,
            "top": top if isinstance(top, str) and valid_hdl_identifier(top) else None,
            "pass_policy": "self-checking-exit-zero",
            "environment_policy": "closed-rtl-test-runtime-v1",
            "ordered_sources": [str(path) for path in source_paths],
            "include_dependencies": [str(path) for path in dependencies],
            "unresolved_literal_includes": unresolved[:100],
            "unresolved_literal_includes_truncated": len(unresolved) > 100,
            "inputs_stable": None,
            "dependency_closure_stable": None,
            "tool_identity_stable": None,
            "runtime_tool": None if runtime_info is None else {
                "name": "vvp", "path": runtime_binary, "version": runtime_info.get("version")
            },
            "stages": [],
        }
        errors = list(input_errors)
        if not source_paths:
            errors.append("provide at least one ordered HDL source")
        if backend not in _BACKENDS:
            errors.append(f"unsupported RTL test backend: {backend}")
        if not isinstance(top, str) or not valid_hdl_identifier(top):
            errors.append(f"unsupported testbench top-module name: {top}")
        if len(defines) > 256 or len(set(defines)) != len(defines):
            errors.append("preprocessor defines must be unique and limited to 256")
        if any(not isinstance(value, str) or len(value) > 1024 or not _DEFINE.fullmatch(value) for value in defines):
            errors.append("invalid preprocessor define")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            errors.append("timeout must be finite and greater than zero")
        if out_dir.is_file() or any(path in source_paths + dependencies for path in (compile_log, run_log)):
            errors.append("the output directory must not overwrite an HDL input")
        expected_executable = out_dir / "rtl-test.vvp" if backend == "iverilog" else out_dir / "build" / "rtl-test.bin"
        if any(os.path.lexists(path) for path in (compile_log, run_log, expected_executable, out_dir / "build")):
            errors.append("RTL test transcripts, executable, and build directory must be absent before launch")
        if errors:
            return result("rtl-test", tool=tool, execution=static_execution("invalid_request"), engineering_status="unknown", summary="The RTL self-test request is incomplete or unsafe.", inputs=inputs, diagnostics=[diagnostic("error", "input.invalid", item) for item in errors[:100]], data=data)
        if not compile_binary or compile_info.get("status") != "available" or not compile_info.get("version"):
            return result("rtl-test", tool=tool, execution=static_execution("not_available"), engineering_status="unknown", summary="The selected RTL compiler is unavailable or unverified.", inputs=inputs, diagnostics=[diagnostic("error", "tool.unusable", f"{compile_tool_name} was not identity/version validated")], data=data)
        if backend == "iverilog" and (not runtime_binary or not runtime_info or runtime_info.get("status") != "available" or not runtime_info.get("version")):
            return result("rtl-test", tool=tool, execution=static_execution("not_available"), engineering_status="unknown", summary="The Icarus runtime is unavailable or unverified.", inputs=inputs, diagnostics=[diagnostic("error", "tool.unusable", "vvp was not identity/version validated")], data=data)
        if not probe_identities_stable:
            data["tool_identity_stable"] = False
            return result("rtl-test", tool=tool, execution=static_execution("not_available"), engineering_status="unknown", summary="An RTL test tool changed during identity/version validation.", inputs=inputs, diagnostics=[diagnostic("error", "tool.changed", "A compiler or runtime identity was not stable across its version probe.")], data=data)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            build_dir = out_dir / "build"
            build_dir.mkdir()
        except OSError as exc:
            return result("rtl-test", tool=tool, execution=static_execution("failed"), engineering_status="unknown", summary="OpenADA could not prepare a fresh RTL test workspace.", inputs=inputs, diagnostics=[diagnostic("error", "output.unavailable", str(exc))], data=data)

        if backend == "iverilog":
            executable = out_dir / "rtl-test.vvp"
            compile_command = [compile_binary, "-g2012", "-s", top, "-o", str(executable)]
            compile_command.extend(f"-I{Path(item).expanduser().resolve()}" for item in include_dirs)
            compile_command.extend(f"-D{item}" for item in defines)
            compile_command.extend(str(path) for path in source_paths)
        else:
            executable = build_dir / "rtl-test.bin"
            compile_command = [compile_binary, "--binary", "--timing", "--Mdir", str(build_dir), "-o", "rtl-test.bin", "--top-module", top]
            compile_command.extend(f"-I{Path(item).expanduser().resolve()}" for item in include_dirs)
            compile_command.extend(f"-D{item}" for item in defines)
            compile_command.extend(str(path) for path in source_paths)
        compile_process = run_process(compile_command, cwd=build_dir, timeout=timeout, env=environment, capture_limit_bytes=_CAPTURE_LIMIT)
        try:
            write_process_transcript(compile_log, compile_process)
        except (OSError, ValueError) as exc:
            return result("rtl-test", tool=tool, execution=compile_process, engineering_status="unknown", summary="The compile transcript could not be retained.", inputs=inputs, diagnostics=[diagnostic("error", "artifact.uncaptured", str(exc))], data={**data, "stages": [_stage(compile_process)]})
        run_process_result: ProcessResult | None = None
        if _complete(compile_process) and compile_process.exit_code == 0 and executable.is_file():
            run_command = [runtime_binary, str(executable)] if backend == "iverilog" else [str(executable)]
            run_process_result = run_process(run_command, cwd=build_dir, timeout=timeout, env=environment, capture_limit_bytes=_CAPTURE_LIMIT)
            try:
                write_process_transcript(run_log, run_process_result)
            except (OSError, ValueError):
                run_process_result = None

        changed = changed_input_paths(inputs)
        closure_stable, closure_changes = hdl_closure_stability(sources, include_dirs, expected_sources=source_paths, expected_dependencies=dependencies, expected_unresolved=unresolved)
        identities_stable = compile_identity == self.discovery._binary_identity(compile_binary)
        if backend == "iverilog":
            identities_stable = identities_stable and runtime_identity == self.discovery._binary_identity(runtime_binary)
        data["inputs_stable"] = not changed
        data["dependency_closure_stable"] = closure_stable
        data["tool_identity_stable"] = identities_stable
        data["stages"] = [_stage(compile_process)] + ([_stage(run_process_result)] if run_process_result else [])
        trustworthy = not changed and closure_stable and identities_stable and _complete(compile_process)
        if trustworthy and compile_process.exit_code != 0:
            status, summary = "fail", "The declared RTL self-test did not compile or elaborate successfully."
        elif trustworthy and run_process_result is not None and _complete(run_process_result):
            status = "pass" if run_process_result.exit_code == 0 else "fail"
            summary = "The declared self-checking RTL test passed." if status == "pass" else "The declared self-checking RTL test exited nonzero."
        else:
            status, summary = "unknown", "The RTL self-test did not produce complete trustworthy evidence."
        diagnostics = []
        if changed:
            diagnostics.append(diagnostic("error", "input.changed", ", ".join(changed[:10])))
        if not closure_stable:
            diagnostics.append(diagnostic("error", "input.dependency_closure_changed", "; ".join(closure_changes[:10])))
        if not identities_stable:
            diagnostics.append(diagnostic("error", "tool.changed", "A selected RTL test executable changed during invocation."))
        if not _complete(compile_process) or (run_process_result is not None and not _complete(run_process_result)):
            diagnostics.append(diagnostic("error", "evidence.incomplete", "A bounded compile or run stage timed out, failed, was truncated, or emitted invalid UTF-8."))
        if compile_process.exit_code == 0 and not executable.is_file():
            diagnostics.append(diagnostic("error", "artifact.missing", "The compiler exited zero without producing the declared test executable."))
        artifacts = []
        for path, kind, role, limit in ((compile_log, "rtl-test-log", "rtl.test.compile.log", _CAPTURE_LIMIT + 4096), (run_log, "rtl-test-log", "rtl.test.run.log", _CAPTURE_LIMIT + 4096), (executable, "rtl-test-executable", "rtl.test.executable", _ARTIFACT_LIMIT)):
            if path.is_file():
                try:
                    artifacts.append(file_record(path, kind=kind, role=role, maximum_bytes=limit))
                except (FileRecordError, OSError, ValueError) as exc:
                    status, summary = "unknown", "RTL test evidence exceeded a bound or changed during capture."
                    diagnostics.append(diagnostic("error", "artifact.uncaptured", str(exc)))
        return result("rtl-test", tool=tool, execution=run_process_result or compile_process, engineering_status=status, summary=summary, inputs=inputs, artifacts=artifacts, diagnostics=diagnostics, data=data)
