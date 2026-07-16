#!/usr/bin/env python3
"""Run pinned Ibex synthesis, timing, and missing-top checks without network."""

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
    ABC_EXECUTABLE_BYTES,
    ABC_EXECUTABLE_PATH,
    ABC_EXECUTABLE_SHA256,
    ABC_EXECUTABLE_VERSION,
    ABC_REPOSITORY_PATH,
    CHAIN_ID,
    ConformanceError,
    DESIGN_TREE,
    LIBERTY_PATH,
    OPENSTA_PATH,
    OPENSTA_VERSION,
    SDC_PATH,
    TECHMAP_PATH,
    UPSTREAM_REVISION,
    YOSYS_PATH,
    YOSYS_VERSION,
    canonical_inventory_sha256,
    default_cache_dir,
    ensure_external_cache,
    ensure_external_design_path,
    inspect_image,
    load_manifest,
    require_mount_safe_path,
    run_checked,
    sha256_file,
    verify_derived_inputs,
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


NATIVE_ARTIFACT_PATHS = [
    "synthesis/synthesize.result.json",
    "synthesis/synthesize.ys",
    "synthesis/synthesize.log",
    "synthesis/inference-stats.json",
    "synthesis/mapped-stats.json",
    "synthesis/mapped.v",
    "synthesis/mapped.json",
    "synthesis/rtl-inputs.json",
    "timing/timing-analyze.result.json",
    "timing/timing-analyze.tcl",
    "timing/timing-input.sdc",
    "timing/timing-analyze.log",
    "timing/check-setup.txt",
    "timing/setup-paths.json",
    "timing/hold-paths.json",
    "negative/synthesize.result.json",
    "negative/synthesize.ys",
    "negative/synthesize.log",
    "negative/rtl-inputs.json",
]


def _semantic_design(manifest: dict[str, Any]) -> dict[str, Any]:
    design = manifest["design"]
    return {
        "class": "public-design",
        "repository": design["repository"],
        "revision": design["revision"],
        "tree": design["tree"],
        "subtree": design["subtree"],
        "license": {
            "path": design["license"]["path"],
            "sha256": design["license"]["sha256"],
        },
        "inputs": [
            {"path": record["path"], "sha256": record["sha256"]}
            for record in manifest["pinned_files"]
            if record["path"] != design["license"]["path"]
        ],
        "extensions": {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument(
        "--receipt-class", choices=("provisional", "release"), default="provisional"
    )
    return parser


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    require_mount_safe_path(source)
    value = f"type=bind,source={source},target={target}"
    return f"{value},readonly" if readonly else value


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
        "1024",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp/openada-home",
        "--env",
        "TMPDIR=/tmp",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=2g",
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
        "--compact",
        "--tool-path",
        f"yosys={YOSYS_PATH}",
        "--tool-path",
        f"abc={ABC_EXECUTABLE_PATH}",
        "--tool-path",
        f"sta={OPENSTA_PATH}",
    ]


def _design_path(relative: str) -> str:
    return f"/design/{relative}"


def _synthesis_argv(manifest: dict[str, Any], name: str) -> list[str]:
    base = manifest["operations"]["synthesize"]
    operation = manifest["operations"][name]
    argv = [
        "synthesize",
        *(_design_path(path) for path in base["source_paths"]),
        "--top",
        operation["top"],
        "--liberty",
        _design_path(base["liberty"]),
        "--frontend",
        base["frontend"],
        "--language",
        base["language"],
    ]
    for directory in base["include_directories"]:
        argv.extend(("--include-dir", _design_path(directory)))
    for techmap in base["techmaps"]:
        argv.extend(("--techmap", _design_path(techmap)))
    for cell in base["dont_use"]:
        argv.extend(("--dont-use", cell))
    argv.extend(
        (
            "--abc-delay-target-ns",
            str(base["abc_delay_target_ns"]),
            "--abc-constraint",
            f"/openada/{ABC_REPOSITORY_PATH}",
            "--output-dir",
            operation["output_directory"],
            "--timeout",
            str(operation["tool_timeout_seconds"]),
        )
    )
    return argv


def _timing_argv(manifest: dict[str, Any]) -> list[str]:
    operation = manifest["operations"]["timing_analyze"]
    return [
        "timing-analyze",
        operation["netlist"],
        "--top",
        operation["top"],
        "--liberty",
        _design_path(operation["liberty"]),
        "--sdc",
        _design_path(operation["sdc"]),
        "--output-dir",
        operation["output_directory"],
        "--timeout",
        str(operation["tool_timeout_seconds"]),
    ]


def _write_result(path: Path, stdout: str, stderr: str, returncode: int) -> None:
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"container returned {returncode} without one JSON result: {exc}; "
            f"stderr={stderr[-4000:]!r}"
        ) from exc
    if not isinstance(document, dict):
        raise ConformanceError("OpenADA container output must be one JSON object")
    path.parent.mkdir(parents=True, exist_ok=True)
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return None if completed.returncode == 0 else (completed.stderr or completed.stdout).strip()[-1000:]


def _run_operation(
    command: list[str],
    result_path: Path,
    *,
    timeout: float,
    expected_cli_exit: int,
    container_engine: str,
    container_name: str,
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
        cleanup = _cleanup_container(container_engine, container_name)
        raise ConformanceError(
            f"container exceeded the {timeout:g}-second timeout"
            + (f"; cleanup error: {cleanup}" if cleanup else "; cleanup completed")
        ) from exc
    except OSError as exc:
        raise ConformanceError(f"cannot execute {command[0]!r}: {exc}") from exc
    _write_result(result_path, completed.stdout, completed.stderr, completed.returncode)
    if completed.returncode != expected_cli_exit:
        raise ConformanceError(
            f"OpenADA container exited with {completed.returncode}; expected {expected_cli_exit}; "
            f"stderr={completed.stderr[-4000:]!r}"
        )


def _validate_evidence_location(path: Path | None, protected: dict[str, Path]) -> Path | None:
    roots = {label: root.expanduser().resolve() for label, root in protected.items()}
    if path is None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        for label, root in roots.items():
            if temporary_root == root or root in temporary_root.parents:
                raise ConformanceError(f"the system temporary directory is inside the {label}")
        return None
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ConformanceError("evidence path may not be a symbolic link")
    evidence = expanded.resolve()
    for label, root in roots.items():
        if evidence == root or root in evidence.parents:
            raise ConformanceError(f"the evidence directory must be outside the {label}")
    if expanded.exists():
        raise ConformanceError(f"evidence path already exists: {evidence}")
    return evidence


def _create_evidence(path: Path | None) -> Path:
    try:
        evidence = (
            Path(tempfile.mkdtemp(prefix="openada-orfs-ibex-")) if path is None else path
        )
        if path is not None:
            evidence.mkdir(parents=True, mode=0o700)
        for name in ("synthesis", "timing", "negative"):
            (evidence / name).mkdir()
    except OSError as exc:
        raise ConformanceError(f"cannot create fresh evidence directory: {exc}") from exc
    return evidence.resolve()


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
        "tracked_files_modified": any(not item.startswith("?? ") for item in entries),
        "untracked_files_present": any(item.startswith("?? ") for item in entries),
        "working_tree_modified": bool(entries),
        "status_entry_count": len(entries),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _file_record(evidence: Path, relative: str) -> dict[str, Any]:
    path = evidence / relative
    if not path.is_file() or path.is_symlink():
        raise ConformanceError(f"required native artifact is missing or unsafe: {path}")
    return {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _write_run_metadata(
    evidence: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    image: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    source_receipt: dict[str, Any],
) -> None:
    state_available = before["commit"] is not None and after["commit"] is not None
    state_unchanged = bool(state_available and before == after)
    document = {
        "schema": "openada.conformance-run/v0alpha1",
        "conformance_id": manifest["id"],
        "chain_id": CHAIN_ID,
        "conformance_manifest_sha256": sha256_file(manifest_path),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "design_revision": manifest["design"]["revision"],
        "design_tree": DESIGN_TREE,
        "upstream_revision": UPSTREAM_REVISION,
        "input_inventory_sha256": canonical_inventory_sha256(manifest),
        "image": {
            "reference": manifest["runtime"]["image"]["reference"],
            "id": image.get("Id"),
            "os": image.get("Os"),
            "architecture": image.get("Architecture"),
        },
        "tools": {
            "yosys": {"path": YOSYS_PATH, "version": YOSYS_VERSION},
            "abc": {
                "path": ABC_EXECUTABLE_PATH,
                "version": ABC_EXECUTABLE_VERSION,
                "bytes": ABC_EXECUTABLE_BYTES,
                "sha256": ABC_EXECUTABLE_SHA256,
            },
            "opensta": {"path": OPENSTA_PATH, "version": OPENSTA_VERSION},
        },
        "openada_checkout": {
            "before": before,
            "after": after,
            "state_unchanged": state_unchanged,
            "commit_exact": bool(state_unchanged and not before["working_tree_modified"]),
        },
        "network": "none during EDA execution",
        "analysis_scope": manifest["policy"]["analysis_scope"],
        "source_attestation": source_receipt,
        "native_artifacts": [_file_record(evidence, path) for path in NATIVE_ARTIFACT_PATHS],
    }
    (evidence / "run.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
            cache_dir / "OpenROAD-flow-scripts", openada_root, cache_dir
        )
        requested = _validate_evidence_location(
            args.evidence_dir,
            {
                "OpenADA checkout": openada_root,
                "conformance cache": cache_dir,
                "design checkout": design_dir,
            },
        )
        if not (openada_root / "bin/openada").is_file():
            raise ConformanceError(f"OpenADA checkout is missing bin/openada: {openada_root}")
        verify_derived_inputs(openada_root, manifest)
        verify_design_checkout(design_dir, manifest)
        image = inspect_image(args.container_engine, manifest)
        for path in (openada_root, design_dir):
            require_mount_safe_path(path)
        before = _git_state(openada_root)
        receipt_before = receipt_git_state(openada_root)
        evidence = _create_evidence(requested)
        require_mount_safe_path(evidence)

        operations = (
            ("synthesize", _synthesis_argv(manifest, "synthesize"), "synthesis/synthesize.result.json"),
            ("timing_analyze", _timing_argv(manifest), "timing/timing-analyze.result.json"),
            ("missing_top", _synthesis_argv(manifest, "missing_top"), "negative/synthesize.result.json"),
        )
        for name, operation_argv, result_filename in operations:
            operation = manifest["operations"][name]
            print(f"Running pinned {name} with network disabled ...", flush=True)
            container_name = f"openada-orfs-ibex-{name.replace('_', '-')}-{os.getpid()}-{secrets.token_hex(4)}"
            command = [
                *_container_base(
                    args.container_engine,
                    manifest,
                    openada_root,
                    design_dir,
                    evidence,
                    container_name,
                ),
                *operation_argv,
            ]
            expected_cli_exit = operation["expect"].get(
                "cli_exit_code", operation["expect"].get("exit_code", 0)
            )
            _run_operation(
                command,
                evidence / result_filename,
                timeout=operation["container_timeout_seconds"],
                expected_cli_exit=expected_cli_exit,
                container_engine=args.container_engine,
                container_name=container_name,
            )

        verify_design_checkout(design_dir, manifest)
        verify_derived_inputs(openada_root, manifest)
        after = _git_state(openada_root)
        receipt_after = receipt_git_state(openada_root)
        subject = semantic_subject(
            openada_root, openada_root / "catalog/semantic-surfaces-v0alpha1.json"
        )
        source_receipt = source_attestation(
            receipt_before,
            receipt_after,
            semantic_subject_sha256=subject,
            receipt_class=args.receipt_class,
        )
        write_receipt_json(
            evidence / "design-provenance.json",
            design_provenance(design_dir, _semantic_design(manifest)),
        )
        _write_run_metadata(
            evidence,
            manifest_path,
            manifest,
            image,
            before,
            after,
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
