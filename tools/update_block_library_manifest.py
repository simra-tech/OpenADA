#!/usr/bin/env python3
"""Regenerate the file inventory of one behavioral block library manifest.

The manifest is the only discovery authority the loader trusts, so every
content change must be re-enumerated here. Header fields (identity, version,
title, description, backend requirements, block list) are preserved from the
existing manifest; only ``files`` is recomputed from the tree. Bump
``library_version`` by hand when content changes meaning.

Usage: python tools/update_block_library_manifest.py blocks/<library-id>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROLE_BY_SHAPE = (
    ("block.json", "contract"),
    (".ngspice.sp", "ngspice-native"),
    (".cosim.sp", "xspice-cosim"),
    (".cosim.v", "verilog-cosim-core"),
    (".va", "verilog-a"),
    (".json", "golden-case"),
)


def _role(relative: str) -> str:
    name = relative.rsplit("/", 1)[-1]
    if name == "block.json":
        return "contract"
    for suffix, role in _ROLE_BY_SHAPE[1:]:
        if name.endswith(suffix):
            if role == "golden-case" and "/cases/" not in relative:
                break
            return role
    raise SystemExit(f"unclassifiable library file: {relative}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    root = Path(sys.argv[1]).resolve()
    manifest_path = root / "library-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"missing manifest header: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())

    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        if path.is_symlink():
            raise SystemExit(f"symlinks are refused: {path}")
        relative = path.relative_to(root).as_posix()
        if not relative.startswith("blocks/"):
            raise SystemExit(f"unexpected file outside blocks/: {relative}")
        data = path.read_bytes()
        files.append(
            {
                "path": relative,
                "role": _role(relative),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest["files"] = files
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(f"{manifest_path}: {len(files)} files enumerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
