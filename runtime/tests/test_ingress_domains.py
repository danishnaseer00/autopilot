"""SPEC-15 AC-3/4/5/11 domain lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.ingress import (
    CERTIFICATE_REVOKE_TOPIC,
    DOMAIN_RECHECK_TOPIC,
    CertificateService,
    DomainAlreadyClaimed,
    DomainService,
    FakeCertificateIssuer,
    InMemoryCertificateStore,
    InMemoryDomainStore,
    InMemoryOutbox,
    IngressConfig,
    IngressSnapshot,
    IssuedCertificate,
    OutboxDispatcher,
    servable_routes,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class Verifier:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.checked: list[str] = []

    async def verify(self, domain) -> bool:
        self.checked.append(domain.hostname)
        return self.answer


def domain_service(store: InMemoryDomainStore) -> DomainService:
    return DomainService(store, clock=lambda: NOW)


async def onboard(service: DomainService):
    return await service.onboard(
        hostname="example.com",
        org_id="org-1",
        project_id="project-1",
        namespace="autopilot-org",
        tokens={"example.com": "tok-apex", "www.example.com": "tok-www"},
        id_factory=iter(["apex", "www"]).__next__,
    )


def test_ac3_onboarding_writes_both_hosts_and_routes_but_stays_unservable():
    store = InMemoryDomainStore()
    service = domain_service(store)
    plan, domains, created = asyncio.run(onboard(service))

    assert created is True
    assert plan.apex == "example.com"
    assert plan.www == "www.example.com"
    assert {domain.hostname for domain in domains} == {"example.com", "www.example.com"}
    assert {domain.status for domain in domains} == {"pending"}
    assert all(domain.verification_token for domain in domains)
    assert asyncio.run(store.route_for("example.com")) is not None
    assert asyncio.run(store.route_for("www.example.com")).kind == "redirect"

    snapshot = IngressSnapshot(
        routes=(plan.apex_route, plan.www_route),
        domains=tuple(domains),
    )
    assert servable_routes(
        snapshot,
        IngressConfig(preview_domain="preview.example.com"),
    ) == ()


def test_ac4_missing_dns_proof_suspends_an_active_domain_within_one_cycle():
    store = InMemoryDomainStore()
    service = domain_service(store)
    plan, domains, _ = asyncio.run(onboard(service))
    issued = IssuedCertificate(
        serial="serial-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(days=90),
        hostnames=("example.com",),
    )
    active = asyncio.run(service.activate("example.com", issued))
    assert active.status == "active"

    result = asyncio.run(service.recheck("example.com", Verifier(False)))
    assert result.verified is False
    assert result.suspended is True
    assert result.domain.status == "suspended"

    snapshot = IngressSnapshot(
        routes=(plan.apex_route,),
        domains=(result.domain,),
    )
    assert servable_routes(
        snapshot,
        IngressConfig(preview_domain="preview.example.com"),
    ) == ()


def test_verification_success_promotes_to_verified_then_active():
    store = InMemoryDomainStore()
    service = domain_service(store)
    _, domains, _ = asyncio.run(onboard(service))
    result = asyncio.run(service.recheck("example.com", Verifier(True)))
    assert result.verified is True
    assert result.requires_certificate is True
    assert result.domain.status == "verified"

    issued = IssuedCertificate(
        serial="serial-2",
        issued_at=NOW,
        expires_at=NOW + timedelta(days=90),
        hostnames=("example.com",),
    )
    active = asyncio.run(service.activate("example.com", issued))
    assert active.status == "active"
    assert active.cert_issued_at == NOW


def test_ac5_www_redirect_route_targets_the_apex():
    store = InMemoryDomainStore()
    service = domain_service(store)
    plan, _, _ = asyncio.run(onboard(service))
    assert plan.www_route.kind == "redirect"
    assert plan.www_route.target_path == "https://example.com"
    assert plan.www_route.status_code == 301
    assert plan.apex_route.kind == "proxy"


def test_a_hostname_cannot_be_claimed_by_a_second_project():
    store = InMemoryDomainStore()
    service = domain_service(store)
    asyncio.run(onboard(service))
    with pytest.raises(DomainAlreadyClaimed):
        asyncio.run(
            service.onboard(
                hostname="example.com",
                org_id="org-2",
                project_id="project-2",
                namespace="autopilot-org-2",
                tokens={"example.com": "other", "www.example.com": "other-www"},
                id_factory=iter(["x", "y"]).__next__,
            )
        )


def test_dns_change_can_enqueue_a_durable_recheck():
    store = InMemoryDomainStore()
    outbox = InMemoryOutbox()
    service = DomainService(store, outbox=outbox, clock=lambda: NOW)
    asyncio.run(onboard(service))

    message = asyncio.run(
        service.request_recheck("example.com", idempotency_key="dns-event-1")
    )
    assert message.topic == DOMAIN_RECHECK_TOPIC
    assert message.payload == {"hostname": "example.com"}
    assert message.status == "pending"


def test_ac11_removal_enqueues_revocation_and_dispatcher_revokes_now():
    domain_store = InMemoryDomainStore()
    domain_service_instance = domain_service(domain_store)
    asyncio.run(onboard(domain_service_instance))

    cert_store = InMemoryCertificateStore()
    issuer = FakeCertificateIssuer(clock=lambda: NOW)
    cert_service = CertificateService(cert_store, issuer, clock=lambda: NOW)
    record = asyncio.run(
        cert_service.request_custom(
            ["example.com"],
            registered_domain="example.com",
            org_id="org-1",
            idempotency_key="acme-domain",
        )
    )

    outbox = InMemoryOutbox()
    service = DomainService(
        domain_store,
        outbox=outbox,
        certificate_store=cert_store,
        clock=lambda: NOW,
    )
    removed = asyncio.run(service.remove("example.com"))
    assert removed.revoked_certificate_ids == (record.id,)
    assert len(removed.outbox_message_ids) == 1

    message = asyncio.run(outbox.get(removed.outbox_message_ids[0]))
    assert message is not None
    assert message.topic == CERTIFICATE_REVOKE_TOPIC
    assert message.status == "pending"
    assert issuer.revoked == []

    async def revoke(message):
        await cert_service.revoke(message.payload["certificate_id"])

    dispatcher = OutboxDispatcher(
        outbox,
        {CERTIFICATE_REVOKE_TOPIC: revoke},
        clock=lambda: NOW,
    )
    completed = asyncio.run(dispatcher.run_once())
    assert completed is not None and completed.status == "succeeded"
    assert issuer.revoked == [(("example.com",), "serial-1")]
    assert asyncio.run(domain_store.find("example.com")) is None
