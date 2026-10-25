"""Publish a built tree as an image through the local Docker daemon.

This is deliberately a *local* adapter: Docker is present on the developer
host, and the same binary is present on the build host in the AWS topology.
The production registry adapter will push an OCI manifest directly; the
pipeline contract (``publish -> revision + digest``) is identical either way.

The image is a static nginx image. The local slice's first project is a static
site; a later build strategy can replace this adapter without changing the
queue, reporter or deployment lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from buildplane.pipeline.errors import PublishError
from buildplane.pipeline.models import BuildOutcome, ClaimedJob, PublishedImage
from buildplane.registry.models import (
    validate_digest,
    validate_registry,
    validate_repository,
    validate_tag,
)

DEFAULT_RUNTIME_BASE = (
    "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
)


class LocalDockerPublisher:
    """Build a static image from the fetched tree and push it to the registry."""

    def __init__(
        self,
        *,
        push_registry: str,
        runtime_registry: str,
        base_image: str = DEFAULT_RUNTIME_BASE,
        docker_binary: str = "docker",
        timeout_seconds: int = 600,
    ) -> None:
        self.push_registry = validate_registry(push_registry)
        self.runtime_registry = validate_registry(runtime_registry)
        self.base_image = base_image
        self.docker_binary = docker_binary
        self.timeout_seconds = timeout_seconds

    async def publish(
        self,
        *,
        claim: ClaimedJob,
        outcome: BuildOutcome,
        source_dir: Path,
    ) -> PublishedImage:
        repository = validate_repository(
            f"autopilot/{claim.org_id}/{claim.project_slug}"
        )
        revision = validate_tag(
            f"r-{claim.git_sha[:12]}-{claim.deployment_id[:8]}"
        )
        push_ref = f"{self.push_registry}/{repository}:{revision}"
        context = source_dir.resolve()
        if not context.is_dir():
            raise PublishError(
                "the build produced no source directory to publish",
                retryable=False,
                code="publish_no_artifact",
            )

        with tempfile.TemporaryDirectory(prefix="autopilot-dockerfile-") as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text(
                f"FROM {self.base_image}\n"
                "COPY . /usr/share/nginx/html\n"
                "EXPOSE 80\n",
                encoding="utf-8",
            )
            await self._run(
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                push_ref,
                str(context),
                failure_code="publish_build_failed",
            )
        await self._run(
            "push",
            push_ref,
            failure_code="publish_push_failed",
        )
        digest = await self._digest(push_ref)
        size = await self._size(push_ref)
        return PublishedImage(
            revision=revision,
            digest=digest,
            registry=self.runtime_registry,
            repository=repository,
            size_bytes=size,
        )

    async def _digest(self, image: str) -> str:
        raw = await self._run(
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            image,
            failure_code="publish_digest_failed",
        )
        try:
            values = json.loads(raw.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise PublishError(
                "the Docker daemon returned an unreadable digest list",
                retryable=False,
                code="publish_digest_invalid",
            ) from exc
        for value in values:
            if "@" not in value:
                continue
            candidate = value.rsplit("@", 1)[1]
            try:
                return validate_digest(candidate)
            except ValueError:
                continue
        raise PublishError(
            "the pushed image has no digest; refusing to record an unpinned image",
            retryable=False,
            code="publish_digest_missing",
        )

    async def _size(self, image: str) -> int:
        raw = await self._run(
            "image",
            "inspect",
            "--format",
            "{{.Size}}",
            image,
            failure_code="publish_size_failed",
        )
        try:
            return int(raw.strip())
        except ValueError:
            return 0

    async def _run(
        self,
        *args: str,
        failure_code: str,
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PublishError(
                f"Docker binary not found: {self.docker_binary!r}",
                retryable=False,
                code="docker_unavailable",
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise PublishError(
                f"Docker {args[0]} exceeded its {self.timeout_seconds}s timeout",
                retryable=True,
                code="publish_timeout",
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[-800:]
            raise PublishError(
                f"Docker {args[0]} failed with exit code {process.returncode}: "
                f"{detail or 'no error output'}",
                retryable=True,
                code=failure_code,
            )
        return stdout.decode("utf-8", "replace")


__all__ = ["DEFAULT_RUNTIME_BASE", "LocalDockerPublisher"]
