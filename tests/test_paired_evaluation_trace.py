from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "evaluation" / "paired-agent"
if str(EVALUATION) not in sys.path:
    sys.path.insert(0, str(EVALUATION))
ADAPTER_PATH = ROOT / "evaluation" / "paired-agent" / "adapters" / "codex_jsonl.py"
SCHEMA_PATH = (
    ROOT
    / "evaluation"
    / "paired-agent"
    / "schemas"
    / "trace-v0alpha1.schema.json"
)

SPEC = importlib.util.spec_from_file_location("openada_codex_jsonl", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
codex_jsonl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_jsonl)

from assemble import validate_trace  # noqa: E402
from common import EvaluationError  # noqa: E402

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def encode_events(*events: dict[str, object]) -> io.BytesIO:
    return io.BytesIO(
        b"".join(
            json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
            for event in events
        )
    )


def base_events(*middle: dict[str, object]) -> tuple[dict[str, object], ...]:
    return (
        {"type": "thread.started", "thread_id": "private-thread-id"},
        {"type": "turn.started"},
        *middle,
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
            },
        },
    )


def reduce(*events: dict[str, object], **kwargs: object) -> dict[str, object]:
    options = {
        "process_exit_code": 0,
        "fresh_thread": True,
        "adapter_duration_ms": 25,
        **kwargs,
    }
    return codex_jsonl.reduce_stream(encode_events(*events), **options)


def assert_valid(result: dict[str, object]) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(result)


def test_supported_source_and_bounds_are_frozen() -> None:
    assert codex_jsonl.SUPPORTED_CODEX_CLI_VERSION == "0.144.3"
    assert codex_jsonl.PARSER_VERSION == 1
    assert codex_jsonl.MAX_LINE_BYTES == 1024 * 1024
    assert codex_jsonl.MAX_STREAM_BYTES == 16 * 1024 * 1024
    assert codex_jsonl.MAX_JSON_DEPTH == 16
    assert codex_jsonl.MAX_EVENTS == 10_000
    assert codex_jsonl.MAX_PUBLIC_ACTIONS == 256
    assert codex_jsonl.MAX_EXACT_SUMMARY_INTEGER == 2**52 - 1


def test_valid_trace_is_schema_valid_and_content_free() -> None:
    command = {
        "id": "private-item-id",
        "type": "command_execution",
        "command": "private executable --private-path",
        "aggregated_output": "private command result",
        "exit_code": 0,
        "status": "completed",
    }
    mcp = {
        "id": "private-mcp-id",
        "type": "mcp_tool_call",
        "server": "private-server",
        "tool": "private-tool",
        "arguments": {"path": "private-path"},
        "result": {"content": [{"text": "private-result"}]},
        "error": None,
        "status": "completed",
    }
    result = reduce(
        *base_events(
            {"type": "item.completed", "item": command},
            {"type": "item.completed", "item": mcp},
            {
                "type": "item.completed",
                "item": {
                    "id": "private-message-id",
                    "type": "agent_message",
                    "text": "private assistant message",
                },
            },
        )
    )

    assert_valid(result)
    serialized = json.dumps(result, sort_keys=True)
    for secret in (
        "private-thread-id",
        "private-item-id",
        "private executable",
        "private-path",
        "private command result",
        "private-server",
        "private-tool",
        "private-result",
        "private assistant message",
    ):
        assert secret not in serialized
    assert result["aggregates"]["command_result_observed_characters"] == len(
        "private command result"
    )
    assert "model_context" not in "command_result_observed_characters"
    assert result["aggregates"]["action_counts"]["command_execution"] == 1
    assert result["aggregates"]["action_counts"]["mcp_tool_call"] == 1
    assert result["stream"]["complete"] is True
    assert result["eligibility"]["usage_metrics"]["eligible"] is True
    assert result["identity"] == {
        "native_thread_observed": True,
        "native_turn_observed": False,
        "api_request_observed": False,
        "execution_context_observed": False,
        "identifiers_synthesized": False,
        "fresh_single_turn_declared": True,
    }


def test_top_level_error_cannot_collide_with_participant_item_id() -> None:
    result = reduce(
        *base_events(
            {"type": "error", "message": "private top-level error"},
            {
                "type": "item.completed",
                "item": {
                    "id": "top-level-error-3",
                    "type": "error",
                    "status": "failed",
                    "message": "private item error",
                },
            },
        )
    )

    assert_valid(result)
    assert result["stream"]["action_count"] == 2
    assert result["stream"]["action_record_count"] == 2
    assert result["aggregates"]["action_counts"]["error"] == 2
    assert len(result["actions"]) == 2
    assert result["eligibility"]["action_metrics"] == {
        "eligible": True,
        "reasons": [],
    }


def test_elapsed_envelopes_are_preserved_only_as_monotonic_numbers() -> None:
    raw = base_events(
        {
            "type": "item.completed",
            "item": {
                "id": "private",
                "type": "web_search",
                "query": "private query",
                "action": {"type": "private action"},
            },
        }
    )
    envelopes = tuple(
        {"elapsed_ms": index * 5, "event": event, "private": "discard me"}
        for index, event in enumerate(raw)
    )
    result = codex_jsonl.reduce_stream(
        encode_events(*envelopes),
        process_exit_code=0,
        fresh_thread=True,
        adapter_duration_ms=30,
    )

    assert_valid(result)
    assert result["actions"] == [
        {
            "sequence": 1,
            "kind": "web_search",
            "status": "completed",
            "exit_bucket": "not_applicable",
            "elapsed_ms": 10,
        }
    ]
    assert "discard me" not in json.dumps(result)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b'{"type":"turn.started","type":"turn.failed"}\n', "duplicate_json_key"),
        (b"\xff\n", "invalid_utf8"),
        (b"[]\n", "event_not_object"),
        (b"not-json\n", "invalid_json"),
        (b"\n", "empty_jsonl_line"),
        (b'{"type":"error","value":NaN}\n', "nonfinite_json_number"),
        (b'{"type":"error","value":1e999}\n', "nonfinite_json_number"),
        (
            b'{"type":"error","value":' + b"9" * 5000 + b"}\n",
            "json_number_out_of_range",
        ),
    ],
)
def test_unsafe_json_is_rejected_without_echo(payload: bytes, code: str) -> None:
    with pytest.raises(codex_jsonl.UnsafeTrace) as caught:
        codex_jsonl.reduce_stream(io.BytesIO(payload))
    assert caught.value.code == code
    source_text = payload.decode("utf-8", errors="ignore").strip()
    if source_text:
        assert source_text not in str(caught.value)


def test_depth_and_line_bounds_are_rejected() -> None:
    nested: object = "private"
    for _ in range(codex_jsonl.MAX_JSON_DEPTH + 1):
        nested = [nested]
    with pytest.raises(codex_jsonl.UnsafeTrace, match="json_depth_limit_exceeded"):
        codex_jsonl.reduce_stream(encode_events({"type": "unknown", "payload": nested}))

    oversized = b'{"type":"error","message":"' + (
        b"x" * codex_jsonl.MAX_LINE_BYTES
    ) + b'"}\n'
    with pytest.raises(codex_jsonl.UnsafeTrace, match="line_byte_limit_exceeded"):
        codex_jsonl.reduce_stream(io.BytesIO(oversized))


def test_stream_and_event_caps_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_jsonl, "MAX_STREAM_BYTES", 20)
    with pytest.raises(codex_jsonl.UnsafeTrace, match="stream_byte_limit_exceeded"):
        codex_jsonl.reduce_stream(
            encode_events({"type": "turn.started"}, {"type": "turn.started"})
        )

    monkeypatch.setattr(codex_jsonl, "MAX_STREAM_BYTES", 1024)
    monkeypatch.setattr(codex_jsonl, "MAX_EVENTS", 1)
    with pytest.raises(codex_jsonl.UnsafeTrace, match="event_limit_exceeded"):
        codex_jsonl.reduce_stream(
            encode_events({"type": "turn.started"}, {"type": "turn.failed"})
        )


def test_non_monotonic_envelope_is_rejected() -> None:
    with pytest.raises(codex_jsonl.UnsafeTrace, match="non_monotonic_elapsed_ms"):
        codex_jsonl.reduce_stream(
            encode_events(
                {"elapsed_ms": 2, "event": {"type": "thread.started", "thread_id": "x"}},
                {"elapsed_ms": 1, "event": {"type": "turn.started"}},
            )
        )


@pytest.mark.parametrize(
    "events",
    [
        (),
        ({"type": "thread.started", "thread_id": "private"},),
        (
            {"type": "thread.started", "thread_id": "private"},
            {"type": "turn.started"},
        ),
    ],
)
def test_legitimate_truncated_streams_remain_valid_itt_trace_inputs(
    events: tuple[dict[str, object], ...],
) -> None:
    result = reduce(*events)

    assert_valid(result)
    assert result["stream"]["complete"] is False
    validate_trace(result)


def test_missing_terminal_cannot_claim_complete_action_eligibility() -> None:
    result = reduce(
        {"type": "thread.started", "thread_id": "private"},
        {"type": "turn.started"},
    )
    result["stream"]["issues"].remove("incomplete_terminal")
    result["stream"]["complete"] = True
    result["eligibility"]["action_metrics"] = {"eligible": True, "reasons": []}

    with pytest.raises(EvaluationError, match="terminal|lifecycle"):
        validate_trace(result)


def test_unknown_variant_is_safely_reduced_but_metrics_are_ineligible() -> None:
    result = reduce(
        *base_events(
            {"type": "future.event", "private_future_payload": "do not publish"}
        )
    )

    assert_valid(result)
    assert result["stream"]["complete"] is False
    assert result["stream"]["issues"] == ["unknown_event_variant"]
    assert result["eligibility"]["action_metrics"]["eligible"] is False
    assert result["usage"] is None
    assert "private_future_payload" not in json.dumps(result)


def test_public_action_kind_cannot_hide_web_or_collaboration_aggregate() -> None:
    result = reduce(
        *base_events(
            {
                "type": "item.completed",
                "item": {
                    "id": "private",
                    "type": "web_search",
                    "query": "private",
                },
            }
        )
    )
    result["actions"][0]["kind"] = "agent_message"

    with pytest.raises(EvaluationError, match="aggregate"):
        validate_trace(result)


def test_non_command_action_cannot_claim_a_command_exit_bucket() -> None:
    result = reduce(
        *base_events(
            {
                "type": "item.completed",
                "item": {"id": "private", "type": "agent_message"},
            }
        )
    )
    result["actions"][0]["exit_bucket"] = "zero"

    with pytest.raises(EvaluationError, match="non-command"):
        validate_trace(result)


def test_lifecycle_conflict_and_incomplete_terminal_are_explicit() -> None:
    item = {"id": "private", "type": "command_execution", "status": "in_progress"}
    conflict = reduce(
        *base_events(
            {"type": "item.started", "item": item},
            {"type": "item.started", "item": item},
        )
    )
    assert_valid(conflict)
    assert "lifecycle_conflict" in conflict["stream"]["issues"]
    assert conflict["stream"]["complete"] is False

    missing = reduce(
        {"type": "thread.started", "thread_id": "private"},
        {"type": "turn.started"},
    )
    assert_valid(missing)
    assert missing["stream"]["terminal"] == "missing"
    assert "incomplete_terminal" in missing["stream"]["issues"]
    assert missing["usage"] is None


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "item.started",
            "item": {
                "id": "private",
                "type": "command_execution",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "private",
                "type": "command_execution",
                "status": "in_progress",
            },
        },
    ],
)
def test_event_and_item_status_conflicts_invalidate_lifecycle(
    event: dict[str, object],
) -> None:
    result = reduce(*base_events(event))

    assert_valid(result)
    assert "lifecycle_conflict" in result["stream"]["issues"]
    assert result["stream"]["complete"] is False
    assert result["eligibility"]["action_metrics"]["eligible"] is False


@pytest.mark.parametrize(
    "events",
    [
        (
            {"type": "thread.started", "thread_id": "private"},
            {
                "type": "item.completed",
                "item": {"id": "private-item", "type": "agent_message"},
            },
            {"type": "turn.started"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            },
        ),
        (
            {"type": "turn.started"},
            {"type": "thread.started", "thread_id": "private"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            },
        ),
    ],
)
def test_out_of_order_turn_lifecycle_is_metric_ineligible(
    events: tuple[dict[str, object], ...],
) -> None:
    result = reduce(*events)

    assert_valid(result)
    assert result["stream"]["complete"] is False
    assert "lifecycle_conflict" in result["stream"]["issues"]
    assert result["eligibility"]["action_metrics"]["eligible"] is False
    assert result["eligibility"]["usage_metrics"]["eligible"] is False


def test_collab_marks_action_coverage_gap_without_publishing_ids_or_prompt() -> None:
    result = reduce(
        *base_events(
            {
                "type": "item.completed",
                "item": {
                    "id": "private-collab",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "private-parent",
                    "receiver_thread_ids": ["private-child"],
                    "prompt": "private delegation prompt",
                    "agents_states": {},
                    "status": "completed",
                },
            }
        )
    )

    assert_valid(result)
    assert result["stream"]["complete"] is True
    assert result["eligibility"]["action_metrics"] == {
        "eligible": False,
        "reasons": ["collab_coverage_gap"],
    }
    serialized = json.dumps(result)
    assert "private-parent" not in serialized
    assert "private-child" not in serialized
    assert "private delegation prompt" not in serialized


def test_public_action_cap_truncates_safely_and_invalidates_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_jsonl, "MAX_PUBLIC_ACTIONS", 2)
    result = reduce(
        *base_events(
            *(
                {
                    "type": "item.completed",
                    "item": {"id": f"private-{index}", "type": "agent_message", "text": "x"},
                }
                for index in range(3)
            )
        )
    )

    assert len(result["actions"]) == 2
    assert result["stream"]["action_count"] == 3
    assert result["stream"]["action_record_count"] == 3
    assert "public_action_cap_exceeded" in result["stream"]["issues"]
    assert result["eligibility"]["action_metrics"]["eligible"] is False


def test_usage_is_never_zero_filled_when_not_eligible() -> None:
    not_fresh = codex_jsonl.reduce_stream(
        encode_events(*base_events()), process_exit_code=0, fresh_thread=False
    )
    assert_valid(not_fresh)
    assert not_fresh["usage"] is None
    assert not_fresh["eligibility"]["usage_metrics"] == {
        "eligible": False,
        "reasons": ["fresh_single_turn_not_declared"],
    }

    failed = codex_jsonl.reduce_stream(
        encode_events(
            {"type": "thread.started", "thread_id": "private"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "private error"}},
        ),
        process_exit_code=1,
        fresh_thread=True,
    )
    assert_valid(failed)
    assert failed["stream"]["complete"] is True
    assert failed["stream"]["terminal"] == "failed"
    assert failed["usage"] is None
    assert "terminal_not_completed" in failed["eligibility"]["usage_metrics"]["reasons"]


def test_missing_command_result_is_unknown_not_zero() -> None:
    result = reduce(
        *base_events(
            {
                "type": "item.completed",
                "item": {
                    "id": "private",
                    "type": "command_execution",
                    "status": "failed",
                    "exit_code": 1,
                },
            }
        )
    )
    assert_valid(result)
    assert result["aggregates"]["command_result_observed_characters"] is None
    assert result["actions"][0]["exit_bucket"] == "nonzero"


def test_provider_session_engineering_and_context_metrics_are_always_ineligible() -> None:
    result = reduce(*base_events())
    assert_valid(result)
    eligibility = result["eligibility"]
    for name in (
        "session_metrics",
        "provider_request_metrics",
        "engineering_outcome_metrics",
        "model_context_bytes_metrics",
    ):
        assert eligibility[name]["eligible"] is False
        assert eligibility[name]["reasons"]


def test_action_metrics_require_explicit_fresh_single_turn_declaration() -> None:
    result = codex_jsonl.reduce_stream(
        encode_events(*base_events()),
        process_exit_code=0,
        fresh_thread=False,
        adapter_duration_ms=25,
    )

    assert_valid(result)
    assert result["stream"]["complete"] is True
    assert result["eligibility"]["action_metrics"] == {
        "eligible": False,
        "reasons": ["fresh_single_turn_not_declared"],
    }


@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": 1,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "reasoning_output_tokens": 1,
        },
        {
            "input_tokens": 3,
            "cached_input_tokens": 1,
            "output_tokens": 1,
            "reasoning_output_tokens": 2,
        },
    ],
)
def test_usage_subsets_cannot_exceed_codex_totals(usage: dict[str, int]) -> None:
    events = list(base_events())
    events[-1] = {"type": "turn.completed", "usage": usage}
    result = reduce(*events)

    assert_valid(result)
    assert result["usage"] is None
    assert "usage_missing_or_invalid" in result["stream"]["issues"]
    assert result["eligibility"]["usage_metrics"]["eligible"] is False


def test_exact_integer_boundary_is_consistent_with_trace_schema() -> None:
    maximum = codex_jsonl.MAX_EXACT_SUMMARY_INTEGER
    events = list(base_events())
    events[-1] = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": maximum,
            "cached_input_tokens": maximum,
            "output_tokens": maximum,
            "reasoning_output_tokens": maximum,
        },
    }
    at_boundary = codex_jsonl.reduce_stream(
        encode_events(*events),
        process_exit_code=0,
        fresh_thread=True,
        adapter_duration_ms=maximum,
    )
    assert_valid(at_boundary)
    assert at_boundary["usage"]["input_tokens"] == maximum
    assert at_boundary["adapter_duration_ms"] == maximum

    events[-1]["usage"]["input_tokens"] = maximum + 1
    above_boundary = reduce(*events)
    assert_valid(above_boundary)
    assert above_boundary["usage"] is None
    assert "usage_missing_or_invalid" in above_boundary["stream"]["issues"]

    with pytest.raises(codex_jsonl.UnsafeTrace, match="invalid_adapter_duration_ms"):
        codex_jsonl.reduce_stream(
            encode_events(*base_events()),
            process_exit_code=0,
            fresh_thread=True,
            adapter_duration_ms=maximum + 1,
        )


def test_unsupported_codex_version_is_rejected() -> None:
    with pytest.raises(codex_jsonl.UnsafeTrace, match="unsupported_codex_cli_version"):
        codex_jsonl.reduce_stream(
            encode_events(*base_events()), codex_cli_version="0.144.4"
        )


def test_cli_unsupported_version_does_not_claim_the_pinned_source_identity() -> None:
    process = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "--codex-cli-version", "0.144.4"],
        input=encode_events(*base_events()).read(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert process.returncode == 2
    result = json.loads(process.stdout)
    assert_valid(result)
    assert result["source"]["codex_cli_version"] is None
    assert "source_identity_mismatch" in result["stream"]["issues"]
    assert b"0.144.4" not in process.stdout


def test_cli_emits_one_json_object_on_ineligible_trace() -> None:
    process = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "--process-exit-code", "0"],
        input=b'{"type":"thread.started","thread_id":"private"}\n',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert process.returncode == 0
    lines = process.stdout.decode("ascii").splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert_valid(result)
    assert result["stream"]["complete"] is False
    assert b"private" not in process.stdout
    assert process.stderr == b""


def test_cli_exits_two_with_ineligible_trace_for_unsafe_input_without_echoing_payload() -> None:
    secret = b'{"type":"turn.started","type":"private-secret"}\n'
    process = subprocess.run(
        [sys.executable, str(ADAPTER_PATH)],
        input=secret,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert process.returncode == 2
    result = json.loads(process.stdout)
    assert_valid(result)
    assert result["stream"] == {
        "complete": False,
        "event_count": 0,
        "action_count": 0,
        "action_record_count": 0,
        "terminal": "missing",
        "process_exit_bucket": "unknown",
        "issues": [
            "adapter_input_rejected",
            "incomplete_terminal",
            "process_exit_unknown",
        ],
    }
    assert result["eligibility"]["action_metrics"] == {
        "eligible": False,
        "reasons": [
            "adapter_input_rejected",
            "fresh_single_turn_not_declared",
            "incomplete_terminal",
        ],
    }
    assert result["usage"] is None
    assert b"private-secret" not in process.stderr
    assert b"private-secret" not in process.stdout
    assert b"duplicate_json_key" in process.stderr


def test_rejected_trace_has_one_canonical_placeholder_shape() -> None:
    result = codex_jsonl.rejected_trace(
        process_exit_code=1,
        fresh_thread=True,
        adapter_duration_ms=25,
    )
    assert_valid(result)
    assert result["adapter_duration_ms"] is None
    assert result["eligibility"]["duration_metrics"] == {
        "eligible": False,
        "reasons": ["adapter_duration_unavailable"],
    }
    validate_trace(result)

    forged = json.loads(json.dumps(result))
    forged["stream"]["event_count"] = 1
    with pytest.raises(EvaluationError, match="placeholder"):
        validate_trace(forged)


def test_duration_before_event_becomes_rejected_without_duration_claim() -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(ADAPTER_PATH),
            "--fresh-thread",
            "--adapter-duration-ms",
            "50",
        ],
        input=json.dumps(
            {
                "elapsed_ms": 100,
                "event": {"type": "thread.started", "thread_id": "private"},
            }
        ).encode("utf-8")
        + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert process.returncode == 2
    result = json.loads(process.stdout)
    assert result["adapter_duration_ms"] is None
    validate_trace(result)


def test_usage_observation_bit_and_issue_are_exactly_derived() -> None:
    result = reduce(*base_events())
    result["usage_observed_valid"] = False
    with pytest.raises(EvaluationError, match="usage"):
        validate_trace(result)

    incomplete = reduce(
        *base_events({"type": "future.event", "private": "discarded"})
    )
    assert incomplete["usage"] is None
    assert incomplete["usage_observed_valid"] is True
    assert "usage_missing_or_invalid" not in incomplete["stream"]["issues"]
    validate_trace(incomplete)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b'{"type":"error","value":1e999}\n', b"nonfinite_json_number"),
        (
            b'{"type":"error","value":' + b"9" * 5000 + b"}\n",
            b"json_number_out_of_range",
        ),
    ],
)
def test_cli_numeric_rejection_is_fixed_and_traceback_free(
    payload: bytes, code: bytes
) -> None:
    process = subprocess.run(
        [sys.executable, str(ADAPTER_PATH)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert process.returncode == 2
    result = json.loads(process.stdout)
    assert_valid(result)
    assert "adapter_input_rejected" in result["stream"]["issues"]
    assert code in process.stderr
    assert b"Traceback" not in process.stderr
    assert b"9999999999" not in process.stderr
