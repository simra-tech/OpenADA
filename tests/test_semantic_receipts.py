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


def test_provider_manifest_hash_refuses_deep_nesting():
    # The provider-manifest semantic hash parses bytes directly, bypassing any
    # caller-side depth guard; deep nesting previously crashed json.loads /
    # json.dumps with an uncaught RecursionError. It must fail closed.
    from tools.semantic_receipts import provider_manifest_semantic_sha256_bytes

    depth = 1100
    deep = (
        '{"conformance_records":[],"x":'
        + "[" * depth + "0" + "]" * depth
        + "}"
    ).encode("utf-8")
    with pytest.raises(SemanticReceiptError, match="nests deeper than"):
        provider_manifest_semantic_sha256_bytes(deep)


def test_canonical_sha256_refuses_deep_value():
    # Defense-in-depth: even a directly constructed deep value must not fault
    # the encoder into an uncaught RecursionError.
    from tools.semantic_receipts import canonical_sha256

    value: dict = {}
    cursor = value
    for _ in range(3000):
        cursor["k"] = {}
        cursor = cursor["k"]
    with pytest.raises(SemanticReceiptError, match="too deeply"):
        canonical_sha256(value)


def test_canonical_sha256_refuses_cyclic_value():
    from tools.semantic_receipts import canonical_sha256

    value: dict = {}
    value["cycle"] = value
    with pytest.raises(SemanticReceiptError, match="not strict canonical JSON"):
        canonical_sha256(value)


def test_max_json_depth_scan_is_string_aware():
    from tools.semantic_receipts import _max_json_depth_within

    # Real shallow structure passes; brackets inside a string literal are text.
    assert _max_json_depth_within('{"a":{"b":[1,2]}}', 64) is True
    assert _max_json_depth_within('{"note":"' + "[" * 500 + '"}', 64) is True
    # An escaped quote does not end the string.
    assert _max_json_depth_within('{"a":"q \\" ' + "[" * 200 + ' t"}', 64) is True
    # Genuine deep structure is refused.
    assert _max_json_depth_within("[" * 65 + "0" + "]" * 65, 64) is False
