"""Thin registry proxy: deny, cache, audit, then serve.

The core stores no denylist or package metadata table. It is deliberately a
single chokepoint: every package request checks the global denylist **before**
the cache, so a version added to the denylist is blocked even when an earlier
tenant already warmed the cache. The HTTP transport and upstream client are
deployment adapters; the policy ordering lives here and is unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from buildplane.supplychain.audit import (
    ACTION_REGISTRY_DENIED,
    ACTION_REGISTRY_FAILOVER,
    ACTION_REGISTRY_PULL,
    AuditLog,
    AuditRecord,
)
from buildplane.supplychain.denylist import Denylist
from buildplane.supplychain.errors import DenylistedPackage, RegistryUnavailable
from buildplane.supplychain.models import PackageRef


@dataclass(frozen=True, slots=True)
class RegistryRequest:
    ref: PackageRef
    org_id: str
    project_id: str
    build_id: str
    integrity: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryArtifact:
    ref: PackageRef
    content: bytes
    media_type: str = "application/octet-stream"
    integrity: str | None = None


class UpstreamRegistry(Protocol):
    async def fetch(self, ref: PackageRef) -> RegistryArtifact: ...


class RegistryCache(Protocol):
    async def get(self, key: str) -> RegistryArtifact | None: ...

    async def put(self, key: str, artifact: RegistryArtifact) -> None: ...


class InMemoryRegistryCache:
    def __init__(self) -> None:
        self._entries: dict[str, RegistryArtifact] = {}

    async def get(self, key: str) -> RegistryArtifact | None:
        return self._entries.get(key)

    async def put(self, key: str, artifact: RegistryArtifact) -> None:
        self._entries[key] = artifact


class StaticUpstreamRegistry:
    """Deterministic upstream used by tests and local smoke runs."""

    def __init__(
        self,
        artifacts: Mapping[tuple[str, str], RegistryArtifact] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._artifacts = dict(artifacts or {})
        self._error = error
        self.calls: list[PackageRef] = []

    async def fetch(self, ref: PackageRef) -> RegistryArtifact:
        self.calls.append(ref)
        if self._error is not None:
            raise self._error
        artifact = self._artifacts.get((ref.name, ref.version))
        if artifact is None:
            raise RegistryUnavailable(
                f"registry has no artifact for {ref.name}@{ref.version}"
            )
        return artifact


class RegistryProxy:
    def __init__(
        self,
        *,
        denylist: Denylist,
        audit_log: AuditLog,
        cache: RegistryCache,
        upstream: UpstreamRegistry,
        fallback: UpstreamRegistry | None = None,
    ) -> None:
        self.denylist = denylist
        self.audit_log = audit_log
        self.cache = cache
        self.upstream = upstream
        self.fallback = fallback

    async def fetch(self, request: RegistryRequest) -> RegistryArtifact:
        advisory = await self.denylist.advisory_for(
            request.ref.name, request.ref.version
        )
        if advisory is not None:
            await self.audit_log.write(
                self._record(
                    ACTION_REGISTRY_DENIED,
                    request,
                    extra={
                        "advisory_id": advisory.advisory_id,
                        "incident_id": advisory.incident_id,
                        "summary": advisory.summary,
                    },
                )
            )
            raise DenylistedPackage(advisory)

        key = _cache_key(request)
        cached = await self.cache.get(key)
        if cached is not None:
            await self.audit_log.write(
                self._record(
                    ACTION_REGISTRY_PULL,
                    request,
                    extra={"cache_hit": True},
                )
            )
            return cached

        artifact = await self._fetch_upstream(request)
        if artifact.ref != request.ref:
            raise RegistryUnavailable(
                "registry returned a different package version than requested",
                retryable=False,
            )
        await self.cache.put(key, artifact)
        await self.audit_log.write(
            self._record(
                ACTION_REGISTRY_PULL,
                request,
                extra={"cache_hit": False, "integrity": artifact.integrity},
            )
        )
        return artifact

    async def _fetch_upstream(self, request: RegistryRequest) -> RegistryArtifact:
        try:
            return await self.upstream.fetch(request.ref)
        except RegistryUnavailable as exc:
            if self.fallback is None:
                raise
            await self.audit_log.write(
                self._record(
                    ACTION_REGISTRY_FAILOVER,
                    request,
                    extra={
                        "reason": str(exc),
                        "fallback": type(self.fallback).__name__,
                    },
                )
            )
            return await self.fallback.fetch(request.ref)

    def _record(
        self,
        action: str,
        request: RegistryRequest,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> AuditRecord:
        metadata = {
            "package": request.ref.name,
            "version": request.ref.version,
            "project_id": request.project_id,
            "build_id": request.build_id,
            "integrity": request.integrity,
        }
        if extra:
            metadata.update(extra)
        return AuditRecord(
            action=action,
            actor_type="system",
            actor_id="registry-proxy",
            org_id=request.org_id,
            target_type="package",
            target_id=f"{request.ref.name}@{request.ref.version}",
            metadata=metadata,
        )


def _cache_key(request: RegistryRequest) -> str:
    return (
        f"{request.ref.name}@{request.ref.version}"
        f"#{request.integrity or 'no-integrity'}"
    )
