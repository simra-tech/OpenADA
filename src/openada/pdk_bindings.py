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
  the opaque ``could not find a valid modelname``.
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

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .contract import FileRecordError, file_record


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

#: A bound deck references a small, fixed set of PDK files. The ceiling keeps a
#: malformed binding from enumerating an unbounded tree. FreePDK45 is the widest
#: reviewed profile at eight per-corner model files.
MAX_BOUND_FILES = 12

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
    #: The value of ``.option scale`` this PDK's own collateral installs, as a
    #: decimal string. Simra's SI geometry is divided by it, so ``1`` means the
    #: deck's numbers pass through untouched and ``1e-6`` means the PDK expects
    #: plain micron numbers.
    geometry_scale: str = "1"
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


def _format_number(value: Decimal) -> str:
    """Render one Decimal as a SPICE literal without losing exactness."""

    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        # ``normalize`` renders 100 as ``1E+2``; legal SPICE, but unreadable in
        # a deck a human may have to review.
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def parse_spice_number(text: str) -> Decimal:
    """Return the SI value of one SPICE numeric literal."""

    match = SPICE_NUMBER_RE.match(text)
    if match is None:
        raise PdkBindingError(
            "pdk.parameter.unparsable",
            f"The parameter value {text!r} is not a SPICE numeric literal.",
            hint="Every geometry a PDK binding rescales must be a plain number.",
        )
    try:
        magnitude = Decimal(match.group("number"))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise PdkBindingError(
            "pdk.parameter.unparsable",
            f"The parameter value {text!r} is not a SPICE numeric literal.",
        ) from exc
    suffix = (match.group("suffix") or "").lower()
    return magnitude * _SPICE_SCALE[suffix]


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
    ),
    corners=("mos_tt", "mos_ss", "mos_ff", "mos_sf", "mos_fs"),
    default_corner="mos_tt",
    device_models={
        "nmos.core": "sg13_lv_nmos",
        "pmos.core": "sg13_lv_pmos",
        "nmos.io": "sg13_hv_nmos",
        "pmos.io": "sg13_hv_pmos",
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
        "Low-voltage MOS only. sg13_lv_nmos/sg13_lv_pmos are subcircuits whose "
        "finger count is ng; PSP103 requires an OSDI preload. PSP103 is a "
        "continuous compact model, so no binning envelope is enforced."
    ),
)

#: SkyWater sky130A ships its FETs as subcircuits over binned BSIM4 cards, and
#: its own corner collateral installs ``.option scale=1.0u``
#: (``libs.tech/ngspice/all.spice:2``, included from ``corners/<corner>.spice``).
#: Geometry must therefore be written in microns. Binning extents read from
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
    },
    geometry_scale="1e-6",
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
    osdi_relative_paths=(),
    identity_relative_path=None,
    notes=(
        "Devices are subcircuits over binned BSIM4 models. The PDK sets "
        "scale=1u itself, so instance geometry is expressed in microns; an "
        "SI-valued card lands outside every bin. Parsing the full tt library "
        "takes ~95 s on a 2025 host."
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
    ),
    corners=("typical", "ff", "ss", "fs", "sf"),
    default_corner="typical",
    device_models={
        "nmos.core": "nfet_03v3",
        "pmos.core": "pfet_03v3",
        "nmos.io": "nfet_06v0",
        "pmos.io": "pfet_06v0",
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
    """

    index: dict[str, str] = {}
    for binding in REGISTRY.values():
        for role, model in binding.device_models.items():
            index.setdefault(model.lower(), role)
    return index


@dataclass(frozen=True, slots=True)
class ResolvedPdkBinding:
    """One PDK binding resolved against a root, with every file content-bound."""

    binding: PdkBinding
    root: Path
    corner: str
    library_paths: tuple[Path, ...]
    library_cards: tuple[str, ...]
    osdi_paths: tuple[Path, ...]
    identity_path: Path | None
    input_records: tuple[dict[str, Any], ...]

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
            "root": str(self.root),
            "corner": self.corner,
            "library": str(self.library_path),
            "library_cards": list(self.library_cards),
            "device_prefix": self.binding.device_prefix,
            "parameter_names": dict(self.binding.parameter_names),
            "geometry_scale": self.binding.geometry_scale,
            "device_models": dict(self.binding.device_models),
            "osdi_modules": [str(path) for path in self.osdi_paths],
            "identity": str(self.identity_path) if self.identity_path else None,
        }


def _bind_file(path: Path, *, kind: str, role: str) -> dict[str, Any]:
    try:
        return file_record(path, kind=kind, role=role)
    except FileRecordError as exc:
        raise PdkBindingError(
            "pdk.file.unreadable",
            f"The PDK file {path} could not be content-bound: {exc}",
        ) from exc


def resolve_pdk_binding(
    pdk_id: str,
    pdk_root: str | Path,
    *,
    corner: str | None = None,
) -> ResolvedPdkBinding:
    """Resolve one named PDK against ``pdk_root`` and content-bind its files.

    ``pdk_root`` is the directory *containing* the PDK tree, matching the
    ``PDK_ROOT``/``PDK`` split every open PDK uses. A root that already points
    at the PDK directory itself is also accepted.
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

    root = Path(pdk_root).expanduser()
    if not root.is_absolute():
        raise PdkBindingError(
            "pdk.root.invalid",
            f"The PDK root must be an absolute path: {root}",
        )
    root = root.resolve()
    # Accept either the parent of the PDK tree or the tree itself.
    candidate = root / binding.pdk_id
    if candidate.is_dir():
        root = candidate
    if not root.is_dir():
        raise PdkBindingError(
            "pdk.root.missing",
            f"The PDK root {root} is not a directory.",
        )

    selected_corner = binding.resolved_corner(corner)

    library_paths: list[Path] = []
    library_cards: list[str] = []
    for entry in binding.library_entries:
        relative, section = entry.resolve(selected_corner)
        path = root / relative
        if not path.is_file():
            raise PdkBindingError(
                "pdk.library.missing",
                f"{binding.pdk_id} expects {path}, which does not exist.",
                hint="Check that --pdk-root points at the installed PDK.",
            )
        library_paths.append(path)
        library_cards.append(
            f".lib {path} {section}" if entry.form == "lib" else f".include {path}"
        )

    osdi_paths: list[Path] = []
    for relative in binding.osdi_relative_paths:
        module = root / relative
        if not module.is_file():
            raise PdkBindingError(
                "pdk.osdi.missing",
                f"{binding.pdk_id} requires the Verilog-A module {module}, "
                "which does not exist.",
                hint=(
                    "Without its OSDI modules this PDK's devices cannot bind and "
                    "ngspice reports an unknown model type."
                ),
            )
        osdi_paths.append(module)

    identity_path: Path | None = None
    if binding.identity_relative_path:
        candidate_identity = root / binding.identity_relative_path
        if candidate_identity.is_file():
            identity_path = candidate_identity

    records: list[dict[str, Any]] = [
        _bind_file(path, kind="spice-model-library", role="pdk.corner-library")
        for path in library_paths
    ]
    for module in osdi_paths:
        records.append(
            _bind_file(module, kind="verilog-a-module", role="pdk.osdi-module")
        )
    if identity_path is not None:
        records.append(
            _bind_file(identity_path, kind="pdk-identity", role="pdk.identity")
        )
    if len(records) > MAX_BOUND_FILES:
        raise PdkBindingError(
            "pdk.files.over_limit",
            f"{len(records)} bound PDK files exceed the ceiling of {MAX_BOUND_FILES}.",
        )

    return ResolvedPdkBinding(
        binding=binding,
        root=root,
        corner=selected_corner,
        library_paths=tuple(library_paths),
        library_cards=tuple(library_cards),
        osdi_paths=tuple(osdi_paths),
        identity_path=identity_path,
        input_records=tuple(records),
    )


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
    # industry uses interchangeably; resolve before consulting the profile.
    resolved_role = canonical_role(model)
    if resolved_role in binding.device_models:  # a canonical role
        return binding.device_models[resolved_role], resolved_role, True
    if lowered in native:  # already this PDK's own model
        role, name = native[lowered]
        return name, role, name != model
    role_name = device_role_index().get(lowered)
    if role_name is None and resolved_role in DEVICE_ROLES:
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


def rewrite_mos_card(line: str, resolved: ResolvedPdkBinding) -> RewrittenCard:
    """Rewrite one canonical Simra MOS card into the target PDK's spelling.

    A line that is not a MOS card is returned unchanged. Everything a PDK can
    differ in -- instance prefix, model vocabulary, parameter spelling and the
    geometry unit convention -- is applied here, so that no caller and no deck
    author ever encodes a PDK-specific syntax rule.
    """

    if line.startswith(".") or line.startswith("*"):
        return RewrittenCard(line, False)
    body = line.rstrip("\n")
    trailing = line[len(body) :]
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
    name = match.group("name")
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
                    si / (divisor * divisor)
                    if key in binding.area_parameters
                    else si / divisor
                )
        parameters.append(f"{mapped}={value}")

    nodes = " ".join(match.group("nodes").split())
    rebuilt = " ".join([name, nodes, target_model, *parameters])
    return RewrittenCard(
        rebuilt + trailing,
        True,
        model_translated=translated,
        source_model=source_model,
        target_model=target_model,
        role=role,
        dropped_parameters=tuple(dict.fromkeys(dropped)),
        junction_parameters=tuple(dict.fromkeys(junction)),
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
    for card in resolved.library_cards:
        lines.append(f"{card}\n")
    return lines


def top_level_voltage_sources(deck_text: str) -> tuple[str, ...]:
    """Return the deck's own independent voltage sources, outermost only.

    Cards inside a ``.SUBCKT`` belong to the device under test, not to the
    testbench that drives it; ngspice names their branch currents through the
    instance path, and probing them is not what a testbench asked for.
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
        if len(names) >= MAX_PROBED_SOURCES:
            break
    return tuple(names)


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


def _write_vectors(saved_nets: Sequence[str], currents: Sequence[str]) -> str:
    """Return the ``write`` vector list: the saved nets, plus every current.

    A bare ``write <file>`` dumps every vector ngspice has, branch currents
    included -- which is how a testbench that declares no saves at all can be
    measured for impedance today. Narrowing the list to ``v(net)`` per saved
    net therefore *removed* capability: the saved-net list says which **nets**
    to keep and was never a statement about currents, and one complex vector
    per source is free beside them.
    """

    if not saved_nets:
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
) -> tuple[str, dict[str, Any]]:
    """Return one PDK-bound deck plus the bounded facts describing the binding.

    The model-free deck gains, in order: its original title line, the PDK's
    OSDI preload, its geometry unit convention, its ordered library prelude, its
    own body with every MOS card rewritten, and -- when ``raw_name`` is given --
    one closed control block that runs the deck's single analysis and writes
    that raw file.
    """

    unresolved = UNRESOLVED_TOKEN_RE.findall(deck_text)
    if unresolved:
        raise PdkBindingError(
            "pdk.deck.unresolved",
            "The deck carries unresolved publisher placeholders and cannot be "
            f"bound: {', '.join(sorted(set(unresolved))[:5])}",
            hint="Resolve every device parameter in Simra before binding a PDK.",
        )
    if raw_name is not None and not _RAW_NAME_RE.match(raw_name):
        raise PdkBindingError(
            "pdk.raw_name.invalid",
            f"The raw output name {raw_name!r} is not a bounded file name.",
        )

    lines = deck_text.splitlines(keepends=True)
    if not lines:
        raise PdkBindingError("pdk.deck.empty", "The deck is empty.")

    current_vectors = source_current_vectors(deck_text) if raw_name is not None else ()
    write_vectors = _write_vectors(saved_nets, current_vectors)
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
    junction_supplied: dict[str, None] = {}
    roles_used: dict[str, None] = {}
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
        if current_vectors:
            # `.save` selects what ngspice *computes*, not just what is
            # written, so a current named only in `write` is refused with
            # "no writable vector found" and the whole run loses its raw file.
            line = _extend_save_card(line, current_vectors)
        card = rewrite_mos_card(line, resolved)
        if card.rewritten:
            rewritten += 1
            if card.model_translated and card.source_model and card.target_model:
                translations[card.source_model] = card.target_model
            for key in card.dropped_parameters:
                dropped[key] = None
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
    facts["junction_geometry"] = binding.junction_geometry
    facts["junction_parameters_supplied"] = sorted(junction_supplied)
    facts["simulation_temperature_c"] = binding.simulation_temperature_c
    facts["model_tnom_c"] = binding.model_tnom_c
    facts["nominal_supply_v"] = {
        role: binding.nominal_supply_v[role]
        for role in sorted(roles_used)
        if role in binding.nominal_supply_v
    }
    facts["corner_skewed_roles"] = (
        None
        if binding.corner_skewed_roles is None
        else list(binding.corner_skewed_roles)
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
    "parse_spice_number",
    "resolve_pdk_binding",
    "rewrite_mos_card",
    "simulatable_pdk_ids",
    "translate_model",
]
