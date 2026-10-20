"""Detection caching (SPEC-07 R-19, SPEC-02 R-7).

The cache key is `detection:<org>:<tree-hash>:<explicit-hash>`:

* `org` first, always (SPEC-02 R-7: tenant identity is part of every cache key);
* `tree-hash` is R-19's `sha256(sorted paths + package.json + Dockerfile)`;
* `explicit-hash` covers the explicit configuration layer, which changes the
  plan without changing the tree. R-19's formula alone would let one project's
  custom `build.command` be served to another project with identical files -
  a correctness bug, logged as a spec-change proposal in DECISIONS.md.

A cache is an optimisation. Any backend failure degrades to computing the plan
fresh; it never fails a build (S5, SPEC-02 §5).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from threading import Lock
from typing import Protocol

from buildplane.detection.detect import (
    DetectionResult,
    ExplicitBuildConfig,
    detect_plan,
    explicit_digest,
)
from buildplane.detection.index import RepositoryIndex
from buildplane.detection.plan import compute_detection_hash

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_IN_MEMORY_ENTRIES = 1024

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def detection_cache_key(
    org_id: str, detection_hash: str, explicit_sha: str
) -> str:
    """Build the tenant-scoped key. Rejects values that could reshape the key."""
    if not _ORG_ID_RE.match(org_id or ""):
        raise ValueError(
            "org_id must be 1-128 characters of [A-Za-z0-9._-] and start alphanumeric"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", detection_hash):
        raise ValueError("detection_hash must be a sha256 hex digest")
    if not re.fullmatch(r"[0-9a-f]{64}", explicit_sha):
        raise ValueError("explicit_sha must be a sha256 hex digest")
    return f"detection:{org_id}:{detection_hash}:{explicit_sha}"


class PlanCache(Protocol):
    def get(self, key: str) -> DetectionResult | None: ...

    def put(self, key: str, result: DetectionResult, ttl_seconds: int) -> None: ...


class InMemoryPlanCache:
    """Bounded LRU cache for one process; unit/contract tests and dev."""

    def __init__(self, max_entries: int = DEFAULT_MAX_IN_MEMORY_ENTRIES):
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> DetectionResult | None:
        with self._lock:
            payload = self._entries.get(key)
            if payload is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
        try:
            return DetectionResult.from_cache_json(payload)
        except Exception:
            # Corrupt entry: evict and behave as a miss. Never serve a partial plan.
            with self._lock:
                self._entries.pop(key, None)
            return None

    def put(self, key: str, result: DetectionResult, ttl_seconds: int) -> None:
        payload = result.to_cache_json()
        with self._lock:
            self._entries[key] = payload
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class RedisPlanCache:
    """Adapter over an injected Redis-compatible client.

    The client is injected rather than imported so the build process owns its
    connection pool (SPEC-11) and this module stays dependency-light. Any object
    with `get(key)` and `setex(key, ttl, value)` works. Use `from_url` for the
    normal path.
    """

    def __init__(self, client):
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisPlanCache":
        try:
            import redis  # noqa: PLC0415 - optional at import time
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "RedisPlanCache.from_url needs the 'redis' package; install it in the "
                "build worker image or inject a client into RedisPlanCache(client)"
            ) from exc
        return cls(redis.Redis.from_url(url, decode_responses=True))

    def get(self, key: str) -> DetectionResult | None:
        payload = self._client.get(key)
        if payload is None:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        try:
            return DetectionResult.from_cache_json(payload)
        except Exception:
            return None

    def put(self, key: str, result: DetectionResult, ttl_seconds: int) -> None:
        self._client.setex(key, ttl_seconds, result.to_cache_json())


def detect_with_cache(
    index: RepositoryIndex,
    org_id: str,
    cache: PlanCache,
    explicit: ExplicitBuildConfig | None = None,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> DetectionResult:
    """Return a cached plan when the org+tree+explicit keys all match, else detect.

    A hit is only served if the cached plan's own `detectionHash` matches the
    key's tree hash. That turns a backend key collision (or a bad restore) into
    a miss instead of a wrong build.
    """
    tree_hash = compute_detection_hash(
        index.detection_inputs(),
        index.read("package.json") if index.has_metadata("package.json") else None,
        index.read("Dockerfile") if index.has_metadata("Dockerfile") else None,
    )
    key = detection_cache_key(org_id, tree_hash, explicit_digest(explicit))

    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None and cached.plan.detectionHash == tree_hash:
        return cached

    result = detect_plan(index, explicit)
    try:
        cache.put(key, result, ttl_seconds)
    except Exception:
        pass
    return result
