"""Named failures for the SPEC-09 build pipeline.

The job lifecycle (SPEC-09 §3.1) makes retryability part of the failure, not a
property of the caller. Every exception raised across a stage therefore says
which stage failed and whether the queue may retry it. The worker/reporting
layer never has to infer that from an exception type or a message string.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base for expected pipeline failures.

    ``stage`` is the SPEC-09 stage name (``fetch``, ``detect``, ``build``).
    ``retryable`` maps directly onto SPEC-09 §3.1's transient/permanent split.
    Unexpected exceptions are not wrapped: they still fail the job, but the
    pipeline marks them permanent because retrying an unknown bug is not a
    recovery strategy.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str = "unknown",
        retryable: bool = False,
        exit_code: int | None = None,
        code: str = "pipeline_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.exit_code = exit_code
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return self.message


class FetchError(PipelineError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        code: str = "fetch_failed",
    ) -> None:
        super().__init__(
            message,
            stage="fetch",
            retryable=retryable,
            code=code,
        )
        self.status_code = status_code


class ExtractionError(PipelineError):
    def __init__(self, message: str, *, code: str = "unsafe_archive") -> None:
        super().__init__(
            message,
            stage="fetch",
            retryable=False,
            code=code,
        )


class ManifestError(PipelineError):
    def __init__(self, message: str, *, code: str = "manifest_invalid") -> None:
        super().__init__(
            message,
            stage="detect",
            retryable=False,
            code=code,
        )


class StrategyError(PipelineError):
    """Detection could not produce a build plan (R-17: never guess)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "unrecognised_project",
        detected: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            message,
            stage="detect",
            retryable=False,
            code=code,
        )
        self.detected = detected


class SandboxFailure(PipelineError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        retryable: bool = False,
        code: str = "sandbox_failed",
    ) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=retryable,
            exit_code=exit_code,
            code=code,
        )


class SandboxTimeout(SandboxFailure):
    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            f"build exceeded its {timeout_seconds}s wall-clock timeout",
            retryable=False,
            code="build_timeout",
        )


class SecretRedactionUnavailable(PipelineError):
    """Fail closed rather than persist logs that may contain a known secret."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=False,
            code="redaction_unavailable",
        )


class SecretResolutionError(PipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=False,
            code="secret_resolution_failed",
        )


class CacheError(PipelineError):
    """Cache failures are never fatal; they degrade to a cold build (R-27)."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=False,
            code="cache_failed",
        )


class LogStoreError(PipelineError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            stage="build",
            retryable=True,
            code="log_store_unavailable",
        )


class LeaseLost(PipelineError):
    """Another worker may own the job now; do not report a result over it."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"lease for build job {job_id} was lost",
            stage="build",
            retryable=True,
            code="lease_lost",
        )
