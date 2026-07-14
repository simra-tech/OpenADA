from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "evaluation" / "paired-agent"
if str(EVALUATION) not in sys.path:
    sys.path.insert(0, str(EVALUATION))

from assemble import assemble_trial  # noqa: E402
from common import (  # noqa: E402
    EvaluationError,
    canonical_json_sha256,
    load_json,
    seal_summary,
    seal_trial,
    treatment_skill_tree_sha256,
    validate_campaign,
    validate_plan,
    verify_summary_seal,
)
from plan import build_plan  # noqa: E402
import summarize as summarize_module  # noqa: E402
from summarize import build_summary as _build_summary  # noqa: E402
import verify_summary as verify_summary_module  # noqa: E402
from verify_summary import authenticate_summary, verify_full_summary  # noqa: E402

ADAPTERS = EVALUATION / "adapters"
if str(ADAPTERS) not in sys.path:
    sys.path.insert(0, str(ADAPTERS))
from codex_jsonl import rejected_trace  # noqa: E402


CREATED = "2026-07-14T12:00:00Z"
PLAN_CREATED = "2026-07-14T12:00:01Z"
ASSEMBLED = "2026-07-14T12:10:00Z"
SEED_HEX = "01" * 32
ACTION_KINDS = (
    "agent_message",
    "reasoning",
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
    "error",
)
SIGNING_SEED = bytes.fromhex("02" * 32)


def build_summary(**kwargs: object) -> dict:
    return _build_summary(**kwargs, private_seed=SIGNING_SEED)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _reseal(trial: dict, campaign: dict) -> None:
    seal_trial(trial, campaign, private_seed=SIGNING_SEED)


def _copy_task(tmp_path: Path) -> None:
    source = EVALUATION / "tasks" / "ihp-inverter-sim"
    for name in ("manifest.json", "task.md", "submission.schema.json", "native_score.py"):
        shutil.copy2(source / name, tmp_path / name)


def _campaign(tmp_path: Path, *, pairs: int = 5) -> tuple[Path, dict]:
    _copy_task(tmp_path)
    private_key = Ed25519PrivateKey.from_private_bytes(SIGNING_SEED)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signing_key = tmp_path / "trial-signing-key.txt"
    signing_key.write_text(SIGNING_SEED.hex() + "\n", encoding="ascii")
    signing_key.chmod(0o600)
    participant_files = [
        {
            "role": "openada-cli",
            "participant_path": "/opt/openada/bin/openada",
            "bytes": 100,
            "sha256": "a" * 64,
            "mode": "0555",
            "media_type": "application/x-executable",
        },
        {
            "role": "openada-package",
            "participant_path": "/opt/openada/site/openada/__init__.py",
            "bytes": 20,
            "sha256": "b" * 64,
            "mode": "0444",
            "media_type": "text/x-python",
        },
        {
            "role": "openada-schema",
            "participant_path": "/opt/openada/share/result-v0alpha1.schema.json",
            "bytes": 30,
            "sha256": "c" * 64,
            "mode": "0444",
            "media_type": "application/schema+json",
        },
        {
            "role": "openada-skill",
            "participant_path": "/opt/openada/skill/SKILL.md",
            "bytes": 40,
            "sha256": "d" * 64,
            "mode": "0444",
            "media_type": "text/markdown",
        },
    ]
    treatment_manifest = {
        "schema": "openada.eval.treatment-manifest/v0alpha1",
        "distribution": {
            "filename": "openada-0.1.0-py3-none-any.whl",
            "bytes": 1000,
            "sha256": "9" * 64,
            "media_type": "application/vnd.python.wheel",
        },
        "participant_files": participant_files,
    }
    treatment_manifest_path = tmp_path / "treatment-manifest.json"
    _write(treatment_manifest_path, treatment_manifest)
    task_manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    task_inputs = {item["role"]: item for item in task_manifest["inputs"]}
    campaign = {
        "schema": "openada.eval.campaign/v0alpha1",
        "campaign_id": "paired-contract-test",
        "created_at": CREATED,
        "task": {
            "id": "ihp-inverter-xschem-ngspice-paired",
            "manifest": {"path": "manifest.json", "sha256": _sha(tmp_path / "manifest.json")},
            "prompt": {"path": "task.md", "sha256": _sha(tmp_path / "task.md")},
            "response_schema": {
                "path": "submission.schema.json",
                "sha256": _sha(tmp_path / "submission.schema.json"),
            },
            "scorer": {
                "path": "native_score.py",
                "sha256": _sha(tmp_path / "native_score.py"),
            },
        },
        "planned_pairs": pairs,
        "agent": {
            "provider": "test-provider",
            "model": "test-model",
            "release": "test-model-2026-07-14",
            "release_is_immutable": True,
            "reasoning": "high",
            "harness": {
                "name": "codex",
                "version": "0.144.3",
                "binary_sha256": "4" * 64,
            },
            "adapter": {
                "name": "codex-jsonl",
                "version": "1",
                "sha256": _sha(EVALUATION / "adapters" / "codex_jsonl.py"),
            },
        },
        "trial_signing": {
            "algorithm": "ed25519",
            "public_key_hex": public_key.hex(),
            "key_id": hashlib.sha256(public_key).hexdigest(),
        },
        "execution_clock": {
            "domain_id": "d" * 64,
            "origin": "first_dispatch_zero_ms",
        },
        "runtime": {
            "image": {
                "reference": task_manifest["runtime"]["image_reference"],
                "digest": task_manifest["runtime"]["image_reference"].rsplit("@", 1)[1],
                "config_digest": task_manifest["runtime"]["image_config_digest"],
                "os": "linux",
                "architecture": "amd64",
            },
            "design": {
                "repository": task_manifest["design"]["repository"],
                "revision": task_manifest["design"]["revision"],
            },
            "pdk": {
                "name": task_manifest["runtime"]["pdk"],
                "revision": task_manifest["runtime"]["pdk_revision"],
                "identity_files": [
                    {
                        "path": task_inputs[role]["path"],
                        "sha256": task_inputs[role]["sha256"],
                    }
                    for role in ("xschem-rcfile", "pdk-revision", "ngspice-init")
                ],
            },
            "tools": [
                dict(tool) for tool in task_manifest["tools"]
            ],
            "startup_files": [
                {
                    "path": task_inputs["ngspice-system-init"]["path"],
                    "sha256": task_inputs["ngspice-system-init"]["sha256"],
                }
            ],
        },
        "treatment": {
            "openada_commit": "8" * 40,
            "openada_version": "0.1.0",
            "result_schema": "openada.result/v0alpha1",
            "bundle_manifest": {
                "path": "treatment-manifest.json",
                "sha256": _sha(treatment_manifest_path),
            },
            "distribution_sha256": "9" * 64,
            "cli_sha256": "a" * 64,
            "skill_tree_sha256": treatment_skill_tree_sha256(participant_files),
        },
        "policies": {
            "task_network": "none",
            "model_transport_network": "provider-only",
            "approval_policy": "never",
            "source_workspace": "fresh-read-only",
            "output_workspace": "fresh-writable",
            "condition_isolation": "raw-absent-treatment-exact",
            "user_intervention": "none",
            "pair_max_span_seconds": 3600,
        },
        "budgets": {
            "wall_time_seconds": 600,
            "max_agent_actions": 100,
            "max_api_requests": 100,
            "max_input_tokens": 1_000_000,
            "max_output_tokens": 100_000,
        },
        "primary_metric": "verified_artifact_complete",
        "metrics": [
            {
                "id": "verified_artifact_complete",
                "kind": "boolean",
                "direction": "higher",
                "primary": True,
            },
            {
                "id": "wall_time_ms",
                "kind": "integer",
                "direction": "lower",
                "primary": False,
            },
            {
                "id": "agent_action_count",
                "kind": "integer",
                "direction": "lower",
                "primary": False,
            },
            {
                "id": "provider_request_count",
                "kind": "integer",
                "direction": "lower",
                "primary": False,
            },
        ],
    }
    path = tmp_path / "campaign.json"
    _write(path, campaign)
    validate_campaign(campaign, campaign_path=path)
    return path, campaign


def _plan(campaign_path: Path, campaign: dict) -> tuple[Path, dict, str]:
    _, campaign_sha, _ = load_json(campaign_path)
    plan = build_plan(
        campaign,
        campaign_sha,
        seed=bytes.fromhex(SEED_HEX),
        created_at=PLAN_CREATED,
    )
    path = campaign_path.parent / "plan.json"
    _write(path, plan)
    _, plan_sha, _ = load_json(path)
    return path, plan, plan_sha


def _trace() -> dict:
    return {
        "schema": "openada.eval.trace/v0alpha1",
        "source": {
            "kind": "codex_exec_jsonl",
            "codex_cli_version": "0.144.3",
            "parser_version": 1,
        },
        "stream": {
            "complete": True,
            "event_count": 3,
            "action_count": 0,
            "action_record_count": 0,
            "terminal": "completed",
            "process_exit_bucket": "zero",
            "issues": [],
        },
        "identity": {
            "native_thread_observed": True,
            "native_turn_observed": False,
            "api_request_observed": False,
            "execution_context_observed": False,
            "identifiers_synthesized": False,
            "fresh_single_turn_declared": True,
        },
        "actions": [],
        "aggregates": {
            "action_counts": {kind: 0 for kind in ACTION_KINDS},
            "command_result_observed_characters": 0,
        },
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 1,
        },
        "usage_observed_valid": True,
        "adapter_duration_ms": 1000,
        "eligibility": {
            "action_metrics": {"eligible": True, "reasons": []},
            "usage_metrics": {"eligible": True, "reasons": []},
            "duration_metrics": {"eligible": True, "reasons": []},
            "session_metrics": {
                "eligible": False,
                "reasons": ["execution_context_unavailable", "native_turn_id_unavailable"],
            },
            "provider_request_metrics": {
                "eligible": False,
                "reasons": [
                    "api_request_id_unavailable",
                    "provider_request_events_unavailable",
                ],
            },
            "engineering_outcome_metrics": {
                "eligible": False,
                "reasons": ["independent_evidence_required"],
            },
            "model_context_bytes_metrics": {
                "eligible": False,
                "reasons": ["model_context_bytes_unavailable"],
            },
        },
    }


def _assemble(
    root: Path,
    campaign_path: Path,
    plan_path: Path,
    plan: dict,
    plan_sha: str,
    assignment: dict,
    *,
    raw_contaminated: bool = False,
    treatment_used: bool = False,
    termination: str = "completed",
    submission_payload: bytes = b"{}\n",
    pair_span_exceeded: bool = False,
    pair_order_reversed: bool = False,
    attempt_count: int = 1,
    wall_time_ms: int = 1000,
    usage_override: dict[str, int] | None = None,
    attestation_overrides: dict[str, bool] | None = None,
) -> dict:
    directory = root / assignment["trial_id"]
    directory.mkdir(parents=True)
    workspace = directory / "workspace"
    workspace.mkdir()
    trace_path = directory / "trace.json"
    submission_path = directory / "submission.json"
    trace_document = _trace()
    if termination == "adapter_failed":
        trace_document = rejected_trace(process_exit_code=0, fresh_thread=True)
    elif termination != "completed":
        trace_document["stream"]["terminal"] = "failed"
        trace_document["stream"]["process_exit_bucket"] = "nonzero"
        trace_document["usage"] = None
        trace_document["usage_observed_valid"] = False
        trace_document["eligibility"]["usage_metrics"] = {
            "eligible": False,
            "reasons": ["terminal_not_completed", "usage_missing_or_invalid"],
        }
    elif usage_override is not None:
        trace_document["usage"] = dict(usage_override)
    _write(trace_path, trace_document)
    submission_path.write_bytes(submission_payload)
    _, campaign_sha, _ = load_json(campaign_path)
    _, trace_sha, trace_bytes = load_json(trace_path)
    submission_bytes = len(submission_payload)
    submission_sha = hashlib.sha256(submission_payload).hexdigest()
    is_treatment = assignment["condition"] == "openada"
    present = is_treatment or raw_contaminated
    pair_assignments = sorted(
        (item for item in plan["assignments"] if item["pair_id"] == assignment["pair_id"]),
        key=lambda item: item["pair_position"],
    )
    pair_index = (min(item["sequence"] for item in pair_assignments) - 1) // 2
    first_started = datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc) + timedelta(
        minutes=pair_index * 2
    )
    durations = {
        item["trial_id"]: wall_time_ms if item["trial_id"] == assignment["trial_id"] else 1000
        for item in pair_assignments
    }
    first_finished = first_started + timedelta(
        milliseconds=durations[pair_assignments[0]["trial_id"]]
    )
    gap_ms = 3_600_001 if pair_span_exceeded else 100
    second_started = first_finished + timedelta(milliseconds=gap_ms)
    second_finished = second_started + timedelta(
        milliseconds=durations[pair_assignments[1]["trial_id"]]
    )
    monotonic_base = pair_index * 120_000
    first_monotonic_finished = (
        monotonic_base + durations[pair_assignments[0]["trial_id"]]
    )
    second_monotonic_started = first_monotonic_finished + gap_ms
    second_monotonic_finished = (
        second_monotonic_started + durations[pair_assignments[1]["trial_id"]]
    )
    timestamp = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    execution_records = [
        {
            "trial_id": pair_assignments[0]["trial_id"],
            "started_at": timestamp(first_started),
            "finished_at": timestamp(first_finished),
            "monotonic_started_ms": monotonic_base,
            "monotonic_finished_ms": first_monotonic_finished,
        },
        {
            "trial_id": pair_assignments[1]["trial_id"],
            "started_at": timestamp(second_started),
            "finished_at": timestamp(second_finished),
            "monotonic_started_ms": second_monotonic_started,
            "monotonic_finished_ms": second_monotonic_finished,
        },
    ]
    if pair_order_reversed:
        execution_records.reverse()
    current_execution = next(
        item for item in execution_records if item["trial_id"] == assignment["trial_id"]
    )
    started = datetime.fromisoformat(current_execution["started_at"].replace("Z", "+00:00"))
    finished = datetime.fromisoformat(current_execution["finished_at"].replace("Z", "+00:00"))
    clock_domain_id = "d" * 64
    supervisor = {
        "schema": "openada.eval.supervisor/v0alpha1",
        "campaign_id": "paired-contract-test",
        "campaign_sha256": campaign_sha,
        "plan_sha256": plan_sha,
        "pair_id": assignment["pair_id"],
        "trial_id": assignment["trial_id"],
        "condition": assignment["condition"],
        "created_at": timestamp(max(first_finished, second_finished) + timedelta(seconds=1)),
        "timing": {
            "started_at": timestamp(started),
            "finished_at": timestamp(finished),
            "wall_time_ms": wall_time_ms,
        },
        "attempt": {
            "attempt_count": attempt_count,
            "assignment_launched_once": attempt_count == 1,
            "prior_attempt_absent": attempt_count == 1,
        },
        "dispatch": {
            "sequence_observed": assignment["sequence"],
            "clock_domain_id": clock_domain_id,
            "monotonic_started_ms": current_execution["monotonic_started_ms"],
            "monotonic_finished_ms": current_execution["monotonic_finished_ms"],
        },
        "pair_execution": {
            "clock_domain_id": clock_domain_id,
            "first": execution_records[0],
            "second": execution_records[1],
        },
        "termination": termination,
        "files": {
            "trace": {
                "path": str(trace_path.resolve()),
                "bytes": trace_bytes,
                "sha256": trace_sha,
            },
            "submission": {
                "path": str(submission_path.resolve()),
                "bytes": submission_bytes,
                "sha256": submission_sha,
            },
        },
        "workspace": {"root": str(workspace.resolve())},
        "attestations": {
            "prompt_exact": True,
            "agent_configuration_exact": True,
            "runtime_identity_exact": True,
            "approval_policy_exact": True,
            "budgets_exact": True,
            "fresh_agent_context": True,
            "prior_session_absent": True,
            "memory_absent": True,
            "web_disabled": True,
            "subagents_disabled": True,
            "credentials_absent_from_executor": True,
            "participant_host_shell_absent": True,
            "participant_container_socket_absent": True,
            "workspace_fresh": True,
            "required_outputs_absent_before": True,
            "source_read_only": True,
            "design_read_only": True,
            "pdk_read_only": True,
            "startup_files_read_only": True,
            "source_unchanged_after": True,
            "task_network_none": True,
            "task_network_enforced": True,
            "extra_condition_difference_absent": True,
            "user_intervention_count": 0,
        },
        "condition_observation": {
            "openada_distribution_present": present,
            "openada_cli_present": present,
            "openada_skill_present": present,
            "openada_package_present": present,
            "openada_treatment_schema_present": present,
            "openada_repository_present": raw_contaminated,
            "openada_prior_output_present": raw_contaminated,
            "openada_system_context_present": present,
            "treatment_bundle_manifest_sha256": (
                json.loads(campaign_path.read_text(encoding="utf-8"))["treatment"][
                    "bundle_manifest"
                ]["sha256"]
                if is_treatment
                else ("e" * 64 if raw_contaminated else None)
            ),
            "openada_identity_exact": present,
            "openada_used": treatment_used if is_treatment else raw_contaminated,
        },
        "authority": {
            "native_session_observed": True,
            "native_turn_observed": False,
            "provider_request_ids_observed": False,
            "identifiers_synthesized": False,
        },
    }
    if attestation_overrides:
        supervisor["attestations"].update(attestation_overrides)
    supervisor_path = directory / "supervisor.json"
    _write(supervisor_path, supervisor)
    return assemble_trial(
        campaign_path=campaign_path,
        plan_path=plan_path,
        supervisor_path=supervisor_path,
        trace_path=trace_path,
        submission_path=submission_path,
        workspace_path=workspace,
        signing_key_path=campaign_path.parent / "trial-signing-key.txt",
        trial_id=assignment["trial_id"],
    )


@pytest.fixture
def assembled_campaign(tmp_path: Path) -> tuple[Path, dict, Path, dict, str, list[dict]]:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trials = [
        _assemble(tmp_path / "captures", campaign_path, plan_path, plan, plan_sha, assignment)
        for assignment in plan["assignments"]
    ]
    return campaign_path, campaign, plan_path, plan, plan_sha, trials


def test_plan_is_deterministic_balanced_and_hides_seed(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path, pairs=6)
    _, campaign_sha, _ = load_json(campaign_path)
    first = build_plan(
        campaign, campaign_sha, seed=bytes.fromhex(SEED_HEX), created_at=PLAN_CREATED
    )
    second = build_plan(
        campaign, campaign_sha, seed=bytes.fromhex(SEED_HEX), created_at=PLAN_CREATED
    )
    assert first == second
    assert first["randomization_algorithm"] == "hmac-sha256-fisher-yates-v1"
    assert SEED_HEX not in json.dumps(first)
    assert first["seed_sha256"] == hashlib.sha256(bytes.fromhex(SEED_HEX)).hexdigest()
    first_conditions = [first["assignments"][index]["condition"] for index in range(0, 12, 2)]
    assert first_conditions.count("raw") == first_conditions.count("openada") == 3
    assert len({item["trial_id"] for item in first["assignments"]}) == 12


def test_plan_randomization_has_a_runtime_independent_known_answer() -> None:
    campaign = {
        "campaign_id": "known-answer",
        "created_at": CREATED,
        "planned_pairs": 5,
    }
    plan = build_plan(
        campaign,
        "ab" * 32,
        seed=bytes(32),
        created_at=PLAN_CREATED,
    )

    assert plan["randomization_algorithm"] == "hmac-sha256-fisher-yates-v1"
    assert [
        (
            item["sequence"],
            item["pair_id"],
            item["trial_id"],
            item["condition"],
            item["pair_position"],
        )
        for item in plan["assignments"]
    ] == [
        (1, "pair-9e703b72c729459618120e1f", "trial-a3fa1f8f1d0cf53431ff84e8", "raw", 1),
        (2, "pair-9e703b72c729459618120e1f", "trial-4ee3c14b8f460ce62489b165", "openada", 2),
        (3, "pair-2ca3e9a6a58eed632a00e79b", "trial-c21be52609bea801cca00b3b", "openada", 1),
        (4, "pair-2ca3e9a6a58eed632a00e79b", "trial-e3ab0bf61855986c5a768cfc", "raw", 2),
        (5, "pair-59c422c37ecac6897d485e31", "trial-4642d10bf8f8faa7f04f5dbc", "openada", 1),
        (6, "pair-59c422c37ecac6897d485e31", "trial-593e3f52cbbdfc75af87af52", "raw", 2),
        (7, "pair-4ae6dfccde21fab93c518625", "trial-c60d92d9219c57f7f337e35d", "raw", 1),
        (8, "pair-4ae6dfccde21fab93c518625", "trial-c8b34f3e07aef65eb97eace2", "openada", 2),
        (9, "pair-f4f34fb79e21e53cd51fb12b", "trial-a674143dda931c1c1f2e957f", "openada", 1),
        (10, "pair-f4f34fb79e21e53cd51fb12b", "trial-c3c029ec69beb96c62e2bd96", "raw", 2),
    ]


def test_plan_validation_rejects_time_order_boolean_positions_and_imbalance(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path, pairs=6)
    _, campaign_sha, _ = load_json(campaign_path)
    plan = build_plan(
        campaign, campaign_sha, seed=bytes.fromhex(SEED_HEX), created_at=PLAN_CREATED
    )

    before = copy.deepcopy(plan)
    before["created_at"] = "2026-07-14T11:59:59Z"
    with pytest.raises(EvaluationError, match="cannot precede"):
        validate_plan(before, campaign, campaign_sha)

    boolean = copy.deepcopy(plan)
    boolean["assignments"][0]["sequence"] = True
    with pytest.raises(EvaluationError, match="ordering fields"):
        validate_plan(boolean, campaign, campaign_sha)

    reversed_positions = copy.deepcopy(plan)
    reversed_positions["assignments"][0]["pair_position"] = 2
    reversed_positions["assignments"][1]["pair_position"] = 1
    with pytest.raises(EvaluationError, match="reversed pair positions"):
        validate_plan(reversed_positions, campaign, campaign_sha)

    imbalanced = copy.deepcopy(plan)
    for index in range(0, len(imbalanced["assignments"]), 2):
        first, second = imbalanced["assignments"][index : index + 2]
        first["condition"] = "raw"
        second["condition"] = "openada"
    with pytest.raises(EvaluationError, match="not balanced"):
        validate_plan(imbalanced, campaign, campaign_sha)

    unsupported_algorithm = copy.deepcopy(plan)
    unsupported_algorithm["randomization_algorithm"] = "runtime-owned-random-v0"
    with pytest.raises(EvaluationError, match="randomization_algorithm"):
        validate_plan(unsupported_algorithm, campaign, campaign_sha)


def test_plan_cli_persists_private_reveal_and_summary_verifies_it(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    seed_path = tmp_path / "private-seed.txt"
    completed = subprocess.run(
        [
            sys.executable,
            str(EVALUATION / "plan.py"),
            str(campaign_path),
            "--generate-seed-file",
            str(seed_path),
            "--created-at",
            PLAN_CREATED,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert seed_path.stat().st_mode & 0o777 == 0o600
    seed_hex = seed_path.read_text(encoding="ascii").removesuffix("\n")
    assert len(seed_hex) == 64
    plan = json.loads(completed.stdout)
    assert plan["seed_sha256"] == hashlib.sha256(bytes.fromhex(seed_hex)).hexdigest()
    plan_path = tmp_path / "generated-plan.json"
    plan_path.write_text(completed.stdout, encoding="utf-8")

    summary = subprocess.run(
        [
            sys.executable,
            str(EVALUATION / "summarize.py"),
            "--campaign",
            str(campaign_path),
            "--plan",
            str(plan_path),
            "--signing-key",
            str(tmp_path / "trial-signing-key.txt"),
            "--seed-file",
            str(seed_path),
            "--created-at",
            ASSEMBLED,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert summary.returncode == 1
    assert summary.stderr == ""
    summary_document = json.loads(summary.stdout)
    assert summary_document["randomization"]["schedule_verified"] is False
    assert summary_document["randomization"]["seed_hex"] is None
    verify_summary_seal(summary_document, campaign)

    before = seed_path.read_bytes()
    collision = subprocess.run(
        [
            sys.executable,
            str(EVALUATION / "plan.py"),
            str(campaign_path),
            "--generate-seed-file",
            str(seed_path),
            "--created-at",
            PLAN_CREATED,
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert collision.returncode == 2
    assert seed_path.read_bytes() == before
    assert str(seed_path) not in collision.stdout


def test_odd_plan_randomizes_which_condition_receives_extra_first_position() -> None:
    campaign = {
        "campaign_id": "odd-balance-known-answer",
        "created_at": CREATED,
        "planned_pairs": 5,
    }
    campaign_sha = "ab" * 32
    observed: set[tuple[int, int]] = set()
    first_orders: list[list[str]] = []
    for number in range(16):
        seed = number.to_bytes(32, byteorder="big")
        plan = build_plan(campaign, campaign_sha, seed=seed, created_at=PLAN_CREATED)
        first = [item["condition"] for item in plan["assignments"][::2]]
        first_orders.append(first)
        observed.add((first.count("raw"), first.count("openada")))
    assert observed == {(2, 3), (3, 2)}
    assert [first_orders[index] for index in (0, 2)] == [
        ["raw", "openada", "openada", "raw", "openada"],
        ["openada", "raw", "raw", "openada", "raw"],
    ]


def test_campaign_freezes_prompt_response_schema_scorer_and_distribution(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    (tmp_path / "task.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="task.prompt hash"):
        validate_campaign(campaign, campaign_path=campaign_path)
    assert "response_schema" in campaign["task"]
    assert "distribution_sha256" in campaign["treatment"]


def test_campaign_metrics_must_match_the_frozen_registry(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    unknown = copy.deepcopy(campaign)
    unknown["metrics"][-1]["id"] = "unregistered_metric"
    with pytest.raises(EvaluationError, match="not in the v0alpha1 registry"):
        validate_campaign(unknown, campaign_path=campaign_path)

    wrong_shape = copy.deepcopy(campaign)
    wrong_shape["metrics"][-1]["direction"] = "higher"
    with pytest.raises(EvaluationError, match="wrong kind or direction"):
        validate_campaign(wrong_shape, campaign_path=campaign_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("image", "digest", "sha256:" + "f" * 64, "image identity"),
        ("image", "config_digest", "sha256:" + "f" * 64, "image identity"),
        ("design", "revision", "f" * 40, "design identity"),
    ],
)
def test_campaign_runtime_cannot_contradict_frozen_task(
    tmp_path: Path, section: str, field: str, value: str, message: str
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    changed = copy.deepcopy(campaign)
    changed["runtime"][section][field] = value

    with pytest.raises(EvaluationError, match=message):
        validate_campaign(changed, campaign_path=campaign_path)


def test_campaign_tools_pdk_and_startup_files_are_bound_to_task_manifest(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    cases = []
    changed = copy.deepcopy(campaign)
    changed["runtime"]["tools"][0]["path"] = "/foss/tools/other/bin/xschem"
    cases.append((changed, "xschem identity"))
    changed = copy.deepcopy(campaign)
    changed["runtime"]["pdk"]["identity_files"][0]["sha256"] = "f" * 64
    cases.append((changed, "PDK files"))
    changed = copy.deepcopy(campaign)
    changed["runtime"]["startup_files"][0]["sha256"] = "f" * 64
    cases.append((changed, "startup files"))

    for changed, message in cases:
        with pytest.raises(EvaluationError, match=message):
            validate_campaign(changed, campaign_path=campaign_path)


def test_campaign_rejects_non_normalized_or_symlinked_task_closure(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    changed = copy.deepcopy(campaign)
    changed["task"]["manifest"]["path"] = "./manifest.json"
    with pytest.raises(EvaluationError, match="normalized relative path"):
        validate_campaign(changed, campaign_path=campaign_path)

    linked = tmp_path / "linked-task"
    linked.symlink_to(tmp_path, target_is_directory=True)
    changed = copy.deepcopy(campaign)
    changed["task"]["manifest"]["path"] = "linked-task/manifest.json"
    with pytest.raises(EvaluationError, match="parent must be a real directory"):
        validate_campaign(changed, campaign_path=campaign_path)


def test_campaign_response_schema_must_be_the_scorer_adjacent_file(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    detached = tmp_path / "detached"
    detached.mkdir()
    schema = detached / "submission.schema.json"
    shutil.copy2(tmp_path / "submission.schema.json", schema)
    changed = copy.deepcopy(campaign)
    changed["task"]["response_schema"] = {
        "path": "detached/submission.schema.json",
        "sha256": _sha(schema),
    }

    with pytest.raises(EvaluationError, match="scorer-adjacent"):
        validate_campaign(changed, campaign_path=campaign_path)


def test_treatment_nonuse_is_itt_but_raw_openada_is_contamination(tmp_path: Path) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    treatment = next(item for item in plan["assignments"] if item["condition"] == "openada")
    raw = next(item for item in plan["assignments"] if item["condition"] == "raw")
    nonuse = _assemble(
        tmp_path / "nonuse", campaign_path, plan_path, plan, plan_sha, treatment
    )
    contaminated = _assemble(
        tmp_path / "contaminated",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        raw,
        raw_contaminated=True,
    )
    assert nonuse["protocol"]["eligible"] is True
    assert nonuse["metrics"]["verified_artifact_complete"]["eligible"] is True
    assert contaminated["protocol"]["eligible"] is False
    assert "raw_openada_contamination" in contaminated["protocol"]["reason_codes"]
    assert contaminated["metrics"]["verified_artifact_complete"]["eligible"] is False


def test_adapter_rejection_is_retained_as_protocol_eligible_itt_outcome(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trial = _assemble(
        tmp_path / "adapter-failed",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        plan["assignments"][0],
        termination="adapter_failed",
    )
    assert trial["termination"] == "adapter_failed"
    assert trial["protocol"]["eligible"] is True
    assert trial["trace_observation"]["stream"]["issues"] == [
        "adapter_input_rejected",
        "incomplete_terminal",
    ]
    assert trial["metrics"]["agent_action_count"]["eligible"] is False
    assert trial["metrics"]["agent_action_count"]["value"] is None
    assert trial["metrics"]["verified_artifact_complete"]["eligible"] is True


def test_reasoning_tokens_are_a_subset_not_an_extra_output_budget(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trial = _assemble(
        tmp_path / "reasoning-subset",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        plan["assignments"][0],
        usage_override={
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 60_000,
            "reasoning_output_tokens": 60_000,
        },
    )
    assert trial["protocol"]["eligible"] is True
    assert "output_token_budget_exceeded" not in trial["protocol"]["reason_codes"]


def test_attempt_and_fresh_context_attestations_are_causal_eligibility_inputs(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    assignment = plan["assignments"][0]
    rerun = _assemble(
        tmp_path / "rerun",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        assignment,
        attempt_count=2,
    )
    assert "selective_rerun_or_prior_attempt" in rerun["protocol"]["reason_codes"]
    assert rerun["metrics"]["verified_artifact_complete"]["value"] is None

    stale = _assemble(
        tmp_path / "stale-context",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        assignment,
        attestation_overrides={"fresh_agent_context": False},
    )
    assert "attestation.fresh_agent_context" in stale["protocol"]["reason_codes"]
    assert "attestation.fresh_agent_context" in stale["protocol"][
        "engineering_reason_codes"
    ]


@pytest.mark.parametrize(
    "assemble_kwargs,reason",
    [
        ({"pair_span_exceeded": True}, "pair_span_budget_exceeded"),
        ({"wall_time_ms": 601_000}, "wall_time_budget_exceeded"),
    ],
)
def test_pair_span_and_budget_violations_are_protocol_ineligible_but_still_scored(
    tmp_path: Path, assemble_kwargs: dict, reason: str
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trial = _assemble(
        tmp_path / reason.replace(".", "-"),
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        plan["assignments"][0],
        **assemble_kwargs,
    )
    assert trial["protocol"]["eligible"] is False
    assert reason in trial["protocol"]["reason_codes"]
    assert trial["protocol"]["engineering_eligible"] is True
    assert trial["score"]["verified_artifact_complete"] is False


def test_trial_row_contains_no_supervisor_or_workspace_paths(assembled_campaign) -> None:
    *_, trials = assembled_campaign
    encoded = json.dumps(trials[0], sort_keys=True)
    assert "workspace_root" not in encoded
    assert "/captures/" not in encoded
    assert trials[0]["inputs"] == {
        "supervisor_bound": True,
        "trace_bound": True,
        "submission_bound": True,
        "workspace_attested": True,
    }
    assert trials[0]["score"]["verified_artifact_complete"] is False


@pytest.mark.parametrize("payload", [b"", b'{"status":NaN}', b'{"x":1,"x":2}'])
def test_malformed_or_empty_final_capture_remains_itt_outcome(
    tmp_path: Path, payload: bytes
) -> None:
    campaign_path, campaign = _campaign(tmp_path)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trial = _assemble(
        tmp_path / "malformed",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        plan["assignments"][0],
        submission_payload=payload,
    )
    assert trial["protocol"]["eligible"] is True
    assert trial["metrics"]["verified_artifact_complete"] == {
        "eligible": True,
        "value": False,
        "source": "native_score",
        "reason_codes": [],
    }
    assert any(item["code"] == "submission.invalid" for item in trial["score"]["diagnostics"])


def test_missing_trial_refuses_all_condition_and_metric_summaries(assembled_campaign) -> None:
    campaign_path, campaign, plan_path, plan, plan_sha, trials = assembled_campaign
    campaign_loaded, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign_loaded,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials[:-1],
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"]["status"] == "refused"
    missing_condition = plan["assignments"][-1]["condition"]
    assert summary["conditions"][missing_condition]["missing_trials"] == 1
    assert summary["conditions"][missing_condition][
        "verified_artifact_complete_counts"
    ]["unknown"] == 1
    assert len(summary["accounting"]["missing_trial_ids"]) == 1
    assert all(metric["raw"] is None for metric in summary["metrics"].values())


def test_duplicates_refuse_but_unplanned_rows_are_invalid(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    extra = copy.deepcopy(trials[0])
    extra["assignment"]["trial_id"] = "trial-" + "f" * 24
    duplicate_summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials + [trials[0]],
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert duplicate_summary["comparison"]["status"] == "refused"
    assert duplicate_summary["accounting"]["duplicate_trial_ids"] == [
        trials[0]["assignment"]["trial_id"]
    ]
    reordered_duplicate_summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=[trials[0], *reversed(trials)],
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert reordered_duplicate_summary == duplicate_summary
    assert len(duplicate_summary["evidence"]) == len(trials) + 1
    assert [item["sequence"] for item in duplicate_summary["evidence"]][:2] == [1, 1]
    with pytest.raises(EvaluationError, match="seal verification"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=trials + [extra],
            seed_hex=SEED_HEX,
            created_at=ASSEMBLED,
        )

    resigned_extra = copy.deepcopy(extra)
    _reseal(resigned_extra, campaign)
    with pytest.raises(EvaluationError, match="not one unique planned assignment"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=trials + [resigned_extra],
            seed_hex=SEED_HEX,
            created_at=ASSEMBLED,
        )
    valid_summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    with pytest.raises(EvaluationError, match="not one unique planned assignment"):
        verify_full_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            summary=valid_summary,
            trials=trials + [resigned_extra],
        )


@pytest.mark.parametrize("mutation", ["score", "metric"])
def test_validly_resealed_derived_field_forgery_fails_semantic_revalidation(
    assembled_campaign, mutation: str
) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    forged = copy.deepcopy(trials[0])
    if mutation == "score":
        forged["score"]["verified_artifact_complete"] = True
    else:
        forged["metrics"]["verified_artifact_complete"] = {
            "eligible": True,
            "value": True,
            "source": "native_score",
            "reason_codes": [],
        }
    _reseal(forged, campaign)

    with pytest.raises(EvaluationError, match="inconsistent|authoritative observations"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=[forged],
            seed_hex=SEED_HEX,
            created_at=ASSEMBLED,
        )


def test_pair_mates_with_different_signed_pair_views_refuse_comparison(
    assembled_campaign,
) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    changed = copy.deepcopy(trials)
    changed[0]["execution_observation"]["pair"]["second"][
        "monotonic_started_ms"
    ] += 1
    _reseal(changed[0], campaign)

    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=changed,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"]["status"] == "refused"
    assert "execution_schedule_not_verified" in summary["comparison"]["reason_codes"]


def test_signed_global_chronology_overlap_refuses_comparison(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    changed = copy.deepcopy(trials)
    second_pair_id = plan["assignments"][2]["pair_id"]
    pair_trials = [
        trial for trial in changed if trial["assignment"]["pair_id"] == second_pair_id
    ]
    delta = 119_000
    for trial in pair_trials:
        execution = trial["execution_observation"]
        execution["monotonic_started_ms"] -= delta
        execution["monotonic_finished_ms"] -= delta
        for record in execution["pair"].values():
            record["monotonic_started_ms"] -= delta
            record["monotonic_finished_ms"] -= delta
        _reseal(trial, campaign)

    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=changed,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"]["status"] == "refused"
    assert "execution_schedule_not_verified" in summary["comparison"]["reason_codes"]


def test_multiple_signed_clock_domains_refuse_comparison(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    changed = copy.deepcopy(trials)
    second_pair_id = plan["assignments"][2]["pair_id"]
    for trial in changed:
        if trial["assignment"]["pair_id"] == second_pair_id:
            trial["execution_observation"]["clock_domain_id"] = "e" * 64
            _reseal(trial, campaign)

    with pytest.raises(EvaluationError, match="clock domain differs"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=changed,
            seed_hex=SEED_HEX,
            created_at=ASSEMBLED,
        )


def test_campaign_clock_nonce_and_zero_origin_are_semantically_bound(
    assembled_campaign,
) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    changed = copy.deepcopy(trials)
    for trial in changed[:2]:
        execution = trial["execution_observation"]
        execution["monotonic_started_ms"] += 1
        execution["monotonic_finished_ms"] += 1
        for record in execution["pair"].values():
            record["monotonic_started_ms"] += 1
            record["monotonic_finished_ms"] += 1
        _reseal(trial, campaign)
    with pytest.raises(EvaluationError, match="does not start at zero"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=changed,
            seed_hex=SEED_HEX,
            created_at=ASSEMBLED,
        )


def test_tampered_but_shape_valid_plan_fails_seed_reproduction(assembled_campaign) -> None:
    campaign_path, campaign, _, plan, _, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    tampered = copy.deepcopy(plan)
    raw_first = next(
        index
        for index in range(0, len(tampered["assignments"]), 2)
        if tampered["assignments"][index]["condition"] == "raw"
    )
    openada_first = next(
        index
        for index in range(0, len(tampered["assignments"]), 2)
        if tampered["assignments"][index]["condition"] == "openada"
    )
    for index in (raw_first, openada_first):
        first, second = tampered["assignments"][index : index + 2]
        first["condition"], second["condition"] = second["condition"], first["condition"]
    validate_plan(tampered, campaign, campaign_sha)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=tampered,
        plan_sha256="c" * 64,
        trials=[],
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["randomization"]["schedule_verified"] is False
    assert "randomization_seed_not_verified" in summary["comparison"]["reason_codes"]
    assert all(
        record["received_trials"] == 0
        and record["missing_trials"] == record["planned_trials"]
        for record in summary["conditions"].values()
    )


def test_partial_accounting_withholds_verified_seed(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials[:-1],
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["randomization"]["schedule_verified"] is False
    assert summary["randomization"]["seed_hex"] is None
    assert summary["comparison"]["status"] == "refused"


def test_summary_seal_evidence_domain_and_tamper_detection(
    assembled_campaign,
) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=list(reversed(trials)),
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )

    verify_summary_seal(summary, campaign)
    authenticate_summary(campaign, campaign_sha, summary)
    assert [item["sequence"] for item in summary["evidence"]] == list(
        range(1, len(trials) + 1)
    )
    first_trial = trials[0]
    first_evidence = summary["evidence"][0]
    assert first_evidence["trial_sha256"] == canonical_json_sha256(first_trial)
    assert first_evidence["signature_sha256"] == hashlib.sha256(
        bytes.fromhex(first_trial["seal"]["signature_hex"])
    ).hexdigest()

    tampered = copy.deepcopy(summary)
    tampered["conditions"]["raw"]["received_trials"] -= 1
    with pytest.raises(EvaluationError, match="summary seal verification"):
        verify_summary_seal(tampered, campaign)

    replayed_trial_signature = copy.deepcopy(summary)
    replayed_trial_signature["seal"] = copy.deepcopy(first_trial["seal"])
    with pytest.raises(EvaluationError, match="summary seal verification"):
        verify_summary_seal(replayed_trial_signature, campaign)

    unsigned = {key: value for key, value in summary.items() if key != "seal"}
    with pytest.raises(EvaluationError, match="summary signing key differs"):
        seal_summary(unsigned, campaign, private_seed=bytes.fromhex("03" * 32))

    wrong_key_campaign = copy.deepcopy(campaign)
    wrong_private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("03" * 32))
    wrong_public = wrong_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    wrong_key_campaign["trial_signing"] = {
        "algorithm": "ed25519",
        "public_key_hex": wrong_public.hex(),
        "key_id": hashlib.sha256(wrong_public).hexdigest(),
    }
    with pytest.raises(EvaluationError, match="summary seal key differs"):
        verify_summary_seal(summary, wrong_key_campaign)


def test_full_summary_verification_and_public_cli(
    assembled_campaign, tmp_path: Path
) -> None:
    campaign_path, _, plan_path, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    verify_full_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        summary=summary,
        trials=list(reversed(trials)),
    )

    summary_path = tmp_path / "summary.json"
    _write(summary_path, summary)
    trial_paths: list[Path] = []
    for trial in reversed(trials):
        trial_path = tmp_path / f"row-{trial['assignment']['sequence']}.json"
        _write(trial_path, trial)
        trial_paths.append(trial_path)

    summary_only = subprocess.run(
        [
            sys.executable,
            str(EVALUATION / "verify_summary.py"),
            "--campaign",
            str(campaign_path),
            "--summary",
            str(summary_path),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert summary_only.returncode == 0
    assert summary_only.stderr == ""
    assert json.loads(summary_only.stdout)["mode"] == "summary-only"

    full = subprocess.run(
        [
            sys.executable,
            str(EVALUATION / "verify_summary.py"),
            "--campaign",
            str(campaign_path),
            "--summary",
            str(summary_path),
            "--plan",
            str(plan_path),
            *[
                argument
                for path in trial_paths
                for argument in ("--trial", str(path))
            ],
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert full.returncode == 0
    assert full.stderr == ""
    verification = json.loads(full.stdout)
    assert verification["mode"] == "full"
    assert verification["evidence_records"] == len(trials)


def test_refused_partial_summary_is_fully_recomputable_and_binds_all_rows(
    assembled_campaign,
) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    partial_trials = trials[:-1]
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=partial_trials,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"]["status"] == "refused"
    assert summary["randomization"] == {
        "seed_sha256": plan["seed_sha256"],
        "seed_hex": None,
        "schedule_verified": False,
    }
    verify_full_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        summary=summary,
        trials=partial_trials,
    )

    with pytest.raises(EvaluationError, match="evidence bindings"):
        verify_full_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            summary=summary,
            trials=partial_trials[:-1],
        )

    missing_evidence = copy.deepcopy(summary)
    missing_evidence["evidence"].pop()
    seal_summary(missing_evidence, campaign, private_seed=SIGNING_SEED)
    authenticate_summary(campaign, campaign_sha, missing_evidence)
    with pytest.raises(EvaluationError, match="evidence bindings"):
        verify_full_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            summary=missing_evidence,
            trials=partial_trials,
        )


@pytest.mark.parametrize(
    "module,required",
    [
        (
            summarize_module,
            [
                "--campaign",
                "campaign.json",
                "--plan",
                "plan.json",
                "--signing-key",
                "signing-key.txt",
            ],
        ),
        (
            verify_summary_module,
            ["--campaign", "campaign.json", "--summary", "summary.json"],
        ),
    ],
)
def test_summary_clis_reject_excess_rows_before_loading_files(
    module: object,
    required: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_load(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("trial-count bound must precede all file loads")

    monkeypatch.setattr(module, "load_json", unexpected_load)
    trial_arguments = [
        argument
        for _ in range(summarize_module.MAX_SUPPLIED_TRIAL_RECORDS + 1)
        for argument in ("--trial", "row.json")
    ]
    assert module.main([*required, *trial_arguments]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "openada.eval.error/v0alpha1"


def test_summary_timestamp_cannot_precede_plan(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, _ = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    with pytest.raises(EvaluationError, match="plan.created_at"):
        build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            plan=plan,
            plan_sha256=plan_sha,
            trials=[],
            seed_hex=None,
            created_at=CREATED,
        )


def test_complete_summary_keeps_failures_and_refuses_provider_metrics(assembled_campaign) -> None:
    campaign_path, campaign, plan_path, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    trials[0] = _assemble(
        campaign_path.parent / "timed-out",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        plan["assignments"][0],
        termination="timed_out",
    )
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"] == {
        "status": "ready",
        "reason_codes": [],
        "minimum_pairs": 5,
        "descriptive_only": True,
    }
    condition = trials[0]["assignment"]["condition"]
    assert summary["conditions"][condition]["termination_counts"]["timed_out"] == 1
    primary = summary["metrics"]["verified_artifact_complete"]
    assert primary["eligible"] is True
    assert primary["pair_count"] == 5
    assert primary["raw"]["true_count"] == 0
    provider = summary["metrics"]["provider_request_count"]
    assert provider["eligible"] is False
    assert provider["raw"] is None
    assert summary["randomization"]["seed_hex"] == SEED_HEX


def test_missing_seed_reveal_refuses_even_complete_trials(assembled_campaign) -> None:
    campaign_path, _, _, plan, plan_sha, trials = assembled_campaign
    campaign, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials,
        seed_hex=None,
        created_at=ASSEMBLED,
    )
    assert summary["comparison"]["status"] == "refused"
    assert summary["randomization"]["seed_hex"] is None
    assert all(
        record["received_trials"] == record["planned_trials"]
        for record in summary["conditions"].values()
    )


def test_six_pair_campaign_cannot_drop_one_invalid_pair_and_compare_five(
    tmp_path: Path,
) -> None:
    campaign_path, campaign = _campaign(tmp_path, pairs=6)
    plan_path, plan, plan_sha = _plan(campaign_path, campaign)
    trials = [
        _assemble(tmp_path / "six", campaign_path, plan_path, plan, plan_sha, assignment)
        for assignment in plan["assignments"]
    ]
    contaminated_assignment = next(
        item for item in plan["assignments"] if item["condition"] == "raw"
    )
    contaminated_index = plan["assignments"].index(contaminated_assignment)
    trials[contaminated_index] = _assemble(
        tmp_path / "contaminated-six",
        campaign_path,
        plan_path,
        plan,
        plan_sha,
        contaminated_assignment,
        raw_contaminated=True,
    )
    _, campaign_sha, _ = load_json(campaign_path)
    summary = build_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha,
        plan=plan,
        plan_sha256=plan_sha,
        trials=trials,
        seed_hex=SEED_HEX,
        created_at=ASSEMBLED,
    )
    assert summary["accounting"]["protocol_eligible_pairs"] == 5
    assert summary["comparison"]["status"] == "refused"
    assert "planned_pair_protocol_ineligible" in summary["comparison"]["reason_codes"]
    condition = contaminated_assignment["condition"]
    assert summary["conditions"][condition]["received_trials"] == 6
    assert summary["conditions"][condition]["protocol_eligible_trials"] == 5


@pytest.mark.parametrize(
    "payload,match",
    [
        ('{"schema":"x","schema":"y"}', "duplicate JSON object key"),
        ('{"value":NaN}', "non-standard JSON constant"),
    ],
)
def test_strict_json_rejects_duplicates_and_nan(tmp_path: Path, payload: str, match: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(EvaluationError, match=match):
        load_json(path)


def test_strict_json_rejects_deep_nesting_before_schema_validation(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    depth = 70
    path.write_text('{"x":' * depth + "0" + "}" * depth, encoding="utf-8")

    with pytest.raises(EvaluationError, match="nesting exceeds"):
        load_json(path)
