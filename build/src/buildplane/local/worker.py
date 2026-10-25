"""Local build-worker composition (SPEC-09).

This module is intentionally thin: it wires the tested pipeline ports to
host-local adapters. The production worker replaces the fetcher/publisher with
registry-backed implementations and keeps the queue, reporter, sandbox, cache,
log store and supply-chain controls unchanged.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Mapping

import asyncpg

from buildplane.local.fetcher import LocalDirectoryTarballFetcher
from buildplane.local.publisher import (
    DEFAULT_RUNTIME_BASE,
    LocalDockerPublisher,
)
from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.cache import InMemoryBuildCache
from buildplane.pipeline.logs import LocalLogStore
from buildplane.pipeline.reporting import PostgresBuildReporter
from buildplane.pipeline.sandbox import DockerSandboxRunner
from buildplane.pipeline.service import BuildPipeline
from buildplane.queue.jobs import PostgresJobQueue
from buildplane.supplychain.audit import InMemoryAuditLog
from buildplane.supplychain.controls import SupplyChainControls
from buildplane.supplychain.denylist import InMemoryDenylist
from buildplane.supplychain.images import (
    AuditLogImageScanExceptionStore,
    ImageScanGate,
    StaticImageScanner,
)
from buildplane.supplychain.install_scripts import InMemoryBuildScriptExceptionStore

DEFAULT_BUILD_IMAGE = (
    "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


class _MappingRedactor:
    """Replace every known secret value before a log byte is stored."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = tuple(
            value for value in values.values() if value
        )

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, "[REDACTED]")
        return text


def _build_supply_chain() -> SupplyChainControls:
    audit = InMemoryAuditLog()
    return SupplyChainControls(
        audit_log=audit,
        denylist=InMemoryDenylist(),
        install_script_exceptions=InMemoryBuildScriptExceptionStore(audit),
        image_scan_gate=ImageScanGate(
            StaticImageScanner(),
            AuditLogImageScanExceptionStore(audit),
        ),
    )


async def main() -> None:
    dsn = os.environ.get("BUILD_DATABASE_URL") or os.environ.get(
        "DATABASE_URL", ""
    )
    if not dsn:
        raise SystemExit("BUILD_DATABASE_URL or DATABASE_URL is required")

    source_dir = _required("LOCAL_SOURCE_DIR")
    push_registry = _optional("BUILD_PUSH_REGISTRY", "localhost:5001")
    runtime_registry = _optional(
        "RUNTIME_REGISTRY", "k3d-autopilot-registry:5001"
    )
    build_image = _optional("BUILD_IMAGE", DEFAULT_BUILD_IMAGE)
    runtime_base = _optional("RUNTIME_BASE_IMAGE", DEFAULT_RUNTIME_BASE)
    log_dir = _optional("BUILD_LOG_DIR", ".local/logs")
    worker_id = _optional("BUILD_WORKER_ID", socket.gethostname())
    org_ids = tuple(
        part.strip()
        for part in os.environ.get("BUILD_ORG_IDS", "").split(",")
        if part.strip()
    ) or None

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    queue = PostgresJobQueue(pool)
    pipeline = BuildPipeline(
        queue=queue,
        fetcher=LocalDirectoryTarballFetcher(source_dir),
        reporter=PostgresBuildReporter(pool),
        builder_images=BuilderImageCatalog.from_mapping(
            {"generic": build_image, "static": build_image, "node": build_image}
        ),
        sandbox=DockerSandboxRunner(),
        log_store=LocalLogStore(log_dir),
        cache=InMemoryBuildCache(),
        supply_chain=_build_supply_chain(),
        redactor_factory=_MappingRedactor,
        publisher=LocalDockerPublisher(
            push_registry=push_registry,
            runtime_registry=runtime_registry,
            base_image=runtime_base,
        ),
        org_ids=org_ids,
    )
    print(
        f"build worker ready id={worker_id} source={source_dir} "
        f"push={push_registry} pull={runtime_registry}",
        flush=True,
    )
    try:
        while True:
            outcome = await pipeline.run_once(worker_id)
            if outcome is None:
                await asyncio.sleep(2)
            else:
                print(
                    f"build: job={outcome.job_id} success={outcome.success} "
                    f"stage={outcome.stage} revision={outcome.revision} "
                    f"digest={outcome.image_digest}",
                    flush=True,
                )
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
