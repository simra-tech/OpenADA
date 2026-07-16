from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).parents[1]
CHAIN = ROOT / "conformance/ihp-ngspice-provider-analyses"


def _oracle_module():
    spec = importlib.util.spec_from_file_location(
        "_ihp_ngspice_provider_oracle", CHAIN / "oracle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _publication_module():
    spec = importlib.util.spec_from_file_location(
        "_ihp_ngspice_provider_publication", CHAIN / "verify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_four_analysis_chain_manifest_and_provisional_run_are_schema_valid() -> None:
    cases = (
        ("manifest.json", "schemas/semantic-chain-manifest-v0alpha1.schema.json"),
        ("semantic-chain-run.json", "schemas/semantic-chain-run-v0alpha1.schema.json"),
    )
    for document_name, schema_name in cases:
        document = json.loads((CHAIN / document_name).read_text(encoding="utf-8"))
        schema = json.loads((ROOT / schema_name).read_text(encoding="utf-8"))
        assert list(
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).iter_errors(document)
        ) == []

    manifest = json.loads((CHAIN / "manifest.json").read_text(encoding="utf-8"))
    repository_rows = [
        row
        for row in manifest["covers"]
        if row.startswith(
            "repository-provider|org.openada.driver.ngspice-pdk-control|"
        )
    ]
    assert len(repository_rows) == 4
    assert {row.rsplit("|", 1)[-1] for row in repository_rows} == {
        f"openada.feature/simulation.analysis.{name}/v1alpha1"
        for name in ("op", "dc", "ac", "tran")
    }


def test_independent_oracle_reconstructs_native_counts_and_scoped_decisions() -> None:
    oracle = _oracle_module()

    raw, normalized, decision = oracle.verify(CHAIN / "artifacts/native-replay")

    assert raw["status"] == "pass"
    assert {
        name: normalized["analyses"][name]["point_count"]
        for name in ("op", "dc", "ac", "tran")
    } == {"op": 1, "dc": 121, "ac": 701, "tran": 1024}
    assert normalized["analyses"]["tran"]["plotname"] == (
        "Transient Analysis (linearized)"
    )
    assert normalized["analyses"]["ac"]["unity_crossing_in_band"] is True
    assert len(decision["decisions"]) == 4
    assert raw["negative_replays"]["tran-native-error"]["native_launched"] is True
    assert all(
        not value["native_launched"]
        for name, value in raw["negative_replays"].items()
        if name != "tran-native-error"
    )


def test_independent_oracle_rejects_one_byte_native_raw_tamper(tmp_path: Path) -> None:
    oracle = _oracle_module()
    evidence = tmp_path / "native-replay"
    shutil.copytree(CHAIN / "artifacts/native-replay", evidence)
    raw = evidence / "native/ac/work/ac.raw"
    body = bytearray(raw.read_bytes())
    body[-1] ^= 1
    raw.write_bytes(body)

    with pytest.raises(oracle.OracleError, match="does not bind raw bytes"):
        oracle.verify(evidence)


def test_checked_in_provider_publication_passes_the_complete_offline_verifier() -> None:
    publication = _publication_module()

    report = publication.verify_publication(
        CHAIN / "artifacts", allow_provisional=True
    )

    assert report["status"] == "pass"
    assert report["analysis_point_counts"] == {
        "op": 1,
        "dc": 121,
        "ac": 701,
        "tran": 1024,
    }
    assert report["negative_replay_count"] == 5
    assert report["tamper_replay_count"] == 5
