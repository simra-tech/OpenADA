"""Versioned result envelope for agent-to-EDA operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import platform
from typing import Any, Iterable

from . import __version__
from .process import ProcessResult


SCHEMA_VERSION = "openada.result/v0alpha1"
MAX_CONTRACT_TEXT_CHARS = 4_000


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bounded_text(value: object, *, limit: int = MAX_CONTRACT_TEXT_CHARS) -> str:
    """Bound explanatory contract text while retaining useful head and tail context."""
    if limit < 0:
        raise ValueError("text limit must be non-negative")
    text = str(value)
    if len(text) <= limit:
        return text
    marker = " ... [truncated] ... "
    if limit <= len(marker):
        return text[:limit]
    retained = limit - len(marker)
    head = retained // 2
    tail = retained - head
    return text[:head] + marker + text[-tail:]


def diagnostic(
    severity: str,
    code: str,
    message: str,
    *,
    hint: str | None = None,
) -> dict[str, str]:
    item = {"severity": severity, "code": code, "message": bounded_text(message)}
    if hint:
        item["hint"] = bounded_text(hint)
    return item


def file_record(path: str | Path, *, kind: str, role: str) -> dict[str, Any]:
    file_path = Path(path).resolve()
    record: dict[str, Any] = {
        "kind": kind,
        "role": role,
        "path": str(file_path),
        "exists": file_path.is_file(),
    }
    if not file_path.is_file():
        return record

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    record["bytes"] = file_path.stat().st_size
    record["sha256"] = digest.hexdigest()
    return record


def tool_record(
    name: str,
    *,
    path: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    return {"name": name, "path": path, "version": version}


def result(
    operation: str,
    *,
    tool: dict[str, Any] | None,
    execution: ProcessResult | dict[str, Any],
    engineering_status: str,
    summary: str,
    inputs: Iterable[dict[str, Any]] = (),
    artifacts: Iterable[dict[str, Any]] = (),
    diagnostics: Iterable[dict[str, Any]] = (),
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(execution, ProcessResult):
        execution_record: dict[str, Any] = {
            "status": execution.status,
            "exit_code": execution.exit_code,
            "duration_ms": execution.duration_ms,
            "command": execution.command,
        }
        if execution.error:
            execution_record["error"] = bounded_text(execution.error)
        if execution.cwd is not None:
            execution_record["cwd"] = execution.cwd
    else:
        execution_record = dict(execution)
        if isinstance(execution_record.get("error"), str):
            execution_record["error"] = bounded_text(execution_record["error"])

    return {
        "schema": SCHEMA_VERSION,
        "operation": operation,
        "tool": tool,
        "execution": execution_record,
        "engineering": {
            "status": engineering_status,
            "summary": bounded_text(summary),
        },
        "inputs": list(inputs),
        "artifacts": list(artifacts),
        "diagnostics": list(diagnostics),
        "data": data or {},
        "provenance": {
            "openada_version": __version__,
            "created_at": _timestamp(),
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        },
    }


def static_execution(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "exit_code": 0 if status == "completed" else None,
        "duration_ms": 0,
        "command": [],
    }
