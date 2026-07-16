from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "tools" / "verify_semantic_coverage.py"
CATALOG = ROOT / "catalog" / "semantic-surfaces-v0alpha1.json"
CHAIN_SCHEMA = ROOT / "schemas" / "semantic-chain-manifest-v0alpha1.schema.json"
RUN_SCHEMA = ROOT / "schemas" / "semantic-chain-run-v0alpha1.schema.json"


def _verifier_module():
    specification = importlib.util.spec_from_file_location(
        "openada_semantic_coverage_verifier",
        VERIFY,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY), *arguments],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _write_catalog(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_audit_emits_the_complete_deterministic_release_matrix() -> None:
    first = _run("--compact")
    second = _run("--compact")

    assert first.returncode == 0, first.stderr
    assert first.stderr == ""
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "pass"
    assert payload["issues"] == []
    assert payload["inventory"] == {
        "active_profile_count": 9,
        "builtin_provider_mapping_count": 10,
        "cli_leaf_count": 20,
        "preflight_assertion_count": 8,
        "profile_count": 10,
        "profile_feature_count": 30,
        "provider_mapping_count": 11,
        "shipped_provider_capability_count": 1,
        "shipped_provider_manifest_count": 1,
        "surface_count": 20,
    }
    assert payload["summary"]["row_count"] == 155
    assert payload["summary"]["active_row_count"] == 147
    assert payload["summary"]["gap_count"] == 0
    assert payload["summary"]["rows_by_coverage_level"] == {
        "agent-ready": 147,
        "unverified": 8,
    }
    assert payload["gaps"] == []


def test_maturity_and_release_evidence_remain_distinct() -> None:
    completed = _run("--compact")
    payload = json.loads(completed.stdout)
    rows = {row["row_id"]: row for row in payload["rows"]}

    spectral = rows[
        "provider|org.openada.kernel.spectral-evidence|"
        "openada.operation/result.spectral.measure/v1alpha1|"
        "openada.feature/spectral.snr/v1alpha1"
    ]
    transient = rows[
        "provider|org.openada.driver.ngspice|"
        "openada.operation/circuit.simulate/v1alpha2|"
        "openada.feature/simulation.analysis.tran/v1alpha1"
    ]
    shipped = rows[
        "repository-provider|org.openada.driver.ngspice-pdk-control|"
        "openada.operation/circuit.simulate/v1alpha2|"
        "openada.assertion/simulation.evidence.valid/v1alpha1|"
        "openada.feature/simulation.analysis.tran/v1alpha1"
    ]
    claim = rows[
        "provider-conformance|org.openada.driver.ngspice-pdk-control|"
        "org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1"
    ]
    assert spectral["implementation_maturity"] == "structured"
    assert transient["implementation_maturity"] == "workflow-validated"
    assert shipped["implementation_maturity"] == "workflow-validated"
    for row in (spectral, transient, shipped, claim):
        assert row["coverage_level"] == "agent-ready"
        assert row["required_coverage_level"] == "agent-ready"
        assert row["gap"] is False
        assert row["missing_evidence"] == []
        assert row["coverage_record_ids"]
    assert spectral["coverage_record_ids"] == [
        "openada.chain/ihp-analog-measurements/v1"
    ]
    assert transient["coverage_record_ids"] == [
        "openada.chain/ihp-inverter-agent-chain/v1"
    ]
    assert set(shipped["coverage_record_ids"]) == {
        "openada.chain/ihp-analog-measurements/v1",
        "openada.chain/ihp-inverter-agent-chain/v1",
        "openada.chain/ihp-ngspice-provider-analyses/v1",
    }
    assert claim["implementation_maturity"] == "workflow-validated"
    assert claim["coverage_record_ids"] == [
        "openada.chain/ihp-ngspice-provider-analyses/v1"
    ]
    assert claim["conformance_resolution"]["status"] == "resolved"
    assert claim["conformance_resolution"]["conformance_record_id"] == (
        "org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1"
    )
    assert claim["conformance_resolution"]["claimed_evidence_uri"].endswith(
        "/conformance/ihp-ngspice-provider-analyses"
    )
    assert claim["conformance_resolution"]["claimed_evidence_sha256"] == (
        claim["conformance_resolution"]["registered_evidence_sha256"]
    )
    assert claim["conformance_resolution"]["registered_chain_id"] == (
        "openada.chain/ihp-ngspice-provider-analyses/v1"
    )
    assert shipped["conformance_resolution"] is None


def test_historical_profile_rows_are_visible_but_not_release_obligations() -> None:
    payload = json.loads(_run("--compact").stdout)
    historical = [
        row
        for row in payload["rows"]
        if row["operation_profile"] == "openada.operation/circuit.simulate/v1alpha1"
    ]
    assert len(historical) == 8
    assert all(row["lifecycle"] == "historical" for row in historical)
    assert all(row["required_coverage_level"] is None for row in historical)
    assert all(row["gap"] is False for row in historical)


def test_enforcement_modes_pass_only_after_every_active_row_is_agent_ready() -> None:
    for arguments in (
        ("--fail-on-gaps", "--compact"),
        ("--mode", "agent-ready", "--compact"),
        ("--mode", "release", "--compact"),
    ):
        completed = _run(*arguments)
        assert completed.returncode == 0
        assert completed.stderr == ""
        payload = json.loads(completed.stdout)
        assert payload["status"] == "pass"
        assert payload["issues"] == []
        assert payload["summary"]["active_row_count"] == 147
        assert payload["summary"]["gap_count"] == 0


def test_uncataloged_cli_leaf_is_an_inventory_error(tmp_path: Path) -> None:
    catalog = _catalog()
    catalog["surfaces"] = [
        surface
        for surface in catalog["surfaces"]
        if surface["command_path"] != ["spectral"]
    ]
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "invalid"
    assert "CLI leaf is not classified: spectral" in payload["issues"]


def test_provider_feature_drift_is_an_inventory_error(tmp_path: Path) -> None:
    catalog = _catalog()
    spectral = next(
        record
        for record in catalog["provider_mappings"]
        if record["provider_id"] == "org.openada.kernel.spectral-evidence"
    )
    spectral["features"][0]["implementation_maturity"] = "workflow-validated"
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "invalid"
    assert any(
        issue.startswith("provider mapping org.openada.kernel.spectral-evidence|")
        and "feature metadata differs" in issue
        for issue in payload["issues"]
    )


def test_uncataloged_shipped_provider_manifest_is_an_inventory_error(tmp_path: Path) -> None:
    catalog = _catalog()
    catalog["provider_manifests"] = []
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "invalid"
    assert (
        "shipped provider manifest is not cataloged: "
        "providers/ngspice-pdk-control/driver-manifest.json"
    ) in payload["issues"]


def test_catalog_schema_is_closed(tmp_path: Path) -> None:
    catalog = _catalog()
    catalog["coverage_waivers"] = []
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert any("coverage_waivers" in issue for issue in payload["issues"])


def test_release_policy_cannot_remove_every_active_obligation(tmp_path: Path) -> None:
    catalog = _catalog()
    catalog["policy"]["active_lifecycles"] = ["experimental-hidden"]
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--mode", "release", "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "invalid"
    assert any("active_lifecycles" in issue for issue in payload["issues"])


def test_catalog_cannot_downgrade_semantic_cli_to_administrative(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    simulate = next(
        surface
        for surface in catalog["surfaces"]
        if surface["command_path"] == ["simulate"]
    )
    simulate["classification"] = "administrative"
    path = _write_catalog(tmp_path, catalog)

    completed = _run("--catalog", str(path), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert any(
        "catalog command simulate classification differs from implementation" in issue
        for issue in payload["issues"]
    )


def test_chain_index_is_closed_and_cannot_silently_waive_rows(tmp_path: Path) -> None:
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "schema": "openada.semantic-chain-index/v0alpha1",
                "records": [],
                "waivers": ["surface|openada.surface/cli.spectral/v1"],
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )

    completed = _run("--chain-index", str(index), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["issues"] == [
        "semantic chain index keys must be exactly schema, records, extensions"
    ]


def test_chain_index_record_must_declare_provider_conformance_links(tmp_path: Path) -> None:
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "schema": "openada.semantic-chain-index/v0alpha1",
                "records": [
                    {
                        "id": "openada.chain/example/v1",
                        "manifest": {},
                        "run": {},
                        "extensions": {},
                    }
                ],
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )

    completed = _run("--chain-index", str(index), "--compact")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["issues"] == ["semantic chain index record 0 has invalid keys"]


def test_matching_registered_digest_is_required_but_never_promotes_coverage() -> None:
    verifier = _verifier_module()
    row = verifier._base_row(
        "provider-conformance|driver|record",
        "provider-conformance-claim",
        lifecycle="active",
        classification="semantic-execution",
        policy={
            "active_lifecycles": ["active"],
            "required_levels": {"semantic-execution": "agent-ready"},
        },
        conformance_record_ids=["record"],
        conformance_claim_digest="a" * 64,
    )
    verifier._apply_coverage([row], {})

    verifier._resolve_provider_conformance_claims(
        [row],
        {
            "record": {
                "chain_record_id": "chain-index-record",
                "chain_id": "openada.chain/example/v1",
                "evidence_sha256": "a" * 64,
            }
        },
    )

    assert row["conformance_resolution"]["status"] == "resolved"
    assert row["coverage_level"] == "unverified"
    assert row["gap"] is True
    assert "registered-conformance-digest" not in row["missing_evidence"]

    row["conformance_claim_digest"] = "b" * 64
    verifier._resolve_provider_conformance_claims(
        [row],
        {
            "record": {
                "chain_record_id": "chain-index-record",
                "chain_id": "openada.chain/example/v1",
                "evidence_sha256": "a" * 64,
            }
        },
    )

    assert row["conformance_resolution"]["status"] == "digest-mismatch"
    assert row["missing_evidence"][-1] == "registered-conformance-digest"


def test_semantic_subject_binds_provider_semantics_but_detaches_run_claim(
    tmp_path: Path,
) -> None:
    verifier = _verifier_module()
    for directory in (
        "src/openada",
        "profiles",
        "schemas",
        "providers/reference",
        "bin",
    ):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/openada/provider.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "profiles/profile.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "schemas/schema.json").write_text("{}\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")

    manifest_path = tmp_path / "providers/reference/driver-manifest.json"
    original_manifest = json.loads(
        (ROOT / "providers/ngspice-pdk-control/driver-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    config_schema_path = tmp_path / "providers/reference/provider-config.schema.json"
    original_config_schema = (
        ROOT / "providers/ngspice-pdk-control/provider-config-v0alpha1.schema.json"
    ).read_text(encoding="utf-8")
    config_schema_path.write_text(original_config_schema, encoding="utf-8")
    launcher_path = tmp_path / "bin/openada-provider-reference"
    original_launcher = (ROOT / "bin/openada-provider-ngspice").read_text(
        encoding="utf-8"
    )
    launcher_path.write_text(original_launcher, encoding="utf-8")
    launcher_path.chmod(0o755)

    baseline = verifier._semantic_subject(catalog, root=tmp_path)

    detached = json.loads(json.dumps(original_manifest))
    detached["conformance_records"][0]["evidence"]["sha256"] = "a" * 64
    manifest_path.write_text(json.dumps(detached), encoding="utf-8")
    assert verifier._semantic_subject(catalog, root=tmp_path) == baseline

    semantic_mutations = []
    changed_version = json.loads(json.dumps(original_manifest))
    changed_version["driver"]["version"] = "0.4.1"
    semantic_mutations.append(changed_version)
    changed_transport = json.loads(json.dumps(original_manifest))
    changed_transport["transports"][0]["argv"] = ["different-provider"]
    semantic_mutations.append(changed_transport)
    changed_capability = json.loads(json.dumps(original_manifest))
    changed_capability["capabilities"][0]["features"] = []
    semantic_mutations.append(changed_capability)
    for changed in semantic_mutations:
        manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        assert verifier._semantic_subject(catalog, root=tmp_path) != baseline

    manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    config_schema_path.write_text(original_config_schema + "\n", encoding="utf-8")
    assert verifier._semantic_subject(catalog, root=tmp_path) != baseline
    config_schema_path.write_text(original_config_schema, encoding="utf-8")

    launcher_path.write_text(original_launcher + "\n", encoding="utf-8")
    assert verifier._semantic_subject(catalog, root=tmp_path) != baseline


def test_agent_ready_chain_schema_requires_the_full_declared_chain() -> None:
    schema = json.loads(CHAIN_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    incomplete = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema": "openada.semantic-chain/v0alpha1",
        "id": "openada.chain/incomplete/v1",
        "extensions": {},
    }

    missing = {
        part
        for error in validator.iter_errors(incomplete)
        for part in (
            "design",
            "runtime",
            "contracts",
            "covers",
            "steps",
            "negative_replays",
            "tamper_replays",
            "agent_evidence",
        )
        if part in error.message
    }
    assert missing == {
        "design",
        "runtime",
        "contracts",
        "covers",
        "steps",
        "negative_replays",
        "tamper_replays",
        "agent_evidence",
    }


def test_semantic_coverage_schemas_are_valid_draft_2020_12() -> None:
    for path in (
        ROOT / "schemas" / "semantic-surface-catalog-v0alpha1.schema.json",
        CHAIN_SCHEMA,
        RUN_SCHEMA,
    ):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_a_chain_cannot_claim_agent_ready_without_row_specific_replays() -> None:
    verifier = _verifier_module()
    row_id = "feature|profile|target"
    manifest = {
        "negative_replays": [
            {"covers": ["feature|profile|different"]},
        ],
        "tamper_replays": [
            {"covers": [row_id]},
        ],
    }
    run = {
        "checks": {
            "contract_test": True,
            "pinned_real_design": True,
            "native_run": True,
            "independent_artifact_check": True,
            "normalized_evidence": True,
            "downstream_decision": True,
            "negative_replay": True,
            "tamper_replay": True,
            "agent_visible_evidence": True,
        }
    }

    evidence = verifier._record_evidence(manifest, run, row_id)

    assert "negative-replay" not in evidence
    assert verifier._level_for_evidence(evidence) == "workflow-validated"


def test_manifest_cannot_inject_a_row_absent_from_positive_step_coverage() -> None:
    verifier = _verifier_module()
    exercised = "surface|openada.surface/cli.profile-list/v1"
    injected = "surface|openada.surface/cli.provider-list/v1"
    rows = {
        exercised: {
            "row_id": exercised,
            "classification": "administrative",
            "provider_id": None,
            "native_mapping": None,
        },
        injected: {
            "row_id": injected,
            "classification": "administrative",
            "provider_id": None,
            "native_mapping": None,
        },
    }
    manifest = {
        "covers": [exercised, injected],
        "steps": [
            {
                "id": "profile-list",
                "kind": "semantic-command",
                "native_execution": False,
                "covers": [exercised],
            }
        ],
    }

    assert verifier._positive_coverage_issues(
        manifest, rows, label="test chain"
    ) == [
        "test chain manifest.covers rows lack a positive semantic-command step: "
        + injected
    ]


def test_provider_row_requires_a_covering_native_semantic_step() -> None:
    verifier = _verifier_module()
    row_id = "provider|org.example.driver|operation|feature"
    rows = {
        row_id: {
            "row_id": row_id,
            "classification": "semantic-execution",
            "provider_id": "org.example.driver",
            "provider_kind": "eda-driver",
            "native_mapping": None,
        }
    }
    manifest = {
        "covers": [row_id],
        "steps": [
            {
                "id": "normalized-only",
                "kind": "semantic-command",
                "native_execution": False,
                "covers": [row_id],
            }
        ],
    }

    assert verifier._positive_coverage_issues(
        manifest, rows, label="test chain"
    ) == [
        "test chain native EDA row lacks a covering native "
        f"semantic-command step: {row_id}"
    ]

    manifest["steps"][0]["native_execution"] = True
    assert verifier._positive_coverage_issues(manifest, rows, label="test chain") == []


def test_evidence_kernel_row_keeps_the_nonnative_evidence_boundary() -> None:
    verifier = _verifier_module()
    row_id = "provider|org.example.kernel|operation|feature"
    rows = {
        row_id: {
            "row_id": row_id,
            "classification": "semantic-execution",
            "provider_id": "org.example.kernel",
            "provider_kind": "evidence-kernel",
            "native_mapping": "org.example.kernel|org.openada.core.runtime",
        }
    }
    manifest = {
        "covers": [row_id],
        "steps": [
            {
                "id": "simulate",
                "kind": "semantic-command",
                "native_execution": True,
                "covers": [],
                "consumes": [],
                "produces": ["native-series"],
            },
            {
                "id": "measure-native-evidence",
                "kind": "semantic-command",
                "native_execution": False,
                "covers": [row_id],
                "consumes": ["native-series"],
                "produces": ["measurement"],
            }
        ],
    }

    assert verifier._positive_coverage_issues(manifest, rows, label="test chain") == []

    manifest["steps"][1]["consumes"] = []
    assert verifier._positive_coverage_issues(
        manifest, rows, label="test chain"
    ) == [
        "test chain artifact-kernel row lacks a covering nonnative step "
        "transitively consuming native evidence: " + row_id
    ]


def test_active_native_mapping_must_name_its_exposed_operation_provider() -> None:
    verifier = _verifier_module()
    operation = "openada.operation/example/v1"
    issues: list[str] = []

    verifier._validate_native_mapping_providers(
        {
            operation: {
                "lifecycle": "active",
                "native_mappings": [
                    {
                        "driver_id": "org.example.unexposed-kernel",
                        "native_product_id": "org.example.format",
                    }
                ],
            }
        },
        [
            {
                "provider_id": "org.example.exposed-kernel",
                "operation_profile": operation,
            }
        ],
        [],
        issues,
    )

    assert issues == [
        f"active native mapping {operation}|org.example.unexposed-kernel does not "
        "name an exposed provider for that operation"
    ]


def test_provider_claim_chain_cannot_hash_its_own_claim_manifest() -> None:
    verifier = _verifier_module()
    conformance_id = "org.example.conformance/provider/v1"
    manifest_path = "providers/example/driver-manifest.json"

    issues = verifier._provider_claim_contract_cycle_issues(
        {"contracts": [{"repository_path": manifest_path}]},
        [conformance_id],
        {conformance_id: manifest_path},
        label="test chain",
    )

    assert issues == [
        f"test chain directly hashes provider manifest {manifest_path!r} for "
        f"conformance record {conformance_id!r}, creating a "
        "manifest/run/claim digest cycle"
    ]


def test_passing_check_requires_a_present_content_addressed_artifact() -> None:
    verifier = _verifier_module()
    encoded = (ROOT / "README.md").read_bytes()
    artifact = {
        "repository_path": "README.md",
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "role": "contract-test",
        "source_step": "contract-step",
        "source_output": "contract-receipt",
        "replay_id": None,
    }
    checks = {
        "contract_test": True,
        "pinned_real_design": True,
        "native_run": True,
        "independent_artifact_check": False,
        "normalized_evidence": False,
        "downstream_decision": False,
        "negative_replay": False,
        "tamper_replay": False,
        "agent_visible_evidence": False,
    }
    run = {"checks": checks, "artifacts": [artifact]}
    manifest = {
        "design": {"class": "public-design"},
        "steps": [
            {
                "id": "contract-step",
                "kind": "source-materialize",
                "native_execution": False,
                "produces": ["contract-receipt"],
            }
        ],
        "negative_replays": [],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "contract-step"},
    }

    issues = verifier._run_artifact_issues(run, manifest, label="test run")

    assert issues == [
        "test run check pinned_real_design lacks a verified "
        "'design-provenance' artifact",
        "test run check native_run lacks a verified 'native-artifact' artifact",
    ]


def test_native_artifact_cannot_substitute_an_oracle_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    payload = b"native evidence\n"
    (tmp_path / "artifact.bin").write_bytes(payload)
    run = {
        "checks": {
            "contract_test": False,
            "pinned_real_design": False,
            "native_run": True,
            "independent_artifact_check": False,
            "normalized_evidence": False,
            "downstream_decision": False,
            "negative_replay": False,
            "tamper_replay": False,
            "agent_visible_evidence": False,
        },
        "artifacts": [
            {
                "repository_path": "artifact.bin",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "role": "native-artifact",
                "source_step": "oracle",
                "source_output": "oracle-verdict",
                "replay_id": None,
            }
        ],
    }
    manifest = {
        "design": {"class": "synthetic"},
        "steps": [
            {
                "id": "oracle",
                "kind": "independent-oracle",
                "native_execution": False,
                "produces": ["oracle-verdict"],
            }
        ],
        "negative_replays": [],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "oracle"},
    }

    issues = verifier._run_artifact_issues(run, manifest, label="test run")

    assert "test run artifact 0 native-artifact must come from a native semantic-command step" in issues
    assert "test run check native_run lacks a verified 'native-artifact' artifact" in issues


def test_decision_and_agent_artifacts_bind_their_distinct_dag_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    decision = b"semantic specification decision\n"
    agent = b"independently synthesized agent evidence\n"
    (tmp_path / "decision.json").write_bytes(decision)
    (tmp_path / "agent.json").write_bytes(agent)
    run = {
        "checks": {
            "contract_test": False,
            "pinned_real_design": False,
            "native_run": False,
            "independent_artifact_check": False,
            "normalized_evidence": False,
            "downstream_decision": True,
            "negative_replay": False,
            "tamper_replay": False,
            "agent_visible_evidence": True,
        },
        "artifacts": [
            {
                "repository_path": "decision.json",
                "bytes": len(decision),
                "sha256": hashlib.sha256(decision).hexdigest(),
                "role": "downstream-decision",
                "source_step": "evaluate",
                "source_output": "specification-verdict",
                "replay_id": None,
            },
            {
                "repository_path": "agent.json",
                "bytes": len(agent),
                "sha256": hashlib.sha256(agent).hexdigest(),
                "role": "agent-visible-evidence",
                "source_step": "agent-decision",
                "source_output": "agent-evidence",
                "replay_id": None,
            },
        ],
    }
    manifest = {
        "design": {"class": "synthetic"},
        "steps": [
            {
                "id": "evaluate",
                "kind": "semantic-command",
                "native_execution": False,
                "produces": ["specification-verdict"],
            },
            {
                "id": "agent-decision",
                "kind": "independent-decision",
                "native_execution": False,
                "produces": ["agent-evidence"],
            },
        ],
        "negative_replays": [],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "agent-decision"},
    }

    assert verifier._run_artifact_issues(run, manifest, label="test run") == []


def test_distinct_trust_roles_cannot_reuse_one_repository_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    payload = b"shared evidence\n"
    (tmp_path / "shared.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    run = {
        "checks": {
            "contract_test": False,
            "pinned_real_design": False,
            "native_run": True,
            "independent_artifact_check": True,
            "normalized_evidence": False,
            "downstream_decision": False,
            "negative_replay": False,
            "tamper_replay": False,
            "agent_visible_evidence": False,
        },
        "artifacts": [
            {
                "repository_path": "shared.bin",
                "bytes": len(payload),
                "sha256": digest,
                "role": "native-artifact",
                "source_step": "native",
                "source_output": "native-output",
                "replay_id": None,
            },
            {
                "repository_path": "shared.bin",
                "bytes": len(payload),
                "sha256": digest,
                "role": "independent-oracle",
                "source_step": "oracle",
                "source_output": "oracle-output",
                "replay_id": None,
            },
        ],
    }
    manifest = {
        "design": {"class": "synthetic"},
        "steps": [
            {
                "id": "native",
                "kind": "semantic-command",
                "native_execution": True,
                "produces": ["native-output"],
            },
            {
                "id": "oracle",
                "kind": "independent-oracle",
                "native_execution": False,
                "produces": ["oracle-output"],
            },
        ],
        "negative_replays": [],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "native"},
    }

    issues = verifier._run_artifact_issues(run, manifest, label="test run")

    assert "test run artifacts 0, 1 reuse repository path 'shared.bin'" in issues
    assert any("trust artifacts 0 (native-artifact), 1 (independent-oracle) reuse SHA-256" in issue for issue in issues)


def test_distinct_trust_roles_cannot_reuse_copied_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    payload = b"copied evidence\n"
    (tmp_path / "native.bin").write_bytes(payload)
    (tmp_path / "oracle.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    run = {
        "checks": {
            "contract_test": False,
            "pinned_real_design": False,
            "native_run": True,
            "independent_artifact_check": True,
            "normalized_evidence": False,
            "downstream_decision": False,
            "negative_replay": False,
            "tamper_replay": False,
            "agent_visible_evidence": False,
        },
        "artifacts": [
            {
                "repository_path": "native.bin",
                "bytes": len(payload),
                "sha256": digest,
                "role": "native-artifact",
                "source_step": "native",
                "source_output": "native-output",
                "replay_id": None,
            },
            {
                "repository_path": "oracle.bin",
                "bytes": len(payload),
                "sha256": digest,
                "role": "independent-oracle",
                "source_step": "oracle",
                "source_output": "oracle-output",
                "replay_id": None,
            },
        ],
    }
    manifest = {
        "design": {"class": "synthetic"},
        "steps": [
            {
                "id": "native",
                "kind": "semantic-command",
                "native_execution": True,
                "produces": ["native-output"],
            },
            {
                "id": "oracle",
                "kind": "independent-oracle",
                "native_execution": False,
                "produces": ["oracle-output"],
            },
        ],
        "negative_replays": [],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "native"},
    }

    issues = verifier._run_artifact_issues(run, manifest, label="test run")

    assert any("trust artifacts 0 (native-artifact), 1 (independent-oracle) reuse SHA-256" in issue for issue in issues)


def test_replay_evidence_is_required_once_for_each_declared_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    payload = b"first replay\n"
    (tmp_path / "replay-one.json").write_bytes(payload)
    run = {
        "checks": {
            "contract_test": False,
            "pinned_real_design": False,
            "native_run": False,
            "independent_artifact_check": False,
            "normalized_evidence": False,
            "downstream_decision": False,
            "negative_replay": True,
            "tamper_replay": False,
            "agent_visible_evidence": False,
        },
        "artifacts": [
            {
                "repository_path": "replay-one.json",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "role": "negative-replay",
                "source_step": None,
                "source_output": None,
                "replay_id": "replay-one",
            }
        ],
    }
    manifest = {
        "design": {"class": "synthetic"},
        "steps": [],
        "negative_replays": [{"id": "replay-one"}, {"id": "replay-two"}],
        "tamper_replays": [],
        "agent_evidence": {"result_step": "unused"},
    }

    issues = verifier._run_artifact_issues(run, manifest, label="test run")

    assert issues == [
        "test run check negative_replay requires exactly one verified "
        "'negative-replay' artifact for replay 'replay-two'; found 0"
    ]


def test_release_rejects_provisional_or_dirty_source_attestation(
    monkeypatch,
) -> None:
    verifier = _verifier_module()
    subject = "a" * 64
    tree = "b" * 40
    monkeypatch.setattr(
        verifier,
        "_semantic_subject_at_revision",
        lambda _catalog, _revision: (subject, tree),
    )
    run = {
        "semantic_subject_sha256": subject,
        "source_attestation": {
            "receipt_class": "provisional",
            "repository_revision": "c" * 40,
            "repository_tree": tree,
            "semantic_subject_sha256": subject,
            "clean_before": False,
            "clean_after": False,
            "state_unchanged": True,
            "extensions": {},
        },
    }

    issues = verifier._source_attestation_issues(
        run,
        subject,
        CATALOG,
        label="test receipt",
        mode="release",
    )

    assert "test receipt is provisional and cannot enter a release index" in issues
    assert (
        "test receipt release receipt was not replayed from unchanged clean source"
        in issues
    )


def test_agent_required_json_pointers_are_resolved_from_bound_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    (tmp_path / "agent.json").write_text(
        json.dumps({"decision": {"status": "proceed"}}),
        encoding="utf-8",
    )
    run = {
        "artifacts": [
            {
                "role": "agent-visible-evidence",
                "repository_path": "agent.json",
            }
        ]
    }
    manifest = {
        "agent_evidence": {
            "required_json_pointers": [
                "/decision/status",
                "/decision/evidence",
            ]
        }
    }

    assert verifier._agent_pointer_issues(
        run, manifest, label="test chain"
    ) == [
        "test chain agent-visible evidence lacks required JSON pointer "
        "'/decision/evidence'"
    ]


def test_offline_release_verifier_is_hash_bound_and_must_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    verifier = _verifier_module()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "SRC", tmp_path / "src")
    implementation = tmp_path / "verify.py"
    implementation.write_text("raise SystemExit(7)\n", encoding="utf-8")
    digest = hashlib.sha256(implementation.read_bytes()).hexdigest()
    reference = {
        "repository_path": "verify.py",
        "sha256": digest,
        "extensions": {},
    }
    manifest = {
        "release_verification": {
            "implementation": reference,
            "arguments": [],
            "timeout_seconds": 10,
            "extensions": {},
        },
        "steps": [
            {
                "kind": "independent-oracle",
                "implementation": reference,
            }
        ],
    }

    issues = verifier._release_verification_issues(
        {"artifacts": []},
        manifest,
        label="test chain",
        mode="release",
    )

    assert any("offline release verifier failed with exit 7" in issue for issue in issues)
