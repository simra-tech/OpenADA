#!/usr/bin/env python3
"""Check one captured OpenADA result against the contributor conformance API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from openada.conformance import result_conformance_issues


MAX_RESULT_JSON_BYTES = 5 * 1024 * 1024


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a captured OpenADA result and optional expectations."
    )
    parser.add_argument("result", help="Path to one OpenADA result JSON document.")
    parser.add_argument("--expect-operation")
    parser.add_argument(
        "--expect-execution",
        choices=["completed", "timed_out", "not_available", "invalid_request", "failed"],
    )
    parser.add_argument(
        "--expect-engineering",
        choices=["pass", "fail", "unknown", "not_applicable"],
    )
    parser.add_argument(
        "--require-artifact-role",
        action="append",
        default=[],
        help="Require an existing artifact with this role. Repeatable.",
    )
    parser.add_argument(
        "--require-diagnostic-code",
        action="append",
        default=[],
        help="Require a diagnostic code. Repeatable.",
    )
    parser.add_argument(
        "--verify-files",
        action="store_true",
        help="Recheck existence, regular-file type, byte count, and SHA-256 for inputs/artifacts.",
    )
    parser.add_argument(
        "--max-file-mib",
        type=int,
        default=100,
        help="Maximum size of each rehashed file when --verify-files is used (default: 100).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=512,
        help="Maximum number of recorded files checked with --verify-files (default: 512).",
    )
    parser.add_argument(
        "--max-total-mib",
        type=int,
        default=512,
        help="Aggregate rehash bound for --verify-files (default: 512 MiB).",
    )
    return parser


def _emit(status: str, result_path: Path, issues: list[str]) -> None:
    print(
        json.dumps(
            {
                "status": status,
                "result": str(result_path),
                "issues": issues,
            },
            allow_nan=False,
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_path = Path(args.result).expanduser().absolute()
    if args.max_file_mib < 0 or args.max_files < 0 or args.max_total_mib < 0:
        _emit("error", result_path, ["file verification bounds must be non-negative"])
        return 2
    try:
        result_stat = result_path.lstat()
        if stat.S_ISLNK(result_stat.st_mode) or not stat.S_ISREG(result_stat.st_mode):
            raise ValueError("result path must be a regular, non-symbolic-link file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(result_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("result path changed from a regular file")
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                result_stat.st_dev,
                result_stat.st_ino,
            ):
                raise ValueError("result path changed while being opened")
            encoded = handle.read(MAX_RESULT_JSON_BYTES + 1)
        if len(encoded) > MAX_RESULT_JSON_BYTES:
            raise ValueError(
                f"result JSON exceeds the {MAX_RESULT_JSON_BYTES}-byte input bound"
            )
        payload = json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_nonstandard_constant,
        )
        issues = list(
            result_conformance_issues(
                payload,
                expected_operation=args.expect_operation,
                expected_execution_status=args.expect_execution,
                expected_engineering_status=args.expect_engineering,
                required_artifact_roles=args.require_artifact_role,
                required_diagnostic_codes=args.require_diagnostic_code,
                verify_recorded_files=args.verify_files,
                max_recorded_file_bytes=args.max_file_mib * 1024 * 1024,
                max_recorded_files=args.max_files,
                max_total_recorded_file_bytes=args.max_total_mib * 1024 * 1024,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        _emit("error", result_path, [str(exc)])
        return 2

    if issues:
        _emit("fail", result_path, issues)
        return 1
    _emit("pass", result_path, [])
    return 0


if __name__ == "__main__":
    sys.exit(main())
