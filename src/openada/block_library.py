"""Reviewed behavioral-block library: resolve, validate, and compose.

This module is the second reviewed collateral authority next to
``pdk_bindings``: it owns *technology-free* behavioral blocks the way the PDK
registry owns process collateral. A deck names a public wrapper subcircuit
(``bhv_<block>_v<abi>``) and never a file path; this module decides which
reviewed bytes satisfy that name, binds them by content digest, and composes
one flattened, self-contained prelude that the existing bounded model-library
path can carry unchanged.

Design rules enforced here rather than trusted from authors:

* The library manifest is the only discovery authority. A file that is not
  enumerated (path, role, byte count, sha256) does not exist; symlinks in any
  path component, ``..`` segments, and case-folded path collisions are
  refused, and every file is captured with one bounded descriptor read whose
  bytes are the only bytes ever parsed afterwards.
* Block sources are DUT-only. Analyses, control blocks, includes, global
  options, initial conditions, and embedded stimulus sources are refused by
  line inspection, not convention. ``.model`` types are held to a closed
  capability allowlist so process-, file-, and state-backed XSPICE collateral
  cannot ride in, and every model or subcircuit reference must resolve inside
  the digest-bound closure.
* The contract is enforced, not decorative: wrapper pins must equal the
  declared ports in order, wrapper header parameters must equal the declared
  parameter vocabulary with matching literal defaults, and contract port and
  parameter records must be internally consistent.
* Public and internal simulator symbols are namespaced per block and checked
  for case-folded collisions within each source and across the selected
  composition closure.
* Composition is deterministic: dependency closure in topological order with
  lexicographic tie-breaks, a digest-stable structured header, and one
  whole-composition digest recorded for evidence binding.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .provider_runtime import (
    _installed_data_path,
    _object_without_duplicate_keys,
    _read_json_object,
    _reject_json_constant,
    _schema_issues,
)

LIBRARY_SCHEMA_ID = "openada.behavioral-block-library/v0alpha1"
BLOCK_SCHEMA_ID = "openada.behavioral-block/v0alpha1"
_LIBRARY_SCHEMA_FILENAME = "behavioral-block-library-v0alpha1.schema.json"
_BLOCK_SCHEMA_FILENAME = "behavioral-block-v0alpha1.schema.json"

MAX_MANIFEST_BYTES = 262_144
MAX_BLOCK_CONTRACT_BYTES = 65_536
MAX_SOURCE_BYTES = 262_144
MAX_COMPOSITION_BYTES = 2_097_152
MAX_CLOSURE_BLOCKS = 64

_LIBRARY_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_BLOCK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_SYMBOL_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Directives a DUT-only behavioral source may declare. Everything else that
# starts with a dot is refused, which forbids .include/.lib/.control/.option/
# .temp/.global/.ic/.nodeset and every analysis card without maintaining a
# denylist that can silently fall behind the simulator.
_ALLOWED_DIRECTIVES = {".subckt", ".ends", ".model", ".param"}

# Element letters a behavioral block implementation may instantiate. V/I are
# excluded on purpose: an embedded source is stimulus, and stimulus belongs to
# the testbench (the paper workflow enforces the same rule for DUTs).
_ALLOWED_ELEMENT_LETTERS = frozenset("abcdegjlrsx")

_ELEMENT_FAMILY_LETTERS = {
    "b-source": frozenset("b"),
    "switch": frozenset("s"),
    "diode": frozenset("d"),
    "spice-primitive": frozenset("cegjlrx"),
    "xspice-analog": frozenset("a"),
    "xspice-digital": frozenset("a"),
    "xspice-bridge": frozenset("a"),
}

# Closed capability map from declared element families to the .model types a
# block source may define. Anything outside this map -- d_process, d_source,
# d_state, filesource, multi_input_pwl, table models, semiconductor models --
# is executable or file-backed collateral that must never ride inside a
# reviewed behavioral block, so the gate is an allowlist, never a denylist.
_MODEL_TYPES_BY_FAMILY: dict[str, frozenset[str]] = {
    "xspice-analog": frozenset(
        {
            "gain",
            "summer",
            "mult",
            "divide",
            "limit",
            "ilimit",
            "hyst",
            "oneshot",
            "climit",
            "slew",
            "aswitch",
            "zener",
            "sine",
            "square",
            "triangle",
            "s_xfer",
        }
    ),
    "xspice-digital": frozenset(
        {
            "d_inverter",
            "d_buffer",
            "d_and",
            "d_nand",
            "d_or",
            "d_nor",
            "d_xor",
            "d_xnor",
            "d_dff",
            "d_dlatch",
            "d_srlatch",
            "d_fdiv",
            "d_tristate",
            "d_pullup",
            "d_pulldown",
        }
    ),
    "xspice-bridge": frozenset({"adc_bridge", "dac_bridge"}),
    "diode": frozenset({"d"}),
    "switch": frozenset({"sw", "csw"}),
}

# SPICE literal numbers as they may appear as wrapper header defaults,
# following ngspice semantics: an optional scale factor is matched
# longest-first ("meg"/"mil" before "m"), and any remaining trailing
# alphabetic characters are a unit annotation the simulator ignores
# (1us == 1u, 1kohm == 1k, 100mohm == 100m).
_SPICE_NUMBER_RE = re.compile(
    r"^(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)"
    r"(?P<letters>[a-z]*)$",
    re.IGNORECASE,
)
# Longest-match-first scale factors, atto included.
_SPICE_SCALE_FACTORS: tuple[tuple[str, float], ...] = (
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


class BlockLibraryError(Exception):
    """A behavioral-block library refusal with a stable diagnostic code."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class BlockBackend:
    kind: str
    file: str
    wrapper: str
    source_text: str
    source_sha256: str


@dataclass(frozen=True)
class Block:
    block_id: str
    contract: Mapping[str, Any]
    contract_path: str
    contract_sha256: str
    abi_version: int
    wrapper: str
    depends: tuple[str, ...]
    native: BlockBackend | None
    veriloga: BlockBackend | None = None


@dataclass(frozen=True)
class BlockLibrary:
    library_id: str
    library_version: str
    root: Path
    manifest: Mapping[str, Any]
    library_digest: str
    blocks: Mapping[str, Block]


@dataclass(frozen=True)
class Composition:
    library_id: str
    library_version: str
    library_digest: str
    requested: tuple[str, ...]
    closure: tuple[str, ...]
    closure_digest: str
    text: str
    text_sha256: str

    def record(self) -> dict[str, Any]:
        """Provenance record for retained evidence and result extensions."""

        return {
            "kind": "behavioral-block-composition",
            "library_id": self.library_id,
            "library_version": self.library_version,
            "library_digest": self.library_digest,
            "requested_blocks": list(self.requested),
            "closure_blocks": list(self.closure),
            "closure_digest": self.closure_digest,
            "composition_sha256": self.text_sha256,
        }


def _load_contract_schema(filename: str) -> dict[str, Any]:
    return _read_json_object(
        _installed_data_path("schemas", filename),
        role="contract",
        maximum_bytes=MAX_MANIFEST_BYTES,
    )


def _blocks_roots() -> list[tuple[Path, tuple[str, ...]]]:
    """Every installed location that may carry a block library, deduplicated.

    Each candidate is a ``(base, chain)`` pair: ``base`` is the TRUSTED
    DISCOVERY BASE -- the directory the running code itself lives under and
    is therefore trusted-as-configured (the repo root for the source tree,
    the installed distribution's site/data root for a wheel, the sysconfig
    data directory) -- and ``chain`` is the relative component path from
    that base down to the blocks directory (e.g. ``("blocks",)`` or
    ``("share", "openada", "blocks")``).  Every path below is derived
    WITHOUT resolving symlinks: lexical normalization only (os.path.abspath
    / os.path.normpath, never Path.resolve()).  The O_NOFOLLOW descriptor
    walk in _open_library_root, which starts AT the trusted base and walks
    every chain component, is the ONLY symlink authority; resolving here
    would silently follow a symlinked component before that walk could
    refuse it.
    """

    roots: dict[str, tuple[Path, tuple[str, ...]]] = {}

    def _add(base: Path, chain: tuple[str, ...]) -> None:
        directory = base.joinpath(*chain)
        if directory.is_dir():
            roots[str(directory)] = (base, chain)

    repo_root = Path(
        os.path.normpath(os.path.join(os.path.abspath(__file__), "..", "..", ".."))
    )
    _add(repo_root, ("blocks",))

    import sysconfig
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        installed = distribution("openada")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        for entry in installed.files or ():
            value = entry.as_posix()
            marker = "/share/openada/blocks/"
            probe = f"/{value}"
            if marker in probe:
                candidate = Path(
                    os.path.abspath(str(installed.locate_file(entry)))
                )
                index = probe.index(marker)
                relative = probe[index + len(marker) :]
                depth = len(Path(relative).parts)
                blocks_directory = candidate
                for _ in range(depth):
                    blocks_directory = blocks_directory.parent
                # Split share/openada/blocks back off so the descriptor walk
                # covers those components too; only the distribution root
                # above them is trusted-as-configured.
                parts = blocks_directory.parts
                if len(parts) >= 4:
                    _add(Path(*parts[:-3]), parts[-3:])
                else:  # pragma: no cover - a blocks dir directly under /
                    _add(blocks_directory, ())

    data_root = Path(os.path.abspath(sysconfig.get_path("data")))
    _add(data_root, ("share", "openada", "blocks"))
    return [roots[key] for key in sorted(roots)]


def list_block_libraries() -> tuple[str, ...]:
    """Identities of every installed library that carries a manifest file."""

    identities: set[str] = set()
    for base, chain in _blocks_roots():
        for manifest in sorted(base.joinpath(*chain).glob("*/library-manifest.json")):
            identities.add(manifest.parent.name)
    return tuple(sorted(identities))


def _open_library_root(base: Path, chain: tuple[str, ...]) -> int:
    """Open the library root directory as the confinement anchor.

    The trusted discovery base is opened first with plain ``O_DIRECTORY``
    (symlink resolution IS permitted there: venv and site-packages roots are
    legitimately symlinked, and the base is where the code itself lives, so
    it is trusted-as-configured).  Every RELATIVE component below the base
    -- ``share``, ``openada``, ``blocks``, and the library directory itself
    -- is then walked ``dir_fd``-relative with ``O_NOFOLLOW|O_DIRECTORY``,
    so a symlink at ANY of those components is refused, not just at the
    final one.  Every later library read walks *relative to the returned
    descriptor*, never through a pathname the filesystem could rebind
    between check and read.
    """

    directory = base.joinpath(*chain)
    base_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(base, base_flags)
    except OSError as exc:
        raise BlockLibraryError(
            "blocks.library.unreadable",
            f"The library root is not reachable: {directory}",
        ) from exc
    component_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for part in chain:
        try:
            child = os.open(part, component_flags, dir_fd=descriptor)
        except OSError as exc:
            symlinked = exc.errno == errno.ELOOP
            if not symlinked and exc.errno == errno.ENOTDIR:
                try:
                    refused = os.lstat(part, dir_fd=descriptor)
                except OSError:
                    refused = None
                symlinked = refused is not None and stat.S_ISLNK(refused.st_mode)
            os.close(descriptor)
            if symlinked:
                raise BlockLibraryError(
                    "blocks.library.symlink",
                    f"A library root path component is a symbolic link: "
                    f"{part!r} in {directory}",
                ) from exc
            raise BlockLibraryError(
                "blocks.library.unreadable",
                f"The library root could not be opened as a directory: {directory}",
            ) from exc
        os.close(descriptor)
        descriptor = child
    return descriptor


def _open_confined_leaf(root_fd: int, path: str) -> int:
    """Open one manifest-relative file strictly descriptor-relative.

    Each intermediate component is opened ``O_DIRECTORY|O_NOFOLLOW`` relative
    to its parent descriptor (parents are closed once traversed) and the leaf
    is opened ``O_NOFOLLOW`` relative to the last directory descriptor, so a
    symbolic link at any depth -- or a concurrent parent replacement -- can
    never route the read outside the opened root. ``ELOOP`` is a symlink
    refusal; every other failure is a missing file.
    """

    parts = path.split("/")
    parent_fd = root_fd
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            if leaf:
                flags |= getattr(os, "O_NONBLOCK", 0)
            else:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                descriptor = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                # O_NOFOLLOW reports a symlink leaf as ELOOP; with O_DIRECTORY
                # a symlinked intermediate surfaces as ENOTDIR, so the refused
                # component is lstat-ed (still descriptor-relative) to name
                # the symlink refusal precisely.
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    try:
                        refused = os.lstat(part, dir_fd=parent_fd)
                    except OSError:
                        refused = None
                    if exc.errno == errno.ELOOP or (
                        refused is not None and stat.S_ISLNK(refused.st_mode)
                    ):
                        raise BlockLibraryError(
                            "blocks.library.symlink",
                            f"Manifest path component is a symbolic link: {path}",
                        ) from exc
                raise BlockLibraryError(
                    "blocks.library.file_missing",
                    f"Manifest file is absent or not reachable: {path}",
                ) from exc
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = descriptor
        return parent_fd
    except BlockLibraryError:
        if parent_fd != root_fd:
            os.close(parent_fd)
        raise


def _read_bounded_descriptor(descriptor: int, bound: int) -> bytes:
    chunks: list[bytes] = []
    remaining = bound
    while remaining > 0:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_manifest(root_fd: int, library_id: str) -> dict[str, Any]:
    """Read library-manifest.json through the confined root descriptor."""

    try:
        descriptor = _open_confined_leaf(root_fd, "library-manifest.json")
    except BlockLibraryError as exc:
        raise BlockLibraryError(
            "blocks.library.unreadable",
            f"library-manifest.json for {library_id!r} could not be opened: "
            f"{exc.message}",
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BlockLibraryError(
                "blocks.library.unreadable",
                f"library-manifest.json for {library_id!r} is not a regular file.",
            )
        if info.st_size > MAX_MANIFEST_BYTES:
            raise BlockLibraryError(
                "blocks.library.unreadable",
                f"library-manifest.json for {library_id!r} exceeds the "
                f"{MAX_MANIFEST_BYTES}-byte bound.",
            )
        data = _read_bounded_descriptor(descriptor, MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_MANIFEST_BYTES:
        raise BlockLibraryError(
            "blocks.library.unreadable",
            f"library-manifest.json for {library_id!r} exceeds the "
            f"{MAX_MANIFEST_BYTES}-byte bound.",
        )
    try:
        parsed = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BlockLibraryError(
            "blocks.library.unreadable",
            f"library-manifest.json for {library_id!r} is not one strict-JSON "
            f"object: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise BlockLibraryError(
            "blocks.library.unreadable",
            f"library-manifest.json for {library_id!r} must contain one JSON "
            "object.",
        )
    return parsed


def _library_root(library_id: str) -> tuple[Path, tuple[str, ...]]:
    """The ``(base, chain)`` locating one library below a trusted base."""

    if not _LIBRARY_ID_RE.match(library_id):
        raise BlockLibraryError(
            "blocks.library.invalid_id",
            f"The block-library identity is not acceptable: {library_id!r}",
        )
    candidates: list[tuple[Path, tuple[str, ...]]] = []
    for base, chain in _blocks_roots():
        directory = base.joinpath(*chain, library_id)
        if (directory / "library-manifest.json").is_file():
            candidates.append((base, chain + (library_id,)))
    if not candidates:
        raise BlockLibraryError(
            "blocks.library.not_found",
            f"No installed behavioral block library has identity {library_id!r}.",
            hint="Run `openada blocks list` for the installed identities.",
        )
    if len(candidates) > 1:
        # Multiple installed roots carry the same identity. Nothing is loaded
        # blindly: the manifests' file inventories must agree byte for byte
        # (identical library digests) before the first root may stand in for
        # all of them.
        digests: set[str] = set()
        for base, chain in candidates:
            candidate_fd = _open_library_root(base, chain)
            try:
                manifest = _read_manifest(candidate_fd, library_id)
            finally:
                os.close(candidate_fd)
            try:
                digests.add(_library_digest(manifest))
            except (KeyError, TypeError) as exc:
                raise BlockLibraryError(
                    "blocks.library.ambiguous",
                    f"Library {library_id!r} is installed in multiple roots and "
                    f"the manifest under {base.joinpath(*chain)} cannot be "
                    "digested for comparison.",
                ) from exc
        if len(digests) != 1:
            raise BlockLibraryError(
                "blocks.library.ambiguous",
                f"Library {library_id!r} is installed in "
                f"{len(candidates)} roots whose manifests differ; refusing to "
                "pick one silently.",
                hint="Remove or update the stale installation.",
            )
    return candidates[0]


def _canonical_relative_path(value: str) -> str:
    if "\\" in value or value.startswith("/"):
        raise BlockLibraryError(
            "blocks.library.path_invalid",
            f"A manifest path must be a relative POSIX path: {value!r}",
        )
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BlockLibraryError(
            "blocks.library.path_invalid",
            f"A manifest path may not contain empty, '.' or '..' segments: {value!r}",
        )
    return value


def _casefold_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _verified_file_bytes(root_fd: int, path: str, record: Mapping[str, Any]) -> bytes:
    """Capture one enumerated file: confined, bounded, and read exactly once.

    The walk is descriptor-relative from the already opened library root
    (``O_NOFOLLOW`` at every component, parents closed as they are traversed),
    the size is compared to the manifest via ``fstat`` on the leaf descriptor
    *before* any bytes are read, and the digest is computed on the bytes read
    from that same descriptor. Those bytes are the only bytes any later stage
    parses.
    """

    descriptor = _open_confined_leaf(root_fd, path)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BlockLibraryError(
                "blocks.library.file_missing",
                f"Manifest file is absent or not a regular file: {path}",
            )
        if info.st_size != record["bytes"]:
            raise BlockLibraryError(
                "blocks.library.file_tampered",
                f"{path}: byte count {info.st_size} does not match the manifest "
                f"({record['bytes']}).",
            )
        data = _read_bounded_descriptor(descriptor, record["bytes"] + 1)
    finally:
        os.close(descriptor)
    if len(data) != record["bytes"]:
        raise BlockLibraryError(
            "blocks.library.file_tampered",
            f"{path}: byte count {len(data)} does not match the manifest "
            f"({record['bytes']}).",
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != record["sha256"]:
        raise BlockLibraryError(
            "blocks.library.file_tampered",
            f"{path}: content digest does not match the manifest.",
        )
    return data


def _contract_from_bytes(raw: bytes, contract_rel: str) -> dict[str, Any]:
    """Parse a block contract from already digest-verified bytes.

    The file is never reopened after inventory verification, so a replacement
    race cannot substitute different bytes under the recorded hash. NaN and
    Infinity are rejected the same way the provider runtime rejects them.
    """

    if len(raw) > MAX_BLOCK_CONTRACT_BYTES:
        raise BlockLibraryError(
            "blocks.library.file_invalid",
            f"{contract_rel} exceeds the {MAX_BLOCK_CONTRACT_BYTES}-byte "
            "contract bound.",
        )
    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BlockLibraryError(
            "blocks.library.file_invalid",
            f"{contract_rel} is not one strict-JSON object: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise BlockLibraryError(
            "blocks.library.file_invalid",
            f"{contract_rel} must contain one JSON object.",
        )
    return parsed


def _verified_inventory(
    root_fd: int, manifest: Mapping[str, Any], library_id: str
) -> tuple[dict[str, Mapping[str, Any]], dict[str, bytes], dict[str, str]]:
    """Schema-check the manifest and capture every enumerated file confined."""

    issues = _schema_issues(manifest, _load_contract_schema(_LIBRARY_SCHEMA_FILENAME))
    if issues:
        raise BlockLibraryError(
            "blocks.library.schema",
            f"library-manifest.json for {library_id!r} violates {LIBRARY_SCHEMA_ID}: "
            + "; ".join(issues[:4]),
        )
    if manifest["library_id"] != library_id:
        raise BlockLibraryError(
            "blocks.library.identity_mismatch",
            f"Manifest declares {manifest['library_id']!r} but was installed as "
            f"{library_id!r}.",
        )

    file_records: dict[str, Mapping[str, Any]] = {}
    casefolded: dict[str, str] = {}
    for record in manifest["files"]:
        path = _canonical_relative_path(record["path"])
        folded = _casefold_key(path)
        if folded in casefolded:
            raise BlockLibraryError(
                "blocks.library.path_collision",
                f"Manifest paths collide case-insensitively: {casefolded[folded]!r} "
                f"and {path!r}.",
            )
        casefolded[folded] = path
        file_records[path] = record

    verified_bytes: dict[str, bytes] = {}
    verified_text: dict[str, str] = {}
    for path, record in file_records.items():
        data = _verified_file_bytes(root_fd, path, record)
        verified_bytes[path] = data
        try:
            verified_text[path] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlockLibraryError(
                "blocks.library.file_invalid",
                f"{path}: library files must be UTF-8 text.",
            ) from exc
    return file_records, verified_bytes, verified_text


def load_block_library(library_id: str) -> BlockLibrary:
    """Load, schema-validate, and content-verify one installed library."""

    base, chain = _library_root(library_id)
    root = base.joinpath(*chain)
    root_fd = _open_library_root(base, chain)
    try:
        manifest = _read_manifest(root_fd, library_id)
        file_records, verified_bytes, verified_text = _verified_inventory(
            root_fd, manifest, library_id
        )
    finally:
        os.close(root_fd)

    library_digest = _library_digest(manifest)

    block_schema = _load_contract_schema(_BLOCK_SCHEMA_FILENAME)
    blocks: dict[str, Block] = {}
    # Subcircuit references that resolve outside their own source are bound in
    # a second phase, once every block (and therefore every dependency's exact
    # public wrapper) is known.
    pending_references: dict[str, tuple[tuple[int, str], ...]] = {}
    declared = {entry["block_id"]: entry["contract"] for entry in manifest["blocks"]}
    if len(declared) != len(manifest["blocks"]):
        raise BlockLibraryError(
            "blocks.library.duplicate_block",
            "The manifest declares one block identity more than once.",
        )
    for block_id, contract_rel in declared.items():
        contract_rel = _canonical_relative_path(contract_rel)
        if contract_rel not in file_records:
            raise BlockLibraryError(
                "blocks.library.file_missing",
                f"Block {block_id!r} names a contract outside the file inventory: "
                f"{contract_rel}",
            )
        contract = _contract_from_bytes(verified_bytes[contract_rel], contract_rel)
        issues = _schema_issues(contract, block_schema)
        if issues:
            raise BlockLibraryError(
                "blocks.block.schema",
                f"{contract_rel} violates {BLOCK_SCHEMA_ID}: " + "; ".join(issues[:4]),
            )
        if contract["block_id"] != block_id:
            raise BlockLibraryError(
                "blocks.block.identity_mismatch",
                f"{contract_rel} declares {contract['block_id']!r} but the manifest "
                f"names it {block_id!r}.",
            )
        block, external_references = _bind_block(
            block_id,
            contract,
            contract_rel,
            file_records,
            verified_text,
        )
        blocks[block_id] = block
        if external_references:
            pending_references[block_id] = external_references

    for block in blocks.values():
        for dependency in block.depends:
            if dependency not in blocks:
                raise BlockLibraryError(
                    "blocks.block.dependency_missing",
                    f"Block {block.block_id!r} depends on unknown block "
                    f"{dependency!r}.",
                )

    # Second bind phase: an X card that does not resolve inside its own source
    # must name the EXACT public wrapper of a declared dependency (case
    # folded), never merely a wrapper-shaped name whose block id happens to be
    # in `depends` -- bhv_leaf_v999 must be refused when the dependency's
    # wrapper is bhv_leaf_v1, or it would resolve from the caller's deck.
    for block_id, references in pending_references.items():
        block = blocks[block_id]
        allowed_wrappers = {
            _casefold_key(blocks[dependency].wrapper)
            for dependency in block.depends
        }
        source_rel = block.native.file if block.native is not None else block.contract_path
        for number, reference in references:
            if _casefold_key(reference) not in allowed_wrappers:
                raise BlockLibraryError(
                    "blocks.source.reference_unresolved",
                    f"{source_rel}:{number}: subcircuit reference {reference!r} "
                    "does not resolve within this block source or to the exact "
                    "public wrapper of a declared dependency.",
                )

    return BlockLibrary(
        library_id=library_id,
        library_version=manifest["library_version"],
        root=root,
        manifest=manifest,
        library_digest=library_digest,
        blocks=blocks,
    )


def _library_digest(manifest: Mapping[str, Any]) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"openada.behavioral-block-library/v0alpha1\n")
    hasher.update(
        f"{manifest['library_id']}@{manifest['library_version']}\n".encode()
    )
    for record in sorted(manifest["files"], key=lambda item: item["path"]):
        hasher.update(
            f"{record['path']}\t{record['role']}\t{record['bytes']}\t"
            f"{record['sha256']}\n".encode()
        )
    return hasher.hexdigest()


def _spice_literal(value: str) -> float | None:
    """Value of one literal SPICE number, or None when it is not literal.

    Non-finite parse results are refused as non-literal: ``1e999`` overflows
    to infinity, and an infinite "default" can never be compared against a
    finite contract default, so it must fail closed rather than compare true.
    """

    match = _SPICE_NUMBER_RE.match(value.strip())
    if match is None:
        return None
    try:
        number = float(match.group("number"))
    except ValueError:  # pragma: no cover - the regex admits only floats
        return None
    letters = match.group("letters").lower()
    for factor, scale in _SPICE_SCALE_FACTORS:
        if letters.startswith(factor):
            # Any characters after the scale factor are a unit annotation
            # ngspice ignores (1us, 1kohm, 100mohm); pure-unit tails such as
            # "ohm" match no factor and scale by one.
            number *= scale
            break
    if not math.isfinite(number):
        return None
    return number


def _defaults_match(header_value: float, contract_value: float) -> bool:
    if not (math.isfinite(header_value) and math.isfinite(contract_value)):
        return False
    if header_value == contract_value:
        return True
    scale = max(abs(header_value), abs(contract_value))
    return abs(header_value - contract_value) <= 1e-12 * scale


def _check_contract_semantics(
    block_id: str, contract: Mapping[str, Any], contract_rel: str
) -> None:
    """Semantic contract invariants the JSON schema cannot express."""

    ports = contract["ports"]
    port_names: set[str] = set()
    for index, port in enumerate(ports):
        if port["ordinal"] != index:
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: port ordinals must be exactly 0..{len(ports) - 1} "
                f"in array order; ports[{index}] declares ordinal "
                f"{port['ordinal']}.",
            )
        folded = _casefold_key(port["name"])
        if folded in port_names:
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: port name {port['name']!r} is declared more "
                "than once.",
            )
        port_names.add(folded)
    for port in ports:
        reference = port.get("reference_port")
        if reference is not None and _casefold_key(reference) not in port_names:
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: port {port['name']!r} names reference_port "
                f"{reference!r}, which is not a declared port.",
            )
    parameter_names: set[str] = set()
    for parameter in contract.get("parameters", ()):
        folded = _casefold_key(parameter["name"])
        if folded in parameter_names:
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: parameter {parameter['name']!r} is declared "
                "more than once.",
            )
        parameter_names.add(folded)
        default = parameter["default"]
        if parameter["type"] == "boolean-as-integer" and default not in (0, 1):
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: parameter {parameter['name']!r} is "
                f"boolean-as-integer, so its default must be exactly 0 or 1, "
                f"not {default!r}.",
            )
        window = parameter.get("range") or {}
        minimum = window.get("minimum")
        maximum = window.get("maximum")
        exclusive_minimum = bool(window.get("exclusive_minimum"))
        if minimum is not None and (
            default <= minimum if exclusive_minimum else default < minimum
        ):
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: parameter {parameter['name']!r} default "
                f"{default!r} lies outside its declared range.",
            )
        if maximum is not None and default > maximum:
            raise BlockLibraryError(
                "blocks.block.contract_invalid",
                f"{contract_rel}: parameter {parameter['name']!r} default "
                f"{default!r} lies outside its declared range.",
            )


def _check_wrapper_abi(
    block_id: str,
    wrapper: str,
    text: str,
    source_rel: str,
    contract: Mapping[str, Any],
) -> None:
    """The wrapper header IS the public ABI; compare it to the contract."""

    header: list[str] | None = None
    header_line = 0
    for number, statement in _source_statements(text, source_rel):
        fields = statement.split()
        if (
            fields[0].lower() == ".subckt"
            and len(fields) >= 2
            and fields[1].lower() == wrapper
        ):
            header = fields
            header_line = number
            break
    if header is None:  # pragma: no cover - wrapper presence is checked earlier
        raise BlockLibraryError(
            "blocks.source.wrapper_missing",
            f"{source_rel}: the public wrapper {wrapper!r} is not defined.",
        )

    pins: list[str] = []
    header_parameters: dict[str, str] = {}
    in_parameters = False
    for token in header[2:]:
        if token.lower() in ("params:", "params"):
            in_parameters = True
            continue
        if "=" in token:
            in_parameters = True
            name, _, value = token.partition("=")
            folded = _casefold_key(name)
            if not name or not value:
                raise BlockLibraryError(
                    "blocks.block.parameter_mismatch",
                    f"{source_rel}:{header_line}: malformed wrapper header "
                    f"parameter {token!r}.",
                )
            if folded in header_parameters:
                raise BlockLibraryError(
                    "blocks.block.parameter_mismatch",
                    f"{source_rel}:{header_line}: wrapper header parameter "
                    f"{name!r} appears more than once.",
                )
            header_parameters[folded] = value
            continue
        if in_parameters:
            raise BlockLibraryError(
                "blocks.block.parameter_mismatch",
                f"{source_rel}:{header_line}: token {token!r} follows the "
                "parameter section without a value.",
            )
        pins.append(_casefold_key(token))

    expected_pins = [_casefold_key(port["name"]) for port in contract["ports"]]
    if pins != expected_pins:
        raise BlockLibraryError(
            "blocks.block.abi_mismatch",
            f"{source_rel}:{header_line}: wrapper pins {pins!r} do not equal "
            f"the contract ports {expected_pins!r} in ordinal order.",
        )

    contract_parameters = {
        _casefold_key(parameter["name"]): parameter
        for parameter in contract.get("parameters", ())
    }
    # The header parameter ORDER is part of the ABI: ngspice binds positional
    # instance parameters by header position, so a reordered header with the
    # same vocabulary is a different public interface.
    expected_order = [
        _casefold_key(parameter["name"])
        for parameter in contract.get("parameters", ())
    ]
    if list(header_parameters) != expected_order:
        raise BlockLibraryError(
            "blocks.block.parameter_mismatch",
            f"{source_rel}:{header_line}: wrapper header parameters "
            f"{list(header_parameters)!r} do not equal the contract "
            f"parameter vocabulary {expected_order!r} in declared order.",
        )
    for name, value in header_parameters.items():
        literal = _spice_literal(value)
        if literal is None:
            raise BlockLibraryError(
                "blocks.block.parameter_mismatch",
                f"{source_rel}:{header_line}: wrapper default {name}={value!r} "
                "is not a literal number; expression defaults cannot be "
                "checked against the contract and are refused.",
            )
        declared = contract_parameters[name]["default"]
        if not _defaults_match(literal, float(declared)):
            raise BlockLibraryError(
                "blocks.block.parameter_mismatch",
                f"{source_rel}:{header_line}: wrapper default {name}={value!r} "
                f"does not equal the contract default {declared!r}.",
            )


def _bind_block(
    block_id: str,
    contract: Mapping[str, Any],
    contract_rel: str,
    file_records: Mapping[str, Mapping[str, Any]],
    verified_text: Mapping[str, str],
) -> tuple[Block, tuple[tuple[int, str], ...]]:
    """Bind one block; also return its unresolved external X references.

    The external references are resolved by the caller once the whole library
    is bound, against the exact public wrappers of the declared dependencies.
    """

    _check_contract_semantics(block_id, contract, contract_rel)
    abi_version = contract["abi_version"]
    expected_wrapper = f"bhv_{block_id}_v{abi_version}"
    depends = tuple(contract.get("depends", ()))
    backends = contract["backends"]
    native: BlockBackend | None = None
    declared_native = backends.get("ngspice-native")
    if declared_native is not None:
        wrapper = declared_native["wrapper"]
        if wrapper != expected_wrapper:
            raise BlockLibraryError(
                "blocks.block.wrapper_mismatch",
                f"{block_id}: the public wrapper must be {expected_wrapper!r}, "
                f"not {wrapper!r}.",
            )
        source_rel = f"blocks/{block_id}/{declared_native['file']}"
        record = file_records.get(source_rel)
        if record is None or record["role"] != "ngspice-native":
            raise BlockLibraryError(
                "blocks.block.source_missing",
                f"{block_id}: implementation {source_rel} is not enumerated with "
                "role ngspice-native.",
            )
        source_text = verified_text[source_rel]
        external_references = _validate_native_source(
            block_id,
            wrapper,
            source_text,
            source_rel,
            declared_native["element_families"],
        )
        _check_wrapper_abi(block_id, wrapper, source_text, source_rel, contract)
        native = BlockBackend(
            kind="ngspice-native",
            file=source_rel,
            wrapper=wrapper,
            source_text=source_text,
            source_sha256=record["sha256"],
        )
    else:
        external_references = ()

    # The verilog-a backend is the reviewed source the OSDI compile path
    # (osdi_compile.py) consumes. It is loaded digest-bound the same way, but
    # its body is validated by the compiler, not the SPICE grammar, so it carries
    # no element-family/ABI check here. Its declared wrapper (the module name)
    # must still equal the block's public wrapper.
    veriloga: BlockBackend | None = None
    declared_veriloga = backends.get("verilog-a")
    if declared_veriloga is not None:
        # The verilog-a backend names its top module (which is the block's public
        # wrapper), not a subckt wrapper; the compiled OSDI module carries that
        # exact name so the preload can bind it.
        va_wrapper = declared_veriloga["module"]
        if va_wrapper != expected_wrapper:
            raise BlockLibraryError(
                "blocks.block.wrapper_mismatch",
                f"{block_id}: the verilog-a module must be {expected_wrapper!r}, "
                f"not {va_wrapper!r}.",
            )
        va_rel = f"blocks/{block_id}/{declared_veriloga['file']}"
        va_record = file_records.get(va_rel)
        if va_record is None or va_record["role"] != "verilog-a":
            raise BlockLibraryError(
                "blocks.block.source_missing",
                f"{block_id}: verilog-a source {va_rel} is not enumerated with "
                "role verilog-a.",
            )
        veriloga = BlockBackend(
            kind="verilog-a",
            file=va_rel,
            wrapper=va_wrapper,
            source_text=verified_text[va_rel],
            source_sha256=va_record["sha256"],
        )

    return (
        Block(
            block_id=block_id,
            contract=contract,
            contract_path=contract_rel,
            contract_sha256=file_records[contract_rel]["sha256"],
            abi_version=abi_version,
            wrapper=expected_wrapper,
            depends=depends,
            native=native,
            veriloga=veriloga,
        ),
        external_references,
    )


def _refuse_inline_comment_characters(
    line: str, source_rel: str, number: int
) -> None:
    """Refuse every inline-comment marker in a behavioral block source.

    ngspice's inline-comment semantics (``;``, ``$``, ``//``) depend on the
    simulator's startup compatibility configuration -- under ``ngbehavior=ps``
    a ``$`` is ordinary text, elsewhere it opens a comment -- so ANY stripping
    or emulation here can disagree with what the engine that finally parses
    the composed deck will execute.  Rather than emulate a moving target, the
    library grammar refuses the whole class: block sources may only carry
    full-line ``*`` comments, and the validated bytes are therefore parsed
    identically under every compatibility mode.
    """

    for marker in (";", "$", "//"):
        if marker in line:
            raise BlockLibraryError(
                "blocks.source.invalid",
                f"{source_rel}:{number}: the inline-comment character "
                f"{marker!r} is not permitted in a block source; its meaning "
                "depends on simulator startup configuration. Use full-line "
                "'*' comments only.",
            )


# Only TAB and SPACE are permitted as intra-line whitespace. Every other C0
# control byte (including a bare CR), DEL, NEL, and the Unicode line/paragraph
# separators are refused outright: str.splitlines() would otherwise treat
# VT/FF/CR/NEL/LS/PS as line boundaries, letting a control byte smuggle a
# comment marker onto a synthetic sub-line that never reaches the marker gate
# while ngspice still parses the original byte. Only a CRLF pair is normalized
# to LF (ngspice strips a CR immediately before LF); a standalone CR would
# survive verbatim into the composed deck as intra-line text, so it is refused
# here rather than silently rewritten.
_FORBIDDEN_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f\x85  ]"
)


def _source_statements(text: str, source_rel: str) -> list[tuple[int, str]]:
    """Logical statements with continuations folded, comments dropped."""

    normalized = text.replace("\r\n", "\n")
    statements: list[tuple[int, str]] = []
    for number, raw in enumerate(normalized.split("\n"), start=1):
        control = _FORBIDDEN_CONTROL_RE.search(raw)
        if control is not None:
            raise BlockLibraryError(
                "blocks.source.invalid",
                f"{source_rel}:{number}: a forbidden control character "
                f"{control.group()!r} is not permitted in a block source; only "
                "tab and space may separate tokens.",
            )
        line = raw.rstrip()
        if not line:
            continue
        lstripped = line.lstrip()
        if lstripped.startswith("*"):
            # A '*'-leading line is a full-line comment in ngspice -- EXCEPT
            # '*#', which ngspice strips and runs as a control-mode command (a
            # convention that hides the command from other simulators while
            # ngspice executes it, in batch too). Skipping it as a comment would
            # let a block source smuggle an arbitrary control command past the
            # element and directive allowlists entirely. The trigger is a '*'
            # immediately followed by '#'; '* #' (with a separator) stays inert.
            if lstripped.startswith("*#"):
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: a '*#' line is an ngspice control "
                    "command, not a comment; a block source may carry only "
                    "inert full-line '*' comments.",
                )
            continue
        _refuse_inline_comment_characters(line, source_rel, number)
        stripped = line.strip()
        if stripped.startswith("+"):
            if not statements:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: a continuation line has nothing to "
                    "continue.",
                )
            first, body = statements[-1]
            statements[-1] = (first, (body + " " + stripped[1:].strip()).rstrip())
            continue
        statements.append((number, stripped))
    return statements


_X_CARD_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$")


def _x_card_child(fields: list[str]) -> tuple[str | None, bool, tuple[str, ...]]:
    """Child named by an X card, a non-compact flag, and the parameter tail.

    The node/child list ends at the FIRST token that opens the parameter
    tail: a ``params:``/``params`` keyword, a bare ``=``, or any token
    containing ``=``.  The child is the token immediately before that
    terminator; for the bare ``=`` spelling the parameter NAME precedes the
    ``=``, so the child sits one token further back.  Scanning forward for
    the first terminator (never filtering ``=``-free tokens and taking the
    last one) is what keeps ``X1 a b caller_model PARAMS: p = bhv_leaf_v1``
    from masquerading as a call of ``bhv_leaf_v1`` while ngspice actually
    invokes ``caller_model``.

    Returns ``(child, non_compact, tail)``: ``non_compact`` is True when the
    card spells parameters with a bare ``=`` or a ``params:`` keyword instead
    of the compact ``name=value`` form, and ``tail`` is EVERY token after the
    child so the caller can validate the complete parameter tail rather than
    stopping at the first terminator.
    """

    tokens = fields[1:]
    head = list(tokens)
    non_compact = False
    for index, token in enumerate(tokens):
        if token == "=" or token.lower() in ("params:", "params"):
            non_compact = True
            head = list(tokens[:index])
            if token == "=" and head:
                # ``name = value``: the parameter name precedes the bare
                # ``=``; the child is one token further back.
                head.pop()
            break
        if "=" in token:
            head = list(tokens[:index])
            break
    child = head[-1] if head else None
    tail = tuple(tokens[len(head):]) if head else ()
    return child, non_compact, tail


def _model_type_token(fields: list[str], source_rel: str, number: int) -> str:
    """The .model type: third field, parenthesized parameters stripped."""

    model_type = fields[2].split("(", 1)[0].strip().lower()
    if not model_type:
        raise BlockLibraryError(
            "blocks.source.invalid",
            f"{source_rel}:{number}: .model requires a type token.",
        )
    return model_type


def _validate_native_source(
    block_id: str,
    wrapper: str,
    text: str,
    source_rel: str,
    element_families: list[str],
) -> tuple[tuple[int, str], ...]:
    """Validate one native source; return its external X-card references.

    Every ``.model`` reference must resolve inside this same source; X-card
    children that do not resolve here are returned for the caller to bind
    against the exact public wrappers of the declared dependencies.
    """

    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise BlockLibraryError(
            "blocks.source.oversize",
            f"{source_rel} exceeds the {MAX_SOURCE_BYTES}-byte source bound.",
        )
    allowed_letters: set[str] = set()
    allowed_model_types: set[str] = set()
    for family in element_families:
        allowed_letters.update(_ELEMENT_FAMILY_LETTERS.get(family, frozenset()))
        allowed_model_types.update(_MODEL_TYPES_BY_FAMILY.get(family, frozenset()))
    prefix = f"bhv_{block_id}"
    wrapper_count = 0
    # Explicit subcircuit state machine: the stack carries case-folded names
    # so `.ends <name>` can be matched and definition nesting refused.
    subckt_stack: list[str] = []
    defined_symbols: dict[str, int] = {}
    defined_models: set[str] = set()
    defined_subckts: set[str] = set()
    model_references: list[tuple[int, str]] = []
    subckt_references: list[tuple[int, str]] = []

    def define_symbol(name: str, number: int) -> None:
        folded = _casefold_key(name)
        if folded in defined_symbols:
            raise BlockLibraryError(
                "blocks.compose.symbol_collision",
                f"{source_rel}:{number}: simulator symbol {name!r} is defined "
                f"more than once within this block (first at line "
                f"{defined_symbols[folded]}).",
            )
        defined_symbols[folded] = number

    for number, statement in _source_statements(text, source_rel):
        fields = statement.split()
        token = fields[0].lower()
        if token.startswith("."):
            if token not in _ALLOWED_DIRECTIVES:
                raise BlockLibraryError(
                    "blocks.source.directive_forbidden",
                    f"{source_rel}:{number}: a behavioral block source may not "
                    f"declare {token}; analyses, control, includes, options and "
                    "initial conditions belong to the operation, never the block.",
                )
            if token == ".subckt":
                if subckt_stack:
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: a .subckt definition may not "
                        "be nested inside another .subckt definition.",
                    )
                if len(fields) < 2:
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: .subckt requires a name.",
                    )
                name = fields[1].lower()
                if name == wrapper:
                    wrapper_count += 1
                elif not name.startswith(prefix):
                    raise BlockLibraryError(
                        "blocks.source.symbol_unprefixed",
                        f"{source_rel}:{number}: internal subcircuit {name!r} must "
                        f"carry the {prefix!r} namespace.",
                    )
                define_symbol(name, number)
                defined_subckts.add(_casefold_key(name))
                subckt_stack.append(_casefold_key(name))
            elif token == ".ends":
                if not subckt_stack:
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: .ends without an open .subckt.",
                    )
                if len(fields) > 1:
                    named = _casefold_key(fields[1])
                    if named != subckt_stack[-1]:
                        raise BlockLibraryError(
                            "blocks.source.invalid",
                            f"{source_rel}:{number}: .ends {fields[1]!r} does "
                            f"not close the open subcircuit "
                            f"{subckt_stack[-1]!r}.",
                        )
                subckt_stack.pop()
            elif token == ".model":
                if len(fields) < 3:
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: .model requires a name and a type.",
                    )
                name = fields[1].lower()
                if not name.startswith(prefix):
                    raise BlockLibraryError(
                        "blocks.source.symbol_unprefixed",
                        f"{source_rel}:{number}: model {name!r} must carry the "
                        f"{prefix!r} namespace.",
                    )
                model_type = _model_type_token(fields, source_rel, number)
                if model_type not in allowed_model_types:
                    raise BlockLibraryError(
                        "blocks.source.model_type_forbidden",
                        f"{source_rel}:{number}: .model type {model_type!r} is "
                        "outside the closed capability allowlist for the "
                        f"declared element families {sorted(element_families)!r}; "
                        "process-, file-, and state-backed models can never "
                        "ride inside a reviewed behavioral block.",
                    )
                define_symbol(name, number)
                defined_models.add(_casefold_key(name))
            elif token == ".param":
                if not subckt_stack:
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: a top-level .param leaks into "
                        "the caller's deck; parameters may only be declared "
                        "inside a subcircuit.",
                    )
            continue
        if not subckt_stack:
            raise BlockLibraryError(
                "blocks.source.invalid",
                f"{source_rel}:{number}: element cards may only appear inside the "
                "block subcircuit.",
            )
        letter = token[0]
        if letter in ("v", "i"):
            raise BlockLibraryError(
                "blocks.source.embedded_stimulus",
                f"{source_rel}:{number}: a behavioral block may not embed a "
                "V/I source; stimulus belongs to the testbench.",
            )
        if letter not in _ALLOWED_ELEMENT_LETTERS:
            raise BlockLibraryError(
                "blocks.source.element_forbidden",
                f"{source_rel}:{number}: element letter {letter!r} is outside the "
                "behavioral source vocabulary.",
            )
        if letter not in allowed_letters:
            raise BlockLibraryError(
                "blocks.source.family_undeclared",
                f"{source_rel}:{number}: element {token!r} needs a capability "
                "family the contract does not declare.",
            )
        # Model-referencing element letters, with the model token's exact
        # position per ngspice card grammar:
        #   A: the model name is the LAST token of the card;
        #   D: Dname n+ n- model [...]        -> fields[3] (two nodes);
        #   J: Jname nd ng ns model [...]     -> fields[4] (three nodes);
        #   S: Sname n+ n- nc+ nc- model [..] -> fields[5] (four nodes).
        # Each extracted token must resolve to a .model defined in this same
        # source; nothing may resolve from the caller's deck.
        if letter == "a":
            # XSPICE code-model instance: the model reference is the last token.
            reference = fields[-1].lower()
            if len(fields) < 3 or not _SYMBOL_RE.match(reference):
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: an A-device must end with its "
                    "model name.",
                )
            model_references.append((number, reference))
        elif letter == "d":
            # Dname n+ n- model [...]: the model is the third token after the
            # device name.
            if len(fields) < 4:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: a D-device requires two nodes and "
                    "a model.",
                )
            model_references.append((number, fields[3].lower()))
        elif letter == "j":
            # Jname nd ng ns model [area] [off] [name=value...]: exactly three
            # nodes precede the mandatory model, so the model is the fourth
            # token after the device name and can never be name=value.
            if len(fields) < 5 or "=" in fields[4]:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: a J-device requires three nodes "
                    "and a model.",
                )
            model_references.append((number, fields[4].lower()))
        elif letter == "s":
            # Sname n+ n- nc+ nc- model [on|off]
            if len(fields) < 6:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: an S-device requires four nodes "
                    "and a model.",
                )
            model_references.append((number, fields[5].lower()))
        elif letter == "x":
            # The grammar is kept closed: a reviewed block source must spell
            # every X-card parameter in the compact name=value form, so the
            # child extraction stays trivial and un-spoofable.
            child, non_compact, tail = _x_card_child(fields)
            if non_compact:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: an X-card in a block source must "
                    "spell every parameter in the compact name=value form; a "
                    "bare '=' token or a params: keyword is refused.",
                )
            if child is None:
                raise BlockLibraryError(
                    "blocks.source.invalid",
                    f"{source_rel}:{number}: an X-card must name a subcircuit.",
                )
            # The COMPLETE tail after the child must be compact name=value
            # tokens (nonempty name, nonempty value).  Stopping at the first
            # terminator would let `=10n`, `p=`, a bare `=`, or a late
            # `params:` keyword ride through after one legitimate assignment.
            for extra in tail:
                if not _X_CARD_PARAM_RE.match(extra):
                    raise BlockLibraryError(
                        "blocks.source.invalid",
                        f"{source_rel}:{number}: an X-card in a block source "
                        "must spell every parameter in the compact name=value "
                        f"form; the token {extra!r} is refused.",
                    )
            subckt_references.append((number, child.lower()))
    if subckt_stack:
        raise BlockLibraryError(
            "blocks.source.invalid",
            f"{source_rel}: .subckt/.ends nesting is unbalanced.",
        )
    if wrapper_count != 1:
        raise BlockLibraryError(
            "blocks.source.wrapper_missing",
            f"{source_rel}: the public wrapper {wrapper!r} must be defined "
            f"exactly once (found {wrapper_count}).",
        )
    # Every model reference must resolve to a .model defined in this same
    # source: nothing may resolve from the caller's deck. X-card children that
    # do not resolve here are returned so the loader can bind them against the
    # exact public wrappers of the declared dependencies once every block in
    # the library is known.
    for number, reference in model_references:
        if _casefold_key(reference) not in defined_models:
            raise BlockLibraryError(
                "blocks.source.reference_unresolved",
                f"{source_rel}:{number}: model reference {reference!r} does not "
                "resolve to a .model defined in this block source.",
            )
    return tuple(
        (number, reference)
        for number, reference in subckt_references
        if _casefold_key(reference) not in defined_subckts
    )


def _closure(library: BlockLibrary, requested: tuple[str, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(block_id: str, chain: tuple[str, ...]) -> None:
        if block_id in visited:
            return
        if block_id in visiting:
            raise BlockLibraryError(
                "blocks.compose.dependency_cycle",
                "Block dependencies form a cycle: " + " -> ".join(chain + (block_id,)),
            )
        if block_id not in library.blocks:
            raise BlockLibraryError(
                "blocks.compose.unknown_block",
                f"The library has no block {block_id!r}.",
                hint=f"Run `openada blocks show {library.library_id}`.",
            )
        visiting.add(block_id)
        for dependency in sorted(library.blocks[block_id].depends):
            visit(dependency, chain + (block_id,))
        visiting.discard(block_id)
        visited.add(block_id)
        ordered.append(block_id)

    for block_id in requested:
        visit(block_id, ())
    if len(ordered) > MAX_CLOSURE_BLOCKS:
        raise BlockLibraryError(
            "blocks.compose.closure_oversize",
            f"The selected closure spans {len(ordered)} blocks; the bound is "
            f"{MAX_CLOSURE_BLOCKS}.",
        )
    return tuple(ordered)


def compose_blocks(library: BlockLibrary, block_ids: tuple[str, ...]) -> Composition:
    """Compose the selected blocks (with dependencies) into one prelude."""

    if not block_ids:
        raise BlockLibraryError(
            "blocks.compose.empty",
            "At least one block must be selected.",
        )
    for block_id in block_ids:
        if not _BLOCK_ID_RE.match(block_id):
            raise BlockLibraryError(
                "blocks.compose.unknown_block",
                f"The block selection {block_id!r} is not a valid block identity.",
            )
    requested = tuple(sorted(dict.fromkeys(block_ids)))
    closure = _closure(library, requested)

    symbols: dict[str, str] = {}
    for block_id in closure:
        block = library.blocks[block_id]
        if block.native is None:
            raise BlockLibraryError(
                "blocks.compose.backend_unavailable",
                f"Block {block_id!r} has no ngspice-native implementation.",
            )
        for number, statement in _source_statements(
            block.native.source_text, block.native.file
        ):
            token = statement.split()[0].lower()
            if token in (".subckt", ".model"):
                name = _casefold_key(statement.split()[1])
                owner = symbols.get(name)
                if owner is not None and owner != block_id:
                    raise BlockLibraryError(
                        "blocks.compose.symbol_collision",
                        f"Simulator symbol {name!r} is defined by both "
                        f"{owner!r} and {block_id!r}.",
                    )
                symbols[name] = block_id

    closure_hasher = hashlib.sha256()
    closure_hasher.update(b"openada.behavioral-block-composition/v0alpha1\n")
    closure_hasher.update(f"{library.library_id}@{library.library_version}\n".encode())
    closure_hasher.update((library.library_digest + "\n").encode())

    lines: list[str] = []
    lines.append("* openada behavioral-block composition")
    lines.append(
        f"* library {library.library_id}@{library.library_version} "
        f"digest {library.library_digest}"
    )
    for block_id in closure:
        block = library.blocks[block_id]
        assert block.native is not None
        closure_hasher.update(
            f"{block_id}\t{block.contract['contract_version']}\t"
            f"{block.abi_version}\t{block.contract_sha256}\t"
            f"{block.native.source_sha256}\n".encode()
        )
        lines.append(
            f"* block {block_id} contract {block.contract['contract_version']} "
            f"abi v{block.abi_version} source sha256 {block.native.source_sha256}"
        )
    closure_digest = closure_hasher.hexdigest()
    lines.append(f"* closure digest {closure_digest}")
    lines.append("")
    for block_id in closure:
        block = library.blocks[block_id]
        assert block.native is not None
        lines.append(f"* ---- begin block {block_id} ({block.native.file}) ----")
        lines.append(block.native.source_text.rstrip("\n"))
        lines.append(f"* ---- end block {block_id} ----")
        lines.append("")
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    if len(text.encode("utf-8")) > MAX_COMPOSITION_BYTES:
        raise BlockLibraryError(
            "blocks.compose.oversize",
            "The composed prelude exceeds the composition byte bound.",
        )
    return Composition(
        library_id=library.library_id,
        library_version=library.library_version,
        library_digest=library.library_digest,
        requested=requested,
        closure=closure,
        closure_digest=closure_digest,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def parse_blocks_selection(value: str) -> tuple[str, tuple[str, ...]]:
    """Parse ``<library>:<block>[,<block>...]`` from the CLI."""

    library_id, separator, selection = value.partition(":")
    if not separator or not selection.strip():
        raise BlockLibraryError(
            "blocks.selection.invalid",
            "The --blocks selection must be <library>:<block>[,<block>...].",
        )
    block_ids = tuple(
        part.strip() for part in selection.split(",") if part.strip()
    )
    if not block_ids:
        raise BlockLibraryError(
            "blocks.selection.invalid",
            "The --blocks selection names no blocks.",
        )
    return library_id.strip(), block_ids
