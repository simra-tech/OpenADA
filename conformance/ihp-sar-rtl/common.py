"""Shared validation helpers for the pinned IHP SAR RTL conformance chain."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable


MANIFEST_SCHEMA = "openada.conformance/v0alpha1"
RESULT_SCHEMA = "openada.result/v0alpha1"
DESIGN_REPOSITORY = "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git"
DESIGN_REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
SOURCE_REPOSITORY_PATH = (
    "modules/module_3_8_bit_SAR_ADC/part_2_digital_comps/algorithm/verilog/sar_logic.v"
)
SOURCE_PATH = f"/design/{SOURCE_REPOSITORY_PATH}"
SOURCE_SHA256 = "b33c7b25215ac916b3b07e0dc385ae353294f6872eaa226f4c0126ecfd7063da"
SOURCE_BYTES = 576
IMAGE_REFERENCE = (
    "hpretl/iic-osic-tools@"
    "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
WRAPPER_PATH = "/openada/conformance/ihp-sar-rtl/yosys_wrapper.py"
NATIVE_YOSYS_PATH = "/foss/tools/yosys/bin/yosys"
YOSYS_VERSION = (
    "Yosys 0.66 (git sha1 86f2ddebc-dirty, g++ 13.3.0-6ubuntu2~24.04.1 "
    "-fPIC -O3)"
)


class ConformanceError(RuntimeError):
    """A deterministic conformance precondition or assertion failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read conformance manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError("conformance manifest root must be an object")
    validate_manifest(document)
    return document


def _expect(actual: Any, expected: Any, location: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{location}: expected {expected!r}, got {actual!r}")


def validate_manifest(manifest: dict[str, Any]) -> None:
    errors: list[str] = []
    _expect(manifest.get("schema"), MANIFEST_SCHEMA, "schema", errors)
    _expect(manifest.get("id"), "ihp-sar-rtl-check", "id", errors)
    design = manifest.get("design") if isinstance(manifest.get("design"), dict) else {}
    _expect(design.get("repository"), DESIGN_REPOSITORY, "design.repository", errors)
    _expect(design.get("revision"), DESIGN_REVISION, "design.revision", errors)
    _expect(
        design.get("license"),
        {"spdx": "Apache-2.0", "path": "LICENSE", "sha256": LICENSE_SHA256},
        "design.license",
        errors,
    )
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    image = runtime.get("image") if isinstance(runtime.get("image"), dict) else {}
    _expect(image.get("reference"), IMAGE_REFERENCE, "runtime.image.reference", errors)
    _expect(image.get("config_digest"), IMAGE_CONFIG_DIGEST, "runtime.image.config_digest", errors)
    _expect(image.get("platform"), "linux/amd64", "runtime.image.platform", errors)
    tool = runtime.get("tool") if isinstance(runtime.get("tool"), dict) else {}
    _expect(tool.get("name"), "yosys", "runtime.tool.name", errors)
    _expect(tool.get("wrapper_path"), WRAPPER_PATH, "runtime.tool.wrapper_path", errors)
    _expect(tool.get("native_path"), NATIVE_YOSYS_PATH, "runtime.tool.native_path", errors)
    _expect(tool.get("version"), YOSYS_VERSION, "runtime.tool.version", errors)
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    _expect(policy.get("eda_network"), "none", "policy.eda_network", errors)
    _expect(policy.get("openada_mount"), "read-only", "policy.openada_mount", errors)
    _expect(policy.get("design_mount"), "read-only", "policy.design_mount", errors)
    _expect(policy.get("evidence_directory"), "new and writable", "policy.evidence_directory", errors)
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    _expect(source.get("path"), SOURCE_PATH, "source.path", errors)
    _expect(source.get("repository_path"), SOURCE_REPOSITORY_PATH, "source.repository_path", errors)
    _expect(source.get("bytes"), SOURCE_BYTES, "source.bytes", errors)
    _expect(source.get("sha256"), SOURCE_SHA256, "source.sha256", errors)
    operations = manifest.get("operations") if isinstance(manifest.get("operations"), dict) else {}
    if set(operations) != {"rtl_check", "missing_top"}:
        errors.append("operations must contain exactly rtl_check and missing_top")
    expected = {
        "rtl_check": {
            "top": "sar_logic",
            "directory": "/evidence/positive",
            "json": "/evidence/positive/sar_logic.json",
            "result": "positive/rtl-check.result.json",
            "script": "/evidence/positive/rtl-check.ys",
            "transcript": "/evidence/positive/yosys.transcript.json",
        },
        "missing_top": {
            "top": "missing_sar_logic",
            "directory": "/evidence/negative",
            "json": "/evidence/negative/missing_sar_logic.json",
            "result": "negative/rtl-check.result.json",
            "script": "/evidence/negative/rtl-check.ys",
            "transcript": "/evidence/negative/yosys.transcript.json",
        },
    }
    for name, pins in expected.items():
        operation = operations.get(name) if isinstance(operations.get(name), dict) else {}
        _expect(operation.get("top"), pins["top"], f"operations.{name}.top", errors)
        _expect(operation.get("output_directory"), pins["directory"], f"operations.{name}.output_directory", errors)
        _expect(operation.get("json_netlist"), pins["json"], f"operations.{name}.json_netlist", errors)
        _expect(operation.get("result_filename"), pins["result"], f"operations.{name}.result_filename", errors)
        script = operation.get("script") if isinstance(operation.get("script"), dict) else {}
        transcript = operation.get("transcript") if isinstance(operation.get("transcript"), dict) else {}
        _expect(script.get("path"), pins["script"], f"operations.{name}.script.path", errors)
        _expect(transcript.get("path"), pins["transcript"], f"operations.{name}.transcript.path", errors)
        _expect(operation.get("container_timeout_seconds"), 180, f"operations.{name}.container_timeout_seconds", errors)
        _expect(operation.get("tool_timeout_seconds"), 120, f"operations.{name}.tool_timeout_seconds", errors)
    if errors:
        raise ConformanceError("invalid conformance manifest:\n- " + "\n- ".join(errors))


def default_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "openada" / "conformance" / "ihp-inverter"


def run_checked(argv: Iterable[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ConformanceError(f"cannot execute {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ConformanceError(
            f"command failed with exit code {completed.returncode}: {command!r}"
            + (f": {detail[-4000:]}" if detail else "")
        )
    return completed


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    reference = manifest["runtime"]["image"]["reference"]
    completed = run_checked([container_engine, "image", "inspect", reference])
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(f"container image inspection was not valid JSON: {exc}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ConformanceError("container image inspection returned an unexpected document")
    record = records[0]
    if record.get("Os") != "linux" or record.get("Architecture") != "amd64":
        raise ConformanceError("local image platform is not the pinned linux/amd64 platform")
    if record.get("Id") != IMAGE_CONFIG_DIGEST:
        raise ConformanceError(
            f"local image config digest is {record.get('Id')!r}, expected {IMAGE_CONFIG_DIGEST!r}"
        )
    if reference not in (record.get("RepoDigests") or []):
        raise ConformanceError(f"local image does not record the required digest: {reference}")
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    if not design_dir.is_dir() or not (design_dir / ".git").exists():
        raise ConformanceError(f"pinned design checkout is missing at {design_dir}")
    head = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != DESIGN_REVISION:
        raise ConformanceError(f"design checkout is at {head}, expected {DESIGN_REVISION}")
    if run_checked(["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]).stdout:
        raise ConformanceError("design checkout has local changes; use a clean pinned checkout")
    records = [manifest["design"]["license"], manifest["source"]]
    for record in records:
        relative = record.get("repository_path") or record["path"]
        candidate = design_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"required regular design file is missing: {candidate}")
        if sha256_file(candidate) != record["sha256"]:
            raise ConformanceError(f"design input hash mismatch for {candidate}")
        if "bytes" in record and candidate.stat().st_size != record["bytes"]:
            raise ConformanceError(f"design input byte count mismatch for {candidate}")
    return head


def ensure_external_cache(cache_dir: Path, repository_root: Path) -> None:
    cache = cache_dir.expanduser().resolve()
    root = repository_root.resolve()
    if cache == root or root in cache.parents:
        raise ConformanceError("the conformance cache must be outside the OpenADA checkout")


def ensure_external_design_path(design_dir: Path, repository_root: Path, cache_dir: Path) -> Path:
    if design_dir.is_symlink():
        raise ConformanceError("the pinned design checkout path may not be a symbolic link")
    design = design_dir.expanduser().resolve()
    root = repository_root.resolve()
    cache = cache_dir.expanduser().resolve()
    if design == root or root in design.parents or design.parent != cache:
        raise ConformanceError("the pinned design checkout must be the cache's IHP-AnalogAcademy child")
    return design


def require_mount_safe_path(path: Path) -> None:
    if "," in str(path):
        raise ConformanceError(f"container bind-mount source paths may not contain commas: {path}")
