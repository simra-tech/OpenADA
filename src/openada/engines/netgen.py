"""Netgen LVS driver with explicit fresh native evidence semantics."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re
from typing import Any

from ..contract import bounded_text, diagnostic, result, static_execution, tool_record
from ..discovery import DiscoveryManager
from ..process import MAX_CAPTURE_LIMIT_BYTES, run_process
from .netgen_outputs import (
    OutputAnchor,
    StableInput,
    anchor_is_fresh,
    capture_json,
    capture_report,
    open_output_anchor,
    open_stable_input,
    parse_netgen_report,
    stable_input_unchanged,
    transcript_assessment,
    write_transcript,
)


MAX_PROVENANCE_INPUTS = 128
MAX_CELL_NAME_CHARS = 1_024
CELL_RE = re.compile(r"[A-Za-z0-9_.$:+-]+")


def _static_invalid(
    tool: dict[str, Any],
    inputs: list[dict[str, Any]],
    data: dict[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    return result(
        "lvs",
        tool=tool,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The Netgen LVS request is invalid.",
        inputs=inputs,
        diagnostics=[diagnostic("error", code, message)],
        data=data,
    )


class NetgenDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"netgen": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("netgen")

    def lvs(
        self,
        layout_netlist: str | Path,
        schematic_netlist: str | Path,
        cell_name: str,
        setup_tcl: str | Path,
        report_path: str | Path,
        *,
        provenance_inputs: Sequence[str | Path] = (),
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        info = self.discovery.inspect_tool("netgen")
        tool = tool_record("netgen", path=self.binary, version=info["version"])
        base_data: dict[str, Any] = {
            "working_directory": None,
            "working_directory_is_sandbox": False,
            "report_output": {
                "ownership": "native",
                "fresh_required": True,
                "parent_anchored": True,
            },
            "json_output": {
                "ownership": "native-netgen-json",
                "fresh_required": True,
                "parent_anchored": True,
            },
            "setup_trust": "caller-supplied executable Tcl; OpenADA does not sandbox the setup",
            "transitive_setup_inputs_enumerated": False,
            "ambient_environment_enumerated": False,
            "inputs_stable": False,
            "changed_inputs": [],
        }
        if (
            not isinstance(cell_name, str)
            or len(cell_name) > MAX_CELL_NAME_CHARS
            or not CELL_RE.fullmatch(cell_name)
        ):
            return _static_invalid(
                tool,
                [],
                base_data,
                code="cell.invalid",
                message=(
                    "Cell names may contain letters, digits, underscore, dot, dollar, colon, "
                    "plus, and minus."
                ),
            )
        if (
            not isinstance(provenance_inputs, Sequence)
            or isinstance(provenance_inputs, (str, bytes))
            or len(provenance_inputs) > MAX_PROVENANCE_INPUTS
        ):
            return _static_invalid(
                tool,
                [],
                base_data,
                code="provenance.invalid",
                message=f"Provide at most {MAX_PROVENANCE_INPUTS} provenance input paths.",
            )
        try:
            run_dir = Path.cwd().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return _static_invalid(
                tool,
                [],
                base_data,
                code="workdir.invalid",
                message=f"Cannot resolve the Netgen working directory: {exc}",
            )
        if not run_dir.is_dir():
            return _static_invalid(
                tool,
                [],
                base_data,
                code="workdir.invalid",
                message="The Netgen working directory is not a directory.",
            )
        base_data["working_directory"] = str(run_dir)

        declarations = [
            (layout_netlist, "layout-netlist", "input"),
            (schematic_netlist, "schematic-netlist", "reference"),
            (setup_tcl, "netgen-setup", "rules"),
            *(
                (path, "netgen-rules-input", "rules-dependency")
                for path in provenance_inputs
            ),
        ]
        snapshots: list[StableInput] = []
        inputs: list[dict[str, Any]] = []
        anchor: OutputAnchor | None = None
        try:
            for path, kind, role in declarations:
                snapshot, input_error = open_stable_input(path, kind=kind, role=role)
                if input_error is not None or snapshot is None:
                    code, message = input_error or ("input.unreadable", "Cannot open input.")
                    return _static_invalid(tool, inputs, base_data, code=code, message=message)
                snapshots.append(snapshot)
                inputs.append(snapshot.record)
            input_paths = [snapshot.path for snapshot in snapshots]
            if len(set(input_paths)) != len(input_paths):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    code="input.duplicate",
                    message="Every LVS netlist, setup, and provenance input must resolve uniquely.",
                )
            if any(
                any(
                    character.isspace() or character in {'{', '}', '"', '\\'}
                    for character in str(path)
                )
                for path in input_paths[:2]
            ):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    code="path.tcl_list_unsafe",
                    message=(
                        "Netlist paths cannot contain whitespace, braces, quotes, or backslashes "
                        "because Netgen parses each path-and-cell argument as a Tcl list."
                    ),
                )
            if not self.binary:
                return result(
                    "lvs",
                    tool=tool,
                    execution=static_execution("not_available"),
                    engineering_status="unknown",
                    summary="Netgen is not available in the selected runtime.",
                    inputs=inputs,
                    diagnostics=[diagnostic("error", "tool.missing", "Netgen was not found.")],
                    data=base_data,
                )

            anchor, output_error = open_output_anchor(report_path, create_parent=True)
            if output_error is not None or anchor is None:
                code, message = output_error or ("output.invalid", "Cannot anchor output.")
                return _static_invalid(tool, inputs, base_data, code=code, message=message)
            base_data["report_output"].update(
                {"path": str(anchor.report_path), "native_json_path": str(anchor.json_path)}
            )
            base_data["json_output"]["path"] = str(anchor.json_path)
            output_paths = {anchor.report_path, anchor.json_path, anchor.transcript_path}
            if output_paths.intersection(input_paths):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    code="output.invalid",
                    message="LVS evidence paths must be distinct from every declared input.",
                )
            if not anchor_is_fresh(anchor):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    code="output.not_fresh",
                    message="A Netgen evidence path appeared or its parent changed before launch.",
                )

            command = [
                self.binary,
                "-batch",
                "lvs",
                f"{snapshots[0].path} {cell_name}",
                f"{snapshots[1].path} {cell_name}",
                str(snapshots[2].path),
                str(anchor.report_path),
                "-json",
            ]
            process = run_process(
                command,
                cwd=run_dir,
                timeout=timeout,
                capture_limit_bytes=MAX_CAPTURE_LIMIT_BYTES,
            )
            transcript_artifact, transcript_capture = write_transcript(anchor, process)
            report_artifact, report_capture, native_report = capture_report(
                anchor,
                expected_cell=cell_name,
            )
            json_artifact, json_capture, native_json = capture_json(
                anchor,
                expected_cell=cell_name,
            )
            changed_inputs = [
                str(snapshot.path)
                for snapshot in snapshots
                if not stable_input_unchanged(snapshot)
            ]
            inputs_stable = not changed_inputs
            assessment = transcript_assessment(process, setup_path=snapshots[2].path)
            report_outcome = native_report.get("outcome") if native_report else "unknown"
            json_outcome = native_json.get("outcome") if native_json else "unknown"
            outcomes_agree = (
                report_outcome == json_outcome and report_outcome in {"pass", "fail"}
            )
            report_device_counts = (
                native_report.get("device_counts") if native_report else None
            )
            report_node_counts = native_report.get("node_counts") if native_report else None
            json_device_sides = native_json.get("device_counts") if native_json else None
            json_node_counts = native_json.get("node_counts") if native_json else None
            try:
                json_device_totals = [
                    sum(entry[1] for entry in side) for side in json_device_sides
                ]
            except (IndexError, TypeError):
                json_device_totals = None
            structural_counts_agree = bool(
                isinstance(report_device_counts, list)
                and len(report_device_counts) == 2
                and report_device_counts == json_device_totals
                and isinstance(report_node_counts, list)
                and len(report_node_counts) == 2
                and report_node_counts == json_node_counts
            )
            evidence_agrees = outcomes_agree and structural_counts_agree
            trustworthy = bool(
                process.status == "completed"
                and process.exit_code == 0
                and inputs_stable
                and transcript_capture.get("status") == "valid"
                and assessment["clean"]
                and report_capture.get("status") == "valid"
                and json_capture.get("status") == "valid"
                and evidence_agrees
            )
            if trustworthy and report_outcome == "pass":
                engineering_status = "pass"
                lvs_match: bool | None = True
                summary = "Netgen produced clean, agreeing native evidence for a unique LVS match."
            elif trustworthy and report_outcome == "fail":
                engineering_status = "fail"
                lvs_match = False
                summary = "Netgen produced clean, agreeing native evidence for an LVS mismatch."
            else:
                engineering_status = "unknown"
                lvs_match = None
                summary = "The LVS run did not yield mutually trustworthy native evidence."

            diagnostics: list[dict[str, Any]] = [
                diagnostic(
                    "warning",
                    "netgen.provenance_incomplete",
                    "The executable setup Tcl may read transitive files or ambient environment state that OpenADA cannot infer.",
                )
            ]
            if assessment["stderr_reviewed_warning_count"]:
                diagnostics.append(
                    diagnostic(
                        "warning",
                        "netgen.stderr_reviewed_warning",
                        (
                            "Netgen emitted "
                            f"{assessment['stderr_reviewed_warning_count']} reviewed "
                            "'Unable to permute model ... pins ...' warning line(s)."
                        ),
                    )
                )
            if process.status != "completed":
                diagnostics.append(
                    diagnostic(
                        "error",
                        f"execution.{process.status}",
                        process.error or "Netgen did not complete.",
                    )
                )
            elif process.exit_code != 0:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "netgen.nonzero_exit",
                        f"Netgen exited with code {process.exit_code}.",
                    )
                )
            if changed_inputs:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "input.changed",
                        "Declared LVS input(s) changed during execution: " + ", ".join(changed_inputs),
                    )
                )
            if transcript_capture.get("status") != "valid":
                diagnostics.append(
                    diagnostic(
                        "error",
                        "transcript.invalid",
                        f"The bounded Netgen transcript status is {transcript_capture.get('status')}.",
                    )
                )
            elif not assessment["complete"]:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "transcript.incomplete",
                        "Netgen output exceeded the complete-stream bound or was not valid UTF-8.",
                    )
                )
            elif not assessment["clean"]:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "netgen.setup_or_transcript_error",
                        "Netgen did not provide a clean setup-read and completion transcript.",
                    )
                )
            for label, capture in (("report", report_capture), ("json", json_capture)):
                if capture.get("status") != "valid":
                    diagnostics.append(
                        diagnostic(
                            "error",
                            f"netgen.{label}_invalid",
                            f"The native Netgen {label} evidence status is {capture.get('status')}.",
                        )
                    )
            if (
                report_capture.get("status") == "valid"
                and json_capture.get("status") == "valid"
                and not evidence_agrees
            ):
                diagnostics.append(
                    diagnostic(
                        "error",
                        "netgen.evidence_conflict",
                        (
                            "The native Netgen report and JSON disagree on their outcome "
                            "or structural device/net counts "
                            f"(report={report_outcome!r}, JSON={json_outcome!r})."
                        ),
                    )
                )
            if engineering_status == "fail":
                diagnostics.append(
                    diagnostic("error", "netgen.mismatch", "Netgen reported an LVS mismatch.")
                )

            comparison = dict(native_json or {})
            comparison.update(
                {
                    "lvs_match": lvs_match,
                    "report_outcome": report_outcome,
                    "json_outcome": json_outcome,
                    "outcomes_agree": outcomes_agree,
                    "structural_counts_agree": structural_counts_agree,
                    "evidence_agrees": evidence_agrees,
                    "report": native_report,
                }
            )
            base_data.update(
                {
                    "inputs_stable": inputs_stable,
                    "changed_inputs": changed_inputs,
                    "lvs_match": lvs_match,
                    "comparison": comparison,
                    "report_output": {**base_data["report_output"], "capture": report_capture},
                    "json_output": {**base_data["json_output"], "capture": json_capture},
                    "transcript": {
                        **transcript_capture,
                        "assessment": assessment,
                        "stdout_tail": bounded_text(process.stdout[-4_000:]),
                        "stderr_tail": bounded_text(process.stderr[-4_000:]),
                        "limitation": (
                            "Pass or fail requires both native streams to fit the complete capture bound; "
                            "the artifact is not an unbounded native log."
                        ),
                    },
                }
            )
            artifacts = [
                artifact
                for artifact in (report_artifact, json_artifact, transcript_artifact)
                if artifact is not None
            ]
            return result(
                "lvs",
                tool=tool,
                execution=process,
                engineering_status=engineering_status,
                summary=summary,
                inputs=inputs,
                artifacts=artifacts,
                diagnostics=diagnostics,
                data=base_data,
            )
        finally:
            if anchor is not None:
                anchor.close()
            for snapshot in snapshots:
                snapshot.close()

    @staticmethod
    def parse_report(path: str | Path) -> dict[str, Any]:
        parsed = parse_netgen_report(path)
        parsed["lvs_match"] = (
            True if parsed.get("outcome") == "pass" else False if parsed.get("outcome") == "fail" else None
        )
        return parsed


NetgenEngine = NetgenDriver
