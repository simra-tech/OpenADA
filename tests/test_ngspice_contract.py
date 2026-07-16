from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from openada.cli import main
from openada.engines.spice import MAX_SOURCE_BYTES, NgspiceDriver, NgspiceOutput


def _ascii_raw(plotname: str = "Transient Analysis") -> bytes:
    return (
        "Title: OpenADA fixture\n"
        "Date: fixture\n"
        f"Plotname: {plotname}\n"
        "Flags: real\n"
        "No. Variables: 2\n"
        "No. Points: 2\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tv(out)\tvoltage\n"
        "Values:\n"
        " 0\t0.0\n"
        "\t1.2\n"
        "\n"
        " 1\t1e-9\n"
        "\t0.0\n"
        "\n"
    ).encode("ascii")


VALID_RAW = _ascii_raw()
CONSTANTS_RAW = _ascii_raw("constants")
VALID_WRDATA = b"0.0 1.2\n1e-9 0.0\n"


def _write_fake_ngspice(
    path: Path,
    *,
    wrapper_raw: bytes = VALID_RAW,
    deck_outputs: dict[str, bytes] | None = None,
    log: str = "No. of Data Rows : 2\n",
    exit_code: int = 0,
    environment_capture: Path | None = None,
) -> None:
    encoded_wrapper = base64.b64encode(wrapper_raw).decode("ascii")
    encoded_outputs = {
        name: base64.b64encode(value).decode("ascii")
        for name, value in (deck_outputs or {}).items()
    }
    body = f"""#!/usr/bin/env python3
import base64
import json
import os
import pathlib
import re
import sys

capture_path = {str(environment_capture) if environment_capture is not None else None!r}
if capture_path is not None:
    with pathlib.Path(capture_path).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(dict(os.environ), sort_keys=True) + '\\n')

if '--version' in sys.argv or '-v' in sys.argv:
    print('ngspice-1.0')
    raise SystemExit(0)

log_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
log_path.write_text({log!r}, encoding='utf-8')

raw_path = None
if '-r' in sys.argv:
    raw_path = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
else:
    script_index = next(
        index for index, value in enumerate(sys.argv)
        if value.endswith('.openada-control.sp')
    )
    script = pathlib.Path(sys.argv[script_index]).read_text(encoding='utf-8')
    writes = re.findall(r'^\\s*write\\s+(\\S+)\\s*$', script, re.MULTILINE)
    if writes:
        raw_path = pathlib.Path(writes[-1])

if raw_path is not None:
    raw_path.write_bytes(base64.b64decode({encoded_wrapper!r}))

for relative, encoded in {encoded_outputs!r}.items():
    pathlib.Path(relative).write_bytes(base64.b64decode(encoded))

raise SystemExit({exit_code})
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _source(tmp_path: Path, body: str = ".op\n.end\n") -> Path:
    source = tmp_path / "tb.spice"
    source.write_text(body, encoding="utf-8")
    return source


def _diagnostic_codes(payload: dict) -> set[str]:
    return {item["code"] for item in payload["diagnostics"]}


def test_default_mode_uses_only_exact_streaming_batch_flags(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    command = payload["execution"]["command"]
    assert payload["engineering"]["status"] == "pass"
    assert len(command) == 7
    assert command[0] == str(binary.resolve())
    assert command[1:3] == ["-b", "-r"]
    assert Path(command[3]).name == "simulation.raw"
    assert command[4] == "-o"
    assert Path(command[5]).name == "simulation.log"
    assert command[6] == str(source.resolve())
    assert "-n" not in command
    assert payload["data"]["execution_mode"] == "batch"


def test_explicit_pdk_environment_is_allowlisted_and_recorded(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        environment_overrides={"PDK": "ihp-sg13g2", "PDK_ROOT": "/foss/pdks"},
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["requested_environment_overrides"] == {
        "PDK": "ihp-sg13g2",
        "PDK_ROOT": "/foss/pdks",
    }
    assert payload["data"]["environment"]["PDK"] == "ihp-sg13g2"
    assert payload["data"]["environment"]["PDK_ROOT"] == "/foss/pdks"
    assert payload["data"]["environment_overrides"] == {
        "PDK": "ihp-sg13g2",
        "PDK_ROOT": "/foss/pdks",
    }

    rejected = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "rejected",
        environment_overrides={"LD_PRELOAD": "/tmp/untrusted.so"},
    )
    assert rejected["execution"]["status"] == "invalid_request"
    assert rejected["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(rejected) == {"environment.invalid"}


def test_sanitized_environment_blocks_hostile_ambient_from_probe_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "ngspice"
    capture = tmp_path / "child-environments.jsonl"
    _write_fake_ngspice(binary, environment_capture=capture)
    source = _source(tmp_path)
    init_file = tmp_path / "provider-init"
    init_file.write_text("set numdgt=12\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    system_init = scripts / "spinit"
    system_init.write_text("* explicit system init\n", encoding="utf-8")
    hostile = {
        "LD_PRELOAD": "/attacker/preload.so",
        "LD_LIBRARY_PATH": "/attacker/lib",
        "PYTHONPATH": "/attacker/python",
        "PYTHONHOME": "/attacker/home",
        "HOME": "/attacker/user-home",
        "NGSPICE_INPUT_DIR": "/attacker/ngspice-input",
        "SPICE_LIB_DIR": "/attacker/spice-lib",
        "SPICE_ASCIIRAWFILE": "1",
        "TMPDIR": "/attacker/tmp",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        init_file=init_file,
        system_init_file=system_init,
        environment_overrides={"PDK": "fixture-pdk", "PDK_ROOT": str(tmp_path)},
        environment_mode="sanitized",
    )

    assert payload["engineering"]["status"] == "pass"
    observed = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(observed) == 2  # accepted version probe and simulation
    expected_names = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PDK",
        "PDK_ROOT",
        "SPICE_SCRIPTS",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
    }
    forbidden_names = set(hostile) - {"HOME", "TMPDIR"}
    for child_environment in observed:
        assert set(child_environment) == expected_names
        assert forbidden_names.isdisjoint(child_environment)
        assert child_environment["HOME"] != hostile["HOME"]
        assert child_environment["TMPDIR"] != hostile["TMPDIR"]
        assert child_environment["PDK"] == "fixture-pdk"
        assert child_environment["PDK_ROOT"] == str(tmp_path)
        assert child_environment["SPICE_SCRIPTS"] == str(scripts)
    policy = payload["data"]["environment_policy"]
    assert policy["mode"] == "sanitized"
    assert policy["ambient_inherited"] is False
    assert policy["child_variable_names"] == sorted(expected_names)
    assert policy["effective_variables"] == observed[-1]


@pytest.mark.parametrize("version_output", ["", "not actually ngspice\n"])
def test_sanitized_mode_does_not_launch_an_unverified_executable(
    tmp_path: Path,
    version_output: str,
) -> None:
    binary = tmp_path / "ngspice"
    launch_marker = tmp_path / "launched"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"version_output = {version_output!r}\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    if version_output:\n"
        "        print(version_output, end='')\n"
        "    raise SystemExit(0)\n"
        f"pathlib.Path({str(launch_marker)!r}).write_text('launched')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        environment_mode="sanitized",
    )

    assert payload["execution"]["status"] == "not_available"
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {"tool.identity_unverified"}
    assert not launch_marker.exists()


@pytest.mark.parametrize(
    "body",
    [
        ".op\n.measure op voltage find v(out)\n.end\n",
        ".control\nrun\n.endc\n.end\n",
    ],
    ids=["measure", "control"],
)
def test_batch_rejects_direct_control_only_deck_features(tmp_path, body):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    source = _source(tmp_path, body)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {"execution_mode.invalid"}


def test_runtime_batch_measurement_suppression_is_unknown(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        log=(
            "No. of Data Rows : 2\n"
            "No .measure possible in batch mode (-b) with -r rawfile set!\n"
        ),
    )
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert "measurement.unavailable" in _diagnostic_codes(payload)
    assert payload["data"]["analysis_evidence"]["raw"] is True


def test_control_mode_normalizes_plain_measurement_and_passes(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        log=(
            "No. of Data Rows : 2\n"
            "Measurements for Transient Analysis\n"
            "delay = 1.25e-09\n"
        ),
    )
    source = _source(
        tmp_path,
        ".tran 1n 2n\n"
        ".measure tran delay trig v(in) val=0.5 rise=1 "
        "targ v(out) val=0.5 fall=1\n"
        ".end\n",
    )

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["measurements"] == [
        {"name": "delay", "value": 1.25e-9, "raw": "1.25e-09"}
    ]
    assert payload["data"]["missing_measurements"] == []
    command = payload["execution"]["command"]
    assert "-b" not in command
    assert "-r" not in command
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "simulation-log",
        "ngspice-control-script",
        "ngspice-raw",
    }


def test_explicit_control_init_uses_no_init_flag_and_is_hashed(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    source = _source(tmp_path)
    init_file = tmp_path / "project-init.sp"
    init_bytes = b"set numdgt=12\n"
    init_file.write_bytes(init_bytes)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        init_file=init_file,
    )

    assert payload["engineering"]["status"] == "pass"
    assert payload["execution"]["command"].count("-n") == 1
    init_record = next(item for item in payload["inputs"] if item["kind"] == "ngspice-init")
    assert init_record["role"] == "configuration"
    assert init_record["bytes"] == len(init_bytes)
    assert init_record["sha256"] == hashlib.sha256(init_bytes).hexdigest()
    assert payload["data"]["initialization"] == {
        "policy": "explicit",
        "file": str(init_file.resolve()),
        "local_user_spiceinit": "disabled",
        "system_spinit": {
            "policy": "native-default-unenumerated",
            "file": None,
        },
        "ambient_startup_files_enumerated": False,
    }
    script_path = next(
        Path(item["path"])
        for item in payload["artifacts"]
        if item["kind"] == "ngspice-control-script"
    )
    assert f"source {init_file.resolve()}" in script_path.read_text(encoding="utf-8")


def test_control_deck_captures_only_declared_raw_and_wrdata_outputs(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        deck_outputs={
            "deck.raw": VALID_RAW,
            "deck.dat": VALID_WRDATA,
            "undeclared.sentinel": b"must remain outside result\n",
        },
    )
    source = _source(tmp_path, ".control\nrun\nwrite deck.raw\nwrdata deck.dat v(out)\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[
            NgspiceOutput("raw", "deck.raw"),
            NgspiceOutput("wrdata", "deck.dat"),
        ],
    )

    assert payload["engineering"]["status"] == "pass"
    assert [item["kind"] for item in payload["data"]["expected_outputs"]] == [
        "raw",
        "wrdata",
    ]
    assert [item["status"] for item in payload["data"]["output_captures"]] == [
        "valid",
        "valid",
    ]
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {
        "simulation-log",
        "ngspice-control-script",
        "ngspice-raw",
        "ngspice-wrdata",
    }
    captured_paths = {Path(item["path"]) for item in payload["artifacts"]}
    sentinel = tmp_path / "undeclared.sentinel"
    assert sentinel.is_file()
    assert sentinel not in captured_paths
    assert all(
        Path(item["path"]) != sentinel
        for item in payload["data"]["output_captures"]
    )
    assert "-b" not in payload["execution"]["command"]
    assert "-r" not in payload["execution"]["command"]


@pytest.mark.parametrize(
    ("state", "content", "capture_status", "validation_reason"),
    [
        ("missing", None, "missing", None),
        ("empty", b"", "empty", "file.empty"),
        ("corrupt", b"not a raw file\n", "invalid", "raw.plot_start_invalid"),
        ("constants-only", CONSTANTS_RAW, "invalid", "raw.constants_only"),
    ],
)
def test_invalid_required_deck_raw_is_unknown_and_regular_files_are_retained(
    tmp_path,
    state,
    content,
    capture_status,
    validation_reason,
):
    binary = tmp_path / "ngspice"
    outputs = {} if content is None else {"required.raw": content}
    _write_fake_ngspice(binary, deck_outputs=outputs)
    source = _source(tmp_path, ".control\nrun\nwrite required.raw\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "required.raw")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    capture = payload["data"]["output_captures"][0]
    assert capture["status"] == capture_status
    required = tmp_path / "required.raw"
    output_artifacts = [
        item for item in payload["artifacts"] if item["role"] == "output"
    ]
    if content is None:
        assert not required.exists()
        assert output_artifacts == []
    else:
        assert required.is_file()
        assert required.read_bytes() == content
        assert [Path(item["path"]) for item in output_artifacts] == [required]
        assert output_artifacts[0]["sha256"] == hashlib.sha256(content).hexdigest()
        assert capture["validation"]["reason"] == validation_reason
    assert "artifact.missing" in _diagnostic_codes(payload) or {
        "artifact.empty",
        "artifact.invalid",
    } & _diagnostic_codes(payload)


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "symlink",
        "directory",
        "missing-parent",
        "duplicate",
        "absolute",
        "escape",
        "glob",
        "whitespace",
        "unicode",
        "collision",
    ],
)
def test_deck_output_paths_fail_closed_before_launch(tmp_path, case):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    source = _source(tmp_path, ".control\nrun\n.endc\n.end\n")

    if case == "stale":
        (tmp_path / "deck.raw").write_bytes(VALID_RAW)
        expected = [NgspiceOutput("raw", "deck.raw")]
    elif case == "symlink":
        (tmp_path / "target.raw").write_bytes(VALID_RAW)
        (tmp_path / "deck.raw").symlink_to("target.raw")
        expected = [NgspiceOutput("raw", "deck.raw")]
    elif case == "directory":
        (tmp_path / "deck.raw").mkdir()
        expected = [NgspiceOutput("raw", "deck.raw")]
    elif case == "missing-parent":
        expected = [NgspiceOutput("raw", "missing/deck.raw")]
    elif case == "duplicate":
        expected = [
            NgspiceOutput("raw", "deck.raw"),
            NgspiceOutput("raw", "deck.raw"),
        ]
    elif case == "absolute":
        expected = [NgspiceOutput("raw", tmp_path / "absolute.raw")]
    elif case == "escape":
        expected = [NgspiceOutput("raw", "../escaped.raw")]
    elif case == "glob":
        expected = [NgspiceOutput("raw", "deck-*.raw")]
    elif case == "whitespace":
        expected = [NgspiceOutput("raw", "deck output.raw")]
    elif case == "unicode":
        expected = [NgspiceOutput("raw", "déck.raw")]
    else:
        expected = [NgspiceOutput("raw", source.name)]

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=expected,
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) <= {"deck_output.invalid", "deck_output.not_fresh"}
    assert len(payload["diagnostics"]) == 1


def test_batch_mode_rejects_deck_owned_outputs(tmp_path):
    source = _source(tmp_path)

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
        expected_outputs=[NgspiceOutput("raw", "deck.raw")],
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(payload) == {"execution_mode.invalid"}


def test_control_deck_outputs_reject_wrapper_raw_file(tmp_path):
    source = _source(tmp_path)

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "deck.raw")],
        raw_file=tmp_path / "wrapper.raw",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(payload) == {"deck_output.invalid"}


def test_convergence_marker_without_required_raw_evidence_remains_unknown(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary, log="timestep too small; simulation stopped\n")
    source = _source(tmp_path, ".control\nrun\nwrite missing.raw\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "missing.raw")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["execution"]["exit_code"] == 0
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["converged"] is None
    assert "simulation.nonconvergent" in _diagnostic_codes(payload)
    assert "artifact.missing" in _diagnostic_codes(payload)


def test_conflicting_native_error_prevents_terminal_nonconvergence_classification(
    tmp_path,
):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        log=(
            "No. of Data Rows : 2\n"
            "timestep too small; simulation stopped\n"
            "fatal error: conflicting native failure\n"
        ),
    )
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["converged"] is None
    assert {"simulation.nonconvergent", "simulation.native_error"}.issubset(
        _diagnostic_codes(payload)
    )


def test_malformed_expect_output_cli_emits_one_json_object(tmp_path, capsys):
    exit_code = main(
        [
            "--compact",
            "simulate",
            str(tmp_path / "tb.spice"),
            "--expect-output",
            "not-a-declaration",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["operation"] == "simulate"
    assert payload["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(payload) == {"request.invalid"}


def test_repeated_expect_output_cli_preserves_order_and_emits_one_json_object(
    tmp_path,
    capsys,
):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        deck_outputs={"deck.raw": VALID_RAW, "deck.dat": VALID_WRDATA},
    )
    source = _source(tmp_path, ".control\nrun\n.endc\n.end\n")

    exit_code = main(
        [
            "--compact",
            "--tool-path",
            f"ngspice={binary}",
            "simulate",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--workdir",
            str(tmp_path),
            "--execution-mode",
            "control",
            "--expect-output",
            "raw=deck.raw",
            "--expect-output",
            "wrdata=deck.dat",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["engineering"]["status"] == "pass"
    assert [
        (item["kind"], item["declared_path"])
        for item in payload["data"]["expected_outputs"]
    ] == [("raw", "deck.raw"), ("wrdata", "deck.dat")]


def test_programmatic_mode_kind_and_output_count_validation_fail_closed(tmp_path):
    source = _source(tmp_path)
    driver = NgspiceDriver("/does/not/matter")

    invalid_mode = driver.simulate(
        source,
        tmp_path / "mode-out",
        execution_mode="automatic",
    )
    invalid_kind = driver.simulate(
        source,
        tmp_path / "kind-out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("csv", "deck.csv")],
    )
    too_many = driver.simulate(
        source,
        tmp_path / "count-out",
        execution_mode="control",
        expected_outputs=[
            NgspiceOutput("raw", f"deck-{index}.raw")
            for index in range(33)
        ],
    )

    assert invalid_mode["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(invalid_mode) == {"execution_mode.invalid"}
    assert invalid_kind["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(invalid_kind) == {"deck_output.invalid"}
    assert too_many["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(too_many) == {"deck_output.invalid"}


def test_batch_rejects_explicit_init_and_uninspectable_long_line(tmp_path):
    source = _source(tmp_path)
    init_file = tmp_path / "project.spiceinit"
    init_file.write_text("set numdgt=12\n", encoding="utf-8")

    explicit_init = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "init-out",
        init_file=init_file,
    )
    source.write_text((" " * 65_537) + ".control\n.end\n", encoding="utf-8")
    long_line = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "long-out",
    )

    assert explicit_init["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(explicit_init) == {"execution_mode.invalid"}
    assert long_line["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(long_line) == {"input.line_too_long"}


def test_ngspice_rejects_oversized_source_before_launch(tmp_path):
    source = _source(tmp_path)
    with source.open("ab") as handle:
        handle.truncate(MAX_SOURCE_BYTES + 1)

    payload = NgspiceDriver("/does/not/matter").simulate(
        source,
        tmp_path / "out",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["execution"]["command"] == []
    assert payload["inputs"] == []
    assert _diagnostic_codes(payload) == {"input.too_large"}


def test_control_rejects_source_name_ngspice_cannot_represent(tmp_path):
    source = tmp_path / "deck with spaces.cir"
    source.write_text(".op\n.end\n", encoding="utf-8")
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    assert _diagnostic_codes(payload) == {"control_path.unsupported"}


def test_post_launch_symlink_output_is_not_captured(tmp_path):
    binary = tmp_path / "ngspice"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('No. of Data Rows : 1\\n')\n"
        "pathlib.Path('target.raw').write_bytes(b'target')\n"
        "pathlib.Path('deck.raw').symlink_to('target.raw')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path, ".control\nwrite deck.raw\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("raw", "deck.raw")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["output_captures"][0]["status"] == "not_regular"
    assert all(
        Path(item["path"]) != tmp_path / "deck.raw"
        for item in payload["artifacts"]
    )


def test_wrdata_only_requires_completed_analysis_log_record(tmp_path):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(
        binary,
        deck_outputs={"deck.dat": VALID_WRDATA},
        log="deck command file completed\n",
    )
    source = _source(tmp_path, ".control\nwrdata deck.dat all\n.endc\n.end\n")

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
        expected_outputs=[NgspiceOutput("wrdata", "deck.dat")],
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["output_captures"][0]["status"] == "valid"
    assert _diagnostic_codes(payload) >= {"simulation.analysis_unproven"}


def test_generated_control_script_mutation_is_detected(tmp_path):
    binary = tmp_path / "ngspice"
    encoded_raw = base64.b64encode(VALID_RAW).decode("ascii")
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, pathlib, re, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "log.write_text('No. of Data Rows : 2\\n')\n"
        "script_index = next(i for i, v in enumerate(sys.argv) if v.endswith('.openada-control.sp'))\n"
        "script = pathlib.Path(sys.argv[script_index])\n"
        "match = re.findall(r'^\\s*write\\s+(\\S+)\\s*$', script.read_text(), re.MULTILINE)\n"
        f"pathlib.Path(match[-1]).write_bytes(base64.b64decode({encoded_raw!r}))\n"
        "script.write_text('* mutated after launch\\n')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(
        source,
        tmp_path / "out",
        execution_mode="control",
    )

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["control_script_capture"]["status"] == "modified"


def test_declared_input_mutation_prevents_pass(tmp_path):
    binary = tmp_path / "ngspice"
    encoded_raw = base64.b64encode(VALID_RAW).decode("ascii")
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, pathlib, sys\n"
        "if '--version' in sys.argv or '-v' in sys.argv:\n"
        "    print('ngspice-1.0')\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('No. of Data Rows : 2\\n')\n"
        f"pathlib.Path(sys.argv[sys.argv.index('-r') + 1]).write_bytes(base64.b64decode({encoded_raw!r}))\n"
        "pathlib.Path(sys.argv[-1]).write_text('* changed during launch\\n.end\\n')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = _source(tmp_path)

    payload = NgspiceDriver(str(binary)).simulate(source, tmp_path / "out")

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["data"]["inputs_stable"] is False
    assert _diagnostic_codes(payload) >= {"input.changed"}


def test_invalid_execution_mode_cli_is_one_invalid_request_object(tmp_path, capsys):
    exit_code = main(
        [
            "--compact",
            "simulate",
            str(tmp_path / "tb.spice"),
            "--execution-mode",
            "automatic",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["operation"] == "simulate"
    assert payload["execution"]["status"] == "invalid_request"
    assert _diagnostic_codes(payload) == {"request.invalid"}
