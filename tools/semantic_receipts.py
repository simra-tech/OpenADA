"""Shared, fail-closed helpers for semantic-chain publication receipts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Mapping


class SemanticReceiptError(RuntimeError):
    """A source or public-design attestation could not be established."""


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SemanticReceiptError(f"cannot inspect Git checkout {root}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SemanticReceiptError(
            f"Git inspection failed in {root}: {detail[-2000:]}"
        )
    return completed.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def provider_manifest_semantic_sha256_bytes(encoded: bytes) -> str:
    """Hash provider semantics while detaching only the receipt back-reference."""

    try:
        manifest = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticReceiptError(f"cannot parse provider manifest semantics: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SemanticReceiptError("provider manifest semantic root must be an object")
    records = manifest.get("conformance_records", [])
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            evidence = record.get("evidence")
            if isinstance(evidence, dict) and "sha256" in evidence:
                evidence["sha256"] = "@detached-semantic-chain-run-sha256@"
    return canonical_sha256(manifest)


def semantic_subject_relative_paths(root: Path, catalog_path: Path) -> set[str]:
    """Return the implementation-owned file set bound by every chain receipt."""

    root = root.resolve()
    catalog_path = catalog_path.resolve()
    paths: set[Path] = set()
    paths.update((root / "src" / "openada").rglob("*.py"))
    paths.update((root / "profiles").glob("*.json"))
    paths.update((root / "schemas").glob("*.json"))
    paths.update((root / "providers").rglob("*.json"))
    paths.update((root / "bin").glob("openada*"))
    paths.update((root / "tools").glob("semantic_*.py"))
    for required in (
        root / "tools" / "verify_semantic_coverage.py",
        root / "pyproject.toml",
    ):
        if required.is_file():
            paths.add(required.resolve())
    try:
        catalog_path.relative_to(root)
    except ValueError:
        pass
    else:
        paths.add(catalog_path)

    relative_paths: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            relative_paths.add(path.resolve().relative_to(root).as_posix())
        except ValueError as exc:
            raise SemanticReceiptError(
                f"semantic subject path is outside repository root: {path}"
            ) from exc
    return relative_paths


def semantic_subject(root: Path, catalog_path: Path) -> str:
    """Hash the exact runtime, contracts, catalog, launchers, and release gate."""

    root = root.resolve()
    catalog_path = catalog_path.resolve()
    entries: list[dict[str, Any]] = []
    for relative in sorted(semantic_subject_relative_paths(root, catalog_path)):
        path = root / relative
        provider_manifest = (
            path.name == "driver-manifest.json"
            and (root / "providers") in path.parents
        )
        entry: dict[str, Any] = {
            "path": relative,
            "sha256": (
                provider_manifest_semantic_sha256_bytes(path.read_bytes())
                if provider_manifest
                else sha256_file(path)
            ),
            "digest_policy": (
                "provider-semantics-detached-run-digest"
                if provider_manifest
                else "exact-bytes"
            ),
        }
        if path.parent == root / "bin" and path.name.startswith("openada"):
            entry["executable_mode"] = stat.S_IMODE(path.stat().st_mode)
        entries.append(entry)
    try:
        catalog_path.relative_to(root)
    except ValueError:
        entries.append(
            {
                "path": str(catalog_path),
                "sha256": sha256_file(catalog_path),
                "digest_policy": "exact-bytes",
            }
        )
    return canonical_sha256(entries)


def git_state(root: Path) -> dict[str, Any]:
    root = root.resolve()
    revision = _git(root, "rev-parse", "HEAD").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "repository_revision": revision,
        "repository_tree": tree,
        "clean": status == "",
        "status_entry_count": len(status.splitlines()),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def source_attestation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    semantic_subject_sha256: str,
    receipt_class: str,
) -> dict[str, Any]:
    if receipt_class not in {"provisional", "release"}:
        raise SemanticReceiptError(f"unsupported receipt class {receipt_class!r}")
    identity_unchanged = (
        before.get("repository_revision") == after.get("repository_revision")
        and before.get("repository_tree") == after.get("repository_tree")
        and before.get("status_sha256") == after.get("status_sha256")
    )
    if not identity_unchanged:
        raise SemanticReceiptError("semantic source changed during replay")
    clean_before = before.get("clean") is True
    clean_after = after.get("clean") is True
    if receipt_class == "release" and not (clean_before and clean_after):
        raise SemanticReceiptError(
            "release receipts require an unchanged clean checkout before and after replay"
        )
    return {
        "receipt_class": receipt_class,
        "repository_revision": str(before["repository_revision"]),
        "repository_tree": str(before["repository_tree"]),
        "semantic_subject_sha256": semantic_subject_sha256,
        "clean_before": clean_before,
        "clean_after": clean_after,
        "state_unchanged": True,
        "extensions": {},
    }


def _normalize_remote(value: str) -> str:
    normalized = value.strip().removesuffix("/").removesuffix(".git")
    if normalized.startswith("git@github.com:"):
        normalized = "https://github.com/" + normalized.removeprefix("git@github.com:")
    return normalized


def design_provenance(
    checkout: Path,
    design: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a clean public Git checkout and exact input bytes to a manifest."""

    checkout = checkout.resolve()
    state = git_state(checkout)
    if not state["clean"]:
        raise SemanticReceiptError("public design checkout has local changes")
    if state["repository_revision"] != design["revision"]:
        raise SemanticReceiptError("public design revision differs from manifest")
    if state["repository_tree"] != design["tree"]:
        raise SemanticReceiptError("public design tree differs from manifest")
    remote = _git(checkout, "remote", "get-url", "origin").strip()
    if _normalize_remote(remote) != _normalize_remote(str(design["repository"])):
        raise SemanticReceiptError("public design origin differs from manifest")

    def source_record(record: Mapping[str, Any]) -> dict[str, Any]:
        candidate = checkout / str(record["path"])
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise SemanticReceiptError(
                f"cannot stat public design input {record['path']}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or candidate.is_symlink()
        ):
            raise SemanticReceiptError(
                f"public design input is not a regular single-link file: {record['path']}"
            )
        observed = sha256_file(candidate)
        if observed != record["sha256"]:
            raise SemanticReceiptError(
                f"public design input digest differs: {record['path']}"
            )
        return {
            "path": str(record["path"]),
            "bytes": metadata.st_size,
            "sha256": observed,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.design-provenance/v0alpha1",
        "repository": design["repository"],
        "revision": design["revision"],
        "tree": design["tree"],
        "checkout_clean": True,
        "remote_url_verified": True,
        "license": source_record(design["license"]),
        "inputs": [source_record(record) for record in design["inputs"]],
        "extensions": {},
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_text(path: Path, encoded: str) -> None:
    """Replace one file without following a predictable staging symlink."""

    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except FileExistsError as exc:
        raise SemanticReceiptError(
            f"refusing to reuse semantic staging path: {temporary}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "SemanticReceiptError",
    "atomic_write_text",
    "canonical_sha256",
    "design_provenance",
    "git_state",
    "provider_manifest_semantic_sha256_bytes",
    "semantic_subject",
    "semantic_subject_relative_paths",
    "sha256_file",
    "source_attestation",
    "write_json",
]
