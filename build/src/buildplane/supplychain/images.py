"""Base-image scanning and time-boxed exceptions (SPEC-10 R-16/R-17).

The scanner is a port: the reference production adapter runs Trivy, while tests
inject deterministic reports. A HIGH or CRITICAL finding blocks the build unless
the organisation holds an unexpired exception recorded in ``audit_log``. There
is no command-line or manifest override.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from buildplane.supplychain.audit import (
    ACTION_IMAGE_SCAN_EXCEPTION,
    AuditLog,
    AuditRecord,
)
from buildplane.supplychain.errors import (
    ImageScanBlocked,
    ImageScanUnavailable,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Severity(StrEnum):
    unknown = "unknown"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"

    @classmethod
    def parse(cls, value: str) -> "Severity":
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.unknown


@dataclass(frozen=True, slots=True)
class ScanFinding:
    vulnerability_id: str
    package: str
    installed_version: str
    severity: Severity
    fixed_version: str | None = None

    def __str__(self) -> str:
        fixed = f" (fixed in {self.fixed_version})" if self.fixed_version else ""
        return (
            f"{self.severity.value.upper()} {self.vulnerability_id} in "
            f"{self.package}@{self.installed_version}{fixed}"
        )


@dataclass(frozen=True, slots=True)
class ImageScanReport:
    image: str
    findings: tuple[ScanFinding, ...] = ()

    @property
    def blocking(self) -> tuple[ScanFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity in (Severity.high, Severity.critical)
        )


class ImageScanner(Protocol):
    async def scan(self, image: str) -> ImageScanReport: ...


class StaticImageScanner:
    def __init__(
        self,
        reports: Mapping[str, ImageScanReport] | None = None,
        *,
        default: ImageScanReport | None = None,
    ) -> None:
        self._reports = dict(reports or {})
        self._default = default

    async def scan(self, image: str) -> ImageScanReport:
        report = self._reports.get(image)
        if report is not None:
            return report
        if self._default is not None:
            return ImageScanReport(image=image, findings=self._default.findings)
        return ImageScanReport(image=image)


class TrivyImageScanner:
    """Run Trivy in JSON mode and parse its finding list.

    Trivy's vulnerability database must be available to the worker. A missing
    binary, timeout or non-zero exit is ``ImageScanUnavailable``; it never
    degrades to "no findings".
    """

    def __init__(
        self,
        *,
        binary: str = "trivy",
        timeout_seconds: int = 300,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    async def scan(self, image: str) -> ImageScanReport:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "image",
                "--quiet",
                "--format",
                "json",
                "--exit-code",
                "0",
                image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise ImageScanUnavailable(
                f"image scanner could not start: {type(exc).__name__}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ImageScanUnavailable("image scan timed out") from None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:200]
            raise ImageScanUnavailable(
                f"image scanner exited with code {process.returncode}: {detail}"
            )
        return parse_trivy_report(image, stdout)


def parse_trivy_report(
    image: str,
    payload: bytes | str | Mapping[str, Any],
) -> ImageScanReport:
    if isinstance(payload, bytes):
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageScanUnavailable("image scanner returned invalid JSON") from exc
    elif isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ImageScanUnavailable("image scanner returned invalid JSON") from exc
    else:
        document = payload
    if not isinstance(document, Mapping):
        raise ImageScanUnavailable("image scanner returned an invalid report")
    findings: list[ScanFinding] = []
    results = document.get("Results", [])
    if not isinstance(results, list):
        raise ImageScanUnavailable("image scanner report has no Results list")
    for result in results:
        if not isinstance(result, Mapping):
            continue
        vulnerabilities = result.get("Vulnerabilities")
        if not isinstance(vulnerabilities, list):
            continue
        for item in vulnerabilities:
            if not isinstance(item, Mapping):
                continue
            vulnerability_id = str(item.get("VulnerabilityID") or "UNKNOWN")
            package = str(item.get("PkgName") or "unknown")
            installed = str(item.get("InstalledVersion") or "unknown")
            severity = Severity.parse(str(item.get("Severity") or "UNKNOWN"))
            fixed = item.get("FixedVersion")
            findings.append(
                ScanFinding(
                    vulnerability_id=vulnerability_id,
                    package=package,
                    installed_version=installed,
                    severity=severity,
                    fixed_version=str(fixed) if fixed else None,
                )
            )
    return ImageScanReport(image=image, findings=tuple(findings))


@dataclass(frozen=True, slots=True)
class ImageScanException:
    org_id: str
    image: str
    reason: str
    granted_by: str
    expires_at: datetime


class AuditLogImageScanExceptionStore:
    """Exceptions live in ``audit_log``; grants are append-only audit rows."""

    def __init__(self, audit_log: AuditLog, *, clock=_utcnow) -> None:
        self._audit_log = audit_log
        self._clock = clock

    async def grant(
        self,
        *,
        org_id: str,
        image: str,
        reason: str,
        granted_by: str,
        expires_at: datetime,
    ) -> ImageScanException:
        if not image.strip():
            raise ValueError("image is required")
        if not reason.strip():
            raise ValueError("reason is required")
        if not granted_by.strip():
            raise ValueError("granted_by is required")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= self._clock():
            raise ValueError("expires_at must be in the future")
        exception = ImageScanException(
            org_id=org_id,
            image=image,
            reason=reason,
            granted_by=granted_by,
            expires_at=expires_at,
        )
        await self._audit_log.write(
            AuditRecord(
                action=ACTION_IMAGE_SCAN_EXCEPTION,
                actor_type="user",
                actor_id=granted_by,
                org_id=org_id,
                target_type="image",
                target_id=image,
                metadata={
                    "image": image,
                    "reason": reason,
                    "granted_by": granted_by,
                    "expires_at": expires_at.isoformat(),
                },
            )
        )
        return exception

    async def active(
        self,
        *,
        org_id: str,
        image: str,
        at: datetime,
    ) -> ImageScanException | None:
        try:
            metadata = await self._audit_log.active_image_exception(
                org_id=org_id,
                image=image,
                at=at,
            )
        except Exception as exc:
            raise ImageScanUnavailable(
                f"image exception lookup failed: {type(exc).__name__}"
            ) from exc
        if metadata is None:
            return None
        expires_at = _parse_time(metadata.get("expires_at"))
        if expires_at is None or expires_at <= at:
            return None
        return ImageScanException(
            org_id=org_id,
            image=image,
            reason=str(metadata.get("reason") or ""),
            granted_by=str(metadata.get("granted_by") or ""),
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class ImageScanDecision:
    report: ImageScanReport
    exception: ImageScanException | None = None


class ImageScanGate:
    def __init__(
        self,
        scanner: ImageScanner,
        exceptions: AuditLogImageScanExceptionStore,
    ) -> None:
        self.scanner = scanner
        self.exceptions = exceptions

    async def check(
        self,
        *,
        image: str,
        org_id: str,
        at: datetime,
    ) -> ImageScanDecision:
        try:
            report = await self.scanner.scan(image)
        except ImageScanUnavailable:
            raise
        except Exception as exc:
            raise ImageScanUnavailable(
                f"image scan failed: {type(exc).__name__}"
            ) from exc
        if report.image != image:
            report = ImageScanReport(image=image, findings=report.findings)
        blocking = report.blocking
        if not blocking:
            return ImageScanDecision(report=report)
        exception = await self.exceptions.active(org_id=org_id, image=image, at=at)
        if exception is not None:
            return ImageScanDecision(report=report, exception=exception)
        raise ImageScanBlocked(image, blocking)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None
