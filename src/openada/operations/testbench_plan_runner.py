"""Deterministic execution and receipt layer for closed testbench plans.

The runner owns process isolation and exact artifact hashing; it never accepts
simulator arguments or environment overrides from a plan.  Execution is
exhaustive over compiler-emitted conditions.  Unsupported extraction or
measurement nodes fail closed: they emit a typed runner-owned UNKNOWN verdict
and no observable value, so they cannot receive credit as DUT invalidity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol

from ..engines.ngspice_outputs import extract_analysis_raw
from .testbench_plan import PreparedTestbenchPlan
from .testbench_plan_ngspice import (
    CompiledCondition,
    PreparedNgspiceCompilation,
    ResolvedBindingValue,
    prepare_testbench_plan_ngspice,
)


TESTBENCH_PLAN_RUN_RECEIPT_SCHEMA = "simra.testbench-plan-run/v1"
TESTBENCH_OBSERVABLES_SCHEMA = "simra.testbench-observables/v1"
TESTBENCH_PLAN_RUNNER_ID = "openada.testbench-plan.runner.ngspice/v1"
MAX_PROCESS_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_WAVEFORM_BYTES = 256 * 1024 * 1024
MAX_ENVELOPE_CONDITIONS = 10_000


class _UnsupportedValidityError(ValueError):
    """The plan node is valid v1 IR but has no runner implementation yet."""


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


def _bounded_text(value: bytes) -> str:
    return value[:MAX_PROCESS_CAPTURE_BYTES].decode("utf-8", errors="replace")


def _read_capture(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_PROCESS_CAPTURE_BYTES)


def _unknown_verdict(reason: object) -> str:
    """Return a bounded non-scoreable verdict for runner-owned uncertainty."""

    prefix = "UNKNOWN(runner: "
    suffix = ")"
    message = str(reason).replace("\x00", "")
    return prefix + message[: 1024 - len(prefix) - len(suffix)] + suffix


def _registered_dut_sha256(compilation: PreparedNgspiceCompilation) -> str:
    digest = compilation.sealed_dut.raw_sha256
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("compiler emitted an invalid sealed DUT digest")
    captured = _sha256(compilation.sealed_dut.raw_bytes)
    if digest != captured:
        raise ValueError("sealed DUT bytes do not match the compiler digest")
    return captured


def _assert_condition_capacity(
    compilation: PreparedNgspiceCompilation,
    *,
    existing_ids: set[str],
) -> None:
    identifiers = [item.condition_id for item in compilation.conditions]
    if len(existing_ids) + len(identifiers) > MAX_ENVELOPE_CONDITIONS:
        raise ValueError(
            "compiled condition inventory exceeds the 10000-condition "
            "testbench-observables/v1 boundary"
        )
    if len(identifiers) != len(set(identifiers)) or existing_ids.intersection(
        identifiers
    ):
        raise ValueError("compiler emitted duplicate condition identifiers")


@dataclass(frozen=True, slots=True)
class SimulatorExecution:
    """Exact outputs returned by one executor invocation."""

    returncode: int
    stdout_bytes: bytes
    stderr_bytes: bytes
    waveform_bytes: bytes
    simulator_identity: str


class ConditionExecutor(Protocol):
    """Injectable execution boundary used by tests and alternative hosts."""

    def __call__(
        self, condition: CompiledCondition, *, timeout_s: float
    ) -> SimulatorExecution: ...


@dataclass(frozen=True, slots=True)
class ConditionAttempt:
    condition_id: str
    stage_id: str
    point_id: str
    condition_sha256: str
    compiled_deck_sha256: str
    waveform_sha256: str
    simulator_identity: str
    simulator_invoked: bool
    returncode: int | None
    status: str
    reason: str
    emitted_observables: tuple[str, ...]
    stdout_sha256: str
    stderr_sha256: str

    def record(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "stage_id": self.stage_id,
            "point_id": self.point_id,
            "condition_sha256": self.condition_sha256,
            "compiled_deck_sha256": self.compiled_deck_sha256,
            "waveform_sha256": self.waveform_sha256,
            "simulator_identity": self.simulator_identity,
            "simulator_invoked": self.simulator_invoked,
            "returncode": self.returncode,
            "status": self.status,
            "reason": self.reason,
            "emitted_observables": list(self.emitted_observables),
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
        }


@dataclass(frozen=True, slots=True)
class TestbenchPlanRunRefusal:
    code: str
    condition_id: str | None
    message: str

    def record(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "condition_id": self.condition_id,
            "message": self.message[:4000],
        }


@dataclass(frozen=True, slots=True)
class TestbenchPlanRunResult:
    """Comparator envelope plus the fuller, non-schema execution receipt."""

    observables: Mapping[str, Any]
    attempts: tuple[ConditionAttempt, ...]
    refusals: tuple[TestbenchPlanRunRefusal, ...]
    receipt: Mapping[str, Any]


def publish_testbench_plan_run(
    result: TestbenchPlanRunResult, output_dir: str | Path
) -> Path:
    """Atomically publish timestamp-free observable and receipt JSON files."""

    if not isinstance(result, TestbenchPlanRunResult):
        raise ValueError("publish input must be a TestbenchPlanRunResult")
    requested = Path(os.path.abspath(os.fspath(output_dir)))
    if requested.is_symlink():
        raise ValueError("output_dir must not be a symbolic link")
    requested.parent.mkdir(parents=True, exist_ok=True)
    parent = requested.parent.resolve(strict=True)
    target = parent / requested.name
    target_was_empty = False
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("output_dir exists and is not a regular directory")
        if any(target.iterdir()):
            raise ValueError("output_dir must be absent or empty")
        target_was_empty = True
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent)
    )
    try:
        payloads = {
            "observables.json": result.observables,
            "run-receipt.json": result.receipt,
        }
        for name, payload in payloads.items():
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            with (staging / name).open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        if target_was_empty:
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise ValueError("empty output_dir changed during publication")
            target.rmdir()
        os.rename(staging, target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return target


@dataclass(frozen=True, slots=True)
class _Waveform:
    axis_name: str
    axis: tuple[float, ...]
    signals: Mapping[str, tuple[float, ...]]


@dataclass(frozen=True, slots=True)
class _ConditionValue:
    value: Any
    condition_ids: frozenset[str]


class HostNgspiceExecutor:
    """Sanitized, bounded native ngspice executor.

    The child receives a minimal deterministic environment and always runs in
    a fresh private directory.  ``SPICE_ASCIIRAWFILE=1`` makes the exact raw
    bytes independently inspectable while ``-n`` disables user init files.
    """

    def __init__(self, binary: str | Path = "ngspice") -> None:
        resolved = shutil.which(str(binary))
        if resolved is None:
            raise ValueError(f"ngspice executable {str(binary)!r} is unavailable")
        self.binary = str(Path(resolved).resolve())
        try:
            identity = subprocess.run(
                [self.binary, "--version-small"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"ngspice identity capture failed: {exc}") from exc
        version = (identity.stdout or identity.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        self.simulator_identity = version[:512] or "ngspice:unknown"

    def __call__(
        self, condition: CompiledCondition, *, timeout_s: float
    ) -> SimulatorExecution:
        with tempfile.TemporaryDirectory(prefix="openada-tbplan-run-") as root_text:
            root = Path(root_text)
            deck = root / "condition.spice"
            waveform = root / "waveform.raw"
            stdout_path = root / "stdout.log"
            stderr_path = root / "stderr.log"
            deck.write_bytes(condition.deck_bytes)
            environment = {
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
                "SPICE_ASCIIRAWFILE": "1",
                "TMPDIR": str(root),
            }
            try:
                with stdout_path.open("xb") as stdout_handle, stderr_path.open(
                    "xb"
                ) as stderr_handle:
                    process = subprocess.run(
                        [
                            self.binary,
                            "-n",
                            "-b",
                            "-r",
                            str(waveform),
                            str(deck),
                        ],
                        cwd=root,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        timeout=timeout_s,
                        check=False,
                    )
                stdout = _read_capture(stdout_path)
                stderr = _read_capture(stderr_path)
                waveform_size = waveform.stat().st_size if waveform.is_file() else 0
                if waveform_size > MAX_WAVEFORM_BYTES:
                    body = b""
                    returncode = -1
                    stderr += b"\nwaveform exceeds runner limit"
                else:
                    body = waveform.read_bytes() if waveform_size else b""
                    returncode = process.returncode
            except subprocess.TimeoutExpired:
                stdout = (
                    _read_capture(stdout_path)
                    if stdout_path.is_file()
                    else b""
                )
                stderr = (
                    _read_capture(stderr_path)
                    if stderr_path.is_file()
                    else b""
                )
                body = b""
                returncode = -1
                stderr += b"\nrunner timeout"
            return SimulatorExecution(
                returncode,
                stdout,
                stderr,
                body,
                self.simulator_identity,
            )


def _execute_compilation_conditions(
    compilation: PreparedNgspiceCompilation,
    point_documents: Mapping[tuple[str, str], Mapping[str, Any]],
    executor: ConditionExecutor,
    *,
    timeout_s: float,
) -> tuple[
    list[ConditionAttempt],
    list[TestbenchPlanRunRefusal],
    dict[tuple[str, str], list[tuple[CompiledCondition, _Waveform]]],
    set[str],
]:
    attempts: list[ConditionAttempt] = []
    refusals: list[TestbenchPlanRunRefusal] = []
    waveforms: dict[tuple[str, str], list[tuple[CompiledCondition, _Waveform]]] = {}
    simulator_identities: set[str] = set()
    for condition in compilation.conditions:
        point = point_documents.get((condition.stage_id, condition.point_id))
        execution: SimulatorExecution | None = None
        waveform_sha = _sha256(b"")
        stdout_sha = _sha256(b"")
        stderr_sha = _sha256(b"")
        reason = ""
        simulator_invoked = False
        try:
            if point is None:
                raise ValueError("compiled condition has no plan point")
            if _sha256(condition.deck_bytes) != condition.deck_sha256:
                raise ValueError("compiled deck bytes do not match compiler digest")
            semantic_digest = _sha256(
                _canonical_bytes(condition.receipt.get("condition", {}))
            )
            if semantic_digest != condition.condition_sha256:
                raise ValueError("compiled condition semantics do not match condition digest")
            if point["state_policy"]["kind"] != "fresh":
                raise ValueError("carryover execution is not supported without a state receipt")
            settle = point["settle_policy"]
            if settle["kind"] == "until_delta":
                raise ValueError("until_delta settle enforcement is not implemented")
            if (
                point["analysis"]["kind"] != "dc_sweep"
                and float(settle["duration"]["value"]) != 0.0
            ):
                raise ValueError(
                    "nonzero transient fixed_time settling is not implemented"
                )
            simulator_invoked = True
            candidate = executor(condition, timeout_s=timeout_s)
            if not isinstance(candidate, SimulatorExecution):
                raise ValueError("executor returned an invalid execution record")
            if (
                isinstance(candidate.returncode, bool)
                or not isinstance(candidate.returncode, int)
                or not isinstance(candidate.stdout_bytes, bytes)
                or not isinstance(candidate.stderr_bytes, bytes)
                or not isinstance(candidate.waveform_bytes, bytes)
                or not isinstance(candidate.simulator_identity, str)
                or not 1 <= len(candidate.simulator_identity) <= 512
            ):
                raise ValueError("executor returned malformed bounded fields")
            if len(candidate.waveform_bytes) > MAX_WAVEFORM_BYTES:
                raise ValueError("executor waveform exceeds runner limit")
            if (
                len(candidate.stdout_bytes) > MAX_PROCESS_CAPTURE_BYTES
                or len(candidate.stderr_bytes) > MAX_PROCESS_CAPTURE_BYTES
            ):
                raise ValueError("executor process capture exceeds runner limit")
            execution = candidate
            simulator_identities.add(execution.simulator_identity)
            stdout_sha = _sha256(execution.stdout_bytes)
            stderr_sha = _sha256(execution.stderr_bytes)
            waveform_sha = _sha256(execution.waveform_bytes)
            if execution.returncode != 0:
                raise ValueError(
                    f"simulator exit {execution.returncode}: "
                    f"{_bounded_text(execution.stderr_bytes)[:512]}"
                )
            if not execution.waveform_bytes:
                raise ValueError("simulator emitted no waveform bytes")
            waveform = _extract_waveform(condition, point, execution.waveform_bytes)
            waveforms.setdefault(
                (condition.stage_id, condition.point_id), []
            ).append((condition, waveform))
            status = "completed"
            reason = "ok"
        except Exception as exc:
            status = "invalid"
            reason = str(exc)[:1024] or type(exc).__name__
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.condition_invalid",
                    condition.condition_id,
                    reason,
                )
            )
        attempts.append(
            ConditionAttempt(
                condition_id=condition.condition_id,
                stage_id=condition.stage_id,
                point_id=condition.point_id,
                condition_sha256=condition.condition_sha256,
                compiled_deck_sha256=_sha256(condition.deck_bytes),
                waveform_sha256=waveform_sha,
                simulator_identity=(
                    execution.simulator_identity
                    if execution is not None
                    else "not-executed"
                ),
                simulator_invoked=simulator_invoked,
                returncode=execution.returncode if execution is not None else None,
                status=status,
                reason=reason,
                emitted_observables=(),
                stdout_sha256=stdout_sha,
                stderr_sha256=stderr_sha,
            )
        )
    return attempts, refusals, waveforms, simulator_identities


def _topological_stages(plan: PreparedTestbenchPlan) -> tuple[str, ...]:
    stages = {str(item["id"]): item for item in plan.document["stages"]}
    output: list[str] = []
    remaining = set(stages)
    while remaining:
        ready = [
            identifier for identifier in stages
            if identifier in remaining
            and set(stages[identifier]["depends_on"]).isdisjoint(remaining)
        ]
        if not ready:
            raise ValueError("validated stage graph unexpectedly contains a cycle")
        output.extend(ready)
        remaining.difference_update(ready)
    return tuple(output)


def _uncompiled_attempts(
    stage: Mapping[str, Any], corner: str, reason: str
) -> tuple[list[ConditionAttempt], bool]:
    output: list[ConditionAttempt] = []
    inventory_complete = True
    for point in stage["points"]:
        if point["condition"]["corner"] != corner:
            continue
        suffixes: list[str] = ["not_executed"]
        analysis = point["analysis"]
        if analysis["kind"] == "dc_sweep":
            try:
                if not all("value" in analysis[field] for field in ("start", "stop", "step")):
                    raise ValueError("unresolved bound DC grid")
                start = Decimal(str(analysis["start"]["value"]))
                stop = Decimal(str(analysis["stop"]["value"]))
                step = Decimal(str(analysis["step"]["value"]))
                if step <= 0 or stop < start:
                    raise ValueError("invalid DC grid")
                quotient = (stop - start) / step
                if quotient != quotient.to_integral_value():
                    raise ValueError("nonintegral DC grid")
                count = int(quotient) + 1
                if not 1 <= count <= MAX_ENVELOPE_CONDITIONS:
                    raise ValueError("DC grid over limit")
                suffixes = [f"not_executed.dc_{index:06d}" for index in range(count)]
            except (InvalidOperation, ValueError, OverflowError):
                inventory_complete = False
        for suffix in suffixes:
            identifier = f"{stage['id']}.{point['id']}.{suffix}"
            semantics = {
                "stage_id": stage["id"],
                "point_id": point["id"],
                "corner": corner,
                "status": "not_executed",
                "reason": reason,
                "inventory_complete": inventory_complete,
            }
            output.append(
                ConditionAttempt(
                    condition_id=identifier,
                    stage_id=str(stage["id"]),
                    point_id=str(point["id"]),
                    condition_sha256=_sha256(_canonical_bytes(semantics)),
                    compiled_deck_sha256=_sha256(b""),
                    waveform_sha256=_sha256(b""),
                    simulator_identity="not-executed",
                    simulator_invoked=False,
                    returncode=None,
                    status="invalid",
                    reason=reason,
                    emitted_observables=(),
                    stdout_sha256=_sha256(b""),
                    stderr_sha256=_sha256(b""),
                )
            )
    return output, inventory_complete


def _binding_component(
    item: _ConditionValue, component: str
) -> float:
    value = item.value
    if component in {"value", "crossing"}:
        selected = value
    elif component in {"slope", "intercept", "r2"} and isinstance(value, Mapping):
        selected = value.get(component)
    else:
        raise ValueError(f"unsupported binding component {component!r}")
    if isinstance(selected, bool) or not isinstance(selected, (int, float)):
        raise ValueError(f"binding component {component!r} is not a scalar")
    result = float(selected)
    if not math.isfinite(result):
        raise ValueError("binding component is non-finite")
    return result


def _resolved_stage_bindings(
    plan: PreparedTestbenchPlan,
    stage_id: str,
    condition_values: Mapping[tuple[str, str], Mapping[str, _ConditionValue]],
    attempts: Sequence[ConditionAttempt],
    corner: str,
) -> tuple[
    list[ResolvedBindingValue],
    list[TestbenchPlanRunRefusal],
    list[Mapping[str, Any]],
]:
    output: list[ResolvedBindingValue] = []
    refusals: list[TestbenchPlanRunRefusal] = []
    source_receipts: list[Mapping[str, Any]] = []
    attempt_index = {item.condition_id: item for item in attempts}
    for binding in plan.document["bindings"]:
        source = binding["from"]
        if source["stage_id"] != stage_id:
            continue
        if "measurement_id" not in source:
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.binding_unsupported",
                    None,
                    f"binding {binding['id']!r} requires an unsupported reduction source",
                )
            )
            continue
        key = (stage_id, str(source["point_id"]))
        item = condition_values.get(key, {}).get(str(source["measurement_id"]))
        if item is None:
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.binding_unavailable",
                    None,
                    f"binding {binding['id']!r} source measurement is unavailable",
                )
            )
            continue
        point = next(
            (
                candidate
                for stage in plan.document["stages"]
                if stage["id"] == stage_id
                for candidate in stage["points"]
                if candidate["id"] == source["point_id"]
            ),
            None,
        )
        point_attempts = [
            attempt for attempt in attempts
            if (attempt.stage_id, attempt.point_id) == key
        ]
        source_valid = bool(point_attempts) and all(
            attempt.status == "completed" for attempt in point_attempts
        )
        if source_valid and point is not None:
            for rule in point["validity_rules"]:
                try:
                    verdict = _evaluate_validity_rule(
                        rule, condition_values[key], plan, corner
                    )
                except Exception:
                    verdict = _unknown_verdict("validity evaluation failed")
                if verdict != "VALID":
                    source_valid = False
                    break
        if not source_valid:
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.binding_source_invalid", None,
                    f"binding {binding['id']!r} source point is not VALID",
                )
            )
            continue
        try:
            value = _binding_component(item, str(source["component"]))
        except ValueError as exc:
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.binding_invalid", None,
                    f"binding {binding['id']!r}: {exc}",
                )
            )
            continue
        contributing = [
            attempt_index[identifier].record()
            for identifier in sorted(item.condition_ids)
            if identifier in attempt_index
        ]
        if len(contributing) != len(item.condition_ids):
            refusals.append(
                TestbenchPlanRunRefusal(
                    "testbench_plan.runner.binding_lineage_missing", None,
                    f"binding {binding['id']!r} lacks a contributing condition receipt",
                )
            )
            continue
        source_receipt = {
            "plan_sha256": plan.canonical_sha256,
            "binding_id": binding["id"],
            "source": dict(source),
            "source_value": value,
            "unit": binding["unit"],
            "conditions": contributing,
        }
        source_receipt_sha = _sha256(_canonical_bytes(source_receipt))
        source_receipts.append(
            {"sha256": source_receipt_sha, "receipt": source_receipt}
        )
        output.append(
            ResolvedBindingValue(
                binding_id=str(binding["id"]),
                value=value,
                unit=str(binding["unit"]),
                source_receipt_sha256=source_receipt_sha,
            )
        )
    return output, refusals, source_receipts


def execute_testbench_plan_ngspice(
    plan: PreparedTestbenchPlan,
    *,
    corner: str,
    dut_artifact: str | Path | None = None,
    dut_sha256: str | None = None,
    executor: ConditionExecutor | None = None,
    timeout_s: float = 120.0,
    output_dir: str | Path | None = None,
    _prepared_compilation: PreparedNgspiceCompilation | None = None,
) -> TestbenchPlanRunResult:
    """Compile, exhaustively execute, evaluate, and receipt one plan/corner.

    ``_prepared_compilation`` is an internal test/integration seam.  Production
    callers receive compiler-owned bytes through the public parameters.
    """

    if not isinstance(plan, PreparedTestbenchPlan):
        raise ValueError("runner input must be a validated PreparedTestbenchPlan")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or not 0 < float(timeout_s) <= 3600
    ):
        raise ValueError("timeout_s must be finite and within (0, 3600]")
    started = time.monotonic()
    selected_corner = next(
        (item for item in plan.corner_bindings if item["id"] == corner), None
    )
    if selected_corner is None:
        raise ValueError(f"corner {corner!r} is not declared by the plan")
    if executor is None:
        try:
            run_executor: ConditionExecutor = HostNgspiceExecutor()
        except ValueError as exc:
            unavailable_reason = str(exc)

            def unavailable_executor(
                _condition: CompiledCondition, *, timeout_s: float
            ) -> SimulatorExecution:
                del timeout_s
                raise RuntimeError(unavailable_reason)

            run_executor = unavailable_executor
    else:
        run_executor = executor
    actual_dut_sha: str | None = None
    point_documents = {
        (str(stage["id"]), str(point["id"])): point
        for stage in plan.document["stages"]
        for point in stage["points"]
    }
    condition_values: dict[
        tuple[str, str], dict[str, _ConditionValue]
    ] = {}
    point_waveforms: dict[
        tuple[str, str], list[tuple[CompiledCondition, _Waveform]]
    ] = {}
    attempts: list[ConditionAttempt] = []
    refusals: list[TestbenchPlanRunRefusal] = []
    simulator_identities: set[str] = set()
    condition_inventory_complete = True
    binding_receipts: list[Mapping[str, Any]] = []
    stage_upstream_lineage: dict[str, set[str]] = {}

    compilations: list[PreparedNgspiceCompilation] = []
    if _prepared_compilation is not None:
        _assert_condition_capacity(_prepared_compilation, existing_ids=set())
        actual_dut_sha = _registered_dut_sha256(_prepared_compilation)
        compilations.append(_prepared_compilation)
        new_attempts, new_refusals, new_waveforms, identities = (
            _execute_compilation_conditions(
                _prepared_compilation, point_documents, run_executor,
                timeout_s=float(timeout_s)
            )
        )
        attempts.extend(new_attempts)
        refusals.extend(new_refusals)
        point_waveforms.update(new_waveforms)
        simulator_identities.update(identities)
    else:
        stages = {str(item["id"]): item for item in plan.document["stages"]}
        binding_values: list[ResolvedBindingValue] = []
        for stage_id in _topological_stages(plan):
            stage = stages[stage_id]
            if not any(
                point["condition"]["corner"] == corner
                for point in stage["points"]
            ):
                continue
            try:
                compilation = prepare_testbench_plan_ngspice(
                    plan,
                    corner=corner,
                    dut_artifact=dut_artifact,
                    dut_sha256=dut_sha256,
                    stage_ids=(stage_id,),
                    binding_values=tuple(binding_values),
                )
            except Exception as exc:
                reason = f"stage compilation refused: {str(exc)[:768]}"
                missing, complete = _uncompiled_attempts(stage, corner, reason)
                condition_inventory_complete &= complete
                attempts.extend(missing)
                refusals.append(
                    TestbenchPlanRunRefusal(
                        "testbench_plan.runner.stage_uncompiled", None, reason
                    )
                )
                continue
            _assert_condition_capacity(
                compilation,
                existing_ids={attempt.condition_id for attempt in attempts},
            )
            compilation_dut_sha = _registered_dut_sha256(compilation)
            if actual_dut_sha is None:
                actual_dut_sha = compilation_dut_sha
            elif actual_dut_sha != compilation_dut_sha:
                raise ValueError(
                    "stage compilations captured different DUT artifacts"
                )
            compilations.append(compilation)
            new_attempts, new_refusals, new_waveforms, identities = (
                _execute_compilation_conditions(
                    compilation, point_documents, run_executor,
                    timeout_s=float(timeout_s)
                )
            )
            attempts.extend(new_attempts)
            refusals.extend(new_refusals)
            point_waveforms.update(new_waveforms)
            simulator_identities.update(identities)
            for key, entries in new_waveforms.items():
                expected = [
                    attempt for attempt in new_attempts
                    if (attempt.stage_id, attempt.point_id) == key
                ]
                if len(entries) != len(expected) or any(
                    attempt.status != "completed" for attempt in expected
                ):
                    continue
                try:
                    condition_values[key] = _evaluate_point_group(
                        point_documents[key], entries, plan, corner,
                        frozenset(stage_upstream_lineage.get(stage_id, ())),
                    )
                except Exception as exc:
                    refusals.append(
                        TestbenchPlanRunRefusal(
                            "testbench_plan.runner.measurement_invalid", None,
                            f"{key[0]}.{key[1]}: {str(exc)[:1024]}",
                        )
                    )
            resolved, binding_refusals, source_receipts = _resolved_stage_bindings(
                plan, stage_id, condition_values, attempts, corner
            )
            binding_values.extend(resolved)
            refusals.extend(binding_refusals)
            binding_receipts.extend(source_receipts)
            bindings_by_id = {
                str(item["id"]): item for item in plan.document["bindings"]
            }
            for source_receipt in source_receipts:
                binding_id = str(source_receipt["receipt"]["binding_id"])
                binding = bindings_by_id[binding_id]
                target_stage = str(binding["to"]["stage_id"])
                stage_upstream_lineage.setdefault(target_stage, set()).update(
                    str(item["condition_id"])
                    for item in source_receipt["receipt"]["conditions"]
                )

    if _prepared_compilation is not None:
        for key, entries in point_waveforms.items():
            expected = [
                attempt for attempt in attempts
                if (attempt.stage_id, attempt.point_id) == key
            ]
            if len(entries) != len(expected) or any(
                attempt.status != "completed" for attempt in expected
            ):
                continue
            try:
                condition_values[key] = _evaluate_point_group(
                    point_documents[key], entries, plan, corner, frozenset()
                )
            except Exception as exc:
                refusals.append(
                    TestbenchPlanRunRefusal(
                        "testbench_plan.runner.measurement_invalid", None,
                        f"{key[0]}.{key[1]}: {str(exc)[:1024]}",
                    )
                )

    if actual_dut_sha is None:
        actual_dut_sha = dut_sha256 or plan.dut.sha256
    if (
        len(actual_dut_sha) != 64
        or any(character not in "0123456789abcdef" for character in actual_dut_sha)
    ):
        raise ValueError("runner DUT digest is not a lowercase SHA-256")
    condition_ids = [attempt.condition_id for attempt in attempts]
    if len(condition_ids) != len(set(condition_ids)):
        raise ValueError("execution produced duplicate condition identifiers")
    if len(condition_ids) > MAX_ENVELOPE_CONDITIONS:
        raise ValueError(
            "execution condition inventory exceeds the "
            "testbench-observables/v1 boundary"
        )

    observable_values, validity, observable_lineage, evaluation_refusals = (
        _evaluate_plan_outputs(plan, condition_values, attempts, corner)
    )
    refusals.extend(evaluation_refusals)
    known_condition_ids = set(condition_ids)
    for name, identifiers in list(observable_lineage.items()):
        unknown = set(identifiers).difference(known_condition_ids)
        if identifiers and not unknown:
            continue
        observable_values.pop(name, None)
        observable_lineage.pop(name, None)
        refusals.append(
            TestbenchPlanRunRefusal(
                "testbench_plan.runner.observable_lineage_invalid",
                None,
                f"observable {name!r} does not have closed condition lineage",
            )
        )
    emitted_by_condition: dict[str, list[str]] = {
        attempt.condition_id: [] for attempt in attempts
    }
    for name, ids in observable_lineage.items():
        for condition_id in ids:
            if condition_id in emitted_by_condition:
                emitted_by_condition[condition_id].append(name)
    attempts = [
        ConditionAttempt(
            **{
                **attempt.record(),
                "emitted_observables": tuple(
                    sorted(emitted_by_condition[attempt.condition_id])
                ),
            }
        )
        for attempt in attempts
    ]
    runtime = max(0.0, time.monotonic() - started)
    envelope = {
        "schema": TESTBENCH_OBSERVABLES_SCHEMA,
        "plan_sha256": plan.canonical_sha256,
        "dut_sha256": actual_dut_sha,
        "corner": corner,
        "validity": validity,
        "observables": observable_values,
        "metadata": {
            "grading_runtime_s": runtime,
            "conditions": [
                {
                    "id": attempt.condition_id,
                    "observables": list(attempt.emitted_observables),
                    "receipt": {
                        "compiled_deck_sha256": attempt.compiled_deck_sha256,
                        "waveform_sha256": attempt.waveform_sha256,
                    },
                }
                for attempt in attempts
            ],
            "lineage": [
                {"observable": name, "condition_ids": list(ids)}
                for name, ids in sorted(observable_lineage.items())
            ],
            "extensions": {},
        },
        "extensions": {},
    }
    attempt_records = [attempt.record() for attempt in attempts]
    refusal_records = [item.record() for item in refusals]
    compiler_receipts = [item.receipt for item in compilations]
    receipt = {
        "schema": TESTBENCH_PLAN_RUN_RECEIPT_SCHEMA,
        "runner_id": TESTBENCH_PLAN_RUNNER_ID,
        "compiler_receipt_sha256": _sha256(
            _canonical_bytes(compiler_receipts)
        ),
        "compiler_receipts": compiler_receipts,
        "binding_receipts": binding_receipts,
        "plan_sha256": plan.canonical_sha256,
        "dut_sha256": actual_dut_sha,
        "corner": corner,
        "condition_inventory_complete": condition_inventory_complete,
        "expected_condition_count": len(attempts),
        "attempted_condition_count": len(attempts),
        "simulator_invocation_count": sum(
            attempt.simulator_invoked for attempt in attempts
        ),
        "completed_condition_count": sum(
            attempt.status == "completed" for attempt in attempts
        ),
        "not_executed_condition_count": sum(
            attempt.returncode is None for attempt in attempts
        ),
        "simulator_identities": sorted(simulator_identities),
        "environment": {
            "LANG": "C",
            "LC_ALL": "C",
            "SPICE_ASCIIRAWFILE": "1",
            "ambient_inherited": False,
        },
        "settle_semantics": {
            "fresh_dc": "independent_operating_point_per_sample",
            "fixed_time_dc": "dc_operating_point_solver",
            "transient_fixed_time": "only_zero_duration_supported",
            "until_delta": "unsupported_fail_closed",
            "carryover": "unsupported_fail_closed",
        },
        "attempts": attempt_records,
        "refusals": refusal_records,
        "observable_envelope_sha256": _sha256(_canonical_bytes(envelope)),
    }
    result = TestbenchPlanRunResult(
        envelope, tuple(attempts), tuple(refusals), receipt
    )
    if output_dir is not None:
        publish_testbench_plan_run(result, output_dir)
    return result


def _extract_waveform(
    condition: CompiledCondition,
    point: Mapping[str, Any],
    waveform_bytes: bytes,
) -> _Waveform:
    names: list[str] = []
    requested_aliases: dict[str, str] = {}
    for probe in condition.expected_probes:
        candidates = probe.get("native_vectors")
        if candidates is None:
            candidates = (probe.get("native_vector") or probe.get("vector"),)
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            candidates = ()
        for native in candidates:
            if isinstance(native, str) and native and native not in names:
                selected = native
                folded = selected.casefold()
                if folded.startswith("v(") and folded.endswith(",0)"):
                    selected = selected[:-3] + ")"
                names.append(selected.casefold())
                requested_aliases[selected.casefold()] = native.casefold()
    if not names:
        raise ValueError("compiler condition declares no native probe vectors")
    with tempfile.TemporaryDirectory(prefix="openada-tbplan-raw-") as root_text:
        path = Path(root_text) / "waveform.raw"
        path.write_bytes(waveform_bytes)
        analysis = point["analysis"]
        binding = {"type": (
            "op"
            if analysis["kind"] == "dc_sweep"
            and "sample_value" in condition.receipt.get("analysis", {})
            else "dc" if analysis["kind"] == "dc_sweep" else "tran"
        )}
        extraction = extract_analysis_raw(
            path,
            backend="ngspice",
            analysis=binding,
            selected_variables=names,
            expected_bytes=len(waveform_bytes),
            expected_sha256=_sha256(waveform_bytes),
            max_points=2_000_000,
            max_selected_scalars=64_000_000,
        )
    if not extraction.valid:
        raise ValueError(f"waveform extraction refused: {extraction.reason}")
    signals: dict[str, tuple[float, ...]] = {}
    for signal in extraction.signals:
        if signal.imaginary_values is not None:
            raise ValueError(f"complex probe {signal.name!r} is unsupported")
        key = signal.name.casefold()
        signals[requested_aliases.get(key, key)] = signal.real_values
    return _Waveform(
        extraction.axis_name or "axis",
        extraction.axis_values,
        signals,
    )


def _probe_vectors(
    condition: CompiledCondition,
) -> dict[str, tuple[str, float]]:
    output: dict[str, tuple[str, float]] = {}
    for probe in condition.expected_probes:
        identifier = probe.get("id") or probe.get("probe_id")
        candidates = probe.get("native_vectors")
        if candidates is None:
            candidates = (probe.get("native_vector") or probe.get("vector"),)
        if (
            isinstance(identifier, str)
            and isinstance(candidates, Sequence)
            and not isinstance(candidates, (str, bytes))
            and len(candidates) == 1
            and isinstance(candidates[0], str)
        ):
            output[identifier] = (
                str(candidates[0]).casefold(),
                float(probe.get("polarity_multiplier", 1)),
            )
    return output


def _signal_values(waveform: _Waveform, vector: str) -> tuple[float, ...] | None:
    direct = waveform.signals.get(vector.casefold())
    if direct is not None:
        return direct
    folded = vector.casefold()
    if folded.startswith("v(") and folded.endswith(",0)"):
        return waveform.signals.get(folded[:-3] + ")")
    return None


def _literal(value: Mapping[str, Any], *, label: str) -> float:
    if "value" not in value:
        raise ValueError(f"{label} requires a compiler-resolved literal")
    result = float(value["value"])
    if not math.isfinite(result):
        raise ValueError(f"{label} is non-finite")
    return result


def _window_indices(
    axis: Sequence[float], window: Mapping[str, Any]
) -> list[int]:
    start = _literal(window["start"], label="window.start")
    stop = _literal(window["stop"], label="window.stop")
    selected = [index for index, value in enumerate(axis) if start <= value <= stop]
    if len(selected) < 2:
        raise ValueError("measurement window has fewer than two waveform samples")
    return selected


def _trapz(axis: Sequence[float], values: Sequence[float], indices: Sequence[int]) -> float:
    total = 0.0
    for left, right in zip(indices, indices[1:]):
        total += (axis[right] - axis[left]) * (values[right] + values[left]) / 2.0
    return total


def _interpolate_at(
    axis: Sequence[float], values: Sequence[float], target: float
) -> float:
    for index, value in enumerate(axis):
        if value == target:
            return float(values[index])
        if index and axis[index - 1] < target < value:
            left_x = axis[index - 1]
            ratio = (target - left_x) / (value - left_x)
            return float(values[index - 1]) + ratio * (
                float(values[index]) - float(values[index - 1])
            )
    raise ValueError("integration boundary is outside the waveform axis")


def _integrate_window(
    axis: Sequence[float], values: Sequence[float], window: Mapping[str, Any]
) -> float:
    if len(axis) != len(values) or len(axis) < 2:
        raise ValueError("integration requires at least two paired waveform samples")
    if any(after <= before for before, after in zip(axis, axis[1:])):
        raise ValueError("integration axis must be strictly increasing")
    start = _literal(window["start"], label="window.start")
    stop = _literal(window["stop"], label="window.stop")
    if stop <= start or start < axis[0] or stop > axis[-1]:
        raise ValueError("integration window is outside the waveform axis")
    selected_axis = [start]
    selected_values = [_interpolate_at(axis, values, start)]
    for x, y in zip(axis, values):
        if start < x < stop:
            selected_axis.append(float(x))
            selected_values.append(float(y))
    selected_axis.append(stop)
    selected_values.append(_interpolate_at(axis, values, stop))
    result = sum(
        (right_x - left_x) * (left_y + right_y) / 2.0
        for left_x, right_x, left_y, right_y in zip(
            selected_axis,
            selected_axis[1:],
            selected_values,
            selected_values[1:],
        )
    )
    if not math.isfinite(result):
        raise ValueError("integrated result is non-finite")
    return result


def _crossing(
    axis: Sequence[float],
    values: Sequence[float],
    threshold: float,
    direction: str,
    occurrence: int,
) -> float:
    seen = 0
    for index, (before, after) in enumerate(zip(values, values[1:])):
        rising = before < threshold <= after
        falling = before > threshold >= after
        if not (rising if direction == "rising" else falling if direction == "falling" else rising or falling):
            continue
        seen += 1
        if seen != occurrence:
            continue
        if after == before:
            return axis[index + 1]
        ratio = (threshold - before) / (after - before)
        return axis[index] + ratio * (axis[index + 1] - axis[index])
    raise ValueError("declared crossing occurrence was not observed")


def _linear_fit(x: Sequence[float], y: Sequence[float]) -> dict[str, float]:
    if len(x) != len(y) or len(x) < 2:
        raise ValueError("linear fit needs at least two paired samples")
    xmean = sum(x) / len(x)
    ymean = sum(y) / len(y)
    denominator = sum((item - xmean) ** 2 for item in x)
    if denominator == 0:
        raise ValueError("linear fit axis has zero variance")
    slope = sum((a - xmean) * (b - ymean) for a, b in zip(x, y)) / denominator
    intercept = ymean - slope * xmean
    residual = sum((b - (slope * a + intercept)) ** 2 for a, b in zip(x, y))
    spread = sum((b - ymean) ** 2 for b in y)
    r2 = 1.0 if spread == 0 and residual == 0 else 1.0 - residual / spread if spread else 0.0
    return {"slope": slope, "intercept": intercept, "r2": r2}


def _evaluate_point_group(
    point: Mapping[str, Any],
    entries: Sequence[tuple[CompiledCondition, _Waveform]],
    plan: PreparedTestbenchPlan,
    corner: str,
    upstream_lineage: frozenset[str],
) -> dict[str, _ConditionValue]:
    analysis_kind = str(point["analysis"]["kind"])
    if analysis_kind == "dc_sweep" and all(
        "sample_value" in condition.receipt.get("analysis", {})
        for condition, _ in entries
    ):
        ordered = sorted(
            entries,
            key=lambda item: int(item[0].receipt["analysis"]["sample_index"]),
        )
        first_map = _probe_vectors(ordered[0][0])
        axis: list[float] = []
        series: dict[str, list[float]] = {name: [] for name in first_map}
        lineage: set[str] = set()
        for condition, waveform in ordered:
            current_map = _probe_vectors(condition)
            if current_map != first_map:
                raise ValueError("fresh DC sample probe identities differ")
            axis.append(float(condition.receipt["analysis"]["sample_value"]["value"]))
            lineage.add(condition.condition_id)
            for probe_id, (vector, polarity) in current_map.items():
                raw_values = _signal_values(waveform, vector)
                if raw_values is None or len(raw_values) != 1:
                    raise ValueError(
                        f"fresh DC sample {condition.condition_id!r} does not contain one {probe_id!r} value"
                    )
                series[probe_id].append(raw_values[0] * polarity)
        combined = _Waveform(
            "dc_source",
            tuple(axis),
            {name: tuple(values) for name, values in series.items()},
        )
        probes = {name: (name, 1.0) for name in series}
        return _evaluate_measurements(
            point,
            combined,
            plan,
            corner,
            probes,
            frozenset(lineage) | upstream_lineage,
        )
    if len(entries) != 1:
        raise ValueError("non-DC point compiled into an ambiguous condition set")
    condition, waveform = entries[0]
    return _evaluate_measurements(
        point,
        waveform,
        plan,
        corner,
        _probe_vectors(condition),
        frozenset((condition.condition_id,)) | upstream_lineage,
    )


def _evaluate_measurements(
    point: Mapping[str, Any],
    waveform: _Waveform,
    plan: PreparedTestbenchPlan,
    corner: str,
    probes: Mapping[str, tuple[str, float]],
    base_lineage: frozenset[str],
) -> dict[str, _ConditionValue]:
    values: dict[str, _ConditionValue] = {}
    for measurement in point["measurements"]:
        identifier = str(measurement["id"])
        kind = str(measurement["kind"])
        lineage = base_lineage
        if kind == "curve":
            selected_probe = probes.get(str(measurement["probe_id"]))
            if selected_probe is None or _signal_values(waveform, selected_probe[0]) is None:
                raise ValueError(f"measurement {identifier!r} probe vector is absent")
            vector, polarity = selected_probe
            source_values = _signal_values(waveform, vector)
            assert source_values is not None
            values[identifier] = _ConditionValue(
                {
                    "x": list(waveform.axis),
                    "y": [item * polarity for item in source_values],
                },
                lineage,
            )
        elif kind == "integrate":
            selected_probe = probes.get(str(measurement["probe_id"]))
            if selected_probe is None or _signal_values(waveform, selected_probe[0]) is None:
                raise ValueError(f"measurement {identifier!r} probe vector is absent")
            vector, polarity = selected_probe
            source_values = _signal_values(waveform, vector)
            assert source_values is not None
            result = _integrate_window(
                waveform.axis,
                tuple(item * polarity for item in source_values),
                measurement["window"],
            )
            if measurement["normalization"]["kind"] == "pulse_count":
                result /= int(measurement["normalization"]["count"])
            values[identifier] = _ConditionValue(result, lineage)
        elif kind in {"linear_fit", "crossing", "sign", "max_abs", "compliance_interval"}:
            parent_id = str(measurement["input_measurement_id"])
            parent = values.get(parent_id)
            if parent is None:
                raise ValueError(f"measurement {identifier!r} parent is unavailable")
            if kind == "linear_fit":
                curve = parent.value
                selected = _window_indices(curve["x"], measurement["window"])
                payload = _linear_fit(
                    [curve["x"][index] for index in selected],
                    [curve["y"][index] for index in selected],
                )
            elif kind == "crossing":
                curve = parent.value
                threshold = _threshold(measurement["threshold"], plan, corner)
                payload = _crossing(
                    curve["x"], curve["y"], threshold,
                    str(measurement["direction"]), int(measurement["occurrence"]),
                )
            elif kind == "sign":
                tolerance = _literal(measurement["zero_tolerance"], label="zero_tolerance")
                source = parent.value["y"] if isinstance(parent.value, Mapping) else [parent.value]
                payload = [0 if abs(item) <= tolerance else 1 if item > 0 else -1 for item in source]
            elif kind == "max_abs":
                source = parent.value["y"] if isinstance(parent.value, Mapping) else parent.value
                payload = max(abs(float(item)) for item in source) if isinstance(source, Sequence) else abs(float(source))
            else:
                curve = parent.value
                lower = _literal(measurement["lower"], label="compliance.lower")
                upper = _literal(measurement["upper"], label="compliance.upper")
                accepted = [lower <= y <= upper for y in curve["y"]]
                accepted_indices = [
                    index for index, item in enumerate(accepted) if item
                ]
                if not accepted_indices:
                    raise ValueError("compliance interval has no accepted samples")
                if any(
                    right != left + 1
                    for left, right in zip(accepted_indices, accepted_indices[1:])
                ):
                    raise ValueError("compliance interval is disjoint")
                payload = {
                    "lower": curve["x"][accepted_indices[0]],
                    "upper": curve["x"][accepted_indices[-1]],
                }
            values[identifier] = _ConditionValue(payload, parent.condition_ids)
        elif kind == "mismatch_fraction":
            actual = values.get(str(measurement["actual_measurement_id"]))
            reference = values.get(str(measurement["reference_measurement_id"]))
            if actual is None or reference is None:
                raise ValueError(f"measurement {identifier!r} parent is unavailable")
            floor = _literal(measurement["floor"], label="mismatch.floor")
            denominator = max(abs(float(actual.value)), abs(float(reference.value)), floor)
            values[identifier] = _ConditionValue(
                abs(float(actual.value) - float(reference.value)) / denominator,
                actual.condition_ids | reference.condition_ids,
            )
        else:
            raise ValueError(f"measurement kind {kind!r} is unsupported")
        if not _finite_json(values[identifier].value):
            raise ValueError(f"measurement {identifier!r} produced non-finite data")
    return values


def _threshold(
    value: Mapping[str, Any], plan: PreparedTestbenchPlan, corner_id: str
) -> float:
    if value.get("kind") != "supply_scaled":
        return _literal(value, label="threshold")
    supply_id = str(value["supply_id"])
    supply = next((item for item in plan.supplies if item.identifier == supply_id), None)
    if supply is None:
        raise ValueError(f"threshold supply {supply_id!r} is unavailable")
    value_id = str(supply.voltage["value_id"])
    corner = next(
        (
            item for item in plan.corner_bindings
            if item["id"] == corner_id
        ),
        None,
    )
    if corner is None:
        raise ValueError(f"threshold supply value {value_id!r} is unavailable")
    voltage = next(entry["value"]["value"] for entry in corner["values"] if entry["id"] == value_id)
    return float(voltage) * float(value["fraction"])


def _evaluate_plan_outputs(
    plan: PreparedTestbenchPlan,
    condition_values: Mapping[tuple[str, str], Mapping[str, _ConditionValue]],
    attempts: Sequence[ConditionAttempt],
    corner: str,
) -> tuple[
    dict[str, Any], dict[str, str], dict[str, tuple[str, ...]], list[TestbenchPlanRunRefusal]
]:
    observables: dict[str, Any] = {}
    validity_by_source: dict[tuple[str, ...], str] = {}
    validity_lineage: dict[tuple[str, ...], frozenset[str]] = {}
    lineage: dict[str, tuple[str, ...]] = {}
    refusals: list[TestbenchPlanRunRefusal] = []
    point_conditions: dict[tuple[str, str], list[str]] = {}
    for attempt in attempts:
        point_conditions.setdefault((attempt.stage_id, attempt.point_id), []).append(
            attempt.condition_id
        )
    for stage in plan.document["stages"]:
        stage_id = str(stage["id"])
        for point in stage["points"]:
            point_id = str(point["id"])
            if point["condition"]["corner"] != corner:
                continue
            key = (stage_id, point_id)
            values = condition_values.get(key, {})
            invalid_conditions = [
                attempt for attempt in attempts
                if (attempt.stage_id, attempt.point_id) == key and attempt.status != "completed"
            ]
            for rule in point["validity_rules"]:
                identity = ("point", stage_id, point_id, str(rule["id"]))
                rule_lineage = {
                    condition_id
                    for item in values.values()
                    for condition_id in item.condition_ids
                }
                if not rule_lineage:
                    rule_lineage.update(point_conditions.get(key, ()))
                validity_lineage[identity] = frozenset(rule_lineage)
                if invalid_conditions:
                    verdict = _unknown_verdict(invalid_conditions[0].reason)
                else:
                    try:
                        verdict = _evaluate_validity_rule(rule, values, plan, corner)
                    except Exception as exc:
                        verdict = _unknown_verdict(
                            f"validity evaluation failed: {str(exc)[:512]}"
                        )
                        refusals.append(
                            TestbenchPlanRunRefusal(
                                (
                                    "testbench_plan.runner.validity_unsupported"
                                    if isinstance(exc, _UnsupportedValidityError)
                                    else "testbench_plan.runner.validity_invalid"
                                ),
                                None,
                                f"{stage_id}.{point_id}.{rule['id']}: {str(exc)[:1024]}",
                            )
                        )
                validity_by_source[identity] = verdict
        selected_stage_attempts = {
            attempt.condition_id for attempt in attempts
            if attempt.stage_id == stage_id
        }
        if stage["reductions"] and selected_stage_attempts:
            for rule in stage["validity_rules"]:
                identity = ("stage", stage_id, str(rule["id"]))
                validity_by_source[identity] = (
                    _unknown_verdict("stage reductions unsupported")
                )
                refusals.append(
                    TestbenchPlanRunRefusal(
                        "testbench_plan.runner.validity_unsupported",
                        None,
                        f"{stage_id}.{rule['id']}: stage reduction validity is unsupported",
                    )
                )
                transitive = set(selected_stage_attempts)
                for (value_stage, _), values in condition_values.items():
                    if value_stage == stage_id:
                        transitive.update(
                            condition_id
                            for item in values.values()
                            for condition_id in item.condition_ids
                        )
                validity_lineage[identity] = frozenset(transitive)
    counts: dict[str, int] = {}
    for identity in validity_by_source:
        rule_id = identity[-1]
        counts[rule_id] = counts.get(rule_id, 0) + 1
    validity: dict[str, str] = {}
    validity_names: dict[tuple[str, ...], str] = {}
    for identity, verdict in validity_by_source.items():
        if identity[0] == "point":
            _, stage_id, point_id, rule_id = identity
            name = rule_id if counts[rule_id] == 1 else f"{stage_id}.{point_id}.{rule_id}"
        else:
            _, stage_id, rule_id = identity
            name = rule_id if counts[rule_id] == 1 else f"{stage_id}.{rule_id}"
        validity[name] = verdict
        validity_names[identity] = name
    for observable in plan.document["observables"]:
        name = str(observable["id"])
        source = observable["source"]
        kind = str(source["kind"])
        if kind == "measurement":
            key = (str(source["stage_id"]), str(source["point_id"]))
            item = condition_values.get(key, {}).get(str(source["measurement_id"]))
            if item is None:
                refusals.append(TestbenchPlanRunRefusal(
                    "testbench_plan.runner.observable_unavailable", None,
                    f"observable {name!r} measurement is unavailable",
                ))
                continue
            observables[name] = item.value
            lineage[name] = tuple(sorted(item.condition_ids))
        elif kind == "validity":
            identity = (
                "point", str(source["stage_id"]), str(source["point_id"]),
                str(source["rule_id"]),
            )
            validity_name = validity_names.get(identity)
            if validity_name is None:
                continue
            observables[name] = validity[validity_name]
            linked = validity_lineage.get(identity, frozenset())
            if linked:
                lineage[name] = tuple(sorted(linked))
            else:
                observables.pop(name, None)
                refusals.append(
                    TestbenchPlanRunRefusal(
                        "testbench_plan.runner.observable_lineage_missing", None,
                        f"validity observable {name!r} has no condition receipt",
                    )
                )
        else:
            identity = (
                "stage", str(source["stage_id"]), str(source.get("rule_id", ""))
            )
            validity_name = validity_names.get(identity)
            if kind == "stage_validity" and validity_name is not None:
                observables[name] = validity[validity_name]
                linked = validity_lineage.get(identity, frozenset())
                if linked:
                    lineage[name] = tuple(sorted(linked))
                else:
                    observables.pop(name, None)
                    refusals.append(
                        TestbenchPlanRunRefusal(
                            "testbench_plan.runner.observable_lineage_missing", None,
                            f"stage-validity observable {name!r} has no condition receipt",
                        )
                    )
            else:
                refusals.append(TestbenchPlanRunRefusal(
                    "testbench_plan.runner.observable_unsupported", None,
                    f"observable {name!r} requires unsupported stage reduction evaluation",
                ))
    return observables, validity, lineage, refusals


def _evaluate_validity_rule(
    rule: Mapping[str, Any],
    values: Mapping[str, _ConditionValue],
    plan: PreparedTestbenchPlan,
    corner: str,
) -> str:
    kind = str(rule["kind"])
    selected_ids = (
        list(rule["measurement_ids"])
        if "measurement_ids" in rule else [rule.get("measurement_id")]
    )
    selected = [values.get(str(item)) for item in selected_ids]
    if kind == "pulse_count":
        raise _UnsupportedValidityError(
            "observed pulse-count extraction is unsupported"
        )
    elif any(item is None for item in selected):
        raise ValueError("validity input is unavailable")
    elif kind == "finite":
        passed = all(_finite_json(item.value) for item in selected if item is not None)
    elif kind == "r2":
        passed = float(selected[0].value["r2"]) >= float(rule["minimum"])
    elif kind == "threshold":
        actual = float(selected[0].value)
        limit = _threshold(rule["value"], plan, corner)
        comparison = str(rule["comparison"])
        passed = {
            "lt": actual < limit,
            "lte": actual <= limit,
            "gt": actual > limit,
            "gte": actual >= limit,
            "abs_lte": abs(actual) <= limit,
        }[comparison]
    elif kind == "settling_delta":
        passed = abs(float(selected[0].value) - float(selected[1].value)) <= _literal(rule["maximum"], label="settling.maximum")
    else:
        raise _UnsupportedValidityError(f"validity kind {kind!r} is unsupported")
    return "VALID" if passed else f"INVALID({rule['on_fail']['reason']})"


def _finite_json(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False
    if isinstance(value, Mapping):
        return all(_finite_json(item) for item in value.values())
    if isinstance(value, Sequence):
        return all(_finite_json(item) for item in value)
    return False


__all__ = [
    "ConditionAttempt",
    "ConditionExecutor",
    "HostNgspiceExecutor",
    "SimulatorExecution",
    "TESTBENCH_OBSERVABLES_SCHEMA",
    "TESTBENCH_PLAN_RUNNER_ID",
    "TESTBENCH_PLAN_RUN_RECEIPT_SCHEMA",
    "TestbenchPlanRunRefusal",
    "TestbenchPlanRunResult",
    "execute_testbench_plan_ngspice",
    "publish_testbench_plan_run",
]
