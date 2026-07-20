from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import sysconfig

from jsonschema import Draft202012Validator
import pytest

from openada.contract import diagnostic, file_record, result, static_execution, tool_record
from openada.provider_runtime import (
    invoke_local_provider,
    provider_manifest_issues,
    resolve_local_provider,
)
from openada.providers import ngspice_pdk_control as provider


ROOT = Path(__file__).parents[1]

_ANALYSIS_CASES = {
    "op": (
        "op",
        {"type": "op", "extensions": {}},
        "openada.feature/simulation.analysis.op/v1alpha1",
    ),
    "dc": (
        "dc VSWEEP 0 1.2 0.1",
        {
            "type": "dc",
            "source_name": "VSWEEP",
            "source_unit": "V",
            "start": 0.0,
            "stop": 1.2,
            "step": 0.1,
            "extensions": {},
        },
        "openada.feature/simulation.analysis.dc/v1alpha1",
    ),
    "ac": (
        "ac dec 10 1 1Meg",
        {
            "type": "ac",
            "sweep": "dec",
            "points": 10,
            "start_hz": 1.0,
            "stop_hz": 1e6,
            "extensions": {},
        },
        "openada.feature/simulation.analysis.ac/v1alpha1",
    ),
    "tran": (
        "tran 50n 2u",
        {
            "type": "tran",
            "step_s": 5e-8,
            "stop_s": 2e-6,
            "extensions": {},
        },
        "openada.feature/simulation.analysis.tran/v1alpha1",
    ),
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_provider_subprocess_fixture(path: Path, native_executable: Path) -> None:
    body = '''from pathlib import Path
import sys

from openada.contract import file_record, result, tool_record
from openada.providers import ngspice_pdk_control as provider

provider.NATIVE_NGSPICE_CANDIDATES = (__NATIVE_EXECUTABLE__,)


def fake_simulate(self, spice_file, output_dir, **kwargs):
    output = Path(output_dir)
    output.mkdir(parents=True)
    raw = output / "result.raw"
    log = output / "ngspice.log"
    raw.write_bytes(b"deterministic provider raw fixture\\n")
    log.write_bytes(b"deterministic provider log fixture\\n")
    return result(
        "simulate",
        tool=tool_record("ngspice", path=self.binary, version="fixture"),
        execution={
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 1,
            "command": [self.binary, "fixture"],
            "cwd": str(output),
        },
        engineering_status="pass",
        summary="Deterministic subprocess fixture produced bounded evidence.",
        inputs=[file_record(spice_file, kind="spice-netlist", role="input")],
        artifacts=[
            file_record(raw, kind="ngspice-raw", role="simulation.result"),
            file_record(log, kind="ngspice-log", role="simulation.log"),
        ],
        data={
            "inputs_stable": True,
            "analysis_evidence": {
                "point_count": 3,
                "dependent_variable_count": 1,
                "finite_value_count": 3,
            },
        },
    )


provider.NgspiceDriver.simulate = fake_simulate
raise SystemExit(provider.main())
'''.replace("__NATIVE_EXECUTABLE__", repr(str(native_executable)))
    path.write_text(
        f"#!{sys.executable}\n" + body,
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture(autouse=True)
def _provider_owned_ngspice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        provider,
        "NATIVE_NGSPICE_CANDIDATES",
        (str(tmp_path / "ngspice"),),
    )


def _case(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    target = tmp_path / "inverter.spice"
    target.write_text(
        """* provider control-deck fixture
.control
save all
tran 50n 2u
write result.raw
.endc
R1 out 0 1k
.end
""",
        encoding="utf-8",
    )
    system_init = tmp_path / "spinit"
    system_init.write_text("* explicit system init\n", encoding="utf-8")
    executable = tmp_path / "ngspice"
    executable.write_bytes(b"\x7fELF" + b"openada-fixture" * 8)
    executable.chmod(0o755)
    pdk_directory = tmp_path / "fixture-pdk"
    pdk_directory.mkdir()
    init_file = pdk_directory / ".spiceinit"
    init_file.write_text("* explicit provider init\n", encoding="utf-8")
    pdk = pdk_directory / "COMMIT"
    pdk.write_text("fixture-pdk-revision\n", encoding="utf-8")
    config = tmp_path / "provider-config.json"
    config.write_text(
        json.dumps(
            {
                "schema": provider.CONFIG_SCHEMA,
                "init_file": {"path": str(init_file), "sha256": _digest(init_file)},
                "system_init_file": {
                    "path": str(system_init),
                    "sha256": _digest(system_init),
                },
                "environment": {
                    "PDK": "fixture-pdk",
                    "PDK_ROOT": str(tmp_path),
                },
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "evidence"
    request = {
        "schema": "openada.request/v0alpha1",
        "request_id": "12345678-1234-4234-8234-123456789abc",
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "target": {
            "kind": "testbench",
            "locator": {
                "type": "filesystem",
                "path": str(target),
                "sha256": _digest(target),
                "extensions": {},
            },
            "extensions": {},
        },
        "configuration": [
            {
                "role": "simulator-configuration",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    "path": str(config),
                    "sha256": _digest(config),
                    "extensions": {},
                },
                "extensions": {},
            },
            {
                "role": "pdk",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    "path": str(pdk),
                    "sha256": _digest(pdk),
                    "extensions": {},
                },
                "extensions": {},
            },
        ],
        "parameters": {
            "analysis": {
                "type": "tran",
                "step_s": 5e-8,
                "stop_s": 2e-6,
                "extensions": {},
            },
            "extensions": {},
        },
        "evidence_policy": {
            "required_artifact_roles": ["simulation.result", "simulation.log"],
            "retain_native_artifacts": True,
            "retain_native_logs": True,
            "provenance": "bounded",
            "identity_requirement": "content-digest",
            "extensions": {},
        },
        "evidence_destination": {
            "locator": {"type": "filesystem", "path": str(destination), "extensions": {}},
            "collision_policy": "fail-if-present",
            "extensions": {},
        },
        "execution_constraints": {
            "completion": "wait",
            "timeout_ms": 30_000,
            "max_log_bytes": 16_777_216,
            "max_artifact_bytes": 268_435_456,
            "side_effects": "evidence-only",
            "extensions": {},
        },
        "driver_selector": {
            "driver_id": provider.DRIVER_ID,
            "driver_version": provider.DRIVER_VERSION,
            "transport_id": "local-json-stdio",
            "required_features": [
                "openada.feature/simulation.analysis.tran/v1alpha1"
            ],
            "extensions": {},
        },
        "extensions": {},
    }
    return request, {
        "target": target,
        "executable": executable,
        "init": init_file,
        "system": system_init,
        "pdk": pdk,
        "config": config,
        "destination": destination,
    }


def _select_analysis(
    request: dict,
    paths: dict[str, Path],
    analysis_type: str,
    *,
    linearize: bool = False,
) -> None:
    directive, parameters, feature = _ANALYSIS_CASES[analysis_type]
    body = paths["target"].read_text(encoding="utf-8").replace(
        "tran 50n 2u",
        directive + ("\nlinearize" if linearize else ""),
    )
    paths["target"].write_text(body, encoding="utf-8")
    request["target"]["locator"]["sha256"] = _digest(paths["target"])
    request["parameters"]["analysis"] = parameters
    request["driver_selector"]["required_features"] = [feature]


def test_reference_provider_manifest_and_configuration_schema_are_valid(
    tmp_path: Path,
) -> None:
    manifest = json.loads(
        (ROOT / "providers/ngspice-pdk-control/driver-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (
            ROOT
            / "providers/ngspice-pdk-control/provider-config-v0alpha1.schema.json"
        ).read_text(encoding="utf-8")
    )
    request, paths = _case(tmp_path)
    config = json.loads(paths["config"].read_text(encoding="utf-8"))

    assert provider_manifest_issues(manifest) == ()
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(config)) == []
    assert request["driver_selector"]["driver_id"] == provider.DRIVER_ID
    assert manifest["driver"]["version"] == provider.DRIVER_VERSION
    assert manifest["capabilities"][0]["features"] == [
        _ANALYSIS_CASES[name][2] for name in ("op", "dc", "ac", "tran")
    ]


@pytest.mark.parametrize("analysis_type", ("op", "dc", "ac", "tran"))
@pytest.mark.parametrize("linearize", (False, True))
def test_provider_accepts_every_advertised_analysis_and_only_tran_linearize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    analysis_type: str,
    linearize: bool,
) -> None:
    request, paths = _case(tmp_path)
    _select_analysis(
        request,
        paths,
        analysis_type,
        linearize=linearize,
    )
    if linearize and analysis_type != "tran":
        with pytest.raises(provider.ProviderInputError, match="linearize"):
            provider.invoke(request)
        assert not paths["destination"].exists()
        return

    observed: dict[str, object] = {}

    def fake_simulate(self, spice_file, output_dir, **kwargs):
        observed["expected_outputs"] = kwargs["expected_outputs"]
        return result(
            "simulate",
            tool=tool_record("ngspice", path=self.binary, version="fixture"),
            execution=static_execution("failed"),
            engineering_status="unknown",
            summary="Fixture intentionally does not launch native EDA.",
            inputs=[file_record(spice_file, kind="spice-netlist", role="input")],
            diagnostics=[diagnostic("error", "simulation.analysis_unproven", "fixture")],
            data={"inputs_stable": True},
        )

    monkeypatch.setattr(provider.NgspiceDriver, "simulate", fake_simulate)
    payload = provider.invoke(request)

    assert payload["data"]["analysis"]["type"] == analysis_type
    assert payload["data"]["extensions"]["org.openada"]["parameters"] == request[
        "parameters"
    ]
    assert observed["expected_outputs"][0].path == "result.raw"


@pytest.mark.parametrize(
    "control_lines",
    [
        ["tran 50n 2u", "save all", "write result.raw"],
        ["save all", "save all", "tran 50n 2u", "write result.raw"],
        ["save all", "tran 50n 2u", "op", "write result.raw"],
        ["save all", "tran 50n 2u", "write result.raw", "write other.raw"],
        ["save all", "linearize", "tran 50n 2u", "write result.raw"],
        ["save all", "tran 50n 2u", "linearize", "linearize", "write result.raw"],
        ["save all", "tran 50n 2u", "linearize extra", "write result.raw"],
        ["save all", "tran 50n 2u", "write result.raw", "linearize"],
    ],
)
def test_provider_rejects_every_out_of_order_or_repeated_closed_command(
    tmp_path: Path,
    control_lines: list[str],
) -> None:
    request, paths = _case(tmp_path)
    body = "* closed-order test\n.control\n" + "\n".join(control_lines) + (
        "\n.endc\nR1 out 0 1k\n.end\n"
    )
    paths["target"].write_text(body, encoding="utf-8")
    request["target"]["locator"]["sha256"] = _digest(paths["target"])

    with pytest.raises(provider.ProviderInputError, match="out-of-order|linearize"):
        provider.invoke(request)

    assert not paths["destination"].exists()


def test_reference_provider_closes_the_generic_transport_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, paths = _case(tmp_path)
    manifest = json.loads(
        (ROOT / "providers/ngspice-pdk-control/driver-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    scripts = Path(sysconfig.get_path("scripts")).resolve()
    resolved = resolve_local_provider(manifest, request, cwd=ROOT)
    assert Path(resolved.executable) == scripts / "openada-provider-ngspice"

    fixture = tmp_path / "openada-provider-fixture"
    _write_provider_subprocess_fixture(fixture, paths["executable"])
    manifest["transports"][0]["argv"] = [str(fixture)]
    monkeypatch.setenv("PATH", str(tmp_path / "hostile-bin"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "hostile-python"))
    monkeypatch.setenv("HOME", str(tmp_path / "hostile-home"))

    payload = invoke_local_provider(manifest, request, cwd=ROOT)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert payload["diagnostics"] == []
    assert paths["destination"].is_dir()


def test_provider_binds_closed_control_deck_and_all_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, paths = _case(tmp_path)

    observed: dict[str, object] = {}

    def fake_simulate(self, spice_file, output_dir, **kwargs):
        observed["binary"] = self.binary
        observed.update(kwargs)
        inputs = [
            file_record(spice_file, kind="spice-netlist", role="input"),
            file_record(kwargs["init_file"], kind="ngspice-init", role="configuration"),
            file_record(
                kwargs["system_init_file"],
                kind="ngspice-system-init",
                role="configuration",
            ),
        ]
        return result(
            "simulate",
            tool=tool_record("ngspice", path="/tools/ngspice", version="fixture"),
            execution=static_execution("failed"),
            engineering_status="unknown",
            summary="Fixture does not launch native EDA.",
            inputs=inputs,
            diagnostics=[
                diagnostic("error", "simulation.analysis_unproven", "fixture")
            ],
            data={"inputs_stable": True},
        )

    monkeypatch.setattr(provider.NgspiceDriver, "simulate", fake_simulate)
    monkeypatch.setenv("PATH", "/attacker/bin")

    payload = provider.invoke(request)

    assert payload["engineering"]["status"] == "unknown"
    assert observed["binary"] != str(paths["executable"])
    assert Path(str(observed["binary"])).name == "openada-native-ngspice"
    assert observed["environment_mode"] == "sanitized"
    assert payload["data"]["protocol"]["driver_id"] == provider.DRIVER_ID
    assert payload["data"]["extensions"]["org.openada"]["parameters"] == request[
        "parameters"
    ]
    retained_paths = {item["path"] for item in payload["inputs"]}
    assert {
        str(paths[name])
        for name in ("target", "executable", "init", "system", "pdk", "config")
    }.issubset(retained_paths)
    assert str(observed["binary"]) in retained_paths
    assert paths["destination"].is_dir()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("shell touch /tmp/escaped", "Unsupported ngspice control command"),
        ("write ../escaped.raw", "control-safe raw write"),
        ("ac dec 10 1 1Meg", "Unsupported ngspice control command"),
    ],
)
def test_provider_rejects_control_commands_outside_its_conformed_subset(
    tmp_path: Path, replacement: str, message: str
) -> None:
    request, paths = _case(tmp_path)
    body = paths["target"].read_text(encoding="utf-8").replace(
        "write result.raw", replacement
    )
    paths["target"].write_text(body, encoding="utf-8")
    request["target"]["locator"]["sha256"] = _digest(paths["target"])

    with pytest.raises(provider.ProviderInputError, match=message):
        provider.invoke(request)


def test_provider_rejects_request_analysis_that_differs_from_control_deck(
    tmp_path: Path,
) -> None:
    request, _paths = _case(tmp_path)
    request["parameters"]["analysis"]["stop_s"] = 3e-6

    with pytest.raises(provider.ProviderInputError, match="do not exactly match"):
        provider.invoke(request)


@pytest.mark.parametrize(
    "required_features",
    [
        [],
        ["openada.feature/simulation.analysis.tran/v1alpha1"],
        [
            "openada.feature/simulation.analysis.ac/v1alpha1",
            "openada.feature/simulation.analysis.tran/v1alpha1",
        ],
    ],
)
def test_provider_requires_exactly_the_feature_matching_the_authoritative_analysis(
    tmp_path: Path,
    required_features: list[str],
) -> None:
    request, paths = _case(tmp_path)
    _select_analysis(request, paths, "ac")
    request["driver_selector"]["required_features"] = required_features

    with pytest.raises(provider.ProviderInputError, match="must contain exactly the feature"):
        provider.invoke(request)

    assert not paths["destination"].exists()


def test_provider_rejects_pdk_identity_outside_selected_pdk(tmp_path: Path) -> None:
    request, _paths = _case(tmp_path)
    unrelated = tmp_path / "unrelated-COMMIT"
    unrelated.write_text("unrelated revision\n", encoding="utf-8")
    locator = next(
        item["locator"]
        for item in request["configuration"]
        if item["role"] == "pdk"
    )
    locator["path"] = str(unrelated)
    locator["sha256"] = _digest(unrelated)

    with pytest.raises(
        provider.ProviderInputError, match="PDK_ROOT/PDK/COMMIT"
    ):
        provider.invoke(request)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda request: request["driver_selector"].__setitem__(
                "transport_id", "another-transport"
            ),
            "local-json-stdio",
        ),
        (
            lambda request: request["driver_selector"].__setitem__(
                "required_features",
                ["openada.feature/simulation.analysis.noise/v1alpha1"],
            ),
            "unsupported provider feature",
        ),
        (
            lambda request: request["target"]["locator"].pop("sha256"),
            "explicit content digest",
        ),
        (
            lambda request: request["configuration"][0].__setitem__(
                "required", False
            ),
            "must be required",
        ),
        (
            lambda request: request["configuration"].append(
                {**request["configuration"][0], "role": "corner"}
            ),
            "exactly one pdk and one simulator-configuration",
        ),
        (
            lambda request: request["evidence_policy"].__setitem__(
                "retain_native_logs", False
            ),
            "requires native artifacts and logs",
        ),
        (
            lambda request: request["evidence_policy"].__setitem__(
                "provenance", "complete-required"
            ),
            "cannot claim complete provenance",
        ),
        (
            lambda request: request["evidence_policy"].__setitem__(
                "identity_requirement", "native-revision"
            ),
            "requires content-digest",
        ),
        (
            lambda request: request["evidence_policy"].__setitem__(
                "identity_requirement", "best-available"
            ),
            "requires content-digest",
        ),
        (
            lambda request: request["execution_constraints"].__setitem__(
                "completion", "submit"
            ),
            "completion='wait'",
        ),
        (
            lambda request: request["execution_constraints"].__setitem__(
                "side_effects", "transactional-design-write"
            ),
            "side_effects='evidence-only'",
        ),
        (
            lambda request: request["execution_constraints"].__setitem__(
                "max_log_bytes", 1024
            ),
            "max_log_bytes",
        ),
        (
            lambda request: request["execution_constraints"].__setitem__(
                "max_artifact_bytes", 1024
            ),
            "max_artifact_bytes",
        ),
        (
            lambda request: request["evidence_destination"].__setitem__(
                "collision_policy", "replace-driver-owned"
            ),
            "supports fail-if-present only",
        ),
        (
            lambda request: request.__setitem__("ignored_execution_switch", True),
            "Additional properties are not allowed",
        ),
    ],
    ids=[
        "transport",
        "feature",
        "target-digest",
        "configuration-required",
        "extra-configuration",
        "retain-log",
        "provenance",
        "identity",
        "best-available-identity",
        "completion",
        "side-effects",
        "log-limit",
        "artifact-limit",
        "collision",
        "unknown-field",
    ],
)
def test_provider_rejects_ambiguous_direct_requests_before_destination_creation(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    request, paths = _case(tmp_path)
    mutation(request)

    with pytest.raises(provider.ProviderInputError, match=message):
        provider.invoke(request)

    assert not paths["destination"].exists()


def test_provider_rejects_request_selected_executable_before_creating_evidence(
    tmp_path: Path,
) -> None:
    request, paths = _case(tmp_path)
    config = json.loads(paths["config"].read_text(encoding="utf-8"))
    hostile = tmp_path / "attacker-ngspice"
    hostile.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hostile.chmod(0o755)
    config["executable_file"] = {
        "path": str(hostile),
        "sha256": _digest(hostile),
    }
    paths["config"].write_text(json.dumps(config), encoding="utf-8")
    configuration_locator = next(
        item["locator"]
        for item in request["configuration"]
        if item["role"] == "simulator-configuration"
    )
    configuration_locator["sha256"] = _digest(paths["config"])

    with pytest.raises(provider.ProviderInputError, match="closed"):
        provider.invoke(request)

    assert not paths["destination"].exists()


def test_provider_rejects_script_at_provider_owned_native_location(
    tmp_path: Path,
) -> None:
    request, paths = _case(tmp_path)
    paths["executable"].write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(provider.ProviderInputError, match="native ELF"):
        provider.invoke(request)

    assert not paths["destination"].exists()


def test_stdio_provider_returns_one_typed_unknown_for_a_rejected_valid_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, paths = _case(tmp_path)
    request["driver_selector"]["transport_id"] = "another-transport"
    monkeypatch.setattr(provider, "_read_request", lambda: request)

    return_code = provider.main()

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert {item["code"] for item in payload["diagnostics"]} == {
        "simulation.request.invalid"
    }
    assert not paths["destination"].exists()


def test_stdio_provider_never_writes_a_partial_oversized_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(provider, "_read_request", lambda: {})
    monkeypatch.setattr(
        provider,
        "invoke",
        lambda _request: {"oversized": "x" * provider.MAX_RESULT_BYTES},
    )

    return_code = provider.main()

    captured = capsys.readouterr()
    assert return_code == 2
    assert captured.out == ""
    assert "simulation.result.over_limit" in captured.err


def test_provider_downgrades_when_selected_pdk_directory_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, paths = _case(tmp_path)

    def fake_simulate(self, spice_file, output_dir, **kwargs):
        selected_pdk = paths["init"].parent
        replacement_source = {
            ".spiceinit": paths["init"].read_bytes(),
            "COMMIT": paths["pdk"].read_bytes(),
        }
        selected_pdk.rename(tmp_path / "original-pdk")
        selected_pdk.mkdir()
        for name, body in replacement_source.items():
            (selected_pdk / name).write_bytes(body)
        return result(
            "simulate",
            tool=tool_record("ngspice", path=self.binary, version="fixture"),
            execution=static_execution("completed"),
            engineering_status="pass",
            summary="fixture",
            inputs=[file_record(spice_file, kind="spice-netlist", role="input")],
            data={"inputs_stable": True},
        )

    monkeypatch.setattr(provider.NgspiceDriver, "simulate", fake_simulate)

    payload = provider.invoke(request)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert "simulation.result.stale" in {
        item["code"] for item in payload["diagnostics"]
    }
    assert (
        payload["data"]["extensions"]["org.openada"]["native_data"][
            "inputs_stable"
        ]
        is False
    )
