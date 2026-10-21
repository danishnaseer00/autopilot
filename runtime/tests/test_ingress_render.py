"""SPEC-15 AC-1/3/5/10 rendering and TLS-placement tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from runtime.ingress import (
    DomainRecord,
    IngressConfig,
    IngressSnapshot,
    RouteRule,
    render_ingress,
    render_not_found_backend,
    servable_routes,
)
from runtime.ingress.domains import plan_onboarding
from runtime.ingress.validation import wildcard_certificate_name

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def route(
    route_id: str,
    host: str,
    *,
    path: str = "/",
    kind: str = "proxy",
    target: str | None = None,
    status: int = 200,
    service: str = "web",
    namespace: str = "autopilot-org",
) -> RouteRule:
    return RouteRule(
        id=route_id,
        org_id="org-1",
        project_id="project-1",
        hostname=host,
        path_prefix=path,
        kind=kind,
        target_path=target,
        status_code=status,
        namespace=namespace,
        service_name=service if kind in {"proxy", "static"} else None,
        service_port=8080 if kind in {"proxy", "static"} else None,
    )


def active_domain(hostname: str) -> DomainRecord:
    return DomainRecord(
        id=f"domain-{hostname}",
        org_id="org-1",
        project_id="project-1",
        hostname=hostname,
        status="active",
        is_production=True,
        verification_token="token",
        cert_issued_at=NOW,
        cert_expires_at=datetime(2027, 1, 19, 12, 0, tzinfo=timezone.utc),
        last_checked_at=NOW,
    )


def config(**overrides) -> IngressConfig:
    values = {
        "preview_domain": "preview.example.com",
        "ingress_namespace": "ingress-nginx",
        "include_not_found_backend": True,
    }
    values.update(overrides)
    return IngressConfig(**values)


def ingress_by_hostname(objects, hostname: str) -> dict:
    for obj in objects:
        if obj.get("kind") != "Ingress":
            continue
        rules = obj.get("spec", {}).get("rules") or []
        if rules and rules[0].get("host") == hostname:
            return obj
    raise AssertionError(f"no ingress for {hostname}")


def test_ac1_100_previews_reuse_exactly_one_wildcard_certificate():
    routes = tuple(
        route(
            f"route-{index}",
            f"pr-{index}-repo-deadbeef.preview.example.com",
        )
        for index in range(100)
    )
    snapshot = IngressSnapshot(routes=routes)
    assert len(servable_routes(snapshot, config())) == 100

    objects = render_ingress(snapshot, config())
    certificates = [obj for obj in objects if obj.get("kind") == "Certificate"]
    assert len(certificates) == 1
    assert certificates[0]["spec"]["dnsNames"] == [
        wildcard_certificate_name("preview.example.com")
    ]
    assert certificates[0]["metadata"]["namespace"] == "ingress-nginx"

    preview_ingresses = [
        obj
        for obj in objects
        if obj.get("kind") == "Ingress"
        and obj["metadata"]["labels"].get("autopilot/role") == "ingress"
    ]
    assert len(preview_ingresses) == 100
    for ingress in preview_ingresses:
        # Preview ingresses rely on the controller's single default wildcard
        # secret.  A spec.tls here would request one namespace-local cert each.
        assert "tls" not in ingress["spec"]


def test_ac3_unverified_domain_has_no_route_and_falls_to_the_designed_404():
    snapshot = IngressSnapshot(
        routes=(route("acme", "acme.com"),),
        domains=(
            DomainRecord(
                id="domain-acme",
                org_id="org-1",
                project_id="project-1",
                hostname="acme.com",
                status="pending",
                verification_token="token",
            ),
        ),
    )
    assert servable_routes(snapshot, config()) == ()

    objects = render_ingress(snapshot, config())
    assert not any(
        obj.get("kind") == "Ingress"
        and obj.get("spec", {}).get("rules", [{}])[0].get("host") == "acme.com"
        for obj in objects
    )
    catch_all = [
        obj
        for obj in objects
        if obj.get("kind") == "Ingress"
        and obj.get("spec", {}).get("defaultBackend")
    ]
    assert len(catch_all) == 1
    assert catch_all[0]["spec"]["defaultBackend"]["service"]["name"] == "autopilot-not-found"


def test_ac5_apex_and_www_both_render_and_www_redirects():
    plan = plan_onboarding(
        hostname="example.com",
        org_id="org-1",
        project_id="project-1",
        namespace="autopilot-org",
        tokens={"example.com": "tok-apex", "www.example.com": "tok-www"},
        id_factory=iter(["apex", "www"]).__next__,
    )
    apex_route = replace(
        plan.apex_route,
        service_name="web",
        service_port=8080,
    )
    snapshot = IngressSnapshot(
        routes=(apex_route, plan.www_route),
        domains=(active_domain("example.com"), active_domain("www.example.com")),
    )
    objects = render_ingress(snapshot, config())

    apex = ingress_by_hostname(objects, "example.com")
    www = ingress_by_hostname(objects, "www.example.com")
    assert apex["spec"]["tls"][0]["hosts"] == ["example.com"]
    assert www["spec"]["tls"][0]["hosts"] == ["www.example.com"]
    assert "return 301 https://example.com$request_uri;" in www["metadata"][
        "annotations"
    ]["nginx.ingress.kubernetes.io/server-snippet"]
    assert apex["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "web"

    issuers = [obj for obj in objects if obj.get("kind") == "Issuer"]
    assert len(issuers) == 1
    assert issuers[0]["metadata"]["namespace"] == "autopilot-org"
    assert (
        issuers[0]["spec"]["acme"]["solvers"][0]["http01"]["ingress"]["class"]
        == "nginx"
    )


def test_path_specific_redirect_and_rewrite_are_rendered_as_server_snippet():
    snapshot = IngressSnapshot(
        routes=(
            route("root", "app.example.com", path="/", service="root"),
            route(
                "old",
                "app.example.com",
                path="/old",
                kind="redirect",
                target="/new",
                status=301,
            ),
            route(
                "legacy",
                "app.example.com",
                path="/legacy",
                kind="rewrite",
                target="/v2/",
            ),
        ),
        domains=(active_domain("app.example.com"),),
    )
    ingress = ingress_by_hostname(render_ingress(snapshot, config()), "app.example.com")
    snippet = ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/server-snippet"]
    assert "location ^~ /old { return 301 /new; }" in snippet
    assert "rewrite ^/legacy(.*)$ /v2/$1 last;" in snippet


def test_ac10_notfound_backend_has_no_ingress_identity_and_catches_unknown_hosts():
    objects = render_not_found_backend(config())
    configmap = next(obj for obj in objects if obj.get("kind") == "ConfigMap")
    body = configmap["data"]["index.html"].lower()
    script = configmap["data"]["server.py"].lower()
    for forbidden in ("nginx", "ingress", "kubernetes"):
        assert forbidden not in body
        assert forbidden not in script
    assert 'send_header("server"' not in script

    catch_all = next(obj for obj in objects if obj.get("kind") == "Ingress")
    assert catch_all["spec"]["defaultBackend"]["service"]["name"] == "autopilot-not-found"
    assert "rules" not in catch_all["spec"]


def test_preview_configuration_never_gets_a_custom_issuer():
    snapshot = IngressSnapshot(
        routes=(
            route(
                "preview",
                "pr-1-repo-deadbeef.preview.example.com",
            ),
        )
    )
    objects = render_ingress(snapshot, config())
    assert not [obj for obj in objects if obj.get("kind") == "Issuer"]
    ingress = ingress_by_hostname(
        objects, "pr-1-repo-deadbeef.preview.example.com"
    )
    assert "cert-manager.io/issuer" not in ingress.get("metadata", {}).get(
        "annotations", {}
    )
