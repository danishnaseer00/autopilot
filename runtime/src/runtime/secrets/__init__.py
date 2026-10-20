"""SPEC-18 secrets: envelope encryption, two resolution paths, audit, redaction."""

from runtime.secrets.audit import (
    AsyncpgAuditSink,
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
)
from runtime.secrets.crypto import aad_for, decrypt, dek_aad, encrypt
from runtime.secrets.errors import (
    AuditWriteFailed,
    ConfirmationRequired,
    InvalidSecret,
    MasterKeyUnavailable,
    RedactionUnavailable,
    SecretAlreadyExists,
    SecretDecryptDenied,
    SecretError,
    SecretNotFound,
    SecretQuotaExceeded,
)
from runtime.secrets.masterkey import (
    FileMasterKey,
    MasterKeyProvider,
    StaticMasterKey,
    UnavailableMasterKey,
    master_key_from_env,
)
from runtime.secrets.redact import (
    REDACTED,
    RedactingFilter,
    Redactor,
    assert_redaction_available,
    install_log_filter,
    redact,
    redact_text,
)
from runtime.secrets.service import RotationResult, SecretMetadata, SecretService

__all__ = [
    "REDACTED",
    "AsyncpgAuditSink",
    "AuditRecord",
    "AuditSink",
    "AuditWriteFailed",
    "ConfirmationRequired",
    "FileMasterKey",
    "InMemoryAuditSink",
    "InvalidSecret",
    "MasterKeyProvider",
    "MasterKeyUnavailable",
    "RedactingFilter",
    "Redactor",
    "RedactionUnavailable",
    "RotationResult",
    "SecretAlreadyExists",
    "SecretDecryptDenied",
    "SecretError",
    "SecretMetadata",
    "SecretNotFound",
    "SecretQuotaExceeded",
    "SecretService",
    "StaticMasterKey",
    "UnavailableMasterKey",
    "aad_for",
    "assert_redaction_available",
    "decrypt",
    "dek_aad",
    "encrypt",
    "install_log_filter",
    "master_key_from_env",
    "redact",
    "redact_text",
]
