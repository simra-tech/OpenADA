"""Compile one closed simra.experiment-template/v1 into runnable typed documents.

A template is a parameterized experiment together with template-relative
specification limits. The compiler resolves a closed parameter overlay,
substitutes declared references, validates the emitted simra.experiment/v1
document with the same validator ``openada experiment run`` uses, computes
absolute unit-bearing specification.evaluate requests from template constants
and parameters, and retains a deterministic compile receipt. Limits are
expressed relative to declared template quantities at authoring time and
become absolute numbers only at compile time — never anchored to one run's
measured extremum.

The template vocabulary is closed. Substitution sites are the two-form
objects ``{"$ref": name}`` (replaced by the declared SPICE scalar token, for
string positions such as element parameters) and ``{"$number": name}``
(replaced by the resolved finite JSON number, for numeric positions such as
measurement request quantities). Any other ``$``-prefixed key is refused.
Specification limit bounds are either finite JSON numbers or the closed
linear form ``{"ref": name, "factor": scalar, "offset": scalar}`` evaluated
exactly in decimal arithmetic; the referenced declaration's unit must equal
the bound's declared unit, and no unit conversion is ever performed.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any

from ..contract import (
    FileRecordError,
    result,
    static_execution,
)
from .experiment import (
    ExperimentIssue,
    _canonical_sha256,
    _decode_json,
    _json_bytes,
    _parse_scalar,
    _read_spec_bytes,
    _sha256_bytes,
    _write_json,
    _INDEPENDENT_SOURCE_KINDS,
    _SLUG_RE,
    _ScalarOutOfRange,
    _VOLTAGE_SOURCE_KINDS,
    EXPERIMENT_SCHEMA,
    MAX_SCALAR_SIGNIFICANT_DIGITS,
)
from .specification_evaluate import (
    _InvalidRequest as _SpecificationInvalid,
    _normalize_specification,
)


TEMPLATE_SCHEMA = "simra.experiment-template/v1"
RECEIPT_SCHEMA = "simra.experiment-template-compile/v1"
COMPILER_ID = "openada.experiment.template-compiler/v2"
OPERATION_NAME = "experiment.compile"

MAX_DECLARATIONS = 64
MAX_SPECIFICATIONS = 128
MAX_CONDITIONS = 64
MAX_DIAGNOSTICS = 32

_REF_FORMS = ("$ref", "$number")


@dataclass(frozen=True, slots=True)
class _Binding:
    """One resolved template quantity: a constant or an overlaid parameter."""

    token: str
    value: Decimal
    unit: str
    origin: str  # "constant" | "default" | "override"


def _pointer(parts: tuple[object, ...]) -> str:
    out = []
    for part in parts:
        text = str(part).replace("~", "~0").replace("/", "~1")
        out.append(text)
    return "".join(f"/{part}" for part in out)


def _recode(issue: ExperimentIssue) -> ExperimentIssue:
    code = issue.code
    if code.startswith("experiment."):
        code = "template." + code[len("experiment."):]
    return ExperimentIssue(code, issue.path, issue.message, issue.cause_code)


class _TemplateValidator:
    def __init__(self, document: object) -> None:
        self.document = document
        self.issues: list[ExperimentIssue] = []
        self.bindings: dict[str, _Binding] = {}
        self.used: set[str] = set()

    def add(
        self,
        code: str,
        path: str,
        message: str,
        *,
        cause_code: str | None = None,
    ) -> None:
        self.issues.append(ExperimentIssue(code, path, message, cause_code))

    def closed(
        self,
        value: object,
        path: str,
        *,
        required: set[str],
        optional: set[str] = frozenset(),
        code: str = "template.document.invalid",
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.add(code, path, "must be a JSON object")
            return None
        keys = set(value)
        for name in sorted(required - keys):
            self.add(code, f"{path}/{name}", f"missing required field {name!r}")
        for name in sorted(keys - required - optional):
            self.add(
                "template.document.unknown_field",
                f"{path}/{name}",
                f"field {name!r} is not declared by {TEMPLATE_SCHEMA}",
            )
        return value

    def slug(self, value: object, path: str, *, code: str) -> str | None:
        if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
            self.add(code, path, "must match ^[a-z][a-z0-9_]{0,63}$")
            return None
        return value

    def unit(self, value: object, path: str, *, code: str) -> str | None:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            self.add(code, path, "must be a unit string of 1 to 64 characters")
            return None
        return value

    def scalar(self, value: object, path: str, *, code: str) -> Any | None:
        try:
            return _parse_scalar(value)
        except _ScalarOutOfRange as exc:
            self.add(code, path, str(exc))
            return None
        except (ValueError, DecimalException, OverflowError):
            self.add(
                code,
                path,
                "must be one strict finite SPICE numeric scalar",
            )
            return None

    # ------------------------------------------------------------------
    # declarations

    def _declarations(self, root: Mapping[str, Any]) -> None:
        constants = root.get("constants")
        if not isinstance(constants, Mapping):
            self.add("template.document.invalid", "/constants", "must be a JSON object")
            constants = {}
        if len(constants) > MAX_DECLARATIONS:
            self.add(
                "template.constant.invalid",
                "/constants",
                f"declares more than {MAX_DECLARATIONS} constants",
            )
            constants = {}
        for name, body in constants.items():
            path = f"/constants/{name}"
            if self.slug(name, path, code="template.constant.name_invalid") is None:
                continue
            item = self.closed(
                body,
                path,
                required={"value", "unit"},
                optional={"description"},
                code="template.constant.invalid",
            )
            if item is None:
                continue
            scalar = self.scalar(
                item.get("value"), f"{path}/value", code="template.constant.invalid"
            )
            unit = self.unit(
                item.get("unit"), f"{path}/unit", code="template.constant.unit_invalid"
            )
            if scalar is None or unit is None:
                continue
            self.bindings[name] = _Binding(
                token=scalar.token, value=scalar.value, unit=unit, origin="constant"
            )

        parameters = root.get("parameters")
        if not isinstance(parameters, Mapping):
            self.add("template.document.invalid", "/parameters", "must be a JSON object")
            parameters = {}
        if len(parameters) > MAX_DECLARATIONS:
            self.add(
                "template.parameter.invalid",
                "/parameters",
                f"declares more than {MAX_DECLARATIONS} parameters",
            )
            parameters = {}
        self.parameter_specs: dict[str, Mapping[str, Any]] = {}
        for name, body in parameters.items():
            path = f"/parameters/{name}"
            if self.slug(name, path, code="template.parameter.name_invalid") is None:
                continue
            if name in self.bindings:
                self.add(
                    "template.declaration.duplicate",
                    path,
                    f"{name!r} is declared as both a constant and a parameter",
                )
                continue
            item = self.closed(
                body,
                path,
                required={"unit"},
                optional={"minimum", "maximum", "default", "description"},
                code="template.parameter.invalid",
            )
            if item is None:
                continue
            unit = self.unit(
                item.get("unit"), f"{path}/unit", code="template.parameter.unit_invalid"
            )
            if unit is None:
                continue
            bounds: dict[str, Any] = {}
            broken = False
            for field in ("minimum", "maximum", "default"):
                if field not in item:
                    continue
                scalar = self.scalar(
                    item[field], f"{path}/{field}", code="template.parameter.invalid"
                )
                if scalar is None:
                    broken = True
                    continue
                bounds[field] = scalar
            if broken:
                continue
            low = bounds.get("minimum")
            high = bounds.get("maximum")
            if low is not None and high is not None and low.value > high.value:
                self.add(
                    "template.parameter.invalid",
                    path,
                    "minimum exceeds maximum",
                )
                continue
            default = bounds.get("default")
            if default is not None and not self._within(default.value, low, high):
                self.add(
                    "template.parameter.out_of_range",
                    f"{path}/default",
                    "default is outside the declared inclusive range",
                )
                continue
            self.parameter_specs[name] = item

    @staticmethod
    def _within(value: Decimal, low: Any, high: Any) -> bool:
        if low is not None and value < low.value:
            return False
        if high is not None and value > high.value:
            return False
        return True

    def _overlay(self, overrides: Sequence[tuple[str, str]]) -> None:
        seen: set[str] = set()
        provided: dict[str, Any] = {}
        for name, token in overrides:
            path = f"/parameters/{name}"
            if name in seen:
                self.add(
                    "template.parameter.duplicate",
                    path,
                    f"parameter {name!r} is set more than once",
                )
                continue
            seen.add(name)
            if name not in self.parameter_specs:
                self.add(
                    "template.parameter.unknown",
                    path,
                    f"parameter {name!r} is not declared by the template",
                )
                continue
            scalar = self.scalar(token, path, code="template.parameter.invalid")
            if scalar is None:
                continue
            provided[name] = scalar
        for name, item in self.parameter_specs.items():
            path = f"/parameters/{name}"
            unit = item["unit"]
            scalar = provided.get(name)
            origin = "override"
            if scalar is None:
                if "default" not in item:
                    self.add(
                        "template.parameter.unbound",
                        path,
                        f"parameter {name!r} has no default and was not set",
                    )
                    continue
                scalar = _parse_scalar(item["default"])
                origin = "default"
            low = _parse_scalar(item["minimum"]) if "minimum" in item else None
            high = _parse_scalar(item["maximum"]) if "maximum" in item else None
            if not self._within(scalar.value, low, high):
                self.add(
                    "template.parameter.out_of_range",
                    path,
                    f"parameter {name!r} value {scalar.token!r} is outside the "
                    "declared inclusive range",
                )
                continue
            self.bindings[name] = _Binding(
                token=scalar.token, value=scalar.value, unit=unit, origin=origin
            )

    # ------------------------------------------------------------------
    # substitution
    #
    # Substitution is site-addressed and fail-closed: a `$ref`/`$number`
    # object is honored only at a site the compiler can type — an element
    # parameter with a known unit, an analysis scalar, temperature_c, or
    # the `value` member of a unit-bearing quantity/condition. Anywhere
    # else it is a refusal, never a silent pass-through.

    def _resolve(
        self,
        form: str,
        name: object,
        parts: tuple[object, ...],
        *,
        allowed_forms: frozenset[str],
        expected_unit: str | None,
    ) -> object:
        path = _pointer(parts)
        if form not in allowed_forms:
            self.add(
                "template.ref.site_invalid",
                path,
                f"{form} is not a valid substitution form at this site; "
                f"allowed here: {', '.join(sorted(allowed_forms)) or 'none'}",
            )
            return None
        if not isinstance(name, str) or name not in self.bindings:
            self.add(
                "template.ref.unknown",
                path,
                f"{form} target {name!r} is not a declared constant or parameter",
            )
            return None
        binding = self.bindings[name]
        self.used.add(name)
        if expected_unit is not None and binding.unit != expected_unit:
            self.add(
                "template.ref.unit_mismatch",
                path,
                f"{name!r} is declared in unit {binding.unit!r} but this site "
                f"requires unit {expected_unit!r}; no conversion is performed",
            )
            return None
        if form == "$ref":
            return binding.token
        value = float(binding.value)
        if not math.isfinite(value):
            self.add(
                "template.ref.non_finite",
                path,
                f"{name!r} does not resolve to a finite JSON number",
            )
            return None
        return value

    @staticmethod
    def _substitution_form(node: object) -> str | None:
        if isinstance(node, Mapping) and len(node) == 1:
            key = next(iter(node))
            if key in _REF_FORMS:
                return key
        return None

    def _copy(
        self,
        node: object,
        parts: tuple[object, ...],
        site: Any,
    ) -> object:
        """Recursively copy ``node``, consulting ``site(parts)`` at each
        substitution form. ``site`` returns ``(allowed_forms, expected_unit)``
        or ``None`` when substitution is forbidden at that position."""

        form = self._substitution_form(node)
        if form is not None:
            policy = site(parts)
            if policy is None:
                self.add(
                    "template.ref.site_invalid",
                    _pointer(parts),
                    "substitution is not declared at this site",
                )
                return None
            allowed_forms, expected_unit = policy
            return self._resolve(
                form,
                node[form],  # type: ignore[index]
                parts,
                allowed_forms=allowed_forms,
                expected_unit=expected_unit,
            )
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, child in node.items():
                if isinstance(key, str) and key.startswith("$"):
                    self.add(
                        "template.ref.invalid",
                        _pointer((*parts, key)),
                        f"{key!r} is not a substitution form; only "
                        f"{{'$ref': name}} and {{'$number': name}} objects are recognized",
                    )
                    continue
                out[key] = self._copy(child, (*parts, key), site)
            return out
        if isinstance(node, list):
            return [
                self._copy(child, (*parts, index), site)
                for index, child in enumerate(node)
            ]
        return node

    @staticmethod
    def _element_parameter_unit(kind: object, name: object) -> str | None:
        source_unit = (
            "V"
            if kind in _VOLTAGE_SOURCE_KINDS
            else "A"
            if kind in _INDEPENDENT_SOURCE_KINDS
            else None
        )
        table: dict[object, str | None] = {
            "dc": source_unit,
            "initial_value": source_unit,
            "pulsed_value": source_unit,
            "amplitude": source_unit,
            "ac_mag": source_unit,
            "ac_phase": "deg",
            "delay_time": "s",
            "rise_time": "s",
            "fall_time": "s",
            "pulse_width": "s",
            "period": "s",
            "delay": "s",
            "freq": "Hz",
            "damping": "1/s",
        }
        if kind == "resistor":
            table = {"r": "Ohm"}
        elif kind == "capacitor":
            table = {"c": "F", "ic": "V"}
        elif kind == "inductor":
            table = {"l": "H"}
        return table.get(name)

    def substitute_experiment(self, body: Mapping[str, Any]) -> object:
        elements = body.get("elements")
        element_kinds: dict[str, object] = {}
        if isinstance(elements, list):
            for element in elements:
                if isinstance(element, Mapping) and isinstance(
                    element.get("name"), str
                ):
                    element_kinds[element["name"]] = element.get("kind")

        def _analysis_scalar_unit(analysis: Mapping[str, Any], field: object) -> str | None:
            kind = analysis.get("kind")
            if kind == "ac" and field in {"start", "stop"}:
                return "Hz"
            if kind == "tran" and field in {"start", "step", "stop", "max_step"}:
                return "s"
            if kind == "dc" and field in {"start", "stop", "step"}:
                source_kind = element_kinds.get(analysis.get("source"))
                if source_kind in _VOLTAGE_SOURCE_KINDS:
                    return "V"
                if source_kind in _INDEPENDENT_SOURCE_KINDS:
                    return "A"
            return None

        both = frozenset(_REF_FORMS)
        ref_only = frozenset({"$ref"})
        number_only = frozenset({"$number"})

        def site(absolute: tuple[object, ...]):
            # Positions are reported below /experiment; classify relative
            # to the experiment body root.
            parts = absolute[1:]
            if (
                len(parts) == 4
                and parts[0] == "elements"
                and parts[2] == "parameters"
            ):
                element = elements[parts[1]] if isinstance(elements, list) else None
                kind = element.get("kind") if isinstance(element, Mapping) else None
                unit = self._element_parameter_unit(kind, parts[3])
                if unit is None:
                    return None
                return ref_only, unit
            if len(parts) == 3 and parts[0] == "analyses":
                analyses = body.get("analyses")
                analysis = (
                    analyses[parts[1]]
                    if isinstance(analyses, list) and isinstance(parts[1], int)
                    else None
                )
                if not isinstance(analysis, Mapping):
                    return None
                unit = _analysis_scalar_unit(analysis, parts[2])
                if unit is None:
                    return None
                return both, unit
            if parts == ("conditions", "pdk", "temperature_c"):
                return both, "degC"
            if (
                len(parts) >= 3
                and parts[0] == "measurements"
                and parts[-1] == "value"
            ):
                container = body
                for part in parts[:-1]:
                    if isinstance(container, Mapping):
                        container = container.get(part)
                    elif isinstance(container, list) and isinstance(part, int):
                        container = container[part]
                    else:
                        return None
                if isinstance(container, Mapping) and isinstance(
                    container.get("unit"), str
                ):
                    return number_only, container["unit"]
                return None
            return None

        return self._copy(body, ("experiment",), site)

    def substitute_condition_list(
        self, conditions: object, base: tuple[object, ...]
    ) -> object:
        def site(parts: tuple[object, ...]):
            if len(parts) == len(base) + 2 and parts[-1] == "value":
                index = parts[len(base)]
                if isinstance(conditions, list) and isinstance(index, int):
                    entry = conditions[index]
                    if isinstance(entry, Mapping) and isinstance(
                        entry.get("unit"), str
                    ):
                        return frozenset({"$number"}), entry["unit"]
            return None

        # The walk below re-derives paths from the root so the site callback
        # sees absolute positions; wrap the list at its base path.
        return self._copy_with_base(conditions, base, site)

    def _copy_with_base(self, node: object, base: tuple[object, ...], site: Any) -> object:
        return self._copy(node, base, site)

    # ------------------------------------------------------------------
    # specifications

    def _limit_value(self, raw: object, unit: str, path: str) -> float | None:
        if isinstance(raw, bool):
            self.add("template.limit.invalid", path, "must be a number or a ref object")
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
            if not math.isfinite(value):
                self.add("template.limit.non_finite", path, "must be finite")
                return None
            return value
        if not isinstance(raw, Mapping):
            self.add(
                "template.limit.invalid",
                path,
                "must be a finite number or {'ref': name, 'factor'?, 'offset'?}",
            )
            return None
        item = self.closed(
            raw,
            path,
            required={"ref"},
            optional={"factor", "offset"},
            code="template.limit.invalid",
        )
        if item is None:
            return None
        name = item["ref"]
        if not isinstance(name, str) or name not in self.bindings:
            self.add(
                "template.ref.unknown",
                f"{path}/ref",
                f"limit ref {name!r} is not a declared constant or parameter",
            )
            return None
        binding = self.bindings[name]
        self.used.add(name)
        if binding.unit != unit:
            self.add(
                "template.limit.unit_mismatch",
                f"{path}/ref",
                f"{name!r} is declared in unit {binding.unit!r} but the bound "
                f"declares unit {unit!r}; no conversion is performed",
            )
            return None
        factor = Decimal(1)
        offset = Decimal(0)
        try:
            if "factor" in item:
                factor = _parse_scalar(item["factor"]).value
            if "offset" in item:
                offset = _parse_scalar(item["offset"]).value
            # Exact for the whole admitted scalar domain; the default
            # 28-digit context would silently round long-digit operands.
            with localcontext() as context:
                context.prec = 2 * MAX_SCALAR_SIGNIFICANT_DIGITS + 32
                computed = factor * binding.value + offset
            value = float(computed)
        except (
            ValueError,
            DecimalException,
            OverflowError,
            _ScalarOutOfRange,
        ) as exc:
            self.add(
                "template.limit.invalid",
                path,
                f"the limit expression could not be evaluated: {exc}",
            )
            return None
        if not math.isfinite(value):
            self.add("template.limit.non_finite", path, "the computed limit is not finite")
            return None
        return value

    def compile_specifications(
        self, root: Mapping[str, Any], measurement_ids: set[str]
    ) -> list[dict[str, Any]] | None:
        raw = root.get("specifications")
        if not isinstance(raw, list) or len(raw) > MAX_SPECIFICATIONS:
            self.add(
                "template.specification.invalid",
                "/specifications",
                f"must be an array of at most {MAX_SPECIFICATIONS} entries",
            )
            return None
        compiled: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(raw):
            path = f"/specifications/{index}"
            item = self.closed(
                entry,
                path,
                required={"specification_id", "measurement_id", "limits"},
                optional={"conditions", "description"},
                code="template.specification.invalid",
            )
            if item is None:
                continue
            sid = item.get("specification_id")
            mid = item.get("measurement_id")
            if not isinstance(sid, str) or not isinstance(mid, str):
                self.add(
                    "template.specification.invalid",
                    path,
                    "specification_id and measurement_id must be strings",
                )
                continue
            if sid in seen_ids:
                self.add(
                    "template.specification.duplicate",
                    f"{path}/specification_id",
                    f"specification_id {sid!r} is declared more than once",
                )
                continue
            seen_ids.add(sid)
            if mid not in measurement_ids:
                self.add(
                    "template.specification.measurement_unknown",
                    f"{path}/measurement_id",
                    f"measurement_id {mid!r} is not declared by the compiled "
                    "experiment's measurements",
                )
                continue
            limits_raw = self.closed(
                item.get("limits"),
                f"{path}/limits",
                required=set(),
                optional={"lower", "upper"},
                code="template.limit.invalid",
            )
            if limits_raw is None:
                continue
            if not limits_raw:
                self.add(
                    "template.limit.invalid",
                    f"{path}/limits",
                    "must declare at least one lower or upper bound",
                )
                continue
            limits: dict[str, Any] = {}
            broken = False
            for bound_name in ("lower", "upper"):
                if bound_name not in limits_raw:
                    continue
                bound_path = f"{path}/limits/{bound_name}"
                bound = self.closed(
                    limits_raw[bound_name],
                    bound_path,
                    required={"value", "unit", "inclusive"},
                    code="template.limit.invalid",
                )
                if bound is None:
                    broken = True
                    continue
                unit = self.unit(
                    bound.get("unit"),
                    f"{bound_path}/unit",
                    code="template.limit.invalid",
                )
                if unit is None or not isinstance(bound.get("inclusive"), bool):
                    if unit is not None:
                        self.add(
                            "template.limit.invalid",
                            f"{bound_path}/inclusive",
                            "must be boolean",
                        )
                    broken = True
                    continue
                value = self._limit_value(
                    bound.get("value"), unit, f"{bound_path}/value"
                )
                if value is None:
                    broken = True
                    continue
                limits[bound_name] = {
                    "value": value,
                    "unit": unit,
                    "inclusive": bound["inclusive"],
                }
            if broken:
                continue
            if set(limits) == {"lower", "upper"}:
                lower, upper = limits["lower"], limits["upper"]
                if lower["unit"] != upper["unit"]:
                    self.add(
                        "template.limit.unit_mismatch",
                        f"{path}/limits",
                        "lower and upper bounds must declare the same exact unit",
                    )
                    continue
                if lower["value"] > upper["value"] or (
                    lower["value"] == upper["value"]
                    and not (lower["inclusive"] and upper["inclusive"])
                ):
                    self.add(
                        "template.limit.empty_interval",
                        f"{path}/limits",
                        "the computed lower and upper limits form an empty interval",
                    )
                    continue
            conditions = item.get("conditions", [])
            if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
                self.add(
                    "template.specification.invalid",
                    f"{path}/conditions",
                    f"must be an array of at most {MAX_CONDITIONS} entries",
                )
                continue
            substituted = self.substitute_condition_list(
                conditions, ("specifications", index, "conditions")
            )
            document = {
                "specification_id": sid,
                "measurement_id": mid,
                "limits": limits,
                "conditions": substituted,
                "extensions": {},
            }
            try:
                _normalize_specification(document)
            except _SpecificationInvalid as exc:
                self.add(
                    "template.specification.invalid",
                    path,
                    f"the compiled specification is not accepted by "
                    f"specification.evaluate: {exc}",
                    cause_code=getattr(exc, "code", None),
                )
                continue
            compiled.append(document)
        return compiled


def _measurement_ids(document: object) -> set[str]:
    if not isinstance(document, Mapping):
        return set()
    measurements = document.get("measurements")
    if not isinstance(measurements, list):
        return set()
    return {
        entry["id"]
        for entry in measurements
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }


def _refusal_payload(
    issues: Sequence[ExperimentIssue], summary: str
) -> dict[str, Any]:
    return result(
        OPERATION_NAME,
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary=summary,
        diagnostics=[
            issue.envelope_diagnostic() for issue in issues[:MAX_DIAGNOSTICS]
        ],
        data={
            "schema": RECEIPT_SCHEMA,
            "refusals": [issue.record() for issue in issues],
            "receipt": None,
            "extensions": {},
        },
    )


def compile_experiment_template(
    template_path: str | Path,
    output_dir: str | Path,
    *,
    pdk: str,
    pdk_root: str | Path | None,
    overrides: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Compile one template with a parameter overlay into typed documents.

    On success the output directory retains ``experiment.spec.json`` (a fully
    validated simra.experiment/v1 document), one specification.evaluate
    request per declared specification under ``specifications/``, and a
    deterministic ``compile-receipt.json``. On refusal nothing is retained.
    """

    template_file = Path(template_path).expanduser().resolve()
    out_dir = Path(output_dir).expanduser().resolve()

    try:
        raw = _read_spec_bytes(template_file)
    except ValueError as exc:
        return _refusal_payload(
            [ExperimentIssue("template.document.invalid", "", str(exc))],
            "OpenADA could not read the experiment template.",
        )
    document, parse_issues = _decode_json(raw)
    issues = [_recode(issue) for issue in parse_issues]
    if document is None or issues:
        return _refusal_payload(
            issues
            or [
                ExperimentIssue(
                    "template.document.invalid", "", "the template is not valid JSON"
                )
            ],
            "OpenADA refused the experiment template.",
        )

    validator = _TemplateValidator(document)
    root = validator.closed(
        document,
        "",
        required={
            "schema",
            "id",
            "constants",
            "parameters",
            "experiment",
            "specifications",
        },
    )
    if root is None:
        return _refusal_payload(
            validator.issues, "OpenADA refused the experiment template."
        )
    if root.get("schema") != TEMPLATE_SCHEMA:
        validator.add(
            "template.schema.unsupported",
            "/schema",
            f"must be exactly {TEMPLATE_SCHEMA!r}",
        )
    template_id = validator.slug(root.get("id"), "/id", code="template.id.invalid")

    validator._declarations(root)
    validator._overlay(list(overrides))
    if validator.issues:
        return _refusal_payload(
            validator.issues, "OpenADA refused the experiment template."
        )

    experiment_body = root.get("experiment")
    if not isinstance(experiment_body, Mapping):
        validator.add("template.document.invalid", "/experiment", "must be a JSON object")
        return _refusal_payload(
            validator.issues, "OpenADA refused the experiment template."
        )
    if experiment_body.get("schema") != EXPERIMENT_SCHEMA:
        validator.add(
            "template.experiment.invalid",
            "/experiment/schema",
            f"must be exactly {EXPERIMENT_SCHEMA!r}",
        )
        return _refusal_payload(
            validator.issues, "OpenADA refused the experiment template."
        )
    compiled_experiment = validator.substitute_experiment(experiment_body)

    specifications = validator.compile_specifications(
        root, _measurement_ids(compiled_experiment)
    )
    unused = sorted(set(validator.bindings) - validator.used)
    for name in unused:
        origin = validator.bindings[name].origin
        section = "constants" if origin == "constant" else "parameters"
        validator.add(
            "template.declaration.unused",
            f"/{section}/{name}",
            f"{name!r} is declared but never referenced",
        )
    if validator.issues or specifications is None:
        return _refusal_payload(
            validator.issues, "OpenADA refused the experiment template."
        )

    # ------------------------------------------------------------------
    # output phase: nothing above touched the filesystem. Everything below
    # is written into a fresh staging directory beside the target and
    # published with one atomic rename after full validation, so a refusal
    # or an interrupted write can never leave partial compile output at
    # the caller-visible path.

    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        return _refusal_payload(
            [
                ExperimentIssue(
                    "template.output.not_empty",
                    "",
                    f"{out_dir} must be an absent or empty directory",
                )
            ],
            "OpenADA refused to overwrite existing compile output.",
        )
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(prefix=f".{out_dir.name}.compile-", dir=out_dir.parent)
        )
    except OSError as exc:
        return _refusal_payload(
            [
                ExperimentIssue(
                    "template.output.invalid",
                    "",
                    f"could not create a staging directory beside {out_dir}: {exc}",
                )
            ],
            "OpenADA could not create the compile output directory.",
        )

    def _cleanup() -> None:
        shutil.rmtree(stage, ignore_errors=True)

    spec_path = stage / "experiment.spec.json"
    try:
        experiment_record = _write_json(
            spec_path, compiled_experiment, role="template.experiment"
        )
    except (FileRecordError, OSError) as exc:
        _cleanup()
        return _refusal_payload(
            [
                ExperimentIssue(
                    "template.output.invalid",
                    "",
                    f"could not retain the compiled experiment: {exc}",
                )
            ],
            "OpenADA could not retain the compiled experiment.",
        )

    prepared, experiment_issues = validate_experiment_path(
        spec_path, pdk=pdk, pdk_root=pdk_root
    )
    if prepared is None or experiment_issues:
        _cleanup()
        nested = [
            ExperimentIssue(
                "template.experiment.invalid",
                f"/experiment{issue.path}",
                issue.message,
                cause_code=issue.code,
            )
            for issue in experiment_issues
        ] or [
            ExperimentIssue(
                "template.experiment.invalid",
                "/experiment",
                "the compiled experiment did not validate",
            )
        ]
        return _refusal_payload(
            nested,
            "The compiled experiment is not a valid simra.experiment/v1 document.",
        )

    artifacts = [experiment_record]
    receipt_specifications = []
    for spec in specifications:
        rel = f"specifications/{spec['specification_id']}.json"
        try:
            record = _write_json(stage / rel, spec, role="template.specification")
        except (FileRecordError, OSError) as exc:
            _cleanup()
            return _refusal_payload(
                [
                    ExperimentIssue(
                        "template.output.invalid",
                        "",
                        f"could not retain {rel}: {exc}",
                    )
                ],
                "OpenADA could not retain a compiled specification.",
            )
        artifacts.append(record)
        receipt_specifications.append(
            {
                "specification_id": spec["specification_id"],
                "measurement_id": spec["measurement_id"],
                "path": rel,
                "raw_sha256": record["sha256"],
                "canonical_sha256": _canonical_sha256(spec),
            }
        )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "compiler": {"id": COMPILER_ID},
        "template": {
            "id": template_id,
            "raw_sha256": _sha256_bytes(raw),
            "canonical_sha256": _canonical_sha256(document),
        },
        "pdk": pdk,
        "parameters": {
            name: {
                "token": binding.token,
                "unit": binding.unit,
                "origin": binding.origin,
            }
            for name, binding in sorted(validator.bindings.items())
            if binding.origin != "constant"
        },
        "experiment": {
            "path": "experiment.spec.json",
            "id": compiled_experiment.get("id"),
            "raw_sha256": experiment_record["sha256"],
            "canonical_sha256": _canonical_sha256(compiled_experiment),
        },
        "specifications": receipt_specifications,
        "extensions": {},
    }
    try:
        receipt_record = _write_json(
            stage / "compile-receipt.json", receipt, role="template.receipt"
        )
    except (FileRecordError, OSError) as exc:
        _cleanup()
        return _refusal_payload(
            [
                ExperimentIssue(
                    "template.output.invalid",
                    "",
                    f"could not retain the compile receipt: {exc}",
                )
            ],
            "OpenADA could not retain the compile receipt.",
        )
    artifacts.append(receipt_record)

    try:
        if out_dir.exists():
            out_dir.rmdir()
        os.rename(stage, out_dir)
    except OSError as exc:
        _cleanup()
        return _refusal_payload(
            [
                ExperimentIssue(
                    "template.output.invalid",
                    "",
                    f"could not publish the staged compile output to {out_dir}: {exc}",
                )
            ],
            "OpenADA could not publish the compile output.",
        )
    for record in artifacts:
        record["path"] = str(out_dir / Path(record["path"]).relative_to(stage))

    return result(
        OPERATION_NAME,
        tool=None,
        execution=static_execution(),
        engineering_status="pass",
        summary=(
            f"Compiled template {template_id!r}: experiment "
            f"{compiled_experiment.get('id')!r} and "
            f"{len(receipt_specifications)} specification document(s)."
        ),
        artifacts=artifacts,
        data={
            "schema": RECEIPT_SCHEMA,
            "refusals": [],
            "receipt": receipt,
            "output_dir": str(out_dir),
            "extensions": {},
        },
    )


def validate_experiment_path(
    spec_path: Path, *, pdk: str, pdk_root: str | Path | None
):
    """Seam for tests; delegates to experiment.validate_experiment."""

    from .experiment import validate_experiment

    return validate_experiment(spec_path, pdk=pdk, pdk_root=pdk_root)


__all__ = [
    "TEMPLATE_SCHEMA",
    "RECEIPT_SCHEMA",
    "COMPILER_ID",
    "OPERATION_NAME",
    "compile_experiment_template",
]
