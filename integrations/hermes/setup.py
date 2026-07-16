"""Build the Hermes adapter with the canonical OpenADA skills."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class BuildPyWithOpenadaSkills(build_py):
    """Copy root skill sources into the adapter wheel without duplicating them."""

    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "openada_hermes_plugin" / "skills"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(REPOSITORY_ROOT / "skills", target)


setup(cmdclass={"build_py": BuildPyWithOpenadaSkills})
