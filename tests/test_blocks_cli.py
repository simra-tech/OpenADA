"""CLI envelope tests for `openada blocks ...` and `simulate --blocks`.

The one simulated run below uses a fake ngspice binary via ``--tool-path``,
following tests/test_circuit_simulate_profile.py, so no simulator install is
needed anywhere in this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from openada.cli import main


VALID_RAW = (
    "Title: blocks CLI fixture\n"
    "Date: fixture\n"
    "Plotname: Transient Analysis\n"
    "Flags: real\n"
    "No. Variables: 2\n"
    "No. Points: 2\n"
    "Variables:\n"
    "\t0\ttime\ttime\n"
    "\t1\tv(out)\tvoltage\n"
    "Values:\n"
    " 0\t0.0\n"
    "\t0.0\n"
    "\n"
    " 1\t2e-6\n"
    "\t0.8646647168\n"
    "\n"
).encode("ascii")


def _write_fake_ngspice(path: Path) -> None:
    raw = base64.b64encode(VALID_RAW).decode("ascii")
    path.write_text(
        f"""#!/usr/bin/env python3
import base64
import pathlib
import sys
if len(sys.argv) == 2 and sys.argv[1] in {{'-v', '--version'}}:
    print('ngspice-45.2')
    raise SystemExit(0)
log = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
result = pathlib.Path(sys.argv[sys.argv.index('-r') + 1])
log.write_text('No. of Data Rows : 2\\n', encoding='utf-8')
result.write_bytes(base64.b64decode({raw!r}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _rc_deck(tmp_path: Path) -> Path:
    deck = tmp_path / "rc.cir"
    deck.write_text(
        "* blocks CLI RC fixture\n"
        "V1 in 0 PULSE(0 1 0 1n 1n 1u 2u)\n"
        "R1 in out 1k\n"
        "C1 out 0 1n\n"
        ".tran 0.1u 2u\n"
        ".end\n",
        encoding="utf-8",
    )
    return deck


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_blocks_list_names_the_packaged_core_library(capsys):
    exit_code = main(["blocks", "list"])
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["schema"] == "openada.result/v0alpha1"
    assert payload["operation"] == "blocks.list"
    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "pass"
    assert "bhv-core" in payload["data"]["libraries"]


def test_blocks_show_emits_one_verified_catalog(capsys):
    exit_code = main(["blocks", "show", "bhv-core"])
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["operation"] == "blocks.show"
    assert payload["engineering"]["status"] == "pass"
    library = payload["data"]["library"]
    assert len(library["library_digest"]) == 64
    catalog = library["blocks"]
    assert [entry["block_id"] for entry in catalog] == [
        "comparator_clocked",
        "opamp_1p",
        "sw_bbm_pair",
    ]
    for entry in catalog:
        assert entry["wrapper"].startswith(f"bhv_{entry['block_id']}_v")
        assert len(entry["contract_sha256"]) == 64
        assert entry["golden_cases"]


def test_bare_blocks_invocation_is_one_named_invalid_request(capsys):
    exit_code = main(["blocks"])
    payload = _payload(capsys)

    assert exit_code == 2
    assert payload["operation"] == "blocks"
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"


def test_blocks_show_unknown_library_is_an_engineering_fail(capsys):
    exit_code = main(["blocks", "show", "no-such-library"])
    payload = _payload(capsys)

    assert exit_code == 1
    assert payload["operation"] == "blocks.show"
    assert payload["execution"]["status"] == "failed"
    assert payload["engineering"]["status"] == "fail"
    assert payload["diagnostics"][0]["code"] == "blocks.library.not_found"
    assert payload["data"]["library"] is None


def _invalid_simulate(capsys, argv: list[str]) -> dict:
    exit_code = main(argv)
    payload = _payload(capsys)
    assert exit_code == 2
    assert payload["execution"]["status"] == "invalid_request"
    assert payload["engineering"]["status"] == "unknown"
    return payload


def test_simulate_blocks_and_models_are_mutually_exclusive(tmp_path, capsys):
    deck = _rc_deck(tmp_path)
    models = tmp_path / "extra.models"
    models.write_text("* empty\n", encoding="utf-8")

    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(deck),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--models",
            str(models),
        ],
    )
    assert "mutually exclusive" in payload["diagnostics"][0]["message"]


def test_simulate_blocks_refuses_a_pdk_binding(tmp_path, capsys):
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--pdk",
            "ihp-sg13g2",
        ],
    )
    assert "--pdk" in payload["diagnostics"][0]["message"]


def test_simulate_blocks_refuses_a_selection_without_a_colon(tmp_path, capsys):
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core opamp_1p",
        ],
    )
    assert "blocks.selection.invalid" in payload["diagnostics"][0]["message"]


def test_simulate_blocks_refuses_an_unknown_library(tmp_path, capsys):
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "no-such-library:opamp_1p",
        ],
    )
    assert "blocks.library.not_found" in payload["diagnostics"][0]["message"]


def test_simulate_blocks_refuses_a_non_ngspice_backend(tmp_path, capsys):
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "xyce",
            "--blocks",
            "bhv-core:opamp_1p",
        ],
    )
    message = payload["diagnostics"][0]["message"]
    assert "ngspice-native" in message
    # The refusal speaks the circuit-simulation profile envelope for the
    # requested backend, mirroring the pdk.backend.unsupported pattern.
    assert payload["operation"] == "simulate"
    assert payload["data"]["protocol"]["operation_profile"].startswith(
        "openada.operation/circuit.simulate/"
    )
    assert payload["data"]["extensions"]["org.openada"]["backend"] == "xyce"


def test_blocks_error_paths_default_to_the_simulation_semantic(tmp_path, capsys):
    # No explicit --backend: --blocks alone selects the circuit-simulation
    # semantic, so the failure envelope must be simulation-flavored with the
    # ngspice default, not a generic invalid request.
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--blocks",
            "no-such-library:opamp_1p",
        ],
    )
    assert "blocks.library.not_found" in payload["diagnostics"][0]["message"]
    assert payload["data"]["protocol"]["operation_profile"].startswith(
        "openada.operation/circuit.simulate/"
    )
    assert payload["data"]["extensions"]["org.openada"]["backend"] == "ngspice"


def test_simulate_blocks_refuses_a_preplaced_composition_file(tmp_path, capsys):
    # The composed prelude is materialized with O_CREAT|O_EXCL: an existing
    # file (or preplaced symlink) at the path is a refusal, never an
    # overwrite.
    output_dir = tmp_path / "evidence"
    output_dir.mkdir()
    preplaced = output_dir / "behavioral-blocks.model.spice"
    preplaced.write_text("* preplaced impostor\n", encoding="utf-8")

    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--output-dir",
            str(output_dir),
        ],
    )
    message = payload["diagnostics"][0]["message"]
    assert "behavioral-blocks.model.spice" in message
    assert "already exists" in message
    # The impostor bytes were not touched.
    assert preplaced.read_text(encoding="utf-8") == "* preplaced impostor\n"


def test_simulate_blocks_refuses_an_unwritable_output_dir(tmp_path, capsys):
    # /dev/null can never hold the materialized composition; the refusal must
    # be the structured circuit-simulate envelope, not a raw exception.
    payload = _invalid_simulate(
        capsys,
        [
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--output-dir",
            "/dev/null",
        ],
    )
    assert "--output-dir" in payload["diagnostics"][0]["message"]
    assert payload["data"]["protocol"]["operation_profile"].startswith(
        "openada.operation/circuit.simulate/"
    )


def test_simulate_blocks_composes_retained_evidence_bound_by_digest(
    tmp_path, capsys
):
    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    output_dir = tmp_path / "evidence"

    exit_code = main(
        [
            "--tool-path",
            f"ngspice={binary}",
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--output-dir",
            str(output_dir),
        ]
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["engineering"]["status"] == "pass"

    model_file = output_dir / "behavioral-blocks.model.spice"
    assert model_file.is_file()
    # The org.openada.behavioral-blocks extension inside the validated
    # retained envelope is the provenance authority; no mutable sidecar file
    # is written next to the composition.
    assert not (output_dir / "behavioral-blocks.provenance.json").exists()

    model_digest = hashlib.sha256(model_file.read_bytes()).hexdigest()

    configuration = payload["data"]["extensions"]["org.openada"]["configuration"]
    assert len(configuration) == 1
    assert configuration[0]["role"] == "spice-model-library"
    assert configuration[0]["sha256"] == model_digest

    retained = payload["data"]["extensions"]["org.openada.behavioral-blocks"]
    assert retained["kind"] == "behavioral-block-composition"
    assert retained["library_id"] == "bhv-core"
    assert retained["requested_blocks"] == ["opamp_1p"]
    assert retained["closure_blocks"] == ["opamp_1p"]
    assert len(retained["library_digest"]) == 64
    assert len(retained["closure_digest"]) == 64
    assert retained["composition_sha256"] == model_digest


def test_simulate_refuses_a_models_file_off_the_pinned_digest(tmp_path):
    # The tamper gate lives INSIDE the operation boundary: when the caller
    # pins expected_models_sha256, simulate() compares the model-library bytes
    # it actually read immediately after loading them, BEFORE any native
    # launch or result retention. The refusal is the operation's own
    # structured envelope under the original request id, and it IS the only
    # result of the call -- nothing chain-consumable is retained.
    from openada.discovery import DiscoveryManager
    from openada.operations.simulate import simulate

    models = tmp_path / "behavioral-blocks.model.spice"
    models.write_text(
        "* mutated composition\n.subckt bhv_impostor a b\nR1 a b 1k\n.ends\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "evidence"
    request_id = "0b0e7d4e-6d54-4d3b-9f3f-2b8a1c9d4e5f"

    payload = simulate(
        _rc_deck(tmp_path),
        output_dir,
        discovery=DiscoveryManager(),
        backend="ngspice",
        models_file=models,
        request_id=request_id,
        expected_models_sha256="0" * 64,
    )

    assert payload["execution"]["status"] == "invalid_request"
    # A refusal never runs a simulator: the recorded command is empty.
    assert payload["execution"]["command"] == []
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "blocks.materialize.tampered"
    assert payload["data"]["protocol"]["request_id"] == request_id
    # No result envelope was retained for a later chain step to consume.
    assert not output_dir.exists()


def test_simulate_accepts_a_models_file_matching_the_pinned_digest(tmp_path):
    # The pin is exact, not merely present: matching bytes pass the gate and
    # the refusal (if any) must come from a later stage, not the tamper check.
    from openada.discovery import DiscoveryManager
    from openada.operations.simulate import simulate

    models = tmp_path / "behavioral-blocks.model.spice"
    text = "* reviewed composition\n.subckt bhv_ok a b\nR1 a b 1k\n.ends\n"
    models.write_text(text, encoding="utf-8")

    payload = simulate(
        _rc_deck(tmp_path),
        tmp_path / "evidence",
        discovery=DiscoveryManager(),
        backend="ngspice",
        models_file=models,
        expected_models_sha256=hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest(),
    )

    codes = [entry["code"] for entry in payload["diagnostics"]]
    assert "blocks.materialize.tampered" not in codes


def test_simulate_blocks_pins_the_composition_digest_at_the_operation(
    tmp_path, capsys, monkeypatch
):
    # The CLI hands the reviewed composition digest to the operation itself;
    # there is no post-hoc envelope rewrite left in the CLI layer.
    import openada.cli as cli

    binary = tmp_path / "ngspice"
    _write_fake_ngspice(binary)
    output_dir = tmp_path / "evidence"

    captured = {}
    real_simulate = cli.simulate

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_simulate(*args, **kwargs)

    monkeypatch.setattr(cli, "simulate", spy)
    exit_code = main(
        [
            "--tool-path",
            f"ngspice={binary}",
            "simulate",
            str(_rc_deck(tmp_path)),
            "--backend",
            "ngspice",
            "--blocks",
            "bhv-core:opamp_1p",
            "--output-dir",
            str(output_dir),
        ]
    )
    payload = _payload(capsys)

    assert exit_code == 0
    assert payload["engineering"]["status"] == "pass"
    model_file = output_dir / "behavioral-blocks.model.spice"
    assert captured["expected_models_sha256"] == hashlib.sha256(
        model_file.read_bytes()
    ).hexdigest()
