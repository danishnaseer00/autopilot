"""Audit writing for SPEC-18 §5.

Every audit row is written **before** the value is returned or the mutation is
applied (SPEC-02 R-9). If the write fails, the operation fails: AC-10 says
"killing the audit write also fails the resolution", so there is deliberately no
best-effort/background path here.

The audit database is separate (SPEC-03 §3); the app holds no BYPASSRLS role.
Rows never contain a secret value - only key names, ids, and outcomes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import asyncpg

from runtime.secrets.errors import AuditWriteFailed

_ACTOR_TYPES = frozenset({"user", "agent", "system"})


@dataclass(frozen=True)
class AuditRecord:
    action: str
    actor_type: str
    actor_id: str
    org_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.actor_type not in _ACTOR_TYPES:
            # audit_log's CHECK would reject it anyway; fail with a useful name.
            raise ValueError(f"actor_type must be one of {sorted(_ACTOR_TYPES)}")


class AuditSink(Protocol):
    async def write(self, record: AuditRecord) -> None: ...


class AsyncpgAuditSink:
    """Writes to ``audit_log`` in the ``autopilot_audit`` database.

    Set the role's DSN in ``AUDIT_DATABASE_URL`` (``autopilot_audit``). The sink
    takes a pool, not a connection, so audit does not accidentally join the
    tenant transaction it is auditing.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write(self, record: AuditRecord) -> None:
        try:
            await self._pool.execute(
                "INSERT INTO audit_log "
                "  (org_id, actor_type, actor_id, action, target_type, "
                "   target_id, metadata) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
                record.org_id,
                record.actor_type,
                record.actor_id,
                record.action,
                record.target_type,
                record.target_id,
                json.dumps(record.metadata or {}),
            )
        except Exception as exc:  # noqa: BLE001 - boundary: any failure is fatal
            # Never include the DB message verbatim: a driver error can echo
            # parameters, and this boundary must not become a value leak (§8).
            raise AuditWriteFailed(
                f"audit write failed for {record.action}; operation refused"
            ) from exc


class InMemoryAuditSink:
    """Test/demo double. Captures rows; raises on demand to prove AC-10."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: list[AuditRecord] = []
        self.fail = fail

    async def write(self, record: AuditRecord) -> None:
        if self.fail:
            raise AuditWriteFailed("audit sink is unavailable (test double)")
        self.rows.append(record)

    def actions(self) -> list[str]:
        return [row.action for row in self.rows]


__all__ = [
    "AsyncpgAuditSink",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
]
