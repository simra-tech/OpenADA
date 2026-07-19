"""JSON-first discovery and offline conformance CLI for ecosystem contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from .canonical import bind_request
from .bundles import ProviderBundleRegistry
from .conformance import ConformanceSuite, fake_backend_cases
from .contracts import SchemaCatalog
from .discovery import register_installed_validators
from .fakes import FakeProviderBackend
from .registries import OperationValidatorRegistry
from ..provider_runtime import list_operation_profiles, load_operation_profile


MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not supported")


def _document(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("input must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(encoded) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("input changed while it was read")
    finally:
        os.close(descriptor)
    result = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=_duplicate_keys,
        parse_constant=_reject_constant,
    )
    if not isinstance(result, dict):
        raise ValueError("input must contain one JSON object")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openada-ecosystem")
    commands = parser.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema", help="discover or validate contract schemas")
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    schema_commands.add_parser("list")
    validate = schema_commands.add_parser("validate")
    validate.add_argument("document")
    profile = commands.add_parser("profile", help="list or show installed profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list")
    profile_show = profile_commands.add_parser("show")
    profile_show.add_argument("identity")
    validator = commands.add_parser("validator", help="list explicitly trusted validators")
    validator_commands = validator.add_subparsers(dest="validator_command", required=True)
    validator_list = validator_commands.add_parser("list")
    validator_list.add_argument("--entry-point", action="append", default=[])
    bundle = commands.add_parser("bundle", help="validate explicit trusted bundles")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    for action in ("validate", "list"):
        bundle_action = bundle_commands.add_parser(action)
        bundle_action.add_argument("manifest", nargs="+" if action == "list" else None)
        bundle_action.add_argument("--root", action="append", required=True)
        bundle_action.add_argument("--validator-entry-point", action="append", default=[])
    for name in ("mapping", "capability", "result", "conformance"):
        contract = commands.add_parser(name, help=f"list or validate {name} documents")
        contract_commands = contract.add_subparsers(dest=f"{name}_command", required=True)
        contract_validate = contract_commands.add_parser("validate")
        contract_validate.add_argument("document")
        contract_list = contract_commands.add_parser("list")
        contract_list.add_argument("document", nargs="*")
    request = commands.add_parser("request", help="bind a canonical request")
    request_commands = request.add_subparsers(dest="request_command", required=True)
    bind = request_commands.add_parser("bind")
    bind.add_argument("document")
    request_validate = request_commands.add_parser("validate")
    request_validate.add_argument("document")
    commands.add_parser("transport", help="list generic transport revisions")
    commands.add_parser("fake-conformance", help="run public deterministic fixtures")
    return parser


_CONTRACT_IDS = {
    "mapping": "openada.driver-mapping/v0alpha1",
    "capability": "openada.capability-manifest/v0alpha1",
    "result": "openada.result/v0alpha2",
    "conformance": "openada.conformance-receipt/v0alpha1",
}


def _summary(document: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    identity = (
        document.get("id")
        or document.get("provider_id")
        or document.get("request_id")
        or document.get("capability_id")
    )
    return {
        "schema": document.get("schema"),
        "identity": identity,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _validate_contract(path: str, contract_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _document(path)
    if document.get("schema") != contract_id:
        raise ValueError(f"expected {contract_id}, got {document.get('schema')!r}")
    SchemaCatalog().validate(document)
    return document, _summary(document)


def _fake_receipt() -> dict[str, Any]:
    backend = FakeProviderBackend()
    return ConformanceSuite().run(
        receipt_id="org.example.conformance.fake",
        profile={
            "identity": "openada.operation/artifact.transform/v1alpha1",
            "revision": "v1alpha1",
            "sha256": "1" * 64,
        },
        mapping={
            "identity": "org.example.mapping.fake",
            "revision": "v1alpha1",
            "sha256": "2" * 64,
        },
        capability_id="org.example.capability.fake",
        cases=fake_backend_cases(backend),
        limitations=(
            "This is deterministic public self-attestation and proves neither an external backend nor deployment availability.",
            "Workflow review and signoff approval are outside this executable fixture.",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "schema" and arguments.schema_command == "list":
            output: Any = {"schemas": SchemaCatalog().schema_ids()}
        elif arguments.command == "schema" and arguments.schema_command == "validate":
            document = _document(arguments.document)
            SchemaCatalog().validate(document)
            output = {"valid": True, "schema": document["schema"]}
        elif arguments.command == "profile" and arguments.profile_command == "list":
            output = {
                "profiles": [
                    {
                        "identity": profile["operation"]["id"],
                        "assertion": profile["assertion"]["id"],
                        "schema": profile["schema"],
                    }
                    for profile in list_operation_profiles()
                ]
            }
        elif arguments.command == "profile" and arguments.profile_command == "show":
            output = load_operation_profile(arguments.identity)
            if output is None:
                raise ValueError(f"installed profile is unavailable: {arguments.identity}")
        elif arguments.command == "validator":
            validators = OperationValidatorRegistry()
            register_installed_validators(validators, arguments.entry_point)
            output = {
                "validators": [
                    {
                        "profile_identity": key.profile_identity,
                        "profile_revision": key.profile_revision,
                        "profile_sha256": key.profile_sha256,
                        "validator_identity": key.validator_identity,
                        "validator_revision": key.validator_revision,
                    }
                    for key in validators.keys()
                ]
            }
        elif arguments.command == "bundle":
            validators = OperationValidatorRegistry()
            register_installed_validators(validators, arguments.validator_entry_point)
            bundles = ProviderBundleRegistry(arguments.root, validators)
            manifests = (
                arguments.manifest
                if isinstance(arguments.manifest, list)
                else [arguments.manifest]
            )
            loaded = [bundles.load(manifest) for manifest in manifests]
            output = {
                "bundles": [
                    {
                        "identity": bundle.identity,
                        "version": bundle.version,
                        "sha256": bundle.manifest_sha256,
                    }
                    for bundle in loaded
                ]
            }
        elif arguments.command in _CONTRACT_IDS:
            command = getattr(arguments, f"{arguments.command}_command")
            paths = arguments.document if command == "list" else [arguments.document]
            records = [
                _validate_contract(path, _CONTRACT_IDS[arguments.command])[1]
                for path in paths
            ]
            collection = {
                "mapping": "mappings",
                "capability": "capabilities",
                "result": "results",
                "conformance": "conformance_receipts",
            }[arguments.command]
            output = {collection: records}
        elif arguments.command == "request" and arguments.request_command == "bind":
            document = bind_request(_document(arguments.document))
            SchemaCatalog().validate(document)
            output = document
        elif arguments.command == "request" and arguments.request_command == "validate":
            document, summary = _validate_contract(
                arguments.document, "openada.request/v0alpha2"
            )
            expected = bind_request(document)["canonical"]["sha256"]
            if document["canonical"]["sha256"] != expected:
                raise ValueError("canonical request digest does not match its bytes")
            output = {"requests": [summary]}
        elif arguments.command == "transport":
            output = {
                "transports": [
                    "org.openada.transport.agent-session/v1alpha1",
                    "org.openada.transport.remote-job/v1alpha1",
                    "org.openada.transport.fake/v1alpha1",
                ]
            }
        elif arguments.command == "fake-conformance":
            output = _fake_receipt()
        else:  # pragma: no cover - argparse enforces the closed grammar
            raise ValueError("unsupported command")
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
