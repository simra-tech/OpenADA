#!/usr/bin/env python3
"""Maintain a bounded, hash-consistent ASIC bootstrap identity ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any


MANIFEST_FORMAT = "openada.skill/bootstrap-manifest/v0alpha1"
CHECK_FORMAT = "openada.skill/bootstrap-manifest-check/v0alpha1"
IDENTITY_CLAIM = "structurally-declared-and-hash-consistent"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BOUND_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_DEPTH = 20
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9+_.-]{0,127}\Z")
OCI_DIGEST = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}\Z")

DELIVERABLES = (
    "synthesized-core",
    "routed-core",
    "full-chip",
    "submission-candidate",
)
REVISION_SCHEMES = ("git-sha1", "git-sha256", "content-sha256")
STAGES = (
    "project",
    "rtl",
    "function",
    "synthesis",
    "physical",
    "timing",
    "padframe",
    "drc",
    "lvs",
    "handoff",
    "submission",
)
GAP_KINDS = (
    "capability",
    "collateral",
    "compatibility",
    "evidence",
    "external-acceptance",
    "resource",
)
COLLATERAL_ROLES = {
    "pdk.revision-attestation",
    "constraints.sdc",
    "standard-cell.liberty",
    "standard-cell.lef",
    "standard-cell.gds",
    "standard-cell.cdl",
    "io.liberty",
    "io.lef",
    "io.gds",
    "io.cdl",
    "bondpad.lef",
    "bondpad.gds",
    "macro.liberty",
    "macro.lef",
    "macro.gds",
    "macro.cdl",
    "macro.verilog",
    "drc.deck",
    "lvs.deck",
    "rcx.rules",
    "antenna.deck",
    "density.deck",
    "fill.deck",
    "seal-ring.config",
    "padframe.config",
    "pdn.config",
    "pinout",
    "package.plan",
    "license.manifest",
    "submission.checklist",
    "waiver.ledger",
}

SYNTHESIS_ROLES = {
    "pdk.revision-attestation",
    "constraints.sdc",
    "standard-cell.liberty",
    "license.manifest",
}
ROUTED_ROLES = SYNTHESIS_ROLES | {
    "standard-cell.lef",
    "standard-cell.gds",
    "standard-cell.cdl",
    "drc.deck",
    "lvs.deck",
    "rcx.rules",
}
FULL_CHIP_ROLES = ROUTED_ROLES | {
    "io.liberty",
    "io.lef",
    "io.gds",
    "io.cdl",
    "bondpad.lef",
    "bondpad.gds",
    "antenna.deck",
    "density.deck",
    "fill.deck",
    "seal-ring.config",
    "padframe.config",
    "pdn.config",
    "pinout",
    "package.plan",
}
SUBMISSION_ROLES = FULL_CHIP_ROLES | {
    "submission.checklist",
    "waiver.ledger",
}

SYNTHESIS_TOOLS = {"verilator", "yosys"}
ROUTED_TOOLS = set(SYNTHESIS_TOOLS)
PDK_EXTRA_PHYSICAL_TOOLS = {
    "ihp-sg13g2": {"klayout", "magic", "netgen", "openroad", "opensta"}
}


class ManifestError(ValueError):
    pass


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ManifestError(f"non-finite JSON number {value!r}")


def _json_depth(value: Any) -> int:
    maximum = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if maximum > MAX_JSON_DEPTH:
            return maximum
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return maximum


def _canonical_directory(value: str, *, must_exist: bool) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ManifestError(f"directory path must be absolute: {value!r}")
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise ManifestError(f"cannot resolve directory {value!r}: {exc}") from exc
    if must_exist and not resolved.is_dir():
        raise ManifestError(f"not a directory: {resolved}")
    return resolved


def _canonical_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ManifestError(f"file path must be absolute: {value!r}")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ManifestError(f"cannot inspect file {value!r}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise ManifestError(f"not a regular file: {resolved}")
    return resolved


def _hash_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManifestError(f"cannot open regular file {path}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ManifestError(f"not a regular file: {path}")
        if before.st_size > MAX_BOUND_FILE_BYTES:
            raise ManifestError(
                f"file exceeds {MAX_BOUND_FILE_BYTES} byte bootstrap bound: {path}"
            )
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        first = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        second = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if first != second:
            raise ManifestError(f"file changed while hashing: {path}")
        return before.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _file_record(path: str) -> dict[str, Any]:
    resolved = _canonical_file(path)
    size, digest = _hash_file(resolved)
    return {"path": str(resolved), "bytes": size, "sha256": digest}


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ManifestError("manifest path must not be a symbolic link")
    try:
        resolved = expanded.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise ManifestError(f"cannot inspect manifest: {exc}") from exc
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is empty or exceeds the 1 MiB bound")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
        try:
            before = os.fstat(descriptor)
            raw = os.read(descriptor, MAX_MANIFEST_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    if len(raw) != size or len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest size changed or exceeds the 1 MiB bound")
    first = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    second = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if first != second:
        raise ManifestError("manifest changed while it was read")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
        )
    except ManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError(f"manifest is not bounded strict UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError("manifest must be one JSON object")
    if _json_depth(document) > MAX_JSON_DEPTH:
        raise ManifestError(f"manifest exceeds the JSON depth bound of {MAX_JSON_DEPTH}")
    return document, raw


def _expect_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ManifestError(f"{label} has missing or unexpected fields")
    return value


def _bounded_string(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ManifestError(f"{label} must be a nonempty string up to {maximum} characters")
    return value


def _safe_id(value: Any, label: str) -> str:
    text = _bounded_string(value, label, maximum=128)
    if SAFE_ID.fullmatch(text) is None:
        diagnostic = f"{label} is not a lowercase stable identifier"
        lowercase = text.lower()
        if lowercase != text and SAFE_ID.fullmatch(lowercase) is not None:
            diagnostic += f"; use {lowercase!r}"
        raise ManifestError(diagnostic)
    return text


def _revision(scheme: str, value: str) -> dict[str, str]:
    return {"scheme": scheme, "value": value}


def _revision_shape(value: Any, label: str) -> dict[str, str]:
    revision = _expect_keys(value, {"scheme", "value"}, label)
    scheme = revision["scheme"]
    digest = revision["value"]
    if scheme not in REVISION_SCHEMES:
        raise ManifestError(f"{label}.scheme is unsupported")
    pattern = HEX40 if scheme == "git-sha1" else SHA256
    if not isinstance(digest, str) or pattern.fullmatch(digest) is None:
        raise ManifestError(f"{label}.value does not match {scheme}")
    return revision


def _record_shape(value: Any, label: str) -> dict[str, Any]:
    record = _expect_keys(value, {"path", "bytes", "sha256"}, label)
    path = _bounded_string(record["path"], f"{label}.path", maximum=4096)
    if not Path(path).is_absolute() or str(Path(path).resolve(strict=False)) != path:
        raise ManifestError(f"{label}.path must be canonical and absolute")
    size = record["bytes"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ManifestError(f"{label}.bytes is invalid")
    if not 0 <= size <= MAX_BOUND_FILE_BYTES:
        raise ManifestError(f"{label}.bytes is outside the bootstrap bound")
    digest = record["sha256"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ManifestError(f"{label}.sha256 is invalid")
    return record


def _check_record_bytes(
    record: dict[str, Any], label: str, *, executable: bool = False
) -> None:
    observed = _file_record(record["path"])
    if observed != record:
        raise ManifestError(f"{label} does not match current regular-file bytes")
    if executable and not os.access(observed["path"], os.X_OK):
        raise ManifestError(f"{label} is no longer executable")


def _required_for(document: dict[str, Any]) -> tuple[set[str], set[str], list[str]]:
    deliverable = document["project"]["deliverable"]
    tools = set(SYNTHESIS_TOOLS)
    roles = set(SYNTHESIS_ROLES)
    stages = ["project", "rtl", "function", "synthesis"]
    if deliverable != "synthesized-core":
        tools |= ROUTED_TOOLS
        tools.add(document["flow"]["tool"])
        tools |= PDK_EXTRA_PHYSICAL_TOOLS.get(document["target"]["pdk_id"], set())
        roles |= ROUTED_ROLES
        stages += ["physical", "timing", "drc", "lvs", "handoff"]
    if deliverable in {"full-chip", "submission-candidate"}:
        roles |= FULL_CHIP_ROLES
        stages.insert(stages.index("drc"), "padframe")
    if deliverable == "submission-candidate":
        roles |= SUBMISSION_ROLES
        stages.append("submission")
    tools |= set(document["flow"]["requirements"]["tools"])
    roles |= set(document["flow"]["requirements"]["collateral_roles"])
    return tools, roles, stages


def _missing_freeze_requirements(document: dict[str, Any]) -> dict[str, Any]:
    required_tools, required_roles, _ = _required_for(document)
    declared_tools = set(document["runtime"]["tools"])
    declared_roles = {item["role"] for item in document["collateral"]}
    template_missing = (
        document["project"]["deliverable"]
        in {"full-chip", "submission-candidate"}
        and not all(document["project"]["template"].values())
    )
    return {
        "template": template_missing,
        "tools": sorted(required_tools - declared_tools),
        "collateral_roles": sorted(required_roles - declared_roles),
    }


def _validate_document(
    document: dict[str, Any], *, check_paths: bool, require_declared: bool
) -> None:
    root = _expect_keys(
        document,
        {
            "format",
            "lifecycle",
            "project",
            "target",
            "runtime",
            "flow",
            "collateral",
            "evidence_root",
            "gaps",
        },
        "manifest",
    )
    if root["format"] != MANIFEST_FORMAT:
        raise ManifestError(f"manifest.format must be {MANIFEST_FORMAT!r}")

    lifecycle = _expect_keys(
        root["lifecycle"], {"state", "revision", "last_change"}, "lifecycle"
    )
    if lifecycle["state"] not in {"draft", "frozen"}:
        raise ManifestError("lifecycle.state must be draft or frozen")
    number = lifecycle["revision"]
    if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 10**9:
        raise ManifestError("lifecycle.revision is invalid")
    _bounded_string(lifecycle["last_change"], "lifecycle.last_change", maximum=512)

    project = _expect_keys(
        root["project"],
        {"name", "root", "deliverable", "top", "spec", "sources", "template"},
        "project",
    )
    _bounded_string(project["name"], "project.name", maximum=128)
    project_root_text = _bounded_string(project["root"], "project.root", maximum=4096)
    project_root = _canonical_directory(project_root_text, must_exist=check_paths)
    if str(project_root) != project_root_text:
        raise ManifestError("project.root must be canonical")
    if project["deliverable"] not in DELIVERABLES:
        raise ManifestError("project.deliverable is unsupported")
    _bounded_string(project["top"], "project.top", maximum=256)
    spec = _record_shape(project["spec"], "project.spec")
    sources = _record_shape(project["sources"], "project.sources")
    if check_paths:
        _check_record_bytes(spec, "project.spec")
        _check_record_bytes(sources, "project.sources")

    template = _expect_keys(
        project["template"], {"origin", "revision", "lock"}, "project.template"
    )
    template_present = any(value is not None for value in template.values())
    if template_present:
        _bounded_string(template["origin"], "project.template.origin", maximum=2048)
        _revision_shape(template["revision"], "project.template.revision")
        lock = _record_shape(template["lock"], "project.template.lock")
        if check_paths:
            _check_record_bytes(lock, "project.template.lock")
    elif template != {"origin": None, "revision": None, "lock": None}:
        raise ManifestError("project.template must be completely present or completely null")

    target = _expect_keys(root["target"], {"pdk_id", "pdk_root", "revision"}, "target")
    _safe_id(target["pdk_id"], "target.pdk_id")
    pdk_root_text = _bounded_string(target["pdk_root"], "target.pdk_root", maximum=4096)
    pdk_root = _canonical_directory(pdk_root_text, must_exist=check_paths)
    if str(pdk_root) != pdk_root_text:
        raise ManifestError("target.pdk_root must be canonical")
    _revision_shape(target["revision"], "target.revision")

    runtime = _expect_keys(
        root["runtime"],
        {"kind", "profile", "identity", "image_reference", "image_platform", "tools"},
        "runtime",
    )
    if runtime["kind"] not in {"native", "oci", "nix"}:
        raise ManifestError("runtime.kind must be native, oci, or nix")
    if runtime["profile"] not in {"native", "iic-osic-tools"}:
        raise ManifestError("runtime.profile is not an OpenADA runtime profile")
    identity = _record_shape(runtime["identity"], "runtime.identity")
    if check_paths:
        _check_record_bytes(identity, "runtime.identity")
    if runtime["kind"] == "oci":
        reference = runtime["image_reference"]
        if not isinstance(reference, str) or OCI_DIGEST.fullmatch(reference) is None:
            raise ManifestError("OCI runtime requires an image pinned by sha256 digest")
        if runtime["image_platform"] not in {"linux/amd64", "linux/arm64"}:
            raise ManifestError("OCI runtime requires an explicit supported platform")
    elif runtime["image_reference"] is not None or runtime["image_platform"] is not None:
        raise ManifestError("non-OCI runtime must leave image identity null")
    tools = runtime["tools"]
    if not isinstance(tools, dict) or len(tools) > 64:
        raise ManifestError("runtime.tools must be a bounded object")
    for name, value in tools.items():
        _safe_id(name, f"runtime.tools key {name!r}")
        tool = _expect_keys(
            value, {"path", "declared_version", "bytes", "sha256"}, f"runtime.tools.{name}"
        )
        _bounded_string(
            tool["declared_version"], f"runtime.tools.{name}.declared_version", maximum=1024
        )
        record = {key: tool[key] for key in ("path", "bytes", "sha256")}
        _record_shape(record, f"runtime.tools.{name}")
        if check_paths:
            _check_record_bytes(record, f"runtime.tools.{name}", executable=True)

    flow = _expect_keys(
        root["flow"], {"name", "tool", "revision", "config", "requirements"}, "flow"
    )
    _safe_id(flow["name"], "flow.name")
    _safe_id(flow["tool"], "flow.tool")
    _revision_shape(flow["revision"], "flow.revision")
    config = _record_shape(flow["config"], "flow.config")
    if check_paths:
        _check_record_bytes(config, "flow.config")
    requirements = _expect_keys(
        flow["requirements"], {"tools", "collateral_roles"}, "flow.requirements"
    )
    if not isinstance(requirements["tools"], list) or len(requirements["tools"]) > 64:
        raise ManifestError("flow.requirements.tools must be a bounded array")
    required_tool_list = [
        _safe_id(value, f"flow.requirements.tools[{index}]")
        for index, value in enumerate(requirements["tools"])
    ]
    if required_tool_list != sorted(set(required_tool_list)):
        raise ManifestError("flow.requirements.tools must be sorted and unique")
    required_role_list = requirements["collateral_roles"]
    if not isinstance(required_role_list, list) or len(required_role_list) > 128:
        raise ManifestError("flow.requirements.collateral_roles must be a bounded array")
    if any(role not in COLLATERAL_ROLES for role in required_role_list):
        raise ManifestError("flow requirements contain an unknown collateral role")
    if required_role_list != sorted(set(required_role_list)):
        raise ManifestError("flow collateral requirements must be sorted and unique")

    collateral = root["collateral"]
    if not isinstance(collateral, list) or len(collateral) > 512:
        raise ManifestError("collateral must be a bounded array")
    seen_ids: set[str] = set()
    roles: set[str] = set()
    for index, value in enumerate(collateral):
        item = _expect_keys(
            value, {"id", "role", "path", "bytes", "sha256"}, f"collateral[{index}]"
        )
        stable_id = _safe_id(item["id"], f"collateral[{index}].id")
        if stable_id in seen_ids:
            raise ManifestError("collateral contains a duplicate stable ID")
        seen_ids.add(stable_id)
        role = item["role"]
        if role not in COLLATERAL_ROLES:
            raise ManifestError(f"collateral[{index}].role is not in the closed role set")
        roles.add(role)
        record = {key: item[key] for key in ("path", "bytes", "sha256")}
        _record_shape(record, f"collateral[{index}]")
        if check_paths:
            _check_record_bytes(record, f"collateral[{index}]")
    if [item["id"] for item in collateral] != sorted(seen_ids):
        raise ManifestError("collateral must be sorted by stable ID")

    evidence_text = _bounded_string(root["evidence_root"], "evidence_root", maximum=4096)
    evidence_root = _canonical_directory(evidence_text, must_exist=False)
    if str(evidence_root) != evidence_text:
        raise ManifestError("evidence_root must be canonical")
    if check_paths and evidence_root.exists() and not evidence_root.is_dir():
        raise ManifestError("evidence_root exists but is not a directory")

    gaps = root["gaps"]
    if not isinstance(gaps, list) or len(gaps) > 128:
        raise ManifestError("gaps must be a bounded array")
    gap_ids: set[str] = set()
    for index, value in enumerate(gaps):
        gap = _expect_keys(
            value,
            {"id", "stage", "kind", "status", "detail", "resolution"},
            f"gaps[{index}]",
        )
        gap_id = _safe_id(gap["id"], f"gaps[{index}].id")
        if gap_id in gap_ids:
            raise ManifestError("gaps contains a duplicate stable ID")
        gap_ids.add(gap_id)
        if gap["stage"] not in STAGES or gap["kind"] not in GAP_KINDS:
            raise ManifestError(f"gaps[{index}] has an unsupported stage or kind")
        _bounded_string(gap["detail"], f"gaps[{index}].detail", maximum=1024)
        if gap["status"] == "open":
            if gap["resolution"] is not None:
                raise ManifestError("an open gap must have null resolution")
        elif gap["status"] == "resolved":
            _bounded_string(gap["resolution"], f"gaps[{index}].resolution", maximum=1024)
        else:
            raise ManifestError("gap status must be open or resolved")
    if [gap["id"] for gap in gaps] != sorted(gap_ids):
        raise ManifestError("gaps must be sorted by stable ID")

    if require_declared:
        missing = _missing_freeze_requirements(document)
        if missing["template"]:
            raise ManifestError("full-chip deliverables require a frozen template origin/revision/lock")
        missing_tools = missing["tools"]
        if missing_tools:
            raise ManifestError(
                f"declared runtime is missing tool identities: {', '.join(missing_tools)}"
            )
        missing_roles = missing["collateral_roles"]
        if missing_roles:
            raise ManifestError(
                f"declared project is missing collateral roles: {', '.join(missing_roles)}"
            )


def _encoded(document: dict[str, Any]) -> bytes:
    payload = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError("encoded manifest exceeds the 1 MiB bound")
    return payload


def _create_new(path: Path, document: dict[str, Any]) -> None:
    parent = path.expanduser().parent.resolve(strict=False)
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / path.name
    payload = _encoded(document)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise ManifestError(f"cannot create new manifest: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _replace_existing(
    path: Path, document: dict[str, Any], *, expected_raw: bytes
) -> None:
    resolved = path.expanduser().resolve(strict=True)
    if path.expanduser().is_symlink():
        raise ManifestError("manifest path must not be a symbolic link")
    _, current_raw = _load_manifest(resolved)
    if current_raw != expected_raw:
        raise ManifestError("manifest changed after it was read; retry the update")
    payload = _encoded(document)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{resolved.name}.", dir=resolved.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    finally:
        if temporary.exists():
            temporary.unlink()


def _emit(outcome: str, **data: Any) -> None:
    print(json.dumps({"format": CHECK_FORMAT, "outcome": outcome, **data}, sort_keys=True))


def _ensure_draft(document: dict[str, Any]) -> None:
    if document["lifecycle"]["state"] != "draft":
        raise ManifestError("manifest is frozen; thaw it with an explicit reason before editing")


def _touch(document: dict[str, Any], action: str) -> None:
    lifecycle = document["lifecycle"]
    lifecycle["revision"] += 1
    lifecycle["last_change"] = action


def _save_mutation(
    path: Path, document: dict[str, Any], raw: bytes, *, action: str
) -> None:
    _touch(document, action)
    _validate_document(document, check_paths=False, require_declared=False)
    _replace_existing(path, document, expected_raw=raw)


def _template_from_args(args: argparse.Namespace) -> dict[str, Any]:
    values = (args.template_origin, args.template_revision_scheme, args.template_revision, args.template_lock)
    if not any(value is not None for value in values):
        return {"origin": None, "revision": None, "lock": None}
    if not all(value is not None for value in values):
        raise ManifestError("template origin, revision scheme/value, and lock must be supplied together")
    return {
        "origin": args.template_origin,
        "revision": _revision(args.template_revision_scheme, args.template_revision),
        "lock": _file_record(args.template_lock),
    }


def _runtime_from_args(args: argparse.Namespace, *, tools: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": args.runtime_kind,
        "profile": args.runtime_profile,
        "identity": _file_record(args.runtime_identity),
        "image_reference": args.image_reference,
        "image_platform": args.image_platform,
        "tools": tools,
    }


def _command_init(args: argparse.Namespace) -> int:
    project_root = _canonical_directory(args.project_root, must_exist=True)
    pdk_root = _canonical_directory(args.pdk_root, must_exist=True)
    evidence_root = _canonical_directory(args.evidence_root, must_exist=False)
    document = {
        "format": MANIFEST_FORMAT,
        "lifecycle": {"state": "draft", "revision": 0, "last_change": "init"},
        "project": {
            "name": args.name,
            "root": str(project_root),
            "deliverable": args.deliverable,
            "top": args.top,
            "spec": _file_record(args.project_spec),
            "sources": _file_record(args.source_manifest),
            "template": _template_from_args(args),
        },
        "target": {
            "pdk_id": args.pdk_id,
            "pdk_root": str(pdk_root),
            "revision": _revision(args.pdk_revision_scheme, args.pdk_revision),
        },
        "runtime": _runtime_from_args(args, tools={}),
        "flow": {
            "name": args.flow_name,
            "tool": args.flow_tool,
            "revision": _revision(args.flow_revision_scheme, args.flow_revision),
            "config": _file_record(args.flow_config),
            "requirements": {
                "tools": sorted(set(args.require_tool)),
                "collateral_roles": sorted(set(args.require_role)),
            },
        },
        "collateral": [],
        "evidence_root": str(evidence_root),
        "gaps": [],
    }
    _validate_document(document, check_paths=True, require_declared=False)
    _create_new(args.output, document)
    _emit(
        "valid",
        operation="init",
        claim="identity-ledger-created-draft",
        manifest=str(args.output.expanduser().resolve(strict=True)),
    )
    return 0


def _command_bind_file(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    if any(item["id"] == args.id for item in document["collateral"]):
        raise ManifestError("collateral ID already exists; use replace-file")
    document["collateral"].append(
        {"id": args.id, "role": args.role, **_file_record(args.path)}
    )
    document["collateral"].sort(key=lambda item: item["id"])
    _save_mutation(args.manifest, document, raw, action=f"bind-file:{args.id}")
    _emit("valid", operation="bind-file", id=args.id, role=args.role)
    return 0


def _command_replace_file(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    matches = [item for item in document["collateral"] if item["id"] == args.id]
    if len(matches) != 1:
        raise ManifestError("collateral ID does not exist")
    replacement = {"id": args.id, "role": args.role, **_file_record(args.path)}
    document["collateral"] = [
        replacement if item["id"] == args.id else item for item in document["collateral"]
    ]
    _save_mutation(args.manifest, document, raw, action=f"replace-file:{args.id}")
    _emit("valid", operation="replace-file", id=args.id, role=args.role)
    return 0


def _command_remove_file(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    retained = [item for item in document["collateral"] if item["id"] != args.id]
    if len(retained) == len(document["collateral"]):
        raise ManifestError("collateral ID does not exist")
    document["collateral"] = retained
    _save_mutation(args.manifest, document, raw, action=f"remove-file:{args.id}")
    _emit("valid", operation="remove-file", id=args.id)
    return 0


def _command_set_tool(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    path = _canonical_file(args.path)
    if not os.access(path, os.X_OK):
        raise ManifestError(f"tool is not executable: {path}")
    record = _file_record(str(path))
    document["runtime"]["tools"][args.name] = {
        "path": record["path"],
        "declared_version": args.version,
        "bytes": record["bytes"],
        "sha256": record["sha256"],
    }
    document["runtime"]["tools"] = dict(sorted(document["runtime"]["tools"].items()))
    _save_mutation(args.manifest, document, raw, action=f"set-tool:{args.name}")
    _emit("valid", operation="set-tool", name=args.name, claim="caller-declared-tool-version")
    return 0


def _command_remove_tool(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    if args.name not in document["runtime"]["tools"]:
        raise ManifestError("tool identity does not exist")
    del document["runtime"]["tools"][args.name]
    _save_mutation(args.manifest, document, raw, action=f"remove-tool:{args.name}")
    _emit("valid", operation="remove-tool", name=args.name)
    return 0


def _command_set_flow(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    document["flow"].update(
        {
            "name": args.name,
            "tool": args.tool,
            "revision": _revision(args.revision_scheme, args.revision),
            "config": _file_record(args.config),
            "requirements": {
                "tools": sorted(set(args.require_tool)),
                "collateral_roles": sorted(set(args.require_role)),
            },
        }
    )
    _save_mutation(args.manifest, document, raw, action="set-flow")
    _emit("valid", operation="set-flow")
    return 0


def _command_set_project(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    document["project"].update(
        {
            "deliverable": args.deliverable,
            "top": args.top,
            "spec": _file_record(args.project_spec),
            "sources": _file_record(args.source_manifest),
        }
    )
    _save_mutation(args.manifest, document, raw, action="set-project")
    _emit("valid", operation="set-project")
    return 0


def _command_set_template(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    if args.clear:
        template = {"origin": None, "revision": None, "lock": None}
    else:
        required = (args.origin, args.revision_scheme, args.revision, args.lock)
        if any(value is None for value in required):
            raise ManifestError(
                "set-template requires origin, revision scheme/value, and lock unless --clear is used"
            )
        template = {
            "origin": args.origin,
            "revision": _revision(args.revision_scheme, args.revision),
            "lock": _file_record(args.lock),
        }
    document["project"]["template"] = template
    _save_mutation(args.manifest, document, raw, action="set-template")
    _emit("valid", operation="set-template", cleared=args.clear)
    return 0


def _command_set_pdk(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    document["target"] = {
        "pdk_id": args.id,
        "pdk_root": str(_canonical_directory(args.root, must_exist=True)),
        "revision": _revision(args.revision_scheme, args.revision),
    }
    document["project"]["template"] = _template_from_args(args)
    document["flow"] = {
        "name": args.flow_name,
        "tool": args.flow_tool,
        "revision": _revision(args.flow_revision_scheme, args.flow_revision),
        "config": _file_record(args.flow_config),
        "requirements": {
            "tools": sorted(set(args.require_tool)),
            "collateral_roles": sorted(set(args.require_role)),
        },
    }
    document["collateral"] = []
    document["runtime"]["tools"] = {}
    _save_mutation(
        args.manifest,
        document,
        raw,
        action="set-pdk:stack-identities-cleared",
    )
    _emit(
        "valid",
        operation="set-pdk",
        claim="template-flow-collateral-and-tools-replaced-or-cleared",
    )
    return 0


def _command_set_runtime(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    document["runtime"] = _runtime_from_args(args, tools={})
    _save_mutation(
        args.manifest, document, raw, action="set-runtime:tool-identities-cleared"
    )
    _emit(
        "valid",
        operation="set-runtime",
        claim="tool-identities-cleared-for-rebinding",
    )
    return 0


def _command_set_evidence_root(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    root = _canonical_directory(args.path, must_exist=False)
    document["evidence_root"] = str(root)
    _save_mutation(args.manifest, document, raw, action="set-evidence-root")
    _emit("valid", operation="set-evidence-root", path=str(root))
    return 0


def _command_add_gap(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    if any(gap["id"] == args.id for gap in document["gaps"]):
        raise ManifestError("gap ID already exists")
    document["gaps"].append(
        {
            "id": args.id,
            "stage": args.stage,
            "kind": args.kind,
            "status": "open",
            "detail": args.detail,
            "resolution": None,
        }
    )
    document["gaps"].sort(key=lambda gap: gap["id"])
    _save_mutation(args.manifest, document, raw, action=f"add-gap:{args.id}")
    _emit("valid", operation="add-gap", id=args.id)
    return 0


def _command_resolve_gap(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    _ensure_draft(document)
    matches = [gap for gap in document["gaps"] if gap["id"] == args.id]
    if len(matches) != 1:
        raise ManifestError("gap ID does not exist")
    gap = matches[0]
    if gap["status"] != "open":
        raise ManifestError("gap is already resolved")
    gap["status"] = "resolved"
    gap["resolution"] = args.resolution
    _save_mutation(args.manifest, document, raw, action=f"resolve-gap:{args.id}")
    _emit("valid", operation="resolve-gap", id=args.id)
    return 0


def _command_freeze(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=True, require_declared=True)
    _ensure_draft(document)
    document["lifecycle"]["state"] = "frozen"
    _touch(document, "freeze")
    _validate_document(document, check_paths=True, require_declared=True)
    _replace_existing(args.manifest, document, expected_raw=raw)
    _, _, stages = _required_for(document)
    _emit(
        "valid",
        operation="freeze",
        claim=IDENTITY_CLAIM,
        lifecycle_revision=document["lifecycle"]["revision"],
        declared_stages=stages,
    )
    return 0


def _command_thaw(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    _validate_document(document, check_paths=False, require_declared=False)
    if document["lifecycle"]["state"] != "frozen":
        raise ManifestError("manifest is already draft")
    document["lifecycle"]["state"] = "draft"
    _touch(document, f"thaw:{args.reason}")
    _validate_document(document, check_paths=False, require_declared=False)
    _replace_existing(args.manifest, document, expected_raw=raw)
    _emit("valid", operation="thaw", reason=args.reason)
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    document, raw = _load_manifest(args.manifest)
    frozen = document.get("lifecycle", {}).get("state") == "frozen"
    check_paths = bool(args.check_paths or args.require_frozen or frozen)
    _validate_document(
        document, check_paths=check_paths, require_declared=args.require_frozen or frozen
    )
    if args.require_frozen and not frozen:
        raise ManifestError("manifest lifecycle state is draft, not frozen")
    _, _, stages = _required_for(document)
    missing = _missing_freeze_requirements(document)
    freeze_ready = check_paths and not any(
        (missing["template"], missing["tools"], missing["collateral_roles"])
    )
    _emit(
        "valid",
        operation="validate",
        claim=IDENTITY_CLAIM if check_paths else "structurally-declared-only",
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        lifecycle_state=document["lifecycle"]["state"],
        lifecycle_revision=document["lifecycle"]["revision"],
        paths_checked=check_paths,
        declared_stages=stages,
        collateral_count=len(document["collateral"]),
        tool_count=len(document["runtime"]["tools"]),
        freeze_ready=freeze_ready,
        missing_freeze_requirements=missing,
    )
    return 0


def _add_revision_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    option = prefix.replace("_", "-")
    parser.add_argument(f"--{option}-revision-scheme", choices=REVISION_SCHEMES, required=True)
    parser.add_argument(f"--{option}-revision", required=True)


def _add_template_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--template-origin")
    parser.add_argument("--template-revision-scheme", choices=REVISION_SCHEMES)
    parser.add_argument("--template-revision")
    parser.add_argument("--template-lock")


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-kind", choices=("native", "oci", "nix"), required=True)
    parser.add_argument(
        "--runtime-profile", choices=("native", "iic-osic-tools"), required=True
    )
    parser.add_argument("--runtime-identity", required=True)
    parser.add_argument("--image-reference")
    parser.add_argument("--image-platform")


def _add_flow_requirement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--require-tool", action="append", default=[])
    parser.add_argument(
        "--require-role", action="append", choices=sorted(COLLATERAL_ROLES), default=[]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create one new draft identity ledger.")
    init.add_argument("--output", type=Path, required=True)
    init.add_argument("--project-root", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--deliverable", choices=DELIVERABLES, required=True)
    init.add_argument("--top", required=True)
    init.add_argument("--project-spec", required=True)
    init.add_argument("--source-manifest", required=True)
    _add_template_arguments(init)
    init.add_argument(
        "--pdk-id",
        required=True,
        help="Lowercase stable ledger ID; for example, sky130a for native name sky130A.",
    )
    init.add_argument("--pdk-root", required=True)
    _add_revision_arguments(init, "pdk")
    _add_runtime_arguments(init)
    init.add_argument("--flow-name", required=True)
    init.add_argument("--flow-tool", required=True)
    _add_revision_arguments(init, "flow")
    init.add_argument("--flow-config", required=True)
    _add_flow_requirement_arguments(init)
    init.add_argument("--evidence-root", required=True)
    init.set_defaults(handler=_command_init)

    bind_file = subparsers.add_parser("bind-file", help="Bind one new stable collateral ID.")
    bind_file.add_argument("manifest", type=Path)
    bind_file.add_argument("--id", required=True)
    bind_file.add_argument("--role", choices=sorted(COLLATERAL_ROLES), required=True)
    bind_file.add_argument("--path", required=True)
    bind_file.set_defaults(handler=_command_bind_file)

    replace_file = subparsers.add_parser(
        "replace-file", help="Replace one existing collateral ID explicitly."
    )
    replace_file.add_argument("manifest", type=Path)
    replace_file.add_argument("--id", required=True)
    replace_file.add_argument("--role", choices=sorted(COLLATERAL_ROLES), required=True)
    replace_file.add_argument("--path", required=True)
    replace_file.set_defaults(handler=_command_replace_file)

    remove_file = subparsers.add_parser("remove-file", help="Remove one collateral ID.")
    remove_file.add_argument("manifest", type=Path)
    remove_file.add_argument("--id", required=True)
    remove_file.set_defaults(handler=_command_remove_file)

    set_tool = subparsers.add_parser("set-tool", help="Hash and declare one executable identity.")
    set_tool.add_argument("manifest", type=Path)
    set_tool.add_argument("--name", required=True)
    set_tool.add_argument("--path", required=True)
    set_tool.add_argument("--version", required=True)
    set_tool.set_defaults(handler=_command_set_tool)

    remove_tool = subparsers.add_parser("remove-tool", help="Remove one tool identity.")
    remove_tool.add_argument("manifest", type=Path)
    remove_tool.add_argument("--name", required=True)
    remove_tool.set_defaults(handler=_command_remove_tool)

    set_flow = subparsers.add_parser(
        "set-flow", help="Atomically replace flow identity and configuration."
    )
    set_flow.add_argument("manifest", type=Path)
    set_flow.add_argument("--name", required=True)
    set_flow.add_argument("--tool", required=True)
    set_flow.add_argument("--revision-scheme", choices=REVISION_SCHEMES, required=True)
    set_flow.add_argument("--revision", required=True)
    set_flow.add_argument("--config", required=True)
    _add_flow_requirement_arguments(set_flow)
    set_flow.set_defaults(handler=_command_set_flow)

    set_project = subparsers.add_parser(
        "set-project", help="Replace deliverable, top, specification, and source manifest."
    )
    set_project.add_argument("manifest", type=Path)
    set_project.add_argument("--deliverable", choices=DELIVERABLES, required=True)
    set_project.add_argument("--top", required=True)
    set_project.add_argument("--project-spec", required=True)
    set_project.add_argument("--source-manifest", required=True)
    set_project.set_defaults(handler=_command_set_project)

    set_template = subparsers.add_parser(
        "set-template", help="Replace or clear the full-chip template identity."
    )
    set_template.add_argument("manifest", type=Path)
    set_template.add_argument("--clear", action="store_true")
    set_template.add_argument("--origin")
    set_template.add_argument("--revision-scheme", choices=REVISION_SCHEMES)
    set_template.add_argument("--revision")
    set_template.add_argument("--lock")
    set_template.set_defaults(handler=_command_set_template)

    set_pdk = subparsers.add_parser(
        "set-pdk", help="Replace PDK identity and clear PDK-dependent collateral."
    )
    set_pdk.add_argument("manifest", type=Path)
    set_pdk.add_argument(
        "--id",
        required=True,
        help="Lowercase stable ledger ID; for example, sky130a for native name sky130A.",
    )
    set_pdk.add_argument("--root", required=True)
    set_pdk.add_argument("--revision-scheme", choices=REVISION_SCHEMES, required=True)
    set_pdk.add_argument("--revision", required=True)
    _add_template_arguments(set_pdk)
    set_pdk.add_argument("--flow-name", required=True)
    set_pdk.add_argument("--flow-tool", required=True)
    set_pdk.add_argument(
        "--flow-revision-scheme", choices=REVISION_SCHEMES, required=True
    )
    set_pdk.add_argument("--flow-revision", required=True)
    set_pdk.add_argument("--flow-config", required=True)
    _add_flow_requirement_arguments(set_pdk)
    set_pdk.set_defaults(handler=_command_set_pdk)

    set_runtime = subparsers.add_parser(
        "set-runtime", help="Replace runtime identity and clear all tool identities."
    )
    set_runtime.add_argument("manifest", type=Path)
    _add_runtime_arguments(set_runtime)
    set_runtime.set_defaults(handler=_command_set_runtime)

    set_evidence = subparsers.add_parser(
        "set-evidence-root", help="Move future evidence to another canonical root."
    )
    set_evidence.add_argument("manifest", type=Path)
    set_evidence.add_argument("--path", required=True)
    set_evidence.set_defaults(handler=_command_set_evidence_root)

    add_gap = subparsers.add_parser("add-gap", help="Add one unresolved gap by stable ID.")
    add_gap.add_argument("manifest", type=Path)
    add_gap.add_argument("--id", required=True)
    add_gap.add_argument("--stage", choices=STAGES, required=True)
    add_gap.add_argument("--kind", choices=GAP_KINDS, required=True)
    add_gap.add_argument("--detail", required=True)
    add_gap.set_defaults(handler=_command_add_gap)

    resolve_gap = subparsers.add_parser("resolve-gap", help="Resolve one retained gap.")
    resolve_gap.add_argument("manifest", type=Path)
    resolve_gap.add_argument("--id", required=True)
    resolve_gap.add_argument("--resolution", required=True)
    resolve_gap.set_defaults(handler=_command_resolve_gap)

    freeze = subparsers.add_parser(
        "freeze", help="Check declared requirements and all current file bytes, then freeze."
    )
    freeze.add_argument("manifest", type=Path)
    freeze.set_defaults(handler=_command_freeze)

    thaw = subparsers.add_parser("thaw", help="Return a frozen ledger to draft state.")
    thaw.add_argument("manifest", type=Path)
    thaw.add_argument("--reason", required=True)
    thaw.set_defaults(handler=_command_thaw)

    validate = subparsers.add_parser("validate", help="Validate structure and identity claims.")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--check-paths", action="store_true")
    validate.add_argument("--require-frozen", action="store_true")
    validate.set_defaults(handler=_command_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return int(args.handler(args))
    except ManifestError as exc:
        _emit("invalid", diagnostic=str(exc))
        return 2
    except (OSError, ValueError) as exc:
        _emit("invalid", diagnostic=f"unexpected bounded bootstrap error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
