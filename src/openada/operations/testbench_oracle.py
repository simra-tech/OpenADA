"""Pure comparison kernel for testbench-plan observables.

The comparator deliberately performs no simulation and reads no files.  It
accepts JSON-compatible mappings, validates a closed tolerance document, and
returns deterministic per-row verdicts.  Error semantics are always explicit:
an absolute error is never silently changed into a relative error and every
relative denominator guard is part of the tolerance artifact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


TOLERANCE_SCHEMA = "simra.testbench-oracle-tolerances/v1"
COMPARISON_SCHEMA = "simra.testbench-oracle-comparison/v1"

MAX_METRICS = 128
MAX_SEQUENCE_ITEMS = 1_000_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 2_000_000

_STATUS_PASS = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_UNKNOWN = "UNKNOWN"
_OPS = frozenset(("<=", ">="))

_COMMON_ROW_FIELDS = frozenset(("name", "kind", "required", "limit"))
_ROW_FIELDS: dict[str, frozenset[str]] = {
    "scalar": _COMMON_ROW_FIELDS
    | frozenset(("observed", "oracle", "error")),
    "curve": _COMMON_ROW_FIELDS
    | frozenset(("observed", "oracle", "x", "y", "error")),
    "mismatch_curve": _COMMON_ROW_FIELDS
    | frozenset(
        (
            "observed_source",
            "observed_sink",
            "oracle_source",
            "oracle_sink",
            "denominator_floor",
        )
    ),
    "compliance_endpoints": _COMMON_ROW_FIELDS
    | frozenset(("observed", "oracle")),
    "signed_response_coverage": _COMMON_ROW_FIELDS
    | frozenset(("observed", "oracle", "x", "y", "zero_epsilon")),
    "invalid_detection_recall": _COMMON_ROW_FIELDS | frozenset(("denominator",)),
    "false_valid_rate": _COMMON_ROW_FIELDS | frozenset(("denominator",)),
    "completeness": _COMMON_ROW_FIELDS
    | frozenset(("observables", "conditions")),
    "grading_runtime": _COMMON_ROW_FIELDS,
    "lineage_presence": _COMMON_ROW_FIELDS
    | frozenset(("observables", "conditions")),
}


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _strict_json(value: object, *, label: str) -> None:
    """Refuse non-JSON, non-finite, cyclic, or unbounded in-process values."""

    active: set[int] = set()
    nodes = 0

    def visit(item: object, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError(f"{label}{path}: JSON node limit exceeded")
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{label}{path}: JSON nesting exceeds {MAX_JSON_DEPTH}")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{label}{path}: JSON numbers must be finite")
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise ValueError(f"{label}{path}: cyclic object is not JSON")
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(
                            f"{label}{path}: JSON object keys must be strings"
                        )
                    visit(child, f"{path}/{key}", depth + 1)
            finally:
                active.remove(identity)
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            if len(item) > MAX_SEQUENCE_ITEMS:
                raise ValueError(f"{label}{path}: JSON array is over the item limit")
            identity = id(item)
            if identity in active:
                raise ValueError(f"{label}{path}: cyclic array is not JSON")
            active.add(identity)
            try:
                for index, child in enumerate(item):
                    visit(child, f"{path}/{index}", depth + 1)
            finally:
                active.remove(identity)
            return
        raise ValueError(
            f"{label}{path}: value of type {type(item).__name__} is not JSON"
        )

    visit(value, "", 0)


def _closed(
    value: object,
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise ValueError(f"{label} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} has unknown field(s): {', '.join(unknown)}")
    return value


def _identifier(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 120
        or not value[0].islower()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)
    ):
        raise ValueError(
            f"{label} must match ^[a-z][a-z0-9_]{{0,119}}$"
        )
    return value


def _field_name(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a bounded nonempty field name")
    return value


def _string_array(
    value: object,
    *,
    label: str,
    maximum: int = 256,
    minimum: int = 1,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 256
            or any(ord(character) < 33 or ord(character) == 127 for character in item)
            for item in value
        )
        or len(value) != len(set(value))
    ):
        qualifier = "a unique string array" if minimum == 0 else "a nonempty unique string array"
        raise ValueError(f"{label} must be {qualifier}")
    return list(value)


def _limit(value: object, *, label: str) -> dict[str, Any]:
    item = _closed(
        value,
        label=label,
        required=frozenset(("op", "value", "unit")),
    )
    if item["op"] not in _OPS:
        raise ValueError(f"{label}.op must be '<=' or '>='")
    if not _finite_number(item["value"]):
        raise ValueError(f"{label}.value must be finite")
    if not isinstance(item["unit"], str) or not 1 <= len(item["unit"]) <= 64:
        raise ValueError(f"{label}.unit must be a bounded nonempty string")
    return {"op": item["op"], "value": float(item["value"]), "unit": item["unit"]}


def _error(value: object, *, label: str, curve: bool) -> dict[str, Any]:
    item = _closed(
        value,
        label=label,
        required=frozenset(("kind",)),
        optional=frozenset(("denominator_floor", "absolute_guard")),
    )
    allowed = {"absolute", "relative"}
    if curve:
        allowed.add("guarded_relative")
    else:
        allowed.add("fraction_absolute")
    if item["kind"] not in allowed:
        raise ValueError(
            f"{label}.kind must be one of {', '.join(sorted(allowed))}"
        )
    output = {"kind": item["kind"]}
    if item["kind"] == "relative":
        floor = item.get("denominator_floor")
        if not _finite_number(floor) or float(floor) <= 0:
            raise ValueError(
                f"{label}.denominator_floor must be explicit and greater than zero"
            )
        if "absolute_guard" in item:
            raise ValueError(f"{label}.absolute_guard is not valid for relative error")
        output["denominator_floor"] = float(floor)
    elif item["kind"] == "guarded_relative":
        guard = item.get("absolute_guard")
        if not _finite_number(guard) or float(guard) <= 0:
            raise ValueError(
                f"{label}.absolute_guard must be explicit and greater than zero"
            )
        if "denominator_floor" in item:
            raise ValueError(
                f"{label}.denominator_floor is not valid for guarded_relative error"
            )
        output["absolute_guard"] = float(guard)
    elif set(item) != {"kind"}:
        raise ValueError(f"{label} declares a guard not used by absolute error")
    return output


def _normalize_tolerances(value: Mapping[str, Any]) -> dict[str, Any]:
    root = _closed(
        value,
        label="tolerances",
        required=frozenset(
            ("schema", "metrics", "lineage_required", "extensions")
        ),
    )
    if root["schema"] != TOLERANCE_SCHEMA:
        raise ValueError(f"tolerances.schema must be {TOLERANCE_SCHEMA!r}")
    if root["extensions"] != {}:
        raise ValueError("tolerances.extensions must be empty in v1")
    if not isinstance(root["lineage_required"], bool):
        raise ValueError("tolerances.lineage_required must be boolean")
    rows = root["metrics"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_METRICS:
        raise ValueError(
            f"tolerances.metrics must contain 1..{MAX_METRICS} rows"
        )
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(rows):
        label = f"tolerances.metrics[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be an object")
        kind = raw.get("kind")
        fields = _ROW_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            raise ValueError(f"{label}.kind is unsupported")
        item = _closed(raw, label=label, required=fields)
        name = _identifier(item["name"], label=f"{label}.name")
        if name in names:
            raise ValueError(f"{label}.name duplicates {name!r}")
        names.add(name)
        if not isinstance(item["required"], bool):
            raise ValueError(f"{label}.required must be boolean")
        row: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "required": item["required"],
            "limit": _limit(item["limit"], label=f"{label}.limit"),
        }
        for field in (
            "observed",
            "oracle",
            "observed_source",
            "observed_sink",
            "oracle_source",
            "oracle_sink",
            "x",
            "y",
        ):
            if field in item:
                row[field] = _field_name(item[field], label=f"{label}.{field}")
        if "error" in item:
            row["error"] = _error(
                item["error"], label=f"{label}.error", curve=kind == "curve"
            )
        if "denominator_floor" in item:
            floor = item["denominator_floor"]
            if not _finite_number(floor) or float(floor) <= 0:
                raise ValueError(
                    f"{label}.denominator_floor must be greater than zero"
                )
            row["denominator_floor"] = float(floor)
        if "zero_epsilon" in item:
            epsilon = item["zero_epsilon"]
            if not _finite_number(epsilon) or float(epsilon) < 0:
                raise ValueError(f"{label}.zero_epsilon must be non-negative")
            row["zero_epsilon"] = float(epsilon)
        if kind in {"invalid_detection_recall", "false_valid_rate"}:
            expected_denominator = (
                "oracle_invalid"
                if kind == "invalid_detection_recall"
                else "submitted_valid"
            )
            if item["denominator"] != expected_denominator:
                raise ValueError(
                    f"{label}.denominator must be {expected_denominator!r}"
                )
            row["denominator"] = expected_denominator
        for field in ("observables", "conditions"):
            if field in item:
                row[field] = _string_array(item[field], label=f"{label}.{field}")
        normalized.append(row)
    return {
        "schema": TOLERANCE_SCHEMA,
        "metrics": normalized,
        "lineage_required": root["lineage_required"],
        "extensions": {},
    }


def _document(value: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    """Accept the oracle fixture shape or the typed plan-emission shape."""

    if value.get("schema") == "simra.testbench-observables/v1":
        root = _closed(
            value,
            label=role,
            required=frozenset(
                (
                    "schema",
                    "plan_sha256",
                    "dut_sha256",
                    "corner",
                    "validity",
                    "observables",
                    "metadata",
                    "extensions",
                )
            ),
        )
        for field in ("plan_sha256", "dut_sha256"):
            digest = root[field]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{role}.{field} must be a lowercase SHA-256")
        if root["extensions"] != {}:
            raise ValueError(f"{role}.extensions must be empty in v1")
        allowed = root
    else:
        allowed = _closed(
            value,
            label=role,
            required=frozenset(("sizing", "corner", "validity", "observables")),
            optional=frozenset(("metadata",)),
        )
        sizing = _closed(
            allowed["sizing"],
            label=f"{role}.sizing",
            required=frozenset(("topology", "parameters")),
        )
        if (
            not isinstance(sizing["topology"], str)
            or not 1 <= len(sizing["topology"]) <= 256
        ):
            raise ValueError(f"{role}.sizing.topology must be bounded nonempty text")
        if not isinstance(sizing["parameters"], Mapping) or any(
            not isinstance(key, str)
            or not key
            or not _finite_number(parameter)
            for key, parameter in (
                sizing["parameters"].items()
                if isinstance(sizing["parameters"], Mapping)
                else ()
            )
        ):
            raise ValueError(
                f"{role}.sizing.parameters must map nonempty names to finite numbers"
            )
    if (
        not isinstance(allowed["corner"], str)
        or not 1 <= len(allowed["corner"]) <= 256
    ):
        raise ValueError(f"{role}.corner must be bounded nonempty text")
    validity = allowed["validity"]
    observables = allowed["observables"]
    if not isinstance(validity, Mapping):
        raise ValueError(f"{role}.validity must be an object")
    if not isinstance(observables, Mapping):
        raise ValueError(f"{role}.observables must be an object")
    for key, verdict in validity.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError(f"{role}.validity keys must be bounded nonempty strings")
        if not isinstance(verdict, str) or not 1 <= len(verdict) <= 1024:
            raise ValueError(f"{role}.validity.{key} must be bounded nonempty text")
    metadata = allowed.get("metadata")
    if metadata is not None:
        _metadata(metadata, role=role)
    return {
        "corner": allowed["corner"],
        "validity": dict(validity),
        "observables": dict(observables),
        "metadata": metadata,
    }


def _metadata(value: object, *, role: str) -> Mapping[str, Any]:
    root = _closed(
        value,
        label=f"{role}.metadata",
        required=frozenset(
            ("grading_runtime_s", "conditions", "lineage", "extensions")
        ),
    )
    if root["extensions"] != {}:
        raise ValueError(f"{role}.metadata.extensions must be empty")
    if not _finite_number(root["grading_runtime_s"]) or float(
        root["grading_runtime_s"]
    ) < 0:
        raise ValueError(f"{role}.metadata.grading_runtime_s must be non-negative")
    conditions = root["conditions"]
    if not isinstance(conditions, list) or len(conditions) > 10_000:
        raise ValueError(f"{role}.metadata.conditions must be a bounded array")
    condition_ids: set[str] = set()
    for index, condition in enumerate(conditions):
        item = _closed(
            condition,
            label=f"{role}.metadata.conditions[{index}]",
            required=frozenset(("id", "observables", "receipt")),
        )
        identifier = _field_name(
            item["id"], label=f"{role}.metadata.conditions[{index}].id"
        )
        if identifier in condition_ids:
            raise ValueError(f"{role}.metadata condition id {identifier!r} repeats")
        condition_ids.add(identifier)
        _string_array(
            item["observables"],
            label=f"{role}.metadata.conditions[{index}].observables",
            maximum=10_000,
            minimum=0,
        )
        receipt = _closed(
            item["receipt"],
            label=f"{role}.metadata.conditions[{index}].receipt",
            required=frozenset(("compiled_deck_sha256", "waveform_sha256")),
        )
        for field in ("compiled_deck_sha256", "waveform_sha256"):
            digest = receipt[field]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(
                    f"{role}.metadata.conditions[{index}].receipt.{field} "
                    "must be a lowercase SHA-256"
                )
    lineage = root["lineage"]
    if not isinstance(lineage, list) or len(lineage) > 10_000:
        raise ValueError(f"{role}.metadata.lineage must be a bounded array")
    lineage_names: set[str] = set()
    for index, entry in enumerate(lineage):
        item = _closed(
            entry,
            label=f"{role}.metadata.lineage[{index}]",
            required=frozenset(("observable", "condition_ids")),
        )
        observable = _field_name(
            item["observable"],
            label=f"{role}.metadata.lineage[{index}].observable",
        )
        if observable in lineage_names:
            raise ValueError(f"{role}.metadata lineage for {observable!r} repeats")
        lineage_names.add(observable)
        ids = _string_array(
            item["condition_ids"],
            label=f"{role}.metadata.lineage[{index}].condition_ids",
            maximum=10_000,
        )
        unknown = sorted(set(ids) - condition_ids)
        if unknown:
            raise ValueError(
                f"{role}.metadata lineage names unknown condition(s): "
                + ", ".join(unknown)
            )
    return root


def _numbers(value: object, *, label: str) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not _finite_number(item) for item in value):
        return None
    return [float(item) for item in value]


def _curve(
    value: object, *, x_name: str, y_name: str
) -> tuple[list[float], list[float]] | None:
    if not isinstance(value, Mapping) or set(value) != {x_name, y_name}:
        return None
    x = _numbers(value[x_name], label=x_name)
    y = _numbers(value[y_name], label=y_name)
    if x is None or y is None or len(x) != len(y):
        return None
    return x, y


def _verdict(error: float, limit: Mapping[str, Any]) -> str:
    target = float(limit["value"])
    if limit["op"] == "<=":
        return _STATUS_PASS if error <= target else _STATUS_FAIL
    return _STATUS_PASS if error >= target else _STATUS_FAIL


def _unknown(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "required": row["required"],
        "status": _STATUS_UNKNOWN,
        "value": None,
        "unit": row["limit"]["unit"],
        "limit": dict(row["limit"]),
        "reason": reason,
        "details": {},
    }


def _measured(
    row: Mapping[str, Any], value: float, *, reason: str, details: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "required": row["required"],
        "status": _verdict(value, row["limit"]),
        "value": value,
        "unit": row["limit"]["unit"],
        "limit": dict(row["limit"]),
        "reason": reason,
        "details": dict(details),
    }


def _lineage_coverage(
    observed: Mapping[str, Any],
    observable_names: Sequence[str],
    condition_names: Sequence[str] | None = None,
) -> tuple[int, int, list[str]] | None:
    metadata = observed.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    conditions = {
        item["id"]: item
        for item in metadata["conditions"]
        if isinstance(item, Mapping)
    }
    lineages = {
        item["observable"]: set(item["condition_ids"])
        for item in metadata["lineage"]
        if isinstance(item, Mapping)
    }
    missing: list[str] = []
    total = 0
    covered = 0
    if condition_names is None:
        for observable in observable_names:
            total += 1
            linked_ids = lineages.get(observable, set())
            linked = any(
                condition_id in conditions
                and observable in conditions[condition_id].get("observables", [])
                for condition_id in linked_ids
            )
            if observable in observed["observables"] and linked:
                covered += 1
            else:
                missing.append(observable)
        return covered, total, missing

    selected_conditions = list(condition_names)
    for condition_id in selected_conditions:
        condition = conditions.get(condition_id)
        for observable in observable_names:
            total += 1
            declared = (
                isinstance(condition, Mapping)
                and observable in condition.get("observables", [])
            )
            linked = condition_id in lineages.get(observable, set())
            if observable in observed["observables"] and declared and linked:
                covered += 1
            else:
                missing.append(f"{condition_id}:{observable}")
    return covered, total, missing


def _condition_coverage(
    observed: Mapping[str, Any],
    observable_names: Sequence[str],
    condition_names: Sequence[str],
) -> tuple[int, int, list[str]] | None:
    """Count executed condition/observable inventory independently of lineage."""

    metadata = observed.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    conditions = {
        item["id"]: item
        for item in metadata["conditions"]
        if isinstance(item, Mapping)
    }
    missing: list[str] = []
    total = 0
    present = 0
    for condition_id in condition_names:
        condition = conditions.get(condition_id)
        declared_observables = (
            condition.get("observables", [])
            if isinstance(condition, Mapping)
            else []
        )
        for observable in observable_names:
            total += 1
            if (
                observable in observed["observables"]
                and observable in declared_observables
            ):
                present += 1
            else:
                missing.append(f"{condition_id}:{observable}")
    return present, total, missing


def _row_observables(row: Mapping[str, Any]) -> list[str]:
    fields = (
        "observed",
        "observed_source",
        "observed_sink",
    )
    return list(dict.fromkeys(str(row[field]) for field in fields if field in row))


def _scalar_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    observed_value = observed["observables"].get(row["observed"])
    oracle_value = oracle["observables"].get(row["oracle"])
    if not _finite_number(observed_value) or not _finite_number(oracle_value):
        return _unknown(row, "the observed or oracle scalar is missing or non-finite")
    delta = abs(float(observed_value) - float(oracle_value))
    error = row["error"]
    if error["kind"] in {"absolute", "fraction_absolute"}:
        if error["kind"] == "fraction_absolute" and not (
            0.0 <= float(observed_value) <= 1.0
            and 0.0 <= float(oracle_value) <= 1.0
        ):
            return _unknown(
                row,
                "fraction_absolute requires observed and oracle values in [0,1]",
            )
        value = delta
    else:
        denominator = max(abs(float(oracle_value)), error["denominator_floor"])
        value = delta / denominator
    return _measured(
        row,
        value,
        reason=f"{error['kind']} scalar error evaluated",
        details={
            "observed": float(observed_value),
            "oracle": float(oracle_value),
            "absolute_error": delta,
            **(
                {"denominator_floor": error["denominator_floor"]}
                if error["kind"] == "relative"
                else {}
            ),
        },
    )


def _curve_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    observed_curve = _curve(
        observed["observables"].get(row["observed"]),
        x_name=row["x"],
        y_name=row["y"],
    )
    oracle_curve = _curve(
        oracle["observables"].get(row["oracle"]),
        x_name=row["x"],
        y_name=row["y"],
    )
    if observed_curve is None or oracle_curve is None:
        return _unknown(row, "the observed or oracle curve is missing or malformed")
    observed_x, observed_y = observed_curve
    oracle_x, oracle_y = oracle_curve
    if observed_x != oracle_x:
        return _unknown(row, "curve domains differ; v1 performs no interpolation")
    error = row["error"]
    errors: list[float] = []
    absolute_errors: list[float] = []
    for observed_value, oracle_value in zip(observed_y, oracle_y):
        absolute = abs(observed_value - oracle_value)
        absolute_errors.append(absolute)
        if error["kind"] == "absolute":
            errors.append(absolute)
        elif error["kind"] == "relative":
            errors.append(
                absolute
                / max(abs(oracle_value), float(error["denominator_floor"]))
            )
        else:
            errors.append(
                absolute / max(abs(oracle_value), float(error["absolute_guard"]))
            )
    worst = max(range(len(errors)), key=errors.__getitem__)
    return _measured(
        row,
        errors[worst],
        reason=f"worst-point {error['kind']} curve error evaluated",
        details={
            "point_count": len(errors),
            "worst_index": worst,
            "worst_x": oracle_x[worst],
            "absolute_error": absolute_errors[worst],
            **(
                {"absolute_guard": error["absolute_guard"]}
                if error["kind"] == "guarded_relative"
                else (
                    {"denominator_floor": error["denominator_floor"]}
                    if error["kind"] == "relative"
                    else {}
                )
            ),
        },
    )


def _array_or_curve_y(value: object) -> list[float] | None:
    # This row intentionally consumes only the oracle fixture's positional
    # source/sink arrays.  Guessing which member of an arbitrary mapping is the
    # ordinate makes the score depend on object insertion order.
    return _numbers(value, label="array")


def _mismatch_values(
    source: object, sink: object, floor: float
) -> list[float] | None:
    source_values = _array_or_curve_y(source)
    sink_values = _array_or_curve_y(sink)
    if (
        source_values is None
        or sink_values is None
        or len(source_values) != len(sink_values)
    ):
        return None
    output: list[float] = []
    for source_value, sink_value in zip(source_values, sink_values):
        source_magnitude = abs(source_value)
        sink_magnitude = abs(sink_value)
        denominator = max(source_magnitude, sink_magnitude)
        if denominator < floor:
            return None
        output.append(abs(source_magnitude - sink_magnitude) / denominator)
    return output


def _mismatch_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    floor = float(row["denominator_floor"])
    observed_values = _mismatch_values(
        observed["observables"].get(row["observed_source"]),
        observed["observables"].get(row["observed_sink"]),
        floor,
    )
    oracle_values = _mismatch_values(
        oracle["observables"].get(row["oracle_source"]),
        oracle["observables"].get(row["oracle_sink"]),
        floor,
    )
    if (
        observed_values is None
        or oracle_values is None
        or len(observed_values) != len(oracle_values)
    ):
        return _unknown(
            row,
            "source/sink arrays are missing, unaligned, or below the explicit denominator floor",
        )
    errors = [abs(left - right) for left, right in zip(observed_values, oracle_values)]
    worst = max(range(len(errors)), key=errors.__getitem__)
    return _measured(
        row,
        errors[worst],
        reason="worst absolute mismatch-fraction error evaluated",
        details={
            "point_count": len(errors),
            "worst_index": worst,
            "observed_fraction": observed_values[worst],
            "oracle_fraction": oracle_values[worst],
            "denominator_floor": floor,
        },
    )


def _compliance_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    left = observed["observables"].get(row["observed"])
    right = oracle["observables"].get(row["oracle"])
    required = {"lo_v", "hi_v"}
    if (
        not isinstance(left, Mapping)
        or not isinstance(right, Mapping)
        or set(left) != required
        or set(right) != required
        or any(not _finite_number(item) for item in (*left.values(), *right.values()))
    ):
        return _unknown(
            row,
            "dense-sweep compliance endpoints {lo_v,hi_v} are not present in both documents",
        )
    errors = {
        "lo_v": abs(float(left["lo_v"]) - float(right["lo_v"])),
        "hi_v": abs(float(left["hi_v"]) - float(right["hi_v"])),
    }
    return _measured(
        row,
        max(errors.values()),
        reason="worst absolute compliance-endpoint error evaluated",
        details=errors,
    )


def _signed_coverage_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    left = _curve(
        observed["observables"].get(row["observed"]),
        x_name=row["x"],
        y_name=row["y"],
    )
    right = _curve(
        oracle["observables"].get(row["oracle"]),
        x_name=row["x"],
        y_name=row["y"],
    )
    if left is None or right is None:
        return _unknown(row, "the signed response curve is absent or malformed")
    left_x, left_y = left
    right_x, right_y = right
    if left_x != right_x:
        return _unknown(row, "signed-response domains differ; v1 does no interpolation")
    epsilon = float(row["zero_epsilon"])
    eligible = [index for index, value in enumerate(right_y) if abs(value) > epsilon]
    if not eligible:
        return _unknown(row, "oracle has no response outside the declared zero band")
    matches = sum(
        1
        for index in eligible
        if abs(left_y[index]) > epsilon
        and math.copysign(1.0, left_y[index]) == math.copysign(1.0, right_y[index])
    )
    coverage = matches / len(eligible)
    return _measured(
        row,
        coverage,
        reason="signed response coverage evaluated",
        details={
            "eligible": len(eligible),
            "matching_sign": matches,
            "zero_epsilon": epsilon,
        },
    )


def _classify_validity(value: object) -> str:
    if value == "VALID":
        return "valid"
    if isinstance(value, str) and value.endswith(")") and (
        len(value) > len("INVALID()") and value.startswith("INVALID(")
        or len(value) > len("NEEDS_FINE_SWEEP()")
        and value.startswith("NEEDS_FINE_SWEEP(")
    ):
        return "invalid"
    return "unknown"


def _validity_counts(
    observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, int]:
    names = sorted(set(observed["validity"]) | set(oracle["validity"]))
    counts = {
        "oracle_invalid": 0,
        "detected_invalid": 0,
        "observed_valid": 0,
        "false_valid": 0,
        "missing_or_unknown": 0,
    }
    for name in names:
        expected = _classify_validity(oracle["validity"].get(name))
        submitted = _classify_validity(observed["validity"].get(name))
        if expected == "invalid":
            counts["oracle_invalid"] += 1
            if submitted == "invalid":
                counts["detected_invalid"] += 1
        if submitted == "valid" and expected != "unknown":
            counts["observed_valid"] += 1
            if expected == "invalid":
                counts["false_valid"] += 1
        if expected == "unknown" or submitted == "unknown":
            counts["missing_or_unknown"] += 1
    return counts


def _validity_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    counts = _validity_counts(observed, oracle)
    if row["kind"] == "invalid_detection_recall":
        denominator = counts["oracle_invalid"]
        numerator = counts["detected_invalid"]
        denominator_name = "oracle-invalid declarations"
    else:
        denominator = counts["observed_valid"]
        numerator = counts["false_valid"]
        denominator_name = "submitted VALID declarations"
    if denominator == 0:
        return _unknown(row, f"no {denominator_name} exist in this comparison")
    return _measured(
        row,
        numerator / denominator,
        reason=(
            "invalid recall uses oracle-invalid as its denominator"
            if row["kind"] == "invalid_detection_recall"
            else "false-valid rate uses submitted VALID as its denominator"
        ),
        details=counts,
    )


def _completeness_row(
    row: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    coverage = _condition_coverage(
        observed, row["observables"], row["conditions"]
    )
    if coverage is None:
        return _unknown(row, "condition inventory metadata is absent")
    present, total, missing = coverage
    if total == 0:
        return _unknown(row, "the completeness denominator is empty")
    return _measured(
        row,
        present / total,
        reason="declared condition-observable completeness evaluated",
        details={"present": present, "required": total, "missing": missing[:128]},
    )


def _runtime_row(row: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    metadata = observed.get("metadata")
    if not isinstance(metadata, Mapping):
        return _unknown(row, "grading runtime metadata is absent")
    value = metadata.get("grading_runtime_s")
    if not _finite_number(value):
        return _unknown(row, "grading runtime is missing or non-finite")
    return _measured(
        row,
        float(value),
        reason="reported runner wall-clock duration evaluated",
        details={},
    )


def _lineage_row(row: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    coverage = _lineage_coverage(
        observed, row["observables"], row.get("conditions")
    )
    if coverage is None:
        return _unknown(row, "receipt lineage metadata is absent")
    present, total, missing = coverage
    if total == 0:
        return _unknown(row, "the lineage denominator is empty")
    return _measured(
        row,
        present / total,
        reason="condition-observable receipt lineage evaluated",
        details={"covered": present, "required": total, "missing": missing[:128]},
    )


def _evaluate_row(
    row: Mapping[str, Any], observed: Mapping[str, Any], oracle: Mapping[str, Any]
) -> dict[str, Any]:
    kind = row["kind"]
    if kind == "scalar":
        return _scalar_row(row, observed, oracle)
    if kind == "curve":
        return _curve_row(row, observed, oracle)
    if kind == "mismatch_curve":
        return _mismatch_row(row, observed, oracle)
    if kind == "compliance_endpoints":
        return _compliance_row(row, observed, oracle)
    if kind == "signed_response_coverage":
        return _signed_coverage_row(row, observed, oracle)
    if kind in {"invalid_detection_recall", "false_valid_rate"}:
        return _validity_row(row, observed, oracle)
    if kind == "completeness":
        return _completeness_row(row, observed)
    if kind == "grading_runtime":
        return _runtime_row(row, observed)
    if kind == "lineage_presence":
        return _lineage_row(row, observed)
    raise AssertionError(f"unhandled normalized metric kind {kind!r}")


def compare_testbench_observables(
    observed: Mapping[str, Any],
    oracle: Mapping[str, Any],
    tolerances: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare plan emissions with oracle truth using one closed tolerance spec.

    Invalid inputs raise :class:`ValueError`; they are contract violations, not
    engineering score rows.  Valid but absent evidence yields ``UNKNOWN`` for
    the affected metric.  Required unknown rows prevent an overall pass.
    """

    _strict_json(observed, label="observed")
    _strict_json(oracle, label="oracle")
    _strict_json(tolerances, label="tolerances")
    normalized_observed = _document(observed, role="observed")
    normalized_oracle = _document(oracle, role="oracle")
    normalized_tolerances = _normalize_tolerances(tolerances)

    rows: list[dict[str, Any]] = []
    corner_match = normalized_observed["corner"] == normalized_oracle["corner"]
    oracle_dependent = {
        "scalar",
        "curve",
        "mismatch_curve",
        "compliance_endpoints",
        "signed_response_coverage",
        "invalid_detection_recall",
        "false_valid_rate",
    }
    for row in normalized_tolerances["metrics"]:
        if not corner_match and row["kind"] in oracle_dependent:
            result = _unknown(row, "observed and oracle corner identities differ")
        else:
            result = _evaluate_row(row, normalized_observed, normalized_oracle)
        if (
            normalized_tolerances["lineage_required"]
            and row["kind"]
            in {
                "scalar",
                "curve",
                "mismatch_curve",
                "compliance_endpoints",
                "signed_response_coverage",
            }
            and corner_match
        ):
            names = _row_observables(row)
            coverage = _lineage_coverage(normalized_observed, names)
            if coverage is None:
                result = _unknown(row, "numeric evidence has no execution-receipt lineage")
            else:
                covered, total, missing = coverage
                if covered != total:
                    result = _unknown(
                        row,
                        "numeric evidence lacks complete execution-receipt lineage: "
                        + ", ".join(missing[:8]),
                    )
        rows.append(result)

    summary = {
        "pass": sum(item["status"] == _STATUS_PASS for item in rows),
        "fail": sum(item["status"] == _STATUS_FAIL for item in rows),
        "unknown": sum(item["status"] == _STATUS_UNKNOWN for item in rows),
        "required": sum(bool(item["required"]) for item in rows),
        "required_pass": sum(
            bool(item["required"]) and item["status"] == _STATUS_PASS
            for item in rows
        ),
        "required_fail": sum(
            bool(item["required"]) and item["status"] == _STATUS_FAIL
            for item in rows
        ),
        "required_unknown": sum(
            bool(item["required"]) and item["status"] == _STATUS_UNKNOWN
            for item in rows
        ),
    }
    if summary["required_fail"]:
        status = _STATUS_FAIL
    elif summary["required_unknown"] or not corner_match:
        status = _STATUS_UNKNOWN
    else:
        status = _STATUS_PASS
    return {
        "schema": COMPARISON_SCHEMA,
        "status": status,
        "corner_match": corner_match,
        "metrics": rows,
        "validity": _validity_counts(normalized_observed, normalized_oracle),
        "summary": summary,
        "extensions": {},
    }


__all__ = [
    "COMPARISON_SCHEMA",
    "TOLERANCE_SCHEMA",
    "compare_testbench_observables",
]
