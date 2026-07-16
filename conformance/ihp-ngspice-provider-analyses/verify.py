#!/usr/bin/env python3
"""Offline publication verifier for the four-analysis ngspice provider chain.

This verifier intentionally imports no OpenADA implementation module.  It
rebuilds the native oracle, exercises every tamper boundary, validates the
public-design receipt, and checks the content-addressed chain-run inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from oracle import OracleError, load_json, sha256, verify


ROOT = HERE.parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from semantic_receipts import semantic_subject  # noqa: E402


class PublicationError(RuntimeError):
    """Retained publication evidence failed an independent check."""


ANALYSES = ("op", "dc", "ac", "tran")
NEGATIVE_FILES = {
    "op-unsafe-command": "op-unsafe-result.json",
    "dc-parameter-mismatch": "dc-mismatch-result.json",
    "ac-feature-mismatch": "ac-feature-result.json",
    "tran-duplicate-write": "tran-duplicate-result.json",
    "tran-native-error": "tran-native-error-result.json",
}
NEGATIVE_DIAGNOSTICS = {
    "op-unsafe-command": "simulation.request.invalid",
    "dc-parameter-mismatch": "simulation.request.invalid",
    "ac-feature-mismatch": "simulation.request.invalid",
    "tran-duplicate-write": "simulation.request.invalid",
    "tran-native-error": "simulation.result.malformed",
}
TAMPER_DEFINITIONS = {
    "op-raw-byte": ("op", "raw-byte-substitution", "does not bind raw bytes"),
    "dc-request-feature": (
        "dc",
        "required-feature-substitution",
        "request feature is not exact",
    ),
    "ac-result-digest": (
        "ac",
        "result-artifact-digest-substitution",
        "does not bind raw bytes",
    ),
    "tran-raw-header": (
        "tran",
        "raw-header-analysis-substitution",
        "plot is",
    ),
    "provider-version": (
        "op",
        "provider-version-substitution",
        "not exact provider pass evidence",
    ),
}


def _expect(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise PublicationError(f"{label} differs from independently reconstructed evidence")


def _regular(path: Path, *, maximum: int = 512 * 1024 * 1024) -> int:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        raise PublicationError(f"unsafe or unbounded retained file: {path}")
    return metadata.st_size


def _read_json(path: Path) -> dict[str, Any]:
    _regular(path, maximum=32 * 1024 * 1024)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {token!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read retained JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"retained JSON root is not an object: {path}")
    return value


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _schema(document: object, path: Path, label: str) -> None:
    schema = _read_json(path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            document
        ),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        pointer = "/".join(str(part) for part in error.absolute_path)
        raise PublicationError(f"{label} violates its schema at {pointer}: {error.message}")


def _expected_agent(
    oracle: dict[str, Any], normalized: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": "openada.agent-evidence/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "chain_id": "openada.chain/ihp-ngspice-provider-analyses/v1",
        "provider": normalized["provider"],
        "analyses": normalized["analyses"],
        "decisions": decision["decisions"],
        "limitations": decision["limitations"],
        "negative_replays": oracle["negative_replays"],
        "standards_assessment": normalized["standards_assessment"],
        "recommended_next_actions": [
            "Use OP only for nominal bias inspection.",
            "Use DC only for the pinned nominal inverter transfer curve.",
            "Use AC gain/crossover estimates only within the pinned OTA testbench and simulated band.",
            "Run explicit PVT, Monte Carlo, extracted, and specification workflows before design signoff.",
        ],
        "extensions": {},
    }


def _mutate_json(path: Path, mutation: Callable[[dict[str, Any]], None]) -> None:
    document = load_json(path)
    mutation(document)
    _write_json(path, document)


def run_tamper_probes(native_root: Path) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for replay_id, (analysis, mutation_name, required_fragment) in TAMPER_DEFINITIONS.items():
        with tempfile.TemporaryDirectory(prefix=f"openada-provider-{replay_id}-") as temporary:
            clone = Path(temporary) / "native-replay"
            shutil.copytree(native_root, clone, copy_function=shutil.copy2)
            if replay_id == "op-raw-byte":
                path = clone / "native/op/work/op.raw"
                payload = bytearray(path.read_bytes())
                payload[-1] ^= 1
                path.write_bytes(payload)
            elif replay_id == "dc-request-feature":
                _mutate_json(
                    clone / "requests/dc.json",
                    lambda value: value["driver_selector"].__setitem__(
                        "required_features",
                        ["openada.feature/simulation.analysis.ac/v1alpha1"],
                    ),
                )
            elif replay_id == "ac-result-digest":
                def replace_digest(value: dict[str, Any]) -> None:
                    matches = [
                        item
                        for item in value["artifacts"]
                        if item.get("role") == "simulation.result"
                    ]
                    if len(matches) != 1:
                        raise PublicationError("AC result lacks one simulation.result artifact")
                    matches[0]["sha256"] = "0" * 64

                _mutate_json(clone / "results/ac.json", replace_digest)
            elif replay_id == "tran-raw-header":
                path = clone / "native/tran/work/tran.raw"
                payload = path.read_bytes()
                original = b"Transient Analysis (linearized)"
                replacement = b"Transient Analysis (tampered!!)"
                if payload.count(original) != 1 or len(original) != len(replacement):
                    raise PublicationError("transient tamper precondition differs")
                path.write_bytes(payload.replace(original, replacement, 1))
            else:
                _mutate_json(
                    clone / "results/op.json",
                    lambda value: value["data"]["protocol"].__setitem__(
                        "driver_version", "9.9.9"
                    ),
                )
            try:
                verify(clone)
            except OracleError as exc:
                if required_fragment not in str(exc):
                    raise PublicationError(
                        f"tamper {replay_id} reached the wrong rejection boundary: {exc}"
                    ) from exc
            else:
                raise PublicationError(f"tamper {replay_id} was accepted")
        receipts[replay_id] = {
            "schema": "openada.tamper-replay/ihp-ngspice-provider-analyses/v1",
            "id": replay_id,
            "analysis": analysis,
            "mutation": mutation_name,
            "expected_status": "invalid_request",
            "observed_status": "invalid_request",
            "required_diagnostic": "evidence.binding.invalid",
            "observed_diagnostic": "evidence.binding.invalid",
            "rejected": True,
            "oracle_rejection": required_fragment,
            "extensions": {},
        }
    return receipts


def _verify_design_provenance(root: Path, manifest: dict[str, Any]) -> None:
    provenance = _read_json(root / "design-provenance.json")
    _schema(
        provenance,
        ROOT / "schemas/design-provenance-v0alpha1.schema.json",
        "design provenance",
    )
    design = manifest["design"]
    for field in ("repository", "revision", "tree"):
        _expect(provenance[field], design[field], f"design provenance {field}")
    _expect(
        {key: provenance["license"][key] for key in ("path", "sha256")},
        {key: design["license"][key] for key in ("path", "sha256")},
        "design provenance license",
    )
    _expect(
        [
            {key: item[key] for key in ("path", "sha256")}
            for item in provenance["inputs"]
        ],
        design["inputs"],
        "design provenance inputs",
    )


def _expected_artifacts(root: Path) -> list[tuple[Path, str, str | None, str | None, str | None]]:
    records: list[tuple[Path, str, str | None, str | None, str | None]] = [
        (root / "design-provenance.json", "design-provenance", "materialize-pinned-sources", "design-provenance", None),
    ]
    native = root / "native-replay"
    for name in ANALYSES:
        records.extend(
            [
                (native / f"decks/{name}.spice", "native-artifact", f"provider-{name}", f"{name}-deck", None),
                (native / f"requests/{name}.json", "native-artifact", f"provider-{name}", f"{name}-request", None),
                (native / f"results/{name}.json", "native-artifact", f"provider-{name}", f"{name}-result", None),
                (native / f"native/{name}/work/{name}.raw", "native-artifact", f"provider-{name}", f"{name}-raw", None),
                (native / f"native/{name}/simulation/{name}.log", "native-artifact", f"provider-{name}", f"{name}-log", None),
                (native / f"native/{name}/simulation/{name}.openada-control.sp", "native-artifact", f"provider-{name}", f"{name}-launcher", None),
            ]
        )
    records.extend(
        [
            (root / "oracle.json", "independent-oracle", "independent-native-oracle", "independent-oracle", None),
            (root / "normalized.json", "normalized-evidence", "normalize-evidence", "normalized-evidence", None),
            (root / "decision.json", "downstream-decision", "publish-scoped-decision", "downstream-decision", None),
            (root / "agent-evidence.json", "agent-visible-evidence", "agent-decision", "agent-visible-evidence", None),
            (root / "contract-tests.json", "contract-test", "contract-tests", "contract-test-report", None),
        ]
    )
    records.extend(
        (root / "negative" / f"{identifier}.json", "negative-replay", None, None, identifier)
        for identifier in NEGATIVE_FILES
    )
    records.extend(
        (root / "tamper" / f"{identifier}.json", "tamper-replay", None, None, identifier)
        for identifier in TAMPER_DEFINITIONS
    )
    return records


def _verify_chain_run(
    root: Path, manifest: dict[str, Any], *, allow_provisional: bool
) -> None:
    run_path = HERE / "semantic-chain-run.json"
    run = _read_json(run_path)
    _schema(run, ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json", "chain run")
    _expect(run["chain_id"], manifest["id"], "chain run ID")
    _expect(run["chain_manifest_sha256"], sha256(HERE / "manifest.json"), "chain manifest digest")
    subject = semantic_subject(ROOT, ROOT / "catalog/semantic-surfaces-v0alpha1.json")
    _expect(run["semantic_subject_sha256"], subject, "semantic subject")
    source = run["source_attestation"]
    if source["receipt_class"] != "release" and not allow_provisional:
        raise PublicationError("source receipt class is not release")
    _expect(source["semantic_subject_sha256"], subject, "source receipt subject")
    _expect(source["state_unchanged"], True, "source state")
    if source["receipt_class"] == "release":
        _expect(
            (source["clean_before"], source["clean_after"]),
            (True, True),
            "source freeze",
        )
    expected = _expected_artifacts(root)
    _expect(len(run["artifacts"]), len(expected), "chain artifact count")
    for position, (record, definition) in enumerate(zip(run["artifacts"], expected, strict=True)):
        path, role, step, output, replay = definition
        _regular(path)
        _expect(record["repository_path"], path.relative_to(ROOT).as_posix(), f"artifact {position} path")
        _expect(record["bytes"], path.stat().st_size, f"artifact {position} bytes")
        _expect(record["sha256"], sha256(path), f"artifact {position} digest")
        _expect(record["role"], role, f"artifact {position} role")
        _expect(record["source_step"], step, f"artifact {position} step")
        _expect(record["source_output"], output, f"artifact {position} output")
        _expect(record["replay_id"], replay, f"artifact {position} replay")


def _verify_tree(root: Path, oracle: dict[str, Any]) -> None:
    expected = {
        "agent-evidence.json",
        "contract-tests.json",
        "decision.json",
        "design-provenance.json",
        "normalized.json",
        "oracle.json",
        *{f"negative/{identifier}.json" for identifier in NEGATIVE_FILES},
        *{f"tamper/{identifier}.json" for identifier in TAMPER_DEFINITIONS},
        *{
            f"native-replay/{record['path']}"
            for record in oracle["native_files"]
        },
    }
    actual: set[str] = set()
    for path in root.rglob("*"):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise PublicationError(f"publication contains symlink {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        _regular(path)
        actual.add(relative)
    _expect(actual, expected, "publication file set")


def verify_publication(
    root: Path, *, allow_provisional: bool = False
) -> dict[str, Any]:
    root = root.resolve()
    native = root / "native-replay"
    manifest = _read_json(HERE / "manifest.json")
    _schema(
        manifest,
        ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json",
        "chain manifest",
    )
    oracle, normalized, decision = verify(native)
    _expect(_read_json(root / "oracle.json"), oracle, "published oracle")
    _expect(_read_json(root / "normalized.json"), normalized, "published normalization")
    _expect(_read_json(root / "decision.json"), decision, "published decision")
    _expect(
        _read_json(root / "agent-evidence.json"),
        _expected_agent(oracle, normalized, decision),
        "published agent evidence",
    )

    expected_contract = {
        "schema": "openada.contract-test-report/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "suites": [
            {
                "repository_path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256(path),
            }
            for path in (
                ROOT / "tests/test_ngspice_provider.py",
                ROOT / "tests/test_ngspice_outputs.py",
            )
        ],
        "extensions": {},
    }
    _expect(_read_json(root / "contract-tests.json"), expected_contract, "contract tests")
    for replay_id, filename in NEGATIVE_FILES.items():
        retained = root / "negative" / f"{replay_id}.json"
        source = native / "negative" / filename
        _expect(retained.read_bytes(), source.read_bytes(), f"negative {replay_id} bytes")
        codes = oracle["negative_replays"][replay_id]["diagnostic_codes"]
        if NEGATIVE_DIAGNOSTICS[replay_id] not in codes:
            raise PublicationError(
                f"negative {replay_id} lacks {NEGATIVE_DIAGNOSTICS[replay_id]}"
            )

    expected_tamper = run_tamper_probes(native)
    for replay_id, receipt in expected_tamper.items():
        _expect(
            _read_json(root / "tamper" / f"{replay_id}.json"),
            receipt,
            f"tamper {replay_id}",
        )
    _verify_design_provenance(root, manifest)
    _verify_tree(root, oracle)
    _verify_chain_run(root, manifest, allow_provisional=allow_provisional)
    return {
        "schema": "openada.publication-verification/ihp-ngspice-provider-analyses/v1",
        "status": "pass",
        "native_tree_sha256": oracle["native_tree_sha256"],
        "analysis_point_counts": {
            name: normalized["analyses"][name]["point_count"] for name in ANALYSES
        },
        "negative_replay_count": len(NEGATIVE_FILES),
        "tamper_replay_count": len(TAMPER_DEFINITIONS),
        "extensions": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("publication", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_publication(args.publication)
    except (OSError, UnicodeError, ValueError, KeyError, OracleError, PublicationError) as exc:
        print(f"publication verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
