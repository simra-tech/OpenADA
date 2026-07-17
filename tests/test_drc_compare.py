from __future__ import annotations

import json
from pathlib import Path

from openada.cli import main
from openada.operations.drc_compare import compare_drc


def _report(
    path: Path,
    deck: Path,
    items: list[tuple[str, str, str]],
) -> None:
    categories = "".join(
        f"<category><name>{category}</name><description>{description}</description></category>"
        for category, description, _ in items
    )
    records = "".join(
        f"<item><tags/><category>{category}</category><cell>TOP</cell><multiplicity>1</multiplicity>"
        f"<values><value>{geometry}</value></values></item>"
        for category, _, geometry in items
    )
    path.write_text(
        f"""<report-database><description>test</description><original-file></original-file>
<generator>drc: script='{deck}'</generator><top-cell>TOP</top-cell>
<categories>{categories}</categories><cells><cell><name>TOP</name></cell></cells>
<items>{records}</items></report-database>""",
        encoding="utf-8",
    )


def _lvs_result(path: Path, *, reference: str, setup: str, layout: str, status: str = "pass") -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "openada.result/v0alpha1",
                "operation": "lvs",
                "engineering": {"status": status, "summary": "fixture"},
                "inputs": [
                    {"kind": "layout-netlist", "role": "input", "sha256": layout},
                    {"kind": "schematic-netlist", "role": "reference", "sha256": reference},
                    {"kind": "netgen-setup", "role": "rules", "sha256": setup},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_revision_comparison_reports_persistent_resolved_and_introduced(tmp_path: Path) -> None:
    baseline_gds = tmp_path / "before.gds"
    candidate_gds = tmp_path / "after.gds"
    baseline_gds.write_bytes(b"before")
    candidate_gds.write_bytes(b"after")
    deck = tmp_path / "rules.drc"
    deck.write_text("# rules\n", encoding="utf-8")
    baseline = tmp_path / "before.lyrdb"
    candidate = tmp_path / "after.lyrdb"
    _report(
        baseline,
        deck,
        [
            ("WIDTH", "minimum width 0.2 um", "box: (1,1;1.1,1.1)"),
            ("SPACE", "minimum spacing 0.3 um", "box: (2,2;2.1,2.1)"),
        ],
    )
    _report(
        candidate,
        deck,
        [
            ("WIDTH", "minimum width 0.2 um", "box: (1,1;1.1,1.1)"),
            ("AREA", "minimum area", "box: (3,3;3.1,3.1)"),
        ],
    )

    payload = compare_drc(baseline_gds, baseline, candidate_gds, candidate)

    examples = payload["data"]["bounded_examples"]
    assert payload["engineering"]["status"] == "pass"
    assert len(examples["persistent"]) == 1
    assert len(examples["resolved"]) == 1
    assert len(examples["introduced"]) == 1
    assert payload["data"]["native_totals"] == {"baseline": 2, "candidate": 2, "delta": 0}
    assert "requires separately validated LVS evidence" in payload["data"]["limitations"][3]


def test_revision_comparison_requires_changed_layout_content(tmp_path: Path) -> None:
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"same")
    deck = tmp_path / "rules.drc"
    deck.write_text("# rules\n", encoding="utf-8")
    baseline = tmp_path / "before.lyrdb"
    candidate = tmp_path / "after.lyrdb"
    item = [("WIDTH", "width", "box: (1,1;1,1)")]
    _report(baseline, deck, item)
    _report(candidate, deck, item)

    payload = compare_drc(gds, baseline, gds, candidate)

    assert payload["engineering"]["status"] == "unknown"
    assert "distinct baseline and candidate GDS" in payload["diagnostics"][0]["message"]


def test_revision_comparison_can_bind_paired_passing_lvs_evidence(tmp_path: Path) -> None:
    before_gds = tmp_path / "before.gds"
    after_gds = tmp_path / "after.gds"
    before_gds.write_bytes(b"before")
    after_gds.write_bytes(b"after")
    deck = tmp_path / "rules.drc"
    deck.write_text("# rules\n", encoding="utf-8")
    before = tmp_path / "before.lyrdb"
    after = tmp_path / "after.lyrdb"
    item = [("WIDTH", "width", "box: (1,1;1,1)")]
    _report(before, deck, item)
    _report(after, deck, item)
    before_lvs = tmp_path / "before-lvs.json"
    after_lvs = tmp_path / "after-lvs.json"
    _lvs_result(before_lvs, reference="reference", setup="setup", layout="before-layout")
    _lvs_result(after_lvs, reference="reference", setup="setup", layout="after-layout")

    payload = compare_drc(
        before_gds,
        before,
        after_gds,
        after,
        baseline_lvs_result=before_lvs,
        candidate_lvs_result=after_lvs,
    )

    invariant = payload["data"]["connectivity_invariant"]
    assert invariant["established"] is True
    assert invariant["reference_netlist_sha256"] == "reference"
    assert invariant["baseline_layout_netlist_sha256"] == "before-layout"
    assert invariant["candidate_layout_netlist_sha256"] == "after-layout"
    assert "does not prove" in invariant["limitation"]


def test_revision_comparison_rejects_mismatched_lvs_references(tmp_path: Path) -> None:
    before_gds = tmp_path / "before.gds"
    after_gds = tmp_path / "after.gds"
    before_gds.write_bytes(b"before")
    after_gds.write_bytes(b"after")
    deck = tmp_path / "rules.drc"
    deck.write_text("# rules\n", encoding="utf-8")
    before = tmp_path / "before.lyrdb"
    after = tmp_path / "after.lyrdb"
    item = [("WIDTH", "width", "box: (1,1;1,1)")]
    _report(before, deck, item)
    _report(after, deck, item)
    before_lvs = tmp_path / "before-lvs.json"
    after_lvs = tmp_path / "after-lvs.json"
    _lvs_result(before_lvs, reference="first", setup="setup", layout="before-layout")
    _lvs_result(after_lvs, reference="second", setup="setup", layout="after-layout")

    payload = compare_drc(
        before_gds,
        before,
        after_gds,
        after,
        baseline_lvs_result=before_lvs,
        candidate_lvs_result=after_lvs,
    )

    assert payload["engineering"]["status"] == "unknown"
    assert payload["diagnostics"][0]["code"] == "drc_compare.lvs.invalid"


def test_deck_comparison_correlates_different_rule_names_on_same_geometry(tmp_path: Path) -> None:
    gds = tmp_path / "layout.gds"
    gds.write_bytes(b"same layout")
    first_deck = tmp_path / "first.drc"
    second_deck = tmp_path / "second.drc"
    first_deck.write_text("# first\n", encoding="utf-8")
    second_deck.write_text("# second\n", encoding="utf-8")
    first = tmp_path / "first.lyrdb"
    second = tmp_path / "second.lyrdb"
    _report(first, first_deck, [("M1_SPACE", "spacing", "box: (4,5;4,5)")])
    _report(second, second_deck, [("MET1_SEP", "separation", "box: (4.0005,5;4.0005,5)")])

    payload = compare_drc(
        gds, first, gds, second, mode="deck", spatial_tolerance_um=0.001
    )

    correlations = payload["data"]["bounded_examples"]["spatial_correlations"]
    assert payload["engineering"]["status"] == "pass"
    assert payload["data"]["identity"]["same_gds_content"] is True
    assert len(correlations) == 1
    assert correlations[0]["same_category"] is False
    assert correlations[0]["distance_um"] < 0.001


def test_deck_comparison_rejects_different_layout_content(tmp_path: Path) -> None:
    first_gds = tmp_path / "first.gds"
    second_gds = tmp_path / "second.gds"
    first_gds.write_bytes(b"first")
    second_gds.write_bytes(b"second")
    first_deck = tmp_path / "first.drc"
    second_deck = tmp_path / "second.drc"
    first_deck.write_text("# first\n", encoding="utf-8")
    second_deck.write_text("# second\n", encoding="utf-8")
    first = tmp_path / "first.lyrdb"
    second = tmp_path / "second.lyrdb"
    item = [("WIDTH", "width", "box: (1,1;1,1)")]
    _report(first, first_deck, item)
    _report(second, second_deck, item)

    payload = compare_drc(first_gds, first, second_gds, second, mode="deck")

    assert payload["engineering"]["status"] == "unknown"
    assert "hash-identical" in payload["diagnostics"][0]["message"]


def test_cli_routes_drc_compare(tmp_path: Path, capsys) -> None:
    before_gds = tmp_path / "before.gds"
    after_gds = tmp_path / "after.gds"
    before_gds.write_bytes(b"before")
    after_gds.write_bytes(b"after")
    deck = tmp_path / "rules.drc"
    deck.write_text("# rules\n", encoding="utf-8")
    before = tmp_path / "before.lyrdb"
    after = tmp_path / "after.lyrdb"
    item = [("WIDTH", "width", "box: (1,1;1,1)")]
    _report(before, deck, item)
    _report(after, deck, item)

    exit_code = main(
        [
            "--compact",
            "drc-compare",
            str(before_gds),
            "--baseline-report",
            str(before),
            "--candidate-gds",
            str(after_gds),
            "--candidate-report",
            str(after),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["operation"] == "drc.compare"
    assert payload["engineering"]["status"] == "pass"
