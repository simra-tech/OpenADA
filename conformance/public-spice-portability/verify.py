#!/usr/bin/env python3
"""Independent oracle for the public SPICE portability evidence chain.

This module deliberately does not import :mod:`openada`.  It reparses the
retained Spice3 raw bytes, rebuilds normalized selections and source
derivations, and checks the agent-facing decision from first principles.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from typing import Any, Mapping
import sys

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
SIMULATION_IDS = (
    "ngspice-op", "ngspice-dc", "ngspice-ac",
    "xyce-dc", "xyce-ac", "xyce-tran",
)
ADMIN_IDS = (
    "capabilities", "doctor", "profile-list", "profile-show",
    "provider-list", "provider-validate",
)
NEGATIVE_IDS = (
    "xyce-ac-presentation-rejected", "xyce-op-unsupported",
    "ngspice-analysis-mismatch", "extract-missing-selector",
    "admin-unknown-profile", "admin-invalid-provider",
)
TAMPER_IDS = (
    "request-contract-byte", "public-source-byte", "derived-deck-byte",
    "native-raw-byte", "simulation-analysis-type", "simulation-backend-id",
    "extraction-series-digest", "admin-result-byte", "agent-decision-byte",
)
TAMPER_CODES = {
    "request-contract-byte": "conformance.contract.tampered",
    "public-source-byte": "conformance.source.tampered",
    "derived-deck-byte": "conformance.derivation.tampered",
    "native-raw-byte": "conformance.native.tampered",
    "simulation-analysis-type": "conformance.analysis.tampered",
    "simulation-backend-id": "conformance.backend.tampered",
    "extraction-series-digest": "conformance.lineage.tampered",
    "admin-result-byte": "conformance.admin.tampered",
    "agent-decision-byte": "conformance.decision.tampered",
}


class ConformanceError(RuntimeError):
    """A retained fact does not establish the declared conclusion."""


class VerificationError(ConformanceError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise VerificationError(code, message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path, *, code: str = "conformance.evidence.invalid") -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= MAX_JSON_BYTES
        ):
            _fail(code, f"JSON evidence is not one bounded regular file: {path}")
        encoded = path.read_bytes()
        if len(encoded) != metadata.st_size:
            _fail(code, f"JSON evidence changed while read: {path}")
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite constant {value!r}")
            ),
        )
    except VerificationError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        _fail(code, f"cannot read strict JSON {path}: {exc}")
    if not isinstance(document, dict):
        _fail(code, f"JSON root is not an object: {path}")
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        _fail("conformance.evidence.invalid", f"cannot hash {path}: {exc}")
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _expect(actual: object, expected: object, label: str, *, code: str) -> None:
    if actual != expected:
        _fail(code, f"{label} differs: expected {expected!r}, observed {actual!r}")


def _close(
    actual: float,
    expected: float,
    label: str,
    *,
    code: str,
    rel: float = 1e-8,
    abs_: float = 1e-12,
) -> None:
    if not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=rel, abs_tol=abs_):
        _fail(code, f"{label} differs: expected {expected:.17g}, observed {actual:.17g}")


def _validate_schema(document: dict[str, Any], schema_path: Path, label: str, *, code: str) -> None:
    schema = _read_json(schema_path, code=code)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        error = errors[0]
        pointer = "#" + "".join(f"/{part}" for part in error.absolute_path)
        _fail(code, f"{label} violates its schema at {pointer}: {error.message}")


def _regular(path: Path, *, code: str, maximum: int = MAX_ARTIFACT_BYTES) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _fail(code, f"cannot stat {path}: {exc}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        _fail(code, f"evidence is not one bounded regular file: {path}")
    return metadata.st_size


def _evidence_path(evidence: Path, container_path: object, *, code: str) -> Path:
    if not isinstance(container_path, str) or not container_path.startswith("/evidence/"):
        _fail(code, f"artifact path is outside /evidence: {container_path!r}")
    candidate = (evidence / container_path.removeprefix("/evidence/")).resolve()
    try:
        candidate.relative_to(evidence.resolve())
    except ValueError:
        _fail(code, f"artifact path escapes evidence: {container_path!r}")
    return candidate


def _parse_value(text: str, *, complex_values: bool, code: str) -> complex:
    try:
        if complex_values:
            real, separator, imaginary = text.partition(",")
            if not separator:
                _fail(code, f"complex raw scalar lacks a comma: {text!r}")
            value = complex(float(real.strip()), float(imaginary.strip()))
        else:
            value = complex(float(text.strip()), 0.0)
    except ValueError as exc:
        _fail(code, f"raw scalar is not numeric: {text!r}: {exc}")
    if not math.isfinite(value.real) or not math.isfinite(value.imag):
        _fail(code, "raw artifact contains a non-finite scalar")
    return value


def _parse_spice3(path: Path) -> dict[str, Any]:
    code = "conformance.native.tampered"
    size = _regular(path, code=code)
    payload = path.read_bytes()
    if len(payload) != size:
        _fail(code, f"native raw changed while read: {path}")
    binary_marker = b"Binary:\n"
    values_marker = b"Values:\n"
    if binary_marker in payload and values_marker not in payload:
        header_bytes, body = payload.split(binary_marker, 1)
        encoding = "binary"
    elif values_marker in payload and binary_marker not in payload:
        header_bytes, body = payload.split(values_marker, 1)
        encoding = "ascii"
    else:
        _fail(code, "native raw must contain exactly one Binary or Values marker")
    try:
        header_text = header_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        _fail(code, f"native raw header is not UTF-8: {exc}")
    lines = header_text.splitlines()
    if not lines or not lines[0].startswith("Title:"):
        _fail(code, "native raw does not start with Title")
    header: dict[str, str] = {}
    try:
        variables_position = next(
            position for position, line in enumerate(lines)
            if line.strip().casefold() == "variables:"
        )
    except StopIteration:
        _fail(code, "native raw lacks Variables marker")
    for line in lines[1:variables_position]:
        key, separator, value = line.partition(":")
        if separator:
            normalized = " ".join(key.casefold().split())
            if normalized in header:
                _fail(code, f"native raw repeats header {normalized!r}")
            header[normalized] = value.strip()
    for required in ("plotname", "flags", "no. variables", "no. points"):
        if required not in header:
            _fail(code, f"native raw lacks header {required!r}")
    try:
        variable_count = int(header["no. variables"])
        point_count = int(header["no. points"])
    except ValueError as exc:
        _fail(code, f"native raw dimensions are invalid: {exc}")
    if not 1 <= variable_count <= 4096 or not 1 <= point_count <= 10_000_000:
        _fail(code, "native raw dimensions exceed the independent bound")
    variable_lines = lines[variables_position + 1 :]
    if len(variable_lines) != variable_count:
        _fail(code, "native raw variable table length differs from its header")
    variables: list[tuple[str, str]] = []
    for index, line in enumerate(variable_lines):
        fields = line.split()
        if len(fields) < 3 or fields[0] != str(index):
            _fail(code, f"native raw variable table is invalid at index {index}")
        variables.append((fields[1], fields[2]))
    names = [name.casefold() for name, _kind in variables]
    if len(names) != len(set(names)):
        _fail(code, "native raw variable names are not unique")
    complex_values = "complex" in header["flags"].casefold().split()
    rows: list[list[complex]] = []
    if encoding == "binary":
        scalar_count = point_count * variable_count * (2 if complex_values else 1)
        if len(body) != scalar_count * 8:
            _fail(code, f"binary raw payload has {len(body)} bytes, expected {scalar_count * 8}")
        scalars = struct.unpack(f"={scalar_count}d", body)
        if not all(math.isfinite(value) for value in scalars):
            _fail(code, "binary raw contains a non-finite scalar")
        offset = 0
        for _point in range(point_count):
            row: list[complex] = []
            for _variable in range(variable_count):
                if complex_values:
                    row.append(complex(scalars[offset], scalars[offset + 1]))
                    offset += 2
                else:
                    row.append(complex(scalars[offset], 0.0))
                    offset += 1
            rows.append(row)
    else:
        try:
            body_text = body.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            _fail(code, f"ASCII raw values are not UTF-8: {exc}")
        value_lines = [line for line in body_text.splitlines() if line.strip()]
        if len(value_lines) != point_count * variable_count:
            _fail(code, "ASCII raw value-line count differs from its dimensions")
        cursor = 0
        for point in range(point_count):
            row = []
            for variable in range(variable_count):
                line = value_lines[cursor].strip()
                cursor += 1
                if variable == 0:
                    fields = line.split(maxsplit=1)
                    if len(fields) != 2 or fields[0] != str(point):
                        _fail(code, f"ASCII raw point index differs at point {point}")
                    line = fields[1]
                row.append(_parse_value(line, complex_values=complex_values, code=code))
            rows.append(row)
    columns = {
        name.casefold(): [row[position] for row in rows]
        for position, (name, _kind) in enumerate(variables)
    }
    return {
        "path": path,
        "sha256": _sha256(path),
        "bytes": size,
        "encoding": encoding,
        "plotname": header["plotname"],
        "numeric_type": "complex" if complex_values else "real",
        "variables": variables,
        "point_count": point_count,
        "columns": columns,
    }


def _verify_manifest(manifest: dict[str, Any], manifest_sha256: str) -> dict[str, Any]:
    _validate_schema(
        manifest,
        REPOSITORY_ROOT / "schemas/semantic-chain-manifest-v0alpha1.schema.json",
        "portability manifest",
        code="conformance.contract.tampered",
    )
    _expect(
        manifest.get("id"),
        "openada.chain/public-spice-portability/v1",
        "manifest.id",
        code="conformance.contract.tampered",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        _fail("conformance.contract.tampered", "manifest digest argument is invalid")
    requests = _read_json(HERE / "requests.json", code="conformance.contract.tampered")
    _expect(
        requests.get("chain_id"), manifest["id"], "requests.chain_id",
        code="conformance.contract.tampered",
    )
    _expect(
        tuple(item.get("id") for item in requests.get("simulations", [])),
        SIMULATION_IDS,
        "simulation request identities",
        code="conformance.contract.tampered",
    )
    _expect(
        tuple(item.get("id") for item in requests.get("admin_commands", [])),
        ADMIN_IDS,
        "admin request identities",
        code="conformance.contract.tampered",
    )
    _expect(
        tuple(item.get("id") for item in requests.get("negative_commands", [])),
        NEGATIVE_IDS,
        "negative request identities",
        code="conformance.contract.tampered",
    )
    for position, contract in enumerate(manifest["contracts"]):
        path = REPOSITORY_ROOT / contract["repository_path"]
        _regular(path, code="conformance.contract.tampered", maximum=MAX_JSON_BYTES)
        _expect(
            _sha256(path), contract["sha256"], f"manifest.contracts[{position}]",
            code="conformance.contract.tampered",
        )
    return requests


def _verify_request_copy(evidence: Path) -> None:
    retained = evidence / "request-contract.json"
    _regular(retained, code="conformance.contract.tampered", maximum=MAX_JSON_BYTES)
    _expect(
        _sha256(retained), _sha256(HERE / "requests.json"), "retained request contract",
        code="conformance.contract.tampered",
    )
    _read_json(retained, code="conformance.contract.tampered")


def _source_destination(repository: str, source_path: str) -> str:
    prefix = "xyce" if repository == "Xyce_Regression" else "ihp"
    return f"/evidence/sources/{prefix}/{source_path}"


def _verify_sources(manifest: dict[str, Any], evidence: Path) -> None:
    identity = _read_json(
        evidence / "source-identities.json", code="conformance.source.tampered"
    )
    expected_records: list[tuple[str, dict[str, Any]]] = []
    design = manifest["design"]
    expected_records.extend(("Xyce_Regression", item) for item in design["inputs"])
    expected_records.append(("Xyce_Regression", design["license"]))
    secondary = design["extensions"]["org.openada"]["secondary_design"]
    expected_records.extend(("IHP-AnalogAcademy", item) for item in secondary["inputs"])
    expected_records.append(("IHP-AnalogAcademy", secondary["license"]))
    _expect(
        identity.get("xyce_revision"), design["revision"], "source.xyce_revision",
        code="conformance.source.tampered",
    )
    _expect(
        identity.get("xyce_tag"), design["extensions"]["org.openada"]["tag"],
        "source.xyce_tag", code="conformance.source.tampered",
    )
    _expect(
        identity.get("ihp_revision"), secondary["revision"], "source.ihp_revision",
        code="conformance.source.tampered",
    )
    records = identity.get("files")
    if not isinstance(records, list) or len(records) != len(expected_records):
        _fail("conformance.source.tampered", "source identity record count differs")
    for position, ((repository, expected), actual) in enumerate(zip(expected_records, records)):
        if not isinstance(actual, dict):
            _fail("conformance.source.tampered", f"source record {position} is invalid")
        destination_text = _source_destination(repository, expected["path"])
        destination = _evidence_path(
            evidence, destination_text, code="conformance.source.tampered"
        )
        size = _regular(destination, code="conformance.source.tampered")
        observed_hash = _sha256(destination)
        _expect(actual.get("repository"), repository, f"source[{position}].repository", code="conformance.source.tampered")
        _expect(actual.get("source_path"), expected["path"], f"source[{position}].source_path", code="conformance.source.tampered")
        _expect(actual.get("path"), destination_text, f"source[{position}].path", code="conformance.source.tampered")
        _expect(actual.get("bytes"), size, f"source[{position}].bytes", code="conformance.source.tampered")
        _expect(actual.get("sha256"), expected["sha256"], f"source[{position}].declared_sha256", code="conformance.source.tampered")
        _expect(observed_hash, expected["sha256"], f"source[{position}].retained_sha256", code="conformance.source.tampered")


def _verify_runtime(manifest: dict[str, Any], evidence: Path) -> None:
    identity = _read_json(
        evidence / "runtime-identities.json", code="conformance.runtime.tampered"
    )
    _expect(identity.get("image_reference"), manifest["runtime"]["image_reference"], "runtime.image_reference", code="conformance.runtime.tampered")
    _expect(identity.get("platform"), "linux/amd64", "runtime.platform", code="conformance.runtime.tampered")
    records = identity.get("records")
    if not isinstance(records, dict):
        _fail("conformance.runtime.tampered", "runtime records are not an object")
    pins = manifest["runtime"]["extensions"]["org.openada"]
    for tool, digest in pins["tool_sha256"].items():
        _expect(records.get(f"tool:{tool}", {}).get("sha256"), digest, f"runtime.tool:{tool}", code="conformance.runtime.tampered")
    pdk = pins["pdk"]
    expected = {
        "pdk_commit": pdk["commit_file"],
        "xschem_rcfile": pdk["xschem_rcfile"],
        "corner_moslv": pdk["model_files"]["corner_moslv"],
        "moslv_modules": pdk["model_files"]["moslv_modules"],
        "moslv_parameters": pdk["model_files"]["moslv_parameters"],
        "psp103_osdi": pdk["psp103_osdi"],
    }
    retained = {
        "corner_moslv": "cornerMOSlv.lib",
        "moslv_modules": "sg13g2_moslv_mod.lib",
        "moslv_parameters": "sg13g2_moslv_parm.lib",
        "psp103_osdi": "psp103.osdi",
    }
    for identifier, pin in expected.items():
        _expect(records.get(identifier, {}).get("path"), pin["path"], f"runtime.{identifier}.path", code="conformance.runtime.tampered")
        _expect(records.get(identifier, {}).get("sha256"), pin["sha256"], f"runtime.{identifier}.sha256", code="conformance.runtime.tampered")
        if identifier in retained:
            path = evidence / "runtime" / retained[identifier]
            _regular(path, code="conformance.runtime.tampered")
            _expect(_sha256(path), pin["sha256"], f"retained runtime {identifier}", code="conformance.runtime.tampered")
    startup = evidence / "runtime/isolated.spiceinit"
    _regular(startup, code="conformance.runtime.tampered")
    _expect(_sha256(startup), "168ff70c9c37e8a2d687e782cb92b9df81e9f35ed1eb1d1ef14c4e02a27c082d", "runtime.isolated_spiceinit", code="conformance.runtime.tampered")
    _expect(records.get("isolated_spiceinit", {}).get("sha256"), _sha256(startup), "runtime record isolated_spiceinit", code="conformance.runtime.tampered")


def _flattened_deck(
    source: Path,
    evidence: Path,
    *,
    directive: str,
    controls: int,
) -> str:
    code = "conformance.derivation.tampered"
    try:
        source_text = source.read_text(encoding="utf-8", errors="strict")
        corner = (evidence / "runtime/cornerMOSlv.lib").read_text(encoding="utf-8", errors="strict")
        modules = (evidence / "runtime/sg13g2_moslv_mod.lib").read_text(encoding="utf-8", errors="strict")
        parameters = (evidence / "runtime/sg13g2_moslv_parm.lib").read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        _fail(code, f"cannot reconstruct reviewed IHP derivation: {exc}")
    match = re.search(
        r"(?ims)^\s*\.LIB\s+mos_tt\s*$\n(?P<body>.*?)"
        r"^\s*\.include\s+sg13g2_moslv_mod\.lib\s*$\n"
        r"^\s*\.ENDL\s+mos_tt\s*$",
        corner,
    )
    if match is None:
        _fail(code, "retained corner lacks the reviewed mos_tt closure")
    embedding = (
        "** begin inlined pinned sg13g2_moslv_parm.lib\n"
        + parameters.rstrip()
        + "\n** end inlined pinned sg13g2_moslv_parm.lib\n"
    )
    modules, include_count = re.subn(
        r"(?im)^\s*\.include\s+sg13g2_moslv_parm\.lib\s*\n", embedding, modules
    )
    no_control, control_count = re.subn(
        r"(?ims)^\s*\.control\s*$.*?^\s*\.endc\s*$\n?", "", source_text
    )
    no_library, library_count = re.subn(
        r"(?im)^\s*\.lib\s+cornerMOSlv\.lib\s+mos_tt\s*$\n?", "", no_control
    )
    if include_count != 4 or control_count != controls or library_count != 1:
        _fail(code, "IHP derivation edit counts differ from the reviewed transform")
    closure = (
        "\n** OpenADA reviewed flattening of pinned IHP mos_tt model closure.\n"
        + match.group("body") + "\n" + modules.rstrip() + "\n" + directive + "\n"
    )
    transformed, end_count = re.subn(
        r"(?im)^\s*\.end\s*$", closure + ".end", no_library
    )
    if end_count != 1:
        _fail(code, "IHP derivation does not have one top-level .end")
    return transformed


def _verify_derivations(evidence: Path) -> None:
    document = _read_json(
        evidence / "derivations.json", code="conformance.derivation.tampered"
    )
    records = document.get("records")
    if not isinstance(records, dict) or set(records) != {
        "inverter-op", "inverter-dc", "ota-ac", "xyce-ac", "xyce-op-unsupported"
    }:
        _fail("conformance.derivation.tampered", "derivation identities differ")
    ihp = {
        "inverter-op": ("work/inverter-xschem.spice", "work/inverter-op.spice", ".op", 1),
        "inverter-dc": ("work/inverter-xschem.spice", "work/inverter-dc.spice", ".dc V1 0 1.2 0.1", 1),
        "ota-ac": ("work/ota-xschem.spice", "work/ota-ac.spice", ".ac dec 10 1 100000", 2),
    }
    for identifier, (source_name, derived_name, directive, controls) in ihp.items():
        source = evidence / source_name
        derived = evidence / derived_name
        expected_text = _flattened_deck(source, evidence, directive=directive, controls=controls)
        try:
            actual_text = derived.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            _fail("conformance.derivation.tampered", f"cannot read {identifier}: {exc}")
        _expect(actual_text, expected_text, f"derived deck {identifier}", code="conformance.derivation.tampered")
        record = records[identifier]
        _expect(record.get("source_sha256"), _sha256(source), f"{identifier}.source_sha256", code="conformance.derivation.tampered")
        _expect(record.get("derived_sha256"), _sha256(derived), f"{identifier}.derived_sha256", code="conformance.derivation.tampered")
    ac_source = evidence / "work/xyce-ac-source.cir"
    ac_derived = evidence / "work/xyce-ac-derived.cir"
    text = ac_source.read_text(encoding="utf-8", errors="strict")
    expected, count = re.subn(r"(?im)^\.print ac v\(1\)\s*\n", "", text)
    if count != 1 or ac_derived.read_text(encoding="utf-8", errors="strict") != expected:
        _fail("conformance.derivation.tampered", "Xyce AC minimal derivation differs")
    _expect(records["xyce-ac"].get("source_sha256"), _sha256(ac_source), "xyce-ac.source_sha256", code="conformance.derivation.tampered")
    _expect(records["xyce-ac"].get("derived_sha256"), _sha256(ac_derived), "xyce-ac.derived_sha256", code="conformance.derivation.tampered")
    op_source = evidence / "work/xyce-dc.cir"
    op_derived = evidence / "work/xyce-op-unsupported.cir"
    expected, count = re.subn(
        r"(?im)^\.dc v1 0 10 \.1\s*$", ".op",
        op_source.read_text(encoding="utf-8", errors="strict"),
    )
    if count != 1 or op_derived.read_text(encoding="utf-8", errors="strict") != expected:
        _fail("conformance.derivation.tampered", "Xyce unsupported-OP derivation differs")
    _expect(records["xyce-op-unsupported"].get("source_sha256"), _sha256(op_source), "xyce-op.source_sha256", code="conformance.derivation.tampered")
    _expect(records["xyce-op-unsupported"].get("derived_sha256"), _sha256(op_derived), "xyce-op.derived_sha256", code="conformance.derivation.tampered")


def _simulation_artifact(result: dict[str, Any], evidence: Path, *, legacy: bool = False) -> tuple[dict[str, Any], Path]:
    code = "conformance.native.tampered"
    role = "output" if legacy else "simulation.result"
    artifacts = [item for item in result.get("artifacts", []) if item.get("role") == role]
    if len(artifacts) != 1:
        _fail(code, f"simulation result does not retain exactly one {role}")
    artifact = artifacts[0]
    path = _evidence_path(evidence, artifact.get("path"), code=code)
    size = _regular(path, code=code)
    _expect(artifact.get("bytes"), size, "native artifact bytes", code=code)
    _expect(artifact.get("sha256"), _sha256(path), "native artifact sha256", code=code)
    return artifact, path


def _result_document(path: Path, *, code: str) -> dict[str, Any]:
    result = _read_json(path, code=code)
    _validate_schema(
        result, REPOSITORY_ROOT / "schemas/result-v0alpha1.schema.json",
        str(path), code=code,
    )
    return result


def _verify_electrical(identifier: str, parsed: dict[str, Any]) -> None:
    code = "conformance.native.invalid"
    columns = parsed["columns"]
    if identifier == "ngspice-op":
        _expect(parsed["plotname"], "Operating Point", "ngspice OP plot", code=code)
        _expect(parsed["point_count"], 1, "ngspice OP point count", code=code)
        _close(columns["v(vin)"][0].real, 0.0, "ngspice OP vin", code=code)
        if not 1.19 < columns["v(vout)"][0].real < 1.21:
            _fail(code, "inverter OP does not establish a high output for low input")
    elif identifier == "ngspice-dc":
        _expect(parsed["plotname"], "DC transfer characteristic", "ngspice DC plot", code=code)
        axis = columns["v(v-sweep)"]
        vin, vout = columns["v(vin)"], columns["v(vout)"]
        _expect(len(axis), 13, "ngspice DC point count", code=code)
        for position, value in enumerate(axis):
            _close(value.real, 0.1 * position, f"ngspice DC axis[{position}]", code=code, rel=1e-12)
            _close(vin[position].real, value.real, f"ngspice DC vin[{position}]", code=code, rel=1e-12)
        if not vout[0].real > 1.19 or not vout[-1].real < 1e-4:
            _fail(code, "inverter DC endpoints do not establish inversion")
        if any(right.real > left.real + 1e-9 for left, right in zip(vout, vout[1:])):
            _fail(code, "inverter DC output is not monotonically nonincreasing")
    elif identifier == "ngspice-ac":
        _expect(parsed["plotname"], "AC Analysis", "ngspice AC plot", code=code)
        frequency, output = columns["frequency"], columns["v(vout)"]
        _expect(len(frequency), 51, "ngspice AC point count", code=code)
        _close(frequency[0].real, 1.0, "ngspice AC first frequency", code=code)
        _close(frequency[-1].real, 100000.0, "ngspice AC final frequency", code=code, rel=1e-10)
        if any(right.real <= left.real for left, right in zip(frequency, frequency[1:])):
            _fail(code, "ngspice AC frequency is not strictly increasing")
        if min(abs(value) for value in output) <= 1.0:
            _fail(code, "OTA AC output does not establish gain across the reviewed band")
    elif identifier == "xyce-dc":
        axis, voltage = columns["sweep"], columns["v(1)"]
        _expect(len(axis), 101, "Xyce DC point count", code=code)
        for position, (x, y) in enumerate(zip(axis, voltage)):
            expected = 0.1 * position
            _close(x.real, expected, f"Xyce DC axis[{position}]", code=code)
            _close(y.real, expected, f"Xyce DC v1[{position}]", code=code)
    elif identifier == "xyce-ac":
        frequency, voltage = columns["frequency"], columns["v(1)"]
        _expect(len(frequency), 51, "Xyce AC point count", code=code)
        for position, (x, actual) in enumerate(zip(frequency, voltage)):
            expected = -1.0 / complex(0.001, 2.0 * math.pi * x.real * 2e-6)
            _close(actual.real, expected.real, f"Xyce AC real[{position}]", code=code, rel=2e-8, abs_=2e-8)
            _close(actual.imag, expected.imag, f"Xyce AC imag[{position}]", code=code, rel=2e-8, abs_=2e-8)
    elif identifier == "xyce-tran":
        times, voltage = columns["time"], columns["v(1)"]
        _expect(len(times), 18, "Xyce transient point count", code=code)
        for position, (time, actual) in enumerate(zip(times, voltage)):
            expected = 10.0 * math.sin(2.0 * math.pi * 10e6 * time.real)
            _close(actual.real, expected, f"Xyce transient v1[{position}]", code=code, rel=2e-8, abs_=2e-8)
        _close(times[-1].real, 1e-8, "Xyce transient stop", code=code, abs_=1e-18)


def _verify_simulations(
    requests: dict[str, Any], evidence: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    definitions = {item["id"]: item for item in requests["simulations"]}
    results: dict[str, dict[str, Any]] = {}
    parsed: dict[str, dict[str, Any]] = {}
    for identifier in SIMULATION_IDS:
        definition = definitions[identifier]
        result = _result_document(
            evidence / "results/sim" / f"{identifier}.json",
            code="conformance.analysis.tampered",
        )
        _expect(result.get("operation"), "simulate", f"{identifier}.operation", code="conformance.analysis.tampered")
        _expect(result.get("engineering", {}).get("status"), "pass", f"{identifier}.engineering", code="conformance.analysis.tampered")
        analysis = result.get("data", {}).get("analysis", {})
        _expect(analysis.get("type"), definition["analysis"], f"{identifier}.analysis", code="conformance.analysis.tampered")
        protocol = result.get("data", {}).get("protocol", {})
        _expect(protocol.get("operation_profile"), "openada.operation/circuit.simulate/v1alpha2", f"{identifier}.profile", code="conformance.backend.tampered")
        _expect(protocol.get("driver_id"), f"org.openada.driver.{definition['backend']}", f"{identifier}.driver", code="conformance.backend.tampered")
        _expect(
            result.get("data", {}).get("extensions", {}).get("org.openada", {}).get("backend"),
            definition["backend"], f"{identifier}.backend", code="conformance.backend.tampered",
        )
        artifact, raw_path = _simulation_artifact(result, evidence)
        raw = _parse_spice3(raw_path)
        variables = len(raw["variables"])
        dependent = variables if definition["analysis"] == "op" else variables - 1
        finite = dependent * raw["point_count"] * (2 if raw["numeric_type"] == "complex" else 1)
        _expect(analysis.get("point_count"), raw["point_count"], f"{identifier}.point_count", code="conformance.native.tampered")
        _expect(analysis.get("dependent_variable_count"), dependent, f"{identifier}.dependent_variable_count", code="conformance.native.tampered")
        _expect(analysis.get("finite_value_count"), finite, f"{identifier}.finite_value_count", code="conformance.native.tampered")
        _expect(artifact.get("sha256"), raw["sha256"], f"{identifier}.raw_digest", code="conformance.native.tampered")
        _verify_electrical(identifier, raw)
        results[identifier] = result
        parsed[identifier] = raw
    legacy = _result_document(
        evidence / "results/legacy-ngspice-op.json",
        code="conformance.backend.tampered",
    )
    _expect(legacy.get("operation"), "simulate", "legacy.operation", code="conformance.backend.tampered")
    _expect(legacy.get("engineering", {}).get("status"), "pass", "legacy.engineering", code="conformance.backend.tampered")
    _artifact, legacy_path = _simulation_artifact(legacy, evidence, legacy=True)
    legacy_raw = _parse_spice3(legacy_path)
    reference = parsed["ngspice-op"]
    _expect([item[0].casefold() for item in legacy_raw["variables"]], [item[0].casefold() for item in reference["variables"]], "legacy variable table", code="conformance.backend.tampered")
    for name, values in reference["columns"].items():
        for position, expected in enumerate(values):
            actual = legacy_raw["columns"][name][position]
            _close(actual.real, expected.real, f"legacy {name}[{position}]", code="conformance.backend.tampered", rel=1e-12)
    results["legacy-ngspice-op"] = legacy
    parsed["legacy-ngspice-op"] = legacy_raw
    return results, parsed


def _raw_axis(identifier: str, raw: dict[str, Any]) -> tuple[str, str, list[float]]:
    mapping = {
        "ngspice-op": ("sample", "1", None),
        "ngspice-dc": ("V1", "V", "v(v-sweep)"),
        "ngspice-ac": ("frequency", "Hz", "frequency"),
        "xyce-dc": ("V1", "V", "sweep"),
        "xyce-ac": ("frequency", "Hz", "frequency"),
        "xyce-tran": ("time", "s", "time"),
    }
    name, unit, native = mapping[identifier]
    values = [0.0] if native is None else [item.real for item in raw["columns"][native]]
    return name, unit, values


def _verify_extractions(
    requests: dict[str, Any],
    evidence: Path,
    simulations: dict[str, dict[str, Any]],
    parsed: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    definitions = {item["id"]: item for item in requests["simulations"]}
    extractions: dict[str, dict[str, Any]] = {}
    for identifier in SIMULATION_IDS:
        result = _result_document(
            evidence / "results/extract" / f"{identifier}.json",
            code="conformance.lineage.tampered",
        )
        _expect(result.get("operation"), "result.series.extract", f"{identifier}.extract.operation", code="conformance.lineage.tampered")
        _expect(result.get("engineering", {}).get("status"), "pass", f"{identifier}.extract.engineering", code="conformance.lineage.tampered")
        series = result.get("data", {}).get("extraction", {}).get("series", {})
        source = series.get("source", {})
        sim_artifact, _path = _simulation_artifact(simulations[identifier], evidence)
        _expect(source.get("lineage", {}).get("artifact_sha256"), sim_artifact["sha256"], f"{identifier}.extract.lineage", code="conformance.lineage.tampered")
        _expect(source.get("lineage", {}).get("request_id"), simulations[identifier]["data"]["protocol"]["request_id"], f"{identifier}.extract.request_lineage", code="conformance.lineage.tampered")
        axis_name, axis_unit, axis_values = _raw_axis(identifier, parsed[identifier])
        axis = series.get("axis", {})
        _expect(axis.get("name"), axis_name, f"{identifier}.axis.name", code="conformance.lineage.tampered")
        _expect(axis.get("unit"), axis_unit, f"{identifier}.axis.unit", code="conformance.lineage.tampered")
        observed_axis = axis.get("values")
        if not isinstance(observed_axis, list) or len(observed_axis) != len(axis_values):
            _fail("conformance.lineage.tampered", f"{identifier} extraction axis length differs")
        for position, expected in enumerate(axis_values):
            _close(float(observed_axis[position]), expected, f"{identifier}.axis[{position}]", code="conformance.lineage.tampered", rel=2e-8)
        selectors = definitions[identifier]["selectors"]
        signals = series.get("signals")
        if not isinstance(signals, list) or len(signals) != len(selectors):
            _fail("conformance.lineage.tampered", f"{identifier} selected signal count differs")
        for signal, selector in zip(signals, selectors):
            _expect(signal.get("name"), selector["output_name"], f"{identifier}.signal.name", code="conformance.lineage.tampered")
            _expect(signal.get("unit"), selector["unit"], f"{identifier}.signal.unit", code="conformance.lineage.tampered")
            native = parsed[identifier]["columns"][selector["native_name"].casefold()]
            expected_values = [
                item.real if selector["component"] == "real" else item.imag
                for item in native
            ]
            observed_values = signal.get("values")
            if not isinstance(observed_values, list) or len(observed_values) != len(expected_values):
                _fail("conformance.lineage.tampered", f"{identifier} signal length differs")
            for position, expected in enumerate(expected_values):
                _close(float(observed_values[position]), expected, f"{identifier}.{selector['output_name']}[{position}]", code="conformance.lineage.tampered", rel=2e-8)
        _expect(series.get("conditions"), definitions[identifier]["conditions"], f"{identifier}.conditions", code="conformance.lineage.tampered")
        normalized_content = {"axis": axis, "signals": signals, "conditions": series["conditions"]}
        _expect(source.get("artifact_sha256"), _canonical_sha256(normalized_content), f"{identifier}.series_sha256", code="conformance.lineage.tampered")
        extractions[identifier] = result
    return extractions


def _verify_admin(evidence: Path) -> dict[str, dict[str, Any]]:
    expected_operations = {
        "capabilities": "doctor", "doctor": "doctor",
        "profile-list": "profile.list", "profile-show": "profile.show",
        "provider-list": "provider.list", "provider-validate": "provider.validate",
    }
    admin: dict[str, dict[str, Any]] = {}
    for identifier in ADMIN_IDS:
        result = _result_document(
            evidence / "results/admin" / f"{identifier}.json",
            code="conformance.admin.tampered",
        )
        _expect(result.get("operation"), expected_operations[identifier], f"admin.{identifier}.operation", code="conformance.admin.tampered")
        expected_status = "not_applicable" if identifier in {"capabilities", "doctor"} else "pass"
        _expect(result.get("engineering", {}).get("status"), expected_status, f"admin.{identifier}.status", code="conformance.admin.tampered")
        admin[identifier] = result
    for identifier in ("capabilities", "doctor"):
        capabilities = admin[identifier].get("data", {}).get("semantic_capabilities")
        if not isinstance(capabilities, list):
            _fail("conformance.admin.tampered", f"{identifier} lacks semantic capabilities")
        by_provider = {item.get("provider_id"): item for item in capabilities if isinstance(item, dict)}
        required = {
            "org.openada.driver.ngspice": {"op", "dc", "ac", "tran"},
            "org.openada.driver.xyce": {"dc", "ac", "tran"},
        }
        for provider, analyses in required.items():
            record = by_provider.get(provider)
            if record is None or record.get("availability") != "available":
                _fail("conformance.admin.tampered", f"{identifier} does not expose available {provider}")
            feature_ids = {item.get("id") for item in record.get("features", [])}
            expected = {f"openada.feature/simulation.analysis.{analysis}/v1alpha1" for analysis in analyses}
            if not expected.issubset(feature_ids):
                _fail("conformance.admin.tampered", f"{identifier} omits reviewed features for {provider}")
        extract = by_provider.get("org.openada.kernel.spice3-series")
        if extract is None or extract.get("operation_profile") != "openada.operation/result.series.extract/v1alpha1":
            _fail("conformance.admin.tampered", f"{identifier} omits the Spice3 extraction kernel")
    profiles = admin["profile-list"].get("data", {}).get("profiles")
    if not isinstance(profiles, list) or "openada.operation/circuit.simulate/v1alpha2" not in {
        item.get("operation_profile") for item in profiles if isinstance(item, dict)
    }:
        _fail("conformance.admin.tampered", "profile list omits circuit.simulate/v1alpha2")
    shown = admin["profile-show"].get("data", {})
    _expect(shown.get("operation_profile"), "openada.operation/circuit.simulate/v1alpha2", "profile.show operation", code="conformance.admin.tampered")
    for identifier in ("provider-list", "provider-validate"):
        data = admin[identifier].get("data", {})
        _expect(data.get("driver", {}).get("id"), "org.openada.driver.ngspice-pdk-control", f"{identifier}.driver", code="conformance.admin.tampered")
        _expect(data.get("driver", {}).get("version"), "0.5.0", f"{identifier}.version", code="conformance.admin.tampered")
        capabilities = data.get("capabilities")
        if not isinstance(capabilities, list) or len(capabilities) != 1:
            _fail("conformance.admin.tampered", f"{identifier} capability shape differs")
        features = set(capabilities[0].get("features", []))
        expected = {
            f"openada.feature/simulation.analysis.{analysis}/v1alpha1"
            for analysis in ("op", "dc", "ac", "tran")
        }
        if features != expected:
            _fail("conformance.admin.tampered", f"{identifier} provider features differ")
    return admin


def _verify_negatives(manifest: dict[str, Any], evidence: Path) -> dict[str, dict[str, Any]]:
    declarations = {item["id"]: item for item in manifest["negative_replays"]}
    expected = {
        "xyce-ac-presentation-rejected": ("simulate", "unknown"),
        "xyce-op-unsupported": ("simulate", "unknown"),
        "ngspice-analysis-mismatch": ("simulate", "unknown"),
        "extract-missing-selector": ("result.series.extract", "unknown"),
        "admin-unknown-profile": ("profile.show", "fail"),
        "admin-invalid-provider": ("provider.validate", "unknown"),
    }
    results: dict[str, dict[str, Any]] = {}
    for identifier in NEGATIVE_IDS:
        result = _result_document(
            evidence / "results/negative" / f"{identifier}.json",
            code="conformance.negative.tampered",
        )
        operation, status = expected[identifier]
        _expect(result.get("operation"), operation, f"negative.{identifier}.operation", code="conformance.negative.tampered")
        _expect(result.get("engineering", {}).get("status"), status, f"negative.{identifier}.status", code="conformance.negative.tampered")
        codes = [item.get("code") for item in result.get("diagnostics", [])]
        required = declarations[identifier]["required_diagnostic"]
        if codes.count(required) != 1:
            _fail("conformance.negative.tampered", f"negative {identifier} lacks exactly one {required}")
        results[identifier] = result
    return results


def _verify_agent(
    manifest: dict[str, Any],
    evidence: Path,
    simulations: dict[str, dict[str, Any]],
    parsed: dict[str, dict[str, Any]],
    extractions: dict[str, dict[str, Any]],
    admin: dict[str, dict[str, Any]],
    negatives: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    code = "conformance.decision.tampered"
    agent = _read_json(evidence / "agent-evidence.json", code=code)
    _expect(agent.get("schema"), "openada.public-spice-portability-agent-evidence/v0alpha1", "agent.schema", code=code)
    _expect(agent.get("chain_id"), manifest["id"], "agent.chain_id", code=code)
    _expect(agent.get("conclusion"), "portable-for-reviewed-analysis-matrix", "agent.conclusion", code=code)
    _expect(agent.get("decision_basis"), {
        "positive_simulations": 6,
        "typed_extractions": 6,
        "admin_surfaces": 6,
        "typed_negative_replays": 6,
        "independent_oracle_required": True,
    }, "agent.decision_basis", code=code)
    matrix = agent.get("matrix")
    if not isinstance(matrix, list) or [item.get("id") for item in matrix] != list(SIMULATION_IDS):
        _fail(code, "agent simulation matrix identities differ")
    definitions = {item["id"]: item for item in _read_json(HERE / "requests.json")["simulations"]}
    for item in matrix:
        identifier = item["id"]
        definition = definitions[identifier]
        analysis = simulations[identifier]["data"]["analysis"]
        protocol = simulations[identifier]["data"]["protocol"]
        _expect(item.get("backend"), definition["backend"], f"agent.{identifier}.backend", code=code)
        _expect(item.get("analysis"), definition["analysis"], f"agent.{identifier}.analysis", code=code)
        _expect(item.get("driver_id"), protocol["driver_id"], f"agent.{identifier}.driver", code=code)
        for field in ("point_count", "dependent_variable_count", "finite_value_count"):
            _expect(item.get(field), analysis[field], f"agent.{identifier}.{field}", code=code)
        sim_path = evidence / "results/sim" / f"{identifier}.json"
        extract_path = evidence / "results/extract" / f"{identifier}.json"
        _expect(item.get("simulation_result", {}).get("sha256"), _sha256(sim_path), f"agent.{identifier}.simulation_digest", code=code)
        _expect(item.get("extraction_result", {}).get("sha256"), _sha256(extract_path), f"agent.{identifier}.extraction_digest", code=code)
        artifact, _path = _simulation_artifact(simulations[identifier], evidence)
        _expect(item.get("native_artifact", {}).get("sha256"), artifact["sha256"], f"agent.{identifier}.native_digest", code=code)
        expected_signals = [signal["name"] for signal in extractions[identifier]["data"]["extraction"]["series"]["signals"]]
        _expect(item.get("selected_series"), expected_signals, f"agent.{identifier}.selected_series", code=code)
    admin_items = agent.get("admin")
    if not isinstance(admin_items, list) or [item.get("id") for item in admin_items] != list(ADMIN_IDS):
        _fail(code, "agent admin identities differ")
    for item in admin_items:
        identifier = item["id"]
        _expect(item.get("operation"), admin[identifier]["operation"], f"agent.admin.{identifier}.operation", code=code)
        _expect(item.get("engineering_status"), admin[identifier]["engineering"]["status"], f"agent.admin.{identifier}.status", code=code)
        _expect(item.get("result", {}).get("sha256"), _sha256(evidence / "results/admin" / f"{identifier}.json"), f"agent.admin.{identifier}.digest", code="conformance.admin.tampered")
    negative_items = agent.get("negative_replays")
    if not isinstance(negative_items, list) or [item.get("id") for item in negative_items] != list(NEGATIVE_IDS):
        _fail(code, "agent negative identities differ")
    for item in negative_items:
        identifier = item["id"]
        _expect(item.get("engineering_status"), negatives[identifier]["engineering"]["status"], f"agent.negative.{identifier}.status", code=code)
        _expect(item.get("result", {}).get("sha256"), _sha256(evidence / "results/negative" / f"{identifier}.json"), f"agent.negative.{identifier}.digest", code=code)
    derivations = _read_json(evidence / "derivations.json", code="conformance.derivation.tampered")["records"]
    _expect(agent.get("source_derivations"), derivations, "agent.source_derivations", code=code)
    _expect(agent.get("source_identity", {}).get("sha256"), _sha256(evidence / "source-identities.json"), "agent.source_identity", code=code)
    _expect(agent.get("runtime_identity", {}).get("sha256"), _sha256(evidence / "runtime-identities.json"), "agent.runtime_identity", code=code)
    limitations = agent.get("limitations")
    if not isinstance(limitations, list) or len(limitations) != 5 or not any("not foundry electrical signoff" in item for item in limitations):
        _fail(code, "agent limitations do not disclose the signoff boundary")
    return agent


def _expected_files(*, require_chain_run: bool) -> set[str]:
    files = {
        "run.json", "request-contract.json", "design-provenance.json",
        "secondary-design-provenance.json", "source-identities.json",
        "runtime-identities.json", "derivations.json", "agent-evidence.json",
        "results/netlist/inverter.json", "results/netlist/ota.json",
        "results/legacy-ngspice-op.json",
        "runtime/cornerMOSlv.lib", "runtime/sg13g2_moslv_mod.lib",
        "runtime/sg13g2_moslv_parm.lib", "runtime/psp103.osdi",
        "runtime/isolated.spiceinit",
        "work/ihp-inverter/inverter_tb.sch", "work/ihp-inverter/inverter.sym",
        "work/ihp-inverter/inverter.sch", "work/ihp-ota/ota_testbench.sch",
        "work/ihp-ota/two_stage_OTA.sym", "work/ihp-ota/two_stage_OTA.sch",
        "work/inverter-xschem.spice", "work/ota-xschem.spice",
        "work/inverter-op.spice", "work/inverter-dc.spice", "work/ota-ac.spice",
        "work/xyce-dc.cir", "work/xyce-ac-source.cir",
        "work/xyce-ac-derived.cir", "work/xyce-tran.cir",
        "work/xyce-op-unsupported.cir", "work/provider-invalid.json",
        "selections/missing-selector.json",
        "sources/xyce/README.md",
        "sources/xyce/Netlists/Output/DC/dc-noprn.cir",
        "sources/xyce/Netlists/Output/TRAN/tran-raw-override-noprint.cir",
        "sources/xyce/Netlists/ACtests/RC_simple.cir",
        "sources/ihp/LICENSE",
        "sources/ihp/modules/module_0_foundations/inverter/inverter_tb.sch",
        "sources/ihp/modules/module_0_foundations/inverter/inverter.sym",
        "sources/ihp/modules/module_0_foundations/inverter/inverter.sch",
        "sources/ihp/modules/module_1_bandgap_reference/part_1_OTA/gmid_example/testbenches/ota_testbench.sch",
        "sources/ihp/modules/module_1_bandgap_reference/part_1_OTA/gmid_example/schematic/two_stage_OTA.sym",
        "sources/ihp/modules/module_1_bandgap_reference/part_1_OTA/gmid_example/schematic/two_stage_OTA.sch",
        "legacy/ngspice-op/inverter-op.log", "legacy/ngspice-op/inverter-op.raw",
    }
    native_names = {
        "ngspice-op": "inverter-op", "ngspice-dc": "inverter-dc",
        "ngspice-ac": "ota-ac", "xyce-dc": "xyce-dc",
        "xyce-ac": "xyce-ac-derived", "xyce-tran": "xyce-tran",
    }
    for identifier, stem in native_names.items():
        files.update(
            {
                f"results/sim/{identifier}.json",
                f"results/extract/{identifier}.json",
                f"selections/{identifier}.json",
            }
        )
        if identifier.startswith("xyce"):
            files.update({f"sim/{identifier}/{stem}.xyce.raw", f"sim/{identifier}/{stem}.xyce.log"})
        else:
            files.update({f"sim/{identifier}/{stem}.raw", f"sim/{identifier}/{stem}.log"})
    files.update(f"results/admin/{identifier}.json" for identifier in ADMIN_IDS)
    files.update(f"results/negative/{identifier}.json" for identifier in NEGATIVE_IDS)
    if require_chain_run:
        files.update(
            {"chain-run.json", "independent-verification.json", "contract-tests.json"}
        )
        files.update(f"tamper/{identifier}.json" for identifier in TAMPER_IDS)
    return files


def _verify_tree(evidence: Path, *, require_chain_run: bool) -> None:
    try:
        root = evidence.lstat()
    except OSError as exc:
        _fail("conformance.evidence.invalid", f"cannot stat evidence root: {exc}")
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
        _fail("conformance.evidence.invalid", "evidence root is not a real directory")
    actual: set[str] = set()
    for path in evidence.rglob("*"):
        relative = path.relative_to(evidence).as_posix()
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode):
            _fail("conformance.evidence.invalid", f"evidence contains symlink {relative}")
        if stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                _fail("conformance.evidence.invalid", f"evidence file is hard-linked: {relative}")
            actual.add(relative)
        elif not stat.S_ISDIR(item.st_mode):
            _fail("conformance.evidence.invalid", f"evidence contains special file {relative}")
    expected = _expected_files(require_chain_run=require_chain_run)
    if actual != expected:
        _fail(
            "conformance.evidence.invalid",
            f"evidence file set differs; missing={sorted(expected-actual)}, unexpected={sorted(actual-expected)}",
        )


def _verify_git_state(record: dict[str, Any], label: str, *, revision: str | None, allow_dirty: bool) -> None:
    before, after = record.get("before"), record.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        _fail("conformance.source-state.invalid", f"{label} lacks before/after source state")
    _expect(before, after, f"{label}.before_after", code="conformance.source-state.invalid")
    _expect(record.get("state_unchanged"), True, f"{label}.state_unchanged", code="conformance.source-state.invalid")
    if revision is not None:
        _expect(before.get("commit"), revision, f"{label}.commit", code="conformance.source-state.invalid")
    if allow_dirty:
        _expect(record.get("commit_exact"), not bool(before.get("working_tree_modified")), f"{label}.commit_exact", code="conformance.source-state.invalid")
    else:
        _expect(before.get("working_tree_modified"), False, f"{label}.working_tree_modified", code="conformance.source-state.invalid")
        _expect(before.get("status_entry_count"), 0, f"{label}.status_entry_count", code="conformance.source-state.invalid")
        _expect(record.get("commit_exact"), True, f"{label}.commit_exact", code="conformance.source-state.invalid")


def _verify_run(manifest: dict[str, Any], evidence: Path, manifest_sha256: str) -> dict[str, Any]:
    run = _read_json(evidence / "run.json", code="conformance.run.invalid")
    _expect(run.get("schema"), "openada.public-spice-portability-run-metadata/v0alpha1", "run.schema", code="conformance.run.invalid")
    _expect(run.get("chain_id"), manifest["id"], "run.chain_id", code="conformance.run.invalid")
    _expect(run.get("chain_manifest_sha256"), manifest_sha256, "run.manifest_sha256", code="conformance.run.invalid")
    provisional = run.get("provisional")
    if not isinstance(provisional, bool):
        _fail("conformance.run.invalid", "run.provisional is not Boolean")
    source = run.get("source_state")
    if not isinstance(source, dict) or set(source) != {"openada", "xyce", "ihp"}:
        _fail("conformance.source-state.invalid", "run.source_state identities differ")
    _verify_git_state(source["openada"], "source_state.openada", revision=None, allow_dirty=provisional)
    _verify_git_state(source["xyce"], "source_state.xyce", revision=manifest["design"]["revision"], allow_dirty=False)
    secondary = manifest["design"]["extensions"]["org.openada"]["secondary_design"]
    _verify_git_state(source["ihp"], "source_state.ihp", revision=secondary["revision"], allow_dirty=False)
    policy = run.get("execution_policy", {})
    _expect(policy.get("network"), "none", "run.execution_policy.network", code="conformance.run.invalid")
    _expect(policy.get("container_root"), "read-only", "run.execution_policy.container_root", code="conformance.run.invalid")
    _expect(policy.get("openada_mount"), "read-only", "run.execution_policy.openada_mount", code="conformance.run.invalid")
    _expect(policy.get("public_source_mounts"), "read-only", "run.execution_policy.public_source_mounts", code="conformance.run.invalid")
    command = run.get("container_command")
    if not isinstance(command, list):
        _fail("conformance.run.invalid", "run.container_command is not an array")
    required_tokens = {"--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "--mount"}
    if not required_tokens.issubset(set(command)):
        _fail("conformance.run.invalid", "container command lacks isolation controls")
    mounts = [command[position + 1] for position, value in enumerate(command[:-1]) if value == "--mount"]
    if len(mounts) != 4 or not any("target=/openada,readonly" in value for value in mounts) or not any("target=/xyce,readonly" in value for value in mounts) or not any("target=/ihp,readonly" in value for value in mounts):
        _fail("conformance.run.invalid", "container source mounts are not the reviewed read-only set")
    observation = run.get("runtime_observation", {})
    _expect(observation.get("matrix"), {"simulation_count": 6, "extraction_count": 6, "admin_count": 6, "negative_count": 6}, "run.runtime_observation.matrix", code="conformance.run.invalid")
    completed = observation.get("completed_operations")
    if not isinstance(completed, list) or len(completed) != 27 or len(completed) != len(set(completed)):
        _fail("conformance.run.invalid", "inside runner did not record 27 unique completed operations")
    return run


def semantic_subject_sha256() -> str:
    return semantic_subject(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
    )


def _run_contract_tests(chain_id: str) -> dict[str, Any]:
    suite = HERE / "test_portability_chain.py"
    environment = os.environ.copy()
    environment.pop("OPENADA_RUN_PUBLIC_SPICE_PORTABILITY", None)
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            str(suite.relative_to(REPOSITORY_ROOT)),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    if completed.returncode != 0:
        _fail(
            "conformance.contract-tests.failed",
            "portability contract tests failed: " + completed.stdout[-4_000:],
        )
    passed = re.findall(r"(?:^|\s)([0-9]+) passed", completed.stdout)
    skipped = re.findall(r"(?:^|\s)([0-9]+) skipped", completed.stdout)
    if len(passed) != 1 or len(skipped) > 1:
        _fail(
            "conformance.contract-tests.failed",
            "cannot parse the focused portability test summary",
        )
    return {
        "schema": "openada.contract-test-report/public-spice-portability/v1",
        "chain_id": chain_id,
        "status": "pass",
        "suite": {
            "repository_path": suite.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": _sha256(suite),
            "passed": int(passed[0]),
            "skipped": int(skipped[0]) if skipped else 0,
            "failed": 0,
        },
        "extensions": {},
    }


def _verify_design_provenance(
    manifest: dict[str, Any], evidence: Path
) -> None:
    schema = REPOSITORY_ROOT / "schemas/design-provenance-v0alpha1.schema.json"
    records = (
        ("design-provenance.json", manifest["design"]),
        (
            "secondary-design-provenance.json",
            manifest["design"]["extensions"]["org.openada"]["secondary_design"],
        ),
    )
    for filename, design in records:
        provenance = _read_json(
            evidence / filename, code="conformance.source-identity.tampered"
        )
        _validate_schema(
            provenance,
            schema,
            filename,
            code="conformance.source-identity.tampered",
        )
        for field in ("repository", "revision", "tree"):
            _expect(
                provenance[field],
                design[field],
                f"{filename}.{field}",
                code="conformance.source-identity.tampered",
            )
        _expect(
            {key: provenance["license"][key] for key in ("path", "sha256")},
            {key: design["license"][key] for key in ("path", "sha256")},
            f"{filename}.license",
            code="conformance.source-identity.tampered",
        )
        _expect(
            [
                {key: item[key] for key in ("path", "sha256")}
                for item in provenance["inputs"]
            ],
            design["inputs"],
            f"{filename}.inputs",
            code="conformance.source-identity.tampered",
        )


def _verify_chain_run(
    manifest: dict[str, Any], evidence: Path, manifest_sha256: str, run_metadata: dict[str, Any]
) -> None:
    run = _read_json(evidence / "chain-run.json", code="conformance.receipt.invalid")
    _validate_schema(
        run, REPOSITORY_ROOT / "schemas/semantic-chain-run-v0alpha1.schema.json",
        "semantic chain run", code="conformance.receipt.invalid",
    )
    _expect(run["chain_id"], manifest["id"], "chain-run.chain_id", code="conformance.receipt.invalid")
    _expect(run["chain_manifest_sha256"], manifest_sha256, "chain-run.manifest", code="conformance.receipt.invalid")
    _expect(run["semantic_subject_sha256"], semantic_subject_sha256(), "chain-run.semantic_subject", code="conformance.receipt.invalid")
    expected_receipt_class = "provisional" if run_metadata["provisional"] else "release"
    _expect(run["source_attestation"]["receipt_class"], expected_receipt_class, "chain-run.source_attestation.receipt_class", code="conformance.receipt.invalid")
    _expect(run["source_attestation"]["semantic_subject_sha256"], run["semantic_subject_sha256"], "chain-run.source_attestation.semantic_subject", code="conformance.receipt.invalid")
    _expect(run["source_attestation"]["state_unchanged"], True, "chain-run.source_attestation.state_unchanged", code="conformance.receipt.invalid")
    if not run_metadata["provisional"]:
        _expect(run["source_attestation"]["clean_before"], True, "chain-run.source_attestation.clean_before", code="conformance.receipt.invalid")
        _expect(run["source_attestation"]["clean_after"], True, "chain-run.source_attestation.clean_after", code="conformance.receipt.invalid")
    if set(run["checks"].values()) != {True}:
        _fail("conformance.receipt.invalid", "chain-run does not assert every agent-ready check")
    extension = run.get("extensions", {}).get("org.openada", {})
    _expect(extension.get("provisional"), run_metadata["provisional"], "chain-run.provisional", code="conformance.receipt.invalid")
    if not run_metadata["provisional"]:
        _expect(extension.get("source_freeze_attested"), True, "chain-run.source_freeze_attested", code="conformance.receipt.invalid")
    steps = {item["id"]: item for item in manifest["steps"]}
    paths: set[str] = set()
    trust_digests: set[str] = set()
    negative_counts = {identifier: 0 for identifier in NEGATIVE_IDS}
    tamper_counts = {identifier: 0 for identifier in TAMPER_IDS}
    required_roles = {
        "contract-test", "design-provenance", "native-artifact", "independent-oracle",
        "normalized-evidence", "downstream-decision", "negative-replay",
        "tamper-replay", "agent-visible-evidence",
    }
    observed_roles: set[str] = set()
    origin_requirements = {
        "contract-test": ("independent-oracle", False),
        "design-provenance": ("source-materialize", False),
        "native-artifact": ("semantic-command", True),
        "independent-oracle": ("independent-oracle", False),
        "normalized-evidence": ("semantic-command", False),
        "downstream-decision": ("semantic-command", False),
        "agent-visible-evidence": ("independent-decision", False),
    }
    for position, artifact in enumerate(run["artifacts"]):
        relative = artifact["repository_path"]
        if relative in paths:
            _fail("conformance.receipt.invalid", f"chain-run reuses artifact path {relative}")
        paths.add(relative)
        prefix = "conformance/public-spice-portability/evidence/"
        if not relative.startswith(prefix):
            _fail("conformance.receipt.invalid", f"chain-run artifact is outside publication root: {relative}")
        path = evidence / relative.removeprefix(prefix)
        size = _regular(path, code="conformance.receipt.invalid", maximum=1024 * 1024 * 1024)
        _expect(artifact["bytes"], size, f"chain artifact {position}.bytes", code="conformance.receipt.invalid")
        _expect(artifact["sha256"], _sha256(path), f"chain artifact {position}.sha256", code="conformance.receipt.invalid")
        role = artifact["role"]
        observed_roles.add(role)
        if role in required_roles:
            if artifact["sha256"] in trust_digests:
                _fail("conformance.receipt.invalid", f"trust artifacts reuse digest {artifact['sha256']}")
            trust_digests.add(artifact["sha256"])
        if role == "negative-replay":
            replay = artifact["replay_id"]
            if replay not in negative_counts:
                _fail("conformance.receipt.invalid", f"unknown negative artifact {replay}")
            negative_counts[replay] += 1
        elif role == "tamper-replay":
            replay = artifact["replay_id"]
            if replay not in tamper_counts:
                _fail("conformance.receipt.invalid", f"unknown tamper artifact {replay}")
            tamper_counts[replay] += 1
        else:
            step = steps.get(artifact["source_step"])
            if step is None or artifact["source_output"] not in step["produces"]:
                _fail("conformance.receipt.invalid", f"artifact {position} origin is not declared by the manifest DAG")
            requirement = origin_requirements.get(role)
            if requirement is not None and (
                step["kind"], step["native_execution"]
            ) != requirement:
                _fail(
                    "conformance.receipt.invalid",
                    f"artifact {position} role {role!r} has an invalid DAG origin",
                )
    if not required_roles.issubset(observed_roles):
        _fail("conformance.receipt.invalid", f"chain-run lacks trust roles {sorted(required_roles-observed_roles)}")
    if set(negative_counts.values()) != {1} or set(tamper_counts.values()) != {1}:
        _fail("conformance.receipt.invalid", "chain-run does not retain exactly one artifact per replay")
    report = _read_json(evidence / "independent-verification.json", code="conformance.receipt.invalid")
    _expect(report.get("status"), "pass", "independent report status", code="conformance.receipt.invalid")
    _expect(tuple(item.get("replay_id") for item in report.get("tamper_replays", [])), TAMPER_IDS, "independent report tamper IDs", code="conformance.receipt.invalid")
    for identifier in TAMPER_IDS:
        replay = _read_json(evidence / "tamper" / f"{identifier}.json", code="conformance.receipt.invalid")
        _expect(replay.get("replay_id"), identifier, f"tamper.{identifier}.id", code="conformance.receipt.invalid")
        _expect(replay.get("required_diagnostic"), TAMPER_CODES[identifier], f"tamper.{identifier}.diagnostic", code="conformance.receipt.invalid")
        _expect(replay.get("status"), "unknown", f"tamper.{identifier}.status", code="conformance.receipt.invalid")
    contract_report = _read_json(
        evidence / "contract-tests.json", code="conformance.contract-tests.failed"
    )
    _expect(
        contract_report,
        _run_contract_tests(manifest["id"]),
        "contract-tests",
        code="conformance.contract-tests.failed",
    )


def _verify_core(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
    require_chain_run: bool,
) -> dict[str, Any]:
    _verify_tree(evidence, require_chain_run=require_chain_run)
    requests = _verify_manifest(manifest, manifest_sha256)
    _verify_request_copy(evidence)
    _verify_design_provenance(manifest, evidence)
    _verify_sources(manifest, evidence)
    _verify_runtime(manifest, evidence)
    _verify_derivations(evidence)
    run_metadata = _verify_run(manifest, evidence, manifest_sha256)
    simulations, parsed = _verify_simulations(requests, evidence)
    extractions = _verify_extractions(requests, evidence, simulations, parsed)
    admin = _verify_admin(evidence)
    negatives = _verify_negatives(manifest, evidence)
    agent = _verify_agent(
        manifest, evidence, simulations, parsed, extractions, admin, negatives
    )
    if require_chain_run:
        _verify_chain_run(manifest, evidence, manifest_sha256, run_metadata)
    raw_summary = {
        identifier: {
            "sha256": parsed[identifier]["sha256"],
            "encoding": parsed[identifier]["encoding"],
            "numeric_type": parsed[identifier]["numeric_type"],
            "point_count": parsed[identifier]["point_count"],
            "variable_count": len(parsed[identifier]["variables"]),
        }
        for identifier in (*SIMULATION_IDS, "legacy-ngspice-op")
    }
    return {
        "run_metadata": run_metadata,
        "raw_summary": raw_summary,
        "agent_evidence_sha256": _sha256(evidence / "agent-evidence.json"),
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mutate_json(path: Path, operation: Any) -> None:
    document = _read_json(path)
    operation(document)
    _write_json(path, document)


def _strip_receipts(root: Path) -> None:
    for name in (
        "chain-run.json",
        "independent-verification.json",
        "contract-tests.json",
    ):
        path = root / name
        if path.exists():
            path.unlink()
    tamper = root / "tamper"
    if tamper.exists():
        shutil.rmtree(tamper)


def _run_tamper_probes(
    manifest: dict[str, Any], evidence: Path, manifest_sha256: str
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for identifier in TAMPER_IDS:
        with tempfile.TemporaryDirectory(prefix=f"openada-portability-{identifier}-") as temporary:
            clone = Path(temporary) / "evidence"
            shutil.copytree(evidence, clone, copy_function=shutil.copy2)
            _strip_receipts(clone)
            mutation: str
            if identifier == "request-contract-byte":
                path = clone / "request-contract.json"
                path.write_bytes(path.read_bytes() + b" ")
                mutation = "append one whitespace byte to retained request contract"
            elif identifier == "public-source-byte":
                path = clone / "sources/xyce/Netlists/Output/DC/dc-noprn.cir"
                path.write_bytes(path.read_bytes() + b"*tamper\n")
                mutation = "append one source comment record"
            elif identifier == "derived-deck-byte":
                path = clone / "work/xyce-ac-derived.cir"
                path.write_bytes(path.read_bytes() + b"*tamper\n")
                mutation = "append one derived-deck comment record"
            elif identifier == "native-raw-byte":
                path = clone / "sim/xyce-dc/xyce-dc.xyce.raw"
                payload = bytearray(path.read_bytes())
                payload[-2] ^= 1
                path.write_bytes(payload)
                mutation = "flip one retained raw byte"
            elif identifier == "simulation-analysis-type":
                path = clone / "results/sim/ngspice-op.json"
                _mutate_json(path, lambda value: value["data"]["analysis"].__setitem__("type", "dc"))
                mutation = "replace normalized OP analysis type with DC"
            elif identifier == "simulation-backend-id":
                path = clone / "results/sim/xyce-dc.json"
                _mutate_json(path, lambda value: value["data"]["protocol"].__setitem__("driver_id", "org.openada.driver.ngspice"))
                mutation = "replace Xyce driver identity with ngspice"
            elif identifier == "extraction-series-digest":
                path = clone / "results/extract/ngspice-dc.json"
                _mutate_json(path, lambda value: value["data"]["extraction"]["series"]["source"]["lineage"].__setitem__("artifact_sha256", "0" * 64))
                mutation = "replace extraction raw-lineage digest"
            elif identifier == "admin-result-byte":
                path = clone / "results/admin/provider-validate.json"
                _mutate_json(path, lambda value: value["engineering"].__setitem__("summary", "tampered provider decision"))
                mutation = "replace provider-validation summary"
            else:
                path = clone / "agent-evidence.json"
                _mutate_json(path, lambda value: value.__setitem__("conclusion", "unreviewed"))
                mutation = "replace agent conclusion"
            observed_code: str | None = None
            observed_message: str | None = None
            try:
                _verify_core(
                    manifest, clone, manifest_sha256=manifest_sha256,
                    require_chain_run=False,
                )
            except VerificationError as exc:
                observed_code, observed_message = exc.code, str(exc)
            required = TAMPER_CODES[identifier]
            if observed_code != required:
                _fail(
                    "conformance.tamper-probe.invalid",
                    f"tamper {identifier} expected {required}, observed {observed_code}: {observed_message}",
                )
            reports.append(
                {
                    "schema": "openada.tamper-replay/v0alpha1",
                    "replay_id": identifier,
                    "status": "unknown",
                    "required_diagnostic": required,
                    "observed_diagnostic": observed_code,
                    "mutation": mutation,
                    "verifier_message": observed_message,
                    "extensions": {},
                }
            )
    return reports


def verify_evidence(
    manifest: dict[str, Any],
    evidence: Path,
    *,
    manifest_sha256: str,
    require_chain_run: bool,
    run_tamper_probes: bool,
) -> dict[str, Any]:
    """Verify one evidence directory and return a deterministic oracle report."""

    facts = _verify_core(
        manifest, evidence, manifest_sha256=manifest_sha256,
        require_chain_run=require_chain_run,
    )
    tamper = (
        _run_tamper_probes(manifest, evidence, manifest_sha256)
        if run_tamper_probes
        else []
    )
    return {
        "schema": "openada.public-spice-portability-independent-verification/v0alpha1",
        "chain_id": manifest["id"],
        "status": "pass",
        "manifest_sha256": manifest_sha256,
        "provisional": facts["run_metadata"]["provisional"],
        "checks": {
            "strict_contracts": "pass",
            "pinned_public_sources": "pass",
            "runtime_identity": "pass",
            "source_derivations": "pass",
            "independent_native_raw_parse": "pass",
            "electrical_oracles": "pass",
            "normalized_extraction_equivalence": "pass",
            "admin_surface_semantics": "pass",
            "negative_replays": "pass",
            "agent_decision_lineage": "pass",
        },
        "raw_summary": facts["raw_summary"],
        "agent_evidence_sha256": facts["agent_evidence_sha256"],
        "tamper_replays": tamper,
        "extensions": {},
    }


__all__ = [
    "ConformanceError", "VerificationError", "semantic_subject_sha256",
    "verify_evidence",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--run-tamper-probes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = _read_json(manifest_path, code="conformance.contract.tampered")
        report = verify_evidence(
            manifest,
            args.evidence.expanduser().resolve(),
            manifest_sha256=_sha256(manifest_path),
            require_chain_run=True,
            run_tamper_probes=args.run_tamper_probes,
        )
    except ConformanceError as exc:
        print(f"independent verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
