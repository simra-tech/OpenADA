#!/usr/bin/env python3
"""Replay pinned Xschem-to-ngspice conformance without network access."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any

from common import (
    ConformanceError,
    default_cache_dir,
    ensure_external_cache,
    ensure_external_design_path,
    inspect_image,
    load_manifest,
    require_mount_safe_path,
    run_checked,
    sha256_file,
    verify_design_checkout,
)
from verify import verify_evidence


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MAX_OBSERVATION_BYTES = 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the pinned IHP Xschem/ngspice workflow in one network-disabled container."
    )
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--container-engine", default="docker")
    return parser


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    require_mount_safe_path(source)
    value = f"type=bind,source={source},target={target}"
    return f"{value},readonly" if readonly else value


def _container_command(
    engine: str,
    manifest: dict[str, Any],
    openada_root: Path,
    design_dir: Path,
    evidence: Path,
    name: str,
) -> list[str]:
    image = manifest["runtime"]["image"]
    return [
        engine,
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--platform",
        image["platform"],
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp/openada-home",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PDK_ROOT=/foss/pdks",
        "--env",
        "PDK=ihp-sg13g2",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--workdir",
        "/evidence",
        "--mount",
        _mount(openada_root, "/openada", readonly=True),
        "--mount",
        _mount(design_dir, "/design", readonly=True),
        "--mount",
        _mount(evidence, "/evidence", readonly=False),
        "--entrypoint",
        "/usr/bin/python3",
        image["reference"],
        "/openada/conformance/ihp-inverter-ngspice/inside.py",
        "--manifest",
        "/openada/conformance/ihp-inverter-ngspice/manifest.json",
        "--evidence",
        "/evidence",
    ]


def _cleanup_container(engine: str, name: str) -> str | None:
    try:
        completed = subprocess.run(
            [engine, "rm", "-f", name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if completed.returncode == 0:
        return None
    return (completed.stderr or completed.stdout).strip()[-1_000:]


def _run_container(command: list[str], *, timeout: float, engine: str, name: str) -> tuple[dict, int, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup = _cleanup_container(engine, name)
        suffix = f"; cleanup error: {cleanup}" if cleanup else "; named container removed"
        raise ConformanceError(f"container exceeded {timeout:g} seconds{suffix}") from exc
    except OSError as exc:
        raise ConformanceError(f"cannot execute container engine {command[0]!r}: {exc}") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_OBSERVATION_BYTES:
        raise ConformanceError("container observation exceeded the runner size bound")
    try:
        observation = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"inside runner returned invalid JSON: {exc}; stderr={completed.stderr[-4_000:]!r}"
        ) from exc
    if not isinstance(observation, dict):
        raise ConformanceError("inside runner observation must be a JSON object")
    return observation, completed.returncode, completed.stderr


def _git_state(openada_root: Path) -> dict[str, Any]:
    try:
        commit = run_checked(["git", "-C", str(openada_root), "rev-parse", "HEAD"]).stdout.strip()
        status = run_checked(
            ["git", "-C", str(openada_root), "status", "--porcelain=v1", "--untracked-files=all"]
        ).stdout
    except ConformanceError:
        return {
            "commit": None,
            "tracked_files_modified": None,
            "untracked_files_present": None,
            "working_tree_modified": None,
            "status_entry_count": None,
            "status_sha256": None,
        }
    entries = status.splitlines()
    return {
        "commit": commit,
        "tracked_files_modified": any(not line.startswith("?? ") for line in entries),
        "untracked_files_present": any(line.startswith("?? ") for line in entries),
        "working_tree_modified": bool(entries),
        "status_entry_count": len(entries),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _checkout_record(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    available = before["commit"] is not None and after["commit"] is not None
    unchanged = before == after if available else None
    return {
        "before": before,
        "after": after,
        "state_unchanged": unchanged,
        "commit_exact": bool(
            unchanged
            and before["working_tree_modified"] is False
            and after["working_tree_modified"] is False
        ),
    }


def _validate_evidence_location(
    path: Path | None, protected: dict[str, Path]
) -> Path | None:
    roots = {label: root.expanduser().resolve() for label, root in protected.items()}
    if path is None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        for label, root in roots.items():
            if temp_root == root or root in temp_root.parents:
                raise ConformanceError(
                    f"the system temporary directory is inside the {label}; select --evidence-dir"
                )
        return None
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ConformanceError(f"evidence path may not be a symbolic link: {expanded}")
    evidence = expanded.resolve()
    for label, root in roots.items():
        if evidence == root or root in evidence.parents:
            raise ConformanceError(
                f"the evidence directory must be outside the {label}; do not contaminate pinned inputs"
            )
    if expanded.exists():
        raise ConformanceError(f"evidence path already exists; choose a fresh path: {evidence}")
    return evidence


def _create_evidence(path: Path | None) -> Path:
    try:
        if path is None:
            return Path(tempfile.mkdtemp(prefix="openada-ihp-ngspice-")).resolve()
        path.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise ConformanceError(f"cannot create fresh evidence directory: {exc}") from exc
    return path


def _write_run_metadata(
    evidence: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    image_record: dict[str, Any],
    checkout_before: dict[str, Any],
    checkout_after: dict[str, Any],
    container_command: list[str],
    observation: dict[str, Any],
) -> None:
    document = {
        "schema": "openada.ngspice-conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "conformance_manifest_sha256": sha256_file(manifest_path),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "design_revision": manifest["design"]["revision"],
        "image": {
            "reference": manifest["runtime"]["image"]["reference"],
            "id": image_record.get("Id"),
            "os": image_record.get("Os"),
            "architecture": image_record.get("Architecture"),
        },
        "openada_checkout": _checkout_record(checkout_before, checkout_after),
        "network": "none during EDA execution",
        "container_command": container_command,
        "runtime_observation": observation,
    }
    temporary = evidence / "run.json.tmp"
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(evidence / "run.json")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence: Path | None = None
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        cache_dir = args.cache_dir.expanduser().resolve()
        openada_root = args.openada_root.expanduser().resolve()
        ensure_external_cache(cache_dir, openada_root)
        design_dir = ensure_external_design_path(
            cache_dir / "IHP-AnalogAcademy", openada_root, cache_dir
        )
        requested = _validate_evidence_location(
            args.evidence_dir,
            {
                "OpenADA checkout": openada_root,
                "conformance cache": cache_dir,
                "pinned design checkout": design_dir,
            },
        )
        if not (openada_root / "bin" / "openada").is_file():
            raise ConformanceError(f"OpenADA source checkout is missing bin/openada: {openada_root}")
        verify_design_checkout(design_dir, manifest)
        image_record = inspect_image(args.container_engine, manifest)
        require_mount_safe_path(openada_root)
        require_mount_safe_path(design_dir)
        checkout_before = _git_state(openada_root)
        evidence = _create_evidence(requested)
        require_mount_safe_path(evidence)

        name = f"openada-ihp-ngspice-{os.getpid()}-{secrets.token_hex(4)}"
        command = _container_command(
            args.container_engine, manifest, openada_root, design_dir, evidence, name
        )
        observation, returncode, stderr = _run_container(
            command,
            timeout=manifest["workflow"]["simulate"]["container_timeout_seconds"],
            engine=args.container_engine,
            name=name,
        )
        verify_design_checkout(design_dir, manifest)
        checkout_after = _git_state(openada_root)
        _write_run_metadata(
            evidence,
            manifest_path,
            manifest,
            image_record,
            checkout_before,
            checkout_after,
            command,
            observation,
        )
        if returncode != 0:
            raise ConformanceError(
                f"inside runner exited with code {returncode}: {stderr[-4_000:]!r}"
            )
        verify_evidence(manifest, evidence, manifest_sha256=sha256_file(manifest_path))
    except ConformanceError as exc:
        print(f"conformance run failed: {exc}", file=sys.stderr)
        if evidence is not None:
            print(f"incomplete evidence retained at: {evidence}", file=sys.stderr)
        return 1
    print(f"Conformance verified. Evidence: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
