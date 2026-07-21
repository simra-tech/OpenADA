#!/usr/bin/env python3
"""Run, independently verify, and publish the four-analysis provider chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from oracle import OracleError, verify as verify_native
from verify import PublicationError, run_tamper_probes, verify_publication


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from semantic_receipts import (  # noqa: E402
    SemanticReceiptError,
    design_provenance,
    git_state,
    semantic_subject as receipt_semantic_subject,
    source_attestation,
)
CHAIN_ID = "openada.chain/ihp-ngspice-provider-analyses/v1"
REVISION = "133ecf657572e021b5921b5a1b7693abfb209623"
IMAGE = (
    "hpretl/iic-osic-tools@sha256:"
    "fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
CONFORMANCE_ID = "org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1"
ANALYSES = ("op", "dc", "ac", "tran")
FEATURES = {
    name: f"openada.feature/simulation.analysis.{name}/v1alpha1"
    for name in ANALYSES
}
GENERAL_ROWS = [
    "surface|openada.surface/cli.provider-invoke/v1",
    "surface-variant|openada.surface/cli.provider-invoke/v1|explicit-local-provider",
    "profile|openada.operation/circuit.simulate/v1alpha2",
    "assertion|openada.operation/circuit.simulate/v1alpha2|openada.assertion/simulation.evidence.valid/v1alpha1",
    "preflight|spice-analysis-evidence-valid",
    f"provider-conformance|org.openada.driver.ngspice-pdk-control|{CONFORMANCE_ID}",
]
ANALYSIS_ROWS = {
    name: [
        "repository-provider|org.openada.driver.ngspice-pdk-control|"
        "openada.operation/circuit.simulate/v1alpha2|"
        "openada.assertion/simulation.evidence.valid/v1alpha1|"
        + FEATURES[name],
        "feature|openada.operation/circuit.simulate/v1alpha2|" + FEATURES[name],
    ]
    for name in ANALYSES
}
DESIGN_INPUTS = {
    "modules/module_0_foundations/inverter/inverter_tb.sch": "521464a42c5352cad371a8b091d71d9a083686749ef49c69b3f07ec838a3cb82",
    "modules/module_0_foundations/inverter/inverter.sch": "6a2e03f44df59976b8ba4fca385b104b80802d367789171790bd238f912ec771",
    "modules/module_0_foundations/inverter/inverter.sym": "8658fa30ac994a0bedf511e59f064e6458e9b4a30d91382f6974a5117bd5c103",
    "modules/module_1_bandgap_reference/part_1_OTA/testbenches/ota_testbench.sch": "861a27e4fcfec15e267ed18816db791460aae1f94f1a7c83ca0c114d4379d4bc",
    "modules/module_1_bandgap_reference/part_1_OTA/schematic/two_stage_OTA.sch": "4385e81ad81c229b81095fe2060153b2817376cfcfbb6d9adfc908d58d7e34a7",
    "modules/module_1_bandgap_reference/part_1_OTA/schematic/two_stage_OTA.sym": "d6cbae50c9f6a425bab127f0644c5959c98c13893442420b6b25531af0d64126",
}


class ReplayError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def file_ref(path: Path) -> dict[str, Any]:
    return {
        "repository_path": str(path.relative_to(ROOT)),
        "sha256": sha256(path),
        "extensions": {},
    }


def validate_checkout(path: Path) -> None:
    if not (path / ".git").is_dir():
        raise ReplayError(f"missing IHP checkout: {path}")
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    if head != REVISION or status:
        raise ReplayError("IHP checkout must be clean and pinned to the reviewed revision")
    expected = {"LICENSE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4", **DESIGN_INPUTS}
    for relative, digest in expected.items():
        candidate = path / relative
        if not candidate.is_file() or candidate.is_symlink() or sha256(candidate) != digest:
            raise ReplayError(f"pinned design input drift: {relative}")


def mount(source: Path, target: str, *, readonly: bool) -> str:
    value = f"type=bind,source={source.resolve()},target={target}"
    return value + (",readonly" if readonly else "")


def _container_user_args(engine: str) -> list[str]:
    identity = "0:0" if Path(engine).name == "podman" else f"{os.getuid()}:{os.getgid()}"
    return ["--user", identity]


def run_native(design: Path, evidence: Path, engine: str) -> None:
    command = [
        engine,
        "run",
        "--rm",
        "--pull=never",
        "--platform",
        "linux/amd64",
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
        # The provider runtime executes a digest-bound, mode-0500 private
        # snapshot of the pinned launcher. Docker otherwise mounts --tmpfs
        # with noexec, which defeats that identity-preserving launch boundary.
        "/tmp:rw,nosuid,nodev,exec,size=512m",
        "--workdir",
        "/evidence",
        "--mount",
        mount(ROOT, "/openada", readonly=True),
        "--mount",
        mount(HERE / "rcfiles/ota.xschemrc", "/ota-rc", readonly=True),
        "--mount",
        mount(HERE / "rcfiles/inverter.xschemrc", "/inverter-rc", readonly=True),
        "--mount",
        mount(design, "/design", readonly=True),
        "--mount",
        mount(evidence, "/evidence", readonly=False),
        "--entrypoint",
        "/usr/bin/python3",
        IMAGE,
        "/openada/conformance/ihp-ngspice-provider-analyses/inside.py",
    ]
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if completed.returncode != 0:
        raise ReplayError(
            f"native container failed ({completed.returncode}): {completed.stderr[-4000:]}"
        )
    value = json.loads(completed.stdout)
    if value.get("status") != "pass":
        raise ReplayError("native container did not return pass")


def contract_tests() -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_ngspice_provider.py",
        "tests/test_ngspice_outputs.py",
        "-q",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if completed.returncode != 0:
        raise ReplayError(f"contract tests failed:\n{completed.stdout[-4000:]}")
    return {
        "schema": "openada.contract-test-report/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "suites": [
            {
                "repository_path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256(path),
            }
            for path in (
                ROOT / "tests/test_ngspice_provider.py",
                ROOT / "tests/test_ngspice_outputs.py",
            )
        ],
        "extensions": {},
    }


def semantic_subject() -> str:
    return receipt_semantic_subject(
        ROOT, ROOT / "catalog/semantic-surfaces-v0alpha1.json"
    )


def _manifest_design() -> dict[str, Any]:
    return {
        "class": "public-design",
        "repository": "https://github.com/IHP-GmbH/IHP-AnalogAcademy.git",
        "revision": REVISION,
        "tree": "2a710fd503226e9642e4337a324e6c192a9d8a31",
        "subtree": "modules",
        "license": {
            "spdx": "Apache-2.0",
            "path": "LICENSE",
            "sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        },
        "inputs": [
            {"path": relative, "sha256": digest}
            for relative, digest in DESIGN_INPUTS.items()
        ],
        "extensions": {},
    }


def step(
    identifier: str,
    kind: str,
    native: bool,
    covers: list[str],
    consumes: list[str],
    produces: list[str],
    *,
    request: bool = False,
    implementation: Path | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": identifier,
        "kind": kind,
        "native_execution": native,
        "covers": covers,
        "consumes": consumes,
        "produces": produces,
        "extensions": {},
    }
    if request:
        value["request"] = file_ref(HERE / "contract.json")
    if implementation is not None:
        value["implementation"] = file_ref(implementation)
    return value


def publish(
    evidence: Path,
    design: Path,
    source_receipt: dict[str, Any],
    contract_report: dict[str, Any],
    tamper_receipts: dict[str, dict[str, Any]],
) -> None:
    destination = HERE / "artifacts"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(evidence, destination / "native-replay")
    oracle, normalized, decision = verify_native(destination / "native-replay")
    write_json(destination / "oracle.json", oracle)
    write_json(destination / "normalized.json", normalized)
    write_json(destination / "decision.json", decision)
    agent = {
        "schema": "openada.agent-evidence/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "chain_id": CHAIN_ID,
        "provider": normalized["provider"],
        "analyses": normalized["analyses"],
        "decisions": decision["decisions"],
        "limitations": decision["limitations"],
        "negative_replays": oracle["negative_replays"],
        "standards_assessment": normalized["standards_assessment"],
        "recommended_next_actions": [
            "Use OP only for nominal bias inspection.",
            "Use DC only for the pinned nominal inverter transfer curve.",
            "Use AC gain/crossover estimates only within the pinned OTA testbench and simulated band.",
            "Run explicit PVT, Monte Carlo, extracted, and specification workflows before design signoff.",
        ],
        "extensions": {},
    }
    write_json(destination / "agent-evidence.json", agent)
    write_json(destination / "contract-tests.json", contract_report)
    write_json(
        destination / "design-provenance.json",
        design_provenance(design, _manifest_design()),
    )

    negative_sources = {
        "op-unsafe-command": "op-unsafe-result.json",
        "dc-parameter-mismatch": "dc-mismatch-result.json",
        "ac-feature-mismatch": "ac-feature-result.json",
        "tran-duplicate-write": "tran-duplicate-result.json",
        "tran-native-error": "tran-native-error-result.json",
    }
    (destination / "negative").mkdir()
    for replay_id, name in negative_sources.items():
        shutil.copy2(
            destination / "native-replay/negative" / name,
            destination / "negative" / f"{replay_id}.json",
        )

    for replay_id, receipt in tamper_receipts.items():
        write_json(destination / "tamper" / f"{replay_id}.json", receipt)

    covers = sorted(set(GENERAL_ROWS + [row for rows in ANALYSIS_ROWS.values() for row in rows]))
    native_outputs = {
        name: [
            f"{name}-deck",
            f"{name}-request",
            f"{name}-result",
            f"{name}-raw",
            f"{name}-log",
            f"{name}-launcher",
        ]
        for name in ANALYSES
    }
    steps = [
        step(
            "materialize-pinned-sources",
            "source-materialize",
            False,
            [],
            [],
            [
                "pinned-inverter",
                "pinned-ota",
                "pinned-pdk",
                "pinned-runtime",
                "design-provenance",
            ],
        )
    ]
    for name in ANALYSES:
        # The conformance record is a joint four-analysis claim, so every
        # native slice explicitly participates in its positive coverage.
        rows = [*ANALYSIS_ROWS[name], GENERAL_ROWS[-1]]
        if name == "op":
            rows.extend(GENERAL_ROWS[:-1])
        steps.append(
            step(
                f"provider-{name}",
                "semantic-command",
                True,
                rows,
                ["pinned-inverter" if name != "ac" else "pinned-ota", "pinned-pdk", "pinned-runtime"],
                native_outputs[name],
                request=True,
                implementation=HERE / "inside.py",
            )
        )
    all_native = [item for outputs in native_outputs.values() for item in outputs]
    steps.extend(
        [
            step(
                "independent-native-oracle",
                "independent-oracle",
                False,
                [],
                all_native,
                ["independent-oracle"],
                implementation=HERE / "oracle.py",
            ),
            step(
                "normalize-evidence",
                "semantic-command",
                False,
                [GENERAL_ROWS[2], GENERAL_ROWS[3]],
                ["independent-oracle"],
                ["normalized-evidence"],
                request=True,
                implementation=HERE / "replay.py",
            ),
            step(
                "publish-scoped-decision",
                "semantic-command",
                False,
                [GENERAL_ROWS[2]],
                ["normalized-evidence", "independent-oracle"],
                ["downstream-decision"],
                request=True,
                implementation=HERE / "replay.py",
            ),
            step(
                "agent-decision",
                "independent-decision",
                False,
                [],
                ["downstream-decision", "normalized-evidence", "independent-oracle"],
                ["agent-visible-evidence"],
                implementation=HERE / "replay.py",
            ),
            step(
                "contract-tests",
                "independent-oracle",
                False,
                [],
                ["agent-visible-evidence"],
                ["contract-test-report"],
                implementation=HERE / "replay.py",
            ),
            step(
                "publication-verifier",
                "independent-oracle",
                False,
                [],
                [
                    "agent-visible-evidence",
                    "contract-test-report",
                    "design-provenance",
                ],
                ["publication-verdict"],
                implementation=HERE / "verify.py",
            ),
        ]
    )
    generic_negative_rows = GENERAL_ROWS
    negative_replays = []
    for name, replay_id, diagnostic in (
        ("op", "op-unsafe-command", "simulation.request.invalid"),
        ("dc", "dc-parameter-mismatch", "simulation.request.invalid"),
        ("ac", "ac-feature-mismatch", "simulation.request.invalid"),
        ("tran", "tran-duplicate-write", "simulation.request.invalid"),
        ("tran", "tran-native-error", "simulation.result.malformed"),
    ):
        replay_covers = list(ANALYSIS_ROWS[name])
        if name == "op":
            replay_covers.extend(generic_negative_rows)
        negative_replays.append(
            {
                "id": replay_id,
                "covers": replay_covers,
                "expected_status": "unknown",
                "required_diagnostic": diagnostic,
                "extensions": {},
            }
        )
    tamper_replays = []
    for name, replay_id in (
        ("op", "op-raw-byte"),
        ("dc", "dc-request-feature"),
        ("ac", "ac-result-digest"),
        ("tran", "tran-raw-header"),
        ("op", "provider-version"),
    ):
        replay_covers = list(ANALYSIS_ROWS[name])
        if name == "op":
            replay_covers.extend(GENERAL_ROWS)
        tamper_replays.append(
            {
                "id": replay_id,
                "covers": replay_covers,
                "expected_status": "invalid_request",
                "required_diagnostic": "evidence.binding.invalid",
                "extensions": {},
            }
        )

    contracts = [
        ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json",
        ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json",
        ROOT / "schemas/design-provenance-v0alpha1.schema.json",
        ROOT / "schemas/result-v0alpha1.schema.json",
        ROOT / "tools/semantic_receipts.py",
        ROOT / "profiles/circuit.simulate-v1alpha2.json",
        ROOT / "providers/ngspice-pdk-control/provider-config-v0alpha1.schema.json",
        HERE / "contract.json",
        HERE / "inside.py",
        HERE / "oracle.py",
        HERE / "replay.py",
        HERE / "verify.py",
        HERE / "rcfiles/inverter.xschemrc",
        HERE / "rcfiles/ota.xschemrc",
        ROOT / "tests/test_ngspice_provider.py",
        ROOT / "tests/test_ngspice_outputs.py",
    ]
    manifest = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain/v0alpha1",
        "id": CHAIN_ID,
        "design": _manifest_design(),
        "runtime": {
            "image_reference": IMAGE,
            "image_config_digest": "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb",
            "platform": "linux/amd64",
            "pdk_revision": "144f811cdffda49b71d28f64e8a92b697b61cf06",
            "tools": [
                {"id": "xschem", "path": "/foss/tools/xschem/bin/xschem", "version": "XSCHEM V3.4.8RC"},
                {"id": "ngspice", "path": "/foss/tools/ngspice/bin/ngspice", "version": "** ngspice-46 : Circuit level simulation program"},
            ],
            "extensions": {},
        },
        "contracts": [file_ref(path) for path in contracts],
        "covers": covers,
        "steps": steps,
        "negative_replays": negative_replays,
        "tamper_replays": tamper_replays,
        "agent_evidence": {
            "result_step": "agent-decision",
            "required_json_pointers": [
                "/provider/driver_id",
                "/analyses/op/point_count",
                "/analyses/dc/point_count",
                "/analyses/ac/point_count",
                "/analyses/tran/point_count",
                "/decisions",
                "/limitations",
                "/standards_assessment",
                "/recommended_next_actions",
            ],
            "extensions": {},
        },
        "release_verification": {
            "implementation": file_ref(HERE / "verify.py"),
            "arguments": [
                "conformance/ihp-ngspice-provider-analyses/artifacts"
            ],
            "timeout_seconds": 120,
            "extensions": {},
        },
        "extensions": {},
    }
    manifest_path = HERE / "manifest.json"
    write_json(manifest_path, manifest)

    artifacts: list[dict[str, Any]] = []
    native_root = destination / "native-replay"
    artifacts.append(
        artifact(
            destination / "design-provenance.json",
            "design-provenance",
            "materialize-pinned-sources",
            "design-provenance",
        )
    )
    for name in ANALYSES:
        entries = [
            (native_root / f"decks/{name}.spice", f"{name}-deck"),
            (native_root / f"requests/{name}.json", f"{name}-request"),
            (native_root / f"results/{name}.json", f"{name}-result"),
            (native_root / f"native/{name}/work/{name}.raw", f"{name}-raw"),
            (native_root / f"native/{name}/simulation/{name}.log", f"{name}-log"),
            (
                native_root / f"native/{name}/simulation/{name}.openada-control.sp",
                f"{name}-launcher",
            ),
        ]
        for path, output in entries:
            artifacts.append(artifact(path, "native-artifact", f"provider-{name}", output))
    artifacts.extend(
        [
            artifact(destination / "oracle.json", "independent-oracle", "independent-native-oracle", "independent-oracle"),
            artifact(destination / "normalized.json", "normalized-evidence", "normalize-evidence", "normalized-evidence"),
            artifact(destination / "decision.json", "downstream-decision", "publish-scoped-decision", "downstream-decision"),
            artifact(destination / "agent-evidence.json", "agent-visible-evidence", "agent-decision", "agent-visible-evidence"),
            artifact(destination / "contract-tests.json", "contract-test", "contract-tests", "contract-test-report"),
        ]
    )
    for replay in negative_replays:
        artifacts.append(replay_artifact(destination / "negative" / f"{replay['id']}.json", "negative-replay", replay["id"]))
    for replay in tamper_replays:
        artifacts.append(replay_artifact(destination / "tamper" / f"{replay['id']}.json", "tamper-replay", replay["id"]))
    run = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain-run/v0alpha1",
        "chain_id": CHAIN_ID,
        "chain_manifest_sha256": sha256(manifest_path),
        "semantic_subject_sha256": semantic_subject(),
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
                "receipt_status": source_receipt["receipt_class"],
                "publication_verifier": "conformance/ihp-ngspice-provider-analyses/verify.py",
            }
        },
    }
    write_json(HERE / "semantic-chain-run.json", run)
    verify_publication(
        destination,
        allow_provisional=source_receipt["receipt_class"] == "provisional",
    )


def artifact(path: Path, role: str, source_step: str, source_output: str) -> dict[str, Any]:
    return {
        "repository_path": str(path.relative_to(ROOT)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "role": role,
        "source_step": source_step,
        "source_output": source_output,
        "replay_id": None,
    }


def replay_artifact(path: Path, role: str, replay_id: str) -> dict[str, Any]:
    return {
        "repository_path": str(path.relative_to(ROOT)),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "role": role,
        "source_step": None,
        "source_output": None,
        "replay_id": replay_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--design-dir",
        type=Path,
        default=Path.home() / ".cache/openada/conformance/ihp-inverter/IHP-AnalogAcademy",
    )
    parser.add_argument("--container-engine", default="docker")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--skip-native", action="store_true")
    parser.add_argument(
        "--receipt-class",
        choices=("provisional", "release"),
        default="provisional",
    )
    args = parser.parse_args()
    design = args.design_dir.resolve()
    validate_checkout(design)
    if args.evidence_dir:
        evidence = args.evidence_dir.resolve()
        if evidence.exists() and not args.skip_native:
            raise ReplayError("evidence directory must be fresh")
        evidence.mkdir(parents=True, exist_ok=args.skip_native)
        cleanup = False
    else:
        evidence = Path(tempfile.mkdtemp(prefix="openada-ihp-provider-analyses-"))
        cleanup = not args.publish
    try:
        source_before = git_state(ROOT)
        if not args.skip_native:
            run_native(design, evidence, args.container_engine)
        verify_native(evidence)
        if args.publish:
            contract_report = contract_tests()
            tamper_receipts = run_tamper_probes(evidence)
            source_after = git_state(ROOT)
            source_receipt = source_attestation(
                source_before,
                source_after,
                semantic_subject_sha256=semantic_subject(),
                receipt_class=args.receipt_class,
            )
            publish(
                evidence,
                design,
                source_receipt,
                contract_report,
                tamper_receipts,
            )
        print(json.dumps({"status": "pass", "evidence": str(evidence), "published": args.publish}))
    finally:
        if cleanup:
            shutil.rmtree(evidence)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        OracleError,
        PublicationError,
        ReplayError,
        SemanticReceiptError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        sys.stderr.write(f"ihp-ngspice-provider-analyses: {exc}\n")
        raise SystemExit(2)
