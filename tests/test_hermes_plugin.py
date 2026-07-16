from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from openada import __version__


ROOT = Path(__file__).parents[1]
SKILLS_ROOT = ROOT / "skills"
ADAPTER_ROOT = ROOT / "integrations" / "hermes"
ADAPTER_MODULE = ADAPTER_ROOT / "src" / "openada_hermes_plugin" / "__init__.py"


def _adapter_module():
    spec = importlib.util.spec_from_file_location(
        "openada_hermes_plugin", ADAPTER_MODULE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePluginContext:
    def __init__(self) -> None:
        self.skills: list[dict[str, object]] = []

    def register_skill(
        self,
        name: str,
        path: Path,
        description: str = "",
        *,
        advertise: bool = False,
        category: str | None = None,
    ) -> None:
        self.skills.append(
            {
                "name": name,
                "path": path,
                "description": description,
                "advertise": advertise,
                "category": category,
            }
        )


def test_adapter_release_matches_and_pins_the_openada_runtime():
    pyproject = (ADAPTER_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    adapter_readme = (ADAPTER_ROOT / "README.md").read_text(encoding="utf-8")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)

    assert version is not None
    assert version.group(1) == __version__
    assert f'"openada=={__version__}"' in pyproject
    assert 'openada = "openada_hermes_plugin"' in pyproject
    assert "both artifacts from the same reviewed" in adapter_readme
    assert "two Python wheels from one immutable" in root_readme


def test_register_advertises_read_only_source_skills(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter_module()
    monkeypatch.setattr(adapter, "_packaged_skill_files", lambda: ())
    context = FakePluginContext()

    adapter.register(context)

    expected_names = {
        path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md")
    }
    assert {skill["name"] for skill in context.skills} == expected_names
    assert all(skill["advertise"] is True for skill in context.skills)
    assert all(skill["category"] == "openada" for skill in context.skills)
    assert all(Path(skill["path"]).is_file() for skill in context.skills)
    assert all(skill["description"] for skill in context.skills)
    assert all(
        (Path(skill["path"]).parent / "agents" / "openai.yaml").is_file()
        for skill in context.skills
    )


def test_register_fails_closed_when_skill_assets_are_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _adapter_module()
    monkeypatch.setattr(adapter, "_packaged_skill_files", lambda: ())
    monkeypatch.setattr(adapter, "_source_skill_files", lambda: ())

    with pytest.raises(RuntimeError, match="skill assets are missing"):
        adapter.register(FakePluginContext())
