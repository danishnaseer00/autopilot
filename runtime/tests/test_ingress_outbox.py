"""SPEC-15 durable retry/outbox semantics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from runtime.ingress import InMemoryOutbox, OutboxDispatcher

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def test_enqueue_is_idempotent_by_topic_and_key():
    store = InMemoryOutbox()
    first = asyncio.run(
        store.enqueue("certificate.issue", "key-1", {"host": "acme.com"}, now=NOW)
    )
    second = asyncio.run(
        store.enqueue("certificate.issue", "key-1", {"host": "other.com"}, now=NOW)
    )
    assert first.id == second.id
    assert second.payload == {"host": "acme.com"}
    assert len(asyncio.run(store.all())) == 1


def test_failure_retries_with_backoff_then_dead_letters():
    store = InMemoryOutbox()
    clock = Clock(NOW)
    calls: list[str] = []

    async def fail(message):
        calls.append(message.id)
        raise RuntimeError("ACME unavailable")

    asyncio.run(store.enqueue("certificate.issue", "key-1", {}, now=NOW, max_attempts=2))
    dispatcher = OutboxDispatcher(
        store,
        {"certificate.issue": fail},
        clock=clock,
        backoff_base_seconds=5,
        backoff_cap_seconds=30,
    )
    first = asyncio.run(dispatcher.run_once())
    assert first is not None and first.status == "pending"
    assert first.attempts == 1
    assert first.last_error is not None and "ACME unavailable" in first.last_error
    assert first.available_at == NOW + timedelta(seconds=5)

    # Not due yet: durable retry does not hammer the backend.
    assert asyncio.run(dispatcher.run_once()) is None
    clock.advance(seconds=6)
    second = asyncio.run(dispatcher.run_once())
    assert second is not None and second.status == "dead"
    assert second.attempts == 2
    assert len(calls) == 2


def test_expired_lease_is_reclaimed_by_a_new_worker_and_survives_restart():
    store = InMemoryOutbox()
    clock = Clock(NOW)
    message = asyncio.run(
        store.enqueue("certificate.issue", "key-1", {"host": "acme.com"}, now=NOW)
    )
    claimed = asyncio.run(
        store.claim(worker_id="worker-1", now=NOW, lease_seconds=30)
    )
    assert claimed is not None and claimed.worker_id == "worker-1"

    # Simulate worker-1 dying without complete/retry. A different dispatcher
    # (new process) picks up the expired lease from the same durable store.
    clock.advance(seconds=31)
    seen: list[dict] = []

    async def handler(message):
        seen.append(message.payload)

    dispatcher = OutboxDispatcher(
        store, {"certificate.issue": handler}, worker_id="worker-2", clock=clock
    )
    result = asyncio.run(dispatcher.run_once())
    assert result is not None and result.status == "succeeded"
    assert result.attempts == 2
    assert seen == [{"host": "acme.com"}]
    assert asyncio.run(store.get(message.id)).status == "succeeded"


def test_unknown_topic_is_retried_not_silently_dropped():
    store = InMemoryOutbox()
    clock = Clock(NOW)
    asyncio.run(store.enqueue("no.such.topic", "key-1", {}, now=NOW))
    dispatcher = OutboxDispatcher(store, {}, clock=clock)
    result = asyncio.run(dispatcher.run_once())
    assert result is not None and result.status == "pending"
    assert "no handler" in (result.last_error or "")
