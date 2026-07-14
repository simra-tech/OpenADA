"""Bounded validation for native KLayout report databases.

KLayout DRC decks are executable Ruby and remain authoritative input.  This
module only establishes that a captured file has the native LYRDB shape we
expect from KLayout's ``report`` function and returns a compact, bounded view
of its engineering contents.
"""

from __future__ import annotations

from collections import defaultdict
import math
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
import xml.etree.ElementTree as ET


MAX_REPORT_BYTES = 256 * 1024 * 1024
MAX_XML_DEPTH = 128
MAX_CATEGORIES = 4_096
MAX_CELLS = 100_000
MAX_ITEMS = 1_000_000
MAX_CATEGORY_NAME_CHARS = 512
MAX_CATEGORY_PATH_CHARS = 4_096
MAX_CELL_NAME_CHARS = 1_024
MAX_CELL_VARIANT_CHARS = 1_024
MAX_CELL_IDENTITY_CHARS = 2_049
MAX_ITEM_TAGS = 256
MAX_ITEM_TAGS_CHARS = 4_096
MAX_ITEM_TAG_CHARS = 512
MAX_DESCRIPTION_CHARS = 1_000
MAX_GENERATOR_CHARS = 4_096
MAX_VIOLATION_EXAMPLES = 200
MAX_CATEGORY_COUNT_SUMMARIES = 200
MAX_GEOMETRIES_PER_VIOLATION = 8
MAX_GEOMETRIES_TOTAL = 256
MAX_COORDINATES_PER_GEOMETRY = 64
MAX_COORDINATES_TOTAL = 4_096
MAX_GEOMETRY_TEXT_CHARS = 1_000
MAX_GEOMETRY_SCAN_CHARS = 65_536
MAX_MARKER_COUNT = (1 << 63) - 1

NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
BOX_RE = re.compile(
    rf"box:\s*\(\s*({NUMBER_PATTERN})\s*,\s*({NUMBER_PATTERN})\s*;"
    rf"\s*({NUMBER_PATTERN})\s*,\s*({NUMBER_PATTERN})\s*\)"
)
PAIR_RE = re.compile(rf"({NUMBER_PATTERN})\s*,\s*({NUMBER_PATTERN})")
GENERATOR_RE = re.compile(r"drc:\s*script=(['\"])(.+)\1")


class _InvalidReport(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child.text
    return None


def _children_named(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _bounded(value: str | None, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    marker = " ... [truncated] ... "
    retained = limit - len(marker)
    head = retained // 2
    return text[:head] + marker + text[-(retained - head) :]


def _invalid(reason: str, message: str, *, size: int | None = None) -> dict:
    validation: dict[str, object] = {"valid": False, "reason": reason}
    if size is not None:
        validation["bytes"] = size
    return {
        "error": _bounded(message, 4_000),
        "validation": validation,
    }


def _parse_positive_count(value: str | None, *, field: str) -> int:
    text = (value or "").strip()
    if not text or not text.isascii() or not text.isdigit() or len(text) > 19:
        raise _InvalidReport(
            f"report.{field}_invalid",
            f"The KLayout report contains an invalid {field} value.",
        )
    parsed = int(text)
    if parsed <= 0 or parsed > MAX_MARKER_COUNT:
        raise _InvalidReport(
            f"report.{field}_invalid",
            f"The KLayout report contains an out-of-range {field} value.",
        )
    return parsed


def _parse_native_tokens(
    value: str | None,
    *,
    separator: str,
    maximum_parts: int,
    maximum_part_chars: int,
    maximum_text_chars: int,
) -> tuple[str, ...] | None:
    """Parse KLayout's quoted path/tag token syntax into exact components."""

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
        maximum_parts=MAX_XML_DEPTH,
        maximum_part_chars=MAX_CATEGORY_NAME_CHARS,
        maximum_text_chars=MAX_CATEGORY_PATH_CHARS,
    )


def _parse_item_tags(value: str | None) -> tuple[str, ...] | None:
    tags = _parse_native_tokens(
        value,
        separator=",",
        maximum_parts=MAX_ITEM_TAGS,
        maximum_part_chars=MAX_ITEM_TAG_CHARS,
        maximum_text_chars=MAX_ITEM_TAGS_CHARS,
    )
    if tags is None or len(set(tags)) != len(tags):
        return None
    return tags


def _category_label(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _finite_numbers(matches: list[re.Match[str]]) -> list[list[float]] | None:
    coordinates: list[list[float]] = []
    for match in matches:
        pair = [float(match.group(1)), float(match.group(2))]
        if not all(math.isfinite(value) for value in pair):
            return None
        coordinates.append(pair)
    return coordinates


def parse_geometry(
    text: str,
    *,
    maximum_coordinates: int = MAX_COORDINATES_PER_GEOMETRY,
) -> tuple[dict | None, int]:
    """Parse one bounded geometry example and return its coordinate-pair cost."""

    value = text.strip()
    if not value:
        return None, 0

    box = BOX_RE.fullmatch(value) if len(value) <= MAX_GEOMETRY_SCAN_CHARS else None
    if box:
        coordinates = [float(part) for part in box.groups()]
        if all(math.isfinite(coordinate) for coordinate in coordinates):
            return {"type": "box", "coordinates": coordinates}, 2
        return {
            "type": "unknown",
            "raw": _bounded(value, MAX_GEOMETRY_TEXT_CHARS),
        }, 0

    scanned = value[:MAX_GEOMETRY_SCAN_CHARS]
    retained_matches: list[re.Match[str]] = []
    saw_additional_match = False
    for match in PAIR_RE.finditer(scanned):
        if len(retained_matches) >= maximum_coordinates:
            saw_additional_match = True
            break
        retained_matches.append(match)
    coordinates = _finite_numbers(retained_matches)
    if coordinates is None:
        return {
            "type": "unknown",
            "raw": _bounded(value, MAX_GEOMETRY_TEXT_CHARS),
        }, 0
    truncated = saw_additional_match or len(value) > len(scanned)
    if value.startswith("polygon:") and coordinates:
        return {
            "type": "polygon",
            "coordinates": coordinates,
            "coordinates_truncated": truncated,
        }, len(coordinates)
    if value.startswith("edge-pair:") and coordinates:
        return {
            "type": "edge-pair",
            "coordinates": coordinates,
            "coordinates_truncated": truncated,
        }, len(coordinates)
    return {
        "type": "unknown",
        "raw": _bounded(value, MAX_GEOMETRY_TEXT_CHARS),
    }, 0


def _generator_script(generator: str) -> Path | None:
    match = GENERATOR_RE.fullmatch(generator)
    if match is None:
        return None
    quote = match.group(1)
    encoded = match.group(2)
    decoded: list[str] = []
    offset = 0
    while offset < len(encoded):
        character = encoded[offset]
        offset += 1
        if character == quote:
            # A delimiter inside the quoted token is only native when escaped.
            return None
        if character != "\\":
            decoded.append(character)
            continue
        if offset >= len(encoded):
            return None
        escaped = encoded[offset]
        offset += 1
        escape_values = {
            "\\": "\\",
            "'": "'",
            '"': '"',
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escaped not in escape_values:
            return None
        decoded.append(escape_values[escaped])
    try:
        return Path("".join(decoded)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _category_declaration_ancestry(tags: list[str]) -> bool:
    """Return whether tags identify a native recursive category declaration."""

    if len(tags) < 3 or tags[:2] != ["report-database", "categories"]:
        return False
    tail = tags[2:]
    return bool(
        len(tail) % 2 == 1
        and all(
            tag == ("category" if index % 2 == 0 else "categories")
            for index, tag in enumerate(tail)
        )
    )


def _category_container_ancestry(tags: list[str]) -> bool:
    """Return whether tags identify the root or a native nested categories node."""

    if tags == ["report-database", "categories"]:
        return True
    if len(tags) < 4 or tags[:2] != ["report-database", "categories"]:
        return False
    tail = tags[2:]
    return bool(
        len(tail) % 2 == 0
        and all(
            tag == ("category" if index % 2 == 0 else "categories")
            for index, tag in enumerate(tail)
        )
    )


def parse_lyrdb_stream(
    handle: BinaryIO,
    *,
    expected_deck: Path | None = None,
    expected_top_cell: str | None = None,
    size: int | None = None,
) -> dict:
    """Validate and summarize a stable, already-open LYRDB stream."""

    categories: dict[tuple[str, ...], str] = {}
    declared_cells: set[str] = set()
    declared_base_cells: set[str] = set()
    category_counts: dict[tuple[str, ...], int] = defaultdict(int)
    violations: list[dict] = []
    total_items = 0
    total_markers = 0
    waived_markers = 0
    total_geometry_values = 0
    retained_geometries = 0
    retained_coordinates = 0
    generator = ""
    top_cell = ""
    description = ""
    original_file = ""
    section_counts: dict[str, int] = defaultdict(int)
    tags: list[str] = []
    in_item = False
    item_category: tuple[str, ...] = ()
    item_cell = ""
    item_category_fields = 0
    item_cell_fields = 0
    item_multiplicity_fields = 0
    item_tags_fields = 0
    item_tags: tuple[str, ...] = ()
    item_multiplicity = 1
    item_waived = False
    item_geometries: list[dict] = []
    item_geometry_values = 0
    elements: list[ET.Element] = []

    try:
        for event, element in ET.iterparse(handle, events=("start", "end")):
            tag = _local_name(element.tag)
            if event == "start":
                if element.tag != tag:
                    raise _InvalidReport(
                        "report.namespace_unsupported",
                        "Native KLayout LYRDB elements must not use an XML namespace.",
                    )
                tags.append(tag)
                elements.append(element)
                if len(tags) > MAX_XML_DEPTH:
                    raise _InvalidReport(
                        "report.xml_too_deep",
                        f"The KLayout report exceeds the {MAX_XML_DEPTH}-level XML depth limit.",
                    )
                if len(tags) == 1 and tag != "report-database":
                    raise _InvalidReport(
                        "report.root_invalid",
                        f"Unexpected KLayout report root: {tag}",
                    )
                if len(tags) == 2 and tags[0] == "report-database":
                    section_counts[tag] += 1
                if tag in {"generator", "top-cell"} and tags != [
                    "report-database",
                    tag,
                ]:
                    raise _InvalidReport(
                        "report.root_structure_invalid",
                        f"The KLayout report {tag} field must be a direct root child.",
                    )
                if tag == "categories" and not _category_container_ancestry(tags):
                    raise _InvalidReport(
                        "report.category_structure_invalid",
                        "KLayout category containers must follow the native recursive hierarchy.",
                    )
                if tag == "cells" and tags != ["report-database", "cells"]:
                    raise _InvalidReport(
                        "report.cell_structure_invalid",
                        "The KLayout cells section must be a direct root child.",
                    )
                if tag == "items" and tags != ["report-database", "items"]:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "The KLayout items section must be a direct root child.",
                    )
                if tag == "item":
                    if in_item or tags != ["report-database", "items", "item"]:
                        raise _InvalidReport(
                            "report.item_structure_invalid",
                            "KLayout report items must be direct children of the items section.",
                        )
                    in_item = True
                    item_category = ()
                    item_cell = ""
                    item_category_fields = 0
                    item_cell_fields = 0
                    item_multiplicity_fields = 0
                    item_tags_fields = 0
                    item_tags = ()
                    item_multiplicity = 1
                    item_waived = False
                    item_geometries = []
                    item_geometry_values = 0
                continue

            parent = tags[-2] if len(tags) > 1 else None
            parent_element = elements[-2] if len(elements) > 1 else None
            if parent == "report-database":
                if tag == "generator":
                    generator = (element.text or "").strip()
                    if len(generator) > MAX_GENERATOR_CHARS:
                        raise _InvalidReport(
                            "report.generator_too_long",
                            "The KLayout report generator field is overlong.",
                        )
                elif tag == "top-cell":
                    top_cell = (element.text or "").strip()
                    if len(top_cell) > MAX_CELL_NAME_CHARS:
                        raise _InvalidReport(
                            "report.top_cell_too_long",
                            "The KLayout report top-cell field is overlong.",
                        )
                elif tag == "description":
                    description = _bounded(element.text, MAX_DESCRIPTION_CHARS)
                elif tag == "original-file":
                    original_file = _bounded(element.text, MAX_GENERATOR_CHARS)

            if tag == "category":
                if in_item:
                    if parent != "item" or len(tags) != 4 or tags[1:3] != ["items", "item"]:
                        raise _InvalidReport(
                            "report.item_structure_invalid",
                            "A KLayout item category must be a direct child of its item.",
                        )
                    item_category_fields += 1
                    if item_category_fields != 1:
                        raise _InvalidReport(
                            "report.item_structure_invalid",
                            "A KLayout report item declares more than one category.",
                        )
                    parsed_category = _parse_category_path(element.text)
                    if not parsed_category:
                        raise _InvalidReport(
                            "report.item_category_invalid",
                            "A KLayout report item has an invalid native category path.",
                        )
                    item_category = parsed_category
                else:
                    if not _category_declaration_ancestry(tags):
                        raise _InvalidReport(
                            "report.category_structure_invalid",
                            "KLayout category declarations must follow the native recursive hierarchy.",
                        )
                    name_fields = _children_named(element, "name")
                    if len(name_fields) != 1:
                        raise _InvalidReport(
                            "report.category_invalid",
                            "A KLayout category must declare exactly one name field.",
                        )
                    name_text = name_fields[0].text
                    name = (name_text or "").strip()
                    if not name or len(name) > MAX_CATEGORY_NAME_CHARS:
                        raise _InvalidReport(
                            "report.category_invalid",
                            "The KLayout report contains a missing or overlong category name.",
                        )
                    ancestor_names: list[str] = []
                    for ancestor_tag, ancestor in zip(tags[2:-1], elements[2:-1]):
                        if ancestor_tag != "category":
                            continue
                        ancestor_name = (_child_text(ancestor, "name") or "").strip()
                        if (
                            not ancestor_name
                            or len(ancestor_name) > MAX_CATEGORY_NAME_CHARS
                        ):
                            raise _InvalidReport(
                                "report.category_invalid",
                                "A parent KLayout category has an invalid name.",
                            )
                        ancestor_names.append(ancestor_name)
                    path = tuple([*ancestor_names, name])
                    if len(_category_label(path)) > MAX_CATEGORY_PATH_CHARS:
                        raise _InvalidReport(
                            "report.category_invalid",
                            "A KLayout category path exceeds the normalized path limit.",
                        )
                    if path in categories:
                        raise _InvalidReport(
                            "report.category_duplicate",
                            (
                                "The KLayout report declares category path "
                                f"{_category_label(path)!r} more than once."
                            ),
                        )
                    if len(categories) >= MAX_CATEGORIES:
                        raise _InvalidReport(
                            "report.too_many_categories",
                            f"The KLayout report exceeds the {MAX_CATEGORIES}-category limit.",
                        )
                    categories[path] = _bounded(
                        _child_text(element, "description"),
                        MAX_DESCRIPTION_CHARS,
                    )
            elif tag == "cell":
                if in_item:
                    if parent != "item" or len(tags) != 4 or tags[1:3] != ["items", "item"]:
                        raise _InvalidReport(
                            "report.item_structure_invalid",
                            "A KLayout item cell must be a direct child of its item.",
                        )
                    item_cell_fields += 1
                    if item_cell_fields != 1:
                        raise _InvalidReport(
                            "report.item_structure_invalid",
                            "A KLayout report item declares more than one cell.",
                        )
                    item_cell = (element.text or "").strip()
                    if (
                        len(item_cell) > MAX_CELL_IDENTITY_CHARS
                        or any(
                            ord(character) < 32 or ord(character) == 127
                            for character in item_cell
                        )
                    ):
                        raise _InvalidReport(
                            "report.item_cell_invalid",
                            "A KLayout report item has an invalid qualified cell identity.",
                        )
                else:
                    if parent != "cells" or len(tags) != 3 or tags[1] != "cells":
                        raise _InvalidReport(
                            "report.cell_structure_invalid",
                            "KLayout cell declarations must be direct children of the cells section.",
                        )
                    name_fields = _children_named(element, "name")
                    variant_fields = _children_named(element, "variant")
                    if len(name_fields) != 1 or len(variant_fields) > 1:
                        raise _InvalidReport(
                            "report.cell_invalid",
                            "A KLayout cell must declare one name and at most one variant field.",
                        )
                    name_text = name_fields[0].text
                    name = (name_text or "").strip()
                    variant = (
                        (variant_fields[0].text or "").strip()
                        if variant_fields
                        else ""
                    )
                    if (
                        (not name and bool(variant))
                        or len(name) > MAX_CELL_NAME_CHARS
                        or len(variant) > MAX_CELL_VARIANT_CHARS
                        or any(
                            ord(character) < 32 or ord(character) == 127
                            for character in name + variant
                        )
                    ):
                        raise _InvalidReport(
                            "report.cell_invalid",
                            "The KLayout report contains an invalid cell name or variant.",
                        )
                    identity = f"{name}:{variant}" if variant else name
                    if len(identity) > MAX_CELL_IDENTITY_CHARS:
                        raise _InvalidReport(
                            "report.cell_invalid",
                            "The KLayout report contains an overlong qualified cell identity.",
                        )
                    if identity in declared_cells:
                        raise _InvalidReport(
                            "report.cell_duplicate",
                            f"The KLayout report declares cell {identity!r} more than once.",
                        )
                    if len(declared_cells) >= MAX_CELLS:
                        raise _InvalidReport(
                            "report.too_many_cells",
                            f"The KLayout report exceeds the {MAX_CELLS}-cell limit.",
                        )
                    declared_cells.add(identity)
                    if name:
                        declared_base_cells.add(name)
            elif tag == "tags" and in_item:
                if tags != ["report-database", "items", "item", "tags"]:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "KLayout item tags must be a direct child of their item.",
                    )
                item_tags_fields += 1
                if item_tags_fields != 1:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "A KLayout report item declares more than one tags field.",
                    )
                parsed_tags = _parse_item_tags(element.text)
                if parsed_tags is None:
                    raise _InvalidReport(
                        "report.item_tags_invalid",
                        "A KLayout report item has invalid, duplicate, or overlong tags.",
                    )
                item_tags = parsed_tags
                item_waived = "waived" in item_tags
            elif tag == "multiplicity" and in_item:
                if tags != ["report-database", "items", "item", "multiplicity"]:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "A KLayout item multiplicity must be a direct child of its item.",
                    )
                item_multiplicity_fields += 1
                if item_multiplicity_fields != 1:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "A KLayout report item declares more than one multiplicity.",
                    )
                item_multiplicity = _parse_positive_count(
                    element.text,
                    field="multiplicity",
                )
            elif tag == "value" and in_item:
                if tags != [
                    "report-database",
                    "items",
                    "item",
                    "values",
                    "value",
                ]:
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        "KLayout item values must be children of the native values field.",
                    )
                item_geometry_values += 1
                total_geometry_values += 1
                if (
                    len(item_geometries) < MAX_GEOMETRIES_PER_VIOLATION
                    and retained_geometries < MAX_GEOMETRIES_TOTAL
                    and retained_coordinates < MAX_COORDINATES_TOTAL
                ):
                    remaining = min(
                        MAX_COORDINATES_PER_GEOMETRY,
                        MAX_COORDINATES_TOTAL - retained_coordinates,
                    )
                    geometry, coordinate_cost = parse_geometry(
                        element.text or "",
                        maximum_coordinates=remaining,
                    )
                    if geometry is not None:
                        item_geometries.append(geometry)
                        retained_geometries += 1
                        retained_coordinates += coordinate_cost
            elif tag == "item":
                if (
                    not item_category
                    or item_cell_fields != 1
                    or item_tags_fields != 1
                    or item_multiplicity_fields != 1
                ):
                    raise _InvalidReport(
                        "report.item_structure_invalid",
                        (
                            "A KLayout report item must contain exactly one tags, category, "
                            "cell, and positive multiplicity field."
                        ),
                    )
                if item_category not in categories:
                    raise _InvalidReport(
                        "report.item_category_unknown",
                        "A KLayout report item references an undeclared category.",
                    )
                if item_cell not in declared_cells:
                    raise _InvalidReport(
                        "report.item_cell_unknown",
                        "A KLayout report item references an undeclared cell.",
                    )
                if total_markers > MAX_MARKER_COUNT - item_multiplicity:
                    raise _InvalidReport(
                        "report.marker_count_invalid",
                        "The KLayout report marker count exceeds the supported range.",
                    )
                if total_items >= MAX_ITEMS:
                    raise _InvalidReport(
                        "report.too_many_items",
                        f"The KLayout report exceeds the {MAX_ITEMS}-item limit.",
                    )
                total_items += 1
                total_markers += item_multiplicity
                category_counts[item_category] += item_multiplicity
                if item_waived:
                    waived_markers += item_multiplicity
                if len(violations) < MAX_VIOLATION_EXAMPLES:
                    violations.append(
                        {
                            "category": _bounded(
                                _category_label(item_category),
                                MAX_CATEGORY_PATH_CHARS,
                            ),
                            "category_path": list(item_category),
                            "description": categories.get(item_category, ""),
                            "cell": _bounded(item_cell, MAX_CELL_NAME_CHARS),
                            "multiplicity": item_multiplicity,
                            "waived": item_waived,
                            "tags": list(item_tags),
                            "geometries": item_geometries,
                            "geometries_truncated": item_geometry_values > len(item_geometries),
                        }
                    )
                in_item = False

            if tag in {"category", "cell", "value", "item", "tags"} or (
                parent == "report-database"
                and tag in {"description", "original-file", "generator", "top-cell"}
            ):
                element.clear()
            if tag in {"category", "cell", "value", "item", "tags"} and parent_element is not None:
                try:
                    parent_element.remove(element)
                except ValueError:
                    pass
            tags.pop()
            elements.pop()

        if tags or elements:
            raise _InvalidReport(
                "report.xml_invalid",
                "The KLayout report XML stack did not close cleanly.",
            )
    except _InvalidReport as exc:
        return _invalid(exc.reason, exc.message, size=size)
    except (ET.ParseError, OSError, ValueError, OverflowError) as exc:
        return _invalid(
            "report.xml_invalid",
            f"Unable to parse KLayout report: {exc}",
            size=size,
        )

    if section_counts.get("generator") != 1:
        return _invalid(
            "report.generator_missing",
            "The KLayout report must contain exactly one generator field.",
            size=size,
        )
    if (
        section_counts.get("top-cell") != 1
        or not top_cell
        or any(ord(character) < 32 or ord(character) == 127 for character in top_cell)
    ):
        return _invalid(
            "report.top_cell_missing",
            "The KLayout report must contain one nonempty top-cell field.",
            size=size,
        )
    for section in ("categories", "cells", "items"):
        if section_counts.get(section) != 1:
            return _invalid(
                f"report.{section}_missing",
                f"The KLayout report must contain exactly one direct {section} section.",
                size=size,
            )
    if not categories:
        return _invalid(
            "report.categories_empty",
            "The KLayout report declares no DRC categories, so no executed check is proven.",
            size=size,
        )
    if not declared_cells or top_cell not in declared_base_cells:
        return _invalid(
            "report.cells_invalid",
            "The KLayout report top cell is not present in its cells section.",
            size=size,
        )
    generator_script = _generator_script(generator)
    if generator_script is None:
        return _invalid(
            "report.generator_invalid",
            "The KLayout report generator does not identify a native DRC script.",
            size=size,
        )
    if expected_deck is not None and generator_script != expected_deck.resolve():
        return _invalid(
            "report.generator_mismatch",
            "The KLayout report generator does not match the executed rule deck.",
            size=size,
        )
    if expected_top_cell is not None and top_cell != expected_top_cell:
        return _invalid(
            "report.top_cell_mismatch",
            (
                "The KLayout report top cell does not match the caller's explicit "
                f"selection: expected {expected_top_cell!r}, got {top_cell!r}."
            ),
            size=size,
        )

    count_summaries = [
        {
            "category": _category_label(category),
            "category_path": list(category),
            "violations": count,
        }
        for category, count in sorted(
            category_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:MAX_CATEGORY_COUNT_SUMMARIES]
    ]
    return {
        "validation": {
            "valid": True,
            "reason": "lyrdb.valid",
            **({"bytes": size} if size is not None else {}),
        },
        "description": description,
        "original_file": original_file,
        "generator": generator,
        "generator_script": str(generator_script),
        "top_cell": top_cell,
        "category_count": len(categories),
        "cell_count": len(declared_cells),
        "item_count": total_items,
        "total_violations": total_markers,
        "waived_violations": waived_markers,
        "category_counts": count_summaries,
        "category_counts_truncated": len(category_counts) > len(count_summaries),
        "violations": violations,
        "violations_truncated": total_items > len(violations),
        "normalization": {
            "geometry_values": total_geometry_values,
            "retained_geometries": retained_geometries,
            "retained_coordinate_pairs": retained_coordinates,
            "global_geometry_limit_reached": (
                total_geometry_values > retained_geometries
                and retained_geometries >= MAX_GEOMETRIES_TOTAL
            ),
        },
    }


def parse_lyrdb(
    path: str | Path,
    *,
    expected_deck: Path | None = None,
    expected_top_cell: str | None = None,
) -> dict:
    """Safely open and validate one standalone LYRDB path."""

    try:
        report = Path(path)
        metadata = report.lstat()
    except (OSError, TypeError, ValueError) as exc:
        return _invalid("file.unreadable", f"Cannot stat KLayout report: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        return _invalid(
            "file.not_regular",
            "The KLayout report is not a regular, non-symlink file.",
            size=metadata.st_size,
        )
    if metadata.st_nlink != 1:
        return _invalid(
            "file.hardlinked",
            "The KLayout report must have exactly one hard link.",
            size=metadata.st_size,
        )
    if metadata.st_size <= 0:
        return _invalid("file.empty", "The KLayout report is empty.", size=metadata.st_size)
    if metadata.st_size > MAX_REPORT_BYTES:
        return _invalid(
            "file.too_large",
            f"The KLayout report exceeds the {MAX_REPORT_BYTES}-byte validation limit.",
            size=metadata.st_size,
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(report, flags)
    except (OSError, TypeError, ValueError) as exc:
        return _invalid("file.unreadable", f"Cannot open KLayout report: {exc}")
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size)
        ):
            return _invalid(
                "file.unstable",
                "The KLayout report identity changed before validation.",
                size=opened.st_size,
            )
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            parsed = parse_lyrdb_stream(
                handle,
                expected_deck=expected_deck,
                expected_top_cell=expected_top_cell,
                size=opened.st_size,
            )
        finished = os.fstat(descriptor)
        current = report.lstat()
        signature = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if signature(opened) != signature(finished) or signature(opened) != signature(current):
            return _invalid(
                "file.unstable",
                "The KLayout report changed during validation.",
                size=finished.st_size,
            )
        return parsed
    except (OSError, TypeError, ValueError) as exc:
        return _invalid("file.unreadable", f"Cannot validate KLayout report: {exc}")
    finally:
        os.close(descriptor)
