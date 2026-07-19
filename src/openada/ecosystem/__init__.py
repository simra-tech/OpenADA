"""Vendor-neutral provider ecosystem contracts and bounded host services."""

from .canonical import (
    CanonicalJSONError,
    RequestBindingError,
    RequestIdentityRegistry,
    bind_request,
    canonical_json_bytes,
    canonical_request_bytes,
)
from .bundles import BundleError, LoadedBundle, ProviderBundleRegistry
from .conformance import ConformanceCase, ConformanceError, ConformanceSuite
from .contracts import ContractError, SchemaCatalog
from .discovery import (
    BUNDLE_ENTRY_POINT_GROUP,
    VALIDATOR_ENTRY_POINT_GROUP,
    DiscoveryError,
    load_installed_bundles,
    register_installed_validators,
)
from .fakes import FakeBackendError, FakeOperationValidator, FakeProviderBackend
from .locators import LocatorError, LocatorResolver, NativeObjectStore, ResolvedLocator
from .registries import (
    CapabilityRegistry,
    DriverMappingRegistry,
    OperationValidatorRegistry,
    ValidationReport,
    ValidatorKey,
)
from .results import ResultSemanticError, validate_result_semantics
from .transports import (
    AgentSessionTransport,
    DeterministicFakeScheduler,
    SessionHandle,
    TransportError,
)

__all__ = [
    "AgentSessionTransport",
    "BundleError",
    "CanonicalJSONError",
    "CapabilityRegistry",
    "ConformanceCase",
    "ConformanceError",
    "ConformanceSuite",
    "ContractError",
    "DiscoveryError",
    "DriverMappingRegistry",
    "DeterministicFakeScheduler",
    "FakeBackendError",
    "FakeOperationValidator",
    "FakeProviderBackend",
    "LoadedBundle",
    "LocatorError",
    "LocatorResolver",
    "NativeObjectStore",
    "OperationValidatorRegistry",
    "ProviderBundleRegistry",
    "RequestBindingError",
    "RequestIdentityRegistry",
    "ResolvedLocator",
    "ResultSemanticError",
    "SchemaCatalog",
    "SessionHandle",
    "TransportError",
    "ValidationReport",
    "ValidatorKey",
    "BUNDLE_ENTRY_POINT_GROUP",
    "VALIDATOR_ENTRY_POINT_GROUP",
    "bind_request",
    "canonical_json_bytes",
    "canonical_request_bytes",
    "load_installed_bundles",
    "register_installed_validators",
    "validate_result_semantics",
]
