"""Bounded subprocess execution shared by deterministic EDA drivers."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping, Sequence


MAX_CAPTURE_CHARS = 12_000
MAX_CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024


def _append_tail(buffer: bytearray, chunk: bytes, limit: int = MAX_CAPTURE_CHARS) -> None:
    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    buffer.extend(chunk)
    overflow = len(buffer) - limit
    if overflow > 0:
        del buffer[:overflow]


def _drain(stream, buffer: bytearray, total: list[int], limit: int) -> None:
    try:
        while chunk := stream.read(8_192):
            total[0] += len(chunk)
            _append_tail(buffer, chunk, limit)
    except (OSError, ValueError):
        pass


def _terminate(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


@dataclass(frozen=True)
class ProcessResult:
    status: str
    command: list[str]
    exit_code: int | None
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    cwd: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_utf8_valid: bool = True
    stderr_utf8_valid: bool = True


def _decode_capture(value: bytearray) -> tuple[str, bool]:
    raw = bytes(value)
    try:
        return raw.decode("utf-8", errors="strict"), True
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), False


def run_process(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = 120.0,
    env: Mapping[str, str] | None = None,
    capture_limit_bytes: int = MAX_CAPTURE_CHARS,
) -> ProcessResult:
    """Execute an argv vector without a shell and bound captured text in memory."""
    argv = [str(part) for part in command]
    started = time.perf_counter()
    try:
        directory_path = (
            Path(cwd).expanduser().resolve(strict=True)
            if cwd is not None
            else Path.cwd().resolve(strict=True)
        )
        if not directory_path.is_dir():
            raise NotADirectoryError(f"working directory is not a directory: {directory_path}")
        working_directory = str(directory_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return ProcessResult(
            status="failed",
            command=argv,
            exit_code=None,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=f"cannot resolve working directory: {exc}",
            cwd=None,
        )
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return ProcessResult(
            status="failed",
            command=argv,
            exit_code=None,
            duration_ms=0,
            error="timeout must be finite and greater than zero",
            cwd=working_directory,
        )
    if (
        not isinstance(capture_limit_bytes, int)
        or isinstance(capture_limit_bytes, bool)
        or capture_limit_bytes <= 0
        or capture_limit_bytes > MAX_CAPTURE_LIMIT_BYTES
    ):
        return ProcessResult(
            status="failed",
            command=argv,
            exit_code=None,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=(
                "capture_limit_bytes must be an integer from 1 through "
                f"{MAX_CAPTURE_LIMIT_BYTES}"
            ),
            cwd=working_directory,
        )
    if not argv:
        return ProcessResult(
            status="failed",
            command=argv,
            exit_code=None,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error="command must contain at least one argv element",
            cwd=working_directory,
        )
    try:
        process = subprocess.Popen(
            argv,
            cwd=working_directory,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as exc:
        cwd_still_exists = Path(working_directory).is_dir()
        return ProcessResult(
            status="not_available" if cwd_still_exists else "failed",
            command=argv,
            exit_code=None,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=(
                str(exc)
                if cwd_still_exists
                else f"working directory disappeared before launch: {working_directory}"
            ),
            cwd=working_directory if cwd_still_exists else None,
        )
    except OSError as exc:
        return ProcessResult(
            status="failed",
            command=argv,
            exit_code=None,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=str(exc),
            cwd=working_directory,
        )

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_total = [0]
    stderr_total = [0]
    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_buffer, stdout_total, capture_limit_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_buffer, stderr_total, capture_limit_bytes),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    status = "completed"
    error = None
    exit_code: int | None
    try:
        exit_code = process.wait(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired:
        status = "timed_out"
        error = f"command exceeded {timeout:g} seconds"
        exit_code = None
        _terminate(process)
        process.wait()

    capture_deadline = started + max(0.1, timeout)
    for reader in readers:
        reader.join(timeout=max(0.0, capture_deadline - time.perf_counter()))
    if any(reader.is_alive() for reader in readers):
        status = "timed_out"
        exit_code = None
        error = f"command process group exceeded {timeout:g} seconds"
        _terminate(process)
        for reader in readers:
            reader.join(timeout=1)
    for stream in (process.stdout, process.stderr):
        if not stream.closed and not any(reader.is_alive() for reader in readers):
            stream.close()

    stdout, stdout_utf8_valid = _decode_capture(stdout_buffer)
    stderr, stderr_utf8_valid = _decode_capture(stderr_buffer)
    return ProcessResult(
        status=status,
        command=argv,
        exit_code=exit_code,
        duration_ms=round((time.perf_counter() - started) * 1000),
        stdout=stdout,
        stderr=stderr,
        error=error,
        cwd=working_directory,
        stdout_bytes=stdout_total[0],
        stderr_bytes=stderr_total[0],
        stdout_truncated=stdout_total[0] > len(stdout_buffer),
        stderr_truncated=stderr_total[0] > len(stderr_buffer),
        stdout_utf8_valid=stdout_utf8_valid,
        stderr_utf8_valid=stderr_utf8_valid,
    )
