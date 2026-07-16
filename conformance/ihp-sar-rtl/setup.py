#!/usr/bin/env python3
"""Fetch the two pinned external inputs for IHP SAR RTL conformance."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

from common import (
    ConformanceError,
    default_cache_dir,
    ensure_external_cache,
    ensure_external_design_path,
    inspect_image,
    load_manifest,
    run_checked,
    verify_design_checkout,
)


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--container-engine", default="docker")
    return parser


def fetch_design(design_dir: Path, manifest: dict) -> None:
    if design_dir.exists():
        verify_design_checkout(design_dir, manifest)
        return
    design_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".ihp-sar-fetch-", dir=design_dir.parent))
    try:
        run_checked(["git", "init", "--quiet", str(temporary)])
        run_checked(
            ["git", "-C", str(temporary), "remote", "add", "origin", manifest["design"]["repository"]]
        )
        run_checked(
            [
                "git", "-C", str(temporary), "fetch", "--depth", "1", "origin",
                manifest["design"]["revision"],
            ]
        )
        run_checked(
            [
                "git", "-C", str(temporary), "checkout", "--quiet", "--detach",
                manifest["design"]["revision"],
            ]
        )
        verify_design_checkout(temporary, manifest)
        temporary.rename(design_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.expanduser().resolve())
        cache_dir = args.cache_dir.expanduser().resolve()
        ensure_external_cache(cache_dir, REPOSITORY_ROOT)
        cache_dir.mkdir(parents=True, exist_ok=True)
        design_dir = ensure_external_design_path(
            cache_dir / "IHP-AnalogAcademy", REPOSITORY_ROOT, cache_dir
        )
        image = manifest["runtime"]["image"]
        print(f"Pulling pinned {image['platform']} image {image['reference']} ...", flush=True)
        run_checked(
            [
                args.container_engine, "pull", "--platform", image["platform"],
                image["reference"],
            ]
        )
        inspect_image(args.container_engine, manifest)
        print(f"Fetching pinned design revision into {design_dir} ...", flush=True)
        fetch_design(design_dir, manifest)
        verify_design_checkout(design_dir, manifest)
    except ConformanceError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    print("Setup complete. Network access is not needed by run.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
