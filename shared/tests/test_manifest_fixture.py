"""SPEC-06 R-17: the §3 example is a fixture. Both the PaaS and RCA suites run
against it, so a schema change that breaks either side fails both immediately.
AC-10 pins the graph shape: 3 nodes, exactly 2 edges (dependsOn + route).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_shared import build_graph, parse_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "example.yaml"


@pytest.fixture(scope="module")
def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fixture_manifest(fixture_text: str):
    parsed = parse_manifest(fixture_text)
    assert parsed.warnings == [], f"fixture must warn on nothing: {parsed.warnings}"
    return parsed


def test_fixture_parses_with_three_services(fixture_manifest):
    names = [s.name for s in fixture_manifest.services]
    assert names == ["frontend", "backend", "database"]


def test_ac10_graph_shape(fixture_manifest):
    graph = build_graph(fixture_manifest.manifest, deployment_id="d-000")
    assert len(graph.nodes) == 3
    assert len(graph.edges) == 2

    edge_pairs = {(e.from_, e.to, e.kind) for e in graph.edges}
    assert ("backend", "database", "dependsOn") in edge_pairs
    assert ("frontend", "backend", "route") in edge_pairs

    backend = next(n for n in graph.nodes if n.id == "backend")
    assert backend.resources == {"cpu": 1000, "memory": 1073741824}


def test_r11_wire_format_json(fixture_manifest):
    graph = build_graph(fixture_manifest.manifest)
    wire = json.loads(graph.to_json())
    assert wire["edges"][0]["to"] == "database"
    sources = [e["from"] for e in wire["edges"]]
    assert "frontend" in sources  # alias spelled "from", not "from_"


def test_resources_normalise(fixture_manifest):
    graph = build_graph(fixture_manifest.manifest)
    by_id = {n.id: n for n in graph.nodes}
    # frontend has both; normalisation uses limits
    assert by_id["frontend"].resources == {
        "cpu": 500,
        "memory": 512 * 1024 * 1024,
    }
    # database cpu "1" -> 1000 millicores; 2Gi -> 2147483648 bytes
    assert by_id["database"].resources["cpu"] == 1000
    assert by_id["database"].resources["memory"] == 2 * 1024**3
    assert by_id["database"].healthcheck is None


def test_env_values_are_secret_references(fixture_manifest):
    for svc in fixture_manifest.services:
        for value in svc.env.values():
            assert value.startswith("${") and value.endswith("}")
