"""AES-256-GCM envelope primitives (SPEC-18 §3.1).

The ciphertext layout is ``nonce || ciphertext+tag``; the raw nonce is returned
alongside for callers that want to store it separately. The GCM tag is appended
to ``ct`` by the library, so ``decrypt`` can take the single blob back.

The AAD is the row binding (R-1): ``f"{org_id}:{project_id}:{key}"``. Copying one
row's ciphertext into another row therefore fails authentication, rather than
decrypting to garbage under the wrong key name.
"""

from __future__ import annotations

import os
import uuid
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from runtime.secrets.errors import InvalidSecret, SecretDecryptDenied

NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16
KEY_BYTES: Final = 32


def aad_for(org_id: str | uuid.UUID, project_id: str | uuid.UUID, key: str) -> str:
    """The per-row AAD (R-1). Canonical UUID spelling, so callers agree."""
    return f"{uuid.UUID(str(org_id))}:{uuid.UUID(str(project_id))}:{key}"


def dek_aad(org_id: str | uuid.UUID) -> bytes:
    """AAD for wrapping the per-org DEK under the master key."""
    return f"{uuid.UUID(str(org_id))}:dek".encode("utf-8")


def encrypt(plaintext: bytes, dek: bytes, aad: str) -> tuple[bytes, bytes]:
    """Encrypt ``plaintext`` under a 256-bit DEK.

    Returns ``(nonce + ct, nonce)`` exactly as SPEC-18 §3.1's signature.
    """
    _require_key(dek)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return nonce + ciphertext, nonce


def decrypt(blob: bytes, dek: bytes, aad: str) -> bytes:
    """Decrypt ``nonce || ciphertext+tag``; any authentication failure is denied.

    The exception never contains the plaintext or a hint about it (S2, §8).
    """
    _require_key(dek)
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise SecretDecryptDenied("secret ciphertext is truncated or malformed")
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(dek).decrypt(nonce, ciphertext, aad.encode("utf-8"))
    except InvalidTag:
        # Tampering, row swap, wrong key, or wrong AAD. Never echo the input.
        raise SecretDecryptDenied("secret authentication failed") from None


def _require_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise InvalidSecret("encryption keys must be 256 bits (32 bytes)")


__all__ = [
    "KEY_BYTES",
    "NONCE_BYTES",
    "TAG_BYTES",
    "aad_for",
    "decrypt",
    "dek_aad",
    "encrypt",
]
