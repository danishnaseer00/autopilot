"""SPEC-06 R-11 graph consumption and SPEC-30 R-13 root causes."""

from __future__ import annotations

import pytest

from autopilot.inject.errors import GroundTruthError
from autopilot.inject.graph import GraphEdge, GraphNode, GraphView
from autopilot.inject.targets import RootCause, TargetSpec


def _graph_document() -> dict:
    return {
        "deployment_id": "dep-1",
        "nodes": [
            {"id": "frontend", "type": "static", "port": 3000},
            {"id": "backend", "type": "node", "port": 8000},
            {"id": "database", "type": "stateful", "port": 5432},
        ],
        "edges": [
            {"from": "frontend", "to": "backend", "kind": "route"},
            {"from": "backend", "to": "database", "kind": "dependsOn"},
        ],
    }


def test_graph_round_trips(app) -> None:
    document = app.graph.to_mapping()
    assert GraphView.from_mapping(document) == app.graph


def test_graph_indexes_nodes_and_edges() -> None:
    graph = GraphView.from_mapping(_graph_document())
    assert graph.deployment_id == "dep-1"
    assert graph.has_service("backend")
    assert not graph.has_service("cache")
    assert graph.service("database") == GraphNode("database", "stateful", 5432)
    assert graph.has_link("backend", "database")
    assert graph.has_link("frontend", "backend")
    assert not graph.has_link("database", "backend")
    assert graph.dependency_edges() == (GraphEdge("backend", "database", "dependsOn"),)


def test_graph_ignores_unknown_top_level_fields() -> None:
    document = _graph_document()
    document["future"] = {"anything": True}
    assert GraphView.from_mapping(document).deployment_id == "dep-1"


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"nodes": [], "edges": {}},
        {"nodes": {}, "edges": []},
    ],
)
def test_malformed_graph_documents_are_rejected(document) -> None:
    with pytest.raises(GroundTruthError):
        GraphView.from_mapping(document)


def test_malformed_nodes_are_rejected() -> None:
    for node in (
        {"type": "node"},
        {"id": "", "type": "node"},
        {"id": "backend"},
        {"id": "backend", "type": "node", "port": True},
    ):
        document = _graph_document()
        document["nodes"].append(node)
        with pytest.raises(GroundTruthError):
            GraphView.from_mapping(document)


def test_duplicate_nodes_are_rejected() -> None:
    document = _graph_document()
    document["nodes"].append({"id": "backend", "type": "node", "port": 8000})
    with pytest.raises(GroundTruthError, match="duplicate graph node"):
        GraphView.from_mapping(document)


def test_edges_must_reference_known_nodes() -> None:
    document = _graph_document()
    document["edges"].append({"from": "backend", "to": "cache", "kind": "dependsOn"})
    with pytest.raises(GroundTruthError, match="unknown node"):
        GraphView.from_mapping(document)


def test_edge_kind_is_constrained() -> None:
    document = _graph_document()
    document["edges"].append({"from": "backend", "to": "frontend", "kind": "calls"})
    with pytest.raises(GroundTruthError, match="kind"):
        GraphView.from_mapping(document)


def test_self_edges_and_duplicates_are_rejected() -> None:
    document = _graph_document()
    document["edges"].append({"from": "backend", "to": "backend", "kind": "dependsOn"})
    with pytest.raises(GroundTruthError, match="self-edge"):
        GraphView.from_mapping(document)

    document = _graph_document()
    document["edges"].append({"from": "backend", "to": "database", "kind": "dependsOn"})
    with pytest.raises(GroundTruthError, match="duplicate graph edge"):
        GraphView.from_mapping(document)


def test_deployment_id_must_be_a_string() -> None:
    document = _graph_document()
    document["deployment_id"] = 3
    with pytest.raises(GroundTruthError, match="deployment_id"):
        GraphView.from_mapping(document)


def test_service_root_cause() -> None:
    root = RootCause.service_root("backend")
    assert root.kind == "service"
    assert root.component == "backend"
    assert root.to_dict() == {"kind": "service", "component": "backend"}
    root.validate_against(GraphView.from_mapping(_graph_document()))
    with pytest.raises(GroundTruthError, match="not in the deployed graph"):
        root.validate_against(GraphView.from_mapping({"nodes": [], "edges": []}))


def test_link_root_cause_is_canonical() -> None:
    root = RootCause.link_root("backend", "database")
    assert root.component == "backend->database"
    assert root.to_dict() == {
        "kind": "link",
        "component": "backend->database",
        "from": "backend",
        "to": "database",
    }
    root.validate_against(GraphView.from_mapping(_graph_document()))


def test_invalid_root_causes_are_rejected() -> None:
    with pytest.raises(GroundTruthError, match="unknown root-cause kind"):
        RootCause(kind="pod", component="backend")
    with pytest.raises(GroundTruthError, match="must not carry link"):
        RootCause(kind="service", component="backend", source="a", target="b")
    with pytest.raises(GroundTruthError, match="needs source and target"):
        RootCause(kind="link", component="a->b")
    with pytest.raises(GroundTruthError, match="must be"):
        RootCause(kind="link", component="a->b", source="a", target="c")


def test_undeclared_link_is_rejected() -> None:
    root = RootCause.link_root("backend", "frontend")
    with pytest.raises(GroundTruthError, match="not a declared graph edge"):
        root.validate_against(GraphView.from_mapping(_graph_document()))


def test_target_validation_and_labels(app) -> None:
    target = TargetSpec(namespace="autopilot-test-store", app_name="storefront", service="backend")
    target.validate_against_graph(app.graph)
    assert target.selector_labels() == {
        "autopilot/managed": "true",
        "autopilot/service": "backend",
    }
    with pytest.raises(GroundTruthError, match="no dependency"):
        target.dependency_selector_labels()


def test_target_labels_include_project_and_deployment() -> None:
    target = TargetSpec(
        namespace="autopilot-test-store",
        app_name="storefront",
        service="backend",
        project_id="project-1",
        deployment_id="deployment-1",
    )
    assert target.selector_labels()["autopilot/project-id"] == "project-1"
    assert target.selector_labels()["autopilot/deployment-id"] == "deployment-1"


def test_link_target_labels_and_validation(app) -> None:
    target = TargetSpec(
        namespace="autopilot-test-store",
        app_name="storefront",
        service="backend",
        dependency="database",
    )
    target.validate_against_graph(app.graph)
    assert target.dependency_selector_labels() == {
        "autopilot/managed": "true",
        "autopilot/service": "database",
    }
    missing = TargetSpec(
        namespace="autopilot-test-store",
        app_name="storefront",
        service="backend",
        dependency="frontend",
    )
    with pytest.raises(GroundTruthError, match="not a declared graph edge"):
        missing.validate_against_graph(app.graph)


def test_target_rejects_unknown_service_and_bad_namespace(app) -> None:
    target = TargetSpec(namespace="autopilot-test-store", app_name="storefront", service="cache")
    with pytest.raises(GroundTruthError, match="not in the graph"):
        target.validate_against_graph(app.graph)
    with pytest.raises(GroundTruthError):
        TargetSpec(namespace="autopilot-test-store", app_name="storefront", service="")
