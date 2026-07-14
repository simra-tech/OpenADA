"""Bounded validators for native SPICE-family output files.

These validators establish that an output is a structurally complete native
artifact.  They deliberately do not interpret a waveform as an engineering
pass or failure.  Files are treated as untrusted: parsing is streaming, all
lines and counts are bounded, non-regular files are rejected, and numeric data
must be finite.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import stat
import struct
from typing import BinaryIO


_FLOAT_RE = re.compile(
    rb"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eEdD][+-]?\d+)?"
)
_COMPLEX_RE = re.compile(
    rb"\(?\s*("
    + _FLOAT_RE.pattern
    + rb")\s*,\s*("
    + _FLOAT_RE.pattern
    + rb")\s*\)?"
)
_CONSTANT_PLOT_NAMES = frozenset({"const", "constant values", "constants"})


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """Resource limits shared by the native-output validators."""

    max_file_bytes: int = 268_435_456
    max_line_bytes: int = 65_536
    max_raw_header_bytes: int = 1_048_576
    max_raw_plotname_bytes: int = 1_024
    max_raw_plots: int = 128
    max_raw_variables: int = 100_000
    max_raw_points: int = 100_000_000
    max_numeric_values: int = 33_554_432
    max_wrdata_rows: int = 25_000_000
    max_wrdata_columns: int = 16_384
    max_wrdata_headers: int = 100_000
    max_wrdata_sections: int = 100_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_LIMITS = ValidationLimits()


@dataclass(frozen=True, slots=True)
class OutputValidation:
    """A compact, JSON-serializable native-output validation result."""

    valid: bool
    reason: str
    metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "metadata": self.metadata,
        }


class _InvalidOutput(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BoundedLineReader:
    def __init__(self, handle: BinaryIO, max_line_bytes: int, reason_prefix: str) -> None:
        self.handle = handle
        self.max_line_bytes = max_line_bytes
        self.reason_prefix = reason_prefix

    def readline(self) -> bytes:
        line = self.handle.readline(self.max_line_bytes + 1)
        if len(line) > self.max_line_bytes:
            raise _InvalidOutput(f"{self.reason_prefix}.line_too_long")
        return line


def _base_metadata(format_name: str, size: int | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {"format": format_name}
    if size is not None:
        metadata["bytes"] = size
    return metadata


def _invalid(
    reason: str,
    *,
    format_name: str,
    size: int | None = None,
    metadata: dict[str, object] | None = None,
) -> OutputValidation:
    details = _base_metadata(format_name, size)
    if metadata:
        details.update(metadata)
    return OutputValidation(False, reason, details)


def _open_regular_file(
    path: str | Path,
    *,
    limits: ValidationLimits,
    format_name: str,
    dir_fd: int | None = None,
) -> tuple[BinaryIO, os.stat_result] | OutputValidation:
    candidate = Path(path)
    try:
        path_stat = os.stat(candidate, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _invalid("file.not_found", format_name=format_name)
    except (OSError, ValueError):
        return _invalid("file.unreadable", format_name=format_name)
    if not stat.S_ISREG(path_stat.st_mode):
        return _invalid(
            "file.not_regular",
            format_name=format_name,
            size=path_stat.st_size,
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(candidate, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return _invalid("file.not_found", format_name=format_name)
    except (OSError, ValueError):
        return _invalid("file.unreadable", format_name=format_name)

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            return _invalid(
                "file.not_regular",
                format_name=format_name,
                size=file_stat.st_size,
            )
        if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            os.close(descriptor)
            return _invalid(
                "file.changed_before_validation",
                format_name=format_name,
                size=file_stat.st_size,
            )
        if file_stat.st_size == 0:
            os.close(descriptor)
            return _invalid("file.empty", format_name=format_name, size=0)
        if file_stat.st_size > limits.max_file_bytes:
            os.close(descriptor)
            return _invalid(
                "file.too_large",
                format_name=format_name,
                size=file_stat.st_size,
            )
        return os.fdopen(descriptor, "rb", closefd=True), file_stat
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        return _invalid("file.unreadable", format_name=format_name)


def _parse_count(value: bytes, *, reason: str, maximum: int) -> int:
    stripped = value.strip()
    maximum_text = str(maximum).encode("ascii")
    if (
        not stripped.isdigit()
        or len(stripped) > len(maximum_text)
        or (len(stripped) == len(maximum_text) and stripped > maximum_text)
    ):
        raise _InvalidOutput(reason)
    parsed = int(stripped)
    if parsed <= 0 or parsed > maximum:
        raise _InvalidOutput(reason)
    return parsed


def _header_key(value: bytes) -> bytes:
    return b" ".join(value.strip().lower().split())


def _file_changed(initial: os.stat_result, final: os.stat_result) -> bool:
    return (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
        initial.st_ctime_ns,
    ) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    )


def _parse_dimensions(token: bytes, *, points: int) -> int:
    raw_dimensions = token[5:]
    if not raw_dimensions:
        raise _InvalidOutput("raw.variable_dimensions_invalid")
    dimensions = raw_dimensions.split(b",")
    length = 1
    for dimension in dimensions:
        points_text = str(points).encode("ascii")
        if (
            not dimension.isdigit()
            or len(dimension) > len(points_text)
            or (len(dimension) == len(points_text) and dimension > points_text)
        ):
            raise _InvalidOutput("raw.variable_dimensions_invalid")
        parsed = int(dimension)
        if parsed <= 0 or parsed > points or length > points // parsed:
            raise _InvalidOutput("raw.variable_dimensions_invalid")
        length *= parsed
    if length > points:
        raise _InvalidOutput("raw.variable_dimensions_invalid")
    return length


def _parse_variable_line(line: bytes, *, index: int, points: int) -> int:
    fields = line.strip().split()
    if len(fields) < 3 or fields[0] != str(index).encode("ascii"):
        raise _InvalidOutput("raw.variable_table_invalid")

    length = points
    dimension_tokens = [field.lower() for field in fields[3:] if field.lower().startswith(b"dims=")]
    if len(dimension_tokens) > 1:
        raise _InvalidOutput("raw.variable_dimensions_invalid")
    if dimension_tokens:
        length = _parse_dimensions(dimension_tokens[0], points=points)
    return length


def _parse_real(value: bytes) -> float:
    stripped = value.strip()
    if not _FLOAT_RE.fullmatch(stripped):
        raise _InvalidOutput("raw.ascii_value_invalid")
    parsed = float(stripped.replace(b"d", b"e").replace(b"D", b"E"))
    if not math.isfinite(parsed):
        raise _InvalidOutput("raw.non_finite_value")
    return parsed


def _parse_complex(value: bytes) -> tuple[float, float]:
    match = _COMPLEX_RE.fullmatch(value.strip())
    if not match:
        raise _InvalidOutput("raw.ascii_value_invalid")
    real = float(match.group(1).replace(b"d", b"e").replace(b"D", b"E"))
    imaginary = float(match.group(2).replace(b"d", b"e").replace(b"D", b"E"))
    if not math.isfinite(real) or not math.isfinite(imaginary):
        raise _InvalidOutput("raw.non_finite_value")
    return real, imaginary


def _read_nonempty_line(reader: _BoundedLineReader) -> bytes:
    while True:
        line = reader.readline()
        if not line:
            raise _InvalidOutput("raw.truncated_ascii_payload")
        if line.strip():
            return line


def _validate_ascii_payload(
    reader: _BoundedLineReader,
    *,
    points: int,
    variable_lengths: list[int],
    complex_values: bool,
    unpadded: bool,
) -> None:
    active_values = len(variable_lengths)
    expirations: dict[int, int] = {}
    if unpadded:
        for length in variable_lengths:
            expirations[length] = expirations.get(length, 0) + 1

    for point_index in range(points):
        if unpadded:
            active_values -= expirations.get(point_index, 0)
        if active_values <= 0:
            raise _InvalidOutput("raw.variable_dimensions_invalid")

        first_line = _read_nonempty_line(reader).strip()
        first_match = re.fullmatch(rb"(\d+)\s+(.+)", first_line)
        if not first_match or first_match.group(1) != str(point_index).encode("ascii"):
            raise _InvalidOutput("raw.ascii_point_index_invalid")
        value_parser = _parse_complex if complex_values else _parse_real
        value_parser(first_match.group(2))

        for _ in range(active_values - 1):
            value_parser(_read_nonempty_line(reader))


def _validate_binary_payload(
    handle: BinaryIO,
    *,
    scalar_count: int,
) -> None:
    remaining = scalar_count * 8
    while remaining:
        chunk_size = min(remaining, 65_536)
        chunk = handle.read(chunk_size)
        if len(chunk) != chunk_size:
            raise _InvalidOutput("raw.truncated_binary_payload")
        for (value,) in struct.iter_unpack("=d", chunk):
            if not math.isfinite(value):
                raise _InvalidOutput("raw.non_finite_value")
        remaining -= chunk_size


def _next_plot_start(reader: _BoundedLineReader) -> bytes:
    while True:
        line = reader.readline()
        if not line or line.strip():
            return line


def _parse_raw_plot(
    reader: _BoundedLineReader,
    first_line: bytes,
    *,
    limits: ValidationLimits,
) -> tuple[dict[str, object], int, int]:
    header_bytes = len(first_line)
    if header_bytes > limits.max_raw_header_bytes:
        raise _InvalidOutput("raw.header_too_large")
    if _header_key(first_line.partition(b":")[0]) != b"title" or b":" not in first_line:
        raise _InvalidOutput("raw.plot_start_invalid")

    required: dict[bytes, bytes] = {}
    while True:
        line = reader.readline()
        if not line:
            raise _InvalidOutput("raw.truncated_header")
        header_bytes += len(line)
        if header_bytes > limits.max_raw_header_bytes:
            raise _InvalidOutput("raw.header_too_large")
        stripped = line.strip()
        if _header_key(stripped.partition(b":")[0]) == b"variables" and b":" in stripped:
            break
        if b":" not in stripped:
            raise _InvalidOutput("raw.header_invalid")
        key, _, value = stripped.partition(b":")
        normalized_key = _header_key(key)
        if normalized_key in {
            b"plotname",
            b"flags",
            b"no. variables",
            b"no. points",
        }:
            if normalized_key in required:
                raise _InvalidOutput("raw.header_duplicate_field")
            required[normalized_key] = value.strip()

    if set(required) != {b"plotname", b"flags", b"no. variables", b"no. points"}:
        raise _InvalidOutput("raw.header_missing_field")
    plot_name_bytes = required[b"plotname"].strip()
    if not plot_name_bytes or len(plot_name_bytes) > limits.max_raw_plotname_bytes:
        raise _InvalidOutput("raw.plotname_invalid")
    plot_name = plot_name_bytes.decode("utf-8", errors="replace")
    variables = _parse_count(
        required[b"no. variables"],
        reason="raw.variable_count_invalid",
        maximum=limits.max_raw_variables,
    )
    points = _parse_count(
        required[b"no. points"],
        reason="raw.point_count_invalid",
        maximum=limits.max_raw_points,
    )

    flags = set(required[b"flags"].lower().split())
    complex_values = b"complex" in flags
    if complex_values == (b"real" in flags):
        raise _InvalidOutput("raw.numeric_type_invalid")
    if b"padded" in flags and b"unpadded" in flags:
        raise _InvalidOutput("raw.padding_flags_invalid")
    unpadded = b"unpadded" in flags

    variable_lengths: list[int] = []
    for index in range(variables):
        line = reader.readline()
        if not line:
            raise _InvalidOutput("raw.truncated_variable_table")
        header_bytes += len(line)
        if header_bytes > limits.max_raw_header_bytes:
            raise _InvalidOutput("raw.header_too_large")
        variable_lengths.append(_parse_variable_line(line, index=index, points=points))

    marker = reader.readline()
    if not marker:
        raise _InvalidOutput("raw.truncated_header")
    header_bytes += len(marker)
    if header_bytes > limits.max_raw_header_bytes:
        raise _InvalidOutput("raw.header_too_large")
    marker_name = _header_key(marker.strip().partition(b":")[0])
    if b":" not in marker or marker_name not in {b"binary", b"values"}:
        raise _InvalidOutput("raw.data_marker_invalid")

    value_count = sum(variable_lengths) if unpadded else variables * points
    if value_count <= 0 or value_count > limits.max_numeric_values:
        raise _InvalidOutput("raw.value_count_too_large")
    scalar_count = value_count * (2 if complex_values else 1)
    if scalar_count > limits.max_numeric_values:
        raise _InvalidOutput("raw.value_count_too_large")

    encoding = "binary" if marker_name == b"binary" else "ascii"
    if encoding == "binary":
        _validate_binary_payload(reader.handle, scalar_count=scalar_count)
    else:
        _validate_ascii_payload(
            reader,
            points=points,
            variable_lengths=variable_lengths,
            complex_values=complex_values,
            unpadded=unpadded,
        )

    plot = {
        "plotname": plot_name,
        "encoding": encoding,
        "numeric_type": "complex" if complex_values else "real",
        "variables": variables,
        "points": points,
        "values": value_count,
        "unpadded": unpadded,
    }
    return plot, value_count, scalar_count


def _validate_spice3_raw(
    path: str | Path,
    *,
    format_name: str,
    limits: ValidationLimits = DEFAULT_LIMITS,
    dir_fd: int | None = None,
) -> OutputValidation:
    opened = _open_regular_file(
        path,
        limits=limits,
        format_name=format_name,
        dir_fd=dir_fd,
    )
    if isinstance(opened, OutputValidation):
        return opened
    handle, initial_stat = opened
    plots: list[dict[str, object]] = []
    value_count = 0
    scalar_count = 0
    metadata: dict[str, object] = {
        "plot_count": 0,
        "analysis_plot_count": 0,
        "has_analysis_plot": False,
        "value_count": 0,
        "numeric_scalar_count": 0,
        "plots": plots,
    }

    try:
        with handle:
            reader = _BoundedLineReader(handle, limits.max_line_bytes, "raw")
            first_line = _next_plot_start(reader)
            while first_line:
                if len(plots) >= limits.max_raw_plots:
                    raise _InvalidOutput("raw.too_many_plots")
                plot, plot_values, plot_scalars = _parse_raw_plot(
                    reader,
                    first_line,
                    limits=limits,
                )
                if value_count > limits.max_numeric_values - plot_values:
                    raise _InvalidOutput("raw.value_count_too_large")
                if scalar_count > limits.max_numeric_values - plot_scalars:
                    raise _InvalidOutput("raw.value_count_too_large")
                plots.append(plot)
                value_count += plot_values
                scalar_count += plot_scalars
                first_line = _next_plot_start(reader)

            final_stat = os.fstat(handle.fileno())
            if _file_changed(initial_stat, final_stat):
                raise _InvalidOutput("file.changed_during_validation")

        analysis_plot_count = sum(
            str(plot["plotname"]).strip().casefold() not in _CONSTANT_PLOT_NAMES
            for plot in plots
        )
        metadata.update(
            {
                "plot_count": len(plots),
                "analysis_plot_count": analysis_plot_count,
                "has_analysis_plot": analysis_plot_count > 0,
                "value_count": value_count,
                "numeric_scalar_count": scalar_count,
            }
        )
        if not plots:
            return _invalid(
                "raw.no_plots",
                format_name=format_name,
                size=initial_stat.st_size,
                metadata=metadata,
            )
        if analysis_plot_count == 0:
            return _invalid(
                "raw.constants_only",
                format_name=format_name,
                size=initial_stat.st_size,
                metadata=metadata,
            )
        details = _base_metadata(format_name, initial_stat.st_size)
        details.update(metadata)
        return OutputValidation(True, "valid", details)
    except _InvalidOutput as error:
        analysis_plot_count = sum(
            str(plot["plotname"]).strip().casefold() not in _CONSTANT_PLOT_NAMES
            for plot in plots
        )
        metadata.update(
            {
                "plot_count": len(plots),
                "analysis_plot_count": analysis_plot_count,
                "has_analysis_plot": analysis_plot_count > 0,
                "value_count": value_count,
                "numeric_scalar_count": scalar_count,
            }
        )
        return _invalid(
            error.reason,
            format_name=format_name,
            size=initial_stat.st_size,
            metadata=metadata,
        )
    except OSError:
        return _invalid(
            "file.read_error",
            format_name=format_name,
            size=initial_stat.st_size,
            metadata=metadata,
        )


def validate_ngspice_raw(
    path: str | Path,
    *,
    limits: ValidationLimits = DEFAULT_LIMITS,
    dir_fd: int | None = None,
) -> OutputValidation:
    """Validate a binary or ASCII Spice3f5/ngspice raw file.

    Consecutive plots and ``appendwrite`` files are supported.  A raw file is
    useful simulation evidence only when it contains at least one plot other
    than ngspice's built-in ``constants`` plot.
    """

    return _validate_spice3_raw(
        path,
        format_name="ngspice-raw",
        limits=limits,
        dir_fd=dir_fd,
    )


def validate_xyce_raw(
    path: str | Path,
    *,
    limits: ValidationLimits = DEFAULT_LIMITS,
    dir_fd: int | None = None,
) -> OutputValidation:
    """Validate a complete Xyce ASCII Spice raw analysis artifact."""

    return _validate_spice3_raw(
        path,
        format_name="xyce-raw",
        limits=limits,
        dir_fd=dir_fd,
    )


def _wrdata_number(token: bytes) -> float | None:
    if token.lower() in {
        b"+inf",
        b"+infinity",
        b"-inf",
        b"-infinity",
        b"inf",
        b"infinity",
        b"nan",
    }:
        return float(token)
    if not _FLOAT_RE.fullmatch(token):
        return None
    return float(token.replace(b"d", b"e").replace(b"D", b"E"))


def validate_ngspice_wrdata(
    path: str | Path,
    *,
    limits: ValidationLimits = DEFAULT_LIMITS,
    dir_fd: int | None = None,
) -> OutputValidation:
    """Validate an ngspice ``wrdata`` ASCII table without loading it in memory."""

    format_name = "ngspice-wrdata"
    opened = _open_regular_file(
        path,
        limits=limits,
        format_name=format_name,
        dir_fd=dir_fd,
    )
    if isinstance(opened, OutputValidation):
        return opened
    handle, initial_stat = opened
    row_count = 0
    numeric_value_count = 0
    header_count = 0
    section_count = 0
    column_counts: set[int] = set()
    current_columns: int | None = None
    pending_header_columns: int | None = None

    def metadata() -> dict[str, object]:
        return {
            "row_count": row_count,
            "numeric_value_count": numeric_value_count,
            "header_row_count": header_count,
            "section_count": section_count,
            "shape_count": len(column_counts),
            "column_count_min": min(column_counts) if column_counts else 0,
            "column_count_max": max(column_counts) if column_counts else 0,
        }

    try:
        with handle:
            reader = _BoundedLineReader(handle, limits.max_line_bytes, "wrdata")
            while True:
                line = reader.readline()
                if not line:
                    break
                tokens = line.split()
                if not tokens:
                    current_columns = None
                    continue
                if len(tokens) > limits.max_wrdata_columns:
                    raise _InvalidOutput("wrdata.too_many_columns")

                parsed = [_wrdata_number(token) for token in tokens]
                numeric_tokens = sum(value is not None for value in parsed)
                if numeric_tokens == 0:
                    if len(tokens) < 2:
                        raise _InvalidOutput("wrdata.header_invalid")
                    if pending_header_columns is not None:
                        raise _InvalidOutput("wrdata.header_without_data")
                    header_count += 1
                    if header_count > limits.max_wrdata_headers:
                        raise _InvalidOutput("wrdata.too_many_headers")
                    pending_header_columns = len(tokens)
                    current_columns = None
                    continue
                if numeric_tokens != len(tokens):
                    raise _InvalidOutput("wrdata.mixed_row")
                if len(tokens) < 2:
                    raise _InvalidOutput("wrdata.row_shape_invalid")
                if any(value is None or not math.isfinite(value) for value in parsed):
                    raise _InvalidOutput("wrdata.non_finite_value")

                if pending_header_columns is not None:
                    if len(tokens) != pending_header_columns:
                        raise _InvalidOutput("wrdata.header_shape_mismatch")
                    current_columns = len(tokens)
                    pending_header_columns = None
                    section_count += 1
                elif current_columns is None:
                    current_columns = len(tokens)
                    section_count += 1
                elif len(tokens) != current_columns:
                    # appendwrite does not emit a delimiter when wr_vecnames is
                    # disabled.  A finite width transition is therefore the
                    # only observable boundary between appended tables.
                    current_columns = len(tokens)
                    section_count += 1
                if section_count > limits.max_wrdata_sections:
                    raise _InvalidOutput("wrdata.too_many_sections")

                row_count += 1
                if row_count > limits.max_wrdata_rows:
                    raise _InvalidOutput("wrdata.too_many_rows")
                if numeric_value_count > limits.max_numeric_values - len(tokens):
                    raise _InvalidOutput("wrdata.too_many_values")
                numeric_value_count += len(tokens)
                column_counts.add(len(tokens))

            if pending_header_columns is not None:
                raise _InvalidOutput("wrdata.header_without_data")
            final_stat = os.fstat(handle.fileno())
            if _file_changed(initial_stat, final_stat):
                raise _InvalidOutput("file.changed_during_validation")

        if row_count == 0 or numeric_value_count == 0:
            raise _InvalidOutput("wrdata.no_finite_data")
        details = _base_metadata(format_name, initial_stat.st_size)
        details.update(metadata())
        return OutputValidation(True, "valid", details)
    except _InvalidOutput as error:
        return _invalid(
            error.reason,
            format_name=format_name,
            size=initial_stat.st_size,
            metadata=metadata(),
        )
    except OSError:
        return _invalid(
            "file.read_error",
            format_name=format_name,
            size=initial_stat.st_size,
            metadata=metadata(),
        )


__all__ = [
    "DEFAULT_LIMITS",
    "OutputValidation",
    "ValidationLimits",
    "validate_ngspice_raw",
    "validate_ngspice_wrdata",
    "validate_xyce_raw",
]
