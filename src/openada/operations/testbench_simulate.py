"""Deprecated alias for the one simulation semantic.

``testbench.simulate/v1alpha1`` was a second operation that meant "simulate".
It existed because ``circuit.simulate`` had not yet been *implemented* for a
published artifact target or a PDK-bound model source - not because the
engineering act was different. Two operations for one act is how a live request
took the path that consulted no PDK binding at all and reported a hand-computed
number as measured.

The semantics now live in :mod:`openada.operations.simulate` under
``circuit.simulate/v1alpha2``, whose published contract already declared the
``artifact`` locator and the ``pdk``/``corner`` configuration roles this module
used to own separately.

What survives here is an alias, not a semantic: it emits one deprecation
diagnostic naming the unified form and delegates. Its result *is* a
``circuit.simulate`` result, so no caller can observe two semantics. The alias
exists because published skills and deployed images still spell the old verb,
and turning those calls into an argparse error would replace a correct answer
with no answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contract import diagnostic
from ..discovery import DiscoveryManager
from ..driver_registry import (
    CIRCUIT_SIMULATE_PROFILE,
    SIMULATION_EVIDENCE_ASSERTION,
    TESTBENCH_EVIDENCE_ASSERTION,
    TESTBENCH_SIMULATE_PROFILE,
    builtin_driver,
)
from .simulate import (
    MAX_DISPATCHED_ANALYSES,
    OPERATION_NAME,
    SUPPORTED_BACKENDS,
    simulate,
)


#: The one diagnostic this alias adds. It is a warning, not an error: the run
#: is honoured in full, and the caller is told exactly what to spell instead.
DEPRECATION_CODE = "simulation.operation.deprecated"


def deprecation_diagnostic() -> dict[str, Any]:
    return diagnostic(
        "warning",
        DEPRECATION_CODE,
        (
            "testbench-simulate is a deprecated alias. Simulation is one semantic: "
            f"{CIRCUIT_SIMULATE_PROFILE}. `openada simulate` accepts a published "
            "Simra artifact or a bare deck, binds an installed PDK with --pdk, and "
            "splits a multi-analysis testbench itself. This result is a "
            "circuit.simulate result."
        ),
        hint=(
            "openada simulate <artifact-or-deck> --backend ngspice "
            "[--pdk <id> --pdk-root <dir>] --output-dir <dir>"
        ),
    )


def resolve_testbench_driver(backend: object) -> Any:
    """Resolve the simulation driver for one backend.

    There is now exactly one driver identity per backend. The separate
    ``org.openada.driver.simra.testbench.*`` identities are retired: a driver
    that dispatches a published artifact and a driver that runs a bare deck were
    always the same ngspice.
    """

    if not isinstance(backend, str) or backend not in SUPPORTED_BACKENDS:
        return None
    return builtin_driver(backend)


def simulate_testbench(
    artifact_file: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager,
    backend: str = "ngspice",
    models_file: str | Path | None = None,
    pdk: str | None = None,
    pdk_root: str | Path | None = None,
    corner: str | None = None,
    timeout: float = 600.0,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Deprecated. Delegate to :func:`openada.operations.simulate.simulate`."""

    return simulate(
        artifact_file,
        output_dir,
        discovery=discovery,
        backend=backend,
        models_file=models_file,
        pdk=pdk,
        pdk_root=pdk_root,
        corner=corner,
        timeout=timeout,
        request_id=request_id,
        extra_diagnostics=[deprecation_diagnostic()],
    )


__all__ = [
    "DEPRECATION_CODE",
    "MAX_DISPATCHED_ANALYSES",
    "OPERATION_NAME",
    "SIMULATION_EVIDENCE_ASSERTION",
    "SUPPORTED_BACKENDS",
    "TESTBENCH_EVIDENCE_ASSERTION",
    "TESTBENCH_SIMULATE_PROFILE",
    "deprecation_diagnostic",
    "resolve_testbench_driver",
    "simulate_testbench",
]
