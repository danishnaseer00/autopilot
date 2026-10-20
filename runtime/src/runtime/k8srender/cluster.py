"""The narrow cluster boundary (SPEC-14 R-11, R-12, R-17).

Everything that mutates a cluster goes through ``ClusterAdapter``. The fake in
this module is what the local acceptance tests use; the coordinator supplies a
real adapter (``KubernetesApiAdapter``) and runs the cluster-only criteria.

The protocol is deliberately smaller than "the Kubernetes API". It has no
generic patch and no namespace delete, which is how R-12 and R-21 stay true even
if a caller is compromised.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Protocol, Sequence

from runtime.k8srender.errors import ClusterError, RolloutTimeout
from runtime.k8srender.names import MANAGED_LABEL, PROJECT_LABEL

FIELD_MANAGER = "autopilot"


class ClusterAdapter(Protocol):
    """The only cluster surface the runtime plane may use."""

    async def apply(
        self,
        namespace: str,
        objects: Sequence[Mapping[str, Any]],
        *,
        field_manager: str = FIELD_MANAGER,
    ) -> None: ...

    async def wait_rollout(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        timeout_seconds: int = 300,
    ) -> None: ...

    async def delete_pods(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        grace_seconds: int = 0,
    ) -> int: ...

    async def scale(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        replicas: int,
    ) -> None: ...

    async def delete_managed(
        self, namespace: str, *, labels: Mapping[str, str]
    ) -> int: ...

    async def apply_secret(
        self,
        namespace: str,
        *,
        name: str,
        values: Mapping[str, str],
        labels: Mapping[str, str] | None = None,
    ) -> None: ...


def _matches(labels: Mapping[str, str], selector: Mapping[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


class FakeCluster:
    """In-memory adapter for AC-6..AC-14 local tests.

    It records call order so tests can prove audit-before-execute and
    apply -> wait -> promote. It never deletes a Namespace even when labelled;
    that mirrors the real adapter's safe-teardown rule (R-12).
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.pods: dict[tuple[str, str], dict[str, str]] = {}
        self.secret_values: dict[tuple[str, str], dict[str, str]] = {}
        self.secret_labels: dict[tuple[str, str], dict[str, str]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.rollout_failures: set[tuple[str, str, str]] = set()
        self.seeded: list[dict[str, Any]] = []

    # -------------------------------------------------------------- inspection

    def get_object(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        value = self.objects.get((kind, namespace, name))
        return copy.deepcopy(value) if value is not None else None

    def object_names(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(sorted(self.objects))

    def call_names(self) -> tuple[str, ...]:
        return tuple(str(call[0]) for call in self.calls)

    def seed_object(self, obj: Mapping[str, Any]) -> None:
        meta = obj["metadata"]
        namespace = str(meta.get("namespace", ""))
        self.objects[(str(obj["kind"]), namespace, str(meta["name"]))] = copy.deepcopy(
            dict(obj)
        )
        self.seeded.append(copy.deepcopy(dict(obj)))

    def add_pod(
        self, namespace: str, name: str, labels: Mapping[str, str]
    ) -> None:
        self.pods[(namespace, name)] = dict(labels)

    # ---------------------------------------------------------------- protocol

    async def apply(
        self,
        namespace: str,
        objects: Sequence[Mapping[str, Any]],
        *,
        field_manager: str = FIELD_MANAGER,
    ) -> None:
        if field_manager != FIELD_MANAGER:
            raise ClusterError("the runtime must use the autopilot field manager")
        names: list[tuple[str, str]] = []
        for obj in objects:
            meta = obj["metadata"]
            kind = str(obj["kind"])
            obj_namespace = (
                "" if kind == "Namespace" else str(meta.get("namespace", namespace))
            )
            if kind != "Namespace" and obj_namespace != namespace:
                raise ClusterError("object is outside the target namespace")
            key = (kind, obj_namespace, str(meta["name"]))
            self.objects[key] = copy.deepcopy(dict(obj))
            names.append((key[0], key[2]))
        self.calls.append(("apply", namespace, tuple(names)))

    async def wait_rollout(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        timeout_seconds: int = 300,
    ) -> None:
        self.calls.append(("wait_rollout", namespace, kind, name, timeout_seconds))
        if (namespace, kind, name) in self.rollout_failures:
            raise RolloutTimeout(
                f"{kind} {namespace}/{name} did not become ready within "
                f"{timeout_seconds}s"
            )

    async def delete_pods(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        grace_seconds: int = 0,
    ) -> int:
        self.calls.append(("delete_pods", namespace, dict(labels), grace_seconds))
        doomed = [
            key
            for key, pod_labels in self.pods.items()
            if key[0] == namespace and _matches(pod_labels, labels)
        ]
        for key in doomed:
            del self.pods[key]
        return len(doomed)

    async def scale(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        replicas: int,
    ) -> None:
        self.calls.append(("scale", namespace, kind, name, replicas))
        key = (kind, namespace, name)
        obj = self.objects.get(key)
        if obj is None:
            raise ClusterError(f"{kind} {namespace}/{name} does not exist")
        obj.setdefault("spec", {})["replicas"] = replicas

    async def delete_managed(
        self, namespace: str, *, labels: Mapping[str, str]
    ) -> int:
        self.calls.append(("delete_managed", namespace, dict(labels)))
        selector = dict(labels)
        if selector.get(MANAGED_LABEL, "true") != "true":
            raise ClusterError(
                "delete_managed only deletes autopilot/managed=true objects"
            )
        if PROJECT_LABEL not in selector:
            raise ClusterError(
                "delete_managed requires a project label; "
                "refusing to sweep every managed object"
            )
        selector[MANAGED_LABEL] = "true"
        doomed = []
        for key, obj in self.objects.items():
            kind, obj_namespace, _name = key
            if kind == "Namespace" or obj_namespace != namespace:
                continue
            if _matches(obj.get("metadata", {}).get("labels", {}), selector):
                doomed.append(key)
        for key in doomed:
            del self.objects[key]

        pod_keys = [
            key
            for key, pod_labels in self.pods.items()
            if key[0] == namespace and _matches(pod_labels, selector)
        ]
        for key in pod_keys:
            del self.pods[key]

        secret_keys = [
            key
            for key, secret_labels in self.secret_labels.items()
            if key[0] == namespace and _matches(secret_labels, selector)
        ]
        for key in secret_keys:
            del self.secret_labels[key]
            self.secret_values.pop(key, None)

        return len(doomed) + len(pod_keys) + len(secret_keys)

    async def apply_secret(
        self,
        namespace: str,
        *,
        name: str,
        values: Mapping[str, str],
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.calls.append(("apply_secret", namespace, name, tuple(sorted(values))))
        self.secret_values[(namespace, name)] = dict(values)
        self.secret_labels[(namespace, name)] = dict(labels or {})


__all__ = ["FIELD_MANAGER", "ClusterAdapter", "FakeCluster"]
