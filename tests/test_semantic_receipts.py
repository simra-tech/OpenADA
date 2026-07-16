from __future__ import annotations

from pathlib import Path

import pytest

from tools.semantic_receipts import (
    SemanticReceiptError,
    atomic_write_text,
)


def test_atomic_semantic_write_replaces_regular_destination(tmp_path: Path) -> None:
    destination = tmp_path / "receipt.json"
    destination.write_text("old\n", encoding="utf-8")

    atomic_write_text(destination, "new\n")

    assert destination.read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "receipt.json.tmp").exists()


def test_atomic_semantic_write_rejects_preexisting_staging_symlink(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "receipt.json"
    victim = tmp_path / "victim.json"
    destination.write_text("old\n", encoding="utf-8")
    victim.write_text("untouched\n", encoding="utf-8")
    staging = tmp_path / "receipt.json.tmp"
    staging.symlink_to(victim)

    with pytest.raises(SemanticReceiptError, match="staging path"):
        atomic_write_text(destination, "attacker-controlled\n")

    assert destination.read_text(encoding="utf-8") == "old\n"
    assert victim.read_text(encoding="utf-8") == "untouched\n"
    assert staging.is_symlink()
