from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from openada.operations.result_measure import (
    ASSERTION_PROFILE as MEASUREMENT_ASSERTION,
    OPERATION_PROFILE as MEASUREMENT_OPERATION,
)
from openada.operations.result_series_extract import (
    ASSERTION_PROFILE as SERIES_ASSERTION,
    OPERATION_PROFILE as SERIES_OPERATION,
)
from openada.operations.result_spectral_measure import (
    ASSERTION_PROFILE as SPECTRAL_ASSERTION,
    OPERATION_PROFILE as SPECTRAL_OPERATION,
)
from openada.operations.result_transfer_measure import (
    ASSERTION_PROFILE as TRANSFER_ASSERTION,
    OPERATION_PROFILE as TRANSFER_OPERATION,
)
from openada.operations.specification_evaluate import (
    ASSERTION_PROFILE as SPECIFICATION_ASSERTION,
    OPERATION_PROFILE as SPECIFICATION_OPERATION,
)


ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "schemas"
PROFILES = ROOT / "profiles"
V0ALPHA1 = SCHEMAS / "operation-profile-v0alpha1.schema.json"
V0ALPHA2 = SCHEMAS / "operation-profile-v0alpha2.schema.json"
MEASUREMENT_PROFILE = PROFILES / "result.measure-v1alpha1.json"
SERIES_PROFILE = PROFILES / "result.series.extract-v1alpha1.json"
SPECTRAL_PROFILE = PROFILES / "result.spectral.measure-v1alpha1.json"
TRANSFER_PROFILE = PROFILES / "result.transfer.measure-v1alpha1.json"
SPECIFICATION_PROFILE = PROFILES / "specification.evaluate-v1alpha1.json"
CIRCUIT_PROFILE = PROFILES / "circuit.simulate-v1alpha1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_published_v0alpha1_schema_remains_byte_identical() -> None:
    assert hashlib.sha256(V0ALPHA1.read_bytes()).hexdigest() == (
        "e7088d259d39b9c887821341074e7a15bec1d8ee1cf8194ab1c50ddf51b353e2"
    )


def test_v0alpha2_is_additive_and_old_profile_stays_on_v0alpha1() -> None:
    old_schema = _load(V0ALPHA1)
    new_schema = _load(V0ALPHA2)
    circuit = _load(CIRCUIT_PROFILE)

    Draft202012Validator.check_schema(old_schema)
    Draft202012Validator.check_schema(new_schema)
    Draft202012Validator(
        old_schema,
        format_checker=FormatChecker(),
    ).validate(circuit)

    assert old_schema["properties"]["schema"]["const"] == (
        "openada.operation-profile/v0alpha1"
    )
    assert new_schema["properties"]["schema"]["const"] == (
        "openada.operation-profile/v0alpha2"
    )
    assert circuit["schema"] == "openada.operation-profile/v0alpha1"
    assert new_schema["properties"]["native_mappings"]["minItems"] == 1


def test_typed_evidence_profiles_and_embedded_schemas_validate() -> None:
    schema = _load(V0ALPHA2)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    for path in (
        MEASUREMENT_PROFILE,
        SERIES_PROFILE,
        SPECTRAL_PROFILE,
        TRANSFER_PROFILE,
        SPECIFICATION_PROFILE,
    ):
        profile = _load(path)
        validator.validate(profile)
        Draft202012Validator.check_schema(profile["request"]["parameters_schema"])
        Draft202012Validator.check_schema(profile["normalized_result"]["data_schema"])
        assert profile["schema"] == "openada.operation-profile/v0alpha2"
        assert profile["native_mappings"]
        if profile["features"]:
            mapped_features = {
                feature
                for mapping in profile["native_mappings"]
                for feature in mapping.get("supported_features", [])
            }
            semantic_features = {
                binding["feature_id"]
                for mapping in profile["native_mappings"]
                for binding in mapping.get("semantic_bindings", [])
            }
            expected = {item["id"] for item in profile["features"]}
            assert mapped_features == expected
            assert semantic_features == expected


def test_module_profile_and_skills_share_exact_public_intent_ids() -> None:
    measurement = _load(MEASUREMENT_PROFILE)
    series = _load(SERIES_PROFILE)
    spectral = _load(SPECTRAL_PROFILE)
    transfer = _load(TRANSFER_PROFILE)
    specification = _load(SPECIFICATION_PROFILE)

    assert MEASUREMENT_OPERATION == "openada.operation/result.measure/v1alpha1"
    assert MEASUREMENT_ASSERTION == "openada.assertion/measurement.valid/v1alpha1"
    assert SPECIFICATION_OPERATION == (
        "openada.operation/specification.evaluate/v1alpha1"
    )
    assert SPECIFICATION_ASSERTION == (
        "openada.assertion/specification.satisfied/v1alpha1"
    )
    assert SERIES_OPERATION == "openada.operation/result.series.extract/v1alpha1"
    assert SERIES_ASSERTION == "openada.assertion/series.extraction.valid/v1alpha1"
    assert SPECTRAL_OPERATION == (
        "openada.operation/result.spectral.measure/v1alpha1"
    )
    assert SPECTRAL_ASSERTION == (
        "openada.assertion/spectral.measurement.valid/v1alpha1"
    )
    assert TRANSFER_OPERATION == (
        "openada.operation/result.transfer.measure/v1alpha1"
    )
    assert TRANSFER_ASSERTION == (
        "openada.assertion/transfer.measurement.valid/v1alpha1"
    )
    assert measurement["operation"]["id"] == MEASUREMENT_OPERATION
    assert measurement["assertion"]["id"] == MEASUREMENT_ASSERTION
    assert specification["operation"]["id"] == SPECIFICATION_OPERATION
    assert specification["assertion"]["id"] == SPECIFICATION_ASSERTION
    assert series["operation"]["id"] == SERIES_OPERATION
    assert series["assertion"]["id"] == SERIES_ASSERTION
    assert spectral["operation"]["id"] == SPECTRAL_OPERATION
    assert spectral["assertion"]["id"] == SPECTRAL_ASSERTION
    assert transfer["operation"]["id"] == TRANSFER_OPERATION
    assert transfer["assertion"]["id"] == TRANSFER_ASSERTION

    skill_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "skills").rglob("SKILL.md"))
    )
    for identifier in (
        MEASUREMENT_OPERATION,
        MEASUREMENT_ASSERTION,
        SERIES_OPERATION,
        SERIES_ASSERTION,
        SPECTRAL_OPERATION,
        SPECTRAL_ASSERTION,
        TRANSFER_OPERATION,
        TRANSFER_ASSERTION,
        SPECIFICATION_OPERATION,
        SPECIFICATION_ASSERTION,
    ):
        assert identifier in skill_text
    assert "openada.assertion/result.measurement.valid/v1alpha1" not in skill_text


def test_profile_schemas_close_extensions_and_bound_condition_strings() -> None:
    for path in (
        MEASUREMENT_PROFILE,
        SERIES_PROFILE,
        SPECTRAL_PROFILE,
        TRANSFER_PROFILE,
        SPECIFICATION_PROFILE,
    ):
        profile = _load(path)
        parameters = profile["request"]["parameters_schema"]
        extensions = parameters["$defs"]["extensions"]
        condition_value = parameters["$defs"]["condition"]["properties"]["value"]
        result_condition_value = profile["normalized_result"]["data_schema"]["$defs"][
            "condition"
        ]["properties"]["value"]

        assert extensions["maxProperties"] == 0
        assert extensions["additionalProperties"] is False
        assert condition_value["maxLength"] == 256
        assert result_condition_value["maxLength"] == 256
