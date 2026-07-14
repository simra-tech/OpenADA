"""Shared circuit.simulate semantics for ngspice and Xyce."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import uuid

from ..contract import (
    diagnostic,
    file_record,
    result,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..driver_registry import BuiltinDriver, builtin_driver


MAX_SOURCE_LINE_BYTES = 65_536
_ANALYSIS_RE = re.compile(r"^\s*\.(op|dc|ac|tran|noise|hb)\b", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.(?:inc(?:lude)?|lib)\b", re.IGNORECASE)
_UNSUPPORTED_RE = re.compile(
    r"^\s*\.(control|measure|meas|print|four|fft|step)\b", re.IGNORECASE
)
_TRAN_RE = re.compile(r"^\s*\.tran\s+(.+?)\s*$", re.IGNORECASE)
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
    value = float(match.group("number")) * factor
    return value if math.isfinite(value) else None


def inspect_transient_deck(path: str | Path) -> dict[str, object]:
    """Inspect the bounded common subset without interpreting includes."""

    source = Path(path)
    analyses: list[str] = []
    unsupported: list[str] = []
    includes = False
    line_too_long = False
    tran_arguments: list[str] | None = None
    try:
        with source.open("rb") as handle:
            line_number = 0
            while True:
                raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                if not raw_line:
                    break
                line_number += 1
                if len(raw_line) > MAX_SOURCE_LINE_BYTES and not raw_line.endswith(b"\n"):
                    line_too_long = True
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
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
                    if analysis == "tran":
                        tran_match = _TRAN_RE.match(line)
                        if tran_match:
                            # Inline comments are deliberately unsupported in the
                            # first profile; they cannot be split consistently
                            # across SPICE dialects without a full deck parser.
                            tran_arguments = tran_match.group(1).split()
                includes = includes or _INCLUDE_RE.match(line) is not None
                unsupported_match = _UNSUPPORTED_RE.match(line)
                if unsupported_match:
                    unsupported.append(unsupported_match.group(1).lower())
    except OSError:
        pass

    parameters = None
    if analyses == ["tran"] and tran_arguments is not None and 2 <= len(tran_arguments) <= 4:
        parsed = [_spice_number(token) for token in tran_arguments]
        if all(value is not None for value in parsed):
            values = [float(value) for value in parsed if value is not None]
            step_s, stop_s = values[:2]
            start_s = values[2] if len(values) >= 3 else 0.0
            max_step_s = values[3] if len(values) == 4 else None
            if (
                step_s > 0
                and stop_s > 0
                and 0 <= start_s < stop_s
                and (max_step_s is None or 0 < max_step_s <= stop_s - start_s)
            ):
                analysis_parameters: dict[str, object] = {
                    "type": "tran",
                    "step_s": step_s,
                    "stop_s": stop_s,
                    "extensions": {},
                }
                if len(values) >= 3:
                    analysis_parameters["start_s"] = start_s
                if max_step_s is not None:
                    analysis_parameters["max_step_s"] = max_step_s
                parameters = {"analysis": analysis_parameters, "extensions": {}}

    return {
        "analyses": analyses,
        "include_detected": includes,
        "unsupported_directives": sorted(set(unsupported)),
        "line_too_long": line_too_long,
        "parameters": parameters,
    }


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
    return result(
        "simulate",
        tool=_selected_tool(discovery, driver),
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The circuit.simulate request is outside the initial shared profile.",
        inputs=[file_record(source, kind="spice-netlist", role="input")],
        diagnostics=[diagnostic("error", code, message)],
    )


def _analysis_counts(native_data: dict) -> tuple[int, int, int] | None:
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
        validation = capture.get("validation")
        metadata = validation.get("metadata") if isinstance(validation, dict) else None
        plots = metadata.get("plots") if isinstance(metadata, dict) else None
        if not isinstance(plots, list):
            continue
        analysis_plots = [
            plot
            for plot in plots
            if isinstance(plot, dict)
            and str(plot.get("plotname", "")).strip().casefold() == "transient analysis"
        ]
        if len(analysis_plots) != 1:
            continue
        plot = analysis_plots[0]
        points = plot.get("points")
        variables = plot.get("variables")
        if (
            isinstance(points, int)
            and not isinstance(points, bool)
            and points > 0
            and isinstance(variables, int)
            and not isinstance(variables, bool)
            and variables >= 2
            and plot.get("numeric_type") == "real"
            and plot.get("unpadded") is False
        ):
            dependent = variables - 1
            return points, dependent, points * dependent
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
        elif original_code in {"artifact.too_large", "execution.output_truncated"}:
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
) -> dict:
    native_data = deepcopy(payload.get("data", {}))
    native_diagnostics = deepcopy(payload.get("diagnostics", []))
    status = payload["engineering"]["status"]
    counts = _analysis_counts(native_data) if status == "pass" else None
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

    raw_valid = bool(
        isinstance(native_data.get("analysis_evidence"), dict)
        and native_data["analysis_evidence"].get("raw") is True
    )
    if raw_valid and counts is not None:
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
                else "openada.operation/circuit.simulate/v1alpha1"
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
            "type": "tran" if deck.get("parameters") is not None else None,
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
                if deck.get("parameters") is not None and inputs_stable
                else "not-established"
            ),
            "freshness": freshness,
            "structure": structure,
            "artifact_roles_present": roles,
            "provenance": "bounded" if inputs_stable else "incomplete",
            "provenance_limitations": [
                "Only the declared top-level model-free transient deck and "
                "selected native executable were content-bound; host runtime "
                "libraries and simulator defaults remain bounded provenance."
            ],
            "extensions": {},
        },
        "extensions": {
            "org.openada": {
                "backend": driver.alias if driver is not None else None,
                "parameters": deck.get("parameters"),
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
) -> dict:
    """Execute the first common transient profile through a selected driver."""

    source = Path(spice_file).expanduser().resolve()
    correlation_id = request_id or str(uuid.uuid4())
    driver = builtin_driver(backend)
    deck = inspect_transient_deck(source)

    invalid: tuple[str, str] | None = None
    if driver is None:
        invalid = ("driver.unsupported", f"Unknown built-in driver selector: {backend!r}.")
    elif not source.is_file():
        invalid = ("input.missing", f"File not found: {source}")
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
    elif deck["analyses"] != ["tran"] or deck["parameters"] is None:
        invalid = (
            "simulation.analysis.unsupported",
            "The initial shared profile requires exactly one parseable top-level .tran analysis.",
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
        )
    return _decorate(
        payload,
        driver=driver,
        request_id=correlation_id,
        deck=deck,
    )


__all__ = ["inspect_transient_deck", "simulate_circuit_profile"]
