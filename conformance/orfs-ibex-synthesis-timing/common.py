"""Shared closed-input helpers for the ORFS Ibex synthesis/timing chain."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable


MANIFEST_SCHEMA = "openada.conformance/v0alpha1"
RESULT_SCHEMA = "openada.result/v0alpha1"
CHAIN_ID = "openada.chain/orfs-ibex-synthesis-timing/v1"
CONFORMANCE_ID = "orfs-ibex-synthesis-timing"
DESIGN_REPOSITORY = "https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git"
DESIGN_REVISION = "bea7dcd7be7f26d1328f6058b01cf42bf4352aa2"
DESIGN_TREE = "b0979ac02253b74d4e22b426d2e4827b1fbf5fc3"
UPSTREAM_REVISION = "77d801001554cce8fe69e742e96539eecbe74425"
IMAGE_REFERENCE = (
    "hpretl/iic-osic-tools@"
    "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0"
)
IMAGE_CONFIG_DIGEST = (
    "sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb"
)
YOSYS_PATH = "/foss/tools/bin/yosys"
YOSYS_VERSION = (
    "Yosys 0.66 (git sha1 86f2ddebc-dirty, g++ 13.3.0-6ubuntu2~24.04.1 "
    "-fPIC -O3)"
)
ABC_EXECUTABLE_PATH = "/foss/tools/yosys/bin/yosys-abc"
ABC_EXECUTABLE_VERSION = "UC Berkeley, ABC 1.01 (compiled Jun 22 2026 11:38:08)"
ABC_EXECUTABLE_BYTES = 21104032
ABC_EXECUTABLE_SHA256 = "8841cec163543e372becfd08940a4c5d03dc836cd1d3257d53525372d7f2b194"
OPENSTA_PATH = "/foss/tools/openroad/bin/sta"
OPENSTA_VERSION = "3.1.0"
OPENSTA_BANNER = "OpenSTA 3.1.0 244797f162"
LIBERTY_PATH = "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"
TECHMAP_PATH = "flow/platforms/nangate45/cells_latch.v"
SDC_PATH = "flow/designs/nangate45/ibex/constraint.sdc"
CONFIG_PATH = "flow/designs/nangate45/ibex/config.mk"
PLATFORM_CONFIG_PATH = "flow/platforms/nangate45/config.mk"
INCLUDE_PATH = "flow/designs/src/ibex_sv/vendor/lowrisc_ip/prim/rtl"
ABC_REPOSITORY_PATH = "conformance/orfs-ibex-synthesis-timing/abc.constr"
ABC_SHA256 = "51bf1a4f73a383e038c95dc3eba81fdd33811736285d77e7aaeabf9bd83c69a1"
ABC_BYTES = 39


class ConformanceError(RuntimeError):
    """A closed conformance precondition or evidence assertion failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value!r}")
            ),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConformanceError(f"cannot read conformance manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConformanceError("conformance manifest root must be an object")
    validate_manifest(document)
    return document


def _expect(actual: Any, expected: Any, location: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{location}: expected {expected!r}, got {actual!r}")


def validate_manifest(manifest: dict[str, Any]) -> None:
    errors: list[str] = []
    _expect(manifest.get("schema"), MANIFEST_SCHEMA, "schema", errors)
    _expect(manifest.get("id"), CONFORMANCE_ID, "id", errors)
    design = manifest.get("design") if isinstance(manifest.get("design"), dict) else {}
    _expect(design.get("repository"), DESIGN_REPOSITORY, "design.repository", errors)
    _expect(design.get("revision"), DESIGN_REVISION, "design.revision", errors)
    _expect(design.get("tree"), DESIGN_TREE, "design.tree", errors)
    _expect(design.get("subtree"), "flow/designs/src/ibex_sv", "design.subtree", errors)
    upstream = design.get("upstream") if isinstance(design.get("upstream"), dict) else {}
    _expect(upstream.get("repository"), "https://github.com/lowRISC/ibex.git", "design.upstream.repository", errors)
    _expect(upstream.get("revision"), UPSTREAM_REVISION, "design.upstream.revision", errors)
    _expect(upstream.get("attestation_path"), "flow/designs/src/ibex_sv/README.md", "design.upstream.attestation_path", errors)
    _expect(upstream.get("attestation_sha256"), "c8ca1237111cc4c0823a807848df1e1264c2635662a75240e3883900686a1cb1", "design.upstream.attestation_sha256", errors)
    _expect(
        design.get("license"),
        {
            "spdx": "Apache-2.0",
            "path": "flow/designs/src/ibex_sv/LICENSE",
            "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        },
        "design.license",
        errors,
    )

    _expect(
        manifest.get("technology"),
        {
            "name": "NangateOpenCellLibrary",
            "version": "PDKv1.3_v2010_12.Apache.CCL",
            "liberty_path": LIBERTY_PATH,
            "latch_techmap_path": TECHMAP_PATH,
            "license": {
                "spdx": "Apache-2.0",
                "path": "flow/platforms/nangate45/LICENSE",
                "sha256": "0d542e0c8804e39aa7f37eb00da5a762149dc682d7829451287e11b938e94594",
            },
        },
        "technology",
        errors,
    )
    _expect(
        manifest.get("repository_license"),
        {
            "spdx": "BSD-3-Clause",
            "path": "LICENSE_BUILD_RUN_SCRIPTS",
            "sha256": "fae0db7a4c00c3125f037e5818e8cd0c8aa5c67fff2e8c558312068e9f2d1592",
        },
        "repository_license",
        errors,
    )

    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    image = runtime.get("image") if isinstance(runtime.get("image"), dict) else {}
    _expect(image.get("name"), "hpretl/iic-osic-tools", "runtime.image.name", errors)
    _expect(image.get("tag"), "2026.06", "runtime.image.tag", errors)
    _expect(
        image.get("manifest_digest"),
        "sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0",
        "runtime.image.manifest_digest",
        errors,
    )
    _expect(image.get("reference"), IMAGE_REFERENCE, "runtime.image.reference", errors)
    _expect(image.get("config_digest"), IMAGE_CONFIG_DIGEST, "runtime.image.config_digest", errors)
    _expect(image.get("platform"), "linux/amd64", "runtime.image.platform", errors)
    tools = runtime.get("tools") if isinstance(runtime.get("tools"), dict) else {}
    _expect(
        tools.get("yosys"),
        {"path": YOSYS_PATH, "version": YOSYS_VERSION},
        "runtime.tools.yosys",
        errors,
    )
    _expect(
        tools.get("abc"),
        {
            "path": ABC_EXECUTABLE_PATH,
            "version": ABC_EXECUTABLE_VERSION,
            "bytes": ABC_EXECUTABLE_BYTES,
            "sha256": ABC_EXECUTABLE_SHA256,
        },
        "runtime.tools.abc",
        errors,
    )
    _expect(
        tools.get("opensta"),
        {"path": OPENSTA_PATH, "version": OPENSTA_VERSION, "banner": OPENSTA_BANNER},
        "runtime.tools.opensta",
        errors,
    )

    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    _expect(policy.get("setup_network"), "allowed for the pinned Git fetch and image pull only", "policy.setup_network", errors)
    _expect(policy.get("eda_network"), "none", "policy.eda_network", errors)
    _expect(policy.get("openada_mount"), "read-only", "policy.openada_mount", errors)
    _expect(policy.get("design_mount"), "read-only", "policy.design_mount", errors)
    _expect(policy.get("evidence_directory"), "new and writable", "policy.evidence_directory", errors)
    _expect(
        policy.get("analysis_scope"),
        "single-corner synthesis-stage timing with ideal interconnect and no SPEF; not signoff",
        "policy.analysis_scope",
        errors,
    )

    records = manifest.get("pinned_files")
    if not isinstance(records, list) or len(records) != 32:
        errors.append("pinned_files must contain exactly 32 reviewed records")
        records = []
    paths = [record.get("path") for record in records if isinstance(record, dict)]
    if len(paths) != len(set(paths)):
        errors.append("pinned_files paths must be unique")
    roles = {record.get("role") for record in records if isinstance(record, dict)}
    required_roles = {
        "rtl-source", "rtl-include", "technology-liberty", "synthesis-techmap",
        "timing-sdc", "flow-configuration", "platform-configuration", "upstream-attestation",
        "design-license", "technology-license", "repository-license",
    }
    if not required_roles.issubset(roles):
        errors.append("pinned_files does not cover every required engineering/provenance role")
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256", "role"}:
            errors.append(f"pinned_files[{index}] is not a closed file record")
            continue
        if (
            not isinstance(record["path"], str)
            or not record["path"]
            or Path(record["path"]).is_absolute()
            or ".." in Path(record["path"]).parts
        ):
            errors.append(f"pinned_files[{index}].path is unsafe")
        if not isinstance(record["bytes"], int) or isinstance(record["bytes"], bool) or record["bytes"] <= 0:
            errors.append(f"pinned_files[{index}].bytes is invalid")
        if not isinstance(record["sha256"], str) or len(record["sha256"]) != 64:
            errors.append(f"pinned_files[{index}].sha256 is invalid")

    derived = manifest.get("derived_inputs")
    expected_derived = {
        "repository_path": ABC_REPOSITORY_PATH,
        "bytes": ABC_BYTES,
        "sha256": ABC_SHA256,
        "role": "synthesis-abc-constraint",
        "derivation": (
            "Reviewed projection of ABC_DRIVER_CELL=BUF_X1 and ABC_LOAD_IN_FF=3.898 "
            "from pinned flow/platforms/nangate45/config.mk."
        ),
    }
    _expect(derived, [expected_derived], "derived_inputs", errors)

    operations = manifest.get("operations") if isinstance(manifest.get("operations"), dict) else {}
    if set(operations) != {"synthesize", "timing_analyze", "missing_top"}:
        errors.append("operations must contain exactly synthesize, timing_analyze, and missing_top")
    synth = operations.get("synthesize") if isinstance(operations.get("synthesize"), dict) else {}
    source_records = [record["path"] for record in records if record.get("role") == "rtl-source"]
    _expect(synth.get("source_paths"), source_records, "operations.synthesize.source_paths", errors)
    _expect(synth.get("top"), "ibex_core", "operations.synthesize.top", errors)
    _expect(synth.get("frontend"), "slang", "operations.synthesize.frontend", errors)
    _expect(synth.get("language"), "1800-2017", "operations.synthesize.language", errors)
    _expect(synth.get("include_directories"), [INCLUDE_PATH], "operations.synthesize.include_directories", errors)
    _expect(synth.get("liberty"), LIBERTY_PATH, "operations.synthesize.liberty", errors)
    _expect(synth.get("techmaps"), [TECHMAP_PATH], "operations.synthesize.techmaps", errors)
    _expect(synth.get("abc_constraint"), ABC_REPOSITORY_PATH, "operations.synthesize.abc_constraint", errors)
    _expect(synth.get("abc_delay_target_ns"), 2.2, "operations.synthesize.abc_delay_target_ns", errors)
    _expect(
        synth.get("dont_use"),
        ["TAPCELL_X1", "FILLCELL_X1", "AOI211_X1", "OAI211_X1"],
        "operations.synthesize.dont_use",
        errors,
    )
    _expect(synth.get("output_directory"), "/evidence/synthesis", "operations.synthesize.output_directory", errors)
    _expect(synth.get("result_filename"), "synthesis/synthesize.result.json", "operations.synthesize.result_filename", errors)
    _expect(synth.get("container_timeout_seconds"), 1500, "operations.synthesize.container_timeout_seconds", errors)
    _expect(synth.get("tool_timeout_seconds"), 1200, "operations.synthesize.tool_timeout_seconds", errors)
    _expect(
        synth.get("expect"),
        {
            "execution_status": "completed",
            "exit_code": 0,
            "engineering_status": "pass",
            "summary": "Yosys produced and independently validated a complete Liberty-mapped ASIC netlist.",
        },
        "operations.synthesize.expect",
        errors,
    )
    timing = operations.get("timing_analyze") if isinstance(operations.get("timing_analyze"), dict) else {}
    _expect(timing.get("top"), "ibex_core", "operations.timing_analyze.top", errors)
    _expect(timing.get("liberty"), LIBERTY_PATH, "operations.timing_analyze.liberty", errors)
    _expect(timing.get("sdc"), SDC_PATH, "operations.timing_analyze.sdc", errors)
    _expect(timing.get("netlist"), "/evidence/synthesis/mapped.v", "operations.timing_analyze.netlist", errors)
    _expect(timing.get("output_directory"), "/evidence/timing", "operations.timing_analyze.output_directory", errors)
    _expect(timing.get("result_filename"), "timing/timing-analyze.result.json", "operations.timing_analyze.result_filename", errors)
    _expect(timing.get("container_timeout_seconds"), 420, "operations.timing_analyze.container_timeout_seconds", errors)
    _expect(timing.get("tool_timeout_seconds"), 300, "operations.timing_analyze.tool_timeout_seconds", errors)
    _expect(
        timing.get("expect"),
        {
            "execution_status": "completed",
            "exit_code": 0,
            "cli_exit_code": 1,
            "engineering_status": "fail",
            "summary": "OpenSTA found a setup or hold timing violation for the supplied corner.",
            "setup_wns_relation": "negative",
            "hold_wns_relation": "nonnegative",
        },
        "operations.timing_analyze.expect",
        errors,
    )
    missing = operations.get("missing_top") if isinstance(operations.get("missing_top"), dict) else {}
    _expect(missing.get("top"), "missing_ibex_core", "operations.missing_top.top", errors)
    _expect(missing.get("output_directory"), "/evidence/negative", "operations.missing_top.output_directory", errors)
    _expect(missing.get("result_filename"), "negative/synthesize.result.json", "operations.missing_top.result_filename", errors)
    _expect(missing.get("container_timeout_seconds"), 420, "operations.missing_top.container_timeout_seconds", errors)
    _expect(missing.get("tool_timeout_seconds"), 300, "operations.missing_top.tool_timeout_seconds", errors)
    _expect(
        missing.get("expect"),
        {
            "execution_status": "completed",
            "cli_exit_code": 1,
            "engineering_status": "fail",
            "diagnostic_substring": "missing_ibex_core",
        },
        "operations.missing_top.expect",
        errors,
    )
    if errors:
        raise ConformanceError("invalid conformance manifest:\n- " + "\n- ".join(errors))


def canonical_inventory_sha256(manifest: dict[str, Any]) -> str:
    payload = {
        "design": manifest["design"],
        "technology": manifest["technology"],
        "repository_license": manifest["repository_license"],
        "pinned_files": manifest["pinned_files"],
        "derived_inputs": manifest["derived_inputs"],
        "operations": manifest["operations"],
    }
    encoded = json.dumps(
        payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "openada" / "conformance" / "orfs-ibex-synthesis-timing"


def run_checked(argv: Iterable[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [str(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ConformanceError(f"cannot execute {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ConformanceError(
            f"command failed with exit code {completed.returncode}: {command!r}"
            + (f": {detail[-4000:]}" if detail else "")
        )
    return completed


def _canonical_oci_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return f"sha256:{digest}"


def _repo_digest_matches(reference: str, record: dict[str, Any]) -> bool:
    repository, separator, digest = reference.rpartition("@")
    expected_digest = _canonical_oci_sha256(digest)
    if not separator or expected_digest is None:
        return False
    accepted_repositories = {repository, f"docker.io/{repository}"}
    for candidate in record.get("RepoDigests") or []:
        if not isinstance(candidate, str):
            continue
        candidate_repository, candidate_separator, candidate_digest = candidate.rpartition("@")
        if (
            candidate_separator
            and candidate_repository in accepted_repositories
            and _canonical_oci_sha256(candidate_digest) == expected_digest
        ):
            return True
    return False


def inspect_image(container_engine: str, manifest: dict[str, Any]) -> dict[str, Any]:
    reference = manifest["runtime"]["image"]["reference"]
    completed = run_checked([container_engine, "image", "inspect", reference])
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConformanceError(f"container image inspection was not valid JSON: {exc}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ConformanceError("container image inspection returned an unexpected document")
    record = records[0]
    if record.get("Os") != "linux" or record.get("Architecture") != "amd64":
        raise ConformanceError("local image platform is not the pinned linux/amd64 platform")
    config_digest = _canonical_oci_sha256(record.get("Id"))
    if config_digest != IMAGE_CONFIG_DIGEST:
        raise ConformanceError(
            f"local image config digest is {record.get('Id')!r}, expected {IMAGE_CONFIG_DIGEST!r}"
        )
    record["Id"] = config_digest
    if not _repo_digest_matches(reference, record):
        raise ConformanceError(f"local image does not record the required digest: {reference}")
    return record


def verify_design_checkout(design_dir: Path, manifest: dict[str, Any]) -> str:
    if not design_dir.is_dir() or not (design_dir / ".git").exists():
        raise ConformanceError(f"pinned design checkout is missing at {design_dir}")
    head = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD"]).stdout.strip()
    if head != DESIGN_REVISION:
        raise ConformanceError(f"design checkout is at {head}, expected {DESIGN_REVISION}")
    tree = run_checked(["git", "-C", str(design_dir), "rev-parse", "HEAD^{tree}"]).stdout.strip()
    if tree != DESIGN_TREE:
        raise ConformanceError(f"design tree is {tree}, expected {DESIGN_TREE}")
    status = run_checked(
        ["git", "-C", str(design_dir), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status:
        raise ConformanceError("design checkout has local changes; use a clean pinned checkout")
    for record in manifest["pinned_files"]:
        candidate = design_dir / record["path"]
        if not candidate.is_file() or candidate.is_symlink():
            raise ConformanceError(f"required regular design file is missing: {candidate}")
        if candidate.stat().st_size != record["bytes"]:
            raise ConformanceError(f"design input byte count mismatch for {candidate}")
        if sha256_file(candidate) != record["sha256"]:
            raise ConformanceError(f"design input hash mismatch for {candidate}")
    try:
        upstream = (design_dir / manifest["design"]["upstream"]["attestation_path"]).read_text(
            encoding="utf-8"
        )
        config = (design_dir / CONFIG_PATH).read_text(encoding="utf-8")
        sdc = (design_dir / SDC_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConformanceError(f"cannot inspect pinned design context: {exc}") from exc
    if UPSTREAM_REVISION not in upstream or "https://github.com/lowRISC/ibex" not in upstream:
        raise ConformanceError("vendored Ibex upstream attestation is absent")
    for line in (
        "export ABC_DRIVER_CELL = BUF_X1",
        "export ABC_LOAD_IN_FF = 3.898",
        "export DONT_USE_CELLS = TAPCELL_X1 FILLCELL_X1 AOI211_X1 OAI211_X1",
    ):
        platform_config = design_dir / PLATFORM_CONFIG_PATH
        platform_text = platform_config.read_text(encoding="utf-8")
        if line not in platform_text:
            raise ConformanceError(f"pinned Nangate configuration is missing {line!r}")
    for token in (
        "current_design ibex_core",
        "set clk_period 2.2",
        "set_clock_latency 0.285",
        "set clk_io_pct 0.2",
    ):
        if token not in sdc:
            raise ConformanceError(f"pinned Ibex SDC is missing {token!r}")
    return head


def verify_derived_inputs(openada_root: Path, manifest: dict[str, Any]) -> None:
    for record in manifest["derived_inputs"]:
        path = openada_root / record["repository_path"]
        if not path.is_file() or path.is_symlink():
            raise ConformanceError(f"derived chain input is missing or unsafe: {path}")
        if path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise ConformanceError(f"derived chain input identity differs: {path}")
    body = (openada_root / ABC_REPOSITORY_PATH).read_text(encoding="utf-8")
    if body != "set_driving_cell BUF_X1\nset_load 3.898\n":
        raise ConformanceError("the reviewed ABC constraint body differs")


def ensure_external_cache(cache_dir: Path, repository_root: Path) -> None:
    cache = cache_dir.expanduser().resolve()
    root = repository_root.resolve()
    if cache == root or root in cache.parents:
        raise ConformanceError("the conformance cache must be outside the OpenADA checkout")


def ensure_external_design_path(design_dir: Path, repository_root: Path, cache_dir: Path) -> Path:
    if design_dir.is_symlink():
        raise ConformanceError("the pinned design checkout path may not be a symbolic link")
    design = design_dir.expanduser().resolve()
    root = repository_root.resolve()
    cache = cache_dir.expanduser().resolve()
    if design == root or root in design.parents or design.parent != cache:
        raise ConformanceError(
            "the pinned design checkout must be the cache's OpenROAD-flow-scripts child"
        )
    return design


def require_mount_safe_path(path: Path) -> None:
    if any(character in str(path) for character in (",", "\n", "\r", "\x00")):
        raise ConformanceError(f"container bind-mount source path is unsafe: {path}")
