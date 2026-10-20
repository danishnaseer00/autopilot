"""Error taxonomy for SPEC-18.

Every message here is safe to log: none of these exceptions may carry a secret
value or a fragment of one (SPEC-18 §8). Key *names* are allowed; values are not.
"""

from __future__ import annotations


class SecretError(Exception):
    """Base class for every secrets failure."""


class InvalidSecret(SecretError, ValueError):
    """A key, target, or environment set is not valid input."""


class SecretNotFound(SecretError):
    """No row matches the request (or the row is outside the caller's tenant)."""


class SecretAlreadyExists(SecretError):
    """The project already has a secret with this key (UNIQUE(project_id, key))."""


class SecretQuotaExceeded(SecretError):
    """S4: too many secrets for one project - rejected before any decrypt storm."""


class ConfirmationRequired(SecretError):
    """A build-visible or non-production secret needs an explicit confirmation.

    SPEC-18 R-5 (build visibility) and §8 (non-production environments).
    """


class SecretDecryptDenied(SecretError):
    """S2: AES-GCM authentication failed. Never return the value.

    This means tampering, a row copied between rows, or a key-rotation bug; the
    caller must alert and write ``secret.decrypt_denied`` (SPEC-18 §5/§6).
    """


class MasterKeyUnavailable(SecretError):
    """S1: the master key provider (KMS or mounted file) is unavailable.

    Fail closed: a build that needs secrets fails instead of running without
    them (SPEC-18 §6, AC-11).
    """


class AuditWriteFailed(SecretError):
    """The audit row could not be written, so the value is not returned (AC-10)."""


class RedactionUnavailable(SecretError):
    """S3: a build has secrets but no redaction values; refuse to ship its logs.

    SPEC-18 §6: "Redaction list empty for a build that has secrets - treat as a
    bug: log and fail the build rather than shipping unredacted logs."
    """


__all__ = [
    "AuditWriteFailed",
    "ConfirmationRequired",
    "InvalidSecret",
    "MasterKeyUnavailable",
    "RedactionUnavailable",
    "SecretAlreadyExists",
    "SecretDecryptDenied",
    "SecretError",
    "SecretNotFound",
    "SecretQuotaExceeded",
]
