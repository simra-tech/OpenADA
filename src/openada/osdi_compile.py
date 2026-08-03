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
