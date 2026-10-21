"""SPEC-15 AC-1/2/11/12 certificate policy tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.ingress import (
    CertificateBudgetExceeded,
    CertificatePolicy,
    CertificateRecord,
    CertificateService,
    FakeCertificateIssuer,
    InMemoryCertificateStore,
    certificate_alerts,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def service(*, failures: int = 0):
    store = InMemoryCertificateStore()
    issuer = FakeCertificateIssuer(clock=lambda: NOW)
    issuer.failures_remaining = failures
    return CertificateService(store, issuer, clock=lambda: NOW), store, issuer


def test_ac1_100_preview_requests_issue_exactly_one_wildcard_certificate():
    cert_service, store, issuer = service()
    issued = [
        asyncio.run(
            cert_service.ensure_wildcard("preview.example.com")
        )
        for _ in range(100)
    ]
    assert len(issuer.calls) == 1
    assert issued[0].hostnames == ("*.preview.example.com",)
    assert all(record.id == issued[0].id for record in issued)
    live = asyncio.run(store.live())
    assert len(live) == 1
    assert live[0].status == "issued"
    assert live[0].is_wildcard is True


def test_ac2_sixth_certificate_for_one_registered_domain_is_blocked():
    cert_service, _, issuer = service()
    for index in range(5):
        asyncio.run(
            cert_service.request_custom(
                [f"svc{index}.acme.com"],
                registered_domain="acme.com",
                org_id="org-1",
                idempotency_key=f"request-{index}",
            )
        )
    assert len(issuer.calls) == 5
    with pytest.raises(CertificateBudgetExceeded) as exc:
        asyncio.run(
            cert_service.request_custom(
                ["sixth.acme.com"],
                registered_domain="acme.com",
                org_id="org-1",
                idempotency_key="request-sixth",
            )
        )
    assert "acme.com" in str(exc.value)
    assert "limit 5" in str(exc.value)
    assert exc.value.used == 5
    assert len(issuer.calls) == 5


def test_a_failed_attempt_can_retry_without_consuming_a_second_budget_slot():
    cert_service, store, issuer = service(failures=1)
    with pytest.raises(Exception):
        asyncio.run(
            cert_service.request_custom(
                ["acme.com"],
                registered_domain="acme.com",
                org_id="org-1",
                idempotency_key="stable-retry",
            )
        )
    record = asyncio.run(
        cert_service.request_custom(
            ["acme.com"],
            registered_domain="acme.com",
            org_id="org-1",
            idempotency_key="stable-retry",
        )
    )
    assert record.status == "issued"
    assert len(issuer.calls) == 2
    live = asyncio.run(store.live())
    assert len(live) == 1


def test_ac11_revocation_is_immediate_not_waiting_for_expiry():
    cert_service, store, issuer = service()
    record = asyncio.run(
        cert_service.request_custom(
            ["acme.com"],
            registered_domain="acme.com",
            org_id="org-1",
            idempotency_key="revoke-me",
        )
    )
    revoked = asyncio.run(cert_service.revoke(record.id))
    assert revoked.status == "revoked"
    assert issuer.revoked == [(("acme.com",), "serial-1")]
    assert asyncio.run(store.get(record.id)).status == "revoked"


def certificate(expires_in: timedelta) -> CertificateRecord:
    return CertificateRecord(
        id="cert-1",
        org_id="org-1",
        registered_domain="acme.com",
        hostnames=("acme.com",),
        is_wildcard=False,
        status="issued",
        idempotency_key="cert-1",
        issuer="fake-acme",
        serial="serial-1",
        issued_at=NOW - timedelta(days=82),
        expires_at=NOW + expires_in,
        created_at=NOW - timedelta(days=82),
    )


def test_ac12_certificate_eight_days_from_expiry_pages_on_the_14_day_threshold():
    alerts = certificate_alerts([certificate(timedelta(days=8))], NOW)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == "expiry"
    assert alert.severity == "S1"
    assert alert.action.startswith("page")
    assert alert.threshold_days == 14
    assert alert.should_page


def test_seven_day_threshold_escalates_and_far_expiry_is_quiet():
    seven = certificate_alerts([certificate(timedelta(days=6))], NOW)
    assert seven[0].threshold_days == 7
    assert seven[0].action == "page, escalate"
    assert certificate_alerts([certificate(timedelta(days=45))], NOW) == ()


def test_unexpected_issuance_is_flagged_when_the_host_is_not_known():
    unexpected = certificate_alerts(
        [certificate(timedelta(days=60))],
        NOW,
        known_hostnames={"other.example.com"},
    )
    assert len(unexpected) == 1
    assert unexpected[0].kind == "unexpected_issuance"
    assert unexpected[0].severity == "S2"
    assert "acme.com" in unexpected[0].message


def test_policy_requires_positive_limit_and_window():
    with pytest.raises(ValueError):
        CertificatePolicy(weekly_limit=0)
    assert CertificatePolicy().weekly_limit == 5
