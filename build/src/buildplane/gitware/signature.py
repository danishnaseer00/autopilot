"""Webhook signatures and secret derivation (SPEC-08 R-3..R-6).

The signature is checked against the exact bytes GitHub signed. The approved
header is `X-Hub-Signature-256`; the legacy SHA-1 header is not accepted.

R-5 asks for `HMAC(master, provider_repo_id)` so the receiver can choose a key
without a database lookup that reveals whether a repository exists. This module
implements that derivation and uses `hmac.compare_digest` for every comparison.
"""

from __future__ import annotations

import hashlib
import hmac

from buildplane.gitware.validation import (
    validate_master_secret,
    validate_provider_repo_id,
)

SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="
_UNRESOLVED_REPO_KEY = b"unresolved"


def _derive(master: bytes, key: bytes) -> bytes:
    # The key is ASCII because a provider repo ID is an integer and the
    # unresolved sentinel is fixed. No tenant-controlled text enters the HMAC
    # key material.
    return hmac.new(master, key, hashlib.sha256).digest()


def derive_webhook_secret(master: bytes, provider_repo_id: int) -> bytes:
    """Derive the per-repository webhook secret (R-5).

    The spec's formula has no project component. Connecting the same provider
    repository to two projects would therefore share a secret; that uniqueness
    constraint is logged as a proposal in DECISIONS.md.
    """

    secret = validate_master_secret(master)
    repo_id = validate_provider_repo_id(provider_repo_id)
    return _derive(secret, str(repo_id).encode("ascii"))


def derive_unresolved_secret(master: bytes) -> bytes:
    """A dummy key used when the body does not identify a repository.

    Verification still performs the same HMAC operation on this path; the
    result is discarded unless the signature happens to match, which cannot
    happen for a real repository ID.
    """

    return _derive(validate_master_secret(master), _UNRESOLVED_REPO_KEY)


def sign_payload(raw: bytes, secret: bytes) -> str:
    """Return the header value GitHub would send for `raw`."""

    return SIGNATURE_PREFIX + hmac.new(secret, raw, hashlib.sha256).hexdigest()


def verify_signature(raw: bytes, header: str | None, secret: bytes) -> bool:
    """Verify `X-Hub-Signature-256` against the raw request bytes.

    `hmac.compare_digest` is used for the final comparison (R-4). A missing or
    non-SHA-256 header is a rejection, never a bypass.
    """

    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = sign_payload(raw, secret)
    return hmac.compare_digest(expected, header)
