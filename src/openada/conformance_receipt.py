"""Verify scheduler-sealed evidence chains without trusting caller-authored JSON.

The ordinary OpenADA result contract proves structure and content linkage.  It
does not prove who produced an envelope: an agent can recompute every digest in
a self-consistent fabricated chain.  This module deliberately exposes only a
verifier.  A privileged scheduler is responsible for capturing the evidence
bytes and sealing them with a private key that is unavailable to the job.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from .conformance import ResultConformanceError, assert_result_conforms
from .pdk_bindings import (
    MAX_PDK_FILE_BYTES,
    MAX_PDK_SNAPSHOT_BYTES,
)
from .operations import extract_result_series, measure_result
from .operations.result_series_extract import MAX_SIMULATION_INPUT_RECORDS


RECEIPT_SCHEMA = "openada.conformance-receipt/v0alpha1"
SIGNATURE_DOMAIN = b"openada.conformance-receipt/v0alpha1\0"
# A complete PDK-bound simulation can carry the extractor's maximum input
# roster plus the bounded artifact roster.  The signed receipt repeats those
# exact records, including their absolute paths, so the former 64 KiB / 128
# record limits made a real 319-file Sky130 snapshot impossible to seal.
MAX_SIMULATION_FILE_RECORDS = MAX_SIMULATION_INPUT_RECORDS + 64
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_JSON_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_RAW_EVIDENCE_BYTES = 256 * 1024 * 1024
MAX_SIMULATION_FILE_BYTES = MAX_PDK_FILE_BYTES
# The PDK closure alone may legally reach MAX_PDK_SNAPSHOT_BYTES.  Reserve the
# independently bounded native artifact plus the five primary JSON records and
# three auxiliary records (snapshot manifest, deck, and log) without silently
# reopening a lower aggregate ceiling in the receipt verifier.
MAX_TOTAL_EVIDENCE_BYTES = (
    MAX_PDK_SNAPSHOT_BYTES
    + MAX_RAW_EVIDENCE_BYTES
    + 8 * MAX_JSON_EVIDENCE_BYTES
)
MAX_JSON_STRUCTURE_DEPTH = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
_EVIDENCE_ROLES = (
    "simulation_envelope",
    "simulation_artifact",
    "selection_request",
    "extraction_envelope",
    "measurement_request",
    "measurement_envelope",
)
_SIMULATION_FILE_FIELDS = {
    "section",
    "index",
    "kind",
    "role",
    "path",
    "bytes",
    "sha256",
}
_CONFIGURATION_ROLE_BY_INPUT_ROLE = {
    "model-library": "spice-model-library",
    # One content-addressed snapshot manifest aggregates the complete active
    # include/lib closure. Individual corner, transitive, and parser-only
    # collateral remain exact simulation inputs, but are not duplicated as
    # hundreds of configuration references.
    "pdk.snapshot": "pdk",
    "pdk.osdi-module": "simulator-configuration",
    "pdk.identity": "pdk",
}


class ConformanceReceiptError(ValueError):
    """A signed evidence receipt or its exact evidence chain was refused."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class VerifiedTypedEvidence:
    """Immutable handoff from verification; no evidence path must be reopened."""

    job_instance: str
    subject_sha256: str
    simulation_context_sha256: str
    selection_request_sha256: str
    measurement_request_sha256: str
    measurement_envelope_sha256: str
    measurement_envelope_bytes: bytes
    measurement_id: str
    value: float
    unit: str


def _fail(code: str, message: str) -> None:
    raise ConformanceReceiptError(code, message)


def _closed(
    value: object,
    label: str,
    *,
    required: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("receipt.structure.invalid", f"{label} must be an object")
    keys = set(value)
    if any(not isinstance(key, str) for key in value):
        _fail(
            "receipt.structure.invalid",
            f"{label} field names must all be strings",
        )
    missing = required - keys
    extra = keys - required
    if missing:
        _fail(
            "receipt.structure.invalid",
            f"{label} is missing fields: {', '.join(sorted(missing))}",
        )
    if extra:
        _fail(
            "receipt.structure.invalid",
            f"{label} contains undeclared fields: {', '.join(sorted(extra))}",
        )
    return value


def _strict_json(body: bytes, label: str) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(
                    "receipt.json.invalid",
                    f"{label} contains duplicate field {key!r}",
                )
            result[key] = value
        return result

    def nonfinite(token: str) -> None:
        _fail(
            "receipt.json.invalid",
            f"{label} contains non-finite number {token}",
        )

    def integer(token: str) -> int:
        if len(token.lstrip("-")) > 1_000:
            _fail("receipt.json.invalid", f"{label} contains an oversized integer")
        return int(token)

    def floating(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            nonfinite(token)
        return value

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
            parse_int=integer,
            parse_float=floating,
        )
    except ConformanceReceiptError:
        raise
    except (RecursionError, UnicodeError, ValueError) as exc:
        _fail("receipt.json.invalid", f"{label} is not strict UTF-8 JSON: {exc}")
    if not isinstance(value, Mapping):
        _fail("receipt.json.invalid", f"{label} must contain a JSON object")
    pending: list[tuple[object, int]] = [(value, 1)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if visited > 1_000_000 or depth > MAX_JSON_STRUCTURE_DEPTH:
            _fail(
                "receipt.json.invalid",
                f"{label} exceeds the JSON structure bound",
            )
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


class _StableCapture:
    def __init__(
        self,
        path: str | Path,
        *,
        limit: int,
        label: str,
        directory_identities: dict[str, tuple[int, int, int]] | None = None,
        file_identities: dict[str, tuple[int, ...]] | None = None,
    ) -> None:
        try:
            supplied = os.fspath(path)
            if (
                not isinstance(supplied, str)
                or not supplied
                or any(ord(character) < 32 for character in supplied)
            ):
                raise ValueError("path contains invalid text")
            lexical = Path(os.path.abspath(supplied))
        except (OSError, TypeError, ValueError) as exc:
            _fail("receipt.file.invalid", f"{label} path is invalid: {exc}")
        if not Path(supplied).is_absolute() or os.path.normpath(supplied) != supplied:
            _fail(
                "receipt.file.invalid",
                f"{label} path must be lexical, normalized, and absolute",
            )
        if len(lexical.parts) > 64:
            _fail(
                "receipt.file.invalid",
                f"{label} path has too many components",
            )
        self.path = lexical
        self.label = label
        self.fd = -1
        self.directory_fds: list[int] = []
        self.directory_links: list[
            tuple[int, str, int, tuple[int, int, int], str]
        ] = []
        registry = (
            directory_identities if directory_identities is not None else {}
        )
        file_registry = file_identities if file_identities is not None else {}
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            root_fd = os.open("/", directory_flags)
            self.directory_fds.append(root_fd)
            root_identity = _directory_identity(os.fstat(root_fd))
            known_root = registry.setdefault("/", root_identity)
            if known_root != root_identity:
                _fail(
                    "receipt.file.unstable",
                    "filesystem root identity changed during capture",
                )
            parent_fd = root_fd
            prefix = Path("/")
            for component in self.path.parts[1:-1]:
                before_directory = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(before_directory.st_mode):
                    _fail(
                        "receipt.file.invalid",
                        f"{label} has a non-directory or linked ancestor",
                    )
                child_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_fd,
                )
                self.directory_fds.append(child_fd)
                opened_directory = os.fstat(child_fd)
                identity = _directory_identity(opened_directory)
                if identity != _directory_identity(before_directory):
                    _fail(
                        "receipt.file.unstable",
                        f"{label} ancestor changed while being opened",
                    )
                prefix /= component
                prefix_text = str(prefix)
                known = registry.setdefault(prefix_text, identity)
                if known != identity:
                    _fail(
                        "receipt.file.unstable",
                        f"{label} ancestor has inconsistent identity",
                    )
                self.directory_links.append(
                    (
                        parent_fd,
                        component,
                        child_fd,
                        identity,
                        prefix_text,
                    )
                )
                parent_fd = child_fd
            self.parent_fd = parent_fd
            self.basename = self.path.name
            before = os.stat(
                self.basename,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        except ConformanceReceiptError:
            self.close()
            raise
        except (OSError, TypeError, ValueError) as exc:
            self.close()
            _fail("receipt.file.invalid", f"cannot stat {label}: {exc}")
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            self.close()
            _fail("receipt.file.invalid", f"{label} is not a regular non-link file")
        if before.st_nlink != 1:
            self.close()
            _fail("receipt.file.invalid", f"{label} must have exactly one hard link")
        if before.st_size > limit:
            self.close()
            _fail(
                "receipt.file.over_limit",
                f"{label} exceeds its {limit}-byte capture limit",
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self.fd = os.open(
                self.basename,
                flags,
                dir_fd=self.parent_fd,
            )
        except (OSError, TypeError, ValueError) as exc:
            self.close()
            _fail("receipt.file.invalid", f"cannot open {label}: {exc}")
        try:
            opened = os.fstat(self.fd)
            self.identity = _fingerprint(opened)
            if self.identity != _fingerprint(before):
                _fail(
                    "receipt.file.unstable",
                    f"{label} changed while being opened",
                )
            known_file = file_registry.setdefault(str(self.path), self.identity)
            if known_file != self.identity:
                _fail(
                    "receipt.file.unstable",
                    f"{label} path has inconsistent file identity",
                )
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(self.fd, min(1024 * 1024, limit + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > limit:
                    _fail(
                        "receipt.file.over_limit",
                        f"{label} grew beyond its {limit}-byte capture limit",
                    )
                chunks.append(chunk)
            self.body = b"".join(chunks)
            self.sha256 = hashlib.sha256(self.body).hexdigest()
            self.verify_unchanged()
        except ConformanceReceiptError:
            self.close()
            raise
        except (OSError, TypeError, ValueError) as exc:
            self.close()
            raise ConformanceReceiptError(
                "receipt.file.invalid",
                f"cannot read {label}: {exc}",
            ) from exc
        except BaseException:
            self.close()
            raise

    def verify_unchanged(self) -> None:
        try:
            opened = os.fstat(self.fd)
            current = os.stat(
                self.basename,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            for parent_fd, name, child_fd, identity, _prefix in (
                self.directory_links
            ):
                linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                held = os.fstat(child_fd)
                if (
                    _directory_identity(linked) != identity
                    or _directory_identity(held) != identity
                ):
                    _fail(
                        "receipt.file.unstable",
                        f"{self.label} ancestor changed during verification",
                    )
        except OSError as exc:
            _fail(
                "receipt.file.unstable",
                f"{self.label} changed during verification: {exc}",
            )
        if (
            _fingerprint(opened) != self.identity
            or _fingerprint(current) != self.identity
        ):
            _fail(
                "receipt.file.unstable",
                f"{self.label} changed during verification",
            )

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        for descriptor in reversed(self.directory_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.directory_fds = []


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(
            "receipt.structure.invalid",
            f"{label} must be a lowercase SHA-256 digest",
        )
    return value


def _canonical_unsigned(receipt: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in receipt.items() if key != "seal"}
    try:
        encoded = json.dumps(
            unsigned,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("receipt.structure.invalid", f"receipt is not canonical JSON: {exc}")
    return SIGNATURE_DOMAIN + encoded


def _canonical_value_sha256(value: object, label: str) -> str:
    pending: list[tuple[object, int, bool]] = [(value, 1, False)]
    active: set[int] = set()
    while pending:
        item, depth, leaving = pending.pop()
        identity = id(item)
        if leaving:
            active.remove(identity)
            continue
        if depth > MAX_JSON_STRUCTURE_DEPTH:
            _fail(
                "receipt.chain.invalid",
                f"{label} exceeds the JSON structure bound",
            )
        if isinstance(item, Mapping):
            children = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if identity in active:
            # json.dumps below will reject the cycle with the stable typed
            # receipt error; do not let the depth preflight loop forever.
            continue
        active.add(identity)
        pending.append((item, depth, True))
        pending.extend((child, depth + 1, False) for child in children)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ConformanceReceiptError(
            "receipt.chain.invalid",
            f"{label} is not strict canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_receipt_shape(
    receipt: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    root = _closed(
        receipt,
        "receipt",
        required={"schema", "job", "evidence", "extensions", "seal"},
    )
    if root["schema"] != RECEIPT_SCHEMA:
        _fail("receipt.structure.invalid", "receipt schema is unsupported")
    if root["extensions"] != {}:
        _fail("receipt.structure.invalid", "receipt extensions must be empty")
    job = _closed(
        root["job"],
        "receipt.job",
        required={"instance", "subject_sha256"},
    )
    instance = job["instance"]
    if not isinstance(instance, str) or not instance or len(instance) > 256:
        _fail(
            "receipt.structure.invalid",
            "receipt.job.instance must be nonempty text of at most 256 characters",
        )
    _sha256(job["subject_sha256"], "receipt.job.subject_sha256")
    evidence = _closed(
        root["evidence"],
        "receipt.evidence",
        required={*_EVIDENCE_ROLES, "simulation_files"},
    )
    for role in _EVIDENCE_ROLES:
        record = _closed(
            evidence[role],
            f"receipt.evidence.{role}",
            required={"bytes", "sha256"},
        )
        size = record["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(
                "receipt.structure.invalid",
                f"receipt.evidence.{role}.bytes must be a non-negative integer",
            )
        if role == "simulation_artifact" and size == 0:
            _fail(
                "receipt.structure.invalid",
                "receipt.evidence.simulation_artifact.bytes must be positive",
            )
        _sha256(record["sha256"], f"receipt.evidence.{role}.sha256")
    simulation_files = evidence["simulation_files"]
    if (
        not isinstance(simulation_files, list)
        or not 2 <= len(simulation_files) <= MAX_SIMULATION_FILE_RECORDS
    ):
        _fail(
            "receipt.structure.invalid",
            "receipt.evidence.simulation_files must contain 2 to "
            f"{MAX_SIMULATION_FILE_RECORDS} records",
        )
    for index, raw_record in enumerate(simulation_files):
        record = _closed(
            raw_record,
            f"receipt.evidence.simulation_files[{index}]",
            required=_SIMULATION_FILE_FIELDS,
        )
        if record["section"] not in {"inputs", "artifacts"}:
            _fail(
                "receipt.structure.invalid",
                f"simulation_files[{index}].section is invalid",
            )
        record_index = record["index"]
        if (
            isinstance(record_index, bool)
            or not isinstance(record_index, int)
            or record_index < 0
        ):
            _fail(
                "receipt.structure.invalid",
                f"simulation_files[{index}].index must be non-negative",
            )
        for field in ("kind", "role", "path"):
            value = record[field]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 4_095
                or any(ord(character) < 32 for character in value)
            ):
                _fail(
                    "receipt.structure.invalid",
                    f"simulation_files[{index}].{field} is invalid",
                )
        size = record["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail(
                "receipt.structure.invalid",
                f"simulation_files[{index}].bytes must be non-negative",
            )
        _sha256(
            record["sha256"],
            f"receipt.evidence.simulation_files[{index}].sha256",
        )
    seal = _closed(
        root["seal"],
        "receipt.seal",
        required={"algorithm", "key_id", "signature_hex"},
    )
    if seal["algorithm"] != "ed25519":
        _fail("receipt.structure.invalid", "receipt seal algorithm must be ed25519")
    _sha256(seal["key_id"], "receipt.seal.key_id")
    signature = seal["signature_hex"]
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        _fail(
            "receipt.structure.invalid",
            "receipt.seal.signature_hex must be 64 lowercase hexadecimal bytes",
        )
    return job, evidence, seal


def _verify_signature(
    receipt: Mapping[str, Any],
    seal: Mapping[str, Any],
    pinned_public_key: bytes,
) -> None:
    if not isinstance(pinned_public_key, bytes) or len(pinned_public_key) != 32:
        _fail(
            "receipt.key.invalid",
            "the caller-pinned Ed25519 public key must be exactly 32 bytes",
        )
    key_id = hashlib.sha256(pinned_public_key).hexdigest()
    if seal["key_id"] != key_id:
        _fail(
            "receipt.key.mismatch",
            "the receipt does not name the caller-pinned public key",
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - isolated base install
        _fail(
            "receipt.crypto.unavailable",
            "receipt verification requires the 'conformance' dependency set",
        )
    try:
        key = Ed25519PublicKey.from_public_bytes(pinned_public_key)
        key.verify(
            bytes.fromhex(str(seal["signature_hex"])),
            _canonical_unsigned(receipt),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ConformanceReceiptError(
            "receipt.signature.invalid",
            "the scheduler seal is not valid for this receipt",
        ) from exc


def _require_result(
    value: Mapping[str, Any],
    *,
    operation: str,
    label: str,
) -> None:
    try:
        assert_result_conforms(
            value,
            expected_operation=operation,
            expected_execution_status="completed",
            expected_engineering_status="pass",
        )
    except (KeyError, ResultConformanceError) as exc:
        raise ConformanceReceiptError(
            "receipt.chain.invalid",
            f"{label} is not a passing conformant {operation} envelope",
        ) from exc


def _rewrite_exact(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, list):
        return [_rewrite_exact(item, old, new) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _rewrite_exact(item, old, new)
            for key, item in value.items()
        }
    return value


def _without_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = deepcopy(dict(value))
    provenance = selected.get("provenance")
    if isinstance(provenance, dict):
        # Wall-clock creation time is the sole nondeterministic result field.
        # Tool/package version and host identity remain part of replay equality.
        provenance.pop("created_at", None)
    return selected


def _compare_replay(
    captured: Mapping[str, Any],
    replayed: Mapping[str, Any],
    *,
    label: str,
) -> None:
    def canonical(value: Mapping[str, Any]) -> bytes:
        try:
            return json.dumps(
                _without_provenance(value),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ConformanceReceiptError(
                "receipt.chain.invalid",
                f"{label} is not strict deterministic JSON",
            ) from exc

    if canonical(captured) != canonical(replayed):
        _fail(
            "receipt.chain.replay_mismatch",
            f"{label} does not match deterministic replay from sealed predecessors",
        )


def _simulation_file_records(
    simulation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    input_count = 0
    log_count = 0
    for section in ("inputs", "artifacts"):
        raw_records = simulation.get(section)
        if not isinstance(raw_records, list):
            _fail(
                "receipt.chain.invalid",
                f"simulation.{section} must be an array",
            )
        for index, raw_record in enumerate(raw_records):
            if not isinstance(raw_record, Mapping):
                _fail(
                    "receipt.chain.invalid",
                    f"simulation.{section}[{index}] must be an object",
                )
            if raw_record.get("exists") is not True:
                _fail(
                    "receipt.chain.incomplete",
                    "a passing sealed simulation may not contain absent file records",
                )
            if raw_record.get("role") == "simulation.result":
                continue
            if section == "inputs":
                input_count += 1
            if section == "artifacts" and raw_record.get("role") == "simulation.log":
                log_count += 1
            try:
                record = {
                    "section": section,
                    "index": index,
                    "kind": raw_record["kind"],
                    "role": raw_record["role"],
                    "path": raw_record["path"],
                    "bytes": raw_record["bytes"],
                    "sha256": raw_record["sha256"],
                }
            except KeyError as exc:
                raise ConformanceReceiptError(
                    "receipt.chain.invalid",
                    f"simulation.{section}[{index}] is not a complete file record",
                ) from exc
            records.append(record)
    if input_count == 0:
        _fail(
            "receipt.chain.incomplete",
            "a sealed simulation must retain at least one exact input file",
        )
    if log_count != 1:
        _fail(
            "receipt.chain.incomplete",
            "a sealed simulation must retain exactly one simulation.log artifact",
        )
    return records


def _simulation_configuration(
    simulation: Mapping[str, Any],
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    try:
        data = simulation["data"]
        extensions = data["extensions"]
        native = extensions["org.openada"]
        parameters = native["parameters"]
    except (KeyError, TypeError) as exc:
        raise ConformanceReceiptError(
            "receipt.chain.invalid",
            "simulation lacks its built-in request and configuration binding",
        ) from exc
    if (
        not isinstance(data, Mapping)
        or not isinstance(extensions, Mapping)
        or not isinstance(native, Mapping)
        or not isinstance(parameters, Mapping)
    ):
        _fail(
            "receipt.chain.invalid",
            "simulation request and configuration binding must be objects",
        )
    inputs = simulation.get("inputs")
    if not isinstance(inputs, list):
        _fail("receipt.chain.invalid", "simulation.inputs must be an array")
    expected: list[dict[str, Any]] = []
    for index, record in enumerate(inputs):
        if not isinstance(record, Mapping):
            _fail(
                "receipt.chain.invalid",
                f"simulation.inputs[{index}] must be an object",
            )
        configuration_role = _CONFIGURATION_ROLE_BY_INPUT_ROLE.get(
            str(record.get("role"))
        )
        if configuration_role is None:
            continue
        try:
            expected.append(
                {
                    "role": configuration_role,
                    "path": record["path"],
                    "sha256": record["sha256"],
                    "bytes": record["bytes"],
                    "identity": "content-digest",
                }
            )
        except KeyError as exc:
            raise ConformanceReceiptError(
                "receipt.chain.invalid",
                f"simulation.inputs[{index}] is not content-bound",
            ) from exc
    actual = native.get("configuration", [])
    if not isinstance(actual, list):
        _fail(
            "receipt.chain.invalid",
            "simulation configuration references must be an array",
        )
    if _canonical_value_sha256(
        actual,
        "simulation configuration",
    ) != _canonical_value_sha256(
        expected,
        "expected simulation configuration",
    ):
        _fail(
            "receipt.chain.invalid",
            "simulation configuration references do not exactly match captured inputs",
        )
    return parameters, expected


def typed_evidence_simulation_context_sha256(
    simulation: Mapping[str, Any],
) -> str:
    """Digest the scheduler-pinned, non-runtime simulation experiment context."""

    parameters, configuration = _simulation_configuration(simulation)
    try:
        data = simulation["data"]
        extensions = data["extensions"]
        inputs = simulation["inputs"]
        context = {
            "tool": simulation["tool"],
            "protocol": data["protocol"],
            "parameters": parameters,
            "inputs": [
                {
                    "kind": record["kind"],
                    "role": record["role"],
                    "bytes": record["bytes"],
                    "sha256": record["sha256"],
                }
                for record in inputs
            ],
            "configuration": configuration,
            "simulation_target": extensions.get("org.openada.simulation-target"),
            "pdk_binding": extensions.get("org.openada.pdk-binding"),
        }
    except (KeyError, TypeError) as exc:
        raise ConformanceReceiptError(
            "receipt.chain.invalid",
            "simulation cannot form a complete experiment context",
        ) from exc
    return _canonical_value_sha256(context, "simulation experiment context")


def verify_typed_evidence_receipt(
    receipt_path: str | Path,
    simulation_path: str | Path,
    raw_path: str | Path,
    selection_path: str | Path,
    extraction_path: str | Path,
    measurement_request_path: str | Path,
    measurement_path: str | Path,
    *,
    expected_job_instance: str,
    expected_subject_sha256: str,
    expected_simulation_context_sha256: str,
    expected_selection_request_sha256: str,
    expected_measurement_request_sha256: str,
    pinned_public_key: bytes,
) -> VerifiedTypedEvidence:
    """Verify one scheduler-sealed simulate→extract→measure evidence chain.

    The expected job, subject, simulation-context, selection-request, and
    measurement-request digests plus ``pinned_public_key`` are trust inputs.
    They must come from the scheduler's frozen claim manifest, not from the
    job or receipt.  The immutable return value is the only supported handoff;
    callers must not reopen an evidence path after verification.
    """

    if (
        not isinstance(expected_job_instance, str)
        or not expected_job_instance
        or len(expected_job_instance) > 256
    ):
        _fail("receipt.expectation.invalid", "expected job instance is invalid")
    expected_subject_sha256 = _sha256(
        expected_subject_sha256,
        "expected_subject_sha256",
    )
    expected_measurement_request_sha256 = _sha256(
        expected_measurement_request_sha256,
        "expected_measurement_request_sha256",
    )
    expected_simulation_context_sha256 = _sha256(
        expected_simulation_context_sha256,
        "expected_simulation_context_sha256",
    )
    expected_selection_request_sha256 = _sha256(
        expected_selection_request_sha256,
        "expected_selection_request_sha256",
    )
    paths = {
        "simulation_envelope": simulation_path,
        "simulation_artifact": raw_path,
        "selection_request": selection_path,
        "extraction_envelope": extraction_path,
        "measurement_request": measurement_request_path,
        "measurement_envelope": measurement_path,
    }
    captures: dict[str, _StableCapture] = {}
    simulation_captures: dict[str, _StableCapture] = {}
    directory_identities: dict[str, tuple[int, int, int]] = {}
    file_identities: dict[str, tuple[int, ...]] = {}
    receipt_capture: _StableCapture | None = None
    try:
        receipt_capture = _StableCapture(
            receipt_path,
            limit=MAX_RECEIPT_BYTES,
            label="receipt",
            directory_identities=directory_identities,
            file_identities=file_identities,
        )
        receipt = _strict_json(receipt_capture.body, "receipt")
        job, evidence, seal = _validate_receipt_shape(receipt)
        _verify_signature(receipt, seal, pinned_public_key)
        if job["instance"] != expected_job_instance:
            _fail(
                "receipt.job.mismatch",
                "the signed job instance does not match the scheduler attempt",
            )
        if job["subject_sha256"] != expected_subject_sha256:
            _fail(
                "receipt.subject.mismatch",
                "the signed subject does not match the frozen assignment",
            )

        total = 0
        for role, path in paths.items():
            limit = (
                MAX_RAW_EVIDENCE_BYTES
                if role == "simulation_artifact"
                else MAX_JSON_EVIDENCE_BYTES
            )
            captured = _StableCapture(
                path,
                limit=limit,
                label=role,
                directory_identities=directory_identities,
                file_identities=file_identities,
            )
            captures[role] = captured
            total += len(captured.body)
            if total > MAX_TOTAL_EVIDENCE_BYTES:
                _fail(
                    "receipt.file.over_limit",
                    "evidence exceeds the aggregate capture limit",
                )
            record = evidence[role]
            if (
                record["bytes"] != len(captured.body)
                or record["sha256"] != captured.sha256
            ):
                _fail(
                    "receipt.evidence.mismatch",
                    f"{role} bytes do not match the scheduler-sealed record",
                )
        if (
            captures["selection_request"].sha256
            != expected_selection_request_sha256
        ):
            _fail(
                "receipt.experiment.mismatch",
                "the sealed selection is not the scheduler-pinned experiment",
            )
        if (
            captures["measurement_request"].sha256
            != expected_measurement_request_sha256
        ):
            _fail(
                "receipt.claim.mismatch",
                "the sealed measurement request is not the scheduler-pinned claim",
            )

        simulation = _strict_json(
            captures["simulation_envelope"].body,
            "simulation envelope",
        )
        selection = _strict_json(
            captures["selection_request"].body,
            "selection request",
        )
        extraction = _strict_json(
            captures["extraction_envelope"].body,
            "extraction envelope",
        )
        measurement_request = _strict_json(
            captures["measurement_request"].body,
            "measurement request",
        )
        measurement = _strict_json(
            captures["measurement_envelope"].body,
            "measurement envelope",
        )
        _require_result(simulation, operation="simulate", label="simulation")
        _require_result(
            extraction,
            operation="result.series.extract",
            label="extraction",
        )
        _require_result(
            measurement,
            operation="result.measure",
            label="measurement",
        )
        observed_simulation_context = (
            typed_evidence_simulation_context_sha256(simulation)
        )
        if observed_simulation_context != expected_simulation_context_sha256:
            _fail(
                "receipt.experiment.mismatch",
                "the sealed simulation is not the scheduler-pinned experiment",
            )
        selection = _closed(
            selection,
            "selection request",
            required={"selectors", "conditions", "extensions"},
        )
        if selection["extensions"] != {}:
            _fail(
                "receipt.chain.invalid",
                "selection request extensions must be empty",
            )
        measurement_request = _closed(
            measurement_request,
            "measurement request",
            required={
                "measurement_id",
                "kind",
                "signal",
                "parameters",
                "extensions",
            },
        )

        expected_simulation_files = _simulation_file_records(simulation)
        if evidence["simulation_files"] != expected_simulation_files:
            _fail(
                "receipt.chain.invalid",
                "sealed simulation file records do not exactly match the envelope",
            )
        for index, record in enumerate(expected_simulation_files):
            path = str(record["path"])
            captured = simulation_captures.get(path)
            if captured is None:
                captured = _StableCapture(
                    path,
                    limit=MAX_SIMULATION_FILE_BYTES,
                    label=f"simulation_files[{index}]",
                    directory_identities=directory_identities,
                    file_identities=file_identities,
                )
                simulation_captures[path] = captured
                total += len(captured.body)
                if total > MAX_TOTAL_EVIDENCE_BYTES:
                    _fail(
                        "receipt.file.over_limit",
                        "evidence exceeds the aggregate capture limit",
                    )
            if (
                record["bytes"] != len(captured.body)
                or record["sha256"] != captured.sha256
            ):
                _fail(
                    "receipt.evidence.mismatch",
                    f"simulation_files[{index}] does not match its exact bytes",
                )

        raw_records = [
            record
            for record in simulation["artifacts"]
            if record.get("role") == "simulation.result"
        ]
        if len(raw_records) != 1:
            _fail(
                "receipt.chain.invalid",
                "simulation must retain exactly one simulation.result artifact",
            )
        raw_record = raw_records[0]
        canonical_raw_path = str(captures["simulation_artifact"].path)
        if (
            raw_record.get("path") != canonical_raw_path
            or raw_record.get("bytes")
            != len(captures["simulation_artifact"].body)
            or raw_record.get("sha256")
            != captures["simulation_artifact"].sha256
        ):
            _fail(
                "receipt.chain.invalid",
                "simulation does not bind the sealed raw path, bytes, and digest",
            )

        extraction_data = extraction.get("data")
        measurement_data = measurement.get("data")
        if not isinstance(extraction_data, Mapping) or not isinstance(
            measurement_data,
            Mapping,
        ):
            _fail(
                "receipt.chain.invalid",
                "derived envelope data must be an object",
            )
        extraction_protocol = extraction_data.get("protocol")
        measurement_protocol = measurement_data.get("protocol")
        if not isinstance(extraction_protocol, Mapping) or not isinstance(
            measurement_protocol,
            Mapping,
        ):
            _fail(
                "receipt.chain.invalid",
                "derived envelope protocols must be objects",
            )
        extraction_request_id = extraction_protocol.get("request_id")
        measurement_request_id = measurement_protocol.get("request_id")
        if not isinstance(extraction_request_id, str) or not isinstance(
            measurement_request_id,
            str,
        ):
            _fail(
                "receipt.chain.invalid",
                "derived envelopes do not retain request identities",
            )

        with tempfile.TemporaryDirectory(
            prefix="openada-receipt-replay-"
        ) as replay_dir:
            replay_raw = Path(replay_dir) / "sealed.raw"
            replay_raw.write_bytes(captures["simulation_artifact"].body)
            simulation_for_replay = deepcopy(dict(simulation))
            simulation_for_replay = _rewrite_exact(
                simulation_for_replay,
                canonical_raw_path,
                str(replay_raw),
            )
            replayed_extraction = extract_result_series(
                simulation_for_replay,
                replay_raw,
                selection["selectors"],
                conditions=selection["conditions"],
                request_id=extraction_request_id,
            )
            replayed_extraction = _rewrite_exact(
                replayed_extraction,
                str(replay_raw),
                canonical_raw_path,
            )
            if not isinstance(replayed_extraction, Mapping):
                _fail(
                    "receipt.chain.invalid",
                    "extraction replay did not return an envelope",
                )
            _compare_replay(
                extraction,
                replayed_extraction,
                label="extraction envelope",
            )
            try:
                replayed_series = replayed_extraction["data"]["extraction"][
                    "series"
                ]
            except (KeyError, TypeError) as exc:
                raise ConformanceReceiptError(
                    "receipt.chain.invalid",
                    "extraction replay did not produce a normalized series",
                ) from exc
            replayed_measurement = measure_result(
                replayed_series,
                measurement_request,
                request_id=measurement_request_id,
            )
            _compare_replay(
                measurement,
                replayed_measurement,
                label="measurement envelope",
            )
            replayed_measurement_data = replayed_measurement.get("data")
            if not isinstance(replayed_measurement_data, Mapping):
                _fail(
                    "receipt.chain.invalid",
                    "replayed measurement data is not an object",
                )
            measured = replayed_measurement_data.get("measurement")
            if not isinstance(measured, Mapping):
                _fail(
                    "receipt.chain.invalid",
                    "replayed typed measurement is not an object",
                )
            measurement_id = measured.get("measurement_id")
            value = measured.get("value")
            unit = measured.get("unit")
            if (
                measured.get("status") != "measured"
                or not isinstance(measurement_id, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not isinstance(unit, str)
            ):
                _fail(
                    "receipt.chain.invalid",
                    "replayed typed measurement has no finite scalar",
                )

        for capture in captures.values():
            capture.verify_unchanged()
        for capture in simulation_captures.values():
            capture.verify_unchanged()
        receipt_capture.verify_unchanged()
        return VerifiedTypedEvidence(
            job_instance=expected_job_instance,
            subject_sha256=expected_subject_sha256,
            simulation_context_sha256=expected_simulation_context_sha256,
            selection_request_sha256=expected_selection_request_sha256,
            measurement_request_sha256=expected_measurement_request_sha256,
            measurement_envelope_sha256=captures[
                "measurement_envelope"
            ].sha256,
            measurement_envelope_bytes=captures[
                "measurement_envelope"
            ].body,
            measurement_id=measurement_id,
            value=float(value),
            unit=unit,
        )
    finally:
        for capture in captures.values():
            capture.close()
        for capture in simulation_captures.values():
            capture.close()
        if receipt_capture is not None:
            receipt_capture.close()


__all__ = [
    "ConformanceReceiptError",
    "RECEIPT_SCHEMA",
    "SIGNATURE_DOMAIN",
    "VerifiedTypedEvidence",
    "typed_evidence_simulation_context_sha256",
    "verify_typed_evidence_receipt",
]
