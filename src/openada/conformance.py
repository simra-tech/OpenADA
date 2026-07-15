"""Reusable result-contract checks for driver contributors and conformance cases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
from itertools import islice
import json
import os
from pathlib import Path
import stat
import sysconfig
from typing import Any

from .contract import bounded_text


RESULT_SCHEMA_FILENAME = "result-v0alpha1.schema.json"
DEFAULT_MAX_RECORDED_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_RECORDED_FILES = 512
DEFAULT_MAX_TOTAL_RECORDED_FILE_BYTES = 512 * 1024 * 1024
MAX_CONFORMANCE_ISSUES = 100
MAX_CONFORMANCE_ISSUE_CHARS = 2_000


class ResultConformanceError(ValueError):
    """Raised when a result does not satisfy its declared contract and expectations."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


def result_schema_path() -> Path:
    """Locate the canonical v0alpha1 schema in a checkout or installed wheel."""
    source_candidate = (
        Path(__file__).resolve().parents[2] / "schemas" / RESULT_SCHEMA_FILENAME
    )
    if source_candidate.is_file():
        return source_candidate

    try:
        installed = distribution("openada")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        suffix = f"share/openada/schemas/{RESULT_SCHEMA_FILENAME}"
        for entry in installed.files or ():
            if entry.as_posix().endswith(suffix):
                candidate = Path(installed.locate_file(entry)).resolve()
                if candidate.is_file():
                    return candidate

    data_candidate = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "openada"
        / "schemas"
        / RESULT_SCHEMA_FILENAME
    )
    if data_candidate.is_file():
        return data_candidate
    raise FileNotFoundError(
        f"OpenADA result schema is not installed: {RESULT_SCHEMA_FILENAME}"
    )


def load_result_schema() -> dict[str, Any]:
    """Load the canonical v0alpha1 JSON Schema."""
    return json.loads(result_schema_path().read_text(encoding="utf-8"))


def _json_pointer(parts: Iterable[object]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "#" if not encoded else "#/" + "/".join(encoded)


def _schema_issues(payload: Mapping[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - exercised in an isolated install
        raise RuntimeError(
            "result validation requires OpenADA's jsonschema dependency; "
            "install it with: python -m pip install 'jsonschema>=4.18'"
        ) from exc

    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return [f"#: result is not strict JSON data: {exc}"]

    validator = Draft202012Validator(
        load_result_schema(),
        format_checker=FormatChecker(),
    )
    sampled_errors = list(
        islice(validator.iter_errors(payload), MAX_CONFORMANCE_ISSUES + 1)
    )
    truncated = len(sampled_errors) > MAX_CONFORMANCE_ISSUES
    if truncated:
        sampled_errors = sampled_errors[: MAX_CONFORMANCE_ISSUES - 1]
    errors = sorted(
        sampled_errors,
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    issues = [
        bounded_text(
            f"{_json_pointer(error.absolute_path)}: {error.message}",
            limit=MAX_CONFORMANCE_ISSUE_CHARS,
        )
        for error in errors
    ]
    if truncated:
        issues.append(
            f"#: additional schema issues omitted after {MAX_CONFORMANCE_ISSUES - 1}"
        )
    return issues


def _recorded_file_issues(
    payload: Mapping[str, Any],
    *,
    max_file_bytes: int | None,
    max_files: int | None,
    max_total_bytes: int | None,
) -> list[str]:
    issues: list[str] = []
    record_count = len(payload["inputs"]) + len(payload["artifacts"])
    if max_files is not None and record_count > max_files:
        return [
            f"#: result records {record_count} files, exceeding the "
            f"{max_files}-file verification bound"
        ]
    total_hashed_bytes = 0
    for section in ("inputs", "artifacts"):
        records = payload[section]
        for index, record in enumerate(records):
            if len(issues) >= MAX_CONFORMANCE_ISSUES:
                return issues
            label = f"#/{section}/{index}"
            path = Path(record["path"])
            if not path.is_absolute():
                issues.append(f"{label}: recorded file path is not absolute")
                continue
            recorded_exists = record["exists"]
            if not recorded_exists:
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    issues.append(
                        f"{label}: cannot verify recorded absent path: {exc}"
                    )
                else:
                    issues.append(f"{label}: path now exists but result records exists=false")
                continue

            try:
                path_stat = path.lstat()
            except OSError as exc:
                issues.append(f"{label}: cannot stat recorded file: {exc}")
                continue
            if stat.S_ISLNK(path_stat.st_mode):
                issues.append(f"{label}: recorded file may not be a symbolic link")
                continue
            if not stat.S_ISREG(path_stat.st_mode):
                issues.append(f"{label}: recorded path is not a regular file")
                continue
            if max_file_bytes is not None and path_stat.st_size > max_file_bytes:
                issues.append(
                    f"{label}: recorded file is {path_stat.st_size} bytes, exceeding "
                    f"the {max_file_bytes}-byte verification bound"
                )
                continue

            digest = hashlib.sha256()
            observed_bytes = 0
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as handle:
                    opened_stat = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened_stat.st_mode):
                        issues.append(f"{label}: recorded path changed from a regular file")
                        continue
                    if (opened_stat.st_dev, opened_stat.st_ino) != (
                        path_stat.st_dev,
                        path_stat.st_ino,
                    ):
                        issues.append(f"{label}: recorded file changed while being opened")
                        continue
                    if (
                        max_file_bytes is not None
                        and opened_stat.st_size > max_file_bytes
                    ):
                        issues.append(
                            f"{label}: opened file is {opened_stat.st_size} bytes, "
                            f"exceeding the {max_file_bytes}-byte verification bound"
                        )
                        continue
                    if (
                        max_total_bytes is not None
                        and total_hashed_bytes + opened_stat.st_size
                        > max_total_bytes
                    ):
                        issues.append(
                            f"{label}: recorded files exceed the "
                            f"{max_total_bytes}-byte aggregate verification bound"
                        )
                        return issues
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        observed_bytes += len(chunk)
                        if (
                            max_file_bytes is not None
                            and observed_bytes > max_file_bytes
                        ):
                            issues.append(
                                f"{label}: file grew beyond the {max_file_bytes}-byte "
                                "verification bound while being read"
                            )
                            break
                        if (
                            max_total_bytes is not None
                            and total_hashed_bytes + len(chunk) > max_total_bytes
                        ):
                            issues.append(
                                f"{label}: recorded files grew beyond the "
                                f"{max_total_bytes}-byte aggregate verification bound"
                            )
                            return issues
                        total_hashed_bytes += len(chunk)
                        digest.update(chunk)
            except OSError as exc:
                issues.append(f"{label}: cannot read recorded file: {exc}")
                continue

            if max_file_bytes is not None and observed_bytes > max_file_bytes:
                continue
            if observed_bytes != record["bytes"]:
                issues.append(
                    f"{label}: byte count mismatch "
                    f"(recorded {record['bytes']}, observed {observed_bytes})"
                )
            observed_hash = digest.hexdigest()
            if observed_hash != record["sha256"]:
                issues.append(
                    f"{label}: SHA-256 mismatch "
                    f"(recorded {record['sha256']}, observed {observed_hash})"
                )
    return issues


def result_conformance_issues(
    payload: Mapping[str, Any],
    *,
    expected_operation: str | None = None,
    expected_execution_status: str | None = None,
    expected_engineering_status: str | None = None,
    required_artifact_roles: Iterable[str] = (),
    required_diagnostic_codes: Iterable[str] = (),
    verify_recorded_files: bool = False,
    max_recorded_file_bytes: int | None = DEFAULT_MAX_RECORDED_FILE_BYTES,
    max_recorded_files: int | None = DEFAULT_MAX_RECORDED_FILES,
    max_total_recorded_file_bytes: int | None = (
        DEFAULT_MAX_TOTAL_RECORDED_FILE_BYTES
    ),
) -> tuple[str, ...]:
    """Return deterministic contract, expectation, and optional file-integrity issues."""
    if max_recorded_file_bytes is not None and max_recorded_file_bytes < 0:
        raise ValueError("max_recorded_file_bytes must be non-negative or None")
    if max_recorded_files is not None and max_recorded_files < 0:
        raise ValueError("max_recorded_files must be non-negative or None")
    if (
        max_total_recorded_file_bytes is not None
        and max_total_recorded_file_bytes < 0
    ):
        raise ValueError(
            "max_total_recorded_file_bytes must be non-negative or None"
        )

    issues = _schema_issues(payload)
    if issues:
        return tuple(issues)

    if expected_operation is not None and payload["operation"] != expected_operation:
        issues.append(
            f"#/operation: expected {expected_operation!r}, observed {payload['operation']!r}"
        )
    observed_execution = payload["execution"]["status"]
    if (
        expected_execution_status is not None
        and observed_execution != expected_execution_status
    ):
        issues.append(
            "#/execution/status: "
            f"expected {expected_execution_status!r}, observed {observed_execution!r}"
        )
    observed_engineering = payload["engineering"]["status"]
    if (
        expected_engineering_status is not None
        and observed_engineering != expected_engineering_status
    ):
        issues.append(
            "#/engineering/status: "
            f"expected {expected_engineering_status!r}, observed {observed_engineering!r}"
        )

    artifact_roles = {
        record["role"] for record in payload["artifacts"] if record["exists"]
    }
    for role in sorted(set(required_artifact_roles) - artifact_roles):
        issues.append(f"#/artifacts: required existing artifact role is missing: {role}")

    diagnostic_codes = {record["code"] for record in payload["diagnostics"]}
    for code in sorted(set(required_diagnostic_codes) - diagnostic_codes):
        issues.append(f"#/diagnostics: required diagnostic code is missing: {code}")

    if verify_recorded_files:
        issues.extend(
            _recorded_file_issues(
                payload,
                max_file_bytes=max_recorded_file_bytes,
                max_files=max_recorded_files,
                max_total_bytes=max_total_recorded_file_bytes,
            )
        )
    if len(issues) > MAX_CONFORMANCE_ISSUES:
        issues = issues[: MAX_CONFORMANCE_ISSUES - 1] + [
            f"#: additional conformance issues omitted after {MAX_CONFORMANCE_ISSUES - 1}"
        ]
    return tuple(
        bounded_text(issue, limit=MAX_CONFORMANCE_ISSUE_CHARS) for issue in issues
    )


def assert_result_conforms(
    payload: Mapping[str, Any],
    **expectations: Any,
) -> None:
    """Raise ``ResultConformanceError`` when any result check fails."""
    issues = result_conformance_issues(payload, **expectations)
    if issues:
        raise ResultConformanceError(issues)
