"""Errors raised by the SPEC-08 git integration domain."""

from __future__ import annotations


class GitwareError(Exception):
    """Base class for gitware failures."""


class InvalidGitSha(GitwareError, ValueError):
    """A SHA that is not the provider's hex format (SPEC-08 R-16)."""


class InvalidGitRef(GitwareError, ValueError):
    """A ref that is not a well-formed git ref."""


class InvalidProviderRepoId(GitwareError, ValueError):
    """A provider repository ID outside the signed BIGINT range."""


class InvalidPrNumber(GitwareError, ValueError):
    """A pull-request number outside the signed 32-bit range."""


class InvalidPreviewDomain(GitwareError, ValueError):
    """A preview suffix that is not a DNS name."""


class InvalidVerificationToken(GitwareError, ValueError):
    """A verification token that cannot be used as a DNS label."""


class WebhookPayloadError(GitwareError, ValueError):
    """A signed webhook whose body is missing or structurally invalid."""


class WebhookHeaderError(GitwareError, ValueError):
    """A required webhook header is missing or malformed."""


class VerificationUnavailable(GitwareError):
    """The DNS or repository API could not answer the ownership question.

    This is deliberately distinct from a negative result: an outage must not
    silently un-verify a healthy project.
    """


class OAuthStateError(GitwareError, ValueError):
    """An OAuth state is malformed, expired, replayed or bound elsewhere."""


class DeploymentBlocked(GitwareError):
    """A deployment was refused before it could be recorded (SPEC-08 R-8)."""

    def __init__(self, code: str, *, status_code: int = 403) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)
