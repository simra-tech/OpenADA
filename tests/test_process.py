from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from openada.process import MAX_CAPTURE_CHARS, MAX_CAPTURE_LIMIT_BYTES, run_process


def test_process_capture_keeps_only_tail():
    payload = "prefix-" + ("x" * (MAX_CAPTURE_CHARS + 1_000)) + "-suffix"

    result = run_process([sys.executable, "-c", f"print({payload!r}, end='')"])

    assert result.status == "completed"
    assert len(result.stdout) <= MAX_CAPTURE_CHARS
    assert result.stdout.endswith("-suffix")
    assert not result.stdout.startswith("prefix-")


def test_process_accepts_a_larger_bounded_complete_capture():
    payload = "prefix-" + ("x" * (MAX_CAPTURE_CHARS + 1_000)) + "-suffix"

    result = run_process(
        [sys.executable, "-c", f"print({payload!r}, end='')"],
        capture_limit_bytes=len(payload.encode("utf-8")),
    )

    assert result.status == "completed"
    assert result.stdout == payload
    assert result.stdout_bytes == len(payload.encode("utf-8"))
    assert result.stdout_truncated is False


def test_process_rejects_unbounded_or_noninteger_capture_limits():
    for value in (0, -1, True, MAX_CAPTURE_LIMIT_BYTES + 1):
        result = run_process(
            [sys.executable, "-c", "raise SystemExit(99)"],
            capture_limit_bytes=value,
        )

        assert result.status == "failed"
        assert result.exit_code is None
        assert "capture_limit_bytes" in (result.error or "")


def test_process_timeout_keeps_bounded_output():
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys,time; print('started', flush=True); time.sleep(2)",
        ],
        timeout=0.1,
    )

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.stdout.strip() == "started"
    assert "0.1 seconds" in (result.error or "")


def test_process_timeout_includes_descendants_holding_capture_pipes(tmp_path):
    pid_file = tmp_path / "child.pid"
    result = run_process(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            ),
            str(pid_file),
        ],
        timeout=0.2,
    )

    assert result.status == "timed_out"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    time.sleep(0.05)
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        alive = False
    else:
        stat = Path(f"/proc/{child_pid}/stat")
        alive = stat.exists() and stat.read_text(encoding="utf-8").split()[2] != "Z"
    assert not alive


def test_process_rejects_nonfinite_timeout():
    result = run_process([sys.executable, "-c", "pass"], timeout=float("inf"))

    assert result.status == "failed"
    assert "finite" in (result.error or "")


def test_process_rejects_non_numeric_timeout():
    for value in (None, "1", True, object()):
        result = run_process([sys.executable, "-c", "pass"], timeout=value)

        assert result.status == "failed"
        assert "finite" in (result.error or "")


def test_process_records_resolved_working_directory(tmp_path):
    result = run_process([sys.executable, "-c", "pass"], cwd=tmp_path)

    assert result.status == "completed"
    assert result.cwd == str(tmp_path.resolve())


def test_process_records_inherited_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = run_process([sys.executable, "-c", "pass"])

    assert result.status == "completed"
    assert result.cwd == str(tmp_path.resolve())


def test_process_returns_failed_for_missing_working_directory(tmp_path):
    missing = tmp_path / "missing"

    result = run_process([sys.executable, "-c", "pass"], cwd=missing)

    assert result.status == "failed"
    assert result.exit_code is None
    assert result.cwd is None
    assert "working directory" in (result.error or "")


def test_process_returns_failed_for_working_directory_symlink_loop(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)

    result = run_process([sys.executable, "-c", "pass"], cwd=first)

    assert result.status == "failed"
    assert result.exit_code is None
    assert result.cwd is None
    assert "working directory" in (result.error or "")


def test_process_returns_failed_when_inherited_cwd_was_deleted():
    script = """
import json
import os
from pathlib import Path
import sys
import tempfile

from openada.process import run_process

directory = Path(tempfile.mkdtemp(prefix="openada-deleted-cwd-"))
os.chdir(directory)
os.rmdir(directory)
result = run_process([sys.executable, "-c", "pass"])
print(json.dumps({"status": result.status, "cwd": result.cwd, "error": result.error}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "failed"
    assert payload["cwd"] is None
    assert "working directory" in payload["error"]


def test_process_returns_failed_for_empty_command(tmp_path):
    result = run_process([], cwd=tmp_path)

    assert result.status == "failed"
    assert "argv" in (result.error or "")


def test_capture_records_invalid_utf8_without_hiding_replacement(tmp_path):
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'valid \\xff invalid')",
        ],
        cwd=tmp_path,
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.stdout == "valid \ufffd invalid"
    assert result.stdout_utf8_valid is False
    assert result.stderr_utf8_valid is True
