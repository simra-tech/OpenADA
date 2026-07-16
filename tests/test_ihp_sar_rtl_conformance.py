from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import shutil
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "conformance/ihp-sar-rtl"
PUBLISHED = HERE / "semantic-artifacts"


def _load_local_modules():
    names = ("common", "verify", "run", "semantic")
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


common, verifier, runner, semantic = _load_local_modules()


def _manifest():
    return common.load_manifest(HERE / "manifest.json")


def _verify(path: Path):
    return verifier.verify_evidence(
        _manifest(),
        path,
        manifest_sha256=common.sha256_file(HERE / "manifest.json"),
    )


def _write(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _update_run_record(evidence: Path, relative: str) -> None:
    run_path = evidence / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    records = [item for item in run["native_artifacts"] if item["path"] == relative]
    assert len(records) == 1
    path = evidence / relative
    records[0]["bytes"] = path.stat().st_size
    records[0]["sha256"] = common.sha256_file(path)
    _write(run_path, run)


def test_manifest_pins_public_source_image_and_real_negative() -> None:
    manifest = _manifest()
    assert manifest["design"]["revision"] == common.DESIGN_REVISION
    assert manifest["source"]["sha256"] == common.SOURCE_SHA256
    assert manifest["source"]["bytes"] == 576
    assert manifest["runtime"]["image"]["reference"] == common.IMAGE_REFERENCE
    assert manifest["runtime"]["image"]["config_digest"] == common.IMAGE_CONFIG_DIGEST
    assert manifest["runtime"]["lint_tool"] == {
        "name": "verilator",
        "requested_path": common.VERILATOR_REQUESTED_PATH,
        "native_path": common.NATIVE_VERILATOR_PATH,
        "version": common.VERILATOR_VERSION,
    }
    assert manifest["operations"]["missing_top"]["expect"]["diagnostic"] == (
        "ERROR: Module `missing_sar_logic' not found!"
    )
    assert manifest["operations"]["rtl_lint"]["language"] == "1800-2017"
    assert manifest["operations"]["rtl_lint_2023"]["language"] == "1800-2023"


def test_wrapper_is_executable_and_calls_only_pinned_yosys() -> None:
    wrapper = HERE / "yosys_wrapper.py"
    assert wrapper.stat().st_mode & 0o111
    body = wrapper.read_text(encoding="utf-8")
    assert f'NATIVE_YOSYS = "{common.NATIVE_YOSYS_PATH}"' in body
    assert "shell=True" not in body


def test_run_schema_accepts_published_run() -> None:
    schema = json.loads((HERE / "run.schema.json").read_text(encoding="utf-8"))
    run = json.loads((PUBLISHED / "run.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(run)) == []


def test_independent_verifier_accepts_complete_publication() -> None:
    verified = _verify(PUBLISHED)
    assert verified["verified"] is True
    assert verified["positive"]["result"]["engineering"]["status"] == "pass"
    assert verified["negative"]["result"]["engineering"]["status"] == "fail"
    assert verified["lint_positive"]["result"]["engineering"]["status"] == "pass"
    assert verified["lint_positive_2023"]["result"]["engineering"]["status"] == "pass"
    assert verified["lint_negative"]["result"]["engineering"]["status"] == "fail"
    assert verified["run"]["openada_checkout"]["state_unchanged"] is True


def test_independent_structure_has_exact_interface_and_state() -> None:
    structure = _verify(PUBLISHED)["positive"]["structure"]
    assert structure["module_names"] == ["sar_logic"]
    assert {name: item["width"] for name, item in structure["ports"].items()} == {
        "clk": 1,
        "Op": 1,
        "En": 1,
        "Om": 1,
        "rst": 1,
        "B": 8,
        "BN": 8,
        "D": 8,
    }
    assert structure["cell_count"] == 18
    assert structure["cell_type_counts"]["$sdffe"] == 3
    assert structure["state_widths"] == [4, 8, 8]
    assert structure["counter_width"] == 4
    assert structure["b_aliases_d"] is True
    assert structure["blackbox_cells"] == 0


def test_real_missing_top_native_failure_is_exact() -> None:
    negative = _verify(PUBLISHED)["negative"]
    assert negative["result"]["execution"]["exit_code"] == 1
    assert negative["diagnostic"] == "ERROR: Module `missing_sar_logic' not found!"
    assert negative["diagnostic"] in negative["native_stderr"]


def test_strict_lint_is_clean_and_real_missing_top_is_exact() -> None:
    verified = _verify(PUBLISHED)
    positive = verified["lint_positive"]
    assert positive["result"]["tool"] == {
        "name": "verilator",
        "path": common.NATIVE_VERILATOR_PATH,
        "version": common.VERILATOR_VERSION,
    }
    assert positive["result"]["data"]["warning_policy"] == "strict"
    assert positive["result"]["data"]["warning_count"] == 0
    assert positive["result"]["data"]["error_count"] == 0
    assert positive["result"]["data"]["inputs_stable"] is True
    assert positive["result"]["data"]["dependency_closure_stable"] is True
    assert positive["result"]["data"]["tool_identity_stable"] is True
    assert positive["transcript"]["diagnostics"] == []
    positive_2023 = verified["lint_positive_2023"]
    assert positive_2023["result"]["engineering"]["status"] == "pass"
    assert positive_2023["result"]["data"]["language"] == "1800-2023"
    assert positive_2023["result"]["data"]["warning_count"] == 0
    assert positive_2023["result"]["data"]["error_count"] == 0
    assert positive_2023["result"]["data"]["diagnostic_count"] == 0
    assert positive_2023["result"]["data"]["inputs_stable"] is True
    assert positive_2023["result"]["data"]["dependency_closure_stable"] is True
    assert positive_2023["result"]["data"]["tool_identity_stable"] is True
    command_2023 = positive_2023["result"]["execution"]["command"]
    assert command_2023[command_2023.index("--default-language") + 1] == "1800-2023"
    for suffix in ("v", "sv", "vh", "svh"):
        assert f"+1800-2023ext+{suffix}" in command_2023
    negative = verified["lint_negative"]
    assert negative["result"]["execution"]["exit_code"] == 1
    assert negative["result"]["engineering"]["status"] == "fail"
    assert [item["message"] for item in negative["transcript"]["diagnostics"]] == [
        "Specified --top-module 'missing_sar_logic' was not found in design.",
        "Exiting due to 1 error(s)",
    ]


def test_reconciled_native_json_port_tamper_is_rejected() -> None:
    verdict = semantic._run_tamper_probe(_manifest(), PUBLISHED)
    assert verdict["status"] == "rejected"
    assert verdict["id"] == "reconciled-json-port-removal"
    assert "positive Yosys JSON ports" in verdict["observed_diagnostic"]
    assert "reconciled" in verdict["mutation"]


def test_reconciled_lint_log_finding_injection_is_rejected() -> None:
    verdict = semantic._run_lint_tamper_probe(_manifest(), PUBLISHED)
    assert verdict["status"] == "rejected"
    assert verdict["id"] == "reconciled-lint-log-finding-injection"
    assert "native Verilator transcript diagnostics" in verdict["observed_diagnostic"]
    assert verdict["covers"] == semantic.LINT_ROWS


def test_source_hash_drift_is_rejected_even_if_run_digest_is_reconciled(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    shutil.copytree(PUBLISHED, evidence)
    result_path = evidence / "positive/rtl-check.result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["inputs"][0]["sha256"] = "a" * 64
    _write(result_path, result)
    _update_run_record(evidence, "positive/rtl-check.result.json")
    with pytest.raises(common.ConformanceError, match=r"positive\.inputs\[0\]"):
        _verify(evidence)


def test_unbound_native_transcript_change_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    shutil.copytree(PUBLISHED, evidence)
    transcript_path = evidence / "positive/yosys.transcript.json"
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript["stdout"]["base64"] = "WA=="
    transcript["stdout"]["bytes"] = 1
    transcript["stdout"]["sha256"] = (
        "4b68ab3847feda7d6c62c1fbcbeebfa35eab7351ed5e78f4ddadea5df64b8015"
    )
    _write(transcript_path, transcript)
    with pytest.raises(common.ConformanceError, match="run.native_artifacts"):
        _verify(evidence)


def test_unexpected_evidence_file_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    shutil.copytree(PUBLISHED, evidence)
    (evidence / "unbound.txt").write_text("not evidence\n", encoding="utf-8")
    with pytest.raises(common.ConformanceError, match="evidence file set"):
        _verify(evidence)


def test_existing_evidence_path_is_refused(tmp_path: Path) -> None:
    existing = tmp_path / "already-exists"
    existing.mkdir()
    with pytest.raises(common.ConformanceError, match="already exists"):
        runner._validate_evidence_location(existing, {"checkout": ROOT})


def test_publication_is_closed_and_has_distinct_trust_artifacts() -> None:
    semantic.verify_publication()
    paths = [
        HERE / "semantic-oracle.json",
        HERE / "semantic-normalized.json",
        HERE / "semantic-decision.json",
        HERE / "semantic-evidence.json",
    ]
    assert len({common.sha256_file(path) for path in paths}) == len(paths)
    agent = json.loads((HERE / "semantic-evidence.json").read_text(encoding="utf-8"))
    assert agent["decision"] == "proceed"
    assert agent["operations"]["missing_top_negative"]["decision"] == "block"
    assert agent["operations"]["rtl_lint"]["engineering_status"] == "pass"
    assert agent["operations"]["rtl_lint"]["evidence"]["language"] == "1800-2017"
    assert agent["operations"]["rtl_lint_2023"]["engineering_status"] == "pass"
    assert agent["operations"]["rtl_lint_2023"]["evidence"]["language"] == "1800-2023"
    assert agent["operations"]["lint_missing_top_negative"]["decision"] == "block"
    assert len(agent["trust_chain"]["native_artifacts"]) == len(semantic.NATIVE_FILES)
    contract = json.loads((HERE / "semantic-contract.json").read_text(encoding="utf-8"))
    assert contract["tests"]["passed"] == 18
    assert contract["tests"]["failed"] == 0


def test_agent_decision_states_next_checks_and_scope_limits() -> None:
    agent = json.loads((HERE / "semantic-evidence.json").read_text(encoding="utf-8"))
    assert "not tapeout signoff" in agent["scope"]
    assert len(agent["basis"]) >= 6
    assert len(agent["next_checks"]) >= 4
    limitation_ids = {item["id"] for item in agent["limitations"]}
    assert "structural-elaboration-only" in limitation_ids
    assert "no-timing-or-physical-closure" in limitation_ids
    assert "strict-lint-only" in limitation_ids
    strict_lint = next(
        item for item in agent["limitations"] if item["id"] == "strict-lint-only"
    )
    for boundary in (
        "functional correctness",
        "CDC safety",
        "timing closure",
        "physical correctness",
    ):
        assert boundary in strict_lint["impact"]


def test_ieee_boundary_is_explicit_and_does_not_claim_conformance() -> None:
    standards = json.loads((HERE / "semantic-evidence.json").read_text(encoding="utf-8"))[
        "standards"
    ]
    assert standards["ieee_measurement_standard"]["status"] == "not-applicable"
    assert standards["hdl_language"]["standard"] == "IEEE 1800-2023"
    assert standards["hdl_language"]["status"] == "context-only"
    assert "not full language-standard compliance" in standards["hdl_language"]["basis"]


def test_semantic_chain_manifest_is_closed_and_covers_structural_and_lint_rows() -> None:
    schema = json.loads(
        (ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    chain = json.loads((HERE / "semantic-chain.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(chain)) == []
    expected = set(semantic.STRUCTURAL_ROWS + semantic.LINT_ROWS)
    assert set(chain["covers"]) == expected
    semantic_steps = [item for item in chain["steps"] if item["kind"] == "semantic-command"]
    assert set().union(*(set(item["covers"]) for item in semantic_steps)) == expected
    native_positive = [
        item
        for item in semantic_steps
        if item["id"] == "rtl-structural-check" and item["native_execution"]
    ]
    assert len(native_positive) == 1
    assert set(native_positive[0]["covers"]) == set(semantic.STRUCTURAL_ROWS)
    lint_positive = [
        item
        for item in semantic_steps
        if item["id"] in {"rtl-lint-clean", "rtl-lint-clean-2023"}
        and item["native_execution"]
    ]
    assert {item["id"] for item in lint_positive} == {
        "rtl-lint-clean",
        "rtl-lint-clean-2023",
    }
    for item in lint_positive:
        assert set(item["covers"]) == set(semantic.LINT_ROWS)
    assert chain["agent_evidence"]["result_step"] == "agent-evidence"


def test_semantic_run_binds_every_artifact_to_one_declared_step_or_replay() -> None:
    schema = json.loads(
        (ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    run = json.loads((HERE / "semantic-chain-run.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(run)) == []
    chain = json.loads((HERE / "semantic-chain.json").read_text(encoding="utf-8"))
    assert run["chain_manifest_sha256"] == common.sha256_file(HERE / "semantic-chain.json")
    steps = {item["id"]: set(item["produces"]) for item in chain["steps"]}
    paths: set[str] = set()
    digests: set[str] = set()
    for artifact in run["artifacts"]:
        path = ROOT / artifact["repository_path"]
        assert path.is_file()
        assert artifact["bytes"] == path.stat().st_size
        assert artifact["sha256"] == common.sha256_file(path)
        assert artifact["repository_path"] not in paths
        assert artifact["sha256"] not in digests
        paths.add(artifact["repository_path"])
        digests.add(artifact["sha256"])
        if artifact["role"] in {"negative-replay", "tamper-replay"}:
            assert artifact["source_step"] is None
            assert artifact["source_output"] is None
            assert artifact["replay_id"]
        else:
            assert artifact["source_output"] in steps[artifact["source_step"]]
            assert artifact["replay_id"] is None
