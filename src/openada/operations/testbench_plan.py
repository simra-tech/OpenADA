"""Closed ``simra.testbench-plan/v1`` contract and semantic validator.

The plan is data, never a SPICE template.  This module accepts strict JSON,
normalizes the bounded graph into immutable records, and refuses unresolved or
ambiguous identities before a compiler or simulator can observe it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import json
import math
import os
from pathlib import Path
import re
import sysconfig
from typing import Any

from ..contract import FileRecordError, stable_regular_file


TESTBENCH_PLAN_SCHEMA = "simra.testbench-plan/v1"
MAX_PLAN_BYTES = 4 * 1024 * 1024

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SPICE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _jsonschema_types():
    """Keep unrelated CLI leaves usable when site packages are hidden."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - exercised through the -S CLI test
        raise ValueError("testbench-plan validation requires jsonschema") from exc
    return Draft202012Validator, FormatChecker


@dataclass(frozen=True, slots=True)
class TestbenchPlanIssue:
    """One stable plan refusal at a JSON Pointer."""

    code: str
    path: str
    message: str

    def record(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message[:4000]}


@dataclass(frozen=True, slots=True)
class DutBinding:
    artifact: str
    sha256: str
    namespace: str
    top: str
    ports: tuple[Mapping[str, Any], ...]
    connections: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Supply:
    identifier: str
    positive: str
    negative: str
    voltage: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Stimulus:
    identifier: str
    kind: str
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Probe:
    identifier: str
    kind: str
    unit: str
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Measurement:
    identifier: str
    kind: str
    unit: str
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Reduction:
    identifier: str
    kind: str
    unit: str
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ConditionPoint:
    identifier: str
    document: Mapping[str, Any]
    measurements: tuple[Measurement, ...]


@dataclass(frozen=True, slots=True)
class Stage:
    identifier: str
    depends_on: tuple[str, ...]
    points: tuple[ConditionPoint, ...]
    reductions: tuple[Reduction, ...] = ()


@dataclass(frozen=True, slots=True)
class Observable:
    identifier: str
    source: Mapping[str, str]
    unit: str
    shape: str


@dataclass(frozen=True, slots=True)
class PreparedTestbenchPlan:
    document: Mapping[str, Any]
    identifier: str
    dut: DutBinding
    supplies: tuple[Supply, ...]
    corner_bindings: tuple[Mapping[str, Any], ...]
    stimuli: tuple[Stimulus, ...]
    probes: tuple[Probe, ...]
    stages: tuple[Stage, ...]
    observables: tuple[Observable, ...]
    raw_sha256: str
    canonical_sha256: str
    dut_binding_canonical_sha256: str
    raw_bytes: bytes | None = None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _schema_path() -> Path:
    source = Path(__file__).resolve().parents[3] / "schemas" / "testbench-plan-v1.schema.json"
    if source.is_file():
        return source
    try:
        installed = distribution("openada")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        suffix = "share/openada/schemas/testbench-plan-v1.schema.json"
        for entry in installed.files or ():
            if entry.as_posix().endswith(suffix):
                candidate = Path(installed.locate_file(entry)).resolve()
                if candidate.is_file():
                    return candidate
    candidate = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "openada"
        / "schemas"
        / "testbench-plan-v1.schema.json"
    )
    if candidate.is_file():
        return candidate
    raise ValueError("the installed testbench-plan schema is unavailable")


def load_testbench_plan_schema() -> dict[str, Any]:
    """Load and meta-validate the published plan schema."""

    try:
        Draft202012Validator, _ = _jsonschema_types()
        body = _schema_path().read_bytes()
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("schema root is not an object")
        Draft202012Validator.check_schema(document)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"the testbench-plan schema is invalid: {exc}") from exc
    return document


def _pointer(parts: Sequence[object]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def _json_depth_within(value: object, limit: int = 64) -> bool:
    pending: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    while pending:
        item, depth, leaving = pending.pop()
        identity = id(item)
        if leaving:
            active.remove(identity)
            continue
        if depth > limit:
            return False
        if isinstance(item, Mapping):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if identity in active:
            return False
        active.add(identity)
        pending.append((item, depth, True))
        pending.extend((child, depth + 1, False) for child in children)
    return True


def _text_depth_within(value: str, limit: int = 64) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                return False
        elif character in "]}":
            depth -= 1
    return True


def validate_testbench_plan(
    path_or_mapping: str | Path | Mapping[str, Any],
    dut_binding: Mapping[str, Any] | None = None,
) -> tuple[PreparedTestbenchPlan | None, list[TestbenchPlanIssue]]:
    """Validate one closed plan without writing files or invoking a simulator."""

    validator = _PlanValidator.from_input(path_or_mapping, dut_binding=dut_binding)
    if isinstance(validator, list):
        return None, validator
    prepared = validator.validate()
    return prepared, validator.issues


class _PlanValidator:
    """Shape and graph checks layered over the published JSON Schema."""

    def __init__(
        self,
        document: Mapping[str, Any],
        *,
        raw_bytes: bytes | None,
        dut_binding: Mapping[str, Any] | None,
    ) -> None:
        self.document = document
        self.raw_bytes = raw_bytes
        self.dut_override = dut_binding
        self.issues: list[TestbenchPlanIssue] = []

    @classmethod
    def from_input(cls, path_or_mapping, *, dut_binding):
        if isinstance(path_or_mapping, Mapping):
            if not _json_depth_within(path_or_mapping):
                return [
                    TestbenchPlanIssue(
                        "testbench_plan.document.over_limit",
                        "",
                        "plan nests deeper than 64 JSON levels",
                    )
                ]
            try:
                canonical = _canonical_bytes(path_or_mapping)
                document = json.loads(canonical.decode("utf-8"))
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                return [TestbenchPlanIssue("testbench_plan.document.invalid", "", str(exc))]
            return cls(document, raw_bytes=None, dut_binding=dut_binding)
        path = Path(path_or_mapping).expanduser().resolve()
        try:
            with stable_regular_file(path) as (handle, opened):
                if not 1 <= opened.st_size <= MAX_PLAN_BYTES:
                    raise ValueError(
                        f"plan size must be within 1..{MAX_PLAN_BYTES} bytes"
                    )
                raw = handle.read(MAX_PLAN_BYTES + 1)
                if len(raw) != opened.st_size:
                    raise ValueError("plan changed while it was read")
            decoded = raw.decode("utf-8")
            if not _text_depth_within(decoded):
                raise ValueError("plan nests deeper than 64 JSON levels")

            def closed_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate JSON object key {key!r}")
                    result[key] = value
                return result

            document = json.loads(
                decoded,
                object_pairs_hook=closed_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value!r} is forbidden")
                ),
            )
        except (
            FileRecordError,
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            return [TestbenchPlanIssue("testbench_plan.document.invalid", "", str(exc))]
        if not isinstance(document, Mapping):
            return [TestbenchPlanIssue("testbench_plan.document.invalid", "", "root must be an object")]
        return cls(dict(document), raw_bytes=raw, dut_binding=dut_binding)

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(TestbenchPlanIssue(code, path, message))

    def validate(self) -> PreparedTestbenchPlan | None:
        try:
            schema = load_testbench_plan_schema()
        except ValueError as exc:
            self.add("testbench_plan.contract.unavailable", "", str(exc))
            return None
        Draft202012Validator, FormatChecker = _jsonschema_types()
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._schema_errors(validator.iter_errors(self.document), prefix="")
        if self.issues:
            return None

        root = self.document
        identifier = str(root["id"])
        dut_document = self._dut_document(schema)
        if dut_document is None:
            return None
        dut = self._validate_dut(dut_document)
        if dut is None:
            return None

        corner_bindings = self._validate_corner_bindings(root["corner_bindings"])
        supplies = self._validate_supplies(root["supplies"], dut, corner_bindings)
        stimuli = self._validate_stimuli(root["stimuli"], dut, supplies)
        probes = self._validate_probes(root["probes"], dut, stimuli)
        (
            stages,
            measurement_index,
            validity_index,
            stage_inputs,
            reduction_index,
            stage_validity_index,
        ) = self._validate_stages(
            root["stages"], dut, stimuli, probes, supplies, corner_bindings
        )
        self._validate_bindings(
            root["bindings"], stages, measurement_index, reduction_index, stage_inputs
        )
        observables = self._validate_observables(
            root["observables"], measurement_index, validity_index,
            reduction_index, stage_validity_index
        )
        self._global_identity_check(
            identifier,
            dut,
            supplies,
            stimuli,
            probes,
            stages,
            root["bindings"],
            observables,
        )
        if self.issues:
            return None

        canonical = _canonical_bytes(root)
        raw_sha = hashlib.sha256(self.raw_bytes or canonical).hexdigest()
        return PreparedTestbenchPlan(
            document=root,
            identifier=identifier,
            dut=dut,
            supplies=tuple(supplies.values()),
            corner_bindings=tuple(corner_bindings.values()),
            stimuli=tuple(stimuli.values()),
            probes=tuple(probes.values()),
            stages=tuple(stages.values()),
            observables=tuple(observables),
            raw_sha256=raw_sha,
            canonical_sha256=hashlib.sha256(canonical).hexdigest(),
            dut_binding_canonical_sha256=hashlib.sha256(
                _canonical_bytes(dut_document)
            ).hexdigest(),
            raw_bytes=self.raw_bytes,
        )

    def _schema_errors(self, errors, *, prefix: str) -> None:
        selected = []
        for error in errors:
            pending = [error]
            unknown = []
            while pending:
                candidate = pending.pop()
                if candidate.validator in {"additionalProperties", "unevaluatedProperties"}:
                    unknown.append(candidate)
                pending.extend(candidate.context)
            selected.extend(unknown or [error])
        seen: set[tuple[str, str, str]] = set()
        for error in sorted(
            selected,
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )[:128]:
            parts = list(error.absolute_path)
            code = "testbench_plan.document.schema_invalid"
            message = error.message
            if error.validator in {"additionalProperties", "unevaluatedProperties"}:
                code = "testbench_plan.document.unknown_field"
                names = re.findall(r"'([^']+)'", message)
                if names:
                    parts.append(names[0])
            elif error.validator == "required":
                names = re.findall(r"'([^']+)'", message)
                if names:
                    parts.append(names[0])
            path = prefix + _pointer(parts)
            identity = (code, path, message)
            if identity not in seen:
                seen.add(identity)
                self.add(code, path, message)
        if len(selected) > 128:
            self.add(
                "testbench_plan.document.over_limit",
                prefix,
                "additional schema errors omitted after 128",
            )

    def _dut_document(self, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
        submitted = self.document["dut"]
        if self.dut_override is None:
            return submitted
        if not isinstance(self.dut_override, Mapping) or not _json_depth_within(
            self.dut_override
        ):
            self.add(
                "testbench_plan.dut.override_invalid",
                "/dut_override",
                "override must be one bounded JSON object",
            )
            return None
        try:
            captured = json.loads(_canonical_bytes(self.dut_override).decode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            self.add("testbench_plan.dut.override_invalid", "/dut_override", str(exc))
            return None
        override_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": schema["$defs"],
            "$ref": "#/$defs/dut",
        }
        Draft202012Validator, _ = _jsonschema_types()
        errors = list(Draft202012Validator(override_schema).iter_errors(captured))
        self._schema_errors(errors, prefix="/dut_override")
        if errors:
            return None
        # A hidden/runtime variant may change only the content locator and
        # digest. The closed namespace and port ABI are part of the submitted
        # plan and cannot be rewritten by an override.
        for field in ("namespace", "top", "ports", "connections", "immutable"):
            if _canonical_bytes(captured[field]) != _canonical_bytes(submitted[field]):
                self.add(
                    "testbench_plan.dut.abi_mismatch",
                    f"/dut_override/{field}",
                    f"runtime DUT override changes sealed field {field!r}",
                )
        return captured if not self.issues else None

    @staticmethod
    def _folded_duplicates(values: Sequence[str]) -> set[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            folded = value.casefold()
            if folded in seen:
                duplicates.add(folded)
            seen.add(folded)
        return duplicates

    def _validate_dut(self, raw: Mapping[str, Any]) -> DutBinding | None:
        artifact = str(raw["artifact"])
        if (
            not artifact.startswith("/")
            or artifact.startswith("~")
            or any(part in {"", ".", ".."} for part in Path(artifact).parts[1:])
            or os.path.normpath(artifact) != artifact
        ):
            self.add(
                "testbench_plan.dut.locator_invalid",
                "/dut/artifact",
                "artifact must be one canonical absolute non-expanding locator",
            )
        namespace = str(raw["namespace"])
        top = str(raw["top"])
        if namespace.casefold() == top.casefold() or namespace.startswith("openada"):
            self.add(
                "testbench_plan.dut.namespace_collision",
                "/dut/namespace",
                "namespace must not shadow the DUT top or OpenADA-owned namespace",
            )
        ports = list(raw["ports"])
        names = [str(port["name"]) for port in ports]
        for folded in self._folded_duplicates(names):
            self.add(
                "testbench_plan.dut.port_duplicate",
                "/dut/ports",
                f"DUT port names collide case-insensitively at {folded!r}",
            )
        internals = [
            str(node) for port in ports for node in port["internal_nodes"]
        ]
        for folded in self._folded_duplicates(internals):
            self.add(
                "testbench_plan.dut.internal_node_duplicate",
                "/dut/ports",
                f"exposed internal nodes collide case-insensitively at {folded!r}",
            )
        collisions = {item.casefold() for item in names} & {
            item.casefold() for item in internals
        }
        for folded in sorted(collisions):
            self.add(
                "testbench_plan.dut.namespace_collision",
                "/dut/ports",
                f"port and internal-node ABI names collide at {folded!r}",
            )
        connections = dict(raw["connections"])
        missing = set(names) - set(connections)
        unknown = set(connections) - set(names)
        for name in sorted(missing):
            self.add(
                "testbench_plan.dut.port_unbound",
                f"/dut/connections/{name}",
                f"DUT port {name!r} has no external connection",
            )
        for name in sorted(unknown):
            self.add(
                "testbench_plan.dut.port_unknown",
                f"/dut/connections/{name}",
                f"{name!r} is not in the sealed port ABI",
            )
        return DutBinding(
            artifact=artifact,
            sha256=str(raw["sha256"]),
            namespace=namespace,
            top=top,
            ports=tuple(dict(port) for port in ports),
            connections=connections,
        )

    def _validate_corner_bindings(
        self, values: Sequence[Mapping[str, Any]]
    ) -> dict[str, Mapping[str, Any]]:
        output: dict[str, Mapping[str, Any]] = {}
        value_abi: dict[str, str] | None = None
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"/corner_bindings/{index}"
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate corner id"
                )
                continue
            entries: dict[str, str] = {}
            for value_index, entry in enumerate(raw["values"]):
                value_id = str(entry["id"])
                if value_id in entries:
                    self.add(
                        "testbench_plan.id.duplicate",
                        f"{path}/values/{value_index}/id",
                        "duplicate corner value id",
                    )
                entries[value_id] = str(entry["value"]["unit"])
            if value_abi is None:
                value_abi = entries
            elif value_abi != entries:
                self.add(
                    "testbench_plan.corner.abi_mismatch",
                    f"{path}/values",
                    "every corner must expose the same typed value ABI",
                )
            output[identifier] = dict(raw)
        return output

    def _validate_supplies(
        self,
        values: Sequence[Mapping[str, Any]],
        dut: DutBinding,
        corners: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Supply]:
        output: dict[str, Supply] = {}
        known_nets = set(dut.connections.values()) | {"0"}
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"/supplies/{index}"
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate supply id"
                )
                continue
            positive = str(raw["positive"])
            negative = str(raw["negative"])
            if positive.casefold() == negative.casefold():
                self.add(
                    "testbench_plan.supply.terminal_short",
                    path,
                    "positive and negative terminals must differ",
                )
            for name, net in (("positive", positive), ("negative", negative)):
                if net not in known_nets:
                    self.add(
                        "testbench_plan.supply.net_unknown",
                        f"{path}/{name}",
                        f"net {net!r} is outside the DUT connection namespace",
                    )
            voltage_id = str(raw["voltage"]["value_id"])
            for corner_id, corner in corners.items():
                values_by_id = {
                    str(item["id"]): item["value"] for item in corner["values"]
                }
                value = values_by_id.get(voltage_id)
                if value is None:
                    self.add(
                        "testbench_plan.supply.corner_value_unknown",
                        f"{path}/voltage/value_id",
                        f"corner {corner_id!r} does not expose {voltage_id!r}",
                    )
                elif value["unit"] != "V" or value["value"] <= 0:
                    self.add(
                        "testbench_plan.supply.voltage_invalid",
                        f"/corner_bindings/{corner_id}/values/{voltage_id}",
                        "supply corner value must be a positive voltage",
                    )
            output[identifier] = Supply(
                identifier, positive, negative, dict(raw["voltage"])
            )
        return output

    def _validate_stimuli(
        self,
        values: Sequence[Mapping[str, Any]],
        dut: DutBinding,
        supplies: Mapping[str, Supply],
    ) -> dict[str, Stimulus]:
        output: dict[str, Stimulus] = {}
        ports = {str(port["name"]): port for port in dut.ports}
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"/stimuli/{index}"
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate stimulus id"
                )
                continue
            kind = str(raw["kind"])
            supply_id = str(raw["supply_id"]) if "supply_id" in raw else None
            if supply_id is not None and supply_id not in supplies:
                self.add(
                    "testbench_plan.stimulus.supply_unknown",
                    f"{path}/supply_id",
                    f"unknown supply {supply_id!r}",
                )
            if kind in {"dc_state", "pulse_train", "small_signal_ac"}:
                port_fields = ("target_port",)
            else:
                port_fields = ("reference_port", "offset_port")
            for field in port_fields:
                port_name = str(raw[field])
                port = ports.get(port_name)
                if port is None:
                    self.add(
                        "testbench_plan.stimulus.port_unknown",
                        f"{path}/{field}",
                        f"unknown DUT port {port_name!r}",
                    )
                elif kind == "small_signal_ac":
                    if port["direction"] not in {"input", "output", "inout"}:
                        self.add(
                            "testbench_plan.stimulus.port_incompatible",
                            f"{path}/{field}",
                            "AC current injection must name a signal DUT port",
                        )
                elif kind == "dc_state" and raw["state"] == "bias":
                    if port["direction"] not in {"output", "inout", "reference"}:
                        self.add(
                            "testbench_plan.stimulus.port_incompatible",
                            f"{path}/{field}",
                            "bias state must drive an output, inout, or reference port",
                        )
                elif port["direction"] not in {"input", "inout"}:
                    self.add(
                        "testbench_plan.stimulus.port_incompatible",
                        f"{path}/{field}",
                        "stimulus target must be an input or inout DUT port",
                    )
            if kind == "small_signal_ac":
                reference_name = str(raw["reference_port"])
                reference = ports.get(reference_name)
                if reference is None:
                    self.add(
                        "testbench_plan.stimulus.port_unknown",
                        f"{path}/reference_port",
                        f"unknown DUT reference port {reference_name!r}",
                    )
                elif reference["direction"] not in {"reference", "supply"}:
                    self.add(
                        "testbench_plan.stimulus.port_incompatible",
                        f"{path}/reference_port",
                        "AC injection reference must be a DUT reference or supply port",
                    )
                if reference_name.casefold() == str(raw["target_port"]).casefold():
                    self.add(
                        "testbench_plan.stimulus.port_incompatible",
                        path,
                        "AC injection target and reference must be distinct",
                    )
                output[identifier] = Stimulus(identifier, kind, dict(raw))
                continue
            if kind == "phase_offset_pair" and str(raw["reference_port"]).casefold() == str(raw["offset_port"]).casefold():
                self.add(
                    "testbench_plan.stimulus.port_incompatible",
                    path,
                    "phase-offset pair requires two distinct DUT ports",
                )
            level_names = (
                ("level",)
                if kind == "dc_state"
                else ("low_level", "high_level")
            )
            for field in level_names:
                if raw[field]["supply_id"] != supply_id:
                    self.add(
                        "testbench_plan.stimulus.supply_conflict",
                        f"{path}/{field}/supply_id",
                        "supply-scaled level must use the stimulus supply",
                    )
            if kind == "dc_state":
                if raw["state"] != "bias":
                    fraction = float(raw["level"]["fraction"])
                    if (raw["state"] == "low" and fraction > 0.5) or (
                        raw["state"] == "high" and fraction <= 0.5
                    ):
                        self.add(
                            "testbench_plan.stimulus.level_inconsistent",
                            f"{path}/level/fraction",
                            "declared logic state conflicts with its supply-scaled level",
                        )
            else:
                low = float(raw["low_level"]["fraction"])
                high = float(raw["high_level"]["fraction"])
                if high <= low:
                    self.add(
                        "testbench_plan.stimulus.level_inconsistent",
                        path,
                        "high_level fraction must exceed low_level fraction",
                    )
                for field in ("delay", "rise_time", "fall_time"):
                    if float(raw[field]["value"]) < 0:
                        self.add(
                            "testbench_plan.stimulus.timing_invalid",
                            f"{path}/{field}/value",
                            "must be non-negative",
                        )
                for field in ("pulse_width", "period"):
                    if float(raw[field]["value"]) <= 0:
                        self.add(
                            "testbench_plan.stimulus.timing_invalid",
                            f"{path}/{field}/value",
                            "must be greater than zero",
                        )
                occupied = sum(
                    float(raw[name]["value"])
                    for name in ("rise_time", "pulse_width", "fall_time")
                )
                if occupied > float(raw["period"]["value"]):
                    self.add(
                        "testbench_plan.stimulus.timing_invalid",
                        f"{path}/period/value",
                        "period must cover rise, pulse width, and fall times",
                    )
            output[identifier] = Stimulus(identifier, kind, dict(raw))
        return output

    def _validate_probes(
        self,
        values: Sequence[Mapping[str, Any]],
        dut: DutBinding,
        stimuli: Mapping[str, Stimulus],
    ) -> dict[str, Probe]:
        output: dict[str, Probe] = {}
        ports = {str(port["name"]): port for port in dut.ports}
        internals = {
            str(node) for port in dut.ports for node in port["internal_nodes"]
        }
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"/probes/{index}"
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate probe id"
                )
                continue
            kind = str(raw["kind"])
            if kind in {"dut_port_voltage", "dut_port_current"}:
                port = str(raw["port"])
                if port not in ports:
                    self.add(
                        "testbench_plan.probe.port_unknown",
                        f"{path}/port",
                        f"unknown DUT port {port!r}",
                    )
            if kind in {"dut_port_voltage", "dut_internal_node"}:
                reference = str(raw["reference_port"])
                if reference not in ports:
                    self.add(
                        "testbench_plan.probe.port_unknown",
                        f"{path}/reference_port",
                        f"unknown reference port {reference!r}",
                    )
            if kind == "dut_internal_node" and str(raw["node"]) not in internals:
                self.add(
                    "testbench_plan.probe.internal_node_unexposed",
                    f"{path}/node",
                    "DUT-internal node is absent from the sealed probe ABI",
                )
            if kind == "stimulus_branch_current":
                stimulus = stimuli.get(str(raw["stimulus_id"]))
                if stimulus is None:
                    self.add(
                        "testbench_plan.probe.stimulus_unknown",
                        f"{path}/stimulus_id",
                        f"unknown stimulus {raw['stimulus_id']!r}",
                    )
                else:
                    branch = str(raw["branch"])
                    expected = (
                        {"reference", "offset"}
                        if stimulus.kind == "phase_offset_pair"
                        else {"single"}
                    )
                    if branch not in expected:
                        self.add(
                            "testbench_plan.probe.branch_incompatible",
                            f"{path}/branch",
                            "phase-pair sources require an exact reference or "
                            "offset branch; single-output sources require 'single'",
                        )
            if kind == "pll_loop_gain":
                stimulus = stimuli.get(str(raw["injection_stimulus_id"]))
                if stimulus is None:
                    self.add(
                        "testbench_plan.probe.stimulus_unknown",
                        f"{path}/injection_stimulus_id",
                        "PLL loop gain names an unknown AC injection",
                    )
                elif stimulus.kind != "small_signal_ac":
                    self.add(
                        "testbench_plan.probe.stimulus_incompatible",
                        f"{path}/injection_stimulus_id",
                        "PLL loop gain requires a small_signal_ac current injection",
                    )
                response = output.get(str(raw["response_probe_id"]))
                if response is None:
                    self.add(
                        "testbench_plan.probe.response_unknown",
                        f"{path}/response_probe_id",
                        "loop response must name an earlier physical voltage probe",
                    )
                elif response.kind not in {"dut_port_voltage", "dut_internal_node"}:
                    self.add(
                        "testbench_plan.probe.response_incompatible",
                        f"{path}/response_probe_id",
                        "loop response must be a physical DUT voltage identity",
                    )
            output[identifier] = Probe(
                identifier, kind, str(raw["unit"]), dict(raw)
            )
        return output

    @staticmethod
    def _quantity_unit(value: object) -> str | None:
        return str(value.get("unit")) if isinstance(value, Mapping) and isinstance(value.get("unit"), str) else None

    @staticmethod
    def _point_key(stage_id: str, point_id: str) -> tuple[str, str]:
        return stage_id, point_id

    def _validate_stages(
        self,
        values: Sequence[Mapping[str, Any]],
        dut: DutBinding,
        stimuli: Mapping[str, Stimulus],
        probes: Mapping[str, Probe],
        supplies: Mapping[str, Supply],
        corners: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        dict[str, Stage],
        dict[tuple[str, str, str], Measurement],
        dict[tuple[str, str, str], Mapping[str, Any]],
        dict[tuple[str, str], str],
        dict[tuple[str, str], Reduction],
        dict[tuple[str, str], Mapping[str, Any]],
    ]:
        stage_raw: dict[str, Mapping[str, Any]] = {}
        stage_positions: dict[str, int] = {}
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            if identifier in stage_raw:
                self.add(
                    "testbench_plan.id.duplicate",
                    f"/stages/{index}/id",
                    "duplicate stage id",
                )
            else:
                stage_raw[identifier] = raw
                stage_positions[identifier] = index
        for identifier, raw in stage_raw.items():
            for dep_index, dependency in enumerate(raw["depends_on"]):
                if dependency not in stage_raw:
                    self.add(
                        "testbench_plan.stage.dependency_unknown",
                        f"/stages/{stage_positions[identifier]}/depends_on/{dep_index}",
                        f"unknown stage {dependency!r}",
                    )
                if dependency == identifier:
                    self.add(
                        "testbench_plan.graph.cycle",
                        f"/stages/{stage_positions[identifier]}/depends_on/{dep_index}",
                        "stage cannot depend on itself",
                    )
        self._check_dag(
            {identifier: tuple(str(item) for item in raw["depends_on"]) for identifier, raw in stage_raw.items()},
            path="/stages",
        )

        output: dict[str, Stage] = {}
        measurements: dict[tuple[str, str, str], Measurement] = {}
        validity: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        stage_inputs: dict[tuple[str, str], str] = {}
        reductions: dict[tuple[str, str], Reduction] = {}
        stage_validity: dict[tuple[str, str], Mapping[str, Any]] = {}
        point_documents: dict[tuple[str, str], Mapping[str, Any]] = {}
        point_indices: dict[tuple[str, str], int] = {}
        exposed_internal = {
            str(node) for port in dut.ports for node in port["internal_nodes"]
        }
        ports = {str(port["name"]) for port in dut.ports}
        for stage_id, raw in stage_raw.items():
            stage_index = stage_positions[stage_id]
            input_ids: set[str] = set()
            for input_index, entry in enumerate(raw["inputs"]):
                input_id = str(entry["id"])
                if input_id in input_ids:
                    self.add(
                        "testbench_plan.id.duplicate",
                        f"/stages/{stage_index}/inputs/{input_index}/id",
                        "duplicate stage input id",
                    )
                input_ids.add(input_id)
                stage_inputs[(stage_id, input_id)] = str(entry["unit"])

            normalized_points: list[ConditionPoint] = []
            point_ids: set[str] = set()
            for point_index, point in enumerate(raw["points"]):
                point_id = str(point["id"])
                point_path = f"/stages/{stage_index}/points/{point_index}"
                if point_id in point_ids:
                    self.add(
                        "testbench_plan.id.duplicate",
                        f"{point_path}/id",
                        "duplicate point id within stage",
                    )
                point_ids.add(point_id)
                point_documents[(stage_id, point_id)] = point
                point_indices[(stage_id, point_id)] = point_index
                parameter_names: set[str] = set()
                corner_id = str(point["condition"]["corner"])
                corner = corners.get(corner_id)
                if corner is None:
                    self.add(
                        "testbench_plan.condition.corner_unknown",
                        f"{point_path}/condition/corner",
                        f"unknown corner binding {corner_id!r}",
                    )
                elif point["condition"]["temperature"] != corner["temperature"]:
                    self.add(
                        "testbench_plan.condition.temperature_mismatch",
                        f"{point_path}/condition/temperature",
                        "condition temperature must equal its sealed corner binding",
                    )
                for parameter_index, parameter in enumerate(point["condition"]["parameters"]):
                    name = str(parameter["name"])
                    if name in parameter_names:
                        self.add(
                            "testbench_plan.condition.parameter_duplicate",
                            f"{point_path}/condition/parameters/{parameter_index}/name",
                            f"duplicate condition parameter {name!r}",
                        )
                    parameter_names.add(name)
                    self._check_value_source(
                        parameter["value"],
                        stage_id,
                        stage_inputs,
                        f"{point_path}/condition/parameters/{parameter_index}/value",
                    )
                self._validate_state_policy(
                    point["state_policy"],
                    stage_id,
                    point_id,
                    raw,
                    point_documents,
                    ports,
                    exposed_internal,
                    point_path,
                )
                self._validate_settle_policy(
                    point["settle_policy"], point["analysis"], probes, point_path
                )
                self._validate_analysis(
                    point["analysis"],
                    stage_id,
                    stage_inputs,
                    stimuli,
                    point_path,
                )
                self._validate_active_stimuli(
                    point["active_stimulus_ids"], point["analysis"], stimuli,
                    supplies, stage_id, stage_inputs, point_path
                )
                point_measurements = self._validate_measurements(
                    point["measurements"],
                    stage_id,
                    point_id,
                    stage_inputs,
                    probes,
                    stimuli,
                    supplies,
                    point["analysis"],
                    point["condition"],
                    point_path,
                )
                for item in point_measurements:
                    measurements[(stage_id, point_id, item.identifier)] = item
                point_validity = self._validate_validity(
                    point["validity_rules"],
                    point_measurements,
                    stimuli,
                    supplies,
                    point_path,
                )
                for rule_id, rule in point_validity.items():
                    validity[(stage_id, point_id, rule_id)] = rule
                normalized_points.append(
                    ConditionPoint(
                        point_id,
                        dict(point),
                        tuple(point_measurements),
                    )
                )
            stage_reductions = self._validate_reductions(
                raw["reductions"], stage_id, raw["points"], measurements,
                supplies, f"/stages/{stage_index}"
            )
            reductions.update({(stage_id, item.identifier): item for item in stage_reductions})
            current_stage_validity = self._validate_stage_validity(
                raw["validity_rules"], stage_reductions, supplies,
                f"/stages/{stage_index}"
            )
            stage_validity.update({(stage_id, key): item for key, item in current_stage_validity.items()})
            output[stage_id] = Stage(
                stage_id,
                tuple(str(item) for item in raw["depends_on"]),
                tuple(normalized_points),
                tuple(stage_reductions),
            )

        # Carryover references can point forward in the document only if their
        # dependency edge is explicit; then the stage DAG still decides order.
        for (stage_id, point_id), point in point_documents.items():
            state = point["state_policy"]
            if state["kind"] != "carryover":
                continue
            source = state["from"]
            source_key = (str(source["stage_id"]), str(source["point_id"]))
            stage_index = stage_positions[stage_id]
            point_index = point_indices[(stage_id, point_id)]
            if source_key not in point_documents:
                self.add(
                    "testbench_plan.state.source_unknown",
                    f"/stages/{stage_index}/points/{point_index}/state_policy/from",
                    "carryover source point does not exist",
                )
            elif source_key[0] == stage_id:
                source_position = point_indices[source_key]
                if source_position >= point_index:
                    self.add(
                        "testbench_plan.graph.cycle",
                        f"/stages/{stage_index}/points/{point_index}/state_policy/from",
                        "same-stage carryover must name an earlier point",
                    )
            elif source_key[0] not in stage_raw[stage_id]["depends_on"]:
                self.add(
                    "testbench_plan.state.dependency_missing",
                    f"/stages/{stage_index}/points/{point_index}/state_policy/from",
                    "cross-stage carryover requires an explicit stage dependency",
                )
        return output, measurements, validity, stage_inputs, reductions, stage_validity

    def _check_dag(self, graph: Mapping[str, Sequence[str]], *, path: str) -> None:
        active: set[str] = set()
        complete: set[str] = set()

        def visit(node: str, trail: tuple[str, ...]) -> None:
            if node in complete:
                return
            if node in active:
                self.add(
                    "testbench_plan.graph.cycle",
                    path,
                    "dependency cycle: " + " -> ".join((*trail, node)),
                )
                return
            active.add(node)
            for parent in graph.get(node, ()):
                if parent in graph:
                    visit(parent, (*trail, node))
            active.remove(node)
            complete.add(node)

        for node in graph:
            visit(node, ())

    def _check_value_source(
        self,
        value: Mapping[str, Any],
        stage_id: str,
        stage_inputs: Mapping[tuple[str, str], str],
        path: str,
        expected_unit: str | None = None,
    ) -> str | None:
        unit = str(value["unit"])
        if "input_id" in value:
            actual = stage_inputs.get((stage_id, str(value["input_id"])))
            if actual is None:
                self.add(
                    "testbench_plan.binding.input_unknown",
                    f"{path}/input_id",
                    f"unknown input {value['input_id']!r} in stage {stage_id!r}",
                )
            elif actual != unit:
                self.add(
                    "testbench_plan.unit.mismatch",
                    f"{path}/unit",
                    f"declared input unit {unit!r} differs from {actual!r}",
                )
            if "offset" in value and value["offset"]["unit"] != unit:
                self.add(
                    "testbench_plan.unit.mismatch",
                    f"{path}/offset/unit",
                    "affine offset must use the value-source output unit",
                )
        if expected_unit is not None and unit != expected_unit:
            self.add(
                "testbench_plan.unit.mismatch",
                f"{path}/unit",
                f"expected {expected_unit!r}, got {unit!r}",
            )
        return unit

    @staticmethod
    def _threshold_unit(
        threshold: Mapping[str, Any], supplies: Mapping[str, Supply]
    ) -> str | None:
        if threshold.get("kind") == "supply_scaled":
            return "V" if threshold.get("supply_id") in supplies else None
        unit = threshold.get("unit")
        return str(unit) if isinstance(unit, str) else None

    def _validate_state_policy(
        self,
        state: Mapping[str, Any],
        stage_id: str,
        point_id: str,
        stage: Mapping[str, Any],
        points: Mapping[tuple[str, str], Mapping[str, Any]],
        ports: set[str],
        internals: set[str],
        point_path: str,
    ) -> None:
        del stage_id, point_id, stage, points
        if state["kind"] != "fresh":
            return
        identities: set[tuple[str, str]] = set()
        for index, initial in enumerate(state["initial_node_voltages"]):
            field = "port" if initial["kind"] == "port" else "node"
            name = str(initial[field])
            valid = ports if initial["kind"] == "port" else internals
            identity = (str(initial["kind"]), name.casefold())
            if identity in identities:
                self.add(
                    "testbench_plan.state.initial_duplicate",
                    f"{point_path}/state_policy/initial_node_voltages/{index}",
                    "duplicate initial-voltage identity",
                )
            identities.add(identity)
            if name not in valid:
                self.add(
                    "testbench_plan.state.node_unknown",
                    f"{point_path}/state_policy/initial_node_voltages/{index}/{field}",
                    "initial-voltage target is absent from the sealed DUT ABI",
                )

    def _validate_settle_policy(
        self,
        settle: Mapping[str, Any],
        analysis: Mapping[str, Any],
        probes: Mapping[str, Probe],
        point_path: str,
    ) -> None:
        path = f"{point_path}/settle_policy"
        analysis_kind = str(analysis["kind"])
        if settle["kind"] == "operating_point":
            if analysis_kind != "dc_sweep":
                self.add(
                    "testbench_plan.settle.analysis_incompatible",
                    path,
                    "operating_point settling is available only for a DC sweep",
                )
            return
        if settle["kind"] == "fixed_time":
            if analysis_kind == "dc_sweep":
                self.add(
                    "testbench_plan.settle.analysis_incompatible",
                    path,
                    "a DC sweep must declare operating_point settling; fixed_time implies elapsed transient time",
                )
            if float(settle["duration"]["value"]) < 0:
                self.add(
                    "testbench_plan.settle.invalid",
                    f"{path}/duration/value",
                    "fixed settle duration must be non-negative",
                )
            return
        probe = probes.get(str(settle["probe_id"]))
        if probe is None:
            self.add(
                "testbench_plan.settle.probe_unknown",
                f"{path}/probe_id",
                f"unknown probe {settle['probe_id']!r}",
            )
        elif settle["tolerance"]["unit"] != probe.unit:
            self.add(
                "testbench_plan.unit.mismatch",
                f"{path}/tolerance/unit",
                f"settling tolerance must use probe unit {probe.unit!r}",
            )
        for field in ("tolerance", "hold_for", "maximum"):
            if float(settle[field]["value"]) <= 0:
                self.add(
                    "testbench_plan.settle.invalid",
                    f"{path}/{field}/value",
                    "must be greater than zero",
                )
        if float(settle["hold_for"]["value"]) > float(settle["maximum"]["value"]):
            self.add(
                "testbench_plan.settle.invalid",
                path,
                "hold_for must not exceed maximum",
            )

    def _validate_analysis(
        self,
        analysis: Mapping[str, Any],
        stage_id: str,
        stage_inputs: Mapping[tuple[str, str], str],
        stimuli: Mapping[str, Stimulus],
        point_path: str,
    ) -> None:
        path = f"{point_path}/analysis"
        kind = str(analysis["kind"])
        stimulus_key = (
            "source_stimulus_id"
            if kind == "dc_sweep"
            else "stimulus_id"
        )
        stimulus = stimuli.get(str(analysis[stimulus_key]))
        expected_kind = {
            "dc_sweep": "dc_state",
            "pulse_train_transient": "pulse_train",
            "phase_offset_pair_transient": "phase_offset_pair",
            "linear_ac": "small_signal_ac",
        }[kind]
        if stimulus is None:
            self.add(
                "testbench_plan.analysis.stimulus_unknown",
                f"{path}/{stimulus_key}",
                f"unknown stimulus {analysis[stimulus_key]!r}",
            )
        elif stimulus.kind != expected_kind:
            self.add(
                "testbench_plan.analysis.stimulus_incompatible",
                f"{path}/{stimulus_key}",
                f"analysis requires {expected_kind!r}, got {stimulus.kind!r}",
            )
        fields = (
            ("start", "stop", "step")
            if kind == "dc_sweep"
            else ("start", "stop")
            if kind == "linear_ac"
            else ("step", "stop")
        )
        expected_unit = "Hz" if kind == "linear_ac" else None
        units = [
            self._check_value_source(
                analysis[field], stage_id, stage_inputs, f"{path}/{field}",
                expected_unit=expected_unit,
            )
            for field in fields
        ]
        if len(set(units)) != 1:
            self.add(
                "testbench_plan.unit.mismatch",
                path,
                "analysis bounds and step must use one exact unit",
            )
        literal = all("value" in analysis[field] for field in fields)
        if literal:
            values = [float(analysis[field]["value"]) for field in fields]
            if kind == "dc_sweep":
                if values[1] <= values[0] or values[2] <= 0:
                    self.add(
                        "testbench_plan.analysis.range_invalid",
                        path,
                        "DC stop must exceed start and step must be positive",
                    )
            elif kind == "linear_ac":
                if values[0] <= 0 or values[1] <= values[0]:
                    self.add(
                        "testbench_plan.analysis.range_invalid",
                        path,
                        "AC start must be positive and stop must exceed start",
                    )
            elif values[0] <= 0 or values[1] <= 0:
                self.add(
                    "testbench_plan.analysis.range_invalid",
                    path,
                    "transient step and stop must be positive",
                )

    def _validate_active_stimuli(
        self,
        active_ids: Sequence[str],
        analysis: Mapping[str, Any],
        stimuli: Mapping[str, Stimulus],
        supplies: Mapping[str, Supply],
        stage_id: str,
        stage_inputs: Mapping[tuple[str, str], str],
        point_path: str,
    ) -> None:
        path = f"{point_path}/active_stimulus_ids"
        analysis_id = str(
            analysis.get("source_stimulus_id", analysis.get("stimulus_id", ""))
        )
        if analysis_id not in active_ids:
            self.add(
                "testbench_plan.stimulus.analysis_inactive",
                path,
                "the selected analysis stimulus must be active at this point",
            )
        targets: dict[str, str] = {}
        for index, identifier in enumerate(active_ids):
            stimulus = stimuli.get(str(identifier))
            if stimulus is None:
                self.add(
                    "testbench_plan.stimulus.active_unknown",
                    f"{path}/{index}",
                    f"unknown active stimulus {identifier!r}",
                )
                continue
            raw = stimulus.document
            target_names = (
                (str(raw["target_port"]),)
                if stimulus.kind in {"dc_state", "pulse_train", "small_signal_ac"}
                else (str(raw["reference_port"]), str(raw["offset_port"]))
            )
            for target in target_names:
                if target in targets:
                    self.add(
                        "testbench_plan.stimulus.target_conflict",
                        f"{path}/{index}",
                        f"active stimuli {targets[target]!r} and {identifier!r} both drive {target!r}",
                    )
                else:
                    targets[target] = str(identifier)
            if stimulus.kind == "phase_offset_pair":
                self._check_value_source(
                    raw["phase_offset"], stage_id, stage_inputs,
                    f"/stimuli/{identifier}/phase_offset", expected_unit="s"
                )
            if stimulus.kind == "small_signal_ac":
                continue
            for level_name in (
                ("level",) if stimulus.kind == "dc_state" else ("low_level", "high_level")
            ):
                if str(raw[level_name]["supply_id"]) not in supplies:
                    self.add(
                        "testbench_plan.stimulus.supply_unknown",
                        f"/stimuli/{identifier}/{level_name}/supply_id",
                        "active stimulus uses an unknown supply",
                    )

    def _validate_measurements(
        self,
        values: Sequence[Mapping[str, Any]],
        stage_id: str,
        point_id: str,
        stage_inputs: Mapping[tuple[str, str], str],
        probes: Mapping[str, Probe],
        stimuli: Mapping[str, Stimulus],
        supplies: Mapping[str, Supply],
        analysis: Mapping[str, Any],
        condition: Mapping[str, Any],
        point_path: str,
    ) -> list[Measurement]:
        del point_id
        output: list[Measurement] = []
        by_id: dict[str, Measurement] = {}
        dependency_graph: dict[str, tuple[str, ...]] = {}
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"{point_path}/measurements/{index}"
            if identifier in by_id:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate measurement id"
                )
                continue
            kind = str(raw["kind"])
            unit = str(raw["unit"])
            parents: list[str] = []
            for name in (
                "input_measurement_id",
                "actual_measurement_id",
                "reference_measurement_id",
                "unity_frequency_measurement_id",
            ):
                if name in raw:
                    parents.append(str(raw[name]))
            dependency_graph[identifier] = tuple(parents)
            parent_items = [by_id.get(parent) for parent in parents]
            for field, parent, item in zip(
                [name for name in (
                    "input_measurement_id",
                    "actual_measurement_id",
                    "reference_measurement_id",
                    "unity_frequency_measurement_id",
                ) if name in raw],
                parents,
                parent_items,
            ):
                if item is None:
                    self.add(
                        "testbench_plan.measurement.parent_unknown",
                        f"{path}/{field}",
                        f"parent {parent!r} must be an earlier measurement in this point",
                    )
            if "probe_id" in raw:
                probe = probes.get(str(raw["probe_id"]))
                if probe is None:
                    self.add(
                        "testbench_plan.measurement.probe_unknown",
                        f"{path}/probe_id",
                        f"unknown probe {raw['probe_id']!r}",
                    )
                elif kind == "curve" and unit != probe.unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/unit",
                        f"curve unit must equal probe unit {probe.unit!r}",
                    )
                elif kind == "integrate":
                    if probe.unit != "A":
                        self.add(
                            "testbench_plan.measurement.probe_incompatible",
                            f"{path}/probe_id",
                            "charge integration requires a current probe",
                        )
                    quantity = str(raw["quantity"])
                    if quantity == "delivered_charge" and probe.kind != "dut_port_current":
                        self.add(
                            "testbench_plan.measurement.probe_cheat",
                            f"{path}/probe_id",
                            "delivered_charge must probe a DUT port current, never a command source",
                        )
                    if quantity == "source_charge" and probe.kind != "stimulus_branch_current":
                        self.add(
                            "testbench_plan.measurement.probe_incompatible",
                            f"{path}/probe_id",
                            "source_charge requires an explicitly identified stimulus branch",
                        )
                elif kind == "loop_transfer":
                    if analysis["kind"] != "linear_ac":
                        self.add(
                            "testbench_plan.measurement.analysis_incompatible",
                            path,
                            "loop_transfer requires a linear_ac analysis",
                        )
                    if probe.kind != "pll_loop_gain":
                        self.add(
                            "testbench_plan.measurement.probe_incompatible",
                            f"{path}/probe_id",
                            "loop_transfer requires an explicit pll_loop_gain probe",
                        )
                    else:
                        if (
                            probe.document["injection_stimulus_id"]
                            != analysis["stimulus_id"]
                        ):
                            self.add(
                                "testbench_plan.measurement.probe_incompatible",
                                f"{path}/probe_id",
                                "loop-gain probe injection must equal the active AC analysis source",
                            )
                        construction = probe.document["construction"]
                        factors = (
                            ("charge_pump_gain", construction["charge_pump_gain"], "A"),
                            ("vco_gain", construction["vco_gain"], "Hz/V"),
                            ("divider_ratio", construction["divider_ratio"], "1"),
                            ("loop_filter/r1", construction["loop_filter"]["r1"], "Ohm"),
                            ("loop_filter/c1", construction["loop_filter"]["c1"], "F"),
                            ("loop_filter/r2", construction["loop_filter"]["r2"], "Ohm"),
                            ("loop_filter/c2", construction["loop_filter"]["c2"], "F"),
                        )
                        for field, value, expected_unit in factors:
                            self._check_value_source(
                                value,
                                stage_id,
                                stage_inputs,
                                f"/probes/{probe.identifier}/construction/{field}",
                                expected_unit=expected_unit,
                            )
                            if "value" in value and float(value["value"]) <= 0:
                                self.add(
                                    "testbench_plan.probe.factor_invalid",
                                    f"/probes/{probe.identifier}/construction/{field}/value",
                                    "PLL loop construction factors must be positive",
                                )
            if "window" in raw:
                for field in ("start", "stop"):
                    self._check_value_source(
                        raw["window"][field],
                        stage_id,
                        stage_inputs,
                        f"{path}/window/{field}",
                    )
                if raw["window"]["start"]["unit"] != raw["window"]["stop"]["unit"]:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/window",
                        "measurement window endpoints must use one exact unit",
                    )
            if kind == "integrate" and raw["normalization"]["kind"] == "pulse_count":
                stimulus = stimuli.get(str(raw["normalization"]["stimulus_id"]))
                if stimulus is None or stimulus.kind not in {"pulse_train", "phase_offset_pair"}:
                    self.add(
                        "testbench_plan.measurement.normalization_invalid",
                        f"{path}/normalization/stimulus_id",
                        "pulse-count normalization requires a declared pulsed stimulus",
                    )
                else:
                    count = int(raw["normalization"]["count"])
                    if count > int(stimulus.document["count"]):
                        self.add(
                            "testbench_plan.measurement.normalization_invalid",
                            f"{path}/normalization/count",
                            "normalization count exceeds the emitted stimulus pulse count",
                        )
                    window = raw["window"]
                    if "value" in window["start"] and "value" in window["stop"]:
                        duration = float(window["stop"]["value"]) - float(window["start"]["value"])
                        expected = count * float(stimulus.document["period"]["value"])
                        tolerance = max(abs(expected) * 1e-12, 1e-30)
                        if abs(duration - expected) > tolerance:
                            self.add(
                                "testbench_plan.measurement.normalization_invalid",
                                f"{path}/window",
                                "literal integration window duration must equal count times period",
                            )
            if kind == "linear_fit" and parent_items and parent_items[0] is not None:
                parent = parent_items[0]
                axis_unit = self._point_curve_axis_unit(
                    parent, analysis=analysis, condition=condition
                )
                expected = (
                    self._divide_units(parent.unit, axis_unit)
                    if axis_unit is not None
                    else None
                )
                if unit != expected:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/unit",
                        f"linear-fit slope unit must be {expected!r}",
                    )
            elif kind == "max_abs" and parent_items and parent_items[0] is not None:
                if unit != parent_items[0].unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/unit",
                        f"derived unit must equal parent unit {parent_items[0].unit!r}",
                    )
            elif kind == "crossing" and parent_items and parent_items[0] is not None:
                axis_unit = self._point_curve_axis_unit(
                    parent_items[0], analysis=analysis, condition=condition
                )
                threshold_unit = self._threshold_unit(raw["threshold"], supplies)
                if threshold_unit is None:
                    self.add(
                        "testbench_plan.measurement.supply_unknown",
                        f"{path}/threshold/supply_id",
                        "supply-scaled threshold names an unknown supply",
                    )
                elif threshold_unit != parent_items[0].unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/threshold/unit",
                        "crossing threshold must match input curve unit",
                    )
                if axis_unit is not None and unit != axis_unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/unit",
                        f"crossing output must use curve axis unit {axis_unit!r}",
                    )
            elif kind == "sign" and parent_items and parent_items[0] is not None:
                if raw["zero_tolerance"]["unit"] != parent_items[0].unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/zero_tolerance/unit",
                        "sign tolerance must match input unit",
                    )
            elif kind == "unity_frequency" and parent_items and parent_items[0] is not None:
                if parent_items[0].kind != "loop_transfer" or unit != "Hz":
                    self.add(
                        "testbench_plan.measurement.parent_incompatible",
                        path,
                        "unity_frequency requires a loop_transfer parent and emits Hz",
                    )
            elif (
                kind == "negative_feedback_phase_margin"
                and len(parent_items) == 2
                and all(parent_items)
            ):
                loop_transfer, unity = parent_items
                same_loop = (
                    unity.kind == "unity_frequency"
                    and unity.document["input_measurement_id"]
                    == loop_transfer.identifier
                )
                probe = probes.get(str(loop_transfer.document.get("probe_id", "")))
                if (
                    loop_transfer.kind != "loop_transfer"
                    or not same_loop
                    or probe is None
                    or probe.kind != "pll_loop_gain"
                    or probe.document.get("feedback") != "negative"
                    or unit != "deg"
                ):
                    self.add(
                        "testbench_plan.measurement.parent_incompatible",
                        path,
                        "phase margin requires the matching negative-feedback loop transfer and unity frequency",
                    )
            elif kind == "mismatch_fraction" and len(parent_items) == 2 and all(parent_items):
                if parent_items[0].unit != parent_items[1].unit or raw["floor"]["unit"] != parent_items[0].unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        path,
                        "mismatch operands and floor must use one exact unit",
                    )
            if kind == "compliance_interval":
                parent = parent_items[0] if parent_items else None
                axis_unit = (
                    self._point_curve_axis_unit(
                        parent, analysis=analysis, condition=condition
                    )
                    if parent is not None
                    else None
                )
                if parent is not None and (
                    raw["lower"]["unit"] != parent.unit
                    or raw["upper"]["unit"] != parent.unit
                ):
                    self.add(
                        "testbench_plan.unit.mismatch",
                        path,
                        "compliance bounds must match the input curve value unit",
                    )
                if axis_unit is not None and unit != axis_unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/unit",
                        f"compliance interval must use curve axis unit {axis_unit!r}",
                    )
                if float(raw["upper"]["value"]) <= float(raw["lower"]["value"]):
                    self.add(
                        "testbench_plan.measurement.range_invalid",
                        path,
                        "compliance upper bound must exceed lower bound",
                    )
            item = Measurement(identifier, kind, unit, dict(raw))
            by_id[identifier] = item
            output.append(item)
        self._check_dag(dependency_graph, path=f"{point_path}/measurements")
        return output

    @staticmethod
    def _point_curve_axis_unit(
        measurement: Measurement,
        *,
        analysis: Mapping[str, Any],
        condition: Mapping[str, Any],
    ) -> str | None:
        if measurement.kind not in {"curve", "loop_transfer"}:
            return None
        del condition
        return {
            "dc_sweep": "V",
            "pulse_train_transient": "s",
            "phase_offset_pair_transient": "s",
            "linear_ac": "Hz",
        }.get(str(analysis["kind"]))

    @staticmethod
    def _fit_unit(parent_unit: str) -> str:
        return {
            "A": "A/V",
            "C": "C/V",
            "V": "V/V",
        }.get(parent_unit, parent_unit)

    def _validate_validity(
        self,
        values: Sequence[Mapping[str, Any]],
        measurements: Sequence[Measurement],
        stimuli: Mapping[str, Stimulus],
        supplies: Mapping[str, Supply],
        point_path: str,
    ) -> dict[str, Mapping[str, Any]]:
        output: dict[str, Mapping[str, Any]] = {}
        by_id = {item.identifier: item for item in measurements}
        for index, raw in enumerate(values):
            identifier = str(raw["id"])
            path = f"{point_path}/validity_rules/{index}"
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate validity-rule id"
                )
                continue
            output[identifier] = raw
            selected: list[Measurement] = []
            if "measurement_id" in raw:
                item = by_id.get(str(raw["measurement_id"]))
                if item is None:
                    self.add(
                        "testbench_plan.validity.measurement_unknown",
                        f"{path}/measurement_id",
                        f"unknown measurement {raw['measurement_id']!r}",
                    )
                else:
                    selected.append(item)
            if "measurement_ids" in raw:
                for item_index, item_id in enumerate(raw["measurement_ids"]):
                    item = by_id.get(str(item_id))
                    if item is None:
                        self.add(
                            "testbench_plan.validity.measurement_unknown",
                            f"{path}/measurement_ids/{item_index}",
                            f"unknown measurement {item_id!r}",
                        )
                    else:
                        selected.append(item)
            kind = str(raw["kind"])
            if kind == "r2" and selected and selected[0].kind != "linear_fit":
                self.add(
                    "testbench_plan.validity.measurement_incompatible",
                    f"{path}/measurement_id",
                    "R-squared validity requires a linear_fit measurement",
                )
            if kind in {"monotonicity", "single_sign_change", "crossings"} and selected and selected[0].kind != "curve":
                self.add(
                    "testbench_plan.validity.measurement_incompatible",
                    f"{path}/measurement_id",
                    f"{kind} validity requires a curve measurement",
                )
            if kind == "unity_crossing" and selected and selected[0].kind != "loop_transfer":
                self.add(
                    "testbench_plan.validity.measurement_incompatible",
                    f"{path}/measurement_id",
                    "unity-crossing validity requires a loop_transfer measurement",
                )
            quantity_field = {
                "settling_delta": "maximum",
                "monotonicity": "tolerance",
                "single_sign_change": "zero_tolerance",
                "threshold": "value",
                "crossings": "threshold",
            }.get(kind)
            threshold_unit = (
                self._threshold_unit(raw[quantity_field], supplies)
                if quantity_field and quantity_field in {"value", "threshold"}
                else self._quantity_unit(raw[quantity_field]) if quantity_field else None
            )
            if quantity_field and selected and threshold_unit != selected[0].unit:
                self.add(
                    "testbench_plan.unit.mismatch",
                    f"{path}/{quantity_field}/unit",
                    f"validity threshold must use measurement unit {selected[0].unit!r}",
                )
            if kind in {"crossings", "pulse_count", "unity_crossing"} and int(raw["maximum_count"]) < int(raw["minimum_count"]):
                self.add(
                    "testbench_plan.validity.range_invalid",
                    path,
                    "maximum_count must be at least minimum_count",
                )
            if kind == "pulse_count":
                stimulus = stimuli.get(str(raw["stimulus_id"]))
                if stimulus is None or stimulus.kind not in {"pulse_train", "phase_offset_pair"}:
                    self.add(
                        "testbench_plan.validity.stimulus_unknown",
                        f"{path}/stimulus_id",
                        "pulse_count requires a declared pulsed stimulus",
                    )
            if kind == "settling_delta" and len(selected) == 2:
                if selected[0].unit != selected[1].unit:
                    self.add(
                        "testbench_plan.unit.mismatch",
                        f"{path}/measurement_ids",
                        "settling comparison measurements must use one exact unit",
                    )
        return output

    @staticmethod
    def _divide_units(numerator: str, denominator: str) -> str | None:
        if denominator == "1":
            return numerator
        if numerator == denominator:
            return "1"
        return {
            ("C", "A"): "s",
            ("A", "V"): "A/V",
            ("C", "V"): "C/V",
            ("V", "s"): "V/s",
            ("A", "s"): "A/s",
            ("C", "s"): "A",
        }.get((numerator, denominator))

    @staticmethod
    def _multiply_units(left: str, right: str) -> str | None:
        if left == "1":
            return right
        if right == "1":
            return left
        pair = frozenset((left, right))
        return {
            frozenset(("A", "s")): "C",
            frozenset(("A/V", "V")): "A",
            frozenset(("C/V", "V")): "C",
            frozenset(("V/s", "s")): "V",
        }.get(pair)

    def _reduction_component_unit(
        self,
        reduction: Reduction,
        component: str,
        reductions: Mapping[str, Reduction],
    ) -> str | None:
        if component in {"lower", "upper", "span"}:
            return reduction.unit if reduction.kind == "compliance_intersection" else None
        if component in {"value", "slope", "crossing"}:
            allowed = {
                "linear_fit": {"value", "slope", "intercept", "r2"},
                "crossing": {"value", "crossing"},
            }.get(reduction.kind, {"value"})
            return reduction.unit if component in allowed else None
        if component == "r2":
            return "1" if reduction.kind == "linear_fit" else None
        if component == "intercept" and reduction.kind == "linear_fit":
            parent = reductions.get(str(reduction.document["input_reduction_id"]))
            return parent.unit if parent is not None else None
        return None

    @staticmethod
    def _reduction_shape(reduction: Reduction, component: str) -> str:
        if component != "value":
            return "scalar"
        return {
            "collect_array": "array",
            "collect_curve": "curve",
            "linear_fit": "fit",
            "compliance_intersection": "interval",
        }.get(reduction.kind, "scalar")

    def _validate_reductions(
        self,
        values: Sequence[Mapping[str, Any]],
        stage_id: str,
        points: Sequence[Mapping[str, Any]],
        measurements: Mapping[tuple[str, str, str], Measurement],
        supplies: Mapping[str, Supply],
        stage_path: str,
    ) -> list[Reduction]:
        output: list[Reduction] = []
        by_id: dict[str, Reduction] = {}
        point_by_id = {str(point["id"]): point for point in points}

        def source_measurement(source, path):
            key = (stage_id, str(source["point_id"]), str(source["measurement_id"]))
            item = measurements.get(key)
            if item is None:
                self.add(
                    "testbench_plan.reduction.source_unknown", path,
                    "reduction source measurement does not exist in this stage",
                )
            elif item.kind in {"curve", "linear_fit", "compliance_interval"}:
                self.add(
                    "testbench_plan.reduction.source_incompatible", path,
                    "collection sources must be scalar point measurements",
                )
            return item

        for index, raw in enumerate(values):
            path = f"{stage_path}/reductions/{index}"
            identifier = str(raw["id"])
            if identifier in by_id:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate reduction id"
                )
                continue
            kind = str(raw["kind"])
            unit = str(raw["unit"])
            if kind == "collect_array":
                seen_sources: set[tuple[str, str]] = set()
                for item_index, entry in enumerate(raw["items"]):
                    source = entry["source"]
                    source_id = (str(source["point_id"]), str(source["measurement_id"]))
                    if source_id in seen_sources:
                        self.add(
                            "testbench_plan.reduction.source_duplicate",
                            f"{path}/items/{item_index}/source",
                            "ordered array sources must be unique",
                        )
                    seen_sources.add(source_id)
                    item = source_measurement(source, f"{path}/items/{item_index}/source")
                    if item is not None and item.unit != unit:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/unit",
                            "array item and reduction units must match exactly",
                        )
            elif kind == "collect_curve":
                seen_sources = set()
                for sample_index, sample in enumerate(raw["samples"]):
                    source = sample["source"]
                    source_id = (str(source["point_id"]), str(source["measurement_id"]))
                    if source_id in seen_sources:
                        self.add(
                            "testbench_plan.reduction.source_duplicate",
                            f"{path}/samples/{sample_index}/source",
                            "curve sample sources must be unique",
                        )
                    seen_sources.add(source_id)
                    item = source_measurement(source, f"{path}/samples/{sample_index}/source")
                    if item is not None and item.unit != unit:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/unit",
                            "curve sample and reduction units must match exactly",
                        )
                    axis = sample["x"]
                    axis_unit = str(axis["unit"])
                    if axis_unit != raw["axis_unit"]:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/samples/{sample_index}/x/unit",
                            "curve sample axis must match axis_unit",
                        )
                    if axis.get("kind") == "condition_parameter":
                        point = point_by_id.get(str(source["point_id"]))
                        parameter = next(
                            (entry for entry in point["condition"]["parameters"]
                             if entry["name"] == axis["name"]), None
                        ) if point is not None else None
                        if parameter is None:
                            self.add(
                                "testbench_plan.reduction.axis_unknown",
                                f"{path}/samples/{sample_index}/x/name",
                                "sample point does not declare this condition parameter",
                            )
                        elif parameter["value"]["unit"] != axis_unit:
                            self.add(
                                "testbench_plan.unit.mismatch",
                                f"{path}/samples/{sample_index}/x/unit",
                                "condition parameter and sample axis units differ",
                            )
            elif kind in {"linear_fit", "crossing", "select"}:
                parent = by_id.get(str(raw["input_reduction_id"]))
                if parent is None:
                    self.add(
                        "testbench_plan.reduction.parent_unknown",
                        f"{path}/input_reduction_id",
                        "reduction parent must be declared earlier in this stage",
                    )
                elif kind in {"linear_fit", "crossing"} and parent.kind != "collect_curve":
                    self.add(
                        "testbench_plan.reduction.parent_incompatible",
                        f"{path}/input_reduction_id",
                        f"{kind} requires a collect_curve parent",
                    )
                elif kind == "select" and parent.kind != "collect_array":
                    self.add(
                        "testbench_plan.reduction.parent_incompatible",
                        f"{path}/input_reduction_id",
                        "select requires a collect_array parent",
                    )
                if parent is not None and kind == "linear_fit":
                    expected = self._divide_units(parent.unit, str(parent.document["axis_unit"]))
                    if expected is None or unit != expected:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/unit",
                            "linear-fit unit is not the closed y/axis unit",
                        )
                    for endpoint in ("start", "stop"):
                        if raw["window"][endpoint]["unit"] != parent.document["axis_unit"]:
                            self.add(
                                "testbench_plan.unit.mismatch", f"{path}/window/{endpoint}/unit",
                                "fit window must use the curve axis unit",
                            )
                elif parent is not None and kind == "crossing":
                    threshold_unit = self._threshold_unit(raw["threshold"], supplies)
                    if threshold_unit != parent.unit or unit != parent.document["axis_unit"]:
                        self.add(
                            "testbench_plan.unit.mismatch", path,
                            "crossing threshold must match y and output must match axis",
                        )
                elif parent is not None and kind == "select":
                    if unit != parent.unit:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/unit",
                            "selected item must retain the collected-array unit",
                        )
                    selector = raw["selector"]
                    if selector["kind"] == "index":
                        if int(selector["index"]) >= len(parent.document["items"]):
                            self.add(
                                "testbench_plan.reduction.selector_unknown", f"{path}/selector/index",
                                "array index is outside the explicit ordered item list",
                            )
                    else:
                        matches = [
                            entry for entry in parent.document["items"]
                            if any(
                                condition["name"] == selector["name"]
                                and condition["value"] == selector["equals"]
                                for condition in entry["condition_values"]
                            )
                        ]
                        if len(matches) != 1:
                            self.add(
                                "testbench_plan.reduction.selector_ambiguous", f"{path}/selector",
                                "condition selector must match exactly one explicit array item",
                            )
            elif kind == "arithmetic":
                operand_units: list[str | None] = []
                for operand_index, operand in enumerate(raw["operands"]):
                    if "value" in operand:
                        operand_units.append(str(operand["unit"]))
                        continue
                    parent = by_id.get(str(operand["reduction_id"]))
                    actual = (
                        self._reduction_component_unit(parent, str(operand["component"]), by_id)
                        if parent is not None else None
                    )
                    if parent is None or actual is None:
                        self.add(
                            "testbench_plan.reduction.operand_unknown",
                            f"{path}/operands/{operand_index}",
                            "arithmetic operand must name an earlier compatible reduction component",
                        )
                    elif actual != operand["unit"]:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/operands/{operand_index}/unit",
                            "operand declaration does not match component unit",
                        )
                    operand_units.append(actual)
                result = operand_units[0] if operand_units else None
                operator = str(raw["operator"])
                if operator in {"subtract", "divide"} and len(operand_units) != 2:
                    self.add(
                        "testbench_plan.reduction.arity_invalid", f"{path}/operands",
                        f"{operator} requires exactly two operands",
                    )
                if operator in {"absolute", "max_abs"} and len(operand_units) != 1:
                    self.add(
                        "testbench_plan.reduction.arity_invalid", f"{path}/operands",
                        f"{operator} requires exactly one operand",
                    )
                for next_unit in operand_units[1:]:
                    if result is None or next_unit is None:
                        result = None
                    elif operator in {"add", "subtract"}:
                        result = result if result == next_unit else None
                    elif operator == "multiply":
                        result = self._multiply_units(result, next_unit)
                    elif operator == "divide":
                        result = self._divide_units(result, next_unit)
                # Both closed unary magnitude operators preserve units. For
                # max_abs, a collected-array value operand is reduced to one scalar.
                if operator == "max_abs" and operand_units:
                    operand = raw["operands"][0]
                    parent = by_id.get(str(operand.get("reduction_id", "")))
                    if parent is None or parent.kind != "collect_array" or operand.get("component") != "value":
                        self.add(
                            "testbench_plan.reduction.operand_incompatible",
                            f"{path}/operands/0",
                            "max_abs requires the value component of a collected array",
                        )
                if result is None or result != unit:
                    self.add(
                        "testbench_plan.unit.unsupported_algebra", f"{path}/unit",
                        "declared arithmetic unit is not produced by the closed unit table",
                    )
            elif kind == "compliance_intersection":
                positive = by_id.get(str(raw["positive_curve_id"]))
                negative = by_id.get(str(raw["negative_curve_id"]))
                if positive is None or negative is None or any(
                    item.kind != "collect_curve" for item in (positive, negative) if item is not None
                ):
                    self.add(
                        "testbench_plan.reduction.parent_incompatible", path,
                        "compliance intersection requires two earlier collected curves",
                    )
                elif (
                    positive.unit != negative.unit
                    or positive.document["axis_unit"] != negative.document["axis_unit"]
                    or unit != positive.document["axis_unit"]
                ):
                    self.add(
                        "testbench_plan.unit.mismatch", path,
                        "compliance curves must share y/axis units and emit an axis interval",
                    )
                if positive is not None:
                    for field in ("positive_reference", "negative_reference"):
                        reference = raw[field]
                        reference_unit = str(reference["unit"])
                        if "reduction_id" in reference:
                            reference_parent = by_id.get(
                                str(reference["reduction_id"])
                            )
                            actual = (
                                self._reduction_component_unit(
                                    reference_parent,
                                    str(reference["component"]),
                                    by_id,
                                )
                                if reference_parent is not None
                                else None
                            )
                            if actual is None:
                                self.add(
                                    "testbench_plan.reduction.operand_unknown",
                                    f"{path}/{field}",
                                    "compliance reference must name an earlier scalar component",
                                )
                            elif actual != reference_unit:
                                self.add(
                                    "testbench_plan.unit.mismatch",
                                    f"{path}/{field}/unit",
                                    "compliance reference component unit mismatch",
                                )
                        if reference_unit != positive.unit:
                            self.add(
                                "testbench_plan.unit.mismatch",
                                f"{path}/{field}/unit",
                                "each compliance reference must match its curve y unit",
                            )
            item = Reduction(identifier, kind, unit, dict(raw))
            by_id[identifier] = item
            output.append(item)
        return output

    def _validate_stage_validity(
        self,
        values: Sequence[Mapping[str, Any]],
        reductions: Sequence[Reduction],
        supplies: Mapping[str, Supply],
        stage_path: str,
    ) -> dict[str, Mapping[str, Any]]:
        output: dict[str, Mapping[str, Any]] = {}
        by_id = {item.identifier: item for item in reductions}
        for index, raw in enumerate(values):
            path = f"{stage_path}/validity_rules/{index}"
            identifier = str(raw["id"])
            if identifier in output:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate stage validity id"
                )
                continue
            output[identifier] = raw
            ids = (
                list(raw.get("reduction_ids", ()))
                if "reduction_ids" in raw else [raw.get("reduction_id")]
            )
            selected = [by_id.get(str(item)) for item in ids]
            if any(item is None for item in selected):
                self.add(
                    "testbench_plan.validity.reduction_unknown", path,
                    "stage validity names an unknown reduction",
                )
                continue
            kind = str(raw["kind"])
            primary = selected[0]
            if kind == "r2" and primary.kind != "linear_fit":
                self.add(
                    "testbench_plan.validity.reduction_incompatible", path,
                    "R-squared validity requires a linear_fit reduction",
                )
            if kind in {"monotonicity", "single_sign_change", "crossings"} and primary.kind != "collect_curve":
                self.add(
                    "testbench_plan.validity.reduction_incompatible", path,
                    f"{kind} requires a collect_curve reduction",
                )
            quantity_field = {
                "settling_delta": "maximum",
                "monotonicity": "tolerance",
                "single_sign_change": "zero_tolerance",
                "threshold": "value",
                "crossings": "threshold",
            }.get(kind)
            if quantity_field:
                threshold_unit = (
                    self._threshold_unit(raw[quantity_field], supplies)
                    if quantity_field in {"value", "threshold"}
                    else str(raw[quantity_field]["unit"])
                )
                if threshold_unit != primary.unit:
                    self.add(
                        "testbench_plan.unit.mismatch", f"{path}/{quantity_field}",
                        "stage validity threshold must match reduction unit",
                    )
            if kind == "settling_delta" and selected[0].unit != selected[1].unit:
                self.add(
                    "testbench_plan.unit.mismatch", f"{path}/reduction_ids",
                    "settling comparison reductions must use one exact unit",
                )
            if kind == "crossings" and raw["maximum_count"] < raw["minimum_count"]:
                self.add(
                    "testbench_plan.validity.range_invalid", path,
                    "maximum_count must be at least minimum_count",
                )
        return output

    def _validate_bindings(
        self,
        values: Sequence[Mapping[str, Any]],
        stages: Mapping[str, Stage],
        measurements: Mapping[tuple[str, str, str], Measurement],
        reductions: Mapping[tuple[str, str], Reduction],
        stage_inputs: Mapping[tuple[str, str], str],
    ) -> None:
        binding_ids: set[str] = set()
        targets: set[tuple[str, str]] = set()
        stage_graph: dict[str, tuple[str, ...]] = {
            stage.identifier: stage.depends_on for stage in stages.values()
        }
        for index, raw in enumerate(values):
            path = f"/bindings/{index}"
            identifier = str(raw["id"])
            if identifier in binding_ids:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate binding id"
                )
            binding_ids.add(identifier)
            source = raw["from"]
            source_stage = str(source["stage_id"])
            measurement = None
            reduction = None
            if "measurement_id" in source:
                source_key = (
                    source_stage,
                    str(source["point_id"]),
                    str(source["measurement_id"]),
                )
                measurement = measurements.get(source_key)
                if measurement is None:
                    self.add(
                        "testbench_plan.binding.source_unknown",
                        f"{path}/from",
                        "binding source measurement does not exist",
                    )
            else:
                reduction = reductions.get((source_stage, str(source["reduction_id"])))
                if reduction is None:
                    self.add(
                        "testbench_plan.binding.source_unknown",
                        f"{path}/from",
                        "binding source reduction does not exist",
                    )
            target = raw["to"]
            target_key = (str(target["stage_id"]), str(target["input_id"]))
            target_unit = stage_inputs.get(target_key)
            if target_unit is None:
                self.add(
                    "testbench_plan.binding.target_unknown",
                    f"{path}/to",
                    "binding target input does not exist",
                )
            if target_key in targets:
                self.add(
                    "testbench_plan.binding.target_duplicate",
                    f"{path}/to",
                    "a stage input must have exactly one binding authority",
                )
            targets.add(target_key)
            target_stage = target_key[0]
            if source_stage == target_stage or source_stage not in stage_graph.get(
                target_stage, ()
            ):
                self.add(
                    "testbench_plan.binding.dependency_missing",
                    path,
                    "binding must flow from an explicitly depended-on earlier stage",
                )
            declared_unit = str(raw["unit"])
            source_unit = None
            if measurement is not None:
                component = str(source["component"])
                if component == "slope":
                    source_unit = self._fit_unit(measurement.unit) if measurement.kind != "linear_fit" else measurement.unit
                elif component == "intercept":
                    source_unit = (
                        self._fit_intercept_unit(measurement.unit)
                        if measurement.kind == "linear_fit"
                        else measurement.unit
                    )
                elif component == "crossing":
                    source_unit = measurement.unit
                else:
                    source_unit = measurement.unit
                allowed = {
                    "linear_fit": {"slope", "intercept"},
                    "crossing": {"crossing", "value"},
                }.get(measurement.kind, {"value"})
                if component not in allowed:
                    self.add(
                        "testbench_plan.binding.component_incompatible",
                        f"{path}/from/component",
                        f"component {component!r} is not emitted by {measurement.kind!r}",
                    )
            elif reduction is not None:
                siblings = {
                    key[1]: item for key, item in reductions.items()
                    if key[0] == source_stage
                }
                component = str(source["component"])
                source_unit = self._reduction_component_unit(
                    reduction, component, siblings
                )
                if source_unit is None:
                    self.add(
                        "testbench_plan.binding.component_incompatible",
                        f"{path}/from/component",
                        f"component {component!r} is not emitted by {reduction.kind!r}",
                    )
            if any(
                item is not None and item != declared_unit
                for item in (source_unit, target_unit)
            ):
                self.add(
                    "testbench_plan.unit.mismatch",
                    f"{path}/unit",
                    "binding source, declared unit, and target input must match exactly",
                )
        for target in sorted(set(stage_inputs) - targets):
            self.add(
                "testbench_plan.binding.input_unbound",
                f"/stages/{target[0]}/inputs/{target[1]}",
                "every declared stage input requires exactly one binding",
            )

    @staticmethod
    def _fit_intercept_unit(slope_unit: str) -> str:
        return {
            "A/V": "A",
            "C/V": "C",
            "V/V": "V",
        }.get(slope_unit, slope_unit)

    def _validate_observables(
        self,
        values: Sequence[Mapping[str, Any]],
        measurements: Mapping[tuple[str, str, str], Measurement],
        validity: Mapping[tuple[str, str, str], Mapping[str, Any]],
        reductions: Mapping[tuple[str, str], Reduction],
        stage_validity: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> list[Observable]:
        output: list[Observable] = []
        identifiers: set[str] = set()
        sources: set[tuple[str, ...]] = set()
        for index, raw in enumerate(values):
            path = f"/observables/{index}"
            identifier = str(raw["id"])
            if identifier in identifiers:
                self.add(
                    "testbench_plan.id.duplicate", f"{path}/id", "duplicate observable id"
                )
            identifiers.add(identifier)
            source = raw["source"]
            kind = str(source["kind"])
            if kind == "measurement":
                source_id = (
                    kind, str(source["stage_id"]), str(source["point_id"]),
                    str(source["measurement_id"]),
                )
            elif kind == "validity":
                source_id = (
                    kind, str(source["stage_id"]), str(source["point_id"]),
                    str(source["rule_id"]),
                )
            elif kind == "reduction":
                source_id = (
                    kind, str(source["stage_id"]), str(source["reduction_id"]),
                    str(source["component"]),
                )
            else:
                source_id = (kind, str(source["stage_id"]), str(source["rule_id"]))
            if source_id in sources:
                self.add(
                    "testbench_plan.observable.source_duplicate",
                    f"{path}/source",
                    "two observables may not alias the same result identity",
                )
            sources.add(source_id)
            unit = str(raw["unit"])
            shape = str(raw["shape"])
            if kind == "measurement":
                item = measurements.get(source_id[1:4])
                if item is None:
                    self.add(
                        "testbench_plan.observable.source_unknown",
                        f"{path}/source",
                        "observable source measurement does not exist",
                    )
                else:
                    expected_shape = {
                        "curve": "curve",
                        "loop_transfer": "curve",
                        "linear_fit": "fit",
                        "compliance_interval": "interval",
                    }.get(item.kind, "scalar")
                    if unit != item.unit:
                        self.add(
                            "testbench_plan.unit.mismatch",
                            f"{path}/unit",
                            f"observable unit must equal measurement unit {item.unit!r}",
                        )
                    if shape != expected_shape:
                        self.add(
                            "testbench_plan.observable.shape_mismatch",
                            f"{path}/shape",
                            f"{item.kind!r} emits shape {expected_shape!r}",
                        )
            elif kind == "validity":
                if source_id[1:4] not in validity:
                    self.add(
                        "testbench_plan.observable.source_unknown",
                        f"{path}/source",
                        "observable source validity rule does not exist",
                    )
                if unit != "1" or shape != "verdict":
                    self.add(
                        "testbench_plan.observable.shape_mismatch",
                        path,
                        "validity observables must be dimensionless verdicts",
                    )
            elif kind == "stage_validity":
                if source_id[1:3] not in stage_validity:
                    self.add(
                        "testbench_plan.observable.source_unknown",
                        f"{path}/source",
                        "observable source stage validity rule does not exist",
                    )
                if unit != "1" or shape != "verdict":
                    self.add(
                        "testbench_plan.observable.shape_mismatch", path,
                        "stage-validity observables must be dimensionless verdicts",
                    )
            else:
                item = reductions.get(source_id[1:3])
                if item is None:
                    self.add(
                        "testbench_plan.observable.source_unknown",
                        f"{path}/source",
                        "observable source reduction does not exist",
                    )
                else:
                    siblings = {
                        key[1]: candidate for key, candidate in reductions.items()
                        if key[0] == source_id[1]
                    }
                    component = source_id[3]
                    expected_unit = self._reduction_component_unit(item, component, siblings)
                    expected_shape = self._reduction_shape(item, component)
                    if expected_unit is None:
                        self.add(
                            "testbench_plan.observable.component_incompatible",
                            f"{path}/source/component",
                            f"component {component!r} is not emitted by {item.kind!r}",
                        )
                    elif unit != expected_unit:
                        self.add(
                            "testbench_plan.unit.mismatch", f"{path}/unit",
                            f"observable component unit must be {expected_unit!r}",
                        )
                    if shape != expected_shape:
                        self.add(
                            "testbench_plan.observable.shape_mismatch", f"{path}/shape",
                            f"reduction component emits shape {expected_shape!r}",
                        )
            output.append(
                Observable(identifier, dict(source), unit, shape)
            )
        return output

    def _global_identity_check(
        self,
        plan_id: str,
        dut: DutBinding,
        supplies: Mapping[str, Supply],
        stimuli: Mapping[str, Stimulus],
        probes: Mapping[str, Probe],
        stages: Mapping[str, Stage],
        bindings: Sequence[Mapping[str, Any]],
        observables: Sequence[Observable],
    ) -> None:
        namespaces = {
            "plan": [plan_id],
            "supply/stimulus/probe": [*supplies, *stimuli, *probes],
            "stage": list(stages),
            "binding": [str(item["id"]) for item in bindings],
            "observable": [item.identifier for item in observables],
        }
        for label, values in namespaces.items():
            for folded in self._folded_duplicates(values):
                self.add(
                    "testbench_plan.id.case_collision",
                    "",
                    f"{label} identifiers collide case-insensitively at {folded!r}",
                )
        generated = [
            f"X_{dut.namespace}_{dut.top}",
            *(f"V_{identifier}" for identifier in supplies),
            *(f"V_{identifier}" for identifier in stimuli),
            *(f"P_{identifier}" for identifier in probes),
        ]
        for folded in self._folded_duplicates(generated):
            self.add(
                "testbench_plan.namespace.collision",
                "",
                f"closed compiler names collide at {folded!r}",
            )


__all__ = [
    "ConditionPoint",
    "DutBinding",
    "Measurement",
    "Observable",
    "PreparedTestbenchPlan",
    "Probe",
    "Reduction",
    "Stage",
    "Stimulus",
    "Supply",
    "TESTBENCH_PLAN_SCHEMA",
    "TestbenchPlanIssue",
    "load_testbench_plan_schema",
    "validate_testbench_plan",
]
