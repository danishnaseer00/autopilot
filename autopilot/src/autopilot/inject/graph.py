"""Consumer view of the SPEC-06 R-11 dependency graph.

This package does **not** derive a graph from a manifest.  SPEC-06 is the
contract: the platform emits ``GET /deployments/{id}/graph`` and consumers read
that document.  :class:`GraphView` only indexes the document's nodes and
directed edges so ground truth can be checked against the structure that was
actually deployed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autopilot.inject.errors import GroundTruthError

EDGE_KINDS = frozenset({"dependsOn", "route"})


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    port: int | None = None


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str

    @property
    def key(self) -> str:
        return f"{self.source}->{self.target}"


@dataclass(frozen=True)
class GraphView:
    """Nodes and directed edges copied from a SPEC-06 graph document."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    deployment_id: str | None = None

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> GraphView:
        """Read a SPEC-06 R-11 graph.

        Unknown keys are ignored (the API may add fields), but malformed nodes
        or edges fail closed: ground truth must never be validated against a
        graph the harness guessed.
        """

        if not isinstance(document, Mapping):
            raise GroundTruthError("graph must be a mapping")
        raw_nodes = document.get("nodes")
        raw_edges = document.get("edges")
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise GroundTruthError("graph must contain nodes[] and edges[]")

        nodes: list[GraphNode] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_nodes):
            if not isinstance(raw, Mapping):
                raise GroundTruthError(f"graph.nodes[{index}] must be an object")
            node_id = raw.get("id")
            node_type = raw.get("type")
            if not isinstance(node_id, str) or not node_id:
                raise GroundTruthError(f"graph.nodes[{index}].id must be a non-empty string")
            if not isinstance(node_type, str) or not node_type:
                raise GroundTruthError(f"graph.nodes[{index}].type must be a non-empty string")
            if node_id in seen:
                raise GroundTruthError(f"duplicate graph node {node_id!r}")
            seen.add(node_id)
            port = raw.get("port")
            if port is not None and (not isinstance(port, int) or isinstance(port, bool)):
                raise GroundTruthError(f"graph.nodes[{index}].port must be an integer")
            nodes.append(GraphNode(id=node_id, type=node_type, port=port))

        edges: list[GraphEdge] = []
        edge_keys: set[tuple[str, str, str]] = set()
        for index, raw in enumerate(raw_edges):
            if not isinstance(raw, Mapping):
                raise GroundTruthError(f"graph.edges[{index}] must be an object")
            source = raw.get("from")
            target = raw.get("to")
            kind = raw.get("kind")
            if not isinstance(source, str) or source not in seen:
                raise GroundTruthError(
                    f"graph.edges[{index}].from references unknown node {source!r}"
                )
            if not isinstance(target, str) or target not in seen:
                raise GroundTruthError(
                    f"graph.edges[{index}].to references unknown node {target!r}"
                )
            if kind not in EDGE_KINDS:
                raise GroundTruthError(
                    f"graph.edges[{index}].kind must be one of {sorted(EDGE_KINDS)}"
                )
            if source == target:
                raise GroundTruthError(f"graph.edges[{index}] is a self-edge")
            key = (source, target, kind)
            if key in edge_keys:
                raise GroundTruthError(f"duplicate graph edge {source}->{target} ({kind})")
            edge_keys.add(key)
            edges.append(GraphEdge(source=source, target=target, kind=kind))

        deployment_id = document.get("deployment_id")
        if deployment_id is not None and not isinstance(deployment_id, str):
            raise GroundTruthError("graph.deployment_id must be a string when present")
        return cls(
            nodes=tuple(nodes),
            edges=tuple(edges),
            deployment_id=deployment_id,
        )

    @property
    def node_ids(self) -> frozenset[str]:
        return frozenset(node.id for node in self.nodes)

    def service(self, name: str) -> GraphNode | None:
        for node in self.nodes:
            if node.id == name:
                return node
        return None

    def has_service(self, name: str) -> bool:
        return name in self.node_ids

    def has_link(self, source: str, target: str, kinds: frozenset[str] = EDGE_KINDS) -> bool:
        return any(
            edge.source == source and edge.target == target and edge.kind in kinds
            for edge in self.edges
        )

    def dependency_edges(self) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.kind == "dependsOn")

    def to_mapping(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "nodes": [
                {"id": node.id, "type": node.type, "port": node.port}
                for node in self.nodes
            ],
            "edges": [
                {"from": edge.source, "to": edge.target, "kind": edge.kind}
                for edge in self.edges
            ],
        }
        if self.deployment_id is not None:
            document["deployment_id"] = self.deployment_id
        return document
