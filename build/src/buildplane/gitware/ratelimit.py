"""Per-project webhook rate accounting (SPEC-08 R-17).

The requirement is unusual on purpose: a burst is *counted and alerted*, never
rejected. GitHub retries are expensive and a 429 would hide the event, so the
receiver keeps processing. The default adapter is process-local; production
injects a shared Redis-backed implementation through the same port.

The threshold is not specified by the spec. 60 deliveries per minute per
project is the conservative default recorded in DECISIONS.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

DEFAULT_BURST_LIMIT = 60
DEFAULT_WINDOW = timedelta(seconds=60)

_logger = logging.getLogger("buildplane.gitware.ratelimit")


class WebhookRateLimiter(Protocol):
    async def record(self, project_id: str, *, now: datetime) -> int:
        """Count one delivery and return the count for the current window."""


AlertCallback = Callable[[str, int], None]


def log_alert(project_id: str, count: int) -> None:
    """Default alert: a structured warning, no payload or token material."""

    _logger.warning(
        "github webhook burst project_id=%s count=%s", project_id, count
    )


@dataclass
class InMemoryRateLimiter:
    limit: int = DEFAULT_BURST_LIMIT
    window: timedelta = DEFAULT_WINDOW
    alert: AlertCallback = log_alert
    _state: dict[str, tuple[datetime, int]] = field(default_factory=dict)
    alerts: list[tuple[str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("rate limit must be positive")
        if self.window <= timedelta(0):
            raise ValueError("rate-limit window must be positive")

    async def record(self, project_id: str, *, now: datetime) -> int:
        start, count = self._state.get(project_id, (now, 0))
        if now - start >= self.window:
            start, count = now, 0
        count += 1
        self._state[project_id] = (start, count)

        # Alert once at the crossing, not on every subsequent delivery; the
        # count itself remains visible in state for metrics/dashboards.
        if count == self.limit + 1:
            self.alerts.append((project_id, count))
            try:
                self.alert(project_id, count)
            except Exception:  # pragma: no cover - alerting must not block
                _logger.exception(
                    "webhook rate-limit alert failed project_id=%s", project_id
                )
        return count

    def count(self, project_id: str) -> int:
        return self._state.get(project_id, (datetime.min, 0))[1]
