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


#: The exact shim source files linked into every cosim object, relative to the
#: ngspice ``scripts/src`` directory. Their digest is part of the provenance:
#: two of them ARE the runtime that schedules every event the core produces.
_SHIM_FILES = ("verilator_main.cpp", "verilator_shim.cpp")
_SHIM_HEADER = Path("ngspice") / "cmtypes.h"


def resolve_cosim_shim_dir(*, ngspice_bin: str | None = None) -> tuple[Path, str]:
    """Locate the trusted ngspice cosim shim sources and digest them.

    ngspice installs ``scripts/src/verilator_shim.cpp`` (the ``d_cosim``
    coroutine bridge) next to its own share tree; both the Debian host layout
    (``/usr/bin/ngspice`` -> ``/usr/share/ngspice/scripts/src``) and the IIC
    image layout (``/foss/tools/ngspice/bin/ngspice`` ->
    ``/foss/tools/ngspice/share/ngspice/scripts/src``) follow
    ``<prefix>/share/ngspice/scripts/src`` for the resolved binary's prefix.
    The shim belongs to the trusted simulator installation, exactly like the
    code models it talks to; it is digested so the provenance records which
    bridge was compiled in.
    """

    resolved = ngspice_bin or shutil.which("ngspice")
    if resolved is None:
        raise CosimCompileError(
            "cosim.shim.unavailable",
            "no ngspice binary is available to locate the d_cosim shim sources.",
        )
    try:
        prefix = Path(resolved).resolve().parent.parent
    except OSError as exc:
        raise CosimCompileError(
            "cosim.shim.unavailable", f"ngspice binary {resolved!r} could not be resolved: {exc}"
        ) from exc
    candidates = [
        prefix / "share" / "ngspice" / "scripts" / "src",
        Path("/usr/share/ngspice/scripts/src"),
        Path("/usr/local/share/ngspice/scripts/src"),
    ]
    for candidate in candidates:
        if not (candidate / _SHIM_HEADER).is_file():
            continue
        hasher = hashlib.sha256()
        complete = True
        for name in _SHIM_FILES:
            path = candidate / name
            if not path.is_file():
                complete = False
                break
            hasher.update(name.encode())
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
        if not complete:
            continue
        if not _PATH_SAFE_RE.fullmatch(str(candidate)):
            raise CosimCompileError(
                "cosim.shim.unsafe_path",
                f"shim directory {str(candidate)!r} contains characters that "
                "cannot ride on a compiler command line unquoted.",
            )
        return candidate, hasher.hexdigest()
    raise CosimCompileError(
        "cosim.shim.unavailable",
        "the ngspice d_cosim shim sources (scripts/src/verilator_shim.cpp and "
        "ngspice/cmtypes.h) were not found next to the resolved ngspice "
        f"installation ({resolved}); this ngspice cannot host compiled cosim "
        "cores.",
    )


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
            "--CFLAGS",
            f"-I{shim_dir}",
            "--CFLAGS",
            "-fpic",
            "--cc",
            "--build",
            "--exe",
            str(shim_dir / "verilator_main.cpp"),
            str(shim_dir / "verilator_shim.cpp"),
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
        obj_dir / "verilator_shim.o",
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
    )


def _wrapper_symbols(source_text: str) -> tuple[str, ...]:
    """The simulator symbols (.subckt/.model names) a wrapper source defines."""

    symbols: list[str] = []
    for raw in source_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("+"):
            continue
        fields = stripped.split()
        token = fields[0].lower()
        if token in (".subckt", ".model") and len(fields) >= 2:
            symbols.append(fields[1].lower())
    return tuple(symbols)
