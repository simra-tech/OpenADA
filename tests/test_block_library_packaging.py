"""The installed wheel must carry every file the block-library manifest
enumerates.

``load_block_library`` digest-verifies each enumerated file, so one file
missing from ``[tool.setuptools.data-files]`` bricks ``--blocks`` (and the
``--osdi``/cosim variants) from the installed release while every
source-checkout test keeps passing. Found by external review 2026-08-03:
the Steps-2/3 commits grew the manifest by five files (the cosim sources
and one case) without updating the packaging table.
"""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]

LIBRARY_DIR = "blocks/bhv-core"
MANIFEST = "library-manifest.json"


def _manifest_enumerated_files() -> set[str]:
    manifest = json.loads((ROOT / LIBRARY_DIR / MANIFEST).read_text())
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("path", "file") and isinstance(value, str):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(manifest)
    return found


def _packaged_library_files() -> set[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    packaged: set[str] = set()
    prefix = LIBRARY_DIR + "/"
    for entries in data_files.values():
        for entry in entries:
            if entry.startswith(prefix):
                packaged.add(entry[len(prefix):])
    return packaged


def test_wheel_data_files_cover_the_block_library_manifest() -> None:
    manifest_files = _manifest_enumerated_files()
    packaged = _packaged_library_files()
    missing = sorted(manifest_files - packaged)
    assert not missing, (
        "library-manifest.json enumerates files the wheel does not package "
        f"(load_block_library will refuse the installed library): {missing}"
    )


def test_packaged_library_files_exist_in_the_tree() -> None:
    stale = sorted(
        entry
        for entry in _packaged_library_files()
        if entry != MANIFEST and not (ROOT / LIBRARY_DIR / entry).is_file()
    )
    assert not stale, f"pyproject packages files missing from the tree: {stale}"
