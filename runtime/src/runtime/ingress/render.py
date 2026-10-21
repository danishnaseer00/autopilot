"""Pure desired-state rendering for SPEC-15 ingress and TLS.

Nothing here talks to Kubernetes or the database.  The reconciler reads the
``routes`` / ``domains`` tables, calls :func:`render_ingress_bundle`, then applies
the complete result.  A render failure therefore leaves the previous good state
untouched (SPEC-16 R-8); a partial apply is repaired by the next full render.

Important TLS design decision (R-1): preview and platform production hostnames
are covered by **one** cert-manager ``Certificate`` for
``*.<preview-domain>`` in the ingress namespace.  Their Ingresses deliberately
omit ``spec.tls`` and rely on the controller's default wildcard certificate;
per-Ingress TLS secrets are namespace-scoped and would force one certificate per
namespace.  Custom domains get their own namespace-scoped HTTP-01 Issuer and a
per-host TLS secret.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from runtime.ingress.errors import IngressRenderError
from runtime.ingress.models import (
    DomainRecord,
    IngressSnapshot,
    RouteRule,
)
from runtime.ingress.routing import designed_not_found
from runtime.ingress.validation import (
    canonical_hostname,
    is_below_suffix,
    wildcard_certificate_name,
)
from runtime.k8srender.names import MANAGED_LABEL, label_value, slugify

INGRESS_ROLE_LABEL = "autopilot/role"
INGRESS_MANAGED_LABEL = "autopilot/ingress-managed"
HOSTNAME_LABEL = "autopilot/hostname"
DEFAULT_WILDCARD_SECRET = "autopilot-preview-tls"
DEFAULT_WILDCARD_CERTIFICATE = "autopilot-preview-wildcard"
DEFAULT_CLUSTER_ISSUER = "letsencrypt"
DEFAULT_CUSTOM_ISSUER = "autopilot-http01"
DEFAULT_INGRESS_NAMESPACE = "ingress-nginx"
DEFAULT_INGRESS_CLASS = "nginx"
DEFAULT_EDGE_SERVICE = "autopilot-edge"
NOT_FOUND_SERVICE = "autopilot-not-found"
NOT_FOUND_CONFIGMAP = "autopilot-not-found-page"

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAME = 63


@dataclass(frozen=True)
class IngressConfig:
    preview_domain: str
    ingress_class: str = DEFAULT_INGRESS_CLASS
    ingress_namespace: str = DEFAULT_INGRESS_NAMESPACE
    wildcard_secret_name: str = DEFAULT_WILDCARD_SECRET
    wildcard_certificate_name: str = DEFAULT_WILDCARD_CERTIFICATE
    cluster_issuer_name: str = DEFAULT_CLUSTER_ISSUER
    custom_issuer_name: str = DEFAULT_CUSTOM_ISSUER
    acme_email: str | None = None
    edge_service_name: str = DEFAULT_EDGE_SERVICE
    edge_service_port: int = 8080
    include_not_found_backend: bool = True
    notfound_image: str = "python:3.12-alpine"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "preview_domain", canonical_hostname(self.preview_domain)
        )
        for field_name in (
            "ingress_class",
            "ingress_namespace",
            "wildcard_secret_name",
            "wildcard_certificate_name",
            "cluster_issuer_name",
            "custom_issuer_name",
            "edge_service_name",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > _MAX_NAME
                or _DNS_LABEL_RE.fullmatch(value) is None
            ):
                raise ValueError(f"{field_name} must be a Kubernetes DNS label")
        if self.acme_email is not None and (
            not isinstance(self.acme_email, str)
            or "@" not in self.acme_email
            or any(c.isspace() for c in self.acme_email)
        ):
            raise ValueError("acme_email must be an email address")
        if (
            isinstance(self.edge_service_port, bool)
            or not isinstance(self.edge_service_port, int)
            or not 1 <= self.edge_service_port <= 65535
        ):
            raise ValueError("edge_service_port must be 1-65535")

    @property
    def wildcard_hostname(self) -> str:
        return wildcard_certificate_name(self.preview_domain)


def is_preview_host(hostname: str, preview_domain: str) -> bool:
    """A concrete preview host is below the configured wildcard suffix."""

    try:
        return is_below_suffix(hostname, preview_domain)
    except ValueError:
        return False


def _stable_name(prefix: str, value: str, *, suffix: str = "") -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    slug = slugify(value, fallback="host")
    raw = f"{prefix}{slug}{suffix}-{digest}"
    if len(raw) <= _MAX_NAME:
        return raw
    head = raw[: _MAX_NAME - 9].rstrip("-._")
    return f"{head}-{digest}"


def ingress_name(hostname: str) -> str:
    return _stable_name("autopilot-ingress-", hostname)


def tls_secret_name(hostname: str) -> str:
    return _stable_name("autopilot-", hostname, suffix="-tls")


def _labels(
    *,
    role: str,
    org_id: str | None = None,
    project_id: str | None = None,
    hostname: str | None = None,
) -> dict[str, str]:
    labels = {
        MANAGED_LABEL: "true",
        INGRESS_MANAGED_LABEL: "true",
        INGRESS_ROLE_LABEL: role,
    }
    if org_id:
        labels["autopilot/org-id"] = str(org_id)
    if project_id:
        labels["autopilot/project-id"] = str(project_id)
    if hostname:
        labels[HOSTNAME_LABEL] = label_value(hostname)
    return labels


def _metadata(
    *,
    name: str,
    namespace: str,
    role: str,
    org_id: str | None = None,
    project_id: str | None = None,
    hostname: str | None = None,
    annotations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": name,
        "namespace": namespace,
        "labels": _labels(
            role=role,
            org_id=org_id,
            project_id=project_id,
            hostname=hostname,
        ),
    }
    if annotations:
        meta["annotations"] = dict(annotations)
    return meta


def _servable_host(hostname: str, snapshot: IngressSnapshot, config: IngressConfig) -> bool:
    if hostname.startswith("*."):
        # Only the platform preview wildcard is supported.  A customer wildcard
        # would need a domain row and a certificate we cannot verify per-host.
        return hostname == config.wildcard_hostname
    if is_preview_host(hostname, config.preview_domain):
        return True
    domain = snapshot.active_domain(hostname)
    return domain is not None


def servable_routes(
    snapshot: IngressSnapshot, config: IngressConfig
) -> tuple[RouteRule, ...]:
    """Filter the table to routes that may receive traffic right now.

    R-3/AC-3 is enforced here as well as by the resolver's input: a route whose
    custom hostname is pending/verified/suspended/failed is not rendered at all.
    The platform wildcard suffix is inherently controlled and needs no customer
    domain row.
    """

    selected = [
        route
        for route in snapshot.routes
        if _servable_host(route.hostname, snapshot, config)
    ]
    return tuple(
        sorted(
            selected,
            key=lambda route: (
                route.hostname,
                len(route.path_prefix),
                route.path_prefix,
                -route.priority,
                route.id,
            ),
        )
    )


def _nginx_escape_path(path: str) -> str:
    # re.escape escapes PCRE metacharacters while leaving '/' alone.  The path
    # validator has already rejected whitespace, ';', braces and control bytes.
    return re.escape(path)


def _redirect_snippet(route: RouteRule, *, host_has_paths: bool) -> str:
    target = route.target_path or "/"
    if target.startswith(("https://", "http://")) and "$request_uri" not in target:
        target = target + "$request_uri"
    if route.path_prefix == "/" and not host_has_paths:
        location = "location /"
    elif route.path_prefix == "/":
        location = "location /"
    else:
        # A prefix redirect must never be shadowed by the proxy catch-all.
        location = f"location ^~ {_nginx_escape_path(route.path_prefix)}"
    return f"{location} {{ return {route.status_code} {target}; }}"


def _rewrite_snippet(route: RouteRule) -> str:
    path = _nginx_escape_path(route.path_prefix)
    target = route.target_path or "/"
    return f"rewrite ^{path}(.*)$ {target}$1 last;"


def _snippet_for(routes: Iterable[RouteRule], *, host_has_paths: bool) -> str | None:
    lines: list[str] = []
    ordered = sorted(
        routes,
        key=lambda route: (-len(route.path_prefix), route.path_prefix, route.id),
    )
    for route in ordered:
        if route.kind == "redirect":
            lines.append(_redirect_snippet(route, host_has_paths=host_has_paths))
        elif route.kind == "rewrite":
            lines.append(_rewrite_snippet(route))
    return "\n".join(lines) if lines else None


def _edge_backend(config: IngressConfig) -> dict[str, Any]:
    return {
        "service": {
            "name": config.edge_service_name,
            "port": {"number": config.edge_service_port},
        }
    }


def _backend(route: RouteRule) -> dict[str, Any]:
    if not route.has_backend:
        raise IngressRenderError(
            f"route {route.id} has no resolved backend service; "
            "promotion must populate deployment_id before it can be rendered"
        )
    return {
        "service": {
            "name": route.service_name,
            "port": {"number": route.service_port},
        }
    }


def render_ingress(
    snapshot: IngressSnapshot,
    config: IngressConfig,
) -> tuple[dict[str, Any], ...]:
    """Render one Ingress per hostname plus the certificate/404 support objects.

    Returns every object in the complete desired state.  The caller applies all
    of it, then removes managed objects that are absent from this tuple.
    """

    routes = servable_routes(snapshot, config)
    if not routes:
        # Still render the wildcard certificate and the designed 404.  An empty
        # route table is a valid converged state.
        hosts: dict[tuple[str, str], list[RouteRule]] = {}
    else:
        hosts = {}
        for route in routes:
            if not route.namespace:
                raise IngressRenderError(
                    f"route {route.id} for {route.hostname!r} has no Kubernetes namespace"
                )
            hosts.setdefault((route.namespace, route.hostname), []).append(route)

    objects: list[dict[str, Any]] = []
    custom_namespaces: set[str] = set()

    for (namespace, hostname), host_routes in sorted(hosts.items()):
        proxy_routes = [
            route
            for route in host_routes
            if route.kind in {"proxy", "static"} and route.has_backend
        ]
        policy_routes = [
            route for route in host_routes if route.kind in {"redirect", "rewrite"}
        ]
        if not proxy_routes and not policy_routes:
            # The route exists but its deployment is not ready (or it is an
            # explicit notfound).  Do not point an Ingress at a missing Service.
            continue

        annotation_prefix = "nginx.ingress.kubernetes.io"
        annotations: dict[str, str] = {}
        paths: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for route in proxy_routes:
            if route.path_prefix in seen_paths:
                continue
            seen_paths.add(route.path_prefix)
            paths.append(
                {
                    "path": route.path_prefix,
                    "pathType": "Prefix",
                    "backend": _backend(route),
                }
            )

        if not paths:
            # Redirect/rewrite-only host: a legal Ingress still needs a path.
            # The server snippet handles the actual locations; this placeholder
            # is never selected by a matching redirect/rewrite rule.
            paths.append(
                {
                    "path": "/.autopilot-route",
                    "pathType": "Prefix",
                    "backend": _edge_backend(config),
                }
            )

        snippet = _snippet_for(policy_routes, host_has_paths=bool(proxy_routes))
        if snippet:
            annotations[f"{annotation_prefix}/server-snippet"] = snippet

        first = host_routes[0]
        custom_host = not hostname.startswith("*.") and not is_preview_host(
            hostname, config.preview_domain
        )
        tls: list[dict[str, Any]] = []
        if custom_host:
            annotations[f"cert-manager.io/issuer"] = config.custom_issuer_name
            tls = [
                {
                    "hosts": [hostname],
                    "secretName": tls_secret_name(hostname),
                }
            ]
            custom_namespaces.add(namespace)
        else:
            # One wildcard certificate is issued once in the ingress namespace
            # and configured as the controller's default certificate.  Setting
            # spec.tls here would create a namespace-local secret and therefore
            # another issuance for every namespace (R-1).
            pass

        spec: dict[str, Any] = {
            "ingressClassName": config.ingress_class,
            "rules": [
                {
                    "host": hostname,
                    "http": {"paths": paths},
                }
            ],
        }
        if tls:
            spec["tls"] = tls

        objects.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": _metadata(
                    name=ingress_name(hostname),
                    namespace=namespace,
                    role="ingress",
                    org_id=first.org_id,
                    project_id=first.project_id,
                    hostname=hostname,
                    annotations=annotations,
                ),
                "spec": spec,
            }
        )

    # One namespace-scoped Issuer for customer domains (R-5).  The wildcard
    # uses the externally managed ClusterIssuer referenced by the Certificate.
    for namespace in sorted(custom_namespaces):
        issuer_spec: dict[str, Any] = {
            "acme": {
                "server": "https://acme-v02.api.letsencrypt.org/directory",
                "privateKeySecretRef": {"name": f"{config.custom_issuer_name}-account"},
                "solvers": [
                    {
                        "http01": {
                            "ingress": {"class": config.ingress_class},
                        }
                    }
                ],
            }
        }
        if config.acme_email:
            issuer_spec["acme"]["email"] = config.acme_email
        objects.append(
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Issuer",
                "metadata": _metadata(
                    name=config.custom_issuer_name,
                    namespace=namespace,
                    role="issuer",
                ),
                "spec": issuer_spec,
            }
        )

    objects.append(_preview_certificate(config))
    if config.include_not_found_backend:
        objects.extend(render_not_found_backend(config))

    return tuple(
        sorted(
            objects,
            key=lambda obj: (
                str(obj.get("kind", "")),
                str(obj.get("metadata", {}).get("namespace", "")),
                str(obj.get("metadata", {}).get("name", "")),
            ),
        )
    )


def _preview_certificate(config: IngressConfig) -> dict[str, Any]:
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": _metadata(
            name=config.wildcard_certificate_name,
            namespace=config.ingress_namespace,
            role="wildcard-certificate",
            hostname=config.wildcard_hostname,
        ),
        "spec": {
            "secretName": config.wildcard_secret_name,
            "dnsNames": [config.wildcard_hostname],
            "issuerRef": {
                "name": config.cluster_issuer_name,
                "kind": "ClusterIssuer",
            },
            "renewBefore": "720h",
            "usages": ["digital signature", "key encipherment"],
        },
    }


_NOT_FOUND_SCRIPT = """\
import http.server

BODY = open("/srv/index.html", "rb").read()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response_only(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(BODY)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Date", self.date_time_string())
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(BODY)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):
        pass


http.server.HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


def render_not_found_backend(config: IngressConfig) -> tuple[dict[str, Any], ...]:
    """A designed 404 backend with no ingress-controller identity in its response.

    The catch-all Ingress has no host: unmatched names fall here instead of the
    controller's own default page (R-11/AC-10).  The backend's ``Server`` header
    is suppressed by using ``send_response_only`` in the tiny server script.
    """

    page = designed_not_found()
    namespace = config.ingress_namespace
    labels = _labels(role="notfound")
    selector = {**labels}

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(
            name=NOT_FOUND_CONFIGMAP,
            namespace=namespace,
            role="notfound",
        ),
        "data": {
            "index.html": page.body,
            "server.py": _NOT_FOUND_SCRIPT,
        },
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _metadata(
            name=NOT_FOUND_SERVICE,
            namespace=namespace,
            role="notfound",
        ),
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "runAsGroup": 65534,
                    },
                    "containers": [
                        {
                            "name": "not-found",
                            "image": config.notfound_image,
                            "command": ["python", "/srv/server.py"],
                            "ports": [{"name": "http", "containerPort": 8080}],
                            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "100m", "memory": "32Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "page",
                                    "mountPath": "/srv",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "page",
                            "configMap": {
                                "name": NOT_FOUND_CONFIGMAP,
                                "defaultMode": 0o444,
                            },
                        }
                    ],
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _metadata(
            name=NOT_FOUND_SERVICE,
            namespace=namespace,
            role="notfound",
        ),
        "spec": {
            "selector": selector,
            "ports": [
                {
                    "name": "http",
                    "port": 8080,
                    "targetPort": "http",
                    "protocol": "TCP",
                }
            ],
        },
    }

    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": _metadata(
            name=NOT_FOUND_SERVICE,
            namespace=namespace,
            role="notfound",
        ),
        "spec": {
            "ingressClassName": config.ingress_class,
            "defaultBackend": {
                "service": {
                    "name": NOT_FOUND_SERVICE,
                    "port": {"number": 8080},
                }
            },
        },
    }
    return (configmap, deployment, service, ingress)


__all__ = [
    "DEFAULT_CLUSTER_ISSUER",
    "DEFAULT_CUSTOM_ISSUER",
    "DEFAULT_EDGE_SERVICE",
    "DEFAULT_INGRESS_CLASS",
    "DEFAULT_INGRESS_NAMESPACE",
    "DEFAULT_WILDCARD_CERTIFICATE",
    "DEFAULT_WILDCARD_SECRET",
    "HOSTNAME_LABEL",
    "INGRESS_MANAGED_LABEL",
    "INGRESS_ROLE_LABEL",
    "IngressConfig",
    "NOT_FOUND_CONFIGMAP",
    "NOT_FOUND_SERVICE",
    "ingress_name",
    "is_preview_host",
    "render_ingress",
    "render_not_found_backend",
    "servable_routes",
    "tls_secret_name",
]
