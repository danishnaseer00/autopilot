"""Shared, deterministic fixtures for the SPEC-30 harness tests.

The default plan is expensive enough (690 runs) that building it once per
session is worthwhile, but every test that mutates plan structure uses
``dataclasses.replace`` and leaves this fixture untouched.
"""

from __future__ import annotations

import pytest

from autopilot.inject.partition import load_partition
from autopilot.inject.plan import build_plan
from autopilot.inject.testapps import default_app


@pytest.fixture(scope="session")
def partition():
    return load_partition()


@pytest.fixture(scope="session")
def app():
    return default_app()


@pytest.fixture(scope="session")
def plan(app, partition):
    return build_plan(app=app, partition=partition)
