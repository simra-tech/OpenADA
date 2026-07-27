"""testbench.simulate semantics for published Simra schematic artifacts.

``circuit.simulate/v1alpha2`` runs exactly one analysis from one self-contained
deck. Simra publishes a testbench as an artifact whose typed declaration may
carry several analyses, and whose emitted deck therefore carries several
top-level analysis cards. Such an artifact is reported as
``simulation_handoff == "split_required"`` and is rejected outright by the
shared simulation profile.

This operation owns that gap. It binds one published artifact by its own
SHA-256 digests, derives exactly one single-analysis deck per declared
analysis, dispatches every derived deck through the reviewed shared simulation
profile, and returns one aggregated evidence envelope. It does not reinterpret
convergence, reimplement a native mapping, or bind an unresolved PDK parameter.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import uuid

from .. import __version__
from ..contract import (
    FileRecordError,
    bounded_text,
    diagnostic,
    file_record,
    result,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..driver_registry import (
    TESTBENCH_DRIVER_ALIASES,
    TESTBENCH_EVIDENCE_ASSERTION,
    TESTBENCH_SIMULATE_PROFILE,
    analysis_feature,
    builtin_driver,
)
from ..engines.simra_artifact import (
    DerivedDeck,
    SimraArtifactError,
    SimraTestbench,
    deck_file_name,
    derive_single_analysis_decks,
    load_model_prelude,
    load_simra_testbench,
)
from .circuit_simulate import simulate_circuit_profile


OPERATION_NAME = "testbench.simulate"
SUPPORTED_BACKENDS = tuple(sorted(TESTBENCH_DRIVER_ALIASES))
MAX_DISPATCHED_ANALYSES = 16
MAX_CHILD_RESULT_BYTES = 8 * 1024 * 1024

#: Worst-wins precedence when aggregating child execution statuses. A parent
#: may never report a stronger execution status than its weakest child.
_EXECUTION_PRECEDENCE = (
    "completed",
    "timed_out",
    "not_available",
    "failed",
    "invalid_request",
)


def _aggregate_execution_status(statuses: Sequence[str]) -> str:
    worst = "completed"
    for status in statuses:
        candidate = status if status in _EXECUTION_PRECEDENCE else "failed"
        if _EXECUTION_PRECEDENCE.index(candidate) > _EXECUTION_PRECEDENCE.index(worst):
            worst = candidate
    return worst


def _aggregate_engineering_status(statuses: Sequence[str]) -> str:
    """Return the aggregate assertion status over every declared analysis.

    ``pass`` requires every declared analysis to have produced valid evidence.
    A single ``unknown`` child makes the whole claim unknown: an unevaluated
    analysis cannot be reported as a circuit failure.
    """

    if not statuses:
        return "unknown"
    if any(status not in {"pass", "fail"} for status in statuses):
        return "unknown"
    if all(status == "pass" for status in statuses):
        return "pass"
    return "fail"


def resolve_testbench_driver(backend: object) -> Any:
    """Resolve the published-testbench driver identity for one backend."""

    if not isinstance(backend, str):
        return None
    alias = TESTBENCH_DRIVER_ALIASES.get(backend)
    return builtin_driver(alias) if alias else None


def _protocol(request_id: str, backend: str | None) -> dict[str, Any]:
    driver = resolve_testbench_driver(backend)
    return {
        "request_id": request_id,
        "operation_profile": TESTBENCH_SIMULATE_PROFILE,
        "assertion_profile": TESTBENCH_EVIDENCE_ASSERTION,
        "implementation_id": driver.driver_id if driver is not None else None,
        "implementation_version": __version__,
        "backend": backend,
    }


def _empty_data(request_id: str, backend: str | None) -> dict[str, Any]:
    return {
        "protocol": _protocol(request_id, backend),
        "artifact": None,
        "dispatch": {
            "mode": None,
            "simulation_handoff": None,
            "declared_analysis_count": 0,
            "dispatched_analysis_count": 0,
            "completed_analysis_count": 0,
            "passing_analysis_count": 0,
        },
        "configuration": [],
        "analyses": [],
        "extensions": {},
    }


def _invalid(
    *,
    request_id: str,
    backend: str | None,
    code: str,
    message: str,
    hint: str | None = None,
    inputs: Iterable[dict[str, Any]] = (),
    data: dict[str, Any] | None = None,
    execution_status: str = "invalid_request",
) -> dict[str, Any]:
    return result(
        OPERATION_NAME,
        tool=None,
        execution={
            "status": execution_status,
            "exit_code": None,
            "duration_ms": 0,
            "command": [],
        },
        engineering_status="unknown",
        summary="The testbench.simulate request could not be bound to a dispatchable artifact.",
        inputs=list(inputs),
        diagnostics=[diagnostic("error", code, message, hint=hint)],
        data=data if data is not None else _empty_data(request_id, backend),
    )


def _artifact_facts(testbench: SimraTestbench) -> dict[str, Any]:
    return {
        "id": testbench.identifier,
        "label": testbench.label,
        "top": testbench.top,
        "descriptor_path": str(testbench.descriptor_path),
        "netlist_path": str(testbench.netlist_path),
        "view_path": str(testbench.view_path),
        "netlist_sha256": testbench.netlist_sha256,
        "view_sha256": testbench.view_sha256,
        "source_sha256": testbench.source_sha256,
        "digests_verified": True,
        "parameters": testbench.parameters_state,
        "self_contained": testbench.self_contained,
        "saved_nets": list(testbench.saved_nets),
    }


def _child_facts(
    deck: DerivedDeck,
    deck_record: dict[str, Any],
    payload: Mapping[str, Any],
    result_record: dict[str, Any] | None,
) -> dict[str, Any]:
    child_data = payload.get("data")
    child_data = child_data if isinstance(child_data, Mapping) else {}
    analysis = child_data.get("analysis")
    evidence = child_data.get("evidence")
    execution = payload.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    engineering = payload.get("engineering")
    engineering = engineering if isinstance(engineering, Mapping) else {}
    protocol = child_data.get("protocol")
    protocol = protocol if isinstance(protocol, Mapping) else {}
    return {
        "index": deck.index,
        "kind": deck.kind,
        "declared": deepcopy(deck.analysis),
        "deck_path": deck_record.get("path"),
        "deck_sha256": deck_record.get("sha256"),
        "result_path": (result_record or {}).get("path"),
        "result_sha256": (result_record or {}).get("sha256"),
        "operation_profile": protocol.get("operation_profile"),
        "driver_id": protocol.get("driver_id"),
        "execution_status": execution.get("status"),
        "exit_code": execution.get("exit_code"),
        "duration_ms": execution.get("duration_ms"),
        "engineering_status": engineering.get("status"),
        "summary": bounded_text(engineering.get("summary") or "", limit=512),
        "analysis": deepcopy(analysis) if isinstance(analysis, Mapping) else None,
        "evidence": deepcopy(evidence) if isinstance(evidence, Mapping) else None,
    }


def _write_bounded(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def simulate_testbench(
    artifact_file: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    backend: str = "ngspice",
    models_file: str | Path | None = None,
    timeout: float = 120.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch every analysis one published Simra testbench artifact declares."""

    request_id_error: str | None = None
    if request_id is None:
        correlation_id = str(uuid.uuid4())
    else:
        try:
            parsed = uuid.UUID(request_id)
        except (AttributeError, TypeError, ValueError):
            request_id_error = "request_id must be a canonical lowercase UUID."
            correlation_id = str(uuid.uuid4())
        else:
            if str(parsed) != request_id:
                request_id_error = "request_id must be a canonical lowercase UUID."
                correlation_id = str(uuid.uuid4())
            else:
                correlation_id = request_id

    if request_id_error is not None:
        return _invalid(
            request_id=correlation_id,
            backend=backend if backend in SUPPORTED_BACKENDS else None,
            code="testbench.request.invalid",
            message=request_id_error,
        )

    # Two identities are in play: the published-testbench driver that owns this
    # operation's assertion, and the shared circuit.simulate driver whose
    # advertised analysis features constrain what may be dispatched.
    testbench_capability = resolve_testbench_driver(backend)
    driver = builtin_driver(backend) if isinstance(backend, str) else None
    if backend not in SUPPORTED_BACKENDS or driver is None or testbench_capability is None:
        return _invalid(
            request_id=correlation_id,
            backend=None,
            code="testbench.backend.unsupported",
            message=(
                f"Backend {backend!r} is not one of the reviewed simulation backends "
                f"{', '.join(SUPPORTED_BACKENDS)}."
            ),
        )

    try:
        testbench = load_simra_testbench(artifact_file)
    except SimraArtifactError as exc:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
        )

    input_records = list(testbench.input_records)
    configuration: list[dict[str, Any]] = []
    model_prelude: str | None = None
    if models_file is not None:
        try:
            model_prelude, models_record = load_model_prelude(models_file)
        except SimraArtifactError as exc:
            return _invalid(
                request_id=correlation_id,
                backend=backend,
                code=exc.code,
                message=exc.message,
                hint=exc.hint,
                inputs=input_records,
            )
        input_records.append(models_record)
        configuration.append(
            {
                "role": "spice-model-library",
                "path": models_record["path"],
                "sha256": models_record.get("sha256"),
                "bytes": models_record.get("bytes"),
                "identity": "content-digest",
            }
        )

    # Simra clears ``simulation_ready`` for every deck that names a device model,
    # because model collateral is outside its schematic contract. Such a deck is
    # dispatchable only when the caller supplies that collateral as an explicit,
    # digest-bound configuration reference.
    if not testbench.self_contained and model_prelude is None:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code="testbench.models.required",
            message=(
                "The published artifact reports simulation_ready=false: its deck names "
                "device models that Simra does not publish. Supply the model collateral "
                "as an explicit spice-model-library configuration reference."
            ),
            hint=(
                "Pass --models with a self-contained SPICE model-card file whose cards "
                "define every model name the deck references."
            ),
            inputs=input_records,
        )

    try:
        decks = derive_single_analysis_decks(testbench, model_prelude=model_prelude)
    except SimraArtifactError as exc:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
            inputs=input_records,
        )

    if len(decks) > MAX_DISPATCHED_ANALYSES:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code="testbench.analyses.over_limit",
            message=(
                f"{len(decks)} derived decks exceed the bounded ceiling of "
                f"{MAX_DISPATCHED_ANALYSES}."
            ),
            inputs=input_records,
        )

    unsupported = [
        deck.kind
        for deck in decks
        if analysis_feature(deck.kind) not in driver.features
    ]
    if unsupported:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code="testbench.analysis.unsupported",
            message=(
                f"Driver {driver.driver_id} does not advertise the "
                f"{', '.join(sorted(set(unsupported)))} analysis feature(s) this "
                "testbench declares."
            ),
            hint="Select a backend whose capability covers every declared analysis.",
            inputs=input_records,
        )

    destination = Path(output_dir).expanduser().resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        deck_directory = destination / "decks"
        deck_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _invalid(
            request_id=correlation_id,
            backend=backend,
            code="testbench.destination.unusable",
            message=f"The evidence destination could not be prepared: {exc}",
            inputs=input_records,
            execution_status="failed",
        )

    artifacts: list[dict[str, Any]] = []
    analyses_facts: list[dict[str, Any]] = []
    execution_statuses: list[str] = []
    engineering_statuses: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    total_duration = 0
    selected_tool: dict[str, Any] | None = None

    for deck in decks:
        name = deck_file_name(deck, total=len(decks))
        deck_path = deck_directory / name
        try:
            _write_bounded(deck_path, deck.text)
        except OSError as exc:
            return _invalid(
                request_id=correlation_id,
                backend=backend,
                code="testbench.destination.unusable",
                message=f"A derived deck could not be written: {exc}",
                inputs=input_records,
                execution_status="failed",
            )
        try:
            deck_record = file_record(
                deck_path,
                kind="spice-netlist",
                role="testbench.analysis.deck",
            )
        except FileRecordError as exc:
            return _invalid(
                request_id=correlation_id,
                backend=backend,
                code="testbench.destination.unusable",
                message=f"A derived deck could not be content-bound: {exc}",
                inputs=input_records,
                execution_status="failed",
            )
        if deck_record.get("sha256") != deck.sha256:
            return _invalid(
                request_id=correlation_id,
                backend=backend,
                code="testbench.deck.unstable",
                message=(
                    f"The derived deck {deck_path} does not hash to its derived digest."
                ),
                inputs=input_records,
                execution_status="failed",
            )
        artifacts.append(deck_record)

        payload = simulate_circuit_profile(
            deck_path,
            destination / f"{Path(name).stem}",
            backend=backend,
            discovery=discovery,
            workdir=deck_directory,
            timeout=timeout,
        )

        child_path = destination / f"{Path(name).stem}.result.json"
        child_record: dict[str, Any] | None = None
        try:
            _write_bounded(
                child_path,
                json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
            )
            child_record = file_record(
                child_path,
                kind="openada-result",
                role="testbench.analysis.result",
                maximum_bytes=MAX_CHILD_RESULT_BYTES,
            )
            artifacts.append(child_record)
        except (OSError, FileRecordError, ValueError) as exc:
            diagnostics.append(
                diagnostic(
                    "error",
                    "testbench.analysis.result_unretained",
                    f"The {deck.kind} analysis result could not be retained: {exc}",
                )
            )

        for entry in payload.get("artifacts") or ():
            if isinstance(entry, Mapping):
                artifacts.append(dict(entry))

        execution = payload.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        engineering = payload.get("engineering")
        engineering = engineering if isinstance(engineering, Mapping) else {}
        execution_status = execution.get("status")
        engineering_status = engineering.get("status")
        execution_statuses.append(
            execution_status if isinstance(execution_status, str) else "failed"
        )
        engineering_statuses.append(
            engineering_status if isinstance(engineering_status, str) else "unknown"
        )
        duration = execution.get("duration_ms")
        if isinstance(duration, int) and duration >= 0:
            total_duration += duration
        if selected_tool is None and isinstance(payload.get("tool"), Mapping):
            selected_tool = dict(payload["tool"])

        for entry in payload.get("diagnostics") or ():
            if not isinstance(entry, Mapping):
                continue
            forwarded = dict(entry)
            forwarded["message"] = bounded_text(
                f"[{deck.kind} analysis {deck.index + 1}] {forwarded.get('message', '')}"
            )
            diagnostics.append(forwarded)

        analyses_facts.append(
            _child_facts(deck, deck_record, payload, child_record)
        )

    execution_status = _aggregate_execution_status(execution_statuses)
    engineering_status = _aggregate_engineering_status(engineering_statuses)
    passing = sum(1 for status in engineering_statuses if status == "pass")
    completed = sum(1 for status in execution_statuses if status == "completed")

    data = {
        "protocol": _protocol(correlation_id, backend),
        "artifact": _artifact_facts(testbench),
        "dispatch": {
            "mode": testbench.dispatch_mode,
            "simulation_handoff": testbench.simulation_handoff,
            "declared_analysis_count": len(testbench.analyses),
            "dispatched_analysis_count": len(decks),
            "completed_analysis_count": completed,
            "passing_analysis_count": passing,
        },
        "configuration": configuration,
        "analyses": analyses_facts,
        "extensions": {},
    }

    if engineering_status == "pass":
        summary = (
            f"All {len(decks)} declared analysis/analyses "
            f"({', '.join(deck.kind for deck in decks)}) of digest-bound testbench "
            f"{testbench.identifier!r} produced valid {backend} evidence."
        )
    elif engineering_status == "fail":
        summary = (
            f"{passing} of {len(decks)} declared analyses of digest-bound testbench "
            f"{testbench.identifier!r} produced valid {backend} evidence; at least one "
            "analysis failed natively."
        )
    else:
        summary = (
            f"The evidence for at least one of {len(decks)} declared analyses of "
            f"testbench {testbench.identifier!r} is insufficient to decide the assertion."
        )

    if testbench.simulation_handoff == "split_required":
        diagnostics.insert(
            0,
            diagnostic(
                "info",
                "testbench.handoff.split",
                (
                    f"The published artifact declares {len(decks)} analyses and reports "
                    "simulation_handoff=split_required; one single-analysis deck was "
                    "derived per declared analysis and dispatched separately."
                ),
            ),
        )

    return result(
        OPERATION_NAME,
        tool=selected_tool or tool_record(driver.native_tool),
        execution={
            "status": execution_status,
            "exit_code": 0 if execution_status == "completed" else None,
            "duration_ms": total_duration,
            "command": [],
        },
        engineering_status=engineering_status,
        summary=summary,
        inputs=input_records,
        artifacts=artifacts,
        diagnostics=diagnostics,
        data=data,
    )


__all__ = [
    "MAX_DISPATCHED_ANALYSES",
    "SUPPORTED_BACKENDS",
    "TESTBENCH_EVIDENCE_ASSERTION",
    "TESTBENCH_SIMULATE_PROFILE",
    "simulate_testbench",
    "resolve_testbench_driver",
]
