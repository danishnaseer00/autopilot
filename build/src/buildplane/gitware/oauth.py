"""Signed, single-use OAuth state (SPEC-08 §7).

The GitHub OAuth endpoints themselves belong to the control plane (SPEC-04),
but the security property of the `state` parameter is a SPEC-08 requirement:
signed, short-lived (10 minutes), single-use, and bound to the user and project
that started the flow. This module implements exactly that primitive so the
control router cannot accidentally ship an unsigned state.

The state carries user/project identifiers and a nonce; it never carries a
token. The HMAC key is injected and must be distinct from the webhook master
key (domain separation is applied to the signed bytes).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from buildplane.gitware.errors import OAuthStateError
from buildplane.gitware.validation import validate_master_secret

DEFAULT_STATE_TTL = timedelta(minutes=10)
MAX_STATE_LENGTH = 4096
_MAX_ID_LENGTH = 128


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True, slots=True)
class OAuthState:
    user_id: str
    project_id: str
    nonce: str
    expires_at: datetime


class NonceStore(Protocol):
    async def consume(self, nonce: str, *, expires_at: datetime) -> bool:
        """Return True if this nonce had not been seen before."""


class InMemoryNonceStore:
    """Process-local single-use set. Production uses Redis with a TTL."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    async def consume(self, nonce: str, *, expires_at: datetime) -> bool:
        if nonce in self._used:
            return False
        self._used.add(nonce)
        return True


class OAuthStateSigner:
    def __init__(
        self,
        secret: bytes,
        *,
        ttl: timedelta = DEFAULT_STATE_TTL,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if ttl <= timedelta(0) or ttl > timedelta(hours=1):
            raise ValueError("OAuth state TTL must be between 0 and 1 hour")
        self._secret = validate_master_secret(secret)
        self._ttl = ttl
        self._now = now

    def issue(self, *, user_id: str, project_id: str) -> str:
        _validate_id(user_id, "user_id")
        _validate_id(project_id, "project_id")
        now = _as_utc(self._now())
        payload = {
            "u": user_id,
            "p": project_id,
            "n": secrets.token_urlsafe(24),
            "exp": int((now + self._ttl).timestamp()),
        }
        body = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        signature = hmac.new(
            self._secret, b"oauth-state." + body, hashlib.sha256
        ).digest()
        return f"{_b64encode(body)}.{_b64encode(signature)}"

    def verify(
        self,
        state: str,
        *,
        expected_user_id: str | None = None,
        expected_project_id: str | None = None,
    ) -> OAuthState:
        if not isinstance(state, str) or not state or len(state) > MAX_STATE_LENGTH:
            raise OAuthStateError("invalid OAuth state length")
        try:
            encoded_body, encoded_signature = state.rsplit(".", 1)
            body = _b64decode(encoded_body)
            signature = _b64decode(encoded_signature)
        except (ValueError, TypeError) as exc:
            raise OAuthStateError("malformed OAuth state") from exc

        expected = hmac.new(
            self._secret, b"oauth-state." + body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise OAuthStateError("OAuth state signature mismatch")

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise OAuthStateError("malformed OAuth state payload") from exc
        if not isinstance(payload, dict):
            raise OAuthStateError("malformed OAuth state payload")

        user_id = payload.get("u")
        project_id = payload.get("p")
        nonce = payload.get("n")
        expires = payload.get("exp")
        if not all(isinstance(value, str) and value for value in (user_id, project_id, nonce)):
            raise OAuthStateError("OAuth state is missing bindings")
        if isinstance(expires, bool) or not isinstance(expires, int):
            raise OAuthStateError("OAuth state is missing an expiry")

        if expected_user_id is not None and not hmac.compare_digest(
            user_id.encode("utf-8"), expected_user_id.encode("utf-8")
        ):
            raise OAuthStateError("OAuth state belongs to another user")
        if expected_project_id is not None and not hmac.compare_digest(
            project_id.encode("utf-8"), expected_project_id.encode("utf-8")
        ):
            raise OAuthStateError("OAuth state belongs to another project")

        try:
            expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise OAuthStateError("OAuth state expiry is invalid") from exc
        if expires_at <= _as_utc(self._now()):
            raise OAuthStateError("OAuth state has expired")
        return OAuthState(
            user_id=user_id,
            project_id=project_id,
            nonce=nonce,
            expires_at=expires_at,
        )

    async def consume(
        self,
        state: str,
        store: NonceStore,
        *,
        expected_user_id: str | None = None,
        expected_project_id: str | None = None,
    ) -> OAuthState:
        parsed = self.verify(
            state,
            expected_user_id=expected_user_id,
            expected_project_id=expected_project_id,
        )
        if not await store.consume(parsed.nonce, expires_at=parsed.expires_at):
            raise OAuthStateError("OAuth state has already been used")
        return parsed


def _validate_id(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID_LENGTH:
        raise ValueError(f"{field} must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} contains a control character")
