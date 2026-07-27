"""Refuse a deck that binds PDK collateral by hand instead of by profile.

OpenADA's contract is that the *driver* owns PDK glue: the corner library entry
point, its ordered prelude, the geometry unit convention and any Verilog-A
preload are properties of the technology, and ``pdk_bindings`` states them once
per PDK. A caller that pastes those cards into a deck bypasses that table, and
the failure mode is silent: ngspice does not know that a module path names a
different PDK than the library beside it, so the run either dies with an opaque
message or - worse - produces numbers from collateral nobody selected.

This module is the check that makes that impossible to do quietly. It reads the
bounded set of cards a deck can use to reach outside itself (``.lib``,
``.include``/``.inc`` and ``pre_osdi``) and reports typed findings:

``pdk.collateral.foreign``
    A reference that sits inside one registered PDK's tree but names a file
    another registered PDK ships. This is exactly the observed live failure:
    ``pre_osdi /foss/pdks/sky130A/libs.tech/ngspice/osdi/psp103.osdi`` applies
    IHP's PSP103 recipe to SkyWater sky130. It is refused before ngspice runs.

``pdk.collateral.missing``
    A reference whose file does not exist. A deck that binds nothing must never
    be run: the resulting evidence is about a circuit with no devices.

``pdk.collateral.conflict``
    A deck that already carries PDK cards handed to a ``--pdk`` binding. The
    driver owns the whole prelude; two preludes is not a merge, it is a race.

``pdk.collateral.hand_bound``
    Any ``pre_osdi`` card, or a ``.lib``/``.include`` reaching into a registered
    PDK's tree. An OSDI preload is never a property of a circuit: it exists
    because *this* PDK happens to ship a Verilog-A compact model, and the driver
    knows which PDKs do. A deck that names one has encoded a technology, which
    is the thing a role-based deck exists not to do. Refused, with the role
    form named.

``simulation.collateral.unmanaged``
    A ``.lib``/``.include`` that is not recognisably any registered PDK's. Not
    refused - OpenADA has no profile to offer instead - but reported, so the
    provenance of what bound the devices is never silent.

The refusals are unconditional. The one way past them is the explicit
``--unmanaged-collateral`` flag, which stamps the result with a permanent
provenance limitation saying the caller, not a reviewed profile, chose the
technology binding. Capability was never the problem; silence was.

Nothing here runs a simulator or reads a PDK's contents; it inspects paths and
the reviewed profile table only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Iterable, Mapping

from .pdk_bindings import REGISTRY, PdkBinding


#: The bounded card vocabulary a deck can use to reach outside itself. Anything
#: else in a SPICE deck is self-contained by construction.
_LIB_RE = re.compile(r"^\s*\.lib\s+(?P<path>\S+)(?:\s+(?P<section>\S+))?\s*$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^\s*\.inc(?:lude)?\s+(?P<path>\S+)\s*$", re.IGNORECASE)
_PRE_OSDI_RE = re.compile(r"^\s*pre_osdi\s+(?P<path>\S+)\s*$", re.IGNORECASE)
_OPTION_SCALE_RE = re.compile(r"^\s*\.option[s]?\b.*\bscale\s*=", re.IGNORECASE)

#: A malformed or hostile deck must not make this check unbounded.
MAX_INSPECTED_LINES = 100_000
MAX_REFERENCES = 256


@dataclass(frozen=True, slots=True)
class CollateralReference:
    """One card through which a deck reaches a file outside itself."""

    card: str
    raw_path: str
    line_number: int
    #: The reference resolved against the run directory, when that is possible.
    resolved: Path | None


@dataclass(frozen=True, slots=True)
class CollateralFinding:
    """One typed reason a deck's own collateral is not acceptable."""

    severity: str
    code: str
    message: str
    hint: str | None = None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def collateral_references(
    deck_text: str,
    *,
    workdir: Path | None = None,
) -> tuple[CollateralReference, ...]:
    """Return every outside-the-deck reference, resolved where possible."""

    references: list[CollateralReference] = []
    for number, line in enumerate(deck_text.splitlines(), start=1):
        if number > MAX_INSPECTED_LINES or len(references) >= MAX_REFERENCES:
            break
        stripped = line.lstrip()
        if not stripped or stripped.startswith("*"):
            continue
        for card, pattern in (
            ("lib", _LIB_RE),
            ("include", _INCLUDE_RE),
            ("pre_osdi", _PRE_OSDI_RE),
        ):
            match = pattern.match(line)
            if match is None:
                continue
            raw = _strip_quotes(match.group("path"))
            resolved: Path | None = None
            try:
                candidate = Path(raw)
                if candidate.is_absolute():
                    resolved = candidate
                elif workdir is not None:
                    resolved = (workdir / candidate).resolve()
            except (OSError, ValueError):
                resolved = None
            references.append(
                CollateralReference(
                    card=card,
                    raw_path=raw,
                    line_number=number,
                    resolved=resolved,
                )
            )
            break
    return tuple(references)


def declares_option_scale(deck_text: str) -> bool:
    """Return whether the deck installs its own geometry unit convention."""

    for number, line in enumerate(deck_text.splitlines(), start=1):
        if number > MAX_INSPECTED_LINES:
            break
        if _OPTION_SCALE_RE.match(line):
            return True
    return False


def _profile_basenames(binding: PdkBinding) -> frozenset[str]:
    """Return every file basename this PDK's reviewed profile declares.

    ``{corner}`` is substituted with each declared corner, because FreePDK45
    selects a corner by directory while everyone else selects a library section:
    the basename must be corner-independent either way.
    """

    names: set[str] = set()
    corners = binding.corners or ("",)
    for entry in binding.library_entries:
        for corner in corners:
            names.add(PurePosixPath(entry.relative_path.replace("{corner}", corner)).name)
    for relative in binding.osdi_relative_paths:
        names.add(PurePosixPath(relative).name)
    if binding.identity_relative_path:
        names.add(PurePosixPath(binding.identity_relative_path).name)
    return frozenset(name.lower() for name in names if name)


def collateral_basename_index() -> Mapping[str, frozenset[str]]:
    """Return ``pdk_id -> declared basenames``, derived from the profiles.

    Derived rather than maintained beside them, so it cannot drift from the
    table that actually binds a deck.
    """

    return {
        pdk_id: _profile_basenames(binding)
        for pdk_id, binding in REGISTRY.items()
        if binding.analog
    }


def _path_parts(raw_path: str, resolved: Path | None) -> tuple[str, ...]:
    """Return the path components of a reference, however it was written.

    A deck authored on one platform may be inspected on another, so both
    separator conventions are considered; this only ever feeds a name match.
    """

    parts: list[str] = []
    if resolved is not None:
        parts.extend(resolved.parts)
    parts.extend(PurePosixPath(raw_path).parts)
    parts.extend(PureWindowsPath(raw_path).parts)
    return tuple(parts)


def _owning_pdk(reference: CollateralReference) -> str | None:
    """Return the registered PDK whose tree a reference sits inside, if any."""

    parts = {part.lower() for part in _path_parts(reference.raw_path, reference.resolved)}
    for pdk_id, binding in REGISTRY.items():
        if binding.analog and pdk_id.lower() in parts:
            return pdk_id
    return None


def _basename(reference: CollateralReference) -> str:
    return PurePosixPath(reference.raw_path.replace("\\", "/")).name.lower()


def _shipping_pdks(basename: str, index: Mapping[str, frozenset[str]]) -> tuple[str, ...]:
    return tuple(
        sorted(pdk_id for pdk_id, names in index.items() if basename in names)
    )


#: What a deck is allowed to say about a device, stated once so every refusal
#: can name it. The vocabulary is the whole contract: a role and SI geometry.
ROLE_FORM_HINT = (
    "A netlist carries devices, not technologies. Write the device as a "
    "canonical role - nmos.core / pmos.core, and .svt / .lvt / .hvt / .io where "
    "a threshold or oxide flavour is meant - with SI geometry (W=2u L=130n), "
    "and pass --pdk <id> --pdk-root <dir>. The driver resolves the model name, "
    "the instance prefix, the parameter spelling, the geometry unit "
    "convention, the ordered library prelude, the corner and any Verilog-A "
    "preload from the reviewed profile for that PDK."
)


def inspect_deck_collateral(
    deck_text: str,
    *,
    workdir: Path | None = None,
    bound_pdk: str | None = None,
) -> tuple[CollateralFinding, ...]:
    """Return every typed finding about a deck's own PDK collateral.

    ``bound_pdk`` is the PDK the caller asked the driver to bind. When it is
    set, any hand-written collateral at all is a conflict: the driver emits the
    complete prelude, and a second one cannot be reconciled with it.
    """

    references = collateral_references(deck_text, workdir=workdir)
    index = collateral_basename_index()
    findings: list[CollateralFinding] = []

    if bound_pdk is not None:
        offenders = [
            f"{reference.card} {reference.raw_path} (line {reference.line_number})"
            for reference in references
        ]
        if declares_option_scale(deck_text):
            offenders.append("a .option scale card")
        if offenders:
            findings.append(
                CollateralFinding(
                    "error",
                    "pdk.collateral.conflict",
                    (
                        f"The deck binds PDK collateral by hand while requesting a "
                        f"{bound_pdk} binding: {', '.join(offenders[:5])}. The driver "
                        "emits the complete prelude for the named PDK - its library "
                        "entries in order, its geometry unit convention and any "
                        "Verilog-A preload - and a second, hand-written prelude "
                        "cannot be reconciled with it."
                    ),
                    hint=(
                        "Publish or pass the model-free deck and let --pdk own the "
                        "prelude, or drop --pdk and own the collateral entirely."
                    ),
                )
            )
        return tuple(findings)

    for reference in references:
        owner = _owning_pdk(reference)
        basename = _basename(reference)
        shippers = _shipping_pdks(basename, index)

        # Foreign is checked before every other classification, and for every
        # card. It is the *sharper* statement: not merely "this deck bound
        # collateral by hand" but "this deck applied one PDK's incantation to
        # another", which is a different mistake with a different correction.
        # Deciding it after the ``pre_osdi`` branch made the module's own
        # headline example - IHP's psp103.osdi preloaded from the sky130A tree -
        # report as merely hand-bound, which named the wrong failure.
        if owner is not None and basename not in index.get(owner, frozenset()):
            foreign = tuple(pdk for pdk in shippers if pdk != owner)
            if foreign:
                findings.append(
                    CollateralFinding(
                        "error",
                        "pdk.collateral.foreign",
                        (
                            f"Line {reference.line_number} reaches into the {owner} tree "
                            f"for {reference.raw_path!r}, but {basename!r} is collateral "
                            f"{' and '.join(foreign)} ships and {owner} does not. One "
                            "PDK's incantation was applied to another."
                        ),
                        hint=(
                            f"Every PDK's prelude differs. Pass --pdk {owner} and the "
                            "driver emits the entry points, preloads and unit "
                            f"convention {owner} actually needs."
                        ),
                    )
                )
                continue

        if reference.card == "pre_osdi":
            # An OSDI preload is never a fact about a circuit. It exists because
            # this particular PDK ships a Verilog-A compact model, and which
            # PDKs do is exactly what the profile table knows. A deck that
            # names one has encoded a technology.
            findings.append(
                CollateralFinding(
                    "error",
                    "pdk.collateral.hand_bound",
                    (
                        f"Line {reference.line_number} preloads the Verilog-A module "
                        f"{reference.raw_path!r} by hand. Whether a simulator needs an "
                        "OSDI preload, and which module, is a property of the "
                        "technology"
                        + (
                            f" - {' and '.join(shippers)} ship this one"
                            if shippers
                            else ""
                        )
                        + ", not of the circuit, and OpenADA resolves it from the "
                        "PDK profile."
                    ),
                    hint=ROLE_FORM_HINT,
                )
            )
            continue

        if reference.resolved is not None and not reference.resolved.exists():
            findings.append(
                CollateralFinding(
                    "error",
                    "pdk.collateral.missing",
                    (
                        f"Line {reference.line_number} binds {reference.raw_path!r}, "
                        "which does not exist. A deck that binds nothing simulates a "
                        "circuit with no devices; its evidence would describe that "
                        "circuit, not the intended one."
                    ),
                    hint=(
                        "Pass --pdk <id> --pdk-root <dir> and the driver resolves and "
                        "content-binds every file the technology needs, or correct the "
                        "path."
                    ),
                )
            )
            continue

        if owner is not None:
            findings.append(
                CollateralFinding(
                    "error",
                    "pdk.collateral.hand_bound",
                    (
                        f"Line {reference.line_number} binds {owner} collateral by hand: "
                        f"{reference.raw_path!r}. The corner entry point, its prelude "
                        "order, the device prefix, the parameter spelling and the "
                        "geometry unit convention are all properties of "
                        f"{owner} that this deck now restates - and restates only for "
                        f"{owner}, so the same deck cannot be asked the same question "
                        "in another technology."
                    ),
                    hint=ROLE_FORM_HINT,
                )
            )
            continue

        findings.append(
            CollateralFinding(
                "warning",
                "simulation.collateral.unmanaged",
                (
                    f"Line {reference.line_number} binds {reference.raw_path!r}, which "
                    "is not collateral any registered PDK profile describes. OpenADA "
                    "cannot state what technology this run was bound to."
                ),
                hint=(
                    "If this is an installed PDK, register a binding profile for it "
                    "and pass --pdk. Model collateral flattened into a single "
                    "self-contained card file can be passed with --models instead."
                ),
            )
        )

    return tuple(findings)


def blocking(findings: Iterable[CollateralFinding]) -> tuple[CollateralFinding, ...]:
    return tuple(finding for finding in findings if finding.severity == "error")


__all__ = [
    "ROLE_FORM_HINT",
    "CollateralFinding",
    "CollateralReference",
    "MAX_INSPECTED_LINES",
    "MAX_REFERENCES",
    "blocking",
    "collateral_basename_index",
    "collateral_references",
    "declares_option_scale",
    "inspect_deck_collateral",
]
