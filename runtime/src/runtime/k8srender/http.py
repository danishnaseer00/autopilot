"""Real Kubernetes API adapter for SPEC-14 R-11/R-12/R-17.

Only the operations in :class:`ClusterAdapter` are implemented. In particular
there is **no arbitrary patch** and **no namespace deletion**. Server-side apply
uses a field manager of ``autopilot`` (R-11), so conflicts are visible instead of
silently overwriting another controller's fields.

Secret values are sent once, in ``stringData``, by ``apply_secret``; they are
never returned or placed in a workload object. Error messages deliberately omit
response bodies because a Kubernetes error can echo a manifest fragment.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx

from runtime.k8srender.cluster import FIELD_MANAGER
from runtime.k8srender.errors import ClusterError, RolloutTimeout
from runtime.k8srender.names import MANAGED_LABEL, PROJECT_LABEL

_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_DEFAULT_BASE_URL = "https://kubernetes.default.svc"


@dataclass(frozen=True)
class _Kind:
    group: str
    version: str
    plural: str
    namespaced: bool = True

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    def collection_path(self, namespace: str) -> str:
        if self.namespaced:
            prefix = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
            return f"{prefix}/namespaces/{quote(namespace, safe='')}/{self.plural}"
        if self.group:
            return f"/apis/{self.group}/{self.version}/{self.plural}"
        return f"/api/{self.version}/{self.plural}"

    def item_path(self, namespace: str, name: str) -> str:
        return f"{self.collection_path(namespace)}/{quote(name, safe='')}"


_KINDS: dict[tuple[str, str], _Kind] = {
    ("v1", "Namespace"): _Kind("", "v1", "namespaces", namespaced=False),
    ("v1", "Service"): _Kind("", "v1", "services"),
    ("v1", "ConfigMap"): _Kind("", "v1", "configmaps"),
    ("v1", "Secret"): _Kind("", "v1", "secrets"),
    ("v1", "ResourceQuota"): _Kind("", "v1", "resourcequotas"),
    ("v1", "LimitRange"): _Kind("", "v1", "limitranges"),
    ("v1", "PersistentVolumeClaim"): _Kind("", "v1", "persistentvolumeclaims"),
    ("v1", "Pod"): _Kind("", "v1", "pods"),
    ("apps/v1", "Deployment"): _Kind("apps", "v1", "deployments"),
    ("apps/v1", "StatefulSet"): _Kind("apps", "v1", "statefulsets"),
    (
        "networking.k8s.io/v1",
        "NetworkPolicy",
    ): _Kind("networking.k8s.io", "v1", "networkpolicies"),
    (
        "rbac.authorization.k8s.io/v1",
        "Role",
    ): _Kind("rbac.authorization.k8s.io", "v1", "roles"),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
    ): _Kind("rbac.authorization.k8s.io", "v1", "rolebindings"),
}

_DELETE_KINDS: tuple[tuple[str, str], ...] = (
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("v1", "Service"),
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "PersistentVolumeClaim"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    ("v1", "Pod"),
)


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _kind_for(obj: Mapping[str, Any]) -> _Kind:
    key = (str(obj.get("apiVersion", "")), str(obj.get("kind", "")))
    kind = _KINDS.get(key)
    if kind is None:
        raise ClusterError(f"unsupported Kubernetes object kind {key[0]} {key[1]}")
    return kind


def _selector(labels: Mapping[str, str]) -> str:
    return ",".join(
        f"{key}={value}" for key, value in sorted(labels.items())
    )


class KubernetesApiAdapter:
    """A small async Kubernetes REST client with exactly the allowed surface."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        ca_file: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return

        resolved_token = token if token is not None else _read_optional(
            _SERVICE_ACCOUNT_DIR / "token"
        )
        if ca_file is None:
            candidate = _SERVICE_ACCOUNT_DIR / "ca.crt"
            ca_file = str(candidate) if candidate.exists() else None
        headers = {"Authorization": f"Bearer {resolved_token}"} if resolved_token else {}
        self._client = httpx.AsyncClient(
            base_url=base_url or _DEFAULT_BASE_URL,
            headers=headers,
            verify=ca_file if ca_file else True,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -------------------------------------------------------------- apply

    async def apply(
        self,
        namespace: str,
        objects: Sequence[Mapping[str, Any]],
        *,
        field_manager: str = FIELD_MANAGER,
    ) -> None:
        if field_manager != FIELD_MANAGER:
            raise ClusterError("the runtime must use the autopilot field manager")
        for obj in objects:
            kind = _kind_for(obj)
            meta = obj.get("metadata", {})
            name = str(meta.get("name", ""))
            if not name:
                raise ClusterError("an applied object has no metadata.name")
            object_namespace = meta.get("namespace")
            if kind.namespaced:
                if object_namespace is not None and str(object_namespace) != namespace:
                    raise ClusterError("object is outside the target namespace")
            elif object_namespace:
                raise ClusterError("cluster-scoped object must not set metadata.namespace")
            path = kind.item_path(namespace, name)
            await self._request(
                "PATCH",
                path,
                params={"fieldManager": field_manager},
                headers={"Content-Type": "application/apply-patch+yaml"},
                content=json.dumps(obj, sort_keys=True, separators=(",", ":")),
            )

    # ------------------------------------------------------------ rollout

    async def wait_rollout(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        timeout_seconds: int = 300,
    ) -> None:
        key = ("apps/v1", kind)
        info = _KINDS.get(key)
        if info is None or kind not in {"Deployment", "StatefulSet"}:
            raise ClusterError(f"{kind} is not a rollout workload")
        path = info.item_path(namespace, name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            response = await self._request("GET", path, allow_not_found=True)
            if response is not None:
                body = response.json()
                if _rollout_ready(kind, body):
                    return
            if loop.time() >= deadline:
                raise RolloutTimeout(
                    f"{kind} {namespace}/{name} did not become ready within "
                    f"{timeout_seconds}s"
                )
            await asyncio.sleep(2)

    # ------------------------------------------------------------ mutations

    async def delete_pods(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        grace_seconds: int = 0,
    ) -> int:
        pods = _KINDS[("v1", "Pod")]
        response = await self._request(
            "GET",
            pods.collection_path(namespace),
            params={"labelSelector": _selector(labels)},
            allow_not_found=True,
        )
        if response is None:
            return 0
        items = response.json().get("items", [])
        deleted = 0
        for item in items:
            name = str(item.get("metadata", {}).get("name", ""))
            if not name:
                continue
            await self._request(
                "DELETE",
                pods.item_path(namespace, name),
                params={"gracePeriodSeconds": int(grace_seconds)},
                allow_not_found=True,
            )
            deleted += 1
        return deleted

    async def scale(
        self,
        namespace: str,
        *,
        kind: str,
        name: str,
        replicas: int,
    ) -> None:
        info = _KINDS.get(("apps/v1", kind))
        if info is None or kind not in {"Deployment", "StatefulSet"}:
            raise ClusterError(f"{kind} cannot be scaled by this interface")
        path = info.item_path(namespace, name) + "/scale"
        await self._request(
            "PATCH",
            path,
            headers={"Content-Type": "application/merge-patch+json"},
            content=json.dumps({"spec": {"replicas": int(replicas)}}),
        )

    async def delete_managed(
        self, namespace: str, *, labels: Mapping[str, str]
    ) -> int:
        selector = dict(labels)
        if selector.get(MANAGED_LABEL, "true") != "true":
            raise ClusterError(
                "delete_managed only deletes autopilot/managed=true objects"
            )
        selector[MANAGED_LABEL] = "true"
        # A project teardown must be scoped. A bare managed=true or org-only
        # selector would sweep the org scaffold or every project in the org;
        # refuse it rather than guess.
        if PROJECT_LABEL not in selector:
            raise ClusterError(
                "delete_managed requires a project label; "
                "refusing to sweep every managed object"
            )
        deleted = 0
        for key in _DELETE_KINDS:
            info = _KINDS[key]
            response = await self._request(
                "GET",
                info.collection_path(namespace),
                params={"labelSelector": _selector(selector)},
                allow_not_found=True,
            )
            if response is None:
                continue
            for item in response.json().get("items", []):
                name = str(item.get("metadata", {}).get("name", ""))
                if not name:
                    continue
                await self._request(
                    "DELETE",
                    info.item_path(namespace, name),
                    params={"gracePeriodSeconds": 0},
                    allow_not_found=True,
                )
                deleted += 1
        return deleted

    async def apply_secret(
        self,
        namespace: str,
        *,
        name: str,
        values: Mapping[str, str],
        labels: Mapping[str, str] | None = None,
    ) -> None:
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(labels or {}),
            },
            "type": "Opaque",
            "stringData": dict(values),
        }
        info = _KINDS[("v1", "Secret")]
        await self._request(
            "PATCH",
            info.item_path(namespace, name),
            params={"fieldManager": FIELD_MANAGER},
            headers={"Content-Type": "application/apply-patch+yaml"},
            content=json.dumps(secret, sort_keys=True, separators=(",", ":")),
        )

    # -------------------------------------------------------------- transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> httpx.Response | None:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # No URL query/body in the message: a selector is safe, a Secret is not.
            raise ClusterError(
                f"Kubernetes API {method} failed ({type(exc).__name__})"
            ) from exc
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code >= 400:
            raise ClusterError(
                f"Kubernetes API {method} returned HTTP {response.status_code}"
            )
        return response


def _rollout_ready(kind: str, body: Mapping[str, Any]) -> bool:
    meta = body.get("metadata", {})
    spec = body.get("spec", {})
    status = body.get("status", {})
    desired = int(spec.get("replicas", 1))
    generation = meta.get("generation")
    observed = status.get("observedGeneration", 0)
    if generation is not None and int(observed) < int(generation):
        return False
    if int(status.get("readyReplicas", 0) or 0) != desired:
        return False
    if kind == "Deployment":
        if int(status.get("updatedReplicas", 0) or 0) != desired:
            return False
        if int(status.get("availableReplicas", 0) or 0) != desired:
            return False
        return True
    # StatefulSet: all pods must be on the current revision.
    update_revision = status.get("updateRevision")
    current_revision = status.get("currentRevision")
    if update_revision and current_revision and update_revision != current_revision:
        return False
    return True


__all__ = ["KubernetesApiAdapter"]
