#!/usr/bin/env python3
"""Run pinned IHP clean/failing DRC and inverter LVS without network access."""

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
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import (  # noqa: E402
    SemanticReceiptError,
    design_provenance,
    git_state as receipt_git_state,
    semantic_subject,
    source_attestation,
    write_json as write_receipt_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run pinned IHP clean/failing DRC and inverter LVS in "
            "network-disabled containers."
        )
    )
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="New output directory (default: a new directory under the system temp root)",
    )
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument(
        "--receipt-class",
        choices=("provisional", "release"),
        default="provisional",
    )
    return parser


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    require_mount_safe_path(source)
    options = f"type=bind,source={source},target={target}"
    return f"{options},readonly" if readonly else options


def _container_base(
    container_engine: str,
    manifest: dict[str, Any],
    openada_root: Path,
    design_dir: Path,
    evidence: Path,
    container_name: str,
) -> list[str]:
    image = manifest["runtime"]["image"]
    return [
        container_engine,
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
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
    ]


def _operation_argv(operation_name: str, operation: dict[str, Any]) -> list[str]:
    arguments = operation["arguments"]
    timeout = str(arguments["timeout_seconds"])
    if operation_name in {"drc", "drc_fail"}:
        command = [
            "drc",
            arguments["gds"],
            "--rules",
            arguments["rules"],
            "--report",
            arguments["report"],
            "--top-cell",
            arguments["top_cell"],
            "--timeout",
            timeout,
        ]
        for path in arguments.get("provenance_inputs", []):
            command.extend(["--provenance-input", path])
        return command
    if operation_name == "lvs":
        command = [
            "lvs",
            arguments["layout_netlist"],
            arguments["schematic_netlist"],
            "--cell",
            arguments["cell"],
            "--setup",
            arguments["setup"],
            "--report",
            arguments["report"],
            "--timeout",
            timeout,
        ]
        for path in arguments.get("provenance_inputs", []):
            command.extend(["--provenance-input", path])
        return command
    raise ConformanceError(f"unsupported operation in manifest: {operation_name}")


def _write_result(path: Path, stdout: str, stderr: str, returncode: int) -> None:
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = stderr.strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise ConformanceError(
            f"container returned {returncode} without one JSON result: {exc}; stderr={detail!r}"
        ) from exc
    if not isinstance(document, dict):
        raise ConformanceError("OpenADA container output must be one JSON object")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _cleanup_container(container_engine: str, container_name: str) -> str | None:
    try:
        completed = subprocess.run(
            [container_engine, "rm", "-f", container_name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except OSError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return "cleanup command exceeded 30 seconds"
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout).strip()
    return detail[-1_000:] or f"cleanup exited with code {completed.returncode}"


def _run_operation(
    command: list[str],
    result_path: Path,
    *,
    timeout: float,
    container_engine: str,
    container_name: str,
    expected_returncode: int,
) -> None:
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
        cleanup_error = _cleanup_container(container_engine, container_name)
        cleanup_status = (
            f"; cleanup could not be confirmed: {cleanup_error}"
            if cleanup_error
            else "; named-container cleanup completed"
        )
        raise ConformanceError(
            f"container exceeded the {timeout:g}-second outer timeout{cleanup_status}"
        ) from exc
    except OSError as exc:
        raise ConformanceError(f"cannot execute {command[0]!r}: {exc}") from exc
    _write_result(result_path, completed.stdout, completed.stderr, completed.returncode)
    if completed.returncode != expected_returncode:
        detail = completed.stderr.strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        suffix = f"; stderr={detail!r}" if detail else ""
        raise ConformanceError(
            "OpenADA container exited with code "
            f"{completed.returncode}; expected {expected_returncode}{suffix}"
        )


def _validate_evidence_location(
    path: Path | None,
    protected_roots: dict[str, Path],
) -> Path | None:
    resolved_roots = {
        label: root.expanduser().resolve() for label, root in protected_roots.items()
    }
    if path is None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        for label, root in resolved_roots.items():
            if temp_root == root or root in temp_root.parents:
                raise ConformanceError(
                    f"the system temporary directory is inside the {label}; "
                    "select an external --evidence-dir"
                )
        return None
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ConformanceError(f"evidence path may not be a symbolic link: {expanded}")
    evidence = expanded.resolve()
    for label, root in resolved_roots.items():
        if evidence == root or root in evidence.parents:
            raise ConformanceError(
                f"the evidence directory must be outside the {label}; "
                "do not contaminate pinned source or design inputs"
            )
    if expanded.exists():
        raise ConformanceError(f"evidence path already exists; choose a fresh path: {evidence}")
    return evidence


def _create_evidence(path: Path | None) -> Path:
    try:
        if path is None:
            return Path(tempfile.mkdtemp(prefix="openada-ihp-inverter-")).resolve()
        path.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise ConformanceError(f"cannot create fresh evidence directory: {exc}") from exc
    return path


def _git_state(openada_root: Path) -> dict[str, Any]:
    try:
        commit = run_checked(["git", "-C", str(openada_root), "rev-parse", "HEAD"]).stdout.strip()
        status = run_checked(
            [
                "git",
                "-C",
                str(openada_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
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
    tracked_modified = any(not entry.startswith("?? ") for entry in entries)
    untracked_present = any(entry.startswith("?? ") for entry in entries)
    return {
        "commit": commit,
        "tracked_files_modified": tracked_modified,
        "untracked_files_present": untracked_present,
        "working_tree_modified": bool(entries),
        "status_entry_count": len(entries),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _checkout_record(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    state_available = before["commit"] is not None and after["commit"] is not None
    state_unchanged = before == after if state_available else None
    commit_exact = bool(
        state_unchanged
        and before["working_tree_modified"] is False
        and after["working_tree_modified"] is False
    )
    return {
        "before": before,
        "after": after,
        "state_unchanged": state_unchanged,
        "commit_exact": commit_exact,
    }


def _write_run_metadata(
    evidence: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    image_record: dict[str, Any],
    checkout_before: dict[str, Any],
    checkout_after: dict[str, Any],
    source_receipt: dict[str, Any],
) -> None:
    metadata = {
        "schema": "openada.conformance-run/v0alpha1",
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
        "source_attestation": source_receipt,
    }
    (evidence / "run.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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
        requested_evidence = _validate_evidence_location(
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
        receipt_before = receipt_git_state(openada_root)

        evidence = _create_evidence(requested_evidence)
        require_mount_safe_path(evidence)
        for operation_name in ("drc", "drc_fail", "lvs"):
            operation = manifest["operations"][operation_name]
            print(f"Running pinned {operation_name.upper()} with network disabled ...", flush=True)
            container_name = (
                f"openada-ihp-{operation_name}-{os.getpid()}-{secrets.token_hex(4)}"
            )
            base = _container_base(
                args.container_engine,
                manifest,
                openada_root,
                design_dir,
                evidence,
                container_name,
            )
            command = [*base, *_operation_argv(operation_name, operation)]
            _run_operation(
                command,
                evidence / operation["result_filename"],
                timeout=operation["container_timeout_seconds"],
                container_engine=args.container_engine,
                container_name=container_name,
                expected_returncode=(
                    0 if operation["expect"]["engineering_status"] == "pass" else 1
                ),
            )

        verify_design_checkout(design_dir, manifest)
        checkout_after = _git_state(openada_root)
        receipt_after = receipt_git_state(openada_root)
        subject = semantic_subject(
            openada_root,
            openada_root / "catalog/semantic-surfaces-v0alpha1.json",
        )
        source_receipt = source_attestation(
            receipt_before,
            receipt_after,
            semantic_subject_sha256=subject,
            receipt_class=args.receipt_class,
        )
        semantic_chain = json.loads(
            (openada_root / "conformance/ihp-inverter/semantic-chain.json").read_text(
                encoding="utf-8"
            )
        )
        write_receipt_json(
            evidence / "design-provenance.json",
            design_provenance(design_dir, semantic_chain["design"]),
        )
        _write_run_metadata(
            evidence,
            manifest_path,
            manifest,
            image_record,
            checkout_before,
            checkout_after,
            source_receipt,
        )
        verify_evidence(manifest, evidence, manifest_sha256=sha256_file(manifest_path))
    except (ConformanceError, SemanticReceiptError) as exc:
        print(f"conformance run failed: {exc}", file=sys.stderr)
        if evidence is not None:
            print(f"incomplete evidence retained at: {evidence}", file=sys.stderr)
        return 1

    print(f"Conformance verified. Evidence: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
