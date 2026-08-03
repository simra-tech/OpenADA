"""Own the simulator's startup files whenever OpenADA binds a PDK.

``pdk_collateral`` refuses a *deck* that binds one PDK's collateral by hand.
That check reads the deck, and for a while that looked like the whole surface.
It is not. ngspice reads a startup file before it reads any deck --- the system
``spinit`` named by ``SPICE_SCRIPTS``, then ``./.spiceinit``, then
``$HOME/.spiceinit`` --- and a startup file can preload Verilog-A modules,
extend ``sourcepath`` and set compatibility modes. None of that is visible in
the deck, in the request, or in the result.

This is not hypothetical. IHP's PDK ships
``<pdk-root>/ihp-sg13g2/libs.tech/ngspice/.spiceinit``, and it reads:

    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/psp103.osdi'
    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/psp103_nqs.osdi'
    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/r3_cmc.osdi'
    osdi  '$PDK_ROOT/$PDK/libs.tech/ngspice/osdi/mosvar.osdi'

Those four modules are IHP's. The paths are not: they are *expanded at run time*
from ``$PDK_ROOT`` and ``$PDK``. On a workstation that installs it into the
user's home --- which is what the IIC-OSIC-Tools image does --- every ngspice
run inherits it, and a run OpenADA binds to sky130A inherits it with
``PDK=sky130A`` exported, because that is what a sky130A binding sets. The file
then names ``<pdk-root>/sky130A/libs.tech/ngspice/osdi/psp103.osdi``: IHP's
compact model, looked up inside SkyWater's tree, where it has never existed.

The observed consequence was four ``couldn't be loaded!`` lines per run, a
``simulation.native_error`` and an ``engineering: unknown`` verdict on a
simulation that had in fact converged and written a valid raw file. Every
sky130A run in the container failed that way, and the deck --- which OpenADA
itself derived --- was blameless.

The fix is the same principle as the rest of the binding: **the driver owns the
technology binding, therefore the driver owns the startup**. When ``--pdk``
binds a PDK, OpenADA writes the startup file itself, hands it to ngspice
explicitly and thereby suppresses every ambient one. What the simulator was
configured with becomes a retained, content-bound input rather than a property
of whoever's home directory the run happened to land in.

Nothing here runs a simulator. It renders text from a reviewed profile.
"""

from __future__ import annotations

from pathlib import Path

from .pdk_bindings import ResolvedPdkBinding


#: The file OpenADA writes beside a bound run's evidence. Named for what it is,
#: so a reviewer reading the evidence directory never wonders whose it was.
MANAGED_STARTUP_NAME = "openada-pdk.spiceinit"

#: Recorded on every bound run, so the evidence states the policy rather than
#: leaving a reader to infer it from an absent diagnostic.
MANAGED_STARTUP_PROVENANCE = (
    "The simulator's startup file was written by OpenADA from the reviewed "
    "binding profile and passed explicitly, so no ambient spinit or .spiceinit "
    "- including one an installed PDK ships into the user's home - could "
    "preload another technology's Verilog-A modules or extend the model search "
    "path for this run."
)


def managed_startup_text(resolved: ResolvedPdkBinding) -> str:
    """Return the complete ngspice startup OpenADA installs for a bound run.

    Deliberately close to empty. Every fact a bound deck needs - the ordered
    library prelude, the geometry unit convention, the temperature and the OSDI
    preloads - is emitted *into the deck* by ``bind_deck``, where it is visible
    to anyone reading the retained evidence. A startup file is the wrong place
    for any of it, which is exactly why inheriting somebody else's was so
    damaging. What is written here is only what a profile explicitly declares
    it cannot express in a deck.
    """

    binding = resolved.binding
    lines = [
        "* OpenADA-managed ngspice startup. Do not edit.",
        f"* pdk: {binding.pdk_id}  corner: {resolved.corner}",
        "*",
        "* This file exists so that ngspice reads no other one. Every card the",
        "* bound deck needs is in the bound deck; a startup file that reached",
        "* outside it would be invisible in the retained evidence.",
    ]
    for directive in binding.startup_directives:
        lines.append(directive)
    return "\n".join(lines) + "\n"


def write_managed_startup(directory: Path, resolved: ResolvedPdkBinding) -> Path:
    """Write the managed startup file and return its path."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANAGED_STARTUP_NAME
    path.write_text(managed_startup_text(resolved), encoding="utf-8")
    return path


#: The startup OpenADA installs for a behavioral-block OSDI run. A block-OSDI run
#: binds no PDK, so it exports no ``PDK_ROOT``/``PDK`` and declares no library
#: prelude - the reviewed ``.control pre_osdi`` cards and wrapper subckts are
#: emitted into the deck by ``osdi_compile.osdi_preload_prelude``. This file's
#: only job is the same hygiene the PDK path buys: reading it explicitly
#: suppresses every ambient ``spinit``/``.spiceinit``, so an installed PDK's
#: startup cannot preload another technology's Verilog-A/OSDI modules into a run
#: that is meant to load exactly the reviewed block modules and nothing else.
MANAGED_OSDI_STARTUP_NAME = "openada-osdi.spiceinit"

MANAGED_OSDI_STARTUP_PROVENANCE = (
    "The simulator's startup file was written by OpenADA as an empty managed "
    "startup and passed explicitly, so no ambient spinit or .spiceinit - "
    "including one an installed PDK ships into the user's home - could preload "
    "another technology's Verilog-A/OSDI modules or extend the model search "
    "path; the run loads exactly the reviewed, digest-bound block OSDI modules "
    "named by the deck's own pre_osdi cards."
)


def managed_osdi_startup_text() -> str:
    """Return the empty managed startup for a block-OSDI (no-PDK) run.

    A block-OSDI run binds no technology, so unlike ``managed_startup_text`` this
    carries no ``startup_directives`` at all. It exists solely so ngspice reads
    no ambient startup file; every OSDI card is in the deck.
    """

    return "\n".join(
        [
            "* OpenADA-managed ngspice startup for a behavioral-block OSDI run.",
            "* Do not edit.",
            "*",
            "* This file exists so that ngspice reads no other one. Every OSDI",
            "* preload the run needs is a reviewed, digest-bound pre_osdi card in",
            "* the bound deck; a startup file that reached outside it would be",
            "* invisible in the retained evidence.",
        ]
    ) + "\n"


def write_managed_osdi_startup(directory: Path) -> Path:
    """Write the empty managed OSDI startup file and return its path."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANAGED_OSDI_STARTUP_NAME
    path.write_text(managed_osdi_startup_text(), encoding="utf-8")
    return path


__all__ = [
    "MANAGED_OSDI_STARTUP_NAME",
    "MANAGED_OSDI_STARTUP_PROVENANCE",
    "MANAGED_STARTUP_NAME",
    "MANAGED_STARTUP_PROVENANCE",
    "managed_osdi_startup_text",
    "managed_startup_text",
    "write_managed_osdi_startup",
    "write_managed_startup",
]
