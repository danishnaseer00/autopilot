"""Injection targets and explicit ground-truth components (SPEC-30 R-13)."""

from __future__ import annotations

from dataclasses import dataclass

from autopilot.inject.errors import GroundTruthError
from autopilot.inject.graph import GraphEdge, GraphView
from autopilot.inject.namespaces import validate_test_namespace


@dataclass(frozen=True)
class RootCause:
    """The component a run's ground truth names.

    ``service`` is a single node.  ``link`` is a directed dependency edge and is
    stored canonically as ``source->target``; for F5 the spec explicitly says the
    root cause is the backend→database link, not either endpoint.
    """

    kind: str
    component: str
    source: str | None = None
    target: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"service", "link"}:
            raise GroundTruthError(f"unknown root-cause kind {self.kind!r}")
        if self.kind == "service":
            if self.source is not None or self.target is not None:
                raise GroundTruthError("service root cause must not carry link endpoints")
            if not self.component:
                raise GroundTruthError("service root cause needs a component")
        else:
            if not self.source or not self.target:
                raise GroundTruthError("link root cause needs source and target")
            expected = f"{self.source}->{self.target}"
            if self.component != expected:
                raise GroundTruthError(
                    f"link root cause component must be {expected!r}, got {self.component!r}"
                )

    @classmethod
    def service_root(cls, service: str) -> RootCause:
        return cls(kind="service", component=service)

    @classmethod
    def link_root(cls, source: str, target: str) -> RootCause:
        return cls(kind="link", component=f"{source}->{target}", source=source, target=target)

    def validate_against(self, graph: GraphView) -> None:
        if self.kind == "service":
            if not graph.has_service(self.component):
                raise GroundTruthError(
                    f"ground-truth service {self.component!r} is not in the deployed graph"
                )
            return
        if not graph.has_link(self.source or "", self.target or ""):
            raise GroundTruthError(
                f"ground-truth link {self.component!r} is not a declared graph edge"
            )

    def to_dict(self) -> dict[str, str]:
        data = {"kind": self.kind, "component": self.component}
        if self.kind == "link":
            data["from"] = self.source or ""
            data["to"] = self.target or ""
        return data


@dataclass(frozen=True)
class TargetSpec:
    """A concrete deployed test application target."""

    namespace: str
    app_name: str
    service: str
    dependency: str | None = None
    project_id: str | None = None
    deployment_id: str | None = None

    def __post_init__(self) -> None:
        validate_test_namespace(self.namespace)
        for field_name in ("app_name", "service"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise GroundTruthError(f"{field_name} must be a non-empty string")
        if self.dependency is not None and not self.dependency:
            raise GroundTruthError("dependency must be a non-empty string when present")

    def validate_against_graph(self, graph: GraphView) -> None:
        if not graph.has_service(self.service):
            raise GroundTruthError(f"target service {self.service!r} is not in the graph")
        if self.dependency is not None and not graph.has_link(self.service, self.dependency):
            raise GroundTruthError(
                f"target link {self.service}->{self.dependency} is not a declared graph edge"
            )

    def selector_labels(self) -> dict[str, str]:
        labels = {
            "autopilot/managed": "true",
            "autopilot/service": self.service,
        }
        if self.project_id:
            labels["autopilot/project-id"] = self.project_id
        if self.deployment_id:
            labels["autopilot/deployment-id"] = self.deployment_id
        return labels

    def dependency_selector_labels(self) -> dict[str, str]:
        if self.dependency is None:
            raise GroundTruthError("no dependency target is configured")
        return {
            "autopilot/managed": "true",
            "autopilot/service": self.dependency,
        }
