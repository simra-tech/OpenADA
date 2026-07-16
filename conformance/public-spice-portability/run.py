#!/usr/bin/env python3
"""Replay the public SPICE portability chain in one isolated container."""

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
    cache_checkouts,
    default_cache_dir,
    ensure_external_cache,
    inspect_image,
    load_manifest,
    require_mount_safe_path,
    run_checked,
    sha256_file,
    verify_ihp_checkout,
    verify_xyce_checkout,
)
from verify import semantic_subject_sha256, verify_evidence


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MAX_OBSERVATION_BYTES = 8 * 1024 * 1024
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import (  # noqa: E402
    SemanticReceiptError,
    design_provenance,
    git_state as receipt_git_state,
    source_attestation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--xyce-dir", type=Path)
    parser.add_argument("--ihp-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--openada-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Create an explicitly provisional replay from an unchanged dirty checkout.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish a clean non-provisional replay under this chain directory.",
    )
    return parser


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    require_mount_safe_path(source)
    value = f"type=bind,source={source},target={target}"
    return f"{value},readonly" if readonly else value


def _container_command(
    engine: str,
    manifest: dict[str, Any],
    openada: Path,
    xyce: Path,
    ihp: Path,
    evidence: Path,
    name: str,
) -> list[str]:
    return [
        engine, "run", "--rm", "--name", name, "--pull=never",
        "--platform", manifest["runtime"]["platform"],
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "512",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--env", "HOME=/tmp/openada-home", "--env", "TMPDIR=/tmp",
        "--env", "PATH=/openada/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--workdir", "/evidence",
        "--mount", _mount(openada, "/openada", readonly=True),
        "--mount", _mount(xyce, "/xyce", readonly=True),
        "--mount", _mount(ihp, "/ihp", readonly=True),
        "--mount", _mount(evidence, "/evidence", readonly=False),
        "--entrypoint", "/usr/bin/python3", manifest["runtime"]["image_reference"],
        "/openada/conformance/public-spice-portability/inside.py",
        "--manifest", "/openada/conformance/public-spice-portability/manifest.json",
        "--evidence", "/evidence",
    ]


def _git_state(path: Path) -> dict[str, Any]:
    commit = run_checked(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    status = run_checked(
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout
    entries = status.splitlines()
    return {
        "commit": commit,
        "tracked_files_modified": any(not line.startswith("?? ") for line in entries),
        "untracked_files_present": any(line.startswith("?? ") for line in entries),
        "working_tree_modified": bool(entries),
        "status_entry_count": len(entries),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _source_record(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    unchanged = before == after
    return {
        "before": before,
        "after": after,
        "state_unchanged": unchanged,
        "commit_exact": bool(unchanged and not before["working_tree_modified"]),
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_contract_tests(chain_id: str) -> dict[str, Any]:
    suite = HERE / "test_portability_chain.py"
    environment = os.environ.copy()
    environment.pop("OPENADA_RUN_PUBLIC_SPICE_PORTABILITY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            str(suite.relative_to(REPOSITORY_ROOT)),
        ],
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
            "portability contract tests failed: " + completed.stdout[-4_000:]
        )
    passed = re.findall(r"(?:^|\s)([0-9]+) passed", completed.stdout)
    skipped = re.findall(r"(?:^|\s)([0-9]+) skipped", completed.stdout)
    if len(passed) != 1 or len(skipped) > 1:
        raise ConformanceError("cannot parse the focused portability test summary")
    return {
        "schema": "openada.contract-test-report/public-spice-portability/v1",
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


def _validate_destination(path: Path | None, protected: dict[str, Path]) -> Path | None:
    if path is None:
        return None
    unresolved = path.expanduser()
    if unresolved.exists() or unresolved.is_symlink():
        raise ConformanceError(f"evidence destination must be fresh: {unresolved}")
    resolved = unresolved.resolve()
    for label, root in protected.items():
        root = root.resolve()
        if resolved == root or root in resolved.parents:
            raise ConformanceError(f"evidence must be outside {label}: {resolved}")
    return resolved


def _create_evidence(path: Path | None) -> Path:
    if path is None:
        return Path(tempfile.mkdtemp(prefix="openada-public-spice-portability-")).resolve()
    path.mkdir(parents=True, mode=0o700)
    return path


def _run_container(command: list[str], *, engine: str, name: str) -> tuple[dict[str, Any], str]:
    try:
        completed = subprocess.run(
            command, check=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run([engine, "rm", "-f", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise ConformanceError("portability container exceeded 900 seconds and was removed") from exc
    except OSError as exc:
        raise ConformanceError(f"cannot execute container engine: {exc}") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_OBSERVATION_BYTES:
        raise ConformanceError("inside observation exceeded the runner bound")
    try:
        observation = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"inside runner returned invalid JSON: {exc}; stderr={completed.stderr[-4000:]!r}"
        ) from exc
    if not isinstance(observation, dict):
        raise ConformanceError("inside observation is not an object")
    if completed.returncode != 0:
        raise ConformanceError(
            f"inside runner exited {completed.returncode}: error={observation.get('error')!r}; "
            f"stderr={completed.stderr[-4000:]!r}"
        )
    if completed.stderr:
        raise ConformanceError(f"inside runner emitted ambient stderr: {completed.stderr[-4000:]!r}")
    return observation, completed.stderr


def _artifact(
    evidence: Path,
    relative: str,
    role: str,
    *,
    source_step: str | None,
    source_output: str | None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    path = evidence / relative
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size <= 0:
        raise ConformanceError(f"unsafe chain artifact: {path}")
    return {
        "repository_path": f"conformance/public-spice-portability/evidence/{relative}",
        "bytes": metadata.st_size,
        "sha256": sha256_file(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": replay_id,
    }


def _write_chain_run(
    evidence: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    source_receipt: dict[str, Any],
) -> None:
    native_paths = {
        "ngspice-op": "sim/ngspice-op/inverter-op.raw",
        "ngspice-dc": "sim/ngspice-dc/inverter-dc.raw",
        "ngspice-ac": "sim/ngspice-ac/ota-ac.raw",
        "xyce-dc": "sim/xyce-dc/xyce-dc.xyce.raw",
        "xyce-ac": "sim/xyce-ac/xyce-ac-derived.xyce.raw",
        "xyce-tran": "sim/xyce-tran/xyce-tran.xyce.raw",
    }
    artifacts = [
        _artifact(evidence, "contract-tests.json", "contract-test", source_step="contract-tests", source_output="contract-test-verdict"),
        _artifact(evidence, "design-provenance.json", "design-provenance", source_step="materialize-pinned-sources", source_output="design-provenance"),
        _artifact(evidence, "secondary-design-provenance.json", "source-provenance", source_step="materialize-pinned-sources", source_output="secondary-design-provenance"),
    ]
    for identifier, relative in native_paths.items():
        artifacts.append(_artifact(evidence, relative, "native-artifact", source_step=f"simulate-{identifier}", source_output=f"{identifier}-raw"))
        artifacts.append(_artifact(evidence, f"results/sim/{identifier}.json", "semantic-result", source_step=f"simulate-{identifier}", source_output=f"{identifier}-result"))
    artifacts.append(_artifact(evidence, "legacy/ngspice-op/inverter-op.raw", "native-artifact", source_step="simulate-legacy-ngspice", source_output="legacy-ngspice-raw"))
    artifacts.append(_artifact(evidence, "independent-verification.json", "independent-oracle", source_step="independent-verifier", source_output="independent-portability-verdict"))
    for identifier in SIMULATION_IDS:
        artifacts.append(_artifact(evidence, f"results/extract/{identifier}.json", "normalized-evidence", source_step=f"extract-{identifier}", source_output=f"{identifier}-series"))
    admin_outputs = {
        "capabilities": "capabilities-result", "doctor": "doctor-result",
        "profile-list": "profile-list-result", "profile-show": "profile-show-result",
        "provider-list": "provider-list-result",
    }
    for identifier, output in admin_outputs.items():
        artifacts.append(_artifact(evidence, f"results/admin/{identifier}.json", "admin-evidence", source_step=identifier, source_output=output))
    artifacts.append(_artifact(evidence, "results/admin/provider-validate.json", "downstream-decision", source_step="provider-validate", source_output="provider-validation-decision"))
    artifacts.append(_artifact(evidence, "agent-evidence.json", "agent-visible-evidence", source_step="agent-decision", source_output="agent-portability-evidence"))
    for identifier in NEGATIVE_IDS:
        artifacts.append(_artifact(evidence, f"results/negative/{identifier}.json", "negative-replay", source_step=None, source_output=None, replay_id=identifier))
    for identifier in TAMPER_IDS:
        artifacts.append(_artifact(evidence, f"tamper/{identifier}.json", "tamper-replay", source_step=None, source_output=None, replay_id=identifier))
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": manifest["id"],
        "chain_manifest_sha256": sha256_file(manifest_path),
        "semantic_subject_sha256": semantic_subject_sha256(),
        "source_attestation": source_receipt,
        "status": "pass",
        "checks": {
            "contract_test": True, "pinned_real_design": True, "native_run": True,
            "independent_artifact_check": True, "normalized_evidence": True,
            "downstream_decision": True, "negative_replay": True,
            "tamper_replay": True, "agent_visible_evidence": True,
        },
        "artifacts": artifacts,
        "extensions": {
            "org.openada": {
                "verifier": "conformance/public-spice-portability/verify.py",
                "provisional": source_receipt["receipt_class"] == "provisional",
                "source_freeze_attested": source_receipt["receipt_class"] == "release",
                "tamper_probe_count": len(TAMPER_IDS),
            }
        },
    }
    _write_json(evidence / "chain-run.json", document)


# Kept local to avoid importing implementation constants from the oracle.
SIMULATION_IDS = (
    "ngspice-op", "ngspice-dc", "ngspice-ac", "xyce-dc", "xyce-ac", "xyce-tran"
)
NEGATIVE_IDS = (
    "xyce-ac-presentation-rejected", "xyce-op-unsupported",
    "ngspice-analysis-mismatch", "extract-missing-selector",
    "admin-unknown-profile", "admin-invalid-provider",
)
TAMPER_IDS = (
    "request-contract-byte", "public-source-byte", "derived-deck-byte",
    "native-raw-byte", "simulation-analysis-type", "simulation-backend-id",
    "extraction-series-digest", "admin-result-byte", "agent-decision-byte",
)


def _publish_evidence(evidence: Path) -> Path:
    destination = HERE / "evidence"
    replacement = HERE / f".evidence.next-{os.getpid()}"
    if replacement.exists() or replacement.is_symlink():
        raise ConformanceError(
            f"unsafe publication staging path exists: {replacement}"
        )
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
        if (
            replacement.exists()
            and replacement.is_dir()
            and not replacement.is_symlink()
        ):
            shutil.rmtree(replacement)
    return destination


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence: Path | None = None
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        openada = args.openada_root.expanduser().resolve()
        cache = args.cache_dir.expanduser().resolve()
        ensure_external_cache(cache, openada)
        defaults = cache_checkouts(cache)
        xyce = (args.xyce_dir or defaults[0]).expanduser().resolve()
        ihp = (args.ihp_dir or defaults[1]).expanduser().resolve()
        for label, path in (("OpenADA", openada), ("Xyce", xyce), ("IHP", ihp)):
            if not path.is_dir() or path.is_symlink():
                raise ConformanceError(f"{label} checkout is unavailable or unsafe: {path}")
            require_mount_safe_path(path)
        verify_xyce_checkout(xyce, manifest)
        verify_ihp_checkout(ihp, manifest)
        image = inspect_image(args.container_engine, manifest)
        before = {"openada": _git_state(openada), "xyce": _git_state(xyce), "ihp": _git_state(ihp)}
        receipt_before = receipt_git_state(openada)
        if before["openada"]["working_tree_modified"] and not args.allow_dirty:
            raise ConformanceError(
                "OpenADA checkout is dirty; commit the exact semantic subject or use "
                "--allow-dirty for an explicitly provisional replay"
            )
        if args.publish and args.allow_dirty:
            raise ConformanceError("a provisional dirty replay cannot be published")
        requested = _validate_destination(
            args.evidence_dir,
            {"OpenADA checkout": openada, "cache": cache, "Xyce checkout": xyce, "IHP checkout": ihp},
        )
        evidence = _create_evidence(requested)
        require_mount_safe_path(evidence)
        name = f"openada-portability-{os.getpid()}-{secrets.token_hex(4)}"
        command = _container_command(args.container_engine, manifest, openada, xyce, ihp, evidence, name)
        observation, _stderr = _run_container(command, engine=args.container_engine, name=name)
        verify_xyce_checkout(xyce, manifest)
        verify_ihp_checkout(ihp, manifest)
        after = {"openada": _git_state(openada), "xyce": _git_state(xyce), "ihp": _git_state(ihp)}
        source_state = {key: _source_record(before[key], after[key]) for key in before}
        if not all(item["state_unchanged"] for item in source_state.values()):
            raise ConformanceError("a source checkout changed during the isolated replay")
        if not args.allow_dirty and not source_state["openada"]["commit_exact"]:
            raise ConformanceError("clean source-freeze attestation was not established")
        run_document = {
            "schema": "openada.public-spice-portability-run-metadata/v0alpha1",
            "chain_id": manifest["id"],
            "chain_manifest_sha256": sha256_file(manifest_path),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provisional": bool(args.allow_dirty),
            "image": {
                "reference": manifest["runtime"]["image_reference"],
                "id": image.get("Id"), "os": image.get("Os"),
                "architecture": image.get("Architecture"),
            },
            "source_state": source_state,
            "execution_policy": {
                "network": "none", "container_root": "read-only",
                "openada_mount": "read-only", "public_source_mounts": "read-only",
                "evidence_mount": "writable", "host_user": f"{os.getuid()}:{os.getgid()}",
            },
            "container_command": command,
            "runtime_observation": observation,
        }
        _write_json(evidence / "run.json", run_document)
        _write_json(
            evidence / "design-provenance.json",
            design_provenance(xyce, manifest["design"]),
        )
        _write_json(
            evidence / "secondary-design-provenance.json",
            design_provenance(
                ihp,
                manifest["design"]["extensions"]["org.openada"]["secondary_design"],
            ),
        )
        report = verify_evidence(
            manifest, evidence, manifest_sha256=sha256_file(manifest_path),
            require_chain_run=False, run_tamper_probes=True,
        )
        tamper = evidence / "tamper"
        tamper.mkdir(mode=0o700)
        for replay in report["tamper_replays"]:
            _write_json(tamper / f"{replay['replay_id']}.json", replay)
        _write_json(evidence / "independent-verification.json", report)
        _write_json(
            evidence / "contract-tests.json", _run_contract_tests(manifest["id"])
        )
        receipt_after = receipt_git_state(openada)
        source_receipt = source_attestation(
            receipt_before,
            receipt_after,
            semantic_subject_sha256=semantic_subject_sha256(),
            receipt_class="provisional" if args.allow_dirty else "release",
        )
        _write_chain_run(
            evidence, manifest_path, manifest, source_receipt=source_receipt
        )
        verify_evidence(
            manifest, evidence, manifest_sha256=sha256_file(manifest_path),
            require_chain_run=True, run_tamper_probes=True,
        )
        if args.publish:
            evidence = _publish_evidence(evidence)
    except (ConformanceError, SemanticReceiptError) as exc:
        print(f"portability replay failed: {exc}", file=sys.stderr)
        if evidence is not None:
            print(f"incomplete evidence retained at: {evidence}", file=sys.stderr)
        return 1
    qualifier = "provisional " if args.allow_dirty else "source-frozen "
    print(f"Public SPICE portability {qualifier}replay verified. Evidence: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
