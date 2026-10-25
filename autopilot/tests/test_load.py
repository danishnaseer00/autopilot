"""SPEC-30 R-10/R-11/Q-4: deterministic Locust profiles and locustfiles."""

from __future__ import annotations

import random

import pytest

from autopilot.inject.errors import ParameterError
from autopilot.inject.load import (
    LoadEndpoint,
    LoadProfile,
    build_load_profile,
    render_locustfile,
)


def test_profile_is_deterministic_for_a_seed(app) -> None:
    first = build_load_profile(app, seed=1234, duration_s=600)
    second = build_load_profile(app, seed=1234, duration_s=600)
    assert first == second


def test_profile_uses_a_seeded_but_varied_load(app) -> None:
    # R-3/R-19: 30 runs must not all use one identical user count.
    profiles = {build_load_profile(app, seed=seed, duration_s=600) for seed in range(20)}
    assert len({profile.vusers for profile in profiles}) > 1


def test_profile_values_are_within_the_declared_bounds(app) -> None:
    profile = build_load_profile(app, seed=7, duration_s=900)
    assert 10 <= profile.vusers <= 50
    assert profile.spawn_rate in {1.0, 2.0, 5.0, 10.0}
    assert profile.duration_s == 900
    assert profile.host == app.load_host
    assert profile.endpoints == tuple(
        LoadEndpoint(path=e.path, weight=e.weight, service=e.service)
        for e in app.load_endpoints
    )
    assert profile.min_rps == max(1.0, round(profile.vusers * 0.2, 1))
    assert profile.seed == 7


def test_duration_must_be_positive(app) -> None:
    with pytest.raises(ParameterError, match="duration"):
        build_load_profile(app, seed=1, duration_s=0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"vusers": 0},
        {"spawn_rate": 0},
        {"duration_s": 0},
        {"host": "frontend:3000"},
        {"endpoints": ()},
        {"min_rps": 0},
        {"seed": -1},
    ],
)
def test_invalid_profiles_fail_closed(overrides) -> None:
    endpoint = LoadEndpoint(path="/", weight=1, service="frontend")
    values = {
        "vusers": 10,
        "spawn_rate": 1.0,
        "duration_s": 60,
        "host": "http://frontend:3000",
        "endpoints": (endpoint,),
        "min_rps": 2.0,
        "seed": 1,
    }
    values.update(overrides)
    with pytest.raises(ParameterError):
        LoadProfile(**values)


def test_endpoint_validation() -> None:
    with pytest.raises(ParameterError, match="start with /"):
        LoadEndpoint(path="api", weight=1, service="backend")
    with pytest.raises(ParameterError, match="positive weight"):
        LoadEndpoint(path="/api", weight=0, service="backend")
    with pytest.raises(ParameterError, match="needs a service"):
        LoadEndpoint(path="/api", weight=1, service="")


def test_locustfile_is_deterministic_and_contains_every_task(app) -> None:
    profile = build_load_profile(app, seed=11, duration_s=600)
    first = render_locustfile(profile)
    assert first == render_locustfile(profile)
    assert "class AutopilotFaultLoad(HttpUser)" in first
    assert "from locust import HttpUser, between, task" in first
    for endpoint in profile.endpoints:
        assert f"self.client.get({endpoint.path!r})" in first
    assert "@task(3)" in first
    assert "@task(2)" in first


def test_locustfile_records_the_profile_in_the_docstring(app) -> None:
    profile = build_load_profile(app, seed=12, duration_s=600)
    text = render_locustfile(profile)
    assert f"{profile.vusers} users" in text
    assert f"{profile.duration_s}s" in text


def test_duplicate_task_names_are_disambiguated() -> None:
    endpoints = (
        LoadEndpoint(path="/api", weight=1, service="backend"),
        LoadEndpoint(path="/api", weight=2, service="backend"),
    )
    profile = LoadProfile(
        vusers=10,
        spawn_rate=1.0,
        duration_s=60,
        host="http://frontend:3000",
        endpoints=endpoints,
        min_rps=2.0,
        seed=1,
    )
    text = render_locustfile(profile)
    assert "def backend_api(self):" in text
    assert "def backend_api_2(self):" in text


def test_task_names_are_valid_python_identifiers() -> None:
    endpoint = LoadEndpoint(path="/", weight=1, service="9-service")
    profile = LoadProfile(
        vusers=10,
        spawn_rate=1.0,
        duration_s=60,
        host="http://frontend:3000",
        endpoints=(endpoint,),
        min_rps=2.0,
        seed=1,
    )
    text = render_locustfile(profile)
    assert "def request_9_service_root(self):" in text


def test_profiles_do_not_use_the_global_random_module(app) -> None:
    # Determinism would be a lie if a module-level RNG were involved.
    random.seed(999)
    first = build_load_profile(app, seed=3, duration_s=60)
    random.seed(1000)
    second = build_load_profile(app, seed=3, duration_s=60)
    assert first == second
