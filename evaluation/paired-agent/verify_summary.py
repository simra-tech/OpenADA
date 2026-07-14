#!/usr/bin/env python3
"""Authenticate or fully recompute one paired-evaluation summary."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from common import (
    EvaluationError,
    canonical_json_bytes,
    canonical_json_sha256,
    emit_error,
    emit_json,
    load_json,
    validate_campaign,
    validate_plan,
    validate_schema,
    verify_summary_seal,
)
from summarize import MAX_SUPPLIED_TRIAL_RECORDS, build_unsigned_summary


VERIFICATION_SCHEMA = "openada.eval.summary-verification/v0alpha1"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Authenticate an evaluation summary with the campaign public key. "
            "Supplying --plan enables full semantic recomputation and requires "
            "every signed trial row committed by the summary evidence list."
        )
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--plan",
        type=Path,
        help=(
            "Exact frozen plan for full verification. Without it, verification "
            "authenticates the summary seal but does not recompute its claims."
        ),
    )
    parser.add_argument(
        "--trial",
        type=Path,
        action="append",
        default=[],
        help=(
            "Signed assembled trial; repeat for every evidence row when --plan "
            "is supplied, including repeated rows in a duplicate/refused summary."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def authenticate_summary(
    campaign: dict[str, Any],
    campaign_sha256: str,
    summary: dict[str, Any],
) -> None:
    """Validate the public shape, campaign binding, and publisher signature."""

    validate_schema(summary, "summary-v0alpha1.schema.json", label="evaluation summary")
    if summary["campaign_id"] != campaign["campaign_id"]:
        raise EvaluationError("summary campaign ID differs from the campaign")
    if summary["campaign_sha256"] != campaign_sha256:
        raise EvaluationError("summary campaign hash differs from the exact campaign bytes")
    verify_summary_seal(summary, campaign)


def verify_full_summary(
    *,
    campaign: dict[str, Any],
    campaign_sha256: str,
    plan: dict[str, Any],
    plan_sha256: str,
    summary: dict[str, Any],
    trials: list[dict[str, Any]],
) -> None:
    """Recompute every unsigned summary field from the exact supplied rows."""

    authenticate_summary(campaign, campaign_sha256, summary)
    validate_plan(plan, campaign, campaign_sha256)
    if summary["plan_sha256"] != plan_sha256:
        raise EvaluationError("summary plan hash differs from the exact plan bytes")
    expected = build_unsigned_summary(
        campaign=campaign,
        campaign_sha256=campaign_sha256,
        plan=plan,
        plan_sha256=plan_sha256,
        trials=trials,
        seed_hex=summary["randomization"]["seed_hex"],
        created_at=summary["created_at"],
    )
    if canonical_json_bytes(summary["evidence"]) != canonical_json_bytes(
        expected["evidence"]
    ):
        raise EvaluationError(
            "supplied trial rows differ from the signed summary evidence bindings"
        )
    unsigned = {key: value for key, value in summary.items() if key != "seal"}
    if canonical_json_bytes(unsigned) != canonical_json_bytes(expected):
        raise EvaluationError(
            "signed summary fields differ from exact semantic recomputation"
        )


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if len(args.trial) > MAX_SUPPLIED_TRIAL_RECORDS:
            raise EvaluationError(
                f"summary input exceeds the {MAX_SUPPLIED_TRIAL_RECORDS}-trial bound"
            )
        campaign_path = args.campaign.expanduser().absolute()
        campaign, campaign_sha256, _ = load_json(campaign_path)
        validate_campaign(campaign, campaign_path=campaign_path)
        summary, _, _ = load_json(args.summary.expanduser().absolute())
        authenticate_summary(campaign, campaign_sha256, summary)

        mode = "summary-only"
        if args.plan is None:
            if args.trial:
                raise EvaluationError("--trial requires --plan for full verification")
        else:
            plan, plan_sha256, _ = load_json(args.plan.expanduser().absolute())
            trials = [
                load_json(path.expanduser().absolute())[0] for path in args.trial
            ]
            verify_full_summary(
                campaign=campaign,
                campaign_sha256=campaign_sha256,
                plan=plan,
                plan_sha256=plan_sha256,
                summary=summary,
                trials=trials,
            )
            mode = "full"

        result = {
            "schema": VERIFICATION_SCHEMA,
            "status": "valid",
            "mode": mode,
            "campaign_sha256": campaign_sha256,
            "plan_sha256": summary["plan_sha256"],
            "canonical_summary_sha256": canonical_json_sha256(summary),
            "evidence_records": len(summary["evidence"]),
        }
        validate_schema(
            result,
            "summary-verification-v0alpha1.schema.json",
            label="summary verification result",
        )
        emit_json(result, pretty=args.pretty)
        return 0
    except (EvaluationError, OSError, ValueError) as exc:
        emit_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
