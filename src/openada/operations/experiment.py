"""Closed, provenance-bound ``simra.experiment/v1`` execution.

An experiment specification is an intermediate representation, not a SPICE
template.  This module validates that closed representation completely before
creating an evidence directory or launching a simulator, serializes one
portable single-analysis deck per declared analysis, and then reuses the
existing simulate -> extract -> measure operations without weakening any of
their contracts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

from ..contract import (
    FileRecordError,
    bounded_text,
    diagnostic,
    file_record,
    result,
    stable_regular_file,
    static_execution,
)
from ..discovery import DiscoveryManager
from ..engines.simra_artifact import SimraArtifactError
from ..pdk_bindings import (
    PdkBindingError,
    REGISTRY,
    ResolvedPdkBinding,
    bind_deck,
    resolve_pdk_binding,
)
from ..provider_runtime import ProviderRuntimeError, load_operation_profile
from .circuit_simulate import MAX_SHARED_ANALYSIS_POINTS
from .result_measure import measure_result
from .result_series_extract import extract_result_series
from .result_spectral_measure import measure_spectrum
from .result_transfer_measure import measure_transfer
from .simulate import simulate


EXPERIMENT_SCHEMA = "simra.experiment/v1"
EXPERIMENT_RUN_SCHEMA = "simra.experiment-run/v1"
COMPOSER_VERSION = "openada.experiment.composer/v1"
OPERATION_NAME = "experiment.run"
EXPERIMENT_EXTENSION = "org.openada.experiment"

MAX_SPEC_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 512
MAX_ANALYSES = 16
MAX_OBSERVATIONS = 32
MAX_MEASUREMENTS = 128
MAX_DERIVATIONS = 128
MAX_DEBUG_SAVES = 128
MAX_RAW_HEADER_BYTES = 256 * 1024

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SPICE_NUMBER_RE = re.compile(
    r"(?P<number>[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>meg|mil|[tgkmunpf])?",
    re.IGNORECASE,
)
_SPICE_SCALE = {
    "": Decimal(1),
    "t": Decimal("1e12"),
    "g": Decimal("1e9"),
    "meg": Decimal("1e6"),
    "k": Decimal("1e3"),
    "mil": Decimal("25.4e-6"),
    "m": Decimal("1e-3"),
    "u": Decimal("1e-6"),
    "n": Decimal("1e-9"),
    "p": Decimal("1e-12"),
    "f": Decimal("1e-15"),
}

_PULSE_PARAMETERS = (
    "initial_value",
    "pulsed_value",
    "delay_time",
    "rise_time",
    "fall_time",
    "pulse_width",
    "period",
)
_SINE_PARAMETERS = ("dc", "amplitude", "freq", "delay", "damping")
_AC_PARAMETERS = ("ac_mag", "ac_phase")
_ELEMENT_REQUIRED: Mapping[str, frozenset[str]] = {
    "vdc": frozenset(("dc",)),
    "idc": frozenset(("dc",)),
    "vpulse": frozenset(_PULSE_PARAMETERS),
    "ipulse": frozenset(_PULSE_PARAMETERS),
    "vsin": frozenset(_SINE_PARAMETERS),
    "isin": frozenset(_SINE_PARAMETERS),
    "vpwl": frozenset(("dc", "points")),
    "ipwl": frozenset(("dc", "points")),
    "resistor": frozenset(("r",)),
    "capacitor": frozenset(("c",)),
    "inductor": frozenset(("l",)),
}
_ELEMENT_ALLOWED: Mapping[str, frozenset[str]] = {
    "vdc": frozenset(("dc", *_AC_PARAMETERS)),
    "idc": frozenset(("dc", *_AC_PARAMETERS)),
    "vpulse": frozenset((*_PULSE_PARAMETERS, *_AC_PARAMETERS)),
    "ipulse": frozenset((*_PULSE_PARAMETERS, *_AC_PARAMETERS)),
    "vsin": frozenset((*_SINE_PARAMETERS, *_AC_PARAMETERS)),
    "isin": frozenset((*_SINE_PARAMETERS, *_AC_PARAMETERS)),
    "vpwl": frozenset(("dc", "points", *_AC_PARAMETERS)),
    "ipwl": frozenset(("dc", "points", *_AC_PARAMETERS)),
    "resistor": frozenset(("r",)),
    "capacitor": frozenset(("c", "ic")),
    "inductor": frozenset(("l",)),
}
_ELEMENT_PREFIX = {
    "vdc": "V",
    "idc": "I",
    "vpulse": "V",
    "ipulse": "I",
    "vsin": "V",
    "isin": "I",
    "vpwl": "V",
    "ipwl": "I",
    "resistor": "R",
    "capacitor": "C",
    "inductor": "L",
}
_VOLTAGE_SOURCE_KINDS = frozenset(("vdc", "vpulse", "vsin", "vpwl"))
_INDEPENDENT_SOURCE_KINDS = frozenset(
    ("vdc", "idc", "vpulse", "ipulse", "vsin", "isin", "vpwl", "ipwl")
)
_SUPPORTED_MEASUREMENT_PROFILES = {
    "openada.operation/result.measure/v1alpha1": ("measurement", "measure"),
    "openada.operation/result.transfer.measure/v1alpha1": ("transfer", "transfer"),
    "openada.operation/result.spectral.measure/v1alpha1": ("spectral", "spectral"),
}
_TRANSFER_UNITS = {
    "low_frequency_gain_db": "dB",
    "low_frequency_impedance": "Ohm",
    "bandwidth_3db": "Hz",
    "unity_gain_frequency": "Hz",
    "phase_margin": "deg",
}
_ANALYSIS_AXIS_UNITS = {"op": "1", "dc_v": "V", "dc_a": "A", "ac": "Hz", "tran": "s"}


@dataclass(frozen=True, slots=True)
class ExperimentIssue:
    code: str
    path: str
    message: str
    cause_code: str | None = None

    def record(self) -> dict[str, str]:
        item = {
            "code": self.code,
            "path": self.path,
            "message": bounded_text(self.message),
        }
        if self.cause_code:
            item["cause_code"] = self.cause_code
        return item

    def envelope_diagnostic(self) -> dict[str, str]:
        cause = f"; underlying cause: {self.cause_code}" if self.cause_code else ""
        return diagnostic(
            "error",
            self.code,
            f"{self.path}: {self.message}{cause}",
            hint=f"JSON Pointer: {self.path}",
        )


@dataclass(frozen=True, slots=True)
class Scalar:
    token: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class Element:
    name: str
    emitted_name: str
    kind: str
    plus: str
    minus: str
    parameters: Mapping[str, Scalar | tuple[tuple[Scalar, Scalar], ...]]


@dataclass(frozen=True, slots=True)
class Analysis:
    identifier: str
    kind: str
    document: Mapping[str, Any]
    card: str
    axis_unit: str


@dataclass(frozen=True, slots=True)
class Observation:
    identifier: str
    analysis_id: str
    kind: str
    native_name: str
    component: str
    unit: str
    net: str | None = None
    element: str | None = None

    @property
    def selector(self) -> dict[str, str]:
        return {
            "native_name": self.native_name,
            "output_name": self.identifier,
            "unit": self.unit,
            "component": self.component,
        }


@dataclass(frozen=True, slots=True)
class Measurement:
    identifier: str
    analysis_id: str
    operation_profile: str
    request: Mapping[str, Any]
    expected_unit: str | None


@dataclass(frozen=True, slots=True)
class Derivation:
    identifier: str
    analysis_id: str
    parents: tuple[str, str]


@dataclass(frozen=True, slots=True)
class PreparedRun:
    analysis: Analysis
    observations: tuple[Observation, ...]
    saved_nets: tuple[str, ...]
    retained_current_sources: tuple[str, ...]
    portable_deck: str
    portable_sha256: str
    bound_deck_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedExperiment:
    spec_path: Path
    spec_bytes: bytes
    spec_document: Mapping[str, Any]
    spec_raw_sha256: str
    spec_canonical_sha256: str
    identifier: str
    bundle: Any
    resolved_pdk: ResolvedPdkBinding
    pdk_root: Path
    elements: tuple[Element, ...]
    analyses: tuple[Analysis, ...]
    observations: tuple[Observation, ...]
    measurements: tuple[Measurement, ...]
    derivations: tuple[Derivation, ...]
    debug_save: tuple[str, ...]
    base_deck: str
    base_deck_sha256: str
    runs: tuple[PreparedRun, ...]


class _JSONObject(list):
    """Marker used to preserve duplicate keys during JSON decoding."""


def _pointer(parts: Sequence[object]) -> str:
    if not parts:
        return ""
    escaped = [
        str(part).replace("~", "~0").replace("/", "~1")
        for part in parts
    ]
    return "/" + "/".join(escaped)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n"
    ).encode("utf-8")


def _read_spec_bytes(path: Path) -> bytes:
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size > MAX_SPEC_BYTES:
                raise ValueError(
                    f"the experiment specification exceeds {MAX_SPEC_BYTES} bytes"
                )
            body = handle.read(MAX_SPEC_BYTES + 1)
    except FileRecordError as exc:
        raise ValueError(f"the experiment specification is not a stable regular file: {exc}") from exc
    if len(body) > MAX_SPEC_BYTES:
        raise ValueError(f"the experiment specification exceeds {MAX_SPEC_BYTES} bytes")
    return body


def _decode_json(
    raw: bytes,
) -> tuple[object | None, list[ExperimentIssue]]:
    issues: list[ExperimentIssue] = []
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [
            ExperimentIssue(
                "experiment.document.invalid",
                "",
                f"the specification is not valid UTF-8: {exc}",
            )
        ]
    try:
        pairs = json.loads(
            decoded,
            object_pairs_hook=_JSONObject,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value!r} is forbidden")
            ),
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        return None, [
            ExperimentIssue(
                "experiment.document.invalid",
                "",
                f"the specification is not one valid strict JSON document: {exc}",
            )
        ]

    def convert(value: object, parts: tuple[object, ...]) -> object:
        if isinstance(value, _JSONObject):
            output: dict[str, object] = {}
            seen: set[str] = set()
            for key, child in value:
                if not isinstance(key, str):
                    issues.append(
                        ExperimentIssue(
                            "experiment.document.invalid",
                            _pointer(parts),
                            "JSON object keys must be strings",
                        )
                    )
                    continue
                child_path = (*parts, key)
                if key in seen:
                    duplicate_code = (
                        "experiment.dut.port_duplicate"
                        if parts == ("dut", "connections")
                        else "experiment.document.duplicate_key"
                    )
                    issues.append(
                        ExperimentIssue(
                            duplicate_code,
                            _pointer(child_path),
                            f"duplicate JSON object key {key!r}",
                        )
                    )
                seen.add(key)
                output[key] = convert(child, child_path)
            return output
        if isinstance(value, list):
            return [convert(child, (*parts, index)) for index, child in enumerate(value)]
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(
                ExperimentIssue(
                    "experiment.document.invalid",
                    _pointer(parts),
                    "JSON numbers must be finite",
                )
            )
        return value

    return convert(pairs, ()), issues


def _emitted_name(name: str, kind: str) -> str:
    prefix = _ELEMENT_PREFIX[kind]
    return name if name[:1].upper() == prefix else f"{prefix}_{name}"


def _scalar(value: object) -> Scalar | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        token = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        token = repr(value)
    elif isinstance(value, str):
        token = value
    else:
        return None
    match = _SPICE_NUMBER_RE.fullmatch(token)
    if match is None:
        return None
    try:
        parsed = Decimal(match.group("number")) * _SPICE_SCALE[
            (match.group("suffix") or "").casefold()
        ]
    except (InvalidOperation, KeyError):
        return None
    if not parsed.is_finite():
        return None
    return Scalar(token=token, value=parsed)


def _ground_alias(value: str) -> bool:
    lowered = value.casefold()
    return lowered in {"gnd", "ground", "gnd!", "0!"} or value.endswith("!")


def _net_valid(value: object) -> bool:
    return value == "0" or (
        isinstance(value, str) and _NAME_RE.fullmatch(value) is not None
    )


def _analysis_axis_unit(analysis: Analysis) -> str:
    return analysis.axis_unit


def _render_element(element: Element) -> str:
    params = element.parameters
    head = [element.emitted_name, element.plus, element.minus]
    ac: list[str] = []
    if "ac_mag" in params:
        ac = [
            "AC",
            str(params["ac_mag"].token),
            str(params["ac_phase"].token),
        ]
    if element.kind in {"vdc", "idc"}:
        return " ".join([*head, "DC", str(params["dc"].token), *ac])
    if element.kind in {"vpulse", "ipulse"}:
        values = " ".join(str(params[key].token) for key in _PULSE_PARAMETERS)
        return " ".join(
            [
                *head,
                "DC",
                str(params["initial_value"].token),
                *ac,
                f"PULSE({values})",
            ]
        )
    if element.kind in {"vsin", "isin"}:
        values = " ".join(str(params[key].token) for key in _SINE_PARAMETERS)
        return " ".join(
            [*head, "DC", str(params["dc"].token), *ac, f"SIN({values})"]
        )
    if element.kind in {"vpwl", "ipwl"}:
        points = params["points"]
        values = " ".join(
            f"{time.token} {sample.token}" for time, sample in points
        )
        return " ".join(
            [*head, "DC", str(params["dc"].token), *ac, f"PWL({values})"]
        )
    if element.kind == "resistor":
        return " ".join([*head, str(params["r"].token)])
    if element.kind == "capacitor":
        tokens = [*head, str(params["c"].token)]
        if "ic" in params:
            tokens.append(f"IC={params['ic'].token}")
        return " ".join(tokens)
    return " ".join([*head, str(params["l"].token)])


def _analysis_card(
    document: Mapping[str, Any],
    elements: Mapping[str, Element],
) -> tuple[str, str] | None:
    kind = document.get("kind")
    if kind == "op":
        return ".OP", "1"
    if kind == "ac":
        return (
            ".AC "
            + " ".join(
                [
                    str(document["sweep"]).upper(),
                    str(document["points"]),
                    str(document["start"]),
                    str(document["stop"]),
                ]
            ),
            "Hz",
        )
    if kind == "dc":
        source = elements[str(document["source"])]
        unit = "V" if source.kind in _VOLTAGE_SOURCE_KINDS else "A"
        return (
            ".DC "
            + " ".join(
                [
                    source.emitted_name,
                    str(document["start"]),
                    str(document["stop"]),
                    str(document["step"]),
                ]
            ),
            unit,
        )
    if kind == "tran":
        values = [str(document["step"]), str(document["stop"])]
        if "start" in document or "max_step" in document:
            values.append(str(document.get("start", "0")))
        if "max_step" in document:
            values.append(str(document["max_step"]))
        return ".TRAN " + " ".join(values), "s"
    return None


def _extract_dut_text(netlist_text: str) -> tuple[str | None, list[str], str | None]:
    """Return exact structural subcircuit bytes and declared names."""

    lines = netlist_text.splitlines(keepends=True)
    start: int | None = None
    depth = 0
    names: list[str] = []
    end: int | None = None
    allowed_instance_prefixes = set("MmRrCcLlXxEeGg")
    for index, line in enumerate(lines):
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped or stripped.startswith("*"):
            continue
        if lowered.startswith(".subckt "):
            if depth:
                return None, [], "nested .SUBCKT definitions are unsupported"
            tokens = stripped.split()
            if len(tokens) < 2 or _NAME_RE.fullmatch(tokens[1]) is None:
                return None, [], "the DUT netlist has an invalid .SUBCKT name"
            if start is None:
                start = index
            depth = 1
            names.append(tokens[1])
            continue
        if lowered.startswith(".ends"):
            if depth != 1:
                return None, [], "the DUT netlist has an unmatched .ENDS"
            depth = 0
            end = index + 1
            continue
        if depth:
            if stripped.startswith("."):
                return (
                    None,
                    [],
                    f"the DUT subcircuit contains forbidden directive {stripped.split()[0]!r}",
                )
            if stripped[:1] not in allowed_instance_prefixes:
                return (
                    None,
                    [],
                    f"the DUT subcircuit contains unsupported card {stripped.split()[0]!r}",
                )
            continue
        # The descriptor netlist may have one title/comment line only outside
        # the structural definitions.
        if index == 0:
            continue
        return None, [], f"the DUT netlist has top-level content {stripped!r}"
    if depth:
        return None, [], "the DUT netlist has an unterminated .SUBCKT"
    if start is None or end is None:
        return None, [], "the DUT netlist contains no structural .SUBCKT"
    for line in lines[end:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("*"):
            return None, [], f"the DUT netlist has trailing content {stripped!r}"
    text = "".join(lines[start:end])
    if not text.endswith("\n"):
        text += "\n"
    return text, names, None


def _scan_scoped_instance_collisions(deck_text: str) -> tuple[str, str] | None:
    scope = "<top>"
    names: dict[str, str] = {}
    for line in deck_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.casefold()
        if lowered.startswith(".subckt "):
            scope = stripped.split()[1]
            names = {}
            continue
        if lowered.startswith(".ends"):
            scope = "<top>"
            names = {}
            continue
        if stripped.startswith("."):
            continue
        if scope == "<top>" and lowered in {"run"}:
            continue
        token = stripped.split()[0]
        if token.casefold() in {"pre_osdi", "write"}:
            continue
        folded = token.casefold()
        prior = names.get(folded)
        if prior is not None:
            return scope, f"{prior}/{token}"
        names[folded] = token
    return None


def _portable_allowlist_issue(
    deck_text: str,
    *,
    expected_text: str,
    expected_top_lines: Sequence[str],
) -> str | None:
    if deck_text != expected_text:
        return "the portable deck differs from the closed serializer output"
    depth = 0
    top_lines: list[str] = []
    for line in deck_text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.casefold()
        if lowered.startswith(".subckt "):
            if depth:
                return "the portable deck contains nested .SUBCKT definitions"
            depth = 1
            continue
        if lowered.startswith(".ends"):
            if depth != 1:
                return "the portable deck has an unmatched .ENDS"
            depth = 0
            continue
        if depth:
            if stripped.startswith("."):
                return (
                    "the portable DUT subcircuit contains a directive other "
                    "than .SUBCKT/.ENDS"
                )
            continue
        top_lines.append(stripped)
    if depth:
        return "the portable deck has an unterminated .SUBCKT"
    if top_lines != list(expected_top_lines):
        return (
            "the portable top-level card sequence differs from the closed "
            "serializer allowlist"
        )
    return None


def _bound_allowlist_issue(
    deck_text: str,
    *,
    resolved: ResolvedPdkBinding,
    expected_top_lines: Sequence[str],
    expected_write_line: str,
) -> str | None:
    allowed_options = [
        f".option temp={resolved.binding.simulation_temperature_c}".casefold()
    ]
    if Decimal(resolved.binding.geometry_scale) != 1:
        allowed_options.append(
            f".option scale={resolved.binding.geometry_scale}".casefold()
        )
    allowed_options.extend(
        f".option {value}".casefold() for value in resolved.binding.ngspice_options
    )
    allowed_libraries = [card.casefold() for card in resolved.library_cards]
    allowed_osdi = [
        f"pre_osdi {path}".casefold() for path in resolved.osdi_paths
    ]
    seen_options: list[str] = []
    seen_libraries: list[str] = []
    seen_osdi: list[str] = []
    structural_lines: list[str] = []
    run_cards = 0
    write_cards = 0
    control_blocks = 0
    depth = 0
    in_control = False
    for line in deck_text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.casefold()
        if lowered == ".control":
            if depth or in_control:
                return "the bound deck has a nested or subcircuit .CONTROL"
            in_control = True
            control_blocks += 1
            continue
        if lowered == ".endc":
            if not in_control:
                return "the bound deck has an unmatched .ENDC"
            in_control = False
            continue
        if in_control:
            if lowered.startswith("pre_osdi "):
                if lowered not in allowed_osdi:
                    return f"bound preload {stripped!r} is not profile-owned"
                seen_osdi.append(lowered)
                continue
            if lowered == "run":
                run_cards += 1
                continue
            if lowered.startswith("write "):
                if stripped != expected_write_line:
                    return (
                        f"bound write card {stripped!r} differs from the exact "
                        "experiment observation set"
                    )
                write_cards += 1
                continue
            return f"bound control command {stripped!r} is outside the allowlist"
        if lowered.startswith(".subckt "):
            if depth:
                return "the bound deck contains nested .SUBCKT definitions"
            depth += 1
            continue
        if lowered.startswith(".ends"):
            depth -= 1
            if depth < 0:
                return "the bound deck has an unmatched .ENDS"
            continue
        if depth:
            if stripped.startswith("."):
                return f"bound DUT subcircuit directive {stripped.split()[0]!r} is forbidden"
            continue
        if lowered.startswith(".option "):
            if lowered not in allowed_options:
                return f"bound option {stripped!r} is not binder-owned"
            seen_options.append(lowered)
            continue
        if lowered.startswith((".lib ", ".include ")):
            if lowered not in allowed_libraries:
                return f"bound collateral card {stripped!r} is not profile-owned"
            seen_libraries.append(lowered)
            continue
        structural_lines.append(stripped)
    if depth:
        return "the bound deck has an unterminated .SUBCKT"
    if in_control:
        return "the bound deck has an unterminated .CONTROL"
    if structural_lines != list(expected_top_lines):
        return (
            "the bound top-level card sequence differs from the closed "
            "experiment serializer"
        )
    if sorted(seen_options) != sorted(allowed_options):
        return "the bound deck does not contain the exact binder-owned option set"
    if sorted(seen_libraries) != sorted(allowed_libraries):
        return "the bound deck does not contain the exact profile-owned collateral set"
    if sorted(seen_osdi) != sorted(allowed_osdi):
        return "the bound deck does not contain the exact profile-owned preload set"
    expected_control_blocks = 2 if allowed_osdi else 1
    if control_blocks != expected_control_blocks:
        return (
            f"the bound deck contains {control_blocks} control blocks, expected "
            f"{expected_control_blocks}"
        )
    if run_cards != 1 or write_cards != 1:
        return (
            "the bound deck must contain exactly one binder-owned run and one "
            "exact write command"
        )
    return None


_PDK_NAMESPACE_RE = re.compile(
    rb"^[ \t]*\.(?P<kind>subckt|model|global)[ \t]+(?P<body>[^\r\n*]+)",
    re.IGNORECASE,
)


def _pdk_namespaces(
    resolved: ResolvedPdkBinding,
) -> tuple[set[str], set[str], str | None]:
    """Return selected-PDK model/subcircuit and global-node namespaces.

    The binding profiles already enumerate every model name emitted by the
    rewriter.  Direct collateral declarations are added here so the composer
    can also reject collisions with support subcircuits and global switches.
    Each library is re-content-bound while it is scanned; a changed PDK file
    invalidates the validation snapshot rather than silently changing the
    namespace after the preflight bind.
    """

    model_names = {
        name.casefold() for name in resolved.binding.device_models.values()
    }
    global_nodes: set[str] = set()
    expected = {
        str(Path(str(record.get("path"))).resolve()): str(record.get("sha256"))
        for record in resolved.input_records
        if record.get("role") == "pdk.corner-library"
    }
    for path in resolved.library_paths:
        digest = hashlib.sha256()
        try:
            with stable_regular_file(path) as (handle, _opened):
                for line in handle:
                    digest.update(line)
                    match = _PDK_NAMESPACE_RE.match(line)
                    if match is None:
                        continue
                    kind = match.group("kind").decode("ascii").casefold()
                    tokens = match.group("body").decode(
                        "latin-1", errors="replace"
                    ).split()
                    if not tokens:
                        continue
                    if kind in {"subckt", "model"}:
                        model_names.add(tokens[0].casefold())
                    else:
                        global_nodes.update(
                            token.casefold()
                            for token in tokens
                            if token != "0" and not token.startswith("$")
                        )
        except (OSError, ValueError) as exc:
            return set(), set(), f"PDK namespace scan failed for {path}: {exc}"
        expected_digest = expected.get(str(path.resolve()))
        if expected_digest is None or digest.hexdigest() != expected_digest:
            return (
                set(),
                set(),
                f"PDK library {path} changed after it was content-bound",
            )
    return model_names, global_nodes, None


class _Validator:
    def __init__(
        self,
        *,
        document: object,
        spec_path: Path,
        spec_bytes: bytes,
        cli_pdk: str,
        pdk_root: str | Path | None,
    ) -> None:
        self.document = document
        self.spec_path = spec_path
        self.spec_bytes = spec_bytes
        self.cli_pdk = cli_pdk
        self.pdk_root = pdk_root
        self.issues: list[ExperimentIssue] = []

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
        code: str = "experiment.document.invalid",
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.add(code, path, "must be a JSON object")
            return None
        keys = set(value)
        for name in sorted(required - keys):
            self.add(code, f"{path}/{name}", f"missing required field {name!r}")
        for name in sorted(keys - required - optional):
            self.add(
                "experiment.document.unknown_field",
                f"{path}/{name}",
                f"field {name!r} is not declared by simra.experiment/v1",
            )
        return value

    def slug(self, value: object, path: str, *, code: str) -> str | None:
        if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
            self.add(code, path, "must match ^[a-z][a-z0-9_]{0,63}$")
            return None
        return value

    def name(self, value: object, path: str, *, code: str) -> str | None:
        if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
            self.add(code, path, "must match ^[A-Za-z][A-Za-z0-9_]{0,63}$")
            return None
        return value

    def net(self, value: object, path: str) -> str | None:
        if isinstance(value, str) and _ground_alias(value):
            self.add(
                "experiment.net.ground_invalid",
                path,
                f"{value!r} is not ground; literal '0' is the only permitted ground spelling",
            )
            return None
        if not _net_valid(value):
            self.add(
                "experiment.net.name_invalid",
                path,
                "must be literal '0' or match ^[A-Za-z][A-Za-z0-9_]{0,63}$",
            )
            return None
        return str(value)

    def scalar(self, value: object, path: str) -> Scalar | None:
        parsed = _scalar(value)
        if parsed is not None:
            return parsed
        if isinstance(value, str):
            self.add(
                "experiment.element.parameter_expression_forbidden",
                path,
                (
                    f"{value!r} is not one strict finite SPICE numeric scalar; "
                    "directives, braces, quotes, expressions, identifiers, and paths are forbidden"
                ),
            )
        else:
            self.add(
                "experiment.element.parameter_invalid",
                path,
                "must be one strict finite SPICE numeric scalar",
            )
        return None

    def validate(self) -> PreparedExperiment | None:
        root = self.closed(
            self.document,
            "",
            required={
                "schema",
                "id",
                "dut",
                "elements",
                "analyses",
                "observations",
                "measurements",
                "derivations",
                "conditions",
            },
            optional={"debug_save"},
        )
        if root is None:
            return None
        if root.get("schema") != EXPERIMENT_SCHEMA:
            self.add(
                "experiment.schema.unsupported",
                "/schema",
                f"expected {EXPERIMENT_SCHEMA!r}, got {root.get('schema')!r}",
            )
        identifier = self.slug(root.get("id"), "/id", code="experiment.id.invalid")

        bundle, connections = self._dut(root.get("dut"))
        elements = self._elements(root.get("elements"))
        analyses = self._analyses(root.get("analyses"), elements)
        resolved = self._conditions(root.get("conditions"))
        net_paths, known_nets = self._nets(elements, connections)
        debug_save = self._debug_save(root.get("debug_save", []), known_nets)
        observations = self._observations(
            root.get("observations"), analyses, elements, known_nets
        )
        measurements = self._measurements(
            root.get("measurements"), analyses, observations
        )
        derivations = self._derivations(
            root.get("derivations"), analyses, measurements
        )

        if bundle is not None and connections is not None:
            self._ports(bundle, connections)
        self._net_connectivity(net_paths, elements, connections)

        if (
            identifier is None
            or bundle is None
            or connections is None
            or resolved is None
            or self.issues
        ):
            return None

        pdk_names, pdk_globals, namespace_error = _pdk_namespaces(resolved)
        if namespace_error is not None:
            self.add(
                "experiment.pdk.binding_failed",
                "/conditions/pdk",
                namespace_error,
                cause_code="pdk.file.unstable",
            )
            return None
        for net in sorted(known_nets):
            if net != "0" and net.casefold() in pdk_globals:
                self.add(
                    "experiment.compose.global_node_collision",
                    net_paths[net][0],
                    f"external net {net!r} collides with a selected-PDK global node",
                )

        dut_text, dut_subckts, dut_error = _extract_dut_text(bundle.netlist_text)
        if dut_error is not None or dut_text is None:
            self.add(
                "experiment.compose.deck_mismatch",
                "/dut/artifact",
                dut_error or "the DUT netlist could not be isolated",
            )
            return None
        top = bundle.top
        if top.casefold() not in {name.casefold() for name in dut_subckts}:
            self.add(
                "experiment.compose.deck_mismatch",
                "/dut/top",
                f"the DUT netlist has no .SUBCKT matching {top!r}",
            )
            return None

        for subckt in dut_subckts:
            if subckt.casefold() in pdk_names:
                self.add(
                    "experiment.compose.subckt_collision",
                    "/dut/top",
                    f"DUT subcircuit {subckt!r} collides with a PDK model/subcircuit name",
                )
        dut_names = {name.casefold() for name in dut_subckts}
        generated_names = [
            ("/dut/top", "X_OPENADA_DUT"),
            *[
                (f"/elements/{index}/name", element.emitted_name)
                for index, element in enumerate(elements)
            ],
        ]
        for path, name in generated_names:
            if name.casefold() in pdk_names:
                self.add(
                    "experiment.element.emitted_name_collision",
                    path,
                    f"emitted instance name {name!r} collides with a selected-PDK model/subcircuit name",
                )
            if path.startswith("/elements/") and name.casefold() in dut_names:
                self.add(
                    "experiment.compose.subckt_collision",
                    path,
                    f"emitted instance name {name!r} collides with a DUT subcircuit name",
                )
        if self.issues:
            return None

        connection_nodes = [connections[name] for name in bundle.port_order]
        x_line = " ".join(["X_OPENADA_DUT", *connection_nodes, top])
        fixture_lines = [_render_element(item) for item in elements]
        top_card_lines = [x_line, *fixture_lines]
        base_lines = [
            f"* OPENADA EXPERIMENT {identifier}\n",
            dut_text,
            x_line + "\n",
            *(line + "\n" for line in fixture_lines),
            ".END\n",
        ]
        base_deck = "".join(base_lines)
        base_sha = _sha256_bytes(base_deck.encode("utf-8"))

        collision = _scan_scoped_instance_collisions(base_deck)
        if collision is not None:
            scope, names = collision
            self.add(
                "experiment.element.emitted_name_collision",
                "/elements",
                f"emitted instance names {names} collide in scope {scope!r}",
            )
            return None

        prepared_runs: list[PreparedRun] = []
        observations_by_analysis: dict[str, list[Observation]] = defaultdict(list)
        for observation in observations:
            observations_by_analysis[observation.analysis_id].append(observation)
        for analysis in analyses:
            selected = tuple(observations_by_analysis.get(analysis.identifier, ()))
            saved_nets = tuple(
                dict.fromkeys(
                    [
                        *(item.net for item in selected if item.net is not None),
                        *debug_save,
                    ]
                )
            )
            retained_element_ids = tuple(
                dict.fromkeys(
                    item.element
                    for item in selected
                    if item.element is not None
                )
            )
            retained_sources = tuple(
                next(
                    element.emitted_name
                    for element in elements
                    if element.name == source
                )
                for source in retained_element_ids
            )
            save_vectors = [
                *(f"v({name})" for name in saved_nets),
                *(f"i({source.lower()})" for source in retained_sources),
            ]
            insert = ""
            save_card: str | None = None
            if save_vectors:
                save_card = ".SAVE " + " ".join(save_vectors)
                insert += save_card + "\n"
            insert += analysis.card + "\n"
            portable = base_deck.removesuffix(".END\n") + insert + ".END\n"
            expected = base_deck.removesuffix(".END\n") + insert + ".END\n"
            expected_top_lines = [
                *top_card_lines,
                *([save_card] if save_card is not None else []),
                analysis.card,
                ".END",
            ]
            complaint = _portable_allowlist_issue(
                portable,
                expected_text=expected,
                expected_top_lines=expected_top_lines,
            )
            if complaint:
                self.add(
                    "experiment.compose.deck_mismatch",
                    f"/analyses/{analysis.identifier}",
                    complaint,
                )
                continue
            try:
                bound, _facts = bind_deck(
                    portable,
                    resolved,
                    raw_name="run.raw",
                    saved_nets=saved_nets,
                    retained_current_sources=retained_sources,
                )
            except PdkBindingError as exc:
                self.add(
                    "experiment.pdk.binding_failed",
                    f"/analyses/{analysis.identifier}",
                    exc.message,
                    cause_code=exc.code,
                )
                continue
            complaint = _bound_allowlist_issue(
                bound,
                resolved=resolved,
                expected_top_lines=expected_top_lines,
                expected_write_line=(
                    "write run.raw"
                    + (f" {' '.join(save_vectors)}" if save_vectors else "")
                ),
            )
            if complaint:
                self.add(
                    "experiment.compose.deck_mismatch",
                    f"/analyses/{analysis.identifier}",
                    complaint,
                )
                continue
            collision = _scan_scoped_instance_collisions(bound)
            if collision is not None:
                scope, names = collision
                self.add(
                    "experiment.element.emitted_name_collision",
                    f"/analyses/{analysis.identifier}",
                    f"PDK rewriting makes {names} collide in scope {scope!r}",
                )
                continue
            prepared_runs.append(
                PreparedRun(
                    analysis=analysis,
                    observations=selected,
                    saved_nets=saved_nets,
                    retained_current_sources=retained_sources,
                    portable_deck=portable,
                    portable_sha256=_sha256_bytes(portable.encode("utf-8")),
                    bound_deck_sha256=_sha256_bytes(bound.encode("utf-8")),
                )
            )

        if self.issues:
            return None
        return PreparedExperiment(
            spec_path=self.spec_path,
            spec_bytes=self.spec_bytes,
            spec_document=root,
            spec_raw_sha256=_sha256_bytes(self.spec_bytes),
            spec_canonical_sha256=_canonical_sha256(root),
            identifier=identifier,
            bundle=bundle,
            resolved_pdk=resolved,
            pdk_root=Path(self.pdk_root).expanduser(),
            elements=tuple(elements),
            analyses=tuple(analyses),
            observations=tuple(observations),
            measurements=tuple(measurements),
            derivations=tuple(derivations),
            debug_save=tuple(debug_save),
            base_deck=base_deck,
            base_deck_sha256=base_sha,
            runs=tuple(prepared_runs),
        )

    def _dut(self, value: object) -> tuple[Any | None, dict[str, str] | None]:
        dut = self.closed(
            value,
            "/dut",
            required={"artifact", "bundle", "top", "connections"},
            code="experiment.dut.bundle_invalid",
        )
        if dut is None:
            return None, None
        artifact_value = dut.get("artifact")
        artifact: Path | None = None
        if not isinstance(artifact_value, str) or not artifact_value:
            self.add(
                "experiment.dut.artifact_missing",
                "/dut/artifact",
                "must name one absolute schematic.artifact.json path",
            )
        else:
            artifact = Path(artifact_value).expanduser()
            if not artifact.is_absolute():
                self.add(
                    "experiment.dut.artifact_missing",
                    "/dut/artifact",
                    "must be an absolute path",
                )
                artifact = None
        top = self.name(
            dut.get("top"),
            "/dut/top",
            code="experiment.dut.port_abi_mismatch",
        )
        bundle_object = self.closed(
            dut.get("bundle"),
            "/dut/bundle",
            required={
                "descriptor_sha256",
                "source_sha256",
                "view_sha256",
                "netlist_sha256",
                "cdl_sha256",
            },
            code="experiment.dut.bundle_invalid",
        )
        expected: dict[str, str] = {}
        if bundle_object is not None:
            for name in (
                "descriptor_sha256",
                "source_sha256",
                "view_sha256",
                "netlist_sha256",
                "cdl_sha256",
            ):
                digest_value = bundle_object.get(name)
                if not isinstance(digest_value, str) or _SHA256_RE.fullmatch(digest_value) is None:
                    self.add(
                        "experiment.dut.digest_mismatch",
                        f"/dut/bundle/{name}",
                        "must be one lowercase SHA-256 digest",
                    )
                else:
                    expected[name] = digest_value

        connections_object = dut.get("connections")
        connections: dict[str, str] | None = None
        if not isinstance(connections_object, Mapping):
            self.add(
                "experiment.dut.port_abi_mismatch",
                "/dut/connections",
                "must be an object mapping every DUT port to one external net",
            )
        else:
            connections = {}
            for port, net_value in connections_object.items():
                if not isinstance(port, str) or _NAME_RE.fullmatch(port) is None:
                    self.add(
                        "experiment.dut.port_unknown",
                        f"/dut/connections/{port}",
                        "DUT port names must be bounded SPICE identifiers",
                    )
                    continue
                net = self.net(net_value, f"/dut/connections/{port}")
                if net is not None:
                    connections[port] = net

        bundle = None
        if (
            artifact is not None
            and top is not None
            and len(expected) == 5
        ):
            try:
                from ..engines.simra_artifact import load_simra_schematic_bundle

                bundle = load_simra_schematic_bundle(
                    artifact,
                    expected_digests=expected,
                    expected_top=top,
                )
            except SimraArtifactError as exc:
                path = (
                    "/dut/bundle"
                    if "digest" in exc.code
                    else "/dut/artifact"
                )
                self.add(exc.code, path, exc.message, cause_code=exc.code)
        return bundle, connections

    def _ports(self, bundle: Any, connections: Mapping[str, str]) -> None:
        ports = tuple(bundle.port_order)
        for port in ports:
            if port == "0":
                self.add(
                    "experiment.dut.port_abi_mismatch",
                    "/dut/artifact",
                    "DUT formal ports may not be named literal '0'",
                )
            if port not in connections:
                self.add(
                    "experiment.dut.port_unbound",
                    f"/dut/connections/{port}",
                    f"DUT port {port!r} is not bound",
                )
        for port in sorted(set(connections) - set(ports)):
            self.add(
                "experiment.dut.port_unknown",
                f"/dut/connections/{port}",
                f"{port!r} is not in the DUT port_order",
            )

    def _elements(self, value: object) -> list[Element]:
        if not isinstance(value, list):
            self.add(
                "experiment.document.invalid",
                "/elements",
                "must be an array",
            )
            return []
        if len(value) > MAX_ELEMENTS:
            self.add(
                "experiment.document.over_limit",
                "/elements",
                f"contains {len(value)} entries; the limit is {MAX_ELEMENTS}",
            )
        output: list[Element] = []
        names: dict[str, tuple[str, int]] = {}
        emitted: dict[str, tuple[str, int]] = {}
        for index, raw in enumerate(value[:MAX_ELEMENTS]):
            path = f"/elements/{index}"
            item = self.closed(
                raw,
                path,
                required={"name", "kind", "plus", "minus", "parameters"},
                code="experiment.document.invalid",
            )
            if item is None:
                continue
            name = self.name(
                item.get("name"),
                f"{path}/name",
                code="experiment.element.name_invalid",
            )
            kind = item.get("kind")
            if kind not in _ELEMENT_REQUIRED:
                self.add(
                    "experiment.element.kind_unsupported",
                    f"{path}/kind",
                    f"kind must be one of {', '.join(_ELEMENT_REQUIRED)}",
                )
                kind = None
            plus = self.net(item.get("plus"), f"{path}/plus")
            minus = self.net(item.get("minus"), f"{path}/minus")
            if plus is not None and minus is not None and plus.casefold() == minus.casefold():
                self.add(
                    "experiment.element.terminal_short",
                    path,
                    "plus and minus must name distinct case-insensitive nets",
                )
            parameters = item.get("parameters")
            normalized: dict[str, Scalar | tuple[tuple[Scalar, Scalar], ...]] = {}
            if not isinstance(parameters, Mapping):
                self.add(
                    "experiment.element.parameter_invalid",
                    f"{path}/parameters",
                    "must be an object",
                )
            elif kind is not None:
                keys = set(parameters)
                for key in sorted(_ELEMENT_REQUIRED[kind] - keys):
                    self.add(
                        "experiment.element.parameter_missing",
                        f"{path}/parameters/{key}",
                        f"{kind} requires parameter {key!r}",
                    )
                for key in sorted(keys - _ELEMENT_ALLOWED[kind]):
                    self.add(
                        "experiment.element.parameter_unexpected",
                        f"{path}/parameters/{key}",
                        f"{kind} does not accept parameter {key!r}",
                    )
                for key in sorted(keys & _ELEMENT_ALLOWED[kind]):
                    raw_parameter = parameters[key]
                    parameter_path = f"{path}/parameters/{key}"
                    if key == "points":
                        points = self._pwl_points(raw_parameter, parameter_path)
                        if points is not None:
                            normalized[key] = points
                    else:
                        parsed = self.scalar(raw_parameter, parameter_path)
                        if parsed is not None:
                            normalized[key] = parsed
                has_mag = "ac_mag" in parameters
                has_phase = "ac_phase" in parameters
                if has_mag != has_phase:
                    self.add(
                        "experiment.analysis.ac_stimulus_incomplete",
                        f"{path}/parameters",
                        "ac_mag and ac_phase must be supplied together or both absent",
                    )
                self._element_ranges(kind, normalized, path)

            if name is not None:
                folded = name.casefold()
                if folded in names:
                    prior, prior_index = names[folded]
                    self.add(
                        "experiment.element.name_duplicate",
                        f"{path}/name",
                        f"{name!r} duplicates element {prior!r} at /elements/{prior_index}/name",
                    )
                else:
                    names[folded] = (name, index)
            emitted_name = _emitted_name(name, kind) if name and kind else None
            if emitted_name is not None:
                folded = emitted_name.casefold()
                if folded in emitted:
                    prior, prior_index = emitted[folded]
                    self.add(
                        "experiment.element.emitted_name_collision",
                        f"{path}/name",
                        (
                            f"{name!r} emits {emitted_name!r}, colliding with "
                            f"{prior!r} at /elements/{prior_index}/name"
                        ),
                    )
                else:
                    emitted[folded] = (name, index)
            if (
                name is not None
                and kind is not None
                and plus is not None
                and minus is not None
                and isinstance(parameters, Mapping)
                and _ELEMENT_REQUIRED[kind].issubset(normalized)
            ):
                output.append(
                    Element(
                        name=name,
                        emitted_name=str(emitted_name),
                        kind=kind,
                        plus=plus,
                        minus=minus,
                        parameters=normalized,
                    )
                )
        return output

    def _pwl_points(
        self, value: object, path: str
    ) -> tuple[tuple[Scalar, Scalar], ...] | None:
        if (
            not isinstance(value, list)
            or len(value) < 2
            or len(value) > 100_000
        ):
            self.add(
                "experiment.element.pwl_invalid",
                path,
                "must contain between 2 and 100000 [time, value] pairs",
            )
            return None
        output: list[tuple[Scalar, Scalar]] = []
        for index, pair in enumerate(value):
            if not isinstance(pair, list) or len(pair) != 2:
                self.add(
                    "experiment.element.pwl_invalid",
                    f"{path}/{index}",
                    "must be one [time, value] pair",
                )
                continue
            time = self.scalar(pair[0], f"{path}/{index}/0")
            sample = self.scalar(pair[1], f"{path}/{index}/1")
            if time is not None and sample is not None:
                output.append((time, sample))
        if len(output) != len(value):
            return None
        times = [item[0].value for item in output]
        if any(item < 0 for item in times):
            self.add(
                "experiment.element.pwl_invalid",
                path,
                "PWL times must be non-negative",
            )
        if any(right <= left for left, right in zip(times, times[1:])):
            self.add(
                "experiment.element.pwl_invalid",
                path,
                "PWL times must be strictly increasing",
            )
        return tuple(output)

    def _element_ranges(
        self,
        kind: str,
        parameters: Mapping[str, Scalar | tuple[tuple[Scalar, Scalar], ...]],
        path: str,
    ) -> None:
        def number(name: str) -> Decimal | None:
            value = parameters.get(name)
            return value.value if isinstance(value, Scalar) else None

        complaints: list[tuple[str, str]] = []
        if kind in {"vpulse", "ipulse"}:
            for key in ("delay_time", "rise_time", "fall_time"):
                value = number(key)
                if value is not None and value < 0:
                    complaints.append((key, "must be non-negative"))
            width = number("pulse_width")
            period = number("period")
            if width is not None and width <= 0:
                complaints.append(("pulse_width", "must be greater than zero"))
            if period is not None and period <= 0:
                complaints.append(("period", "must be greater than zero"))
            if width is not None and period is not None and width > period:
                complaints.append(("pulse_width", "must not exceed period"))
        if kind in {"vsin", "isin"}:
            frequency = number("freq")
            delay = number("delay")
            damping = number("damping")
            if frequency is not None and frequency <= 0:
                complaints.append(("freq", "must be greater than zero"))
            if delay is not None and delay < 0:
                complaints.append(("delay", "must be non-negative"))
            if damping is not None and damping < 0:
                complaints.append(("damping", "must be non-negative"))
        for key, message in complaints:
            self.add(
                "experiment.element.parameter_out_of_range",
                f"{path}/parameters/{key}",
                message,
            )

    def _analyses(
        self, value: object, elements: Sequence[Element]
    ) -> list[Analysis]:
        if not isinstance(value, list):
            self.add("experiment.analysis.missing", "/analyses", "must be a non-empty array")
            return []
        if not value:
            self.add("experiment.analysis.missing", "/analyses", "must declare at least one analysis")
        if len(value) > MAX_ANALYSES:
            self.add(
                "experiment.analysis.over_limit",
                "/analyses",
                f"contains {len(value)} entries; the limit is {MAX_ANALYSES}",
            )
        element_by_name = {item.name: item for item in elements}
        output: list[Analysis] = []
        ids: dict[str, int] = {}
        for index, raw in enumerate(value[:MAX_ANALYSES]):
            path = f"/analyses/{index}"
            if not isinstance(raw, Mapping):
                self.add("experiment.analysis.invalid", path, "must be an object")
                continue
            kind = raw.get("kind")
            required = {
                "op": {"id", "kind"},
                "ac": {"id", "kind", "sweep", "points", "start", "stop", "ac_excitation"},
                "dc": {"id", "kind", "source", "start", "stop", "step"},
                "tran": {"id", "kind", "step", "stop"},
            }.get(kind)
            optional = {"start", "max_step", "sampling_plan"} if kind == "tran" else set()
            if required is None:
                self.closed(
                    raw,
                    path,
                    required={"id", "kind"},
                    code="experiment.analysis.invalid",
                )
                self.add(
                    "experiment.analysis.unsupported",
                    f"{path}/kind",
                    "kind must be op, dc, ac, or tran",
                )
                continue
            self.closed(
                raw,
                path,
                required=required,
                optional=optional,
                code="experiment.analysis.invalid",
            )
            identifier = self.slug(
                raw.get("id"),
                f"{path}/id",
                code="experiment.analysis.invalid",
            )
            if identifier is not None:
                if identifier in ids:
                    self.add(
                        "experiment.analysis.id_duplicate",
                        f"{path}/id",
                        f"duplicates /analyses/{ids[identifier]}/id",
                    )
                else:
                    ids[identifier] = index

            normalized = dict(raw)
            valid = True
            if kind == "op":
                pass
            elif kind == "ac":
                if raw.get("sweep") not in {"lin", "dec", "oct"}:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/sweep",
                        "must be lin, dec, or oct",
                    )
                    valid = False
                points = raw.get("points")
                if (
                    isinstance(points, bool)
                    or not isinstance(points, int)
                    or points < 1
                ):
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/points",
                        "must be a positive integer",
                    )
                    valid = False
                elif points > MAX_SHARED_ANALYSIS_POINTS:
                    self.add(
                        "experiment.analysis.over_limit",
                        f"{path}/points",
                        f"must not exceed {MAX_SHARED_ANALYSIS_POINTS}",
                    )
                    valid = False
                for key in ("start", "stop"):
                    parsed = self.scalar(raw.get(key), f"{path}/{key}")
                    if parsed is None:
                        valid = False
                    else:
                        normalized[key] = parsed.token
                        normalized[f"_{key}_value"] = parsed.value
                start = normalized.get("_start_value")
                stop = normalized.get("_stop_value")
                if isinstance(start, Decimal) and start <= 0:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/start",
                        "AC start must be greater than zero",
                    )
                    valid = False
                if isinstance(start, Decimal) and isinstance(stop, Decimal) and stop <= start:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/stop",
                        "AC stop must be greater than start",
                    )
                    valid = False
                if (
                    valid
                    and isinstance(points, int)
                    and isinstance(start, Decimal)
                    and isinstance(stop, Decimal)
                ):
                    if raw.get("sweep") == "lin":
                        estimated_points = points
                    else:
                        divisor = (
                            Decimal(10).ln()
                            if raw.get("sweep") == "dec"
                            else Decimal(2).ln()
                        )
                        estimated_points = int(
                            (
                                Decimal(points)
                                * ((stop / start).ln() / divisor)
                            ).to_integral_value(rounding=ROUND_CEILING)
                        ) + 1
                    if estimated_points > MAX_SHARED_ANALYSIS_POINTS:
                        self.add(
                            "experiment.analysis.over_limit",
                            path,
                            (
                                f"the AC sweep would retain about {estimated_points} "
                                f"points; the limit is {MAX_SHARED_ANALYSIS_POINTS}"
                            ),
                        )
                        valid = False
                self._ac_excitation(raw, path, element_by_name)
            elif kind == "dc":
                source = raw.get("source")
                if source not in element_by_name or element_by_name[source].kind not in _INDEPENDENT_SOURCE_KINDS:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/source",
                        "must name one experiment-owned independent source",
                    )
                    valid = False
                for key in ("start", "stop", "step"):
                    parsed = self.scalar(raw.get(key), f"{path}/{key}")
                    if parsed is None:
                        valid = False
                    else:
                        normalized[key] = parsed.token
                        normalized[f"_{key}_value"] = parsed.value
                start = normalized.get("_start_value")
                stop = normalized.get("_stop_value")
                step = normalized.get("_step_value")
                if isinstance(start, Decimal) and isinstance(stop, Decimal) and stop <= start:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/stop",
                        "DC stop must be greater than start",
                    )
                    valid = False
                if isinstance(step, Decimal) and step <= 0:
                    self.add(
                        "experiment.analysis.invalid",
                        f"{path}/step",
                        "DC step must be greater than zero",
                    )
                    valid = False
                if (
                    valid
                    and isinstance(start, Decimal)
                    and isinstance(stop, Decimal)
                    and isinstance(step, Decimal)
                ):
                    estimated_points = int(
                        ((stop - start) / step).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ) + 1
                    if estimated_points > MAX_SHARED_ANALYSIS_POINTS:
                        self.add(
                            "experiment.analysis.over_limit",
                            path,
                            (
                                f"the DC sweep would retain about {estimated_points} "
                                f"points; the limit is {MAX_SHARED_ANALYSIS_POINTS}"
                            ),
                        )
                        valid = False
            else:
                for key in ("step", "stop", "start", "max_step"):
                    if key not in raw:
                        continue
                    parsed = self.scalar(raw.get(key), f"{path}/{key}")
                    if parsed is None:
                        valid = False
                    else:
                        normalized[key] = parsed.token
                        normalized[f"_{key}_value"] = parsed.value
                step = normalized.get("_step_value")
                stop = normalized.get("_stop_value")
                start = normalized.get("_start_value", Decimal(0))
                maximum = normalized.get("_max_step_value")
                if isinstance(step, Decimal) and step <= 0:
                    self.add("experiment.analysis.invalid", f"{path}/step", "must be greater than zero")
                    valid = False
                if isinstance(stop, Decimal) and stop <= 0:
                    self.add("experiment.analysis.invalid", f"{path}/stop", "must be greater than zero")
                    valid = False
                if isinstance(start, Decimal) and start < 0:
                    self.add("experiment.analysis.invalid", f"{path}/start", "must be non-negative")
                    valid = False
                if isinstance(start, Decimal) and isinstance(stop, Decimal) and start >= stop:
                    self.add("experiment.analysis.invalid", f"{path}/start", "must be less than stop")
                    valid = False
                if isinstance(maximum, Decimal) and maximum <= 0:
                    self.add("experiment.analysis.invalid", f"{path}/max_step", "must be greater than zero")
                    valid = False
                if (
                    valid
                    and isinstance(step, Decimal)
                    and isinstance(stop, Decimal)
                    and isinstance(start, Decimal)
                ):
                    estimated_points = int(
                        ((stop - start) / step).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ) + 1
                    if estimated_points > MAX_SHARED_ANALYSIS_POINTS:
                        self.add(
                            "experiment.analysis.over_limit",
                            path,
                            (
                                "the transient would retain about "
                                f"{estimated_points} requested output points; "
                                f"the limit is {MAX_SHARED_ANALYSIS_POINTS}"
                            ),
                        )
                        valid = False
                self._sampling_plan(raw.get("sampling_plan"), path, normalized)

            card_input = {
                key: value
                for key, value in normalized.items()
                if not str(key).startswith("_")
            }
            rendered = (
                _analysis_card(card_input, element_by_name)
                if valid and identifier is not None
                else None
            )
            if rendered is not None:
                card, axis_unit = rendered
                output.append(
                    Analysis(
                        identifier=identifier,
                        kind=str(kind),
                        document=card_input,
                        card=card,
                        axis_unit=axis_unit,
                    )
                )
        return output

    def _sampling_plan(
        self,
        value: object,
        analysis_path: str,
        normalized_analysis: Mapping[str, Any],
    ) -> None:
        if value is None:
            return
        plan = self.closed(
            value,
            f"{analysis_path}/sampling_plan",
            required={"kind", "point_count"},
            code="experiment.analysis.invalid",
        )
        if plan is None:
            return
        if plan.get("kind") != "coherent_uniform":
            self.add(
                "experiment.analysis.invalid",
                f"{analysis_path}/sampling_plan/kind",
                "v1 supports only coherent_uniform",
            )
        count = plan.get("point_count")
        if isinstance(count, bool) or not isinstance(count, int) or not 8 <= count <= 100_000:
            self.add(
                "experiment.analysis.invalid",
                f"{analysis_path}/sampling_plan/point_count",
                "must be an integer from 8 to 100000",
            )
            return
        if count & (count - 1):
            self.add(
                "experiment.analysis.invalid",
                f"{analysis_path}/sampling_plan/point_count",
                "must be a power of two",
            )
        step = normalized_analysis.get("_step_value")
        stop = normalized_analysis.get("_stop_value")
        start = normalized_analysis.get("_start_value", Decimal(0))
        if all(isinstance(item, Decimal) for item in (step, stop, start)):
            if stop - start != step * (count - 1):
                self.add(
                    "experiment.analysis.invalid",
                    f"{analysis_path}/sampling_plan",
                    "coherent_uniform requires stop-start == step*(point_count-1)",
                )

    def _ac_excitation(
        self,
        raw: Mapping[str, Any],
        path: str,
        elements: Mapping[str, Element],
    ) -> None:
        value = raw.get("ac_excitation")
        if not isinstance(value, list) or not value:
            self.add(
                "experiment.analysis.ac_stimulus_missing",
                f"{path}/ac_excitation",
                "must name a non-empty set of experiment-owned independent sources",
            )
            return
        names: list[str] = []
        for index, name in enumerate(value):
            if not isinstance(name, str) or name not in elements:
                self.add(
                    "experiment.analysis.invalid",
                    f"{path}/ac_excitation/{index}",
                    "must name an existing experiment element",
                )
                continue
            if elements[name].kind not in _INDEPENDENT_SOURCE_KINDS:
                self.add(
                    "experiment.analysis.invalid",
                    f"{path}/ac_excitation/{index}",
                    f"{name!r} is not an independent source",
                )
                continue
            if name in names:
                self.add(
                    "experiment.analysis.invalid",
                    f"{path}/ac_excitation/{index}",
                    f"duplicate excitation {name!r}",
                )
            names.append(name)
        nonzero = {
            name
            for name, element in elements.items()
            if isinstance(element.parameters.get("ac_mag"), Scalar)
            and element.parameters["ac_mag"].value != 0
        }
        listed_nonzero = {
            name
            for name in names
            if isinstance(elements[name].parameters.get("ac_mag"), Scalar)
            and elements[name].parameters["ac_mag"].value != 0
        }
        if not listed_nonzero:
            listed_with_ac = any(
                isinstance(elements[name].parameters.get("ac_mag"), Scalar)
                for name in names
            )
            self.add(
                (
                    "experiment.analysis.ac_stimulus_all_zero"
                    if listed_with_ac
                    else "experiment.analysis.ac_stimulus_missing"
                ),
                f"{path}/ac_excitation",
                "at least one declared excitation must have nonzero ac_mag",
            )
        missing = sorted(nonzero - set(names))
        if missing:
            self.add(
                "experiment.analysis.ac_stimulus_incomplete",
                f"{path}/ac_excitation",
                "every nonzero AC source must be declared; missing " + ", ".join(missing),
            )

    def _nets(
        self,
        elements: Sequence[Element],
        connections: Mapping[str, str] | None,
    ) -> tuple[dict[str, list[str]], set[str]]:
        paths: dict[str, list[str]] = defaultdict(list)
        spellings: dict[str, set[str]] = defaultdict(set)
        for index, element in enumerate(elements):
            for terminal in ("plus", "minus"):
                value = getattr(element, terminal)
                paths[value].append(f"/elements/{index}/{terminal}")
                spellings[value.casefold()].add(value)
        if connections is not None:
            for port, net in connections.items():
                paths[net].append(f"/dut/connections/{port}")
                spellings[net.casefold()].add(net)
        for group in spellings.values():
            if len(group) > 1:
                ordered = sorted(group)
                for spelling in ordered[1:]:
                    self.add(
                        "experiment.net.case_collision",
                        paths[spelling][0],
                        "case-insensitive SPICE net collision: " + "/".join(ordered),
                    )
        return paths, set(paths)

    def _net_connectivity(
        self,
        net_paths: Mapping[str, Sequence[str]],
        elements: Sequence[Element],
        connections: Mapping[str, str] | None,
    ) -> None:
        if connections is None:
            return
        if "0" not in net_paths:
            self.add(
                "experiment.net.ground_invalid",
                "/dut/connections",
                "at least one DUT binding or element terminal must reference literal '0'",
            )
        for net, paths in sorted(net_paths.items()):
            if len(paths) == 1:
                self.add(
                    "experiment.net.dangling",
                    paths[0],
                    f"net {net!r} has only one composed incidence",
                )
        graph: dict[str, set[str]] = defaultdict(set)
        for element in elements:
            graph[element.plus].add(element.minus)
            graph[element.minus].add(element.plus)
        anchors = {"0", *connections.values()}
        remaining = set(graph)
        while remaining:
            first = next(iter(remaining))
            active = [first]
            component: set[str] = set()
            while active:
                node = active.pop()
                if node in component:
                    continue
                component.add(node)
                active.extend(graph[node] - component)
            remaining -= component
            if not component & anchors:
                representative = sorted(component)[0]
                self.add(
                    "experiment.net.floating_component",
                    net_paths[representative][0],
                    "external component has neither ground nor a DUT-port binding: "
                    + ", ".join(sorted(component)),
                )

    def _debug_save(self, value: object, known_nets: set[str]) -> list[str]:
        if not isinstance(value, list):
            self.add("experiment.document.invalid", "/debug_save", "must be an array")
            return []
        if len(value) > MAX_DEBUG_SAVES:
            self.add(
                "experiment.document.over_limit",
                "/debug_save",
                f"contains {len(value)} entries; the limit is {MAX_DEBUG_SAVES}",
            )
        output: list[str] = []
        for index, raw in enumerate(value[:MAX_DEBUG_SAVES]):
            net = self.net(raw, f"/debug_save/{index}")
            if net is None:
                continue
            if net not in known_nets:
                self.add(
                    "experiment.observation.unknown_net",
                    f"/debug_save/{index}",
                    f"net {net!r} is not present in the composed experiment",
                )
            elif net in output:
                self.add(
                    "experiment.observation.duplicate",
                    f"/debug_save/{index}",
                    f"duplicate debug save {net!r}",
                )
            else:
                output.append(net)
        return output

    def _observations(
        self,
        value: object,
        analyses: Sequence[Analysis],
        elements: Sequence[Element],
        known_nets: set[str],
    ) -> list[Observation]:
        if not isinstance(value, list):
            self.add("experiment.observation.invalid", "/observations", "must be an array")
            return []
        if len(value) > MAX_OBSERVATIONS:
            self.add(
                "experiment.document.over_limit",
                "/observations",
                f"contains {len(value)} entries; the limit is {MAX_OBSERVATIONS}",
            )
        analysis_by_id = {item.identifier: item for item in analyses}
        element_by_name = {item.name: item for item in elements}
        output: list[Observation] = []
        ids: dict[str, int] = {}
        for index, raw in enumerate(value[:MAX_OBSERVATIONS]):
            path = f"/observations/{index}"
            item = self.closed(
                raw,
                path,
                required={"id", "analysis_id", "quantity"},
                optional={"component"},
                code="experiment.observation.invalid",
            )
            if item is None:
                continue
            identifier = self.slug(
                item.get("id"),
                f"{path}/id",
                code="experiment.observation.invalid",
            )
            analysis_id = item.get("analysis_id")
            if not isinstance(analysis_id, str) or analysis_id not in analysis_by_id:
                self.add(
                    "experiment.observation.invalid",
                    f"{path}/analysis_id",
                    "must name an existing analysis id",
                )
                analysis = None
            else:
                analysis = analysis_by_id[analysis_id]
            component = item.get("component", "real")
            if component not in {"real", "imaginary"}:
                self.add(
                    "experiment.observation.component_incompatible",
                    f"{path}/component",
                    "must be real or imaginary",
                )
                component = None
            if analysis is not None and component == "imaginary" and analysis.kind != "ac":
                self.add(
                    "experiment.observation.component_incompatible",
                    f"{path}/component",
                    "imaginary components exist only for AC analyses",
                )
            quantity = item.get("quantity")
            kind: str | None = None
            native_name: str | None = None
            unit: str | None = None
            net: str | None = None
            element_name: str | None = None
            if not isinstance(quantity, Mapping):
                self.add(
                    "experiment.observation.invalid",
                    f"{path}/quantity",
                    "must be an object",
                )
            else:
                kind = quantity.get("kind")
                if kind == "node_voltage":
                    self.closed(
                        quantity,
                        f"{path}/quantity",
                        required={"kind", "net"},
                        code="experiment.observation.invalid",
                    )
                    raw_net = quantity.get("net")
                    if not isinstance(raw_net, str) or raw_net not in known_nets:
                        self.add(
                            "experiment.observation.unknown_net",
                            f"{path}/quantity/net",
                            "must name one external composed net",
                        )
                    else:
                        net = raw_net
                        native_name = f"v({net.lower()})"
                        unit = "V"
                elif kind == "element_current":
                    self.closed(
                        quantity,
                        f"{path}/quantity",
                        required={"kind", "element"},
                        code="experiment.observation.invalid",
                    )
                    raw_element = quantity.get("element")
                    element = element_by_name.get(raw_element)
                    if element is None:
                        self.add(
                            "experiment.observation.unknown_element",
                            f"{path}/quantity/element",
                            "must name one experiment-owned element",
                        )
                    elif element.kind not in _VOLTAGE_SOURCE_KINDS:
                        self.add(
                            "experiment.observation.current_source_unsupported",
                            f"{path}/quantity/element",
                            "v1 observes current only through experiment-owned independent voltage sources",
                        )
                    else:
                        element_name = element.name
                        native_name = f"i({element.emitted_name.lower()})"
                        unit = "A"
                else:
                    self.add(
                        "experiment.observation.dut_abi_missing",
                        f"{path}/quantity/kind",
                        (
                            "v1 supports only external node_voltage and experiment-owned "
                            "voltage-source element_current; DUT-internal observations have no ABI"
                        ),
                    )
            if identifier is not None:
                if identifier in ids:
                    self.add(
                        "experiment.observation.duplicate",
                        f"{path}/id",
                        f"duplicates /observations/{ids[identifier]}/id",
                    )
                else:
                    ids[identifier] = index
            if all(
                item is not None
                for item in (
                    identifier,
                    analysis,
                    component,
                    kind,
                    native_name,
                    unit,
                )
            ):
                output.append(
                    Observation(
                        identifier=str(identifier),
                        analysis_id=str(analysis_id),
                        kind=str(kind),
                        native_name=str(native_name),
                        component=str(component),
                        unit=str(unit),
                        net=net,
                        element=element_name,
                    )
                )
        identities: dict[tuple[str, str, str], str] = {}
        for item in output:
            identity = (item.analysis_id, item.native_name, item.component)
            prior = identities.get(identity)
            if prior is not None:
                self.add(
                    "experiment.observation.duplicate",
                    "/observations",
                    f"{prior!r} and {item.identifier!r} select the same native component",
                )
            else:
                identities[identity] = item.identifier
        return output

    def _profile_schema_issues(
        self,
        profile_id: str,
        request: object,
        path: str,
    ) -> None:
        definition_name = _SUPPORTED_MEASUREMENT_PROFILES[profile_id][0]
        try:
            profile = load_operation_profile(profile_id)
        except ProviderRuntimeError as exc:
            self.add(
                "experiment.measurement.request_invalid",
                path,
                exc.message,
                cause_code=exc.code,
            )
            return
        if profile is None:
            self.add(
                "experiment.measurement.profile_unsupported",
                path,
                f"installed profile {profile_id!r} is unavailable",
            )
            return
        parameter_schema = profile["request"]["parameters_schema"]
        schema = {
            "$schema": parameter_schema.get("$schema"),
            "$ref": f"#/$defs/{definition_name}",
            "$defs": parameter_schema["$defs"],
        }
        try:
            from jsonschema import Draft202012Validator, FormatChecker

            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            errors = sorted(
                validator.iter_errors(request),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
        except Exception as exc:
            self.add(
                "experiment.measurement.request_invalid",
                path,
                f"the installed live request schema could not be evaluated: {exc}",
            )
            return
        for error in errors[:128]:
            suffix = _pointer(tuple(error.absolute_path))
            self.add(
                "experiment.measurement.request_invalid",
                path + suffix,
                error.message,
            )
        if len(errors) > 128:
            self.add(
                "experiment.document.over_limit",
                path,
                "additional request-schema errors omitted after 128",
            )

    def _measurements(
        self,
        value: object,
        analyses: Sequence[Analysis],
        observations: Sequence[Observation],
    ) -> list[Measurement]:
        if not isinstance(value, list):
            self.add("experiment.measurement.request_invalid", "/measurements", "must be an array")
            return []
        if len(value) > MAX_MEASUREMENTS:
            self.add(
                "experiment.document.over_limit",
                "/measurements",
                f"contains {len(value)} entries; the limit is {MAX_MEASUREMENTS}",
            )
        analysis_by_id = {item.identifier: item for item in analyses}
        observation_by_id = {item.identifier: item for item in observations}
        output: list[Measurement] = []
        ids: dict[str, int] = {}
        for index, raw in enumerate(value[:MAX_MEASUREMENTS]):
            path = f"/measurements/{index}"
            item = self.closed(
                raw,
                path,
                required={"id", "analysis_id", "operation_profile", "request"},
                code="experiment.measurement.request_invalid",
            )
            if item is None:
                continue
            identifier = self.slug(
                item.get("id"),
                f"{path}/id",
                code="experiment.measurement.request_invalid",
            )
            if identifier is not None:
                if identifier in ids:
                    self.add(
                        "experiment.measurement.id_duplicate",
                        f"{path}/id",
                        f"duplicates /measurements/{ids[identifier]}/id",
                    )
                else:
                    ids[identifier] = index
            analysis_id = item.get("analysis_id")
            if analysis_id is None:
                self.add(
                    "experiment.measurement.analysis_required",
                    f"{path}/analysis_id",
                    "analysis_id is required; v1 has no index default",
                )
                analysis = None
            elif not isinstance(analysis_id, str) or analysis_id not in analysis_by_id:
                self.add(
                    "experiment.measurement.analysis_unknown",
                    f"{path}/analysis_id",
                    f"unknown analysis id {analysis_id!r}",
                )
                analysis = None
            else:
                analysis = analysis_by_id[analysis_id]
            profile = item.get("operation_profile")
            if profile not in _SUPPORTED_MEASUREMENT_PROFILES:
                self.add(
                    "experiment.measurement.profile_unsupported",
                    f"{path}/operation_profile",
                    "must be one exact supported live operation profile id",
                )
                profile = None
            request = item.get("request")
            if profile is not None:
                self._profile_schema_issues(profile, request, f"{path}/request")
            if isinstance(request, Mapping) and identifier is not None:
                if request.get("measurement_id") != identifier:
                    self.add(
                        "experiment.measurement.request_invalid",
                        f"{path}/request/measurement_id",
                        "must equal the enclosing experiment measurement id",
                    )
            expected_unit: str | None = None
            if (
                profile is not None
                and isinstance(request, Mapping)
                and analysis is not None
            ):
                expected_unit = self._measurement_bindings(
                    profile,
                    request,
                    analysis,
                    observation_by_id,
                    f"{path}/request",
                )
            if all(
                entry is not None
                for entry in (identifier, analysis, profile)
            ) and isinstance(request, Mapping):
                output.append(
                    Measurement(
                        identifier=str(identifier),
                        analysis_id=str(analysis_id),
                        operation_profile=str(profile),
                        request=dict(request),
                        expected_unit=expected_unit,
                    )
                )
        return output

    def _measurement_bindings(
        self,
        profile: str,
        request: Mapping[str, Any],
        analysis: Analysis,
        observations: Mapping[str, Observation],
        path: str,
    ) -> str | None:
        def observation(name: object, suffix: str) -> Observation | None:
            item = observations.get(name) if isinstance(name, str) else None
            if item is None or item.analysis_id != analysis.identifier:
                self.add(
                    "experiment.measurement.selector_missing",
                    path + suffix,
                    f"{name!r} is not an observation from analysis {analysis.identifier!r}",
                )
                return None
            return item

        if profile.endswith("/result.measure/v1alpha1"):
            selected = observation(request.get("signal"), "/signal")
            if selected is None:
                return None
            kind = request.get("kind")
            params = request.get("parameters")
            if isinstance(params, Mapping):
                self._ordinary_units(
                    str(kind),
                    params,
                    axis_unit=_analysis_axis_unit(analysis),
                    signal_unit=selected.unit,
                    path=path + "/parameters",
                )
            return (
                _analysis_axis_unit(analysis)
                if kind in {"crossing", "rise_time", "fall_time", "settling_time"}
                else selected.unit
            )

        if profile.endswith("/result.transfer.measure/v1alpha1"):
            if analysis.kind != "ac":
                self.add(
                    "experiment.measurement.analysis_incompatible",
                    path,
                    "result.transfer.measure requires an AC analysis",
                )
            try:
                from .result_transfer_measure import _normalize_request

                _normalize_request(request)
            except Exception as exc:
                self.add(
                    "experiment.measurement.request_invalid",
                    path,
                    str(exc),
                    cause_code=getattr(exc, "code", None),
                )
            selected: dict[str, Observation] = {}
            for side in ("input", "output"):
                operand = request.get(side)
                if not isinstance(operand, Mapping):
                    continue
                for name, value in operand.items():
                    item = observation(value, f"/{side}/{name}")
                    if item is not None:
                        selected[f"{side}.{name}"] = item
                        expected_component = (
                            "imaginary" if "imaginary" in name else "real"
                        )
                        if item.component != expected_component:
                            self.add(
                                "experiment.observation.component_incompatible",
                                f"{path}/{side}/{name}",
                                f"observation {item.identifier!r} is {item.component}, expected {expected_component}",
                            )
            metric = request.get("metric")
            metric_kind = metric.get("kind") if isinstance(metric, Mapping) else None
            input_units = {
                item.unit for key, item in selected.items() if key.startswith("input.")
            }
            output_units = {
                item.unit for key, item in selected.items() if key.startswith("output.")
            }
            if metric_kind == "low_frequency_impedance":
                if input_units and input_units != {"A"}:
                    self.add(
                        "experiment.measurement.request_invalid",
                        f"{path}/input",
                        "low_frequency_impedance input components must be amperes",
                    )
                if output_units and output_units != {"V"}:
                    self.add(
                        "experiment.measurement.request_invalid",
                        f"{path}/output",
                        "low_frequency_impedance output components must be volts",
                    )
            elif input_units and output_units and input_units != output_units:
                self.add(
                    "experiment.measurement.request_invalid",
                    path,
                    "dimensionless transfer metrics require identical input/output units",
                )
            return _TRANSFER_UNITS.get(metric_kind)

        if analysis.kind != "tran":
            self.add(
                "experiment.measurement.analysis_incompatible",
                path,
                "result.spectral.measure requires a transient analysis",
            )
        observation(request.get("signal"), "/signal")
        plan = analysis.document.get("sampling_plan")
        if not isinstance(plan, Mapping):
            self.add(
                "experiment.measurement.analysis_incompatible",
                path,
                "result.spectral.measure requires a coherent_uniform sampling_plan on its transient analysis",
            )
            return "dB"
        dft_length = (
            ((request.get("method") or {}).get("dft_length"))
            if isinstance(request.get("method"), Mapping)
            else None
        )
        if not isinstance(dft_length, int):
            return "dB"
        point_count = plan.get("point_count")
        if dft_length != point_count:
            self.add(
                "experiment.measurement.request_invalid",
                f"{path}/method/dft_length",
                "must equal the transient sampling_plan point_count",
            )
            return "dB"
        # Validate the semantic portion of the live request with the exact
        # declared record length.  This is the same normalizer used at runtime.
        try:
            from .result_spectral_measure import _normalize_request

            _normalize_request(request, point_count=int(point_count))
        except Exception as exc:
            self.add(
                "experiment.measurement.request_invalid",
                path,
                str(exc),
                cause_code=getattr(exc, "code", None),
            )
        return "dB"

    def _ordinary_units(
        self,
        kind: str,
        params: Mapping[str, Any],
        *,
        axis_unit: str,
        signal_unit: str,
        path: str,
    ) -> None:
        def quantity(name: str, expected: str) -> float | None:
            value = params.get(name)
            if not isinstance(value, Mapping):
                return None
            unit = value.get("unit")
            if unit != expected:
                self.add(
                    "experiment.measurement.request_invalid",
                    f"{path}/{name}/unit",
                    f"must be exactly {expected!r}",
                )
            number = value.get("value")
            return float(number) if isinstance(number, (int, float)) and not isinstance(number, bool) else None

        def window() -> None:
            value = params.get("window")
            if not isinstance(value, Mapping):
                return
            start = value.get("start")
            stop = value.get("stop")
            for name, item in (("start", start), ("stop", stop)):
                if isinstance(item, Mapping) and item.get("unit") != axis_unit:
                    self.add(
                        "experiment.measurement.request_invalid",
                        f"{path}/window/{name}/unit",
                        f"must be exactly {axis_unit!r}",
                    )
            if isinstance(start, Mapping) and isinstance(stop, Mapping):
                left = start.get("value")
                right = stop.get("value")
                if (
                    isinstance(left, (int, float))
                    and not isinstance(left, bool)
                    and isinstance(right, (int, float))
                    and not isinstance(right, bool)
                    and right <= left
                ):
                    self.add(
                        "experiment.measurement.request_invalid",
                        f"{path}/window",
                        "window stop must be greater than start",
                    )

        if kind == "sample_at":
            quantity("at", axis_unit)
        elif kind in {"minimum", "maximum", "mean", "rms"}:
            window()
        elif kind == "crossing":
            quantity("threshold", signal_unit)
            window()
        elif kind in {"rise_time", "fall_time"}:
            lower = quantity("lower_threshold", signal_unit)
            upper = quantity("upper_threshold", signal_unit)
            if lower is not None and upper is not None and upper <= lower:
                self.add(
                    "experiment.measurement.request_invalid",
                    path,
                    "upper_threshold must be greater than lower_threshold",
                )
            window()
        elif kind == "settling_time":
            quantity("target", signal_unit)
            tolerance = quantity("tolerance", signal_unit)
            quantity("reference", axis_unit)
            hold = quantity("hold_for", axis_unit)
            if tolerance is not None and tolerance <= 0:
                self.add(
                    "experiment.measurement.request_invalid",
                    f"{path}/tolerance/value",
                    "must be greater than zero",
                )
            if hold is not None and hold <= 0:
                self.add(
                    "experiment.measurement.request_invalid",
                    f"{path}/hold_for/value",
                    "must be greater than zero",
                )
            window()

    def _derivations(
        self,
        value: object,
        analyses: Sequence[Analysis],
        measurements: Sequence[Measurement],
    ) -> list[Derivation]:
        if not isinstance(value, list):
            self.add("experiment.derivation.kind_unsupported", "/derivations", "must be an array")
            return []
        if len(value) > MAX_DERIVATIONS:
            self.add(
                "experiment.document.over_limit",
                "/derivations",
                f"contains {len(value)} entries; the limit is {MAX_DERIVATIONS}",
            )
        analysis_ids = {item.identifier for item in analyses}
        measurement_by_id = {item.identifier: item for item in measurements}
        output: list[Derivation] = []
        ids: dict[str, int] = {}
        for index, raw in enumerate(value[:MAX_DERIVATIONS]):
            path = f"/derivations/{index}"
            item = self.closed(
                raw,
                path,
                required={"id", "kind", "analysis_id", "parents"},
                code="experiment.derivation.kind_unsupported",
            )
            if item is None:
                continue
            identifier = self.slug(
                item.get("id"),
                f"{path}/id",
                code="experiment.derivation.kind_unsupported",
            )
            if identifier is not None:
                if identifier in ids or identifier in measurement_by_id:
                    self.add(
                        "experiment.derivation.context_mismatch",
                        f"{path}/id",
                        "derivation ids must be unique across measurements and derivations",
                    )
                else:
                    ids[identifier] = index
            if item.get("kind") != "subtract":
                self.add(
                    "experiment.derivation.kind_unsupported",
                    f"{path}/kind",
                    "v1 supports only subtract",
                )
            analysis_id = item.get("analysis_id")
            if not isinstance(analysis_id, str) or analysis_id not in analysis_ids:
                self.add(
                    "experiment.derivation.context_mismatch",
                    f"{path}/analysis_id",
                    "must name an existing analysis",
                )
            parents = item.get("parents")
            parent_values: list[str] = []
            if (
                not isinstance(parents, list)
                or len(parents) != 2
                or any(not isinstance(parent, str) for parent in parents)
            ):
                self.add(
                    "experiment.derivation.parent_unknown",
                    f"{path}/parents",
                    "subtract requires exactly two measurement ids in order",
                )
            else:
                parent_values = list(parents)
                for parent_index, parent in enumerate(parent_values):
                    if parent not in measurement_by_id:
                        self.add(
                            "experiment.derivation.parent_unknown",
                            f"{path}/parents/{parent_index}",
                            f"unknown parent measurement {parent!r}",
                        )
                selected = [
                    measurement_by_id[parent]
                    for parent in parent_values
                    if parent in measurement_by_id
                ]
                if len(selected) == 2:
                    units = {entry.expected_unit for entry in selected}
                    if len(units) != 1 or None in units:
                        self.add(
                            "experiment.derivation.unit_mismatch",
                            f"{path}/parents",
                            "subtract parents must have one identical statically known unit",
                        )
                    contexts = {entry.analysis_id for entry in selected}
                    if contexts != {analysis_id}:
                        self.add(
                            "experiment.derivation.context_mismatch",
                            f"{path}/parents",
                            "subtract parents must belong to the declared same analysis",
                        )
                    if any(
                        entry.operation_profile
                        != "openada.operation/result.measure/v1alpha1"
                        or entry.request.get("kind") != "crossing"
                        for entry in selected
                    ):
                        self.add(
                            "experiment.derivation.context_mismatch",
                            f"{path}/parents",
                            "v1 subtract parents must both be crossing measurements",
                        )
            if (
                identifier is not None
                and item.get("kind") == "subtract"
                and isinstance(analysis_id, str)
                and analysis_id in analysis_ids
                and len(parent_values) == 2
                and all(parent in measurement_by_id for parent in parent_values)
            ):
                output.append(
                    Derivation(
                        identifier=identifier,
                        analysis_id=analysis_id,
                        parents=(parent_values[0], parent_values[1]),
                    )
                )
        return output

    def _conditions(self, value: object) -> ResolvedPdkBinding | None:
        conditions = self.closed(
            value,
            "/conditions",
            required={"pdk"},
            code="experiment.condition.invalid",
        )
        if conditions is None:
            return None
        pdk = self.closed(
            conditions.get("pdk"),
            "/conditions/pdk",
            required={"id", "corner"},
            optional={"temperature_c"},
            code="experiment.condition.invalid",
        )
        if pdk is None:
            return None
        if "supply_overrides" in conditions:
            self.add(
                "experiment.condition.supply_override_unsupported",
                "/conditions/supply_overrides",
                "supplies have one authority: experiment element values",
            )
        if "supply_overrides" in pdk:
            self.add(
                "experiment.condition.supply_override_unsupported",
                "/conditions/pdk/supply_overrides",
                "supplies have one authority: experiment element values",
            )
        pdk_id = pdk.get("id")
        corner = pdk.get("corner")
        if not isinstance(pdk_id, str):
            self.add("experiment.condition.invalid", "/conditions/pdk/id", "must be text")
            return None
        if pdk_id != self.cli_pdk:
            self.add(
                "experiment.condition.pdk_conflict",
                "/conditions/pdk/id",
                f"specification PDK {pdk_id!r} differs from CLI --pdk {self.cli_pdk!r}",
            )
        if not isinstance(corner, str):
            self.add("experiment.condition.invalid", "/conditions/pdk/corner", "must be text")
            return None
        if pdk_id not in REGISTRY:
            self.add(
                "experiment.pdk.unavailable",
                "/conditions/pdk/id",
                f"no reviewed binding profile exists for {pdk_id!r}",
                cause_code="pdk.unknown",
            )
            return None
        if self.pdk_root is None:
            self.add(
                "experiment.pdk.root_invalid",
                "/conditions/pdk/id",
                "PDK binding requires --pdk-root or PDK_ROOT",
                cause_code="pdk.root.required",
            )
            return None
        try:
            resolved = resolve_pdk_binding(pdk_id, self.pdk_root, corner=corner)
        except PdkBindingError as exc:
            code = (
                "experiment.condition.corner_unknown"
                if exc.code in {"pdk.corner.invalid", "pdk.corner.unknown"}
                else "experiment.pdk.binding_failed"
            )
            self.add(code, "/conditions/pdk", exc.message, cause_code=exc.code)
            return None
        if "temperature_c" in pdk:
            temperature = _scalar(pdk.get("temperature_c"))
            profile_temperature = _scalar(resolved.binding.simulation_temperature_c)
            if (
                temperature is None
                or profile_temperature is None
                or temperature.value != profile_temperature.value
            ):
                self.add(
                    "experiment.condition.temperature_unsupported",
                    "/conditions/pdk/temperature_c",
                    (
                        "v1 runs only at the binding profile temperature "
                        f"{resolved.binding.simulation_temperature_c} degC"
                    ),
                )
        return resolved


def _validation_payload(
    issues: Sequence[ExperimentIssue],
    *,
    execution_status: str = "invalid_request",
    engineering_status: str = "unknown",
    summary: str = "The experiment request was refused before any simulator ran.",
    manifest: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    ordered = sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message))
    return result(
        OPERATION_NAME,
        tool=None,
        execution=static_execution(execution_status),
        engineering_status=engineering_status,
        summary=summary,
        artifacts=artifacts,
        diagnostics=[item.envelope_diagnostic() for item in ordered],
        data={
            "schema": EXPERIMENT_RUN_SCHEMA,
            "refusals": [item.record() for item in ordered],
            "manifest": dict(manifest) if manifest is not None else None,
            "extensions": {},
        },
    )


def validate_experiment(
    spec_path: str | Path,
    *,
    pdk: str,
    pdk_root: str | Path | None,
) -> tuple[PreparedExperiment | None, list[ExperimentIssue]]:
    """Read and fully validate an experiment without creating output files."""

    path = Path(spec_path).expanduser().resolve()
    try:
        raw = _read_spec_bytes(path)
    except ValueError as exc:
        return None, [
            ExperimentIssue(
                "experiment.document.invalid",
                "",
                str(exc),
            )
        ]
    document, parse_issues = _decode_json(raw)
    if document is None:
        return None, parse_issues
    validator = _Validator(
        document=document,
        spec_path=path,
        spec_bytes=raw,
        cli_pdk=pdk,
        pdk_root=pdk_root,
    )
    validator.issues.extend(parse_issues)
    prepared = validator.validate()
    return prepared, validator.issues


def _write_bytes(path: Path, body: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return file_record(path, kind="experiment-evidence", role="experiment.artifact")


def _write_json(path: Path, value: object, *, role: str) -> dict[str, Any]:
    record = _write_bytes(path, _json_bytes(value))
    record["kind"] = "openada-result" if role.endswith("result") else "experiment-json"
    record["role"] = role
    return record


def _raw_artifact(envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in envelope.get("artifacts", ())
        if isinstance(item, Mapping)
        and item.get("role") == "simulation.result"
        and item.get("exists") is True
    ]
    return matches[0] if len(matches) == 1 else None


def _input_by_sha256(
    envelope: Mapping[str, Any], sha256: str
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in envelope.get("inputs", ())
        if isinstance(item, Mapping)
        and item.get("kind") == "spice-netlist"
        and item.get("sha256") == sha256
        and item.get("exists") is True
    ]
    return matches[0] if len(matches) == 1 else None


_RAW_VARIABLE_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(\S+)")


def _raw_vector_names(path: Path) -> tuple[str, ...]:
    try:
        with path.open("rb") as handle:
            header = handle.read(MAX_RAW_HEADER_BYTES)
    except OSError:
        return ()
    names: list[str] = []
    collecting = False
    for line in header.decode("latin-1", errors="replace").splitlines():
        lowered = line.strip().casefold()
        if lowered.startswith("variables:"):
            collecting = True
            continue
        if not collecting:
            continue
        if lowered.startswith(("binary:", "values:")):
            break
        match = _RAW_VARIABLE_RE.match(line)
        if match is None:
            break
        names.append(match.group(1))
    return tuple(names)


def _conditions_for_extraction(prepared: PreparedExperiment) -> list[dict[str, object]]:
    binding = prepared.resolved_pdk
    try:
        temperature: object = float(binding.binding.simulation_temperature_c)
    except ValueError:
        temperature = binding.binding.simulation_temperature_c
    return [
        {"name": "pdk", "value": binding.pdk_id, "unit": "1"},
        {"name": "corner", "value": binding.corner, "unit": "1"},
        {"name": "temperature", "value": temperature, "unit": "degC"},
    ]


def _measurement_value(envelope: Mapping[str, Any]) -> tuple[float, str] | None:
    data = envelope.get("data")
    measurement = data.get("measurement") if isinstance(data, Mapping) else None
    if not isinstance(measurement, Mapping) or measurement.get("status") != "measured":
        return None
    value = measurement.get("value")
    unit = measurement.get("unit")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not isinstance(unit, str)
    ):
        return None
    return float(value), unit


def _operation_request_sha(envelope: Mapping[str, Any]) -> str | None:
    data = envelope.get("data")
    measurement = data.get("measurement") if isinstance(data, Mapping) else None
    value = measurement.get("request_sha256") if isinstance(measurement, Mapping) else None
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _experiment_extension(
    prepared: PreparedExperiment,
    run: PreparedRun,
) -> dict[str, Any]:
    bundle_digests = {
        name: prepared.bundle.bundle_digests[name]
        for name in (
            "descriptor_sha256",
            "source_sha256",
            "view_sha256",
            "netlist_sha256",
            "cdl_sha256",
        )
    }
    return {
        "schema": EXPERIMENT_SCHEMA,
        # Compatibility aliases retained alongside the amended complete record.
        "spec_sha256": prepared.spec_raw_sha256,
        "dut_netlist_sha256": bundle_digests["netlist_sha256"],
        "spec_raw_sha256": prepared.spec_raw_sha256,
        "spec_canonical_sha256": prepared.spec_canonical_sha256,
        "dut_bundle": bundle_digests,
        "base_deck_sha256": prepared.base_deck_sha256,
        "run_deck_sha256": run.portable_sha256,
        "analysis_id": run.analysis.identifier,
        "composer_version": COMPOSER_VERSION,
    }


def _run_experiment_impl(
    spec_path: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    pdk: str,
    pdk_root: str | Path | None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Validate, compose, execute, extract, and measure one experiment."""

    prepared, issues = validate_experiment(spec_path, pdk=pdk, pdk_root=pdk_root)
    if prepared is None or issues:
        return _validation_payload(issues)

    destination = Path(output_dir).expanduser().resolve()
    if destination.exists():
        try:
            occupied = any(destination.iterdir())
        except OSError as exc:
            return _validation_payload(
                [
                    ExperimentIssue(
                        "experiment.evidence.collection_incomplete",
                        "",
                        f"the output directory cannot be inspected: {exc}",
                    )
                ],
                execution_status="failed",
            )
        if occupied:
            return _validation_payload(
                [
                    ExperimentIssue(
                        "experiment.evidence.collection_incomplete",
                        "",
                        f"the output directory must be absent or empty: {destination}",
                    )
                ]
            )
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _validation_payload(
            [
                ExperimentIssue(
                    "experiment.evidence.collection_incomplete",
                    "",
                    f"the output directory could not be created: {exc}",
                )
            ],
            execution_status="failed",
        )

    artifacts: list[dict[str, Any]] = []
    runtime_issues: list[ExperimentIssue] = []
    spec_record = _write_bytes(destination / "experiment.spec.json", prepared.spec_bytes)
    spec_record["kind"] = "experiment-specification"
    spec_record["role"] = "experiment.specification"
    artifacts.append(spec_record)
    base_record = _write_bytes(
        destination / "base.spice", prepared.base_deck.encode("utf-8")
    )
    base_record["kind"] = "spice-netlist"
    base_record["role"] = "experiment.base-deck"
    artifacts.append(base_record)

    run_id = str(uuid.uuid4())
    manifest: dict[str, Any] = {
        "schema": EXPERIMENT_RUN_SCHEMA,
        "run_id": run_id,
        "experiment_id": prepared.identifier,
        "spec": {
            "path": spec_record["path"],
            "raw_sha256": prepared.spec_raw_sha256,
            "canonical_sha256": prepared.spec_canonical_sha256,
        },
        "pdk": {
            "id": prepared.resolved_pdk.pdk_id,
            "corner": prepared.resolved_pdk.corner,
            "temperature_c": prepared.resolved_pdk.binding.simulation_temperature_c,
        },
        "base_deck": {
            "path": base_record["path"],
            "sha256": prepared.base_deck_sha256,
        },
        "analyses": [],
        "derivations": [],
        "status": "running",
        "extensions": {},
    }

    measurement_results: dict[str, tuple[Measurement, Mapping[str, Any], dict[str, Any]]] = {}
    measurements_by_analysis: dict[str, list[Measurement]] = defaultdict(list)
    for measurement in prepared.measurements:
        measurements_by_analysis[measurement.analysis_id].append(measurement)

    extra_inputs = [
        spec_record,
        base_record,
        *[dict(record) for record in prepared.bundle.input_records],
    ]
    extraction_conditions = _conditions_for_extraction(prepared)

    for run in prepared.runs:
        analysis_dir = destination / "analyses" / run.analysis.identifier
        analysis_dir.mkdir(parents=True, exist_ok=False)
        run_record = _write_bytes(
            analysis_dir / "run.spice", run.portable_deck.encode("utf-8")
        )
        run_record["kind"] = "spice-netlist"
        run_record["role"] = "experiment.run-deck"
        artifacts.append(run_record)
        extension = _experiment_extension(prepared, run)
        simulation = simulate(
            Path(run_record["path"]),
            analysis_dir / "simulation",
            discovery=discovery,
            backend="ngspice",
            pdk=prepared.resolved_pdk.pdk_id,
            pdk_root=prepared.pdk_root,
            corner=prepared.resolved_pdk.corner,
            timeout=timeout,
            request_id=str(uuid.uuid4()),
            saved_nets=run.saved_nets,
            retained_current_sources=run.retained_current_sources,
            extra_input_records=extra_inputs,
            extra_data_extensions={EXPERIMENT_EXTENSION: extension},
        )
        simulation_path = analysis_dir / "simulation" / "simulate.result.json"
        analysis_manifest: dict[str, Any] = {
            "id": run.analysis.identifier,
            "kind": run.analysis.kind,
            "portable_deck": {
                "path": run_record["path"],
                "sha256": run.portable_sha256,
            },
            "preflight_bound_deck_sha256": run.bound_deck_sha256,
            "simulation": {
                "path": str(simulation_path),
                "request_id": ((simulation.get("data") or {}).get("protocol") or {}).get("request_id"),
                "execution_status": (simulation.get("execution") or {}).get("status"),
                "engineering_status": (simulation.get("engineering") or {}).get("status"),
            },
            "extraction": None,
            "measurements": [],
            "status": "unknown",
        }
        if simulation_path.is_file():
            try:
                simulation_record = file_record(
                    simulation_path,
                    kind="openada-result",
                    role="simulation.envelope",
                )
                artifacts.append(simulation_record)
                analysis_manifest["simulation"]["sha256"] = simulation_record.get("sha256")
            except FileRecordError as exc:
                runtime_issues.append(
                    ExperimentIssue(
                        "experiment.result.missing",
                        f"/analyses/{run.analysis.identifier}",
                        f"simulate envelope could not be content-bound: {exc}",
                    )
                )
        actual_bound_deck = _input_by_sha256(
            simulation, run.bound_deck_sha256
        )
        if actual_bound_deck is not None:
            analysis_manifest["bound_deck"] = {
                "path": actual_bound_deck.get("path"),
                "sha256": actual_bound_deck.get("sha256"),
            }
        if (
            (simulation.get("execution") or {}).get("status") == "completed"
            and (simulation.get("engineering") or {}).get("status") == "pass"
            and actual_bound_deck is None
        ):
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.result.missing",
                    f"/analyses/{run.analysis.identifier}",
                    (
                        "the passing simulation envelope names no unique retained "
                        "input matching the validated PDK-bound deck digest"
                    ),
                )
            )
            manifest["analyses"].append(analysis_manifest)
            continue
        if actual_bound_deck is not None:
            try:
                rebound = file_record(
                    Path(str(actual_bound_deck.get("path"))),
                    kind="spice-netlist",
                    role="simulation.deck",
                )
            except FileRecordError as exc:
                runtime_issues.append(
                    ExperimentIssue(
                        "experiment.compose.deck_mismatch",
                        f"/analyses/{run.analysis.identifier}",
                        f"the retained PDK-bound deck could not be re-content-bound: {exc}",
                    )
                )
                manifest["analyses"].append(analysis_manifest)
                continue
            if rebound.get("sha256") != run.bound_deck_sha256:
                runtime_issues.append(
                    ExperimentIssue(
                        "experiment.compose.deck_mismatch",
                        f"/analyses/{run.analysis.identifier}",
                        "the retained PDK-bound deck differs from the fully validated preflight deck",
                    )
                )
                manifest["analyses"].append(analysis_manifest)
                continue
        if (
            (simulation.get("execution") or {}).get("status") != "completed"
            or (simulation.get("engineering") or {}).get("status") != "pass"
        ):
            diagnostic_count = 0
            for entry in simulation.get("diagnostics") or ():
                if not isinstance(entry, Mapping):
                    continue
                diagnostic_count += 1
                runtime_issues.append(
                    ExperimentIssue(
                        (
                            "experiment.result.execution_incomplete"
                            if (simulation.get("execution") or {}).get("status") != "completed"
                            else "experiment.result.engineering_failed"
                        ),
                        f"/analyses/{run.analysis.identifier}",
                        str(entry.get("message") or "simulation did not pass"),
                        cause_code=str(entry.get("code")) if entry.get("code") else None,
                    )
                )
            if diagnostic_count == 0:
                runtime_issues.append(
                    ExperimentIssue(
                        (
                            "experiment.result.execution_incomplete"
                            if (simulation.get("execution") or {}).get("status")
                            != "completed"
                            else "experiment.result.engineering_failed"
                        ),
                        f"/analyses/{run.analysis.identifier}",
                        "simulation did not produce a complete passing envelope",
                    )
                )
            manifest["analyses"].append(analysis_manifest)
            continue

        raw = _raw_artifact(simulation)
        if raw is None:
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.result.raw_missing",
                    f"/analyses/{run.analysis.identifier}",
                    "the passing simulation envelope names no unique simulation.result artifact",
                )
            )
            manifest["analyses"].append(analysis_manifest)
            continue
        raw_path = Path(str(raw["path"]))
        available = {name.casefold() for name in _raw_vector_names(raw_path)}
        missing = sorted(
            {
                observation.native_name
                for observation in run.observations
                if observation.native_name.casefold() not in available
            }
        )
        if missing:
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.observation.not_retained",
                    f"/analyses/{run.analysis.identifier}",
                    "requested native vectors are absent from the retained raw: "
                    + ", ".join(missing),
                )
            )
            manifest["analyses"].append(analysis_manifest)
            continue

        if not run.observations:
            analysis_manifest["status"] = "pass"
            manifest["analyses"].append(analysis_manifest)
            continue

        selection = {
            "selectors": [item.selector for item in run.observations],
            "conditions": extraction_conditions,
            "extensions": {},
        }
        selection_path = analysis_dir / "extract.request.json"
        selection_record = _write_json(
            selection_path, selection, role="series.extraction-request"
        )
        artifacts.append(selection_record)
        retained_selection = json.loads(selection_path.read_text(encoding="utf-8"))
        extraction = extract_result_series(
            simulation,
            raw_path,
            retained_selection["selectors"],
            conditions=retained_selection["conditions"],
            request_id=str(uuid.uuid4()),
        )
        extraction_path = analysis_dir / "extract.result.json"
        extraction_record = _write_json(
            extraction_path, extraction, role="series.extraction-result"
        )
        artifacts.append(extraction_record)
        extraction_data = (extraction.get("data") or {}).get("extraction") or {}
        analysis_manifest["extraction"] = {
            "request_path": selection_record["path"],
            "request_raw_sha256": selection_record["sha256"],
            "request_canonical_sha256": extraction_data.get("request_sha256"),
            "result_path": extraction_record["path"],
            "result_sha256": extraction_record["sha256"],
            "engineering_status": (extraction.get("engineering") or {}).get("status"),
        }
        if (extraction.get("engineering") or {}).get("status") != "pass":
            for entry in extraction.get("diagnostics") or ():
                if not isinstance(entry, Mapping):
                    continue
                runtime_issues.append(
                    ExperimentIssue(
                        (
                            "experiment.observation.not_retained"
                            if entry.get("code") in {"series.selector.missing", "series.selector.component_invalid"}
                            else "experiment.result.envelope_invalid"
                        ),
                        f"/analyses/{run.analysis.identifier}",
                        str(entry.get("message") or "series extraction did not pass"),
                        cause_code=str(entry.get("code")) if entry.get("code") else None,
                    )
                )
            manifest["analyses"].append(analysis_manifest)
            continue
        series = extraction_data.get("series")
        if not isinstance(series, Mapping):
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.result.series_digest_mismatch",
                    f"/analyses/{run.analysis.identifier}",
                    "passing extraction carries no normalized series",
                )
            )
            manifest["analyses"].append(analysis_manifest)
            continue

        analysis_measurements_pass = True
        for measurement in measurements_by_analysis.get(run.analysis.identifier, ()):
            request_path = analysis_dir / "measurements" / f"{measurement.identifier}.request.json"
            request_record = _write_json(
                request_path,
                measurement.request,
                role="measurement.request",
            )
            artifacts.append(request_record)
            retained_request = json.loads(request_path.read_text(encoding="utf-8"))
            if measurement.operation_profile.endswith("/result.measure/v1alpha1"):
                measured = measure_result(
                    series, retained_request, request_id=str(uuid.uuid4())
                )
            elif measurement.operation_profile.endswith(
                "/result.transfer.measure/v1alpha1"
            ):
                measured = measure_transfer(
                    series, retained_request, request_id=str(uuid.uuid4())
                )
            else:
                measured = measure_spectrum(
                    series, retained_request, request_id=str(uuid.uuid4())
                )
            result_path = analysis_dir / "measurements" / f"{measurement.identifier}.result.json"
            result_record = _write_json(
                result_path, measured, role="measurement.result"
            )
            artifacts.append(result_record)
            canonical_request_sha = _operation_request_sha(measured)
            measurement_manifest = {
                "id": measurement.identifier,
                "operation_profile": measurement.operation_profile,
                "request_path": request_record["path"],
                "request_raw_sha256": request_record["sha256"],
                "request_canonical_sha256": canonical_request_sha,
                "result_path": result_record["path"],
                "result_sha256": result_record["sha256"],
                "execution_status": (measured.get("execution") or {}).get("status"),
                "engineering_status": (measured.get("engineering") or {}).get("status"),
            }
            value = _measurement_value(measured)
            if value is not None:
                measurement_manifest["value"] = value[0]
                measurement_manifest["unit"] = value[1]
            analysis_manifest["measurements"].append(measurement_manifest)
            if (
                (measured.get("execution") or {}).get("status") != "completed"
                or (measured.get("engineering") or {}).get("status") != "pass"
                or canonical_request_sha is None
                or value is None
            ):
                analysis_measurements_pass = False
                for entry in measured.get("diagnostics") or ():
                    if not isinstance(entry, Mapping):
                        continue
                    runtime_issues.append(
                        ExperimentIssue(
                            "experiment.result.engineering_failed",
                            f"/measurements/{measurement.identifier}",
                            str(entry.get("message") or "measurement did not pass"),
                            cause_code=str(entry.get("code")) if entry.get("code") else None,
                        )
                    )
            measurement_results[measurement.identifier] = (
                measurement,
                measured,
                result_record,
            )

        analysis_manifest["status"] = (
            "pass" if analysis_measurements_pass else "fail"
        )
        manifest["analyses"].append(analysis_manifest)

    derivation_dir = destination / "derivations"
    for derivation in prepared.derivations:
        parents = [
            measurement_results.get(parent)
            for parent in derivation.parents
        ]
        if any(parent is None for parent in parents):
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.derivation.parent_unknown",
                    f"/derivations/{derivation.identifier}",
                    "one or both parent measurement results are unavailable",
                )
            )
            continue
        parent_records: list[dict[str, Any]] = []
        values: list[tuple[float, str]] = []
        for parent_id, parent in zip(derivation.parents, parents):
            assert parent is not None
            _measurement, envelope, record = parent
            value = _measurement_value(envelope)
            if value is None:
                break
            values.append(value)
            parent_records.append(
                {
                    "measurement_id": parent_id,
                    "result_path": record["path"],
                    "result_sha256": record["sha256"],
                    "value": value[0],
                    "unit": value[1],
                }
            )
        if len(values) != 2 or values[0][1] != values[1][1]:
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.derivation.unit_mismatch",
                    f"/derivations/{derivation.identifier}",
                    "parent results are missing or have different units",
                )
            )
            continue
        derived_value = values[0][0] - values[1][0]
        if not math.isfinite(derived_value):
            runtime_issues.append(
                ExperimentIssue(
                    "experiment.result.value_invalid",
                    f"/derivations/{derivation.identifier}",
                    "subtraction did not produce a finite scalar",
                )
            )
            continue
        derived = {
            "schema": "simra.experiment-derivation/v1",
            "id": derivation.identifier,
            "kind": "derivation",
            "operation": "subtract",
            "analysis_id": derivation.analysis_id,
            "parents": parent_records,
            "status": "derived",
            "value": derived_value,
            "unit": values[0][1],
            "extensions": {},
        }
        derived_record = _write_json(
            derivation_dir / f"{derivation.identifier}.result.json",
            derived,
            role="derivation.result",
        )
        artifacts.append(derived_record)
        manifest["derivations"].append(
            {
                "id": derivation.identifier,
                "result_path": derived_record["path"],
                "result_sha256": derived_record["sha256"],
                "value": derived_value,
                "unit": values[0][1],
                "parents": parent_records,
            }
        )

    manifest["status"] = "fail" if runtime_issues else "pass"
    log_lines = [
        "openada experiment run",
        f"run_id={run_id}",
        f"experiment_id={prepared.identifier}",
        f"status={manifest['status']}",
        f"analysis_count={len(manifest['analyses'])}",
        f"measurement_count={len(measurement_results)}",
        f"derivation_count={len(manifest['derivations'])}",
        f"diagnostic_count={len(runtime_issues)}",
    ]
    for analysis_record in manifest["analyses"]:
        log_lines.append(
            "analysis "
            + str(analysis_record.get("id"))
            + " "
            + str(analysis_record.get("status"))
        )
    log_record = _write_bytes(
        destination / "experiment.log",
        ("\n".join(log_lines) + "\n").encode("utf-8"),
    )
    log_record["kind"] = "text-log"
    log_record["role"] = "experiment.log"
    artifacts.append(log_record)
    manifest["log"] = {
        "path": log_record["path"],
        "sha256": log_record["sha256"],
    }
    manifest_record = _write_json(
        destination / "run-manifest.json",
        manifest,
        role="experiment.run-manifest",
    )
    artifacts.append(manifest_record)

    if runtime_issues:
        return _validation_payload(
            runtime_issues,
            execution_status="completed",
            engineering_status="fail",
            summary="The experiment ran, but one or more analyses or measurements did not pass.",
            manifest=manifest,
            artifacts=artifacts,
        )
    return result(
        OPERATION_NAME,
        tool=None,
        execution=static_execution("completed"),
        engineering_status="pass",
        summary="Every experiment analysis, extraction, measurement, and derivation passed.",
        artifacts=artifacts,
        data={
            "schema": EXPERIMENT_RUN_SCHEMA,
            "refusals": [],
            "manifest": manifest,
            "extensions": {},
        },
    )


def run_experiment(
    spec_path: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    pdk: str,
    pdk_root: str | Path | None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run one experiment and keep evidence-publication failures typed."""

    try:
        return _run_experiment_impl(
            spec_path,
            output_dir,
            discovery=discovery,
            pdk=pdk,
            pdk_root=pdk_root,
            timeout=timeout,
        )
    except (FileRecordError, OSError) as exc:
        return _validation_payload(
            [
                ExperimentIssue(
                    "experiment.evidence.collection_incomplete",
                    "",
                    f"the experiment evidence bundle could not be completed: {exc}",
                )
            ],
            execution_status="failed",
            engineering_status="fail",
            summary="The experiment evidence bundle could not be completed.",
        )


__all__ = [
    "COMPOSER_VERSION",
    "EXPERIMENT_EXTENSION",
    "EXPERIMENT_RUN_SCHEMA",
    "EXPERIMENT_SCHEMA",
    "ExperimentIssue",
    "PreparedExperiment",
    "run_experiment",
    "validate_experiment",
]
