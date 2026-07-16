#!/usr/bin/env python3
"""Execute the real IHP measurement chain inside the pinned offline image."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any

from common import ConformanceError, load_manifest, sha256_file, strict_json


HERE = Path(__file__).resolve().parent
CONTAINER_HERE = Path("/openada/conformance/ihp-analog-measurements")
MAX_RESULT_BYTES = 16 * 1024 * 1024


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _regular(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
    ):
        raise ConformanceError(f"{label} is not one nonempty regular file: {path}")
    return {"path": str(path), "bytes": metadata.st_size, "sha256": sha256_file(path)}


def _invoke(
    argv: list[str],
    output: Path,
    *,
    cwd: Path,
    allowed_codes: set[int] = frozenset({0}),
) -> tuple[dict[str, Any], int]:
    environment = os.environ.copy()
    environment["PWD"] = str(cwd)
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if completed.stderr:
        raise ConformanceError(
            f"OpenADA invocation emitted ambient stderr: {completed.stderr[-4000:]!r}"
        )
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        raise ConformanceError("OpenADA result exceeded the retained-output bound")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(
            f"OpenADA returned invalid JSON: {exc}; tail={completed.stdout[-1000:]!r}"
        ) from exc
    if not isinstance(document, dict):
        raise ConformanceError("OpenADA result root must be one object")
    _write_json(output, document)
    if completed.returncode not in allowed_codes:
        raise ConformanceError(
            f"OpenADA exited {completed.returncode}, expected {sorted(allowed_codes)}: {argv!r}"
        )
    return document, completed.returncode


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise ConformanceError(f"{label} source transform expected exactly one match")
    return text.replace(old, new, 1)


def _materialize_sources(
    manifest: dict[str, Any], requests: dict[str, Any], evidence: Path
) -> dict[str, Any]:
    sources = evidence / "sources"
    sources.mkdir(exist_ok=True)
    records: dict[str, Any] = {}
    inputs = {record["path"]: record for record in manifest["design"]["inputs"]}
    for name, transform in requests["source_transforms"].items():
        source = Path("/design") / transform["source"]
        expected = inputs[transform["source"]]["sha256"]
        if sha256_file(source) != expected:
            raise ConformanceError(f"pinned {name} source differs from its manifest hash")
        upstream_copy = sources / f"{name}-upstream.sch"
        shutil.copyfile(source, upstream_copy)
        text = source.read_text(encoding="utf-8", errors="strict")
        if name == "ota":
            text = _replace_once(
                text,
                transform["old_control"],
                transform["new_control"],
                label="OTA control closure",
            )
        else:
            text = _replace_once(
                text,
                transform["old_pulse"],
                transform["new_pulse"],
                label="inverter coherent pulse",
            )
            text = _replace_once(
                text,
                transform["old_control"],
                transform["new_control"],
                label="inverter linearized control closure",
            )
        derived = evidence / transform["derived"]
        _write_text(derived, text)
        rcfile = evidence / transform["rcfile"]
        _write_text(rcfile, transform["rcfile_text"])
        records[name] = {
            "upstream": _regular(upstream_copy, label=f"{name} retained upstream source"),
            "derived": _regular(derived, label=f"{name} derived schematic"),
            "rcfile": _regular(rcfile, label=f"{name} deterministic rcfile"),
        }
    _write_json(evidence / "source-record.json", records)
    return records


def _provider_configuration(manifest: dict[str, Any]) -> dict[str, Any]:
    pins = manifest["runtime"]["extensions"]["org.openada.measurements"]
    return {
        "schema": "openada.ngspice-provider-config/v0alpha1",
        "init_file": dict(pins["ngspice_init"]),
        "system_init_file": dict(pins["ngspice_system_init"]),
        "environment": {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
        "extensions": {},
    }


def _provider_request(
    manifest: dict[str, Any],
    deck: Path,
    configuration: Path,
    analysis_record: dict[str, Any],
    *,
    request_id: str,
    destination: str,
) -> dict[str, Any]:
    pdk = manifest["runtime"]["extensions"]["org.openada.measurements"]["pdk_commit"]
    feature = analysis_record["required_feature"]
    return {
        "schema": "openada.request/v0alpha1",
        "request_id": request_id,
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "target": {
            "kind": "testbench",
            "locator": {
                "type": "filesystem",
                "path": str(deck),
                "sha256": sha256_file(deck),
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
                    "path": str(configuration),
                    "sha256": sha256_file(configuration),
                    "extensions": {},
                },
                "extensions": {},
            },
            {
                "role": "pdk",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    "path": pdk["path"],
                    "sha256": pdk["sha256"],
                    "extensions": {},
                },
                "extensions": {},
            },
        ],
        "parameters": {"analysis": deepcopy(analysis_record["analysis"]), "extensions": {}},
        "evidence_policy": {
            "required_artifact_roles": [
                "simulation.result",
                "simulation.log",
                "simulation.launcher",
            ],
            "retain_native_artifacts": True,
            "retain_native_logs": True,
            "provenance": "bounded",
            "identity_requirement": "content-digest",
            "extensions": {},
        },
        "evidence_destination": {
            "locator": {"type": "filesystem", "path": destination, "extensions": {}},
            "collision_policy": "fail-if-present",
            "extensions": {},
        },
        "execution_constraints": {
            "completion": "wait",
            "timeout_ms": 180000,
            "max_log_bytes": 16777216,
            "max_artifact_bytes": 268435456,
            "side_effects": "evidence-only",
            "extensions": {},
        },
        "driver_selector": {
            "driver_id": "org.openada.driver.ngspice-pdk-control",
            "driver_version": "0.5.0",
            "transport_id": "local-json-stdio",
            "required_features": [feature],
            "extensions": {},
        },
        "extensions": {},
    }


def _expect_result(
    result: dict[str, Any],
    *,
    engineering: str,
    diagnostic: str | None = None,
) -> None:
    if result.get("engineering", {}).get("status") != engineering:
        raise ConformanceError(
            f"result engineering status is {result.get('engineering')!r}, expected {engineering!r}"
        )
    if diagnostic is not None and diagnostic not in {
        item.get("code") for item in result.get("diagnostics", [])
    }:
        raise ConformanceError(f"result lacks required diagnostic {diagnostic!r}")


def _transfer_request(contract: dict[str, Any], metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "measurement_id": f"ota.open-loop.{metric['kind'].replace('_', '-')}",
        "input": deepcopy(contract["input"]),
        "output": deepcopy(contract["output"]),
        "interpretation": (
            "loop-gain-negative-feedback"
            if metric["kind"] == "phase_margin"
            else "forward"
        ),
        "method": deepcopy(contract["method"]),
        "metric": {"kind": metric["kind"], "unit": metric["unit"]},
        "extensions": {},
    }


def _spectral_request(contract: dict[str, Any], metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "measurement_id": f"inverter.output.{metric['kind']}",
        "signal": contract["signal"],
        "method": deepcopy(contract["method"]),
        "band": deepcopy(contract["band"]),
        "fundamental": deepcopy(contract["fundamental"]),
        "harmonics": deepcopy(contract["harmonics"]),
        "metric": {"kind": metric["kind"], "unit": "dB"},
        "standards_context": deepcopy(contract["standards_context"]),
        "extensions": {},
    }


def _negative_measurement_request(
    family: str, request: dict[str, Any], kind: str
) -> tuple[dict[str, Any], str]:
    mutated = deepcopy(request)
    if family == "transfer":
        if kind == "low_frequency_gain_db":
            mutated["metric"]["unit"] = "Hz"
            return mutated, "transfer.unit.mismatch"
        if kind == "bandwidth_3db":
            mutated["method"]["bandwidth_drop_db"] = 2.0
            return mutated, "transfer.method.unsupported"
        if kind == "unity_gain_frequency":
            mutated["method"]["crossing_policy"] = "first-falling"
            return mutated, "transfer.method.unsupported"
        mutated["interpretation"] = "forward"
        return mutated, "transfer.phase_margin.invalid_context"
    if kind == "snr":
        mutated["fundamental"]["frequency"]["value"] = 500001.0
        return mutated, "spectral.coherence.not_established"
    if kind == "sinad":
        mutated["method"]["dft_length"] = 512
        return mutated, "spectral.method.record_length_mismatch"
    if kind == "thd":
        mutated["method"]["window"] = "hann"
        return mutated, "spectral.method.unsupported"
    mutated["standards_context"]["alignment"] = "candidate"
    return mutated, "spectral.standard_context.invalid"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=CONTAINER_HERE / "manifest.json")
    parser.add_argument("--requests", type=Path, default=CONTAINER_HERE / "requests.json")
    parser.add_argument("--evidence", type=Path, default=Path("/evidence"))
    args = parser.parse_args(argv)
    observation: dict[str, Any] = {
        "schema": "openada.ihp-analog-measurement-observation/v0alpha1",
        "completed_operations": [],
        "openada_invocations": [],
        "network": "none",
    }
    try:
        evidence = args.evidence.resolve()
        if evidence != Path("/evidence"):
            raise ConformanceError("inside replay requires the canonical /evidence mount")
        manifest = load_manifest(args.manifest)
        requests = strict_json(args.requests, label="measurement request contract")
        for name in ("sources", "work", "results", "requests", "negative"):
            (evidence / name).mkdir(parents=True, exist_ok=False)
        Path("/tmp/openada-home").mkdir(parents=True, exist_ok=True)
        _materialize_sources(manifest, requests, evidence)
        observation["completed_operations"].append("materialize-pinned-sources")

        pins = manifest["runtime"]["extensions"]["org.openada.measurements"]
        prefix = [
            "/openada/bin/openada",
            "--tool-path",
            f"xschem={pins['xschem_executable']['path']}",
        ]
        netlists = {
            "ota": ("sources/ota-ac.sch", "sources/ota.xschemrc", "work/ota.cir"),
            "inverter": (
                "sources/inverter-spectral.sch",
                "sources/inverter.xschemrc",
                "work/inverter.cir",
            ),
        }
        for name, (schematic, rcfile, deck) in netlists.items():
            command = [
                *prefix,
                "netlist",
                f"/evidence/{schematic}",
                "--rcfile",
                f"/evidence/{rcfile}",
                "--output",
                f"/evidence/{deck}",
                "--timeout",
                "120",
            ]
            observation["openada_invocations"].append(command)
            result, _ = _invoke(
                command, evidence / "results" / f"{name}-netlist.json", cwd=evidence
            )
            _expect_result(result, engineering="pass")
            observation["completed_operations"].append(f"netlist:{name}")

        missing_source = (evidence / "sources/inverter-spectral.sch").read_text(
            encoding="utf-8"
        )
        _write_text(
            evidence / "sources/inverter-missing-symbol.sch",
            missing_source
            + "C {__openada_measurement_missing__.sym} 620 -100 0 0 "
            + "{name=x_openada_measurement_missing}\n",
        )
        missing_command = [
            *prefix,
            "netlist",
            "/evidence/sources/inverter-missing-symbol.sch",
            "--rcfile",
            "/evidence/sources/inverter.xschemrc",
            "--output",
            "/evidence/work/inverter-missing-symbol.cir",
            "--timeout",
            "120",
        ]
        observation["openada_invocations"].append(missing_command)
        missing, _ = _invoke(
            missing_command,
            evidence / "negative/netlist-missing-symbol.json",
            cwd=evidence,
            allowed_codes={1},
        )
        _expect_result(missing, engineering="fail", diagnostic="xschem.missing_symbol")
        observation["completed_operations"].append("netlist:missing-symbol")

        config = evidence / "requests/provider-config.json"
        _write_json(config, _provider_configuration(manifest))
        provider_manifest = "/openada/providers/ngspice-pdk-control/driver-manifest.json"
        simulations: dict[str, dict[str, Any]] = {}
        provider_cases = {
            "ac": ("work/ota.cir", "ota_ac", "24000000-0000-4000-8000-000000000001"),
            "tran": (
                "work/inverter.cir",
                "inverter_tran",
                "24000000-0000-4000-8000-000000000002",
            ),
        }
        for name, (deck_relative, analysis_name, request_id) in provider_cases.items():
            analysis = requests["native_analyses"][analysis_name]
            request_path = evidence / f"requests/provider-{name}.json"
            destination = f"/evidence/provider-{name}"
            request = _provider_request(
                manifest,
                evidence / deck_relative,
                config,
                analysis,
                request_id=request_id,
                destination=destination,
            )
            _write_json(request_path, request)
            command = [
                *prefix,
                "provider",
                "invoke",
                "--manifest",
                provider_manifest,
                "--cwd",
                "/openada",
                str(request_path),
            ]
            observation["openada_invocations"].append(command)
            result, _ = _invoke(
                command, evidence / f"results/provider-{name}.json", cwd=evidence
            )
            _expect_result(result, engineering="pass")
            simulations[name] = result
            observation["completed_operations"].append(f"provider:{name}")

            negative_request = deepcopy(request)
            if name == "ac":
                negative_request["parameters"]["analysis"]["stop_hz"] = 9000000.0
            else:
                negative_request["parameters"]["analysis"]["stop_s"] = 3.2e-5
            negative_request["request_id"] = (
                "24100000-0000-4000-8000-000000000001"
                if name == "ac"
                else "24100000-0000-4000-8000-000000000002"
            )
            negative_request["evidence_destination"]["locator"]["path"] = (
                f"/evidence/provider-{name}-negative"
            )
            negative_path = evidence / f"requests/provider-{name}-negative.json"
            _write_json(negative_path, negative_request)
            negative_command = [
                *prefix,
                "provider",
                "invoke",
                "--manifest",
                provider_manifest,
                "--cwd",
                "/openada",
                str(negative_path),
            ]
            observation["openada_invocations"].append(negative_command)
            negative_result, _ = _invoke(
                negative_command,
                evidence / f"negative/provider-{name}-parameter-mismatch.json",
                cwd=evidence,
                allowed_codes={2},
            )
            _expect_result(
                negative_result,
                engineering="unknown",
                diagnostic="simulation.request.invalid",
            )
            observation["completed_operations"].append(
                f"provider:{name}:parameter-mismatch"
            )

        extractions: dict[str, dict[str, Any]] = {}
        extraction_cases = {
            "ac": ("ota_ac", "/evidence/provider-ac/work/ota_ac.raw"),
            "tran": (
                "inverter_tran",
                "/evidence/provider-tran/work/inverter_spectral.raw",
            ),
        }
        for name, (selection_name, artifact) in extraction_cases.items():
            selection = requests["extraction"][selection_name]
            selection_path = evidence / f"requests/extract-{name}.json"
            _write_json(
                selection_path,
                {
                    "selectors": selection["selectors"],
                    "conditions": selection["conditions"],
                    "extensions": {},
                },
            )
            command = [
                *prefix,
                "extract",
                "--simulation",
                f"/evidence/results/provider-{name}.json",
                "--artifact",
                artifact,
                "--selection",
                str(selection_path),
                "--request-id",
                selection["request_id"],
            ]
            observation["openada_invocations"].append(command)
            result, _ = _invoke(
                command, evidence / f"results/extract-{name}.json", cwd=evidence
            )
            _expect_result(result, engineering="pass")
            extractions[name] = result
            observation["completed_operations"].append(f"extract:{name}")

            missing_selection = deepcopy(
                {
                    "selectors": selection["selectors"],
                    "conditions": selection["conditions"],
                    "extensions": {},
                }
            )
            missing_selection["selectors"] = [
                {
                    "native_name": "v(__openada_measurement_missing__)",
                    "output_name": "missing.real",
                    "unit": "V",
                    "component": "real",
                }
            ]
            missing_path = evidence / f"requests/extract-{name}-negative.json"
            _write_json(missing_path, missing_selection)
            missing_command = [
                *prefix,
                "extract",
                "--simulation",
                f"/evidence/results/provider-{name}.json",
                "--artifact",
                artifact,
                "--selection",
                str(missing_path),
                "--request-id",
                (
                    "21100000-0000-4000-8000-000000000001"
                    if name == "ac"
                    else "21100000-0000-4000-8000-000000000002"
                ),
            ]
            observation["openada_invocations"].append(missing_command)
            missing_result, _ = _invoke(
                missing_command,
                evidence / f"negative/extract-{name}-missing-selector.json",
                cwd=evidence,
                allowed_codes={2},
            )
            _expect_result(
                missing_result,
                engineering="unknown",
                diagnostic="series.selector.missing",
            )
            observation["completed_operations"].append(
                f"extract:{name}:missing-selector"
            )

        measurement_summary: dict[str, Any] = {
            "schema": "openada.ihp-analog-normalized-measurements/v0alpha1",
            "transfer": {},
            "spectral": {},
            "standards": requests["standards"],
        }
        for family, command_name, series_file in (
            ("transfer", "transfer", "/evidence/results/extract-ac.json"),
            ("spectral", "spectral", "/evidence/results/extract-tran.json"),
        ):
            contract = requests[family]
            for index, metric in enumerate(contract["metrics"], start=1):
                kind = metric["kind"]
                measurement = (
                    _transfer_request(contract, metric)
                    if family == "transfer"
                    else _spectral_request(contract, metric)
                )
                request_path = evidence / f"requests/{family}-{kind}.json"
                _write_json(request_path, measurement)
                command = [
                    *prefix,
                    command_name,
                    "--series",
                    series_file,
                    "--measurement",
                    str(request_path),
                    "--request-id",
                    metric["request_id"],
                ]
                observation["openada_invocations"].append(command)
                result_path = evidence / f"results/{family}-{kind}.json"
                result, _ = _invoke(command, result_path, cwd=evidence)
                _expect_result(result, engineering="pass")
                value = result["data"]["measurement"]["value"]
                if abs(value - metric["expected"]) > metric["absolute_tolerance"]:
                    raise ConformanceError(
                        f"{family} {kind} value {value!r} differs from reviewed "
                        f"reference {metric['expected']!r}"
                    )
                measurement_summary[family][kind] = {
                    "value": value,
                    "unit": result["data"]["measurement"]["unit"],
                    "result": _regular(result_path, label=f"{family} {kind} result"),
                }
                observation["completed_operations"].append(f"{family}:{kind}")

                negative_request, diagnostic = _negative_measurement_request(
                    family, measurement, kind
                )
                negative_request_path = evidence / f"requests/{family}-{kind}-negative.json"
                _write_json(negative_request_path, negative_request)
                negative_command = [
                    *prefix,
                    command_name,
                    "--series",
                    series_file,
                    "--measurement",
                    str(negative_request_path),
                    "--request-id",
                    f"{250 + (0 if family == 'transfer' else 10):03d}00000-0000-4000-8000-{index:012d}",
                ]
                observation["openada_invocations"].append(negative_command)
                negative_path = evidence / f"negative/{family}-{kind}.json"
                negative_result, _ = _invoke(
                    negative_command,
                    negative_path,
                    cwd=evidence,
                    allowed_codes={2},
                )
                _expect_result(
                    negative_result, engineering="unknown", diagnostic=diagnostic
                )
                observation["completed_operations"].append(
                    f"{family}:{kind}:negative"
                )

        _write_json(evidence / "normalized-evidence.json", measurement_summary)
        observation["completed_operations"].append("normalized-evidence")
        decision = {
            "schema": "openada.ihp-analog-engineering-decision/v0alpha1",
            "status": "proceed-to-requirements-and-pvt-review",
            "design_pass": False,
            "signoff": False,
            "ota": {
                "conclusion": "nominal-tt-open-loop-response-measured",
                "project_numeric_specification_present": False,
                "pvt_sweep_performed": False,
                "monte_carlo_or_mismatch_performed": False,
                "decision": "Do not claim a design pass; define requirements and review PVT and statistical coverage next.",
            },
            "inverter": {
                "conclusion": "high-harmonic-content-expected-for-square-wave",
                "converter_quality_metric": False,
                "decision": "Do not interpret SNR, SINAD, THD, or SFDR as ADC or DAC quality for this inverter waveform.",
            },
            "standards": deepcopy(requests["standards"]),
            "normalized_evidence_sha256": sha256_file(
                evidence / "normalized-evidence.json"
            ),
            "extensions": {},
        }
        _write_json(evidence / "engineering-decision.json", decision)
        observation["completed_operations"].append("engineering-decision")
        observation["source_record"] = _regular(
            evidence / "source-record.json", label="source record"
        )
        observation["native_artifacts"] = {
            "ota_raw": _regular(
                evidence / "provider-ac/work/ota_ac.raw", label="OTA AC raw"
            ),
            "inverter_raw": _regular(
                evidence / "provider-tran/work/inverter_spectral.raw",
                label="inverter linearized raw",
            ),
        }
    except (
        ConformanceError,
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as exc:
        observation["error"] = str(exc)[:4000]
        print(json.dumps(observation, allow_nan=False, sort_keys=True))
        print(f"inside measurement chain failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(observation, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
