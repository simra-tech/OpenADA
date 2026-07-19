from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).resolve().parents[1]
PROFILE_SCHEMA = json.loads(
    (ROOT / "schemas" / "operation-profile-v0alpha2.schema.json").read_text(
        encoding="utf-8"
    )
)
PROFILE_FILENAMES = {
    "artifact.transform-v1alpha1.json",
    "digital.hdl.simulate-v1alpha1.json",
    "electromagnetic.analyze-v1alpha1.json",
    "network.parameters.extract-v1alpha1.json",
}
PROFILE_PATHS = tuple(
    ROOT / "profiles" / filename for filename in sorted(PROFILE_FILENAMES)
)
EXPECTED_OPERATIONS = {
    "openada.operation/artifact.transform/v1alpha1",
    "openada.operation/digital.hdl.simulate/v1alpha1",
    "openada.operation/electromagnetic.analyze/v1alpha1",
    "openada.operation/network.parameters.extract/v1alpha1",
}
SHA256 = "a" * 64


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _profiles() -> tuple[dict, ...]:
    return tuple(_load(path) for path in PROFILE_PATHS)


def _profile(operation: str) -> dict:
    return next(
        profile for profile in _profiles() if profile["operation"]["id"] == operation
    )


def _validator(profile: dict, section: str) -> Draft202012Validator:
    return Draft202012Validator(
        profile[section][
            "parameters_schema" if section == "request" else "data_schema"
        ],
        format_checker=FormatChecker(),
    )


def _assert_valid(validator: Draft202012Validator, value: dict) -> None:
    errors = sorted(
        validator.iter_errors(value), key=lambda error: list(error.absolute_path)
    )
    assert errors == []


def _dimensions(*, conclusion: str = "inconclusive") -> dict:
    return {
        "dependency_readiness": "unknown",
        "execution_state": "not-started",
        "artifact_readiness": "unknown",
        "engineering_conclusion": conclusion,
        "workflow_review": "unreviewed",
        "signoff_approval": "not-requested",
        "extensions": {},
    }


def test_profiles_and_embedded_schemas_are_closed_draft_2020_12_contracts() -> None:
    Draft202012Validator.check_schema(PROFILE_SCHEMA)
    outer = Draft202012Validator(PROFILE_SCHEMA, format_checker=FormatChecker())

    profiles = _profiles()
    assert {profile["operation"]["id"] for profile in profiles} == EXPECTED_OPERATIONS
    for profile in profiles:
        outer.validate(profile)
        Draft202012Validator.check_schema(profile["request"]["parameters_schema"])
        Draft202012Validator.check_schema(
            profile["normalized_result"]["data_schema"]
        )
        assert profile["schema"] == "openada.operation-profile/v0alpha2"
        assert profile["request"]["parameters_schema"]["additionalProperties"] is False
        assert (
            profile["normalized_result"]["data_schema"]["additionalProperties"]
            is False
        )


def test_profiles_are_packaged_but_fail_closed_in_the_semantic_catalog() -> None:
    catalog = _load(ROOT / "catalog" / "semantic-surfaces-v0alpha1.json")
    records = {
        record["repository_path"]: record for record in catalog["profiles"]
    }
    expected_paths = {f"profiles/{filename}" for filename in PROFILE_FILENAMES}

    assert expected_paths <= records.keys()
    for path in expected_paths:
        assert records[path]["lifecycle"] == "experimental-hidden"
        assert records[path]["dispatchable"] is False

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    profile_data = pyproject.split('"share/openada/profiles" = [', 1)[1].split(
        "]", 1
    )[0]
    for path in expected_paths:
        assert f'"{path}"' in profile_data


def test_every_feature_has_an_exact_fake_mapping_without_native_availability_claims() -> None:
    for profile in _profiles():
        declared = {feature["id"] for feature in profile["features"]}
        mapped = {
            feature
            for mapping in profile["native_mappings"]
            for feature in mapping["supported_features"]
        }
        bound = {
            binding["feature_id"]
            for mapping in profile["native_mappings"]
            for binding in mapping["semantic_bindings"]
        }
        assert mapped == declared
        assert bound == declared
        for mapping in profile["native_mappings"]:
            assert mapping["driver_id"].startswith("org.example.")
            assert mapping["native_product_id"].startswith("org.example.")
            assert any(
                "does not advertise native availability" in limitation
                for limitation in mapping["limitations"]
            )


def test_feature_paths_facts_bindings_artifacts_and_diagnostics_are_closed() -> None:
    for profile in _profiles():
        parameters = profile["request"]["parameters_schema"]
        facts = {fact["path"] for fact in profile["normalized_result"]["facts"]}
        evidence_roles = {
            artifact["role"] for artifact in profile["evidence"]["artifact_roles"]
        }

        for feature in profile["features"]:
            selected = parameters
            for component in feature["parameter_path"].split("."):
                if "$ref" in selected:
                    reference = selected["$ref"]
                    assert reference.startswith("#/$defs/")
                    selected = parameters["$defs"][reference.rsplit("/", 1)[1]]
                selected = selected["properties"][component]
        for mapping in profile["native_mappings"]:
            for binding in mapping["semantic_bindings"]:
                assert set(binding["output_fact_paths"]) <= facts
            for artifact in mapping["artifact_bindings"]:
                assert artifact["canonical_role"] in evidence_roles
        codes = [diagnostic["code"] for diagnostic in profile["diagnostics"]]
        assert len(codes) == len(set(codes))

        json.dumps(profile, allow_nan=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("operation", "request_payload"),
    [
        (
            "openada.operation/digital.hdl.simulate/v1alpha1",
            {
                "language": "systemverilog",
                "standard": "1800-2017",
                "top": "tb_top",
                "compile_options": {
                    "optimization": "none",
                    "warning_policy": "strict",
                    "defines": ["PUBLIC_FIXTURE=1"],
                    "extensions": {},
                },
                "self_check": {
                    "policy": "assertion-required",
                    "identity": "tb_top.completed",
                    "extensions": {},
                },
                "step_timeout_ms": 1000,
                "emit_waveform": True,
                "extensions": {},
            },
        ),
        (
            "openada.operation/network.parameters.extract/v1alpha1",
            {
                "source_sha256": SHA256,
                "format": {
                    "family": "two-port-ascii-interchange",
                    "revision": "1.0",
                    "extensions": {},
                },
                "parameter_family": "S",
                "source_encoding": "real-imaginary",
                "source_frequency_unit": "GHz",
                "ports": ["input", "output"],
                "matrix_elements": ["S11", "S21"],
                "max_points": 1024,
                "extensions": {},
            },
        ),
        (
            "openada.operation/electromagnetic.analyze/v1alpha1",
            {
                "analysis_model": "planar",
                "geometry_id": "fixture.geometry.v1",
                "material_model_id": "fixture.materials.v1",
                "ports": [
                    {
                        "id": "port.1",
                        "kind": "lumped",
                        "direction": "positive",
                        "reference_id": "reference.1",
                        "extensions": {},
                    }
                ],
                "boundary_id": "fixture.boundary.v1",
                "mesh_id": "fixture.mesh.v1",
                "sweep": {
                    "id": "fixture.sweep.v1",
                    "start_hz": 1e6,
                    "stop_hz": 1e9,
                    "points": 11,
                    "spacing": "logarithmic",
                    "extensions": {},
                },
                "solver_id": "fixture.solver.v1",
                "convergence": {
                    "id": "fixture.convergence.v1",
                    "metric": "network-delta",
                    "threshold": 0.001,
                    "maximum_passes": 8,
                    "extensions": {},
                },
                "reference_comparison": {
                    "policy": "not-requested",
                    "reference_sha256": None,
                    "tolerance": None,
                    "extensions": {},
                },
                "extensions": {},
            },
        ),
        (
            "openada.operation/artifact.transform/v1alpha1",
            {
                "input_sha256": SHA256,
                "transform": {"id": "convert", "revision": "v1", "extensions": {}},
                "input_format": {
                    "id": "example.source",
                    "revision": "v1",
                    "extensions": {},
                },
                "output_format": {
                    "id": "example.target",
                    "revision": "v1",
                    "extensions": {},
                },
                "output_role": "converted.artifact",
                "output_relative_path": "results/output.bin",
                "maximum_output_bytes": 4096,
                "options": {
                    "normalization": "deterministic",
                    "diagnostic_policy": "warnings-and-errors",
                    "extensions": {},
                },
                "extensions": {},
            },
        ),
    ],
)
def test_positive_requests_and_security_canaries(
    operation: str, request_payload: dict
) -> None:
    validator = _validator(_profile(operation), "request")
    _assert_valid(validator, request_payload)

    for canary in (
        "native_action",
        "command",
        "environment",
        "credential",
        "setup_text",
        "secret_store_location",
    ):
        injected = deepcopy(request_payload)
        injected[canary] = "org.example.canary"
        assert list(validator.iter_errors(injected)), canary


def test_language_pairs_ports_reference_policy_and_paths_fail_closed() -> None:
    digital = _profile("openada.operation/digital.hdl.simulate/v1alpha1")
    digital_validator = _validator(digital, "request")
    invalid_language_pair = {
        "language": "verilog",
        "standard": "1800-2017",
        "top": "tb",
        "compile_options": {
            "optimization": "none",
            "warning_policy": "default",
            "defines": [],
            "extensions": {},
        },
        "self_check": {
            "policy": "assertion-required",
            "identity": "done",
            "extensions": {},
        },
        "step_timeout_ms": 1,
        "extensions": {},
    }
    assert list(digital_validator.iter_errors(invalid_language_pair))

    network = _profile("openada.operation/network.parameters.extract/v1alpha1")
    network_request = {
        "source_sha256": SHA256,
        "format": {
            "family": "two-port-ascii-interchange",
            "revision": "1.0",
            "extensions": {},
        },
        "parameter_family": "S",
        "source_encoding": "real-imaginary",
        "source_frequency_unit": "Hz",
        "ports": ["only-one"],
        "matrix_elements": ["S11"],
        "max_points": 1,
        "extensions": {},
    }
    assert list(_validator(network, "request").iter_errors(network_request))

    electromagnetic = _profile("openada.operation/electromagnetic.analyze/v1alpha1")
    required_reference = {
        "policy": "required",
        "reference_sha256": None,
        "tolerance": None,
        "extensions": {},
    }
    parameter_schema = electromagnetic["request"]["parameters_schema"]
    reference_schema = {
        **parameter_schema["properties"]["reference_comparison"],
        "$defs": parameter_schema["$defs"],
    }
    assert list(Draft202012Validator(reference_schema).iter_errors(required_reference))

    transform = _profile("openada.operation/artifact.transform/v1alpha1")
    output_path_schema = transform["request"]["parameters_schema"]["properties"][
        "output_relative_path"
    ]
    path_validator = Draft202012Validator(output_path_schema)
    assert list(path_validator.iter_errors("../escape.bin"))
    assert list(path_validator.iter_errors("safe/../../escape.bin"))


def test_partial_and_unavailable_results_are_representable_without_inventing_success() -> None:
    digital = _profile("openada.operation/digital.hdl.simulate/v1alpha1")
    _assert_valid(
        _validator(digital, "normalized_result"),
        {
            "language": "verilog",
            "standard": "1364-2005",
            "top": "tb",
            "steps": [
                {
                    "role": "library-preparation",
                    "depends_on": [],
                    "execution_state": "failed",
                    "artifact_readiness": "unknown",
                    "diagnostics": ["dependency unavailable"],
                    "extensions": {},
                }
            ],
            "self_check": {
                "policy": "assertion-required",
                "identity": "done",
                "status": "inconclusive",
                "evidence_sha256": None,
                "extensions": {},
            },
            "lineage": {
                "source_digests": [],
                "file_list_sha256": None,
                "executable_sha256": None,
                "log_sha256": None,
                "waveform_sha256": None,
                "extensions": {},
            },
            "overall": _dimensions(),
            "extensions": {},
        },
    )

    network = _profile("openada.operation/network.parameters.extract/v1alpha1")
    _assert_valid(
        _validator(network, "normalized_result"),
        {
            "format_revision": "two-port-ascii-interchange/1.0",
            "source_sha256": SHA256,
            "parameter_family": "S",
            "source_encoding": "real-imaginary",
            "ports": ["input", "output"],
            "reference_impedance_ohm": None,
            "frequency_hz": [],
            "series": [],
            "normalized_series_sha256": None,
            "comparison": {
                "parser_identity": "org.example.parser.independent",
                "status": "not-run",
                "normalized_series_sha256": None,
                "extensions": {},
            },
            "dimensions": {
                **_dimensions(),
                "engineering_conclusion": "inconclusive",
            },
            "extensions": {},
        },
    )

    electromagnetic = _profile("openada.operation/electromagnetic.analyze/v1alpha1")
    _assert_valid(
        _validator(electromagnetic, "normalized_result"),
        {
            "analysis_model": "planar",
            "identities": {
                "geometry": "fixture.geometry.v1",
                "materials": "fixture.materials.v1",
                "boundary": "fixture.boundary.v1",
                "mesh": "fixture.mesh.v1",
                "sweep": "fixture.sweep.v1",
                "solver": "fixture.solver.v1",
                "convergence": "fixture.convergence.v1",
                "extensions": {},
            },
            "ports": [
                {
                    "id": "port.1",
                    "kind": "lumped",
                    "direction": "positive",
                    "reference_id": "reference.1",
                    "extensions": {},
                }
            ],
            "frequency_hz": [],
            "mesh": None,
            "solver": {
                "backend_identity": None,
                "version": None,
                "iterations": 0,
                "termination": "unknown",
                "extensions": {},
            },
            "convergence": {
                "metric": "network-delta",
                "threshold": 0.001,
                "achieved": None,
                "status": "inconclusive",
                "extensions": {},
            },
            "artifacts": {
                "network_sha256": None,
                "field_sha256": None,
                "mesh_report_sha256": None,
                "solver_log_sha256": None,
                "extensions": {},
            },
            "reference_comparison": {
                "policy": "not-requested",
                "status": "not-evaluated",
                "maximum_error": None,
                "tolerance": None,
                "extensions": {},
            },
            "dimensions": _dimensions(),
            "extensions": {},
        },
    )

    transform = _profile("openada.operation/artifact.transform/v1alpha1")
    _assert_valid(
        _validator(transform, "normalized_result"),
        {
            "input": None,
            "transform": {"id": "convert", "revision": "v1", "extensions": {}},
            "output": None,
            "freshness": "unknown",
            "containment": "unknown",
            "format_validation": "not-run",
            "engineering_evaluation": "not-evaluated",
            "equivalence_evaluation": "not-evaluated",
            "dimensions": _dimensions(conclusion="not-evaluated"),
            "extensions": {},
        },
    )


def test_transform_cannot_claim_engineering_or_equivalence_pass() -> None:
    transform = _profile("openada.operation/artifact.transform/v1alpha1")
    data_schema = transform["normalized_result"]["data_schema"]
    assert data_schema["properties"]["engineering_evaluation"] == {
        "const": "not-evaluated"
    }
    assert data_schema["properties"]["equivalence_evaluation"] == {
        "const": "not-evaluated"
    }
    assert data_schema["$defs"]["dimensions"]["properties"][
        "engineering_conclusion"
    ] == {"const": "not-evaluated"}


def test_public_profile_content_contains_no_private_or_vendor_residue() -> None:
    paths = (*PROFILE_PATHS, ROOT / "docs" / "provider-ecosystem-profiles.md")
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "commercialada",
        "cadence",
        "synopsys",
        "siemens",
        "mentor graphics",
        "ansys",
        "keysight",
        "silvaco",
        "globalfoundries",
        "gf22",
        "lm_license_file",
        "cds_license_file",
    )
    lowered = content.lower()
    assert all(term not in lowered for term in forbidden)
    assert re.search(r"/(?:home|opt|eda)/", content) is None
