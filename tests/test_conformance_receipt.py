from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from openada import conformance_receipt as receipt_module
from openada.conformance_receipt import (
    ConformanceReceiptError,
    RECEIPT_SCHEMA,
    SIGNATURE_DOMAIN,
    VerifiedTypedEvidence,
    typed_evidence_simulation_context_sha256,
    verify_typed_evidence_receipt,
)
from openada.contract import result, static_execution, tool_record
from openada.operations import extract_result_series, measure_result


SIMULATION_REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EXTRACTION_REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
MEASUREMENT_REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SUBJECT_SHA256 = hashlib.sha256(b"frozen assignment and claim manifest").hexdigest()


def _raw_body() -> bytes:
    variables = "\t0\ttime\ttime\n\t1\tv(out)\tvoltage\n"
    header = (
        "Title: signed receipt fixture\n"
        "Date: fixture\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        "No. Variables: 2\n"
        "No. Points: 3\n"
        "Variables:\n"
        f"{variables}"
        "Binary:\n"
    ).encode("ascii")
    values = (0.0, 0.0, 1e-6, 0.5, 2e-6, 1.0)
    return header + struct.pack("=6d", *values)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _file_record(path: Path, *, kind: str, role: str) -> dict[str, object]:
    body = path.read_bytes()
    return {
        "kind": kind,
        "role": role,
        "path": str(path),
        "exists": True,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _simulation(
    raw_path: Path,
    raw_body: bytes,
    *,
    deck_path: Path,
    log_path: Path,
    model_path: Path,
) -> dict:
    raw_record = {
        "kind": "ngspice-raw",
        "role": "simulation.result",
        "path": str(raw_path),
        "exists": True,
        "bytes": len(raw_body),
        "sha256": hashlib.sha256(raw_body).hexdigest(),
    }
    log_record = _file_record(
        log_path,
        kind="simulation-log",
        role="simulation.log",
    )
    analysis = {
        "type": "tran",
        "step_s": 1e-6,
        "stop_s": 2e-6,
        "extensions": {},
    }
    return result(
        "simulate",
        tool=tool_record(
            "ngspice",
            path="/usr/bin/ngspice",
            version="ngspice-45.2",
        ),
        execution=static_execution("completed"),
        engineering_status="pass",
        summary="The fixture produced exact native evidence.",
        inputs=[
            _file_record(
                deck_path,
                kind="spice-netlist",
                role="simulation.deck",
            ),
            _file_record(
                model_path,
                kind="spice-model-library",
                role="model-library",
            ),
        ],
        artifacts=[log_record, raw_record],
        data={
            "protocol": {
                "request_id": SIMULATION_REQUEST_ID,
                "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
                "assertion_profile": (
                    "openada.assertion/simulation.evidence.valid/v1alpha1"
                ),
                "driver_id": "org.openada.driver.ngspice",
                "driver_version": "0.4.0",
            },
            "analysis": {
                "type": "tran",
                "completion": "completed",
                "convergence": "converged",
                "point_count": 3,
                "dependent_variable_count": 1,
                "finite_value_count": 3,
                "extensions": {},
            },
            "evidence": {
                "request_binding": "exact",
                "freshness": "fresh",
                "structure": "valid",
                "artifact_roles_present": [
                    "simulation.log",
                    "simulation.result",
                ],
                "provenance": "bounded",
                "provenance_limitations": ["fixture runtime"],
                "extensions": {},
            },
            "extensions": {
                "org.openada": {
                    "backend": "ngspice",
                    "parameters": {
                        "analysis": analysis,
                        "extensions": {},
                    },
                    "native_data": {},
                    "native_diagnostics": [],
                    "configuration": [
                        {
                            "role": "spice-model-library",
                            "path": str(model_path),
                            "bytes": model_path.stat().st_size,
                            "sha256": hashlib.sha256(
                                model_path.read_bytes()
                            ).hexdigest(),
                            "identity": "content-digest",
                        }
                    ],
                }
            },
        },
    )


def _raw_public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _seal(
    unsigned: dict,
    private_key: Ed25519PrivateKey,
) -> dict:
    public_key = _raw_public_key(private_key)
    canonical = json.dumps(
        unsigned,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        **unsigned,
        "seal": {
            "algorithm": "ed25519",
            "key_id": hashlib.sha256(public_key).hexdigest(),
            "signature_hex": private_key.sign(
                SIGNATURE_DOMAIN + canonical
            ).hex(),
        },
    }


def _chain(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    *,
    job: str = "scheduler/job-0001/attempt-1",
    subject_sha256: str = SUBJECT_SHA256,
) -> tuple[dict[str, Path], bytes]:
    deck_path = tmp_path / "fixture.spice"
    deck_path.write_text(
        "Signed receipt fixture\nVout out 0 1\n.tran 1u 2u\n.end\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "analysis.log"
    log_path.write_text(
        "ngspice-45.2\nTransient analysis completed\n",
        encoding="utf-8",
    )
    model_path = tmp_path / "fixture.models"
    model_path.write_text(
        ".model fixture resistor r=1\n",
        encoding="utf-8",
    )
    raw_path = tmp_path / "analysis.raw"
    raw_body = _raw_body()
    raw_path.write_bytes(raw_body)
    simulation = _simulation(
        raw_path,
        raw_body,
        deck_path=deck_path,
        log_path=log_path,
        model_path=model_path,
    )
    simulation_path = tmp_path / "simulation.json"
    _write_json(simulation_path, simulation)

    selection = {
        "selectors": [
            {
                "native_name": "v(out)",
                "output_name": "v(out)",
                "unit": "V",
                "component": "real",
            }
        ],
        "conditions": [
            {"name": "temperature", "value": 27, "unit": "degC"}
        ],
        "extensions": {},
    }
    selection_path = tmp_path / "selection.json"
    _write_json(selection_path, selection)
    extraction = extract_result_series(
        simulation,
        raw_path,
        selection["selectors"],
        conditions=selection["conditions"],
        request_id=EXTRACTION_REQUEST_ID,
    )
    assert extraction["engineering"]["status"] == "pass"
    extraction_path = tmp_path / "extraction.json"
    _write_json(extraction_path, extraction)

    measurement_request = {
        "measurement_id": "output.maximum",
        "kind": "maximum",
        "signal": "v(out)",
        "parameters": {},
        "extensions": {},
    }
    measurement_request_path = tmp_path / "measurement-request.json"
    _write_json(measurement_request_path, measurement_request)
    measurement = measure_result(
        extraction["data"]["extraction"]["series"],
        measurement_request,
        request_id=MEASUREMENT_REQUEST_ID,
    )
    assert measurement["engineering"]["status"] == "pass"
    measurement_path = tmp_path / "measurement.json"
    _write_json(measurement_path, measurement)

    paths = {
        "simulation_envelope": simulation_path,
        "simulation_artifact": raw_path,
        "selection_request": selection_path,
        "extraction_envelope": extraction_path,
        "measurement_request": measurement_request_path,
        "measurement_envelope": measurement_path,
    }
    simulation_files = []
    for section in ("inputs", "artifacts"):
        for index, record in enumerate(simulation[section]):
            if record["role"] == "simulation.result":
                continue
            simulation_files.append(
                {
                    "section": section,
                    "index": index,
                    "kind": record["kind"],
                    "role": record["role"],
                    "path": record["path"],
                    "bytes": record["bytes"],
                    "sha256": record["sha256"],
                }
            )
    unsigned = {
        "schema": RECEIPT_SCHEMA,
        "job": {
            "instance": job,
            "subject_sha256": subject_sha256,
        },
        "evidence": {
            **{
                role: {
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for role, path in paths.items()
            },
            "simulation_files": simulation_files,
        },
        "extensions": {},
    }
    receipt_path = tmp_path / "receipt.json"
    _write_json(receipt_path, _seal(unsigned, private_key))
    paths["receipt"] = receipt_path
    paths["simulation_log"] = log_path
    paths["simulation_deck"] = deck_path
    paths["simulation_model"] = model_path
    return paths, _raw_public_key(private_key)


def _verify(
    paths: dict[str, Path],
    public_key: bytes,
    *,
    job: str = "scheduler/job-0001/attempt-1",
    subject_sha256: str = SUBJECT_SHA256,
    simulation_context_sha256: str | None = None,
    selection_request_sha256: str | None = None,
    measurement_request_sha256: str | None = None,
) -> VerifiedTypedEvidence:
    if simulation_context_sha256 is None:
        simulation_context_sha256 = typed_evidence_simulation_context_sha256(
            json.loads(paths["simulation_envelope"].read_text(encoding="utf-8"))
        )
    if selection_request_sha256 is None:
        selection_request_sha256 = hashlib.sha256(
            paths["selection_request"].read_bytes()
        ).hexdigest()
    if measurement_request_sha256 is None:
        measurement_request_sha256 = hashlib.sha256(
            paths["measurement_request"].read_bytes()
        ).hexdigest()
    return verify_typed_evidence_receipt(
        paths["receipt"],
        paths["simulation_envelope"],
        paths["simulation_artifact"],
        paths["selection_request"],
        paths["extraction_envelope"],
        paths["measurement_request"],
        paths["measurement_envelope"],
        expected_job_instance=job,
        expected_subject_sha256=subject_sha256,
        expected_simulation_context_sha256=simulation_context_sha256,
        expected_selection_request_sha256=selection_request_sha256,
        expected_measurement_request_sha256=measurement_request_sha256,
        pinned_public_key=public_key,
    )


def _reseal_evidence_file(
    paths: dict[str, Path],
    private_key: Ed25519PrivateKey,
    role: str,
) -> None:
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    unsigned = deepcopy(receipt)
    unsigned.pop("seal")
    body = paths[role].read_bytes()
    unsigned["evidence"][role] = {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    _write_json(paths["receipt"], _seal(unsigned, private_key))


def test_scheduler_sealed_complete_chain_verifies(tmp_path: Path) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )

    verified = _verify(paths, trusted_public_key)

    assert verified.measurement_id == "output.maximum"
    assert verified.value == 1.0
    assert verified.unit == "V"
    assert verified.measurement_envelope_bytes == paths[
        "measurement_envelope"
    ].read_bytes()
    assert verified.selection_request_sha256 == hashlib.sha256(
        paths["selection_request"].read_bytes()
    ).hexdigest()


def test_self_authored_consistent_envelopes_cannot_choose_the_trust_key(
    tmp_path: Path,
) -> None:
    attacker_key = Ed25519PrivateKey.generate()
    paths, _attacker_public_key = _chain(tmp_path, attacker_key)
    # The agent can hand-edit both envelopes and recompute every ordinary
    # digest. It still cannot choose the out-of-band scheduler key.
    for role in ("simulation_envelope", "measurement_envelope"):
        envelope = json.loads(paths[role].read_text(encoding="utf-8"))
        envelope["engineering"]["summary"] = "Agent-authored but self-consistent."
        _write_json(paths[role], envelope)
        _reseal_evidence_file(paths, attacker_key, role)
    trusted_public_key = _raw_public_key(Ed25519PrivateKey.generate())

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.key.mismatch"


def test_valid_receipt_from_one_job_cannot_be_laundered_into_another(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
        job="scheduler/job-A/attempt-1",
    )

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(
            paths,
            trusted_public_key,
            job="scheduler/job-B/attempt-1",
        )

    assert caught.value.code == "receipt.job.mismatch"


def test_stale_raw_bytes_cannot_reuse_the_sealed_declared_digest(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    stale = bytearray(paths["simulation_artifact"].read_bytes())
    stale[-1] ^= 1
    paths["simulation_artifact"].write_bytes(stale)

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.evidence.mismatch"


def test_path_replacement_between_open_and_digest_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    raw_path = paths["simulation_artifact"]
    replacement = tmp_path / "replacement.raw"
    replacement.write_bytes(raw_path.read_bytes())
    raw_inode = raw_path.stat().st_ino
    original_read = receipt_module.os.read
    swapped = False
    descriptor_count = len(list(Path("/proc/self/fd").iterdir()))

    def swapping_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        body = original_read(descriptor, count)
        if (
            not swapped
            and body
            and os.fstat(descriptor).st_ino == raw_inode
        ):
            os.replace(replacement, raw_path)
            swapped = True
        return body

    monkeypatch.setattr(receipt_module.os, "read", swapping_read)

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert swapped
    assert caught.value.code == "receipt.file.unstable"
    assert len(list(Path("/proc/self/fd").iterdir())) == descriptor_count


def test_trusted_seal_still_refuses_a_recomputed_handwritten_measurement(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    forged = json.loads(paths["measurement_envelope"].read_text(encoding="utf-8"))
    forged["data"]["measurement"]["value"] = 9001.0
    paths["measurement_envelope"].write_text(
        json.dumps(forged, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _reseal_evidence_file(paths, private_key, "measurement_envelope")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.chain.replay_mismatch"


def test_boolean_cannot_impersonate_the_replayed_numeric_scalar(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    forged = json.loads(paths["measurement_envelope"].read_text(encoding="utf-8"))
    forged["data"]["measurement"]["value"] = True
    _write_json(paths["measurement_envelope"], forged)
    _reseal_evidence_file(paths, private_key, "measurement_envelope")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.chain.replay_mismatch"


def test_signed_base_envelope_with_malformed_protocol_is_bounded_refusal(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    malformed = json.loads(paths["extraction_envelope"].read_text(encoding="utf-8"))
    malformed["data"]["protocol"] = 1
    _write_json(paths["extraction_envelope"], malformed)
    _reseal_evidence_file(paths, private_key, "extraction_envelope")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.chain.invalid"


def test_scheduler_seal_must_cover_every_simulation_input_and_log(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    paths["simulation_log"].unlink()

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.file.invalid"


def test_same_job_cannot_substitute_a_different_claim_request(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(
            paths,
            trusted_public_key,
            measurement_request_sha256=hashlib.sha256(
                b"a different scheduler-pinned claim request"
            ).hexdigest(),
        )

    assert caught.value.code == "receipt.claim.mismatch"


def test_same_job_cannot_substitute_a_different_vector_or_condition(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    expected_selection_sha256 = hashlib.sha256(
        paths["selection_request"].read_bytes()
    ).hexdigest()
    selection = json.loads(paths["selection_request"].read_text(encoding="utf-8"))
    selection["conditions"] = [
        {"name": "temperature", "value": 999, "unit": "degC"},
        {"name": "corner", "value": "malicious", "unit": "1"},
    ]
    _write_json(paths["selection_request"], selection)
    _reseal_evidence_file(paths, private_key, "selection_request")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(
            paths,
            trusted_public_key,
            selection_request_sha256=expected_selection_sha256,
        )

    assert caught.value.code == "receipt.experiment.mismatch"


def test_configuration_reference_cannot_escape_the_captured_inputs(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    expected_context = typed_evidence_simulation_context_sha256(
        json.loads(paths["simulation_envelope"].read_text(encoding="utf-8"))
    )
    simulation = json.loads(
        paths["simulation_envelope"].read_text(encoding="utf-8")
    )
    simulation["data"]["extensions"]["org.openada"]["configuration"] = [
        {
            "role": "spice-model-library",
            "path": "/definitely/not/the/sealed/input.spice",
            "sha256": "0" * 64,
            "bytes": 1,
            "identity": "content-digest",
        }
    ]
    _write_json(paths["simulation_envelope"], simulation)
    _reseal_evidence_file(paths, private_key, "simulation_envelope")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(
            paths,
            trusted_public_key,
            simulation_context_sha256=expected_context,
        )

    assert caught.value.code == "receipt.chain.invalid"


def test_pdk_snapshot_manifest_aggregates_transitive_configuration(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "simulation.raw"
    deck_path = tmp_path / "run.spice"
    log_path = tmp_path / "simulation.log"
    model_path = tmp_path / "corner.spice"
    parser_path = tmp_path / "parser-only.spice"
    snapshot_path = tmp_path / "openada-pdk-snapshot.json"
    deck_path.write_text("* deck\n.end\n", encoding="utf-8")
    log_path.write_text("completed\n", encoding="utf-8")
    model_path.write_text(".model n nmos\n", encoding="utf-8")
    parser_path.write_text("* parser transport\n", encoding="utf-8")
    snapshot_path.write_text('{"schema":"openada.pdk-snapshot/v1"}\n')

    simulation = _simulation(
        raw_path,
        _raw_body(),
        deck_path=deck_path,
        log_path=log_path,
        model_path=model_path,
    )
    deck_record = simulation["inputs"][0]
    snapshot_record = _file_record(
        snapshot_path,
        kind="pdk-snapshot-manifest",
        role="pdk.snapshot",
    )
    corner_record = _file_record(
        model_path,
        kind="spice-model-library",
        role="pdk.corner-library",
    )
    parser_record = _file_record(
        parser_path,
        kind="spice-model-library",
        role="pdk.parser-library",
    )
    simulation["inputs"] = [
        deck_record,
        snapshot_record,
        corner_record,
        parser_record,
    ]
    simulation["data"]["extensions"]["org.openada"]["configuration"] = [
        {
            "role": "pdk",
            "path": snapshot_record["path"],
            "bytes": snapshot_record["bytes"],
            "sha256": snapshot_record["sha256"],
            "identity": "content-digest",
        }
    ]

    context = typed_evidence_simulation_context_sha256(simulation)

    assert len(context) == 64


def test_complete_pdk_roster_above_legacy_receipt_bounds_verifies(
    tmp_path: Path,
) -> None:
    """A Sky130-scale exact roster fits without allocating large collateral."""

    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    simulation = json.loads(
        paths["simulation_envelope"].read_text(encoding="utf-8")
    )
    parser_record = {
        **simulation["inputs"][1],
        "role": "pdk.parser-library",
    }
    # Reusing one exact immutable fixture file keeps the test small while the
    # signed roster itself exercises both historical gates: 128 records and a
    # 64 KiB receipt. A real sky130A tt capture currently carries 319 files.
    simulation["inputs"].extend(
        dict(parser_record) for _index in range(320)
    )
    _write_json(paths["simulation_envelope"], simulation)

    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    unsigned = deepcopy(receipt)
    unsigned.pop("seal")
    simulation_body = paths["simulation_envelope"].read_bytes()
    unsigned["evidence"]["simulation_envelope"] = {
        "bytes": len(simulation_body),
        "sha256": hashlib.sha256(simulation_body).hexdigest(),
    }
    simulation_files = []
    for section in ("inputs", "artifacts"):
        for index, record in enumerate(simulation[section]):
            if record["role"] == "simulation.result":
                continue
            simulation_files.append(
                {
                    "section": section,
                    "index": index,
                    "kind": record["kind"],
                    "role": record["role"],
                    "path": record["path"],
                    "bytes": record["bytes"],
                    "sha256": record["sha256"],
                }
            )
    unsigned["evidence"]["simulation_files"] = simulation_files
    _write_json(paths["receipt"], _seal(unsigned, private_key))

    assert len(simulation_files) > 128
    assert paths["receipt"].stat().st_size > 64 * 1024
    assert (
        receipt_module.MAX_SIMULATION_FILE_BYTES
        == receipt_module.MAX_PDK_FILE_BYTES
    )
    assert receipt_module.MAX_TOTAL_EVIDENCE_BYTES >= (
        receipt_module.MAX_PDK_SNAPSHOT_BYTES
        + receipt_module.MAX_RAW_EVIDENCE_BYTES
        + 8 * receipt_module.MAX_JSON_EVIDENCE_BYTES
    )
    verified = _verify(paths, trusted_public_key)
    assert verified.measurement_id == "output.maximum"


def test_tool_identity_cannot_diverge_from_the_pinned_simulation_context(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    expected_context = typed_evidence_simulation_context_sha256(
        json.loads(paths["simulation_envelope"].read_text(encoding="utf-8"))
    )
    simulation = json.loads(
        paths["simulation_envelope"].read_text(encoding="utf-8")
    )
    simulation["tool"]["path"] = "/tmp/fake-ngspice"
    simulation["tool"]["version"] = "ngspice-999"
    _write_json(paths["simulation_envelope"], simulation)
    _reseal_evidence_file(paths, private_key, "simulation_envelope")

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(
            paths,
            trusted_public_key,
            simulation_context_sha256=expected_context,
        )

    assert caught.value.code == "receipt.experiment.mismatch"


def test_symlinked_ancestor_is_refused_before_it_can_toggle_targets(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (alternate / "selection.json").write_bytes(
        paths["selection_request"].read_bytes()
    )
    linked = tmp_path / "linked"
    linked.symlink_to(alternate, target_is_directory=True)
    supplied_selection = linked / "selection.json"

    with pytest.raises(ConformanceReceiptError) as caught:
        verify_typed_evidence_receipt(
            paths["receipt"],
            paths["simulation_envelope"],
            paths["simulation_artifact"],
            supplied_selection,
            paths["extraction_envelope"],
            paths["measurement_request"],
            paths["measurement_envelope"],
            expected_job_instance="scheduler/job-0001/attempt-1",
            expected_subject_sha256=SUBJECT_SHA256,
            expected_simulation_context_sha256=(
                typed_evidence_simulation_context_sha256(
                    json.loads(
                        paths["simulation_envelope"].read_text(encoding="utf-8")
                    )
                )
            ),
            expected_selection_request_sha256=hashlib.sha256(
                paths["selection_request"].read_bytes()
            ).hexdigest(),
            expected_measurement_request_sha256=hashlib.sha256(
                paths["measurement_request"].read_bytes()
            ).hexdigest(),
            pinned_public_key=trusted_public_key,
        )

    assert caught.value.code == "receipt.file.invalid"


def test_control_character_in_signed_file_path_is_bounded_refusal(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    paths, trusted_public_key = _chain(tmp_path, private_key)
    simulation = json.loads(
        paths["simulation_envelope"].read_text(encoding="utf-8")
    )
    simulation["inputs"][0]["path"] = "/tmp/bad\u0000path"
    _write_json(paths["simulation_envelope"], simulation)
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    unsigned = deepcopy(receipt)
    unsigned.pop("seal")
    body = paths["simulation_envelope"].read_bytes()
    unsigned["evidence"]["simulation_envelope"] = {
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    unsigned["evidence"]["simulation_files"][0]["path"] = "/tmp/bad\u0000path"
    _write_json(paths["receipt"], _seal(unsigned, private_key))

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.structure.invalid"


def test_deep_unauthenticated_json_is_refused_without_recursion_escape(
    tmp_path: Path,
) -> None:
    paths, trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    paths["receipt"].write_text(
        '{"nested":' + "[" * 1_500 + "0" + "]" * 1_500 + "}",
        encoding="utf-8",
    )

    with pytest.raises(ConformanceReceiptError) as caught:
        _verify(paths, trusted_public_key)

    assert caught.value.code == "receipt.json.invalid"


def test_public_context_digest_bounds_recursive_python_values(
    tmp_path: Path,
) -> None:
    paths, _trusted_public_key = _chain(
        tmp_path,
        Ed25519PrivateKey.generate(),
    )
    simulation = json.loads(
        paths["simulation_envelope"].read_text(encoding="utf-8")
    )
    nested: object = 0
    for _index in range(2_000):
        nested = [nested]
    simulation["data"]["extensions"]["org.openada"]["parameters"][
        "extensions"
    ] = {"nested": nested}

    with pytest.raises(ConformanceReceiptError) as caught:
        typed_evidence_simulation_context_sha256(simulation)

    assert caught.value.code == "receipt.chain.invalid"
