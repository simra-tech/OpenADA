#!/usr/bin/env python3
"""Independently verify IHP inverter results and retained native evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import sys
from typing import Any
import xml.etree.ElementTree as ET

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from common import (
    ConformanceError,
    DRC_OPERATION_NAMES,
    RESULT_SCHEMA,
    load_manifest,
    sha256_file,
)


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from semantic_receipts import semantic_subject  # noqa: E402
RESULT_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "result-v0alpha1.schema.json"
RUN_SCHEMA_PATH = HERE / "run.schema.json"
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 25_000
MAX_TRANSCRIPT_TAIL_BYTES = 12_000
MAX_NETGEN_JSON_BYTES = 64 * 1024 * 1024
MAX_NETGEN_TRANSCRIPT_BYTES = 34 * 1024 * 1024
MAX_NETGEN_STREAM_BYTES = 16 * 1024 * 1024
MAX_NETGEN_REPORT_LINE_BYTES = 256 * 1024
MAX_NETGEN_JSON_DEPTH = 64
MAX_NETGEN_JSON_NODES = 1_000_000
MAX_NETGEN_JSON_STRING_CHARS = 65_536
MAX_NETGEN_COMPARISONS = 4_096
MAX_NETGEN_EXAMPLES = 200
MAX_NATIVE_XML_DEPTH = 128
MAX_NATIVE_CATEGORIES = 4_096
MAX_NATIVE_CELLS = 100_000
MAX_NATIVE_ITEMS = 1_000_000
MAX_NATIVE_COUNT = (1 << 63) - 1
MAX_NATIVE_NAME_CHARS = 1_024
MAX_NATIVE_PATH_CHARS = 4_096
MAX_NATIVE_TAGS = 256
MAX_NATIVE_TAG_CHARS = 512
MAX_NATIVE_TAGS_CHARS = 4_096
MAX_NATIVE_DESCRIPTION_CHARS = 1_000
MAX_NATIVE_GEOMETRY_SCAN_CHARS = 65_536
MAX_NATIVE_GEOMETRIES_PER_ITEM = 8
MAX_NATIVE_COORDINATE_PAIRS = 4_096
ASCII_COUNT_RE = re.compile(r"[1-9][0-9]{0,18}")
NATIVE_NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
NATIVE_COORDINATE_PAIR_RE = re.compile(
    rf"({NATIVE_NUMBER_PATTERN})\s*,\s*({NATIVE_NUMBER_PATTERN})"
)
TRANSCRIPT_METADATA_RE = re.compile(
    rb"(stdout|stderr): retained_tail_bytes=(0|[1-9][0-9]{0,4}) "
    rb"observed_bytes=(0|[1-9][0-9]{0,18}) truncated=(true|false)"
)
TRANSITIVE_PROVENANCE_MESSAGE = (
    "KLayout decks are executable Ruby. Only the main deck, declared provenance "
    "inputs, and optional waiver database are hashed by this operation."
)
TRANSCRIPT_LIMITATION = (
    "The artifact retains bounded stdout/stderr tails, not an unbounded native log."
)
NETGEN_PROVENANCE_MESSAGE = (
    "The executable setup Tcl may read transitive files or ambient environment "
    "state that OpenADA cannot infer."
)
NETGEN_SETUP_TRUST = (
    "caller-supplied executable Tcl; OpenADA does not sandbox the setup"
)
NETGEN_TRANSCRIPT_LIMITATION = (
    "Pass or fail requires both native streams to fit the complete capture bound; "
    "the artifact is not an unbounded native log."
)
NETGEN_TRANSCRIPT_METADATA_RE = re.compile(
    rb"(stdout|stderr): retained_utf8_bytes=(0|[1-9][0-9]{0,18}) "
    rb"observed_bytes=(0|[1-9][0-9]{0,18}) truncated=(true|false)"
)
REVIEWED_NETGEN_STDERR_RE = re.compile(
    r"^Unable to permute model "
    r"(?P<model>[A-Za-z0-9_.$:+/@-]{1,256}) pins "
    r"(?P<first>[A-Za-z0-9_.$:+/@-]{1,128}), "
    r"(?P<second>[A-Za-z0-9_.$:+/@-]{1,128})\.$"
)
FINAL_LVS_MATCH_RE = re.compile(
    r"^\s*Final result:\s*Circuits match uniquely\.\s*$", re.IGNORECASE
)
UNIQUE_LVS_MATCH_RE = re.compile(
    r"^\s*(?:Circuits|Netlists) match uniquely\.\s*$", re.IGNORECASE
)
LVS_CIRCUIT_PAIR_RE = re.compile(
    r"^Circuit 1:\s*(?P<left>.*?)\s*\|Circuit 2:\s*(?P<right>.*?)\s*$",
    re.IGNORECASE,
)
LVS_DEVICE_COUNT_RE = re.compile(
    r"^Number of devices:\s*(?P<left>\d+)\s*\|Number of devices:\s*(?P<right>\d+)\s*$",
    re.IGNORECASE,
)
LVS_NET_COUNT_RE = re.compile(
    r"^Number of nets:\s*(?P<left>\d+)\s*\|Number of nets:\s*(?P<right>\d+)\s*$",
    re.IGNORECASE,
)
LVS_DEVICE_EQUIVALENCE_RE = re.compile(
    r"^Device classes\s+(?P<left>\S+)\s+and\s+(?P<right>\S+)\s+are equivalent\.\s*$",
    re.IGNORECASE,
)
LVS_MISMATCH_RE = re.compile(
    r"(?:circuits?|netlists?)\s+do\s+not\s+match|"
    r"\bno\s+matching\b|"
    r"\bmismatches?\b|"
    r"\bproperty errors?\b|"
    r"\bport errors?\b|"
    r"\bfailed (?:pin )?matching\b|"
    r"\bnetworks? match locally but not globally\b",
    re.IGNORECASE,
)
NEGATED_MISMATCH_RE = re.compile(
    r"\b(?:no|zero)\s+mismatches?\b|"
    r"\bmismatch(?:es)?\s*(?:count)?\s*[:=]\s*0\b",
    re.IGNORECASE,
)


def _require_regular_file(path: Path, *, label: str, maximum_bytes: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConformanceError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ConformanceError(f"{label} is not a regular, non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise ConformanceError(f"{label} must have exactly one hard link: {path}")
    if metadata.st_size <= 0:
        raise ConformanceError(f"{label} is empty: {path}")
    if metadata.st_size > maximum_bytes:
        raise ConformanceError(
            f"{label} exceeds the {maximum_bytes}-byte verification limit: {path}"
        )
    return metadata.st_size


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular_file(path, label=label, maximum_bytes=MAX_JSON_BYTES)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError(f"{label} root must be an object: {path}")
    return document


def _load_validator(path: Path, *, label: str) -> Draft202012Validator:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read {label} schema {path}: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConformanceError(f"invalid {label} schema: {exc.message}") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_schema(
    document: dict[str, Any], validator: Draft202012Validator, *, label: str
) -> None:
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "<root>"
    raise ConformanceError(f"{label} violates its JSON Schema at {location}: {error.message}")


def _record_map(records: Any, location: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ConformanceError(f"{location} must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConformanceError(f"{location}[{index}] must be a file record with a path")
        if record["path"] in mapped:
            raise ConformanceError(f"{location} contains duplicate path {record['path']!r}")
        mapped[record["path"]] = record
    return mapped


def _expect_equal(actual: Any, expected: Any, location: str) -> None:
    if actual != expected:
        raise ConformanceError(f"{location}: expected {expected!r}, got {actual!r}")


def _verify_input_records(operation_name: str, operation: dict, result: dict) -> None:
    actual_inputs = _record_map(result.get("inputs"), f"{operation_name}.inputs")
    expected_inputs = {record["path"]: record for record in operation["inputs"]}
    if set(actual_inputs) != set(expected_inputs):
        missing = sorted(set(expected_inputs) - set(actual_inputs))
        unexpected = sorted(set(actual_inputs) - set(expected_inputs))
        raise ConformanceError(
            f"{operation_name}.inputs paths differ; missing={missing}, unexpected={unexpected}"
        )
    for path, expected in expected_inputs.items():
        actual = actual_inputs[path]
        _expect_equal(actual.get("exists"), True, f"{operation_name}.inputs[{path}].exists")
        _expect_equal(actual.get("kind"), expected["kind"], f"{operation_name}.inputs[{path}].kind")
        _expect_equal(actual.get("role"), expected["role"], f"{operation_name}.inputs[{path}].role")
        _expect_equal(
            actual.get("sha256"), expected["sha256"], f"{operation_name}.inputs[{path}].sha256"
        )
        size = actual.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ConformanceError(f"{operation_name}.inputs[{path}].bytes must be positive")


def _verify_artifacts(
    operation_name: str, operation: dict, result: dict, evidence: Path
) -> dict[str, Path]:
    expected_records = [operation["artifact"]]
    if operation_name in DRC_OPERATION_NAMES:
        expected_records.append(operation["transcript_artifact"])
    else:
        expected_records.extend(
            [operation["json_artifact"], operation["transcript_artifact"]]
        )
    actual_artifacts = _record_map(result.get("artifacts"), f"{operation_name}.artifacts")
    expected_paths = {record["path"] for record in expected_records}
    if set(actual_artifacts) != expected_paths:
        raise ConformanceError(
            f"{operation_name}.artifacts paths differ from {sorted(expected_paths)!r}"
        )
    verified: dict[str, Path] = {}
    for expected in expected_records:
        actual = actual_artifacts[expected["path"]]
        label = f"{operation_name}.{expected['kind']}"
        _expect_equal(actual.get("exists"), True, f"{label}.exists")
        _expect_equal(actual.get("kind"), expected["kind"], f"{label}.kind")
        _expect_equal(actual.get("role"), expected["role"], f"{label}.role")

        artifact_path = evidence / expected["filename"]
        maximum_bytes = {
            "klayout-transcript": MAX_TRANSCRIPT_BYTES,
            "netgen-comparison-json": MAX_NETGEN_JSON_BYTES,
            "netgen-transcript": MAX_NETGEN_TRANSCRIPT_BYTES,
        }.get(expected["kind"], MAX_ARTIFACT_BYTES)
        size = _require_regular_file(
            artifact_path,
            label=f"{operation_name} native artifact",
            maximum_bytes=maximum_bytes,
        )
        _expect_equal(actual.get("bytes"), size, f"{label}.bytes")
        _expect_equal(
            actual.get("sha256"),
            sha256_file(artifact_path),
            f"{label}.sha256",
        )
        verified[expected["kind"]] = artifact_path
    return verified


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child.text
    return None


def _positive_ascii_count(value: str | None, *, label: str) -> int:
    text = (value or "").strip()
    if not text.isascii() or ASCII_COUNT_RE.fullmatch(text) is None:
        raise ConformanceError(f"native DRC report has invalid {label}")
    parsed = int(text)
    if parsed <= 0 or parsed > MAX_NATIVE_COUNT:
        raise ConformanceError(f"native DRC report has out-of-range {label}")
    return parsed


def _children_named(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _category_declaration_ancestry(tags: tuple[str, ...]) -> bool:
    if len(tags) < 3 or tags[:2] != ("report-database", "categories"):
        return False
    tail = tags[2:]
    return bool(
        len(tail) % 2 == 1
        and all(
            tag == ("category" if index % 2 == 0 else "categories")
            for index, tag in enumerate(tail)
        )
    )


def _category_container_ancestry(tags: tuple[str, ...]) -> bool:
    if tags == ("report-database", "categories"):
        return True
    if len(tags) < 4 or tags[:2] != ("report-database", "categories"):
        return False
    tail = tags[2:]
    return bool(
        len(tail) % 2 == 0
        and all(
            tag == ("category" if index % 2 == 0 else "categories")
            for index, tag in enumerate(tail)
        )
    )


def _parse_native_tokens(
    value: str | None,
    *,
    separator: str,
    maximum_parts: int,
    maximum_part_chars: int,
    maximum_text_chars: int,
) -> tuple[str, ...] | None:
    text = (value or "").strip()
    if not text:
        return ()
    if len(text) > maximum_text_chars:
        return None
    parts: list[str] = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset >= len(text):
            return None
        if text[offset] == "'":
            offset += 1
            characters: list[str] = []
            closed = False
            while offset < len(text):
                character = text[offset]
                offset += 1
                if character == "\\":
                    if offset >= len(text):
                        return None
                    characters.append(text[offset])
                    offset += 1
                elif character == "'":
                    closed = True
                    break
                else:
                    characters.append(character)
            if not closed:
                return None
            part = "".join(characters)
            while offset < len(text) and text[offset].isspace():
                offset += 1
            if offset < len(text) and text[offset] != separator:
                return None
        else:
            start = offset
            while offset < len(text) and text[offset] != separator:
                if text[offset] in {"'", "\\"}:
                    return None
                offset += 1
            part = text[start:offset].strip()
        if (
            not part
            or len(part) > maximum_part_chars
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
        ):
            return None
        parts.append(part)
        if len(parts) > maximum_parts:
            return None
        if offset == len(text):
            break
        if text[offset] != separator:
            return None
        offset += 1
        if offset == len(text):
            return None
    return tuple(parts)


def _parse_category_path(value: str | None) -> tuple[str, ...] | None:
    return _parse_native_tokens(
        value,
        separator=".",
        maximum_parts=MAX_NATIVE_XML_DEPTH,
        maximum_part_chars=MAX_NATIVE_NAME_CHARS,
        maximum_text_chars=MAX_NATIVE_PATH_CHARS,
    )


def _parse_item_tags(value: str | None) -> tuple[str, ...] | None:
    tags = _parse_native_tokens(
        value,
        separator=",",
        maximum_parts=MAX_NATIVE_TAGS,
        maximum_part_chars=MAX_NATIVE_TAG_CHARS,
        maximum_text_chars=MAX_NATIVE_TAGS_CHARS,
    )
    if tags is None or len(set(tags)) != len(tags):
        return None
    return tags


def _parse_native_geometry(value: str | None) -> tuple[dict[str, Any] | None, int]:
    text = (value or "").strip()
    if not text:
        return None, 0
    if len(text) > MAX_NATIVE_GEOMETRY_SCAN_CHARS:
        raise ConformanceError("native DRC item contains an overlong geometry value")
    matches = list(NATIVE_COORDINATE_PAIR_RE.finditer(text))
    if len(matches) > MAX_NATIVE_COORDINATE_PAIRS:
        raise ConformanceError("native DRC geometry exceeds the coordinate-pair limit")
    coordinates: list[list[float]] = []
    for match in matches:
        pair = [float(match.group(1)), float(match.group(2))]
        if not all(math.isfinite(coordinate) for coordinate in pair):
            raise ConformanceError("native DRC geometry contains a non-finite coordinate")
        coordinates.append(pair)
    if text.startswith("edge-pair:") and coordinates:
        return {
            "type": "edge-pair",
            "coordinates": coordinates,
            "coordinates_truncated": False,
        }, len(coordinates)
    if text.startswith("polygon:") and coordinates:
        return {
            "type": "polygon",
            "coordinates": coordinates,
            "coordinates_truncated": False,
        }, len(coordinates)
    return {"type": "unknown", "raw": text}, 0


def _verify_native_drc(path: Path, operation: dict) -> dict[str, Any]:
    tags: list[str] = []
    elements: list[ET.Element] = []
    root_checked = False
    section_counts: dict[str, int] = {}
    generator = ""
    top_cell = ""
    categories: dict[tuple[str, ...], str] = {}
    cells: set[str] = set()
    base_cells: set[str] = set()
    category_counts: dict[tuple[str, ...], int] = {}
    violations: list[dict[str, Any]] = []
    item_count = 0
    weighted_count = 0
    waived_count = 0
    total_geometry_values = 0
    retained_geometries = 0
    retained_coordinate_pairs = 0
    item_fields: dict[str, Any] | None = None
    expected = operation["native_report"]
    expected_item_count = expected.get("expected_item_count", 0)
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start":
                if element.tag != tag:
                    raise ConformanceError(
                        "native DRC report uses a non-native XML namespace"
                    )
                tags.append(tag)
                elements.append(element)
                ancestry = tuple(tags)
                if not root_checked:
                    root_checked = True
                    if ancestry != ("report-database",):
                        raise ConformanceError(
                            f"native DRC report has unexpected root element: {tag}"
                        )
                if len(tags) > MAX_NATIVE_XML_DEPTH:
                    raise ConformanceError("native DRC report exceeds the XML depth limit")
                if len(tags) == 2 and tags[0] == "report-database":
                    section_counts[tag] = section_counts.get(tag, 0) + 1
                if tag in {"generator", "top-cell"} and ancestry != (
                    "report-database",
                    tag,
                ):
                    raise ConformanceError(
                        f"native DRC {tag} field is outside its exact root ancestry"
                    )
                if tag == "categories" and not _category_container_ancestry(ancestry):
                    raise ConformanceError(
                        "native DRC categories container is outside the recursive native hierarchy"
                    )
                if tag == "cells" and ancestry != ("report-database", "cells"):
                    raise ConformanceError("native DRC cells section is not a direct root child")
                if tag == "items" and ancestry != ("report-database", "items"):
                    raise ConformanceError("native DRC items section is not a direct root child")
                if tag == "item":
                    if ancestry != ("report-database", "items", "item"):
                        raise ConformanceError(
                            "native DRC item is outside the direct root items section"
                        )
                    item_count += 1
                    if item_count > MAX_NATIVE_ITEMS:
                        raise ConformanceError("native DRC report exceeds the item-count limit")
                    item_fields = {
                        "category_count": 0,
                        "category": None,
                        "cell_count": 0,
                        "cell": None,
                        "tags_count": 0,
                        "tags": (),
                        "multiplicity_count": 0,
                        "multiplicity": None,
                        "geometry_value_count": 0,
                        "geometries": [],
                        "coordinate_pair_count": 0,
                    }
                elif tag == "category" and not (
                    ancestry == ("report-database", "items", "item", "category")
                    or _category_declaration_ancestry(ancestry)
                ):
                    raise ConformanceError(
                        "native DRC category is outside its exact native ancestry"
                    )
                elif tag == "cell" and ancestry not in {
                    ("report-database", "cells", "cell"),
                    ("report-database", "items", "item", "cell"),
                }:
                    raise ConformanceError(
                        "native DRC cell is outside its exact root ancestry"
                    )
                elif tag == "tags" and ancestry not in {
                    ("report-database", "tags"),
                    ("report-database", "items", "item", "tags"),
                }:
                    raise ConformanceError("native DRC tags field is outside its exact ancestry")
                elif tag == "multiplicity" and ancestry != (
                    "report-database",
                    "items",
                    "item",
                    "multiplicity",
                ):
                    raise ConformanceError(
                        "native DRC multiplicity is not a direct item child"
                    )
                elif tag == "values" and ancestry != (
                    "report-database",
                    "items",
                    "item",
                    "values",
                ):
                    raise ConformanceError("native DRC values field is outside its item")
                elif tag == "value" and ancestry != (
                    "report-database",
                    "items",
                    "item",
                    "values",
                    "value",
                ):
                    raise ConformanceError("native DRC value is outside its native values field")
                continue

            ancestry = tuple(tags)
            parent = tags[-2] if len(tags) > 1 else None
            parent_element = elements[-2] if len(elements) > 1 else None
            if ancestry == ("report-database", "generator"):
                if len(element):
                    raise ConformanceError(
                        "native DRC generator must contain text only"
                    )
                generator = (element.text or "").strip()
            elif ancestry == ("report-database", "top-cell"):
                if len(element):
                    raise ConformanceError(
                        "native DRC top-cell must contain text only"
                    )
                top_cell = (element.text or "").strip()
            elif tag == "category" and _category_declaration_ancestry(ancestry):
                name_fields = _children_named(element, "name")
                if len(name_fields) != 1:
                    raise ConformanceError(
                        "native DRC category must contain exactly one direct name"
                    )
                name = (name_fields[0].text or "").strip()
                if (
                    not name
                    or len(name) > MAX_NATIVE_NAME_CHARS
                    or any(ord(character) < 32 or ord(character) == 127 for character in name)
                ):
                    raise ConformanceError("native DRC report has an invalid category name")
                ancestor_names: list[str] = []
                for ancestor_tag, ancestor in zip(tags[2:-1], elements[2:-1]):
                    if ancestor_tag != "category":
                        continue
                    ancestor_name = (_child_text(ancestor, "name") or "").strip()
                    if not ancestor_name:
                        raise ConformanceError("native DRC parent category lacks a name")
                    ancestor_names.append(ancestor_name)
                category_path = tuple([*ancestor_names, name])
                if category_path in categories:
                    raise ConformanceError(
                        f"native DRC report has duplicate category path {category_path!r}"
                    )
                if len(categories) >= MAX_NATIVE_CATEGORIES:
                    raise ConformanceError("native DRC report exceeds the category-count limit")
                description_fields = _children_named(element, "description")
                if len(description_fields) > 1:
                    raise ConformanceError(
                        "native DRC category contains more than one direct description"
                    )
                description = (
                    (description_fields[0].text or "").strip()
                    if description_fields
                    else ""
                )
                if len(description) > MAX_NATIVE_DESCRIPTION_CHARS or any(
                    ord(character) < 32 and character not in {"\t", "\n", "\r"}
                    for character in description
                ):
                    raise ConformanceError(
                        "native DRC category contains an invalid description"
                    )
                categories[category_path] = description
            elif ancestry == ("report-database", "cells", "cell"):
                name_fields = _children_named(element, "name")
                variant_fields = _children_named(element, "variant")
                if len(name_fields) != 1 or len(variant_fields) > 1:
                    raise ConformanceError(
                        "native DRC cell must contain one name and at most one variant"
                    )
                name = (name_fields[0].text or "").strip()
                variant = (
                    (variant_fields[0].text or "").strip() if variant_fields else ""
                )
                if (
                    (not name and bool(variant))
                    or len(name) > MAX_NATIVE_NAME_CHARS
                    or len(variant) > MAX_NATIVE_NAME_CHARS
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in name + variant
                    )
                ):
                    raise ConformanceError("native DRC report has an invalid cell identity")
                identity = f"{name}:{variant}" if variant else name
                if identity in cells:
                    raise ConformanceError("native DRC report has a duplicate cell identity")
                if len(cells) >= MAX_NATIVE_CELLS:
                    raise ConformanceError("native DRC report exceeds the cell-count limit")
                cells.add(identity)
                if name:
                    base_cells.add(name)
            elif ancestry == ("report-database", "items", "item", "category"):
                assert item_fields is not None
                item_fields["category_count"] += 1
                category_path = _parse_category_path(element.text)
                if not category_path:
                    raise ConformanceError("native DRC item has an invalid category path")
                item_fields["category"] = category_path
            elif ancestry == ("report-database", "items", "item", "cell"):
                assert item_fields is not None
                item_fields["cell_count"] += 1
                cell = (element.text or "").strip()
                if len(cell) > MAX_NATIVE_PATH_CHARS or any(
                    ord(character) < 32 or ord(character) == 127 for character in cell
                ):
                    raise ConformanceError("native DRC item has an invalid cell identity")
                item_fields["cell"] = cell
            elif ancestry == ("report-database", "items", "item", "tags"):
                assert item_fields is not None
                item_fields["tags_count"] += 1
                native_tags = _parse_item_tags(element.text)
                if native_tags is None:
                    raise ConformanceError("native DRC item has invalid or duplicate tags")
                item_fields["tags"] = native_tags
            elif ancestry == ("report-database", "items", "item", "multiplicity"):
                assert item_fields is not None
                item_fields["multiplicity_count"] += 1
                item_fields["multiplicity"] = _positive_ascii_count(
                    element.text,
                    label="item multiplicity",
                )
            elif ancestry == (
                "report-database",
                "items",
                "item",
                "values",
                "value",
            ):
                assert item_fields is not None
                item_fields["geometry_value_count"] += 1
                total_geometry_values += 1
                if item_fields["geometry_value_count"] > MAX_NATIVE_GEOMETRIES_PER_ITEM:
                    raise ConformanceError(
                        "native DRC item exceeds the reviewed geometry-value limit"
                    )
                geometry, coordinate_pairs = _parse_native_geometry(element.text)
                if geometry is not None:
                    item_fields["geometries"].append(geometry)
                    retained_geometries += 1
                    item_fields["coordinate_pair_count"] += coordinate_pairs
                    retained_coordinate_pairs += coordinate_pairs
                    if retained_coordinate_pairs > MAX_NATIVE_COORDINATE_PAIRS:
                        raise ConformanceError(
                            "native DRC report exceeds the coordinate-pair limit"
                        )
            elif ancestry == ("report-database", "items", "item"):
                assert item_fields is not None
                if (
                    item_fields["category_count"] != 1
                    or item_fields["cell_count"] != 1
                    or item_fields["tags_count"] != 1
                ):
                    raise ConformanceError(
                        "native DRC item must contain one direct tags, category, and cell field"
                    )
                if item_fields["multiplicity_count"] != 1:
                    raise ConformanceError(
                        "native DRC item must contain one positive ASCII multiplicity"
                    )
                multiplicity = item_fields["multiplicity"]
                assert isinstance(multiplicity, int)
                category_path = item_fields["category"]
                cell = item_fields["cell"]
                if category_path not in categories:
                    raise ConformanceError("native DRC item references an undeclared category")
                if cell not in cells:
                    raise ConformanceError("native DRC item references an undeclared cell")
                if weighted_count > MAX_NATIVE_COUNT - multiplicity:
                    raise ConformanceError("native DRC weighted count exceeds the supported range")
                weighted_count += multiplicity
                if "waived" in item_fields["tags"]:
                    waived_count += multiplicity
                category_counts[category_path] = (
                    category_counts.get(category_path, 0) + multiplicity
                )
                if len(violations) <= expected_item_count:
                    violations.append(
                        {
                            "category": ".".join(category_path),
                            "category_path": list(category_path),
                            "description": categories[category_path],
                            "cell": cell,
                            "multiplicity": multiplicity,
                            "waived": "waived" in item_fields["tags"],
                            "tags": list(item_fields["tags"]),
                            "geometries": item_fields["geometries"],
                            "geometries_truncated": (
                                item_fields["geometry_value_count"]
                                > len(item_fields["geometries"])
                            ),
                            "geometry_value_count": item_fields["geometry_value_count"],
                            "coordinate_pair_count": item_fields["coordinate_pair_count"],
                        }
                    )
                item_fields = None
            if tag in {"category", "cell", "item", "value"} or parent == "report-database":
                element.clear()
            if tag in {"category", "cell", "item", "tags", "value"} and parent_element is not None:
                try:
                    parent_element.remove(element)
                except ValueError:
                    pass
            tags.pop()
            elements.pop()
    except ET.ParseError as exc:
        raise ConformanceError(f"native DRC report is malformed XML: {exc}") from exc
    except OSError as exc:
        raise ConformanceError(f"cannot parse native DRC report: {exc}") from exc
    if not root_checked or tags or elements:
        raise ConformanceError("native DRC report has an incomplete XML root")
    for section in ("generator", "top-cell", "categories", "cells", "items"):
        if section_counts.get(section) != 1:
            raise ConformanceError(
                f"native DRC report must contain one direct {section} section"
            )
    _expect_equal(generator, expected["generator"], "native DRC generator")
    _expect_equal(top_cell, expected["top_cell"], "native DRC top cell")
    if top_cell not in base_cells:
        raise ConformanceError("native DRC top cell is absent from the cells section")
    if len(categories) < expected["minimum_categories"]:
        raise ConformanceError("native DRC report contains no executed check categories")
    expected_total = expected.get("expected_total_violations", 0)
    expected_waived = expected.get("expected_waived_violations", 0)
    if expected_item_count == 0 and (item_count != 0 or weighted_count != 0):
        raise ConformanceError(
            f"native DRC report contains {item_count} item(s), weighted as "
            f"{weighted_count} violation(s)"
        )
    _expect_equal(item_count, expected_item_count, "native DRC item count")
    _expect_equal(weighted_count, expected_total, "native DRC total violations")
    _expect_equal(waived_count, expected_waived, "native DRC waived violations")
    count_summaries = [
        {
            "category": ".".join(category_path),
            "category_path": list(category_path),
            "violations": count,
        }
        for category_path, count in sorted(
            category_counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    _expect_equal(
        count_summaries,
        expected.get("expected_category_counts", []),
        "native DRC category counts",
    )
    expected_violations = expected.get("expected_violations", [])
    _expect_equal(
        len(violations),
        len(expected_violations),
        "native DRC reviewed violation count",
    )
    reviewed_violations = [
        {key: violation.get(key) for key in reviewed}
        for violation, reviewed in zip(violations, expected_violations)
    ]
    _expect_equal(
        reviewed_violations,
        expected_violations,
        "native DRC reviewed violations",
    )
    normalization = {
        "geometry_values": total_geometry_values,
        "retained_geometries": retained_geometries,
        "retained_coordinate_pairs": retained_coordinate_pairs,
        "global_geometry_limit_reached": False,
    }
    _expect_equal(
        normalization,
        expected.get(
            "expected_normalization",
            {
                "geometry_values": 0,
                "retained_geometries": 0,
                "retained_coordinate_pairs": 0,
                "global_geometry_limit_reached": False,
            },
        ),
        "native DRC normalization",
    )
    normalized_violations = [
        {
            key: value
            for key, value in violation.items()
            if key not in {"geometry_value_count", "coordinate_pair_count"}
        }
        for violation in violations
    ]
    return {
        "generator": generator,
        "top_cell": top_cell,
        "category_count": len(categories),
        "cell_count": len(cells),
        "item_count": item_count,
        "total_violations": weighted_count,
        "waived_violations": waived_count,
        "category_counts": count_summaries,
        "violations": normalized_violations,
        "normalization": normalization,
    }


def _take_transcript_line(body: bytes, offset: int, *, label: str) -> tuple[bytes, int]:
    end = body.find(b"\n", offset)
    if end < 0 or end - offset > 256:
        raise ConformanceError(f"native DRC transcript has an invalid {label} line")
    return body[offset:end], end + 1


def _transcript_stream_metadata(line: bytes, *, stream: str) -> dict[str, Any]:
    match = TRANSCRIPT_METADATA_RE.fullmatch(line)
    if match is None or match.group(1) != stream.encode("ascii"):
        raise ConformanceError(f"native DRC transcript has invalid {stream} metadata")
    retained = int(match.group(2))
    observed = int(match.group(3))
    if retained > MAX_TRANSCRIPT_TAIL_BYTES or observed > MAX_NATIVE_COUNT:
        raise ConformanceError(f"native DRC transcript has out-of-range {stream} metadata")
    truncated = match.group(4) == b"true"
    # observed_bytes counts raw process bytes, while retained_tail_bytes counts
    # the UTF-8-normalized artifact tail. Replacement characters can expand an
    # untruncated raw stream, so their lengths are intentionally not equated.
    if truncated != (observed > MAX_TRANSCRIPT_TAIL_BYTES) or (
        (observed == 0) != (retained == 0)
    ) or (
        truncated
        and not MAX_TRANSCRIPT_TAIL_BYTES - 3 <= retained <= MAX_TRANSCRIPT_TAIL_BYTES
    ):
        raise ConformanceError(
            f"native DRC transcript has inconsistent {stream} metadata"
        )
    return {
        "retained_tail_bytes": retained,
        "observed_bytes": observed,
        "truncated": truncated,
    }


def _verify_native_transcript(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ConformanceError(f"cannot read native DRC transcript: {exc}") from exc
    if len(body) > MAX_TRANSCRIPT_BYTES:
        raise ConformanceError("native DRC transcript exceeds its bounded size")

    offset = 0
    header, offset = _take_transcript_line(body, offset, label="header")
    if header != b"OpenADA bounded KLayout process transcript":
        raise ConformanceError("native DRC transcript lacks the OpenADA capture header")

    stdout_line, offset = _take_transcript_line(body, offset, label="stdout metadata")
    stdout = _transcript_stream_metadata(stdout_line, stream="stdout")
    marker, offset = _take_transcript_line(body, offset, label="stdout marker")
    if marker != b"--- stdout tail ---":
        raise ConformanceError("native DRC transcript has an invalid stdout marker")
    stdout_end = offset + stdout["retained_tail_bytes"]
    if stdout_end >= len(body) or body[stdout_end : stdout_end + 1] != b"\n":
        raise ConformanceError("native DRC transcript stdout tail has the wrong byte length")
    stdout_bytes = body[offset:stdout_end]
    offset = stdout_end + 1

    stderr_line, offset = _take_transcript_line(body, offset, label="stderr metadata")
    stderr = _transcript_stream_metadata(stderr_line, stream="stderr")
    marker, offset = _take_transcript_line(body, offset, label="stderr marker")
    if marker != b"--- stderr tail ---":
        raise ConformanceError("native DRC transcript has an invalid stderr marker")
    stderr_end = offset + stderr["retained_tail_bytes"]
    if stderr_end + 1 != len(body) or body[stderr_end:] != b"\n":
        raise ConformanceError("native DRC transcript stderr tail has the wrong byte length")
    stderr_bytes = body[offset:stderr_end]
    try:
        stdout_tail = stdout_bytes.decode("utf-8", errors="strict")
        stderr_tail = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ConformanceError(f"native DRC transcript tail is not UTF-8: {exc}") from exc
    return {
        "stdout_retained_bytes": stdout["retained_tail_bytes"],
        "stderr_retained_bytes": stderr["retained_tail_bytes"],
        "stdout_observed_bytes": stdout["observed_bytes"],
        "stderr_observed_bytes": stderr["observed_bytes"],
        "stdout_truncated": stdout["truncated"],
        "stderr_truncated": stderr["truncated"],
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }


def _bounded_contract_text(value: str, *, limit: int = 4_000) -> str:
    if len(value) <= limit:
        return value
    marker = " ... [truncated] ... "
    retained = limit - len(marker)
    head = retained // 2
    return value[:head] + marker + value[-(retained - head) :]


def _verify_drc_diagnostics(operation_name: str, result: dict[str, Any]) -> None:
    expected = [
        {
            "severity": "warning",
            "code": "provenance.transitive_rules_unenumerated",
            "message": TRANSITIVE_PROVENANCE_MESSAGE,
        }
    ]
    _expect_equal(
        result.get("diagnostics"), expected, f"{operation_name}.diagnostics"
    )


def _verify_drc_result_data(
    operation_name: str,
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    report_path: Path,
    transcript_path: Path,
    native_report: dict[str, Any],
    native_transcript: dict[str, Any],
) -> None:
    data = result.get("data")
    if not isinstance(data, dict):
        raise ConformanceError(f"{operation_name}.data must be an object")
    arguments = operation["arguments"]
    report_record = _record_map(
        result["artifacts"], f"{operation_name}.artifacts"
    )[arguments["report"]]
    transcript_record = _record_map(
        result["artifacts"], f"{operation_name}.artifacts"
    )[
        operation["transcript_artifact"]["path"]
    ]

    _expect_equal(
        data.get("drc_clean"),
        operation["expect"]["drc_clean"],
        f"{operation_name}.data.drc_clean",
    )
    _expect_equal(data.get("inputs_stable"), True, "drc.data.inputs_stable")
    _expect_equal(data.get("changed_inputs"), [], "drc.data.changed_inputs")
    _expect_equal(data.get("working_directory"), "/evidence", "drc.data.working_directory")
    _expect_equal(
        data.get("working_directory_is_sandbox"),
        False,
        "drc.data.working_directory_is_sandbox",
    )
    _expect_equal(data.get("top_cell"), arguments["top_cell"], "drc.data.top_cell")
    _expect_equal(data.get("deck_variables"), [], "drc.data.deck_variables")
    _expect_equal(
        data.get("startup"),
        {
            "batch_flag": "-b",
            "database_only": True,
            "configuration_files": "disabled",
            "implicit_macros": "disabled",
        },
        "drc.data.startup",
    )
    _expect_equal(
        data.get("transitive_rule_inputs_enumerated"),
        False,
        "drc.data.transitive_rule_inputs_enumerated",
    )
    _expect_equal(
        data.get("ambient_environment_enumerated"),
        False,
        "drc.data.ambient_environment_enumerated",
    )
    _expect_equal(
        data.get("deck_trust"),
        "caller-supplied executable Ruby; OpenADA does not sandbox the deck",
        "drc.data.deck_trust",
    )
    _expect_equal(
        data.get("environment"),
        {
            "PDK": None,
            "PDK_ROOT": "/foss/pdks",
            "KLAYOUT_PATH": None,
            "KLAYOUT_HOME": None,
        },
        "drc.data.environment",
    )

    report_output = data.get("report_output")
    if not isinstance(report_output, dict):
        raise ConformanceError("drc.data.report_output must be an object")
    for key, expected in {
        "ownership": "variable",
        "binding_variable": "report",
        "fresh_required": True,
        "parent_anchored": True,
        "declared_path": arguments["report"],
        "path": arguments["report"],
    }.items():
        _expect_equal(report_output.get(key), expected, f"drc.data.report_output.{key}")
    capture = report_output.get("capture")
    if not isinstance(capture, dict):
        raise ConformanceError("drc.data.report_output.capture must be an object")
    for key, expected in {
        "path": arguments["report"],
        "origin": "deck",
        "parent_anchored": True,
        "status": "valid",
        "bytes": report_record["bytes"],
        "sha256": report_record["sha256"],
    }.items():
        _expect_equal(capture.get(key), expected, f"drc.data.report_output.capture.{key}")

    report = data.get("report")
    if not isinstance(report, dict):
        raise ConformanceError("drc.data.report must be an object")
    expected_validation = {
        "valid": True,
        "reason": "lyrdb.valid",
        "bytes": report_path.stat().st_size,
    }
    _expect_equal(report.get("validation"), expected_validation, "drc.data.report.validation")
    _expect_equal(
        capture.get("validation"),
        expected_validation,
        "drc.data.report_output.capture.validation",
    )
    for key in (
        "generator",
        "top_cell",
        "category_count",
        "cell_count",
        "item_count",
        "total_violations",
        "waived_violations",
    ):
        _expect_equal(report.get(key), native_report[key], f"drc.data.report.{key}")
    _expect_equal(
        report.get("generator_script"),
        arguments["rules"],
        "drc.data.report.generator_script",
    )
    _expect_equal(
        report.get("category_counts"),
        native_report["category_counts"],
        f"{operation_name}.data.report.category_counts",
    )
    _expect_equal(
        report.get("category_counts_truncated"),
        False,
        "drc.data.report.category_counts_truncated",
    )
    _expect_equal(
        report.get("violations"),
        native_report["violations"],
        f"{operation_name}.data.report.violations",
    )
    _expect_equal(
        report.get("violations_truncated"),
        False,
        "drc.data.report.violations_truncated",
    )
    _expect_equal(
        report.get("normalization"),
        native_report["normalization"],
        f"{operation_name}.data.report.normalization",
    )

    waiver = data.get("waiver_database")
    if not isinstance(waiver, dict):
        raise ConformanceError("drc.data.waiver_database must be an object")
    _expect_equal(
        waiver,
        {
            "policy": "disabled-by-absence",
            "path": arguments["report"] + ".w",
            "declared": False,
            "status": "absent",
        },
        "drc.data.waiver_database",
    )

    transcript = data.get("transcript")
    if not isinstance(transcript, dict):
        raise ConformanceError("drc.data.transcript must be an object")
    transcript_expected = {
        "path": operation["transcript_artifact"]["path"],
        "origin": "openada",
        "capture_policy": "bounded process tails",
        "status": "valid",
        "bytes": transcript_record["bytes"],
        "sha256": transcript_record["sha256"],
        "stdout_retained_bytes": native_transcript["stdout_retained_bytes"],
        "stderr_retained_bytes": native_transcript["stderr_retained_bytes"],
        "stdout_observed_bytes": native_transcript["stdout_observed_bytes"],
        "stderr_observed_bytes": native_transcript["stderr_observed_bytes"],
        "stdout_truncated": native_transcript["stdout_truncated"],
        "stderr_truncated": native_transcript["stderr_truncated"],
        "stdout_tail": _bounded_contract_text(native_transcript["stdout_tail"]),
        "stderr_tail": _bounded_contract_text(native_transcript["stderr_tail"]),
        "limitation": TRANSCRIPT_LIMITATION,
    }
    for key, expected in transcript_expected.items():
        _expect_equal(transcript.get(key), expected, f"drc.data.transcript.{key}")
    _expect_equal(transcript_path.stat().st_size, transcript_record["bytes"], "drc transcript bytes")


def _verify_native_lvs_report(path: Path, *, expected_cell: str) -> dict[str, Any]:
    try:
        body = path.read_bytes()
        text = body.decode("utf-8", errors="strict")
    except OSError as exc:
        raise ConformanceError(f"cannot parse native LVS report: {exc}") from exc
    except UnicodeError as exc:
        raise ConformanceError(f"native LVS report is not UTF-8: {exc}") from exc

    saw_final_match = False
    saw_unique_match = False
    mismatch_lines: list[str] = []
    trailing: list[str] = []
    device_counts: list[int] | None = None
    node_counts: list[int] | None = None
    summary_binding: list[str] | None = None
    pins_binding: list[str] | None = None
    device_classes_binding: list[str] | None = None
    pin_lists_equivalent = False
    section: str | None = None
    summary_pair_count = 0
    pins_pair_count = 0
    pin_equivalence_count = 0
    device_equivalence_count = 0
    final_match_count = 0
    unique_match_count = 0
    for line in text.splitlines():
        if len(line.encode("utf-8")) > MAX_NETGEN_REPORT_LINE_BYTES:
            raise ConformanceError("native LVS report contains an overlong line")
        stripped = line.strip()
        if stripped:
            trailing.append(stripped)
            trailing = trailing[-8:]
        if stripped == "Subcircuit summary:":
            section = "summary"
        elif stripped == "Subcircuit pins:":
            section = "pins"
        pair = LVS_CIRCUIT_PAIR_RE.fullmatch(stripped)
        if pair and section == "summary":
            summary_pair_count += 1
            if summary_binding is None:
                summary_binding = [pair.group("left"), pair.group("right")]
        elif pair and section == "pins":
            pins_pair_count += 1
            if pins_binding is None:
                pins_binding = [pair.group("left"), pair.group("right")]
        if FINAL_LVS_MATCH_RE.fullmatch(stripped):
            saw_final_match = True
            final_match_count += 1
        if UNIQUE_LVS_MATCH_RE.fullmatch(stripped):
            saw_unique_match = True
            unique_match_count += 1
        mismatch = bool(LVS_MISMATCH_RE.search(stripped)) and not bool(
            NEGATED_MISMATCH_RE.search(stripped)
        )
        unexpected_final = stripped.lower().startswith("final result:") and not bool(
            FINAL_LVS_MATCH_RE.fullmatch(stripped)
        )
        if mismatch or unexpected_final:
            mismatch_lines.append(stripped[:1_000])
        device_match = LVS_DEVICE_COUNT_RE.fullmatch(stripped)
        if device_match and device_counts is None:
            device_counts = [
                int(device_match.group("left")),
                int(device_match.group("right")),
            ]
            if any(count > MAX_NATIVE_COUNT for count in device_counts):
                raise ConformanceError("native LVS report has an out-of-range device count")
        net_match = LVS_NET_COUNT_RE.fullmatch(stripped)
        if net_match and node_counts is None:
            node_counts = [int(net_match.group("left")), int(net_match.group("right"))]
            if any(count > MAX_NATIVE_COUNT for count in node_counts):
                raise ConformanceError("native LVS report has an out-of-range node count")
        if stripped == "Cell pin lists are equivalent.":
            pin_lists_equivalent = True
            pin_equivalence_count += 1
        device_equivalence = LVS_DEVICE_EQUIVALENCE_RE.fullmatch(stripped)
        if device_equivalence:
            device_equivalence_count += 1
            device_classes_binding = [
                device_equivalence.group("left"),
                device_equivalence.group("right"),
            ]

    terminal_is_final = bool(
        trailing
        and (
            FINAL_LVS_MATCH_RE.fullmatch(trailing[-1])
            or (
                trailing[-1] == "."
                and len(trailing) >= 2
                and FINAL_LVS_MATCH_RE.fullmatch(trailing[-2])
            )
        )
    )
    if mismatch_lines:
        raise ConformanceError(
            f"native LVS report contains mismatch evidence: {mismatch_lines[:3]!r}"
        )
    if not saw_unique_match or not saw_final_match or not terminal_is_final:
        raise ConformanceError("native LVS report lacks the unique-match final result")
    expected_binding = [expected_cell, expected_cell]
    structure_complete = bool(
        summary_binding == expected_binding
        and device_counts is not None
        and node_counts is not None
        and device_counts[0] == device_counts[1]
        and node_counts[0] == node_counts[1]
        and pins_binding == expected_binding
        and pin_lists_equivalent
        and device_classes_binding == expected_binding
        and summary_pair_count == 1
        and pins_pair_count == 1
        and pin_equivalence_count == 1
        and device_equivalence_count == 1
        and final_match_count == 1
        and unique_match_count >= 1
    )
    if not structure_complete:
        raise ConformanceError(
            "native LVS report does not bind its summary, pins, and device classes "
            f"to {expected_cell!r}"
        )
    return {
        "validation": {"valid": True, "bytes": len(body), "reason": "report.valid"},
        "outcome": "pass",
        "final_match": True,
        "legacy_terminal_match": False,
        "unique_match_markers": True,
        "terminal_outcome": "pass",
        "terminal_style": "final-result",
        "terminal_conflict": False,
        "top_cell": expected_cell,
        "comparison_count": summary_pair_count,
        "top_comparison_count": 1,
        "summary_binding": summary_binding,
        "pins_binding": pins_binding,
        "device_classes_binding": device_classes_binding,
        "pin_lists_equivalent": True,
        "structure_complete": True,
        "device_counts": device_counts,
        "node_counts": node_counts,
        "mismatch_count": 0,
        "mismatches": [],
        "mismatches_truncated": False,
    }


class _DuplicateNativeJSONKey(ValueError):
    pass


def _native_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateNativeJSONKey(f"duplicate key {key!r}")
        value[key] = item
    return value


def _reject_native_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _check_native_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NETGEN_JSON_NODES:
            raise ValueError("native LVS JSON exceeds the node limit")
        if depth > MAX_NETGEN_JSON_DEPTH:
            raise ValueError("native LVS JSON exceeds the depth limit")
        if isinstance(current, dict):
            for key, item in current.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > MAX_NETGEN_JSON_STRING_CHARS:
            raise ValueError("native LVS JSON contains an overlong string")
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("native LVS JSON contains a non-finite number")


def _native_device_sides(value: Any) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("devices must contain two sides")
    sides: list[list[tuple[str, int]]] = []
    for side in value:
        if not isinstance(side, list):
            raise ValueError("each devices side must be an array")
        normalized: list[tuple[str, int]] = []
        names: set[str] = set()
        for entry in side:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not entry[0]
                or not isinstance(entry[1], int)
                or isinstance(entry[1], bool)
                or entry[1] < 0
                or entry[1] > MAX_NATIVE_COUNT
            ):
                raise ValueError("native LVS JSON has an invalid device count")
            if entry[0] in names:
                raise ValueError("native LVS JSON has a duplicate device class")
            names.add(entry[0])
            normalized.append((entry[0], entry[1]))
        sides.append(sorted(normalized))
    return sides[0], sides[1]


def _native_pin_sides(value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("pins must contain two sides")
    if any(
        not isinstance(side, list)
        or not all(isinstance(pin, str) and pin for pin in side)
        or len(set(side)) != len(side)
        for side in value
    ):
        raise ValueError("native LVS JSON has an invalid pin list")
    return value[0], value[1]


def _verify_native_lvs_json(path: Path, *, expected_cell: str) -> dict[str, Any]:
    try:
        body = path.read_bytes()
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_native_json_object,
            parse_constant=_reject_native_json_constant,
        )
        _check_native_json_shape(payload)
        if (
            not isinstance(payload, list)
            or not payload
            or len(payload) > MAX_NETGEN_COMPARISONS
        ):
            raise ValueError("native LVS JSON must be a bounded nonempty comparison array")

        known_keys = {
            "name",
            "devices",
            "nets",
            "pins",
            "badnets",
            "badelements",
            "properties",
            "goodnets",
            "goodelements",
        }
        top_matches = 0
        mismatch_count = 0
        mismatch_examples: list[str] = []
        top_device_counts: list[list[list[Any]]] | None = None
        top_node_counts: list[int] | None = None
        top_pin_counts: list[int] | None = None
        for index, comparison in enumerate(payload):
            if not isinstance(comparison, dict) or set(comparison) - known_keys:
                raise ValueError("native LVS JSON comparison has an unknown shape")
            names = comparison.get("name")
            if (
                not isinstance(names, list)
                or len(names) != 2
                or not all(isinstance(name, str) and name for name in names)
            ):
                raise ValueError("native LVS JSON comparison lacks two cell names")
            left_devices, right_devices = _native_device_sides(comparison.get("devices"))
            nets = comparison.get("nets")
            if (
                not isinstance(nets, list)
                or len(nets) != 2
                or any(
                    not isinstance(count, int) or isinstance(count, bool) or count < 0
                    or count > MAX_NATIVE_COUNT
                    for count in nets
                )
            ):
                raise ValueError("native LVS JSON has invalid net counts")
            for field in ("badnets", "badelements"):
                if not isinstance(comparison.get(field), list):
                    raise ValueError(f"native LVS JSON lacks {field}")
            properties = comparison.get("properties", [])
            if not isinstance(properties, list):
                raise ValueError("native LVS JSON properties must be an array")
            pins_value = comparison.get("pins")
            pins = _native_pin_sides(pins_value) if pins_value is not None else None

            reasons: list[str] = []
            if left_devices != right_devices:
                reasons.append("device-counts")
            if nets[0] != nets[1]:
                reasons.append("net-counts")
            if pins is not None and pins[0] != pins[1]:
                reasons.append("pins")
            for field in ("badnets", "badelements", "properties"):
                if comparison.get(field):
                    reasons.append(field)
            if names == [expected_cell, expected_cell]:
                top_matches += 1
                top_device_counts = [
                    [[name, count] for name, count in left_devices],
                    [[name, count] for name, count in right_devices],
                ]
                top_node_counts = [nets[0], nets[1]]
                top_pin_counts = [len(pins[0]), len(pins[1])] if pins is not None else None
                if pins is None:
                    reasons.append("missing-pins")
            mismatch_count += len(reasons)
            for reason in reasons:
                if len(mismatch_examples) < MAX_NETGEN_EXAMPLES:
                    mismatch_examples.append(f"comparison[{index}].{reason}")
        if top_matches != 1:
            raise ValueError(
                "native LVS JSON does not contain exactly one requested top comparison"
            )
        if mismatch_count:
            raise ConformanceError(
                f"native LVS JSON contains mismatch evidence: {mismatch_examples[:3]!r}"
            )
        return {
            "validation": {"valid": True, "bytes": len(body), "reason": "json.valid"},
            "outcome": "pass",
            "comparison_count": len(payload),
            "top_cell": expected_cell,
            "top_comparison_count": 1,
            "device_counts": top_device_counts,
            "node_counts": top_node_counts,
            "pin_counts": top_pin_counts,
            "mismatch_count": 0,
            "mismatches": [],
            "mismatches_truncated": False,
        }
    except ConformanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ConformanceError(f"cannot parse native LVS JSON: {exc}") from exc


def _take_netgen_transcript_line(
    body: bytes, offset: int, *, label: str
) -> tuple[bytes, int]:
    end = body.find(b"\n", offset)
    if end < 0 or end - offset > 256:
        raise ConformanceError(f"native LVS transcript has an invalid {label} line")
    return body[offset:end], end + 1


def _netgen_stream_metadata(line: bytes, *, stream: str) -> dict[str, Any]:
    match = NETGEN_TRANSCRIPT_METADATA_RE.fullmatch(line)
    if match is None or match.group(1) != stream.encode("ascii"):
        raise ConformanceError(f"native LVS transcript has invalid {stream} metadata")
    retained = int(match.group(2))
    observed = int(match.group(3))
    truncated = match.group(4) == b"true"
    if observed > MAX_NATIVE_COUNT or retained > MAX_NETGEN_STREAM_BYTES:
        raise ConformanceError(f"native LVS transcript has out-of-range {stream} metadata")
    if truncated != (observed > MAX_NETGEN_STREAM_BYTES) or (
        not truncated and retained != observed
    ):
        raise ConformanceError(f"native LVS transcript has inconsistent {stream} metadata")
    return {"retained": retained, "observed": observed, "truncated": truncated}


def _verify_native_lvs_transcript(path: Path, *, setup_path: str) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ConformanceError(f"cannot read native LVS transcript: {exc}") from exc
    if len(body) > MAX_NETGEN_TRANSCRIPT_BYTES:
        raise ConformanceError("native LVS transcript exceeds its bounded size")

    offset = 0
    header, offset = _take_netgen_transcript_line(body, offset, label="header")
    if header != b"OpenADA bounded complete Netgen process transcript":
        raise ConformanceError("native LVS transcript lacks the OpenADA capture header")
    stdout_line, offset = _take_netgen_transcript_line(
        body, offset, label="stdout metadata"
    )
    stdout_meta = _netgen_stream_metadata(stdout_line, stream="stdout")
    marker, offset = _take_netgen_transcript_line(body, offset, label="stdout marker")
    if marker != b"--- stdout ---":
        raise ConformanceError("native LVS transcript has an invalid stdout marker")
    stdout_end = offset + stdout_meta["retained"]
    if stdout_end >= len(body) or body[stdout_end : stdout_end + 1] != b"\n":
        raise ConformanceError("native LVS transcript stdout has the wrong byte length")
    stdout_bytes = body[offset:stdout_end]
    offset = stdout_end + 1

    stderr_line, offset = _take_netgen_transcript_line(
        body, offset, label="stderr metadata"
    )
    stderr_meta = _netgen_stream_metadata(stderr_line, stream="stderr")
    marker, offset = _take_netgen_transcript_line(body, offset, label="stderr marker")
    if marker != b"--- stderr ---":
        raise ConformanceError("native LVS transcript has an invalid stderr marker")
    stderr_end = offset + stderr_meta["retained"]
    if stderr_end + 1 != len(body) or body[stderr_end:] != b"\n":
        raise ConformanceError("native LVS transcript stderr has the wrong byte length")
    stderr_bytes = body[offset:stderr_end]
    try:
        stdout = stdout_bytes.decode("utf-8", errors="strict")
        stderr = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ConformanceError(f"native LVS transcript is not UTF-8: {exc}") from exc

    stdout_lines = stdout.splitlines()
    stderr_lines = stderr.splitlines()
    reviewed_stderr = [
        line for line in stderr_lines if REVIEWED_NETGEN_STDERR_RE.fullmatch(line)
    ]
    unrecognized_stderr = [
        line for line in stderr_lines if not REVIEWED_NETGEN_STDERR_RE.fullmatch(line)
    ]
    setup_read = any(line.strip() == f"Reading setup file {setup_path}" for line in stdout_lines)
    setup_error = any(
        re.fullmatch(
            r"Warning:\s+There were errors reading the setup file", line.strip(), re.I
        )
        for line in stdout_lines
    ) or any(
        re.match(r"^Error\s+.+:\d+\s+\(ignoring\),", line.strip(), re.I)
        for line in stderr_lines
    )
    stdout_error = any(
        re.match(r"^Error(?:\s|:)", line.strip(), re.I) for line in stdout_lines
    )
    lvs_done = any(line.strip() == "LVS Done." for line in stdout_lines)
    complete = not stdout_meta["truncated"] and not stderr_meta["truncated"]
    stderr_accepted = bool(
        (stderr_meta["observed"] == 0 and not stderr_lines)
        or (stderr_lines and not unrecognized_stderr)
    )
    assessment = {
        "complete": complete,
        "utf8_valid": True,
        "setup_read": setup_read,
        "setup_error": setup_error,
        "stdout_error": stdout_error,
        "stderr_empty": stderr_meta["observed"] == 0,
        "stderr_policy": "empty-or-reviewed-netgen-permute-warning",
        "stderr_line_count": len(stderr_lines),
        "stderr_reviewed_warning_count": len(reviewed_stderr),
        "stderr_unrecognized_count": len(unrecognized_stderr),
        "stderr_accepted": stderr_accepted,
        "lvs_done": lvs_done,
        "clean": bool(
            complete
            and setup_read
            and not setup_error
            and not stdout_error
            and stderr_accepted
            and lvs_done
        ),
    }
    if not assessment["clean"]:
        raise ConformanceError(
            f"native LVS transcript does not prove a clean setup and completion: {assessment!r}"
        )
    return {
        "bytes": len(body),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_retained_bytes": stdout_meta["retained"],
        "stderr_retained_bytes": stderr_meta["retained"],
        "stdout_observed_bytes": stdout_meta["observed"],
        "stderr_observed_bytes": stderr_meta["observed"],
        "stdout_truncated": stdout_meta["truncated"],
        "stderr_truncated": stderr_meta["truncated"],
        "assessment": assessment,
    }


def _verify_lvs_diagnostics(result: dict[str, Any], *, reviewed_stderr_count: int) -> None:
    expected = [
        {
            "severity": "warning",
            "code": "netgen.provenance_incomplete",
            "message": NETGEN_PROVENANCE_MESSAGE,
        }
    ]
    if reviewed_stderr_count:
        expected.append(
            {
                "severity": "warning",
                "code": "netgen.stderr_reviewed_warning",
                "message": (
                    f"Netgen emitted {reviewed_stderr_count} reviewed "
                    "'Unable to permute model ... pins ...' warning line(s)."
                ),
            }
        )
    _expect_equal(result.get("diagnostics"), expected, "lvs.diagnostics")


def _verify_lvs_result_data(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    native_report: dict[str, Any],
    native_json: dict[str, Any],
    native_transcript: dict[str, Any],
) -> None:
    records = _record_map(result["artifacts"], "lvs.artifacts")
    report_record = records[operation["artifact"]["path"]]
    json_record = records[operation["json_artifact"]["path"]]
    transcript_record = records[operation["transcript_artifact"]["path"]]
    json_device_totals = [
        sum(count for _, count in side) for side in native_json["device_counts"]
    ]
    if any(total > MAX_NATIVE_COUNT for total in json_device_totals):
        raise ConformanceError("native LVS JSON device totals exceed the supported range")
    if native_report["device_counts"] != json_device_totals:
        raise ConformanceError(
            "native LVS report and JSON device counts disagree: "
            f"report={native_report['device_counts']!r}, json={json_device_totals!r}"
        )
    if native_report["node_counts"] != native_json["node_counts"]:
        raise ConformanceError(
            "native LVS report and JSON node counts disagree: "
            f"report={native_report['node_counts']!r}, json={native_json['node_counts']!r}"
        )
    comparison = dict(native_json)
    comparison.update(
        {
            "lvs_match": True,
            "report_outcome": "pass",
            "json_outcome": "pass",
            "outcomes_agree": True,
            "structural_counts_agree": True,
            "evidence_agrees": True,
            "report": native_report,
        }
    )
    expected = {
        "working_directory": "/evidence",
        "working_directory_is_sandbox": False,
        "report_output": {
            "ownership": "native",
            "fresh_required": True,
            "parent_anchored": True,
            "path": operation["artifact"]["path"],
            "native_json_path": operation["json_artifact"]["path"],
            "capture": {
                "path": operation["artifact"]["path"],
                "origin": "netgen",
                "parent_anchored": True,
                "status": "valid",
                "bytes": report_record["bytes"],
                "sha256": report_record["sha256"],
                "validation": native_report["validation"],
            },
        },
        "json_output": {
            "ownership": "native-netgen-json",
            "fresh_required": True,
            "parent_anchored": True,
            "path": operation["json_artifact"]["path"],
            "capture": {
                "path": operation["json_artifact"]["path"],
                "origin": "netgen",
                "parent_anchored": True,
                "status": "valid",
                "bytes": json_record["bytes"],
                "sha256": json_record["sha256"],
                "validation": native_json["validation"],
            },
        },
        "setup_trust": NETGEN_SETUP_TRUST,
        "transitive_setup_inputs_enumerated": False,
        "ambient_environment_enumerated": False,
        "inputs_stable": True,
        "changed_inputs": [],
        "lvs_match": True,
        "comparison": comparison,
        "transcript": {
            "path": operation["transcript_artifact"]["path"],
            "origin": "openada",
            "capture_policy": "bounded complete-or-unknown process streams",
            "stdout_observed_bytes": native_transcript["stdout_observed_bytes"],
            "stderr_observed_bytes": native_transcript["stderr_observed_bytes"],
            "stdout_retained_bytes": native_transcript["stdout_retained_bytes"],
            "stderr_retained_bytes": native_transcript["stderr_retained_bytes"],
            "stdout_truncated": native_transcript["stdout_truncated"],
            "stderr_truncated": native_transcript["stderr_truncated"],
            "status": "valid",
            "bytes": transcript_record["bytes"],
            "sha256": transcript_record["sha256"],
            "assessment": native_transcript["assessment"],
            "stdout_tail": _bounded_contract_text(native_transcript["stdout"][-4_000:]),
            "stderr_tail": _bounded_contract_text(native_transcript["stderr"][-4_000:]),
            "limitation": NETGEN_TRANSCRIPT_LIMITATION,
        },
    }
    _expect_equal(result.get("data"), expected, "lvs.data")


def _verify_execution_identity(operation_name: str, operation: dict, result: dict) -> None:
    tool = result["tool"]
    identity = operation["tool_identity"]
    _expect_equal(tool.get("name"), operation["tool"], f"{operation_name}.tool.name")
    _expect_equal(tool.get("path"), identity["path"], f"{operation_name}.tool.path")
    _expect_equal(tool.get("version"), identity["version"], f"{operation_name}.tool.version")

    execution = result["execution"]
    _expect_equal(execution.get("cwd"), "/evidence", f"{operation_name}.execution.cwd")
    command = execution["command"]
    arguments = operation["arguments"]
    if operation_name in DRC_OPERATION_NAMES:
        expected_command = [
            identity["path"],
            "-b",
            "-r",
            arguments["rules"],
            "-rd",
            f"input={arguments['gds']}",
            "-rd",
            f"report={arguments['report']}",
            "-rd",
            f"topcell={arguments['top_cell']}",
        ]
        if command != expected_command:
            raise ConformanceError(
                f"{operation_name}.execution.command differs from the reviewed argv: "
                f"{command!r}"
            )
        return

    expected_command = [
        identity["path"],
        "-batch",
        "lvs",
        f"{arguments['layout_netlist']} {arguments['cell']}",
        f"{arguments['schematic_netlist']} {arguments['cell']}",
        arguments["setup"],
        arguments["report"],
        "-json",
    ]
    if command != expected_command:
        raise ConformanceError(f"lvs.execution.command differs from the reviewed argv: {command!r}")


def _verify_run_metadata(
    manifest: dict[str, Any],
    evidence: Path,
    manifest_sha256: str,
    validator: Draft202012Validator,
) -> None:
    metadata = _read_json(evidence / "run.json", label="run metadata")
    _validate_schema(metadata, validator, label="run metadata")
    _expect_equal(metadata.get("conformance_id"), manifest["id"], "run.conformance_id")
    _expect_equal(
        metadata.get("conformance_manifest_sha256"),
        manifest_sha256,
        "run.conformance_manifest_sha256",
    )
    _expect_equal(
        metadata.get("design_revision"), manifest["design"]["revision"], "run.design_revision"
    )
    _expect_equal(
        metadata["image"].get("reference"),
        manifest["runtime"]["image"]["reference"],
        "run.image.reference",
    )
    checkout = metadata["openada_checkout"]
    before = checkout["before"]
    after = checkout["after"]
    for label, state in (("before", before), ("after", after)):
        if state["commit"] is None:
            unavailable_values = (
                state["tracked_files_modified"],
                state["untracked_files_present"],
                state["working_tree_modified"],
                state["status_entry_count"],
                state["status_sha256"],
            )
            if any(value is not None for value in unavailable_values):
                raise ConformanceError(
                    f"run.openada_checkout.{label} mixes unavailable Git state "
                    "with populated fields"
                )
            continue
        available_values = (
            state["tracked_files_modified"],
            state["untracked_files_present"],
            state["working_tree_modified"],
            state["status_entry_count"],
            state["status_sha256"],
        )
        if any(value is None for value in available_values):
            raise ConformanceError(
                f"run.openada_checkout.{label} has a commit but incomplete Git state"
            )
        expected_modified = state["status_entry_count"] > 0
        _expect_equal(
            state["working_tree_modified"],
            expected_modified,
            f"run.openada_checkout.{label}.working_tree_modified",
        )
        _expect_equal(
            state["working_tree_modified"],
            state["tracked_files_modified"] or state["untracked_files_present"],
            f"run.openada_checkout.{label}.status_classes",
        )
        if state["status_entry_count"] == 0:
            _expect_equal(
                state["status_sha256"],
                hashlib.sha256(b"").hexdigest(),
                f"run.openada_checkout.{label}.status_sha256",
            )

    state_available = before["commit"] is not None and after["commit"] is not None
    expected_unchanged = before == after if state_available else None
    _expect_equal(
        checkout["state_unchanged"],
        expected_unchanged,
        "run.openada_checkout.state_unchanged",
    )
    expected_commit_exact = bool(
        expected_unchanged
        and before["working_tree_modified"] is False
        and after["working_tree_modified"] is False
    )
    _expect_equal(
        checkout["commit_exact"],
        expected_commit_exact,
        "run.openada_checkout.commit_exact",
    )
    source = metadata.get("source_attestation")
    if source is not None:
        subject = semantic_subject(
            REPOSITORY_ROOT,
            REPOSITORY_ROOT / "catalog/semantic-surfaces-v0alpha1.json",
        )
        _expect_equal(
            source["semantic_subject_sha256"],
            subject,
            "run.source_attestation.semantic_subject_sha256",
        )
        _expect_equal(
            source["state_unchanged"], True, "run.source_attestation.state_unchanged"
        )
        if source["receipt_class"] == "release":
            _expect_equal(source["clean_before"], True, "run.source_attestation.clean_before")
            _expect_equal(source["clean_after"], True, "run.source_attestation.clean_after")


def _verify_design_provenance(evidence: Path) -> None:
    provenance = _read_json(
        evidence / "design-provenance.json", label="design provenance"
    )
    validator = _load_validator(
        REPOSITORY_ROOT / "schemas/design-provenance-v0alpha1.schema.json",
        label="design provenance",
    )
    _validate_schema(provenance, validator, label="design provenance")
    chain = _read_json(
        HERE / "semantic-chain.json", label="semantic chain manifest"
    )
    design = chain["design"]
    for field in ("repository", "revision", "tree"):
        _expect_equal(provenance[field], design[field], f"design provenance {field}")
    _expect_equal(
        {key: provenance["license"][key] for key in ("path", "sha256")},
        {key: design["license"][key] for key in ("path", "sha256")},
        "design provenance license",
    )
    _expect_equal(
        [
            {key: item[key] for key in ("path", "sha256")}
            for item in provenance["inputs"]
        ],
        design["inputs"],
        "design provenance inputs",
    )


def verify_evidence(
    manifest: dict[str, Any], evidence: Path, *, manifest_sha256: str
) -> None:
    try:
        evidence_mode = evidence.lstat().st_mode
    except OSError as exc:
        raise ConformanceError(f"cannot stat evidence directory {evidence}: {exc}") from exc
    if not stat.S_ISDIR(evidence_mode):
        raise ConformanceError(f"evidence path is not a real, non-symlink directory: {evidence}")
    evidence = evidence.resolve()

    expected_files = {"run.json"}
    for operation in manifest["operations"].values():
        expected_files.add(operation["result_filename"])
        expected_files.add(operation["artifact"]["filename"])
        if operation.get("json_artifact"):
            expected_files.add(operation["json_artifact"]["filename"])
        if operation.get("transcript_artifact"):
            expected_files.add(operation["transcript_artifact"]["filename"])
    actual_files = {entry.name for entry in evidence.iterdir()}
    allowed_files = expected_files | {"design-provenance.json"}
    if actual_files not in (expected_files, allowed_files):
        raise ConformanceError(
            "evidence directory contents differ; "
            f"missing={sorted(expected_files - actual_files)}, "
            f"unexpected={sorted(actual_files - allowed_files)}"
        )

    result_validator = _load_validator(RESULT_SCHEMA_PATH, label="OpenADA result")
    run_validator = _load_validator(RUN_SCHEMA_PATH, label="conformance run")
    _verify_run_metadata(manifest, evidence, manifest_sha256, run_validator)
    if (evidence / "design-provenance.json").is_file():
        _verify_design_provenance(evidence)

    for operation_name in ("drc", "drc_fail", "lvs"):
        operation = manifest["operations"][operation_name]
        result = _read_json(
            evidence / operation["result_filename"],
            label=f"{operation_name} result",
        )
        _validate_schema(result, result_validator, label=f"{operation_name} result")
        _expect_equal(result.get("schema"), RESULT_SCHEMA, f"{operation_name}.schema")
        semantic_operation = (
            "drc" if operation_name in DRC_OPERATION_NAMES else operation_name
        )
        _expect_equal(
            result.get("operation"), semantic_operation, f"{operation_name}.operation"
        )
        _verify_execution_identity(operation_name, operation, result)

        expectation = operation["expect"]
        execution = result["execution"]
        _expect_equal(
            execution.get("status"), expectation["execution_status"], f"{operation_name}.execution.status"
        )
        _expect_equal(
            execution.get("exit_code"), expectation["exit_code"], f"{operation_name}.execution.exit_code"
        )
        engineering = result["engineering"]
        _expect_equal(
            engineering.get("status"),
            expectation["engineering_status"],
            f"{operation_name}.engineering.status",
        )
        if operation_name in DRC_OPERATION_NAMES:
            expected_summary = (
                "KLayout reported zero DRC violations."
                if expectation["total_violations"] == 0
                else (
                    f"KLayout reported {expectation['total_violations']} "
                    "DRC violation(s)."
                )
            )
            _expect_equal(
                engineering.get("summary"),
                expected_summary,
                f"{operation_name}.engineering.summary",
            )
            _verify_drc_diagnostics(operation_name, result)

        _verify_input_records(operation_name, operation, result)
        artifact_paths = _verify_artifacts(operation_name, operation, result, evidence)
        if operation_name in DRC_OPERATION_NAMES:
            native_report = _verify_native_drc(
                artifact_paths["klayout-lyrdb"],
                operation,
            )
            native_transcript = _verify_native_transcript(
                artifact_paths["klayout-transcript"]
            )
            _verify_drc_result_data(
                operation_name,
                operation,
                result,
                report_path=artifact_paths["klayout-lyrdb"],
                transcript_path=artifact_paths["klayout-transcript"],
                native_report=native_report,
                native_transcript=native_transcript,
            )
        else:
            native_report = _verify_native_lvs_report(
                artifact_paths["netgen-comparison"],
                expected_cell=operation["arguments"]["cell"],
            )
            native_json = _verify_native_lvs_json(
                artifact_paths["netgen-comparison-json"],
                expected_cell=operation["arguments"]["cell"],
            )
            native_transcript = _verify_native_lvs_transcript(
                artifact_paths["netgen-transcript"],
                setup_path=operation["arguments"]["setup"],
            )
            _verify_lvs_diagnostics(
                result,
                reviewed_stderr_count=native_transcript["assessment"][
                    "stderr_reviewed_warning_count"
                ],
            )
            _verify_lvs_result_data(
                operation,
                result,
                native_report=native_report,
                native_json=native_json,
                native_transcript=native_transcript,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the pinned IHP inverter conformance evidence.")
    parser.add_argument("evidence", type=Path, nargs="?", help="Evidence directory produced by run.py")
    parser.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Validate the static manifest without reading run evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        if not args.manifest_only:
            if args.evidence is None:
                raise ConformanceError("an evidence directory is required unless --manifest-only is used")
            evidence = args.evidence.expanduser()
            if not evidence.is_absolute():
                evidence = Path.cwd() / evidence
            verify_evidence(
                manifest,
                evidence,
                manifest_sha256=sha256_file(manifest_path),
            )
    except ConformanceError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    if args.manifest_only:
        print(f"Manifest verified: {manifest['id']}")
    else:
        print(f"Conformance verified: {manifest['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
