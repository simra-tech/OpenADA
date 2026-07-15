from __future__ import annotations

import os
from pathlib import Path

import pytest

from openada.discovery import DiscoveryManager, TOOL_SPECS


def _write_executable(path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_native_discovery_uses_path_and_bounds_version(tmp_path, monkeypatch):
    binary = tmp_path / "ngspice"
    _write_executable(binary, 'print("ngspice-1.2.3")\n')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    manager = DiscoveryManager(profile="native")
    info = manager.inspect_tool("ngspice")

    assert info["status"] == "available"
    assert info["binary"] == str(binary.resolve())
    assert info["version"] == "ngspice-1.2.3"
    assert info["operations"] == ["simulate"]


@pytest.mark.parametrize(
    ("tool", "version_line", "exit_code"),
    [
        ("xschem", "XSCHEM V3.4.8RC", 1),
        ("ngspice", "** ngspice-46 : Circuit level simulation program", 0),
        ("xyce", "Xyce Release 7.10.0-opensource", 0),
    ],
)
def test_structured_discovery_accepts_pinned_integer_and_rc_versions(
    tmp_path, tool, version_line, exit_code
):
    binary = tmp_path / tool
    _write_executable(
        binary,
        f"print({version_line!r})\nraise SystemExit({exit_code})\n",
    )

    info = DiscoveryManager(
        profile="native",
        binary_overrides={tool: binary},
    ).inspect_tool(tool, include_probe_details=True)

    assert info["status"] == "available"
    assert info["version"] == version_line
    assert info["version_probe"] == {
        "status": "accepted",
        "binary_identity_stable": True,
        "accepted_exit_code": exit_code,
    }


@pytest.mark.parametrize(
    ("tool", "version_line", "stderr_line", "expected_status"),
    [
        (
            "xschem",
            "XSCHEM V3.4.8RC",
            "unexpected loader warning",
            "nonzero_probe_stderr",
        ),
        (
            "ngspice",
            "** ngspice-46 : Circuit level simulation program",
            "",
            "probe_failed",
        ),
    ],
)
def test_nonzero_version_exit_policy_is_xschem_specific_and_stderr_clean(
    tmp_path, tool, version_line, stderr_line, expected_status
):
    binary = tmp_path / tool
    _write_executable(
        binary,
        "import sys\n"
        f"print({version_line!r})\n"
        f"print({stderr_line!r}, file=sys.stderr) if {bool(stderr_line)!r} else None\n"
        "raise SystemExit(1)\n",
    )

    info = DiscoveryManager(
        profile="native",
        binary_overrides={tool: binary},
    ).inspect_tool(tool, include_probe_details=True)

    assert info["status"] == "unusable"
    assert info["version"] is None
    assert info["version_probe"] == {
        "status": expected_status,
        "binary_identity_stable": True,
        "accepted_exit_code": None,
    }


def test_explicit_pdk_roots_are_reported(tmp_path):
    pdk_root = tmp_path / "pdks"
    (pdk_root / "sky130A").mkdir(parents=True)

    payload = DiscoveryManager(profile="native", pdk_roots=[pdk_root]).get_capabilities([])

    assert payload["pdk_roots"] == [str(pdk_root.resolve())]
    assert payload["pdks"] == [{"name": "sky130A", "root": str(pdk_root.resolve())}]


def test_public_conformance_maturity_is_machine_readable():
    assert TOOL_SPECS["xschem"].maturity == "workflow-validated"
    assert TOOL_SPECS["ngspice"].maturity == "workflow-validated"
    assert TOOL_SPECS["klayout"].maturity == "workflow-validated"
    assert TOOL_SPECS["netgen"].maturity == "workflow-validated"
    assert TOOL_SPECS["xyce"].maturity == "workflow-validated"
    assert TOOL_SPECS["xyce"].operations == ("simulate",)


def test_xyce_discovery_rejects_a_zero_exit_non_xyce_banner(tmp_path):
    binary = tmp_path / "Xyce"
    _write_executable(binary, 'print("generic simulator 7.10")\n')

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"xyce": binary},
    ).inspect_tool("xyce", include_probe_details=True)

    assert info["status"] == "unusable"
    assert info["version"] is None
    assert info["version_probe"]["status"] == "output_identity_mismatch"


def test_invalid_binary_override_does_not_fall_back_to_path(tmp_path, monkeypatch):
    binary = tmp_path / "ngspice"
    _write_executable(binary, 'print("ngspice path version")\n')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    manager = DiscoveryManager(
        profile="native",
        binary_overrides={"ngspice": tmp_path / "missing-ngspice"},
    )

    assert manager.inspect_tool("ngspice")["status"] == "missing"


def test_binary_with_only_failing_version_probes_is_unusable(tmp_path):
    binary = tmp_path / "ngspice"
    _write_executable(
        binary,
        "import sys; print('loader failure', file=sys.stderr); raise SystemExit(126)\n",
    )

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"ngspice": binary},
    ).inspect_tool("ngspice")

    assert info["status"] == "unusable"
    assert info["version"] is None


def test_netgen_uses_headless_batch_version_probe(tmp_path):
    binary = tmp_path / "netgen"
    _write_executable(
        binary,
        """import sys
if sys.argv[1:] == ['-batch']:
    print('Netgen 1.5.321 compiled on test date')
    raise SystemExit(0)
print('display unavailable', file=sys.stderr)
raise SystemExit(1)
""",
    )

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"netgen": binary},
    ).inspect_tool("netgen")

    assert info["status"] == "available"
    assert info["version"] == "Netgen 1.5.321 compiled on test date"


def test_netgen_discovers_debian_netgen_lvs_binary(tmp_path, monkeypatch):
    binary = tmp_path / "netgen-lvs"
    _write_executable(
        binary,
        """import sys
if sys.argv[1:] == ['-batch']:
    print('Netgen 1.5.133 compiled on Debian test date')
    raise SystemExit(0)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    info = DiscoveryManager(profile="native").inspect_tool("netgen")

    assert info["status"] == "available"
    assert info["binary"] == str(binary.resolve())
    assert info["version"] == "Netgen 1.5.133 compiled on Debian test date"


def test_netgen_prefers_lvs_binary_when_generic_name_collides(tmp_path, monkeypatch):
    generic = tmp_path / "netgen"
    _write_executable(generic, 'print("unrelated mesh generator")\n')
    lvs = tmp_path / "netgen-lvs"
    _write_executable(
        lvs,
        """import sys
if sys.argv[1:] == ['-batch']:
    print('Netgen 1.5.133 compiled on Debian test date')
    raise SystemExit(0)
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    info = DiscoveryManager(profile="native").inspect_tool("netgen")

    assert info["status"] == "available"
    assert info["binary"] == str(lvs.resolve())
    assert info["version"] == "Netgen 1.5.133 compiled on Debian test date"


def test_netgen_rejects_generic_only_path_collision(tmp_path, monkeypatch):
    generic = tmp_path / "netgen"
    generic.write_text(
        "#!/usr/bin/python3\nprint('unrelated-mesh-generator-1.0')\n",
        encoding="utf-8",
    )
    generic.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    info = DiscoveryManager(profile="native").inspect_tool(
        "netgen", include_probe_details=True
    )

    assert info["status"] == "unusable"
    assert info["binary"] == str(generic.resolve())
    assert info["version"] is None
    assert info["version_probe"] == {
        "status": "output_identity_mismatch",
        "binary_identity_stable": True,
        "accepted_exit_code": None,
    }


def test_netgen_vlsi_banner_is_not_locked_to_major_version_one(tmp_path):
    binary = tmp_path / "netgen-lvs"
    _write_executable(binary, 'print("Netgen 2.0 compiled on future test date")\n')

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"netgen": binary},
    ).inspect_tool("netgen", include_probe_details=True)

    assert info["status"] == "available"
    assert info["version"] == "Netgen 2.0 compiled on future test date"
    assert info["version_probe"] == {
        "status": "accepted",
        "binary_identity_stable": True,
        "accepted_exit_code": 0,
    }


def test_version_probe_cannot_leave_files_in_workspace(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "ngspice"
    _write_executable(
        binary,
        """import pathlib
pathlib.Path('probe-side-effect').write_text('generated')
print('ngspice-1.0')
""",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"ngspice": binary},
    ).inspect_tool("ngspice")

    assert info["status"] == "available"
    assert not (workspace / "probe-side-effect").exists()


@pytest.mark.parametrize(
    ("body", "expected_probe_status"),
    [
        ("pass\n", "output_unparseable"),
        ("print('unrelated-tool 1.0')\n", "output_identity_mismatch"),
        ("print('ngspice-1.0 ' + ('x' * 600))\n", "output_malformed"),
        ("import sys; sys.stdout.write('x' * 13000)\n", "output_truncated"),
        (
            "import sys; sys.stdout.buffer.write(b'ngspice \\xff invalid\\n')\n",
            "output_invalid_utf8",
        ),
    ],
)
def test_version_discovery_requires_complete_parseable_utf8(
    tmp_path, body, expected_probe_status
):
    binary = tmp_path / "ngspice"
    _write_executable(binary, body)

    info = DiscoveryManager(
        profile="native",
        binary_overrides={"ngspice": binary},
    ).inspect_tool("ngspice", include_probe_details=True)

    assert info["status"] == "unusable"
    assert info["version"] is None
    assert info["version_probe"] == {
        "status": expected_probe_status,
        "binary_identity_stable": True,
        "accepted_exit_code": None,
    }


def test_capability_discovery_can_skip_pdk_catalog_inventory(tmp_path, monkeypatch):
    pdk_root = tmp_path / "pdks"
    (pdk_root / "secret-pdk-name").mkdir(parents=True)

    def forbidden_inventory(self):
        raise AssertionError(f"unexpected inventory of {self}")

    monkeypatch.setattr(Path, "iterdir", forbidden_inventory)
    payload = DiscoveryManager(profile="native", pdk_roots=[pdk_root]).get_capabilities(
        [], enumerate_pdks=False
    )

    assert payload["pdk_roots"] == [str(pdk_root.resolve())]
    assert payload["pdks"] == []


def test_pdk_discovery_omits_control_character_environment_path(tmp_path, monkeypatch):
    pdk_root = tmp_path / "pdk\nprivate"
    pdk_root.mkdir()
    monkeypatch.setenv("PDK_ROOT", str(pdk_root))

    payload = DiscoveryManager(profile="native").get_capabilities(
        [], enumerate_pdks=False
    )

    assert str(pdk_root.resolve()) not in payload["pdk_roots"]
