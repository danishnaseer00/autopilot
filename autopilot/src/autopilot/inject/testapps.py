"""Fixed test-application catalogue (SPEC-30 R-14/R-15/R-16).

Results are only comparable across months if the application is fixed and
version-controlled.  This module loads a pinned catalogue and refuses a changed
copy.  It records the SPEC-06 graph shape but does not derive it: the deployed
``GET /deployments/{id}/graph`` document is authoritative and is what
``GraphView`` consumes at run time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from autopilot.inject.catalogue import FAULT_CATALOGUE
from autopilot.inject.errors import TestAppCatalogueError
from autopilot.inject.graph import GraphView

DATA_DIR = Path(__file__).resolve().parent / "data"
TESTAPPS_V1_PATH = DATA_DIR / "testapps-v1.json"
TESTAPPS_V1_SHA256 = "c6db74c76f875b218ffc9ef4bbf1cddd8d3acebe8ec5d99370bf27a601ff8cd4"

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_SERVICE_TYPES = frozenset({"static", "node", "python", "stateful"})


def catalogue_digest(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TestService:
    name: str
    type: str
    port: int
    load_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _DNS_LABEL.match(self.name) or len(self.name) > 63:
            raise TestAppCatalogueError(f"service name {self.name!r} is not a DNS label")
        if self.type not in _SERVICE_TYPES:
            raise TestAppCatalogueError(f"service {self.name!r} has unknown type {self.type!r}")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise TestAppCatalogueError(f"service {self.name!r} has invalid port {self.port!r}")
        for path in self.load_paths:
            if not isinstance(path, str) or not path.startswith("/"):
                raise TestAppCatalogueError(
                    f"service {self.name!r} has invalid load path {path!r}"
                )


@dataclass(frozen=True)
class Observability:
    prometheus: bool
    traces: bool
    logs: bool
    app_side_config: bool

    def validate(self) -> None:
        missing = [
            name
            for name in ("prometheus", "traces", "logs")
            if not getattr(self, name)
        ]
        if missing:
            raise TestAppCatalogueError(
                "test app must emit metrics, logs and traces by default; missing "
                + ", ".join(missing)
            )
        if self.app_side_config:
            raise TestAppCatalogueError(
                "test app must not require application-side telemetry configuration"
            )


@dataclass(frozen=True)
class LoadEndpointSpec:
    path: str
    weight: int
    service: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise TestAppCatalogueError(f"load endpoint path {self.path!r} must start with /")
        if not isinstance(self.weight, int) or isinstance(self.weight, bool) or self.weight < 1:
            raise TestAppCatalogueError(f"load endpoint {self.path!r} needs a positive weight")
        if not isinstance(self.service, str) or not self.service:
            raise TestAppCatalogueError(f"load endpoint {self.path!r} needs a service")


@dataclass(frozen=True)
class TestApplication:
    app_id: str
    version: str
    description: str
    services: tuple[TestService, ...]
    graph: GraphView
    observability: Observability
    load_host: str
    load_endpoints: tuple[LoadEndpointSpec, ...]
    fault_hooks: Mapping[str, str]

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> TestApplication:
        if not isinstance(document, Mapping):
            raise TestAppCatalogueError("test application must be an object")
        app_id = document.get("app_id")
        version = document.get("version")
        if not isinstance(app_id, str) or not _DNS_LABEL.match(app_id):
            raise TestAppCatalogueError(f"invalid app_id {app_id!r}")
        if not isinstance(version, str) or not version:
            raise TestAppCatalogueError(f"{app_id} needs a version")
        description = document.get("description", "")
        if not isinstance(description, str):
            raise TestAppCatalogueError(f"{app_id} description must be a string")

        raw_services = document.get("services")
        if not isinstance(raw_services, list) or len(raw_services) < 2:
            raise TestAppCatalogueError(
                f"{app_id} must be multi-service; a single-service app cannot "
                "evaluate cross-service localization"
            )
        services: list[TestService] = []
        for index, raw in enumerate(raw_services):
            if not isinstance(raw, Mapping):
                raise TestAppCatalogueError(f"{app_id}.services[{index}] must be an object")
            try:
                services.append(
                    TestService(
                        name=raw["name"],
                        type=raw["type"],
                        port=raw["port"],
                        load_paths=tuple(raw.get("load_paths", ())),
                    )
                )
            except KeyError as exc:
                raise TestAppCatalogueError(
                    f"{app_id}.services[{index}] is missing {exc.args[0]}"
                ) from exc
        names = [service.name for service in services]
        if len(names) != len(set(names)):
            raise TestAppCatalogueError(f"{app_id} has duplicate service names")

        graph_document = document.get("graph")
        if not isinstance(graph_document, Mapping):
            raise TestAppCatalogueError(f"{app_id} needs a SPEC-06 graph")
        graph = GraphView.from_mapping(graph_document)
        if graph.node_ids != frozenset(names):
            raise TestAppCatalogueError(
                f"{app_id} graph nodes do not match services: "
                f"{sorted(graph.node_ids)} != {sorted(names)}"
            )
        for service in services:
            node = graph.service(service.name)
            assert node is not None
            if node.type != service.type:
                raise TestAppCatalogueError(
                    f"{app_id}.{service.name} graph type {node.type!r} != {service.type!r}"
                )
            if node.port is not None and node.port != service.port:
                raise TestAppCatalogueError(
                    f"{app_id}.{service.name} graph port {node.port} != {service.port}"
                )
        if not graph.dependency_edges():
            raise TestAppCatalogueError(
                f"{app_id} graph has no dependsOn edge; there is no cross-service "
                "root cause to localise"
            )

        raw_observability = document.get("observability")
        if not isinstance(raw_observability, Mapping):
            raise TestAppCatalogueError(f"{app_id} needs an observability block")
        try:
            observability = Observability(
                prometheus=bool(raw_observability["prometheus"]),
                traces=bool(raw_observability["traces"]),
                logs=bool(raw_observability["logs"]),
                app_side_config=bool(raw_observability["app_side_config"]),
            )
        except KeyError as exc:
            raise TestAppCatalogueError(
                f"{app_id}.observability is missing {exc.args[0]}"
            ) from exc
        observability.validate()

        raw_load = document.get("load")
        if not isinstance(raw_load, Mapping):
            raise TestAppCatalogueError(f"{app_id} needs a load block")
        load_host = raw_load.get("host")
        if not isinstance(load_host, str) or not load_host.startswith(("http://", "https://")):
            raise TestAppCatalogueError(f"{app_id} load.host must be an http(s) URL")
        raw_endpoints = raw_load.get("endpoints")
        if not isinstance(raw_endpoints, list) or not raw_endpoints:
            raise TestAppCatalogueError(f"{app_id} needs at least one load endpoint")
        endpoints: list[LoadEndpointSpec] = []
        for index, raw in enumerate(raw_endpoints):
            if not isinstance(raw, Mapping):
                raise TestAppCatalogueError(f"{app_id}.load.endpoints[{index}] must be an object")
            try:
                endpoint = LoadEndpointSpec(
                    path=raw["path"],
                    weight=raw["weight"],
                    service=raw["service"],
                )
            except KeyError as exc:
                raise TestAppCatalogueError(
                    f"{app_id}.load.endpoints[{index}] is missing {exc.args[0]}"
                ) from exc
            service = next((item for item in services if item.name == endpoint.service), None)
            if service is None:
                raise TestAppCatalogueError(
                    f"{app_id} load endpoint {endpoint.path!r} targets unknown service "
                    f"{endpoint.service!r}"
                )
            if endpoint.path not in service.load_paths:
                raise TestAppCatalogueError(
                    f"{app_id} load path {endpoint.path!r} is not declared on "
                    f"service {endpoint.service!r}"
                )
            endpoints.append(endpoint)

        raw_hooks = document.get("fault_hooks", {})
        if not isinstance(raw_hooks, Mapping):
            raise TestAppCatalogueError(f"{app_id} fault_hooks must be an object")
        hooks: dict[str, str] = {}
        for fault_id, path in raw_hooks.items():
            if fault_id not in FAULT_CATALOGUE:
                raise TestAppCatalogueError(f"{app_id} has a hook for unknown fault {fault_id!r}")
            if not isinstance(path, str) or not path.startswith("/"):
                raise TestAppCatalogueError(
                    f"{app_id} hook for {fault_id} must be an absolute path"
                )
            hooks[fault_id] = path
        for fault in FAULT_CATALOGUE.values():
            if fault.injector == "application_hook" and fault.fault_id not in hooks:
                raise TestAppCatalogueError(
                    f"{app_id} is missing an application hook for {fault.fault_id}"
                )

        return cls(
            app_id=app_id,
            version=version,
            description=description,
            services=tuple(services),
            graph=graph,
            observability=observability,
            load_host=load_host,
            load_endpoints=tuple(endpoints),
            fault_hooks=MappingProxyType(hooks),
        )

    def service(self, name: str) -> TestService:
        for service in self.services:
            if service.name == name:
                return service
        raise TestAppCatalogueError(f"{self.app_id} has no service {name!r}")

    def services_of_type(self, types: tuple[str, ...]) -> tuple[TestService, ...]:
        if not types:
            return self.services
        return tuple(service for service in self.services if service.type in types)


@dataclass(frozen=True)
class TestAppCatalogue:
    version: int
    catalogue_id: str
    created_at: str
    apps: tuple[TestApplication, ...]
    sha256: str

    def app(self, app_id: str) -> TestApplication:
        for app in self.apps:
            if app.app_id == app_id:
                return app
        raise TestAppCatalogueError(f"unknown test application {app_id!r}")


def load_test_app_catalogue(path: Path | None = None) -> TestAppCatalogue:
    """Load and validate the fixed catalogue, checking the committed digest."""

    source = path or TESTAPPS_V1_PATH
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TestAppCatalogueError(f"cannot read test-app catalogue {source}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TestAppCatalogueError(f"invalid test-app catalogue JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise TestAppCatalogueError("test-app catalogue must be an object")

    digest = catalogue_digest(document)
    checks_pin = path is None or source.resolve() == TESTAPPS_V1_PATH.resolve()
    if checks_pin and TESTAPPS_V1_SHA256 != "TO_BE_PINNED" and digest != TESTAPPS_V1_SHA256:
        raise TestAppCatalogueError(
            "committed test-app catalogue digest mismatch: "
            f"expected {TESTAPPS_V1_SHA256}, got {digest}. "
            "The catalogue is fixed by SPEC-30 R-16; create a new version instead "
            "of editing it in place."
        )

    if document.get("version") != 1:
        raise TestAppCatalogueError("unsupported test-app catalogue version")
    catalogue_id = document.get("catalogue_id")
    created_at = document.get("created_at")
    if not isinstance(catalogue_id, str) or not catalogue_id:
        raise TestAppCatalogueError("test-app catalogue needs catalogue_id")
    if not isinstance(created_at, str) or not created_at:
        raise TestAppCatalogueError("test-app catalogue needs created_at")
    raw_apps = document.get("apps")
    if not isinstance(raw_apps, list) or not raw_apps:
        raise TestAppCatalogueError("test-app catalogue needs at least one app")
    apps = tuple(TestApplication.from_document(raw) for raw in raw_apps)
    if len({app.app_id for app in apps}) != len(apps):
        raise TestAppCatalogueError("duplicate test application id")
    return TestAppCatalogue(
        version=1,
        catalogue_id=catalogue_id,
        created_at=created_at,
        apps=apps,
        sha256=digest,
    )


def default_app() -> TestApplication:
    return load_test_app_catalogue().app("storefront")
