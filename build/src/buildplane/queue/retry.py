"""Retry classification and backoff for SPEC-11 R-16.

The pipeline already labels expected failures as retryable at the stage that
raised them; this module turns that label plus the job's attempt count into one
decision. It is pure so the reporter and tests share the same rule.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

DEFAULT_MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Whether this failure gets another attempt, and why not if it does not."""

    retry: bool
    dead_letter: bool
    delay_seconds: int | None
    next_attempt: int


def backoff_seconds(attempts: int) -> int:
    """Deterministic exponential backoff, capped at five minutes."""

    return min(MAX_BACKOFF_SECONDS, 5 * (2 ** min(max(attempts, 0), 6)))


def stable_jitter(job_id: str, *, max_seconds: int = 2) -> int:
    """Return a stable 0..max_seconds jitter for one job.

    R-16 asks for jitter on registry rate limits. A stable value keeps a retry
    deterministic for tests while still spreading different jobs apart.
    """

    if max_seconds <= 0:
        return 0
    digest = hashlib.sha256(str(job_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (max_seconds + 1)


def decide_retry(
    *,
    attempts: int,
    max_attempts: int,
    retryable: bool,
) -> RetryDecision:
    """Map one completed attempt to retry, dead-letter or terminal failure.

    ``attempts`` is the count already recorded on the job before this failure.
    A job with attempts=0 and max_attempts=3 therefore gets two retries before
    the third failure lands in the DLQ.
    """

    if attempts < 0:
        raise ValueError("attempts must not be negative")
    if max_attempts < 0:
        raise ValueError("max_attempts must not be negative")
    if not retryable:
        return RetryDecision(
            retry=False,
            dead_letter=False,
            delay_seconds=None,
            next_attempt=attempts,
        )
    next_attempt = attempts + 1
    if max_attempts <= 0 or next_attempt >= max_attempts:
        return RetryDecision(
            retry=False,
            dead_letter=True,
            delay_seconds=None,
            next_attempt=next_attempt,
        )
    return RetryDecision(
        retry=True,
        dead_letter=False,
        delay_seconds=backoff_seconds(attempts),
        next_attempt=next_attempt,
    )
