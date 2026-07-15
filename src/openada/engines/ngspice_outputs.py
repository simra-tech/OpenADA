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


@dataclass(slots=True)
class _AxisSummary:
    """Streaming sweep-axis facts used for exact request binding."""

    first: float | None = None
    last: float | None = None
    previous: float | None = None
    linear_step: float | None = None
    log_ratio: float | None = None
    strictly_increasing: bool = True
    linear_uniform: bool = True
    log_uniform: bool = True

    def add(self, value: float) -> None:
        if self.first is None:
            self.first = value
            self.last = value
            self.previous = value
            return
        assert self.previous is not None
        if value <= self.previous:
            self.strictly_increasing = False
        delta = value - self.previous
        if self.linear_step is None:
            self.linear_step = delta
        elif not math.isclose(
            delta,
            self.linear_step,
            rel_tol=1e-8,
            abs_tol=max(1e-15, abs(self.linear_step) * 1e-12),
        ):
            self.linear_uniform = False
        if self.previous > 0 and value > 0:
            ratio = value / self.previous
            if self.log_ratio is None:
                self.log_ratio = ratio
            elif not math.isclose(
                ratio,
                self.log_ratio,
                rel_tol=1e-8,
                abs_tol=1e-12,
            ):
                self.log_uniform = False
        else:
            self.log_uniform = False
        self.previous = value
        self.last = value

    def metadata(self) -> dict[str, object] | None:
        if self.first is None or self.last is None:
            return None
        return {
            "axis_first": self.first,
            "axis_last": self.last,
            "axis_strictly_increasing": self.strictly_increasing,
            "axis_linear_step": (
                self.linear_step if self.linear_uniform else None
            ),
            "axis_log_ratio": self.log_ratio if self.log_uniform else None,
        }


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


def _parse_variable_line(line: bytes, *, index: int, points: int) -> tuple[int, str]:
    fields = line.strip().split()
    if len(fields) < 3 or fields[0] != str(index).encode("ascii"):
        raise _InvalidOutput("raw.variable_table_invalid")

    length = points
    dimension_tokens = [field.lower() for field in fields[3:] if field.lower().startswith(b"dims=")]
    if len(dimension_tokens) > 1:
        raise _InvalidOutput("raw.variable_dimensions_invalid")
    if dimension_tokens:
        length = _parse_dimensions(dimension_tokens[0], points=points)
    name = fields[1].decode("utf-8", errors="replace")
    return length, name


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
) -> dict[str, object] | None:
    active_values = len(variable_lengths)
    expirations: dict[int, int] = {}
    if unpadded:
        for length in variable_lengths:
            expirations[length] = expirations.get(length, 0) + 1

    axis = _AxisSummary()
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
        axis_value = value_parser(first_match.group(2))
        if complex_values:
            assert isinstance(axis_value, tuple)
            axis_real, _axis_imaginary = axis_value
        else:
            assert isinstance(axis_value, float)
            axis_real = axis_value
        axis.add(axis_real)

        for _ in range(active_values - 1):
            value_parser(_read_nonempty_line(reader))
    return axis.metadata()


def _validate_binary_payload(
    handle: BinaryIO,
    *,
    scalar_count: int,
    scalars_per_point: int | None,
) -> dict[str, object] | None:
    remaining = scalar_count * 8
    scalar_index = 0
    axis = _AxisSummary()
    while remaining:
        chunk_size = min(remaining, 65_536)
        chunk = handle.read(chunk_size)
        if len(chunk) != chunk_size:
            raise _InvalidOutput("raw.truncated_binary_payload")
        for (value,) in struct.iter_unpack("=d", chunk):
            if not math.isfinite(value):
                raise _InvalidOutput("raw.non_finite_value")
            if scalars_per_point is not None:
                offset = scalar_index % scalars_per_point
                if offset == 0:
                    axis.add(value)
            scalar_index += 1
        remaining -= chunk_size
    return axis.metadata()


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
    continuation: bool,
) -> tuple[dict[str, object], int, int]:
    header_bytes = len(first_line)
    if header_bytes > limits.max_raw_header_bytes:
        raise _InvalidOutput("raw.header_too_large")
    first_key = _header_key(first_line.partition(b":")[0])
    if b":" not in first_line or (
        first_key != b"title" and not (continuation and first_key == b"plotname")
    ):
        raise _InvalidOutput("raw.plot_start_invalid")

    required: dict[bytes, bytes] = {}
    if first_key == b"plotname":
        required[b"plotname"] = first_line.partition(b":")[2].strip()
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
    variable_names: list[str] = []
    for index in range(variables):
        line = reader.readline()
        if not line:
            raise _InvalidOutput("raw.truncated_variable_table")
        header_bytes += len(line)
        if header_bytes > limits.max_raw_header_bytes:
            raise _InvalidOutput("raw.header_too_large")
        length, name = _parse_variable_line(line, index=index, points=points)
        variable_lengths.append(length)
        variable_names.append(name)

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
    axis_metadata: dict[str, object] | None
    if encoding == "binary":
        axis_metadata = _validate_binary_payload(
            reader.handle,
            scalar_count=scalar_count,
            scalars_per_point=(
                variables * (2 if complex_values else 1)
                if not unpadded
                else None
            ),
        )
    else:
        axis_metadata = _validate_ascii_payload(
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
        "independent_variable": variable_names[0],
    }
    if axis_metadata is not None:
        plot.update(axis_metadata)
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
                    continuation=bool(plots),
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


def _analysis_plot_matches(plotname: object, analysis_type: str) -> bool:
    normalized = " ".join(str(plotname).strip().casefold().split())
    if analysis_type == "op":
        return normalized in {"operating point", "dc operating point"}
    if analysis_type == "dc":
        return normalized == "dc transfer characteristic" or (
            normalized.startswith("dc sweep:")
            and normalized.endswith("dc transfer characteristic")
        )
    if analysis_type == "ac":
        return normalized == "ac analysis"
    if analysis_type == "tran":
        return normalized == "transient analysis"
    return False


def _axis_matches(observed: object, expected: object) -> bool:
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        return False
    observed_value = float(observed)
    expected_value = float(expected)
    return math.isfinite(observed_value) and math.isfinite(expected_value) and math.isclose(
        observed_value,
        expected_value,
        rel_tol=1e-8,
        abs_tol=max(1e-15, abs(expected_value) * 1e-12),
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _grid_intervals(span_in_steps: float) -> int | None:
    if not math.isfinite(span_in_steps) or span_in_steps < 0:
        return None
    nearest = round(span_in_steps)
    if math.isclose(span_in_steps, nearest, rel_tol=1e-11, abs_tol=1e-11):
        return int(nearest)
    return math.floor(span_in_steps)


def _expected_dc_axis(
    analysis: dict[str, object],
) -> tuple[int, float, float] | None:
    start = _finite_float(analysis.get("start"))
    stop = _finite_float(analysis.get("stop"))
    step = _finite_float(analysis.get("step"))
    if start is None or stop is None or step is None or stop <= start or step <= 0:
        return None
    intervals = _grid_intervals((stop - start) / step)
    if intervals is None:
        return None
    return intervals + 1, start + intervals * step, step


def _expected_ac_axis(
    analysis: dict[str, object],
    *,
    format_name: object,
) -> tuple[int, float, float | None, float | None] | None:
    start = _finite_float(analysis.get("start_hz"))
    stop = _finite_float(analysis.get("stop_hz"))
    points = analysis.get("points")
    sweep = analysis.get("sweep")
    if (
        start is None
        or stop is None
        or start <= 0
        or stop <= start
        or isinstance(points, bool)
        or not isinstance(points, int)
        or points <= 0
        or sweep not in {"lin", "dec", "oct"}
    ):
        return None
    if sweep == "lin":
        if points == 1:
            return 1, start, None, None
        return points, stop, (stop - start) / (points - 1), None
    base = 10.0 if sweep == "dec" else 2.0
    intervals = _grid_intervals(points * math.log(stop / start, base))
    if intervals is None:
        return None
    if intervals == 0:
        return 1, start, None, None
    if sweep == "dec" and format_name == "ngspice-raw":
        # ngspice chooses the floor-derived point count but redistributes DEC
        # points uniformly in log space so the final point lands on stop.
        ratio = (stop / start) ** (1.0 / intervals)
        return intervals + 1, stop, None, ratio
    ratio = base ** (1.0 / points)
    return intervals + 1, start * (ratio**intervals), None, ratio


def _normalized_axis_name(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def analysis_raw_counts(
    capture: dict[str, object],
    analysis: dict[str, object],
) -> tuple[int, int, int] | None:
    """Bind one validated raw plot to a closed circuit.simulate analysis.

    Structural validation proves that every retained numeric scalar is finite.
    This second, deliberately narrow check selects the requested native plot,
    rejects a mismatched numeric representation, and binds DC/AC sweep bounds
    to the typed request.
    """

    validation = capture.get("validation")
    metadata = validation.get("metadata") if isinstance(validation, dict) else None
    plots = metadata.get("plots") if isinstance(metadata, dict) else None
    analysis_type = analysis.get("type")
    if not isinstance(plots, list) or analysis_type not in {"op", "dc", "ac", "tran"}:
        return None
    matching = [
        plot
        for plot in plots
        if isinstance(plot, dict)
        and _analysis_plot_matches(plot.get("plotname"), str(analysis_type))
    ]
    if len(matching) != 1:
        return None
    plot = matching[0]
    points = plot.get("points")
    variables = plot.get("variables")
    numeric_type = plot.get("numeric_type")
    if (
        isinstance(points, bool)
        or not isinstance(points, int)
        or points <= 0
        or isinstance(variables, bool)
        or not isinstance(variables, int)
        or plot.get("unpadded") is not False
        or numeric_type not in {"real", "complex"}
    ):
        return None
    if analysis_type == "ac":
        if numeric_type != "complex" or variables < 2:
            return None
        expected = _expected_ac_axis(
            analysis,
            format_name=metadata.get("format"),
        )
        if expected is None:
            return None
        expected_points, expected_last, expected_step, expected_ratio = expected
        if points != expected_points or plot.get("axis_strictly_increasing") is not True:
            return None
        if _normalized_axis_name(plot.get("independent_variable")) != "frequency":
            return None
        if not _axis_matches(plot.get("axis_first"), analysis.get("start_hz")):
            return None
        if not _axis_matches(plot.get("axis_last"), expected_last):
            return None
        if expected_step is not None and not _axis_matches(
            plot.get("axis_linear_step"), expected_step
        ):
            return None
        if expected_ratio is not None and not _axis_matches(
            plot.get("axis_log_ratio"), expected_ratio
        ):
            return None
    elif analysis_type == "dc":
        if numeric_type != "real" or variables < 2:
            return None
        expected = _expected_dc_axis(analysis)
        if expected is None:
            return None
        expected_points, expected_last, expected_step = expected
        if points != expected_points or plot.get("axis_strictly_increasing") is not True:
            return None
        source_name = _normalized_axis_name(analysis.get("source_name"))
        axis_name = _normalized_axis_name(plot.get("independent_variable"))
        if not source_name or (axis_name != "sweep" and not axis_name.endswith(source_name)):
            return None
        if not _axis_matches(plot.get("axis_first"), analysis.get("start")):
            return None
        if not _axis_matches(plot.get("axis_last"), expected_last):
            return None
        if expected_points > 1 and not _axis_matches(
            plot.get("axis_linear_step"), expected_step
        ):
            return None
    elif analysis_type == "tran":
        if numeric_type != "real" or variables < 2:
            return None
        if _normalized_axis_name(plot.get("independent_variable")) != "time":
            return None
        if points > 1 and plot.get("axis_strictly_increasing") is not True:
            return None
    else:
        if numeric_type != "real" or points != 1 or variables < 1:
            return None

    dependent_variables = variables if analysis_type == "op" else variables - 1
    scalar_width = 2 if numeric_type == "complex" else 1
    return points, dependent_variables, points * dependent_variables * scalar_width


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
    "analysis_raw_counts",
    "validate_ngspice_raw",
    "validate_ngspice_wrdata",
    "validate_xyce_raw",
]
