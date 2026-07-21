#!/usr/bin/env python3
"""Replay, independently verify, and publish the measurement chain."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from common import (
    ConformanceError,
    REPOSITORY_ROOT,
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
from verify import NEGATIVES, TAMPER_IDS, verify_evidence


HERE = Path(__file__).resolve().parent
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


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    require_mount_safe_path(source)
    value = f"type=bind,source={source},target={target}"
    return value + (",readonly" if readonly else "")


def _git_state(root: Path) -> dict[str, Any]:
    return receipt_git_state(root)


def _semantic_subject() -> str:
    return semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )


def _coverage_module():
    verifier_path = REPOSITORY_ROOT / "tools/verify_semantic_coverage.py"
    spec = importlib.util.spec_from_file_location(
        "_openada_measurement_coverage_validation", verifier_path
    )
    if spec is None or spec.loader is None:
        raise ConformanceError("cannot load semantic coverage verification")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_evidence_location(
    path: Path | None, *, cache: Path, design: Path
) -> Path | None:
    roots = {
        "OpenADA checkout": REPOSITORY_ROOT.resolve(),
        "conformance cache": cache.resolve(),
        "pinned design checkout": design.resolve(),
    }
    if path is None:
        return None
    if path.expanduser().is_symlink():
        raise ConformanceError("evidence destination may not be a symbolic link")
    candidate = path.expanduser().resolve()
    for label, root in roots.items():
        if candidate == root or root in candidate.parents:
            raise ConformanceError(f"evidence destination must be outside the {label}")
    if candidate.exists():
        raise ConformanceError(f"evidence destination already exists: {candidate}")
    return candidate


def _fresh_evidence(path: Path | None) -> Path:
    if path is None:
        return Path(tempfile.mkdtemp(prefix="openada-ihp-analog-measurements-")).resolve()
    path.mkdir(parents=True, mode=0o700)
    return path


def _container_user_args(engine: str) -> list[str]:
    identity = "0:0" if Path(engine).name == "podman" else f"{os.getuid()}:{os.getgid()}"
    return ["--user", identity]


def _run_native(
    engine: str,
    manifest: dict[str, Any],
    design: Path,
    evidence: Path,
) -> tuple[dict[str, Any], list[str]]:
    name = f"openada-ihp-analog-measurements-{os.getpid()}"
    command = [
        engine,
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--platform",
        manifest["runtime"]["platform"],
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        *_container_user_args(engine),
        "--env",
        "HOME=/tmp/openada-home",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PDK=ihp-sg13g2",
        "--env",
        "PDK_ROOT=/foss/pdks",
        "--env",
        "PATH=/openada/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--tmpfs",
        # Provider invocation executes its digest-bound private launcher
        # snapshot; Docker otherwise forces noexec on a tmpfs mount.
        "/tmp:rw,nosuid,nodev,exec,size=512m",
        "--workdir",
        "/evidence",
        "--mount",
        _mount(REPOSITORY_ROOT, "/openada", readonly=True),
        "--mount",
        _mount(design, "/design", readonly=True),
        "--mount",
        _mount(evidence, "/evidence", readonly=False),
        "--entrypoint",
        "/usr/bin/python3",
        manifest["runtime"]["image_reference"],
        "/openada/conformance/ihp-analog-measurements/inside.py",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        subprocess.run(
            [engine, "rm", "-f", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raise ConformanceError(f"native container did not complete safely: {exc}") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_OBSERVATION_BYTES:
        raise ConformanceError("inside observation exceeded the runner bound")
    try:
        observation = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"inside runner returned invalid JSON: {exc}; stderr={completed.stderr[-4000:]!r}"
        ) from exc
    if not isinstance(observation, dict):
        raise ConformanceError("inside observation must be one JSON object")
    if completed.returncode != 0 or observation.get("error") is not None:
        raise ConformanceError(
            f"inside replay failed with {completed.returncode}: "
            f"{observation.get('error')!r}; stderr={completed.stderr[-4000:]!r}"
        )
    return observation, command


def _artifact(
    path: Path,
    *,
    role: str,
    source_step: str | None,
    source_output: str | None,
    replay_id: str | None = None,
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ConformanceError(f"cannot index unsafe artifact: {path}")
    return {
        "repository_path": str(path.relative_to(REPOSITORY_ROOT)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": replay_id,
    }


def _artifact_inventory(root: Path) -> list[dict[str, Any]]:
    artifacts = [
        _artifact(
            root / "contract-test.json",
            role="contract-test",
            source_step="materialize-pinned-sources",
            source_output="contract-test-verdict",
        )
    ]
    artifacts.append(
        _artifact(
            root / "design-provenance.json",
            role="design-provenance",
            source_step="materialize-pinned-sources",
            source_output="design-provenance",
        )
    )
    native = [
        ("results/ota-netlist.json", "netlist-ota", "ota-netlist-result"),
        ("work/ota.cir", "netlist-ota", "ota-deck"),
        ("results/inverter-netlist.json", "netlist-inverter", "inverter-netlist-result"),
        ("work/inverter.cir", "netlist-inverter", "inverter-deck"),
        ("results/provider-ac.json", "provider-ac", "ota-ac-provider-result"),
        ("provider-ac/work/ota_ac.raw", "provider-ac", "ota-ac-raw"),
        ("provider-ac/simulation/ota.log", "provider-ac", "ota-ac-log"),
        ("provider-ac/simulation/ota.openada-control.sp", "provider-ac", "ota-ac-launcher"),
        ("results/provider-tran.json", "provider-tran", "inverter-tran-provider-result"),
        ("provider-tran/work/inverter_spectral.raw", "provider-tran", "inverter-tran-raw"),
        ("provider-tran/simulation/inverter.log", "provider-tran", "inverter-tran-log"),
        ("provider-tran/simulation/inverter.openada-control.sp", "provider-tran", "inverter-tran-launcher"),
    ]
    for relative, step, output in native:
        artifacts.append(
            _artifact(
                root / relative,
                role="native-artifact",
                source_step=step,
                source_output=output,
            )
        )
    artifacts.append(
        _artifact(
            root / "independent-oracle.json",
            role="independent-oracle",
            source_step="independent-native-oracle",
            source_output="independent-oracle-verdict",
        )
    )
    normalized = [
        ("results/extract-ac.json", "extract-ac", "ota-ac-extraction-result"),
        ("results/extract-tran.json", "extract-tran", "inverter-tran-extraction-result"),
    ]
    for kind in (
        "low_frequency_gain_db",
        "bandwidth_3db",
        "unity_gain_frequency",
        "phase_margin",
    ):
        normalized.append(
            (
                f"results/transfer-{kind}.json",
                f"transfer-{kind.replace('_', '-')}",
                f"transfer-{kind}-result",
            )
        )
    for kind in ("snr", "sinad", "thd", "sfdr"):
        normalized.append(
            (f"results/spectral-{kind}.json", f"spectral-{kind}", f"spectral-{kind}-result")
        )
    normalized.append(
        (
            "normalized-evidence.json",
            "spectral-sfdr",
            "normalized-measurement-evidence",
        )
    )
    for relative, step, output in normalized:
        artifacts.append(
            _artifact(
                root / relative,
                role="normalized-evidence",
                source_step=step,
                source_output=output,
            )
        )
    artifacts.extend(
        [
            _artifact(
                root / "engineering-decision.json",
                role="downstream-decision",
                source_step="spectral-sfdr",
                source_output="downstream-engineering-decision",
            ),
            _artifact(
                root / "agent-evidence.json",
                role="agent-visible-evidence",
                source_step="agent-evidence",
                source_output="agent-visible-measurement-evidence",
            ),
        ]
    )
    for replay_id, (filename, _status, _diagnostic) in NEGATIVES.items():
        artifacts.append(
            _artifact(
                root / "negative" / filename,
                role="negative-replay",
                source_step=None,
                source_output=None,
                replay_id=replay_id,
            )
        )
    for replay_id in TAMPER_IDS:
        artifacts.append(
            _artifact(
                root / "tamper" / f"{replay_id}.json",
                role="tamper-replay",
                source_step=None,
                source_output=None,
                replay_id=replay_id,
            )
        )
    return artifacts


def _publish(
    evidence: Path,
    manifest_path: Path,
    *,
    source_receipt: dict[str, Any],
) -> Path:
    destination = HERE / "semantic-artifacts"
    replacement = HERE / ".semantic-artifacts.next"
    if replacement.exists():
        shutil.rmtree(replacement)
    shutil.copytree(evidence, replacement)
    if destination.exists():
        shutil.rmtree(destination)
    replacement.rename(destination)
    verify_evidence(destination, manifest_path=manifest_path)
    chain_run = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": "openada.chain/ihp-analog-measurements/v1",
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
        "artifacts": _artifact_inventory(destination),
        "extensions": {
            "org.openada.measurements": {
                "publication": f"{source_receipt['receipt_class']} fresh replay",
                "source_revision": source_receipt["repository_revision"],
            }
        },
    }
    run_path = HERE / "semantic-chain-run.json"
    _write_json(run_path, chain_run)
    return run_path


def _validate_temp_index(
    manifest_path: Path,
    run_path: Path,
    *,
    receipt_class: str = "provisional",
) -> dict[str, Any]:
    record = {
        "schema": "openada.semantic-chain-index/v0alpha1",
        "records": [
            {
                "id": f"ihp-analog-measurements-v1-{receipt_class}",
                "conformance_record_ids": [],
                "manifest": {
                    "repository_path": str(manifest_path.relative_to(REPOSITORY_ROOT)),
                    "sha256": sha256_file(manifest_path),
                    "extensions": {},
                },
                "run": {
                    "repository_path": str(run_path.relative_to(REPOSITORY_ROOT)),
                    "sha256": sha256_file(run_path),
                    "extensions": {},
                },
                "extensions": {},
            }
        ],
        "extensions": {},
    }
    with tempfile.TemporaryDirectory(prefix="openada-measurement-index-") as temporary:
        index = Path(temporary) / "index.json"
        _write_json(index, record)
        coverage = _coverage_module()
        report = coverage.build_report(
            coverage.DEFAULT_CATALOG.resolve(),
            index.resolve(),
            mode="release" if receipt_class == "release" else "audit",
        )
    if report["issues"]:
        raise ConformanceError(
            "temporary semantic index has issues: " + "; ".join(report["issues"][:8])
        )
    manifest = load_manifest(manifest_path)
    by_id = {row["row_id"]: row for row in report["rows"]}
    incomplete = [
        row_id
        for row_id in manifest["covers"]
        if by_id[row_id]["coverage_level"] != "agent-ready" or by_id[row_id]["gap"]
    ]
    if incomplete:
        raise ConformanceError(
            "temporary semantic index did not make all 47 rows agent-ready: "
            + ", ".join(incomplete[:8])
        )
    return {
        "status": report["status"],
        "chain_rows": len(manifest["covers"]),
        "chain_agent_ready_rows": len(manifest["covers"]),
        "semantic_subject_sha256": report["semantic_subject_sha256"],
        "global_gap_count_without_shared_index": report["summary"]["gap_count"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the fully verified receipt without editing the shared index.",
    )
    parser.add_argument(
        "--receipt-class",
        choices=("provisional", "release"),
        default="provisional",
        help="Release requires an unchanged clean source checkout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        cache = args.cache_dir.expanduser().resolve()
        ensure_external_cache(cache, REPOSITORY_ROOT)
        design = ensure_external_design_path(
            cache / "IHP-AnalogAcademy", REPOSITORY_ROOT, cache
        )
        verify_design_checkout(design, manifest)
        inspect_image(args.container_engine, manifest)
        requested = _validate_evidence_location(
            args.evidence_dir, cache=cache, design=design
        )
        evidence = _fresh_evidence(requested)
        before = _git_state(REPOSITORY_ROOT)
        semantic_subject_before = _semantic_subject()
        _write_json(
            evidence / "design-provenance.json",
            design_provenance(design, manifest["design"]),
        )
        observation, container_command = _run_native(
            args.container_engine, manifest, design, evidence
        )
        _write_json(evidence / "runtime-observation.json", observation)
        _write_json(
            evidence / "run-metadata.json",
            {
                "schema": "openada.ihp-analog-measurement-run/v0alpha1",
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "chain_manifest_sha256": sha256_file(manifest_path),
                "design_revision": manifest["design"]["revision"],
                "image_reference": manifest["runtime"]["image_reference"],
                "network": "none during all EDA execution",
                "container_command": container_command,
                "checkout_before": before,
                "semantic_subject_before": semantic_subject_before,
                "extensions": {},
            },
        )
        report = verify_evidence(
            evidence,
            manifest_path=manifest_path,
            materialize=True,
            run_tamper_probes=True,
        )
        after = _git_state(REPOSITORY_ROOT)
        semantic_subject_after = _semantic_subject()
        if semantic_subject_before != semantic_subject_after:
            raise ConformanceError(
                "the semantic execution subject changed during the native replay"
            )
        source_receipt = source_attestation(
            before,
            after,
            semantic_subject_sha256=semantic_subject_after,
            receipt_class=args.receipt_class,
        )
        published: dict[str, Any] | None = None
        if args.publish:
            run_path = _publish(
                evidence, manifest_path, source_receipt=source_receipt
            )
            published = {
                "semantic_artifacts": str(HERE / "semantic-artifacts"),
                "chain_run": str(run_path),
                "chain_run_sha256": sha256_file(run_path),
                "temporary_index_validation": _validate_temp_index(
                    manifest_path,
                    run_path,
                    receipt_class=args.receipt_class,
                ),
            }
    except (ConformanceError, SemanticReceiptError, OSError, ValueError, KeyError) as exc:
        print(f"measurement replay failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "pass",
                "evidence": str(evidence),
                "verification": report["status"],
                "transfer": report["oracle"]["transfer"]["metrics"],
                "spectral": report["oracle"]["spectral"]["metrics"],
                "published": published,
            },
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
