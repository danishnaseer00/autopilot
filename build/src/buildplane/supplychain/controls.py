"""The SPEC-10 control-plane object used by the build pipeline.

Ordering is the security property:

1. require the lockfile and parse its exact dependency set;
2. refuse globally denylisted versions before anything is cached or built;
3. decide install-script permissions from the audited exception table;
4. enforce the base-image scan gate;
5. record/compare the lockfile hash;
6. verify provenance as a warning-only signal.

The guard returns a plan object rather than side-effecting the sandbox config.
Exceptions only enter the command argv (``npm rebuild <pkg>``); the container
baseline is untouched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TYPE_CHECKING

from buildplane.pipeline.models import ClaimedJob
from buildplane.pipeline.commands import supports_install_script_rebuild
from buildplane.supplychain.audit import (
    ACTION_IMAGE_SCAN_EXCEPTION_USED,
    ACTION_INSTALL_SCRIPT_USED,
    ACTION_LOCKFILE_CHANGED,
    ACTION_LOCKFILE_OBSERVED,
    ACTION_PROVENANCE,
    ACTION_REGISTRY_DENIED,
    AuditLog,
    AuditRecord,
)
from buildplane.supplychain.base_images import parse_base_images
from buildplane.supplychain.denylist import Denylist
from buildplane.supplychain.errors import DenylistedPackage, SupplyChainError
from buildplane.supplychain.images import ImageScanDecision, ImageScanGate
from buildplane.supplychain.install_scripts import (
    BuildScriptExceptionStore,
    decide_install_scripts,
)
from buildplane.supplychain.lockfiles import (
    LockfileInfo,
    lockfile_required,
    missing_lockfile,
    parse_locked_packages,
    verify_lockfile_unchanged,
)
from buildplane.supplychain.models import LockedPackage
from buildplane.supplychain.provenance import (
    ProvenanceVerifier,
    verify_packages,
)

if TYPE_CHECKING:
    from buildplane.pipeline.strategy import ResolvedStrategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SupplyChainPlan:
    lockfile: LockfileInfo
    packages: tuple[LockedPackage, ...]
    allowed_install_scripts: tuple[str, ...]
    notices: tuple[str, ...]
    warnings: tuple[str, ...]
    image_scan: ImageScanDecision | None = None
    base_image_scans: tuple[ImageScanDecision, ...] = ()

    @property
    def cache_component(self) -> str | None:
        if not self.allowed_install_scripts:
            return None
        payload = "\x00".join(sorted(self.allowed_install_scripts))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"scripts={digest}"

    def effective_strategy_id(self, base: str) -> str:
        if self.cache_component is None:
            return base
        return f"{base}:supply={self.cache_component}"

    def effective_lockfile_hash(self, base: str) -> str:
        if self.cache_component is None:
            return base
        payload = f"{base}\x00{self.cache_component}".encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


class SupplyChainControls:
    def __init__(
        self,
        *,
        audit_log: AuditLog,
        denylist: Denylist,
        install_script_exceptions: BuildScriptExceptionStore,
        image_scan_gate: ImageScanGate,
        provenance: ProvenanceVerifier | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.audit_log = audit_log
        self.denylist = denylist
        self.install_script_exceptions = install_script_exceptions
        self.image_scan_gate = image_scan_gate
        self.provenance = provenance
        self._clock = clock

    async def preflight(
        self,
        *,
        claim: ClaimedJob,
        strategy: "ResolvedStrategy",
        build_image: str,
        read_file: Callable[[str], bytes | None],
        now: datetime | None = None,
    ) -> SupplyChainPlan:
        at = now or self._clock()
        info = strategy.lockfile
        plan = strategy.plan
        if lockfile_required(plan) and not info.present:
            error = missing_lockfile(plan)
            assert error is not None
            raise error

        packages: tuple[LockedPackage, ...] = ()
        if info.present and info.name is not None and info.content is not None:
            packages = parse_locked_packages(info.name, info.content)

        notices: list[str] = []
        warnings: list[str] = []
        await self._check_denylist(claim, packages)
        allowed, script_notices, script_warnings = await self._install_scripts(
            claim, plan, packages, at
        )
        notices.extend(script_notices)
        warnings.extend(script_warnings)

        image_decision = await self._scan_image(
            claim,
            image=build_image,
            at=at,
            label="builder",
            notices=notices,
            warnings=warnings,
        )

        base_image_scans: list[ImageScanDecision] = []
        scanned = {build_image: image_decision}
        if strategy.dockerfile is not None:
            content = read_file(strategy.dockerfile)
            if content is None:
                raise SupplyChainError(
                    f"{strategy.dockerfile} is missing from the build context",
                    stage="build",
                    code="dockerfile_missing",
                )
            for base in parse_base_images(
                content, filename=strategy.dockerfile
            ):
                if base.reference in scanned:
                    base_image_scans.append(scanned[base.reference])
                    continue
                decision = await self._scan_image(
                    claim,
                    image=base.reference,
                    at=at,
                    label=f"base image from {strategy.dockerfile}:{base.line}",
                    notices=notices,
                    warnings=warnings,
                )
                scanned[base.reference] = decision
                base_image_scans.append(decision)

        await self._lockfile_history(claim, info, at, warnings)
        await self._provenance(claim, packages, notices, warnings)
        return SupplyChainPlan(
            lockfile=info,
            packages=packages,
            allowed_install_scripts=allowed,
            notices=tuple(notices),
            warnings=tuple(warnings),
            image_scan=image_decision,
            base_image_scans=tuple(base_image_scans),
        )

    async def _scan_image(
        self,
        claim: ClaimedJob,
        *,
        image: str,
        at: datetime,
        label: str,
        notices: list[str],
        warnings: list[str],
    ) -> ImageScanDecision:
        decision = await self.image_scan_gate.check(
            image=image,
            org_id=claim.org_id,
            at=at,
        )
        if decision.exception is not None:
            exception = decision.exception
            warnings.append(
                f"{label} scan findings accepted by audited exception until "
                f"{exception.expires_at.isoformat()}"
            )
            await self._write(
                AuditRecord(
                    action=ACTION_IMAGE_SCAN_EXCEPTION_USED,
                    actor_type="system",
                    actor_id="build-plane",
                    org_id=claim.org_id,
                    target_type="image",
                    target_id=image,
                    metadata={
                        "image": image,
                        "job_id": claim.job_id,
                        "project_id": claim.project_id,
                        "expires_at": exception.expires_at.isoformat(),
                    },
                )
            )
        elif decision.report.findings:
            notices.append(
                f"{label} scan reported {len(decision.report.findings)} "
                f"finding(s) below the block threshold"
            )
        return decision

    def post_build(
        self,
        plan: SupplyChainPlan,
        *,
        read_file: Callable[[str], bytes | None],
    ) -> None:
        verify_lockfile_unchanged(plan.lockfile, read_file)

    async def _check_denylist(
        self,
        claim: ClaimedJob,
        packages: tuple[LockedPackage, ...],
    ) -> None:
        for package in packages:
            try:
                advisory = await self.denylist.advisory_for(
                    package.name, package.version
                )
            except Exception as exc:
                raise SupplyChainError(
                    f"denylist lookup failed: {type(exc).__name__}",
                    code="denylist_unavailable",
                ) from exc
            if advisory is None:
                continue
            await self._write(
                AuditRecord(
                    action=ACTION_REGISTRY_DENIED,
                    actor_type="system",
                    actor_id="build-plane",
                    org_id=claim.org_id,
                    target_type="package",
                    target_id=f"{package.name}@{package.version}",
                    metadata={
                        "package": package.name,
                        "version": package.version,
                        "advisory_id": advisory.advisory_id,
                        "incident_id": advisory.incident_id,
                        "summary": advisory.summary,
                        "project_id": claim.project_id,
                        "build_id": claim.job_id,
                    },
                )
            )
            raise DenylistedPackage(advisory)

    async def _install_scripts(
        self,
        claim: ClaimedJob,
        plan,
        packages: tuple[LockedPackage, ...],
        at: datetime,
    ) -> tuple[tuple[str, ...], list[str], list[str]]:
        try:
            active = await self.install_script_exceptions.active(
                org_id=claim.org_id,
                project_id=claim.project_id,
                at=at,
            )
        except Exception as exc:
            raise SupplyChainError(
                f"install-script exception lookup failed: {type(exc).__name__}",
                code="install_script_exception_lookup_failed",
            ) from exc
        decision = decide_install_scripts(packages, active, at=at)
        notices: list[str] = []
        warnings: list[str] = []
        supported = supports_install_script_rebuild(plan)
        if decision.allowed and not supported:
            warnings.append(
                "install-script exceptions cannot be applied to this build "
                "strategy; scripts remain disabled"
            )
        else:
            for package in decision.detected:
                if package.name not in decision.allowed:
                    continue
                expires = active.get(package.name)
                notices.append(
                    f"install-script exception applied for {package.name}@"
                    f"{package.version}"
                    + (f" until {expires.isoformat()}" if expires else "")
                )
                await self._write(
                    AuditRecord(
                        action=ACTION_INSTALL_SCRIPT_USED,
                        actor_type="system",
                        actor_id="build-plane",
                        org_id=claim.org_id,
                        target_type="project",
                        target_id=claim.project_id,
                        metadata={
                            "project_id": claim.project_id,
                            "job_id": claim.job_id,
                            "package_name": package.name,
                            "version": package.version,
                            "expires_at": (
                                expires.isoformat() if expires is not None else None
                            ),
                        },
                    )
                )
        if decision.suppressed or (decision.allowed and not supported):
            warnings.append(
                "install scripts are disabled for "
                + _render_packages(decision.suppressed or decision.detected)
                + "; add an audited project exception only if the build requires it"
            )
        if decision.allowed and supported:
            permitted = tuple(
                package
                for package in decision.detected
                if package.name in decision.allowed
            )
            warnings.append(
                "install scripts permitted by audited exception for "
                + _render_packages(permitted)
            )
        return (
            decision.allowed if supported else (),
            notices,
            warnings,
        )

    async def _lockfile_history(
        self,
        claim: ClaimedJob,
        info: LockfileInfo,
        at: datetime,
        warnings: list[str],
    ) -> None:
        if not info.present or info.name is None:
            return
        previous = await self.audit_log.previous_lockfile_hash(
            org_id=claim.org_id,
            project_id=claim.project_id,
            git_sha=claim.git_sha,
            lockfile_name=info.name,
        )
        if previous is not None and previous != info.hash:
            warnings.append(
                f"{info.name} changed between builds of the same commit; "
                "investigate the dependency source"
            )
            await self._write(
                AuditRecord(
                    action=ACTION_LOCKFILE_CHANGED,
                    actor_type="system",
                    actor_id="build-plane",
                    org_id=claim.org_id,
                    target_type="project",
                    target_id=claim.project_id,
                    metadata={
                        "project_id": claim.project_id,
                        "git_sha": claim.git_sha,
                        "lockfile_name": info.name,
                        "previous_hash": previous,
                        "lockfile_hash": info.hash,
                    },
                )
            )
        await self._write(
            AuditRecord(
                action=ACTION_LOCKFILE_OBSERVED,
                actor_type="system",
                actor_id="build-plane",
                org_id=claim.org_id,
                target_type="project",
                target_id=claim.project_id,
                metadata={
                    "project_id": claim.project_id,
                    "git_sha": claim.git_sha,
                    "lockfile_name": info.name,
                    "lockfile_hash": info.hash,
                    "job_id": claim.job_id,
                },
            )
        )

    async def _provenance(
        self,
        claim: ClaimedJob,
        packages: tuple[LockedPackage, ...],
        notices: list[str],
        warnings: list[str],
    ) -> None:
        if self.provenance is None or not packages:
            return
        try:
            report = await verify_packages(self.provenance, packages)
        except Exception as exc:
            warnings.append(
                f"provenance verification unavailable: {type(exc).__name__}"
            )
            return
        if report.verified:
            notices.append(
                f"provenance verified for {len(report.verified)} package(s)"
            )
        if report.missing:
            notices.append(
                f"provenance absent for {len(report.missing)} package(s); "
                "not an error"
            )
        warnings.extend(report.warnings)
        try:
            await self._write(
                AuditRecord(
                    action=ACTION_PROVENANCE,
                    actor_type="system",
                    actor_id="build-plane",
                    org_id=claim.org_id,
                    target_type="project",
                    target_id=claim.project_id,
                    metadata={
                        "project_id": claim.project_id,
                        "job_id": claim.job_id,
                        "verified": len(report.verified),
                        "missing": len(report.missing),
                        "invalid": len(report.invalid),
                        "invalid_packages": [
                            f"{package.name}@{package.version}"
                            for package in report.invalid[:50]
                        ],
                    },
                )
            )
        except Exception as exc:
            warnings.append(
                f"provenance audit write failed: {type(exc).__name__}"
            )

    async def _write(self, record: AuditRecord) -> None:
        await self.audit_log.write(record)


def _render_packages(packages: tuple[LockedPackage, ...]) -> str:
    shown = ", ".join(f"{p.name}@{p.version}" for p in packages[:10])
    if len(packages) > 10:
        shown += f" (+{len(packages) - 10} more)"
    return shown
