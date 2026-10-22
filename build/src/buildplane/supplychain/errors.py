"""Named SPEC-10 failures.

Every failure is a ``PipelineError`` so the existing reporter continues to make
the retryable/permanent split from the exception rather than from call-site
guessing. Security failures are permanent by default: retrying the same
denylisted package or the same vulnerable image does not make it safe.
"""

from __future__ import annotations

from buildplane.pipeline.errors import PipelineError
from buildplane.supplychain.models import Advisory


class SupplyChainError(PipelineError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "detect",
        retryable: bool = False,
        code: str = "supply_chain_failed",
        exit_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            stage=stage,
            retryable=retryable,
            exit_code=exit_code,
            code=code,
        )


class LockfileMissing(SupplyChainError):
    def __init__(self, code: str) -> None:
        super().__init__(
            f"no {code} in the build context; commit a lockfile so dependency "
            "resolution is reproducible (SPEC-10 R-5)",
            code="lockfile_missing",
        )


class LockfileMalformed(SupplyChainError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name} could not be parsed; the build cannot prove its dependency set",
            code="lockfile_malformed",
        )


class LockfileMutated(SupplyChainError):
    def __init__(self, name: str, detail: str) -> None:
        super().__init__(
            f"the build {detail} {name}; lockfiles must be committed, not "
            "generated or modified during a build (SPEC-10 R-6)",
            stage="build",
            code="lockfile_mutated",
        )


class DenylistedPackage(SupplyChainError):
    def __init__(self, advisory: Advisory) -> None:
        super().__init__(
            f"dependency {advisory.package}@{advisory.version} is globally "
            f"blocked by {advisory.advisory_id}: {advisory.summary}",
            code="denylisted_package",
        )
        self.advisory = advisory


class RegistryUnavailable(SupplyChainError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=retryable,
            code="registry_unavailable",
        )


class AuditWriteFailed(SupplyChainError):
    """A control decision could not be recorded, so the decision fails closed."""

    def __init__(self, message: str = "audit log is unavailable") -> None:
        super().__init__(
            message,
            stage="build",
            retryable=True,
            code="audit_unavailable",
        )


class ImageScanBlocked(SupplyChainError):
    def __init__(self, image: str, findings: tuple[object, ...]) -> None:
        rendered = ", ".join(str(finding) for finding in findings[:4])
        suffix = "" if len(findings) <= 4 else f" (+{len(findings) - 4} more)"
        super().__init__(
            f"base image scan blocked {image}: {rendered}{suffix}. Add a "
            "time-boxed, audited image-scan exception only after review",
            stage="build",
            code="image_scan_blocked",
        )
        self.image = image
        self.findings = findings


class BaseImageNotPinned(SupplyChainError):
    def __init__(self, filename: str, line: int, reference: str) -> None:
        if reference == "<missing>":
            detail = "has no image reference"
        else:
            detail = f"references {reference!r} by tag"
        super().__init__(
            f"{filename}:{line} {detail}; every Dockerfile FROM must be "
            "pinned by sha256 digest (SPEC-12 R-12)",
            stage="build",
            retryable=False,
            code="base_image_not_pinned",
        )
        self.filename = filename
        self.line = line
        self.reference = reference


class ImageScanUnavailable(SupplyChainError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=True,
            code="image_scan_unavailable",
        )
