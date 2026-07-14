from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "evaluation" / "paired-agent"
if str(EVALUATION) not in sys.path:
    sys.path.insert(0, str(EVALUATION))

import keygen  # noqa: E402
from common import EvaluationError, load_trial_signing_seed  # noqa: E402


def test_keygen_cli_creates_owner_only_private_seed_and_public_identity(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "trial-signing-key.txt"
    process = subprocess.run(
        [sys.executable, str(EVALUATION / "keygen.py"), str(private_path)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.returncode == 0
    assert process.stderr == ""
    identity = json.loads(process.stdout)
    assert identity["algorithm"] == "ed25519"
    assert len(identity["public_key_hex"]) == 64
    assert identity["key_id"] == hashlib.sha256(
        bytes.fromhex(identity["public_key_hex"])
    ).hexdigest()
    seed_hex = private_path.read_text(encoding="ascii").removesuffix("\n")
    assert len(seed_hex) == 64
    assert seed_hex not in process.stdout
    assert private_path.stat().st_mode & 0o777 == 0o600
    assert load_trial_signing_seed(private_path).hex() == seed_hex


def test_keygen_never_overwrites_existing_file_or_discloses_its_path(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "existing-private-key.txt"
    private_path.write_text("do-not-overwrite\n", encoding="utf-8")
    before = private_path.read_bytes()
    process = subprocess.run(
        [sys.executable, str(EVALUATION / "keygen.py"), str(private_path)],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.returncode == 2
    assert private_path.read_bytes() == before
    assert str(private_path) not in process.stdout
    error = json.loads(process.stdout)
    assert error == {
        "message": "evaluation input or invariant failed",
        "schema": "openada.eval.error/v0alpha1",
        "status": "error",
    }


def test_keygen_removes_partial_file_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "partial-key.txt"
    monkeypatch.setattr(keygen.os, "write", lambda descriptor, payload: 0)
    with pytest.raises(EvaluationError, match="cannot write"):
        keygen.generate(private_path)
    assert not private_path.exists()


def test_signing_seed_loader_rejects_non_owner_only_mode(tmp_path: Path) -> None:
    private_path = tmp_path / "permissive-key.txt"
    private_path.write_text("01" * 32 + "\n", encoding="ascii")
    private_path.chmod(0o640)
    with pytest.raises(EvaluationError, match="owner-only"):
        load_trial_signing_seed(private_path)
