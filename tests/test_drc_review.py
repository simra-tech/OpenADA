from __future__ import annotations

import json
from pathlib import Path

import pytest

from openada.cli import main
from openada.discovery import DiscoveryManager
from openada.operations.drc_review import review_drc


def _fake_klayout(path: Path, *, corrupt_png: bool = False) -> None:
    path.write_text(
        f'''#!/usr/bin/env python3
import ast
import binascii
import json
from pathlib import Path
import struct
import sys
import zlib

if len(sys.argv) == 2 and sys.argv[1] in {{"-v", "--version"}}:
    print("KLayout 0.30.9")
    raise SystemExit(0)

renderer = Path(sys.argv[sys.argv.index("-r") + 1])
line = next(line for line in renderer.read_text().splitlines() if line.startswith("CONFIG_PATH = "))
config_path = Path(ast.literal_eval(line.split("=", 1)[1].strip()))
config = json.loads(config_path.read_text())
output = Path(config["output_dir"])

def png(width, height):
    if {corrupt_png!r}:
        return b"not-a-png"
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", binascii.crc32(kind + body) & 0xffffffff)
    rows = b"".join(b"\\x00" + b"\\x00\\x00\\x00" * width for _ in range(height))
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

overview = output / "00-overview.png"
overview.write_bytes(png(config["width"], config["height"]))
views = [{{"kind": "overview", "path": str(overview), "expanded_markers": len(config["markers"])}}]
if config["markers"] and config["max_cluster_views"]:
    cluster = output / "01-cluster.png"
    cluster.write_bytes(png(config["width"], config["height"]))
    marker = config["markers"][0]
    views.append({{"kind": "cluster", "path": str(cluster), "category": marker["category"], "cell": marker["cell"], "expanded_markers": 4, "bounds_um": [0, 0, 2, 2]}})
summary = {{
    "schema": "openada.drc-review-render/v1alpha1",
    "top_cell": config["top_cell"],
    "retained_marker_examples": len(config["markers"]),
    "expanded_physical_markers": 4 if config["markers"] else 0,
    "unplaced_marker_examples": 0,
    "views": views,
}}
(output / "render-summary.json").write_text(json.dumps(summary))
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _report(
    path: Path,
    deck: Path,
    *,
    geometry: str = "box: (1,2;1.2,2.3)",
    category: str = "WIDTH",
    description: str = "width",
) -> None:
    path.write_text(
        f"""<report-database><description>test</description>
<original-file></original-file><generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories><category><name>M1</name><categories><category><name>{category}</name><description>{description}</description></category></categories></category></categories>
<cells><cell><name>TOP</name></cell><cell><name>LEAF</name></cell></cells>
<items><item><tags/><category>'M1'.{category}</category><cell>LEAF</cell><multiplicity>4</multiplicity>
<values><value>{geometry}</value></values></item></items></report-database>""",
        encoding="utf-8",
    )


def _inputs(tmp_path: Path, *, corrupt_png: bool = False) -> tuple[Path, Path, Path, DiscoveryManager]:
    binary = tmp_path / "klayout"
    _fake_klayout(binary, corrupt_png=corrupt_png)
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"GDS fixture")
    deck = tmp_path / "rules.drc"
    deck.write_text("# fixture\n", encoding="utf-8")
    report = tmp_path / "report.lyrdb"
    _report(report, deck)
    discovery = DiscoveryManager(binary_overrides={"klayout": str(binary)})
    return gds, report, binary, discovery


def test_review_generates_bounded_overview_and_cluster_artifacts(tmp_path: Path) -> None:
    gds, report, binary, discovery = _inputs(tmp_path)
    output = tmp_path / "review"

    payload = review_drc(gds, report, output, discovery=discovery, width=320, height=256)

    assert payload["engineering"]["status"] == "pass"
    assert payload["tool"] == {"name": "klayout", "path": str(binary), "version": "KLayout 0.30.9"}
    assert payload["data"]["source_report"]["total_violations"] == 4
    assert payload["data"]["review"]["expanded_physical_markers"] == 4
    assert payload["data"]["diagnosis"]["rule_family_counts"] == {"minimum-width": 1}
    assert payload["data"]["diagnosis"]["markers"][0]["diagnosis"]["observations"]["width_um"] == pytest.approx(0.2)
    assert [view["kind"] for view in payload["data"]["review"]["views"]] == ["overview", "cluster"]
    images = [item for item in payload["artifacts"] if item["kind"] == "drc-review-png"]
    assert len(images) == 2
    assert all(item["exists"] and item["sha256"] for item in images)
    assert payload["execution"]["command"] == [str(binary), "-b", "-r", str(output / "render-review.py")]


def test_review_deduplicates_equivalent_native_cell_variants(tmp_path: Path) -> None:
    gds, report, _, discovery = _inputs(tmp_path)
    deck = tmp_path / "rules.drc"
    report.write_text(
        f"""<report-database><description>test</description><original-file></original-file>
<generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories><category><name>GRID</name></category></categories>
<cells><cell><name>TOP</name></cell><cell><name>LEAF</name><variant>m0</variant></cell><cell><name>LEAF</name><variant>r0</variant></cell></cells>
<items><item><tags/><category>GRID</category><cell>LEAF:m0</cell><multiplicity>8</multiplicity><values><value>box: (1,2;1.2,2.3)</value></values></item>
<item><tags/><category>GRID</category><cell>LEAF:r0</cell><multiplicity>8</multiplicity><values><value>box: (1,2;1.2,2.3)</value></values></item></items></report-database>""",
        encoding="utf-8",
    )

    payload = review_drc(gds, report, tmp_path / "review", discovery=discovery, width=320, height=256)

    config = json.loads((tmp_path / "review" / "review-config.json").read_text())
    assert payload["engineering"]["status"] == "pass"
    assert len(config["markers"]) == 1
    assert config["markers"][0]["cell"] == "LEAF"
    assert config["markers"][0]["source_cells"] == ["LEAF:m0", "LEAF:r0"]


def test_review_measures_declared_grid_offsets_without_claiming_a_fix(tmp_path: Path) -> None:
    gds, report, _, discovery = _inputs(tmp_path)
    _report(
        report,
        tmp_path / "rules.drc",
        geometry="box: (0.866,18.957;0.866,18.957)",
        category="metal1_pin_Offgrid",
        description="metal1_pin layer is Offgrid (drawing grid of 5 nm)",
    )

    payload = review_drc(
        gds, report, tmp_path / "review", discovery=discovery, width=320, height=256
    )

    diagnosis = payload["data"]["diagnosis"]["markers"][0]["diagnosis"]
    assert diagnosis["rule_family"] == "off-grid"
    assert diagnosis["declared_length_constraints"] == [
        {"value": 5.0, "unit": "nm", "value_um": 0.005, "source": "native-rule-description"}
    ]
    assert diagnosis["observations"]["grid"]["maximum_offset_um"] == pytest.approx(0.002)
    assert "not a reconstructed rule measurement" in diagnosis["limitations"][0]


def test_review_refuses_nonfresh_output_directory(tmp_path: Path) -> None:
    gds, report, _, discovery = _inputs(tmp_path)
    output = tmp_path / "review"
    output.mkdir()
    (output / "ambient.txt").write_text("old\n", encoding="utf-8")

    payload = review_drc(gds, report, output, discovery=discovery)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["execution"]["status"] == "invalid_request"
    assert "must be empty" in payload["diagnostics"][0]["message"]


def test_review_rejects_valid_report_without_renderable_geometry(tmp_path: Path) -> None:
    gds, report, _, discovery = _inputs(tmp_path)
    deck = tmp_path / "rules.drc"
    _report(report, deck, geometry="text: not geometry")

    payload = review_drc(gds, report, tmp_path / "review", discovery=discovery)

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "drc_review.markers.unavailable"
    assert not (tmp_path / "review" / "render-review.py").exists()


def test_review_does_not_accept_corrupt_renderer_images(tmp_path: Path) -> None:
    gds, report, _, discovery = _inputs(tmp_path, corrupt_png=True)

    payload = review_drc(gds, report, tmp_path / "review", discovery=discovery, width=320, height=256)

    assert payload["execution"]["status"] == "completed"
    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "drc_review.artifact.invalid"


def test_cli_routes_drc_review_and_emits_one_result(tmp_path: Path, capsys) -> None:
    gds, report, binary, _ = _inputs(tmp_path)
    output = tmp_path / "review"

    exit_code = main(
        [
            "--compact",
            "--tool-path",
            f"klayout={binary}",
            "drc-review",
            str(gds),
            "--report",
            str(report),
            "--output-dir",
            str(output),
            "--width",
            "320",
            "--height",
            "256",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["operation"] == "drc.review"
    assert payload["engineering"]["status"] == "pass"
