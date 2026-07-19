"""Closed, revision-aware locator resolution with bounded identity checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from threading import RLock
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .canonical import canonical_json_bytes
from .contracts import SchemaCatalog


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_ENTRIES = 10_000
MAX_TREE_BYTES = 256 * 1024 * 1024


class LocatorError(ValueError):
    """A locator is invalid, unavailable, ambiguous, stale, or escapes policy."""


@dataclass(frozen=True, slots=True)
class ResolvedLocator:
    kind: str
    value: str
    identity_kind: str
    identity_value: str
    contained: bool
    metadata: dict[str, Any]


def _relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise LocatorError("filesystem locator must be a contained relative path")
    return path


def _stable_regular(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LocatorError(f"cannot securely open regular file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LocatorError(f"locator does not resolve to a regular file: {path}")
        if before.st_size > MAX_FILE_BYTES:
            raise LocatorError(f"regular file exceeds {MAX_FILE_BYTES} bytes: {path}")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(encoded) > MAX_FILE_BYTES or len(encoded) != before.st_size:
            raise LocatorError(f"regular file is over limit or unstable: {path}")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise LocatorError(f"regular file changed while it was read: {path}")
        return encoded, before
    finally:
        os.close(descriptor)


def _tree_snapshot(path: Path) -> tuple[str, int, int]:
    if path.is_symlink() or not path.is_dir():
        raise LocatorError(f"tree locator must resolve to a real directory: {path}")
    root = path.resolve()
    entries: list[dict[str, Any]] = []
    total = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            raise LocatorError(f"tree contains a symbolic link: {relative}")
        try:
            resolved = child.resolve(strict=True)
        except OSError as exc:
            raise LocatorError(f"tree entry is unstable: {relative}") from exc
        if resolved != root and root not in resolved.parents:
            raise LocatorError(f"tree entry escapes the selected root: {relative}")
        if child.is_dir():
            entries.append({"path": relative, "kind": "directory"})
        elif child.is_file():
            expected = resolved.stat()
            encoded, metadata = _stable_regular(child)
            if (expected.st_dev, expected.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise LocatorError(f"tree entry changed before identity binding: {relative}")
            total += len(encoded)
            entries.append(
                {
                    "path": relative,
                    "kind": "regular-file",
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "mode": stat.S_IMODE(metadata.st_mode),
                }
            )
        else:
            raise LocatorError(f"tree contains an unsupported filesystem entry: {relative}")
        if len(entries) > MAX_TREE_ENTRIES or total > MAX_TREE_BYTES:
            raise LocatorError("tree locator exceeds its entry or byte limit")
    digest = hashlib.sha256(canonical_json_bytes(entries)).hexdigest()
    return digest, len(entries), total


class NativeObjectStore:
    """In-memory fake native objects with exact optimistic revisions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._objects: dict[str, tuple[str, Any]] = {}

    def create(self, identity: str, revision: str, value: Any) -> None:
        with self._lock:
            if identity in self._objects:
                raise LocatorError(f"native object already exists: {identity}")
            self._objects[identity] = (revision, deepcopy(value))

    def read(self, identity: str, revision: str) -> Any:
        with self._lock:
            current = self._objects.get(identity)
            if current is None:
                raise LocatorError(f"native object does not exist: {identity}")
            if current[0] != revision:
                raise LocatorError("native object revision precondition failed")
            return deepcopy(current[1])

    def mutate(
        self,
        identity: str,
        precondition: str,
        postcondition: str,
        operation: Callable[[Any], Any],
    ) -> Any:
        with self._lock:
            current = self._objects.get(identity)
            if current is None or current[0] != precondition:
                raise LocatorError("native object revision precondition failed")
            if precondition == postcondition:
                raise LocatorError("native mutation must advance the revision")
            value = operation(deepcopy(current[1]))
            self._objects[identity] = (postcondition, deepcopy(value))
            return deepcopy(value)


class LocatorResolver:
    """Resolve only an explicit closed locator vocabulary under host policy."""

    def __init__(
        self,
        roots: Mapping[str, str | Path] | None = None,
        *,
        approved_uri_origins: Iterable[str] = (),
        schemas: SchemaCatalog | None = None,
        native_objects: NativeObjectStore | None = None,
    ) -> None:
        self._roots: dict[str, Path] = {}
        for identity, root in (roots or {}).items():
            resolved = Path(root).resolve()
            if not resolved.is_dir():
                raise LocatorError(f"locator root is not a directory: {identity}")
            self._roots[identity] = resolved
        self._origins = frozenset(approved_uri_origins)
        self._schemas = schemas or SchemaCatalog()
        self.native_objects = native_objects or NativeObjectStore()
        self._artifacts: dict[str, tuple[str, Any]] = {}
        self._sessions: dict[str, tuple[str, Any]] = {}

    def register_artifact(self, reference: str, sha256: str, value: Any) -> None:
        previous = self._artifacts.get(reference)
        candidate = (sha256, deepcopy(value))
        if previous is not None and previous != candidate:
            raise LocatorError(f"artifact reference conflicts: {reference}")
        self._artifacts[reference] = candidate

    def register_session(self, reference: str, revision: str, value: Any) -> None:
        previous = self._sessions.get(reference)
        candidate = (revision, deepcopy(value))
        if previous is not None and previous != candidate:
            raise LocatorError(f"session reference conflicts: {reference}")
        self._sessions[reference] = candidate

    @staticmethod
    def _expect(locator: Mapping[str, Any], kind: str, value: str) -> None:
        identity = locator["identity"]
        if identity["kind"] != kind or identity["value"] != value:
            raise LocatorError(
                f"locator identity mismatch: expected {kind}:{value}, got "
                f"{identity['kind']}:{identity['value']}"
            )

    def _filesystem(self, locator: Mapping[str, Any]) -> ResolvedLocator:
        root_id = locator["root_id"]
        if root_id not in self._roots:
            raise LocatorError(f"filesystem locator root is not approved: {root_id}")
        root = self._roots[root_id]
        if locator["intent"] == "mutate":
            mutation = locator["mutation"]
            if locator["identity"]["value"] != mutation["precondition"]:
                raise LocatorError("filesystem identity must equal the mutation precondition")
            if mutation["precondition"] == mutation["postcondition"]:
                raise LocatorError("filesystem mutation must advance the identity")
        path = root / _relative(locator["value"])
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise LocatorError(f"filesystem locator is unavailable: {path}") from exc
        if resolved != root and root not in resolved.parents:
            raise LocatorError("filesystem locator escapes its approved root")
        if locator["kind"] == "regular-file":
            expected = resolved.stat()
            encoded, observed = _stable_regular(path)
            if (expected.st_dev, expected.st_ino) != (observed.st_dev, observed.st_ino):
                raise LocatorError("filesystem locator changed before identity binding")
            digest = hashlib.sha256(encoded).hexdigest()
            self._expect(locator, "sha256", digest)
            metadata = {"size": len(encoded)}
            identity_kind = "sha256"
        else:
            digest, entries, size = _tree_snapshot(path)
            self._expect(locator, "snapshot", digest)
            metadata = {"entries": entries, "size": size}
            identity_kind = "snapshot"
        return ResolvedLocator(
            kind=locator["kind"],
            value=str(resolved),
            identity_kind=identity_kind,
            identity_value=digest,
            contained=True,
            metadata=metadata,
        )

    def resolve(self, locator: Mapping[str, Any]) -> ResolvedLocator:
        self._schemas.validate(locator)
        if locator.get("schema") != "openada.locator/v0alpha1":
            raise LocatorError("locator uses an unsupported schema")
        kind = locator["kind"]
        if kind in {"regular-file", "directory-tree", "workspace"}:
            return self._filesystem(locator)
        if locator["root_id"] is not None:
            raise LocatorError(f"{kind} locator cannot select a filesystem root")
        value = locator["value"]
        if kind == "artifact-reference":
            if locator["intent"] != "read":
                raise LocatorError("artifact references are immutable in locator v0alpha1")
            record = self._artifacts.get(value)
            if record is None:
                raise LocatorError(f"artifact reference is unavailable: {value}")
            self._expect(locator, "sha256", record[0])
            return ResolvedLocator(kind, value, "sha256", record[0], True, {})
        if kind == "session":
            if locator["intent"] != "read":
                raise LocatorError("session locators are read-only in locator v0alpha1")
            record = self._sessions.get(value)
            if record is None:
                raise LocatorError(f"session reference is unavailable: {value}")
            self._expect(locator, "opaque", record[0])
            return ResolvedLocator(kind, value, "opaque", record[0], True, {})
        if kind == "native-object":
            revision = locator["identity"]["value"]
            if locator["identity"]["kind"] != "native-revision":
                raise LocatorError("native object requires a native-revision identity")
            if locator["intent"] == "mutate" and revision != locator["mutation"]["precondition"]:
                raise LocatorError("native identity must equal the mutation precondition")
            self.native_objects.read(value, revision)
            return ResolvedLocator(kind, value, "native-revision", revision, True, {})
        if kind == "approved-uri":
            if locator["intent"] != "read":
                raise LocatorError("approved URI locators are read-only in locator v0alpha1")
            parsed = urlsplit(value)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if parsed.username or parsed.password or origin not in self._origins:
                raise LocatorError(f"URI origin is not approved: {origin}")
            if parsed.scheme not in {"https", "file"} or parsed.fragment:
                raise LocatorError("URI uses an unsupported scheme or fragment")
            identity = locator["identity"]
            if identity["kind"] != "opaque":
                raise LocatorError("approved URI requires a host-issued opaque identity")
            return ResolvedLocator(
                kind, value, "opaque", identity["value"], True, {"origin": origin}
            )
        raise LocatorError(f"unknown locator kind: {kind}")

    def verify_postcondition(self, locator: Mapping[str, Any]) -> ResolvedLocator:
        """Resolve a mutation target against its exact declared postcondition."""

        self._schemas.validate(locator)
        if locator.get("intent") != "mutate":
            raise LocatorError("postcondition verification requires mutation intent")
        mutation = locator["mutation"]
        if locator["kind"] == "native-object":
            self.native_objects.read(locator["value"], mutation["postcondition"])
            return ResolvedLocator(
                "native-object",
                locator["value"],
                "native-revision",
                mutation["postcondition"],
                True,
                {},
            )
        post = deepcopy(dict(locator))
        post["intent"] = "read"
        post["mutation"] = None
        post["identity"] = deepcopy(dict(post["identity"]))
        post["identity"]["value"] = mutation["postcondition"]
        return self.resolve(post)

    def mutate_native(
        self, locator: Mapping[str, Any], operation: Callable[[Any], Any]
    ) -> Any:
        self._schemas.validate(locator)
        if locator.get("kind") != "native-object" or locator.get("intent") != "mutate":
            raise LocatorError("typed native mutation requires a mutate native-object locator")
        mutation = locator["mutation"]
        if locator["identity"] != {
            "kind": "native-revision",
            "value": mutation["precondition"],
            "extensions": {},
        }:
            raise LocatorError("native locator identity must equal the mutation precondition")
        return self.native_objects.mutate(
            locator["value"],
            mutation["precondition"],
            mutation["postcondition"],
            operation,
        )
