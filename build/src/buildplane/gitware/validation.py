"""Validation for every value that crosses the git boundary.

SPEC-08 R-16 is the reason this module exists: `git_sha` is untrusted input and
is rejected unless it is exactly the provider's hex length. The same discipline
is applied to refs, repository IDs, PR numbers and the preview DNS suffix. None
of these values are ever interpolated into a shell command or a filesystem path
in this package; validation is the second layer, not the only one.
"""

from __future__ import annotations

import re

from buildplane.gitware.errors import (
    InvalidGitRef,
    InvalidGitSha,
    InvalidPreviewDomain,
    InvalidPrNumber,
    InvalidProviderRepoId,
    InvalidVerificationToken,
    WebhookHeaderError,
)

GIT_SHA_RE = re.compile(r"[0-9a-fA-F]+")
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
VERIFICATION_TOKEN_RE = re.compile(r"[a-z0-9]{8,62}")

GITHUB_SHA_LENGTH = 40
MAX_GIT_REF_LENGTH = 255
MAX_PROVIDER_REPO_ID = 2**63 - 1
MAX_PR_NUMBER = 2**31 - 1
MAX_DOMAIN_LENGTH = 253
MAX_DELIVERY_ID_LENGTH = 128
MAX_MASTER_SECRET_LENGTH = 4096
MIN_MASTER_SECRET_BYTES = 16

# Characters git itself forbids in a ref. `*` is allowed only as the explicit
# catch-all rule, never in an event ref.
_REF_FORBIDDEN = frozenset(" ~^:?*[\\")


def validate_git_sha(value: object, *, length: int = GITHUB_SHA_LENGTH) -> str:
    """Return a lowercase hex SHA or raise `InvalidGitSha` (R-16).

    GitHub uses 40 lowercase hex characters. A provider with a different digest
    length can pass `length` explicitly; the hex alphabet stays the same.
    """

    if length < 1 or length > 64:
        raise ValueError("unsupported SHA length")
    if not isinstance(value, str):
        raise InvalidGitSha("git_sha must be a hexadecimal string")
    if len(value) != length or GIT_SHA_RE.fullmatch(value) is None:
        raise InvalidGitSha(
            f"git_sha must be {length} hexadecimal characters"
        )
    # Canonicalise provider output (GitHub sends lowercase, but uppercase is
    # still a hexadecimal SHA). The raw bytes are what the signature covered;
    # this normalisation happens only after verification.
    return value.lower()


def is_zero_sha(value: str) -> bool:
    """True for the all-zero SHA git uses for a deleted ref."""

    return bool(value) and set(value) == {"0"}


def validate_git_ref(value: object) -> str:
    """Validate a full git ref without letting branch names become paths.

    This is intentionally a little stricter than `git check-ref-format`: event
    refs are only used as database values, so rejecting exotic-but-legal names
    is preferable to carrying a parser bug into a later stage.
    """

    if not isinstance(value, str):
        raise InvalidGitRef("git_ref must be a string")
    if not value or len(value) > MAX_GIT_REF_LENGTH:
        raise InvalidGitRef("git_ref has an invalid length")
    if not value.startswith("refs/"):
        raise InvalidGitRef("git_ref must start with refs/")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidGitRef("git_ref contains a control character")
    if any(char in _REF_FORBIDDEN for char in value):
        raise InvalidGitRef("git_ref contains a forbidden character")
    if ".." in value or "@{" in value or "//" in value:
        raise InvalidGitRef("git_ref contains a forbidden sequence")
    for component in value.split("/"):
        if not component or component.startswith(".") or component.endswith(".lock"):
            raise InvalidGitRef("git_ref contains an invalid component")
    return value


def validate_provider_repo_id(value: object) -> int:
    """Validate a numeric provider repo ID (SPEC-08 R-1).

    `bool` is rejected explicitly: it is an `int` subclass in Python and would
    otherwise become repository 0 or 1.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidProviderRepoId("provider_repo_id must be an integer")
    if value < 1 or value > MAX_PROVIDER_REPO_ID:
        raise InvalidProviderRepoId("provider_repo_id is outside the BIGINT range")
    return value


def validate_pr_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPrNumber("pull request number must be an integer")
    if value < 1 or value > MAX_PR_NUMBER:
        raise InvalidPrNumber("pull request number is outside the 32-bit range")
    return value


def validate_preview_domain(value: object) -> str:
    """Return a normalised DNS suffix such as `preview.example.com`."""

    if not isinstance(value, str):
        raise InvalidPreviewDomain("preview domain must be a string")
    domain = value.strip().lower().rstrip(".")
    if not domain or len(domain) > MAX_DOMAIN_LENGTH:
        raise InvalidPreviewDomain("preview domain has an invalid length")
    labels = domain.split(".")
    if len(labels) < 2:
        raise InvalidPreviewDomain("preview domain needs at least two labels")
    for label in labels:
        if len(label) > 63 or DNS_LABEL_RE.fullmatch(label) is None:
            raise InvalidPreviewDomain("preview domain contains an invalid label")
    return domain


def validate_verification_token(value: object) -> str:
    """Tokens become the `_<token>` label of a TXT record, so constrain them."""

    if not isinstance(value, str) or VERIFICATION_TOKEN_RE.fullmatch(value) is None:
        raise InvalidVerificationToken(
            "verification_token must be 8-62 lowercase alphanumeric characters"
        )
    return value


def validate_master_secret(value: object) -> bytes:
    """The HMAC master key used to derive webhook secrets (R-5).

    Key material is injected by the caller (settings/KMS); this module never
    reads the environment or a file. A 32-byte key is the intended production
    size; the lower bound only rejects obviously unusable values.
    """

    if not isinstance(value, (bytes, bytearray)):
        raise ValueError("master_secret must be bytes")
    secret = bytes(value)
    if len(secret) < MIN_MASTER_SECRET_BYTES or len(secret) > MAX_MASTER_SECRET_LENGTH:
        raise ValueError("master_secret has an invalid length")
    return secret


def validate_delivery_id(value: object) -> str:
    """GitHub's `X-GitHub-Delivery` is a GUID; it is the idempotency key."""

    if not isinstance(value, str):
        raise WebhookHeaderError("delivery id must be a string")
    if not value or len(value) > MAX_DELIVERY_ID_LENGTH:
        raise WebhookHeaderError("delivery id has an invalid length")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise WebhookHeaderError("delivery id contains a control character")
    return value
