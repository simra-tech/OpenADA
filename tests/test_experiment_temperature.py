"""Explicit non-default temperature in simra.experiment/v1 conditions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiment_acceptance_spec import (
    PDK_ROOT,
    SOURCE_FOLLOWER_BUNDLE,
    acceptance_assets_available,
    gain_spec,
)
from openada.operations.experiment import validate_experiment


def _validation_assets_available() -> bool:
    return (
        (PDK_ROOT / "ihp-sg13g2").is_dir()
        and (SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json").is_file()
    )


requires_assets = pytest.mark.skipif(
    not _validation_assets_available(),
    reason="requires the DUT fixture bundle and a local ihp-sg13g2 PDK tree",
)


def _validate(tmp_path: Path, spec: dict):
    spec_path = tmp_path / "experiment.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    return validate_experiment(spec_path, pdk="ihp-sg13g2", pdk_root=PDK_ROOT)


@requires_assets
def test_non_default_temperature_becomes_the_binding_temperature(tmp_path):
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "85"
    prepared, issues = _validate(tmp_path, spec)
    assert not issues, [issue.record() for issue in issues]
    assert prepared.resolved_pdk.binding.simulation_temperature_c == "85"


@requires_assets
def test_non_default_temperature_lands_in_the_bound_deck(tmp_path):
    from openada.pdk_bindings import bind_deck

    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "-40"
    prepared, issues = _validate(tmp_path, spec)
    assert not issues
    bound_text, facts = bind_deck(
        prepared.runs[0].portable_deck,
        prepared.resolved_pdk,
    )
    lines = [line.strip().casefold() for line in bound_text.splitlines()]
    assert ".option temp=-40" in lines
    assert facts["simulation_temperature_c"] == "-40"


@requires_assets
def test_profile_default_temperature_stays_verbatim(tmp_path):
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = 27
    prepared, issues = _validate(tmp_path, spec)
    assert not issues
    assert prepared.resolved_pdk.binding.simulation_temperature_c == "27"


@requires_assets
def test_non_numeric_temperature_is_refused(tmp_path):
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "{hot}"
    prepared, issues = _validate(tmp_path, spec)
    assert prepared is None
    assert any(
        issue.code == "experiment.condition.invalid"
        and issue.path == "/conditions/pdk/temperature_c"
        for issue in issues
    )


@requires_assets
@pytest.mark.parametrize("temperature", ["-274", "-273.15", "1001", "1e6"])
def test_out_of_range_temperature_is_refused(tmp_path, temperature):
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = temperature
    prepared, issues = _validate(tmp_path, spec)
    assert prepared is None
    assert any(
        issue.code == "experiment.condition.temperature_unsupported"
        for issue in issues
    )


@pytest.mark.skipif(
    not acceptance_assets_available(),
    reason="requires ngspice, the IHP PDK, and the DUT fixture bundle",
)
def test_experiment_runs_at_the_declared_temperature(tmp_path):
    from openada.discovery import DiscoveryManager
    from openada.operations.experiment import run_experiment

    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "85"
    spec_path = tmp_path / "experiment.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    payload = run_experiment(
        spec_path,
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
    )
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"][:3]
    manifest = payload["data"]["manifest"]
    assert manifest["pdk"]["temperature_c"] == "85"
    extraction = json.loads(
        next((tmp_path / "evidence" / "analyses").rglob("extract.result.json")).read_text()
    )
    conditions = extraction["data"]["extraction"]["series"]["conditions"]
    assert {"name": "temperature", "value": 85.0, "unit": "degC"} in conditions


@requires_assets
def test_suffixed_temperature_token_is_canonicalized(tmp_path):
    # "27m" is the SPICE scalar 0.027 degC; the binding token must be the
    # canonical suffix-free spelling so the deck and the typed conditions
    # carry one identical numeric value.
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "27m"
    prepared, issues = _validate(tmp_path, spec)
    assert not issues
    assert prepared.resolved_pdk.binding.simulation_temperature_c == "0.027"


@requires_assets
def test_beyond_precision_temperature_bound_is_enforced(tmp_path):
    # Would round to exactly 1000 under the default 28-digit context and
    # slip past the closed upper bound.
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = (
        "1000.0000000000000000000000000001"
    )
    prepared, issues = _validate(tmp_path, spec)
    assert prepared is None
    assert any(
        issue.code == "experiment.condition.temperature_unsupported"
        for issue in issues
    )


@requires_assets
def test_negative_zero_temperature_canonicalizes_to_plain_zero(tmp_path):
    spec = gain_spec()
    spec["conditions"]["pdk"]["temperature_c"] = "-0.0"
    prepared, issues = _validate(tmp_path, spec)
    assert not issues
    assert prepared.resolved_pdk.binding.simulation_temperature_c == "0"


def test_off_reference_advisory_does_not_claim_the_distance_is_small():
    from openada.operations.simulate import _binding_advisories

    notes = _binding_advisories(
        {"model_tnom_c": "27", "simulation_temperature_c": "200"},
        deck_text="",
        corner="mos_tt",
        default_corner="mos_tt",
    )
    advisory = next(
        note for note in notes if note["code"] == "pdk.temperature.off_reference"
    )
    assert "distance is small" not in advisory["message"]
    assert "extrapolated" in advisory["message"]
