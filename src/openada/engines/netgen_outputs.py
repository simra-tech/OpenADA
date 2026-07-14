"""Bounded native Netgen LVS evidence parsing and filesystem capture."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, BinaryIO, Callable

from ..process import ProcessResult


MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_REPORT_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REPORT_LINE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000
MAX_JSON_STRING_CHARS = 65_536
MAX_COMPARISONS = 4_096
MAX_NORMALIZED_EXAMPLES = 200
MAX_PATH_BYTES = 4_095

_FINAL_MATCH_RE = re.compile(
    r"^Final result:\s*(?:Circuits|Netlists) match uniquely\.\s*$", re.I
)
_FINAL_FAILURE_RE = re.compile(
    r"^Final result:\s*(?:"
    r"(?:Circuits|Netlists) do not match|"
    r"Circuits match uniquely with port errors|"
    r"Top level cell failed pin matching|"
    r"Subcell\(s\) failed matching|"
    r"Property errors were found"
    r")\.\s*$",
    re.I,
)
_PROPERTY_FAILURE_RE = re.compile(
    r"^(?:Property errors were found\.|The following cells had property errors:)\s*$",
    re.I,
)
_UNIQUE_MATCH_RE = re.compile(r"^(?:Circuits|Netlists) match uniquely\.\s*$", re.I)
_TERMINAL_FAILURE_RE = re.compile(
    r"^(?:"
    r"(?:Circuits|Netlists) do not match|"
    r"Circuits match uniquely with port errors|"
    r"Top level cell failed pin matching|"
    r"Subcell\(s\) failed matching|"
    r"Property errors were found"
    r")\.\s*$",
    re.I,
)
_CIRCUIT_PAIR_RE = re.compile(
    r"^Circuit 1:\s*(?P<left>.*?)\s*\|Circuit 2:\s*(?P<right>.*?)\s*$",
    re.I,
)
_DEVICE_COUNT_RE = re.compile(
    r"^Number of devices:\s*(?P<left>\d+)\s*\|Number of devices:\s*(?P<right>\d+)\s*$",
    re.I,
)
_NET_COUNT_RE = re.compile(
    r"^Number of nets:\s*(?P<left>\d+)\s*\|Number of nets:\s*(?P<right>\d+)\s*$",
    re.I,
)
_DEVICE_EQUIVALENCE_RE = re.compile(
    r"^Device classes\s+(?P<left>\S+)\s+and\s+(?P<right>\S+)\s+are equivalent\.\s*$",
    re.I,
)
_NEGATED_MISMATCH_RE = re.compile(
    r"\b(?:no|zero)\s+mismatches?\b|\bmismatch(?:es)?\s*(?:count)?\s*[:=]\s*0\b",
    re.I,
)
_FAILURE_RE = re.compile(
    r"\b(?:netlists?|circuits?)\s+do\s+not\s+match\b|"
    r"\bmismatches?\b|"
    r"\bproperty errors?\b|"
    r"\bport errors?\b|"
    r"\bfailed (?:pin )?matching\b|"
    r"\bnetworks? match locally but not globally\b|"
    r"\bno matching\b",
    re.I,
)
_REVIEWED_STDERR_RE = re.compile(
    r"^Unable to permute model "
    r"(?P<model>[A-Za-z0-9_.$:+/@-]{1,256}) pins "
    r"(?P<first>[A-Za-z0-9_.$:+/@-]{1,128}), "
    r"(?P<second>[A-Za-z0-9_.$:+/@-]{1,128})\.$"
)


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
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


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _lstat(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result | None:
    try:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _lexical_absolute(path: str | Path) -> Path:
    value = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(value):
        value = os.path.join(os.fspath(Path.cwd()), value)
    return Path(os.path.abspath(value))


def _open_real_directory(
    path: Path,
    *,
    create_missing: bool,
) -> tuple[int, tuple[tuple[str, tuple[int, int, int]], ...]]:
    absolute = _lexical_absolute(path)
    current_fd = os.open(os.path.sep, _directory_flags())
    signatures: list[tuple[str, tuple[int, int, int]]] = []
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise NotADirectoryError(
                    f"output parent component is not a directory: {component}"
                )
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


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class StableInput:
    path: Path
    descriptor: int
    signature: tuple[int, int, int, int, int, int]
    record: dict[str, Any]

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            pass


def open_stable_input(
    path: str | Path,
    *,
    kind: str,
    role: str,
) -> tuple[StableInput | None, tuple[str, str] | None]:
    """Open, bound, and hash one exact regular input for post-run rechecking."""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        encoded = os.fsencode(resolved)
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeEncodeError) as exc:
        return None, ("input.missing", f"Cannot resolve the declared input: {exc}")
    if len(encoded) > MAX_PATH_BYTES or any(ord(character) < 32 for character in str(resolved)):
        return None, ("input.invalid", "A declared input path is overlong or contains control text.")
    try:
        before = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        return None, ("input.unreadable", f"Cannot inspect {resolved}: {exc}")
    if not stat.S_ISREG(before.st_mode):
        return None, ("input.invalid", f"The declared input is not a regular file: {resolved}")
    if before.st_size > MAX_INPUT_BYTES:
        return None, (
            "input.too_large",
            f"The declared input exceeds the {MAX_INPUT_BYTES}-byte validation limit: {resolved}",
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        return None, ("input.unreadable", f"Cannot open {resolved}: {exc}")
    snapshot: StableInput | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_signature(opened) != _file_signature(before):
            return None, ("input.unstable", f"The declared input changed while opening: {resolved}")
        digest = _hash_descriptor(descriptor)
        finished = os.fstat(descriptor)
        current = os.stat(resolved, follow_symlinks=False)
        if (
            _file_signature(opened) != _file_signature(finished)
            or _file_signature(opened) != _file_signature(current)
        ):
            return None, ("input.unstable", f"The declared input changed while hashing: {resolved}")
        record = {
            "kind": kind,
            "role": role,
            "path": str(resolved),
            "exists": True,
            "bytes": finished.st_size,
            "sha256": digest,
        }
        snapshot = StableInput(
            path=resolved,
            descriptor=descriptor,
            signature=_file_signature(opened),
            record=record,
        )
        return snapshot, None
    except OSError as exc:
        return None, ("input.unreadable", f"Cannot hash {resolved}: {exc}")
    finally:
        if snapshot is None:
            os.close(descriptor)


def stable_input_unchanged(value: StableInput) -> bool:
    try:
        opened = os.fstat(value.descriptor)
        current = os.stat(value.path, follow_symlinks=False)
        return bool(
            _file_signature(opened) == value.signature
            and _file_signature(current) == value.signature
            and _hash_descriptor(value.descriptor) == value.record["sha256"]
        )
    except (OSError, KeyError):
        return False


@dataclass(slots=True)
class OutputAnchor:
    report_path: Path
    report_name: str
    json_path: Path
    json_name: str
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


def _revalidate_anchor(anchor: OutputAnchor) -> bool:
    try:
        descriptor, signatures = _open_real_directory(anchor.parent_path, create_missing=False)
    except OSError:
        return False
    try:
        return bool(
            signatures == anchor.signatures
            and _directory_signature(os.fstat(descriptor))
            == _directory_signature(os.fstat(anchor.parent_fd))
        )
    except OSError:
        return False
    finally:
        os.close(descriptor)


def open_output_anchor(
    report_path: str | Path,
    *,
    create_parent: bool,
) -> tuple[OutputAnchor | None, tuple[str, str] | None]:
    try:
        report = _lexical_absolute(report_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return None, ("output.invalid", f"Cannot normalize the Netgen report path: {exc}")
    if not report.suffix:
        return None, (
            "output.invalid",
            "The Netgen report filename must have a suffix so its native JSON path is unambiguous.",
        )
    json_path = report.with_suffix(".json")
    transcript = report.with_name(report.name + ".openada.log")
    paths = (report, json_path, transcript)
    if len(set(paths)) != len(paths):
        return None, ("output.invalid", "The report and derived evidence paths collide.")
    try:
        encoded_paths = [os.fsencode(path) for path in paths]
        encoded_names = [os.fsencode(path.name) for path in paths]
    except (OSError, TypeError, ValueError, UnicodeEncodeError) as exc:
        return None, ("output.invalid", f"Cannot encode the Netgen evidence paths: {exc}")
    if (
        any(not path.name or path.name in {".", ".."} for path in paths)
        or any(len(path) > MAX_PATH_BYTES for path in encoded_paths)
        or any(any(byte < 32 for byte in name) for name in encoded_names)
    ):
        return None, ("output.invalid", "A Netgen evidence path is empty, overlong, or unsafe.")
    try:
        parent_fd, signatures = _open_real_directory(report.parent, create_missing=create_parent)
    except (OSError, TypeError, ValueError) as exc:
        return None, ("output.anchor_failed", f"Cannot anchor the report parent: {exc}")
    anchor = OutputAnchor(
        report_path=report,
        report_name=report.name,
        json_path=json_path,
        json_name=json_path.name,
        transcript_path=transcript,
        transcript_name=transcript.name,
        parent_path=report.parent,
        parent_fd=parent_fd,
        signatures=signatures,
    )
    try:
        name_max = os.fpathconf(parent_fd, "PC_NAME_MAX")
        path_max = os.fpathconf(parent_fd, "PC_PATH_MAX")
        if name_max >= 0 and any(len(value) > name_max for value in encoded_names):
            raise ValueError(f"an evidence filename exceeds the {name_max}-byte filesystem limit")
        if path_max >= 0 and any(len(value) >= path_max for value in encoded_paths):
            raise ValueError(f"an evidence path exceeds the {path_max}-byte filesystem limit")
        for path in paths:
            if _lstat(path.name, dir_fd=parent_fd) is not None:
                raise FileExistsError(f"evidence path must be fresh: {path}")
    except (OSError, ValueError) as exc:
        anchor.close()
        code = "output.not_fresh" if isinstance(exc, FileExistsError) else "output.invalid"
        return None, (code, str(exc))
    return anchor, None


def anchor_is_fresh(anchor: OutputAnchor) -> bool:
    try:
        return bool(
            _revalidate_anchor(anchor)
            and all(
                _lstat(name, dir_fd=anchor.parent_fd) is None
                for name in (anchor.report_name, anchor.json_name, anchor.transcript_name)
            )
        )
    except OSError:
        return False


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _bounded_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError("native JSON exceeds the structural node bound")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("native JSON exceeds the structural depth bound")
        if isinstance(current, dict):
            for key, item in current.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > MAX_JSON_STRING_CHARS:
            raise ValueError("native JSON contains an overlong string")
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("native JSON contains a non-finite number")
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            raise ValueError("native JSON contains an unsupported value")


def _paired_nonnegative_counts(value: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("expected a two-sided count list")
    normalized: list[list[Any]] = []
    for side in value:
        if not isinstance(side, list):
            raise ValueError("expected a list of device counts")
        entries: list[tuple[str, int]] = []
        names: set[str] = set()
        for entry in side:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not entry[0]
                or not isinstance(entry[1], int)
                or isinstance(entry[1], bool)
                or entry[1] < 0
            ):
                raise ValueError("invalid native device count")
            if entry[0] in names:
                raise ValueError("duplicate native device class")
            names.add(entry[0])
            entries.append((entry[0], entry[1]))
        normalized.append(sorted(entries))
    return normalized[0], normalized[1]


def _paired_strings(value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("expected a two-sided string list")
    sides: list[list[str]] = []
    for side in value:
        if (
            not isinstance(side, list)
            or not all(isinstance(item, str) and item for item in side)
            or len(set(side)) != len(side)
        ):
            raise ValueError("invalid native pin list")
        sides.append(side)
    return sides[0], sides[1]


def parse_netgen_json_stream(
    handle: BinaryIO,
    *,
    size: int,
    expected_cell: str,
) -> dict[str, Any]:
    validation: dict[str, Any] = {"valid": False, "bytes": size, "reason": "json.invalid"}
    if size <= 0:
        validation["reason"] = "json.empty"
        return {"validation": validation, "outcome": "unknown"}
    if size > MAX_JSON_BYTES:
        validation["reason"] = "json.too_large"
        return {"validation": validation, "outcome": "unknown"}
    try:
        body = handle.read(MAX_JSON_BYTES + 1)
        if len(body) != size or len(body) > MAX_JSON_BYTES:
            raise ValueError("native JSON size changed or exceeded its bound")
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
        _bounded_json_shape(payload)
        if not isinstance(payload, list) or not payload or len(payload) > MAX_COMPARISONS:
            raise ValueError("native JSON must be a bounded nonempty comparison array")

        top_matches = 0
        mismatch_count = 0
        mismatch_examples: list[str] = []
        top_device_counts: list[list[list[Any]]] | None = None
        top_node_counts: list[int] | None = None
        top_pin_counts: list[int] | None = None
        known_keys = {
            "name",
            "devices",
            "nets",
            "pins",
            "badnets",
            "badelements",
            "properties",
            "goodnets",
            "goodelements",
        }
        for index, comparison in enumerate(payload):
            if not isinstance(comparison, dict) or set(comparison) - known_keys:
                raise ValueError("native JSON comparison has an unknown shape")
            names = comparison.get("name")
            if (
                not isinstance(names, list)
                or len(names) != 2
                or not all(isinstance(item, str) and item for item in names)
            ):
                raise ValueError("native JSON comparison lacks two cell names")
            devices_left, devices_right = _paired_nonnegative_counts(comparison.get("devices"))
            nets = comparison.get("nets")
            if (
                not isinstance(nets, list)
                or len(nets) != 2
                or any(
                    not isinstance(item, int) or isinstance(item, bool) or item < 0
                    for item in nets
                )
            ):
                raise ValueError("native JSON comparison has invalid net counts")
            for field in ("badnets", "badelements"):
                if not isinstance(comparison.get(field), list):
                    raise ValueError(f"native JSON comparison lacks {field}")
            properties = comparison.get("properties", [])
            if not isinstance(properties, list):
                raise ValueError("native JSON properties must be a list")
            pins_value = comparison.get("pins")
            pins: tuple[list[str], list[str]] | None = None
            if pins_value is not None:
                pins = _paired_strings(pins_value)

            reasons: list[str] = []
            if devices_left != devices_right:
                reasons.append("device-counts")
            if nets[0] != nets[1]:
                reasons.append("net-counts")
            if pins is not None and pins[0] != pins[1]:
                reasons.append("pins")
            for field in ("badnets", "badelements", "properties"):
                if comparison.get(field):
                    reasons.append(field)
            mismatch_count += len(reasons)
            for reason in reasons:
                if len(mismatch_examples) < MAX_NORMALIZED_EXAMPLES:
                    mismatch_examples.append(f"comparison[{index}].{reason}")

            if names == [expected_cell, expected_cell]:
                top_matches += 1
                top_device_counts = [
                    [[name, count] for name, count in devices_left],
                    [[name, count] for name, count in devices_right],
                ]
                top_node_counts = [nets[0], nets[1]]
                top_pin_counts = [len(pins[0]), len(pins[1])] if pins is not None else None
                if pins is None:
                    reasons.append("missing-pins")
                    mismatch_count += 1
                    if len(mismatch_examples) < MAX_NORMALIZED_EXAMPLES:
                        mismatch_examples.append(f"comparison[{index}].missing-pins")
        if top_matches != 1:
            raise ValueError("native JSON does not contain exactly one requested top comparison")

        outcome = "fail" if mismatch_count else "pass"
        validation.update({"valid": True, "reason": "json.valid"})
        return {
            "validation": validation,
            "outcome": outcome,
            "comparison_count": len(payload),
            "top_cell": expected_cell,
            "top_comparison_count": top_matches,
            "device_counts": top_device_counts,
            "node_counts": top_node_counts,
            "pin_counts": top_pin_counts,
            "mismatch_count": mismatch_count,
            "mismatches": mismatch_examples,
            "mismatches_truncated": mismatch_count > len(mismatch_examples),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        validation["reason"] = "json.invalid"
        validation["detail"] = str(exc)[:1_000]
        return {"validation": validation, "outcome": "unknown"}


def parse_netgen_report_stream(
    handle: BinaryIO,
    *,
    size: int,
    expected_cell: str | None = None,
) -> dict[str, Any]:
    validation: dict[str, Any] = {"valid": False, "bytes": size, "reason": "report.invalid"}
    if size <= 0:
        validation["reason"] = "report.empty"
        return {"validation": validation, "outcome": "unknown"}
    if size > MAX_REPORT_BYTES:
        validation["reason"] = "report.too_large"
        return {"validation": validation, "outcome": "unknown"}
    saw_final_match = False
    saw_unique_match = False
    failure_count = 0
    failures: list[str] = []
    trailing: list[str] = []
    final_results: list[tuple[int, str]] = []
    property_failures: list[int] = []
    comparisons: list[dict[str, Any]] = []
    current_comparison: dict[str, Any] | None = None
    section: str | None = None
    nonempty_line = 0
    consumed = 0
    try:
        while line := handle.readline(MAX_REPORT_LINE_BYTES + 1):
            consumed += len(line)
            if len(line) > MAX_REPORT_LINE_BYTES:
                raise ValueError("native report contains an overlong line")
            text = line.decode("utf-8", errors="strict")
            stripped = text.strip()
            if stripped:
                nonempty_line += 1
                trailing.append(stripped)
                trailing = trailing[-8:]
            if stripped == "Subcircuit summary:":
                if len(comparisons) >= MAX_COMPARISONS:
                    raise ValueError("native report exceeds the comparison-count bound")
                section = "summary"
                current_comparison = {
                    "summary_binding": None,
                    "pins_binding": None,
                    "device_counts": None,
                    "node_counts": None,
                    "pin_lists_equivalent": False,
                    "device_classes_binding": None,
                }
                comparisons.append(current_comparison)
            elif stripped == "Subcircuit pins:":
                section = "pins"
            pair_match = _CIRCUIT_PAIR_RE.fullmatch(stripped)
            if pair_match and current_comparison is not None:
                if (
                    section == "summary"
                    and current_comparison["summary_binding"] is None
                ):
                    current_comparison["summary_binding"] = [
                        pair_match.group("left"),
                        pair_match.group("right"),
                    ]
                elif section == "pins" and current_comparison["pins_binding"] is None:
                    current_comparison["pins_binding"] = [
                        pair_match.group("left"),
                        pair_match.group("right"),
                    ]
            if _FINAL_MATCH_RE.fullmatch(stripped):
                saw_final_match = True
                final_results.append((nonempty_line, "pass"))
            elif _FINAL_FAILURE_RE.fullmatch(stripped):
                final_results.append((nonempty_line, "fail"))
            if _PROPERTY_FAILURE_RE.fullmatch(stripped):
                property_failures.append(nonempty_line)
            if _UNIQUE_MATCH_RE.fullmatch(stripped):
                saw_unique_match = True
            if _FAILURE_RE.search(stripped) and not _NEGATED_MISMATCH_RE.search(stripped):
                failure_count += 1
                if len(failures) < MAX_NORMALIZED_EXAMPLES:
                    failures.append(stripped[:1_000])
            device_match = _DEVICE_COUNT_RE.fullmatch(stripped)
            if (
                device_match
                and current_comparison is not None
                and section == "summary"
                and current_comparison["device_counts"] is None
            ):
                current_comparison["device_counts"] = [
                    int(device_match.group("left")),
                    int(device_match.group("right")),
                ]
            net_match = _NET_COUNT_RE.fullmatch(stripped)
            if (
                net_match
                and current_comparison is not None
                and section == "summary"
                and current_comparison["node_counts"] is None
            ):
                current_comparison["node_counts"] = [
                    int(net_match.group("left")),
                    int(net_match.group("right")),
                ]
            if stripped == "Cell pin lists are equivalent." and current_comparison is not None:
                current_comparison["pin_lists_equivalent"] = True
            device_equivalence = _DEVICE_EQUIVALENCE_RE.fullmatch(stripped)
            if device_equivalence and current_comparison is not None:
                current_comparison["device_classes_binding"] = [
                    device_equivalence.group("left"),
                    device_equivalence.group("right"),
                ]
        if consumed != size:
            raise ValueError("native report size changed while parsing")
        terminal_outcome: str | None = None
        terminal_style: str | None = None
        terminal_conflict = False
        if final_results:
            final_line, terminal_outcome = final_results[-1]
            terminal_style = "final-result"
            terminal_conflict = any(outcome != terminal_outcome for _, outcome in final_results)
            property_failure_after_match = bool(
                terminal_outcome == "pass"
                and any(line_number > final_line for line_number in property_failures)
            )
            if property_failure_after_match and not terminal_conflict:
                terminal_outcome = "fail"
                terminal_style = "post-final-property-error"
                terminal_is_final = True
            elif terminal_outcome == "pass":
                terminal_is_final = bool(
                    trailing
                    and (
                        _FINAL_MATCH_RE.fullmatch(trailing[-1])
                        or (
                            trailing[-1] == "."
                            and len(trailing) >= 2
                            and _FINAL_MATCH_RE.fullmatch(trailing[-2])
                        )
                    )
                )
            else:
                # Netgen may append bounded property/subcell detail after a
                # conclusive failure. JSON must independently corroborate the
                # report before the driver can expose engineering failure.
                terminal_is_final = True
            if not terminal_is_final or final_line <= 0:
                terminal_outcome = None
        elif trailing:
            if property_failures:
                terminal_outcome = "fail"
                terminal_style = "legacy-property-error"
            elif _UNIQUE_MATCH_RE.fullmatch(trailing[-1]):
                terminal_outcome = "pass"
                terminal_style = "legacy-terminal"
            elif _TERMINAL_FAILURE_RE.fullmatch(trailing[-1]):
                terminal_outcome = "fail"
                terminal_style = "legacy-terminal"
            if terminal_outcome is not None and terminal_style != "legacy-property-error":
                opposing = (
                    _TERMINAL_FAILURE_RE if terminal_outcome == "pass" else _UNIQUE_MATCH_RE
                )
                terminal_conflict = any(
                    opposing.fullmatch(line) for line in trailing[-4:-1]
                )

        if expected_cell is not None:
            expected_binding = [expected_cell, expected_cell]
            top_comparisons = [
                comparison
                for comparison in comparisons
                if comparison["summary_binding"] == expected_binding
            ]
        else:
            final_comparison = comparisons[-1] if comparisons else None
            final_binding = (
                final_comparison["summary_binding"] if final_comparison else None
            )
            expected_binding = (
                final_binding
                if final_binding and final_binding[0] == final_binding[1]
                else None
            )
            top_comparisons = [final_comparison] if expected_binding is not None else []
        top_comparison = top_comparisons[0] if len(top_comparisons) == 1 else None
        summary_binding = (
            top_comparison["summary_binding"] if top_comparison is not None else None
        )
        pins_binding = (
            top_comparison["pins_binding"] if top_comparison is not None else None
        )
        device_counts = (
            top_comparison["device_counts"] if top_comparison is not None else None
        )
        node_counts = top_comparison["node_counts"] if top_comparison is not None else None
        pin_lists_equivalent = bool(
            top_comparison is not None and top_comparison["pin_lists_equivalent"]
        )
        device_classes_binding = (
            top_comparison["device_classes_binding"]
            if top_comparison is not None
            else None
        )
        summary_bound = bool(
            top_comparison is not None
            and expected_binding is not None
            and summary_binding == expected_binding
        )
        counts_bound = bool(
            device_counts is not None
            and node_counts is not None
            and len(device_counts) == 2
            and len(node_counts) == 2
        )
        pass_structure = bool(
            summary_bound
            and counts_bound
            and device_counts is not None
            and device_counts[0] == device_counts[1]
            and node_counts is not None
            and node_counts[0] == node_counts[1]
            and pins_binding == expected_binding
            and pin_lists_equivalent
            and device_classes_binding == expected_binding
        )
        fail_structure = bool(summary_bound and counts_bound)
        if terminal_conflict:
            outcome = "unknown"
            reason = "report.conflicting_terminal_results"
        elif terminal_outcome == "pass" and pass_structure and failure_count == 0:
            outcome = "pass"
            reason = "report.valid"
        elif terminal_outcome == "fail" and fail_structure:
            outcome = "fail"
            reason = "report.valid"
        elif terminal_outcome is not None and not (
            pass_structure if terminal_outcome == "pass" else fail_structure
        ):
            outcome = "unknown"
            reason = "report.unbound_top_cell"
        elif terminal_outcome == "pass" and failure_count:
            outcome = "unknown"
            reason = "report.conflicting_evidence"
        else:
            outcome = "unknown"
            reason = "report.no_terminal_result"
        validation.update(
            {
                "valid": outcome != "unknown",
                "reason": reason,
            }
        )
        return {
            "validation": validation,
            "outcome": outcome,
            "final_match": saw_final_match,
            "legacy_terminal_match": terminal_style == "legacy-terminal" and outcome == "pass",
            "unique_match_markers": saw_unique_match,
            "terminal_outcome": terminal_outcome,
            "terminal_style": terminal_style,
            "terminal_conflict": terminal_conflict,
            "top_cell": expected_cell,
            "comparison_count": len(comparisons),
            "top_comparison_count": len(top_comparisons),
            "summary_binding": summary_binding,
            "pins_binding": pins_binding,
            "device_classes_binding": device_classes_binding,
            "pin_lists_equivalent": pin_lists_equivalent,
            "structure_complete": (
                pass_structure
                if terminal_outcome == "pass"
                else fail_structure
                if terminal_outcome == "fail"
                else False
            ),
            "device_counts": device_counts,
            "node_counts": node_counts,
            "mismatch_count": failure_count,
            "mismatches": failures,
            "mismatches_truncated": failure_count > len(failures),
        }
    except (OSError, UnicodeError, ValueError) as exc:
        validation["reason"] = "report.invalid"
        validation["detail"] = str(exc)[:1_000]
        return {"validation": validation, "outcome": "unknown"}


def _capture_file(
    anchor: OutputAnchor,
    *,
    path: Path,
    name: str,
    kind: str,
    max_bytes: int,
    parser: Callable[[BinaryIO, int], dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    capture: dict[str, Any] = {
        "path": str(path),
        "origin": "netgen",
        "parent_anchored": True,
        "status": "missing",
    }
    if not _revalidate_anchor(anchor):
        capture["status"] = "parent_changed"
        return None, capture, None
    try:
        before = _lstat(name, dir_fd=anchor.parent_fd)
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
    if before.st_size > max_bytes:
        capture["status"] = "too_large"
        return None, capture, None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=anchor.parent_fd)
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
            parsed = parser(handle, opened.st_size)
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
            capture["status"] = "unstable"
            return None, capture, parsed
        artifact = {
            "kind": kind,
            "role": "evidence",
            "path": str(path),
            "exists": True,
            "bytes": finished.st_size,
            "sha256": digest,
        }
        capture.update(
            {
                "status": (
                    "valid" if parsed.get("validation", {}).get("valid") is True else "invalid"
                ),
                "sha256": digest,
                "validation": parsed.get("validation"),
            }
        )
        return artifact, capture, parsed
    except OSError:
        capture["status"] = "unreadable"
        return None, capture, None
    finally:
        os.close(descriptor)


def capture_report(
    anchor: OutputAnchor,
    *,
    expected_cell: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    return _capture_file(
        anchor,
        path=anchor.report_path,
        name=anchor.report_name,
        kind="netgen-comparison",
        max_bytes=MAX_REPORT_BYTES,
        parser=lambda handle, size: parse_netgen_report_stream(
            handle,
            size=size,
            expected_cell=expected_cell,
        ),
    )


def capture_json(
    anchor: OutputAnchor,
    *,
    expected_cell: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    return _capture_file(
        anchor,
        path=anchor.json_path,
        name=anchor.json_name,
        kind="netgen-comparison-json",
        max_bytes=MAX_JSON_BYTES,
        parser=lambda handle, size: parse_netgen_json_stream(
            handle,
            size=size,
            expected_cell=expected_cell,
        ),
    )


def transcript_assessment(process: ProcessResult, *, setup_path: Path) -> dict[str, Any]:
    stdout_lines = process.stdout.splitlines()
    stderr_lines = process.stderr.splitlines()
    reviewed_stderr = [line for line in stderr_lines if _REVIEWED_STDERR_RE.fullmatch(line)]
    unrecognized_stderr = [
        line for line in stderr_lines if not _REVIEWED_STDERR_RE.fullmatch(line)
    ]
    setup_read = any(line.strip() == f"Reading setup file {setup_path}" for line in stdout_lines)
    setup_error = any(
        re.fullmatch(r"Warning:\s+There were errors reading the setup file", line.strip(), re.I)
        for line in stdout_lines
    ) or any(
        re.match(r"^Error\s+.+:\d+\s+\(ignoring\),", line.strip(), re.I)
        for line in stderr_lines
    )
    stdout_error = any(
        re.match(r"^Error(?:\s|:)", line.strip(), re.I) for line in stdout_lines
    )
    lvs_done = any(line.strip() == "LVS Done." for line in stdout_lines)
    utf8_valid = "\ufffd" not in process.stdout and "\ufffd" not in process.stderr
    complete = not process.stdout_truncated and not process.stderr_truncated and utf8_valid
    stderr_accepted = bool(
        (process.stderr_bytes == 0 and not stderr_lines)
        or (stderr_lines and not unrecognized_stderr)
    )
    clean = bool(
        complete
        and setup_read
        and not setup_error
        and not stdout_error
        and stderr_accepted
        and lvs_done
    )
    return {
        "complete": complete,
        "utf8_valid": utf8_valid,
        "setup_read": setup_read,
        "setup_error": setup_error,
        "stdout_error": stdout_error,
        "stderr_empty": process.stderr_bytes == 0,
        "stderr_policy": "empty-or-reviewed-netgen-permute-warning",
        "stderr_line_count": len(stderr_lines),
        "stderr_reviewed_warning_count": len(reviewed_stderr),
        "stderr_unrecognized_count": len(unrecognized_stderr),
        "stderr_accepted": stderr_accepted,
        "lvs_done": lvs_done,
        "clean": clean,
    }


def _transcript_bytes(process: ProcessResult) -> bytes:
    stdout = process.stdout.encode("utf-8", errors="replace")
    stderr = process.stderr.encode("utf-8", errors="replace")
    return b"\n".join(
        (
            b"OpenADA bounded complete Netgen process transcript",
            (
                f"stdout: retained_utf8_bytes={len(stdout)} observed_bytes={process.stdout_bytes} "
                f"truncated={str(process.stdout_truncated).lower()}"
            ).encode("ascii"),
            b"--- stdout ---",
            stdout,
            (
                f"stderr: retained_utf8_bytes={len(stderr)} observed_bytes={process.stderr_bytes} "
                f"truncated={str(process.stderr_truncated).lower()}"
            ).encode("ascii"),
            b"--- stderr ---",
            stderr,
            b"",
        )
    )


def write_transcript(
    anchor: OutputAnchor,
    process: ProcessResult,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    body = _transcript_bytes(process)
    capture: dict[str, Any] = {
        "path": str(anchor.transcript_path),
        "origin": "openada",
        "capture_policy": "bounded complete-or-unknown process streams",
        "stdout_observed_bytes": process.stdout_bytes,
        "stderr_observed_bytes": process.stderr_bytes,
        "stdout_retained_bytes": len(process.stdout.encode("utf-8", errors="replace")),
        "stderr_retained_bytes": len(process.stderr.encode("utf-8", errors="replace")),
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
    try:
        descriptor = os.open(anchor.transcript_name, flags, 0o600, dir_fd=anchor.parent_fd)
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
        capture.update({"status": "valid", "bytes": metadata.st_size, "sha256": digest})
        return (
            {
                "kind": "netgen-transcript",
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


def _standalone_parse(
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
    parser: Callable[[BinaryIO, int], dict[str, Any]],
) -> dict[str, Any]:
    """Parse one stable, real, single-link file without following path aliases."""

    def invalid(reason: str, detail: str) -> dict[str, Any]:
        return {
            "validation": {
                "valid": False,
                "reason": f"{label}.{reason}",
                "detail": detail[:1_000],
            },
            "outcome": "unknown",
        }

    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        absolute = _lexical_absolute(path)
        encoded_path = os.fsencode(absolute)
        encoded_name = os.fsencode(absolute.name)
        if (
            not absolute.name
            or absolute.name in {".", ".."}
            or len(encoded_path) > MAX_PATH_BYTES
            or any(byte < 32 for byte in encoded_name)
        ):
            return invalid("invalid_path", "The evidence path is empty, overlong, or unsafe.")
        parent_fd, signatures = _open_real_directory(
            absolute.parent,
            create_missing=False,
        )
        before = _lstat(absolute.name, dir_fd=parent_fd)
        if before is None:
            return invalid("unreadable", "The evidence file does not exist.")
        if not stat.S_ISREG(before.st_mode):
            return invalid("untrusted_path", "The evidence path is not a real regular file.")
        if before.st_nlink != 1:
            return invalid(
                "untrusted_path",
                "The evidence file must have exactly one hard link.",
            )
        if before.st_size > max_bytes:
            return invalid("too_large", f"The evidence exceeds {max_bytes} bytes.")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(absolute.name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_signature(opened) != _file_signature(before)
        ):
            return invalid("unstable", "The evidence identity changed while opening.")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            parsed = parser(handle, opened.st_size)
        finished = os.fstat(descriptor)
        current = _lstat(absolute.name, dir_fd=parent_fd)
        check_fd, check_signatures = _open_real_directory(
            absolute.parent,
            create_missing=False,
        )
        try:
            parent_stable = bool(
                check_signatures == signatures
                and _directory_signature(os.fstat(check_fd))
                == _directory_signature(os.fstat(parent_fd))
            )
        finally:
            os.close(check_fd)
        if (
            current is None
            or finished.st_nlink != 1
            or current.st_nlink != 1
            or _file_signature(opened) != _file_signature(finished)
            or _file_signature(opened) != _file_signature(current)
            or not parent_stable
        ):
            return invalid("unstable", "The evidence changed while parsing.")
        return parsed
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeEncodeError) as exc:
        return invalid("unreadable", str(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


def parse_netgen_report(
    path: str | Path,
    *,
    expected_cell: str | None = None,
) -> dict[str, Any]:
    return _standalone_parse(
        path,
        label="report",
        max_bytes=MAX_REPORT_BYTES,
        parser=lambda handle, size: parse_netgen_report_stream(
            handle,
            size=size,
            expected_cell=expected_cell,
        ),
    )


def parse_netgen_json(path: str | Path, *, expected_cell: str) -> dict[str, Any]:
    return _standalone_parse(
        path,
        label="json",
        max_bytes=MAX_JSON_BYTES,
        parser=lambda handle, size: parse_netgen_json_stream(
            handle,
            size=size,
            expected_cell=expected_cell,
        ),
    )
