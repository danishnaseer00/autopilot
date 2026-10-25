"""Local runtime worker: ready deployment -> Kubernetes -> live route.

The control plane records intent; the runtime plane owns cluster mutation
(SPEC-04 R-4 / SPEC-14). This worker is the local composition of that plane:
it polls the ``ready`` deployment rows produced by the build worker, renders
the stored manifest with a digest-pinned image, applies it through the narrow
``KubernetesApiAdapter``, waits for the rollout, and only then cuts the route
over.

The same module runs against a real cluster by changing configuration only:
``KUBERNETES_BASE_URL`` points at the API server (in-cluster it defaults to the
service-account mount) and ``RUNTIME_REGISTRY`` is the pull host.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

import asyncpg

from runtime.k8srender.context import RenderContext
from runtime.k8srender.deploy import DeploymentRuntime
from runtime.k8srender.http import KubernetesApiAdapter


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not value.strip():
        raise SystemExit(f"{name} is required")
    return value.strip()


def _dsn() -> str:
    return os.environ.get("RUNTIME_DATABASE_URL") or os.environ.get(
        "DATABASE_URL", ""
    )


def _manifest(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("deployment manifest is not an object")
    return value


def _first_service(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    services = manifest.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("manifest has no services")
    service = services[0]
    if not isinstance(service, Mapping):
        raise ValueError("manifest service is not an object")
    return service


def _runtime_image(
    *,
    org_id: str,
    project_slug: str,
    registry: str,
    digest: str,
) -> str:
    repository = f"autopilot/{org_id}/{project_slug}"
    return f"{registry}/{repository}@{digest}"


def _ingress(
    *,
    ctx: RenderContext,
    service_name: str,
    port: int,
    hostname: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": ctx.object_name(service_name, "ingress"),
            "namespace": ctx.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "autopilot",
                "autopilot/managed": "true",
            },
            "annotations": {
                "nginx.ingress.kubernetes.io/ssl-redirect": "false",
                "nginx.ingress.kubernetes.io/proxy-body-size": "10m",
            },
        },
        "spec": {
            "ingressClassName": "nginx",
            "rules": [
                {
                    "host": hostname,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": ctx.object_name(service_name),
                                        "port": {"number": int(port)},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        },
    }


async def _claim_ready(
    conn: asyncpg.Connection, org_ids: tuple[str, ...] | None = None
) -> asyncpg.Record | None:
    scope = ""
    params: list[object] = []
    if org_ids:
        scope = "AND d.org_id = ANY($1::uuid[])"
        params.append([uuid.UUID(org_id) for org_id in org_ids])
    return await conn.fetchrow(
        f"""
        SELECT d.*, p.slug AS project_slug, p.k8s_namespace, o.slug AS org_slug
          FROM deployments d
          JOIN projects p ON p.id = d.project_id AND p.org_id = d.org_id
          JOIN orgs o ON o.id = d.org_id
         WHERE d.status = 'ready'
           AND d.revision IS NOT NULL
           AND d.image_digest IS NOT NULL
           {scope}
         ORDER BY d.created_at
         LIMIT 1
        """,
        *params,
    )


async def _ensure_route(
    conn: asyncpg.Connection,
    *,
    deployment: asyncpg.Record,
    apps_domain: str,
) -> asyncpg.Record:
    route = await conn.fetchrow(
        "SELECT * FROM routes WHERE project_id = $1 "
        "ORDER BY priority DESC, created_at LIMIT 1",
        deployment["project_id"],
    )
    if route is not None:
        previous = route["revision"]
        await conn.execute(
            "UPDATE routes SET deployment_id = $1, revision = $2, updated_at = now() "
            "WHERE id = $3",
            deployment["id"],
            deployment["revision"],
            route["id"],
        )
        await conn.execute(
            "INSERT INTO route_history "
            "(hostname, from_revision, to_revision, deployment_id, actor) "
            "VALUES ($1, $2, $3, $4, $5)",
            route["hostname"],
            previous,
            deployment["revision"],
            deployment["id"],
            "system:runtime-worker",
        )
        return await conn.fetchrow("SELECT * FROM routes WHERE id = $1", route["id"])

    hostname = f"{deployment['project_slug']}.{apps_domain}"
    taken = await conn.fetchval("SELECT 1 FROM routes WHERE hostname = $1", hostname)
    if taken:
        hostname = f"{deployment['project_slug']}-{uuid.uuid4().hex[:6]}.{apps_domain}"
    route = await conn.fetchrow(
        "INSERT INTO routes (org_id, project_id, hostname, deployment_id, revision) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING *",
        deployment["org_id"],
        deployment["project_id"],
        hostname,
        deployment["id"],
        deployment["revision"],
    )
    await conn.execute(
        "INSERT INTO route_history "
        "(hostname, from_revision, to_revision, deployment_id, actor) "
        "VALUES ($1, NULL, $2, $3, $4)",
        hostname,
        deployment["revision"],
        deployment["id"],
        "system:runtime-worker",
    )
    return route


async def _process(
    conn: asyncpg.Connection,
    adapter: KubernetesApiAdapter,
    *,
    apps_domain: str,
    runtime_registry: str,
    org_ids: tuple[str, ...] | None = None,
) -> bool:
    deployment = await _claim_ready(conn, org_ids)
    if deployment is None:
        return False

    lock_key = str(deployment["id"])
    locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", lock_key)
    if not locked:
        return False
    try:
        manifest = _manifest(deployment["manifest"])
        service = _first_service(manifest)
        service_name = str(service.get("name", "web"))
        port = int((service.get("runtime") or {}).get("port", 80))

        image = await conn.fetchrow(
            "SELECT registry, digest FROM images WHERE deployment_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            deployment["id"],
        )
        registry = str(image["registry"]) if image else runtime_registry
        digest = str(image["digest"]) if image else str(deployment["image_digest"])
        image_ref = _runtime_image(
            org_id=str(deployment["org_id"]),
            project_slug=str(deployment["project_slug"]),
            registry=registry,
            digest=digest,
        )

        ctx = RenderContext(
            org_id=str(deployment["org_id"]),
            project_id=str(deployment["project_id"]),
            deployment_id=str(deployment["id"]),
            revision=str(deployment["revision"]),
            namespace=str(deployment["k8s_namespace"]),
            project_slug=str(deployment["project_slug"]),
            environment=str(deployment["environment"]),
            runtime_image=image_ref,
        )

        runtime = DeploymentRuntime(adapter, rollout_timeout_seconds=180)
        await runtime.apply_revision(manifest, ctx)
        route = await _ensure_route(
            conn,
            deployment=deployment,
            apps_domain=apps_domain,
        )
        await adapter.apply(
            ctx.namespace,
            [
                _ingress(
                    ctx=ctx,
                    service_name=service_name,
                    port=port,
                    hostname=str(route["hostname"]),
                )
            ],
        )
        await conn.execute(
            "UPDATE deployments SET status = 'live', stage = 'live', "
            "error_message = NULL WHERE id = $1 AND status = 'ready'",
            deployment["id"],
        )
        print(
            f"runtime: deployed {deployment['id']} "
            f"revision={deployment['revision']} host={route['hostname']}",
            flush=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the worker must stay alive
        message = f"rollout failed: {type(exc).__name__}: {exc}"[:4000]
        await conn.execute(
            "UPDATE deployments SET status = 'failed', stage = 'rollout', "
            "error_message = $1, finished_at = now() WHERE id = $2",
            message,
            deployment["id"],
        )
        print(f"runtime: deployment {deployment['id']} failed: {message}", flush=True)
        return False
    finally:
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtext($1))",
            lock_key,
        )


async def main() -> None:
    dsn = _dsn()
    if not dsn:
        raise SystemExit("RUNTIME_DATABASE_URL or DATABASE_URL is required")
    apps_domain = _env("APPS_DOMAIN", "apps.localtest.me")
    runtime_registry = _env("RUNTIME_REGISTRY", "k3d-autopilot-registry:5001")
    base_url = os.environ.get("KUBERNETES_BASE_URL") or None
    token = os.environ.get("KUBERNETES_TOKEN") or None
    ca_file = os.environ.get("KUBERNETES_CA_FILE") or None
    org_ids = tuple(
        part.strip()
        for part in os.environ.get("RUNTIME_ORG_IDS", "").split(",")
        if part.strip()
    ) or None

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    adapter = KubernetesApiAdapter(
        base_url=base_url,
        token=token,
        ca_file=ca_file,
    )
    print(
        f"runtime worker ready domain={apps_domain} registry={runtime_registry} "
        f"api={base_url or 'in-cluster'}",
        flush=True,
    )
    try:
        while True:
            async with pool.acquire() as conn:
                did_work = await _process(
                    conn,
                    adapter,
                    apps_domain=apps_domain,
                    runtime_registry=runtime_registry,
                    org_ids=org_ids,
                )
            if not did_work:
                await asyncio.sleep(2)
    finally:
        await adapter.aclose()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
