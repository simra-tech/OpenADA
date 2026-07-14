#!/usr/bin/env python3
"""Execute the two OpenADA operations inside the frozen reference container."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

from common import ConformanceError, load_manifest, sha256_file


HERE = Path(__file__).resolve().parent
CONTAINER_MANIFEST = Path("/openada/conformance/ihp-inverter-ngspice/manifest.json")
MAX_RESULT_BYTES = 5 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the pinned workflow inside its container.")
    parser.add_argument("--manifest", type=Path, default=CONTAINER_MANIFEST)
    parser.add_argument("--evidence", type=Path, default=Path("/evidence"))
    return parser


def _regular_file_record(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat pinned runtime input {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise ConformanceError(f"pinned runtime input is not a nonempty regular file: {path}")
    return {
        "path": str(path),
        "bytes": metadata.st_size,
        "sha256": sha256_file(path),
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _invoke(argv: list[str], result_path: Path, *, cwd: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    # Python's subprocess cwd does not rewrite the inherited PWD variable.
    # Xschem consults both, so keep the process environment truthful.
    environment["PWD"] = str(cwd)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=240,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConformanceError(f"cannot complete OpenADA invocation {argv!r}: {exc}") from exc
    if len(completed.stdout.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        raise ConformanceError("OpenADA result exceeded the inside-runner size bound")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr[-4_000:]
        raise ConformanceError(
            f"OpenADA returned non-JSON output for {argv[-1]!r}: {exc}; stderr={detail!r}"
        ) from exc
    if not isinstance(document, dict):
        raise ConformanceError("OpenADA result root must be one JSON object")
    _write_json(result_path, document)
    if completed.returncode != 0:
        raise ConformanceError(
            f"OpenADA invocation exited with code {completed.returncode}; result retained at {result_path}"
        )
    return document


def _assert_pinned_file(record: dict[str, Any], expected: dict[str, str], label: str) -> None:
    if record["path"] != expected["path"] or record["sha256"] != expected["sha256"]:
        raise ConformanceError(
            f"{label} identity differs: expected {expected!r}, observed {record!r}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observation: dict[str, Any] = {
        "schema": "openada.ngspice-container-observation/v0alpha1",
        "pdk": {},
        "ngspice_system_init": {},
        "openada_invocations": [],
        "completed_operations": [],
    }
    try:
        manifest = load_manifest(args.manifest.resolve())
        evidence = args.evidence.resolve()
        if evidence != Path("/evidence"):
            raise ConformanceError("inside runner requires the reviewed /evidence mount target")
        home = Path(os.environ.get("HOME", ""))
        if home != Path("/tmp/openada-home"):
            raise ConformanceError("inside runner requires HOME=/tmp/openada-home")
        home.mkdir(mode=0o700)
        home_mode = home.lstat().st_mode
        if (
            not stat.S_ISDIR(home_mode)
            or home.is_symlink()
            or stat.S_IMODE(home_mode) != 0o700
        ):
            raise ConformanceError("the isolated tool HOME is not a real directory under /tmp")
        work = evidence / "work"
        simulation = evidence / "simulation"
        work.mkdir(mode=0o700)
        simulation.mkdir(mode=0o700)

        pdk = manifest["runtime"]["pdk"]
        commit_record = _regular_file_record(Path(pdk["commit_file"]["path"]))
        rcfile_record = _regular_file_record(Path(pdk["xschem_rcfile"]["path"]))
        init_record = _regular_file_record(Path(pdk["ngspice_init"]["path"]))
        system_init_expected = manifest["runtime"]["ngspice_system_init"]
        system_init_record = _regular_file_record(Path(system_init_expected["path"]))
        _assert_pinned_file(commit_record, pdk["commit_file"], "PDK COMMIT file")
        _assert_pinned_file(rcfile_record, pdk["xschem_rcfile"], "Xschem rcfile")
        _assert_pinned_file(init_record, pdk["ngspice_init"], "ngspice init file")
        _assert_pinned_file(
            system_init_record, system_init_expected, "ngspice system init file"
        )
        commit_value = Path(commit_record["path"]).read_text(encoding="ascii").strip()
        if commit_value != pdk["revision"]:
            raise ConformanceError(
                f"PDK COMMIT value is {commit_value!r}, expected {pdk['revision']!r}"
            )
        commit_record["value"] = commit_value
        observation["pdk"] = {
            "name": pdk["name"],
            "revision": pdk["revision"],
            "commit_file": commit_record,
            "xschem_rcfile": rcfile_record,
            "ngspice_init": init_record,
        }
        observation["ngspice_system_init"] = system_init_record

        python = "/usr/bin/python3"
        openada = "/openada/bin/openada"
        common_prefix = [python, openada, "--profile", "iic-osic-tools", "--compact"]
        net_args = manifest["workflow"]["netlist"]["arguments"]
        netlist_argv = [
            *common_prefix,
            "netlist",
            net_args["schematic"],
            "--output",
            net_args["output"],
            "--rcfile",
            net_args["rcfile"],
            "--timeout",
            str(net_args["timeout_seconds"]),
        ]
        netlist_cwd = Path(net_args["schematic"]).parent
        observation["openada_invocations"].append(
            {"operation": "netlist", "cwd": str(netlist_cwd), "argv": netlist_argv}
        )
        netlist_result = _invoke(
            netlist_argv,
            evidence / manifest["workflow"]["netlist"]["result_filename"],
            cwd=netlist_cwd,
        )
        if netlist_result.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("netlist result did not report engineering pass")
        observation["completed_operations"].append("netlist")

        sim_args = manifest["workflow"]["simulate"]["arguments"]
        simulate_argv = [
            *common_prefix,
            "simulate",
            sim_args["spice_file"],
            "--output-dir",
            sim_args["output_dir"],
            "--workdir",
            sim_args["workdir"],
            "--execution-mode",
            sim_args["execution_mode"],
            "--expect-output",
            sim_args["expect_output"],
            "--init-file",
            sim_args["init_file"],
            "--system-init-file",
            sim_args["system_init_file"],
            "--timeout",
            str(sim_args["timeout_seconds"]),
        ]
        observation["openada_invocations"].append(
            {"operation": "simulate", "cwd": "/evidence/work", "argv": simulate_argv}
        )
        simulate_result = _invoke(
            simulate_argv,
            evidence / manifest["workflow"]["simulate"]["result_filename"],
            cwd=work,
        )
        if simulate_result.get("engineering", {}).get("status") != "pass":
            raise ConformanceError("simulate result did not report engineering pass")
        observation["completed_operations"].append("simulate")
    except (ConformanceError, OSError, UnicodeError) as exc:
        observation["error"] = str(exc)[:4_000]
        print(json.dumps(observation, sort_keys=True))
        print(f"inside conformance run failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(observation, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
