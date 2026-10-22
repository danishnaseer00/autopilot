"""Unit coverage for SPEC-12 registry policy and credentials."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from buildplane.registry.client import InMemoryContainerRegistry
from buildplane.registry.credentials import RegistryCredentialIssuer
from buildplane.registry.errors import (
    ImageDigestMismatch,
    ImageIdentityConflict,
    ImageQuotaExceeded,
    ImageTooLarge,
    ImmutableTagConflict,
    PullAccessDenied,
    PushAccessDenied,
)
from buildplane.registry.models import (
    ImageArtifact,
    ImagePublication,
    PublishRequest,
    digest_for,
    repository_for,
    revision_for,
)
from buildplane.registry.publisher import ContainerImagePublisher, InMemoryImageStore
from buildplane.supplychain.audit import (
    ACTION_IMAGE_DIGEST_MISMATCH,
    ACTION_IMAGE_PUSHED,
    InMemoryAuditLog,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
SECRET = b"spec-12-test-secret-material-0001"
SHA = "a" * 40


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _request(
    *,
    content: bytes = b"image-layer",
    org_id: str = "org-a",
    project_id: str = "project-a",
    deployment_id: str = "deployment-a",
    tag: str | None = None,
    repository: str | None = None,
    credential: str | None = None,
) -> PublishRequest:
    return PublishRequest(
        org_id=org_id,
        project_id=project_id,
        deployment_id=deployment_id,
        registry="registry.platform.internal",
        repository=repository or repository_for("acme", "demo"),
        tag=tag or revision_for(SHA, NOW),
        artifact=ImageArtifact(content),
        build_image_digest="sha256:" + "b" * 64,
        manifest_hash="sha256:" + "c" * 64,
        credential=credential,
    )


def test_revision_and_repository_naming_use_content_identity():
    tag = revision_for(SHA, NOW)
    assert tag == "2026-10-21T120000Z-aaaaaaaa"
    assert repository_for("acme", "demo") == "autopilot/acme/demo"
    assert digest_for(b"image-layer") == (
        "sha256:"
        + "074c2199afcd766054d7576f2cd83e084c6764a8af722d8322d7ed51a9339f63"
    )


def test_registry_rejects_different_content_at_existing_tag():
    async def scenario():
        registry = InMemoryContainerRegistry()
        repository = repository_for("acme", "demo")
        tag = revision_for(SHA, NOW)
        first = b"one"
        await registry.push(
            repository=repository,
            tag=tag,
            digest=digest_for(first),
            content=first,
        )
        assert (
            await registry.push(
                repository=repository,
                tag=tag,
                digest=digest_for(first),
                content=first,
            )
            == digest_for(first)
        )
        with pytest.raises(ImmutableTagConflict):
            await registry.push(
                repository=repository,
                tag=tag,
                digest=digest_for(b"two"),
                content=b"two",
            )
        assert await registry.pull(repository=repository, reference=tag) == first

    asyncio.run(scenario())


def test_pull_credentials_are_short_lived_and_namespace_scoped():
    async def scenario():
        clock = MutableClock()
        issuer = RegistryCredentialIssuer(SECRET, clock=clock)
        registry = InMemoryContainerRegistry(
            credentials=issuer, require_credentials=True
        )
        repository_a = repository_for("acme", "demo")
        repository_b = repository_for("other", "demo")
        tag = revision_for(SHA, NOW)
        content = b"acme-image"
        push = issuer.issue_push(org_id="org-a", repository=repository_a)
        await registry.push(
            repository=repository_a,
            tag=tag,
            digest=digest_for(content),
            content=content,
            credential=push.token,
        )

        pull_a = issuer.issue_pull(org_id="org-a", org_slug="acme")
        assert (
            await registry.pull(
                repository=repository_a, reference=tag, credential=pull_a.token
            )
            == content
        )
        with pytest.raises(PullAccessDenied):
            await registry.pull(
                repository=repository_b, reference=tag, credential=pull_a.token
            )

        clock.value = NOW + timedelta(minutes=16)
        with pytest.raises(PullAccessDenied):
            issuer.verify_pull(pull_a.token, repository=repository_a)

    asyncio.run(scenario())


def test_push_credential_is_scoped_to_one_repository():
    async def scenario():
        issuer = RegistryCredentialIssuer(SECRET)
        repository_a = repository_for("acme", "demo")
        repository_b = repository_for("acme", "other")
        push = issuer.issue_push(org_id="org-a", repository=repository_a)
        issuer.verify_push(push.token, repository=repository_a)
        with pytest.raises(PushAccessDenied):
            issuer.verify_push(push.token, repository=repository_b)
        pull = issuer.issue_pull(org_id="org-a", org_slug="acme")
        with pytest.raises(PushAccessDenied):
            issuer.verify_push(pull.token, repository=repository_a)

    asyncio.run(scenario())


def test_publisher_records_a_verified_push_and_audits_it():
    async def scenario():
        registry = InMemoryContainerRegistry()
        store = InMemoryImageStore()
        audit = InMemoryAuditLog()
        publisher = ContainerImagePublisher(
            registry=registry, store=store, audit_log=audit, clock=lambda: NOW
        )
        request = _request()
        image = await publisher.publish(request)

        assert image.digest == request.digest
        assert image.tag == request.tag
        assert store.images_for("org-a") == (image,)
        assert [row.action for row in audit.rows] == [ACTION_IMAGE_PUSHED]
        assert audit.rows[0].metadata["digest"] == request.digest

    asyncio.run(scenario())


def test_per_image_limit_is_enforced_before_the_push():
    async def scenario():
        publisher = ContainerImagePublisher(
            registry=InMemoryContainerRegistry(),
            store=InMemoryImageStore(),
            clock=lambda: NOW,
            max_image_bytes=4,
        )
        with pytest.raises(ImageTooLarge) as caught:
            await publisher.publish(_request(content=b"12345"))
        assert caught.value.code == "image_too_large"
        assert caught.value.limit_bytes == 4

    asyncio.run(scenario())


def test_same_digest_for_another_deployment_fails_closed_and_cleans_the_tag():
    async def scenario():
        registry = InMemoryContainerRegistry()
        store = InMemoryImageStore()
        publisher = ContainerImagePublisher(
            registry=registry, store=store, clock=lambda: NOW
        )
        first = _request(deployment_id="deployment-a")
        await publisher.publish(first)
        second = _request(
            deployment_id="deployment-b",
            tag=revision_for("d" * 40, NOW),
        )
        with pytest.raises(ImageIdentityConflict) as caught:
            await publisher.publish(second)
        assert caught.value.code == "image_identity_conflict"
        assert not registry.has_tag(second.repository, second.tag)
        assert len(store.images_for("org-a")) == 1

    asyncio.run(scenario())


def test_quota_rejection_names_current_usage_and_limit():
    async def scenario():
        registry = InMemoryContainerRegistry()
        store = InMemoryImageStore(org_limit_bytes=64)
        existing = ImagePublication(
            org_id="org-a",
            project_id="project-a",
            deployment_id="deployment-old",
            registry="registry.platform.internal",
            tag=revision_for("b" * 40, NOW),
            digest=digest_for(b"x" * 40),
            size_bytes=40,
            build_image_digest="sha256:" + "b" * 64,
            manifest_hash="sha256:" + "c" * 64,
            repository=repository_for("acme", "demo"),
        )
        await store.record(existing, at=NOW)
        publisher = ContainerImagePublisher(
            registry=registry,
            store=store,
            clock=lambda: NOW,
        )
        with pytest.raises(ImageQuotaExceeded) as caught:
            await publisher.publish(_request(content=b"y" * 40))
        error = caught.value
        assert error.code == "quota_exceeded"
        assert error.used_bytes == 40
        assert error.limit_bytes == 64
        assert "current usage=40 bytes" in str(error)
        assert "limit=64 bytes" in str(error)
        assert len(store.images_for("org-a")) == 1

    asyncio.run(scenario())


class _WrongDigestRegistry:
    async def push(
        self,
        *,
        repository,
        tag,
        digest,
        content,
        credential=None,
    ):
        return "sha256:" + "f" * 64

    async def pull(self, *, repository, reference, credential=None):
        raise AssertionError("not used")

    async def delete(self, *, repository, reference):
        return True


def test_digest_mismatch_alerts_and_does_not_retry_or_record():
    async def scenario():
        alerts = []
        audit = InMemoryAuditLog()
        store = InMemoryImageStore()
        publisher = ContainerImagePublisher(
            registry=_WrongDigestRegistry(),
            store=store,
            audit_log=audit,
            alert=alerts.append,
            clock=lambda: NOW,
        )
        with pytest.raises(ImageDigestMismatch) as caught:
            await publisher.publish(_request())
        error = caught.value
        assert error.retryable is False
        assert error.code == "image_digest_mismatch"
        assert alerts and alerts[0].severity == "S2"
        assert alerts[0].kind == "image_digest_mismatch"
        assert audit.rows[-1].action == ACTION_IMAGE_DIGEST_MISMATCH
        assert store.images_for("org-a") == ()

    asyncio.run(scenario())
