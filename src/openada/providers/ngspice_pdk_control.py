"""Evidence-bounded ngspice provider for explicitly configured PDK control decks.

The built-in ``circuit.simulate/v1alpha2`` bridge intentionally accepts only
self-contained model-free decks.  This provider is the separate connection
layer for a small, auditable open-PDK subset: one filesystem testbench, one
hash-bound provider configuration, one PDK identity file, and exactly one OP,
DC, AC, or transient analysis in a closed ngspice control block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from ..contract import (
    FileRecordError,
    diagnostic,
    file_record,
    result,
    stable_regular_file,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..driver_registry import (
    AC_SWEEP_FEATURE,
    CIRCUIT_SIMULATE_PROFILE,
    DC_SWEEP_FEATURE,
    OPERATING_POINT_FEATURE,
    SIMULATION_EVIDENCE_ASSERTION,
    TRANSIENT_FEATURE,
    BuiltinDriver,
)
from ..engines import NgspiceDriver, NgspiceOutput
from ..engines.spice import (
    MAX_CAPTURE_BYTES as NGSPICE_MAX_ARTIFACT_BYTES,
    MAX_LOG_BYTES as NGSPICE_MAX_LOG_BYTES,
)
from ..operations.circuit_simulate import (
    MAX_SOURCE_BYTES,
    circuit_simulation_parameter_issue,
    circuit_simulation_parameters_match,
    decorate_circuit_simulation_result,
    parse_simulation_analysis_line,
)
from ..provider_runtime import ProviderRuntimeError, provider_request_issues


DRIVER_ID = "org.openada.driver.ngspice-pdk-control"
DRIVER_VERSION = "0.5.0"
CONFIG_SCHEMA = "openada.ngspice-provider-config/v0alpha1"
MAX_REQUEST_BYTES = 1 * 1024 * 1024
MAX_RESULT_BYTES = 5 * 1024 * 1024
MAX_CONFIG_BYTES = 1 * 1024 * 1024
MAX_NATIVE_EXECUTABLE_BYTES = 512 * 1024 * 1024
NATIVE_NGSPICE_CANDIDATES = (
    "/foss/tools/ngspice/bin/ngspice",
    "/usr/local/bin/ngspice",
    "/usr/bin/ngspice",
    "/bin/ngspice",
)
MAX_LINE_BYTES = 65_536
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$"
)
_PDK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONFIGURATION_ROLES = frozenset({"pdk", "simulator-configuration"})
_PROVIDER_ARTIFACT_ROLES = frozenset(
    {"simulation.result", "simulation.log", "simulation.launcher"}
)
_ANALYSIS_FEATURES = {
    "op": OPERATING_POINT_FEATURE,
    "dc": DC_SWEEP_FEATURE,
    "ac": AC_SWEEP_FEATURE,
    "tran": TRANSIENT_FEATURE,
}


@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    path: Path
    signature: tuple[int, int, int]

_BINDING = BuiltinDriver(
    alias="ngspice",
    driver_id=DRIVER_ID,
    version=DRIVER_VERSION,
    native_tool="ngspice",
    operation_profile=CIRCUIT_SIMULATE_PROFILE,
    assertion_profile=SIMULATION_EVIDENCE_ASSERTION,
    features=(
        OPERATING_POINT_FEATURE,
        DC_SWEEP_FEATURE,
        AC_SWEEP_FEATURE,
        TRANSIENT_FEATURE,
    ),
    factory=lambda discovery: NgspiceDriver(discovery=discovery),
)


class ProviderInputError(ValueError):
    """A provider-specific request issue that can be returned as unknown."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON object key {key!r}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _strict_json(body: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} is not strict JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} must contain one JSON object."
        )
    return parsed


def _read_request() -> dict[str, Any]:
    body = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(body) > MAX_REQUEST_BYTES:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"Provider request exceeds {MAX_REQUEST_BYTES} bytes.",
        )
    return _strict_json(body, label="Provider request")


def _directory_signature(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _bind_canonical_directory(path: Path, *, label: str) -> _DirectoryBinding:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} is unavailable: {exc}"
        ) from exc
    if (
        not path.is_absolute()
        or resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            f"{label} must be one canonical non-linked directory.",
        )
    return _DirectoryBinding(path=path, signature=_directory_signature(metadata))


def _directory_binding_is_stable(binding: _DirectoryBinding) -> bool:
    try:
        current = binding.path.lstat()
        return (
            binding.path.resolve(strict=True) == binding.path
            and stat.S_ISDIR(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and _directory_signature(current) == binding.signature
        )
    except OSError:
        return False


def _canonical_file(path_value: object, *, label: str) -> Path:
    if (
        not isinstance(path_value, str)
        or not path_value
        or len(path_value) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in path_value)
    ):
        raise ProviderInputError(
            "simulation.request.invalid", f"{label}.path must be bounded text."
        )
    path = Path(path_value)
    if not path.is_absolute() or os.path.abspath(path_value) != path_value:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"{label}.path must be canonical and absolute.",
        )
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} is unavailable: {exc}"
        ) from exc
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            f"{label} must be one canonical non-linked regular file.",
        )
    return path


def _read_stable_file(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    try:
        with stable_regular_file(path) as (handle, opened):
            if opened.st_size > maximum_bytes:
                raise ProviderInputError(
                    "simulation.evidence.over_limit",
                    f"{label} exceeds {maximum_bytes} bytes.",
                )
            body = handle.read(maximum_bytes + 1)
    except FileRecordError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} is unstable: {exc}"
        ) from exc
    if len(body) > maximum_bytes:
        raise ProviderInputError(
            "simulation.evidence.over_limit", f"{label} exceeds {maximum_bytes} bytes."
        )
    return body


def _bound_file(
    value: object,
    *,
    label: str,
    kind: str,
    maximum_bytes: int = MAX_SOURCE_BYTES,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"{label} must contain exactly path and sha256.",
        )
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label}.sha256 is invalid."
        )
    path = _canonical_file(value.get("path"), label=label)
    try:
        record = file_record(
            path,
            kind=kind,
            role="configuration",
            maximum_bytes=maximum_bytes,
        )
    except FileRecordError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"{label} cannot be bound: {exc}"
        ) from exc
    if not record["exists"] or record.get("sha256") != digest:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"{label} content does not match its declared SHA-256.",
        )
    return path, record


def _provider_native_executable() -> tuple[Path, dict[str, Any]]:
    """Resolve ngspice only from provider-owned absolute locations."""

    rejected: list[str] = []
    for candidate_value in NATIVE_NGSPICE_CANDIDATES:
        candidate = Path(candidate_value)
        try:
            path = _canonical_file(candidate_value, label="Provider-owned ngspice")
            metadata = path.lstat()
            if not os.access(path, os.X_OK):
                raise OSError("not executable")
            body = _read_stable_file(
                path,
                label="Provider-owned ngspice",
                maximum_bytes=MAX_NATIVE_EXECUTABLE_BYTES,
            )
            if not body.startswith(b"\x7fELF"):
                raise OSError("not a native ELF executable")
            record = file_record(
                path,
                kind="eda-executable",
                role="configuration",
                maximum_bytes=MAX_NATIVE_EXECUTABLE_BYTES,
            )
            if (
                record.get("bytes") != metadata.st_size
                or record.get("sha256") != hashlib.sha256(body).hexdigest()
            ):
                raise OSError("identity changed while binding")
            return path, record
        except (OSError, ProviderInputError, FileRecordError) as exc:
            rejected.append(f"{candidate}: {exc}")
    raise ProviderInputError(
        "simulation.request.invalid",
        "No provider-owned native ELF ngspice executable is available at the "
        "fixed search locations (" + "; ".join(rejected) + ").",
    )


def _snapshot_native_executable(
    source: Path,
    destination: Path,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a private non-writable executable snapshot before native launch."""

    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        source_fd = os.open(source, read_flags)
        try:
            opened = os.fstat(source_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != expected.get("bytes")
            ):
                raise OSError("source identity changed before snapshot")
            destination_fd = os.open(destination, write_flags, 0o500)
            digest = hashlib.sha256()
            total = 0
            prefix = bytearray()
            try:
                while block := os.read(source_fd, 1024 * 1024):
                    total += len(block)
                    if total > MAX_NATIVE_EXECUTABLE_BYTES:
                        raise OSError("native executable exceeded snapshot ceiling")
                    if len(prefix) < 4:
                        prefix.extend(block[: 4 - len(prefix)])
                    digest.update(block)
                    position = 0
                    while position < len(block):
                        written = os.write(destination_fd, block[position:])
                        if written <= 0:
                            raise OSError("short executable snapshot write")
                        position += written
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            finished = os.fstat(source_fd)
        finally:
            os.close(source_fd)
        path_after = source.stat()
        if (
            total != expected.get("bytes")
            or digest.hexdigest() != expected.get("sha256")
            or bytes(prefix) != b"\x7fELF"
            or (
                finished.st_dev,
                finished.st_ino,
                finished.st_mode,
                finished.st_size,
                finished.st_mtime_ns,
                finished.st_ctime_ns,
            )
            != (
                path_after.st_dev,
                path_after.st_ino,
                path_after.st_mode,
                path_after.st_size,
                path_after.st_mtime_ns,
                path_after.st_ctime_ns,
            )
        ):
            raise OSError("native executable changed while snapshotting")
        snapshot = file_record(
            destination,
            kind="eda-executable-snapshot",
            role="configuration",
            maximum_bytes=MAX_NATIVE_EXECUTABLE_BYTES,
        )
        if snapshot.get("sha256") != expected.get("sha256"):
            raise OSError("private executable snapshot digest differs")
        return snapshot
    except (OSError, FileRecordError) as exc:
        try:
            destination.unlink()
        except OSError:
            pass
        raise ProviderInputError(
            "simulation.request.invalid",
            f"Provider-owned ngspice could not be snapshotted safely: {exc}",
        ) from exc


def _validate_connection_request(request: Mapping[str, Any]) -> None:
    """Validate every execution-affecting field before touching the destination."""

    try:
        issues = provider_request_issues(request)
    except ProviderRuntimeError as exc:
        detail = "; ".join(exc.issues) if exc.issues else str(exc)
        raise ProviderInputError("simulation.request.invalid", detail) from exc
    if issues:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Request contract violation: " + "; ".join(issues[:8]),
        )

    selector = request.get("driver_selector")
    if (
        not isinstance(selector, Mapping)
        or selector.get("driver_id") != DRIVER_ID
        or selector.get("driver_version") != DRIVER_VERSION
        or selector.get("transport_id") != "local-json-stdio"
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "The request must select this exact provider version and local-json-stdio transport.",
        )
    unsupported_features = set(selector.get("required_features", ())) - set(
        _ANALYSIS_FEATURES.values()
    )
    if unsupported_features:
        raise ProviderInputError(
            "simulation.analysis.unsupported",
            "The request requires unsupported provider feature(s): "
            + ", ".join(sorted(unsupported_features)),
        )

    target_locator = request["target"]["locator"]
    if "sha256" not in target_locator:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The simulation target requires an explicit content digest.",
        )

    configuration = request["configuration"]
    observed_roles = [item["role"] for item in configuration]
    if len(observed_roles) != 2 or set(observed_roles) != _CONFIGURATION_ROLES:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider configuration must contain exactly one pdk and one "
            "simulator-configuration reference.",
        )
    for item in configuration:
        if item["required"] is not True or "sha256" not in item["locator"]:
            raise ProviderInputError(
                "simulation.request.invalid",
                f"Configuration role {item['role']!r} must be required and content-digested.",
            )

    evidence_policy = request["evidence_policy"]
    if (
        evidence_policy["retain_native_artifacts"] is not True
        or evidence_policy["retain_native_logs"] is not True
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "This evidence-only provider requires native artifacts and logs to be retained.",
        )
    unsupported_roles = (
        set(evidence_policy["required_artifact_roles"]) - _PROVIDER_ARTIFACT_ROLES
    )
    if unsupported_roles:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The provider cannot produce required artifact role(s): "
            + ", ".join(sorted(unsupported_roles)),
        )
    if evidence_policy["provenance"] != "bounded":
        raise ProviderInputError(
            "simulation.request.invalid",
            "The provider cannot claim complete provenance for transitive PDK model access.",
        )
    if evidence_policy["identity_requirement"] != "content-digest":
        raise ProviderInputError(
            "simulation.request.invalid",
            "The provider requires content-digest evidence identity.",
        )

    constraints = request["execution_constraints"]
    if constraints["completion"] != "wait":
        raise ProviderInputError(
            "simulation.request.invalid", "The provider supports completion='wait' only."
        )
    if constraints["side_effects"] != "evidence-only":
        raise ProviderInputError(
            "simulation.request.invalid",
            "The provider requires side_effects='evidence-only'.",
        )
    if constraints["max_log_bytes"] < NGSPICE_MAX_LOG_BYTES:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"The provider requires max_log_bytes >= {NGSPICE_MAX_LOG_BYTES}.",
        )
    if constraints["max_artifact_bytes"] < NGSPICE_MAX_ARTIFACT_BYTES:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"The provider requires max_artifact_bytes >= {NGSPICE_MAX_ARTIFACT_BYTES}.",
        )
    if request["evidence_destination"]["collision_policy"] != "fail-if-present":
        raise ProviderInputError(
            "simulation.request.invalid",
            "The provider requires collision_policy='fail-if-present'.",
        )


def _configuration_reference(
    request: Mapping[str, Any], role: str
) -> Mapping[str, Any]:
    configuration = request.get("configuration")
    if not isinstance(configuration, list):
        raise ProviderInputError(
            "simulation.request.invalid", "configuration must be an array."
        )
    matches = [item for item in configuration if isinstance(item, Mapping) and item.get("role") == role]
    if len(matches) != 1:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"The ngspice PDK provider requires exactly one {role!r} configuration.",
        )
    reference = matches[0]
    locator = reference.get("locator")
    if not isinstance(locator, Mapping) or locator.get("type") != "filesystem":
        raise ProviderInputError(
            "simulation.request.invalid",
            f"The {role!r} configuration must use a filesystem locator.",
        )
    return locator


def _load_provider_configuration(
    request: Mapping[str, Any],
) -> tuple[
    Path,
    Path,
    Path,
    Path,
    dict[str, str],
    list[dict[str, Any]],
    tuple[_DirectoryBinding, _DirectoryBinding],
]:
    locator = _configuration_reference(request, "simulator-configuration")
    config_path = _canonical_file(locator.get("path"), label="Provider configuration")
    body = _read_stable_file(
        config_path, label="Provider configuration", maximum_bytes=MAX_CONFIG_BYTES
    )
    locator_digest = locator.get("sha256")
    observed_digest = hashlib.sha256(body).hexdigest()
    if locator_digest != observed_digest:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider configuration does not match its request locator SHA-256.",
        )
    document = _strict_json(body, label="Provider configuration")
    required = {
        "schema",
        "init_file",
        "system_init_file",
        "environment",
        "extensions",
    }
    if set(document) != required or document.get("schema") != CONFIG_SCHEMA:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"Provider configuration must be one closed {CONFIG_SCHEMA} object.",
        )
    if document.get("extensions") != {}:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider configuration extensions are not implemented in v0alpha1.",
        )
    environment = document.get("environment")
    if not isinstance(environment, Mapping) or set(environment) != {"PDK", "PDK_ROOT"}:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider configuration environment must contain exactly PDK and PDK_ROOT.",
        )
    pdk_name = environment.get("PDK")
    pdk_root_value = environment.get("PDK_ROOT")
    if (
        not isinstance(pdk_name, str)
        or _PDK_NAME_RE.fullmatch(pdk_name) is None
        or not isinstance(pdk_root_value, str)
        or not pdk_root_value
        or len(pdk_root_value) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in pdk_root_value)
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider PDK environment values are invalid.",
        )
    pdk_root = Path(pdk_root_value)
    try:
        pdk_root_metadata = pdk_root.lstat()
        pdk_root_resolved = pdk_root.resolve(strict=True)
    except OSError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"PDK_ROOT is unavailable: {exc}"
        ) from exc
    if (
        not pdk_root.is_absolute()
        or os.path.abspath(pdk_root_value) != pdk_root_value
        or pdk_root_resolved != pdk_root
        or stat.S_ISLNK(pdk_root_metadata.st_mode)
        or not stat.S_ISDIR(pdk_root_metadata.st_mode)
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "PDK_ROOT must be one canonical non-linked directory.",
        )
    pdk_root_binding = _bind_canonical_directory(pdk_root, label="PDK_ROOT")
    executable_path, executable_record = _provider_native_executable()
    init_path, init_record = _bound_file(
        document["init_file"], label="init_file", kind="ngspice-init"
    )
    system_path, system_record = _bound_file(
        document["system_init_file"],
        label="system_init_file",
        kind="ngspice-system-init",
    )
    if system_path.name != "spinit":
        raise ProviderInputError(
            "simulation.request.invalid", "system_init_file must be named 'spinit'."
        )
    try:
        config_record = file_record(
            config_path,
            kind="openada-ngspice-provider-config",
            role="configuration",
            maximum_bytes=MAX_CONFIG_BYTES,
        )
    except FileRecordError as exc:
        raise ProviderInputError(
            "simulation.request.invalid",
            f"Provider configuration cannot be rebound: {exc}",
        ) from exc
    if config_record.get("sha256") != observed_digest:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Provider configuration changed after it was parsed.",
        )

    pdk_locator = _configuration_reference(request, "pdk")
    pdk_path = _canonical_file(pdk_locator.get("path"), label="PDK identity")
    pdk_digest = pdk_locator.get("sha256")
    try:
        pdk_record = file_record(
            pdk_path,
            kind="pdk-revision",
            role="configuration",
            maximum_bytes=MAX_CONFIG_BYTES,
        )
    except FileRecordError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"PDK identity cannot be bound: {exc}"
        ) from exc
    if (
        not isinstance(pdk_digest, str)
        or _SHA256_RE.fullmatch(pdk_digest) is None
        or pdk_record.get("sha256") != pdk_digest
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "PDK identity does not match its request locator SHA-256.",
        )
    selected_pdk = pdk_root / pdk_name
    try:
        selected_metadata = selected_pdk.lstat()
        selected_resolved = selected_pdk.resolve(strict=True)
        init_path.relative_to(selected_pdk)
    except (OSError, ValueError) as exc:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The selected PDK directory or its explicit init file is inconsistent.",
        ) from exc
    if (
        selected_resolved != selected_pdk
        or stat.S_ISLNK(selected_metadata.st_mode)
        or not stat.S_ISDIR(selected_metadata.st_mode)
        or pdk_path != selected_pdk / "COMMIT"
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "PDK identity must be the selected canonical PDK_ROOT/PDK/COMMIT file.",
        )
    selected_pdk_binding = _bind_canonical_directory(
        selected_pdk, label="Selected PDK directory"
    )
    return executable_path, init_path, system_path, pdk_path, {
        "PDK": pdk_name,
        "PDK_ROOT": pdk_root_value,
    }, [
        config_record,
        executable_record,
        init_record,
        system_record,
        pdk_record,
    ], (pdk_root_binding, selected_pdk_binding)


def _parse_control_deck(path: Path) -> tuple[dict[str, object], str]:
    body = _read_stable_file(path, label="Simulation target", maximum_bytes=MAX_SOURCE_BYTES)
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", "Simulation target must be UTF-8 text."
        ) from exc

    inside = False
    control_count = 0
    end_count = 0
    state = "expect-save"
    analysis: dict[str, object] | None = None
    output: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ProviderInputError(
                "simulation.evidence.over_limit",
                f"Simulation target line {line_number} exceeds {MAX_LINE_BYTES} bytes.",
            )
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        lowered = stripped.casefold()
        if lowered == ".control":
            if inside:
                raise ProviderInputError(
                    "simulation.request.invalid", "Nested .control blocks are forbidden."
                )
            inside = True
            control_count += 1
            continue
        if lowered == ".endc":
            if not inside:
                raise ProviderInputError(
                    "simulation.request.invalid", "An unmatched .endc was found."
                )
            inside = False
            end_count += 1
            continue
        if not inside:
            continue

        tokens = stripped.split()
        command = tokens[0].casefold()
        if (
            command == "save"
            and len(tokens) == 2
            and tokens[1].casefold() == "all"
            and state == "expect-save"
        ):
            state = "expect-analysis"
        elif command in _ANALYSIS_FEATURES and state == "expect-analysis":
            analysis = parse_simulation_analysis_line(command, "." + stripped)
            if analysis is None:
                raise ProviderInputError(
                    "simulation.request.invalid",
                    f"The control {command.upper()} analysis directive is invalid.",
                )
            state = "expect-linearize-or-write"
        elif command == "linearize" and len(tokens) == 1:
            analysis_type = (
                analysis.get("analysis", {}).get("type")
                if isinstance(analysis, Mapping)
                and isinstance(analysis.get("analysis"), Mapping)
                else None
            )
            if state != "expect-linearize-or-write" or analysis_type != "tran":
                raise ProviderInputError(
                    "simulation.request.invalid",
                    "The exact 'linearize' command is allowed only once after TRAN and before write.",
                )
            state = "expect-write"
        elif command == "write" and len(tokens) == 2 and state in {
            "expect-linearize-or-write",
            "expect-write",
        }:
            if _OUTPUT_RE.fullmatch(tokens[1]) is None:
                raise ProviderInputError(
                    "simulation.request.invalid",
                    "Exactly one control-safe raw write filename is allowed.",
                )
            output = tokens[1]
            state = "complete"
        else:
            raise ProviderInputError(
                "simulation.request.invalid",
                "Unsupported ngspice control command, or repeated/out-of-order "
                f"closed command, on line {line_number}: {command!r}.",
            )

    if inside or control_count != 1 or end_count != 1:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The target must contain exactly one complete .control/.endc block.",
        )
    if state != "complete" or analysis is None or output is None:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The control block must contain, in order, exactly 'save all', one OP, DC, AC, or TRAN analysis, optional exact 'linearize' for TRAN only, and one raw write.",
        )
    return analysis, output


def _request_target(request: Mapping[str, Any]) -> Path:
    target = request.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "testbench":
        raise ProviderInputError(
            "simulation.request.invalid", "target.kind must be 'testbench'."
        )
    locator = target.get("locator")
    if not isinstance(locator, Mapping) or locator.get("type") != "filesystem":
        raise ProviderInputError(
            "simulation.request.invalid", "The target must use a filesystem locator."
        )
    path = _canonical_file(locator.get("path"), label="Simulation target")
    declared_digest = locator.get("sha256")
    try:
        record = file_record(
            path, kind="spice-netlist", role="input", maximum_bytes=MAX_SOURCE_BYTES
        )
    except FileRecordError as exc:
        raise ProviderInputError(
            "simulation.request.invalid", f"Simulation target cannot be bound: {exc}"
        ) from exc
    if declared_digest is not None and record.get("sha256") != declared_digest:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Simulation target does not match its request locator SHA-256.",
        )
    return path


def _request_parameters(request: Mapping[str, Any]) -> dict[str, object]:
    parameters = request.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ProviderInputError(
            "simulation.request.invalid", "parameters must be an object."
        )
    issue = circuit_simulation_parameter_issue(parameters)
    if issue is not None:
        raise ProviderInputError("simulation.request.invalid", issue)
    normalized = json.loads(json.dumps(parameters, allow_nan=False))
    if normalized.get("analysis", {}).get("type") not in _ANALYSIS_FEATURES:
        raise ProviderInputError(
            "simulation.analysis.unsupported",
            "This provider capability implements exactly OP, DC, AC, and transient analysis.",
        )
    return normalized


def _destination(request: Mapping[str, Any]) -> tuple[Path, _DirectoryBinding]:
    destination = request.get("evidence_destination")
    locator = destination.get("locator") if isinstance(destination, Mapping) else None
    if not isinstance(locator, Mapping) or locator.get("type") != "filesystem":
        raise ProviderInputError(
            "simulation.request.invalid",
            "evidence_destination must use a filesystem locator.",
        )
    raw_path = locator.get("path")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or len(raw_path) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
    ):
        raise ProviderInputError(
            "simulation.request.invalid", "Evidence destination path is invalid."
        )
    path = Path(raw_path)
    if not path.is_absolute() or os.path.abspath(raw_path) != raw_path:
        raise ProviderInputError(
            "simulation.request.invalid",
            "Evidence destination must be canonical and absolute.",
        )
    if path.exists() or path.is_symlink():
        raise ProviderInputError(
            "simulation.result.stale", "Evidence destination already exists."
        )
    parent_binding = _bind_canonical_directory(
        path.parent, label="Evidence destination parent"
    )
    return path, parent_binding


def _create_destination(path: Path, parent_binding: _DirectoryBinding) -> _DirectoryBinding:
    """Create one fresh destination relative to the already-bound parent."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(parent_binding.path, flags)
    except OSError as exc:
        raise ProviderInputError(
            "simulation.result.stale",
            f"Evidence destination parent could not be reopened safely: {exc}",
        ) from exc
    try:
        opened_parent = os.fstat(parent_fd)
        if _directory_signature(opened_parent) != parent_binding.signature:
            raise ProviderInputError(
                "simulation.result.stale",
                "Evidence destination parent changed before creation.",
            )
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ProviderInputError(
                "simulation.result.stale", "Evidence destination already exists."
            )
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
    except OSError as exc:
        raise ProviderInputError(
            "simulation.result.stale",
            f"Evidence destination could not be created safely: {exc}",
        ) from exc
    finally:
        os.close(parent_fd)
    if not _directory_binding_is_stable(parent_binding):
        raise ProviderInputError(
            "simulation.result.stale",
            "Evidence destination parent changed during creation.",
        )
    return _bind_canonical_directory(path, label="Evidence destination")


def _request_input_records(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    references: list[tuple[Mapping[str, Any], str]] = []
    target = request.get("target")
    if isinstance(target, Mapping) and isinstance(target.get("locator"), Mapping):
        references.append((target["locator"], "spice-netlist"))
    configuration = request.get("configuration")
    if isinstance(configuration, list):
        for item in configuration:
            if isinstance(item, Mapping) and isinstance(item.get("locator"), Mapping):
                references.append((item["locator"], f"{item.get('role', 'configuration')}"))
    for locator, kind in references:
        if locator.get("type") != "filesystem" or not isinstance(locator.get("path"), str):
            continue
        try:
            maximum_bytes = (
                MAX_SOURCE_BYTES if kind == "spice-netlist" else MAX_CONFIG_BYTES
            )
            record = file_record(
                locator["path"],
                kind=kind,
                role="configuration",
                maximum_bytes=maximum_bytes,
            )
        except FileRecordError:
            continue
        if record["path"] not in {item["path"] for item in records}:
            records.append(record)
    return records


def _unknown_result(
    request: Mapping[str, Any], exc: ProviderInputError
) -> dict[str, Any]:
    request_id = request.get("request_id")
    if not isinstance(request_id, str):
        raise exc
    parameters = request.get("parameters")
    normalized_parameters = dict(parameters) if isinstance(parameters, Mapping) else None
    payload = result(
        "simulate",
        tool=tool_record("ngspice", path=None, version=None),
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The ngspice PDK provider rejected its closed connection request.",
        inputs=_request_input_records(request),
        diagnostics=[diagnostic("error", exc.code, str(exc))],
        data={"inputs_stable": True},
    )
    return decorate_circuit_simulation_result(
        payload,
        driver=_BINDING,
        request_id=request_id,
        deck={},
        parameters=normalized_parameters,
        provenance_limitations=[
            "Native simulation was not launched because provider-specific request validation failed."
        ],
    )


def invoke(request: Mapping[str, Any]) -> dict[str, Any]:
    """Invoke the closed local provider and return one typed result envelope."""

    if not isinstance(request, Mapping):
        raise ProviderInputError(
            "simulation.request.invalid", "The provider request must be an object."
        )
    _validate_connection_request(request)
    if request.get("operation_profile") != CIRCUIT_SIMULATE_PROFILE or request.get(
        "assertion_profile"
    ) != SIMULATION_EVIDENCE_ASSERTION:
        raise ProviderInputError(
            "simulation.request.invalid",
            "The request does not select circuit.simulate/v1alpha2 and its evidence assertion.",
        )
    selector = request.get("driver_selector")
    if (
        not isinstance(selector, Mapping)
        or selector.get("driver_id") != DRIVER_ID
        or selector.get("driver_version") != DRIVER_VERSION
    ):
        raise ProviderInputError(
            "simulation.request.invalid", "The request selected a different provider identity."
        )
    target = _request_target(request)
    parameters = _request_parameters(request)
    observed_parameters, output_name = _parse_control_deck(target)
    if not circuit_simulation_parameters_match(parameters, observed_parameters):
        raise ProviderInputError(
            "simulation.request.invalid",
            "The request analysis parameters do not exactly match the target control analysis.",
        )
    analysis_type = parameters["analysis"]["type"]
    required_features = selector.get("required_features")
    expected_feature = _ANALYSIS_FEATURES[str(analysis_type)]
    if (
        not isinstance(required_features, list)
        or len(required_features) != 1
        or required_features[0] != expected_feature
    ):
        raise ProviderInputError(
            "simulation.request.invalid",
            "driver_selector.required_features must contain exactly the feature "
            f"for the authoritative {str(analysis_type).upper()} analysis: "
            f"{expected_feature}.",
        )
    (
        executable_path,
        init_path,
        system_path,
        _pdk_path,
        environment,
        configuration_records,
        pdk_directory_bindings,
    ) = _load_provider_configuration(request)
    destination, destination_parent_binding = _destination(request)

    timeout_ms = request.get("execution_constraints", {}).get("timeout_ms")
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or timeout_ms <= 0
    ):
        raise ProviderInputError(
            "simulation.request.invalid", "execution timeout_ms must be positive."
        )

    destination_binding = _create_destination(
        destination, destination_parent_binding
    )
    workdir = destination / "work"
    output_dir = destination / "simulation"
    workdir.mkdir()
    executable_snapshot_path = workdir / "openada-native-ngspice"
    executable_snapshot_record = _snapshot_native_executable(
        executable_path,
        executable_snapshot_path,
        next(
            record
            for record in configuration_records
            if record.get("kind") == "eda-executable"
        ),
    )
    configuration_records.append(executable_snapshot_record)

    native = NgspiceDriver(str(executable_snapshot_path)).simulate(
        target,
        output_dir,
        workdir=workdir,
        execution_mode="control",
        expected_outputs=[NgspiceOutput(kind="raw", path=output_name)],
        init_file=init_path,
        system_init_file=system_path,
        environment_overrides=environment,
        environment_mode="sanitized",
        timeout=timeout_ms / 1_000.0,
    )

    existing_paths = {item["path"] for item in native.get("inputs", [])}
    stable = True
    expected_target_digest = request["target"]["locator"]["sha256"]
    target_records = [
        item
        for item in native.get("inputs", [])
        if item.get("path") == str(target)
    ]
    if (
        len(target_records) != 1
        or target_records[0].get("sha256") != expected_target_digest
    ):
        stable = False
    for original in configuration_records:
        try:
            maximum_bytes = (
                MAX_NATIVE_EXECUTABLE_BYTES
                if str(original["kind"]).startswith("eda-executable")
                else MAX_CONFIG_BYTES
            )
            current = file_record(
                original["path"],
                kind=original["kind"],
                role="configuration",
                maximum_bytes=maximum_bytes,
            )
        except FileRecordError:
            stable = False
            continue
        if any(current.get(name) != original.get(name) for name in ("exists", "bytes", "sha256")):
            stable = False
        if current["path"] not in existing_paths:
            native.setdefault("inputs", []).append(current)
            existing_paths.add(current["path"])
    directory_bindings = (
        *pdk_directory_bindings,
        destination_parent_binding,
        destination_binding,
    )
    if not all(_directory_binding_is_stable(item) for item in directory_bindings):
        stable = False
    if not stable:
        native["engineering"] = {
            "status": "unknown",
            "summary": "A provider configuration input changed during native execution.",
        }
        native.setdefault("diagnostics", []).append(
            diagnostic(
                "error",
                "input.changed",
                "A hash-bound executable/configuration file, PDK path, or evidence "
                "destination identity changed during execution.",
            )
        )
        native.setdefault("data", {})["inputs_stable"] = False

    request_id = request.get("request_id")
    if not isinstance(request_id, str):
        raise ProviderInputError(
            "simulation.request.invalid", "request_id must be text."
        )
    return decorate_circuit_simulation_result(
        native,
        driver=_BINDING,
        request_id=request_id,
        deck={},
        parameters=parameters,
        provenance_limitations=[
            "The target deck, provider configuration, explicit ngspice startup files, provider-owned native executable and its private launch snapshot, PDK revision label, and retained native evidence are content-bound.",
            "Model files reached transitively through the PDK startup search path are not enumerated or proven clean; the recorded COMMIT file is an identity label, not a complete PDK tree attestation.",
            "Native shared libraries, kernel state, and container runtime remain bounded rather than complete provenance.",
        ],
    )


def main() -> int:
    request: dict[str, Any] | None = None
    try:
        request = _read_request()
        payload = invoke(request)
    except ProviderInputError as exc:
        if request is None:
            sys.stderr.write(f"{exc.code}: {exc}\n")
            return 2
        try:
            payload = _unknown_result(request, exc)
        except (ProviderInputError, KeyError, TypeError, ValueError) as nested:
            sys.stderr.write(f"{exc.code}: {nested}\n")
            return 2
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        sys.stderr.write(f"simulation.result.invalid: {exc}\n")
        return 2
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        sys.stderr.write(
            f"simulation.result.over_limit: provider result exceeds {MAX_RESULT_BYTES} bytes\n"
        )
        return 2
    # Serialize completely before the first stdout write so a malformed result
    # cannot leave a partial JSON document on the transport stream.
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = [
    "CONFIG_SCHEMA",
    "DRIVER_ID",
    "DRIVER_VERSION",
    "ProviderInputError",
    "invoke",
    "main",
]
