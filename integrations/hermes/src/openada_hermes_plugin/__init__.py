"""Read-only Hermes adapter for OpenADA's shipped skills.

The OpenADA CLI and semantic contracts remain the portable integration
surface. This package contains only the optional ``hermes_agent.plugins``
adapter and its immutable skill assets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _packaged_skill_files() -> tuple[Path, ...]:
    skills_root = Path(__file__).resolve().parent / "skills"
    if not skills_root.is_dir():
        return ()
    return tuple(
        sorted(skills_root.glob("*/SKILL.md"), key=lambda path: path.parent.name)
    )


def _source_skill_files() -> tuple[Path, ...]:
    """Support an editable source checkout without changing plugin behavior."""
    skills_root = Path(__file__).resolve().parents[4] / "skills"
    if not skills_root.is_dir():
        return ()
    return tuple(
        sorted(skills_root.glob("*/SKILL.md"), key=lambda path: path.parent.name)
    )


def _skill_files() -> tuple[Path, ...]:
    skill_files = _packaged_skill_files() or _source_skill_files()
    if not skill_files:
        raise RuntimeError(
            "OpenADA skill assets are missing; reinstall the complete adapter wheel"
        )
    return skill_files


def _skill_metadata(skill_file: Path) -> tuple[str, str]:
    """Read the two required scalar fields from Agent Skills frontmatter."""
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"Invalid skill frontmatter in {skill_file}")

    try:
        closing_marker = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"Unterminated skill frontmatter in {skill_file}") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:closing_marker]:
        key, separator, value = line.partition(":")
        if separator and key in {"name", "description"}:
            metadata[key] = value.strip()

    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not name or not description or name != skill_file.parent.name:
        raise ValueError(f"Invalid skill metadata in {skill_file}")
    return name, description


def register(ctx: Any) -> None:
    """Advertise every shipped OpenADA skill without registering model tools."""
    for skill_file in _skill_files():
        name, description = _skill_metadata(skill_file)
        try:
            ctx.register_skill(
                name,
                skill_file,
                description,
                advertise=True,
                category="openada",
            )
        except TypeError as exc:
            # Hermes <= 0.18 exposes the same namespaced, read-only skill
            # contract without the newer discovery metadata keywords.
            if "unexpected keyword argument" not in str(exc):
                raise
            ctx.register_skill(name, skill_file, description)
