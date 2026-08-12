"""Deterministic ngspice compiler for closed testbench-plan artifacts.

The compiler deliberately accepts only the typed :mod:`testbench_plan` model.
In particular, DUT text is treated as a digest-pinned structural artifact: it
is checked, captured, and renamed into a compiler-owned namespace before it can
be placed in a generated deck.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from ..contract import FileRecordError, stable_regular_file
from .testbench_plan import PreparedTestbenchPlan


__all__ = [
    "CompiledCondition",
    "NgspiceCompilationBundle",
    "PreparedNgspiceCompilation",
    "ResolvedBindingValue",
    "SealedDut",
    "TESTBENCH_PLAN_COMPILE_RECEIPT_SCHEMA",
    "TESTBENCH_PLAN_NGSPICE_COMPILER_ID",
    "TestbenchPlanCompileError",
    "compile_testbench_plan_ngspice",
    "prepare_testbench_plan_ngspice",
    "seal_structural_dut",
]


TESTBENCH_PLAN_COMPILE_RECEIPT_SCHEMA = "simra.testbench-plan-compile/v1"
TESTBENCH_PLAN_NGSPICE_COMPILER_ID = "openada.testbench-plan.ngspice/v1"
MAX_DUT_BYTES = 4 * 1024 * 1024
MAX_DC_SAMPLES = 1_000_000

_SPICE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DEVICE_PREFIXES = frozenset({"c", "d", "j", "l", "m", "q", "r"})


class TestbenchPlanCompileError(ValueError):
    """Stable refusal raised before a compiler output becomes observable."""

    __test__ = False

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def record(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message[:4000]}


@dataclass(frozen=True, slots=True)
class SealedDut:
    """Captured and namespace-rewritten structural DUT."""

    raw_bytes: bytes
    raw_sha256: str
    canonical_bytes: bytes
    canonical_sha256: str
    original_top: str
    sealed_top: str


@dataclass(frozen=True, slots=True)
class CompiledCondition:
    stage_id: str
    point_id: str
    condition_id: str
    condition_sha256: str
    relative_deck_path: str
    deck_bytes: bytes
    deck_sha256: str
    expected_probes: tuple[Mapping[str, Any], ...]
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ResolvedBindingValue:
    """One closed, receipt-backed value produced by an upstream stage."""

    binding_id: str
    value: int | float
    unit: str
    source_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedNgspiceCompilation:
    plan_bytes: bytes
    sealed_dut: SealedDut
    conditions: tuple[CompiledCondition, ...]
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NgspiceCompilationBundle:
    output_dir: Path
    receipt: Mapping[str, Any]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fail(code: str, path: str, message: str) -> None:
    raise TestbenchPlanCompileError(code, path, message)


def _compiler_top(namespace: str, top: str) -> str:
    name = f"OPENADA_{namespace}_{top}"
    if not _SPICE_NAME_RE.fullmatch(name):
        _fail(
            "testbench_plan.compiler.namespace_invalid",
            "/dut/namespace",
            "the sealed namespace and top exceed the ngspice identifier boundary",
        )
    return name


def seal_structural_dut(
    raw_bytes: bytes,
    *,
    expected_sha256: str,
    namespace: str,
    top: str,
    ports: Sequence[str],
) -> SealedDut:
    """Verify and rename one strict structural subcircuit.

    This intentionally supports a small, auditable device-card allowlist.  A
    digest-pinned artifact containing sources, controlled/behavioral elements,
    directives, continuation lines, or another subcircuit is refused.
    """

    if not _SHA256_RE.fullmatch(expected_sha256):
        _fail(
            "testbench_plan.compiler.dut_digest_invalid",
            "/dut/sha256",
            "expected DUT digest must be lowercase SHA-256",
        )
    if not 1 <= len(raw_bytes) <= MAX_DUT_BYTES:
        _fail(
            "testbench_plan.compiler.dut_size_invalid",
            "/dut/artifact",
            f"DUT size must be within 1..{MAX_DUT_BYTES} bytes",
        )
    raw_sha256 = _sha256(raw_bytes)
    if raw_sha256 != expected_sha256:
        _fail(
            "testbench_plan.compiler.dut_digest_mismatch",
            "/dut/sha256",
            f"DUT SHA-256 is {raw_sha256}, expected {expected_sha256}",
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        _fail(
            "testbench_plan.compiler.dut_text_invalid",
            "/dut/artifact",
            f"DUT must be UTF-8 text: {exc}",
        )
    if "\x00" in text or "\r" in text:
        _fail(
            "testbench_plan.compiler.dut_text_invalid",
            "/dut/artifact",
            "DUT must use NUL-free LF-delimited UTF-8 text",
        )

    declared_ports = tuple(str(item) for item in ports)
    if not declared_ports or any(not _SPICE_NAME_RE.fullmatch(item) for item in declared_ports):
        _fail(
            "testbench_plan.compiler.dut_ports_invalid",
            "/dut/ports",
            "DUT ports must be non-empty ngspice identifiers",
        )
    if len({item.casefold() for item in declared_ports}) != len(declared_ports):
        _fail(
            "testbench_plan.compiler.dut_ports_invalid",
            "/dut/ports",
            "DUT ports collide case-insensitively",
        )

    sealed_top = _compiler_top(namespace, top)
    output: list[str] = []
    inside = False
    seen_subckt = False
    seen_ends = False
    instances: set[str] = set()
    for line_number, source_line in enumerate(text.split("\n"), start=1):
        line = source_line.strip()
        pointer = f"/dut/artifact/line/{line_number}"
        if not line:
            continue
        if line.startswith("*"):
            # Comments are retained only as normalized whole-line comments.
            output.append(line)
            continue
        if line.startswith("+"):
            _fail(
                "testbench_plan.compiler.dut_continuation_forbidden",
                pointer,
                "continuation lines are outside the closed DUT grammar",
            )
        if (
            ";" in line
            or "$" in line
            or "\\" in line
            or "{" in line
            or "}" in line
            or "\"" in line
            or "'" in line
        ):
            _fail(
                "testbench_plan.compiler.dut_token_forbidden",
                pointer,
                "inline comments, escaping, and expression tokens are forbidden",
            )
        tokens = line.split()
        head = tokens[0]
        folded = head.casefold()
        if folded == ".subckt":
            if inside or seen_subckt or seen_ends:
                _fail(
                    "testbench_plan.compiler.dut_shadowing",
                    pointer,
                    "the sealed DUT must contain exactly one non-nested subcircuit",
                )
            if len(tokens) < 3 or tokens[1].casefold() != top.casefold():
                _fail(
                    "testbench_plan.compiler.dut_top_mismatch",
                    pointer,
                    f"expected exactly one .SUBCKT named {top!r}",
                )
            actual_ports = tuple(tokens[2:])
            if actual_ports != declared_ports:
                _fail(
                    "testbench_plan.compiler.dut_ports_mismatch",
                    pointer,
                    f"subcircuit ports {actual_ports!r} do not match sealed ABI {declared_ports!r}",
                )
            inside = True
            seen_subckt = True
            output.append(".SUBCKT " + " ".join((sealed_top, *declared_ports)))
            continue
        if folded == ".ends":
            if not inside or seen_ends or len(tokens) not in {1, 2}:
                _fail(
                    "testbench_plan.compiler.dut_boundary_invalid",
                    pointer,
                    "unexpected or malformed .ENDS",
                )
            if len(tokens) == 2 and tokens[1].casefold() != top.casefold():
                _fail(
                    "testbench_plan.compiler.dut_top_mismatch",
                    pointer,
                    ".ENDS name does not match the sealed top",
                )
            output.append(f".ENDS {sealed_top}")
            inside = False
            seen_ends = True
            continue
        if head.startswith("."):
            _fail(
                "testbench_plan.compiler.dut_directive_forbidden",
                pointer,
                f"directive {head!r} is outside the structural DUT allowlist",
            )
        if not inside:
            _fail(
                "testbench_plan.compiler.dut_top_level_card",
                pointer,
                "device cards outside the sealed subcircuit are forbidden",
            )
        if not _SPICE_NAME_RE.fullmatch(head):
            _fail(
                "testbench_plan.compiler.dut_instance_invalid",
                pointer,
                f"invalid instance identifier {head!r}",
            )
        if head[0].casefold() not in _ALLOWED_DEVICE_PREFIXES:
            _fail(
                "testbench_plan.compiler.dut_element_forbidden",
                pointer,
                f"element {head!r} is outside the structural allowlist",
            )
        if head.casefold() in instances:
            _fail(
                "testbench_plan.compiler.dut_instance_collision",
                pointer,
                f"instance {head!r} collides case-insensitively",
            )
        instances.add(head.casefold())
        output.append(" ".join(tokens))

    if inside or not seen_subckt or not seen_ends:
        _fail(
            "testbench_plan.compiler.dut_boundary_invalid",
            "/dut/artifact",
            "the sealed DUT requires exactly one complete .SUBCKT/.ENDS pair",
        )
    canonical = ("\n".join(output) + "\n").encode("utf-8")
    return SealedDut(
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        canonical_bytes=canonical,
        canonical_sha256=_sha256(canonical),
        original_top=top,
        sealed_top=sealed_top,
    )


def _capture_dut(
    plan: PreparedTestbenchPlan,
    *,
    dut_artifact: str | Path | None,
    dut_sha256: str | None,
) -> SealedDut:
    path = Path(dut_artifact or plan.dut.artifact)
    expected = dut_sha256 or plan.dut.sha256
    try:
        with stable_regular_file(path) as (handle, opened):
            if not 1 <= opened.st_size <= MAX_DUT_BYTES:
                raise ValueError(f"DUT size must be within 1..{MAX_DUT_BYTES} bytes")
            raw = handle.read(MAX_DUT_BYTES + 1)
            if len(raw) != opened.st_size:
                raise ValueError("DUT changed while it was read")
    except (FileRecordError, OSError, ValueError) as exc:
        _fail(
            "testbench_plan.compiler.dut_unavailable",
            "/dut/artifact",
            str(exc),
        )
    return seal_structural_dut(
        raw,
        expected_sha256=expected,
        namespace=plan.dut.namespace,
        top=plan.dut.top,
        ports=tuple(str(port["name"]) for port in plan.dut.ports),
    )


def prepare_testbench_plan_ngspice(
    plan: PreparedTestbenchPlan,
    *,
    corner: str,
    dut_artifact: str | Path | None = None,
    dut_sha256: str | None = None,
    stage_ids: Sequence[str] | None = None,
    binding_values: Sequence[ResolvedBindingValue] = (),
) -> PreparedNgspiceCompilation:
    """Prepare deterministic deck bytes without mutating the filesystem."""

    if not isinstance(plan, PreparedTestbenchPlan):
        _fail(
            "testbench_plan.compiler.plan_unprepared",
            "",
            "compile input must be a validated PreparedTestbenchPlan",
        )
    canonical = _canonical_bytes(plan.document)
    if _sha256(canonical) != plan.canonical_sha256:
        _fail(
            "testbench_plan.compiler.plan_changed",
            "",
            "validated plan document changed after preparation",
        )
    if plan.raw_bytes is not None and _sha256(plan.raw_bytes) != plan.raw_sha256:
        _fail(
            "testbench_plan.compiler.plan_changed",
            "",
            "validated raw plan bytes changed after preparation",
        )
    sealed = _capture_dut(
        plan, dut_artifact=dut_artifact, dut_sha256=dut_sha256
    )
    # Conditions are filled by the typed deck serializer below.  Keeping this
    # stage pure makes output publication independently atomic and testable.
    selected_corner = _select_corner(plan, corner)
    resolved_bindings = _resolve_binding_values(plan, binding_values)
    conditions = _compile_conditions(
        plan,
        sealed,
        selected_corner,
        stage_ids=stage_ids,
        binding_values=resolved_bindings,
    )
    plan_bytes = plan.raw_bytes or _canonical_bytes(plan.document)
    receipt = _compile_receipt(plan, sealed, selected_corner, conditions)
    return PreparedNgspiceCompilation(plan_bytes, sealed, conditions, receipt)


def compile_testbench_plan_ngspice(
    plan: PreparedTestbenchPlan,
    output_dir: str | Path,
    *,
    corner: str,
    dut_artifact: str | Path | None = None,
    dut_sha256: str | None = None,
    stage_ids: Sequence[str] | None = None,
    binding_values: Sequence[ResolvedBindingValue] = (),
) -> NgspiceCompilationBundle:
    """Compile and atomically publish a timestamp-free ngspice bundle."""

    prepared = prepare_testbench_plan_ngspice(
        plan,
        corner=corner,
        dut_artifact=dut_artifact,
        dut_sha256=dut_sha256,
        stage_ids=stage_ids,
        binding_values=binding_values,
    )
    target = Path(output_dir)
    _publish_compilation(prepared, target)
    return NgspiceCompilationBundle(target, prepared.receipt)


# The remaining helpers are intentionally defined in this module (rather than
# accepting backend strings) so every emitted token has a typed origin.
def _select_corner(
    plan: PreparedTestbenchPlan, identifier: str
) -> Mapping[str, Any]:
    if not isinstance(identifier, str) or not identifier:
        _fail(
            "testbench_plan.compiler.corner_invalid",
            "/corner",
            "corner must be one declared corner identifier",
        )
    matches = [item for item in plan.corner_bindings if item["id"] == identifier]
    if len(matches) != 1:
        _fail(
            "testbench_plan.compiler.corner_unknown",
            "/corner",
            f"unknown corner {identifier!r}",
        )
    return matches[0]


def _decimal(value: object, *, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        _fail(
            "testbench_plan.compiler.number_invalid", path, "value must be numeric"
        )
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        _fail(
            "testbench_plan.compiler.number_invalid", path, "value is not decimal"
        )
    if not result.is_finite() or abs(result) > Decimal("1e300"):
        _fail(
            "testbench_plan.compiler.number_invalid",
            path,
            "value must be finite and bounded by 1e300",
        )
    return result


def _number_token(value: Decimal) -> str:
    if value == 0:
        return "0"
    value = value.normalize()
    adjusted = value.adjusted()
    if -3 <= adjusted <= 6:
        return format(value, "f")
    digits = "".join(str(item) for item in value.as_tuple().digits)
    sign = "-" if value.as_tuple().sign else ""
    mantissa = digits[0]
    if len(digits) > 1:
        mantissa += "." + digits[1:]
    return f"{sign}{mantissa}e{adjusted}"


def _resolved_quantity(
    source: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
    *,
    path: str,
) -> Mapping[str, str]:
    return {
        "value": _number_token(_resolve_decimal(source, inputs, path=path)),
        "unit": str(source["unit"]),
    }


def _resolve_decimal(
    source: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
    *,
    path: str,
) -> Decimal:
    if "value" in source:
        return _decimal(source["value"], path=f"{path}/value")
    input_id = str(source.get("input_id", ""))
    bound = inputs.get(input_id)
    if bound is None:
        _fail(
            "testbench_plan.compiler.binding_unresolved",
            path,
            f"stage input {input_id!r} has no receipt-backed binding value",
        )
    if bound["unit"] != source["unit"]:
        _fail(
            "testbench_plan.compiler.binding_unit_mismatch",
            path,
            f"input {input_id!r} has unit {bound['unit']!r}, expected {source['unit']!r}",
        )
    result = Decimal(str(bound["resolved_value"]))
    if "factor" in source:
        factor = _decimal(source["factor"], path=f"{path}/factor")
        offset = source["offset"]
        if offset["unit"] != source["unit"]:
            _fail(
                "testbench_plan.compiler.binding_unit_mismatch",
                f"{path}/offset/unit",
                "affine offset unit must match the resolved value unit",
            )
        result = result * factor + _decimal(
            offset["value"], path=f"{path}/offset/value"
        )
    if not result.is_finite() or abs(result) > Decimal("1e300"):
        _fail(
            "testbench_plan.compiler.number_invalid",
            path,
            "resolved affine value must be finite and bounded by 1e300",
        )
    return result


def _resolve_binding_values(
    plan: PreparedTestbenchPlan,
    values: Sequence[ResolvedBindingValue],
) -> Mapping[str, Mapping[str, Any]]:
    if isinstance(values, (str, bytes)):
        _fail(
            "testbench_plan.compiler.binding_values_invalid",
            "/binding_values",
            "binding_values must contain only ResolvedBindingValue records",
        )
    declared = {str(item["id"]): item for item in plan.document["bindings"]}
    output: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(values):
        path = f"/binding_values/{index}"
        if not isinstance(item, ResolvedBindingValue):
            _fail(
                "testbench_plan.compiler.binding_values_invalid",
                path,
                "binding value must be a ResolvedBindingValue record",
            )
        binding = declared.get(item.binding_id)
        if binding is None:
            _fail(
                "testbench_plan.compiler.binding_unknown",
                f"{path}/binding_id",
                f"unknown binding {item.binding_id!r}",
            )
        if item.binding_id in output:
            _fail(
                "testbench_plan.compiler.binding_duplicate",
                f"{path}/binding_id",
                f"duplicate binding {item.binding_id!r}",
            )
        if item.unit != binding["unit"]:
            _fail(
                "testbench_plan.compiler.binding_unit_mismatch",
                f"{path}/unit",
                f"binding requires unit {binding['unit']!r}",
            )
        if not _SHA256_RE.fullmatch(item.source_receipt_sha256):
            _fail(
                "testbench_plan.compiler.binding_receipt_invalid",
                f"{path}/source_receipt_sha256",
                "source receipt digest must be lowercase SHA-256",
            )
        source_value = _decimal(item.value, path=f"{path}/value")
        transform = binding["transform"]
        resolved = source_value
        if transform["kind"] == "scale":
            resolved *= _decimal(transform["factor"], path=f"/bindings/{item.binding_id}/transform/factor")
        if not resolved.is_finite() or abs(resolved) > Decimal("1e300"):
            _fail(
                "testbench_plan.compiler.number_invalid",
                f"{path}/value",
                "resolved binding value must be finite and bounded by 1e300",
            )
        output[item.binding_id] = {
            "binding_id": item.binding_id,
            "source_value": _number_token(source_value),
            "resolved_value": _number_token(resolved),
            "unit": item.unit,
            "source_receipt_sha256": item.source_receipt_sha256,
            "transform": dict(transform),
        }
    return output


def _stage_input_values(
    plan: PreparedTestbenchPlan,
    stage: Mapping[str, Any],
    *,
    binding_values: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    by_target: dict[str, Mapping[str, Any]] = {}
    for binding in plan.document["bindings"]:
        target = binding["to"]
        if target["stage_id"] != stage["id"]:
            continue
        provided = binding_values.get(binding["id"])
        if provided is not None:
            by_target[str(target["input_id"])] = provided
    for item in stage["inputs"]:
        if item["id"] not in by_target:
            _fail(
                "testbench_plan.compiler.binding_unresolved",
                f"/stages/{stage['id']}/inputs/{item['id']}",
                f"stage input {item['id']!r} requires a receipt-backed binding value",
            )
    return by_target


def _dc_grid(start: Decimal, stop: Decimal, step: Decimal) -> tuple[Decimal, ...]:
    if step <= 0 or stop < start:
        _fail(
            "testbench_plan.compiler.dc_range_invalid",
            "/analysis",
            "DC range requires stop >= start and a positive step",
        )
    count = int(((stop - start) / step).to_integral_value(rounding="ROUND_FLOOR")) + 1
    if not 1 <= count <= MAX_DC_SAMPLES:
        _fail(
            "testbench_plan.compiler.dc_range_over_limit",
            "/analysis",
            f"DC range expands outside 1..{MAX_DC_SAMPLES} samples",
        )
    values = tuple(start + step * index for index in range(count))
    if values[-1] != stop:
        _fail(
            "testbench_plan.compiler.dc_range_not_integral",
            "/analysis",
            "DC range must land exactly on stop",
        )
    return values


def _corner_values(corner: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return {str(item["id"]): item["value"] for item in corner["values"]}


def _supply_values(
    plan: PreparedTestbenchPlan, corner: Mapping[str, Any]
) -> Mapping[str, Decimal]:
    values = _corner_values(corner)
    output: dict[str, Decimal] = {}
    for supply in plan.supplies:
        reference = supply.voltage
        value = values.get(str(reference["value_id"]))
        if value is None or value["unit"] != "V":
            _fail(
                "testbench_plan.compiler.corner_value_unresolved",
                f"/corner_bindings/{corner['id']}/values",
                f"supply {supply.identifier!r} has no voltage binding",
            )
        output[supply.identifier] = _decimal(
            value["value"], path=f"/corner_bindings/{corner['id']}/{reference['value_id']}"
        )
    return output


def _scaled_level(
    level: Mapping[str, Any], supply_values: Mapping[str, Decimal], *, path: str
) -> Decimal:
    supply_id = str(level["supply_id"])
    if supply_id not in supply_values:
        _fail(
            "testbench_plan.compiler.supply_unresolved", path, f"unknown supply {supply_id!r}"
        )
    return supply_values[supply_id] * _decimal(level["fraction"], path=f"{path}/fraction")


def _pulse_levels(
    stimulus: Mapping[str, Any],
    polarity: str,
    supply_values: Mapping[str, Decimal],
) -> tuple[Decimal, Decimal]:
    low = _scaled_level(stimulus["low_level"], supply_values, path="/stimulus/low_level")
    high = _scaled_level(stimulus["high_level"], supply_values, path="/stimulus/high_level")
    return (low, high) if polarity == "active_high" else (high, low)


def _stimulus_source_names(stimulus: Mapping[str, Any]) -> tuple[str, ...]:
    base = f"V_STIM_{stimulus['id']}"
    if stimulus["kind"] == "phase_offset_pair":
        return f"{base}_REF", f"{base}_OFFSET"
    return (base,)


def _compile_one_condition(
    plan: PreparedTestbenchPlan,
    sealed: SealedDut,
    corner: Mapping[str, Any],
    stage_id: str,
    point: Mapping[str, Any],
    stage_inputs: Mapping[str, Mapping[str, Any]],
    resolved_parameters: Sequence[Mapping[str, Any]],
    stimuli: Mapping[str, Mapping[str, Any]],
    *,
    dc_sample: tuple[int, Decimal] | None,
) -> CompiledCondition:
    analysis = point["analysis"]
    resolved_analysis: dict[str, Any] = {"kind": analysis["kind"]}
    for field in ("source_stimulus_id", "stimulus_id"):
        if field in analysis:
            resolved_analysis[field] = analysis[field]
    for field in ("start", "stop", "step"):
        if field in analysis:
            resolved_analysis[field] = _resolved_quantity(
                analysis[field], stage_inputs, path=f"/analysis/{field}"
            )
    if dc_sample is not None:
        sample_index, sample_value = dc_sample
        resolved_analysis["sample_index"] = sample_index
        resolved_analysis["sample_value"] = {
            "value": _number_token(sample_value), "unit": analysis["start"]["unit"]
        }
    active = tuple(str(item) for item in point["active_stimulus_ids"])
    stimulus_details: list[Mapping[str, Any]] = []
    for identifier in active:
        item = stimuli[identifier]
        detail: dict[str, Any] = {"id": identifier, "kind": item["kind"]}
        if item["kind"] == "phase_offset_pair":
            detail["resolved_phase_offset"] = _resolved_quantity(
                item["phase_offset"], stage_inputs,
                path=f"/stimuli/{identifier}/phase_offset",
            )
        stimulus_details.append(detail)
    condition_semantics = {
        "stage_id": stage_id,
        "point_id": point["id"],
        "corner": corner,
        "parameters": list(resolved_parameters),
        "resolved_bindings": [dict(stage_inputs[key]) for key in sorted(stage_inputs)],
        "state_policy": point["state_policy"],
        "settle_policy": point["settle_policy"],
        "active_stimuli": stimulus_details,
        "analysis": resolved_analysis,
    }
    condition_sha = _sha256(_canonical_bytes(condition_semantics))
    if dc_sample is None:
        condition_id = f"{stage_id}.{point['id']}"
        relative = f"conditions/{stage_id}/{point['id']}.spice"
    else:
        sample_index, sample_value = dc_sample
        value_hash = _sha256(_number_token(sample_value).encode("ascii"))[:8]
        condition_id = f"{stage_id}.{point['id']}.dc_{sample_index:06d}_{value_hash}"
        relative = f"conditions/{stage_id}/{point['id']}/dc_{sample_index:06d}_{value_hash}.spice"
    deck, expected_probes = _serialize_deck(
        plan,
        sealed,
        corner,
        point,
        stage_inputs,
        condition_id=condition_id,
        condition_sha256=condition_sha,
        dc_sample=None if dc_sample is None else dc_sample[1],
    )
    deck_sha = _sha256(deck)
    receipt = {
        "condition_id": condition_id,
        "condition_sha256": condition_sha,
        "stage_id": stage_id,
        "point_id": point["id"],
        "condition": condition_semantics,
        "analysis": resolved_analysis,
        "deck": {"path": relative, "raw_sha256": deck_sha},
        "expected_probes": [dict(item) for item in expected_probes],
        "expected_measurements": [
            {"id": item["id"], "kind": item["kind"], "unit": item["unit"]}
            for item in point["measurements"]
        ],
        "validity_rules": [item["id"] for item in point["validity_rules"]],
    }
    return CompiledCondition(
        stage_id=stage_id,
        point_id=str(point["id"]),
        condition_id=condition_id,
        condition_sha256=condition_sha,
        relative_deck_path=relative,
        deck_bytes=deck,
        deck_sha256=deck_sha,
        expected_probes=expected_probes,
        receipt=receipt,
    )


def _serialize_deck(
    plan: PreparedTestbenchPlan,
    sealed: SealedDut,
    corner: Mapping[str, Any],
    point: Mapping[str, Any],
    stage_inputs: Mapping[str, Mapping[str, Any]],
    *,
    condition_id: str,
    condition_sha256: str,
    dc_sample: Decimal | None,
) -> tuple[bytes, tuple[Mapping[str, Any], ...]]:
    supplies = {item.identifier: item for item in plan.supplies}
    supply_values = _supply_values(plan, corner)
    stimuli = {item.identifier: item.document for item in plan.stimuli}
    active_ids = tuple(str(item) for item in point["active_stimulus_ids"])
    active = tuple(stimuli[item] for item in active_ids)

    instance_name = "X_OPENADA_DUT"
    current_ports = {
        str(probe.document["port"])
        for probe in plan.probes
        if probe.kind == "dut_port_current"
    }
    dut_terminals = {
        str(port["name"]): (
            f"N_OPENADA_DUT_{port['name']}"
            if str(port["name"]) in current_ports
            else str(plan.dut.connections[str(port["name"])])
        )
        for port in plan.dut.ports
    }

    lines = [
        "OpenADA closed testbench-plan condition",
        f"* compiler_id: {TESTBENCH_PLAN_NGSPICE_COMPILER_ID}",
        f"* plan_sha256: {plan.canonical_sha256}",
        f"* dut_sha256: {sealed.raw_sha256}",
        f"* condition_id: {condition_id}",
        f"* condition_sha256: {condition_sha256}",
        f".TEMP {_number_token(_decimal(corner['temperature']['value'], path='/corner/temperature/value'))}",
        "",
    ]
    lines.extend(sealed.canonical_bytes.decode("utf-8").rstrip("\n").split("\n"))
    lines.append("")

    for supply in plan.supplies:
        lines.append(
            " ".join(
                (
                    f"V_SUPPLY_{supply.identifier}",
                    supply.positive,
                    supply.negative,
                    "DC",
                    _number_token(supply_values[supply.identifier]),
                )
            )
        )

    analysis = point["analysis"]
    analysis_stimulus_id = str(
        analysis.get("source_stimulus_id", analysis.get("stimulus_id", ""))
    )
    emitted_sources: dict[str, tuple[str, ...]] = {}
    for stimulus in active:
        source_lines, source_names = _serialize_stimulus(
            stimulus,
            plan,
            supplies,
            supply_values,
            stage_inputs,
            dc_value=(
                dc_sample
                if stimulus["id"] == analysis_stimulus_id
                and stimulus["kind"] == "dc_state"
                else None
            ),
        )
        lines.extend(source_lines)
        emitted_sources[str(stimulus["id"])] = source_names

    for port in sorted(current_ports, key=str.casefold):
        external = str(plan.dut.connections[port])
        internal = dut_terminals[port]
        lines.append(f"V_PROBE_PORT_{port} {external} {internal} DC 0")
    instance_terminals = [
        dut_terminals[str(port["name"])] for port in plan.dut.ports
    ]
    lines.append(
        " ".join((instance_name, *instance_terminals, sealed.sealed_top))
    )

    state = point["state_policy"]
    initial_values: list[str] = []
    if state["kind"] == "fresh":
        for initial in state["initial_node_voltages"]:
            value = _number_token(
                _decimal(initial["value"]["value"], path="/state_policy/initial_node_voltages/value")
            )
            if initial["kind"] == "port":
                node = dut_terminals[str(initial["port"])]
            else:
                node = f"{instance_name}.{initial['node']}"
            initial_values.append(f"V({node})={value}")
    if initial_values:
        lines.append(".IC " + " ".join(initial_values))

    expected_probes, save_vectors = _expected_probes(
        plan,
        dut_terminals,
        instance_name=instance_name,
        emitted_sources=emitted_sources,
    )
    _require_measurement_probes(point, expected_probes)
    if save_vectors:
        lines.append(".SAVE " + " ".join(save_vectors))

    kind = str(analysis["kind"])
    if kind == "dc_sweep":
        if dc_sample is None:
            source = f"V_STIM_{analysis['source_stimulus_id']}"
            start = _number_token(_resolve_decimal(analysis["start"], stage_inputs, path="/analysis/start"))
            stop = _number_token(_resolve_decimal(analysis["stop"], stage_inputs, path="/analysis/stop"))
            step = _number_token(_resolve_decimal(analysis["step"], stage_inputs, path="/analysis/step"))
            lines.append(f".DC {source} {start} {stop} {step}")
        else:
            lines.append(".OP")
    elif kind in {"pulse_train_transient", "phase_offset_pair_transient"}:
        step = _number_token(_resolve_decimal(analysis["step"], stage_inputs, path="/analysis/step"))
        stop = _number_token(_resolve_decimal(analysis["stop"], stage_inputs, path="/analysis/stop"))
        suffix = " UIC" if initial_values else ""
        lines.append(f".TRAN {step} {stop}{suffix}")
    else:
        _fail(
            "testbench_plan.compiler.analysis_unsupported",
            "/analysis/kind",
            f"unsupported analysis kind {kind!r}",
        )
    lines.append(".END")
    return ("\n".join(lines) + "\n").encode("utf-8"), expected_probes


def _serialize_stimulus(
    stimulus: Mapping[str, Any],
    plan: PreparedTestbenchPlan,
    supplies: Mapping[str, Any],
    supply_values: Mapping[str, Decimal],
    stage_inputs: Mapping[str, Mapping[str, Any]],
    *,
    dc_value: Decimal | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    supply = supplies[str(stimulus["supply_id"])]
    negative = str(supply.negative)
    kind = str(stimulus["kind"])
    base = f"V_STIM_{stimulus['id']}"
    if kind == "dc_state":
        value = dc_value
        if value is None:
            value = _scaled_level(stimulus["level"], supply_values, path=f"/stimuli/{stimulus['id']}/level")
        target = str(plan.dut.connections[str(stimulus["target_port"])])
        return (f"{base} {target} {negative} DC {_number_token(value)}",), (base,)

    delay = _decimal(stimulus["delay"]["value"], path=f"/stimuli/{stimulus['id']}/delay")
    rise = _number_token(_decimal(stimulus["rise_time"]["value"], path="/stimulus/rise_time"))
    fall = _number_token(_decimal(stimulus["fall_time"]["value"], path="/stimulus/fall_time"))
    width = _number_token(_decimal(stimulus["pulse_width"]["value"], path="/stimulus/pulse_width"))
    period_value = _decimal(stimulus["period"]["value"], path="/stimulus/period")
    period = _number_token(period_value)
    count = str(stimulus["count"])

    def pulse_line(name: str, port: str, polarity: str, source_delay: Decimal) -> str:
        initial, pulsed = _pulse_levels(stimulus, polarity, supply_values)
        target = str(plan.dut.connections[port])
        arguments = " ".join(
            (
                _number_token(initial),
                _number_token(pulsed),
                _number_token(source_delay),
                rise,
                fall,
                width,
                period,
                count,
            )
        )
        return f"{name} {target} {negative} PULSE({arguments})"

    if kind == "pulse_train":
        return (
            pulse_line(base, str(stimulus["target_port"]), str(stimulus["polarity"]), delay),
        ), (base,)
    if kind == "phase_offset_pair":
        phase = _resolve_decimal(
            stimulus["phase_offset"], stage_inputs,
            path=f"/stimuli/{stimulus['id']}/phase_offset",
        )
        offset_delay = ((delay + phase) % period_value + period_value) % period_value
        reference_name, offset_name = _stimulus_source_names(stimulus)
        return (
            pulse_line(
                reference_name,
                str(stimulus["reference_port"]),
                str(stimulus["reference_polarity"]),
                delay,
            ),
            pulse_line(
                offset_name,
                str(stimulus["offset_port"]),
                str(stimulus["offset_polarity"]),
                offset_delay,
            ),
        ), (reference_name, offset_name)
    _fail(
        "testbench_plan.compiler.stimulus_unsupported",
        f"/stimuli/{stimulus['id']}/kind",
        f"unsupported stimulus kind {kind!r}",
    )


def _expected_probes(
    plan: PreparedTestbenchPlan,
    dut_terminals: Mapping[str, str],
    *,
    instance_name: str,
    emitted_sources: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    expected: list[Mapping[str, Any]] = []
    vectors: list[str] = []
    for probe in plan.probes:
        raw = probe.document
        item: dict[str, Any] = {
            "id": probe.identifier,
            "kind": probe.kind,
            "unit": probe.unit,
            "available": True,
        }
        native: tuple[str, ...]
        if probe.kind == "dut_port_voltage":
            port = str(raw["port"])
            reference = str(raw["reference_port"])
            native = (f"v({dut_terminals[port]},{dut_terminals[reference]})",)
            item["identity"] = {"port": port, "reference_port": reference}
            item["polarity"] = "as_declared"
        elif probe.kind == "dut_port_current":
            port = str(raw["port"])
            native = (f"i(V_PROBE_PORT_{port})",)
            item["identity"] = {"port": port, "branch": f"V_PROBE_PORT_{port}"}
            item["polarity_multiplier"] = 1 if raw["direction"] == "into_dut" else -1
        elif probe.kind == "dut_internal_node":
            reference = str(raw["reference_port"])
            native = (f"v({instance_name}.{raw['node']},{dut_terminals[reference]})",)
            item["identity"] = {
                "instance": instance_name,
                "node": raw["node"],
                "reference_port": reference,
            }
            item["polarity"] = "as_declared"
        elif probe.kind == "stimulus_branch_current":
            stimulus_id = str(raw["stimulus_id"])
            names = emitted_sources.get(stimulus_id, ())
            branch = str(raw["branch"])
            if branch == "single":
                selected = names[:1] if len(names) == 1 else ()
            elif branch == "reference":
                selected = names[:1] if len(names) == 2 else ()
            else:
                selected = names[1:2] if len(names) == 2 else ()
            native = tuple(f"i({name})" for name in selected)
            item["identity"] = {
                "stimulus_id": stimulus_id,
                "branch": branch,
                "source": selected[0] if selected else None,
            }
            item["polarity_multiplier"] = (
                -1 if raw["direction"] == "delivered_by_stimulus" else 1
            )
            if not native:
                item["available"] = False
        else:
            _fail(
                "testbench_plan.compiler.probe_unsupported",
                f"/probes/{probe.identifier}/kind",
                f"unsupported probe kind {probe.kind!r}",
            )
        item["native_vectors"] = list(native)
        expected.append(item)
        vectors.extend(native)
    # Preserve plan order while avoiding duplicate vectors.
    unique = tuple(dict.fromkeys(vectors))
    return tuple(expected), unique


def _require_measurement_probes(
    point: Mapping[str, Any], expected: Sequence[Mapping[str, Any]]
) -> None:
    availability = {str(item["id"]): bool(item["available"]) for item in expected}
    for measurement in point["measurements"]:
        probe_id = measurement.get("probe_id")
        if probe_id is not None and not availability.get(str(probe_id), False):
            _fail(
                "testbench_plan.compiler.measurement_probe_unavailable",
                f"/measurements/{measurement['id']}/probe_id",
                f"probe {probe_id!r} is not emitted in this condition",
            )
def _compile_conditions(
    plan: PreparedTestbenchPlan,
    sealed: SealedDut,
    corner: Mapping[str, Any],
    *,
    stage_ids: Sequence[str] | None,
    binding_values: Mapping[str, Mapping[str, Any]],
) -> tuple[CompiledCondition, ...]:
    stage_documents = {
        str(item["id"]): item for item in plan.document["stages"]
    }
    if stage_ids is None:
        selected_ids = tuple(stage.identifier for stage in plan.stages)
    else:
        if isinstance(stage_ids, (str, bytes)):
            _fail(
                "testbench_plan.compiler.stage_selector_invalid",
                "/stage_ids",
                "stage_ids must be a sequence of declared stage identifiers",
            )
        selected_ids = tuple(str(item) for item in stage_ids)
        if not selected_ids or len(set(selected_ids)) != len(selected_ids):
            _fail(
                "testbench_plan.compiler.stage_selector_invalid",
                "/stage_ids",
                "stage_ids must be non-empty and unique",
            )
        unknown = sorted(set(selected_ids) - set(stage_documents))
        if unknown:
            _fail(
                "testbench_plan.compiler.stage_unknown",
                "/stage_ids",
                f"unknown stages: {', '.join(unknown)}",
            )

    stimuli = {item.identifier: item.document for item in plan.stimuli}
    compiled: list[CompiledCondition] = []
    matching_points = 0
    for stage_id in selected_ids:
        stage = stage_documents[stage_id]
        stage_inputs = _stage_input_values(
            plan, stage, binding_values=binding_values
        )
        for point in stage["points"]:
            if point["condition"]["corner"] != corner["id"]:
                continue
            matching_points += 1
            state = point["state_policy"]
            if state["kind"] == "carryover":
                _fail(
                    "testbench_plan.compiler.state_unresolved",
                    f"/stages/{stage_id}/points/{point['id']}/state_policy",
                    "carryover compilation requires an upstream state receipt",
                )
            analysis = point["analysis"]
            resolved_parameters = tuple(
                {
                    "name": parameter["name"],
                    "value": _resolved_quantity(
                        parameter["value"], stage_inputs,
                        path=f"/stages/{stage_id}/points/{point['id']}/condition/parameters/{parameter['name']}",
                    ),
                }
                for parameter in point["condition"]["parameters"]
            )
            if analysis["kind"] == "dc_sweep" and state["kind"] == "fresh":
                start = _resolve_decimal(analysis["start"], stage_inputs, path="/analysis/start")
                stop = _resolve_decimal(analysis["stop"], stage_inputs, path="/analysis/stop")
                step = _resolve_decimal(analysis["step"], stage_inputs, path="/analysis/step")
                for sample_index, sample in enumerate(_dc_grid(start, stop, step)):
                    compiled.append(
                        _compile_one_condition(
                            plan,
                            sealed,
                            corner,
                            stage_id,
                            point,
                            stage_inputs,
                            resolved_parameters,
                            stimuli,
                            dc_sample=(sample_index, sample),
                        )
                    )
            else:
                compiled.append(
                    _compile_one_condition(
                        plan,
                        sealed,
                        corner,
                        stage_id,
                        point,
                        stage_inputs,
                        resolved_parameters,
                        stimuli,
                        dc_sample=None,
                    )
                )
    if matching_points == 0:
        _fail(
            "testbench_plan.compiler.corner_has_no_points",
            "/corner",
            f"corner {corner['id']!r} has no points in the selected stages",
        )
    return tuple(compiled)


def _compile_receipt(
    plan: PreparedTestbenchPlan,
    sealed: SealedDut,
    corner: Mapping[str, Any],
    conditions: Sequence[CompiledCondition],
) -> Mapping[str, Any]:
    corner_bytes = _canonical_bytes(corner)
    return {
        "schema": TESTBENCH_PLAN_COMPILE_RECEIPT_SCHEMA,
        "compiler_id": TESTBENCH_PLAN_NGSPICE_COMPILER_ID,
        "plan": {
            "id": plan.identifier,
            "raw_sha256": plan.raw_sha256,
            "canonical_sha256": plan.canonical_sha256,
        },
        "dut": {
            "binding_canonical_sha256": plan.dut_binding_canonical_sha256,
            "raw_sha256": sealed.raw_sha256,
            "canonical_sha256": sealed.canonical_sha256,
            "namespace": plan.dut.namespace,
            "top": plan.dut.top,
            "sealed_top": sealed.sealed_top,
        },
        "corner": {
            "id": corner["id"],
            "canonical_sha256": _sha256(corner_bytes),
        },
        "conditions": [dict(item.receipt) for item in conditions],
    }


def _publish_compilation(
    prepared: PreparedNgspiceCompilation, target: Path
) -> None:
    target = target.resolve()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    target_was_empty = False
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            _fail(
                "testbench_plan.compiler.output_exists",
                "/output_dir",
                "output path exists and is not a regular directory",
            )
        try:
            next(target.iterdir())
        except StopIteration:
            target_was_empty = True
        else:
            _fail(
                "testbench_plan.compiler.output_not_empty",
                "/output_dir",
                "output directory must be absent or empty",
            )
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent))
    try:
        _write_new(staging / "plan.json", prepared.plan_bytes)
        _write_new(staging / "dut.raw.spice", prepared.sealed_dut.raw_bytes)
        _write_new(staging / "dut.sealed.spice", prepared.sealed_dut.canonical_bytes)
        for item in prepared.conditions:
            _write_new(staging / item.relative_deck_path, item.deck_bytes)
        receipt_bytes = json.dumps(
            prepared.receipt, allow_nan=False, ensure_ascii=False,
            indent=2, sort_keys=True,
        ).encode("utf-8") + b"\n"
        _write_new(staging / "compile-receipt.json", receipt_bytes)
        if target_was_empty:
            # Recheck immediately before the only publication boundary.
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                _fail(
                    "testbench_plan.compiler.output_changed",
                    "/output_dir",
                    "empty output directory changed during compilation",
                )
            target.rmdir()
        os.rename(staging, target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _write_new(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
