from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from openada.discovery import DiscoveryManager
from openada.engines.simra_artifact import (
    SimraArtifactError,
    derive_single_analysis_decks,
    load_model_prelude,
    load_simra_testbench,
)
from openada.operations.testbench_simulate import (
    TESTBENCH_EVIDENCE_ASSERTION,
    TESTBENCH_SIMULATE_PROFILE,
    simulate_testbench,
    resolve_testbench_driver,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "conformance" / "testbench-simulate-v0alpha1" / "fixtures"
MODEL_FREE = FIXTURES / "model-free-differential"
NMOS_SOURCE = FIXTURES / "nmos-common-source"
PROFILE_PATH = ROOT / "profiles" / "testbench.simulate-v1alpha1.json"
RESULT_SCHEMA_PATH = ROOT / "schemas" / "result-v0alpha1.schema.json"
OPERATION_PROFILE_SCHEMA_PATH = (
    ROOT / "schemas" / "operation-profile-v0alpha2.schema.json"
)

PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
RESULT_VALIDATOR = Draft202012Validator(
    json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
)
DATA_VALIDATOR = Draft202012Validator(PROFILE["normalized_result"]["data_schema"])

NGSPICE = shutil.which("ngspice")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_contract(payload: dict) -> None:
    assert sorted(payload) == [
        "artifacts",
        "data",
        "diagnostics",
        "engineering",
        "execution",
        "inputs",
        "operation",
        "provenance",
        "schema",
        "tool",
    ]
    assert not list(RESULT_VALIDATOR.iter_errors(payload))
    assert not list(DATA_VALIDATOR.iter_errors(payload["data"]))


def _published(tmp_path: Path, source: Path) -> Path:
    """Copy one published artifact so a test may mutate it without side effects."""

    destination = tmp_path / source.name
    shutil.copytree(source, destination)
    return destination / "schematic.artifact.json"


def _republish(descriptor: Path, **validation: object) -> None:
    """Rewrite one descriptor, keeping its published digests consistent."""

    document = json.loads(descriptor.read_text(encoding="utf-8"))
    document["validation"].update(validation)
    document["hashes"]["netlist_sha256"] = _digest(
        descriptor.parent / document["netlist"]
    )
    document["hashes"]["view_sha256"] = _digest(descriptor.parent / document["view"])
    descriptor.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# The published fixtures are real compiler output, not hand-written stand-ins.
# --------------------------------------------------------------------------


def test_published_fixtures_carry_the_digests_they_declare() -> None:
    for directory in (MODEL_FREE, NMOS_SOURCE):
        document = json.loads(
            (directory / "schematic.artifact.json").read_text(encoding="utf-8")
        )
        assert document["schema"] == "simra.schematic-artifact/v2"
        assert document["hashes"]["netlist_sha256"] == _digest(
            directory / document["netlist"]
        )
        assert document["hashes"]["view_sha256"] == _digest(
            directory / document["view"]
        )


def test_model_free_fixture_is_the_split_required_case() -> None:
    testbench = load_simra_testbench(MODEL_FREE / "schematic.artifact.json")
    assert testbench.simulation_handoff == "split_required"
    assert testbench.dispatch_mode == "split"
    assert [analysis["kind"] for analysis in testbench.analyses] == ["op", "tran"]
    assert testbench.saved_nets == ("D", "DB", "OUTP", "OUTN")
    assert testbench.parameters_state == "resolved"
    # A model-free deck runs with no external collateral.
    assert testbench.self_contained is True


def test_model_naming_fixture_is_resolved_but_not_self_contained() -> None:
    """A MOS testbench is fully bound yet still needs collateral Simra omits."""

    testbench = load_simra_testbench(NMOS_SOURCE / "schematic.artifact.json")
    assert testbench.parameters_state == "resolved"
    assert testbench.self_contained is False
    assert [analysis["kind"] for analysis in testbench.analyses] == ["op", "dc"]


# --------------------------------------------------------------------------
# Deck derivation
# --------------------------------------------------------------------------


def test_derivation_emits_one_deck_per_declared_analysis() -> None:
    testbench = load_simra_testbench(MODEL_FREE / "schematic.artifact.json")
    decks = derive_single_analysis_decks(testbench)

    assert [deck.kind for deck in decks] == ["op", "tran"]
    for deck in decks:
        lines = deck.text.splitlines()
        cards = [line for line in lines if line.upper().startswith((".OP", ".TRAN"))]
        assert len(cards) == 1
        assert cards[0].split()[0].upper() == f".{deck.kind.upper()}"
        # Everything that is not an analysis card survives byte-for-byte.
        assert ".SUBCKT MODEL_FREE_DIFF_DRIVER_V2 D DB OUTP OUTN VSS" in deck.text
        assert ".SAVE D DB OUTP OUTN" in deck.text
        assert lines[-1] == ".END"
        assert deck.sha256 == hashlib.sha256(deck.text.encode("utf-8")).hexdigest()

    assert decks[0].sha256 != decks[1].sha256


def test_derivation_preserves_the_declared_transient_parameters() -> None:
    testbench = load_simra_testbench(MODEL_FREE / "schematic.artifact.json")
    transient = derive_single_analysis_decks(testbench)[1]
    assert transient.analysis == {"kind": "tran", "step": "100p", "stop": "20n"}
    assert ".TRAN 100p 20n" in transient.text


def test_derivation_refuses_a_deck_that_contradicts_its_declaration(
    tmp_path: Path,
) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    deck = descriptor.parent / "design.spice"
    deck.write_text(
        deck.read_text(encoding="utf-8").replace(".TRAN 100p 20n\n", ""),
        encoding="utf-8",
    )
    _republish(descriptor)

    testbench = load_simra_testbench(descriptor)
    with pytest.raises(SimraArtifactError) as excinfo:
        derive_single_analysis_decks(testbench)
    assert excinfo.value.code == "testbench.deck.mismatch"


def test_model_prelude_is_composed_after_the_title_line() -> None:
    testbench = load_simra_testbench(NMOS_SOURCE / "schematic.artifact.json")
    prelude, record = load_model_prelude(NMOS_SOURCE / "nmos_lv.models")
    assert record["sha256"] == _digest(NMOS_SOURCE / "nmos_lv.models")

    decks = derive_single_analysis_decks(testbench, model_prelude=prelude)
    for deck in decks:
        lines = deck.text.splitlines()
        # The SPICE title line must remain first or the simulator eats a card.
        assert lines[0].startswith("* SIMRA")
        assert any(line.startswith(".MODEL nmos_lv") for line in lines[1:6])
        assert lines[-1] == ".END"


def test_model_prelude_refuses_an_include_chain(tmp_path: Path) -> None:
    models = tmp_path / "pdk.spice"
    models.write_text(
        "* vendor entry point\n.lib /pdk/sky130.lib.spice tt\n", encoding="utf-8"
    )
    with pytest.raises(SimraArtifactError) as excinfo:
        load_model_prelude(models)
    assert excinfo.value.code == "configuration.models.not_self_contained"


# --------------------------------------------------------------------------
# Artifact binding refusals
# --------------------------------------------------------------------------


def test_digest_mismatch_is_refused(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    deck = descriptor.parent / "design.spice"
    deck.write_text(
        deck.read_text(encoding="utf-8").replace("R_LOAD OUTP OUTN 100", "R_LOAD OUTP OUTN 101"),
        encoding="utf-8",
    )
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.artifact.digest_mismatch"


def test_unresolved_placeholder_is_refused_without_binding(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, NMOS_SOURCE)
    deck = descriptor.parent / "design.spice"
    deck.write_text(
        deck.read_text(encoding="utf-8").replace(
            "NF=1", "NF={SIMRA_UNRESOLVED_M_1_NF}"
        ),
        encoding="utf-8",
    )
    _republish(descriptor)

    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.parameters.unresolved"
    assert "SIMRA_UNRESOLVED_M_1_NF" in excinfo.value.message


def test_partial_parameter_state_is_refused_before_readiness(tmp_path: Path) -> None:
    """The actionable parameter state must win over the derived readiness flag."""

    descriptor = _published(tmp_path, MODEL_FREE)
    _republish(descriptor, parameters="partial", simulation_ready=False)
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.parameters.unresolved"


def test_a_non_testbench_artifact_is_refused(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    document = json.loads(descriptor.read_text(encoding="utf-8"))
    document["kind"] = "schematic"
    descriptor.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.artifact.not_a_testbench"


def test_a_traversing_netlist_reference_is_refused(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    document = json.loads(descriptor.read_text(encoding="utf-8"))
    document["netlist"] = "../design.spice"
    descriptor.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.artifact.invalid"


def test_a_handoff_disagreeing_with_the_declaration_is_refused(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    _republish(descriptor, simulation_handoff="direct")
    with pytest.raises(SimraArtifactError) as excinfo:
        load_simra_testbench(descriptor)
    assert excinfo.value.code == "testbench.handoff.inconsistent"


# --------------------------------------------------------------------------
# Operation-level refusals return a contract-valid envelope, never an exception.
# --------------------------------------------------------------------------


def test_missing_artifact_returns_an_unknown_envelope(tmp_path: Path) -> None:
    payload = simulate_testbench(
        tmp_path / "absent" / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["protocol"]["operation_profile"] == TESTBENCH_SIMULATE_PROFILE
    assert payload["data"]["protocol"]["assertion_profile"] == TESTBENCH_EVIDENCE_ASSERTION
    # No backend resolved, so no implementation identity may be claimed.
    assert payload["data"]["protocol"]["implementation_id"] == (
        resolve_testbench_driver("ngspice").driver_id
    )
    assert [item["code"] for item in payload["diagnostics"]] == [
        "testbench.artifact.missing"
    ]
    assert not (tmp_path / "out").exists()


def test_model_naming_artifact_without_collateral_is_refused(tmp_path: Path) -> None:
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert [item["code"] for item in payload["diagnostics"]] == [
        "testbench.models.required"
    ]
    # Nothing was launched, so nothing may be claimed.
    assert payload["data"]["dispatch"]["dispatched_analysis_count"] == 0


def test_an_unsupported_backend_is_refused(tmp_path: Path) -> None:
    payload = simulate_testbench(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        backend="spectre",
    )
    _assert_contract(payload)
    assert [item["code"] for item in payload["diagnostics"]] == [
        "testbench.backend.unsupported"
    ]


def test_a_noncanonical_request_id_is_refused(tmp_path: Path) -> None:
    payload = simulate_testbench(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        request_id="NOT-A-UUID",
    )
    _assert_contract(payload)
    assert [item["code"] for item in payload["diagnostics"]] == [
        "testbench.request.invalid"
    ]


def test_xyce_refuses_a_testbench_declaring_an_operating_point(tmp_path: Path) -> None:
    """The Xyce capability advertises no OP feature, so the split is refused early."""

    payload = simulate_testbench(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        backend="xyce",
    )
    _assert_contract(payload)
    assert [item["code"] for item in payload["diagnostics"]] == [
        "testbench.analysis.unsupported"
    ]


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def test_profile_validates_and_declares_immutable_identities() -> None:
    schema = json.loads(OPERATION_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(PROFILE))
    Draft202012Validator.check_schema(PROFILE["request"]["parameters_schema"])
    Draft202012Validator.check_schema(PROFILE["normalized_result"]["data_schema"])

    assert PROFILE["operation"]["id"] == TESTBENCH_SIMULATE_PROFILE
    assert PROFILE["assertion"]["id"] == TESTBENCH_EVIDENCE_ASSERTION
    assert PROFILE["operation"]["side_effect_mode"] == "evidence-only"
    assert PROFILE["operation"]["locator_types"] == ["filesystem"]
    assert PROFILE["normalized_result"]["result_schema"] == "openada.result/v0alpha1"
    assert [mapping["driver_id"] for mapping in PROFILE["native_mappings"]] == [
        resolve_testbench_driver("ngspice").driver_id,
        resolve_testbench_driver("xyce").driver_id,
    ]


def test_profile_separates_process_completion_from_engineering_truth() -> None:
    truth = PROFILE["assertion"]["truth_table"]
    assert truth["pass"]["allowed_execution_statuses"] == ["completed"]
    assert truth["fail"]["allowed_execution_statuses"] == ["completed"]
    assert "invalid_request" in truth["unknown"]["allowed_execution_statuses"]


def test_profile_is_packaged_for_installation() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"profiles/testbench.simulate-v1alpha1.json",' in pyproject


def test_profile_declares_every_diagnostic_the_driver_can_emit(tmp_path: Path) -> None:
    declared = {item["code"] for item in PROFILE["diagnostics"]}
    # Codes reached by the refusal tests above must be documented in the profile.
    for code in (
        "testbench.parameters.unresolved",
        "testbench.models.required",
        "testbench.deck.mismatch",
        "testbench.artifact.digest_mismatch",
        "testbench.analysis.unsupported",
        "testbench.handoff.split",
        "configuration.models.not_self_contained",
    ):
        assert code in declared


# --------------------------------------------------------------------------
# Native end-to-end evidence
# --------------------------------------------------------------------------


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_split_required_artifact_runs_every_declared_analysis(tmp_path: Path) -> None:
    payload = simulate_testbench(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"

    dispatch = payload["data"]["dispatch"]
    assert dispatch["mode"] == "split"
    assert dispatch["simulation_handoff"] == "split_required"
    assert dispatch["declared_analysis_count"] == 2
    assert dispatch["dispatched_analysis_count"] == 2
    assert dispatch["completed_analysis_count"] == 2
    assert dispatch["passing_analysis_count"] == 2

    assert payload["data"]["artifact"]["digests_verified"] is True
    assert "testbench.handoff.split" in {
        item["code"] for item in payload["diagnostics"]
    }

    operating_point, transient = payload["data"]["analyses"]
    assert operating_point["kind"] == "op"
    assert operating_point["engineering_status"] == "pass"
    assert operating_point["analysis"]["point_count"] == 1
    assert operating_point["analysis"]["convergence"] == "converged"
    assert operating_point["analysis"]["finite_value_count"] == 4

    assert transient["kind"] == "tran"
    assert transient["engineering_status"] == "pass"
    assert transient["analysis"]["convergence"] == "converged"
    assert transient["analysis"]["point_count"] > 1
    assert transient["analysis"]["dependent_variable_count"] == 4

    # Every dispatched analysis kept its own deck and its own child envelope.
    for facts in (operating_point, transient):
        deck = Path(facts["deck_path"])
        assert deck.is_file()
        assert _digest(deck) == facts["deck_sha256"]
        child = json.loads(Path(facts["result_path"]).read_text(encoding="utf-8"))
        assert not list(RESULT_VALIDATOR.iter_errors(child))
        assert child["data"]["protocol"]["operation_profile"] == (
            "openada.operation/circuit.simulate/v1alpha2"
        )

    roles = {item["role"] for item in payload["artifacts"]}
    assert {
        "testbench.analysis.deck",
        "testbench.analysis.result",
        "simulation.log",
        "simulation.result",
    } <= roles


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_model_naming_artifact_runs_with_supplied_collateral(tmp_path: Path) -> None:
    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        models_file=NMOS_SOURCE / "nmos_lv.models",
    )
    _assert_contract(payload)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["artifact"]["self_contained"] is False

    configuration = payload["data"]["configuration"]
    assert len(configuration) == 1
    assert configuration[0]["role"] == "spice-model-library"
    assert configuration[0]["identity"] == "content-digest"
    assert configuration[0]["sha256"] == _digest(NMOS_SOURCE / "nmos_lv.models")

    operating_point, sweep = payload["data"]["analyses"]
    assert operating_point["kind"] == "op"
    assert operating_point["analysis"]["point_count"] == 1
    assert sweep["kind"] == "dc"
    # 0 V to 1.8 V in 50 mV steps is 37 inclusive sweep points.
    assert sweep["analysis"]["point_count"] == 37
    assert sweep["analysis"]["convergence"] == "converged"


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_cli_dispatches_a_published_artifact_and_exits_on_the_assertion(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "openada",
            "--compact",
            "testbench-simulate",
            str(MODEL_FREE / "schematic.artifact.json"),
            "--output-dir",
            str(tmp_path / "out"),
            "--backend",
            "ngspice",
            "--request-id",
            "3f2c1a6e-7b4d-4c8a-9e15-2d6f0b7a4c31",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    _assert_contract(payload)
    assert completed.returncode == 0
    assert payload["operation"] == "testbench.simulate"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["protocol"]["request_id"] == (
        "3f2c1a6e-7b4d-4c8a-9e15-2d6f0b7a4c31"
    )
    assert payload["data"]["protocol"]["backend"] == "ngspice"


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_collateral_free_dispatch_of_a_model_naming_deck_stays_unknown(
    tmp_path: Path,
) -> None:
    """An undefined model must never be reported as an engineering failure."""

    descriptor = _published(tmp_path, NMOS_SOURCE)
    _republish(descriptor, simulation_ready=True)

    payload = simulate_testbench(
        descriptor,
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["dispatch"]["passing_analysis_count"] == 0
