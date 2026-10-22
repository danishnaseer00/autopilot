"""Named SPEC-12 failures.

Every error is a ``PipelineError`` so the build reporter can apply the
existing retryable/permanent split. Integrity and quota failures are permanent:
retrying the same bytes at the same tag cannot repair either one.
"""

from __future__ import annotations

from buildplane.pipeline.errors import PipelineError


class RegistryError(PipelineError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "publish",
        retryable: bool = False,
        code: str = "registry_failed",
        exit_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            stage=stage,
            retryable=retryable,
            code=code,
            exit_code=exit_code,
        )


class ContainerRegistryUnavailable(RegistryError):
    def __init__(self, message: str = "container registry is unavailable") -> None:
        super().__init__(
            message,
            stage="publish",
            retryable=True,
            code="registry_unavailable",
        )


class ImmutableTagConflict(RegistryError):
    def __init__(self, repository: str, tag: str) -> None:
        super().__init__(
            f"tag {repository}:{tag} already points at different content",
            stage="publish",
            retryable=False,
            code="immutable_tag_conflict",
        )
        self.repository = repository
        self.tag = tag


class ImageDigestMismatch(RegistryError):
    def __init__(
        self,
        *,
        repository: str,
        tag: str,
        local_digest: str,
        remote_digest: str,
    ) -> None:
        super().__init__(
            "pushed image digest does not match the locally built image: "
            f"{repository}:{tag} local={local_digest} remote={remote_digest}",
            stage="publish",
            retryable=False,
            code="image_digest_mismatch",
        )
        self.repository = repository
        self.tag = tag
        self.local_digest = local_digest
        self.remote_digest = remote_digest


class ImageQuotaExceeded(RegistryError):
    def __init__(
        self,
        *,
        org_id: str,
        used_bytes: int,
        limit_bytes: int,
        requested_bytes: int,
    ) -> None:
        self.org_id = org_id
        self.used_bytes = used_bytes
        self.limit_bytes = limit_bytes
        self.requested_bytes = requested_bytes
        super().__init__(
            "quota exceeded: image push would exceed the organisation "
            f"storage quota (code=quota_exceeded, current usage={used_bytes} "
            f"bytes, limit={limit_bytes} bytes, "
            f"requested={requested_bytes} bytes)",
            stage="publish",
            retryable=False,
            code="quota_exceeded",
        )


class ImageTooLarge(RegistryError):
    def __init__(
        self,
        *,
        size_bytes: int,
        limit_bytes: int,
    ) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            "image_too_large: image is "
            f"{size_bytes} bytes; the per-image limit is {limit_bytes} bytes",
            stage="publish",
            retryable=False,
            code="image_too_large",
        )


class ImageIdentityConflict(RegistryError):
    """The same digest is already attached to another deployment.

    ``images.digest`` is globally unique in the baseline schema, so silently
    reusing that row would make retention and rollback reason about the wrong
    deployment. Fail closed and record the schema limitation for coordinator
    review instead of corrupting the reference graph.
    """

    def __init__(self, digest: str) -> None:
        super().__init__(
            f"image digest {digest} is already recorded for another deployment",
            stage="publish",
            retryable=False,
            code="image_identity_conflict",
        )
        self.digest = digest


class PullAccessDenied(RegistryError):
    def __init__(self, message: str = "registry credential is not valid for this request") -> None:
        super().__init__(
            message,
            stage="fetch",
            retryable=False,
            code="registry_pull_denied",
        )


class PushAccessDenied(RegistryError):
    def __init__(self, message: str = "push credential is not valid for this repository") -> None:
        super().__init__(
            message,
            stage="publish",
            retryable=False,
            code="registry_push_denied",
        )
