"""The derived service-dependency graph (SPEC-06 R-10..R-12).

- Edges come ONLY from the manifest: dependsOn (caller -> dependency) and
  routes. Nothing is inferred from observed traffic (R-10) - observed traces
  later validate or contradict this declared graph.
- Node resources are the *limits* when set, else the requests, normalised to
  millicores/bytes so both consumers see identical integers (R-11).

Route-edge semantics (pending RCA-owner sign-off per R-15; fixture-derived):
the host's traffic enters through the most general route, so every non-root
route contributes an edge from the service holding "/" to its own service.
For the SPEC-06 §3 example this yields exactly the fixture: one dependsOn edge
(backend -> database) and one route edge (frontend -> backend).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    port: int
    healthcheck: str | None = None
    resources: dict[str, int]


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")  # the wire format spells it "from"
    to: str
    kind: str = "dependsOn"


class GraphDoc(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    deployment_id: str | None = None
    nodes: list[GraphNode]
    edges: list[GraphEdge]

    def to_json(self) -> str:
        # by_alias: the wire format spells the edge source "from" (R-11)
        return self.model_dump_json(exclude_none=True, by_alias=True)


def cpu_to_millicores(value: str) -> int:
    text = str(value).strip()
    if text.endswith("m"):
        return int(text[:-1])
    numeric = float(text)
    return int(numeric * 1000)


def memory_to_bytes(value: str) -> int:
    text = str(value).strip()
    binary = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    decimal = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    if text.endswith("i") or text[-1] in "kMGTK":
        for suffix, factor in binary.items():
            if text.endswith(suffix):
                return int(text[: -len(suffix)]) * factor
        for suffix, factor in decimal.items():
            if text.endswith(suffix):
                return int(text[: -len(suffix)]) * factor
    return int(text)


def _normalise_resources(service) -> dict[str, int]:
    source = (
        service.resources.limits
        if service.resources is not None and service.resources.limits is not None
        else (service.resources.requests if service.resources else None)
    )
    out: dict[str, int] = {}
    if source is None:
        return out
    if source.cpu is not None:
        out["cpu"] = cpu_to_millicores(source.cpu)
    if source.memory is not None:
        out["memory"] = memory_to_bytes(source.memory)
    return out


def build_graph(manifest: ManifestDocument, deployment_id: str | None = None) -> GraphDoc:
    """Build R-11's graph shape from a validated manifest."""
    from platform_shared.manifest.models import ManifestDocument as _Doc

    assert isinstance(manifest, _Doc)

    nodes = [
        GraphNode(
            id=svc.name,
            type=svc.type,
            port=svc.runtime.port,
            healthcheck=(svc.healthcheck.path if svc.healthcheck else None),
            resources=_normalise_resources(svc),
        )
        for svc in manifest.services
    ]

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()

    def add(src: str, dst: str, kind: str) -> None:
        key = (src, dst, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append(GraphEdge(**{"from": src}, to=dst, kind=kind))

    for svc in manifest.services:
        for dep in svc.dependsOn:
            add(svc.name, dep, "dependsOn")

    exposure = manifest.exposure
    if exposure is not None:
        routes = exposure.routes
        root = next((r.service for r in routes if r.path == "/"), None)
        for route in routes:
            if route.path != "/" and root is not None and route.service != root:
                add(root, route.service, "route")

    return GraphDoc(deployment_id=deployment_id, nodes=nodes, edges=edges)
