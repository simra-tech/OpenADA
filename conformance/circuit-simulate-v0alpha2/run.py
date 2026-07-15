#!/usr/bin/env python3
"""Replay the pinned native ngspice/Xyce portability proof without networking."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Any

from verify import ConformanceError, load_manifest, verify_evidence


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = HERE / "manifest.json"
MAX_CONTAINER_OUTPUT_BYTES = 5 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_checked(
    argv: list[str],
    *,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except OSError as exc:
        raise ConformanceError(f"cannot execute {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConformanceError(f"command exceeded {timeout:g} seconds: {argv!r}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-4_000:]
        raise ConformanceError(
            f"command failed with exit {completed.returncode}: {argv!r}; {detail}"
        )
    return completed


def _inspect_image(engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    image = manifest["runtime"]["image"]
    completed = _run_checked([engine, "image", "inspect", image["reference"]])
    try:
        documents = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(f"container image inspection returned invalid JSON: {exc}") from exc
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
        raise ConformanceError("container image inspection must return exactly one object")
    record = documents[0]
    if record.get("Id") != image["config_digest"]:
        raise ConformanceError(
            f"local image ID {record.get('Id')!r} does not match {image['config_digest']!r}"
        )
    if image["reference"] not in record.get("RepoDigests", []):
        raise ConformanceError("local image does not advertise the pinned repository digest")
    if record.get("Os") != "linux" or record.get("Architecture") != "amd64":
        raise ConformanceError("local image is not the pinned linux/amd64 runtime")
    return record


def _safe_mount_source(path: Path) -> str:
    value = str(path.resolve())
    if not value.startswith("/") or any(character in value for character in (",", "\n", "\r", "\0")):
        raise ConformanceError(f"path cannot be represented as a bind mount: {value!r}")
    return value


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    value = f"type=bind,source={_safe_mount_source(source)},target={target}"
    return f"{value},readonly" if readonly else value


def _container_command(
    engine: str,
    manifest: dict[str, Any],
    openada_root: Path,
    evidence: Path,
    backend: str,
    container_name: str,
    case_name: str | None = None,
) -> list[str]:
    image = manifest["runtime"]["image"]
    operation = manifest["operation"]
    if case_name is None:
        fixture = manifest["fixture"]
        backend_specification = manifest["backends"][backend]
        analysis_arguments: list[str] = []
    else:
        case = manifest["capability_cases"][case_name]
        fixture = case["fixture"]
        backend_specification = case["backends"][backend]
        analysis = case["parameters"]["analysis"]
        analysis_arguments = ["--analysis", analysis["type"]]
        if analysis["type"] == "dc":
            analysis_arguments.extend(
                [
                    "--source-name",
                    analysis["source_name"],
                    "--source-unit",
                    analysis["source_unit"],
                    "--start",
                    str(analysis["start"]),
                    "--stop",
                    str(analysis["stop"]),
                    "--step",
                    str(analysis["step"]),
                ]
            )
        elif analysis["type"] == "ac":
            analysis_arguments.extend(
                [
                    "--sweep",
                    analysis["sweep"],
                    "--points",
                    str(analysis["points"]),
                    "--start-hz",
                    str(analysis["start_hz"]),
                    "--stop-hz",
                    str(analysis["stop_hz"]),
                ]
            )
    return [
        engine,
        "run",
        "--rm",
        "--name",
        container_name,
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
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--workdir",
        str(Path(fixture["container_path"]).parent),
        "--mount",
        _mount(openada_root, "/openada", readonly=True),
        "--mount",
        _mount(evidence, "/evidence", readonly=False),
        "--entrypoint",
        "/usr/bin/python3",
        image["reference"],
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
        "simulate",
        fixture["container_path"],
        "--backend",
        backend,
        *analysis_arguments,
        "--output-dir",
        backend_specification["output_directory"],
        "--timeout",
        str(operation["timeout_seconds"]),
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


def _run_backend(
    command: list[str],
    *,
    engine: str,
    container_name: str,
    timeout: float,
) -> dict[str, Any]:
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
        cleanup = _cleanup_container(engine, container_name)
        suffix = f"; cleanup failed: {cleanup}" if cleanup else "; named container removed"
        raise ConformanceError(f"container exceeded {timeout:g} seconds{suffix}") from exc
    except OSError as exc:
        raise ConformanceError(f"cannot execute container engine {engine!r}: {exc}") from exc
    stdout_bytes = completed.stdout.encode("utf-8", errors="replace")
    stderr_bytes = completed.stderr.encode("utf-8", errors="replace")
    if len(stdout_bytes) > MAX_CONTAINER_OUTPUT_BYTES or len(stderr_bytes) > MAX_CONTAINER_OUTPUT_BYTES:
        raise ConformanceError("container output exceeds the runner evidence bound")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"container returned invalid result JSON: {exc}; stderr={completed.stderr[-4_000:]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise ConformanceError("OpenADA container result must be a JSON object")
    if completed.returncode != 0:
        raise ConformanceError(
            f"OpenADA container exited {completed.returncode}: {completed.stderr[-4_000:]!r}"
        )
    return result


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _git_state(openada_root: Path) -> dict[str, Any]:
    try:
        commit = _run_checked(["git", "-C", str(openada_root), "rev-parse", "HEAD"]).stdout.strip()
        status = _run_checked(
            ["git", "-C", str(openada_root), "status", "--porcelain=v1", "--untracked-files=all"]
        ).stdout
    except ConformanceError:
        return {"commit": None, "working_tree_modified": None, "status_sha256": None}
    return {
        "commit": commit,
        "working_tree_modified": bool(status),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _validate_evidence_location(path: Path | None, openada_root: Path) -> Path | None:
    if path is None:
        return None
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ConformanceError(f"evidence path may not be a symbolic link: {expanded}")
    evidence = expanded.resolve()
    root = openada_root.resolve()
    if evidence == root or root in evidence.parents:
        raise ConformanceError("evidence must be outside the OpenADA checkout")
    if evidence.exists():
        raise ConformanceError(f"evidence path already exists; choose a fresh path: {evidence}")
    return evidence


def _create_evidence(path: Path | None) -> Path:
    try:
        if path is None:
            return Path(tempfile.mkdtemp(prefix="openada-circuit-simulate-")).resolve()
        path.mkdir(parents=True, mode=0o700)
        return path.resolve()
    except OSError as exc:
        raise ConformanceError(f"cannot create evidence directory: {exc}") from exc


def replay(
    *,
    manifest_path: Path,
    openada_root: Path,
    evidence_path: Path | None,
    container_engine: str,
) -> tuple[Path, dict[str, Any]]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    openada_root = openada_root.expanduser().resolve()
    if not (openada_root / "bin" / "openada").is_file():
        raise ConformanceError(f"OpenADA entry point is missing under {openada_root}")
    fixture = openada_root / manifest["fixture"]["repository_path"]
    if not fixture.is_file() or _sha256(fixture) != manifest["fixture"]["sha256"]:
        raise ConformanceError("selected checkout does not contain the pinned SPICE fixture")
    image_record = _inspect_image(container_engine, manifest)
    requested_evidence = _validate_evidence_location(evidence_path, openada_root)
    evidence = _create_evidence(requested_evidence)
    for backend in manifest["backends"]:
        (evidence / backend).mkdir(mode=0o700)
    for case in manifest["capability_cases"].values():
        for specification in case["backends"].values():
            output_directory = specification["output_directory"]
            if not output_directory.startswith("/evidence/"):
                raise ConformanceError("capability output directory escapes /evidence")
            (evidence / output_directory.removeprefix("/evidence/")).mkdir(
                parents=True,
                mode=0o700,
                exist_ok=True,
            )

    commands: dict[str, list[str]] = {}
    try:
        for backend in ("ngspice", "xyce"):
            name = f"openada-circuit-{backend}-{os.getpid()}-{secrets.token_hex(4)}"
            if re.fullmatch(r"openada-circuit-(?:ngspice|xyce)-[1-9][0-9]*-[0-9a-f]{8}", name) is None:
                raise ConformanceError("generated container name is malformed")
            command = _container_command(
                container_engine,
                manifest,
                openada_root,
                evidence,
                backend,
                name,
            )
            commands[backend] = command
            result = _run_backend(
                command,
                engine=container_engine,
                container_name=name,
                timeout=manifest["operation"]["container_timeout_seconds"],
            )
            _write_json(evidence / manifest["backends"][backend]["result_filename"], result)

        for case_name in ("op", "dc", "ac"):
            case = manifest["capability_cases"][case_name]
            for backend in case["backends"]:
                name = f"openada-circuit-{backend}-{os.getpid()}-{secrets.token_hex(4)}"
                if re.fullmatch(
                    r"openada-circuit-(?:ngspice|xyce)-[1-9][0-9]*-[0-9a-f]{8}",
                    name,
                ) is None:
                    raise ConformanceError("generated container name is malformed")
                command = _container_command(
                    container_engine,
                    manifest,
                    openada_root,
                    evidence,
                    backend,
                    name,
                    case_name,
                )
                commands[f"{case_name}.{backend}"] = command
                result = _run_backend(
                    command,
                    engine=container_engine,
                    container_name=name,
                    timeout=manifest["operation"]["container_timeout_seconds"],
                )
                _write_json(evidence / case["backends"][backend]["result_filename"], result)

        run_document = {
            "schema": "openada.circuit-simulate-conformance-run/v0alpha2",
            "conformance_id": manifest["id"],
            "manifest_sha256": _sha256(manifest_path),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "image": {
                "reference": manifest["runtime"]["image"]["reference"],
                "id": image_record["Id"],
                "os": image_record["Os"],
                "architecture": image_record["Architecture"],
            },
            "network": "none during EDA execution",
            "openada_checkout": _git_state(openada_root),
            "container_commands": commands,
        }
        _write_json(evidence / "run.json", run_document)
        verification = verify_evidence(evidence, manifest_path=manifest_path)
        return evidence, verification
    except Exception as exc:
        raise ConformanceError(f"conformance evidence retained at {evidence}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--container-engine", default="docker")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        evidence, verification = replay(
            manifest_path=arguments.manifest,
            openada_root=arguments.openada_root,
            evidence_path=arguments.evidence_dir,
            container_engine=arguments.container_engine,
        )
        print(
            json.dumps(
                {"evidence_directory": str(evidence), "verification": verification},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ConformanceError as exc:
        print(f"conformance replay failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
