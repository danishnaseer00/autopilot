"""Shared fixtures for the build-plane tests."""

from __future__ import annotations

import json
from typing import Mapping

import pytest

from buildplane.detection import RepositoryIndex


@pytest.fixture
def make_index():
    """Build a RepositoryIndex from a `{path: text|bytes|None}` mapping."""

    def _make(files: Mapping[str, str | bytes | None], *, root_path: str = "."):
        entries = []
        for path, value in files.items():
            if isinstance(value, str):
                value = value.encode("utf-8")
            entries.append((path, value))
        return RepositoryIndex.from_entries(entries, root_path=root_path)

    return _make


@pytest.fixture
def package_json():
    """Render a package.json document."""

    def _pkg(**fields) -> str:
        document = {"name": "fixture", "version": "1.0.0"}
        document.update(fields)
        return json.dumps(document)

    return _pkg
