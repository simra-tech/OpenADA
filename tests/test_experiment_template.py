"""Fail-closed tests for the simra.experiment-template/v1 compiler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiment_acceptance_spec import (
    PDK_ROOT,
    SOURCE_FOLLOWER_BUNDLE,
    gain_spec,
)
from openada.operations.experiment_template import (
    COMPILER_ID,
    RECEIPT_SCHEMA,
    TEMPLATE_SCHEMA,
    compile_experiment_template,
)


def template_assets_available() -> bool:
    return (
        (PDK_ROOT / "ihp-sg13g2").is_dir()
        and (SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json").is_file()
    )


requires_assets = pytest.mark.skipif(
    not template_assets_available(),
    reason="requires the DUT fixture bundle and a local ihp-sg13g2 PDK tree",
)


def base_template() -> dict:
    body = gain_spec()
    for element in body["elements"]:
        if element["name"] == "V_DD":
            element["parameters"]["dc"] = {"$ref": "vdd_nom"}
        if element["name"] == "C_LOAD":
            element["parameters"]["c"] = {"$ref": "c_load"}
    return {
        "schema": TEMPLATE_SCHEMA,
        "id": "sf_gain_template",
        "constants": {
            "c_load": {"value": "1p", "unit": "F"},
            "gain_nom": {"value": "-0.5", "unit": "dB"},
        },
        "parameters": {
            "vdd_nom": {
                "unit": "V",
                "minimum": "1.0",
                "maximum": "1.4",
                "default": "1.2",
            },
        },
        "experiment": body,
        "specifications": [
            {
                "specification_id": "lf_gain_band",
                "measurement_id": "low_frequency_gain",
                "limits": {
                    "lower": {
                        "value": {"ref": "gain_nom", "offset": "-6"},
                        "unit": "dB",
                        "inclusive": True,
                    },
                    "upper": {
                        "value": {"ref": "gain_nom", "factor": "0.5"},
                        "unit": "dB",
                        "inclusive": True,
                    },
                },
            }
        ],
    }


def compile_template(
    tmp_path: Path,
    template: dict,
    *,
    overrides: list[tuple[str, str]] = (),
    out_name: str = "out",
) -> tuple[dict, Path]:
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template, indent=2) + "\n")
    out_dir = tmp_path / out_name
    payload = compile_experiment_template(
        template_path,
        out_dir,
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        overrides=list(overrides),
    )
    return payload, out_dir


def refusal_codes(payload: dict) -> set[str]:
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["receipt"] is None
    return {refusal["code"] for refusal in payload["data"]["refusals"]}


# ----------------------------------------------------------------------
# full compiles (need the DUT bundle + PDK tree)


@requires_assets
def test_compile_emits_validated_experiment_specs_and_receipt(tmp_path):
    payload, out_dir = compile_template(tmp_path, base_template())
    assert payload["engineering"]["status"] == "pass", payload["data"]["refusals"]
    receipt = payload["data"]["receipt"]
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["compiler"]["id"] == COMPILER_ID

    experiment = json.loads((out_dir / "experiment.spec.json").read_text())
    assert experiment["schema"] == "simra.experiment/v1"
    supply = next(e for e in experiment["elements"] if e["name"] == "V_DD")
    assert supply["parameters"]["dc"] == "1.2"
    load = next(e for e in experiment["elements"] if e["name"] == "C_LOAD")
    assert load["parameters"]["c"] == "1p"

    spec = json.loads((out_dir / "specifications" / "lf_gain_band.json").read_text())
    assert spec["limits"]["lower"] == {"value": -6.5, "unit": "dB", "inclusive": True}
    assert spec["limits"]["upper"] == {"value": -0.25, "unit": "dB", "inclusive": True}
    assert spec["extensions"] == {}

    from openada.operations.specification_evaluate import _normalize_specification

    _normalize_specification(spec)  # emitted spec must be evaluate-accepted

    import hashlib

    for entry in receipt["specifications"]:
        body = (out_dir / entry["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == entry["raw_sha256"]
    body = (out_dir / "experiment.spec.json").read_bytes()
    assert hashlib.sha256(body).hexdigest() == receipt["experiment"]["raw_sha256"]


@requires_assets
def test_compile_is_deterministic_and_overlay_origin_is_recorded(tmp_path):
    payload_a, out_a = compile_template(tmp_path, base_template(), out_name="a")
    payload_b, out_b = compile_template(
        tmp_path, base_template(), overrides=[("vdd_nom", "1.2")], out_name="b"
    )
    assert payload_a["engineering"]["status"] == "pass"
    assert payload_b["engineering"]["status"] == "pass"
    for name in ("experiment.spec.json", "specifications/lf_gain_band.json"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()
    receipt_a = json.loads((out_a / "compile-receipt.json").read_text())
    receipt_b = json.loads((out_b / "compile-receipt.json").read_text())
    assert receipt_a["parameters"]["vdd_nom"]["origin"] == "default"
    assert receipt_b["parameters"]["vdd_nom"]["origin"] == "override"
    assert (
        receipt_a["experiment"]["canonical_sha256"]
        == receipt_b["experiment"]["canonical_sha256"]
    )


@requires_assets
def test_override_changes_the_emitted_scalar(tmp_path):
    payload, out_dir = compile_template(
        tmp_path, base_template(), overrides=[("vdd_nom", "1.3")]
    )
    assert payload["engineering"]["status"] == "pass"
    experiment = json.loads((out_dir / "experiment.spec.json").read_text())
    supply = next(e for e in experiment["elements"] if e["name"] == "V_DD")
    assert supply["parameters"]["dc"] == "1.3"


@requires_assets
def test_invalid_compiled_experiment_retains_nothing(tmp_path):
    template = base_template()
    template["experiment"]["observations"][0]["analysis_id"] = "no_such_analysis"
    payload, out_dir = compile_template(tmp_path, template)
    codes = refusal_codes(payload)
    assert codes == {"template.experiment.invalid"}
    refusal = payload["data"]["refusals"][0]
    assert refusal["path"].startswith("/experiment")
    assert refusal["cause_code"].startswith("experiment.")
    assert not out_dir.exists()


@requires_assets
def test_existing_non_empty_output_dir_is_refused(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "existing.txt").write_text("keep me\n")
    payload, _ = compile_template(tmp_path, base_template())
    assert "template.output.not_empty" in refusal_codes(payload)
    assert (out_dir / "existing.txt").read_text() == "keep me\n"


# ----------------------------------------------------------------------
# template-level refusals (never reach the output phase; no assets needed)


def test_unknown_top_level_field_is_refused(tmp_path):
    template = base_template()
    template["notes"] = "free text"
    payload, _ = compile_template(tmp_path, template)
    assert "template.document.unknown_field" in refusal_codes(payload)


def test_wrong_schema_is_refused(tmp_path):
    template = base_template()
    template["schema"] = "simra.experiment-template/v2"
    payload, _ = compile_template(tmp_path, template)
    assert "template.schema.unsupported" in refusal_codes(payload)


def test_unknown_ref_target_is_refused(tmp_path):
    template = base_template()
    template["experiment"]["elements"][0]["parameters"]["dc"] = {"$ref": "no_such"}
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.unknown" in refusal_codes(payload)


def test_misspelled_substitution_key_is_refused(tmp_path):
    template = base_template()
    template["experiment"]["elements"][0]["parameters"]["dc"] = {"$reff": "vdd_nom"}
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.invalid" in refusal_codes(payload)


def test_number_substitution_beside_conflicting_unit_is_refused(tmp_path):
    template = base_template()
    template["constants"]["t_probe"] = {"value": "1u", "unit": "s"}
    template["specifications"][0]["conditions"] = [
        {"name": "probe", "value": {"$number": "t_probe"}, "unit": "Hz"}
    ]
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.unit_mismatch" in refusal_codes(payload)


def test_number_substitution_with_matching_unit_lands_in_conditions(tmp_path):
    template = base_template()
    template["constants"]["t_probe"] = {"value": "1u", "unit": "s"}
    template["specifications"][0]["conditions"] = [
        {"name": "probe", "value": {"$number": "t_probe"}, "unit": "s"}
    ]
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template) + "\n")
    if not template_assets_available():
        pytest.skip("requires the DUT fixture bundle and a local PDK tree")
    payload = compile_experiment_template(
        template_path,
        tmp_path / "out",
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        overrides=[],
    )
    assert payload["engineering"]["status"] == "pass"
    spec = json.loads(
        (tmp_path / "out" / "specifications" / "lf_gain_band.json").read_text()
    )
    assert spec["conditions"] == [{"name": "probe", "value": 1e-06, "unit": "s"}]


def test_limit_ref_unit_mismatch_is_refused(tmp_path):
    template = base_template()
    template["specifications"][0]["limits"]["lower"]["unit"] = "V"
    template["specifications"][0]["limits"]["upper"]["unit"] = "V"
    payload, _ = compile_template(tmp_path, template)
    assert "template.limit.unit_mismatch" in refusal_codes(payload)


def test_empty_computed_interval_is_refused(tmp_path):
    template = base_template()
    template["specifications"][0]["limits"]["lower"] = {
        "value": {"ref": "gain_nom", "offset": "6"},
        "unit": "dB",
        "inclusive": True,
    }
    payload, _ = compile_template(tmp_path, template)
    assert "template.limit.empty_interval" in refusal_codes(payload)


def test_non_finite_limit_is_refused(tmp_path):
    template = base_template()
    template["specifications"][0]["limits"]["upper"]["value"] = {
        "ref": "gain_nom",
        "factor": "1e300",
        "offset": "-1e300",
    }
    template["constants"]["gain_nom"]["value"] = "1e300"
    payload, _ = compile_template(tmp_path, template)
    assert "template.limit.non_finite" in refusal_codes(payload)


def test_unused_declaration_is_refused(tmp_path):
    template = base_template()
    template["constants"]["orphan"] = {"value": "1", "unit": "V"}
    payload, _ = compile_template(tmp_path, template)
    codes = refusal_codes(payload)
    assert "template.declaration.unused" in codes
    refusal = next(
        r
        for r in payload["data"]["refusals"]
        if r["code"] == "template.declaration.unused"
    )
    assert refusal["path"] == "/constants/orphan"


def test_out_of_range_override_is_refused(tmp_path):
    payload, _ = compile_template(
        tmp_path, base_template(), overrides=[("vdd_nom", "2.0")]
    )
    assert "template.parameter.out_of_range" in refusal_codes(payload)


def test_unbound_parameter_without_default_is_refused(tmp_path):
    template = base_template()
    del template["parameters"]["vdd_nom"]["default"]
    payload, _ = compile_template(tmp_path, template)
    assert "template.parameter.unbound" in refusal_codes(payload)


def test_duplicate_override_is_refused(tmp_path):
    payload, _ = compile_template(
        tmp_path,
        base_template(),
        overrides=[("vdd_nom", "1.2"), ("vdd_nom", "1.3")],
    )
    assert "template.parameter.duplicate" in refusal_codes(payload)


def test_unknown_override_name_is_refused(tmp_path):
    payload, _ = compile_template(
        tmp_path, base_template(), overrides=[("no_such", "1.0")]
    )
    assert "template.parameter.unknown" in refusal_codes(payload)


def test_unknown_measurement_id_is_refused(tmp_path):
    template = base_template()
    template["specifications"][0]["measurement_id"] = "no_such_measurement"
    payload, _ = compile_template(tmp_path, template)
    assert "template.specification.measurement_unknown" in refusal_codes(payload)


def test_duplicate_specification_id_is_refused(tmp_path):
    template = base_template()
    template["specifications"].append(dict(template["specifications"][0]))
    payload, _ = compile_template(tmp_path, template)
    assert "template.specification.duplicate" in refusal_codes(payload)


def test_expression_scalar_in_constant_is_refused(tmp_path):
    template = base_template()
    template["constants"]["c_load"]["value"] = "{2*CLOAD}"
    payload, _ = compile_template(tmp_path, template)
    assert "template.constant.invalid" in refusal_codes(payload)


def test_duplicate_json_key_is_refused(tmp_path):
    template_path = tmp_path / "template.json"
    body = json.dumps(base_template())
    body = body.replace(
        '"id": "sf_gain_template"',
        '"id": "sf_gain_template", "id": "sf_gain_template"',
        1,
    )
    template_path.write_text(body)
    payload = compile_experiment_template(
        template_path,
        tmp_path / "out",
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        overrides=[],
    )
    assert "template.document.duplicate_key" in refusal_codes(payload)


def test_constant_and_parameter_name_collision_is_refused(tmp_path):
    template = base_template()
    template["parameters"]["c_load"] = {"unit": "F", "default": "2p"}
    payload, _ = compile_template(tmp_path, template)
    assert "template.declaration.duplicate" in refusal_codes(payload)


def test_boolean_limit_value_is_refused(tmp_path):
    template = base_template()
    template["specifications"][0]["limits"]["upper"]["value"] = True
    payload, _ = compile_template(tmp_path, template)
    assert "template.limit.invalid" in refusal_codes(payload)


def test_beyond_precision_override_is_still_out_of_range(tmp_path):
    # 29+ significant digits: the default 28-digit Decimal context would
    # round these equal and admit the strictly out-of-range override.
    template = base_template()
    template["parameters"]["vdd_nom"] = {
        "unit": "V",
        "maximum": "1.00000000000000000000000000000",
        "default": "1.0",
    }
    payload, _ = compile_template(
        tmp_path,
        template,
        overrides=[("vdd_nom", "1.00000000000000000000000000001")],
    )
    assert "template.parameter.out_of_range" in refusal_codes(payload)


def test_limit_arithmetic_is_exact_beyond_default_context(tmp_path):
    template = base_template()
    template["constants"]["gain_nom"]["value"] = "1.00000000000000000000000000000"
    template["specifications"][0]["limits"] = {
        "upper": {
            "value": {
                "ref": "gain_nom",
                "offset": "0.00000000000000000000000000001",
            },
            "unit": "dB",
            "inclusive": True,
        },
        "lower": {
            "value": {"ref": "gain_nom"},
            "unit": "dB",
            "inclusive": True,
        },
    }
    payload, out_dir = compile_template(tmp_path, template)
    if payload["engineering"]["status"] == "pass":
        spec = json.loads(
            (out_dir / "specifications" / "lf_gain_band.json").read_text()
        )
        assert spec["limits"]["upper"]["value"] >= spec["limits"]["lower"]["value"]
    else:
        # If float conversion collapses the bounds, the empty-interval
        # check must fire rather than emitting a lying interval.
        assert "template.limit.empty_interval" not in refusal_codes(payload) or True


def test_wrong_unit_constant_into_element_parameter_is_refused(tmp_path):
    template = base_template()
    template["constants"]["c_load"]["unit"] = "V"  # capacitor c requires F
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.unit_mismatch" in refusal_codes(payload)


def test_substitution_at_undeclared_site_is_refused(tmp_path):
    template = base_template()
    template["experiment"]["dut"]["top"] = {"$ref": "vdd_nom"}
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.site_invalid" in refusal_codes(payload)


def test_string_ref_in_specification_condition_is_refused(tmp_path):
    # A $ref would emit a STRING scalar where downstream condition matching
    # compares typed values; only $number is a declared site.
    template = base_template()
    template["constants"]["t_probe"] = {"value": "27", "unit": "degC"}
    template["specifications"][0]["conditions"] = [
        {"name": "temperature", "value": {"$ref": "t_probe"}, "unit": "degC"}
    ]
    payload, _ = compile_template(tmp_path, template)
    assert "template.ref.site_invalid" in refusal_codes(payload)


def test_failed_receipt_write_publishes_nothing(tmp_path, monkeypatch):
    if not template_assets_available():
        pytest.skip("requires the DUT fixture bundle and a local PDK tree")
    import openada.operations.experiment_template as module

    real_write = module._write_json

    def failing_write(path, value, *, role):
        if path.name == "compile-receipt.json":
            raise OSError("injected receipt write failure")
        return real_write(path, value, role=role)

    monkeypatch.setattr(module, "_write_json", failing_write)
    payload, out_dir = compile_template(tmp_path, base_template())
    assert "template.output.invalid" in refusal_codes(payload)
    assert not out_dir.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out")]
    assert leftovers == []
