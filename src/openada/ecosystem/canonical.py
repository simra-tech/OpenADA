"""Bounded cross-language canonical JSON for request identity.

``openada.canonical-json/v1`` deliberately supports the interoperable JSON
subset used by the v0alpha2 request contract: null, booleans, strings,
IEEE-754-safe integers, arrays, and objects with string keys. Decimal
engineering values are represented as strings carrying explicit units. Binary
floating point is rejected instead of pretending that implementation-specific
formatting is canonical.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from threading import RLock
from typing import Any, Mapping


ALGORITHM = "openada.canonical-json/v1"
MAX_CANONICAL_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64
MAX_CONTAINER_ITEMS = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_ZERO_DIGEST = "0" * 64
_FORBIDDEN_REQUEST_KEYS = {
    "argv",
    "command",
    "credential",
    "credentials",
    "environment",
    "env",
    "importpath",
    "nativeaction",
    "password",
    "secret",
    "secrethandle",
    "secrethandles",
    "secretstore",
    "secretstorelocation",
    "setuptext",
    "shell",
}


class CanonicalJSONError(ValueError):
    """A value is outside the canonical JSON v1 subset or bounds."""


class RequestBindingError(ValueError):
    """A canonical request binding is absent, inconsistent, or reused."""


def _string(value: str) -> bytes:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CanonicalJSONError("canonical strings cannot contain lone surrogates")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _encode(value: Any, *, depth: int) -> bytes:
    if depth > MAX_DEPTH:
        raise CanonicalJSONError(f"canonical JSON exceeds maximum depth {MAX_DEPTH}")
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalJSONError("canonical integer is outside the exact cross-language range")
        return str(value).encode("ascii")
    if isinstance(value, float):
        raise CanonicalJSONError(
            "binary floating point is not part of openada.canonical-json/v1; use a decimal string"
        )
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CanonicalJSONError("canonical array exceeds the item limit")
        return b"[" + b",".join(_encode(item, depth=depth + 1) for item in value) + b"]"
    if isinstance(value, Mapping):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CanonicalJSONError("canonical object exceeds the member limit")
        if not all(isinstance(key, str) for key in value):
            raise CanonicalJSONError("canonical object keys must be strings")
        members = []
        for key in sorted(value):
            members.append(
                _string(key) + b":" + _encode(value[key], depth=depth + 1)
            )
        return b"{" + b",".join(members) + b"}"
    raise CanonicalJSONError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one value using the bounded normative v1 algorithm."""

    encoded = _encode(value, depth=0)
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise CanonicalJSONError(
            f"canonical JSON is {len(encoded)} bytes; limit is {MAX_CANONICAL_BYTES}"
        )
    return encoded


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _reject_injected_context(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise RequestBindingError("request parameter keys must be strings")
            if _normalized_key(key) in _FORBIDDEN_REQUEST_KEYS:
                location = ".".join((*path, key))
                raise RequestBindingError(
                    f"request parameters cannot carry host context or native action field {location!r}"
                )
            _reject_injected_context(child, path=(*path, key))
    elif isinstance(value, (list, tuple)):
        for position, child in enumerate(value):
            _reject_injected_context(child, path=(*path, str(position)))


def canonical_request_bytes(request: Mapping[str, Any]) -> bytes:
    """Canonicalize a request with its digest field deterministically zeroed."""

    if request.get("schema") != "openada.request/v0alpha2":
        raise RequestBindingError("canonical request must use openada.request/v0alpha2")
    canonical = request.get("canonical")
    if not isinstance(canonical, Mapping) or canonical.get("algorithm") != ALGORITHM:
        raise RequestBindingError(f"canonical request must select {ALGORITHM}")
    parameters = request.get("parameters")
    if not isinstance(parameters, Mapping):
        raise RequestBindingError("canonical request parameters must be an object")
    _reject_injected_context(parameters)
    normalized = deepcopy(dict(request))
    normalized["canonical"] = dict(normalized["canonical"])
    normalized["canonical"]["sha256"] = _ZERO_DIGEST
    return canonical_json_bytes(normalized)


def bind_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with the exact canonical request SHA-256 populated."""

    bound = deepcopy(dict(request))
    encoded = canonical_request_bytes(bound)
    bound["canonical"]["sha256"] = hashlib.sha256(encoded).hexdigest()
    return bound


class RequestIdentityRegistry:
    """Reject reuse of one request ID with different canonical bytes."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: dict[str, tuple[str, bytes]] = {}

    def register(self, request: Mapping[str, Any]) -> str:
        request_id = request.get("request_id")
        if not isinstance(request_id, str):
            raise RequestBindingError("canonical request has no string request_id")
        encoded = canonical_request_bytes(request)
        observed = hashlib.sha256(encoded).hexdigest()
        claimed = request.get("canonical", {}).get("sha256")
        if claimed != observed:
            raise RequestBindingError("canonical request digest does not match its bytes")
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None and previous != (observed, encoded):
                raise RequestBindingError(
                    "request ID was already registered with different canonical bytes"
                )
            self._requests[request_id] = (observed, encoded)
        return observed
