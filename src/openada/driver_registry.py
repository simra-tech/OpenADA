"""Reviewed built-in driver identities and semantic capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import __version__
from .discovery import DiscoveryManager
from .engines import NgspiceDriver, RTLTestDriver, XyceDriver


CIRCUIT_SIMULATE_PROFILE = "openada.operation/circuit.simulate/v1alpha2"
SIMULATION_EVIDENCE_ASSERTION = (
    "openada.assertion/simulation.evidence.valid/v1alpha1"
)
OPERATING_POINT_FEATURE = "openada.feature/simulation.analysis.op/v1alpha1"
DC_SWEEP_FEATURE = "openada.feature/simulation.analysis.dc/v1alpha1"
AC_SWEEP_FEATURE = "openada.feature/simulation.analysis.ac/v1alpha1"
TRANSIENT_FEATURE = "openada.feature/simulation.analysis.tran/v1alpha1"
ANALYSIS_FEATURES = {
    "op": OPERATING_POINT_FEATURE,
    "dc": DC_SWEEP_FEATURE,
    "ac": AC_SWEEP_FEATURE,
    "tran": TRANSIENT_FEATURE,
}
RTL_TEST_PROFILE = "openada.operation/rtl.test/v1alpha1"
RTL_TEST_ASSERTION = "openada.assertion/rtl.self-test.passes/v1alpha1"
RTL_TEST_BACKEND_FEATURE = "openada.feature/rtl.test.backend/v1alpha1"


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
        features=(
            OPERATING_POINT_FEATURE,
            DC_SWEEP_FEATURE,
            AC_SWEEP_FEATURE,
            TRANSIENT_FEATURE,
        ),
        factory=lambda discovery: NgspiceDriver(discovery=discovery),
    ),
    "xyce": BuiltinDriver(
        alias="xyce",
        driver_id="org.openada.driver.xyce",
        version=__version__,
        native_tool="xyce",
        operation_profile=CIRCUIT_SIMULATE_PROFILE,
        assertion_profile=SIMULATION_EVIDENCE_ASSERTION,
        features=(DC_SWEEP_FEATURE, AC_SWEEP_FEATURE, TRANSIENT_FEATURE),
        factory=lambda discovery: XyceDriver(discovery=discovery),
    ),
    "iverilog-rtl-test": BuiltinDriver(
        alias="iverilog-rtl-test",
        driver_id="org.openada.driver.iverilog.rtl-test",
        version=__version__,
        native_tool="iverilog",
        operation_profile=RTL_TEST_PROFILE,
        assertion_profile=RTL_TEST_ASSERTION,
        features=(RTL_TEST_BACKEND_FEATURE,),
        factory=lambda discovery: RTLTestDriver(discovery=discovery),
    ),
    "verilator-rtl-test": BuiltinDriver(
        alias="verilator-rtl-test",
        driver_id="org.openada.driver.verilator.rtl-test",
        version=__version__,
        native_tool="verilator",
        operation_profile=RTL_TEST_PROFILE,
        assertion_profile=RTL_TEST_ASSERTION,
        features=(RTL_TEST_BACKEND_FEATURE,),
        factory=lambda discovery: RTLTestDriver(discovery=discovery),
    ),
}

BUILTIN_DRIVERS_BY_ID = {
    driver.driver_id: driver for driver in BUILTIN_DRIVERS.values()
}


def builtin_driver(selector: str) -> BuiltinDriver | None:
    """Resolve one reviewed alias or immutable driver identity."""

    return BUILTIN_DRIVERS.get(selector) or BUILTIN_DRIVERS_BY_ID.get(selector)


def analysis_feature(analysis_type: str) -> str | None:
    """Return the immutable optional feature for one closed analysis type."""

    return ANALYSIS_FEATURES.get(analysis_type)


__all__ = [
    "AC_SWEEP_FEATURE",
    "ANALYSIS_FEATURES",
    "BUILTIN_DRIVERS",
    "BUILTIN_DRIVERS_BY_ID",
    "BuiltinDriver",
    "CIRCUIT_SIMULATE_PROFILE",
    "DC_SWEEP_FEATURE",
    "OPERATING_POINT_FEATURE",
    "SIMULATION_EVIDENCE_ASSERTION",
    "TRANSIENT_FEATURE",
    "analysis_feature",
    "builtin_driver",
]
