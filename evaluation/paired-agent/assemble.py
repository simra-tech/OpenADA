#!/usr/bin/env python3
"""Bind one planned assignment to trusted capture inputs and native scoring."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any

from common import (
    EvaluationError,
    IHP_PAIRED_TASK_DIR,
    MAX_TRACE_BYTES,
    NATIVE_SCORE_SCHEMA,
    TRIAL_SCHEMA,
    emit_error,
    emit_json,
    file_record,
    find_assignment,
    load_json,
    load_trial_signing_seed,
    parse_timestamp,
    read_regular_bytes,
    seal_trial,
    validate_campaign,
    validate_plan,
    validate_schema,
)


TRACE_SCHEMA = "openada.eval.trace/v0alpha1"
TRACE_ELIGIBILITY_KEYS = {
    "action_metrics",
    "usage_metrics",
    "duration_metrics",
    "session_metrics",
    "provider_request_metrics",
    "engineering_outcome_metrics",
    "model_context_bytes_metrics",
}
TRACE_STREAM_BLOCKERS = {
    "unknown_event_variant",
    "lifecycle_conflict",
    "event_after_terminal",
    "incomplete_terminal",
    "public_action_cap_exceeded",
    "adapter_input_rejected",
    "source_identity_mismatch",
}
TRACE_ACTION_KINDS = {
    "agent_message",
    "reasoning",
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
    "error",
}
AUTHORITY_METRICS = {
    "session_count",
    "provider_request_count",
    "request_latency_ms",
    "ttft_ms",
    "api_retry_count",
    "model_context_bytes",
}
PROTOCOL_ATTESTATIONS = {
    "prompt_exact",
    "agent_configuration_exact",
    "runtime_identity_exact",
    "approval_policy_exact",
    "budgets_exact",
    "fresh_agent_context",
    "prior_session_absent",
    "memory_absent",
    "web_disabled",
    "subagents_disabled",
    "credentials_absent_from_executor",
    "participant_host_shell_absent",
    "participant_container_socket_absent",
    "workspace_fresh",
    "required_outputs_absent_before",
    "source_read_only",
    "design_read_only",
    "pdk_read_only",
    "startup_files_read_only",
    "source_unchanged_after",
    "task_network_none",
    "task_network_enforced",
    "extra_condition_difference_absent",
}
ENGINEERING_ATTESTATIONS = {
    "prompt_exact",
    "agent_configuration_exact",
    "runtime_identity_exact",
    "fresh_agent_context",
    "prior_session_absent",
    "memory_absent",
    "web_disabled",
    "subagents_disabled",
    "credentials_absent_from_executor",
    "participant_host_shell_absent",
    "participant_container_socket_absent",
    "workspace_fresh",
    "required_outputs_absent_before",
    "source_read_only",
    "design_read_only",
    "pdk_read_only",
    "startup_files_read_only",
    "source_unchanged_after",
    "task_network_none",
    "task_network_enforced",
    "extra_condition_difference_absent",
}
OPENADA_PRESENCE_FIELDS = {
    "openada_distribution_present",
    "openada_cli_present",
    "openada_skill_present",
    "openada_package_present",
    "openada_treatment_schema_present",
    "openada_repository_present",
    "openada_prior_output_present",
    "openada_system_context_present",
}
TREATMENT_REQUIRED_PRESENCE = {
    "openada_distribution_present",
    "openada_cli_present",
    "openada_skill_present",
    "openada_package_present",
    "openada_treatment_schema_present",
    "openada_system_context_present",
}
TREATMENT_FORBIDDEN_PRESENCE = {
    "openada_repository_present",
    "openada_prior_output_present",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Assemble one immutable planned trial from a trusted supervisor "
            "record, reduced trace, submission, and read-only scored workspace."
        )
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--supervisor", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--signing-key",
        type=Path,
        required=True,
        help="Owner-only file containing the campaign Ed25519 private seed as lowercase hex.",
    )
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--pretty", action="store_true")
    return parser


def _exact_keys(value: object, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvaluationError(f"{label} has missing or unexpected fields")
    return value


def _bounded_reasons(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise EvaluationError(f"{label} must be a bounded reason-code array")
    reasons: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128:
            raise EvaluationError(f"{label} contains an invalid reason code")
        reasons.append(item)
    if len(reasons) != len(set(reasons)):
        raise EvaluationError(f"{label} contains duplicate reason codes")
    if reasons != sorted(reasons):
        raise EvaluationError(f"{label} reason codes must be sorted")
    return reasons


def _trace_eligibility(value: object, *, label: str) -> dict[str, Any]:
    item = _exact_keys(value, {"eligible", "reasons"}, label=label)
    if not isinstance(item["eligible"], bool):
        raise EvaluationError(f"{label}.eligible must be boolean")
    reasons = _bounded_reasons(item["reasons"], label=f"{label}.reasons")
    if item["eligible"] and reasons:
        raise EvaluationError(f"{label} cannot be eligible with reason codes")
    if not item["eligible"] and not reasons:
        raise EvaluationError(f"{label} must explain ineligibility")
    return {"eligible": item["eligible"], "reasons": reasons}


def validate_trace(trace: dict[str, Any]) -> None:
    validate_schema(trace, "trace-v0alpha1.schema.json", label="reduced trace")
    _exact_keys(
        trace,
        {
            "schema",
            "source",
            "stream",
            "identity",
            "actions",
            "aggregates",
            "usage",
            "usage_observed_valid",
            "adapter_duration_ms",
            "eligibility",
        },
        label="trace",
    )
    if trace["schema"] != TRACE_SCHEMA:
        raise EvaluationError(f"trace.schema must be {TRACE_SCHEMA!r}")
    source = _exact_keys(
        trace["source"],
        {"kind", "codex_cli_version", "parser_version"},
        label="trace.source",
    )
    expected_source = {
        "kind": "codex_exec_jsonl",
        "codex_cli_version": "0.144.3",
        "parser_version": 1,
    }
    rejected_source = (
        source["kind"] == "codex_exec_jsonl"
        and source["codex_cli_version"] is None
        and source["parser_version"] == 1
        and "adapter_input_rejected" in trace["stream"]["issues"]
        and "source_identity_mismatch" in trace["stream"]["issues"]
    )
    if source != expected_source and not rejected_source:
        raise EvaluationError("trace source identity differs from the pinned reducer")
    stream = _exact_keys(
        trace["stream"],
        {
            "complete",
            "event_count",
            "action_count",
            "action_record_count",
            "terminal",
            "process_exit_bucket",
            "issues",
        },
        label="trace.stream",
    )
    if not isinstance(stream["complete"], bool):
        raise EvaluationError("trace.stream.complete must be boolean")
    for field in ("event_count", "action_count", "action_record_count"):
        if isinstance(stream[field], bool) or not isinstance(stream[field], int) or stream[field] < 0:
            raise EvaluationError(f"trace.stream.{field} must be a non-negative integer")
    if stream["terminal"] not in {"completed", "failed", "missing", "conflict"}:
        raise EvaluationError("trace.stream.terminal has an unsupported value")
    if stream["process_exit_bucket"] not in {"zero", "nonzero", "unknown"}:
        raise EvaluationError("trace.stream.process_exit_bucket has an unsupported value")
    issues = set(stream["issues"])
    if stream["terminal"] in {"missing", "conflict"} and "incomplete_terminal" not in issues:
        raise EvaluationError("trace missing/conflicting terminal lacks its blocker issue")
    expected_complete = not bool(issues & TRACE_STREAM_BLOCKERS)
    if stream["complete"] is not expected_complete:
        raise EvaluationError("trace stream completeness conflicts with its issues")
    expected_process_issues: set[str] = set()
    if stream["process_exit_bucket"] == "unknown":
        expected_process_issues.add("process_exit_unknown")
    elif (
        stream["terminal"] == "completed"
        and stream["process_exit_bucket"] != "zero"
    ) or (
        stream["terminal"] == "failed"
        and stream["process_exit_bucket"] != "nonzero"
    ):
        expected_process_issues.add("process_terminal_inconsistent")
    for issue in ("process_exit_unknown", "process_terminal_inconsistent"):
        if (issue in issues) != (issue in expected_process_issues):
            raise EvaluationError("trace process issue codes conflict with terminal state")
    rejected = "adapter_input_rejected" in issues
    if stream["complete"]:
        if (
            stream["terminal"] not in {"completed", "failed"}
            or not trace["identity"]["native_thread_observed"]
        ):
            raise EvaluationError("trace lacks the required native thread/turn lifecycle")
        if stream["event_count"] != stream["action_record_count"] + 3:
            raise EvaluationError("complete trace event count conflicts with lifecycle/actions")
    identity = _exact_keys(
        trace["identity"],
        {
            "native_thread_observed",
            "native_turn_observed",
            "api_request_observed",
            "execution_context_observed",
            "identifiers_synthesized",
            "fresh_single_turn_declared",
        },
        label="trace.identity",
    )
    if any(not isinstance(value, bool) for value in identity.values()):
        raise EvaluationError("trace identity observations must be boolean")
    if identity["identifiers_synthesized"]:
        raise EvaluationError("trace reducer may not synthesize unavailable identifiers")
    if rejected:
        expected_rejected_issues = {
            "adapter_input_rejected",
            "incomplete_terminal",
        }
        if stream["process_exit_bucket"] == "unknown":
            expected_rejected_issues.add("process_exit_unknown")
        if source["codex_cli_version"] is None:
            expected_rejected_issues.add("source_identity_mismatch")
        if issues != expected_rejected_issues:
            raise EvaluationError("rejected trace carries non-canonical issue codes")
        if (
            stream["event_count"] != 0
            or stream["action_count"] != 0
            or stream["action_record_count"] != 0
            or stream["terminal"] != "missing"
            or any(
                identity[key]
                for key in (
                    "native_thread_observed",
                    "native_turn_observed",
                    "api_request_observed",
                    "execution_context_observed",
                    "identifiers_synthesized",
                )
            )
        ):
            raise EvaluationError("rejected trace differs from the fixed placeholder shape")

    actions = trace["actions"]
    if not isinstance(actions, list) or len(actions) > 100_000:
        raise EvaluationError("trace.actions must be a bounded array")
    if stream["action_count"] > stream["action_record_count"]:
        raise EvaluationError("trace unique action count exceeds observed action records")
    if stream["action_record_count"] > stream["event_count"]:
        raise EvaluationError("trace action records exceed source events")
    expected_public_actions = min(stream["action_record_count"], 256)
    if len(actions) != expected_public_actions:
        raise EvaluationError("trace public action row count is not the exact bounded prefix")
    if ("public_action_cap_exceeded" in issues) != (
        stream["action_record_count"] > 256
    ):
        raise EvaluationError("trace public-action cap issue is inconsistent")
    previous_sequence = 0
    previous_elapsed = -1
    public_kinds: set[str] = set()
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise EvaluationError(f"trace.actions[{index}] must be an object")
        required = {"sequence", "kind", "status", "exit_bucket"}
        if set(action) not in (required, required | {"elapsed_ms"}):
            raise EvaluationError(f"trace.actions[{index}] has an invalid shape")
        if action["sequence"] != index:
            raise EvaluationError("trace action sequences must be the contiguous public prefix")
        previous_sequence = action["sequence"]
        if not isinstance(action["kind"], str) or not action["kind"]:
            raise EvaluationError("trace action kind must be non-empty")
        public_kinds.add(action["kind"])
        if not isinstance(action["status"], str) or not action["status"]:
            raise EvaluationError("trace action status must be non-empty")
        if action["exit_bucket"] not in {
            "zero",
            "nonzero",
            "missing",
            "declined",
            "not_applicable",
        }:
            raise EvaluationError("trace action exit bucket has an unsupported value")
        if action["kind"] != "command_execution" and action["exit_bucket"] != "not_applicable":
            raise EvaluationError("non-command trace action has a command exit bucket")
        if action["kind"] == "command_execution":
            if action["status"] == "declined" and action["exit_bucket"] != "declined":
                raise EvaluationError("declined command lacks the declined exit bucket")
            if action["status"] != "declined" and action["exit_bucket"] not in {
                "zero",
                "nonzero",
                "missing",
            }:
                raise EvaluationError("command trace action has an invalid exit bucket")
        if "elapsed_ms" in action and (
            isinstance(action["elapsed_ms"], bool)
            or not isinstance(action["elapsed_ms"], int)
            or action["elapsed_ms"] < 0
        ):
            raise EvaluationError("trace action elapsed_ms must be non-negative")
        if "elapsed_ms" in action:
            if action["elapsed_ms"] < previous_elapsed:
                raise EvaluationError("trace action elapsed time must be monotonic")
            previous_elapsed = action["elapsed_ms"]

    aggregates = _exact_keys(
        trace["aggregates"],
        {"action_counts", "command_result_observed_characters"},
        label="trace.aggregates",
    )
    counts = aggregates["action_counts"]
    if not isinstance(counts, dict) or set(counts) != TRACE_ACTION_KINDS:
        raise EvaluationError("trace aggregate action kinds differ from the fixed reducer")
    if any(
        not isinstance(key, str)
        or not key
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in counts.items()
    ):
        raise EvaluationError("trace aggregate action counts are invalid")
    if sum(counts.values()) != stream["action_count"]:
        raise EvaluationError("trace aggregate action counts do not sum to action_count")
    if any(counts[kind] == 0 for kind in public_kinds):
        raise EvaluationError("trace public action kind is absent from aggregate counts")
    if stream["action_record_count"] <= 256 and {
        kind for kind, count in counts.items() if count
    } != public_kinds:
        raise EvaluationError("trace complete public prefix conflicts with aggregate kinds")
    if ("collab_coverage_gap" in issues) != (counts["collab_tool_call"] > 0):
        raise EvaluationError("trace collaboration issue conflicts with observed actions")
    observed_chars = aggregates["command_result_observed_characters"]
    if observed_chars is not None and (
        isinstance(observed_chars, bool)
        or not isinstance(observed_chars, int)
        or observed_chars < 0
    ):
        raise EvaluationError("trace observed character count must be null or non-negative")
    if counts["command_execution"] == 0 and observed_chars != 0 and not rejected:
        raise EvaluationError("trace command characters exist without a command action")
    if rejected and (any(counts.values()) or observed_chars is not None):
        raise EvaluationError("rejected trace cannot publish action observations")

    usage = trace["usage"]
    usage_observed_valid = trace["usage_observed_valid"]
    if not isinstance(usage_observed_valid, bool):
        raise EvaluationError("trace usage_observed_valid must be boolean")
    if usage is not None:
        usage = _exact_keys(
            usage,
            {
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            },
            label="trace.usage",
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in usage.values()
        ):
            raise EvaluationError("trace usage values must be non-negative integers")
        if usage["cached_input_tokens"] > usage["input_tokens"]:
            raise EvaluationError("trace cached input tokens exceed total input tokens")
        if usage["reasoning_output_tokens"] > usage["output_tokens"]:
            raise EvaluationError("trace reasoning tokens exceed total output tokens")
    duration = trace["adapter_duration_ms"]
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration < 0
    ):
        raise EvaluationError("trace adapter duration must be null or non-negative")
    if duration is not None and previous_elapsed >= 0 and duration < previous_elapsed:
        raise EvaluationError("trace adapter duration precedes a public action envelope")
    if rejected and (usage is not None or duration is not None):
        raise EvaluationError("rejected trace cannot publish usage or duration observations")
    if rejected and usage_observed_valid:
        raise EvaluationError("rejected trace cannot claim a valid usage snapshot")
    eligibility = trace["eligibility"]
    if not isinstance(eligibility, dict) or set(eligibility) != TRACE_ELIGIBILITY_KEYS:
        raise EvaluationError("trace eligibility keys differ from the fixed reducer contract")
    parsed_eligibility = {
        key: _trace_eligibility(
            eligibility[key], label=f"trace.eligibility.{key}"
        )
        for key in sorted(TRACE_ELIGIBILITY_KEYS)
    }
    action_reasons = sorted(issues & TRACE_STREAM_BLOCKERS)
    if counts["collab_tool_call"]:
        action_reasons.append("collab_coverage_gap")
    if not identity["fresh_single_turn_declared"]:
        action_reasons.append("fresh_single_turn_not_declared")
    if parsed_eligibility["action_metrics"] != _trace_eligibility(
        {"eligible": not action_reasons, "reasons": sorted(set(action_reasons))},
        label="expected trace action eligibility",
    ):
        raise EvaluationError("trace action eligibility conflicts with reduced evidence")

    usage_reasons: list[str] = []
    if not stream["complete"]:
        usage_reasons.append("stream_incomplete")
    if stream["terminal"] != "completed":
        usage_reasons.append("terminal_not_completed")
    if not identity["fresh_single_turn_declared"]:
        usage_reasons.append("fresh_single_turn_not_declared")
    if not usage_observed_valid:
        usage_reasons.append("usage_missing_or_invalid")
    if stream["process_exit_bucket"] == "unknown":
        usage_reasons.append("process_exit_unknown")
    elif not (
        (stream["terminal"] == "completed" and stream["process_exit_bucket"] == "zero")
        or (stream["terminal"] == "failed" and stream["process_exit_bucket"] == "nonzero")
    ):
        usage_reasons.append("process_terminal_inconsistent")
    expected_usage = {
        "eligible": not usage_reasons,
        "reasons": sorted(set(usage_reasons)),
    }
    if parsed_eligibility["usage_metrics"] != expected_usage:
        raise EvaluationError("trace usage eligibility conflicts with reduced evidence")
    if not expected_usage["eligible"] and usage is not None:
        raise EvaluationError("trace must erase usage when usage metrics are ineligible")
    if expected_usage["eligible"] and usage is None:
        raise EvaluationError("eligible trace usage metric lacks the observed usage")
    if usage is not None and not usage_observed_valid:
        raise EvaluationError("trace usage exists without a valid observed snapshot")
    expected_usage_issue = (
        stream["terminal"] == "completed" and not usage_observed_valid
    )
    if ("usage_missing_or_invalid" in issues) != expected_usage_issue:
        raise EvaluationError("trace usage issue conflicts with the observed snapshot")

    duration_reasons = [] if duration is not None else ["adapter_duration_unavailable"]
    if parsed_eligibility["duration_metrics"] != {
        "eligible": not duration_reasons,
        "reasons": duration_reasons,
    }:
        raise EvaluationError("trace duration eligibility conflicts with reduced evidence")
    fixed_ineligible = {
        "session_metrics": [
            "execution_context_unavailable",
            "native_turn_id_unavailable",
        ],
        "provider_request_metrics": [
            "api_request_id_unavailable",
            "provider_request_events_unavailable",
        ],
        "engineering_outcome_metrics": ["independent_evidence_required"],
        "model_context_bytes_metrics": ["model_context_bytes_unavailable"],
    }
    for key, reasons in fixed_ineligible.items():
        if parsed_eligibility[key] != {"eligible": False, "reasons": reasons}:
            raise EvaluationError(f"trace {key} authority boundary is inconsistent")


def _resolve_declared(path_value: str, *, base: Path) -> Path:
    path = Path(path_value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _verify_declared_file(
    declared: dict[str, Any], actual: Path, *, base: Path, maximum_bytes: int
) -> None:
    declared_path = _resolve_declared(declared["path"], base=base)
    if declared_path != actual.resolve():
        raise EvaluationError("supervisor file path differs from the supplied input")
    actual_record = file_record(actual, maximum_bytes=maximum_bytes)
    if declared["bytes"] != actual_record["bytes"] or declared["sha256"] != actual_record["sha256"]:
        raise EvaluationError("supervisor file identity differs from the supplied input")


def _validate_workspace(path: Path, protected_files: list[Path]) -> Path:
    expanded = path.expanduser().absolute()
    try:
        metadata = expanded.lstat()
    except OSError as exc:
        raise EvaluationError("cannot stat scored workspace") from exc
    if not stat.S_ISDIR(metadata.st_mode) or expanded.is_symlink():
        raise EvaluationError("scored workspace must be a real directory, not a symlink")
    resolved = expanded.resolve()
    for protected in protected_files:
        candidate = protected.resolve()
        if candidate == resolved or resolved in candidate.parents:
            raise EvaluationError("trusted capture inputs must remain outside the participant workspace")
    return resolved


def _load_scorer(path: Path) -> ModuleType:
    # Execute only the packaged canonical scorer, and execute the exact bytes
    # already read through the bounded no-follow/single-link helper.  Campaign
    # bundles are data and never an executable-code authority.
    if path.resolve() != (IHP_PAIRED_TASK_DIR / "native_score.py").resolve():
        raise EvaluationError("refusing to execute a non-canonical native scorer")
    payload = read_regular_bytes(path, maximum_bytes=1024 * 1024)
    try:
        source = payload.decode("utf-8")
        code = compile(source, "<openada-canonical-native-score>", "exec")
    except (UnicodeError, SyntaxError) as exc:
        raise EvaluationError("cannot compile the canonical native scorer") from exc
    module = ModuleType("openada_eval_native_score")
    module.__file__ = str(path)
    try:
        exec(code, module.__dict__)
    except Exception as exc:
        raise EvaluationError("cannot initialize the canonical native scorer") from exc
    if not callable(getattr(module, "score_workspace", None)):
        raise EvaluationError("frozen native scorer lacks score_workspace")
    return module


def _pair_execution_observation(
    campaign: dict[str, Any],
    plan: dict[str, Any],
    assignment: dict[str, Any],
    supervisor: dict[str, Any],
    *,
    supervisor_created: Any,
) -> dict[str, Any]:
    planned = sorted(
        (
            item
            for item in plan["assignments"]
            if item["pair_id"] == assignment["pair_id"]
        ),
        key=lambda item: item["pair_position"],
    )
    if len(planned) != 2:
        raise EvaluationError("planned pair does not contain exactly two assignments")
    pair = supervisor["pair_execution"]
    dispatch = supervisor["dispatch"]
    if pair["clock_domain_id"] != dispatch["clock_domain_id"]:
        raise EvaluationError("pair and dispatch clock domains differ")
    if dispatch["clock_domain_id"] != campaign["execution_clock"]["domain_id"]:
        raise EvaluationError("supervisor clock domain differs from the campaign")
    records = [pair["first"], pair["second"]]
    if {record["trial_id"] for record in records} != {
        item["trial_id"] for item in planned
    }:
        raise EvaluationError("pair execution does not bind both planned trial IDs")
    if planned[0]["sequence"] == 1 and records[0]["monotonic_started_ms"] != 0:
        raise EvaluationError("campaign-relative monotonic clock must start at zero")
    public_records: list[dict[str, Any]] = []
    parsed: dict[str, tuple[Any, Any]] = {}
    for label, record in zip(("first", "second"), records):
        started = parse_timestamp(
            record["started_at"], label=f"pair_execution.{label}.started_at"
        )
        finished = parse_timestamp(
            record["finished_at"], label=f"pair_execution.{label}.finished_at"
        )
        if finished < started:
            raise EvaluationError("pair execution finish precedes its start")
        monotonic_duration = (
            record["monotonic_finished_ms"] - record["monotonic_started_ms"]
        )
        if monotonic_duration < 0:
            raise EvaluationError("pair monotonic finish precedes its start")
        utc_duration = int((finished - started) / timedelta(milliseconds=1))
        if abs(monotonic_duration - utc_duration) > 2_000:
            raise EvaluationError("pair monotonic and UTC durations conflict")
        parsed[record["trial_id"]] = (started, finished)
        public_records.append(
            {
                "trial_id": record["trial_id"],
                "monotonic_started_ms": record["monotonic_started_ms"],
                "monotonic_finished_ms": record["monotonic_finished_ms"],
            }
        )
    current = next(
        record for record in records if record["trial_id"] == assignment["trial_id"]
    )
    if current["started_at"] != supervisor["timing"]["started_at"] or current[
        "finished_at"
    ] != supervisor["timing"]["finished_at"]:
        raise EvaluationError("pair execution timing differs from this supervisor record")
    if (
        current["monotonic_started_ms"] != dispatch["monotonic_started_ms"]
        or current["monotonic_finished_ms"] != dispatch["monotonic_finished_ms"]
    ):
        raise EvaluationError("pair execution differs from the observed dispatch interval")
    latest_finish = max(finish for _, finish in parsed.values())
    if supervisor_created < latest_finish:
        raise EvaluationError("supervisor record predates completion of its claimed pair")
    return {
        "dispatch_sequence_observed": dispatch["sequence_observed"],
        "clock_domain_id": dispatch["clock_domain_id"],
        "monotonic_started_ms": dispatch["monotonic_started_ms"],
        "monotonic_finished_ms": dispatch["monotonic_finished_ms"],
        "pair": {
            "first": public_records[0],
            "second": public_records[1],
        },
    }


def _policy_observation(supervisor: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": dict(supervisor["attempt"]),
        "attestations": dict(supervisor["attestations"]),
        "condition_observation": dict(supervisor["condition_observation"]),
        "authority": dict(supervisor["authority"]),
    }


def _public_trace_observation(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": trace["schema"],
        "source": dict(trace["source"]),
        "stream": dict(trace["stream"]),
        "identity": dict(trace["identity"]),
        "actions": [dict(action) for action in trace["actions"]],
        "aggregates": {
            "action_counts": dict(trace["aggregates"]["action_counts"]),
            "command_result_observed_characters": trace["aggregates"][
                "command_result_observed_characters"
            ],
        },
        "usage": dict(trace["usage"]) if trace["usage"] is not None else None,
        "usage_observed_valid": trace["usage_observed_valid"],
        "adapter_duration_ms": trace["adapter_duration_ms"],
        "eligibility": {
            key: {
                "eligible": value["eligible"],
                "reasons": list(value["reasons"]),
            }
            for key, value in trace["eligibility"].items()
        },
    }


def derive_protocol(
    campaign: dict[str, Any],
    plan: dict[str, Any],
    assignment: dict[str, Any],
    termination: str,
    timing: dict[str, Any],
    policy: dict[str, Any],
    execution: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    attestations = policy["attestations"]
    protocol = [
        f"attestation.{key}"
        for key in sorted(PROTOCOL_ATTESTATIONS)
        if not attestations[key]
    ]
    engineering = [
        f"attestation.{key}"
        for key in sorted(ENGINEERING_ATTESTATIONS)
        if not attestations[key]
    ]
    if campaign["policies"]["user_intervention"] == "none" and attestations[
        "user_intervention_count"
    ]:
        protocol.append("user_intervention")
        engineering.append("user_intervention")
    attempt = policy["attempt"]
    if (
        attempt["attempt_count"] != 1
        or not attempt["assignment_launched_once"]
        or not attempt["prior_attempt_absent"]
    ):
        protocol.append("selective_rerun_or_prior_attempt")
        engineering.append("selective_rerun_or_prior_attempt")
    observation = policy["condition_observation"]
    if assignment["condition"] == "raw":
        if (
            any(observation[key] for key in OPENADA_PRESENCE_FIELDS)
            or observation["openada_identity_exact"]
            or observation["openada_used"]
            or observation["treatment_bundle_manifest_sha256"] is not None
        ):
            protocol.append("raw_openada_contamination")
            engineering.append("raw_openada_contamination")
    else:
        if (
            not all(observation[key] for key in TREATMENT_REQUIRED_PRESENCE)
            or any(observation[key] for key in TREATMENT_FORBIDDEN_PRESENCE)
            or not observation["openada_identity_exact"]
            or observation["treatment_bundle_manifest_sha256"]
            != campaign["treatment"]["bundle_manifest"]["sha256"]
        ):
            protocol.append("treatment_identity_not_exact")
            engineering.append("treatment_identity_not_exact")
        # openada_used is intentionally not an eligibility requirement: non-use
        # remains an intention-to-treat outcome.
    if not trace["identity"]["fresh_single_turn_declared"]:
        protocol.append("fresh_single_turn_not_declared")
        engineering.append("fresh_single_turn_not_declared")
    if trace["aggregates"]["action_counts"]["web_search"]:
        protocol.append("web_search_observed")
        engineering.append("web_search_observed")
    if trace["aggregates"]["action_counts"]["collab_tool_call"]:
        protocol.append("subagent_activity_observed")
        engineering.append("subagent_activity_observed")
    if trace["source"]["codex_cli_version"] != "0.144.3":
        protocol.append("trace_source_identity_mismatch")
        engineering.append("trace_source_identity_mismatch")

    pair = execution["pair"]
    if execution["dispatch_sequence_observed"] != assignment["sequence"]:
        protocol.append("dispatch_sequence_mismatch")
    planned_pair = sorted(
        (
            item
            for item in plan["assignments"]
            if item["pair_id"] == assignment["pair_id"]
        ),
        key=lambda item: item["pair_position"],
    )
    if planned_pair and pair["first"]["trial_id"] != planned_pair[0]["trial_id"]:
        protocol.append("pair_execution_order_mismatch")
    if pair["first"]["monotonic_finished_ms"] > pair["second"]["monotonic_started_ms"]:
        protocol.append("pair_execution_not_sequential")
    pair_span = (
        pair["second"]["monotonic_finished_ms"]
        - pair["first"]["monotonic_started_ms"]
    )
    if pair_span < 0:
        protocol.append("pair_execution_not_sequential")
    elif pair_span > campaign["policies"]["pair_max_span_seconds"] * 1000:
        protocol.append("pair_span_budget_exceeded")

    if timing["wall_time_ms"] > campaign["budgets"]["wall_time_seconds"] * 1000:
        protocol.append("wall_time_budget_exceeded")
    if trace["stream"]["action_count"] > campaign["budgets"]["max_agent_actions"]:
        protocol.append("agent_action_budget_exceeded")
    usage = trace["usage"]
    if isinstance(usage, dict):
        if usage["input_tokens"] > campaign["budgets"]["max_input_tokens"]:
            protocol.append("input_token_budget_exceeded")
        if usage["output_tokens"] > campaign["budgets"]["max_output_tokens"]:
            protocol.append("output_token_budget_exceeded")
    duration = trace["adapter_duration_ms"]
    if duration is not None and abs(duration - timing["wall_time_ms"]) > 2_000:
        protocol.append("adapter_duration_conflict")
    rejected = "adapter_input_rejected" in trace["stream"]["issues"]
    completed_zero = (
        trace["stream"]["terminal"] == "completed"
        and trace["stream"]["process_exit_bucket"] == "zero"
        and not rejected
    )
    if (termination == "completed") != completed_zero:
        protocol.append("termination_trace_mismatch")
    if (termination == "adapter_failed") != rejected:
        protocol.append("termination_trace_mismatch")
    if policy["authority"]["identifiers_synthesized"]:
        protocol.append("authority_identifiers_synthesized")
    protocol = sorted(set(protocol))
    engineering = sorted(set(engineering))
    return {
        "eligible": not protocol,
        "reason_codes": protocol,
        "engineering_eligible": not engineering,
        "engineering_reason_codes": engineering,
    }


def _metric(
    value: bool | int | float | None,
    *,
    eligible: bool,
    source: str,
    reasons: list[str],
) -> dict[str, Any]:
    if eligible and value is None:
        raise EvaluationError("eligible metric cannot have a null value")
    return {
        "eligible": eligible,
        "value": value if eligible else None,
        "source": source,
        "reason_codes": [] if eligible else sorted(set(reasons or ["metric_unavailable"])),
    }


def _metrics(
    campaign: dict[str, Any],
    timing: dict[str, Any],
    trace: dict[str, Any],
    score: dict[str, Any],
    *,
    protocol_eligible: bool,
    engineering_eligible: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    trace_eligibility = trace["eligibility"]

    def trace_reasons(
        eligibility: dict[str, Any], *, unavailable: str | None = None
    ) -> list[str]:
        reasons = list(eligibility["reasons"])
        if not protocol_eligible:
            reasons.append("protocol_ineligible")
        if unavailable is not None:
            reasons.append(unavailable)
        return sorted(set(reasons))

    for specification in campaign["metrics"]:
        name = specification["id"]
        if name == "verified_artifact_complete":
            result[name] = _metric(
                score["verified_artifact_complete"],
                eligible=engineering_eligible,
                source="native_score",
                reasons=["engineering_protocol_ineligible"],
            )
        elif name == "reported_status_correct":
            value = score["reported_status_correct"]
            result[name] = _metric(
                value,
                eligible=engineering_eligible and value is not None,
                source="native_score",
                reasons=[
                    "engineering_protocol_ineligible"
                    if not engineering_eligible
                    else "status_not_reported"
                ],
            )
        elif name == "wall_time_ms":
            result[name] = _metric(
                timing["wall_time_ms"],
                eligible=protocol_eligible,
                source="supervisor",
                reasons=["protocol_ineligible"],
            )
        elif name == "agent_action_count":
            eligibility = trace_eligibility["action_metrics"]
            result[name] = _metric(
                trace["stream"]["action_count"],
                eligible=protocol_eligible and eligibility["eligible"],
                source="trace",
                reasons=trace_reasons(eligibility),
            )
        elif name == "command_result_observed_characters":
            eligibility = trace_eligibility["action_metrics"]
            value = trace["aggregates"]["command_result_observed_characters"]
            result[name] = _metric(
                value,
                eligible=protocol_eligible and eligibility["eligible"] and value is not None,
                source="trace",
                reasons=trace_reasons(
                    eligibility,
                    unavailable="metric_unavailable" if value is None else None,
                ),
            )
        elif name in {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        }:
            eligibility = trace_eligibility["usage_metrics"]
            usage = trace["usage"]
            value = usage.get(name) if isinstance(usage, dict) else None
            result[name] = _metric(
                value,
                eligible=protocol_eligible and eligibility["eligible"] and value is not None,
                source="trace",
                reasons=trace_reasons(
                    eligibility,
                    unavailable="usage_unavailable" if value is None else None,
                ),
            )
        elif name == "adapter_duration_ms":
            eligibility = trace_eligibility["duration_metrics"]
            value = trace["adapter_duration_ms"]
            result[name] = _metric(
                value,
                eligible=protocol_eligible and eligibility["eligible"] and value is not None,
                source="trace",
                reasons=trace_reasons(
                    eligibility,
                    unavailable="duration_unavailable" if value is None else None,
                ),
            )
        elif name == "native_execution_verified":
            result[name] = _metric(
                None,
                eligible=False,
                source="unavailable",
                reasons=["trusted_executor_audit_unavailable"],
            )
        elif name in AUTHORITY_METRICS:
            result[name] = _metric(
                None,
                eligible=False,
                source="unavailable",
                reasons=["authoritative_provider_telemetry_unavailable"],
            )
        else:
            result[name] = _metric(
                None,
                eligible=False,
                source="unavailable",
                reasons=["unsupported_metric"],
            )
    return result


def assemble_trial(
    *,
    campaign_path: Path,
    plan_path: Path,
    supervisor_path: Path,
    trace_path: Path,
    submission_path: Path,
    workspace_path: Path,
    signing_key_path: Path,
    trial_id: str,
) -> dict[str, Any]:
    campaign_path = campaign_path.expanduser().absolute()
    plan_path = plan_path.expanduser().absolute()
    supervisor_path = supervisor_path.expanduser().absolute()
    trace_path = trace_path.expanduser().absolute()
    submission_path = submission_path.expanduser().absolute()
    signing_key_path = signing_key_path.expanduser().absolute()
    private_seed = load_trial_signing_seed(signing_key_path)
    campaign, campaign_sha256, _ = load_json(campaign_path)
    validate_campaign(campaign, campaign_path=campaign_path)
    plan, plan_sha256, _ = load_json(plan_path)
    validate_plan(plan, campaign, campaign_sha256)
    assignment = find_assignment(plan, trial_id)
    supervisor, _, _ = load_json(supervisor_path)
    validate_schema(supervisor, "supervisor-v0alpha1.schema.json", label="supervisor")
    trace, _, _ = load_json(trace_path, maximum_bytes=MAX_TRACE_BYTES)
    validate_trace(trace)
    # Final capture validity is an outcome. Require only a bounded stable file;
    # the condition-blind scorer classifies empty, malformed, duplicate-key, or
    # schema-invalid content without deleting the assignment.
    read_regular_bytes(submission_path, maximum_bytes=1024 * 1024)
    workspace = _validate_workspace(
        workspace_path,
        [
            campaign_path,
            plan_path,
            supervisor_path,
            trace_path,
            submission_path,
            signing_key_path,
        ],
    )

    if supervisor["campaign_id"] != campaign["campaign_id"]:
        raise EvaluationError("supervisor campaign ID differs from the campaign")
    if supervisor["campaign_sha256"] != campaign_sha256:
        raise EvaluationError("supervisor campaign hash differs from the campaign bytes")
    if supervisor["plan_sha256"] != plan_sha256:
        raise EvaluationError("supervisor plan hash differs from the plan bytes")
    for key in ("pair_id", "trial_id", "condition"):
        if supervisor[key] != assignment[key]:
            raise EvaluationError(f"supervisor {key} differs from the planned assignment")
    supervisor_base = supervisor_path.resolve().parent
    _verify_declared_file(
        supervisor["files"]["trace"],
        trace_path,
        base=supervisor_base,
        maximum_bytes=MAX_TRACE_BYTES,
    )
    _verify_declared_file(
        supervisor["files"]["submission"],
        submission_path,
        base=supervisor_base,
        maximum_bytes=1024 * 1024,
    )
    if _resolve_declared(supervisor["workspace"]["root"], base=supervisor_base) != workspace:
        raise EvaluationError("supervisor workspace root differs from the scored workspace")

    start = parse_timestamp(supervisor["timing"]["started_at"], label="supervisor.started_at")
    finish = parse_timestamp(supervisor["timing"]["finished_at"], label="supervisor.finished_at")
    created = parse_timestamp(supervisor["created_at"], label="supervisor.created_at")
    if finish < start:
        raise EvaluationError("supervisor finish time precedes start time")
    if start < parse_timestamp(plan["created_at"], label="plan.created_at"):
        raise EvaluationError("supervisor start time precedes the frozen plan")
    if created < finish:
        raise EvaluationError("supervisor record time precedes trial completion")
    observed_ms = int((finish - start) / timedelta(milliseconds=1))
    if abs(observed_ms - supervisor["timing"]["wall_time_ms"]) > 2_000:
        raise EvaluationError("supervisor wall time conflicts with its wall-clock interval")
    execution_observation = _pair_execution_observation(
        campaign,
        plan,
        assignment,
        supervisor,
        supervisor_created=created,
    )
    monotonic_duration = (
        execution_observation["monotonic_finished_ms"]
        - execution_observation["monotonic_started_ms"]
    )
    if monotonic_duration < 0 or abs(
        monotonic_duration - supervisor["timing"]["wall_time_ms"]
    ) > 2_000:
        raise EvaluationError("dispatch monotonic duration conflicts with wall time")
    policy_observation = _policy_observation(supervisor)
    trace_observation = _public_trace_observation(trace)
    scorer_path = IHP_PAIRED_TASK_DIR / "native_score.py"
    manifest_path = IHP_PAIRED_TASK_DIR / "manifest.json"
    scorer = _load_scorer(scorer_path)
    try:
        score = scorer.score_workspace(workspace, submission_path, manifest_path)
    except Exception as exc:  # the scorer's named setup error is dynamically loaded
        raise EvaluationError("condition-blind native scorer failed safely") from exc
    if not isinstance(score, dict) or score.get("schema") != NATIVE_SCORE_SCHEMA:
        raise EvaluationError("condition-blind scorer did not return the fixed score document")
    if score.get("task_id") != campaign["task"]["id"]:
        raise EvaluationError("native score task ID differs from the campaign task")

    timing = {"wall_time_ms": supervisor["timing"]["wall_time_ms"]}
    protocol = derive_protocol(
        campaign,
        plan,
        assignment,
        supervisor["termination"],
        timing,
        policy_observation,
        execution_observation,
        trace_observation,
    )
    metrics = _metrics(
        campaign,
        timing,
        trace_observation,
        score,
        protocol_eligible=protocol["eligible"],
        engineering_eligible=protocol["engineering_eligible"],
    )
    trial = {
        "schema": TRIAL_SCHEMA,
        "campaign_id": campaign["campaign_id"],
        "campaign_sha256": campaign_sha256,
        "plan_sha256": plan_sha256,
        "assignment": assignment,
        "termination": supervisor["termination"],
        "timing": timing,
        "inputs": {
            "supervisor_bound": True,
            "trace_bound": True,
            "submission_bound": True,
            "workspace_attested": True,
        },
        "policy_observation": policy_observation,
        "execution_observation": execution_observation,
        "trace_observation": trace_observation,
        "protocol": protocol,
        "score": score,
        "metrics": metrics,
    }
    seal_trial(trial, campaign, private_seed=private_seed)
    validate_schema(trial, "trial-v0alpha1.schema.json", label="assembled trial")
    return trial


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        trial = assemble_trial(
            campaign_path=args.campaign,
            plan_path=args.plan,
            supervisor_path=args.supervisor,
            trace_path=args.trace,
            submission_path=args.submission,
            workspace_path=args.workspace,
            signing_key_path=args.signing_key,
            trial_id=args.trial_id,
        )
        emit_json(trial, pretty=args.pretty)
        return 0
    except (EvaluationError, OSError, ValueError) as exc:
        emit_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
