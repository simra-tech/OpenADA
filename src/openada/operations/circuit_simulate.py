"""Shared circuit.simulate semantics for ngspice and Xyce."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
from typing import Mapping
import uuid

from ..contract import (
    FileRecordError,
    diagnostic,
    file_record,
    result,
    static_execution,
    stable_regular_file,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..driver_registry import (
    CIRCUIT_SIMULATE_PROFILE,
    SIMULATION_EVIDENCE_ASSERTION,
    BuiltinDriver,
    analysis_feature,
    builtin_driver,
)
from ..engines.ngspice_outputs import analysis_raw_counts
from ..engines.spice import MAX_SOURCE_BYTES


MAX_SOURCE_LINE_BYTES = 65_536
MAX_SHARED_ANALYSIS_POINTS = 1_000_000
_ANALYSIS_RE = re.compile(
    r"^\s*\.(op|dc|ac|tran|noise|hb|tf|pz|sens|sp|disto)\b",
    re.IGNORECASE,
)
_INCLUDE_RE = re.compile(r"^\s*\.(?:inc(?:lude)?|lib)\b", re.IGNORECASE)
_UNSUPPORTED_RE = re.compile(
    r"^\s*\.(control|measure|meas|print|four|fft|step)\b", re.IGNORECASE
)
_OP_RE = re.compile(r"^\s*\.op\s*$", re.IGNORECASE)
_DC_RE = re.compile(r"^\s*\.dc\s+(.+?)\s*$", re.IGNORECASE)
_AC_RE = re.compile(r"^\s*\.ac\s+(.+?)\s*$", re.IGNORECASE)
_TRAN_RE = re.compile(r"^\s*\.tran\s+(.+?)\s*$", re.IGNORECASE)
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:+-]*$")
_EXTENSION_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$")
_SPICE_NUMBER_RE = re.compile(
    r"^(?P<number>[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>[A-Za-z]*)$"
)
_SPICE_SUFFIXES = {
    "": 1.0,
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}


def _spice_number(token: str) -> float | None:
    match = _SPICE_NUMBER_RE.fullmatch(token)
    if match is None:
        return None
    suffix = match.group("suffix").casefold()
    factor = 1.0 if suffix in {"", "s"} else None
    for candidate in ("meg", "t", "g", "k", "m", "u", "n", "p", "f"):
        if suffix.startswith(candidate):
            factor = _SPICE_SUFFIXES[candidate]
            break
    if factor is None:
        return None
    try:
        value = float(match.group("number")) * factor
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _grid_intervals(span_in_steps: float) -> int | None:
    if not math.isfinite(span_in_steps) or span_in_steps < 0:
        return None
    nearest = round(span_in_steps)
    if math.isclose(span_in_steps, nearest, rel_tol=1e-11, abs_tol=1e-11):
        return int(nearest)
    return math.floor(span_in_steps)


def _covering_grid_intervals(span_in_steps: float) -> int | None:
    """Return intervals needed to cover a domain without undercounting its end."""

    if not math.isfinite(span_in_steps) or span_in_steps < 0:
        return None
    nearest = round(span_in_steps)
    if math.isclose(span_in_steps, nearest, rel_tol=1e-11, abs_tol=1e-11):
        return int(nearest)
    return math.ceil(span_in_steps)


def _declared_analysis_points(analysis: Mapping[str, object]) -> int | None:
    """Return the exact sweep count or declared nominal transient-grid count."""

    analysis_type = analysis.get("type")
    try:
        if analysis_type == "op":
            return 1
        if analysis_type == "dc":
            intervals = _grid_intervals(
                (float(analysis["stop"]) - float(analysis["start"]))
                / float(analysis["step"])
            )
            return None if intervals is None else intervals + 1
        if analysis_type == "ac":
            points = int(analysis["points"])
            if analysis.get("sweep") == "lin":
                return points
            base = 10.0 if analysis.get("sweep") == "dec" else 2.0
            intervals = _grid_intervals(
                points
                * math.log(
                    float(analysis["stop_hz"]) / float(analysis["start_hz"]),
                    base,
                )
            )
            return None if intervals is None else intervals + 1
        if analysis_type == "tran":
            start = float(analysis.get("start_s", 0.0))
            step = float(analysis["step_s"])
            max_step = float(analysis.get("max_step_s", step))
            interval = min(step, max_step)
            intervals = _covering_grid_intervals(
                (float(analysis["stop_s"]) - start) / interval
            )
            return None if intervals is None else intervals + 1
    except (KeyError, OverflowError, TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _analysis_within_point_limit(analysis: Mapping[str, object]) -> bool:
    points = _declared_analysis_points(analysis)
    return points is not None and 1 <= points <= MAX_SHARED_ANALYSIS_POINTS


def _parse_analysis_parameters(
    analysis: str,
    line: str,
) -> dict[str, object] | None:
    if analysis == "op":
        if _OP_RE.match(line) is None:
            return None
        analysis_parameters = {"type": "op", "extensions": {}}
    elif analysis == "dc":
        match = _DC_RE.match(line)
        arguments = match.group(1).split() if match else []
        if (
            len(arguments) != 4
            or len(arguments[0]) > 256
            or _SOURCE_NAME_RE.fullmatch(arguments[0]) is None
        ):
            return None
        source_name = arguments[0]
        source_unit = {"v": "V", "i": "A"}.get(source_name[0].casefold())
        values = [_spice_number(token) for token in arguments[1:]]
        if source_unit is None or any(value is None for value in values):
            return None
        start, stop, step = (float(value) for value in values if value is not None)
        if not start < stop or step <= 0:
            return None
        analysis_parameters = {
            "type": "dc",
            "source_name": source_name,
            "source_unit": source_unit,
            "start": start,
            "stop": stop,
            "step": step,
            "extensions": {},
        }
    elif analysis == "ac":
        match = _AC_RE.match(line)
        arguments = match.group(1).split() if match else []
        if len(arguments) != 4 or arguments[0].casefold() not in {"lin", "dec", "oct"}:
            return None
        try:
            points = int(arguments[1])
        except ValueError:
            return None
        start_hz = _spice_number(arguments[2])
        stop_hz = _spice_number(arguments[3])
        if (
            str(points) != arguments[1]
            or not 1 <= points <= MAX_SHARED_ANALYSIS_POINTS
            or start_hz is None
            or stop_hz is None
            or start_hz <= 0
            or start_hz >= stop_hz
        ):
            return None
        analysis_parameters = {
            "type": "ac",
            "sweep": arguments[0].casefold(),
            "points": points,
            "start_hz": float(start_hz),
            "stop_hz": float(stop_hz),
            "extensions": {},
        }
    elif analysis == "tran":
        match = _TRAN_RE.match(line)
        arguments = match.group(1).split() if match else []
        if not 2 <= len(arguments) <= 4:
            return None
        parsed = [_spice_number(token) for token in arguments]
        if any(value is None for value in parsed):
            return None
        values = [float(value) for value in parsed if value is not None]
        step_s, stop_s = values[:2]
        start_s = values[2] if len(values) >= 3 else 0.0
        max_step_s = values[3] if len(values) == 4 else None
        if not (
            step_s > 0
            and stop_s > 0
            and 0 <= start_s < stop_s
            and (max_step_s is None or 0 < max_step_s <= stop_s - start_s)
        ):
            return None
        analysis_parameters = {
            "type": "tran",
            "step_s": step_s,
            "stop_s": stop_s,
            "extensions": {},
        }
        if len(values) >= 3:
            analysis_parameters["start_s"] = start_s
        if max_step_s is not None:
            analysis_parameters["max_step_s"] = max_step_s
    else:
        return None
    if not _analysis_within_point_limit(analysis_parameters):
        return None
    return {"analysis": analysis_parameters, "extensions": {}}


def inspect_simulation_deck(path: str | Path) -> dict[str, object]:
    """Inspect the bounded common subset without interpreting includes."""

    source = Path(path)
    analyses: list[str] = []
    unsupported: list[str] = []
    includes = False
    line_too_long = False
    source_too_large = False
    source_unstable = False
    analysis_lines: list[str] = []
    try:
        with stable_regular_file(source) as (handle, opened):
            if opened.st_size > MAX_SOURCE_BYTES:
                source_too_large = True
            else:
                line_number = 0
                source_bytes = 0
                while True:
                    raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                    if not raw_line:
                        break
                    source_bytes += len(raw_line)
                    if source_bytes > MAX_SOURCE_BYTES:
                        source_too_large = True
                        break
                    line_number += 1
                    if len(raw_line) > MAX_SOURCE_LINE_BYTES and not raw_line.endswith(b"\n"):
                        line_too_long = True
                        while raw_line and not raw_line.endswith(b"\n"):
                            raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                            source_bytes += len(raw_line)
                            if source_bytes > MAX_SOURCE_BYTES:
                                source_too_large = True
                                break
                        if source_too_large:
                            break
                        continue
                    if line_number == 1:
                        continue
                    line = raw_line.decode("utf-8", errors="replace")
                    stripped = line.lstrip()
                    if not stripped or stripped.startswith("*"):
                        continue
                    analysis_match = _ANALYSIS_RE.match(line)
                    if analysis_match:
                        analysis = analysis_match.group(1).lower()
                        analyses.append(analysis)
                        analysis_lines.append(line)
                    includes = includes or _INCLUDE_RE.match(line) is not None
                    unsupported_match = _UNSUPPORTED_RE.match(line)
                    if unsupported_match:
                        unsupported.append(unsupported_match.group(1).lower())
    except FileRecordError:
        source_unstable = True

    parameters = (
        _parse_analysis_parameters(analyses[0], analysis_lines[0])
        if len(analyses) == 1 and len(analysis_lines) == 1
        else None
    )

    return {
        "analyses": analyses,
        "include_detected": includes,
        "unsupported_directives": sorted(set(unsupported)),
        "line_too_long": line_too_long,
        "source_too_large": source_too_large,
        "source_unstable": source_unstable,
        "parameters": parameters,
    }


def inspect_transient_deck(path: str | Path) -> dict[str, object]:
    """Compatibility alias for the generalized bounded deck inspection."""

    return inspect_simulation_deck(path)


def _valid_extensions(value: object) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= 64
        and all(
            isinstance(name, str)
            and _EXTENSION_NAME_RE.fullmatch(name) is not None
            and isinstance(extension, dict)
            for name, extension in value.items()
        )
    )


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_requested_parameters(
    parameters: Mapping[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    if set(parameters) != {"analysis", "extensions"} or not _valid_extensions(
        parameters.get("extensions")
    ):
        return None, "Parameters must contain only closed analysis and extensions objects."
    if parameters.get("extensions"):
        return None, "The built-in circuit.simulate mappings do not implement request extensions."
    analysis = parameters.get("analysis")
    if not isinstance(analysis, Mapping):
        return None, "parameters.analysis must be an object."
    analysis_type = analysis.get("type")
    required: dict[str, set[str]] = {
        "op": {"type", "extensions"},
        "dc": {
            "type",
            "source_name",
            "source_unit",
            "start",
            "stop",
            "step",
            "extensions",
        },
        "ac": {
            "type",
            "sweep",
            "points",
            "start_hz",
            "stop_hz",
            "extensions",
        },
        "tran": {"type", "step_s", "stop_s", "extensions"},
    }
    if analysis_type not in required:
        return None, "analysis.type must be one of op, dc, ac, or tran."
    allowed = set(required[str(analysis_type)])
    if analysis_type == "tran":
        allowed.update({"start_s", "max_step_s"})
    if not required[str(analysis_type)].issubset(analysis) or set(analysis) - allowed:
        return None, f"The {analysis_type} analysis object has missing or unknown fields."
    if not _valid_extensions(analysis.get("extensions")) or analysis.get("extensions"):
        return None, "The built-in analysis mapping requires an empty extensions object."

    normalized = dict(analysis)
    if analysis_type == "dc":
        source_name = analysis.get("source_name")
        if (
            not isinstance(source_name, str)
            or len(source_name) > 256
            or _SOURCE_NAME_RE.fullmatch(source_name) is None
            or analysis.get("source_unit") not in {"V", "A"}
            or not all(_finite_number(analysis.get(name)) for name in ("start", "stop", "step"))
        ):
            return None, "The DC source identity or numeric sweep parameters are invalid."
        if (
            (source_name[0].casefold() == "v") != (analysis.get("source_unit") == "V")
            or source_name[0].casefold() not in {"v", "i"}
        ):
            return None, "DC source_unit must agree with the voltage/current source name."
        if not float(analysis["start"]) < float(analysis["stop"]) or float(
            analysis["step"]
        ) <= 0:
            return None, "The DC sweep must be ascending with a positive step."
        for name in ("start", "stop", "step"):
            normalized[name] = float(analysis[name])
    elif analysis_type == "ac":
        points = analysis.get("points")
        if (
            analysis.get("sweep") not in {"lin", "dec", "oct"}
            or isinstance(points, bool)
            or not isinstance(points, int)
            or not 1 <= points <= MAX_SHARED_ANALYSIS_POINTS
            or not all(_finite_number(analysis.get(name)) for name in ("start_hz", "stop_hz"))
            or float(analysis["start_hz"]) <= 0
            or float(analysis["start_hz"]) >= float(analysis["stop_hz"])
        ):
            return None, "The AC sweep kind, count, or frequency bounds are invalid."
        normalized["start_hz"] = float(analysis["start_hz"])
        normalized["stop_hz"] = float(analysis["stop_hz"])
    elif analysis_type == "tran":
        if not all(_finite_number(analysis.get(name)) for name in ("step_s", "stop_s")):
            return None, "The transient step and stop values must be finite numbers."
        start_s = analysis.get("start_s", 0.0)
        max_step_s = analysis.get("max_step_s")
        if (
            not _finite_number(start_s)
            or (max_step_s is not None and not _finite_number(max_step_s))
            or float(analysis["step_s"]) <= 0
            or float(analysis["stop_s"]) <= 0
            or float(start_s) < 0
            or float(start_s) >= float(analysis["stop_s"])
            or (
                max_step_s is not None
                and (
                    float(max_step_s) <= 0
                    or float(max_step_s) > float(analysis["stop_s"]) - float(start_s)
                )
            )
        ):
            return None, "The transient time controls violate the profile constraints."
        normalized["step_s"] = float(analysis["step_s"])
        normalized["stop_s"] = float(analysis["stop_s"])
        if "start_s" in analysis:
            normalized["start_s"] = float(start_s)
        if max_step_s is not None:
            normalized["max_step_s"] = float(max_step_s)

    if not _analysis_within_point_limit(normalized):
        return (
            None,
            "The declared analysis exceeds the 1000000-point shared evidence limit.",
        )
    return {"analysis": normalized, "extensions": {}}, None


def _same_number(left: object, right: object) -> bool:
    return _finite_number(left) and _finite_number(right) and math.isclose(
        float(left),
        float(right),
        rel_tol=1e-12,
        abs_tol=0.0,
    )


def _parameters_match(declared: dict[str, object], observed: dict[str, object]) -> bool:
    declared_analysis = declared.get("analysis")
    observed_analysis = observed.get("analysis")
    if not isinstance(declared_analysis, dict) or not isinstance(observed_analysis, dict):
        return False
    analysis_type = declared_analysis.get("type")
    if analysis_type != observed_analysis.get("type"):
        return False
    if analysis_type == "op":
        return True
    if analysis_type == "dc":
        return (
            str(declared_analysis.get("source_name", "")).casefold()
            == str(observed_analysis.get("source_name", "")).casefold()
            and declared_analysis.get("source_unit") == observed_analysis.get("source_unit")
            and all(
                _same_number(declared_analysis.get(name), observed_analysis.get(name))
                for name in ("start", "stop", "step")
            )
        )
    if analysis_type == "ac":
        return (
            declared_analysis.get("sweep") == observed_analysis.get("sweep")
            and declared_analysis.get("points") == observed_analysis.get("points")
            and all(
                _same_number(declared_analysis.get(name), observed_analysis.get(name))
                for name in ("start_hz", "stop_hz")
            )
        )
    if analysis_type == "tran":
        return all(
            _same_number(
                declared_analysis.get(name, 0.0 if name == "start_s" else None),
                observed_analysis.get(name, 0.0 if name == "start_s" else None),
            )
            for name in ("step_s", "stop_s", "start_s")
        ) and (
            ("max_step_s" not in declared_analysis and "max_step_s" not in observed_analysis)
            or _same_number(
                declared_analysis.get("max_step_s"),
                observed_analysis.get("max_step_s"),
            )
        )
    return False


def _selected_tool(discovery: DiscoveryManager, driver: BuiltinDriver | None) -> dict | None:
    if driver is None:
        return None
    info = discovery.inspect_tool(driver.native_tool)
    return tool_record(
        driver.native_tool,
        path=info["binary"],
        version=info["version"],
    )


def _profile_invalid(
    *,
    discovery: DiscoveryManager,
    driver: BuiltinDriver | None,
    source: Path,
    code: str,
    message: str,
) -> dict:
    try:
        inputs = [
            file_record(
                source,
                kind="spice-netlist",
                role="input",
                maximum_bytes=MAX_SOURCE_BYTES,
            )
        ]
    except FileRecordError:
        inputs = []
    return result(
        "simulate",
        tool=_selected_tool(discovery, driver),
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The circuit.simulate request is outside the initial shared profile.",
        inputs=inputs,
        diagnostics=[diagnostic("error", code, message)],
    )


def invalid_circuit_simulation_request(
    message: str,
    *,
    backend: str | None = None,
    analysis_type: str | None = None,
    code: str = "simulation.request.invalid",
) -> dict:
    """Return a profile-shaped CLI validation failure without launching EDA."""

    driver = builtin_driver(backend) if backend is not None else None
    normalized_analysis = (
        analysis_type if analysis_type in {"op", "dc", "ac", "tran"} else None
    )
    return result(
        "simulate",
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="OpenADA could not parse the shared circuit.simulate request.",
        diagnostics=[diagnostic("error", code, message)],
        data={
            "protocol": {
                "request_id": str(uuid.uuid4()),
                "operation_profile": CIRCUIT_SIMULATE_PROFILE,
                "assertion_profile": SIMULATION_EVIDENCE_ASSERTION,
                "driver_id": driver.driver_id if driver is not None else None,
                "driver_version": driver.version if driver is not None else None,
            },
            "analysis": {
                "type": normalized_analysis,
                "completion": "unproven",
                "convergence": "not-established",
                "point_count": None,
                "dependent_variable_count": None,
                "finite_value_count": None,
                "extensions": {},
            },
            "evidence": {
                "request_binding": "not-established",
                "freshness": "not-established",
                "structure": "not-established",
                "artifact_roles_present": [],
                "provenance": "incomplete",
                "provenance_limitations": [
                    "No native simulation was launched because CLI validation "
                    "failed, so request binding and evidence provenance were not "
                    "established."
                ],
                "extensions": {},
            },
            "extensions": {
                "org.openada": {
                    "backend": driver.alias if driver is not None else None,
                    "parameters": None,
                    "native_data": {},
                    "native_diagnostics": [],
                }
            },
        },
    )


def _analysis_counts(
    native_data: dict,
    analysis: dict[str, object],
) -> tuple[int, int, int] | None:
    evidence = native_data.get("analysis_evidence")
    if isinstance(evidence, dict):
        direct = tuple(
            evidence.get(name)
            for name in (
                "point_count",
                "dependent_variable_count",
                "finite_value_count",
            )
        )
        if all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in direct
        ):
            return direct  # type: ignore[return-value]

    captures = native_data.get("output_captures")
    if not isinstance(captures, list):
        return None
    for capture in captures:
        if not isinstance(capture, dict) or capture.get("status") != "valid":
            continue
        counts = analysis_raw_counts(capture, analysis)
        if counts is not None:
            return counts
    return None


def _canonicalize_artifacts(payload: dict) -> list[str]:
    roles: list[str] = []
    for artifact in payload.get("artifacts", []):
        kind = str(artifact.get("kind", "")).casefold()
        if "raw" in kind:
            role = "simulation.result"
        elif "log" in kind:
            role = "simulation.log"
        elif "control" in kind or "launcher" in kind:
            role = "simulation.launcher"
        else:
            continue
        artifact["role"] = role
        if role not in roles:
            roles.append(role)
    return roles


def _canonicalize_diagnostics(payload: dict) -> list[dict]:
    native = payload.get("diagnostics", [])
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in native:
        original_code = str(item.get("code", ""))
        if original_code in {
            "simulation.request.invalid",
            "simulation.analysis.unsupported",
            "simulation.tool.unavailable",
            "simulation.analysis.non_convergent",
            "simulation.analysis.unproven",
            "simulation.result.missing",
            "simulation.result.stale",
            "simulation.result.malformed",
            "simulation.evidence.over_limit",
            "simulation.provenance.incomplete",
        }:
            code = original_code
        elif original_code in {"simulation.nonconvergent"}:
            code = "simulation.analysis.non_convergent"
        elif original_code in {"tool.missing", "execution.not_available"}:
            code = "simulation.tool.unavailable"
        elif original_code in {
            "input.transitive_uninspected",
            "simulation.feature.unsupported",
            "request.invalid",
            "execution_mode.invalid",
        }:
            code = "simulation.request.invalid"
        elif original_code in {"artifact.missing", "simulation.log.missing"}:
            code = "simulation.result.missing"
        elif original_code in {
            "artifact.invalid",
            "artifact.empty",
            "simulation.log.malformed",
            "simulation.native_error",
        }:
            code = "simulation.result.malformed"
        elif original_code in {"input.changed"}:
            code = "simulation.result.stale"
        elif original_code in {
            "artifact.too_large",
            "execution.output_truncated",
            "input.too_large",
        }:
            code = "simulation.evidence.over_limit"
        elif item.get("severity") == "warning":
            code = "simulation.provenance.incomplete"
        else:
            code = "simulation.analysis.unproven"
        message = str(item.get("message", "The native driver reported incomplete evidence."))
        key = (code, message)
        if key in seen:
            continue
        seen.add(key)
        normalized_item = {
            "severity": item.get("severity", "error"),
            "code": code,
            "message": message,
        }
        if item.get("hint"):
            normalized_item["hint"] = item["hint"]
        normalized.append(normalized_item)
    if payload["engineering"]["status"] == "unknown" and not normalized:
        normalized.append(
            diagnostic(
                "error",
                "simulation.analysis.unproven",
                "The shared simulation assertion could not be established from native evidence.",
            )
        )
    payload["diagnostics"] = normalized
    return normalized


def _decorate(
    payload: dict,
    *,
    driver: BuiltinDriver | None,
    request_id: str,
    deck: dict[str, object],
    parameters: dict[str, object] | None,
) -> dict:
    native_data = deepcopy(payload.get("data", {}))
    native_diagnostics = deepcopy(payload.get("diagnostics", []))
    analysis = parameters.get("analysis") if isinstance(parameters, dict) else None
    status = payload["engineering"]["status"]
    counts = (
        _analysis_counts(native_data, analysis)
        if status == "pass" and isinstance(analysis, dict)
        else None
    )
    if status == "pass" and counts is None:
        payload["engineering"] = {
            "status": "unknown",
            "summary": (
                "The native result could not be bound structurally to the requested "
                "analysis and sweep bounds."
            ),
        }
        payload.setdefault("diagnostics", []).append(
            diagnostic(
                "error",
                "simulation.result.malformed",
                "The retained native result does not contain exactly one compatible "
                "requested-analysis plot with the required bounds and finite values.",
            )
        )
        status = "unknown"
    roles = _canonicalize_artifacts(payload)
    _canonicalize_diagnostics(payload)
    inputs_stable = native_data.get("inputs_stable") is True

    captures: list[dict] = []
    log_capture = native_data.get("log_capture")
    if isinstance(log_capture, dict):
        captures.append(log_capture)
    output_captures = native_data.get("output_captures")
    if isinstance(output_captures, list):
        captures.extend(item for item in output_captures if isinstance(item, dict))
    capture_statuses = {item.get("status") for item in captures}
    if capture_statuses & {"unstable", "parent_changed", "modified", "hardlinked"}:
        freshness = "stale"
    elif inputs_stable and roles:
        freshness = "fresh"
    else:
        freshness = "not-established"

    if counts is not None:
        structure = "valid"
    elif any(item not in {"missing", None} for item in capture_statuses):
        structure = "invalid"
    else:
        structure = "not-established"

    if status == "pass":
        completion = "completed"
        convergence = "converged"
    elif status == "fail":
        completion = "terminal-failure"
        convergence = "non-converged"
    else:
        completion = "unproven"
        convergence = "not-established"

    normalized = {
        "protocol": {
            "request_id": request_id,
            "operation_profile": (
                driver.operation_profile
                if driver is not None
                else "openada.operation/circuit.simulate/v1alpha2"
            ),
            "assertion_profile": (
                driver.assertion_profile
                if driver is not None
                else "openada.assertion/simulation.evidence.valid/v1alpha1"
            ),
            "driver_id": driver.driver_id if driver is not None else None,
            "driver_version": driver.version if driver is not None else None,
        },
        "analysis": {
            "type": analysis.get("type") if isinstance(analysis, dict) else None,
            "completion": completion,
            "convergence": convergence,
            "point_count": counts[0] if counts else None,
            "dependent_variable_count": counts[1] if counts else None,
            "finite_value_count": counts[2] if counts else None,
            "extensions": {},
        },
        "evidence": {
            "request_binding": (
                "exact"
                if (
                    parameters is not None
                    and inputs_stable
                    and (counts is not None or status == "fail")
                )
                else "not-established"
            ),
            "freshness": freshness,
            "structure": structure,
            "artifact_roles_present": roles,
            "provenance": "bounded" if inputs_stable else "incomplete",
            "provenance_limitations": [
                "Only the declared top-level model-free analysis deck and "
                "selected native executable were content-bound; host runtime "
                "libraries and simulator defaults remain bounded provenance."
            ],
            "extensions": {},
        },
        "extensions": {
            "org.openada": {
                "backend": driver.alias if driver is not None else None,
                "parameters": parameters,
                "native_data": native_data,
                "native_diagnostics": native_diagnostics,
            }
        },
    }
    payload["data"] = normalized
    return payload


def simulate_circuit_profile(
    spice_file: str | Path,
    output_dir: str | Path,
    *,
    backend: str,
    discovery: DiscoveryManager,
    workdir: str | Path | None = None,
    timeout: float = 120.0,
    request_id: str | None = None,
    parameters: Mapping[str, object] | None = None,
) -> dict:
    """Execute one closed circuit.simulate analysis through a selected driver."""

    source = Path(spice_file).expanduser().resolve()
    request_id_error: str | None = None
    if request_id is None:
        correlation_id = str(uuid.uuid4())
    else:
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (AttributeError, ValueError):
            request_id_error = "request_id must be a canonical lowercase UUID."
            correlation_id = str(uuid.uuid4())
        else:
            if str(parsed_request_id) != request_id:
                request_id_error = "request_id must be a canonical lowercase UUID."
                correlation_id = str(uuid.uuid4())
            else:
                correlation_id = request_id
    driver = builtin_driver(backend)
    deck = inspect_simulation_deck(source)
    requested: dict[str, object] | None = None
    parameter_error: str | None = None
    if parameters is None:
        observed = deck.get("parameters")
        requested = deepcopy(observed) if isinstance(observed, dict) else None
    elif not isinstance(parameters, Mapping):
        parameter_error = "parameters must be a closed JSON object."
    else:
        requested, parameter_error = _validate_requested_parameters(parameters)
    analysis = requested.get("analysis") if isinstance(requested, dict) else None
    analysis_type = analysis.get("type") if isinstance(analysis, dict) else None

    invalid: tuple[str, str] | None = None
    if driver is None:
        invalid = ("driver.unsupported", f"Unknown built-in driver selector: {backend!r}.")
    elif request_id_error is not None:
        invalid = ("simulation.request.invalid", request_id_error)
    elif not source.is_file():
        invalid = ("input.missing", f"File not found: {source}")
    elif deck["source_unstable"]:
        invalid = (
            "simulation.request.invalid",
            "The top-level SPICE input could not be inspected as one stable regular file.",
        )
    elif deck["source_too_large"]:
        invalid = (
            "simulation.evidence.over_limit",
            f"The top-level SPICE input must not exceed {MAX_SOURCE_BYTES} bytes.",
        )
    elif parameter_error is not None:
        invalid = ("simulation.request.invalid", parameter_error)
    elif deck["line_too_long"]:
        invalid = (
            "input.line_too_long",
            f"At least one source line exceeds {MAX_SOURCE_LINE_BYTES} bytes.",
        )
    elif deck["include_detected"]:
        invalid = (
            "input.transitive_uninspected",
            "The initial shared profile accepts no .include, .inc, or .lib directives.",
        )
    elif deck["unsupported_directives"]:
        invalid = (
            "simulation.feature.unsupported",
            "The initial shared profile accepts no control, measurement, print, "
            "step, Fourier, or FFT directives.",
        )
    elif deck["parameters"] is None:
        invalid = (
            "simulation.analysis.unsupported",
            "The shared profile requires exactly one parseable top-level .op, .dc, .ac, or .tran analysis.",
        )
    elif requested is None or not _parameters_match(requested, deck["parameters"]):
        invalid = (
            "simulation.request.invalid",
            "The typed analysis parameters do not match the authoritative deck directive.",
        )
    elif not isinstance(analysis_type, str) or analysis_feature(analysis_type) not in driver.features:
        invalid = (
            "simulation.analysis.unsupported",
            f"Driver {driver.driver_id} does not advertise the {analysis_type!r} analysis feature.",
        )

    if invalid is not None:
        payload = _profile_invalid(
            discovery=discovery,
            driver=driver,
            source=source,
            code=invalid[0],
            message=invalid[1],
        )
        return _decorate(
            payload,
            driver=driver,
            request_id=correlation_id,
            deck=deck,
            parameters=requested,
        )

    assert driver is not None
    implementation = driver.factory(discovery)
    if driver.alias == "ngspice":
        payload = implementation.simulate(  # type: ignore[attr-defined]
            source,
            output_dir,
            workdir=workdir,
            execution_mode="batch",
            timeout=timeout,
        )
    else:
        payload = implementation.simulate(  # type: ignore[attr-defined]
            source,
            output_dir,
            workdir=workdir,
            timeout=timeout,
            analysis=analysis,
        )
    return _decorate(
        payload,
        driver=driver,
        request_id=correlation_id,
        deck=deck,
        parameters=requested,
    )


__all__ = [
    "MAX_SHARED_ANALYSIS_POINTS",
    "MAX_SOURCE_BYTES",
    "invalid_circuit_simulation_request",
    "inspect_simulation_deck",
    "inspect_transient_deck",
    "simulate_circuit_profile",
]
