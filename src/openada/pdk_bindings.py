"""Per-PDK binding profiles that turn a model-free Simra deck into a runnable one.

Simra publishes a deliberately model-free deck in **one canonical form**: every
MOS is a SPICE ``M`` card, every geometry is an SI-valued SPICE number, and the
parameter keys are always ``W``/``L``/``M``/``NF``. Nothing in that deck is
PDK-specific except the model token, and even that may be written as a
technology-independent *device role* (``nmos.core``).

Whether such a card is runnable depends entirely on the target PDK, and the
differences are not cosmetic. Verified against installed collateral and real
ngspice 45.2 runs:

* **IHP SG13G2** ships ``sg13_lv_nmos`` as a ``.subckt`` whose finger count is
  ``ng``; its PSP103 core is Verilog-A and must be preloaded through OSDI.
  Geometry is SI.
* **SkyWater sky130A** ships ``sky130_fd_pr__nfet_01v8`` as a ``.subckt``, and
  its own corner collateral installs ``.option scale=1.0u``
  (``libs.tech/ngspice/all.spice:2``, reached from ``corners/<corner>.spice``).
  Every instance geometry must therefore be written as a **plain micron
  number** -- ``W=2 L=0.15``, not ``W=2u L=0.15u``. An SI-valued card is scaled
  by a further 1e-6, lands outside every model bin, and ngspice rejects it with
  the opaque ``could not find a valid modelname``. Its documented ngspice flow
  also states ``.option wnflag=1`` so multi-finger bin selection uses ``W/NF``.
* **GlobalFoundries gf180mcuD** ships subcircuits with SI geometry, but its
  corner library references global switches (``fnoicor``, ``sw_stat_global``,
  ...) that live in a *separate* file which must be included first. Without it
  every model card fails to evaluate.
* **FreePDK45** ships flat BSIM4 ``.model`` cards as HSPICE ``.inc`` files with
  no library sections at all: the corner is selected by *directory*, and the
  ``M`` prefix Simra already emits is correct.

So the device prefix, the parameter spelling, the geometry unit convention, the
ordered library prelude, the corner-selection mechanism, the OSDI preload and
the model vocabulary are all *properties of the PDK*, not constants. Encoding
them here keeps that knowledge in one reviewed table instead of spreading it
across agent instructions, where every new PDK becomes a new class of silent
failure.

This module owns the binding only. It resolves one named PDK against a root,
content-binds every file it will reference, and rewrites one deck. It runs no
simulator and makes no engineering claim.
"""

from __future__ import annotations

import atexit
import dataclasses
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence

from .contract import FileRecordError, file_record, stable_regular_file


#: Simra emits one MOS per line as ``<name> <d> <g> <s> <b> <model> <k=v>...``.
#: The instance token always carries SPICE's ``M`` prefix
#: (``simra/plugins/schematic/compiler/netlist_v2.py``).
MOS_CARD_RE = re.compile(
    r"^(?P<name>[Mm][^\s]*)"
    r"(?P<nodes>(?:[ \t]+[^\s]+){4})"
    r"[ \t]+(?P<model>[^\s=]+)"
    r"(?P<params>(?:[ \t]+[^\s]+=[^\s]+)*)"
    r"[ \t]*$"
)
PARAMETER_RE = re.compile(r"(?P<key>[^\s=]+)=(?P<value>[^\s]+)")
UNRESOLVED_TOKEN_RE = re.compile(r"\{SIMRA_UNRESOLVED_[A-Za-z0-9_]+\}")
_END_CARD_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE)
_CORNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RAW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: A saved net reaches both ``.save`` and ``write ... v(<net>)``.  Keep the
#: grammar aligned with the published Simra net-name grammar; parentheses or
#: whitespace here would escape the generated vector expression.
_SAVED_NET_RE = re.compile(r"^[A-Za-z0-9_.:+$\[\]-]{1,256}$")
#: An explicitly retained current names the already-emitted voltage-source
#: card, not a native expression supplied by the caller.
_EMITTED_VSOURCE_NAME_RE = re.compile(r"^[Vv][A-Za-z0-9_.$#+-]*$")
#: An independent voltage source at the top of the deck. ngspice gives each one
#: a branch-current vector, and that vector is the only thing in the whole
#: pipeline carrying amperes.
_VSOURCE_CARD_RE = re.compile(r"^(?P<name>[Vv][A-Za-z0-9_.$#+-]*)[ \t]+\S")
_SUBCKT_OPEN_RE = re.compile(r"^\s*\.subckt\b", re.IGNORECASE)
_SUBCKT_CLOSE_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)
#: ``.save`` does not merely select what is written: it selects what ngspice
#: *computes*. A branch current absent from this card cannot be written even by
#: name, and ngspice fails the whole ``write`` with "no writable vector found".
_SAVE_CARD_RE = re.compile(r"^(?P<head>\s*\.save\b)(?P<rest>.*?)(?P<eol>\r?\n?)$", re.IGNORECASE)
#: One complex vector per source is negligible beside a saved net; this only
#: bounds a pathological deck.
MAX_PROBED_SOURCES = 16

#: One SPICE numeric literal: a number, an optional engineering suffix, and any
#: trailing alphabetic noise SPICE ignores (``1uF`` is one microfarad).
SPICE_NUMBER_RE = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>meg|mil|[tgkmunpf])?"
    r"(?P<trailing>[A-Za-z]*)$",
    re.IGNORECASE,
)
_SPICE_SCALE: Mapping[str, Decimal] = {
    "": Decimal(1),
    "t": Decimal("1e12"),
    "g": Decimal("1e9"),
    "meg": Decimal("1e6"),
    "k": Decimal("1e3"),
    "mil": Decimal("25.4e-6"),
    "m": Decimal("1e-3"),
    "u": Decimal("1e-6"),
    "n": Decimal("1e-9"),
    "p": Decimal("1e-12"),
    "f": Decimal("1e-15"),
}

#: A PDK entry point can fan out substantially: sky130's selected corner, for
#: example, includes the device, passive and specialised-cell model files it
#: activates.  Keep the complete closure bounded without retaining the old
#: twelve-file fiction that omitted those transitive inputs.
MAX_BOUND_FILES = 512
MAX_CLOSURE_EDGES = 1_024
MAX_CLOSURE_DEPTH = 64
MAX_PDK_FILE_BYTES = 256 * 1024 * 1024
MAX_PDK_SNAPSHOT_BYTES = 512 * 1024 * 1024

PDK_SNAPSHOT_SCHEMA = "openada.pdk-snapshot/v1"
_SNAPSHOT_DOMAIN = b"openada.pdk-snapshot/v1\0"
_CLOSURE_DOMAIN = b"openada.pdk-closure/v1\0"
_INCLUDE_DIRECTIVES = frozenset((".include", ".inc"))
_NAMESPACE_DIRECTIVES = frozenset((".model", ".subckt", ".global"))

#: The canonical device roles a Simra deck may name instead of a PDK model. The
#: vocabulary is deliberately tiny: it covers what a model-free schematic can
#: actually mean without knowing the technology.
DEVICE_ROLES = (
    "nmos.core",
    "pmos.core",
    "nmos.svt",
    "pmos.svt",
    "nmos.lvt",
    "pmos.lvt",
    "nmos.hvt",
    "pmos.hvt",
    "nmos.io",
    "pmos.io",
)

#: ``svt`` (standard threshold) and ``core`` name the same device. Both spellings
#: are in circulation - a designer says "SVT", a PDK says "core" or "01v8" - and
#: an author should not have to know which one a given profile chose. The alias
#: is resolved before any profile is consulted, so every PDK gets it for free.
ROLE_SYNONYMS: Mapping[str, str] = {
    "nmos.svt": "nmos.core",
    "pmos.svt": "pmos.core",
}


def canonical_role(role: str) -> str:
    """Return the profile-facing spelling of one canonical device role."""

    return ROLE_SYNONYMS.get(role, role)


#: Non-MOS canonical roles. Each names a device family a paper schematic can
#: mean without knowing the technology; the per-PDK ``device_cards`` table
#: says how (and whether) a given PDK spells it. Ideal R/C/L cards — a plain
#: numeric third token — are never touched by binding.
DIODE_ROLES = ("diode.core",)
BJT_ROLES = ("bjt.npn", "bjt.pnp")
RESISTOR_ROLES = ("resistor.poly",)
CAPACITOR_ROLES = ("cap.mim",)
EXTENDED_DEVICE_ROLES = DIODE_ROLES + BJT_ROLES + RESISTOR_ROLES + CAPACITOR_ROLES

#: Canonical node counts per leading card letter for the extended roles.
#: BJTs carry an explicit substrate (C B E SUB) and physical resistors an
#: explicit body (n1 n2 BODY) — exactly the MOS-bulk precedent: the author
#: states the tie, the binder never invents a global node.
_EXTENDED_CARD_NODES = {"d": 2, "q": 4, "r": 3, "c": 2}
#: Which roles each card letter may carry.
_EXTENDED_CARD_ROLES = {
    "d": frozenset(DIODE_ROLES),
    "q": frozenset(BJT_ROLES),
    "r": frozenset(RESISTOR_ROLES),
    "c": frozenset(CAPACITOR_ROLES),
}


#: The junction-geometry keys. A PDK either computes them from ``w`` and the
#: finger count when they are absent, or treats absence as zero - and zero
#: junction area removes every drain/source junction capacitance from the
#: answer, which is invisible at DC and material in transient and AC.
_JUNCTION_PARAMETERS = frozenset(("ad", "as", "pd", "ps"))

#: A device instance line that starts with ``M`` and names a canonical role but
#: does not have exactly four nodes. sky130A ships 5-terminal isolated devices
#: (``sky130_fd_pr__nfet_20v0_iso d g s b sub``), so "every MOS has four nodes"
#: is a property of the six devices currently mapped, not of the domain.
_ROLE_TOKEN_RE = re.compile(
    r"(?:^|[ \t])(?P<role>[np]mos\.[a-z]+)(?:[ \t]|$)"
)


class PdkBindingError(Exception):
    """One bounded, typed reason a deck cannot be bound to a PDK."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PdkLibraryEntry:
    """One card of the ordered prelude a PDK needs before any device binds.

    ``relative_path`` and ``section`` may both carry ``{corner}``: a PDK selects
    a corner either by library section (IHP, sky130, gf180mcu) or by directory
    (FreePDK45), and the profile must be able to say which.
    """

    relative_path: str
    #: ``"lib"`` emits ``.lib <path> <section>``; ``"include"`` emits
    #: ``.include <path>``. A bare ``.include`` of a sectioned corner library
    #: fails with "unimplemented dot command '.lib'" because no section is ever
    #: selected, so the distinction is load-bearing.
    form: str = "lib"
    section: str | None = "{corner}"
    #: Extended-role families this entry exists for. Empty means always load.
    #: A tagged entry is loaded only when the deck actually uses one of these
    #: roles — the collateral for unused families would otherwise install
    #: hundreds of global parameter/model names (gf180) or generic model
    #: names like ``darea`` (ihp diodes.lib) into every MOS-only deck.
    families: tuple[str, ...] = ()

    def resolve(self, corner: str) -> tuple[str, str | None]:
        relative = self.relative_path.replace("{corner}", corner)
        section = self.section.replace("{corner}", corner) if self.section else None
        return relative, section


@dataclass(frozen=True, slots=True)
class DeviceGeometry:
    """The geometry envelope the *simulator* enforces for one device role.

    These are model-binning extents read out of the PDK's own model cards, in
    metres. They are a necessary, not sufficient, condition: the bins tile a
    grid, and this records its bounding box. Their purpose is to turn ngspice's
    opaque ``could not find a valid modelname`` into a diagnostic that names the
    device, the offending dimension and the legal range.
    """

    l_min: str
    l_max: str
    w_min: str
    w_max: str

    def check(self, *, dimension: str, value: Decimal) -> str | None:
        low, high = (
            (Decimal(self.l_min), Decimal(self.l_max))
            if dimension == "l"
            else (Decimal(self.w_min), Decimal(self.w_max))
        )
        if value < low or value > high:
            # Rendered in scientific notation: a geometry diagnostic is read by
            # a human comparing it against a datasheet, not by a parser.
            return (
                f"{dimension}={float(value):.4g} m is outside the model-binning "
                f"range [{float(low):.4g}, {float(high):.4g}] m"
            )
        return None


@dataclass(frozen=True, slots=True)
class PdkDeviceCard:
    """How one PDK spells one extended-role device card.

    ``card_kind`` is ``"subckt"`` (the instance becomes an ``x``-prefixed
    subcircuit call) or ``"model"`` (the native card letter stays and the
    model name is substituted). The canonical card states every terminal
    explicitly — BJTs a substrate, physical resistors a body — and
    ``emit_nodes`` says how many of those (in order) the PDK device accepts;
    a canonical terminal past that count is dropped-and-reported, never
    silently rewired to an invented global node. ``parameter_names`` maps
    canonical lowercase keys to the PDK spelling; unmapped keys are
    dropped-and-reported exactly like the MOS path.
    ``geometry_parameters`` are the canonical keys holding lengths that
    rescale by the binding's geometry divisor; ``area_parameters`` hold areas
    and rescale by its square, exactly like the MOS path.
    ``companion_parameters`` emit one canonical key under *additional* PDK
    spellings with the same value: several subcircuits read a separate
    mismatch-scaling parameter (sky130 ``mult``/``mf``, gf180 pnp ``par``)
    that the hierarchical instance ``m`` does not reach, so canonical M must
    be stated in both places or Monte-Carlo mismatch scales wrongly. Each
    companion is only declared where the subckt body verifiably reads it.
    """

    model: str
    card_kind: str
    emit_nodes: int | None = None
    parameter_names: Mapping[str, str] = field(default_factory=dict)
    geometry_parameters: frozenset[str] = frozenset()
    area_parameters: frozenset[str] = frozenset()
    companion_parameters: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class PdkBinding:
    """One reviewed description of how a PDK expects a MOS card to be written."""

    pdk_id: str
    display_name: str
    #: ``"x"`` when the PDK ships its devices as subcircuits, ``"m"`` when it
    #: ships plain ``.model`` cards and Simra's emitted prefix is already right.
    device_prefix: str
    #: Simra parameter key (lowercased) -> the PDK's spelling. A key absent from
    #: this mapping is dropped rather than passed through, because an unknown
    #: parameter is a hard ngspice error, not a warning.
    parameter_names: Mapping[str, str]
    #: The ordered prelude, emitted before the deck body.
    library_entries: tuple[PdkLibraryEntry, ...]
    corners: tuple[str, ...]
    default_corner: str
    #: Canonical device role -> the model or subcircuit name this PDK ships.
    #: Also the alias source: a deck naming another PDK's model for the same
    #: role is translated through the union of every registered profile.
    device_models: Mapping[str, str]
    #: Extended-role card mechanics (diode/BJT/PDK resistor/PDK capacitor).
    #: Keys must also appear in ``device_models`` so translation, aliasing and
    #: the unsupported-role refusal work identically for every family. Their
    #: collateral binds its TYPICAL section regardless of the chosen MOS
    #: corner in this revision; corner mapping for non-MOS classes is future
    #: vocabulary work, not silently improvised here.
    device_cards: Mapping[str, "PdkDeviceCard"] = field(default_factory=dict)
    #: The value of ``.option scale`` this PDK's own collateral installs, as a
    #: decimal string. Simra's SI geometry is divided by it, so ``1`` means the
    #: deck's numbers pass through untouched and ``1e-6`` means the PDK expects
    #: plain micron numbers.
    geometry_scale: str = "1"
    #: Additional ngspice ``.option`` assignments the reviewed PDK flow
    #: requires in every composed deck. Values omit the ``.option`` prefix so
    #: the binding owns the card spelling and placement.
    ngspice_options: tuple[str, ...] = ()
    #: Simra parameter keys whose value is a length and must be rescaled.
    geometry_parameters: frozenset[str] = frozenset(("w", "l"))
    #: Role -> simulator-enforced geometry envelope, where the PDK bins.
    device_geometry: Mapping[str, DeviceGeometry] = field(default_factory=dict)
    #: Role -> the supply this device family is characterised for, in volts.
    #: A role-based deck names a device, not a bias, so the same deck at
    #: ``VDD = 1.2`` is nominal on a 1.2 V process, 33 % under-driven on a 1.8 V
    #: one and 2.75x under-driven on a 3.3 V one. The driver cannot rewrite the
    #: deck's sources - that is the experiment - but it must never let the
    #: difference be invisible.
    nominal_supply_v: Mapping[str, str] = field(default_factory=dict)
    #: The temperature this PDK's model cards were extracted at, in Celsius.
    #: No installed PDK sets ``.temp`` itself, so every deck silently runs at
    #: ngspice's 27 C default - which is the extraction point for some of them
    #: and not for others.
    model_tnom_c: str | None = None
    #: The temperature the bound deck states explicitly. Stating it means a
    #: reviewer never has to know a simulator default to read the evidence.
    simulation_temperature_c: str = "27"
    #: How this PDK treats omitted junction geometry. ``"auto"`` means the
    #: device computes ``ad``/``as``/``pd``/``ps`` from ``w`` and the finger
    #: count; ``"zero"`` means omitted junction geometry really is zero, and
    #: every junction capacitance is then absent from the answer.
    junction_geometry: str = "zero"
    #: Simra parameter keys whose value is an *area* and therefore rescales by
    #: the square of ``geometry_scale``. Perimeters rescale linearly and belong
    #: in ``geometry_parameters``.
    area_parameters: frozenset[str] = frozenset()
    #: Roles a corner selection actually skews. ``None`` means every role.
    #: gf180mcuD's ``ff``/``ss``/``fs``/``sf`` sections select skewed 3.3 V
    #: devices but *typical* 6 V devices, so a deck mixing core and IO roles at
    #: a non-typical corner gets a mixed-corner answer.
    corner_skewed_roles: tuple[str, ...] | None = None
    #: Per-corner overrides of ``corner_skewed_roles``: which roles a specific
    #: corner skews when that differs from the binding-wide set. sky130's sf
    #: and fs corner files include the *typical* NPN (corners/sf.spice:29,
    #: fs.spice:29 select npn_05v5__t) while ff/ss select __f/__s, so "the
    #: NPN follows the corner" is a per-corner fact, not a per-PDK one.
    corner_skewed_role_overrides: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    #: Verilog-A modules ngspice must load before any device binds. Empty for a
    #: PDK whose devices are built-in models.
    osdi_relative_paths: tuple[str, ...] = ()
    #: Relative path of a PDK identity/version file, recorded when present.
    identity_relative_path: str | None = None
    #: Simulator startup lines this PDK genuinely cannot express in a deck.
    #: Empty for every reviewed profile so far, and that is the point: OpenADA
    #: writes the startup file itself precisely so nobody else's can apply, so
    #: anything listed here has to earn its place by being unstateable in the
    #: deck. See ``pdk_startup``.
    startup_directives: tuple[str, ...] = ()
    #: False for a platform that ships no transistor models at all. Such a
    #: platform is registered so that asking for it fails with a reason rather
    #: than "unknown PDK".
    analog: bool = True
    unsupported_reason: str = ""
    notes: str = ""

    @property
    def geometry_divisor(self) -> Decimal:
        return Decimal(self.geometry_scale)

    def skewed_roles_for(self, corner: str) -> tuple[str, ...] | None:
        """Return the roles this specific corner skews (None = every role)."""

        if self.corner_skewed_roles is None:
            return None
        return self.corner_skewed_role_overrides.get(
            corner, self.corner_skewed_roles
        )

    def resolved_corner(self, corner: str | None) -> str:
        if corner is None:
            return self.default_corner
        if not _CORNER_RE.match(corner):
            raise PdkBindingError(
                "pdk.corner.invalid",
                f"The corner name {corner!r} is not a bounded library section token.",
            )
        if corner not in self.corners:
            raise PdkBindingError(
                "pdk.corner.unknown",
                f"{self.pdk_id} does not declare the corner {corner!r}.",
                hint=f"Declared corners: {', '.join(self.corners)}.",
            )
        return corner


#: The one Decimal context every binding-path computation runs under. The
#: ambient (caller-controlled) context must never shape a bound deck: the
#: default 28-digit precision rounds long authored significands before they
#: reach the wide formatter, and a hostile ambient Emax (e.g. 9) turns an
#: ordinary ``1e60`` into an arithmetic error. 200 digits comfortably covers
#: the canonical 1e-60..1e60 magnitude window squared by an area derivation
#: and scaled by a unit divisor; the exponent range is the decimal default,
#: stated explicitly so no caller can narrow it.
_BINDING_DECIMAL_CONTEXT = Context(prec=200, Emax=999_999, Emin=-999_999)


def _format_number(value: Decimal) -> str:
    """Render one Decimal as a SPICE literal without losing exactness.

    Rendering runs under the binding's own wide context: the default
    28-digit context makes ``quantize`` raise InvalidOperation for any value
    past ~1e28, and inputs up to the canonical 1e60 magnitude window (squared
    by an area derivation, further scaled by a unit divisor) are legitimate.
    A value even that context cannot render is refused typed, never allowed
    to escape as a bare decimal exception.
    """

    try:
        with localcontext(_BINDING_DECIMAL_CONTEXT):
            normalized = value.normalize()
            exponent = normalized.as_tuple().exponent
            if isinstance(exponent, int) and exponent > 0:
                # ``normalize`` renders 100 as ``1E+2``; legal SPICE, but
                # unreadable in a deck a human may have to review.
                return str(normalized.quantize(Decimal(1)))
            return format(normalized, "f")
    except ArithmeticError as exc:
        raise PdkBindingError(
            "pdk.parameter.invalid",
            f"The numeric value {value} is too extreme to render as a SPICE "
            "literal.",
            hint="Keep device scalars within 1e-60..1e60 SI.",
        ) from exc


def _rescaled(si: Decimal, divisor: Decimal, *, squared: bool) -> Decimal:
    """Divide one SI value by the PDK's unit divisor, context-independent.

    Areas rescale by the divisor squared, lengths and perimeters linearly.
    Runs under the binding context so the ambient Decimal context can
    neither round the quotient nor overflow it.
    """

    with localcontext(_BINDING_DECIMAL_CONTEXT):
        return si / (divisor * divisor) if squared else si / divisor


def parse_spice_number(text: str) -> Decimal:
    """Return the SI value of one SPICE numeric literal."""

    match = SPICE_NUMBER_RE.match(text)
    if match is None:
        raise PdkBindingError(
            "pdk.parameter.unparsable",
            f"The parameter value {text!r} is not a SPICE numeric literal.",
            hint="Every geometry a PDK binding rescales must be a plain number.",
        )
    # The binding context preserves 200 significant digits exactly; a longer
    # significand would round silently — which can move a value across a bin
    # boundary the author wrote it against — so it is refused typed instead.
    # Trailing zeros round away exactly and do not count; nor do the
    # exponent characters, which the ``number`` group also carries.
    mantissa_text = re.split("[eE]", match.group("number"), maxsplit=1)[0]
    digits = mantissa_text.lstrip("+-").replace(".", "")
    if len(digits.lstrip("0").rstrip("0")) > 200:
        raise PdkBindingError(
            "pdk.parameter.unparsable",
            f"The parameter value {text!r} carries more than 200 significant "
            "digits, which the binding cannot preserve exactly.",
            hint="Author device scalars with at most 200 significant digits.",
        )
    try:
        # The whole conversion is guarded AND runs under the binding's own
        # wide context: the constructor raises InvalidOperation for
        # exponents beyond Decimal's own limits, the suffix multiplication
        # raises Overflow well before that, and the ambient context must
        # decide neither (a caller-narrowed precision would round a long
        # authored significand here, before the wide formatter ever sees
        # it, and an ambient Emax of 9 would misclassify a plain 1e60).
        # A regex-valid literal that defeats arithmetic is a typed refusal,
        # never an escaping decimal exception.
        with localcontext(_BINDING_DECIMAL_CONTEXT):
            magnitude = Decimal(match.group("number"))
            suffix = (match.group("suffix") or "").lower()
            return magnitude * _SPICE_SCALE[suffix]
    except ArithmeticError as exc:
        raise PdkBindingError(
            "pdk.parameter.unparsable",
            f"The parameter value {text!r} is outside any representable "
            "numeric range.",
            hint="Keep device scalars within 1e-60..1e60 SI.",
        ) from exc


#: IHP SG13G2's low-voltage MOS devices are subcircuits taking ``ng`` fingers,
#: and their PSP103 core is Verilog-A. Verified against
#: ``libs.tech/ngspice/models/sg13g2_moslv_mod.lib:66`` and a passing transient
#: run of a published Simra inverter testbench.
IHP_SG13G2 = PdkBinding(
    pdk_id="ihp-sg13g2",
    display_name="IHP SG13G2",
    device_prefix="x",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "ng"},
    library_entries=(
        PdkLibraryEntry("libs.tech/ngspice/models/cornerMOSlv.lib", "lib", "{corner}"),
        # The 3.3 V devices live in a separate corner library using the same
        # section names and a disjoint parameter namespace (sg13g2_hv_* vs
        # sg13g2_lv_*), so including both is safe and makes the .io roles
        # reachable. Verified: sg13g2_moshv_mod.lib:66 and :155 define
        # sg13_hv_nmos / sg13_hv_pmos as .subckt d g s b.
        PdkLibraryEntry("libs.tech/ngspice/models/cornerMOShv.lib", "lib", "{corner}"),
        # Non-MOS device families. Their corner namespaces are disjoint from
        # the MOS tokens (res_typ/cap_typ/hbt_typ vs mos_tt), so this revision
        # binds their TYPICAL sections for every corner. The junction diodes
        # (dantenna/dpantenna) have no corner wrapper at all — the PDK's own
        # testbenches do a bare include of diodes.lib, and so do we.
        # Family-tagged: loaded only when the deck uses the family. diodes.lib
        # in particular installs generic model names (darea, dperim, dcorner)
        # via a bare include, which must not shadow user models in MOS-only
        # decks.
        PdkLibraryEntry(
            "libs.tech/ngspice/models/cornerRES.lib",
            "lib",
            "res_typ",
            families=("resistor.poly",),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/models/cornerCAP.lib",
            "lib",
            "cap_typ",
            families=("cap.mim",),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/models/cornerHBT.lib",
            "lib",
            "hbt_typ",
            families=("bjt.npn", "bjt.pnp"),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/models/diodes.lib",
            "include",
            None,
            families=("diode.core",),
        ),
    ),
    corners=("mos_tt", "mos_ss", "mos_ff", "mos_sf", "mos_fs"),
    default_corner="mos_tt",
    device_models={
        "nmos.core": "sg13_lv_nmos",
        "pmos.core": "sg13_lv_pmos",
        "nmos.io": "sg13_hv_nmos",
        "pmos.io": "sg13_hv_pmos",
        "diode.core": "dantenna",
        "bjt.npn": "npn13G2",
        "bjt.pnp": "pnpMPA",
        "resistor.poly": "rppd",
        "cap.mim": "cap_cmim",
    },
    device_cards={
        # dantenna is the PDK's general-purpose junction diode subcircuit,
        # sized by w/l (SI metres; profile scale is 1; unsupplied geometry
        # defaults to 780n x 780n inside diodes.lib). Canonical M emits as
        # the hierarchical instance ``m`` — dantenna declares no ``m`` of its
        # own, so ngspice multiplies the whole subcircuit.
        "diode.core": PdkDeviceCard(
            model="dantenna",
            card_kind="subckt",
            parameter_names={"w": "w", "l": "l", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
        ),
        # npn13G2 is c b e bn — the canonical SUB terminal lands on bn, which
        # the author states like MOS bulk (never invented by the binder).
        # Canonical M is functional multiplicity, so it emits as the
        # hierarchical instance ``m``; the HBT's own Nx is finger GEOMETRY,
        # not parallel multiplicity (sg13g2_hbt_mod.lib:56-66 scales cbeo/cje
        # by Nx**0.975 and cjcp by Nx**0.8 while currents scale linearly), so
        # M must never be spelled Nx.
        "bjt.npn": PdkDeviceCard(
            model="npn13G2",
            card_kind="subckt",
            parameter_names={"m": "m"},
        ),
        # pnpMPA models no substrate terminal: canonical SUB is dropped and
        # reported, never rewired. Hierarchical ``m`` carries multiplicity.
        "bjt.pnp": PdkDeviceCard(
            model="pnpMPA",
            card_kind="subckt",
            emit_nodes=3,
            parameter_names={"m": "m"},
        ),
        # rppd is 1 2 bn: the canonical BODY terminal lands on bn. rppd
        # forwards its own ``m`` parameter onto the r3_cmc core's device
        # multiplier (resistors_mod.lib:189), so m=M is exact multiplicity.
        "resistor.poly": PdkDeviceCard(
            model="rppd",
            card_kind="subckt",
            parameter_names={"w": "w", "l": "l", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
        ),
        # cap_cmim declares no ``m``: the hierarchical instance ``m``
        # multiplies the whole subcircuit.
        "cap.mim": PdkDeviceCard(
            model="cap_cmim",
            card_kind="subckt",
            parameter_names={"w": "w", "l": "l", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
        ),
    },
    geometry_scale="1",
    nominal_supply_v={
        "nmos.core": "1.2",
        "pmos.core": "1.2",
        "nmos.io": "3.3",
        "pmos.io": "3.3",
    },
    model_tnom_c="27",
    # PSP103 computes ad/as/pd/ps from w and ng when they are omitted
    # (sg13g2_moslv_mod.lib:67-88), so junction capacitance is present without
    # the author supplying layout geometry.
    junction_geometry="auto",
    # The cornerMOS libraries follow the requested corner; the non-MOS
    # families are pinned to their typical sections (res_typ/cap_typ/hbt_typ,
    # bare diodes.lib), so a corner skews the MOS devices only.
    corner_skewed_roles=("nmos.core", "nmos.io", "pmos.core", "pmos.io"),
    # The PDK's own .spiceinit loads four modules. Loading only the two PSP
    # ones works for a MOS-only deck and fails the moment the deck instantiates
    # a PDK resistor (r3_cmc) or a varicap (mosvar).
    osdi_relative_paths=(
        "libs.tech/ngspice/osdi/psp103.osdi",
        "libs.tech/ngspice/osdi/psp103_nqs.osdi",
        "libs.tech/ngspice/osdi/r3_cmc.osdi",
        "libs.tech/ngspice/osdi/mosvar.osdi",
    ),
    identity_relative_path="COMMIT",
    notes=(
        "LV and HV MOS plus junction diodes (dantenna), HBTs (npn13G2, "
        "pnpMPA), poly resistors (rppd) and MIM caps (cap_cmim). MOS follows "
        "the requested corner; the other families are bound typical-only. "
        "sg13_lv_nmos/sg13_lv_pmos are subcircuits whose finger count is ng; "
        "PSP103 requires an OSDI preload and is a continuous compact model, "
        "so no binning envelope is enforced."
    ),
)

#: SkyWater sky130A ships its FETs as subcircuits over binned BSIM4 cards, and
#: its own corner collateral installs ``.option scale=1.0u``
#: (``libs.tech/ngspice/all.spice:2``, included from ``corners/<corner>.spice``).
#: Its documented xschem flow also installs ``.option wnflag=1`` so bin
#: selection uses W/NF for multi-finger devices. Geometry must therefore be
#: written in microns. Binning extents read from
#: ``libs.ref/sky130_fd_pr/spice/sky130_fd_pr__{n,p}fet_01v8__tt.pm3.spice``.
SKY130A = PdkBinding(
    pdk_id="sky130A",
    display_name="SkyWater sky130A",
    device_prefix="x",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "nf"},
    library_entries=(
        PdkLibraryEntry("libs.tech/ngspice/sky130.lib.spice", "lib", "{corner}"),
    ),
    corners=("tt", "ss", "ff", "sf", "fs"),
    default_corner="tt",
    device_models={
        "nmos.core": "sky130_fd_pr__nfet_01v8",
        "pmos.core": "sky130_fd_pr__pfet_01v8",
        "nmos.lvt": "sky130_fd_pr__nfet_01v8_lvt",
        # Verified present at libs.ref/sky130_fd_pr/spice/
        # sky130_fd_pr__pfet_01v8_lvt__tt.pm3.spice:30 and already included by
        # corners/tt.spice. Its absence made a role-based deck using
        # nmos.lvt+pmos.lvt bind on FreePDK45 and fail on sky130.
        "pmos.lvt": "sky130_fd_pr__pfet_01v8_lvt",
        "pmos.hvt": "sky130_fd_pr__pfet_01v8_hvt",
        "nmos.io": "sky130_fd_pr__nfet_g5v0d10v5",
        "pmos.io": "sky130_fd_pr__pfet_g5v0d10v5",
        # The tt section reaches all four non-MOS families through all.spice,
        # so no extra library entries are needed for these.
        "diode.core": "sky130_fd_pr__diode_pw2nd_05v5",
        "bjt.npn": "sky130_fd_pr__npn_05v5_W1p00L1p00",
        "bjt.pnp": "sky130_fd_pr__pnp_05v5_W0p68L0p68",
        "resistor.poly": "sky130_fd_pr__res_high_po",
        "cap.mim": "sky130_fd_pr__cap_mim_m3_1",
    },
    device_cards={
        # A plain `.model d` card: the D instance letter survives. Like the
        # MOS geometry, area/pj are written in the PDK's micron convention
        # (verified: at 100 uA, area=100 pj=40 gives a 0.67 V forward drop
        # while an SI area=1p makes rs explode to 9.8e10 ohm), so area
        # rescales by the divisor squared and pj linearly.
        "diode.core": PdkDeviceCard(
            model="sky130_fd_pr__diode_pw2nd_05v5",
            card_kind="model",
            parameter_names={"area": "area", "pj": "pj", "m": "m"},
            geometry_parameters=frozenset(("pj",)),
            area_parameters=frozenset(("area",)),
        ),
        # Fixed-geometry subcircuits (W1p00L1p00 etc.), c b e s: the
        # canonical SUB terminal lands on s. Canonical M emits as the plain
        # instance `m` — ngspice's hierarchical multiplier, which the subckt
        # does not declare as a formal and therefore inherits — AND as the
        # subckt-local `mult`, which the body reads for Monte-Carlo mismatch
        # scaling (model.spice:41,44 divide the is/bf mismatch terms by
        # sqrt(mult); ngspice lets an instance assignment override the inner
        # .param). One value, both meanings preserved.
        "bjt.npn": PdkDeviceCard(
            model="sky130_fd_pr__npn_05v5_W1p00L1p00",
            card_kind="subckt",
            parameter_names={"m": "m"},
            companion_parameters={"m": ("mult",)},
        ),
        # Collector Base Emitter only: canonical SUB dropped and reported.
        # Its body also reads `mult` for mismatch (model.spice:28,30).
        "bjt.pnp": PdkDeviceCard(
            model="sky130_fd_pr__pnp_05v5_W0p68L0p68",
            card_kind="subckt",
            emit_nodes=3,
            parameter_names={"m": "m"},
            companion_parameters={"m": ("mult",)},
        ),
        # res_high_po is r0 r1 b: the canonical BODY terminal lands on b.
        # Its w/l are in the PDK's micron convention, so the geometry
        # divisor applies. Nominal multiplicity is the hierarchical
        # instance `m` (verified live, the M=2 divider probe reads exactly
        # 0.400 V); the body separately reads `mult` for mismatch
        # (model.spice:35,37,41 divide the Pelgrom terms by sqrt(...*mult)),
        # so M is stated there too.
        "resistor.poly": PdkDeviceCard(
            model="sky130_fd_pr__res_high_po",
            card_kind="subckt",
            parameter_names={"w": "w", "l": "l", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
            companion_parameters={"m": ("mult",)},
        ),
        # cap_mim declares mf=1 as a formal and reads it only in the czero
        # mismatch term (model.spice:25); hierarchical `m` is the nominal
        # multiplier and mf mirrors M for mismatch.
        "cap.mim": PdkDeviceCard(
            model="sky130_fd_pr__cap_mim_m3_1",
            card_kind="subckt",
            parameter_names={"w": "w", "l": "l", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
            companion_parameters={"m": ("mf",)},
        ),
    },
    geometry_scale="1e-6",
    ngspice_options=("wnflag=1",),
    device_geometry={
        "nmos.core": DeviceGeometry(
            l_min="1.5e-7", l_max="1e-4", w_min="3.6e-7", w_max="1e-4"
        ),
        "pmos.core": DeviceGeometry(
            l_min="1.5e-7", l_max="1e-4", w_min="4.2e-7", w_max="1e-4"
        ),
    },
    # Read from the device names, which is the only place sky130A states them:
    # nothing in the installed tree carries a numeric supply.
    nominal_supply_v={
        "nmos.core": "1.8",
        "pmos.core": "1.8",
        "nmos.lvt": "1.8",
        "pmos.lvt": "1.8",
        "pmos.hvt": "1.8",
        "nmos.io": "5.0",
        "pmos.io": "5.0",
    },
    model_tnom_c="30",
    junction_geometry="zero",
    # Verified in the corner files: every FET flavour switches per corner,
    # while the PNP and the diodes come from the corner-independent all.spice
    # and every named section includes r+c/res_typical__cap_typical.spice.
    # The NPN follows only the uniform corners: ff.spice:29/ss.spice:29
    # select npn_05v5__f/__s, but sf.spice:29 and fs.spice:29 select the
    # TYPICAL npn_05v5__t — so bjt.npn is skewed at ff/ss and not at sf/fs.
    corner_skewed_roles=(
        "bjt.npn",
        "nmos.core",
        "nmos.io",
        "nmos.lvt",
        "pmos.core",
        "pmos.hvt",
        "pmos.io",
        "pmos.lvt",
    ),
    corner_skewed_role_overrides={
        corner: (
            "nmos.core",
            "nmos.io",
            "nmos.lvt",
            "pmos.core",
            "pmos.hvt",
            "pmos.io",
            "pmos.lvt",
        )
        for corner in ("sf", "fs")
    },
    osdi_relative_paths=(),
    identity_relative_path=None,
    notes=(
        "Devices are subcircuits over binned BSIM4 models. The PDK sets "
        "scale=1u itself, so instance geometry is expressed in microns; its "
        "documented ngspice flow sets wnflag=1 so multi-finger bin selection "
        "uses W/NF. An SI-valued card lands outside every bin. Parsing the full "
        "tt library takes ~95 s on a 2025 host."
    ),
)

#: GlobalFoundries gf180mcuD ships subcircuits with SI geometry, but its corner
#: library evaluates global switches (``fnoicor``, ``sw_stat_global``,
#: ``sw_stat_mismatch``) defined only in ``design.ngspice``. Including the
#: corner library alone yields "Undefined parameter [fnoicor]" on every model.
GF180MCUD = PdkBinding(
    pdk_id="gf180mcuD",
    display_name="GlobalFoundries GF180MCU (D)",
    device_prefix="x",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "nf"},
    library_entries=(
        PdkLibraryEntry("libs.tech/ngspice/design.ngspice", "include", None),
        PdkLibraryEntry("libs.tech/ngspice/sm141064.ngspice", "lib", "{corner}"),
        # Non-MOS families live in per-class sections whose names do not
        # follow the MOS corner tokens; this revision binds their TYPICAL
        # sections for every corner. mimcap_typical re-dispatches into
        # sm141064_mim.ngspice internally.
        # Family-tagged: loaded only when the deck uses the family. Each
        # section installs hundreds of global .param names, which a MOS-only
        # deck must not pay for or collide with.
        PdkLibraryEntry(
            "libs.tech/ngspice/sm141064.ngspice",
            "lib",
            "diode_typical",
            families=("diode.core",),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/sm141064.ngspice",
            "lib",
            "bjt_typical",
            families=("bjt.npn", "bjt.pnp"),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/sm141064.ngspice",
            "lib",
            "res_typical",
            families=("resistor.poly",),
        ),
        PdkLibraryEntry(
            "libs.tech/ngspice/sm141064.ngspice",
            "lib",
            "mimcap_typical",
            families=("cap.mim",),
        ),
    ),
    corners=("typical", "ff", "ss", "fs", "sf"),
    default_corner="typical",
    device_models={
        "nmos.core": "nfet_03v3",
        "pmos.core": "pfet_03v3",
        "nmos.io": "nfet_06v0",
        "pmos.io": "pfet_06v0",
        "diode.core": "diode_nd2ps_03v3",
        "bjt.npn": "npn_10p00x10p00",
        "bjt.pnp": "pnp_10p00x10p00",
        "resistor.poly": "ppolyf_u",
        "cap.mim": "cap_mim_2f0_m2m3_noshield",
    },
    device_cards={
        "diode.core": PdkDeviceCard(
            model="diode_nd2ps_03v3",
            card_kind="model",
            parameter_names={"area": "area", "pj": "pj", "m": "m"},
        ),
        # Every one of these subcircuits declares ``par=1``. Nominal
        # multiplicity is the plain instance ``m`` — ngspice's hierarchical
        # multiplier, inherited because ``m`` is not a formal — verified
        # live: the M=2 divider probe reads exactly 0.400 V. Whether the
        # body ALSO reads par differs per device (verified in
        # sm141064.ngspice): the pnp model divides its is/bf mismatch terms
        # by sqrt(par) (lines 47324, 47336), so par mirrors M there; the
        # npn body (47432-47481), ppolyf_u (38723, mis_r=0) and
        # cap_mim_2f0_m2m3_noshield (sm141064_mim.ngspice:72) never read
        # par, so no companion is emitted for them.
        "bjt.npn": PdkDeviceCard(
            model="npn_10p00x10p00",
            card_kind="subckt",
            parameter_names={"m": "m"},
        ),
        "bjt.pnp": PdkDeviceCard(
            model="pnp_10p00x10p00",
            card_kind="subckt",
            emit_nodes=3,
            parameter_names={"m": "m"},
            companion_parameters={"m": ("par",)},
        ),
        # ppolyf_u is (r0 r1 bulk) with SI r_width/r_length; the canonical
        # BODY terminal lands on bulk.
        "resistor.poly": PdkDeviceCard(
            model="ppolyf_u",
            card_kind="subckt",
            parameter_names={"w": "r_width", "l": "r_length", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
        ),
        "cap.mim": PdkDeviceCard(
            model="cap_mim_2f0_m2m3_noshield",
            card_kind="subckt",
            parameter_names={"w": "c_width", "l": "c_length", "m": "m"},
            geometry_parameters=frozenset(("w", "l")),
        ),
    },
    geometry_scale="1",
    # The full bin grid of section nfet_03v3_t / pfet_03v3_t in
    # libs.tech/ngspice/sm141064.ngspice: lmin {2.8e-7, 5e-7, 1.2e-6, 1e-5},
    # lmax {5e-7, 1.2e-6, 1e-5, 5.0001e-5}, wmin {2.2e-7, 5e-7, 1.2e-6, 1e-5},
    # wmax {5e-7, 1.2e-6, 1e-5, 1.00001e-4}. An earlier l_max of 1e-5 took the
    # *third* bin's top for the grid's top and refused every legal device wider
    # than 10 um in length - a false refusal in the very check that exists to
    # prevent false refusals.
    device_geometry={
        "nmos.core": DeviceGeometry(
            l_min="2.8e-7", l_max="5.0001e-5", w_min="2.2e-7", w_max="1.00001e-4"
        ),
        "pmos.core": DeviceGeometry(
            l_min="2.8e-7", l_max="5.0001e-5", w_min="2.2e-7", w_max="1.00001e-4"
        ),
    },
    nominal_supply_v={
        "nmos.core": "3.3",
        "pmos.core": "3.3",
        "nmos.io": "6.0",
        "pmos.io": "6.0",
    },
    model_tnom_c="25",
    junction_geometry="zero",
    # sm141064.ngspice .lib ff selects nfet_03v3_f/pfet_03v3_f but then selects
    # nfet_06v0_t/pfet_06v0_t - typical. Identical in ss, fs and sf. A corner
    # therefore skews the core devices only.
    corner_skewed_roles=("nmos.core", "pmos.core"),
    osdi_relative_paths=(),
    identity_relative_path=None,
    notes=(
        "design.ngspice must precede the corner library; it defines the global "
        "statistical and noise switches every model card references."
    ),
)

#: FreePDK45 is a transistor-model kit, not a PDK: flat BSIM4 ``.model`` cards
#: in HSPICE ``.inc`` files, one per flavour, with the corner selected by
#: directory. Simra's emitted ``M`` prefix is already correct and the models are
#: unbinned, so no geometry envelope is enforced. Non-manufacturable.
FREEPDK45 = PdkBinding(
    pdk_id="freepdk45",
    display_name="FreePDK45 predictive transistor models",
    device_prefix="m",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "nf"},
    library_entries=tuple(
        PdkLibraryEntry(
            f"ncsu_basekit/models/hspice/tran_models/models_{{corner}}/{name}.inc",
            "include",
            None,
        )
        for name in (
            "NMOS_VTG",
            "PMOS_VTG",
            "NMOS_VTL",
            "PMOS_VTL",
            "NMOS_VTH",
            "PMOS_VTH",
            "NMOS_THKOX",
            "PMOS_THKOX",
        )
    ),
    corners=("nom", "ff", "ss"),
    default_corner="nom",
    device_models={
        "nmos.core": "NMOS_VTG",
        "pmos.core": "PMOS_VTG",
        "nmos.lvt": "NMOS_VTL",
        "pmos.lvt": "PMOS_VTL",
        "nmos.hvt": "NMOS_VTH",
        "pmos.hvt": "PMOS_VTH",
        "nmos.io": "NMOS_THKOX",
        "pmos.io": "PMOS_THKOX",
    },
    geometry_scale="1",
    # ncsu_basekit/doc/FreePDK45_Manual.txt:159 characterises these models at a
    # 1.0 V supply. The thick-oxide flavour has vth0 = 1.507 and no stated
    # supply, so none is declared for the .io roles rather than one invented.
    nominal_supply_v={
        "nmos.core": "1.0",
        "pmos.core": "1.0",
        "nmos.lvt": "1.0",
        "pmos.lvt": "1.0",
        "nmos.hvt": "1.0",
        "pmos.hvt": "1.0",
    },
    model_tnom_c="27",
    junction_geometry="zero",
    osdi_relative_paths=(),
    identity_relative_path="README.txt",
    notes=(
        "Predictive, non-manufacturable model kit for circuit simulation only. "
        "No layout, DRC, LVS, extraction, silicon or signoff claim follows from "
        "a FreePDK45 run."
    ),
)


def _digital_only(pdk_id: str, display_name: str, reason: str) -> PdkBinding:
    """Register a platform that ships no transistor models at all.

    Asking for such a platform must fail with the reason, not with "unknown
    PDK": the platform *is* installed, it simply cannot answer an analog
    question, and an agent that hears "unknown" will try to spell it differently
    instead of choosing a different technology.
    """

    return PdkBinding(
        pdk_id=pdk_id,
        display_name=display_name,
        device_prefix="m",
        parameter_names={},
        library_entries=(),
        corners=(),
        default_corner="",
        device_models={},
        analog=False,
        unsupported_reason=reason,
    )


_ORFS_DIGITAL_REASON = (
    "this is an OpenROAD-flow-scripts digital platform. It ships LEF, Liberty "
    "and GDS for place-and-route and no transistor models for any simulator, so "
    "transistor-level simulation is not possible against it at any corner."
)

ASAP7 = _digital_only("asap7", "ASAP7 predictive FinFET", _ORFS_DIGITAL_REASON)
NANGATE45 = _digital_only(
    "nangate45", "Nangate45 digital platform", _ORFS_DIGITAL_REASON
)
GT2N = _digital_only("gt2n", "GT2N predictive 2 nm GAAFET", _ORFS_DIGITAL_REASON)


REGISTRY: dict[str, PdkBinding] = {
    binding.pdk_id: binding
    for binding in (
        IHP_SG13G2,
        SKY130A,
        GF180MCUD,
        FREEPDK45,
        ASAP7,
        NANGATE45,
        GT2N,
    )
}


def available_pdk_ids() -> tuple[str, ...]:
    return tuple(sorted(REGISTRY))


def simulatable_pdk_ids() -> tuple[str, ...]:
    """Return the PDKs that can answer a transistor-level question."""

    return tuple(
        sorted(pdk_id for pdk_id, binding in REGISTRY.items() if binding.analog)
    )


def device_role_index() -> dict[str, str]:
    """Return every registered PDK's model name mapped to its canonical role.

    This is what lets a deck authored against one PDK bind to another without
    the author knowing either vocabulary. It is derived from the profiles
    rather than maintained separately, so it cannot drift from them.

    Uniqueness is validated rather than resolved by registry order: one model
    name mapping to two different roles would make alias translation depend
    on iteration order, which is exactly the kind of silent first-wins this
    table exists to prevent.
    """

    index: dict[str, str] = {}
    for binding in REGISTRY.values():
        for role, model in binding.device_models.items():
            existing = index.get(model.lower())
            if existing is not None and existing != role:
                raise ValueError(
                    f"The registered PDK profiles map the model name "
                    f"{model!r} to two different roles ({existing!r} and "
                    f"{role!r}); alias translation would be ambiguous."
                )
            index[model.lower()] = role
    return index


def _cleanup_temp_snapshot_parent(parent: Path) -> None:
    """Reclaim a system-temp snapshot tree at process exit.

    Published snapshots are 0o500 directories of 0o400 files, so the write
    bits must come back before rmtree can unlink anything. Best-effort:
    a snapshot another actor already removed, or one on a read-only mount,
    must never turn process exit into a crash.
    """

    for directory, _children, _files in os.walk(
        parent, topdown=False, followlinks=False
    ):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    shutil.rmtree(parent, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class ResolvedPdkBinding:
    """One binding backed only by an immutable, content-addressed PDK snapshot.

    ``source_root`` is provenance.  Every executable locator lives below
    ``root`` (the PDK directory inside ``snapshot_root``), and no later
    consumer needs or is permitted to reopen the installed source tree.
    """

    binding: PdkBinding
    source_root: Path
    snapshot_root: Path
    root: Path
    corner: str
    library_paths: tuple[Path, ...]
    library_cards: tuple[str, ...]
    osdi_paths: tuple[Path, ...]
    identity_path: Path | None
    input_records: tuple[dict[str, Any], ...]
    configuration_records: tuple[dict[str, Any], ...]
    closure_records: tuple[dict[str, Any], ...]
    closure_root_sha256: str
    snapshot_root_sha256: str
    namespace_model_names: tuple[str, ...]
    namespace_global_nodes: tuple[str, ...]
    #: The extended-role families this snapshot's captured libraries can
    #: serve. ``None`` means the full conservative closure (no deck text was
    #: supplied, nothing was gated). A gated snapshot records what it kept so
    #: a later ``bind_deck`` against a *different* deck can refuse instead of
    #: rewriting a device whose library was never captured.
    captured_families: frozenset[str] | None = None

    @property
    def pdk_id(self) -> str:
        return self.binding.pdk_id

    @property
    def library_path(self) -> Path:
        """The primary corner library: the first sectioned entry, else the first."""

        for entry, path in zip(self.binding.library_entries, self.library_paths):
            if entry.form == "lib":
                return path
        return self.library_paths[0]

    def facts(self) -> dict[str, Any]:
        """Return the bounded, serializable description of this binding."""

        return {
            "pdk_id": self.binding.pdk_id,
            "display_name": self.binding.display_name,
            "source_root": str(self.source_root),
            "snapshot_root": str(self.snapshot_root),
            "root": str(self.root),
            "corner": self.corner,
            "library": str(self.library_path),
            "library_cards": list(self.library_cards),
            "closure_records": [dict(record) for record in self.closure_records],
            "closure_root_sha256": self.closure_root_sha256,
            "snapshot_root_sha256": self.snapshot_root_sha256,
            "device_prefix": self.binding.device_prefix,
            "parameter_names": dict(self.binding.parameter_names),
            "geometry_scale": self.binding.geometry_scale,
            "device_models": dict(self.binding.device_models),
            "osdi_modules": [str(path) for path in self.osdi_paths],
            "identity": str(self.identity_path) if self.identity_path else None,
            "captured_families": (
                None
                if self.captured_families is None
                else sorted(self.captured_families)
            ),
        }

    def verify_snapshot(self) -> None:
        """Refuse if any published snapshot byte or locator is no longer exact.

        Snapshot files are non-writable and their directories are non-writable,
        but a same-UID process can still rename an ancestor it owns.  Native
        launch therefore verifies every retained file immediately before and
        after execution rather than treating chmod as an integrity boundary.
        """

        try:
            snapshot_info = self.snapshot_root.lstat()
            root_info = self.root.lstat()
        except OSError as exc:
            raise PdkBindingError(
                "pdk.snapshot.unstable",
                f"The captured PDK snapshot is unavailable: {exc}",
            ) from exc
        if (
            stat.S_ISLNK(snapshot_info.st_mode)
            or not stat.S_ISDIR(snapshot_info.st_mode)
            or snapshot_info.st_mode
            & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or self.snapshot_root.name != self.snapshot_root_sha256
            or self.root != self.snapshot_root / self.pdk_id
        ):
            raise PdkBindingError(
                "pdk.snapshot.unstable",
                "The captured PDK snapshot root is not the immutable "
                "content-addressed directory that was resolved.",
            )

        expected_by_path: dict[str, Mapping[str, Any]] = {}
        for expected in self.input_records:
            path = Path(str(expected.get("path", "")))
            if not path.is_absolute():
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    "The captured PDK snapshot contains a non-absolute input "
                    f"locator: {path}.",
                )
            try:
                path.relative_to(self.snapshot_root)
            except ValueError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured input {path} is outside its snapshot root.",
                ) from exc
            if str(path) in expected_by_path:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured PDK snapshot repeats input path {path}.",
                )
            expected_by_path[str(path)] = expected
            try:
                info = path.lstat()
            except OSError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured PDK snapshot file {path} is unavailable: {exc}",
                ) from exc
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured PDK snapshot file {path} is no longer one "
                    "non-writable, non-linked regular file.",
                )
            try:
                observed = file_record(
                    path,
                    kind=str(expected.get("kind")),
                    role=str(expected.get("role")),
                    maximum_bytes=MAX_PDK_FILE_BYTES,
                )
            except FileRecordError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured PDK snapshot file {path} could not be "
                    f"verified: {exc}",
                ) from exc
            if (
                observed.get("exists") is not True
                or observed.get("bytes") != expected.get("bytes")
                or observed.get("sha256") != expected.get("sha256")
                or observed.get("path") != expected.get("path")
            ):
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"The captured PDK snapshot file {path} changed after "
                    "publication.",
                )

        expected_files = {Path(path) for path in expected_by_path}
        expected_directories = {self.snapshot_root}
        for path in expected_files:
            parent = path.parent
            while True:
                expected_directories.add(parent)
                if parent == self.snapshot_root:
                    break
                parent = parent.parent

        observed_files: set[Path] = set()
        observed_directories: set[Path] = set()
        pending = [self.snapshot_root]
        while pending:
            directory = pending.pop()
            try:
                info = directory.lstat()
            except OSError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Snapshot directory {directory} is unavailable: {exc}",
                ) from exc
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Snapshot directory {directory} is not one real "
                    "non-writable directory.",
                )
            observed_directories.add(directory)
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Snapshot directory {directory} cannot be enumerated: {exc}",
                ) from exc
            for entry in entries:
                path = directory / entry.name
                try:
                    entry_info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PdkBindingError(
                        "pdk.snapshot.unstable",
                        f"Snapshot entry {path} is unavailable: {exc}",
                    ) from exc
                if stat.S_ISLNK(entry_info.st_mode):
                    raise PdkBindingError(
                        "pdk.snapshot.unstable",
                        f"Snapshot entry {path} is a symbolic link.",
                    )
                if stat.S_ISDIR(entry_info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(entry_info.st_mode):
                    observed_files.add(path)
                else:
                    raise PdkBindingError(
                        "pdk.snapshot.unstable",
                        f"Snapshot entry {path} is not a regular file or directory.",
                    )

        if (
            observed_files != expected_files
            or observed_directories != expected_directories
        ):
            unexpected = sorted(
                str(path)
                for path in (
                    (observed_files - expected_files)
                    | (observed_directories - expected_directories)
                )
            )
            missing = sorted(
                str(path)
                for path in (
                    (expected_files - observed_files)
                    | (expected_directories - observed_directories)
                )
            )
            detail = (
                f"unexpected={unexpected[:3]}, missing={missing[:3]}"
            )
            raise PdkBindingError(
                "pdk.snapshot.unstable",
                "The captured PDK snapshot tree contains undeclared or "
                f"missing entries ({detail}).",
            )

        runtime_paths = (
            *self.library_paths,
            *self.osdi_paths,
            *((self.identity_path,) if self.identity_path is not None else ()),
        )
        for path in runtime_paths:
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Runtime PDK locator {path} is outside the captured tree.",
                ) from exc
            if str(path) not in expected_by_path:
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Runtime PDK locator {path} has no captured input record.",
                )

        digest_records: list[dict[str, Any]] = []
        for index, record in enumerate(self.closure_records):
            relative = Path(str(record.get("relative_path", "")))
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or record.get("index") != index
            ):
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    "The ordered PDK closure contains an invalid relative "
                    f"locator or index at entry {index}.",
                )
            path = self.root / relative
            expected = expected_by_path.get(str(path))
            if (
                record.get("path") != str(path)
                or expected is None
                or record.get("bytes") != expected.get("bytes")
                or record.get("sha256") != expected.get("sha256")
            ):
                raise PdkBindingError(
                    "pdk.snapshot.unstable",
                    f"Ordered PDK closure entry {index} no longer matches its "
                    "captured file.",
                )
            digest_records.append(
                {
                    key: record.get(key)
                    for key in (
                        "index",
                        "form",
                        "relative_path",
                        "section",
                        "bytes",
                        "sha256",
                    )
                }
            )
        if (
            _canonical_digest(_CLOSURE_DOMAIN, digest_records)
            != self.closure_root_sha256
        ):
            raise PdkBindingError(
                "pdk.snapshot.unstable",
                "The ordered PDK closure facts no longer match their digest root.",
            )


@dataclass(frozen=True, slots=True)
class _CapturedPdkFile:
    relative_path: Path
    body: bytes
    sha256: str

    @property
    def bytes(self) -> int:
        return len(self.body)


def _canonical_digest(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _without_inline_comment(line: str) -> str:
    """Remove a SPICE ``$`` end-of-line comment without touching quoted paths."""

    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            continue
        if (
            character == "$"
            and quote is None
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index]
    return line


def _directive_tokens(line: str) -> tuple[str, ...]:
    stripped = _without_inline_comment(line).strip()
    if not stripped.startswith(".") or stripped.startswith(".*"):
        return ()
    try:
        lexer = shlex.shlex(stripped, posix=True)
        lexer.commenters = ""
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError as exc:
        raise PdkBindingError(
            "pdk.closure.directive_invalid",
            f"The PDK collateral contains an unparseable directive: "
            f"{stripped[:200]!r}.",
        ) from exc


def _normalized_source_path(
    root: Path,
    base: Path,
    locator: str,
    *,
    label: str,
) -> tuple[Path, Path]:
    """Resolve one closed relative collateral locator inside ``root``."""

    if (
        not locator
        or "\x00" in locator
        or "\r" in locator
        or "\n" in locator
        or "$" in locator
        or Path(locator).is_absolute()
    ):
        raise PdkBindingError(
            "pdk.closure.locator_invalid",
            f"{label} uses a dynamic, absolute, or malformed collateral "
            f"locator {locator!r}; a closed snapshot requires one relative path.",
        )
    lexical = Path(os.path.abspath(base / locator))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise PdkBindingError(
            "pdk.closure.path_escape",
            f"{label} resolves outside the selected PDK root: {locator!r}.",
        ) from exc
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise PdkBindingError(
            "pdk.closure.missing",
            f"{label} references missing or unreadable collateral {lexical}.",
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PdkBindingError(
            "pdk.closure.path_escape",
            f"{label} resolves through the filesystem outside the selected PDK "
            f"root: {locator!r}.",
        ) from exc
    # Mirroring a symlink into a regular snapshot file changes relative path
    # semantics for any descendants. Refuse aliases instead of guessing.
    if resolved != lexical:
        raise PdkBindingError(
            "pdk.closure.symlink",
            f"{label} resolves through a symbolic-link alias {lexical}; the "
            "active PDK closure must have one canonical in-root path per file.",
        )
    return lexical, relative


class _PdkSnapshotBuilder:
    """Read the selected PDK closure once and publish one immutable tree."""

    def __init__(
        self,
        binding: PdkBinding,
        source_root: Path,
        corner: str,
    ) -> None:
        self.binding = binding
        self.source_root = source_root
        self.corner = corner
        self.captured: dict[Path, _CapturedPdkFile] = {}
        self.roles: dict[Path, tuple[str, str]] = {}
        self.closure_records: list[dict[str, Any]] = []
        self.namespace_models: set[str] = {
            name.casefold() for name in binding.device_models.values()
        }
        self.namespace_globals: set[str] = set()
        self._walked_views: set[tuple[Path, str | None]] = set()
        self._active_views: list[tuple[Path, str | None]] = []
        self._transport_walked: set[Path] = set()
        self._transport_active: list[Path] = []
        self._total_bytes = 0

    def _capture(self, relative: Path) -> _CapturedPdkFile:
        existing = self.captured.get(relative)
        if existing is not None:
            return existing
        if len(self.captured) >= MAX_BOUND_FILES - 1:
            raise PdkBindingError(
                "pdk.files.over_limit",
                f"The active PDK snapshot exceeds the {MAX_BOUND_FILES}-file "
                "ceiling.",
            )
        source = self.source_root / relative
        try:
            with stable_regular_file(source) as (handle, opened):
                if opened.st_size > MAX_PDK_FILE_BYTES:
                    raise PdkBindingError(
                        "pdk.file.over_limit",
                        f"The PDK file {source} is {opened.st_size} bytes; the "
                        f"per-file ceiling is {MAX_PDK_FILE_BYTES}.",
                    )
                body = handle.read(MAX_PDK_FILE_BYTES + 1)
                if len(body) != opened.st_size:
                    raise PdkBindingError(
                        "pdk.file.unstable",
                        f"The PDK file {source} changed during its one capture.",
                    )
        except PdkBindingError:
            raise
        except FileRecordError as exc:
            raise PdkBindingError(
                "pdk.file.unreadable",
                f"The PDK file {source} could not be captured safely: {exc}",
            ) from exc
        self._total_bytes += len(body)
        if self._total_bytes > MAX_PDK_SNAPSHOT_BYTES:
            raise PdkBindingError(
                "pdk.files.over_limit",
                f"The active PDK snapshot exceeds the "
                f"{MAX_PDK_SNAPSHOT_BYTES}-byte ceiling.",
            )
        captured = _CapturedPdkFile(
            relative_path=relative,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
        )
        self.captured[relative] = captured
        return captured

    def capture_material(
        self,
        relative: Path,
        *,
        kind: str,
        role: str,
    ) -> _CapturedPdkFile:
        captured = self._capture(relative)
        priority = {
            "pdk.parser-library": 0,
            "pdk.transitive-library": 1,
            "pdk.corner-library": 2,
            "pdk.osdi-module": 3,
            "pdk.identity": 3,
        }
        existing = self.roles.get(relative)
        if existing is None or priority.get(role, 0) > priority.get(existing[1], 0):
            self.roles[relative] = (kind, role)
        return captured

    def capture_transport_closure(
        self,
        relative: Path,
        *,
        depth: int = 0,
    ) -> None:
        """Capture files ngspice's library preprocessor may open.

        Ngspice selects the requested ``.lib`` section semantically, but its
        source preprocessor still opens ``.include`` files appearing in other
        sections (and recursively preprocesses them).  Those bytes do not enter
        the *active semantic closure* or namespace, yet they must exist in the
        private tree or a selected-corner launch fails before section
        evaluation.  Keep this parser transport closure separate so inactive
        model names never become collision evidence.
        """

        if depth > MAX_CLOSURE_DEPTH:
            raise PdkBindingError(
                "pdk.closure.over_limit",
                "The PDK parser transport closure exceeds the bounded depth "
                f"of {MAX_CLOSURE_DEPTH}.",
            )
        if relative in self._transport_active:
            cycle = " -> ".join(
                path.as_posix() for path in (*self._transport_active, relative)
            )
            raise PdkBindingError(
                "pdk.closure.cycle",
                f"The PDK parser transport closure is cyclic: {cycle}.",
            )
        if relative in self._transport_walked:
            return

        captured = self.capture_material(
            relative,
            kind="spice-model-library",
            role="pdk.parser-library",
        )
        self._transport_active.append(relative)
        try:
            text = captured.body.decode("latin-1")
            for line_number, line in enumerate(text.splitlines(), start=1):
                tokens = _directive_tokens(line)
                if not tokens:
                    continue
                directive = tokens[0].casefold()
                child_locator: str | None = None
                if directive in _INCLUDE_DIRECTIVES:
                    if len(tokens) != 2:
                        raise PdkBindingError(
                            "pdk.closure.directive_invalid",
                            f"{relative}:{line_number} has a malformed "
                            f"{tokens[0]} directive.",
                        )
                    child_locator = tokens[1]
                elif directive == ".lib" and len(tokens) == 3:
                    child_locator = tokens[1]
                elif directive == ".lib" and len(tokens) not in {2, 3}:
                    raise PdkBindingError(
                        "pdk.closure.directive_invalid",
                        f"{relative}:{line_number} has a malformed .lib "
                        "directive.",
                    )
                if child_locator is None:
                    continue
                _source, child = _normalized_source_path(
                    self.source_root,
                    self.source_root / relative.parent,
                    child_locator,
                    label=f"{relative}:{line_number}",
                )
                if directive == ".lib" and (
                    child in self._transport_active
                    or child in self._transport_walked
                ):
                    # A library file may select another section of itself (the
                    # gf180 wrapper does this extensively). The one captured
                    # file already transports every section; recursing here
                    # would confuse legal section selection with an include
                    # cycle.
                    self.capture_material(
                        child,
                        kind="spice-model-library",
                        role="pdk.parser-library",
                    )
                    continue
                self.capture_transport_closure(child, depth=depth + 1)
            self._transport_walked.add(relative)
        finally:
            self._transport_active.pop()

    def _scan_namespace(self, line: str) -> None:
        tokens = _directive_tokens(line)
        if len(tokens) < 2:
            return
        directive = tokens[0].casefold()
        if directive not in _NAMESPACE_DIRECTIVES:
            return
        if directive in {".model", ".subckt"}:
            self.namespace_models.add(tokens[1].casefold())
            return
        self.namespace_globals.update(
            token.casefold()
            for token in tokens[1:]
            if token != "0" and not token.startswith("$")
        )

    def walk(
        self,
        relative: Path,
        *,
        section: str | None,
        form: str,
        depth: int = 0,
    ) -> None:
        if depth > MAX_CLOSURE_DEPTH:
            raise PdkBindingError(
                "pdk.closure.over_limit",
                f"The active PDK include/library closure exceeds the "
                f"{MAX_CLOSURE_DEPTH}-edge depth ceiling.",
            )
        view = (relative, section.casefold() if section is not None else None)
        if view in self._active_views:
            cycle = " -> ".join(
                f"{path}{(':' + selected) if selected else ''}"
                for path, selected in (*self._active_views, view)
            )
            raise PdkBindingError(
                "pdk.closure.cycle",
                f"The active PDK include/library closure is cyclic: {cycle}.",
            )

        captured = self.capture_material(
            relative,
            kind="spice-model-library",
            role="pdk.transitive-library",
        )
        if len(self.closure_records) >= MAX_CLOSURE_EDGES:
            raise PdkBindingError(
                "pdk.closure.over_limit",
                f"The active PDK include/library closure exceeds the "
                f"{MAX_CLOSURE_EDGES}-edge ceiling.",
            )
        self.closure_records.append(
            {
                "index": len(self.closure_records),
                "form": form,
                "relative_path": relative.as_posix(),
                "section": section,
                "bytes": captured.bytes,
                "sha256": captured.sha256,
            }
        )
        if view in self._walked_views:
            return

        self._active_views.append(view)
        try:
            text = captured.body.decode("latin-1")
            current_section: str | None = None
            requested_found = section is None
            for line_number, line in enumerate(text.splitlines(), start=1):
                tokens = _directive_tokens(line)
                directive = tokens[0].casefold() if tokens else ""

                if directive == ".lib" and len(tokens) == 2:
                    if current_section is not None:
                        raise PdkBindingError(
                            "pdk.closure.section_invalid",
                            f"{relative}:{line_number} opens nested .lib sections.",
                        )
                    current_section = tokens[1]
                    if (
                        section is not None
                        and current_section.casefold() == section.casefold()
                    ):
                        requested_found = True
                    continue
                if directive in {".endl", ".endlib"}:
                    if len(tokens) not in {1, 2} or current_section is None:
                        raise PdkBindingError(
                            "pdk.closure.section_invalid",
                            f"{relative}:{line_number} has an unmatched or "
                            "malformed library-section terminator.",
                        )
                    if (
                        len(tokens) == 2
                        and tokens[1].casefold() != current_section.casefold()
                    ):
                        raise PdkBindingError(
                            "pdk.closure.section_invalid",
                            f"{relative}:{line_number} closes {tokens[1]!r}, not "
                            f"the active section {current_section!r}.",
                        )
                    current_section = None
                    continue

                active = current_section is None or (
                    section is not None
                    and current_section.casefold() == section.casefold()
                )
                if not active:
                    continue
                self._scan_namespace(line)

                if directive in _INCLUDE_DIRECTIVES:
                    if len(tokens) != 2:
                        raise PdkBindingError(
                            "pdk.closure.directive_invalid",
                            f"{relative}:{line_number} has a malformed "
                            f"{tokens[0]} directive.",
                        )
                    _source, child = _normalized_source_path(
                        self.source_root,
                        self.source_root / relative.parent,
                        tokens[1],
                        label=f"{relative}:{line_number}",
                    )
                    self.walk(
                        child,
                        section=None,
                        form="include",
                        depth=depth + 1,
                    )
                elif directive == ".lib":
                    if len(tokens) != 3:
                        raise PdkBindingError(
                            "pdk.closure.directive_invalid",
                            f"{relative}:{line_number} has a malformed .lib "
                            "file-selection directive.",
                        )
                    _source, child = _normalized_source_path(
                        self.source_root,
                        self.source_root / relative.parent,
                        tokens[1],
                        label=f"{relative}:{line_number}",
                    )
                    self.walk(
                        child,
                        section=tokens[2],
                        form="lib",
                        depth=depth + 1,
                    )

            if current_section is not None:
                raise PdkBindingError(
                    "pdk.closure.section_invalid",
                    f"{relative} has an unterminated .lib section "
                    f"{current_section!r}.",
                )
            if not requested_found:
                raise PdkBindingError(
                    "pdk.library.section_missing",
                    f"The captured PDK library {relative} has no section "
                    f"{section!r}.",
                )
            self._walked_views.add(view)
        finally:
            self._active_views.pop()

    @staticmethod
    def _write_file(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        try:
            position = 0
            while position < len(body):
                written = os.write(descriptor, body[position:])
                if written <= 0:
                    raise OSError("short PDK snapshot write")
                position += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def publish(
        self,
        *,
        snapshot_parent: str | Path | None,
        osdi_relatives: Sequence[Path],
        identity_relative: Path | None,
    ) -> tuple[
        Path,
        str,
        str,
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
    ]:
        closure_digest_records = [dict(record) for record in self.closure_records]
        closure_root_sha256 = _canonical_digest(
            _CLOSURE_DOMAIN, closure_digest_records
        )
        material_records = [
            {
                "relative_path": relative.as_posix(),
                "kind": self.roles[relative][0],
                "role": self.roles[relative][1],
                "bytes": captured.bytes,
                "sha256": captured.sha256,
            }
            for relative, captured in self.captured.items()
        ]
        snapshot_description = {
            "schema": PDK_SNAPSHOT_SCHEMA,
            "pdk_id": self.binding.pdk_id,
            "corner": self.corner,
            "closure_root_sha256": closure_root_sha256,
            "closure": closure_digest_records,
            "files": material_records,
        }
        snapshot_root_sha256 = _canonical_digest(
            _SNAPSHOT_DOMAIN, snapshot_description
        )
        manifest = {
            **snapshot_description,
            "snapshot_root_sha256": snapshot_root_sha256,
        }
        manifest_body = (
            json.dumps(
                manifest,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        if snapshot_parent is None:
            private_parent = Path(
                tempfile.mkdtemp(prefix="openada-pdk-snapshot-")
            )
            # A system-temp snapshot is this process's private model closure:
            # nothing may reopen it after the run, so reclaim it at process
            # exit. (The 0o500/0o400 integrity chmods must be relaxed first
            # or rmtree cannot unlink the tree.) Snapshots published under an
            # explicit snapshot_parent stay: the caller owns that location.
            atexit.register(_cleanup_temp_snapshot_parent, private_parent)
        else:
            parent = Path(snapshot_parent).expanduser()
            if not parent.is_absolute():
                raise PdkBindingError(
                    "pdk.snapshot.destination_invalid",
                    f"The PDK snapshot parent must be absolute: {parent}.",
                )
            try:
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_parent = Path(
                    tempfile.mkdtemp(prefix=".openada-pdk-", dir=parent)
                )
            except OSError as exc:
                raise PdkBindingError(
                    "pdk.snapshot.destination_invalid",
                    f"The PDK snapshot parent {parent} is unusable: {exc}",
                ) from exc

        staging = private_parent / ".capture"
        final = private_parent / snapshot_root_sha256
        try:
            staging.mkdir(mode=0o700)
            pdk_directory = staging / self.binding.pdk_id
            for relative, captured in self.captured.items():
                self._write_file(pdk_directory / relative, captured.body)
            self._write_file(
                staging / "openada-pdk-snapshot.json", manifest_body
            )
            # Files were created 0400. Remove write permission from every
            # snapshot directory before atomic publication as the digest name.
            for directory, _children, _files in os.walk(
                staging, topdown=False, followlinks=False
            ):
                os.chmod(directory, 0o500)
            os.rename(staging, final)
        except OSError as exc:
            shutil.rmtree(private_parent, ignore_errors=True)
            raise PdkBindingError(
                "pdk.snapshot.publication_failed",
                f"The immutable PDK snapshot could not be published: {exc}",
            ) from exc

        snapshot_pdk_root = final / self.binding.pdk_id
        published_records: list[dict[str, Any]] = []
        by_relative: dict[Path, dict[str, Any]] = {}
        try:
            for relative, captured in self.captured.items():
                kind, role = self.roles[relative]
                record = file_record(
                    snapshot_pdk_root / relative,
                    kind=kind,
                    role=role,
                    maximum_bytes=MAX_PDK_FILE_BYTES,
                )
                if (
                    record.get("exists") is not True
                    or record.get("bytes") != captured.bytes
                    or record.get("sha256") != captured.sha256
                ):
                    raise PdkBindingError(
                        "pdk.snapshot.publication_failed",
                        f"The published PDK snapshot file {relative} differs "
                        "from its one captured preimage.",
                    )
                published_records.append(record)
                by_relative[relative] = record
            manifest_record = file_record(
                final / "openada-pdk-snapshot.json",
                kind="pdk-snapshot-manifest",
                role="pdk.snapshot",
                maximum_bytes=MAX_PDK_FILE_BYTES,
            )
            if (
                manifest_record.get("exists") is not True
                or manifest_record.get("sha256")
                != hashlib.sha256(manifest_body).hexdigest()
            ):
                raise PdkBindingError(
                    "pdk.snapshot.publication_failed",
                    "The published PDK snapshot manifest differs from its "
                    "canonical preimage.",
                )
        except (FileRecordError, PdkBindingError) as exc:
            if isinstance(exc, PdkBindingError):
                raise
            raise PdkBindingError(
                "pdk.snapshot.publication_failed",
                f"The published PDK snapshot could not be verified: {exc}",
            ) from exc

        closure_facts = tuple(
            {
                **record,
                "path": str(
                    snapshot_pdk_root / Path(str(record["relative_path"]))
                ),
            }
            for record in closure_digest_records
        )
        input_records = (manifest_record, *published_records)
        configuration_records = (
            manifest_record,
            *(
                by_relative[relative]
                for relative in osdi_relatives
            ),
            *(
                (by_relative[identity_relative],)
                if identity_relative is not None
                else ()
            ),
        )
        return (
            final,
            closure_root_sha256,
            snapshot_root_sha256,
            tuple(input_records),
            tuple(configuration_records),
            closure_facts,
        )

def _extended_family_tokens() -> dict[str, str]:
    """Every token that can bind to an extended family, mapped to its family.

    ``translate_model()`` accepts target-native and cross-PDK aliases
    (``dantenna``, ``sky130_fd_pr__diode_pw2nd_05v5``, ``npn_10p00x10p00``,
    ...) in addition to the canonical roles, so the family scanner must
    recognise exactly the same vocabulary: a scanner narrower than the binder
    would gate out a library the rewritten deck then references (review
    round-2 blocking finding 1).
    """

    tokens: dict[str, str] = {role: role for role in EXTENDED_DEVICE_ROLES}
    for name, role in device_role_index().items():
        if role in EXTENDED_DEVICE_ROLES:
            tokens[name] = role
    return tokens


def scan_deck_families(deck_text: str) -> frozenset[str]:
    """Return the extended-role families a deck's text could bind.

    The token set is the full device registry: every canonical extended role
    plus every registered PDK's model name for an extended family, folded
    case-insensitively. Deliberately over-approximate — a mention inside a
    comment still counts, because loading one unused library is recoverable
    and an undefined subcircuit at simulation time is not.
    """

    found: set[str] = set()
    lowered = deck_text.lower()
    for token, family in _extended_family_tokens().items():
        if family in found:
            continue
        pattern = rf"(?<![a-z0-9_.]){re.escape(token)}(?![a-z0-9_.])"
        if re.search(pattern, lowered):
            found.add(family)
    return frozenset(found)


def resolve_pdk_binding(
    pdk_id: str,
    pdk_root: str | Path,
    *,
    corner: str | None = None,
    snapshot_parent: str | Path | None = None,
    deck_text: str | None = None,
) -> ResolvedPdkBinding:
    """Capture one named PDK's complete active closure into one immutable tree.

    ``pdk_root`` is the directory *containing* the PDK tree, matching the
    ``PDK_ROOT``/``PDK`` split every open PDK uses. A root that already points
    at the PDK directory itself is also accepted.  The installed tree is read
    only during this call; all returned locators name the captured snapshot.

    ``deck_text`` gates family-tagged library entries: an entry tagged with
    extended-role families is captured and loaded only when the deck names one
    of them. Without ``deck_text`` every entry loads (the conservative,
    backward-compatible closure).
    """

    binding = REGISTRY.get(pdk_id)
    if binding is None:
        raise PdkBindingError(
            "pdk.unknown",
            f"No binding profile is registered for the PDK {pdk_id!r}.",
            hint=f"Registered PDKs: {', '.join(available_pdk_ids())}.",
        )
    if not binding.analog:
        raise PdkBindingError(
            "pdk.analog.unsupported",
            f"{binding.display_name} cannot be bound to a transistor-level deck: "
            f"{binding.unsupported_reason}",
            hint=(
                "PDKs that can answer a transistor-level question: "
                f"{', '.join(simulatable_pdk_ids())}."
            ),
        )

    captured_families: frozenset[str] | None = None
    if deck_text is not None:
        used_families = scan_deck_families(deck_text)
        kept_entries = tuple(
            entry
            for entry in binding.library_entries
            if not entry.families or set(entry.families) & used_families
        )
        # A family is served by this gated snapshot if some kept entry is
        # tagged with it, or if no entry anywhere is tagged with it (its
        # collateral rides an always-loaded entry, as on sky130). Recording
        # the served set — not the scanned set — keeps reuse exact: a
        # MOS-only sky130 snapshot can still bind a diode deck, an IHP one
        # cannot.
        all_tagged = {
            family
            for entry in binding.library_entries
            for family in entry.families
        }
        kept_tagged = {
            family for entry in kept_entries for family in entry.families
        }
        captured_families = frozenset(EXTENDED_DEVICE_ROLES) - (
            all_tagged - kept_tagged
        )
        binding = dataclasses.replace(binding, library_entries=kept_entries)

    supplied_root = Path(pdk_root).expanduser()
    if not supplied_root.is_absolute():
        raise PdkBindingError(
            "pdk.root.invalid",
            f"The PDK root must be an absolute path: {supplied_root}",
        )
    root = supplied_root.resolve()
    # Accept either the parent of the PDK tree or the tree itself.
    candidate = root / binding.pdk_id
    if candidate.is_dir():
        # Catalog roots commonly expose each installed immutable PDK through a
        # symlink. Resolve that catalog alias once at the trust boundary; no
        # source symlink is copied into or consulted from the snapshot.
        root = candidate.resolve()
    if not root.is_dir():
        raise PdkBindingError(
            "pdk.root.missing",
            f"The PDK root {root} is not a directory.",
        )

    selected_corner = binding.resolved_corner(corner)

    builder = _PdkSnapshotBuilder(binding, root, selected_corner)
    library_relatives: list[Path] = []
    for entry in binding.library_entries:
        relative, section = entry.resolve(selected_corner)
        try:
            _source, relative_path = _normalized_source_path(
                root,
                root,
                relative,
                label=f"{binding.pdk_id} binding profile",
            )
        except PdkBindingError as exc:
            if exc.code != "pdk.closure.missing":
                raise
            raise PdkBindingError(
                "pdk.library.missing",
                f"{binding.pdk_id} expects {root / relative}, which does not "
                "exist or cannot be captured.",
                hint="Check that --pdk-root points at the installed PDK.",
            ) from exc
        library_relatives.append(relative_path)
        builder.capture_transport_closure(relative_path)
        builder.capture_material(
            relative_path,
            kind="spice-model-library",
            role="pdk.corner-library",
        )
        # A root role takes precedence if a profile entry was already reached
        # transitively from an earlier ordered entry.
        builder.roles[relative_path] = (
            "spice-model-library",
            "pdk.corner-library",
        )
        builder.walk(
            relative_path,
            section=section if entry.form == "lib" else None,
            form=entry.form,
        )

    osdi_relatives: list[Path] = []
    for relative in binding.osdi_relative_paths:
        try:
            _source, module_relative = _normalized_source_path(
                root,
                root,
                relative,
                label=f"{binding.pdk_id} OSDI profile",
            )
        except PdkBindingError as exc:
            raise PdkBindingError(
                "pdk.osdi.missing",
                f"{binding.pdk_id} requires the Verilog-A module "
                f"{root / relative}, which could not be captured: {exc.message}",
                hint=(
                    "Without its OSDI modules this PDK's devices cannot bind and "
                    "ngspice reports an unknown model type."
                ),
            ) from exc
        builder.capture_material(
            module_relative,
            kind="verilog-a-module",
            role="pdk.osdi-module",
        )
        osdi_relatives.append(module_relative)

    identity_relative: Path | None = None
    if binding.identity_relative_path:
        candidate_identity = root / binding.identity_relative_path
        try:
            candidate_identity.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PdkBindingError(
                "pdk.file.unreadable",
                f"The PDK identity file {candidate_identity} could not be "
                f"inspected: {exc}",
            ) from exc
        else:
            _source, identity_relative = _normalized_source_path(
                root,
                root,
                binding.identity_relative_path,
                label=f"{binding.pdk_id} identity profile",
            )
            builder.capture_material(
                identity_relative,
                kind="pdk-identity",
                role="pdk.identity",
            )

    (
        snapshot_root,
        closure_root_sha256,
        snapshot_root_sha256,
        input_records,
        configuration_records,
        closure_records,
    ) = builder.publish(
        snapshot_parent=snapshot_parent,
        osdi_relatives=osdi_relatives,
        identity_relative=identity_relative,
    )
    snapshot_pdk_root = snapshot_root / binding.pdk_id
    library_paths = tuple(
        snapshot_pdk_root / relative for relative in library_relatives
    )
    library_cards = tuple(
        (
            f".lib {path} {entry.resolve(selected_corner)[1]}"
            if entry.form == "lib"
            else f".include {path}"
        )
        for entry, path in zip(binding.library_entries, library_paths)
    )
    osdi_paths = tuple(
        snapshot_pdk_root / relative for relative in osdi_relatives
    )
    identity_path = (
        snapshot_pdk_root / identity_relative
        if identity_relative is not None
        else None
    )

    resolved = ResolvedPdkBinding(
        binding=binding,
        source_root=root,
        snapshot_root=snapshot_root,
        root=snapshot_pdk_root,
        corner=selected_corner,
        library_paths=library_paths,
        library_cards=library_cards,
        osdi_paths=osdi_paths,
        identity_path=identity_path,
        input_records=input_records,
        configuration_records=configuration_records,
        closure_records=closure_records,
        closure_root_sha256=closure_root_sha256,
        snapshot_root_sha256=snapshot_root_sha256,
        namespace_model_names=tuple(sorted(builder.namespace_models)),
        namespace_global_nodes=tuple(sorted(builder.namespace_globals)),
        captured_families=captured_families,
    )
    resolved.verify_snapshot()
    return resolved


def translate_model(model: str, binding: PdkBinding) -> tuple[str, str | None, bool]:
    """Map one deck model token onto this PDK's vocabulary.

    Returns ``(pdk_model, role, translated)``. A token that is already this
    PDK's own model name passes through untouched; a canonical role name or
    another registered PDK's model name for the same role is rewritten.
    """

    lowered = model.lower()
    native = {
        name.lower(): (role, name) for role, name in binding.device_models.items()
    }

    # ``nmos.svt`` and ``nmos.core`` are the same device under two names the
    # industry uses interchangeably, and SPICE is case-insensitive, so the
    # role vocabulary is too; resolve before consulting the profile.
    resolved_role = canonical_role(lowered)
    if resolved_role in binding.device_models:  # a canonical role
        return binding.device_models[resolved_role], resolved_role, True
    if lowered in native:  # already this PDK's own model
        role, name = native[lowered]
        return name, role, name != model
    role_name = device_role_index().get(lowered)
    if role_name is None and (
        resolved_role in DEVICE_ROLES or resolved_role in EXTENDED_DEVICE_ROLES
    ):
        # A canonical role this PDK does not ship is a different failure from an
        # unrecognised token, and the caller needs to hear which.
        role_name = resolved_role
    if role_name is not None and role_name in binding.device_models:
        return binding.device_models[role_name], role_name, True
    if role_name is not None:
        raise PdkBindingError(
            "pdk.model.unavailable",
            f"{binding.display_name} ships no device for the role {role_name!r}, "
            f"which the deck names as {model!r}.",
            hint=f"Roles this PDK offers: {', '.join(sorted(binding.device_models))}.",
        )
    raise PdkBindingError(
        "pdk.model.unknown",
        f"The deck names the device model {model!r}, which is neither a "
        f"canonical device role nor a model {binding.display_name} ships.",
        hint=(
            "Author the deck against a canonical role "
            f"({', '.join(DEVICE_ROLES)}) and the driver will bind it to any "
            "PDK, or name a model this PDK ships: "
            f"{', '.join(sorted(binding.device_models.values()))}."
        ),
    )


@dataclass(frozen=True, slots=True)
class RewrittenCard:
    """One MOS card rewritten into a PDK's spelling, with what changed."""

    text: str
    rewritten: bool
    model_translated: bool = False
    source_model: str | None = None
    target_model: str | None = None
    role: str | None = None
    #: Parameter keys the target PDK does not accept and that were therefore
    #: not emitted. Dropping is right - an unmapped key is a hard error on a
    #: ``.model`` PDK and silently ignored on a ``.subckt`` one - but doing it
    #: without saying so would make the author's intent vanish.
    dropped_parameters: tuple[str, ...] = ()
    #: Junction-geometry keys the author did supply.
    junction_parameters: tuple[str, ...] = ()
    #: Canonical terminals the target device does not model (e.g. a substrate
    #: node on a 3-terminal PNP subcircuit). Dropped and reported, like
    #: parameters.
    dropped_nodes: tuple[str, ...] = ()
    #: The card's instance name as the author wrote it, so a production fact
    #: or advisory can say *which* device dropped a node or had its geometry
    #: derived, not merely that one did.
    instance: str | None = None
    #: The canonical parameter keys the matched device card accepts. Threaded
    #: into the dropped-parameter advisory so the hint describes THIS card,
    #: not the binding-wide MOS vocabulary (an IHP diode dropping ``area``
    #: must not be told ``nf`` is accepted).
    accepted_parameters: tuple[str, ...] = ()
    #: Human-readable record of a geometry conversion (e.g. AREA/PJ -> W/L on
    #: a PDK that sizes its diode by W/L). None when nothing was derived.
    geometry_derived: str | None = None


#: Trailing-comment split, aligned with Simra's netlist_tool grammar: ``;``
#: anywhere starts a comment (Simra splits on the first ``;`` with no
#: whitespace required), and ``$`` preceded by whitespace starts a comment
#: whether or not any text follows (a bare trailing ``$`` is legal). A ``$``
#: glued to a token is not a comment in ngspice and stays in the card.
_TRAILING_COMMENT_RE = re.compile(
    r"(?P<card>.*?)(?P<comment>\s*;.*|\s\$(?:\s.*)?)$"
)


def _split_line_ending(line: str) -> tuple[str, str]:
    """Split one line into (body, line-ending), CRLF-aware.

    A deck written on Windows carries ``\\r\\n``; stripping only ``\\n``
    leaves the ``\\r`` glued to the last token, where it defeats every
    end-anchored card regex (review round-2 should-fix 2).
    """

    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _split_card_cosmetics(line: str) -> tuple[str, str, str, str]:
    """Split one physical/logical line into (indent, card, comment, newline).

    Rewriters match against the bare card; indentation and trailing comments
    are legal SPICE the author wrote deliberately, so they are reattached
    verbatim around whatever the rewrite produced.
    """

    body, newline = _split_line_ending(line)
    stripped = body.lstrip(" \t")
    indent = body[: len(body) - len(stripped)]
    comment = ""
    match = _TRAILING_COMMENT_RE.match(stripped)
    if match and not stripped.startswith(("*", ";")):
        stripped, comment = match.group("card").rstrip(), match.group("comment")
    return indent, stripped, comment, newline


def join_spice_continuations(deck_text: str) -> str:
    """Join ``+`` continuation lines into logical cards.

    SPICE reads a leading ``+`` as "continue the previous card"; the binder's
    per-line rewriters must therefore see joined cards or a continued device
    card would be rewritten only in part (production finding: a Sky130 card
    whose geometry sat on the continuation kept SI values past the micron
    rescale). Comment and blank lines pass through; a ``+`` with no previous
    card is left as-is for the simulator to refuse. Joining is CRLF-aware: a
    ``\\r`` must neither survive mid-card nor lose the line ending.
    """

    logical: list[str] = []
    for line in deck_text.splitlines(keepends=True):
        body, ending = _split_line_ending(line)
        stripped = body.lstrip(" \t")
        if stripped.startswith("+") and logical:
            prev, prev_ending = _split_line_ending(logical[-1])
            newline = ending or prev_ending
            logical[-1] = prev + " " + stripped[1:].strip() + newline
            continue
        logical.append(line)
    return "".join(logical)


def rewrite_mos_card(line: str, resolved: ResolvedPdkBinding) -> RewrittenCard:
    """Rewrite one canonical Simra MOS card into the target PDK's spelling.

    A line that is not a MOS card is returned unchanged. Everything a PDK can
    differ in -- instance prefix, model vocabulary, parameter spelling and the
    geometry unit convention -- is applied here, so that no caller and no deck
    author ever encodes a PDK-specific syntax rule.
    """

    if line.lstrip(" \t").startswith((".", "*")):
        return RewrittenCard(line, False)
    indent, body, comment, trailing = _split_card_cosmetics(line)
    match = MOS_CARD_RE.match(body)
    if match is None:
        role_hint = _ROLE_TOKEN_RE.search(body)
        if role_hint is not None and body[:1] in {"M", "m"}:
            raise PdkBindingError(
                "pdk.device.unbindable",
                f"The device card {body.split()[0]!r} names the canonical role "
                f"{role_hint.group('role')!r} but is not a four-terminal MOS card "
                "of the form '<name> <d> <g> <s> <b> <role> <k=v>...', so the "
                "driver cannot bind it to any PDK.",
                hint=(
                    "Canonical roles describe four-terminal MOS devices. A device "
                    "with a separate substrate or isolation terminal has no "
                    "canonical role yet and must be named by its PDK model."
                ),
            )
        return RewrittenCard(line, False)

    binding = resolved.binding
    source_name = match.group("name")
    name = source_name
    if binding.device_prefix == "x":
        # Prepend rather than substitute: Simra also emits ``X`` cards for
        # subcircuit instances, and substituting ``M_DUT`` -> ``X_DUT`` could
        # collide with one of them.
        name = f"x{name}"

    source_model = match.group("model")
    target_model, role, translated = translate_model(source_model, binding)

    divisor = binding.geometry_divisor
    envelope = binding.device_geometry.get(role) if role else None

    parameters: list[str] = []
    dropped: list[str] = []
    junction: list[str] = []
    for parameter in PARAMETER_RE.finditer(match.group("params")):
        key = parameter.group("key").lower()
        value = parameter.group("value")
        if key in _JUNCTION_PARAMETERS:
            junction.append(key)
        mapped = binding.parameter_names.get(key)
        if mapped is None:
            # Dropped deliberately: an unmapped key is a hard ngspice error
            # ("unknown parameter (ng)") on a PDK that ships .model cards, and
            # silently ignored on one that ships subcircuits. Dropping is the
            # only behaviour that is safe on both - but it is recorded, because
            # on three of the four installed PDKs neither dropping nor passing
            # it through would have produced any signal at all.
            dropped.append(key)
            continue
        if key in binding.geometry_parameters or key in binding.area_parameters:
            si = parse_spice_number(value)
            if envelope is not None:
                complaint = envelope.check(dimension=key, value=si)
                if complaint is not None:
                    raise PdkBindingError(
                        "pdk.device.geometry_out_of_range",
                        f"{name} ({target_model}): {complaint}.",
                        hint=(
                            "The simulator selects a model card by binning on "
                            "geometry; outside the envelope ngspice reports "
                            "'could not find a valid modelname'. Resize the "
                            "device, or choose a PDK whose devices cover it."
                        ),
                    )
            if divisor != 1:
                # An area rescales by the square of the unit convention; a
                # length or a perimeter rescales linearly.
                value = _format_number(
                    _rescaled(
                        si,
                        divisor,
                        squared=key in binding.area_parameters,
                    )
                )
        parameters.append(f"{mapped}={value}")

    nodes = " ".join(match.group("nodes").split())
    rebuilt = " ".join([name, nodes, target_model, *parameters])
    return RewrittenCard(
        indent + rebuilt + comment + trailing,
        True,
        model_translated=translated,
        source_model=source_model,
        target_model=target_model,
        role=role,
        dropped_parameters=tuple(dict.fromkeys(dropped)),
        junction_parameters=tuple(dict.fromkeys(junction)),
        instance=source_name,
        accepted_parameters=tuple(sorted(binding.parameter_names)),
    )


#: One extended-role device per line: ``<name> <nodes...> <model> <k=v>...``
#: with the canonical node count per letter (see ``_EXTENDED_CARD_NODES``):
#: 2 for D and C, 3 for physical R (n1 n2 BODY), 4 for Q (C B E SUB). The
#: model token must not contain ``=``.
_EXTENDED_CARD_RES = {
    letter: re.compile(
        rf"^(?P<name>[{letter.upper()}{letter}][^\s]*)"
        rf"(?P<nodes>(?:[ \t]+[^\s]+){{{count}}})"
        r"[ \t]+(?P<model>[^\s=]+)"
        r"(?P<params>(?:[ \t]+[^\s]+=[^\s]+)*)"
        r"[ \t]*$"
    )
    for letter, count in _EXTENDED_CARD_NODES.items()
}


def _is_spice_number(token: str) -> bool:
    try:
        parse_spice_number(token)
        return True
    except PdkBindingError:
        return False


#: Canonical extended-card parameter classes the binder validates itself.
#: A deck normally arrives through Simra's lint, but nothing forces that
#: path: a bare deck handed straight to OpenADA must meet the same canonical
#: contract or the violation sails through as a silent drop or a cryptic
#: simulator error.
_EXTENDED_LENGTH_KEYS = frozenset(("w", "l", "pj"))
_EXTENDED_AREA_KEYS = frozenset(("area",))
_EXTENDED_MULTIPLICITY_KEYS = frozenset(("m",))
#: The same believable-magnitude window Simra's lint enforces (SI). Stated
#: here too because a deck can reach the binder without passing lint, and
#: the geometry derivation squares an area while the unit rescale divides by
#: a squared divisor — bounded inputs keep every downstream Decimal step
#: inside the wide formatting context.
_EXTENDED_SCALAR_ABS_MAX = Decimal("1e60")
_EXTENDED_SCALAR_ABS_MIN = Decimal("1e-60")


def _in_scalar_window(value: Decimal) -> bool:
    """Whether one positive scalar sits inside the believable SI window."""

    return _EXTENDED_SCALAR_ABS_MIN <= value <= _EXTENDED_SCALAR_ABS_MAX


def _validated_extended_parameters(
    name: str, params_text: str
) -> dict[str, str]:
    """Parse one extended card's ``k=v`` list under the canonical contract.

    Typed refusals (``pdk.parameter.invalid``): a duplicated key, nonpositive
    geometry/area/perimeter, and a multiplicity that is not a whole number of
    parallel devices >= 1. Keys outside the canonical classes are collected
    untouched; whether they map or drop is the caller's per-PDK decision.
    """

    supplied: dict[str, str] = {}
    for parameter in PARAMETER_RE.finditer(params_text):
        key = parameter.group("key").lower()
        value = parameter.group("value")
        if key in supplied:
            raise PdkBindingError(
                "pdk.parameter.invalid",
                f"{name}: the parameter {key.upper()} is given twice; one "
                "card, one value.",
            )
        supplied[key] = value
    for key, value in supplied.items():
        if key in _EXTENDED_LENGTH_KEYS or key in _EXTENDED_AREA_KEYS:
            magnitude = parse_spice_number(value)
            if magnitude <= 0:
                raise PdkBindingError(
                    "pdk.parameter.invalid",
                    f"{name}: {key.upper()}={value} — geometry, area and "
                    "perimeter must be positive.",
                )
            if (
                magnitude > _EXTENDED_SCALAR_ABS_MAX
                or magnitude < _EXTENDED_SCALAR_ABS_MIN
            ):
                raise PdkBindingError(
                    "pdk.parameter.invalid",
                    f"{name}: {key.upper()}={value} — outside the believable "
                    "1e-60..1e60 SI magnitude window.",
                )
        elif key in _EXTENDED_MULTIPLICITY_KEYS:
            magnitude = parse_spice_number(value)
            if magnitude < 1 or magnitude != magnitude.to_integral_value():
                raise PdkBindingError(
                    "pdk.parameter.invalid",
                    f"{name}: {key.upper()}={value} — a device multiplicity "
                    "is a whole number of parallel devices, at least 1.",
                )
            if magnitude > _EXTENDED_SCALAR_ABS_MAX:
                raise PdkBindingError(
                    "pdk.parameter.invalid",
                    f"{name}: {key.upper()}={value} — outside the believable "
                    "1e-60..1e60 SI magnitude window.",
                )
    return supplied


def _derive_diode_geometry(
    name: str,
    card_spec: PdkDeviceCard,
    supplied: Mapping[str, str],
) -> tuple[dict[str, str], str | None]:
    """Convert diode geometry between the AREA/PJ and W/L conventions.

    Both spellings are canonical (Simra requires AREA or W/L), but each PDK's
    device takes exactly one of them, and a card written in the other one
    used to lose ALL geometry to the dropped-parameter path — the bound
    device silently took the PDK's unrelated default size (review round-2
    blocking finding 4). The conversion is exact where mathematics allows:

    * target wants AREA/PJ, author gave W/L: ``AREA = W*L``,
      ``PJ = 2*(W+L)`` — exact for the rectangle the author stated.
    * target wants W/L, author gave AREA (+PJ): the rectangle with that area
      and perimeter is recovered from ``x^2 - (PJ/2)x + AREA = 0``; with PJ
      absent or inconsistent (negative discriminant) the square
      ``W = L = sqrt(AREA)`` is assumed and the advisory says so.

    Returns the (possibly rewritten) parameter mapping and a human-readable
    derivation record for the ``pdk.device.geometry_derived`` advisory, or
    raises a typed refusal when no geometry can be supplied or derived at
    all — an unsized PDK diode is never emitted.
    """

    accepted = set(card_spec.parameter_names)
    wants_area = "area" in accepted
    wants_wl = "w" in accepted and "l" in accepted
    has_area = "area" in supplied
    has_wl = "w" in supplied and "l" in supplied
    result = dict(supplied)
    note: str | None = None

    if wants_area and not has_area and has_wl:
        with localcontext(_BINDING_DECIMAL_CONTEXT):
            width = parse_spice_number(supplied["w"])
            length = parse_spice_number(supplied["l"])
            area_value = width * length
            perimeter_value = 2 * (width + length)
        # Derived values obey the same believable window as authored ones:
        # two individually in-window lengths can multiply into an area far
        # outside it, and emitting that would launder an absurd scalar
        # through the derivation (round-4 should-fix 1).
        if not _in_scalar_window(area_value) or (
            "pj" in accepted and not _in_scalar_window(perimeter_value)
        ):
            raise PdkBindingError(
                "pdk.device.geometry_missing",
                f"{name}: the stated W/L describe an equivalent AREA/PJ "
                "outside the believable 1e-60..1e60 SI window, so no "
                f"geometry the {card_spec.model} device can take exists.",
                hint="Resize the diode so AREA=W*L stays within 1e-60..1e60.",
            )
        area = _format_number(area_value)
        result["area"] = area
        derived = [f"AREA={area}"]
        if "pj" in accepted and "pj" not in result:
            perimeter = _format_number(perimeter_value)
            result["pj"] = perimeter
            derived.append(f"PJ={perimeter}")
        del result["w"]
        del result["l"]
        # Geometry leads the emitted parameter list, like an authored card.
        result = {
            key: result[key]
            for key in ("area", "pj", *result)
            if key in result
        }
        note = (
            f"W/L -> {' '.join(derived)} "
            "(exact: AREA=W*L, PJ=2*(W+L), SI values before unit rescale)"
        )
    elif wants_wl and not has_wl and has_area:
        with localcontext(_BINDING_DECIMAL_CONTEXT):
            area = parse_spice_number(supplied["area"])
            perimeter = (
                parse_spice_number(supplied["pj"])
                if "pj" in supplied
                else None
            )
            width = length = None
            how = ""
            if perimeter is not None:
                half = perimeter / 2
                discriminant = half * half - 4 * area
                if discriminant >= 0:
                    root = discriminant.sqrt()
                    width = (half + root) / 2
                    # Small root via AREA / large_root: the textbook
                    # ``(half - root) / 2`` cancels catastrophically when
                    # AREA << PJ^2 and rounds to exactly zero — round-3
                    # blocking 2 caught a lint-clean AREA=1e-30 PJ=1
                    # emitting ``w=0.5 l=0``, an unsized device. The
                    # quotient form keeps w*l = AREA by construction.
                    length = area / width if width > 0 else None
                    how = "the exact rectangle with this AREA and PJ"
            # A rectangle whose dimensions are computable but outside the
            # believable window is as unusable as a degenerate one: two
            # in-window authored values (AREA=1e-60, PJ=1e60) can demand a
            # side of 2e-120 (round-4 should-fix 1).
            rectangle_out_of_window = (
                width is not None
                and length is not None
                and width.is_finite()
                and length.is_finite()
                and width > 0
                and length > 0
                and not (
                    _in_scalar_window(width) and _in_scalar_window(length)
                )
            )
            if (
                width is None
                or length is None
                or not width.is_finite()
                or not length.is_finite()
                or width <= 0
                or length <= 0
                or rectangle_out_of_window
            ):
                # Every derived value must be a positive finite in-window
                # length; anything else falls back to the square (positive
                # by construction, since the validator guaranteed AREA > 0).
                width = length = area.sqrt()
                if rectangle_out_of_window:
                    how = (
                        "assumed square W=L=sqrt(AREA): the exact "
                        "rectangle's dimensions fall outside the believable "
                        "1e-60..1e60 window"
                    )
                elif perimeter is None:
                    how = "assumed square W=L=sqrt(AREA): no PJ was supplied"
                else:
                    how = (
                        "assumed square W=L=sqrt(AREA): the supplied PJ "
                        "yields no numerically valid rectangle of this AREA"
                    )
        if not _in_scalar_window(width):
            raise PdkBindingError(
                "pdk.device.geometry_missing",
                f"{name}: no diode geometry inside the believable "
                "1e-60..1e60 SI window can realise the stated AREA/PJ on "
                f"the {card_spec.model} device.",
                hint="Resize the diode so sqrt(AREA) stays within "
                "1e-60..1e60.",
            )
        result["w"] = _format_number(width)
        result["l"] = _format_number(length)
        source = "AREA/PJ" if perimeter is not None else "AREA"
        note = (
            f"{source} -> W={result['w']} L={result['l']} "
            f"({how}; SI values before unit rescale)"
        )
        result.pop("area", None)
        result.pop("pj", None)
        # Geometry leads the emitted parameter list, like an authored card.
        result = {
            key: result[key] for key in ("w", "l", *result) if key in result
        }

    required = (
        ("area",) if wants_area else (("w", "l") if wants_wl else ())
    )
    if required and not all(key in result for key in required):
        raise PdkBindingError(
            "pdk.device.geometry_missing",
            f"{name}: the diode card supplies no geometry the "
            f"{card_spec.model} device can take and none can be derived — "
            f"it needs {' and '.join(key.upper() for key in required)} "
            "(or the convertible spelling: AREA [PJ] <-> W L). An unsized "
            "PDK diode would silently take the PDK's unrelated default "
            "geometry, so it is never emitted.",
            hint="State AREA= (with optional PJ=) or W= and L= on the card.",
        )
    return result, note


def rewrite_device_card(line: str, resolved: ResolvedPdkBinding) -> RewrittenCard:
    """Rewrite one extended-role card (D/Q, role-tagged R/C) for the PDK.

    Anything else — ideal R/C/L with numeric values, sources, X instances,
    directives — is returned unchanged. Indentation and trailing comments are
    preserved around the rewrite; role matching is case-insensitive. A card
    that names an extended role with the wrong arity is a typed refusal,
    never a silent pass-through into a cryptic simulator error.
    """

    if line.lstrip(" \t").startswith((".", "*")):
        return RewrittenCard(line, False)
    indent, card, comment, newline = _split_card_cosmetics(line)
    letter = card[:1].lower()
    if letter not in _EXTENDED_CARD_RES:
        return RewrittenCard(line, False)

    binding = resolved.binding
    allowed_roles = _EXTENDED_CARD_ROLES[letter]
    match = _EXTENDED_CARD_RES[letter].match(card)

    # Ideal passives stay ideal: on R/C a numeric token in the value/model
    # position (2-node form) is a value, not a model. This runs before the
    # arity checks so plain ``R1 A B 10k`` never enters role machinery. Note
    # the ngspice number grammar accepts trailing letters, so a token like
    # ``10ohm`` is necessarily a value — a physical device must use a role.
    if letter in ("r", "c"):
        tokens = card.split()
        if len(tokens) >= 4 and _is_spice_number(tokens[3]):
            return RewrittenCard(line, False)

    if match is None:
        # Arity guard: a role token on a malformed card must not slip through.
        for role in allowed_roles:
            if re.search(
                rf"(?:^|[ \t]){re.escape(role)}(?:[ \t]|$)", card, re.IGNORECASE
            ):
                raise PdkBindingError(
                    "pdk.device.unbindable",
                    f"The device card {card.split()[0]!r} names the canonical "
                    f"role {role!r} but does not have the canonical "
                    f"{_EXTENDED_CARD_NODES[letter]}-terminal form, so the "
                    "driver cannot bind it to any PDK.",
                    hint=(
                        "Canonical BJTs are C B E SUB and physical resistors "
                        "n1 n2 BODY — the substrate/body tie is stated by the "
                        "author, exactly like MOS bulk."
                    ),
                )
        return RewrittenCard(line, False)

    source_model = match.group("model")
    resolved_role = canonical_role(source_model.lower())
    known_native = {
        name.lower()
        for other in (binding, *REGISTRY.values())
        for role, name in other.device_models.items()
        if role in allowed_roles
    }
    if (
        resolved_role not in allowed_roles
        and source_model.lower() not in known_native
    ):
        if letter in ("d", "q"):
            # D/Q cards exist only to name devices; an unknown token there is
            # an error the author needs to hear, phrased by translate_model.
            translate_model(source_model, binding)
        return RewrittenCard(line, False)

    target_model, role, translated = translate_model(source_model, binding)
    card_spec = binding.device_cards.get(role or "")
    if card_spec is None:
        raise PdkBindingError(
            "pdk.model.unavailable",
            f"{binding.display_name} ships no device for the role {role!r}, "
            f"which the deck names as {source_model!r}.",
            hint=(
                "Roles this PDK offers: "
                f"{', '.join(sorted(binding.device_cards) or binding.device_models)}."
            ),
        )

    source_name = match.group("name")
    name = source_name
    if card_spec.card_kind == "subckt":
        name = f"x{name}"

    supplied = _validated_extended_parameters(
        source_name, match.group("params")
    )
    geometry_derived: str | None = None
    if role == "diode.core":
        # Diode geometry has two canonical spellings and each PDK takes one;
        # convert instead of letting a lint-clean card lose its size.
        supplied, geometry_derived = _derive_diode_geometry(
            source_name, card_spec, supplied
        )

    divisor = binding.geometry_divisor
    parameters: list[str] = []
    dropped: list[str] = []
    for key, value in supplied.items():
        mapped = card_spec.parameter_names.get(key)
        if mapped is None:
            dropped.append(key)
            continue
        if divisor != 1 and (
            key in card_spec.geometry_parameters
            or key in card_spec.area_parameters
        ):
            si = parse_spice_number(value)
            value = _format_number(
                _rescaled(
                    si,
                    divisor,
                    squared=key in card_spec.area_parameters,
                )
            )
        parameters.append(f"{mapped}={value}")
        # A companion spelling carries the same value into a separately-read
        # parameter (mismatch scaling: sky130 mult/mf, gf180 pnp par).
        for companion in card_spec.companion_parameters.get(key, ()):
            parameters.append(f"{companion}={value}")

    canonical_nodes = match.group("nodes").split()
    keep = (
        len(canonical_nodes)
        if card_spec.emit_nodes is None
        else card_spec.emit_nodes
    )
    emitted_nodes = canonical_nodes[:keep]
    dropped_nodes = tuple(canonical_nodes[keep:])
    rebuilt = " ".join([name, *emitted_nodes, target_model, *parameters])
    return RewrittenCard(
        indent + rebuilt + comment + newline,
        True,
        model_translated=translated,
        source_model=source_model,
        target_model=target_model,
        role=role,
        dropped_parameters=tuple(dict.fromkeys(dropped)),
        dropped_nodes=dropped_nodes,
        instance=source_name,
        accepted_parameters=tuple(sorted(card_spec.parameter_names)),
        geometry_derived=geometry_derived,
    )


def _prelude_lines(resolved: ResolvedPdkBinding) -> list[str]:
    lines: list[str] = []
    if resolved.osdi_paths:
        # ngspice accepts ``pre_osdi`` inside a control block; the same command
        # in a .spiceinit is rejected by this build.
        lines.append(".control\n")
        for module in resolved.osdi_paths:
            lines.append(f"pre_osdi {module}\n")
        lines.append(".endc\n")
    # Stated rather than inherited: no installed PDK sets a temperature itself,
    # so every deck silently ran at the simulator's default. A reviewer should
    # be able to read the condition off the deck.
    lines.append(f".option temp={resolved.binding.simulation_temperature_c}\n")
    scale = resolved.binding.geometry_scale
    if Decimal(scale) != 1:
        # Stated explicitly rather than relied upon: sky130 installs this from
        # its own corner collateral, but a deck that says what convention its
        # numbers are in can be reviewed without reading the PDK.
        lines.append(f".option scale={scale}\n")
    for option in resolved.binding.ngspice_options:
        lines.append(f".option {option}\n")
    for card in resolved.library_cards:
        lines.append(f"{card}\n")
    return lines


def _top_level_voltage_sources(
    deck_text: str,
    *,
    maximum: int | None,
) -> tuple[str, ...]:
    """Return canonical lowercase top-level voltage-source card names.

    ``maximum`` preserves the historical bounded discovery path.  The
    experiment path supplies an already-bounded exact set and therefore asks
    for the complete roster so every requested name can be checked rather than
    silently disappearing after source sixteen.
    """

    names: list[str] = []
    seen: set[str] = set()
    depth = 0
    for line in deck_text.splitlines():
        if _SUBCKT_OPEN_RE.match(line):
            depth += 1
            continue
        if _SUBCKT_CLOSE_RE.match(line):
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        match = _VSOURCE_CARD_RE.match(line)
        if match is None:
            continue
        name = match.group("name").lower()
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
        if maximum is not None and len(names) >= maximum:
            break
    return tuple(names)


def top_level_voltage_sources(deck_text: str) -> tuple[str, ...]:
    """Return the deck's own independent voltage sources, outermost only.

    Cards inside a ``.SUBCKT`` belong to the device under test, not to the
    testbench that drives it; ngspice names their branch currents through the
    instance path, and probing them is not what a testbench asked for.
    """

    return _top_level_voltage_sources(deck_text, maximum=MAX_PROBED_SOURCES)


def source_current_vectors(deck_text: str) -> tuple[str, ...]:
    """Return one ``i(source)`` vector per top-level independent source.

    These are the only amperes anywhere in the pipeline.
    `result.transfer.measure`'s `low_frequency_impedance` is volts over
    amperes, so without them a driving-point impedance cannot be expressed as
    a typed measurement at all -- and two live jobs
    (job_35abcfd447c0d85f, job_383d058ac044c601) proved it: both were asked for
    an output impedance, both built the right 1 V AC probe across the node,
    both reached `extract`, and both stopped there with nothing in amperes to
    name. `.SAVE`-ing only nets discarded the injected current before it ever
    reached the raw file.
    """

    return tuple(f"i({source})" for source in top_level_voltage_sources(deck_text))


def _explicit_source_current_vectors(
    deck_text: str,
    retained_current_sources: Sequence[str],
) -> tuple[str, ...]:
    """Resolve an exact caller-owned source set to canonical native vectors."""

    requested: list[str] = []
    seen: set[str] = set()
    for index, source in enumerate(retained_current_sources):
        if (
            not isinstance(source, str)
            or _EMITTED_VSOURCE_NAME_RE.fullmatch(source) is None
        ):
            raise PdkBindingError(
                "pdk.current_source.invalid",
                "retained_current_sources"
                f"[{index}] must be one emitted independent voltage-source name.",
            )
        canonical = source.lower()
        if canonical in seen:
            raise PdkBindingError(
                "pdk.current_source.duplicate",
                f"The retained current source {source!r} is duplicated "
                "case-insensitively.",
            )
        seen.add(canonical)
        requested.append(canonical)

    available = set(_top_level_voltage_sources(deck_text, maximum=None))
    missing = [source for source in requested if source not in available]
    if missing:
        raise PdkBindingError(
            "pdk.current_source.unknown",
            "Every explicitly retained current must name a top-level independent "
            "voltage source in the composed deck; not found as such: "
            f"{', '.join(missing)}.",
        )
    return tuple(f"i({source})" for source in requested)


def _validated_saved_nets(saved_nets: Sequence[str]) -> tuple[str, ...]:
    """Return a closed, case-insensitively unique saved-net sequence."""

    normalized: list[str] = []
    seen: set[str] = set()
    for index, net in enumerate(saved_nets):
        if not isinstance(net, str) or _SAVED_NET_RE.fullmatch(net) is None:
            raise PdkBindingError(
                "pdk.saved_net.invalid",
                f"saved_nets[{index}] is not a bounded SPICE net name.",
            )
        folded = net.casefold()
        if folded in seen:
            raise PdkBindingError(
                "pdk.saved_net.duplicate",
                f"The saved net {net!r} is duplicated case-insensitively.",
            )
        seen.add(folded)
        normalized.append(net)
    return tuple(normalized)


def _extend_save_card(line: str, currents: Sequence[str]) -> str:
    """Add the branch currents to a deck's own ``.save`` card, once."""

    match = _SAVE_CARD_RE.match(line)
    if match is None:
        return line
    rest = match.group("rest")
    present = {token.lower() for token in rest.split()}
    missing = [vector for vector in currents if vector not in present]
    if not missing:
        return line
    return f"{match.group('head')}{rest} {' '.join(missing)}{match.group('eol')}"


def _write_vectors(
    saved_nets: Sequence[str],
    currents: Sequence[str],
    *,
    exact_current_set: bool,
) -> str:
    """Return the ``write`` vector list: the saved nets, plus every current.

    A legacy bare ``write <file>`` dumps every vector ngspice has, branch
    currents included -- which is how a testbench that declares no saves at
    all can be measured for impedance today.  Preserve that only when the
    caller omitted an exact current set.  An explicit experiment-owned current
    set is itself the complete write list even when there are no saved nets.
    """

    if not saved_nets and not exact_current_set:
        return ""
    vectors = [f"v({net})" for net in saved_nets]
    seen = {vector.lower() for vector in vectors}
    for vector in currents:
        if vector in seen:
            continue
        seen.add(vector)
        vectors.append(vector)
    return " ".join(vectors)


def bind_deck(
    deck_text: str,
    resolved: ResolvedPdkBinding,
    *,
    raw_name: str | None = None,
    saved_nets: tuple[str, ...] = (),
    retained_current_sources: tuple[str, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return one PDK-bound deck plus the bounded facts describing the binding.

    The model-free deck gains, in order: its original title line, the PDK's
    OSDI preload, its geometry unit convention and required simulator options,
    its ordered library prelude, its own body with every MOS card rewritten,
    and -- when ``raw_name`` is given -- one closed control block that runs the
    deck's single analysis and writes that raw file.
    """

    unresolved = UNRESOLVED_TOKEN_RE.findall(deck_text)
    if unresolved:
        raise PdkBindingError(
            "pdk.deck.unresolved",
            "The deck carries unresolved publisher placeholders and cannot be "
            f"bound: {', '.join(sorted(set(unresolved))[:5])}",
            hint="Resolve every device parameter in Simra before binding a PDK.",
        )
    if resolved.captured_families is not None:
        # This snapshot was gated against another deck's family usage; a deck
        # needing a family whose libraries were never captured would rewrite
        # into a subcircuit the composed prelude cannot define.
        missing = scan_deck_families(deck_text) - resolved.captured_families
        if missing:
            raise PdkBindingError(
                "pdk.snapshot.family_missing",
                f"The captured {resolved.pdk_id} snapshot was resolved for a "
                "deck that used none of the device families "
                f"{', '.join(sorted(missing))}, so their model libraries were "
                "not captured; binding this deck against it would reference "
                "undefined subcircuits.",
                hint=(
                    "Re-resolve the PDK binding with this deck's text (or "
                    "with no deck text for the full conservative closure) and "
                    "bind again."
                ),
            )
    if raw_name is not None and not _RAW_NAME_RE.match(raw_name):
        raise PdkBindingError(
            "pdk.raw_name.invalid",
            f"The raw output name {raw_name!r} is not a bounded file name.",
        )

    lines = join_spice_continuations(deck_text).splitlines(keepends=True)
    if not lines:
        raise PdkBindingError("pdk.deck.empty", "The deck is empty.")

    normalized_saved_nets = _validated_saved_nets(saved_nets)
    explicit_current_vectors = (
        _explicit_source_current_vectors(deck_text, retained_current_sources)
        if retained_current_sources is not None
        else None
    )
    if raw_name is None:
        current_vectors: tuple[str, ...] = ()
    elif explicit_current_vectors is None:
        # Backward compatibility: legacy callers retain the first bounded set
        # of every top-level voltage-source current.
        current_vectors = source_current_vectors(deck_text)
    else:
        # An explicit empty tuple intentionally retains no currents.
        current_vectors = explicit_current_vectors
    write_vectors = _write_vectors(
        normalized_saved_nets,
        current_vectors,
        exact_current_set=retained_current_sources is not None,
    )
    bound: list[str] = []
    # A SPICE deck's first line is its title and is never a directive.
    title = lines[0]
    if not title.endswith("\n"):
        title += "\n"
    bound.append(title)
    bound.extend(_prelude_lines(resolved))

    rewritten = 0
    translations: dict[str, str] = {}
    dropped: dict[str, None] = {}
    #: One record per dropped parameter occurrence, carrying the scoped
    #: instance and the canonical keys THAT card accepts. Keying by bare
    #: parameter name and unioning accepted sets across card types would
    #: resurrect the false hint this fact exists to prevent (a MOS and a
    #: diode both dropping ``area`` must not merge into one vocabulary).
    dropped_parameter_records: list[dict[str, Any]] = []
    dropped_nodes: dict[str, list[str]] = {}
    geometry_derived: dict[str, str] = {}
    junction_supplied: dict[str, None] = {}
    roles_used: dict[str, None] = {}
    #: The enclosing ``.subckt`` names, so two subcircuits each containing a
    #: ``Q2`` stay two distinct occurrences ("A/Q2" vs "B/Q2") in every fact
    #: and advisory instead of merging into one misleading entry.
    subckt_stack: list[str] = []
    emitted_control = False
    for line in lines[1:]:
        if raw_name is not None and not emitted_control and _END_CARD_RE.match(line):
            bound.append(".control\n")
            bound.append("run\n")
            bound.append(
                f"write {raw_name}"
                f"{(' ' + write_vectors) if write_vectors else ''}\n"
            )
            bound.append(".endc\n")
            emitted_control = True
        if _SUBCKT_OPEN_RE.match(line):
            tokens = line.split()
            subckt_stack.append(tokens[1] if len(tokens) > 1 else "?")
        elif _SUBCKT_CLOSE_RE.match(line) and subckt_stack:
            subckt_stack.pop()
        if current_vectors:
            # `.save` selects what ngspice *computes*, not just what is
            # written, so a current named only in `write` is refused with
            # "no writable vector found" and the whole run loses its raw file.
            line = _extend_save_card(line, current_vectors)
        card = rewrite_mos_card(line, resolved)
        if not card.rewritten:
            card = rewrite_device_card(line, resolved)
        if card.rewritten:
            rewritten += 1
            occurrence = (
                "/".join((*subckt_stack, card.instance))
                if card.instance
                else None
            )
            if card.model_translated and card.source_model and card.target_model:
                translations[card.source_model] = card.target_model
            for key in card.dropped_parameters:
                dropped[key] = None
                dropped_parameter_records.append(
                    {
                        "instance": occurrence,
                        "parameter": key,
                        "accepted": sorted(card.accepted_parameters),
                    }
                )
            if card.dropped_nodes and occurrence:
                dropped_nodes.setdefault(occurrence, []).extend(
                    card.dropped_nodes
                )
            if card.geometry_derived and occurrence:
                geometry_derived[occurrence] = card.geometry_derived
            for key in card.junction_parameters:
                junction_supplied[key] = None
            if card.role:
                roles_used[card.role] = None
        bound.append(card.text)

    if raw_name is not None and not emitted_control:
        bound.append(".control\n")
        bound.append("run\n")
        bound.append(
            f"write {raw_name}{(' ' + write_vectors) if write_vectors else ''}\n"
        )
        bound.append(".endc\n")

    text = "".join(bound)
    if not text.endswith("\n"):
        text += "\n"

    binding = resolved.binding
    facts = resolved.facts()
    facts["rewritten_device_count"] = rewritten
    facts["model_translations"] = dict(sorted(translations.items()))
    facts["raw_output"] = raw_name
    facts["roles_bound"] = sorted(roles_used)
    facts["dropped_parameters"] = sorted(dropped)
    facts["dropped_parameter_records"] = dropped_parameter_records
    facts["dropped_nodes"] = {
        instance: list(nodes) for instance, nodes in sorted(dropped_nodes.items())
    }
    facts["geometry_derived"] = dict(sorted(geometry_derived.items()))
    facts["junction_geometry"] = binding.junction_geometry
    facts["junction_parameters_supplied"] = sorted(junction_supplied)
    facts["simulation_temperature_c"] = binding.simulation_temperature_c
    facts["model_tnom_c"] = binding.model_tnom_c
    facts["saved_nets"] = list(normalized_saved_nets)
    facts["retained_current_vectors"] = list(current_vectors)
    facts["current_retention"] = (
        "explicit" if retained_current_sources is not None else "legacy-auto"
    )
    facts["nominal_supply_v"] = {
        role: binding.nominal_supply_v[role]
        for role in sorted(roles_used)
        if role in binding.nominal_supply_v
    }
    # Corner-dependent: sky130's sf/fs select the typical NPN while ff/ss
    # select skewed ones, so the skew set must be read for THIS corner.
    skewed_for_corner = binding.skewed_roles_for(resolved.corner)
    facts["corner_skewed_roles"] = (
        None if skewed_for_corner is None else list(skewed_for_corner)
    )
    return text, facts


__all__ = [
    "DEVICE_ROLES",
    "DeviceGeometry",
    "FREEPDK45",
    "GF180MCUD",
    "IHP_SG13G2",
    "MAX_BOUND_FILES",
    "MOS_CARD_RE",
    "PdkBinding",
    "PdkBindingError",
    "PdkLibraryEntry",
    "REGISTRY",
    "ResolvedPdkBinding",
    "RewrittenCard",
    "SKY130A",
    "available_pdk_ids",
    "bind_deck",
    "device_role_index",
    "join_spice_continuations",
    "rewrite_device_card",
    "scan_deck_families",
    "parse_spice_number",
    "resolve_pdk_binding",
    "rewrite_mos_card",
    "simulatable_pdk_ids",
    "translate_model",
]
