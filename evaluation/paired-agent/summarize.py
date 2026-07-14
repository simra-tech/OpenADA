#!/usr/bin/env python3
"""Account for a complete paired campaign and emit descriptive metrics only."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import sys
from typing import Any

from common import (
    EvaluationError,
    IHP_PAIRED_TASK_DIR,
    SUMMARY_SCHEMA,
    emit_error,
    emit_json,
    find_assignment,
    load_json,
    load_trial_signing_seed,
    median,
    parse_timestamp,
    read_regular_bytes,
    seal_summary,
    trial_evidence_records,
    utc_now,
    validate_campaign,
    validate_plan,
    validate_schema,
    verify_trial_seal,
)
from assemble import _metrics, derive_protocol, validate_trace
from plan import build_plan


MINIMUM_PAIRED_BLOCKS = 5
MAX_SUPPLIED_TRIAL_RECORDS = 10_000
TERMINATIONS = (
    "completed",
    "timed_out",
    "budget_exhausted",
    "agent_failed",
    "adapter_failed",
    "supervisor_aborted",
)
SCORE_ARTIFACT_STATUSES = {
    "missing",
    "unsafe_parent",
    "invalid_type",
    "hardlinked",
    "empty",
    "oversized",
    "unstable",
    "unsafe",
    "valid",
    "malformed",
    "semantic_fail",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Verify complete planned-trial accounting and emit condition-level "
            "descriptive paired summaries without p-values."
        )
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--signing-key",
        type=Path,
        required=True,
        help="Owner-only Ed25519 seed whose public key is frozen by the campaign.",
    )
    parser.add_argument(
        "--trial",
        type=Path,
        action="append",
        default=[],
        help="Assembled trial JSON; repeat for every planned assignment.",
    )
    seed = parser.add_mutually_exclusive_group()
    seed.add_argument(
        "--seed-hex",
        help=(
            "Post-run 64-character seed reveal. Descriptive comparison is "
            "refused until the reveal reproduces the frozen assignment plan; "
            "the value is visible in process listings."
        ),
    )
    seed.add_argument(
        "--seed-file",
        type=Path,
        help="Private mode-0600 seed file created by plan.py (recommended).",
    )
    parser.add_argument("--created-at", help="Deterministic UTC time for tests.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def _revealed_seed(value: str | None) -> bytes | None:
    if value is None:
        return None
    if len(value) != 64 or value != value.lower():
        return None
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return None
    return decoded if len(decoded) == 32 else None


def _seed_file_value(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        target = path.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise EvaluationError("cannot resolve --seed-file") from exc
    payload = read_regular_bytes(target, maximum_bytes=65)
    if len(payload) == 65 and payload.endswith(b"\n"):
        payload = payload[:-1]
    if len(payload) != 64:
        raise EvaluationError("seed file must contain exactly 64 lowercase hex characters")
    try:
        value = payload.decode("ascii")
    except UnicodeError as exc:
        raise EvaluationError("seed file is not ASCII hexadecimal") from exc
    if _revealed_seed(value) is None:
        raise EvaluationError("seed file must contain exactly 64 lowercase hex characters")
    return value


def _verify_schedule(
    campaign: dict[str, Any],
    campaign_sha256: str,
    plan: dict[str, Any],
    seed_hex: str | None,
) -> tuple[bool, str | None]:
    seed = _revealed_seed(seed_hex)
    if seed is None:
        return False, None
    if hashlib.sha256(seed).hexdigest() != plan["seed_sha256"]:
        return False, None
    reproduced = build_plan(
        campaign,
        campaign_sha256,
        seed=seed,
        created_at=plan["created_at"],
    )
    if reproduced != plan:
        return False, None
    return True, seed.hex()


def _metric_value_is_valid(value: object, kind: str) -> bool:
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _numeric(value: bool | int | float) -> int | float:
    return int(value) if isinstance(value, bool) else value


def _distribution(values: list[bool | int | float], *, kind: str) -> dict[str, Any]:
    numbers = [_numeric(value) for value in values]
    return {
        "count": len(numbers),
        "minimum": min(numbers),
        "maximum": max(numbers),
        "median": median(numbers),
        "true_count": sum(1 for value in values if value is True)
        if kind == "boolean"
        else None,
    }


def _empty_metric(specification: dict[str, Any], reasons: list[str], pair_count: int) -> dict[str, Any]:
    return {
        "eligible": False,
        "kind": specification["kind"],
        "direction": specification["direction"],
        "reason_codes": sorted(set(reasons)),
        "pair_count": pair_count,
        "raw": None,
        "openada": None,
        "paired_delta": None,
    }


def _condition_record(
    trials: list[dict[str, Any]], *, planned_trials: int
) -> dict[str, Any]:
    terminations = {name: 0 for name in TERMINATIONS}
    for trial in trials:
        terminations[trial["termination"]] += 1
    return {
        "planned_trials": planned_trials,
        "received_trials": len(trials),
        "missing_trials": planned_trials - len(trials),
        "protocol_eligible_trials": sum(
            1 for trial in trials if trial["protocol"]["eligible"]
        ),
        "engineering_eligible_trials": sum(
            1 for trial in trials if trial["protocol"]["engineering_eligible"]
        ),
        "termination_counts": terminations,
        "verified_artifact_complete_counts": {
            "true": sum(
                1 for trial in trials if trial["score"]["verified_artifact_complete"]
            ),
            "false": sum(
                1 for trial in trials if not trial["score"]["verified_artifact_complete"]
            ),
            "unknown": planned_trials - len(trials),
        },
    }


def _validate_score_semantics(score: dict[str, Any], campaign: dict[str, Any]) -> None:
    if score["task_id"] != campaign["task"]["id"]:
        raise EvaluationError("trial score task ID differs from the campaign")
    manifest, _, _ = load_json(IHP_PAIRED_TASK_DIR / "manifest.json")
    expected = [
        (item["role"], item["path"]) for item in manifest["artifacts"]
    ]
    actual = [(item["role"], item["path"]) for item in score["artifacts"]]
    if actual != expected:
        raise EvaluationError("trial score artifact set/order differs from the frozen task")
    semantic_statuses = {"valid", "malformed", "semantic_fail"}
    for artifact in score["artifacts"]:
        status = artifact["status"]
        if status not in SCORE_ARTIFACT_STATUSES:
            raise EvaluationError("trial score contains an unsupported artifact status")
        has_hash = artifact["sha256"] is not None
        if (status in semantic_statuses) != has_hash:
            raise EvaluationError("trial score artifact hash conflicts with its status")
        if has_hash and (
            not isinstance(artifact["bytes"], int)
            or isinstance(artifact["bytes"], bool)
            or artifact["bytes"] <= 0
        ):
            raise EvaluationError("trial score hashed artifact lacks a positive byte count")
        if status == "missing" and (
            artifact["bytes"] is not None or artifact["sha256"] is not None
        ):
            raise EvaluationError("trial score missing artifact carries observed bytes")
    by_role = {item["role"]: item for item in score["artifacts"]}
    raw_status = by_role["simulation-raw"]["status"]
    expected_verdict = (
        "pass"
        if raw_status == "valid"
        else "fail"
        if raw_status == "semantic_fail"
        else "unknown"
    )
    if score["engineering_verdict"] != expected_verdict:
        raise EvaluationError("trial engineering verdict conflicts with native raw status")
    if by_role["simulation-log"]["status"] == "valid" and raw_status not in {
        "valid",
        "semantic_fail",
    }:
        raise EvaluationError("trial valid log is not bound to a parsed native raw file")
    submission_valid = score["provenance"]["submission_valid"]
    if (score["reported_status_correct"] is None) == submission_valid:
        raise EvaluationError("trial reported-status result conflicts with submission validity")
    reported_hashes = [item["reported_hash_correct"] for item in score["artifacts"]]
    if submission_valid:
        if any(not isinstance(value, bool) for value in reported_hashes):
            raise EvaluationError("valid submission lacks per-artifact report decisions")
    elif any(value is not None for value in reported_hashes):
        raise EvaluationError("invalid submission cannot carry trusted report decisions")
    if not submission_valid and any(
        score["provenance"][key]
        for key in score["provenance"]
        if key != "submission_valid"
    ):
        raise EvaluationError("invalid submission cannot establish provenance")
    if score["provenance"]["limitations_reported"] != submission_valid:
        raise EvaluationError("trial limitation reporting conflicts with submission validity")
    if score["provenance"]["artifact_hashes_exact"] and (
        not score["provenance"]["artifact_set_exact"]
        or any(value is not True for value in reported_hashes)
    ):
        raise EvaluationError("trial exact artifact hashes conflict with report records")
    expected_complete = bool(
        score["engineering_verdict"] == "pass"
        and all(item["status"] == "valid" for item in score["artifacts"])
        and score["reported_status_correct"] is True
        and all(score["provenance"].values())
    )
    if score["verified_artifact_complete"] is not expected_complete:
        raise EvaluationError("trial artifact-completeness result is internally inconsistent")
    if expected_complete and score["diagnostics"]:
        raise EvaluationError("complete trial score cannot contain diagnostics")


def _validate_trial_semantics(
    campaign: dict[str, Any],
    campaign_sha256: str,
    plan: dict[str, Any],
    plan_sha256: str,
    trial: dict[str, Any],
) -> None:
    validate_schema(trial, "trial-v0alpha1.schema.json", label="assembled trial")
    verify_trial_seal(trial, campaign)
    if trial["campaign_id"] != campaign["campaign_id"]:
        raise EvaluationError("trial campaign ID differs from the campaign")
    if trial["campaign_sha256"] != campaign_sha256:
        raise EvaluationError("trial campaign hash differs from the campaign bytes")
    if trial["plan_sha256"] != plan_sha256:
        raise EvaluationError("trial plan hash differs from the exact plan bytes")
    assignment = find_assignment(plan, trial["assignment"]["trial_id"])
    if trial["assignment"] != assignment:
        raise EvaluationError("trial assignment differs from the frozen plan")
    execution = trial["execution_observation"]
    if execution["clock_domain_id"] != campaign["execution_clock"]["domain_id"]:
        raise EvaluationError("trial clock domain differs from the campaign")
    pair_records = execution["pair"]
    planned_pair = sorted(
        (
            item
            for item in plan["assignments"]
            if item["pair_id"] == assignment["pair_id"]
        ),
        key=lambda item: item["pair_position"],
    )
    if {pair_records["first"]["trial_id"], pair_records["second"]["trial_id"]} != {
        item["trial_id"] for item in planned_pair
    }:
        raise EvaluationError("trial pair observation differs from the frozen pair")
    if (
        planned_pair[0]["sequence"] == 1
        and pair_records["first"]["monotonic_started_ms"] != 0
    ):
        raise EvaluationError("trial campaign-relative monotonic clock does not start at zero")
    current = next(
        record
        for record in pair_records.values()
        if record["trial_id"] == assignment["trial_id"]
    )
    if (
        current["monotonic_started_ms"] != execution["monotonic_started_ms"]
        or current["monotonic_finished_ms"] != execution["monotonic_finished_ms"]
    ):
        raise EvaluationError("trial dispatch interval differs from its pair observation")
    observed_duration = (
        execution["monotonic_finished_ms"] - execution["monotonic_started_ms"]
    )
    if observed_duration < 0 or abs(
        observed_duration - trial["timing"]["wall_time_ms"]
    ) > 2_000:
        raise EvaluationError("trial monotonic interval conflicts with wall time")
    _validate_score_semantics(trial["score"], campaign)
    validate_trace(trial["trace_observation"])
    expected_protocol = derive_protocol(
        campaign,
        plan,
        assignment,
        trial["termination"],
        trial["timing"],
        trial["policy_observation"],
        execution,
        trial["trace_observation"],
    )
    if trial["protocol"] != expected_protocol:
        raise EvaluationError("trial protocol fields differ from their observations")
    expected_metrics = _metrics(
        campaign,
        trial["timing"],
        trial["trace_observation"],
        trial["score"],
        protocol_eligible=expected_protocol["eligible"],
        engineering_eligible=expected_protocol["engineering_eligible"],
    )
    if trial["metrics"] != expected_metrics:
        raise EvaluationError("trial metrics differ from their authoritative observations")


def _execution_schedule_verified(
    plan: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    counts: Counter[str],
) -> bool:
    if any(
        assignment["trial_id"] not in by_id
        or counts[assignment["trial_id"]] != 1
        for assignment in plan["assignments"]
    ):
        return False
    trials = [by_id[item["trial_id"]] for item in plan["assignments"]]
    clock_domains = {
        trial["execution_observation"]["clock_domain_id"] for trial in trials
    }
    if len(clock_domains) != 1:
        return False
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        grouped[trial["assignment"]["pair_id"]].append(trial)
    for pair_trials in grouped.values():
        if len(pair_trials) != 2:
            return False
        first = pair_trials[0]["execution_observation"]
        second = pair_trials[1]["execution_observation"]
        if first["clock_domain_id"] != second["clock_domain_id"]:
            return False
        if first["pair"] != second["pair"]:
            return False
    previous_finish: int | None = None
    for assignment, trial in zip(plan["assignments"], trials):
        execution = trial["execution_observation"]
        if execution["dispatch_sequence_observed"] != assignment["sequence"]:
            return False
        if previous_finish is not None and execution["monotonic_started_ms"] < previous_finish:
            return False
        previous_finish = execution["monotonic_finished_ms"]
    return True


def build_unsigned_summary(
    *,
    campaign: dict[str, Any],
    campaign_sha256: str,
    plan: dict[str, Any],
    plan_sha256: str,
    trials: list[dict[str, Any]],
    seed_hex: str | None,
    created_at: str,
) -> dict[str, Any]:
    if len(trials) > MAX_SUPPLIED_TRIAL_RECORDS:
        raise EvaluationError(
            f"summary input exceeds the {MAX_SUPPLIED_TRIAL_RECORDS}-trial bound"
        )
    summary_time = parse_timestamp(created_at, label="summary.created_at")
    plan_time = parse_timestamp(plan["created_at"], label="plan.created_at")
    if summary_time < plan_time:
        raise EvaluationError("summary.created_at cannot precede plan.created_at")
    for trial in trials:
        _validate_trial_semantics(
            campaign,
            campaign_sha256,
            plan,
            plan_sha256,
            trial,
        )
    planned_by_id = {item["trial_id"]: item for item in plan["assignments"]}
    planned_ids = set(planned_by_id)
    received_ids = [trial["assignment"]["trial_id"] for trial in trials]
    counts = Counter(received_ids)
    received_set = set(received_ids)
    missing = sorted(planned_ids - received_set)
    duplicates = sorted(trial_id for trial_id, count in counts.items() if count > 1)

    by_id: dict[str, dict[str, Any]] = {}
    for trial in trials:
        trial_id = trial["assignment"]["trial_id"]
        if trial_id in planned_by_id:
            assignment = planned_by_id[trial_id]
            if trial["assignment"] != assignment:
                raise EvaluationError(
                    f"trial {trial_id} assignment fields differ from the frozen plan"
                )
        if trial_id not in by_id:
            by_id[trial_id] = trial

    pair_assignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in plan["assignments"]:
        pair_assignments[assignment["pair_id"]].append(assignment)
    complete_pair_ids = [
        pair_id
        for pair_id, assignments in pair_assignments.items()
        if all(item["trial_id"] in by_id and counts[item["trial_id"]] == 1 for item in assignments)
    ]
    eligible_pair_ids: list[str] = []
    for pair_id in complete_pair_ids:
        pair_trials = [by_id[item["trial_id"]] for item in pair_assignments[pair_id]]
        if not all(trial["protocol"]["eligible"] for trial in pair_trials):
            continue
        eligible_pair_ids.append(pair_id)

    execution_schedule_verified = _execution_schedule_verified(plan, by_id, counts)

    seed_reproduced, verified_seed = _verify_schedule(
        campaign, campaign_sha256, plan, seed_hex
    )
    accounting_complete = (
        not missing
        and not duplicates
        and len(trials) == len(plan["assignments"])
    )
    schedule_verified = seed_reproduced and accounting_complete
    revealed = verified_seed if schedule_verified else None
    refusal_reasons: list[str] = []
    if missing:
        refusal_reasons.append("planned_trials_missing")
    if duplicates:
        refusal_reasons.append("duplicate_trial_assignments")
    if len(complete_pair_ids) < campaign["planned_pairs"]:
        refusal_reasons.append("planned_pairs_incomplete")
    if len(eligible_pair_ids) != campaign["planned_pairs"]:
        refusal_reasons.append("planned_pair_protocol_ineligible")
    if len(eligible_pair_ids) < MINIMUM_PAIRED_BLOCKS:
        refusal_reasons.append("fewer_than_five_protocol_eligible_pairs")
    if not schedule_verified:
        refusal_reasons.append("randomization_seed_not_verified")
    if not execution_schedule_verified:
        refusal_reasons.append("execution_schedule_not_verified")
    comparison_ready = not refusal_reasons

    accounting = {
        "planned_pairs": campaign["planned_pairs"],
        "planned_trials": campaign["planned_pairs"] * 2,
        "received_trial_records": len(trials),
        "unique_planned_trials_received": len(received_set & planned_ids),
        "missing_trial_ids": missing,
        "duplicate_trial_ids": duplicates,
        "complete_pairs": len(complete_pair_ids),
        "protocol_eligible_pairs": len(eligible_pair_ids),
        "execution_schedule_verified": execution_schedule_verified,
    }
    conditions = {}
    for condition in ("raw", "openada"):
        planned_condition = [
            item for item in plan["assignments"] if item["condition"] == condition
        ]
        received_condition = [
            by_id[item["trial_id"]]
            for item in planned_condition
            if item["trial_id"] in by_id and counts[item["trial_id"]] == 1
        ]
        conditions[condition] = _condition_record(
            received_condition,
            planned_trials=len(planned_condition),
        )
    metrics: dict[str, Any] = {}
    if not comparison_ready:
        for specification in campaign["metrics"]:
            metrics[specification["id"]] = _empty_metric(
                specification,
                ["comparison_refused"],
                0,
            )
    else:
        cohort_trials: list[dict[str, Any]] = []
        for pair_id in eligible_pair_ids:
            cohort_trials.extend(
                by_id[item["trial_id"]] for item in pair_assignments[pair_id]
            )
        for specification in campaign["metrics"]:
            name = specification["id"]
            missing_metric = False
            values: dict[str, list[bool | int | float]] = {"raw": [], "openada": []}
            deltas: list[int | float] = []
            for pair_id in eligible_pair_ids:
                pair_values: dict[str, bool | int | float] = {}
                for assignment in pair_assignments[pair_id]:
                    trial = by_id[assignment["trial_id"]]
                    metric = trial["metrics"].get(name)
                    if (
                        not isinstance(metric, dict)
                        or metric.get("eligible") is not True
                        or not _metric_value_is_valid(metric.get("value"), specification["kind"])
                    ):
                        missing_metric = True
                        continue
                    value = metric["value"]
                    pair_values[assignment["condition"]] = value
                    values[assignment["condition"]].append(value)
                if set(pair_values) == {"raw", "openada"}:
                    deltas.append(
                        _numeric(pair_values["openada"]) - _numeric(pair_values["raw"])
                    )
                else:
                    missing_metric = True
            if missing_metric:
                metrics[name] = _empty_metric(
                    specification,
                    ["metric_incomplete_for_fixed_cohort"],
                    len(eligible_pair_ids),
                )
                continue
            metrics[name] = {
                "eligible": True,
                "kind": specification["kind"],
                "direction": specification["direction"],
                "reason_codes": [],
                "pair_count": len(eligible_pair_ids),
                "raw": _distribution(values["raw"], kind=specification["kind"]),
                "openada": _distribution(
                    values["openada"], kind=specification["kind"]
                ),
                "paired_delta": {
                    "definition": "openada_minus_raw",
                    "count": len(deltas),
                    "minimum": min(deltas),
                    "maximum": max(deltas),
                    "median": median(deltas),
                },
            }

    summary = {
        "schema": SUMMARY_SCHEMA,
        "campaign_id": campaign["campaign_id"],
        "campaign_sha256": campaign_sha256,
        "plan_sha256": plan_sha256,
        "created_at": created_at,
        "evidence": trial_evidence_records(trials, plan),
        "randomization": {
            "seed_sha256": plan["seed_sha256"],
            "seed_hex": revealed,
            "schedule_verified": schedule_verified,
        },
        "accounting": accounting,
        "comparison": {
            "status": "ready" if comparison_ready else "refused",
            "reason_codes": sorted(set(refusal_reasons)),
            "minimum_pairs": MINIMUM_PAIRED_BLOCKS,
            "descriptive_only": True,
        },
        "conditions": conditions,
        "metrics": metrics,
    }
    return summary


def build_summary(
    *,
    campaign: dict[str, Any],
    campaign_sha256: str,
    plan: dict[str, Any],
    plan_sha256: str,
    trials: list[dict[str, Any]],
    seed_hex: str | None,
    created_at: str,
    private_seed: bytes,
) -> dict[str, Any]:
    summary = build_unsigned_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha256,
        plan=plan,
        plan_sha256=plan_sha256,
        trials=trials,
        seed_hex=seed_hex,
        created_at=created_at,
    )
    seal_summary(summary, campaign, private_seed=private_seed)
    validate_schema(summary, "summary-v0alpha1.schema.json", label="evaluation summary")
    return summary


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if len(args.trial) > MAX_SUPPLIED_TRIAL_RECORDS:
            raise EvaluationError(
                f"summary input exceeds the {MAX_SUPPLIED_TRIAL_RECORDS}-trial bound"
            )
        campaign_path = args.campaign.expanduser().absolute()
        plan_path = args.plan.expanduser().absolute()
        campaign, campaign_sha256, _ = load_json(campaign_path)
        validate_campaign(campaign, campaign_path=campaign_path)
        plan, plan_sha256, _ = load_json(plan_path)
        validate_plan(plan, campaign, campaign_sha256)
        trials: list[dict[str, Any]] = []
        for path in args.trial:
            trial, _, _ = load_json(path.expanduser().absolute())
            trials.append(trial)
        seed_hex = args.seed_hex if args.seed_hex is not None else _seed_file_value(args.seed_file)
        private_seed = load_trial_signing_seed(args.signing_key)
        summary = build_summary(
            campaign=campaign,
            campaign_sha256=campaign_sha256,
            plan=plan,
            plan_sha256=plan_sha256,
            trials=trials,
            seed_hex=seed_hex,
            created_at=args.created_at or utc_now(),
            private_seed=private_seed,
        )
        emit_json(summary, pretty=args.pretty)
        return 0 if summary["comparison"]["status"] == "ready" else 1
    except (EvaluationError, OSError, ValueError) as exc:
        emit_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
