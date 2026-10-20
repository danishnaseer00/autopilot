"""Detection cache: key scoping and degradation (SPEC-07 R-19, SPEC-02 R-7)."""

from __future__ import annotations

import json

import pytest

from buildplane.detection import (
    ExplicitBuildConfig,
    InMemoryPlanCache,
    RedisPlanCache,
    detect_with_cache,
    detection_cache_key,
    explicit_digest,
)

ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"


def _repo(make_index, *, command: str | None = None, astro: bool = False):
    dependency = {"astro": "^4"} if astro else {"vite": "^5"}
    files = {
        "package.json": json.dumps({"name": "app", "devDependencies": dependency}),
        "src/index.js": "console.log(1)",
    }
    if command:
        files["build.sh"] = "#!/bin/sh\n"
    return make_index(files)


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.ttls[key] = ttl
        self.store[key] = value


# --------------------------------------------------------------------------
# AC-11: identical trees reuse the cached plan
# --------------------------------------------------------------------------


def test_identical_trees_hit_the_cache(make_index):
    cache = InMemoryPlanCache()
    first = detect_with_cache(_repo(make_index), ORG_A, cache)
    second = detect_with_cache(_repo(make_index), ORG_A, cache)

    assert first.plan == second.plan
    assert cache.hits == 1
    assert cache.misses == 1
    assert len(cache) == 1


def test_cache_key_is_tenant_scoped(make_index):
    cache = InMemoryPlanCache()
    detect_with_cache(_repo(make_index), ORG_A, cache)
    detect_with_cache(_repo(make_index), ORG_B, cache)

    # Same tree, different tenant: two entries, never a shared prefix.
    assert len(cache) == 2
    assert cache.hits == 0


def test_explicit_layer_changes_the_cache_key(make_index):
    cache = InMemoryPlanCache()
    detect_with_cache(_repo(make_index), ORG_A, cache)
    detect_with_cache(
        _repo(make_index), ORG_A, cache, ExplicitBuildConfig(build_command="./custom.sh")
    )

    # Same tree hash, different explicit digest (R-19's formula alone is not enough).
    assert len(cache) == 2


def test_explicit_digest_is_stable_and_order_independent():
    first = explicit_digest(
        ExplicitBuildConfig(build_command="./x.sh", output_dir="dist", node_version=20)
    )
    second = explicit_digest(
        ExplicitBuildConfig(node_version=20, output_dir="dist", build_command="./x.sh")
    )
    assert first == second
    assert explicit_digest(None) != first


def test_cached_result_carries_warnings(make_index):
    cache = InMemoryPlanCache()
    files = {
        "package.json": json.dumps({"devDependencies": {"vite": "5"}}),
        "pnpm-lock.yaml": "lock",
        "package-lock.json": "{}",
    }
    index = make_index(files)
    first = detect_with_cache(index, ORG_A, cache)
    second = detect_with_cache(index, ORG_A, cache)

    assert first.warnings
    assert second.warnings == first.warnings


# --------------------------------------------------------------------------
# Degradation: a cache is an optimisation
# --------------------------------------------------------------------------


class BrokenCache:
    def get(self, key):
        raise RuntimeError("redis down")

    def put(self, key, result, ttl_seconds):
        raise RuntimeError("redis down")


def test_broken_cache_still_produces_a_plan(make_index):
    result = detect_with_cache(_repo(make_index), ORG_A, BrokenCache())
    assert result.plan.outputKind == "static"


class WrongHashCache:
    def __init__(self):
        self.entry = None

    def get(self, key):
        return self.entry

    def put(self, key, result, ttl_seconds):
        self.entry = result


def test_a_cached_plan_with_the_wrong_hash_is_a_miss(make_index):
    cache = WrongHashCache()
    first = detect_with_cache(_repo(make_index, astro=True), ORG_A, cache)
    # Corrupt the hash, as a bad restore would.
    corrupted = first.plan.model_copy(update={"detectionHash": "0" * 64})
    cache.entry = type(first)(plan=corrupted, warnings=first.warnings)
    second = detect_with_cache(_repo(make_index, astro=True), ORG_A, cache)
    assert second.plan.detectionHash == first.plan.detectionHash


def test_redis_adapter_round_trips_and_sets_ttl(make_index):
    client = FakeRedis()
    cache = RedisPlanCache(client)
    result = detect_with_cache(_repo(make_index), ORG_A, cache, ttl_seconds=1234)

    assert len(client.store) == 1
    key = next(iter(client.store))
    assert key.startswith(f"detection:{ORG_A}:")
    assert client.ttls[key] == 1234
    assert RedisPlanCache(client).get(key) == result


def test_redis_adapter_ignores_corrupt_payloads(make_index):
    client = FakeRedis()
    cache = RedisPlanCache(client)
    detect_with_cache(_repo(make_index), ORG_A, cache)
    key = next(iter(client.store))
    client.store[key] = "not json"

    assert cache.get(key) is None
    plan = detect_with_cache(_repo(make_index), ORG_A, cache)
    assert plan.plan.outputKind == "static"


def test_in_memory_cache_is_bounded(make_index):
    cache = InMemoryPlanCache(max_entries=2)
    for org in ("org-a", "org-b", "org-c"):
        detect_with_cache(_repo(make_index), org, cache)
    assert len(cache) == 2


# --------------------------------------------------------------------------
# Key validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("org", ["", "a:b", "a/b", "../x", "x" * 200, " space"])
def test_invalid_org_ids_are_rejected(org):
    with pytest.raises(ValueError):
        detection_cache_key(org, "a" * 64, "b" * 64)


def test_key_shape():
    key = detection_cache_key(ORG_A, "a" * 64, "b" * 64)
    assert key == f"detection:{ORG_A}:{'a' * 64}:{'b' * 64}"
