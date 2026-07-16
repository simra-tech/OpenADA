from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import sys

from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "conformance/orfs-ibex-synthesis-timing"
PUBLISHED = HERE / "semantic-artifacts"


def _load_local_modules():
    names = ("common", "verify", "semantic", "run")
    saved = {name: sys.modules.get(name) for name in names}
    for name in names:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(HERE))
    try:
        loaded = tuple(importlib.import_module(name) for name in names)
    finally:
        sys.path.pop(0)
        for name in names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]
    return loaded


common, verify, semantic, runner = _load_local_modules()


def _manifest() -> dict:
    return common.load_manifest(HERE / "manifest.json")


def _published() -> Path:
    if not (PUBLISHED / "run.json").is_file():
        pytest.skip("ORFS Ibex release evidence has not been replayed yet")
    return PUBLISHED


def _verify(evidence: Path) -> dict:
    return verify.verify_evidence(
        _manifest(),
        evidence,
        manifest_sha256=common.sha256_file(HERE / "manifest.json"),
    )


def test_manifest_pins_public_ibex_runtime_and_complete_input_inventory() -> None:
    manifest = _manifest()
    assert manifest["design"]["repository"] == common.DESIGN_REPOSITORY
    assert manifest["design"]["revision"] == common.DESIGN_REVISION
    assert manifest["design"]["tree"] == common.DESIGN_TREE
    assert manifest["design"]["upstream"]["revision"] == common.UPSTREAM_REVISION
    assert manifest["runtime"]["image"]["reference"] == common.IMAGE_REFERENCE
    assert manifest["runtime"]["image"]["config_digest"] == common.IMAGE_CONFIG_DIGEST
    assert len(manifest["pinned_files"]) == 32
    assert len(manifest["operations"]["synthesize"]["source_paths"]) == 21
    assert manifest["derived_inputs"] == [
        {
            "repository_path": common.ABC_REPOSITORY_PATH,
            "bytes": common.ABC_BYTES,
            "sha256": common.ABC_SHA256,
            "role": "synthesis-abc-constraint",
            "derivation": (
                "Reviewed projection of ABC_DRIVER_CELL=BUF_X1 and ABC_LOAD_IN_FF=3.898 "
                "from pinned flow/platforms/nangate45/config.mk."
            ),
        }
    ]


def test_runner_builds_exact_semantic_command_vectors() -> None:
    manifest = _manifest()
    synthesis = runner._synthesis_argv(manifest, "synthesize")
    assert synthesis[0] == "synthesize"
    assert synthesis.count("--dont-use") == 4
    assert synthesis[synthesis.index("--abc-delay-target-ns") + 1] == "2.2"
    assert synthesis[synthesis.index("--abc-constraint") + 1] == (
        "/openada/conformance/orfs-ibex-synthesis-timing/abc.constr"
    )
    assert synthesis[synthesis.index("--top") + 1] == "ibex_core"
    negative = runner._synthesis_argv(manifest, "missing_top")
    assert negative[negative.index("--top") + 1] == "missing_ibex_core"
    timing = runner._timing_argv(manifest)
    assert timing == [
        "timing-analyze",
        "/evidence/synthesis/mapped.v",
        "--top",
        "ibex_core",
        "--liberty",
        "/design/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib",
        "--sdc",
        "/design/flow/designs/nangate45/ibex/constraint.sdc",
        "--output-dir",
        "/evidence/timing",
        "--timeout",
        "300",
    ]


def test_independent_script_oracles_bind_every_configuration_choice() -> None:
    manifest = _manifest()
    synthesis = verify._expected_synthesis_script(manifest, "ibex_core")
    assert "read_slang --std 1800-2017 --top ibex_core" in synthesis
    assert "synth -top ibex_core -flatten -noabc" in synthesis
    assert synthesis.count('-dont_use "') == 8
    assert '-constr "/openada/conformance/orfs-ibex-synthesis-timing/abc.constr"' in synthesis
    assert " -D 2200" in synthesis
    assert "check -assert" in synthesis
    timing = verify._expected_timing_script(manifest)
    assert 'read_verilog "/evidence/synthesis/mapped.v"' in timing
    assert 'read_sdc "/evidence/timing/timing-input.sdc"' in timing
    assert "check_setup -verbose -unconstrained_endpoints" in timing
    assert "report_checks -path_delay max" in timing
    assert "report_checks -path_delay min" in timing
    assert timing.endswith("OPENADA_ANALYSIS_COMPLETE\n")


def test_run_schema_is_closed_and_binds_all_native_artifacts() -> None:
    schema = json.loads((HERE / "run.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    artifacts = schema["properties"]["native_artifacts"]
    assert artifacts["minItems"] == artifacts["maxItems"] == len(
        verify.NATIVE_ARTIFACT_PATHS
    )
    assert set(verify.NATIVE_ARTIFACT_PATHS) == set(runner.NATIVE_ARTIFACT_PATHS)


def test_independent_verifier_accepts_complete_publication() -> None:
    verified = _verify(_published())
    assert verified["verified"] is True
    assert verified["synthesis"]["result"]["engineering"]["status"] == "pass"
    assert verified["timing"]["result"]["engineering"]["status"] == "fail"
    assert verified["negative"]["result"]["engineering"]["status"] == "fail"
    assert verified["run"]["openada_checkout"]["state_unchanged"] is True


def test_synthesis_is_complete_liberty_mapping_with_agent_decision_facts() -> None:
    verified = _verify(_published())
    synthesis = verified["synthesis"]
    assert synthesis["result"]["data"]["mapping_complete"] is True
    assert synthesis["result"]["data"]["unmapped_cell_types"] == []
    assert synthesis["result"]["data"]["mapped_structure"] == {
        "top": "ibex_core",
        "num_cells": synthesis["stats"]["num_cells"],
        "num_cells_by_type": synthesis["stats"]["num_cells_by_type"],
    }
    assert synthesis["stats"]["num_cells"] == synthesis["structure"]["cell_count"]
    assert synthesis["stats"]["num_memories"] == 0
    assert synthesis["stats"]["num_processes"] == 0
    assert synthesis["structure"]["port_count"] == 30
    assert synthesis["structure"]["port_bit_count"] == 264
    assert synthesis["structure"]["liberty_cell_inventory_count"] == 135
    assert synthesis["structure"]["liberty_cell_inventory_sha256"] == (
        verify.LIBERTY_CELL_INVENTORY_SHA256
    )
    assert synthesis["stats"]["area"] > 0
    assert synthesis["stats"]["sequential_area"] > 0


def test_real_timing_failure_is_complete_and_not_promoted_to_pass() -> None:
    timing = _verify(_published())["timing"]["result"]
    data = timing["data"]
    assert timing["execution"]["status"] == "completed"
    assert timing["execution"]["exit_code"] == 0
    assert timing["engineering"]["status"] == "fail"
    assert data["constraints_complete"] is True
    assert data["netlist_validation"] == "self-contained"
    assert data["liberty_validation"] == "self-contained"
    assert data["sdc_policy"] == "openada-sdc-v1"
    assert data["sdc_validation"] == "parsed-safe-subset"
    assert data["reports_complete"] is True
    assert data["path_reports_agree_with_wns"] is True
    assert data["setup"]["wns_s"] < 0
    assert data["setup"]["tns_s"] < 0
    assert data["hold"]["wns_s"] >= 0
    assert data["hold"]["tns_s"] == 0
    assert data["timing_constraints_satisfied"] is False
    assert data["signoff_level"] is False


def test_real_missing_top_failure_is_native_and_exact() -> None:
    negative = _verify(_published())["negative"]
    assert negative["result"]["execution"]["status"] == "completed"
    assert negative["result"]["execution"]["exit_code"] != 0
    assert negative["result"]["engineering"]["status"] == "fail"
    assert "missing_ibex_core" in (
        negative["transcript"]["stdout"] + negative["transcript"]["stderr"]
    )


def test_reconciled_mapped_stat_tamper_is_rejected_as_unknown() -> None:
    verdict = semantic._run_synthesis_tamper_probe(_manifest(), _published())
    assert verdict["status"] == "rejected"
    assert verdict["expected_status"] == "unknown"
    assert verdict["id"] == semantic.SYNTH_TAMPER_ID
    assert semantic.SYNTH_TAMPER_DIAGNOSTIC in verdict["observed_diagnostic"]
    assert verdict["covers"] == semantic.SYNTHESIS_ROWS


def test_reconciled_timing_path_tamper_is_rejected_as_unknown() -> None:
    verdict = semantic._run_timing_tamper_probe(_manifest(), _published())
    assert verdict["status"] == "rejected"
    assert verdict["expected_status"] == "unknown"
    assert verdict["id"] == semantic.TIMING_TAMPER_ID
    assert semantic.TIMING_TAMPER_DIAGNOSTIC in verdict["observed_diagnostic"]
    assert verdict["covers"] == semantic.TIMING_ROWS


def test_native_input_tamper_is_rejected_even_if_run_hash_is_reconciled(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    shutil.copytree(_published(), evidence)
    path = evidence / "synthesis/rtl-inputs.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["declared_include_directories"] = []
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result_path = evidence / "synthesis/synthesize.result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    record = next(item for item in result["artifacts"] if item["role"] == "rtl.dependencies")
    record["bytes"] = path.stat().st_size
    record["sha256"] = common.sha256_file(path)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run_path = evidence / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    for relative in ("synthesis/rtl-inputs.json", "synthesis/synthesize.result.json"):
        artifact = next(item for item in run["native_artifacts"] if item["path"] == relative)
        artifact["bytes"] = (evidence / relative).stat().st_size
        artifact["sha256"] = common.sha256_file(evidence / relative)
    run_path.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(common.ConformanceError, match="synthesis input manifest"):
        _verify(evidence)


def test_agent_evidence_blocks_timing_and_preserves_synthesis_evidence() -> None:
    _published()
    agent = json.loads((HERE / "semantic-evidence.json").read_text(encoding="utf-8"))
    assert agent["decision"] == "block"
    assert agent["operations"]["synthesis"]["engineering_status"] == "pass"
    assert agent["operations"]["synthesis"]["evidence"]["mapping_complete"] is True
    assert agent["operations"]["synthesis"]["evidence"]["mapped_structure"]["top"] == "ibex_core"
    assert agent["operations"]["timing"]["engineering_status"] == "fail"
    assert agent["operations"]["timing"]["evidence"]["netlist_validation"] == "self-contained"
    assert agent["operations"]["timing"]["evidence"]["liberty_validation"] == "self-contained"
    assert agent["operations"]["timing"]["evidence"]["setup"]["wns_s"] < 0
    assert len(agent["trust_chain"]["native_artifacts"]) == len(semantic.NATIVE_FILES)
    limitation_ids = {item["id"] for item in agent["limitations"]}
    assert {"single-corner-ideal-interconnect", "setup-timing-fails", "no-physical-or-power-closure"} <= limitation_ids


def test_standards_boundary_is_explicit_and_does_not_claim_conformance() -> None:
    _published()
    standards = json.loads((HERE / "semantic-evidence.json").read_text(encoding="utf-8"))["standards"]
    assert standards["ieee_measurement_standard"]["status"] == "not-applicable"
    assert standards["hdl_language"]["standard"] == "IEEE 1800-2023"
    assert standards["hdl_language"]["status"] == "context-only"
    assert standards["timing_formats"]["status"] == "implementation-specific"


def test_semantic_chain_manifest_is_closed_and_covers_exact_digital_rows() -> None:
    schema = json.loads(
        (ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    chain = json.loads((HERE / "semantic-chain.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(chain)) == []
    assert chain["id"] == common.CHAIN_ID
    assert chain["covers"] == semantic.ROWS
    semantic_steps = [item for item in chain["steps"] if item["kind"] == "semantic-command"]
    assert set().union(*(set(item["covers"]) for item in semantic_steps)) == set(
        semantic.ROWS
    )
    native_synthesis = next(item for item in semantic_steps if item["id"] == "synthesize-ibex")
    native_timing = next(item for item in semantic_steps if item["id"] == "analyze-ibex-timing")
    assert native_synthesis["native_execution"] is True
    assert native_synthesis["covers"] == semantic.SYNTHESIS_ROWS
    assert native_timing["native_execution"] is True
    assert native_timing["covers"] == semantic.TIMING_ROWS
    assert {item["id"] for item in chain["negative_replays"]} == {
        semantic.SYNTH_NEGATIVE_ID,
        semantic.TIMING_NEGATIVE_ID,
    }
    assert {item["id"] for item in chain["tamper_replays"]} == {
        semantic.SYNTH_TAMPER_ID,
        semantic.TIMING_TAMPER_ID,
    }


def test_semantic_chain_run_binds_distinct_complete_trust_artifacts() -> None:
    _published()
    semantic.verify_publication()
    run_schema = json.loads(
        (ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    run = json.loads((HERE / "semantic-chain-run.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(run_schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(run)) == []
    assert run["status"] == "pass"
    assert all(run["checks"].values())
    trust_roles = {
        "independent-oracle",
        "normalized-evidence",
        "downstream-decision",
        "agent-visible-evidence",
    }
    hashes = [item["sha256"] for item in run["artifacts"] if item["role"] in trust_roles]
    assert len(hashes) == len(trust_roles)
    assert len(set(hashes)) == len(hashes)


def test_contract_report_counts_the_complete_chain_suite() -> None:
    _published()
    contract = json.loads((HERE / "semantic-contract.json").read_text(encoding="utf-8"))
    assert contract == semantic._contract_document()
    assert contract["tests"]["failed"] == 0
    assert contract["tests"]["passed"] == 16
