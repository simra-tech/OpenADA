"""Runtime-neutral discovery for open-source EDA tools and PDKs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Iterable, Mapping

from .process import run_process


@dataclass(frozen=True)
class ToolSpec:
    name: str
    binaries: tuple[str, ...]
    version_args: tuple[tuple[str, ...], ...]
    maturity: str
    operations: tuple[str, ...] = ()
    version_pattern: str | None = None
    accepted_version_exit_codes: tuple[int, ...] = (0,)


TOOL_SPECS: dict[str, ToolSpec] = {
    "xschem": ToolSpec(
        "xschem",
        ("xschem",),
        (("--version",), ("-v",)),
        "workflow-validated",
        ("netlist",),
        r"(?i)^xschem[ \t]+v?\d+(?:\.\d+)*(?:[a-z][a-z0-9]*)?\b",
        (0, 1),
    ),
    "ngspice": ToolSpec(
        "ngspice",
        ("ngspice",),
        (("--version",), ("-v",)),
        "workflow-validated",
        ("simulate",),
        r"(?i)^(?:\*+[ \t]*)?ngspice[- \t]+v?\d+(?:\.\d+)*(?:[a-z][a-z0-9]*)?\b",
    ),
    "klayout": ToolSpec(
        "klayout",
        ("klayout",),
        (("-v",), ("--version",)),
        "workflow-validated",
        ("drc",),
        r"(?i)^klayout[ \t]+v?\d+(?:\.\d+)*(?:[a-z][a-z0-9]*)?\b",
    ),
    "netgen": ToolSpec(
        "netgen",
        # Debian ships the VLSI LVS tool as ``netgen-lvs`` because the
        # unqualified ``netgen`` package is an unrelated mesh generator.
        # Prefer the unambiguous name when both happen to be installed.
        ("netgen-lvs", "netgen"),
        (("-batch",), ("-version",), ("--version",)),
        "workflow-validated",
        ("lvs",),
        r"(?i)^netgen[ \t]+v?\d+(?:\.\d+)*(?:[a-z][a-z0-9]*)?"
        r"[ \t]+compiled[ \t]+on\b",
    ),
    "yosys": ToolSpec(
        "yosys",
        ("yosys",),
        (("-V",), ("--version",)),
        "structured",
        ("rtl-check",),
        r"(?i)^yosys[ \t]+v?\d+(?:\.\d+)*(?:[a-z][a-z0-9]*)?\b",
    ),
    "magic": ToolSpec("magic", ("magic",), (("--version",), ("-version",)), "discovered"),
    "xyce": ToolSpec(
        "xyce",
        ("Xyce", "xyce"),
        (("-v",),),
        "workflow-validated",
        ("simulate",),
        r"(?i)^xyce(?:\(tm\))?[ \t]+(?:release[ \t]+)?"
        r"v?\d+(?:\.\d+)+(?:[-+][a-z0-9._-]+)?\b",
    ),
    "openroad": ToolSpec("openroad", ("openroad",), (("-version",), ("--version",)), "discovered"),
    "iverilog": ToolSpec("iverilog", ("iverilog",), (("-V",),), "discovered"),
    "verilator": ToolSpec("verilator", ("verilator",), (("--version",),), "discovered"),
    "slang": ToolSpec("slang", ("slang",), (("--version",),), "discovered"),
    "surelog": ToolSpec("surelog", ("surelog",), (("--version",), ("-version",)), "discovered"),
    "openvaf": ToolSpec("openvaf", ("openvaf",), (("--version",),), "discovered"),
    "qucs-s": ToolSpec("qucs-s", ("qucs-s",), (("--version",),), "discovered"),
    "gtkwave": ToolSpec("gtkwave", ("gtkwave",), (("--version",), ("-V",)), "discovered"),
    "librelane": ToolSpec("librelane", ("librelane",), (("--version",),), "discovered"),
}


IIC_SEARCH_ROOTS = (Path("/foss/tools/bin"), Path("/foss/tools/klayout"))
IIC_PDK_ROOT = Path("/foss/pdks")
MAX_DISCOVERY_PATH_CHARS = 4_095
MAX_VERSION_TEXT_CHARS = 500


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            expanded = path.expanduser()
        except (OSError, RuntimeError, ValueError):
            continue
        key = str(expanded)
        if key not in seen:
            seen.add(key)
            result.append(expanded)
    return result


class DiscoveryManager:
    """Discover tools from native PATH or an optional reference runtime profile."""

    def __init__(
        self,
        *,
        profile: str = "auto",
        pdk_roots: Iterable[str | Path] | None = None,
        binary_overrides: Mapping[str, str | Path] | None = None,
    ) -> None:
        if profile not in {"auto", "native", "iic-osic-tools"}:
            raise ValueError(f"unsupported runtime profile: {profile}")
        self.profile = self._detect_profile() if profile == "auto" else profile
        self._explicit_pdk_roots = [Path(item) for item in (pdk_roots or ())]
        self._binary_overrides = {
            key: str(value) for key, value in (binary_overrides or {}).items()
        }

    @staticmethod
    def _detect_profile() -> str:
        if Path("/foss/tools").is_dir() and IIC_PDK_ROOT.is_dir():
            return "iic-osic-tools"
        return "native"

    def find_binary(self, tool_name: str) -> str | None:
        spec = TOOL_SPECS.get(tool_name)
        binary_names = spec.binaries if spec else (tool_name,)

        override = self._binary_overrides.get(tool_name)
        if override:
            try:
                path = Path(override).expanduser()
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path.resolve())
            except (OSError, RuntimeError, ValueError):
                return None
            return None

        if self.profile == "iic-osic-tools":
            for root in IIC_SEARCH_ROOTS:
                for binary_name in binary_names:
                    candidate = root / binary_name
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        return str(candidate.resolve())

        for binary_name in binary_names:
            discovered = shutil.which(binary_name)
            if discovered:
                return str(Path(discovered).resolve())
        return None

    @staticmethod
    def _binary_identity(binary: str) -> tuple[int, int, int, int, int, int] | None:
        try:
            metadata = Path(binary).stat()
        except OSError:
            return None
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _version(
        self, spec: ToolSpec, binary: str, timeout: float
    ) -> tuple[str | None, str, int | None]:
        observed_failure = "probe_failed"
        # Some legacy EDA CLIs interpret an unsupported version flag as a
        # design filename. Probe from an isolated directory so discovery can
        # never leave generated files in the user's workspace.
        with tempfile.TemporaryDirectory(prefix="openada-probe-") as probe_dir:
            for args in spec.version_args:
                process = run_process([binary, *args], cwd=probe_dir, timeout=timeout)
                if process.status == "timed_out":
                    observed_failure = "probe_timed_out"
                    continue
                if (
                    process.status != "completed"
                    or process.exit_code not in spec.accepted_version_exit_codes
                ):
                    continue
                if process.exit_code != 0 and process.stderr:
                    observed_failure = "nonzero_probe_stderr"
                    continue
                if process.stdout_truncated or process.stderr_truncated:
                    observed_failure = "output_truncated"
                    continue
                if not process.stdout_utf8_valid or not process.stderr_utf8_valid:
                    observed_failure = "output_invalid_utf8"
                    continue
                combined = "\n".join(part for part in (process.stdout, process.stderr) if part)
                candidate_lines = [
                    line.strip()
                    for line in combined.splitlines()
                    if line.strip() and re.search(r"[A-Za-z0-9]", line)
                ]
                if not candidate_lines:
                    observed_failure = "output_unparseable"
                    continue
                version_line = (
                    next(
                        (
                            line
                            for line in candidate_lines
                            if re.search(spec.version_pattern, line)
                        ),
                        None,
                    )
                    if spec.version_pattern
                    else candidate_lines[0]
                )
                if version_line is None:
                    observed_failure = "output_identity_mismatch"
                    continue
                if len(version_line) > MAX_VERSION_TEXT_CHARS or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in version_line
                ):
                    observed_failure = "output_malformed"
                    continue
                return version_line, "accepted", process.exit_code
        return None, observed_failure, None

    def inspect_tool(
        self,
        tool_name: str,
        *,
        version_timeout: float = 3.0,
        include_probe_details: bool = False,
    ) -> dict:
        if tool_name not in TOOL_SPECS:
            raise KeyError(f"unknown tool: {tool_name}")
        spec = TOOL_SPECS[tool_name]
        binary = self.find_binary(tool_name)
        before_identity = self._binary_identity(binary) if binary else None
        version, probe_status, accepted_exit_code = (
            self._version(spec, binary, version_timeout)
            if binary and before_identity is not None
            else (None, "not_run", None)
        )
        after_identity = self._binary_identity(binary) if binary else None
        identity_stable = (
            binary is not None
            and before_identity is not None
            and before_identity == after_identity
        )
        if binary and not identity_stable:
            version = None
            probe_status = "binary_identity_changed"
            accepted_exit_code = None
        record = {
            "status": "available" if binary and version else ("unusable" if binary else "missing"),
            "binary": binary,
            "version": version,
            "maturity": spec.maturity,
            "operations": list(spec.operations),
        }
        if include_probe_details:
            record["version_probe"] = {
                "status": probe_status if binary else "not_run",
                "binary_identity_stable": identity_stable if binary else None,
                "accepted_exit_code": accepted_exit_code,
            }
        return record

    def pdk_roots(self) -> list[Path]:
        candidates = list(self._explicit_pdk_roots)
        env_root = os.environ.get("PDK_ROOT")
        if env_root:
            candidates.append(Path(env_root))
        if self.profile == "iic-osic-tools":
            candidates.append(IIC_PDK_ROOT)
        candidates.extend((Path("/usr/local/share/pdk"), Path("/opt/pdk")))
        roots: list[Path] = []
        for path in _unique_paths(candidates):
            path_text = str(path)
            if len(path_text) > MAX_DISCOVERY_PATH_CHARS or any(
                ord(character) < 32 or ord(character) == 127 for character in path_text
            ):
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved_text = str(resolved)
                if (
                    len(resolved_text) <= MAX_DISCOVERY_PATH_CHARS
                    and not any(
                        ord(character) < 32 or ord(character) == 127
                        for character in resolved_text
                    )
                    and resolved.is_dir()
                ):
                    roots.append(resolved)
            except (OSError, RuntimeError, ValueError):
                continue
        return roots

    def get_capabilities(
        self,
        tool_names: Iterable[str] | None = None,
        *,
        version_timeout: float = 3.0,
        enumerate_pdks: bool = True,
        include_probe_details: bool = False,
    ) -> dict:
        names = list(tool_names) if tool_names is not None else list(TOOL_SPECS)
        unknown = sorted(set(names) - set(TOOL_SPECS))
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(unknown)}")

        roots = self.pdk_roots()
        pdks: list[dict[str, str]] = []
        if enumerate_pdks:
            for root in roots:
                for entry in sorted(root.iterdir(), key=lambda item: item.name.lower()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        pdks.append({"name": entry.name, "root": str(root)})

        return {
            "runtime": {
                "profile": self.profile,
                "reference_profile": self.profile == "iic-osic-tools",
            },
            "tools": {
                name: self.inspect_tool(
                    name,
                    version_timeout=version_timeout,
                    include_probe_details=include_probe_details,
                )
                for name in names
            },
            "pdk_roots": [str(path) for path in roots],
            "pdks": pdks,
        }
