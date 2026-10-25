"""SPEC-30 R-14..R-16/AC-9/AC-10: the fixed multi-service test application."""

from __future__ import annotations

import copy
import json

import pytest

import autopilot.inject.testapps as testapps_module
from autopilot.inject.catalogue import FAULT_CATALOGUE
from autopilot.inject.errors import TestAppCatalogueError
from autopilot.inject.testapps import (
    TESTAPPS_V1_SHA256,
    TestApplication,
    default_app,
    load_test_app_catalogue,
)

# Imported names starting with ``Test`` are production classes, not pytest tests.
TestApplication.__test__ = False
TestAppCatalogueError.__test__ = False


def _raw_document() -> dict:
    return json.loads(testapps_module.TESTAPPS_V1_PATH.read_text(encoding="utf-8"))


def _raw_app() -> dict:
    return copy.deepcopy(_raw_document()["apps"][0])


def test_catalogue_is_pinned_and_versioned() -> None:
    catalogue = load_test_app_catalogue()
    assert catalogue.version == 1
    assert catalogue.catalogue_id == "autopilot-testapps-v1"
    assert catalogue.sha256 == TESTAPPS_V1_SHA256
    assert len(catalogue.apps) == 1


def test_editing_the_catalogue_in_place_fails_closed(tmp_path, monkeypatch) -> None:
    # R-16: month 1 and month 4 must be comparable.
    document = _raw_document()
    document["created_at"] = "2027-01-01"
    edited = tmp_path / "testapps-v1.json"
    edited.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(testapps_module, "TESTAPPS_V1_PATH", edited)
    with pytest.raises(TestAppCatalogueError, match="digest mismatch"):
        load_test_app_catalogue()


def test_explicit_default_path_cannot_bypass_the_pin(tmp_path, monkeypatch) -> None:
    document = _raw_document()
    document["created_at"] = "2027-01-01"
    edited = tmp_path / "testapps-v1.json"
    edited.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(testapps_module, "TESTAPPS_V1_PATH", edited)
    with pytest.raises(TestAppCatalogueError, match="digest mismatch"):
        load_test_app_catalogue(testapps_module.TESTAPPS_V1_PATH)


def test_app_is_multi_service_with_a_dependency_graph(app) -> None:
    # AC-9/R-14.
    assert len(app.services) == 3
    assert app.graph.node_ids == frozenset({"frontend", "backend", "database"})
    assert app.graph.has_link("backend", "database", frozenset({"dependsOn"}))
    assert app.graph.has_link("frontend", "backend", frozenset({"route"}))
    assert app.graph.dependency_edges()


def test_graph_and_services_agree(app) -> None:
    for service in app.services:
        node = app.graph.service(service.name)
        assert node is not None
        assert node.type == service.type
        assert node.port == service.port


def test_telemetry_is_on_by_default(app) -> None:
    # AC-10/R-15.
    assert app.observability.prometheus is True
    assert app.observability.traces is True
    assert app.observability.logs is True
    assert app.observability.app_side_config is False


def test_application_faults_have_hook_paths(app) -> None:
    for fault in FAULT_CATALOGUE.values():
        if fault.injector == "application_hook":
            assert fault.fault_id in app.fault_hooks
            assert app.fault_hooks[fault.fault_id].startswith("/")

def test_load_endpoints_are_declared_services(app) -> None:
    assert app.load_endpoints
    for endpoint in app.load_endpoints:
        service = app.service(endpoint.service)
        assert endpoint.path in service.load_paths


def test_default_app_is_the_storefront(app) -> None:
    assert default_app() == app


def test_service_lookup_fails_closed(app) -> None:
    with pytest.raises(TestAppCatalogueError, match="no service"):
        app.service("nonexistent")


def test_services_of_type_filters() -> None:
    app = default_app()
    assert app.services_of_type(()) == app.services
    assert [s.name for s in app.services_of_type(("node",))] == ["backend"]
    assert [s.name for s in app.services_of_type(("static", "python"))] == ["frontend"]


def test_unknown_app_id_fails(tmp_path) -> None:
    catalogue = load_test_app_catalogue()
    with pytest.raises(TestAppCatalogueError, match="unknown test application"):
        catalogue.app("missing")


def test_single_service_app_is_rejected() -> None:
    document = _raw_app()
    document["services"] = document["services"][:1]
    document["graph"]["nodes"] = document["graph"]["nodes"][:1]
    document["graph"]["edges"] = []
    document["load"]["endpoints"] = document["load"]["endpoints"][:1]
    with pytest.raises(TestAppCatalogueError, match="multi-service"):
        TestApplication.from_document(document)


def test_graph_nodes_must_match_services() -> None:
    document = _raw_app()
    document["services"] = [s for s in document["services"] if s["name"] != "database"]
    with pytest.raises(TestAppCatalogueError, match="graph nodes do not match"):
        TestApplication.from_document(document)


def test_app_side_telemetry_configuration_is_rejected() -> None:
    document = _raw_app()
    document["observability"]["app_side_config"] = True
    with pytest.raises(TestAppCatalogueError, match="application-side"):
        TestApplication.from_document(document)


def test_missing_observability_signal_is_rejected() -> None:
    document = _raw_app()
    document["observability"]["traces"] = False
    with pytest.raises(TestAppCatalogueError, match="metrics, logs and traces"):
        TestApplication.from_document(document)


def test_missing_application_hook_is_rejected() -> None:
    document = _raw_app()
    del document["fault_hooks"]["F7"]
    with pytest.raises(TestAppCatalogueError, match="missing an application hook for F7"):
        TestApplication.from_document(document)


def test_unknown_fault_hook_is_rejected() -> None:
    document = _raw_app()
    document["fault_hooks"]["F99"] = "/hook"
    with pytest.raises(TestAppCatalogueError, match="unknown fault"):
        TestApplication.from_document(document)


def test_load_endpoint_must_target_a_declared_service() -> None:
    document = _raw_app()
    document["load"]["endpoints"][0]["service"] = "cache"
    with pytest.raises(TestAppCatalogueError, match="unknown service"):
        TestApplication.from_document(document)


def test_load_path_must_be_declared_on_the_service() -> None:
    document = _raw_app()
    document["load"]["endpoints"][0]["path"] = "/admin"
    with pytest.raises(TestAppCatalogueError, match="not declared"):
        TestApplication.from_document(document)


def test_invalid_load_host_is_rejected() -> None:
    document = _raw_app()
    document["load"]["host"] = "frontend:3000"
    with pytest.raises(TestAppCatalogueError, match="http"):
        TestApplication.from_document(document)


def test_duplicate_service_names_are_rejected() -> None:
    document = _raw_app()
    document["services"][1]["name"] = "frontend"
    with pytest.raises(TestAppCatalogueError, match="duplicate service names"):
        TestApplication.from_document(document)


def test_graph_without_a_dependency_edge_is_rejected() -> None:
    document = _raw_app()
    document["graph"]["edges"] = [
        {"from": "frontend", "to": "backend", "kind": "route"}
    ]
    with pytest.raises(TestAppCatalogueError, match="no dependsOn edge"):
        TestApplication.from_document(document)


def test_unsupported_catalogue_version_is_rejected(tmp_path) -> None:
    document = _raw_document()
    document["version"] = 2
    path = tmp_path / "testapps-v2.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(TestAppCatalogueError, match="unsupported"):
        load_test_app_catalogue(path)


def test_duplicate_app_ids_are_rejected(tmp_path) -> None:
    document = _raw_document()
    document["apps"].append(copy.deepcopy(document["apps"][0]))
    path = tmp_path / "testapps-dupe.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(TestAppCatalogueError, match="duplicate test application"):
        load_test_app_catalogue(path)


def test_malformed_json_is_rejected(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TestAppCatalogueError, match="invalid test-app catalogue JSON"):
        load_test_app_catalogue(path)
