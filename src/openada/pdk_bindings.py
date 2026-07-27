"""Per-PDK binding profiles that turn a model-free Simra deck into a runnable one.

Simra publishes a deliberately model-free deck: it names a device model and its
sizing but emits no model collateral, and it writes every MOS as a SPICE ``M``
card with Simra's own parameter spelling (``W``/``L``/``M``/``NF``). Whether
that card is runnable depends entirely on the target PDK:

* IHP SG13G2 ships ``sg13_lv_nmos`` as a ``.subckt`` (``sg13g2_moslv_mod.lib``)
  whose finger count is ``ng``, and its PSP103 core is a Verilog-A module that
  ngspice must load through OSDI before any device will bind.
* A PDK shipping plain ``.model`` cards needs neither the ``X`` prefix nor a
  parameter rename.

So the device prefix, the parameter spelling, the library entry point and the
OSDI preload are all *properties of the PDK*, not constants. Encoding them here
keeps that knowledge in one reviewed table instead of spreading it across agent
instructions, where every new PDK would become a new class of silent failure.

This module owns the binding only. It resolves one named PDK against a root,
content-binds every file it will reference, and rewrites one deck. It runs no
simulator and makes no engineering claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

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

#: A bound deck references a small, fixed set of PDK files. The ceiling keeps a
#: malformed binding from enumerating an unbounded tree.
MAX_BOUND_FILES = 8


class PdkBindingError(Exception):
    """One bounded, typed reason a deck cannot be bound to a PDK."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


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
    #: Relative path of the corner library entry point, from the PDK root.
    library_relative_path: str
    corners: tuple[str, ...]
    default_corner: str
    #: Verilog-A modules ngspice must load before any device binds. Empty for a
    #: PDK whose devices are built-in models.
    osdi_relative_paths: tuple[str, ...] = ()
    #: Relative path of a PDK identity/version file, recorded when present.
    identity_relative_path: str | None = None
    notes: str = ""

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


#: IHP SG13G2's low-voltage MOS devices are subcircuits taking ``ng`` fingers,
#: and their PSP103 core is Verilog-A. Verified against
#: ``libs.tech/ngspice/models/sg13g2_moslv_mod.lib:66`` and a passing transient
#: run of a published Simra inverter testbench.
IHP_SG13G2 = PdkBinding(
    pdk_id="ihp-sg13g2",
    display_name="IHP SG13G2",
    device_prefix="x",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "ng"},
    library_relative_path="libs.tech/ngspice/models/cornerMOSlv.lib",
    corners=(
        "mos_tt",
        "mos_ss",
        "mos_ff",
        "mos_sf",
        "mos_fs",
    ),
    default_corner="mos_tt",
    osdi_relative_paths=(
        "libs.tech/ngspice/osdi/psp103.osdi",
        "libs.tech/ngspice/osdi/psp103_nqs.osdi",
    ),
    identity_relative_path="COMMIT",
    notes=(
        "Low-voltage MOS only. sg13_lv_nmos/sg13_lv_pmos are subcircuits whose "
        "finger count is ng; PSP103 requires an OSDI preload."
    ),
)

#: SkyWater sky130A ships BSIM4 ``.model`` cards, so Simra's emitted ``M``
#: prefix and ``nf`` spelling are already correct and no OSDI preload applies.
#: Declared from the PDK's published layout; not exercised by a live run here.
SKY130A = PdkBinding(
    pdk_id="sky130A",
    display_name="SkyWater sky130A",
    device_prefix="m",
    parameter_names={"w": "w", "l": "l", "m": "m", "nf": "nf"},
    library_relative_path="libs.tech/ngspice/sky130.lib.spice",
    corners=("tt", "ss", "ff", "sf", "fs"),
    default_corner="tt",
    osdi_relative_paths=(),
    identity_relative_path=None,
    notes="Declared from the published PDK layout; not verified by a live run.",
)

REGISTRY: dict[str, PdkBinding] = {
    IHP_SG13G2.pdk_id: IHP_SG13G2,
    SKY130A.pdk_id: SKY130A,
}


def available_pdk_ids() -> tuple[str, ...]:
    return tuple(sorted(REGISTRY))


@dataclass(frozen=True, slots=True)
class ResolvedPdkBinding:
    """One PDK binding resolved against a root, with every file content-bound."""

    binding: PdkBinding
    root: Path
    corner: str
    library_path: Path
    osdi_paths: tuple[Path, ...]
    identity_path: Path | None
    input_records: tuple[dict[str, Any], ...]

    @property
    def pdk_id(self) -> str:
        return self.binding.pdk_id

    def facts(self) -> dict[str, Any]:
        """Return the bounded, serializable description of this binding."""

        return {
            "pdk_id": self.binding.pdk_id,
            "display_name": self.binding.display_name,
            "root": str(self.root),
            "corner": self.corner,
            "library": str(self.library_path),
            "device_prefix": self.binding.device_prefix,
            "parameter_names": dict(self.binding.parameter_names),
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

    library_path = root / binding.library_relative_path
    if not library_path.is_file():
        raise PdkBindingError(
            "pdk.library.missing",
            f"{binding.pdk_id} expects its corner library at {library_path}, "
            "which does not exist.",
            hint="Check that --pdk-root points at the installed PDK.",
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
        _bind_file(library_path, kind="spice-model-library", role="pdk.corner-library")
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
        library_path=library_path,
        osdi_paths=tuple(osdi_paths),
        identity_path=identity_path,
        input_records=tuple(records),
    )


def rewrite_mos_card(line: str, resolved: ResolvedPdkBinding) -> tuple[str, bool]:
    """Rewrite one Simra MOS card into the target PDK's spelling.

    Returns ``(line, rewritten)``. A line that is not a MOS card is returned
    unchanged.
    """

    if line.startswith(".") or line.startswith("*"):
        return line, False
    body = line.rstrip("\n")
    trailing = line[len(body) :]
    match = MOS_CARD_RE.match(body)
    if match is None:
        return line, False

    binding = resolved.binding
    name = match.group("name")
    if binding.device_prefix == "x":
        # Prepend rather than substitute: Simra also emits ``X`` cards for
        # subcircuit instances, and substituting ``M_DUT`` -> ``X_DUT`` could
        # collide with one of them.
        name = f"x{name}"

    parameters: list[str] = []
    for parameter in PARAMETER_RE.finditer(match.group("params")):
        key = parameter.group("key").lower()
        value = parameter.group("value")
        mapped = binding.parameter_names.get(key)
        if mapped is None:
            # Dropped deliberately: an unknown parameter is a hard ngspice
            # error ("unknown parameter (nf)"), not a warning.
            continue
        parameters.append(f"{mapped}={value}")

    nodes = " ".join(match.group("nodes").split())
    rebuilt = " ".join([name, nodes, match.group("model"), *parameters])
    return rebuilt + trailing, True


def _prelude_lines(resolved: ResolvedPdkBinding) -> list[str]:
    lines: list[str] = []
    if resolved.osdi_paths:
        # ngspice accepts ``pre_osdi`` inside a control block; the same command
        # in a .spiceinit is rejected by this build.
        lines.append(".control\n")
        for module in resolved.osdi_paths:
            lines.append(f"pre_osdi {module}\n")
        lines.append(".endc\n")
    # Two-argument form: a bare ``.include`` of a corner library fails with
    # "unimplemented dot command '.lib'" because the section is never selected.
    lines.append(f".lib {resolved.library_path} {resolved.corner}\n")
    return lines


def bind_deck(
    deck_text: str,
    resolved: ResolvedPdkBinding,
    *,
    raw_name: str | None = None,
    saved_nets: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    """Return one PDK-bound deck plus the bounded facts describing the binding.

    The model-free deck gains, in order: its original title line, the PDK's
    OSDI preload, the two-argument corner ``.lib``, its own body with every MOS
    card rewritten, and -- when ``raw_name`` is given -- one closed control
    block that runs the deck's single analysis and writes that raw file.
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

    bound: list[str] = []
    # A SPICE deck's first line is its title and is never a directive.
    title = lines[0]
    if not title.endswith("\n"):
        title += "\n"
    bound.append(title)
    bound.extend(_prelude_lines(resolved))

    rewritten = 0
    emitted_control = False
    for line in lines[1:]:
        if raw_name is not None and not emitted_control and _END_CARD_RE.match(line):
            bound.append(".control\n")
            bound.append("run\n")
            saved = " ".join(f"v({net})" for net in saved_nets)
            bound.append(f"write {raw_name}{(' ' + saved) if saved else ''}\n")
            bound.append(".endc\n")
            emitted_control = True
        replaced, changed = rewrite_mos_card(line, resolved)
        if changed:
            rewritten += 1
        bound.append(replaced)

    if raw_name is not None and not emitted_control:
        bound.append(".control\n")
        bound.append("run\n")
        saved = " ".join(f"v({net})" for net in saved_nets)
        bound.append(f"write {raw_name}{(' ' + saved) if saved else ''}\n")
        bound.append(".endc\n")

    text = "".join(bound)
    if not text.endswith("\n"):
        text += "\n"

    facts = resolved.facts()
    facts["rewritten_device_count"] = rewritten
    facts["raw_output"] = raw_name
    return text, facts


__all__ = [
    "IHP_SG13G2",
    "MAX_BOUND_FILES",
    "MOS_CARD_RE",
    "PdkBinding",
    "PdkBindingError",
    "REGISTRY",
    "ResolvedPdkBinding",
    "SKY130A",
    "available_pdk_ids",
    "bind_deck",
    "resolve_pdk_binding",
    "rewrite_mos_card",
]
