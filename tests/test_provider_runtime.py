from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from openada.cli import main
from openada.provider_runtime import (
    MAX_MANIFEST_BYTES,
    MAX_PROVIDER_CONFIGURATION_BYTES,
    MAX_PROVIDER_TARGET_BYTES,
    ProviderRuntimeError,
    invoke_local_provider,
    load_provider_manifest,
    provider_manifest_issues,
    provider_request_issues,
    resolve_local_provider,
    validate_provider_result,
)


ROOT = Path(__file__).parents[1]
MANIFEST_TEMPLATE = json.loads(
    (ROOT / "conformance" / "driver-kit" / "driver-manifest.template.json").read_text(
        encoding="utf-8"
    )
)
REQUEST_TEMPLATE = json.loads(
    (ROOT / "conformance" / "driver-kit" / "request.template.json").read_text(
        encoding="utf-8"
    )
)


PROVIDER_SCRIPT = r'''#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode = sys.argv[1]
request = json.load(sys.stdin)
if mode == "timeout":
    time.sleep(2)
if mode == "stderr":
    sys.stderr.write("ambient provider log\n")
if mode == "over-limit":
    sys.stdout.write("x" * 100000)
    raise SystemExit(0)
if mode == "duplicate-json":
    sys.stdout.write('{"schema":"openada.result/v0alpha1","schema":"duplicate"}')
    raise SystemExit(0)

destination = Path(request["evidence_destination"]["locator"]["path"])
destination.mkdir(parents=True, exist_ok=True)
artifact_directory = destination
if mode == "outside-destination":
    artifact_directory = destination.parent / "outside-evidence"
    artifact_directory.mkdir(parents=True, exist_ok=True)
raw_path = artifact_directory / "simulation.raw"
log_path = artifact_directory / "simulation.log"
raw_path.write_bytes(b"synthetic raw evidence\n")
log_path.write_bytes(b"synthetic simulator transcript\n")

def file_record(path, kind, role):
    body = path.read_bytes()
    return {
        "kind": kind,
        "role": role,
        "path": str(path.resolve()),
        "exists": True,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }

target_path = Path(request["target"]["locator"]["path"])
if mode == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (destination / "child.pid").write_text(str(child.pid), encoding="ascii")
if mode == "mutate-entrypoint":
    Path(__file__).write_text("#!/usr/bin/env python3\n", encoding="utf-8")

driver_id = "org.example.openada.driver.local-test"
if mode == "wrong-identity":
    driver_id = "org.example.openada.driver.someone-else"
point_count = -1 if mode == "invalid-data" else 3
execution_status = "failed" if mode == "failed-execution" else "completed"
result_exit_code = 17 if mode == "result-exit17" else (7 if execution_status == "failed" else 0)
artifacts = [] if mode == "missing-artifacts" else [
    file_record(raw_path, "synthetic-raw", "simulation.result"),
    file_record(log_path, "synthetic-log", "simulation.log"),
]
payload = {
    "schema": "openada.result/v0alpha1",
    "operation": "simulate",
    "tool": None if mode == "null-tool" else {"name": "LocalTestSpice", "path": sys.executable, "version": "1.0"},
    "execution": {
        "status": execution_status,
        "exit_code": result_exit_code,
        "duration_ms": 1,
        "command": [] if mode == "empty-command" else ["local-test-spice"],
        "cwd": os.getcwd(),
    },
    "engineering": {"status": "pass", "summary": "Produced test evidence."},
    "inputs": [file_record(target_path, "spice-netlist", "simulation.input")],
    "artifacts": artifacts,
    "diagnostics": [],
    "data": {
        "protocol": {
            "request_id": request["request_id"],
            "operation_profile": request["operation_profile"],
            "assertion_profile": request["assertion_profile"],
            "driver_id": driver_id,
            "driver_version": "0.1.0",
        },
        "analysis": {
            "type": "ac" if mode == "wrong-analysis" else request["parameters"]["analysis"]["type"],
            "completion": "completed",
            "convergence": "converged",
            "point_count": point_count,
            "dependent_variable_count": 2,
            "finite_value_count": 6,
            "extensions": {},
        },
        "evidence": {
            "request_binding": "exact",
            "freshness": "fresh",
            "structure": "valid",
            "artifact_roles_present": ["simulation.result", "simulation.log"],
            "provenance": "bounded",
            "provenance_limitations": ["Synthetic provider fixture."],
            "extensions": {},
        },
        "extensions": {},
    },
    "provenance": {
        "openada_version": "external-test-provider",
        "created_at": "2026-07-15T00:00:00Z",
        "host": {"system": "test", "machine": "test", "python": "test"},
    },
}
if mode == "mutate-input":
    target_path.write_text("* provider changed the request input\n", encoding="utf-8")
json.dump(payload, sys.stdout, allow_nan=False)
if mode == "nonzero-exit":
    raise SystemExit(17)
'''


def _provider_script(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "external_provider.py"
    path.write_text(PROVIDER_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


def _manifest(tmp_path: Path, mode: str = "success") -> dict:
    manifest = deepcopy(MANIFEST_TEMPLATE)
    manifest["driver"].update(
        {
            "id": "org.example.openada.driver.local-test",
            "name": "Local test provider",
            "version": "0.1.0",
        }
    )
    manifest["transports"][0]["argv"] = [
        str(_provider_script(tmp_path)),
        mode,
    ]
    return manifest


def _request(tmp_path: Path) -> dict:
    request = deepcopy(REQUEST_TEMPLATE)
    target = (
        ROOT
        / "conformance"
        / "circuit-simulate-v0alpha2"
        / "fixtures"
        / "rc-transient.cir"
    )
    request["target"]["locator"]["path"] = str(target.resolve())
    request["target"]["locator"]["sha256"] = hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    request["evidence_destination"]["locator"]["path"] = str(
        (tmp_path / "evidence").resolve()
    )
    request["driver_selector"] = {
        "driver_id": "org.example.openada.driver.local-test",
        "driver_version": "0.1.0",
        "transport_id": "local-json-stdio",
        "required_features": [
            "openada.feature/simulation.analysis.tran/v1alpha1"
        ],
        "extensions": {},
    }
    return request


def test_template_manifest_and_request_pass_runtime_validation(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    request = _request(tmp_path)

    assert provider_manifest_issues(manifest) == ()
    assert provider_request_issues(request) == ()


def test_manifest_cross_reference_validation_rejects_false_maturity(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest["conformance_records"][0]["driver_version"] = "9.9.9"

    issues = provider_manifest_issues(manifest)

    assert any("targets driver version" in issue for issue in issues)
    assert any("lacks a matching passing conformance record" in issue for issue in issues)


def test_manifest_rejects_control_argv_and_partial_native_product_coverage(
    tmp_path: Path,
) -> None:
    control = _manifest(tmp_path)
    control["transports"][0]["argv"][0] += "\0hidden"
    assert any("control characters" in issue for issue in provider_manifest_issues(control))

    partial = _manifest(tmp_path)
    second_product = deepcopy(partial["native_products"][0])
    second_product["product_id"] = "org.example.eda.second-spice"
    second_product["name"] = "Second test SPICE"
    partial["native_products"].append(second_product)
    partial["capabilities"][0]["native_product_ids"].append(
        second_product["product_id"]
    )
    issues = provider_manifest_issues(partial)
    assert any("do not cover every advertised native product" in issue for issue in issues)


def test_manifest_loader_rejects_duplicate_keys_links_and_oversize(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema": 1, "schema": 2}', encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(duplicate)
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_MANIFEST_BYTES + 1)

    for path in (duplicate, linked, oversized):
        with pytest.raises(ProviderRuntimeError) as error:
            load_provider_manifest(path)
        assert error.value.code == "provider.manifest.invalid"


def test_resolver_requires_exact_selector_and_derived_analysis_feature(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    request = _request(tmp_path)
    del request["driver_selector"]

    with pytest.raises(ProviderRuntimeError) as missing:
        resolve_local_provider(manifest, request)
    assert missing.value.code == "provider.selection.required"

    request = _request(tmp_path)
    manifest["capabilities"][0]["features"] = []
    with pytest.raises(ProviderRuntimeError) as unsupported:
        resolve_local_provider(manifest, request)
    assert unsupported.value.code == "provider.resolution.none"


def test_resolver_rejects_discovered_and_ambiguous_capabilities(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    discovered = _manifest(tmp_path)
    discovered["capabilities"][0]["maturity"] = "discovered"
    with pytest.raises(ProviderRuntimeError) as unavailable:
        resolve_local_provider(discovered, request)
    assert unavailable.value.code == "provider.resolution.none"

    ambiguous = _manifest(tmp_path)
    second = deepcopy(ambiguous["transports"][0])
    second["id"] = "second-local-json-stdio"
    ambiguous["transports"].append(second)
    ambiguous["capabilities"][0]["transport_ids"].append(second["id"])
    del request["driver_selector"]["transport_id"]
    with pytest.raises(ProviderRuntimeError) as collision:
        resolve_local_provider(ambiguous, request)
    assert collision.value.code == "provider.resolution.ambiguous"


def test_request_semantics_and_profile_side_effect_mode_are_enforced(
    tmp_path: Path,
) -> None:
    invalid = _request(tmp_path)
    invalid["parameters"]["analysis"]["start_s"] = 2.0
    invalid["parameters"]["analysis"]["stop_s"] = 1.0
    issues = provider_request_issues(invalid)
    assert any("transient time controls" in issue for issue in issues)

    manifest = _manifest(tmp_path)
    manifest["capabilities"][0]["side_effect_modes"] = [
        "transactional-design-write"
    ]
    request = _request(tmp_path)
    request["execution_constraints"]["side_effects"] = (
        "transactional-design-write"
    )
    with pytest.raises(ProviderRuntimeError) as mismatch:
        resolve_local_provider(manifest, request)
    assert mismatch.value.code == "provider.resolution.none"


def test_unregistered_profile_is_rejected_before_provider_launch(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    request = _request(tmp_path)
    profile_id = "openada.operation/circuit.simulate/v1alpha1"
    manifest["capabilities"][0]["operation_profile"] = profile_id
    manifest["conformance_records"][0]["operation_profile"] = profile_id
    request["operation_profile"] = profile_id

    with pytest.raises(ProviderRuntimeError) as unsupported:
        resolve_local_provider(manifest, request)
    assert unsupported.value.code == "provider.profile.unsupported"


def test_external_local_provider_round_trip_returns_validated_result(
    tmp_path: Path,
) -> None:
    payload = invoke_local_provider(_manifest(tmp_path), _request(tmp_path))

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["protocol"] == {
        "request_id": REQUEST_TEMPLATE["request_id"],
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "driver_id": "org.example.openada.driver.local-test",
        "driver_version": "0.1.0",
    }


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("wrong-identity", "provider.result.identity_mismatch"),
        ("invalid-data", "provider.result.data_invalid"),
        ("stderr", "provider.transport.stderr"),
        ("duplicate-json", "provider.result.invalid"),
    ],
)
def test_provider_rejects_unbound_or_nonprotocol_results(
    tmp_path: Path,
    mode: str,
    code: str,
) -> None:
    with pytest.raises(ProviderRuntimeError) as error:
        invoke_local_provider(_manifest(tmp_path, mode), _request(tmp_path))

    assert error.value.code == code


def test_provider_enforces_timeout_and_stdout_bound(tmp_path: Path) -> None:
    timed_request = _request(tmp_path)
    timed_request["execution_constraints"]["timeout_ms"] = 20
    with pytest.raises(ProviderRuntimeError) as timeout:
        invoke_local_provider(_manifest(tmp_path, "timeout"), timed_request)
    assert timeout.value.code == "provider.transport.timed_out"

    with pytest.raises(ProviderRuntimeError) as over_limit:
        invoke_local_provider(
            _manifest(tmp_path, "over-limit"),
            _request(tmp_path),
            max_result_bytes=1_024,
        )
    assert over_limit.value.code == "provider.result.over_limit"


def test_provider_rejects_nonzero_transport_exit_and_entrypoint_mutation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProviderRuntimeError) as nonzero:
        invoke_local_provider(_manifest(tmp_path, "nonzero-exit"), _request(tmp_path))
    assert nonzero.value.code == "provider.transport.failed"

    with pytest.raises(ProviderRuntimeError) as changed:
        invoke_local_provider(
            _manifest(tmp_path / "mutation", "mutate-entrypoint"),
            _request(tmp_path / "mutation"),
        )
    assert changed.value.code == "provider.transport.identity_changed"


@pytest.mark.parametrize("mode", ["failed-execution", "missing-artifacts"])
def test_provider_rejects_false_conclusive_evidence(
    tmp_path: Path, mode: str
) -> None:
    with pytest.raises(ProviderRuntimeError) as invalid:
        invoke_local_provider(_manifest(tmp_path, mode), _request(tmp_path))
    assert invalid.value.code == "provider.result.evidence_invalid"


@pytest.mark.parametrize(
    "mode",
    [
        "result-exit17",
        "wrong-analysis",
        "null-tool",
        "empty-command",
        "outside-destination",
    ],
)
def test_provider_rejects_false_native_execution_or_destination_binding(
    tmp_path: Path, mode: str
) -> None:
    with pytest.raises(ProviderRuntimeError) as invalid:
        invoke_local_provider(_manifest(tmp_path, mode), _request(tmp_path))

    assert invalid.value.code == "provider.result.evidence_invalid"


def test_provider_rejects_unbound_locator_and_unsafe_destination_policy(
    tmp_path: Path,
) -> None:
    relative = _request(tmp_path)
    relative["target"]["locator"]["path"] = "relative-testbench.cir"
    assert any(
        "canonical absolute filesystem path" in issue
        for issue in provider_request_issues(relative)
    )

    replacement = _request(tmp_path)
    replacement["evidence_destination"]["collision_policy"] = (
        "replace-driver-owned"
    )
    assert any(
        "supports fail-if-present only" in issue
        for issue in provider_request_issues(replacement)
    )

    existing = _request(tmp_path / "existing")
    manifest = _manifest(tmp_path / "existing")
    Path(existing["evidence_destination"]["locator"]["path"]).mkdir()
    with pytest.raises(ProviderRuntimeError) as collision:
        invoke_local_provider(manifest, existing)
    assert collision.value.code == "provider.evidence.destination_exists"


def test_provider_binds_declared_input_digest_before_launch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["target"]["locator"]["sha256"] = "0" * 64

    with pytest.raises(ProviderRuntimeError) as mismatch:
        invoke_local_provider(_manifest(tmp_path), request)

    assert mismatch.value.code == "provider.request.invalid"
    assert any("declared digest" in issue for issue in mismatch.value.issues)
    assert not Path(request["evidence_destination"]["locator"]["path"]).exists()


def test_provider_rejects_noncanonical_input_parent_and_oversize_target(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    target = real_parent / "testbench.cir"
    target.write_text("* testbench\n.end\n", encoding="utf-8")
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    request = _request(tmp_path / "linked-case")
    request["target"]["locator"].update(
        {
            "path": str(linked_parent / target.name),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    )

    with pytest.raises(ProviderRuntimeError) as linked:
        resolve_local_provider(_manifest(tmp_path / "linked-case"), request)
    assert linked.value.code == "provider.request.invalid"
    assert any("canonical" in issue for issue in linked.value.issues)

    large_target = tmp_path / "large.cir"
    with large_target.open("wb") as handle:
        handle.truncate(MAX_PROVIDER_TARGET_BYTES + 1)
    request = _request(tmp_path / "large-case")
    request["target"]["locator"].update(
        {"path": str(large_target), "sha256": "0" * 64}
    )
    with pytest.raises(ProviderRuntimeError) as oversized:
        resolve_local_provider(_manifest(tmp_path / "large-case"), request)
    assert oversized.value.code == "provider.request.invalid"
    assert any("pre-launch ceiling" in issue for issue in oversized.value.issues)


def test_provider_allows_large_bounded_configuration_input(tmp_path: Path) -> None:
    model_library = tmp_path / "large-model-library.lib"
    with model_library.open("wb") as handle:
        handle.truncate(MAX_PROVIDER_TARGET_BYTES + 1)
    request = _request(tmp_path)
    request["configuration"].append(
        {
            "role": "pdk",
            "required": True,
            "locator": {
                "type": "filesystem",
                "path": str(model_library),
                "extensions": {},
            },
            "extensions": {},
        }
    )

    resolved = resolve_local_provider(_manifest(tmp_path), request)

    pdk = next(item for item in resolved.request_inputs if item.label.startswith("#/configuration"))
    assert pdk.bytes == MAX_PROVIDER_TARGET_BYTES + 1
    assert pdk.maximum_bytes == MAX_PROVIDER_CONFIGURATION_BYTES


def test_provider_rejects_request_input_mutation_during_execution(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mutable-testbench.cir"
    target.write_text("* mutable testbench\n.end\n", encoding="utf-8")
    request = _request(tmp_path)
    request["target"]["locator"].update(
        {
            "path": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    )

    with pytest.raises(ProviderRuntimeError) as changed:
        invoke_local_provider(_manifest(tmp_path, "mutate-input"), request)

    assert changed.value.code == "provider.result.evidence_invalid"
    assert any("changed or became unavailable" in issue for issue in changed.value.issues)


def test_provider_binds_existing_cwd_relative_entrypoint_argument(
    tmp_path: Path,
) -> None:
    case = tmp_path / "basename-entrypoint"
    script = _provider_script(case)
    manifest = _manifest(case)
    manifest["transports"][0]["argv"] = [
        os.sys.executable,
        script.name,
        "mutate-entrypoint",
    ]

    with pytest.raises(ProviderRuntimeError) as changed:
        invoke_local_provider(manifest, _request(case), cwd=case)

    assert changed.value.code == "provider.transport.identity_changed"


def test_provider_enforces_request_artifact_ceiling_and_recorded_file_identity(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    request = _request(tmp_path)
    request["execution_constraints"]["max_artifact_bytes"] = 1
    with pytest.raises(ProviderRuntimeError) as over_limit:
        invoke_local_provider(manifest, request)
    assert over_limit.value.code == "provider.result.evidence_invalid"

    request = _request(tmp_path / "tamper")
    manifest = _manifest(tmp_path / "tamper")
    payload = invoke_local_provider(manifest, request)
    Path(payload["artifacts"][0]["path"]).write_bytes(b"changed\n")
    resolved = resolve_local_provider(manifest, request)
    with pytest.raises(ProviderRuntimeError) as changed:
        validate_provider_result(payload, request, resolved)
    assert changed.value.code == "provider.result.file_invalid"


def test_wait_transport_does_not_leave_provider_descendants_running(
    tmp_path: Path,
) -> None:
    invoke_local_provider(_manifest(tmp_path, "descendant"), _request(tmp_path))
    child_pid = int((tmp_path / "evidence" / "child.pid").read_text(encoding="ascii"))
    state_path = Path(f"/proc/{child_pid}/stat")
    for _ in range(100):
        if not state_path.exists():
            break
        fields = state_path.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            break
        time.sleep(0.01)
    else:
        os.kill(child_pid, 9)
        pytest.fail("provider descendant remained live after wait completion")


def test_result_validation_rejects_envelope_operation_mismatch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    request = _request(tmp_path)
    resolved = resolve_local_provider(manifest, request)
    payload = invoke_local_provider(manifest, request)
    payload["operation"] = "circuit.simulate"

    with pytest.raises(ProviderRuntimeError) as error:
        validate_provider_result(payload, request, resolved)

    assert error.value.code == "provider.result.identity_mismatch"


def test_provider_cli_validate_list_and_invoke_round_trip(
    tmp_path: Path, capsys
) -> None:
    manifest_path = tmp_path / "manifest.json"
    request_path = tmp_path / "request.json"
    manifest_path.write_text(json.dumps(_manifest(tmp_path)), encoding="utf-8")
    request_path.write_text(json.dumps(_request(tmp_path)), encoding="utf-8")

    assert main(["--compact", "provider", "validate", str(manifest_path)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["operation"] == "provider.validate"

    assert main(
        ["--compact", "provider", "list", "--manifest", str(manifest_path)]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["data"]["runtime_scope"]["marketplace"] is False
    assert listed["data"]["runtime_scope"]["registered_operation_profiles"] == [
        "openada.operation/circuit.simulate/v1alpha2"
    ]
    assert len(listed["data"]["capabilities"]) == 1

    assert main(
        [
            "--compact",
            "provider",
            "invoke",
            "--manifest",
            str(manifest_path),
            str(request_path),
        ]
    ) == 0
    invoked = json.loads(capsys.readouterr().out)
    assert invoked["operation"] == "simulate"
    assert invoked["engineering"]["status"] == "pass"


def test_provider_cli_preserves_transport_failure_status(
    tmp_path: Path, capsys
) -> None:
    manifest_path = tmp_path / "manifest.json"
    request_path = tmp_path / "request.json"
    request = _request(tmp_path)
    request["execution_constraints"]["timeout_ms"] = 20
    manifest_path.write_text(
        json.dumps(_manifest(tmp_path, "timeout")), encoding="utf-8"
    )
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert main(
        [
            "--compact",
            "provider",
            "invoke",
            "--manifest",
            str(manifest_path),
            str(request_path),
        ]
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "provider.invoke"
    assert payload["execution"]["status"] == "timed_out"
    assert payload["diagnostics"][0]["code"] == "provider.transport.timed_out"
