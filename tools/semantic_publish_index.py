#!/usr/bin/env python3
"""Build the release semantic-chain index from seven verified run receipts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import stat
import sys
from typing import Any

from semantic_receipts import (
    SemanticReceiptError,
    atomic_write_text,
    semantic_subject,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog/semantic-surfaces-v0alpha1.json"
DEFAULT_INDEX = ROOT / "conformance/semantic-chains/index.json"


@dataclass(frozen=True)
class ChainPublication:
    manifest: str
    run: str
    conformance_record_ids: tuple[str, ...] = ()


PUBLICATIONS = (
    ChainPublication(
        "conformance/ihp-inverter/semantic-chain.json",
        "conformance/ihp-inverter/semantic-chain-run.json",
    ),
    ChainPublication(
        "conformance/ihp-analog-measurements/manifest.json",
        "conformance/ihp-analog-measurements/semantic-chain-run.json",
    ),
    ChainPublication(
        "conformance/ihp-inverter-agent-chain/manifest.json",
        "conformance/ihp-inverter-agent-chain/evidence/chain-run.json",
    ),
    ChainPublication(
        "conformance/ihp-ngspice-provider-analyses/manifest.json",
        "conformance/ihp-ngspice-provider-analyses/semantic-chain-run.json",
        (
            "org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1",
        ),
    ),
    ChainPublication(
        "conformance/ihp-sar-rtl/semantic-chain.json",
        "conformance/ihp-sar-rtl/semantic-chain-run.json",
    ),
    ChainPublication(
        "conformance/public-spice-portability/manifest.json",
        "conformance/public-spice-portability/evidence/chain-run.json",
    ),
    ChainPublication(
        "conformance/orfs-ibex-synthesis-timing/semantic-chain.json",
        "conformance/orfs-ibex-synthesis-timing/semantic-chain-run.json",
    ),
)


class IndexError(RuntimeError):
    """A release receipt cannot safely enter the shared index."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key {key!r}")
        output[key] = value
    return output


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise IndexError(f"cannot stat {label} {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path.is_symlink()
        or metadata.st_size <= 0
    ):
        raise IndexError(f"{label} is not one nonempty regular file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise IndexError(f"cannot parse {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IndexError(f"{label} root is not an object: {path}")
    return value


def _file_ref(relative: str) -> dict[str, object]:
    return {
        "repository_path": relative,
        "sha256": sha256_file(ROOT / relative),
        "extensions": {},
    }


def build_index() -> dict[str, object]:
    subject = semantic_subject(ROOT, CATALOG)
    records: list[dict[str, object]] = []
    chain_ids: set[str] = set()
    conformance_ids: set[str] = set()

    for publication in PUBLICATIONS:
        manifest_path = ROOT / publication.manifest
        run_path = ROOT / publication.run
        manifest = _load_json(manifest_path, label="chain manifest")
        run = _load_json(run_path, label="chain run")

        chain_id = manifest.get("id")
        if not isinstance(chain_id, str) or not chain_id:
            raise IndexError(f"chain manifest has no valid ID: {publication.manifest}")
        if chain_id in chain_ids:
            raise IndexError(f"duplicate chain ID {chain_id!r}")
        chain_ids.add(chain_id)
        if run.get("chain_id") != chain_id:
            raise IndexError(f"run does not identify {chain_id!r}: {publication.run}")
        if run.get("chain_manifest_sha256") != sha256_file(manifest_path):
            raise IndexError(f"run does not bind manifest bytes: {publication.run}")
        if run.get("semantic_subject_sha256") != subject:
            raise IndexError(f"run is stale for the semantic subject: {publication.run}")
        if run.get("status") != "pass":
            raise IndexError(f"run is not passing: {publication.run}")

        source = run.get("source_attestation")
        if not isinstance(source, dict):
            raise IndexError(f"run has no source attestation: {publication.run}")
        required_source = {
            "receipt_class": "release",
            "semantic_subject_sha256": subject,
            "clean_before": True,
            "clean_after": True,
            "state_unchanged": True,
        }
        for key, expected in required_source.items():
            if source.get(key) != expected:
                raise IndexError(
                    f"run source attestation {key!r} is not {expected!r}: "
                    f"{publication.run}"
                )

        for conformance_id in publication.conformance_record_ids:
            if conformance_id in conformance_ids:
                raise IndexError(
                    f"provider conformance record is registered twice: {conformance_id}"
                )
            conformance_ids.add(conformance_id)

        records.append(
            {
                "id": chain_id,
                "conformance_record_ids": list(publication.conformance_record_ids),
                "manifest": _file_ref(publication.manifest),
                "run": _file_ref(publication.run),
                "extensions": {},
            }
        )

    return {
        "schema": "openada.semantic-chain-index/v0alpha1",
        "records": records,
        "extensions": {},
    }


def _encoded(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the repository index; otherwise check exact bytes.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=DEFAULT_INDEX,
        help="Index path (default: repository semantic-chain index).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    index_path = args.index if args.index.is_absolute() else ROOT / args.index
    try:
        encoded = _encoded(build_index())
        if args.write:
            if (index_path.exists() or index_path.is_symlink()) and (
                index_path.is_symlink() or not index_path.is_file()
            ):
                raise IndexError(f"index destination is not a regular file: {index_path}")
            atomic_write_text(index_path, encoded)
            print(f"published {len(PUBLICATIONS)} release records to {index_path}")
            return 0
        observed = index_path.read_text(encoding="utf-8")
        if observed != encoded:
            raise IndexError(f"release index is stale: {index_path}")
        print(f"verified {len(PUBLICATIONS)} release records in {index_path}")
        return 0
    except (OSError, IndexError, SemanticReceiptError, ValueError) as exc:
        print(f"semantic index publication failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
