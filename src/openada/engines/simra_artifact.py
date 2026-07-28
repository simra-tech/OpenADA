"""Bounded reader and analysis splitter for published Simra schematic artifacts.

Simra publishes one compiled schematic as a directory containing a
``schematic.artifact.json`` descriptor, a ``design.spice`` deck, and a
``schematic.simra.json`` typed view. The descriptor carries SHA-256 digests for
every published file and a ``validation`` block whose ``simulation_handoff``
value states whether the emitted deck carries one analysis (``direct``) or more
than one (``split_required``).

This module owns the native-format binding only: it validates the descriptor,
verifies the published digests against the bytes on disk, recovers the typed
``testbench`` declaration from the view, and derives one single-analysis deck
per declared analysis. It runs no simulator and makes no engineering claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ..contract import (
    FileRecordError,
    FileRecordLimitError,
    bounded_text,
    file_record,
    stable_regular_file,
)


ARTIFACT_SCHEMA = "simra.schematic-artifact/v2"
VIEW_SCHEMA = "simra.schematic/v2"

MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
MAX_VIEW_BYTES = 64 * 1024 * 1024
MAX_DECK_BYTES = 16 * 1024 * 1024
MAX_MODELS_BYTES = 16 * 1024 * 1024
MAX_ANALYSES = 16
MAX_SAVED_NETS = 1_024
MAX_UNRESOLVED_EXAMPLES = 20
MAX_UNRESOLVED_TOKEN_DISPLAY_CHARS = 256
MAX_UNRESOLVED_MESSAGE_CHARS = 3_500
MAX_DIAGNOSTIC_NAME_CHARS = 128

SUPPORTED_ANALYSIS_KINDS = ("op", "dc", "ac", "tran")
SUPPORTED_HANDOFFS = ("direct", "split_required")

#: Every top-level analysis card ngspice recognizes, not only the supported
#: subset. An unsupported card in a published deck must be reported, never
#: silently dropped while splitting.
ANALYSIS_CARD_RE = re.compile(
    r"^\s*\.(op|dc|ac|tran|noise|hb|tf|pz|sens|sp|disto)\b",
    re.IGNORECASE,
)
UNRESOLVED_TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9_])"
    rf"SIMRA_UNRESOLVED_[A-Za-z0-9_]+"
    rf"(?![A-Za-z0-9_])"
)
_SUBCKT_CELL_RE = re.compile(r"^\s*\.subckt\s+(?P<cell>\S+)", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\s*\.ends(?:\s|$)", re.IGNORECASE)
#: Directives that would make a composed model prelude non-self-contained or
#: would terminate/redirect the deck the shared simulation profile inspects.
MODELS_FORBIDDEN_RE = re.compile(
    r"^\s*\.(inc|include|lib|control|endc|end|title|measure|meas|print|four|fft|step|op|dc|ac|tran)\b",
    re.IGNORECASE,
)
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NET_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+$\[\]-]{1,256}$")
_DIAGNOSTIC_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")
_DIAGNOSTIC_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DIAGNOSTIC_PARAMETER_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


class SimraArtifactError(Exception):
    """One bounded, typed reason a published artifact cannot be dispatched."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _UnresolvedPlaceholder:
    token: str
    cell_name: str | None


@dataclass(frozen=True, slots=True)
class DerivedDeck:
    """One single-analysis deck derived from a published multi-analysis deck."""

    index: int
    kind: str
    analysis: dict[str, Any]
    text: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SimraTestbench:
    """One validated, digest-bound Simra testbench artifact."""

    descriptor_path: Path
    directory: Path
    identifier: str
    label: str
    top: str
    netlist_path: Path
    view_path: Path
    analyses: tuple[dict[str, Any], ...]
    saved_nets: tuple[str, ...]
    simulation_handoff: str
    parameters_state: str
    #: Simra's ``validation.simulation_ready``: the deck runs with no external
    #: model collateral. False for every MOS testbench by construction.
    self_contained: bool
    netlist_sha256: str
    view_sha256: str
    source_sha256: str
    deck_text: str
    input_records: tuple[dict[str, Any], ...] = field(default=())

    @property
    def dispatch_mode(self) -> str:
        return "split" if len(self.analyses) > 1 else "direct"


def _read_json_document(
    path: Path,
    *,
    role: str,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Return one bounded JSON object with its digest and retained file record."""

    try:
        record = file_record(
            path,
            kind="simra-artifact",
            role=role,
            maximum_bytes=maximum_bytes,
        )
    except FileRecordLimitError as exc:
        raise SimraArtifactError(
            "testbench.artifact.over_limit",
            f"The Simra {role} exceeds the bounded {maximum_bytes}-byte limit: {exc.observed_bytes} bytes.",
        ) from exc
    except FileRecordError as exc:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} could not be captured as one stable regular file: {path}",
        ) from exc
    if not record["exists"]:
        raise SimraArtifactError(
            "testbench.artifact.missing",
            f"The Simra {role} is not a readable regular file: {path}",
        )

    try:
        with stable_regular_file(path) as (handle, _opened):
            raw = handle.read(maximum_bytes + 1)
    except FileRecordError as exc:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} changed during bounded capture: {path}",
        ) from exc
    if len(raw) > maximum_bytes:
        raise SimraArtifactError(
            "testbench.artifact.over_limit",
            f"The Simra {role} exceeds the bounded {maximum_bytes}-byte limit.",
        )
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} changed between digest capture and read: {path}",
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The Simra {role} is not one valid UTF-8 JSON document: {path}",
        ) from exc
    if not isinstance(document, dict):
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The Simra {role} is not a JSON object: {path}",
        )
    return document, record["sha256"], record


def _read_text_file(
    path: Path,
    *,
    kind: str,
    role: str,
    maximum_bytes: int,
) -> tuple[str, str, dict[str, Any]]:
    try:
        record = file_record(path, kind=kind, role=role, maximum_bytes=maximum_bytes)
    except FileRecordLimitError as exc:
        raise SimraArtifactError(
            "testbench.artifact.over_limit",
            f"The Simra {role} exceeds the bounded {maximum_bytes}-byte limit: {exc.observed_bytes} bytes.",
        ) from exc
    except FileRecordError as exc:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} could not be captured as one stable regular file: {path}",
        ) from exc
    if not record["exists"]:
        raise SimraArtifactError(
            "testbench.artifact.missing",
            f"The Simra {role} is not a readable regular file: {path}",
        )
    try:
        with stable_regular_file(path) as (handle, _opened):
            raw = handle.read(maximum_bytes + 1)
    except FileRecordError as exc:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} changed during bounded capture: {path}",
        ) from exc
    if len(raw) > maximum_bytes:
        raise SimraArtifactError(
            "testbench.artifact.over_limit",
            f"The Simra {role} exceeds the bounded {maximum_bytes}-byte limit.",
        )
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise SimraArtifactError(
            "testbench.artifact.unstable",
            f"The Simra {role} changed between digest capture and read: {path}",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The Simra {role} is not valid UTF-8: {path}",
        ) from exc
    return text, record["sha256"], record


def _sibling(directory: Path, value: object, *, field_name: str) -> Path:
    if not isinstance(value, str) or _PATH_COMPONENT_RE.fullmatch(value) is None:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The descriptor {field_name!r} must be one bounded published file name in the artifact directory.",
        )
    candidate = directory / value
    if candidate.parent != directory:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The descriptor {field_name!r} must not escape the artifact directory.",
        )
    return candidate


def _diagnostic_name(value: object) -> str:
    if isinstance(value, str) and _DIAGNOSTIC_NAME_RE.fullmatch(value) is not None:
        return bounded_text(value, limit=MAX_DIAGNOSTIC_NAME_CHARS)
    return "unknown"


def _diagnostic_kind(value: object) -> str:
    if isinstance(value, str) and _DIAGNOSTIC_KIND_RE.fullmatch(value) is not None:
        return value
    return "unknown"


def _artifact_local_location(
    source: object,
    *,
    descriptor_source: object,
) -> str:
    """Return a bounded source locator that cannot disclose a host path."""

    published_name = (
        descriptor_source
        if isinstance(descriptor_source, str)
        and _PATH_COMPONENT_RE.fullmatch(descriptor_source) is not None
        else None
    )
    line: int | None = None
    column: int | None = None
    if isinstance(source, Mapping):
        source_name = source.get("artifact")
        if (
            isinstance(source_name, str)
            and _PATH_COMPONENT_RE.fullmatch(source_name) is not None
        ):
            published_name = source_name
        candidate_line = source.get("line")
        if (
            isinstance(candidate_line, int)
            and not isinstance(candidate_line, bool)
            and 1 <= candidate_line <= 1_000_000_000
        ):
            line = candidate_line
        candidate_column = source.get("column")
        if (
            isinstance(candidate_column, int)
            and not isinstance(candidate_column, bool)
            and 0 <= candidate_column <= 1_000_000_000
        ):
            column = candidate_column

    location = (
        f"artifact-local {published_name}"
        if published_name is not None
        else "the artifact-local source"
    )
    if line is not None:
        location += f":{line}"
        if column is not None:
            location += f":{column}"
    return location


def _instance_placeholder_prefix(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    return f"SIMRA_UNRESOLVED_{token}_"


def _unresolved_placeholders(
    deck_text: str,
    *,
    top_cell: str,
) -> list[_UnresolvedPlaceholder]:
    """Return each distinct placeholder with its netlist cell context."""

    cell_stack: list[str] = []
    seen: set[tuple[str | None, str]] = set()
    unresolved: list[_UnresolvedPlaceholder] = []
    for line in deck_text.splitlines():
        subcircuit = _SUBCKT_CELL_RE.match(line)
        if subcircuit is not None:
            cell_stack.append(subcircuit.group("cell"))
        cell_name = cell_stack[-1] if cell_stack else top_cell
        for token in dict.fromkeys(UNRESOLVED_TOKEN_RE.findall(line)):
            key = (cell_name.casefold(), token)
            if key not in seen:
                seen.add(key)
                unresolved.append(
                    _UnresolvedPlaceholder(token=token, cell_name=cell_name)
                )
        if _ENDS_RE.match(line) is not None and cell_stack:
            cell_stack.pop()
    return unresolved


def _unresolved_parameter_diagnostic(
    *,
    parameters_state: object,
    unresolved: list[_UnresolvedPlaceholder],
    view_document: Mapping[str, Any],
    descriptor_source: object,
) -> tuple[str, str]:
    """Describe unresolved authoring values using only bounded artifact-local facts."""

    examples = unresolved[:MAX_UNRESOLVED_EXAMPLES]
    best_matches: dict[
        int,
        tuple[tuple[int, int], list[dict[str, str]]],
    ] = {}
    nonresolved_instances: list[dict[str, str]] = []

    cells = view_document.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, Mapping):
                continue
            raw_cell_name = cell.get("name")
            cell_name = _diagnostic_name(raw_cell_name)
            raw_cell_kind = cell.get("kind")
            if raw_cell_kind == "design":
                cell_role = "circuit"
            elif raw_cell_kind == "testbench":
                cell_role = "testbench"
            else:
                cell_role = "published"
            entities = cell.get("entities")
            if not isinstance(entities, Mapping):
                continue
            instances = entities.get("instances")
            if not isinstance(instances, list):
                continue
            for instance in instances:
                if not isinstance(instance, Mapping):
                    continue
                raw_name = instance.get("name")
                if (
                    not isinstance(raw_name, str)
                    or _DIAGNOSTIC_NAME_RE.fullmatch(raw_name) is None
                ):
                    continue
                record = {
                    "cell_name": cell_name,
                    "cell_role": cell_role,
                    "device_kind": _diagnostic_kind(instance.get("kind")),
                    "instance_name": _diagnostic_name(raw_name),
                    "location": _artifact_local_location(
                        instance.get("source"),
                        descriptor_source=descriptor_source,
                    ),
                }
                parameter_status = instance.get("parameter_status")
                is_nonresolved = (
                    parameter_status == "partial"
                    or parameter_status == "unresolved"
                )
                if (
                    is_nonresolved
                    and len(nonresolved_instances) < MAX_UNRESOLVED_EXAMPLES
                ):
                    nonresolved_instances.append(record)

                prefix = _instance_placeholder_prefix(raw_name)
                for occurrence_index, occurrence in enumerate(examples):
                    if (
                        occurrence.cell_name is not None
                        and (
                            not isinstance(raw_cell_name, str)
                            or raw_cell_name.casefold()
                            != occurrence.cell_name.casefold()
                        )
                    ):
                        continue
                    token = occurrence.token
                    if not token.startswith(prefix):
                        continue
                    parameter = token[len(prefix) :]
                    if _DIAGNOSTIC_PARAMETER_RE.fullmatch(parameter) is None:
                        continue
                    candidate = dict(record)
                    candidate["parameter"] = parameter.casefold()
                    # A full instance-name match wins over a shorter prefix
                    # (M_1 over M). Netlist cell context disambiguates equal
                    # instance names in separate subcircuits. The view's
                    # non-resolved status breaks any remaining equal-name tie
                    # while still allowing a deck-tamper refusal to map a token
                    # whose view status remains "resolved".
                    rank = (len(prefix), 1 if is_nonresolved else 0)
                    current = best_matches.get(occurrence_index)
                    if current is None or rank > current[0]:
                        best_matches[occurrence_index] = (rank, [candidate])
                    elif rank == current[0] and len(current[1]) < MAX_UNRESOLVED_EXAMPLES:
                        current[1].append(candidate)

    grouped: dict[tuple[str, str, str, str, str], list[str]] = {}
    mapped: set[int] = set()
    ambiguous: dict[int, list[dict[str, str]]] = {}
    for occurrence_index, _occurrence in enumerate(examples):
        match = best_matches.get(occurrence_index)
        if match is None:
            continue
        distinct = {
            (
                candidate["cell_role"],
                candidate["cell_name"],
                candidate["device_kind"],
                candidate["instance_name"],
                candidate["location"],
            ): candidate
            for candidate in match[1]
        }
        if len(distinct) != 1:
            ambiguous[occurrence_index] = list(distinct.values())
            continue
        candidate = next(iter(distinct.values()))
        key = (
            candidate["cell_role"],
            candidate["cell_name"],
            candidate["device_kind"],
            candidate["instance_name"],
            candidate["location"],
        )
        parameters = grouped.setdefault(key, [])
        parameter = candidate["parameter"]
        if parameter not in parameters:
            parameters.append(parameter)
        mapped.add(occurrence_index)

    if unresolved:
        details = [
            (
                f"{device_kind} instance {instance_name!r} in {cell_role} "
                f"cell {cell_name!r} at {location} has unset parameters: "
                + ", ".join(parameters)
            )
            for (
                cell_role,
                cell_name,
                device_kind,
                instance_name,
                location,
            ), parameters in grouped.items()
        ]
        message = (
            f"The published artifact carries {len(unresolved)} unresolved "
            "parameter placeholder(s)"
        )
        if details:
            message += ": " + "; ".join(details)
        if ambiguous:
            candidates = []
            for occurrence_index, records in ambiguous.items():
                locations = ", ".join(
                    (
                        f"{item['device_kind']} instance "
                        f"{item['instance_name']!r} in {item['cell_role']} cell "
                        f"{item['cell_name']!r} at {item['location']}"
                    )
                    for item in records
                )
                candidates.append(
                    f"{bounded_text(examples[occurrence_index].token, limit=MAX_UNRESOLVED_TOKEN_DISPLAY_CHARS)}: "
                    f"{locations}"
                )
            message += (
                "; placeholder(s) with ambiguous view ownership: "
                + "; ".join(candidates)
            )
        unmapped = [
            occurrence.token
            for occurrence_index, occurrence in enumerate(examples)
            if occurrence_index not in mapped and occurrence_index not in ambiguous
        ]
        if unmapped:
            message += (
                "; placeholder(s) without bounded instance metadata: "
                + ", ".join(
                    bounded_text(
                        token,
                        limit=MAX_UNRESOLVED_TOKEN_DISPLAY_CHARS,
                    )
                    for token in unmapped
                )
            )
        omitted = len(unresolved) - len(examples)
        if omitted:
            message += (
                f"; {omitted} additional placeholder(s) omitted by the "
                f"{MAX_UNRESOLVED_EXAMPLES}-placeholder detail limit"
            )
        message += "."
        hint = (
            "Set the listed parameters at the named artifact-local source "
            "locations and recompile; this driver will not invent authoring values."
        )
        return bounded_text(message, limit=MAX_UNRESOLVED_MESSAGE_CHARS), hint

    state = (
        repr(parameters_state)
        if isinstance(parameters_state, str)
        and parameters_state in {"partial", "unresolved"}
        else "'invalid'"
    )
    message = (
        f"The published artifact reports validation.parameters={state}, but its "
        "digest-bound deck carries no unresolved parameter placeholders"
    )
    if nonresolved_instances:
        details = [
            (
                f"{item['device_kind']} instance {item['instance_name']!r} in "
                f"{item['cell_role']} cell {item['cell_name']!r} at "
                f"{item['location']}"
            )
            for item in nonresolved_instances
        ]
        message += "; the view marks these instances non-resolved: " + "; ".join(
            details
        )
    else:
        message += " and the view identifies no non-resolved instance"
    message += "."
    hint = (
        "Recompile the named artifact-local source so the view and deck publish "
        "matching unresolved parameter names; this driver will not invent "
        "authoring values."
    )
    return bounded_text(message, limit=MAX_UNRESOLVED_MESSAGE_CHARS), hint


def _typed_analysis(entry: object, position: int) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise SimraArtifactError(
            "testbench.analyses.invalid",
            f"Declared analysis {position} is not a JSON object.",
        )
    kind = entry.get("kind")
    if kind not in SUPPORTED_ANALYSIS_KINDS:
        raise SimraArtifactError(
            "testbench.analyses.unsupported",
            f"Declared analysis {position} has kind {kind!r}; "
            f"only {', '.join(SUPPORTED_ANALYSIS_KINDS)} are dispatchable.",
        )
    normalized: dict[str, Any] = {}
    for name, value in entry.items():
        if not isinstance(name, str) or len(name) > 64:
            raise SimraArtifactError(
                "testbench.analyses.invalid",
                f"Declared analysis {position} has an unbounded field name.",
            )
        if isinstance(value, str):
            if len(value) > 128:
                raise SimraArtifactError(
                    "testbench.analyses.invalid",
                    f"Declared analysis {position} field {name!r} is unbounded.",
                )
            normalized[name] = value
        elif isinstance(value, bool) or value is None:
            raise SimraArtifactError(
                "testbench.analyses.invalid",
                f"Declared analysis {position} field {name!r} is not a SPICE-typed value.",
            )
        elif isinstance(value, (int, float)):
            normalized[name] = value
        else:
            raise SimraArtifactError(
                "testbench.analyses.invalid",
                f"Declared analysis {position} field {name!r} is not a SPICE-typed value.",
            )
    return normalized


def load_simra_testbench(descriptor_file: str | Path) -> SimraTestbench:
    """Return one digest-verified, dispatchable Simra testbench artifact.

    Raises :class:`SimraArtifactError` with a stable diagnostic code when the
    artifact is absent, unstable, unbound, not a testbench, or carries anything
    the bounded simulation profiles cannot honestly run.
    """

    descriptor_path = Path(descriptor_file).expanduser()
    if not descriptor_path.is_absolute():
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            f"The artifact descriptor locator must be an absolute path: {descriptor_path}",
        )
    directory = descriptor_path.parent
    descriptor, _descriptor_sha, descriptor_record = _read_json_document(
        descriptor_path,
        role="artifact-descriptor",
        maximum_bytes=MAX_DESCRIPTOR_BYTES,
    )

    if descriptor.get("schema") != ARTIFACT_SCHEMA:
        raise SimraArtifactError(
            "testbench.artifact.unsupported_schema",
            f"Expected a {ARTIFACT_SCHEMA} descriptor, got {descriptor.get('schema')!r}.",
        )
    if descriptor.get("kind") != "testbench":
        raise SimraArtifactError(
            "testbench.artifact.not_a_testbench",
            f"The published artifact kind is {descriptor.get('kind')!r}; "
            "only a testbench declares analyses to dispatch.",
        )

    hashes = descriptor.get("hashes")
    if not isinstance(hashes, Mapping):
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            "The descriptor publishes no hashes object.",
        )
    validation = descriptor.get("validation")
    if not isinstance(validation, Mapping):
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            "The descriptor publishes no validation object.",
        )

    identifier = descriptor.get("id")
    top = descriptor.get("top")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            "The descriptor publishes no bounded artifact id.",
        )
    if not isinstance(top, str) or not top or len(top) > 256:
        raise SimraArtifactError(
            "testbench.artifact.invalid",
            "The descriptor publishes no bounded top cell name.",
        )
    label = descriptor.get("label")
    if not isinstance(label, str) or len(label) > 512:
        label = identifier

    netlist_path = _sibling(directory, descriptor.get("netlist"), field_name="netlist")
    view_path = _sibling(directory, descriptor.get("view"), field_name="view")

    if validation.get("netlistable") is not True or descriptor.get("netlistable") is not True:
        raise SimraArtifactError(
            "testbench.artifact.not_netlistable",
            "The published artifact does not claim a runnable netlist.",
        )
    # Report the actionable parameter state before the derived readiness flag:
    # Simra clears simulation_ready whenever parameters are partial, and
    # "simulation_ready=false" tells an author nothing about what to bind.
    parameters_state = validation.get("parameters")
    # Preserve the existing handoff refusal precedence for resolved artifacts.
    # A partial artifact still takes the parameter refusal, after its published
    # deck/view have supplied the actionable explanation.
    self_contained = validation.get("simulation_ready") is True
    handoff = validation.get("simulation_handoff")
    if parameters_state == "resolved" and handoff not in SUPPORTED_HANDOFFS:
        raise SimraArtifactError(
            "testbench.handoff.unsupported",
            f"The published artifact reports simulation_handoff={handoff!r}; "
            f"only {' and '.join(SUPPORTED_HANDOFFS)} are dispatchable.",
        )

    deck_text, netlist_sha256, netlist_record = _read_text_file(
        netlist_path,
        kind="spice-netlist",
        role="netlist",
        maximum_bytes=MAX_DECK_BYTES,
    )
    view_document, view_sha256, view_record = _read_json_document(
        view_path,
        role="schematic-view",
        maximum_bytes=MAX_VIEW_BYTES,
    )

    published_netlist_sha = hashes.get("netlist_sha256")
    published_view_sha = hashes.get("view_sha256")
    if published_netlist_sha != netlist_sha256:
        raise SimraArtifactError(
            "testbench.artifact.digest_mismatch",
            f"{netlist_path} hashes to {netlist_sha256} but the descriptor publishes "
            f"{published_netlist_sha!r}.",
        )
    if published_view_sha != view_sha256:
        raise SimraArtifactError(
            "testbench.artifact.digest_mismatch",
            f"{view_path} hashes to {view_sha256} but the descriptor publishes "
            f"{published_view_sha!r}.",
        )
    source_sha256 = hashes.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        source_sha256 = ""

    if view_document.get("schema") != VIEW_SCHEMA:
        raise SimraArtifactError(
            "testbench.artifact.unsupported_schema",
            f"Expected a {VIEW_SCHEMA} view, got {view_document.get('schema')!r}.",
        )

    # Simra publishes the exact missing parameter names in the deck and the
    # owning instance/cell/source location in the typed view. Read and
    # digest-check both before refusing so the author sees what and where to
    # edit, without echoing the descriptor's absolute host location.
    unresolved = _unresolved_placeholders(deck_text, top_cell=top)
    if parameters_state != "resolved" or unresolved:
        message, hint = _unresolved_parameter_diagnostic(
            parameters_state=parameters_state,
            unresolved=unresolved,
            view_document=view_document,
            descriptor_source=descriptor.get("source"),
        )
        raise SimraArtifactError(
            "testbench.parameters.unresolved",
            message,
            hint=hint,
        )

    # ``simulation_ready`` is narrower than its name suggests: Simra clears it
    # for every testbench containing a MOS instance, because model collateral is
    # deliberately outside the schematic contract. It therefore means "runs with
    # no external collateral", not "is fit to simulate". Carry it as a fact and
    # let the operation decide whether the caller supplied the missing models.

    testbench = view_document.get("testbench")
    if not isinstance(testbench, Mapping):
        raise SimraArtifactError(
            "testbench.declaration.missing",
            "The published view carries no typed testbench declaration.",
        )
    declared = testbench.get("analyses")
    if not isinstance(declared, list) or not declared:
        raise SimraArtifactError(
            "testbench.declaration.missing",
            "The published testbench declares no analyses.",
        )
    if len(declared) > MAX_ANALYSES:
        raise SimraArtifactError(
            "testbench.analyses.over_limit",
            f"The published testbench declares {len(declared)} analyses; "
            f"the bounded ceiling is {MAX_ANALYSES}.",
        )
    analyses = tuple(
        _typed_analysis(entry, position) for position, entry in enumerate(declared)
    )
    if (handoff == "direct") != (len(analyses) == 1):
        raise SimraArtifactError(
            "testbench.handoff.inconsistent",
            f"simulation_handoff={handoff!r} disagrees with {len(analyses)} declared analyses.",
        )

    saved = testbench.get("save", [])
    if not isinstance(saved, list) or len(saved) > MAX_SAVED_NETS:
        raise SimraArtifactError(
            "testbench.declaration.invalid",
            "The published testbench save list is missing or unbounded.",
        )
    saved_nets: list[str] = []
    for name in saved:
        if not isinstance(name, str) or _NET_NAME_RE.fullmatch(name) is None:
            raise SimraArtifactError(
                "testbench.declaration.invalid",
                "The published testbench save list contains an unbounded net name.",
            )
        saved_nets.append(name)

    return SimraTestbench(
        descriptor_path=Path(descriptor_record["path"]),
        directory=directory,
        identifier=identifier,
        label=label,
        top=top,
        netlist_path=Path(netlist_record["path"]),
        view_path=Path(view_record["path"]),
        analyses=analyses,
        saved_nets=tuple(saved_nets),
        simulation_handoff=handoff,
        parameters_state=parameters_state,
        self_contained=self_contained,
        netlist_sha256=netlist_sha256,
        view_sha256=view_sha256,
        source_sha256=source_sha256,
        deck_text=deck_text,
        input_records=(descriptor_record, netlist_record, view_record),
    )


def _deck_lines(deck_text: str) -> list[str]:
    return deck_text.splitlines(keepends=True)


def load_model_prelude(models_file: str | Path) -> tuple[str, dict[str, Any]]:
    """Return one self-contained model-card prelude and its retained record.

    A Simra deck names device models but never emits ``.include`` or ``.lib``,
    so a model-bound testbench needs its model cards supplied. The bounded
    shared simulation profile accepts no include directive, so the prelude must
    itself be self-contained; a hierarchical PDK entry file is refused rather
    than silently escalated to an uninspected include chain.
    """

    path = Path(models_file).expanduser()
    if not path.is_absolute():
        raise SimraArtifactError(
            "configuration.models.invalid",
            f"The model-library locator must be an absolute path: {path}",
        )
    text, _digest, record = _read_text_file(
        path,
        kind="spice-model-library",
        role="model-library",
        maximum_bytes=MAX_MODELS_BYTES,
    )
    for number, line in enumerate(text.splitlines(), start=1):
        if MODELS_FORBIDDEN_RE.match(line):
            raise SimraArtifactError(
                "configuration.models.not_self_contained",
                f"{path}:{number} declares a directive this bounded profile cannot accept: "
                f"{line.strip()[:120]}",
                hint=(
                    "Supply a flattened self-contained model card set. A hierarchical PDK "
                    "entry file with .include or .lib is outside testbench.simulate/v1alpha1."
                ),
            )
    if not text.endswith("\n"):
        text += "\n"
    return text, record


def derive_single_analysis_decks(
    testbench: SimraTestbench,
    *,
    model_prelude: str | None = None,
) -> tuple[DerivedDeck, ...]:
    """Split one published deck into exactly one deck per declared analysis.

    The published deck's ordered top-level analysis cards must correspond
    one-to-one with the typed ``testbench.analyses`` declaration. Every other
    line, including the title, subcircuit definitions, sources, ``.SAVE``, and
    ``.END``, is carried through byte-for-byte.
    """

    lines = _deck_lines(testbench.deck_text)
    card_positions: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        # The first line of a SPICE deck is its title, never a directive; the
        # shared profile's own inspection skips it, so splitting must agree.
        if index == 0:
            continue
        match = ANALYSIS_CARD_RE.match(line)
        if match is not None:
            card_positions.append((index, match.group(1).lower()))

    declared_kinds = [analysis["kind"] for analysis in testbench.analyses]
    observed_kinds = [kind for _position, kind in card_positions]
    if observed_kinds != declared_kinds:
        raise SimraArtifactError(
            "testbench.deck.mismatch",
            "The published deck's analysis cards "
            f"({', '.join(observed_kinds) or 'none'}) do not match the typed declaration "
            f"({', '.join(declared_kinds)}).",
            hint="Recompile the testbench so the emitted deck and its declaration agree.",
        )

    prelude_lines: list[str] = []
    if model_prelude:
        prelude_lines = _deck_lines(model_prelude)

    derived: list[DerivedDeck] = []
    for selected, (position, kind) in enumerate(card_positions):
        dropped = {other for other, _kind in card_positions} - {position}
        kept: list[str] = []
        for index, line in enumerate(lines):
            if index in dropped:
                continue
            kept.append(line)
            if index == 0 and prelude_lines:
                if not line.endswith("\n"):
                    kept[-1] = line + "\n"
                kept.extend(prelude_lines)
        text = "".join(kept)
        if not text.endswith("\n"):
            text += "\n"
        derived.append(
            DerivedDeck(
                index=selected,
                kind=kind,
                analysis=dict(testbench.analyses[selected]),
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(derived)


def deck_file_name(deck: DerivedDeck, *, total: int) -> str:
    """Return one stable, ordered, collision-free derived-deck file name."""

    width = max(2, len(str(total)))
    return f"analysis-{deck.index + 1:0{width}d}-{deck.kind}.spice"


__all__ = [
    "ANALYSIS_CARD_RE",
    "ARTIFACT_SCHEMA",
    "DerivedDeck",
    "MAX_ANALYSES",
    "MAX_DECK_BYTES",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_MODELS_BYTES",
    "MAX_VIEW_BYTES",
    "SUPPORTED_ANALYSIS_KINDS",
    "SUPPORTED_HANDOFFS",
    "SimraArtifactError",
    "SimraTestbench",
    "VIEW_SCHEMA",
    "deck_file_name",
    "derive_single_analysis_decks",
    "load_model_prelude",
    "load_simra_testbench",
]
