from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_HEX = "28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
MANIFEST_DIGEST = "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
PODMAN_REPO_DIGEST = f"docker.io/hpretl/iic-osic-tools@{MANIFEST_DIGEST}"


CASES = (
    ("ihp_inverter", "conformance/ihp-inverter/common.py", "conformance/ihp-inverter/manifest.json"),
    (
        "ihp_analog_measurements",
        "conformance/ihp-analog-measurements/common.py",
        "conformance/ihp-analog-measurements/manifest.json",
    ),
    (
        "ihp_inverter_agent",
        "conformance/ihp-inverter-agent-chain/common.py",
        "conformance/ihp-inverter-agent-chain/manifest.json",
    ),
    (
        "ihp_ngspice",
        "conformance/ihp-inverter-ngspice/common.py",
        "conformance/ihp-inverter-ngspice/manifest.json",
    ),
    ("ihp_sar_rtl", "conformance/ihp-sar-rtl/common.py", "conformance/ihp-sar-rtl/manifest.json"),
    (
        "public_spice_portability",
        "conformance/public-spice-portability/common.py",
        "conformance/public-spice-portability/manifest.json",
    ),
    (
        "orfs_ibex",
        "conformance/orfs-ibex-synthesis-timing/common.py",
        "conformance/orfs-ibex-synthesis-timing/manifest.json",
    ),
)

RUNNERS = (
    "conformance/ihp-inverter/run.py",
    "conformance/ihp-analog-measurements/run.py",
    "conformance/ihp-inverter-agent-chain/run.py",
    "conformance/ihp-ngspice-provider-analyses/replay.py",
    "conformance/ihp-sar-rtl/run.py",
    "conformance/public-spice-portability/run.py",
    "conformance/orfs-ibex-synthesis-timing/run.py",
)


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_openada_test_{name}", ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(*, repository: str = PODMAN_REPO_DIGEST) -> dict[str, object]:
    return {
        "Id": CONFIG_HEX,
        "Digest": MANIFEST_DIGEST,
        "RepoDigests": [repository],
        "Os": "linux",
        "Architecture": "amd64",
    }


def _stub_inspection(monkeypatch: pytest.MonkeyPatch, module: ModuleType, record: dict[str, object]) -> None:
    result = SimpleNamespace(stdout=json.dumps([record]))
    monkeypatch.setattr(module, "run_checked", lambda argv: result)
    shared = getattr(module, "_shared", None)
    if shared is not None:
        monkeypatch.setattr(shared, "run_checked", lambda argv: result)


@pytest.mark.parametrize(("name", "common_path", "manifest_path"), CASES)
def test_podman_image_identity_is_canonicalized(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    common_path: str,
    manifest_path: str,
) -> None:
    module = _load(name, common_path)
    manifest = json.loads((ROOT / manifest_path).read_text(encoding="utf-8"))
    _stub_inspection(monkeypatch, module, _record())

    inspected = module.inspect_image("podman", manifest)

    assert inspected["Id"] == f"sha256:{CONFIG_HEX}"


@pytest.mark.parametrize(("name", "common_path", "manifest_path"), CASES)
def test_same_digest_from_unrelated_repository_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    common_path: str,
    manifest_path: str,
) -> None:
    module = _load(name, common_path)
    manifest = json.loads((ROOT / manifest_path).read_text(encoding="utf-8"))
    _stub_inspection(
        monkeypatch,
        module,
        _record(repository=f"docker.io/org.example/unrelated@{MANIFEST_DIGEST}"),
    )

    with pytest.raises(module.ConformanceError, match="digest"):
        module.inspect_image("podman", manifest)


@pytest.mark.parametrize("runner_path", RUNNERS)
def test_rootless_podman_uses_single_id_user_mapping(runner_path: str) -> None:
    path = ROOT / runner_path
    program = f"""
import importlib.util
import json
import sys
from pathlib import Path
path = Path({str(path)!r})
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location('_openada_runner_test', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({{
    'podman': module._container_user_args('podman'),
    'absolute_podman': module._container_user_args('/usr/bin/podman'),
    'docker': module._container_user_args('docker'),
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "podman": ["--user", "0:0"],
        "absolute_podman": ["--user", "0:0"],
        "docker": ["--user", f"{os.getuid()}:{os.getgid()}"],
    }
