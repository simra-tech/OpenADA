from __future__ import annotations

import base64
import errno
import os
from pathlib import Path
import shutil

import pytest

from openada.engines.spice import NgspiceDriver, NgspiceOutput, _move_regular_output


VALID_RAW = (
    b"Title: review fixture\n"
    b"Plotname: Transient Analysis\n"
    b"Flags: real\n"
    b"No. Variables: 1\n"
    b"No. Points: 1\n"
    b"Variables:\n"
    b"0 v(out) voltage\n"
    b"Values:\n"
    b"0 1.0\n"
)


def _fake_ngspice(path: Path, *, log: str = "No. of Data Rows : 1\n") -> None:
    encoded = base64.b64encode(VALID_RAW).decode("ascii")
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, pathlib, re, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        f"log.write_text({log!r}, encoding='utf-8')\n"
        "if '-r' in sys.argv:\n"
        "    raw = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])\n"
        "else:\n"
        "    script = pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')\n"
        "    raw = pathlib.Path(re.findall(r'^\\s*write\\s+(\\S+)\\s*$', script, re.MULTILINE)[-1])\n"
        f"raw.write_bytes(base64.b64decode({encoded!r}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _source(tmp_path: Path, body: str = ".op\n.end\n", name: str = "tb.spice") -> Path:
    source = tmp_path / name
    source.write_text(body, encoding="utf-8")
    return source


def _codes(payload: dict) -> set[str]:
    return {item["code"] for item in payload["diagnostics"]}


@pytest.mark.parametrize("collision", ["raw", "log", "launcher"])
def test_wrapper_outputs_never_overwrite_explicit_init(tmp_path, collision):
    source = _source(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    paths = {
        "raw": output_dir / "tb.raw",
        "log": output_dir / "tb.log",
        "launcher": output_dir / "tb.openada-control.sp",
    }
    init_file = paths[collision]
    original = b"set numdgt=12\n"
    init_file.write_bytes(original)

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        output_dir,
        execution_mode="control",
        init_file=init_file,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert init_file.read_bytes() == original
    assert _codes(payload) == {"output.invalid"}


def test_overlong_deck_output_component_is_invalid_request(tmp_path):
    source = _source(tmp_path, ".control\nrun\n.endc\n.end\n")

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "x" * 300)],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"deck_output.invalid"}


def test_unscoped_print_cannot_satisfy_declared_measurement(tmp_path):
    binary = tmp_path / "ngspice"
    _fake_ngspice(binary, log="No. of Data Rows : 1\npeak = 42\n")
    source = _source(tmp_path, ".tran 1n 2n\n.measure tran peak max v(out)\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["measurements"] == []
    assert _codes(payload) >= {"measurement.missing"}


def test_duplicate_native_measurement_is_ambiguous(tmp_path):
    binary = tmp_path / "ngspice"
    _fake_ngspice(
        binary,
        log=(
            "No. of Data Rows : 1\n"
            "Measurements for Transient Analysis\n\n"
            "peak = 1.0\n\n"
            "Measurements for Transient Analysis\n\n"
            "peak = 2.0\n\n"
        ),
    )
    source = _source(tmp_path, ".tran 1n 2n\n.measure tran peak max v(out)\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["duplicate_measurements"] == ["peak"]
    assert _codes(payload) >= {"measurement.ambiguous"}


def test_control_launcher_embeds_safe_paths_and_forces_interactive_mode(tmp_path):
    binary = tmp_path / "ngspice"
    _fake_ngspice(binary)
    unsafe_parent = tmp_path / "parent with spaces"
    unsafe_parent.mkdir()
    source = _source(unsafe_parent, name="-deck.cir")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        workdir=unsafe_parent,
    )

    assert payload["engineering"]["status"] == "pass"
    assert "-i" in payload["execution"]["command"]
    assert payload["execution"]["command"][-1].endswith(".openada-control.sp")
    launcher = next(
        Path(item["path"])
        for item in payload["artifacts"]
        if item["kind"] == "ngspice-control-script"
    ).read_text(encoding="utf-8")
    assert "source ./-deck.cir" in launcher
    assert "$1" not in launcher and "$2" not in launcher and "$3" not in launcher


def test_pure_control_script_is_rejected_in_batch(tmp_path):
    source = _source(tmp_path, "*ng_script\nrun\nquit\n")

    payload = NgspiceDriver("/does/not/matter").simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "invalid_request"
    assert _codes(payload) == {"execution_mode.invalid"}


def test_transitive_control_is_fail_closed_before_batch_launch(tmp_path):
    marker = tmp_path / "launched"
    binary = tmp_path / "ngspice"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        f"pathlib.Path({str(marker)!r}).write_text('launched')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path, ".include hidden.inc\n.op\n.end\n")
    (tmp_path / "hidden.inc").write_text(".control\nrun\n.endc\n", encoding="utf-8")

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "invalid_request"
    assert not marker.exists()
    assert _codes(payload) == {"input.transitive_uninspected"}


def test_recovered_solver_warning_does_not_become_engineering_fail(tmp_path):
    binary = tmp_path / "ngspice"
    _fake_ngspice(
        binary,
        log=(
            "Warning: singular matrix: check node floating\n"
            "Trying gmin stepping\n"
            "Transient op finished successfully\n"
            "No. of Data Rows : 1\n"
        ),
    )
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["converged"] is True
    assert _codes(payload) == {"simulation.solver_recovered"}


def test_hardlinked_deck_output_is_not_current_run_evidence(tmp_path):
    encoded = base64.b64encode(VALID_RAW).decode("ascii")
    binary = tmp_path / "ngspice"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, os, pathlib, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('No. of Data Rows : 1\\n')\n"
        f"pathlib.Path('stale.raw').write_bytes(base64.b64decode({encoded!r}))\n"
        "os.link('stale.raw', 'deck.raw')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path, ".control\nrun\nwrite deck.raw\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "deck.raw")],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["output_captures"][0]["status"] == "hardlinked"


def test_replaced_output_parent_is_not_followed(tmp_path):
    encoded = base64.b64encode(VALID_RAW).decode("ascii")
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    binary = tmp_path / "ngspice"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, pathlib, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('No. of Data Rows : 1\\n')\n"
        "pathlib.Path('nested').rename('original-parent')\n"
        "pathlib.Path('nested').symlink_to('outside', target_is_directory=True)\n"
        f"pathlib.Path('nested/deck.raw').write_bytes(base64.b64decode({encoded!r}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path, ".control\nrun\nwrite nested/deck.raw\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "nested/deck.raw")],
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["output_captures"][0]["status"] == "parent_changed"
    assert (outside / "deck.raw").is_file()
    assert all(item["path"] != str(outside / "deck.raw") for item in payload["artifacts"])


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="native ngspice unavailable")
def test_native_nested_init_cannot_clobber_launcher_paths(tmp_path):
    source = _source(
        tmp_path,
        "V1 in 0 pulse(0 1 0 1n 1n 5n 10n)\n"
        "R1 in out 1k\n"
        "C1 out 0 1p\n"
        ".tran 0.1n 20n\n"
        ".end\n",
    )
    nested = tmp_path / "nested.sp"
    nested.write_text("*ng_script_with_params\nset numdgt=11\n", encoding="utf-8")
    init_file = tmp_path / "project.spiceinit"
    init_file.write_text(f"source {nested.name}\n", encoding="utf-8")

    payload = NgspiceDriver(shutil.which("ngspice")).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        init_file=init_file,
    )

    assert payload["engineering"]["status"] == "pass"
    assert "-i" in payload["execution"]["command"]


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="native ngspice unavailable")
def test_native_recovered_singular_matrix_is_not_engineering_failure(tmp_path):
    source = _source(
        tmp_path,
        "* floating node recovery test\n"
        "V1 in 0 1\n"
        "C1 in float 1u\n"
        ".tran 1n 10n\n"
        ".end\n",
        name="recovered.cir",
    )

    payload = NgspiceDriver(shutil.which("ngspice")).simulate(
        source,
        tmp_path / "out",
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["analysis_evidence"]["raw"] is True
    assert payload["data"]["solver_warning_count"] > 0
    assert "simulation.nonconvergent" not in _codes(payload)


def test_explicit_system_spinit_is_hashed_and_selected(tmp_path):
    binary = tmp_path / "ngspice"
    _fake_ngspice(binary)
    source = _source(tmp_path)
    system_dir = tmp_path / "system scripts"
    system_dir.mkdir()
    spinit = system_dir / "spinit"
    spinit.write_text("set num_threads=1\n", encoding="utf-8")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        system_init_file=spinit,
    )

    assert payload["engineering"]["status"] == "pass"
    record = next(item for item in payload["inputs"] if item["kind"] == "ngspice-system-init")
    assert record["path"] == str(spinit.resolve())
    assert payload["data"]["environment_overrides"] == {
        "SPICE_SCRIPTS": str(system_dir.resolve())
    }
    assert payload["data"]["initialization"]["system_spinit"] == {
        "policy": "explicit",
        "file": str(spinit.resolve()),
    }
    assert payload["execution"]["command"].count("-n") == 1


def test_cross_device_wrapper_capture_copies_from_verified_descriptor(tmp_path, monkeypatch):
    source = tmp_path / "private-output.raw"
    destination = tmp_path / "evidence" / "captured.raw"
    source.write_bytes(VALID_RAW)
    real_replace = os.replace

    def replace_with_one_cross_device_error(old, new):
        if Path(old) == source:
            raise OSError(errno.EXDEV, "cross-device fixture")
        return real_replace(old, new)

    monkeypatch.setattr(os, "replace", replace_with_one_cross_device_error)

    assert _move_regular_output(source, destination, maximum_bytes=len(VALID_RAW)) is True
    assert not source.exists()
    assert destination.read_bytes() == VALID_RAW
