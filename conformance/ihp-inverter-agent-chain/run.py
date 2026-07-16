#!/usr/bin/env python3
"""Replay the pinned IHP agent chain in one network-disabled container."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
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
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import (  # noqa: E402
    SemanticReceiptError,
    design_provenance,
    git_state as receipt_git_state,
    semantic_subject,
    source_attestation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the public IHP inverter agent-evidence chain."
    )
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Retain the verified replay at conformance/ihp-inverter-agent-chain/evidence.",
    )
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
    runtime = manifest["runtime"]
    return [
        engine,
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--platform",
        runtime["platform"],
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
        "PATH=/openada/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
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
        runtime["image_reference"],
        "/openada/conformance/ihp-inverter-agent-chain/inside.py",
        "--manifest",
        "/openada/conformance/ihp-inverter-agent-chain/manifest.json",
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


def _run_container(
    command: list[str], *, timeout: float, engine: str, name: str
) -> tuple[dict[str, Any], int, str]:
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


def _validate_evidence_location(path: Path | None, protected: dict[str, Path]) -> Path | None:
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
            return Path(tempfile.mkdtemp(prefix="openada-ihp-agent-chain-")).resolve()
        path.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise ConformanceError(f"cannot create fresh evidence directory: {exc}") from exc
    return path


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_contract_tests(chain_id: str) -> dict[str, Any]:
    suite = HERE / "test_agent_chain.py"
    environment = os.environ.copy()
    environment.pop("OPENADA_RUN_IHP_AGENT_CHAIN", None)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(suite.relative_to(REPOSITORY_ROOT))],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ConformanceError(
            "agent-chain contract tests failed: " + completed.stdout[-4_000:]
        )
    passed = re.findall(r"(?:^|\s)([0-9]+) passed", completed.stdout)
    skipped = re.findall(r"(?:^|\s)([0-9]+) skipped", completed.stdout)
    if len(passed) != 1 or len(skipped) > 1:
        raise ConformanceError("cannot parse the focused agent-chain test summary")
    return {
        "schema": "openada.contract-test-report/ihp-inverter-agent-chain/v1",
        "chain_id": chain_id,
        "status": "pass",
        "suite": {
            "repository_path": suite.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": sha256_file(suite),
            "passed": int(passed[0]),
            "skipped": int(skipped[0]) if skipped else 0,
            "failed": 0,
        },
        "extensions": {},
    }


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
        "schema": "openada.ihp-agent-chain-run-metadata/v0alpha1",
        "chain_id": manifest["id"],
        "chain_manifest_sha256": sha256_file(manifest_path),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "design_revision": manifest["design"]["revision"],
        "image": {
            "reference": manifest["runtime"]["image_reference"],
            "id": image_record.get("Id"),
            "os": image_record.get("Os"),
            "architecture": image_record.get("Architecture"),
        },
        "openada_checkout": _checkout_record(checkout_before, checkout_after),
        "network": "none during EDA execution",
        "container_command": container_command,
        "runtime_observation": observation,
    }
    _write_json(evidence / "run.json", document)


def _semantic_subject() -> str:
    return semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )


def _artifact_record(
    path: Path,
    repository_path: str,
    role: str,
    *,
    source_step: str | None,
    source_output: str | None,
    replay_id: str | None,
) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size <= 0:
        raise ConformanceError(f"cannot index unsafe repository artifact: {path}")
    return {
        "repository_path": repository_path,
        "bytes": metadata.st_size,
        "sha256": sha256_file(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": replay_id,
    }


def _retained_artifact(
    evidence: Path,
    relative: str,
    role: str,
    *,
    source_step: str | None = None,
    source_output: str | None = None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    publication = f"conformance/ihp-inverter-agent-chain/evidence/{relative}"
    return _artifact_record(
        evidence / relative,
        publication,
        role,
        source_step=source_step,
        source_output=source_output,
        replay_id=replay_id,
    )


def _write_chain_run(
    evidence: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    source_receipt: dict[str, Any],
) -> None:
    artifacts: list[dict[str, Any]] = [
        _retained_artifact(
            evidence,
            "contract-tests.json",
            "contract-test",
            source_step="contract-tests",
            source_output="contract-test-verdict",
        ),
        _retained_artifact(
            evidence,
            "design-provenance.json",
            "design-provenance",
            source_step="materialize-pinned-sources",
            source_output="design-provenance",
        ),
        _retained_artifact(
            evidence,
            "provider/work/test_inverter.raw",
            "native-artifact",
            source_step="provider-invoke",
            source_output="native-raw",
        ),
        _retained_artifact(
            evidence,
            "independent-verification.json",
            "independent-oracle",
            source_step="independent-verifier",
            source_output="independent-chain-verdict",
        ),
        _retained_artifact(
            evidence,
            "extract.json",
            "normalized-evidence",
            source_step="extract",
            source_output="normalized-vin-vout-series",
        ),
        _retained_artifact(
            evidence,
            "specifications/sample-at-pass.json",
            "downstream-decision",
            source_step="evaluate-pass",
            source_output="nine-passing-specification-decisions",
        ),
        _retained_artifact(
            evidence,
            "agent-evidence.json",
            "agent-visible-evidence",
            source_step="agent-decision",
            source_output="agent-evidence",
        ),
    ]
    negative_paths = {
        "netlist-missing-symbol": "negative/netlist-missing-symbol.json",
        "isolated-terminal-nonconvergence": "provider-fail-result.json",
        "isolated-builtin-terminal-nonconvergence": "builtin-fail-result.json",
        "extract-missing-selector": "negative/extract-missing-selector.json",
        **{
            f"measure-{identifier}": f"negative/measure-{identifier}.json"
            for identifier in (
                "sample-at", "minimum", "maximum", "mean", "rms", "crossing",
                "rise-time", "fall-time", "settling-time",
            )
        },
        "deliberately-violated-limits": "specifications/sample-at-fail.json",
        "specification-condition-mismatch": "negative/spec-sample-at-condition-mismatch.json",
    }
    declared_negative = [item["id"] for item in manifest["negative_replays"]]
    if list(negative_paths) != declared_negative:
        raise ConformanceError("negative replay artifact mapping drifted from the manifest")
    artifacts.extend(
        _retained_artifact(
            evidence,
            relative,
            "negative-replay",
            replay_id=replay_id,
        )
        for replay_id, relative in negative_paths.items()
    )
    declared_tamper = [item["id"] for item in manifest["tamper_replays"]]
    artifacts.extend(
        _retained_artifact(
            evidence,
            f"tamper/{replay_id}.json",
            "tamper-replay",
            replay_id=replay_id,
        )
        for replay_id in declared_tamper
    )
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": manifest["id"],
        "chain_manifest_sha256": sha256_file(manifest_path),
        "semantic_subject_sha256": _semantic_subject(),
        "source_attestation": source_receipt,
        "status": "pass",
        "checks": {
            "contract_test": True,
            "pinned_real_design": True,
            "native_run": True,
            "independent_artifact_check": True,
            "normalized_evidence": True,
            "downstream_decision": True,
            "negative_replay": True,
            "tamper_replay": True,
            "agent_visible_evidence": True,
        },
        "artifacts": artifacts,
        "extensions": {
            "org.openada": {
                "verifier": "conformance/ihp-inverter-agent-chain/verify.py",
                "tamper_probe_count": len(declared_tamper),
                "publication": "release fresh replay",
            }
        },
    }
    _write_json(evidence / "chain-run.json", document)


def _publish_evidence(evidence: Path) -> Path:
    destination = HERE / "evidence"
    replacement = HERE / f".evidence.next-{os.getpid()}"
    if replacement.exists() or replacement.is_symlink():
        raise ConformanceError(f"unsafe publication staging path exists: {replacement}")
    try:
        shutil.copytree(evidence, replacement, copy_function=shutil.copy2)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ConformanceError(
                    f"refusing to replace unsafe publication destination: {destination}"
                )
            shutil.rmtree(destination)
        os.replace(replacement, destination)
    except OSError as exc:
        raise ConformanceError(f"cannot publish retained evidence: {exc}") from exc
    finally:
        if replacement.exists() and replacement.is_dir() and not replacement.is_symlink():
            shutil.rmtree(replacement)
    return destination


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
        if not (openada_root / "bin/openada").is_file():
            raise ConformanceError(f"OpenADA source checkout is missing bin/openada: {openada_root}")
        if not (openada_root / "bin/openada-provider-ngspice").is_file():
            raise ConformanceError("OpenADA source checkout is missing the ngspice provider launcher")
        verify_design_checkout(design_dir, manifest)
        image_record = inspect_image(args.container_engine, manifest)
        for path in (openada_root, design_dir):
            require_mount_safe_path(path)
        checkout_before = _git_state(openada_root)
        receipt_before = receipt_git_state(openada_root)
        evidence = _create_evidence(requested)
        require_mount_safe_path(evidence)

        name = f"openada-ihp-agent-{os.getpid()}-{secrets.token_hex(4)}"
        command = _container_command(
            args.container_engine, manifest, openada_root, design_dir, evidence, name
        )
        observation, returncode, stderr = _run_container(
            command, timeout=600, engine=args.container_engine, name=name
        )
        verify_design_checkout(design_dir, manifest)
        checkout_after = _git_state(openada_root)
        _write_json(
            evidence / "design-provenance.json",
            design_provenance(design_dir, manifest["design"]),
        )
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
        verification_report = verify_evidence(
            manifest,
            evidence,
            manifest_sha256=sha256_file(manifest_path),
            require_chain_run=False,
            run_tamper_probes=True,
        )
        tamper_dir = evidence / "tamper"
        tamper_dir.mkdir(mode=0o700)
        for replay in verification_report["tamper_replays"]:
            _write_json(tamper_dir / f"{replay['replay_id']}.json", replay)
        _write_json(evidence / "independent-verification.json", verification_report)
        _write_json(
            evidence / "contract-tests.json", _run_contract_tests(manifest["id"])
        )
        receipt_after = receipt_git_state(openada_root)
        subject = _semantic_subject()
        source_receipt = source_attestation(
            receipt_before,
            receipt_after,
            semantic_subject_sha256=subject,
            receipt_class="release",
        )
        _write_chain_run(evidence, manifest_path, manifest, source_receipt)
        verify_evidence(
            manifest,
            evidence,
            manifest_sha256=sha256_file(manifest_path),
            require_chain_run=True,
            run_tamper_probes=True,
        )
        published = _publish_evidence(evidence) if args.publish else None
    except (ConformanceError, SemanticReceiptError) as exc:
        print(f"conformance run failed: {exc}", file=sys.stderr)
        if evidence is not None:
            print(f"incomplete evidence retained at: {evidence}", file=sys.stderr)
        return 1
    print(f"Agent chain verified. Evidence: {published or evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
