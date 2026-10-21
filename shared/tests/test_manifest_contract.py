"""SPEC-06 contract tests: every AC/requirement the manifest validation owns.

AC-1 (explicit build strategy overrides detected defaults) is exercised by the
build plane's resolution tests (SPEC-05 owns detection); here we prove the
parsed document preserves an explicit strategy.
"""

from __future__ import annotations

import pytest

from platform_shared import (
    ManifestValidationError,
    ManifestYamlError,
    build_graph,
    parse_manifest,
)

BASE = """
version: 1
name: contract-app
services:
  - name: web
    source: ./web
    type: static
    build: { strategy: dockerfile, dockerfile: Dockerfile }
    runtime: { port: 3000 }
    resources: { requests: { cpu: 100m, memory: 128Mi } }
  - name: api
    source: ./api
    type: node
    runtime: { port: 8000 }
    dependsOn: [web]
    resources: { requests: { cpu: 200m, memory: 256Mi } }
exposure:
  routes:
    - path: /
      service: web
    - path: /api
      service: api
      stripPrefix: true
"""


def parse(text: str):
    return parse_manifest(text)


def message(exc: ManifestValidationError) -> str:
    return " | ".join(f"{p}: {m}" for p, m in exc.errors)


def test_minimal_two_service_manifest_is_valid():
    parsed = parse(BASE)
    assert [s.name for s in parsed.services] == ["web", "api"]
    assert parsed.warnings == []
    graph = build_graph(parsed.manifest)
    assert {e.kind for e in graph.edges} == {"dependsOn", "route"}


def test_ac4_unknown_dependency_names_the_path():
    with pytest.raises(ManifestValidationError) as exc:
        parse(BASE.replace("dependsOn: [web]", "dependsOn: [db]", 1))
    assert "services[1].dependsOn" in message(exc.value)
    assert "unknown service 'db'" in message(exc.value)


def test_ac5_cycle_is_rejected_and_named():
    explicit = (
        "version: 1\n"
        "name: cycle-app\n"
        "services:\n"
        "  - name: a\n"
        "    source: ./a\n"
        "    type: node\n"
        "    runtime: { port: 8000 }\n"
        "    dependsOn: [b]\n"
        "    resources: { requests: { cpu: 100m, memory: 128Mi } }\n"
        "  - name: b\n"
        "    source: ./b\n"
        "    type: node\n"
        "    runtime: { port: 8001 }\n"
        "    dependsOn: [a]\n"
        "    resources: { requests: { cpu: 100m, memory: 128Mi } }\n"
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(explicit)
    assert "dependsOn cycle" in message(exc.value)
    assert "a -> b -> a" in message(exc.value)


def test_ac6_route_to_unknown_service_rejected():
    document = BASE.replace("service: api\n      stripPrefix: true",
                            "service: missing\n      stripPrefix: true")
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "exposure.routes[1].service" in message(exc.value)
    assert "unknown service 'missing'" in message(exc.value)


def test_ac7_image_and_build_are_mutually_exclusive():
    document = (
        "version: 1\n"
        "name: ambiguous\n"
        "services:\n"
        "  - name: svc\n"
        "    source: ./s\n"
        "    type: node\n"
        "    image: registry/svc:1\n"
        "    build: { strategy: buildpack }\n"
        "    runtime: { port: 8000 }\n"
        "    resources: { requests: { cpu: 100m, memory: 128Mi } }\n"
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].build" in message(exc.value)
    assert "R-7" in message(exc.value)


def test_ac8_missing_requests_rejected_by_name():
    document = (
        "version: 1\n"
        "name: greedy\n"
        "services:\n"
        "  - name: worker\n"
        "    source: ./w\n"
        "    type: node\n"
        "    runtime: { port: 8000 }\n"
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].resources.requests" in message(exc.value)
    assert "'worker'" in message(exc.value)


def test_ac9_reserved_service_name_rejected():
    document = (
        "version: 1\n"
        "name: squatter\n"
        "services:\n"
        "  - name: autopilot-api\n"
        "    source: ./s\n"
        "    type: node\n"
        "    runtime: { port: 8000 }\n"
        "    resources: { requests: { cpu: 100m, memory: 128Mi } }\n"
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].name" in message(exc.value)


def test_ac12_version_99_fails_but_unknown_top_key_warns():
    with pytest.raises(ManifestValidationError) as exc:
        parse(BASE.replace("version: 1", "version: 99", 1))
    assert "99" in message(exc.value)

    parsed = parse("experiment: plasma\n" + BASE)
    assert any("unknown top-level key 'experiment'" in w for w in parsed.warnings)


def test_ac13_unknown_key_inside_service_entry_rejected():
    document = BASE.replace(
        "    runtime: { port: 3000 }\n",
        "    runtime: { port: 3000 }\n    resource: bogus\n", 1
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].resource" in message(exc.value), (
        "misspelled keys inside a service entry must be errors, not warnings"
    )


def test_ac14_stateful_without_persistent_volume_rejected():
    document = (
        "version: 1\n"
        "name: eager\n"
        "services:\n"
        "  - name: db\n"
        "    source: ./db\n"
        "    type: stateful\n"
        "    image: postgres:16-alpine\n"
        "    runtime: { port: 5432 }\n"
        "    resources: { requests: { cpu: 250m, memory: 512Mi } }\n"
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].runtime.persistentVolume" in message(exc.value)


def test_section5_manifest_over_256kb_rejected():
    huge = BASE + "observability:\n  logs:\n    shipped: true\n" + "# pad\n" * 50000
    assert len(huge.encode()) > 256 * 1024
    with pytest.raises(ManifestValidationError) as exc:
        parse(huge)
    assert "size limit" in message(exc.value)


def test_invalid_yaml_reports_line_and_column():
    with pytest.raises(ManifestYamlError) as exc:
        parse("version: 1\nname: x\nservices:\n  - [unclosed\n")
    assert exc.value.line is not None


def test_env_literal_values_rejected_spec02_r5():
    document = BASE.replace(
        "type: static",
        "type: static\n    env: { DATABASE_URL: postgres://user:pw@db/x }", 1
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "services[0].env.DATABASE_URL" in message(exc.value)


def test_ac1_explicit_build_strategy_is_preserved():
    parsed = parse(BASE)
    web = parsed.services[0]
    assert web.build is not None
    assert web.build.strategy == "dockerfile"  # explicit beats detected default
    assert web.build.dockerfile == "Dockerfile"


def test_build_command_and_output_dir_are_carried():
    """SPEC-07 AC-4 needs an explicit build command; the manifest must carry
    it (amendment 2026-10-20, additive: both keys stay optional)."""
    document = BASE.replace(
        "    build: { strategy: dockerfile, dockerfile: Dockerfile }",
        "    build:\n"
        "      strategy: dockerfile\n"
        "      dockerfile: Dockerfile\n"
        "      command: ['npm', 'run', 'build']\n"
        "      outputDir: dist",
    )
    web = parse(document).services[0]
    assert web.build is not None
    assert web.build.command == ["npm", "run", "build"]
    assert web.build.outputDir == "dist"


def test_max_replicas_is_carried_and_hashed():
    """SPEC-14 R-23 / AC-13: the agent scale ceiling is part of the resolved
    manifest, so it must survive validation and enter manifest_hash."""
    document = BASE.replace(
        "    runtime: { port: 3000 }",
        "    runtime: { port: 3000 }\n    maxReplicas: 5",
    )
    parsed = parse(document)
    web = parsed.services[0]
    assert web.maxReplicas == 5
    dumped = parsed.manifest.model_dump(mode="json", exclude_none=True)
    assert dumped["services"][0]["maxReplicas"] == 5


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5", "'5'"])
def test_max_replicas_must_be_a_positive_integer(value: str):
    document = BASE.replace(
        "    runtime: { port: 3000 }",
        f"    runtime: {{ port: 3000 }}\n    maxReplicas: {value}",
    )
    with pytest.raises(ManifestValidationError) as exc:
        parse(document)
    assert "maxReplicas" in message(exc.value)
