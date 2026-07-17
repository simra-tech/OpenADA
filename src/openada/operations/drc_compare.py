"""Compare bounded native KLayout DRC evidence across revisions or decks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contract import FileRecordError, diagnostic, file_record, result, static_execution
from ..engines.klayout_outputs import MAX_REPORT_BYTES, parse_lyrdb
from .drc_review import MAX_GDS_BYTES, _marker_examples


MODES = {"revision", "deck"}
MAX_LVS_RESULT_BYTES = 16 * 1024 * 1024


def _invalid(message: str, *, code: str = "drc_compare.request.invalid") -> dict:
    return result(
        "drc.compare",
        tool=None,
        execution=static_execution("invalid_request"),
        engineering_status="unknown",
        summary="The DRC comparison request is invalid.",
        diagnostics=[diagnostic("error", code, message)],
    )


def _fingerprint(marker: dict[str, Any]) -> tuple[Any, ...]:
    return (
        marker["category"],
        marker["cell"],
        tuple(round(float(value), 9) for value in marker["box_um"]),
        marker["waived"],
    )


def _record(marker: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": marker["category"],
        "cell": marker["cell"],
        "box_um": marker["box_um"],
        "waived": marker["waived"],
        "multiplicity": marker["multiplicity"],
        "diagnosis": marker["diagnosis"],
    }


def _distance(first: list[float], second: list[float]) -> float:
    first_center = ((first[0] + first[2]) / 2.0, (first[1] + first[3]) / 2.0)
    second_center = ((second[0] + second[2]) / 2.0, (second[1] + second[3]) / 2.0)
    return ((first_center[0] - second_center[0]) ** 2 + (first_center[1] - second_center[1]) ** 2) ** 0.5


def _spatial_correlations(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    tolerance_um: float,
) -> list[dict[str, Any]]:
    correlations: list[dict[str, Any]] = []
    used: set[int] = set()
    for first in baseline:
        choices = [
            (index, _distance(first["box_um"], second["box_um"]), second)
            for index, second in enumerate(candidate)
            if index not in used and first["cell"] == second["cell"]
        ]
        if not choices:
            continue
        index, distance, second = min(choices, key=lambda item: (item[1], item[0]))
        if distance > tolerance_um:
            continue
        used.add(index)
        correlations.append(
            {
                "cell": first["cell"],
                "distance_um": distance,
                "baseline_category": first["category"],
                "candidate_category": second["category"],
                "same_category": first["category"] == second["category"],
                "baseline_box_um": first["box_um"],
                "candidate_box_um": second["box_um"],
            }
        )
    return correlations


def _lvs_connectivity_invariant(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    documents = []
    for label, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("schema") != "openada.result/v0alpha1"
            or document.get("operation") != "lvs"
        ):
            raise ValueError(f"{label} LVS result is not an OpenADA LVS result")
        if document.get("engineering", {}).get("status") != "pass":
            raise ValueError(f"{label} LVS result does not establish a passing match")
        inputs = document.get("inputs")
        if not isinstance(inputs, list):
            raise ValueError(f"{label} LVS result has no bounded input inventory")
        references = [item for item in inputs if item.get("role") == "reference"]
        setups = [item for item in inputs if item.get("kind") == "netgen-setup"]
        layouts = [item for item in inputs if item.get("kind") == "layout-netlist"]
        if len(references) != 1 or len(setups) != 1 or len(layouts) != 1:
            raise ValueError(f"{label} LVS result does not identify unique reference, setup, and layout netlists")
        documents.append(
            {
                "reference_sha256": references[0].get("sha256"),
                "setup_sha256": setups[0].get("sha256"),
                "layout_netlist_sha256": layouts[0].get("sha256"),
            }
        )
    if documents[0]["reference_sha256"] != documents[1]["reference_sha256"]:
        raise ValueError("LVS results do not use the same reference netlist")
    if documents[0]["setup_sha256"] != documents[1]["setup_sha256"]:
        raise ValueError("LVS results do not use the same Netgen setup")
    return {
        "established": True,
        "reference_netlist_sha256": documents[0]["reference_sha256"],
        "netgen_setup_sha256": documents[0]["setup_sha256"],
        "baseline_layout_netlist_sha256": documents[0]["layout_netlist_sha256"],
        "candidate_layout_netlist_sha256": documents[1]["layout_netlist_sha256"],
        "meaning": "Both retained OpenADA LVS results pass against the same reference netlist and Netgen setup.",
        "limitation": "This comparison does not prove that either extracted layout netlist was derived from the declared GDS input.",
    }


def compare_drc(
    baseline_gds: str | Path,
    baseline_report: str | Path,
    candidate_gds: str | Path,
    candidate_report: str | Path,
    *,
    mode: str = "revision",
    spatial_tolerance_um: float = 0.001,
    baseline_lvs_result: str | Path | None = None,
    candidate_lvs_result: str | Path | None = None,
) -> dict:
    """Compare exact and spatially correlated bounded DRC marker examples."""

    if mode not in MODES:
        return _invalid(f"mode must be one of: {', '.join(sorted(MODES))}")
    if not 0 <= spatial_tolerance_um <= 10.0:
        return _invalid("spatial_tolerance_um must be from 0 through 10")
    if (baseline_lvs_result is None) != (candidate_lvs_result is None):
        return _invalid("baseline_lvs_result and candidate_lvs_result must be supplied together")
    if mode == "deck" and baseline_lvs_result is not None:
        return _invalid("paired LVS evidence is supported only in revision mode")
    paths = [
        Path(baseline_gds).expanduser().resolve(),
        Path(baseline_report).expanduser().resolve(),
        Path(candidate_gds).expanduser().resolve(),
        Path(candidate_report).expanduser().resolve(),
    ]
    try:
        inputs = [
            file_record(paths[0], kind="gds", role="baseline.layout", maximum_bytes=MAX_GDS_BYTES),
            file_record(paths[1], kind="klayout-lyrdb", role="baseline.drc", maximum_bytes=MAX_REPORT_BYTES),
            file_record(paths[2], kind="gds", role="candidate.layout", maximum_bytes=MAX_GDS_BYTES),
            file_record(paths[3], kind="klayout-lyrdb", role="candidate.drc", maximum_bytes=MAX_REPORT_BYTES),
        ]
        lvs_paths = None
        if baseline_lvs_result is not None and candidate_lvs_result is not None:
            lvs_paths = (
                Path(baseline_lvs_result).expanduser().resolve(),
                Path(candidate_lvs_result).expanduser().resolve(),
            )
            inputs.extend(
                [
                    file_record(lvs_paths[0], kind="openada-result", role="baseline.lvs", maximum_bytes=MAX_LVS_RESULT_BYTES),
                    file_record(lvs_paths[1], kind="openada-result", role="candidate.lvs", maximum_bytes=MAX_LVS_RESULT_BYTES),
                ]
            )
    except (OSError, ValueError, FileRecordError) as exc:
        return _invalid(str(exc))
    if any(not item.get("exists") for item in inputs):
        return _invalid("one or more declared comparison inputs do not exist")

    baseline = parse_lyrdb(paths[1])
    candidate = parse_lyrdb(paths[3])
    for label, parsed in (("baseline", baseline), ("candidate", candidate)):
        if not parsed.get("validation", {}).get("valid"):
            return _invalid(
                f"{label} report is invalid: {parsed.get('error', 'unknown report error')}",
                code="drc_compare.report.invalid",
            )
    same_gds = inputs[0]["sha256"] == inputs[2]["sha256"]
    same_deck = baseline.get("generator_script") == candidate.get("generator_script")
    if mode == "revision" and same_gds:
        return _invalid("revision mode requires distinct baseline and candidate GDS content")
    if mode == "deck" and not same_gds:
        return _invalid("deck mode requires hash-identical baseline and candidate GDS content")
    if mode == "deck" and same_deck:
        return _invalid("deck mode requires reports that identify different generator scripts")

    baseline_markers = _marker_examples(baseline)
    candidate_markers = _marker_examples(candidate)
    baseline_by_key = {_fingerprint(marker): marker for marker in baseline_markers}
    candidate_by_key = {_fingerprint(marker): marker for marker in candidate_markers}
    persistent_keys = sorted(baseline_by_key.keys() & candidate_by_key.keys())
    resolved_keys = sorted(baseline_by_key.keys() - candidate_by_key.keys())
    introduced_keys = sorted(candidate_by_key.keys() - baseline_by_key.keys())
    correlations = _spatial_correlations(
        [baseline_by_key[key] for key in resolved_keys],
        [candidate_by_key[key] for key in introduced_keys],
        tolerance_um=spatial_tolerance_um,
    )
    try:
        connectivity = (
            _lvs_connectivity_invariant(*lvs_paths) if lvs_paths is not None else None
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _invalid(str(exc), code="drc_compare.lvs.invalid")
    try:
        final_inputs = [
            file_record(paths[0], kind="gds", role="baseline.layout", maximum_bytes=MAX_GDS_BYTES),
            file_record(paths[1], kind="klayout-lyrdb", role="baseline.drc", maximum_bytes=MAX_REPORT_BYTES),
            file_record(paths[2], kind="gds", role="candidate.layout", maximum_bytes=MAX_GDS_BYTES),
            file_record(paths[3], kind="klayout-lyrdb", role="candidate.drc", maximum_bytes=MAX_REPORT_BYTES),
        ]
        if lvs_paths is not None:
            final_inputs.extend(
                [
                    file_record(lvs_paths[0], kind="openada-result", role="baseline.lvs", maximum_bytes=MAX_LVS_RESULT_BYTES),
                    file_record(lvs_paths[1], kind="openada-result", role="candidate.lvs", maximum_bytes=MAX_LVS_RESULT_BYTES),
                ]
            )
        if final_inputs != inputs:
            raise ValueError("one or more comparison inputs changed while being parsed")
    except (OSError, ValueError, FileRecordError) as exc:
        return _invalid(str(exc), code="drc_compare.input.changed")

    limitations = [
        "Comparison uses bounded normalized LYRDB examples and is not exhaustive when either report is truncated.",
        "Resolved markers do not establish DRC cleanliness; introduced markers do not identify the responsible edit.",
        "Spatial correlation is proximity evidence, not proof that two decks implement equivalent rules.",
        "Connectivity is outside this operation and requires separately validated LVS evidence.",
    ]
    data = {
        "schema": "openada.drc-comparison/v1alpha1",
        "mode": mode,
        "identity": {
            "same_gds_content": same_gds,
            "same_generator_script": same_deck,
            "baseline_generator_script": baseline.get("generator_script"),
            "candidate_generator_script": candidate.get("generator_script"),
        },
        "native_totals": {
            "baseline": baseline["total_violations"],
            "candidate": candidate["total_violations"],
            "delta": candidate["total_violations"] - baseline["total_violations"],
        },
        "bounded_examples": {
            "baseline": len(baseline_markers),
            "candidate": len(candidate_markers),
            "persistent": [_record(baseline_by_key[key]) for key in persistent_keys],
            "resolved": [_record(baseline_by_key[key]) for key in resolved_keys],
            "introduced": [_record(candidate_by_key[key]) for key in introduced_keys],
            "spatial_correlations": correlations,
            "spatial_tolerance_um": spatial_tolerance_um,
        },
        "truncated": bool(
            baseline.get("violations_truncated") or candidate.get("violations_truncated")
        ),
        "connectivity_invariant": connectivity,
        "limitations": limitations,
    }
    return result(
        "drc.compare",
        tool=None,
        execution=static_execution("completed"),
        engineering_status="pass",
        summary=(
            f"Compared validated DRC evidence: {len(persistent_keys)} persistent, "
            f"{len(resolved_keys)} resolved, and {len(introduced_keys)} introduced bounded example(s)."
        ),
        inputs=inputs,
        diagnostics=[diagnostic("warning", "drc_compare.diagnostic_only", limitations[0])],
        data=data,
    )


__all__ = ["MODES", "compare_drc"]
