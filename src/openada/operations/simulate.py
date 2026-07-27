"""The one simulation semantic: ``circuit.simulate``.

Simulating a circuit is a single engineering act. What differs between callers
is never the *meaning* of that act - it is the glue: whether the thing to
simulate arrived as a bare deck or as a published Simra artifact, whether the
device models come from a flattened card file or from an installed PDK, and
whether the testbench declares one analysis or several. OpenADA exists to make
that glue the driver's problem, so all of it lives behind one operation.

That operation is ``openada.operation/circuit.simulate/v1alpha2``. Its published
contract already says so: its ``locator_types`` include ``artifact`` and its
``configuration_roles`` already include ``pdk`` and ``corner``. This module is
the implementation catching up with the contract.

**One result shape.** Every simulation - deck or artifact, model-free or
``--models`` or ``--pdk``, one analysis or sixteen - returns exactly one
``circuit.simulate/v1alpha2`` envelope. A PDK-bound run used to return a raw
native ngspice payload with no ``analysis`` or ``evidence`` block at all; it now
returns the same reviewed evidence as everything else.

**A split testbench is N requests, not a second operation.** When a published
artifact declares several analyses the driver derives one single-analysis deck
per declaration and runs them all, writing one complete
``circuit.simulate/v1alpha2`` envelope per analysis. The envelope returned to
the caller is the *weakest* of those, so no aggregate claim can be stronger than
its worst member, and ``data.extensions["org.openada.simulation-dispatch"]``
names every analysis and where its own result was retained.

``execution.status`` ("the tool ran") and ``engineering.status`` ("the circuit
passes") stay strictly separate throughout. Nothing here promotes one from the
other.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import uuid

from ..contract import (
    FileRecordError,
    bounded_text,
    diagnostic,
    file_record,
    result,
    static_execution,
)
from ..discovery import DiscoveryManager
from ..driver_registry import (
    CIRCUIT_SIMULATE_PROFILE,
    SIMULATION_EVIDENCE_ASSERTION,
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
from ..engines.spice import MAX_SOURCE_BYTES, NgspiceDriver, NgspiceOutput
from ..pdk_bindings import (
    PdkBindingError,
    ResolvedPdkBinding,
    available_pdk_ids,
    bind_deck,
    resolve_pdk_binding,
    simulatable_pdk_ids,
)
from ..pdk_collateral import blocking, inspect_deck_collateral
from .circuit_simulate import (
    decorate_circuit_simulation_result,
    inspect_simulation_deck,
    simulate_circuit_profile,
)


#: The envelope operation name. There is exactly one, and it is the same one a
#: bare `openada simulate deck.spice` has always reported.
OPERATION_NAME = "simulate"

SUPPORTED_BACKENDS = ("ngspice", "xyce")
MAX_DISPATCHED_ANALYSES = 16
MAX_CHILD_RESULT_BYTES = 8 * 1024 * 1024
#: The published profile's ``evidence.limits.max_artifact_count``.
MAX_RETAINED_ARTIFACTS = 32

#: Extension keys. The result data schema requires dotted lowercase names with
#: no underscores, which is why the binding facts are ``pdk-binding`` and not
#: the ``pdk_binding`` the retired operation used.
PDK_BINDING_EXTENSION = "org.openada.pdk-binding"
TARGET_EXTENSION = "org.openada.simulation-target"
DISPATCH_EXTENSION = "org.openada.simulation-dispatch"

#: Worst-wins precedence. A dispatch may never report a stronger execution
#: status than its weakest analysis.
_EXECUTION_PRECEDENCE = (
    "completed",
    "timed_out",
    "not_available",
    "failed",
    "invalid_request",
)
_ENGINEERING_PRECEDENCE = ("pass", "not_applicable", "fail", "unknown")

#: How a bound PDK file is reported as a configuration reference.
_PDK_CONFIGURATION_ROLES = {
    "pdk.corner-library": "spice-model-library",
    "pdk.osdi-module": "simulator-configuration",
    "pdk.identity": "pdk",
}

#: Detecting a published Simra artifact descriptor must never read an unbounded
#: file; a descriptor is small and its schema token is near the top.
MAX_TARGET_PROBE_BYTES = 4 * 1024 * 1024
_SIMRA_ARTIFACT_SCHEMA_PREFIX = "simra.schematic-artifact/"


class SimulationRequestError(Exception):
    """One bounded, typed reason a simulation request cannot be honoured."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SimulationTarget:
    """What the caller asked to simulate, after the driver worked out which."""

    kind: str  # "deck" | "simra-artifact"
    path: Path
    testbench: SimraTestbench | None = None


def classify_target(path: str | Path) -> SimulationTarget:
    """Return whether a target is a bare deck or a published Simra artifact.

    The caller states an engineering need - "simulate this" - and never which
    of OpenADA's internal paths should honour it. Detection is by content, not
    by file name, because a descriptor may be published under any name.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise SimulationRequestError(
            "input.missing",
            f"File not found: {source}",
        )
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise SimulationRequestError(
            "input.missing", f"{source} could not be inspected: {exc}"
        ) from exc
    if size <= MAX_TARGET_PROBE_BYTES:
        try:
            head = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            head = ""
        stripped = head.lstrip()
        if stripped.startswith("{"):
            try:
                document = json.loads(head)
            except ValueError:
                document = None
            if isinstance(document, Mapping):
                schema = document.get("schema")
                if isinstance(schema, str) and schema.startswith(
                    _SIMRA_ARTIFACT_SCHEMA_PREFIX
                ):
                    return SimulationTarget("simra-artifact", source)
    return SimulationTarget("deck", source)


def _protocol(request_id: str, backend: str | None) -> dict[str, Any]:
    driver = builtin_driver(backend) if isinstance(backend, str) else None
    return {
        "request_id": request_id,
        "operation_profile": CIRCUIT_SIMULATE_PROFILE,
        "assertion_profile": SIMULATION_EVIDENCE_ASSERTION,
        "driver_id": driver.driver_id if driver is not None else None,
        "driver_version": driver.version if driver is not None else None,
    }


def _unproven_data(
    request_id: str,
    backend: str | None,
    *,
    limitation: str,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    driver = builtin_driver(backend) if isinstance(backend, str) else None
    payload_extensions: dict[str, Any] = {
        "org.openada": {
            "backend": driver.alias if driver is not None else None,
            "parameters": None,
            "native_data": {},
            "native_diagnostics": [],
        }
    }
    payload_extensions.update(dict(extensions or {}))
    return {
        "protocol": _protocol(request_id, backend),
        "analysis": {
            "type": None,
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
            "provenance_limitations": [limitation],
            "extensions": {},
        },
        "extensions": payload_extensions,
    }


def _refusal(
    *,
    request_id: str,
    backend: str | None,
    code: str,
    message: str,
    hint: str | None = None,
    inputs: Iterable[dict[str, Any]] = (),
    execution_status: str = "invalid_request",
    extensions: Mapping[str, Any] | None = None,
    extra_diagnostics: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Return a typed refusal in the one simulation result shape.

    A refusal is never silent and never runs a simulator: ``execution.status``
    records that nothing ran, and ``engineering.status`` stays ``unknown``
    because an unrun analysis says nothing about the circuit.
    """

    return result(
        OPERATION_NAME,
        tool=None,
        execution=static_execution(execution_status),
        engineering_status="unknown",
        summary="The circuit.simulate request was refused before any simulator ran.",
        inputs=list(inputs),
        diagnostics=[diagnostic("error", code, message, hint=hint), *extra_diagnostics],
        data=_unproven_data(
            request_id,
            backend,
            limitation=(
                "No native simulation was launched because the request was "
                "refused, so request binding and evidence provenance were not "
                "established."
            ),
            extensions=extensions,
        ),
    )


def _correlation_id(request_id: str | None) -> tuple[str, str | None]:
    if request_id is None:
        return str(uuid.uuid4()), None
    try:
        parsed = uuid.UUID(request_id)
    except (AttributeError, TypeError, ValueError):
        return str(uuid.uuid4()), "request_id must be a canonical lowercase UUID."
    if str(parsed) != request_id:
        return str(uuid.uuid4()), "request_id must be a canonical lowercase UUID."
    return request_id, None


#: Stamped onto any run whose technology binding the caller took over.
UNMANAGED_COLLATERAL_LIMITATION = (
    "The caller asserted ownership of the technology binding with "
    "--unmanaged-collateral. The device models, corner, geometry unit "
    "convention and any Verilog-A preload this run used were chosen by the "
    "deck, not by a reviewed OpenADA PDK profile, and OpenADA cannot state "
    "which technology the evidence describes."
)


def _collateral_diagnostics(
    deck_text: str,
    *,
    workdir: Path | None,
    bound_pdk: str | None,
    unmanaged: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(blocking, advisory)`` diagnostics for a deck's own collateral.

    ``unmanaged`` demotes every refusal to a warning. It is reachable only from
    an explicit ``--unmanaged-collateral``, and the run it permits is stamped
    with :data:`UNMANAGED_COLLATERAL_LIMITATION`. Capability was never the
    problem with the deck that applied IHP's preload to sky130; silence was.
    """

    findings = inspect_deck_collateral(
        deck_text, workdir=workdir, bound_pdk=bound_pdk
    )
    if unmanaged:
        return [], [
            diagnostic(
                "warning" if finding.severity == "error" else finding.severity,
                finding.code,
                finding.message,
                hint=finding.hint,
            )
            for finding in findings
        ]
    errors = [
        diagnostic("error", finding.code, finding.message, hint=finding.hint)
        for finding in blocking(findings)
    ]
    advisories = [
        diagnostic(finding.severity, finding.code, finding.message, hint=finding.hint)
        for finding in findings
        if finding.severity != "error"
    ]
    return errors, advisories


#: One DC source card, for reading the bias a deck asks for. Only the top-level
#: form matters; a source inside a subcircuit is not the supply.
_DC_SOURCE_RE = re.compile(
    r"^\s*(?P<name>[Vv][^\s]*)(?:[ \t]+[^\s]+){2}[ \t]+"
    r"(?:DC[ \t]+)?(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*)",
    re.IGNORECASE,
)
#: How far a deck's supply may sit from the device family's nominal before the
#: driver says so. Twenty per cent is generous: it passes a deliberate +/-10 %
#: supply-tolerance study and catches a deck written for a different node.
_SUPPLY_TOLERANCE = 0.20


def _largest_dc_source_v(deck_text: str) -> float | None:
    """Return the largest DC source magnitude a deck declares, if any."""

    from ..pdk_bindings import parse_spice_number, PdkBindingError

    largest: float | None = None
    for line in deck_text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        match = _DC_SOURCE_RE.match(line)
        if match is None:
            continue
        try:
            value = abs(float(parse_spice_number(match.group("value"))))
        except (PdkBindingError, ArithmeticError, ValueError):
            continue
        if largest is None or value > largest:
            largest = value
    return largest


def _binding_advisories(
    facts: Mapping[str, Any],
    *,
    deck_text: str,
    corner: str,
    default_corner: str,
) -> list[dict[str, Any]]:
    """Turn the binding's PDK facts into things the caller can act on.

    A role-based deck deliberately says nothing about the technology. That is
    the point - but it means several PDK facts that change the *answer* are
    invisible from the deck alone. None of these refuses a run; each states a
    fact the caller cannot otherwise see.
    """

    notes: list[dict[str, Any]] = []
    pdk_id = facts.get("pdk_id")

    dropped = list(facts.get("dropped_parameters") or ())
    if dropped:
        notes.append(
            diagnostic(
                "warning",
                "pdk.parameter.dropped",
                (
                    f"{pdk_id} does not accept the instance parameter(s) "
                    f"{', '.join(dropped)}; they were not emitted. On a PDK that "
                    "ships subcircuits ngspice would have ignored them without a "
                    "word, so the intent would simply have vanished."
                ),
                hint=(
                    "Parameters this PDK accepts: "
                    f"{', '.join(sorted(facts.get('parameter_names') or ()))}."
                ),
            )
        )

    supplies = facts.get("nominal_supply_v") or {}
    declared = _largest_dc_source_v(deck_text)
    if supplies and declared is not None and declared > 0:
        nominal = max(float(value) for value in supplies.values())
        if nominal > 0 and abs(declared - nominal) / nominal > _SUPPLY_TOLERANCE:
            notes.append(
                diagnostic(
                    "warning",
                    "pdk.bias.off_nominal",
                    (
                        f"The deck's largest DC source is {declared:g} V, and the "
                        f"{pdk_id} device family it was bound to is characterised "
                        f"for {nominal:g} V ({', '.join(f'{r}={v} V' for r, v in sorted(supplies.items()))}). "
                        "A canonical role names a device, not a bias, so the same "
                        "deck is a different operating condition on every "
                        "technology. The run is valid; what it describes may not "
                        "be the intended circuit."
                    ),
                    hint=(
                        "Scale the deck's supplies to the technology, or choose a "
                        "PDK whose device family matches the intended supply."
                    ),
                )
            )

    skewed = facts.get("corner_skewed_roles")
    roles = list(facts.get("roles_bound") or ())
    if skewed is not None and corner != default_corner:
        unskewed = [role for role in roles if role not in set(skewed)]
        if unskewed:
            notes.append(
                diagnostic(
                    "warning",
                    "pdk.corner.partial",
                    (
                        f"The {pdk_id} corner {corner!r} does not skew "
                        f"{', '.join(unskewed)}: its corner sections select skewed "
                        f"{', '.join(skewed)} devices and typical ones for the "
                        "rest. This deck therefore mixes corners."
                    ),
                    hint=f"Only {', '.join(skewed)} follow a corner on this PDK.",
                )
            )

    if (
        facts.get("junction_geometry") == "zero"
        and roles
        and not facts.get("junction_parameters_supplied")
    ):
        notes.append(
            diagnostic(
                "info",
                "pdk.junction_geometry.absent",
                (
                    f"{pdk_id} treats omitted ad/as/pd/ps as zero, and the deck "
                    "supplied none, so drain and source junction capacitance is "
                    "absent from this answer. Operating points are unaffected; "
                    "transient delays, AC poles and switching energy are not. "
                    "Other PDKs compute this geometry from w and the finger "
                    "count, so the same deck is not equally complete on all of "
                    "them."
                ),
            )
        )

    tnom = facts.get("model_tnom_c")
    temperature = facts.get("simulation_temperature_c")
    if tnom is not None and temperature is not None and str(tnom) != str(temperature):
        notes.append(
            diagnostic(
                "info",
                "pdk.temperature.off_reference",
                (
                    f"The deck states .option temp={temperature} C and {pdk_id}'s "
                    f"model cards are extracted at tnom={tnom} C, so the models "
                    "are being extrapolated. The distance is small; it is stated "
                    "because it differs between PDKs and is otherwise invisible."
                ),
            )
        )

    return notes


def _target_facts(target: SimulationTarget) -> dict[str, Any]:
    facts: dict[str, Any] = {"kind": target.kind, "path": str(target.path)}
    testbench = target.testbench
    if testbench is not None:
        facts.update(
            {
                "artifact_id": testbench.identifier,
                "label": testbench.label,
                "top": testbench.top,
                "netlist_path": str(testbench.netlist_path),
                "netlist_sha256": testbench.netlist_sha256,
                "view_sha256": testbench.view_sha256,
                "source_sha256": testbench.source_sha256,
                "digests_verified": True,
                "parameters": testbench.parameters_state,
                "self_contained": testbench.self_contained,
                "simulation_handoff": testbench.simulation_handoff,
                "declared_analysis_count": len(testbench.analyses),
                "saved_nets": list(testbench.saved_nets),
            }
        )
    return facts


def _weakest(payloads: Sequence[Mapping[str, Any]]) -> int:
    """Return the index of the analysis whose outcome is weakest.

    Deterministic: execution precedence first (a tool that did not run is worse
    than one that ran), then engineering precedence, then declaration order.
    """

    def rank(index: int) -> tuple[int, int, int]:
        payload = payloads[index]
        execution = payload.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        engineering = payload.get("engineering")
        engineering = engineering if isinstance(engineering, Mapping) else {}
        execution_status = execution.get("status")
        engineering_status = engineering.get("status")
        execution_rank = (
            _EXECUTION_PRECEDENCE.index(execution_status)
            if execution_status in _EXECUTION_PRECEDENCE
            else len(_EXECUTION_PRECEDENCE)
        )
        engineering_rank = (
            _ENGINEERING_PRECEDENCE.index(engineering_status)
            if engineering_status in _ENGINEERING_PRECEDENCE
            else len(_ENGINEERING_PRECEDENCE)
        )
        return (-execution_rank, -engineering_rank, index)

    return min(range(len(payloads)), key=rank)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _run_bound_deck(
    deck_path: Path,
    output_dir: Path,
    *,
    discovery: DiscoveryManager,
    workdir: Path,
    resolved_pdk: ResolvedPdkBinding,
    raw_name: str,
    timeout: float,
    request_id: str,
) -> dict[str, Any]:
    """Run one PDK-bound deck and return the reviewed evidence envelope.

    A bound deck carries includes and a control block, so it runs through the
    native control-mode path rather than the model-free shared bridge. The
    result is then decorated into the *same* ``circuit.simulate/v1alpha2``
    evidence block every other simulation produces: the model source changes
    what the simulator is told, never what the evidence means.
    """

    payload = NgspiceDriver(discovery=discovery).simulate(
        deck_path,
        output_dir,
        workdir=workdir,
        execution_mode="control",
        expected_outputs=[NgspiceOutput(kind="raw", path=raw_name)],
        environment_overrides={
            "PDK_ROOT": str(resolved_pdk.root.parent),
            "PDK": resolved_pdk.pdk_id,
        },
        timeout=timeout,
    )
    deck = inspect_simulation_deck(deck_path)
    parameters = deck.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else None
    return decorate_circuit_simulation_result(
        payload,
        driver=builtin_driver("ngspice"),
        request_id=request_id,
        deck=deck,
        parameters=parameters,
        provenance_limitations=[
            "The top-level bound deck, the selected native executable and every "
            f"file the {resolved_pdk.pdk_id} binding profile names were "
            "content-bound; the model library's own transitive includes, host "
            "runtime libraries and simulator defaults remain bounded provenance."
        ],
    )


def _resolve_model_source(
    *,
    correlation_id: str,
    backend: str,
    models_file: str | Path | None,
    pdk: str | None,
    pdk_root: str | Path | None,
    corner: str | None,
    inputs: list[dict[str, Any]],
    configuration: list[dict[str, Any]],
) -> tuple[ResolvedPdkBinding | None, str | None]:
    """Resolve at most one model source, or raise a typed refusal."""

    if pdk is not None and models_file is not None:
        raise SimulationRequestError(
            "simulation.models.ambiguous",
            "Both a PDK binding and a self-contained model-card file were "
            "supplied; exactly one model source may bind a deck.",
            hint="Pass --pdk for an installed PDK, or --models for flattened cards.",
        )
    if corner is not None and pdk is None:
        raise SimulationRequestError(
            "pdk.corner.unbound",
            "A corner may only be selected together with --pdk.",
            hint=f"Installed PDKs that bind a corner: {', '.join(simulatable_pdk_ids())}.",
        )

    resolved: ResolvedPdkBinding | None = None
    if pdk is not None:
        if backend != "ngspice":
            raise SimulationRequestError(
                "pdk.backend.unsupported",
                f"PDK binding is implemented for ngspice only; backend {backend!r} "
                "has no reviewed binding profile.",
                hint="Rerun with --backend ngspice, or supply --models instead.",
            )
        if pdk_root is None:
            raise SimulationRequestError(
                "pdk.root.required",
                f"Binding the PDK {pdk!r} requires an explicit --pdk-root.",
                hint="Pass the directory containing the installed PDK tree.",
            )
        try:
            resolved = resolve_pdk_binding(pdk, pdk_root, corner=corner)
        except PdkBindingError as exc:
            raise SimulationRequestError(exc.code, exc.message, hint=exc.hint) from exc
        inputs.extend(resolved.input_records)
        for record in resolved.input_records:
            configuration.append(
                {
                    "role": _PDK_CONFIGURATION_ROLES[record["role"]],
                    "path": record.get("path"),
                    "sha256": record.get("sha256"),
                    "bytes": record.get("bytes"),
                    "identity": "content-digest",
                }
            )

    model_prelude: str | None = None
    if models_file is not None:
        try:
            model_prelude, models_record = load_model_prelude(models_file)
        except SimraArtifactError as exc:
            raise SimulationRequestError(exc.code, exc.message, hint=exc.hint) from exc
        inputs.append(models_record)
        configuration.append(
            {
                "role": "spice-model-library",
                "path": models_record["path"],
                "sha256": models_record.get("sha256"),
                "bytes": models_record.get("bytes"),
                "identity": "content-digest",
            }
        )
    return resolved, model_prelude


def simulate(
    target: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    backend: str = "ngspice",
    models_file: str | Path | None = None,
    pdk: str | None = None,
    pdk_root: str | Path | None = None,
    corner: str | None = None,
    parameters: Mapping[str, object] | None = None,
    workdir: str | Path | None = None,
    # A PDK binding pays the model library's parse cost on every derived deck;
    # sky130A's tt section alone takes ~95 s. See cli.py's --timeout default.
    timeout: float = 600.0,
    request_id: str | None = None,
    unmanaged_collateral: bool = False,
    extra_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run one circuit simulation, whatever shape the request arrived in.

    ``target`` is either a SPICE deck or a published Simra schematic artifact
    descriptor; the driver decides which by reading it. The model source is at
    most one of ``models_file`` or ``pdk``. Every path returns exactly one
    ``circuit.simulate/v1alpha2`` envelope.
    """

    correlation_id, request_id_error = _correlation_id(request_id)
    forwarded = [dict(entry) for entry in extra_diagnostics]
    normalized_backend = backend if backend in SUPPORTED_BACKENDS else None

    def refuse(
        code: str,
        message: str,
        *,
        hint: str | None = None,
        inputs: Iterable[dict[str, Any]] = (),
        execution_status: str = "invalid_request",
        extensions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _refusal(
            request_id=correlation_id,
            backend=normalized_backend,
            code=code,
            message=message,
            hint=hint,
            inputs=inputs,
            execution_status=execution_status,
            extensions=extensions,
            extra_diagnostics=forwarded,
        )

    if request_id_error is not None:
        return refuse("simulation.request.invalid", request_id_error)

    driver = builtin_driver(backend) if isinstance(backend, str) else None
    if normalized_backend is None or driver is None:
        return refuse(
            "simulation.backend.unsupported",
            f"Backend {backend!r} is not one of the reviewed simulation backends "
            f"{', '.join(SUPPORTED_BACKENDS)}.",
        )

    try:
        selected = classify_target(target)
    except SimulationRequestError as exc:
        return refuse(exc.code, exc.message, hint=exc.hint)

    input_records: list[dict[str, Any]] = []
    configuration: list[dict[str, Any]] = []
    try:
        resolved_pdk, model_prelude = _resolve_model_source(
            correlation_id=correlation_id,
            backend=normalized_backend,
            models_file=models_file,
            pdk=pdk,
            pdk_root=pdk_root,
            corner=corner,
            inputs=input_records,
            configuration=configuration,
        )
    except SimulationRequestError as exc:
        return refuse(
            exc.code,
            exc.message,
            hint=exc.hint,
            extensions={TARGET_EXTENSION: _target_facts(selected)},
        )

    if selected.kind == "simra-artifact":
        try:
            testbench = load_simra_testbench(selected.path)
        except SimraArtifactError as exc:
            return refuse(
                exc.code,
                exc.message,
                hint=exc.hint,
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )
        selected = replace(selected, testbench=testbench)
        input_records = list(testbench.input_records) + input_records
        if not testbench.self_contained and model_prelude is None and resolved_pdk is None:
            return refuse(
                "simulation.models.required",
                "The published artifact reports simulation_ready=false: its deck "
                "names device models that Simra does not publish. Supply the model "
                "collateral as an explicit, digest-bound configuration reference.",
                hint=(
                    "Pass --pdk with --pdk-root to bind an installed PDK, or --models "
                    "with a self-contained SPICE model-card file. Known PDKs: "
                    f"{', '.join(available_pdk_ids())}."
                ),
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )
        try:
            decks = derive_single_analysis_decks(testbench, model_prelude=model_prelude)
        except SimraArtifactError as exc:
            return refuse(
                exc.code,
                exc.message,
                hint=exc.hint,
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )
        saved_nets = testbench.saved_nets
    else:
        try:
            deck_text = selected.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return refuse(
                "input.missing",
                f"{selected.path} could not be read: {exc}",
                inputs=input_records,
            )
        if selected.path.stat().st_size > MAX_SOURCE_BYTES:
            return refuse(
                "simulation.evidence.over_limit",
                f"The top-level SPICE input must not exceed {MAX_SOURCE_BYTES} bytes.",
                inputs=input_records,
            )
        try:
            input_records.insert(
                0,
                file_record(
                    selected.path,
                    kind="spice-netlist",
                    role="input",
                    maximum_bytes=MAX_SOURCE_BYTES,
                ),
            )
        except FileRecordError as exc:
            return refuse(
                "simulation.request.invalid",
                f"{selected.path} could not be content-bound: {exc}",
                inputs=input_records,
            )
        if model_prelude is not None:
            # A model-card file is composed after the title line, which is the
            # one line SPICE never reads as a directive.
            lines = deck_text.splitlines(keepends=True)
            title = lines[0] if lines else "* openada\n"
            if not title.endswith("\n"):
                title += "\n"
            deck_text = title + model_prelude + "".join(lines[1:])
        decks = (
            DerivedDeck(
                index=0,
                kind=str(
                    (inspect_simulation_deck(selected.path).get("analyses") or ["deck"])[0]
                ),
                analysis={},
                text=deck_text,
                sha256=hashlib.sha256(deck_text.encode("utf-8")).hexdigest(),
            ),
        )
        saved_nets = ()

    target_facts = _target_facts(selected)

    if len(decks) > MAX_DISPATCHED_ANALYSES:
        return refuse(
            "simulation.analyses.over_limit",
            f"{len(decks)} derived decks exceed the bounded ceiling of "
            f"{MAX_DISPATCHED_ANALYSES}.",
            inputs=input_records,
            extensions={TARGET_EXTENSION: target_facts},
        )

    unsupported = sorted(
        {
            deck.kind
            for deck in decks
            if selected.kind == "simra-artifact"
            and analysis_feature(deck.kind) not in driver.features
        }
    )
    if unsupported:
        return refuse(
            "simulation.analysis.unsupported",
            f"Driver {driver.driver_id} does not advertise the "
            f"{', '.join(unsupported)} analysis feature(s) this testbench declares.",
            hint="Select a backend whose capability covers every declared analysis.",
            inputs=input_records,
            extensions={TARGET_EXTENSION: target_facts},
        )

    destination = Path(output_dir).expanduser().resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        deck_directory = destination / "decks"
        deck_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return refuse(
            "simulation.destination.unusable",
            f"The evidence destination could not be prepared: {exc}",
            inputs=input_records,
            execution_status="failed",
            extensions={TARGET_EXTENSION: target_facts},
        )

    if workdir is not None:
        run_directory = Path(workdir).expanduser().resolve()
    elif selected.kind == "deck":
        # A bare deck resolves its own relative paths against its own directory,
        # exactly as the native interface has always done.
        run_directory = selected.path.parent
    else:
        run_directory = deck_directory

    # The collateral check runs on what the caller actually wrote, before any
    # binding, because the whole point is to catch a deck that reaches into a
    # PDK by hand.
    advisories: list[dict[str, Any]] = []
    for deck in decks:
        errors, notes = _collateral_diagnostics(
            deck.text,
            workdir=run_directory if selected.kind == "deck" else None,
            bound_pdk=resolved_pdk.pdk_id if resolved_pdk is not None else None,
            unmanaged=unmanaged_collateral,
        )
        advisories.extend(notes)
        if errors:
            head = errors[0]
            return refuse(
                str(head["code"]),
                str(head["message"]),
                hint=head.get("hint"),
                inputs=input_records,
                extensions={TARGET_EXTENSION: target_facts},
            )

    artifacts: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    roster: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = list(forwarded) + advisories
    pdk_facts: dict[str, Any] | None = None
    total_duration = 0

    for deck in decks:
        name = (
            deck_file_name(deck, total=len(decks))
            if selected.kind == "simra-artifact"
            else f"{selected.path.stem}.spice"
        )
        stem = Path(name).stem
        raw_name: str | None = None
        if resolved_pdk is not None:
            raw_name = f"{stem}.raw"
            try:
                bound_text, facts = bind_deck(
                    deck.text,
                    resolved_pdk,
                    raw_name=raw_name,
                    saved_nets=saved_nets,
                )
            except PdkBindingError as exc:
                return refuse(
                    exc.code,
                    exc.message,
                    hint=exc.hint,
                    inputs=input_records,
                    extensions={TARGET_EXTENSION: target_facts},
                )
            if pdk_facts is None:
                pdk_facts = {
                    key: value for key, value in facts.items() if key != "raw_output"
                }
                pdk_facts["raw_outputs"] = []
            pdk_facts["raw_outputs"].append(raw_name)
            deck = replace(
                deck,
                text=bound_text,
                sha256=hashlib.sha256(bound_text.encode("utf-8")).hexdigest(),
            )

        # A deck the driver did not derive is run where it lives. Only a deck
        # OpenADA composed - split from an artifact, given a model prelude, or
        # bound to a PDK - is written into the evidence directory, so that what
        # ran is exactly what was retained.
        derived = (
            selected.kind == "simra-artifact"
            or resolved_pdk is not None
            or model_prelude is not None
        )
        if not derived:
            deck_path = selected.path
            deck_record = dict(input_records[0])
            deck_record["role"] = "simulation.deck"
        else:
            deck_path = deck_directory / name
            try:
                deck_path.write_text(deck.text, encoding="utf-8")
                deck_record = file_record(
                    deck_path, kind="spice-netlist", role="simulation.deck"
                )
            except (OSError, FileRecordError) as exc:
                return refuse(
                    "simulation.destination.unusable",
                    f"A derived deck could not be retained: {exc}",
                    inputs=input_records,
                    execution_status="failed",
                    extensions={TARGET_EXTENSION: target_facts},
                )
            if deck_record.get("sha256") != deck.sha256:
                return refuse(
                    "simulation.deck.unstable",
                    f"The derived deck {deck_path} does not hash to its derived digest.",
                    inputs=input_records,
                    execution_status="failed",
                    extensions={TARGET_EXTENSION: target_facts},
                )

        if resolved_pdk is not None:
            assert raw_name is not None
            payload = _run_bound_deck(
                deck_path,
                destination / stem,
                discovery=discovery,
                workdir=deck_directory,
                resolved_pdk=resolved_pdk,
                raw_name=raw_name,
                timeout=timeout,
                request_id=correlation_id,
            )
        else:
            payload = simulate_circuit_profile(
                deck_path,
                destination / stem,
                backend=normalized_backend,
                discovery=discovery,
                workdir=run_directory if selected.kind == "deck" else deck_directory,
                timeout=timeout,
                request_id=correlation_id,
                parameters=parameters,
            )
        payloads.append(payload)

        child_record: dict[str, Any] | None = None
        if len(decks) > 1:
            child_path = destination / f"{stem}.result.json"
            try:
                _write_json(child_path, payload)
                child_record = file_record(
                    child_path,
                    kind="openada-result",
                    role="simulation.analysis-result",
                    maximum_bytes=MAX_CHILD_RESULT_BYTES,
                )
                artifacts.append(child_record)
            except (OSError, FileRecordError, ValueError) as exc:
                diagnostics.append(
                    diagnostic(
                        "error",
                        "simulation.result.missing",
                        f"The {deck.kind} analysis result could not be retained: {exc}",
                    )
                )

        artifacts.append(deck_record)
        for entry in payload.get("artifacts") or ():
            if isinstance(entry, Mapping):
                artifacts.append(dict(entry))

        execution = payload.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        engineering = payload.get("engineering")
        engineering = engineering if isinstance(engineering, Mapping) else {}
        duration = execution.get("duration_ms")
        if isinstance(duration, int) and duration >= 0:
            total_duration += duration
        roster.append(
            {
                "index": deck.index,
                "kind": deck.kind,
                "declared": dict(deck.analysis) if deck.analysis else {},
                "deck_path": deck_record.get("path"),
                "deck_sha256": deck_record.get("sha256"),
                "result_path": (child_record or {}).get("path"),
                "result_sha256": (child_record or {}).get("sha256"),
                "execution_status": execution.get("status"),
                "engineering_status": engineering.get("status"),
                "duration_ms": execution.get("duration_ms"),
                "summary": bounded_text(engineering.get("summary") or "", limit=512),
            }
        )

        if len(decks) > 1:
            for entry in payload.get("diagnostics") or ():
                if not isinstance(entry, Mapping):
                    continue
                relayed = dict(entry)
                relayed["message"] = bounded_text(
                    f"[{deck.kind} analysis {deck.index + 1}] "
                    f"{relayed.get('message', '')}"
                )
                diagnostics.append(relayed)

    chosen_index = _weakest(payloads)
    envelope = payloads[chosen_index]

    extensions = envelope.setdefault("data", {}).setdefault("extensions", {})
    extensions[TARGET_EXTENSION] = target_facts
    if pdk_facts is not None:
        extensions[PDK_BINDING_EXTENSION] = pdk_facts
    if len(decks) > 1:
        extensions[DISPATCH_EXTENSION] = {
            "mode": selected.testbench.dispatch_mode if selected.testbench else "direct",
            "declared_analysis_count": (
                len(selected.testbench.analyses) if selected.testbench else 1
            ),
            "dispatched_analysis_count": len(decks),
            "completed_analysis_count": sum(
                1 for entry in roster if entry["execution_status"] == "completed"
            ),
            "passing_analysis_count": sum(
                1 for entry in roster if entry["engineering_status"] == "pass"
            ),
            "reported_analysis_index": chosen_index,
            "analyses": roster,
        }
        diagnostics.insert(
            0,
            diagnostic(
                "info",
                "simulation.analyses.dispatched",
                (
                    f"The published artifact declares {len(decks)} analyses; one "
                    "single-analysis deck was derived per declaration and every one "
                    "was dispatched through circuit.simulate/v1alpha2. The envelope "
                    f"returned is the weakest of them ({roster[chosen_index]['kind']}, "
                    f"analysis {chosen_index + 1}); every analysis result is retained "
                    "and named in "
                    f"data.extensions['{DISPATCH_EXTENSION}'].analyses."
                ),
            ),
        )

    if pdk_facts is not None and resolved_pdk is not None:
        diagnostics.extend(
            _binding_advisories(
                pdk_facts,
                deck_text=decks[0].text,
                corner=resolved_pdk.corner,
                default_corner=resolved_pdk.binding.default_corner,
            )
        )

    if configuration:
        extensions.setdefault("org.openada", {})
        native = extensions["org.openada"]
        if isinstance(native, dict):
            native["configuration"] = configuration

    merged_inputs = list(input_records)
    seen = {record.get("path") for record in merged_inputs}
    for record in envelope.get("inputs") or ():
        if isinstance(record, Mapping) and record.get("path") not in seen:
            merged_inputs.append(dict(record))
            seen.add(record.get("path"))
    envelope["inputs"] = merged_inputs

    if len(decks) > 1:
        merged_artifacts = artifacts[:MAX_RETAINED_ARTIFACTS]
        if len(artifacts) > MAX_RETAINED_ARTIFACTS:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "simulation.evidence.over_limit",
                    f"{len(artifacts)} retained files exceed the profile's "
                    f"{MAX_RETAINED_ARTIFACTS}-artifact ceiling; the listing was "
                    "truncated. Every analysis result path is still named in "
                    f"data.extensions['{DISPATCH_EXTENSION}'].",
                )
            )
        envelope["artifacts"] = merged_artifacts
        envelope["execution"] = dict(envelope.get("execution") or {})
        envelope["execution"]["duration_ms"] = total_duration

    if unmanaged_collateral:
        # Permanent, in the evidence itself: a reader must never have to know
        # which flags produced a result to know what it is provenance for.
        evidence = envelope.setdefault("data", {}).setdefault("evidence", {})
        limitations = list(evidence.get("provenance_limitations") or ())
        limitations.append(UNMANAGED_COLLATERAL_LIMITATION)
        evidence["provenance_limitations"] = limitations
        evidence["provenance"] = "incomplete"

    existing = list(envelope.get("diagnostics") or ())
    envelope["diagnostics"] = diagnostics + existing
    envelope["operation"] = OPERATION_NAME
    return envelope


def simulate_legacy_native(
    target: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    raw_file: str | Path | None = None,
    workdir: str | Path | None = None,
    execution_mode: str = "batch",
    expected_outputs: Sequence[NgspiceOutput] = (),
    init_file: str | Path | None = None,
    system_init_file: str | Path | None = None,
    timeout: float = 120.0,
    unmanaged_collateral: bool = False,
) -> dict[str, Any]:
    """Invoke ngspice directly, for a caller that owns its own collateral.

    This is a *tool invocation*, not a semantic claim: it emits no
    ``operation_profile`` and asserts nothing about a circuit. It exists because
    a deck with hand-owned includes, ``.measure`` blocks and deck-owned outputs
    is a real thing people have, and refusing to run one would just push them
    back to a bare shell.

    What it will not do any more is run a deck that reaches into an installed
    PDK's tree and binds the wrong technology's collateral. That failure used to
    surface as ngspice's opaque "could not find a valid modelname", or as
    nothing at all; it is now a typed refusal that names ``--pdk``.
    """

    source = Path(target).expanduser().resolve()
    run_directory = (
        Path(workdir).expanduser().resolve() if workdir is not None else source.parent
    )
    try:
        classified = classify_target(source)
    except SimulationRequestError:
        classified = SimulationTarget("deck", source)
    if classified.kind == "simra-artifact":
        return _refusal(
            request_id=str(uuid.uuid4()),
            backend="ngspice",
            code="simulation.target.artifact",
            message=(
                f"{source} is a published Simra schematic artifact, not a SPICE "
                "deck. The native ngspice interface cannot verify its digests, "
                "split its declared analyses or bind a PDK to it, so running it "
                "would silently simulate nothing."
            ),
            hint=(
                "Use the simulation semantic, which honours an artifact target: "
                "openada simulate <artifact> --backend ngspice --pdk <id> "
                "--pdk-root <dir> --output-dir <dir>."
            ),
        )

    try:
        deck_text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        deck_text = ""
    errors, advisories = _collateral_diagnostics(
        deck_text,
        workdir=run_directory,
        bound_pdk=None,
        unmanaged=unmanaged_collateral,
    )
    if errors:
        head = errors[0]
        return _refusal(
            request_id=str(uuid.uuid4()),
            backend="ngspice",
            code=str(head["code"]),
            message=str(head["message"]),
            hint=head.get("hint"),
            extra_diagnostics=errors[1:],
        )

    payload = NgspiceDriver(discovery=discovery).simulate(
        source,
        output_dir,
        raw_file=raw_file,
        workdir=workdir,
        execution_mode=execution_mode,
        expected_outputs=expected_outputs,
        init_file=init_file,
        system_init_file=system_init_file,
        timeout=timeout,
    )
    if unmanaged_collateral:
        advisories.append(
            diagnostic(
                "warning",
                "simulation.provenance.incomplete",
                UNMANAGED_COLLATERAL_LIMITATION,
            )
        )
    if advisories:
        payload["diagnostics"] = list(payload.get("diagnostics") or ()) + advisories
    return payload


__all__ = [
    "DISPATCH_EXTENSION",
    "UNMANAGED_COLLATERAL_LIMITATION",
    "MAX_DISPATCHED_ANALYSES",
    "MAX_RETAINED_ARTIFACTS",
    "OPERATION_NAME",
    "PDK_BINDING_EXTENSION",
    "SUPPORTED_BACKENDS",
    "TARGET_EXTENSION",
    "SimulationRequestError",
    "SimulationTarget",
    "classify_target",
    "simulate",
    "simulate_legacy_native",
]
