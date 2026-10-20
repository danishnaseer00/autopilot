"""SPEC-18 service policy that must hold without a database.

The fork check (R-3), confirmation gates (R-5/§8), and audit-before-return
(AC-10) are properties of the code path, so they are tested with fake
connections here. Storage/RLS/rotation are in test_secrets_contract.py.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from runtime.secrets.audit import InMemoryAuditSink
from runtime.secrets.crypto import aad_for, dek_aad, encrypt
from runtime.secrets.errors import (
    AuditWriteFailed,
    ConfirmationRequired,
    InvalidSecret,
    MasterKeyUnavailable,
    SecretQuotaExceeded,
)
from runtime.secrets.masterkey import StaticMasterKey, UnavailableMasterKey
from runtime.secrets.service import SecretService

ORG = uuid.uuid4()
PROJECT = uuid.uuid4()


class ForbiddenConnection:
    """Any database interaction is a test failure."""

    async def fetch(self, *args, **kwargs):
        raise AssertionError("database must not be touched")

    async def fetchrow(self, *args, **kwargs):
        raise AssertionError("database must not be touched")

    async def fetchval(self, *args, **kwargs):
        raise AssertionError("database must not be touched")

    async def execute(self, *args, **kwargs):
        raise AssertionError("database must not be touched")


class FakeConnection:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


def _service(master=None, audit=None, max_secrets=50) -> SecretService:
    return SecretService(
        master_key=master or StaticMasterKey(os.urandom(32)),
        audit=audit or InMemoryAuditSink(),
        max_secrets_per_project=max_secrets,
    )


def test_fork_build_returns_empty_before_any_lookup() -> None:
    """R-3 is structural: the fork check precedes the query, not a filter."""
    audit = InMemoryAuditSink()
    service = _service(master=UnavailableMasterKey(), audit=audit)

    result = asyncio.run(
        service.resolve_for_build(
            ForbiddenConnection(),  # type: ignore[arg-type]
            org_id=ORG,
            project_id=PROJECT,
            environment="production",
            fork=True,
        )
    )

    assert result == {}
    assert audit.rows == []


def test_build_visibility_requires_confirmation() -> None:
    service = _service()
    with pytest.raises(ConfirmationRequired):
        asyncio.run(
            service.create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="BUILD_TOKEN",
                value="value-long-enough",
                target="build",
                actor_id="user-1",
            )
        )


def test_non_production_environment_requires_confirmation() -> None:
    service = _service()
    with pytest.raises(ConfirmationRequired):
        asyncio.run(
            service.create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="PREVIEW_KEY",
                value="value-long-enough",
                environments=["preview"],
                actor_id="user-1",
            )
        )


def test_update_to_build_visibility_requires_confirmation() -> None:
    class Existing(FakeConnection):
        async def fetchrow(self, sql, *args):
            return {"id": uuid.uuid4(), "target": "runtime", "environments": ["production"]}

    service = _service()
    with pytest.raises(ConfirmationRequired):
        asyncio.run(
            service.update_secret(
                Existing(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="API_KEY",
                value="new-value",
                target="build",
                actor_id="user-1",
            )
        )


def test_quota_is_checked_before_any_key_work() -> None:
    class Full(FakeConnection):
        async def fetchval(self, sql, *args):
            assert "count(*)" in sql
            return 2

    service = _service(max_secrets=2)
    with pytest.raises(SecretQuotaExceeded):
        asyncio.run(
            service.create_secret(
                Full(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="THIRD_KEY",
                value="value-long-enough",
                actor_id="user-1",
            )
        )


@pytest.mark.parametrize("key", ["", "   ", "HAS=EQ", "HAS SPACE", "HAS\x00NUL"])
def test_invalid_keys_are_rejected(key: str) -> None:
    with pytest.raises(InvalidSecret):
        asyncio.run(
            _service().create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key=key,
                value="value-long-enough",
                actor_id="user-1",
            )
        )


@pytest.mark.parametrize("environments", [[], [""], ["  "], "production"])
def test_invalid_environments_are_rejected(environments) -> None:
    with pytest.raises(InvalidSecret):
        asyncio.run(
            _service().create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="API_KEY",
                value="value-long-enough",
                environments=environments,
                actor_id="user-1",
            )
        )


def test_invalid_target_is_rejected() -> None:
    with pytest.raises(InvalidSecret):
        asyncio.run(
            _service().create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="API_KEY",
                value="value-long-enough",
                target="everything",
                actor_id="user-1",
            )
        )


def test_invalid_actor_type_is_rejected() -> None:
    with pytest.raises(InvalidSecret):
        asyncio.run(
            _service().create_secret(
                ForbiddenConnection(),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                key="API_KEY",
                value="value-long-enough",
                actor_id="user-1",
                actor_type="robot",
            )
        )


def _encrypted_row(
    master: StaticMasterKey,
    *,
    key: str = "API_KEY",
    value: str = "super-secret-value",
    target: str = "runtime",
    environments: tuple[str, ...] = ("production",),
) -> dict:
    dek = os.urandom(32)
    wrapped = master.wrap_dek(dek, aad=dek_aad(ORG))
    ciphertext, _ = encrypt(value.encode(), dek, aad_for(ORG, PROJECT, key))
    return {
        "id": uuid.uuid4(),
        "key": key,
        "ciphertext": ciphertext,
        "data_key": wrapped,
        "target": target,
        "environments": list(environments),
    }


def test_resolution_returns_values_and_writes_one_audit_row_per_secret() -> None:
    master = StaticMasterKey(os.urandom(32))
    audit = InMemoryAuditSink()
    service = _service(master=master, audit=audit)
    conn = FakeConnection([_encrypted_row(master)])

    result = asyncio.run(
        service.resolve_for_runtime(
            conn,  # type: ignore[arg-type]
            org_id=ORG,
            project_id=PROJECT,
            environment="production",
            deployment_id=uuid.uuid4(),
        )
    )

    assert result == {"API_KEY": "super-secret-value"}
    assert audit.actions() == ["secret.read_value"]
    assert "super-secret-value" not in str(audit.rows[0].metadata)
    # last_used_at is touched before the audit row is written
    assert any("last_used_at" in sql for sql, _args in conn.calls)
    query_args = conn.calls[0][1]
    assert query_args[3] == ["runtime", "both"]


def test_killing_the_audit_write_fails_the_resolution() -> None:
    """AC-10: a value is never returned if its audit row cannot be written."""
    master = StaticMasterKey(os.urandom(32))
    service = _service(master=master, audit=InMemoryAuditSink(fail=True))
    conn = FakeConnection([_encrypted_row(master)])

    with pytest.raises(AuditWriteFailed):
        asyncio.run(
            service.resolve_for_runtime(
                conn,  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                environment="production",
            )
        )


def test_no_rows_means_no_audit_rows() -> None:
    audit = InMemoryAuditSink()
    service = _service(audit=audit)
    result = asyncio.run(
        service.resolve_for_build(
            FakeConnection(),  # type: ignore[arg-type]
            org_id=ORG,
            project_id=PROJECT,
            environment="preview",
        )
    )
    assert result == {}
    assert audit.rows == []


def test_master_key_unavailable_fails_resolution_closed() -> None:
    """AC-11: no value is returned when the key provider is down."""
    row = _encrypted_row(StaticMasterKey(os.urandom(32)))
    service = _service(master=UnavailableMasterKey("KMS down"), audit=InMemoryAuditSink())

    with pytest.raises(MasterKeyUnavailable):
        asyncio.run(
            service.resolve_for_runtime(
                FakeConnection([row]),  # type: ignore[arg-type]
                org_id=ORG,
                project_id=PROJECT,
                environment="production",
            )
        )


def test_build_resolution_asks_only_for_build_visible_targets() -> None:
    master = StaticMasterKey(os.urandom(32))
    service = _service(master=master)
    conn = FakeConnection([])

    asyncio.run(
        service.resolve_for_build(
            conn,  # type: ignore[arg-type]
            org_id=ORG,
            project_id=PROJECT,
            environment="production",
        )
    )

    assert conn.calls[0][1][3] == ["build", "both"]
