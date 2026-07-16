"""Closed identities and helpers for the public SPICE portability chain."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
SHARED_COMMON = HERE.parent / "ihp-inverter" / "common.py"
CHAIN_ID = "openada.chain/public-spice-portability/v1"
IMAGE_REFERENCE = (
    "hpretl/iic-osic-tools@sha256:"
    "fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
IHP_REPOSITORY = "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git"
IHP_REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
XYCE_REPOSITORY = "https://github.com/Xyce/Xyce_Regression.git"
XYCE_TAG = "Release-7.10.0"
XYCE_TAG_OBJECT = "2a339ec3845af0aef99a7e6cc488a41acf64f6ed"
XYCE_REVISION = "d6e278e371ec2f3df1325dcff4552e585bc7ecc1"
PDK_REVISION = "144f811cdffda49b71d28f64e8a92b697b61cf06"


def _load_shared() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_openada_public_portability_shared", SHARED_COMMON
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shared helpers: {SHARED_COMMON}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()
ConformanceError = _shared.ConformanceError
ensure_external_cache = _shared.ensure_external_cache
require_mount_safe_path = _shared.require_mount_safe_path
run_checked = _shared.run_checked
sha256_file = _shared.sha256_file


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    reference = manifest["runtime"]["image_reference"]
    completed = run_checked([container_engine, "image", "inspect", reference])
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(f"container image inspection was invalid JSON: {exc}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ConformanceError("container image inspection returned an unexpected document")
    record = records[0]
    if record.get("Os") != "linux" or record.get("Architecture") != "amd64":
        raise ConformanceError("local image platform differs from linux/amd64")
    if record.get("Id") != IMAGE_CONFIG_DIGEST:
        raise ConformanceError("local image configuration digest differs from the pin")
    digests = record.get("RepoDigests")
    if not isinstance(digests, list) or reference not in digests:
        raise ConformanceError("local image does not record the pinned manifest digest")
    return record


FEATURE_OP = (
    "feature|openada.operation/circuit.simulate/v1alpha2|"
    "openada.feature/simulation.analysis.op/v1alpha1"
)
FEATURE_DC = (
    "feature|openada.operation/circuit.simulate/v1alpha2|"
    "openada.feature/simulation.analysis.dc/v1alpha1"
)
FEATURE_AC = (
    "feature|openada.operation/circuit.simulate/v1alpha2|"
    "openada.feature/simulation.analysis.ac/v1alpha1"
)


def simulation_mapping(driver: str, product: str, analysis: str) -> str:
    return (
        "native-mapping|openada.operation/circuit.simulate/v1alpha2|"
        f"{driver}|{product}|analysis:{analysis}"
    )


def extraction_mapping(native_format: str, analysis: str) -> str:
    return (
        "native-mapping|openada.operation/result.series.extract/v1alpha1|"
        f"org.openada.kernel.spice3-series|{native_format}|analysis:{analysis}"
    )


def provider_row(driver: str, feature: str) -> str:
    return (
        f"provider|{driver}|openada.operation/circuit.simulate/v1alpha2|{feature}"
    )


EXPECTED_ROWS = (
    FEATURE_OP,
    FEATURE_DC,
    FEATURE_AC,
    simulation_mapping("org.openada.driver.ngspice", "org.ngspice.simulator", "op"),
    simulation_mapping("org.openada.driver.ngspice", "org.ngspice.simulator", "dc"),
    simulation_mapping("org.openada.driver.ngspice", "org.ngspice.simulator", "ac"),
    simulation_mapping("org.openada.driver.xyce", "gov.sandia.xyce", "dc"),
    simulation_mapping("org.openada.driver.xyce", "gov.sandia.xyce", "ac"),
    simulation_mapping("org.openada.driver.xyce", "gov.sandia.xyce", "tran"),
    extraction_mapping("org.openada.format.ngspice-raw", "op"),
    extraction_mapping("org.openada.format.ngspice-raw", "dc"),
    extraction_mapping("org.openada.format.ngspice-raw", "ac"),
    extraction_mapping("org.openada.format.xyce-raw", "dc"),
    extraction_mapping("org.openada.format.xyce-raw", "ac"),
    extraction_mapping("org.openada.format.xyce-raw", "tran"),
    provider_row("org.openada.driver.ngspice", "openada.feature/simulation.analysis.op/v1alpha1"),
    provider_row("org.openada.driver.ngspice", "openada.feature/simulation.analysis.dc/v1alpha1"),
    provider_row("org.openada.driver.ngspice", "openada.feature/simulation.analysis.ac/v1alpha1"),
    provider_row("org.openada.driver.xyce", "openada.feature/simulation.analysis.dc/v1alpha1"),
    provider_row("org.openada.driver.xyce", "openada.feature/simulation.analysis.ac/v1alpha1"),
    provider_row("org.openada.driver.xyce", "openada.feature/simulation.analysis.tran/v1alpha1"),
    "surface-variant|openada.surface/cli.simulate/v1|legacy-ngspice",
    "surface-variant|openada.surface/cli.simulate/v1|shared-xyce",
    "surface|openada.surface/cli.capabilities/v1",
    "surface|openada.surface/cli.doctor/v1",
    "surface|openada.surface/cli.profile-list/v1",
    "surface|openada.surface/cli.profile-show/v1",
    "surface|openada.surface/cli.provider-list/v1",
    "surface|openada.surface/cli.provider-validate/v1",
)


def default_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "openada" / "conformance" / "public-spice-portability"


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ConformanceError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"JSON root is not an object: {path}")
    return document


def load_requests(path: Path | None = None) -> dict[str, Any]:
    document = _read_json(path or HERE / "requests.json")
    if set(document) != {
        "schema", "chain_id", "simulations", "legacy_simulation",
        "admin_commands", "negative_commands", "extensions",
    }:
        raise ConformanceError("portability requests have an unexpected top-level shape")
    if document["schema"] != "openada.public-spice-portability-requests/v0alpha1":
        raise ConformanceError("unsupported portability request schema")
    if document["chain_id"] != CHAIN_ID:
        raise ConformanceError("request chain identity drifted")
    expected_simulations = [
        "ngspice-op", "ngspice-dc", "ngspice-ac",
        "xyce-dc", "xyce-ac", "xyce-tran",
    ]
    if [item.get("id") for item in document["simulations"]] != expected_simulations:
        raise ConformanceError("simulation request order or identities drifted")
    if [item.get("id") for item in document["admin_commands"]] != [
        "capabilities", "doctor", "profile-list", "profile-show",
        "provider-list", "provider-validate",
    ]:
        raise ConformanceError("admin request order or identities drifted")
    if [item.get("id") for item in document["negative_commands"]] != [
        "xyce-ac-presentation-rejected", "xyce-op-unsupported",
        "ngspice-analysis-mismatch", "extract-missing-selector",
        "admin-unknown-profile", "admin-invalid-provider",
    ]:
        raise ConformanceError("negative request order or identities drifted")
    return document


def load_manifest(path: Path) -> dict[str, Any]:
    document = _read_json(path)
    schema_path = REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json"
    schema = _read_json(schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ConformanceError(
            f"portability manifest violates its schema at {location}: {error.message}"
        )
    if document["id"] != CHAIN_ID:
        raise ConformanceError("portability chain identity drifted")
    if tuple(document["covers"]) != EXPECTED_ROWS:
        raise ConformanceError("manifest must cover exactly the reviewed 29 rows in order")
    semantic_steps = [
        step for step in document["steps"] if step["kind"] == "semantic-command"
    ]
    exercised = {
        row for step in semantic_steps for row in step.get("covers", [])
    }
    if exercised != set(EXPECTED_ROWS):
        raise ConformanceError("positive semantic steps do not close the exact manifest surface")
    design = document["design"]
    if (
        design["repository"] != XYCE_REPOSITORY
        or design["revision"] != XYCE_REVISION
        or design["class"] != "public-design"
    ):
        raise ConformanceError("primary Xyce public design identity drifted")
    secondary = design["extensions"]["org.openada"]["secondary_design"]
    if secondary["repository"] != IHP_REPOSITORY or secondary["revision"] != IHP_REVISION:
        raise ConformanceError("secondary IHP public design identity drifted")
    runtime = document["runtime"]
    if (
        runtime["image_reference"] != IMAGE_REFERENCE
        or runtime["image_config_digest"] != IMAGE_CONFIG_DIGEST
        or runtime["platform"] != "linux/amd64"
        or runtime["pdk_revision"] != PDK_REVISION
    ):
        raise ConformanceError("runtime identity drifted")
    for contract in document["contracts"]:
        candidate = REPOSITORY_ROOT / contract["repository_path"]
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"declared contract is unavailable: {candidate}")
        if sha256_file(candidate) != contract["sha256"]:
            raise ConformanceError(f"contract hash drift: {contract['repository_path']}")
    load_requests()
    return document


def _git_output(checkout: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ConformanceError(
            f"git {' '.join(arguments)} failed for {checkout}: {completed.stderr[-1000:]!r}"
        )
    return completed.stdout.strip()


def _verify_clean_checkout(
    checkout: Path,
    *,
    revision: str,
    files: list[dict[str, str]],
) -> None:
    if not checkout.is_dir() or checkout.is_symlink():
        raise ConformanceError(f"checkout is unavailable or unsafe: {checkout}")
    if _git_output(checkout, "rev-parse", "HEAD") != revision:
        raise ConformanceError(f"checkout revision differs from pin: {checkout}")
    if _git_output(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ConformanceError(f"checkout is not clean: {checkout}")
    for record in files:
        path = checkout / record["path"]
        if not path.is_file() or path.is_symlink() or sha256_file(path) != record["sha256"]:
            raise ConformanceError(f"pinned checkout file drift: {record['path']}")


def verify_xyce_checkout(checkout: Path, manifest: dict[str, Any]) -> None:
    design = manifest["design"]
    files = [*design["inputs"], {"path": design["license"]["path"], "sha256": design["license"]["sha256"]}]
    _verify_clean_checkout(checkout, revision=XYCE_REVISION, files=files)
    if _git_output(checkout, "rev-parse", f"refs/tags/{XYCE_TAG}") != XYCE_TAG_OBJECT:
        raise ConformanceError("Xyce annotated tag object differs from the reviewed pin")
    if _git_output(checkout, "rev-parse", f"refs/tags/{XYCE_TAG}^{{}}") != XYCE_REVISION:
        raise ConformanceError("Xyce tag no longer peels to the reviewed revision")


def verify_ihp_checkout(checkout: Path, manifest: dict[str, Any]) -> None:
    secondary = manifest["design"]["extensions"]["org.openada"]["secondary_design"]
    files = [*secondary["inputs"], {"path": secondary["license"]["path"], "sha256": secondary["license"]["sha256"]}]
    _verify_clean_checkout(checkout, revision=IHP_REVISION, files=files)


def cache_checkouts(cache_dir: Path) -> tuple[Path, Path]:
    return cache_dir / "Xyce_Regression", cache_dir / "IHP-AnalogAcademy"


def ensure_checkout_path(path: Path, repository_root: Path, cache_dir: Path) -> Path:
    resolved = path.expanduser().resolve()
    for forbidden in (repository_root.resolve(), cache_dir.resolve()):
        if resolved == forbidden:
            raise ConformanceError(f"checkout must be below, not equal to, {forbidden}")
    if repository_root.resolve() in resolved.parents:
        raise ConformanceError("public source checkout must stay outside the OpenADA repository")
    if cache_dir.resolve() not in resolved.parents:
        raise ConformanceError("public source checkout must stay inside the selected cache")
    return resolved


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
