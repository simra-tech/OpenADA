#!/usr/bin/env python3
"""Run the reviewed public-IHP OP/DC/AC/TRAN provider replay in-container."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


EVIDENCE = Path("/evidence")
OPENADA = Path("/openada")
DESIGN = Path("/design")
PROVIDER_MANIFEST = OPENADA / "providers/ngspice-pdk-control/driver-manifest.json"
PDK_COMMIT = Path("/foss/pdks/ihp-sg13g2/COMMIT")
NGSPICE = Path("/foss/tools/ngspice/bin/ngspice")
NGSPICE_INIT = Path("/foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit")
NGSPICE_SYSTEM_INIT = Path("/foss/tools/ngspice/share/ngspice/scripts/spinit")
INVERTER_SCHEMATIC = DESIGN / "modules/module_0_foundations/inverter/inverter_tb.sch"
OTA_SCHEMATIC = (
    DESIGN
    / "modules/module_1_bandgap_reference/part_1_OTA/testbenches/ota_testbench.sch"
)
PDK_SHA256 = "9d288516f92afa199f28b8541a42574112147c16b1cec1f4082b13c4e43163c5"
EXECUTABLE_SHA256 = "6aacaca88f656e5e19074ac070fb410bf6cc437df1de88ec28d50a24c6239a1b"
INIT_SHA256 = "56ec1880a943fa481c3c321d62857b6240387e39d5aa8ded403835c34edb515d"
SYSTEM_INIT_SHA256 = "b088c11a27e21ceadb14abbf9dff877105177bd025ca37750877d71e7f6f87af"
FEATURES = {
    "op": "openada.feature/simulation.analysis.op/v1alpha1",
    "dc": "openada.feature/simulation.analysis.dc/v1alpha1",
    "ac": "openada.feature/simulation.analysis.ac/v1alpha1",
    "tran": "openada.feature/simulation.analysis.tran/v1alpha1",
}
ANALYSES: dict[str, dict[str, Any]] = {
    "op": {"type": "op", "extensions": {}},
    "dc": {
        "type": "dc",
        "source_name": "V1",
        "source_unit": "V",
        "start": 0.0,
        "stop": 1.2,
        "step": 0.01,
        "extensions": {},
    },
    "ac": {
        "type": "ac",
        "sweep": "dec",
        "points": 100,
        "start_hz": 1.0,
        "stop_hz": 10e6,
        "extensions": {},
    },
    "tran": {
        "type": "tran",
        "step_s": 31.25e-9,
        "stop_s": 32.46875e-6,
        "start_s": 0.5e-6,
        "extensions": {},
    },
}
DIRECTIVES = {
    "op": "op",
    "dc": "dc V1 0 1.2 0.01",
    "ac": "ac dec 100 1 10e6",
    "tran": "tran 31.25n 32.46875u 0.5u",
}
REQUEST_IDS = {
    "op": "11000000-0000-4000-8000-000000000001",
    "dc": "11000000-0000-4000-8000-000000000002",
    "ac": "11000000-0000-4000-8000-000000000003",
    "tran": "11000000-0000-4000-8000-000000000004",
}


class ReplayError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def invoke(
    argv: list[str],
    *,
    cwd: Path,
    allowed: set[int] = {0},
    home: Path = Path("/tmp/openada-home"),
) -> dict[str, Any]:
    child_environment = dict(os.environ)
    child_environment.update(
        {
            "HOME": str(home),
            "TMPDIR": "/tmp",
            "PATH": "/openada/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PDK": "ihp-sg13g2",
            "PDK_ROOT": "/foss/pdks",
            "PWD": str(cwd),
        }
    )
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
        env=child_environment,
    )
    if completed.returncode not in allowed:
        raise ReplayError(
            f"command returned {completed.returncode}: {argv!r}; "
            f"stderr={completed.stderr[-2000:]!r}; stdout={completed.stdout[-2000:]!r}"
        )
    if completed.stderr:
        raise ReplayError(f"command emitted stderr: {completed.stderr[-2000:]!r}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"command returned non-JSON: {completed.stdout[-2000:]!r}") from exc
    if not isinstance(value, dict):
        raise ReplayError("command result must be a JSON object")
    return value


def openada_prefix() -> list[str]:
    return [
        "/usr/bin/python3",
        "/openada/bin/openada",
        "--profile",
        "iic-osic-tools",
        "--compact",
    ]


def netlist(schematic: Path, output: Path, rcfile: Path, result_path: Path) -> None:
    isolated_home = Path("/tmp/openada-home")
    result = invoke(
        [
            *openada_prefix(),
            "netlist",
            str(schematic),
            "--output",
            str(output),
            "--rcfile",
            str(rcfile),
            "--timeout",
            "120",
        ],
        cwd=schematic.parent,
        home=isolated_home,
    )
    write_json(result_path, result)
    if result.get("engineering", {}).get("status") != "pass":
        raise ReplayError(f"Xschem failed for {schematic}")


def closed_deck(source: Path, output: Path, analysis: str) -> None:
    text = source.read_text(encoding="utf-8", errors="strict")
    without_controls, count = re.subn(
        r"(?ims)^\s*\.control\s*$.*?^\s*\.endc\s*$\n?", "", text
    )
    expected = 2 if analysis == "ac" else 1
    if count != expected:
        raise ReplayError(
            f"{source} has {count} control blocks; expected {expected} for {analysis}"
        )
    if analysis == "tran":
        without_controls, pulse_count = re.subn(
            r"(?im)^V1\s+Vin\s+GND\s+PULSE\([^\n]+\)$",
            "V1 Vin GND PULSE(0 1.2 0.5u 10n 10n 0.98u 2u)",
            without_controls,
        )
        if pulse_count != 1:
            raise ReplayError("inverter pulse source no longer matches the reviewed transform")
    command_lines = ["save all", DIRECTIVES[analysis]]
    if analysis == "tran":
        command_lines.append("linearize")
    command_lines.append(f"write {analysis}.raw")
    control = "\n.control\n" + "\n".join(command_lines) + "\n.endc\n\n"
    transformed, end_count = re.subn(
        r"(?im)^\s*\.end\s*$", control + ".end", without_controls
    )
    if end_count != 1:
        raise ReplayError(f"{source} does not have exactly one top-level .end")
    output.write_text(transformed, encoding="utf-8")


def provider_config(path: Path) -> None:
    write_json(
        path,
        {
            "schema": "openada.ngspice-provider-config/v0alpha1",
            "init_file": {"path": str(NGSPICE_INIT), "sha256": INIT_SHA256},
            "system_init_file": {
                "path": str(NGSPICE_SYSTEM_INIT),
                "sha256": SYSTEM_INIT_SHA256,
            },
            "environment": {"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
            "extensions": {},
        },
    )


def request(
    deck: Path,
    config: Path,
    analysis: str,
    destination: Path,
    *,
    request_id: str | None = None,
    required_feature: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "openada.request/v0alpha1",
        "request_id": request_id or REQUEST_IDS[analysis],
        "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
        "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
        "target": {
            "kind": "testbench",
            "locator": {
                "type": "filesystem",
                "path": str(deck),
                "sha256": sha256(deck),
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
                    "sha256": sha256(config),
                    "extensions": {},
                },
                "extensions": {},
            },
            {
                "role": "pdk",
                "required": True,
                "locator": {
                    "type": "filesystem",
                    "path": str(PDK_COMMIT),
                    "sha256": PDK_SHA256,
                    "extensions": {},
                },
                "extensions": {},
            },
        ],
        "parameters": {
            "analysis": dict(parameters or ANALYSES[analysis]),
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
            "locator": {
                "type": "filesystem",
                "path": str(destination),
                "extensions": {},
            },
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
            "required_features": [required_feature or FEATURES[analysis]],
            "extensions": {},
        },
        "extensions": {},
    }


def invoke_provider(request_path: Path, result_path: Path, *, allowed: set[int]) -> dict[str, Any]:
    value = invoke(
        [
            *openada_prefix(),
            "provider",
            "invoke",
            "--manifest",
            str(PROVIDER_MANIFEST),
            "--cwd",
            str(OPENADA),
            str(request_path),
        ],
        cwd=EVIDENCE,
        allowed=allowed,
    )
    write_json(result_path, value)
    return value


def negative_deck(source: Path, output: Path, analysis: str) -> None:
    text = source.read_text(encoding="utf-8")
    if analysis == "op":
        text = text.replace("op\nwrite op.raw", "op\nshell echo forbidden\nwrite op.raw")
    elif analysis == "tran":
        text = text.replace("write tran.raw", "write tran.raw\nwrite duplicate.raw")
    else:
        raise ReplayError(f"no deck mutation for {analysis}")
    if text == source.read_text(encoding="utf-8"):
        raise ReplayError(f"negative deck mutation did not apply for {analysis}")
    output.write_text(text, encoding="utf-8")


def native_failure_deck(source: Path, output: Path) -> None:
    text = source.read_text(encoding="utf-8")
    marker = re.search(r"(?im)^\s*\.control\s*$", text)
    if marker is None or "__openada_missing_subckt" in text:
        raise ReplayError("native failure transform did not match")
    injection = "X_OPENADA_NATIVE Vout __openada_missing_subckt\n"
    output.write_text(text[: marker.start()] + injection + text[marker.start() :], encoding="utf-8")


def main() -> int:
    if EVIDENCE.resolve() != EVIDENCE or not EVIDENCE.is_dir():
        raise ReplayError("/evidence must be a mounted directory")
    home = Path("/tmp/openada-home")
    home.mkdir(mode=0o700)
    expected_files = {
        PDK_COMMIT: PDK_SHA256,
        NGSPICE: EXECUTABLE_SHA256,
        NGSPICE_INIT: INIT_SHA256,
        NGSPICE_SYSTEM_INIT: SYSTEM_INIT_SHA256,
    }
    for path, expected in expected_files.items():
        if sha256(path) != expected:
            raise ReplayError(f"runtime input drift: {path}")

    for name in ("generated", "decks", "requests", "results", "negative", "native"):
        (EVIDENCE / name).mkdir(mode=0o700)
    ota_rc = Path("/ota-rc")
    inverter_rc = Path("/inverter-rc")
    inverter_source = EVIDENCE / "generated/inverter-source"
    inverter_source.mkdir(mode=0o700)
    for source in (
        INVERTER_SCHEMATIC,
        INVERTER_SCHEMATIC.parent / "inverter.sch",
        INVERTER_SCHEMATIC.parent / "inverter.sym",
        Path("/foss/pdks/ihp-sg13g2/libs.tech/xschem/sg13g2_pr/sg13_lv_nmos.sym"),
        Path("/foss/pdks/ihp-sg13g2/libs.tech/xschem/sg13g2_pr/sg13_lv_pmos.sym"),
    ):
        (inverter_source / source.name).write_bytes(source.read_bytes())
    # Xschem 3.4.8RC does not resolve the public schematic's prefixed PDK
    # symbol names reliably in an isolated, freshly created HOME.  Relocate
    # only those two symbol locators to the content-identical local copies;
    # instance connectivity and all device parameters remain byte-for-byte.
    local_inverter = inverter_source / "inverter.sch"
    local_text = local_inverter.read_text(encoding="utf-8")
    local_text, relocated = re.subn(
        r"\{sg13g2_pr/(sg13_lv_[np]mos\.sym)\}", r"{\1}", local_text
    )
    if relocated != 2:
        raise ReplayError("inverter PDK symbol relocation no longer matches two devices")
    local_inverter.write_text(local_text, encoding="utf-8")
    inverter_netlist = EVIDENCE / "generated/inverter.spice"
    ota_netlist = EVIDENCE / "generated/ota.spice"
    netlist(OTA_SCHEMATIC, ota_netlist, ota_rc, EVIDENCE / "results/netlist-ota.json")
    netlist(
        inverter_source / "inverter_tb.sch",
        inverter_netlist,
        inverter_rc,
        EVIDENCE / "results/netlist-inverter.json",
    )
    if "IS MISSING" in ota_netlist.read_text(encoding="utf-8"):
        raise ReplayError("OTA hierarchy did not resolve")

    config = EVIDENCE / "generated/provider-config.json"
    provider_config(config)
    observation: dict[str, Any] = {
        "schema": "openada.ihp-ngspice-provider-analyses-observation/v1",
        "runtime": {
            "network": "none",
            "provider_version": "0.5.0",
            "ngspice_version": "** ngspice-46 : Circuit level simulation program",
            "pdk_revision": PDK_COMMIT.read_text(encoding="ascii").strip(),
        },
        "analyses": {},
        "negatives": {},
    }
    for analysis in ("op", "dc", "ac", "tran"):
        source = ota_netlist if analysis == "ac" else inverter_netlist
        deck = EVIDENCE / f"decks/{analysis}.spice"
        closed_deck(source, deck, analysis)
        request_path = EVIDENCE / f"requests/{analysis}.json"
        destination = EVIDENCE / f"native/{analysis}"
        write_json(request_path, request(deck, config, analysis, destination))
        result_path = EVIDENCE / f"results/{analysis}.json"
        result = invoke_provider(request_path, result_path, allowed={0})
        if result.get("engineering", {}).get("status") != "pass":
            raise ReplayError(f"provider {analysis} did not report engineering pass")
        observation["analyses"][analysis] = {
            "deck": str(deck.relative_to(EVIDENCE)),
            "request": str(request_path.relative_to(EVIDENCE)),
            "result": str(result_path.relative_to(EVIDENCE)),
            "destination": str(destination.relative_to(EVIDENCE)),
        }

    # Four authoritative request/deck boundary negatives, one per feature.
    op_bad = EVIDENCE / "negative/op-unsafe.spice"
    negative_deck(EVIDENCE / "decks/op.spice", op_bad, "op")
    op_request = EVIDENCE / "negative/op-unsafe-request.json"
    write_json(
        op_request,
        request(
            op_bad,
            config,
            "op",
            EVIDENCE / "negative/native-op-unsafe",
            request_id="22000000-0000-4000-8000-000000000001",
        ),
    )
    observation["negatives"]["op-unsafe-command"] = invoke_provider(
        op_request, EVIDENCE / "negative/op-unsafe-result.json", allowed={2}
    )

    dc_parameters = dict(ANALYSES["dc"])
    dc_parameters["stop"] = 1.1
    dc_request = EVIDENCE / "negative/dc-mismatch-request.json"
    write_json(
        dc_request,
        request(
            EVIDENCE / "decks/dc.spice",
            config,
            "dc",
            EVIDENCE / "negative/native-dc-mismatch",
            request_id="22000000-0000-4000-8000-000000000002",
            parameters=dc_parameters,
        ),
    )
    observation["negatives"]["dc-parameter-mismatch"] = invoke_provider(
        dc_request, EVIDENCE / "negative/dc-mismatch-result.json", allowed={2}
    )

    ac_request = EVIDENCE / "negative/ac-feature-request.json"
    write_json(
        ac_request,
        request(
            EVIDENCE / "decks/ac.spice",
            config,
            "ac",
            EVIDENCE / "negative/native-ac-feature",
            request_id="22000000-0000-4000-8000-000000000003",
            required_feature=FEATURES["tran"],
        ),
    )
    observation["negatives"]["ac-feature-mismatch"] = invoke_provider(
        ac_request, EVIDENCE / "negative/ac-feature-result.json", allowed={2}
    )

    tran_bad = EVIDENCE / "negative/tran-duplicate.spice"
    negative_deck(EVIDENCE / "decks/tran.spice", tran_bad, "tran")
    tran_request = EVIDENCE / "negative/tran-duplicate-request.json"
    write_json(
        tran_request,
        request(
            tran_bad,
            config,
            "tran",
            EVIDENCE / "negative/native-tran-duplicate",
            request_id="22000000-0000-4000-8000-000000000004",
        ),
    )
    observation["negatives"]["tran-duplicate-write"] = invoke_provider(
        tran_request, EVIDENCE / "negative/tran-duplicate-result.json", allowed={2}
    )

    terminal_deck = EVIDENCE / "negative/tran-native-error.spice"
    native_failure_deck(EVIDENCE / "decks/tran.spice", terminal_deck)
    terminal_request = EVIDENCE / "negative/tran-native-error-request.json"
    write_json(
        terminal_request,
        request(
            terminal_deck,
            config,
            "tran",
            EVIDENCE / "negative/native-tran-error",
            request_id="22000000-0000-4000-8000-000000000005",
        ),
    )
    terminal = invoke_provider(
        terminal_request, EVIDENCE / "negative/tran-native-error-result.json", allowed={2}
    )
    if (
        terminal.get("engineering", {}).get("status") != "unknown"
        or not terminal.get("execution", {}).get("command")
        or "simulation.result.malformed"
        not in {item.get("code") for item in terminal.get("diagnostics", [])}
    ):
        raise ReplayError("native ngspice failure did not produce typed unknown evidence")
    observation["negatives"]["tran-native-error"] = terminal

    write_json(EVIDENCE / "observation.json", observation)
    sys.stdout.write(json.dumps({"status": "pass", "observation": "observation.json"}) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReplayError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"ihp-ngspice-provider-replay: {exc}\n")
        raise SystemExit(2)
