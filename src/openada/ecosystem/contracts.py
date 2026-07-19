"""Stable schema loading and validation independent of provider execution."""

from __future__ import annotations

from copy import deepcopy
from importlib.metadata import PackageNotFoundError, distribution
import json
from pathlib import Path
import sysconfig
from typing import Any, Mapping


MAX_SCHEMA_BYTES = 4 * 1024 * 1024


class ContractError(ValueError):
    """A schema is unavailable, ambiguous, malformed, or rejects a document."""


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ContractError(f"duplicate JSON member {key!r}")
        document[key] = value
    return document


def _constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number {value!r} is not supported")


def _strict_object(path: Path) -> dict[str, Any]:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read contract {path}: {exc}") from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ContractError(f"contract {path} exceeds {MAX_SCHEMA_BYTES} bytes")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(
            text, object_pairs_hook=_duplicates, parse_constant=_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"contract {path} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ContractError(f"contract {path} is not a JSON object")
    return value


class SchemaCatalog:
    """Discover immutable built-in schemas from source or an installed wheel."""

    def __init__(self, schema_directory: str | Path | None = None) -> None:
        self._explicit = Path(schema_directory).resolve() if schema_directory else None

    def _directories(self) -> tuple[Path, ...]:
        candidates: list[Path] = []
        if self._explicit is not None:
            candidates.append(self._explicit)
        source = Path(__file__).resolve().parents[3] / "schemas"
        if source.is_dir():
            candidates.append(source)
        try:
            installed = distribution("openada")
        except PackageNotFoundError:
            installed = None
        if installed is not None:
            for entry in installed.files or ():
                value = entry.as_posix()
                if "/share/openada/schemas/" in f"/{value}" and value.endswith(
                    ".schema.json"
                ):
                    candidates.append(Path(installed.locate_file(entry)).resolve().parent)
        candidates.append(
            Path(sysconfig.get_path("data")) / "share" / "openada" / "schemas"
        )
        unique: dict[str, Path] = {}
        for path in candidates:
            if path.is_dir():
                unique[str(path.resolve())] = path.resolve()
        return tuple(unique[key] for key in sorted(unique))

    def documents(self) -> tuple[dict[str, Any], ...]:
        by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        for directory in self._directories():
            for path in sorted(directory.glob("*.schema.json")):
                document = _strict_object(path)
                schema_id = document.get("$id")
                if not isinstance(schema_id, str):
                    raise ContractError(f"schema {path} has no $id")
                canonical = json.dumps(
                    document, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                previous = by_id.get(schema_id)
                if previous is not None and previous[0] != canonical:
                    raise ContractError(f"conflicting installed schema {schema_id}")
                by_id[schema_id] = (canonical, document)
        return tuple(deepcopy(by_id[key][1]) for key in sorted(by_id))

    def schema_ids(self) -> tuple[str, ...]:
        return tuple(document["$id"] for document in self.documents())

    def by_contract_id(self, contract_id: str) -> dict[str, Any]:
        matches = []
        for document in self.documents():
            const = document.get("properties", {}).get("schema", {}).get("const")
            if const == contract_id:
                matches.append(document)
        if not matches:
            raise ContractError(f"unknown installed contract schema {contract_id!r}")
        if len(matches) != 1:
            raise ContractError(f"ambiguous installed contract schema {contract_id!r}")
        return matches[0]

    def validate(self, document: Mapping[str, Any]) -> None:
        contract_id = document.get("schema")
        if not isinstance(contract_id, str):
            raise ContractError("document has no schema identity")
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError as exc:
            raise ContractError("contract validation requires jsonschema") from exc
        schema = self.by_contract_id(contract_id)
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).iter_errors(document),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if errors:
            first = errors[0]
            location = "/".join(str(part) for part in first.absolute_path)
            raise ContractError(f"{contract_id} rejects {location or '#'}: {first.message}")
