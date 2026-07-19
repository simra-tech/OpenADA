"""Host-authorized opaque context resolution."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Mapping

from .contracts import SchemaCatalog


class ContextResolutionError(ValueError):
    """An opaque context name is missing, ambiguous, or malformed."""


class HostContextResolver:
    """Map non-secret request names to preapproved filtered provider contexts."""

    def __init__(self, schemas: SchemaCatalog | None = None) -> None:
        self._schemas = schemas or SchemaCatalog()
        self._lock = RLock()
        self._contexts: dict[str, dict[str, Any]] = {}

    def register(self, context: Mapping[str, Any]) -> None:
        self._schemas.validate(context)
        if context.get("schema") != "openada.invocation-context/v0alpha1":
            raise ContextResolutionError("unsupported invocation context schema")
        name = str(context["context_name"])
        document = deepcopy(dict(context))
        with self._lock:
            previous = self._contexts.get(name)
            if previous is not None and previous != document:
                raise ContextResolutionError(
                    f"context name has conflicting host policy: {name}"
                )
            self._contexts[name] = document

    def resolve(self, context_name: str | None, provider_id: str) -> dict[str, Any]:
        if context_name is None:
            return {
                "schema": "openada.invocation-context/v0alpha1",
                "context_name": "default-empty",
                "provider_id": provider_id,
                "filtered_environment": {},
                "secret_handles": [],
                "setup_identity": None,
                "authorization_identity": None,
                "extensions": {},
            }
        with self._lock:
            context = self._contexts.get(context_name)
        if context is None:
            raise ContextResolutionError(f"host context is not approved: {context_name}")
        if context["provider_id"] != provider_id:
            raise ContextResolutionError("host context is not approved for this provider")
        return deepcopy(context)
