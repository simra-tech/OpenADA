from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from openada.cli import (
    MAX_PREFLIGHT_PATH_CHARS,
    MAX_PREFLIGHT_PDK_ROOTS,
    MAX_PREFLIGHT_TOOL_OVERRIDES,
    main,
)
from openada.discovery import DiscoveryManager, TOOL_SPECS
from openada.preflight import PREFLIGHT_SPECS


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "schemas" / "result-v0alpha1.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

ASSERTIONS = {
    "schematic-netlist-generated": ("xschem", "netlist"),
    "spice-analysis-evidence-valid": ("ngspice", "simulate"),
    "drc-clean": ("klayout", "drc"),
    "lvs-match": ("netgen", "lvs"),
    "rtl-structural-check-passes": ("yosys", "rtl-check"),
    "rtl-lint-clean": ("verilator", "rtl-lint"),
    "asic-netlist-synthesized": ("yosys", "synthesize"),
    "timing-constraints-satisfied": ("sta", "timing-analyze"),
}


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _payload(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _preflight_argv(
    root: Path | str,
    assertion: str,
    overrides: dict[str, Path | str],
    *,
    pdk_roots: tuple[Path | str, ...] = (),
    doctor_options: tuple[str, ...] = (),
) -> list[str]:
    argv: list[str] = ["--compact"]
    for name, path in overrides.items():
        argv.extend(("--tool-path", f"{name}={path}"))
    for pdk_root in pdk_roots:
        argv.extend(("--pdk-root", str(pdk_root)))
    argv.extend(
        (
            "doctor",
            "--project-root",
            str(root),
            "--assertion",
            assertion,
            *doctor_options,
        )
    )
    return argv


def _assert_schema_valid(payload: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    assert not errors, "\n".join(error.message for error in errors)


def test_preflight_specs_are_fixed_one_to_one_operation_mappings():
    assert {
        assertion: (spec.tool, spec.operation)
        for assertion, spec in PREFLIGHT_SPECS.items()
    } == ASSERTIONS
    assert len({spec.operation for spec in PREFLIGHT_SPECS.values()}) == len(ASSERTIONS)
    for spec in PREFLIGHT_SPECS.values():
        assert spec.operation in TOOL_SPECS[spec.tool].operations


@pytest.mark.parametrize(("assertion", "expected"), ASSERTIONS.items())
def test_preflight_probes_exactly_one_mapped_tool(
    assertion: str,
    expected: tuple[str, str],
    tmp_path: Path,
    capsys,
):
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    markers = tmp_path / "markers"
    markers.mkdir()
    overrides: dict[str, Path] = {}
    for tool in {tool for tool, _ in ASSERTIONS.values()}:
        executable = bin_dir / tool
        marker = markers / tool
        banner = (
            "Netgen 2.0 compiled on test date"
            if tool == "netgen"
            else "OpenSTA 1.0"
            if tool == "sta"
            else f"{tool} 1.0"
        )
        _write_executable(
            executable,
            (
                "from pathlib import Path\n"
                f"with Path({str(marker)!r}).open('a', encoding='utf-8') as handle:\n"
                "    handle.write('probe\\n')\n"
                f"print({banner!r})\n"
            ),
        )
        overrides[tool] = executable

    exit_code = main(_preflight_argv(project, assertion, overrides))
    payload = _payload(capsys)
    expected_tool, expected_operation = expected

    assert exit_code == 0
    assert payload["operation"] == "doctor"
    assert payload["engineering"]["status"] == "pass"
    assert "no design assertion was executed" in payload["engineering"]["summary"]
    assert set(payload["data"]["tools"]) == {expected_tool}
    assert payload["data"]["tools"][expected_tool]["version_probe"] == {
        "status": "accepted",
        "binary_identity_stable": True,
        "accepted_exit_code": 0,
    }
    preflight = payload["data"]["preflight"]
    assert preflight["assertion"] == assertion
    assert preflight["assertion_evaluated"] is False
    assert preflight["target"] == {
        "operation": expected_operation,
        "tool": expected_tool,
    }
    assert preflight["tool_ready"] is True
    assert preflight["project_inventory_performed"] is False
    assert preflight["project_collateral_enumerated"] is False
    assert payload["data"]["pdks"] == []
    assert preflight["pdk"]["catalog_enumerated"] is False
    assert (markers / expected_tool).read_text(encoding="utf-8") == "probe\n"
    assert not any(
        (markers / tool).exists() for tool in overrides if tool != expected_tool
    )
    _assert_schema_valid(payload)


def test_preflight_does_not_inventory_project_or_pdk_catalog(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "private-project"
    project.mkdir()
    (project / "private-design-name.sch").write_text("secret", encoding="utf-8")
    pdk_root = tmp_path / "pdks"
    pdk_root.mkdir()
    (pdk_root / "private-pdk-name").mkdir()
    binary = tmp_path / "ngspice"
    _write_executable(binary, 'print("ngspice-1.0")\n')

    def forbidden_inventory(self):
        raise AssertionError(f"directory inventory is forbidden: {self}")

    monkeypatch.setattr(Path, "iterdir", forbidden_inventory)
    exit_code = main(
        _preflight_argv(
            project,
            "spice-analysis-evidence-valid",
            {"ngspice": binary},
            pdk_roots=(pdk_root,),
        )
    )
    payload = _payload(capsys)

    assert exit_code == 0
    serialized = json.dumps(payload, sort_keys=True)
    assert "private-design-name" not in serialized
    assert "private-pdk-name" not in serialized
    assert payload["data"]["pdk_roots"] == [str(pdk_root.resolve())]
    assert payload["data"]["pdks"] == []
    assert payload["data"]["preflight"]["pdk"] == {
        "applicable": True,
        "roots": [str(pdk_root.resolve())],
        "selected": None,
        "catalog_enumerated": False,
    }


def test_preflight_version_probe_is_isolated_from_project(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        "from pathlib import Path\n"
        "Path('probe-side-effect').write_text('generated', encoding='utf-8')\n"
        'print("Yosys 1.0")\n',
    )

    exit_code = main(
        _preflight_argv(
            project,
            "rtl-structural-check-passes",
            {"yosys": binary},
        )
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["engineering"]["status"] == "pass"
    assert list(project.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--project-root", "."],
        ["doctor", "--assertion", "drc-clean"],
        [
            "doctor",
            "--project-root",
            ".",
            "--assertion",
            "drc-clean",
            "--tool",
            "klayout",
        ],
        [
            "doctor",
            "--project-root",
            ".",
            "--assertion",
            "drc-clean",
            "--require",
            "klayout",
        ],
        [
            "doctor",
            "--project-root",
            ".",
            "--project-root",
            ".",
            "--assertion",
            "drc-clean",
        ],
        [
            "doctor",
            "--project-root",
            ".",
            "--assertion",
            "drc-clean",
            "--assertion",
            "lvs-match",
        ],
        [
            "doctor",
            "--project-root",
            ".",
            "--assertion",
            "drc-clean",
            "--version-timeout",
            "31",
        ],
    ],
)
def test_invalid_preflight_combinations_are_one_bounded_contract(
    argv: list[str], capsys
):
    exit_code = main(argv)
    payload = _payload(capsys)

    assert exit_code == 2
    assert payload["operation"] == "doctor"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "request.invalid"
    _assert_schema_valid(payload)


@pytest.mark.parametrize("kind", ["missing", "file", "loop", "control", "long"])
def test_preflight_rejects_invalid_project_roots(kind: str, tmp_path: Path, capsys):
    binary = tmp_path / "klayout"
    marker = tmp_path / "probed"
    _write_executable(
        binary,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('KLayout 1.0')\n",
    )
    if kind == "missing":
        project: str | Path = tmp_path / "missing"
    elif kind == "file":
        project = tmp_path / "file"
        project.write_text("not a directory", encoding="utf-8")
    elif kind == "loop":
        project = tmp_path / "loop"
        project.symlink_to(project)
    elif kind == "control":
        project = str(tmp_path / "project") + "\nprivate"
    else:
        project = "x" * (MAX_PREFLIGHT_PATH_CHARS + 1)

    exit_code = main(
        _preflight_argv(project, "drc-clean", {"klayout": binary})
    )
    payload = _payload(capsys)

    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert not marker.exists()
    _assert_schema_valid(payload)


def test_preflight_canonicalizes_symlinked_project_root(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(project, target_is_directory=True)
    binary = tmp_path / "xschem"
    _write_executable(binary, 'print("XSCHEM V1.0")\n')

    exit_code = main(
        _preflight_argv(
            link,
            "schematic-netlist-generated",
            {"xschem": binary},
        )
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["data"]["preflight"]["project_root"] == {
        "path": str(project.resolve()),
        "kind": "directory",
        "canonicalized": True,
        "identity_stable": True,
    }


@pytest.mark.parametrize("option", ["--tool-path", "--pdk-root"])
def test_preflight_rejects_unexpandable_caller_paths_as_invalid_request(
    option: str,
    tmp_path: Path,
    capsys,
):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "ngspice"
    marker = tmp_path / "probed"
    _write_executable(
        binary,
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        'print("ngspice-1.0")\n',
    )
    unknown_user_path = "~openada-user-that-must-not-exist-7f93d1/path"
    argv = ["--compact"]
    if option == "--tool-path":
        argv.extend((option, f"ngspice={unknown_user_path}"))
    else:
        argv.extend(
            (
                option,
                unknown_user_path,
                "--tool-path",
                f"ngspice={binary}",
            )
        )
    argv.extend(
        (
            "doctor",
            "--project-root",
            str(project),
            "--assertion",
            "spice-analysis-evidence-valid",
        )
    )

    exit_code = main(argv)
    payload = _payload(capsys)

    assert exit_code == 2
    assert payload["operation"] == "doctor"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "request.invalid"
    assert not marker.exists()
    _assert_schema_valid(payload)


@pytest.mark.parametrize(
    ("body", "probe_status"),
    [
        ("pass\n", "output_unparseable"),
        ("print('unrelated-mesh-generator-1.0')\n", "output_identity_mismatch"),
        (
            "print('Netgen 2.0 compiled on ' + ('x' * 600))\n",
            "output_malformed",
        ),
        ("import sys\nsys.stdout.write('x' * 13000)\n", "output_truncated"),
        (
            "import sys\nsys.stdout.buffer.write(b'Netgen invalid \\xff\\n')\n",
            "output_invalid_utf8",
        ),
    ],
)
def test_untrustworthy_version_output_cannot_make_preflight_ready(
    body: str,
    probe_status: str,
    tmp_path: Path,
    capsys,
):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "netgen"
    _write_executable(binary, body)

    exit_code = main(
        _preflight_argv(project, "lvs-match", {"netgen": binary})
    )
    payload = _payload(capsys)
    tool = payload["data"]["tools"]["netgen"]

    assert exit_code == 1
    assert payload["engineering"]["status"] == "fail"
    assert tool["status"] == "unusable"
    assert tool["version"] is None
    assert tool["version_probe"] == {
        "status": probe_status,
        "binary_identity_stable": True,
        "accepted_exit_code": None,
    }
    assert payload["data"]["preflight"]["tool_ready"] is False


def test_timed_out_version_probe_cannot_make_preflight_ready(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "ngspice"
    _write_executable(binary, "import time\ntime.sleep(5)\n")

    exit_code = main(
        _preflight_argv(
            project,
            "spice-analysis-evidence-valid",
            {"ngspice": binary},
            doctor_options=("--version-timeout", "0.05"),
        )
    )
    payload = _payload(capsys)

    assert exit_code == 1
    assert payload["data"]["tools"]["ngspice"]["version_probe"] == {
        "status": "probe_timed_out",
        "binary_identity_stable": True,
        "accepted_exit_code": None,
    }
    assert payload["data"]["preflight"]["tool_ready"] is False


def test_missing_selected_tool_is_failed_readiness_not_design_conclusion(
    tmp_path: Path, capsys
):
    project = tmp_path / "project"
    project.mkdir()

    exit_code = main(
        _preflight_argv(project, "lvs-match", {"netgen": tmp_path / "missing"})
    )
    payload = _payload(capsys)

    assert exit_code == 1
    assert payload["engineering"]["status"] == "fail"
    assert "no design assertion was executed" in payload["engineering"]["summary"]
    assert payload["data"]["tools"]["netgen"]["status"] == "missing"
    assert payload["data"]["preflight"]["assertion_evaluated"] is False
    assert payload["diagnostics"][0]["code"] == "tool.required_unavailable"


def test_project_root_replacement_during_probe_forces_unknown(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    displaced = tmp_path / "displaced"
    binary = tmp_path / "yosys"
    _write_executable(
        binary,
        "from pathlib import Path\n"
        f"project = Path({str(project)!r})\n"
        f"project.rename(Path({str(displaced)!r}))\n"
        f"Path({str(replacement)!r}).rename(project)\n"
        'print("Yosys 1.0")\n',
    )

    exit_code = main(
        _preflight_argv(
            project,
            "rtl-structural-check-passes",
            {"yosys": binary},
        )
    )
    payload = _payload(capsys)

    assert exit_code == 2
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "preflight.project_root_changed"
    assert payload["data"]["preflight"]["assertion_evaluated"] is False
    assert payload["data"]["preflight"]["project_root"]["identity_stable"] is False


def test_binary_identity_change_during_probe_prevents_ready(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "ngspice"
    _write_executable(binary, 'print("ngspice-1.0")\n')
    identities = iter(((1, 2, 3, 4, 5, 6), (1, 5, 3, 4, 5, 6)))
    monkeypatch.setattr(
        DiscoveryManager,
        "_binary_identity",
        staticmethod(lambda _binary: next(identities)),
    )

    exit_code = main(
        _preflight_argv(
            project,
            "spice-analysis-evidence-valid",
            {"ngspice": binary},
        )
    )
    payload = _payload(capsys)
    tool = payload["data"]["tools"]["ngspice"]

    assert exit_code == 1
    assert tool["status"] == "unusable"
    assert tool["version"] is None
    assert tool["version_probe"] == {
        "status": "binary_identity_changed",
        "binary_identity_stable": False,
        "accepted_exit_code": None,
    }


def test_preflight_repeated_path_inputs_are_bounded(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "yosys"
    _write_executable(binary, 'print("Yosys 1.0")\n')

    too_many_pdks: list[str] = ["--compact"]
    for index in range(MAX_PREFLIGHT_PDK_ROOTS + 1):
        too_many_pdks.extend(("--pdk-root", str(tmp_path / f"pdk-{index}")))
    too_many_pdks.extend(
        (
            "--tool-path",
            f"yosys={binary}",
            "doctor",
            "--project-root",
            str(project),
            "--assertion",
            "rtl-structural-check-passes",
        )
    )
    assert main(too_many_pdks) == 2
    pdk_payload = _payload(capsys)
    assert pdk_payload["execution"]["status"] == "invalid_request"

    too_many_overrides: list[str] = ["--compact"]
    for _ in range(MAX_PREFLIGHT_TOOL_OVERRIDES + 1):
        too_many_overrides.extend(("--tool-path", f"yosys={binary}"))
    too_many_overrides.extend(
        (
            "doctor",
            "--project-root",
            str(project),
            "--assertion",
            "rtl-structural-check-passes",
        )
    )
    assert main(too_many_overrides) == 2
    override_payload = _payload(capsys)
    assert override_payload["execution"]["status"] == "invalid_request"


def test_legacy_doctor_shape_and_pdk_catalog_behavior_remain_available(
    tmp_path: Path, capsys
):
    pdk_root = tmp_path / "pdks"
    (pdk_root / "test-pdk").mkdir(parents=True)
    binary = tmp_path / "ngspice"
    _write_executable(binary, 'print("ngspice-1.0")\n')

    exit_code = main(
        [
            "--compact",
            "--pdk-root",
            str(pdk_root),
            "--tool-path",
            f"ngspice={binary}",
            "doctor",
            "--tool",
            "ngspice",
            "--require",
            "ngspice",
            "--version-timeout",
            "31",
        ]
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert "preflight" not in payload["data"]
    assert "version_probe" not in payload["data"]["tools"]["ngspice"]
    assert payload["data"]["pdks"] == [
        {"name": "test-pdk", "root": str(pdk_root.resolve())}
    ]
