from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
from types import ModuleType

import pytest

from experiment_acceptance_spec import (
    PDK_ROOT,
    SOURCE_FOLLOWER_DIGESTS,
    acceptance_assets_available,
    gain_spec,
)
from openada.discovery import DiscoveryManager
from openada.operations.experiment import run_experiment


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sandboxy_checkout(locator: str | None = None) -> Path:
    path = Path(locator or "../sandboxy").expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


SANDBOXY_CHECKOUT = _sandboxy_checkout(
    os.environ.get("SANDBOXY_CHECKOUT")
)
MEASUREMENT_GUARD = SANDBOXY_CHECKOUT / "sandbox/measurement_guard.py"
REQUIRE_SANDBOXY_COMPAT = (
    os.environ.get("OPENADA_REQUIRE_SANDBOXY_COMPAT") == "1"
)


@pytest.fixture(scope="module")
def ihp_gain_run(tmp_path_factory):
    if not acceptance_assets_available():
        pytest.skip("the real IHP PDK and SourceFollower acceptance bundle are required")
    root = tmp_path_factory.mktemp("experiment-ihp-gain")
    spec_path = root / "gain.experiment.json"
    spec_path.write_text(
        json.dumps(gain_spec(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_dir = root / "evidence"
    payload = run_experiment(
        spec_path,
        output_dir,
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        timeout=300.0,
    )
    return payload, output_dir


def test_real_ihp_source_follower_gain_passes(ihp_gain_run):
    payload, output_dir = ihp_gain_run
    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    manifest = payload["data"]["manifest"]
    assert manifest["status"] == "pass"
    assert len(manifest["analyses"]) == 1
    measurement = manifest["analyses"][0]["measurements"][0]
    assert measurement["id"] == "low_frequency_gain"
    assert measurement["unit"] == "dB"
    assert -8.0 <= measurement["value"] <= 0.0

    request_path = Path(measurement["request_path"])
    assert request_path.parent.name == "requests"
    assert request_path.name == f"{measurement['request_raw_sha256']}.json"
    retained_request = json.loads(request_path.read_text(encoding="utf-8"))
    assert retained_request == gain_spec()["measurements"][0]["request"]
    assert measurement["request_raw_sha256"]
    assert measurement["request_canonical_sha256"]

    simulation = json.loads(
        (
            output_dir
            / "analyses"
            / "ac_gain"
            / "simulation"
            / "simulate.result.json"
        ).read_text(encoding="utf-8")
    )
    extension = simulation["data"]["extensions"]["org.openada.experiment"]
    assert extension["schema"] == "simra.experiment/v1"
    assert extension["spec_sha256"] == manifest["spec"]["raw_sha256"]
    assert (
        extension["dut_netlist_sha256"]
        == SOURCE_FOLLOWER_DIGESTS["netlist_sha256"]
    )
    assert extension["analysis_id"] == "ac_gain"
    assert any(
        record["role"] == "experiment.specification"
        and record["sha256"] == manifest["spec"]["raw_sha256"]
        for record in simulation["inputs"]
    )


def _require_measurement_guard(
    measurement_guard: Path = MEASUREMENT_GUARD,
    *,
    required: bool = REQUIRE_SANDBOXY_COMPAT,
) -> Path:
    if measurement_guard.is_file():
        return measurement_guard
    reason = (
        "Sandboxy ledger compatibility was not exercised because "
        f"{measurement_guard} is unavailable; set SANDBOXY_CHECKOUT to the "
        "coordinated Sandboxy checkout"
    )
    if required:
        pytest.fail(
            reason
            + " (OPENADA_REQUIRE_SANDBOXY_COMPAT=1 makes this a release failure)"
        )
    pytest.skip(reason)


def _load_measurement_guard() -> ModuleType:
    measurement_guard = _require_measurement_guard()
    spec = importlib.util.spec_from_file_location(
        "sandboxy_measurement_guard_acceptance",
        measurement_guard,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sandboxy_ledger_compatibility_checkout_is_available() -> None:
    assert _require_measurement_guard().is_file()


def test_relative_sandboxy_checkout_is_resolved_from_repository_root() -> None:
    assert _sandboxy_checkout("../sandboxy") == (
        REPO_ROOT.parent / "sandboxy"
    ).resolve()


def test_required_sandboxy_compatibility_does_not_skip(tmp_path: Path) -> None:
    missing = tmp_path / "missing-sandboxy/sandbox/measurement_guard.py"
    with pytest.raises(pytest.fail.Exception, match="release failure"):
        _require_measurement_guard(missing, required=True)


def test_optional_sandboxy_compatibility_skips_loudly(tmp_path: Path) -> None:
    missing = tmp_path / "missing-sandboxy/sandbox/measurement_guard.py"
    with pytest.raises(pytest.skip.Exception, match="SANDBOXY_CHECKOUT"):
        _require_measurement_guard(missing, required=False)


def test_retained_gain_chain_backs_one_unsealed_typed_result(ihp_gain_run):
    payload, output_dir = ihp_gain_run
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]

    measurement_guard = _load_measurement_guard()
    entries = [
        (str(path.relative_to(output_dir)), path.read_bytes())
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    ]
    ledger = measurement_guard.scan_artifacts(entries)

    assert ledger["backed"] is True, ledger
    assert ledger["measurements"] == [
        {
            "path": (
                "analyses/ac_gain/measurements/"
                "low_frequency_gain.result.json"
            ),
            "operation": "result.transfer.measure",
            "measurement_id": "low_frequency_gain",
            "status": "measured",
            "backed": True,
            "reason": None,
        }
    ]
    assert ledger["untyped_simulator_outputs"] == []
    assert len(ledger["typed_results"]) == 1, ledger
    typed_result = ledger["typed_results"][0]
    assert typed_result["schema"] == "sandboxy.typed-result/v1"
    assert typed_result["measurement_id"] == "low_frequency_gain"
    assert typed_result["kind"] == "low_frequency_gain_db"
    assert typed_result["unit"] == "dB"
    assert typed_result["origin"] == "simulated"
    assert typed_result["attestation"] == "unsealed"
    assert "backed" not in typed_result
    assert (
        typed_result["digest_consistency"]
        == "claimed_raw_lineage_only"
    )
    assert typed_result["analysis"] == "ac"
    assert typed_result["pdk_id"] == "ihp-sg13g2"
    assert typed_result["corner"] == "mos_tt"
    assert -8.0 <= typed_result["value"] <= 0.0


def test_element_current_observation_is_the_exact_retained_current_set(
    tmp_path: Path,
) -> None:
    if not acceptance_assets_available():
        pytest.skip("the real IHP PDK and SourceFollower acceptance bundle are required")
    specification = gain_spec()
    specification["id"] = "sf_ac_input_current"
    specification["observations"] = [
        {
            "id": "input_current",
            "analysis_id": "ac_gain",
            "quantity": {"kind": "element_current", "element": "V_IN"},
            "component": "real",
        }
    ]
    specification["measurements"] = []
    spec_path = tmp_path / "current.experiment.json"
    spec_path.write_text(
        json.dumps(specification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "evidence"

    payload = run_experiment(
        spec_path,
        output_dir,
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
        timeout=300.0,
    )

    assert payload["execution"]["status"] == "completed", payload["diagnostics"]
    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]
    simulation = json.loads(
        (
            output_dir
            / "analyses"
            / "ac_gain"
            / "simulation"
            / "simulate.result.json"
        ).read_text(encoding="utf-8")
    )
    binding = simulation["data"]["extensions"]["org.openada.pdk-binding"]
    assert binding["saved_nets"] == []
    assert binding["retained_current_vectors"] == ["i(v_in)"]
    assert binding["current_retention"] == "explicit"

    bound_deck = (
        output_dir
        / "analyses"
        / "ac_gain"
        / "simulation"
        / "decks"
        / "run.spice"
    ).read_text(encoding="utf-8")
    assert ".SAVE i(v_in)" in bound_deck
    assert "write run.raw i(v_in)" in bound_deck

    extraction = json.loads(
        (
            output_dir / "analyses" / "ac_gain" / "extract.result.json"
        ).read_text(encoding="utf-8")
    )
    signals = extraction["data"]["extraction"]["series"]["signals"]
    assert [signal["name"] for signal in signals] == ["input_current"]
    assert signals[0]["unit"] == "A"
    assert signals[0]["values"]
    assert all(math.isfinite(value) for value in signals[0]["values"])
