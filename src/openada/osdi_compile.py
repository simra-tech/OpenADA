"""Compile a reviewed Verilog-A behavioral source to an OSDI module and make it
a drop-in for the ngspice-native block backend.

This is the Step-2 counterpart to :mod:`openada.block_library`. The block library
composes ``ngspice-native`` ``.subckt`` text directly into the model-library
role. Here the ``verilog-a`` backend of the same block is compiled with
OpenVAF / OpenVAF-Reloaded to a ``.osdi`` shared object, and a preload prelude is
emitted so a testbench that instantiates the public ``bhv_<block>_v<abi>``
wrapper by an ``X`` card runs identically to the native backend — the OSDI module
sits behind a generated wrapper subcircuit of the exact same pins and header
parameters.

Every step is digest-bound and fail-closed, mirroring the block library:
the Verilog-A source bytes, the produced ``.osdi`` bytes, and the exact compiler
version string are all recorded so a run can attest what physics it executed.

ngspice constraints this module encodes:
  * ``pre_osdi`` is only honored inside a ``.control`` block, emitted once at the
    top of the deck (before the circuit is parsed).
  * an OSDI module is bound to a ``.model`` name and instantiated with an ``N``
    device; the generated wrapper subcircuit hides that behind the public name.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .discovery import TOOL_SPECS

#: Ceiling on a compiled OSDI module — a reviewed behavioral cell is tiny; a huge
#: artifact means the compiler emitted something unexpected and we refuse it
#: rather than load it blindly.
MAX_OSDI_BYTES = 8 * 1024 * 1024
#: Ceiling on a Verilog-A source. Reviewed cells are a few dozen lines.
MAX_VA_BYTES = 256 * 1024

#: A plain SPICE scalar (optionally signed, with an engineering suffix) — the
#: only shape a wrapper header default or a passed parameter override may take.
#: No braces, expressions, or identifiers reach the generated deck.
_SPICE_SCALAR_RE = re.compile(
    r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?:t|g|meg|k|m|u|n|p|f|a)?$",
    re.IGNORECASE,
)
#: A plain SPICE identifier (net or parameter name). Case-insensitive to match
#: ngspice, but the generated text preserves the reviewed spelling.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: A Verilog-A module name as emitted by the block wrapper contract.
_MODULE_RE = re.compile(r"^bhv_[A-Za-z0-9_]+_v[0-9]+$")
#: Any whitespace (incl. newline/CR/tab) or C0/C1 control byte. A compiled OSDI
#: path is written onto a bare ``pre_osdi <path>`` line inside a .control block;
#: ngspice tokenizes on whitespace and breaks on newlines, so a work_dir with a
#: space or newline would split that command (or smuggle a second one). The path
#: is refused rather than quoted — ngspice ``pre_osdi`` has no quoting.
_PATH_UNSAFE_RE = re.compile(r"[\s\x00-\x1f\x7f]")
#: Verilog-A event/timing constructs the OSDI path must refuse. Classic
#: openvaf 23.5.0 rejects them at compile; OpenVAF-Reloaded (openvaf-r, the
#: prod-image compiler) COMPILES them but the event gating is silently not
#: honored through OSDI/ngspice (measured 2026-08-04: an @(cross)-guarded
#: assignment behaves as continuously evaluated — the sampled state follows
#: the input after the edge instead of latching). Fail closed on the SOURCE so
#: neither compiler generation can ship silently-wrong event semantics.
#: (@(initial_step)/@(final_step) are NOT screened: both compilers support
#: them and their run-at-analysis-start semantics need no event queue —
#: opamp_1p relies on @(initial_step).)
_EVENT_CONSTRUCT_RE = re.compile(
    r"@\s*\(\s*(?:cross|above|timer)\b|\btransition\s*\(|\babsdelay\s*\("
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class OsdiCompileError(Exception):
    """An OSDI compile/preload refusal with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OsdiModule:
    """A compiled OSDI module and the provenance that binds it to its source."""

    module_name: str
    osdi_path: Path
    source_sha256: str
    osdi_sha256: str
    compiler: str
    compiler_version: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_openvaf(*, override: str | None = None) -> tuple[str, str]:
    """Return ``(binary_path, version_string)`` for an available OpenVAF.

    Probes the binaries the discovery layer declares for the ``openvaf`` tool
    (``openvaf-r`` on the prod image, ``openvaf`` on classic/local installs).
    Refuses closed if none resolve, so the caller never shells out to a missing
    compiler.
    """

    candidates: Sequence[str]
    if override is not None:
        candidates = (override,)
    else:
        candidates = TOOL_SPECS["openvaf"].binaries
    for name in candidates:
        resolved = shutil.which(name)
        if resolved is None and Path(name).is_file():
            resolved = name
        if resolved is None:
            continue
        try:
            completed = subprocess.run(
                [resolved, "--version"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OsdiCompileError(
                "osdi.compiler.unusable",
                f"OpenVAF binary {resolved!r} could not be probed: {exc}",
            ) from exc
        version = (completed.stdout or "").strip().splitlines()
        return resolved, (version[0].strip() if version else "unknown")
    raise OsdiCompileError(
        "osdi.compiler.unavailable",
        "no OpenVAF compiler is available; the OSDI backend needs one of "
        + ", ".join(repr(b) for b in candidates)
        + " on PATH.",
    )


def compile_verilog_a(
    source_text: str,
    expected_module: str,
    work_dir: Path,
    *,
    openvaf_bin: str | None = None,
) -> OsdiModule:
    """Compile one reviewed Verilog-A source to a digest-bound OSDI module.

    ``expected_module`` is the public wrapper name the block contract promises;
    the compiled ``<module>.osdi`` is named for it so the preload prelude can
    bind it deterministically. The source and output are size-bounded and
    fully digest-recorded.
    """

    if not _MODULE_RE.fullmatch(expected_module):
        raise OsdiCompileError(
            "osdi.module.invalid",
            f"module name {expected_module!r} is not a bhv_<block>_v<abi> wrapper.",
        )
    if not isinstance(source_text, str):
        raise OsdiCompileError(
            "osdi.source.invalid",
            f"Verilog-A source must be text, not {type(source_text).__name__}.",
        )
    # The produced .osdi path is emitted verbatim onto a pre_osdi line; a
    # work_dir carrying whitespace/newlines would break that deck command, so
    # the destination is refused up front rather than after a wasted compile.
    if _PATH_UNSAFE_RE.search(str(work_dir)):
        raise OsdiCompileError(
            "osdi.workdir.unsafe",
            f"work_dir {str(work_dir)!r} contains whitespace or a control "
            "character; the compiled OSDI path must sit on a bare pre_osdi line.",
        )
    encoded = source_text.encode("utf-8")
    if not 0 < len(encoded) <= MAX_VA_BYTES:
        raise OsdiCompileError(
            "osdi.source.oversize",
            f"Verilog-A source is {len(encoded)} bytes, outside 1..{MAX_VA_BYTES}.",
        )
    # Event/timing constructs are refused BEFORE the compiler runs: classic
    # openvaf rejects them anyway, and openvaf-r compiles them into OSDI whose
    # event gating ngspice silently does not honor (continuously-evaluated
    # bodies) — a wrong-answer trap, not a capability.
    stripped = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", source_text))
    event_hit = _EVENT_CONSTRUCT_RE.search(stripped)
    if event_hit:
        raise OsdiCompileError(
            "osdi.source.event_constructs",
            f"Verilog-A source for {expected_module} uses the event/timing "
            f"construct {event_hit.group(0).strip()!r}; the OSDI path refuses "
            "these because their sampled semantics are not honored through "
            "OSDI/ngspice (silently continuous evaluation). Use a continuous "
            "formulation or the block's native/cosim backend.",
        )
    source_sha256 = _sha256_bytes(encoded)
    binary, version = resolve_openvaf(override=openvaf_bin)

    work_dir.mkdir(parents=True, exist_ok=True)
    # Compile in an isolated scratch dir so a hostile source cannot overwrite a
    # sibling; only the named source and output live here.
    scratch = Path(tempfile.mkdtemp(prefix="osdi-", dir=work_dir))
    va_path = scratch / f"{expected_module}.va"
    osdi_path = scratch / f"{expected_module}.osdi"
    va_path.write_bytes(encoded)
    try:
        completed = subprocess.run(
            [binary, str(va_path), "-o", str(osdi_path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            cwd=str(scratch),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OsdiCompileError(
            "osdi.compile.failed",
            f"OpenVAF invocation failed: {exc}",
        ) from exc
    if completed.returncode != 0 or not osdi_path.is_file():
        raise OsdiCompileError(
            "osdi.compile.failed",
            f"OpenVAF could not compile {expected_module}: "
            + (completed.stdout or "").strip()[-2000:],
        )
    osdi_bytes = osdi_path.read_bytes()
    if not 0 < len(osdi_bytes) <= MAX_OSDI_BYTES:
        raise OsdiCompileError(
            "osdi.artifact.oversize",
            f"compiled OSDI is {len(osdi_bytes)} bytes, outside 1..{MAX_OSDI_BYTES}.",
        )
    return OsdiModule(
        module_name=expected_module,
        osdi_path=osdi_path,
        source_sha256=source_sha256,
        osdi_sha256=_sha256_bytes(osdi_bytes),
        compiler=Path(binary).name,
        compiler_version=version,
    )


def _validate_ports(ports: Sequence[str]) -> tuple[str, ...]:
    if not ports:
        raise OsdiCompileError("osdi.interface.invalid", "a wrapper needs at least one port.")
    seen: set[str] = set()
    out: list[str] = []
    for port in ports:
        if not isinstance(port, str) or not _IDENT_RE.fullmatch(port):
            raise OsdiCompileError(
                "osdi.interface.invalid", f"port {port!r} is not a plain SPICE net name."
            )
        key = port.lower()
        if key in seen:
            raise OsdiCompileError("osdi.interface.invalid", f"port {port!r} is repeated.")
        seen.add(key)
        out.append(port)
    return tuple(out)


def _validate_parameters(parameters: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in parameters.items():
        if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
            raise OsdiCompileError(
                "osdi.parameter.invalid", f"parameter name {name!r} is not a plain identifier."
            )
        key = name.lower()
        if key in seen:
            raise OsdiCompileError("osdi.parameter.invalid", f"parameter {name!r} is repeated.")
        seen.add(key)
        text = _format_scalar(value)
        if not _SPICE_SCALAR_RE.fullmatch(text):
            raise OsdiCompileError(
                "osdi.parameter.invalid",
                f"parameter {name}={value!r} is not a plain SPICE scalar.",
            )
        out.append((name, text))
    return tuple(out)


def _format_scalar(value: object) -> str:
    if isinstance(value, bool):  # bool is an int subclass; never a device scalar
        raise OsdiCompileError("osdi.parameter.invalid", "a boolean is not a device scalar.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # repr() round-trips a float exactly and stays a plain decimal/exponent.
        return repr(value)
    if isinstance(value, str):
        return value.strip()
    raise OsdiCompileError("osdi.parameter.invalid", f"unsupported scalar type {type(value).__name__}.")


def osdi_preload_prelude(
    modules: Sequence[tuple[OsdiModule, Sequence[str], Mapping[str, object]]],
) -> str:
    """Emit the deck prelude that preloads OSDI modules and wraps them.

    For each ``(module, ports, parameters)`` triple, emit a ``.model`` bound to
    the OSDI module and a wrapper ``.subckt`` of the module's public name whose
    pins equal ``ports`` in order and whose header parameters (with the reviewed
    defaults) forward into the ``N`` device. A single ``.control`` block at the
    top preloads every ``.osdi`` — that block must precede the circuit so ngspice
    resolves the modules when the wrapper is parsed.
    """

    if not modules:
        raise OsdiCompileError("osdi.preload.empty", "no OSDI modules to preload.")
    seen_modules: set[str] = set()
    preload_lines = [".control"]
    wrapper_lines: list[str] = []
    for module, ports, parameters in modules:
        if module.module_name in seen_modules:
            raise OsdiCompileError(
                "osdi.preload.duplicate", f"module {module.module_name} preloaded twice."
            )
        seen_modules.add(module.module_name)
        if _PATH_UNSAFE_RE.search(str(module.osdi_path)):
            raise OsdiCompileError(
                "osdi.preload.unsafe_path",
                f"OSDI path {str(module.osdi_path)!r} contains whitespace or a "
                "control character and cannot sit on a bare pre_osdi line.",
            )
        validated_ports = _validate_ports(ports)
        validated_params = _validate_parameters(parameters)
        preload_lines.append(f"pre_osdi {module.osdi_path}")
        model_alias = f"{module.module_name}__osdi"
        header = f".subckt {module.module_name} {' '.join(validated_ports)}"
        if validated_params:
            header += " " + " ".join(f"{n}={v}" for n, v in validated_params)
        # The block's Verilog-A parameters are MODEL parameters (declared without
        # an instance attribute), so ngspice takes them on the ``.model`` line,
        # not the ``N`` device; the subckt header forwards each with ``{name}``.
        model_params = (
            " " + " ".join(f"{n}={{{n}}}" for n, _ in validated_params)
            if validated_params
            else ""
        )
        wrapper_lines.extend(
            [
                header,
                f".model {model_alias} {module.module_name}{model_params}",
                f"N1 {' '.join(validated_ports)} {model_alias}",
                f".ends {module.module_name}",
            ]
        )
    preload_lines.append(".endc")
    return "\n".join(preload_lines + wrapper_lines) + "\n"


#: A sanctioned preload's `.control` block may contain exactly one kind of line:
#: `pre_osdi <path>`. Anything else — `shell`, `system`, `source`, `run`, a bare
#: expression — would be arbitrary control the exemption must never authorize.
_PRE_OSDI_LINE_RE = re.compile(r"^pre_osdi\s+(\S+)$")


@dataclass(frozen=True)
class VerifiedOsdiPreload:
    """The result of validating an OSDI preload's structure and its modules.

    ``modules`` is the ordered ``(osdi_path, sha256)`` of every ``pre_osdi``
    target, each re-hashed from disk at validation time so the caller can retain
    them as content-bound inputs and the provenance's "compiled OSDI" claim is
    backed by the bytes that were actually about to load.
    """

    modules: tuple[tuple[str, str], ...]


def validate_osdi_preload(
    text: str,
    *,
    expected_osdi_sha256: Mapping[str, str] | None = None,
) -> VerifiedOsdiPreload:
    """Fail-closed structural gate for a behavioral-block OSDI preload.

    The OSDI preload is the ONE model source allowed to carry a ``.control``
    block into a run deck (it must, to ``pre_osdi``-load the module). That
    exemption is dangerous if authorized by opaque text, so it is authorized
    here by SHAPE instead: the single ``.control`` block may contain nothing but
    ``pre_osdi <path>`` lines, every referenced ``.osdi`` must exist and (when a
    digest map is supplied) hash to its reviewed value, and the wrapper section
    below ``.endc`` may hold only ``.subckt``/``.model``/``N…``/``.ends`` cards
    for ``bhv_<block>_v<abi>`` modules. Any ``shell``/``system``/``source``/bare
    directive — inside or outside the control block — is refused. A validated
    preload therefore cannot execute anything but the reviewed module loads,
    whoever composed the text.
    """

    if not isinstance(text, str):
        raise OsdiCompileError("osdi.preload.invalid", "preload must be text.")
    lines = text.splitlines()
    state = "pre-control"  # -> "in-control" -> "wrapper"
    seen_control = False
    modules: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        lowered = line.lower()
        if state == "pre-control":
            if lowered == ".control":
                if seen_control:
                    raise OsdiCompileError(
                        "osdi.preload.unsafe_control",
                        "an OSDI preload declares more than one .control block.",
                    )
                seen_control = True
                state = "in-control"
                continue
            raise OsdiCompileError(
                "osdi.preload.unsafe_control",
                f"an OSDI preload must open with its .control block; got {line!r}.",
            )
        if state == "in-control":
            if lowered == ".endc":
                state = "wrapper"
                continue
            if lowered == ".control":
                raise OsdiCompileError(
                    "osdi.preload.unsafe_control",
                    "an OSDI preload nests a second .control block.",
                )
            match = _PRE_OSDI_LINE_RE.match(line)
            if match is None:
                raise OsdiCompileError(
                    "osdi.preload.unsafe_control",
                    "an OSDI preload's control block may contain only "
                    f"`pre_osdi <path>` lines; refused {line!r}.",
                )
            path = match.group(1)
            if _PATH_UNSAFE_RE.search(path):
                raise OsdiCompileError(
                    "osdi.preload.unsafe_path",
                    f"OSDI path {path!r} carries whitespace or a control character.",
                )
            modules.append((path, ""))
            continue
        # state == "wrapper": ordinary SPICE subckt cards only. A second
        # .control here would re-enter control mode with arbitrary content.
        if lowered.startswith(".control") or lowered.startswith("pre_osdi"):
            raise OsdiCompileError(
                "osdi.preload.unsafe_control",
                f"an OSDI preload places control content below .endc: {line!r}.",
            )
        first = lowered.split(None, 1)[0]
        if first not in {".subckt", ".model", ".ends"} and not first.startswith("n"):
            raise OsdiCompileError(
                "osdi.preload.unsafe_control",
                "an OSDI preload's wrapper section may hold only "
                f".subckt/.model/N/.ends cards; refused {line!r}.",
            )
    if not seen_control:
        raise OsdiCompileError(
            "osdi.preload.unsafe_control", "an OSDI preload declares no .control block."
        )
    if state == "in-control":
        raise OsdiCompileError(
            "osdi.preload.unsafe_control", "an OSDI preload's .control block is unterminated."
        )
    if not modules:
        raise OsdiCompileError(
            "osdi.preload.empty", "an OSDI preload loads no module."
        )

    # Content-bind every referenced .osdi from disk, and (when given) verify it
    # against the reviewed compile digest — so a module swapped after compose is
    # refused before ngspice ever maps it.
    verified: list[tuple[str, str]] = []
    for path, _ in modules:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise OsdiCompileError(
                "osdi.preload.missing_module",
                f"the OSDI module {path!r} could not be read for content binding: {exc}.",
            )
        if len(data) > MAX_OSDI_BYTES:
            raise OsdiCompileError(
                "osdi.preload.missing_module",
                f"the OSDI module {path!r} exceeds the {MAX_OSDI_BYTES}-byte ceiling.",
            )
        actual = _sha256_bytes(data)
        if expected_osdi_sha256 is not None:
            expected = expected_osdi_sha256.get(path)
            if expected is None:
                raise OsdiCompileError(
                    "osdi.preload.unbound_module",
                    f"the OSDI module {path!r} has no reviewed digest to bind against.",
                )
            if actual != expected:
                raise OsdiCompileError(
                    "blocks.materialize.tampered",
                    f"the OSDI module {path!r} does not hash to its reviewed compile "
                    f"digest {expected}; it changed after composition, so no simulator "
                    "was launched.",
                )
        verified.append((path, actual))
    return VerifiedOsdiPreload(modules=tuple(verified))


@dataclass(frozen=True)
class OsdiComposition:
    """A compiled OSDI preload for a set of blocks, with per-block provenance."""

    library_id: str
    requested: tuple[str, ...]
    prelude_text: str
    modules: tuple[OsdiModule, ...]
    #: Per requested block, in order: public wrapper name, declared relational
    #: parameter constraints, and contract parameter defaults.
    wrappers: tuple[str, ...] = ()
    constraints: tuple[tuple[Mapping[str, object], ...], ...] = ()
    defaults: tuple[Mapping[str, float], ...] = ()

    def verify_deck(self, deck_text: str) -> None:
        """Check each block's declared relational parameter constraints
        against the effective parameters of the caller's instantiating cards
        (the same fail-closed rule the cosim path applies): without this the
        contract admits a parameterization the realization cannot honor and
        the simulator silently returns a latency other than the declared one.
        """

        from .cosim_compile import (
            CosimCompileError,
            _folded_statements,
            instantiation_parameter_maps,
            verify_parameter_constraints,
        )

        # Wrapper-only use: the compiled OSDI module types and the prelude's
        # model aliases must never be bound or instantiated by the caller
        # directly -- a caller .model card or a direct N reference would
        # bypass every per-instance constraint check below (reproduced in
        # review: td<=tedge/2 accepted through a direct .model+N pair).
        module_types = {m.module_name.lower() for m in self.modules}
        aliases = {f"{m.module_name.lower()}__osdi" for m in self.modules}
        # _folded_statements strips inline comments per PHYSICAL line before
        # stitching '+' continuations, mirroring ngspice's parse order — so a
        # protected name cannot hide behind a comment or a continuation.
        for _number, statement in _folded_statements(deck_text):
            fields = statement.split()
            if not fields:
                continue
            lead = fields[0].lower()
            if lead == ".model" and len(fields) >= 3:
                if fields[2].split("(")[0].lower() in module_types:
                    raise OsdiCompileError(
                        "osdi.instantiation.direct",
                        f"the caller deck binds the compiled OSDI module "
                        f"{fields[2]!r} with its own .model card "
                        f"({fields[1]!r}); composed blocks may only be "
                        "instantiated through their public wrapper subckt, "
                        "where the declared parameter constraints are "
                        "checked.",
                    )
            elif lead.startswith("n") and any(
                f.lower() in aliases or f.lower() in module_types
                for f in fields[1:]
            ):
                raise OsdiCompileError(
                    "osdi.instantiation.direct",
                    f"the caller deck instantiates a composition-owned OSDI "
                    f"model directly ({statement.split()[0]}); composed "
                    "blocks may only be instantiated through their public "
                    "wrapper subckt.",
                )

        for wrapper, constraints, defaults in zip(
            self.wrappers, self.constraints, self.defaults
        ):
            if not constraints:
                continue
            try:
                # EVERY instantiating card is checked individually; an
                # uninstantiated block is checked at its contract defaults.
                maps = instantiation_parameter_maps(deck_text, wrapper, defaults)
                for effective in maps or [dict(defaults)]:
                    verify_parameter_constraints(
                        constraints, effective, block_id=wrapper
                    )
            except CosimCompileError as exc:
                raise OsdiCompileError(
                    exc.code.replace("cosim.", "osdi.", 1), exc.message
                ) from exc


def _block_interface(block: object, block_id: str) -> tuple[list[str], dict[str, object]]:
    contract = getattr(block, "contract", None)
    if not isinstance(contract, Mapping):
        raise OsdiCompileError("osdi.block.invalid", f"block {block_id!r} has no contract.")
    ports = [p["name"] for p in contract.get("ports", []) if isinstance(p, Mapping) and "name" in p]
    parameters = {
        p["name"]: p.get("default")
        for p in contract.get("parameters", [])
        if isinstance(p, Mapping) and "name" in p and "default" in p
    }
    return ports, parameters


def compose_blocks_osdi(
    library: object,
    block_ids: Sequence[str],
    work_dir: Path,
    *,
    openvaf_bin: str | None = None,
) -> OsdiComposition:
    """Compile the verilog-a backend of each requested block to OSDI and emit the
    preload prelude (drop-in for the ngspice-native composition).

    The block's declared ports (in order) and parameter defaults come from its
    reviewed contract, so the generated wrapper's interface is exactly the
    contract's — the same interface the ngspice-native backend exposes. A block
    without a verilog-a backend is refused: OSDI is not a silent fallback.
    """

    blocks = getattr(library, "blocks", None)
    if not isinstance(blocks, Mapping):
        raise OsdiCompileError("osdi.library.invalid", "library exposes no blocks mapping.")
    requested = tuple(block_ids)
    if not requested:
        raise OsdiCompileError("osdi.compose.empty", "no blocks requested.")
    seen: set[str] = set()
    validated: list[tuple[str, object]] = []
    for block_id in requested:
        if block_id in seen:
            raise OsdiCompileError(
                "osdi.compose.duplicate", f"block {block_id!r} requested twice."
            )
        seen.add(block_id)
        block = blocks.get(block_id)
        if block is None:
            raise OsdiCompileError("osdi.compose.unknown", f"block {block_id!r} is not in the library.")
        if getattr(block, "veriloga", None) is None:
            raise OsdiCompileError(
                "osdi.compose.no_veriloga",
                f"block {block_id!r} has no verilog-a backend to compile to OSDI.",
            )
        validated.append((block_id, block))
    triples: list[tuple[OsdiModule, Sequence[str], Mapping[str, object]]] = []
    modules: list[OsdiModule] = []
    wrappers: list[str] = []
    constraints: list[tuple[Mapping[str, object], ...]] = []
    defaults: list[Mapping[str, float]] = []
    for block_id, block in validated:
        veriloga = block.veriloga
        module = compile_verilog_a(
            veriloga.source_text, veriloga.wrapper, work_dir, openvaf_bin=openvaf_bin
        )
        # The compiled bytes must be exactly the reviewed, digest-bound source.
        if module.source_sha256 != veriloga.source_sha256:
            raise OsdiCompileError(
                "osdi.compose.tampered",
                f"block {block_id!r} verilog-a source digest changed before compile.",
            )
        ports, parameters = _block_interface(block, block_id)
        triples.append((module, ports, parameters))
        modules.append(module)
        wrappers.append(veriloga.wrapper)
        constraints.append(tuple(getattr(veriloga, "parameter_constraints", ()) or ()))
        defaults.append(
            {str(k): float(v) for k, v in parameters.items() if isinstance(v, (int, float))}
        )
    prelude = osdi_preload_prelude(triples)
    return OsdiComposition(
        library_id=str(getattr(library, "library_id", "")),
        requested=requested,
        prelude_text=prelude,
        modules=tuple(modules),
        wrappers=tuple(wrappers),
        constraints=tuple(constraints),
        defaults=tuple(defaults),
    )
