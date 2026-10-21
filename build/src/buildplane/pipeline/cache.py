"""Build cache keys, layered entries and output verification (SPEC-09 §4).

The cache is content-addressed and deliberately boring:

* a dependency layer keyed only on the lockfile and builder image digest;
* a build layer keyed on the five inputs of R-22/R-25;
* an output hash stored with every entry so a hit can be audited (R-28).

The in-memory adapter is the reference implementation. The production object
store is SPEC-19's concern; the interfaces here are what it must satisfy.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from buildplane.pipeline.errors import CacheError

_CACHE_VERSION = b"autopilot-build-cache-v1"
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _encode_component(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _hash_fields(prefix: bytes, fields: list[str] | tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(prefix)
    digest.update(b"\x00")
    for field in fields:
        digest.update(_encode_component(field))
    return "sha256:" + digest.hexdigest()


def image_digest(build_image: str) -> str:
    """Return the pinned digest, rejecting tags (R-23)."""

    if "@" not in build_image:
        raise ValueError(
            "build_image must be pinned by digest (image@sha256:<hex>), not a tag"
        )
    digest = build_image.rsplit("@", 1)[1]
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"build_image digest is not a sha256 digest: {digest!r}")
    return digest


def build_cache_key(
    *,
    lockfile_hash: str,
    strategy_id: str,
    build_image: str,
    manifest_hash: str,
    build_env_keys: tuple[str, ...] | list[str],
) -> str:
    """R-25: sha256(lockfile + strategy + image digest + manifest + env names)."""

    digest = image_digest(build_image)
    keys = tuple(sorted(set(build_env_keys)))
    for key in keys:
        if not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(
                f"build_env_keys may contain names only, got {key!r}"
            )
    return _hash_fields(
        _CACHE_VERSION,
        [lockfile_hash, strategy_id, digest, manifest_hash, *keys],
    )


def dependency_cache_key(*, lockfile_hash: str, build_image: str) -> str:
    """R-26: the dependency layer is independent of manifest/strategy/env."""

    return _hash_fields(
        _CACHE_VERSION + b":dependencies",
        [lockfile_hash, image_digest(build_image)],
    )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    dependency_key: str
    output_hash: str
    artifact_path: str | None = None
    size_bytes: int = 0
    org_id: str | None = None
    created_at: datetime | None = None
    last_used: datetime | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.dependency_key or not self.output_hash:
            raise ValueError("cache entries need key, dependency_key and output_hash")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


class BuildCache(Protocol):
    async def get(self, key: str) -> CacheEntry | None: ...

    async def put(self, entry: CacheEntry) -> None: ...

    async def evict(self, key: str) -> None: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryBuildCache:
    """Bounded LRU cache used by tests and the single-process worker.

    ``per_org_bytes`` is a placeholder for SPEC-20's quota. Eviction is by
    ``last_used``; entries are never shared across orgs by the pipeline because
    ``org_id`` is carried with the entry for quota accounting.
    """

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        max_bytes: int = 10 * 1024 * 1024 * 1024,
        per_org_bytes: int | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.per_org_bytes = per_org_bytes
        self._clock = clock
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()

    @property
    def entries(self) -> tuple[CacheEntry, ...]:
        return tuple(self._entries.values())

    async def get(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        touched = replace(entry, last_used=self._clock())
        self._entries[key] = touched
        self._entries.move_to_end(key)
        return touched

    async def put(self, entry: CacheEntry) -> CacheEntry:
        now = self._clock()
        stored = replace(
            entry,
            created_at=entry.created_at or now,
            last_used=entry.last_used or now,
        )
        self._entries[stored.key] = stored
        self._entries.move_to_end(stored.key)
        self._evict()
        return stored

    async def evict(self, key: str) -> None:
        self._entries.pop(key, None)

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

        total = sum(entry.size_bytes for entry in self._entries.values())
        while total > self.max_bytes and self._entries:
            _, removed = self._entries.popitem(last=False)
            total -= removed.size_bytes

        if self.per_org_bytes is not None:
            by_org: dict[str, int] = {}
            for entry in self._entries.values():
                if entry.org_id is not None:
                    by_org[entry.org_id] = by_org.get(entry.org_id, 0) + entry.size_bytes
            for org_id, size in by_org.items():
                if size <= self.per_org_bytes:
                    continue
                for key, entry in tuple(self._entries.items()):
                    if entry.org_id != org_id:
                        continue
                    if size <= self.per_org_bytes:
                        break
                    self._entries.pop(key, None)
                    size -= entry.size_bytes


def hash_output_tree(
    root: str | os.PathLike[str],
    *,
    relative: str = ".",
) -> str:
    """Hash a local artifact tree deterministically.

    The artifact may be a directory produced by a static/framework build. For
    an image digest produced by SPEC-12, the publisher supplies the hash
    directly instead of calling this helper.
    """

    base = Path(root).resolve()
    if relative in ("", "."):
        target = base
    else:
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base):
            raise CacheError(f"artifact path escapes the workspace: {relative!r}")
        target = candidate
    if not target.exists() and not target.is_symlink():
        raise CacheError(f"artifact path does not exist: {relative!r}")

    digest = hashlib.sha256()
    digest.update(b"autopilot-output-v1\x00")

    def add_file(path: Path, rel: str) -> None:
        encoded = rel.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        if path.is_symlink():
            target_text = os.readlink(path)
            digest.update(b"L")
            digest.update(_encode_component(target_text))
            return
        digest.update(b"F")
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)

    if target.is_file() or target.is_symlink():
        add_file(target, target.name)
        return "sha256:" + digest.hexdigest()

    for directory, dirs, files in os.walk(target, followlinks=False):
        dirs.sort()
        files.sort()
        current = Path(directory)
        for name in dirs:
            path = current / name
            if path.is_symlink():
                resolved = path.resolve(strict=False)
                if not resolved.is_relative_to(base):
                    raise CacheError(
                        f"artifact symlink escapes the workspace: {path!r}"
                    )
        for name in files:
            path = current / name
            rel = path.relative_to(target).as_posix()
            add_file(path, rel)

    return "sha256:" + digest.hexdigest()


def verify_cache_entry(entry: CacheEntry, actual_output_hash: str) -> bool:
    """R-28 audit primitive: does the recorded hash match a rebuild?"""

    return entry.output_hash == actual_output_hash


@dataclass(slots=True)
class CacheAudit:
    """Accumulates sampled hit/mismatch counts for the cache audit."""

    checked: int = 0
    mismatches: int = 0

    def record(self, entry: CacheEntry, actual_output_hash: str) -> bool:
        self.checked += 1
        ok = verify_cache_entry(entry, actual_output_hash)
        if not ok:
            self.mismatches += 1
        return ok

    @property
    def mismatch_rate(self) -> float:
        return self.mismatches / self.checked if self.checked else 0.0

    @property
    def above_threshold(self) -> bool:
        return self.mismatch_rate > 0.01
