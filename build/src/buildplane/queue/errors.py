"""Queue admission failures (SPEC-11 R-6/R-8 and §9)."""

from __future__ import annotations


class QueueError(Exception):
    """Base class for expected build-queue failures."""


class QueueBackpressure(QueueError):
    """The enqueue path deliberately refused new work.

    ``QueueBackpressure`` is a domain outcome, not an infrastructure error: the
    caller should surface it to the customer instead of retrying the build.
    """

    status_code = 429
    code = "build_queue_backpressure"

    def __init__(
        self,
        message: str = "Build queue is at capacity. Try again shortly.",
        *,
        status_code: int | None = None,
        retry_after_seconds: int = 5,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code if status_code is not None else type(self).status_code
        self.retry_after_seconds = retry_after_seconds


class QueueFull(QueueBackpressure):
    """A free-plan org tried to enqueue while the global queue was full."""

    code = "build_queue_full"

    def __init__(
        self,
        message: str = "Build queue full. Try again shortly.",
        *,
        retry_after_seconds: int = 5,
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds)


class QuotaExceeded(QueueBackpressure):
    """The org's enqueue rate/build quota is exhausted (SPEC-20)."""

    code = "quota_exceeded"

    def __init__(
        self,
        message: str = "Build quota exceeded. Try again shortly.",
        *,
        retry_after_seconds: int = 60,
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds)


class QueueUnavailable(QueueError):
    """A queue dependency (Redis for the enqueue counter) is unavailable.

    This is deliberately not a domain outcome. The producer must roll back the
    deployment insert; the orphan reconciler can re-enqueue later (SPEC-11 §9).
    """

    status_code = 503
    code = "build_queue_unavailable"

    def __init__(self, message: str = "Build queue backend unavailable.") -> None:
        super().__init__(message)
        self.message = message
