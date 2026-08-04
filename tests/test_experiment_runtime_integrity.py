from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from openada.contract import file_record, result, static_execution
import openada.operations.experiment as experiment_operation


ORDINARY_PROFILE = "openada.operation/result.measure/v1alpha2"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _measurement(identifier: str, *, expected_unit: str = "V"):
    request = {
        "measurement_id": identifier,
        "kind": "sample_at",
        "signal": "output_v",
        "parameters": {"at": {"value": 0.0, "unit": "s"}},
        "extensions": {},
    }
    body = experiment_operation._json_bytes(request)
    return experiment_operation.Measurement(
        identifier=identifier,
        analysis_id="edge",
        operation_profile=ORDINARY_PROFILE,
        request=request,
        request_bytes=body,
        request_raw_sha256=_sha256(body),
        request_canonical_sha256=experiment_operation._canonical_sha256(
            request
        ),
        expected_unit=expected_unit,
    )


def _prepared(
    *,
    measurements=(),
    derivations=(),
):
    portable = "* portable\n.END\n"
    bound = b"* bound\n.END\n"
    base = "* base\n.END\n"
    analysis = experiment_operation.Analysis(
        identifier="edge",
        kind="tran",
        document={"id": "edge", "kind": "tran", "step": "1n", "stop": "2n"},
        card=".TRAN 1n 2n",
        axis_unit="s",
        estimated_points=3,
    )
    observation = experiment_operation.Observation(
        identifier="output_v",
        analysis_id="edge",
        kind="node_voltage",
        native_name="v(out)",
        component="real",
        unit="V",
        net="OUT",
    )
    run = experiment_operation.PreparedRun(
        analysis=analysis,
        observations=(observation,),
        saved_nets=("OUT",),
        retained_current_sources=(),
        portable_deck=portable,
        portable_sha256=_sha256(portable.encode()),
        bound_deck_sha256=_sha256(bound),
    )
    bundle = SimpleNamespace(
        input_records=[],
        bundle_digests={
            "descriptor_sha256": "1" * 64,
            "source_sha256": "2" * 64,
            "view_sha256": "3" * 64,
            "netlist_sha256": "4" * 64,
            "cdl_sha256": "5" * 64,
        },
    )
    resolved = SimpleNamespace(
        pdk_id="fake-pdk",
        corner="tt",
        binding=SimpleNamespace(simulation_temperature_c="27"),
        closure_root_sha256="8" * 64,
        snapshot_root_sha256="9" * 64,
        closure_records=(),
    )
    return SimpleNamespace(
        spec_path=Path("/unused/spec.json"),
        spec_bytes=b'{"schema":"simra.experiment/v1"}\n',
        spec_document={},
        spec_raw_sha256=_sha256(
            b'{"schema":"simra.experiment/v1"}\n'
        ),
        spec_canonical_sha256="6" * 64,
        identifier="runtime_integrity",
        bundle=bundle,
        resolved_pdk=resolved,
        analyses=(analysis,),
        observations=(observation,),
        measurements=tuple(measurements),
        derivations=tuple(derivations),
        base_deck=base,
        base_deck_sha256=_sha256(base.encode()),
        runs=(run,),
    ), bound


def _passing_simulator(bound: bytes):
    def simulate(_netlist, output_dir, **kwargs):
        output = Path(output_dir)
        deck_path = output / "decks" / "run.spice"
        deck_path.parent.mkdir(parents=True, exist_ok=True)
        deck_path.write_bytes(bound)
        raw_path = output / "run.raw"
        raw_path.write_text(
            "Title: fake\n"
            "Variables:\n"
            "\t0\ttime\ttime\n"
            "\t1\tv(out)\tvoltage\n"
            "Binary:\n",
            encoding="utf-8",
        )
        envelope = result(
            "simulate",
            tool=None,
            execution=static_execution("completed"),
            engineering_status="pass",
            summary="Synthetic passing simulation.",
            inputs=[
                file_record(
                    deck_path,
                    kind="spice-netlist",
                    role="simulation.deck",
                )
            ],
            artifacts=[
                file_record(
                    raw_path,
                    kind="ngspice-raw",
                    role="simulation.result",
                )
            ],
            data={
                "protocol": {
                    "request_id": kwargs["request_id"],
                    "operation_profile": (
                        "openada.operation/circuit.simulate/v1alpha2"
                    ),
                }
            },
        )
        (output / "simulate.result.json").write_text(
            json.dumps(envelope) + "\n",
            encoding="utf-8",
        )
        return envelope

    return simulate


def _extraction_payload(
    simulation,
    artifact_path,
    selectors,
    *,
    conditions,
    request_id,
    engineering_status="pass",
):
    raw = simulation["artifacts"][0]
    request_sha256 = experiment_operation._canonical_sha256(
        {
            "simulation": {
                "request_id": simulation["data"]["protocol"]["request_id"],
                "artifact_sha256": raw["sha256"],
            },
            "selectors": list(selectors),
            "conditions": list(conditions),
        }
    )
    return result(
        "result.series.extract",
        tool=None,
        execution=static_execution("completed"),
        engineering_status=engineering_status,
        summary="Synthetic series extraction.",
        data={
            "protocol": {
                "request_id": request_id,
                "operation_profile": (
                    experiment_operation.EXTRACTION_OPERATION_PROFILE
                ),
            },
            "extraction": {
                "status": (
                    "extracted"
                    if engineering_status == "pass"
                    else "unknown"
                ),
                "request_sha256": request_sha256,
                "series": {
                    "source": {
                        "operation": (
                            experiment_operation.EXTRACTION_OPERATION_PROFILE
                        ),
                        "request_id": request_id,
                        "artifact_role": "measurement.source",
                        "artifact_sha256": "7" * 64,
                        "lineage": {
                            "operation": "circuit.simulate",
                            "request_id": (
                                simulation["data"]["protocol"]["request_id"]
                            ),
                            "artifact_role": "simulation.result",
                            "artifact_sha256": raw["sha256"],
                            "binding": "unverified",
                        },
                    },
                    "conditions": list(conditions),
                },
            },
        },
    )


def _measurement_payload(
    request,
    *,
    engineering_status="pass",
    unit="V",
    source=None,
):
    return result(
        "result.measure",
        tool=None,
        execution=static_execution("completed"),
        engineering_status=engineering_status,
        summary="Synthetic measurement.",
        data={
            "protocol": {
                "request_id": str(uuid.uuid4()),
                "operation_profile": ORDINARY_PROFILE,
            },
            "measurement": {
                "measurement_id": request["measurement_id"],
                "status": "measured",
                "request_sha256": (
                    experiment_operation._canonical_sha256(request)
                ),
                "value": 1.0,
                "unit": unit,
                "source": dict(source or {}),
            },
        },
    )


def _run(
    tmp_path,
    monkeypatch,
    *,
    prepared,
    simulate,
    extract,
    measure=None,
):
    monkeypatch.setattr(
        experiment_operation,
        "validate_experiment",
        lambda *args, **kwargs: (prepared, []),
    )
    monkeypatch.setattr(experiment_operation, "simulate", simulate)
    monkeypatch.setattr(
        experiment_operation,
        "extract_result_series",
        extract,
    )
    if measure is not None:
        monkeypatch.setattr(experiment_operation, "measure_result", measure)
    return experiment_operation.run_experiment(
        tmp_path / "unused.json",
        tmp_path / "evidence",
        discovery=SimpleNamespace(),
        pdk="fake-pdk",
        pdk_root=Path("/unused/pdk"),
    )


def test_missing_persisted_simulation_envelope_cannot_pass(
    tmp_path,
    monkeypatch,
):
    prepared, _bound = _prepared()

    def missing_simulation(*args, **kwargs):
        return {
            "execution": {"status": "completed"},
            "engineering": {"status": "pass"},
            "inputs": [],
            "artifacts": [],
            "diagnostics": [],
            "data": {"protocol": {"request_id": str(uuid.uuid4())}},
        }

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=missing_simulation,
        extract=lambda *args, **kwargs: pytest.fail(
            "missing simulation evidence reached extraction"
        ),
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["manifest"]["status"] == "fail"
    assert "experiment.result.missing" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize("tamper", ["profile", "request_id"])
def test_simulation_child_with_wrong_protocol_identity_cannot_pass(
    tamper,
    tmp_path,
    monkeypatch,
):
    prepared, bound = _prepared()
    prepared.runs = (
        replace(
            prepared.runs[0],
            observations=(),
            saved_nets=(),
        ),
    )
    prepared.observations = ()
    simulator = _passing_simulator(bound)

    def malformed_simulation(*args, **kwargs):
        envelope = simulator(*args, **kwargs)
        protocol = envelope["data"]["protocol"]
        if tamper == "profile":
            protocol["operation_profile"] = "attacker.operation/wrong/v1"
        else:
            protocol["request_id"] = str(uuid.uuid4())
        output = Path(args[1])
        (output / "simulate.result.json").write_text(
            json.dumps(envelope) + "\n",
            encoding="utf-8",
        )
        return envelope

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=malformed_simulation,
        extract=lambda *args, **kwargs: pytest.fail(
            "an incomplete simulation child reached extraction"
        ),
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["manifest"]["status"] == "fail"
    assert "experiment.result.envelope_invalid" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize(
    "omitted",
    [
        "schema",
        "operation",
        "tool",
        "execution",
        "engineering",
        "inputs",
        "artifacts",
        "diagnostics",
        "data",
        "provenance",
    ],
)
@pytest.mark.parametrize(
    "child",
    ["simulation", "extraction", "measurement"],
)
def test_passing_child_with_incomplete_base_envelope_cannot_pass(
    child,
    omitted,
    tmp_path,
    monkeypatch,
):
    measurements = (
        (_measurement("sample"),)
        if child == "measurement"
        else ()
    )
    prepared, bound = _prepared(measurements=measurements)
    simulator = _passing_simulator(bound)

    def simulate(*args, **kwargs):
        envelope = simulator(*args, **kwargs)
        if child == "simulation":
            envelope.pop(omitted)
            output = Path(args[1])
            (output / "simulate.result.json").write_text(
                json.dumps(envelope) + "\n",
                encoding="utf-8",
            )
        return envelope

    def extract(*args, **kwargs):
        envelope = _extraction_payload(*args, **kwargs)
        if child == "extraction":
            envelope.pop(omitted)
        return envelope

    def measure(_series, request, **kwargs):
        envelope = _measurement_payload(request)
        if child == "measurement":
            envelope.pop(omitted)
        return envelope

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=simulate,
        extract=extract,
        measure=measure if child == "measurement" else None,
    )

    manifest = payload["data"]["manifest"]
    assert payload["engineering"]["status"] == "fail"
    assert manifest["status"] == "fail"
    assert manifest["completeness"]["status"] == "fail"
    assert "experiment.result.envelope_invalid" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize(
    "child",
    ["simulation", "extraction", "measurement"],
)
def test_passing_child_with_wrong_operation_cannot_pass(
    child,
    tmp_path,
    monkeypatch,
):
    measurements = (
        (_measurement("sample"),)
        if child == "measurement"
        else ()
    )
    prepared, bound = _prepared(measurements=measurements)
    simulator = _passing_simulator(bound)

    def simulate(*args, **kwargs):
        envelope = simulator(*args, **kwargs)
        if child == "simulation":
            envelope["operation"] = "attacker.simulate"
            output = Path(args[1])
            (output / "simulate.result.json").write_text(
                json.dumps(envelope) + "\n",
                encoding="utf-8",
            )
        return envelope

    def extract(*args, **kwargs):
        envelope = _extraction_payload(*args, **kwargs)
        if child == "extraction":
            envelope["operation"] = "attacker.extract"
        return envelope

    def measure(_series, request, **kwargs):
        envelope = _measurement_payload(request)
        if child == "measurement":
            envelope["operation"] = "attacker.measure"
        return envelope

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=simulate,
        extract=extract,
        measure=measure if child == "measurement" else None,
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["manifest"]["status"] == "fail"
    assert "experiment.result.envelope_invalid" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize(
    "child",
    ["simulation", "extraction", "measurement"],
)
def test_diagnostic_free_failed_children_cannot_pass(
    child,
    tmp_path,
    monkeypatch,
):
    measurements = (_measurement("sample"),) if child == "measurement" else ()
    prepared, bound = _prepared(measurements=measurements)

    simulator = _passing_simulator(bound)

    def simulate(*args, **kwargs):
        envelope = simulator(*args, **kwargs)
        if child != "simulation":
            return envelope
        envelope["engineering"]["status"] = "fail"
        output = Path(args[1])
        (output / "simulate.result.json").write_text(
            json.dumps(envelope) + "\n",
            encoding="utf-8",
        )
        return envelope

    def extract(*args, **kwargs):
        return _extraction_payload(
            *args,
            **kwargs,
            engineering_status=(
                "fail" if child == "extraction" else "pass"
            ),
        )

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=simulate,
        extract=extract,
        measure=(
            lambda _series, request, **kwargs: _measurement_payload(
                request,
                engineering_status="fail",
            )
            if child == "measurement"
            else None
        ),
    )

    manifest = payload["data"]["manifest"]
    assert payload["engineering"]["status"] == "fail"
    assert manifest["status"] == "fail"
    assert manifest["completeness"]["status"] == "fail"
    assert "experiment.result.engineering_failed" in {
        item["code"] for item in payload["diagnostics"]
    }


def test_request_path_replacement_is_audited_and_operation_uses_capture(
    tmp_path,
    monkeypatch,
):
    declaration = _measurement("sample")
    prepared, bound = _prepared(measurements=(declaration,))
    original_writer = experiment_operation._write_captured_json
    observed_requests: list[dict[str, Any]] = []

    def replace_after_publication(path, body, *, role):
        record = original_writer(path, body, role=role)
        if role == "measurement.request":
            Path(path).write_bytes(b'{"measurement_id":"attacker"}\n')
        return record

    def measure(_series, request, **kwargs):
        observed_requests.append(dict(request))
        return _measurement_payload(request)

    monkeypatch.setattr(
        experiment_operation,
        "_write_captured_json",
        replace_after_publication,
    )
    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=_passing_simulator(bound),
        extract=_extraction_payload,
        measure=measure,
    )

    assert observed_requests == [declaration.request]
    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["manifest"]["completeness"]["evidence_audit"] == "fail"
    assert "experiment.evidence.collection_incomplete" in {
        item["code"] for item in payload["diagnostics"]
    }


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("failed", "experiment.derivation.parent_invalid"),
        ("wrong_unit", "experiment.derivation.unit_mismatch"),
    ],
)
def test_derivation_runtime_parent_invariants_refuse(
    mode,
    expected_code,
    tmp_path,
    monkeypatch,
):
    first = _measurement("first", expected_unit="s")
    second = _measurement("second", expected_unit="s")
    derivation = experiment_operation.Derivation(
        identifier="delay",
        analysis_id="edge",
        parents=("first", "second"),
    )
    prepared, bound = _prepared(
        measurements=(first, second),
        derivations=(derivation,),
    )

    def measure(_series, request, **kwargs):
        return _measurement_payload(
            request,
            engineering_status=("fail" if mode == "failed" else "pass"),
            unit=("V" if mode == "wrong_unit" else "s"),
        )

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=_passing_simulator(bound),
        extract=_extraction_payload,
        measure=measure,
    )

    assert payload["engineering"]["status"] == "fail"
    assert payload["data"]["manifest"]["status"] == "fail"
    assert expected_code in {
        item["code"] for item in payload["diagnostics"]
    }


def test_passing_derivation_records_complete_parent_digest_bindings(
    tmp_path,
    monkeypatch,
):
    first = _measurement("first", expected_unit="s")
    second = _measurement("second", expected_unit="s")
    derivation = experiment_operation.Derivation(
        identifier="delay",
        analysis_id="edge",
        parents=("first", "second"),
    )
    prepared, bound = _prepared(
        measurements=(first, second),
        derivations=(derivation,),
    )

    def measure(series, request, **kwargs):
        conditions = series["conditions"]
        source = {
            **series["source"],
            "series_sha256": series["source"]["artifact_sha256"],
            "conditions_sha256": (
                experiment_operation._canonical_sha256(conditions)
            ),
            "conditions": conditions,
        }
        return _measurement_payload(request, unit="s", source=source)

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=_passing_simulator(bound),
        extract=_extraction_payload,
        measure=measure,
    )

    assert payload["engineering"]["status"] == "pass", payload["diagnostics"]
    retained = payload["data"]["manifest"]["derivations"][0]
    assert retained["status"] == "pass"
    assert len(retained["parents"]) == 2
    required = {
        "result_sha256",
        "request_raw_sha256",
        "request_canonical_sha256",
        "extraction_result_sha256",
        "extraction_request_raw_sha256",
        "extraction_request_canonical_sha256",
        "series_sha256",
        "simulation_result_sha256",
        "raw_artifact_sha256",
    }
    for parent in retained["parents"]:
        assert required.issubset(parent)
        assert all(
            len(parent[name]) == 64
            for name in required
        )


@pytest.mark.parametrize(
    "tamper",
    ["profile", "request_digest", "raw_lineage", "conditions"],
)
def test_derivation_parent_profile_request_and_lineage_tampering_refuses(
    tamper,
    tmp_path,
    monkeypatch,
):
    first = _measurement("first", expected_unit="s")
    second = _measurement("second", expected_unit="s")
    derivation = experiment_operation.Derivation(
        identifier="delay",
        analysis_id="edge",
        parents=("first", "second"),
    )
    prepared, bound = _prepared(
        measurements=(first, second),
        derivations=(derivation,),
    )

    def measure(series, request, **kwargs):
        conditions = series["conditions"]
        source = {
            **series["source"],
            "series_sha256": series["source"]["artifact_sha256"],
            "conditions_sha256": (
                experiment_operation._canonical_sha256(conditions)
            ),
            "conditions": conditions,
        }
        payload = _measurement_payload(request, unit="s", source=source)
        if request["measurement_id"] != "first":
            return payload
        measured = payload["data"]["measurement"]
        if tamper == "profile":
            payload["data"]["protocol"]["operation_profile"] = "wrong-profile"
        elif tamper == "request_digest":
            measured["request_sha256"] = "0" * 64
        elif tamper == "raw_lineage":
            measured["source"]["lineage"]["artifact_sha256"] = "0" * 64
        else:
            changed = [
                {"name": "corner", "value": "attacker", "unit": "1"}
            ]
            measured["source"]["conditions"] = changed
            measured["source"]["conditions_sha256"] = (
                experiment_operation._canonical_sha256(changed)
            )
        return payload

    payload = _run(
        tmp_path,
        monkeypatch,
        prepared=prepared,
        simulate=_passing_simulator(bound),
        extract=_extraction_payload,
        measure=measure,
    )

    assert payload["engineering"]["status"] == "fail"
    assert "experiment.derivation.parent_invalid" in {
        item["code"] for item in payload["diagnostics"]
    }


def test_extraction_digest_binds_simulation_raw_selectors_and_conditions():
    request = {
        "selectors": [
            {
                "native_name": "v(out)",
                "output_name": "output_v",
                "unit": "V",
                "component": "real",
            }
        ],
        "conditions": [{"name": "corner", "value": "tt", "unit": "1"}],
        "extensions": {},
    }
    simulation = {"data": {"protocol": {"request_id": str(uuid.uuid4())}}}
    raw = {"sha256": "8" * 64}
    digest = experiment_operation._expected_extraction_request_sha(
        simulation,
        raw,
        request,
    )

    assert digest is not None
    assert digest != experiment_operation._expected_extraction_request_sha(
        simulation,
        {"sha256": "9" * 64},
        request,
    )
    changed = json.loads(json.dumps(request))
    changed["conditions"][0]["value"] = "ff"
    assert digest != experiment_operation._expected_extraction_request_sha(
        simulation,
        raw,
        changed,
    )
