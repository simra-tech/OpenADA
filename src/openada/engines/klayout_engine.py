"""KLayout batch DRC driver with explicit deck-owned report semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from ..contract import bounded_text, diagnostic, file_record, result, static_execution, tool_record
from ..discovery import DiscoveryManager
from ..process import MAX_CAPTURE_CHARS, ProcessResult, run_process
from .klayout_outputs import (
    MAX_REPORT_BYTES,
    parse_geometry,
    parse_lyrdb,
    parse_lyrdb_stream,
)


MAX_PROVENANCE_INPUTS = 128
MAX_DECK_VARIABLES = 64
MAX_VARIABLE_NAME_CHARS = 64
MAX_VARIABLE_VALUE_CHARS = 4_096
MAX_PATH_CHARS = 4_096
MAX_PATH_COMPONENT_CHARS = 255
MAX_TOP_CELL_CHARS = 1_024
VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RELEVANT_ENVIRONMENT = ("PDK", "PDK_ROOT", "KLAYOUT_PATH", "KLAYOUT_HOME")


@dataclass(slots=True)
class _OutputAnchor:
    report_path: Path
    report_name: str
    transcript_path: Path
    transcript_name: str
    parent_path: Path
    parent_fd: int
    signatures: tuple[tuple[str, tuple[int, int, int]], ...]

    def close(self) -> None:
        try:
            os.close(self.parent_fd)
        except OSError:
            pass


@dataclass(slots=True)
class _AnchoredInput:
    path: Path
    name: str
    descriptor: int
    signature: tuple[int, int, int, int, int, int]
    record: dict[str, Any]

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            pass


def _lstat(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result | None:
    try:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _directory_signature(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _lexical_absolute(path: str | Path, *, base: Path | None = None) -> Path:
    value = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(value):
        value = os.path.join(os.fspath(base or Path.cwd()), value)
    return Path(os.path.abspath(value))


def _open_real_directory(
    path: Path,
    *,
    create_missing: bool,
) -> tuple[int, tuple[tuple[str, tuple[int, int, int]], ...]]:
    absolute = _lexical_absolute(path)
    if not absolute.is_absolute():
        raise OSError("directory path is not absolute")
    flags = _directory_flags()
    current_fd = os.open(os.path.sep, flags)
    signatures: list[tuple[str, tuple[int, int, int]]] = []
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(component, flags, dir_fd=current_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise NotADirectoryError(f"output parent component is not a directory: {component}")
            signatures.append((component, _directory_signature(metadata)))
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, tuple(signatures)
    except Exception:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise


def _revalidate_anchor(anchor: _OutputAnchor) -> bool:
    try:
        descriptor, signatures = _open_real_directory(
            anchor.parent_path,
            create_missing=False,
        )
    except OSError:
        return False
    try:
        return (
            signatures == anchor.signatures
            and _directory_signature(os.fstat(descriptor))
            == _directory_signature(os.fstat(anchor.parent_fd))
        )
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _safe_output_name(path: Path) -> bool:
    name = path.name
    try:
        encoded_names = [
            os.fsencode(name),
            os.fsencode(name + ".w"),
            os.fsencode(name + ".openada.log"),
        ]
        encoded_paths = [
            os.fsencode(str(path)),
            os.fsencode(str(path.with_name(name + ".w"))),
            os.fsencode(str(path.with_name(name + ".openada.log"))),
        ]
    except (TypeError, UnicodeEncodeError):
        return False
    return bool(
        name
        and name not in {".", ".."}
        and "\x00" not in name
        and all(len(value) <= MAX_PATH_COMPONENT_CHARS for value in encoded_names)
        and all(len(value) < MAX_PATH_CHARS for value in encoded_paths)
    )


def _filesystem_output_names_safe(
    anchor: _OutputAnchor,
) -> tuple[bool, str | None]:
    """Validate all derived names against the anchored filesystem's real limits."""

    try:
        name_max = os.fpathconf(anchor.parent_fd, "PC_NAME_MAX")
        path_max = os.fpathconf(anchor.parent_fd, "PC_PATH_MAX")
        encoded_names = (
            os.fsencode(anchor.report_name),
            os.fsencode(anchor.report_name + ".w"),
            os.fsencode(anchor.transcript_name),
        )
        encoded_paths = (
            os.fsencode(anchor.report_path),
            os.fsencode(anchor.report_path.with_name(anchor.report_name + ".w")),
            os.fsencode(anchor.transcript_path),
        )
    except (OSError, ValueError, TypeError, UnicodeEncodeError) as exc:
        return False, f"Cannot determine the anchored output filesystem limits: {exc}"
    if name_max >= 0 and any(len(value) > name_max for value in encoded_names):
        return False, (
            "The report path or a derived .w/.openada.log sidecar exceeds the "
            f"anchored filesystem's {name_max}-byte name limit."
        )
    # POSIX PATH_MAX includes the terminating null byte; keep one byte in reserve.
    if path_max >= 0 and any(len(value) >= path_max for value in encoded_paths):
        return False, (
            "The report path or a derived .w/.openada.log sidecar exceeds the "
            f"anchored filesystem's {path_max}-byte path limit."
        )
    return True, None


def _open_output_anchor(
    report_path: Path,
    *,
    create_parent: bool,
) -> tuple[_OutputAnchor | None, tuple[str, str] | None]:
    if not _safe_output_name(report_path):
        return None, (
            "deck_output.invalid",
            "The report path or a derived .w/.openada.log sidecar name is empty or overlong.",
        )
    transcript_path = report_path.with_name(report_path.name + ".openada.log")
    try:
        parent_fd, signatures = _open_real_directory(
            report_path.parent,
            create_missing=create_parent,
        )
    except OSError as exc:
        return None, (
            "deck_output.anchor_failed",
            f"Cannot anchor the report parent directory: {exc}",
        )
    anchor = _OutputAnchor(
        report_path=report_path,
        report_name=report_path.name,
        transcript_path=transcript_path,
        transcript_name=transcript_path.name,
        parent_path=report_path.parent,
        parent_fd=parent_fd,
        signatures=signatures,
    )
    names_safe, names_error = _filesystem_output_names_safe(anchor)
    if not names_safe:
        anchor.close()
        return None, (
            "deck_output.invalid",
            names_error or "The anchored output names exceed their filesystem limits.",
        )
    try:
        report_metadata = _lstat(anchor.report_name, dir_fd=anchor.parent_fd)
        transcript_metadata = _lstat(anchor.transcript_name, dir_fd=anchor.parent_fd)
    except OSError as exc:
        anchor.close()
        return None, (
            "deck_output.anchor_failed",
            f"Cannot inspect the anchored report outputs: {exc}",
        )
    if report_metadata is not None:
        anchor.close()
        return None, (
            "deck_output.not_fresh",
            f"The deck-owned report must not exist before launch: {report_path}",
        )
    if transcript_metadata is not None:
        anchor.close()
        return None, (
            "deck_output.not_fresh",
            f"The OpenADA transcript must not exist before launch: {transcript_path}",
        )
    return anchor, None


def _anchor_is_fresh(anchor: _OutputAnchor) -> bool:
    try:
        return bool(
            _revalidate_anchor(anchor)
            and _lstat(anchor.report_name, dir_fd=anchor.parent_fd) is None
            and _lstat(anchor.transcript_name, dir_fd=anchor.parent_fd) is None
        )
    except OSError:
        return False


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_anchored_input(
    anchor: _OutputAnchor,
    *,
    path: Path,
    name: str,
    kind: str,
    role: str,
) -> tuple[_AnchoredInput | None, tuple[str, str] | None]:
    """Open and hash an exact sibling input through the retained parent fd."""

    try:
        if not _revalidate_anchor(anchor):
            return None, (
                "waiver.unstable",
                "The report parent changed while opening the waiver database.",
            )
        before = _lstat(name, dir_fd=anchor.parent_fd)
    except OSError as exc:
        return None, ("waiver.unreadable", f"Cannot inspect the waiver database: {exc}")
    if before is None:
        return None, ("waiver.invalid", "The explicit waiver database does not exist.")
    if not stat.S_ISREG(before.st_mode):
        return None, (
            "waiver.invalid",
            "The explicit waiver database must be a regular, non-symlink file.",
        )
    if before.st_nlink != 1:
        return None, (
            "waiver.invalid",
            "The explicit waiver database must have exactly one hard link.",
        )
    if before.st_size > MAX_REPORT_BYTES:
        return None, (
            "waiver.invalid",
            f"The explicit waiver database exceeds {MAX_REPORT_BYTES} bytes.",
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=anchor.parent_fd)
    except OSError as exc:
        return None, ("waiver.unreadable", f"Cannot open the waiver database: {exc}")
    snapshot: _AnchoredInput | None = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_signature(opened) != _file_signature(before)
        ):
            return None, (
                "waiver.unstable",
                "The waiver database identity changed while it was opened.",
            )
        digest = _hash_descriptor(descriptor)
        finished = os.fstat(descriptor)
        current = _lstat(name, dir_fd=anchor.parent_fd)
        if (
            current is None
            or finished.st_nlink != 1
            or current.st_nlink != 1
            or _file_signature(opened) != _file_signature(finished)
            or _file_signature(opened) != _file_signature(current)
            or not _revalidate_anchor(anchor)
        ):
            return None, (
                "waiver.unstable",
                "The waiver database changed while it was hashed.",
            )
        record = {
            "kind": kind,
            "role": role,
            "path": str(path),
            "exists": True,
            "bytes": finished.st_size,
            "sha256": digest,
        }
        snapshot = _AnchoredInput(
            path=path,
            name=name,
            descriptor=descriptor,
            signature=_file_signature(opened),
            record=record,
        )
        return snapshot, None
    except OSError as exc:
        return None, ("waiver.unreadable", f"Cannot hash the waiver database: {exc}")
    finally:
        # Ownership transfers to the returned snapshot only on success.
        if snapshot is None:
            os.close(descriptor)


def _anchored_input_is_stable(anchor: _OutputAnchor, value: _AnchoredInput) -> bool:
    try:
        opened = os.fstat(value.descriptor)
        current = _lstat(value.name, dir_fd=anchor.parent_fd)
        return bool(
            current is not None
            and opened.st_nlink == 1
            and current.st_nlink == 1
            and _file_signature(opened) == value.signature
            and _file_signature(current) == value.signature
            and _hash_descriptor(value.descriptor) == value.record["sha256"]
            and _revalidate_anchor(anchor)
        )
    except (OSError, KeyError):
        return False


def _capture_report(
    anchor: _OutputAnchor,
    *,
    deck: Path,
    top_cell: str | None,
) -> tuple[dict | None, dict, dict | None]:
    capture: dict[str, Any] = {
        "path": str(anchor.report_path),
        "origin": "deck",
        "parent_anchored": True,
        "status": "missing",
    }
    if not _revalidate_anchor(anchor):
        capture["status"] = "parent_changed"
        return None, capture, None
    try:
        before = _lstat(anchor.report_name, dir_fd=anchor.parent_fd)
    except OSError:
        capture["status"] = "unreadable"
        return None, capture, None
    if before is None:
        return None, capture, None
    capture["bytes"] = before.st_size
    if not stat.S_ISREG(before.st_mode):
        capture["status"] = "not_regular"
        return None, capture, None
    if before.st_nlink != 1:
        capture.update({"status": "hardlinked", "link_count": before.st_nlink})
        return None, capture, None
    if before.st_size > MAX_REPORT_BYTES:
        capture["status"] = "too_large"
        return None, capture, None

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(anchor.report_name, flags, dir_fd=anchor.parent_fd)
    except OSError:
        capture["status"] = "unreadable"
        return None, capture, None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_signature(opened) != _file_signature(before)
        ):
            capture["status"] = "unstable"
            return None, capture, None
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            parsed = parse_lyrdb_stream(
                handle,
                expected_deck=deck,
                expected_top_cell=top_cell,
                size=opened.st_size,
            )
        digest = _hash_descriptor(descriptor)
        finished = os.fstat(descriptor)
        current = _lstat(anchor.report_name, dir_fd=anchor.parent_fd)
        if (
            current is None
            or _file_signature(opened) != _file_signature(finished)
            or _file_signature(opened) != _file_signature(current)
            or not _revalidate_anchor(anchor)
        ):
            capture["status"] = "unstable"
            return None, capture, parsed
        artifact = {
            "kind": "klayout-lyrdb",
            "role": "evidence",
            "path": str(anchor.report_path),
            "exists": True,
            "bytes": finished.st_size,
            "sha256": digest,
        }
        capture["sha256"] = digest
        capture["status"] = (
            "valid" if parsed.get("validation", {}).get("valid") is True else "invalid"
        )
        capture["validation"] = parsed.get("validation")
        return artifact, capture, parsed
    except OSError:
        capture["status"] = "unreadable"
        return None, capture, None
    finally:
        os.close(descriptor)


def _transcript_tail(value: str) -> tuple[str, bytes]:
    """Return a valid UTF-8 suffix within the process capture's byte ceiling."""

    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURE_CHARS:
        return value, encoded
    suffix = encoded[-MAX_CAPTURE_CHARS:]
    # The suffix can begin inside one multi-byte code point.  At most three
    # leading continuation bytes need to be discarded for valid Python text.
    for offset in range(4):
        try:
            retained = suffix[offset:].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        return retained, suffix[offset:]
    return "", b""


def _transcript_bytes(process: ProcessResult) -> bytes:
    stdout_text, stdout_tail = _transcript_tail(process.stdout)
    stderr_text, stderr_tail = _transcript_tail(process.stderr)
    sections = [
        b"OpenADA bounded KLayout process transcript",
        (
            f"stdout: retained_tail_bytes={len(stdout_tail)} "
            f"observed_bytes={process.stdout_bytes} truncated={str(process.stdout_truncated).lower()}"
        ).encode("ascii"),
        b"--- stdout tail ---",
        stdout_tail,
        (
            f"stderr: retained_tail_bytes={len(stderr_tail)} "
            f"observed_bytes={process.stderr_bytes} truncated={str(process.stderr_truncated).lower()}"
        ).encode("ascii"),
        b"--- stderr tail ---",
        stderr_tail,
        b"",
    ]
    # Keep the decoded values live here as an assertion that transcript tails
    # always remain valid UTF-8, which the independent verifier requires.
    assert stdout_text.encode("utf-8") == stdout_tail
    assert stderr_text.encode("utf-8") == stderr_tail
    return b"\n".join(sections)


def _write_transcript(
    anchor: _OutputAnchor,
    process: ProcessResult,
) -> tuple[dict | None, dict]:
    capture: dict[str, Any] = {
        "path": str(anchor.transcript_path),
        "origin": "openada",
        "capture_policy": "bounded process tails",
        "stdout_observed_bytes": process.stdout_bytes,
        "stderr_observed_bytes": process.stderr_bytes,
        "stdout_truncated": process.stdout_truncated,
        "stderr_truncated": process.stderr_truncated,
        "status": "missing",
    }
    if not _revalidate_anchor(anchor):
        capture["status"] = "parent_changed"
        return None, capture
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    body = _transcript_bytes(process)
    stdout_text, stdout_tail = _transcript_tail(process.stdout)
    stderr_text, stderr_tail = _transcript_tail(process.stderr)
    capture["stdout_retained_bytes"] = len(stdout_tail)
    capture["stderr_retained_bytes"] = len(stderr_tail)
    try:
        descriptor = os.open(
            anchor.transcript_name,
            flags,
            0o600,
            dir_fd=anchor.parent_fd,
        )
    except OSError:
        capture["status"] = "collision"
        return None, capture
    try:
        written = 0
        while written < len(body):
            written += os.write(descriptor, body[written:])
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        current = _lstat(anchor.transcript_name, dir_fd=anchor.parent_fd)
        if (
            current is None
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _file_signature(metadata) != _file_signature(current)
            or not _revalidate_anchor(anchor)
        ):
            capture["status"] = "unstable"
            return None, capture
        digest = hashlib.sha256(body).hexdigest()
        capture.update(
            {
                "status": "valid",
                "bytes": metadata.st_size,
                "sha256": digest,
            }
        )
        return (
            {
                "kind": "klayout-transcript",
                "role": "evidence",
                "path": str(anchor.transcript_path),
                "exists": True,
                "bytes": metadata.st_size,
                "sha256": digest,
            },
            capture,
        )
    except OSError:
        capture["status"] = "unwritable"
        return None, capture
    finally:
        os.close(descriptor)


def _valid_variable_name(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= MAX_VARIABLE_NAME_CHARS
        and VARIABLE_NAME_RE.fullmatch(value)
    )


def _valid_scalar(value: object, *, limit: int) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= limit
        and "\x00" not in value
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _normalize_deck_variables(
    values: Mapping[str, str] | Sequence[tuple[str, str]],
    *,
    reserved: set[str],
) -> tuple[list[tuple[str, str]], tuple[str, str] | None]:
    if isinstance(values, Mapping):
        items: Sequence[tuple[str, str]] = list(values.items())
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        items = values
    else:
        return [], ("deck_variable.invalid", "Deck variables must be a bounded mapping or sequence.")
    if len(items) > MAX_DECK_VARIABLES:
        return [], (
            "deck_variable.invalid",
            f"At most {MAX_DECK_VARIABLES} KLayout deck variables may be declared.",
        )
    normalized: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            return [], ("deck_variable.invalid", "Each deck variable must be a name/value pair.")
        name, value = item
        if not _valid_variable_name(name):
            return [], ("deck_variable.invalid", "A KLayout deck variable name is invalid.")
        if name in reserved:
            return [], (
                "deck_variable.reserved",
                f"Deck variable {name!r} is controlled by a dedicated OpenADA option.",
            )
        if not _valid_scalar(value, limit=MAX_VARIABLE_VALUE_CHARS):
            return [], (
                "deck_variable.invalid",
                f"Deck variable {name!r} has an overlong or control-containing value.",
            )
        if name in normalized:
            return [], ("deck_variable.duplicate", f"Deck variable {name!r} is repeated.")
        normalized[name] = value
    return sorted(normalized.items()), None


def _resolve_script_report(value: str | Path, *, run_dir: Path) -> tuple[Path | None, str | None]:
    try:
        raw = os.fspath(value)
    except TypeError:
        return None, "The expected report path is invalid."
    if not isinstance(raw, str) or not raw or "\x00" in raw or len(raw) > MAX_PATH_CHARS:
        return None, "The expected report path is empty or overlong."
    relative = Path(raw)
    if (
        relative.is_absolute()
        or os.fspath(relative) != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None, "A script-owned report must be an exact path relative to --workdir."
    if any(len(part) > MAX_PATH_COMPONENT_CHARS for part in relative.parts):
        return None, "The expected report contains an overlong path component."
    if any(character in raw for character in "*?[]"):
        return None, "The expected report must be an exact path, not a glob."
    return _lexical_absolute(relative, base=run_dir), None


def _records_changed(records: list[dict]) -> list[str]:
    changed: list[str] = []
    for record in records:
        try:
            current = file_record(
                record["path"],
                kind=record["kind"],
                role=record["role"],
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            changed.append(record["path"])
            continue
        if any(
            current.get(field) != record.get(field)
            for field in ("path", "exists", "bytes", "sha256")
        ):
            changed.append(record["path"])
    return changed


def _static_invalid(
    tool: dict,
    inputs: list[dict],
    data: dict,
    *,
    summary: str,
    code: str,
    message: str,
) -> dict:
    return result(
        "drc",
        tool=tool,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary=summary,
        inputs=inputs,
        diagnostics=[diagnostic("error", code, message)],
        data=data,
    )


class KLayoutDriver:
    def __init__(
        self,
        binary_path: str | None = None,
        *,
        discovery: DiscoveryManager | None = None,
    ) -> None:
        self.discovery = discovery or DiscoveryManager(
            binary_overrides={"klayout": binary_path} if binary_path else None
        )
        self.binary = self.discovery.find_binary("klayout")

    def drc(
        self,
        gds_path: str | Path,
        rule_deck_path: str | Path,
        report_path: str | Path | None = None,
        *,
        expected_report: str | Path | None = None,
        workdir: str | Path | None = None,
        top_cell: str | None = None,
        report_variable: str = "report",
        deck_variables: Mapping[str, str] | Sequence[tuple[str, str]] = (),
        provenance_inputs: Sequence[str | Path] = (),
        waiver_file: str | Path | None = None,
        timeout: float = 180.0,
    ) -> dict:
        try:
            gds = Path(gds_path).expanduser().resolve()
            deck = Path(rule_deck_path).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            info = self.discovery.inspect_tool("klayout")
            tool = tool_record("klayout", path=self.binary, version=info["version"])
            return _static_invalid(
                tool,
                [],
                {},
                summary="A KLayout input path could not be resolved.",
                code="input.unreadable",
                message=f"Cannot resolve a required DRC input path: {exc}",
            )
        try:
            run_dir = (
                Path(workdir).expanduser().resolve(strict=True)
                if workdir is not None
                else Path.cwd().resolve(strict=True)
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            info = self.discovery.inspect_tool("klayout")
            tool = tool_record("klayout", path=self.binary, version=info["version"])
            return _static_invalid(
                tool,
                [],
                {"working_directory": None},
                summary="The KLayout working directory is invalid.",
                code="workdir.invalid",
                message=f"Cannot resolve the working directory: {exc}",
            )

        info = self.discovery.inspect_tool("klayout")
        tool = tool_record("klayout", path=self.binary, version=info["version"])
        ownership = "script" if expected_report is not None else "variable"
        base_data: dict[str, Any] = {
            "report_output": {
                "ownership": ownership,
                "binding_variable": report_variable if ownership == "variable" else None,
                "fresh_required": True,
                "parent_anchored": True,
            },
            "working_directory": str(run_dir),
            "working_directory_is_sandbox": False,
            "top_cell": top_cell,
            "startup": {
                "batch_flag": "-b",
                "database_only": True,
                "configuration_files": "disabled",
                "implicit_macros": "disabled",
            },
            "transitive_rule_inputs_enumerated": False,
            "deck_trust": "caller-supplied executable Ruby; OpenADA does not sandbox the deck",
            "environment": {
                name: (
                    bounded_text(os.environ[name], limit=1_024)
                    if name in os.environ
                    else None
                )
                for name in RELEVANT_ENVIRONMENT
            },
            "ambient_environment_enumerated": False,
        }
        inputs: list[dict[str, Any]] = []
        for path, kind, role in (
            (gds, "gds", "input"),
            (deck, "klayout-drc-deck", "rules"),
        ):
            try:
                inputs.append(file_record(path, kind=kind, role=role))
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A KLayout input could not be hashed.",
                    code="input.unreadable",
                    message=f"Cannot hash {path}: {exc}",
                )
        if inputs[0]["path"] == inputs[1]["path"]:
            return _static_invalid(
                tool,
                inputs[:1],
                base_data,
                summary="The KLayout GDS and rule deck inputs collide.",
                code="input.duplicate",
                message="The GDS and executable rule deck must resolve to distinct files.",
            )
        missing = [record["path"] for record in inputs if not record["exists"]]
        if missing:
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="One or more DRC inputs do not exist.",
                code="input.missing",
                message="Missing DRC input(s): " + ", ".join(missing),
            )
        if not run_dir.is_dir():
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The KLayout working directory is invalid.",
                code="workdir.invalid",
                message=f"Working directory is not a directory: {run_dir}",
            )
        if (report_path is None) == (expected_report is None):
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="Exactly one KLayout report mode must be selected.",
                code="deck_output.invalid",
                message="Provide either a variable-bound report path or one script-owned expected report.",
            )
        if not _valid_variable_name(report_variable) or report_variable in {
            "input",
            "topcell",
        }:
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The KLayout report variable is invalid.",
                code="report_variable.invalid",
                message=(
                    "The report variable must be a bounded Ruby-style identifier distinct "
                    "from OpenADA's dedicated input and topcell bindings."
                ),
            )
        if ownership == "script" and report_variable != "report":
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="A report variable cannot be selected in script-owned mode.",
                code="report_variable.invalid",
                message="Omit --report-variable when using --expect-report.",
            )
        if top_cell is not None and (
            not top_cell or not _valid_scalar(top_cell, limit=MAX_TOP_CELL_CHARS)
        ):
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The KLayout top-cell selection is invalid.",
                code="top_cell.invalid",
                message="The top cell is empty, overlong, or contains control characters.",
            )

        normalized_variables, variable_error = _normalize_deck_variables(
            deck_variables,
            reserved={"input", "topcell", "report", report_variable},
        )
        if variable_error:
            code, message = variable_error
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="A KLayout deck variable is invalid.",
                code=code,
                message=message,
            )
        base_data["deck_variables"] = [
            {"name": name, "value": bounded_text(value, limit=1_024)}
            for name, value in normalized_variables
        ]

        if not isinstance(provenance_inputs, Sequence) or isinstance(
            provenance_inputs, (str, bytes)
        ):
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The declared KLayout provenance inputs are invalid.",
                code="provenance_input.invalid",
                message=f"Declare at most {MAX_PROVENANCE_INPUTS} provenance input files.",
            )
        if len(provenance_inputs) > MAX_PROVENANCE_INPUTS:
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The declared KLayout provenance inputs are invalid.",
                code="provenance_input.invalid",
                message=f"Declare at most {MAX_PROVENANCE_INPUTS} provenance input files.",
            )
        seen_inputs = {Path(record["path"]) for record in inputs}
        for value in provenance_inputs:
            try:
                path = Path(value).expanduser().resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A declared KLayout provenance input is invalid.",
                    code="provenance_input.invalid",
                    message="Each provenance input must be a resolvable file path.",
                )
            if path in seen_inputs:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A KLayout provenance input is duplicated.",
                    code="provenance_input.duplicate",
                    message=f"Input is declared more than once: {path}",
                )
            try:
                record = file_record(
                    path,
                    kind="klayout-rules-input",
                    role="rules-dependency",
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A declared KLayout provenance input could not be hashed.",
                    code="provenance_input.unreadable",
                    message=f"Cannot hash provenance input {path}: {exc}",
                )
            inputs.append(record)
            seen_inputs.add(path)
            if not record["exists"]:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A declared KLayout provenance input is missing.",
                    code="provenance_input.missing",
                    message=f"Provenance input not found: {path}",
                )

        if ownership == "script":
            assert expected_report is not None
            resolved_report, report_error = _resolve_script_report(
                expected_report,
                run_dir=run_dir,
            )
            if resolved_report is None:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="The script-owned KLayout report declaration is invalid.",
                    code="deck_output.invalid",
                    message=report_error or "Invalid expected report path.",
                )
            report_file = resolved_report
            create_parent = False
            base_data["report_output"]["declared_path"] = os.fspath(expected_report)
        else:
            assert report_path is not None
            try:
                report_file = _lexical_absolute(report_path)
            except (OSError, RuntimeError, TypeError, ValueError):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="The variable-bound KLayout report path is invalid.",
                    code="deck_output.invalid",
                    message="The report path cannot be resolved as an exact filesystem path.",
                )
            create_parent = True
            base_data["report_output"]["declared_path"] = str(report_file)
        base_data["report_output"]["path"] = str(report_file)

        if report_file in seen_inputs:
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The KLayout report collides with an input.",
                code="deck_output.invalid",
                message="Choose a report path distinct from the GDS, deck, and provenance inputs.",
            )
        if not self.binary:
            return result(
                "drc",
                tool=tool,
                execution=static_execution("not_available"),
                engineering_status="unknown",
                summary="KLayout is not available in the selected runtime.",
                inputs=inputs,
                diagnostics=[diagnostic("error", "tool.missing", "KLayout was not found.")],
                data=base_data,
            )

        anchor, anchor_error = _open_output_anchor(
            report_file,
            create_parent=create_parent,
        )
        if anchor is None:
            assert anchor_error is not None
            code, message = anchor_error
            return _static_invalid(
                tool,
                inputs,
                base_data,
                summary="The KLayout report path is not a fresh anchored output.",
                code=code,
                message=message,
            )

        waiver_snapshot: _AnchoredInput | None = None
        try:
            waiver_name = anchor.report_name + ".w"
            waiver_path = anchor.report_path.with_name(waiver_name)
            if waiver_path in seen_inputs:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="The automatic KLayout waiver path collides with another input.",
                    code="waiver.invalid",
                    message=(
                        "The exact <report>.w path must be distinct from the GDS, deck, "
                        "and every declared provenance input."
                    ),
                )
            try:
                waiver_metadata = _lstat(waiver_name, dir_fd=anchor.parent_fd)
            except OSError as exc:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="The KLayout waiver path cannot be inspected.",
                    code="waiver.unreadable",
                    message=f"Cannot inspect the automatic waiver sidecar: {exc}",
                )
            if waiver_file is None:
                base_data["waiver_database"] = {
                    "policy": "disabled-by-absence",
                    "path": str(waiver_path),
                    "declared": False,
                }
                if waiver_metadata is not None:
                    return _static_invalid(
                        tool,
                        inputs,
                        base_data,
                        summary="An ambient KLayout waiver database is undeclared.",
                        code="waiver.undeclared",
                        message=(
                            f"KLayout automatically reads {waiver_path}; declare it explicitly "
                            "or choose a fresh report basename."
                        ),
                    )
            else:
                try:
                    declared_waiver = _lexical_absolute(waiver_file)
                except (OSError, TypeError, ValueError):
                    declared_waiver = Path()
                base_data["waiver_database"] = {
                    "policy": "explicit",
                    "path": str(waiver_path),
                    "declared": True,
                }
                if declared_waiver != waiver_path:
                    return _static_invalid(
                        tool,
                        inputs,
                        base_data,
                        summary="The explicit KLayout waiver path is invalid.",
                        code="waiver.invalid",
                        message=f"The waiver database must be the automatic sidecar {waiver_path}.",
                    )
                waiver_snapshot, waiver_error = _open_anchored_input(
                    anchor,
                    path=waiver_path,
                    name=waiver_name,
                    kind="klayout-waiver-database",
                    role="configuration",
                )
                if waiver_snapshot is None:
                    assert waiver_error is not None
                    waiver_code, waiver_message = waiver_error
                    return _static_invalid(
                        tool,
                        inputs,
                        base_data,
                        summary="The explicit KLayout waiver database is missing or unsafe.",
                        code=waiver_code,
                        message=waiver_message,
                    )
                inputs.append(waiver_snapshot.record)

            if not _anchor_is_fresh(anchor):
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="The KLayout output path changed before launch.",
                    code="deck_output.not_fresh",
                    message="The report or transcript appeared, or its parent changed, before launch.",
                )
            ordinary_inputs = [
                record
                for record in inputs
                if record["kind"] != "klayout-waiver-database"
            ]
            changed_before = _records_changed(ordinary_inputs)
            if waiver_snapshot is not None and not _anchored_input_is_stable(
                anchor, waiver_snapshot
            ):
                changed_before.append(str(waiver_snapshot.path))
            if changed_before:
                return _static_invalid(
                    tool,
                    inputs,
                    base_data,
                    summary="A KLayout input changed before launch.",
                    code="input.changed_before_launch",
                    message="Changed input(s): " + ", ".join(changed_before),
                )

            command = [
                self.binary,
                "-b",
                "-r",
                str(deck),
                "-rd",
                f"input={gds}",
            ]
            if ownership == "variable":
                command.extend(["-rd", f"{report_variable}={report_file}"])
            if top_cell is not None:
                command.extend(["-rd", f"topcell={top_cell}"])
            for name, value in normalized_variables:
                command.extend(["-rd", f"{name}={value}"])

            process = run_process(command, cwd=run_dir, timeout=timeout)
            report_artifact, report_capture, parsed = _capture_report(
                anchor,
                deck=deck,
                top_cell=top_cell,
            )
            transcript_artifact, transcript_capture = _write_transcript(anchor, process)
            stdout_text, _ = _transcript_tail(process.stdout)
            stderr_text, _ = _transcript_tail(process.stderr)
            unexpected_waiver = False
            try:
                observed_waiver = _lstat(waiver_name, dir_fd=anchor.parent_fd)
            except OSError:
                observed_waiver = None
                unexpected_waiver = waiver_file is None
            waiver_stable = bool(
                waiver_snapshot is None
                or _anchored_input_is_stable(anchor, waiver_snapshot)
            )
            if waiver_file is None and observed_waiver is not None:
                unexpected_waiver = True
                base_data["waiver_database"]["status"] = "appeared_during_run"
            elif waiver_snapshot is not None and not waiver_stable:
                base_data["waiver_database"]["status"] = "changed_during_run"
            else:
                base_data["waiver_database"]["status"] = (
                    "stable" if waiver_file is not None else "absent"
                )

            changed_inputs = _records_changed(ordinary_inputs)
            if waiver_snapshot is not None and not waiver_stable:
                changed_inputs.append(str(waiver_snapshot.path))
            inputs_stable = not changed_inputs
            base_data.update(
                {
                    "report_output": {
                        **base_data["report_output"],
                        "capture": report_capture,
                    },
                    "transcript": {
                        **transcript_capture,
                        "stdout_tail": bounded_text(stdout_text, limit=4_000),
                        "stderr_tail": bounded_text(stderr_text, limit=4_000),
                        "limitation": (
                            "The artifact retains bounded stdout/stderr tails, not an unbounded native log."
                        ),
                    },
                    "inputs_stable": inputs_stable,
                    "changed_inputs": changed_inputs,
                    "report": parsed,
                }
            )

            diagnostics: list[dict] = []
            if process.status != "completed":
                diagnostics.append(
                    diagnostic(
                        "error",
                        f"execution.{process.status}",
                        process.error or "KLayout did not complete.",
                    )
                )
            elif process.exit_code != 0:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "klayout.nonzero_exit",
                        f"KLayout exited with code {process.exit_code}.",
                    )
                )
            if report_capture["status"] == "missing":
                diagnostics.append(
                    diagnostic(
                        "error",
                        "artifact.missing",
                        "KLayout did not produce the exact declared report database.",
                    )
                )
            elif report_capture["status"] != "valid":
                message = (
                    parsed.get("error")
                    if isinstance(parsed, dict) and parsed.get("error")
                    else f"KLayout report capture status: {report_capture['status']}."
                )
                diagnostics.append(diagnostic("error", "report.invalid", message))
            if transcript_capture["status"] != "valid":
                diagnostics.append(
                    diagnostic(
                        "error",
                        "transcript.invalid",
                        f"The bounded KLayout transcript status is {transcript_capture['status']}.",
                    )
                )
            if changed_inputs:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "input.changed",
                        "One or more KLayout inputs changed during execution.",
                    )
                )
            if unexpected_waiver or not waiver_stable:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "waiver.changed",
                        (
                            "The automatic KLayout waiver sidecar appeared or the explicitly "
                            "declared waiver database changed during execution."
                        ),
                    )
                )
            diagnostics.append(
                diagnostic(
                    "warning",
                    "provenance.transitive_rules_unenumerated",
                    (
                        "KLayout decks are executable Ruby. Only the main deck, declared provenance "
                        "inputs, and optional waiver database are hashed by this operation."
                    ),
                )
            )

            count = (
                parsed.get("total_violations")
                if isinstance(parsed, dict)
                and parsed.get("validation", {}).get("valid") is True
                else None
            )
            base_data["drc_clean"] = count == 0 if count is not None else None
            trustworthy = bool(
                process.status == "completed"
                and process.exit_code == 0
                and report_capture["status"] == "valid"
                and transcript_capture["status"] == "valid"
                and inputs_stable
                and not unexpected_waiver
                and count is not None
            )
            if not trustworthy:
                engineering_status = "unknown"
                summary = "The DRC run did not yield a trustworthy engineering result."
            elif count == 0:
                engineering_status = "pass"
                summary = "KLayout reported zero DRC violations."
            else:
                engineering_status = "fail"
                summary = f"KLayout reported {count} DRC violation(s)."

            artifacts = [
                artifact
                for artifact in (report_artifact, transcript_artifact)
                if artifact is not None
            ]
            return result(
                "drc",
                tool=tool,
                execution=process,
                engineering_status=engineering_status,
                summary=summary,
                inputs=inputs,
                artifacts=artifacts,
                diagnostics=diagnostics,
                data=base_data,
            )
        finally:
            if waiver_snapshot is not None:
                waiver_snapshot.close()
            anchor.close()

    @staticmethod
    def parse_lyrdb(path: str | Path) -> dict:
        return parse_lyrdb(path)

    @staticmethod
    def _parse_geometry(text: str) -> dict | None:
        geometry, _ = parse_geometry(text)
        return geometry


KLayoutEngine = KLayoutDriver
