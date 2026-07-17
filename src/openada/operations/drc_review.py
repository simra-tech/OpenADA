"""Generate bounded, visual KLayout review evidence from an existing DRC report."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any

from ..contract import (
    FileRecordError,
    diagnostic,
    file_record,
    result,
    static_execution,
    tool_record,
)
from ..discovery import DiscoveryManager
from ..engines.klayout_outputs import MAX_REPORT_BYTES, parse_lyrdb
from ..process import run_process


MAX_GDS_BYTES = 2 * 1024 * 1024 * 1024
MAX_LAYER_PROPERTIES_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_VIEWS = 12
MAX_IMAGE_DIMENSION = 4_096
MIN_IMAGE_DIMENSION = 256
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RULE_FAMILIES = (
    ("off-grid", ("offgrid", "off-grid", "grid")),
    ("enclosure", ("enclosure", "enclose", "extension")),
    ("spacing", ("spacing", "space", "separation")),
    ("minimum-width", ("width", "minimum width", "minwidth")),
    ("minimum-area", ("area", "minimum area", "minarea")),
    ("overlap", ("overlap", "overlapping")),
    ("density", ("density",)),
    ("antenna", ("antenna",)),
)
_LENGTH_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<value>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>nm|um|µm)\b",
    re.IGNORECASE,
)


def _invalid(message: str, *, code: str = "drc_review.request.invalid") -> dict:
    return result(
        "drc.review",
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The DRC visual-review request is invalid.",
        diagnostics=[diagnostic("error", code, message)],
    )


def _real_empty_output_directory(path: str | Path) -> Path:
    output = Path(path).expanduser().absolute()
    ancestor = output
    missing: list[Path] = []
    while not ancestor.exists():
        missing.append(ancestor)
        if ancestor.parent == ancestor:
            raise ValueError("output directory has no existing ancestor")
        ancestor = ancestor.parent
    if ancestor.is_symlink() or not ancestor.is_dir():
        raise ValueError("output directory must descend from a real directory")
    current = ancestor
    for component in reversed(missing):
        current = current / component.name
        current.mkdir()
    opened = os.stat(output, follow_symlinks=False)
    if not stat.S_ISDIR(opened.st_mode):
        raise ValueError("output directory is not a real directory")
    if any(output.iterdir()):
        raise ValueError("output directory must be empty for fresh review evidence")
    return output


def _geometry_box(geometry: dict[str, Any]) -> list[float] | None:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "box" and isinstance(coordinates, list) and len(coordinates) == 4:
        values = [float(value) for value in coordinates]
        return [min(values[0], values[2]), min(values[1], values[3]), max(values[0], values[2]), max(values[1], values[3])]
    if kind in {"polygon", "edge-pair"} and isinstance(coordinates, list):
        pairs = [pair for pair in coordinates if isinstance(pair, list) and len(pair) == 2]
        if pairs:
            xs = [float(pair[0]) for pair in pairs]
            ys = [float(pair[1]) for pair in pairs]
            return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _rule_family(category: str, description: str) -> tuple[str, str]:
    text = f"{category} {description}".lower().replace("_", " ")
    for family, tokens in _RULE_FAMILIES:
        if any(token in text for token in tokens):
            return family, "rule-text-match"
    return "unclassified", "none"


def _length_constraints(description: str) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    for match in _LENGTH_RE.finditer(description):
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        value_um = value / 1000.0 if unit == "nm" else value
        constraints.append(
            {
                "value": value,
                "unit": match.group("unit"),
                "value_um": value_um,
                "source": "native-rule-description",
            }
        )
    return constraints


def _grid_observation(box: list[float], constraints: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not constraints or constraints[0]["value_um"] <= 0:
        return None
    grid = constraints[0]["value_um"]
    coordinates = {"left": box[0], "bottom": box[1], "right": box[2], "top": box[3]}
    offsets = {
        name: min(value % grid, grid - (value % grid))
        for name, value in coordinates.items()
    }
    return {
        "declared_grid_um": grid,
        "coordinate_offsets_um": offsets,
        "maximum_offset_um": max(offsets.values()),
        "interpretation": "distance from each retained marker-box coordinate to the nearest declared grid line",
    }


def _diagnose_marker(marker: dict[str, Any]) -> dict[str, Any]:
    box = marker["box_um"]
    description = marker.get("description", "")
    family, basis = _rule_family(marker["category"], description)
    constraints = _length_constraints(description)
    observations: dict[str, Any] = {
        "box_um": box,
        "center_um": [(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0],
        "width_um": box[2] - box[0],
        "height_um": box[3] - box[1],
        "geometry_types": marker.get("geometry_types", []),
        "multiplicity": marker["multiplicity"],
    }
    if family == "off-grid":
        grid = _grid_observation(box, constraints)
        if grid is not None:
            observations["grid"] = grid
    return {
        "rule_family": family,
        "classification_basis": basis,
        "rule_description": description,
        "declared_length_constraints": constraints,
        "observations": observations,
        "limitations": [
            "Marker bounds are native report geometry, not a reconstructed rule measurement.",
            "Rule-family classification is lexical and does not identify the layout edit that will satisfy the deck.",
        ],
    }


def _marker_examples(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, tuple[float, ...], bool], dict[str, Any]] = {}
    for violation in parsed.get("violations", []):
        if not isinstance(violation, dict):
            continue
        source_cell = str(violation.get("cell", ""))
        base_cell = source_cell.split(":", 1)[0]
        for geometry in violation.get("geometries", []):
            if not isinstance(geometry, dict):
                continue
            box = _geometry_box(geometry)
            if box is None:
                continue
            category = str(violation.get("category", ""))
            waived = bool(violation.get("waived", False))
            key = (category, base_cell, tuple(box), waived)
            marker = unique.setdefault(
                key,
                {
                    "category": category,
                    "cell": base_cell,
                    "source_cells": [],
                    "multiplicity": 0,
                    "waived": waived,
                    "box_um": box,
                    "description": str(violation.get("description", "")),
                    "geometry_types": [],
                },
            )
            geometry_type = str(geometry.get("type", ""))
            if geometry_type and geometry_type not in marker["geometry_types"]:
                marker["geometry_types"].append(geometry_type)
            if source_cell not in marker["source_cells"]:
                marker["source_cells"].append(source_cell)
            marker["multiplicity"] = max(
                marker["multiplicity"], int(violation.get("multiplicity", 1))
            )
    markers = list(unique.values())
    for index, marker in enumerate(markers):
        marker["id"] = f"m{index}"
        marker["source_cells"].sort()
        marker["geometry_types"].sort()
        marker["diagnosis"] = _diagnose_marker(marker)
    return markers


def _diagnosis_payload(markers: list[dict[str, Any]]) -> dict[str, Any]:
    family_counts: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for marker in markers:
        family = marker["diagnosis"]["rule_family"]
        family_counts[family] = family_counts.get(family, 0) + 1
        records.append(
            {
                "marker_id": marker["id"],
                "category": marker["category"],
                "cell": marker["cell"],
                "source_cells": marker["source_cells"],
                "waived": marker["waived"],
                "diagnosis": marker["diagnosis"],
            }
        )
    return {
        "schema": "openada.drc-review-diagnosis/v1alpha1",
        "marker_count": len(records),
        "rule_family_counts": dict(sorted(family_counts.items())),
        "markers": records,
        "interpretation": "Observed marker geometry and conservative lexical rule classification; not an automated repair prescription.",
    }


def _renderer_source(config_path: Path) -> str:
    # KLayout selects Python from the .py suffix passed through -r. Keep this
    # renderer standalone because KLayout's embedded Python does not import the
    # caller's OpenADA installation reliably across packaged runtimes.
    return f'''from __future__ import annotations
import json
from pathlib import Path
import klayout.db as db
import klayout.lay as lay

CONFIG_PATH = {str(config_path)!r}

with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
    config = json.load(handle)

view = lay.LayoutView()
cv_index = view.load_layout(config["gds"])
layout = view.cellview(cv_index).layout()
top = layout.cell(config["top_cell"])
if top is None:
    raise RuntimeError("configured top cell is absent from the GDS")
if config.get("layer_properties"):
    view.load_layer_props(config["layer_properties"], cv_index, True)
else:
    view.add_missing_layers()

def transformed_box(box, transform):
    points = [
        transform * db.DPoint(box.left, box.bottom),
        transform * db.DPoint(box.left, box.top),
        transform * db.DPoint(box.right, box.bottom),
        transform * db.DPoint(box.right, box.top),
    ]
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return db.DBox(min(xs), min(ys), max(xs), max(ys))

def nonzero_box(values):
    box = db.DBox(*values)
    minimum = config["minimum_marker_um"]
    if box.width() < minimum:
        center = box.center().x
        box.left = center - minimum / 2.0
        box.right = center + minimum / 2.0
    if box.height() < minimum:
        center = box.center().y
        box.bottom = center - minimum / 2.0
        box.top = center + minimum / 2.0
    return box

markers_by_cell = {{}}
for marker in config["markers"]:
    markers_by_cell.setdefault(marker["cell"].split(":", 1)[0], []).append(marker)

expanded = []
for marker in markers_by_cell.get(config["top_cell"], []):
    expanded.append((marker, nonzero_box(marker["box_um"]), "top:" + marker["id"]))
iterator = top.begin_instances_rec()
occurrence_index = 0
while not iterator.at_end():
    cell = iterator.inst_cell()
    local = markers_by_cell.get(cell.name, [])
    if local:
        occurrence = "instance:" + str(occurrence_index)
        occurrence_index += 1
        transform = iterator.dtrans() * iterator.inst_dtrans()
        for marker in local:
            expanded.append((marker, transformed_box(nonzero_box(marker["box_um"]), transform), occurrence))
    iterator.next()

marker_layer = layout.layer(config["marker_layer"], config["marker_datatype"])
scale = 1.0 / layout.dbu
for marker, box, occurrence in expanded:
    top.shapes(marker_layer).insert(db.Box(
        round(box.left * scale), round(box.bottom * scale),
        round(box.right * scale), round(box.top * scale)
    ))
view.add_missing_layers()
for properties in view.each_layer():
    if (properties.source_layer, properties.source_datatype) == (
        config["marker_layer"], config["marker_datatype"]
    ):
        properties.name = "OpenADA DRC review markers"
        properties.frame_color = 0xFF0000
        properties.fill_color = 0xFF0000
        properties.dither_pattern = 1
        properties.width = 3
        properties.visible = True
    elif properties.valid:
        properties.frame_color = 0x526273
        properties.fill_color = 0x2F3C49
view.set_config("background-color", "#000000")
view.set_config("grid-visible", "true")
view.cellview(cv_index).cell = top
view.max_hier()

output = Path(config["output_dir"])
views = []
overview = output / "00-overview.png"
view.zoom_box(top.dbbox())
view.save_image_with_options(str(overview), config["width"], config["height"], 0, 2, 0)
views.append({{"kind": "overview", "path": str(overview), "expanded_markers": len(expanded)}})

groups = {{}}
for marker, box, occurrence in expanded:
    key = (marker["category"], marker["cell"], occurrence)
    groups.setdefault(key, []).append(box)
ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
for index, ((category, cell, occurrence), boxes) in enumerate(ranked[:config["max_cluster_views"]], start=1):
    left = min(box.left for box in boxes)
    bottom = min(box.bottom for box in boxes)
    right = max(box.right for box in boxes)
    top_edge = max(box.top for box in boxes)
    margin = max(config["minimum_cluster_margin_um"], max(right - left, top_edge - bottom) * 0.10)
    target = db.DBox(left - margin, bottom - margin, right + margin, top_edge + margin)
    image = output / f"{{index:02d}}-cluster.png"
    view.zoom_box(target)
    view.save_image_with_options(str(image), config["width"], config["height"], 0, 2, 0)
    views.append({{
        "kind": "cluster", "path": str(image), "category": category,
        "cell": cell, "occurrence": occurrence, "expanded_markers": len(boxes),
        "bounds_um": [target.left, target.bottom, target.right, target.top],
    }})

summary = {{
    "schema": "openada.drc-review-render/v1alpha1",
    "top_cell": config["top_cell"],
    "retained_marker_examples": len(config["markers"]),
    "expanded_physical_markers": len(expanded),
    "unplaced_marker_examples": len(config["markers"]) - len({{marker["id"] for marker, _, _ in expanded}}),
    "views": views,
}}
with open(output / "render-summary.json", "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
    handle.write("\\n")
'''


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"renderer output is not a native PNG: {path.name}")
    return struct.unpack(">II", header[16:24])


def _read_render_summary(path: Path, output: Path, width: int, height: int) -> tuple[dict, list[dict]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != "openada.drc-review-render/v1alpha1":
        raise ValueError("renderer summary has an unknown shape")
    views = document.get("views")
    if not isinstance(views, list) or not views or len(views) > MAX_VIEWS:
        raise ValueError("renderer summary contains an invalid view list")
    artifacts: list[dict] = []
    normalized_views: list[dict] = []
    for view in views:
        if not isinstance(view, dict) or view.get("kind") not in {"overview", "cluster"}:
            raise ValueError("renderer summary contains an invalid view")
        image = Path(str(view.get("path", ""))).resolve()
        if image.parent != output.resolve():
            raise ValueError("renderer summary references an image outside the output directory")
        if _png_dimensions(image) != (width, height):
            raise ValueError("renderer produced an image with unexpected dimensions")
        artifacts.append(file_record(image, kind="drc-review-png", role="review.image", maximum_bytes=MAX_IMAGE_BYTES))
        normalized_views.append({**view, "path": str(image)})
    return {**document, "views": normalized_views}, artifacts


def review_drc(
    gds_file: str | Path,
    report_file: str | Path,
    output_dir: str | Path,
    *,
    discovery: DiscoveryManager | None = None,
    layer_properties: str | Path | None = None,
    max_cluster_views: int = 6,
    width: int = 1600,
    height: int = 1200,
    timeout: float = 180.0,
) -> dict:
    """Render an overview and representative hierarchical DRC clusters."""

    if not 0 <= max_cluster_views < MAX_VIEWS:
        return _invalid(f"max_cluster_views must be from 0 through {MAX_VIEWS - 1}")
    if not MIN_IMAGE_DIMENSION <= width <= MAX_IMAGE_DIMENSION or not MIN_IMAGE_DIMENSION <= height <= MAX_IMAGE_DIMENSION:
        return _invalid(f"image dimensions must be from {MIN_IMAGE_DIMENSION} through {MAX_IMAGE_DIMENSION}")
    gds = Path(gds_file).expanduser().resolve()
    report_path = Path(report_file).expanduser().resolve()
    lyp = Path(layer_properties).expanduser().resolve() if layer_properties else None
    try:
        output = _real_empty_output_directory(output_dir)
        inputs = [
            file_record(gds, kind="gds", role="input", maximum_bytes=MAX_GDS_BYTES),
            file_record(report_path, kind="klayout-lyrdb", role="drc.evidence", maximum_bytes=MAX_REPORT_BYTES),
        ]
        if lyp is not None:
            inputs.append(file_record(lyp, kind="klayout-layer-properties", role="review.configuration", maximum_bytes=MAX_LAYER_PROPERTIES_BYTES))
    except (OSError, ValueError, FileRecordError) as exc:
        return _invalid(str(exc))
    if any(not item.get("exists") for item in inputs):
        return _invalid("one or more declared review inputs do not exist")

    parsed = parse_lyrdb(report_path)
    if not parsed.get("validation", {}).get("valid"):
        return _invalid(
            str(parsed.get("error", "the KLayout report is invalid")),
            code=str(parsed.get("validation", {}).get("reason", "drc_review.report.invalid")),
        )
    markers = _marker_examples(parsed)
    if parsed["total_violations"] and not markers:
        return result(
            "drc.review",
            tool=None,
            execution=static_execution("invalid_request"),
            engineering_status="unknown",
            summary="The DRC report is valid but exposes no renderable marker examples.",
            inputs=inputs,
            diagnostics=[diagnostic("error", "drc_review.markers.unavailable", "The bounded LYRDB normalization retained no box, polygon, or edge-pair geometry for rendering.")],
            data={"source_report": parsed},
        )

    manager = discovery or DiscoveryManager()
    binary = manager.find_binary("klayout")
    info = manager.inspect_tool("klayout")
    tool = tool_record("klayout", path=binary, version=info.get("version"))
    if binary is None:
        return result(
            "drc.review",
            tool=tool,
            execution=static_execution("not_available"),
            engineering_status="unknown",
            summary="KLayout is unavailable, so visual DRC evidence was not generated.",
            inputs=inputs,
            diagnostics=[diagnostic("error", "tool.not_available", "KLayout is required to render the GDS review views.")],
            data={"source_report": parsed},
        )

    config_path = output / "review-config.json"
    renderer_path = output / "render-review.py"
    summary_path = output / "render-summary.json"
    transcript_path = output / "render.openada.log"
    config = {
        "schema": "openada.drc-review-config/v1alpha1",
        "gds": str(gds),
        "report": str(report_path),
        "top_cell": parsed["top_cell"],
        "layer_properties": str(lyp) if lyp else None,
        "output_dir": str(output),
        "width": width,
        "height": height,
        "max_cluster_views": max_cluster_views,
        "minimum_marker_um": 0.05,
        "minimum_cluster_margin_um": 1.0,
        "marker_layer": 900,
        "marker_datatype": 0,
        "markers": markers,
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    renderer_path.write_text(_renderer_source(config_path), encoding="utf-8")
    process = run_process([binary, "-b", "-r", renderer_path], cwd=output, timeout=timeout)
    transcript_path.write_text(
        json.dumps(
            {
                "command": process.command,
                "cwd": process.cwd,
                "status": process.status,
                "exit_code": process.exit_code,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "stdout_bytes": process.stdout_bytes,
                "stderr_bytes": process.stderr_bytes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    support_artifacts = [
        file_record(config_path, kind="drc-review-configuration", role="review.configuration"),
        file_record(renderer_path, kind="drc-review-renderer", role="review.renderer"),
        file_record(transcript_path, kind="drc-review-transcript", role="review.evidence"),
    ]
    if process.status != "completed" or process.exit_code != 0:
        return result(
            "drc.review",
            tool=tool,
            execution=process,
            engineering_status="unknown",
            summary="KLayout did not complete the visual DRC review.",
            inputs=inputs,
            artifacts=support_artifacts,
            diagnostics=[diagnostic("error", "drc_review.render.failed", process.error or process.stderr or f"KLayout exited with {process.exit_code}")],
            data={"source_report": parsed, "retained_marker_examples": len(markers)},
        )
    try:
        render_summary, image_artifacts = _read_render_summary(summary_path, output, width, height)
        final_inputs = [
            file_record(gds, kind="gds", role="input", maximum_bytes=MAX_GDS_BYTES),
            file_record(report_path, kind="klayout-lyrdb", role="drc.evidence", maximum_bytes=MAX_REPORT_BYTES),
        ]
        if lyp is not None:
            final_inputs.append(file_record(lyp, kind="klayout-layer-properties", role="review.configuration", maximum_bytes=MAX_LAYER_PROPERTIES_BYTES))
        if final_inputs != inputs:
            raise ValueError("one or more review inputs changed during rendering")
        summary_artifact = file_record(summary_path, kind="drc-review-summary", role="review.evidence")
    except (OSError, ValueError, FileRecordError, json.JSONDecodeError) as exc:
        return result(
            "drc.review",
            tool=tool,
            execution=process,
            engineering_status="unknown",
            summary="KLayout completed, but the visual review artifacts are invalid.",
            inputs=inputs,
            artifacts=support_artifacts,
            diagnostics=[diagnostic("error", "drc_review.artifact.invalid", str(exc))],
            data={"source_report": parsed, "retained_marker_examples": len(markers)},
        )

    limitations = [
        "Images are diagnostic views of bounded retained LYRDB examples, not a replacement for the native report or rule deck.",
        "Hierarchical expansion matches report cell identities to GDS cell names; it does not infer waived or unreported violations.",
    ]
    diagnostics = [diagnostic("warning", "drc_review.diagnostic_only", limitations[0])]
    if parsed.get("violations_truncated") or parsed.get("normalization", {}).get("global_geometry_limit_reached"):
        diagnostics.append(diagnostic("warning", "drc_review.examples.truncated", "The source report exceeds the bounded retained-example budget, so images are representative rather than exhaustive."))
    return result(
        "drc.review",
        tool=tool,
        execution=process,
        engineering_status="pass",
        summary=f"Generated {len(render_summary['views'])} bounded DRC review view(s) from the validated native report.",
        inputs=inputs,
        artifacts=[*support_artifacts, summary_artifact, *image_artifacts],
        diagnostics=diagnostics,
        data={
            "source_report": {
                "validation": parsed["validation"],
                "top_cell": parsed["top_cell"],
                "total_violations": parsed["total_violations"],
                "waived_violations": parsed["waived_violations"],
                "category_counts": parsed["category_counts"],
                "violations_truncated": parsed["violations_truncated"],
            },
            "review": render_summary,
            "diagnosis": _diagnosis_payload(markers),
            "limitations": limitations,
        },
    )


__all__ = ["review_drc"]
