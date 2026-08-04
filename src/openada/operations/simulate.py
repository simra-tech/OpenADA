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
import os
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
from ..cosim_compile import CosimCompileError, verify_single_instantiation
from ..engines.spice import (
    MAX_SOURCE_BYTES,
    NgspiceDriver,
    NgspiceOutput,
    NgspicePinnedInput,
)
from ..pdk_bindings import (
    PdkBindingError,
    ResolvedPdkBinding,
    available_pdk_ids,
    bind_deck,
    resolve_pdk_binding,
    simulatable_pdk_ids,
)
from ..osdi_compile import OsdiCompileError, validate_osdi_preload
from ..pdk_collateral import blocking, inspect_deck_collateral
from ..pdk_startup import (
    MANAGED_OSDI_STARTUP_PROVENANCE,
    MANAGED_STARTUP_PROVENANCE,
    write_managed_osdi_startup,
    write_managed_startup,
)
from .circuit_simulate import (
    decorate_circuit_simulation_result,
    inspect_simulation_deck,
    NgspiceProfileExecution,
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
    "pdk.snapshot": "pdk",
    "pdk.osdi-module": "simulator-configuration",
    "pdk.identity": "pdk",
}

#: Detecting a published Simra artifact descriptor must never read an unbounded
#: file; a descriptor is small and its schema token is near the top.
MAX_TARGET_PROBE_BYTES = 4 * 1024 * 1024
_SIMRA_ARTIFACT_SCHEMA_PREFIX = "simra.schematic-artifact/"
MAX_EXTRA_INPUT_RECORDS = 16
MAX_EXTRA_DATA_EXTENSIONS = 16
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXTENSION_KEY_RE = re.compile(
    r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$"
)
_RESERVED_DATA_EXTENSIONS = frozenset(
    {
        "org.openada",
        PDK_BINDING_EXTENSION,
        TARGET_EXTENSION,
        DISPATCH_EXTENSION,
    }
)


class SimulationRequestError(Exception):
    """One bounded, typed reason a simulation request cannot be honoured."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


def _extra_inputs(
    value: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Validate already-captured additive simulation inputs.

    The caller still owns stable capture (normally through ``file_record``);
    this boundary accepts only the exact closed file-record shape that result
    envelopes and downstream extraction already understand.
    """

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise SimulationRequestError(
            "simulation.extra_input.invalid",
            "extra_input_records must be a bounded sequence of file records.",
        )
    if len(value) > MAX_EXTRA_INPUT_RECORDS:
        raise SimulationRequestError(
            "simulation.extra_input.over_limit",
            f"{len(value)} extra input records exceed the ceiling of "
            f"{MAX_EXTRA_INPUT_RECORDS}.",
        )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}] must be an object.",
            )
        required = {"kind", "role", "path", "exists", "bytes", "sha256"}
        if set(raw) != required:
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}] must contain exactly "
                f"{', '.join(sorted(required))}.",
            )
        if any(
            not isinstance(raw[field], str) or not raw[field]
            for field in ("kind", "role", "path")
        ):
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}] kind, role, and path must be "
                "nonempty text.",
            )
        size = raw["bytes"]
        digest = raw["sha256"]
        if raw["exists"] is not True:
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}] must bind an existing file.",
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}].bytes must be a non-negative integer.",
            )
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise SimulationRequestError(
                "simulation.extra_input.invalid",
                f"extra_input_records[{index}].sha256 must be a lowercase SHA-256.",
            )
        normalized.append(dict(raw))
    return tuple(normalized)


def _extra_extensions(
    value: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return a JSON-safe closed set of non-reserved result-data extensions."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SimulationRequestError(
            "simulation.extension.invalid",
            "extra_data_extensions must be an object.",
        )
    if len(value) > MAX_EXTRA_DATA_EXTENSIONS:
        raise SimulationRequestError(
            "simulation.extension.over_limit",
            f"{len(value)} extra data extensions exceed the ceiling of "
            f"{MAX_EXTRA_DATA_EXTENSIONS}.",
        )
    normalized: dict[str, dict[str, Any]] = {}
    for key, extension in value.items():
        if (
            not isinstance(key, str)
            or _EXTENSION_KEY_RE.fullmatch(key) is None
        ):
            raise SimulationRequestError(
                "simulation.extension.invalid",
                f"The extra data extension key {key!r} is not canonical.",
            )
        if key in _RESERVED_DATA_EXTENSIONS:
            raise SimulationRequestError(
                "simulation.extension.conflict",
                f"The extra data extension {key!r} is owned by simulate.",
            )
        if not isinstance(extension, Mapping):
            raise SimulationRequestError(
                "simulation.extension.invalid",
                f"The extra data extension {key!r} must be an object.",
            )
        try:
            # Clone at the request boundary so later caller mutation cannot
            # change what is retained, and reject NaN/infinity at the same time.
            encoded = json.dumps(
                extension,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            cloned = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise SimulationRequestError(
                "simulation.extension.invalid",
                f"The extra data extension {key!r} is not finite JSON: {exc}",
            ) from exc
        if not isinstance(cloned, dict):  # guarded by Mapping, kept explicit
            raise SimulationRequestError(
                "simulation.extension.invalid",
                f"The extra data extension {key!r} must normalize to an object.",
            )
        normalized[key] = cloned
    return normalized


def _text_sequence(
    value: Sequence[str] | None,
    *,
    label: str,
) -> tuple[str, ...] | None:
    """Copy an optional text sequence without treating one string as a list."""

    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise SimulationRequestError(
            "simulation.request.invalid",
            f"{label} must be a sequence of names.",
        )
    if any(not isinstance(item, str) for item in value):
        raise SimulationRequestError(
            "simulation.request.invalid",
            f"Every {label} entry must be text.",
        )
    return tuple(value)


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


def _artifact_deck_probe_text(descriptor_path: Path) -> str | None:
    """Best-effort read of a Simra artifact's published deck, for family scans.

    Mirrors the loader's sibling resolution (the descriptor names its netlist
    as one file in the artifact directory) without importing its validation:
    any irregularity returns ``None``, which keeps the full conservative
    library closure. The load-bearing digest and shape checks still happen in
    ``load_simra_testbench``; this read only decides which family-tagged
    libraries the PDK snapshot captures.
    """

    try:
        document = json.loads(
            descriptor_path.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(document, Mapping):
        return None
    netlist = document.get("netlist")
    if not isinstance(netlist, str) or not netlist:
        return None
    candidate = descriptor_path.parent / netlist
    if candidate.parent != descriptor_path.parent:
        return None
    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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

#: An independent source declaring a small-signal magnitude. Without at least
#: one of these an ``.AC`` sweep solves a circuit nothing is driving.
_AC_STIMULUS_RE = re.compile(
    r"^\s*[VvIi][^\s]*(?:[ \t]+[^\s]+){2}.*(?<![A-Za-z0-9_])ac(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _declares_ac_stimulus(deck_text: str) -> bool:
    """Return whether any top-level source card carries an ``AC`` magnitude."""

    for line in deck_text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        if _AC_STIMULUS_RE.match(line):
            return True
    return False


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
        # The hint must describe the card that dropped the key, not the
        # binding-wide MOS vocabulary — and not a union across card types
        # either: a MOS and a diode both dropping ``area`` accept different
        # keys, so each occurrence record keeps its own instance and its own
        # accepted set (deduplicated only when both agree exactly).
        records = [
            record
            for record in (facts.get("dropped_parameter_records") or ())
            if isinstance(record, Mapping) and record.get("accepted")
        ]
        per_card_hints = []
        seen: set[tuple[Any, ...]] = set()
        for record in records:
            accepted = tuple(record["accepted"])
            key = (record.get("parameter"), accepted)
            if key in seen:
                continue
            seen.add(key)
            instance = record.get("instance") or "?"
            per_card_hints.append(
                f"{record.get('parameter')} ({instance}): accepted on that "
                f"card are {', '.join(accepted)}"
            )
        hint = (
            "; ".join(per_card_hints) + "."
            if per_card_hints
            else (
                "Parameters this PDK accepts: "
                f"{', '.join(sorted(facts.get('parameter_names') or ()))}."
            )
        )
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
                hint=hint,
            )
        )

    for instance, nodes in sorted((facts.get("dropped_nodes") or {}).items()):
        notes.append(
            diagnostic(
                "warning",
                "pdk.device.node_dropped",
                (
                    f"{instance}: the canonical terminal(s) "
                    f"{', '.join(nodes)} were dropped because the bound "
                    f"{pdk_id} device models no such terminal. The author's "
                    "stated tie is electrically absent from this answer — "
                    "material whenever it differs from the device's implicit "
                    "reference."
                ),
                hint=(
                    "If the substrate/body tie matters here, choose a PDK "
                    "whose device models that terminal."
                ),
            )
        )

    for instance, derivation in sorted(
        (facts.get("geometry_derived") or {}).items()
    ):
        notes.append(
            diagnostic(
                "warning",
                "pdk.device.geometry_derived",
                (
                    f"{instance}: the deck's diode geometry was converted to "
                    f"the convention the bound {pdk_id} device takes: "
                    f"{derivation}"
                ),
                hint=(
                    "State the geometry in the target's own convention to "
                    "avoid the conversion (exact for W/L->AREA/PJ; a square "
                    "is assumed only when PJ is absent or inconsistent)."
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
    expected_deck_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one PDK-bound deck and return the reviewed evidence envelope.

    A bound deck carries includes and a control block, so it runs through the
    native control-mode path rather than the model-free shared bridge. The
    result is then decorated into the *same* ``circuit.simulate/v1alpha2``
    evidence block every other simulation produces: the model source changes
    what the simulator is told, never what the evidence means.

    The run's ngspice startup file is OpenADA's own. A binding exports
    ``PDK_ROOT`` and ``PDK``, and an ambient ``.spiceinit`` - IHP ships one, and
    the workstation image installs it into the user's home - expands exactly
    those two variables to preload *its* Verilog-A modules from *this* PDK's
    tree. Handing ngspice an explicit startup file suppresses every ambient one,
    so the technology binding stays the profile's and the startup becomes a
    content-bound input rather than an invisible property of the host. See
    ``pdk_startup``.
    """

    resolved_pdk.verify_snapshot()
    startup_path = write_managed_startup(output_dir, resolved_pdk)
    payload = NgspiceDriver(discovery=discovery).simulate(
        deck_path,
        output_dir,
        workdir=workdir,
        execution_mode="control",
        expected_outputs=[NgspiceOutput(kind="raw", path=raw_name)],
        init_file=startup_path,
        environment_overrides={
            "PDK_ROOT": str(resolved_pdk.root.parent),
            "PDK": resolved_pdk.pdk_id,
        },
        timeout=timeout,
        expected_source_sha256=expected_deck_sha256,
    )
    resolved_pdk.verify_snapshot()
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
            "The top-level bound deck, the selected native executable, the "
            "simulator startup file, and the complete active ordered "
            f"include/library closure for {resolved_pdk.pdk_id} were captured "
            "into one immutable content-addressed snapshot and verified before "
            "and after native launch; host runtime libraries and simulator "
            "defaults remain bounded provenance.",
            MANAGED_STARTUP_PROVENANCE,
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
    resolved_pdk_binding: ResolvedPdkBinding | None,
    snapshot_parent: Path,
    inputs: list[dict[str, Any]],
    configuration: list[dict[str, Any]],
    deck_text: str | None = None,
    expected_models_sha256: str | None = None,
    permitted_executable_models: Mapping[str, tuple[str, str]] | None = None,
    osdi_preload_text: str | None = None,
    osdi_preload_sha256: str | None = None,
    osdi_module_digests: Mapping[str, str] | None = None,
) -> tuple[ResolvedPdkBinding | None, str | None, tuple[tuple[str, str], ...]]:
    """Resolve at most one model source, or raise a typed refusal.

    ``permitted_executable_models`` maps a generated ``d_cosim`` binding-card
    name to the ``(absolute object path, sha256)`` of the compiled object that
    card must load. The allowance is honored only alongside a pinned
    composition digest, and every named object's bytes are re-read and
    verified HERE -- inside the operation boundary, after the model library is
    loaded and before any native launch -- then recorded as a bound input, so
    the retained provenance describes the bytes the simulator actually loaded
    rather than the bytes some earlier step happened to compile.
    """

    permitted = dict(permitted_executable_models or {})
    # The executable-model allowance exists ONLY for reviewed, digest-pinned
    # block compositions: the permitted names are meaningless without the
    # digest that binds them to the reviewed bytes, so an unpinned allowance
    # is refused as an internal contract violation rather than honored.
    if permitted and expected_models_sha256 is None:
        raise SimulationRequestError(
            "simulation.models.allowance_unbound",
            "permitted executable models were named without a pinned "
            "composition digest; the allowance only exists for digest-bound "
            "reviewed compositions.",
        )
    for name, declared in permitted.items():
        if (
            not isinstance(declared, tuple)
            or len(declared) != 2
            or not all(isinstance(part, str) for part in declared)
        ):
            raise SimulationRequestError(
                "simulation.models.allowance_unbound",
                f"the executable-model allowance for {name!r} must declare "
                "(object_path, sha256); an unverifiable allowance is refused.",
            )

    model_source_count = sum(
        1
        for present in (
            models_file is not None,
            pdk is not None or resolved_pdk_binding is not None,
            osdi_preload_text is not None,
        )
        if present
    )
    if model_source_count > 1:
        raise SimulationRequestError(
            "simulation.models.ambiguous",
            "Exactly one model source may bind a deck: a self-contained "
            "model-card file (--models), a PDK binding (--pdk), or a reviewed "
            "behavioral-block OSDI preload (--blocks --osdi).",
            hint="Pass one of --models, --pdk, or --blocks --osdi.",
        )
    if corner is not None and pdk is None and resolved_pdk_binding is None:
        raise SimulationRequestError(
            "pdk.corner.unbound",
            "A corner may only be selected together with --pdk.",
            hint=f"Installed PDKs that bind a corner: {', '.join(simulatable_pdk_ids())}.",
        )

    resolved: ResolvedPdkBinding | None = resolved_pdk_binding
    if resolved is not None:
        if backend != "ngspice":
            raise SimulationRequestError(
                "pdk.backend.unsupported",
                f"PDK binding is implemented for ngspice only; backend {backend!r} "
                "has no reviewed binding profile.",
                hint="Rerun with --backend ngspice, or supply --models instead.",
            )
        if pdk is not None or pdk_root is not None or corner is not None:
            raise SimulationRequestError(
                "pdk.binding.conflict",
                (
                    "An already captured PDK binding is the complete model "
                    "source; pdk, pdk_root, and corner must not be supplied "
                    "again because no later consumer may reopen live paths."
                ),
            )
        try:
            resolved.verify_snapshot()
        except PdkBindingError as exc:
            raise SimulationRequestError(
                exc.code, exc.message, hint=exc.hint
            ) from exc
    elif pdk is not None:
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
                f"Binding the PDK {pdk!r} requires a PDK root, and neither "
                "--pdk-root nor the PDK_ROOT environment variable supplied one.",
                hint=(
                    "Pass --pdk-root <dir> with the directory containing the "
                    "installed PDK tree, or export PDK_ROOT=<dir>. `openada doctor` "
                    "reports the roots it can already see."
                ),
            )
        try:
            resolved = resolve_pdk_binding(
                pdk,
                pdk_root,
                corner=corner,
                snapshot_parent=snapshot_parent,
                deck_text=deck_text,
            )
        except PdkBindingError as exc:
            raise SimulationRequestError(exc.code, exc.message, hint=exc.hint) from exc

    if resolved is not None:
        inputs.extend(resolved.input_records)
        for record in resolved.configuration_records:
            configuration_role = _PDK_CONFIGURATION_ROLES.get(
                str(record.get("role"))
            )
            if configuration_role is None:
                raise SimulationRequestError(
                    "pdk.snapshot.invalid",
                    "The captured PDK snapshot exposed an undeclared "
                    f"configuration role {record.get('role')!r}.",
                )
            configuration.append(
                {
                    "role": configuration_role,
                    "path": record.get("path"),
                    "sha256": record.get("sha256"),
                    "bytes": record.get("bytes"),
                    "identity": "content-digest",
                }
            )

    model_prelude: str | None = None
    # The exact `(osdi_path, sha256)` of every ``pre_osdi`` module the validated
    # preload references -- the authoritative set to content-bind at the driver
    # boundary (never the raw, possibly-incomplete caller digest map).
    verified_osdi_modules: tuple[tuple[str, str], ...] = ()
    if models_file is not None:
        try:
            model_prelude, models_record = load_model_prelude(
                models_file,
                permitted_executable_models={
                    name.lower(): object_path
                    for name, (object_path, _digest) in permitted.items()
                },
            )
        except SimraArtifactError as exc:
            raise SimulationRequestError(exc.code, exc.message, hint=exc.hint) from exc
        # The tamper check lives INSIDE the operation boundary, immediately
        # after the bytes that will bind this run were read and BEFORE any
        # native launch or result retention: when the caller pins the digest
        # of a reviewed composition, a mismatch is the operation's own
        # pre-launch refusal, never a post-hoc rewrite of a retained result.
        if (
            expected_models_sha256 is not None
            and models_record.get("sha256") != expected_models_sha256
        ):
            raise SimulationRequestError(
                "blocks.materialize.tampered",
                "The model library read for this run does not hash to the "
                f"reviewed composition digest {expected_models_sha256}; the "
                "materialized file changed after verification, so no "
                "simulator was launched and no result was retained.",
            )
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
        # Every admitted executable object is verified against its declared
        # digest HERE -- after the model text is bound, before any launch --
        # and recorded, so the retained evidence names the exact bytes ngspice
        # will dlopen instead of merely the bytes something once compiled.
        for name in sorted(permitted):
            object_path, object_sha256 = permitted[name]
            candidate = Path(object_path)
            # The path ngspice will dlopen must mean the same thing to both
            # processes and must not be redirectable between verification and
            # load: a relative path resolves against the SIMULATOR's working
            # directory, not ours, and a symlinked component can be repointed
            # after the bytes were read.
            if not candidate.is_absolute():
                raise SimulationRequestError(
                    "simulation.models.executable_unverifiable",
                    f"the compiled object {object_path} bound by the model "
                    f"card {name!r} is a relative path; it would resolve "
                    "against the simulator's working directory, not the one "
                    "verified here.",
                )
            if str(candidate) != os.path.normpath(str(candidate)):
                raise SimulationRequestError(
                    "simulation.models.executable_unverifiable",
                    f"the compiled object {object_path} bound by the model "
                    f"card {name!r} is not a normalized path.",
                )
            try:
                if candidate.resolve(strict=True) != candidate:
                    raise SimulationRequestError(
                        "simulation.models.executable_unverifiable",
                        f"the compiled object {object_path} bound by the model "
                        f"card {name!r} reaches through a symbolic link; the "
                        "verified bytes and the loaded bytes could differ.",
                    )
            except OSError as exc:
                raise SimulationRequestError(
                    "simulation.models.executable_unverifiable",
                    f"the compiled object {object_path} bound by the model "
                    f"card {name!r} could not be resolved: {exc}.",
                ) from exc
            try:
                object_bytes = candidate.read_bytes()
            except OSError as exc:
                raise SimulationRequestError(
                    "simulation.models.executable_unverifiable",
                    f"the compiled object {object_path} bound by the model "
                    f"card {name!r} could not be read for verification: {exc}; "
                    "no simulator was launched.",
                ) from exc
            observed = hashlib.sha256(object_bytes).hexdigest()
            if observed != object_sha256:
                raise SimulationRequestError(
                    "simulation.models.executable_tampered",
                    f"the compiled object {object_path} bound by the model "
                    f"card {name!r} hashes to {observed}, not the declared "
                    f"{object_sha256}; the object changed after composition, "
                    "so no simulator was launched and no result was retained.",
                )
            record = {
                "kind": "xspice-cosim-object",
                "role": "model-implementation",
                "path": str(candidate),
                "bytes": len(object_bytes),
                "sha256": observed,
            }
            inputs.append(record)
            configuration.append(
                {
                    "role": "xspice-cosim-object",
                    "path": str(candidate),
                    "sha256": observed,
                    "bytes": len(object_bytes),
                    "identity": "content-digest",
                }
            )
    elif osdi_preload_text is not None:
        # A behavioral-block OSDI preload is a reviewed, digest-bound composition
        # (osdi_compile.compose_blocks_osdi): a `.control pre_osdi .endc` block
        # plus wrapper subcircuits. It deliberately does NOT go through
        # load_model_prelude's self-contained gate (it must carry a control
        # block to load OSDI) and its `pre_osdi` cards are exempt from the
        # hand-bound-collateral refusal because they are library-owned and
        # verified here by digest, never hand-written by the caller.
        encoded = osdi_preload_text.encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest()
        if osdi_preload_sha256 is not None and actual != osdi_preload_sha256:
            raise SimulationRequestError(
                "blocks.materialize.tampered",
                "The behavioral-block OSDI preload for this run does not hash to "
                f"the reviewed composition digest {osdi_preload_sha256}; the "
                "composition changed after verification, so no simulator was "
                "launched and no result was retained.",
            )
        # The preload is the ONE model source allowed to carry a `.control` block
        # into the run deck, so it is authorized by SHAPE, not by the text hash
        # above (which a caller could recompute): its control block may hold only
        # `pre_osdi <path>` lines, and every referenced .osdi is re-hashed from
        # disk here and (when a digest map is supplied) pinned to its reviewed
        # compile digest, so a swapped module is refused before ngspice maps it.
        try:
            verified_preload = validate_osdi_preload(
                osdi_preload_text, expected_osdi_sha256=osdi_module_digests
            )
        except OsdiCompileError as exc:
            raise SimulationRequestError(exc.code, exc.message)
        model_prelude = osdi_preload_text
        verified_osdi_modules = verified_preload.modules
        configuration.append(
            {
                "role": "osdi-block-preload",
                "path": None,
                "sha256": actual,
                "bytes": len(encoded),
                "identity": "content-digest",
            }
        )
        for module_path, module_sha256 in verified_preload.modules:
            configuration.append(
                {
                    "role": "osdi-block-module",
                    "path": module_path,
                    "sha256": module_sha256,
                    "identity": "content-digest",
                }
            )
    return resolved, model_prelude, verified_osdi_modules


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
    resolved_pdk_binding: ResolvedPdkBinding | None = None,
    parameters: Mapping[str, object] | None = None,
    workdir: str | Path | None = None,
    # A PDK binding pays the model library's parse cost on every derived deck;
    # sky130A's tt section alone takes ~95 s. See cli.py's --timeout default.
    timeout: float = 600.0,
    request_id: str | None = None,
    unmanaged_collateral: bool = False,
    extra_diagnostics: Sequence[Mapping[str, Any]] = (),
    saved_nets: Sequence[str] | None = None,
    retained_current_sources: Sequence[str] | None = None,
    extra_input_records: Sequence[Mapping[str, Any]] = (),
    extra_data_extensions: Mapping[str, Any] | None = None,
    expected_models_sha256: str | None = None,
    permitted_executable_models: Mapping[str, tuple[str, str]] | None = None,
    cosim_wrappers: Sequence[str] = (),
    cosim_composition: Any | None = None,
    osdi_composition: Any | None = None,
    osdi_preload_text: str | None = None,
    osdi_preload_sha256: str | None = None,
    osdi_module_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one circuit simulation, whatever shape the request arrived in.

    ``target`` is either a SPICE deck or a published Simra schematic artifact
    descriptor; the driver decides which by reading it. The model source is at
    most one of ``models_file`` or ``pdk`` (or the already captured
    ``resolved_pdk_binding`` used by experiment preflight). Every path returns exactly one
    ``circuit.simulate/v1alpha2`` envelope. ``saved_nets`` and
    ``retained_current_sources`` are the explicit observation binding for a
    caller-composed PDK deck; omitting them preserves the legacy behavior.
    Additive input records and data extensions are applied before any result
    envelope is retained. ``expected_models_sha256`` pins ``models_file`` to a
    reviewed digest: the bytes actually read are compared immediately after
    loading and before any native launch, and a mismatch is this operation's
    own ``blocks.materialize.tampered`` pre-launch refusal.
    """

    correlation_id, request_id_error = _correlation_id(request_id)
    forwarded = [dict(entry) for entry in extra_diagnostics]
    normalized_backend = backend if backend in SUPPORTED_BACKENDS else None
    normalized_extra_inputs: tuple[dict[str, Any], ...] = ()
    normalized_extra_extensions: dict[str, dict[str, Any]] = {}
    normalized_saved_nets: tuple[str, ...] | None = None
    normalized_current_sources: tuple[str, ...] | None = None
    preflight_error: SimulationRequestError | None = None
    try:
        normalized_extra_inputs = _extra_inputs(extra_input_records)
        normalized_extra_extensions = _extra_extensions(extra_data_extensions)
        normalized_saved_nets = _text_sequence(saved_nets, label="saved_nets")
        normalized_current_sources = _text_sequence(
            retained_current_sources,
            label="retained_current_sources",
        )
    except SimulationRequestError as exc:
        preflight_error = exc

    def refuse(
        code: str,
        message: str,
        *,
        hint: str | None = None,
        inputs: Iterable[dict[str, Any]] = (),
        execution_status: str = "invalid_request",
        extensions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_extensions = dict(normalized_extra_extensions)
        merged_extensions.update(dict(extensions or {}))
        return _refusal(
            request_id=correlation_id,
            backend=normalized_backend,
            code=code,
            message=message,
            hint=hint,
            inputs=inputs,
            execution_status=execution_status,
            extensions=merged_extensions,
            extra_diagnostics=forwarded,
        )

    if request_id_error is not None:
        return refuse("simulation.request.invalid", request_id_error)
    if preflight_error is not None:
        return refuse(
            preflight_error.code,
            preflight_error.message,
            hint=preflight_error.hint,
        )

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

    input_records: list[dict[str, Any]] = [
        dict(record) for record in normalized_extra_inputs
    ]
    configuration: list[dict[str, Any]] = []
    destination = Path(output_dir).expanduser().resolve()
    # Family-tagged PDK libraries load only when the deck names the family.
    # The probe is a best-effort read of the caller's own deck; an unreadable
    # target scans as None and keeps the full (conservative) closure. A Simra
    # artifact's deck is its published sibling netlist file — cheaply readable
    # before resolution, and safe to gate on now that the family scanner
    # recognises the binder's full alias vocabulary (a scan miss can only
    # over-load, and a reused gated snapshot refuses a mismatched deck with
    # pdk.snapshot.family_missing instead of failing in the simulator).
    deck_probe_text: str | None = None
    if selected.kind != "simra-artifact":
        try:
            deck_probe_text = selected.path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            deck_probe_text = None
    else:
        deck_probe_text = _artifact_deck_probe_text(selected.path)
    permitted = dict(permitted_executable_models or {})
    caller_deck_text: str | None = None
    try:
        resolved_pdk, model_prelude, verified_osdi_modules = _resolve_model_source(
            correlation_id=correlation_id,
            backend=normalized_backend,
            models_file=models_file,
            pdk=pdk,
            pdk_root=pdk_root,
            corner=corner,
            resolved_pdk_binding=resolved_pdk_binding,
            # System temp, like `experiment run`: the snapshot is a read-only
            # (0500/0400) integrity-verified model cache, and parking it under
            # the evidence dir made that dir un-removable and polluted output
            # collection wherever the evidence tree gets harvested.
            snapshot_parent=None,
            inputs=input_records,
            configuration=configuration,
            deck_text=deck_probe_text,
            expected_models_sha256=expected_models_sha256,
            permitted_executable_models=permitted_executable_models,
            osdi_preload_text=osdi_preload_text,
            osdi_preload_sha256=osdi_preload_sha256,
            osdi_module_digests=osdi_module_digests,
        )
    except SimulationRequestError as exc:
        return refuse(
            exc.code,
            exc.message,
            hint=exc.hint,
            extensions={TARGET_EXTENSION: _target_facts(selected)},
        )

    if selected.kind == "simra-artifact":
        if normalized_saved_nets is not None:
            return refuse(
                "simulation.saved_nets.artifact_unsupported",
                "Explicit saved_nets are only accepted for a caller-composed bare "
                "deck; a Simra artifact owns its published saved-net set.",
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )
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
        caller_deck_text = testbench.deck_text
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
        bound_saved_nets = testbench.saved_nets
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
        # The bytes the caller actually wrote, kept for the hand-bound-collateral
        # check: a reviewed model prelude (self-contained cards, or a sanctioned
        # digest-bound OSDI preload) is spliced below but must not be scanned as
        # if the caller hand-bound it.
        caller_deck_text = deck_text
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
                collateral_text=caller_deck_text,
            ),
        )
        bound_saved_nets = normalized_saved_nets or ()

    # The d_cosim single-instance rule is enforced HERE, at the operation
    # boundary, on the caller's own deck text (before the composed prelude is
    # spliced in -- the prelude's wrapper bodies are DEFINITIONS, not
    # instances). Enforcing it only in the CLI left the programmatic
    # compose->simulate path unchecked and mis-scanned an artifact descriptor
    # as if it were a deck.
    # OSDI relational parameter constraints are enforced HERE, at the
    # operation boundary on the resolved caller deck text (the CLI-side check
    # sees only the original source, which for an artifact is a JSON
    # descriptor, not the deck).
    if osdi_composition is not None and caller_deck_text is not None:
        try:
            osdi_composition.verify_deck(caller_deck_text)
        except OsdiCompileError as exc:
            return refuse(
                exc.code,
                exc.message,
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )
    if permitted and caller_deck_text is not None:
        try:
            if cosim_composition is not None:
                cosim_composition.verify_deck(caller_deck_text)
            else:
                verify_single_instantiation(
                    caller_deck_text, cosim_wrappers or (), tuple(permitted)
                )
        except CosimCompileError as exc:
            return refuse(
                exc.code,
                exc.message,
                inputs=input_records,
                extensions={TARGET_EXTENSION: _target_facts(selected)},
            )

    target_facts = _target_facts(selected)

    if (
        resolved_pdk is None
        and (
            normalized_saved_nets is not None
            or normalized_current_sources is not None
        )
    ):
        return refuse(
            "simulation.binding_options.unbound",
            "saved_nets and retained_current_sources require a PDK-bound deck, "
            "because that binding owns the retained raw write set.",
            inputs=input_records,
            extensions={TARGET_EXTENSION: target_facts},
        )

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

    # A bound deck runs the analysis it declares, because a PDK binding is a
    # model source and not a rewrite of the experiment. So a caller who also
    # names an analysis must be naming the same one; the alternative is that the
    # request says ``op``, the deck says ``.ac``, an AC sweep runs and nothing
    # anywhere reports the disagreement.
    if resolved_pdk is not None and isinstance(parameters, Mapping):
        requested = parameters.get("analysis")
        requested_analysis = (
            requested.get("type") if isinstance(requested, Mapping) else None
        )
        declared = {deck.kind for deck in decks if deck.kind != "deck"}
        if (
            isinstance(requested_analysis, str)
            and declared
            and requested_analysis not in declared
        ):
            return refuse(
                "simulation.request.invalid",
                f"The request names the {requested_analysis} analysis, but the "
                f"deck a {resolved_pdk.pdk_id} binding will run declares "
                f"{', '.join(sorted(declared))}. A PDK binding supplies device "
                "models; it does not rewrite the experiment.",
                hint=(
                    "Drop --analysis to run what the deck declares, or state the "
                    "analysis in the deck."
                ),
                inputs=input_records,
                extensions={TARGET_EXTENSION: target_facts},
            )

    # The collateral check runs on what the caller actually wrote, before any
    # binding, because the whole point is to catch a deck that reaches into a
    # PDK by hand.
    advisories: list[dict[str, Any]] = []
    for deck in decks:
        errors, notes = _collateral_diagnostics(
            deck.collateral_text if deck.collateral_text is not None else deck.text,
            workdir=run_directory if selected.kind == "deck" else None,
            bound_pdk=resolved_pdk.pdk_id if resolved_pdk is not None else None,
            unmanaged=unmanaged_collateral,
        )
        advisories.extend(notes)
        # An AC sweep with nothing driving it converges over every point and
        # returns zero everywhere, and the run is a legitimate `pass`: the tool
        # ran, the evidence is structurally valid, and the *question* was empty.
        # A live job reported a 280 MOhm cascode output impedance off exactly
        # such a run - its testbench wired `V_OUT_STIM` with a DC value and no
        # AC magnitude - and nothing in the evidence contradicted the number.
        if deck.kind == "ac" and not _declares_ac_stimulus(deck.text):
            advisories.append(
                diagnostic(
                    "warning",
                    "simulation.stimulus.absent",
                    (
                        "The deck declares an AC analysis but no independent source "
                        "carries an AC magnitude, so the sweep solves a circuit "
                        "nothing is driving: every dependent vector is identically "
                        "zero at every frequency. A gain, impedance or bandwidth "
                        "derived from this run would be a ratio of zeros."
                    ),
                    hint=(
                        "Give the stimulating source an AC magnitude - "
                        "`V_IN in 0 DC 0.8 AC 1` - and re-run."
                    ),
                )
            )
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
        # A multi-analysis artifact launches the simulator once per derived
        # deck, so the admitted executable objects are re-verified before EACH
        # dispatch: verifying once before the loop would let an object swapped
        # between analyses be attributed to the digest checked earlier.
        for permitted_name in sorted(permitted):
            permitted_path, permitted_sha256 = permitted[permitted_name]
            try:
                current = hashlib.sha256(Path(permitted_path).read_bytes()).hexdigest()
            except OSError as exc:
                return refuse(
                    "simulation.models.executable_unverifiable",
                    f"the compiled object {permitted_path} bound by the model "
                    f"card {permitted_name!r} could not be re-read before this "
                    f"analysis: {exc}.",
                    inputs=input_records,
                    extensions={TARGET_EXTENSION: target_facts},
                )
            if current != permitted_sha256:
                return refuse(
                    "simulation.models.executable_tampered",
                    f"the compiled object {permitted_path} bound by the model "
                    f"card {permitted_name!r} changed between analyses; no "
                    "further simulator launch was made.",
                    inputs=input_records,
                    extensions={TARGET_EXTENSION: target_facts},
                )
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
                    saved_nets=bound_saved_nets,
                    retained_current_sources=normalized_current_sources,
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
            # The deck's launch is content-bound to this raw-byte digest. If the
            # caller's file could not be stably content-captured (file_record
            # returns no digest, e.g. it was replaced mid-operation), refuse
            # rather than forward a None binding and launch it unverified. A
            # derived deck is instead guarded by the deck.sha256 check below.
            if not isinstance(deck_record.get("sha256"), str):
                return refuse(
                    "simulation.deck.unstable",
                    f"{deck_path} could not be content-bound before launch; its "
                    "digest is required to bind the run to the bytes that were "
                    "profile-checked.",
                    inputs=input_records,
                    execution_status="failed",
                    extensions={TARGET_EXTENSION: target_facts},
                )
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
            try:
                payload = _run_bound_deck(
                    deck_path,
                    destination / stem,
                    discovery=discovery,
                    workdir=deck_directory,
                    resolved_pdk=resolved_pdk,
                    raw_name=raw_name,
                    timeout=timeout,
                    request_id=correlation_id,
                    expected_deck_sha256=deck_record.get("sha256"),
                )
            except PdkBindingError as exc:
                return refuse(
                    exc.code,
                    exc.message,
                    hint=exc.hint,
                    inputs=input_records,
                    execution_status="failed",
                    extensions={TARGET_EXTENSION: target_facts},
                )
        else:
            osdi_inspection_source: Path | None = None
            osdi_ready = (
                osdi_preload_text is not None
                and normalized_backend == "ngspice"
                and deck.collateral_text is not None
            )
            if osdi_ready:
                # The reviewed pre_osdi deck cannot run in the batch profile
                # runner: batch refuses its `.control` block, and even in control
                # mode a bare `.op`/`.tran` does not auto-run. So the OSDI path
                # runs through ngspice control mode with a wrapper-owned raw and
                # an empty managed startup (no PDK) that disables the local/user
                # `.spiceinit`. The profile gate still validates the caller's own
                # control-free bytes — but the SAME bytes that were spliced into
                # the run deck, re-read from a stable file we write here. For a
                # bare deck those are the pristine caller bytes; for a published
                # artifact they are the per-analysis control-free deck the driver
                # derived (`derive_single_analysis_decks` keeps the caller's cards
                # WITHOUT the composed pre_osdi prelude). Either way the inspected
                # bytes are exactly what was spliced into the run deck, re-read
                # from a stable file rather than a path that could change between
                # the splice and the inspection. Only the launch differs; the
                # evidence's meaning does not.
                osdi_run_dir = destination / stem
                try:
                    osdi_startup = write_managed_osdi_startup(osdi_run_dir)
                    osdi_inspection_source = deck_directory / f"{stem}.caller.spice"
                    osdi_inspection_source.write_text(
                        deck.collateral_text or "", encoding="utf-8"
                    )
                except OSError as exc:
                    return refuse(
                        "simulation.destination.unusable",
                        f"The OSDI run's managed startup or inspection deck could "
                        f"not be retained: {exc}",
                        inputs=input_records,
                        execution_status="failed",
                        extensions={TARGET_EXTENSION: target_facts},
                    )

                # The composed OSDI run deck and every reviewed .osdi module are
                # content-bound at the driver boundary: ngspice refuses to launch
                # unless the deck still hashes to its derived digest and each
                # module still matches its reviewed OpenVAF compile digest,
                # re-checked immediately before ngspice maps them (narrows the
                # resolve-time -> launch TOCTOU inside the private evidence dir;
                # the final hash-to-open() gap is the documented FD residual).
                osdi_pinned_modules = tuple(
                    NgspicePinnedInput(
                        path=Path(module_path),
                        sha256=module_sha256,
                        kind="osdi-block-module",
                    )
                    for module_path, module_sha256 in verified_osdi_modules
                )
                execution = NgspiceProfileExecution(
                    execution_mode="control",
                    # The RAW-byte digest of the exact file the driver opens and
                    # scans (`deck_record` is a file_record over raw bytes, as is
                    # the driver's own scan), so the driver refuses to launch a
                    # deck whose file changed between this capture and launch. For
                    # a derived OSDI deck this is verified == deck.sha256 above;
                    # deck.sha256 itself is NOT usable as the bind (it is the
                    # text-mode-normalized digest, which diverges from the raw
                    # bytes the driver hashes for a CRLF deck). Never None: the
                    # non-derived path is guarded below and a derived record is
                    # verified against deck.sha256.
                    expected_source_sha256=deck_record.get("sha256"),
                    pinned_inputs=osdi_pinned_modules,
                    # For the sanctioned OSDI preload, the run deck carries a
                    # reviewed `.control pre_osdi` block the initial shared profile
                    # forbids; profile-check the caller's own control-free deck
                    # (the exact bytes spliced, written to a stable file above).
                    inspection_source=osdi_inspection_source,
                    init_file=osdi_startup,
                    provenance_limitations=(
                        "The reviewed behavioral-block OSDI preload (the digest-"
                        "bound pre_osdi cards and wrapper subckts) and the caller's "
                        "control-free deck were composed into one deck, whose "
                        "digest was verified before launch; every compiled .osdi "
                        "module was re-hashed from disk against its reviewed "
                        "OpenVAF compile digest before ngspice mapped it. Host "
                        "runtime libraries and simulator defaults remain bounded "
                        "provenance.",
                        MANAGED_OSDI_STARTUP_PROVENANCE,
                    ),
                )
            else:
                # Every OpenADA-derived deck reaching the shared batch profile —
                # a bare deck, a per-analysis artifact split, a --models
                # composition, or a d_cosim deck — is now content-bound at the
                # driver: it must still hash to its retained digest at launch
                # (entry check + pre-run_process recheck). A cosim deck also pins
                # each compiled d_cosim `.so` to its reviewed digest, re-verified
                # immediately before launch (the operation already re-hashes them
                # per analysis; this closes the last rehash->launch window, same
                # descriptor-level FD residual as the OSDI path).
                cosim_pinned_objects = tuple(
                    NgspicePinnedInput(
                        path=Path(object_path),
                        sha256=object_sha256,
                        kind="cosim-code-model",
                    )
                    for _name, (object_path, object_sha256) in sorted(permitted.items())
                )
                execution = NgspiceProfileExecution(
                    execution_mode="batch",
                    # The RAW-byte digest of the file the driver opens (see the
                    # OSDI spec above for why deck.sha256's text-normalized digest
                    # cannot be used here); the driver refuses a file changed
                    # between capture and launch. Guarded non-None below.
                    expected_source_sha256=deck_record.get("sha256"),
                    pinned_inputs=cosim_pinned_objects,
                )
            payload = simulate_circuit_profile(
                deck_path,
                destination / stem,
                backend=normalized_backend,
                discovery=discovery,
                workdir=run_directory if selected.kind == "deck" else deck_directory,
                timeout=timeout,
                request_id=correlation_id,
                parameters=parameters,
                execution=execution,
            )

        # Apply caller-owned provenance before either a per-analysis child or
        # the final envelope can be retained.  The extension keys were cloned
        # and collision-checked at the request boundary.
        payload_extensions = payload.setdefault("data", {}).setdefault(
            "extensions", {}
        )
        payload_extensions.update(normalized_extra_extensions)
        payload_inputs = [
            dict(record)
            for record in payload.get("inputs") or ()
            if isinstance(record, Mapping)
        ]
        payload_paths = {record.get("path") for record in payload_inputs}
        for record in normalized_extra_inputs:
            if record.get("path") not in payload_paths:
                payload_inputs.insert(0, dict(record))
                payload_paths.add(record.get("path"))
        payload["inputs"] = payload_inputs
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
    return _retain_and_state_reportability(envelope, destination=destination)


#: Where a completed simulation leaves its own envelope. The typed chain -
#: ``extract`` then ``measure``/``transfer``/``spectral`` - takes the envelope as
#: a *file*, and the envelope has only ever gone to stdout. So every agent that
#: wanted a number faced a choice between saving the envelope itself and just
#: parsing the ``.raw`` with its own Python, and the second is one step shorter.
#: It was chosen four times, and each time the number was then reported as
#: "measured" with nothing in the result contract vouching for it. Retaining the
#: envelope makes the typed chain the shorter path, not the longer one.
RETAINED_RESULT_NAME = "simulate.result.json"

#: How much of a raw file's plain-text header is read to name its vectors. The
#: header precedes the binary payload and is small; the bound keeps a hostile or
#: truncated file from being read whole.
MAX_RAW_HEADER_BYTES = 64 * 1024
_RAW_VARIABLE_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(\S+)")


def _native_vector_names(raw_path: Path) -> tuple[str, ...]:
    """Return the vector names a retained raw file declares, axis first.

    ``extract`` selects by *native* vector name, and every failed attempt in
    the observed runs started with not knowing what those names were. They are
    in the raw file's own header; naming them costs one bounded read and saves
    a round of guessing.
    """

    try:
        with raw_path.open("rb") as handle:
            head = handle.read(MAX_RAW_HEADER_BYTES)
    except OSError:
        return ()
    text = head.decode("latin-1", errors="replace")
    names: list[str] = []
    collecting = False
    for line in text.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("variables:"):
            collecting = True
            continue
        if collecting:
            if lowered.startswith(("binary:", "values:")):
                break
            match = _RAW_VARIABLE_RE.match(line)
            if match is None:
                break
            names.append(match.group(1))
            if len(names) >= 64:
                break
    return tuple(names)


#: The selection ``extract`` needs, written out so the next command is a
#: command and not a schema exercise. ``--selection`` takes a *path*; every
#: observed attempt to pass the JSON inline was refused, and each refusal was a
#: turn spent not measuring anything.
SELECTION_TEMPLATE_NAME = "simulate.selection.json"

#: ngspice names a vector for what it is. The unit follows from the name, and
#: guessing it wrong is the difference between a typed measurement and another
#: ``series.selector.invalid``.
_VECTOR_UNITS = (("v(", "V"), ("i(", "A"), ("@", "A"))


def _vector_unit(name: str) -> str | None:
    lowered = name.lower()
    for prefix, unit in _VECTOR_UNITS:
        if lowered.startswith(prefix):
            return unit
    return None


#: How many raw vectors a template may name. Bounded on *vectors* rather than
#: selectors, so an AC plot -- two selectors per vector -- is not silently cut
#: to half the signals a DC plot gets.
MAX_TEMPLATE_VECTORS = 8


def _template_vectors(signals: Sequence[str]) -> list[str]:
    """Pick the vectors a template names, reserving a slot for every unit.

    Deck order is kept whenever it fits, because it is the order the author
    wrote and the order a reader expects. When the ceiling bites, though, that
    order drops the currents first -- ngspice lists every ``v(...)`` before
    every ``i(...)`` -- and the currents are the *entire* operand set for
    `low_frequency_impedance`. So a truncated template gives up its last slots
    to whichever units it would otherwise have lost.
    """

    unique: list[str] = []
    for name in signals:
        if _vector_unit(name) is not None and name not in unique:
            unique.append(name)
    if len(unique) <= MAX_TEMPLATE_VECTORS:
        return unique
    kept = unique[:MAX_TEMPLATE_VECTORS]
    present = {_vector_unit(name) for name in kept}
    missing = sorted({_vector_unit(name) for name in unique} - present)
    for offset, unit in enumerate(missing):
        if offset >= len(kept):
            break
        kept[len(kept) - 1 - offset] = next(
            name for name in unique if _vector_unit(name) == unit
        )
    return kept


def _write_selection_template(
    destination: Path, signals: Sequence[str], *, complex_plot: bool = False
) -> Path | None:
    """Write a ready-to-run ``extract`` selection and return its path.

    An AC plot is complex, and ``result.transfer.measure`` -- the only operation
    that measures a gain, an impedance or any other ratio -- takes each operand
    as a *Cartesian pair*. A real-only template therefore hands the caller a
    series no transfer request can consume. That is not hypothetical: a live job
    followed this file verbatim, extracted four real components off an AC run,
    and then computed its differential gain by hand because nothing it had could
    be fed to `openada transfer`. So an AC template emits both components.
    """

    # ngspice repeats a vector name when a net is saved twice - a `.SAVE VIN`
    # beside a `V_IN` current, say - and ``extract`` refuses both a repeated
    # native identity and a repeated output name. A template that reproduced
    # the duplicate would be refused on its first use, which is exactly the
    # friction this file exists to remove.
    components = (("real", "_re"), ("imaginary", "_im")) if complex_plot else (("real", ""),)
    selectors: list[dict[str, str]] = []
    for name in _template_vectors(signals):
        unit = _vector_unit(name)
        stem = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
        for component, suffix in components:
            selectors.append(
                {
                    "native_name": name,
                    "output_name": f"{stem}{suffix}",
                    "unit": unit,
                    "component": component,
                }
            )
    if not selectors:
        return None
    path = destination / SELECTION_TEMPLATE_NAME
    try:
        destination.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"selectors": selectors, "conditions": [], "extensions": {}}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


#: What to run *after* ``extract``, per analysis, naming the kinds rather than
#: only the commands. Naming the operation was not enough: two live jobs read
#: "then `openada measure`, `openada transfer` or `openada spectral`", reached
#: extract, and stopped -- both had been asked for a *derived ratio*, and
#: neither had any way to learn from here that a ratio is what `transfer` is.
#: An unreachable capability and an absent one cost the same turn.
_MEASUREMENT_MENU = {
    "ac": (
        "`openada transfer` on the series it returns. Its metric kinds are "
        "low_frequency_gain_db (dB), low_frequency_impedance (Ohm, output in V "
        "over input in A), bandwidth_3db, unity_gain_frequency and phase_margin. "
        "Each operand is a Cartesian {real, imaginary} pair and may add "
        "negative_real/negative_imaginary to become differential, which is how a "
        "differential gain and any other two-terminal ratio becomes one typed "
        "measurement instead of arithmetic in an answer. The amperes an "
        "impedance needs are the `i(<source>)` vectors already listed in the "
        "selection file: hold the node with a 1 V AC source and name that "
        "source's current as the input. Run `openada transfer --help` for the "
        "exact shape."
    ),
    "default": (
        "`openada measure` on the series it returns; its kinds are sample_at, "
        "minimum, maximum, mean, rms, crossing, rise_time, fall_time and "
        "settling_time, each reading ONE named signal. A ratio of two signals is "
        "not among them - for a gain, a differential gain or a driving-point "
        "impedance, run an AC analysis and use `openada transfer`. `openada "
        "spectral` covers snr, sinad, thd and sfdr on a coherent single tone."
    ),
}


def _retain_and_state_reportability(
    envelope: dict[str, Any], *, destination: Path
) -> dict[str, Any]:
    """Retain the envelope and say plainly what may be reported from it.

    ``execution.status`` and ``engineering.status`` have always carried this,
    and reading them correctly is a real skill. The failure this addresses is
    not that the information was absent - it is that a number can be lifted out
    of a ``.raw`` file and written into an answer as "Measured" without anything
    in the evidence ever contradicting it. So the envelope now states the
    permission in words, in the same object the caller is already reading.
    """

    analysis = (envelope.get("data") or {}).get("analysis") or {}
    completion = analysis.get("completion") if isinstance(analysis, Mapping) else None
    engineering = envelope.get("engineering") or {}
    status = engineering.get("status") if isinstance(engineering, Mapping) else None
    established = completion == "completed" and status in {"pass", "fail"}

    raw_paths = [
        str(record.get("path"))
        for record in envelope.get("artifacts") or ()
        if isinstance(record, Mapping) and record.get("role") == "simulation.result"
    ]
    retained = destination / RETAINED_RESULT_NAME

    claims: list[dict[str, Any]] = []
    if not established:
        claims.append(
            diagnostic(
                "error",
                "claim.measurement.unsupported",
                (
                    "This run establishes no measurable quantity: "
                    f"analysis completion is {completion!r} and engineering status is "
                    f"{status!r}. No number derived from it - including one read out of "
                    "a retained raw file - may be reported as measured, simulated or "
                    "verified. Report it as an estimate, or say the simulation did not "
                    "produce a usable result."
                ),
                hint=(
                    "Fix the reason named by the other diagnostics and re-run. A "
                    "quantity becomes reportable only once an analysis completes and a "
                    "typed measurement envelope carries it."
                ),
            )
        )
    else:
        raw = raw_paths[0] if raw_paths else "<raw>"
        vectors = _native_vector_names(Path(raw)) if raw_paths else ()
        # An operating point has no sweep, so it has no axis and every vector is
        # a signal. Every other analysis puts its axis first, and the axis is not
        # a signal: selecting it alongside the dependent vectors is refused with
        # ``series.selector.missing``, which names neither the axis nor the
        # offending selector. Stating it here is cheaper than discovering it.
        swept = analysis.get("type") != "op" if isinstance(analysis, Mapping) else True
        axis = vectors[0] if vectors and swept else None
        # ngspice saves every internal subcircuit body node alongside the nets
        # the deck named. A '#' marks one; a caller almost never wants them, and
        # listing them buries the nets that were asked for.
        candidates = vectors[1:] if swept else vectors
        signals = tuple(name for name in candidates if "#" not in name)
        analysis_type = (
            analysis.get("type") if isinstance(analysis, Mapping) else None
        )
        selection_path = _write_selection_template(
            destination, signals, complex_plot=analysis_type == "ac"
        )
        if signals and axis is not None:
            vector_note = (
                f" The retained raw declares {axis!r} as the axis - which must "
                "NOT appear in selectors - over the vectors "
                f"{', '.join(signals[:12])}."
            )
        elif signals:
            vector_note = (
                " The retained raw declares the vectors "
                f"{', '.join(signals[:12])}; an operating point has no axis."
            )
        else:
            vector_note = ""
        template = (
            f" --selection {selection_path}"
            if selection_path is not None
            else " --selection <selection.json>"
        )
        claims.append(
            diagnostic(
                "info",
                "claim.measurement.typed_chain",
                (
                    "The analysis completed, so a quantity may be reported as measured "
                    "- but only once a typed measurement envelope carries it. A number "
                    "parsed out of the raw file by hand is not one: nothing in the "
                    "result contract binds it to this run. This envelope has been "
                    f"retained at {retained}.{vector_note} Continue with: openada "
                    f"extract --simulation {retained} --artifact {raw}{template}, then "
                    f"{_MEASUREMENT_MENU[analysis_type if analysis_type in _MEASUREMENT_MENU else 'default']}"
                ),
            )
        )
    envelope["diagnostics"] = claims + list(envelope.get("diagnostics") or ())

    try:
        destination.mkdir(parents=True, exist_ok=True)
        retained.write_text(
            json.dumps(envelope, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError):
        # Retention is a convenience, never a precondition for the evidence.
        envelope["diagnostics"] = [
            diagnostic(
                "warning",
                "simulation.result.missing",
                f"The result envelope could not be retained at {retained}; the typed "
                "chain needs it as a file, so save this output before calling "
                "`openada extract`.",
            ),
            *envelope["diagnostics"],
        ]
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

    # One raw capture: the SAME bytes drive the collateral scan and the launch
    # digest, so the deck the driver is bound to is exactly the deck this scan
    # reviewed. A read failure is a typed refusal rather than an empty scan +
    # unbound launch.
    try:
        raw_source_bytes = source.read_bytes()
    except OSError as exc:
        return _refusal(
            request_id=str(uuid.uuid4()),
            backend="ngspice",
            code="input.missing",
            message=f"{source} could not be read to content-bind the run: {exc}",
        )
    deck_text = raw_source_bytes.decode("utf-8", errors="replace")
    source_digest = hashlib.sha256(raw_source_bytes).hexdigest()
    errors, advisories = _collateral_diagnostics(
        deck_text,
        workdir=run_directory,
        bound_pdk=None,
        unmanaged=unmanaged_collateral,
    )
    # A startup file binds collateral exactly as a deck does, and it does so
    # before the deck is read - which is how one PDK's Verilog-A preload reached
    # a run bound to another without appearing in any netlist. A caller who
    # names a startup file explicitly is choosing its contents, so the same
    # rules apply to it, with the same escape.
    for label, candidate in (("init file", init_file), ("system init file", system_init_file)):
        if candidate is None:
            continue
        startup_path = Path(candidate).expanduser()
        try:
            startup_text = startup_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        startup_errors, startup_advisories = _collateral_diagnostics(
            startup_text,
            workdir=startup_path.parent,
            bound_pdk=None,
            unmanaged=unmanaged_collateral,
        )
        for entry in (*startup_errors, *startup_advisories):
            entry["message"] = f"The {label} {startup_path} binds PDK collateral. " + str(
                entry.get("message", "")
            )
        errors.extend(startup_errors)
        advisories.extend(startup_advisories)
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

    # Bind the launch to the digest of the exact bytes collateral-scanned above
    # (source_digest): the driver refuses if the file changed between that scan
    # and launch.
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
        expected_source_sha256=source_digest,
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
