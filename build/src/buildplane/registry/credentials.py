"""Short-lived, namespace-scoped registry credentials (SPEC-12 R-4/R-5).

The token is an HMAC-signed capability, not a registry password. It is minted
by the control plane and handed to one build or one runtime workload for a
short window. A pull token covers exactly one org namespace; a push token
covers exactly one repository. Neither carries any package or source secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from buildplane.registry.errors import PullAccessDenied, PushAccessDenied
from buildplane.registry.models import (
    REGISTRY_NAMESPACE,
    validate_repository,
    validate_slug,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True, slots=True)
class PullCredential:
    token: str = field(repr=False)
    org_id: str
    scope: str
    expires_at: datetime

    @property
    def authorization(self) -> str:
        return f"Bearer {self.token}"


@dataclass(frozen=True, slots=True)
class CredentialClaims:
    org_id: str
    scope: str
    actions: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    credential_id: str


class RegistryCredentialIssuer:
    """Mint and verify the two credential scopes the registry accepts."""

    def __init__(
        self,
        secret: bytes,
        *,
        clock=_utcnow,
        default_ttl_seconds: int = 900,
        max_ttl_seconds: int = 3600,
    ) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise ValueError("registry credential secret must be at least 16 bytes")
        if default_ttl_seconds <= 0 or max_ttl_seconds <= 0:
            raise ValueError("credential lifetimes must be positive")
        if default_ttl_seconds > max_ttl_seconds:
            raise ValueError("default_ttl_seconds cannot exceed max_ttl_seconds")
        self._secret = bytes(secret)
        self._clock = clock
        self.default_ttl_seconds = default_ttl_seconds
        self.max_ttl_seconds = max_ttl_seconds

    def issue_pull(
        self,
        *,
        org_id: str,
        org_slug: str,
        ttl_seconds: int | None = None,
    ) -> PullCredential:
        scope = f"{REGISTRY_NAMESPACE}/{validate_slug(org_slug)}"
        claims = self._issue(
            org_id=org_id,
            scope=scope,
            action="pull",
            ttl_seconds=ttl_seconds,
        )
        return PullCredential(
            token=claims.token,
            org_id=org_id,
            scope=scope,
            expires_at=claims.expires_at,
        )

    def issue_push(
        self,
        *,
        org_id: str,
        repository: str,
        ttl_seconds: int | None = None,
    ) -> PullCredential:
        validate_repository(repository)
        claims = self._issue(
            org_id=org_id,
            scope=repository,
            action="push",
            ttl_seconds=ttl_seconds,
        )
        return PullCredential(
            token=claims.token,
            org_id=org_id,
            scope=repository,
            expires_at=claims.expires_at,
        )

    def verify(
        self,
        token: str,
        *,
        repository: str,
        action: str,
    ) -> CredentialClaims:
        if action not in {"pull", "push"}:
            raise ValueError("action must be 'pull' or 'push'")
        try:
            claims = self._decode(token)
        except PullAccessDenied as exc:
            if action == "push":
                raise PushAccessDenied(str(exc)) from exc
            raise
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if claims.expires_at <= now:
            raise _denied(action, "registry credential has expired")
        if action not in claims.actions:
            raise _denied(action, "registry credential does not grant this action")
        if action == "pull":
            if not _repository_in_scope(repository, claims.scope):
                raise PullAccessDenied(
                    "registry credential is not scoped to this repository"
                )
        elif claims.scope != repository:
            raise PushAccessDenied("registry credential is not scoped to this repository")
        return claims

    def verify_pull(self, token: str, *, repository: str) -> CredentialClaims:
        return self.verify(token, repository=repository, action="pull")

    def verify_push(self, token: str, *, repository: str) -> CredentialClaims:
        return self.verify(token, repository=repository, action="push")

    @dataclass(frozen=True, slots=True)
    class _Minted:
        token: str
        expires_at: datetime

    def _issue(
        self,
        *,
        org_id: str,
        scope: str,
        action: str,
        ttl_seconds: int | None,
    ) -> "RegistryCredentialIssuer._Minted":
        if not isinstance(org_id, str) or not org_id.strip():
            raise ValueError("org_id is required")
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0 or ttl > self.max_ttl_seconds:
            raise ValueError(
                f"ttl_seconds must be between 1 and {self.max_ttl_seconds}"
            )
        issued = self._clock()
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        expires = issued + timedelta(seconds=ttl)
        payload = {
            "v": 1,
            "jti": uuid.uuid4().hex,
            "org": org_id,
            "scope": scope,
            "actions": [action],
            "iat": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
        }
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return self._Minted(token=f"{encoded}.{signature}", expires_at=expires)

    def _decode(self, token: str) -> CredentialClaims:
        if not isinstance(token, str) or token.count(".") != 1:
            raise PullAccessDenied("registry credential is malformed")
        encoded, signature = token.split(".", 1)
        try:
            expected = _b64encode(
                hmac.new(
                    self._secret, encoded.encode("ascii"), hashlib.sha256
                ).digest()
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise PullAccessDenied("registry credential is malformed") from exc
        if not hmac.compare_digest(expected, signature):
            raise PullAccessDenied("registry credential signature is invalid")
        try:
            document = json.loads(_b64decode(encoded))
            actions = document["actions"]
            if document.get("v") != 1 or not isinstance(actions, list):
                raise ValueError("unsupported credential")
            if not all(isinstance(item, str) for item in actions):
                raise ValueError("invalid actions")
            return CredentialClaims(
                org_id=str(document["org"]),
                scope=str(document["scope"]),
                actions=frozenset(actions),
                issued_at=datetime.fromtimestamp(int(document["iat"]), timezone.utc),
                expires_at=datetime.fromtimestamp(int(document["exp"]), timezone.utc),
                credential_id=str(document["jti"]),
            )
        except (
            KeyError,
            OSError,
            OverflowError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise PullAccessDenied("registry credential is malformed") from exc


def _repository_in_scope(repository: str, scope: str) -> bool:
    if repository == scope:
        return True
    return repository.startswith(scope + "/")


def _denied(action: str, message: str) -> PushAccessDenied | PullAccessDenied:
    if action == "push":
        return PushAccessDenied(message)
    return PullAccessDenied(message)
