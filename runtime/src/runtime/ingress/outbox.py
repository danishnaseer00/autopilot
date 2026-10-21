"""Durable retry/outbox for SPEC-15 side effects.

Polling convergence is the retry mechanism for *ingress desired state*: the
table is the source of truth, so a missed apply or a partial apply is repaired
by the next full render.  Some operations cannot be expressed that way - asking
cert-manager to issue/revoke a certificate, or re-checking a DNS proof - because
they have an external side effect and must not be lost when a pod dies between
the database commit and the API call.

``OutboxStore`` is that durable seam.  The SQL implementation lives in
``runtime.ingress.postgres`` and uses ``SELECT ... FOR UPDATE SKIP LOCKED``;
the in-memory implementation is the reference for local tests.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

from runtime.ingress.errors import OutboxError, OutboxMessageNotFound
from runtime.ingress.models import OutboxMessage

Handler = Callable[[OutboxMessage], Awaitable[None]]


class OutboxStore(Protocol):
    async def enqueue(
        self,
        topic: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        available_at: datetime | None = None,
        max_attempts: int = 8,
    ) -> OutboxMessage: ...

    async def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxMessage | None: ...

    async def complete(
        self, message_id: str, *, worker_id: str, now: datetime
    ) -> OutboxMessage: ...

    async def retry(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
        now: datetime,
        available_at: datetime,
    ) -> OutboxMessage: ...

    async def get(self, message_id: str) -> OutboxMessage | None: ...


class InMemoryOutbox:
    """Reference outbox with lease expiry and idempotent enqueue."""

    def __init__(self) -> None:
        self._messages: dict[str, OutboxMessage] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        topic: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        available_at: datetime | None = None,
        max_attempts: int = 8,
    ) -> OutboxMessage:
        if not topic or not idempotency_key:
            raise OutboxError("outbox topic and idempotency key are required")
        if max_attempts < 1:
            raise OutboxError("max_attempts must be positive")
        async with self._lock:
            existing_id = self._by_key.get((topic, idempotency_key))
            if existing_id is not None:
                return self._messages[existing_id]
            message = OutboxMessage(
                id=str(uuid.uuid4()),
                topic=topic,
                idempotency_key=idempotency_key,
                payload=copy.deepcopy(dict(payload)),
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                available_at=available_at or now,
                created_at=now,
            )
            self._messages[message.id] = message
            self._by_key[(topic, idempotency_key)] = message.id
            return message

    async def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxMessage | None:
        if lease_seconds < 1:
            raise OutboxError("lease_seconds must be positive")
        async with self._lock:
            due = [
                message
                for message in self._messages.values()
                if (
                    message.status == "pending"
                    and message.available_at <= now
                )
                or (
                    message.status == "running"
                    and message.lease_expires_at is not None
                    and message.lease_expires_at <= now
                )
            ]
            if not due:
                return None
            message = min(due, key=lambda item: (item.available_at, item.id))
            claimed = OutboxMessage(
                **{
                    **message.__dict__,
                    "status": "running",
                    "attempts": message.attempts + 1,
                    "worker_id": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            self._messages[claimed.id] = claimed
            return claimed

    async def complete(
        self, message_id: str, *, worker_id: str, now: datetime
    ) -> OutboxMessage:
        return await self._finish(
            message_id,
            worker_id=worker_id,
            status="succeeded",
            last_error=None,
            now=now,
        )

    async def retry(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
        now: datetime,
        available_at: datetime,
    ) -> OutboxMessage:
        current = await self._require_claimed(message_id, worker_id)
        status = "dead" if current.attempts >= current.max_attempts else "pending"
        async with self._lock:
            updated = OutboxMessage(
                **{
                    **current.__dict__,
                    "status": status,
                    "last_error": error[:500],
                    "available_at": available_at,
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            self._messages[updated.id] = updated
            return updated

    async def get(self, message_id: str) -> OutboxMessage | None:
        return self._messages.get(str(message_id))

    async def all(self) -> tuple[OutboxMessage, ...]:
        return tuple(
            sorted(self._messages.values(), key=lambda item: (item.created_at, item.id))
        )

    async def _require_claimed(
        self, message_id: str, worker_id: str
    ) -> OutboxMessage:
        current = self._messages.get(str(message_id))
        if current is None:
            raise OutboxMessageNotFound(str(message_id))
        if current.status != "running" or current.worker_id != worker_id:
            raise OutboxError(
                f"outbox message {message_id} is not owned by worker {worker_id!r}"
            )
        return current

    async def _finish(
        self,
        message_id: str,
        *,
        worker_id: str,
        status: str,
        last_error: str | None,
        now: datetime,
    ) -> OutboxMessage:
        current = await self._require_claimed(message_id, worker_id)
        async with self._lock:
            updated = OutboxMessage(
                **{
                    **current.__dict__,
                    "status": status,
                    "last_error": last_error,
                    "worker_id": None,
                    "lease_expires_at": None,
                }
            )
            self._messages[updated.id] = updated
            return updated


class OutboxDispatcher:
    """Claim one due message, run its handler, then complete/retry it.

    Delivery is at-least-once: an expired lease can cause a second worker to
    re-run a handler whose side effect already happened.  Handlers must
    therefore be idempotent (for example keyed by ``message.id`` or the
    certificate id in the payload).
    """

    def __init__(
        self,
        store: OutboxStore,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 30,
        backoff_base_seconds: int = 5,
        backoff_cap_seconds: int = 300,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if backoff_base_seconds < 1 or backoff_cap_seconds < backoff_base_seconds:
            raise ValueError("invalid outbox backoff")
        self._store = store
        self._handlers = dict(handlers)
        self._worker_id = worker_id or f"ingress-{uuid.uuid4().hex[:8]}"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_seconds = lease_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def run_once(self) -> OutboxMessage | None:
        now = self._clock()
        message = await self._store.claim(
            worker_id=self._worker_id, now=now, lease_seconds=self._lease_seconds
        )
        if message is None:
            return None

        if message.attempts > message.max_attempts:
            return await self._store.retry(
                message.id,
                worker_id=self._worker_id,
                error="attempt limit exceeded before handler ran",
                now=now,
                available_at=now,
            )

        handler = self._handlers.get(message.topic)
        if handler is None:
            return await self._retry(
                message,
                error=f"no handler registered for topic {message.topic!r}",
                now=now,
            )

        try:
            await handler(message)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            return await self._retry(message, error=error, now=now)
        return await self._store.complete(
            message.id, worker_id=self._worker_id, now=now
        )

    async def run_until_idle(self, *, max_messages: int = 100) -> int:
        processed = 0
        while processed < max_messages:
            result = await self.run_once()
            if result is None:
                break
            processed += 1
        return processed

    async def _retry(
        self, message: OutboxMessage, *, error: str, now: datetime
    ) -> OutboxMessage:
        delay = min(
            self._backoff_cap,
            self._backoff_base * (2 ** max(message.attempts - 1, 0)),
        )
        return await self._store.retry(
            message.id,
            worker_id=self._worker_id,
            error=error,
            now=now,
            available_at=now + timedelta(seconds=delay),
        )


__all__ = [
    "Handler",
    "InMemoryOutbox",
    "OutboxDispatcher",
    "OutboxStore",
]
