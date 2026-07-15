"""Typed extraction of normalized real series from native SPICE raw evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

from ..contract import diagnostic, result, static_execution
from ..driver_registry import CIRCUIT_SIMULATE_PROFILE, SIMULATION_EVIDENCE_ASSERTION
from ..engines.ngspice_outputs import RawSeriesExtraction, extract_analysis_raw
from .result_measure import normalized_series_sha256


OPERATION_PROFILE = "openada.operation/result.series.extract/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/series.extraction.valid/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.spice3-series"
IMPLEMENTATION_VERSION = "1.0.0"
MAX_POINTS = 100_000
MAX_SELECTORS = 32
MAX_CONDITIONS = 64
MAX_SELECTED_SCALARS = 1_000_000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NATIVE_TYPE_UNITS = {
    "voltage": "V",
    "current": "A",
}
_DRIVER_BACKENDS = {
    "org.openada.driver.ngspice": ("ngspice", "ngspice", "ngspice-raw"),
    "org.openada.driver.xyce": ("xyce", "xyce", "xyce-raw"),
}


class _InvalidRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _closed_object(
    value: object,
    label: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    code: str = "series.request.invalid",
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidRequest(code, f"{label} must be an object.")
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise _InvalidRequest(code, f"{label} field names must all be strings.")
    key_set = set(keys)
    missing = required - key_set
    unexpected = key_set - required - optional
    if missing:
        raise _InvalidRequest(
            code,
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if unexpected:
        raise _InvalidRequest(
            code,
            f"{label} contains undeclared fields: {', '.join(sorted(unexpected))}.",
        )
    return value


def _text(value: object, label: str, *, limit: int = 256, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidRequest(
            code,
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _canonical_uuid(value: object, label: str, *, code: str) -> str:
    text = _text(value, label, limit=36, code=code)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as exc:
        raise _InvalidRequest(code, f"{label} must be a UUID.") from exc
    if str(parsed) != text:
        raise _InvalidRequest(
            code,
            f"{label} must use canonical lowercase UUID form.",
        )
    return text


def _correlation_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    return _canonical_uuid(
        value,
        "request_id",
        code="series.request.invalid",
    )


def _finite_number(value: object, label: str, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidRequest(code, f"{label} must be a JSON number.")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise _InvalidRequest(code, f"{label} must be finite.") from exc
    if not math.isfinite(parsed):
        raise _InvalidRequest(code, f"{label} must be finite.")
    return parsed


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_file_record(value: object, label: str) -> dict[str, object]:
    item = _closed_object(
        value,
        label,
        required={"kind", "role", "path", "exists"},
        optional={"bytes", "sha256"},
        code="series.simulation.invalid",
    )
    kind = _text(
        item["kind"], f"{label}.kind", code="series.simulation.invalid"
    )
    role = _text(
        item["role"], f"{label}.role", code="series.simulation.invalid"
    )
    path = _text(
        item["path"],
        f"{label}.path",
        limit=4_095,
        code="series.simulation.invalid",
    )
    exists = item["exists"]
    if not isinstance(exists, bool):
        raise _InvalidRequest(
            "series.simulation.invalid", f"{label}.exists must be boolean."
        )
    record: dict[str, object] = {
        "kind": kind,
        "role": role,
        "path": path,
        "exists": exists,
    }
    if exists:
        size = item.get("bytes")
        digest = item.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise _InvalidRequest(
                "series.simulation.invalid",
                f"{label} lacks a valid byte count or SHA-256 digest.",
            )
        record.update({"bytes": size, "sha256": digest})
    elif "bytes" in item or "sha256" in item:
        raise _InvalidRequest(
            "series.simulation.invalid",
            f"{label} may not bind bytes or a digest when exists is false.",
        )
    return record


def _validate_base_envelope(value: object) -> Mapping[str, Any]:
    root = _closed_object(
        value,
        "simulation_result",
        required={
            "schema",
            "operation",
            "tool",
            "execution",
            "engineering",
            "inputs",
            "artifacts",
            "diagnostics",
            "data",
            "provenance",
        },
        code="series.simulation.invalid",
    )
    if root["schema"] != "openada.result/v0alpha1" or root["operation"] != "simulate":
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result must be a complete simulate openada.result/v0alpha1 envelope.",
        )

    tool = _closed_object(
        root["tool"],
        "simulation_result.tool",
        required={"name", "path", "version"},
        code="series.simulation.invalid",
    )
    _text(tool["name"], "simulation_result.tool.name", code="series.simulation.invalid")
    if tool["path"] is not None:
        _text(
            tool["path"],
            "simulation_result.tool.path",
            limit=4_095,
            code="series.simulation.invalid",
        )
    if tool["version"] is not None:
        _text(
            tool["version"],
            "simulation_result.tool.version",
            limit=1_000,
            code="series.simulation.invalid",
        )

    execution = _closed_object(
        root["execution"],
        "simulation_result.execution",
        required={"status", "exit_code", "duration_ms", "command"},
        optional={"cwd", "error"},
        code="series.simulation.invalid",
    )
    if (
        execution["status"]
        not in {"completed", "timed_out", "not_available", "invalid_request", "failed"}
        or (
            execution["exit_code"] is not None
            and (
                isinstance(execution["exit_code"], bool)
                or not isinstance(execution["exit_code"], int)
            )
        )
        or isinstance(execution["duration_ms"], bool)
        or not isinstance(execution["duration_ms"], int)
        or execution["duration_ms"] < 0
        or not _is_sequence(execution["command"])
        or any(not isinstance(item, str) for item in execution["command"])
    ):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result.execution is malformed.",
        )
    for optional_text in ("cwd", "error"):
        if optional_text in execution:
            _text(
                execution[optional_text],
                f"simulation_result.execution.{optional_text}",
                limit=4_095 if optional_text == "cwd" else 4_000,
                code="series.simulation.invalid",
            )

    engineering = _closed_object(
        root["engineering"],
        "simulation_result.engineering",
        required={"status", "summary"},
        code="series.simulation.invalid",
    )
    if engineering["status"] not in {"pass", "fail", "unknown"}:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result.engineering.status is invalid.",
        )
    _text(
        engineering["summary"],
        "simulation_result.engineering.summary",
        limit=4_000,
        code="series.simulation.invalid",
    )

    for collection_name in ("inputs", "artifacts"):
        collection = root[collection_name]
        if not _is_sequence(collection) or len(collection) > 64:
            raise _InvalidRequest(
                "series.simulation.invalid",
                f"simulation_result.{collection_name} must be a bounded array.",
            )
        for index, item in enumerate(collection):
            _validate_file_record(item, f"simulation_result.{collection_name}[{index}]")

    diagnostics = root["diagnostics"]
    if not _is_sequence(diagnostics) or len(diagnostics) > 256:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result.diagnostics must be a bounded array.",
        )
    for index, raw_diagnostic in enumerate(diagnostics):
        item = _closed_object(
            raw_diagnostic,
            f"simulation_result.diagnostics[{index}]",
            required={"severity", "code", "message"},
            optional={"hint"},
            code="series.simulation.invalid",
        )
        if item["severity"] not in {"info", "warning", "error"}:
            raise _InvalidRequest(
                "series.simulation.invalid",
                f"simulation_result.diagnostics[{index}].severity is invalid.",
            )
        for field in ("code", "message"):
            _text(
                item[field],
                f"simulation_result.diagnostics[{index}].{field}",
                limit=4_000,
                code="series.simulation.invalid",
            )
        if "hint" in item:
            _text(
                item["hint"],
                f"simulation_result.diagnostics[{index}].hint",
                limit=4_000,
                code="series.simulation.invalid",
            )

    provenance = _closed_object(
        root["provenance"],
        "simulation_result.provenance",
        required={"openada_version", "created_at", "host"},
        code="series.simulation.invalid",
    )
    _text(
        provenance["openada_version"],
        "simulation_result.provenance.openada_version",
        limit=100,
        code="series.simulation.invalid",
    )
    _text(
        provenance["created_at"],
        "simulation_result.provenance.created_at",
        limit=100,
        code="series.simulation.invalid",
    )
    host = _closed_object(
        provenance["host"],
        "simulation_result.provenance.host",
        required={"system", "machine", "python"},
        code="series.simulation.invalid",
    )
    for field in ("system", "machine", "python"):
        if not isinstance(host[field], str):
            raise _InvalidRequest(
                "series.simulation.invalid",
                f"simulation_result.provenance.host.{field} must be text.",
            )
    return root


def _simulation_source(
    simulation_result: object,
    artifact_path: str | Path,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    str,
    dict[str, int],
]:
    root = _validate_base_envelope(simulation_result)
    data = _closed_object(
        root["data"],
        "simulation_result.data",
        required={"protocol", "analysis", "evidence", "extensions"},
        code="series.simulation.invalid",
    )
    protocol = _closed_object(
        data["protocol"],
        "simulation_result.data.protocol",
        required={
            "request_id",
            "operation_profile",
            "assertion_profile",
            "driver_id",
            "driver_version",
        },
        code="series.simulation.invalid",
    )
    simulation_request_id = _canonical_uuid(
        protocol["request_id"],
        "simulation_result.data.protocol.request_id",
        code="series.simulation.invalid",
    )
    if (
        protocol["operation_profile"] != CIRCUIT_SIMULATE_PROFILE
        or protocol["assertion_profile"] != SIMULATION_EVIDENCE_ASSERTION
        or protocol["driver_id"] not in _DRIVER_BACKENDS
    ):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result does not identify a supported circuit.simulate/v1alpha2 driver binding.",
        )
    driver_id = str(protocol["driver_id"])
    backend, tool_name, artifact_kind = _DRIVER_BACKENDS[driver_id]
    driver_version = _text(
        protocol["driver_version"],
        "simulation_result.data.protocol.driver_version",
        limit=100,
        code="series.simulation.invalid",
    )
    tool = root["tool"]
    assert isinstance(tool, Mapping)
    if tool.get("name") != tool_name:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result tool identity conflicts with its driver identity.",
        )

    analysis_record = _closed_object(
        data["analysis"],
        "simulation_result.data.analysis",
        required={
            "type",
            "completion",
            "convergence",
            "point_count",
            "dependent_variable_count",
            "finite_value_count",
            "extensions",
        },
        code="series.simulation.invalid",
    )
    analysis_type = analysis_record["type"]
    if analysis_type not in {"op", "dc", "ac", "tran"}:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result has no extractable analysis type.",
        )
    for count_name in ("point_count", "dependent_variable_count", "finite_value_count"):
        count = analysis_record[count_name]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise _InvalidRequest(
                "series.simulation.invalid",
                f"simulation_result.data.analysis.{count_name} must be positive.",
            )
    if not isinstance(analysis_record["extensions"], Mapping):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result.data.analysis.extensions must be an object.",
        )

    evidence = _closed_object(
        data["evidence"],
        "simulation_result.data.evidence",
        required={
            "request_binding",
            "freshness",
            "structure",
            "artifact_roles_present",
            "provenance",
            "provenance_limitations",
            "extensions",
        },
        code="series.simulation.invalid",
    )
    roles = evidence["artifact_roles_present"]
    if (
        not _is_sequence(roles)
        or any(not isinstance(role, str) for role in roles)
        or len(roles) != len(set(roles))
    ):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result artifact role evidence is malformed.",
        )
    retained_artifacts = [
        _validate_file_record(item, f"simulation_result.artifacts[{index}]")
        for index, item in enumerate(root["artifacts"])
    ]
    actual_roles = {
        str(item["role"]) for item in retained_artifacts if item["exists"] is True
    }
    if (
        root["execution"]["status"] != "completed"
        or root["execution"]["exit_code"] != 0
        or root["engineering"]["status"] != "pass"
        or analysis_record["completion"] != "completed"
        or analysis_record["convergence"] != "converged"
        or evidence["request_binding"] != "exact"
        or evidence["freshness"] != "fresh"
        or evidence["structure"] != "valid"
        or set(roles) != actual_roles
        or not {"simulation.result", "simulation.log"}.issubset(actual_roles)
    ):
        raise _InvalidRequest(
            "series.simulation.unproven",
            "simulation_result does not prove exact, fresh, structurally valid converged analysis evidence.",
        )

    extensions = data["extensions"]
    if not isinstance(extensions, Mapping) or "org.openada" not in extensions:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result lacks its built-in request parameter binding.",
        )
    native_extension = _closed_object(
        extensions["org.openada"],
        "simulation_result.data.extensions.org.openada",
        required={"backend", "parameters", "native_data", "native_diagnostics"},
        code="series.simulation.invalid",
    )
    if native_extension["backend"] != backend:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result backend conflicts with its driver identity.",
        )
    parameters = _closed_object(
        native_extension["parameters"],
        "simulation_result.data.extensions.org.openada.parameters",
        required={"analysis", "extensions"},
        code="series.simulation.invalid",
    )
    if parameters["extensions"] != {}:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "The built-in circuit simulation request extensions must be empty.",
        )
    requested_analysis = parameters["analysis"]
    if not isinstance(requested_analysis, Mapping) or requested_analysis.get("type") != analysis_type:
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result normalized and request-bound analysis identities conflict.",
        )
    if not isinstance(native_extension["native_data"], Mapping) or not _is_sequence(
        native_extension["native_diagnostics"]
    ):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result native extension records are malformed.",
        )

    raw_artifacts = [
        item for item in retained_artifacts if item["role"] == "simulation.result"
    ]
    if len(raw_artifacts) != 1:
        raise _InvalidRequest(
            "series.artifact.ambiguous",
            "simulation_result must retain exactly one simulation.result artifact.",
        )
    artifact = raw_artifacts[0]
    if (
        artifact["kind"] != artifact_kind
        or artifact["exists"] is not True
        or artifact.get("bytes", 0) <= 0
    ):
        raise _InvalidRequest(
            "series.simulation.invalid",
            "simulation_result retains no supported complete native raw artifact.",
        )
    recorded_path = Path(str(artifact["path"]))
    if not recorded_path.is_absolute() or str(recorded_path) != os.path.abspath(recorded_path):
        raise _InvalidRequest(
            "series.artifact.path_mismatch",
            "The retained simulation.result artifact path is not canonical and absolute.",
        )
    try:
        supplied_path = Path(os.path.abspath(Path(artifact_path).expanduser()))
    except (OSError, TypeError, ValueError) as exc:
        raise _InvalidRequest(
            "series.request.invalid", "artifact_path is invalid."
        ) from exc
    if supplied_path != recorded_path:
        raise _InvalidRequest(
            "series.artifact.path_mismatch",
            "artifact_path must name the exact path retained by simulation_result.",
        )

    source = {
        "operation_profile": CIRCUIT_SIMULATE_PROFILE,
        "request_id": simulation_request_id,
        "driver_id": driver_id,
        "driver_version": driver_version,
        "backend": backend,
        "analysis_type": analysis_type,
        "artifact": artifact,
        "binding": "not-established",
    }
    counts = {
        "point_count": int(analysis_record["point_count"]),
        "dependent_variable_count": int(
            analysis_record["dependent_variable_count"]
        ),
        "finite_value_count": int(analysis_record["finite_value_count"]),
    }
    return source, dict(requested_analysis), artifact, str(supplied_path), counts


def _selectors(value: object) -> list[dict[str, str]]:
    if not _is_sequence(value) or not 1 <= len(value) <= MAX_SELECTORS:
        raise _InvalidRequest(
            "series.selector.invalid",
            f"selectors must contain between 1 and {MAX_SELECTORS} entries.",
        )
    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    output_names: set[str] = set()
    for index, raw_selector in enumerate(value):
        selector = _closed_object(
            raw_selector,
            f"selectors[{index}]",
            required={"native_name", "output_name", "unit", "component"},
            code="series.selector.invalid",
        )
        native_name = _text(
            selector["native_name"],
            f"selectors[{index}].native_name",
            code="series.selector.invalid",
        )
        output_name = _text(
            selector["output_name"],
            f"selectors[{index}].output_name",
            code="series.selector.invalid",
        )
        unit = _text(
            selector["unit"],
            f"selectors[{index}].unit",
            limit=64,
            code="series.selector.invalid",
        )
        component = selector["component"]
        if component not in {"real", "imaginary"}:
            raise _InvalidRequest(
                "series.selector.invalid",
                f"selectors[{index}].component must be real or imaginary.",
            )
        identity = (native_name, str(component))
        if identity in identities:
            raise _InvalidRequest(
                "series.selector.invalid",
                "selectors may not repeat one native vector component.",
            )
        if output_name in output_names:
            raise _InvalidRequest(
                "series.selector.invalid",
                "selectors output_name values must be unique.",
            )
        identities.add(identity)
        output_names.add(output_name)
        normalized.append(
            {
                "native_name": native_name,
                "output_name": output_name,
                "unit": unit,
                "component": str(component),
            }
        )
    return normalized


def _condition(value: object, index: int) -> dict[str, object]:
    item = _closed_object(
        value,
        f"conditions[{index}]",
        required={"name", "value", "unit"},
        code="series.request.invalid",
    )
    name = _text(
        item["name"],
        f"conditions[{index}].name",
        code="series.request.invalid",
    )
    unit = _text(
        item["unit"],
        f"conditions[{index}].unit",
        limit=64,
        code="series.request.invalid",
    )
    condition_value = item["value"]
    if isinstance(condition_value, bool):
        normalized_value: object = condition_value
    elif isinstance(condition_value, (int, float)):
        normalized_value = _finite_number(
            condition_value,
            f"conditions[{index}].value",
            code="series.request.invalid",
        )
    elif isinstance(condition_value, str):
        normalized_value = _text(
            condition_value,
            f"conditions[{index}].value",
            code="series.request.invalid",
        )
    else:
        raise _InvalidRequest(
            "series.request.invalid",
            f"conditions[{index}].value must be a finite number, string, or boolean.",
        )
    return {"name": name, "value": normalized_value, "unit": unit}


def _conditions(value: object) -> list[dict[str, object]]:
    if not _is_sequence(value) or len(value) > MAX_CONDITIONS:
        raise _InvalidRequest(
            "series.request.invalid",
            f"conditions must be an array of at most {MAX_CONDITIONS} entries.",
        )
    normalized = [_condition(item, index) for index, item in enumerate(value)]
    names = [str(item["name"]) for item in normalized]
    if len(names) != len(set(names)):
        raise _InvalidRequest(
            "series.request.invalid", "conditions names must be unique."
        )
    return normalized


def _axis_definition(
    analysis: Mapping[str, object],
    extracted: RawSeriesExtraction,
) -> dict[str, object]:
    analysis_type = analysis.get("type")
    if analysis_type == "op":
        return {"name": "sample", "unit": "1", "values": [0.0]}
    expected = {
        "dc": (str(analysis.get("source_name", "")), analysis.get("source_unit"), {
            "V": "voltage",
            "A": "current",
        }.get(analysis.get("source_unit"))),
        "ac": ("frequency", "Hz", "frequency"),
        "tran": ("time", "s", "time"),
    }.get(analysis_type)
    if expected is None:
        raise _InvalidRequest(
            "series.simulation.invalid", "The simulation analysis type is unsupported."
        )
    axis_name, axis_unit, native_type = expected
    if (
        not axis_name
        or not isinstance(axis_unit, str)
        or native_type is None
        or not isinstance(extracted.axis_native_type, str)
        or extracted.axis_native_type.casefold() != native_type
        or len(extracted.axis_values) == 0
    ):
        raise _InvalidRequest(
            "series.unit.mismatch",
            "The native independent vector type does not prove the typed analysis axis unit.",
        )
    return {
        "name": axis_name,
        "unit": axis_unit,
        "values": list(extracted.axis_values),
    }


def _normalized_signals(
    extracted: RawSeriesExtraction,
    selectors: Sequence[dict[str, str]],
) -> list[dict[str, object]]:
    native = {signal.name: signal for signal in extracted.signals}
    output: list[dict[str, object]] = []
    numeric_type = extracted.metadata.get("numeric_type")
    for selector in selectors:
        signal = native.get(selector["native_name"])
        if signal is None:
            raise _InvalidRequest(
                "series.selector.missing",
                f"Native vector {selector['native_name']!r} was not extracted.",
            )
        proven_unit = _NATIVE_TYPE_UNITS.get(signal.native_type.casefold())
        if proven_unit is None or selector["unit"] != proven_unit:
            raise _InvalidRequest(
                "series.unit.mismatch",
                f"Native vector {selector['native_name']!r} has type "
                f"{signal.native_type!r}, which does not prove requested unit "
                f"{selector['unit']!r}.",
            )
        component = selector["component"]
        if component == "imaginary":
            if numeric_type != "complex" or signal.imaginary_values is None:
                raise _InvalidRequest(
                    "series.selector.component_invalid",
                    f"Native vector {selector['native_name']!r} is real and has no imaginary component.",
                )
            values = signal.imaginary_values
        else:
            values = signal.real_values
        output.append(
            {
                "name": selector["output_name"],
                "unit": selector["unit"],
                "values": list(values),
            }
        )
    return output


def _source_record(
    source: Mapping[str, object],
    *,
    binding: str,
) -> dict[str, object]:
    return {
        "operation_profile": source["operation_profile"],
        "request_id": source["request_id"],
        "driver_id": source["driver_id"],
        "driver_version": source["driver_version"],
        "backend": source["backend"],
        "analysis_type": source["analysis_type"],
        "artifact": dict(source["artifact"]),
        "binding": binding,
    }


def _extraction_template(
    *,
    source: Mapping[str, object] | None,
    request_sha256: str | None,
) -> dict[str, object]:
    return {
        "status": "unknown",
        "request_sha256": request_sha256,
        "source": (
            _source_record(source, binding="not-established")
            if source is not None
            else None
        ),
        "plot": None,
        "series": None,
        "extensions": {},
    }


def _payload(
    request_id: str,
    extraction: dict[str, object],
    *,
    status: str,
    summary: str,
    execution_status: str,
    inputs: Sequence[dict[str, object]] = (),
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "result.series.extract",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=status,
        summary=summary,
        inputs=inputs,
        diagnostics=diagnostics,
        data={
            "protocol": {
                "request_id": request_id,
                "operation_profile": OPERATION_PROFILE,
                "assertion_profile": ASSERTION_PROFILE,
                "implementation_id": IMPLEMENTATION_ID,
                "implementation_version": IMPLEMENTATION_VERSION,
            },
            "extraction": extraction,
            "extensions": {},
        },
    )


def _raw_diagnostic(extracted: RawSeriesExtraction) -> tuple[str, str]:
    reason = extracted.reason
    if reason == "raw.extraction_over_limit":
        return (
            "series.source.over_limit",
            "The selected native series exceeds the bounded extraction ceiling.",
        )
    if reason == "raw.selected_variable_missing":
        return (
            "series.selector.missing",
            "At least one exact native vector selector is absent or ambiguous.",
        )
    if reason == "raw.encoding_unsupported":
        return (
            "series.format.unsupported",
            "The retained backend artifact uses an unsupported raw encoding.",
        )
    if reason in {
        "file.size_mismatch",
        "file.digest_mismatch",
        "file.changed_during_extraction",
    }:
        return (
            "series.artifact.binding_mismatch",
            "The native artifact does not match the exact retained path, size, and digest binding.",
        )
    return (
        "series.artifact.malformed",
        f"The retained native artifact could not be extracted safely ({reason}).",
    )


def extract_result_series(
    simulation_result: Mapping[str, object],
    artifact_path: str | Path,
    selectors: Sequence[Mapping[str, object]],
    *,
    conditions: Sequence[Mapping[str, object]] = (),
    request_id: str | None = None,
) -> dict[str, Any]:
    """Return a measurement-compatible normalized real series.

    The function accepts only a complete passing ``circuit.simulate/v1alpha2``
    result and the exact native artifact retained by that result.  Every output
    vector has an explicit exact native selector, Cartesian component, output
    name, and unit.  It performs no unit conversion, magnitude/phase transform,
    alias inference, or native EDA execution.
    """

    try:
        correlation_id = _correlation_id(request_id)
    except _InvalidRequest as exc:
        correlation_id = str(uuid.uuid4())
        return _payload(
            correlation_id,
            _extraction_template(source=None, request_sha256=None),
            status="unknown",
            summary="The result.series.extract correlation identity is invalid.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    source: dict[str, object] | None = None
    request_digest: str | None = None
    try:
        source, analysis, artifact, exact_path, source_counts = _simulation_source(
            simulation_result,
            artifact_path,
        )
        normalized_selectors = _selectors(selectors)
        normalized_conditions = _conditions(conditions)
        request_digest = _canonical_sha256(
            {
                "simulation": {
                    "request_id": source["request_id"],
                    "artifact_sha256": artifact["sha256"],
                },
                "selectors": normalized_selectors,
                "conditions": normalized_conditions,
            }
        )
    except _InvalidRequest as exc:
        return _payload(
            correlation_id,
            _extraction_template(source=source, request_sha256=request_digest),
            status="unknown",
            summary="The result.series.extract request could not be evaluated safely.",
            execution_status="invalid_request",
            inputs=[dict(source["artifact"])] if source is not None else (),
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )
    except (OverflowError, TypeError, ValueError) as exc:
        return _payload(
            correlation_id,
            _extraction_template(source=source, request_sha256=None),
            status="unknown",
            summary="The result.series.extract request could not be canonicalized.",
            execution_status="invalid_request",
            inputs=[dict(source["artifact"])] if source is not None else (),
            diagnostics=[
                diagnostic("error", "series.request.invalid", str(exc))
            ],
        )

    selected_native_names = list(
        dict.fromkeys(selector["native_name"] for selector in normalized_selectors)
    )
    extracted = extract_analysis_raw(
        exact_path,
        backend=str(source["backend"]),
        analysis=analysis,
        selected_variables=selected_native_names,
        expected_bytes=int(artifact["bytes"]),
        expected_sha256=str(artifact["sha256"]),
        max_points=MAX_POINTS,
        max_selected_scalars=MAX_SELECTED_SCALARS,
    )
    if not extracted.valid:
        code, message = _raw_diagnostic(extracted)
        return _payload(
            correlation_id,
            _extraction_template(source=source, request_sha256=request_digest),
            status="unknown",
            summary="The native simulation artifact did not yield a trustworthy normalized series.",
            execution_status="completed",
            inputs=[dict(artifact)],
            diagnostics=[diagnostic("error", code, message)],
        )

    variables = int(extracted.metadata["variables"])
    points = int(extracted.metadata["points"])
    dependent_variables = variables if analysis["type"] == "op" else variables - 1
    scalar_width = 2 if extracted.metadata["numeric_type"] == "complex" else 1
    observed_counts = {
        "point_count": points,
        "dependent_variable_count": dependent_variables,
        "finite_value_count": points * dependent_variables * scalar_width,
    }
    if observed_counts != source_counts:
        return _payload(
            correlation_id,
            _extraction_template(source=source, request_sha256=request_digest),
            status="unknown",
            summary="The source simulation facts conflict with the exact native artifact.",
            execution_status="completed",
            inputs=[dict(artifact)],
            diagnostics=[
                diagnostic(
                    "error",
                    "series.artifact.binding_mismatch",
                    "Re-parsed native point, dependent-variable, or finite-value "
                    "counts do not match simulation_result.data.analysis.",
                )
            ],
        )

    try:
        axis = _axis_definition(analysis, extracted)
        signals = _normalized_signals(extracted, normalized_selectors)
        series_sha256 = normalized_series_sha256(
            axis=axis,
            signals=signals,
            conditions=normalized_conditions,
        )
    except (_InvalidRequest, ValueError) as exc:
        code = exc.code if isinstance(exc, _InvalidRequest) else "series.source.invalid"
        return _payload(
            correlation_id,
            _extraction_template(source=source, request_sha256=request_digest),
            status="unknown",
            summary="The selected native vectors could not be normalized safely.",
            execution_status=(
                "invalid_request"
                if code in {
                    "series.selector.component_invalid",
                    "series.unit.mismatch",
                }
                else "completed"
            ),
            inputs=[dict(artifact)],
            diagnostics=[diagnostic("error", code, str(exc))],
        )

    series = {
        "source": {
            "operation": OPERATION_PROFILE,
            "request_id": correlation_id,
            "artifact_role": "measurement.source",
            "artifact_sha256": series_sha256,
            "lineage": {
                "operation": "circuit.simulate",
                "request_id": source["request_id"],
                "artifact_role": "simulation.result",
                "artifact_sha256": artifact["sha256"],
                "binding": "unverified",
            },
        },
        "axis": axis,
        "signals": signals,
        "conditions": normalized_conditions,
        "extensions": {},
    }
    extraction = {
        "status": "extracted",
        "request_sha256": request_digest,
        "source": _source_record(source, binding="verified"),
        "plot": {
            "plotname": extracted.metadata["plotname"],
            "encoding": extracted.metadata["encoding"],
            "numeric_type": extracted.metadata["numeric_type"],
            "point_count": extracted.metadata["points"],
            "native_axis_name": extracted.axis_name,
            "native_axis_type": extracted.axis_native_type,
            "extensions": {},
        },
        "series": series,
        "extensions": {},
    }
    return _payload(
        correlation_id,
        extraction,
        status="pass",
        summary="Extracted a bounded normalized real series from exact native simulation evidence.",
        execution_status="completed",
        inputs=[dict(artifact)],
    )


__all__ = [
    "ASSERTION_PROFILE",
    "IMPLEMENTATION_ID",
    "IMPLEMENTATION_VERSION",
    "MAX_CONDITIONS",
    "MAX_POINTS",
    "MAX_SELECTED_SCALARS",
    "MAX_SELECTORS",
    "OPERATION_PROFILE",
    "extract_result_series",
]
