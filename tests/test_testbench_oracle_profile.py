from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).parents[1]
PROFILE = json.loads(
    (ROOT / "profiles" / "testbench.oracle.compare-v1alpha1.json").read_text(
        encoding="utf-8"
    )
)
PROFILE_SCHEMA = json.loads(
    (ROOT / "schemas" / "operation-profile-v0alpha2.schema.json").read_text(
        encoding="utf-8"
    )
)
FIXTURES = ROOT / "conformance" / "testbench-oracle-v1" / "fixtures"

FEATURES = [
    "openada.feature/testbench-oracle.scalar-error/v1alpha1",
    "openada.feature/testbench-oracle.curve-exact-grid/v1alpha1",
    "openada.feature/testbench-oracle.mismatch-compliance/v1alpha1",
    "openada.feature/testbench-oracle.signed-coverage/v1alpha1",
    "openada.feature/testbench-oracle.validity-honesty/v1alpha1",
    "openada.feature/testbench-oracle.coverage-cost-lineage/v1alpha1",
]


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_profile_validates_against_v0alpha2_meta_schema() -> None:
    Draft202012Validator.check_schema(PROFILE_SCHEMA)
    errors = sorted(
        Draft202012Validator(
            PROFILE_SCHEMA,
            format_checker=FormatChecker(),
        ).iter_errors(PROFILE),
        key=lambda error: list(error.absolute_path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_embedded_request_and_result_schemas_are_closed_and_valid() -> None:
    request_schema = PROFILE["request"]["parameters_schema"]
    result_schema = PROFILE["normalized_result"]["data_schema"]
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(result_schema)

    request = {
        "observed": _fixture("observed.json"),
        "oracle": _fixture("oracle.json"),
        "tolerances": _fixture("tolerances.json"),
        "extensions": {},
    }
    request_validator = Draft202012Validator(request_schema)
    assert not list(request_validator.iter_errors(request))
    request["expression"] = "abs(observed-oracle)"
    assert list(request_validator.iter_errors(request))

    result = {
        "protocol": {
            "request_id": "00000000-0000-4000-8000-000000000001",
            "operation_profile": (
                "openada.operation/testbench.oracle.compare/v1alpha1"
            ),
            "assertion_profile": (
                "openada.assertion/testbench.oracle.comparison.valid/v1alpha1"
            ),
            "implementation_id": "org.openada.kernel.testbench-oracle",
            "conformance_id": "testbench-oracle-comparator-v1",
        },
        "comparison": _fixture("expected-comparison.json"),
        "request_sha256": "a" * 64,
        "extensions": {},
    }
    result_validator = Draft202012Validator(
        result_schema,
        format_checker=FormatChecker(),
    )
    assert not list(result_validator.iter_errors(result))
    result["protocol"]["backend"] = "ngspice"
    assert list(result_validator.iter_errors(result))


def test_semantic_constraints_bind_all_three_published_contracts() -> None:
    constraints = "\n".join(PROFILE["request"]["semantic_constraints"])
    for identity in (
        "simra.testbench-observables/v1",
        "simra.testbench-oracle-tolerances/v1",
        "simra.testbench-oracle-comparison/v1",
    ):
        assert identity in constraints
    assert "schemas/testbench-observables-v1.schema.json" in constraints
    assert "schemas/testbench-oracle-tolerances-v1.schema.json" in constraints
    assert "schemas/testbench-oracle-comparison-v1.schema.json" in constraints


def test_feature_and_pure_kernel_mapping_are_exact_and_conformance_bound() -> None:
    assert PROFILE["operation"]["id"] == (
        "openada.operation/testbench.oracle.compare/v1alpha1"
    )
    assert PROFILE["assertion"]["id"] == (
        "openada.assertion/testbench.oracle.comparison.valid/v1alpha1"
    )
    assert [item["id"] for item in PROFILE["features"]] == FEATURES
    mapping = PROFILE["native_mappings"]
    assert len(mapping) == 1
    assert mapping[0]["driver_id"] == "org.openada.kernel.testbench-oracle"
    assert mapping[0]["native_product_id"] == "org.openada.core.runtime"
    assert mapping[0]["supported_features"] == FEATURES
    assert [item["feature_id"] for item in mapping[0]["semantic_bindings"]] == FEATURES
    assert "testbench-oracle-comparator-v1" in mapping[0]["limitations"][-1]
    assert PROFILE["normalized_result"]["data_schema"]["$defs"]["protocol"][
        "properties"
    ]["conformance_id"] == {"const": "testbench-oracle-comparator-v1"}


def test_profile_freezes_unknown_and_absolute_near_zero_boundaries() -> None:
    unknown = PROFILE["assertion"]["truth_table"]["unknown"]
    assert "invalid_request" in unknown["allowed_execution_statuses"]
    assert any("UNKNOWN" in item for item in unknown["required_evidence"])
    constraints = PROFILE["request"]["semantic_constraints"]
    assert any("required UNKNOWN" in item for item in constraints)
    assert any("Scalar absolute" in item for item in constraints)
    assert any("performs no interpolation" in item for item in constraints)
