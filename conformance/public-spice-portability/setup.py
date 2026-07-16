#!/usr/bin/env python3
"""Fetch the two exact public designs and frozen reference runtime."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

from common import (
    ConformanceError,
    IHP_REPOSITORY,
    IHP_REVISION,
    XYCE_REPOSITORY,
    XYCE_REVISION,
    XYCE_TAG,
    cache_checkouts,
    default_cache_dir,
    ensure_checkout_path,
    ensure_external_cache,
    inspect_image,
    load_manifest,
    run_checked,
    verify_ihp_checkout,
    verify_xyce_checkout,
)


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch pinned IHP and Xyce public portability fixtures."
    )
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--container-engine", default="docker")
    return parser


def _fetch_revision(
    destination: Path,
    *,
    repository: str,
    revision: str,
    tag: str | None,
) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-fetch-", dir=destination.parent)
    )
    try:
        run_checked(["git", "init", "--quiet", str(temporary)])
        run_checked(["git", "-C", str(temporary), "remote", "add", "origin", repository])
        if tag is None:
            run_checked(
                [
                    "git", "-C", str(temporary), "fetch", "--depth", "1",
                    "origin", revision,
                ]
            )
        else:
            run_checked(
                [
                    "git", "-C", str(temporary), "fetch", "--depth", "1",
                    "origin", "tag", tag,
                ]
            )
        run_checked(
            ["git", "-C", str(temporary), "checkout", "--quiet", "--detach", revision]
        )
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.expanduser().resolve())
        cache = args.cache_dir.expanduser().resolve()
        ensure_external_cache(cache, REPOSITORY_ROOT)
        cache.mkdir(parents=True, exist_ok=True)
        xyce, ihp = cache_checkouts(cache)
        xyce = ensure_checkout_path(xyce, REPOSITORY_ROOT, cache)
        ihp = ensure_checkout_path(ihp, REPOSITORY_ROOT, cache)
        print(f"Pulling pinned image {manifest['runtime']['image_reference']} ...")
        run_checked(
            [
                args.container_engine,
                "pull",
                "--platform",
                manifest["runtime"]["platform"],
                manifest["runtime"]["image_reference"],
            ]
        )
        inspect_image(args.container_engine, manifest)
        print(f"Fetching {XYCE_TAG} into {xyce} ...")
        _fetch_revision(
            xyce,
            repository=XYCE_REPOSITORY,
            revision=XYCE_REVISION,
            tag=XYCE_TAG,
        )
        print(f"Fetching IHP AnalogAcademy {IHP_REVISION} into {ihp} ...")
        _fetch_revision(
            ihp,
            repository=IHP_REPOSITORY,
            revision=IHP_REVISION,
            tag=None,
        )
        verify_xyce_checkout(xyce, manifest)
        verify_ihp_checkout(ihp, manifest)
    except ConformanceError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    print("Setup complete. run.py needs no network access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
