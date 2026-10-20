"""SPEC-18 secrets service: envelope storage, the two resolution functions,
rotation, deletion, and audit.

The service takes an ``asyncpg`` connection owned by the caller. That is
deliberate: the caller opens the tenant transaction (``set_config('app.org_id',
..., true)``), so RLS context and the audit-before-return ordering are visible in
one place. The service never commits; a failure rolls the whole transaction back.

Two functions must never be confused (SPEC-18 §3.2):

* :meth:`SecretService.resolve_for_build` - only secrets opted into build
  visibility, and **structurally** empty for a fork-triggered build (R-3).
* :meth:`SecretService.resolve_for_runtime` - all secrets whose environments
  include the target environment (R-6 keeps production out of previews).

Neither consults a cache (R-4): every call unwraps the DEK, decrypts, and writes
audit rows before returning values.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence

import asyncpg

from runtime.secrets.audit import AuditRecord, AuditSink
from runtime.secrets.crypto import aad_for, decrypt, dek_aad, encrypt
from runtime.secrets.errors import (
    AuditWriteFailed,
    ConfirmationRequired,
    InvalidSecret,
    SecretAlreadyExists,
    SecretDecryptDenied,
    SecretNotFound,
    SecretQuotaExceeded,
)
from runtime.secrets.masterkey import MasterKeyProvider

Target = Literal["build", "runtime", "both"]
_BUILD_TARGETS = ("build", "both")
_BUILD_VISIBLE_TARGETS = frozenset(_BUILD_TARGETS)
_VALID_TARGETS = frozenset({"build", "runtime", "both"})
_MAX_KEY_LENGTH = 253  # DNS-label-ish ceiling; far above a real env var name
_ACTOR_TYPES = frozenset({"user", "agent", "system"})


@dataclass(frozen=True)
class SecretMetadata:
    """Everything the console may see. Never includes ciphertext or a value."""

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    key: str
    target: str
    environments: tuple[str, ...]
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RotationResult:
    version: int
    live_deployments: int
    stale_previews: int


class SecretService:
    """Secret storage and resolution for one process.

    ``master_key`` is a provider (KMS in production, mounted file in the FYP
    fallback). ``audit`` writes to the separate audit database. Both are
    required: there is no "run without audit" mode (AC-10).
    """

    def __init__(
        self,
        *,
        master_key: MasterKeyProvider,
        audit: AuditSink,
        max_secrets_per_project: int = 50,
    ) -> None:
        if max_secrets_per_project < 0:
            raise InvalidSecret("max_secrets_per_project must be non-negative")
        self._master = master_key
        self._audit = audit
        self._max_secrets = max_secrets_per_project

    # ------------------------------------------------------------------ CRUD

    async def create_secret(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        key: str,
        value: str,
        target: str = "runtime",
        environments: Sequence[str] = ("production",),
        confirm_build_visibility: bool = False,
        confirm_non_production_environments: bool = False,
        actor_id: str,
        actor_type: str = "user",
    ) -> SecretMetadata:
        """Create a secret. ``target='build'`` needs the explicit warning flag."""
        org, project = _ids(org_id, project_id)
        key = _validate_key(key)
        target = _validate_target(target)
        envs = _normalise_environments(environments)
        _require_confirmations(
            target,
            envs,
            confirm_build_visibility=confirm_build_visibility,
            confirm_non_production_environments=confirm_non_production_environments,
        )
        _validate_actor(actor_type)

        # Serialise creates per project so two concurrent requests cannot both
        # pass the quota check and store one more secret than the plan allows.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"secrets:{project}",
        )
        if self._max_secrets > 0:
            count = await conn.fetchval(
                "SELECT count(*) FROM env_vars "
                "WHERE org_id = $1 AND project_id = $2",
                org,
                project,
            )
            if count >= self._max_secrets:
                raise SecretQuotaExceeded(
                    f"project already has {count} secrets (limit {self._max_secrets})"
                )

        dek, wrapped_dek = await self._ensure_org_dek(conn, org)
        secret_id = uuid.uuid4()
        ciphertext, _nonce = encrypt(
            value.encode("utf-8"), dek, aad_for(org, project, key)
        )

        # R-5/§5: the audit row exists before the write it describes.
        await self._audit_or_fail(
            AuditRecord(
                action="secret.write",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="env_var",
                target_id=str(secret_id),
                metadata=_write_metadata(key, target, envs, created=True),
            )
        )

        try:
            row = await conn.fetchrow(
                "INSERT INTO env_vars "
                "  (id, org_id, project_id, key, ciphertext, data_key, "
                "   target, environments) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "RETURNING id, org_id, project_id, key, target, environments, "
                "          last_used_at, created_at, updated_at",
                secret_id,
                org,
                project,
                key,
                ciphertext,
                wrapped_dek,
                target,
                list(envs),
            )
        except asyncpg.UniqueViolationError:
            raise SecretAlreadyExists(f"secret {key!r} already exists") from None
        return _metadata(row)

    async def update_secret(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        key: str,
        value: str,
        target: str | None = None,
        environments: Sequence[str] | None = None,
        confirm_build_visibility: bool = False,
        confirm_non_production_environments: bool = False,
        actor_id: str,
        actor_type: str = "user",
    ) -> SecretMetadata:
        """Replace a secret's value and/or its visibility.

        Confirmations are required for the *final* state, not only for changes:
        every write to a build-visible or non-production secret is an explicit
        acknowledgement (R-5, §8).
        """
        org, project = _ids(org_id, project_id)
        key = _validate_key(key)
        _validate_actor(actor_type)

        existing = await conn.fetchrow(
            "SELECT id, target, environments FROM env_vars "
            "WHERE org_id = $1 AND project_id = $2 AND key = $3 "
            "FOR UPDATE",
            org,
            project,
            key,
        )
        if existing is None:
            raise SecretNotFound(f"secret {key!r} not found")

        final_target = (
            _validate_target(target) if target is not None else str(existing["target"])
        )
        final_envs = (
            _normalise_environments(environments)
            if environments is not None
            else tuple(existing["environments"])
        )
        _require_confirmations(
            final_target,
            final_envs,
            confirm_build_visibility=confirm_build_visibility,
            confirm_non_production_environments=confirm_non_production_environments,
        )

        dek, wrapped_dek = await self._ensure_org_dek(conn, org)
        ciphertext, _nonce = encrypt(
            value.encode("utf-8"), dek, aad_for(org, project, key)
        )

        await self._audit_or_fail(
            AuditRecord(
                action="secret.write",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="env_var",
                target_id=str(existing["id"]),
                metadata=_write_metadata(
                    key, final_target, final_envs, created=False
                ),
            )
        )

        row = await conn.fetchrow(
            "UPDATE env_vars "
            "   SET ciphertext = $4, data_key = $5, target = $6, "
            "       environments = $7, updated_at = now() "
            " WHERE org_id = $1 AND project_id = $2 AND key = $3 "
            "RETURNING id, org_id, project_id, key, target, environments, "
            "          last_used_at, created_at, updated_at",
            org,
            project,
            key,
            ciphertext,
            wrapped_dek,
            final_target,
            list(final_envs),
        )
        return _metadata(row)

    async def list_secrets(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
    ) -> list[SecretMetadata]:
        """Metadata only - ciphertext and plaintext never leave this call."""
        org, project = _ids(org_id, project_id)
        rows = await conn.fetch(
            "SELECT id, org_id, project_id, key, target, environments, "
            "       last_used_at, created_at, updated_at "
            "  FROM env_vars "
            " WHERE org_id = $1 AND project_id = $2 "
            " ORDER BY key",
            org,
            project,
        )
        return [_metadata(row) for row in rows]

    async def get_secret(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        key: str,
    ) -> SecretMetadata:
        org, project = _ids(org_id, project_id)
        key = _validate_key(key)
        row = await conn.fetchrow(
            "SELECT id, org_id, project_id, key, target, environments, "
            "       last_used_at, created_at, updated_at "
            "  FROM env_vars "
            " WHERE org_id = $1 AND project_id = $2 AND key = $3",
            org,
            project,
            key,
        )
        if row is None:
            raise SecretNotFound(f"secret {key!r} not found")
        return _metadata(row)

    async def delete_secret(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        key: str,
        actor_id: str,
        actor_type: str = "user",
    ) -> int:
        """Real deletion (R-12), then mark referencing deployments stale.

        Returns the number of deployments marked. There is no soft-delete copy:
        the ciphertext row is removed, and no plaintext cache exists to purge.
        """
        org, project = _ids(org_id, project_id)
        key = _validate_key(key)
        _validate_actor(actor_type)

        row = await conn.fetchrow(
            "SELECT id, environments FROM env_vars "
            "WHERE org_id = $1 AND project_id = $2 AND key = $3 "
            "FOR UPDATE",
            org,
            project,
            key,
        )
        if row is None:
            raise SecretNotFound(f"secret {key!r} not found")

        await self._audit_or_fail(
            AuditRecord(
                action="secret.delete",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="env_var",
                target_id=str(row["id"]),
                metadata={
                    "key": key,
                    "environments": _jsonable_envs(row["environments"]),
                },
            )
        )

        await conn.execute(
            "DELETE FROM env_vars WHERE org_id = $1 AND project_id = $2 AND key = $3",
            org,
            project,
            key,
        )

        marked = await conn.fetchval(
            "WITH marked AS ("
            "  UPDATE deployments SET stale_secrets = true "
            "   WHERE org_id = $1 AND project_id = $2 AND stale_secrets = false "
            "     AND ( $3 = ANY(build_env_keys) "
            "           OR (environment = ANY($4::text[]) "
            "               AND status IN ('ready','live')) ) "
            "  RETURNING 1"
            ") SELECT count(*) FROM marked",
            org,
            project,
            key,
            list(row["environments"]),
        )
        return int(marked)

    async def rotate_secret(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        key: str,
        value: str,
        actor_id: str,
        actor_type: str = "user",
    ) -> RotationResult:
        """Rotate in one transaction (R-11).

        * the ciphertext is replaced atomically;
        * ``projects.secrets_version`` is bumped - the deployment controller
          compares it with ``deployments.secrets_version`` and rolls the pods;
        * every ``live`` deployment for the project gets the new version in the
          same transaction;
        * preview deployments that are built/in-flight are marked ``stale``, so
          the console can name them for a rebuild.
        """
        org, project = _ids(org_id, project_id)
        key = _validate_key(key)
        _validate_actor(actor_type)

        row = await conn.fetchrow(
            "SELECT id FROM env_vars "
            "WHERE org_id = $1 AND project_id = $2 AND key = $3 "
            "FOR UPDATE",
            org,
            project,
            key,
        )
        if row is None:
            raise SecretNotFound(f"secret {key!r} not found")

        dek, wrapped_dek = await self._ensure_org_dek(conn, org)
        ciphertext, _nonce = encrypt(
            value.encode("utf-8"), dek, aad_for(org, project, key)
        )

        await self._audit_or_fail(
            AuditRecord(
                action="secret.rotate",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="env_var",
                target_id=str(row["id"]),
                metadata={"key": key},
            )
        )

        await conn.execute(
            "UPDATE env_vars "
            "   SET ciphertext = $4, data_key = $5, updated_at = now() "
            " WHERE org_id = $1 AND project_id = $2 AND key = $3",
            org,
            project,
            key,
            ciphertext,
            wrapped_dek,
        )

        version = await conn.fetchval(
            "UPDATE projects "
            "   SET secrets_version = secrets_version + 1, updated_at = now() "
            " WHERE org_id = $1 AND id = $2 "
            "RETURNING secrets_version",
            org,
            project,
        )
        if version is None:
            # The secret existed but its project vanished between statements:
            # impossible under the FK, but fail loudly rather than half-rotate.
            raise SecretNotFound("project not found during rotation")

        live = await conn.fetchval(
            "WITH updated AS ("
            "  UPDATE deployments SET secrets_version = $3 "
            "   WHERE org_id = $1 AND project_id = $2 AND status = 'live' "
            "  RETURNING 1"
            ") SELECT count(*) FROM updated",
            org,
            project,
            version,
        )
        stale = await conn.fetchval(
            "WITH marked AS ("
            "  UPDATE deployments SET stale_secrets = true "
            "   WHERE org_id = $1 AND project_id = $2 "
            "     AND environment = 'preview' "
            "     AND status IN ('queued','building','ready') "
            "     AND stale_secrets = false "
            "  RETURNING 1"
            ") SELECT count(*) FROM marked",
            org,
            project,
        )
        return RotationResult(
            version=int(version),
            live_deployments=int(live),
            stale_previews=int(stale),
        )

    # ------------------------------------------------------------- resolution

    async def resolve_for_build(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        environment: str,
        fork: bool = False,
        build_id: str | uuid.UUID | None = None,
        actor_id: str = "build-worker",
        actor_type: str = "system",
    ) -> dict[str, str]:
        """Resolve only build-visible secrets for a build environment.

        R-3 is structural: the fork check is the first statement, before any
        lookup, decrypt, or audit call. A future condition cannot accidentally
        put it after a query. Forks always receive ``{}``.
        """
        if fork:
            return {}
        return await self._resolve(
            conn,
            org_id=org_id,
            project_id=project_id,
            environment=environment,
            targets=_BUILD_TARGETS,
            audit_extra={"build_id": str(build_id) if build_id else None},
            actor_id=actor_id,
            actor_type=actor_type,
        )

    async def resolve_for_runtime(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        environment: str,
        deployment_id: str | uuid.UUID | None = None,
        actor_id: str = "deployment-controller",
        actor_type: str = "system",
    ) -> dict[str, str]:
        """Resolve every secret available in ``environment`` (R-6 in the query).

        A production-only row does not satisfy ``environment = ANY(environments)``
        for a preview, so production values never reach previews regardless of
        what the caller does.
        """
        return await self._resolve(
            conn,
            org_id=org_id,
            project_id=project_id,
            environment=environment,
            targets=("runtime", "both"),
            audit_extra={
                "deployment_id": str(deployment_id) if deployment_id else None
            },
            actor_id=actor_id,
            actor_type=actor_type,
        )

    async def _resolve(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        environment: str,
        targets: tuple[str, ...],
        audit_extra: dict[str, Any],
        actor_id: str,
        actor_type: str,
    ) -> dict[str, str]:
        org, project = _ids(org_id, project_id)
        environment = _validate_environment(environment)
        _validate_actor(actor_type)

        rows = await conn.fetch(
            "SELECT id, key, ciphertext, data_key, target, environments "
            "  FROM env_vars "
            " WHERE org_id = $1 AND project_id = $2 "
            "   AND $3 = ANY(environments) "
            "   AND target::text = ANY($4::text[]) "
            " ORDER BY key",
            org,
            project,
            environment,
            list(targets),
        )
        if not rows:
            return {}

        decrypted: list[tuple[asyncpg.Record, str]] = []
        for row in rows:
            try:
                dek = self._master.unwrap_dek(
                    bytes(row["data_key"]), aad=dek_aad(org)
                )
                plaintext = decrypt(
                    bytes(row["ciphertext"]),
                    dek,
                    aad_for(org, project, str(row["key"])),
                )
                value = plaintext.decode("utf-8")
            except SecretDecryptDenied:
                # S2: never return the value. Record the denial if audit is up;
                # the decryption failure is the primary signal either way.
                try:
                    await self._audit_or_fail(
                        AuditRecord(
                            action="secret.decrypt_denied",
                            org_id=org,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            target_type="env_var",
                            target_id=str(row["id"]),
                            metadata={
                                "key": str(row["key"]),
                                "environment": environment,
                            },
                        )
                    )
                except AuditWriteFailed:
                    pass
                raise
            decrypted.append((row, value))

        # Metadata touch, then audit, then return: audit must exist before the
        # caller can observe a value (AC-10). If audit fails, the exception
        # propagates and the caller receives nothing.
        await conn.execute(
            "UPDATE env_vars SET last_used_at = now() "
            " WHERE org_id = $1 AND id = ANY($2::uuid[])",
            org,
            [row["id"] for row, _value in decrypted],
        )
        for row, _value in decrypted:
            await self._audit_or_fail(
                AuditRecord(
                    action="secret.read_value",
                    org_id=org,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    target_type="env_var",
                    target_id=str(row["id"]),
                    metadata={
                        "key": str(row["key"]),
                        "environment": environment,
                        **{
                            key: value
                            for key, value in audit_extra.items()
                            if value is not None
                        },
                    },
                )
            )
        return {str(row["key"]): value for row, value in decrypted}

    # --------------------------------------------------------------- internals

    async def _ensure_org_dek(
        self, conn: asyncpg.Connection, org_id: uuid.UUID
    ) -> tuple[bytes, bytes]:
        """Return ``(plaintext_dek, wrapped_dek)`` for the org.

        First use creates and stores the DEK under the master key (SPEC-18 §3.1).
        ``FOR UPDATE`` serialises concurrent first writes, so two requests cannot
        each generate a DEK and have one silently invalidate the other's rows.
        """
        row = await conn.fetchrow(
            "SELECT dek_wrapped FROM orgs WHERE id = $1 FOR UPDATE", org_id
        )
        if row is None:
            raise SecretNotFound("organisation not found in tenant context")
        wrapped = row["dek_wrapped"]
        if wrapped is not None:
            wrapped_bytes = bytes(wrapped)
            return self._master.unwrap_dek(wrapped_bytes, aad=dek_aad(org_id)), wrapped_bytes

        dek = os.urandom(32)
        wrapped_bytes = self._master.wrap_dek(dek, aad=dek_aad(org_id))
        await conn.execute(
            "UPDATE orgs SET dek_wrapped = $2 WHERE id = $1", org_id, wrapped_bytes
        )
        return dek, wrapped_bytes

    async def _audit_or_fail(self, record: AuditRecord) -> None:
        """Any sink failure becomes ``AuditWriteFailed``; no value escapes."""
        try:
            await self._audit.write(record)
        except AuditWriteFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary: audit failure is fatal
            raise AuditWriteFailed(
                f"audit write failed for {record.action}; operation refused"
            ) from exc


# ---------------------------------------------------------------- validation


def _ids(*values: str | uuid.UUID) -> tuple[uuid.UUID, ...]:
    try:
        return tuple(uuid.UUID(str(value)) for value in values)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidSecret("org_id/project_id must be UUIDs") from exc


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise InvalidSecret("secret key must be a non-empty string")
    if len(key) > _MAX_KEY_LENGTH:
        raise InvalidSecret("secret key is too long")
    if "=" in key or "\x00" in key or any(character.isspace() for character in key):
        raise InvalidSecret("secret key must not contain '=', whitespace, or NUL")
    return key


def _validate_target(target: str) -> str:
    if target not in _VALID_TARGETS:
        raise InvalidSecret(f"target must be one of {sorted(_VALID_TARGETS)}")
    return target


def _validate_environment(environment: str) -> str:
    if not isinstance(environment, str) or not environment.strip():
        raise InvalidSecret("environment must be a non-empty string")
    return environment


def _validate_actor(actor_type: str) -> None:
    if actor_type not in _ACTOR_TYPES:
        raise InvalidSecret(f"actor_type must be one of {sorted(_ACTOR_TYPES)}")


def _normalise_environments(environments: Iterable[str]) -> tuple[str, ...]:
    if isinstance(environments, (str, bytes)):
        raise InvalidSecret("environments must be a sequence of environment names")
    cleaned: list[str] = []
    for environment in environments:
        if not isinstance(environment, str) or not environment.strip():
            raise InvalidSecret("environment names must be non-empty strings")
        name = environment.strip()
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise InvalidSecret("a secret must be available in at least one environment")
    return tuple(cleaned)


def _require_confirmations(
    target: str,
    environments: Sequence[str],
    *,
    confirm_build_visibility: bool,
    confirm_non_production_environments: bool,
) -> None:
    if target in _BUILD_VISIBLE_TARGETS and not confirm_build_visibility:
        # R-5: build visibility is opt-in and warned, never a plain checkbox.
        raise ConfirmationRequired(
            "target='build' requires confirm_build_visibility=True (R-5)"
        )
    if any(environment != "production" for environment in environments):
        if not confirm_non_production_environments:
            # §8: preview URLs are reachable by anyone who has the URL.
            raise ConfirmationRequired(
                "non-production environments require "
                "confirm_non_production_environments=True"
            )


def _write_metadata(
    key: str, target: str, environments: Sequence[str], *, created: bool
) -> dict[str, Any]:
    return {
        "key": key,
        "target": target,
        "environments": [str(environment) for environment in environments],
        "build_visible": target in _BUILD_VISIBLE_TARGETS,
        "non_production_environments": [
            str(environment) for environment in environments if environment != "production"
        ],
        "created": created,
    }


def _jsonable_envs(environments: Iterable[str]) -> list[str]:
    return [str(environment) for environment in environments]


def _metadata(row: asyncpg.Record) -> SecretMetadata:
    return SecretMetadata(
        id=row["id"],
        org_id=row["org_id"],
        project_id=row["project_id"],
        key=str(row["key"]),
        target=str(row["target"]),
        environments=tuple(str(environment) for environment in row["environments"]),
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = ["RotationResult", "SecretMetadata", "SecretService"]
