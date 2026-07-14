#!/usr/bin/env python3
"""Create a frozen, interleaved paired assignment plan before trials run."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sys

from common import (
    EvaluationError,
    PLAN_RANDOMIZATION_ALGORITHM,
    PLAN_SCHEMA,
    emit_error,
    emit_json,
    load_json,
    parse_timestamp,
    utc_now,
    validate_campaign,
    validate_plan,
)


# Version 1 consumes one HMAC-SHA256 digest per draw.  The HMAC message is this
# domain, the lowercase ASCII campaign digest, a NUL separator, and an unsigned
# 64-bit big-endian counter starting at zero.  A draw below ``upper_bound`` uses
# rejection sampling over the full 256-bit digest space.  Lists are shuffled by
# descending Fisher-Yates indices.  These details are part of the persisted
# algorithm identity and are covered by known-answer tests.
_RANDOMIZATION_DOMAIN = (
    b"openada.eval.plan/randomization/hmac-sha256-fisher-yates-v1\x00"
)
_HMAC_SPACE = 1 << 256
_MAX_HMAC_COUNTER = (1 << 64) - 1


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Validate a paired evaluation campaign and emit its preassigned "
            "randomized AB/BA trial plan."
        )
    )
    parser.add_argument("campaign", type=Path, help="Frozen campaign JSON file.")
    seed = parser.add_mutually_exclusive_group(required=True)
    seed.add_argument(
        "--seed-hex",
        help=(
            "Exactly 64 lowercase hexadecimal characters (tests/reproduction only; "
            "visible in process listings)."
        ),
    )
    seed.add_argument(
        "--generate-seed-file",
        type=Path,
        help=(
            "Atomically create a mode-0600 seed reveal file; keep it private "
            "until every planned trial is complete."
        ),
    )
    parser.add_argument(
        "--created-at",
        help="RFC 3339 UTC plan time (tests/reproduction only; default: current time).",
    )
    parser.add_argument("--pretty", action="store_true", help="Indent emitted JSON.")
    return parser


def _seed(value: str | None) -> bytes:
    if value is None:
        return secrets.token_bytes(32)
    if len(value) != 64 or value != value.lower():
        raise EvaluationError("--seed-hex must contain exactly 64 lowercase hex characters")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise EvaluationError("--seed-hex is not valid hexadecimal") from exc
    if len(decoded) != 32:
        raise EvaluationError("--seed-hex must decode to exactly 32 bytes")
    return decoded


def _write_seed_file(path: Path, seed: bytes) -> None:
    try:
        target = path.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise EvaluationError("cannot resolve --generate-seed-file") from exc
    payload = seed.hex().encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(target, flags, 0o600)
        created = True
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short seed-file write")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = None
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        raise EvaluationError("cannot create the private seed reveal file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _opaque_id(
    seed: bytes,
    campaign_sha256: str,
    *,
    label: str,
    index: int,
) -> str:
    message = f"{campaign_sha256}:{label}:{index}".encode("ascii")
    token = hmac.new(seed, message, hashlib.sha256).hexdigest()[:24]
    return f"{label}-{token}"


class _DeterministicRandomizer:
    """Runtime-independent bounded draws from a campaign-separated HMAC PRF."""

    def __init__(self, seed: bytes, campaign_sha256: str) -> None:
        if (
            len(campaign_sha256) != 64
            or campaign_sha256 != campaign_sha256.lower()
            or any(character not in "0123456789abcdef" for character in campaign_sha256)
        ):
            raise EvaluationError("campaign SHA-256 must be a lowercase hexadecimal digest")
        self._seed = seed
        self._message_prefix = (
            _RANDOMIZATION_DOMAIN + campaign_sha256.encode("ascii") + b"\x00"
        )
        self._counter = 0

    def _candidate(self) -> int:
        if self._counter > _MAX_HMAC_COUNTER:
            raise EvaluationError("deterministic randomization stream is exhausted")
        message = self._message_prefix + self._counter.to_bytes(8, "big")
        self._counter += 1
        return int.from_bytes(
            hmac.new(self._seed, message, hashlib.sha256).digest(), "big"
        )

    def below(self, upper_bound: int) -> int:
        if (
            isinstance(upper_bound, bool)
            or not isinstance(upper_bound, int)
            or not 1 <= upper_bound <= _HMAC_SPACE
        ):
            raise EvaluationError("deterministic random bound is outside its domain")
        acceptance_limit = _HMAC_SPACE - (_HMAC_SPACE % upper_bound)
        while True:
            candidate = self._candidate()
            if candidate < acceptance_limit:
                return candidate % upper_bound

    def shuffle(self, values: list[str]) -> None:
        for index in range(len(values) - 1, 0, -1):
            selected = self.below(index + 1)
            values[index], values[selected] = values[selected], values[index]


def build_plan(
    campaign: dict,
    campaign_sha256: str,
    *,
    seed: bytes,
    created_at: str,
) -> dict:
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise EvaluationError("plan seed must contain exactly 32 bytes")
    parse_timestamp(created_at, label="plan.created_at")
    campaign_time = parse_timestamp(campaign["created_at"], label="campaign.created_at")
    if parse_timestamp(created_at, label="plan.created_at") < campaign_time:
        raise EvaluationError("plan.created_at cannot precede campaign.created_at")

    pair_count = campaign["planned_pairs"]
    # Balance first-condition order to within one pair, then randomize which
    # opaque block receives each order. Pair members remain adjacent.
    randomizer = _DeterministicRandomizer(seed, campaign_sha256)
    raw_first_count = pair_count // 2
    openada_first_count = pair_count // 2
    if pair_count % 2:
        if randomizer.below(2):
            raw_first_count += 1
        else:
            openada_first_count += 1
    orders = ["raw"] * raw_first_count + ["openada"] * openada_first_count
    randomizer.shuffle(orders)

    assignments: list[dict] = []
    sequence = 1
    for index, first in enumerate(orders, start=1):
        second = "openada" if first == "raw" else "raw"
        pair_id = _opaque_id(seed, campaign_sha256, label="pair", index=index)
        for position, condition in enumerate((first, second), start=1):
            trial_index = (index - 1) * 2 + position
            assignments.append(
                {
                    "sequence": sequence,
                    "pair_id": pair_id,
                    "trial_id": _opaque_id(
                        seed,
                        campaign_sha256,
                        label="trial",
                        index=trial_index,
                    ),
                    "condition": condition,
                    "pair_position": position,
                }
            )
            sequence += 1

    plan = {
        "schema": PLAN_SCHEMA,
        "randomization_algorithm": PLAN_RANDOMIZATION_ALGORITHM,
        "campaign_id": campaign["campaign_id"],
        "campaign_sha256": campaign_sha256,
        "created_at": created_at,
        "seed_sha256": hashlib.sha256(seed).hexdigest(),
        "planned_pairs": pair_count,
        "assignments": assignments,
    }
    validate_plan(plan, campaign, campaign_sha256)
    return plan


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        campaign_path = args.campaign.expanduser().absolute()
        campaign, campaign_sha256, _ = load_json(campaign_path)
        validate_campaign(campaign, campaign_path=campaign_path)
        created_at = args.created_at or utc_now()
        seed = _seed(args.seed_hex)
        plan = build_plan(
            campaign,
            campaign_sha256,
            seed=seed,
            created_at=created_at,
        )
        if args.generate_seed_file is not None:
            _write_seed_file(args.generate_seed_file, seed)
        emit_json(plan, pretty=args.pretty)
        return 0
    except (EvaluationError, OSError, ValueError) as exc:
        emit_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
