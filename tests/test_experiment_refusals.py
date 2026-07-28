from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from experiment_acceptance_spec import (
    PDK_ROOT,
    SOURCE_FOLLOWER_BUNDLE,
    gain_spec,
)
from openada.discovery import DiscoveryManager
from openada.engines import simra_artifact
import openada.operations.experiment as experiment_operation


def _validation_assets_available() -> bool:
    return (
        (PDK_ROOT / "ihp-sg13g2").exists()
        and (SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json").is_file()
    )


def _parallel_output_capacitor() -> dict[str, Any]:
    return {
        "name": "C_KEEP",
        "kind": "capacitor",
        "plus": "OUT",
        "minus": "0",
        "parameters": {"c": "1p"},
    }


def _ordinary_measurement(
    identifier: str,
    kind: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": identifier,
        "analysis_id": "ac_gain",
        "operation_profile": "openada.operation/result.measure/v1alpha1",
        "request": {
            "measurement_id": identifier,
            "kind": kind,
            "signal": "out_real",
            "parameters": parameters,
            "extensions": {},
        },
    }


def _mutate_refusal(spec: dict[str, Any], case: str) -> None:
    if case == "missing_parameter":
        spec["elements"][3]["parameters"] = {}
        spec["elements"].append(_parallel_output_capacitor())
    elif case == "unbound_port":
        del spec["dut"]["connections"]["VSS"]
    elif case == "wrong_bundle_digest":
        spec["dut"]["bundle"]["netlist_sha256"] = "0" * 64
    elif case == "emitted_name_collision":
        spec["elements"].extend(
            [
                {
                    "name": "LOAD",
                    "kind": "resistor",
                    "plus": "OUT",
                    "minus": "0",
                    "parameters": {"r": "1k"},
                },
                {
                    "name": "R_LOAD",
                    "kind": "resistor",
                    "plus": "OUT",
                    "minus": "0",
                    "parameters": {"r": "2k"},
                },
            ]
        )
    elif case == "ac_excitation_empty":
        spec["analyses"][0]["ac_excitation"] = []
    elif case == "ac_excitation_all_zero":
        spec["elements"][2]["parameters"]["ac_mag"] = "0"
    elif case == "ac_excitation_incomplete":
        spec["elements"][0]["parameters"].update(
            {"ac_mag": "1", "ac_phase": "0"}
        )
    elif case == "measurement_unknown_analysis":
        spec["measurements"][0]["analysis_id"] = "missing_analysis"
    elif case == "measurement_unknown_observation":
        spec["measurements"][0]["request"]["input"]["real"] = (
            "missing_observation"
        )
    elif case == "derivation_mixed_units":
        spec["measurements"].extend(
            [
                _ordinary_measurement(
                    "crossing_parent",
                    "crossing",
                    {
                        "threshold": {"value": 0.2, "unit": "V"},
                        "direction": "rising",
                        "occurrence": 1,
                    },
                ),
                _ordinary_measurement("voltage_parent", "minimum", {}),
            ]
        )
        spec["derivations"] = [
            {
                "id": "mixed_subtract",
                "kind": "subtract",
                "analysis_id": "ac_gain",
                "parents": ["crossing_parent", "voltage_parent"],
            }
        ]
    elif case == "behavioral_source_kind":
        spec["elements"][3]["kind"] = "behavioral"
        spec["elements"].append(_parallel_output_capacitor())
    elif case == "controlled_source_kind":
        spec["elements"][3]["kind"] = "vcvs"
        spec["elements"].append(_parallel_output_capacitor())
    else:  # pragma: no cover - keeps the table and mutator in lockstep
        raise AssertionError(f"unknown refusal case {case!r}")


def _assert_refused_without_execution(
    *,
    spec: dict[str, Any],
    expected_code: str,
    expected_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    spec_path = tmp_path / "refused.experiment.json"
    spec_path.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "must-not-exist"
    calls: list[object] = []

    def forbidden_simulate(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("an invalid experiment reached the simulator")

    monkeypatch.setattr(experiment_operation, "simulate", forbidden_simulate)
    payload = experiment_operation.run_experiment(
        spec_path,
        output_dir,
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
    )

    assert calls == []
    assert not output_dir.exists()
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    matches = [
        item
        for item in payload["diagnostics"]
        if item["code"] == expected_code
    ]
    assert matches, payload["diagnostics"]
    assert any(
        item["hint"] == f"JSON Pointer: {expected_path}" for item in matches
    ), matches
    return payload


@pytest.mark.parametrize(
    ("case", "code", "path"),
    [
        (
            "missing_parameter",
            "experiment.element.parameter_missing",
            "/elements/3/parameters/c",
        ),
        (
            "unbound_port",
            "experiment.dut.port_unbound",
            "/dut/connections/VSS",
        ),
        (
            "wrong_bundle_digest",
            "experiment.dut.digest_mismatch",
            "/dut/bundle",
        ),
        (
            "emitted_name_collision",
            "experiment.element.emitted_name_collision",
            "/elements/5/name",
        ),
        (
            "ac_excitation_empty",
            "experiment.analysis.ac_stimulus_missing",
            "/analyses/0/ac_excitation",
        ),
        (
            "ac_excitation_all_zero",
            "experiment.analysis.ac_stimulus_all_zero",
            "/analyses/0/ac_excitation",
        ),
        (
            "ac_excitation_incomplete",
            "experiment.analysis.ac_stimulus_incomplete",
            "/analyses/0/ac_excitation",
        ),
        (
            "measurement_unknown_analysis",
            "experiment.measurement.analysis_unknown",
            "/measurements/0/analysis_id",
        ),
        (
            "measurement_unknown_observation",
            "experiment.measurement.selector_missing",
            "/measurements/0/request/input/real",
        ),
        (
            "derivation_mixed_units",
            "experiment.derivation.unit_mismatch",
            "/derivations/0/parents",
        ),
        (
            "behavioral_source_kind",
            "experiment.element.kind_unsupported",
            "/elements/3/kind",
        ),
        (
            "controlled_source_kind",
            "experiment.element.kind_unsupported",
            "/elements/3/kind",
        ),
    ],
)
def test_required_structural_refusals_do_not_execute_or_create_output(
    case,
    code,
    path,
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    _mutate_refusal(spec, case)
    _assert_refused_without_execution(
        spec=spec,
        expected_code=code,
        expected_path=path,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


FORBIDDEN_SCALAR_FORMS = [
    "1k .include /etc/passwd",
    ".include /etc/passwd",
    ".inc /etc/passwd",
    ".lib /tmp/models.lib tt",
    ".control\nshell cat /etc/passwd\n.endc",
    ".endc",
    ".model evil nmos",
    ".subckt evil A B",
    ".ends evil",
    ".option scale=1u",
    ".option wnflag=0",
    ".option temp=125",
    ".param leaked=1k",
    ".func leaked(x) x",
    ".measure tran leaked find v(OUT)",
    ".alter",
    ".global NVDD",
    "pre_osdi /tmp/evil.osdi",
    "shell cat /etc/passwd",
    "write stolen.raw all",
    "wrdata stolen.dat v(OUT)",
    "plot v(OUT)",
    "print v(OUT)",
    "{1k}",
    '"1k"',
    "1k*2",
    "1k+2",
    "sqrt(4)",
    "$fixture_r",
    "@m1[id]",
    "PARAM_R",
    "agauss(1k,1,1)",
    "random()",
    "V(OUT)+1",
    "v(OUT)",
    "v(X_OPENADA_DUT.Y)",
    "i(V_IN)",
    "/etc/passwd",
    "stolen.raw",
    "scale=1u",
    "wnflag=0",
    "temperature=125",
    "startup_policy=unmanaged",
    "model=nmos.core",
    "collateral=/tmp/models.lib",
    "B_LEAK OUT 0 V=1",
    "E_LEAK OUT 0 IN 0 1",
    "1k\n.end\n.include /etc/passwd",
]


@pytest.mark.parametrize("forbidden", FORBIDDEN_SCALAR_FORMS)
def test_every_forbidden_directive_brace_expression_and_path_scalar_is_refused(
    forbidden,
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    spec["elements"][0]["parameters"].update(
        {"ac_mag": forbidden, "ac_phase": "0"}
    )
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.element.parameter_expression_forbidden",
        expected_path="/elements/0/parameters/ac_mag",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_one_validation_pass_reports_all_independent_document_errors(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    _mutate_refusal(spec, "missing_parameter")
    _mutate_refusal(spec, "emitted_name_collision")
    _mutate_refusal(spec, "ac_excitation_empty")
    _mutate_refusal(spec, "measurement_unknown_analysis")
    spec["elements"][0]["parameters"].update(
        {"ac_mag": "{not_a_number}", "ac_phase": "0"}
    )

    payload = _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.element.parameter_missing",
        expected_path="/elements/3/parameters/c",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    observed = {
        (item["code"], item["hint"])
        for item in payload["diagnostics"]
    }
    assert {
        (
            "experiment.element.parameter_expression_forbidden",
            "JSON Pointer: /elements/0/parameters/ac_mag",
        ),
        (
            "experiment.element.parameter_missing",
            "JSON Pointer: /elements/3/parameters/c",
        ),
        (
            "experiment.element.emitted_name_collision",
            "JSON Pointer: /elements/6/name",
        ),
        (
            "experiment.analysis.ac_stimulus_missing",
            "JSON Pointer: /analyses/0/ac_excitation",
        ),
        (
            "experiment.measurement.analysis_unknown",
            "JSON Pointer: /measurements/0/analysis_id",
        ),
    }.issubset(observed)


@pytest.mark.parametrize(
    ("target", "code", "path"),
    [
        (
            "element",
            "experiment.element.parameter_out_of_range",
            "/elements/3/parameters/c",
        ),
        (
            "analysis",
            "experiment.analysis.invalid",
            "/analyses/0/stop",
        ),
    ],
)
def test_extreme_decimal_exponents_are_typed_preflight_refusals(
    target,
    code,
    path,
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    if target == "element":
        spec["elements"][3]["parameters"]["c"] = "1e1000000"
    else:
        spec["analyses"][0]["stop"] = "1e1000000"
    _assert_refused_without_execution(
        spec=spec,
        expected_code=code,
        expected_path=path,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_extreme_decimal_report_collects_all_typed_errors_without_traceback(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    spec["elements"][3]["parameters"]["c"] = "1e1000000"
    spec["analyses"][0]["stop"] = "1e1000000"
    payload = _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.element.parameter_out_of_range",
        expected_path="/elements/3/parameters/c",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    observed = {
        (item["code"], item["hint"]) for item in payload["diagnostics"]
    }
    assert (
        "experiment.analysis.invalid",
        "JSON Pointer: /analyses/0/stop",
    ) in observed


@pytest.mark.parametrize(
    ("target", "needle", "replacement", "expected_code", "expected_path"),
    [
        (
            "element",
            '"c": "1p"',
            '"c": 1e1000000',
            "experiment.element.parameter_out_of_range",
            "/elements/3/parameters/c",
        ),
        (
            "measurement",
            '"bandwidth_drop_db": 3.0',
            '"bandwidth_drop_db": 1e1000000',
            "experiment.measurement.request_invalid",
            "/measurements/0/request",
        ),
    ],
)
def test_unquoted_extreme_json_exponents_are_typed_without_traceback(
    target,
    needle,
    replacement,
    expected_code,
    expected_path,
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    raw = json.dumps(gain_spec(), indent=2)
    assert needle in raw, target
    spec_path = tmp_path / f"{target}.experiment.json"
    spec_path.write_text(
        raw.replace(needle, replacement, 1) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "must-not-exist"

    monkeypatch.setattr(
        experiment_operation,
        "simulate",
        lambda *args, **kwargs: pytest.fail(
            "an out-of-range experiment reached simulation"
        ),
    )
    payload = experiment_operation.run_experiment(
        spec_path,
        output_dir,
        discovery=DiscoveryManager(),
        pdk="ihp-sg13g2",
        pdk_root=PDK_ROOT,
    )

    assert not output_dir.exists()
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert any(
        item["code"] == expected_code
        and item["hint"] == f"JSON Pointer: {expected_path}"
        for item in payload["diagnostics"]
    ), payload["diagnostics"]


@pytest.mark.parametrize(
    "locator",
    (
        "relative/schematic.artifact.json",
        "/tmp/publication/../schematic.artifact.json",
        "~/schematic.artifact.json",
        "//tmp/publication/schematic.artifact.json",
        "/tmp/publication/control\tschematic.artifact.json",
    ),
)
def test_suspicious_dut_locators_fire_publication_untrusted_before_lookup(
    locator,
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    spec["dut"]["artifact"] = locator
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.dut.publication_untrusted",
        expected_path="/dut/artifact",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_every_ascii_control_in_dut_locator_is_publication_untrusted() -> None:
    for codepoint in (*range(0x20), 0x7F):
        spec = gain_spec()
        spec["dut"]["artifact"] = (
            f"/tmp/publication/control{chr(codepoint)}"
            "schematic.artifact.json"
        )
        validator = experiment_operation._Validator(
            document=spec,
            spec_path=Path("/tmp/refused.experiment.json"),
            spec_bytes=b"{}",
            cli_pdk="ihp-sg13g2",
            pdk_root=PDK_ROOT,
        )

        bundle, _connections = validator._dut(spec["dut"])

        assert bundle is None
        assert [issue.code for issue in validator.issues] == [
            "experiment.dut.publication_untrusted"
        ]


def test_parent_symlink_dut_locator_is_publication_untrusted_before_lookup(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the SourceFollower acceptance bundle is required")
    alias = tmp_path / "publication-alias"
    alias.symlink_to(SOURCE_FOLLOWER_BUNDLE, target_is_directory=True)
    spec = gain_spec()
    spec["dut"]["artifact"] = str(alias / "schematic.artifact.json")
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.dut.publication_untrusted",
        expected_path="/dut/artifact",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_transitive_ihp_nmoscl_2_namespace_collision_refuses_experiment(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    published = simra_artifact.load_simra_schematic_bundle(
        SOURCE_FOLLOWER_BUNDLE / "schematic.artifact.json",
        expected_digests=spec["dut"]["bundle"],
        expected_top=spec["dut"]["top"],
    )
    collision_name = "nmoscl_2"
    collision_text = published.netlist_text.replace(
        published.top,
        collision_name,
    )
    collision_bundle = replace(
        published,
        top=collision_name,
        design_subckt_names=(collision_name,),
        netlist_text=collision_text,
        netlist_bytes=collision_text.encode("utf-8"),
    )
    spec["dut"]["top"] = collision_name
    monkeypatch.setattr(
        simra_artifact,
        "load_simra_schematic_bundle",
        lambda *args, **kwargs: collision_bundle,
    )

    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.compose.subckt_collision",
        expected_path="/dut/top",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_extractor_point_limit_is_refused_before_simulation(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    spec["analyses"] = [
        {
            "id": "long_transient",
            "kind": "tran",
            "step": "1n",
            "stop": "100u",
        }
    ]
    spec["observations"] = [
        {
            "id": "output_v",
            "analysis_id": "long_transient",
            "quantity": {"kind": "node_voltage", "net": "OUT"},
        }
    ]
    spec["measurements"] = []
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.analysis.over_limit",
        expected_path="/analyses/0",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


def test_transient_max_step_is_included_in_extractor_point_preflight(
    tmp_path,
    monkeypatch,
):
    if not _validation_assets_available():
        pytest.skip("the IHP PDK and SourceFollower acceptance bundle are required")
    spec = gain_spec()
    spec["analyses"] = [
        {
            "id": "max_step_limited",
            "kind": "tran",
            "step": "10u",
            "stop": "1m",
            "max_step": "1n",
        }
    ]
    spec["observations"] = [
        {
            "id": "output_v",
            "analysis_id": "max_step_limited",
            "quantity": {"kind": "node_voltage", "net": "OUT"},
        }
    ]
    spec["measurements"] = []
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.analysis.over_limit",
        expected_path="/analyses/0",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )


SIMRA_ROOT = Path("/home/specialpedrito/simra/simra")
SIMRA_PYTHON = SIMRA_ROOT / ".venv" / "bin" / "python"
SIZED_FOLLOWER_SOURCE = (
    SIMRA_ROOT / "plugins" / "schematic" / "examples" / "sized-source-follower.ord"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_partial_source_follower(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source_text = SIZED_FOLLOWER_SOURCE.read_text(encoding="utf-8")
    resolved = (
        'Nmos M_BIAS: .$model="nmos.core"; .$w=10u; .$l=180n; '
        ".$m=1; .$nf=1;"
    )
    partial = (
        'Nmos M_BIAS: .$model="nmos.core"; .$l=180n; .$m=1; .$nf=1;'
    )
    assert source_text.count(resolved) == 1
    source_path = tmp_path / "partial-source-follower.ord"
    source_path.write_text(source_text.replace(resolved, partial), encoding="utf-8")
    bundle_dir = tmp_path / "partial-source-follower"
    completed = subprocess.run(
        [
            str(SIMRA_PYTHON),
            "-m",
            "plugins.schematic",
            "compile",
            str(source_path),
            "--top",
            "SourceFollower",
            "--output",
            str(bundle_dir),
            "--allow-code-execution",
            "--contract-version",
            "v2",
        ],
        cwd=SIMRA_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    descriptor_path = bundle_dir / "schematic.artifact.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    return descriptor_path, descriptor


def test_compiler_authored_display_candidate_dut_is_not_promoted(
    tmp_path,
    monkeypatch,
):
    if not (
        SIMRA_PYTHON.is_file()
        and SIZED_FOLLOWER_SOURCE.is_file()
        and (PDK_ROOT / "ihp-sg13g2").exists()
    ):
        pytest.skip("the Simra compiler runtime and real IHP PDK are required")
    descriptor_path, descriptor = _compile_partial_source_follower(tmp_path)
    assert descriptor["kind"] == "schematic"
    assert descriptor["netlistable"] is True
    assert descriptor["lifecycle"]["state"] == "display_candidate"
    assert {
        "instance": "M_BIAS",
        "parameter": "w",
    } in descriptor["lifecycle"]["unresolved"]
    assert (
        "emitted netlist contains unresolved placeholder tokens"
        in descriptor["lifecycle"]["blockers"]
    )
    assert "SIMRA_UNRESOLVED_M_BIAS_W" in (
        descriptor_path.parent / "design.spice"
    ).read_text(encoding="utf-8")

    spec = gain_spec()
    spec["dut"]["artifact"] = str(descriptor_path)
    spec["dut"]["bundle"] = {
        "descriptor_sha256": _sha256(descriptor_path),
        **descriptor["hashes"],
    }
    _assert_refused_without_execution(
        spec=spec,
        expected_code="experiment.dut.not_promoted",
        expected_path="/dut/artifact",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
