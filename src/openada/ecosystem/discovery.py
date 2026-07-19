"""Explicit installed-entry-point discovery for host-trusted extensions."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Sequence

from .bundles import LoadedBundle, ProviderBundleRegistry
from .registries import OperationValidatorRegistry, ValidatorKey


VALIDATOR_ENTRY_POINT_GROUP = "openada.operation_validators.v1"
BUNDLE_ENTRY_POINT_GROUP = "openada.provider_bundles.v1"


class DiscoveryError(ValueError):
    """An explicitly selected installed extension is missing or malformed."""


def _selected(group: str, names: Sequence[str]) -> tuple[Any, ...]:
    if not names:
        return ()
    requested = set(names)
    if len(requested) != len(names):
        raise DiscoveryError("installed entry-point selection contains duplicates")
    points = metadata.entry_points()
    candidates = tuple(points.select(group=group)) if hasattr(points, "select") else tuple(points.get(group, ()))
    by_name: dict[str, Any] = {}
    for point in candidates:
        if point.name in requested:
            if point.name in by_name:
                raise DiscoveryError(f"installed entry point is ambiguous: {point.name}")
            by_name[point.name] = point
    missing = sorted(requested - set(by_name))
    if missing:
        raise DiscoveryError(f"installed entry points are unavailable: {missing}")
    return tuple(by_name[name] for name in names)


def register_installed_validators(
    registry: OperationValidatorRegistry, names: Sequence[str]
) -> tuple[ValidatorKey, ...]:
    """Load only explicitly allowlisted validator entry points.

    Installation and the host allowlist are the executable-code trust action.
    Provider manifests cannot select an entry point or import path.
    """

    registered: list[ValidatorKey] = []
    for point in _selected(VALIDATOR_ENTRY_POINT_GROUP, names):
        factory = point.load()
        value = factory() if callable(factory) else factory
        records: Iterable[Any]
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], ValidatorKey):
            records = (value,)
        elif isinstance(value, (tuple, list)):
            records = value
        else:
            raise DiscoveryError(f"validator entry point returned an invalid value: {point.name}")
        for record in records:
            if not isinstance(record, tuple) or len(record) != 2 or not isinstance(record[0], ValidatorKey):
                raise DiscoveryError(f"validator entry point returned an invalid record: {point.name}")
            registry.register(record[0], record[1])
            registered.append(record[0])
    return tuple(registered)


def load_installed_bundles(
    registry: ProviderBundleRegistry, names: Sequence[str]
) -> tuple[LoadedBundle, ...]:
    """Load declarative bundle paths from explicitly allowlisted installations."""

    loaded: list[LoadedBundle] = []
    for point in _selected(BUNDLE_ENTRY_POINT_GROUP, names):
        factory = point.load()
        value = factory() if callable(factory) else factory
        paths = value if isinstance(value, (tuple, list)) else (value,)
        if not paths or any(not isinstance(path, (str, Path)) for path in paths):
            raise DiscoveryError(f"bundle entry point returned an invalid path: {point.name}")
        for path in paths:
            loaded.append(registry.load(path))
    return tuple(loaded)
