"""Shared setup and runner checks for the pinned IHP agent chain."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
SHARED_COMMON = HERE.parent / "ihp-inverter" / "common.py"
CHAIN_ID = "openada.chain/ihp-inverter-agent-chain/v1"
DESIGN_REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
IMAGE_REFERENCE = (
    "hpretl/iic-osic-tools@sha256:"
    "fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
PDK_REVISION = "144f811cdffda49b71d28f64e8a92b697b61cf06"
SCHEMATIC_RELATIVE = Path(
    "modules/module_0_foundations/inverter/inverter_tb.sch"
)


def _load_shared() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_openada_ihp_agent_chain_shared", SHARED_COMMON
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


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the closed chain manifest and reject pin or contract drift."""

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ConformanceError(f"cannot read chain manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError("chain manifest root must be one JSON object")

    schema_path = REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(document),
            key=lambda error: [str(item) for item in error.absolute_path],
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConformanceError(f"cannot load semantic-chain schema: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(
            f"chain manifest violates its schema at {location}: {error.message}"
        )

    if document.get("id") != CHAIN_ID:
        raise ConformanceError(f"chain id must be {CHAIN_ID!r}")
    design = document["design"]
    if (
        design["repository"]
        != "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git"
        or design["revision"] != DESIGN_REVISION
        or design["inputs"]
        != [
            {
                "path": str(SCHEMATIC_RELATIVE),
                "sha256": "521464a42c5352cad371a8b091d71d9a083686749ef49c69b3f07ec838a3cb82",
            },
            {
                "path": "modules/module_0_foundations/inverter/inverter.sym",
                "sha256": "8658fa30ac994a0bedf511e59f064e6458e9b4a30d91382f6974a5117bd5c103",
            },
            {
                "path": "modules/module_0_foundations/inverter/inverter.sch",
                "sha256": "6a2e03f44df59976b8ba4fca385b104b80802d367789171790bd238f912ec771",
            },
        ]
    ):
        raise ConformanceError("chain manifest design identity differs from the reviewed IHP input")
    runtime = document["runtime"]
    if (
        runtime["image_reference"] != IMAGE_REFERENCE
        or runtime["image_config_digest"] != IMAGE_CONFIG_DIGEST
        or runtime["platform"] != "linux/amd64"
        or runtime["pdk_revision"] != PDK_REVISION
    ):
        raise ConformanceError("chain manifest runtime identity differs from the reviewed image/PDK")
    details = document.get("extensions", {}).get("org.openada", {})
    provider = details.get("provider", {})
    required_provider = {
        "manifest_path": "/openada/providers/ngspice-pdk-control/driver-manifest.json",
        "driver_id": "org.openada.driver.ngspice-pdk-control",
        "driver_version": "0.5.0",
        "transport_id": "local-json-stdio",
        "configuration_schema": "openada.ngspice-provider-config/v0alpha1",
        "environment": {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
        "evidence_destination": "/evidence/provider",
        "raw_artifact": "/evidence/provider/work/test_inverter.raw",
        "analysis": {
            "type": "tran",
            "step_s": 5e-8,
            "stop_s": 2e-6,
            "extensions": {},
        },
    }
    if provider != required_provider:
        raise ConformanceError("chain provider selection or transient parameters drifted")
    measurements = details.get("measurements")
    if not isinstance(measurements, list) or [item.get("id") for item in measurements] != [
        "sample-at",
        "minimum",
        "maximum",
        "mean",
        "rms",
        "crossing",
        "rise-time",
        "fall-time",
        "settling-time",
    ]:
        raise ConformanceError("chain manifest must declare the reviewed nine ordinary measurements")

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
    """Bind both the pinned image manifest and config identities."""

    facade = {
        "runtime": {
            "image": {
                "reference": manifest["runtime"]["image_reference"],
            }
        }
    }
    record = _shared.inspect_image(container_engine, facade)
    if record.get("Id") != manifest["runtime"]["image_config_digest"]:
        raise ConformanceError(
            f"local image config digest is {record.get('Id')!r}, "
            f"expected {manifest['runtime']['image_config_digest']!r}"
        )
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    """Require a clean detached checkout and the exact public input/license bytes."""

    if not design_dir.is_dir() or not (design_dir / ".git").exists():
        raise ConformanceError(
            f"pinned design checkout is missing at {design_dir}; run setup.py first"
        )
    revision = manifest["design"]["revision"]
    head = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != revision:
        raise ConformanceError(f"design checkout is at {head}, expected {revision}")
    status = run_checked(
        ["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status:
        raise ConformanceError("design checkout has local changes; use a clean pinned checkout")

    expected = [manifest["design"]["license"], *manifest["design"]["inputs"]]
    for record in expected:
        candidate = design_dir / record["path"]
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"required regular design file is missing: {candidate}")
        observed = sha256_file(candidate)
        if observed != record["sha256"]:
            raise ConformanceError(
                f"design input hash mismatch for {candidate}: "
                f"expected {record['sha256']}, got {observed}"
            )
    return head


__all__ = [
    "CHAIN_ID",
    "ConformanceError",
    "default_cache_dir",
    "ensure_external_cache",
    "ensure_external_design_path",
    "inspect_image",
    "load_manifest",
    "require_mount_safe_path",
    "run_checked",
    "sha256_file",
    "verify_design_checkout",
]
