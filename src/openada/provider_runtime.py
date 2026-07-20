"""Validated invocation boundary for explicit external local providers.

This module deliberately implements only the smallest runtime slice described by
``openada.driver-manifest/v0alpha1``: one explicitly supplied manifest, one
request with an explicit driver selector, and one synchronous ``local-cli``
JSON-stdio transport.  It does not discover, install, rank, or trust providers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
from itertools import islice
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sysconfig
import tempfile
import threading
from typing import Any

from .conformance import result_conformance_issues


REQUEST_SCHEMA_ID = "openada.request/v0alpha1"
MANIFEST_SCHEMA_ID = "openada.driver-manifest/v0alpha1"
RESULT_SCHEMA_ID = "openada.result/v0alpha1"

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 1 * 1024 * 1024
MAX_PROFILE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 5 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PROVIDER_TARGET_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_CONFIGURATION_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_INPUT_TOTAL_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_EVIDENCE_BYTES = 512 * 1024 * 1024
MAX_VALIDATION_ISSUES = 100
MAX_VALIDATION_ISSUE_CHARS = 2_000
LOCAL_PROVIDER_SANITIZED_PATH = "/usr/bin:/bin"
_LOCAL_PROVIDER_FIXED_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": LOCAL_PROVIDER_SANITIZED_PATH,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "TZ": "UTC",
}

_SCHEMA_FILENAMES = {
    REQUEST_SCHEMA_ID: "request-v0alpha1.schema.json",
    MANIFEST_SCHEMA_ID: "driver-manifest-v0alpha1.schema.json",
    RESULT_SCHEMA_ID: "result-v0alpha1.schema.json",
    "openada.operation-profile/v0alpha1": "operation-profile-v0alpha1.schema.json",
    "openada.operation-profile/v0alpha2": "operation-profile-v0alpha2.schema.json",
}
_UNDERSTOOD_RESULT_SCHEMA_IDS = {RESULT_SCHEMA_ID}
_SIDE_EFFECT_RANK = {
    "read-only": 0,
    "evidence-only": 1,
    "transactional-design-write": 2,
}
_MATURITY_RANK = {
    "discovered": 0,
    "structured": 1,
    "workflow-validated": 2,
}
_KNOWN_ENVELOPE_OPERATIONS = {
    "openada.operation/circuit.simulate/v1alpha1": "simulate",
    "openada.operation/circuit.simulate/v1alpha2": "simulate",
}
_EXTERNALLY_DISPATCHABLE_PROFILES = {
    "openada.operation/circuit.simulate/v1alpha2",
}


class ProviderRuntimeError(RuntimeError):
    """Stable provider-boundary failure suitable for CLI normalization."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        issues: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.issues = tuple(issues)
        detail = "; ".join(self.issues)
        super().__init__(f"{message}: {detail}" if detail else message)


@dataclass(frozen=True, slots=True)
class _BoundRequestInput:
    label: str
    path: str
    bytes: int
    sha256: str
    maximum_bytes: int
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ResolvedLocalProvider:
    """One exact manifest capability and local transport selection."""

    driver_id: str
    driver_version: str
    capability_index: int
    capability: Mapping[str, Any]
    transport_id: str
    transport: Mapping[str, Any]
    argv: tuple[str, ...]
    executable: str
    bound_argv_files: tuple[tuple[int, str], ...]
    required_features: tuple[str, ...]
    operation_profile: Mapping[str, Any]
    request_inputs: tuple[_BoundRequestInput, ...]


@dataclass(frozen=True, slots=True)
class _ProviderProcessObservation:
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    write_errors: tuple[str, ...]
    returncode: int


def _bounded_text(value: object, *, limit: int = MAX_VALIDATION_ISSUE_CHARS) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _json_pointer(parts: Sequence[object]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "#" if not encoded else "#/" + "/".join(encoded)


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON object key {key!r}")
        parsed[key] = value
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _strict_json_issue(value: object) -> str | None:
    """Return one bounded issue when an in-process value is not strict JSON."""

    active: set[int] = set()
    nodes = 0

    def visit(item: object, path: tuple[object, ...], depth: int) -> str | None:
        nonlocal nodes
        nodes += 1
        if nodes > 1_000_000:
            return "#: JSON value exceeds the one-million-node validation bound"
        if depth > 100:
            return f"{_json_pointer(path)}: JSON nesting exceeds 100 levels"
        if item is None or isinstance(item, (str, bool, int)):
            return None
        if isinstance(item, float):
            if not math.isfinite(item):
                return f"{_json_pointer(path)}: JSON number must be finite"
            return None
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                return f"{_json_pointer(path)}: cyclic object is not JSON"
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        return f"{_json_pointer(path)}: JSON object keys must be strings"
                    issue = visit(child, (*path, key), depth + 1)
                    if issue is not None:
                        return issue
            finally:
                active.remove(identity)
            return None
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            identity = id(item)
            if identity in active:
                return f"{_json_pointer(path)}: cyclic array is not JSON"
            active.add(identity)
            try:
                for index, child in enumerate(item):
                    issue = visit(child, (*path, index), depth + 1)
                    if issue is not None:
                        return issue
            finally:
                active.remove(identity)
            return None
        return f"{_json_pointer(path)}: value of type {type(item).__name__} is not JSON"

    return visit(value, (), 0)


def _read_json_object(
    path: str | Path,
    *,
    role: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    """Read one stable, regular, non-linked, strict-JSON object within a bound."""

    try:
        candidate = Path(path).expanduser()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProviderRuntimeError(
            f"provider.{role}.invalid",
            f"The {role} path could not be expanded",
            issues=(_bounded_text(exc),),
        ) from exc

    try:
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise OSError("symbolic links are not accepted")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ProviderRuntimeError(
            f"provider.{role}.invalid",
            f"The {role} could not be opened as a regular non-linked file",
            issues=(_bounded_text(exc),),
        ) from exc

    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError(f"the {role} must be a regular file")
        if (before.st_dev, before.st_ino) != (initial.st_dev, initial.st_ino):
            raise ValueError(f"the {role} changed while it was being opened")
        if initial.st_size > maximum_bytes:
            raise ValueError(
                f"the {role} is {initial.st_size} bytes, exceeding the "
                f"{maximum_bytes}-byte limit"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final = os.fstat(descriptor)
        if len(body) > maximum_bytes:
            raise ValueError(f"the {role} exceeds the {maximum_bytes}-byte limit")
        identity = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(initial, name) != getattr(final, name) for name in identity):
            raise ValueError(f"the {role} changed while it was being read")
        if len(body) != initial.st_size:
            raise ValueError(f"the {role} changed while it was being read")
        parsed = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderRuntimeError(
            f"provider.{role}.invalid",
            f"The {role} is not a stable bounded strict-JSON object",
            issues=(_bounded_text(exc),),
        ) from exc
    finally:
        os.close(descriptor)

    if not isinstance(parsed, dict):
        raise ProviderRuntimeError(
            f"provider.{role}.invalid",
            f"The {role} must contain one JSON object",
        )
    return parsed


def _installed_data_path(directory: str, filename: str) -> Path:
    source = Path(__file__).resolve().parents[2] / directory / filename
    if source.is_file():
        return source

    try:
        installed = distribution("openada")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        suffix = f"share/openada/{directory}/{filename}"
        for entry in installed.files or ():
            if entry.as_posix().endswith(suffix):
                candidate = Path(installed.locate_file(entry)).resolve()
                if candidate.is_file():
                    return candidate

    data = Path(sysconfig.get_path("data")) / "share" / "openada" / directory / filename
    if data.is_file():
        return data
    raise ProviderRuntimeError(
        "provider.contract.unavailable",
        f"The installed OpenADA contract file is unavailable: {directory}/{filename}",
    )


def _load_schema(schema_id: str) -> dict[str, Any]:
    filename = _SCHEMA_FILENAMES.get(schema_id)
    if filename is None:
        raise ProviderRuntimeError(
            "provider.contract.unsupported",
            f"OpenADA does not understand result or contract schema {schema_id!r}",
        )
    return _read_json_object(
        _installed_data_path("schemas", filename),
        role="contract",
        maximum_bytes=MAX_PROFILE_BYTES,
    )


def _jsonschema_types():
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - isolated-install behavior
        raise ProviderRuntimeError(
            "provider.validation.unavailable",
            "External-provider validation requires the package's jsonschema "
            "dependency; reinstall OpenADA",
        ) from exc
    return Draft202012Validator, FormatChecker


def _schema_issues(value: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    strict_issue = _strict_json_issue(value)
    if strict_issue is not None:
        return [strict_issue]
    Draft202012Validator, FormatChecker = _jsonschema_types()
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        sampled = list(
            islice(validator.iter_errors(value), MAX_VALIDATION_ISSUES + 1)
        )
    except Exception as exc:
        raise ProviderRuntimeError(
            "provider.contract.invalid",
            "An installed OpenADA schema could not be evaluated",
            issues=(_bounded_text(exc),),
        ) from exc
    truncated = len(sampled) > MAX_VALIDATION_ISSUES
    errors = sampled[:MAX_VALIDATION_ISSUES]
    errors.sort(key=lambda item: tuple(str(part) for part in item.absolute_path))
    issues = [
        _bounded_text(f"{_json_pointer(tuple(error.absolute_path))}: {error.message}")
        for error in errors[:MAX_VALIDATION_ISSUES]
    ]
    if truncated:
        issues[-1:] = [
            f"#: additional schema issues omitted after {MAX_VALIDATION_ISSUES - 1}"
        ]
    return issues


def _profile_paths() -> list[Path]:
    paths: dict[str, Path] = {}
    source_directory = Path(__file__).resolve().parents[2] / "profiles"
    if source_directory.is_dir():
        for path in source_directory.glob("*.json"):
            paths[str(path.resolve())] = path

    try:
        installed = distribution("openada")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        for entry in installed.files or ():
            value = entry.as_posix()
            if "/share/openada/profiles/" in f"/{value}" and value.endswith(".json"):
                path = Path(installed.locate_file(entry)).resolve()
                if path.is_file():
                    paths[str(path)] = path

    directory = Path(sysconfig.get_path("data")) / "share" / "openada" / "profiles"
    if directory.is_dir():
        for path in directory.glob("*.json"):
            paths[str(path.resolve())] = path
    return [paths[key] for key in sorted(paths)]


def load_operation_profile(operation_profile_id: str) -> dict[str, Any] | None:
    """Return one schema-valid locally installed operation profile, if known."""

    matches: list[dict[str, Any]] = []
    for path in _profile_paths():
        document = _read_json_object(
            path,
            role="profile",
            maximum_bytes=MAX_PROFILE_BYTES,
        )
        operation = document.get("operation")
        if not isinstance(operation, Mapping) or operation.get("id") != operation_profile_id:
            continue
        schema_id = document.get("schema")
        if not isinstance(schema_id, str):
            raise ProviderRuntimeError(
                "provider.profile.invalid",
                f"Installed profile {operation_profile_id!r} has no schema identity",
            )
        issues = _schema_issues(document, _load_schema(schema_id))
        if issues:
            raise ProviderRuntimeError(
                "provider.profile.invalid",
                f"Installed profile {operation_profile_id!r} is invalid",
                issues=issues,
            )
        matches.append(document)
    if not matches:
        return None
    canonical = {
        json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for item in matches
    }
    if len(canonical) != 1:
        raise ProviderRuntimeError(
            "provider.profile.ambiguous",
            f"Conflicting installed definitions exist for {operation_profile_id!r}",
        )
    return matches[0]


def list_operation_profiles() -> tuple[dict[str, Any], ...]:
    """Return every distinct, schema-valid operation profile installed locally."""

    profiles: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in _profile_paths():
        document = _read_json_object(
            path,
            role="profile",
            maximum_bytes=MAX_PROFILE_BYTES,
        )
        schema_id = document.get("schema")
        operation = document.get("operation")
        operation_id = operation.get("id") if isinstance(operation, Mapping) else None
        if not isinstance(schema_id, str) or not isinstance(operation_id, str):
            raise ProviderRuntimeError(
                "provider.profile.invalid",
                f"Installed profile {path} has no schema or operation identity",
            )
        issues = _schema_issues(document, _load_schema(schema_id))
        if issues:
            raise ProviderRuntimeError(
                "provider.profile.invalid",
                f"Installed profile {operation_id!r} is invalid",
                issues=issues,
            )
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        previous = profiles.get(operation_id)
        if previous is not None and previous[0] != canonical:
            raise ProviderRuntimeError(
                "provider.profile.ambiguous",
                f"Conflicting installed definitions exist for {operation_id!r}",
            )
        profiles[operation_id] = (canonical, document)
    return tuple(profiles[identity][1] for identity in sorted(profiles))


def _duplicates(values: Sequence[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated


def provider_manifest_issues(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return schema and cross-reference issues for one v0alpha1 manifest."""

    schema_issues = _schema_issues(manifest, _load_schema(MANIFEST_SCHEMA_ID))
    if schema_issues:
        return tuple(schema_issues)

    issues: list[str] = []
    driver_version = manifest["driver"]["version"]
    products = manifest["native_products"]
    transports = manifest["transports"]
    capabilities = manifest["capabilities"]
    records = manifest["conformance_records"]

    product_ids = [item["product_id"] for item in products]
    transport_ids = [item["id"] for item in transports]
    record_ids = [item["record_id"] for item in records]
    for label, repeated in (
        ("native product", _duplicates(product_ids)),
        ("transport", _duplicates(transport_ids)),
        ("conformance record", _duplicates(record_ids)),
    ):
        for value in sorted(repeated):
            issues.append(f"#: duplicate {label} identity {value!r}")

    product_by_id = {item["product_id"]: item for item in products}
    transport_by_id = {item["id"]: item for item in transports}
    record_by_id = {item["record_id"]: item for item in records}

    for index, transport in enumerate(transports):
        if transport["type"] != "local-cli":
            continue
        for argument_index, argument in enumerate(transport["argv"]):
            if any(ord(character) < 32 or ord(character) == 127 for character in argument):
                issues.append(
                    f"#/transports/{index}/argv/{argument_index}: local CLI "
                    "arguments may not contain NUL or control characters"
                )

    for index, record in enumerate(records):
        observed_products = [
            item["product_id"] for item in record["native_product_versions"]
        ]
        for product_id in sorted(_duplicates(observed_products)):
            issues.append(
                f"#/conformance_records/{index}/native_product_versions: "
                f"duplicate native product identity {product_id!r}"
            )
        for product_id in observed_products:
            if product_id not in product_by_id:
                issues.append(
                    f"#/conformance_records/{index}/native_product_versions: "
                    f"unknown native product {product_id!r}"
                )

    capability_digests: dict[str, int] = {}
    for index, capability in enumerate(capabilities):
        digest = hashlib.sha256(
            json.dumps(
                capability,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        prior = capability_digests.get(digest)
        if prior is not None:
            issues.append(
                f"#/capabilities/{index}: duplicates capability record {prior}; "
                "v0alpha1 has no capability_id"
            )
        capability_digests[digest] = index

        for transport_id in capability["transport_ids"]:
            if transport_id not in transport_by_id:
                issues.append(
                    f"#/capabilities/{index}/transport_ids: unknown transport "
                    f"{transport_id!r}"
                )
        for product_id in capability["native_product_ids"]:
            if product_id not in product_by_id:
                issues.append(
                    f"#/capabilities/{index}/native_product_ids: unknown native "
                    f"product {product_id!r}"
                )
        for result_schema in capability["result_schema_ids"]:
            if result_schema not in _UNDERSTOOD_RESULT_SCHEMA_IDS:
                issues.append(
                    f"#/capabilities/{index}/result_schema_ids: unsupported result "
                    f"schema {result_schema!r}"
                )

        supporting = False
        supporting_products: set[str] = set()
        for record_id in capability["conformance_record_ids"]:
            record = record_by_id.get(record_id)
            if record is None:
                issues.append(
                    f"#/capabilities/{index}/conformance_record_ids: unknown record "
                    f"{record_id!r}"
                )
                continue
            if record["operation_profile"] != capability["operation_profile"]:
                issues.append(
                    f"#/capabilities/{index}: record {record_id!r} has a different "
                    "operation profile"
                )
            if record["assertion_profile"] not in capability["assertion_profiles"]:
                issues.append(
                    f"#/capabilities/{index}: record {record_id!r} has an "
                    "unadvertised assertion profile"
                )
            if record["result_schema_id"] not in capability["result_schema_ids"]:
                issues.append(
                    f"#/capabilities/{index}: record {record_id!r} has an "
                    "unadvertised result schema"
                )
            if record["driver_version"] != driver_version:
                issues.append(
                    f"#/capabilities/{index}: record {record_id!r} targets driver "
                    f"version {record['driver_version']!r}, not {driver_version!r}"
                )
            record_products = {
                item["product_id"] for item in record["native_product_versions"]
            }
            undeclared_products = record_products - set(capability["native_product_ids"])
            if undeclared_products:
                issues.append(
                    f"#/capabilities/{index}: record {record_id!r} names native "
                    f"products outside the capability: {', '.join(sorted(undeclared_products))}"
                )
            if (
                record["status"] == "pass"
                and _MATURITY_RANK[record["level"]]
                >= _MATURITY_RANK[capability["maturity"]]
                and record["operation_profile"] == capability["operation_profile"]
                and record["assertion_profile"] in capability["assertion_profiles"]
                and record["result_schema_id"] in capability["result_schema_ids"]
                and record["driver_version"] == driver_version
            ):
                supporting = True
                supporting_products.update(record_products)
        if capability["maturity"] != "discovered" and not supporting:
            issues.append(
                f"#/capabilities/{index}: {capability['maturity']} maturity lacks a "
                "matching passing conformance record at that level"
            )
        elif capability["maturity"] != "discovered":
            missing_products = set(capability["native_product_ids"]) - supporting_products
            if missing_products:
                issues.append(
                    f"#/capabilities/{index}: passing conformance records do not "
                    "cover every advertised native product: "
                    + ", ".join(sorted(missing_products))
                )

    return tuple(issues[:MAX_VALIDATION_ISSUES])


def validate_provider_manifest(manifest: Mapping[str, Any]) -> None:
    """Raise a stable error unless a manifest passes schema and reference checks."""

    issues = provider_manifest_issues(manifest)
    if issues:
        raise ProviderRuntimeError(
            "provider.manifest.invalid",
            "The external provider manifest is invalid",
            issues=issues,
        )


def load_provider_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate one explicit external-provider manifest."""

    manifest = _read_json_object(
        path,
        role="manifest",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )
    validate_provider_manifest(manifest)
    return manifest


def _path_value(value: Mapping[str, Any], dotted_path: str) -> object:
    current: object = value
    for component in dotted_path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return _MISSING
        current = current[component]
    return current


_MISSING = object()


def _profile_protocol_fields(profile: Mapping[str, Any]) -> set[str]:
    data_schema = profile["normalized_result"]["data_schema"]
    protocol_schema = data_schema.get("properties", {}).get("protocol", {})
    reference = protocol_schema.get("$ref") if isinstance(protocol_schema, Mapping) else None
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.rsplit("/", 1)[-1]
        protocol_schema = data_schema.get("$defs", {}).get(name, {})
    required = protocol_schema.get("required", ()) if isinstance(protocol_schema, Mapping) else ()
    return {item for item in required if isinstance(item, str)}


def _external_profile_issue(profile: Mapping[str, Any]) -> str | None:
    operation_id = profile["operation"]["id"]
    if operation_id not in _EXTERNALLY_DISPATCHABLE_PROFILES:
        return (
            f"profile {operation_id!r} has no registered host semantic validator "
            "for the v0alpha1 local-provider runtime"
        )
    protocol_fields = _profile_protocol_fields(profile)
    if not {"request_id", "operation_profile", "assertion_profile", "driver_id", "driver_version"}.issubset(
        protocol_fields
    ):
        return (
            f"profile {operation_id!r} cannot echo the selected external driver "
            "identity in its closed result protocol"
        )
    return None


def _profile_semantic_issues(
    profile: Mapping[str, Any], request: Mapping[str, Any]
) -> list[str]:
    operation_id = profile["operation"]["id"]
    issues: list[str] = []
    if operation_id == "openada.operation/circuit.simulate/v1alpha2":
        from .operations.circuit_simulate import circuit_simulation_parameter_issue

        issue = circuit_simulation_parameter_issue(request["parameters"])
        if issue is not None:
            issues.append(f"#/parameters: {issue}")

        references = [("#/target/locator", request["target"]["locator"])]
        references.extend(
            (f"#/configuration/{index}/locator", item["locator"])
            for index, item in enumerate(request["configuration"])
        )
        for label, locator in references:
            if locator["type"] != "filesystem":
                issues.append(
                    f"{label}/type: the v0alpha1 local-provider runtime can bind "
                    "filesystem inputs only"
                )
                continue
            path = locator["path"]
            if not os.path.isabs(path) or os.path.abspath(path) != path:
                issues.append(
                    f"{label}/path: the v0alpha1 local-provider runtime requires "
                    "a canonical absolute filesystem path"
                )

        destination = request["evidence_destination"]
        destination_locator = destination["locator"]
        if destination_locator["type"] != "filesystem":
            issues.append(
                "#/evidence_destination/locator/type: the v0alpha1 local-provider "
                "runtime requires a filesystem destination"
            )
        else:
            path = destination_locator["path"]
            if not os.path.isabs(path) or os.path.abspath(path) != path:
                issues.append(
                    "#/evidence_destination/locator/path: the v0alpha1 "
                    "local-provider runtime requires a canonical absolute path"
                )
        if destination["collision_policy"] != "fail-if-present":
            issues.append(
                "#/evidence_destination/collision_policy: the v0alpha1 "
                "local-provider runtime supports fail-if-present only"
            )
    declared_roles = {
        item["role"] for item in profile["evidence"]["artifact_roles"]
    }
    for role in request["evidence_policy"]["required_artifact_roles"]:
        if role not in declared_roles:
            issues.append(
                f"#/evidence_policy/required_artifact_roles: role {role!r} is "
                "not declared by the installed operation profile"
            )
    return issues


def _required_profile_features(
    profile: Mapping[str, Any], request: Mapping[str, Any]
) -> set[str]:
    parameters = request["parameters"]
    required: set[str] = set()
    for feature in profile.get("features", []):
        value = _path_value(parameters, feature["parameter_path"])
        if value is not _MISSING and value in feature["parameter_values"]:
            required.add(feature["id"])
    selector = request.get("driver_selector", {})
    required.update(selector.get("required_features", []))
    return required


def provider_request_issues(
    request: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return base and locally known operation-profile request issues."""

    issues = _schema_issues(request, _load_schema(REQUEST_SCHEMA_ID))
    if issues:
        return tuple(issues)
    if profile is None:
        profile = load_operation_profile(request["operation_profile"])
    if profile is None:
        return ()

    semantic_issues: list[str] = []
    if request["assertion_profile"] != profile["assertion"]["id"]:
        semantic_issues.append(
            "#/assertion_profile: does not match the installed operation profile"
        )
    if request["target"]["kind"] not in profile["operation"]["target_kinds"]:
        semantic_issues.append(
            "#/target/kind: is not accepted by the installed operation profile"
        )
    locator_type = request["target"]["locator"]["type"]
    if locator_type not in profile["operation"]["locator_types"]:
        semantic_issues.append(
            "#/target/locator/type: is not accepted by the installed operation profile"
        )
    authorized_side_effects = request["execution_constraints"]["side_effects"]
    profile_side_effects = profile["operation"]["side_effect_mode"]
    if _SIDE_EFFECT_RANK[profile_side_effects] > _SIDE_EFFECT_RANK[authorized_side_effects]:
        semantic_issues.append(
            "#/execution_constraints/side_effects: does not authorize the installed "
            "operation profile's side-effect mode"
        )

    configuration_roles = {
        item["role"]: item for item in profile["request"]["configuration_roles"]
    }
    observed_roles: set[str] = set()
    for index, reference in enumerate(request["configuration"]):
        role = reference["role"]
        definition = configuration_roles.get(role)
        if definition is None:
            semantic_issues.append(
                f"#/configuration/{index}/role: {role!r} is not declared by the "
                "installed operation profile"
            )
            continue
        if role in observed_roles:
            semantic_issues.append(
                f"#/configuration/{index}/role: duplicate configuration role {role!r}"
            )
        observed_roles.add(role)
        configuration_locator = reference["locator"]
        if configuration_locator["type"] not in definition["locator_types"]:
            semantic_issues.append(
                f"#/configuration/{index}/locator/type: is not accepted for role "
                f"{role!r}"
            )
        if (
            definition["identity_requirement"] == "content-digest"
            and "sha256" not in configuration_locator
        ):
            semantic_issues.append(
                f"#/configuration/{index}/locator: role {role!r} requires a "
                "content digest"
            )
    for role, definition in configuration_roles.items():
        if definition["required"] and role not in observed_roles:
            semantic_issues.append(
                f"#/configuration: required role {role!r} is missing"
            )
    parameter_issues = _schema_issues(
        request["parameters"], profile["request"]["parameters_schema"]
    )
    semantic_issues.extend(
        f"#/parameters{issue[1:]}" if issue.startswith("#") else issue
        for issue in parameter_issues
    )
    semantic_issues.extend(_profile_semantic_issues(profile, request))
    return tuple(semantic_issues[:MAX_VALIDATION_ISSUES])


def validate_provider_request(
    request: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> None:
    """Raise unless a request validates against its base and local profile schema."""

    issues = provider_request_issues(request, profile=profile)
    if issues:
        raise ProviderRuntimeError(
            "provider.request.invalid",
            "The external provider request is invalid",
            issues=issues,
        )


def load_provider_request(path: str | Path) -> dict[str, Any]:
    """Load and validate one bounded provider request."""

    request = _read_json_object(
        path,
        role="request",
        maximum_bytes=MAX_REQUEST_BYTES,
    )
    validate_provider_request(request)
    return request


def _resolve_executable(argv0: str, *, cwd: Path) -> str:
    if os.path.sep in argv0 or (os.path.altsep and os.path.altsep in argv0):
        path = Path(argv0).expanduser()
        if not path.is_absolute():
            path = cwd / path
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProviderRuntimeError(
                "provider.transport.unavailable",
                f"The provider executable could not be resolved: {argv0!r}",
                issues=(_bounded_text(exc),),
            ) from exc
    else:
        # Bare provider entrypoints are resolved only from paths derived from
        # the authorized working directory/current Python installation plus
        # fixed system locations.  Ambient PATH must not select provider code.
        search_entries: list[str] = []
        scripts = sysconfig.get_path("scripts")
        if scripts:
            search_entries.append(scripts)
        search_entries.append(str(cwd / "bin"))
        search_entries.extend(LOCAL_PROVIDER_SANITIZED_PATH.split(os.pathsep))
        search_path = os.pathsep.join(dict.fromkeys(search_entries))
        found = shutil.which(argv0, path=search_path)
        if found is None:
            raise ProviderRuntimeError(
                "provider.transport.unavailable",
                f"The provider executable is not on the bounded provider search path: {argv0!r}",
            )
        resolved = Path(found).resolve()
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ProviderRuntimeError(
            "provider.transport.unavailable",
            f"The provider executable could not be inspected: {resolved}",
            issues=(_bounded_text(exc),),
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ProviderRuntimeError(
            "provider.transport.unavailable",
            f"The provider executable is not an executable regular file: {resolved}",
        )
    return str(resolved)


def _working_directory(cwd: str | Path | None) -> Path:
    try:
        path = Path.cwd() if cwd is None else Path(cwd).expanduser()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProviderRuntimeError(
            "provider.transport.invalid",
            "The provider working directory could not be resolved",
            issues=(_bounded_text(exc),),
        ) from exc
    if not resolved.is_dir():
        raise ProviderRuntimeError(
            "provider.transport.invalid",
            f"The provider working directory is not a directory: {resolved}",
        )
    return resolved


def _snapshot_request_file(
    label: str,
    path_text: str,
    declared_sha256: str | None,
    *,
    maximum_bytes: int,
) -> tuple[_BoundRequestInput | None, str | None]:
    """Read one canonical regular request input once under a strict size bound."""

    path = Path(path_text)
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise OSError("symbolic links are not accepted")
        if not stat.S_ISREG(path_stat.st_mode):
            raise OSError("path is not a regular file")
        if path.resolve(strict=True) != path:
            raise OSError("path is not canonical or traverses a symbolic link")
        if path_stat.st_size > maximum_bytes:
            return (
                None,
                f"{label}/path: input exceeds the {maximum_bytes}-byte "
                "pre-launch ceiling",
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        return None, f"{label}/path: input is not a stable canonical regular file ({_bounded_text(exc)})"

    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("opened input is not a regular file")
            if (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                raise OSError("input changed while it was being opened")
            if opened.st_size > maximum_bytes:
                raise OSError("input grew beyond the pre-launch byte ceiling")
            digest = hashlib.sha256()
            observed_bytes = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                observed_bytes += len(chunk)
                if observed_bytes > maximum_bytes:
                    raise OSError("input grew beyond the pre-launch byte ceiling")
                digest.update(chunk)
            final = os.fstat(handle.fileno())
    except OSError as exc:
        return None, f"{label}/path: input could not be snapshotted safely ({_bounded_text(exc)})"

    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(opened, field) != getattr(final, field) for field in fields):
        return None, f"{label}/path: input changed while it was being hashed"
    if observed_bytes != opened.st_size:
        return None, f"{label}/path: input size changed while it was being hashed"
    observed_sha256 = digest.hexdigest()
    if declared_sha256 is not None and declared_sha256 != observed_sha256:
        return None, f"{label}/sha256: declared digest does not match the pre-launch input"
    identity = tuple(int(getattr(final, field)) for field in fields)
    return (
        _BoundRequestInput(
            label=label,
            path=path_text,
            bytes=observed_bytes,
            sha256=observed_sha256,
            maximum_bytes=maximum_bytes,
            identity=identity,  # type: ignore[arg-type]
        ),
        None,
    )


def _snapshot_request_inputs(
    request: Mapping[str, Any],
) -> tuple[tuple[_BoundRequestInput, ...], list[str]]:
    references = [
        (
            "#/target/locator",
            request["target"]["locator"],
            MAX_PROVIDER_TARGET_BYTES,
        )
    ]
    references.extend(
        (
            f"#/configuration/{index}/locator",
            item["locator"],
            MAX_PROVIDER_CONFIGURATION_BYTES,
        )
        for index, item in enumerate(request["configuration"])
    )
    snapshots: list[_BoundRequestInput] = []
    issues: list[str] = []
    total = 0
    for label, locator, maximum_bytes in references:
        path = locator.get("path")
        if (
            locator.get("type") != "filesystem"
            or not isinstance(path, str)
            or not os.path.isabs(path)
            or os.path.abspath(path) != path
        ):
            continue
        snapshot, issue = _snapshot_request_file(
            label,
            path,
            locator.get("sha256"),
            maximum_bytes=maximum_bytes,
        )
        if issue is not None:
            issues.append(issue)
            continue
        assert snapshot is not None
        total += snapshot.bytes
        if total > MAX_PROVIDER_INPUT_TOTAL_BYTES:
            issues.append(
                f"#/configuration: request inputs exceed the "
                f"{MAX_PROVIDER_INPUT_TOTAL_BYTES}-byte aggregate pre-launch ceiling"
            )
            break
        snapshots.append(snapshot)
    return tuple(snapshots), issues


def _bound_argv_files(
    argv: Sequence[str], *, cwd: Path
) -> tuple[tuple[int, str], ...]:
    """Resolve standalone path arguments that already name provider code/data."""

    bound: list[tuple[int, str]] = []
    for index, argument in enumerate(argv[1:], start=1):
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProviderRuntimeError(
                "provider.transport.unavailable",
                f"A provider argv path could not be inspected: {argument!r}",
                issues=(_bounded_text(exc),),
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ProviderRuntimeError(
                "provider.transport.unavailable",
                f"A provider argv path may not be a symbolic link: {argument!r}",
            )
        if stat.S_ISREG(metadata.st_mode):
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ProviderRuntimeError(
                    "provider.transport.unavailable",
                    f"A provider argv path could not be resolved: {argument!r}",
                    issues=(_bounded_text(exc),),
                ) from exc
            bound.append((index, str(resolved)))
    return tuple(bound)


def resolve_local_provider(
    manifest: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    cwd: str | Path | None = None,
) -> ResolvedLocalProvider:
    """Resolve exactly one dispatchable local-cli/wait capability pair."""

    validate_provider_manifest(manifest)
    profile = load_operation_profile(request.get("operation_profile", ""))
    validate_provider_request(request, profile=profile)
    if profile is None:
        raise ProviderRuntimeError(
            "provider.profile.unsupported",
            f"No installed operation profile can validate {request['operation_profile']!r}",
        )
    profile_issue = _external_profile_issue(profile)
    if profile_issue is not None:
        raise ProviderRuntimeError(
            "provider.profile.unsupported",
            "The installed operation profile is not dispatchable through the "
            "v0alpha1 local-provider boundary",
            issues=(profile_issue,),
        )
    if request["evidence_policy"]["identity_requirement"] not in {
        "content-digest",
        "best-available",
    }:
        raise ProviderRuntimeError(
            "provider.evidence.unsupported",
            "The local-provider result envelope cannot represent the requested "
            "evidence identity mechanism",
        )

    selector = request.get("driver_selector")
    if not isinstance(selector, Mapping):
        raise ProviderRuntimeError(
            "provider.selection.required",
            "External-provider invocation requires an explicit driver_selector",
        )
    driver = manifest["driver"]
    if selector["driver_id"] != driver["id"]:
        raise ProviderRuntimeError(
            "provider.selection.mismatch",
            f"Request selected {selector['driver_id']!r}, not manifest driver {driver['id']!r}",
        )
    if "driver_version" in selector and selector["driver_version"] != driver["version"]:
        raise ProviderRuntimeError(
            "provider.selection.mismatch",
            f"Request selected driver version {selector['driver_version']!r}, not "
            f"{driver['version']!r}",
        )
    if request["execution_constraints"]["completion"] != "wait":
        raise ProviderRuntimeError(
            "provider.transport.unsupported",
            "The first external-provider boundary supports completion='wait' only",
        )

    required_features = _required_profile_features(profile, request)
    locator_type = request["target"]["locator"]["type"]
    authorized_side_effects = request["execution_constraints"]["side_effects"]
    profile_side_effects = profile["operation"]["side_effect_mode"]
    selected_transport = selector.get("transport_id")
    transport_by_id = {item["id"]: item for item in manifest["transports"]}
    candidates: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    for index, capability in enumerate(manifest["capabilities"]):
        if capability["maturity"] == "discovered":
            continue
        if capability["operation_profile"] != request["operation_profile"]:
            continue
        if request["assertion_profile"] not in capability["assertion_profiles"]:
            continue
        if RESULT_SCHEMA_ID not in capability["result_schema_ids"]:
            continue
        if locator_type not in capability["locator_types"]:
            continue
        if "wait" not in capability["completion_modes"]:
            continue
        if not required_features.issubset(set(capability["features"])):
            continue
        if (
            profile_side_effects not in capability["side_effect_modes"]
            or _SIDE_EFFECT_RANK[profile_side_effects]
            > _SIDE_EFFECT_RANK[authorized_side_effects]
        ):
            continue
        for transport_id in capability["transport_ids"]:
            if selected_transport is not None and transport_id != selected_transport:
                continue
            transport = transport_by_id[transport_id]
            if transport["type"] != "local-cli":
                continue
            if "wait" not in transport["completion_modes"]:
                continue
            candidates.append((index, capability, transport))

    if not candidates:
        raise ProviderRuntimeError(
            "provider.resolution.none",
            "No dispatchable local-cli capability exactly satisfies the request",
        )
    if len(candidates) != 1:
        labels = [f"capability[{index}]/{transport['id']}" for index, _, transport in candidates]
        raise ProviderRuntimeError(
            "provider.resolution.ambiguous",
            "The v0alpha1 manifest resolves to more than one local capability/transport",
            issues=(", ".join(labels),),
        )

    directory = _working_directory(cwd)
    index, capability, transport = candidates[0]
    executable = _resolve_executable(transport["argv"][0], cwd=directory)
    bound_argv_files = _bound_argv_files(transport["argv"], cwd=directory)
    request_inputs, input_issues = _snapshot_request_inputs(request)
    if input_issues:
        raise ProviderRuntimeError(
            "provider.request.invalid",
            "The external provider request inputs could not be bound safely",
            issues=input_issues,
        )
    return ResolvedLocalProvider(
        driver_id=driver["id"],
        driver_version=driver["version"],
        capability_index=index,
        capability=capability,
        transport_id=transport["id"],
        transport=transport,
        argv=tuple(transport["argv"]),
        executable=executable,
        bound_argv_files=bound_argv_files,
        required_features=tuple(sorted(required_features)),
        operation_profile=profile,
        request_inputs=request_inputs,
    )


def _executable_identity(path: str) -> tuple[int, int, int, int, int, int]:
    metadata = Path(path).stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _prepare_evidence_destination(
    request: Mapping[str, Any],
) -> tuple[Path, tuple[int, int, int]]:
    """Check fail-if-present and pin the existing canonical parent directory."""

    destination = Path(request["evidence_destination"]["locator"]["path"])
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProviderRuntimeError(
            "provider.evidence.destination_invalid",
            "The evidence destination could not be inspected safely",
            issues=(_bounded_text(exc),),
        ) from exc
    else:
        raise ProviderRuntimeError(
            "provider.evidence.destination_exists",
            "The fail-if-present evidence destination already exists",
            issues=(str(destination),),
        )

    parent = destination.parent
    try:
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("destination parent must be a non-linked directory")
        if parent.resolve(strict=True) != parent:
            raise OSError("destination parent is not canonical or traverses a link")
    except OSError as exc:
        raise ProviderRuntimeError(
            "provider.evidence.destination_invalid",
            "The evidence destination requires an existing canonical parent directory",
            issues=(_bounded_text(exc),),
        ) from exc
    return destination, (parent_stat.st_dev, parent_stat.st_ino, parent_stat.st_mode)


def _check_evidence_destination_parent(
    destination: Path, expected: tuple[int, int, int]
) -> None:
    try:
        parent_stat = destination.parent.lstat()
        observed = (parent_stat.st_dev, parent_stat.st_ino, parent_stat.st_mode)
    except OSError as exc:
        raise ProviderRuntimeError(
            "provider.evidence.destination_changed",
            "The evidence destination parent disappeared during provider execution",
            issues=(_bounded_text(exc),),
        ) from exc
    if observed != expected:
        raise ProviderRuntimeError(
            "provider.evidence.destination_changed",
            "The evidence destination parent identity changed during provider execution",
        )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def _drain_bounded(
    stream,
    buffer: bytearray,
    total: list[int],
    maximum: int,
    process: subprocess.Popen[bytes],
) -> None:
    try:
        while chunk := stream.read(65_536):
            total[0] += len(chunk)
            remaining = maximum + 1 - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if total[0] > maximum:
                _kill_process_group(process)
    except (OSError, ValueError):
        pass


def _write_stdin(stream, body: bytes, error: list[str]) -> None:
    try:
        stream.write(body)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        error.append(_bounded_text(exc))
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _local_provider_environment(root: Path) -> dict[str, str]:
    """Return the exact environment visible to a local provider process."""

    home = root / "home"
    temporary = root / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    return {
        **_LOCAL_PROVIDER_FIXED_ENVIRONMENT,
        "HOME": str(home),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "TMPDIR": str(temporary),
    }


def _snapshot_transport_file(
    source: Path,
    destination: Path,
    *,
    expected_identity: tuple[int, int, int, int, int, int],
    executable: bool,
) -> tuple[int, int, int, int, int, int, str]:
    """Copy one already-bound launch file into a fresh private directory."""

    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        source_fd = os.open(source, read_flags)
        try:
            opened = os.fstat(source_fd)
            observed_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            if (
                observed_identity != expected_identity
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > MAX_PROVIDER_CONFIGURATION_BYTES
            ):
                raise OSError("launch input identity changed or is not a bounded regular file")
            destination_fd = os.open(destination, write_flags, 0o500 if executable else 0o400)
            digest = hashlib.sha256()
            total = 0
            try:
                while block := os.read(source_fd, 1024 * 1024):
                    total += len(block)
                    if total > MAX_PROVIDER_CONFIGURATION_BYTES:
                        raise OSError("launch input exceeded the snapshot ceiling")
                    digest.update(block)
                    position = 0
                    while position < len(block):
                        position += os.write(destination_fd, block[position:])
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            finished = os.fstat(source_fd)
        finally:
            os.close(source_fd)
        if _executable_identity(str(source)) != expected_identity or (
            finished.st_dev,
            finished.st_ino,
            finished.st_mode,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        ) != expected_identity:
            raise OSError("launch input changed while its private snapshot was created")
        snapshot = destination.stat()
        return (
            snapshot.st_dev,
            snapshot.st_ino,
            snapshot.st_mode,
            snapshot.st_size,
            snapshot.st_mtime_ns,
            snapshot.st_ctime_ns,
            digest.hexdigest(),
        )
    except OSError:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _snapshot_transport_argv(
    argv: Sequence[str],
    snapshot_indices: Sequence[int],
    expected_identities: Mapping[str, tuple[int, int, int, int, int, int]],
    root: Path,
) -> tuple[list[str], dict[Path, tuple[int, int, int, int, int, int, str]]]:
    private = root / "launch"
    private.mkdir(mode=0o700)
    snapped = list(argv)
    identities: dict[Path, tuple[int, int, int, int, int, int, str]] = {}
    for index in sorted(set(snapshot_indices)):
        source = Path(argv[index])
        destination = private / f"{index:03d}-{source.name}"
        identity = _snapshot_transport_file(
            source,
            destination,
            expected_identity=expected_identities[str(source)],
            executable=index == 0 or os.access(source, os.X_OK),
        )
        snapped[index] = str(destination)
        identities[destination] = identity
    return snapped, identities


def _run_local_provider_transport(
    argv: Sequence[str],
    *,
    directory: Path,
    request_body: bytes,
    timeout: float,
    max_result_bytes: int,
    max_stderr_bytes: int,
    snapshot_indices: Sequence[int],
    expected_identities: Mapping[str, tuple[int, int, int, int, int, int]],
) -> _ProviderProcessObservation:
    """Run one JSON-stdio provider under a closed, private environment."""

    with tempfile.TemporaryDirectory(
        prefix="openada-provider-environment-"
    ) as environment_directory:
        environment = _local_provider_environment(Path(environment_directory))
        environment_root = Path(environment_directory)
        try:
            launch_argv, snapshot_identities = _snapshot_transport_argv(
                argv,
                snapshot_indices,
                expected_identities,
                environment_root,
            )
            process = subprocess.Popen(
                launch_argv,
                cwd=str(directory),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (OSError, ValueError) as exc:
            raise ProviderRuntimeError(
                "provider.transport.unavailable",
                "The external provider could not be launched",
                issues=(_bounded_text(exc),),
            ) from exc

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        stdout_total = [0]
        stderr_total = [0]
        write_errors: list[str] = []
        threads = (
            threading.Thread(
                target=_write_stdin,
                args=(process.stdin, request_body, write_errors),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_bounded,
                args=(
                    process.stdout,
                    stdout,
                    stdout_total,
                    max_result_bytes,
                    process,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_bounded,
                args=(
                    process.stderr,
                    stderr,
                    stderr_total,
                    max_stderr_bytes,
                    process,
                ),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            process.wait()
            raise ProviderRuntimeError(
                "provider.transport.timed_out",
                f"The provider exceeded the declared {timeout:g}-second timeout",
            ) from exc
        else:
            # A wait transport owns the whole fresh process group.  A provider
            # may not return while leaving inherited descendants alive.
            _kill_process_group(process)
        finally:
            for thread in threads:
                thread.join(timeout=1.0)
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        assert process.returncode is not None
        for snapshot, expected in snapshot_identities.items():
            try:
                observed = snapshot.stat()
                digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            except OSError as exc:
                raise ProviderRuntimeError(
                    "provider.transport.identity_changed",
                    "A private provider launch snapshot disappeared during invocation",
                    issues=(_bounded_text(exc),),
                ) from exc
            identity = (
                observed.st_dev,
                observed.st_ino,
                observed.st_mode,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
                digest,
            )
            if identity != expected:
                raise ProviderRuntimeError(
                    "provider.transport.identity_changed",
                    "A private provider launch snapshot changed during invocation",
                    issues=(str(snapshot),),
                )
        return _ProviderProcessObservation(
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_bytes=stdout_total[0],
            stderr_bytes=stderr_total[0],
            write_errors=tuple(write_errors),
            returncode=process.returncode,
        )


def _parse_result(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderRuntimeError(
            "provider.result.invalid",
            "The provider did not return one strict UTF-8 JSON result",
            issues=(_bounded_text(exc),),
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderRuntimeError(
            "provider.result.invalid",
            "The provider result must be one JSON object",
        )
    return parsed


def _expected_envelope_operation(profile_id: str) -> str:
    known = _KNOWN_ENVELOPE_OPERATIONS.get(profile_id)
    if known is not None:
        return known
    operation_path = profile_id.split("/", 2)[1]
    return operation_path


def _provider_result_evidence_issues(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    engineering_status = payload["engineering"]["status"]
    execution_status = payload["execution"]["status"]
    truth = profile["assertion"]["truth_table"].get(engineering_status)
    if not isinstance(truth, Mapping):
        issues.append(
            f"#/engineering/status: {engineering_status!r} is not an outcome "
            "defined by the selected assertion profile"
        )
    elif execution_status not in truth["allowed_execution_statuses"]:
        issues.append(
            f"#/execution/status: {execution_status!r} is not allowed for "
            f"engineering outcome {engineering_status!r}"
        )

    evidence = profile["evidence"]
    role_definitions = {
        item["role"]: item for item in evidence["artifact_roles"]
    }
    log_roles = {
        role
        for role, definition in role_definitions.items()
        if "log" in definition["kind"].casefold() or role.endswith(".log")
    }
    artifacts = payload["artifacts"]
    limits = evidence["limits"]
    if len(artifacts) > limits["max_artifact_count"]:
        issues.append(
            f"#/artifacts: {len(artifacts)} records exceed the profile limit of "
            f"{limits['max_artifact_count']}"
        )
    actual_roles = {
        item["role"] for item in artifacts if item["exists"] is True
    }
    undeclared_roles = actual_roles - set(role_definitions)
    if undeclared_roles:
        issues.append(
            "#/artifacts: result contains roles outside the selected profile: "
            + ", ".join(sorted(undeclared_roles))
        )

    request_limits = request["execution_constraints"]
    artifact_limit = min(
        request_limits["max_artifact_bytes"], limits["max_artifact_bytes"]
    )
    log_limit = min(request_limits["max_log_bytes"], limits["max_log_bytes"])
    for index, artifact in enumerate(artifacts):
        if not artifact["exists"]:
            continue
        ceiling = log_limit if artifact["role"] in log_roles else artifact_limit
        if artifact["bytes"] > ceiling:
            issues.append(
                f"#/artifacts/{index}/bytes: {artifact['bytes']} exceeds the "
                f"effective {ceiling}-byte evidence ceiling"
            )

    if engineering_status in {"pass", "fail"}:
        required_roles = set(request["evidence_policy"]["required_artifact_roles"])
        required_roles.update(
            role
            for role, definition in role_definitions.items()
            if engineering_status in definition["required_for"]
        )
        missing_roles = required_roles - actual_roles
        if missing_roles:
            issues.append(
                "#/artifacts: conclusive engineering status lacks required roles: "
                + ", ".join(sorted(missing_roles))
            )

    normalized_evidence = payload["data"].get("evidence")
    if isinstance(normalized_evidence, Mapping):
        present = normalized_evidence.get("artifact_roles_present")
        if isinstance(present, Sequence) and not isinstance(
            present, (str, bytes, bytearray)
        ):
            if set(present) != actual_roles:
                issues.append(
                    "#/data/evidence/artifact_roles_present: does not exactly "
                    "match retained existing artifact roles"
                )
        if (
            request["evidence_policy"]["provenance"] == "complete-required"
            and normalized_evidence.get("provenance") != "complete"
        ):
            issues.append(
                "#/data/evidence/provenance: complete provenance was required"
            )

    if request["evidence_policy"]["identity_requirement"] == "content-digest":
        if any(
            artifact["exists"]
            and ("bytes" not in artifact or "sha256" not in artifact)
            for artifact in artifacts
        ):
            issues.append(
                "#/artifacts: content-digest evidence requires bytes and SHA-256"
            )

    return issues


def _circuit_result_issues(
    payload: Mapping[str, Any], request: Mapping[str, Any]
) -> list[str]:
    issues: list[str] = []
    status = payload["engineering"]["status"]
    execution = payload["execution"]
    data = payload["data"]
    analysis = data["analysis"]
    evidence = data["evidence"]
    requested_analysis = request["parameters"]["analysis"]["type"]
    if status in {"pass", "fail"}:
        if payload["tool"] is None:
            issues.append("#/tool: a conclusive circuit result requires native tool identity")
        if not execution["command"]:
            issues.append(
                "#/execution/command: a conclusive circuit result requires the native command identity"
            )
        if execution["exit_code"] is None:
            issues.append(
                "#/execution/exit_code: a conclusive circuit result requires a native exit code"
            )
        if analysis["type"] != requested_analysis:
            issues.append(
                "#/data/analysis/type: conclusive result does not match the requested analysis type"
            )
    if status == "pass":
        if execution["exit_code"] != 0:
            issues.append("#/execution/exit_code: pass requires native exit code 0")
        expected = {
            "completion": "completed",
            "convergence": "converged",
        }
        for field, value in expected.items():
            if analysis[field] != value:
                issues.append(
                    f"#/data/analysis/{field}: pass requires {value!r}"
                )
        for field in ("point_count", "dependent_variable_count", "finite_value_count"):
            value = analysis[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                issues.append(f"#/data/analysis/{field}: pass requires a positive integer")
        for field, value in {
            "request_binding": "exact",
            "freshness": "fresh",
            "structure": "valid",
        }.items():
            if evidence[field] != value:
                issues.append(f"#/data/evidence/{field}: pass requires {value!r}")
    elif status == "fail":
        if analysis["completion"] != "terminal-failure":
            issues.append(
                "#/data/analysis/completion: fail requires 'terminal-failure'"
            )
        if analysis["convergence"] != "non-converged":
            issues.append(
                "#/data/analysis/convergence: fail requires 'non-converged'"
            )
        for field in ("point_count", "dependent_variable_count", "finite_value_count"):
            value = analysis[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                issues.append(
                    f"#/data/analysis/{field}: fail requires a positive integer"
                )
        for field, value in {
            "request_binding": "exact",
            "freshness": "fresh",
            "structure": "valid",
        }.items():
            if evidence[field] != value:
                issues.append(f"#/data/evidence/{field}: fail requires {value!r}")
    elif status == "unknown" and not payload["diagnostics"]:
        issues.append("#/diagnostics: an unknown result requires a stable diagnostic")
    return issues


def _request_input_binding_issues(
    payload: Mapping[str, Any], request: Mapping[str, Any]
) -> list[str]:
    if payload["engineering"]["status"] not in {"pass", "fail"}:
        return []
    inputs = [item for item in payload["inputs"] if item["exists"]]
    issues: list[str] = []
    references = [("target", request["target"]["locator"])]
    references.extend(
        (f"configuration[{index}]", item["locator"])
        for index, item in enumerate(request["configuration"])
    )
    for label, locator in references:
        if locator["type"] != "filesystem":
            issues.append(
                f"#/inputs: conclusive result cannot bind non-filesystem {label}"
            )
            continue
        if (
            not Path(locator["path"]).is_absolute()
            or os.path.abspath(locator["path"]) != locator["path"]
        ):
            issues.append(
                f"#/inputs: conclusive result cannot bind non-canonical {label} path"
            )
            continue
        matches = [item for item in inputs if item["path"] == locator["path"]]
        if len(matches) != 1:
            issues.append(
                f"#/inputs: conclusive result does not bind exactly one {label} "
                f"filesystem path {locator['path']!r}"
            )
            continue
        expected_digest = locator.get("sha256")
        if expected_digest is not None and matches[0].get("sha256") != expected_digest:
            issues.append(
                f"#/inputs: {label} SHA-256 does not match the request locator"
            )
    return issues


def _request_snapshot_result_issues(
    payload: Mapping[str, Any], snapshots: Sequence[_BoundRequestInput]
) -> list[str]:
    """Recheck host snapshots and require provider input records to match them."""

    issues: list[str] = []
    retained_inputs = [item for item in payload["inputs"] if item["exists"]]
    for snapshot in snapshots:
        observed, issue = _snapshot_request_file(
            snapshot.label,
            snapshot.path,
            snapshot.sha256,
            maximum_bytes=snapshot.maximum_bytes,
        )
        if issue is not None or observed is None:
            issues.append(
                f"{snapshot.label}/path: input changed or became unavailable after "
                f"provider execution ({issue or 'snapshot unavailable'})"
            )
            continue
        if (
            observed.identity != snapshot.identity
            or observed.bytes != snapshot.bytes
            or observed.sha256 != snapshot.sha256
        ):
            issues.append(
                f"{snapshot.label}/path: input identity or content changed during provider execution"
            )
        matches = [item for item in retained_inputs if item["path"] == snapshot.path]
        if len(matches) != 1:
            issues.append(
                f"#/inputs: provider result does not retain exactly one host-bound "
                f"record for {snapshot.label}"
            )
            continue
        if (
            matches[0].get("bytes") != snapshot.bytes
            or matches[0].get("sha256") != snapshot.sha256
        ):
            issues.append(
                f"#/inputs: provider result identity conflicts with host snapshot for {snapshot.label}"
            )
    return issues


def _evidence_destination_issues(
    payload: Mapping[str, Any], request: Mapping[str, Any]
) -> list[str]:
    """Require every provider artifact to stay in the caller-authorized directory."""

    issues: list[str] = []
    locator = request["evidence_destination"]["locator"]
    if locator["type"] != "filesystem":
        return [
            "#/evidence_destination/locator/type: provider artifacts require a filesystem destination"
        ]
    destination = Path(locator["path"])
    conclusive = payload["engineering"]["status"] in {"pass", "fail"}
    try:
        destination_stat = destination.lstat()
        if stat.S_ISLNK(destination_stat.st_mode):
            raise OSError("destination is a symbolic link")
        if not stat.S_ISDIR(destination_stat.st_mode):
            raise OSError("destination is not a directory")
        resolved_destination = destination.resolve(strict=True)
        if resolved_destination != destination:
            raise OSError("destination is not canonical or traverses a symbolic link")
    except OSError as exc:
        if conclusive or payload["artifacts"]:
            issues.append(
                "#/evidence_destination: retained artifacts are not rooted in a "
                f"stable canonical directory ({_bounded_text(exc)})"
            )
        return issues

    for index, artifact in enumerate(payload["artifacts"]):
        path_text = artifact["path"]
        path = Path(path_text)
        if not path.is_absolute() or os.path.abspath(path_text) != path_text:
            issues.append(
                f"#/artifacts/{index}/path: artifact path is not canonical and absolute"
            )
            continue
        try:
            relative = path.relative_to(destination)
        except ValueError:
            issues.append(
                f"#/artifacts/{index}/path: artifact is outside evidence_destination"
            )
            continue
        if not relative.parts:
            issues.append(
                f"#/artifacts/{index}/path: artifact path names the destination directory"
            )
            continue
        if not artifact["exists"]:
            continue
        try:
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_destination)
        except (OSError, ValueError) as exc:
            issues.append(
                f"#/artifacts/{index}/path: artifact escapes or cannot be resolved "
                f"inside evidence_destination ({_bounded_text(exc)})"
            )
            continue
        if resolved_path != path:
            issues.append(
                f"#/artifacts/{index}/path: artifact path traverses a symbolic link"
            )
    return issues


def validate_provider_result(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
    resolved: ResolvedLocalProvider,
) -> None:
    """Validate the generic envelope, local profile data, and exact echoes."""

    issues = _schema_issues(payload, _load_schema(RESULT_SCHEMA_ID))
    if issues:
        raise ProviderRuntimeError(
            "provider.result.invalid",
            "The provider result does not satisfy openada.result/v0alpha1",
            issues=issues,
        )

    expected_operation = _expected_envelope_operation(request["operation_profile"])
    if payload["operation"] != expected_operation:
        raise ProviderRuntimeError(
            "provider.result.identity_mismatch",
            f"Result operation {payload['operation']!r} does not match "
            f"{expected_operation!r}",
        )

    data_schema = resolved.operation_profile["normalized_result"]["data_schema"]
    data_issues = _schema_issues(payload["data"], data_schema)
    if data_issues:
        raise ProviderRuntimeError(
            "provider.result.data_invalid",
            "The provider result data does not satisfy the installed operation profile",
            issues=data_issues,
        )
    protocol = payload["data"].get("protocol")
    expected = {
        "request_id": request["request_id"],
        "operation_profile": request["operation_profile"],
        "assertion_profile": request["assertion_profile"],
        "driver_id": resolved.driver_id,
        "driver_version": resolved.driver_version,
    }
    if not isinstance(protocol, Mapping):
        raise ProviderRuntimeError(
            "provider.result.identity_mismatch",
            "Result data.protocol is unavailable for request and provider binding",
        )
    mismatches = [
        f"{name}: expected {value!r}, observed {protocol.get(name)!r}"
        for name, value in expected.items()
        if protocol.get(name) != value
    ]
    if mismatches:
        raise ProviderRuntimeError(
            "provider.result.identity_mismatch",
            "The provider result does not echo the request correlation and provider identity",
            issues=mismatches,
        )

    evidence_issues = _provider_result_evidence_issues(
        payload, request, resolved.operation_profile
    )
    evidence_issues.extend(_request_input_binding_issues(payload, request))
    evidence_issues.extend(
        _request_snapshot_result_issues(payload, resolved.request_inputs)
    )
    evidence_issues.extend(_evidence_destination_issues(payload, request))
    if request["operation_profile"] == "openada.operation/circuit.simulate/v1alpha2":
        evidence_issues.extend(_circuit_result_issues(payload, request))
    if evidence_issues:
        raise ProviderRuntimeError(
            "provider.result.evidence_invalid",
            "The provider result does not satisfy the selected assertion and "
            "request evidence policy",
            issues=evidence_issues[:MAX_VALIDATION_ISSUES],
        )

    profile_limits = resolved.operation_profile["evidence"]["limits"]
    verification_limit = min(
        MAX_PROVIDER_EVIDENCE_BYTES,
        max(profile_limits["max_artifact_bytes"], profile_limits["max_log_bytes"]),
    )
    file_issues = result_conformance_issues(
        payload,
        verify_recorded_files=True,
        max_recorded_file_bytes=verification_limit,
        max_recorded_files=512,
        max_total_recorded_file_bytes=MAX_PROVIDER_EVIDENCE_BYTES,
    )
    if file_issues:
        raise ProviderRuntimeError(
            "provider.result.file_invalid",
            "The provider result's recorded local files could not be verified",
            issues=file_issues,
        )


def invoke_local_provider(
    manifest: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    cwd: str | Path | None = None,
    max_result_bytes: int = MAX_RESULT_BYTES,
    max_stderr_bytes: int = MAX_STDERR_BYTES,
) -> dict[str, Any]:
    """Invoke one resolved provider without a shell and return its validated result."""

    if (
        not isinstance(max_result_bytes, int)
        or isinstance(max_result_bytes, bool)
        or max_result_bytes <= 0
    ):
        raise ValueError("max_result_bytes must be a positive integer")
    if (
        not isinstance(max_stderr_bytes, int)
        or isinstance(max_stderr_bytes, bool)
        or max_stderr_bytes <= 0
    ):
        raise ValueError("max_stderr_bytes must be a positive integer")
    directory = _working_directory(cwd)
    resolved = resolve_local_provider(manifest, request, cwd=directory)
    request_body = json.dumps(
        request,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if len(request_body) > MAX_REQUEST_BYTES:
        raise ProviderRuntimeError(
            "provider.request.over_limit",
            f"Serialized request exceeds the {MAX_REQUEST_BYTES}-byte transport limit",
        )
    evidence_destination, destination_parent_identity = (
        _prepare_evidence_destination(request)
    )

    argv = [resolved.executable, *resolved.argv[1:]]
    for index, path in resolved.bound_argv_files:
        argv[index] = path
    identity_paths = [resolved.executable, *(path for _, path in resolved.bound_argv_files)]
    before_identities = {
        path: _executable_identity(path) for path in identity_paths
    }
    timeout = request["execution_constraints"]["timeout_ms"] / 1000.0
    observation = _run_local_provider_transport(
        argv,
        directory=directory,
        request_body=request_body,
        timeout=timeout,
        max_result_bytes=max_result_bytes,
        max_stderr_bytes=max_stderr_bytes,
        snapshot_indices=[0, *(index for index, _ in resolved.bound_argv_files)],
        expected_identities=before_identities,
    )

    try:
        after_identities = {
            path: _executable_identity(path) for path in identity_paths
        }
    except OSError as exc:
        raise ProviderRuntimeError(
            "provider.transport.identity_changed",
            "A provider entrypoint or bound argv file disappeared after invocation",
            issues=(_bounded_text(exc),),
        ) from exc
    changed_identity_paths = [
        path
        for path in identity_paths
        if before_identities[path] != after_identities[path]
    ]
    if changed_identity_paths:
        raise ProviderRuntimeError(
            "provider.transport.identity_changed",
            "Provider entrypoint or standalone argv file identity changed during invocation",
            issues=tuple(changed_identity_paths),
        )
    _check_evidence_destination_parent(
        evidence_destination, destination_parent_identity
    )
    if observation.stdout_bytes > max_result_bytes:
        raise ProviderRuntimeError(
            "provider.result.over_limit",
            f"Provider stdout exceeded the {max_result_bytes}-byte result limit",
        )
    if observation.stderr_bytes > max_stderr_bytes:
        raise ProviderRuntimeError(
            "provider.transport.stderr_over_limit",
            f"Provider stderr exceeded the {max_stderr_bytes}-byte transport limit",
        )
    if observation.write_errors:
        raise ProviderRuntimeError(
            "provider.transport.failed",
            "The request could not be written completely to provider stdin",
            issues=observation.write_errors,
        )
    if observation.returncode != 0:
        raise ProviderRuntimeError(
            "provider.transport.failed",
            "The provider transport process did not exit zero after delivering its result",
            issues=(f"exit code: {observation.returncode}",),
        )
    if observation.stderr_bytes:
        try:
            stderr_text = observation.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            stderr_text = "provider stderr was not valid UTF-8"
        raise ProviderRuntimeError(
            "provider.transport.stderr",
            "The provider wrote outside the JSON result stream",
            issues=(_bounded_text(stderr_text),),
        )
    payload = _parse_result(observation.stdout)
    validate_provider_result(payload, request, resolved)
    return payload


__all__ = [
    "LOCAL_PROVIDER_SANITIZED_PATH",
    "MANIFEST_SCHEMA_ID",
    "MAX_MANIFEST_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_STDERR_BYTES",
    "ProviderRuntimeError",
    "REQUEST_SCHEMA_ID",
    "RESULT_SCHEMA_ID",
    "ResolvedLocalProvider",
    "invoke_local_provider",
    "list_operation_profiles",
    "load_operation_profile",
    "load_provider_manifest",
    "load_provider_request",
    "provider_manifest_issues",
    "provider_request_issues",
    "resolve_local_provider",
    "validate_provider_manifest",
    "validate_provider_request",
    "validate_provider_result",
]
