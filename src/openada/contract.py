"""Versioned result envelope for agent-to-EDA operations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import stat
from typing import Any, BinaryIO, Iterable, Iterator

from . import __version__
from .process import ProcessResult


SCHEMA_VERSION = "openada.result/v0alpha1"
MAX_CONTRACT_TEXT_CHARS = 4_000


class FileRecordError(ValueError):
    """Base class for unsafe or unstable file-record capture."""


class FileRecordLimitError(FileRecordError):
    """Raised before a file record can hash beyond its declared byte ceiling."""

    def __init__(self, path: Path, maximum_bytes: int, observed_bytes: int) -> None:
        self.path = path
        self.maximum_bytes = maximum_bytes
        self.observed_bytes = observed_bytes
        super().__init__(
            f"{path} is {observed_bytes} bytes; the limit is {maximum_bytes} bytes"
        )


class FileRecordUnavailableError(FileRecordError):
    """Raised when a path cannot be opened as a nonblocking regular file."""


class FileRecordChangedError(FileRecordError):
    """Raised when an opened file or its path identity changes during capture."""


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def stable_regular_file(
    path: str | Path,
) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Open one regular file without blocking on a replaced FIFO and verify identity."""

    file_path = Path(path).resolve()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(file_path, flags)
    except OSError as exc:
        raise FileRecordUnavailableError(
            f"{file_path} cannot be opened as a regular file"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileRecordUnavailableError(f"{file_path} is not a regular file")
        handle = os.fdopen(descriptor, "rb", closefd=False)
        try:
            yield handle, opened
        finally:
            handle.close()
        try:
            finished = os.fstat(descriptor)
            current = os.stat(file_path, follow_symlinks=False)
        except OSError as exc:
            raise FileRecordChangedError(
                f"{file_path} changed during bounded capture"
            ) from exc
        if (
            _file_identity(finished) != _file_identity(opened)
            or _file_identity(current) != _file_identity(opened)
        ):
            raise FileRecordChangedError(
                f"{file_path} changed during bounded capture"
            )
    finally:
        os.close(descriptor)


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


def file_record(
    path: str | Path,
    *,
    kind: str,
    role: str,
    maximum_bytes: int | None = None,
) -> dict[str, Any]:
    file_path = Path(path).resolve()
    if maximum_bytes is not None and maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    record: dict[str, Any] = {
        "kind": kind,
        "role": role,
        "path": str(file_path),
        "exists": False,
    }
    digest = hashlib.sha256()
    observed_bytes = 0
    try:
        with stable_regular_file(file_path) as (handle, opened):
            record["exists"] = True
            if maximum_bytes is not None and opened.st_size > maximum_bytes:
                raise FileRecordLimitError(file_path, maximum_bytes, opened.st_size)
            while True:
                read_size = 1024 * 1024
                if maximum_bytes is not None:
                    read_size = min(read_size, maximum_bytes - observed_bytes + 1)
                chunk = handle.read(read_size)
                if not chunk:
                    break
                observed_bytes += len(chunk)
                if maximum_bytes is not None and observed_bytes > maximum_bytes:
                    raise FileRecordLimitError(file_path, maximum_bytes, observed_bytes)
                digest.update(chunk)
            if observed_bytes != opened.st_size:
                raise FileRecordChangedError(
                    f"{file_path} changed during bounded capture"
                )
    except FileRecordUnavailableError:
        return record
    record["bytes"] = observed_bytes
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
