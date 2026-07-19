"""Cross-step semantic checks for additive multi-step result envelopes."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import SchemaCatalog


class ResultSemanticError(ValueError):
    """A schema-valid result makes an inconsistent workflow truth claim."""


def validate_result_semantics(
    result: Mapping[str, Any], schemas: SchemaCatalog | None = None
) -> None:
    """Validate dependency order and conservative truth across result dimensions."""

    catalog = schemas or SchemaCatalog()
    catalog.validate(result)
    if result.get("schema") != "openada.result/v0alpha2":
        raise ResultSemanticError("multi-step result uses an unsupported schema")
    seen: set[str] = set()
    step_outputs: set[str] = set()
    for index, step in enumerate(result["steps"]):
        identity = step["id"]
        if identity in seen:
            raise ResultSemanticError(f"result repeats step identity {identity!r}")
        missing = set(step["depends_on"]) - seen
        if missing:
            raise ResultSemanticError(
                f"step {identity!r} has missing or forward dependencies: {sorted(missing)}"
            )
        seen.add(identity)
        if len(set(step["outputs"])) != len(step["outputs"]):
            raise ResultSemanticError(f"step {identity!r} repeats an output identity")
        step_outputs.update(step["outputs"])
        dimensions = step["dimensions"]
        termination = step["termination"]
        execution = dimensions["execution_state"]
        expected_execution = {
            "completed": "completed",
            "failed": "failed",
            "timed-out": "timed-out",
            "cancelled": "cancelled",
            "not-started": "not-started",
            "unknown": "unknown",
        }[termination]
        if execution != expected_execution:
            raise ResultSemanticError(
                f"step {identity!r} termination conflicts with execution state"
            )
        _conservative_dimensions(dimensions, f"step {identity!r}")
    artifacts = {artifact["sha256"] for artifact in result["artifacts"]}
    if not step_outputs.issubset(artifacts):
        raise ResultSemanticError("one or more step outputs lack artifact records")
    overall = result["overall"]
    _conservative_dimensions(overall, "overall result")
    if overall["execution_state"] == "completed" and any(
        step["termination"] != "completed" for step in result["steps"]
    ):
        raise ResultSemanticError(
            "overall execution cannot be completed when a step is non-complete"
        )
    if overall["signoff_approval"] == "approved" and overall["workflow_review"] != "reviewed":
        raise ResultSemanticError("signoff approval requires an explicit workflow review")


def _conservative_dimensions(dimensions: Mapping[str, Any], role: str) -> None:
    if dimensions["engineering_conclusion"] == "pass" and (
        dimensions["dependency_readiness"] not in {"ready", "not-applicable"}
        or dimensions["execution_state"] != "completed"
        or dimensions["artifact_readiness"] not in {"ready", "not-applicable"}
    ):
        raise ResultSemanticError(
            f"{role} cannot claim engineering pass without ready dependencies, completed execution, and ready artifacts"
        )
