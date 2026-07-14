"""ngspice simulation driver with explicit execution and output ownership."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Sequence

from ..contract import (
    bounded_text,
    diagnostic,
    file_record,
    result,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..process import run_process
from .ngspice_outputs import (
    OutputValidation,
    validate_ngspice_raw,
    validate_ngspice_wrdata,
)


EXECUTION_MODES = frozenset({"batch", "control"})
OUTPUT_KINDS = {
    "raw": ("ngspice-raw", validate_ngspice_raw),
    "wrdata": ("ngspice-wrdata", validate_ngspice_wrdata),
}
MAX_EXPECTED_OUTPUTS = 32
MAX_OUTPUT_PATH_CHARS = 4_096
MAX_OUTPUT_COMPONENT_CHARS = 255
MAX_SOURCE_LINE_BYTES = 65_536
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MAX_MEASUREMENTS = 200
MAX_MEASUREMENT_NAME_CHARS = 256
MAX_SOLVER_WARNING_EXAMPLES = 50

TERMINAL_CONVERGENCE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do not converge",
        r"timestep too small",
        r"transient op failed",
    )
)
RECOVERABLE_SOLVER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"convergence failure",
        r"singular matrix",
        r"matrix is singular",
        r"iteration limit reached",
        r"failed to converge",
    )
)
NATIVE_ERROR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"simulation interrupted due to error",
        r"run simulation not started",
        r"fatal error",
        r"^\s*error on line\b",
        r"^\s*error:\s+no such vector\b",
        r"unknown subckt",
        r"could not find a valid modelname",
        r"cannot find model",
    )
)
ANALYSIS_COMPLETE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*No\. of Data Rows\s*:\s*[1-9][0-9]*\s*$",
        r"^\s*Measurements for\s+\S.*\s+Analysis\s*$",
    )
)
CIRCUIT_TITLE_RE = re.compile(r"^\s*Circuit\s*:", re.IGNORECASE)
GENERIC_NATIVE_ERROR_RE = re.compile(r"^\s*(?:fatal\s+)?error(?:\s*:|\s+on\b)", re.IGNORECASE)
HARMLESS_GRAPHICS_ERROR_RE = re.compile(
    r"^\s*error:\s*\(external\)\s+no graphics interface\b",
    re.IGNORECASE,
)
BATCH_MEASUREMENT_WARNING_RE = re.compile(
    r"No\s+\.measure\s+possible\s+in\s+batch\s+mode.*-r\s+rawfile",
    re.IGNORECASE,
)
MEASUREMENT_RE = re.compile(
    r"^\s*([A-Za-z_][\w.$#-]*)\s*=\s*"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\b"
)
NONFINITE_MEASUREMENT_RE = re.compile(
    r"^\s*([A-Za-z_][\w.$#-]*)\s*=\s*[+-]?(?:nan|inf(?:inity)?)\b",
    re.IGNORECASE,
)
MEASUREMENT_DECLARATION_RE = re.compile(
    r"^\s*\.meas(?:ure)?\s+\S+\s+([A-Za-z_][\w.$#-]*)\b",
    re.IGNORECASE,
)
MEASUREMENT_SECTION_RE = re.compile(
    r"^\s*Measurements for\s+\S.*\s+Analysis\s*$",
    re.IGNORECASE,
)
CONTROL_DECLARATION_RE = re.compile(r"^\s*\.control(?:\s|$)", re.IGNORECASE)
TRANSITIVE_INCLUDE_RE = re.compile(
    r"^\s*\.(?:inc(?:lude)?|lib)\b",
    re.IGNORECASE,
)
PURE_CONTROL_SCRIPT_RE = re.compile(
    r"^\*ng_script(?:_with_params)?(?:\s|$)",
    re.IGNORECASE,
)
CONTROL_ARGUMENT_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")


@dataclass(frozen=True, slots=True)
class NgspiceOutput:
    """One required file written directly by an ngspice control deck."""

    kind: str
    path: str | Path


@dataclass(frozen=True, slots=True)
class _ResolvedOutput:
    kind: str
    declared_path: str
    path: Path


@dataclass(slots=True)
class _OutputAnchor:
    output: _ResolvedOutput
    root_fd: int
    parent_fd: int
    root_signature: tuple[int, int, int]
    directory_signatures: tuple[tuple[str, tuple[int, int, int]], ...]

    def close(self) -> None:
        for descriptor in {self.root_fd, self.parent_fd}:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _tail(value: str, limit: int = 4_000) -> str:
    return value[-limit:]


def _static_invalid(
    tool: dict,
    input_records: list[dict],
    *,
    summary: str,
    code: str,
    message: str,
    hint: str | None = None,
    data: dict | None = None,
) -> dict:
    return result(
        "simulate",
        tool=tool,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary=summary,
        inputs=input_records,
        diagnostics=[diagnostic("error", code, message, hint=hint)],
        data=data,
    )


def _lstat(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result | None:
    try:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_signature(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _content_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _hash_regular_file(
    path: str | Path,
    expected: os.stat_result,
    *,
    dir_fd: int | None = None,
) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_signature(opened) != _stat_signature(expected):
            raise OSError("file identity changed before capture")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        finished = os.fstat(descriptor)
        return digest.hexdigest(), finished
    finally:
        os.close(descriptor)


def _read_captured_text(path: Path, capture: dict, *, maximum_bytes: int) -> str | None:
    expected_digest = capture.get("sha256")
    expected_size = capture.get("bytes")
    if not isinstance(expected_digest, str) or not isinstance(expected_size, int):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != expected_size
            or opened.st_size > maximum_bytes
        ):
            return None
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(64 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    return None
                digest.update(chunk)
                chunks.append(chunk)
        finished = os.fstat(descriptor)
        if (
            _stat_signature(finished) != _stat_signature(opened)
            or digest.hexdigest() != expected_digest
        ):
            return None
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def _capture_file(
    path: Path,
    *,
    kind: str,
    role: str,
    validator: Callable[..., OutputValidation] | None = None,
    maximum_bytes: int = MAX_CAPTURE_BYTES,
    dir_fd: int | None = None,
    capture_name: str | None = None,
    require_single_link: bool = False,
) -> tuple[dict | None, dict]:
    capture: dict = {"path": str(path), "status": "missing"}
    lookup: str | Path = capture_name if capture_name is not None else path
    before = _lstat(lookup, dir_fd=dir_fd)
    if before is None:
        return None, capture
    if not stat.S_ISREG(before.st_mode):
        capture["status"] = "not_regular"
        return None, capture
    if require_single_link and before.st_nlink != 1:
        capture["status"] = "hardlinked"
        capture["link_count"] = before.st_nlink
        return None, capture
    capture["bytes"] = before.st_size
    if before.st_size > maximum_bytes:
        capture["status"] = "too_large"
        return None, capture

    validation = validator(lookup, dir_fd=dir_fd) if validator else None
    middle = _lstat(lookup, dir_fd=dir_fd)
    if middle is None or _stat_signature(middle) != _stat_signature(before):
        capture["status"] = "unstable"
        if validation:
            capture["validation"] = validation.to_dict()
        return None, capture

    try:
        digest, opened_after = _hash_regular_file(lookup, middle, dir_fd=dir_fd)
    except OSError:
        capture["status"] = "unstable"
        if validation:
            capture["validation"] = validation.to_dict()
        return None, capture
    after = _lstat(lookup, dir_fd=dir_fd)
    if (
        after is None
        or _stat_signature(opened_after) != _stat_signature(middle)
        or _stat_signature(after) != _stat_signature(middle)
    ):
        capture["status"] = "unstable"
        if validation:
            capture["validation"] = validation.to_dict()
        return None, capture

    artifact = {
        "kind": kind,
        "role": role,
        "path": str(path),
        "exists": True,
        "bytes": after.st_size,
        "sha256": digest,
    }
    capture["sha256"] = digest
    if validation:
        capture["validation"] = validation.to_dict()
        if after.st_size == 0:
            capture["status"] = "empty"
        else:
            capture["status"] = "valid" if validation.valid else "invalid"
    else:
        capture["status"] = "valid" if after.st_size > 0 else "empty"
    return artifact, capture


def _scan_source(path: Path) -> tuple[set[str], bool, bool, bool, bool, bool, bool, bool]:
    measurements: set[str] = set()
    has_control = False
    has_pure_control = False
    has_transitive_include = False
    too_many_measurements = False
    duplicate_measurement_declaration = False
    invalid_measurement_name = False
    long_line = False
    with path.open("rb") as handle:
        line_number = 0
        while True:
            raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            if len(raw_line) > MAX_SOURCE_LINE_BYTES and not raw_line.endswith(b"\n"):
                long_line = True
                while raw_line and not raw_line.endswith(b"\n"):
                    raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                continue
            line = raw_line.decode("utf-8", errors="replace")
            if line_number == 1 and PURE_CONTROL_SCRIPT_RE.match(line):
                has_pure_control = True
            declaration = MEASUREMENT_DECLARATION_RE.match(line)
            if declaration:
                name = declaration.group(1)
                if len(name) > MAX_MEASUREMENT_NAME_CHARS:
                    invalid_measurement_name = True
                elif name.lower() in measurements:
                    duplicate_measurement_declaration = True
                elif name.lower() not in measurements and len(measurements) >= MAX_MEASUREMENTS:
                    too_many_measurements = True
                else:
                    measurements.add(name.lower())
            if CONTROL_DECLARATION_RE.match(line):
                has_control = True
            if TRANSITIVE_INCLUDE_RE.match(line):
                has_transitive_include = True
    return (
        measurements,
        has_control,
        has_pure_control,
        has_transitive_include,
        too_many_measurements,
        duplicate_measurement_declaration,
        invalid_measurement_name,
        long_line,
    )


def _resolve_expected_outputs(
    values: Sequence[NgspiceOutput],
    *,
    run_dir: Path,
    reserved: set[Path],
) -> tuple[list[_ResolvedOutput], tuple[str, str] | None]:
    if isinstance(values, (str, bytes)):
        return [], ("deck_output.invalid", "Expected outputs must be a sequence of NgspiceOutput records.")
    try:
        value_count = len(values)
    except (TypeError, ValueError, OverflowError):
        return [], ("deck_output.invalid", "Expected outputs must be a bounded sequence.")
    if value_count > MAX_EXPECTED_OUTPUTS:
        return [], (
            "deck_output.invalid",
            f"At most {MAX_EXPECTED_OUTPUTS} deck-owned outputs may be declared.",
        )

    resolved: list[_ResolvedOutput] = []
    seen: set[Path] = set()
    for index, value in enumerate(values):
        if not isinstance(value, NgspiceOutput):
            return [], (
                "deck_output.invalid",
                f"Expected output {index} is not an NgspiceOutput record.",
            )
        if not isinstance(value.kind, str) or value.kind not in OUTPUT_KINDS:
            return [], (
                "deck_output.invalid",
                f"Expected output {index} has an unsupported kind.",
            )
        try:
            declared = os.fspath(value.path)
        except TypeError:
            return [], ("deck_output.invalid", f"Expected output {index} has an invalid path.")
        if not isinstance(declared, str):
            return [], ("deck_output.invalid", f"Expected output {index} has an invalid path.")
        if (
            not declared
            or len(declared) > MAX_OUTPUT_PATH_CHARS
            or "\x00" in declared
            or CONTROL_ARGUMENT_SAFE_RE.fullmatch(declared) is None
        ):
            return [], (
                "deck_output.invalid",
                f"Expected output {index} must use a bounded control-safe relative path.",
            )
        relative = Path(declared)
        if relative.is_absolute() or relative.name in {"", ".", ".."}:
            return [], (
                "deck_output.invalid",
                f"Expected output {declared!r} must be a nonempty path relative to the working directory.",
            )
        if any(part in {"", ".", ".."} for part in relative.parts):
            return [], (
                "deck_output.invalid",
                f"Expected output {declared!r} contains an unsafe path component.",
            )
        if any(len(part) > MAX_OUTPUT_COMPONENT_CHARS for part in relative.parts):
            return [], (
                "deck_output.invalid",
                f"Expected output {declared!r} contains an overlong path component.",
            )
        if any(character in declared for character in "*?[]"):
            return [], (
                "deck_output.invalid",
                f"Expected output {declared!r} must be an exact path, not a glob.",
            )

        parent = run_dir
        for component in relative.parts[:-1]:
            parent = parent / component
            try:
                parent_stat = _lstat(parent)
            except OSError:
                return [], (
                    "deck_output.invalid",
                    f"Expected output parent cannot be inspected safely: {parent}",
                )
            if parent_stat is None or not stat.S_ISDIR(parent_stat.st_mode):
                return [], (
                    "deck_output.invalid",
                    f"Expected output parent is not an existing real directory: {parent}",
                )
        candidate = parent / relative.name
        try:
            candidate.relative_to(run_dir)
        except ValueError:
            return [], (
                "deck_output.invalid",
                f"Expected output escapes the working directory: {declared!r}",
            )
        if candidate in reserved:
            return [], (
                "deck_output.invalid",
                f"Expected output collides with an OpenADA input or output: {declared!r}",
            )
        if candidate in seen:
            return [], ("deck_output.invalid", f"Expected output is declared more than once: {declared!r}")
        try:
            candidate_stat = _lstat(candidate)
        except OSError:
            return [], (
                "deck_output.invalid",
                f"Expected output cannot be inspected safely: {candidate}",
            )
        if candidate_stat is not None:
            return [], (
                "deck_output.not_fresh",
                f"Deck-owned output must not exist before launch: {candidate}",
            )
        seen.add(candidate)
        resolved.append(_ResolvedOutput(value.kind, declared, candidate))
    return resolved, None


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_output_anchor(
    output: _ResolvedOutput,
    *,
    run_dir: Path,
) -> tuple[_OutputAnchor | None, str | None]:
    flags = _directory_open_flags()
    root_fd = -1
    current_fd = -1
    try:
        root_fd = os.open(run_dir, flags)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise OSError("working directory is not a real directory")
        root_path_stat = _lstat(run_dir)
        if root_path_stat is None or _directory_signature(root_path_stat) != _directory_signature(root_stat):
            raise OSError("working-directory identity changed")

        current_fd = os.dup(root_fd)
        signatures: list[tuple[str, tuple[int, int, int]]] = []
        relative = Path(output.declared_path)
        for component in relative.parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            next_stat = os.fstat(next_fd)
            if not stat.S_ISDIR(next_stat.st_mode):
                os.close(next_fd)
                raise OSError(f"output parent component is not a real directory: {component}")
            signatures.append((component, _directory_signature(next_stat)))
            os.close(current_fd)
            current_fd = next_fd

        if _lstat(relative.name, dir_fd=current_fd) is not None:
            raise FileExistsError(f"deck-owned output appeared before launch: {output.path}")
        return (
            _OutputAnchor(
                output=output,
                root_fd=root_fd,
                parent_fd=current_fd,
                root_signature=_directory_signature(root_stat),
                directory_signatures=tuple(signatures),
            ),
            None,
        )
    except OSError as error:
        for descriptor in {root_fd, current_fd}:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return None, str(error)


def _revalidate_output_anchor(anchor: _OutputAnchor, *, run_dir: Path) -> bool:
    flags = _directory_open_flags()
    current_fd = -1
    try:
        root_stat = os.fstat(anchor.root_fd)
        root_path_stat = _lstat(run_dir)
        if (
            root_path_stat is None
            or _directory_signature(root_stat) != anchor.root_signature
            or _directory_signature(root_path_stat) != anchor.root_signature
        ):
            return False
        current_fd = os.dup(anchor.root_fd)
        for component, expected_signature in anchor.directory_signatures:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            next_stat = os.fstat(next_fd)
            os.close(current_fd)
            current_fd = next_fd
            if _directory_signature(next_stat) != expected_signature:
                return False
        return _directory_signature(os.fstat(anchor.parent_fd)) == (
            anchor.directory_signatures[-1][1]
            if anchor.directory_signatures
            else anchor.root_signature
        )
    except OSError:
        return False
    finally:
        if current_fd >= 0:
            try:
                os.close(current_fd)
            except OSError:
                pass


def _safe_control_argument(path: Path, *, cwd: Path) -> str | None:
    absolute = str(path)
    relative = os.path.relpath(path, cwd)
    for candidate in (absolute, relative):
        if not os.path.isabs(candidate) and candidate.startswith("-"):
            candidate = f"./{candidate}"
        if (
            candidate
            and len(candidate) <= MAX_OUTPUT_PATH_CHARS
            and CONTROL_ARGUMENT_SAFE_RE.fullmatch(candidate) is not None
        ):
            return candidate
    return None


def _write_control_script(
    path: Path,
    *,
    source_argument: str,
    init_argument: str | None,
    wrapper_raw_argument: str | None,
    deck_owned_outputs: bool,
) -> None:
    # ngspice 45.2 can segfault when a plain *ng_script sources a nested
    # *ng_script_with_params.  The parameter-capable marker avoids that native
    # bug; OpenADA still embeds every path and never consumes positional argv.
    lines = ["*ng_script_with_params", "set noaskquit"]
    if init_argument is not None:
        lines.append(f"source {init_argument}")
    lines.append(f"source {source_argument}")
    if not deck_owned_outputs:
        if wrapper_raw_argument is None:
            raise ValueError("wrapper raw argument is required for wrapper-owned control mode")
        lines.extend(
            [
                "strcmp __openada_plot $curplot const",
                "if $__openada_plot eq 0",
                "  run",
                f"  write {wrapper_raw_argument}",
                "else",
                f"  write {wrapper_raw_argument}",
                "end",
            ]
        )
    lines.append("quit")
    body = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _move_regular_output(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int = MAX_CAPTURE_BYTES,
) -> bool:
    source_stat = _lstat(source)
    if (
        source_stat is None
        or not stat.S_ISREG(source_stat.st_mode)
        or source_stat.st_size > maximum_bytes
    ):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError:
        return False
    opened_stat = os.fstat(source_fd)
    if (
        not stat.S_ISREG(opened_stat.st_mode)
        or _content_signature(opened_stat) != _content_signature(source_stat)
    ):
        os.close(source_fd)
        return False
    try:
        try:
            os.replace(source, destination)
            destination_stat = _lstat(destination)
            finished_stat = os.fstat(source_fd)
            return (
                destination_stat is not None
                and _content_signature(destination_stat) == _content_signature(opened_stat)
                and _content_signature(finished_stat) == _content_signature(opened_stat)
            )
        except OSError as error:
            if error.errno != errno.EXDEV:
                return False

        descriptor, staged_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            copied = 0
            with os.fdopen(os.dup(source_fd), "rb") as input_handle, os.fdopen(
                descriptor, "wb"
            ) as output_handle:
                while chunk := input_handle.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > maximum_bytes or copied > opened_stat.st_size:
                        raise OSError("source grew during cross-device capture")
                    output_handle.write(chunk)
                if copied != opened_stat.st_size:
                    raise OSError("source size changed during cross-device capture")
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if _content_signature(os.fstat(source_fd)) != _content_signature(opened_stat):
                raise OSError("source changed during cross-device capture")
            os.replace(staged_name, destination)
            current_source = _lstat(source)
            if current_source is not None and _content_signature(current_source) == _content_signature(opened_stat):
                source.unlink()
            return True
        except OSError:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(staged_name)
            except OSError:
                pass
            return False
    finally:
        os.close(source_fd)


def _scan_line(
    line: str,
) -> tuple[str | None, str | None, str | None, bool, bool]:
    # ngspice echoes the untrusted first deck line as ``Circuit: <title>``.
    # A title is presentation text, not solver or analysis evidence, even when
    # it contains strings copied from native diagnostics.
    if CIRCUIT_TITLE_RE.match(line):
        return None, None, None, False, False

    convergence_error = None
    if not re.search(r"failures?\s*[:=]\s*0\b", line, re.IGNORECASE):
        for pattern in TERMINAL_CONVERGENCE_PATTERNS:
            if pattern.search(line):
                convergence_error = line.strip()[:1_000]
                break

    solver_warning = None
    for pattern in RECOVERABLE_SOLVER_PATTERNS:
        if pattern.search(line):
            solver_warning = line.strip()[:1_000]
            break

    native_error = None
    for pattern in NATIVE_ERROR_PATTERNS:
        if pattern.search(line):
            native_error = line.strip()[:1_000]
            break
    if (
        native_error is None
        and GENERIC_NATIVE_ERROR_RE.search(line)
        and not HARMLESS_GRAPHICS_ERROR_RE.search(line)
    ):
        native_error = line.strip()[:1_000]

    measurement_warning = bool(BATCH_MEASUREMENT_WARNING_RE.search(line))
    completed_analysis = any(pattern.search(line) for pattern in ANALYSIS_COMPLETE_PATTERNS)
    return (
        convergence_error,
        solver_warning,
        native_error,
        measurement_warning,
        completed_analysis,
    )


def _parse_measurement_sections(
    log_text: str,
    *,
    expected_names: set[str],
) -> tuple[list[dict], set[str], set[str], int]:
    measurements: list[dict] = []
    invalid_measurements: set[str] = set()
    duplicate_measurements: set[str] = set()
    observed: set[str] = set()
    section_state = 0
    section_count = 0

    for line in log_text.splitlines():
        if MEASUREMENT_SECTION_RE.match(line):
            section_state = 1
            section_count += 1
            continue
        if section_state == 0:
            continue
        if not line.strip():
            if section_state == 2:
                section_state = 0
            continue

        match = MEASUREMENT_RE.match(line)
        nonfinite = NONFINITE_MEASUREMENT_RE.match(line) if match is None else None
        if match is None and nonfinite is None:
            section_state = 0
            continue
        section_state = 2
        name = (match or nonfinite).group(1)
        normalized_name = name.lower()
        if normalized_name not in expected_names:
            continue
        if normalized_name in observed:
            duplicate_measurements.add(normalized_name)
            continue
        observed.add(normalized_name)
        if nonfinite is not None:
            invalid_measurements.add(normalized_name)
            continue
        assert match is not None
        raw_value = match.group(2)
        value = float(raw_value)
        if math.isfinite(value):
            measurements.append({"name": name, "value": value, "raw": raw_value})
        else:
            invalid_measurements.add(normalized_name)

    if duplicate_measurements:
        measurements = [
            item
            for item in measurements
            if item["name"].lower() not in duplicate_measurements
        ]
    return measurements, invalid_measurements, duplicate_measurements, section_count


class NgspiceDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"ngspice": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("ngspice")

    def simulate(
        self,
        spice_file: str | Path,
        output_dir: str | Path,
        *,
        raw_file: str | Path | None = None,
        workdir: str | Path | None = None,
        execution_mode: str = "batch",
        expected_outputs: Sequence[NgspiceOutput] = (),
        init_file: str | Path | None = None,
        system_init_file: str | Path | None = None,
        timeout: float = 120.0,
    ) -> dict:
        source = Path(spice_file).expanduser().resolve()
        out_dir = Path(output_dir).expanduser().resolve()
        run_dir = Path(workdir).expanduser().resolve() if workdir else source.parent
        info = self.discovery.inspect_tool("ngspice")
        tool = tool_record("ngspice", path=self.binary, version=info["version"])
        input_records = [file_record(source, kind="spice-netlist", role="input")]

        base_data = {
            "execution_mode": bounded_text(execution_mode, limit=128),
            "expected_outputs": [],
            "working_directory": str(run_dir),
            "working_directory_is_sandbox": False,
            "transitive_inputs_enumerated": False,
        }
        if not source.is_file():
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE input does not exist.",
                code="input.missing",
                message=f"File not found: {source}",
                data=base_data,
            )
        if not isinstance(execution_mode, str) or execution_mode not in EXECUTION_MODES:
            return _static_invalid(
                tool,
                input_records,
                summary="The ngspice execution mode is invalid.",
                code="execution_mode.invalid",
                message=(
                    f"Execution mode must be one of {sorted(EXECUTION_MODES)}, "
                    f"got {base_data['execution_mode']!r}."
                ),
                data=base_data,
            )
        if not run_dir.is_dir():
            return _static_invalid(
                tool,
                input_records,
                summary="The simulation working directory does not exist or is not a directory.",
                code="workdir.invalid",
                message=f"Working directory is not a directory: {run_dir}",
                data=base_data,
            )

        init_path = Path(init_file).expanduser().resolve() if init_file is not None else None
        system_init_path = (
            Path(system_init_file).expanduser().resolve()
            if system_init_file is not None
            else None
        )
        if init_path is not None:
            input_records.append(file_record(init_path, kind="ngspice-init", role="configuration"))
            if execution_mode != "control":
                return _static_invalid(
                    tool,
                    input_records,
                    summary="An explicit ngspice init file requires control mode.",
                    code="execution_mode.invalid",
                    message="Use --execution-mode control with --init-file.",
                    data=base_data,
                )
            if not init_path.is_file():
                return _static_invalid(
                    tool,
                    input_records,
                    summary="The explicit ngspice init file does not exist.",
                    code="init_file.invalid",
                    message=f"Init file not found: {init_path}",
                    data=base_data,
                )

        if system_init_path is not None:
            input_records.append(
                file_record(system_init_path, kind="ngspice-system-init", role="configuration")
            )
            if execution_mode != "control":
                return _static_invalid(
                    tool,
                    input_records,
                    summary="An explicit ngspice system init file requires control mode.",
                    code="execution_mode.invalid",
                    message="Use --execution-mode control with --system-init-file.",
                    data=base_data,
                )
            if not system_init_path.is_file() or system_init_path.name != "spinit":
                return _static_invalid(
                    tool,
                    input_records,
                    summary="The explicit ngspice system init file is invalid.",
                    code="system_init_file.invalid",
                    message=(
                        "The system init must be a readable regular file named 'spinit': "
                        f"{system_init_path}"
                    ),
                    data=base_data,
                )
        if execution_mode == "batch" and expected_outputs:
            return _static_invalid(
                tool,
                input_records,
                summary="Deck-owned outputs require ngspice control mode.",
                code="execution_mode.invalid",
                message="Use --execution-mode control when declaring --expect-output.",
                data=base_data,
            )
        if execution_mode == "control" and expected_outputs and raw_file is not None:
            return _static_invalid(
                tool,
                input_records,
                summary="Wrapper-owned and deck-owned raw outputs cannot be requested together.",
                code="deck_output.invalid",
                message="Omit --raw-file when the deck owns explicitly declared outputs.",
                data=base_data,
            )

        requested_raw_path = Path(raw_file).expanduser() if raw_file is not None else None
        if requested_raw_path is not None and requested_raw_path.is_symlink():
            return _static_invalid(
                tool,
                input_records,
                summary="The wrapper-owned raw output path is unsafe.",
                code="output.invalid",
                message=f"The raw output path may not be a symbolic link: {requested_raw_path}",
                data=base_data,
            )
        raw_path = (
            requested_raw_path.resolve()
            if requested_raw_path is not None
            else out_dir / f"{source.stem}.raw"
        )
        log_path = out_dir / f"{source.stem}.log"
        control_script_path = out_dir / f"{source.stem}.openada-control.sp"
        wrapper_raw_required = not expected_outputs
        reserved = {source, log_path}
        if wrapper_raw_required:
            reserved.add(raw_path)
        if execution_mode == "control":
            reserved.add(control_script_path)
        if init_path is not None:
            reserved.add(init_path)
        if system_init_path is not None:
            reserved.add(system_init_path)

        output_paths = [log_path]
        if wrapper_raw_required:
            output_paths.append(raw_path)
        if execution_mode == "control":
            output_paths.append(control_script_path)
        input_paths = {source}
        if init_path is not None:
            input_paths.add(init_path)
        if system_init_path is not None:
            input_paths.add(system_init_path)
        invalid_output = (
            out_dir.is_file()
            or len(set(output_paths)) != len(output_paths)
            or any(path in input_paths for path in output_paths)
            or any(len(path.name) > MAX_OUTPUT_COMPONENT_CHARS for path in output_paths)
            or any(path.exists() and path.is_dir() for path in output_paths)
        )
        if invalid_output:
            return _static_invalid(
                tool,
                input_records,
                summary="Simulation outputs must be distinct files and must not overwrite an input.",
                code="output.invalid",
                message="Choose a writable output directory and output paths distinct from every input.",
                data=base_data,
            )

        resolved_outputs, output_error = _resolve_expected_outputs(
            expected_outputs,
            run_dir=run_dir,
            reserved=reserved,
        )
        base_data["expected_outputs"] = [
            {
                "kind": item.kind,
                "declared_path": item.declared_path,
                "path": str(item.path),
            }
            for item in resolved_outputs
        ]
        if output_error:
            code, message = output_error
            return _static_invalid(
                tool,
                input_records,
                summary="A deck-owned output declaration is invalid.",
                code=code,
                message=message,
                data=base_data,
            )

        (
            declared_measurements,
            has_control,
            has_pure_control,
            has_transitive_include,
            too_many_measurements,
            duplicate_measurement_declaration,
            invalid_measurement_name,
            long_source_line,
        ) = _scan_source(source)
        base_data["transitive_include_detected"] = has_transitive_include
        if invalid_measurement_name:
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE deck contains an invalid measurement identifier.",
                code="measurement.name_invalid",
                message=(
                    "Top-level .measure identifiers are limited to "
                    f"{MAX_MEASUREMENT_NAME_CHARS} characters."
                ),
                data=base_data,
            )
        if duplicate_measurement_declaration:
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE deck declares a measurement identifier more than once.",
                code="measurement.duplicate",
                message="Top-level .measure identifiers must be unique for unambiguous normalization.",
                data=base_data,
            )
        if too_many_measurements:
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE deck declares too many measurements for bounded normalization.",
                code="measurement.too_many",
                message=f"At most {MAX_MEASUREMENTS} distinct top-level .measure names are supported.",
                data=base_data,
            )
        if execution_mode == "batch" and (
            declared_measurements or has_control or has_pure_control
        ):
            features = []
            if declared_measurements:
                features.append(".measure")
            if has_control:
                features.append(".control")
            if has_pure_control:
                features.append("pure ngspice control script")
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE deck requires explicit ngspice control-mode semantics.",
                code="execution_mode.invalid",
                message="Batch streaming does not safely support " + " and ".join(features) + ".",
                hint="Use --execution-mode control; declare every deck-owned write/wrdata file with --expect-output.",
                data=base_data,
            )
        if execution_mode == "batch" and has_transitive_include:
            return _static_invalid(
                tool,
                input_records,
                summary="Batch safety cannot be proven across unenumerated transitive includes.",
                code="input.transitive_uninspected",
                message=(
                    "The top-level deck contains .include/.inc/.lib, but this preview does not "
                    "recursively attest included files against control-only directives."
                ),
                hint="Use control mode, or flatten and review the batch deck before execution.",
                data=base_data,
            )
        if long_source_line:
            return _static_invalid(
                tool,
                input_records,
                summary="The SPICE deck cannot be safely inspected for control directives.",
                code="input.line_too_long",
                message=(
                    f"At least one source line exceeds the {MAX_SOURCE_LINE_BYTES}-byte inspection bound. "
                    "Shorten the line before execution."
                ),
                data=base_data,
            )

        control_source_argument = None
        control_init_argument = None
        if execution_mode == "control":
            control_source_argument = _safe_control_argument(source, cwd=run_dir)
            control_init_argument = (
                _safe_control_argument(init_path, cwd=run_dir)
                if init_path is not None
                else None
            )
            if control_source_argument is None or (
                init_path is not None and control_init_argument is None
            ):
                return _static_invalid(
                    tool,
                    input_records,
                    summary="A path cannot be represented safely in ngspice control-script arguments.",
                    code="control_path.unsupported",
                    message=(
                        "ngspice splits or misparses control-script paths with whitespace, "
                        "non-ASCII characters, or control metacharacters. Use a control-safe "
                        "source and init path."
                    ),
                    data=base_data,
                )

        if not self.binary:
            return result(
                "simulate",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="ngspice is not available in the selected runtime.",
                inputs=input_records,
                diagnostics=[
                    diagnostic(
                        "error",
                        "tool.missing",
                        "ngspice was not found.",
                        hint="Install ngspice, add it to PATH, or select the IIC-OSIC-TOOLS profile.",
                    )
                ],
                data=base_data,
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        if wrapper_raw_required:
            raw_path.parent.mkdir(parents=True, exist_ok=True)

        script_artifact = None
        script_capture = None
        initial_script_sha256 = None
        startup_is_explicit = init_path is not None or system_init_path is not None
        initialization = {
            "policy": "explicit" if startup_is_explicit else "native-default",
            "file": str(init_path) if init_path is not None else None,
            "local_user_spiceinit": "disabled" if startup_is_explicit else "native-default",
            "system_spinit": {
                "policy": "explicit" if system_init_path is not None else "native-default-unenumerated",
                "file": str(system_init_path) if system_init_path is not None else None,
            },
            "ambient_startup_files_enumerated": system_init_path is not None,
        }
        base_data["initialization"] = initialization
        process_environment = None
        if system_init_path is not None:
            process_environment = dict(os.environ)
            process_environment["SPICE_SCRIPTS"] = str(system_init_path.parent)
        effective_environment = process_environment or os.environ
        base_data["environment"] = {
            name: (
                bounded_text(effective_environment[name], limit=1_024)
                if name in effective_environment
                else None
            )
            for name in (
                "PDK",
                "PDK_ROOT",
                "SPICE_ASCIIRAWFILE",
                "SPICE_LIB_DIR",
                "SPICE_SCRIPTS",
                "NGSPICE_INPUT_DIR",
            )
        }
        base_data["environment_overrides"] = (
            {"SPICE_SCRIPTS": str(system_init_path.parent)}
            if system_init_path is not None
            else {}
        )

        output_anchors: list[_OutputAnchor] = []
        with tempfile.TemporaryDirectory(prefix="openada-ngspice-") as temp_dir:
            temp_root = Path(temp_dir)
            temp_raw = temp_root / "simulation.raw"
            temp_log = temp_root / "simulation.log"
            if execution_mode == "batch":
                command = [
                    self.binary,
                    "-b",
                    "-r",
                    str(temp_raw),
                    "-o",
                    str(temp_log),
                    str(source),
                ]
            else:
                source_argument = control_source_argument
                init_argument = control_init_argument
                raw_argument = (
                    _safe_control_argument(temp_raw, cwd=run_dir) if wrapper_raw_required else None
                )
                if source_argument is None or (init_path is not None and init_argument is None) or (
                    wrapper_raw_required and raw_argument is None
                ):
                    return _static_invalid(
                        tool,
                        input_records,
                        summary="A path cannot be represented safely in ngspice control-script arguments.",
                        code="control_path.unsupported",
                        message=(
                            "ngspice cannot represent one of the generated control-script paths "
                            "safely. Select a control-safe temporary directory."
                        ),
                        data=base_data,
                    )
                assert source_argument is not None
                _write_control_script(
                    control_script_path,
                    source_argument=source_argument,
                    init_argument=init_argument,
                    wrapper_raw_argument=raw_argument,
                    deck_owned_outputs=bool(resolved_outputs),
                )
                _, initial_script_capture = _capture_file(
                    control_script_path,
                    kind="ngspice-control-script",
                    role="evidence",
                    maximum_bytes=MAX_LOG_BYTES,
                )
                initial_script_sha256 = initial_script_capture.get("sha256")
                command = [self.binary, "-i"]
                if startup_is_explicit:
                    command.append("-n")
                command.extend(["-o", str(temp_log), str(control_script_path)])

            if resolved_outputs:
                rechecked_outputs, recheck_error = _resolve_expected_outputs(
                    expected_outputs,
                    run_dir=run_dir,
                    reserved=reserved,
                )
                if recheck_error:
                    code, message = recheck_error
                    return _static_invalid(
                        tool,
                        input_records,
                        summary="A deck-owned output path changed before ngspice launch.",
                        code=code,
                        message=message,
                        data=base_data,
                    )
                resolved_outputs = rechecked_outputs
                for expected in resolved_outputs:
                    anchor, anchor_error = _open_output_anchor(expected, run_dir=run_dir)
                    if anchor is None:
                        for opened_anchor in output_anchors:
                            opened_anchor.close()
                        return _static_invalid(
                            tool,
                            input_records,
                            summary="A deck-owned output path could not be anchored before launch.",
                            code="deck_output.anchor_failed",
                            message=anchor_error or f"Cannot anchor {expected.declared_path!r}.",
                            data=base_data,
                        )
                    output_anchors.append(anchor)
            process = run_process(
                command,
                cwd=run_dir,
                timeout=timeout,
                env=process_environment,
            )
            produced_log = _move_regular_output(
                temp_log,
                log_path,
                maximum_bytes=MAX_LOG_BYTES,
            )
            produced_raw = False
            if wrapper_raw_required:
                produced_raw = _move_regular_output(temp_raw, raw_path)

        if execution_mode == "control":
            script_artifact, script_capture = _capture_file(
                control_script_path,
                kind="ngspice-control-script",
                role="evidence",
                maximum_bytes=MAX_LOG_BYTES,
            )
            if (
                initial_script_sha256 is None
                or script_capture.get("sha256") != initial_script_sha256
            ):
                script_capture["status"] = "modified"
                script_capture["expected_sha256"] = initial_script_sha256

        log_artifact = None
        log_capture = {"path": str(log_path), "status": "missing"}
        if produced_log:
            log_artifact, log_capture = _capture_file(
                log_path,
                kind="simulation-log",
                role="evidence",
                maximum_bytes=MAX_LOG_BYTES,
            )

        wrapper_artifact = None
        wrapper_capture = None
        if wrapper_raw_required:
            wrapper_capture = {"path": str(raw_path), "status": "missing"}
            if produced_raw:
                wrapper_artifact, wrapper_capture = _capture_file(
                    raw_path,
                    kind="ngspice-raw",
                    role="output",
                    validator=validate_ngspice_raw,
                )
            wrapper_capture["kind"] = "raw"
            wrapper_capture["origin"] = "wrapper"

        deck_captures: list[dict] = []
        deck_artifacts: list[dict] = []
        try:
            for anchor in output_anchors:
                expected = anchor.output
                artifact_kind, validator = OUTPUT_KINDS[expected.kind]
                if not _revalidate_output_anchor(anchor, run_dir=run_dir):
                    artifact = None
                    capture = {"path": str(expected.path), "status": "parent_changed"}
                else:
                    artifact, capture = _capture_file(
                        expected.path,
                        kind=artifact_kind,
                        role="output",
                        validator=validator,
                        dir_fd=anchor.parent_fd,
                        capture_name=Path(expected.declared_path).name,
                        require_single_link=True,
                    )
                capture.update(
                    {
                        "kind": expected.kind,
                        "origin": "deck",
                        "declared_path": expected.declared_path,
                        "parent_anchored": True,
                    }
                )
                deck_captures.append(capture)
                if artifact:
                    deck_artifacts.append(artifact)
        finally:
            for anchor in output_anchors:
                anchor.close()

        changed_inputs: list[str] = []
        for input_record in input_records:
            current_record = file_record(
                input_record["path"],
                kind=input_record["kind"],
                role=input_record["role"],
            )
            if any(
                current_record.get(field) != input_record.get(field)
                for field in ("exists", "bytes", "sha256")
            ):
                changed_inputs.append(input_record["path"])
        inputs_stable = not changed_inputs

        convergence_error = None
        solver_warnings: list[str] = []
        solver_warning_examples_seen: set[str] = set()
        solver_warning_count = 0
        native_error = None
        batch_measurement_warning = False
        completed_analysis_in_log = False
        log_text = ""
        if log_capture["status"] == "valid":
            captured_log_text = _read_captured_text(
                log_path,
                log_capture,
                maximum_bytes=MAX_LOG_BYTES,
            )
            if captured_log_text is None:
                log_capture["status"] = "unstable"
                log_artifact = None
            else:
                log_text = captured_log_text
        for captured in (process.stdout, process.stderr, log_text):
            for line in captured.splitlines():
                (
                    line_convergence,
                    line_solver_warning,
                    line_error,
                    line_warning,
                    line_analysis,
                ) = _scan_line(line)
                convergence_error = convergence_error or line_convergence
                if line_solver_warning:
                    solver_warning_count += 1
                    if (
                        len(solver_warnings) < MAX_SOLVER_WARNING_EXAMPLES
                        and line_solver_warning not in solver_warning_examples_seen
                    ):
                        solver_warning_examples_seen.add(line_solver_warning)
                        solver_warnings.append(line_solver_warning)
                native_error = native_error or line_error
                batch_measurement_warning = batch_measurement_warning or line_warning
                completed_analysis_in_log = completed_analysis_in_log or line_analysis

        (
            measurements,
            invalid_measurements,
            duplicate_measurements,
            measurement_section_count,
        ) = _parse_measurement_sections(
            log_text,
            expected_names=declared_measurements,
        )
        observed_measurement_names = {item["name"].lower() for item in measurements}
        missing_measurements = sorted(declared_measurements - observed_measurement_names)
        valid_log = log_capture["status"] == "valid"
        valid_wrapper = bool(wrapper_capture and wrapper_capture["status"] == "valid")
        valid_deck_outputs = all(item["status"] == "valid" for item in deck_captures)
        raw_analysis_evidence = valid_wrapper or any(
            item["status"] == "valid" and item["kind"] == "raw"
            for item in deck_captures
        )
        enough_analysis_evidence = raw_analysis_evidence or completed_analysis_in_log
        completed = process.status == "completed"
        passed = (
            completed
            and process.exit_code == 0
            and convergence_error is None
            and native_error is None
            and not batch_measurement_warning
            and valid_log
            and (valid_wrapper if wrapper_raw_required else valid_deck_outputs)
            and enough_analysis_evidence
            and not missing_measurements
            and not invalid_measurements
            and not duplicate_measurements
            and inputs_stable
            and (script_capture is None or script_capture["status"] == "valid")
        )

        diagnostics: list[dict] = []
        if process.status != "completed":
            diagnostics.append(
                diagnostic(
                    "error",
                    f"execution.{process.status}",
                    process.error or "ngspice did not complete.",
                )
            )
        elif process.exit_code != 0:
            diagnostics.append(
                diagnostic("error", "ngspice.nonzero_exit", f"ngspice exited with code {process.exit_code}.")
            )
        if convergence_error:
            diagnostics.append(diagnostic("error", "simulation.nonconvergent", convergence_error))
        elif solver_warnings:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "simulation.solver_recovered",
                    "ngspice emitted solver warning(s), but retained analysis evidence does not "
                    "establish a terminal convergence failure: " + solver_warnings[0],
                )
            )
        if native_error:
            diagnostics.append(diagnostic("error", "simulation.native_error", native_error))
        if batch_measurement_warning:
            diagnostics.append(
                diagnostic(
                    "error",
                    "measurement.unavailable",
                    "ngspice suppressed .measure processing in streaming batch mode.",
                    hint="Use --execution-mode control for decks that obtain or include measurements.",
                )
            )
        if not valid_log:
            log_code = {
                "missing": "artifact.missing",
                "empty": "artifact.empty",
            }.get(log_capture["status"], "artifact.invalid")
            diagnostics.append(
                diagnostic(
                    "error",
                    log_code,
                    f"The simulation log is not valid current-run evidence ({log_capture['status']}).",
                )
            )
        if wrapper_capture and not valid_wrapper:
            wrapper_code = {
                "missing": "artifact.missing",
                "empty": "artifact.empty",
            }.get(wrapper_capture["status"], "artifact.invalid")
            diagnostics.append(
                diagnostic(
                    "error",
                    wrapper_code,
                    f"The wrapper-owned raw output is not valid current-run evidence ({wrapper_capture['status']}).",
                )
            )
        if script_capture and script_capture["status"] != "valid":
            diagnostics.append(
                diagnostic(
                    "error",
                    "artifact.invalid",
                    "The generated ngspice control launcher changed or became invalid during execution "
                    f"({script_capture['status']}).",
                )
            )
        for capture in deck_captures:
            if capture["status"] != "valid":
                output_code = {
                    "missing": "artifact.missing",
                    "empty": "artifact.empty",
                }.get(capture["status"], "artifact.invalid")
                diagnostics.append(
                    diagnostic(
                        "error",
                        output_code,
                        f"Required deck-owned {capture['kind']} output {capture['declared_path']!r} "
                        f"is not valid current-run evidence ({capture['status']}).",
                    )
                )
        if missing_measurements:
            diagnostics.append(
                diagnostic(
                    "error",
                    "measurement.missing",
                    "ngspice did not emit finite values for declared measurement(s): "
                    + ", ".join(missing_measurements[:MAX_MEASUREMENTS]),
                )
            )
        if invalid_measurements:
            diagnostics.append(
                diagnostic(
                    "error",
                    "measurement.nonfinite",
                    "ngspice emitted non-finite measurement value(s): "
                    + ", ".join(sorted(invalid_measurements)),
                )
            )
        if duplicate_measurements:
            diagnostics.append(
                diagnostic(
                    "error",
                    "measurement.ambiguous",
                    "ngspice emitted a declared measurement more than once: "
                    + ", ".join(sorted(duplicate_measurements)),
                )
            )
        if changed_inputs:
            diagnostics.append(
                diagnostic(
                    "error",
                    "input.changed",
                    "Declared simulation input(s) changed during execution: "
                    + ", ".join(changed_inputs),
                )
            )
        if not enough_analysis_evidence:
            diagnostics.append(
                diagnostic(
                    "error",
                    "simulation.analysis_unproven",
                    "No structurally valid analysis raw file or completed-analysis log record was captured.",
                )
            )
        if long_source_line:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "input.line_too_long",
                    f"At least one source line exceeded {MAX_SOURCE_LINE_BYTES} bytes and was not inspected for directives.",
                )
            )

        artifacts = []
        if log_artifact:
            artifacts.append(log_artifact)
        if script_artifact:
            artifacts.append(script_artifact)
        if wrapper_artifact:
            artifacts.append(wrapper_artifact)
        artifacts.extend(deck_artifacts)

        if convergence_error and inputs_stable:
            engineering_status = "fail"
            summary = "ngspice reported a convergence failure."
        elif passed:
            engineering_status = "pass"
            summary = "ngspice produced all required, structurally valid simulation evidence."
        else:
            engineering_status = "unknown"
            summary = "The simulation did not yield enough trustworthy evidence for an engineering conclusion."

        output_captures = []
        if wrapper_capture:
            output_captures.append(wrapper_capture)
        output_captures.extend(deck_captures)
        data = {
            **base_data,
            "converged": (
                False
                if convergence_error and inputs_stable
                else (True if passed else None)
            ),
            "measurements": measurements[:MAX_MEASUREMENTS],
            "measurements_truncated": len(measurements) > MAX_MEASUREMENTS,
            "missing_measurements": missing_measurements,
            "duplicate_measurements": sorted(duplicate_measurements),
            "measurement_section_count": measurement_section_count,
            "solver_warning_count": solver_warning_count,
            "solver_warning_examples": solver_warnings,
            "solver_warning_examples_truncated": solver_warning_count > len(solver_warnings),
            "log_tail": _tail(log_text or "\n".join((process.stdout, process.stderr))),
            "log_capture": log_capture,
            "output_captures": output_captures,
            "control_script_capture": script_capture,
            "analysis_evidence": {
                "raw": raw_analysis_evidence,
                "completed_log_record": completed_analysis_in_log,
            },
            "inputs_stable": inputs_stable,
        }
        return result(
            "simulate",
            tool=tool,
            execution=process,
            engineering_status=engineering_status,
            summary=summary,
            inputs=input_records,
            artifacts=artifacts,
            diagnostics=diagnostics,
            data=data,
        )


SpiceEngine = NgspiceDriver


__all__ = ["NgspiceDriver", "NgspiceOutput", "SpiceEngine"]
