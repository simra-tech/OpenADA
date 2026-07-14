from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).parents[1]
SCHEMA_DIR = ROOT / "schemas"
TEMPLATE_DIR = ROOT / "conformance" / "driver-kit"

REQUEST_SCHEMA_PATH = SCHEMA_DIR / "request-v0alpha1.schema.json"
DRIVER_MANIFEST_SCHEMA_PATH = SCHEMA_DIR / "driver-manifest-v0alpha1.schema.json"
OPERATION_PROFILE_SCHEMA_PATH = SCHEMA_DIR / "operation-profile-v0alpha1.schema.json"
SIMULATION_PROFILE_PATH = ROOT / "profiles" / "circuit.simulate-v1alpha1.json"
REQUEST_TEMPLATE_PATH = TEMPLATE_DIR / "request.template.json"
DRIVER_MANIFEST_TEMPLATE_PATH = TEMPLATE_DIR / "driver-manifest.template.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


REQUEST_SCHEMA = _load(REQUEST_SCHEMA_PATH)
DRIVER_MANIFEST_SCHEMA = _load(DRIVER_MANIFEST_SCHEMA_PATH)
OPERATION_PROFILE_SCHEMA = _load(OPERATION_PROFILE_SCHEMA_PATH)
SIMULATION_PROFILE = _load(SIMULATION_PROFILE_PATH)
REQUEST_TEMPLATE = _load(REQUEST_TEMPLATE_PATH)
DRIVER_MANIFEST_TEMPLATE = _load(DRIVER_MANIFEST_TEMPLATE_PATH)

REQUEST_VALIDATOR = Draft202012Validator(
    REQUEST_SCHEMA,
    format_checker=FormatChecker(),
)
DRIVER_MANIFEST_VALIDATOR = Draft202012Validator(
    DRIVER_MANIFEST_SCHEMA,
    format_checker=FormatChecker(),
)


def test_protocol_schemas_are_valid_and_have_immutable_identifiers() -> None:
    Draft202012Validator.check_schema(REQUEST_SCHEMA)
    Draft202012Validator.check_schema(DRIVER_MANIFEST_SCHEMA)
    Draft202012Validator.check_schema(OPERATION_PROFILE_SCHEMA)

    assert REQUEST_SCHEMA["properties"]["schema"]["const"] == (
        "openada.request/v0alpha1"
    )
    assert DRIVER_MANIFEST_SCHEMA["properties"]["schema"]["const"] == (
        "openada.driver-manifest/v0alpha1"
    )
    assert OPERATION_PROFILE_SCHEMA["properties"]["schema"]["const"] == (
        "openada.operation-profile/v0alpha1"
    )


def test_circuit_simulate_profile_and_embedded_closed_schemas_validate() -> None:
    Draft202012Validator(
        OPERATION_PROFILE_SCHEMA,
        format_checker=FormatChecker(),
    ).validate(SIMULATION_PROFILE)
    Draft202012Validator.check_schema(
        SIMULATION_PROFILE["request"]["parameters_schema"]
    )
    Draft202012Validator.check_schema(
        SIMULATION_PROFILE["normalized_result"]["data_schema"]
    )

    mappings = {
        item["driver_id"]: item for item in SIMULATION_PROFILE["native_mappings"]
    }
    assert set(mappings) == {
        "org.openada.driver.ngspice",
        "org.openada.driver.xyce",
    }
    assert all(
        item["supported_analyses"] == ["tran"] for item in mappings.values()
    )
    assert all(
        set(item["analysis_commands"]) == set(item["supported_analyses"])
        for item in mappings.values()
    )


def test_protocol_templates_validate_with_format_checking() -> None:
    REQUEST_VALIDATOR.validate(REQUEST_TEMPLATE)
    DRIVER_MANIFEST_VALIDATOR.validate(DRIVER_MANIFEST_TEMPLATE)
    Draft202012Validator(
        SIMULATION_PROFILE["request"]["parameters_schema"],
        format_checker=FormatChecker(),
    ).validate(REQUEST_TEMPLATE["parameters"])
    profile_features = {item["id"] for item in SIMULATION_PROFILE["features"]}
    assert set(REQUEST_TEMPLATE["driver_selector"]["required_features"]) <= profile_features


def test_request_rejects_unversioned_profiles_and_undeclared_fields() -> None:
    request = deepcopy(REQUEST_TEMPLATE)
    request["operation_profile"] = "circuit.simulate"
    request["unexpected"] = True

    errors = list(REQUEST_VALIDATOR.iter_errors(request))

    assert any(list(error.path) == ["operation_profile"] for error in errors)
    assert any(list(error.path) == [] for error in errors)


def test_request_locator_is_discriminated_and_side_effect_authority_is_closed() -> None:
    request = deepcopy(REQUEST_TEMPLATE)
    request["target"]["locator"]["session_id"] = "ambient-session"
    request["execution_constraints"]["side_effects"] = "unbounded-write"

    errors = list(REQUEST_VALIDATOR.iter_errors(request))

    assert any(list(error.path) == ["target", "locator"] for error in errors)
    assert any(
        list(error.path) == ["execution_constraints", "side_effects"]
        for error in errors
    )


def test_request_identity_policy_supports_native_databases_without_false_hashes() -> None:
    for identity_requirement in (
        "content-digest",
        "native-revision",
        "snapshot",
        "best-available",
    ):
        request = deepcopy(REQUEST_TEMPLATE)
        request["evidence_policy"]["identity_requirement"] = identity_requirement
        REQUEST_VALIDATOR.validate(request)


def test_request_requires_an_explicit_typed_evidence_destination() -> None:
    missing = deepcopy(REQUEST_TEMPLATE)
    del missing["evidence_destination"]
    relative = deepcopy(REQUEST_TEMPLATE)
    relative["evidence_destination"]["locator"]["path"] = "relative/evidence"

    missing_errors = list(REQUEST_VALIDATOR.iter_errors(missing))
    relative_errors = list(REQUEST_VALIDATOR.iter_errors(relative))

    assert any(list(error.path) == [] for error in missing_errors)
    assert any(
        list(error.path) == ["evidence_destination", "locator", "path"]
        for error in relative_errors
    )


def test_structured_capability_requires_a_conformance_reference() -> None:
    for maturity in ("structured", "workflow-validated"):
        manifest = deepcopy(DRIVER_MANIFEST_TEMPLATE)
        capability = manifest["capabilities"][0]
        capability["maturity"] = maturity
        capability["conformance_record_ids"] = []

        errors = list(DRIVER_MANIFEST_VALIDATOR.iter_errors(manifest))

        assert any(
            list(error.path) == ["capabilities", 0, "conformance_record_ids"]
            for error in errors
        )


def test_protocol_extensions_require_a_reverse_dns_namespace() -> None:
    request = deepcopy(REQUEST_TEMPLATE)
    request["extensions"] = {"backend": {"queue": "interactive"}}

    errors = list(REQUEST_VALIDATOR.iter_errors(request))

    assert any(list(error.path) == ["extensions"] for error in errors)
