from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.contract import (
    MAX_CONTRACT_TEXT_CHARS,
    SCHEMA_VERSION,
    bounded_text,
    diagnostic,
    file_record,
    result,
    static_execution,
)
from openada.process import ProcessResult


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "result-v0alpha1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _assert_schema_valid(payload):
    errors = sorted(VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)


def test_public_schema_is_valid_and_matches_contract_version():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["properties"]["schema"]["const"] == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("execution_status", "engineering_status"),
    [
        ("completed", "pass"),
        ("completed", "fail"),
        ("completed", "not_applicable"),
        ("invalid_request", "unknown"),
        ("not_available", "unknown"),
        ("timed_out", "unknown"),
        ("failed", "unknown"),
    ],
)
def test_result_envelope_validates_for_public_statuses(
    execution_status,
    engineering_status,
):
    payload = result(
        "conformance-test",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=engineering_status,
        summary="Schema validation fixture.",
        diagnostics=[diagnostic("info", "schema.fixture", "Validation fixture.")],
    )

    assert payload["schema"] == SCHEMA_VERSION
    _assert_schema_valid(payload)


def test_process_result_with_cwd_validates_against_schema(tmp_path):
    payload = result(
        "cwd-test",
        tool=None,
        execution=ProcessResult(
            status="completed",
            command=["tool", "--check"],
            exit_code=0,
            duration_ms=1,
            cwd=str(tmp_path.resolve()),
        ),
        engineering_status="pass",
        summary="Working directory provenance fixture.",
    )

    assert payload["execution"]["cwd"] == str(tmp_path.resolve())
    _assert_schema_valid(payload)


def test_schema_requires_typed_provenance_and_hashes_for_existing_files():
    payload = result(
        "schema-negative-test",
        tool=None,
        execution=static_execution(),
        engineering_status="not_applicable",
        summary="Negative schema fixture.",
    )
    payload["provenance"]["host"] = None
    payload["provenance"]["created_at"] = "not-a-timestamp"
    payload["inputs"] = [
        {"kind": "test", "role": "input", "path": "/tmp/input", "exists": True}
    ]

    errors = list(VALIDATOR.iter_errors(payload))
    assert errors
    assert any(list(error.path) == ["provenance", "host"] for error in errors)
    assert any(list(error.path) == ["provenance", "created_at"] for error in errors)
    assert any(list(error.path) == ["inputs", 0] for error in errors)


def test_file_record_hashes_native_artifact(tmp_path):
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(b"openada\n")

    record = file_record(artifact, kind="test", role="evidence")

    assert record["exists"] is True
    assert record["bytes"] == len(b"openada\n")
    assert record["sha256"] == hashlib.sha256(b"openada\n").hexdigest()


def test_contract_explanatory_text_is_bounded():
    long_text = "head-" + ("x" * 10_000) + "-tail"

    payload = result(
        "bounded-text",
        tool=None,
        execution={
            "status": "failed",
            "exit_code": None,
            "duration_ms": 0,
            "command": [],
            "error": long_text,
        },
        engineering_status="unknown",
        summary=long_text,
        diagnostics=[diagnostic("error", "bounded.test", long_text, hint=long_text)],
    )

    for value in (
        payload["execution"]["error"],
        payload["engineering"]["summary"],
        payload["diagnostics"][0]["message"],
        payload["diagnostics"][0]["hint"],
    ):
        assert len(value) == MAX_CONTRACT_TEXT_CHARS
        assert value.startswith("head-")
        assert value.endswith("-tail")
    assert bounded_text("short") == "short"
    _assert_schema_valid(payload)


def test_schema_rejects_unbounded_explanatory_text():
    payload = result(
        "unbounded-negative",
        tool=None,
        execution=static_execution(),
        engineering_status="not_applicable",
        summary="bounded",
    )
    payload["engineering"]["summary"] = "x" * (MAX_CONTRACT_TEXT_CHARS + 1)

    errors = list(VALIDATOR.iter_errors(payload))

    assert any(list(error.path) == ["engineering", "summary"] for error in errors)
