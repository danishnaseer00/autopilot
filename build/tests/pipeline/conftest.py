"""Shared fixtures for the SPEC-09 pipeline tests."""

from __future__ import annotations

import pytest

from support import make_claim


@pytest.fixture
def claim():
    return make_claim()
