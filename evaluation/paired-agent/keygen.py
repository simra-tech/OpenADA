#!/usr/bin/env python3
"""Create one owner-only Ed25519 trial-signing seed and print its public identity."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys

from common import EvaluationError, emit_error, emit_json


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "private_key",
        type=Path,
        help="New owner-only file for the 32-byte private seed as lowercase hex.",
    )
    return parser


def generate(private_key_path: Path) -> dict[str, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise EvaluationError("Ed25519 support requires the conformance dependencies") from exc
    seed = secrets.token_bytes(32)
    try:
        path = private_key_path.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise EvaluationError("cannot create the owner-only trial signing key") from exc
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
    except OSError as exc:
        raise EvaluationError("cannot create the owner-only trial signing key") from exc
    try:
        payload = seed.hex().encode("ascii") + b"\n"
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short signing-key write")
            written += count
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
                path.unlink()
            except OSError:
                pass
        raise EvaluationError("cannot write the owner-only trial signing key") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "ed25519",
        "public_key_hex": public_key.hex(),
        "key_id": hashlib.sha256(public_key).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        emit_json(generate(args.private_key))
        return 0
    except (EvaluationError, OSError, ValueError) as exc:
        emit_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
