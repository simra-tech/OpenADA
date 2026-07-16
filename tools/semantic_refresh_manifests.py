#!/usr/bin/env python3
"""Refresh exact repository-file digests in static semantic-chain manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

from semantic_receipts import SemanticReceiptError, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
STATIC_MANIFESTS = (
    ROOT / "conformance/ihp-inverter/semantic-chain.json",
    ROOT / "conformance/ihp-sar-rtl/semantic-chain.json",
    ROOT / "conformance/orfs-ibex-synthesis-timing/semantic-chain.json",
    ROOT / "conformance/ihp-analog-measurements/manifest.json",
    ROOT / "conformance/ihp-inverter-agent-chain/manifest.json",
    ROOT / "conformance/public-spice-portability/manifest.json",
)
REFERENCE_RE = re.compile(
    r'(?P<prefix>"repository_path"\s*:\s*"(?P<path>[A-Za-z0-9._/-]+)"'
    r'\s*,\s*"sha256"\s*:\s*")(?P<digest>[0-9a-f]{64})(?P<suffix>")'
)


class RefreshError(RuntimeError):
    """A manifest reference cannot be refreshed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _validate_json(encoded: str, path: Path) -> None:
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_strict_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token!r}")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RefreshError(f"cannot parse {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise RefreshError(f"manifest root is not an object: {path.relative_to(ROOT)}")


def _replacement(match: re.Match[str], manifest: Path) -> str:
    relative = match.group("path")
    candidate = (ROOT / relative).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise RefreshError(f"reference escapes repository root: {relative}") from exc
    if candidate == manifest.resolve():
        raise RefreshError(f"manifest contains a self-digest reference: {relative}")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise RefreshError(f"cannot stat referenced file {relative}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or candidate.is_symlink()
        or metadata.st_size <= 0
    ):
        raise RefreshError(f"reference is not one nonempty regular file: {relative}")
    return match.group("prefix") + _sha256(candidate) + match.group("suffix")


def refresh(path: Path, *, write: bool) -> tuple[bool, int]:
    path = path.resolve()
    encoded = path.read_text(encoding="utf-8")
    _validate_json(encoded, path)
    matches = list(REFERENCE_RE.finditer(encoded))
    if not matches:
        raise RefreshError(f"manifest has no repository file references: {path}")
    refreshed = REFERENCE_RE.sub(lambda match: _replacement(match, path), encoded)
    _validate_json(refreshed, path)
    changed = refreshed != encoded
    if changed and write:
        atomic_write_text(path, refreshed)
    return changed, len(matches)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Update manifests in place; without this flag, drift exits nonzero.",
    )
    parser.add_argument(
        "manifests",
        nargs="*",
        type=Path,
        help="Optional manifest paths; defaults to the six static publishers.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifests = args.manifests or list(STATIC_MANIFESTS)
    drifted: list[str] = []
    try:
        for manifest in manifests:
            path = manifest if manifest.is_absolute() else ROOT / manifest
            changed, reference_count = refresh(path, write=args.write)
            relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
            state = "updated" if changed and args.write else "drift" if changed else "current"
            print(f"{relative}: {state} ({reference_count} references)")
            if changed:
                drifted.append(relative)
    except (OSError, RefreshError, SemanticReceiptError, ValueError) as exc:
        print(f"semantic manifest refresh failed: {exc}", file=sys.stderr)
        return 2
    if drifted and not args.write:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
