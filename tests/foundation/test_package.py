"""Verify the installed distribution and deliberate root package boundary."""

from __future__ import annotations

import importlib
import os
import tomllib
from importlib.metadata import distribution
from pathlib import Path

from packaging.utils import canonicalize_name


def test_installed_distribution_and_public_import() -> None:
    package = importlib.import_module("pokerdemo")
    metadata = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    name = metadata["project"]["name"]
    installed = distribution(name)
    assert canonicalize_name(installed.metadata["Name"]) == canonicalize_name(name)
    assert installed.version
    assert package.__name__ == "pokerdemo"
    if os.environ.get("TEST_INSTALLED_WHEEL") == "1":
        assert "site-packages" in Path(package.__file__).resolve().parts
