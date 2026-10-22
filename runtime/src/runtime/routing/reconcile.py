"""Polling routing-config reconciler (SPEC-16 R-1, R-3..R-5, R-19..R-21).

There is no event path and no notifier.  Each poll renders a complete desired
config and compares its fingerprint with what the edge reports.  A missing or
different fingerprint triggers one atomic whole-config apply.  Render or apply
failures leave the previous config untouched and are retried next poll.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from runtime.ingress.models import IngressSnapshot
from runtime.ingress.render import IngressConfig
from runtime.ingress.routing import MAX_REWRITE_DEPTH
from runtime.routing.config import (
    DEFAULT_RATE_LIMIT,
    RoutingConfig,
    render_routing_config,
)
from runtime.routing.models import RoutingReconcileResult
from runtime.routing.ratelimit import RateLimitPolicy


class RoutingSource(Protocol):
    """Read the complete route/domain snapshot for one poll."""

    async def snapshot(self) -> IngressSnapshot: ...


class EdgeConfigApi(Protocol):
    """Whole-config edge API.

    ``apply`` must replace the configuration atomically: a partially written
    config is worse than the previous one.  ``current_fingerprint`` returns
    ``None`` when the edge has no config, which is also how a deleted config is
    detected and recreated on the next poll.
    """

    async def current_fingerprint(self) -> str | None: ...

    async def apply(self, config: RoutingConfig) -> None: ...


class InMemoryEdgeConfigApi:
    """Reference edge API used by local tests and the in-process facade."""

    def __init__(self) -> None:
        self._current: RoutingConfig | None = None
        self.apply_calls = 0
        self.changes = 0

    async def current_fingerprint(self) -> str | None:
        return self._current.fingerprint if self._current is not None else None

    async def apply(self, config: RoutingConfig) -> None:
        self.apply_calls += 1
        if (
            self._current is not None
            and self._current.fingerprint == config.fingerprint
        ):
            return
        self._current = config
        self.changes += 1

    @property
    def current(self) -> RoutingConfig | None:
        return self._current

    def clear(self) -> None:
        """Simulate the edge losing its full config between polls."""

        self._current = None


class RoutingReconciler:
    """Converge the edge's routing config toward the route table."""

    def __init__(
        self,
        source: RoutingSource,
        edge: EdgeConfigApi,
        ingress_config: IngressConfig,
        *,
        rate_limit: RateLimitPolicy = DEFAULT_RATE_LIMIT,
        max_rewrites: int = MAX_REWRITE_DEPTH,
        poll_interval: float = 5.0,
    ) -> None:
        if poll_interval < 0:
            raise ValueError("poll interval must not be negative")
        self._source = source
        self._edge = edge
        self._ingress_config = ingress_config
        self._rate_limit = rate_limit
        self._max_rewrites = max_rewrites
        self._poll_interval = poll_interval
        self._last_good: RoutingConfig | None = None

    @property
    def last_good(self) -> RoutingConfig | None:
        return self._last_good

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def _failure(self, error: Exception) -> RoutingReconcileResult:
        last = self._last_good
        return RoutingReconcileResult(
            changed=False,
            failed=True,
            retained=last is not None,
            routes=last.route_count if last is not None else 0,
            fingerprint=last.fingerprint if last is not None else None,
            error=f"{type(error).__name__}: {error}",
        )

    async def run_once(self) -> RoutingReconcileResult:
        """One full poll.  Never raises for source/render/edge failures."""

        try:
            snapshot = await self._source.snapshot()
            desired = render_routing_config(
                snapshot,
                self._ingress_config,
                rate_limit=self._rate_limit,
                max_rewrites=self._max_rewrites,
            )
        except Exception as exc:  # noqa: BLE001 - retain last-good config
            return self._failure(exc)

        try:
            current = await self._edge.current_fingerprint()
            if current == desired.fingerprint:
                self._last_good = desired
                return RoutingReconcileResult(
                    changed=False,
                    failed=False,
                    retained=False,
                    routes=desired.route_count,
                    fingerprint=desired.fingerprint,
                )
            await self._edge.apply(desired)
        except Exception as exc:  # noqa: BLE001 - retain last-good config
            return self._failure(exc)

        self._last_good = desired
        return RoutingReconcileResult(
            changed=True,
            failed=False,
            retained=False,
            routes=desired.route_count,
            fingerprint=desired.fingerprint,
        )

    async def run_forever(
        self,
        *,
        max_cycles: int | None = None,
        interval_seconds: float | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> tuple[RoutingReconcileResult, ...]:
        """Poll until stopped or ``max_cycles`` polls have completed."""

        if max_cycles is not None and max_cycles < 0:
            raise ValueError("max_cycles must not be negative")
        interval = self._poll_interval if interval_seconds is None else interval_seconds
        if interval < 0:
            raise ValueError("poll interval must not be negative")

        results: list[RoutingReconcileResult] = []
        while max_cycles is None or len(results) < max_cycles:
            if stop_event is not None and stop_event.is_set():
                break
            results.append(await self.run_once())
            if max_cycles is not None and len(results) >= max_cycles:
                break
            if stop_event is not None and stop_event.is_set():
                break
            await self._wait(interval, stop_event)
        return tuple(results)

    @staticmethod
    async def _wait(interval: float, stop_event: asyncio.Event | None) -> None:
        if interval <= 0:
            return
        if stop_event is None:
            await asyncio.sleep(interval)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass


__all__ = (
    "EdgeConfigApi",
    "InMemoryEdgeConfigApi",
    "RoutingReconciler",
    "RoutingSource",
)
