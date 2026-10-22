"""The verified image-publication service (SPEC-12 R-8..R-11).

The ordering is the integrity property:

1. reject an oversized image and an already-over-quota org before uploading;
2. push to the registry, which owns tag immutability;
3. compare the registry's digest with the local digest;
4. persist the image and the deployment pointer only after verification.

A mismatch is not retried. It is alerted as an S2 integrity failure because
retrying the same bytes would either reproduce the bug or hide a compromised
registry.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from buildplane.registry.client import ContainerRegistry
from buildplane.registry.credentials import RegistryCredentialIssuer
from buildplane.registry.errors import (
    ImageDigestMismatch,
    ImageIdentityConflict,
    ImageQuotaExceeded,
    ImageTooLarge,
)
from buildplane.registry.models import (
    DEFAULT_MAX_IMAGE_BYTES,
    ImagePublication,
    PublishRequest,
    PublishedImage,
    RegistryAlert,
    StorageUsage,
)
from buildplane.supplychain.audit import (
    ACTION_IMAGE_DIGEST_MISMATCH,
    ACTION_IMAGE_PUSHED,
    AuditLog,
    AuditRecord,
)

logger = logging.getLogger(__name__)

AlertCallback = Callable[[RegistryAlert], None | Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImageStore(Protocol):
    async def usage(self, org_id: str) -> StorageUsage: ...

    async def record(
        self, publication: ImagePublication, *, at: datetime
    ) -> PublishedImage: ...


class InMemoryImageStore:
    """Reference storage adapter with the same quota rules as Postgres."""

    def __init__(
        self,
        *,
        org_limit_bytes: int = 20 * 1024 * 1024 * 1024,
        image_limit_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        limits: dict[str, int] | None = None,
    ) -> None:
        if org_limit_bytes < 0:
            raise ValueError("org_limit_bytes cannot be negative")
        self.org_limit_bytes = org_limit_bytes
        self.image_limit_bytes = image_limit_bytes
        self._limits = dict(limits or {})
        self._images: list[PublishedImage] = []
        self._next_id = 0

    async def usage(self, org_id: str) -> StorageUsage:
        return StorageUsage(
            used_bytes=sum(
                image.size_bytes for image in self._images if image.org_id == org_id
            ),
            limit_bytes=self._limits.get(org_id, self.org_limit_bytes),
            image_limit_bytes=self.image_limit_bytes,
        )

    async def record(
        self, publication: ImagePublication, *, at: datetime
    ) -> PublishedImage:
        if publication.size_bytes > self.image_limit_bytes:
            raise ImageTooLarge(
                size_bytes=publication.size_bytes,
                limit_bytes=self.image_limit_bytes,
            )
        existing = next(
            (image for image in self._images if image.digest == publication.digest),
            None,
        )
        if existing is not None:
            if (
                existing.org_id == publication.org_id
                and existing.project_id == publication.project_id
                and existing.deployment_id == publication.deployment_id
                and existing.tag == publication.tag
            ):
                return existing
            raise ImageIdentityConflict(publication.digest)

        usage = await self.usage(publication.org_id)
        if usage.used_bytes + publication.size_bytes > usage.limit_bytes:
            raise ImageQuotaExceeded(
                org_id=publication.org_id,
                used_bytes=usage.used_bytes,
                limit_bytes=usage.limit_bytes,
                requested_bytes=publication.size_bytes,
            )
        self._next_id += 1
        image = PublishedImage(
            id=str(self._next_id),
            org_id=publication.org_id,
            project_id=publication.project_id,
            deployment_id=publication.deployment_id,
            tag=publication.tag,
            digest=publication.digest,
            registry=publication.registry,
            size_bytes=publication.size_bytes,
            build_image_digest=publication.build_image_digest,
            manifest_hash=publication.manifest_hash,
        )
        self._images.append(image)
        return image

    def images_for(self, org_id: str) -> tuple[PublishedImage, ...]:
        return tuple(image for image in self._images if image.org_id == org_id)

    def all_images(self) -> tuple[PublishedImage, ...]:
        return tuple(self._images)


class ContainerImagePublisher:
    """Push one image and record only a verified result."""

    def __init__(
        self,
        *,
        registry: ContainerRegistry,
        store: ImageStore,
        audit_log: AuditLog | None = None,
        alert: AlertCallback | None = None,
        credentials: RegistryCredentialIssuer | None = None,
        clock: Callable[[], datetime] = _utcnow,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        if max_image_bytes <= 0:
            raise ValueError("max_image_bytes must be positive")
        self.registry = registry
        self.store = store
        self.audit_log = audit_log
        self.alert = alert or _log_alert
        self.credentials = credentials
        self._clock = clock
        self.max_image_bytes = max_image_bytes

    async def publish(self, request: PublishRequest) -> PublishedImage:
        at = self._clock()
        if request.size_bytes > self.max_image_bytes:
            raise ImageTooLarge(
                size_bytes=request.size_bytes,
                limit_bytes=self.max_image_bytes,
            )

        usage = await self.store.usage(request.org_id)
        if usage.used_bytes + request.size_bytes > usage.limit_bytes:
            raise ImageQuotaExceeded(
                org_id=request.org_id,
                used_bytes=usage.used_bytes,
                limit_bytes=usage.limit_bytes,
                requested_bytes=request.size_bytes,
            )

        credential = request.credential or self._mint_push_credential(request)
        try:
            remote_digest = await self.registry.push(
                repository=request.repository,
                tag=request.tag,
                digest=request.digest,
                content=request.artifact.content,
                credential=credential,
            )
        except ImageDigestMismatch as exc:
            await self._alert_mismatch(request, exc, at)
            raise
        if remote_digest != request.digest:
            exc = ImageDigestMismatch(
                repository=request.repository,
                tag=request.tag,
                local_digest=request.digest,
                remote_digest=remote_digest,
            )
            await self._alert_mismatch(request, exc, at)
            raise exc

        publication = ImagePublication(
            org_id=request.org_id,
            project_id=request.project_id,
            deployment_id=request.deployment_id,
            registry=request.registry,
            tag=request.tag,
            digest=request.digest,
            size_bytes=request.size_bytes,
            build_image_digest=request.build_image_digest,
            manifest_hash=request.manifest_hash,
            repository=request.repository,
        )
        try:
            image = await self.store.record(publication, at=at)
        except Exception:
            # The registry has accepted bytes that the database will not
            # reference. Remove the untracked tag so retention cannot lose it.
            await self._delete_quietly(request.repository, request.tag)
            raise

        if self.audit_log is not None:
            try:
                await self.audit_log.write(
                    AuditRecord(
                        action=ACTION_IMAGE_PUSHED,
                        actor_type="system",
                        actor_id="build-plane",
                        org_id=request.org_id,
                        target_type="image",
                        target_id=request.digest,
                        metadata={
                            "deployment_id": request.deployment_id,
                            "project_id": request.project_id,
                            "registry": request.registry,
                            "repository": request.repository,
                            "tag": request.tag,
                            "digest": request.digest,
                            "size_bytes": request.size_bytes,
                            "build_image_digest": request.build_image_digest,
                            "manifest_hash": request.manifest_hash,
                        },
                    )
                )
            except Exception as exc:
                # The image is already durable and verified. An audit outage
                # must be visible, but it must not turn a successful push into
                # a retry that would hit the immutable tag.
                logger.error("image.push audit write failed: %s", type(exc).__name__)
        return image

    def _mint_push_credential(self, request: PublishRequest) -> str | None:
        if self.credentials is None:
            return None
        return self.credentials.issue_push(
            org_id=request.org_id,
            repository=request.repository,
            ttl_seconds=300,
        ).token

    async def _alert_mismatch(
        self,
        request: PublishRequest,
        error: ImageDigestMismatch,
        at: datetime,
    ) -> None:
        alert = RegistryAlert(
            kind="image_digest_mismatch",
            severity="S2",
            message=(
                "pushed image digest did not match the locally built image; "
                "deployment left unverified"
            ),
            org_id=request.org_id,
            project_id=request.project_id,
            deployment_id=request.deployment_id,
            details={
                "repository": request.repository,
                "tag": request.tag,
                "local_digest": error.local_digest,
                "remote_digest": error.remote_digest,
            },
        )
        try:
            result = self.alert(alert)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # alerting must not hide the integrity failure
            logger.error("image.digest_mismatch alert failed: %s", type(exc).__name__)
        if self.audit_log is not None:
            try:
                await self.audit_log.write(
                    AuditRecord(
                        action=ACTION_IMAGE_DIGEST_MISMATCH,
                        actor_type="system",
                        actor_id="build-plane",
                        org_id=request.org_id,
                        target_type="image",
                        target_id=request.digest,
                        metadata={
                            "deployment_id": request.deployment_id,
                            "project_id": request.project_id,
                            "repository": request.repository,
                            "tag": request.tag,
                            "local_digest": error.local_digest,
                            "remote_digest": error.remote_digest,
                            "severity": "S2",
                            "retry": False,
                            "at": at.isoformat(),
                        },
                    )
                )
            except Exception as exc:
                logger.error(
                    "image.digest_mismatch audit write failed: %s",
                    type(exc).__name__,
                )

    async def _delete_quietly(self, repository: str, tag: str) -> None:
        try:
            await self.registry.delete(repository=repository, reference=tag)
        except Exception as exc:
            logger.error("unreferenced image cleanup failed: %s", type(exc).__name__)


def _log_alert(alert: RegistryAlert) -> None:
    logger.error(
        "registry alert kind=%s severity=%s org=%s project=%s deployment=%s %s",
        alert.kind,
        alert.severity,
        alert.org_id,
        alert.project_id,
        alert.deployment_id,
        alert.message,
    )
