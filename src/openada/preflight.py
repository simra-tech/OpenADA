"""Fixed, operation-level assertions for bounded first-run preflight."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreflightSpec:
    assertion: str
    operation: str
    tool: str
    pdk_applicable: bool
    startup_policy: str
    startup_options: tuple[str, ...] = ()


PREFLIGHT_SPECS: dict[str, PreflightSpec] = {
    "schematic-netlist-generated": PreflightSpec(
        assertion="schematic-netlist-generated",
        operation="netlist",
        tool="xschem",
        pdk_applicable=True,
        startup_policy="explicit-rcfile-when-required",
        startup_options=("--rcfile",),
    ),
    "spice-analysis-evidence-valid": PreflightSpec(
        assertion="spice-analysis-evidence-valid",
        operation="simulate",
        tool="ngspice",
        pdk_applicable=True,
        startup_policy="native-default-or-operation-explicit",
        startup_options=("--init-file", "--system-init-file"),
    ),
    "drc-clean": PreflightSpec(
        assertion="drc-clean",
        operation="drc",
        tool="klayout",
        pdk_applicable=True,
        startup_policy="configuration-files-and-implicit-macros-disabled",
    ),
    "lvs-match": PreflightSpec(
        assertion="lvs-match",
        operation="lvs",
        tool="netgen",
        pdk_applicable=True,
        startup_policy="explicit-setup-required",
        startup_options=("--setup",),
    ),
    "rtl-structural-check-passes": PreflightSpec(
        assertion="rtl-structural-check-passes",
        operation="rtl-check",
        tool="yosys",
        pdk_applicable=False,
        startup_policy="no-startup-selector-in-contract",
    ),
}
