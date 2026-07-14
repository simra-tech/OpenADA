"""Reviewed built-in driver identities and semantic capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import __version__
from .discovery import DiscoveryManager
from .engines import NgspiceDriver, XyceDriver


CIRCUIT_SIMULATE_PROFILE = "openada.operation/circuit.simulate/v1alpha1"
SIMULATION_EVIDENCE_ASSERTION = (
    "openada.assertion/simulation.evidence.valid/v1alpha1"
)
TRANSIENT_FEATURE = "openada.feature/simulation.analysis.tran/v1alpha1"


@dataclass(frozen=True, slots=True)
class BuiltinDriver:
    alias: str
    driver_id: str
    version: str
    native_tool: str
    operation_profile: str
    assertion_profile: str
    features: tuple[str, ...]
    factory: Callable[[DiscoveryManager], object]


BUILTIN_DRIVERS: dict[str, BuiltinDriver] = {
    "ngspice": BuiltinDriver(
        alias="ngspice",
        driver_id="org.openada.driver.ngspice",
        version=__version__,
        native_tool="ngspice",
        operation_profile=CIRCUIT_SIMULATE_PROFILE,
        assertion_profile=SIMULATION_EVIDENCE_ASSERTION,
        features=(TRANSIENT_FEATURE,),
        factory=lambda discovery: NgspiceDriver(discovery=discovery),
    ),
    "xyce": BuiltinDriver(
        alias="xyce",
        driver_id="org.openada.driver.xyce",
        version=__version__,
        native_tool="xyce",
        operation_profile=CIRCUIT_SIMULATE_PROFILE,
        assertion_profile=SIMULATION_EVIDENCE_ASSERTION,
        features=(TRANSIENT_FEATURE,),
        factory=lambda discovery: XyceDriver(discovery=discovery),
    ),
}

BUILTIN_DRIVERS_BY_ID = {
    driver.driver_id: driver for driver in BUILTIN_DRIVERS.values()
}


def builtin_driver(selector: str) -> BuiltinDriver | None:
    """Resolve one reviewed alias or immutable driver identity."""

    return BUILTIN_DRIVERS.get(selector) or BUILTIN_DRIVERS_BY_ID.get(selector)


__all__ = [
    "BUILTIN_DRIVERS",
    "BUILTIN_DRIVERS_BY_ID",
    "BuiltinDriver",
    "CIRCUIT_SIMULATE_PROFILE",
    "SIMULATION_EVIDENCE_ASSERTION",
    "TRANSIENT_FEATURE",
    "builtin_driver",
]
