"""SPEC-12 R-12/R-13: Dockerfile base images are pinned and scanned."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from buildplane.detection import RepositoryIndex
from buildplane.pipeline.strategy import resolve_strategy
from buildplane.supplychain.audit import (
    ACTION_IMAGE_SCAN_EXCEPTION_USED,
    InMemoryAuditLog,
)
from buildplane.supplychain.base_images import parse_base_images
from buildplane.supplychain.controls import SupplyChainControls
from buildplane.supplychain.denylist import InMemoryDenylist
from buildplane.supplychain.errors import BaseImageNotPinned, ImageScanBlocked
from buildplane.supplychain.images import (
    AuditLogImageScanExceptionStore,
    ImageScanGate,
    ImageScanReport,
    ScanFinding,
    Severity,
    StaticImageScanner,
)
from buildplane.supplychain.install_scripts import InMemoryBuildScriptExceptionStore
from support import builder_ref, make_claim

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64
BASE = f"node:20@sha256:{DIGEST}"


def _index(dockerfile: str) -> RepositoryIndex:
    return RepositoryIndex.from_entries(
        [("Dockerfile", dockerfile.encode("utf-8"))], root_path="."
    )


def _controls(audit: InMemoryAuditLog, scanner: StaticImageScanner):
    owner = AuditLogImageScanExceptionStore(audit, clock=lambda: NOW)
    return (
        SupplyChainControls(
            audit_log=audit,
            denylist=InMemoryDenylist(),
            install_script_exceptions=InMemoryBuildScriptExceptionStore(
                audit, clock=lambda: NOW
            ),
            image_scan_gate=ImageScanGate(scanner, owner),
            clock=lambda: NOW,
        ),
        owner,
    )


def test_parser_accepts_digest_pins_and_skips_stage_and_scratch_references():
    content = f"""
    FROM --platform=linux/amd64 {BASE} AS build
    FROM build AS final
    FROM scratch
    """
    images = parse_base_images(content)
    assert [image.reference for image in images] == [BASE]
    assert images[0].stage == "build"


def test_parser_rejects_a_tag_only_base_image():
    with pytest.raises(BaseImageNotPinned) as caught:
        parse_base_images("FROM node:20 AS build\n")
    assert caught.value.code == "base_image_not_pinned"
    assert caught.value.line == 1
    assert caught.value.reference == "node:20"


def test_comment_backslash_does_not_hide_the_next_from():
    with pytest.raises(BaseImageNotPinned) as caught:
        parse_base_images("# not a continuation \\\nFROM node:20\n")
    assert caught.value.reference == "node:20"


def test_high_finding_blocks_the_build_and_an_audited_exception_allows_it():
    async def scenario():
        dockerfile = f"FROM {BASE}\nRUN true\n"
        read_file = lambda path: dockerfile.encode("utf-8")  # noqa: E731
        strategy = resolve_strategy(
            _index(dockerfile), None, read_file=read_file
        )
        finding = ScanFinding(
            vulnerability_id="CVE-2026-0001",
            package="libssl",
            installed_version="1.0",
            severity=Severity.high,
        )
        scanner = StaticImageScanner(
            {BASE: ImageScanReport(image=BASE, findings=(finding,))}
        )
        audit = InMemoryAuditLog()
        controls, exceptions = _controls(audit, scanner)
        claim = make_claim(org_id="org-a", project_id="project-a")

        with pytest.raises(ImageScanBlocked):
            await controls.preflight(
                claim=claim,
                strategy=strategy,
                build_image=builder_ref("dockerfile"),
                read_file=read_file,
                now=NOW,
            )

        await exceptions.grant(
            org_id="org-a",
            image=BASE,
            reason="temporary base image exception for a reviewed CVE",
            granted_by="security-reviewer",
            expires_at=NOW + timedelta(hours=1),
        )
        plan = await controls.preflight(
            claim=claim,
            strategy=strategy,
            build_image=builder_ref("dockerfile"),
            read_file=read_file,
            now=NOW,
        )
        assert plan.base_image_scans
        assert plan.base_image_scans[0].exception is not None
        assert any("audited exception" in warning for warning in plan.warnings)
        assert any(
            row.action == ACTION_IMAGE_SCAN_EXCEPTION_USED for row in audit.rows
        )

    asyncio.run(scenario())
