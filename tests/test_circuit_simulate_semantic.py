"""The one simulation semantic, exercised through ``circuit.simulate/v1alpha2``.

``testbench.simulate/v1alpha1`` was a second operation that meant "simulate".
Its semantics now live in :mod:`openada.operations.simulate`, so every case this
file used to state about "the testbench operation" is restated here against the
unified contract: one operation name, one profile, one result data schema, and
one envelope per request whatever shape the request arrived in.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from openada import conformance
from openada.cli import main
from openada.discovery import DiscoveryManager
from openada.driver_registry import (
    BUILTIN_DRIVERS,
    CIRCUIT_SIMULATE_PROFILE,
    SIMULATION_EVIDENCE_ASSERTION,
    TESTBENCH_SIMULATE_PROFILE,
)
from openada.engines.simra_artifact import (
    SimraArtifactError,
    derive_single_analysis_decks,
    load_model_prelude,
    load_simra_testbench,
)
from openada.operations.simulate import (
    DISPATCH_EXTENSION,
    OPERATION_NAME,
    PDK_BINDING_EXTENSION,
    TARGET_EXTENSION,
    simulate,
    simulate_legacy_native,
)
from openada.operations.testbench_simulate import (
    DEPRECATION_CODE,
    REMOVAL_NOT_BEFORE,
    REMOVAL_VERSION,
    deprecation_diagnostic,
    resolve_testbench_driver,
    simulate_testbench,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "conformance" / "testbench-simulate-v0alpha1" / "fixtures"
MODEL_FREE = FIXTURES / "model-free-differential"
NMOS_SOURCE = FIXTURES / "nmos-common-source"
#: Retired, but still published so an in-flight reader can resolve the old id.
RETIRED_PROFILE_PATH = ROOT / "profiles" / "testbench.simulate-v1alpha1.json"
#: The one simulation profile.
PROFILE_PATH = ROOT / "profiles" / "circuit.simulate-v1alpha2.json"
OPERATION_PROFILE_SCHEMA_PATH = (
    ROOT / "schemas" / "operation-profile-v0alpha2.schema.json"
)

RETIRED_PROFILE = json.loads(RETIRED_PROFILE_PATH.read_text(encoding="utf-8"))
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
#: ``request_id`` is ``format: uuid``, which only binds with a format checker.
DATA_VALIDATOR = Draft202012Validator(
    PROFILE["normalized_result"]["data_schema"], format_checker=FormatChecker()
)

NGSPICE = shutil.which("ngspice")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_contract(payload: dict) -> None:
    """Assert the one shape every simulation returns, whatever it was asked."""

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
    assert payload["operation"] == OPERATION_NAME == "simulate"
    assert conformance.result_conformance_issues(payload) == ()
    assert not list(DATA_VALIDATOR.iter_errors(payload["data"]))
    assert payload["data"]["protocol"]["operation_profile"] == CIRCUIT_SIMULATE_PROFILE
    assert payload["data"]["protocol"]["assertion_profile"] == (
        SIMULATION_EVIDENCE_ASSERTION
    )


def _codes(payload: dict) -> list[str]:
    return [item["code"] for item in payload["diagnostics"]]


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
    descriptor.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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
# Artifact binding refusals. These codes are raised by the artifact engine and
# are unchanged by the unification.
# --------------------------------------------------------------------------


def test_digest_mismatch_is_refused(tmp_path: Path) -> None:
    descriptor = _published(tmp_path, MODEL_FREE)
    deck = descriptor.parent / "design.spice"
    deck.write_text(
        deck.read_text(encoding="utf-8").replace(
            "R_LOAD OUTP OUTN 100", "R_LOAD OUTP OUTN 101"
        ),
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


def test_an_artifact_refusal_surfaces_as_one_contract_valid_envelope(
    tmp_path: Path,
) -> None:
    """The engine raises; the operation never does. A caller gets evidence."""

    descriptor = _published(tmp_path, MODEL_FREE)
    _republish(descriptor, simulation_handoff="direct")

    payload = simulate(descriptor, tmp_path / "out", discovery=DiscoveryManager())
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == ["testbench.handoff.inconsistent"]
    # The target was classified before it was rejected, so the refusal still
    # says what was asked about.
    assert payload["data"]["extensions"][TARGET_EXTENSION]["kind"] == "simra-artifact"


# --------------------------------------------------------------------------
# Operation-level refusals return a contract-valid envelope, never an exception.
# --------------------------------------------------------------------------


def test_missing_target_returns_an_unknown_envelope(tmp_path: Path) -> None:
    payload = simulate(
        tmp_path / "absent" / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == ["input.missing"]
    # A backend was named and resolved, so the driver identity is stated even
    # though nothing ran.
    assert payload["data"]["protocol"]["driver_id"] == (
        resolve_testbench_driver("ngspice").driver_id
    )
    # A refusal never creates an evidence directory it did not fill.
    assert not (tmp_path / "out").exists()


def test_model_naming_artifact_without_collateral_is_refused(tmp_path: Path) -> None:
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == ["simulation.models.required"]
    # Nothing was launched, so nothing may be claimed.
    assert payload["data"]["analysis"]["completion"] == "unproven"
    assert payload["data"]["evidence"]["provenance"] == "incomplete"
    assert DISPATCH_EXTENSION not in payload["data"]["extensions"]
    assert PDK_BINDING_EXTENSION not in payload["data"]["extensions"]
    target = payload["data"]["extensions"][TARGET_EXTENSION]
    assert target["kind"] == "simra-artifact"
    assert target["self_contained"] is False


def test_an_unsupported_backend_is_refused(tmp_path: Path) -> None:
    payload = simulate(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        backend="spectre",
    )
    _assert_contract(payload)
    assert _codes(payload) == ["simulation.backend.unsupported"]
    # No backend resolved, so no implementation identity may be claimed.
    assert payload["data"]["protocol"]["driver_id"] is None


def test_a_noncanonical_request_id_is_refused(tmp_path: Path) -> None:
    payload = simulate(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        request_id="NOT-A-UUID",
    )
    _assert_contract(payload)
    assert _codes(payload) == ["simulation.request.invalid"]


def test_xyce_refuses_a_testbench_declaring_an_operating_point(tmp_path: Path) -> None:
    """The Xyce capability advertises no OP feature, so the split is refused early."""

    payload = simulate(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        backend="xyce",
    )
    _assert_contract(payload)
    assert _codes(payload) == ["simulation.analysis.unsupported"]
    assert payload["data"]["protocol"]["driver_id"] == "org.openada.driver.xyce"


# --------------------------------------------------------------------------
# The deprecated alias is an alias, not a second semantic.
# --------------------------------------------------------------------------


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_the_deprecated_alias_returns_a_circuit_simulate_result(tmp_path: Path) -> None:
    payload = simulate_testbench(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["operation"] == "simulate"
    assert payload["data"]["protocol"]["operation_profile"] == CIRCUIT_SIMULATE_PROFILE
    assert payload["engineering"]["status"] == "pass"

    deprecations = [
        item for item in payload["diagnostics"] if item["code"] == DEPRECATION_CODE
    ]
    assert len(deprecations) == 1
    assert deprecations[0]["severity"] == "warning"
    assert CIRCUIT_SIMULATE_PROFILE in deprecations[0]["message"]


def test_the_deprecated_alias_carries_its_warning_through_a_refusal(
    tmp_path: Path,
) -> None:
    """A refusal is still one envelope, and it still names the unified form."""

    payload = simulate_testbench(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["data"]["protocol"]["operation_profile"] == CIRCUIT_SIMULATE_PROFILE
    assert _codes(payload) == ["simulation.models.required", DEPRECATION_CODE]
    assert [
        item["severity"]
        for item in payload["diagnostics"]
        if item["code"] == DEPRECATION_CODE
    ] == ["warning"]


def test_the_native_interface_refuses_a_published_artifact(tmp_path: Path) -> None:
    """Running an artifact as if it were a deck would simulate nothing at all.

    The native path cannot verify digests, split declared analyses or bind a
    PDK, so handing it a descriptor is a typed refusal that names the semantic
    form instead of a silent empty run.
    """

    payload = simulate_legacy_native(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _codes(payload) == ["simulation.target.artifact"]
    assert "openada simulate" in payload["diagnostics"][0]["hint"]


def test_the_alias_resolves_the_one_driver_identity_per_backend() -> None:
    assert resolve_testbench_driver("ngspice").driver_id == "org.openada.driver.ngspice"
    assert resolve_testbench_driver("xyce").driver_id == "org.openada.driver.xyce"
    assert resolve_testbench_driver("spectre") is None
    assert resolve_testbench_driver(None) is None


# --------------------------------------------------------------------------
# Profile documents
# --------------------------------------------------------------------------


def test_the_retired_profile_still_validates_and_is_marked_deprecated() -> None:
    """The document survives so an old identifier still resolves to something.

    What must not survive is any live binding: no driver advertises it, and the
    resolver hands back the one driver identity per backend instead.
    """

    schema = json.loads(OPERATION_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(RETIRED_PROFILE))
    Draft202012Validator.check_schema(RETIRED_PROFILE["request"]["parameters_schema"])
    Draft202012Validator.check_schema(
        RETIRED_PROFILE["normalized_result"]["data_schema"]
    )

    assert RETIRED_PROFILE["operation"]["id"] == TESTBENCH_SIMULATE_PROFILE
    assert RETIRED_PROFILE["status"] == "deprecated"
    assert RETIRED_PROFILE["operation"]["side_effect_mode"] == "evidence-only"
    assert RETIRED_PROFILE["normalized_result"]["result_schema"] == (
        "openada.result/v0alpha1"
    )

    assert not [
        driver
        for driver in BUILTIN_DRIVERS.values()
        if driver.operation_profile == TESTBENCH_SIMULATE_PROFILE
    ]
    assert resolve_testbench_driver("ngspice").driver_id == "org.openada.driver.ngspice"


def test_the_retired_alias_states_when_it_is_removed() -> None:
    """A deprecation with no stated end becomes permanent by default.

    The profile document, the module constants and the warning every alias call
    returns must all name the same removal version and date, so no reader of
    any one of them can conclude the alias is simply how things are now.
    """

    stated = RETIRED_PROFILE["extensions"]["org.openada"]["deprecation"]
    assert stated["superseded_by"] == CIRCUIT_SIMULATE_PROFILE
    assert stated["removal_version"] == REMOVAL_VERSION
    assert stated["removal_not_before"] == REMOVAL_NOT_BEFORE
    # A date, not just a version: a version alone can be deferred forever.
    assert date.fromisoformat(stated["removal_not_before"]) > date(2026, 7, 27)

    warning = deprecation_diagnostic()
    assert warning["severity"] == "warning"
    assert warning["code"] == DEPRECATION_CODE
    assert REMOVAL_VERSION in warning["message"]
    assert REMOVAL_NOT_BEFORE in warning["message"]


def test_the_alias_re_export_surface_is_only_its_entry_point() -> None:
    """The retired name may not offer a second spelling of anything live.

    Every re-export of the retired name is a fresh way for it to acquire a
    caller. ``openada.operations`` therefore publishes the alias entry point
    the CLI dispatches to and nothing else; the profile and assertion
    identifiers belong to ``driver_registry``.
    """

    import openada.operations as operations

    retired = {
        name
        for name in operations.__all__
        if "testbench" in name.lower() or "TESTBENCH" in name
    }
    assert retired == {"simulate_testbench"}
    assert set(operations.__all__) == {
        name for name in operations.__all__ if hasattr(operations, name)
    }


def test_profile_separates_process_completion_from_engineering_truth() -> None:
    """"The tool ran" and "the circuit passes" are never promoted into each other."""

    truth = PROFILE["assertion"]["truth_table"]
    allowed = {
        verdict: set(facts["allowed_execution_statuses"])
        for verdict, facts in truth.items()
    }
    # A pass may only ever be claimed for a process that completed.
    assert allowed["pass"] == {"completed"}
    # A request that never reached a simulator says nothing about the circuit.
    assert "invalid_request" not in allowed["pass"]
    assert "invalid_request" not in allowed["fail"]
    assert "invalid_request" in allowed["unknown"]
    # And a process that did complete does not by itself decide the verdict.
    assert {"pass", "fail", "unknown"} == set(allowed)
    assert all("completed" in statuses for statuses in allowed.values())


def test_profile_is_packaged_for_installation() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"profiles/testbench.simulate-v1alpha1.json",' in pyproject


#: Every error/warning code the unified simulation path can put in a result.
#: Sources: ``operations/simulate.py`` (request, dispatch and destination
#: refusals), ``operations/testbench_simulate.py`` (the alias warning),
#: ``pdk_collateral.py`` (hand-bound technology), ``pdk_bindings.py`` (binding
#: refusals) and ``engines/simra_artifact.py`` (artifact refusals).
UNIFIED_DIAGNOSTIC_CODES = frozenset(
    {
        "input.missing",
        "simulation.request.invalid",
        "simulation.backend.unsupported",
        "simulation.models.ambiguous",
        "simulation.models.required",
        "simulation.analysis.unsupported",
        "simulation.analyses.over_limit",
        "simulation.destination.unusable",
        "simulation.deck.unstable",
        "simulation.evidence.over_limit",
        "simulation.result.missing",
        "simulation.provenance.incomplete",
        "simulation.target.artifact",
        "simulation.collateral.unmanaged",
        "simulation.operation.deprecated",
        "pdk.collateral.foreign",
        "pdk.collateral.missing",
        "pdk.collateral.conflict",
        "pdk.collateral.hand_bound",
        "pdk.corner.unbound",
        "pdk.corner.unknown",
        "pdk.backend.unsupported",
        "pdk.root.required",
        "pdk.analog.unsupported",
        "testbench.artifact.invalid",
        "testbench.artifact.digest_mismatch",
        "testbench.artifact.not_a_testbench",
        "testbench.handoff.inconsistent",
        "testbench.parameters.unresolved",
        "testbench.deck.mismatch",
        "configuration.models.not_self_contained",
    }
)

#: The one code the unified path can emit that the published profile
#: deliberately does not declare.
#:
#: ``simulation.operation.deprecated`` is not a fact about circuit.simulate. It
#: is emitted by the retired ``testbench-simulate`` alias, which delegates here
#: and therefore carries the warning out inside a circuit.simulate envelope. It
#: is scheduled for removal with the alias
#: (:data:`openada.operations.testbench_simulate.REMOVAL_VERSION`), and a
#: published profile's diagnostics are part of an identifier the compatibility
#: policy treats as immutable: declaring a code that is meant to disappear is
#: exactly how a retired semantic becomes permanent. It leaves this list by
#: being deleted along with the alias, not by being declared.
PENDING_PROFILE_DIAGNOSTICS = frozenset({"simulation.operation.deprecated"})


def test_profile_declares_every_diagnostic_the_unified_path_can_emit() -> None:
    declared = {item["code"] for item in PROFILE["diagnostics"]}

    undeclared = sorted(UNIFIED_DIAGNOSTIC_CODES - declared)
    assert undeclared == sorted(PENDING_PROFILE_DIAGNOSTICS), (
        "profiles/circuit.simulate-v1alpha2.json diagnostics[] and the codes the "
        "unified path emits have drifted apart"
    )

    # The pending list may only ever shrink: a code the profile already declares
    # must never be carried here as an excuse.
    assert not (PENDING_PROFILE_DIAGNOSTICS & declared)
    assert PENDING_PROFILE_DIAGNOSTICS <= UNIFIED_DIAGNOSTIC_CODES


def test_the_codes_the_refusal_tests_reach_are_all_accounted_for() -> None:
    """No refusal this file exercises may be an undocumented code."""

    for code in (
        "input.missing",
        "simulation.models.required",
        "simulation.backend.unsupported",
        "simulation.request.invalid",
        "simulation.analysis.unsupported",
        "simulation.operation.deprecated",
        "testbench.handoff.inconsistent",
        "testbench.parameters.unresolved",
        "testbench.deck.mismatch",
        "testbench.artifact.digest_mismatch",
        "configuration.models.not_self_contained",
    ):
        assert code in UNIFIED_DIAGNOSTIC_CODES


# --------------------------------------------------------------------------
# Native end-to-end evidence
# --------------------------------------------------------------------------


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_split_required_artifact_runs_every_declared_analysis(tmp_path: Path) -> None:
    payload = simulate(
        MODEL_FREE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"

    dispatch = payload["data"]["extensions"][DISPATCH_EXTENSION]
    assert dispatch["mode"] == "split"
    assert dispatch["declared_analysis_count"] == 2
    assert dispatch["dispatched_analysis_count"] == 2
    assert dispatch["completed_analysis_count"] == 2
    assert dispatch["passing_analysis_count"] == 2

    target = payload["data"]["extensions"][TARGET_EXTENSION]
    assert target["kind"] == "simra-artifact"
    assert target["digests_verified"] is True
    assert target["simulation_handoff"] == "split_required"
    assert target["declared_analysis_count"] == 2

    assert "simulation.analyses.dispatched" in _codes(payload)

    operating_point, transient = dispatch["analyses"]
    assert [facts["kind"] for facts in dispatch["analyses"]] == ["op", "tran"]
    assert [facts["index"] for facts in dispatch["analyses"]] == [0, 1]
    for facts in dispatch["analyses"]:
        assert facts["execution_status"] == "completed"
        assert facts["engineering_status"] == "pass"
    assert transient["declared"] == {"kind": "tran", "step": "100p", "stop": "20n"}

    # Both analyses pass, so the weakest is the first declared, and the returned
    # envelope's own evidence block is that analysis's.
    assert dispatch["reported_analysis_index"] == 0
    assert payload["data"]["analysis"]["type"] == "op"
    assert payload["data"]["analysis"]["point_count"] == 1

    # Every dispatched analysis kept its own deck and its own full envelope.
    for facts in (operating_point, transient):
        deck = Path(facts["deck_path"])
        assert deck.is_file()
        assert _digest(deck) == facts["deck_sha256"]

        result_path = Path(facts["result_path"])
        assert result_path.name == f"analysis-0{facts['index'] + 1}-{facts['kind']}.result.json"
        assert _digest(result_path) == facts["result_sha256"]
        child = json.loads(result_path.read_text(encoding="utf-8"))
        assert conformance.result_conformance_issues(child) == ()
        assert not list(DATA_VALIDATOR.iter_errors(child["data"]))
        assert child["data"]["protocol"]["operation_profile"] == (
            CIRCUIT_SIMULATE_PROFILE
        )

    op_child = json.loads(Path(operating_point["result_path"]).read_text("utf-8"))
    assert op_child["data"]["analysis"]["type"] == "op"
    assert op_child["data"]["analysis"]["convergence"] == "converged"
    assert op_child["data"]["analysis"]["point_count"] == 1
    assert op_child["data"]["analysis"]["finite_value_count"] == 4

    tran_child = json.loads(Path(transient["result_path"]).read_text("utf-8"))
    assert tran_child["data"]["analysis"]["type"] == "tran"
    assert tran_child["data"]["analysis"]["convergence"] == "converged"
    assert tran_child["data"]["analysis"]["point_count"] > 1
    assert tran_child["data"]["analysis"]["dependent_variable_count"] == 4

    roles = {item["role"] for item in payload["artifacts"]}
    assert {
        "simulation.deck",
        "simulation.analysis-result",
        "simulation.log",
        "simulation.result",
    } <= roles


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_model_naming_artifact_runs_with_supplied_collateral(tmp_path: Path) -> None:
    payload = simulate(
        NMOS_SOURCE / "schematic.artifact.json",
        tmp_path / "out",
        discovery=DiscoveryManager(),
        models_file=NMOS_SOURCE / "nmos_lv.models",
    )
    _assert_contract(payload)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["extensions"][TARGET_EXTENSION]["self_contained"] is False

    configuration = payload["data"]["extensions"]["org.openada"]["configuration"]
    assert len(configuration) == 1
    assert configuration[0]["role"] == "spice-model-library"
    assert configuration[0]["identity"] == "content-digest"
    assert configuration[0]["sha256"] == _digest(NMOS_SOURCE / "nmos_lv.models")

    dispatch = payload["data"]["extensions"][DISPATCH_EXTENSION]
    operating_point, sweep = dispatch["analyses"]
    assert operating_point["kind"] == "op"
    assert sweep["kind"] == "dc"

    op_child = json.loads(Path(operating_point["result_path"]).read_text("utf-8"))
    assert op_child["data"]["analysis"]["point_count"] == 1

    dc_child = json.loads(Path(sweep["result_path"]).read_text("utf-8"))
    # 0 V to 1.8 V in 50 mV steps is 37 inclusive sweep points.
    assert dc_child["data"]["analysis"]["point_count"] == 37
    assert dc_child["data"]["analysis"]["convergence"] == "converged"


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_cli_simulate_honours_an_artifact_target_without_a_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``openada simulate`` decides by what the request needs, not by a flag.

    A published artifact can only be honoured by the semantic path, so it goes
    there whether or not ``--backend`` was spelled; the native ngspice interface
    is never silently handed one.
    """

    exit_code = main(
        [
            "simulate",
            str(MODEL_FREE / "schematic.artifact.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    _assert_contract(payload)
    assert exit_code == 0
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["protocol"]["driver_id"] == "org.openada.driver.ngspice"
    assert payload["data"]["extensions"][TARGET_EXTENSION]["kind"] == "simra-artifact"
    assert payload["data"]["extensions"][DISPATCH_EXTENSION][
        "dispatched_analysis_count"
    ] == 2


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
    # The deprecated verb is an alias: what comes back is a circuit.simulate
    # result, so no caller can observe two semantics.
    assert payload["operation"] == "simulate"
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["protocol"]["request_id"] == (
        "3f2c1a6e-7b4d-4c8a-9e15-2d6f0b7a4c31"
    )
    assert payload["data"]["protocol"]["driver_id"] == "org.openada.driver.ngspice"
    assert DEPRECATION_CODE in _codes(payload)


@pytest.mark.skipif(NGSPICE is None, reason="native ngspice unavailable")
def test_collateral_free_dispatch_of_a_model_naming_deck_stays_unknown(
    tmp_path: Path,
) -> None:
    """An undefined model must never be reported as an engineering failure."""

    descriptor = _published(tmp_path, NMOS_SOURCE)
    _republish(descriptor, simulation_ready=True)

    payload = simulate(
        descriptor,
        tmp_path / "out",
        discovery=DiscoveryManager(),
    )
    _assert_contract(payload)
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["extensions"][DISPATCH_EXTENSION][
        "passing_analysis_count"
    ] == 0
