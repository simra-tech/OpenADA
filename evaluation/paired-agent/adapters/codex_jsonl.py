#!/usr/bin/env python3
"""Reduce Codex exec JSONL to a bounded, content-free evaluation trace."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, BinaryIO


SCHEMA_VERSION = "openada.eval.trace/v0alpha1"
SOURCE_KIND = "codex_exec_jsonl"
SUPPORTED_CODEX_CLI_VERSION = "0.144.3"
PARSER_VERSION = 1

MAX_LINE_BYTES = 1 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_EVENTS = 10_000
MAX_PUBLIC_ACTIONS = 256
MAX_JSON_INTEGER_CHARACTERS = 20
MAX_EXACT_SUMMARY_INTEGER = 2**52 - 1

ACTION_KINDS = (
    "agent_message",
    "reasoning",
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
    "error",
)
ITEM_EVENT_TYPES = {"item.started", "item.updated", "item.completed"}
TOP_LEVEL_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "error",
    *ITEM_EVENT_TYPES,
}
ITEM_STATUSES = {"in_progress", "completed", "failed", "declined"}
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class UnsafeTrace(ValueError):
    """A source stream cannot be inspected within the reducer's safety bounds."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UnsafeTrace("duplicate_json_key")
        result[key] = value
    return result


def _reject_nonfinite_number(_: str) -> None:
    raise UnsafeTrace("nonfinite_json_number")


def _parse_json_int(value: str) -> int:
    if len(value.removeprefix("-")) > MAX_JSON_INTEGER_CHARACTERS:
        raise UnsafeTrace("json_number_out_of_range")
    parsed = int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        raise UnsafeTrace("json_number_out_of_range")
    return parsed


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise UnsafeTrace("nonfinite_json_number")
    return parsed


def _json_depth(value: Any) -> int:
    maximum = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_JSON_DEPTH:
            return maximum
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return maximum


def _read_objects(stream: BinaryIO) -> Iterable[dict[str, Any]]:
    total_bytes = 0
    event_count = 0
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 3)
        if not raw:
            break
        total_bytes += len(raw)
        payload = raw[:-1] if raw.endswith(b"\n") else raw
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        if len(payload) > MAX_LINE_BYTES:
            raise UnsafeTrace("line_byte_limit_exceeded")
        if total_bytes > MAX_STREAM_BYTES:
            raise UnsafeTrace("stream_byte_limit_exceeded")
        event_count += 1
        if event_count > MAX_EVENTS:
            raise UnsafeTrace("event_limit_exceeded")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise UnsafeTrace("invalid_utf8") from exc
        if not text.strip():
            raise UnsafeTrace("empty_jsonl_line")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_number,
                parse_int=_parse_json_int,
                parse_float=_parse_json_float,
            )
        except UnsafeTrace:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise UnsafeTrace("invalid_json") from exc
        if not isinstance(value, dict):
            raise UnsafeTrace("event_not_object")
        if _json_depth(value) > MAX_JSON_DEPTH:
            raise UnsafeTrace("json_depth_limit_exceeded")
        yield value


def _bounded_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_EXACT_SUMMARY_INTEGER:
        return None
    return value


def _unwrap_event(
    value: dict[str, Any], previous_elapsed_ms: int | None
) -> tuple[dict[str, Any], int | None]:
    if "event" not in value:
        return value, previous_elapsed_ms
    event = value.get("event")
    elapsed_ms = _bounded_nonnegative_int(value.get("elapsed_ms"))
    if not isinstance(event, dict) or elapsed_ms is None:
        raise UnsafeTrace("invalid_adapter_envelope")
    if previous_elapsed_ms is not None and elapsed_ms < previous_elapsed_ms:
        raise UnsafeTrace("non_monotonic_elapsed_ms")
    return event, elapsed_ms


def _process_exit_bucket(exit_code: int | None) -> str:
    if exit_code is None:
        return "unknown"
    return "zero" if exit_code == 0 else "nonzero"


def _action_status(event_type: str, item: dict[str, Any]) -> str:
    source_status = item.get("status")
    if event_type == "item.started":
        return "in_progress"
    if event_type == "item.updated":
        return "in_progress"
    if source_status in ITEM_STATUSES:
        return source_status
    return "completed"


def _exit_bucket(kind: str, status: str, item: dict[str, Any]) -> str:
    if kind != "command_execution":
        return "not_applicable"
    if status == "declined":
        return "declined"
    exit_code = item.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return "missing"
    return "zero" if exit_code == 0 else "nonzero"


def _metric(eligible: bool, reasons: list[str]) -> dict[str, Any]:
    return {"eligible": eligible, "reasons": sorted(set(reasons))}


def _parse_usage(event: dict[str, Any]) -> dict[str, int] | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    parsed: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        number = _bounded_nonnegative_int(usage.get(field))
        if number is None:
            return None
        parsed[field] = number
    if parsed["cached_input_tokens"] > parsed["input_tokens"]:
        return None
    if parsed["reasoning_output_tokens"] > parsed["output_tokens"]:
        return None
    return parsed


def rejected_trace(
    *,
    codex_cli_version: str = SUPPORTED_CODEX_CLI_VERSION,
    process_exit_code: int | None = None,
    fresh_thread: bool = False,
    adapter_duration_ms: int | None = None,
) -> dict[str, Any]:
    """Represent an unsafe/unreadable source without publishing source content.

    A reducer failure is a trial outcome, not permission to drop the randomized
    assignment.  The CLI emits this fixed-shape document while returning a
    non-zero status so a trusted supervisor can retain an ``adapter_failed``
    trial.  Zero counts below are placeholders only: every affected metric is
    explicitly ineligible and therefore must be published with a null value.
    """

    bounded_exit = (
        process_exit_code
        if process_exit_code is None
        or (isinstance(process_exit_code, int) and not isinstance(process_exit_code, bool))
        else None
    )
    # Once source reduction fails, event-relative timing cannot be checked
    # against the launcher duration.  Preserve neither a potentially
    # contradictory value nor a false duration claim.
    bounded_duration = None
    process_bucket = _process_exit_bucket(bounded_exit)
    issues = ["adapter_input_rejected", "incomplete_terminal"]
    if process_bucket == "unknown":
        issues.append("process_exit_unknown")
    source_version: str | None = (
        SUPPORTED_CODEX_CLI_VERSION
        if codex_cli_version == SUPPORTED_CODEX_CLI_VERSION
        else None
    )
    if source_version is None:
        issues.append("source_identity_mismatch")
    usage_reasons = [
        "stream_incomplete",
        "terminal_not_completed",
        "usage_missing_or_invalid",
    ]
    if not fresh_thread:
        usage_reasons.append("fresh_single_turn_not_declared")
    if process_bucket == "unknown":
        usage_reasons.append("process_exit_unknown")
    else:
        usage_reasons.append("process_terminal_inconsistent")
    duration_reasons = ["adapter_duration_unavailable"]
    action_reasons = ["adapter_input_rejected", "incomplete_terminal"]
    if source_version is None:
        action_reasons.append("source_identity_mismatch")
    if not fresh_thread:
        action_reasons.append("fresh_single_turn_not_declared")
    return {
        "schema": SCHEMA_VERSION,
        "source": {
            "kind": SOURCE_KIND,
            "codex_cli_version": source_version,
            "parser_version": PARSER_VERSION,
        },
        "stream": {
            "complete": False,
            "event_count": 0,
            "action_count": 0,
            "action_record_count": 0,
            "terminal": "missing",
            "process_exit_bucket": process_bucket,
            "issues": sorted(issues),
        },
        "identity": {
            "native_thread_observed": False,
            "native_turn_observed": False,
            "api_request_observed": False,
            "execution_context_observed": False,
            "identifiers_synthesized": False,
            "fresh_single_turn_declared": bool(fresh_thread),
        },
        "actions": [],
        "aggregates": {
            "action_counts": {kind: 0 for kind in ACTION_KINDS},
            "command_result_observed_characters": None,
        },
        "usage": None,
        "usage_observed_valid": False,
        "adapter_duration_ms": bounded_duration,
        "eligibility": {
            "action_metrics": _metric(False, action_reasons),
            "usage_metrics": _metric(False, usage_reasons),
            "duration_metrics": _metric(not duration_reasons, duration_reasons),
            "session_metrics": _metric(
                False,
                ["native_turn_id_unavailable", "execution_context_unavailable"],
            ),
            "provider_request_metrics": _metric(
                False,
                ["api_request_id_unavailable", "provider_request_events_unavailable"],
            ),
            "engineering_outcome_metrics": _metric(
                False, ["independent_evidence_required"]
            ),
            "model_context_bytes_metrics": _metric(
                False, ["model_context_bytes_unavailable"]
            ),
        },
    }


def reduce_stream(
    stream: BinaryIO,
    *,
    codex_cli_version: str = SUPPORTED_CODEX_CLI_VERSION,
    process_exit_code: int | None = None,
    fresh_thread: bool = False,
    adapter_duration_ms: int | None = None,
) -> dict[str, Any]:
    """Return one content-free summary from raw or elapsed-time-enveloped JSONL."""

    if codex_cli_version != SUPPORTED_CODEX_CLI_VERSION:
        raise UnsafeTrace("unsupported_codex_cli_version")
    if process_exit_code is not None and (
        isinstance(process_exit_code, bool) or not isinstance(process_exit_code, int)
    ):
        raise UnsafeTrace("invalid_process_exit_code")
    if adapter_duration_ms is not None and _bounded_nonnegative_int(adapter_duration_ms) is None:
        raise UnsafeTrace("invalid_adapter_duration_ms")

    event_count = 0
    thread_started_count = 0
    turn_started_count = 0
    terminal_events: list[str] = []
    terminal_seen = False
    native_thread_observed = False
    previous_elapsed_ms: int | None = None
    active_items: dict[str, str] = {}
    completed_items: set[str] = set()
    # Keep reducer-generated events in a namespace that participant-controlled
    # item IDs cannot enter.  Otherwise an item ID such as
    # ``top-level-error-3`` could collide with a generated error identity and
    # undercount an otherwise eligible action metric.
    unique_actions: set[tuple[str, ...]] = set()
    action_counts = {kind: 0 for kind in ACTION_KINDS}
    actions: list[dict[str, Any]] = []
    action_record_count = 0
    command_chars = 0
    command_chars_known = True
    usage: dict[str, int] | None = None
    usage_observed_valid = False
    issues: set[str] = set()
    collab_observed = False
    phase = "before_thread"

    for wrapped in _read_objects(stream):
        event_count += 1
        event, elapsed_ms = _unwrap_event(wrapped, previous_elapsed_ms)
        if "event" in wrapped:
            previous_elapsed_ms = elapsed_ms
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in TOP_LEVEL_EVENT_TYPES:
            issues.add("unknown_event_variant")
            continue
        if terminal_seen:
            issues.add("event_after_terminal")

        if event_type == "thread.started":
            thread_started_count += 1
            native_thread_observed = isinstance(event.get("thread_id"), str) and bool(
                event.get("thread_id")
            )
            if not native_thread_observed:
                issues.add("unknown_event_variant")
            if event_count != 1 or thread_started_count != 1 or phase != "before_thread":
                issues.add("lifecycle_conflict")
            else:
                phase = "before_turn"
            continue
        if event_type == "turn.started":
            turn_started_count += 1
            if turn_started_count != 1 or phase != "before_turn":
                issues.add("lifecycle_conflict")
            else:
                phase = "in_turn"
            continue
        if event_type in {"turn.completed", "turn.failed"}:
            terminal_seen = True
            terminal_events.append("completed" if event_type == "turn.completed" else "failed")
            if len(terminal_events) != 1 or phase != "in_turn":
                issues.add("lifecycle_conflict")
            else:
                phase = "terminal"
            if event_type == "turn.completed":
                usage = _parse_usage(event)
                if usage is None:
                    issues.add("usage_missing_or_invalid")
                else:
                    usage_observed_valid = True
            continue

        if event_type == "error":
            if phase != "in_turn":
                issues.add("lifecycle_conflict")
            unique_actions.add(("top_level_error", str(event_count)))
            action_counts["error"] += 1
            action_record_count += 1
            if len(actions) < MAX_PUBLIC_ACTIONS:
                record: dict[str, Any] = {
                    "sequence": action_record_count,
                    "kind": "error",
                    "status": "completed",
                    "exit_bucket": "not_applicable",
                }
                if elapsed_ms is not None:
                    record["elapsed_ms"] = elapsed_ms
                actions.append(record)
            else:
                issues.add("public_action_cap_exceeded")
            continue

        item = event.get("item")
        if phase != "in_turn":
            issues.add("lifecycle_conflict")
        if not isinstance(item, dict):
            issues.add("unknown_event_variant")
            continue
        raw_id = item.get("id")
        kind = item.get("type")
        if not isinstance(raw_id, str) or not raw_id or kind not in ACTION_KINDS:
            issues.add("unknown_event_variant")
            continue
        status = _action_status(event_type, item)
        if "status" in item and item.get("status") not in ITEM_STATUSES:
            issues.add("unknown_event_variant")
        source_status = item.get("status")
        if event_type in {"item.started", "item.updated"}:
            if source_status is not None and source_status != "in_progress":
                issues.add("lifecycle_conflict")
        elif source_status is not None and source_status not in {
            "completed",
            "failed",
            "declined",
        }:
            issues.add("lifecycle_conflict")
        if kind == "collab_tool_call":
            collab_observed = True
            issues.add("collab_coverage_gap")

        identity = ("item", kind, raw_id)
        if identity not in unique_actions:
            unique_actions.add(identity)
            action_counts[kind] += 1

        if event_type == "item.started":
            if raw_id in active_items or raw_id in completed_items:
                issues.add("lifecycle_conflict")
            else:
                active_items[raw_id] = kind
        elif event_type == "item.updated":
            if active_items.get(raw_id) != kind:
                issues.add("lifecycle_conflict")
        else:
            active_kind = active_items.pop(raw_id, None)
            if active_kind is not None and active_kind != kind:
                issues.add("lifecycle_conflict")
            if raw_id in completed_items:
                issues.add("lifecycle_conflict")
            completed_items.add(raw_id)
            if kind == "command_execution":
                output = item.get("aggregated_output")
                if isinstance(output, str):
                    command_chars += len(output)
                else:
                    command_chars_known = False

        action_record_count += 1
        if len(actions) < MAX_PUBLIC_ACTIONS:
            record = {
                "sequence": action_record_count,
                "kind": kind,
                "status": status,
                "exit_bucket": _exit_bucket(kind, status, item),
            }
            if elapsed_ms is not None:
                record["elapsed_ms"] = elapsed_ms
            actions.append(record)
        else:
            issues.add("public_action_cap_exceeded")

    if thread_started_count != 1 or turn_started_count != 1:
        issues.add("incomplete_terminal")
    if active_items:
        issues.add("lifecycle_conflict")
    if len(terminal_events) != 1:
        issues.add("incomplete_terminal")
    if adapter_duration_ms is not None and previous_elapsed_ms is not None:
        if adapter_duration_ms < previous_elapsed_ms:
            raise UnsafeTrace("adapter_duration_before_event")

    if len(terminal_events) == 1:
        terminal = terminal_events[0]
    elif not terminal_events:
        terminal = "missing"
    else:
        terminal = "conflict"
    process_bucket = _process_exit_bucket(process_exit_code)
    process_consistent = (
        (terminal == "completed" and process_bucket == "zero")
        or (terminal == "failed" and process_bucket == "nonzero")
    )
    if process_bucket == "unknown":
        issues.add("process_exit_unknown")
    elif terminal in {"completed", "failed"} and not process_consistent:
        issues.add("process_terminal_inconsistent")

    stream_blockers = {
        "unknown_event_variant",
        "lifecycle_conflict",
        "event_after_terminal",
        "incomplete_terminal",
        "public_action_cap_exceeded",
    }
    stream_complete = not bool(issues & stream_blockers)

    action_reasons = sorted(issues & stream_blockers)
    if collab_observed:
        action_reasons.append("collab_coverage_gap")
    if not fresh_thread:
        action_reasons.append("fresh_single_turn_not_declared")
    action_eligible = not action_reasons

    usage_reasons: list[str] = []
    if not stream_complete:
        usage_reasons.append("stream_incomplete")
    if terminal != "completed":
        usage_reasons.append("terminal_not_completed")
    if not fresh_thread:
        usage_reasons.append("fresh_single_turn_not_declared")
    if usage is None:
        usage_reasons.append("usage_missing_or_invalid")
    if process_bucket == "unknown":
        usage_reasons.append("process_exit_unknown")
    elif not process_consistent:
        usage_reasons.append("process_terminal_inconsistent")
    usage_eligible = not usage_reasons
    if not usage_eligible:
        usage = None

    duration_reasons = [] if adapter_duration_ms is not None else ["adapter_duration_unavailable"]

    return {
        "schema": SCHEMA_VERSION,
        "source": {
            "kind": SOURCE_KIND,
            "codex_cli_version": SUPPORTED_CODEX_CLI_VERSION,
            "parser_version": PARSER_VERSION,
        },
        "stream": {
            "complete": stream_complete,
            "event_count": event_count,
            "action_count": len(unique_actions),
            "action_record_count": action_record_count,
            "terminal": terminal,
            "process_exit_bucket": process_bucket,
            "issues": sorted(issues),
        },
        "identity": {
            "native_thread_observed": native_thread_observed,
            "native_turn_observed": False,
            "api_request_observed": False,
            "execution_context_observed": False,
            "identifiers_synthesized": False,
            "fresh_single_turn_declared": bool(fresh_thread),
        },
        "actions": actions,
        "aggregates": {
            "action_counts": action_counts,
            "command_result_observed_characters": command_chars
            if command_chars_known
            else None,
        },
        "usage": usage,
        "usage_observed_valid": usage_observed_valid,
        "adapter_duration_ms": adapter_duration_ms,
        "eligibility": {
            "action_metrics": _metric(action_eligible, action_reasons),
            "usage_metrics": _metric(usage_eligible, usage_reasons),
            "duration_metrics": _metric(not duration_reasons, duration_reasons),
            "session_metrics": _metric(
                False,
                ["native_turn_id_unavailable", "execution_context_unavailable"],
            ),
            "provider_request_metrics": _metric(
                False,
                ["api_request_id_unavailable", "provider_request_events_unavailable"],
            ),
            "engineering_outcome_metrics": _metric(
                False, ["independent_evidence_required"]
            ),
            "model_context_bytes_metrics": _metric(
                False, ["model_context_bytes_unavailable"]
            ),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Codex JSONL path, or - for stdin")
    parser.add_argument(
        "--codex-cli-version",
        default=SUPPORTED_CODEX_CLI_VERSION,
        help="exact source Codex CLI version",
    )
    parser.add_argument("--process-exit-code", type=int)
    parser.add_argument("--fresh-thread", action="store_true")
    parser.add_argument("--adapter-duration-ms", type=int)
    return parser


def _emit(document: dict[str, Any]) -> None:
    json.dump(document, sys.stdout, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handle: BinaryIO
    close_handle = False
    if args.input == "-":
        handle = sys.stdin.buffer
    else:
        try:
            handle = Path(args.input).open("rb")
        except OSError:
            _emit(
                rejected_trace(
                    codex_cli_version=args.codex_cli_version,
                    process_exit_code=args.process_exit_code,
                    fresh_thread=args.fresh_thread,
                    adapter_duration_ms=args.adapter_duration_ms,
                )
            )
            print("codex_jsonl: unsafe input: input_open_failed", file=sys.stderr)
            return 2
        close_handle = True
    try:
        reduced = reduce_stream(
            handle,
            codex_cli_version=args.codex_cli_version,
            process_exit_code=args.process_exit_code,
            fresh_thread=args.fresh_thread,
            adapter_duration_ms=args.adapter_duration_ms,
        )
    except UnsafeTrace as exc:
        _emit(
            rejected_trace(
                codex_cli_version=args.codex_cli_version,
                process_exit_code=args.process_exit_code,
                fresh_thread=args.fresh_thread,
                adapter_duration_ms=args.adapter_duration_ms,
            )
        )
        print(f"codex_jsonl: unsafe input: {exc.code}", file=sys.stderr)
        return 2
    finally:
        if close_handle:
            handle.close()
    _emit(reduced)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
