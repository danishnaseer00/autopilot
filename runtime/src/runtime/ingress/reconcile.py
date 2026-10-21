"""SPEC-15 polling reconciler.

The reconciler is deliberately non-incremental (SPEC-16 R-1/R-2):

* read the complete route/domain snapshot;
* render the complete desired Ingress/Certificate state in memory;
* apply it;
* delete managed objects that the render no longer contains.

A missed event is therefore harmless and a partial apply is repaired on the
next poll.  A render failure returns before the first apply, leaving the last
good objects untouched (R-8).
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from runtime.ingress.models import IngressSnapshot, ReconcileResult
from runtime.ingress.render import (
    INGRESS_MANAGED_LABEL,
    IngressConfig,
    render_ingress,
)

ObjectKey = tuple[str, str, str]


class IngressSource(Protocol):
    async def snapshot(self) -> IngressSnapshot: ...


class IngressApi(Protocol):
    """Kubernetes seam scoped to the ingress plane.

    ``list_managed`` must return only objects carrying
    ``autopilot/ingress-managed=true``.  The shared ``autopilot/managed`` label
    is deliberately not enough: deployments and services rendered by SPEC-14
    also carry it, and deleting those from the ingress reconciler would be an
    outage.
    """

    async def apply(
        self, namespace: str, objects: Sequence[Mapping[str, Any]]
    ) -> int: ...

    async def list_managed(self) -> tuple[ObjectKey, ...]: ...

    async def delete(self, kind: str, namespace: str, name: str) -> bool: ...


def object_key(obj: Mapping[str, Any]) -> ObjectKey:
    metadata = obj.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("rendered object has no metadata")
    kind = obj.get("kind")
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not all(isinstance(value, str) and value for value in (kind, namespace, name)):
        raise ValueError("rendered object needs kind, metadata.namespace and metadata.name")
    return str(kind), str(namespace), str(name)


class FakeIngressApi:
    """In-memory Kubernetes API with server-side-apply-like idempotence."""

    def __init__(self) -> None:
        self._objects: dict[ObjectKey, dict[str, Any]] = {}
        self._changes = 0
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.fail_apply_after: int | None = None
        self.fail_delete = False

    @property
    def changes(self) -> int:
        return self._changes

    def get(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        value = self._objects.get((kind, namespace, name))
        return copy.deepcopy(value) if value is not None else None

    def keys(self) -> tuple[ObjectKey, ...]:
        return tuple(sorted(self._objects))

    def seed(self, obj: Mapping[str, Any]) -> ObjectKey:
        """Insert an unrelated object (used to prove delete scoping)."""

        key = object_key(obj)
        self._objects[key] = copy.deepcopy(dict(obj))
        return key

    def by_kind(self, kind: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            copy.deepcopy(value)
            for key, value in sorted(self._objects.items())
            if key[0] == kind
        )

    async def apply(
        self, namespace: str, objects: Sequence[Mapping[str, Any]]
    ) -> int:
        names: list[str] = []
        changed = 0
        for index, obj in enumerate(objects):
            if self.fail_apply_after is not None and index >= self.fail_apply_after:
                raise RuntimeError("simulated partial apply")
            key = object_key(obj)
            if key[1] != namespace:
                raise ValueError(
                    f"object {key} was passed to namespace {namespace!r}"
                )
            names.append(key[2])
            materialised = copy.deepcopy(dict(obj))
            if self._objects.get(key) != materialised:
                self._objects[key] = materialised
                changed += 1
        self._changes += changed
        self.calls.append(("apply", namespace, tuple(names)))
        return changed

    async def list_managed(self) -> tuple[ObjectKey, ...]:
        managed: list[ObjectKey] = []
        for key, obj in self._objects.items():
            metadata = obj.get("metadata", {})
            labels = metadata.get("labels", {})
            if labels.get(INGRESS_MANAGED_LABEL) == "true":
                managed.append(key)
        return tuple(sorted(managed))

    async def delete(self, kind: str, namespace: str, name: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("simulated delete failure")
        existed = self._objects.pop((kind, namespace, name), None) is not None
        self.calls.append(("delete", namespace, (name,)))
        return existed

    def delete_all_managed(self) -> int:
        count = 0
        for key in list(self._objects):
            labels = self._objects[key].get("metadata", {}).get("labels", {})
            if labels.get(INGRESS_MANAGED_LABEL) == "true":
                del self._objects[key]
                count += 1
        return count

    def delete_kind(self, kind: str) -> int:
        keys = [key for key in self._objects if key[0] == kind]
        for key in keys:
            del self._objects[key]
        return len(keys)


class IngressReconciler:
    def __init__(
        self,
        source: IngressSource,
        api: IngressApi,
        config: IngressConfig,
        *,
        poll_interval_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._source = source
        self._api = api
        self._config = config
        self.poll_interval_seconds = poll_interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_success_at: datetime | None = None
        self.last_result: ReconcileResult | None = None

    async def run_once(self) -> ReconcileResult:
        try:
            snapshot = await self._source.snapshot()
            objects = render_ingress(snapshot, self._config)
            desired = {object_key(obj) for obj in objects}
            if len(desired) != len(objects):
                raise ValueError("render produced duplicate object keys")
        except Exception as exc:
            result = ReconcileResult(
                applied=0,
                deleted=0,
                changed=0,
                failed=True,
                error=f"{type(exc).__name__}: {exc}"[:500],
                rendered=0,
            )
            self.last_result = result
            return result

        by_namespace: dict[str, list[Mapping[str, Any]]] = {}
        for obj in objects:
            key = object_key(obj)
            by_namespace.setdefault(key[1], []).append(obj)

        applied = 0
        changed = 0
        deleted = 0
        try:
            for namespace in sorted(by_namespace):
                changed += await self._api.apply(namespace, by_namespace[namespace])
                applied += len(by_namespace[namespace])

            existing = set(await self._api.list_managed())
            for kind, namespace, name in sorted(existing - desired):
                if await self._api.delete(kind, namespace, name):
                    deleted += 1
        except Exception as exc:
            # A partial apply is deliberately not followed by deletion.  The
            # next full render repairs it; keeping the previous objects avoids
            # making an outage worse in the meantime.
            result = ReconcileResult(
                applied=applied,
                deleted=deleted,
                changed=changed,
                failed=True,
                error=f"{type(exc).__name__}: {exc}"[:500],
                rendered=len(objects),
            )
            self.last_result = result
            return result

        result = ReconcileResult(
            applied=applied,
            deleted=deleted,
            changed=changed,
            failed=False,
            error=None,
            rendered=len(objects),
        )
        self.last_result = result
        if not result.failed:
            self.last_success_at = self._clock()
        return result

    async def run_forever(
        self,
        *,
        stop: asyncio.Event | None = None,
        max_cycles: int | None = None,
    ) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            await self.run_once()
            cycles += 1
            if stop is not None and stop.is_set():
                break
            if max_cycles is not None and cycles >= max_cycles:
                break
            await asyncio.sleep(self.poll_interval_seconds)
        return cycles


__all__ = [
    "FakeIngressApi",
    "IngressApi",
    "IngressReconciler",
    "IngressSource",
    "ObjectKey",
    "object_key",
]
