from __future__ import annotations

import asyncio

import pytest

from buildplane.pipeline.cache import (
    CacheAudit,
    CacheEntry,
    InMemoryBuildCache,
    build_cache_key,
    dependency_cache_key,
    hash_output_tree,
    image_digest,
    verify_cache_entry,
)
from buildplane.pipeline.errors import CacheError
from buildplane.pipeline.strategy import strategy_id
from buildplane.detection import RepositoryIndex, detect_plan
from support import builder_ref, sha256_hex


def _key(**overrides):
    values = dict(
        lockfile_hash="sha256:" + sha256_hex("b"),
        strategy_id="fallback:static",
        build_image=builder_ref(),
        manifest_hash="sha256:" + sha256_hex("c"),
        build_env_keys=("B", "A"),
    )
    values.update(overrides)
    return build_cache_key(**values)


def test_cache_key_is_order_independent_for_env_names():
    assert _key(build_env_keys=("A", "B")) == _key(build_env_keys=("B", "A"))


def test_cache_key_changes_with_each_reproducibility_input():
    key = _key()
    assert key != _key(lockfile_hash="sha256:" + sha256_hex("d"))
    assert key != _key(strategy_id="explicit:static")
    assert key != _key(manifest_hash="sha256:" + sha256_hex("e"))
    assert key != _key(build_env_keys=("A",))
    assert key != _key(
        build_image="registry.example.test/other@sha256:" + sha256_hex("b")
    )


def test_cache_key_accepts_names_only_and_pinned_images():
    with pytest.raises(ValueError, match="names only"):
        _key(build_env_keys=("API_KEY=secret",))
    with pytest.raises(ValueError, match="pinned by digest"):
        _key(build_image="registry.example.test/builder:latest")
    with pytest.raises(ValueError, match="sha256"):
        image_digest("registry.example.test/builder@sha256:abc")


def test_dependency_layer_ignores_manifest_strategy_and_env_names():
    first = dependency_cache_key(
        lockfile_hash="sha256:" + sha256_hex("f"), build_image=builder_ref()
    )
    second = dependency_cache_key(
        lockfile_hash="sha256:" + sha256_hex("f"), build_image=builder_ref()
    )
    assert first == second
    assert first != dependency_cache_key(
        lockfile_hash="sha256:" + sha256_hex("1"), build_image=builder_ref()
    )


def test_node_version_is_folded_into_strategy_id():
    index = RepositoryIndex.from_entries(
        [("index.html", b"<html></html>")]
    )
    plan = detect_plan(index).plan
    plan_18 = plan.model_copy(update={"nodeVersion": 18})
    plan_20 = plan.model_copy(update={"nodeVersion": 20})
    assert "node18" in strategy_id(plan_18)
    assert "node20" in strategy_id(plan_20)
    assert strategy_id(plan_18) != strategy_id(plan_20)


def test_output_hash_is_deterministic_and_covers_contents(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("one", encoding="utf-8")
    first = hash_output_tree(tmp_path, relative="dist")
    second = hash_output_tree(tmp_path, relative="dist")
    assert first == second
    assert first.startswith("sha256:")

    (dist / "index.html").write_text("two", encoding="utf-8")
    assert hash_output_tree(tmp_path, relative="dist") != first


def test_output_hash_rejects_paths_outside_the_workspace(tmp_path):
    with pytest.raises(CacheError, match="escapes"):
        hash_output_tree(tmp_path, relative="../outside")


def test_in_memory_cache_is_lru_and_evicts():
    async def run():
        cache = InMemoryBuildCache(max_entries=2)
        for name in ("one", "two"):
            await cache.put(
                CacheEntry(
                    key=name,
                    dependency_key="dep",
                    output_hash="sha256:" + sha256_hex(name[0]),
                )
            )
        await cache.get("one")  # make 'two' the oldest
        await cache.put(
            CacheEntry(
                key="three",
                dependency_key="dep",
                output_hash="sha256:" + sha256_hex("3"),
            )
        )
        return cache

    cache = asyncio.run(run())
    keys = {entry.key for entry in cache.entries}
    assert "two" not in keys
    assert {"one", "three"} == keys


def test_cache_audit_records_a_deliberate_mismatch():
    entry = CacheEntry(
        key="k",
        dependency_key="d",
        output_hash="sha256:" + sha256_hex("a"),
    )
    assert verify_cache_entry(entry, entry.output_hash)
    audit = CacheAudit()
    assert audit.record(entry, "sha256:" + sha256_hex("b")) is False
    assert audit.checked == 1
    assert audit.mismatches == 1
    assert audit.above_threshold
