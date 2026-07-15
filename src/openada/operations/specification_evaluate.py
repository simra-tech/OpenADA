"""Typed specification evaluation with exact unit and condition binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any
import uuid

from ..contract import diagnostic, result, static_execution


OPERATION_PROFILE = "openada.operation/specification.evaluate/v1alpha1"
ASSERTION_PROFILE = "openada.assertion/specification.satisfied/v1alpha1"
IMPLEMENTATION_ID = "org.openada.kernel.typed-evidence"
IMPLEMENTATION_VERSION = "1.0.0"
MAX_CONDITIONS = 64
SPECIFICATION_LIMIT_KINDS = ("lower", "upper")

_ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _InvalidRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _closed_object(
    value: object,
    label: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidRequest("specification.request.invalid", f"{label} must be an object.")
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} field names must all be strings.",
        )
    key_set = set(keys)
    missing = required - key_set
    unexpected = key_set - required - optional
    if missing:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} is missing required fields: {', '.join(sorted(missing))}.",
        )
    if unexpected:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(unexpected))}.",
        )
    return value


def _text(value: object, label: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} must be nonempty text of at most {limit} characters.",
        )
    return value


def _unit(value: object, label: str) -> str:
    return _text(value, label, limit=64)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidRequest("specification.request.invalid", f"{label} must be a JSON number.")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} must be representable as a finite JSON number.",
        ) from exc
    if not math.isfinite(parsed):
        raise _InvalidRequest("specification.request.invalid", f"{label} must be finite.")
    return parsed


def _extensions(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _InvalidRequest("specification.request.invalid", f"{label} must be an object.")
    if value:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label} must be empty in v1alpha1.",
        )
    return {}


def _uuid_text(value: object, label: str) -> str:
    text = _text(value, label, limit=36)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise _InvalidRequest("specification.source.invalid", f"{label} must be a UUID.") from exc
    if str(parsed) != text:
        raise _InvalidRequest(
            "specification.source.invalid",
            f"{label} must use canonical lowercase UUID form.",
        )
    return text


def _correlation_id(value: str | None) -> str:
    return str(uuid.uuid4()) if value is None else _uuid_text(value, "request_id")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scalar(value: object, label: str) -> object:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite_number(value, label)
    if isinstance(value, str):
        return _text(value, label)
    if isinstance(value, bool):
        return value
    raise _InvalidRequest(
        "specification.request.invalid",
        f"{label} must be a finite number, string, or boolean.",
    )


def _condition(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required={"name", "value", "unit"})
    return {
        "name": _text(item["name"], f"{label}.name"),
        "value": _scalar(item["value"], f"{label}.value"),
        "unit": _unit(item["unit"], f"{label}.unit"),
    }


def _source(value: object) -> dict[str, Any]:
    source = _closed_object(
        value,
        "measurement.source",
        required={
            "operation",
            "request_id",
            "artifact_role",
            "artifact_sha256",
            "series_sha256",
            "conditions_sha256",
            "conditions",
            "lineage",
        },
    )
    artifact_role = _text(source["artifact_role"], "measurement.source.artifact_role", limit=120)
    if not _ROLE_RE.fullmatch(artifact_role):
        raise _InvalidRequest(
            "specification.source.invalid",
            "measurement.source.artifact_role is not a canonical role.",
        )
    if artifact_role != "measurement.source":
        raise _InvalidRequest(
            "specification.source.invalid",
            "measurement.source.artifact_role must be 'measurement.source'.",
        )
    digests: dict[str, str] = {}
    for field in ("artifact_sha256", "series_sha256", "conditions_sha256"):
        digest = _text(source[field], f"measurement.source.{field}", limit=64)
        if not _SHA256_RE.fullmatch(digest):
            raise _InvalidRequest(
                "specification.source.invalid",
                f"measurement.source.{field} must be a lowercase SHA-256 digest.",
            )
        digests[field] = digest
    if digests["artifact_sha256"] != digests["series_sha256"]:
        raise _InvalidRequest(
            "specification.source.invalid",
            "measurement.source artifact_sha256 and series_sha256 must identify the same canonical normalized series.",
        )
    if not _is_sequence(source["conditions"]) or len(source["conditions"]) > MAX_CONDITIONS:
        raise _InvalidRequest(
            "specification.source.invalid",
            f"measurement.source.conditions must be an array of at most {MAX_CONDITIONS} entries.",
        )
    conditions = [
        _condition(item, f"measurement.source.conditions[{index}]")
        for index, item in enumerate(source["conditions"])
    ]
    names = [item["name"] for item in conditions]
    if len(names) != len(set(names)):
        raise _InvalidRequest(
            "specification.source.invalid",
            "measurement.source.conditions names must be unique.",
        )
    if digests["conditions_sha256"] != _canonical_sha256(conditions):
        raise _InvalidRequest(
            "specification.source.invalid",
            "measurement.source.conditions_sha256 does not match the normalized condition bindings.",
        )
    lineage: dict[str, object] | None = None
    if source["lineage"] is not None:
        raw_lineage = _closed_object(
            source["lineage"],
            "measurement.source.lineage",
            required={
                "operation",
                "request_id",
                "artifact_role",
                "artifact_sha256",
                "binding",
            },
        )
        lineage_digest = _text(
            raw_lineage["artifact_sha256"],
            "measurement.source.lineage.artifact_sha256",
            limit=64,
        )
        if not _SHA256_RE.fullmatch(lineage_digest):
            raise _InvalidRequest(
                "specification.source.invalid",
                "measurement.source.lineage.artifact_sha256 must be a lowercase SHA-256 digest.",
            )
        lineage_role = _text(
            raw_lineage["artifact_role"],
            "measurement.source.lineage.artifact_role",
            limit=120,
        )
        if not _ROLE_RE.fullmatch(lineage_role):
            raise _InvalidRequest(
                "specification.source.invalid",
                "measurement.source.lineage.artifact_role is not a canonical role.",
            )
        if raw_lineage["binding"] != "unverified":
            raise _InvalidRequest(
                "specification.source.invalid",
                "measurement.source.lineage.binding must be exactly 'unverified'.",
            )
        lineage = {
            "operation": _text(
                raw_lineage["operation"], "measurement.source.lineage.operation"
            ),
            "request_id": _uuid_text(
                raw_lineage["request_id"], "measurement.source.lineage.request_id"
            ),
            "artifact_role": lineage_role,
            "artifact_sha256": lineage_digest,
            "binding": "unverified",
        }
    return {
        "operation": _text(source["operation"], "measurement.source.operation"),
        "request_id": _uuid_text(source["request_id"], "measurement.source.request_id"),
        "artifact_role": artifact_role,
        **digests,
        "conditions": conditions,
        "lineage": lineage,
    }


def _normalize_measurement(value: object) -> dict[str, Any]:
    item = _closed_object(
        value,
        "measurement",
        required={
            "measurement_id",
            "kind",
            "status",
            "request_sha256",
            "value",
            "unit",
            "signal",
            "location",
            "algorithm",
            "sample_count",
            "source",
            "extensions",
        },
    )
    _extensions(item["extensions"], "measurement.extensions")
    measurement_id = _text(item["measurement_id"], "measurement.measurement_id", limit=120)
    if not _ROLE_RE.fullmatch(measurement_id):
        raise _InvalidRequest(
            "specification.measurement.invalid",
            "measurement.measurement_id is not a canonical identifier.",
        )
    status = _text(item["status"], "measurement.status", limit=20)
    if status not in {"measured", "not_found", "unknown"}:
        raise _InvalidRequest(
            "specification.measurement.invalid",
            "measurement.status must be measured, not_found, or unknown.",
        )
    request_sha256: str | None
    if item["request_sha256"] is None:
        request_sha256 = None
    else:
        request_sha256 = _text(
            item["request_sha256"], "measurement.request_sha256", limit=64
        )
        if not _SHA256_RE.fullmatch(request_sha256):
            raise _InvalidRequest(
                "specification.measurement.invalid",
                "measurement.request_sha256 must be a lowercase SHA-256 digest.",
            )
    if status in {"measured", "not_found"} and request_sha256 is None:
        raise _InvalidRequest(
            "specification.measurement.invalid",
            "A measured or not_found record must retain measurement.request_sha256.",
        )
    measured_value: float | None
    measured_unit: str | None
    if status == "measured":
        measured_value = _finite_number(item["value"], "measurement.value")
        measured_unit = _unit(item["unit"], "measurement.unit")
    else:
        if item["value"] is not None:
            raise _InvalidRequest(
                "specification.measurement.invalid",
                "A non-measured record must carry null measurement.value.",
            )
        measured_value = None
        measured_unit = None if item["unit"] is None else _unit(item["unit"], "measurement.unit")

    location: dict[str, object] | None = None
    if item["location"] is not None:
        raw_location = _closed_object(
            item["location"], "measurement.location", required={"value", "unit"}
        )
        location = {
            "value": _finite_number(raw_location["value"], "measurement.location.value"),
            "unit": _unit(raw_location["unit"], "measurement.location.unit"),
        }
    algorithm = _closed_object(
        item["algorithm"], "measurement.algorithm", required={"id", "version"}
    )
    algorithm_id = _text(algorithm["id"], "measurement.algorithm.id")
    algorithm_version = _text(algorithm["version"], "measurement.algorithm.version", limit=100)
    sample_count = item["sample_count"]
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 0
        or sample_count > 100_000
    ):
        raise _InvalidRequest(
            "specification.measurement.invalid",
            "measurement.sample_count must be an integer from 0 to 100000.",
        )
    return {
        "measurement_id": measurement_id,
        "kind": _text(item["kind"], "measurement.kind", limit=40),
        "status": status,
        "request_sha256": request_sha256,
        "value": measured_value,
        "unit": measured_unit,
        "signal": _text(item["signal"], "measurement.signal"),
        "location": location,
        "algorithm": {"id": algorithm_id, "version": algorithm_version},
        "sample_count": sample_count,
        "source": _source(item["source"]),
        "extensions": dict(item["extensions"]),
    }


def _bound(value: object, label: str) -> dict[str, object]:
    item = _closed_object(value, label, required={"value", "unit", "inclusive"})
    if not isinstance(item["inclusive"], bool):
        raise _InvalidRequest(
            "specification.request.invalid",
            f"{label}.inclusive must be boolean.",
        )
    return {
        "value": _finite_number(item["value"], f"{label}.value"),
        "unit": _unit(item["unit"], f"{label}.unit"),
        "inclusive": item["inclusive"],
    }


def _normalize_specification(value: object) -> dict[str, Any]:
    item = _closed_object(
        value,
        "specification",
        required={"specification_id", "measurement_id", "limits", "conditions", "extensions"},
    )
    _extensions(item["extensions"], "specification.extensions")
    specification_id = _text(item["specification_id"], "specification.specification_id", limit=120)
    measurement_id = _text(item["measurement_id"], "specification.measurement_id", limit=120)
    if not _ROLE_RE.fullmatch(specification_id) or not _ROLE_RE.fullmatch(measurement_id):
        raise _InvalidRequest(
            "specification.request.invalid",
            "specification identifiers must be canonical role-like identifiers.",
        )
    limits = _closed_object(
        item["limits"],
        "specification.limits",
        required=set(),
        optional={"lower", "upper"},
    )
    if not limits:
        raise _InvalidRequest(
            "specification.request.invalid",
            "specification.limits must declare at least one lower or upper bound.",
        )
    normalized_limits = {
        name: _bound(raw, f"specification.limits.{name}")
        for name, raw in limits.items()
    }
    if set(normalized_limits) == {"lower", "upper"}:
        lower = normalized_limits["lower"]
        upper = normalized_limits["upper"]
        if lower["unit"] != upper["unit"]:
            raise _InvalidRequest(
                "specification.unit.mismatch",
                "Lower and upper specification limits must use the same exact unit.",
            )
        if lower["value"] > upper["value"] or (
            lower["value"] == upper["value"]
            and (not lower["inclusive"] or not upper["inclusive"])
        ):
            raise _InvalidRequest(
                "specification.request.invalid",
                "The declared lower and upper limits form an empty interval.",
            )

    if not _is_sequence(item["conditions"]) or len(item["conditions"]) > MAX_CONDITIONS:
        raise _InvalidRequest(
            "specification.request.invalid",
            f"specification.conditions must be an array of at most {MAX_CONDITIONS} entries.",
        )
    conditions = [
        _condition(raw, f"specification.conditions[{index}]")
        for index, raw in enumerate(item["conditions"])
    ]
    names = [condition["name"] for condition in conditions]
    if len(names) != len(set(names)):
        raise _InvalidRequest(
            "specification.request.invalid",
            "specification.conditions names must be unique.",
        )
    return {
        "specification_id": specification_id,
        "measurement_id": measurement_id,
        "limits": normalized_limits,
        "conditions": conditions,
        "extensions": dict(item["extensions"]),
    }


def _evaluation_template(
    *,
    specification_id: str | None,
    measurement_id: str | None,
    measurement: dict[str, Any] | None,
    specification: dict[str, Any] | None,
    limits: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "specification_id": specification_id,
        "measurement_id": measurement_id,
        "status": "unknown",
        "measured": (
            {"value": measurement["value"], "unit": measurement["unit"]}
            if measurement is not None and measurement.get("status") == "measured"
            else None
        ),
        "limits": [
            {"kind": name, **bound}
            for name, bound in (limits or {}).items()
        ],
        "conditions": {
            "status": "not-established",
            "required_count": 0,
            "matched_count": 0,
        },
        "margin": None,
        "algorithm": {
            "id": "openada.algorithm/specification.closed-interval/v1",
            "version": IMPLEMENTATION_VERSION,
        },
        "source": (
            {
                "measurement_sha256": _canonical_sha256(measurement),
                "measurement_source": measurement["source"],
                "specification_sha256": _canonical_sha256(specification),
                "specification": specification,
            }
            if measurement is not None and specification is not None
            else None
        ),
        "extensions": {},
    }


def _payload(
    correlation_id: str,
    evaluation: dict[str, Any],
    *,
    status: str,
    summary: str,
    execution_status: str = "completed",
    diagnostics: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return result(
        "specification.evaluate",
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=status,
        summary=summary,
        diagnostics=diagnostics,
        data={
            "protocol": {
                "request_id": correlation_id,
                "operation_profile": OPERATION_PROFILE,
                "assertion_profile": ASSERTION_PROFILE,
                "implementation_id": IMPLEMENTATION_ID,
                "implementation_version": IMPLEMENTATION_VERSION,
            },
            "evaluation": evaluation,
            "extensions": {},
        },
    )


def evaluate_specification(
    measurement: Mapping[str, object],
    specification: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Compare one typed measurement with explicit bounds and conditions.

    Units are intentionally not converted in v1alpha1.  Exact unit equality and
    exact binding of every declared operating condition are prerequisites for a
    pass or fail decision; otherwise the result remains unknown.
    """

    try:
        correlation_id = _correlation_id(request_id)
    except _InvalidRequest as exc:
        correlation_id = str(uuid.uuid4())
        empty = _evaluation_template(
            specification_id=None,
            measurement_id=None,
            measurement=None,
            specification=None,
            limits=None,
        )
        return _payload(
            correlation_id,
            empty,
            status="unknown",
            summary="The specification.evaluate correlation identity is invalid.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )

    normalized_measurement: dict[str, Any] | None = None
    normalized_specification: dict[str, Any] | None = None
    try:
        normalized_measurement = _normalize_measurement(measurement)
        normalized_specification = _normalize_specification(specification)
        if normalized_specification["measurement_id"] != normalized_measurement["measurement_id"]:
            raise _InvalidRequest(
                "specification.measurement.mismatch",
                "The specification measurement_id does not match the supplied measurement.",
            )
        evaluation = _evaluation_template(
            specification_id=normalized_specification["specification_id"],
            measurement_id=normalized_measurement["measurement_id"],
            measurement=normalized_measurement,
            specification=normalized_specification,
            limits=normalized_specification["limits"],
        )
        required_conditions = normalized_specification["conditions"]
        evaluation["conditions"]["required_count"] = len(required_conditions)

        if normalized_measurement["status"] != "measured":
            return _payload(
                correlation_id,
                evaluation,
                status="unknown",
                summary="The specification cannot be evaluated because its measurement has no value.",
                diagnostics=[
                    diagnostic(
                        "error",
                        "specification.measurement.unknown",
                        f"Measurement status {normalized_measurement['status']!r} carries no finite value.",
                        hint="Produce a successful typed measurement before evaluating this specification.",
                    )
                ],
            )

        measured_unit = normalized_measurement["unit"]
        for name, bound in normalized_specification["limits"].items():
            if bound["unit"] != measured_unit:
                return _payload(
                    correlation_id,
                    evaluation,
                    status="unknown",
                    summary="The specification units do not match the measured value.",
                    diagnostics=[
                        diagnostic(
                            "error",
                            "specification.unit.mismatch",
                            f"The {name} limit uses {bound['unit']!r}, while the measurement uses {measured_unit!r}.",
                            hint="Supply limits in the exact measurement unit; v1alpha1 performs no implicit conversion.",
                        )
                    ],
                )

        actual_conditions = {
            item["name"]: item for item in normalized_measurement["source"]["conditions"]
        }
        matched = 0
        for required in required_conditions:
            actual = actual_conditions.get(required["name"])
            if actual is None:
                evaluation["conditions"]["matched_count"] = matched
                return _payload(
                    correlation_id,
                    evaluation,
                    status="unknown",
                    summary="A required specification condition is absent from the measurement evidence.",
                    diagnostics=[
                        diagnostic(
                            "error",
                            "specification.condition.unproven",
                            f"Measurement evidence does not bind condition {required['name']!r}.",
                        )
                    ],
                )
            if actual["unit"] != required["unit"]:
                evaluation["conditions"]["matched_count"] = matched
                return _payload(
                    correlation_id,
                    evaluation,
                    status="unknown",
                    summary="A required specification condition has incompatible units.",
                    diagnostics=[
                        diagnostic(
                            "error",
                            "specification.unit.mismatch",
                            f"Condition {required['name']!r} uses {actual['unit']!r} in the measurement and {required['unit']!r} in the specification.",
                        )
                    ],
                )
            if type(actual["value"]) is not type(required["value"]) or actual["value"] != required["value"]:
                evaluation["conditions"]["matched_count"] = matched
                return _payload(
                    correlation_id,
                    evaluation,
                    status="unknown",
                    summary="The measurement was not produced at a required specification condition.",
                    diagnostics=[
                        diagnostic(
                            "error",
                            "specification.condition.unproven",
                            f"Condition {required['name']!r} does not exactly match the measurement evidence.",
                        )
                    ],
                )
            matched += 1
        evaluation["conditions"] = {
            "status": "matched",
            "required_count": len(required_conditions),
            "matched_count": matched,
        }

        measured_value = normalized_measurement["value"]
        assert isinstance(measured_value, float)
        violations: list[str] = []
        margins: list[tuple[float, str]] = []
        lower = normalized_specification["limits"].get("lower")
        if lower is not None:
            margin = measured_value - lower["value"]
            if not math.isfinite(margin):
                raise _InvalidRequest(
                    "specification.value.non_finite",
                    "The lower-bound margin is not representable as a finite JSON number.",
                )
            margins.append((margin, "lower"))
            if measured_value < lower["value"] or (
                measured_value == lower["value"] and not lower["inclusive"]
            ):
                violations.append("lower")
        upper = normalized_specification["limits"].get("upper")
        if upper is not None:
            margin = upper["value"] - measured_value
            if not math.isfinite(margin):
                raise _InvalidRequest(
                    "specification.value.non_finite",
                    "The upper-bound margin is not representable as a finite JSON number.",
                )
            margins.append((margin, "upper"))
            if measured_value > upper["value"] or (
                measured_value == upper["value"] and not upper["inclusive"]
            ):
                violations.append("upper")
        limiting_margin, limiting_bound = min(margins, key=lambda item: item[0])
        evaluation["margin"] = {
            "value": limiting_margin,
            "unit": measured_unit,
            "relative_to": limiting_bound,
        }

        if violations:
            evaluation["status"] = "fail"
            return _payload(
                correlation_id,
                evaluation,
                status="fail",
                summary=f"Specification {normalized_specification['specification_id']!r} is violated.",
                diagnostics=[
                    diagnostic(
                        "error",
                        "specification.limit.violated",
                        f"The measured value violates the {', '.join(violations)} limit.",
                    )
                ],
            )

        evaluation["status"] = "pass"
        return _payload(
            correlation_id,
            evaluation,
            status="pass",
            summary=f"Specification {normalized_specification['specification_id']!r} is satisfied.",
        )
    except _InvalidRequest as exc:
        evaluation = _evaluation_template(
            specification_id=(
                normalized_specification["specification_id"]
                if normalized_specification is not None
                else None
            ),
            measurement_id=(
                normalized_measurement["measurement_id"]
                if normalized_measurement is not None
                else None
            ),
            measurement=normalized_measurement,
            specification=normalized_specification,
            limits=(
                normalized_specification["limits"]
                if normalized_specification is not None
                else None
            ),
        )
        return _payload(
            correlation_id,
            evaluation,
            status="unknown",
            summary="The specification.evaluate request could not be evaluated safely.",
            execution_status="invalid_request",
            diagnostics=[diagnostic("error", exc.code, str(exc))],
        )


__all__ = [
    "ASSERTION_PROFILE",
    "IMPLEMENTATION_ID",
    "IMPLEMENTATION_VERSION",
    "OPERATION_PROFILE",
    "SPECIFICATION_LIMIT_KINDS",
    "evaluate_specification",
]
