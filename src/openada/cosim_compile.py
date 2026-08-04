"""Compile a reviewed digital Verilog core to an ngspice ``d_cosim`` shared
object and compose the mixed-signal block backend around it.

This is the Step-3 counterpart to :mod:`openada.osdi_compile`. Step 2 covers
the continuous-analog blocks (Verilog-A compiled by OpenVAF to OSDI); the
event/clocked blocks use ``transition()`` and ``@(...)`` constructs OpenVAF
rejects, so their compiled path is mixed-signal instead: a reviewed *digital*
Verilog core is compiled with Verilator into a shared object exporting the
``Cosim_setup`` entry the XSPICE ``d_cosim`` code model loads, and a reviewed
analog wrapper subcircuit (validated by the block-library SPICE grammar)
carries the bridges, declared latencies, and output stage around that core.

The split is deliberate and closed:

* All *parameterizable* behavior (thresholds, latencies, edge times, output
  levels) lives in the analog wrapper, where ngspice parameter expansion
  works. The compiled core is parameter-free sampled/combinational logic --
  a SPICE instance parameter can never reach a compiled shared object.
* The composer, never the block author, emits the one ``.model <name>
  d_cosim(...)`` line binding the wrapper to the compiled object, so no
  reviewed source ever carries a file path and the path that is emitted is
  machine-generated, character-validated, and digest-bound.

Every artifact is digest-bound and fail-closed, mirroring Steps 1-2: the
Verilog source bytes, the produced ``.so`` bytes, the Verilator version, and
the ngspice cosim shim sources that were linked in are all recorded so a run
can attest exactly what logic it executed.

ngspice constraints this module encodes (verified against ngspice-45.2 and
the prod image's ngspice-46, 2026-08-03):

* ``d_cosim`` ships in the standard ``digital.cm`` code-model library, which
  ngspice loads at startup -- no ``.control`` block or ``codemodel`` line is
  needed, so the composition stays a self-contained model prelude.
* The shipped Icarus ``ivlng.so`` shim is unusable as built (``Cosim_setup``
  is a local, non-exported symbol in both the Debian and IIC builds), so the
  supported path is the Verilator one: the ``vlnggen`` recipe is driven
  directly (the image's own ``vlnggen`` script mangles ``--Mdir`` and cannot
  run), building against the trusted ngspice ``scripts/src`` shim sources.
* The Verilator shim binds the a-device port vectors to the module ports in
  the order Verilator emits them in the generated header, which is NOT
  necessarily the module's declaration order. The compile therefore parses
  the generated header and refuses any disagreement with the block's
  declared port order, so a reordering can never silently swap clock and
  data again.
* The shim is 2-state: a digital UNKNOWN input holds its last known value
  inside the core. Wrappers must therefore resolve declared ambiguity bands
  in the analog domain (a crisp ``adc_bridge``) before the core.
* **EXACTLY ONE ``d_cosim`` instance may exist in a deck.** This is an
  ngspice-side limit, NOT a shim limit -- OpenADA compiles its own corrected
  shim (see ``runtime/cosim/openada_verilator_shim.cpp``) and the restriction
  still stands. Two independent ngspice defects force it, both reproduced on
  ngspice-45.2 and ngspice-46 with a minimal hand-written shim that shares no
  state at all (see ``UPSTREAM-NGSPICE-DCOSIM-SHIM-UAF.md``):

  1. ``cm_irreversible()`` (src/xspice/cm/cm.c) omits a ``break`` after its
     duplicate-place warning, so a second instance requesting the same
     ``irreversible`` place overwrites the first in ``evt->info.hybrids`` and
     leaves an uninitialized slot that ``EVTcall_hybrids()`` then
     dereferences -- SIGSEGV. ``irreversible`` is a MODEL parameter, so two
     instances of one ``.model`` card cannot even be given distinct places.
  2. Worse, when the crash IS avoided (distinct models with distinct places),
     two co-existing instances silently produce results that depend on
     netlist card order: a buck converter reads 1.885890 V when instantiated
     first and 1.250168e-07 V when instantiated second, reproducibly, with no
     diagnostic.

  A wrong number with no warning is the worst outcome available here, so this
  module refuses multi-instance compositions and decks outright rather than
  emitting a deck whose correctness depends on card order.
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
from .provider_runtime import _installed_data_path

#: Ceiling on a compiled cosim shared object. The Verilator runtime plus a
#: reviewed behavioral core links to ~200 KiB; a huge artifact means the
#: toolchain emitted something unexpected and it is refused, never loaded.
MAX_SO_BYTES = 16 * 1024 * 1024
#: Ceiling on a digital core source. Reviewed cores are a few dozen lines.
MAX_CORE_BYTES = 65_536
#: Ceiling on ports per direction: reviewed cores are tiny.
MAX_PORTS = 32
#: Ceiling on the composed mixed-signal prelude, mirroring the native
#: composition bound in block_library.
MAX_COMPOSITION_BYTES = 2_097_152

#: The compiled core module name promised by the block contract.
_CORE_MODULE_RE = re.compile(r"^bhv_[a-z0-9_]+_v[0-9]+_core$")
#: The generated d_cosim model name (reserved to the composer).
_MODEL_NAME_RE = re.compile(r"^bhv_[a-z0-9_]+_cosim$")
#: A core port name: plain lower-case identifier, matching both the Verilog
#: subset the cores use and the SPICE net grammar of the wrapper.
_PORT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: The one path alphabet allowed to reach a generated deck line. The compiled
#: object path sits inside ``simulation="<path>"`` on a ``.model`` card;
#: rather than reason about ngspice's quoting under every compatibility mode,
#: anything outside this closed set is refused.
_PATH_SAFE_RE = re.compile(r"^[A-Za-z0-9/._+-]+$")
#: One Verilator-emitted top-class port macro, e.g. ``VL_IN8(&clk,0,0);``.
_VL_PORT_RE = re.compile(
    r"^\s*VL_(IN|OUT|INOUT)(8|16|32|64|W)?\(&([A-Za-z_][A-Za-z0-9_$]*)"
    r"\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*\d+\s*)?\)\s*;\s*$"
)

#: The fixed event delay written onto every generated d_cosim model card.
#: Zero is refused by the code model ("output scheduled with impossible
#: delay"), so the minimum representable 1 ps is pinned and documented; the
#: wrapper's calibrated delay stage subtracts it inside the declared latency
#: arithmetic.
COSIM_MODEL_DELAY = "1p"


class CosimCompileError(Exception):
    """A cosim compile/compose refusal with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CosimModule:
    """A compiled cosim core and the provenance binding it to its source."""

    core_module: str
    so_path: Path
    source_sha256: str
    so_sha256: str
    verilator: str
    verilator_version: str
    shim_sha256: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(
    argv: Sequence[str], *, cwd: Path, timeout: float, what: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CosimCompileError(
            "cosim.toolchain.failed", f"{what} invocation failed: {exc}"
        ) from exc


def resolve_verilator(*, override: str | None = None) -> tuple[str, str]:
    """Return ``(binary_path, version_string)`` for an available Verilator.

    Probes the binaries the discovery layer declares for the ``verilator``
    tool. Refuses closed if none resolve, so the caller never shells out to a
    missing compiler.
    """

    candidates: Sequence[str]
    if override is not None:
        candidates = (override,)
    else:
        candidates = TOOL_SPECS["verilator"].binaries
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
            raise CosimCompileError(
                "cosim.compiler.unusable",
                f"Verilator binary {resolved!r} could not be probed: {exc}",
            ) from exc
        version = (completed.stdout or "").strip().splitlines()
        return resolved, (version[0].strip() if version else "unknown")
    raise CosimCompileError(
        "cosim.compiler.unavailable",
        "no Verilator compiler is available; the cosim backend needs one of "
        + ", ".join(repr(b) for b in candidates)
        + " on PATH.",
    )


#: The reviewed shim translation units OpenADA compiles into every cosim
#: object. These are OUR sources (runtime/cosim/), not ngspice's: the shipped
#: ngspice shim destroys its VerilatedContext at the end of Cosim_setup while
#: the Verilated model keeps a raw pointer to it, and keeps the
#: edge-detection memory in file-scope statics -- a use-after-free at one
#: instance and a segfault at two. See runtime/cosim/openada_verilator_shim.cpp
#: and UPSTREAM-NGSPICE-DCOSIM-SHIM-UAF.md.
SHIM_SOURCES = ("openada_verilator_main.cpp", "openada_verilator_shim.cpp")
#: The ABI headers still belong to the trusted ngspice installation: ngspice
#: owns the d_cosim interface, OpenADA owns only the logic on our side of it.
_ABI_HEADER = Path("ngspice") / "cmtypes.h"
_COSIM_HEADER = Path("ngspice") / "cosim.h"


def resolve_cosim_abi_dir(*, ngspice_bin: str | None = None) -> Path:
    """Locate the trusted ngspice d_cosim ABI headers.

    Both the Debian host layout (``/usr/bin/ngspice`` ->
    ``/usr/share/ngspice/scripts/src``) and the IIC image layout
    (``/foss/tools/ngspice/bin/ngspice`` ->
    ``/foss/tools/ngspice/share/ngspice/scripts/src``) follow
    ``<prefix>/share/ngspice/scripts/src`` for the resolved binary's prefix.
    Only the headers are taken from there; the shim logic is OpenADA's.
    """

    resolved = ngspice_bin or shutil.which("ngspice")
    if resolved is None:
        raise CosimCompileError(
            "cosim.shim.unavailable",
            "no ngspice binary is available to locate the d_cosim ABI headers.",
        )
    # A named simulator that does not exist must not silently fall back to
    # some OTHER installation's headers: the object would be compiled against
    # one ngspice's d_cosim ABI and loaded by another.
    if not Path(resolved).is_file():
        raise CosimCompileError(
            "cosim.shim.unavailable",
            f"the named ngspice binary {resolved!r} does not exist, so its "
            "d_cosim ABI headers cannot be located.",
        )
    try:
        prefix = Path(resolved).resolve().parent.parent
    except OSError as exc:
        raise CosimCompileError(
            "cosim.shim.unavailable",
            f"ngspice binary {resolved!r} could not be resolved: {exc}",
        ) from exc
    # Same-prefix only.  The standard ``/usr/bin`` -> ``/usr/share`` and
    # ``/usr/local/bin`` -> ``/usr/local/share`` layouts already fall out of
    # the resolved binary's own prefix, so the historical ``/usr`` and
    # ``/usr/local`` fallbacks bought nothing but the risk of compiling a
    # named custom-prefix ngspice against SOME OTHER installation's d_cosim
    # ABI headers.  A named ngspice whose own prefix lacks the headers is
    # refused rather than compiled against a foreign ABI.
    candidate = prefix / "share" / "ngspice" / "scripts" / "src"
    if (candidate / _ABI_HEADER).is_file() and (candidate / _COSIM_HEADER).is_file():
        if not _PATH_SAFE_RE.fullmatch(str(candidate)):
            raise CosimCompileError(
                "cosim.shim.unsafe_path",
                f"ngspice header directory {str(candidate)!r} contains "
                "characters that cannot ride on a compiler command line "
                "unquoted.",
            )
        return candidate
    raise CosimCompileError(
        "cosim.shim.unavailable",
        "the ngspice d_cosim ABI headers (ngspice/cmtypes.h and "
        f"ngspice/cosim.h) were not found under the resolved ngspice "
        f"installation's own prefix ({candidate}); this ngspice cannot host "
        "compiled cosim cores. A named ngspice is never compiled against a "
        "different installation's ABI headers.",
    )


def resolve_cosim_shim_dir(*, ngspice_bin: str | None = None) -> tuple[Path, str]:
    """Return OpenADA's reviewed shim source directory and its content digest.

    The digest covers every byte of every shim translation unit, so the
    provenance of a run records exactly which co-simulation runtime scheduled
    its events -- the shim IS the runtime, not a build detail. ``ngspice_bin``
    is accepted (and the ABI headers probed) so an ngspice that cannot host
    cosim at all still fails closed here rather than at link time.
    """

    resolve_cosim_abi_dir(ngspice_bin=ngspice_bin)
    try:
        directory = _installed_data_path("runtime/cosim", SHIM_SOURCES[0]).parent
    except Exception as exc:
        # A missing shim must be a typed refusal at this boundary, never a raw
        # provider-runtime traceback -- and never a silent fall back to
        # ngspice's shipped shim, which is the defective one.
        raise CosimCompileError(
            "cosim.shim.unavailable",
            "OpenADA's reviewed d_cosim shim sources are not installed: "
            f"{exc}",
        ) from exc
    if not _PATH_SAFE_RE.fullmatch(str(directory)):
        raise CosimCompileError(
            "cosim.shim.unsafe_path",
            f"shim directory {str(directory)!r} contains characters that "
            "cannot ride on a compiler command line unquoted.",
        )
    hasher = hashlib.sha256()
    for name in SHIM_SOURCES:
        path = directory / name
        if not path.is_file():
            raise CosimCompileError(
                "cosim.shim.unavailable",
                f"the reviewed shim source {name} is missing from {directory}.",
            )
        data = path.read_bytes()
        if not 0 < len(data) <= MAX_CORE_BYTES:
            raise CosimCompileError(
                "cosim.shim.oversize",
                f"the reviewed shim source {name} is {len(data)} bytes, "
                f"outside 1..{MAX_CORE_BYTES}.",
            )
        hasher.update(name.encode())
        hasher.update(b"\0")
        hasher.update(data)
    return directory, hasher.hexdigest()




def _validate_port_list(ports: Sequence[str], *, direction: str) -> tuple[str, ...]:
    if not ports:
        raise CosimCompileError(
            "cosim.interface.invalid", f"a cosim core needs at least one {direction} port."
        )
    if len(ports) > MAX_PORTS:
        raise CosimCompileError(
            "cosim.interface.invalid",
            f"{len(ports)} {direction} ports exceed the bound of {MAX_PORTS}.",
        )
    seen: set[str] = set()
    for port in ports:
        if not isinstance(port, str) or not _PORT_RE.fullmatch(port):
            raise CosimCompileError(
                "cosim.interface.invalid",
                f"{direction} port {port!r} is not a plain lower-case identifier.",
            )
        if port in seen:
            raise CosimCompileError(
                "cosim.interface.invalid", f"{direction} port {port!r} is repeated."
            )
        seen.add(port)
    return tuple(ports)


def _parse_verilated_ports(header_path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the (inputs, outputs) port order the Verilator shim will bind.

    The shim walks the generated ``inputs.h``/``outputs.h`` in the order the
    port macros appear in the Verilated top class header, so THAT order -- not
    the module's declaration order -- is the a-device port order. Only scalar
    single-bit ports are accepted in this revision: the reviewed wrappers
    bridge each bit explicitly, and a silently-split bus would change pin
    meaning without changing pin count.
    """

    try:
        text = header_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CosimCompileError(
            "cosim.compile.failed", f"Verilated header {header_path} unreadable: {exc}"
        ) from exc
    inputs: list[str] = []
    outputs: list[str] = []
    for line in text.splitlines():
        match = _VL_PORT_RE.match(line)
        if match is None:
            continue
        direction, width, name, msb, lsb = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4),
            match.group(5),
        )
        if direction == "INOUT":
            raise CosimCompileError(
                "cosim.interface.unsupported",
                f"core port {name!r} is an inout; the analog wrapper bridges "
                "directed ports only.",
            )
        if width != "8" or msb != "0" or lsb != "0":
            raise CosimCompileError(
                "cosim.interface.unsupported",
                f"core port {name!r} is not a scalar single-bit port; buses "
                "must be spelled as explicit scalar ports so the wrapper "
                "bridges every bit by name.",
            )
        if not _PORT_RE.fullmatch(name):
            raise CosimCompileError(
                "cosim.interface.invalid",
                f"Verilated port name {name!r} is outside the reviewed port grammar.",
            )
        (inputs if direction == "IN" else outputs).append(name)
    if not inputs or not outputs:
        raise CosimCompileError(
            "cosim.interface.invalid",
            f"the Verilated header declares {len(inputs)} inputs and "
            f"{len(outputs)} outputs; a cosim core needs at least one of each.",
        )
    return tuple(inputs), tuple(outputs)


def _write_port_headers(
    obj_dir: Path, inputs: Sequence[str], outputs: Sequence[str]
) -> None:
    """Write the ``VL_DATA`` port tables the ngspice verilator shim compiles in.

    This replicates what the ngspice ``vlnggen`` control script generates by
    sed-like text surgery, from the already-validated parsed port lists (all
    scalar, so every entry is ``VL_DATA(8,<name>,0,0)``).
    """

    banner = "/* Generated by openada.cosim_compile: do not edit. */\n"
    (obj_dir / "inputs.h").write_text(
        banner + "".join(f"VL_DATA(8,{name},0,0)\n" for name in inputs),
        encoding="utf-8",
    )
    (obj_dir / "outputs.h").write_text(
        banner + "".join(f"VL_DATA(8,{name},0,0)\n" for name in outputs),
        encoding="utf-8",
    )
    (obj_dir / "inouts.h").write_text(banner, encoding="utf-8")


def compile_verilog_digital(
    source_text: str,
    core_module: str,
    declared_inputs: Sequence[str],
    declared_outputs: Sequence[str],
    work_dir: Path,
    *,
    verilator_bin: str | None = None,
    ngspice_bin: str | None = None,
) -> CosimModule:
    """Compile one reviewed digital Verilog core to a digest-bound ``.so``.

    ``core_module`` is the module name the block contract promises;
    ``declared_inputs``/``declared_outputs`` are the contract's ordered port
    lists, cross-checked against the order Verilator actually emitted so the
    a-device binding in the reviewed wrapper can never silently rotate.
    """

    if not _CORE_MODULE_RE.fullmatch(core_module):
        raise CosimCompileError(
            "cosim.module.invalid",
            f"core module {core_module!r} is not a bhv_<block>_v<abi>_core name.",
        )
    if not isinstance(source_text, str):
        raise CosimCompileError(
            "cosim.source.invalid",
            f"core source must be text, not {type(source_text).__name__}.",
        )
    encoded = source_text.encode("utf-8")
    if not 0 < len(encoded) <= MAX_CORE_BYTES:
        raise CosimCompileError(
            "cosim.source.oversize",
            f"core source is {len(encoded)} bytes, outside 1..{MAX_CORE_BYTES}.",
        )
    declared_in = _validate_port_list(declared_inputs, direction="input")
    declared_out = _validate_port_list(declared_outputs, direction="output")
    # The produced .so path is emitted inside simulation="<path>" on a .model
    # card; a work_dir outside the closed path alphabet is refused up front.
    if not _PATH_SAFE_RE.fullmatch(str(work_dir)):
        raise CosimCompileError(
            "cosim.workdir.unsafe",
            f"work_dir {str(work_dir)!r} contains characters outside the "
            "closed path alphabet allowed on a generated d_cosim model card.",
        )
    source_sha256 = _sha256_bytes(encoded)
    verilator, verilator_version = resolve_verilator(override=verilator_bin)
    abi_dir = resolve_cosim_abi_dir(ngspice_bin=ngspice_bin)
    shim_dir, shim_sha256 = resolve_cosim_shim_dir(ngspice_bin=ngspice_bin)

    work_dir.mkdir(parents=True, exist_ok=True)
    # Compile in an isolated scratch dir so a hostile source cannot overwrite
    # a sibling; only the named source, objects, and output live here.
    scratch = Path(tempfile.mkdtemp(prefix="cosim-", dir=work_dir))
    v_path = scratch / f"{core_module}.v"
    obj_dir = scratch / "obj"
    so_path = scratch / f"{core_module}.so"
    v_path.write_bytes(encoded)

    # Pass 1: header-only verilation, to learn the port binding order.
    first = _run(
        [
            verilator,
            "--Mdir",
            str(obj_dir),
            "--prefix",
            "Vlng",
            # Bind the compiled top to the module the contract PROMISES: without
            # it Verilator picks a top by inference, so a source whose only
            # module is named something else would compile and be recorded
            # under the contract's module name.
            "--top-module",
            core_module,
            "--CFLAGS",
            "-fpic",
            "--cc",
            str(v_path),
        ],
        cwd=scratch,
        timeout=180,
        what="Verilator",
    )
    header = obj_dir / "Vlng.h"
    if first.returncode != 0 or not header.is_file():
        raise CosimCompileError(
            "cosim.compile.failed",
            f"Verilator could not verilate {core_module}: "
            + (first.stdout or "").strip()[-2000:],
        )
    inputs, outputs = _parse_verilated_ports(header)
    if inputs != declared_in or outputs != declared_out:
        raise CosimCompileError(
            "cosim.interface.mismatch",
            f"{core_module}: the compiled port binding order (inputs "
            f"{list(inputs)}, outputs {list(outputs)}) does not equal the "
            f"contract's declared order (inputs {list(declared_in)}, outputs "
            f"{list(declared_out)}); the a-device pins in the reviewed "
            "wrapper would bind the wrong signals, so the compile is refused.",
        )
    _write_port_headers(obj_dir, inputs, outputs)

    # Pass 2: build the shim + core objects (verilator drives make), then link
    # the shared object exactly as the ngspice vlnggen recipe does.
    second = _run(
        [
            verilator,
            "--Mdir",
            str(obj_dir),
            "--prefix",
            "Vlng",
            "--top-module",
            core_module,
            "--CFLAGS",
            f"-I{abi_dir}",
            "--CFLAGS",
            "-fpic",
            "--cc",
            "--build",
            "--exe",
            *(str(shim_dir / name) for name in SHIM_SOURCES),
            str(v_path),
        ],
        cwd=scratch,
        timeout=900,
        what="Verilator",
    )
    if second.returncode != 0:
        raise CosimCompileError(
            "cosim.compile.failed",
            f"Verilator could not build {core_module}: "
            + (second.stdout or "").strip()[-2000:],
        )
    link_objects = [
        obj_dir / "openada_verilator_shim.o",
        obj_dir / "verilated.o",
        obj_dir / "verilated_threads.o",
        obj_dir / "Vlng__ALL.a",
    ]
    for path in link_objects:
        if not path.is_file():
            raise CosimCompileError(
                "cosim.compile.failed",
                f"expected build object {path.name} was not produced.",
            )
    linked = _run(
        ["g++", "--shared", *map(str, link_objects), "-pthread", "-lpthread", "-o", str(so_path)],
        cwd=scratch,
        timeout=300,
        what="g++",
    )
    if linked.returncode != 0 or not so_path.is_file():
        raise CosimCompileError(
            "cosim.compile.failed",
            f"linking {core_module}.so failed: " + (linked.stdout or "").strip()[-2000:],
        )
    so_bytes = so_path.read_bytes()
    if not 0 < len(so_bytes) <= MAX_SO_BYTES:
        raise CosimCompileError(
            "cosim.artifact.oversize",
            f"compiled object is {len(so_bytes)} bytes, outside 1..{MAX_SO_BYTES}.",
        )
    if not _PATH_SAFE_RE.fullmatch(str(so_path)):
        raise CosimCompileError(
            "cosim.artifact.unsafe_path",
            f"compiled object path {str(so_path)!r} is outside the closed "
            "path alphabet allowed on a generated d_cosim model card.",
        )
    return CosimModule(
        core_module=core_module,
        so_path=so_path,
        source_sha256=source_sha256,
        so_sha256=_sha256_bytes(so_bytes),
        verilator=Path(verilator).name,
        verilator_version=verilator_version,
        shim_sha256=shim_sha256,
        inputs=inputs,
        outputs=outputs,
    )


def cosim_model_line(module: CosimModule, model_name: str) -> str:
    """Emit the one generated ``.model`` card binding wrapper to compiled core."""

    if not _MODEL_NAME_RE.fullmatch(model_name):
        raise CosimCompileError(
            "cosim.model.invalid",
            f"model name {model_name!r} is not a bhv_<block>_cosim binding name.",
        )
    path_text = str(module.so_path)
    if not _PATH_SAFE_RE.fullmatch(path_text) or not module.so_path.is_absolute():
        raise CosimCompileError(
            "cosim.model.unsafe_path",
            f"compiled object path {path_text!r} cannot ride on a generated "
            "d_cosim model card.",
        )
    return (
        f".model {model_name} d_cosim(simulation=\"{path_text}\" "
        f"delay={COSIM_MODEL_DELAY})"
    )


@dataclass(frozen=True)
class CosimComposition:
    """A compiled mixed-signal composition with per-block provenance."""

    library_id: str
    library_version: str
    library_digest: str
    requested: tuple[str, ...]
    text: str
    text_sha256: str
    modules: tuple[CosimModule, ...]
    model_names: tuple[str, ...]
    #: Public wrapper name per requested block, in the same order.
    wrappers: tuple[str, ...] = ()
    #: Declared relational parameter constraints per block, same order.
    constraints: tuple[tuple[Mapping[str, object], ...], ...] = ()
    #: Contract parameter defaults per block, same order.
    defaults: tuple[Mapping[str, float], ...] = ()

    def verify_deck(self, deck_text: str) -> None:
        """Every deck-level rule this composition's realization requires.

        One place, so the CLI, the operation boundary, and any programmatic
        caller enforce the SAME rules: exactly one d_cosim instance, and each
        backend's declared relational parameter constraints checked against
        the effective parameters of the instantiating card.
        """

        verify_single_instantiation(deck_text, self.wrappers, self.model_names)
        for wrapper, constraints, defaults in zip(
            self.wrappers, self.constraints, self.defaults
        ):
            if not constraints:
                continue
            for effective in instantiation_parameter_maps(
                deck_text, wrapper, defaults
            ) or [dict(defaults)]:
                verify_parameter_constraints(
                    constraints, effective, block_id=wrapper
                )

    def executable_allowance(self) -> dict[str, tuple[str, str]]:
        """The generated binding cards as ``{model name: (object path, sha256)}``.

        This is the exact shape the simulate operation's sanctioned
        executable-model allowance takes: naming a card is never enough, the
        caller must also declare WHICH object it binds and what those bytes
        hash to, and the operation re-verifies both before launching.
        """

        return {
            name: (str(module.so_path), module.so_sha256)
            for name, module in zip(self.model_names, self.modules)
        }

    def verify_objects(self) -> None:
        """Re-verify every compiled object against its recorded digest.

        The composition TEXT is digest-pinned inside the simulate operation,
        but the text only carries the object PATHS; the bytes ngspice will
        dlopen live on disk. Callers re-verify immediately before launching
        the simulation so a compiled object replaced after compose is a typed
        pre-launch refusal, narrowing the swap window to the same
        verify-to-launch interval the model-file tamper check accepts.
        """

        for module in self.modules:
            try:
                data = module.so_path.read_bytes()
            except OSError as exc:
                raise CosimCompileError(
                    "cosim.materialize.tampered",
                    f"compiled object {module.so_path} could not be re-read "
                    f"before launch: {exc}",
                ) from exc
            if _sha256_bytes(data) != module.so_sha256:
                raise CosimCompileError(
                    "cosim.materialize.tampered",
                    f"compiled object {module.so_path} no longer hashes to the "
                    "digest recorded at compile time; the file changed after "
                    "composition, so no simulator is launched.",
                )

    def record(self) -> dict[str, object]:
        """Provenance record for retained evidence and result extensions."""

        return {
            "kind": "behavioral-block-cosim-composition",
            "library_id": self.library_id,
            "library_version": self.library_version,
            "library_digest": self.library_digest,
            "requested_blocks": list(self.requested),
            "composition_sha256": self.text_sha256,
            "generated_models": list(self.model_names),
            "cores": [
                {
                    "core_module": module.core_module,
                    "source_sha256": module.source_sha256,
                    "object_sha256": module.so_sha256,
                    "object_path": str(module.so_path),
                    "compiler": module.verilator,
                    "compiler_version": module.verilator_version,
                    "shim_sha256": module.shim_sha256,
                }
                for module in self.modules
            ],
        }


def compose_blocks_cosim(
    library: object,
    block_ids: Sequence[str],
    work_dir: Path,
    *,
    verilator_bin: str | None = None,
    ngspice_bin: str | None = None,
) -> CosimComposition:
    """Compile the cosim backend of each requested block and compose the
    mixed-signal model prelude (drop-in for the ngspice-native composition).

    Each block contributes its reviewed wrapper subcircuit text verbatim plus
    one generated ``.model ... d_cosim`` binding card. A block without a
    cosim backend is refused: the mixed-signal path is never a silent
    fallback. Cross-block dependencies are refused in this revision -- none
    of the reviewed event blocks declare any, and a dependency closure that
    mixes backends is a scope decision the contract has not made.
    """

    blocks = getattr(library, "blocks", None)
    if not isinstance(blocks, Mapping):
        raise CosimCompileError("cosim.library.invalid", "library exposes no blocks mapping.")
    if not block_ids:
        raise CosimCompileError("cosim.compose.empty", "no blocks requested.")
    requested = tuple(sorted(dict.fromkeys(block_ids)))
    if len(requested) != len(tuple(block_ids)):
        raise CosimCompileError(
            "cosim.compose.duplicate", "a block was requested more than once."
        )
    # Each reviewed cosim wrapper carries exactly one d_cosim instance, and the
    # shipped shim cannot host two (see the module docstring: dangling
    # VerilatedContext + shared static state, ngspice segfaults). A multi-block
    # cosim composition is therefore refused up front rather than compiled into
    # a deck that crashes the simulator.
    if len(requested) > 1:
        raise CosimCompileError(
            "cosim.compose.multi_instance",
            "the mixed-signal backend carries ONE block per deck: ngspice's "
            "d_cosim path crashes on a duplicate irreversible place and, when "
            "that is avoided, returns results that depend on netlist card "
            "order with no diagnostic (see "
            "UPSTREAM-NGSPICE-DCOSIM-SHIM-UAF.md). Requested: "
            f"{list(requested)}.",
        )

    header_symbols: dict[str, str] = {}
    modules: list[CosimModule] = []
    model_names: list[str] = []
    wrapper_texts: list[tuple[str, object]] = []
    for block_id in requested:
        block = blocks.get(block_id)
        if block is None:
            raise CosimCompileError(
                "cosim.compose.unknown", f"block {block_id!r} is not in the library."
            )
        if tuple(getattr(block, "depends", ()) or ()):
            raise CosimCompileError(
                "cosim.compose.depends_unsupported",
                f"block {block_id!r} declares dependencies; the cosim "
                "composition of this revision carries single blocks only.",
            )
        cosim = getattr(block, "cosim", None)
        if cosim is None:
            raise CosimCompileError(
                "cosim.compose.no_backend",
                f"block {block_id!r} has no xspice-cosim backend to compile.",
            )
        module = compile_verilog_digital(
            cosim.core_source_text,
            cosim.core_module,
            cosim.core_inputs,
            cosim.core_outputs,
            work_dir,
            verilator_bin=verilator_bin,
            ngspice_bin=ngspice_bin,
        )
        # The compiled bytes must be exactly the reviewed, digest-bound source.
        if module.source_sha256 != cosim.core_source_sha256:
            raise CosimCompileError(
                "cosim.compose.tampered",
                f"block {block_id!r} core source digest changed before compile.",
            )
        model_name = f"bhv_{block_id}_cosim"
        for line_symbol in _wrapper_symbols(cosim.source_text):
            owner = header_symbols.get(line_symbol)
            if owner is not None and owner != block_id:
                raise CosimCompileError(
                    "cosim.compose.symbol_collision",
                    f"simulator symbol {line_symbol!r} is defined by both "
                    f"{owner!r} and {block_id!r}.",
                )
            header_symbols[line_symbol] = block_id
        if model_name in header_symbols:
            raise CosimCompileError(
                "cosim.compose.symbol_collision",
                f"the generated binding name {model_name!r} is already defined "
                "by a wrapper source.",
            )
        header_symbols[model_name] = block_id
        modules.append(module)
        model_names.append(model_name)
        wrapper_texts.append((block_id, cosim))

    lines: list[str] = []
    lines.append("* openada behavioral-block cosim composition")
    library_id = str(getattr(library, "library_id", ""))
    library_version = str(getattr(library, "library_version", ""))
    library_digest = str(getattr(library, "library_digest", ""))
    lines.append(
        f"* library {library_id}@{library_version} digest {library_digest}"
    )
    for (block_id, cosim), module in zip(wrapper_texts, modules):
        lines.append(
            f"* block {block_id} wrapper sha256 {cosim.source_sha256} "
            f"core sha256 {module.source_sha256} object sha256 {module.so_sha256}"
        )
    lines.append("")
    for (block_id, cosim), module, model_name in zip(
        wrapper_texts, modules, model_names
    ):
        lines.append(f"* ---- begin block {block_id} ({cosim.file}) ----")
        lines.append(cosim.source_text.rstrip("\n"))
        lines.append(cosim_model_line(module, model_name))
        lines.append(f"* ---- end block {block_id} ----")
        lines.append("")
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    if len(text.encode("utf-8")) > MAX_COMPOSITION_BYTES:
        raise CosimCompileError(
            "cosim.compose.oversize",
            "The composed mixed-signal prelude exceeds the composition byte bound.",
        )
    return CosimComposition(
        library_id=library_id,
        library_version=library_version,
        library_digest=library_digest,
        requested=requested,
        text=text,
        text_sha256=_sha256_bytes(text.encode("utf-8")),
        modules=tuple(modules),
        model_names=tuple(model_names),
        wrappers=tuple(cosim.wrapper for _block_id, cosim in wrapper_texts),
        constraints=tuple(
            tuple(getattr(cosim, "parameter_constraints", ()) or ())
            for _block_id, cosim in wrapper_texts
        ),
        defaults=tuple(
            _contract_defaults(blocks[block_id]) for block_id, _cosim in wrapper_texts
        ),
    )


def _x_card_child(fields: Sequence[str]) -> str | None:
    """The subcircuit an ``X`` card instantiates, under every parameter spelling.

    ngspice accepts ``X1 n1 n2 sub p=1``, ``X1 n1 n2 sub params: p=1``, and
    ``X1 n1 n2 sub p = 1`` (spaces around ``=``). Reading "the last token that
    contains no ``=``" gets the last two wrong and mistakes a parameter VALUE
    for the subcircuit name, which is how a second instance slipped past the
    gate. Tokens are therefore re-joined around ``=`` first, then the whole
    parameter tail is dropped.
    """

    joined: list[str] = []
    for token in fields:
        if token == "=" and joined:
            joined[-1] = joined[-1] + "="
            continue
        if token.startswith("=") and joined:
            joined[-1] = joined[-1] + token
            continue
        if joined and joined[-1].endswith("="):
            joined[-1] = joined[-1] + token
            continue
        joined.append(token)
    child: str | None = None
    for token in joined[1:]:
        lowered = token.lower()
        if lowered in ("params:", "params"):
            break
        if "=" in token:
            break
        child = lowered
    return child


#: A SPICE scalar with an optional engineering suffix, as an X-card parameter
#: value may spell it. Matches block_library's literal grammar; an expression
#: default (braces, identifiers) is not a literal and is refused rather than
#: guessed at.
_SCALAR_RE = re.compile(
    r"^(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)"
    r"(?P<letters>[a-z]*)$",
    re.IGNORECASE,
)
_SCALE_FACTORS: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
)


def _scalar_value(text: str) -> float | None:
    match = _SCALAR_RE.fullmatch(text.strip())
    if match is None:
        return None
    value = float(match.group("number"))
    letters = match.group("letters").lower()
    for factor, scale in _SCALE_FACTORS:
        if letters.startswith(factor):
            value *= scale
            break
    return value


def verify_parameter_constraints(
    constraints: Sequence[Mapping[str, object]],
    effective: Mapping[str, float],
    *,
    block_id: str,
) -> None:
    """Check the backend's declared relational constraints, fail-closed.

    A per-parameter ``range`` cannot say ``td > tedge/2 + 7 ps``, so the
    backend declares such relations and they are checked HERE against the
    effective parameters of the instantiating card. Without this the contract
    admits a parameterization the realization cannot honor and the simulator
    silently clamps it, returning a latency other than the declared one.
    """

    for constraint in constraints:
        left_name = str(constraint["left"])
        if left_name not in effective:
            raise CosimCompileError(
                "cosim.parameters.unresolved",
                f"{block_id}: constraint {constraint['id']!r} names the "
                f"parameter {left_name!r}, which has no effective value.",
            )
        total = float(constraint["right_constant"])
        for term in constraint["right_terms"]:  # type: ignore[union-attr]
            name = str(term["parameter"])  # type: ignore[index]
            if name not in effective:
                raise CosimCompileError(
                    "cosim.parameters.unresolved",
                    f"{block_id}: constraint {constraint['id']!r} names the "
                    f"parameter {name!r}, which has no effective value.",
                )
            total += float(term["coefficient"]) * effective[name]  # type: ignore[index]
        left = effective[left_name]
        relation = str(constraint["relation"])
        satisfied = left > total if relation == ">" else left >= total
        if not satisfied:
            raise CosimCompileError(
                "cosim.parameters.constraint_violated",
                f"{block_id}: {left_name}={left!r} violates the declared "
                f"constraint {constraint['id']!r} ({left_name} {relation} "
                f"{total!r}). {constraint.get('rationale', '')}".strip(),
            )


def instantiation_parameters(
    deck_text: str, wrapper: str, defaults: Mapping[str, float]
) -> dict[str, float]:
    """The effective parameters of the deck's instantiation of ``wrapper``.

    Contract defaults overlaid with the ``name=value`` overrides on the X card
    (continuations folded, every parameter spelling accepted). A non-literal
    override is refused rather than ignored: silently falling back to the
    default would check a constraint against parameters the run does not use.
    """

    maps = instantiation_parameter_maps(deck_text, wrapper, defaults)
    effective = dict(defaults)
    for per_instance in maps:
        effective.update(per_instance)
    return effective


def instantiation_parameter_maps(
    deck_text: str, wrapper: str, defaults: Mapping[str, float]
) -> list[dict[str, float]]:
    """One effective-parameter map PER instantiating X card of ``wrapper``.

    Constraint verification must check EVERY instance: overlaying all cards
    into one map lets an invalid instance hide behind a later valid one (an
    invalid X1 followed by a valid X2 would pass).
    """

    maps: list[dict[str, float]] = []
    target = wrapper.lower()
    for _number, statement in _folded_statements(deck_text):
        fields = statement.split()
        if fields[0][0].lower() != "x":
            continue
        if _x_card_child(fields) != target:
            continue
        effective = dict(defaults)
        for name, value in _x_card_parameters(fields).items():
            scalar = _scalar_value(value)
            if scalar is None:
                raise CosimCompileError(
                    "cosim.parameters.unresolved",
                    f"the instantiation of {wrapper} passes {name}={value!r}, "
                    "which is not a literal scalar; the backend's declared "
                    "parameter constraints cannot be checked against it.",
                )
            effective[name.lower()] = scalar
        maps.append(effective)
    return maps


#: Inline comment forms ngspice strips from each PHYSICAL line before
#: stitching ``+`` continuations (inpcom.c): ``;`` (no whitespace required),
#: ``//``, and a whitespace-preceded ``$``. Stripping must happen pre-fold —
#: post-fold stripping lets ``.model evil ; x`` + a ``+`` continuation hide a
#: protected name from verification while ngspice still binds it.
_INLINE_COMMENT_RE = re.compile(r";.*|//.*|\s\$.*")


def _folded_statements(deck_text: str) -> list[tuple[int, str]]:
    """Deck statements with ``+`` continuations folded and comments dropped.

    Mirrors ngspice's parse order: inline comments are removed from each
    physical line FIRST, then continuations are stitched.
    """

    statements: list[tuple[int, str]] = []
    for number, raw in enumerate(deck_text.splitlines(), start=1):
        stripped = _INLINE_COMMENT_RE.sub("", raw).strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+") and statements:
            first, body = statements[-1]
            statements[-1] = (first, body + " " + stripped[1:].strip())
            continue
        statements.append((number, stripped))
    return statements


def _x_card_parameters(fields: Sequence[str]) -> dict[str, str]:
    """The ``name=value`` overrides on an X card, under every spelling."""

    joined: list[str] = []
    for token in fields:
        if token == "=" and joined:
            joined[-1] = joined[-1] + "="
            continue
        if token.startswith("=") and joined:
            joined[-1] = joined[-1] + token
            continue
        if joined and joined[-1].endswith("="):
            joined[-1] = joined[-1] + token
            continue
        joined.append(token)
    parameters: dict[str, str] = {}
    for token in joined:
        if token.lower() in ("params:", "params"):
            continue
        name, separator, value = token.partition("=")
        if separator and name and value:
            parameters[name] = value
    return parameters


def verify_single_instantiation(
    deck_text: str,
    wrappers: Sequence[str],
    binding_models: Sequence[str] = (),
) -> None:
    """Refuse a deck that would place more than one ``d_cosim`` in the circuit.

    The shipped shim supports exactly one instance (see the module docstring),
    so exactly one may exist. Every way a deck can create one is counted:

    * an ``X`` card instantiating a composed wrapper (each wrapper body holds
      exactly one ``d_cosim`` A-device), under every parameter spelling; and
    * an ``A`` card referencing a generated binding model DIRECTLY, which is a
      d_cosim instance without any wrapper in sight.

    Refused shapes: more than one instance in total; any instance nested
    inside a deck-defined ``.subckt``, because the realized count then depends
    on how often that subcircuit is itself instantiated, which this bounded
    scan does not resolve and guessing it would be the difference between a
    correct run and a simulator crash; and zero instances, because compiling a
    core the run cannot execute would attest physics that never ran.
    """

    wanted = {name.lower() for name in wrappers}
    bindings = {name.lower() for name in binding_models}
    if not wanted and not bindings:
        return
    # Continuations are FOLDED first: `X1 a b c d e\n+ bhv_x_v1` is ONE card to
    # the simulator, and reading raw lines would see neither an X-card naming
    # the wrapper nor anything to count -- which let a second instance through
    # (self-review finding, same class as the wrapper-symbol fold).
    statements: list[tuple[int, str]] = []
    for number, raw in enumerate(deck_text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+") and statements:
            first, body = statements[-1]
            statements[-1] = (first, body + " " + stripped[1:].strip())
            continue
        statements.append((number, stripped))
    depth = 0
    total = 0
    for number, stripped in statements:
        fields = stripped.split()
        token = fields[0].lower()
        if token == ".subckt":
            depth += 1
            continue
        if token == ".ends":
            depth = max(0, depth - 1)
            continue
        letter = token[0]
        referenced: str | None = None
        if letter == "x":
            child = _x_card_child(fields)
            if child in wanted:
                referenced = child
        elif letter == "a":
            # An XSPICE A-device names its model LAST: a card referencing a
            # generated binding model IS a d_cosim instance, with no wrapper
            # anywhere in the deck.
            model = fields[-1].lower()
            if model in bindings:
                referenced = model
        if referenced is None:
            continue
        if depth > 0:
            raise CosimCompileError(
                "cosim.deck.nested_instance",
                f"line {number}: the cosim instance {referenced!r} sits inside "
                "a deck-defined subcircuit, so its realized instance count is "
                "not bounded by inspection; the mixed-signal backend admits "
                "exactly one top-level instantiation.",
            )
        total += 1
    if total > 1:
        raise CosimCompileError(
            "cosim.deck.multi_instance",
            f"the deck instantiates the cosim wrapper {total} times; ngspice "
            "hosts exactly one d_cosim instance per run (a second either "
            "aborts the simulator or silently makes the results depend on "
            "card order -- see UPSTREAM-NGSPICE-DCOSIM-SHIM-UAF.md). Use the "
            "ngspice-native backend for multi-instance decks.",
        )
    if total == 0:
        raise CosimCompileError(
            "cosim.deck.uninstantiated",
            "the deck never instantiates any composed cosim wrapper "
            f"({', '.join(sorted(wanted))}); compiling a core the run cannot "
            "execute would attest physics that never ran.",
        )


def _contract_defaults(block: object) -> dict[str, float]:
    """The block contract's literal parameter defaults, as numbers."""

    contract = getattr(block, "contract", None)
    if not isinstance(contract, Mapping):
        return {}
    defaults: dict[str, float] = {}
    for parameter in contract.get("parameters", ()):
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        value = parameter.get("default")
        if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(
            value, bool
        ):
            defaults[name.lower()] = float(value)
    return defaults


def _wrapper_symbols(source_text: str) -> tuple[str, ...]:
    """The simulator symbols (.subckt/.model names) a wrapper source defines.

    Continuations are FOLDED before the name is read: ngspice (and the block
    library's own validator) treat ``.model\\n+ <name> <type>`` as one card, so
    reading raw lines and skipping ``+`` continuations would miss a symbol the
    simulator does define -- and a missed symbol is a missed collision with a
    generated binding card.
    """

    statements: list[str] = []
    for raw in source_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+") and statements:
            statements[-1] = statements[-1] + " " + stripped[1:].strip()
            continue
        statements.append(stripped)
    symbols: list[str] = []
    for statement in statements:
        fields = statement.split()
        token = fields[0].lower()
        if token in (".subckt", ".model") and len(fields) >= 2:
            symbols.append(fields[1].lower())
    return tuple(symbols)
