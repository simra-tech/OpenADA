"""Bounded, strict helpers for the offline paired-agent evaluation tools."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE / "schemas"
CAMPAIGN_SCHEMA = "openada.eval.campaign/v0alpha1"
PLAN_SCHEMA = "openada.eval.plan/v0alpha1"
PLAN_RANDOMIZATION_ALGORITHM = "hmac-sha256-fisher-yates-v1"
SUPERVISOR_SCHEMA = "openada.eval.supervisor/v0alpha1"
TRIAL_SCHEMA = "openada.eval.trial/v0alpha1"
SUMMARY_SCHEMA = "openada.eval.summary/v0alpha1"
NATIVE_SCORE_SCHEMA = "openada.eval.score/v0alpha1"

MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_TRACE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_JSON_BYTES = 5 * 1024 * 1024
MAX_ISSUES = 100
MAX_MESSAGE_CHARS = 2_000
MAX_JSON_INTEGER_CHARACTERS = 20
MAX_JSON_DEPTH = 64
MAX_EXACT_SUMMARY_INTEGER = 2**52 - 1
TRIAL_SIGNATURE_DOMAIN = b"openada.eval.trial/v0alpha1\x00"
SUMMARY_SIGNATURE_DOMAIN = b"openada.eval.summary/v0alpha1\x00"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[a-z]+-[0-9a-f]{24}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
IHP_PAIRED_TASK_ID = "ihp-inverter-xschem-ngspice-paired"
IHP_PAIRED_TASK_DIR = HERE / "tasks" / "ihp-inverter-sim"
IHP_TASK_FILES = {
    "manifest": "manifest.json",
    "prompt": "task.md",
    "response_schema": "submission.schema.json",
    "scorer": "native_score.py",
}
SUPPORTED_METRICS = {
    "verified_artifact_complete": ("boolean", "higher"),
    "reported_status_correct": ("boolean", "higher"),
    "wall_time_ms": ("integer", "lower"),
    "agent_action_count": ("integer", "lower"),
    "command_result_observed_characters": ("integer", "lower"),
    "input_tokens": ("integer", "lower"),
    "cached_input_tokens": ("integer", "lower"),
    "output_tokens": ("integer", "lower"),
    "reasoning_output_tokens": ("integer", "lower"),
    "adapter_duration_ms": ("integer", "lower"),
    "native_execution_verified": ("boolean", "higher"),
    "session_count": ("integer", "lower"),
    "provider_request_count": ("integer", "lower"),
    "request_latency_ms": ("integer", "lower"),
    "ttft_ms": ("integer", "lower"),
    "api_retry_count": ("integer", "lower"),
    "model_context_bytes": ("integer", "lower"),
}


class EvaluationError(RuntimeError):
    """A bounded evaluation input or invariant failed."""


def _bounded_message(value: object) -> str:
    text = str(value)
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    half = (MAX_MESSAGE_CHARS - 3) // 2
    return f"{text[:half]}...{text[-half:]}"


def _reject_constant(_: str) -> None:
    raise ValueError("non-standard JSON constant is not allowed")


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is not allowed")
        result[key] = value
    return result


def _parse_json_int(value: str) -> int:
    if len(value.removeprefix("-")) > MAX_JSON_INTEGER_CHARACTERS:
        raise ValueError("JSON integer is outside the reviewed bound")
    parsed = int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        raise ValueError("JSON integer is outside the reviewed bound")
    return parsed


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number is not allowed")
    return parsed


def _json_depth_within_bound(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return True


def _regular_open(path: Path, *, maximum_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise EvaluationError(f"cannot stat input {path}: {_bounded_message(exc)}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise EvaluationError(f"input must be a regular, non-symbolic-link file: {path}")
    if before.st_nlink != 1:
        raise EvaluationError(f"input must have exactly one hard link: {path}")
    if before.st_size > maximum_bytes:
        raise EvaluationError(
            f"input exceeds the {maximum_bytes}-byte bound: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvaluationError(f"cannot open input {path}: {_bounded_message(exc)}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise EvaluationError(f"opened input is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EvaluationError(f"input identity changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise EvaluationError(
                    f"input exceeds the {maximum_bytes}-byte bound: {path}"
                )
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise EvaluationError(f"input changed while being read: {path}")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def read_regular_bytes(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> bytes:
    return _regular_open(path, maximum_bytes=maximum_bytes)[0]


def load_json(
    path: Path,
    *,
    maximum_bytes: int = MAX_JSON_BYTES,
) -> tuple[dict[str, Any], str, int]:
    encoded = read_regular_bytes(path, maximum_bytes=maximum_bytes)
    try:
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
            parse_int=_parse_json_int,
            parse_float=_parse_json_float,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise EvaluationError(f"cannot decode strict JSON {path}: {_bounded_message(exc)}") from exc
    if not isinstance(document, dict):
        raise EvaluationError(f"JSON root must be an object: {path}")
    if not _json_depth_within_bound(document):
        raise EvaluationError(f"JSON nesting exceeds the reviewed bound: {path}")
    return document, hashlib.sha256(encoded).hexdigest(), len(encoded)


def sha256_regular_file(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> str:
    return hashlib.sha256(
        read_regular_bytes(path, maximum_bytes=maximum_bytes)
    ).hexdigest()


def _load_schema(filename: str) -> dict[str, Any]:
    schema, _, _ = load_json(SCHEMA_DIR / filename)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise EvaluationError(
            f"invalid bundled schema {filename}: {_bounded_message(exc.message)}"
        ) from exc
    return schema


def validate_schema(document: dict[str, Any], filename: str, *, label: str) -> None:
    validator = Draft202012Validator(
        _load_schema(filename), format_checker=FormatChecker()
    )
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if not errors:
        return
    issues: list[str] = []
    for error in errors[:MAX_ISSUES]:
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        issues.append(_bounded_message(f"{location}: {error.message}"))
    raise EvaluationError(f"{label} violates its schema: {'; '.join(issues)}")


def _safe_relative_path(raw: object, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 2000:
        raise EvaluationError(f"{label} must be a non-empty bounded path")
    path = Path(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise EvaluationError(f"{label} must be a normalized relative path")
    return path


def _task_file_path(base: Path, relative: Path, *, label: str) -> Path:
    current = base
    for component in relative.parts[:-1]:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise EvaluationError(f"cannot stat {label} parent directory") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise EvaluationError(f"{label} parent must be a real directory")
    target = base / relative
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise EvaluationError(f"cannot resolve {label}") from exc
    if resolved != base and base not in resolved.parents:
        raise EvaluationError(f"{label} escapes the campaign bundle")
    return target


def _records_by_key(
    records: object,
    *,
    key: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise EvaluationError(f"{label} must be an array")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get(key), str):
            raise EvaluationError(f"{label} contains an invalid record")
        value = record[key]
        if value in result:
            raise EvaluationError(f"{label} contains duplicate {key} values")
        result[value] = record
    return result


def _validate_ihp_task_binding(
    campaign: dict[str, Any], manifest: dict[str, Any], task_paths: dict[str, Path]
) -> None:
    if manifest.get("schema") != "openada.eval.task/v0alpha1":
        raise EvaluationError("task manifest has an unsupported schema")
    if manifest.get("id") != IHP_PAIRED_TASK_ID:
        raise EvaluationError("task manifest ID differs from the campaign task")
    if campaign["task"]["id"] != IHP_PAIRED_TASK_ID:
        raise EvaluationError("campaign task ID is unsupported by this evaluation kit")
    if task_paths["response_schema"] != (
        task_paths["scorer"].parent / "submission.schema.json"
    ):
        raise EvaluationError(
            "task response schema must be the scorer-adjacent submission.schema.json"
        )

    # The sole v0alpha1 task is executable packaged policy, not campaign-owned
    # plugin code.  A self-consistent campaign hash must not authorize an
    # arbitrary Python scorer to execute with the evaluator's host privileges.
    # Bind every copied task file to the reviewed bytes shipped with this kit.
    for key, filename in IHP_TASK_FILES.items():
        canonical = sha256_regular_file(
            IHP_PAIRED_TASK_DIR / filename, maximum_bytes=MAX_JSON_BYTES
        )
        if campaign["task"][key]["sha256"] != canonical:
            raise EvaluationError(
                f"task.{key} differs from the canonical packaged IHP task"
            )

    design = manifest.get("design")
    runtime = manifest.get("runtime")
    if not isinstance(design, dict) or not isinstance(runtime, dict):
        raise EvaluationError("task manifest lacks reviewed design/runtime identities")
    if campaign["runtime"]["design"] != {
        "repository": design.get("repository"),
        "revision": design.get("revision"),
    }:
        raise EvaluationError("campaign design identity differs from the frozen task")

    image_reference = runtime.get("image_reference")
    if not isinstance(image_reference, str) or "@sha256:" not in image_reference:
        raise EvaluationError("task manifest image reference is not digest pinned")
    manifest_digest = image_reference.rsplit("@", 1)[1]
    platform = runtime.get("platform")
    if not isinstance(platform, str) or platform.count("/") != 1:
        raise EvaluationError("task manifest platform is invalid")
    os_name, architecture = platform.split("/", 1)
    expected_image = {
        "reference": image_reference,
        "digest": manifest_digest,
        "config_digest": runtime.get("image_config_digest"),
        "os": os_name,
        "architecture": architecture,
    }
    if campaign["runtime"]["image"] != expected_image:
        raise EvaluationError("campaign image identity differs from the frozen task")

    if campaign["runtime"]["pdk"]["name"] != runtime.get("pdk") or campaign[
        "runtime"
    ]["pdk"]["revision"] != runtime.get("pdk_revision"):
        raise EvaluationError("campaign PDK identity differs from the frozen task")

    manifest_tools = _records_by_key(
        manifest.get("tools"), key="name", label="task manifest tools"
    )
    campaign_tools = _records_by_key(
        campaign["runtime"]["tools"], key="name", label="campaign runtime tools"
    )
    if set(manifest_tools) != set(campaign_tools):
        raise EvaluationError("campaign tool set differs from the frozen task")
    for name, expected in manifest_tools.items():
        actual = campaign_tools[name]
        if (
            actual["path"] != expected.get("path")
            or actual["version"] != expected.get("version")
            or actual["binary_sha256"] != expected.get("binary_sha256")
        ):
            raise EvaluationError(
                f"campaign {name} identity differs from the frozen task"
            )

    inputs = _records_by_key(
        manifest.get("inputs"), key="role", label="task manifest inputs"
    )
    pdk_roles = {"xschem-rcfile", "pdk-revision", "ngspice-init"}
    startup_roles = {"ngspice-system-init"}
    if not pdk_roles.issubset(inputs) or not startup_roles.issubset(inputs):
        raise EvaluationError("task manifest lacks reviewed PDK/startup identities")
    expected_pdk_files = {
        (inputs[role].get("path"), inputs[role].get("sha256")) for role in pdk_roles
    }
    actual_pdk_files = {
        (record["path"], record["sha256"])
        for record in campaign["runtime"]["pdk"]["identity_files"]
    }
    if actual_pdk_files != expected_pdk_files:
        raise EvaluationError("campaign PDK files differ from the frozen task inputs")
    expected_startup_files = {
        (inputs[role].get("path"), inputs[role].get("sha256"))
        for role in startup_roles
    }
    actual_startup_files = {
        (record["path"], record["sha256"])
        for record in campaign["runtime"]["startup_files"]
    }
    if actual_startup_files != expected_startup_files:
        raise EvaluationError("campaign startup files differ from the frozen task inputs")


def treatment_skill_tree_sha256(files: list[dict[str, Any]]) -> str:
    skill_files = sorted(
        (record for record in files if record.get("role") == "openada-skill"),
        key=lambda record: record["participant_path"],
    )
    encoded = json.dumps(
        skill_files,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(
        b"openada.eval.skill-tree/v0alpha1\x00" + encoded
    ).hexdigest()


def _validate_treatment_manifest(campaign: dict[str, Any], base: Path) -> None:
    record = campaign["treatment"]["bundle_manifest"]
    relative = _safe_relative_path(
        record["path"], label="treatment.bundle_manifest.path"
    )
    target = _task_file_path(base, relative, label="treatment.bundle_manifest")
    document, actual_sha, _ = load_json(target)
    if actual_sha != record["sha256"]:
        raise EvaluationError(
            "treatment bundle manifest hash differs from the campaign identity"
        )
    validate_schema(
        document,
        "treatment-manifest-v0alpha1.schema.json",
        label="treatment bundle manifest",
    )
    files = document["participant_files"]
    paths = [item["participant_path"] for item in files]
    if len(paths) != len(set(paths)):
        raise EvaluationError("treatment manifest contains duplicate participant paths")
    roles = [item["role"] for item in files]
    for required in ("openada-cli", "openada-package", "openada-schema", "openada-skill"):
        if required not in roles:
            raise EvaluationError(f"treatment manifest lacks the required {required} role")
    cli = [item for item in files if item["role"] == "openada-cli"]
    if len(cli) != 1 or cli[0]["sha256"] != campaign["treatment"]["cli_sha256"]:
        raise EvaluationError("treatment CLI identity differs from its bundle manifest")
    if document["distribution"]["sha256"] != campaign["treatment"][
        "distribution_sha256"
    ]:
        raise EvaluationError(
            "treatment wheel identity differs from its bundle manifest"
        )
    if treatment_skill_tree_sha256(files) != campaign["treatment"][
        "skill_tree_sha256"
    ]:
        raise EvaluationError(
            "treatment skill tree identity differs from its canonical manifest records"
        )


def validate_campaign(
    campaign: dict[str, Any],
    *,
    campaign_path: Path | None = None,
    verify_task_files: bool = True,
) -> None:
    validate_schema(campaign, "campaign-v0alpha1.schema.json", label="campaign")
    metric_ids = [item["id"] for item in campaign["metrics"]]
    if len(metric_ids) != len(set(metric_ids)):
        raise EvaluationError("campaign metrics contain duplicate IDs")
    primary = [item for item in campaign["metrics"] if item["primary"]]
    if len(primary) != 1 or primary[0]["id"] != campaign["primary_metric"]:
        raise EvaluationError(
            "campaign must declare exactly one primary verified_artifact_complete metric"
        )
    for metric in campaign["metrics"]:
        expected = SUPPORTED_METRICS.get(metric["id"])
        if expected is None:
            raise EvaluationError(
                f"campaign metric {metric['id']} is not in the v0alpha1 registry"
            )
        if (metric["kind"], metric["direction"]) != expected:
            raise EvaluationError(
                f"campaign metric {metric['id']} has the wrong kind or direction"
            )
    if campaign["agent"]["harness"]["name"] != "codex" or campaign["agent"][
        "harness"
    ]["version"] != "0.144.3":
        raise EvaluationError("v0alpha1 requires the pinned Codex 0.144.3 harness")
    expected_adapter_sha = sha256_regular_file(
        HERE / "adapters" / "codex_jsonl.py", maximum_bytes=MAX_JSON_BYTES
    )
    if campaign["agent"]["adapter"] != {
        "name": "codex-jsonl",
        "version": "1",
        "sha256": expected_adapter_sha,
    }:
        raise EvaluationError("campaign adapter identity differs from the packaged reducer")
    try:
        signing_public = bytes.fromhex(campaign["trial_signing"]["public_key_hex"])
    except ValueError as exc:
        raise EvaluationError("campaign trial signing public key is invalid") from exc
    if hashlib.sha256(signing_public).hexdigest() != campaign["trial_signing"]["key_id"]:
        raise EvaluationError("campaign trial signing key ID is inconsistent")
    tool_names = [item["name"] for item in campaign["runtime"]["tools"]]
    if len(tool_names) != len(set(tool_names)):
        raise EvaluationError("campaign runtime tools contain duplicate names")
    tool_paths = [item["path"] for item in campaign["runtime"]["tools"]]
    if len(tool_paths) != len(set(tool_paths)):
        raise EvaluationError("campaign runtime tools contain duplicate paths")
    for section in ("identity_files",):
        paths = [item["path"] for item in campaign["runtime"]["pdk"][section]]
        if len(paths) != len(set(paths)):
            raise EvaluationError(f"campaign PDK {section} contains duplicate paths")
    startup_paths = [item["path"] for item in campaign["runtime"]["startup_files"]]
    if len(startup_paths) != len(set(startup_paths)):
        raise EvaluationError("campaign startup_files contains duplicate paths")
    if verify_task_files:
        if campaign_path is None:
            raise EvaluationError("campaign path is required to verify frozen task files")
        base = campaign_path.resolve().parent
        task_paths: dict[str, Path] = {}
        for key in ("manifest", "prompt", "response_schema", "scorer"):
            record = campaign["task"][key]
            relative = _safe_relative_path(record["path"], label=f"task.{key}.path")
            target = _task_file_path(base, relative, label=f"task.{key}")
            actual = sha256_regular_file(target, maximum_bytes=MAX_JSON_BYTES)
            if actual != record["sha256"]:
                raise EvaluationError(
                    f"task.{key} hash differs from the frozen campaign identity"
                )
            task_paths[key] = target
        manifest, _, _ = load_json(task_paths["manifest"])
        _validate_ihp_task_binding(campaign, manifest, task_paths)
        _validate_treatment_manifest(campaign, base)


def parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        raise EvaluationError(f"{label} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise EvaluationError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EvaluationError(f"{label} must use UTC Z")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_plan(plan: dict[str, Any], campaign: dict[str, Any], campaign_sha256: str) -> None:
    expected_keys = {
        "schema",
        "randomization_algorithm",
        "campaign_id",
        "campaign_sha256",
        "created_at",
        "seed_sha256",
        "planned_pairs",
        "assignments",
    }
    if set(plan) != expected_keys:
        raise EvaluationError("plan has missing or unexpected top-level fields")
    if plan.get("schema") != PLAN_SCHEMA:
        raise EvaluationError(f"plan.schema must be {PLAN_SCHEMA!r}")
    if plan.get("randomization_algorithm") != PLAN_RANDOMIZATION_ALGORITHM:
        raise EvaluationError(
            "plan.randomization_algorithm is not the supported deterministic algorithm"
        )
    if plan.get("campaign_id") != campaign["campaign_id"]:
        raise EvaluationError("plan campaign ID differs from the campaign")
    if plan.get("campaign_sha256") != campaign_sha256:
        raise EvaluationError("plan campaign hash differs from the exact campaign bytes")
    plan_created = parse_timestamp(plan.get("created_at"), label="plan.created_at")
    campaign_created = parse_timestamp(
        campaign["created_at"], label="campaign.created_at"
    )
    if plan_created < campaign_created:
        raise EvaluationError("plan.created_at cannot precede campaign.created_at")
    if not isinstance(plan.get("seed_sha256"), str) or SHA256_RE.fullmatch(
        plan["seed_sha256"]
    ) is None:
        raise EvaluationError("plan.seed_sha256 must be a lowercase SHA-256 digest")
    planned_pairs = campaign["planned_pairs"]
    if plan.get("planned_pairs") != planned_pairs:
        raise EvaluationError("plan.planned_pairs differs from the campaign")
    assignments = plan.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != planned_pairs * 2:
        raise EvaluationError("plan must contain exactly two assignments per planned pair")
    trial_ids: set[str] = set()
    pair_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(assignments, start=1):
        if not isinstance(item, dict) or set(item) != {
            "sequence",
            "pair_id",
            "trial_id",
            "condition",
            "pair_position",
        }:
            raise EvaluationError(f"plan assignment {index} has an invalid shape")
        if (
            isinstance(item["sequence"], bool)
            or not isinstance(item["sequence"], int)
            or item["sequence"] != index
            or isinstance(item["pair_position"], bool)
            or not isinstance(item["pair_position"], int)
            or item["pair_position"] not in (1, 2)
        ):
            raise EvaluationError(f"plan assignment {index} has invalid ordering fields")
        if item["condition"] not in ("raw", "openada"):
            raise EvaluationError(f"plan assignment {index} has an invalid condition")
        if not isinstance(item["pair_id"], str) or OPAQUE_ID_RE.fullmatch(item["pair_id"]) is None:
            raise EvaluationError(f"plan assignment {index} has an invalid opaque pair ID")
        if not isinstance(item["trial_id"], str) or OPAQUE_ID_RE.fullmatch(item["trial_id"]) is None:
            raise EvaluationError(f"plan assignment {index} has an invalid opaque trial ID")
        if item["trial_id"] in trial_ids:
            raise EvaluationError("plan contains duplicate trial IDs")
        trial_ids.add(item["trial_id"])
        if item["pair_id"] not in grouped:
            pair_order.append(item["pair_id"])
            grouped[item["pair_id"]] = []
        grouped[item["pair_id"]].append(item)
    if len(grouped) != planned_pairs:
        raise EvaluationError("plan does not contain the declared number of unique pairs")
    for pair_index, pair_id in enumerate(pair_order):
        items = sorted(grouped[pair_id], key=lambda item: item["sequence"])
        if len(items) != 2:
            raise EvaluationError(f"pair {pair_id} does not contain exactly two trials")
        if {item["condition"] for item in items} != {"raw", "openada"}:
            raise EvaluationError(f"pair {pair_id} does not contain both conditions")
        expected_sequences = {pair_index * 2 + 1, pair_index * 2 + 2}
        if {item["sequence"] for item in items} != expected_sequences:
            raise EvaluationError(f"pair {pair_id} is not an adjacent interleaved block")
        if {item["pair_position"] for item in items} != {1, 2}:
            raise EvaluationError(f"pair {pair_id} has invalid pair positions")
        if [item["pair_position"] for item in items] != [1, 2]:
            raise EvaluationError(f"pair {pair_id} has reversed pair positions")
    first_conditions = [
        grouped[pair_id][0]["condition"]
        for pair_id in pair_order
        if grouped[pair_id][0]["pair_position"] == 1
    ]
    if len(first_conditions) != planned_pairs or abs(
        first_conditions.count("raw") - first_conditions.count("openada")
    ) > 1:
        raise EvaluationError("plan first-condition order is not balanced")


def find_assignment(plan: dict[str, Any], trial_id: str) -> dict[str, Any]:
    matches = [item for item in plan["assignments"] if item["trial_id"] == trial_id]
    if len(matches) != 1:
        raise EvaluationError(f"trial ID is not one unique planned assignment: {trial_id!r}")
    return matches[0]


def file_record(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    encoded = read_regular_bytes(path, maximum_bytes=maximum_bytes)
    return {
        "path": str(path.resolve()),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def canonical_json_bytes(document: object) -> bytes:
    """Encode one JSON value with the evaluation kit's frozen canonical form."""

    try:
        return json.dumps(
            document,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationError("document is not canonicalizable JSON") from exc


def canonical_json_sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def trial_signature_payload(trial: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in trial.items() if key != "seal"}
    return TRIAL_SIGNATURE_DOMAIN + canonical_json_bytes(unsigned)


def summary_signature_payload(summary: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in summary.items() if key != "seal"}
    return SUMMARY_SIGNATURE_DOMAIN + canonical_json_bytes(unsigned)


def trial_evidence_records(
    trials: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Bind every supplied plan-bound row in deterministic sequence order."""

    planned = {item["trial_id"]: item for item in plan["assignments"]}
    records: list[dict[str, Any]] = []
    for trial in trials:
        try:
            trial_id = trial["assignment"]["trial_id"]
            assignment = planned[trial_id]
            signature = bytes.fromhex(trial["seal"]["signature_hex"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvaluationError("trial cannot be represented as signed evidence") from exc
        if trial["assignment"] != assignment:
            raise EvaluationError(
                "trial evidence assignment differs from the frozen plan"
            )
        if len(signature) != 64:
            raise EvaluationError("trial evidence signature has an invalid length")
        records.append(
            {
                "sequence": assignment["sequence"],
                "trial_id": trial_id,
                "trial_sha256": canonical_json_sha256(trial),
                "signature_sha256": hashlib.sha256(signature).hexdigest(),
            }
        )
    return sorted(
        records,
        key=lambda item: (
            item["sequence"],
            item["trial_sha256"],
            item["signature_sha256"],
        ),
    )


def load_trial_signing_seed(path: Path) -> bytes:
    expanded = path.expanduser().absolute()
    try:
        metadata = expanded.lstat()
    except OSError as exc:
        raise EvaluationError("cannot stat the trial signing key") from exc
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise EvaluationError("trial signing key must be owner-only")
    payload = read_regular_bytes(expanded, maximum_bytes=65)
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    try:
        seed = bytes.fromhex(payload.decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise EvaluationError("trial signing key must be 64 lowercase hex characters") from exc
    if len(payload) != 64 or payload.decode("ascii") != payload.decode("ascii").lower() or len(seed) != 32:
        raise EvaluationError("trial signing key must be 64 lowercase hex characters")
    return seed


def seal_trial(
    trial: dict[str, Any], campaign: dict[str, Any], *, private_seed: bytes
) -> None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise EvaluationError("Ed25519 support requires the conformance dependencies") from exc
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_seed)
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    except ValueError as exc:
        raise EvaluationError("trial signing key is invalid") from exc
    if public_bytes.hex() != campaign["trial_signing"]["public_key_hex"]:
        raise EvaluationError("trial signing key differs from the campaign public key")
    signature = private_key.sign(trial_signature_payload(trial))
    trial["seal"] = {
        "algorithm": "ed25519",
        "key_id": campaign["trial_signing"]["key_id"],
        "signature_hex": signature.hex(),
    }


def verify_trial_seal(trial: dict[str, Any], campaign: dict[str, Any]) -> None:
    seal = trial.get("seal")
    if not isinstance(seal, dict) or seal.get("algorithm") != "ed25519":
        raise EvaluationError("trial lacks the required Ed25519 seal")
    if seal.get("key_id") != campaign["trial_signing"]["key_id"]:
        raise EvaluationError("trial seal key differs from the campaign")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(campaign["trial_signing"]["public_key_hex"])
        )
        signature = bytes.fromhex(seal["signature_hex"])
        public_key.verify(signature, trial_signature_payload(trial))
    except ImportError as exc:
        raise EvaluationError("Ed25519 support requires the conformance dependencies") from exc
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise EvaluationError("trial seal verification failed") from exc


def seal_summary(
    summary: dict[str, Any], campaign: dict[str, Any], *, private_seed: bytes
) -> None:
    """Seal a summary under the campaign key and summary-specific domain."""

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise EvaluationError("Ed25519 support requires the conformance dependencies") from exc
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(private_seed)
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    except ValueError as exc:
        raise EvaluationError("summary signing key is invalid") from exc
    if public_bytes.hex() != campaign["trial_signing"]["public_key_hex"]:
        raise EvaluationError("summary signing key differs from the campaign public key")
    signature = private_key.sign(summary_signature_payload(summary))
    summary["seal"] = {
        "algorithm": "ed25519",
        "key_id": campaign["trial_signing"]["key_id"],
        "signature_hex": signature.hex(),
    }


def verify_summary_seal(summary: dict[str, Any], campaign: dict[str, Any]) -> None:
    seal = summary.get("seal")
    if not isinstance(seal, dict) or seal.get("algorithm") != "ed25519":
        raise EvaluationError("summary lacks the required Ed25519 seal")
    if seal.get("key_id") != campaign["trial_signing"]["key_id"]:
        raise EvaluationError("summary seal key differs from the campaign")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(campaign["trial_signing"]["public_key_hex"])
        )
        signature = bytes.fromhex(seal["signature_hex"])
        public_key.verify(signature, summary_signature_payload(summary))
    except ImportError as exc:
        raise EvaluationError("Ed25519 support requires the conformance dependencies") from exc
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise EvaluationError("summary seal verification failed") from exc


def emit_json(document: dict[str, Any], *, pretty: bool = False) -> None:
    encoded = (
        json.dumps(
            document,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_JSON_BYTES:
        raise EvaluationError(
            f"output JSON exceeds the {MAX_OUTPUT_JSON_BYTES}-byte bound"
        )
    sys.stdout.buffer.write(encoded)


def emit_error(exc: BaseException) -> None:
    # Evaluation inputs include restricted capture paths and schema values.
    # Detailed exceptions remain available to direct/library callers and test
    # logs, but the publishable CLI channel uses a fixed content-free failure.
    del exc
    emit_json(
        {
            "schema": "openada.eval.error/v0alpha1",
            "status": "error",
            "message": "evaluation input or invariant failed",
        }
    )


def median(values: Iterable[float | int]) -> float | int:
    ordered = sorted(values)
    if not ordered:
        raise EvaluationError("cannot compute a median for an empty metric")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    left = ordered[middle - 1]
    right = ordered[middle]
    if isinstance(left, int) and isinstance(right, int):
        if abs(left) > MAX_EXACT_SUMMARY_INTEGER or abs(right) > MAX_EXACT_SUMMARY_INTEGER:
            raise EvaluationError("integer metric exceeds the exact-summary bound")
        total = left + right
        return total // 2 if total % 2 == 0 else total / 2
    value = (left + right) / 2
    if not math.isfinite(value):
        raise EvaluationError("metric median is not finite")
    return int(value) if float(value).is_integer() else value
