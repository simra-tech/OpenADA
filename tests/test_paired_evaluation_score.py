from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "evaluation" / "paired-agent" / "tasks" / "ihp-inverter-sim"
MANIFEST = json.loads((TASK / "manifest.json").read_text(encoding="utf-8"))
SYNTHETIC_NETLIST = b"""* synthetic representation of the frozen active deck
V1 Vin GND PULSE(0 1.2 0.5u 10n 10n 1u 2u 1)
V2 Vdd GND 1.2
x1 Vdd Vin Vout GND inverter
.control
save all
tran 50n 2u
write test_inverter.raw
.endc
.lib cornerMOSlv.lib mos_tt
.subckt inverter Vdd Vin Vout Gnd
XM1 Gnd Vin Vout Gnd sg13_lv_nmos w=1.0u l=0.45u ng=1 m=1 mm_ok=1
XM2 Vout Vin Vdd Vdd sg13_lv_pmos w=2.0u l=0.45u ng=1 m=1 mm_ok=1
.ends
.GLOBAL GND
.end
"""


def _load_scorer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_openada_paired_native_score", TASK / "native_score.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCORER = _load_scorer()


def _binary_raw(*, invert: bool = True) -> bytes:
    point_count = 80
    names = ["time", "v(vdd)", "v(vin)", "v(vout)"]
    header = (
        "Title: synthetic paired IHP inverter\n"
        "Date: fixture\n"
        "Command: ngspice-46 synthetic\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        f"No. Variables: {len(names)}\n"
        f"No. Points: {point_count}\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tv(vdd)\tvoltage\n"
        "\t2\tv(vin)\tvoltage\n"
        "\t3\tv(vout)\tvoltage\n"
        "Binary:\n"
    ).encode("ascii")
    values: list[float] = []
    for index in range(point_count):
        time = 2e-6 * index / (point_count - 1)
        vin = 1.2 if 0.5e-6 < time < 1.5e-6 else 0.0
        vout = (0.0 if vin > 0.6 else 1.2) if invert else vin
        values.extend((time, 1.2, vin, vout))
    return header + struct.pack(f"<{len(values)}d", *values)


def _write_workspace(root: Path, *, invert: bool = True) -> Path:
    workspace = root / "workspace"
    (workspace / "work").mkdir(parents=True)
    (workspace / "evidence" / "simulation").mkdir(parents=True)
    (workspace / "work" / "inverter_tb.spice").write_bytes(SYNTHETIC_NETLIST)
    (workspace / "work" / "test_inverter.raw").write_bytes(_binary_raw(invert=invert))
    (workspace / "evidence" / "simulation" / "inverter_tb.log").write_text(
        'No. of Data Rows : 80\nbinary raw file "test_inverter.raw"\nngspice-46 done\n',
        encoding="utf-8",
    )
    return workspace


def _file_record(workspace: Path, role: str, path: str) -> dict:
    payload = (workspace / path).read_bytes()
    return {
        "role": role,
        "path": path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_submission(
    root: Path,
    workspace: Path,
    *,
    status: str = "pass",
    process_completed: bool | None = True,
) -> Path:
    submission = {
        "schema": "openada.eval.submission/v0alpha1",
        "task_id": MANIFEST["id"],
        "status": status,
        "process_completed": process_completed,
        "summary": "Synthetic native evidence scored independently.",
        "tools": MANIFEST["tools"],
        "inputs": MANIFEST["inputs"],
        "artifacts": [
            _file_record(workspace, item["role"], item["path"])
            for item in MANIFEST["artifacts"]
        ],
        "limitations": ["Fixture evidence is not foundry signoff."],
    }
    path = root / "submission.json"
    path.write_text(json.dumps(submission), encoding="utf-8")
    return path


def _score(workspace: Path, submission: Path) -> dict:
    return SCORER.score_workspace(workspace, submission)


def _artifact(score: dict, role: str) -> dict:
    return next(item for item in score["artifacts"] if item["role"] == role)


def test_condition_blind_positive_score_has_exact_artifact_and_provenance_records(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)

    score = _score(workspace, submission)

    assert score["schema"] == "openada.eval.score/v0alpha1"
    assert score["task_id"] == MANIFEST["id"]
    assert score["engineering_verdict"] == "pass"
    assert score["verified_artifact_complete"] is True
    assert score["reported_status_correct"] is True
    assert all(item["status"] == "valid" for item in score["artifacts"])
    assert all(item["reported_hash_correct"] is True for item in score["artifacts"])
    assert all(score["provenance"].values())
    assert score["diagnostics"] == []
    assert "condition" not in score


def test_structurally_valid_non_inverting_waveform_is_engineering_fail(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path, invert=False)
    submission = _write_submission(tmp_path, workspace, status="fail")

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "fail"
    assert score["reported_status_correct"] is True
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "simulation-raw")["status"] == "semantic_fail"
    assert any(item["code"] == "waveform.semantic_failure" for item in score["diagnostics"])


def test_valid_waveform_with_missing_log_is_pass_but_task_incomplete(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace, process_completed=None)
    (workspace / "evidence" / "simulation" / "inverter_tb.log").unlink()

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "pass"
    assert score["reported_status_correct"] is True
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "simulation-log")["status"] == "missing"


def test_malformed_native_evidence_stays_unknown_despite_clean_log_claims(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    (workspace / "work" / "test_inverter.raw").write_bytes(b"not a raw file")
    submission = _write_submission(tmp_path, workspace, status="unknown", process_completed=None)

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "unknown"
    assert score["reported_status_correct"] is True
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "simulation-raw")["status"] == "malformed"
    assert _artifact(score, "simulation-log")["status"] == "malformed"


def test_malformed_raw_header_diagnostic_does_not_echo_participant_text(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    private_header = "private-participant-header"
    raw = workspace / "work" / "test_inverter.raw"
    raw.write_bytes(
        (
            "Title: synthetic\n"
            f"{private_header}: first\n"
            f"{private_header}: second\n"
            "Variables:\n"
        ).encode("ascii")
    )
    submission = _write_submission(
        tmp_path, workspace, status="unknown", process_completed=None
    )

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "unknown"
    assert private_header not in json.dumps(score)


@pytest.mark.parametrize("case", ["symlink", "hardlink", "oversized"])
def test_unsafe_or_oversized_raw_artifact_is_never_followed_or_scored(
    tmp_path: Path, case: str
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    raw = workspace / "work" / "test_inverter.raw"
    if case == "symlink":
        target = workspace / "work" / "outside.raw"
        target.write_bytes(_binary_raw())
        raw.unlink()
        raw.symlink_to(target)
        expected = "invalid_type"
    elif case == "hardlink":
        os.link(raw, workspace / "work" / "raw-alias")
        expected = "hardlinked"
    else:
        raw.write_bytes(b"")
        with raw.open("r+b") as handle:
            handle.truncate(
                next(
                    item["maximum_bytes"]
                    for item in MANIFEST["artifacts"]
                    if item["role"] == "simulation-raw"
                )
                + 1
            )
        expected = "oversized"

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "unknown"
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "simulation-raw")["status"] == expected
    assert _artifact(score, "simulation-raw")["sha256"] is None


def test_spoofed_submission_hash_status_and_log_do_not_override_native_score(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    submission_path = _write_submission(tmp_path, workspace)
    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    submission["status"] = "fail"
    submission["artifacts"][0]["sha256"] = "0" * 64
    submission_path.write_text(json.dumps(submission), encoding="utf-8")
    (workspace / "evidence" / "simulation" / "inverter_tb.log").write_text(
        'No. of Data Rows : 999\nbinary raw file "test_inverter.raw"\nngspice-46 done\n',
        encoding="utf-8",
    )

    score = _score(workspace, submission_path)

    assert score["engineering_verdict"] == "pass"
    assert score["reported_status_correct"] is False
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "generated-netlist")["reported_hash_correct"] is False
    assert _artifact(score, "simulation-log")["status"] == "malformed"
    assert score["provenance"]["artifact_hashes_exact"] is False


def test_malformed_netlist_does_not_override_valid_waveform_verdict(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path)
    (workspace / "work" / "inverter_tb.spice").write_bytes(b"\xff\xfe")
    submission = _write_submission(tmp_path, workspace, status="unknown", process_completed=None)

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "pass"
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "generated-netlist")["status"] == "malformed"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"tran 50n 2u\n", b""),
        (b"x1 Vdd Vin Vout GND inverter\n", b""),
        (b".ends\n", b""),
        (b".end\n", b""),
        (
            b"XM1 Gnd Vin Vout Gnd sg13_lv_nmos w=1.0u l=0.45u ng=1 m=1 mm_ok=1\n",
            b"XM1 Gnd Vin Vout Gnd sg13_lv_nmos w=1.0u l=0.45u ng=1 m=1 mm_ok=1\n"
            b"XM1 Gnd Vin Vout Gnd sg13_lv_nmos w=1.0u l=0.45u ng=1 m=1 mm_ok=1\n",
        ),
        (b".end\n", b"V3 unexpected GND 1.2\n.end\n"),
    ],
)
def test_incomplete_duplicate_or_conflicting_deck_is_not_artifact_complete(
    tmp_path: Path, old: bytes, new: bytes
) -> None:
    workspace = _write_workspace(tmp_path)
    netlist = workspace / "work" / "inverter_tb.spice"
    payload = netlist.read_bytes()
    assert old in payload
    netlist.write_bytes(payload.replace(old, new, 1))
    submission = _write_submission(tmp_path, workspace)

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "pass"
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "generated-netlist")["status"] == "semantic_fail"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"save all", "ſave all".encode("utf-8")),
        (b"x1 Vdd Vin Vout GND inverter", "x1 Vdd Vin Vout GND ınverter".encode("utf-8")),
        (b"tran 50n 2u", "tran\u200350n\u20032u".encode("utf-8")),
    ],
)
def test_unicode_confusables_and_whitespace_cannot_satisfy_native_deck_grammar(
    tmp_path: Path, old: bytes, new: bytes
) -> None:
    workspace = _write_workspace(tmp_path)
    netlist = workspace / "work" / "inverter_tb.spice"
    payload = netlist.read_bytes()
    assert old in payload
    netlist.write_bytes(payload.replace(old, new, 1))
    submission = _write_submission(tmp_path, workspace)

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "pass"
    assert score["verified_artifact_complete"] is False
    assert _artifact(score, "generated-netlist")["status"] == "malformed"


def test_submission_symlink_is_not_trusted(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path)
    real_submission = _write_submission(tmp_path, workspace)
    linked_submission = tmp_path / "linked-submission.json"
    linked_submission.symlink_to(real_submission)

    score = _score(workspace, linked_submission)

    assert score["engineering_verdict"] == "pass"
    assert score["reported_status_correct"] is None
    assert score["verified_artifact_complete"] is False
    assert score["provenance"]["submission_valid"] is False


@pytest.mark.parametrize("invalid_value", ["duplicate", "nan"])
def test_submission_rejects_duplicate_keys_and_nonfinite_json_without_echoing_content(
    tmp_path: Path, invalid_value: str
) -> None:
    workspace = _write_workspace(tmp_path)
    submission_path = _write_submission(tmp_path, workspace)
    text = submission_path.read_text(encoding="utf-8")
    if invalid_value == "duplicate":
        text = text.replace('"status": "pass"', '"status": "pass", "status": "fail"', 1)
    else:
        text = text.replace('"process_completed": true', '"process_completed": NaN', 1)
    submission_path.write_text(text, encoding="utf-8")

    score = _score(workspace, submission_path)
    encoded = json.dumps(score)

    assert score["engineering_verdict"] == "pass"
    assert score["reported_status_correct"] is None
    assert score["provenance"]["submission_valid"] is False
    assert "fail" not in encoded
    assert str(tmp_path) not in encoded


def test_submission_schema_diagnostic_does_not_echo_untrusted_fields_or_values(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    submission_path = _write_submission(tmp_path, workspace)
    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    secret = "/private/participant/path"
    submission[secret] = secret
    submission_path.write_text(json.dumps(submission), encoding="utf-8")

    score = _score(workspace, submission_path)
    encoded = json.dumps(score)

    assert score["provenance"]["submission_valid"] is False
    assert secret not in encoded


def test_deeply_nested_submission_is_bounded_and_remains_an_outcome(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path)
    submission = tmp_path / "submission.json"
    submission.write_text(
        '{"nested":' * (SCORER.MAX_JSON_DEPTH + 2)
        + "0"
        + "}" * (SCORER.MAX_JSON_DEPTH + 2),
        encoding="utf-8",
    )

    score = _score(workspace, submission)

    assert score["engineering_verdict"] == "pass"
    assert score["verified_artifact_complete"] is False
    assert score["provenance"]["submission_valid"] is False


def test_manifest_rejects_semantic_drift(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    manifest = json.loads((TASK / "manifest.json").read_text(encoding="utf-8"))
    manifest["waveform"]["vdd_max"] = 100.0
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SCORER.ScoreSetupError, match="reviewed semantics"):
        SCORER.score_workspace(workspace, submission, manifest_path)


@pytest.mark.parametrize("case", ["symlink", "hardlink", "oversized", "fifo"])
def test_manifest_must_be_bounded_stable_single_link_regular_file(
    tmp_path: Path, case: str
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    copied = tmp_path / "copied-manifest.json"
    copied.write_bytes((TASK / "manifest.json").read_bytes())
    candidate = tmp_path / "candidate-manifest.json"
    if case == "symlink":
        candidate.symlink_to(copied)
    elif case == "hardlink":
        os.link(copied, candidate)
    elif case == "oversized":
        candidate.write_bytes(b"{}")
        with candidate.open("r+b") as handle:
            handle.truncate(SCORER.MAX_SETUP_JSON_BYTES + 1)
    else:
        os.mkfifo(candidate)

    with pytest.raises(SCORER.ScoreSetupError):
        SCORER.score_workspace(workspace, submission, candidate)


@pytest.mark.parametrize("invalid_value", ["duplicate", "nan"])
def test_cli_rejects_non_strict_manifest_with_bounded_path_free_failure(
    tmp_path: Path, invalid_value: str
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    text = (TASK / "manifest.json").read_text(encoding="utf-8")
    if invalid_value == "duplicate":
        text = text.replace(
            '"schema": "openada.eval.task/v0alpha1"',
            '"schema": "openada.eval.task/v0alpha1", "schema": "bad"',
            1,
        )
    else:
        text = text.replace('"vdd_max": 1.21', '"vdd_max": NaN', 1)
    manifest = tmp_path / "private-manifest.json"
    manifest.write_text(text, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(TASK / "native_score.py"),
            "--workspace",
            str(workspace),
            "--submission",
            str(submission),
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("scoring failed: ")
    assert str(tmp_path) not in completed.stderr


def test_cli_missing_workspace_failure_does_not_echo_requested_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "private-workspace"
    submission = tmp_path / "private-submission.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(TASK / "native_score.py"),
            "--workspace",
            str(workspace),
            "--submission",
            str(submission),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "scoring failed: cannot stat the requested workspace\n"
    assert str(tmp_path) not in completed.stderr


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--workspace", "~openada-user-that-does-not-exist/workspace"),
        ("--submission", "~openada-user-that-does-not-exist/submission.json"),
        ("--manifest", "~openada-user-that-does-not-exist/manifest.json"),
    ],
)
def test_cli_unknown_user_paths_fail_without_traceback_or_path_echo(
    tmp_path: Path, argument: str, value: str
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    arguments = {
        "--workspace": str(workspace),
        "--submission": str(submission),
        "--manifest": str(TASK / "manifest.json"),
    }
    arguments[argument] = value
    argv = [sys.executable, str(TASK / "native_score.py")]
    for name, configured in arguments.items():
        argv.extend((name, configured))

    completed = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert "openada-user-that-does-not-exist" not in completed.stderr


def test_cli_manifest_parse_error_does_not_echo_private_duplicate_key(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)
    submission = _write_submission(tmp_path, workspace)
    secret = "private-secret-manifest-key"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(f'{{"{secret}":1,"{secret}":2}}', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(TASK / "native_score.py"),
            "--workspace",
            str(workspace),
            "--submission",
            str(submission),
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert secret not in completed.stderr
    assert "Traceback" not in completed.stderr
