"""Shared pins and bounded helpers for the IHP analog-measurement chain."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
SHARED_COMMON = HERE.parent / "ihp-inverter" / "common.py"
CHAIN_ID = "openada.chain/ihp-analog-measurements/v1"
DESIGN_REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
IMAGE_REFERENCE = (
    "hpretl/iic-osic-tools@sha256:"
    "fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
PDK_REVISION = "144f811cdffda49b71d28f64e8a92b697b61cf06"


def _load_shared() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_openada_ihp_measurements_shared", SHARED_COMMON
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared conformance helpers: {SHARED_COMMON}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()
ConformanceError = _shared.ConformanceError
default_cache_dir = _shared.default_cache_dir
ensure_external_cache = _shared.ensure_external_cache
ensure_external_design_path = _shared.ensure_external_design_path
require_mount_safe_path = _shared.require_mount_safe_path
run_checked = _shared.run_checked
sha256_file = _shared.sha256_file


def strict_json(path: Path, *, label: str) -> dict[str, Any]:
    """Read one bounded JSON object while rejecting duplicate keys and NaN."""

    def closed(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        if not 0 < path.stat().st_size <= 16 * 1024 * 1024:
            raise ValueError("file size is outside the reviewed bound")
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=closed,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be one object")
    return document


def load_manifest(path: Path) -> dict[str, Any]:
    document = strict_json(path, label="measurement-chain manifest")
    schema_path = REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json"
    schema = strict_json(schema_path, label="semantic-chain manifest schema")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(
            f"measurement-chain manifest violates its schema at {location}: "
            f"{error.message}"
        )
    if document.get("id") != CHAIN_ID:
        raise ConformanceError(f"chain id must be {CHAIN_ID!r}")
    design = document["design"]
    if (
        design["repository"]
        != "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git"
        or design["revision"] != DESIGN_REVISION
    ):
        raise ConformanceError("public design identity drifted")
    runtime = document["runtime"]
    if (
        runtime["image_reference"] != IMAGE_REFERENCE
        or runtime["image_config_digest"] != IMAGE_CONFIG_DIGEST
        or runtime["platform"] != "linux/amd64"
        or runtime["pdk_revision"] != PDK_REVISION
    ):
        raise ConformanceError("pinned runtime identity drifted")
    for contract in document["contracts"]:
        candidate = REPOSITORY_ROOT / contract["repository_path"]
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"declared contract is unavailable: {candidate}")
        observed = sha256_file(candidate)
        if observed != contract["sha256"]:
            raise ConformanceError(
                f"contract hash drift for {contract['repository_path']}: "
                f"expected {contract['sha256']}, got {observed}"
            )
    return document


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    facade = {"runtime": {"image": {"reference": manifest["runtime"]["image_reference"]}}}
    record = _shared.inspect_image(container_engine, facade)
    if record.get("Id") != manifest["runtime"]["image_config_digest"]:
        raise ConformanceError(
            f"image config digest is {record.get('Id')!r}, expected "
            f"{manifest['runtime']['image_config_digest']!r}"
        )
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    if not design_dir.is_dir() or not (design_dir / ".git").is_dir():
        raise ConformanceError(
            f"pinned design checkout is missing at {design_dir}; run setup.py first"
        )
    revision = run_checked(
        ["git", "-C", str(design_dir), "rev-parse", "HEAD"]
    ).stdout.strip()
    if revision != DESIGN_REVISION:
        raise ConformanceError(f"design checkout is at {revision}, expected {DESIGN_REVISION}")
    status = run_checked(
        ["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status:
        raise ConformanceError("design checkout has local changes")
    for record in [manifest["design"]["license"], *manifest["design"]["inputs"]]:
        candidate = design_dir / record["path"]
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"pinned design input is unavailable: {candidate}")
        if sha256_file(candidate) != record["sha256"]:
            raise ConformanceError(f"pinned design input hash drifted: {candidate}")
    return revision


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CHAIN_ID",
    "ConformanceError",
    "DESIGN_REVISION",
    "IMAGE_REFERENCE",
    "REPOSITORY_ROOT",
    "canonical_sha256",
    "default_cache_dir",
    "ensure_external_cache",
    "ensure_external_design_path",
    "inspect_image",
    "load_manifest",
    "require_mount_safe_path",
    "run_checked",
    "sha256_file",
    "strict_json",
    "verify_design_checkout",
]
