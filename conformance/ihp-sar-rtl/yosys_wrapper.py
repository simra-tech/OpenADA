#!/usr/bin/env python3
"""Execute the pinned Yosys binary and retain its exact native byte streams."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


NATIVE_YOSYS = "/foss/tools/yosys/bin/yosys"
TRANSCRIPT_ENV = "OPENADA_YOSYS_TRANSCRIPT"
MAX_STREAM_BYTES = 8 * 1024 * 1024


def _stream_record(body: bytes) -> dict[str, object]:
    return {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "base64": base64.b64encode(body).decode("ascii"),
    }


def _write_transcript(path: Path, document: dict[str, object]) -> None:
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def main() -> int:
    transcript_value = os.environ.get(TRANSCRIPT_ENV)
    if not transcript_value:
        print(f"{TRANSCRIPT_ENV} is required", file=sys.stderr)
        return 125
    transcript = Path(transcript_value)
    command = [NATIVE_YOSYS, *sys.argv[1:]]
    try:
        completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        print(f"cannot execute pinned Yosys: {exc}", file=sys.stderr)
        return 125
    if len(completed.stdout) > MAX_STREAM_BYTES or len(completed.stderr) > MAX_STREAM_BYTES:
        print("native Yosys output exceeded the conformance transcript bound", file=sys.stderr)
        return 125
    transcript.parent.mkdir(parents=True, exist_ok=True)
    _write_transcript(
        transcript,
        {
            "schema": "openada.yosys-native-transcript/v1",
            "command": command,
            "cwd": os.getcwd(),
            "exit_code": completed.returncode,
            "stdout": _stream_record(completed.stdout),
            "stderr": _stream_record(completed.stderr),
        },
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
