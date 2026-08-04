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
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
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
    _SLUG_RE,
    _ScalarOutOfRange,
    EXPERIMENT_SCHEMA,
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

    def _resolve(
        self, form: str, name: object, parts: tuple[object, ...], sibling_unit: str | None
    ) -> object:
        path = _pointer(parts)
        if not isinstance(name, str) or name not in self.bindings:
            self.add(
                "template.ref.unknown",
                path,
                f"{form} target {name!r} is not a declared constant or parameter",
            )
            return None
        binding = self.bindings[name]
        self.used.add(name)
        if sibling_unit is not None and binding.unit != sibling_unit:
            self.add(
                "template.ref.unit_mismatch",
                path,
                f"{name!r} is declared in unit {binding.unit!r} but is substituted "
                f"beside unit {sibling_unit!r}; no conversion is performed",
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

    def substitute(
        self,
        node: object,
        parts: tuple[object, ...] = (),
        *,
        sibling_unit: str | None = None,
    ) -> object:
        if isinstance(node, Mapping):
            keys = set(node)
            if len(keys) == 1 and next(iter(keys)) in _REF_FORMS:
                form = next(iter(keys))
                return self._resolve(form, node[form], parts, sibling_unit)
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
                child_unit = None
                if key == "value" and isinstance(node.get("unit"), str):
                    child_unit = node["unit"]
                out[key] = self.substitute(
                    child, (*parts, key), sibling_unit=child_unit
                )
            return out
        if isinstance(node, list):
            return [
                self.substitute(child, (*parts, index))
                for index, child in enumerate(node)
            ]
        return node

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
            substituted = self.substitute(
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
    compiled_experiment = validator.substitute(experiment_body, ("experiment",))

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
    # output phase: nothing above touched the filesystem

    created_dir = False
    if out_dir.exists():
        if not out_dir.is_dir() or any(out_dir.iterdir()):
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
    else:
        try:
            out_dir.mkdir(parents=True)
            created_dir = True
        except OSError as exc:
            return _refusal_payload(
                [
                    ExperimentIssue(
                        "template.output.invalid",
                        "",
                        f"could not create {out_dir}: {exc}",
                    )
                ],
                "OpenADA could not create the compile output directory.",
            )

    def _cleanup() -> None:
        if created_dir:
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            for child in ("experiment.spec.json",):
                try:
                    (out_dir / child).unlink()
                except OSError:
                    pass
            shutil.rmtree(out_dir / "specifications", ignore_errors=True)

    spec_path = out_dir / "experiment.spec.json"
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
            record = _write_json(out_dir / rel, spec, role="template.specification")
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
            out_dir / "compile-receipt.json", receipt, role="template.receipt"
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
