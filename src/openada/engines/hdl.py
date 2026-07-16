"""Shared bounded HDL input and transcript evidence helpers."""

from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import re

from ..contract import FileRecordError, file_record, stable_regular_file
from ..process import ProcessResult


MAX_HDL_INPUTS = 4_096
MAX_UNRESOLVED_INCLUDES = 4_096
MAX_HDL_PARSE_BYTES = 16 * 1024 * 1024
MAX_HDL_PATH_CHARS = 4_096
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
_INCLUDE = re.compile(r'^\s*`include\s+"([^"\r\n]+)"', re.MULTILINE)
_INCLUDE_ARGUMENT = re.compile(r"^\s*`include\s+([^\r\n]+)", re.MULTILINE)


def _has_ascii_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def valid_hdl_identifier(value: str) -> bool:
    """Accept one ordinary, unescaped HDL module/design identifier."""
    return len(value) <= 256 and bool(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value)
    )


def changed_input_paths(
    records: list[dict],
    *,
    maximum_bytes_by_kind: dict[str, int] | None = None,
) -> list[str]:
    """Rehash captured inputs and return paths whose identity or content changed."""
    limits = maximum_bytes_by_kind or {}
    changed: list[str] = []
    for before in records:
        path = before.get("path")
        kind = before.get("kind")
        role = before.get("role")
        if not all(isinstance(value, str) and value for value in (path, kind, role)):
            continue
        try:
            after = file_record(
                path,
                kind=kind,
                role=role,
                maximum_bytes=limits.get(kind, MAX_HDL_PARSE_BYTES),
            )
        except (FileRecordError, OSError, ValueError):
            changed.append(path)
            continue
        if any(
            before.get(key) != after.get(key)
            for key in ("exists", "bytes", "sha256")
        ):
            changed.append(path)
    return list(dict.fromkeys(changed))


def hdl_closure_stability(
    sources: list[str | Path],
    include_dirs: list[str | Path],
    *,
    expected_sources: list[Path],
    expected_dependencies: list[Path],
    expected_unresolved: list[str],
) -> tuple[bool, list[str]]:
    """Rescan an HDL closure and explain any durable before/after difference."""
    (
        observed_sources,
        observed_dependencies,
        _records,
        errors,
        observed_unresolved,
    ) = resolve_hdl_inputs(sources, include_dirs)
    differences = list(errors)
    if observed_sources != expected_sources:
        differences.append("ordered source identity changed")
    if observed_dependencies != expected_dependencies:
        differences.append("resolved literal include closure changed")
    if observed_unresolved != expected_unresolved:
        differences.append("unresolved literal include inventory changed")
    return not differences, differences


def resolve_hdl_inputs(
    sources: list[str | Path],
    include_dirs: list[str | Path],
) -> tuple[list[Path], list[Path], list[dict], list[str], list[str]]:
    """Resolve and hash the ordered sources plus their literal include closure.

    Macro-computed includes are intentionally outside this first contract. Literal
    includes are resolved against the including file first and then the declared
    include directories, matching common SystemVerilog frontend behavior.
    """
    errors: list[str] = []
    source_paths: list[Path] = []
    directory_paths: list[Path] = []
    if len(sources) > MAX_HDL_INPUTS:
        errors.append(f"HDL source list exceeds {MAX_HDL_INPUTS} files")
    if len(include_dirs) > MAX_HDL_INPUTS:
        errors.append(f"HDL include-directory list exceeds {MAX_HDL_INPUTS} entries")
    for label, items, destination in (
        ("source", sources[:MAX_HDL_INPUTS], source_paths),
        ("include directory", include_dirs[:MAX_HDL_INPUTS], directory_paths),
    ):
        for item in items:
            try:
                path = Path(item).expanduser().resolve()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(f"invalid HDL {label} path {item!r}: {exc}")
                continue
            path_text = str(path)
            if len(path_text) > MAX_HDL_PATH_CHARS:
                errors.append(
                    f"HDL {label} path exceeds {MAX_HDL_PATH_CHARS} characters"
                )
                continue
            if _has_ascii_control(path_text):
                errors.append(f"HDL {label} path contains an ASCII control character")
                continue
            destination.append(path)

    unresolved_includes: list[str] = []
    unresolved_seen: set[str] = set()
    for directory in directory_paths:
        if not directory.is_dir():
            errors.append(f"include directory does not exist: {directory}")

    records: list[dict] = []
    for path in source_paths:
        try:
            records.append(
                file_record(
                    path,
                    kind="hdl-source",
                    role="rtl.source",
                    maximum_bytes=MAX_HDL_PARSE_BYTES,
                )
            )
        except FileRecordError as exc:
            errors.append(str(exc))
    missing = [record["path"] for record in records if not record["exists"]]
    errors.extend(f"source file does not exist: {path}" for path in missing)

    dependencies: list[Path] = []
    seen = set(source_paths)
    queue = deque(path for path in source_paths if path.is_file())
    while queue and not errors:
        current = queue.popleft()
        try:
            with stable_regular_file(current) as (handle, opened):
                if opened.st_size > MAX_HDL_PARSE_BYTES:
                    errors.append(
                        f"{current} exceeds the {MAX_HDL_PARSE_BYTES}-byte HDL parse limit"
                    )
                    break
                text = handle.read(MAX_HDL_PARSE_BYTES + 1).decode(
                    "utf-8", errors="strict"
                )
        except (FileRecordError, OSError, UnicodeError) as exc:
            errors.append(f"cannot parse HDL includes in {current}: {exc}")
            break
        for argument in _INCLUDE_ARGUMENT.findall(text):
            if not argument.lstrip().startswith('"'):
                errors.append(
                    f"macro-computed HDL includes are unsupported in v1: {current}:{argument.strip()}"
                )
        for requested in _INCLUDE.findall(text):
            if _has_ascii_control(requested):
                errors.append(
                    f"literal HDL include path contains an ASCII control character: {current}"
                )
                continue
            candidates = [current.parent / requested]
            candidates.extend(directory / requested for directory in directory_paths)
            resolved = None
            for candidate in candidates:
                try:
                    if candidate.is_file():
                        resolved = candidate.resolve()
                        resolved_text = str(resolved)
                        if len(resolved_text) > MAX_HDL_PATH_CHARS:
                            errors.append(
                                "resolved HDL include path exceeds "
                                f"{MAX_HDL_PATH_CHARS} characters"
                            )
                            resolved = None
                            break
                        if _has_ascii_control(resolved_text):
                            errors.append(
                                "resolved HDL include path contains an ASCII control character"
                            )
                            resolved = None
                            break
                        break
                except (OSError, RuntimeError, ValueError):
                    continue
            if errors:
                continue
            if resolved is None:
                # The reference may be inside an inactive preprocessor branch.
                # The native frontend remains authoritative about whether it is
                # required; retain the unresolved literal rather than guessing.
                unresolved = f"{current}:{requested}"
                if len(unresolved) > MAX_HDL_PATH_CHARS:
                    errors.append(
                        "unresolved HDL include identity exceeds "
                        f"{MAX_HDL_PATH_CHARS} characters"
                    )
                    unresolved = unresolved[:MAX_HDL_PATH_CHARS]
                if _has_ascii_control(unresolved):
                    errors.append(
                        "unresolved HDL include identity contains an ASCII control character"
                    )
                    continue
                if unresolved not in unresolved_seen:
                    if len(unresolved_includes) >= MAX_UNRESOLVED_INCLUDES:
                        errors.append(
                            "unresolved HDL include inventory exceeds "
                            f"{MAX_UNRESOLVED_INCLUDES} entries"
                        )
                        break
                    unresolved_seen.add(unresolved)
                    unresolved_includes.append(unresolved)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            dependencies.append(resolved)
            if len(seen) > MAX_HDL_INPUTS:
                errors.append(
                    f"HDL source and include closure exceeds {MAX_HDL_INPUTS} files"
                )
                break
            try:
                record = file_record(
                    resolved,
                    kind="hdl-include",
                    role="rtl.include",
                    maximum_bytes=MAX_HDL_PARSE_BYTES,
                )
            except FileRecordError as exc:
                errors.append(str(exc))
                continue
            records.append(record)
            if not record["exists"]:
                errors.append(f"included HDL file does not exist: {resolved}")
                continue
            queue.append(resolved)
    return source_paths, dependencies, records, errors, unresolved_includes


def write_process_transcript(path: Path, process: ProcessResult) -> None:
    """Persist exactly the bounded native output that informed normalization."""
    sections = [
        f"status: {process.status}",
        f"exit_code: {process.exit_code}",
        f"stdout_bytes: {process.stdout_bytes}",
        f"stderr_bytes: {process.stderr_bytes}",
        f"stdout_truncated: {str(process.stdout_truncated).lower()}",
        f"stderr_truncated: {str(process.stderr_truncated).lower()}",
        "--- stdout ---",
        process.stdout,
        "--- stderr ---",
        process.stderr,
    ]
    encoded = "\n".join(sections).encode("utf-8", errors="replace")
    if len(encoded) > MAX_TRANSCRIPT_BYTES:
        raise ValueError(
            f"process transcript exceeds {MAX_TRANSCRIPT_BYTES} bytes"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short write while retaining process transcript")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
