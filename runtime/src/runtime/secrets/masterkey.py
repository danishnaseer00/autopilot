"""Master-key providers for wrapping per-org DEKs (SPEC-18 §3.1, Q-1).

The production design is cloud KMS: the master key never enters this process,
and KMS returns only the wrapped/unwrapped DEK. The FYP default (Q-1) is a
32-byte key from a mounted file; that is implemented here with the documented
risk that the key is resident in the process while it runs.

Failing to reach the provider raises :class:`MasterKeyUnavailable`, which callers
must let propagate: SPEC-18 §6 requires builds that need secrets to fail rather
than run without them (AC-11).
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Mapping, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from runtime.secrets.crypto import KEY_BYTES, NONCE_BYTES
from runtime.secrets.errors import InvalidSecret, MasterKeyUnavailable, SecretDecryptDenied


class MasterKeyProvider(Protocol):
    """Wraps and unwraps DEKs. An interface so KMS can replace the file later."""

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> bytes: ...

    def unwrap_dek(self, wrapped: bytes, *, aad: bytes) -> bytes: ...


class _AesGcmMasterKey:
    """A local AES-256-GCM master key (file fallback / tests).

    Wrapped DEK layout: ``nonce || ciphertext+tag``. AAD is ``b"<org>:dek"`` so a
    wrapped DEK cannot be moved between orgs even with a copy of the blob.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise InvalidSecret("master keys must be 256 bits (32 bytes)")
        self._key = key

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> bytes:
        if len(dek) != KEY_BYTES:
            raise InvalidSecret("DEKs must be 256 bits (32 bytes)")
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(self._key).encrypt(nonce, dek, aad)

    def unwrap_dek(self, wrapped: bytes, *, aad: bytes) -> bytes:
        if len(wrapped) < NONCE_BYTES + 16:
            raise SecretDecryptDenied("wrapped data key is malformed")
        nonce, ciphertext = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            dek = AESGCM(self._key).decrypt(nonce, ciphertext, aad)
        except InvalidTag:
            raise SecretDecryptDenied("data key authentication failed") from None
        if len(dek) != KEY_BYTES:
            raise SecretDecryptDenied("data key has the wrong length")
        return dek


class StaticMasterKey(_AesGcmMasterKey):
    """In-memory master key for tests and short-lived jobs. Not for production."""


class FileMasterKey(_AesGcmMasterKey):
    """Q-1 fallback: a master key from a mounted file.

    The file may contain 32 raw bytes, base64 (with or without a trailing
    newline), or 64 hex characters. A missing/unreadable file is fail-closed at
    startup: the process should not come up able to serve secrets.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise MasterKeyUnavailable(
                f"master key file is not readable: {self._path.name}"
            ) from exc
        super().__init__(_decode_key(raw, self._path.name))


class UnavailableMasterKey:
    """Test/degraded-mode double: every wrap or unwrap fails closed (AC-11)."""

    def __init__(self, reason: str = "master key provider unavailable") -> None:
        self._reason = reason

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> bytes:
        raise MasterKeyUnavailable(self._reason)

    def unwrap_dek(self, wrapped: bytes, *, aad: bytes) -> bytes:
        raise MasterKeyUnavailable(self._reason)


def _decode_key(raw: bytes, name: str) -> bytes:
    # Check the raw length *before* stripping: a valid 32-byte key may begin or
    # end with a whitespace byte, and stripping it would silently change the key.
    if len(raw) == KEY_BYTES:
        return raw
    raw = raw.strip()
    if len(raw) == KEY_BYTES:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if len(decoded) == KEY_BYTES:
        return decoded
    try:
        decoded = bytes.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        decoded = b""
    if len(decoded) == KEY_BYTES:
        return decoded
    raise MasterKeyUnavailable(
        f"master key file {name} does not contain a 32-byte key (raw/base64/hex)"
    )


def master_key_from_env(env: Mapping[str, str] | None = None) -> MasterKeyProvider:
    """Build the configured provider: ``MASTER_KEY_FILE`` -> mounted file.

    Deliberately no ``MASTER_KEY`` environment variable: a key in process config
    is the thing SPEC-18 §3.1 and §8 forbid. Tests may construct StaticMasterKey
    directly; deployment puts the file on a tmpfs/managed mount.
    """
    source = os.environ if env is None else env
    path = source.get("MASTER_KEY_FILE")
    if not path:
        raise MasterKeyUnavailable(
            "MASTER_KEY_FILE is not set; secrets are unavailable (fail closed)"
        )
    return FileMasterKey(path)


__all__ = [
    "FileMasterKey",
    "MasterKeyProvider",
    "StaticMasterKey",
    "UnavailableMasterKey",
    "master_key_from_env",
]
