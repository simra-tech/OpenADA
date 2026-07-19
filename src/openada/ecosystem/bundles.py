"""Trusted, immutable loading for externally installed provider bundles."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import re
from threading import RLock
from typing import Any, Iterable, Mapping

from .contracts import SchemaCatalog
from .registries import OperationValidatorRegistry


MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_BYTES = 16 * 1024 * 1024


class BundleError(ValueError):
    """A provider bundle is untrusted, malformed, conflicting, or changed."""


_VERSION = re.compile(r"^v([0-9]+)(?:(alpha|beta|rc)([1-9][0-9]*))?$")


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise BundleError(f"unsupported contract version syntax: {value}")
    stage = {"alpha": 0, "beta": 1, "rc": 2, None: 3}[match.group(2)]
    return int(match.group(1)), stage, int(match.group(3) or 0)


def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BundleError(f"duplicate JSON member {key!r}")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise BundleError(f"non-finite JSON number {value!r} is not supported")


def _secure_bytes(path: Path, roots: tuple[Path, ...], limit: int) -> bytes:
    """Read a stable regular file without following its final symlink."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BundleError(f"cannot resolve provider resource {path}: {exc}") from exc
    if not _contained(resolved, roots):
        raise BundleError(f"provider resource escapes every approved root: {path}")
    try:
        expected = resolved.stat()
    except OSError as exc:
        raise BundleError(f"cannot stat provider resource {path}: {exc}") from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundleError(f"cannot securely open provider resource {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            raise BundleError(f"provider resource changed before it was opened: {path}")
        if not stat.S_ISREG(before.st_mode):
            raise BundleError(f"provider resource is not a regular file: {path}")
        if before.st_nlink != 1:
            raise BundleError(f"provider resource must have exactly one hard link: {path}")
        if before.st_size > limit:
            raise BundleError(f"provider resource exceeds {limit} bytes: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if not stable or len(encoded) != before.st_size:
            raise BundleError(f"provider resource changed while it was read: {path}")
        if len(encoded) > limit:
            raise BundleError(f"provider resource exceeds {limit} bytes: {path}")
        return encoded
    finally:
        os.close(descriptor)


def _strict_document(encoded: bytes, path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_duplicates,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"provider resource is not strict UTF-8 JSON: {path}") from exc
    if not isinstance(document, dict):
        raise BundleError(f"provider resource is not a JSON object: {path}")
    return document


@dataclass(frozen=True, slots=True)
class LoadedBundle:
    identity: str
    version: str
    manifest_sha256: str
    manifest_path: Path
    resources: tuple[tuple[Path, str], ...]
    document: dict[str, Any]


class ProviderBundleRegistry:
    """Load explicit bundle manifests from host-approved filesystem roots.

    Manifests are declarative only.  This class never scans for bundles, fetches
    content, imports code named by a manifest, or executes bundle-owned text.
    """

    def __init__(
        self,
        approved_roots: Iterable[str | Path],
        validators: OperationValidatorRegistry,
        schemas: SchemaCatalog | None = None,
        supported_contract_version: str = "v1alpha1",
    ) -> None:
        roots = tuple(sorted({Path(root).resolve() for root in approved_roots}))
        if not roots or any(not root.is_dir() for root in roots):
            raise BundleError("approved provider roots must be existing directories")
        self._roots = roots
        self._validators = validators
        self._schemas = schemas or SchemaCatalog()
        self._contract_version = supported_contract_version
        self._lock = RLock()
        self._bundles: dict[tuple[str, str], LoadedBundle] = {}
        self._profiles: dict[tuple[str, str], str] = {}
        self._assertions: dict[tuple[str, str], str] = {}

    @staticmethod
    def _profile_parts(document: Mapping[str, Any]) -> tuple[str, str, str, str]:
        operation = document.get("operation")
        assertion = document.get("assertion")
        if not isinstance(operation, Mapping) or not isinstance(assertion, Mapping):
            raise BundleError("a profile resource lacks operation or assertion identity")
        operation_id = operation.get("id")
        assertion_id = assertion.get("id")
        if not isinstance(operation_id, str) or not isinstance(assertion_id, str):
            raise BundleError("a profile resource has malformed identities")
        operation_revision = operation_id.rsplit("/", 1)[-1]
        assertion_revision = assertion_id.rsplit("/", 1)[-1]
        return operation_id, operation_revision, assertion_id, assertion_revision

    def _load_resource(
        self, root: Path, resource: Mapping[str, Any]
    ) -> tuple[Path, str, dict[str, Any]]:
        relative = Path(str(resource["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise BundleError("bundle resource path must be relative and contained")
        path = root / relative
        encoded = _secure_bytes(path, self._roots, MAX_RESOURCE_BYTES)
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != resource["sha256"]:
            raise BundleError(f"bundle resource digest mismatch: {relative}")
        return path.resolve(), digest, _strict_document(encoded, path)

    def load(self, manifest_path: str | Path) -> LoadedBundle:
        path = Path(manifest_path)
        encoded = _secure_bytes(path, self._roots, MAX_BUNDLE_BYTES)
        document = _strict_document(encoded, path)
        self._schemas.validate(document)
        if document.get("schema") != "openada.provider-bundle/v0alpha1":
            raise BundleError("provider bundle uses an unsupported schema")
        contract_range = document["contract_range"]
        selected = _version_key(self._contract_version)
        if not (
            _version_key(contract_range["minimum"])
            <= selected
            < _version_key(contract_range["maximum_exclusive"])
        ):
            raise BundleError(
                f"provider bundle is incompatible with contract {self._contract_version}"
            )
        for validator in document["validators"]:
            if not self._validators.has(validator["identity"], validator["revision"]):
                raise BundleError(
                    "provider bundle requires an unregistered host-trusted validator: "
                    f"{validator['identity']}@{validator['revision']}"
                )

        root = path.resolve().parent
        resources: dict[Path, tuple[str, dict[str, Any]]] = {}
        profile_identities: set[tuple[str, str]] = set()
        assertion_identities: set[tuple[str, str]] = set()
        for resource in document["profiles"]:
            resource_path, digest, profile = self._load_resource(root, resource)
            operation_id, revision, _, _ = self._profile_parts(profile)
            if (operation_id, revision) != (resource["identity"], resource["revision"]):
                raise BundleError("profile identity or revision does not match its manifest")
            if (operation_id, revision) in profile_identities:
                raise BundleError("provider bundle repeats a profile identity")
            profile_identities.add((operation_id, revision))
            resources[resource_path] = (digest, profile)
        for resource in document["assertions"]:
            resource_path, digest, profile = self._load_resource(root, resource)
            _, _, assertion_id, revision = self._profile_parts(profile)
            if (assertion_id, revision) != (
                resource["identity"],
                resource["revision"],
            ):
                raise BundleError("assertion identity or revision does not match its manifest")
            if (assertion_id, revision) in assertion_identities:
                raise BundleError("provider bundle repeats an assertion identity")
            assertion_identities.add((assertion_id, revision))
            previous = resources.get(resource_path)
            if previous is not None and previous[0] != digest:
                raise BundleError("provider resource changed between manifest references")
            resources[resource_path] = (digest, profile)

        bundle = document["bundle"]
        manifest_digest = hashlib.sha256(encoded).hexdigest()
        loaded = LoadedBundle(
            identity=bundle["id"],
            version=bundle["version"],
            manifest_sha256=manifest_digest,
            manifest_path=path.resolve(),
            resources=tuple(
                (resource, value[0]) for resource, value in sorted(
                    resources.items(), key=lambda item: str(item[0])
                )
            ),
            document=deepcopy(document),
        )
        key = (loaded.identity, loaded.version)
        with self._lock:
            previous = self._bundles.get(key)
            if previous is not None and previous.manifest_sha256 != manifest_digest:
                raise BundleError(f"provider bundle identity has conflicting content: {key}")
            for resource in document["profiles"]:
                resource_key = (resource["identity"], resource["revision"])
                previous_digest = self._profiles.get(resource_key)
                if previous_digest is not None and previous_digest != resource["sha256"]:
                    raise BundleError(
                        f"profile identity has conflicting content: {resource_key}"
                    )
            for resource in document["assertions"]:
                resource_key = (resource["identity"], resource["revision"])
                previous_digest = self._assertions.get(resource_key)
                if previous_digest is not None and previous_digest != resource["sha256"]:
                    raise BundleError(
                        f"assertion identity has conflicting content: {resource_key}"
                    )
            self._bundles[key] = loaded
            self._profiles.update(
                {
                    (resource["identity"], resource["revision"]): resource["sha256"]
                    for resource in document["profiles"]
                }
            )
            self._assertions.update(
                {
                    (resource["identity"], resource["revision"]): resource["sha256"]
                    for resource in document["assertions"]
                }
            )
        return deepcopy(loaded)

    def verify_unchanged(self, identity: str, version: str) -> None:
        with self._lock:
            loaded = self._bundles.get((identity, version))
        if loaded is None:
            raise BundleError(f"provider bundle is not registered: {(identity, version)}")
        manifest = _secure_bytes(loaded.manifest_path, self._roots, MAX_BUNDLE_BYTES)
        if hashlib.sha256(manifest).hexdigest() != loaded.manifest_sha256:
            raise BundleError("provider bundle manifest changed after registration")
        for path, expected in loaded.resources:
            encoded = _secure_bytes(path, self._roots, MAX_RESOURCE_BYTES)
            if hashlib.sha256(encoded).hexdigest() != expected:
                raise BundleError(f"provider resource changed after registration: {path}")

    def records(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            return tuple(
                (identity, version, value.manifest_sha256)
                for (identity, version), value in sorted(self._bundles.items())
            )
